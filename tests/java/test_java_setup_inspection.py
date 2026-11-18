"""Public Java setup metadata without project builds or runtime launch."""
from dataclasses import replace
import os
from pathlib import Path
import subprocess

import pytest

import langadapter as lb
from langadapter import setup as _setup
from langadapter.execution import ContainerExecutor, LocalExecutor
from langadapter.java import _setup as _inspection
from langadapter.java.adapter import JavaAdapter


@pytest.fixture
def selected(tmp_path, monkeypatch):
    root = tmp_path / "subject"
    root.mkdir()
    (root / "Subject.java").write_text("class Subject {}\n", encoding="ascii")
    (root / "pom.xml").write_text("<project/>\n", encoding="ascii")
    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True)
    (home / "release").write_text('JAVA_VERSION="21.0.11"\n', encoding="ascii")
    for name in ("java", "javac"):
        tool = home / "bin" / (name + (".exe" if os.name == "nt" else ""))
        tool.write_text("selected tool", encoding="ascii")
        tool.chmod(0o755)
    maven = tmp_path / "maven" / "mvn"
    maven.parent.mkdir()
    maven.write_text("selected launcher", encoding="ascii")
    context = lb.ProjectContext(str(root), lb.Interpreter(str(home)),
        environment=lb.Environment({"PATH": str(home / "bin"), "PATHEXT": ".exe", "API_KEY": "private",
                                    "LANGADAPTER_MAVEN": str(maven)}, inherit=False))
    request = lb.ProjectSetupRequest(context, ("Subject.java",), ("tests.run_suite", "mutations.run"), allow_acquisition=True)

    def refuse(*args, **kwargs):
        raise AssertionError("inspection cannot build, launch a selected JVM, bootstrap or provision")

    monkeypatch.setattr(JavaAdapter, "project", refuse)
    monkeypatch.setattr(JavaAdapter, "_jdk", refuse)
    monkeypatch.setattr(LocalExecutor, "run", refuse)
    return request, home


def _without_action(report, operations):
    """Required items and operations that are not confirmed yet name no next step."""
    return ([item.key for item in report.items if item.required and item.action is None
             and not (item.readiness is lb.Availability.AVAILABLE and item.outcome is lb.Outcome.COMPLETED)]
            + [item.name for item in report.operations if item.name in operations
               and item.action is None and item.readiness is not lb.Availability.AVAILABLE])


def test_public_metadata_protects_descriptors_without_claiming_ready(selected):
    request, home = selected
    report = lb.inspect_project("java", request)
    assert report.outcome is lb.Outcome.COMPLETED and report.context is request.context
    assert report.inputs.required == ("Subject.java", "pom.xml")
    items = {item.key: item for item in report.items}
    assert items["runtime"].required and items["runtime"].version == "21.0.11"
    assert items["runtime"].selected == str(home) and items["runtime"].readiness is lb.Availability.AVAILABLE
    assert items["build_descriptor"].required and items["build_descriptor"].selected == "maven"
    assert items["build_descriptor"].readiness is None
    assert all(item.readiness is not lb.Availability.AVAILABLE for item in report.operations)
    assert "private" not in repr(report.safe_export())
    # The build is first tried by a run; the operations by the verification check.
    assert items["build_descriptor"].action == "start a run, which tries it for the first time"
    assert not _without_action(report, request.required_operations)


def _cache(directory, coordinates):
    from langadapter.java import _tools

    for item in coordinates:
        path = directory / _tools._relative(item)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"cached jar placeholder\n")
    return directory


