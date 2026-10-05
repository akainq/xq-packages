"""Builds the index of the XQ package registry and its website.

    python tools/build.py [--index out/index] [--site out/site] [--cache .cache] [--check]

The registry is the files `packages/<name>.toml` of this repository, one a package, which say where its git
repository is. A version of a package is a tag `vX.Y.Z` of that repository whose `xq.toml` has the same name and
version, which has a module to import (`lib.xq`, or `[lib] main`) and depends only on packages of the registry, in
versions that are in it.

This script reads the tags of every package and writes the index into the directory `--index`, a checkout of the
branch `index`: a file `<name>.toml` a package, with its versions, their commits, the XQ they are written for and
what they depend on — what `xq` reads. It reads that directory first: a version in the index stays as it is there,
even if its tag is moved later. With `--site`, it also writes the website: the packages with a search, and a page a
package with its versions and README. With `--check` (for pull requests), it writes no index and says what is wrong;
the exit code is 1 if a package cannot be in the registry.

Needs Python 3.11 and git. No other dependencies: the Markdown of READMEs and the highlighting are done here.
"""
import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = "https://github.com/akainq/xq-packages"
XQ_SITE = "https://akainq.github.io/xq/"
DOCS = XQ_SITE + "projects.html#packages"

NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
# Names that are not packages': the standard library's, XQ's, and the website's own files.
RESERVED = {"std", "xq", "index", "assets", "packages"}
TAG = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
REQ = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
PACKAGE_KEYS = {"repository"}


# ---------------------------------------------------------------- versions

def version(s):
    """`"1.2.3"` as (1, 2, 3), or None."""
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", s) if isinstance(s, str) else None
    return tuple(int(x) for x in m.groups()) if m else None


def req(s):
    """The smallest version requirement `s` takes (`"1.2"` takes 1.2.0), or None."""
    m = REQ.fullmatch(s) if isinstance(s, str) else None
    return (int(m[1]), int(m[2]), int(m[3] or 0)) if m else None


def line(v):
    """The versions compatible with `v`: from 1.0 on, those of its first number; before, of its second."""
    return (0, v[1]) if v[0] == 0 else (v[0], 0)


def matches(r, v):
    return v >= r and line(v) == line(r)


def text(v):
    return ".".join(str(x) for x in v)


# ---------------------------------------------------------------- git

class GitError(Exception):
    pass


def git(*args, cwd=None, check=True):
    r = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if check and r.returncode != 0:
        said = [l for l in r.stderr.decode("utf-8", "replace").splitlines() if l.startswith(("fatal:", "error:"))]
        raise GitError("; ".join(said) or r.stderr.decode("utf-8", "replace").strip())
    return r.stdout.decode("utf-8", "replace")


def sanitize(url):
    """A directory name for `url`; a long one keeps its end and a hash of the whole."""
    s = re.sub(r"[^A-Za-z0-9._-]", "_", url.split("://")[-1].rstrip("/").removesuffix(".git"))
    return s if len(s) <= 64 else s[-40:] + "-" + hashlib.sha1(url.encode()).hexdigest()[:16]


def remove(path):
    """Removes directory `path`, and the files git makes read-only in it (on Windows)."""
    def again(fn, p, _):
        os.chmod(p, 0o700)
        fn(p)

    if Path(path).exists():
        shutil.rmtree(path, **({"onexc": again} if sys.version_info >= (3, 12) else {"onerror": again}))


