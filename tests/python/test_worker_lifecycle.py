"""Partial native worker acquisition retains all cleanup handles."""
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from langadapter.python.cosmic_ray import CosmicRaySettings
import langadapter as lb
from langadapter.mutation import MutationCleanupError
from langadapter.python import _cosmic_ray as cr
from langadapter.python._cr_session_runner import WORKER_SECRET, worker_command
from langadapter.python.adapter import PythonAdapter


class Port:
    def __init__(self, number):
        self.number = number
        self.closed = False

    def getsockname(self):
        return ("127.0.0.1", self.number)

    def bind(self, address):
        pass

    def close(self):
        self.closed = True


def _clone_dirs(pool):
    """The clones in the pool's directory, each with its run inventory beside it."""
    return [path for path in pool.clones.iterdir() if path.is_dir()]


@pytest.fixture
def pool(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "target.py").write_text("value = 1\n", encoding="utf-8")
    clones = tmp_path / "clones"
    clones.mkdir()
    context = lb.ProjectContext(str(project), interpreter=lb.Interpreter(path=sys.executable))
    engine = cr.CosmicRayEngine({**CosmicRaySettings()._engine_config("local"), "cosmic_ray_distributor": "http"},
                                adapter=PythonAdapter(context))
    session = cr.CosmicRayEngineSession(engine, project_root=project, workers=2)
    ports = [Port(9200), Port(9201)]
    pending = iter(ports)
    processes, stopped = [], []
    monkeypatch.setattr(cr.socket, "socket", lambda *args: next(pending))
    monkeypatch.setattr(cr, "_resolve_project_tooling", lambda python: ("pytest", "cosmic-ray"))
    monkeypatch.setattr(cr, "_clone_base_dir", lambda config, root: clones)
    monkeypatch.setattr(cr, "_sweep_stale_run_directories", lambda *args, **kwargs: None)
    monkeypatch.setattr(cr, "_sweep_stale_clones", lambda *args, **kwargs: None)
    monkeypatch.setattr(engine, "_build_env", lambda *args: {})
    # The import probe's answer: nothing shadows the project's source (the
    # launcher double below answers no command).
    monkeypatch.setattr(engine, "_source_dirs", lambda project_root, env: ((), (), ()))
    monkeypatch.setattr(engine, "_wait_for_worker", lambda *args, **kwargs: True)
    monkeypatch.setattr(cr.time, "sleep", lambda seconds: None)

    def spawn(*args, **kwargs):
        proc = SimpleNamespace(pid=len(processes) + 1, cwd=kwargs["cwd"])
        proc._langadapter_command_job = SimpleNamespace(close=lambda: None)
        proc.poll = lambda: 0 if proc in stopped else None
        proc.wait = lambda timeout=None: (0 if proc in stopped else
                                         pytest.fail("worker waited before termination"))
        processes.append(proc)
        return proc

    monkeypatch.setattr(cr._process_tree, "start", spawn)
    monkeypatch.setattr(cr._process_tree, "stop_all", lambda procs, grace: stopped.extend(procs))
    monkeypatch.setattr(cr._process_tree, "stop", lambda proc, grace: stopped.append(proc))
    return SimpleNamespace(engine=engine, session=session, ports=ports,
                           processes=processes, stopped=stopped, clones=clones, spawn=spawn)


@pytest.mark.parametrize("failure", [OSError("copy failed"), KeyboardInterrupt(), SystemExit(2)])
def test_clone_failure_and_cancellation_close_ports_and_remove_partial_clones(pool, monkeypatch, failure):
    def copy(*args):
        raise failure

    monkeypatch.setattr(pool.engine, "_clone_one", copy)
    with pytest.raises(type(failure)):
        pool.session.__enter__()
    assert all(port.closed for port in pool.ports)
    assert not list(pool.clones.iterdir()) and not pool.processes
    assert not pool.session._opened


def test_second_spawn_failure_stops_first_worker_and_every_port(pool, monkeypatch):
    def spawn(*args, **kwargs):
        if pool.processes:
            raise OSError("second process failed")
        return pool.spawn(*args, **kwargs)

    monkeypatch.setattr(cr._process_tree, "start", spawn)
    with pytest.raises(OSError, match="second process"):
        pool.session.__enter__()
    assert pool.stopped == pool.processes and len(pool.processes) == 1
    assert all(port.closed for port in pool.ports) and not list(pool.clones.iterdir())