@pytest.mark.parametrize("coverage", [False, True])
def test_measurement_tool_facts_follow_requested_facets_without_download(selected, tmp_path, monkeypatch, coverage):
    from langadapter.java import _jacoco
    from langadapter.java import _pit
    from langadapter.java import _tools

    request, _ = selected
    monkeypatch.setattr(_tools.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("inspection downloaded"))
    cache = _cache(tmp_path / "tools", _pit.JARS)  # PIT cached, JaCoCo absent
    environment = lb.Environment(dict(request.context.environment, LANGADAPTER_JAVA_TOOLS=str(cache)),
                                 inherit=False)
    request = replace(request, context=replace(request.context, environment=environment),
                      required_facets={"tests.run_suite": ("coverage",)} if coverage else ())
    report = lb.inspect_project("java", request)
    assert report.outcome is lb.Outcome.COMPLETED and report.context is request.context
    items = {item.key: item for item in report.items}
    assert items["pitest"].required and items["pitest"].outcome is lb.Outcome.COMPLETED
    assert items["pitest"].readiness is None and items["pitest"].version == _pit.VERSION
    assert items["jacoco"].required is coverage
    # Uncached jars are downloaded on first use: unverified at inspection.
    assert items["jacoco"].readiness is None and items["jacoco"].outcome is None
    assert "the first run downloads them" in items["jacoco"].reason and items["jacoco"].action
    # Cached jars are not run by any check: a run is their first try.
    assert items["pitest"].action == "start a run, which tries it for the first time"
    suite = next(item for item in report.operations if item.name == "tests.run_suite")
    assert suite.readiness is None and suite.action == "run the tool check to try it"
    _cache(cache, (_jacoco.AGENT, *_jacoco.CORE))
    items = {item.key: item for item in lb.inspect_project("java", request).items}
    assert items["jacoco"].outcome is lb.Outcome.COMPLETED and items["jacoco"].version == _jacoco.VERSION


def test_inspection_and_runs_locate_container_tools_from_the_host_control_values(tmp_path):
    from langadapter.java import _tools

    host_cache = tmp_path / "host-tools"
    control = lb.Environment({"LANGADAPTER_JAVA_TOOLS": str(host_cache)}, inherit=False)
    # The container's own captured values name a container home, which is no host path.
    container_values = lb.Environment({"HOME": "/home/container"}, inherit=False)
    container = lb.ProjectContext("/container/project", lb.Interpreter("/container/jdk/bin/java"),
        execution=lb.ContainerExecution("borrowed", str(tmp_path), helper_python="/usr/bin/python3",
                                        control_environment=control),
        environment=container_values)
    assert _tools.host_environment(container) == control
    assert _tools.cache_dir(_tools.host_environment(container)) == host_cache
    unselected = lb.ProjectContext("/container/project", lb.Interpreter("/container/jdk/bin/java"),
        execution=lb.ContainerExecution("borrowed", str(tmp_path), helper_python="/usr/bin/python3"),
        environment=container_values)
    assert dict(_tools.host_environment(unselected)) == {}
    local = lb.ProjectContext(str(tmp_path), lb.Interpreter("java"), environment=container_values)
    assert _tools.host_environment(local) == local.environment


def test_container_tool_cache_is_observed_from_the_captured_host_control_values(tmp_path):
    from langadapter.java import _pit
    from langadapter.java import _setup as _inspection

    cache = _cache(tmp_path / "host-tools", _pit.JARS)
    context = lb.ProjectContext("/container/project", lb.Interpreter("/container/jdk/bin/java"),
        execution=lb.ContainerExecution("borrowed", str(tmp_path), helper_python="/usr/bin/python3",
                                        control_environment={"LANGADAPTER_JAVA_TOOLS": str(cache)}),
        environment=lb.Environment({}, inherit=False))
    request = lb.ProjectSetupRequest(context, ("Subject.java",), ("mutations.run",))
    object.__setattr__(request, "_deadline", float("inf"))
    item = _inspection._tools_item(request, "pitest", True, _pit.JARS, _pit.VERSION)
    assert item.outcome is lb.Outcome.COMPLETED and item.selected == str(cache)


