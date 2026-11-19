"""The Cosmic Ray engine's run with subprocesses and workers mocked: the
init, exec and parse pipeline and the session lifecycle."""
from __future__ import annotations

import contextlib
import json
import shlex
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from langadapter.python.cosmic_ray import CosmicRaySettings
import langadapter as lb
from langadapter._linux_job import ControlError
from langadapter.python import _cosmic_ray as cre
from langadapter.python._cr_session_runner import WORKER_SECRET, http_exec_command
from langadapter.python.adapter import PythonAdapter


@pytest.fixture(autouse=True)
def no_native_processes(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("controlled mutation tests must not launch native commands")

    monkeypatch.setattr(cre.subprocess, "Popen", forbidden)
    monkeypatch.setattr(cre._process_tree, "start", forbidden)
    monkeypatch.setattr(cre._process_tree, "run", lambda *args, **kwargs:
                        cre.subprocess.run(*args, capture_output=True, **kwargs))
    monkeypatch.setattr(cre, "_project_cosmic_ray_metadata", lambda executor, python, *, flags: (
        "8.4.1", ["core/ReplaceAndWithOr"], f"py{sys.version_info.major}.{sys.version_info.minor}"))
    # Nothing shadows the project's source. The doubles answer every command
    # with empty output, which the probe would refuse (test_editable_install.py
    # runs the probe).
    monkeypatch.setattr(cre.CosmicRayEngine, "_source_dirs",
                        lambda self, project_root, env: ((), (), ()))
    # The tests import the module from the project (test_import_origin.py
    # runs the origin check).
    monkeypatch.setattr(cre.CosmicRayEngine, "_origin_check",
                        lambda self, test_command, **options: None)
    from langadapter.python._mutation import OwnedDirectory

    create = OwnedDirectory.create

    def allocate(cls, **options):
        options.setdefault("parent", str(tmp_path))
        options.setdefault("receipt_parent", str(tmp_path))
        return create(**options)

    monkeypatch.setattr(OwnedDirectory, "create", classmethod(allocate))


def _engine(root, config):
    """An engine for a local project at ``root`` in this interpreter."""
    context = lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable))
    return cre.CosmicRayEngine({**CosmicRaySettings()._engine_config("local"), **config}, adapter=PythonAdapter(context))


def _run(engine, *, project_root, workers=1, **request):
    """One run of ``engine`` in a session of its own."""
    with engine.session(project_root=project_root, workers=workers) as session:
        return session.run(**request)


def _build_cr_session_db(
    db_path: Path,
    *,
    catalogue: int = 10,
    outcomes: dict = None,
) -> None:
    """Build a Cosmic Ray-shaped session sqlite db."""
    if outcomes is None:
        outcomes = {"KILLED": 5, "SURVIVED": 3, ("KILLED", "timeout"): 1}
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE work_items (job_id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE work_results "
            "(job_id TEXT, test_outcome TEXT, worker_outcome TEXT, diff TEXT, output TEXT)"
        )
        for i in range(catalogue):
            conn.execute("INSERT INTO work_items VALUES (?)", (f"j{i}",))
        idx = 0
        for outcome, count in outcomes.items():
            # ("KILLED", "timeout") is Cosmic Ray's record of a mutant timeout.
            test_outcome, output = outcome if isinstance(outcome, tuple) else (outcome, "")
            for _ in range(count):
                conn.execute(
                    "INSERT INTO work_results VALUES (?, ?, ?, ?, ?)",
                    (f"j{idx}", test_outcome, "NORMAL", "diff", output),
                )
                idx += 1
        conn.commit()
    finally:
        conn.close()


def _killed(db_path: Path) -> int:
    """The assertion kills (not the clock's) in a session database: the
    fakes' sign of which session a run used."""
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM work_results WHERE test_outcome = 'KILLED' "
            "AND coalesce(output, '') != 'timeout'").fetchone()[0]


# ---------------------------------------------------------------------------
# One run in a session -- single-worker (local distributor) full path
# ---------------------------------------------------------------------------

