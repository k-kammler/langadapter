# Install the toolchains

`langadapter toolchains` installs the tools a language's projects need on
Ubuntu 24.04 on x86_64: on the computer itself, in WSL with Ubuntu 24.04 and
in an Ubuntu 24.04 container. Each language's part ends with `langadapter check` on a small
example, so the command shows that what it installed works.

## Install with the recipe

Name the languages, or `all` for every language that has a part on this
system:

```
langadapter toolchains python java
```

The parts install below `~/.local/share/langadapter/toolchains`, or below
`$XDG_DATA_HOME/langadapter/toolchains` where that variable is set, unless
`--into DIR` names another directory. The command runs the parts in the order
named and stops at the first part that fails, with that part's status. It
ends with one line per language that a shell reads to find the tools:

```
. /home/user/.local/share/langadapter/toolchains/python.env
. /home/user/.local/share/langadapter/toolchains/java.env
```

Add these lines to the shell's start file, such as `~/.bashrc`, so later
shells find the tools too. An install directory that holds a double quote, a
dollar sign, a backtick, a backslash, a line break or a colon is refused,
since a shell reads it in these files and a colon splits their `PATH` line.

`--list` shows each part and what it downloads, and runs nothing. It too
needs a system with parts, Ubuntu 24.04, also in WSL: on another system the
command says so, as for an install.

```
langadapter toolchains all --list
```

## What each part installs

| Language | Installs | How runs find it |
| --- | --- | --- |
| python | uv, CPython through uv, and an environment with pytest, coverage.py and Cosmic Ray at the versions LangAdapter pins | `python.env` puts uv on `PATH`. A project keeps its own interpreter, or names `python_version`, and LangAdapter provisions an environment with uv ([Python projects](python.md)). |
| java | a Temurin JDK and Maven | `java.env` sets `JAVA_HOME`, `LANGADAPTER_MAVEN`, `LANGADAPTER_JAVA_TOOLS`, the tool cache below the install directory, and `PATH` ([Java projects](java.md)). |
| c | GCC 13 with its gcov, make and binutils from the Ubuntu archive, clang and Mull from their Ubuntu packages, and uv and CMake through uv | `c.env` puts Mull, clang and CMake on `PATH`, and the system's GCC is the project's compiler ([C projects](c.md)). |

