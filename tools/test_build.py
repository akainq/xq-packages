"""Tests of tools/build.py: packages in git repositories made here, the index and the website built from them.

    python tools/test_build.py
"""
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

BUILD = Path(__file__).resolve().parent / "build.py"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "xq", "GIT_AUTHOR_EMAIL": "xq@example.com", "GIT_COMMITTER_NAME": "xq", "GIT_COMMITTER_EMAIL": "xq@example.com"}


def git(d, *args):
    r = subprocess.run(["git", "-c", "init.defaultBranch=main", "-c", "core.autocrlf=false", *args], cwd=d, env=ENV, capture_output=True, text=True)
    assert r.returncode == 0, f"git {args}: {r.stderr}"
    return r.stdout.strip()


def write(d, files):
    for path, body in files.items():
        p = d / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body.encode())


def release(d, tag, files, annotated=False):
    write(d, files)
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", tag)
    git(d, "tag", *(["-a", "-m", tag] if annotated else []), tag)
    return git(d, "rev-parse", "HEAD")


def manifest(name, v, deps="", extra=""):
    return f'[package]\nname = "{name}"\nversion = "{v}"\nxq = "0.10"\ndescription = "{name} {v}"\nlicense = "MIT"\n{extra}\n[dependencies]\n{deps}'


def url(p):
    s = p.as_posix()
    return "file://" + s if s.startswith("/") else "file:///" + s


def main():
    tmp = Path(tempfile.mkdtemp(prefix="xq-registry-"))
    try:
        run(tmp)
    finally:
        sys.path.insert(0, str(BUILD.parent))
        import build

        build.remove(tmp)
    print("ok")


def build(tmp, *more):
    return subprocess.run(
        [sys.executable, str(BUILD), "--packages", str(tmp / "packages"), "--index", str(tmp / "index"), "--site", str(tmp / "site"),
         "--cache", str(tmp / "cache"), "--local", *more],
        capture_output=True, text=True, encoding="utf-8",
    )


