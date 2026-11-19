"""Collision grouping distinguishes native rejections from unavailable validation."""
import subprocess
import sys

import pytest

import langadapter as lb
from langadapter.sources import (
    CollisionResolution, SourceObservation, SourceStatus, SourceValidation,
)


SOURCES = {
    "python": ("HELPER = 1\ndef shared():\n    return 0\ndef test_a():\n    assert HELPER\n",
               "HELPER = 2\ndef shared():\n    return 0\ndef test_b():\n    assert HELPER\n"),
    "java": ("class First { static int helper() { return 1; } @Test void first() { assert true; } }",
             "class Second { static int helper() { return 2; } @Test void second() { assert true; } }"),
}


def native_adapter(tmp_path, monkeypatch, language, validator=None):
    adapter = lb.create(language, lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    # Both languages' source operations are their adapter's methods.
    operations = adapter
    verdict = validator or (lambda source: SourceValidation("valid", "", 0, 0, "fixture-validator"))
    if language == "python":
        monkeypatch.setattr(operations, "validate_source", verdict)
    else:
        monkeypatch.setattr(operations, "_validate_sources", lambda sources: [verdict(source) for source in sources])
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("fixture launched validator"))
    return adapter


@pytest.mark.parametrize("language", ["python", "java"])
@pytest.mark.parametrize("localize", [False, True])
def test_group_retains_native_renames_sharing_and_localization(tmp_path, monkeypatch, language, localize):
    adapter = native_adapter(tmp_path, monkeypatch, language)
    first, second = SOURCES[language]
    result = adapter.sources.resolve_collisions([first, second], localize_autouse=localize)
    native = adapter.resolve_collisions([first, second], localize_autouse=localize)
    assert result.completed and result.value == native.value and result.value is not native.value
    assert result.value.renames[1]
    assert result.parser == native.parser


@pytest.mark.parametrize("language", ["python", "java"])
def test_judged_invalid_source_is_a_completed_rejection(tmp_path, monkeypatch, language):
    first, second = SOURCES[language]
    adapter = native_adapter(tmp_path, monkeypatch, language, lambda source: SourceValidation(
        "syntax_error" if source == second else "valid", "compiler rejection", 1, 1, "fixture-validator"))
    result = adapter.sources.resolve_collisions([first, second])
    assert result.completed
    assert result.value.ejected == (1,)
    assert result.value.sources == (first, second)


@pytest.mark.parametrize("language", ["python", "java"])
@pytest.mark.parametrize("status", ["infrastructure_failure", "timeout"])
def test_native_validator_failure_is_not_a_completed_code_rejection(tmp_path, monkeypatch, language, status):
    first, second = SOURCES[language]
    adapter = native_adapter(tmp_path, monkeypatch, language, lambda source: SourceValidation(
        status if source == second else "valid", "validator did not judge", 0, 0, "fixture-validator"))
    native = adapter.resolve_collisions([first, second])
    assert native.status is SourceStatus.INFRASTRUCTURE_FAILURE and native.value is None
    assert native.reason.startswith("source 1: ")
    result = adapter.sources.resolve_collisions([first, second])
    # The refusal classifies a validator timeout as an infrastructure failure.
    assert result.status is SourceStatus.INFRASTRUCTURE_FAILURE
    assert result.value is None and "source 1" in result.reason and "did not judge" in result.reason


def test_java_rewrite_validation_failure_is_also_unavailable(tmp_path, monkeypatch):
    first, second = SOURCES["java"]
    adapter = native_adapter(tmp_path, monkeypatch, "java", lambda source: SourceValidation(
        "valid" if source in (first, second) else "infrastructure_failure",
        "rewrite validator failed", 0, 0, "fixture-validator"))
    native = adapter.resolve_collisions([first, second])
    assert native.status is SourceStatus.INFRASTRUCTURE_FAILURE and "source 1" in native.reason
    result = adapter.sources.resolve_collisions([first, second])
    assert result.status is SourceStatus.INFRASTRUCTURE_FAILURE and result.value is None
    assert "rewrite validator failed" in result.reason


def test_preference_receives_original_indices_and_preserves_native_choice(tmp_path, monkeypatch):
    fixture = "import pytest\n@pytest.fixture\ndef db():\n    return 1\n@pytest.mark.usefixtures('db')\ndef test_a():\n    pass\n"
    second = fixture.replace("return 1", "return 2").replace("test_a", "test_b")
    adapter = native_adapter(tmp_path, monkeypatch, "python")
    calls = []
    def preference(name, owners):
        calls.append((name, owners))
        return owners[-1]
    result = adapter.sources.resolve_collisions([fixture, second], preference=preference)
    assert result.completed and calls == [("db", (0, 1))]
    assert result.value.choices == (("db", 1, (0,)),)
    assert {"db", "test_a"} <= set(result.value.dropped_defs[0])
    for selected in (True, "1", None, 7):
        with pytest.raises(lb.InvalidRequest, match="preference"):
            adapter.sources.resolve_collisions([fixture, second], preference=lambda name, owners: selected)


@pytest.mark.parametrize("kwargs", [
    {"sources": "source"}, {"sources": None}, {"sources": [None]},
    {"sources": (source for source in ("first", "second"))},
    {"sources": [], "preference": 1}, {"sources": [], "localize_autouse": "false"},
])
def test_requests_are_validated_before_unsupported_default(tmp_path, kwargs):
    with pytest.raises(lb.InvalidRequest):
        lb.LangAdapter(lb.ProjectContext(str(tmp_path))).sources.resolve_collisions(**kwargs)


