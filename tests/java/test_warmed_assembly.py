"""Actual private protocol-to-result assembly with no native execution."""
from dataclasses import replace
import json

import pytest

from langadapter.runs import WarmedBenchmarkIdentity, WarmedSuiteBenchmarkRequest
from langadapter.outcomes import ExecutionIdentity, ToolchainIdentity, Outcome
from langadapter.errors import InvalidRequest
from langadapter.java import _warmed as assembly, _warmed as native
from langadapter.java._warmed import ForkExecution
from langadapter.suites import Suite

from test_warmed_protocol import report


@pytest.fixture
def inputs(tmp_path):
    suite = tmp_path / "ExampleTest.java"
    suite.write_text("class ExampleTest {}", encoding="utf-8")
    request = WarmedSuiteBenchmarkRequest(Suite(str(tmp_path), (str(suite),)),
        "native_lifecycle", 2, 60, startup_timeout_s=30, forks=2, warmup_runs=1, runs=2)
    identity = WarmedBenchmarkIdentity(
        suite=(), project_inputs=(), toolchain=ToolchainIdentity("java", "fixture"),
        execution=ExecutionIdentity("local"), build_tool="fixture", build_version=None,
        engine_version=None, jvm_args=(), runner_options=(), mode="warmed_jvm",
        timing_boundary="junit_launcher_execute", state_policy="native_lifecycle",
        forks=2, warmup_runs=1, runs=2, iteration_timeout_s=2, total_timeout_s=60,
        startup_timeout_s=30, requested_cpus=None, serialize=True)
    return request, identity


def execution(fork, samples=(1_000_000, 3_000_000), *, changes=None, partial=None,
              outcome=Outcome.COMPLETED, finished=True):
    protocol = native.ForkProtocol(("a" if fork == 0 else "b") * 32, fork, 1, 2)
    transcript = native.Transcript(protocol)
    transcript.accept(native.decode(json.dumps(report(protocol, "ready")).encode(), protocol))
    for index, value in enumerate((99_000_000, *samples)):
        transcript.accept(native.decode(json.dumps(report(protocol, "start", index)).encode(), protocol))
        value = report(protocol, "iteration", index, elapsed_ns=value)
        if changes:
            changes(value, index)
        transcript.accept(native.decode(json.dumps(value).encode(), protocol))
        if partial is not None and len(transcript.iterations) == partial:
            break
    if finished:
        transcript.accept(native.decode(json.dumps(report(
            protocol, "complete", outcome=outcome.value,
            iterations_completed=len(transcript.iterations))).encode(), protocol))
    return ForkExecution(transcript, outcome, "native reason", (f"fork {fork} diagnostic",))


def assemble(inputs, executions, **options):
    request, identity = inputs
    arguments = dict(outcome=Outcome.COMPLETED, reason="", operation_elapsed_s=123,
                     diagnostics=("outer raw diagnostic",))
    arguments.update(options)
    return assembly.assemble(request, identity, executions, **arguments)


def headlines(result):
    return (result.mean_of_fork_means_ms, result.median_of_fork_medians_ms,
            result.sample_min_ms, result.sample_max_ms, result.between_fork_mean_sd_ms)


def test_assembly_preserves_raw_records_and_only_summarizes_measurements(inputs):
    received = (execution(0), execution(1, (2_000_000, 6_000_000)))
    result = assemble(inputs, received, allowed_cpus=((2, 7), (2, 7)))
    assert result.validity == "complete" and result.outcome is Outcome.COMPLETED
    assert result.forks_started == result.forks_completed == 2
    assert result.warmup_completed == 2 and result.measurements_completed == 4
    assert result.operation_elapsed_s == 123 and result.disposal_confirmed
    assert result.iterations[0] is received[0].transcript.iterations[0]
    assert result.iterations[0].elapsed_ns == 99_000_000
    assert result.mean_of_fork_means_ms == result.median_of_fork_medians_ms == 3
    assert result.sample_min_ms == 1 and result.sample_max_ms == 6
    assert result.allowed_cpus == (2, 7)
    assert result.diagnostics == ("outer raw diagnostic", "fork 0 diagnostic", "fork 1 diagnostic")


@pytest.mark.parametrize("field", ["unique_id", "status", "kind"])
def test_cross_fork_native_identity_or_outcome_change_has_no_headline(inputs, field):
    def changed(value, index):
        if field == "unique_id":
            value["tests"][0][field] = "changed"
        elif field == "status":
            value["tests"][1][field] = "aborted"
        else:
            value["tests"][1][field] = "test"
            value["counts"]["passed"] = 2
    result = assemble(inputs, (execution(0), execution(1, changes=changed)))
    assert result.validity == "non_repeatable" and result.outcome is Outcome.COMPLETED
    assert len(result.iterations) == 6 and headlines(result) == (None,) * 5
    assert all(fork.mean_ms is None for fork in result.forks)


