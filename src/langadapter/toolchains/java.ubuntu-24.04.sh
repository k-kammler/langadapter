#!/bin/sh
# The Java toolchain on Ubuntu 24.04: a Temurin JDK and Maven, proved by
# langadapter check on a small example.
# download checked: Temurin JDK 21.0.12.1+1, the Linux x64 archive, by its SHA-256
# download checked: Maven 3.9.16, the binary archive, by its SHA-512
# download pinned: JaCoCo, PIT and the JUnit launcher at LangAdapter's pinned versions, from Maven Central on the check's first run, into LangAdapter's tool cache below the install directory, which java.env names
# download pinned: Maven's build plugins and the example's JUnit 5.14.4, from Maven Central, which Maven checks only with a warning, into the example's Maven repository below the install directory
set -eu

if [ "$#" -ne 3 ] || [ -z "$1" ] || [ -z "$2" ]; then
    echo "usage: sh java.ubuntu-24.04.sh DIR PYTHON EXTENSION" >&2
    exit 2
fi
case "$(uname -m)" in
    x86_64) ;;
    *) echo "langadapter toolchains java installs x86_64 tools, and this machine is $(uname -m). The guide lists the steps: https://k-kammler.github.io/langadapter/guide/toolchains/" >&2
       exit 2 ;;
esac
mkdir -p "$1"
DIR=$(cd "$1" && pwd)
PYTHON=$2
HERE=$DIR/java
mkdir -p "$HERE/downloads"
# The interpreter starts here, not in a directory whose files it would read.
cd "$HERE"

JDK_RELEASE=21.0.12.1+1
JDK_ARCHIVE=OpenJDK21U-jdk_x64_linux_hotspot_21.0.12.1_1.tar.gz
JDK_URL=https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12.1%2B1/$JDK_ARCHIVE
JDK_SHA256=ce79869e1307ed8ee1e2baa86a412b1eb5b75d10a01006d788a6f968bcfaee94
MAVEN_VERSION=3.9.16
MAVEN_ARCHIVE=apache-maven-$MAVEN_VERSION-bin.tar.gz
MAVEN_URL=https://archive.apache.org/dist/maven/maven-3/$MAVEN_VERSION/binaries/$MAVEN_ARCHIVE
MAVEN_SHA512=831a8591fe20c8243b1dbe7d71e3244f31d1665b0804b2e825e38cbbe5ce0cafb8338851f90780735568773e0a6cd07bbec107cda0b896b008b861075358b6f6

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

# ARCHIVE unpacked into HOME, a directory below this part's, through a
# directory that becomes HOME only when the archive unpacked whole.
unpacked() {
    if [ ! -d "$2" ]; then
        mkdir -p "$2.unpacking"
        tar -xzf "$HERE/downloads/$1" -C "$2.unpacking" --strip-components=1
        mv "$2.unpacking" "$2"
    fi
}

checked "$JDK_ARCHIVE" "$JDK_URL" sha256sum "$JDK_SHA256"
checked "$MAVEN_ARCHIVE" "$MAVEN_URL" sha512sum "$MAVEN_SHA512"
JDK_HOME=$HERE/jdk-$JDK_RELEASE
MAVEN_HOME=$HERE/apache-maven-$MAVEN_VERSION
unpacked "$JDK_ARCHIVE" "$JDK_HOME"
unpacked "$MAVEN_ARCHIVE" "$MAVEN_HOME"

{
    echo "# The Java toolchain LangAdapter installed: the JDK and Maven."
    echo "export JAVA_HOME=\"$JDK_HOME\""
    echo "export LANGADAPTER_MAVEN=\"$MAVEN_HOME/bin/mvn\""
    echo "export LANGADAPTER_JAVA_TOOLS=\"$HERE/tools\""
    echo "export PATH=\"$JDK_HOME/bin:$MAVEN_HOME/bin:\$PATH\""
} > "$DIR/java.env"

EXAMPLE=$HERE/example
mkdir -p "$EXAMPLE/.mvn" "$EXAMPLE/src/main/java/example" "$EXAMPLE/src/test/java/example"
# The example's Maven repository lies below the install directory too.
echo "-Dmaven.repo.local=$HERE/repository" > "$EXAMPLE/.mvn/maven.config"
cat > "$EXAMPLE/pom.xml" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>example</groupId>
  <artifactId>calc</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.release>21</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>5.14.4</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
</project>
EOF
cat > "$EXAMPLE/src/main/java/example/Calc.java" <<'EOF'
package example;

public final class Calc {
    private Calc() {
    }

    public static int sign(int number) {
        if (number > 0) {
            return 1;
        }
        if (number < 0) {
            return -1;
        }
        return 0;
    }
}
EOF
cat > "$EXAMPLE/src/test/java/example/CalcTest.java" <<'EOF'
package example;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcTest {
    @Test
    void positive() {
        assertEquals(1, Calc.sign(3));
    }

    @Test
    void negative() {
        assertEquals(-1, Calc.sign(-2));
    }
}
EOF

. "$DIR/java.env"
"$PYTHON" -m langadapter.cli check java --example "$EXAMPLE"
