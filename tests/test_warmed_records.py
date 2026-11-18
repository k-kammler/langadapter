from dataclasses import FrozenInstanceError, asdict, replace
import math

import pytest

from langadapter import ExecutionIdentity, FileIdentity, Outcome, Suite, ToolchainIdentity
from langadapter.runs import (
    WarmedBenchmarkIdentity, WarmedBenchmarkProgress, WarmedFork, WarmedIteration,
    WarmedSuiteBenchmark, WarmedSuiteBenchmarkRequest, WarmedTestCounts,
    WarmedTestObservation,
)
from langadapter.errors import InvalidRequest


@pytest.fixture
def suite(tmp_path):
    (tmp_path / "CalcTest.java").write_text("class CalcTest {}", encoding="ascii")
    return Suite(str(tmp_path), ("CalcTest.java",))


def _request(suite, **changes):
    return WarmedSuiteBenchmarkRequest(suite, "native_lifecycle", 2, 30, **changes)


def _identity(**changes):
    values = dict(suite=(), project_inputs=(), toolchain=ToolchainIdentity("java", "21"),
                  execution=ExecutionIdentity("local"), build_tool=None, build_version=None,
                  engine_version=None, jvm_args=(), runner_options=(), mode="warmed_jvm",
                  timing_boundary="junit_launcher_execute", state_policy="native_lifecycle",
                  forks=1, warmup_runs=1, runs=1, iteration_timeout_s=2,
                  total_timeout_s=30, startup_timeout_s=60, requested_cpus=None, serialize=True)
    return WarmedBenchmarkIdentity(**(values | changes))


def _result(**changes):
    values = dict(outcome=Outcome.TIMED_OUT, reason="cut short", validity="incomplete",
                  requested_forks=1, requested_warmup_runs=1, requested_runs=1,
                  forks_started=1, forks_completed=0, warmup_completed=0,
                  measurements_completed=0, iterations=(), forks=(), operation_elapsed_s=None,
                  identity=_identity(), requested_cpus=None, allowed_cpus=None,
                  cancelled=False, disposal_confirmed=True)
    return WarmedSuiteBenchmark(**(values | changes))


def _complete_result(*, duration=0, outcome=Outcome.COMPLETED):
    test = WarmedTestObservation("[test:m]", "m", "test", "successful"
                                if outcome is Outcome.COMPLETED else "failed")
    counts = WarmedTestCounts(passed=1) if outcome is Outcome.COMPLETED else WarmedTestCounts(failed=1)
    iterations = tuple(WarmedIteration(0, kind, 0, duration, outcome, True, counts, (test,))
                       for kind in ("warmup", "measurement"))
    fork = WarmedFork(0, 1, 1, True, True, None, (duration,))
    return _result(validity="complete", outcome=outcome, iterations=iterations, forks=(fork,),
                   forks_completed=1, warmup_completed=1, measurements_completed=1)


def test_request_exact_explicit_policy_and_defaults(suite):
    request = _request(suite)
    request.validate()
    assert (request.forks, request.warmup_runs, request.runs) == (3, 5, 10)
    assert request.startup_timeout_s == 60 and request.serialize is True
    assert request.cpus is None and request.runner_options is None and request.on_progress is None
    with pytest.raises(TypeError):
        WarmedSuiteBenchmarkRequest(suite=suite, iteration_timeout_s=2, total_timeout_s=30)
    with pytest.raises(FrozenInstanceError):
        request.runs = 2


@pytest.mark.parametrize("name", ["forks", "warmup_runs", "runs"])
@pytest.mark.parametrize("value", [False, True, 0, -1, 1.5, "3"])
def test_request_count_validation_precedes_suite_read(suite, monkeypatch, name, value):
    monkeypatch.setattr(Suite, "validate", lambda self: pytest.fail("invalid request read suite"))
    with pytest.raises(InvalidRequest, match=name):
        replace(_request(suite), **{name: value}).validate()


@pytest.mark.parametrize("name", ["iteration_timeout_s", "total_timeout_s", "startup_timeout_s"])
@pytest.mark.parametrize("value", [False, True, 0, -1, math.inf, math.nan, "2", 10**400])
def test_request_timeout_validation_precedes_suite_read(suite, monkeypatch, name, value):
    monkeypatch.setattr(Suite, "validate", lambda self: pytest.fail("invalid request read suite"))
    with pytest.raises(InvalidRequest, match=name):
        replace(_request(suite), **{name: value}).validate()


