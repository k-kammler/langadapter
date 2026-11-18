"""The outcome vocabulary and the identity of observations."""
import json

import pytest

import langadapter as lb


def test_the_outcome_vocabulary():
    assert [o.value for o in lb.Outcome] == [
        "completed", "test_failures", "no_tests", "collection_failed",
        "timed_out", "infrastructure_failure", "unsupported", "missing_setup",
    ]
    assert str(lb.Outcome.NO_TESTS) == "no_tests"
    assert lb.Outcome("timed_out") is lb.Outcome.TIMED_OUT


def _identity(suite_order, *, image=None):
    return lb.ObservationIdentity(
        target=lb.FileIdentity("xarray/core/groupby.py", "a" * 64),
        suite=tuple(lb.FileIdentity(p, "b" * 64) for p in suite_order),
        toolchain=lb.ToolchainIdentity(
            "CPython", "3.10.16",
            tools=(("pytest", "8.1.1"), ("coverage", "7.6.8"))),
        instrument=lb.InstrumentIdentity("determinism", "7"),
        execution=lb.ExecutionIdentity("container", image=image),
    )


def test_the_same_observation_has_the_same_identity_everywhere():
    first = _identity(["test_groupby.py", "conftest.py"], image="sweb:1")
    second = _identity(["conftest.py", "test_groupby.py"], image="sweb:1")
    assert first == second
    record = first.to_dict()
    assert json.loads(json.dumps(record)) == record
    assert record["toolchain"]["tools"] == {"coverage": "7.6.8",
                                           "pytest": "8.1.1"}
    assert [f["path"] for f in record["suite"]] == ["conftest.py",
                                                    "test_groupby.py"]
    assert first != _identity(["test_groupby.py", "conftest.py"])


def test_an_execution_identity_is_local_or_container():
    with pytest.raises(lb.InvalidRequest):
        lb.ExecutionIdentity("cluster")


def test_an_execution_identity_records_its_supervision_mode_when_known():
    """A container without pidfd records its supervision mode. A local
    identity records none."""
    assert lb.ExecutionIdentity("local").to_dict() == {"kind": "local", "image": None}
    assert lb.ExecutionIdentity("container", "img", "process_group").to_dict() == {
        "kind": "container", "image": "img", "supervision": "process_group"}
    with pytest.raises(lb.InvalidRequest, match="supervision"):
        lb.ExecutionIdentity("local", None, "threads")


def test_the_supervision_mode_follows_the_route(monkeypatch, tmp_path):
    import sys

    from langadapter import _linux_command, _linux_container, _linux_job
    from langadapter.execution import execution_identity, supervision_of

    local = lb.ProjectContext(str(tmp_path), lb.Interpreter(sys.executable))
    monkeypatch.setattr(_linux_command, "_LOCAL_SUPERVISION", [])
    monkeypatch.setattr(_linux_command, "_supported_host", lambda: False)
    assert supervision_of(local) is None
    monkeypatch.setattr(_linux_command, "_supported_host", lambda: True)
    monkeypatch.setattr(_linux_job, "_pidfd_works", lambda: False)
    assert execution_identity(local) == lb.ExecutionIdentity("local", None, "process_group")
    bare = lb.ProjectContext("/repo", lb.Interpreter("/venv/bin/python"),
                             execution=lb.ContainerExecution("c1", str(tmp_path)))
    assert supervision_of(bare) is None
    helped = lb.ProjectContext("/repo", lb.Interpreter("/venv/bin/python"),
                               execution=lb.ContainerExecution(
                                   "c1", str(tmp_path), helper_python="/venv/bin/python"))
    monkeypatch.setattr(_linux_container, "_SUPERVISION", {})
    # Before the helper's first report a supervised route reads as
    # unreported, not as unsupervised.
    assert supervision_of(helped) == "unreported"
    assert execution_identity(helped).to_dict() != execution_identity(bare).to_dict()
    _linux_container._SUPERVISION[("docker", "c1", "/venv/bin/python")] = "process_group"
    assert execution_identity(helped).supervision == "process_group"


def test_the_errors_are_public_types():
    refused = lb.InvalidRequest("timeout", "must not be negative")
    assert isinstance(refused, lb.LangAdapterError)
    assert isinstance(refused, ValueError)
    assert refused.field == "timeout"
    assert str(refused) == "timeout: must not be negative"
    assert isinstance(lb.UnsupportedLanguage("x", ["b", "a"]), LookupError)
    assert lb.UnsupportedLanguage("x", ["b", "a"]).registered == ("a", "b")
