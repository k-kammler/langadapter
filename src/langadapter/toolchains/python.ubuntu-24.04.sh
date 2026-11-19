#!/bin/sh
# The Python toolchain on Ubuntu 24.04: uv, CPython and the tools LangAdapter
# pins for it, proved by langadapter check on a small example.
# download checked: uv 0.12.1, the x86_64-unknown-linux-gnu archive, by its SHA-256
# download pinned: CPython 3.12.13 through uv, which checks it against uv's own checksum
# download pinned: pytest, coverage.py and Cosmic Ray at LangAdapter's pinned versions, from PyPI through uv
set -eu

if [ "$#" -ne 3 ] || [ -z "$1" ] || [ -z "$2" ]; then
    echo "usage: sh python.ubuntu-24.04.sh DIR PYTHON EXTENSION" >&2
    exit 2
fi
case "$(uname -m)" in
    x86_64) ;;
    *) echo "langadapter toolchains python installs x86_64 tools, and this machine is $(uname -m). The guide lists the steps: https://k-kammler.github.io/langadapter/guide/toolchains/" >&2
       exit 2 ;;
esac
mkdir -p "$1"
DIR=$(cd "$1" && pwd)
PYTHON=$2
HERE=$DIR/python
mkdir -p "$HERE/bin" "$HERE/downloads"
# The interpreter and uv start here, not in a directory whose files they would read.
cd "$HERE"

UV_VERSION=0.12.1
UV_ARCHIVE=uv-x86_64-unknown-linux-gnu.tar.gz
UV_SHA256=90b2f223fb69d19db49e117da601f64978593417988530aa733d456141b4bcbb
CPYTHON_VERSION=3.12.13

# The URL's content goes to FILE, read by the interpreter the command runs in,
# so the part needs no download program.
fetch() {
    "$PYTHON" -c 'import shutil, sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=60) as response, open(sys.argv[2], "wb") as out:
    shutil.copyfileobj(response, out)' "$1" "$2"
}

# FILE in the downloads directory, fetched from URL unless it is there with
# its checksum (TOOL is sha256sum or sha512sum), and checked before anything
# unpacks it.
checked() {
    if ! (cd "$HERE/downloads" && echo "$4  $1" | "$3" -c --status 2>/dev/null); then
        echo "fetching $1"
        fetch "$2" "$HERE/downloads/$1"
        (cd "$HERE/downloads" && echo "$4  $1" | "$3" -c -)
    fi
}

checked "uv-$UV_VERSION-$UV_ARCHIVE" \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$UV_ARCHIVE" sha256sum "$UV_SHA256"
tar -xzf "$HERE/downloads/uv-$UV_VERSION-$UV_ARCHIVE" -C "$HERE/bin" --strip-components=1

# uv keeps its interpreters and its cache below this part's directory, and
# takes only interpreters it manages.
UV_PYTHON_INSTALL_DIR=$HERE/interpreters
UV_CACHE_DIR=$HERE/cache
UV_MANAGED_PYTHON=1
export UV_PYTHON_INSTALL_DIR UV_CACHE_DIR UV_MANAGED_PYTHON
PATH=$HERE/bin:$PATH
export PATH
uv python install --no-bin "$CPYTHON_VERSION"

# The tool environment, made by LangAdapter's provisioning, so the pins have
# one definition.
TOOLS_PYTHON=$("$PYTHON" -c 'import sys
from pathlib import Path
from langadapter.context import ProvisioningRequest
from langadapter.python.adapter import PythonAdapter
print(PythonAdapter.provision_runtime(
    ProvisioningRequest(sys.argv[1], None), store=Path(sys.argv[2]), name="toolchain",
    log=lambda line: print(line, file=sys.stderr)))' "$CPYTHON_VERSION" "$HERE/environments")

{
    echo "# The Python toolchain LangAdapter installed: uv on PATH."
    echo "export PATH=\"$HERE/bin:\$PATH\""
} > "$DIR/python.env"

mkdir -p "$HERE/example"
cat > "$HERE/example/calc.py" <<'EOF'
def sign(number):
    if number > 0:
        return 1
    if number < 0:
        return -1
    return 0
EOF
cat > "$HERE/example/test_calc.py" <<'EOF'
from calc import sign


def test_positive():
    assert sign(3) == 1


def test_negative():
    assert sign(-2) == -1
EOF

. "$DIR/python.env"
PATH=$(dirname "$TOOLS_PYTHON"):$PATH
export PATH
"$PYTHON" -m langadapter.cli check python --example "$HERE/example"
