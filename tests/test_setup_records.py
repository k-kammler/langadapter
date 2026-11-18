"""Setup facts cannot substitute addresses, unknowns or labels for observations."""
from dataclasses import FrozenInstanceError, replace
import json
import subprocess

import pytest

import langadapter as lb
from langadapter import setup


@pytest.fixture
def context(tmp_path):
    return lb.ProjectContext(str(tmp_path), environment=lb.Environment({"API_KEY": "private-bare-value"}, inherit=False))


def request(context, **changes):
    return lb.ProjectSetupRequest(context, ["src/File.java"], ["tests.run_suite"], **changes)


def item(**changes):
    values = {"key": "runtime", "required": True, "readiness": None, "outcome": None}
    return lb.ProjectSetupItem(**(values | changes))


def report(context=None, **changes):
    values = {"scope": "inspection", "outcome": lb.Outcome.COMPLETED, "reason": "", "context": context,
              "inputs": None, "items": [item()], "operations": [lb.OperationCapability("tests.run_suite", True)]}
    return lb.ProjectSetupReport(**(values | changes))


def test_request_copies_inputs_has_no_context_export_and_reuses_no_clock(context):
    sources, operations = ["src/File.java"], ["tests.run_suite"]
    value = lb.ProjectSetupRequest(context, sources, operations, lb.ProjectInputSelection([".venv"]))
    sources.append("changed.java")
    operations.append("mutations.run")
    assert value.source_files == ("src/File.java",) and value.required_operations == ("tests.run_suite",)
    assert value._deadline is None and "private-bare-value" not in repr(value)
    assert str(context.root) not in repr(value)
    with pytest.raises(FrozenInstanceError):
        value.allow_acquisition = True
    with pytest.raises(lb.InvalidRequest, match="establish its deadline"):
        value.remaining_timeout()


@pytest.mark.parametrize("field,value", [
    ("source_files", "x.py"), ("source_files", ["../x.py"]), ("source_files", ["x\\y.py"]),
    ("source_files", ["C:/x.py"]), ("source_files", ["/x.py"]), ("source_files", ["./x.py"]),
    ("required_operations", "tests.run_suite"), ("required_operations", ["not_a_group"]),
    ("required_operations", ["tests.run_suite", "tests.run_suite"]), ("required_operations", None),
    ("context", {}), ("inputs", {}), ("allow_acquisition", 1), ("allow_acquisition", "false"),
    ("probe_timeout_s", 0), ("probe_timeout_s", -1), ("probe_timeout_s", True),
    ("probe_timeout_s", float("nan")), ("total_timeout_s", float("inf")), ("total_timeout_s", "120"),
])
def test_request_rejects_invalid_paths_scope_and_bounds(context, field, value):
    values = {"context": context, "source_files": [], "required_operations": []}
    with pytest.raises(lb.InvalidRequest):
        lb.ProjectSetupRequest(**(values | {field: value}))


def test_required_facets_are_normalized_hashable_and_survive_replace(context):
    value = request(context, required_facets={"tests.run_suite": ["coverage"]})
    assert value.required_facets == (("tests.run_suite", ("coverage",)),)
    assert value.facets("tests.run_suite") == ("coverage",) and value.facets("tests.collect_suite") == ()
    assert replace(value, probe_timeout_s=5).required_facets == value.required_facets
    assert hash(value.required_facets) and "coverage" in repr(value)
    assert request(context).required_facets == ()


@pytest.mark.parametrize("facets", [
    {"tests.collect_suite": ("coverage",)},       # not a required operation
    {"tests.run_suite": ("mutation",)},           # not a facet the operation offers
    {"tests.run_suite": ()},                      # an empty requirement is no requirement
    {"tests.run_suite": "coverage"},              # a bare string is not a facet sequence
    (("tests.run_suite", ("coverage",)), ("tests.run_suite", ("coverage",))),
    "tests.run_suite", 1,
])
def test_required_facets_refuse_unknown_or_unrequired_measurements(context, facets):
    with pytest.raises(lb.InvalidRequest, match="required_facets"):
        request(context, required_facets=facets)


