"""The Cosmic Ray engine without real ``cosmic-ray`` runs: the pooled
session, operator sets, the catalogue cache, module paths, worker clones,
session parsing and capture, session directories and the container launcher with
a recording executor."""
from __future__ import annotations

import contextlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from langadapter.python.cosmic_ray import CosmicRaySettings
import langadapter as lb
from langadapter.python import _cosmic_ray as cr_engine, _cr_verify, _project_files
from langadapter.python._runs import run_record_mark
from langadapter.python._cosmic_ray import (
    _OPERATOR_SET_SHORTCUTS,
    _resolve_project_tooling,
    _wong_mathur_selective_operators,
)
from langadapter.execution import container_work_dir_for
from langadapter.mutation import FAILED_INITIALIZATION, FAILED_SESSION, FAILED_WORKERS
from langadapter.python._cr_session_runner import EVENT_LOOP_COSMIC_RAY
from langadapter.python._run_definition import RUN_VARIABLES
from langadapter.python.adapter import PythonAdapter

def _record(text="", **record):
    """A per-mutant output: ``text`` and its run record's line."""
    return text + "\n" + run_record_mark() + json.dumps(record)


_CONTAINER_PYTHON = "/home/swe/miniconda3/envs/x/bin/python"


def _local_engine(config):
    """An engine for a local project in the calling interpreter."""
    context = lb.ProjectContext(
        root=str(Path(__file__).parent),
        interpreter=lb.Interpreter(path=sys.executable))
    return cr_engine.CosmicRayEngine(
        {**CosmicRaySettings()._engine_config("local"), **config}, adapter=PythonAdapter(context))


def _container_engine(config, work_dir, executor=None, engine_class=None):
    """An engine for a project at ``/home/swe/repo`` in a container whose
    commands go through ``executor``."""
    context = lb.ProjectContext(
        root="/home/swe/repo",
        interpreter=lb.Interpreter(path=_CONTAINER_PYTHON),
        execution=lb.ContainerExecution(container="tg_ctr",
                                        work_dir=str(work_dir)))
    adapter = PythonAdapter(context, executor=executor)
    if isinstance(executor, _FakeContainerExecutor):
        executor.work_dir = Path(work_dir)
    return (engine_class or cr_engine.CosmicRayEngine)({**CosmicRaySettings()._engine_config("local"), **config}, adapter=adapter)


class _RetryEngine(cr_engine.CosmicRayEngine):
    """The settings of a retry after an out-of-memory failure: unresolved
    kills fail the run and auxiliary passes run serially."""

    fail_on_unresolved = True
    serial_auxiliary = True


# The pooled session.

def test_cosmic_ray_session_workers_1_uses_local_distributor() -> None:
    """``workers=1`` runs Cosmic Ray's local distributor, with no HTTP
    workers."""
    engine = _local_engine({"engine": "cosmic_ray"})
    sess = engine.session(project_root=Path("."), workers=1)
    with sess as s:
        assert s.use_http is False
        assert s.workers == 1
        assert s.worker_urls == []


def test_cosmic_ray_session_workers_n_marks_http_mode() -> None:
    """``cosmic_ray_distributor=http`` with ``workers > 1`` selects HTTP mode
    (the session is not entered, so no workers are spawned)."""
    engine = _local_engine({
        "engine": "cosmic_ray", "cosmic_ray_distributor": "http"})
    sess = engine.session(project_root=Path("."), workers=4)
    assert sess.use_http is True
    assert sess.workers == 4


def test_cosmic_ray_session_run_outside_with_raises() -> None:
    """``run`` outside the session's ``with`` block raises instead of giving
    empty results."""
    engine = _local_engine({"engine": "cosmic_ray"})
    sess = engine.session(project_root=Path("."), workers=1)
    with pytest.raises(RuntimeError, match="Session.+outside.+with"):
        sess.run(target=Path("x.py"), test_dir=Path("tests"))


# Cosmic Ray operator-set shortcuts.

def test_wong_mathur_operators_count_and_shape() -> None:
    """Wong and Mathur's (1995) selective set has 42 AOR, 30 ROR and 2 LCR
    operators, 74 in all."""
    ops = _wong_mathur_selective_operators()
    assert len(ops) == 74
    # AOR: ReplaceBinaryOperator_<src>_<dst> for all pairs from 7 arith
    aor = [o for o in ops if "ReplaceBinaryOperator" in o]
    assert len(aor) == 42  # 7 * 6
    # ROR: ReplaceComparisonOperator_<src>_<dst> for all pairs from 6 comp
    ror = [o for o in ops if "ReplaceComparisonOperator" in o]
    assert len(ror) == 30  # 6 * 5
    # LCR: 2 fixed strings
    lcr = [o for o in ops if o in (
        "core/ReplaceAndWithOr", "core/ReplaceOrWithAnd",
    )]
    assert len(lcr) == 2


def test_shortcut_registry_contains_wong_mathur() -> None:
    """``wong_mathur_selective`` names the 74-operator set: a typo in the
    registry would silently give Cosmic Ray's defaults."""
    assert "wong_mathur_selective" in _OPERATOR_SET_SHORTCUTS
    fn = _OPERATOR_SET_SHORTCUTS["wong_mathur_selective"]
    assert callable(fn)
    assert len(fn()) == 74


# The catalogue cache's content-hashed path.

def test_cache_path_returns_none_when_disabled(tmp_path: Path) -> None:
    """An empty or unset ``cosmic_ray_catalogue_cache_dir`` turns the
    catalogue cache off."""
    engine = _local_engine({"cosmic_ray_catalogue_cache_dir": ""})
    target = tmp_path / "src.py"
    target.write_text("def f(): pass", encoding="utf-8")
    assert engine._resolve_cache_path(target, operators=None) is None


def test_cache_path_changes_when_source_changes(tmp_path: Path) -> None:
    """The cache key hashes the source, so an edited target never reuses a
    stale catalogue."""
    cache_dir = tmp_path / "cache"
    engine = _local_engine({
        "cosmic_ray_catalogue_cache_dir": str(cache_dir),
    })

    target = tmp_path / "src.py"
    target.write_text("def f(): return 1", encoding="utf-8")
    p1 = engine._resolve_cache_path(target, operators=["core/X"])

    target.write_text("def f(): return 2", encoding="utf-8")
    p2 = engine._resolve_cache_path(target, operators=["core/X"])

    assert p1 is not None and p2 is not None
    assert p1 != p2
    # Same source -> same path (idempotent).
    target.write_text("def f(): return 2", encoding="utf-8")
    p3 = engine._resolve_cache_path(target, operators=["core/X"])
    assert p2 == p3


def test_two_files_of_one_name_and_content_never_share_a_catalogue(tmp_path: Path) -> None:
    """Two files of one name and the same bytes get separate catalogues: a
    restored session mutates the module path it names, so it is in the key."""
    engine = _local_engine({
        "cosmic_ray_catalogue_cache_dir": str(tmp_path / "cache"),
    })
    files = [tmp_path / "pkg" / "retry.py", tmp_path / "pkg" / "asyncio" / "retry.py"]
    for target in files:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def f(): return 1", encoding="utf-8")
    first, second = (engine._resolve_cache_path(
        target, operators=["core/X"], module_path=engine._target_to_module_path(target, tmp_path))
        for target in files)
    assert first is not None and second is not None and first != second
    assert first == engine._resolve_cache_path(files[0], operators=["core/X"],
                                               module_path="pkg/retry.py")


def test_target_to_module_path_relative(tmp_path: Path) -> None:
    """The target's module path is relative to the project root, with
    forward slashes, as Cosmic Ray's command line takes it."""
    engine = _local_engine({})
    target = tmp_path / "pkg" / "sub" / "mod.py"
    target.parent.mkdir(parents=True)
    target.write_text("# stub")
    assert engine._target_to_module_path(target, tmp_path) == "pkg/sub/mod.py"


# _resolve_project_tooling: the interpreter and cosmic-ray binary a run uses.

def test_resolve_project_tooling_uses_project_python_and_sibling_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mutants' test interpreter and the cosmic-ray binary come from the
    project interpreter's environment, even when its bin or Scripts directory
    is not on PATH."""
    is_win = os.name == "nt"
    bindir = tmp_path / "venv" / ("Scripts" if is_win else "bin")
    bindir.mkdir(parents=True)
    py = bindir / ("python.exe" if is_win else "python")
    py.write_text("")
    cr = bindir / ("cosmic-ray.exe" if is_win else "cosmic-ray")
    cr.write_text("")
    cr.chmod(0o755)
    executable, cosmic_ray_exe = _resolve_project_tooling(str(py))

    assert executable == str(py).replace(os.sep, "/")
    assert Path(cosmic_ray_exe) == cr


def test_resolve_project_tooling_falls_back_to_path_when_no_sibling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a cosmic-ray next to the interpreter, the bare name leaves the
    lookup to PATH."""
    py = tmp_path / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("")
    _, cosmic_ray_exe = _resolve_project_tooling(str(py))

    assert cosmic_ray_exe == "cosmic-ray"


# Worker clone location and the stale-clone sweep.

def test_clone_base_dir_auto_prefers_node_local_scratch(
    tmp_path, monkeypatch,
) -> None:
    """``auto`` or empty picks node-local scratch, /dev/shm before the system
    temporary directory, each needing write access and 1 GiB free, and never the
    launch directory's possibly shared file system while such scratch exists."""
    shm = tmp_path / "shm"
    tmp = tmp_path / "systmp"
    shm.mkdir()
    tmp.mkdir()
    import langadapter.python._cosmic_ray as moved_engine
    monkeypatch.setattr(moved_engine, "_node_local_scratch_candidates",
                        lambda: (shm, tmp))
    # Pin the free-space probe: the test ranks candidates, and the real
    # drive's free space dips under concurrent runs.
    monkeypatch.setattr(cr_engine.shutil, "disk_usage",
                        lambda p: type("D", (), {"free": 2 << 30})())
    target = tmp_path / "checkout"
    target.mkdir()
    out = cr_engine._clone_base_dir({"cosmic_ray_clone_dir": "auto"}, target)
    assert out == shm / "cr_worker_clones"
    assert out.is_dir()
    # /dev/shm without headroom falls through to the system temp.
    monkeypatch.setattr(cr_engine.shutil, "disk_usage",
                        lambda p: type("D", (), {"free": 0})()
                        if str(p) == str(shm)
                        else type("D", (), {"free": 2 << 30})())
    out = cr_engine._clone_base_dir({"cosmic_ray_clone_dir": ""}, target)
    assert out == tmp / "cr_worker_clones"


def test_clone_base_dir_last_resort_never_inside_project_root(
    tmp_path, monkeypatch,
) -> None:
    """Without node-local scratch the clones go beside the launch directory,
    never inside project_root (they would copy themselves)."""
    monkeypatch.setattr(cr_engine.shutil, "disk_usage",
                        lambda p: type("D", (), {"free": 0})())
    proj = tmp_path / "project"
    proj.mkdir()
    monkeypatch.chdir(proj)
    out = cr_engine._clone_base_dir({"cosmic_ray_clone_dir": ""}, proj)
    assert out == proj.parent / "cr_worker_clones"
    assert proj not in out.resolve().parents


def test_clone_base_dir_explicit_path_overrides(tmp_path: Path) -> None:
    """An explicit clone directory wins over the default and is created."""
    target = tmp_path / "scratch" / "cr_clones"
    out = cr_engine._clone_base_dir({"cosmic_ray_clone_dir": str(target)},
                                    tmp_path / "project")
    assert out == target and target.is_dir()


def test_sweep_stale_clones_removes_old_keeps_recent_and_foreign(
    tmp_path: Path,
) -> None:
    """The sweep removes this engine's marked ``cr_worker_*`` clones older
    than the age bound and keeps recent clones, unmarked directories, session
    directories and other names."""
    import time as _time
    old = cr_engine._mark_owned(tmp_path / "cr_worker_3_dead")
    old.mkdir()
    (old / "payload").write_text("x", encoding="utf-8")
    recent = cr_engine._mark_owned(tmp_path / "cr_worker_4_live")
    recent.mkdir()
    unmarked = tmp_path / "cr_worker_5_someone_elses"
    unmarked.mkdir()
    session = cr_engine._mark_owned(tmp_path / "cr_session_retained")
    session.mkdir()
    foreign = tmp_path / "pytest-of-someone"
    foreign.mkdir()
    past = _time.time() - 24 * 3600
    for directory in (old, unmarked, session):
        os.utime(directory, (past, past))

    removed = cr_engine._sweep_stale_clones(tmp_path, max_age_hours=12)

    assert removed == 1
    assert not old.exists()        # old marked clone reclaimed
    assert not cr_engine._owner_marker(old).exists()
    assert recent.exists()         # too new -> a live run's clone, kept
    assert unmarked.exists()       # not this engine's -> never touched
    assert session.exists()        # session databases are retained evidence
    assert foreign.exists()        # wrong prefix -> never touched


def test_clone_cleanup_removes_markers_and_unmarks_a_clone_it_keeps(
    tmp_path: Path, monkeypatch,
) -> None:
    removed = cr_engine._mark_owned(tmp_path / "cr_worker_0_done")
    removed.mkdir()
    kept = cr_engine._mark_owned(tmp_path / "cr_worker_1_held")
    kept.mkdir()
    rmtree = cr_engine.shutil.rmtree

    def partial(path, *args, **kwargs):
        if Path(path) == kept:
            raise OSError("held by another process")
        rmtree(path, *args, **kwargs)

    monkeypatch.setattr(cr_engine.shutil, "rmtree", partial)
    monkeypatch.setattr(cr_engine.time, "sleep", lambda seconds: None)
    clones = [removed, kept]
    with pytest.raises(OSError, match="could not remove 1 worker clone"):
        cr_engine.CosmicRayEngine._cleanup_http_workers([], clones)
    assert not removed.exists() and not cr_engine._owner_marker(removed).exists()
    # The kept clone is evidence: it stays for a retry and is never swept.
    assert kept.exists() and clones == [kept]
    assert not cr_engine._owner_marker(kept).exists()


