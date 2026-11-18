"""Tests for the in-container mutation launcher (``_cr_session_runner``).

The real fleet needs a container and is covered by the in-container smoke.
These tests cover the pure helpers and the fleet orchestration with the
cosmic-ray CLI stubbed out, and Cosmic Ray's real HTTP worker on the
loopback address.
"""
from __future__ import annotations

import contextlib
import http.server
import json
import os
import shlex
import signal
import socket
import sqlite3
import subprocess
import threading
from pathlib import Path

import pytest

from langadapter.python import _cr_session_runner as runner
import sys
import time
import urllib.error
import urllib.request
from langadapter.python._cr_session_runner import worker_command

#: The variable that gives a fleet's secret to its workers and its exec.
_SECRET_VARIABLE = "LANGADAPTER_" + runner.WORKER_SECRET


def _make_session_with_specs(path: Path, specs) -> None:
    """Write a minimal cosmic-ray session from (job_id, operator_name)
    ``specs``, with no work results."""
    with contextlib.closing(sqlite3.connect(str(path))) as conn:
        conn.execute("CREATE TABLE work_items (job_id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE mutation_specs (job_id TEXT PRIMARY KEY, operator_name TEXT)")
        conn.execute(
            "CREATE TABLE work_results (job_id TEXT PRIMARY KEY, worker_outcome TEXT, "
            "output TEXT, test_outcome TEXT, diff TEXT)")
        for jid, op in specs:
            conn.execute("INSERT INTO work_items VALUES (?)", (jid,))
            conn.execute("INSERT INTO mutation_specs VALUES (?, ?)", (jid, op))
        conn.commit()


def test_base_env_prepends_project_root_and_applies_extra() -> None:
    env = runner._base_env(Path("/proj/root"), {"PYTHONUTF8": "1"})
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(Path("/proj/root"))
    assert env["PYTHONUTF8"] == "1"


def test_base_env_pins_the_blas_threads_for_the_worker_fleet() -> None:
    # The container boundary strips the host env, so the launcher pins the
    # BLAS thread count for each fresh worker process.
    env = runner._base_env(Path("/proj/root"), {})
    for thread_var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                       "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        assert env[thread_var] == "1"


def test_base_env_keeps_the_callers_hash_seed(monkeypatch) -> None:
    """Mutants run with the caller's hash seed, forwarded by the container
    executor as on the host: none is pinned."""
    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    assert "PYTHONHASHSEED" not in runner._base_env(Path("/proj/root"), {})
    monkeypatch.setenv("PYTHONHASHSEED", "7")
    assert runner._base_env(Path("/proj/root"), {})["PYTHONHASHSEED"] == "7"


def test_base_env_respects_explicit_thread_override() -> None:
    env = runner._base_env(Path("/proj/root"), {"OMP_NUM_THREADS": "4"})
    assert env["OMP_NUM_THREADS"] == "4"


def test_wrapper_probe_uses_the_same_no_op_command(tmp_path: Path) -> None:
    wrapper = tmp_path / "wrapper.sh"
    assert runner._wrapper_probe_argv(wrapper) == ["sh", str(wrapper), "true"]


@pytest.mark.skipif(os.name == "nt", reason="the wrapper is a POSIX shell")
def test_wrapper_probe_names_a_path_the_runs_cannot_see(tmp_path: Path) -> None:
    """Given the suite, the probe checks it is visible where the run's
    command starts, behind the wrapper's isolation."""
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text('exec "$@"\n', encoding="utf-8")
    suite = tmp_path / "suite"
    suite.mkdir()
    seen = subprocess.run(runner._wrapper_probe_argv(wrapper, [str(suite)]),
                          capture_output=True, text=True)
    assert seen.returncode == 0, seen
    missing = tmp_path / "missing"
    hidden = subprocess.run(
        runner._wrapper_probe_argv(wrapper, [str(suite), str(missing)]),
        capture_output=True, text=True)
    assert hidden.returncode == 71
    assert hidden.stdout.strip() == f"not visible to the run: {missing}"


def test_wrapper_probe_failure_detail_includes_stderr_and_stdout() -> None:
    result = runner.subprocess.CompletedProcess(
        ["probe"], 71, stdout="secondary\n", stderr="observer failed\n",
    )

    assert runner._probe_failure_detail(result) == (
        "observer failed | secondary"
    )


def test_append_http_distributor_writes_block(tmp_path: Path) -> None:
    cfg = tmp_path / "cosmic-ray.toml"
    cfg.write_text("[cosmic-ray]\nmodule-path = 'm.py'\n", encoding="utf-8")
    runner._append_http_distributor(
        cfg, ["http://127.0.0.1:5001", "http://127.0.0.1:5002"])
    txt = cfg.read_text(encoding="utf-8")
    assert "[cosmic-ray.distributor]" in txt
    assert "name = 'http'" in txt
    assert "'http://127.0.0.1:5001'" in txt
    assert "'http://127.0.0.1:5002'" in txt
    # Appended, not overwritten.
    assert "module-path = 'm.py'" in txt


def test_main_usage_returns_2() -> None:
    assert runner.main(["_cr_session_runner.py"]) == 2


def _cosmic_ray_session(path: Path, operators) -> None:
    """A session as Cosmic Ray's own API writes it: one mutant per operator
    name, job ids j0, j1 and so on."""
    work_db = pytest.importorskip("cosmic_ray.work_db")
    from cosmic_ray.work_item import MutationSpec, WorkItem

    with work_db.use_db(str(path)) as db:
        try:
            for i, operator in enumerate(operators):
                db.add_work_item(WorkItem(job_id=f"j{i}", mutations=(
                    MutationSpec(Path("m.py"), operator, 0, (1, 0), (1, 1)),)))
        finally:
            # WorkDB.close does nothing, so its engine's pool keeps the
            # connection open, which Python 3.13 reports when it is collected.
            db._engine.dispose()