def test_a_single_worker_run_completes_on_its_session(monkeypatch, tmp_path):
    """Single-worker (local distributor) run: init succeeds, exec
    succeeds, and the run ends without the wall clock on the session
    init made."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "setup.cfg").write_text(
        "[tool:pytest]\nfilterwarnings = default\n",
        encoding="utf-8",
    )
    target = project_root / "foo.py"
    target.write_text("def f(): return 1\n", encoding="utf-8")
    test_dir = project_root / "tests"
    test_dir.mkdir()

    session_path = project_root / "cr-session.sqlite"

    def fake_run(args, **kw):
        argv = list(args)
        if "init" in argv:
            # As Cosmic Ray's init, create the session file.
            _build_cr_session_db(
                session_path,
                catalogue=10,
                outcomes={"KILLED": 6, "SURVIVED": 2, ("KILLED", "timeout"): 1},
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "exec" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cre.subprocess, "run", fake_run)
    copied_config = test_dir / "project_pytest.cfg"
    seen = []

    def start(*args, **kwargs):
        # The settings copy the tests read during the run.
        seen.append(copied_config.read_text(encoding="utf-8"))
        return _FakePopen(poll_returns=(0,))

    monkeypatch.setattr(cre._process_tree, "start", start)
    monkeypatch.setattr(cre.time, "sleep", lambda *_a, **_k: None)
    # The session lives in ``project_root``, where fake_run builds it,
    # instead of the default temporary directory.
    engine = _engine(
        project_root,
        {"log_fn": None,
         "cosmic_ray_session_dir": str(project_root)},
    )
    out = _run(engine,
        target=target,
        project_root=project_root,
        test_dir=test_dir,
        workers=1,
    )
    assert out is False
    assert _killed(session_path) == 6
    assert seen and set(seen) == {"[tool:pytest]\nfilterwarnings = default\n"}
    # The session's copy is gone with the session.
    assert not copied_config.exists()
    cosmic_config = (project_root / "cosmic-ray.toml").read_text(
        encoding="utf-8"
    )
    assert "project_pytest.cfg" in cosmic_config
    assert str(project_root / "setup.cfg") not in cosmic_config


def test_each_path_of_the_per_mutant_command_stays_one_argument(monkeypatch, tmp_path):
    """Cosmic Ray splits the per-mutant command with ``shlex.split``. A project
    directory with a space and an apostrophe, and a suite of two files, give
    pytest each file as one argument, in the run's command and the check's
    before the first mutant."""
    project_root = tmp_path / "my proj's"
    project_root.mkdir()
    target = project_root / "foo.py"
    target.write_text("def f(): return 1\n", encoding="utf-8")
    test_dir = project_root / "tests"
    test_dir.mkdir()
    for name in ("test_a.py", "test_b.py"):
        (test_dir / name).write_text("def test_f():\n    pass\n", encoding="utf-8")
    session_path = project_root / "cr-session.sqlite"

    def fake_run(args, **kw):
        if "init" in list(args):
            _build_cr_session_db(session_path)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    commands = []
    write = cre.CosmicRayEngine._write_config_toml

    def recorded(self, config_path, **settings):
        commands.append(settings["test_command"])
        return write(self, config_path, **settings)

    monkeypatch.setattr(cre.subprocess, "run", fake_run)
    monkeypatch.setattr(cre._process_tree, "start",
                        lambda *args, **kwargs: _FakePopen(poll_returns=(0,)))
    monkeypatch.setattr(cre.time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(cre.CosmicRayEngine, "_write_config_toml", recorded)
    monkeypatch.setattr(cre.CosmicRayEngine, "_origin_check",
                        lambda self, test_command, **options: commands.append(test_command))
    engine = _engine(project_root, {"log_fn": None,
                                    "cosmic_ray_session_dir": str(project_root)})
    _run(engine, target=target, project_root=project_root, test_dir=test_dir,
         test_files=["test_a.py", "test_b.py"], workers=1)
    assert len(commands) == 2
    for command in commands:
        argv = shlex.split(command)
        rootdir = next(a for a in argv if a.startswith("--rootdir="))[len("--rootdir="):]
        assert rootdir.endswith("my proj's/tests")
        files = argv[argv.index("pytest") + 1:]
        assert files[:2] == [f"{rootdir}/test_a.py", f"{rootdir}/test_b.py"]
        assert files[2].startswith("-")


def test_run_ends_when_init_fails(monkeypatch, tmp_path):
    """cosmic-ray init returns non-zero -> the run ends (``_abort``), not
    truncated, and no exec is attempted."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    target = project_root / "foo.py"
    target.write_text("", encoding="utf-8")
    test_dir = project_root / "tests"
    test_dir.mkdir()

    exec_calls = {"n": 0}

    def fake_run(args, **kw):
        argv = list(args)
        if "init" in argv:
            return SimpleNamespace(
                returncode=2, stdout="", stderr="init error",
            )
        if "exec" in argv:
            exec_calls["n"] += 1
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cre.subprocess, "run", fake_run)
    engine = _engine(tmp_path, {"log_fn": None})
    out = _run(engine,
        target=target,
        project_root=project_root,
        test_dir=test_dir,
        workers=1,
    )
    assert out is False
    assert exec_calls["n"] == 0