def test_sweep_stale_clones_disabled_when_hours_non_positive(
    tmp_path: Path,
) -> None:
    """``max_age_hours <= 0`` disables the sweep entirely."""
    import time as _time
    old = tmp_path / "cr_worker_0_dead"
    old.mkdir()
    past = _time.time() - 100 * 3600
    os.utime(old, (past, past))
    assert cr_engine._sweep_stale_clones(tmp_path, max_age_hours=0) == 0
    assert old.exists()


# _write_config_toml: the cosmic-ray.toml handed to Cosmic Ray's CLI.

def _cosmic_ray_settings(text: str) -> dict:
    """The ``[cosmic-ray]`` table of a written config."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    return tomllib.loads(text)["cosmic-ray"]


def test_write_config_toml_local_distributor(tmp_path: Path) -> None:
    """Without worker_urls the settings name the local distributor and keep
    the test command, module path and per-mutant timeout, a quote and a
    backslash included."""
    engine = _local_engine({})
    cfg = tmp_path / "cr.toml"
    engine._write_config_toml(
        cfg,
        module_path="src/o'brien.py",
        test_command="C:\\py\\python.exe -m pytest -x tests",
        per_mutant_timeout=42,
    )
    settings = _cosmic_ray_settings(cfg.read_text(encoding="utf-8"))
    assert settings["module-path"] == "src/o'brien.py"
    assert settings["timeout"] == 42
    assert settings["test-command"] == "C:\\py\\python.exe -m pytest -x tests"
    assert settings["distributor"] == {"name": "local"}


def test_write_config_toml_http_distributor_with_workers(tmp_path: Path) -> None:
    """With worker_urls the settings name the http distributor and list each
    URL."""
    engine = _local_engine({})
    cfg = tmp_path / "cr.toml"
    engine._write_config_toml(
        cfg,
        module_path="src/foo.py",
        test_command="pytest tests",
        per_mutant_timeout=30,
        worker_urls=["http://127.0.0.1:8001", "http://127.0.0.1:8002"],
    )
    settings = _cosmic_ray_settings(cfg.read_text(encoding="utf-8"))
    assert settings["distributor"] == {"name": "http", "http": {
        "worker-urls": ["http://127.0.0.1:8001", "http://127.0.0.1:8002"]}}


def test_write_config_toml_emits_operator_filter_when_provided(
    tmp_path: Path,
) -> None:
    """Cosmic Ray has no allow-list at init time, so a restricted
    ``operators`` list becomes an operators-filter that excludes the rest of
    the catalogue and not the allowed names."""
    pytest.importorskip("cosmic_ray")
    engine = _local_engine({})
    cfg = tmp_path / "cr.toml"
    engine._write_config_toml(
        cfg,
        module_path="src/foo.py",
        test_command="pytest tests",
        per_mutant_timeout=30,
        operators=["core/ReplaceAndWithOr", "core/ReplaceOrWithAnd"],
    )
    settings = _cosmic_ray_settings(cfg.read_text(encoding="utf-8"))
    exclude = settings["filters"]["operators-filter"]["exclude-operators"]
    assert exclude
    assert "^core/ReplaceAndWithOr$" not in exclude
    assert "^core/ReplaceOrWithAnd$" not in exclude


# A fake session database in Cosmic Ray 8.x's schema, no real run.

def _make_fake_session_db(
    path: Path,
    *,
    catalogue_size: int,
    outcomes: dict,
    survivor_diffs: list = None,
    operators: list = None,
    outputs: dict = None,
    skipped: int = 0,
) -> None:
    """Write a minimal Cosmic Ray 8.x session.sqlite.

    ``outcomes`` maps a test_outcome to a count. ``survivor_diffs`` holds
    (job_id, start_pos_row, diff_text) for surviving mutants. ``operators``
    names ``job_i``'s operator (default ``op``). ``outputs`` maps a job_id
    to its test output. ``skipped`` adds filtered-operator rows.
    """
    import contextlib
    import sqlite3
    with contextlib.closing(sqlite3.connect(str(path))) as conn:
        # As in Cosmic Ray 8.x, work_items holds only job_id and mutation_specs
        # the operator and source position.
        conn.execute("CREATE TABLE work_items (job_id TEXT PRIMARY KEY)")
        conn.execute(
            """CREATE TABLE mutation_specs (
                job_id TEXT PRIMARY KEY,
                module_path TEXT,
                operator_name TEXT,
                operator_args TEXT,
                occurrence INTEGER,
                start_pos_row INTEGER,
                start_pos_col INTEGER,
                end_pos_row INTEGER,
                end_pos_col INTEGER,
                definition_name TEXT
            )"""
        )
        conn.execute(
            """CREATE TABLE work_results (
                job_id TEXT PRIMARY KEY,
                worker_outcome TEXT,
                test_outcome TEXT,
                diff TEXT,
                output TEXT
            )"""
        )
        for job_idx in range(catalogue_size):
            op = (
                operators[job_idx]
                if operators is not None and job_idx < len(operators)
                else "op"
            )
            jid = f"job_{job_idx}"
            conn.execute("INSERT INTO work_items (job_id) VALUES (?)", (jid,))
            conn.execute(
                "INSERT INTO mutation_specs (job_id, module_path, operator_name, "
                "occurrence, start_pos_row, start_pos_col, end_pos_row, end_pos_col) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (jid, "m", op, 0, 0, 0, 0, 0),
            )
        result_job = 0
        for outcome, count in outcomes.items():
            for _ in range(count):
                jid = f"job_{result_job}"
                conn.execute(
                    "INSERT INTO work_results VALUES (?, ?, ?, ?, ?)",
                    (jid, "NORMAL", outcome, "", (outputs or {}).get(jid, "")),
                )
                result_job += 1
        for jid, row, diff in (survivor_diffs or []):
            conn.execute(
                "UPDATE work_results SET diff = ? WHERE job_id = ?", (diff, jid),
            )
            conn.execute(
                "UPDATE mutation_specs SET start_pos_row = ? WHERE job_id = ?",
                (row, jid),
            )
        # Operators excluded by the selective filter: extra work_items with a
        # SKIPPED ("Filtered operator") result and no test_outcome.
        for s_idx in range(skipped):
            jid = f"skip_{s_idx}"
            conn.execute("INSERT INTO work_items (job_id) VALUES (?)", (jid,))
            conn.execute(
                "INSERT INTO mutation_specs (job_id, module_path, operator_name, "
                "occurrence, start_pos_row, start_pos_col, end_pos_row, end_pos_col) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (jid, "m", "core/ReplaceBinaryOperator_Add_BitOr", 0, 99, 0, 0, 0),
            )
            conn.execute(
                "INSERT INTO work_results VALUES (?, ?, ?, ?, ?)",
                (jid, "skipped", None, "", "Filtered operator"),
            )
        conn.commit()


def test_poll_session_progress_emits_breakdown(tmp_path: Path) -> None:
    """The progress poller classifies the session's results and logs one
    line per interval until the stop event fires."""
    import threading
    from langadapter.python._cr_verify import _poll_session_progress

    db = tmp_path / "session.sqlite"
    _make_fake_session_db(
        db,
        catalogue_size=20,
        outcomes={"KILLED": 11, "SURVIVED": 3, "INCOMPETENT": 1},
        outputs={"job_10": "timeout"},
    )

    logs: list[str] = []
    def _log(msg, level=0):
        logs.append(msg)

    stop = threading.Event()
    thread = threading.Thread(
        target=_poll_session_progress,
        kwargs={
            "session_path": db,
            "stop_event": stop,
            "interval_seconds": 1,
            "log_fn": _log,
            "start_perf": 0.0,
        },
        daemon=True,
    )
    thread.start()
    import time as _t
    _t.sleep(1.2)
    stop.set()
    thread.join(timeout=2.0)

    assert logs, "poller should have emitted at least one progress line"
    line = logs[0]
    # 15 of 20 done (10 killed, 3 survived, 1 timeout, 1 incompetent): the
    # 5 untested have no result row.
    assert "15/20" in line
    assert "killed 10" in line
    assert "survived 3" in line
    assert "timeout 1" in line
    assert "incompetent 1" in line


def test_poll_session_progress_reports_counts_and_survives_a_failing_callback(
        tmp_path: Path) -> None:
    """The progress callback receives the run's finished and total mutants
    each tick, without those the operator filter skipped. A callback that
    raises stops neither the poller nor its log line."""
    import threading
    import time as _t
    from langadapter.mutation import MutationProgress
    from langadapter.python._cr_verify import _poll_session_progress

    db = tmp_path / "session.sqlite"
    _make_fake_session_db(db, catalogue_size=20,
                          outcomes={"KILLED": 10, "SURVIVED": 3}, skipped=4)
    events: list = []
    logs: list[str] = []

    def failing(event):
        events.append(event)
        raise RuntimeError("display failed")

    stop = threading.Event()
    thread = threading.Thread(
        target=_poll_session_progress,
        kwargs={"session_path": db, "stop_event": stop, "interval_seconds": 1,
                "log_fn": lambda m, level=0: logs.append(m), "start_perf": 0.0,
                "progress": failing},
        daemon=True,
    )
    thread.start()
    _t.sleep(1.2)
    stop.set()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert events and isinstance(events[0], MutationProgress)
    assert (events[0].kind, events[0].finished, events[0].total) == ("mutants", 13, 20)
    assert logs  # the log line is still written


def test_poll_session_progress_counts_a_clock_kill_as_a_timeout(tmp_path: Path) -> None:
    """Cosmic Ray 8.x records a per-mutant timeout as KILLED with the output
    ``timeout``: the progress line counts it as a timeout, as the run's
    summary does, not as a kill."""
    import threading
    import time as _t
    from langadapter.python._cr_verify import _poll_session_progress

    db = tmp_path / "session.sqlite"
    _make_fake_session_db(db, catalogue_size=4, outcomes={"KILLED": 3},
                          outputs={"job_0": "timeout"})
    logs: list[str] = []
    stop = threading.Event()
    thread = threading.Thread(
        target=_poll_session_progress,
        kwargs={"session_path": db, "stop_event": stop, "interval_seconds": 1,
                "log_fn": lambda m, level=0: logs.append(m), "start_perf": 0.0},
        daemon=True,
    )
    thread.start()
    _t.sleep(1.2)
    stop.set()
    thread.join(timeout=2.0)
    assert logs and "killed 2, survived 0, timeout 1," in logs[-1], logs


def test_poll_session_progress_reports_the_final_counts_after_the_stop(tmp_path: Path) -> None:
    """Results committed after the poller's last one-second read still reach
    the progress callback: after the stop it reads once more, so a finished
    run's last record holds every result (and the log keeps its lines)."""
    import threading
    from langadapter.python._cr_verify import _poll_session_progress

    db = tmp_path / "session.sqlite"
    _make_fake_session_db(db, catalogue_size=6, outcomes={"KILLED": 4, "SURVIVED": 2})
    events: list = []
    logs: list[str] = []
    stop = threading.Event()
    stop.set()  # the run ended before the poller's next read
    _poll_session_progress(session_path=db, stop_event=stop, interval_seconds=1,
                           log_fn=lambda m, level=0: logs.append(m), start_perf=0.0,
                           progress=events.append)
    assert [(e.kind, e.finished, e.total) for e in events] == [("mutants", 6, 6)]
    assert logs == []


def test_poll_session_progress_swallows_db_errors(tmp_path: Path) -> None:
    """A poller that cannot read the session skips the tick without
    raising."""
    import threading
    from langadapter.python._cr_verify import _poll_session_progress

    missing = tmp_path / "never_exists.sqlite"
    logs: list[str] = []
    stop = threading.Event()
    thread = threading.Thread(
        target=_poll_session_progress,
        kwargs={
            "session_path": missing,
            "stop_event": stop,
            "interval_seconds": 1,
            "log_fn": lambda m, level=0: logs.append(m),
            "start_perf": 0.0,
        },
        daemon=True,
    )
    thread.start()
    import time as _t
    _t.sleep(1.2)
    stop.set()
    thread.join(timeout=2.0)
    assert not logs
    assert not thread.is_alive()


# The kill matrix of a session database, as a session's capture reads it.

def _session_matrix(engine, session_dir: Path) -> dict:
    """The kill matrix of the session database in ``session_dir``, read as
    a session's capture reads it and saved as JSON."""
    import json
    run = engine._read_mutation_run(session_dir / "cr-session.sqlite", Path("foo.py"))
    return json.loads(json.dumps(run.to_kill_matrix()))


def test_cosmic_ray_kill_matrix_translates_outcomes(
    tmp_path: Path,
) -> None:
    """Cosmic Ray's outcomes map to the shared status vocabulary, a mutant
    without a result row or with an INCOMPETENT one is ``untested``, and
    ``summary['detected']`` counts killed and timeout."""
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    _make_fake_session_db(
        session_dir / "cr-session.sqlite",
        catalogue_size=5,
        outcomes={"KILLED": 2, "SURVIVED": 1, "INCOMPETENT": 1},
    )
    engine = _local_engine({"cosmic_ray_session_dir": str(session_dir)})
    engine._full_operator_cache_loaded = True
    engine._full_operator_cache = ["op"]
    engine._remember_requested_catalogue(["op"])
    payload = _session_matrix(engine, session_dir)
    assert payload["schema_version"] == 3
    assert "captured_at" not in payload
    # detected = killed (2) + timeout (0) = 2. The INCOMPETENT mutant
    # measured nothing, so the matrix is stamped incomplete.
    assert payload["summary"] == {
        "total": 5, "detected": 2, "survived": 1,
        "timeout": 0, "non_viable": 0, "untested": 2,
        "unresolved": 0,
    }
    assert payload["incomplete"]["untested"] == 2
    statuses = sorted({m["status"] for m in payload["mutants"]})
    assert statuses == ["killed", "survived", "untested"]
    # Each mutant names its operator, so a selective score can be filtered
    # from this full-catalogue capture.
    assert all("operator" in m for m in payload["mutants"])
    # Each measured mutant has its worker_outcome, so a process-level kill
    # (crash, resource kill) is told apart from a test-failure kill.
    assert all("worker_outcome" in m for m in payload["mutants"]
               if m["status"] != "untested")
    assert payload["catalogue_provenance"] == {
        "requested_catalogue": "full",
        "requested_operators": ["op"],
        "observed_operators": ["op"],
        "observed_mutant_ids": [f"job_{index}" for index in range(5)],
    }
    assert "mutation_oom_retry" not in payload


