# XQ packages

The package registry of [XQ](https://akainq.github.io/xq/): libraries from public git repositories, under short
names. The website, with a search: **[akainq.github.io/xq-packages](https://akainq.github.io/xq-packages/)**.

## Using a package

In a project (a directory with `xq.toml`):

```bash
xq add qr          # the newest version of qr, into [dependencies] of xq.toml
xq update          # the newest versions in their lines (1.x stays 1.x)
```

```toml
[dependencies]
qr = "1.2"         # 1.2.0 or newer, before 2.0
```

```ts
import { encode } from "qr";
```

`xq build`, `xq run`, `xq check` and `xq test` fetch the packages with git and write the versions chosen to `xq.lock`
(exact commits; commit it). A program has one version of a package: the smallest one everyone asking for it
accepts. More in [the reference](https://akainq.github.io/xq/projects.html#packages).

## Adding a package

A package is a public git repository (GitHub or any other host reachable over `https://`) with:

- `xq.toml` at its root, with the package's name, version and the XQ it is written for:

  ```toml
  [package]
  name = "qr"
  version = "0.1.0"
  xq = "0.10"
  description = "QR codes: encoding, and drawing them as text or images"
  repository = "https://github.com/someone/qr"
  license = "MIT"

  [dependencies]
  util = "1.1"     # packages of the registry only: no git = or path =
  ```

- a module to import: `lib.xq` at the root, or the one `[lib] main = "src/qr.xq"` names; the other modules of the
  package are imported as `"qr/name"`, relative to it;
- a tag `v0.1.0` on the commit with that version in `xq.toml`;
- a README (`README.md`), shown on the package's page.

Then open a pull request that adds one file, `packages/qr.toml`:

```toml
repository = "https://github.com/someone/qr"
```

The name is the file's: a lower case letter, then lower case letters, digits, `-` and `_`, 64 at most. Names are
first come, first served; `std`, `xq`, `index`, `assets` and `packages` are reserved. A check on the pull request
reads the repository and says what is wrong, if anything. Once it is merged, the package is in the registry within
minutes.

## Publishing a version

Change the version in `xq.toml`, commit, and tag the commit:

```bash
git tag v0.2.0
git push origin v0.2.0
```

The registry reads the tags of every package every hour. A version is taken if its `xq.toml` has the package's name
and the tag's version, it has its module, and the registry has the packages it depends on, in versions that suit.
Once in, a version never changes: moving its tag later changes nothing (publish a new version to fix a mistake).
Versions follow the lines of [semantic versioning](https://semver.org): from 1.0 on, a version that breaks what
used to work changes the first number; before 1.0, the second one.

## How it works

`tools/build.py` (Python 3.11 and git, nothing else) runs on GitHub Actions on every change of this branch and
every hour. For every `packages/<name>.toml`, it reads the tags `vX.Y.Z` of the repository and checks the new
versions, then:

- writes the **index** to the branch [`index`](../../tree/index): a file `<name>.toml` a package, with its repository and
  versions — the commit of each, the XQ it is written for and what it depends on. This is what `xq` reads (it
  fetches the branch with git: `XQ_REGISTRY` names another registry);
- builds the **website** — the list with a search, a page a package with its versions and README — and puts it on
  GitHub Pages. `packages.json` there lists the packages for tools.

The index of a package:

```toml
name = "qr"
repository = "https://github.com/someone/qr"
description = "QR codes: encoding, and drawing them as text or images"
license = "MIT"

[[version]]
version = "0.1.0"
commit = "5ffbceb884a6488a2fccbacf736dcd7a179a0b93"
date = "2026-10-05"
xq = "0.10"
dependencies = { util = "1.1" }
```

To try a change of `tools/build.py` or of the website:

```bash
python tools/build.py --index out/index --site out/site
python tools/test_build.py
```

## License

The tools and the website of the registry: MIT ([LICENSE](LICENSE)). Every package has its own license, which its
page shows.