def run(tmp):
    util = tmp / "repos" / "util"
    util.mkdir(parents=True)
    git(util, "init", "-q")
    lib = "export function twice(n: int): int {\n  return n * 2;\n}\n"
    u1 = release(util, "v1.0.0", {"xq.toml": manifest("util", "1.0.0"), "lib.xq": lib})
    u11 = release(util, "v1.1.0", {"xq.toml": manifest("util", "1.1.0")}, annotated=True)
    release(util, "v1.2.0", {"xq.toml": manifest("util", "1.2.1")})  # says another version
    git(util, "rm", "-q", "lib.xq")
    release(util, "v2.0.0", {"xq.toml": manifest("util", "2.0.0")})  # nothing to import
    git(util, "tag", "release-1")  # not a version

    greet = tmp / "repos" / "greet"
    greet.mkdir(parents=True)
    git(greet, "init", "-q")
    readme = (
        '<p align="center">\n<img src="logo.png">\n</p>\n<!-- a comment\nover lines -->\n\n'
        "# greet\n\n[![badge](https://img.shields.io/badge/x-y-green)](https://example.com/ci) Says **hello**.\n\n"
        "See [the guide](docs/guide.md), [a script](javascript:alert(1)) and https://example.com/a_b_c.\n\n"
        "Usage\n-----\n\n```ts\nimport { greet } from \"greet\";\n```\n\n- one\n- two\n  - deeper\n\n> quoted <b>not bold</b>\n"
    )
    files = {"xq.toml": manifest("greet", "0.1.0", 'util = "1.1"\n', '\n[lib]\nmain = "src/greet.xq"\n'), "src/greet.xq": "export const x = 1;\n", "README.md": readme}
    g1 = release(greet, "v0.1.0", files)
    release(greet, "v0.2.0", {"xq.toml": manifest("greet", "0.2.0", 'util = "3.0"\n', '\n[lib]\nmain = "src/greet.xq"\n')})  # util 3 is not there
    release(greet, "v0.3.0", {"xq.toml": manifest("greet", "0.3.0", f'util = {{ git = "{url(util)}", tag = "v1.0.0" }}\n', '\n[lib]\nmain = "src/greet.xq"\n')})

    write(tmp / "packages", {"util.toml": f'repository = "{url(util)}"\n', "greet.toml": f'repository = "{url(greet)}"\n'})
    r = build(tmp)
    assert r.returncode == 0, r.stderr
    log = r.stderr
    for want in ["util: v1.2.0: xq.toml says version '1.2.1'", "util: v2.0.0: no module to import", "greet: v0.2.0: depends on util 3.0",
                 "greet: v0.3.0: depends on util from git"]:
        assert want in log, (want, log)

    index = {f.stem: tomllib.loads(f.read_text(encoding="utf-8")) for f in (tmp / "index").glob("*.toml")}
    assert [v["version"] for v in index["util"]["version"]] == ["1.0.0", "1.1.0"], index["util"]
    assert [v["commit"] for v in index["util"]["version"]] == [u1, u11]
    assert index["util"]["description"] == "util 1.1.0"
    g = index["greet"]["version"]
    assert [(v["version"], v["commit"], v["dependencies"]) for v in g] == [("0.1.0", g1, {"util": "1.1"})], g
    assert (tmp / "index" / "README.md").is_file()

    site = tmp / "site"
    home = (site / "index.html").read_text(encoding="utf-8")
    assert 'href="greet.html"' in home and 'href="util.html"' in home and "2 packages" in home
    page = (site / "greet.html").read_text(encoding="utf-8")
    assert "javascript:" not in page and "<b>not bold</b>" not in page and "&lt;b&gt;not bold&lt;/b&gt;" in page, page
    assert '<img src="https://img.shields.io/badge/x-y-green" alt="badge" loading="lazy">' in page
    assert '<a href="https://example.com/a_b_c" rel="nofollow noopener">https://example.com/a_b_c</a>.' in page
    assert "the guide" in page and 'href="docs/guide.md"' not in page  # not a repository of GitHub: no link
    assert '<h3 id="usage">Usage</h3>' in page and "<strong>hello</strong>" in page and "comment" not in page
    assert '<span class="kw">import</span>' in page and "<li>two<ul><li>deeper</li></ul></li>" in page
    assert 'href="util.html"' in page  # the dependency
    assert 'Used by</dt><dd><a href="greet.html">greet</a>' in (site / "util.html").read_text(encoding="utf-8")

    # A version does not change: a moved tag is noticed, and that is all. New versions come in, and those that
    # waited for a dependency once it is there.
    git(util, "tag", "-f", "v1.0.0", "HEAD")
    release(util, "v3.0.0", {"xq.toml": manifest("util", "3.0.0"), "lib.xq": lib})
    r = build(tmp)
    assert r.returncode == 0, r.stderr
    assert "v1.0.0 was moved" in r.stderr, r.stderr
    index = {f.stem: tomllib.loads(f.read_text(encoding="utf-8")) for f in (tmp / "index").glob("*.toml")}
    assert [(v["version"], v["commit"]) for v in index["util"]["version"]][:2] == [("1.0.0", u1), ("1.1.0", u11)]
    assert [v["version"] for v in index["util"]["version"]] == ["1.0.0", "1.1.0", "3.0.0"]
    assert [v["version"] for v in index["greet"]["version"]] == ["0.1.0", "0.2.0"]

    # A repository that does not answer: the index keeps what it has.
    moved = util.with_name("util-moved")
    util.rename(moved)
    build_py = BUILD.parent
    sys.path.insert(0, str(build_py))
    import build as b

    b.remove(tmp / "cache")
    r = build(tmp)
    assert r.returncode == 0 and "util: cannot fetch" in r.stderr and "keeps the versions it has" in r.stderr, r.stderr
    index = {f.stem: tomllib.loads(f.read_text(encoding="utf-8")) for f in (tmp / "index").glob("*.toml")}
    assert [v["version"] for v in index["util"]["version"]] == ["1.0.0", "1.1.0", "3.0.0"]
    moved.rename(util)

    # A package taken out of the registry leaves the index.
    (tmp / "packages" / "greet.toml").unlink()
    assert build(tmp).returncode == 0
    assert not (tmp / "index" / "greet.toml").exists()

    # The check of a pull request: a bad name, a repository that is not there, a package without versions.
    write(tmp / "packages", {"Bad.toml": f'repository = "{url(util)}"\n'})
    r = build(tmp, "--check")
    assert r.returncode == 1 and "Bad is not a name of a package" in r.stderr, r.stderr
    (tmp / "packages" / "Bad.toml").unlink()
    write(tmp / "packages", {"gone.toml": f'repository = "{url(tmp / "repos" / "gone")}"\n'})
    r = build(tmp, "--check")
    assert r.returncode == 1 and "gone: cannot fetch" in r.stderr, r.stderr
    (tmp / "packages" / "gone.toml").unlink()
    empty = tmp / "repos" / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    release(empty, "start", {"xq.toml": manifest("empty", "0.1.0"), "lib.xq": lib})
    write(tmp / "packages", {"empty.toml": f'repository = "{url(empty)}"\n'})
    r = build(tmp, "--check")
    assert r.returncode == 1 and "empty: no version yet" in r.stderr, r.stderr
    git(empty, "tag", "v0.1.0")
    r = build(tmp, "--check")
    assert r.returncode == 0, r.stderr
    # Without --local, only https://.
    r = subprocess.run([sys.executable, str(BUILD), "--packages", str(tmp / "packages"), "--index", str(tmp / "index"), "--cache", str(tmp / "cache"), "--check"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 1 and 'repository = "https://…"' in r.stderr, r.stderr


if __name__ == "__main__":
    main()
