"""Source-only contexts retain validation without requiring a dummy runtime."""
import builtins
import subprocess

import pytest

import langadapter as lb
from langadapter.execution import ContainerExecutor, LocalExecutor


class AnalysisAdapter(lb.LangAdapter):
    language = "analysis-fixture"
    file_extensions = (".analysis-fixture",)

    def analyze(self, source):
        return tuple(source.splitlines())


@pytest.fixture
def no_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("source-only read attempted execution")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(LocalExecutor, "run", forbidden)
    monkeypatch.setattr(ContainerExecutor, "run", forbidden)
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "cosmic_ray" or name.startswith("cosmic_ray."):
            pytest.fail("source-only read imported the mutation engine")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)


def test_independent_analysis_adapter_needs_no_runtime(tmp_path, no_execution):
    lb.register(AnalysisAdapter.language, AnalysisAdapter.file_extensions, AnalysisAdapter)
    try:
        context = lb.ProjectContext(root=str(tmp_path))
        adapter = lb.create(AnalysisAdapter.language, context)
        assert context.interpreter is None
        assert adapter.analyze("") == ()
        assert adapter.analyze("first\nsecond") == ("first", "second")
    finally:
        lb.unregister(AnalysisAdapter.language)


@pytest.mark.parametrize("language, source", [
    ("python", "def test_example():\n    assert True\n"),
    ("java", "class Example { @Test void testExample() { assert true; } }"),
])
@pytest.mark.parametrize("execution_kind", ["local", "container"])
def test_builtin_static_reads_need_no_runtime(tmp_path, no_execution, language, source, execution_kind):
    execution = (lb.LocalExecution() if execution_kind == "local" else
                 lb.ContainerExecution("not-started", str(tmp_path)))
    context = lb.ProjectContext(str(tmp_path) if execution_kind == "local" else "/project",
                                execution=execution)
    adapter = lb.create(language, context)
    assert adapter.check_structure(source).ok
    assert adapter.test_metrics(source).tests == 1
    assert adapter.skeleton(source)
    assert adapter.function_fingerprints(source)
    assert adapter.optimization_metrics(source)["nloc"] > 0
    assert not adapter.check_structure("def broken( {" if language == "python" else "class {;").ok


@pytest.mark.parametrize("changes, field", [
    ({"root": ""}, "root"),
    ({"interpreter": lb.Interpreter()}, "interpreter"),
    ({"interpreter": lb.Interpreter(path="/missing/tool")}, "interpreter.path"),
    ({"interpreter": "none"}, "interpreter"),
    ({"environment": {"": "bad"}}, "environment"),
    ({"interpreter_flags": ("",)}, "interpreter_flags"),
    ({"test_framework": lb.TestFrameworkConfig(source="unknown")}, "test_framework.source"),
])
def test_absent_runtime_does_not_skip_other_validation(tmp_path, changes, field):
    fields = {"root": str(tmp_path), "interpreter": None}
    fields.update(changes)
    with pytest.raises(lb.InvalidRequest) as error:
        lb.ProjectContext(**fields).validate()
    assert error.value.field == field


def test_absent_runtime_does_not_allow_invalid_container_paths(tmp_path):
    with pytest.raises(lb.InvalidRequest) as error:
        lb.ProjectContext("relative", execution=lb.ContainerExecution("unused", str(tmp_path))).validate()
    assert error.value.field == "root"
