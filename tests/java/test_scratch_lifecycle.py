"""Java scratch ownership and interrupted disposal without launching tools."""
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter import _process_tree, execution
from langadapter.java import (
    adapter as java_adapter, _jacoco, _tools as _junit, _pit, _mutation, _sources,
    _runs, _build as jvm_project,
)
from langadapter.java._build import Compilation, JavaProject, Resolution
from langadapter.mutation import MutationRequest
from langadapter.runs import RunDiagnostic, SuiteRunRequest
from langadapter.sources import SourceStatus


class MountedContainer(execution.ContainerExecutor):
    def __init__(self, zone):
        super().__init__("selected", work_dir=str(zone), container_work_dir="/workzone")
        self.calls = []
        self.wrong_marker = False

    def run(self, argv, **options):
        self.calls.append(list(argv))
        if argv[0] == "cat":
            output = "wrong marker" if self.wrong_marker else Path(argv[-1]).read_text(encoding="ascii")
        else:
            assert argv[:3] == ["rm", "-rf", "--"]
            assert Path(argv[-1]).is_relative_to(Path(self._host_work_dir))
            output = ""
        return subprocess.CompletedProcess(argv, 0, output, "")


@pytest.fixture
def scratch_adapter(tmp_path, monkeypatch):
    zone, receipts = tmp_path / "zone", tmp_path / "receipts"
    zone.mkdir()
    receipts.mkdir()
    owners = []
    create = execution.OwnedDirectory.create

    def allocate(**options):
        owned = create(**options, receipt_parent=str(receipts))
        owners.append(owned)
        return owned

    monkeypatch.setattr(execution.OwnedDirectory, "create", allocate)

    def adapter(container):
        executor = MountedContainer(zone) if container else execution.LocalExecutor()
        mode = (lb.ContainerExecution("selected", str(zone), container_work_dir="/workzone")
                if container else lb.LocalExecution(work_dir=str(zone)))
        context = lb.ProjectContext(
            root="/project" if container else str(tmp_path),
            interpreter=lb.Interpreter(path="selected-java"), execution=mode)
        return java_adapter.JavaAdapter(context, executor=executor)

    return adapter, owners, zone


@pytest.mark.parametrize("container", [False, True])
@pytest.mark.parametrize("error_type", [RuntimeError, subprocess.TimeoutExpired, KeyboardInterrupt, SystemExit])
def test_java_aborted_scratch_preserves_its_factory_receipt(
        scratch_adapter, container, error_type, recwarn):
    create, owners, zone = scratch_adapter
    adapter = create(container)
    error = subprocess.TimeoutExpired("java", 1) if error_type is subprocess.TimeoutExpired else error_type("interrupted")
    with pytest.raises(error_type) as raised:
        with adapter._scratch("run") as scratch:
            assert Path(scratch).parent == zone
            (Path(scratch) / "partial.json").write_text("partial", encoding="utf-8")
            raise error
    assert raised.value is error
    assert Path(scratch).is_dir()
    assert len(owners) == 1
    owners[0].verify()
    assert (owners[0].root / "partial.json").read_text(encoding="utf-8") == "partial"
    if container:
        assert adapter._executor.calls == [["cat", str(owners[0].marker)]]
    assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)