@pytest.mark.parametrize("changes,field", [({"state_policy": "reset"}, "state_policy"),
                                         ({"serialize": 1}, "serialize"),
                                         ({"cpus": []}, "cpus"),
                                         ({"cpus": [0, 0]}, "cpus"),
                                         ({"cpus": [-1]}, "cpus"),
                                         ({"cpus": [True]}, "cpus"),
                                         ({"cpus": [[2]]}, "cpus"),
                                         ({"on_progress": "print"}, "on_progress")])
def test_request_refuses_ambiguous_controls(suite, changes, field):
    with pytest.raises(InvalidRequest, match=field):
        replace(_request(suite), **changes).validate()


def test_mutable_processor_input_is_copied(suite):
    cpus = [2, 5]
    request = _request(suite, cpus=cpus)
    cpus.append(9)
    request.validate()
    assert request.cpus == (2, 5)


def test_admission_progress_keeps_unknowns_and_zero_based_indices():
    event = WarmedBenchmarkProgress("iteration_start", 0, "warmup", 0, 3, 5, 10, 1, 0, 0, 0)
    assert event.fork == 0 and event.repetition == 0
    assert event.elapsed_ns is None and event.operation_elapsed_s is None and event.outcome is None
    assert event.warmup_completed == event.measurements_completed == 0
    with pytest.raises(FrozenInstanceError):
        event.warmup_completed = 1


@pytest.mark.parametrize("changes", [{"stage": "measured"}, {"kind": "startup"},
                                    {"fork": -1}, {"repetition": True},
                                    {"elapsed_ns": 1.5}, {"operation_elapsed_s": math.nan}])
def test_progress_refuses_invalid_native_values(changes):
    event = WarmedBenchmarkProgress("iteration_end", 0, "warmup", 0, 1, 1, 1, 1, 0, 1, 0)
    with pytest.raises(InvalidRequest):
        replace(event, **changes)


@pytest.mark.parametrize("stage", ["setup", "fork_start", "fork_end", "complete"])
@pytest.mark.parametrize("changes", [{"kind": "warmup"}, {"repetition": 0}])
def test_noniteration_progress_cannot_claim_iteration_fields(stage, changes):
    event = WarmedBenchmarkProgress(stage, None, None, None, 1, 1, 1, 0, 0, 0, 0)
    with pytest.raises(InvalidRequest, match="stage|repetition"):
        replace(event, **changes)


@pytest.mark.parametrize("stage", ["iteration_start", "iteration_end"])
@pytest.mark.parametrize("name", ["fork", "kind", "repetition"])
def test_iteration_progress_requires_complete_native_location(stage, name):
    event = WarmedBenchmarkProgress(stage, 0, "measurement", 0, 1, 1, 1, 1, 0, 0, 0)
    with pytest.raises(InvalidRequest, match="stage|repetition"):
        replace(event, **{name: None})


@pytest.mark.parametrize("changes", [{"elapsed_ns": 0}, {"outcome": Outcome.COMPLETED}])
def test_iteration_admission_cannot_publish_unreceived_native_observations(changes):
    event = WarmedBenchmarkProgress("iteration_start", 0, "measurement", 0, 1, 1, 1, 1, 0, 0, 0)
    with pytest.raises(InvalidRequest, match="stage"):
        replace(event, **changes)


@pytest.mark.parametrize("stage,fork,kind,repetition", [
    ("setup", None, None, None), ("fork_start", 0, None, None),
    ("iteration_start", 0, "warmup", 0), ("iteration_end", 0, "measurement", 0),
    ("fork_end", 0, None, None), ("complete", None, None, None),
])
def test_native_progress_stage_locations_remain_valid(stage, fork, kind, repetition):
    event = WarmedBenchmarkProgress(stage, fork, kind, repetition, 1, 1, 1, 1, 0, 0, 0)
    assert (event.stage, event.fork, event.kind, event.repetition) == (stage, fork, kind, repetition)
    assert event.outcome is None and event.elapsed_ns is None
    if stage == "iteration_end":
        completed = replace(event, elapsed_ns=0, outcome=Outcome.COMPLETED, measurements_completed=1)
        assert completed.elapsed_ns == 0 and completed.outcome is Outcome.COMPLETED


