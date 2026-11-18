# Tested environments

LangAdapter 0.1.0 was tested in the combinations below. Other combinations
may work, but they were not tested.

## Where the library runs

| What | Tested |
| --- | --- |
| Operating systems | Windows 11, Ubuntu 24.04 under WSL, AlmaLinux 8.10 |
| Python for the library itself | 3.10, 3.11, 3.12, 3.13 and 3.14, installed from the built wheel into a new virtual environment |
| The library's own test suite | Python 3.10, 3.11, 3.12, 3.13 and 3.14 on Windows 11 and on Ubuntu 24.04 under WSL, with a real JDK for the Java tests |

## Python projects

| What | Tested |
| --- | --- |
| Project interpreters | 3.10 to 3.15 |
| Tools in the project's environment | pytest 8.1.1, coverage 7.6.8 and Cosmic Ray 8.4.1, and on Python 3.14 and 3.15 pytest 8.4.2, coverage 7.16.2 and Cosmic Ray 8.4.1 |
| Operations | test runs, coverage, per-test evidence, mutation sessions, timing |

## Java projects

| What | Tested |
| --- | --- |
| JDKs | Temurin 11, 17, 21, 25, 26 and 27 |
| Build tools | Maven 3.9, a project's Maven wrapper, Gradle 9.8 |
| Test frameworks | JUnit 4 (4.13.2, run on the JUnit Platform), JUnit 5, JUnit 6 |
| Measurement tools | JaCoCo 0.8.15, PIT 1.30.0 with its JUnit 5 plugin 1.2.3 |
| Projects | language levels 8 (on JDK 11 and 17), 11, 17 and 21, each with Maven and with Gradle, except level 8 on JDK 11 with Gradle |
| Operations | project resolution, test runs, coverage, per-test evidence, mutation, timing |

A project that declares a JUnit 4 version older than 4.12 runs its tests with
JUnit 4.13.2.

## C projects

| What | Tested |
| --- | --- |
| System | Ubuntu 24.04 |
| Compiler and coverage | GCC 13.3 with its gcov |
| Mutation | Mull 0.34.2 with clang 18.1.3 |
| Builds | make, CMake 4.4.4 |

The C adapter runs on Linux only. It needs GCC 7 or newer with its gcov,
and refuses clang as the project's compiler and an older GCC, which does not
report its version with `-dumpfullversion`. For mutation it takes Mull for
an LLVM release `N` from 14 to 23 (`mull-runner-14` to `mull-runner-23`)
with clang `N`, found as [C projects](../guide/c.md#what-it-needs)
describes, and refuses a clang of another release. It compares no Mull
version. Its default operators are Mull's default group
as Mull 0.34.2 names its operators, so a newer Mull with a larger group
measures the same operators. The extra `langadapter[c]` installs gcovr 8.7
or newer, below 9, which reads gcov's counters.

## The toolchain recipe

`langadapter toolchains` ([Install the toolchains](../guide/toolchains.md))
was tested on Ubuntu 24.04 on x86_64 under WSL, run as a user from the built
wheel. Its parts install x86_64 tools.

| Part | Installs |
| --- | --- |
| python | uv 0.12.1, CPython 3.12.13 and the pinned pytest, coverage and Cosmic Ray |
| java | Temurin JDK 21.0.12.1+1 and Maven 3.9.16 |
| c | GCC 13.3 with its gcov, make and binutils from the archive, clang 18.1.3, Mull 0.34.2, uv 0.12.1 and CMake 4.4.4 |

## Support ranges

The setup check compares a project's runtime with these ranges.

| Runtime | Tested | Expected to work | Not supported |
| --- | --- | --- | --- |
| Project Python | 3.10 to 3.15 | 3.9 | below 3.9, and Python 2 |
| JDK | 11, 17, 21, 25, 26 and 27 | the releases between them | below 11 |

A runtime that is expected to work, or newer than the newest tested one,
runs, and the setup check names it with a note. A runtime that is not
supported is refused with the reason and the supported versions.

## Containers

| What | Tested |
| --- | --- |
| Python projects in Linux Docker containers | from Ubuntu 24.04 under WSL, with project Python 3.10 in the container image: coverage and mutation |

## macOS

This release has not run on a Mac. LangAdapter puts its temporary directories
below the physical path of the system's temporary directory, which macOS keeps
below the link `/var`. It checks a suite for links only inside its own
directory and resolves the directories a caller passes to the collection check.
That is tested on Linux with a temporary directory behind a symbolic link, a
mutation session without `/dev/shm` included. If a command on a Mac fails with
a message that names `/var`, set `TMPDIR` to the directory's physical path:

```text
export TMPDIR="$(cd "$TMPDIR" && pwd -P)/"
```

No Java project has run on a Mac.

LangAdapter runs a command on macOS in its own process group: stopping it
ends the group, but a process that leaves the group (a new session, a
daemon) is neither ended nor seen. Cleanup is therefore not confirmed: the
first command warns, and observations record the supervision
`process_group_unconfirmed`. macOS volumes usually ignore letter case in
paths, while LangAdapter compares paths there case-sensitively, so name
files with their exact spelling. A macOS JDK bundle (`name.jdk`) works when
you select the bundle, its `Contents/Home` or its `java`.

## Not tested

- Project environments prepared by Conda, uv, Poetry or Pipenv. An
  interpreter from such an environment is used like any other interpreter,
  but the environments' own activation is not part of the tests.
- The Gradle wrapper.
- The library itself on Python 3.15. Its parser library, tree-sitter 0.25,
  has no wheels for 3.15, so it installs there only with a C compiler. A
  project on Python 3.15 is measured by the library on Python 3.14 or
  older, and the setup check notes that a source using syntax newer than
  the library's Python is reported as unreadable.
- Java projects in containers, warmed timing of Java tests in a Linux
  container among them, and containers run by Apptainer or Podman.
- POSIX systems other than macOS. Commands run there as on macOS, in a
  process group without confirmed cleanup.

## Not supported

- Hosts without processes, such as emscripten and wasi: they run no
  command. Importing the library and the source operations work there.
- Project runtimes outside the support ranges above. A project built for
  Java 8 runs on JDK 11 or newer.
- Windows container images.
- The top project of a multi-module build, selected as the project, when it
  only aggregates its modules. Select the module that holds the code: it
  resolves with the modules it depends on.
- Java builds other than Maven and Gradle.
- A project with both Maven and Gradle build files. Every run is refused,
  and the setup check says to remove the build files of the build system the
  project does not use.
- Test frameworks other than pytest for Python and JUnit for Java. A TestNG
  suite without JUnit is refused by name. A classpath with JUnit as well runs
  its JUnit tests.
- Running a command on Windows in a working directory longer than 258
  characters. Windows refuses it even with long paths enabled. A prepared
  copy keeps its project directory within that length where it can, and
  otherwise refuses with the length and what to do.
