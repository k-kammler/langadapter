#!/bin/sh
# The C toolchain on Ubuntu 24.04: GCC with its gcov, clang and Mull for
# mutation, and CMake, proved by langadapter check on a small example.
# download archive: GCC 13 with its gcov, make, binutils and the libraries clang needs, installed with apt-get as root
# download checked: clang 18.1.3, Ubuntu 24.04's packages of version 18.1.3-1ubuntu1 from Launchpad, each by its SHA-256
# download checked: Mull 0.34.2 for LLVM 18, its Ubuntu 24.04 package from Mull's release, by its SHA-256
# download checked: uv 0.12.1, the x86_64-unknown-linux-gnu archive, by its SHA-256
# download pinned: CMake 4.4.4 through uv, from PyPI
set -eu

if [ "$#" -ne 3 ] || [ -z "$1" ] || [ -z "$2" ]; then
    echo "usage: sh c.ubuntu-24.04.sh DIR PYTHON EXTENSION" >&2
    exit 2
fi
case "$(uname -m)" in
    x86_64) ;;
    *) echo "c.ubuntu-24.04.sh installs x86_64 tools, and this machine is $(uname -m). The guide lists the steps: https://k-kammler.github.io/langadapter/guide/toolchains/" >&2
       exit 2 ;;
esac

# GCC 13 with its gcov and the C library's headers, make, binutils, and the
# libraries of clang's packages that a minimal system lacks.
SYSTEM_PACKAGES="gcc-13 gcc libc6-dev make binutils libedit2 libffi8 libxml2"
missing=
for package in $SYSTEM_PACKAGES; do
    if ! dpkg-query -W -f='${Status}\n' "$package" 2>/dev/null | grep -q '^install ok installed$'; then
        missing="$missing $package"
    fi
done
if [ -n "$missing" ]; then
    if [ "$(id -u)" -ne 0 ]; then
        echo "c.ubuntu-24.04.sh needs system packages that are not installed. Install them, then run it again:" >&2
        echo "sudo apt-get install$missing" >&2
        exit 1
    fi
    DEBIAN_FRONTEND=noninteractive apt-get update
    # $missing splits into package names on purpose.
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $missing
fi

mkdir -p "$1"
DIR=$(cd "$1" && pwd)
PYTHON=$2
HERE=$DIR/c
mkdir -p "$HERE/downloads" "$HERE/uv"
# The interpreter and uv start here, not in a directory whose files they would read.
cd "$HERE"

CLANG_VERSION=18.1.3-1ubuntu1
LAUNCHPAD=https://launchpad.net/ubuntu/+archive/primary/+files
# Ubuntu 24.04's packages of clang, each with its SHA-256.
CLANG_PACKAGES="clang-18:628b16701014ef7ad648380b20ea74b90dd543857f933b3e34d2fc042783de25
libclang-common-18-dev:d41631bb752815a8196261d1030c096591c59678c8adfbf2e6a740a691b49e80
libclang-cpp18:e91eda104813ee0468d145d010a4a4e142401f3f17f9a65596c66d416298d018
libclang1-18:1ed67106d743ab4f12db2726c5fc11c436c5e1fba613e9f1006bf3fa05cc69bd
libllvm18:af6197096ce3b89aab86de59f01c1b5feb008b2ae1514a03906c0664edb8b0b1
llvm-18-linker-tools:50f0bcfa784e42f8bf0eac31aeb6a20798fb6174cc31efe021f12ae71af91b23"
MULL_VERSION=0.34.2
MULL_PACKAGE=Mull-18-$MULL_VERSION-LLVM-18.1.3-ubuntu-amd64-24.04.deb
MULL_SHA256=e6a1abece45104479ef4afca41438b49bb7214d359aa359eaa3f236f363b33b3
UV_VERSION=0.12.1
UV_ARCHIVE=uv-x86_64-unknown-linux-gnu.tar.gz
UV_SHA256=90b2f223fb69d19db49e117da601f64978593417988530aa733d456141b4bcbb
CMAKE_VERSION=4.4.4

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

for entry in $CLANG_PACKAGES; do
    package=${entry%%:*}_${CLANG_VERSION}_amd64.deb
    checked "$package" "$LAUNCHPAD/$package" sha256sum "${entry#*:}"
done
checked "$MULL_PACKAGE" \
    "https://github.com/mull-project/mull/releases/download/$MULL_VERSION/$MULL_PACKAGE" sha256sum "$MULL_SHA256"
checked "uv-$UV_VERSION-$UV_ARCHIVE" \
    "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/$UV_ARCHIVE" sha256sum "$UV_SHA256"

# The packages unpacked with dpkg -x in the layout the C adapter reads for
# Mull: mull-runner-18 in usr/bin, its IR frontend in usr/lib and clang 18 in
# usr/lib/llvm-18/bin, through a directory that becomes ROOT only when every
# package unpacked. ROOT is named by both versions, so another pin unpacks
# into a directory of its own.
ROOT=$HERE/root-$CLANG_VERSION-mull-$MULL_VERSION
if [ ! -d "$ROOT" ]; then
    mkdir -p "$ROOT.unpacking"
    for entry in $CLANG_PACKAGES; do
        dpkg -x "$HERE/downloads/${entry%%:*}_${CLANG_VERSION}_amd64.deb" "$ROOT.unpacking"
    done
    dpkg -x "$HERE/downloads/$MULL_PACKAGE" "$ROOT.unpacking"
    mv "$ROOT.unpacking" "$ROOT"
fi

# CMake in a tool environment of uv's, its commands in this part's bin
# directory, which c.env puts on PATH, uv's cache below this part's directory.
tar -xzf "$HERE/downloads/uv-$UV_VERSION-$UV_ARCHIVE" -C "$HERE/uv" --strip-components=1
PATH=$HERE/bin:$PATH UV_CACHE_DIR=$HERE/cache UV_TOOL_DIR=$HERE/tools UV_TOOL_BIN_DIR=$HERE/bin \
    UV_PYTHON_DOWNLOADS=never "$HERE/uv/uv" tool install --python "$PYTHON" "cmake==$CMAKE_VERSION"

{
    echo "# The C toolchain LangAdapter installed: Mull, clang and CMake on PATH."
    echo "export PATH=\"$ROOT/usr/bin:$HERE/bin:\$PATH\""
} > "$DIR/c.env"

mkdir -p "$HERE/example"
cat > "$HERE/example/calc.c" <<'EOF'
int sign(int number) {
    if (number > 0) {
        return 1;
    }
    if (number < 0) {
        return -1;
    }
    return 0;
}
EOF
cat > "$HERE/example/test_calc.c" <<'EOF'
#include "utest.h"

int sign(int number);

UTEST(sign, positive) {
    ASSERT_EQ(1, sign(3));
}

UTEST(sign, negative) {
    ASSERT_EQ(-1, sign(-2));
}

UTEST_MAIN()
EOF

. "$DIR/c.env"
"$PYTHON" -m langadapter.cli check c --example "$HERE/example"