@pytest.mark.parametrize("language", ["python", "java"])
def test_source_only_collision_is_missing_setup_without_validator(tmp_path, monkeypatch, language):
    adapter = lb.create(language, lb.ProjectContext(str(tmp_path)))
    # Both languages' source operations are their adapter's methods.
    monkeypatch.setattr(adapter, "resolve_collisions", lambda *args, **kwargs: pytest.fail("missing-runtime resolution ran"))
    result = adapter.sources.resolve_collisions(SOURCES[language])
    assert result.status is SourceStatus.MISSING_SETUP and result.value is None


def completed(resolution=CollisionResolution(()), parser="fixture-parser"):
    return SourceObservation(SourceStatus.COMPLETED, value=resolution, parser=parser)


def test_completed_empty_resolution_and_result_snapshot(tmp_path):
    adapter = lb.LangAdapter(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    native = completed()
    adapter.resolve_collisions = lambda sources, **kwargs: native
    result = adapter.sources.resolve_collisions([])
    assert result == native and result.value is not native.value
    mutable = {}
    native = completed(CollisionResolution(["input"], renames=[mutable], deduped=[()],
                                           dropped_defs=[()], localized_autouse=[()]))
    result = adapter.sources.resolve_collisions(["input"])
    mutable["old"] = "new"
    assert result.value.renames == [{}]


@pytest.mark.parametrize("status", ["infrastructure_failure", "timeout", "missing_setup"])
def test_an_extension_whose_validation_could_not_run_is_answered_as_it_is(tmp_path, status):
    """An extension builds its answer with the public SourceObservation and
    SourceStatus, and the operation returns an unfinished one unchanged."""
    adapter = lb.LangAdapter(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    native = SourceObservation(status, parser="fixture-parser", reason="source 0: no validator")
    adapter.resolve_collisions = lambda sources, **kwargs: native
    assert adapter.sources.resolve_collisions(["input"]) == native


@pytest.mark.parametrize("value", [
    None, "not a result", CollisionResolution(()), completed(CollisionResolution(("extra",))),
    completed("not a resolution"), completed(CollisionResolution((), renames=5)),
    completed(parser=None), completed(parser=7),
    SourceObservation("parse_error", reason="not a validation failure"),
    SourceObservation("unsupported", reason="no collision check"),
    SourceObservation("refused", reason="refused"), SourceObservation("timeout"),
])
def test_an_answer_of_another_form_is_refused(tmp_path, value):
    adapter = lb.LangAdapter(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    adapter.resolve_collisions = lambda sources, **kwargs: value
    result = adapter.sources.resolve_collisions([])
    assert result.status is SourceStatus.INFRASTRUCTURE_FAILURE and result.value is None
    assert "must return a SourceObservation" in result.reason


def one_source(ejected=(), reasons=()):
    """A resolution of the one source "a", its per-source fields filled."""
    return CollisionResolution(("a",), ejected=ejected, eject_reasons=reasons, renames=({},),
                               deduped=((),), dropped_defs=((),), localized_autouse=((),))


@pytest.mark.parametrize("ejected,reasons", [
    ((0,), ()), ((1,), ("an index past the input",)), ((0, 0), ("twice", "twice")),
    ((True,), ("a boolean",)), ((-1,), ("a negative index",)), ((), ("a reason alone",)),
    (([0],), ("an unhashable index",)),
])
def test_ejected_sources_line_up_with_the_input(tmp_path, ejected, reasons):
    """Each ejected index names one input source, once, with its reason at
    the same place in eject_reasons."""
    adapter = lb.LangAdapter(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    adapter.resolve_collisions = lambda sources, **kwargs: completed(one_source((0,), ("left out",)))
    assert adapter.sources.resolve_collisions(["a"]).completed
    adapter.resolve_collisions = lambda sources, **kwargs: completed(one_source(ejected, reasons))
    result = adapter.sources.resolve_collisions(["a"])
    assert result.status is SourceStatus.INFRASTRUCTURE_FAILURE and result.value is None
    assert "must return a SourceObservation" in result.reason


@pytest.mark.parametrize("language,checker", [
    ("python", "the project interpreter"), ("java", "javac's parser")])
def test_each_source_whose_validation_could_not_run_is_named_in_order(
        tmp_path, monkeypatch, language, checker):
    """A built-in answer is an infrastructure failure whose reason names
    each source whose validation could not run, joined by ". "."""
    first, second = SOURCES[language]
    adapter = native_adapter(tmp_path, monkeypatch, language, lambda source: SourceValidation(
        "timeout", f"no verdict for the {'first' if source == first else 'second'}", 0, 0,
        "fixture-validator"))
    result = adapter.sources.resolve_collisions([first, second])
    assert result.status is SourceStatus.INFRASTRUCTURE_FAILURE and result.value is None
    assert result.reason == (
        f"source 0: {checker} could not validate source 0: no verdict for the first. "
        f"source 1: {checker} could not validate source 1: no verdict for the second")


def test_real_python_resolution_matches_native_without_extra_validation(tmp_path, monkeypatch):
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    operations = adapter
    validate = operations.validate_source
    calls = []
    def observed(source):
        calls.append(source)
        return validate(source)
    monkeypatch.setattr(operations, "validate_source", observed)
    result = adapter.sources.resolve_collisions(SOURCES["python"])
    assert result.completed and calls == list(SOURCES["python"])
    assert result == adapter.resolve_collisions(SOURCES["python"])
