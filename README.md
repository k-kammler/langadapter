# LangAdapter

[![PyPI version](https://img.shields.io/pypi/v/langadapter?color=green)](https://pypi.org/project/langadapter/)
[![Python versions](https://img.shields.io/pypi/pyversions/langadapter?color=green)](https://pypi.org/project/langadapter/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](https://github.com/k-kammler/langadapter/blob/main/LICENSE)
[![Documentation](https://github.com/k-kammler/langadapter/actions/workflows/docs.yml/badge.svg)](https://k-kammler.github.io/langadapter/)

LangAdapter gives a Python program one common interface to software projects
in different programming languages. With the same calls, a program can read
and edit a project's source code, check what the project's setup supports,
and run its tests with coverage, mutation and timing measurements. Python,
Java and C come with it, and it is built to be extended: a new language is
one file with its adapter class, and code metrics and tool installers are
added the same way, with no change to the library. LangAdapter needs no
language model.

## Supported systems

| Projects in | Windows | Linux | macOS |
| --- | --- | --- | --- |
| Python | yes | yes | expected to work |
| Java | yes | yes | expected to work |
| C | in WSL | yes | not yet supported |

LangAdapter was tested on Windows 11, Ubuntu 24.04 under WSL and AlmaLinux
8.10. On Windows and Linux it confirms that a command and everything it
started have ended. This release has not run on a Mac. On macOS and other
POSIX systems it runs each command in its own process group and cannot
confirm that every descendant ended, so it warns and records that in its
observations. On Windows, C projects run with LangAdapter installed inside
WSL. Python and Java projects can also run in a running Linux container you
provide.
[Tested environments](https://k-kammler.github.io/langadapter/reference/tested-environments/)
says what is known.

## Install

The library needs Python 3.10 or newer. Install it into the Python
environment of the program that uses it, such as a virtual environment.
On Ubuntu 24.04, also in WSL, Python is `python3`, and a virtual
environment needs the package `python3-venv`:

```
sudo apt-get install python3-venv
python3 -m venv .venv
.venv/bin/python -m pip install langadapter
```

On Windows:

```
py -m venv .venv
.venv\Scripts\python -m pip install langadapter
```

In a source checkout, `.venv/bin/python -m pip install .` installs it from
the checkout. For C projects, install it with the extra for C:
`.venv/bin/python -m pip install "langadapter[c]"`. On Windows the
environment's Python is `.venv\Scripts\python`.

The project under test keeps its own runtime and tools, which
[Getting started](https://k-kammler.github.io/langadapter/getting-started/)
names for each language. On Ubuntu 24.04 on x86_64, `langadapter toolchains`
installs them, as
[Install the toolchains](https://k-kammler.github.io/langadapter/guide/toolchains/)
describes.

## Example

Run a Python project's tests with the project's interpreter and read the
result:

```python
import langadapter as la
from langadapter.runs import SuiteRunRequest

project = "path/to/project"
project_python = "path/to/project/.venv/bin/python"  # path/to/project/.venv/Scripts/python.exe on Windows
context = la.ProjectContext(root=project, interpreter=la.Interpreter(path=project_python))
adapter = la.create("python", context)
suite = la.Suite(project, ("tests/test_example.py",))
result = adapter.tests.run_suite(SuiteRunRequest(suite, timeout_s=120))
print(result.outcome, result.counts.passed, "passed")
```

If the project has a `conftest.py` in its directory or in `tests`, list it as a
setup file, for example
`la.Suite(project, ("tests/test_example.py",), ("tests/conftest.py",))`. A run
refuses a suite that leaves out a setup file pytest would load.

Every result carries an outcome such as `completed`, `test_failures`,
`missing_setup` or `unsupported`, and a reason, as
[Outcomes](https://k-kammler.github.io/langadapter/explanation/outcomes/)
describes.

## Documentation

The documentation site is <https://k-kammler.github.io/langadapter/>.

- [Getting started](https://k-kammler.github.io/langadapter/getting-started/):
  install, run a suite and read its result. Start here.
- [Python](https://k-kammler.github.io/langadapter/guide/python/),
  [Java](https://k-kammler.github.io/langadapter/guide/java/) and
  [C](https://k-kammler.github.io/langadapter/guide/c/) projects: the guide
  for each language.
- [Mutation sessions](https://k-kammler.github.io/langadapter/guide/mutation-sessions/)
  and [timing](https://k-kammler.github.io/langadapter/guide/timing/): a
  suite measured against the mutants of one source file, and timed.
- [Extension points](https://k-kammler.github.io/langadapter/extending/):
  extending the library at each of them, such as a new language adapter or
  metric provider, with
  [Add a language](https://k-kammler.github.io/langadapter/extending/add-a-language/).
- [API reference](https://k-kammler.github.io/langadapter/reference/): generated
  from the modules and their docstrings.

The guides also cover containers, source analysis and settings, and the
explanations cover outcomes, prepared copies of a project and environment
selection. The guides are also in the repository's
[docs directory](https://github.com/k-kammler/langadapter/tree/v0.1.1/docs)
and installed with the package under `share/doc/langadapter/docs`, without
the API reference, which is on the site only: in the installed copy,
Python's `help()` shows each name's documentation. Building the site
yourself is described in
[CONTRIBUTING.md](https://github.com/k-kammler/langadapter/blob/v0.1.1/CONTRIBUTING.md).
What the library depends on and downloads, with each license, is in
[THIRD_PARTY_NOTICES.md](https://github.com/k-kammler/langadapter/blob/v0.1.1/THIRD_PARTY_NOTICES.md).

## License

MIT, see [LICENSE](https://github.com/k-kammler/langadapter/blob/v0.1.1/LICENSE).
