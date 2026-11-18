"""Neutral mutation discovery and native session contracts without engines."""
import dataclasses
import subprocess
import sys
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter.mutation import MutationRequest, OperatorCatalogue
from langadapter.mutation import MutationRun
from langadapter.mutation import MutationCleanupError, MutationSession
from langadapter.python.cosmic_ray import CosmicRaySettings


@pytest.fixture
def mutation_request(tmp_path):
    (tmp_path / "test_target.py").write_text("def test_value(): pass\n", encoding="utf-8")
    return MutationRequest("target.py", lb.Suite(str(tmp_path), ("test_target.py",)))


class FixtureSession(MutationSession):
    def __init__(self, workers=1):
        super().__init__(workers_requested=workers, workers_started=workers)
        self.requests = []
        self.raw = MutationRun.failed("target.py", lb.Outcome.TIMED_OUT, "native incomplete")

    def measure(self, request):
        self.requests.append(request)
        return self.raw

    def release(self):
        pass


def forbidden(*args, **kwargs):
    pytest.fail("unexpected engine/runtime probe")


def test_independent_partial_adapter_uses_unsupported_defaults(tmp_path, mutation_request, monkeypatch):
    class Partial(lb.LangAdapter):
        language = "mutation-partial"
        file_extensions = (".mutation-partial",)

    lb.register(Partial.language, Partial.file_extensions, Partial)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    try:
        adapter = lb.create(Partial.language, lb.ProjectContext(str(tmp_path)))
        assert adapter.mutations is adapter.mutations
        assert isinstance(adapter.mutations, lb.MutationOperations)
        assert adapter.mutations.engines() == ()
        catalogue = adapter.mutations.operators("no-engine")
        assert catalogue.outcome is lb.Outcome.UNSUPPORTED and catalogue.operators is None
        with adapter.mutations.open_session("no-engine", workers=3) as session:
            assert session.outcome is lb.Outcome.UNSUPPORTED and session.workers_started == 0
            assert not session.is_open
            result = session.run(mutation_request)
            assert result.outcome is lb.Outcome.UNSUPPORTED and result.score() is None
            assert result.resolution.method == "measured"
        assert not session.is_open
        assert all(not item.supported for item in adapter.operation_capabilities()
                   if item.name.startswith("mutations."))
    finally:
        lb.unregister(Partial.language)


@pytest.mark.parametrize("language,engine", [("python", "cosmic_ray"), ("java", "pit")])
def test_missing_runtime_does_not_hide_known_native_engine(tmp_path, language, engine, mutation_request, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    adapter = lb.create(language, lb.ProjectContext(str(tmp_path)))
    assert adapter.mutations.engines() == (engine,)
    catalogue = adapter.mutations.operators(engine)
    assert catalogue.outcome is lb.Outcome.MISSING_SETUP and catalogue.operators is None
    with adapter.mutations.open_session(engine) as session:
        assert session.outcome is lb.Outcome.MISSING_SETUP and session.workers_started == 0
        result = session.run(mutation_request)
        assert result.outcome is lb.Outcome.MISSING_SETUP and result.score() is None
        assert result.resolution.requested == "measure" and result.resolution.method == "measured"
    capabilities = {item.name: item for item in adapter.operation_capabilities()}
    assert capabilities["mutations.engines"].readiness is None
    assert not capabilities["mutations.engines"].execution_backed
    assert capabilities["mutations.operators"].readiness is lb.Availability.MISSING_SETUP
    measured = capabilities["mutations.run"]
    assert measured.resolution_methods == ("measured",)
    assert measured.evidence_granularity == ("aggregate", "per_mutant", "per_test")
    assert "mutations.derive" not in capabilities


@pytest.mark.parametrize("language,engine", [("python", "cosmic_ray"), ("java", "pit")])
def test_unknown_native_engine_is_refused_before_tools(tmp_path, language, engine, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    adapter = lb.create(language, lb.ProjectContext(str(tmp_path)))
    for call in (adapter.mutations.operators, adapter.mutations.open_session):
        with pytest.raises(lb.InvalidRequest, match="engine"):
            call("unknown")


@pytest.mark.parametrize("engine", [None, "", " ", 2, [], {}])
def test_even_partial_adapters_refuse_malformed_engine_names(tmp_path, engine):
    adapter = lb.LangAdapter(lb.ProjectContext(str(tmp_path)))
    for call in (adapter.mutations.operators, adapter.mutations.open_session):
        with pytest.raises(lb.InvalidRequest, match="engine"):
            call(engine)


@pytest.mark.parametrize("options", [{"workers": 0}, {"workers": True}, {"workers": 1.5},
                                      {"allow_fewer_workers": 1}, {"distributor": ""}, {"distributor": 3}])
def test_partial_open_still_validates_common_options(tmp_path, options):
    adapter = lb.LangAdapter(lb.ProjectContext(str(tmp_path)))
    with pytest.raises(lb.InvalidRequest):
        adapter.mutations.open_session("no-engine", **options)


@pytest.mark.parametrize("options", [{"distributor": "http"}, {"settings": CosmicRaySettings()},
                                      {"allow_fewer_workers": True}])
def test_pit_refuses_foreign_options_even_without_runtime(tmp_path, options):
    adapter = lb.create("java", lb.ProjectContext(str(tmp_path)))
    with pytest.raises(lb.InvalidRequest, match="options"):
        adapter.mutations.open_session("pit", **options)


@pytest.mark.parametrize("options", [{"settings": {}}, {"workers": 2, "distributor": "local"},
                                     {"distributor": "wrong"}])
def test_python_refuses_native_option_errors_without_runtime(tmp_path, options):
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path)))
    with pytest.raises(lb.InvalidRequest):
        adapter.mutations.open_session("cosmic_ray", **options)


