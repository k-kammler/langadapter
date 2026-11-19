"""Python's provisioning (PythonAdapter.provision_runtime): uv mocked, the
file system real."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from langadapter.adapter import LangAdapter
from langadapter.context import ProvisioningRequest
from langadapter.errors import InvalidRequest
from langadapter.python import _provision
from langadapter.python.adapter import PythonAdapter

PINS = ["pytest==8.1.1", "coverage==7.6.8", "cosmic-ray==8.4.1"]


def _fake_uv(monkeypatch, tmp_path, recorded, *, venv_exit=0, install_exit=0,
             install_timeout=False):
    """A fake uv run by the process-tree runner: `uv venv` creates the
    python file and exits with ``venv_exit``, `uv pip install` exits with
    ``install_exit`` or runs out of time. Each call records its argv, env
    and timeout."""
    uv = tmp_path / "uv.exe"
    uv.write_text("", encoding="utf-8")
    monkeypatch.setattr(_provision.shutil, "which", lambda name: str(uv))
    monkeypatch.setattr(_provision, "tool_pins", lambda version: list(PINS))
    script = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"

    def fake_run(cmd, **kw):
        recorded.append(([str(c) for c in cmd], kw.get("env"), kw.get("timeout")))
        venv = cmd[1] == "venv"
        if venv:
            python = Path(cmd[2]) / script
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("", encoding="utf-8")
        elif install_timeout:
            raise subprocess.TimeoutExpired(cmd, kw["timeout"], output="",
                                            stderr="Resolving dependencies")

        class Done:
            returncode = venv_exit if venv else install_exit
            stdout = ""
            stderr = ("No interpreter found for Python 3.99" if venv and venv_exit else
                      "No solution found: package conflict" if install_exit else "")
        return Done()

    monkeypatch.setattr(_provision._process_tree, "run", fake_run)


def _environment(python) -> Path:
    """The environment directory of the interpreter ``python``."""
    return Path(python).parent.parent


def test_equal_inputs_reuse_an_environment_and_changed_ones_make_another(monkeypatch, tmp_path):
    """An environment's directory is its name and a hash of its inputs. The same
    inputs run nothing. Changed requirements or a changed tool pin make
    another environment beside it and leave the first one's interpreter in
    place, which a run may still use."""
    recorded: list = []
    _fake_uv(monkeypatch, tmp_path, recorded)
    requirements = tmp_path / "reqs.txt"
    requirements.write_text("mpmath==1.3.0\n", encoding="utf-8")
    store = tmp_path / "venvs"
    lines: list = []
    python = PythonAdapter.provision_runtime(
        ProvisioningRequest("3.10", str(requirements)), store=store, name="demo",
        environment={"PATH": "launch"}, log=lines.append)
    first = _environment(python)
    assert Path(python).exists() and first.parent == store and first.name.startswith("demo-")
    assert recorded[0][0][1:4] == ["venv", str(first), "--python"]
    install, env, _timeout = recorded[1]
    assert install[1:3] == ["pip", "install"] and str(requirements) in install
    assert install[-3:] == PINS and env == {"PATH": "launch"}
    assert (first / _provision.MARKER).exists()
    assert lines[0].startswith("provisioning Python 3.10 at") and lines[-1].startswith(
        "environment ready")
    # The same inputs run nothing.
    recorded.clear()
    assert PythonAdapter.provision_runtime(ProvisioningRequest("3.10", str(requirements)),
                                           store=store, name="demo") == python
    assert recorded == []
    # Changed requirements make another environment, the first one stays.
    requirements.write_text("mpmath==1.2.0\n", encoding="utf-8")
    second = PythonAdapter.provision_runtime(ProvisioningRequest("3.10", str(requirements)),
                                             store=store, name="demo")
    assert recorded and recorded[0][0][1] == "venv"
    # So does a changed tool pin.
    monkeypatch.setattr(_provision, "tool_pins",
                        lambda version: [*PINS[:-1], "cosmic-ray==8.4.2"])
    third = PythonAdapter.provision_runtime(ProvisioningRequest("3.10", str(requirements)),
                                            store=store, name="demo")
    assert len({python, second, third}) == 3
    assert all(Path(made).exists() for made in (python, second, third))
    # The locks are always released.
    assert sorted(path.name for path in store.iterdir()) == sorted(
        _environment(made).name for made in (python, second, third))


def test_equal_requirements_in_two_dirs_make_two_environments(monkeypatch, tmp_path):
    """The requirements file's location is an input: equal text in two
    project directories gives two environments, each made by uv run in its
    file's directory, where a relative entry such as ``-e .`` resolves. A
    relative store stays where the caller names it."""
    recorded: list = []
    _fake_uv(monkeypatch, tmp_path, recorded)
    run, run_dirs = _provision._process_tree.run, []

    def run_in(cmd, **kw):
        run_dirs.append(kw.get("cwd"))
        return run(cmd, **kw)

    monkeypatch.setattr(_provision._process_tree, "run", run_in)
    monkeypatch.chdir(tmp_path)
    made = []
    for project in ("one", "two"):
        (tmp_path / project).mkdir()
        (tmp_path / project / "requirements.txt").write_text("-e .\n", encoding="utf-8")
        made.append(_provision.provision("3.10", f"{project}/requirements.txt",
                                         store=Path("venvs"), name="demo", log=lambda line: None))
    assert made[0] != made[1]
    assert [_environment(python).parent for python in made] == [tmp_path / "venvs"] * 2
    assert run_dirs == [str(tmp_path / "one")] * 2 + [str(tmp_path / "two")] * 2
    assert [argv[2] for argv, _env, _timeout in recorded if argv[1] == "venv"] == [
        str(_environment(python)) for python in made]


def test_the_pins_are_langadapters_for_the_requested_python():
    from langadapter.python import _pinned_tools

    assert _provision.tool_pins("3.14") == [f"{tool}=={pin}"
                                            for tool, pin in _pinned_tools("3.14").items()]


def test_a_missing_requirements_file_or_uv_names_its_setting(monkeypatch, tmp_path):
    with pytest.raises(InvalidRequest, match="points at .*missing.txt, which does not exist") as refused:
        _provision.provision("3.10", str(tmp_path / "missing.txt"), store=tmp_path, name="demo")
    assert refused.value.field == "requirements"
    monkeypatch.setattr(_provision.shutil, "which", lambda name: None)
    with pytest.raises(InvalidRequest, match="pip install uv") as refused:
        _provision.provision("3.10", None, store=tmp_path, name="demo")
    assert refused.value.field == "python_version"


def test_a_failed_install_removes_the_environment_and_its_lock(monkeypatch, tmp_path):
    _fake_uv(monkeypatch, tmp_path, [], install_exit=1)
    with pytest.raises(RuntimeError, match="package conflict"):
        _provision.provision("3.10", None, store=tmp_path / "venvs", name="demo")
    assert list((tmp_path / "venvs").iterdir()) == []


def test_a_failed_environment_creation_keeps_uvs_output(monkeypatch, tmp_path):
    """uv's own account of a failed `uv venv` reaches the error, and nothing
    is installed or left behind."""
    recorded: list = []
    _fake_uv(monkeypatch, tmp_path, recorded, venv_exit=2)
    with pytest.raises(RuntimeError, match=r"creation failed \(uv exit 2\)"
                                           r"[\s\S]*No interpreter found for Python 3\.99"):
        _provision.provision("3.99", None, store=tmp_path / "venvs", name="demo")
    assert [argv[1] for argv, _env, _timeout in recorded] == ["venv"]
    assert list((tmp_path / "venvs").iterdir()) == []


def test_the_commands_share_a_time_limit_a_waiting_provisioning_outlasts(monkeypatch, tmp_path):
    """Both uv commands run under one deadline, shorter than another
    provisioning waits for the lock. An install that runs out of it is
    stopped and leaves nothing."""
    recorded: list = []
    _fake_uv(monkeypatch, tmp_path, recorded, install_timeout=True)
    with pytest.raises(RuntimeError, match=r"install into the environment did not finish within"
                                           r"[\s\S]*Last output:\nResolving dependencies"):
        _provision.provision("3.10", None, store=tmp_path / "venvs", name="demo")
    (_venv, _env, first), (_install, _env, second) = recorded
    assert second <= first <= _provision.PROVISION_SECONDS < _provision.LOCK_WAIT_SECONDS
    assert list((tmp_path / "venvs").iterdir()) == []


def test_a_lock_another_provisioning_holds_makes_this_one_wait_and_stop(monkeypatch, tmp_path):
    """While another provisioning of the same environment holds its lock,
    this one makes nothing, waits and stops, naming the lock, which stays
    with the environment it guards."""
    recorded: list = []
    _fake_uv(monkeypatch, tmp_path, recorded)
    monkeypatch.setattr(_provision, "LOCK_WAIT_SECONDS", 0.2)
    store = tmp_path / "venvs"
    python = _provision.provision("3.10", None, store=store, name="demo", log=lambda line: None)
    environment = _environment(python)
    # Half made, as while another provisioning makes it.
    (environment / _provision.MARKER).unlink()
    lock = store / f".{environment.name}.lock"
    lock.mkdir()
    recorded.clear()
    with pytest.raises(RuntimeError, match=r"timed out waiting .* Remove the stale lock .*"
                                           + re.escape(lock.name)):
        _provision.provision("3.10", None, store=store, name="demo")
    assert recorded == [] and lock.exists() and Path(python).exists()


def test_a_language_without_provisioning_refuses_a_request(tmp_path):
    class Plain(LangAdapter):
        language = "plain"

    with pytest.raises(InvalidRequest, match="the plain adapter provisions no runtime") as refused:
        Plain.provision_runtime(ProvisioningRequest("1.0"), store=tmp_path, name="demo")
    assert refused.value.field == "runtime"


@pytest.mark.parametrize("name", ["", ".", "..", "../outside", "a/b", "a\\b", "/abs", "C:abs"])
def test_an_environment_name_that_is_no_plain_dir_name_is_refused(tmp_path, name):
    """The environment's directory lies in the store: a name that would lead
    elsewhere is refused before anything is made or removed."""
    store, outside = tmp_path / "store", tmp_path / "outside-abc"
    outside.mkdir()
    with pytest.raises(InvalidRequest) as refused:
        PythonAdapter.provision_runtime(ProvisioningRequest("3.10", None), store=store, name=name)
    assert refused.value.field == "name"
    assert outside.is_dir() and not store.exists()