def test_apply_operator_filter_skips_only_excluded(tmp_path: Path) -> None:
    """Cosmic Ray's cr-filter-operators, run by the launcher's interpreter,
    gives mutants whose operator matches an exclude pattern a 'skipped'
    result. Others are left for exec to run."""
    db = tmp_path / "s.sqlite"
    _cosmic_ray_session(db, ["core/OpA", "core/OpB", "core/OpA"])
    cfg = tmp_path / "c.toml"
    cfg.write_text(
        "[cosmic-ray]\nmodule-path = 'm.py'\n\n"
        "[cosmic-ray.filters.operators-filter]\n"
        "exclude-operators = ['^core/OpA$']\n", encoding="utf-8")
    runner._apply_operator_filter(db, cfg, 120)
    with contextlib.closing(sqlite3.connect(str(db))) as conn:
        skipped = conn.execute(
            "SELECT job_id FROM work_results WHERE lower(worker_outcome)='skipped'").fetchall()
    assert sorted(x[0] for x in skipped) == ["j0", "j2"]


def test_apply_operator_filter_raises_when_the_filter_fails(tmp_path: Path) -> None:
    pytest.importorskip("cosmic_ray.work_db")
    db = tmp_path / "s.sqlite"
    db.write_bytes(b"not a session")
    cfg = tmp_path / "c.toml"
    cfg.write_text("[cosmic-ray]\nmodule-path = 'm.py'\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="cr-filter-operators exited 1"):
        runner._apply_operator_filter(db, cfg, 120)


def test_run_session_init_failure_returns_rc_and_writes_status(
    tmp_path: Path, monkeypatch,
) -> None:
    """A failing ``cosmic-ray init`` returns its exit code before any worker
    starts, writes the status file and leaves no node-local session dir."""
    session = tmp_path / "cr-session.sqlite"
    local = tmp_path / "localsess"
    local.mkdir()
    spec = {
        "local_session_dir": str(local),
        "cosmic_ray_command": ["/bin/false-cosmic-ray"],
        "config_path": str(tmp_path / "cosmic-ray.toml"),
        "session_path": str(session),
        "project_root": str(tmp_path),
        "clone_base": str(tmp_path / "clones"),
        "test_dir": None,
        "rel_test_dir": None,
        "sync_tests_to_clones": False,
        "workers": 2,
        "wall_clock_timeout": 60,
        "run_init": True,
        "run_filter": False,
    }

    def fake_run_cli(argv, *, cwd, env, timeout):
        return subprocess.CompletedProcess(argv, 7, "", "init blew up")

    monkeypatch.setattr(runner, "_run_cli", fake_run_cli)
    rc = runner.run_session(spec)
    assert rc == 7
    status = json.loads(
        Path(str(session) + ".runner_status.json").read_text(encoding="utf-8"))
    assert status["ready_workers"] == 0
    assert status["truncated"] is False
    assert list(local.iterdir()) == []


# ---------------------------------------------------------------------------
# Pure fleet helpers.
# ---------------------------------------------------------------------------

def test_alloc_port_sockets_returns_distinct_bound_ports() -> None:
    socks = runner._alloc_port_sockets(4)
    try:
        ports = [s.getsockname()[1] for s in socks]
        assert len(ports) == 4
        assert len(set(ports)) == 4
        assert all(p > 0 for p in ports)
    finally:
        for s in socks:
            s.close()


def test_a_worker_clone_holds_the_whole_project_git_included(tmp_path) -> None:
    """A worker clone holds the whole project, .git included, as on the
    host, so tests that read repository metadata behave the same."""
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (project / "pkg").mkdir()
    (project / "pkg" / "mod.py").write_text("X = 1\n", encoding="utf-8")
    clone = tmp_path / "clone"
    clone.mkdir()
    runner._clone_one(project, clone)
    assert (clone / ".git" / "HEAD").read_text(encoding="utf-8") == "ref: refs/heads/main\n"
    assert (clone / "pkg" / "mod.py").read_text(encoding="utf-8") == "X = 1\n"


def test_wait_for_worker_true_on_listener_false_on_dead() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert runner._wait_for_worker(port, timeout=2.0) is True
    finally:
        srv.close()
    # Nothing listens now: times out and returns False.
    assert runner._wait_for_worker(port, timeout=0.5) is False


# ---------------------------------------------------------------------------
# Fleet orchestration with a fake cosmic-ray: workers bind real loopback
# ports and exec marks mutants KILLED, so clone, spawn, readiness, exec and
# teardown run without a container.
# ---------------------------------------------------------------------------

def _is_worker(argv):
    """Whether ``argv`` starts an HTTP worker (``runner.worker_command``):
    the interpreter running the loopback program, the port last."""
    return list(argv[1:3]) == ["-c", runner.LOOPBACK_WORKER]


class _FakeWorkerProc:
    """A fake http-worker that accepts on its port when ``listen``, else
    stays unreachable so the launcher drops it."""

    def __init__(self, port: int, *, listen: bool) -> None:
        self.returncode = None
        self._stop = threading.Event()
        self._sock = None
        self._thread = None
        if listen:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("127.0.0.1", port))
            self._sock.listen(8)
            self._thread = threading.Thread(target=self._serve, daemon=True)
            self._thread.start()

    def _serve(self) -> None:
        self._sock.settimeout(0.1)
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
                conn.close()
            except OSError:
                pass

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self.returncode is None:
            self.returncode = -15

    def wait(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def kill(self) -> None:
        self.terminate()


class _FakeExecProc:
    """A fake ``cosmic-ray exec`` that marks every untested mutant KILLED,
    or never finishes when ``hang``."""

    def __init__(self, session_path: str, *, hang: bool) -> None:
        self.returncode = None
        if not hang:
            with contextlib.closing(sqlite3.connect(session_path)) as conn:
                rows = conn.execute(
                    "SELECT ms.job_id FROM mutation_specs ms LEFT JOIN work_results "
                    "wr ON wr.job_id = ms.job_id WHERE wr.job_id IS NULL").fetchall()
                conn.executemany(
                    "INSERT INTO work_results "
                    "(job_id, worker_outcome, output, test_outcome, diff) "
                    "VALUES (?, 'normal', '', 'KILLED', '')",
                    [(r[0],) for r in rows])
                conn.commit()
            self.returncode = 0

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        if self.returncode is None:
            self.returncode = -15

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = -15
        return self.returncode

    def kill(self) -> None:
        self.terminate()


def _fake_popen(session_path, *, ready_count=None, hang_exec=False,
                exec_missing=False):
    """A ``subprocess.Popen`` replacement in which every worker listens, or
    only the first ``ready_count``."""
    state = {"n": 0}

    def fake(argv, **kw):
        if _is_worker(argv):
            port = int(argv[-1])
            listen = ready_count is None or state["n"] < ready_count
            state["n"] += 1
            return _FakeWorkerProc(port, listen=listen)
        if "exec" in argv:
            if exec_missing:
                raise FileNotFoundError("cosmic-ray not found")
            # exec works on the node-local session init populated (argv[-1]).
            return _FakeExecProc(argv[-1], hang=hang_exec)
        raise AssertionError(f"unexpected Popen argv: {argv}")

    return fake


def _fake_init(specs):
    def fake(argv, *, cwd, env, timeout):
        if "init" in argv:
            _make_session_with_specs(Path(argv[3]), specs)
        return subprocess.CompletedProcess(argv, 0, "", "")
    return fake


def _fleet_spec(tmp_path: Path, *, workers, ready_timeout=5, wall_clock=30):
    proj = tmp_path / "proj"
    (proj / "pkg").mkdir(parents=True, exist_ok=True)
    (proj / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    cfg = tmp_path / "cosmic-ray.toml"
    cfg.write_text("[cosmic-ray]\nmodule-path = 'pkg/mod.py'\n", encoding="utf-8")
    # The isolation wrapper beside cosmic-ray.toml, without which the
    # launcher aborts.
    (tmp_path / "_cr_fdlimit_run.sh").write_text(
        '#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    # A private work session dir: a concurrent test reclaiming
    # cr_localsession_* in the shared /dev/shm fallback would delete it.
    localsess = tmp_path / "localsess"
    localsess.mkdir(exist_ok=True)
    return {
        "local_session_dir": str(localsess),
        "cosmic_ray_command": ["/fake/cosmic-ray"],
        "config_path": str(cfg),
        "session_path": str(tmp_path / "cr-session.sqlite"),
        "project_root": str(proj),
        "clone_base": str(tmp_path / "clones"),
        "test_dir": None, "rel_test_dir": None, "sync_tests_to_clones": False,
        "workers": workers, "ready_timeout": ready_timeout,
        "wall_clock_timeout": wall_clock, "init_timeout": 30,
        "run_init": True, "run_filter": False,
        # The instrument and session runner use the adapter's prefix.
        "env_prefix": "LANGADAPTER_",
    }


def _killed_count(session_path: Path) -> int:
    with contextlib.closing(sqlite3.connect(str(session_path))) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM work_results WHERE test_outcome='KILLED'").fetchone()[0]
    return n


def test_run_session_full_fleet_all_workers_ready(tmp_path, monkeypatch) -> None:
    spec = _fleet_spec(tmp_path, workers=3, ready_timeout=5, wall_clock=30)
    session = Path(spec["session_path"])
    specs = [("j1", "core/OpA"), ("j2", "core/OpB"), ("j3", "core/OpC")]
    monkeypatch.setattr(runner, "_run_cli", _fake_init(specs))
    monkeypatch.setattr(runner.subprocess, "Popen", _fake_popen(session))

    rc = runner.run_session(spec)

    assert rc == 0
    assert _killed_count(session) == 3
    status = json.loads(
        Path(str(session) + ".runner_status.json").read_text(encoding="utf-8"))
    assert status["ready_workers"] == 3
    assert status["truncated"] is False
    # One distributor url per ready worker.
    cfg_text = Path(spec["config_path"]).read_text(encoding="utf-8")
    assert "[cosmic-ray.distributor]" in cfg_text
    assert cfg_text.count("http://127.0.0.1:") == 3
    # Worker clones are torn down.
    assert not list((tmp_path / "clones").glob("cr_worker_*"))


def test_the_fleet_and_its_exec_get_one_secret(tmp_path, monkeypatch) -> None:
    """Each worker and the exec get the run's secret in their environment,
    and the exec sends its jobs with ``HTTP_EXEC_COSMIC_RAY``."""
    spec = _fleet_spec(tmp_path, workers=2)
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "core/OpA")]))
    fake = _fake_popen(Path(spec["session_path"]))
    started = []

    def popen(argv, **kw):
        started.append((list(argv), kw["env"].get(_SECRET_VARIABLE)))
        return fake(argv, **kw)

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    assert runner.run_session(spec) == 0
    (exec_argv, exec_secret), = [(argv, secret) for argv, secret in started if "exec" in argv]
    assert exec_argv[:4] == runner.http_exec_command(sys.executable, "LANGADAPTER_")
    assert len(started) == 3 and len(exec_secret) == 64
    assert {secret for _, secret in started} == {exec_secret}


