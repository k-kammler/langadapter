from dataclasses import replace
import math

import pytest

from langadapter import ExecutionIdentity, Outcome, ToolchainIdentity
from langadapter.runs import (
    WarmedBenchmarkIdentity, WarmedFork, WarmedIteration, WarmedSuiteBenchmark,
    WarmedTestCounts, WarmedTestObservation,
)
from langadapter.runs import summarize
from langadapter.errors import InvalidRequest


def _result(samples, *, outcome=Outcome.COMPLETED, warmup_ns=999_000_000):
    forks_requested, runs = len(samples), len(samples[0])
    test_status = "successful" if outcome is Outcome.COMPLETED else "failed"
    tests = (WarmedTestObservation("[engine:junit]/[method:m]", "m", "test", test_status),)
    counts = WarmedTestCounts(passed=1) if outcome is Outcome.COMPLETED else WarmedTestCounts(failed=1)
    iterations, forks = [], []
    for index, values in enumerate(samples):
        iterations.append(WarmedIteration(index, "warmup", 0, warmup_ns, outcome, True, counts, tests))
        iterations.extend(WarmedIteration(index, "measurement", repetition, value,
                                          outcome, True, counts, tests)
                          for repetition, value in enumerate(values))
        forks.append(WarmedFork(index, 1, len(values), True, True, None, values))
    identity = WarmedBenchmarkIdentity(
        suite=(), project_inputs=(), toolchain=ToolchainIdentity("java", "fixture"),
        execution=ExecutionIdentity("local"), build_tool=None, build_version=None,
        engine_version=None, jvm_args=(), runner_options=(), mode="warmed_jvm",
        timing_boundary="junit_launcher_execute", state_policy="native_lifecycle",
        forks=forks_requested, warmup_runs=1, runs=runs, iteration_timeout_s=2,
        total_timeout_s=60, startup_timeout_s=30, requested_cpus=None, serialize=True)
    return WarmedSuiteBenchmark(
        outcome=outcome, reason="native fixture", validity="complete", requested_forks=forks_requested,
        requested_warmup_runs=1, requested_runs=runs, forks_started=forks_requested,
        forks_completed=forks_requested, warmup_completed=forks_requested,
        measurements_completed=forks_requested * runs, iterations=iterations, forks=forks,
        operation_elapsed_s=123, identity=identity, requested_cpus=None, allowed_cpus=None,
        cancelled=False, disposal_confirmed=True, diagnostics=("retained native diagnostic",))


def _headlines(result):
    return (result.mean_of_fork_means_ms, result.median_of_fork_medians_ms,
            result.sample_min_ms, result.sample_max_ms, result.between_fork_mean_sd_ms)


@pytest.mark.parametrize("samples_ms,expected", [
    (((1, 1, 10), (2, 3, 25), (4, 20, 24)), (10, 3, 1, 25, 6)),
    (((1, 2, 10, 11),), (6, 6, 1, 11, None)),
    (((1, 5), (2, 10)), (4.5, 4.5, 1, 10, 3 / math.sqrt(2))),
    (((0, 2), (0, 4)), (1.5, 1.5, 0, 4, 1 / math.sqrt(2))),
])
def test_known_answer_summaries_respect_fork_boundaries(samples_ms, expected):
    samples_ns = tuple(tuple(value * 1_000_000 for value in values) for values in samples_ms)
    original = _result(samples_ns)
    result = summarize(original)
    for actual, wanted in zip(_headlines(result), expected):
        assert actual is None if wanted is None else actual == pytest.approx(wanted)
    assert result.iterations is original.iterations and result.identity is original.identity
    assert tuple(fork.samples_ns for fork in result.forks) == samples_ns
    assert _headlines(original) == (None,) * 5
    assert all(fork.mean_ms is None for fork in original.forks)


def test_per_fork_summaries_and_median_are_not_pooled_iterations():
    result = summarize(_result(((1_000_000, 1_000_000, 10_000_000),
                                (2_000_000, 3_000_000, 25_000_000),
                                (4_000_000, 20_000_000, 24_000_000))))
    assert [(fork.mean_ms, fork.median_ms, fork.min_ms, fork.max_ms) for fork in result.forks] == [
        (4, 1, 1, 10), (10, 3, 2, 25), (16, 20, 4, 24)]
    assert result.median_of_fork_medians_ms == 3  # The pooled median would be 4.
    assert result.between_fork_mean_sd_ms == 6  # Population SD would be sqrt(24).


def test_warmups_operation_duration_and_outliers_never_change_sample_membership():
    original = _result(((1, 2, 999_999_999),), warmup_ns=10**12)
    result = summarize(original)
    assert result.forks[0].samples_ns == (1, 2, 999_999_999)
    assert result.sample_max_ms == 999.999999
    assert result.sample_min_ms == 0.000001
    assert result.forks[0].mean_ms == pytest.approx(1000.000002 / 3)
    assert result.operation_elapsed_s == 123 and result.comparison == "not_assessed"
    assert result.iterations[0].elapsed_ns == 10**12
    assert result.diagnostics == original.diagnostics


def test_one_nanosecond_and_zero_are_not_rounded_to_an_epsilon():
    assert _headlines(summarize(_result(((1,),)))) == (0.000001,) * 4 + (None,)
    result = summarize(_result(((0,), (0,))))
    assert _headlines(result) == (0, 0, 0, 0, 0)
    assert all("resolution" in item.diagnostics[-1]
               for item in result.iterations if item.kind == "measurement")


def test_small_between_fork_variation_survives_a_large_duration_offset():
    base = 2**61
    result = summarize(_result(((base, base), (base + 2, base + 2))))
    assert result.between_fork_mean_sd_ms == pytest.approx(math.sqrt(2) / 1_000_000)
    assert result.forks[0].samples_ns == (base, base)