def test_a_failed_worker_request_withholds_the_score(tmp_path: Path) -> None:
    """A result without a test outcome is untested: no test detected the
    mutant. A failed request to an HTTP worker (ABNORMAL) measured nothing,
    so it withholds the score as a mutant without any result does. A
    mutation that could not be applied (NO-TEST) singles out no slow
    mutants, so the matrix scores, counting it as not killed."""
    import contextlib
    import sqlite3
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    session = session_dir / "cr-session.sqlite"
    _make_fake_session_db(session, catalogue_size=5,
                          outcomes={"KILLED": 1, "SURVIVED": 1})
    with contextlib.closing(sqlite3.connect(str(session))) as conn:
        conn.execute("INSERT INTO work_results VALUES (?, ?, ?, ?, ?)",
                     ("job_2", "ABNORMAL", None, None, "Cannot connect to host"))
        conn.execute("INSERT INTO work_results VALUES (?, ?, ?, ?, ?)",
                     ("job_3", "NO-TEST", None, None, None))
        conn.commit()
    engine = _local_engine({"cosmic_ray_session_dir": str(session_dir)})
    engine._full_operator_cache_loaded = True
    engine._full_operator_cache = ["op"]
    engine._remember_requested_catalogue(["op"])
    payload = _session_matrix(engine, session_dir)
    # job_4 has no result, as a run the clock ended leaves it.
    assert payload["summary"]["untested"] == 3
    assert (payload["summary"]["detected"], payload["summary"]["timeout"]) == (1, 0)
    assert payload["incomplete"]["untested"] == 2
    with contextlib.closing(sqlite3.connect(str(session))) as conn:
        conn.execute("INSERT INTO work_results VALUES (?, ?, ?, ?, ?)",
                     ("job_4", "NORMAL", "SURVIVED", "", ""))
        conn.commit()
    run = engine._read_mutation_run(session, tmp_path / "foo.py")
    assert not run.complete and run.score() is None
    with contextlib.closing(sqlite3.connect(str(session))) as conn:
        conn.execute("UPDATE work_results SET worker_outcome = 'NORMAL', "
                     "test_outcome = 'SURVIVED' WHERE job_id = 'job_2'")
        conn.commit()
    run = engine._read_mutation_run(session, tmp_path / "foo.py")
    # 1 killed of 5: the mutation not applied counts as not killed.
    assert run.complete and run.score() == 20.0


def test_cosmic_ray_capture_excludes_filtered_operators(tmp_path: Path) -> None:
    """Filtered-operator rows are left out of the captured matrix, so a
    full-catalogue score derived from it is not inflated."""
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    _make_fake_session_db(
        session_dir / "cr-session.sqlite",
        catalogue_size=2,
        outcomes={"KILLED": 1, "SURVIVED": 1},
        skipped=3,
    )
    engine = _local_engine({"cosmic_ray_session_dir": str(session_dir)})
    payload = _session_matrix(engine, session_dir)
    # Only the two real mutants. The three filtered operators are gone.
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["timeout"] == 0
    assert {m["status"] for m in payload["mutants"]} == {"killed", "survived"}
    assert all(m["status"] != "skipped" for m in payload["mutants"])


def test_cosmic_ray_capture_preserves_equivalent_catalogue_rows(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    db = session_dir / "cr-session.sqlite"
    _make_fake_session_db(
        db,
        catalogue_size=2,
        outcomes={"KILLED": 2},
        outputs={
            "job_0": _record("FAILED test_x.py::test_a - AssertionError",
                             failed=["test_x.py::test_a"], errors=[]),
            "job_1": _record("FAILED test_x.py::test_a - AssertionError",
                             failed=["test_x.py::test_a"], errors=[]),
        },
    )
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "UPDATE mutation_specs SET operator_name = ?, start_pos_row = ?",
            ("core/Op", 7),
        )
        conn.execute("UPDATE work_results SET diff = ?", ("@@\n-a\n+b\n",))
        conn.commit()

    engine = _local_engine({"cosmic_ray_session_dir": str(session_dir)})
    payload = _session_matrix(engine, session_dir)
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["detected"] == 2
    assert [mutant["id"] for mutant in payload["mutants"]] == [
        "job_0", "job_1",
    ]
    assert "equivalent_duplicates" not in payload["catalogue_provenance"]
    assert all(
        "equivalent_engine_ids" not in mutant
        for mutant in payload["mutants"]
    )


def test_cosmic_ray_capture_preserves_conflicting_equivalent_rows(
    tmp_path: Path,
) -> None:
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    db = session_dir / "cr-session.sqlite"
    _make_fake_session_db(
        db,
        catalogue_size=2,
        outcomes={"KILLED": 1, "SURVIVED": 1},
    )
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        conn.execute(
            "UPDATE mutation_specs SET operator_name = ?, start_pos_row = ?",
            ("core/Op", 7),
        )
        conn.execute("UPDATE work_results SET diff = ?", ("@@\n-a\n+b\n",))
        conn.commit()
    engine = _local_engine({
        "cosmic_ray_session_dir": str(session_dir),
    })
    payload = _session_matrix(engine, session_dir)
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["detected"] == 1
    assert payload["summary"]["survived"] == 1


def test_mutation_diag_tb_default_and_override(monkeypatch) -> None:
    """Each mutant's run uses ``--tb=no`` unless LANGADAPTER_MUTATION_DIAG_TB
    names another traceback style."""
    from langadapter.python._cosmic_ray import _mutation_diag_tb
    monkeypatch.delenv("LANGADAPTER_MUTATION_DIAG_TB", raising=False)
    assert _mutation_diag_tb() == "no"
    monkeypatch.setenv("LANGADAPTER_MUTATION_DIAG_TB", "short")
    assert _mutation_diag_tb() == "short"


def test_cr_runner_log_tees_to_file(tmp_path: Path) -> None:
    """The launcher also writes its log to a host-visible file, so a run that
    hangs or dies inside the blocking exec is traceable on disk."""
    from langadapter.python import _cr_session_runner as r
    log_file = tmp_path / "cr_runner.log"
    prev = r._LOG_FH
    try:
        r._LOG_FH = open(log_file, "a", buffering=1, encoding="utf-8")
        r._log("worker spawn step reached")
    finally:
        if r._LOG_FH is not None and r._LOG_FH is not prev:
            r._LOG_FH.close()
        r._LOG_FH = prev
    assert "worker spawn step reached" in log_file.read_text(encoding="utf-8")


def test_raise_open_file_limit_lifts_soft_to_bounded_target() -> None:
    """The worker spawner lifts its open-file soft limit to min(hard, target),
    so a large fleet does not hit EMFILE in collection (a false kill), and never
    to an unbounded cap that slows every spawn. Without RLIMIT_NOFILE it
    returns None."""
    from langadapter.python._cr_session_runner import (
        _raise_open_file_limit, _OPEN_FILES_TARGET)
    try:
        import resource
    except ImportError:
        assert _raise_open_file_limit() is None
        return
    orig = resource.getrlimit(resource.RLIMIT_NOFILE)
    try:
        # Start from a low soft limit so the raise is observable.
        _, hard = orig
        low = 256 if hard == resource.RLIM_INFINITY or hard > 256 else hard
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (low, hard))
        except (ValueError, OSError):
            low = orig[0]
        expected = (_OPEN_FILES_TARGET if hard == resource.RLIM_INFINITY
                    else min(hard, _OPEN_FILES_TARGET))
        result = _raise_open_file_limit()
        assert result is not None
        soft, _ = result
        assert soft == max(low, expected)
        # Never raised to an unbounded cap: bounded by the target.
        if hard == resource.RLIM_INFINITY:
            assert soft == _OPEN_FILES_TARGET
    finally:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, orig)
        except (ValueError, OSError):
            pass


def test_cosmic_ray_capture_full_output_gated_by_config(
    tmp_path: Path,
) -> None:
    """The raw per-mutant test output, a large diagnostic, enters the matrix
    only with ``cosmic_ray_keep_output`` set."""
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    _make_fake_session_db(
        session_dir / "cr-session.sqlite",
        catalogue_size=1,
        outcomes={"KILLED": 1},
        outputs={"job_0": _record("E   MemoryError\n1 failed in 0.1s",
                                         failed=[], errors=[], counts={"failed": 1})},
    )

    off = _local_engine({"cosmic_ray_session_dir": str(session_dir)})
    payload = _session_matrix(off, session_dir)
    assert all("output" not in m for m in payload["mutants"])

    on = _local_engine({
        "cosmic_ray_session_dir": str(session_dir),
        "cosmic_ray_keep_output": True,
    })
    payload = _session_matrix(on, session_dir)
    assert any("MemoryError" in m.get("output", "") for m in payload["mutants"])


def test_cosmic_ray_capture_score_is_marker_independent(
    tmp_path: Path,
) -> None:
    """A KILLED mutant whose output names a resource error stays killed
    and counts toward the score: no output text reclassifies a verdict."""
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    _make_fake_session_db(
        session_dir / "cr-session.sqlite",
        catalogue_size=1,
        outcomes={"KILLED": 1},
        outputs={"job_0": "E   OSError: [Errno 24] Too many open files\n"
                          "1 error in 6.5s"},
    )
    engine = _local_engine({"cosmic_ray_session_dir": str(session_dir)})
    payload = _session_matrix(engine, session_dir)
    summary = payload["summary"]
    assert summary["detected"] == 1
    assert summary["survived"] == 0
    m = payload["mutants"][0]
    assert m["status"] == "killed"
    # No reclassification provenance keys are written.
    assert "infra_error" not in m
    assert "infra_marker" not in m
    assert "suspect_infra_kills" not in summary


def test_cosmic_ray_capture_persists_diff_for_self_sufficient_matrix(
    tmp_path: Path,
) -> None:
    """The matrix stores each mutant's diff, so survivors, also of a derived
    operator subset, render from it without a live session."""
    from langadapter import mutation as km
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    _make_fake_session_db(
        session_dir / "cr-session.sqlite",
        catalogue_size=1,
        outcomes={"SURVIVED": 1},
        survivor_diffs=[("job_0", 7, "- a = 1\n+ a = 2")],
    )
    engine = _local_engine({"cosmic_ray_session_dir": str(session_dir)})
    payload = _session_matrix(engine, session_dir)
    survivors = km.survivors_from_matrix(payload)
    assert len(survivors) == 1
    assert survivors[0]["id"] == "job_0"
    assert survivors[0]["diff"] == "- a = 1\n+ a = 2"


def test_cosmic_ray_capture_records_killer_test_per_mutant(
    tmp_path: Path,
) -> None:
    """Each mutant's ``killer`` is the first FAILED node id in its stored
    output (-x), and a survivor has none."""
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    _make_fake_session_db(
        session_dir / "cr-session.sqlite",
        catalogue_size=3,
        outcomes={"KILLED": 2, "SURVIVED": 1},
        outputs={
            "job_0": _record(".F\nFAILED test_foo.py::test_a - assert 1 == 2\n1 failed",
                             failed=["test_foo.py::test_a"], errors=[]),
            "job_1": _record(".F\nFAILED test_foo.py::TestC::test_b - assert\n1 failed",
                             failed=["test_foo.py::TestC::test_b"], errors=[]),
            "job_2": _record("...\n3 passed", failed=[], errors=[]),  # survivor: no FAILED line
        },
    )
    engine = _local_engine({"cosmic_ray_session_dir": str(session_dir)})
    by_id = {m["id"]: m for m in _session_matrix(engine, session_dir)["mutants"]}
    assert by_id["job_0"]["killer"] == "test_foo.py::test_a"
    assert by_id["job_1"]["killer"] == "test_foo.py::TestC::test_b"
    assert "killer" not in by_id["job_2"]


def test_cosmic_ray_capture_records_operator_for_selective_derivation(
    tmp_path: Path,
) -> None:
    """Each mutant's operator is recorded, so a selective score derives from
    the full-catalogue run by filtering, with no second run."""
    from langadapter import mutation as km
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    _make_fake_session_db(
        session_dir / "cr-session.sqlite",
        catalogue_size=4,
        outcomes={"KILLED": 3, "SURVIVED": 1},
        operators=["OpA", "OpA", "OpB", "OpB"],
    )
    engine = _local_engine({"cosmic_ray_session_dir": str(session_dir)})
    matrix = _session_matrix(engine, session_dir)

    assert km.has_operators(matrix)
    # Full catalogue: 3 of 4 valid mutants killed.
    assert km.matrix_score_percent(matrix) == 75.0
    # OpA kills both its mutants, OpB one of two.
    assert km.matrix_score_percent(matrix, operators={"OpA"}) == 100.0
    assert km.matrix_score_percent(matrix, operators={"OpB"}) == 50.0


# Catalogue cache: corruption detection and invalidation


def test_validate_session_db_accepts_real_cosmic_ray_session(
    tmp_path: Path,
) -> None:
    """A SQLite file with Cosmic Ray's ``work_items`` table, as a fresh
    session has it, passes."""
    import sqlite3
    from langadapter.python._cosmic_ray import _validate_session_db

    db = tmp_path / "ok.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE work_items (job_id TEXT PRIMARY KEY)")
        conn.commit()
    finally:
        conn.close()
    _validate_session_db(db)