def test_run_session_explicit_http_single_worker_keeps_clone_distributor(
    tmp_path, monkeypatch,
) -> None:
    spec = _fleet_spec(tmp_path, workers=1, ready_timeout=5, wall_clock=30)
    spec["use_http"] = True
    session = Path(spec["session_path"])
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "core/OpA")]))
    monkeypatch.setattr(runner.subprocess, "Popen", _fake_popen(session))

    assert runner.run_session(spec) == 0
    assert _killed_count(session) == 1
    status = json.loads(
        Path(str(session) + ".runner_status.json").read_text(encoding="utf-8")
    )
    assert status["ready_workers"] == 1
    config = Path(spec["config_path"]).read_text(encoding="utf-8")
    assert config.count("http://127.0.0.1:") == 1


def test_workers_share_one_tmpdir_and_the_launchers_home(tmp_path, monkeypatch) -> None:
    """Workers share one session TMPDIR and keep the launcher's HOME and
    caches, as host workers under a set TMPDIR do. The TMPDIR is the
    session's own directory under the system temp, removed at the end: not
    under the clone, whose filesystem (9p, drvfs) may lack POSIX unlink-open
    semantics so pytest's capture dies. Each run's own temporary directory
    lies inside it."""
    import tempfile as _tempfile
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    cache_vars = ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "MPLCONFIGDIR",
                  "NUMBA_CACHE_DIR")
    for var in cache_vars:
        monkeypatch.delenv(var, raising=False)
    spec = _fleet_spec(tmp_path, workers=2, ready_timeout=5, wall_clock=30)
    session = Path(spec["session_path"])
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "op"), ("j2", "op")]))
    envs = []
    real_fake = _fake_popen(session)

    def capturing(argv, **kw):
        if _is_worker(argv):
            envs.append(kw.get("env") or {})
        return real_fake(argv, **kw)

    monkeypatch.setattr(runner.subprocess, "Popen", capturing)
    rc = runner.run_session(spec)
    assert rc == 0
    assert len(envs) == 2
    sys_tmp = Path(_tempfile.gettempdir())
    run_tmp = Path(envs[0]["TMPDIR"])
    assert {env["TMPDIR"] for env in envs} == {str(run_tmp)}
    assert sys_tmp in run_tmp.parents
    assert run_tmp.parent != sys_tmp
    assert Path(spec["clone_base"]) not in run_tmp.parents
    assert not run_tmp.parent.exists()
    for env in envs:
        assert env["HOME"] == str(home)
        for var in cache_vars:
            assert var not in env, var