@pytest.mark.parametrize("stage,delivered", [
    ("compile", False), ("tools", False), ("journal", False), ("agent", False),
    ("launch", False), ("report", False), ("analysis", False), ("timeout", False),
    ("classes", False), ("empty", False), ("no_data", False), ("no_target", False),
    ("nonzero", False), ("analysis_result", False),
    ("journal_missing", False), ("journal_active", False),
    ("empty", True), ("nonzero", True), ("journal_missing", True), ("journal_active", True),
    ("launch", True), ("timeout", True),
])
def test_java_suite_failure_retains_every_owning_stage(
        scratch_adapter, monkeypatch, recwarn, stage, delivered):
    create, owners, _zone = scratch_adapter
    adapter = create(False)
    root = Path(adapter.context.root)
    target = root / "Calc.java"
    target.write_text("public class Calc {}", encoding="utf-8")
    tests = root / "suite"
    tests.mkdir()
    source = tests / "CalcTest.java"
    source.write_text("public class CalcTest {}", encoding="utf-8")
    project = JavaProject(str(root), "maven", 17, (), (), (), (), ())
    monkeypatch.setattr(adapter, "project", lambda: Resolution(lb.Outcome.COMPLETED, project))
    monkeypatch.setattr(adapter, "_jdk", lambda: SimpleNamespace(
        home="selected-jdk", version="21", tool=lambda name: "selected-" + name))

    def compile(project, jdk, suite, output, executor, **options):
        Path(output).mkdir()
        return Compilation(lb.Outcome.COMPLETED, output)

    def agent(destfile, *args, **options):
        destfile.write_bytes(b"controlled execution data")
        return "-javaagent:controlled"

    def failure(*args, **options):
        raise OSError("controlled " + stage + " failure")

    monkeypatch.setattr(java_adapter, "compile_suite", compile)
    monkeypatch.setattr(_junit, "toolset", lambda *args, **options: _junit.Toolset("fixture", ()))
    monkeypatch.setattr(_junit, "launch", lambda *args, **options: _junit.Launch(0, "controlled", ()))
    monkeypatch.setattr(_junit, "read_reports", lambda reports: [
        _junit.Case("CalcTest", "passes()", "passed", 0.01, "")])
    monkeypatch.setattr(_jacoco, "agent_argument", agent)
    if stage.startswith("journal_"):
        monkeypatch.setattr(_runs, "prepare_journal", lambda *args: Compilation(
            lb.Outcome.COMPLETED, "controlled-journal"))
        observation = (_runs.JournalObservation(issue="controlled missing journal")
                       if stage == "journal_missing" else _runs.JournalObservation(
                           observed=True, diagnostics=(RunDiagnostic(
                               "active_at_end", "controlled active test",
                               native_test="CalcTest#passes()"),)))
        monkeypatch.setattr(_runs, "read_journal", lambda *args: observation)
    if stage == "compile":
        monkeypatch.setattr(java_adapter, "compile_suite", failure)
    elif stage == "tools":
        monkeypatch.setattr(_junit, "toolset", failure)
    elif stage == "journal":
        monkeypatch.setattr(_runs, "prepare_journal", lambda *args: Compilation(
            lb.Outcome.INFRASTRUCTURE_FAILURE, None, "controlled journal failure"))
    elif stage == "agent":
        monkeypatch.setattr(_jacoco, "agent_argument", failure)
    elif stage == "launch":
        monkeypatch.setattr(_junit, "launch", failure)
    elif stage == "report":
        monkeypatch.setattr(_junit, "read_reports", failure)
    elif stage == "analysis":
        monkeypatch.setattr(_jacoco, "analyze", failure)
    elif stage == "classes":
        monkeypatch.setattr(java_adapter, "_class_names", failure)
    elif stage == "empty":
        monkeypatch.setattr(_junit, "read_reports", lambda reports: [])
    elif stage == "nonzero":
        monkeypatch.setattr(_junit, "launch", lambda *args, **options: _junit.Launch(1, "controlled", ()))
    elif stage == "no_data":
        monkeypatch.setattr(_jacoco, "agent_argument", lambda *args, **options: "-javaagent:controlled")
    elif stage == "no_target":
        monkeypatch.setattr(_jacoco, "analyze", lambda *args, **options: ({}, ""))
    elif stage == "analysis_result":
        monkeypatch.setattr(_jacoco, "analyze", lambda *args, **options: (
            None, "controlled analysis failure"))
    elif stage == "timeout":
        monkeypatch.setattr(_junit, "launch", lambda *args, **options: _junit.Launch(
            None, "partial", (), timed_out=True))
    request = SuiteRunRequest(
        lb.Suite(str(tests), (source.name,)), 1, timeout_journal=stage.startswith("journal"),
        coverage_target="Calc.java" if stage in (
            "agent", "analysis", "analysis_result", "no_data", "no_target") else None)
    result = (adapter.tests.run_as_delivered(request) if delivered else adapter.tests.run_suite(request))
    assert result.outcome is (lb.Outcome.TIMED_OUT if stage == "timeout"
                              else lb.Outcome.INFRASTRUCTURE_FAILURE)
    assert result.coverage is None
    assert len(owners) == (2 if delivered else 1)
    for owned in owners:
        owned.verify()
    if delivered:
        assert (owners[0].root / source.name).read_bytes() == source.read_bytes()
    assert target.read_text(encoding="utf-8") == "public class Calc {}"
    assert source.read_text(encoding="utf-8") == "public class CalcTest {}"
    assert sum("retained after interrupted operation" in str(w.message)
               for w in recwarn.list) == len(owners)


