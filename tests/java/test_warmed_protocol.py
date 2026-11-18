"""Native protocol controls, without a JVM or public warmed-operation claim."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from langadapter.java import _warmed_fork as helper, _warmed as native
from langadapter.outcomes import Outcome


@pytest.fixture
def protocol():
    return native.ForkProtocol("a" * 32, 1, 1, 2)


def report(protocol, event, index=None, **changes):
    value = {
        "schema": 1, "token": protocol.token, "fork": protocol.fork, "event": event,
        "jvm_name": "67@fixture", "jvm_start_ms": 1000,
    }
    if event == "ready":
        value.update(warmup_runs=protocol.warmup_runs, runs=protocol.runs)
    elif event == "complete":
        value.update(outcome="completed", iterations_completed=protocol.warmup_runs + protocol.runs)
    else:
        kind, repetition = protocol.location(index)
        value.update(index=index, kind=kind, repetition=repetition)
        if event == "iteration":
            value.update(
                elapsed_ns=123, outcome="completed", report_complete=True, reason="",
                diagnostics=[], counts={
                    "discovered": 1, "started": 1, "passed": 1, "failed": 0,
                    "skipped": 0, "aborted": 0, "container_failures": 0,
                }, tests=[{
                    "unique_id": "[engine:junit-jupiter]/[class:WidgetTest]/[method:value()]",
                    "display_name": "value()", "kind": "test", "status": "successful", "reason": "",
                }, {
                    "unique_id": "[engine:junit-jupiter]", "display_name": "JUnit Jupiter",
                    "kind": "container", "status": "successful", "reason": "",
                }])
    value.update(changes)
    return value


def decode(protocol, value):
    return native.decode(json.dumps(value, ensure_ascii=False).encode("utf-8"), protocol)


def accept_iteration(transcript, index, **changes):
    protocol = transcript.protocol
    transcript.accept(decode(protocol, report(protocol, "start", index)))
    transcript.accept(decode(protocol, report(protocol, "iteration", index, **changes)))


def admitted(protocol):
    transcript = native.Transcript(protocol)
    transcript.accept(decode(protocol, report(protocol, "ready")))
    return transcript


def test_complete_transcript_retains_same_jvm_raw_records_without_disposal_claim(protocol):
    transcript = admitted(protocol)
    for index in range(3):
        accept_iteration(transcript, index, elapsed_ns=index)
    transcript.accept(decode(protocol, report(protocol, "complete")))
    assert transcript.finished and transcript.outcome is Outcome.COMPLETED
    assert transcript.jvm == ("67@fixture", 1000)
    assert transcript.repeatable is True
    assert [(record.kind, record.repetition, record.elapsed_ns) for record in transcript.iterations] == [
        ("warmup", 0, 0), ("measurement", 0, 1), ("measurement", 1, 2)]
    assert "resolution" in transcript.iterations[0].diagnostics[0]
    assert not hasattr(transcript, "disposal_confirmed")


@pytest.mark.parametrize("event", ["ready", "start", "iteration", "complete"])
@pytest.mark.parametrize("field,value", [
    ("schema", True), ("schema", 2), ("token", "b" * 32), ("fork", True),
    ("fork", 2), ("jvm_name", ""), ("jvm_start_ms", -1), ("jvm_start_ms", 1.2),
])
def test_envelope_refuses_cross_channel_missing_and_malformed_receipts(protocol, event, field, value):
    payload = report(protocol, event, 0)
    payload[field] = value
    with pytest.raises(native.ProtocolError):
        decode(protocol, payload)


@pytest.mark.parametrize("event", ["ready", "start", "iteration", "complete"])
@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_exact_report_fields_never_refill_missing_data(protocol, event, mutation):
    payload = report(protocol, event, 0)
    if mutation == "missing":
        del payload["jvm_start_ms"]
    else:
        payload["disposal_confirmed"] = True
    with pytest.raises(native.ProtocolError):
        decode(protocol, payload)


@pytest.mark.parametrize("field,value", [
    ("elapsed_ns", -1), ("elapsed_ns", True), ("elapsed_ns", 1.5),
    ("elapsed_ns", 2**63), ("elapsed_ns", None), ("report_complete", 1),
    ("outcome", "timed_out"), ("outcome", "unknown"), ("tests", {}),
    ("diagnostics", "warning"), ("kind", "measurement"), ("repetition", True),
    ("index", True), ("reason", None),
])
def test_iteration_shape_and_native_duration_are_not_guessed(protocol, field, value):
    payload = report(protocol, "iteration", 0)
    payload[field] = value
    with pytest.raises(native.ProtocolError):
        decode(protocol, payload)


@pytest.mark.parametrize("count", ["passed", "failed", "skipped", "aborted", "container_failures"])
def test_known_terminal_count_must_match_identities(protocol, count):
    payload = report(protocol, "iteration", 0)
    payload["counts"][count] += 1
    with pytest.raises(native.ProtocolError, match="contradicts"):
        decode(protocol, payload)


def test_unknown_counts_and_dynamic_discovery_remain_native(protocol):
    payload = report(protocol, "iteration", 0)
    payload["counts"] = {key: None for key in payload["counts"]}
    payload["counts"].update(discovered=25, started=2)
    iteration = decode(protocol, payload).iteration
    assert iteration.counts.discovered == 25
    assert iteration.counts.started == 2
    assert iteration.counts.passed is None
    assert len(iteration.tests) == 2


@pytest.mark.parametrize("mutation", ["missing_count", "extra_count", "duplicate_id", "bad_status",
                                     "bad_kind", "nontext_name", "bad_count", "negative_count"])
def test_incomplete_or_invalid_test_evidence_is_rejected(protocol, mutation):
    payload = report(protocol, "iteration", 0)
    if mutation == "missing_count":
        del payload["counts"]["failed"]
    elif mutation == "extra_count":
        payload["counts"]["free"] = 0
    elif mutation == "duplicate_id":
        payload["tests"].append(deepcopy(payload["tests"][0]))
    elif mutation == "bad_count":
        payload["counts"]["started"] = False
    elif mutation == "negative_count":
        payload["counts"]["started"] = -1
    elif mutation == "nontext_name":
        payload["tests"][0]["display_name"] = None
    elif mutation == "bad_status":
        payload["tests"][0]["status"] = "passed"
    else:
        payload["tests"][0]["kind"] = "both"
    with pytest.raises(native.ProtocolError):
        decode(protocol, payload)


@pytest.mark.parametrize("outcome", ["test_failures", "no_tests"])
def test_native_verdict_cannot_contradict_green_test_evidence(protocol, outcome):
    with pytest.raises(native.ProtocolError):
        decode(protocol, report(protocol, "iteration", 0, outcome=outcome))


@pytest.mark.parametrize("outcome,status,kind", [
    ("test_failures", "failed", "test"), ("test_failures", "failed", "container"),
    ("no_tests", "skipped", "test"), ("no_tests", "aborted", "test"),
])
def test_failed_empty_and_aborted_native_evidence_is_retained(protocol, outcome, status, kind):
    payload = report(protocol, "iteration", 0, outcome=outcome)
    payload["tests"] = [dict(payload["tests"][0], kind=kind, status=status, reason="native reason")]
    payload["counts"] = dict.fromkeys(payload["counts"], 0)
    if kind == "container":
        payload["counts"]["container_failures"] = 1
    else:
        key = {"failed": "failed", "skipped": "skipped", "aborted": "aborted"}[status]
        payload["counts"][key] = 1
    transcript = admitted(protocol)
    transcript.accept(decode(protocol, report(protocol, "start", 0)))
    transcript.accept(decode(protocol, payload))
    transcript.accept(decode(protocol, report(protocol, "complete", outcome=outcome,
                                             iterations_completed=1)))
    assert transcript.outcome.value == outcome
    assert transcript.iterations[0].tests[0].reason == "native reason"


def test_discovery_exception_preserves_unknowns_and_no_timing(protocol):
    payload = report(protocol, "iteration", 0, outcome="collection_failed", report_complete=False,
                     elapsed_ns=None, tests=[], diagnostics=["native stack"], reason="discovery error")
    payload["counts"] = dict.fromkeys(payload["counts"], None)
    transcript = admitted(protocol)
    transcript.accept(decode(protocol, report(protocol, "start", 0)))
    transcript.accept(decode(protocol, payload))
    transcript.accept(decode(protocol, report(protocol, "complete", outcome="collection_failed",
                                             iterations_completed=0)))
    assert transcript.iterations[0].counts.started is None
    assert transcript.iterations[0].elapsed_ns is None
    assert transcript.iterations[0].diagnostics == ("native stack",)


@pytest.mark.parametrize("stage", ["no_ready", "double_ready", "skip_start", "overlap_start",
                                  "wrong_index", "changed_jvm", "early_complete", "lost_record",
                                  "wrong_completed_count", "post_complete"])
def test_ordered_protocol_never_invents_lost_or_extra_samples(protocol, stage):
    transcript = native.Transcript(protocol) if stage == "no_ready" else admitted(protocol)
    with pytest.raises(native.ProtocolError):
        if stage in ("no_ready", "wrong_index"):
            transcript.accept(decode(protocol, report(protocol, "start", 1)))
        elif stage == "double_ready":
            transcript.accept(decode(protocol, report(protocol, "ready")))
        elif stage == "skip_start":
            transcript.accept(decode(protocol, report(protocol, "iteration", 0)))
        elif stage == "changed_jvm":
            transcript.accept(decode(protocol, report(protocol, "start", 0, jvm_start_ms=1001)))
        elif stage in ("early_complete", "wrong_completed_count"):
            accept_iteration(transcript, 0)
            transcript.accept(decode(protocol, report(protocol, "complete", iterations_completed=1
                                                     if stage == "early_complete" else 2)))
        elif stage in ("lost_record", "overlap_start"):
            transcript.accept(decode(protocol, report(protocol, "start", 0)))
            transcript.accept(decode(protocol, report(protocol, "complete") if stage == "lost_record"
                                     else report(protocol, "start", 1)))
        else:
            for index in range(3):
                accept_iteration(transcript, index)
            terminal = decode(protocol, report(protocol, "complete"))
            transcript.accept(terminal)
            transcript.accept(terminal)


@pytest.mark.parametrize("mutation", ["identity", "skip", "container", "dynamic_parent"])
def test_repeatability_compares_native_ids_and_status_not_aggregate_counts(protocol, mutation):
    transcript = admitted(protocol)
    accept_iteration(transcript, 0)
    payload = report(protocol, "iteration", 1)
    if mutation in ("identity", "dynamic_parent"):
        payload["tests"][0]["unique_id"] += "/[dynamic-container:other]/[dynamic-test:1]"
    elif mutation == "container":
        payload["tests"][1]["status"] = "aborted"
    else:
        payload["tests"].append(dict(payload["tests"][0], unique_id="disabled", status="skipped"))
        payload["counts"]["skipped"] = 1
    transcript.accept(decode(protocol, report(protocol, "start", 1)))
    transcript.accept(decode(protocol, payload))
    assert transcript.repeatable is False
    assert len(transcript.iterations) == 2


def test_stable_assumption_abort_with_passing_test_is_not_a_failure(protocol):
    transcript = admitted(protocol)
    for index in range(3):
        payload = report(protocol, "iteration", index)
        payload["tests"].append(dict(payload["tests"][0], unique_id="assumption", status="aborted"))
        payload["counts"]["aborted"] = 1
        transcript.accept(decode(protocol, report(protocol, "start", index)))
        transcript.accept(decode(protocol, payload))
    transcript.accept(decode(protocol, report(protocol, "complete")))
    assert transcript.repeatable is True and transcript.outcome is Outcome.COMPLETED


@pytest.mark.parametrize("data", [b"", b"{", b"\xff", b"[]", b"null",
                                 b'{"event":"ready","event":"ready"}',
                                 b'{"schema":NaN}', b'{"schema":Infinity}'])
def test_malformed_json_is_never_a_native_observation(protocol, data):
    with pytest.raises(native.ProtocolError):
        native.decode(data, protocol)


def test_report_limit_and_unicode_roundtrip(protocol, monkeypatch, tmp_path):
    payload = report(protocol, "iteration", 0)
    payload["tests"][0]["display_name"] = 'value " \\ \n \t \u2603 \U0001f600'
    path = tmp_path / "iteration-0.json"
    path.write_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    assert native.read(path, protocol).iteration.tests[0].display_name == payload["tests"][0]["display_name"]
    monkeypatch.setattr(native, "MAX_REPORT_BYTES", 5)
    with pytest.raises(native.ProtocolError, match="limit"):
        native.read(path, protocol)


def test_directory_report_refused(protocol, tmp_path):
    with pytest.raises(native.ProtocolError, match="regular"):
        native.read(tmp_path, protocol)


def test_report_replacement_during_open_is_not_adopted(protocol, tmp_path, monkeypatch):
    path = tmp_path / "ready.json"
    path.write_bytes(json.dumps(report(protocol, "ready")).encode())
    original = path.lstat()

    class OtherIdentity:
        st_dev = original.st_dev
        st_ino = original.st_ino + 1

    monkeypatch.setattr(native.os, "fstat", lambda descriptor: OtherIdentity())
    with pytest.raises(native.ProtocolError, match="replaced"):
        native.read(path, protocol)


def test_helper_source_keeps_native_execute_boundary_and_fresh_plan():
    source = helper.HELPER_SOURCE
    loop = source[source.index("for (int index = 0;"):source.index('Map<String, Object> terminal')]
    assert source.count("LauncherFactory.create()") == 1
    assert loop.count("launcher.discover(request.build())") == 1
    timed = loop[loop.index("long start = System.nanoTime();"):loop.index("complete = observation")]
    assert "launcher.execute(plan, observation)" in timed
    assert "elapsed = System.nanoTime() - start" in timed
    assert not any(word in timed for word in ("publish(", "discover(", "json(", "awaitPermit("))
    assert "awaitPermit(index)" in loop
    assert "dynamicTestRegistered" in source
    assert "synchronized void executionFinished" in source
    assert not any(word in source for word in ("System.gc(", "URLClassLoader", "Path.of(", "Files.writeString("))
    assert helper.HELPER_CLASS == "LangAdapterWarmed"


@pytest.mark.parametrize("arguments", [("a", 0, 1, 1), ("g" * 32, 0, 1, 1),
                                       ("a" * 32, True, 1, 1), ("a" * 32, 0, 0, 1),
                                       ("a" * 32, 0, 1, False)])
def test_protocol_admission_rejects_invalid_channel_and_budgets(arguments):
    with pytest.raises(native.ProtocolError):
        native.ForkProtocol(*arguments)


def test_unrequested_location_refused(protocol):
    with pytest.raises(native.ProtocolError, match="budget"):
        protocol.location(3)


def test_report_reads_do_not_execute_tools(protocol, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("native process creation is outside protocol parsing")

    import subprocess
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    path = Path(tmp_path) / "ready.json"
    path.write_bytes(json.dumps(report(protocol, "ready")).encode())
    assert native.read(path, protocol).event == "ready"