def test_each_worker_gets_the_session_secret_in_its_environment(pool, monkeypatch):
    started = []

    def spawn(command, **kwargs):
        started.append((command, kwargs["env"].get("LANGADAPTER_" + WORKER_SECRET)))
        return pool.spawn(command, **kwargs)

    monkeypatch.setattr(cr._process_tree, "start", spawn)
    pool.session.__enter__()
    pool.session.close()
    assert started == [(worker_command(sys.executable, port.number, "LANGADAPTER_"),
                        pool.session.worker_secret) for port in pool.ports]
    assert len(pool.session.worker_secret) == 64


@pytest.mark.parametrize("readiness", [False, OSError("probe failed")])
def test_readiness_failure_stops_every_acquired_worker(pool, monkeypatch, readiness):
    def probe(*args, **kwargs):
        if isinstance(readiness, BaseException):
            raise readiness
        return readiness

    monkeypatch.setattr(pool.engine, "_wait_for_worker", probe)
    with pytest.raises((RuntimeError, OSError)):
        pool.session.__enter__()
    assert all(proc in pool.stopped for proc in pool.processes)
    assert not list(pool.clones.iterdir()) and not pool.session._worker_procs


def test_second_bind_failure_tracks_the_unbound_socket_too(pool, monkeypatch):
    def refused(address):
        raise OSError("cannot bind")

    monkeypatch.setattr(pool.ports[1], "bind", refused)
    with pytest.raises(OSError, match="cannot bind"):
        pool.session.__enter__()
    assert all(port.closed for port in pool.ports)
    assert not list(pool.clones.iterdir())


def test_startup_cleanup_failure_retains_handle(pool, monkeypatch):
    monkeypatch.setattr(pool.engine, "_wait_for_worker", lambda *args, **kwargs: False)

    def refused(*args, **kwargs):
        raise OSError("stop denied")

    monkeypatch.setattr(cr._process_tree, "stop_all", refused)
    monkeypatch.setattr(cr._process_tree, "stop", refused)
    with pytest.raises(MutationCleanupError) as caught:
        pool.session.__enter__()
    assert caught.value.cleanup_handle is pool.session
    assert pool.session.use_http and not pool.session._opened
    assert len(pool.session._worker_procs) == 2 and len(pool.session._worker_clones) == 2
    assert len(_clone_dirs(pool)) == 2
    monkeypatch.setattr(cr._process_tree, "stop_all", lambda procs, grace: pool.stopped.extend(procs))
    caught.value.cleanup_handle.close()
    assert not list(pool.clones.iterdir()) and not pool.session._worker_procs


def test_containment_close_failure_retains_workers_and_clones_for_retry(pool, monkeypatch):
    pool.session.__enter__()
    original = cr._process_tree.finish
    calls = []

    def refused(proc, **kwargs):
        calls.append(proc)
        raise OSError("containment close refused")

    monkeypatch.setattr(cr._process_tree, "finish", refused)
    with pytest.raises(MutationCleanupError, match="containment") as caught:
        pool.session.close()
    assert caught.value.cleanup_handle is pool.session
    assert len(pool.session._worker_procs) == len(pool.session._worker_clones) == 2
    assert all(proc in calls for proc in pool.processes)
    assert len(_clone_dirs(pool)) == 2
    monkeypatch.setattr(cr._process_tree, "finish", original)
    caught.value.cleanup_handle.close()
    assert not pool.session._worker_procs and not list(pool.clones.iterdir())


def test_unconfirmed_disposal_retains_workers_and_clones_for_retry(pool, monkeypatch):
    pool.session.__enter__()
    original = cr._process_tree.finish
    monkeypatch.setattr(cr._process_tree, "finish", lambda *args, **kwargs: False)
    with pytest.raises(MutationCleanupError, match="unconfirmed"):
        pool.session.close()
    assert len(pool.session._worker_procs) == len(pool.session._worker_clones) == 2
    assert len(_clone_dirs(pool)) == 2
    monkeypatch.setattr(cr._process_tree, "finish", original)
    pool.session.close()
    assert not pool.session._worker_procs and not list(pool.clones.iterdir())