def test_native_counts_do_not_turn_missing_into_zero():
    counts = WarmedTestCounts(passed=0, container_failures=0)
    assert counts.passed == counts.container_failures == 0
    assert counts.discovered is None and counts.failed is None
    for field in counts.__dataclass_fields__:
        with pytest.raises(InvalidRequest, match=field):
            replace(counts, **{field: True})


def test_iteration_keeps_raw_native_order_container_failure_and_zero_duration():
    items = [WarmedTestObservation("[engine:x]/[class:C]", "C", "container", "failed", "setup"),
             WarmedTestObservation("[engine:x]/[class:C]/[method:m]", "m", "test", "aborted")]
    diagnostics = ["timer resolution limited"]
    iteration = WarmedIteration(0, "measurement", 0, 0, Outcome.TEST_FAILURES, True,
                               WarmedTestCounts(container_failures=1, aborted=1), items,
                               diagnostics=diagnostics)
    items.clear()
    diagnostics.clear()
    assert len(iteration.tests) == 2 and iteration.tests[0].kind == "container"
    assert iteration.tests[0].reason == "setup"
    assert iteration.elapsed_ns == 0 and iteration.counts.passed is None
    assert iteration.diagnostics[0] == "timer resolution limited"
    assert "resolution" in iteration.diagnostics[-1]
    with pytest.raises(FrozenInstanceError):
        iteration.tests = ()


@pytest.mark.parametrize("elapsed", [-1, False, math.nan, math.inf, 1.5])
def test_iteration_requires_integer_native_nanoseconds(elapsed):
    with pytest.raises(InvalidRequest, match="elapsed_ns"):
        WarmedIteration(0, "measurement", 0, elapsed, Outcome.COMPLETED, True,
                        WarmedTestCounts(), ())


def test_iteration_duplicate_identifiers_are_not_substituted_by_counts():
    item = WarmedTestObservation("[test:m]", "m", "test", "successful")
    with pytest.raises(InvalidRequest, match="duplicate"):
        WarmedIteration(0, "measurement", 0, 20, Outcome.COMPLETED, True,
                        WarmedTestCounts(passed=2), (item, item))


def test_identity_copies_nested_input_collections_and_keeps_unknown_versions():
    files = [FileIdentity("CalcTest.java", "input")]
    args, options, cpus = ["-Xmx128m"], [["engine", "native"]], [2]
    identity = _identity(suite=files, jvm_args=args, runner_options=options, requested_cpus=cpus)
    files.clear()
    args.clear()
    options[0][1] = "changed"
    cpus.clear()
    assert identity.suite == (FileIdentity("CalcTest.java", "input"),)
    assert identity.jvm_args == ("-Xmx128m",) and identity.runner_options == (("engine", "native"),)
    assert identity.requested_cpus == (2,) and identity.schema_version == 1
    assert identity.build_tool is identity.build_version is identity.engine_version is None
    assert "environment" not in asdict(identity) and "pid" not in asdict(identity)


def test_identity_copies_nested_toolchain_members():
    tools = [["JUnit Platform", "fixture"]]
    identity = _identity(toolchain=ToolchainIdentity("java", "21", tools))
    tools[0][1] = "changed"
    assert identity.toolchain.tools == (("JUnit Platform", "fixture"),)


@pytest.mark.parametrize("changes", [{"mode": "fresh_process"}, {"state_policy": "reset"},
                                    {"timing_boundary": "process"}, {"schema_version": True},
                                    {"forks": 0}, {"runner_options": (("alone",),)}])
def test_identity_refuses_inconsistent_mode_or_parameters(changes):
    with pytest.raises(InvalidRequest):
        _identity(**changes)


@pytest.mark.parametrize("changes", [{"clock": "wall"}, {"unit": "ms"},
                                    {"comparison": "improved"}, {"requested_runs": 2}])
def test_result_cannot_relabel_native_units_or_input_budgets(changes):
    with pytest.raises(InvalidRequest):
        replace(_result(), **changes)