@pytest.mark.parametrize("delivered,missing_coverage,case", [
    (False, False, "passed"), (False, False, "failed"),
    (True, False, "passed"), (True, False, "failed"),
    (False, True, "passed"), (False, True, "failed"),
])
def test_java_suite_native_outcome_and_disposal_controls(
        scratch_adapter, monkeypatch, recwarn, case, delivered, missing_coverage):
    create, owners, _zone = scratch_adapter
    adapter = create(False)
    root = Path(adapter.context.root)
    target = root / "Calc.java"
    target.write_text("public class Calc {}", encoding="utf-8")
    suite_root = root / "suite"
    suite_root.mkdir()
    source = suite_root / "CalcTest.java"
    source.write_text("public class CalcTest {}", encoding="utf-8")
    project = JavaProject(str(root), "maven", 17, (), (), (), (), ())
    monkeypatch.setattr(adapter, "project", lambda: Resolution(lb.Outcome.COMPLETED, project))
    monkeypatch.setattr(adapter, "_jdk", lambda: SimpleNamespace(
        home="selected-jdk", version="21", tool=lambda name: "selected-" + name))
    monkeypatch.setattr(java_adapter, "compile_suite", lambda *args, **options: Compilation(
        lb.Outcome.COMPLETED, str(root / "classes")))
    monkeypatch.setattr(_junit, "toolset", lambda *args, **options: _junit.Toolset("fixture", ()))
    monkeypatch.setattr(_junit, "launch", lambda *args, **options: _junit.Launch(
        0 if case == "passed" else 1, "controlled", ()))
    monkeypatch.setattr(_junit, "read_reports", lambda reports: [
        _junit.Case("CalcTest", "passes()", case, 0.01, "")])
    monkeypatch.setattr(_jacoco, "agent_argument", lambda *args, **options: "-javaagent:controlled")
    request = SuiteRunRequest(lb.Suite(str(suite_root), (source.name,)), 1,
                              coverage_target="Calc.java" if missing_coverage else None)
    run = (adapter.tests.run_as_delivered(request) if delivered else adapter.tests.run_suite(request))
    assert run.outcome is (lb.Outcome.TEST_FAILURES if case == "failed" else
                           lb.Outcome.INFRASTRUCTURE_FAILURE if missing_coverage else lb.Outcome.COMPLETED)
    assert run.counts.failed == int(case == "failed")
    assert run.counts.passed == int(case == "passed")
    assert run.coverage is None
    assert len(owners) == (2 if delivered else 1)
    retained = owners if missing_coverage else []
    for owned in owners:
        if owned in retained:
            owned.verify()
        else:
            assert not owned.root.exists() and not owned.receipt.exists()
    assert sum("retained after interrupted operation" in str(w.message)
               for w in recwarn.list) == len(retained)
    assert source.read_text(encoding="utf-8") == "public class CalcTest {}"
    assert target.read_text(encoding="utf-8") == "public class Calc {}"


@pytest.mark.parametrize("outcome", [lb.Outcome.INFRASTRUCTURE_FAILURE, lb.Outcome.TIMED_OUT, lb.Outcome.COMPLETED])
def test_project_resolution_retains_failed_owned_stage(
        scratch_adapter, monkeypatch, recwarn, outcome):
    create, owners, _zone = scratch_adapter
    adapter = create(False)
    monkeypatch.setattr(adapter, "_jdk", lambda: object())
    resolutions = []

    def resolve(root, runtime, executor, scratch, **options):
        Path(scratch, "partial-model.json").write_text("partial", encoding="utf-8")
        resolutions.append(scratch)
        return Resolution(outcome, reason="controlled resolution")

    monkeypatch.setattr(jvm_project, "resolve", resolve)
    first = adapter.project()
    assert adapter.project() is first and len(resolutions) == 1
    assert first.outcome is outcome and len(owners) == 1
    if outcome is lb.Outcome.COMPLETED:
        assert not owners[0].root.exists() and not owners[0].receipt.exists()
        assert not recwarn.list
    else:
        owners[0].verify()
        assert (owners[0].root / "partial-model.json").read_text(encoding="utf-8") == "partial"
        assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)