def test_removal_failure_retains_session_handles_until_retry(pool, monkeypatch):
    pool.session.__enter__()
    original = cr.shutil.rmtree

    def refused(path, *args, **kwargs):
        raise OSError("remove denied")

    monkeypatch.setattr(cr.shutil, "rmtree", refused)
    with pytest.raises(MutationCleanupError, match="remove") as caught:
        pool.session.close()
    assert caught.value.cleanup_handle is pool.session
    assert len(pool.session._worker_clones) == 2 and not pool.session._opened
    assert not pool.session._worker_procs
    stopped_count = len(pool.stopped)
    with pytest.raises(RuntimeError, match="outside"):
        pool.session.run(target=Path("target.py"), test_dir=Path("tests"))
    monkeypatch.setattr(cr.shutil, "rmtree", original)
    pool.session.close()
    pool.session.close()
    assert not list(pool.clones.iterdir()) and not pool.session._worker_clones
    assert len(pool.stopped) == stopped_count


def test_false_removal_success_is_not_reported_as_cleanup(pool, monkeypatch):
    pool.session.__enter__()
    original = cr.shutil.rmtree
    monkeypatch.setattr(cr.shutil, "rmtree", lambda *args, **kwargs: None)
    with pytest.raises(MutationCleanupError, match="still exists"):
        pool.session.close()
    assert len(pool.session._worker_clones) == 2
    monkeypatch.setattr(cr.shutil, "rmtree", original)
    pool.session.close()


def test_false_stop_success_retains_every_clone(pool, monkeypatch):
    pool.session.__enter__()
    monkeypatch.setattr(cr._process_tree, "stop_all", lambda procs, grace: None)
    with pytest.raises(MutationCleanupError, match="still running"):
        pool.session.close()
    assert len(_clone_dirs(pool)) == 2
    monkeypatch.setattr(cr._process_tree, "stop_all", lambda procs, grace: pool.stopped.extend(procs))
    pool.session.close()


def test_owned_session_directory_failure_is_retained(pool, monkeypatch):
    pool.session.__enter__()
    directory = pool.clones.parent / "session"
    directory.mkdir()
    pool.engine._default_session_dir = directory
    pool.session._session_dir = directory
    original = cr.shutil.rmtree

    def remove(path, *args, **kwargs):
        if path == directory:
            raise OSError("session denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(cr.shutil, "rmtree", remove)
    with pytest.raises(MutationCleanupError, match="session denied"):
        pool.session.close()
    assert pool.engine._default_session_dir == directory and directory.exists()
    monkeypatch.setattr(cr.shutil, "rmtree", original)
    pool.session.close()
    assert pool.engine._default_session_dir is None and not directory.exists()


def test_failed_source_is_restored_only_after_worker_disposal(pool, monkeypatch, tmp_path):
    from langadapter.python._mutation import SourceBackup, OwnedDirectory

    create = OwnedDirectory.create
    monkeypatch.setattr(OwnedDirectory, "create", classmethod(
        lambda cls, **options: create(parent=str(tmp_path), receipt_parent=str(tmp_path), **options)))
    pool.session.__enter__()
    target = pool.session.project_root / "target.py"
    backup = SourceBackup(target, project_root=pool.session.project_root)
    original = backup.original
    target.write_bytes(b"value = 2\n")
    pool.session._source_backup = backup
    failure = OSError("native run interrupted")
    pool.session._run_failure = failure
    pool.engine._retained_session = pool.session
    restore = backup.restore

    def restore_after_workers():
        assert not pool.session._worker_procs
        assert all(process in pool.stopped for process in pool.processes)
        assert all(process._langadapter_command_disposed for process in pool.processes)
        restore()

    monkeypatch.setattr(backup, "restore", restore_after_workers)
    pool.session.close()
    assert target.read_bytes() == original
    assert failure._langadapter_source_backup is backup.owner
    backup.owner.verify()
    assert pool.engine._retained_session is None
    backup.remove()
