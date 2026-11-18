"""Python suite setup, pytest configuration, interpreter environments
and the bytecode store.

A suite staged outside the project would make pytest search the suite's
ancestors for a configuration and collect unrelated directories, so the project's
configuration is copied beside it and named with ``-c`` (``isolation_args``).
A fresh prepared copy
compiles every module again, so ``BytecodeStore`` keeps what one session's
copies compiled and writes it into each new copy outside the timed
intervals: pass it as ``prepare_project(..., bytecode=store)``.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import List, Optional, Tuple, Callable, Dict, Mapping, Sequence
from langadapter.context import ContainerExecution, ProjectContext
from langadapter.errors import InvalidRequest
from langadapter.execution import _mounted_work_dir, rebase_path
from langadapter.outcomes import Outcome
from langadapter.suites import Suite
import contextlib
import os
import tempfile
import sys
import json
import posixpath
from langadapter import _process_tree
from langadapter._owned_directory import OwnedDirectory


#: The line that opens the determinism instrument in the setup file of a
#: suite delivered with it. What precedes it is the suite's import shim.
_INSTRUMENT_MARKER = "# Deterministic random state for every test."
#: The stem of the project's pytest configuration copied beside a suite.
_PYTEST_CONFIG_COPY = "project_pytest"


@dataclass(frozen=True)
class SuitePreparation:
    """The setup written, including files left by a partially failed write.
    ``isolation_files`` holds the path of the anchoring ``pytest.ini``
    written into the suite root on request."""

    outcome: Outcome
    reason: str = ""
    setup_files: Tuple[str, ...] = ()
    isolation_files: Tuple[str, ...] = ()


def prepare_suite(adapter, suite_root: str, *,
                  isolation_config: bool = False) -> SuitePreparation:
    """Write ``conftest.py``, the import shim, into ``suite_root`` (replacing
    one there), with an anchoring ``pytest.ini`` only on request. The directory
    must exist, in a container context within the
    shared work directory, else ``InvalidRequest``. The shim finds the import root
    from its own place, in a container by the container's paths."""
    root = Path(suite_root).resolve()
    if not root.is_dir():
        raise InvalidRequest("suite_root", f"no directory at {root}")
    if not adapter.context.within_work_dir(str(root)):
        raise InvalidRequest("suite_root", "must lie within the shared work folder (execution.work_dir)")
    execution = adapter.context.execution
    executing_root = None
    if isinstance(execution, ContainerExecution):
        executing_root = rebase_path(
            str(root), execution.work_dir,
            _mounted_work_dir(execution))
    try:
        _setup_test_env(adapter,
            str(root), adapter.context.root,
            isolation_config=isolation_config,
            execution_suite_root=executing_root,
        )
    except OSError as exc:
        return SuitePreparation(
            Outcome.INFRASTRUCTURE_FAILURE, str(exc), setup_files(root),
            (str(root / "pytest.ini"),) if (root / "pytest.ini").is_file() else (),
        )
    return SuitePreparation(
        Outcome.COMPLETED, setup_files=setup_files(root),
        isolation_files=(str(root / "pytest.ini"),) if isolation_config else (),
    )


def setup_files(suite_root) -> Tuple[str, ...]:
    """The setup file of a prepared generated suite, if present."""
    conftest = Path(suite_root) / "conftest.py"
    return (str(conftest),) if conftest.is_file() else ()


def loaded_setup_files(suite_root: Path, test_files: List[Path]) -> List[Path]:
    """The conftest files pytest loads between the root and each test file,
    resolved on the host where the suite is stored, also in a container's work
    directory. A test file outside the root raises ``InvalidRequest``."""
    root = Path(suite_root).resolve()
    loaded = set()
    for test in test_files:
        directory = Path(test).resolve().parent
        if directory != root and root not in directory.parents:
            raise InvalidRequest("test_files", f"{test} does not lie under {root}")
        while True:
            candidate = directory / "conftest.py"
            if candidate.is_file():
                loaded.add(candidate)
            if directory == root:
                break
            directory = directory.parent
    return sorted(loaded)