def test_validate_session_db_rejects_non_sqlite_file(tmp_path: Path) -> None:
    """Reject a non-SQLite session before invoking the mutation engine."""
    import sqlite3
    from langadapter.python._cosmic_ray import _validate_session_db

    db = tmp_path / "corrupt.sqlite"
    db.write_bytes(b"this is not a sqlite database at all")
    with pytest.raises(sqlite3.DatabaseError):
        _validate_session_db(db)


def test_validate_session_db_rejects_sqlite_without_work_items_table(
    tmp_path: Path,
) -> None:
    """A SQLite file without the ``work_items`` table is refused before exec
    would fail less clearly."""
    import sqlite3
    from langadapter.python._cosmic_ray import _validate_session_db

    db = tmp_path / "wrong_schema.sqlite"
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE something_else (x INTEGER)")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(sqlite3.DatabaseError):
        _validate_session_db(db)


def test_validate_session_db_rejects_partial_corruption(
    tmp_path: Path,
) -> None:
    """Corrupt table data is refused even when the SQLite header and
    sqlite_master are readable."""
    import sqlite3
    from langadapter.python._cosmic_ray import _validate_session_db

    db = tmp_path / "partial_corrupt.sqlite"
    # Build a real cosmic-ray-shaped session, then corrupt later pages.
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("CREATE TABLE work_items (job_id TEXT PRIMARY KEY)")
        # Add enough rows to force allocation beyond page 1.
        conn.executemany(
            "INSERT INTO work_items VALUES (?)",
            [(f"j{i}",) for i in range(500)],
        )
        conn.commit()
    finally:
        conn.close()

    # Overwrite every page after the first (SQLite's default 4096 bytes),
    # leaving the header and sqlite_master intact.
    raw = bytearray(db.read_bytes())
    page_size = 4096
    assert len(raw) > page_size, "test file too small for partial corruption"
    for i in range(page_size, len(raw)):
        raw[i] = 0xFF
    db.write_bytes(bytes(raw))

    # sqlite_master still lists work_items, so only a deep check fails.
    with pytest.raises(sqlite3.DatabaseError):
        _validate_session_db(db)


def test_persist_cache_refuses_corrupt_session(tmp_path: Path) -> None:
    """``_persist_cache`` refuses a malformed session, as an interrupted run
    leaves it, so it cannot poison the cache for later targets, and logs the
    refusal."""
    bad_session = tmp_path / "session.sqlite"
    bad_session.write_bytes(b"not sqlite bytes")
    cache_path = tmp_path / "cache" / "x.sqlite"
    logs: list[str] = []
    cr_engine.CosmicRayEngine._persist_cache(
        bad_session, cache_path, log_fn=lambda m, level=0: logs.append(m),
    )
    assert not cache_path.exists(), (
        "cache file was persisted from a corrupt source, so future runs "
        "will keep hitting the same failure"
    )
    assert any("REFUSED to cache" in m for m in logs)


def test_persist_cache_refuses_corrupt_session_without_a_log_function(tmp_path: Path) -> None:
    """Without a log function ``_persist_cache`` refuses a malformed session
    as well: nothing is persisted, the session is left as it was, and nothing
    is raised."""
    bad_session = tmp_path / "session.sqlite"
    bad_session.write_bytes(b"not sqlite bytes")
    cache_path = tmp_path / "cache" / "x.sqlite"
    cr_engine.CosmicRayEngine._persist_cache(bad_session, cache_path)
    assert not cache_path.parent.exists()
    assert bad_session.read_bytes() == b"not sqlite bytes"


def test_kill_matrix_summary_detected_includes_timeout_not_incompetent(
    tmp_path: Path,
) -> None:
    """``summary["detected"]`` counts killed and timeout, and the timeout
    field keeps the breakdown. An incompetent mutant measured nothing and
    counts as untested."""
    session_dir = tmp_path / "sess"
    session_dir.mkdir()
    # 3 killed, 2 survived, 1 timeout and 4 incompetent: 4 detected in all.
    _make_fake_session_db(
        session_dir / "cr-session.sqlite",
        catalogue_size=10,
        outcomes={"KILLED": 4, "SURVIVED": 2, "INCOMPETENT": 4},
        outputs={"job_3": "timeout"},
    )
    engine = _local_engine({"cosmic_ray_session_dir": str(session_dir)})
    payload = _session_matrix(engine, session_dir)
    summary = payload["summary"]
    assert summary["detected"] == 3 + 1, summary
    assert summary["survived"] == 2
    assert summary["timeout"] == 1
    assert (summary["non_viable"], summary["untested"]) == (0, 4)


def _cosmic_ray_session(path: Path, operators, done=()) -> None:
    """A session as Cosmic Ray's own API writes it: one mutant per operator
    name, job ids j0, j1 and so on, and a NORMAL result for each job id in
    ``done``."""
    work_db = pytest.importorskip("cosmic_ray.work_db")
    from cosmic_ray.work_item import MutationSpec, WorkerOutcome, WorkItem, WorkResult

    with work_db.use_db(str(path)) as db:
        try:
            for i, operator in enumerate(operators):
                db.add_work_item(WorkItem(job_id=f"j{i}", mutations=(
                    MutationSpec(Path("m.py"), operator, 0, (1, 0), (1, 1)),)))
            for job_id in done:
                db.set_result(job_id, WorkResult(worker_outcome=WorkerOutcome.NORMAL))
        finally:
            # WorkDB.close does nothing, so its engine's pool keeps the
            # connection open, which Python 3.13 reports when it is collected.
            db._engine.dispose()


def _filter_config(path: Path, patterns) -> Path:
    """A cosmic-ray.toml whose operators filter excludes ``patterns``."""
    text = "[cosmic-ray]\nmodule-path = 'm.py'\n"
    if patterns:
        text += ("\n[cosmic-ray.filters.operators-filter]\nexclude-operators = ["
                 + ", ".join(f"'{pattern}'" for pattern in patterns) + "]\n")
    path.write_text(text, encoding="utf-8")
    return path


def _results(session: Path) -> dict:
    conn = sqlite3.connect(str(session))
    try:
        return {job: (outcome, output) for job, outcome, output in conn.execute(
            "SELECT job_id, worker_outcome, output FROM work_results")}
    finally:
        conn.close()


def test_the_operator_filter_skips_the_pending_mutants_of_excluded_operators(
    tmp_path: Path,
) -> None:
    """Cosmic Ray's cr-filter-operators, run by the project interpreter,
    marks the pending mutants of the excluded operators skipped, leaves the
    others pending and keeps a result the session already holds."""
    session = tmp_path / "session.sqlite"
    _cosmic_ray_session(session, [
        "core/ReplaceBinaryOperator_Add_Sub", "core/ReplaceUnaryOperator_Negate",
        "core/ReplaceBooleanOperator_And_Or", "core/ReplaceUnaryOperator_Negate",
    ], done=["j3"])
    config = _filter_config(tmp_path / "cosmic-ray.toml", [
        "^core/ReplaceUnaryOperator_.*$", "^core/ReplaceBooleanOperator_.*$"])
    logs: list = []

    _local_engine({})._apply_operator_filter(
        sys.executable, config, session, log_fn=lambda m, level=0: logs.append(m))

    rows = _results(session)
    assert {job for job, (outcome, _) in rows.items()
            if outcome.lower() == "skipped"} == {"j1", "j2"}
    assert rows["j1"][1] == "Filtered operator"
    assert "j0" not in rows and rows["j3"][0].lower() == "normal"
    assert "[CosmicRayEngine] operator filter applied" in logs


def test_a_config_without_excluded_operators_leaves_the_session_as_it_is(
    tmp_path: Path,
) -> None:
    session = tmp_path / "session.sqlite"
    _cosmic_ray_session(session, ["core/ReplaceUnaryOperator_Negate"])

    _local_engine({})._apply_operator_filter(
        sys.executable, _filter_config(tmp_path / "cosmic-ray.toml", []), session)

    assert _results(session) == {}


def test_a_session_the_filter_cannot_read_stops_the_run_with_its_reason(
    tmp_path: Path,
) -> None:
    """A session file that is no database: cr-filter-operators fails, and
    the run stops with its reason rather than measuring every mutant."""
    pytest.importorskip("cosmic_ray.work_db")
    session = tmp_path / "session.sqlite"
    session.write_bytes(b"not a session")

    with pytest.raises(RuntimeError, match="the operator filter failed") as stopped:
        _local_engine({})._apply_operator_filter(
            sys.executable, _filter_config(tmp_path / "cosmic-ray.toml", ["^X.*$"]),
            session)
    assert "cr-filter-operators exited 1" in str(stopped.value)
    assert "not a database" in str(stopped.value)


def test_resolve_paths_defaults_to_local_tempdir_not_project_root(
    tmp_path,
) -> None:
    """An unset session directory defaults to a local temporary directory outside
    the project root."""
    engine = _local_engine({})
    project_root = tmp_path / "work" / "rank1"
    project_root.mkdir(parents=True)

    session_path, config_path = engine._resolve_paths()
    assert session_path.name == "cr-session.sqlite"
    assert config_path.name == "cosmic-ray.toml"
    # Not under project_root, which may be on a network file system.
    try:
        session_path.relative_to(project_root)
        in_project_root = True
    except ValueError:
        in_project_root = False
    assert not in_project_root, (
        f"session_path {session_path} resolved inside project_root "
        f"{project_root}: on a network file system this triggers sqlite disk I/O "
        f"errors under HTTP-master write load."
    )
    if engine._default_session_dir is not None:
        shutil.rmtree(engine._default_session_dir, ignore_errors=True)


def test_resolve_paths_honors_explicit_session_dir_override(
    tmp_path,
) -> None:
    """A set ``cosmic_ray_session_dir`` wins over the temporary default."""
    explicit = tmp_path / "explicit_session_dir"
    engine = _local_engine({"cosmic_ray_session_dir": str(explicit)})
    project_root = tmp_path / "work"
    project_root.mkdir()

    session_path, _ = engine._resolve_paths()
    assert session_path.parent == explicit
    # No auto-tempdir created when override is in play.
    assert getattr(engine, "_default_session_dir", None) in (None, False)


import shutil  # noqa: E402  -- needed by the helpers above


def test_sync_test_dir_to_clones_propagates_edits_to_every_clone(
    tmp_path,
) -> None:
    """Clones are made when the session opens and the suite can change
    between runs: after the sync every clone's tests directory matches the live
    test_dir byte for byte."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    test_dir = project_root / "tests"
    test_dir.mkdir()
    (test_dir / "test_a.py").write_text("def test_a(): assert 1\n")

    # Clones with stale test files, as the session made them on opening.
    clones = []
    for i in range(3):
        c = tmp_path / f"clone_{i}"
        (c / "tests").mkdir(parents=True)
        (c / "tests" / "test_a.py").write_text("# stale stub\n")
        clones.append(c)

    cr_engine.CosmicRayEngine._sync_test_dir_to_clones(
        test_dir=test_dir,
        rel_test_dir="tests",
        clone_dirs=clones,
        log_fn=None,
    )

    expected = (test_dir / "test_a.py").read_bytes()
    for c in clones:
        synced = (c / "tests" / "test_a.py").read_bytes()
        assert synced == expected, (
            f"clone {c} test file not synced to live bytes, so "
            f"workers will test against stale tests"
        )


def test_sync_test_dir_to_clones_picks_up_new_files(tmp_path) -> None:
    """A test file added after the clones were made reaches every clone
    with the sync, so the workers run it."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    test_dir = project_root / "tests"
    test_dir.mkdir()
    # The original file (already in the clones).
    (test_dir / "test_original.py").write_text("def test_orig(): pass\n")
    # A file added after the clones were made.
    (test_dir / "test_added_after_clone.py").write_text(
        "def test_new(): assert True\n",
    )

    clone = tmp_path / "clone_0"
    (clone / "tests").mkdir(parents=True)
    # Clone was made when only the original file existed.
    (clone / "tests" / "test_original.py").write_text("def test_orig(): pass\n")

    cr_engine.CosmicRayEngine._sync_test_dir_to_clones(
        test_dir=test_dir,
        rel_test_dir="tests",
        clone_dirs=[clone],
        log_fn=None,
    )

    assert (clone / "tests" / "test_added_after_clone.py").exists(), (
        "new test file not propagated to clone -- workers won't run it"
    )


