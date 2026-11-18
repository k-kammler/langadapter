"""Common test groups preserve native requests and unavailable observations."""
import dataclasses
import subprocess
import sys

import pytest

import langadapter as lb
from langadapter.runs import (
    Benchmark, BenchmarkRequest, CollectionRequest, CollectionRun,
    PerTestEvidence, PerTestEvidenceRequest, RunCounts, SuiteRun, SuiteRunRequest,
)


@pytest.fixture
def suite(tmp_path):
    (tmp_path / "test_example.py").write_text("def test_example(): pass\n", encoding="utf-8")
    return lb.Suite(str(tmp_path), ("test_example.py",))


def requests(suite):
    return {
        "run_suite": SuiteRunRequest(suite, 10),
        "collect_suite": CollectionRequest(suite, 10),
        "run_as_delivered": SuiteRunRequest(suite, 10),
        "benchmark": BenchmarkRequest(suite, 2, 10),
        "per_test_evidence": PerTestEvidenceRequest(suite, "target.py", 10),
    }


@pytest.mark.parametrize("method", tuple(requests(None)))
def test_partial_registered_adapter_returns_explicit_unsupported_without_tools(tmp_path, suite, monkeypatch, method):
    class Partial(lb.LangAdapter):
        language = "test-group-partial"
        file_extensions = (".test-group-partial",)

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("group launched a process"))
    lb.register(Partial.language, Partial.file_extensions, Partial)
    try:
        adapter = lb.create(Partial.language, lb.ProjectContext(str(tmp_path)))
        assert adapter.tests is adapter.tests and isinstance(adapter.tests, lb.TestOperations)
        result = getattr(adapter.tests, method)(requests(suite)[method])
        assert result.outcome is lb.Outcome.UNSUPPORTED and result.reason
        assert result.identity.suite == suite.identity()
        assert result.identity.toolchain.version == "unknown"
        assert result.identity.instrument is None
        if isinstance(result, SuiteRun):
            assert result.counts == RunCounts() and result.coverage is None and result.wall_time_s is None
        elif isinstance(result, Benchmark):
            assert result.times_ms == () and result.median_ms is None and result.allowed_cpus is None
            assert result.warmup_runs == 0
        elif isinstance(result, PerTestEvidence):
            assert result.tests == () and result.suite_time_ms is None and result.coverage is None
        else:
            assert result.wall_time_s is None and result.returncode is None
        capability = next(item for item in adapter.operation_capabilities() if item.name == f"tests.{method}")
        assert not capability.supported and capability.readiness is lb.Availability.UNSUPPORTED
    finally:
        lb.unregister(Partial.language)


@pytest.mark.parametrize("language", ("python", "java"))
@pytest.mark.parametrize("method", tuple(requests(None)))
def test_builtin_setup_verdict_reflects_implementation_without_probes(tmp_path, suite, monkeypatch, language, method):
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("missing runtime launched a process"))
    adapter = lb.create(language, lb.ProjectContext(str(tmp_path)))
    result = getattr(adapter.tests, method)(requests(suite)[method])
    capability = next(item for item in adapter.operation_capabilities() if item.name == f"tests.{method}")
    if language == "python" or method in {"run_suite", "benchmark", "per_test_evidence"}:
        assert capability.supported
    assert capability.execution_backed
    if capability.supported:
        assert result.outcome is lb.Outcome.MISSING_SETUP
        assert capability.readiness is lb.Availability.MISSING_SETUP
    else:
        assert result.outcome is lb.Outcome.UNSUPPORTED
        assert capability.readiness is lb.Availability.UNSUPPORTED


@pytest.mark.parametrize("timeout", (0, -1, float("nan"), float("inf"), True, 10 ** 400),
                         ids=("zero", "negative", "nan", "inf", "bool", "beyond-float"))