def test_run_ends_when_init_binary_missing(monkeypatch, tmp_path):
    """``cosmic-ray`` executable not on PATH -> FileNotFoundError -> the
    run ends, not truncated."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    target = project_root / "foo.py"
    target.write_text("", encoding="utf-8")
    test_dir = project_root / "tests"
    test_dir.mkdir()

    def fake_run(*a, **kw):
        raise FileNotFoundError("cosmic-ray not found")

    monkeypatch.setattr(cre.subprocess, "run", fake_run)
    engine = _engine(tmp_path, {"log_fn": None})
    out = _run(engine,
        target=target, project_root=project_root,
        test_dir=test_dir, workers=1,
    )
    assert out is False


class _FakePopen:
    """A ``subprocess.Popen`` double for the exec wait loop.

    ``poll_returns`` gives successive ``.poll()`` values (``None`` means
    still running), ``output`` is what ``.communicate()`` returns.
    """

    def __init__(self, *, poll_returns=(None,), output=("", "")) -> None:
        self._poll_iter = iter(poll_returns)
        self._last_poll = None
        self._output = output
        self.returncode = None
        self._langadapter_command_job = SimpleNamespace(close=lambda: None)

    def poll(self):
        try:
            self._last_poll = next(self._poll_iter)
        except StopIteration:
            pass  # keep returning the last value
        self.returncode = self._last_poll
        return self._last_poll

    def terminate(self):
        # The POSIX SIGTERM exit code.
        if self.returncode is None:
            self.returncode = -15

    def kill(self):
        if self.returncode is None:
            self.returncode = -9

    def communicate(self, timeout=None):
        # As Popen's: a bounded read of a command still running times out.
        if timeout is not None and self.returncode is None and self._last_poll is None:
            raise cre.subprocess.TimeoutExpired("fake", timeout)
        if self.returncode is None:
            self.returncode = 0
        return self._output

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.fixture
def local_run(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "target.py"
    original = b"value = 1\n"
    target.write_bytes(original)
    tests = project / "tests"
    tests.mkdir()
    (tests / "test_target.py").write_text("def test_target(): pass\n", encoding="utf-8")
    database = project / "cr-session.sqlite"
    master = _FakePopen(poll_returns=(0,))
    launches = []

    def auxiliary(args, **kwargs):
        if "init" in args:
            _build_cr_session_db(database)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def launch(*args, **kwargs):
        launches.append(args)
        target.write_bytes(b"value = 2\n")
        return master

    monkeypatch.setattr(cre._process_tree, "run", auxiliary)
    monkeypatch.setattr(cre._process_tree, "start", launch)
    engine = _engine(project, {"cosmic_ray_session_dir": str(project)})
    return SimpleNamespace(project=project, target=target, original=original, tests=tests,
                           database=database, engine=engine, master=master, launches=launches)


@pytest.mark.parametrize("kind", [OSError, RuntimeError, KeyboardInterrupt])
def test_preserved_primary_cannot_authorize_restore_or_reuse(monkeypatch, local_run, kind):
    case = local_run
    original_error = kind("mutation interrupted")
    confirmed = False
    real_finish = cre._process_tree.finish

    def interrupted(*args, **kwargs):
        raise original_error

    def finish(command, *, primary=None):
        if not confirmed:
            if primary is not None:
                primary._langadapter_command = command
            return False
        return real_finish(command, primary=primary)

    monkeypatch.setattr(cre._process_tree, "drain_output", interrupted)
    monkeypatch.setattr(cre._process_tree, "finish", finish)
    monkeypatch.setattr(case.engine, "_raise_on_instrument_fault",
                        lambda *a, **k: pytest.fail("must not check the session's results"))
    with pytest.raises(kind) as caught:
        _run(case.engine, target=case.target, project_root=case.project, test_dir=case.tests)
    assert caught.value is original_error
    assert case.target.read_bytes() == b"value = 2\n"
    owner = original_error._langadapter_source_backup
    owner.verify()
    assert (owner.root / "original.bin").read_bytes() == case.original
    session = case.engine._retained_session
    assert session._target_commands == [case.master]
    with pytest.raises(RuntimeError, match="retained handle"):
        with case.engine.session(project_root=case.project, workers=1):
            pytest.fail("must not reopen while writers are uncertain")
    confirmed = True
    session.close()
    assert case.target.read_bytes() == case.original
    assert case.engine._retained_session is None
    assert case.database.is_file()
    if kind is KeyboardInterrupt:
        # After an interrupt the restored source's backup goes, a failure
        # keeps it as evidence.
        assert not owner.root.exists() and not owner.receipt.exists()
    else:
        owner.verify()
        owner.remove()


def test_successful_local_run_removes_original_backup(local_run, tmp_path):
    case = local_run
    result = _run(case.engine, target=case.target, project_root=case.project, test_dir=case.tests)
    assert result is False and case.target.read_bytes() == case.original
    assert not list(tmp_path.glob("langadapter-mutation-source-*"))
    assert not list(tmp_path.glob("langadapter-ownership-*"))


def test_failed_run_keeps_default_database_with_explicit_error_reference(monkeypatch, local_run, tmp_path):
    case = local_run
    directory = tmp_path / "session"
    directory.mkdir()
    case.engine.config.pop("cosmic_ray_session_dir")
    case.engine._default_session_dir = directory
    failure = OSError("mutation output failed")

    def auxiliary(args, **kwargs):
        if "init" in args:
            _build_cr_session_db(directory / "cr-session.sqlite")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def refuse(*args, **kwargs):
        raise failure

    monkeypatch.setattr(cre._process_tree, "run", auxiliary)
    monkeypatch.setattr(cre._process_tree, "drain_output", refuse)
    with pytest.raises(OSError) as caught:
        _run(case.engine, target=case.target, project_root=case.project, test_dir=case.tests)
    assert caught.value is failure
    assert case.target.read_bytes() == case.original
    assert failure._langadapter_mutation_artifacts == directory
    assert (directory / "cr-session.sqlite").is_file()
    assert case.engine._default_session_dir is None
    assert case.engine._retained_session is None
    failure._langadapter_source_backup.verify()
    failure._langadapter_source_backup.remove()


def test_an_interrupted_run_leaves_neither_database_nor_backup(monkeypatch, local_run, tmp_path):
    """An interrupt is no failure: once cleanup confirms disposal and
    restores the source, the session directory, the source backup and its
    ownership receipt go, as after a completed run."""
    case = local_run
    directory = tmp_path / "session"
    directory.mkdir()
    case.engine.config.pop("cosmic_ray_session_dir")
    case.engine._default_session_dir = directory
    stop = KeyboardInterrupt()

    def auxiliary(args, **kwargs):
        if "init" in args:
            _build_cr_session_db(directory / "cr-session.sqlite")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def interrupted(*args, **kwargs):
        raise stop

    monkeypatch.setattr(cre._process_tree, "run", auxiliary)
    monkeypatch.setattr(cre._process_tree, "drain_output", interrupted)
    with pytest.raises(KeyboardInterrupt) as caught:
        _run(case.engine, target=case.target, project_root=case.project, test_dir=case.tests)
    assert caught.value is stop
    assert case.target.read_bytes() == case.original
    assert not directory.exists() and not hasattr(stop, "_langadapter_mutation_artifacts")
    assert case.engine._retained_session is None
    assert not list(tmp_path.glob("langadapter-mutation-source-*"))
    assert not list(tmp_path.glob("langadapter-ownership-*"))


@pytest.mark.parametrize("close_route", ["explicit", "context_exit"])
def test_recovered_old_session_cannot_restore_again_or_detach_new_database(
    monkeypatch, local_run, tmp_path, close_route,
):
    case = local_run
    failure = OSError("native output failed")
    confirmed = [False]
    directory = tmp_path / "failed-session"
    directory.mkdir()
    case.engine.config.pop("cosmic_ray_session_dir")
    case.engine._default_session_dir = directory

    def initialize(args, **kwargs):
        if "init" in args:
            _build_cr_session_db(directory / "cr-session.sqlite")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fail_output(*args, **kwargs):
        raise failure

    def dispose():
        if not confirmed[0]:
            raise OSError("owned writer is still unresolved")

    monkeypatch.setattr(cre._process_tree, "run", initialize)
    monkeypatch.setattr(cre._process_tree, "drain_output", fail_output)
    case.master._langadapter_command_job.close = dispose
    with pytest.raises(OSError) as caught:
        _run(case.engine, target=case.target, project_root=case.project, test_dir=case.tests)
    assert caught.value is failure
    session = failure._langadapter_mutation_session
    from langadapter.mutation import MutationCleanupError

    with pytest.raises(MutationCleanupError):
        session.close()
    assert case.engine._retained_session is session
    confirmed[0] = True
    session.close()
    assert case.target.read_bytes() == case.original
    assert case.engine._retained_session is None
    assert failure._langadapter_mutation_artifacts == directory
    failure._langadapter_source_backup.verify()
    newer = tmp_path / "new-session"
    newer.mkdir()
    (newer / "cr-session.sqlite").write_bytes(b"new session database")
    case.engine._default_session_dir = newer
    with case.engine.session(project_root=case.project, workers=1) as current:
        case.target.write_bytes(b"value = 99\n")
        if close_route == "explicit":
            session.close()
        else:
            session.__exit__(None, None, None)
        assert case.target.read_bytes() == b"value = 99\n"
        assert case.engine._default_session_dir == newer and current._opened
        assert (newer / "cr-session.sqlite").read_bytes() == b"new session database"
        assert failure._langadapter_mutation_artifacts == directory
        assert session._source_backup is None
    assert not newer.exists() and (directory / "cr-session.sqlite").is_file()
    with pytest.raises(RuntimeError, match="closed mutation session"):
        session.__enter__()
    failure._langadapter_source_backup.remove()


@pytest.mark.parametrize("kind", [cre.subprocess.TimeoutExpired, FileNotFoundError])
def test_init_keeps_uncertain_writer_until_same_owner_recovers(
    monkeypatch, local_run, kind,
):
    case = local_run
    failure = kind("uncertain writer", 1) if kind is cre.subprocess.TimeoutExpired else kind("uncertain writer")
    owner = _FakePopen(poll_returns=(0,))
    owner._langadapter_command_disposed = False
    failure._langadapter_command = owner
    confirmed = [False]
    case.engine.config.pop("cosmic_ray_session_dir")
    monkeypatch.setattr(case.engine, "_raise_on_instrument_fault",
                        lambda *a, **k: pytest.fail("must not check the session's results"))

    def initialize(args, **kwargs):
        if "init" in args:
            _build_cr_session_db(Path(args[-1]))
            raise failure
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def dispose():
        if not confirmed[0]:
            raise OSError("writer disposal refused")

    owner._langadapter_command_job.close = dispose
    monkeypatch.setattr(cre._process_tree, "run", initialize)
    with pytest.raises(kind) as caught:
        _run(case.engine, target=case.target, project_root=case.project, test_dir=case.tests,
             operators=["core/ReplaceAndWithOr"])
    assert caught.value is failure and not case.launches
    session = failure._langadapter_mutation_session
    assert case.engine._retained_session is session and session._target_commands == [owner]
    database_dir = case.engine._default_session_dir
    assert session._session_dir == database_dir
    assert (database_dir / "cr-session.sqlite").is_file() and session._source_backup is None
    with pytest.raises(RuntimeError, match="retained handle"):
        with case.engine.session(project_root=case.project, workers=1):
            pytest.fail("must not reuse pending database")
    from langadapter.mutation import MutationCleanupError

    with pytest.raises(MutationCleanupError):
        session.close()
    assert (database_dir / "cr-session.sqlite").is_file() and session._target_commands == [owner]
    confirmed[0] = True
    session.close()
    assert owner._langadapter_command_disposed is True
    assert case.engine._default_session_dir is None and case.engine._retained_session is None
    assert failure._langadapter_mutation_artifacts == database_dir
    assert (database_dir / "cr-session.sqlite").is_file() and not case.launches
    with case.engine.session(project_root=case.project, workers=1):
        session.close()
    assert (database_dir / "cr-session.sqlite").is_file()


@pytest.mark.parametrize("stage", ["init", "filter"])
@pytest.mark.parametrize("has_owner", [False, True])
def test_confirmed_stop_and_missing_before_launch_keep_existing_failure_policy(
    monkeypatch, local_run, stage, has_owner,
):
    case = local_run
    failure = cre.subprocess.TimeoutExpired("confirmed stopped writer", 1) if has_owner else FileNotFoundError("no writer")
    if has_owner:
        failure._langadapter_command = SimpleNamespace(_langadapter_command_disposed=True)

    def initialize_or_filter(args, **kwargs):
        if "init" in args:
            _build_cr_session_db(case.database)
        if ("init" in args) == (stage == "init"):
            raise failure
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cre._process_tree, "run", initialize_or_filter)
    if stage == "init":
        result = _run(case.engine, target=case.target, project_root=case.project,
                      test_dir=case.tests, operators=["core/ReplaceAndWithOr"])
        assert result is False and not case.launches
    else:
        # A failed operator filter stops the run before any mutant runs
        # rather than measuring the whole catalogue as the subset.
        with pytest.raises(RuntimeError, match="the operator filter failed"):
            _run(case.engine, target=case.target, project_root=case.project,
                 test_dir=case.tests, operators=["core/ReplaceAndWithOr"])
        assert not case.launches
    assert getattr(case.engine, "_retained_session", None) is None


def test_unreadable_original_refuses_native_exec(monkeypatch, local_run):
    case = local_run
    read = Path.read_bytes
    failure = OSError("original source cannot be saved")

    def read_source(path):
        if path == case.target:
            raise failure
        return read(path)

    monkeypatch.setattr(Path, "read_bytes", read_source)
    with pytest.raises(OSError) as caught:
        _run(case.engine, target=case.target, project_root=case.project, test_dir=case.tests)
    assert caught.value is failure and not case.launches


def test_restoration_failure_refuses_score_and_retains_backup(monkeypatch, local_run):
    case = local_run
    write = Path.write_bytes
    failure = OSError("restoration refused")
    writable = False

    def write_source(path, data):
        if path == case.target and data == case.original and not writable:
            raise failure
        return write(path, data)

    monkeypatch.setattr(Path, "write_bytes", write_source)
    monkeypatch.setattr(case.engine, "_raise_on_instrument_fault",
                        lambda *a, **k: pytest.fail("must not check the session's results"))
    with pytest.raises(OSError) as caught:
        _run(case.engine, target=case.target, project_root=case.project, test_dir=case.tests)
    assert caught.value is failure and case.target.read_bytes() == b"value = 2\n"
    owner = failure._langadapter_source_backup
    owner.verify()
    session = case.engine._retained_session
    assert not session._target_commands
    writable = True
    session.close()
    assert case.target.read_bytes() == case.original
    owner.verify()
    owner.remove()


@pytest.mark.parametrize("alias", ["hardlink", "parent"])
def test_restore_refuses_replaced_source_alias(monkeypatch, local_run, alias):
    case = local_run
    launch = cre._process_tree.start
    lstat, resolve = Path.lstat, Path.resolve

    def aliased_stat(path):
        identity = lstat(path)
        if path == case.target:
            return SimpleNamespace(st_mode=identity.st_mode, st_nlink=2)
        return identity

    def aliased_resolve(path, **kwargs):
        if path == case.target:
            return case.project.parent / "foreign.py"
        return resolve(path, **kwargs)

    def replace(*args, **kwargs):
        command = launch(*args, **kwargs)
        monkeypatch.setattr(Path, "lstat" if alias == "hardlink" else "resolve",
                            aliased_stat if alias == "hardlink" else aliased_resolve)
        return command

    monkeypatch.setattr(cre._process_tree, "start", replace)
    monkeypatch.setattr(case.engine, "_raise_on_instrument_fault",
                        lambda *a, **k: pytest.fail("must not check the session's results"))
    with pytest.raises(RuntimeError, match="unaliased") as caught:
        _run(case.engine, target=case.target, project_root=case.project, test_dir=case.tests)
    assert case.target.read_bytes() == b"value = 2\n"
    owner = caught.value._langadapter_source_backup
    owner.verify()
    assert (owner.root / "original.bin").read_bytes() == case.original
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(Path, "resolve", resolve)
    case.engine._retained_session.close()
    owner.remove()


def test_backup_refuses_temporary_storage_inside_project(monkeypatch, local_run):
    from langadapter.python import _mutation as _source_backup

    case = local_run
    monkeypatch.setattr(_source_backup.tempfile, "gettempdir", lambda: str(case.project))
    with pytest.raises(RuntimeError, match="outside the project"):
        _run(case.engine, target=case.target, project_root=case.project, test_dir=case.tests)
    assert not case.launches and case.target.read_bytes() == case.original


def test_partially_admitted_missing_command_keeps_cleanup_owner(monkeypatch, local_run):
    case = local_run
    failure = FileNotFoundError("admission interrupted after creation")
    failure._langadapter_command = case.master

    def launch(*args, **kwargs):
        case.target.write_bytes(b"value = 2\n")
        raise failure

    def refused(command, **kwargs):
        raise OSError("owned command remains uncertain")

    monkeypatch.setattr(cre._process_tree, "start", launch)
    monkeypatch.setattr(cre._process_tree, "finish", refused)
    with pytest.raises(FileNotFoundError) as caught:
        _run(case.engine, target=case.target, project_root=case.project, test_dir=case.tests)
    assert caught.value is failure and case.target.read_bytes() == b"value = 2\n"
    owner = failure._langadapter_source_backup
    owner.verify()
    assert case.engine._retained_session._target_commands == [case.master]
    owner.remove()


@pytest.mark.parametrize("stage", ["verification", "adjudication"])
@pytest.mark.parametrize("disposed", [False, True])
def test_auxiliary_timeout_restores_only_after_confirmed_disposal(
        monkeypatch, local_run, stage, disposed):
    case = local_run
    auxiliary = cre._process_tree.run
    failure = cre.subprocess.TimeoutExpired("auxiliary exec", 60, output="partial")
    command = _FakePopen(poll_returns=(0,))
    command._langadapter_command_disposed = disposed
    failure._langadapter_command = command

    if not disposed:
        def refuse():
            raise OSError("auxiliary writer is still uncertain")
        command._langadapter_command_job.close = refuse

    def execute(args, **kwargs):
        if "exec" in args:
            case.target.write_bytes(b"value = 3\n")
            raise failure
        return auxiliary(args, **kwargs)

    def verify(**kwargs):
        if stage == "verification":
            kwargs["exec_verify"](case.database, 1)
        else:
            case.engine._last_unresolved_job_ids = {"j0"}

    def adjudicate(**kwargs):
        kwargs["exec_adjudicate"](case.database, 1, case.tests / "test_target.py")

    monkeypatch.setattr(cre._process_tree, "run", execute)
    monkeypatch.setattr(case.engine, "_verify_unattributed_kills", verify)
    monkeypatch.setattr(case.engine, "_adjudication_enabled", lambda: True)
    monkeypatch.setattr(case.engine, "_adjudicate_unresolved_per_file", adjudicate)
    if disposed:
        result = _run(case.engine, target=case.target, project_root=case.project, test_dir=case.tests)
        assert result is False and case.target.read_bytes() == case.original
    else:
        monkeypatch.setattr(case.engine, "_raise_on_instrument_fault",
                        lambda *a, **k: pytest.fail("must not check the session's results"))
        with pytest.raises(cre.subprocess.TimeoutExpired) as caught:
            _run(case.engine, target=case.target, project_root=case.project, test_dir=case.tests)
        assert caught.value is failure and failure.output == "partial"
        assert case.target.read_bytes() == b"value = 3\n"
        owner = failure._langadapter_source_backup
        owner.verify()
        assert (owner.root / "original.bin").read_bytes() == case.original
        owner.remove()


def test_master_cleanup_failure_retains_original_without_restoring_or_reading_results(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "target.py"
    original = b"value = 1\n"
    target.write_bytes(original)
    tests = project / "tests"
    tests.mkdir()
    session_path = project / "cr-session.sqlite"

    def auxiliary(args, **kwargs):
        if "init" in args:
            _build_cr_session_db(session_path)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def launch(*args, **kwargs):
        target.write_bytes(b"value = 2\n")
        return _FakePopen(poll_returns=(0,))

    def refused(proc, **kwargs):
        raise OSError("master containment close refused")

    monkeypatch.setattr(cre._process_tree, "run", auxiliary)
    monkeypatch.setattr(cre._process_tree, "start", launch)
    monkeypatch.setattr(cre._process_tree, "finish", refused)
    engine = _engine(project, {"cosmic_ray_session_dir": str(project)})
    monkeypatch.setattr(engine, "_raise_on_instrument_fault", lambda *a, **k: pytest.fail(
        "uncertain cleanup must not read or report a completed measurement"))
    with pytest.raises(OSError, match="master containment") as caught:
        _run(engine, target=target, project_root=project, test_dir=tests, workers=1)
    backup = caught.value._langadapter_source_backup
    backup.verify()
    assert (backup.root / "original.bin").read_bytes() == original
    assert target.read_bytes() == b"value = 2\n"
    assert session_path.is_file()
    assert engine._retained_session._target_commands
    backup.remove()


@pytest.mark.parametrize("mode", ["released", "held", "stop_failure"])
def test_master_exit_with_inherited_output_is_drained_before_reporting(
        monkeypatch, tmp_path, mode):
    project = tmp_path / "project"
    project.mkdir()
    target = project / "target.py"
    original = b"value = 1\n"
    target.write_bytes(original)
    tests = project / "tests"
    tests.mkdir()
    session_path = project / "cr-session.sqlite"
    calls = []
    first = cre.subprocess.TimeoutExpired("master output", 10,
                                         output="partial", stderr="reason")
    cleanup = OSError("containment did not stop output writer")

    class Master(_FakePopen):
        def communicate(self, timeout=None):
            assert timeout == cre._process_tree._OUTPUT_DRAIN_SECONDS, (
                "output EOF must never be awaited without a bound")
            calls.append("drain")
            if calls.count("drain") == 1 or mode == "held":
                raise first
            assert "stop" in calls
            return "native output", "native diagnostic"

    master = Master(poll_returns=(7,))

    def auxiliary(args, **kwargs):
        if "init" in args:
            _build_cr_session_db(session_path)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def launch(*args, **kwargs):
        target.write_bytes(b"value = 2\n")
        return master

    def stop(proc, **kwargs):
        assert proc is master and proc.returncode == 7
        calls.append("stop")
        if mode == "stop_failure":
            raise cleanup

    monkeypatch.setattr(cre._process_tree, "run", auxiliary)
    monkeypatch.setattr(cre._process_tree, "start", launch)
    monkeypatch.setattr(cre._process_tree, "stop", stop)
    engine = _engine(project, {"cosmic_ray_session_dir": str(project)})
    checked = []
    check = engine._raise_on_instrument_fault

    def observe_check(*args, **kwargs):
        assert calls == ["drain", "stop", "drain"]
        assert target.read_bytes() == original
        checked.append(True)
        return check(*args, **kwargs)

    monkeypatch.setattr(engine, "_raise_on_instrument_fault", observe_check)
    if mode == "released":
        result = _run(engine, target=target, project_root=project, test_dir=tests, workers=1)
        assert result is False and checked
    else:
        with pytest.raises(cre.subprocess.TimeoutExpired) as caught:
            _run(engine, target=target, project_root=project, test_dir=tests, workers=1)
        assert caught.value is first and first.output == "partial" and first.stderr == "reason"
        assert first._langadapter_command is master
        if mode == "stop_failure" and sys.version_info >= (3, 11):
            assert f"Command cleanup failed: {cleanup}" in first.__notes__
        assert not checked
    assert master.returncode == 7 and session_path.is_file()
    if mode == "released":
        assert target.read_bytes() == original
    else:
        backup = first._langadapter_source_backup
        backup.verify()
        assert (backup.root / "original.bin").read_bytes() == original
        assert target.read_bytes() == b"value = 2\n"
        backup.remove()


def test_run_returns_truncated_on_wall_clock_timeout(monkeypatch, tmp_path):
    """An exec past ``wall_clock_timeout`` gives truncated results with the
    partial counts the session database holds. A ``time.monotonic`` past
    the cap times it out on the first check, with no real sleep."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    target = project_root / "foo.py"
    target.write_text("", encoding="utf-8")
    test_dir = project_root / "tests"
    test_dir.mkdir()
    session_path = project_root / "cr-session.sqlite"

    def fake_run(args, **kw):
        argv = list(args)
        if "init" in argv:
            _build_cr_session_db(
                session_path, catalogue=10,
                outcomes={"KILLED": 2},
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cre.subprocess, "run", fake_run)
    # The exec never finishes.
    monkeypatch.setattr(
        cre._process_tree, "start",
        lambda *a, **kw: _FakePopen(poll_returns=(None,) * 10),
    )
    monotonic_values = iter([0.0] + [100.0] * 10)
    monkeypatch.setattr(
        cre.time, "monotonic", lambda: next(monotonic_values),
    )
    monkeypatch.setattr(cre.time, "sleep", lambda *_a, **_k: None)
    # The exec stops with its process tree, including the test run.
    stopped = []

    def stop(proc, grace=10.0):
        stopped.append(proc)
        proc.terminate()

    monkeypatch.setattr(cre._process_tree, "stop", stop)

    engine = _engine(
        project_root,
        {"log_fn": None,
         "cosmic_ray_session_dir": str(project_root)},
    )
    out = _run(engine,
        target=target, project_root=project_root,
        test_dir=test_dir, workers=1, wall_clock_timeout=10,
    )
    assert out is True
    assert _killed(session_path) == 2
    assert len(stopped) == 1