def test_within_fork_pattern_change_preserves_partial_raw_samples(inputs):
    def changed(value, index):
        if index == 1:
            value["tests"][0]["unique_id"] = "different"
    result = assemble(inputs, (execution(0, changes=changed, partial=2, finished=False),))
    assert result.validity == "non_repeatable"
    assert result.forks_completed == 0 and result.forks_started == 1
    assert result.forks[0].samples_ns == (1_000_000,)
    assert headlines(result) == (None,) * 5


def test_partial_timeout_retains_samples_counts_and_actual_outcome(inputs):
    received = execution(0, partial=2, outcome=Outcome.TIMED_OUT, finished=False)
    result = assemble(inputs, (received,))
    assert result.outcome is Outcome.TIMED_OUT and result.validity == "incomplete"
    assert result.reason == "native reason"
    assert result.warmup_completed == result.measurements_completed == 1
    assert result.forks[0].samples_ns == (1_000_000,)
    assert headlines(result) == (None,) * 5


def test_complete_native_protocol_is_not_a_completed_requested_budget(inputs):
    def red(value, index):
        value["outcome"] = "test_failures"
        value["tests"][0]["status"] = "failed"
        value["counts"]["passed"] = 0
        value["counts"]["failed"] = 1
    received = execution(0, changes=red, partial=1, outcome=Outcome.TEST_FAILURES)
    result = assemble(inputs, (received,))
    assert received.transcript.finished
    assert result.outcome is Outcome.TEST_FAILURES and result.validity == "incomplete"
    assert result.forks_completed == 0 and result.warmup_completed == 1
    assert headlines(result) == (None,) * 5


@pytest.mark.parametrize("outcome", [Outcome.MISSING_SETUP, Outcome.UNSUPPORTED,
                                   Outcome.INFRASTRUCTURE_FAILURE, Outcome.TIMED_OUT])
def test_no_native_launch_has_no_samples_or_invented_forks(inputs, outcome):
    result = assemble(inputs, (), outcome=outcome, reason="actual setup reason")
    assert result.outcome is outcome and result.reason == "actual setup reason"
    assert result.validity == "no_samples" and result.forks_started == 0
    assert result.iterations == result.forks == ()
    assert result.allowed_cpus is None and headlines(result) == (None,) * 5


def test_partial_green_without_actual_stop_reason_cannot_claim_success(inputs):
    result = assemble(inputs, (execution(0),))
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert result.validity == "incomplete" and headlines(result) == (None,) * 5


def test_native_root_failure_preserves_complete_protocol_without_success_summary(inputs):
    first = replace(execution(0), outcome=Outcome.INFRASTRUCTURE_FAILURE,
                    reason="a timing fork exited with an error")
    result = assemble(inputs, (first, execution(1)))
    assert result.validity == "complete" and result.forks_completed == 2
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert result.reason == first.reason and headlines(result) == (None,) * 5
    assert result.forks[0].samples_ns == (1_000_000, 3_000_000)


def test_outer_failure_is_not_overwritten_by_green_received_records(inputs):
    result = assemble(inputs, (execution(0), execution(1)), outcome=Outcome.TIMED_OUT,
                      reason="Operation deadline after native reports")
    assert result.validity == "complete" and result.outcome is Outcome.TIMED_OUT
    assert result.reason == "Operation deadline after native reports"
    assert headlines(result) == (None,) * 5


def test_zero_duration_stays_raw_and_unknown_terminal_counts_stay_unknown(inputs):
    def unknown(value, index):
        value["counts"] = dict.fromkeys(value["counts"])
    result = assemble(inputs, (execution(0, (0, 0), changes=unknown),
                               execution(1, (0, 0), changes=unknown)))
    assert headlines(result) == (0,) * 5
    assert result.warmup_completed == 2 and result.measurements_completed == 4
    assert result.forks[0].samples_ns == (0, 0)
    assert all(item.counts.passed is None for item in result.iterations)
    assert "resolution" in result.iterations[1].diagnostics[0]


@pytest.mark.parametrize("change", ["order", "duplicate", "budget", "extra", "placement", "settings"])
def test_assembly_refuses_mismatched_native_or_request_identity(inputs, change):
    request, identity = inputs
    received = (execution(0), execution(1))
    if change == "order":
        received = tuple(reversed(received))
    elif change == "duplicate":
        received = (received[0], received[0])
    elif change == "budget":
        received[0].transcript.protocol = native.ForkProtocol("a" * 32, 0, 2, 2)
    elif change == "extra":
        received = (*received, received[0])
    elif change == "placement":
        identity = replace(identity, requested_cpus=(2,))
    else:
        identity = replace(identity, total_timeout_s=61)
    with pytest.raises(InvalidRequest):
        assemble((request, identity), received)


def test_unknown_or_different_allowed_cpus_never_become_observed_usage(inputs):
    received = (execution(0), execution(1))
    for cpus in (None, ((2,), None), ((2,), (7,))):
        result = assemble(inputs, received, allowed_cpus=cpus)
        assert result.allowed_cpus is None and result.requested_cpus is None
    with pytest.raises(InvalidRequest):
        assemble(inputs, received, allowed_cpus=((2,),))