def _setup_test_env(
    adapter,
    test_output_dir: str,
    project_root: str,
    *,
    isolation_config: bool = True,
    execution_suite_root: Optional[str] = None,
) -> None:
    """Write ``conftest.py``, and ``pytest.ini`` with
    ``isolation_config``, into the test directory.

    The conftest puts ``project_root`` and its parent on ``sys.path``,
    relative to its own location (``execution_suite_root`` where a
    container run sees the directory). A coverage run wraps the whole pytest process,
    so each module is imported once and keeps its identity.
    """
    from langadapter.python._plugins import SUITE_INI_TEXT, render

    conftest = Path(test_output_dir) / "conftest.py"
    # _PROJECT_ROOT is computed when the conftest runs: Cosmic Ray's http
    # workers copy the test directory, and a baked-in path would make every
    # worker import the unmutated source (a 0% kill rate). The expression
    # walks up from __file__ to the common parent of the test directory and
    # project_root, then down project_root's subpath.
    if execution_suite_root is None:
        confpath_dir = Path(test_output_dir).resolve()
        root_abs = Path(project_root).resolve()
    else:
        confpath_dir = PurePosixPath(execution_suite_root)
        root_abs = PurePosixPath(project_root)
    try:
        if execution_suite_root is None:
            common = Path(os.path.commonpath([str(confpath_dir), str(root_abs)]))
        else:
            common = PurePosixPath(posixpath.commonpath(
                [str(confpath_dir), str(root_abs)]))
        levels_up = len(confpath_dir.relative_to(common).parts)
        sub = root_abs.relative_to(common)
        rel_expr = "os.path.abspath(os.path.dirname(__file__))"
        for _ in range(levels_up):
            rel_expr = f"os.path.dirname({rel_expr})"
        # POSIX separators: on a Windows host str(sub) would give a Linux
        # container one file name with backslashes. Windows accepts "/".
        sub_posix = sub.as_posix()
        if sub_posix and sub_posix != ".":
            project_root_expr = (
                f"os.path.join({rel_expr}, {repr(sub_posix)})"
            )
        else:
            project_root_expr = rel_expr
    except (ValueError, OSError):
        # No common path (another drive on Windows): an absolute path,
        # which breaks worker clones but works in the original work directory.
        project_root_expr = repr(project_root)

    conftest.write_text(
        render(f"""import sys
import os

# project_root resolved relative to this conftest's location (so worker
# clones / renamed work zones still find the right import root).
#
# Mutation override: a cosmic-ray worker runs THIS conftest from the
# live test dir (shared, sibling-layout) but must import the source
# from its OWN clone, where the mutation lives. The __file__-relative
# expression below resolves to the live (unmutated) source, so without
# this override every mutant survives. The worker sets
# @PREFIX@MUTATION_SOURCE_ROOT to its clone's source root; when present it
# takes precedence so the worker imports the mutated copy.
_MUT_SOURCE_ROOT = os.environ.get("@PREFIX@MUTATION_SOURCE_ROOT")
if _MUT_SOURCE_ROOT:
    _PROJECT_ROOT = _MUT_SOURCE_ROOT
else:
    _PROJECT_ROOT = {project_root_expr}
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, os.path.dirname(_PROJECT_ROOT))

# Open-file watcher (diagnostic, off unless @PREFIX@FDLIMIT_DEBUG is set): a daemon
# thread polls this process's own descriptor count and, the first time it
# crosses @PREFIX@FDLEAK_THRESHOLD, writes a histogram of what the descriptors
# point to into @PREFIX@FDLEAK_DIR. It runs inside the affected pytest, so it
# catches a sub-second descriptor climb that an external sampler misses, and
# names the leaking handles. Zero footprint when the env var is unset.
if os.environ.get("@PREFIX@FDLIMIT_DEBUG"):
    def _fd_watch():
        import time, collections, re
        _d = "/proc/self/fd"
        _thr = int(os.environ.get("@PREFIX@FDLEAK_THRESHOLD", "3000"))
        _out = (os.environ.get("@PREFIX@FDLEAK_DIR")
                or os.environ.get("@PREFIX@PROJECT_WORKZONE") or ".")
        _done = False
        while not _done:
            try:
                _fds = os.listdir(_d)
            except OSError:
                return
            if len(_fds) >= _thr:
                _hist = collections.Counter()
                for _f in _fds:
                    try:
                        _t = os.readlink(os.path.join(_d, _f))
                    except OSError:
                        _t = "?"
                    _hist[re.sub(r"[0-9]+", "N", _t)] += 1
                try:
                    with open(os.path.join(_out, "fdleak_%d.txt" % os.getpid()),
                              "w") as _fh:
                        _fh.write("total=%d\\n" % len(_fds))
                        for _tt, _cc in _hist.most_common(30):
                            _fh.write("%d\\t%s\\n" % (_cc, _tt))
                except OSError:
                    pass
                _done = True
            time.sleep(0.01)
    import threading as _threading
    _threading.Thread(target=_fd_watch, daemon=True).start()

"""),
        encoding="utf-8",
    )

    # An empty [pytest] section makes this directory the rootdir, before an
    # ancestor's possibly strict configuration. The tests run on defaults.
    pytest_ini = Path(test_output_dir) / "pytest.ini"
    if isolation_config:
        pytest_ini.write_text(SUITE_INI_TEXT, encoding="utf-8")