def test_run_ends_when_exec_binary_missing(monkeypatch, tmp_path):
    """init succeeds but the exec subprocess raises FileNotFoundError: the
    run ends, not truncated. ``subprocess.Popen`` runs the exec phase, so
    the error is raised from its constructor.
    """
    project_root = tmp_path / "project"
    project_root.mkdir()
    target = project_root / "foo.py"
    target.write_text("", encoding="utf-8")
    test_dir = project_root / "tests"
    test_dir.mkdir()
    session_path = project_root / "cr-session.sqlite"

    def fake_run(args, **kw):
        argv = list(args)
        if "init" in argv:
            _build_cr_session_db(session_path, catalogue=5)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_popen(*args, **kwargs):
        raise FileNotFoundError("cosmic-ray missing mid-run")

    monkeypatch.setattr(cre.subprocess, "run", fake_run)
    monkeypatch.setattr(cre._process_tree, "start", fake_popen)
    engine = _engine(tmp_path, {"log_fn": None})
    out = _run(engine,
        target=target, project_root=project_root,
        test_dir=test_dir, workers=1,
    )
    assert out is False


def test_run_uses_cache_when_hit_and_skips_init(monkeypatch, tmp_path):
    """A catalogue cache for this target and operators replaces init and
    is copied into the session path."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    target = project_root / "foo.py"
    target.write_text("def f(): return 1\n", encoding="utf-8")
    test_dir = project_root / "tests"
    test_dir.mkdir()

    cache_dir = tmp_path / "artifacts" / "cache" / "mutation" / "cosmic_ray"
    cache_dir.mkdir(parents=True)

    # The engine names the cache file (its name holds a hash).
    engine = _engine(project_root, {
        "log_fn": None,
        "cosmic_ray_catalogue_cache_dir": str(cache_dir),
    })
    cache_version, _catalogue, cache_python = engine._cosmic_ray_metadata()
    cache_path = engine._resolve_cache_path(
        target, operators=None, module_path=engine._target_to_module_path(target, project_root))
    assert cache_path is not None
    _build_cr_session_db(
        cache_path, catalogue=8,
        outcomes={"KILLED": 4, "SURVIVED": 4},
    )

    seen_commands: list = []

    def fake_run(args, **kw):
        argv = list(args)
        # The cache key holds the Cosmic Ray and Python versions the project
        # interpreter reports. Unanswered, the probe leaves them unknown and
        # the run misses this cache.
        if any(cre._PROJECT_METADATA_PREFIX in str(a) for a in argv):
            return SimpleNamespace(
                returncode=0,
                stdout=cre._PROJECT_METADATA_PREFIX + json.dumps(
                    {"version": cache_version,
                     "operators": ["core/ReplaceAndWithOr"],
                     "python": cache_python},
                    sort_keys=True) + "\n",
                stderr="",
            )
        seen_commands.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cre.subprocess, "run", fake_run)
    monkeypatch.setattr(engine, "_executor", lambda: SimpleNamespace(run=fake_run))
    monkeypatch.setattr(
        cre._process_tree, "start",
        lambda *a, **kw: _FakePopen(poll_returns=(0,)),
    )
    monkeypatch.setattr(cre.time, "sleep", lambda *_a, **_k: None)
    with engine.session(project_root=project_root, workers=1) as session:
        session.run(target=target, test_dir=test_dir)
        # The run's session is the cached catalogue's.
        assert _killed(engine._resolve_paths()[0]) == 4
    assert all("init" not in cmd for cmd in seen_commands)


# ---------------------------------------------------------------------------
# CosmicRayEngineSession lifecycle
# ---------------------------------------------------------------------------

def test_session_enter_workers_one_skips_http_setup(monkeypatch, tmp_path):
    """One worker starts no HTTP workers."""
    engine = _engine(tmp_path, {"log_fn": None})
    called = {"http_setup": 0}

    def fake_setup(*a, **kw):
        called["http_setup"] += 1
        return [], [], []

    monkeypatch.setattr(engine, "_start_http_workers", fake_setup)
    sess = cre.CosmicRayEngineSession(engine, project_root=tmp_path, workers=1)
    with sess as s:
        assert s.use_http is False
        assert s.worker_urls == []
    assert called["http_setup"] == 0


def test_session_enter_workers_multiple_spawns_http_workers(
    monkeypatch, tmp_path,
):
    """Several workers start HTTP workers, and the session holds their
    processes, URLs and clones until exit."""
    engine = _engine(tmp_path, {"log_fn": None, "cosmic_ray_distributor": "http"})

    def fake_setup(n, *, project_root, env, log_fn=None, owner=None):
        procs = [SimpleNamespace(terminate=lambda: None, wait=lambda timeout=0: None, poll=lambda: 0,
                                 _langadapter_command_job=SimpleNamespace(close=lambda: None))
                 for _ in range(n)]
        urls = [f"http://127.0.0.1:{9000 + i}" for i in range(n)]
        clones = [tmp_path / f"clone_{i}" for i in range(n)]
        for c in clones:
            c.mkdir()
        return procs, urls, clones

    monkeypatch.setattr(engine, "_start_http_workers", fake_setup)
    monkeypatch.setattr(cre._process_tree, "stop_all", lambda procs, grace: None)
    sess = cre.CosmicRayEngineSession(engine, project_root=tmp_path, workers=4)
    with sess as s:
        assert s.use_http is True
        assert len(s.worker_urls) == 4
    assert sess._worker_procs == []


def test_a_failed_copy_of_the_suite_into_a_clone_fails_the_run(monkeypatch, tmp_path):
    """A clone the run's suite cannot be copied into would measure the
    previous suite: the run stops before the tool starts, naming the clone."""
    project_root = tmp_path / "project"
    test_dir = project_root / "tests"
    test_dir.mkdir(parents=True)
    target = project_root / "foo.py"
    target.write_text("def f(): return 1\n", encoding="utf-8")
    (test_dir / "test_foo.py").write_text("def test_f():\n    pass\n", encoding="utf-8")
    clones = [tmp_path / "c0", tmp_path / "c1"]
    for clone in clones:
        clone.mkdir()

    def fake_setup(n, *, project_root, env, log_fn=None, owner=None):
        return [], [f"http://127.0.0.1:{9000 + i}" for i in range(n)], list(clones)

    copytree = cre.shutil.copytree

    def failing(source, destination, **options):
        if Path(destination).parent == clones[1]:
            raise OSError("disk full")
        return copytree(source, destination, **options)

    monkeypatch.setattr(cre.shutil, "copytree", failing)
    monkeypatch.setattr(cre.subprocess, "run", lambda *a, **k: pytest.fail("init must not run"))
    monkeypatch.setattr(cre._process_tree, "start", lambda *a, **k: pytest.fail("exec must not run"))
    engine = _engine(project_root, {"log_fn": None, "cosmic_ray_distributor": "http",
                                    "cosmic_ray_session_dir": str(project_root)})
    monkeypatch.setattr(engine, "_start_http_workers", fake_setup)
    with engine.session(project_root=project_root, workers=2) as session:
        assert session.run(target=target, test_dir=test_dir) is False
    assert engine._last_failure == (
        cre.FAILED_WORKERS,
        f"the suite could not be copied into the worker clone {clones[1]} (OSError: disk full)")


def test_a_run_stops_before_its_first_mutant_when_a_worker_stopped(monkeypatch, tmp_path):
    """A worker that stopped since the session started would fail every
    request sent to it: the run stops before the tool starts, naming how
    the worker ended, an exit status or the end its supervisor reports."""
    project_root = tmp_path / "project"
    test_dir = project_root / "tests"
    test_dir.mkdir(parents=True)
    target = project_root / "foo.py"
    target.write_text("def f(): return 1\n", encoding="utf-8")
    (test_dir / "test_foo.py").write_text("def test_f():\n    pass\n", encoding="utf-8")

    def ended():
        raise ControlError("the supervisor reports the worker's end")

    monkeypatch.setattr(cre.subprocess, "run", lambda *a, **k: pytest.fail("init must not run"))
    monkeypatch.setattr(cre._process_tree, "start", lambda *a, **k: pytest.fail("exec must not run"))
    for workers, reason in (
            ([SimpleNamespace(poll=lambda: None), SimpleNamespace(poll=lambda: 3)],
             "1 of 2 HTTP workers stopped before this run (exit 3)"),
            ([SimpleNamespace(poll=ended), SimpleNamespace(poll=lambda: None)],
             "1 of 2 HTTP workers stopped before this run "
             "(the supervisor reports the worker's end)")):
        engine = _engine(project_root, {"log_fn": None, "cosmic_ray_distributor": "http",
                                        "cosmic_ray_session_dir": str(project_root)})
        monkeypatch.setattr(engine, "_start_http_workers", lambda n, *, project_root, env,
                            log_fn=None, owner=None, workers=workers: (
                                list(workers), [f"http://127.0.0.1:{9000 + i}" for i in range(n)],
                                []))
        monkeypatch.setattr(engine, "_cleanup_http_workers", lambda *a, **k: None)
        with engine.session(project_root=project_root, workers=2) as session:
            assert session.run(target=target, test_dir=test_dir) is False
        assert engine._last_failure == (cre.FAILED_WORKERS, reason)


def test_an_http_run_sends_its_jobs_with_the_session_secret(monkeypatch, tmp_path):
    """With the HTTP distributor the exec runs ``HTTP_EXEC_COSMIC_RAY`` with
    the session's secret, the one its workers get, in its environment."""
    project_root = tmp_path / "project"
    target = project_root / "foo.py"
    test_dir = project_root / "tests"
    test_dir.mkdir(parents=True)
    target.write_text("def f(): return 1\n", encoding="utf-8")
    session_path = project_root / "cr-session.sqlite"

    def fake_run(args, **kw):
        if "init" in args:
            _build_cr_session_db(session_path, catalogue=2, outcomes={"KILLED": 2})
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_setup(n, *, project_root, env, log_fn=None, owner=None):
        return [], [f"http://127.0.0.1:{9000 + i}" for i in range(n)], []

    started = []

    def start(command, **kwargs):
        started.append((command, kwargs["env"].get("LANGADAPTER_" + WORKER_SECRET)))
        return _FakePopen(poll_returns=(0,))

    monkeypatch.setattr(cre.subprocess, "run", fake_run)
    monkeypatch.setattr(cre._process_tree, "start", start)
    monkeypatch.setattr(cre.time, "sleep", lambda *_a, **_k: None)
    engine = _engine(project_root, {"log_fn": None, "cosmic_ray_distributor": "http",
                                    "cosmic_ray_session_dir": str(project_root)})
    monkeypatch.setattr(engine, "_start_http_workers", fake_setup)
    with engine.session(project_root=project_root, workers=2) as session:
        session.run(target=target, test_dir=test_dir)
    (command, secret), = started
    assert command[:4] == http_exec_command(engine.adapter.interpreter, "LANGADAPTER_")
    assert command[4:6] == ["exec", str(project_root / "cosmic-ray.toml")]
    assert secret == session.worker_secret