def test_assessment_deadline_is_entry_scoped_caps_probes_and_disables_inspection_acquisition(context, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(setup.time, "monotonic", lambda: clock[0])
    original = request(context, total_timeout_s=12, probe_timeout_s=5, allow_acquisition=True)
    inspection = original._for_assessment(inspection=True)
    assert inspection.context is original.context and inspection.allow_acquisition is False
    assert original.allow_acquisition is True and original._deadline is None
    assert inspection.remaining_timeout() == 5
    assert inspection.remaining_timeout("owned setup", probe=False) == 12
    with pytest.raises(lb.InvalidRequest, match="probe"):
        inspection.remaining_timeout(probe="false")
    clock[0] += 10
    assert inspection.remaining_timeout() == 2
    assert inspection.remaining_timeout("owned setup", probe=False) == 2
    verification = original._for_assessment(inspection=False)
    assert verification.allow_acquisition is True and verification.remaining_timeout() == 5
    clock[0] += 2
    with pytest.raises(subprocess.TimeoutExpired):
        inspection.remaining_timeout("entry deadline")
    assert verification.remaining_timeout() == 5


@pytest.mark.parametrize("changes", [
    {"key": ""}, {"required": 1}, {"ambiguous": "true"}, {"readiness": "available"},
    {"outcome": "completed"}, {"readiness": lb.Availability.AVAILABLE},
    {"readiness": lb.Availability.AVAILABLE, "outcome": lb.Outcome.INFRASTRUCTURE_FAILURE},
    {"version": "3.12"}, {"selected": "/python", "version": "3.12"},
    {"version": "3.12", "outcome": lb.Outcome.COMPLETED},
    {"ambiguous": True, "selected": "/python"}, {"ambiguous": True, "version": "3.12"},
    {"ambiguous": True, "readiness": lb.Availability.AVAILABLE, "outcome": lb.Outcome.COMPLETED},
    {"candidates": "/python"}, {"candidates": ["x", "x"]}, {"selected": ""}, {"reason": None},
])
def test_items_refuse_invented_or_inconsistent_facts(changes):
    with pytest.raises(lb.InvalidRequest):
        item(**changes)


def test_unknown_candidate_is_not_probed_and_optional_failure_does_not_erase_selection(context):
    candidate = item(selected="/selected/python", candidates=["/selected/python"])
    assert candidate.readiness is None and candidate.outcome is None and candidate.version is None
    missing = item(key="optional-tool", required=False, readiness=lb.Availability.MISSING_SETUP,
                   outcome=lb.Outcome.MISSING_SETUP, reason="no tool")
    value = report(context, items=[candidate, missing])
    assert value.outcome is lb.Outcome.COMPLETED and value.context is context
    assert value.items[1].outcome is lb.Outcome.MISSING_SETUP
    assert value.operations[0].readiness is None


def test_observed_version_requires_completed_selection_and_preserves_genuine_version_zero():
    value = item(selected="/tool", version="0", outcome=lb.Outcome.COMPLETED,
                 readiness=lb.Availability.AVAILABLE)
    assert value.version == "0"
    assert replace(value, readiness=None).version == "0"


@pytest.mark.parametrize("changes", [
    {"scope": "provisioning"}, {"outcome": "completed"}, {"context": {}}, {"inputs": {}},
    {"items": None}, {"items": [{}]}, {"items": [item(), item()]},
    {"operations": ["tests.run_suite"]}, {"operations": None},
    {"operations": [lb.OperationCapability("tests.run_suite", True)] * 2},
    {"diagnostic_paths": "x"}, {"diagnostic_paths": ["x", "x"]},
])
def test_report_validation_never_fabricates_a_record(changes):
    with pytest.raises(lb.InvalidRequest):
        report(**changes)


def test_required_ambiguity_refuses_context_optional_ambiguity_remains_a_fact(context):
    ambiguous = item(ambiguous=True, candidates=["a", "b"], outcome=lb.Outcome.COMPLETED)
    with pytest.raises(lb.InvalidRequest, match="ambiguous"):
        report(context, items=[ambiguous])
    assert report(None, items=[ambiguous]).context is None
    assert report(context, items=[replace(ambiguous, required=False)]).context is context


def test_report_copies_sequences_and_exports_detached_lists(context):
    items, operations, paths = [item()], [lb.OperationCapability("tests.run_suite", True)], ["/diagnostic"]
    value = report(context, items=items, operations=operations, diagnostic_paths=paths,
                   inputs=lb.ProjectInputSelection([".venv"], ["src/Target.java"]))
    items.clear()
    operations.clear()
    paths.clear()
    output = value.safe_export()
    output["items"].clear()
    output["operations"].clear()
    output["inputs"]["required"].clear()
    assert len(value.items) == len(value.operations) == len(value.diagnostic_paths) == 1
    assert value.inputs.required == ("src/Target.java",)
    with pytest.raises(FrozenInstanceError):
        value.context = None


def test_secret_safe_export_omits_context_native_output_and_redacts_labeled_and_known_secrets(context):
    secret = "private-bare-value"
    text = "known=" + secret + " password=otherpass Bearer otherbearer https://user:otherpass@host/path?api_key=querykey"
    value = report(context, reason=text, items=[item(reason=text, action=text, selected="/" + secret,
                                                    candidates=["/" + secret])],
                   operations=[lb.OperationCapability("tests.run_suite", True, reason=text, action=text)],
                   diagnostic_paths=["/" + secret])
    output = json.dumps(value.safe_export())
    assert value.safe_export()["operations"][0]["action"].startswith("known=[redacted]")
    for forbidden in (secret, "otherpass", "otherbearer", "querykey", "API_KEY", str(context.root)):
        assert forbidden not in output and forbidden not in repr(value) and forbidden not in repr(value.items[0])
    assert "context" not in value.safe_export() and "native_output" not in value.safe_export()
    assert value.context is context and value.reason == text


def test_a_short_sensitive_value_is_redacted_only_in_place_and_a_long_one_everywhere(context):
    """A value of 6 characters or more is redacted wherever it occurs. A
    shorter one, such as "cache" or "1", only where a pattern finds a
    secret, so paths and versions that contain it stay readable."""
    context = replace(context, environment=lb.Environment(
        {"CACHE_TOKEN": "cache", "DEBUG_SECRET": "1", "API_KEY": "s3cret"}, inherit=False))
    text = "/home/cache/python 3.10.1 token=cache s3cret/x"
    value = report(context, reason=text, items=[item(outcome=lb.Outcome.COMPLETED, selected="/home/cache/python",
                                                     version="3.10.1")])
    output = value.safe_export()
    assert output["reason"] == "/home/cache/python 3.10.1 token=[redacted] [redacted]/x"
    assert output["items"][0]["selected"] == "/home/cache/python" and output["items"][0]["version"] == "3.10.1"


def test_control_secrets_do_not_escape_report(context, tmp_path):
    execution = lb.ContainerExecution("fixture", str(tmp_path), control_environment={"ACCESS_TOKEN": "control-private"})
    context = replace(context, execution=execution)
    value = report(context, reason="control-private", items=[item(reason="control-private")])
    assert "control-private" not in json.dumps(value.safe_export())


@pytest.mark.parametrize("authorization", ["Authorization: Basic other-private-credential", "authorization=basic other-private-credential",
    'Authorization: Basic "other-private-credential"', "Authorization: Bearer other-private-credential"])
def test_safe_export_consumes_full_labeled_authorization_preserving_raw_observation(context, authorization):
    value = report(context, reason=authorization, items=[item(reason=authorization, action=authorization)])
    output = json.dumps(value.safe_export())
    assert "other-private-credential" not in output and "[redacted]" in output
    assert value.reason == authorization and value.items[0].reason == authorization


@pytest.mark.parametrize("field", ["execution_kinds", "evidence_granularity", "resolution_methods"])
def test_every_operation_metadata_string_passes_the_existing_known_secret_redactor(context, field):
    raw = ("private-bare-value", "ordinary", "Authorization: Basic other-private-credential")
    operation = lb.OperationCapability("tests.run_suite", True, **{field: raw})
    value = report(context, operations=[operation])
    output = value.safe_export()
    assert output["operations"][0][field] == ["[redacted]", "ordinary", "Authorization: [redacted]"]
    output["operations"][0][field].clear()
    assert getattr(operation, field) is raw


def test_ordinary_safe_export_status_version_path_and_metadata_values_stay_exact(context):
    operation = lb.OperationCapability("tests.run_suite", True, readiness=lb.Availability.AVAILABLE,
        execution_backed=True, execution_kinds=("local",), evidence_granularity=("per_test",), resolution_methods=("measured",))
    value = report(context, items=[item(outcome=lb.Outcome.COMPLETED, selected="/tools/python", version="3.10.19")],
        operations=[operation], diagnostic_paths=("/diagnostic/report.json",))
    output = value.safe_export()
    assert output["items"][0]["version"] == "3.10.19" and output["items"][0]["selected"] == "/tools/python"
    assert output["diagnostic_paths"] == ["/diagnostic/report.json"]
    assert output["operations"][0] == {"name": "tests.run_suite", "supported": True, "reason": "",
        "readiness": "available", "execution_backed": True, "execution_kinds": ["local"],
        "evidence_granularity": ["per_test"], "resolution_methods": ["measured"], "action": None,
        "tried_by": None}
    waiting = report(context, items=[item(outcome=lb.Outcome.COMPLETED, tried_by="run")],
                     operations=[lb.OperationCapability("tests.run_suite", True, tried_by="verification")])
    assert waiting.safe_export()["items"][0]["tried_by"] == "run"
    assert waiting.safe_export()["operations"][0]["tried_by"] == "verification"


@pytest.mark.parametrize("changes", [{"tried_by": "later"}, {"tried_by": "run", "readiness": lb.Availability.AVAILABLE,
    "outcome": lb.Outcome.COMPLETED}, {"tried_by": "run", "outcome": lb.Outcome.INFRASTRUCTURE_FAILURE},
    {"tried_by": "verification", "readiness": lb.Availability.MISSING_SETUP, "outcome": lb.Outcome.MISSING_SETUP},
    {"tried_by": "run", "ambiguous": True, "candidates": ("maven", "gradle")}])
def test_only_an_item_not_tried_yet_names_what_tries_it(changes):
    with pytest.raises(lb.InvalidRequest, match="tried_by"):
        item(**changes)


@pytest.mark.parametrize("scope", ["inspection", "verification"])
def test_optional_registered_adapter_hooks_return_honest_unsupported_without_abstract_burden(context, tmp_path, scope):
    class Minimal(lb.LangAdapter):
        language = "setup-record-fixture"

    # The selected input exists, so the assessment reaches the adapter's hook.
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "File.java").write_text("class File {}\n")
    lb.register(Minimal.language, [".setup-record-fixture"], Minimal)
    try:
        adapter = lb.create(Minimal.language, context)
        value = getattr(adapter, "inspect_project" if scope == "inspection" else "verify_project")(request(context))
        assert value.scope == scope and value.outcome is lb.Outcome.UNSUPPORTED
        assert value.context is None and value.items[0].required is True
        assert all(operation.readiness is not lb.Availability.AVAILABLE for operation in value.operations)
        # Without operations or a setup check, the plain next step says so.
        assert value.items[0].action == "none for now: the setup-record-fixture adapter has no setup check"
        # An operation the adapter does not implement has no next step.
        assert {operation.action for operation in value.operations} == {None}
    finally:
        lb.unregister(Minimal.language)
