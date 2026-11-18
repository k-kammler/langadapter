# Third-party notices

LangAdapter's own code is under the MIT License (see `LICENSE`). Its packages
contain two pieces of third-party code, a function adapted from eight lines
of pytest and the test framework header utest.h, each described below with
its notice. This file also lists what LangAdapter depends on and what it
downloads while it runs, each with its license.

## Code from pytest

The plugin text `_PATHS_PLUGIN_TEXT` in `langadapter/python/_runs.py`, which
LangAdapter loads into a project's pytest run in a prepared copy, defines
`_makepath`. It follows the eight lines of `FormattedExcinfo._makepath` in
pytest 8.1.1 (`_pytest/_code/code.py`), changed to print a frame's original
path from the original project's directory instead of the working directory.
pytest is under the MIT License:

```text
The MIT License (MIT)

Copyright (c) 2004 Holger Krekel and others

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## utest.h

`langadapter/c/data/utest.h` is utest.h by Neil Henning
(https://github.com/sheredom/utest.h), commit
706f3574fec4915e78d9234870cfcdf22f7af5d9 of October 6, 2026, unchanged
(SHA-256 406ff019fe24672fdafd79062399c17bd98cdefbf52ef8d17b34b7a436fd49ac).
The C adapter puts it on the include path of the C tests it compiles. It is
released into the public domain under the Unlicense:

```text
This is free and unencumbered software released into the public domain.

Anyone is free to copy, modify, publish, use, compile, sell, or
distribute this software, either in source code form or as a compiled
binary, for any purpose, commercial or non-commercial, and by any
means.

In jurisdictions that recognize copyright laws, the author or authors
of this software dedicate any and all copyright interest in the
software to the public domain. We make this dedication for the benefit
of the public at large and to the detriment of our heirs and
successors. We intend this dedication to be an overt act of
relinquishment in perpetuity of all present and future rights to this
software under copyright law.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
IN NO EVENT SHALL THE AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR
OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE,
ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR
OTHER DEALINGS IN THE SOFTWARE.

For more information, please refer to <http://unlicense.org/>
```

## Python dependencies

Installed with LangAdapter from PyPI:

| Package | Versions | License |
| --- | --- | --- |
| tree-sitter | 0.25 | MIT |
| tree-sitter-python | 0.25 | MIT |
| tree-sitter-java | 0.23.5 and later 0.23 releases | MIT |
| lizard | 1.17 and later 1.x releases | MIT |
| pygments (installed with lizard) | any that lizard accepts | BSD-2-Clause |
| pathspec (installed with lizard) | any that lizard accepts | MPL-2.0 |
| tomli-w | 1.0 and later 1.x releases | MIT |
| packaging | 22 or later | Apache-2.0 or BSD-2-Clause |

Installed with the extra for C (`langadapter[c]`):

| Package | Versions | License |
| --- | --- | --- |
| tree-sitter-c | 0.24.2 and later 0.24 releases | MIT |
| gcovr | 8.7 and later 8.x releases | BSD-3-Clause |
| setuptools | 70 or later | MIT |
| Jinja2 (installed with gcovr) | any that gcovr accepts | BSD-3-Clause |
| MarkupSafe (installed with Jinja2) | any that Jinja2 accepts | BSD-3-Clause |
| lxml (installed with gcovr) | any that gcovr accepts | BSD-3-Clause |
| colorlog (installed with gcovr) | any that gcovr accepts | MIT |
| pygments (installed with gcovr) | any that gcovr accepts | BSD-2-Clause |
| tomli (installed with gcovr on Python 3.10) | any that gcovr accepts | MIT |
| typing-extensions (installed with gcovr on Python 3.10) | any that gcovr accepts | PSF-2.0 |
| colorama (installed with colorlog on Windows) | any that colorlog accepts | BSD-3-Clause |

lxml's binary wheels bundle libxml2, libxslt and libexslt (MIT), zlib (Zlib)
and iconv (LGPL-2.1), and parts of lxml are under the ElementTree license and
PSF-2.0, as the wheels' `LICENSES.txt` states.

## Tools in a Python project's environment

LangAdapter does not install these. Its Python operations run them in the
project's own environment, and the setup check names the versions LangAdapter
expects:

| Package | Version | License |
| --- | --- | --- |
| pytest | 8.1.1, and 8.4.2 for Python 3.14 and later | MIT |
| coverage | 7.6.8, and 7.16.2 for Python 3.14 and later | Apache-2.0 |
| cosmic-ray | 8.4.1 | MIT |

## Tools for C projects

LangAdapter installs no compiler or tool for C. Its C operations run what
they find for the project: GCC and its gcov, the build tools (make, cmake),
sh for the compiler and archiver recorder of every recorded build and for a
generated `configure`, ar and nm of GNU binutils, ctest for `c_tests`
`ctest`, stdbuf of GNU coreutils for a run as delivered, grep for the
processors a benchmark may use, prlimit of util-linux for
`c_address_space_limit` and its taskset for a benchmark's processor set,
and Mull (Apache-2.0) with its clang for mutation.

## Java tools downloaded at run time

For Java projects, LangAdapter downloads the jars below from Maven Central
(`https://repo1.maven.org/maven2`) the first time an operation needs them.
Each jar is checked against the repository's SHA-1 and kept in the tool cache:
the directory `LANGADAPTER_JAVA_TOOLS` names, else `~/.cache/langadapter/java`.
With `LANGADAPTER_JAVA_TOOLS_OFFLINE=1`, nothing is downloaded and only the
cache is used. The jars run in the project's JVM and are not part of
LangAdapter's packages. Each keeps its own license.