_TRACEBACK_STYLES = ("auto", "long", "short", "line", "native", "no")


@dataclass(frozen=True)
class PytestOptions:
    """Traceback style, first-failure stopping, disabled pytest plugins and,
    with ``leave_out_timing_plugins``, the plugins that change a run's time
    left out where the project's pytest loads them: pytest-cov's tracer
    (``--no-cov``), pytest-xdist's workers (``-n 0``) and pytest-benchmark's
    measurement (``--benchmark-disable``). The project's own options for
    them stay valid."""

    traceback: str = "short"
    stop_at_first_failure: bool = False
    disabled_plugins: tuple[str, ...] = ()
    leave_out_timing_plugins: bool = False

    def validate(self) -> None:
        """Refuse an unknown traceback style, a disabled plugin that is no
        plugin name or a ``leave_out_timing_plugins`` that is no boolean
        with ``InvalidRequest``, naming the field."""
        if self.traceback not in _TRACEBACK_STYLES:
            raise InvalidRequest(
                "runner_options.traceback",
                f"must be one of {', '.join(_TRACEBACK_STYLES)}")
        for name in self.disabled_plugins:
            if not name or name.startswith("-") or any(c.isspace() for c in name):
                raise InvalidRequest(
                    "runner_options.disabled_plugins", f"not a plugin name: {name!r}")
        if not isinstance(self.leave_out_timing_plugins, bool):
            raise InvalidRequest("runner_options.leave_out_timing_plugins", "must be bool")

def _probe(report: str) -> str:
    """A pytest start in the current directory that runs the statement
    ``report``, with ``config`` in scope, once pytest is configured, and
    stops before it collects. ``-o addopts=`` keeps options for plugins the
    environment lacks from stopping it. Its arguments go to pytest."""
    return ("import sys, pytest\n"
            "class Probe:\n"
            "    def pytest_configure(self, config):\n"
            f"        {report}\n"
            "        pytest.exit('', returncode=0)\n"
            "sys.exit(pytest.main(['-o', 'addopts=', '-p', 'no:cacheprovider', *sys.argv[1:],\n"
            "                     '--co', '-q', '.'], plugins=[Probe()]))\n")


#: Prints the configuration file pytest reads when it starts in the current
#: directory (``config.inipath``).
_CONFIG_PROBE = _probe("print('pytest configuration:', config.inipath or '', flush=True)")

#: The pytest plugins that change a run's time, by the name each registers
#: under, with the flags that leave each out where it is loaded
#: (``PytestOptions.leave_out_timing_plugins``): pytest-cov's tracer,
#: pytest-xdist's workers and pytest-benchmark's measurement.
_TIMING_PLUGINS = (("pytest_cov", ("--no-cov",)), ("xdist", ("-n", "0")),
                   ("benchmark", ("--benchmark-disable",)))

