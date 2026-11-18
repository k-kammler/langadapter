"""Private warmed setup through actual native resolver/toolset/compiler callers."""
from dataclasses import replace
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
from types import SimpleNamespace

import pytest

from langadapter._owned_directory import OwnershipRefused
from langadapter.runs import WarmedSuiteBenchmarkRequest
from langadapter.context import (
    ContainerExecution, Interpreter, LocalExecution, ProjectContext,
)
from langadapter.errors import InvalidRequest
from langadapter.execution import executor_for
from langadapter.java import (
    _warmed_fork as _warmed_junit, _warmed_fork as setup, _env as _deadline, _tools,
)
from langadapter.java._profile import JvmProfile
from langadapter.java._tools import jar as native_jar
from langadapter.java._build import launch_argv
from langadapter.outcomes import Outcome
from langadapter.suites import Suite


@pytest.fixture(params=["maven", "gradle", "container"])
def selected(request, tmp_path, monkeypatch):
    container, tool = request.param == "container", "gradle" if request.param == "gradle" else "maven"
    root = tmp_path / "project"
    root.mkdir()
    jdk_home = tmp_path / "jdk"
    (jdk_home / "bin").mkdir(parents=True)
    for name in ("java", "javac"):
        (jdk_home / "bin" / (name + (".exe" if os.name == "nt" else ""))).write_text("", encoding="utf-8")
    (jdk_home / "release").write_text('JAVA_VERSION="21.0.11"', encoding="utf-8")
    (root / ("pom.xml" if tool == "maven" else "build.gradle")).write_text(
        "<project/>" if tool == "maven" else "", encoding="utf-8")
    wrapper = "mvnw.cmd" if os.name == "nt" else "mvnw"
    if tool == "gradle":
        wrapper = "gradlew.bat" if os.name == "nt" else "gradlew"
    (root / wrapper).write_text("", encoding="utf-8")
    test = root / "ExampleTest.java"
    test.write_text("package demo; class ExampleTest {}", encoding="utf-8")
    resource = root / "fixture.txt"
    resource.write_text("delivered resource", encoding="utf-8")
    context = ProjectContext("/project" if container else str(root),
        Interpreter("/jdk/bin/java" if container else str(jdk_home / "bin" / ("java.exe" if os.name == "nt" else "java"))),
        execution=ContainerExecution("selected", str(tmp_path), helper_python="/control/python",
            container_work_dir="/zone") if container else LocalExecution(),
        interpreter_flags=("-ea",), environment={"NATIVE_SETTING": "selected"})
    executor = executor_for(context)
    state = SimpleNamespace(clock=100.0, calls=[], fetched=[], context=context, executor=executor,
        directory=tmp_path / "setup", tool=tool, container=container, fail=None,
        request=WarmedSuiteBenchmarkRequest(Suite(str(root), (str(test),), (str(resource),)),
            "native_lifecycle", 3, 60, forks=2, warmup_runs=1, runs=2))
    monkeypatch.setattr(_deadline, "time", SimpleNamespace(monotonic=lambda: state.clock))

    def fetch(coordinate, environment, *, extension="jar", deadline=None):
        # Tools are cached on the host. A container without captured control
        # values falls back to this process, never to its own environment.
        assert environment == ({} if container else dict(context.environment)) and deadline == 160
        state.fetched.append(coordinate)
        if state.fail == "tools":
            raise _tools.ToolUnavailable("selected engine is not cached")
        _deadline.remaining(deadline, 120, "fixture tool")
        state.clock += 0.25
        _, artifact, version = coordinate.split(":")
        cached = tmp_path / f"{artifact}-{version}.{extension}"
        cached.write_text("<project/>" if extension == "pom" else coordinate, encoding="utf-8")
        return cached

    monkeypatch.setattr(_tools, "jar", fetch)

    def run(argv, **options):
        state.calls.append((argv, options, state.clock))
        assert 0 < options["timeout"] <= 160 - state.clock
        state.clock += 0.5
        if container and argv[0] == "test":
            return subprocess.CompletedProcess(argv, 0 if argv[-1] in (
                "/project/pom.xml", "/project/mvnw") else 1, "", "")
        if container and argv[0] == "cat":
            return subprocess.CompletedProcess(argv, 0, 'JAVA_VERSION="21.0.11"'
                if argv[-1].endswith("/release") else "<project/>", "")
        if "-version" in argv:
            return subprocess.CompletedProcess(argv, 0, "javac 21", "")
        if argv[0].endswith(("javac", "javac.exe")):
            helper = argv[-1].endswith("LangAdapterWarmed.java")
            stage = "helper" if helper else "suite"
            if isinstance(state.fail, BaseException):
                raise state.fail
            if state.fail == stage:
                return subprocess.CompletedProcess(argv, 1, "native compile error", "")
            if state.fail == "expired":
                state.clock = 160
            Path(argv[argv.index("-d") + 1]).mkdir(parents=True, exist_ok=True)
            return subprocess.CompletedProcess(argv, 0, "", "")
        assert ("mvnw" in _launcher(argv)) if tool == "maven" else ("gradlew" in _launcher(argv))
        if state.fail == "build":
            return subprocess.CompletedProcess(argv, 3, "native build failure", "")
        build = state.directory / "build"
        if tool == "maven":
            (build / "maven-effective-pom.xml").write_text("<project/>", encoding="utf-8")
            (build / "maven-test-classpath.txt").write_text("/cache/junit-jupiter-api-5.14.4.jar", encoding="utf-8")
        else:
            data = dict(projectDir=str(root), mainSources=[], testSources=[], mainClasses=[],
                        testClasses=[], testClasspath=["/cache/junit-jupiter-api-5.14.4.jar"], release=8)
            (build / "gradle-projects" / "root.json").write_text(json.dumps(data), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "native build output", "")

    executor.run = run
    return state