| Jar | Version | Used for | License |
| --- | --- | --- | --- |
| `org.jacoco:org.jacoco.agent` (runtime), `org.jacoco:org.jacoco.core` | 0.8.15 | Coverage | EPL-2.0 |
| `org.ow2.asm:asm`, `asm-commons`, `asm-tree` | 9.10.1 | Coverage and mutation testing | BSD-3-Clause |
| `org.ow2.asm:asm-util`, `asm-analysis` | 9.10.1 | Mutation testing | BSD-3-Clause |
| `org.pitest:pitest`, `pitest-entry`, `pitest-command-line`, `pitest-html-report` | 1.30.0 | Mutation testing | Apache-2.0 |
| `org.pitest:pitest-junit5-plugin` | 1.2.3 | Mutation testing on the JUnit Platform | Apache-2.0 |
| `net.sf.jopt-simple:jopt-simple` | 4.9 | Mutation testing | MIT |
| `org.apache.commons:commons-lang3` | 3.18.0 | Mutation testing | Apache-2.0 |
| `org.apache.commons:commons-text` | 1.14.0 | Mutation testing | Apache-2.0 |
| `org.junit.platform:junit-platform-console`, `-reporting`, `-launcher`, `-engine`, `-commons` | The project's JUnit Platform version, else 1.14.4. The launcher, engine and commons 1.14.4 always, to compile the per-test evidence listener | Running tests and per-test evidence | EPL-2.0 (the console includes picocli, Apache-2.0) |
| `org.junit.jupiter:junit-jupiter-engine` | The project's JUnit Jupiter version | Running JUnit 5 and 6 tests | EPL-2.0 |
| `org.junit.vintage:junit-vintage-engine` | Matching the JUnit Platform | Running JUnit 4 tests | EPL-2.0 |
| `org.opentest4j.reporting:open-test-reporting-tooling-spi` | Matching the JUnit Platform | Test reports | Apache-2.0 |
| `junit:junit` | 4.13.2 | Projects on an older JUnit 4 | EPL-1.0 |
| `org.hamcrest:hamcrest-core` | 1.3 | With JUnit 4.13.2 | BSD-3-Clause |
| `org.opentest4j:opentest4j` | 1.3.0 | Running tests | Apache-2.0 |
| `org.apiguardian:apiguardian-api` | 1.1.2 | Per-test evidence | Apache-2.0 |

The project's own Maven runs two plugins to read the project, and fetches them
itself from the repositories the project uses:

| Plugin | Version | License |
| --- | --- | --- |
| `org.apache.maven.plugins:maven-help-plugin` | 3.5.2 | Apache-2.0 |
| `org.apache.maven.plugins:maven-dependency-plugin` | 3.11.0 | Apache-2.0 |
