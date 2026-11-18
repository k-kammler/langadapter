"""The hosts that run a project's commands: Windows and Linux confirm that
every process a command started has ended, other POSIX systems use a
process group and warn that cleanup is unconfirmed, and a host without
processes refuses before anything starts, saying why, while the source
operations stay available."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import threading

import pytest

import langadapter as lb
from langadapter import _host, _process_tree
from langadapter import _process_tree as _posix_group


@pytest.fixture
def elsewhere(monkeypatch):
    """A host without processes."""
    monkeypatch.setattr(sys, "platform", "emscripten")


@pytest.fixture
def unconfirmed(monkeypatch):
    """macOS, with the unconfirmed-cleanup warning not yet given."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(_posix_group, "_WARNED", threading.Event())


def test_windows_and_linux_confirm_cleanup_and_other_posix_systems_run_unconfirmed(monkeypatch):
    for platform, runs, confirms in (("win32", True, True), ("linux", True, True),
                                     ("darwin", True, False), ("freebsd14", True, False),
                                     ("cygwin", True, False), ("emscripten", False, False),
                                     ("wasi", False, False)):
        monkeypatch.setattr(sys, "platform", platform)
        assert (_host.runs_commands(), _host.confirms_cleanup()) == (runs, confirms), platform
        assert _posix_group.applies() is (runs and not confirms)


def test_a_context_on_macos_is_accepted_with_a_warning(tmp_path, unconfirmed):
    with pytest.warns(RuntimeWarning, match="cleanup is not confirmed"):
        lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    assert "process_group_unconfirmed" in _posix_group.warning()
    assert "darwin" in _posix_group.warning() and "Windows and Linux confirm it" in _posix_group.warning()


def test_the_warning_is_given_once_per_process(tmp_path, unconfirmed):
    with pytest.warns(RuntimeWarning):
        lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))


def test_a_context_that_runs_commands_is_refused_without_processes(tmp_path, elsewhere):
    with pytest.raises(lb.InvalidRequest) as refused:
        lb.create("python", lb.ProjectContext(
            str(tmp_path), lb.Interpreter(path=sys.executable)))
    assert refused.value.field == "execution"
    message = str(refused.value)
    assert "emscripten has no processes" in message


def test_the_refusal_says_why_and_what_works_elsewhere(elsewhere):
    message = _host.refusal()
    assert "Windows and Linux, where it supervises them" in message
    assert "every process a command started has ended" in message
    assert "other POSIX systems such as macOS in the command's own process group" in message
    assert "source operations" in message and "without an interpreter" in message


def test_the_python_setup_check_names_the_host_elsewhere(tmp_path, elsewhere):
    """A project environment found without a selected interpreter cannot be
    probed here, and the check names the host instead of doubting the file."""
    for interpreter in (tmp_path / ".venv" / "bin" / "python",
                        tmp_path / ".venv" / "Scripts" / "python.exe"):
        interpreter.parent.mkdir(parents=True, exist_ok=True)
        interpreter.write_bytes(b"")
    (tmp_path / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (tmp_path / "target.py").write_text("value = 1\n", encoding="utf-8")
    context = lb.ProjectContext(str(tmp_path), environment=lb.Environment({}, inherit=False))
    report = lb.inspect_project("python", lb.ProjectSetupRequest(
        context, ("target.py",), ("tests.run_suite",)))
    reasons = " ".join(item.reason for item in report.items)
    assert "may not be a Python interpreter" not in reasons
    assert _host.refusal() in reasons
    assert any(item.outcome is lb.Outcome.UNSUPPORTED for item in report.items)


def test_preparing_a_project_elsewhere_leaves_nothing(tmp_path, monkeypatch, elsewhere):
    scratch = tmp_path / "temp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    (tmp_path / "project").mkdir()
    with pytest.raises(lb.InvalidRequest):
        lb.prepare_project(lb.ProjectContext(
            str(tmp_path / "project"), lb.Interpreter(path=sys.executable)))
    assert list(scratch.iterdir()) == []


def test_the_source_operations_stay_available_elsewhere(tmp_path, elsewhere):
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path)))
    assert adapter.check_structure("def f():\n    return 1\n").ok


def test_nothing_starts_elsewhere(monkeypatch, elsewhere):
    def launched(*args, **kwargs):
        pytest.fail("a command started on a host without processes")

    monkeypatch.setattr(subprocess, "Popen", launched)
    with pytest.raises(OSError, match="emscripten has no processes"):
        _process_tree.run([sys.executable, "-c", "pass"], timeout=10)