def test_sync_test_dir_to_clones_propagates_both_coverage_and_mutation_files(
    tmp_path,
) -> None:
    """With a coverage file and a mutation file of which only the mutation
    file changes between runs, every clone has the live bytes of both after
    each sync."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    test_dir = project_root / "tests"
    test_dir.mkdir()

    coverage_file = test_dir / "test_thing.py"
    mutation_file = test_dir / "test_thing_mutation.py"
    coverage_file.write_text("def test_cov_v1(): assert True\n")
    mutation_file.write_text("def test_mut_v1(): assert True\n")

    clones = [tmp_path / f"clone_{i}" for i in range(4)]
    for c in clones:
        (c / "tests").mkdir(parents=True)
        # Both files are stale on the clones (an earlier snapshot).
        (c / "tests" / "test_thing.py").write_text("# stale coverage\n")
        (c / "tests" / "test_thing_mutation.py").write_text("# stale mut\n")

    cr_engine.CosmicRayEngine._sync_test_dir_to_clones(
        test_dir=test_dir,
        rel_test_dir="tests",
        clone_dirs=clones,
        log_fn=None,
    )

    expected_cov = coverage_file.read_bytes()
    expected_mut = mutation_file.read_bytes()
    for c in clones:
        cov_bytes = (c / "tests" / "test_thing.py").read_bytes()
        mut_bytes = (c / "tests" / "test_thing_mutation.py").read_bytes()
        assert cov_bytes == expected_cov, (
            f"clone {c} coverage file out of sync, so workers will see a "
            f"stale coverage suite"
        )
        assert mut_bytes == expected_mut, (
            f"clone {c} mutation file out of sync, so workers will see a "
            f"stale mutation suite and kill counts will not change "
            f"between measure_mutation calls"
        )

    # Second round: only the mutation file changes, and after the resync every
    # clone has its new bytes and the unchanged coverage file.
    mutation_file.write_text("def test_mut_v2(): assert 1 + 1 == 2\n")
    cr_engine.CosmicRayEngine._sync_test_dir_to_clones(
        test_dir=test_dir,
        rel_test_dir="tests",
        clone_dirs=clones,
        log_fn=None,
    )
    new_expected_mut = mutation_file.read_bytes()
    for c in clones:
        cov_bytes = (c / "tests" / "test_thing.py").read_bytes()
        mut_bytes = (c / "tests" / "test_thing_mutation.py").read_bytes()
        assert cov_bytes == expected_cov  # untouched between rounds
        assert mut_bytes == new_expected_mut, (
            f"clone {c} mutation file not re-synced on second call -- "
            f"this is the byte-identical-score bug"
        )


def test_alloc_free_port_sockets_returns_bound_sockets(tmp_path) -> None:
    """``_alloc_free_port_sockets`` returns N bound sockets on distinct
    unprivileged ports, so each closes only as its worker spawns and the
    race for a port lasts microseconds."""
    import socket as _socket
    from types import SimpleNamespace
    socks = cr_engine.CosmicRayEngine._alloc_free_port_sockets(
        4, owner=SimpleNamespace(_worker_sockets=[]))
    try:
        assert len(socks) == 4
        ports = []
        for s in socks:
            assert isinstance(s, _socket.socket)
            host, port = s.getsockname()
            assert host == "127.0.0.1"
            assert 1024 <= port <= 65535
            ports.append(port)
        # All four ports are distinct (kernel won't double-issue).
        assert len(set(ports)) == 4
    finally:
        for s in socks:
            s.close()


def test_a_failed_operator_filter_stops_the_run_with_its_error(
    tmp_path: Path, monkeypatch,
) -> None:
    """When the operator filter cannot be applied, the run stops with the
    filter's error instead of measuring every mutant of the catalogue as
    the requested operator subset. No other filter runs."""
    from langadapter.python import _cosmic_ray as cre

    calls = []

    def failing_filter(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "no such table: mutation_specs")
    monkeypatch.setattr(cre._process_tree, "run", failing_filter)
    logs = []
    engine = _local_engine({})
    with pytest.raises(RuntimeError, match="the operator filter failed") as stopped:
        engine._apply_operator_filter(
            "python", tmp_path / "c.toml", tmp_path / "s.sqlite",
            log_fn=lambda m, level=0: logs.append(m))
    assert [argv[1:4] for argv in calls] == [
        ["-I", "-m", "cosmic_ray.tools.filters.operators_filter"]]
    assert "cr-filter-operators exited 1: no such table: mutation_specs" in str(stopped.value)
    assert any("the operator filter failed" in line for line in logs)


# Container mode: the worker fleet runs in the container through the launcher.
# A fake executor records the argv and writes the session the launcher would
# leave in the work directory, with no real container or cosmic-ray process.

#: A fake interpreter's catalogue, with more names than any requested subset,
#: since the engine realizes an allow-list by excluding the rest.
_FAKE_CATALOGUE = (
    "core/ReplaceAndWithOr",
    "core/ReplaceOrWithAnd",
    "core/NumberReplacer",
    "core/ReplaceTrueWithFalse",
)


def _catalogue_probe_stdout(
    version: str = "8.4.6", operators=_FAKE_CATALOGUE, python: str = "py3.12",
) -> str:
    """What the project interpreter prints for the engine's metadata probe
    (Cosmic Ray version, catalogue, Python version). A fake without it turns
    the catalogue cache off and makes an operator subset unrealizable."""
    return cr_engine._PROJECT_METADATA_PREFIX + json.dumps(
        {"version": version, "operators": list(operators), "python": python},
        sort_keys=True,
    ) + "\n"


def _is_catalogue_probe(argv) -> bool:
    return any(cr_engine._PROJECT_METADATA_PREFIX in str(a) for a in argv)


def _source_probe(argv):
    """The session's source-dir probe answered for a flat layout (none),
    or None for another command."""
    if cr_engine._SHADOWED_ROOTS not in argv:
        return None
    return subprocess.CompletedProcess(list(argv), 0, '{"inside": [], "outside": []}\n', "")


def _answer_origin(argv, env, work_dir, seen=None):
    """Answer the origin run (``_import_origin``) with a record at the host
    path of the container's work directory, by default with the module imported
    from the project."""
    target = env["LANGADAPTER_IMPORT_ORIGIN_TARGET"]
    inside = container_work_dir_for(str(work_dir))
    record = Path(env["LANGADAPTER_IMPORT_ORIGIN_RECORD"]).as_posix()
    host = Path(work_dir).joinpath(*Path(record).relative_to(inside).parts)
    seen = seen or {"pkg.mod": [{"file": target, "copy": True, "counted": True}]}
    host.write_text(json.dumps({"target": target, "names": list(seen), "seen": seen}),
                    encoding="utf-8")
    return subprocess.CompletedProcess(list(argv), 0, "1 passed\n", "")


class _FakeContainerExecutor:
    """Stand-in for ``ContainerExecutor``: records each launcher run and calls
    ``on_run(argv, cwd)`` to write the session and status file, and answers
    the metadata, source and origin probes."""

    def __init__(self, on_run) -> None:
        #: Launcher runs only, so a count of them stays exact. Probes go to
        #: probe_calls.
        self.calls: list = []
        self.probe_calls: list = []
        #: The origin runs (``_import_origin``), answered from ``origin_seen``:
        #: by default the tests imported the module from the project.
        self.origin_calls: list = []
        self.origin_seen = None
        self.work_dir = None
        #: The container's Python, as the toolchain probe answers it.
        self.python_version = "3.12.3"
        self._on_run = on_run

    def run(self, argv, *, cwd=None, env=None, timeout=None, input=None):
        if "langadapter_origin" in argv:
            self.origin_calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env or {})})
            return _answer_origin(argv, env, self.work_dir, self.origin_seen)
        if _is_catalogue_probe(argv):
            self.probe_calls.append({"argv": list(argv), "cwd": cwd})
            return subprocess.CompletedProcess(
                list(argv), 0, _catalogue_probe_stdout(), "")
        if any("metadata.version(name)" in str(part) for part in argv):
            # The toolchain probe, which the version profile reads.
            self.probe_calls.append({"argv": list(argv), "cwd": cwd})
            return subprocess.CompletedProcess(list(argv), 0, json.dumps(
                {"implementation": "CPython", "version": self.python_version,
                 "tools": {}}), "")
        if _source_probe(argv) is not None:
            self.probe_calls.append({"argv": list(argv), "cwd": cwd})
            return _source_probe(argv)
        if any(part in (_project_files._EXISTS, _project_files._REMOVE_EMPTY_DIR)
               for part in argv):
            # The session's check of pytest-benchmark's storage: none here.
            self.probe_calls.append({"argv": list(argv), "cwd": cwd})
            return subprocess.CompletedProcess(list(argv), 1, "", "")
        self.calls.append({"argv": list(argv), "cwd": cwd, "timeout": timeout})
        # The launcher's exit status, 0 unless on_run returns one.
        status = self._on_run(list(argv), cwd)
        return subprocess.CompletedProcess(list(argv), status or 0, "[cr-runner] ok\n", "")


def _container_summary(engine, target: Path) -> dict:
    """The counts of the container run's session database, read as the
    session's capture reads it."""
    return engine._read_mutation_run(engine._container_session_path(), target).summary


def _work_dir(tmp_path: Path) -> Path:
    wz = tmp_path / "wz"
    wz.mkdir()
    return wz


def test_container_session_path_helper(tmp_path) -> None:
    engine = _local_engine({})
    assert engine._container_session_path() is None
    wz = tmp_path / "wz"
    engine = _container_engine({}, wz)
    sp = engine._container_session_path()
    assert sp == wz / "cr" / "cr-session.sqlite"


def test_local_mode_session_is_not_container_mode() -> None:
    """A local context uses host workers, not the container launcher."""
    engine = _local_engine({"engine": "cosmic_ray"})
    sess = engine.session(project_root=Path("."), workers=4)
    assert sess.container_mode is False


def test_container_session_enter_skips_host_worker_spawn(
    monkeypatch, tmp_path,
) -> None:
    """In container mode __enter__ opens with no host worker pool. The
    launcher spawns the fleet inside the container on each exec."""
    engine = _container_engine({"engine": "cosmic_ray"},
                               _work_dir(tmp_path),
                               _FakeContainerExecutor(lambda argv, cwd: None))

    def _boom(*a, **k):
        raise AssertionError("host worker spawn must not run in container mode")

    monkeypatch.setattr(engine, "_start_http_workers", _boom)
    sess = engine.session(project_root=Path("/home/swe/repo"), workers=8)
    assert sess.container_mode is True
    with sess as s:
        assert s._opened is True
        assert s._worker_procs == []
        assert s.worker_urls == []


def test_a_container_run_whose_filter_failed_stops_with_its_error(
    monkeypatch, tmp_path,
) -> None:
    """The launcher's filter failure stops the run with the host's refusal,
    instead of the results of an exec over the whole catalogue."""
    wz = _work_dir(tmp_path)
    engine_config = {
        "engine": "cosmic_ray",
        "cosmic_ray_catalogue_cache_dir": "",
    }

    def fake_launcher(argv, cwd):
        session = wz / "cr" / "cr-session.sqlite"
        _make_fake_session_db(session, catalogue_size=2, outcomes={"SURVIVED": 2})
        Path(str(session) + ".runner_status.json").write_text(
            json.dumps({"truncated": False, "exec_returncode": None, "ready_workers": 0,
                        "filter_failed": "OperationalError: no such table: mutation_specs"}),
            encoding="utf-8")

    ex = _FakeContainerExecutor(fake_launcher)
    engine = _container_engine(engine_config, wz, ex)
    test_dir = wz / "tests"
    test_dir.mkdir()
    with engine.session(project_root=Path("/home/swe/repo"), workers=1) as s:
        with pytest.raises(RuntimeError, match="the operator filter failed") as stopped:
            s.run(target=Path("/home/swe/repo/pkg/mod.py"), test_dir=test_dir,
                  per_mutant_timeout=30, wall_clock_timeout=600,
                  operators=["core/ReplaceAndWithOr"])
    assert "no such table: mutation_specs" in str(stopped.value)


def test_a_container_python_3_14_starts_cosmic_ray_with_an_event_loop(
    monkeypatch, tmp_path,
) -> None:
    """The spec's Cosmic Ray command line is the container Python's
    (``PythonProfile.cosmic_ray_command``): on 3.14 the interpreter runs
    Cosmic Ray's command line once it has set an event loop."""
    wz = _work_dir(tmp_path)

    def fake_launcher(argv, cwd):
        session = wz / "cr" / "cr-session.sqlite"
        _make_fake_session_db(session, catalogue_size=1, outcomes={"KILLED": 1},
                              outputs={"job_0": _record("FAILED t.py::test_a - x\n1 failed in 0.1s",
                                                         failed=["t.py::test_a"], errors=[],
                                                         counts={"failed": 1})})
        Path(str(session) + ".runner_status.json").write_text(
            json.dumps({"truncated": False, "exec_returncode": 0, "ready_workers": 2}),
            encoding="utf-8")

    ex = _FakeContainerExecutor(fake_launcher)
    ex.python_version = "3.14.7"
    engine = _container_engine({"engine": "cosmic_ray", "cosmic_ray_distributor": "http",
                                "cosmic_ray_catalogue_cache_dir": ""}, wz, ex)
    test_dir = wz / "tests"
    test_dir.mkdir()
    with engine.session(project_root=Path("/home/swe/repo"), workers=2) as s:
        s.run(target=Path("/home/swe/repo/pkg/mod.py"), test_dir=test_dir,
              per_mutant_timeout=30, wall_clock_timeout=600, operators=["core/ReplaceAndWithOr"])
    spec = json.loads((wz / "cr" / "spec.json").read_text(encoding="utf-8"))
    assert spec["cosmic_ray_command"] == [_CONTAINER_PYTHON, "-c", EVENT_LOOP_COSMIC_RAY]


