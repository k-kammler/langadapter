"""Selected-execution metadata without admitting public container operations."""
from contextlib import contextmanager
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter.capabilities import Availability
from langadapter.execution import OwnedDirectory
from langadapter.java import _jacoco, _tools as _junit, _pit, _tools, _env as jdk
from langadapter.java.adapter import JavaAdapter
from langadapter.java._build import Resolution


@pytest.fixture(params=[False, True], ids=["native", "container"])
def metadata_flow(request, tmp_path, monkeypatch):
    container = request.param
    zone = tmp_path / "zone"
    zone.mkdir()
    execution = (lb.ContainerExecution(container="selected", work_dir=str(zone),
                                       container_work_dir="/workzone", helper_python="/control/python",
                                       control_environment=lb.Environment({"HOST_SETTING": "host"},
                                                                          inherit=False))
                 if container else lb.LocalExecution())
    context = lb.ProjectContext(
        root="/project" if container else str(tmp_path),
        interpreter=lb.Interpreter(path="/chosen/jdk/bin/java"),
        execution=execution, environment={"JAVA_EXTRA": "selected"})
    adapter = JavaAdapter(context)
    # Tools are cached on the host: a container looks them up with its host control values.
    host = execution.control_environment if container else context.environment
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    owners = []
    create = OwnedDirectory.create

    def allocate(**options):
        owned = create(**options, receipt_parent=str(receipts))
        owners.append(owned)
        return owned

    monkeypatch.setattr(OwnedDirectory, "create", allocate)
    if container:
        def ownership_io(argv, **options):
            if argv[0] == "cat":
                assert Path(argv[-1]).name == ".langadapter-owner"
                output = Path(argv[-1]).read_text(encoding="ascii")
            else:
                assert argv[:3] == ["rm", "-rf", "--"]
                assert Path(argv[-1]).is_relative_to(zone)
                output = ""
            return subprocess.CompletedProcess(argv, 0, output, "")

        monkeypatch.setattr(adapter._executor, "run", ownership_io)
    calls = []
    monkeypatch.setattr(adapter, "_context_issue", lambda: "")
    selected = jdk._ContainerJdk("/chosen/jdk", 21, "21.0.7") if container else jdk.Jdk(
        str(tmp_path / "native-jdk"), 17, "17.0.12")

    def native_read(path):
        assert not container, "container metadata must not inspect the host JDK"
        calls.append(("jdk", path))
        return selected

    def container_read(path, executor, *, environment, strict=False):
        assert container and executor is adapter._executor
        assert environment == context.environment
        calls.append(("jdk", path))
        return selected

    monkeypatch.setattr(jdk, "read", native_read)
    monkeypatch.setattr(jdk, "read_in_container", container_read)
    monkeypatch.setattr(adapter, "project", lambda: Resolution(
        lb.Outcome.COMPLETED, project=SimpleNamespace(run_classpath=lambda: ("project.jar",))))
    monkeypatch.setattr("langadapter.java.adapter.importlib_util.find_spec", lambda _: object())

    def options_of(options):
        if container:
            assert options["executor"] is adapter._executor
            scratch = options["scratch"]
            assert scratch.is_dir() and scratch.is_relative_to(zone)
        else:
            assert options == {}
        return options

    def tools(classpath, environment, **options):
        assert classpath == ("project.jar",)
        assert environment == host
        calls.append(("junit", options_of(options)))
        return SimpleNamespace(platform="1.14.4")

    def pit(environment, **options):
        assert environment == host
        calls.append(("pit", options_of(options)))
        return ("/workzone/pit.jar" if container else "cached-pit.jar",)

    cache = tmp_path / "cache"
    cache.mkdir()

    def jar(coordinates, environment):
        assert environment == host
        path = cache / (coordinates.replace(":", "_") + ".jar")
        path.write_bytes(b"cached fixture")
        calls.append(("jar", coordinates))
        return path

    real_stage = _tools.stage_files

    def stage(paths, directory, *, executor):
        assert container and executor is adapter._executor
        assert tuple(c[1] for c in calls if c[0] == "jar") == (_jacoco.AGENT, *_jacoco.CORE)
        staged = real_stage(paths, directory, executor=executor)
        assert all(path.startswith("/workzone/") for path in staged)
        calls.append(("staged", staged))
        return staged

    monkeypatch.setattr(_junit, "toolset", tools)
    monkeypatch.setattr(_pit, "classpath", pit)
    monkeypatch.setattr(_tools, "jar", jar)
    monkeypatch.setattr(_tools, "stage_files", stage)
    return SimpleNamespace(adapter=adapter, calls=calls, container=container,
                           selected=selected, context=context, owners=owners)


def test_runtime_metadata_uses_the_selected_jdk(metadata_flow):
    flow = metadata_flow
    assert flow.adapter.runtime_version() == flow.selected.version
    assert flow.calls == [("jdk", "/chosen/jdk/bin/java")]


