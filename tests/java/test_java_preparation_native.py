"""Opt-in public Java preparation with cached JDK and Maven tools."""
import os
from pathlib import Path

import pytest

import langadapter as lb
from langadapter._owned_directory import OwnershipRefused
from langadapter.java import _tools
from langadapter.runs import SuiteRunRequest


pytestmark = pytest.mark.skipif(
    os.environ.get("LANGADAPTER_TEST_PREPARED") != "1",
    reason="requires explicitly scheduled prepared-project native verification")


def test_public_java_copy_uses_cached_native_build_and_refuses_expired_context(
    tmp_path, java_project, jdks, maven, monkeypatch,
):
    if 21 not in jdks:
        pytest.skip("requires selected JDK 21")

    def no_download(*args, **kwargs):
        pytest.fail("prepared native fixture attempted a tool download")

    monkeypatch.setattr(_tools.urllib.request, "urlopen", no_download)
    source = java_project("maven")
    (source / ".mvn").mkdir()
    (source / ".mvn" / "maven.config").write_text("-o\n", encoding="ascii")
    java = jdks[21] / "bin" / ("java.exe" if os.name == "nt" else "java")
    target = source / "src" / "main" / "java" / "demo" / "Calc.java"
    original = target.read_bytes()
    owner = lb.prepare_project(lb.ProjectContext(str(source), lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven}),
        parent=str(tmp_path), receipt_parent=str(tmp_path))
    with owner.use() as selected:
        adapter = lb.create("java", selected)
        suite = lb.Suite(str(Path(selected.root) / "src" / "test" / "java"), ("demo/CalcTest.java",))
        request = SuiteRunRequest(suite, timeout_s=120)
        measured = adapter.tests.run_suite(request)
        assert measured.outcome is lb.Outcome.COMPLETED, (measured.reason, measured.output)
        assert measured.counts.passed == 3
        (Path(selected.root) / "src" / "main" / "java" / "demo" / "Calc.java").write_bytes(b"changed")
    with pytest.raises(OwnershipRefused):
        adapter.tests.run_suite(request)
    with owner.use() as selected:
        assert (Path(selected.root) / "src" / "main" / "java" / "demo" / "Calc.java").read_bytes() == original
        assert (Path(selected.root) / ".mvn" / "maven.config").read_text() == "-o\n"
    owner.close()
    assert target.read_bytes() == original and measured.counts.passed == 3


def test_a_warmed_benchmark_inside_a_prepared_copys_use_is_refused(
    tmp_path, java_project, jdks, maven, monkeypatch,
):
    """A warmed benchmark runs on the project's own context. Inside a prepared
    copy's use it raises OwnershipRefused: each fork prepares its own copy of
    the build, and a prepared copy refuses a further copy while an operation
    runs in it. The copy closes afterwards."""
    import tempfile

    from langadapter.runs import WarmedSuiteBenchmarkRequest

    if 21 not in jdks:
        pytest.skip("requires selected JDK 21")
    # The interrupted operation keeps its scratch directory and receipt in the
    # temporary directory, here the test's own.
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    source = java_project("maven")
    java = jdks[21] / "bin" / ("java.exe" if os.name == "nt" else "java")
    owner = lb.prepare_project(lb.ProjectContext(str(source), lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven}),
        parent=str(tmp_path), receipt_parent=str(tmp_path))
    with owner.use() as selected:
        adapter = lb.create("java", selected)
        suite = lb.Suite(str(Path(selected.root) / "src" / "test" / "java"), ("demo/CalcTest.java",))
        with pytest.raises(OwnershipRefused, match="Workspace has an active execution operation"):
            adapter.tests.benchmark_warmed(WarmedSuiteBenchmarkRequest(
                suite, "native_lifecycle", 120, 600, forks=2, warmup_runs=1, runs=2))
    owner.close()
    assert not Path(owner.root).exists()