def test_container_run_invokes_launcher_and_parses_session(
    monkeypatch, tmp_path,
) -> None:
    """A container run writes settings without a distributor block and a spec
    with the container wiring, copies the runner into the work directory, calls
    the executor once and parses the session the launcher leaves."""
    wz = _work_dir(tmp_path)
    engine_config = {
        "engine": "cosmic_ray",
        "cosmic_ray_distributor": "http",  # exercise the parallel fleet path
        "cosmic_ray_catalogue_cache_dir": "",  # disable cache for this test
    }

    def fake_launcher(argv, cwd):
        session = wz / "cr" / "cr-session.sqlite"
        _make_fake_session_db(
            session, catalogue_size=4, outcomes={"KILLED": 3, "SURVIVED": 1},
            outputs={f"job_{i}": _record("FAILED t.py::test_a - x\n1 failed in 0.1s", failed=["t.py::test_a"],
                                         errors=[], counts={"failed": 1}) for i in range(3)})
        Path(str(session) + ".runner_status.json").write_text(
            json.dumps({"truncated": False, "exec_returncode": 0,
                        "ready_workers": 8}), encoding="utf-8")

    ex = _FakeContainerExecutor(fake_launcher)
    engine = _container_engine(engine_config, wz, ex)

    target = Path("/home/swe/repo/pkg/mod.py")
    test_dir = wz / "tests"  # outside project_root -> no per-clone sync
    test_dir.mkdir()
    with engine.session(project_root=Path("/home/swe/repo"), workers=8) as s:
        s.run(
            target=target, test_dir=test_dir,
            per_mutant_timeout=30, wall_clock_timeout=600,
            operators=["core/ReplaceAndWithOr"])
        summary = _container_summary(engine, target)

    assert (summary["detected"], summary["survived"]) == (3, 1)

    assert len(ex.calls) == 1
    argv = ex.calls[0]["argv"]
    assert argv[0] == "/home/swe/miniconda3/envs/x/bin/python"
    assert argv[1].endswith("_cr_session_runner.py")
    assert argv[2].endswith("spec.json")
    assert ex.calls[0]["cwd"] == "/home/swe/repo"

    cfg = (wz / "cr" / "cosmic-ray.toml").read_text(encoding="utf-8")
    settings = _cosmic_ray_settings(cfg)
    assert "distributor" not in settings
    assert settings["module-path"] == "pkg/mod.py"
    # operators restricted -> the operator-filter block is still emitted
    assert settings["filters"]["operators-filter"]["exclude-operators"]
    # pytest runs through the fd-limit wrapper, which raises the open-file
    # soft limit before import, so EMFILE in collection is no false kill.
    assert "_cr_fdlimit_run.sh" in cfg
    fdwrap = (wz / "cr" / "_cr_fdlimit_run.sh").read_text(encoding="utf-8")
    # Only the soft limit rises, to 65536 bounded by the hard limit.
    assert "ulimit -S -n" in fdwrap and "_want=65536" in fdwrap
    # Single-threaded numeric libs in the kill process: deterministic floats and
    # no thread-pool descriptor source under the fleet.
    assert "OMP_NUM_THREADS=1" in fdwrap and "NUMBA_NUM_THREADS=1" in fdwrap
    # GIL-free fd watcher present (off unless LANGADAPTER_FDLEAK_WATCH).
    assert "LANGADAPTER_FDLEAK_WATCH" in fdwrap

    spec = json.loads((wz / "cr" / "spec.json").read_text(encoding="utf-8"))
    assert spec["workers"] == 8
    assert spec["run_init"] is True
    assert spec["run_filter"] is True
    assert spec["sync_tests_to_clones"] is False
    assert spec["rel_test_dir"] is None
    (command,) = spec["cosmic_ray_command"]
    assert command.endswith("/cosmic-ray")
    # Clones copy the whole project, .git included, as the host engine's do.
    assert "clone_ignore" not in spec
    # No override -> clones default beside the session in the work directory.
    assert spec["clone_base"].endswith("cr_worker_clones")

    assert (wz / "cr" / "_cr_session_runner.py").exists()


def _origin_engine(tmp_path):
    """A container engine whose launcher leaves one killed mutant, with
    the journal plugin on."""
    wz = _work_dir(tmp_path)

    def fake_launcher(argv, cwd):
        session = wz / "cr" / "cr-session.sqlite"
        _make_fake_session_db(session, catalogue_size=1, outcomes={"KILLED": 1})
        Path(str(session) + ".runner_status.json").write_text(
            json.dumps({"truncated": False, "exec_returncode": 0, "ready_workers": 1}),
            encoding="utf-8")

    ex = _FakeContainerExecutor(fake_launcher)
    engine = _container_engine({"engine": "cosmic_ray",
                                "cosmic_ray_catalogue_cache_dir": "",
                                "_timeout_journal": True, "_journal_plugin": True}, wz, ex)
    (wz / "tests").mkdir()
    return engine, ex, wz


def _origin_run(engine, wz):
    """The counts of one container run of the project, read in its session."""
    target = Path("/home/swe/repo/pkg/mod.py")
    with engine.session(project_root=Path("/home/swe/repo"), workers=1) as s:
        s.run(target=target, test_dir=wz / "tests",
              per_mutant_timeout=30, wall_clock_timeout=600,
              operators=["core/ReplaceAndWithOr"])
        return _container_summary(engine, target)


def test_container_run_checks_where_the_tests_import_the_target_first(tmp_path) -> None:
    """Before the launcher, the test command runs once in the project with
    the origin plugin, its record in the work directory and the journal plugin's
    directory on the path."""
    engine, ex, wz = _origin_engine(tmp_path)
    assert _origin_run(engine, wz)["detected"] == 1
    (origin,) = ex.origin_calls
    assert ex.calls[0]["argv"][2].endswith("spec.json")
    assert origin["cwd"] == "/home/swe/repo"
    assert origin["argv"][0] == "sh" and origin["argv"][1].endswith("_cr_fdlimit_run.sh")
    plugin = origin["argv"].index("langadapter_origin")
    assert origin["argv"][plugin - 1] == "-p" and "-x" not in origin["argv"]
    assert origin["argv"][plugin + 1].startswith("--junit-xml=")
    assert origin["argv"][plugin + 2:] == ["-o", "junit_family=xunit1"]
    env = origin["env"]
    assert env["LANGADAPTER_IMPORT_ORIGIN_TARGET"] == "/home/swe/repo/pkg/mod.py"
    assert env["LANGADAPTER_IMPORT_ORIGIN_ROOT"] == "/home/swe/repo"
    inside = container_work_dir_for(str(wz))
    assert env["LANGADAPTER_IMPORT_ORIGIN_RECORD"].startswith(inside + "/")
    assert [entry.rsplit("/", 1)[-1] for entry in env["PYTHONPATH"].split(":")][:2] == [
        "origin_plugin", "journal_plugin"]
    # The wrapper names the target, so the check run restores the module
    # before each test, as the launcher's runs do.
    wrapper = (wz / "cr" / "_cr_fdlimit_run.sh").read_text(encoding="utf-8")
    assert ("export LANGADAPTER_MUTATION_TARGET=pkg/mod.py LANGADAPTER_MUTATION_SOURCE_ROOT="
            '"${LANGADAPTER_MUTATION_SOURCE_ROOT:-$PWD}"\n') in wrapper


def test_container_run_of_a_module_from_another_tree_is_refused_before_the_launcher(
        tmp_path) -> None:
    engine, ex, wz = _origin_engine(tmp_path)
    ex.origin_seen = {"pkg.mod": [{"file": "/home/swe/original/pkg/mod.py", "copy": False,
                                   "counted": True, "hook": True}]}
    with pytest.raises(_cr_verify.MutationInstrumentFault,
                       match="imported pkg.mod from /home/swe/original/pkg/mod.py"):
        _origin_run(engine, wz)
    assert ex.calls == []


def test_container_clone_dir_override_redirects_clone_base(
    monkeypatch, tmp_path,
) -> None:
    """``cosmic_ray_clone_dir`` moves the container fleet's clones off the
    work directory (for example onto /dev/shm), so their concurrent reads do not
    pile onto one network file system directory."""
    wz = _work_dir(tmp_path)
    shm = tmp_path / "shm_clones"
    engine_config = {
        "engine": "cosmic_ray",
        "cosmic_ray_distributor": "http",
        "cosmic_ray_catalogue_cache_dir": "",
        "cosmic_ray_clone_dir": str(shm),
    }

    def fake_launcher(argv, cwd):
        session = wz / "cr" / "cr-session.sqlite"
        _make_fake_session_db(
            session, catalogue_size=2, outcomes={"KILLED": 1, "SURVIVED": 1})
        Path(str(session) + ".runner_status.json").write_text(
            json.dumps({"truncated": False, "exec_returncode": 0,
                        "ready_workers": 4}), encoding="utf-8")

    ex = _FakeContainerExecutor(fake_launcher)
    engine = _container_engine(engine_config, wz, ex)
    test_dir = wz / "tests"
    test_dir.mkdir()
    with engine.session(project_root=Path("/home/swe/repo"), workers=4) as s:
        s.run(target=Path("/home/swe/repo/pkg/mod.py"), test_dir=test_dir,
              per_mutant_timeout=30, wall_clock_timeout=600)

    spec = json.loads((wz / "cr" / "spec.json").read_text(encoding="utf-8"))
    # The session claims a unique subdir under the configured base, so
    # concurrent fleets sharing one base never touch each other's clones.
    base, session = spec["clone_base"].rsplit("/", 1)
    assert base.endswith("shm_clones")
    assert session.startswith("cr_session_")
    assert "cr_worker_clones" not in spec["clone_base"]


def test_container_run_local_distributor_writes_block_and_no_fleet(
    monkeypatch, tmp_path,
) -> None:
    """The local default in container mode writes a local distributor block
    (exec runs serially, without a fleet) and use_http=False in the spec."""
    wz = _work_dir(tmp_path)
    engine_config = {
        "engine": "cosmic_ray",  # cosmic_ray_distributor defaults local
        "cosmic_ray_catalogue_cache_dir": "",
    }

    def fake_launcher(argv, cwd):
        session = wz / "cr" / "cr-session.sqlite"
        _make_fake_session_db(
            session, catalogue_size=4, outcomes={"KILLED": 3, "SURVIVED": 1})
        Path(str(session) + ".runner_status.json").write_text(
            json.dumps({"truncated": False, "exec_returncode": 0,
                        "ready_workers": 0}), encoding="utf-8")

    ex = _FakeContainerExecutor(fake_launcher)
    engine = _container_engine(engine_config, wz, ex)

    target = Path("/home/swe/repo/pkg/mod.py")
    test_dir = wz / "tests"
    test_dir.mkdir()
    with engine.session(project_root=Path("/home/swe/repo"), workers=4) as s:
        s.run(
            target=target, test_dir=test_dir,
            per_mutant_timeout=30, wall_clock_timeout=600,
            operators=["core/ReplaceAndWithOr"])
        summary = _container_summary(engine, target)

    assert (summary["detected"], summary["survived"]) == (3, 1)

    cfg = (wz / "cr" / "cosmic-ray.toml").read_text(encoding="utf-8")
    assert "[cosmic-ray.distributor]" in cfg

    spec = json.loads((wz / "cr" / "spec.json").read_text(encoding="utf-8"))
    assert spec["use_http"] is False


@pytest.mark.parametrize("restored", [True, False])
def test_a_launcher_that_ended_first_has_the_container_target_restored(
    monkeypatch, tmp_path, restored,
) -> None:
    """A launcher that ended before writing the local distributor's target
    back leaves the original's copy beside the session, and the host
    restores the target with the launcher's restore command. A copy that
    stays fails the run, naming it."""
    wz = _work_dir(tmp_path)
    session = wz / "cr" / "cr-session.sqlite"
    copy = Path(str(session) + ".original")

    def fake_launcher(argv, cwd):
        if "--restore" in argv:
            if restored:
                copy.unlink()
            return 0 if restored else 1
        _make_fake_session_db(session, catalogue_size=1, outcomes={"SURVIVED": 1})
        Path(str(session) + ".runner_status.json").write_text(
            json.dumps({"truncated": False, "exec_returncode": 0,
                        "ready_workers": 0}), encoding="utf-8")
        copy.write_bytes(b"x = 1\n")

    ex = _FakeContainerExecutor(fake_launcher)
    engine = _container_engine({"engine": "cosmic_ray", "cosmic_ray_catalogue_cache_dir": ""},
                               wz, ex)
    test_dir = wz / "tests"
    test_dir.mkdir()
    run = dict(target=Path("/home/swe/repo/pkg/mod.py"), test_dir=test_dir,
               per_mutant_timeout=30, wall_clock_timeout=600, operators=None)
    with engine.session(project_root=Path("/home/swe/repo"), workers=1) as s:
        if restored:
            s.run(**run)
        else:
            with pytest.raises(RuntimeError, match="may hold a mutant") as kept:
                s.run(**run)
            assert str(copy) in str(kept.value)
    launcher, restore = (call["argv"] for call in ex.calls)
    assert restore == [launcher[0], launcher[1], "--restore", launcher[2]]
    assert copy.exists() is not restored


def test_container_run_test_dir_under_project_root_syncs(
    monkeypatch, tmp_path,
) -> None:
    """When the test dir lives under project_root, the spec marks it for
    per-clone sync and the test-command uses the relative path."""
    wz = _work_dir(tmp_path)
    engine_config = {
        "engine": "cosmic_ray",
        "cosmic_ray_catalogue_cache_dir": "",
    }

    def fake_launcher(argv, cwd):
        session = wz / "cr" / "cr-session.sqlite"
        _make_fake_session_db(
            session, catalogue_size=2, outcomes={"KILLED": 2})
        Path(str(session) + ".runner_status.json").write_text(
            json.dumps({"truncated": False}), encoding="utf-8")

    ex = _FakeContainerExecutor(fake_launcher)
    engine = _container_engine(engine_config, wz, ex)

    with engine.session(project_root=Path("/home/swe/repo"), workers=4) as s:
        s.run(
            target=Path("/home/swe/repo/pkg/mod.py"),
            test_dir=Path("/home/swe/repo/tests"),
            per_mutant_timeout=30, wall_clock_timeout=600)

    spec = json.loads((wz / "cr" / "spec.json").read_text(encoding="utf-8"))
    assert spec["sync_tests_to_clones"] is True
    assert spec["rel_test_dir"] == "tests"
    assert spec["run_filter"] is False  # no operators passed, config unset
    cfg = (wz / "cr" / "cosmic-ray.toml").read_text(encoding="utf-8")
    # A synced suite is named through the wrapper's __CR_WORKER_CWD__ token,
    # which each worker resolves to its own clone.
    assert "-m pytest __CR_WORKER_CWD__/tests " in cfg
    # The per-mutant pytest is anchored to the suite directory, so it never scans
    # a host path above the work directory, whose failures would score as kills.
    assert "--rootdir=__CR_WORKER_CWD__/tests" in cfg