@pytest.fixture
def source_adapter(scratch_adapter, monkeypatch):
    create, owners, zone = scratch_adapter
    adapter = create(False)
    project = JavaProject(adapter.context.root, "maven", 17, (), (), (), (), ())
    monkeypatch.setattr(adapter, "project", lambda: Resolution(lb.Outcome.COMPLETED, project))
    monkeypatch.setattr(adapter, "_jdk", lambda: SimpleNamespace(
        home="selected-jdk", feature=21, version="21", tool=lambda name: "selected-" + name))
    monkeypatch.setattr(_sources, "_parse_helper", lambda *args, **options: zone / "parser")
    return adapter, owners


@pytest.mark.parametrize("stage", ["exit", "missing", "timeout", "valid", "syntax"])
def test_public_source_validation_retains_only_incomplete_parser_execution(
        source_adapter, monkeypatch, recwarn, stage):
    adapter, owners = source_adapter

    def parse(argv, **options):
        assert argv[0] == "selected-java" and argv[3] == "LangAdapterJavaParse"
        assert Path(argv[-1]).read_text(encoding="utf-8") == "class Example {}"
        if stage == "timeout":
            raise subprocess.TimeoutExpired(argv, options["timeout"])
        text = {"exit": "partial", "missing": "", "valid": "valid",
                "syntax": "syntax_error\t1\t2\tcontrolled syntax rejection"}[stage]
        return subprocess.CompletedProcess(argv, 1 if stage == "exit" else 0, text, "")

    monkeypatch.setattr(_process_tree, "run", parse)
    result = adapter.sources.validate_source("class Example {}")
    assert len(owners) == 1
    if stage in ("valid", "syntax"):
        assert result.status is SourceStatus.COMPLETED
        assert result.value.status == ("valid" if stage == "valid" else "syntax_error")
        assert not owners[0].root.exists() and not owners[0].receipt.exists()
        assert not recwarn.list
    else:
        assert result.status is (SourceStatus.TIMEOUT if stage == "timeout" else SourceStatus.INFRASTRUCTURE_FAILURE)
        assert result.value is None
        owners[0].verify()
        assert (owners[0].root / "0" / "Example.java").is_file()
        assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)


@pytest.mark.parametrize("operation", ["imports", "collection"])
@pytest.mark.parametrize("outcome", [lb.Outcome.INFRASTRUCTURE_FAILURE, lb.Outcome.TIMED_OUT])
def test_source_recovery_preserves_containing_failed_stage(
        source_adapter, monkeypatch, recwarn, operation, outcome):
    adapter, owners = source_adapter
    source = "class Example {}"
    monkeypatch.setattr(_sources.JavaSources, "_refusal", lambda *args: None)
    if operation == "imports":
        monkeypatch.setattr(_sources, "compile_tests", lambda *args, **options: Compilation(
            outcome, None, "controlled source compile failure"))
        result = adapter.sources.prune_unresolvable_imports(source)
    else:
        monkeypatch.setattr(adapter, "run_suite", lambda request: java_adapter._failed_run(
            outcome, "controlled collection failure", "", None))
        result = adapter.sources.prune_collection_breakers(source, timeout_s=1)
    assert result.status == "infrastructure_failure" and result.source == source
    assert len(owners) == 1
    owners[0].verify()
    assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)


@pytest.mark.parametrize("container", [False, True])
def test_java_idle_scratch_uses_shared_verified_disposal(scratch_adapter, container):
    create, owners, _zone = scratch_adapter
    adapter = create(container)
    with adapter._scratch("run") as scratch:
        owners[0].verify()
        (Path(scratch) / "output.txt").write_text("scratch", encoding="utf-8")
    assert not owners[0].root.exists() and not owners[0].receipt.exists()
    if container:
        assert adapter._executor.calls == [
            ["cat", str(owners[0].marker)], ["cat", str(owners[0].marker)],
            ["rm", "-rf", "--", str(owners[0].root)]]


