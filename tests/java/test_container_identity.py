"""Container target containment and byte identity never consult host project files."""
import hashlib
import subprocess
from pathlib import PurePosixPath

import pytest

import langadapter as lb
from langadapter.execution import ContainerExecutor
from langadapter.outcomes import FileIdentity
from langadapter.java import _jacoco
from langadapter.java._env import JavaEnv


class IdentityContainer(ContainerExecutor):
    def __init__(self, zone):
        super().__init__("selected", work_dir=str(zone), container_work_dir="/workzone")
        self.calls = []
        self.bytes = b"package example;\r\n// \xff\x00\nclass Calc {}\r\n"
        self.canonical = {"/project": "/real/project",
                          "/project/Calc.java": "/real/project/Calc.java",
                          "/real/project/Calc.java": "/real/project/Calc.java"}
        self.path_output = None
        self.hash_output = None
        self.returncode = 0
        self.file_exists = True

    def run(self, argv, **options):
        self.calls.append((argv, options))
        if argv[0] == "realpath":
            assert argv[1:4] == ["-e", "-z", "--"]
            output = self.path_output
            if output is None:
                output = "".join(self.canonical.get(path, path) + "\0" for path in argv[4:])
        elif argv[0] == "sha256sum":
            assert argv[1:3] == ["-z", "--"]
            output = self.hash_output
            if output is None:
                output = hashlib.sha256(self.bytes).hexdigest() + f"  {argv[3]}\0"
        else:
            assert argv[:2] == ["test", "-f"]
            return subprocess.CompletedProcess(argv, 0 if self.file_exists else 1, "", "")
        return subprocess.CompletedProcess(argv, self.returncode, output, "container filesystem unavailable")


def test_container_target_resolves_against_real_container_root(tmp_path):
    container = IdentityContainer(tmp_path)
    files = JavaEnv(container)
    assert files.project_file("target", "Calc.java", "/project") == PurePosixPath("/real/project/Calc.java")
    assert container.calls[0][0] == ["test", "-f", "/project/Calc.java"]
    assert container.calls[1][0] == ["realpath", "-e", "-z", "--", "/project", "/project/Calc.java"]
    assert container.calls[2][0] == ["test", "-f", "/real/project/Calc.java"]


def test_container_hash_matches_exact_native_bytes_without_decoding(tmp_path):
    container = IdentityContainer(tmp_path)
    native = tmp_path / "Calc.java"
    native.write_bytes(container.bytes)
    assert JavaEnv(container).identity("/project/Calc.java", "/project") == FileIdentity.of(native, tmp_path)
    assert all(options == {"timeout": 60} for _, options in container.calls)


@pytest.mark.parametrize("value", ["link.java", "../outside.java", "/outside.java"])
def test_container_symlink_and_traversal_escape_is_refused_before_hash(tmp_path, value):
    container = IdentityContainer(tmp_path)
    path = str(PurePosixPath("/project") / value)
    container.canonical[path] = "/outside.java"
    with pytest.raises(lb.InvalidRequest) as error:
        JavaEnv(container).project_file("coverage_target", value, "/project")
    assert error.value.field == "coverage_target"
    assert [argv[0] for argv, _ in container.calls] == ["test", "realpath"]


def test_container_parent_traversal_is_not_normalized_before_following_links(tmp_path):
    container = IdentityContainer(tmp_path)
    path = "/project/link/../Calc.java"
    container.canonical[path] = "/outside/Calc.java"
    with pytest.raises(lb.InvalidRequest):
        JavaEnv(container).project_file("target", "link/../Calc.java", "/project")
    assert container.calls[1][0][-1] == path


@pytest.mark.parametrize("payload", ["", "/project\0", "/project\0relative\0", "/project\0/../outside\0"])
def test_invalid_canonical_path_report_is_not_accepted(tmp_path, payload):
    container = IdentityContainer(tmp_path)
    container.path_output = payload
    with pytest.raises(OSError, match="path report"):
        JavaEnv(container).identity("/project/Calc.java", "/project")
    assert len(container.calls) == 1


@pytest.mark.parametrize("payload", ["", "g" * 64 + "  /real/project/Calc.java\0",
                                      "a" * 64 + "  /wrong.java\0",
                                      "a" * 64 + "  /real/project/Calc.java\n"])