@pytest.mark.parametrize(("stage", "status", "failure"), [
    ("workers", 4, FAILED_WORKERS),
    ("isolation", 6, FAILED_WORKERS),
    ("initialization", 3, FAILED_INITIALIZATION),
    (None, 1, FAILED_SESSION),
])
def test_a_container_step_that_stops_the_launcher_fails_the_run(
        tmp_path, stage, status, failure) -> None:
    """The step the launcher names when it stops (failed_stage) is the run's
    failure kind, and a launcher exit without one is a failed session."""
    wz = _work_dir(tmp_path)

    def fake_launcher(argv, cwd):
        session = wz / "cr" / "cr-session.sqlite"
        Path(str(session) + ".runner_status.json").write_text(json.dumps(
            {"truncated": False, **({"failed_stage": stage} if stage else {})}),
            encoding="utf-8")
        return status

    engine = _container_engine({"engine": "cosmic_ray", "cosmic_ray_catalogue_cache_dir": ""},
                               wz, _FakeContainerExecutor(fake_launcher))
    with engine.session(project_root=Path("/home/swe/repo"), workers=4) as s:
        s.run(target=Path("/home/swe/repo/pkg/mod.py"),
              test_dir=Path("/home/swe/repo/tests"),
              per_mutant_timeout=30, wall_clock_timeout=600)
    assert engine._last_failure == (
        failure, f"the mutation tool's {stage or 'launcher'} failed in the container "
                 f"(launcher exit {status})")


def test_container_command_keeps_each_path_one_argument(monkeypatch, tmp_path) -> None:
    """Cosmic Ray splits the per-mutant command with ``shlex.split``. A suite
    directory whose name holds a space, with two files, gives pytest each file
    as one argument in the container's command."""
    wz = _work_dir(tmp_path)
    engine_config = {"engine": "cosmic_ray", "cosmic_ray_catalogue_cache_dir": ""}

    def fake_launcher(argv, cwd):
        session = wz / "cr" / "cr-session.sqlite"
        _make_fake_session_db(session, catalogue_size=2, outcomes={"KILLED": 2})
        Path(str(session) + ".runner_status.json").write_text(
            json.dumps({"truncated": False}), encoding="utf-8")

    engine = _container_engine(engine_config, wz, _FakeContainerExecutor(fake_launcher))
    with engine.session(project_root=Path("/home/swe/repo"), workers=4) as s:
        s.run(target=Path("/home/swe/repo/pkg/mod.py"),
              test_dir=Path("/home/swe/repo/unit tests"),
              test_files=["test_a.py", "test_b.py"],
              per_mutant_timeout=30, wall_clock_timeout=600)

    cfg = _cosmic_ray_settings((wz / "cr" / "cosmic-ray.toml").read_text(encoding="utf-8"))
    argv = shlex.split(cfg["test-command"])
    worker_test_dir = "__CR_WORKER_CWD__/unit tests"
    files = argv[argv.index("pytest") + 1:]
    assert files[:2] == [f"{worker_test_dir}/test_a.py", f"{worker_test_dir}/test_b.py"]
    assert f"--rootdir={worker_test_dir}" in argv


def test_container_run_without_a_session_ends_cleanly(monkeypatch, tmp_path) -> None:
    """A launcher that leaves no session (init failed) ends the run, not
    truncated and without a crash. There is no session to read."""
    wz = _work_dir(tmp_path)
    engine_config = {
        "engine": "cosmic_ray",
        "cosmic_ray_catalogue_cache_dir": "",
    }
    ex = _FakeContainerExecutor(lambda argv, cwd: None)  # writes nothing
    engine = _container_engine(engine_config, wz, ex)

    with engine.session(project_root=Path("/home/swe/repo"), workers=4) as s:
        res = s.run(
            target=Path("/home/swe/repo/pkg/mod.py"),
            test_dir=wz / "tests",
            per_mutant_timeout=30, wall_clock_timeout=600)
    assert res is False
    assert not engine._container_session_path().exists()


def test_container_run_cache_probe_and_persist(monkeypatch, tmp_path) -> None:
    """With the cache on, the host reads the target's bytes from the
    container for the key and stores the launcher's bare catalogue."""
    import base64
    wz = _work_dir(tmp_path)
    cache_dir = tmp_path / "cache"
    engine_config = {
        "engine": "cosmic_ray",
        "cosmic_ray_catalogue_cache_dir": str(cache_dir),
    }
    src = b"def f():\n    return 1\n"

    class CacheExec:
        def __init__(self):
            self.calls = []

        def run(self, argv, *, cwd=None, env=None, timeout=None, input=None):
            if _is_catalogue_probe(argv):
                return subprocess.CompletedProcess(
                    list(argv), 0, _catalogue_probe_stdout(), "")
            if _source_probe(argv) is not None:
                return _source_probe(argv)
            if "langadapter_origin" in argv:
                return _answer_origin(argv, env, wz)
            if any("metadata.version(name)" in str(part) for part in argv):
                # The toolchain probe, which the version profile reads.
                return subprocess.CompletedProcess(list(argv), 0, json.dumps(
                    {"implementation": "CPython", "version": "3.12.3", "tools": {}}), "")
            self.calls.append(list(argv))
            if len(argv) >= 2 and argv[1] == "-c":
                # source-bytes probe
                return subprocess.CompletedProcess(
                    list(argv), 0, base64.b64encode(src).decode(), "")
            # launcher: write session + bare-catalogue snapshot + status
            session = wz / "cr" / "cr-session.sqlite"
            _make_fake_session_db(
                session, catalogue_size=3, outcomes={"KILLED": 3})
            (wz / "cr" / "cr-session.sqlite.catalogue").write_bytes(
                session.read_bytes())
            Path(str(session) + ".runner_status.json").write_text(
                json.dumps({"truncated": False}), encoding="utf-8")
            return subprocess.CompletedProcess(list(argv), 0, "ok\n", "")

    ex = CacheExec()
    engine = _container_engine(engine_config, wz, ex)

    with engine.session(project_root=Path("/home/swe/repo"), workers=2) as s:
        s.run(
            target=Path("/home/swe/repo/pkg/mod.py"), test_dir=wz / "tests",
            per_mutant_timeout=30, wall_clock_timeout=600)
        summary = _container_summary(engine, Path("/home/swe/repo/pkg/mod.py"))

    assert summary["detected"] == 3
    # The source-bytes probe ran (a `python -c` exec) plus the launcher.
    assert any(len(a) >= 2 and a[1] == "-c" for a in ex.calls)
    persisted = list(cache_dir.glob("*.sqlite"))
    assert len(persisted) == 1


def test_container_run_without_work_dir_raises() -> None:
    """Container mode without a work directory raises instead of running
    locally."""
    engine = _container_engine({"engine": "cosmic_ray"}, "")
    with engine.session(project_root=Path("/repo"), workers=2) as s:
        with pytest.raises(RuntimeError, match="requires a work folder"):
            s.run(target=Path("/repo/m.py"), test_dir=Path("/repo/tests"),
                  per_mutant_timeout=10, wall_clock_timeout=10)


def test_container_run_truncated_status_propagates(monkeypatch, tmp_path) -> None:
    """A launcher status of truncated=True is the run's truncated flag."""
    wz = _work_dir(tmp_path)
    engine_config = {
        "engine": "cosmic_ray",
        "cosmic_ray_catalogue_cache_dir": "",
    }

    def fake_launcher(argv, cwd):
        session = wz / "cr" / "cr-session.sqlite"
        _make_fake_session_db(
            session, catalogue_size=4, outcomes={"KILLED": 2},  # 2 untested
            outputs={f"job_{i}": _record("FAILED t.py::test_a - x\n1 failed in 0.1s", failed=["t.py::test_a"],
                                         errors=[], counts={"failed": 1}) for i in range(2)})
        Path(str(session) + ".runner_status.json").write_text(
            json.dumps({"truncated": True}), encoding="utf-8")

    ex = _FakeContainerExecutor(fake_launcher)
    engine = _container_engine(engine_config, wz, ex)
    with engine.session(project_root=Path("/home/swe/repo"), workers=4) as s:
        truncated = s.run(
            target=Path("/home/swe/repo/pkg/mod.py"), test_dir=wz / "tests",
            per_mutant_timeout=30, wall_clock_timeout=600)
        summary = _container_summary(engine, Path("/home/swe/repo/pkg/mod.py"))
    assert truncated is True
    assert summary["detected"] == 2


def test_container_cache_hit_skips_init_on_second_run(monkeypatch, tmp_path) -> None:
    """First run is a cache miss (init + persist). The second run restores the
    cached catalogue host-side and tells the launcher to skip init."""
    import base64
    wz = _work_dir(tmp_path)
    cache_dir = tmp_path / "cache"
    engine_config = {
        "engine": "cosmic_ray",
        "cosmic_ray_catalogue_cache_dir": str(cache_dir),
    }
    src = b"def f():\n    return 1\n"
    run_inits: list = []

    class CacheExec:
        def run(self, argv, *, cwd=None, env=None, timeout=None, input=None):
            if _is_catalogue_probe(argv):
                return subprocess.CompletedProcess(
                    list(argv), 0, _catalogue_probe_stdout(), "")
            if _source_probe(argv) is not None:
                return _source_probe(argv)
            if len(argv) >= 2 and argv[1] == "-c":
                return subprocess.CompletedProcess(
                    list(argv), 0, base64.b64encode(src).decode(), "")
            spec = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
            run_inits.append(spec["run_init"])
            session = wz / "cr" / "cr-session.sqlite"
            if spec["run_init"]:
                # Cache miss: launcher inits the catalogue + snapshots it.
                _make_fake_session_db(
                    session, catalogue_size=2, outcomes={"KILLED": 2},
                    outputs={f"job_{i}": _record("FAILED t.py::test_a - x\n1 failed in 0.1s", failed=["t.py::test_a"],
                                                 errors=[], counts={"failed": 1})
                             for i in range(2)})
                (wz / "cr" / "cr-session.sqlite.catalogue").write_bytes(
                    session.read_bytes())
            # Cache hit: the host already restored the catalogue into session.
            Path(str(session) + ".runner_status.json").write_text(
                json.dumps({"truncated": False}), encoding="utf-8")
            return subprocess.CompletedProcess(list(argv), 0, "", "")

    engine = _container_engine(engine_config, wz, CacheExec())
    for _ in range(2):
        with engine.session(project_root=Path("/home/swe/repo"), workers=2) as s:
            s.run(
                target=Path("/home/swe/repo/pkg/mod.py"), test_dir=wz / "tests",
                per_mutant_timeout=10, wall_clock_timeout=60)
    assert run_inits == [True, False]


def test_container_unattributed_kill_verified_through_launcher(
    monkeypatch, tmp_path,
) -> None:
    """An unattributed kill runs the launcher again over a session of the
    suspects only (no init, no filter), and the new measurement replaces
    the record before the session is read."""
    import sqlite3 as _sqlite3
    wz = _work_dir(tmp_path)
    engine_config = {
        "engine": "cosmic_ray",
        "cosmic_ray_catalogue_cache_dir": "",
    }
    specs: list = []

    def fake_launcher(argv, cwd):
        spec = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        specs.append(spec)
        if len(specs) == 1:
            session = wz / "cr" / "cr-session.sqlite"
            # job_0: killed with empty output (the suspect). Job_1 survived.
            _make_fake_session_db(
                session, catalogue_size=2,
                outcomes={"KILLED": 1, "SURVIVED": 1})
            Path(str(session) + ".runner_status.json").write_text(
                json.dumps({"truncated": False}), encoding="utf-8")
        else:
            # The verification launcher: the suspect re-measures green.
            vdb = (wz / "cr" /
                   f"cr-session.sqlite.verify1_{len(specs) - 1}")
            with contextlib.closing(_sqlite3.connect(str(vdb))) as conn:
                conn.execute(
                    "INSERT INTO work_results VALUES (?, ?, ?, ?, ?)",
                    ("job_0", "NORMAL", "SURVIVED", "", _record("1 passed in 0.1s", counts={"passed": 1})))
                conn.commit()

    ex = _FakeContainerExecutor(fake_launcher)
    engine = _container_engine(engine_config, wz, ex)
    with engine.session(project_root=Path("/home/swe/repo"), workers=2) as s:
        s.run(
            target=Path("/home/swe/repo/pkg/mod.py"), test_dir=wz / "tests",
            per_mutant_timeout=10, wall_clock_timeout=60)
        summary = _container_summary(engine, Path("/home/swe/repo/pkg/mod.py"))

    assert len(ex.calls) == 2
    vspec = specs[1]
    assert vspec["run_init"] is False
    assert vspec["run_filter"] is False
    assert vspec["session_path"].endswith(".verify1_1")
    # The verify run gets its own settings file: the launcher appends a
    # distributor block, and the sweep's file already has one.
    assert vspec["config_path"].endswith("cosmic-ray-verify.toml")
    assert vspec["config_path"] != specs[0]["config_path"]
    assert (summary["detected"], summary["survived"]) == (0, 2)