@pytest.mark.parametrize("method", tuple(requests(None)))
def test_malformed_requests_are_refused_even_for_unsupported_operations(tmp_path, suite, method, timeout):
    adapter = lb.LangAdapter(lb.ProjectContext(str(tmp_path)))
    with pytest.raises(lb.InvalidRequest, match="request"):
        getattr(adapter.tests, method)(None)
    request = dataclasses.replace(requests(suite)[method], timeout_s=timeout)
    with pytest.raises(lb.InvalidRequest, match="timeout_s"):
        getattr(adapter.tests, method)(request)
    dataclasses.replace(request, timeout_s=0.5).validate()


@pytest.mark.parametrize("field, value", (("runs", 0), ("runs", True), ("runs", 1.0),
                                          ("warmup_runs", -1), ("warmup_runs", True)))
def test_a_benchmarks_repetition_counts_are_whole_numbers(suite, field, value):
    request = dataclasses.replace(requests(suite)["benchmark"], **{field: value})
    with pytest.raises(lb.InvalidRequest, match=f"^{field}:"):
        request.validate()


@pytest.mark.parametrize("language", ("python", "java"))
@pytest.mark.parametrize("method", tuple(requests(None)))
def test_builtin_delegation_keeps_exact_requests_and_observations(tmp_path, suite, language, method, monkeypatch):
    adapter = lb.create(language, lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    group = adapter.tests
    request = requests(suite)[method]
    identity = lb.ObservationIdentity(None, suite.identity(), lb.ToolchainIdentity("native", "fixture"),
                                      None, lb.ExecutionIdentity("local"))
    outputs = {
        "run_suite": SuiteRun(lb.Outcome.TEST_FAILURES, "native failure", RunCounts(1, 0, 1, 0, 0),
                              (), (), 0.25, "native log", None, identity),
        "collect_suite": CollectionRun(lb.Outcome.NO_TESTS, "native empty", identity, "native discovery"),
        "run_as_delivered": SuiteRun(lb.Outcome.COMPLETED, "", RunCounts(1, 1, 0, 0, 0),
                                     (), (), 0.2, "", None, identity),
        "benchmark": Benchmark(lb.Outcome.TIMED_OUT, "native timeout", (2.5,), 1, None,
                               "native timing", identity),
        "per_test_evidence": PerTestEvidence(lb.Outcome.COMPLETED, "", (), (), (), "not_requested",
                                             (), 0.0, None, identity),
    }
    seen = []

    def native(actual, **kwargs):
        seen.append((actual, kwargs))
        return outputs[method]

    monkeypatch.setattr(adapter, method, native, raising=False)
    result = getattr(group, method)(request)
    assert result == outputs[method] and result is not outputs[method]
    assert seen == [(request, {"sources": None} if method == "run_as_delivered" else {})]
    capability = next(item for item in adapter.operation_capabilities() if item.name == f"tests.{method}")
    assert capability.supported and capability.readiness is None


@pytest.mark.parametrize("sources", [{"../test_example.py": "pass"}, {"absent.py": "pass"},
                                    {"test_example.py": None}, {"test_example.py": 3}, []])
def test_replacement_contract_is_checked_before_unsupported_result(tmp_path, suite, sources):
    adapter = lb.LangAdapter(lb.ProjectContext(str(tmp_path)))
    with pytest.raises(lb.InvalidRequest, match="sources"):
        adapter.tests.run_as_delivered(SuiteRunRequest(suite, 10), sources=sources)


def test_replacements_are_snapshotted_and_declared_setup_cannot_be_replaced(tmp_path, suite):
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    suite = dataclasses.replace(suite, setup_files=("conftest.py",))
    adapter = lb.LangAdapter(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    request = SuiteRunRequest(suite, 10)
    observed = adapter.tests._suite_failure(request, lb.Outcome.COMPLETED, "")
    received = []
    adapter.run_as_delivered = lambda request, *, sources: received.append(sources) or observed
    sources = {"test_example.py": "def test_example(): assert True"}
    adapter.tests.run_as_delivered(request, sources=sources)
    sources["test_example.py"] = "changed"
    assert received[0]["test_example.py"] != "changed"
    with pytest.raises(lb.InvalidRequest, match="sources"):
        adapter.tests.run_as_delivered(request, sources={"conftest.py": "pass"})


def test_an_extension_implements_a_test_operation_by_its_hook(tmp_path, suite):
    class External(lb.LangAdapter):
        language = "external"

        def run_suite(self, request):
            return self.tests._suite_failure(request, lb.Outcome.TEST_FAILURES, "known native verdict")

    adapter = External(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    assert adapter.tests.supports("run_suite") and not adapter.tests.supports("collect_suite")
    assert adapter.tests.run_suite(SuiteRunRequest(suite, 10)).outcome is lb.Outcome.TEST_FAILURES


def test_an_extension_refines_its_operation_report_in_the_public_hooks(tmp_path, suite):
    """An extension names its missing setup, refuses an operation in its
    context, gives an operation's probed readiness and chooses the profile
    that refuses its runtime, each in its public hook."""
    from langadapter.capabilities import Capability, CapabilityReport
    from langadapter.versions import VersionProfile

    class TooOld(VersionProfile):
        def refusal(self):
            return "the runtime is too old"

    class External(lb.LangAdapter):
        language = "external"
        setup_issue = context_issue = ""
        profile_type = VersionProfile

        def run_suite(self, request):
            return self.tests._suite_failure(request, lb.Outcome.TEST_FAILURES, "known native verdict")

        def execution_setup_issue(self):
            return self.setup_issue

        def operation_context_issue(self, name):
            return self.context_issue

        def operation_readiness(self, name, report):
            return Capability(name, lb.Availability.MISSING_SETUP, "the runner is missing")

        def choose_profile(self):
            return self.profile_type(None, self.support)

    def adapter(**settings):
        made = External(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
        vars(made).update(settings)
        return made, {entry.name: entry for entry in made.operation_capabilities()}["tests.run_suite"]

    request = SuiteRunRequest(suite, 10)
    missing, entry = adapter(setup_issue="set the runner's path")
    assert entry.supported and entry.readiness is lb.Availability.MISSING_SETUP
    assert entry.reason == "set the runner's path"
    run = missing.tests.run_suite(request)
    assert (run.outcome, run.reason) == (lb.Outcome.MISSING_SETUP, "set the runner's path")

    refused, entry = adapter(context_issue="this context runs no suite")
    assert not refused.tests.supports("run_suite")
    assert not entry.supported and entry.reason == "this context runs no suite"
    run = refused.tests.run_suite(request)
    assert (run.outcome, run.reason) == (lb.Outcome.UNSUPPORTED, "this context runs no suite")

    probed, _ = adapter()
    probed.capabilities = lambda: CapabilityReport("external", sys.executable, lb.Outcome.COMPLETED, "",
                                                   "1.0", (), (), "none", ())
    readiness = probed.probe_capabilities().operation("tests.run_suite")
    assert (readiness.readiness, readiness.reason) == (lb.Availability.MISSING_SETUP, "the runner is missing")

    old, _ = adapter(profile_type=TooOld)
    assert isinstance(old.profile(), TooOld)
    run = old.tests.run_suite(request)
    assert (run.outcome, run.reason) == (lb.Outcome.UNSUPPORTED, "the runtime is too old")


def test_java_container_without_control_setup_does_not_claim_tool_readiness(tmp_path, suite, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("container support was probed"))
    context = lb.ProjectContext("/project", lb.Interpreter(path="/usr/bin/java"),
                               execution=lb.ContainerExecution("fixture", str(tmp_path),
                                                              repository_dir="/project",
                                                              container_work_dir="/project"))
    adapter = lb.create("java", context)
    result = adapter.tests.run_suite(SuiteRunRequest(suite, 10))
    assert result.outcome is lb.Outcome.MISSING_SETUP
    capability = next(item for item in adapter.operation_capabilities() if item.name == "tests.run_suite")
    assert capability.supported and capability.readiness is lb.Availability.MISSING_SETUP
