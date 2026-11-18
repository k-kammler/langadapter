"""Pure full normalization reports, without runtime or target validation."""
import dataclasses
import hashlib
import subprocess

import pytest

import langadapter as lb
from langadapter.sources import SourceStatus, SuiteNormalization


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("pure binding analysis must not launch a process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


EXAMPLES = [
    ("python", "def test_one():\n    assert False\n\ndef test_one():\n    assert True\n",
     "def test_one():\n    assert True\n", "def broken(:"),
    ("java", "class Example {\n void testOne() {}\n void testOne() { assert true; }\n}\n",
     "class Example { void testOne() { assert true; } }", "class Example {"),
]


@pytest.mark.parametrize("language, changed, unchanged, invalid", EXAMPLES)
@pytest.mark.parametrize("kind", ["changed", "unchanged", "invalid", "empty"])
def test_full_group_report_matches_native_without_runtime(
        tmp_path, monkeypatch, language, changed, unchanged, invalid, kind):
    adapter = lb.create(language, lb.ProjectContext(str(tmp_path)))
    def forbidden(*args, **kwargs):
        pytest.fail("static normalization must not validate against a target runtime")
    monkeypatch.setattr(adapter, "normalize_shadowed", forbidden)
    monkeypatch.setattr(adapter, "validate_source", forbidden)
    source = {"changed": changed, "unchanged": unchanged, "invalid": invalid, "empty": ""}[kind]
    native = adapter.normalize_bindings(source)
    report = adapter.sources.normalize_bindings(source)
    assert report.status is SourceStatus.COMPLETED
    assert report.value == native
    assert report.parser
    if language == "python":
        assert "Python ast" in report.parser and "tree-sitter" not in report.parser
    else:
        assert "tree-sitter-java" in report.parser
    assert native.before_sha256 == hashlib.sha256(source.encode()).hexdigest()
    assert native.after_sha256 == hashlib.sha256(native.source.encode()).hexdigest()
    assert native.changed is (kind == "changed")
    assert native.parse_ok is (kind != "invalid")
    if kind == "changed":
        assert native.removed_bindings and native.rule_version
        removed = native.removed_bindings[0]
        assert removed.name and removed.kind
        assert removed.removed_start_line < removed.retained_start_line
        again = adapter.sources.normalize_bindings(native.source)
        assert again.value.source == native.source and not again.value.changed
    if kind == "invalid":
        assert native.parse_error and native.source == source
        assert native.removed_bindings == ()
    with pytest.raises(dataclasses.FrozenInstanceError):
        native.source = "different"
    capability = next(item for item in adapter.operation_capabilities()
                      if item.name == "sources.normalize_bindings")
    assert capability.supported and capability.readiness is None
    assert not capability.execution_backed and capability.execution_kinds == ()


def test_java_missing_grammar_has_no_fabricated_report(tmp_path, monkeypatch):
    from langadapter import _treesitter
    def missing(*args, **kwargs):
        raise ModuleNotFoundError("tree_sitter_java is unavailable")
    monkeypatch.setattr(_treesitter, "parse", missing)
    adapter = lb.create("java", lb.ProjectContext(str(tmp_path)))
    report = adapter.sources.normalize_bindings("class Example {}")
    assert report.status is SourceStatus.MISSING_SETUP
    assert report.value is None and "tree_sitter_java" in report.reason


def test_python_binding_analysis_does_not_require_tree_sitter(tmp_path, monkeypatch):
    from langadapter import _treesitter
    def missing(*args, **kwargs):
        raise ModuleNotFoundError("structural grammar is unavailable")
    monkeypatch.setattr(_treesitter, "parse", missing)
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path)))
    report = adapter.sources.normalize_bindings("def test_one():\n    assert True\n")
    assert report.completed and report.value.parse_ok
    assert "Python ast" in report.parser


@pytest.mark.parametrize("error, status", [
    (OSError("analysis failed"), SourceStatus.INFRASTRUCTURE_FAILURE),
    (TimeoutError("analysis timed out"), SourceStatus.TIMEOUT),
])
def test_analysis_failure_has_no_empty_success(tmp_path, monkeypatch, error, status):
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path)))
    def failed(source):
        raise error
    monkeypatch.setattr(adapter, "normalize_bindings", failed)
    report = adapter.sources.normalize_bindings("source")
    assert report.status is status and report.value is None and report.reason


def test_independent_registration_and_unsupported_default(tmp_path):
    class Native(lb.LangAdapter):
        language = "normalization-fixture"
        file_extensions = (".normalization-fixture",)

        def normalize_bindings(self, source):
            digest = hashlib.sha256(source.encode()).hexdigest()
            return SuiteNormalization(source, False, True, None, (), digest, digest, "fixture-v1")

    lb.register(Native.language, Native.file_extensions, Native)
    try:
        adapter = lb.create(Native.language, lb.ProjectContext(str(tmp_path)))
        assert adapter.sources.normalize_bindings("source").value == adapter.normalize_bindings("source")
        report = lb.LangAdapter(lb.ProjectContext(str(tmp_path))).sources.normalize_bindings("source")
        assert report.status is SourceStatus.UNSUPPORTED and report.value is None
    finally:
        lb.unregister(Native.language)


@pytest.mark.parametrize("value", ["not a report", SuiteNormalization(
    "partial", True, False, "bad parse", (), "before", "after", "fixture-v1")])
def test_malformed_native_report_is_not_a_completed_observation(tmp_path, monkeypatch, value):
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path)))
    monkeypatch.setattr(adapter, "normalize_bindings", lambda source: value)
    report = adapter.sources.normalize_bindings("original")
    assert report.status is SourceStatus.INFRASTRUCTURE_FAILURE and report.value is None


def test_invalid_request_is_not_a_parse_failure(tmp_path):
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path)))
    with pytest.raises(lb.InvalidRequest, match="must be text"):
        adapter.sources.normalize_bindings(None)