def _launcher(argv):
    """The build launcher a command line starts, also through sh."""
    for command in argv[1:]:
        if argv[:len(launch_argv(command))] == launch_argv(command):
            return command
    return argv[0]


def prepare(state, **changes):
    from langadapter.java.adapter import _class_names
    return setup.prepare_setup(state.context, state.executor, state.request, state.directory,
                         total_deadline=changes.pop("total_deadline", 160),
                         class_names=changes.pop("class_names", _class_names), **changes)


def test_selected_setup_builds_once_and_compiles_suite_resources_and_helper(selected):
    result = prepare(selected)
    assert result.outcome is Outcome.COMPLETED and result.output == "native build output"
    value = result.prepared
    assert value.project.build_tool == selected.tool and value.project.root == selected.context.root
    assert value.jdk.version == "21.0.11" and value.tools.platform == "1.14.4"
    assert value.class_names == ("demo.ExampleTest",)
    assert (Path(value.classes) / "fixture.txt").read_text() == "delivered resource"
    compilers = [entry for entry in selected.calls if "--release" in entry[0]]
    builds = [entry for entry in selected.calls
              if "mvnw" in _launcher(entry[0]) or "gradlew" in _launcher(entry[0])]
    assert len(builds) == 1 and len(compilers) == 2
    helper = compilers[-1][0]
    assert helper[helper.index("--release") + 1] == str(JvmProfile.helper_release)
    # The launcher jars the project's run classpath lacks.
    assert all(PurePosixPath(jar).name in helper[helper.index("-cp") + 1] for jar in value.tools.jars)
    assert any("junit-platform-launcher-" in jar for jar in value.tools.jars)
    assert Path(compilers[-1][0][-1]).read_text() == _warmed_junit.HELPER_SOURCE
    assert all(entry[1]["env"]["NATIVE_SETTING"] == "selected" for entry in (*builds, *compilers))
    assert selected.context.interpreter_flags == ("-ea",)
    assert all(entry[1]["timeout"] <= 160 - entry[2] for entry in selected.calls)
    assert all("junit-platform" not in entry[0][0] for entry in selected.calls)
    if selected.container:
        assert all(PurePosixPath(path).is_absolute() for path in value.tools.jars)


@pytest.mark.parametrize("stage,outcome", [("build", Outcome.INFRASTRUCTURE_FAILURE),
    ("tools", Outcome.MISSING_SETUP), ("suite", Outcome.COLLECTION_FAILED),
    ("helper", Outcome.COLLECTION_FAILED), ("expired", Outcome.TIMED_OUT)])
def test_setup_failure_preserves_native_reason_and_never_returns_prepared(selected, stage, outcome):
    selected.fail = stage
    result = prepare(selected)
    assert result.outcome is outcome and result.prepared is None
    assert result.reason and selected.directory.exists()
    if stage == "suite":
        assert not (selected.directory / "LangAdapterWarmed.java").exists()
    if stage == "helper":
        # The compiler's message, then the jars the adapter added.
        assert result.output.startswith("native compile error")
        assert "Launcher jars on the classpath: " in result.output and "junit-platform-launcher-" in result.output