@pytest.mark.parametrize("name", ["build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts"])
def test_gradle_native_descriptor_names_are_protected_without_execution(selected, name):
    request, _ = selected
    root = Path(request.context.root)
    (root / "pom.xml").unlink()
    (root / name).write_text("throw new RuntimeException('do not evaluate')\n", encoding="ascii")
    report = lb.inspect_project("java", request)
    assert report.outcome is lb.Outcome.COMPLETED and report.context is request.context
    assert report.inputs.required == ("Subject.java", name)
    assert report.items[1].selected == "gradle"


@pytest.mark.parametrize("name", _inspection._WRAPPERS)
def test_present_wrapper_inputs_are_protected_but_never_started(selected, name):
    request, _ = selected
    path = Path(request.context.root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("do not execute", encoding="ascii")
    report = lb.inspect_project("java", request)
    assert name in report.inputs.required
    assert report.outcome is lb.Outcome.COMPLETED


@pytest.mark.parametrize("name", ["pom.xml", "mvnw", "gradle/wrapper/gradle-wrapper.properties"])
def test_known_native_input_exclusions_refuse_before_metadata(selected, name):
    request, _ = selected
    path = Path(request.context.root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("native input", encoding="ascii")
    with pytest.raises(lb.InvalidRequest, match="required"):
        lb.inspect_project("java", replace(request, inputs=lb.ProjectInputSelection(exclude=(name,))))


def test_linked_descriptor_is_not_adopted_as_a_native_input(selected, tmp_path):
    request, _ = selected
    target = tmp_path / "external.xml"
    target.write_text("<project/>\n", encoding="ascii")
    descriptor = Path(request.context.root) / "pom.xml"
    descriptor.unlink()
    try:
        descriptor.symlink_to(target)
    except OSError:
        pytest.skip("host does not permit this test's symlink")
    report = lb.inspect_project("java", request)
    assert report.outcome is lb.Outcome.MISSING_SETUP and report.context is None
    assert report.items[0].reason.startswith("selected project input cannot be a link: ")


def _without_launcher_variables(request):
    environment = lb.Environment({key: value for key, value in request.context.environment.items()
                                  if not key.startswith("LANGADAPTER_")}, inherit=False)
    return replace(request, context=replace(request.context, environment=environment))


@pytest.mark.parametrize("build,wrapper", [("pom.xml", "mvnw"), ("build.gradle", "gradlew")])
def test_a_build_without_its_launcher_is_missing_setup(selected, build, wrapper):
    """A build with no wrapper, LANGADAPTER_ variable or PATH launcher is missing setup."""
    request, _ = selected
    root = Path(request.context.root)
    (root / "pom.xml").unlink()
    (root / build).write_text("<project/>\n" if build == "pom.xml" else "", encoding="ascii")
    report = lb.inspect_project("java", _without_launcher_variables(request))
    launcher = next(item for item in report.items if item.key == "build_launcher")
    assert launcher.required and launcher.readiness is lb.Availability.MISSING_SETUP
    assert wrapper in launcher.action and launcher.tried_by is None
    suite = next(item for item in report.operations if item.name == "tests.run_suite")
    assert suite.readiness is lb.Availability.MISSING_SETUP and suite.action == launcher.action
    assert not _without_action(report, request.required_operations)


def test_the_projects_wrapper_and_the_path_are_launchers(selected, tmp_path):
    request, home = selected
    request = _without_launcher_variables(request)
    command = home / "bin" / ("mvn.exe" if os.name == "nt" else "mvn")
    command.write_text("launcher on the selected path", encoding="ascii")
    command.chmod(0o755)
    found = next(item for item in lb.inspect_project("java", request).items if item.key == "build_launcher")
    assert found.selected == str(command) and found.tried_by == "run" and found.readiness is None
    wrapper = Path(request.context.root) / ("mvnw.cmd" if os.name == "nt" else "mvnw")
    wrapper.write_text("the project's wrapper", encoding="ascii")
    found = next(item for item in lb.inspect_project("java", request).items if item.key == "build_launcher")
    assert found.selected == str(wrapper)


@pytest.mark.parametrize("operation,required", [("tests.run_suite", True), ("sources.metrics", False)])
def test_ambiguous_build_selection_is_explicit_and_does_not_pick_a_tool(selected, operation, required):
    request, _ = selected
    (Path(request.context.root) / "build.gradle").write_text("do not evaluate", encoding="ascii")
    report = lb.inspect_project("java", replace(request, required_operations=(operation,)))
    item = report.items[1]
    assert item.ambiguous and item.candidates == ("maven", "gradle") and item.selected is None
    assert item.required is required and item.action.startswith("remove the build files")
    assert (report.context is None) is required
    # A build waits for the choice in both scopes; nothing tries it before.
    for scope in (lb.inspect_project, lb.verify_project):
        status = next(entry for entry in scope("java", replace(request, required_operations=(operation,))).operations
                      if entry.name == operation)
        if required:
            assert status.action == item.action and status.tried_by is None and status.readiness is None
    assert {"pom.xml", "build.gradle"} <= set(report.inputs.required)


@pytest.mark.parametrize("operation,required", [("tests.run_suite", True), ("sources.validate_source", False),
                                               ("sources.metrics", False)])
def test_missing_build_is_not_missing_for_javac_only_or_pure_operations(selected, operation, required):
    request, _ = selected
    (Path(request.context.root) / "pom.xml").unlink()
    report = lb.inspect_project("java", replace(request, required_operations=(operation,)))
    assert report.outcome is lb.Outcome.COMPLETED and report.context is request.context
    assert report.items[1].required is required and report.items[1].readiness is lb.Availability.MISSING_SETUP
    status = next(item for item in report.operations if item.name == operation)
    assert status.readiness is (lb.Availability.MISSING_SETUP if required else None)
    assert "pom.xml" in report.items[1].action and not _without_action(report, (operation,))


@pytest.mark.parametrize("missing", ["release", "java", "javac"])
@pytest.mark.parametrize("operation,required", [("tests.run_suite", True), ("sources.metrics", False)])
def test_runtime_metadata_missing_never_becomes_ready_or_fails_optional_pure_scope(selected, missing, operation, required):
    request, home = selected
    path = home / "release" if missing == "release" else home / "bin" / (missing + (".exe" if os.name == "nt" else ""))
    path.unlink()
    report = lb.inspect_project("java", replace(request, required_operations=(operation,)))
    assert report.outcome is (lb.Outcome.MISSING_SETUP if required else lb.Outcome.COMPLETED)
    assert (report.context is None) is required
    assert report.items[0].version is None and report.items[0].required is required
    assert report.items[0].outcome is lb.Outcome.MISSING_SETUP
    assert "JDK" in report.items[0].action and not _without_action(report, (operation,))


@pytest.mark.parametrize("text", ["", "JAVA_VERSION=noquotes", 'JAVA_VERSION="not-a-version"', 'JAVA_VERSION=""'])
def test_malformed_release_is_failed_metadata_not_missing_or_invented_version(selected, text):
    request, home = selected
    (home / "release").write_text(text, encoding="ascii")
    report = lb.inspect_project("java", request)
    assert report.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE and report.context is None
    assert report.items[0].version is None and report.items[0].readiness is None
    assert not _without_action(report, request.required_operations)


@pytest.mark.parametrize("error,outcome", [(OSError("private"), lb.Outcome.INFRASTRUCTURE_FAILURE),
    (subprocess.TimeoutExpired("private", 1), lb.Outcome.TIMED_OUT), (FileNotFoundError("private"), lb.Outcome.MISSING_SETUP)])
def test_failed_metadata_retains_typed_native_failure(selected, monkeypatch, error, outcome):
    request, _ = selected
    original = _inspection.JavaEnv.read

    def read(files, path):
        if str(path).endswith("release"):
            raise error
        return original(files, path)

    monkeypatch.setattr(_inspection.JavaEnv, "read", read)
    report = lb.inspect_project("java", request)
    assert report.outcome is outcome and report.items[0].version is None
    assert "private" not in repr(report.safe_export())
    assert not _without_action(report, request.required_operations)


@pytest.mark.parametrize("marker", ["_langadapter_command", "_langadapter_prepared_copy",
                                   "_langadapter_prepared_project", "_langadapter_owned_directories"])
def test_runtime_metadata_never_swallows_unresolved_owner(selected, monkeypatch, marker):
    request, _ = selected
    error = OSError("same primary")
    owner = object()
    setattr(error, marker, (owner,) if marker.endswith("directories") else owner)
    monkeypatch.setattr(_inspection.JavaEnv, "read", lambda *a: (_ for _ in ()).throw(error))
    with pytest.raises(OSError) as caught:
        lb.inspect_project("java", request)
    assert caught.value is error


@pytest.mark.parametrize("runtime", [None, lb.Interpreter(provisioning=lb.ProvisioningRequest("21"))])
def test_unselected_runtime_is_unattempted_and_not_provisioned(selected, runtime):
    request, _ = selected
    report = lb.inspect_project("java", replace(request, context=replace(request.context, interpreter=runtime)))
    # No JDK is missing setup, as for Python; a provisioning request is not provisioned.
    expected = lb.Outcome.MISSING_SETUP if runtime is None else None
    assert report.outcome is (expected or lb.Outcome.UNSUPPORTED) and report.context is None
    assert report.items[0].outcome is expected and report.items[0].version is None


@pytest.mark.parametrize("runtime", [None, lb.Interpreter(provisioning=lb.ProvisioningRequest("21"))])
def test_unselected_optional_runtime_does_not_fail_pure_source_assessment(selected, runtime):
    request, _ = selected
    context = replace(request.context, interpreter=runtime)
    report = lb.inspect_project("java", replace(request, context=context, required_operations=("sources.metrics",)))
    assert report.outcome is lb.Outcome.COMPLETED and report.context is context
    assert report.items[0].outcome is (lb.Outcome.MISSING_SETUP if runtime is None else None)
    assert not report.items[0].required
    assert all(item.readiness is not lb.Availability.AVAILABLE for item in report.operations)


@pytest.mark.parametrize("error", [OSError("private"), subprocess.TimeoutExpired("private", 1)])
def test_optional_failed_runtime_keeps_valid_pure_selection(selected, monkeypatch, error):
    request, _ = selected
    monkeypatch.setattr(_inspection.JavaEnv, "read", lambda *a: (_ for _ in ()).throw(error))
    report = lb.inspect_project("java", replace(request, required_operations=("sources.metrics",)))
    assert report.outcome is lb.Outcome.COMPLETED and report.context is request.context
    assert not report.items[0].required and report.items[0].version is None


def test_metadata_cancellation_preserves_original_exception(selected, monkeypatch):
    request, _ = selected
    error = KeyboardInterrupt("original")
    monkeypatch.setattr(_inspection.JavaEnv, "read", lambda *a: (_ for _ in ()).throw(error))
    with pytest.raises(KeyboardInterrupt) as caught:
        lb.inspect_project("java", request)
    assert caught.value is error


def test_a_jdk_below_the_supported_range_is_refused_by_its_release_metadata(selected):
    request, home = selected
    (home / "release").write_text('JAVA_VERSION="1.8.0_412"\n', encoding="ascii")
    report = lb.inspect_project("java", request)
    assert report.items[0].outcome is lb.Outcome.UNSUPPORTED and report.items[0].version is None
    assert report.items[0].reason.startswith("JDK 1.8.0_412 is not supported")
    assert report.outcome is lb.Outcome.UNSUPPORTED
    assert all(item.readiness is not lb.Availability.AVAILABLE for item in report.operations)


def test_named_java_uses_only_explicit_host_path(selected, monkeypatch):
    request, home = selected
    monkeypatch.setenv("PATH", "ambient cannot replace selection")
    context = replace(request.context, interpreter=lb.Interpreter("java"))
    report = lb.inspect_project("java", replace(request, context=context))
    address = str(home / "bin" / ("java.exe" if os.name == "nt" else "java"))
    assert report.context is context and report.items[0].selected == address


def test_empty_captured_path_never_selects_ambient_java(selected):
    request, _ = selected
    context = replace(request.context, interpreter=lb.Interpreter("java"), environment=lb.Environment(inherit=False))
    report = lb.inspect_project("java", replace(request, context=context))
    assert report.outcome is lb.Outcome.MISSING_SETUP and report.context is None


@pytest.mark.parametrize("launcher", [True, False])
def test_container_metadata_uses_container_paths_and_captured_values_without_host_read(selected, monkeypatch, launcher):
    request, _ = selected
    context = replace(request.context, root="/project", interpreter=lb.Interpreter("/runtime/bin/java"),
        execution=lb.ContainerExecution("borrowed", request.context.root, container_work_dir="/zone",
                                      helper_python="/control/python"),
        environment=lb.Environment({"PATH": "/usr/bin", "API_KEY": "private"}, inherit=False))
    calls = []

    def run(executor, argv, **options):
        calls.append((argv, options))
        assert options.get("env", executor._context.environment) is context.environment
        if _setup._CONTAINER_INPUTS in argv:
            return subprocess.CompletedProcess(argv, 0, '{"ok":true}', "")
        if argv[0] == "cat":
            assert argv[-1] in ("/runtime/release", "/project/pom.xml")
            text = 'JAVA_VERSION="21.0.11"\n' if argv[-1] == "/runtime/release" else "<project/>\n"
            return subprocess.CompletedProcess(argv, 0, text, "")
        if argv[0] == "sh":
            # The launcher on the container's PATH, looked up without starting it.
            assert argv[1:] == ["-c", 'command -v "$1"', "sh", "mvn"]
            return subprocess.CompletedProcess(argv, 0 if launcher else 1, "/usr/bin/mvn\n" if launcher else "", "")
        assert argv[0] == "test" and argv[1] == "-f"
        present = argv[-1] in ("/project/pom.xml", "/runtime/release", "/runtime/bin/java", "/runtime/bin/javac")
        return subprocess.CompletedProcess(argv, 0 if present else 1, "", "")

    monkeypatch.setattr(ContainerExecutor, "run", run)
    report = lb.inspect_project("java", replace(request, context=context))
    assert report.outcome is lb.Outcome.COMPLETED and report.context is context
    assert report.inputs.required == ("Subject.java", "pom.xml")
    assert report.items[0].version == "21.0.11"
    assert len([argv for argv, _ in calls if _setup._CONTAINER_INPUTS in argv]) == 2
    assert all(0 < options["timeout"] <= 10 for _, options in calls)
    found = next(item for item in report.items if item.key == "build_launcher")
    suite = next(item for item in report.operations if item.name == "tests.run_suite")
    if launcher:
        assert found.selected == "/usr/bin/mvn" and found.tried_by == "run" and found.readiness is None
        assert suite.readiness is None
    else:
        assert found.required and found.readiness is lb.Availability.MISSING_SETUP
        assert suite.readiness is lb.Availability.MISSING_SETUP and suite.action == found.action


def test_java_pure_verification_keeps_native_execution_unattempted(selected):
    request, _ = selected
    report = lb.verify_project("java", replace(request, allow_acquisition=False,
                                              required_operations=("sources.metrics",)))
    assert report.scope == "verification" and report.outcome is lb.Outcome.COMPLETED
    assert report.context is request.context
    assert all(item.outcome is None and not item.required
               for item in report.items if item.key in {"jvm", "compiler", "native_execution"})
    assert all(item.readiness is not lb.Availability.AVAILABLE for item in report.operations)