def test_session_hard_fails_on_worker_setup_failure_by_default(
    monkeypatch, tmp_path,
):
    """A failed worker setup raises from ``__enter__`` instead of falling
    back to a sequential run the time budget was not sized for."""
    engine = _engine(tmp_path, {"log_fn": None, "cosmic_ray_distributor": "http"})

    def bombed_setup(*a, **kw):
        raise RuntimeError("All workers failed")

    monkeypatch.setattr(engine, "_start_http_workers", bombed_setup)
    with pytest.raises(RuntimeError, match="All workers failed"):
        with cre.CosmicRayEngineSession(
            engine, project_root=tmp_path, workers=4,
        ):
            pass


# ---------------------------------------------------------------------------
# _cleanup_http_workers
# ---------------------------------------------------------------------------

def test_cleanup_http_workers_stops_each_tree_and_removes_clones(
        tmp_path, monkeypatch):
    """All workers stop together with their process trees, and each clone
    directory is removed."""
    stopped = []
    monkeypatch.setattr(cre._process_tree, "stop_all",
                        lambda procs, grace=10.0: stopped.append(list(procs)))
    clones = [tmp_path / f"clone_{i}" for i in range(3)]
    for c in clones:
        c.mkdir()
        (c / "file.py").write_text("x")

    procs = [SimpleNamespace(poll=lambda: 0, wait=lambda timeout: 0,
                             _langadapter_command_job=SimpleNamespace(close=lambda: None))
             for _ in range(3)]
    original_procs, original_clones = list(procs), list(clones)
    cre.CosmicRayEngine._cleanup_http_workers(procs, clones)
    assert stopped == [original_procs]
    assert not procs and not clones
    for c in original_clones:
        assert not c.exists()