def test_actual_tool_download_expiry_is_timed_out_setup(selected, monkeypatch, tmp_path):
    monkeypatch.setattr(_tools, "jar", native_jar)
    monkeypatch.setattr(_tools, "cache_dir", lambda environment: tmp_path / "tool-cache")

    def fetch(*args, **kwargs):
        selected.clock = 160
        raise TimeoutError("selected download reached the entry deadline")

    monkeypatch.setattr(_tools.urllib.request, "urlopen", fetch)
    result = prepare(selected)
    assert result.outcome is Outcome.TIMED_OUT and result.prepared is None
    assert "Java tool download" in result.reason
    assert not any("--release" in argv for argv, _, _ in selected.calls)


def test_uncertain_compiler_failure_preserves_primary_and_actual_owner(selected):
    error = subprocess.TimeoutExpired("selected javac", 1)
    error._langadapter_command = object()
    selected.fail = error
    with pytest.raises(subprocess.TimeoutExpired) as captured:
        prepare(selected)
    assert captured.value is error and selected.directory.exists()


def test_interruption_is_not_a_setup_failure_value(selected):
    error = KeyboardInterrupt()
    selected.fail = error
    with pytest.raises(KeyboardInterrupt) as captured:
        prepare(selected)
    assert captured.value is error


def test_deadline_before_setup_refuses_all_work(selected):
    result = prepare(selected, total_deadline=100)
    assert result.outcome is Outcome.TIMED_OUT
    assert selected.calls == selected.fetched == [] and not selected.directory.exists()


def test_previous_setup_evidence_is_not_overwritten(selected):
    selected.directory.mkdir()
    (selected.directory / "raw.txt").write_bytes(b"original evidence")
    result = prepare(selected)
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert selected.calls == [] and (selected.directory / "raw.txt").read_bytes() == b"original evidence"


def test_missing_source_grammar_is_not_a_native_empty_suite(selected, monkeypatch):
    def unavailable(_):
        raise ModuleNotFoundError("tree_sitter_java is unavailable")

    result = prepare(selected, class_names=unavailable)
    assert result.outcome is Outcome.MISSING_SETUP and result.prepared is None
    assert "tree_sitter_java" in result.reason
    assert selected.calls == selected.fetched == [] and not selected.directory.exists()


@pytest.mark.parametrize("feature", [8, 11])
def test_selected_jdk_is_not_silently_replaced_for_native_requirements(selected, monkeypatch, feature):
    read = setup.jdks.read_in_container if selected.container else setup.jdks.read
    name = "read_in_container" if selected.container else "read"

    def selected_version(*args, **kwargs):
        return replace(read(*args, **kwargs), feature=feature, version=str(feature))

    monkeypatch.setattr(setup.jdks, name, selected_version)
    if feature == 11:
        toolset = setup._junit.toolset
        monkeypatch.setattr(setup._junit, "toolset", lambda *a, **kw:
                            replace(toolset(*a, **kw), platform="6.0.0"))
    result = prepare(selected)
    assert result.outcome is Outcome.MISSING_SETUP and result.prepared is None
    assert f"JDK {11 if feature == 8 else 17}" in result.reason
    assert not any("--release" in entry[0] for entry in selected.calls)
    if feature == 8:
        assert selected.fetched == []


def test_missing_native_class_directory_cannot_be_a_prepared_success(selected, monkeypatch):
    compile_tests = setup.projects.compile_tests

    def absent_directory(*args, **kwargs):
        value = compile_tests(*args, **kwargs)
        return replace(value, classes=None) if args[2][-1].endswith("LangAdapterWarmed.java") else value

    monkeypatch.setattr(setup.projects, "compile_tests", absent_directory)
    result = prepare(selected)
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE and result.prepared is None
    assert "no class directory" in result.reason


@pytest.mark.parametrize("change", ["wrong_executor", "no_jdk", "collision"])
def test_invalid_or_missing_selection_is_admitted_before_native_work(selected, change):
    if change == "wrong_executor":
        selected.executor = executor_for(replace(selected.context, interpreter_flags=("-wrong",)))
        with pytest.raises(OwnershipRefused):
            prepare(selected)
    elif change == "no_jdk":
        selected.context = replace(selected.context, interpreter=None)
        selected.executor = executor_for(selected.context)
        result = prepare(selected)
        assert result.outcome is Outcome.MISSING_SETUP
    else:
        Path(selected.request.suite.files()[0]).write_text("class LangAdapterWarmed {}", encoding="utf-8")
        with pytest.raises(InvalidRequest, match="conflicts"):
            prepare(selected)
    assert selected.calls == selected.fetched == []
