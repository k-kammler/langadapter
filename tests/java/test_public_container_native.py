"""Opt-in public Java container callers with real offline Maven project resolution."""
import os
from pathlib import Path, PurePosixPath

import pytest

import langadapter as lb
from langadapter._owned_directory import OwnershipRefused
from langadapter.java import _tools
from langadapter.mutation import Catalogue, MutationRequest
from langadapter.runs import (
    BenchmarkRequest, CollectionRequest, PerTestEvidenceRequest, SuiteRunRequest,
    WarmedSuiteBenchmarkRequest,
)

pytestmark = pytest.mark.mutation_tool


@pytest.mark.parametrize("operation", ["observations", "mutation"])
def test_public_java_container_uses_resolved_project_and_owned_lifetime(
        tmp_path, java_project, monkeypatch, operation):
    names = ("LANGADAPTER_TEST_CONTAINER", "LANGADAPTER_TEST_CONTAINER_JAVA",
             "LANGADAPTER_TEST_WORK_DIR", "LANGADAPTER_TEST_CONTAINER_HELPER_PYTHON",
             "LANGADAPTER_TEST_CONTAINER_MAVEN", "LANGADAPTER_TEST_CONTAINER_MAVEN_REPOSITORY",
             "LANGADAPTER_TEST_TOOL_CACHE")
    container, java, zone, helper, maven, repository, cache = (
        os.environ.get(name) for name in names)
    if not all((container, java, zone, helper, maven, repository, cache)):
        pytest.skip("requires a scheduled offline container with mapped project, Maven/JDK caches and control helper")

    def no_download(*args, **kwargs):
        pytest.fail("public container fixture attempted a tool download")

    monkeypatch.setattr(_tools.urllib.request, "urlopen", no_download)
    source = java_project("maven")
    relative = source.resolve().relative_to(Path(zone).resolve())
    container_root = PurePosixPath("/workzone") / PurePosixPath(relative.as_posix())
    (source / ".mvn").mkdir()
    (source / ".mvn" / "maven.config").write_text(
        f"-o\n-Dmaven.repo.local={repository}\n", encoding="utf-8")
    target = source / "src" / "main" / "java" / "demo" / "Calc.java"
    original = target.read_bytes()
    execution = lb.ContainerExecution(container, work_dir=zone,
        container_work_dir="/workzone", helper_python=helper)
    owner = lb.prepare_project(lb.ProjectContext(str(container_root), lb.Interpreter(path=java),
        execution=execution, environment={"LANGADAPTER_MAVEN": maven,
                                           "LANGADAPTER_JAVA_TOOLS": cache}),
        receipt_parent=str(tmp_path))
    receipt = Path(owner._prepared_copy.owned.receipt)
    assert receipt.is_file()
    with owner.use() as selected:
        adapter = lb.create("java", selected)
        resolution = adapter.project()
        assert resolution.outcome is lb.Outcome.COMPLETED, (resolution.reason, resolution.output)
        assert resolution.project.root == selected.root and resolution.project.build_tool == "maven"
        copied = Path(zone) / Path(PurePosixPath(selected.root).relative_to("/workzone").as_posix())
        copied_target = copied / target.relative_to(source)
        suite = lb.Suite(str(copied / "src" / "test" / "java"), ("demo/CalcTest.java",))
        target_path = "src/main/java/demo/Calc.java"
        identity = adapter.sources.target_identity(target_path)
        assert identity.completed and identity.value.module_name == "demo.Calc"
        assert identity.value.project_root == selected.root
        if operation == "mutation":
            boundary = "org.pitest.mutationtest.engine.gregor.mutators.ConditionalsBoundaryMutator"
            request = MutationRequest(target_path, suite, catalogue=Catalogue(operators=(boundary,)),
                                      attribution="all", workers=1, wall_clock_limit_s=120)
            with adapter.mutations.open_session("pit", workers=1) as session:
                measured = session.run(request)
            assert not session.is_open
            assert measured.outcome is lb.Outcome.COMPLETED, measured.reason
            assert measured.complete and measured.score() == 0
            assert len(measured.mutants) == 2
            assert all(item.row["native_status"] == "SURVIVED" for item in measured.mutants)
            assert measured.identity.observation.target.path == target_path
            assert measured.identity.observation.execution.kind == "container"
        else:
            validity = adapter.sources.validate_source(original.decode("utf-8"))
            assert validity.completed and validity.value.status == "valid", validity
            collected = adapter.tests.collect_suite(CollectionRequest(suite, 120))
            assert collected.outcome is lb.Outcome.COMPLETED and collected.returncode == 0, collected.reason
            request = SuiteRunRequest(suite, 120, coverage_target=target_path, timeout_journal=True)
            measured = adapter.tests.run_suite(request)
            assert measured.outcome is lb.Outcome.COMPLETED, (measured.reason, measured.output)
            assert measured.counts.passed == 3 and len(measured.tests) == 3
            assert measured.coverage.target == measured.identity.target
            assert measured.coverage.branches and measured.coverage.taken
            evidence = adapter.tests.per_test_evidence(PerTestEvidenceRequest(suite, target_path, 120))
            assert evidence.outcome is lb.Outcome.COMPLETED and len(evidence.tests) == 3, evidence.reason
            assert all(item.items for item in evidence.tests) and not evidence.untimed
            delivered = adapter.tests.run_as_delivered(SuiteRunRequest(suite, 120, timeout_journal=True))
            assert delivered.outcome is lb.Outcome.COMPLETED and delivered.counts.passed == 3
            benchmark = adapter.tests.benchmark(BenchmarkRequest(suite, 1, 120, warmup_runs=1))
            assert benchmark.outcome is lb.Outcome.COMPLETED, benchmark.reason
            assert len(benchmark.times_ms) == 1 and benchmark.mean_ms > 0
            # A warmed benchmark runs on the project's own context. Inside a
            # prepared copy's use each fork's own copy of the build is refused
            # while the benchmark runs (docs/guide/timing.md).
            with pytest.raises(OwnershipRefused, match="Workspace has an active execution operation"):
                adapter.tests.benchmark_warmed(WarmedSuiteBenchmarkRequest(
                    suite, "native_lifecycle", 120, 600, forks=2, warmup_runs=1, runs=2))
        assert copied_target.read_bytes() == original and target.read_bytes() == original
        copied_target.write_bytes(b"changed copy only")
    with pytest.raises(OwnershipRefused):
        adapter.tests.run_suite(SuiteRunRequest(suite, 120))
    with owner.use() as selected:
        assert selected.root == owner.root and copied_target.read_bytes() == original
    owner.close()
    owner.close()
    assert not copied.exists() and not receipt.exists()
    assert target.read_bytes() == original
    observation = measured.identity.observation if operation == "mutation" else measured.identity
    assert observation.execution.kind == "container"