#: Prints which of ``_TIMING_PLUGINS`` pytest holds.
_PLUGIN_PROBE = _probe(
    f"print('pytest plugins:', *[name for name in {tuple(name for name, _ in _TIMING_PLUGINS)!r}"
    " if config.pluginmanager.has_plugin(name)], flush=True)")


class _PytestMissing(RuntimeError):
    """The project's interpreter cannot import pytest."""


def _probed(executor, python: str, key: str, script: str, label: str, *,
            flags: Sequence[str]) -> str:
    """What the probe ``script`` prints after ``pytest <label>:``, run by the
    project's interpreter ``python`` with ``flags`` in ``key`` through
    ``executor`` with the plugins every run disables disabled. Raises ``_PytestMissing`` when the
    interpreter cannot import pytest, else a RuntimeError with the probe's
    exit status and output when it prints none."""
    from langadapter.python._run_definition import DISABLED_PLUGINS

    disabled = [argument for name in DISABLED_PLUGINS for argument in ("-p", f"no:{name}")]
    done = executor.run([python, *flags, "-c", script, *disabled], cwd=key,
                        timeout=_COLLECTION_TIMEOUT_S)
    named = [line.split(":", 1)[1].strip() for line in (done.stdout or "").splitlines()
             if line.startswith(f"pytest {label}:")]
    if named:
        return named[0]
    if "No module named 'pytest'" in (done.stderr or ""):
        raise _PytestMissing(
            f"pytest is not installed in the project's interpreter {python}")
    raise RuntimeError(
        f"pytest named no {label} for {key} (exit {done.returncode}): "
        f"{(done.stderr or done.stdout or '').strip()[-500:]}")


def project_pytest_config(
    executor,
    python: str,
    project_dir: Optional[str],
    cache: Optional[Dict[str, Optional[str]]] = None,
    *,
    flags: Sequence[str],
) -> Optional[str]:
    """The configuration file pytest reads for ``project_dir``, by pytest's own
    order and rules, asked of the project's interpreter ``python`` with
    ``flags`` through ``executor`` with the plugins every run disables
    disabled, or None when pytest reads none. ``cache``, when given, holds
    the answer per directory. Raises RuntimeError when the interpreter
    cannot import pytest or pytest names none for another reason."""
    if not project_dir:
        return None
    key = str(project_dir).rstrip("/")
    if cache is not None and key in cache:
        return cache[key]
    found = _probed(executor, python, key, _CONFIG_PROBE, "configuration",
                    flags=flags) or None
    if cache is not None:
        cache[key] = found
    return found


def _loaded_timing_plugins(executor, python: str, project_dir: str, *,
                           flags: Sequence[str]) -> Tuple[str, ...]:
    """The names of ``_TIMING_PLUGINS`` the project's pytest loads in
    ``project_dir`` (``_PLUGIN_PROBE``), asked of its interpreter ``python``
    with ``flags`` through ``executor``. Raises as ``project_pytest_config`` does."""
    return tuple(_probed(executor, python, str(project_dir).rstrip("/"), _PLUGIN_PROBE,
                         "plugins", flags=flags).split())