def test_summaries_are_recomputed_from_raw_records_not_existing_headlines():
    original = _result(((1_000_000, 3_000_000),))
    stale = replace(original, mean_of_fork_means_ms=999, median_of_fork_medians_ms=999,
                    sample_min_ms=999, sample_max_ms=999,
                    forks=(replace(original.forks[0], mean_ms=999, median_ms=999,
                                   min_ms=999, max_ms=999),))
    result = summarize(stale)
    assert _headlines(result) == (2, 2, 1, 3, None)
    assert result.forks[0].mean_ms == result.forks[0].median_ms == 2
    assert result.forks[0].min_ms == 1 and result.forks[0].max_ms == 3
    assert stale.mean_of_fork_means_ms == stale.forks[0].mean_ms == 999


def test_stable_native_assumption_abort_remains_eligible():
    original = _result(((1, 3),))
    assumption = WarmedTestObservation("[engine:junit]/[method:assumption]", "assumption", "test", "aborted")
    iterations = tuple(replace(item, tests=item.tests + (assumption,),
                               counts=WarmedTestCounts(passed=1, aborted=1)) for item in original.iterations)
    result = summarize(replace(original, iterations=iterations))
    assert result.mean_of_fork_means_ms == 0.000002
    assert all(item.tests[-1].status == "aborted" for item in result.iterations)


@pytest.mark.parametrize("kind", ["warmup", "measurement"])
@pytest.mark.parametrize("counts", [WarmedTestCounts(passed=1, failed=1),
                                   WarmedTestCounts(passed=1, container_failures=1),
                                   WarmedTestCounts(passed=0)])
def test_summary_checks_native_count_consistency_before_calculation(monkeypatch, kind, counts):
    original = _result(((1_000_000, 3_000_000),))
    records = tuple(replace(item, counts=counts) if item.kind == kind else item
                    for item in original.iterations)
    # Exercise summary admission independently of construction of current records.
    object.__setattr__(original, "iterations", records)
    monkeypatch.setattr("langadapter.runs.mean",
                        lambda values: pytest.fail("contradictory native counts reached statistics"))
    with pytest.raises(InvalidRequest, match="counts"):
        summarize(original)
    assert original.iterations is records and _headlines(original) == (None,) * 5


def test_summary_uses_native_identities_when_aggregate_counts_are_unknown():
    original = _result(((1_000_000, 3_000_000),))
    records = tuple(replace(item, counts=WarmedTestCounts()) for item in original.iterations)
    result = summarize(replace(original, iterations=records))
    assert result.mean_of_fork_means_ms == 2
    assert all(item.counts == WarmedTestCounts() for item in result.iterations)


@pytest.mark.parametrize("validity,outcome,cancelled,disposed", [
    ("incomplete", Outcome.COMPLETED, False, True),
    ("incomplete", Outcome.TIMED_OUT, False, True),
    ("incomplete", Outcome.INFRASTRUCTURE_FAILURE, False, True),
    ("non_repeatable", Outcome.COMPLETED, False, True),
    ("incomplete", Outcome.COMPLETED, True, True),
    ("incomplete", Outcome.COMPLETED, False, False),
])
def test_ineligible_observations_retain_raw_records_without_a_summary(
        validity, outcome, cancelled, disposed):
    original = replace(_result(((1, 3), (5, 7))), validity=validity, outcome=outcome,
                       cancelled=cancelled, disposal_confirmed=disposed, reason="retained reason")
    result = summarize(original)
    assert result is original and result.reason == "retained reason"
    assert _headlines(result) == (None,) * 5
    assert all((fork.mean_ms, fork.median_ms, fork.min_ms, fork.max_ms) == (None,) * 4
               for fork in result.forks)


def test_partial_fork_retains_samples_but_neither_fork_gets_a_summary():
    original = _result(((1, 3), (5, 7)))
    partial = replace(original, validity="incomplete", outcome=Outcome.TIMED_OUT,
                      iterations=original.iterations[:-1], measurements_completed=3,
                      forks_completed=1,
                      forks=(original.forks[0], replace(original.forks[1], complete=False,
                                                       measurements_completed=1, samples_ns=(5,))))
    result = summarize(partial)
    assert result is partial and result.forks[1].samples_ns == (5,)
    assert result.forks[0].complete and result.forks[0].mean_ms is None
    assert _headlines(result) == (None,) * 5


def test_complete_native_failure_is_not_successful_timing():
    original = _result(((1, 3),), outcome=Outcome.TEST_FAILURES)
    assert original.validity == "complete" and original.iterations[0].tests[0].status == "failed"
    assert summarize(original) is original and _headlines(original) == (None,) * 5


@pytest.mark.parametrize("outcome", [Outcome.UNSUPPORTED, Outcome.MISSING_SETUP, Outcome.NO_TESTS])
def test_no_samples_remains_unavailable(outcome):
    original = replace(_result(((1,),)), validity="no_samples", outcome=outcome,
                       forks_started=0, forks_completed=0, warmup_completed=0,
                       measurements_completed=0, iterations=(), forks=())
    assert summarize(original) is original and _headlines(original) == (None,) * 5


def test_unrepresentable_summary_rejects_without_modifying_raw_evidence():
    original = _result(((10**400,),))
    with pytest.raises(InvalidRequest, match="samples_ns.*finite representable"):
        summarize(original)
    assert original.forks[0].samples_ns == (10**400,)
    assert _headlines(original) == (None,) * 5


@pytest.mark.parametrize("value", [None, {}, (), 0])
def test_statistics_requires_the_validated_record_type(value):
    with pytest.raises(InvalidRequest, match="result"):
        summarize(value)