[Tested environments](../reference/tested-environments.md) names the versions
each part installs. On a machine that is not x86_64 a part stops before it
fetches anything. A built-in part's versions are fixed. For other versions,
install the tools yourself, as [Windows and macOS](#windows-and-macos) lists.
The python part's environment with pytest, coverage.py and Cosmic Ray has
the interpreter `DIR/python/environments/toolchain-<digest>/bin/python`,
where `<digest>` is twelve hexadecimal digits of the environment's inputs,
and that interpreter can be a project's (`Interpreter(path=...)`).

## What the parts download

Each part names its downloads by kind, and `--list` prints them:

- Checked by the part: the files a part fetches itself, checked against a
  checksum the part holds before it unpacks them. These are uv's archive,
  the JDK and Maven archives, and the Ubuntu packages of clang and Mull.
- Pinned through uv or Maven: CPython comes through uv, which checks it
  against its own checksum. pytest, coverage.py and Cosmic Ray come from
  PyPI at the versions LangAdapter pins, and CMake at the version the C part
  pins, without a checksum LangAdapter holds.
  The Java check's first run fetches the JUnit launcher, JaCoCo and PIT into
  LangAdapter's tool cache, as every first run does, and Maven fetches its
  build plugins and the example's JUnit from Maven Central into the
  example's Maven repository below the install directory
  (`DIR/java/repository`). Maven only warns when a checksum there differs.
- From the archive: the packages a part installs with apt-get rest on the
  Ubuntu archive's signatures.

A part never calls sudo. Run as root, as in a container, it installs the
system packages it needs with apt-get. Run as a user, it stops with the exact
`sudo apt-get install` line when one is missing.

## In a container

The parts download through the command's Python, which needs the system's
certificates. A container build installs them and a Python with venv first:

```
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y python3-venv ca-certificates
RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install "langadapter[c]"
RUN /opt/venv/bin/langadapter toolchains python java c --into /opt/toolchains
```

[Containers](containers.md) says how a project then runs in such a
container.

## A language from an extension

An extension file can bring its own part beside `langadapter_language`, as
`langadapter_toolchain`: a mapping from a system name such as `ubuntu-24.04`
to the part's shell text. `--extension FILE` loads the file first, and
`--list` shows its part before anything runs:

```
langadapter toolchains lua --extension lua.py --list
```

The part's text runs with the rights of the user who runs the command, as
the extension's own code does when it loads.

A part is a POSIX shell script that takes three arguments: the install
directory, the Python the command runs in, and the extension: the file's
absolute path for a file, the module's name for an installed module, and
empty for a built-in part. It writes below
`DIR/<language>/` and the file `DIR/<language>.env`, and keeps what its
check downloads below `DIR/<language>/` too, as the Java part keeps
LangAdapter's tool cache and its example's Maven repository. It
names each download in a header line `# download <kind>: <text>`, and an
extension's part ends with
`"$2" -m langadapter.cli check <language> --extension "$3" --example DIR/<language>/example`,
so the check runs the LangAdapter of the command and finds its language.

The system name is `<ID>-<VERSION_ID>` from `/etc/os-release`, such as
`ubuntu-24.04`, and a system that the mapping does not name has no part. The
built-in parts name their downloads with the kinds `checked`, `pinned` and
`archive`. An extension's part may use any kind word, which `--list` prints
as written. A part belongs to the language the same file names as
`langadapter_language`, so a file without one brings no part. A part is read
and shown only on a system with parts, and `langadapter check` names no
part. From Windows, run `langadapter toolchains LANGUAGE --extension FILE
--list` in WSL to see it. The part writes the example its check runs itself. A whole part for
a language `lua`, at the end of `lua.py`:

```python
langadapter_toolchain = {"ubuntu-24.04": """#!/bin/sh
# download archive: lua5.4, installed with apt-get as root
set -eu
mkdir -p "$1"
DIR=$(cd "$1" && pwd)
HERE=$DIR/lua
mkdir -p "$HERE/example"
if [ "$(id -u)" -eq 0 ]; then
    apt-get update && apt-get install -y --no-install-recommends lua5.4
elif ! command -v lua5.4 >/dev/null; then
    echo "sudo apt-get install lua5.4" >&2
    exit 1
fi
echo "export LUA=$(command -v lua5.4)" > "$DIR/lua.env"
cat > "$HERE/example/calc.lua" <<'EOF'
function add(a, b) return a + b end
EOF
cat > "$HERE/example/test_calc.lua" <<'EOF'
assert(add(2, 3) == 5)
EOF
"$2" -m langadapter.cli check lua --extension "$3" --example "$HERE/example"
"""}
```

`DIR/lua.env` is a shell file of `export` lines, which the command names at
its end for a shell to read.

## Windows and macOS

The recipe runs on Ubuntu 24.04. On Windows, WSL with Ubuntu 24.04 runs it,
and Testuary's guide
[Set up your machine](https://k-kammler.github.io/testuary/guide/machine/#windows-with-wsl)
shows how to set WSL up. On Windows itself and on macOS, install the tools by
hand:

- Python: an interpreter with pytest, coverage.py and Cosmic Ray at the
  versions [Tested environments](../reference/tested-environments.md) names,
  installed into the project's environment with pip, or uv with
  `python_version` ([Python projects](python.md)).
- Java: a JDK of a version
  [Tested environments](../reference/tested-environments.md) names, such as
  Temurin's, with `JAVA_HOME` set, and Maven or Gradle on `PATH`, or the
  project's own wrapper ([Java projects](java.md)).

`langadapter check python --example DIR` or
`langadapter check java --example DIR` then runs the language's tests on an
example directory whose tests pass.
[Check on an example](../extending/add-a-language.md#check-on-an-example)
gives the rules of such a run: the program that runs the tests, for Python
the project's activated environment, the tools it runs or leaves not checked,
and the exit statuses. [C projects](c.md) run on Linux only.