def isolation_args(
    config_path: Optional[str],
    read_file: Callable[[str], Optional[str]],
    suite_root: Optional[str] = None,
    *,
    execution_suite_root: Optional[str] = None,
) -> List[str]:
    """pytest arguments isolating a staged suite: no cache provider and, for a
    project configuration ``config_path``, its exact copy ``_PYTEST_CONFIG_COPY<suffix>``
    in ``suite_root`` with ``-c``, named under ``execution_suite_root`` (as a
    worker clone or container sees the root) when given. ``ValueError`` without
    a suite root, ``RuntimeError`` when it cannot be read or copied."""
    args = ["-p", "no:cacheprovider"]
    if not config_path:
        return args
    if not suite_root:
        raise ValueError(
            "pytest isolation requires suite_root when the project has a "
            "pytest config")
    text = read_file(config_path)
    if text is None:
        raise RuntimeError(
            f"could not read resolved pytest config: {config_path}")
    suffix = Path(config_path).suffix.lower()
    if suffix not in {".ini", ".cfg", ".toml"}:
        raise RuntimeError(
            f"unsupported pytest config extension {suffix!r}: {config_path}")
    root = Path(suite_root)
    if not root.is_dir():
        raise RuntimeError(f"pytest suite root is not a directory: {suite_root}")
    local_copy = root / f"{_PYTEST_CONFIG_COPY}{suffix}"
    try:
        current = (local_copy.read_text(encoding="utf-8")
                   if local_copy.exists() else None)
        if current != text:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", newline="", dir=root,
                prefix=f".{local_copy.name}.", suffix=".tmp", delete=False,
            ) as handle:
                handle.write(text)
                temporary = Path(handle.name)
            try:
                os.replace(temporary, local_copy)
            finally:
                with contextlib.suppress(OSError):
                    temporary.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"could not materialize pytest config in suite root "
            f"{suite_root}: {exc}") from exc
    if execution_suite_root is None:
        execution_copy = str(local_copy)
    else:
        execution_copy = (
            f"{str(execution_suite_root).replace(chr(92), '/').rstrip('/')}"
            f"/{local_copy.name}")
    return args + ["-c", execution_copy]


#: How long pytest may take to collect a directory's tests (``_collected_test_files``).
_COLLECTION_TIMEOUT_S = 600


def _collected_test_files(adapter, test_paths, timeout_s: float = _COLLECTION_TIMEOUT_S, *,
                         configured: bool = False) -> List[str]:
    """Each path, a directory replaced by the test files pytest collects in it
    with a run's settings (the project's pytest configuration, the directory as
    rootdir and confcutdir): the files it collects tests from, then those it
    could not collect, as the run record names them. ``configured`` names no
    directory to pytest, so the configuration's ``testpaths`` bound what it
    collects below the directory. A directory without Python files has none.
    Collecting imports the directory's test modules. Raises RuntimeError when
    pytest gives no account of a directory (a usage error, a crash, the
    timeout), naming pytest's diagnostic where it gave one."""
    from langadapter.python._run_definition import DISABLED_PLUGINS
    from langadapter.python._runs import _SuiteRunner
    from langadapter.runs import SuiteRunRequest

    files: List[str] = []
    for path in test_paths:
        test_dir = Path(path)
        if not test_dir.is_dir():
            files.append(str(path))
            continue
        tests = sorted(found for found in test_dir.rglob("*.py")
                       if "__pycache__" not in found.parts and found.name != "conftest.py")
        if not tests:
            continue
        suite = Suite(str(test_dir), tuple(map(str, tests)),
                      tuple(map(str, loaded_setup_files(test_dir, tests))))
        run = _SuiteRunner(adapter, SuiteRunRequest(
            suite, timeout_s, runner_options=PytestOptions(disabled_plugins=DISABLED_PLUGINS),
        )).collect(targets=[str(test_dir)], configured=configured)
        if not run.files and run.outcome not in (Outcome.COMPLETED, Outcome.NO_TESTS):
            raise RuntimeError(f"pytest could not collect {test_dir}: {run.reason}"
                               + (f": {run.diagnostic}" if run.diagnostic else ""))
        files.extend(str(test_dir / name) for name in run.files)
    return files


@dataclass(frozen=True)
class _EnvironmentDiscovery:
    """The interpreter addresses looked at and the existing ones, one per
    environment (an activated environment that is the project's ``.venv``
    appears once). ``callers_own`` is the activated environment this process
    runs in when it lies outside the project: it is not among ``existing``,
    since a project runs with it only when it is selected."""

    candidates: Tuple[str, ...]
    existing: Tuple[str, ...]
    callers_own: Optional[str] = None

    @property
    def selected(self) -> Optional[str]:
        """The only existing environment's interpreter, else None."""
        return self.existing[0] if len(self.existing) == 1 else None


def _environment_addresses(root: str, environment: Mapping[str, str], *,
                          posix: bool = False) -> Tuple[str, ...]:
    """The interpreter addresses of the project's ``.venv`` and the activated
    environments (``VIRTUAL_ENV``, ``CONDA_PREFIX`` from ``environment``, never
    this process, relative homes ignored), not checked to exist: ``bin/python``,
    on Windows ``python.exe`` at a Conda root or in a virtual environment's
    ``Scripts``. ``posix`` gives the Linux form for a container."""
    return tuple(address for _, address in _environments(root, environment, posix=posix))


