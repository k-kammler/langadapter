"""Fixtures of the C adapter's tests: copies of the example projects and
adapters for them, run with the GCC on PATH. The tests need Linux and GCC
with its gcov, the extension tests also the headers of the Python running
them, and the mutation tests Mull (``mull-runner-N`` on PATH, with its IR
frontend and clang N). Without them they are skipped."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import langadapter as la

PROJECTS = Path(__file__).parent / "projects"
#: The example projects' own test files are inputs of the tests, never
#: collected as tests.
collect_ignore = ["projects"]


def _compiler():
    """gcc, on Linux with its own gcov beside it, as the adapter finds it, and
    the make and sh the builds use."""
    from langadapter.c import _toolchain

    gcc = shutil.which("gcc") if sys.platform.startswith("linux") else None
    if gcc is None or _toolchain.gcov_beside(gcc) is None:
        return None
    return gcc if all(shutil.which(tool) for tool in ("make", "sh")) else None


needs_gcc = pytest.mark.skipif(
    _compiler() is None, reason="needs Linux, gcc with its gcov, and make and sh on PATH")
needs_linux = pytest.mark.skipif(not sys.platform.startswith("linux"),
                                 reason="the C adapter runs on Linux only")


def _python_headers() -> bool:
    """Whether this Python has its headers (``Python.h``), which a setuptools
    build of an extension compiles with."""
    import sysconfig

    return (Path(sysconfig.get_paths()["include"]) / "Python.h").is_file()


needs_python_headers = pytest.mark.skipif(
    not _python_headers(), reason="needs the headers of this Python (python3-dev)")


def _mull():
    """A usable Mull as the adapter finds it: the runner, its IR frontend and
    its clang N, or None."""
    from langadapter.c import _mull

    if _compiler() is None:
        return None
    found = _mull.probe("", lambda argv: subprocess.run(argv, capture_output=True,
                                                        text=True, timeout=60), os.environ)
    return found if isinstance(found, _mull.Mull) else None


needs_mull = pytest.mark.skipif(
    _mull() is None,
    reason="needs Linux, gcc and mull-runner-N on PATH with its IR frontend and clang N")


@pytest.fixture
def project(tmp_path):
    """A copy of the named example project, and the directory of its suite."""
    def make(name: str) -> Path:
        root = tmp_path / name
        shutil.copytree(PROJECTS / name, root)
        return root
    return make


@pytest.fixture
def adapter():
    """A C adapter for the project at ``root``, with ``settings`` as the
    context's variables."""
    from langadapter.c.adapter import CAdapter

    def make(root: Path, **settings) -> CAdapter:
        environment = CAdapter.context_environment(settings)
        context = la.ProjectContext(root=str(root), interpreter=la.Interpreter(path=_compiler()),
                                    environment=environment)
        return CAdapter(context)
    return make


@pytest.fixture
def suite(tmp_path):
    """A suite of one test file with ``text``, in a directory outside the project."""
    def make(text: str, name: str = "test_calc.c") -> la.Suite:
        suite_dir = tmp_path / "suite"
        suite_dir.mkdir(exist_ok=True)
        (suite_dir / name).write_text(text, encoding="utf-8")
        return la.Suite(str(suite_dir), (name,))
    return make


def negative_count_output(source: str) -> str:
    """gcovr 8.8's output, its traceback shortened, when gcov read a branch
    count of ``source``, a path under gcovr's ``--root`` (``{root}``), as
    -2. It was captured on a real coverage build whose gcov wrote the count
    -2, GCC bug 68080's symptom. gcovr then exits with status 64."""
    location = "{root}/" + source
    return f"""\
(WARNING) Unrecognized GCOV output for {location}
\t  branch  0 taken -2 (fallthrough)
\tThis is indicative of a gcov output parse error.
\tPlease report this to the gcovr developers
\tat <https://github.com/gcovr/gcovr/issues>.
(WARNING) Exception during parsing:
\tNegativeHitsError: {location}:2 Got negative hit value in: branch  0 taken -2 (fallthrough)
This is caused by a bug in gcov tool, see
https://gcc.gnu.org/bugzilla/show_bug.cgi?id=68080. Use option
--gcov-ignore-parse-errors with a value of negative_hits.warn,
or negative_hits.warn_once_per_file.
(ERROR) Exiting because of parse errors.
\tYou can run gcovr with --gcov-ignore-parse-errors=...
\tto continue anyway.
(ERROR) Traceback (most recent call last):
  File "gcovr/formats/gcov/parser/common.py", line 90, in raise_if_not_ignored
    raise NegativeHitsError(line, persistent_states)
gcovr.formats.gcov.parser.common.NegativeHitsError: {location}:2 Got negative hit value in: \
branch  0 taken -2 (fallthrough)
This is caused by a bug in gcov tool, see
https://gcc.gnu.org/bugzilla/show_bug.cgi?id=68080. Use option
--gcov-ignore-parse-errors with a value of negative_hits.warn,
or negative_hits.warn_once_per_file.

(ERROR) Error occurred while reading reports: Worker thread raised exception, workers canceled.
"""