def test_workers_get_timeout_evidence_env_and_baseline_snapshot(tmp_path, monkeypatch) -> None:
    """With a module_path, the launcher snapshots the baseline module
    beside cosmic-ray.toml and gives each worker its own evidence dir and the
    mutation target. Without one nothing is set."""
    spec = _fleet_spec(tmp_path, workers=2, ready_timeout=5, wall_clock=30)
    spec["module_path"] = "pkg/mod.py"
    session = Path(spec["session_path"])
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "op"), ("j2", "op")]))
    envs = []
    real_fake = _fake_popen(session)

    def capturing(argv, **kw):
        if _is_worker(argv):
            envs.append(kw.get("env") or {})
            # Ordinary runs name the journal directory without arming the
            # exact-resource observer. The pytest hook creates it lazily.
            assert not Path(
                envs[-1]["LANGADAPTER_TIMEOUT_EVIDENCE_DIR"]
            ).exists()
        return real_fake(argv, **kw)

    monkeypatch.setattr(runner.subprocess, "Popen", capturing)
    assert runner.run_session(spec) == 0
    evidence_base = Path(spec["config_path"]).parent / "evidence"
    # Synced to the host at teardown. During the run the journals stream
    # to the node-local live dir under the clone base.
    assert (evidence_base / "baseline_module.txt").read_text(
        encoding="utf-8") == "x = 1\n"
    live = Path(spec["clone_base"]) / "evidence_live"
    dirs = set()
    for env in envs:
        assert env["LANGADAPTER_MUTATION_TARGET"] == "pkg/mod.py"
        d = env["LANGADAPTER_TIMEOUT_EVIDENCE_DIR"]
        assert d.startswith(str(live))
        dirs.add(d)
    assert len(dirs) == 2                          # per-worker journals

    # no module_path -> no evidence env, no snapshot
    spec2 = _fleet_spec(tmp_path / "b", workers=1, ready_timeout=5,
                        wall_clock=30)
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "op")]))
    envs2 = []
    real_fake2 = _fake_popen(Path(spec2["session_path"]))

    def capturing2(argv, **kw):
        if _is_worker(argv):
            envs2.append(kw.get("env") or {})
        return real_fake2(argv, **kw)

    monkeypatch.setattr(runner.subprocess, "Popen", capturing2)
    assert runner.run_session(spec2) == 0
    assert all("LANGADAPTER_TIMEOUT_EVIDENCE_DIR" not in e for e in envs2)
    assert not (Path(spec2["config_path"]).parent / "evidence").exists()


def test_a_verification_invocation_keeps_the_earlier_journals(tmp_path, monkeypatch) -> None:
    """With ``keep_evidence``, the journals of earlier invocations (each
    verification batch or adjudication) move into numbered directories instead
    of being replaced, so a mutant verified in an earlier batch keeps its
    evidence."""
    from langadapter.python import _mutation as TE

    spec = _fleet_spec(tmp_path, workers=2, ready_timeout=5, wall_clock=30)
    spec["module_path"] = "pkg/mod.py"
    session = Path(spec["session_path"])
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "op"), ("j2", "op")]))
    real_fake = _fake_popen(session)
    runs = []

    def journaling(argv, **kw):
        if _is_worker(argv):
            # A worker's test run journals into the directory it is given.
            evidence_dir = Path(kw["env"]["LANGADAPTER_TIMEOUT_EVIDENCE_DIR"])
            evidence_dir.mkdir(parents=True, exist_ok=True)
            runs.append(len(runs) + 1)
            (evidence_dir / "journal.jsonl").write_text(
                json.dumps({"run": runs[-1], "module_sha": f"s{runs[-1]}"}) + "\n"
                + json.dumps({"start": "t.py::test_x"}) + "\n", encoding="utf-8")
        return real_fake(argv, **kw)

    monkeypatch.setattr(runner.subprocess, "Popen", journaling)
    evidence = Path(spec["config_path"]).parent / "evidence"
    assert runner.run_session(spec) == 0
    for _ in range(2):
        session.unlink()
        assert runner.run_session({**spec, "keep_evidence": True}) == 0
    assert runs == [1, 2, 3, 4, 5, 6]
    names = {p.name for p in evidence.iterdir()}
    assert {"baseline_module.txt", "x0001", "x0002"} < names and len(names) == 5
    assert list(TE.load_all_blocks(evidence)) == [f"s{n}" for n in runs]
    # A new session's sweep starts from its own evidence only.
    session.unlink()
    assert runner.run_session(spec) == 0
    assert list(TE.load_all_blocks(evidence)) == ["s7", "s8"]


def test_local_distributor_runs_get_the_sessions_tmpdir(
    tmp_path: Path, monkeypatch,
) -> None:
    """Local distributor runs get the session's TMPDIR, as workers do: a
    directory under the launcher's temp that the session removes at its end."""
    launcher_tmp = tmp_path / "launcher-tmp"
    launcher_tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(launcher_tmp))
    monkeypatch.setattr(runner.tempfile, "tempdir", str(launcher_tmp))
    spec = _fleet_spec(tmp_path / "ordinary", workers=1, ready_timeout=5,
                       wall_clock=30)
    session = Path(spec["session_path"])
    inner = _fake_init([("j1", "op")])
    observed = {}

    def cli(argv, **kwargs):
        if argv[0] == "sh":
            observed.update(kwargs["env"])
        return inner(argv, **kwargs)

    monkeypatch.setattr(runner, "_run_cli", cli)
    monkeypatch.setattr(runner.subprocess, "Popen", _fake_popen(session))

    assert runner.run_session(spec) == 0
    run_tmp = Path(observed["TMPDIR"])
    assert run_tmp.parent.parent == launcher_tmp
    assert not run_tmp.parent.exists()