def _environments(root, environment, *, posix=False):
    """Each candidate environment directory with its interpreter address, once."""
    path = PurePosixPath if posix else Path
    windows = not posix and os.name == "nt"
    homes = [(path(root) / ".venv", False)]
    homes += [(path(environment[key]), key == "CONDA_PREFIX")
              for key in ("VIRTUAL_ENV", "CONDA_PREFIX") if environment.get(key)]
    found = []
    for home, conda in homes:
        if not home.is_absolute():
            continue
        if not windows:
            executable = home / "bin" / "python"
        else:
            executable = home / "python.exe" if conda else home / "Scripts" / "python.exe"
        if all(str(executable) != address for _, address in found):
            found.append((home, str(executable)))
    return found


def _discover_environments(root: str, environment: Mapping[str, str]) -> _EnvironmentDiscovery:
    """The project's environments, without choosing: ``root/.venv`` and those
    ``VIRTUAL_ENV`` and ``CONDA_PREFIX`` in ``environment`` name, never the
    calling interpreter. An environment is its directory, so a ``.venv`` that is
    also the activated one counts once and two made from one base Python stay
    two. The activated environment this process runs in, outside the project
    (a tool's own), is named apart as ``callers_own``, not counted.
    Environments inside a container are not discovered: select the
    container's interpreter."""
    environments = _environments(root, environment)
    found, seen, own = [], set(), None
    for home, value in environments:
        try:
            if not Path(value).is_file():
                continue
            identity = os.path.normcase(os.path.realpath(home))
        except OSError:
            continue
        if identity in seen:
            continue
        seen.add(identity)
        if _callers_own_environment(value, root):
            own = value
        else:
            found.append(value)
    return _EnvironmentDiscovery(tuple(value for _, value in environments), tuple(found), own)


def _discovered_interpreter(root: str, environment: Mapping[str, str]) -> str:
    """The interpreter of the only environment ``_discover_environments`` finds
    at ``root``, when none is selected. Raises ``InvalidRequest`` on
    python_executable when there is none or several, or when the only one is
    the caller's own activated environment outside the project, which a project
    uses only when it is selected. Beside a project environment it is left
    aside."""
    from langadapter.errors import InvalidRequest

    found = _discover_environments(root, environment)
    if found.selected is not None:
        return found.selected
    if not found.existing and found.callers_own is not None:
        raise InvalidRequest(
            "python_executable",
            f"the only environment found is the one the caller runs in ({sys.prefix}), "
            "activated in the shell, and a project runs with it only when it is selected: "
            "select the project's interpreter, or this one on purpose, or create the "
            "project's .venv")
    if not found.existing:
        raise InvalidRequest(
            "python_executable",
            "no interpreter is selected, and no project .venv or activated environment was "
            "found (looked at: " + (", ".join(found.candidates) or "no address") + "): select "
            "one, or set python_version")
    raise InvalidRequest("python_executable", "several interpreters could run the project ("
                         + "; ".join(found.existing) + "): select one")


def _callers_own_environment(interpreter: str, root: str) -> bool:
    """Whether ``interpreter`` belongs to the environment this process runs
    in and that environment lies outside the project at ``root``: a project
    runs with it only when it is selected."""
    return _is_own_environment(interpreter) and not _lies_inside(interpreter, root)


def _environment_dirs(interpreter: str) -> set:
    # A virtual environment keeps its interpreter in Scripts or bin, a Windows
    # Conda environment keeps it at its root.
    path = Path(interpreter)
    return {os.path.normcase(os.path.realpath(env_dir)) for env_dir in (path.parent, path.parent.parent)}


def _is_own_environment(interpreter: str) -> bool:
    """Whether ``interpreter`` belongs to the environment this process runs in."""
    return os.path.normcase(os.path.realpath(sys.prefix)) in _environment_dirs(interpreter)