@pytest.mark.parametrize("validity,outcome", [("no_samples", Outcome.UNSUPPORTED),
                                           ("no_samples", Outcome.MISSING_SETUP),
                                           ("incomplete", Outcome.TIMED_OUT),
                                           ("non_repeatable", Outcome.COMPLETED),
                                           ("complete", Outcome.TEST_FAILURES)])
def test_unverified_observations_have_no_headline_or_fork_summary(validity, outcome):
    sample = WarmedIteration(0, "measurement", 0, 11, Outcome.COMPLETED, True,
                            WarmedTestCounts(passed=1), ())
    fork = WarmedFork(0, 0, 1, False, True, None, (11,))
    result = (_complete_result(duration=11, outcome=outcome) if validity == "complete" else
              _result(validity=validity, outcome=outcome, iterations=[sample], forks=[fork],
                      measurements_completed=1))
    assert result.iterations[-1].elapsed_ns == 11 and result.forks[0].samples_ns == (11,)
    assert result.mean_of_fork_means_ms is None and result.forks[0].mean_ms is None
    assert result.comparison == "not_assessed"
    with pytest.raises(InvalidRequest, match="summary"):
        replace(result, mean_of_fork_means_ms=0)
    with pytest.raises(InvalidRequest, match="summary"):
        replace(result, forks=[replace(result.forks[0], mean_ms=0)])


@pytest.mark.parametrize("changes", [{"cancelled": True}, {"disposal_confirmed": False}])
def test_completed_label_cannot_override_cancellation_or_uncertain_disposal(changes):
    with pytest.raises(InvalidRequest, match="validity|summary"):
        replace(_complete_result(), mean_of_fork_means_ms=1, **changes)


def test_single_fork_keeps_zero_and_disallows_between_fork_sd():
    result = replace(_complete_result(), mean_of_fork_means_ms=0,
                     median_of_fork_medians_ms=0, sample_min_ms=0, sample_max_ms=0)
    assert result.mean_of_fork_means_ms == 0 and result.between_fork_mean_sd_ms is None
    assert result.clock == "java.lang.System.nanoTime" and result.unit == "ns"
    assert result.timing_boundary == "junit_launcher_execute" and result.mode == "warmed_jvm"
    with pytest.raises(InvalidRequest, match="at least two"):
        replace(result, between_fork_mean_sd_ms=0)


def test_complete_label_requires_native_membership_not_equal_counts():
    with pytest.raises(InvalidRequest, match="complete requires"):
        _result(validity="complete", outcome=Outcome.COMPLETED, mean_of_fork_means_ms=1)
    result = _complete_result()
    wrong = replace(result.iterations[1], tests=(WarmedTestObservation(
        "[test:different]", "m", "test", "successful"),))
    with pytest.raises(InvalidRequest, match="population"):
        replace(result, iterations=(result.iterations[0], wrong))
    missing = replace(result.iterations[1], tests=())
    with pytest.raises(InvalidRequest, match="population"):
        replace(result, iterations=(result.iterations[0], missing))


@pytest.mark.parametrize("changes,field", [({"measurements_completed": 0}, "measurements_completed"),
                                         ({"forks_completed": 0}, "forks_completed"),
                                         ({"forks_started": 0}, "forks_completed"),
                                         ({"forks": ()}, "forks_completed")])
def test_result_rejects_inconsistent_protocol_counters(changes, field):
    with pytest.raises(InvalidRequest, match=field):
        replace(_complete_result(), **changes)


def test_warmup_durations_cannot_be_reported_as_measured_samples():
    result = _complete_result(duration=21)
    warmup = replace(result.iterations[0], elapsed_ns=1000)
    replace(result, iterations=(warmup, result.iterations[1]))
    with pytest.raises(InvalidRequest, match="samples"):
        replace(result, forks=(replace(result.forks[0], samples_ns=(1000, 21)),))


def test_stable_passing_test_with_unchanged_abort_is_not_reclassified():
    result = _complete_result(duration=10)
    aborted = WarmedTestObservation("[test:assumption]", "assumption", "test", "aborted")
    records = tuple(replace(item, tests=item.tests + (aborted,),
                            counts=WarmedTestCounts(passed=1, aborted=1)) for item in result.iterations)
    mixed = replace(result, iterations=records)
    assert mixed.outcome is Outcome.COMPLETED and mixed.validity == "complete"
    assert mixed.iterations[0].counts.aborted == 1