def test_run_session_does_sqlite_on_local_disk_then_copies_back(tmp_path, monkeypatch) -> None:
    """init and exec run on a node-local session, never the host
    session_path (a shared FS where concurrent SQLite writes raise disk I/O
    errors and record 0 mutants), and the result is copied back."""
    spec = _fleet_spec(tmp_path, workers=2, ready_timeout=5, wall_clock=30)
    host_session = Path(spec["session_path"])
    seen = {}

    def rec_init(argv, *, cwd, env, timeout):
        if "init" in argv:
            seen["init_session"] = argv[-1]
            _make_session_with_specs(Path(argv[-1]), [("j1", "op"), ("j2", "op")])
        return subprocess.CompletedProcess(argv, 0, "", "")

    def rec_popen(argv, **kw):
        if _is_worker(argv):
            return _FakeWorkerProc(int(argv[-1]), listen=True)
        if "exec" in argv:
            seen["exec_session"] = argv[-1]
            return _FakeExecProc(argv[-1], hang=False)
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(runner, "_run_cli", rec_init)
    monkeypatch.setattr(runner.subprocess, "Popen", rec_popen)

    rc = runner.run_session(spec)
    assert rc == 0
    # One staged path in its own mkdtemp dir under the local session base.
    assert seen["init_session"] == seen["exec_session"]
    assert Path(seen["exec_session"]) != host_session
    work = Path(seen["exec_session"])
    assert work.parent.parent == Path(spec["local_session_dir"])
    assert work.parent.name.startswith("cr_localsession_")
    # Copied back to the host path.
    assert _killed_count(host_session) == 2


def test_local_session_base_uses_the_configured_dir_or_refuses(tmp_path) -> None:
    """A configured dir is used, and made when missing. An unusable one
    refuses the run instead of falling through to /dev/shm or the system
    temp, which may be a shared file system. Without one: /dev/shm when
    present, else the system temp (None)."""
    origin = "configured cosmic_ray_local_session_dir"
    good = tmp_path / "scratch"
    good.mkdir()
    assert runner._local_session_base(str(good)) == (str(good), origin)

    missing = tmp_path / "runs" / "task" / "sessions"
    assert runner._local_session_base(str(missing)) == (str(missing), origin)
    assert missing.is_dir()

    occupied = tmp_path / "a_file"
    occupied.write_text("x", encoding="utf-8")
    with pytest.raises(runner._SessionRefused, match="refused rather than moved"):
        runner._local_session_base(str(occupied))

    assert runner._local_session_base("") in (
        ("/dev/shm", "/dev/shm"), (None, "system temporary directory"))


def test_run_session_refuses_an_unusable_configured_session_dir(tmp_path, monkeypatch) -> None:
    """The launcher records the refusal for the host and runs nothing."""
    occupied = tmp_path / "a_file"
    occupied.write_text("x", encoding="utf-8")
    spec = _fleet_spec(tmp_path, workers=2, ready_timeout=5, wall_clock=30)
    spec["local_session_dir"] = str(occupied)

    def no_cli(argv, **kw):
        raise AssertionError(f"nothing may run: {argv}")

    monkeypatch.setattr(runner, "_run_cli", no_cli)
    monkeypatch.setattr(runner.subprocess, "Popen", no_cli)
    assert runner.run_session(spec) == 5
    status = json.loads(Path(spec["session_path"] + ".runner_status.json")
                        .read_text(encoding="utf-8"))
    assert str(occupied) in status["session_refused"]
    assert status["session_dir"] is None
    assert not Path(spec["session_path"]).exists()


def test_a_failed_operator_filter_stops_the_container_run(tmp_path, monkeypatch) -> None:
    """A failing filter stops the launcher before any worker or exec
    starts, as on the host, and the status names the failure: exec would
    otherwise run every mutant of the catalogue as the requested operators."""
    spec = _fleet_spec(tmp_path, workers=2, ready_timeout=5, wall_clock=30)
    spec["run_filter"] = True
    session = Path(spec["session_path"])
    init = _fake_init([("j1", "core/OpA"), ("j2", "core/OpB")])

    def cli(argv, **kw):
        if "init" in argv:
            return init(argv, **kw)
        return subprocess.CompletedProcess(argv, 2, "", "no such table")

    def broken(session_path, config_path, timeout):
        raise sqlite3.OperationalError("no such table: mutation_specs")

    def nothing_starts(*args, **kwargs):
        pytest.fail("no worker or exec may start after a failed filter")

    monkeypatch.setattr(runner, "_run_cli", cli)
    monkeypatch.setattr(runner, "_apply_operator_filter", broken)
    monkeypatch.setattr(runner.subprocess, "Popen", nothing_starts)
    assert runner.run_session(spec) == 8
    status = json.loads(
        Path(str(session) + ".runner_status.json").read_text(encoding="utf-8"))
    assert "no such table: mutation_specs" in status["filter_failed"]
    assert status["exec_returncode"] is None
    assert list(Path(spec["local_session_dir"]).iterdir()) == []


def test_run_session_no_workers_ready_returns_4(tmp_path, monkeypatch) -> None:
    spec = _fleet_spec(tmp_path, workers=2, ready_timeout=1, wall_clock=10)
    session = Path(spec["session_path"])
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "op")]))
    monkeypatch.setattr(
        runner.subprocess, "Popen", _fake_popen(session, ready_count=0))

    rc = runner.run_session(spec)

    assert rc == 4
    status = json.loads(
        Path(str(session) + ".runner_status.json").read_text(encoding="utf-8"))
    assert status["ready_workers"] == 0
    # The host reads the step that stopped the run as its failure kind.
    assert status["failed_stage"] == "workers"
    # Aborted before exec: no distributor block.
    assert "[cosmic-ray.distributor]" not in Path(
        spec["config_path"]).read_text(encoding="utf-8")
    assert not list((tmp_path / "clones").glob("cr_worker_*"))
    assert list(Path(spec["local_session_dir"]).iterdir()) == []