def test_java_wrong_container_mount_is_retained_before_tool_work(scratch_adapter, recwarn):
    create, owners, _zone = scratch_adapter
    adapter = create(True)
    adapter._executor.wrong_marker = True
    with pytest.raises(execution.OwnershipRefused, match="does not match its creation receipt"):
        with adapter._scratch("run"):
            pytest.fail("mismapped scratch was admitted")
    owners[0].verify()
    assert adapter._executor.calls == [["cat", str(owners[0].marker)]]
    assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)


def test_java_catalogue_catches_failure_after_scratch_retention(scratch_adapter, monkeypatch, recwarn):
    create, owners, _zone = scratch_adapter
    adapter = create(False)
    monkeypatch.setattr(adapter, "_jdk", lambda: SimpleNamespace(
        home="selected-jdk", tool=lambda name: name))
    stages = []

    def fail(*args, **options):
        stages.append(args[2])
        raise subprocess.TimeoutExpired("java", 1)

    monkeypatch.setattr(java_adapter._pit, "mutator_ids", fail)
    result = adapter.mutations.operators("pit")
    assert result.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert result.operators is None
    assert len(stages) == 1 and stages[0].is_dir()
    assert len(owners) == 1
    owners[0].verify()
    assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)


@pytest.mark.parametrize("stage", ["prepare", "compile", "launch", "read", "timeout", "complete"])
def test_public_pit_session_preserves_failed_native_stages(
        scratch_adapter, monkeypatch, recwarn, stage):
    create, owners, _zone = scratch_adapter
    adapter = create(False)
    root = Path(adapter.context.root)
    target = root / "Calc.java"
    target.write_text("public class Calc {}", encoding="utf-8")
    tests = root / "suite"
    tests.mkdir()
    (tests / "CalcTest.java").write_text("public class CalcTest {}", encoding="utf-8")
    project = JavaProject(str(root), "maven", 17, (), (), (), (), ())
    monkeypatch.setattr(adapter, "project", lambda: Resolution(lb.Outcome.COMPLETED, project))
    monkeypatch.setattr(adapter, "_jdk", lambda: SimpleNamespace(
        home="selected-jdk", version="21", tool=lambda name: "selected-" + name))
    monkeypatch.setattr(_junit, "toolset", lambda *args, **options: _junit.Toolset("fixture", ()))
    monkeypatch.setattr(_pit, "mutator_ids", lambda *args, **options: ("controlled",))
    monkeypatch.setattr(_pit, "test_order", lambda *args, **options: root / "order")
    monkeypatch.setattr(_mutation, "compile_suite", lambda *args, **options: Compilation(
        lb.Outcome.COMPLETED, str(root / "classes")))

    def fail(*args, **options):
        raise subprocess.SubprocessError("controlled PIT " + stage + " failure")

    def report(java, **options):
        path = options["scratch"] / "mutations.xml"
        path.write_text("<mutations/>" if stage != "read" else "<mutations>", encoding="utf-8")
        return _pit.Report(0, "controlled", path)

    monkeypatch.setattr(_pit, "run", report)
    if stage == "prepare":
        monkeypatch.setattr(_pit, "mutator_ids", fail)
    elif stage == "compile":
        monkeypatch.setattr(_mutation, "compile_suite", fail)
    elif stage == "launch":
        monkeypatch.setattr(_pit, "run", fail)
    elif stage == "timeout":
        monkeypatch.setattr(_pit, "run", lambda *args, **options: _pit.Report(
            None, "partial", None, timed_out=True))
    with adapter.mutations.open_session("pit") as session:
        result = session.run(MutationRequest("Calc.java", lb.Suite(str(tests), ("CalcTest.java",))))
        assert session.is_open
    assert not session.is_open
    assert result.score() is None
    assert len(owners) == 1
    if stage == "complete":
        assert result.outcome is lb.Outcome.COMPLETED and result.complete
        assert not owners[0].root.exists() and not owners[0].receipt.exists()
        assert not recwarn.list
    else:
        assert result.outcome is (lb.Outcome.TIMED_OUT if stage == "timeout"
                                  else lb.Outcome.INFRASTRUCTURE_FAILURE)
        owners[0].verify()
        assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)
    assert target.read_text(encoding="utf-8") == "public class Calc {}"