def test_capability_metadata_uses_selected_jdk_and_staged_tools(metadata_flow):
    flow = metadata_flow
    report = flow.adapter.capabilities()
    assert report.outcome is lb.Outcome.COMPLETED, report.reason
    assert report.runtime == f"Java {flow.selected.version}"
    assert report.capability("coverage").availability is Availability.AVAILABLE
    assert report.capability("mutation").availability is Availability.AVAILABLE
    assert report.mutation_engines == ("pit",)
    assert len([c for c in flow.calls if c[0] == "staged"]) == int(flow.container)


def test_tool_staging_is_selected_even_with_a_readable_host_jdk(metadata_flow, monkeypatch):
    flow = metadata_flow
    monkeypatch.setattr(jdk, "read", lambda _path: flow.selected)
    report = flow.adapter.capabilities()
    assert report.outcome is lb.Outcome.COMPLETED
    assert len([c for c in flow.calls if c[0] == "staged"]) == int(flow.container)


def test_missing_selected_jdk_cannot_fall_back_to_the_host(metadata_flow, monkeypatch):
    flow = metadata_flow
    name = "read_in_container" if flow.container else "read"
    monkeypatch.setattr(jdk, name, lambda *args, **kwargs: None)
    assert flow.adapter.runtime_version() is None
    report = flow.adapter.capabilities()
    assert report.outcome is lb.Outcome.MISSING_SETUP
    assert report.runtime is None and flow.calls == []


@pytest.mark.parametrize("tool", ["junit", "jacoco", "pit"])
def test_tool_failure_does_not_erase_independent_capability(metadata_flow, monkeypatch, recwarn, tool):
    def fail(*args, **kwargs):
        raise OSError("staging failed")

    module, name = {"junit": (_junit, "toolset"), "jacoco": (_tools, "jar"),
                    "pit": (_pit, "classpath")}[tool]
    monkeypatch.setattr(module, name, fail)
    report = metadata_flow.adapter.capabilities()
    assert report.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "staging failed" in report.reason
    assert report.capability("source_merging").availability is Availability.AVAILABLE
    if tool != "junit":
        assert report.capability("suite_runs").availability is Availability.AVAILABLE
    if tool == "jacoco":
        assert report.capability("mutation").availability is Availability.AVAILABLE
        assert report.capability("coverage").availability is Availability.MISSING_SETUP
    assert len(metadata_flow.owners) == int(metadata_flow.container)
    for owned in metadata_flow.owners:
        owned.verify()
    assert sum("retained after interrupted operation" in str(w.message)
               for w in recwarn.list) == len(metadata_flow.owners)


def test_missing_tool_is_not_an_execution_failure(metadata_flow, monkeypatch):
    def missing(*args, **kwargs):
        raise _tools.ToolUnavailable("not cached")

    monkeypatch.setattr(_pit, "classpath", missing)
    report = metadata_flow.adapter.capabilities()
    assert report.outcome is lb.Outcome.COMPLETED
    assert report.capability("mutation").availability is Availability.MISSING_SETUP
    assert report.capability("coverage").availability is Availability.AVAILABLE


def test_container_scratch_failure_is_reported_without_false_tool_readiness(metadata_flow, monkeypatch):
    if not metadata_flow.container:
        pytest.skip("container-only scratch admission")

    @contextmanager
    def scratch(_name):
        raise OSError("work directory unavailable")
        yield

    monkeypatch.setattr(metadata_flow.adapter, "_scratch", scratch)
    report = metadata_flow.adapter.capabilities()
    assert report.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "work directory unavailable" in report.reason
    assert report.capability("suite_runs").availability is Availability.MISSING_SETUP
    assert report.capability("source_merging").availability is Availability.AVAILABLE
    assert [c for c in metadata_flow.calls if c[0] != "jdk"] == []


def test_public_container_guard_still_runs_before_metadata_probes(tmp_path, monkeypatch):
    context = lb.ProjectContext(
        root="/project", interpreter=lb.Interpreter(path="/chosen/jdk/bin/java"),
        execution=lb.ContainerExecution(container="selected", work_dir=str(tmp_path)))
    adapter = JavaAdapter(context)

    def forbidden(*args, **kwargs):
        pytest.fail("the public guard must refuse before tool probing")

    monkeypatch.setattr(jdk, "read", forbidden)
    monkeypatch.setattr(jdk, "read_in_container", forbidden)
    monkeypatch.setattr(_junit, "toolset", forbidden)
    assert adapter.runtime_version() is None
    report = adapter.capabilities()
    assert report.outcome is lb.Outcome.MISSING_SETUP
    assert all(c.availability is Availability.MISSING_SETUP for c in report.capabilities)