@pytest.mark.parametrize(
    ("boundary", "expect_fail_fast"),
    [
        ("initial_consolidation", False),
        ("mutation_consolidation", False),
    ],
)
def test_retry_fail_fast_policy_is_boundary_scoped(
    monkeypatch, tmp_path, boundary, expect_fail_fast,
) -> None:
    """Consolidation retries retain full killer attribution."""
    import sqlite3 as _sqlite3

    wz = _work_dir(tmp_path)
    engine_config = {
        "engine": "cosmic_ray",
        "cosmic_ray_catalogue_cache_dir": "",
        "_omit_fail_fast": True,
    }
    configs: list[str] = []
    specs: list[dict] = []

    def fake_launcher(argv, cwd):
        spec = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        specs.append(spec)
        config_path = wz / "cr" / Path(spec["config_path"]).name
        configs.append(config_path.read_text(encoding="utf-8"))
        if len(configs) == 1:
            session = wz / "cr" / "cr-session.sqlite"
            _make_fake_session_db(
                session, catalogue_size=1, outcomes={"KILLED": 1},
            )
            Path(str(session) + ".runner_status.json").write_text(
                json.dumps({"truncated": False}), encoding="utf-8",
            )
        else:
            vdb = wz / "cr" / "cr-session.sqlite.verify1_1"
            with contextlib.closing(_sqlite3.connect(str(vdb))) as conn:
                conn.execute(
                    "INSERT INTO work_results VALUES (?, ?, ?, ?, ?)",
                    ("job_0", "NORMAL", "SURVIVED", "", _record("1 passed in 0.1s", counts={"passed": 1})),
                )
                conn.commit()

    ex = _FakeContainerExecutor(fake_launcher)
    engine = _container_engine(engine_config, wz, ex, _RetryEngine)
    with engine.session(project_root=Path("/home/swe/repo"), workers=2) as s:
        s.run(
            target=Path("/home/swe/repo/pkg/mod.py"),
            test_dir=wz / "tests",
            per_mutant_timeout=10,
            wall_clock_timeout=60,
        )
        summary = _container_summary(engine, Path("/home/swe/repo/pkg/mod.py"))

    assert len(configs) == 2
    assert (" -x -q " in configs[0]) is expect_fail_fast
    assert (" -x -q " in configs[1]) is expect_fail_fast
    for spec in specs:
        env_extra = spec["env_extra"]
        assert env_extra == {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", **RUN_VARIABLES}
    assert (summary["detected"], summary["survived"]) == (0, 1)


# Where the live session runs: a configured node-local directory is used or the
# run is refused, and every run logs the place, the fleet and the open-file
# limit.

def _recorder():
    lines = []

    def log(message, level=1):
        lines.append((level, message))

    return lines, log


def test_a_local_session_goes_below_the_configured_node_local_dir(tmp_path) -> None:
    node_local_dir = tmp_path / "shm" / "runs" / "task" / "sessions"
    engine = _local_engine({"cosmic_ray_local_session_dir": str(node_local_dir)})
    try:
        session_path, config_path = engine._resolve_paths()
        assert session_path.parent.parent == node_local_dir
        assert session_path.parent.name.startswith("cr_session_")
        assert config_path.parent == session_path.parent
        assert engine._session_origin == "configured cosmic_ray_local_session_dir"
    finally:
        shutil.rmtree(engine._default_session_dir, ignore_errors=True)


def test_an_unusable_node_local_dir_refuses_the_local_session(tmp_path) -> None:
    """An unusable configured directory refuses the session instead of falling
    back to the system temporary directory, which may be a shared file system."""
    occupied = tmp_path / "not_a_dir"
    occupied.write_text("x", encoding="utf-8")
    engine = _local_engine({"cosmic_ray_local_session_dir": str(occupied)})
    with pytest.raises(cr_engine.SessionFolderRefused,
                       match="refused rather than moved") as refused:
        engine._resolve_paths()
    assert str(occupied) in str(refused.value)
    assert getattr(engine, "_default_session_dir", None) is None


def test_a_local_run_records_where_its_session_runs(tmp_path, monkeypatch) -> None:
    from types import SimpleNamespace

    node_local_dir = tmp_path / "sessions"
    lines, log = _recorder()
    engine = _local_engine({"log_fn": log,
                            "cosmic_ray_local_session_dir": str(node_local_dir)})

    class Stop(Exception):
        pass

    def stop():
        raise Stop

    monkeypatch.setattr(engine, "_remember_requested_catalogue", lambda operators: stop())
    sess = SimpleNamespace(container_mode=False, project_root=tmp_path, env={},
                           _run_bases=set(), _run_owner="token")
    try:
        with pytest.raises(Stop):
            engine._run_in_session(sess=sess, target=tmp_path / "mod.py",
                                   test_dir=tmp_path, per_mutant_timeout=10,
                                   wall_clock_timeout=60, operators=None)
        (line,) = [message for level, message in lines
                   if "session SQLite" in message and level == 1]
        assert line.startswith(f"[CosmicRayEngine] session SQLite at {node_local_dir}")
        assert "(configured cosmic_ray_local_session_dir)" in line
        if os.name == "posix":
            assert "open-file limit" in line
    finally:
        shutil.rmtree(engine._default_session_dir, ignore_errors=True)


def _container_status_run(tmp_path, status, *, session=True):
    wz = _work_dir(tmp_path)
    lines, log = _recorder()

    def fake_launcher(argv, cwd):
        path = wz / "cr" / "cr-session.sqlite"
        if session:
            _make_fake_session_db(path, catalogue_size=2,
                                  outcomes={"KILLED": 1, "SURVIVED": 1},
                                  outputs={"job_0": _record("FAILED t.py::test_a - x\n1 failed in 0.1s",
                                                         failed=["t.py::test_a"], errors=[],
                                                         counts={"failed": 1})})
        Path(str(path) + ".runner_status.json").write_text(
            json.dumps(status), encoding="utf-8")

    engine = _container_engine({"engine": "cosmic_ray",
                                "cosmic_ray_distributor": "http",
                                "cosmic_ray_catalogue_cache_dir": "", "log_fn": log},
                               wz, _FakeContainerExecutor(fake_launcher))
    test_dir = wz / "tests"
    test_dir.mkdir()
    with engine.session(project_root=Path("/home/swe/repo"), workers=8) as s:
        result = s.run(target=Path("/home/swe/repo/pkg/mod.py"), test_dir=test_dir,
                       per_mutant_timeout=30, wall_clock_timeout=600)
    return result, lines


def test_a_container_run_records_where_its_session_ran_and_its_fleet(tmp_path) -> None:
    """The launcher's place, fleet and open-file limit enter the run's log
    as a line at the record's level."""
    _, lines = _container_status_run(tmp_path, {
        "truncated": False, "exec_returncode": 0, "ready_workers": 7, "workers": 8,
        "distributor": "http", "open_file_limit": [65536, 1048576],
        "session_dir": "/dev/shm/testuary_1/c/t/sessions/cr_localsession_x",
        "session_origin": "configured cosmic_ray_local_session_dir"})
    assert (1, "[CosmicRayEngine] sweep: session SQLite at "
               "/dev/shm/testuary_1/c/t/sessions/cr_localsession_x "
               "(configured cosmic_ray_local_session_dir), 7/8 workers ready, "
               "open-file limit: workers 65536, tests 65536 (hard 1048576)") in lines


def test_a_refused_container_session_fails_the_run(tmp_path) -> None:
    reason = ("the configured node-local session directory /dev/shm/x "
              "(cosmic_ray_local_session_dir) cannot be used: no directory can be made in "
              "it (PermissionError: Permission denied). The run is refused rather than "
              "moved to another directory")
    with pytest.raises(cr_engine.SessionFolderRefused, match="refused rather than moved"):
        _container_status_run(tmp_path, {"truncated": False, "exec_returncode": None,
                                         "ready_workers": 0, "distributor": "http",
                                         "workers": 8, "session_refused": reason},
                              session=False)


def test_a_test_run_gets_a_bounded_open_file_limit() -> None:
    """Each mutant's test run gets a soft open-file limit of 65536, bounded
    by the hard limit, or keeps a higher one: a huge soft limit slows every
    spawn that closes descriptors."""
    try:
        import resource
        unlimited = resource.RLIM_INFINITY
    except ImportError:
        unlimited = -1
    limit = cr_engine.wrapped_open_file_limit
    assert limit(1024, 4096) == 4096
    assert limit(1024, 1048576) == 65536
    assert limit(1024, unlimited) == 65536
    assert limit(100000, 1048576) == 100000
    assert limit(unlimited, unlimited) == unlimited


@pytest.mark.skipif(os.name != "posix", reason="the shell wrapper runs on POSIX hosts")
@pytest.mark.parametrize("soft, expected", [("1024", "65536"), ("70000", "70000")])
def test_the_shell_wrapper_raises_the_soft_limit_to_the_bound(soft, expected) -> None:
    text = cr_engine._per_run_wrapper_text(True)
    lines = text.split("\n")
    start = next(i for i, line in enumerate(lines) if line.startswith("_hard=$(ulimit -Hn)"))
    block = "\n".join(lines[start:start + 5])
    script = f"ulimit -H -n 1048576 2>/dev/null; ulimit -S -n {soft}\n{block}\nulimit -Sn\n"
    done = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30)
    if done.returncode != 0 or not done.stdout.strip():
        pytest.skip(f"the host refuses these limits: {done.stderr.strip()}")
    hard = subprocess.run(["sh", "-c", "ulimit -H -n 1048576 2>/dev/null; ulimit -Hn"],
                          capture_output=True, text=True, timeout=30).stdout.strip()
    want = expected if hard == "unlimited" or int(hard) >= int(expected) else hard
    assert done.stdout.strip().splitlines()[-1] == want


def test_the_session_dir_can_be_checked_before_the_run(tmp_path) -> None:
    """The public check refuses an unusable node-local session directory before
    the run, with the run's own rule and text."""
    from langadapter.python import cosmic_ray as public

    assert public.SessionFolderRefused is cr_engine.SessionFolderRefused
    public.check_local_session_dir("")
    public.check_local_session_dir(None)
    missing = tmp_path / "runs" / "task" / "sessions"
    public.check_local_session_dir(str(missing))
    assert missing.is_dir()
    occupied = tmp_path / "a_file"
    occupied.write_text("x", encoding="utf-8")
    with pytest.raises(public.SessionFolderRefused) as before:
        public.check_local_session_dir(str(occupied))
    engine = _local_engine({"cosmic_ray_local_session_dir": str(occupied)})
    with pytest.raises(public.SessionFolderRefused) as at_run:
        engine._resolve_paths()
    assert str(before.value) == str(at_run.value)
    assert "refused rather than moved" in str(before.value)


def _refuse_new_dirs(target_dir: Path):
    """Make ``target_dir`` refuse new entries while it stays writable by its
    mode: mode 0666 on POSIX (write bit, no search bit), a deny of adding
    directories for the current user on Windows. Returns the undo."""
    if os.name == "nt":
        user = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True,
                              text=True, check=True, timeout=30).stdout.strip()
        sid = user.split(",")[-1].strip('"')
        subprocess.run(["icacls", str(target_dir), "/deny", f"*{sid}:(AD)"],
                       capture_output=True, check=True, timeout=30)
        return lambda: subprocess.run(["icacls", str(target_dir), "/remove:d", f"*{sid}"],
                                      capture_output=True, check=True, timeout=30)
    if os.geteuid() == 0:
        pytest.skip("root passes every permission check")
    os.chmod(target_dir, 0o666)
    return lambda: os.chmod(target_dir, 0o755)


def test_a_session_dir_without_room_for_a_dir_is_refused(tmp_path) -> None:
    """A session directory with the write bit but no search bit is refused with
    one text by the check, the local run and the container launcher, and a
    usable directory keeps nothing from the check."""
    from langadapter.python import _cr_session_runner as runner
    from langadapter.python import cosmic_ray as public

    usable = tmp_path / "usable"
    public.check_local_session_dir(str(usable))
    assert list(usable.iterdir()) == []
    assert runner._local_session_base(str(usable))[0] == str(usable)
    assert list(usable.iterdir()) == []

    refused_dir = tmp_path / "sessions"
    refused_dir.mkdir()
    undo = _refuse_new_dirs(refused_dir)
    try:
        assert os.access(refused_dir, os.W_OK)
        with pytest.raises(public.SessionFolderRefused) as before:
            public.check_local_session_dir(str(refused_dir))
        engine = _local_engine({"cosmic_ray_local_session_dir": str(refused_dir)})
        with pytest.raises(public.SessionFolderRefused) as at_run:
            engine._resolve_paths()
        with pytest.raises(runner._SessionRefused) as in_container:
            runner._local_session_base(str(refused_dir))
    finally:
        undo()
    assert str(before.value) == str(at_run.value) == str(in_container.value)
    assert ("cannot be used: no directory can be made in it (PermissionError: "
            in str(before.value))
    assert getattr(engine, "_default_session_dir", None) is None
    assert list(refused_dir.iterdir()) == []


def test_a_mutant_has_the_same_shown_id_in_every_run(tmp_path: Path) -> None:
    """A row's mutant_id comes from what makes the mutant (module, operator,
    occurrence), so it is the same in every run, while Cosmic Ray's fresh
    job id stays the row's id within a run."""
    import contextlib
    import sqlite3

    sessions = []
    for name in ("first", "second"):
        session = tmp_path / f"{name}.sqlite"
        _make_fake_session_db(session, catalogue_size=3,
                              outcomes={"KILLED": 1, "SURVIVED": 2})
        with contextlib.closing(sqlite3.connect(str(session))) as conn:
            for table in ("work_items", "mutation_specs", "work_results"):
                conn.execute(f"UPDATE {table} SET job_id = ? || job_id", (name,))
            conn.execute("UPDATE mutation_specs SET occurrence = "
                         "CAST(substr(job_id, length(job_id)) AS INTEGER)")
            conn.commit()
        sessions.append(session)
    engine = _local_engine({})
    engine._remember_requested_catalogue(["op"])
    runs = [engine._read_mutation_run(s, tmp_path / "m.py") for s in sessions]
    shown = [[m.mutant_id for m in run.mutants] for run in runs]
    assert shown[0] == shown[1] and len(set(shown[0])) == 3
    assert all(len(i) == 32 and int(i, 16) >= 0 for i in shown[0])
    assert {m.engine_id for m in runs[0].mutants} == {"firstjob_0", "firstjob_1", "firstjob_2"}


def test_persist_cache_logs_a_failed_copy_and_leaves_no_temporary_file(
        tmp_path: Path, monkeypatch) -> None:
    """A copy into the cache that fails part-way is logged, raises nothing,
    and leaves neither the entry nor the writer's temporary file."""
    session = tmp_path / "session.sqlite"
    _make_fake_session_db(session, catalogue_size=2, outcomes={"KILLED": 2})
    cache_path = tmp_path / "cache" / "x.sqlite"

    def partial(source, destination, *args, **kwargs):
        Path(destination).write_bytes(b"half a database")
        raise OSError("disk full")

    monkeypatch.setattr(cr_engine.shutil, "copy2", partial)
    logs: list[str] = []
    cr_engine.CosmicRayEngine._persist_cache(
        session, cache_path, log_fn=lambda m, level=0: logs.append(m))
    assert not cache_path.exists()
    assert list(cache_path.parent.iterdir()) == []
    assert any("catalogue cache write failed" in m and "disk full" in m for m in logs), logs