def test_a_failed_copy_of_the_suite_into_a_clone_stops_the_launcher(tmp_path, monkeypatch) -> None:
    """A clone the suite cannot be copied into would measure the previous
    suite: the launcher stops before its workers start, naming the step."""
    spec = _fleet_spec(tmp_path, workers=2)
    tests = Path(spec["project_root"]) / "tests"
    tests.mkdir()
    (tests / "test_mod.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    spec.update(test_dir=str(tests), rel_test_dir="tests", sync_tests_to_clones=True)
    copytree = runner.shutil.copytree
    copies = []

    def failing(source, destination, **options):
        # The clone copies the directory first, then the sync copies it again.
        if Path(destination) == tmp_path / "clones" / "cr_worker_1" / "tests":
            copies.append(destination)
            if len(copies) == 2:
                raise OSError("disk full")
        return copytree(source, destination, **options)

    monkeypatch.setattr(runner.shutil, "copytree", failing)
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "op")]))
    monkeypatch.setattr(runner.subprocess, "Popen", _fake_popen(Path(spec["session_path"])))
    assert runner.run_session(spec) == 4
    status = json.loads(Path(spec["session_path"] + ".runner_status.json").read_text(
        encoding="utf-8"))
    assert status["failed_stage"] == "workers"
    assert not (tmp_path / "clones").exists()


def test_run_session_partial_ready_proceeds_with_healthy(tmp_path, monkeypatch) -> None:
    spec = _fleet_spec(tmp_path, workers=3, ready_timeout=1, wall_clock=30)
    session = Path(spec["session_path"])
    monkeypatch.setattr(
        runner, "_run_cli", _fake_init([("j1", "a"), ("j2", "b")]))
    # Only the first 2 of 3 workers listen. The 3rd is dropped.
    monkeypatch.setattr(
        runner.subprocess, "Popen", _fake_popen(session, ready_count=2))

    rc = runner.run_session(spec)

    assert rc == 0
    status = json.loads(
        Path(str(session) + ".runner_status.json").read_text(encoding="utf-8"))
    assert status["ready_workers"] == 2
    cfg_text = Path(spec["config_path"]).read_text(encoding="utf-8")
    assert cfg_text.count("http://127.0.0.1:") == 2
    assert _killed_count(session) == 2


def test_run_session_stops_unready_workers_before_the_run(tmp_path, monkeypatch) -> None:
    """An unready worker is stopped at once, as on the host, not left
    running beside the measured mutants until teardown."""
    spec = _fleet_spec(tmp_path, workers=3, ready_timeout=1, wall_clock=30)
    session = Path(spec["session_path"])
    monkeypatch.setattr(
        runner, "_run_cli", _fake_init([("j1", "a"), ("j2", "b")]))
    popen = _fake_popen(session, ready_count=2)
    workers, seen = [], {}

    def fake(argv, **kw):
        proc = popen(argv, **kw)
        if _is_worker(argv):
            workers.append(proc)
        elif "exec" in argv:
            seen["stopped"] = [worker.returncode is not None for worker in workers]
        return proc

    monkeypatch.setattr(runner.subprocess, "Popen", fake)
    assert runner.run_session(spec) == 0
    assert seen["stopped"] == [False, False, True]


def test_run_session_exec_wall_clock_truncation(tmp_path, monkeypatch) -> None:
    spec = _fleet_spec(tmp_path, workers=2, ready_timeout=5, wall_clock=2)
    session = Path(spec["session_path"])
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "a")]))
    monkeypatch.setattr(
        runner.subprocess, "Popen", _fake_popen(session, hang_exec=True))

    rc = runner.run_session(spec)

    assert rc == 0  # truncation is not an error. Partial results stand
    status = json.loads(
        Path(str(session) + ".runner_status.json").read_text(encoding="utf-8"))
    assert status["truncated"] is True
    assert not list((tmp_path / "clones").glob("cr_worker_*"))


def test_exec_and_workers_lead_sessions_of_their_own(tmp_path, monkeypatch) -> None:
    """exec and each worker lead their own process group, which a stop
    ends together with the test run in flight."""
    spec = _fleet_spec(tmp_path, workers=2, ready_timeout=5, wall_clock=30)
    session = Path(spec["session_path"])
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "a")]))
    inner = _fake_popen(session)
    started = []

    def capturing(argv, **kw):
        if _is_worker(argv) or "exec" in argv:
            started.append(kw.get("start_new_session"))
        return inner(argv, **kw)

    monkeypatch.setattr(runner.subprocess, "Popen", capturing)

    assert runner.run_session(spec) == 0
    assert len(started) == 3 and all(started)


def test_the_wall_clock_ends_the_exec_with_its_process_group(
        tmp_path, monkeypatch) -> None:
    spec = _fleet_spec(tmp_path, workers=1, ready_timeout=5, wall_clock=2)
    session = Path(spec["session_path"])
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "a")]))
    inner = _fake_popen(session, hang_exec=True)
    procs = {}
    signals = []

    def popen(argv, **kw):
        proc = inner(argv, **kw)
        if "exec" in argv:
            proc.pid = 4242
            procs[proc.pid] = proc
        return proc

    def killpg(pid, sig):
        signals.append((pid, sig))
        procs[pid].terminate()

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner.os, "killpg", killpg, raising=False)

    assert runner.run_session(spec) == 0
    assert (4242, runner.signal.SIGTERM) in signals


def test_a_stopped_local_exec_has_its_mutant_written_back(
        tmp_path, monkeypatch) -> None:
    """An exec the wall clock stops cannot restore the module the local
    distributor mutates in place, so the launcher writes it back."""
    spec = _fleet_spec(tmp_path, workers=1, ready_timeout=5, wall_clock=2)
    spec["module_path"] = "pkg/mod.py"
    target = Path(spec["project_root"]) / "pkg" / "mod.py"
    original = target.read_bytes()
    session = Path(spec["session_path"])
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "a")]))
    inner = _fake_popen(session, hang_exec=True)

    def popen(argv, **kw):
        if "exec" in argv:
            target.write_text("x = 2\n", encoding="utf-8")
        return inner(argv, **kw)

    monkeypatch.setattr(runner.subprocess, "Popen", popen)

    assert runner.run_session(spec) == 0
    assert target.read_bytes() == original