def test_cleanup_http_workers_retains_clones_on_stop_failure(tmp_path, monkeypatch):
    """A failed stop is reported, and clones remain while workers may still use them."""
    class BadProc:
        def terminate(self):
            raise OSError("already dead")

        def wait(self, timeout=0):
            raise OSError("can't wait")

    clones = [tmp_path / "clone"]
    clones[0].mkdir()
    def refused(procs, grace):
        raise OSError("cannot stop the tree")

    monkeypatch.setattr(cre._process_tree, "stop_all", refused)
    with pytest.raises(OSError, match="cannot stop"):
        cre.CosmicRayEngine._cleanup_http_workers([BadProc(), BadProc()], clones)
    assert clones[0].exists()


class _EndedWorker:
    """A worker whose lease expired before cleanup: its supervisor ended it
    and confirmed that nothing it owned remains, and every wait or poll
    raises its own end."""

    def __init__(self, unfinished=True):
        self._unfinished_raised = unfinished
        self.error = ControlError("Native execution did not complete: host control lease expired")
        self.error._langadapter_linux_command = self

    def poll(self):
        raise self.error


def _fleet(monkeypatch, ended):
    healthy = [SimpleNamespace(poll=lambda: 0) for _ in range(2)]
    procs = [healthy[0], ended, healthy[1]]

    def stop_all(procs, grace=10.0):
        raise ended.error

    def stop(proc, grace=10.0):
        if proc is ended:
            raise ended.error

    monkeypatch.setattr(cre._process_tree, "stop_all", stop_all)
    monkeypatch.setattr(cre._process_tree, "stop", stop)
    monkeypatch.setattr(cre._process_tree, "finish", lambda proc, primary=None: True)
    return procs