@pytest.mark.parametrize("kind", ["warmup", "measurement"])
@pytest.mark.parametrize("counts", [WarmedTestCounts(passed=1, failed=1),
                                   WarmedTestCounts(passed=1, container_failures=1),
                                   WarmedTestCounts(passed=0), WarmedTestCounts(passed=2),
                                   WarmedTestCounts(passed=1, skipped=1),
                                   WarmedTestCounts(passed=1, aborted=1)])
def test_complete_success_refuses_known_counts_contradicting_native_tests(kind, counts):
    original = _complete_result(duration=10)
    records = tuple(replace(item, counts=counts) if item.kind == kind else item
                    for item in original.iterations)
    with pytest.raises(InvalidRequest, match="counts"):
        replace(original, iterations=records)


@pytest.mark.parametrize("counts", [WarmedTestCounts(), WarmedTestCounts(passed=1),
                                   WarmedTestCounts(passed=1, failed=0, container_failures=0),
                                   WarmedTestCounts(discovered=20, started=3, passed=1)])
def test_complete_success_keeps_unknown_counts_and_native_discovery_semantics(counts):
    original = _complete_result(duration=10)
    records = tuple(replace(item, counts=counts) for item in original.iterations)
    result = replace(original, iterations=records)
    assert all(item.counts is counts for item in result.iterations)
    assert counts.skipped is None and counts.aborted is None


def test_complete_counts_reconcile_test_statuses_without_counting_containers_as_tests():
    original = _complete_result(duration=10)
    additional = (
        WarmedTestObservation("[test:skip]", "skip", "test", "skipped"),
        WarmedTestObservation("[test:abort]", "abort", "test", "aborted"),
        WarmedTestObservation("[container:C]", "C", "container", "successful"),
        WarmedTestObservation("[container:skip]", "skip", "container", "skipped"),
    )
    counts = WarmedTestCounts(passed=1, skipped=1, aborted=1, failed=0, container_failures=0)
    records = tuple(replace(item, tests=item.tests + additional, counts=counts)
                    for item in original.iterations)
    result = replace(original, iterations=records)
    assert result.validity == "complete" and result.outcome is Outcome.COMPLETED
    assert all(item.counts is counts for item in result.iterations)


@pytest.mark.parametrize("outcome,validity", [(Outcome.TEST_FAILURES, "complete"),
                                           (Outcome.TIMED_OUT, "incomplete")])
def test_failed_or_partial_observations_keep_unreconciled_raw_counts(outcome, validity):
    original = _complete_result(duration=10, outcome=Outcome.TEST_FAILURES)
    counts = WarmedTestCounts(passed=1, failed=2, container_failures=1)
    records = tuple(replace(item, counts=counts) for item in original.iterations)
    result = replace(original, iterations=records, outcome=outcome, validity=validity)
    assert all(item.counts is counts for item in result.iterations)
    assert result.mean_of_fork_means_ms is None and result.forks[0].mean_ms is None


@pytest.mark.parametrize("status", ["discovered", "skipped", "aborted", "failed"])
def test_no_native_passing_test_cannot_publish_successful_timing(status):
    result = _complete_result()
    test = WarmedTestObservation("[test:m]", "m", "test", status)
    records = tuple(replace(item, tests=(test,)) for item in result.iterations)
    with pytest.raises(InvalidRequest, match="passing test evidence"):
        replace(result, iterations=records, mean_of_fork_means_ms=0)


def test_a_refused_warmed_benchmark_has_the_pre_setup_identity(suite):
    """A language without a warmed benchmark is refused with the identity a
    warmed run has before its setup: an unknown toolchain, the context's
    flags and the requested processor set."""
    import sys

    import langadapter as lb
    from langadapter.versions import unknown

    adapter = lb.create("python", lb.ProjectContext(
        suite.root, lb.Interpreter(path=sys.executable), interpreter_flags=("-X", "dev")))
    result = adapter.tests.benchmark_warmed(_request(suite, cpus=(1,)))
    assert result.outcome is Outcome.UNSUPPORTED
    assert result.identity.toolchain == ToolchainIdentity.of(unknown("python"))
    assert result.identity.jvm_args == ("-X", "dev")
    assert result.identity.requested_cpus == (1,) and result.requested_cpus == (1,)