def test_a_sigterm_during_a_local_exec_stops_it_and_restores_the_target(
        tmp_path, monkeypatch) -> None:
    """A SIGTERM (a cancel, the host's stop) while the local distributor's
    exec holds a mutant in the module stops exec and writes the original
    back before the launcher exits. While exec runs, the original's copy lies
    beside the session for the host, and the restore removes it."""
    spec = _fleet_spec(tmp_path, workers=1, ready_timeout=5, wall_clock=30)
    spec["module_path"] = "pkg/mod.py"
    target = Path(spec["project_root"]) / "pkg" / "mod.py"
    original = target.read_bytes()
    session = Path(spec["session_path"])
    copy = runner.target_original_copy(session)
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "a")]))
    installed = []
    monkeypatch.setattr(runner.signal, "signal",
                        lambda signum, handler: installed.append((signum, handler)) or "previous")
    inner = _fake_popen(session)
    started = []

    class Signalled(_FakeExecProc):
        def poll(self):
            assert copy.read_bytes() == original
            signum, handler = installed[0]
            handler(signum, None)

    def popen(argv, **kw):
        if "exec" in argv:
            target.write_text("x = 2\n", encoding="utf-8")
            started.append(Signalled(argv[-1], hang=True))
            return started[0]
        return inner(argv, **kw)

    monkeypatch.setattr(runner.subprocess, "Popen", popen)

    with pytest.raises(SystemExit) as stopped:
        runner.run_session(spec)
    assert stopped.value.code == 128 + signal.SIGTERM
    assert target.read_bytes() == original and not copy.exists()
    assert started[0].returncode is not None
    assert installed[-1] == (signal.SIGTERM, "previous")
    status = json.loads(Path(str(session) + ".runner_status.json").read_text(encoding="utf-8"))
    assert status["failed_stage"] == "exec"


def test_the_restore_writes_the_target_back_from_its_copy(tmp_path) -> None:
    """With --restore the launcher writes the target back from the copy a
    stopped launcher left and removes it. Without a copy it does nothing."""
    spec = _fleet_spec(tmp_path, workers=1)
    spec["module_path"] = "pkg/mod.py"
    target = Path(spec["project_root"]) / "pkg" / "mod.py"
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    copy = runner.target_original_copy(Path(spec["session_path"]))
    copy.write_bytes(target.read_bytes())
    target.write_text("x = 2\n", encoding="utf-8")
    assert runner.main(["_cr_session_runner.py", "--restore", str(spec_file)]) == 0
    assert target.read_text(encoding="utf-8") == "x = 1\n" and not copy.exists()
    assert runner.main(["_cr_session_runner.py", "--restore", str(spec_file)]) == 0


def test_run_session_exec_binary_missing_returns_5(tmp_path, monkeypatch) -> None:
    spec = _fleet_spec(tmp_path, workers=2, ready_timeout=5, wall_clock=30)
    session = Path(spec["session_path"])
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "a")]))
    monkeypatch.setattr(
        runner.subprocess, "Popen", _fake_popen(session, exec_missing=True))

    rc = runner.run_session(spec)

    assert rc == 5
    # Workers are torn down although exec never started.
    assert not list((tmp_path / "clones").glob("cr_worker_*"))


def test_run_session_missing_wrapper_aborts_before_exec(tmp_path, monkeypatch) -> None:
    # Every per-mutant run invokes the isolation wrapper. Without it each
    # mutant would be an instant no-output kill, so the sweep is refused.
    spec = _fleet_spec(tmp_path, workers=1)
    (tmp_path / "_cr_fdlimit_run.sh").unlink()
    session = Path(spec["session_path"])
    monkeypatch.setattr(runner, "_run_cli", _fake_init([("j1", "a")]))
    monkeypatch.setattr(runner.subprocess, "Popen", _fake_popen(session))

    rc = runner.run_session(spec)

    assert rc == 6
    assert not list((tmp_path / "clones").glob("cr_worker_*"))


def test_run_session_probe_failure_aborts(tmp_path, monkeypatch) -> None:
    # A wrapper whose isolation fails exits nonzero on the probe. Every
    # per-mutant run would fail the same way, so the session aborts.
    spec = _fleet_spec(tmp_path, workers=1)
    session = Path(spec["session_path"])
    inner = _fake_init([("j1", "a")])

    def cli(argv, **kw):
        if argv[0] == "sh":
            return subprocess.CompletedProcess(argv, 70, "", "")
        return inner(argv, **kw)

    monkeypatch.setattr(runner, "_run_cli", cli)
    monkeypatch.setattr(runner.subprocess, "Popen", _fake_popen(session))

    rc = runner.run_session(spec)

    assert rc == 6
    assert not list((tmp_path / "clones").glob("cr_worker_*"))


def test_progress_snapshot_counts_outcomes(tmp_path) -> None:
    """The progress poller reads live counts from the session: total from
    mutation_specs, the others from work_results."""
    sess = tmp_path / "cr.sqlite"
    _make_session_with_specs(sess, [(f"j{i}", "op") for i in range(4)])
    with contextlib.closing(sqlite3.connect(str(sess))) as conn:
        conn.executemany(
            "INSERT INTO work_results (job_id, worker_outcome, output, test_outcome) "
            "VALUES (?, ?, ?, ?)",
            [("j0", "normal", "", "KILLED"), ("j1", "normal", "", "SURVIVED"),
             ("j2", "normal", "timeout", "KILLED")])
        conn.commit()
    assert runner._progress_snapshot(sess) == {
        "total": 4, "done": 3, "killed": 1, "survived": 1, "timed_out": 1}


def test_progress_snapshot_none_on_unreadable_db(tmp_path) -> None:
    """A missing or unreadable session never disturbs the exec: returns None."""
    assert runner._progress_snapshot(tmp_path / "nope.sqlite") is None


