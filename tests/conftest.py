"""The test session's temporary directory for the library's own directories.

The library keeps a failed run's scratch directory for diagnosis, and many tests
fail runs on purpose. Their directories go below pytest's rotated base directory,
not the system's temporary directory, where they would pile up.

It also gives tests the determinism instrument as a suite delivered with it
carries it in conftest.py."""
import os
import tempfile
from pathlib import Path, PureWindowsPath

import pytest

_VARIABLES = ("TEMP", "TMP", "TMPDIR")
#: The system's temporary directory, before the session moves it.
_SYSTEM_TEMPORARY = tempfile.gettempdir()


@pytest.fixture(scope="session")
def system_temporary_dir():
    """The system's temporary directory, for a test that needs its copy apart
    from the test's own directory. The test removes what it makes there."""
    return _SYSTEM_TEMPORARY


@pytest.fixture(scope="session", autouse=True)
def library_temporary_dir(tmp_path_factory):
    temp_dir = tmp_path_factory.mktemp("system-temp", numbered=False)
    saved = {name: os.environ.get(name) for name in _VARIABLES}
    saved_dir = tempfile.tempdir
    for name in _VARIABLES:
        os.environ[name] = str(temp_dir)
    tempfile.tempdir = str(temp_dir)
    try:
        yield temp_dir
    finally:
        tempfile.tempdir = saved_dir
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


#: The determinism instrument as the conftest.py of a suite delivered before
#: it left LangAdapter carries it, which the adapter still
#: recognizes (``carried_instrument``) so that it loads its own journal and
#: leak plugins beside it only once.
CARRIED_INSTRUMENT = (Path(__file__).parent / "python" / "carried_instrument.txt").read_text(
    encoding="utf-8")


@pytest.fixture
def registry_kept():
    """The test's extension loads leave the registry, the loaded files and
    the imported modules as they were."""
    import sys

    from langadapter import registry
    kept = (dict(registry._by_name), dict(registry._by_file_extension), dict(registry._loaded),
            list(registry._metric_providers), set(sys.modules))
    yield
    registry._by_name.clear()
    registry._by_name.update(kept[0])
    registry._by_file_extension.clear()
    registry._by_file_extension.update(kept[1])
    registry._loaded.clear()
    registry._loaded.update(kept[2])
    registry._metric_providers[:] = kept[3]
    for name in set(sys.modules) - kept[4]:
        del sys.modules[name]


@pytest.fixture
def carried_instrument():
    """The carried instrument's text."""
    return CARRIED_INSTRUMENT


@pytest.fixture
def carry_instrument():
    """Write the carried instrument as ``conftest.py`` into a directory, as a
    suite delivered with it carries it, and return the file. An existing
    conftest.py stays as it is."""
    def carry(suite_dir):
        conftest = Path(suite_dir) / "conftest.py"
        if not conftest.exists():
            conftest.write_text(CARRIED_INSTRUMENT, encoding="utf-8")
        return conftest
    return carry


#: pytest's configuration files in its order, each with the section that makes
#: it one, for the stand-in below.
_PYTEST_CONFIGURATIONS = (("pytest.ini", "[pytest]"), ("pyproject.toml", "[tool.pytest.ini_options]"),
                          ("tox.ini", "[pytest]"), ("setup.cfg", "[tool:pytest]"))


@pytest.fixture(autouse=True)
def stand_in_pytest_configuration(monkeypatch):
    """The project's pytest configuration without starting pytest, for tests
    whose executors are stand-ins that cannot run it: the first of pytest's
    files in the project's root that holds its section, the file pytest reads
    for these tests' projects. test_pytest_configuration asks pytest itself."""
    from langadapter.python import adapter

    def from_the_root(executor, python, project_dir, cache=None, *, flags):
        for name, section in _PYTEST_CONFIGURATIONS:
            path = Path(str(project_dir or "")) / name
            if project_dir and path.is_file() and section in path.read_text(
                    encoding="utf-8", errors="ignore"):
                return f"{str(project_dir).rstrip('/')}/{name}"
        return None

    monkeypatch.setattr(adapter, "project_pytest_config", from_the_root)


@pytest.fixture(scope="session")
def stand_in_runners(tmp_path_factory):
    """A directory holding a stand-in docker, which starts no container and
    exits 1."""
    directory = tmp_path_factory.mktemp("stand-in-runners")
    if os.name == "nt":
        (directory / "docker.cmd").write_text(
            "@echo the stand-in runner starts no container 1>&2\r\n@exit /b 1\r\n",
            encoding="ascii")
    else:
        runner = directory / "docker"
        runner.write_text("#!/bin/sh\necho 'the stand-in runner starts no container' >&2\n"
                          "exit 1\n", encoding="ascii")
        runner.chmod(0o755)
    return directory


@pytest.fixture(autouse=True)
def stand_in_container_runner(stand_in_runners, monkeypatch):
    """A container context's runner check finds docker also on a machine
    without one, for the tests whose container executors are stand-ins: the
    stand-in sits at the end of PATH, so a real docker comes first."""
    monkeypatch.setenv("PATH", os.environ.get("PATH", "") + os.pathsep + str(stand_in_runners))


#: The marker of a test that starts a real mutation tool (the suite split):
#: such tests run in a selection of their own.
MUTATION_TOOL = "mutation_tool"
#: The Cosmic Ray and PIT commands the current test started.
_TOOL_STARTS = []


def starts_mutation_tool(args):
    """Whether a command starts Cosmic Ray or PIT as LangAdapter launches
    them: Cosmic Ray's console script, its command line in a Python -c, an
    HTTP worker or the container's session runner, PIT through its argument
    file or report class. A probe that only names a tool does not count."""
    words = ([os.fsdecode(args)] if isinstance(args, (str, bytes))
             else [os.fsdecode(a) if isinstance(a, (bytes, os.PathLike)) else str(a) for a in args])
    return bool(words) and (PureWindowsPath(words[0]).stem.lower() == "cosmic-ray" or any(
        mark in word for word in words
        for mark in ("cosmic_ray.cli", "'http-worker'", "_cr_session_runner", "pit.args",
                     "org.pitest")))


def pytest_configure(config):
    """Record each command LangAdapter's process tree starts that starts a
    mutation tool."""
    from langadapter import _process_tree

    original = _process_tree.start
    if getattr(original, "records_mutation_tools", False):
        return

    def start(args, **options):
        if starts_mutation_tool(args):
            _TOOL_STARTS.append(args)
        return original(args, **options)

    start.records_mutation_tools = True
    _process_tree.start = start


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    _TOOL_STARTS.clear()


@pytest.fixture(autouse=True)
def mutation_tool_guard(request):
    """Fail a test that starts Cosmic Ray or PIT, in its fixtures' setup or
    itself, without the mutation_tool marker. It sees the pytest process
    only: a test whose child process starts a tool is marked by hand."""
    yield
    if _TOOL_STARTS and request.node.get_closest_marker(MUTATION_TOOL) is None:
        command = " ".join(map(str, _TOOL_STARTS[0]))[:200]
        pytest.fail(f"the test starts a mutation tool ({command}) without the "
                    f"{MUTATION_TOOL} marker", pytrace=False)