def mirror(url, cache):
    """A bare mirror of repository `url` (without the files of old commits), up to date."""
    d = cache / (sanitize(url) + ".git")
    if (d / "HEAD").is_file():
        git("fetch", "--quiet", "--prune", "--force", "origin", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*", cwd=d)
    else:
        remove(d)
        d.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--quiet", "--bare", "--filter=blob:none", url, str(d))
    return d


def tags(d):
    """The tags of mirror `d`: tag → commit."""
    out = {}
    for row in git("for-each-ref", "--format=%(refname:strip=2) %(objectname) %(*objectname) %(objecttype) %(*objecttype)", "refs/tags", cwd=d).splitlines():
        parts = row.split()
        if len(parts) == 3:  # a tag of a commit: name, commit, "commit"
            if parts[2] == "commit":
                out[parts[0]] = parts[1]
        elif len(parts) == 5 and parts[4] == "commit":  # an annotated tag: its commit second
            out[parts[0]] = parts[2]
    return out


def show(d, commit, path):
    r = subprocess.run(["git", "show", f"{commit}:{path}"], cwd=d, capture_output=True)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


def has_file(d, commit, path):
    row = git("ls-tree", commit, "--", path, cwd=d, check=False).strip()
    return row.split(" ")[1:2] == ["blob"] if row else False


# ---------------------------------------------------------------- packages

class Problems:
    """What is wrong, a package at a time; `fatal` keeps a package out of the registry."""

    def __init__(self):
        self.items = []

    def add(self, name, what, fatal=False):
        self.items.append((name, what, fatal))
        print(f"{'error' if fatal else 'warning'}: {name}: {what}", file=sys.stderr)

    def fatal(self):
        return any(f for _, _, f in self.items)


def candidate(name, d, tag, commit):
    """Version `tag` of package `name` (mirror `d`): its record for the index, or why it cannot be one."""
    v = text(tuple(int(x) for x in TAG.fullmatch(tag).groups()))
    manifest = show(d, commit, "xq.toml")
    if manifest is None:
        return None, "no xq.toml"
    try:
        t = tomllib.loads(manifest)
    except tomllib.TOMLDecodeError as e:
        return None, f"xq.toml: {e}"
    pkg = t.get("package") or {}
    if pkg.get("name") != name:
        return None, f"xq.toml names package {pkg.get('name')!r}, not {name!r}"
    if pkg.get("version") != v:
        return None, f"xq.toml says version {pkg.get('version')!r}, the tag {v}"
    xq = pkg.get("xq")
    if xq is not None and req(xq) is None:
        return None, f"xq = {xq!r} is not a version of XQ, like \"0.10\""
    main = (t.get("lib") or {}).get("main", "lib.xq")
    if not isinstance(main, str) or main.startswith("/") or ".." in Path(main).parts or not has_file(d, commit, main):
        return None, f"no module to import: {main} is not there (lib.xq, or [lib] main = \"…\")"
    deps = {}
    for dep, spec in (t.get("dependencies") or {}).items():
        if isinstance(spec, dict):
            if "git" in spec or "path" in spec:
                where = "git" if "git" in spec else "a directory"
                return None, f"depends on {dep} from {where}: a package of the registry depends only on packages of the registry"
            spec = spec.get("version")
        if req(spec) is None:
            return None, f"dependency {dep} = {spec!r} is not a version, like \"1.2\""
        if dep == name:
            return None, "depends on itself"
        deps[dep] = spec
    date = git("show", "-s", "--format=%cs", commit, cwd=d).strip()
    record = {"version": v, "commit": commit, "date": date, "dependencies": deps}
    if xq is not None:
        record["xq"] = xq
    about = {k: pkg[k] for k in ("description", "license") if isinstance(pkg.get(k), str)}
    return (record, about), None


def read_index(index):
    """The index as it is: name → its entry (with `versions`: version → record)."""
    out = {}
    if not index.is_dir():
        return out
    for f in sorted(index.glob("*.toml")):
        try:
            t = tomllib.loads(f.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            print(f"warning: the index, {f.name}: {e}; read anew", file=sys.stderr)
            continue
        versions = {}
        for r in t.get("version", []):
            rec = {"version": r["version"], "commit": r["commit"], "date": r.get("date", ""), "dependencies": dict(r.get("dependencies", {}))}
            if "xq" in r:
                rec["xq"] = r["xq"]
            versions[r["version"]] = rec
        out[t["name"]] = {k: t[k] for k in ("repository", "description", "license") if k in t} | {"versions": versions}
    return out


def crawl(files, previous, cache, problems, local):
    """The packages of the registry (`files`: their `packages/<name>.toml`), each with the versions it has."""
    packages = {}
    pending = []  # versions new to the index: (name, record), accepted when their dependencies are in it
    for f in files:
        name = f.stem
        if not NAME.fullmatch(name) or name in RESERVED:
            why = "is reserved" if name in RESERVED else "is not a name of a package: a lower case letter, then letters, digits, `-` and `_` (64 at most)"
            problems.add(name, f"packages/{f.name}: {name} {why}", fatal=True)
            continue
        try:
            meta = tomllib.loads(f.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            problems.add(name, f"packages/{f.name}: {e}", fatal=True)
            continue
        unknown = set(meta) - PACKAGE_KEYS
        repo = meta.get("repository")
        schemes = ("https://", "file://") if local else ("https://",)
        if unknown:
            problems.add(name, f"packages/{f.name}: unknown keys {', '.join(sorted(unknown))} (it has only `repository`)", fatal=True)
            continue
        if not isinstance(repo, str) or not repo.startswith(schemes):
            problems.add(name, f"packages/{f.name}: repository = \"https://…\", the address of a public git repository", fatal=True)
            continue
        before = previous.get(name, {})
        try:
            d = mirror(repo, cache)
        except GitError as e:
            if not before.get("versions"):
                problems.add(name, f"cannot fetch {repo}: {e}", fatal=True)
                continue
            # A repository that does not answer now: its versions stay as they are.
            problems.add(name, f"cannot fetch {repo}: {e}; the index keeps the versions it has")
            about = {k: before[k] for k in ("description", "license") if k in before}
            packages[name] = {"name": name, "repository": repo, "mirror": None, "versions": dict(before["versions"]), "about": about}
            continue
        kept = dict(before.get("versions", {}))
        p = {"name": name, "repository": repo, "mirror": d, "versions": kept, "about": {k: before[k] for k in ("description", "license") if k in before}}
        packages[name] = p
        found = tags(d)
        for v, rec in kept.items():
            tag = f"v{v}"
            if tag not in found:
                problems.add(name, f"{tag} is gone from the repository; the index keeps it ({rec['commit'][:12]})")
            elif found[tag] != rec["commit"]:
                problems.add(name, f"{tag} was moved to {found[tag][:12]}; the index keeps {rec['commit'][:12]}: a version does not change")
        for tag, commit in sorted(found.items()):
            m = TAG.fullmatch(tag)
            if not m or text(tuple(int(x) for x in m.groups())) in kept:
                continue
            got, why = candidate(name, d, tag, commit)
            if why:
                problems.add(name, f"{tag}: {why}")
            else:
                pending.append((name, got))
    # A version is in once what it depends on is: the registry has a version of it that suits.
    def suits(dep, r):
        return dep in packages and any(matches(req(r), version(x)) for x in packages[dep]["versions"])

    while True:
        ready = [(n, got) for n, got in pending if all(suits(dep, r) for dep, r in got[0]["dependencies"].items())]
        if not ready:
            break
        for n, (rec, about) in ready:
            packages[n]["versions"][rec["version"]] = rec
            packages[n].setdefault("new", {})[rec["version"]] = about
        pending = [x for x in pending if x not in ready]
    for n, (rec, _) in pending:
        missing = [f"{dep} {r}" for dep, r in rec["dependencies"].items() if not suits(dep, r)]
        problems.add(n, f"v{rec['version']}: depends on {', '.join(missing)}, which the registry does not have")
    for p in packages.values():
        if p["versions"]:
            newest = max(p["versions"], key=version)
            # The description and license are those of the newest version.
            if newest in p.get("new", {}):
                p["about"] = p["new"][newest]
            p["newest"] = newest
        else:
            problems.add(p["name"], "no version yet: tag a commit vX.Y.Z (see the README of the registry)")
    return packages


# ---------------------------------------------------------------- the index

def q(s):
    """A TOML string."""
    s = re.sub(r"[\x00-\x1f\x7f]", " ", s)
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def entry(p):
    out = [
        f"# The index of the XQ package registry ({REGISTRY}), made by tools/build.py from packages/{p['name']}.toml.",
        "",
        f"name = {q(p['name'])}",
        f"repository = {q(p['repository'])}",
    ]
    for k in ("description", "license"):
        if k in p["about"]:
            out.append(f"{k} = {q(p['about'][k])}")
    for v in sorted(p["versions"], key=version):
        rec = p["versions"][v]
        out += ["", "[[version]]", f"version = {q(v)}", f"commit = {q(rec['commit'])}", f"date = {q(rec['date'])}"]
        if "xq" in rec:
            out.append(f"xq = {q(rec['xq'])}")
        if rec["dependencies"]:
            out.append("dependencies = { " + ", ".join(f"{d} = {q(r)}" for d, r in sorted(rec["dependencies"].items())) + " }")
    return "\n".join(out) + "\n"


INDEX_README = f"""# The index of the XQ package registry

This branch is written by `tools/build.py` of the branch `main` of [{REGISTRY}]({REGISTRY}): do not change it by hand.
A file `<name>.toml` a package: its repository, and its versions — the commit of each, the XQ it is written for and
what it depends on. `xq` reads it to choose the versions of packages.
"""


def write_index(index, packages):
    index.mkdir(parents=True, exist_ok=True)
    for f in index.glob("*.toml"):
        if f.stem not in packages:
            f.unlink()
    for name, p in packages.items():
        (index / f"{name}.toml").write_bytes(entry(p).encode())
    (index / "README.md").write_bytes(INDEX_README.encode())


# ---------------------------------------------------------------- highlighting (as the website of XQ does it)

KEYWORDS = {
    "function", "const", "let", "var", "if", "else", "return", "match", "receive", "after", "for", "of", "while",
    "import", "export", "from", "as", "type", "interface", "declare", "library", "in", "out", "inout", "this",
    "true", "false", "null", "typeof", "new", "break", "continue",
}
TYPES = {
    "int", "float", "bool", "string", "void", "unknown", "never", "Pid", "Process", "Result", "Map", "Set", "Array",
    "Uint8Array", "Int8Array", "Uint16Array", "Int16Array", "Uint32Array", "Int32Array", "Float32Array",
    "Float64Array", "Pointer", "Priority", "i8", "i16", "i32", "i64", "u8", "u16", "u32", "u64", "f32", "f64",
}
BUILTINS = {
    "spawn", "spawnLink", "self", "send", "link", "monitor", "trapExit", "exit", "panic", "sleep", "now",
    "setPriority", "whereis", "args", "range", "console", "Math", "JSON", "Binary", "performance", "TextEncoder",
    "TextDecoder",
}
TOKEN = re.compile(
    r"(?P<com>//[^\n]*|/\*.*?\*/)"
    r"|(?P<str>\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)"
    r"|(?P<num>\b0[xX][0-9a-fA-F_]+\b|\b\d[\d_]*(?:\.\d[\d_]*)?(?:[eE][+-]?\d+)?\b)"
    r"|(?P<id>[A-Za-z_$][\w$]*)"
    r"|(?P<op>=>|===|!==|==|!=|<=|>=|&&|\|\||\?\?|\.\.\.|[-+*/%=<>!&|^~?:])"
    r"|(?P<ws>\s+)"
    r"|(?P<other>.)",
    re.S,
)


def span(cls, s):
    return f'<span class="{cls}">{html.escape(s, quote=False)}</span>'


def highlight_xq(code):
    out, prev, i = [], "", 0
    while i < len(code):
        if code[i] == "/" and code[i + 1 : i + 2] not in ("/", "*") and (prev == "" or prev in "(,=:[!&|?{};" or prev in {"return", "=>"}):
            m = re.compile(r"/(?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\\\n\[])+/[a-z]*").match(code, i)
            if m:
                out.append(span("re", m.group()))
                prev, i = m.group(), m.end()
                continue
        m = TOKEN.match(code, i)
        kind, tok = m.lastgroup, m.group()
        i = m.end()
        if kind == "id":
            after = code[i:].lstrip(" ")
            if tok in KEYWORDS:
                out.append(span("kw", tok))
            elif tok in TYPES or (tok[0].isupper() and not tok.isupper()):
                out.append(span("ty", tok))
            elif tok in BUILTINS:
                out.append(span("bi", tok))
            elif after.startswith(("(", "<")):
                out.append(span("fn", tok))
            else:
                out.append(html.escape(tok, quote=False))
        elif kind in ("com", "str", "num", "op"):
            out.append(span(kind, tok))
        else:
            out.append(html.escape(tok, quote=False))
        if kind != "ws":
            prev = tok if kind in ("id", "op", "other") else "x"
    return "".join(out)


def highlight_shell(code):
    out = []
    for row in code.split("\n"):
        m = re.match(r"(\s*)(\$ )?(.*?)(\s+#.*)?$", row)
        lead, prompt, cmd, comment = m.group(1), m.group(2) or "", m.group(3), m.group(4) or ""
        out.append(html.escape(lead) + (span("op", prompt) if prompt else "") + html.escape(cmd) + (span("com", comment) if comment else ""))
    return "\n".join(out)


def toml_comment(row):
    quote = None
    for i, c in enumerate(row):
        if quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#":
            return i
    return len(row)


def highlight_toml(code):
    out = []
    for row in code.split("\n"):
        cut = toml_comment(row)
        body = row[:cut]
        if re.match(r"\s*\[", body):
            s = span("ty", body)
        else:
            m = re.match(r"(\s*)([\w.\-]+)(\s*=\s*)(.*)$", body)
            if m:
                value = re.sub(
                    r"(\"[^\"]*\"|'[^']*')|\b(true|false)\b|(\b\d[\d.]*\b)",
                    lambda v: span("str", v.group(1)) if v.group(1) else span("kw", v.group(2)) if v.group(2) else span("num", v.group(3)),
                    html.escape(m.group(4), quote=False),
                )
                s = html.escape(m.group(1)) + span("fn", m.group(2)) + html.escape(m.group(3)) + value
            else:
                s = html.escape(body)
        out.append(s + (span("com", row[cut:]) if cut < len(row) else ""))
    return "\n".join(out)


def highlight(code, lang):
    if lang in ("ts", "xq", "typescript", "js", "javascript"):
        return highlight_xq(code)
    if lang in ("bash", "sh", "shell", "console"):
        return highlight_shell(code)
    if lang == "toml":
        return highlight_toml(code)
    return html.escape(code, quote=False)


def code_block(code, lang):
    return f'<pre class="code" data-lang="{html.escape(lang or "text")}"><code>{highlight(code, lang)}</code></pre>'


# ---------------------------------------------------------------- READMEs: Markdown, from strangers

class Readme:
    """The Markdown of a package's README as HTML: the common part of Markdown, everything escaped, links and images
    only to the web (relative ones to the repository at the version's commit)."""

    def __init__(self, name, repository, commit):
        gh = re.match(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", repository)
        self.blob = f"https://github.com/{gh[1]}/blob/{commit}/" if gh else None
        self.raw = f"https://raw.githubusercontent.com/{gh[1]}/{commit}/" if gh else None
        # The heading `# name` the README starts with: the page has it already.
        self.title = name
        self.html = []

    def images(self, tags):
        """The `<img>` of a line of HTML tags, as images of the README (the rest of the tags is left out)."""
        def attr(tag, key):
            m = re.search(r"\b" + key + r"""\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", tag, re.I)
            return next((g for g in m.groups() if g is not None), None) if m else None

        out = []
        for tag in re.findall(r"<img\s[^>]*>", tags, re.I):
            src, alt, width = attr(tag, "src"), attr(tag, "alt") or "", attr(tag, "width")
            u = self.url(src, image=True) if src else None
            if u:
                w = f' width="{int(width)}"' if width and width.isdigit() else ""
                out.append(f'<img src="{html.escape(u)}" alt="{html.escape(alt)}"{w} loading="lazy">')
        return out

    def url(self, u, image=False):
        """Where a link (or an image) of the README goes: the web as it is, the repository's own files at the commit,
        nowhere else (no `javascript:`)."""
        u = html.unescape(u)
        if u.startswith(("https://", "http://")) or (u.startswith("mailto:") and not image) or (u.startswith("#") and not image):
            return u
        if re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*:", u) or u.startswith("//"):
            return None
        while u.startswith("./"):
            u = u[2:]
        base = self.raw if image else self.blob
        if not base:
            return None
        u = base + u.lstrip("/")
        # raw.githubusercontent.com gives an SVG as an image only when asked to sanitize it.
        return u + "?sanitize=true" if image and u.lower().endswith(".svg") else u

    def inline(self, s):
        parts = re.split(r"(`+)(.+?)\1", s)
        out = []
        for k in range(0, len(parts), 3):
            out.append(self.inline_text(parts[k]))
            if k + 2 < len(parts):
                out.append("<code>" + html.escape(parts[k + 2].strip() if parts[k + 1] == "``" else parts[k + 2], quote=False) + "</code>")
        return "".join(out)

    def inline_text(self, s):
        s = html.escape(s, quote=False)
        # The tags made here are kept aside: emphasis and bare addresses are looked for in the text alone.
        kept = []

        def keep(tag):
            kept.append(tag)
            return f"\x00{len(kept) - 1}\x00"

        def image(m):
            u = self.url(m.group(2), image=True)
            alt = html.escape(html.unescape(m.group(1)))
            return keep(f'<img src="{html.escape(u)}" alt="{alt}" loading="lazy">') if u else alt

        def link(m):
            u = self.url(m.group(2))
            if not u:
                return m.group(1)
            ext = ' rel="nofollow noopener"' if u.startswith("http") else ""
            return keep(f'<a href="{html.escape(u)}"{ext}>{re.sub(chr(0) + r"(\d+)" + chr(0), lambda k: kept[int(k.group(1))], m.group(1))}</a>')

        def bare(m):
            url = m.group().rstrip(".,;:!?")
            return keep(f'<a href="{url}" rel="nofollow noopener">{url}</a>') + m.group()[len(url):]

        title = r'(?:\s+"[^"]*")?'
        s = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)" + title + r"\)", image, s)
        s = re.sub(r"\[((?:\x00\d+\x00|[^\]])+)\]\(([^)\s]+)" + title + r"\)", link, s)
        s = re.sub(r"\bhttps://[^\s<>\x00)]+", bare, s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", s)
        s = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?![\w_])", r"<em>\1</em>", s)
        return re.sub(r"\x00(\d+)\x00", lambda m: kept[int(m.group(1))], s)

    def render(self, md):
        rows, comment = [], False
        for r in md.replace("\r\n", "\n").split("\n"):
            # HTML is left out: comments, and the lines that are only tags (the wrappers of badges and logos).
            if comment or r.lstrip().startswith("<!--"):
                comment = "-->" not in r
                continue
            if re.fullmatch(r"\s*(</?[a-zA-Z][^>]*>\s*)+", r):
                imgs = self.images(r)
                if imgs:
                    self.html.append('<p class="figure">' + " ".join(imgs) + "</p>")
                    rows.append(f"{len(self.html) - 1}")
            else:
                rows.append(r)
        out, buf, i = [], [], 0

        def para():
            if buf:
                out.append("<p>" + self.inline(" ".join(s.strip() for s in buf)) + "</p>")
                buf.clear()

        while i < len(rows):
            row = rows[i]
            if row.startswith(""):
                para()
                out.append(self.html[int(row[1:])])
                i += 1
                continue
            fence = re.match(r"\s*(```|~~~)\s*([\w+-]*)", row)
            if fence:
                para()
                code, i = [], i + 1
                while i < len(rows) and not rows[i].strip().startswith(fence.group(1)):
                    code.append(rows[i])
                    i += 1
                out.append(code_block("\n".join(code), fence.group(2).lower()))
                i += 1
                continue
            m = re.match(r"(#{1,6})\s+(.+?)\s*#*\s*$", row)
            if m:
                para()
                level = min(len(m.group(1)) + 1, 6)  # the page's own heading is the h1
                title = m.group(2)
                if self.title and not out and title.strip().lower() == self.title:
                    self.title = None
                    i += 1
                    continue
                self.title = None
                anchor = re.sub(r"[^\w\- ]", "", re.sub(r"<[^>]+>", "", title).strip().lower()).replace(" ", "-")
                out.append(f'<h{level} id="{html.escape(anchor)}">{self.inline(title)}</h{level}>')
                i += 1
                continue
            if re.fullmatch(r"\s*([-*_])(\s*\1){2,}\s*", row):
                para()
                out.append("<hr>")
                i += 1
                continue
            if row.startswith("|") and i + 1 < len(rows) and re.match(r"\s*\|?\s*:?-{3,}", rows[i + 1]):
                para()
                head = cells(row)
                i += 2
                body = []
                while i < len(rows) and rows[i].startswith("|"):
                    body.append(cells(rows[i]))
                    i += 1
                t = ['<div class="table"><table><thead><tr>'] + [f"<th>{self.inline(c)}</th>" for c in head] + ["</tr></thead><tbody>"]
                t += ["<tr>" + "".join(f"<td>{self.inline(c)}</td>" for c in r) + "</tr>" for r in body]
                out.append("".join(t) + "</tbody></table></div>")
                continue
            if row.startswith(">"):
                para()
                quote = []
                while i < len(rows) and rows[i].startswith(">"):
                    quote.append(rows[i][1:].lstrip())
                    i += 1
                out.append("<blockquote>" + self.render("\n".join(quote)) + "</blockquote>")
                continue
            if re.match(r"\s*([-*+]|\d+[.)]) ", row):
                para()
                block = []
                while i < len(rows) and rows[i].strip() and (re.match(r"\s*([-*+]|\d+[.)]) ", rows[i]) or rows[i].startswith(" ")):
                    block.append(rows[i])
                    i += 1
                out.append(self.listing(block))
                continue
            if not row.strip():
                para()
                i += 1
                continue
            # A heading underlined with === or ---.
            if not buf and i + 1 < len(rows) and re.fullmatch(r"(=+|-+)\s*", rows[i + 1]):
                level = 2 if rows[i + 1].startswith("=") else 3
                anchor = re.sub(r"[^\w\- ]", "", row.strip().lower()).replace(" ", "-")
                out.append(f'<h{level} id="{html.escape(anchor)}">{self.inline(row.strip())}</h{level}>')
                i += 2
                continue
            buf.append(row)
            i += 1
        para()
        return "\n".join(out)

    def listing(self, block):
        indent = len(block[0]) - len(block[0].lstrip())
        ordered = bool(re.match(r"\s*\d+[.)] ", block[0]))
        items = []
        for row in block:
            lead = len(row) - len(row.lstrip())
            if lead <= indent and re.match(r"\s*([-*+]|\d+[.)]) ", row):
                items.append([re.sub(r"^\s*([-*+]|\d+[.)]) ", "", row)])
            elif items:
                items[-1].append(row)
        out = ["<ol>" if ordered else "<ul>"]
        for item in items:
            words, nested = [item[0]], []
            for row in item[1:]:
                if nested or re.match(r"\s*([-*+]|\d+[.)]) ", row):
                    nested.append(row)
                else:
                    words.append(row)
            first = " ".join(s.strip() for s in words)
            box = re.match(r"\[([ xX])\] (.*)", first)
            body = (("☑ " if box.group(1) != " " else "☐ ") + self.inline(box.group(2))) if box else self.inline(first)
            out.append(f"<li>{body}{self.listing(nested) if nested else ''}</li>")
        out.append("</ol>" if ordered else "</ul>")
        return "".join(out)


def cells(row):
    out, cur, ticks = [], "", False
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    i = 0
    while i < len(row):
        c = row[i]
        if c == "\\" and row[i + 1 : i + 2] == "|":
            cur += "|"
            i += 2
            continue
        if c == "`":
            ticks = not ticks
        if c == "|" and not ticks:
            out.append(cur.strip())
            cur = ""
        else:
            cur += c
        i += 1
    out.append(cur.strip())
    return out


# ---------------------------------------------------------------- the website

def e(s):
    return html.escape(str(s))


def page(title, description, body, count, updated, kind=""):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<meta name="description" content="{e(description)}">
<link rel="icon" href="assets/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="assets/style.css">
<script src="assets/site.js" defer></script>
</head>
<body class="registry {kind}">

<header class="top">
  <div class="wrap">
    <a class="brand" href="./"><img src="assets/logo.svg" alt="" width="28" height="28"><span>XQ packages</span></a>
    <nav>
      <a href="{XQ_SITE}">XQ</a>
      <a href="{DOCS}">Docs</a>
      <a href="{REGISTRY}#adding-a-package">Add a package</a>
      <a href="{REGISTRY}">GitHub</a>
    </nav>
  </div>
</header>

{body}

<footer class="bottom">
  <div class="wrap">
    <span>{count} package{'' if count == 1 else 's'}</span>
    {f'<span>Updated {e(updated)}</span>' if updated else ''}
    <a href="{REGISTRY}">github.com/akainq/xq-packages</a>
  </div>
</footer>
</body>
</html>
"""


def short_repo(url):
    return re.sub(r"^https?://", "", url).removesuffix(".git").rstrip("/")


def commit_link(repo, commit):
    gh = re.match(r"https://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$", repo)
    c = f"<code>{e(commit[:10])}</code>"
    return f'<a href="https://github.com/{gh[1]}/commit/{e(commit)}">{c}</a>' if gh else c


def write_site(site, packages):
    if site.exists():
        shutil.rmtree(site)
    (site / "assets").mkdir(parents=True)
    for f in ["style.css", "site.js", "logo.svg", "favicon.svg"]:
        shutil.copyfile(ROOT / "site" / f, site / "assets" / f)
    (site / ".nojekyll").write_bytes(b"")
    listed = sorted((p for p in packages.values() if p.get("newest")), key=lambda p: p["name"])
    count = len(listed)
    dates = [rec["date"] for p in listed for rec in p["versions"].values() if rec.get("date")]
    updated = max(dates) if dates else ""
    users = {}
    for p in listed:
        for d in p["versions"][p["newest"]]["dependencies"]:
            users.setdefault(d, []).append(p["name"])

    # The list, with a search.
    items = []
    for p in listed:
        newest = p["versions"][p["newest"]]
        desc = p["about"].get("description", "")
        lic = p["about"].get("license")
        meta = " · ".join(x for x in [e(lic) if lic else "", f"updated {e(newest['date'])}" if newest.get("date") else ""] if x)
        items.append(
            f'<li data-text="{e((p["name"] + " " + desc).lower())}"><a href="{e(p["name"])}.html">'
            f'<b>{e(p["name"])}</b><span class="ver">{e(p["newest"])}</span>'
            f'<p>{e(desc) if desc else "<i>No description.</i>"}</p><span class="meta">{meta}</span></a></li>'
        )
    if items:
        listing = f'<ul class="pkgs" id="list">{"".join(items)}</ul><p class="empty" hidden>No packages match.</p>'
    else:
        listing = (
            '<div class="first-pkg"><h2>No packages yet</h2><p>The registry has just opened. A package is a public git '
            f'repository with an <code>xq.toml</code> and a tag <code>v0.1.0</code>: <a href="{REGISTRY}#adding-a-package">add '
            "yours</a>.</p></div>"
        )
    body = f"""<main class="wrap">
<section class="reg-hero">
  <h1>XQ <span class="grad">packages</span></h1>
  <p class="lede">Libraries for <a href="{XQ_SITE}">XQ</a> from public git repositories. A project uses one with
    <code>xq add name</code>; <a href="{DOCS}">how packages work</a>.</p>
  {'<input id="q" class="search" type="search" placeholder="Search packages" autocomplete="off" aria-label="Search packages">' if items else ''}
</section>
{listing}
</main>"""
    (site / "index.html").write_bytes(page("XQ packages", "The package registry of the XQ programming language.", body, count, updated).encode())

    # A page a package.
    for p in listed:
        name, newest = p["name"], p["versions"][p["newest"]]
        desc = p["about"].get("description", "")
        r = req(p["newest"])
        want = f"{r[0]}.{r[1]}" if r[2] == 0 else p["newest"]
        rows = []
        for v in sorted(p["versions"], key=version, reverse=True):
            rec = p["versions"][v]
            deps = ", ".join(f'<a href="{e(d)}.html">{e(d)}</a> {e(x)}' for d, x in sorted(rec["dependencies"].items())) or "—"
            rows.append(
                f"<tr><td><b>{e(v)}</b></td><td>{e(rec.get('date', ''))}</td><td>{e(rec.get('xq', '—'))}</td><td>{deps}</td>"
                f"<td>{commit_link(p['repository'], rec['commit'])}</td></tr>"
            )
        readme = None
        for f in ("README.md", "readme.md", "Readme.md", "README") if p["mirror"] else ():
            readme = show(p["mirror"], newest["commit"], f)
            if readme is not None:
                break
        readme_html = Readme(name, p["repository"], newest["commit"]).render(readme) if readme else "<p><i>No README.</i></p>"
        meta = [("Repository", f'<a href="{e(p["repository"])}" rel="noopener">{e(short_repo(p["repository"]))}</a>')]
        if p["about"].get("license"):
            meta.append(("License", e(p["about"]["license"])))
        if newest.get("xq"):
            meta.append(("XQ", e(newest["xq"])))
        meta.append(("Published", e(newest.get("date", ""))))
        deps = newest["dependencies"]
        meta.append(("Dependencies", ", ".join(f'<a href="{e(d)}.html">{e(d)}</a>' for d in sorted(deps)) or "none"))
        if users.get(name):
            meta.append(("Used by", ", ".join(f'<a href="{e(u)}.html">{e(u)}</a>' for u in sorted(users[name]))))
        dl = "".join(f"<dt>{k}</dt><dd>{v}</dd>" for k, v in meta)
        body = f"""<main class="wrap pkg-grid">
<div class="pkg-main content">
  <p class="crumbs"><a href="./">Packages</a> / {e(name)}</p>
  <h1>{e(name)} <span class="ver">{e(p['newest'])}</span></h1>
  {f'<p class="lede">{e(desc)}</p>' if desc else ''}
  <h2 id="use">Use it</h2>
  {code_block(f"xq add {name}", "bash")}
  <p>or in <code>xq.toml</code>:</p>
  {code_block(f'[dependencies]{chr(10)}{name} = "{want}"', "toml")}
  <h2 id="versions">Versions</h2>
  <div class="table"><table><thead><tr><th>Version</th><th>Date</th><th>XQ</th><th>Dependencies</th><th>Commit</th></tr></thead>
  <tbody>{''.join(rows)}</tbody></table></div>
  <h2 id="readme">README</h2>
  <div class="readme">{readme_html}</div>
</div>
<aside class="pkg-meta"><dl>{dl}</dl></aside>
</main>"""
        title = f"{name} — XQ packages"
        (site / f"{name}.html").write_bytes(page(title, desc or f"The XQ package {name}.", body, count, updated, "package").encode())

    # For tools: what is in the registry.
    data = [
        {"name": p["name"], "version": p["newest"], "description": p["about"].get("description"), "repository": p["repository"]}
        for p in listed
    ]
    (site / "packages.json").write_bytes(json.dumps(data, ensure_ascii=False, indent=1).encode())


# ---------------------------------------------------------------- main

def main():
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--packages", default=str(ROOT / "packages"), help="the files of the packages (packages/)")
    ap.add_argument("--index", default=str(ROOT / "out" / "index"), help="the index: read, then written (a checkout of the branch index)")
    ap.add_argument("--site", default=None, help="where to write the website")
    ap.add_argument("--cache", default=str(ROOT / ".cache"), help="where to keep the mirrors of the repositories")
    ap.add_argument("--check", action="store_true", help="check the packages only: write no index")
    ap.add_argument("--local", action="store_true", help="let repositories be file:// (for tests)")
    a = ap.parse_args()
    files = sorted(Path(a.packages).glob("*.toml"))
    index = Path(a.index)
    problems = Problems()
    packages = crawl(files, read_index(index), Path(a.cache) / "git", problems, a.local)
    if not a.check:
        write_index(index, packages)
    if a.site:
        write_site(Path(a.site), packages)
    versions = sum(len(p["versions"]) for p in packages.values())
    print(f"{len(packages)} packages, {versions} versions")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary and problems.items:
        with open(summary, "a", encoding="utf-8") as f:
            f.write("| Package | Problem |\n|---|---|\n")
            for name, what, fatal in problems.items:
                cell = ("**error**: " if fatal else "") + what.replace("|", "\\|")
                f.write(f"| {name} | {cell} |\n")
    if a.check and (problems.fatal() or any(not p["versions"] for p in packages.values())):
        sys.exit(1)


if __name__ == "__main__":
    main()