# Cosmic Ray's HTTP worker as the engine starts it: Cosmic Ray's own
# command line, listening on the loopback address only.
def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _other_address():
    """An address of this machine that is not the loopback one, or None.
    A UDP socket's connect sends nothing: it only picks the interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            address = s.getsockname()[0]
        except OSError:
            return None
    return None if address.startswith("127.") or address == "0.0.0.0" else address


def _accepts(address, port):
    try:
        with socket.create_connection((address, port), timeout=1):
            return True
    except OSError:
        return False


@contextlib.contextmanager
def _worker(tmp_path, secret):
    """The port of Cosmic Ray's HTTP worker as the engine starts it, with
    the fleet's ``secret``, once it accepts connections."""
    pytest.importorskip("cosmic_ray")
    pytest.importorskip("aiohttp")
    port = _free_port()
    worker = subprocess.Popen(worker_command(sys.executable, port, "LANGADAPTER_"),
                              cwd=str(tmp_path), env={**os.environ, _SECRET_VARIABLE: secret},
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 60
        while not _accepts("127.0.0.1", port):
            assert worker.poll() is None, worker.stderr.read().decode(errors="replace")
            assert time.monotonic() < deadline, "the worker did not start"
            time.sleep(0.2)
        yield port
    finally:
        worker.terminate()
        worker.communicate(timeout=30)  # waits and closes the stderr pipe


def _status(port, job, secret):
    """The HTTP status the worker on ``port`` answers: a POST of ``job``, or
    a GET when it is None, with ``secret`` in the request's header unless
    it is None."""
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers[runner.SECRET_HEADER] = secret
    data = None if job is None else json.dumps(job).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{port}/", data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status
    except urllib.error.HTTPError as refused:
        refused.close()  # the error holds the response's socket
        return refused.code


@pytest.mark.mutation_tool
def test_the_worker_listens_on_loopback_only(tmp_path):
    """The worker answers on 127.0.0.1 (a GET of / gets 405, only a job's
    POST is allowed) and refuses connections on any other address."""
    with _worker(tmp_path, "s3cret") as port:
        assert _status(port, None, "s3cret") == 405
        other = _other_address()
        if other is not None:
            assert not _accepts(other, port), other


@pytest.mark.mutation_tool
def test_the_worker_runs_only_the_jobs_that_carry_its_secret(tmp_path):
    """A job without the fleet's secret, or with another, gets 403 and runs
    no test command. With it, Cosmic Ray's handler runs the job's command,
    whose environment no longer holds the secret."""
    marker = tmp_path / "ran"
    write = f"import os; open('ran', 'w').write(str(os.environ.get({_SECRET_VARIABLE!r})))"
    job = {"mutations": [], "timeout": 60, "test_command": " ".join(
        shlex.quote(part) for part in (sys.executable.replace(os.sep, "/"), "-c", write))}
    with _worker(tmp_path, "s3cret") as port:
        assert _status(port, job, None) == 403
        assert _status(port, job, "other") == 403
        assert not marker.exists()
        assert _status(port, job, "s3cret") == 200
    assert marker.read_text(encoding="utf-8") == "None"


def test_a_worker_without_its_secret_stops(tmp_path):
    """A worker whose environment gives no secret stops before it listens."""
    env = {key: value for key, value in os.environ.items() if key != _SECRET_VARIABLE}
    done = subprocess.run(worker_command(sys.executable, _free_port(), "LANGADAPTER_"),
                          cwd=str(tmp_path), env=env, capture_output=True, text=True, timeout=60)
    assert (done.returncode, done.stderr.strip()) == (1, f"{_SECRET_VARIABLE} is not set")


def test_the_exec_program_sends_each_job_with_the_secret(tmp_path):
    """``HTTP_EXEC_COSMIC_RAY`` runs Cosmic Ray's command line, here a
    stand-in that sends one job, and the job carries the secret, which the
    command line's environment no longer holds."""
    pytest.importorskip("aiohttp")
    stand_in = tmp_path / "stand_in" / "cosmic_ray"
    stand_in.mkdir(parents=True)
    (stand_in / "__init__.py").write_text("", encoding="utf-8")
    (stand_in / "cli.py").write_text(
        "import asyncio, os, sys\n"
        "import aiohttp\n"
        "async def _send():\n"
        "    async with aiohttp.request('POST', sys.argv[2], json={}) as response:\n"
        "        return response.status\n"
        "def main():\n"
        "    status = asyncio.get_event_loop().run_until_complete(_send())\n"
        f"    print(sys.argv[1], status, os.environ.get({_SECRET_VARIABLE!r}))\n"
        "    return 0\n", encoding="utf-8")
    sent = []

    class Worker(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            sent.append(self.headers.get(runner.SECRET_HEADER))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Worker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        done = subprocess.run(
            [*runner.http_exec_command(sys.executable, "LANGADAPTER_"), "exec",
             f"http://127.0.0.1:{server.server_port}/"],
            cwd=str(tmp_path), capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(stand_in.parent), _SECRET_VARIABLE: "s3cret"})
    finally:
        server.shutdown()
        server.server_close()
    assert done.returncode == 0, done.stderr
    assert (done.stdout.split(), sent) == (["exec", "200", "None"], ["s3cret"])


def test_the_exec_program_waits_for_each_job_without_a_limit(tmp_path):
    """Cosmic Ray sends each job with aiohttp's default limit of five
    minutes, which a longer per-mutant timeout outlasts. ``HTTP_EXEC_COSMIC_RAY``
    sends it with a ClientTimeout of no limit, here seen by a stand-in
    aiohttp."""
    stand_in = tmp_path / "stand_in"
    (stand_in / "cosmic_ray").mkdir(parents=True)
    (stand_in / "cosmic_ray" / "__init__.py").write_text("", encoding="utf-8")
    (stand_in / "cosmic_ray" / "cli.py").write_text(
        "import aiohttp\n"
        "def main():\n"
        "    aiohttp.request('POST', 'http://127.0.0.1:1/', json={})\n"
        "    return 0\n", encoding="utf-8")
    (stand_in / "aiohttp").mkdir()
    (stand_in / "aiohttp" / "__init__.py").write_text(
        "class ClientTimeout:\n"
        "    def __init__(self, **limits):\n"
        "        self.limits = limits\n"
        "def request(method, url, **options):\n"
        "    print(sorted(options), options['timeout'].limits)\n", encoding="utf-8")
    done = subprocess.run(
        [*runner.http_exec_command(sys.executable, "LANGADAPTER_"), "exec"],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(stand_in), _SECRET_VARIABLE: "s3cret"})
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "['headers', 'json', 'timeout'] {}"