@pytest.mark.parametrize("language,engine", [("python", "cosmic_ray"), ("java", "pit")])
def test_native_calls_preserve_options_catalogue_and_common_session(tmp_path, language, engine, mutation_request, monkeypatch):
    adapter = lb.create(language, lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    catalogue = OperatorCatalogue(engine, ("op2", "op1"), tool_version="fixture")
    received = []
    session = FixtureSession(2)
    monkeypatch.setattr(adapter, "mutation_operators", lambda actual: received.append(actual) or catalogue)
    monkeypatch.setattr(adapter, "open_mutation_session", lambda actual, **opts: received.append((actual, opts)) or session)
    assert adapter.mutations.operators(engine) == catalogue
    options = {"workers": 2}
    if language == "python":
        options.update(distributor="http", allow_fewer_workers=True, settings=CosmicRaySettings(keep_output=True))
    with adapter.mutations.open_session(engine, **options) as actual:
        assert actual is session
        measured = actual.run(mutation_request)
        assert measured.outcome is lb.Outcome.TIMED_OUT and measured.score() is None
        assert actual.requests == [mutation_request]
    assert received == [engine, (engine, options)]
    assert measured.resolution.requested == "measure" and measured.resolution.method == "measured"
    assert adapter.mutations.supports("run")


def test_container_java_keeps_engine_discovery_but_requires_control_setup(tmp_path, mutation_request, monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    adapter = lb.create("java", lb.ProjectContext("/project", lb.Interpreter(path="/usr/bin/java"),
                        execution=lb.ContainerExecution("fixture", str(tmp_path), container_work_dir="/project")))
    assert adapter.mutations.engines() == ("pit",)
    assert adapter.mutations.operators("pit").outcome is lb.Outcome.MISSING_SETUP
    with adapter.mutations.open_session("pit") as session:
        assert session.run(mutation_request).outcome is lb.Outcome.MISSING_SETUP
    assert adapter.mutations.supports("open_session")


def test_bad_native_session_is_closed_before_type_failure(tmp_path, monkeypatch):
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    closed = []
    native = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(adapter, "open_mutation_session", lambda *args, **kwargs: native)
    with pytest.raises(TypeError, match="common MutationSession"):
        adapter.mutations.open_session("cosmic_ray")
    assert closed == [True]


def test_bad_native_session_retains_cleanup_handle_on_failure(tmp_path, monkeypatch):
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    def broken():
        raise OSError("busy")
    native = SimpleNamespace(close=broken)
    monkeypatch.setattr(adapter, "open_mutation_session", lambda *args, **kwargs: native)
    with pytest.raises(MutationCleanupError) as caught:
        adapter.mutations.open_session("cosmic_ray")
    assert caught.value.cleanup_handle is native


def test_an_extension_implements_mutation_by_its_hooks(tmp_path):
    class Extension(lb.LangAdapter):
        native_mutation_engines = ("example",)

        def open_mutation_session(self, engine, **options):
            return FixtureSession()

    adapter = Extension(lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)))
    assert adapter.mutations.supports("engines") and adapter.mutations.supports("run")
    assert not adapter.mutations.supports("operators")
    with adapter.mutations.open_session("example") as session:
        assert isinstance(session, MutationSession)


def test_an_extension_checks_its_engine_options_in_the_public_hook(tmp_path):
    """Options beyond workers reach an extension's engine only through its
    validate_mutation_session_options, which refuses one it does not take.
    Without the hook such an option is refused."""
    opened = []

    class Plain(lb.LangAdapter):
        native_mutation_engines = ("example",)

        def open_mutation_session(self, engine, **options):
            opened.append(options)
            return FixtureSession()

    class Checked(Plain):
        def validate_mutation_session_options(self, engine, *, workers, distributor,
                                              allow_fewer_workers, settings):
            if distributor not in (None, "queue") or allow_fewer_workers or settings is not None:
                raise lb.InvalidRequest("distributor", "the example engine takes only queue")

    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable))
    with Checked(context).mutations.open_session("example", distributor="queue"):
        pass
    assert opened == [{"workers": 1, "distributor": "queue"}]
    with pytest.raises(lb.InvalidRequest, match="only queue"):
        Checked(context).mutations.open_session("example", distributor="http")
    with pytest.raises(lb.InvalidRequest, match="options"):
        Plain(context).mutations.open_session("example", distributor="queue")
    assert len(opened) == 1


def test_unsupported_session_validates_run_request(mutation_request, tmp_path):
    session = lb.LangAdapter(lb.ProjectContext(str(tmp_path))).mutations.open_session("no-engine")
    with pytest.raises(lb.InvalidRequest):
        session.run(dataclasses.replace(mutation_request, workers=0))
