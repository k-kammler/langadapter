"""Selected-executor source validation before public container admission."""
import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter import _process_tree
from langadapter.execution import ContainerExecutor, OwnedDirectory
from langadapter.java import _env as jdk, _sources, _validity
from langadapter.java.adapter import JavaAdapter
from langadapter.java._build import Resolution


SOURCE = "package demo; public class CalcTest { int value = 2; }\n"
TARGET = "package demo; public class Calc { int value = 1; }\n"


class SourceContainer(ContainerExecutor):
    def __init__(self, zone):
        super().__init__("selected", work_dir=str(zone), container_work_dir="/workzone",
                         helper_python="/control/python")
        self.calls = []
        self.verdict = "valid\n"
        self.compile_code = 0
        self.error = None
        self.target = TARGET
        self.canonical = "/project/src/Calc.java"

    def run(self, argv, **options):
        self.calls.append((list(argv), options))
        if argv[0] == "cat" and Path(argv[-1]).name == ".langadapter-owner":
            output = Path(argv[-1]).read_text(encoding="ascii")
        elif argv[:3] == ["rm", "-rf", "--"]:
            assert Path(argv[-1]).is_relative_to(Path(self._host_work_dir))
            output = ""
        elif argv == ["/selected/jdk/bin/javac", "-version"]:
            # The JDK's classification reads its release file and compiler.
            output = "javac 21.0.7\n"
        elif argv[0].endswith("/javac"):
            if self.error is not None:
                raise self.error
            output_path = Path(argv[argv.index("-d") + 1])
            source = Path(argv[-1])
            assert source.read_text(encoding="utf-8") == _sources._PARSE_HELPER
            if self.compile_code == 0:
                output_path.mkdir()
                (output_path / "LangAdapterJavaParse.class").write_bytes(b"fixture")
            return subprocess.CompletedProcess(argv, self.compile_code, "", "compiler diagnostic")
        elif argv[0].endswith("/java"):
            if self.error is not None:
                raise self.error
            assert Path(argv[2]).is_dir()
            assert Path(argv[-1]).read_text(encoding="utf-8") == SOURCE
            output = self.verdict
        elif argv[0] == "test":
            assert argv[1] == "-f"
            output = ""
        elif argv[0] == "realpath":
            output = "/project\0" + self.canonical + "\0"
        elif argv == ["cat", "/selected/jdk/release"]:
            # An operation classifies the context's JDK before it starts.
            output = 'JAVA_VERSION="21.0.7"\n'
        else:
            assert argv == ["cat", "--", self.canonical]
            output = self.target
        return subprocess.CompletedProcess(argv, 0, output, "")


@pytest.fixture
def flow(tmp_path, monkeypatch):
    zone = tmp_path / "zone"
    zone.mkdir()
    container = SourceContainer(zone)
    adapter = JavaAdapter(lb.ProjectContext(
        "/project", lb.Interpreter(path="/selected/jdk/bin/java"),
        execution=lb.ContainerExecution("selected", work_dir=str(zone), container_work_dir="/workzone",
                                        helper_python="/control/python"),
        environment={"JAVA_EXTRA": "selected", "PATH": "/selected/bin"}), executor=container)
    # Mechanism-only fixture; public admission is tested separately.
    monkeypatch.setattr(adapter, "_context_issue", lambda: "")
    monkeypatch.setattr(adapter, "_jdk", lambda: jdk._ContainerJdk("/selected/jdk", 21, "21.0.7"))
    monkeypatch.setattr(adapter, "project", lambda: Resolution(
        lb.Outcome.COMPLETED, SimpleNamespace(release_of_tests=lambda: 17)))
    owners = []
    create = OwnedDirectory.create

    def allocate(**options):
        owned = create(**options, receipt_parent=str(tmp_path))
        owners.append(owned)
        return owned

    def forbidden(*args, **kwargs):
        pytest.fail("container validation must not use a host tool or tool cache")

    monkeypatch.setattr(OwnedDirectory, "create", allocate)
    monkeypatch.setattr(_process_tree, "run", forbidden)
    monkeypatch.setattr(_sources._tools, "cache_dir", forbidden)
    return adapter, container, owners


def test_container_validation_compiles_and_runs_only_in_selected_scratch(flow, monkeypatch):
    adapter, container, owners = flow
    monkeypatch.setenv("JAVA_HOME", "host-must-not-win")
    verdict = adapter.validate_source(SOURCE)
    assert verdict.status == "valid" and verdict.interpreter == "javac 21.0.7 --release 17"
    assert verdict.source_sha256 == hashlib.sha256(SOURCE.encode()).hexdigest()
    native = [(argv, options) for argv, options in container.calls if argv[0].startswith("/selected/")]
    assert len(native) == 2
    compile_argv, compile_options = native[0]
    parse_argv, parse_options = native[1]
    assert compile_argv[:3] == ["/selected/jdk/bin/javac", "--release", "11"]
    assert parse_argv[:1] == ["/selected/jdk/bin/java"]
    assert parse_argv[3:5] == ["LangAdapterJavaParse", "17"]
    assert compile_options["timeout"] == 300 and parse_options["timeout"] == 130
    assert compile_options["env"] == parse_options["env"] == {
        "JAVA_EXTRA": "selected", "PATH": "/selected/jdk/bin:/selected/bin", "JAVA_HOME": "/selected/jdk"}
    assert all(Path(argv[-1]).is_relative_to(owners[0].root) for argv, _ in native)
    assert not owners[0].root.exists()
    assert adapter.validate_source(SOURCE) == verdict
    assert len([argv for argv, _ in container.calls if argv[0].startswith("/selected/")]) == 2