def test_invalid_container_hash_is_not_an_identity(tmp_path, payload):
    container = IdentityContainer(tmp_path)
    container.hash_output = payload
    with pytest.raises(OSError, match="hash report"):
        JavaEnv(container).identity("/project/Calc.java", "/project")


def test_failed_container_path_probe_does_not_use_host_files(tmp_path):
    container = IdentityContainer(tmp_path)
    (tmp_path / "Calc.java").write_bytes(container.bytes)
    container.returncode = 127
    with pytest.raises(OSError, match="resolve container"):
        JavaEnv(container).identity("/project/Calc.java", "/project")
    assert len(container.calls) == 1


def test_failed_container_hash_does_not_become_an_identity(tmp_path, monkeypatch):
    container = IdentityContainer(tmp_path)
    run = container.run

    def failed_hash(argv, **options):
        if argv[0] == "sha256sum":
            return subprocess.CompletedProcess(argv, 1, "", "cannot read")
        return run(argv, **options)

    monkeypatch.setattr(container, "run", failed_hash)
    with pytest.raises(OSError, match="cannot hash container"):
        JavaEnv(container).identity("/project/Calc.java", "/project")


def test_container_file_name_with_line_break_does_not_split_hash_report(tmp_path):
    container = IdentityContainer(tmp_path)
    container.canonical["/project/Line\nbreak.java"] = "/real/project/Line\nbreak.java"
    identity = JavaEnv(container).identity("/project/Line\nbreak.java", "/project")
    assert identity.path == "Line\nbreak.java"
    assert identity.sha256 == hashlib.sha256(container.bytes).hexdigest()


def test_missing_container_file_is_a_request_error(tmp_path):
    container = IdentityContainer(tmp_path)
    container.file_exists = False
    with pytest.raises(lb.InvalidRequest) as error:
        JavaEnv(container).project_file("target", "Calc.java", "/project")
    assert error.value.field == "target"
    assert [argv[0] for argv, _ in container.calls] == ["test"]


def test_native_target_resolution_and_identity_are_unchanged(tmp_path):
    path = tmp_path / "Calc.java"
    path.write_bytes(b"class Calc {}\r\n")
    files = JavaEnv()
    assert files.project_file("target", "Calc.java", tmp_path) == path.resolve()
    assert files.identity(path, tmp_path) == FileIdentity.of(path, tmp_path)
    with pytest.raises(lb.InvalidRequest) as error:
        files.project_file("target", "../outside.java", tmp_path)
    assert error.value.field == "target"


def test_adapter_target_identity_uses_container_bytes_and_execution(tmp_path, monkeypatch):
    container = IdentityContainer(tmp_path)
    test = tmp_path / "CalcTest.java"
    test.write_text("class CalcTest {}", encoding="utf-8")
    context = lb.ProjectContext(root="/project", interpreter=lb.Interpreter(path="/opt/jdk/bin/java"),
                                execution=lb.ContainerExecution(container="selected", image="selected-image",
                                                                work_dir=str(tmp_path)))
    adapter = lb.create("java", context)
    monkeypatch.setattr(adapter, "_executor", container)
    target = adapter._project_file("target", "Calc.java")
    identity = adapter._observation_identity(lb.Suite(str(tmp_path), (test.name,)), target)
    assert identity.target == FileIdentity("Calc.java", hashlib.sha256(container.bytes).hexdigest())
    assert identity.suite == lb.Suite(str(tmp_path), (test.name,)).identity()
    assert identity.execution.kind == "container" and identity.execution.image == "selected-image"


def test_coverage_items_use_container_target_identity(tmp_path):
    container = IdentityContainer(tmp_path)
    observation = _jacoco.observation(["5:1:2:0"], PurePosixPath("/project/Calc.java"),
                                     "/project", executor=container)
    assert observation.target == FileIdentity("Calc.java", hashlib.sha256(container.bytes).hexdigest())
    assert observation.statements == (5,) and observation.covered == (5,)
    assert [item.file for item in observation.branches] == ["Calc.java", "Calc.java"]
    assert _jacoco.observation(None, PurePosixPath("/project/Calc.java"), "/project", executor=container) is None