#: A gcovr package that writes GCOVR_STAND_IN_OUTPUT to stderr, its
#: ``--root`` argument in place of ``{root}``, and exits with
#: GCOVR_STAND_IN_STATUS.
GCOVR_STAND_IN = """\
import os
import sys

root = sys.argv[sys.argv.index("--root") + 1]
sys.stderr.write(os.environ["GCOVR_STAND_IN_OUTPUT"].replace("{root}", root))
sys.exit(int(os.environ["GCOVR_STAND_IN_STATUS"]))
"""


GCOVR_REFUSING = """\
import json
import os
import subprocess
import sys

here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if p and os.path.abspath(p) != here]
done = subprocess.run([sys.executable, "-m", "gcovr", *sys.argv[1:]],
                      env=dict(os.environ, PYTHONPATH=os.pathsep.join(path)))
if done.returncode:
    sys.exit(done.returncode)
report = sys.argv[sys.argv.index("--json") + 1]
with open(report, encoding="utf-8") as handle:
    record = json.load(handle)
line, least = int(os.environ["GCOVR_REFUSING_LINE"]), int(os.environ["GCOVR_REFUSING_LEAST"])
if any(int(entry["line_number"]) == line and int(entry["count"]) >= least
       for found in record.get("files", ()) for entry in found.get("lines", ())):
    os.remove(report)
    root = sys.argv[sys.argv.index("--root") + 1]
    sys.stderr.write(os.environ["GCOVR_REFUSING_OUTPUT"].replace("{root}", root))
    sys.exit(64)
other = int(os.environ.get("GCOVR_REFUSING_OTHER", "0"))
if other and any(int(entry["line_number"]) == other and int(entry["count"]) >= 1
                 for found in record.get("files", ()) for entry in found.get("lines", ())):
    os.remove(report)
    sys.stderr.write("(ERROR) Error occurred while reading reports: the stand-in fails")
    sys.exit(1)
"""


@pytest.fixture
def gcovr_refusing(tmp_path):
    """The context variables that put first on PYTHONPATH a gcovr that reads
    as the real one and refuses the counts in which the target's ``line``
    ran at least ``least`` times, writing ``output`` and exiting with 64 as
    gcovr does on a negative count (``GCOVR_REFUSING``): one test's own
    counts can be refused while another's are read. Counts in which the
    line ``other`` ran, and that it does not refuse, it fails to read in
    another way, exiting with 1."""
    def make(line: int, output: str, least: int = 1, other: int = 0) -> dict:
        package = tmp_path / "refusing" / "gcovr"
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text(GCOVR_REFUSING, encoding="utf-8")
        return {"PYTHONPATH": str(package.parent), "GCOVR_REFUSING_LINE": str(line),
                "GCOVR_REFUSING_LEAST": str(least), "GCOVR_REFUSING_OUTPUT": output,
                "GCOVR_REFUSING_OTHER": str(other)}
    return make


@pytest.fixture
def gcovr_stand_in(tmp_path):
    """The context variables that put first on PYTHONPATH a gcovr that
    writes ``output`` and exits with ``status`` (``GCOVR_STAND_IN``)."""
    def make(status: int, output: str) -> dict:
        package = tmp_path / "stand_in" / "gcovr"
        package.mkdir(parents=True, exist_ok=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "__main__.py").write_text(GCOVR_STAND_IN, encoding="utf-8")
        return {"PYTHONPATH": str(package.parent), "GCOVR_STAND_IN_STATUS": str(status),
                "GCOVR_STAND_IN_OUTPUT": output}
    return make