def test_cleanup_http_workers_disposes_a_worker_its_lease_ended(tmp_path, monkeypatch):
    """A worker whose lease expired after its last request is disposed, not a
    failed cleanup: the run it served already shows its end (untested
    mutants, incomplete adjudication), and refusing the session would lose
    that finished measurement. The end is logged."""
    ended = _EndedWorker()
    procs = _fleet(monkeypatch, ended)
    clones = [tmp_path / f"clone_{i}" for i in range(3)]
    for clone in clones:
        clone.mkdir()
    logs = []
    cre.CosmicRayEngine._cleanup_http_workers(procs, clones,
                                              log_fn=lambda text, level: logs.append(text))
    assert not procs and not clones
    assert any("ended before cleanup" in text and "lease expired" in text for text in logs)


def test_cleanup_http_workers_raises_a_control_failure_that_is_no_worker_end(
        tmp_path, monkeypatch):
    """A control failure other than a worker's own end still fails the
    cleanup and keeps the clones."""
    ended = _EndedWorker(unfinished=False)
    procs = _fleet(monkeypatch, ended)
    clones = [tmp_path / "clone"]
    clones[0].mkdir()
    with pytest.raises(ControlError, match="lease expired"):
        cre.CosmicRayEngine._cleanup_http_workers(procs, clones)
    assert clones[0].exists()