def _lies_inside(interpreter: str, root: str) -> bool:
    """Whether ``interpreter``'s environment lies inside the project at
    ``root``, whatever its name (.venv, venv, env or a Conda prefix there)."""
    top = os.path.normcase(os.path.realpath(root))
    for env_dir in _environment_dirs(interpreter):
        try:
            if env_dir != top and os.path.commonpath([top, env_dir]) == top:
                return True
        except ValueError:
            continue  # another drive is never inside the project
    return False


def environment_dir(interpreter: str, root: str) -> Optional[str]:
    """The project-relative directory (holding ``pyvenv.cfg`` or ``conda-meta``) of
    ``interpreter``'s environment inside ``root``, else None, compared as
    written. It is the run's runtime, not a project input, and stays out of
    project copies (a POSIX virtual environment's links cannot be copied)."""
    path = Path(os.path.abspath(interpreter))
    top = Path(os.path.abspath(root))
    for home in (path.parent, path.parent.parent):
        if (home / "pyvenv.cfg").is_file() or (home / "conda-meta").is_dir():
            try:
                relative = home.relative_to(top)
            except ValueError:
                return None
            return relative.as_posix() if relative.parts else None
    return None


class BytecodeStore:
    """An owned directory of compiled modules, keyed by the compiling interpreter
    (magic number, cache tag), the part of the copy (the project or a suite
    directory beside it), the path in that part and the source's SHA-256. The
    project's interpreter writes into a copy only its own entries of unchanged
    sources, and stores a module only when its code equals a fresh compile.
    Entries are written whole, so sharing sessions never see a partial one.
    ``close`` removes the directory."""

    def __init__(self, owned: OwnedDirectory) -> None:
        self._owned = owned

    @classmethod
    def create(cls, *, parent: Optional[str] = None,
               receipt_parent: Optional[str] = None) -> "BytecodeStore":
        """A new store below ``parent``, by default the system's temporary
        directory. A container's commands reach only a store in its work directory."""
        return cls(OwnedDirectory.create(parent=parent, prefix="langadapter_bytecode_",
                                         receipt_parent=receipt_parent))

    @property
    def root(self) -> str:
        """The store's directory."""
        return str(self._owned.root)

    def close(self) -> None:
        """Remove the store. A store whose ownership cannot be verified is
        kept, and ``OwnershipRefused`` is raised."""
        self._owned.remove()

    def seed(self, context: ProjectContext,
             parts: Sequence[Tuple[str, str]]) -> Optional[int]:
        """Write the stored modules of each part's sources beside them, and
        return the number written (None without a count). A part is a name and
        its sources' directory as the context's commands see it. Entries are kept
        by part and relative path, so a copy placing a part elsewhere finds them."""
        return self._run(context, "seed", parts)

    def harvest(self, context: ProjectContext,
                parts: Sequence[Tuple[str, str]]) -> Optional[int]:
        """Store the modules the context's runs compiled below each part's
        directory, and return the number stored: None without a count or after a
        cancel, whose shortened runs leave nothing worth keeping."""
        if _process_tree.cancelled():
            return None
        return self._run(context, "harvest", parts)

    def _run(self, context: ProjectContext, operation: str,
             parts: Sequence[Tuple[str, str]]) -> Optional[int]:
        from langadapter._execution_scope import reraise_unresolved
        from langadapter.execution import container_path, executor_for

        python = context.interpreter.path
        if python is None:
            return None
        store = self.root
        if isinstance(context.execution, ContainerExecution):
            store = container_path(context.execution, store)
        join = posixpath.join if isinstance(context.execution, ContainerExecution) else os.path.join
        helper = Path(__file__).with_name("_bytecode_helper.py").read_text(encoding="utf-8")
        command = [python, *context.interpreter_flags, "-I", "-c", helper, operation,
                   *(item for name, root in parts for item in (join(store, name), root))]
        try:
            done = executor_for(context).run(command, timeout=600)
            if done.returncode != 0:
                return None
            return json.loads((done.stdout or "").strip().splitlines()[-1])[operation]
        except Exception as error:
            # An optimization only: a run without the store compiles its own.
            reraise_unresolved(error)
            return None
