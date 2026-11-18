"""The project files a request names, checked and identified where the
project runs: on this machine, or in the container through the executor."""
import base64
import hashlib
import subprocess
import sys
from pathlib import PurePosixPath

import pytest

import langadapter as lb
from langadapter.errors import InvalidRequest
from langadapter.execution import Executor
from langadapter.python.adapter import PythonAdapter
from langadapter.python._project_files import (
    file_identity, project_file, read_container_bytes,
)


class _Container(Executor):
    """A container holding ``files`` (path to bytes), which its interpreter
    checks and reads."""

    def __init__(self, files):
        self.files = files
        self.commands = []

    def run(self, argv, *, cwd=None, env=None, timeout=None, input=None):
        argv = [str(a) for a in argv]
        self.commands.append(argv)
        path = argv[-1]
        if "isfile" in argv[2]:
            return subprocess.CompletedProcess(
                argv, 0 if path in self.files else 1, "", "")
        if path not in self.files:
            return subprocess.CompletedProcess(argv, 1, "", "no such file")
        return subprocess.CompletedProcess(
            argv, 0, base64.b64encode(self.files[path]).decode(), "")


def _implementation(files):
    executor = _Container(files)
    context = lb.ProjectContext(
        root="/testbed", interpreter=lb.Interpreter(path="/venv/bin/python"),
        execution=lb.ContainerExecution(container="c", work_dir="/wz"))
    return PythonAdapter(context, executor=executor), executor


def test_a_container_file_is_checked_and_read_there():
    implementation, executor = _implementation({"/testbed/pkg/m.py": b"x = 1\n"})
    path = project_file(implementation, "target", "pkg/m.py")
    assert path == PurePosixPath("/testbed/pkg/m.py")
    assert project_file(implementation, "target", "/testbed/pkg/./m.py") == path
    assert file_identity(implementation, path) == lb.FileIdentity(
        "pkg/m.py", hashlib.sha256(b"x = 1\n").hexdigest())
    assert {c[0] for c in executor.commands} == {"/venv/bin/python"}


def test_a_container_file_outside_the_root_or_missing_is_refused():
    implementation, executor = _implementation({"/etc/passwd": b"root:x", "/testbed/m.py": b""})
    for path in ("../etc/passwd", "/etc/passwd", "/testbed/../etc/passwd",
                 "/testbed"):
        with pytest.raises(InvalidRequest) as refused:
            project_file(implementation, "coverage_target", path)
        assert refused.value.field == "coverage_target"
        assert "does not lie under the project root" in refused.value.message
    assert executor.commands == []
    with pytest.raises(InvalidRequest, match="no file at missing.py in the "
                                             "container"):
        project_file(implementation, "target", "missing.py")


def test_an_empty_container_file_has_an_identity():
    implementation, _executor = _implementation({"/testbed/pkg/__init__.py": b""})
    path = project_file(implementation, "target", "pkg/__init__.py")
    assert file_identity(implementation, path).sha256 == hashlib.sha256(b"").hexdigest()


def test_bytes_read_in_the_container():
    _implementation_adapter, executor = _implementation({"/f.py": b"hello\x00bytes",
                                         "/empty.py": b""})
    assert read_container_bytes(executor, "/py", "/f.py", flags=()) == b"hello\x00bytes"
    assert read_container_bytes(executor, "/py", "/empty.py", flags=()) == b""
    assert read_container_bytes(executor, "/py", "/missing.py", flags=()) is None


@pytest.mark.parametrize("disposed", [False, True])
def test_a_container_read_keeps_an_unconfirmed_command(disposed):
    """A read whose command may still run is raised, not read as no file."""
    from types import SimpleNamespace

    error = OSError("the read failed")
    error._langadapter_command = SimpleNamespace(_closed=True, _langadapter_command_disposed=disposed)

    def run(argv, **options):
        raise error

    executor = SimpleNamespace(run=run)
    if disposed:
        assert read_container_bytes(executor, "/py", "/f.py", flags=()) is None
    else:
        with pytest.raises(OSError) as caught:
            read_container_bytes(executor, "/py", "/f.py", flags=())
        assert caught.value is error

    class Garbled(Executor):
        def run(self, argv, **kw):
            return subprocess.CompletedProcess(argv, 0, "warning: no base64", "")

    class Gone(Executor):
        def run(self, argv, **kw):
            raise OSError("the runner is gone")

    assert read_container_bytes(Garbled(), "/py", "/f.py", flags=()) is None
    assert read_container_bytes(Gone(), "/py", "/f.py", flags=()) is None


def test_a_local_file_comes_back_resolved(tmp_path):
    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "m.py").write_text("x = 1\n", encoding="utf-8")
    implementation = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    path = project_file(implementation, "target", "pkg/../pkg/m.py")
    assert path == (root / "pkg" / "m.py").resolve()
    assert file_identity(implementation, path) == lb.FileIdentity.of(
        root / "pkg" / "m.py", root)
    with pytest.raises(InvalidRequest, match="does not lie under"):
        project_file(implementation, "target", "../outside.py")
    with pytest.raises(InvalidRequest, match="no file at pkg/none.py"):
        project_file(implementation, "target", "pkg/none.py")