def test_container_validation_preserves_no_build_fallback(flow, monkeypatch):
    adapter, _, _ = flow
    monkeypatch.setattr(adapter, "project", lambda: Resolution(lb.Outcome.UNSUPPORTED, reason="no build"))
    verdict = adapter.validate_source(SOURCE)
    assert verdict.status == "valid"
    assert verdict.interpreter == "javac 21.0.7 --release 21 (no project build at the root)"


def test_container_validation_keeps_native_syntax_error(flow):
    adapter, container, owners = flow
    container.verdict = "error\t2\t7\texpected semicolon\n"
    verdict = adapter.validate_source(SOURCE)
    assert (verdict.status, verdict.line, verdict.column, verdict.message) == (
        "syntax_error", 2, 7, "expected semicolon")
    assert not owners[0].root.exists()


@pytest.mark.parametrize("failure", ["compile", "timeout", "unresolved", "missing_verdict"])
def test_container_validation_retains_failures_without_a_syntax_verdict(flow, failure, recwarn):
    adapter, container, owners = flow
    if failure == "compile":
        container.compile_code = 3
    elif failure == "timeout":
        container.error = subprocess.TimeoutExpired(["javac"], 300)
    elif failure == "unresolved":
        container.error = OSError("container owner is unresolved")
    else:
        container.verdict = ""
    verdict = adapter.validate_source(SOURCE)
    assert verdict.status == ("timeout" if failure == "timeout" else "infrastructure_failure")
    assert verdict.source_sha256 not in adapter._parse_verdicts
    assert owners[0].root.is_dir()
    owners[0].verify()
    assert recwarn.list
    assert not [argv for argv, _ in container.calls if argv[0] == "rm"]


@pytest.mark.parametrize("operation", ["flat", "grouped", "edit"])
@pytest.mark.parametrize("failure", [OSError, subprocess.TimeoutExpired])
def test_container_source_failure_preserves_same_unresolved_owner(flow, operation, failure, recwarn):
    adapter, container, owners = flow
    error = (failure("container command failed") if failure is OSError
             else failure(["javac"], 300))
    handle = SimpleNamespace(_langadapter_command_disposed=False)
    error._langadapter_command = handle
    container.error = error
    call = {
        "flat": lambda: adapter.validate_source(SOURCE),
        "grouped": lambda: adapter.sources.validate_source(SOURCE),
        "edit": lambda: adapter.sources.merge_sources(SOURCE, SOURCE),
    }[operation]
    with pytest.raises(failure) as caught:
        call()
    assert caught.value is error and caught.value._langadapter_command is handle
    assert owners[0].root.is_dir() and owners[0].receipt.is_file()
    owners[0].verify()
    assert not adapter._parse_verdicts


@pytest.mark.parametrize("where", ["missing", "unmapped"])
def test_container_helper_refuses_missing_or_unmapped_scratch_before_compile(flow, tmp_path, where):
    _, container, _ = flow
    scratch = None if where == "missing" else tmp_path / "outside"
    with pytest.raises(OSError, match="mounted host scratch|mounted host work folder"):
        _sources._parse_helper("/selected/javac", {}, {}, executor=container, scratch=scratch)
    assert container.calls == []
    assert not (tmp_path / "outside").exists()


def test_container_target_policy_identity_reads_the_selected_source(flow):
    adapter, container, _ = flow
    identity = adapter.target_identity("src/Calc.java")
    assert identity.project_root == "/project" and identity.source_path == "src/Calc.java"
    assert identity.module_name == "demo.Calc" and identity.target_source == TARGET
    assert identity.target_source_sha256 == hashlib.sha256(TARGET.encode()).hexdigest()
    assert container.calls[-1][0] == ["cat", "--", "/project/src/Calc.java"]
    finding = adapter.analyze_validity("class CalcTest { @Test void copied() { } }", identity)
    assert finding.status == "completed"


def test_container_target_policy_refuses_escape_before_source_read(flow):
    adapter, container, _ = flow
    container.canonical = "/foreign/Calc.java"
    with pytest.raises(lb.InvalidRequest, match="outside the project"):
        adapter.target_identity("src/Calc.java")
    assert not [argv for argv, _ in container.calls if argv[0] == "cat"]


def test_native_target_policy_fields_remain_unchanged(tmp_path):
    path = tmp_path / "Calc.java"
    path.write_text(TARGET, encoding="utf-8")
    identity = _validity.target_identity(path, tmp_path)
    assert identity.project_root == str(tmp_path) and identity.source_path == "Calc.java"
    assert identity.module_name == "demo.Calc" and identity.target_source == TARGET
    assert identity.target_source_sha256 == hashlib.sha256(TARGET.encode()).hexdigest()


def test_the_target_module_reads_the_package_as_java_does(tmp_path):
    """A package name with spaces names the module without them, as the
    other Java reads of a package do."""
    path = tmp_path / "Calc.java"
    path.write_bytes(b"package demo . calc;\n\nclass Calc {\n}\n")
    assert _validity.target_identity(path, tmp_path).module_name == "demo.calc.Calc"


def test_the_target_reads_alike_on_the_host_and_from_a_container(tmp_path):
    """The target's text, and so its hash, is UTF-8 with replaced bytes and
    universal newlines on the host as the container executor's output is
    (``execution._as_text``): a CRLF file with an invalid byte reads alike."""
    from langadapter.execution import _as_text

    data = b'package demo;\r\n\r\nclass Calc {\r\n    String s = "\xff";\r\n}\r\n'
    path = tmp_path / "Calc.java"
    path.write_bytes(data)
    assert _validity.target_identity(path, tmp_path).target_source == _as_text(data)
