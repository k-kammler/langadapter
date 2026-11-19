"""Java verification tries the build in the prepared copy
(verify-build-and-collection): it resolves offline unless the request allows
acquisition, leaves a failure that only a download would fix to the first
run, reports a descriptor error, and with real tools compiles and discovers
the tests and starts the measurement tools."""
from dataclasses import replace
import hashlib
import io
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter.execution import LocalExecutor

# Maven 3.9.16's own output, offline with an empty local repository.
DOWNLOAD = (
    "[ERROR] Plugin org.apache.maven.plugins:maven-help-plugin:3.5.2 or one of its dependencies "
    "could not be resolved:\n"
    "[ERROR] \tCannot access central (https://repo.maven.apache.org/maven2) in offline mode and the "
    "artifact org.apache.maven.plugins:maven-help-plugin:jar:3.5.2 has not been downloaded from it before.\n")
DESCRIPTOR = (
    "[ERROR] [ERROR] Some problems were encountered while processing the POMs:\n"
    "[FATAL] Non-readable POM /project/pom.xml: no more data available\n"
    "[ERROR] The build could not read 1 project -> [Help 1]\n")
UNMATCHED = "[ERROR] the repository manager refused the request while offline\n"
#: The next step of a step that needs a download first.
DOWNLOAD_STEP = ("a tool check that may download, or a run with network access, downloads "
                 "what is missing once, before any offline run")
# Gradle 9.8.0's output, offline, for a buildscript classpath not in its cache.
BUILDSCRIPT = (
    "FAILURE: Build failed with an exception.\n\n* What went wrong:\n"
    "A problem occurred evaluating root project 'source'.\n"
    "> Could not resolve all files for configuration ':classpath'.\n"
    "   > Could not resolve org.example:build-plugin:1.0.\n"
    "      > No cached version of org.example:build-plugin:1.0 available for offline mode.\n")


@pytest.fixture
def controlled(tmp_path, monkeypatch):
    """A Maven project with a selected JDK whose version commands answer, and
    a build launcher whose runs answer with ``state.output``."""
    root = tmp_path / "source"
    (root / "src" / "test" / "java").mkdir(parents=True)
    (root / "Subject.java").write_bytes(b"class Subject {}\n")
    (root / "src" / "test" / "java" / "SubjectTest.java").write_bytes(b"class SubjectTest {}\n")
    (root / "pom.xml").write_bytes(b"<project/>\n")
    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True)
    (home / "release").write_text('JAVA_VERSION="21.0.11"\n', encoding="ascii")
    for name in ("java", "javac"):
        (home / "bin" / (name + (".exe" if os.name == "nt" else ""))).write_bytes(b"selected tool\n")
    maven = tmp_path / "maven" / "mvn"
    maven.parent.mkdir()
    maven.write_bytes(b"selected launcher")
    cache = tmp_path / "tools"
    cache.mkdir()
    # The placeholder jars _with_coverage caches stand for the pinned ones.
    from langadapter.java import _jacoco, _tools
    for coordinates in (_jacoco.AGENT, *_jacoco.CORE):
        monkeypatch.setitem(_tools.TOOL_SHA256, coordinates, hashlib.sha256(b"jar").hexdigest())
    context = lb.ProjectContext(str(root), lb.Interpreter(str(home)),
        execution=lb.LocalExecution(str(tmp_path)),
        environment=lb.Environment({"LANGADAPTER_MAVEN": str(maven),
                                    "LANGADAPTER_JAVA_TOOLS": str(cache)}, inherit=False))
    request = lb.ProjectSetupRequest(context, ("Subject.java",), ("tests.run_suite",))
    state = SimpleNamespace(builds=[], output=DOWNLOAD, code=1, slow=False, agents=[], agent=(0, ""))

    def run(executor, argv, **options):
        if any(str(argument).startswith("-javaagent:") for argument in argv):
            state.agents.append(list(argv))
            return subprocess.CompletedProcess(argv, state.agent[0], "", state.agent[1])
        if argv[-1] == "-version":
            compiler = Path(argv[0]).stem == "javac"
            text = "javac 21.0.11\n" if compiler else 'openjdk version "21.0.11" 2026-04-21\n'
            return subprocess.CompletedProcess(argv, 0, text if compiler else "", "" if compiler else text)
        state.builds.append(list(argv))
        if state.slow:
            raise subprocess.TimeoutExpired(argv, options.get("timeout"))
        return subprocess.CompletedProcess(argv, state.code, state.output, "")

    monkeypatch.setattr(LocalExecutor, "run", run)
    return request, state


def _item(report, key):
    return next(item for item in report.items if item.key == key)


@pytest.mark.parametrize("acquisition", [False, True])
def test_verification_resolves_offline_unless_acquisition_is_allowed(controlled, acquisition):
    request, state = controlled
    lb.verify_project("java", replace(request, allow_acquisition=acquisition))
    (build,) = [argv for argv in state.builds if "process-test-resources" in argv]
    assert ("-o" in build) is not acquisition


def test_a_failure_only_a_download_fixes_is_left_to_the_first_run(controlled):
    """The plugin is missing from the local repository: the first run, which
    may download it, tries the build, and the check passes as before. The
    build's items and the report say what the check left, and the next step
    is a run with network access."""
    request, state = controlled
    report = lb.verify_project("java", request)
    resolution = _item(report, "build_resolution")
    assert resolution.tried_by == "run" and "maven-help-plugin" in resolution.reason
    assert resolution.action == DOWNLOAD_STEP
    for key in ("build_descriptor", "build_launcher"):
        item = _item(report, key)
        assert item.tried_by == "run"
        assert item.reason == "found, and the check's build did not resolve the project"
        assert item.action == resolution.action
    assert report.reason == ("ran the JDK's version commands, tried in a project copy what it "
                             "could without running a test, and left the other steps to the "
                             "first run")
    assert report.outcome is lb.Outcome.COMPLETED


@pytest.mark.parametrize("where", ["MAVEN_ARGS", "maven.config"])
def test_a_download_the_offline_runs_cannot_make_is_missing_setup(controlled, where):
    """The project's runs build offline too, so the first run would fail
    the same way: the check reports the artifact as missing setup."""
    request, state = controlled
    values = dict(request.context.environment)
    if where == "MAVEN_ARGS":
        values["MAVEN_ARGS"] = "-B -o"
    else:
        (Path(request.context.root) / ".mvn").mkdir()
        (Path(request.context.root) / ".mvn" / "maven.config").write_bytes(b"-o\n")
    context = replace(request.context, environment=lb.Environment(values, inherit=False))
    report = lb.verify_project("java", replace(request, context=context))
    resolution = _item(report, "build_resolution")
    assert resolution.required and resolution.tried_by is None
    assert resolution.outcome is lb.Outcome.MISSING_SETUP and "maven-help-plugin" in resolution.reason
    assert "runs build offline" in resolution.reason
    assert report.outcome is lb.Outcome.MISSING_SETUP


def test_an_offline_failure_no_marker_names_is_left_to_the_first_run_with_its_output(controlled):
    request, state = controlled
    state.output = UNMATCHED
    report = lb.verify_project("java", request)
    resolution = _item(report, "build_resolution")
    assert resolution.tried_by == "run" and "refused the request while offline" in resolution.reason
    assert report.outcome is lb.Outcome.COMPLETED


def test_an_uncached_buildscript_classpath_is_left_to_the_first_run(controlled):
    """Gradle's own evaluation failure, with the download a run may make
    below it: the download text wins."""
    request, state = controlled
    root = Path(request.context.root)
    (root / "pom.xml").unlink()
    (root / "build.gradle").write_bytes(b"plugins { id 'java' }\n")
    gradle = Path(request.context.environment["LANGADAPTER_MAVEN"]).with_name("gradle")
    gradle.write_bytes(b"selected launcher")
    values = dict(request.context.environment, LANGADAPTER_GRADLE=str(gradle))
    context = replace(request.context, environment=lb.Environment(values, inherit=False))
    state.output = BUILDSCRIPT
    report = lb.verify_project("java", replace(request, context=context))
    (build,) = [argv for argv in state.builds if "langadapterResolve" in argv]
    assert "--offline" in build
    resolution = _item(report, "build_resolution")
    assert resolution.tried_by == "run" and "org.example:build-plugin:1.0" in resolution.reason
    assert report.outcome is lb.Outcome.COMPLETED


def test_a_descriptor_error_fails_verification(controlled):
    """A broken POM fails a run the same way, so verification reports it."""
    request, state = controlled
    state.output = DESCRIPTOR
    report = lb.verify_project("java", request)
    resolution = _item(report, "build_resolution")
    assert resolution.required and resolution.tried_by is None
    assert resolution.outcome is not lb.Outcome.COMPLETED and "Non-readable POM" in resolution.reason
    for key in ("build_descriptor", "build_launcher"):
        assert _item(report, key).action == "fix what the build reports, then run the tool check again"
    assert report.outcome is not lb.Outcome.COMPLETED


@pytest.mark.parametrize("acquisition", [False, True])
def test_a_build_wrapper_runs_only_with_acquisition(controlled, acquisition):
    """The project's wrapper may download its build tool before Maven's
    offline option applies: without acquisition the first run tries the
    build and the steps after it, and nothing is started."""
    request, state = controlled
    for name in ("mvnw", "mvnw.cmd"):
        (Path(request.context.root) / name).write_bytes(b"wrapper\n")
    report = lb.verify_project("java", replace(request, allow_acquisition=acquisition))
    assert bool(state.builds) is acquisition
    if not acquisition:
        for key in ("build_resolution", "test_compilation", "test_discovery"):
            item = _item(report, key)
            assert item.tried_by == "run" and "wrapper may download its build tool" in item.reason
        # The report's reason and the runtime item say the build was not tried.
        assert "left the build to the first run" in report.reason
        assert "not compiled or run yet" in _item(report, "runtime").reason
        assert report.outcome is lb.Outcome.COMPLETED
    else:
        # The tool check replaces what its build made untrue in the JDK's reason.
        assert _item(report, "runtime").reason.startswith(
            "JDK found, and the build was tried in a project copy")


def test_a_discovery_that_fails_is_reported_without_failing_verification(controlled, monkeypatch):
    """The build resolves and the tests compile, and JUnit's discovery fails:
    the item reports it without being required, since a run discovers only
    the tests it selects, and the check completes."""
    from langadapter.java import _build, _runs, _tools

    request, state = controlled

    def resolved(root, jdk, executor, scratch, **options):
        return _build.Resolution(lb.Outcome.COMPLETED, project=_build.JavaProject(
            root=root, build_tool="maven", release=21, main_sources=(),
            test_sources=(str(Path(root) / "src" / "test" / "java"),), main_classes=(),
            test_classes=(), test_classpath=()))

    monkeypatch.setattr(_build, "resolve", resolved)
    monkeypatch.setattr(_build, "compile_tests", lambda project, jdk, sources, out, executor, **options:
                        _build.Compilation(lb.Outcome.COMPLETED, classes=out))
    monkeypatch.setattr(_tools, "toolset", lambda classpath, environment, **options:
                        _tools.Toolset("1.14.4", ()))
    monkeypatch.setattr(_runs, "discover", lambda *arguments, **options: (
        lb.Outcome.COLLECTION_FAILED, "JUnit discovery failed", "an engine failed\n", 1, 0.1))
    report = lb.verify_project("java", request)
    discovery = _item(report, "test_discovery")
    assert not discovery.required and discovery.tried_by is None
    assert discovery.outcome is lb.Outcome.COLLECTION_FAILED
    assert "JUnit discovery failed" in discovery.reason and "an engine failed" in discovery.reason
    assert _item(report, "test_compilation").outcome is lb.Outcome.COMPLETED
    assert report.outcome is lb.Outcome.COMPLETED


def _with_coverage(request, cached):
    """The request requiring JaCoCo, with its jars in the tool cache or not."""
    from langadapter.java import _jacoco, _tools

    if cached:
        cache = Path(request.context.environment["LANGADAPTER_JAVA_TOOLS"])
        for coordinates in (_jacoco.AGENT, *_jacoco.CORE):
            jar = cache / _tools._relative(coordinates)
            jar.parent.mkdir(parents=True, exist_ok=True)
            jar.write_bytes(b"jar")
    return replace(request, required_facets={"tests.run_suite": ("coverage",)})


def test_a_cached_tool_that_does_not_start_fails_verification(controlled):
    """JaCoCo's agent does not start on the selected JDK: the run would fail
    the same way, so the required tool item fails with the JVM's output."""
    request, state = controlled
    state.agent = (1, "Error opening zip file or JAR manifest missing")
    report = lb.verify_project("java", _with_coverage(request, cached=True))
    jacoco = _item(report, "jacoco")
    assert len(state.agents) == 1
    assert jacoco.required and jacoco.tried_by is None
    assert jacoco.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert "did not start" in jacoco.reason and "JAR manifest missing" in jacoco.reason
    assert report.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE


def test_a_tool_that_needs_a_download_first_is_left_to_the_first_run(controlled):
    """JaCoCo's jars are not in the tool cache: the check starts nothing, and
    the first run, which downloads them, tries the tool."""
    request, state = controlled
    report = lb.verify_project("java", _with_coverage(request, cached=False))
    jacoco = _item(report, "jacoco")
    assert state.agents == []
    assert jacoco.required and jacoco.tried_by == "run" and jacoco.outcome is None
    assert jacoco.action == DOWNLOAD_STEP
    assert report.outcome is lb.Outcome.COMPLETED


def _central(monkeypatch, answer):
    """Maven Central as the tool cache reaches it: each fetch is recorded and
    gets ``answer``, the bytes of a jar or an exception to raise."""
    import urllib.request

    fetched = []

    def urlopen(url, timeout=None):
        fetched.append(url)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(answer)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return fetched


def test_a_check_that_may_download_fetches_and_starts_a_tool(controlled, monkeypatch):
    """With acquisition allowed, the check downloads JaCoCo's jars into the
    tool cache and starts the agent once, so no run has to."""
    from langadapter.java import _jacoco

    request, state = controlled
    fetched = _central(monkeypatch, b"jar")
    report = lb.verify_project("java", replace(_with_coverage(request, cached=False),
                                               allow_acquisition=True))
    jacoco = _item(report, "jacoco")
    assert len(fetched) == 5 and len(state.agents) == 1
    assert jacoco.outcome is lb.Outcome.COMPLETED and jacoco.tried_by is None
    assert jacoco.reason == "started once on the selected JDK"
    assert jacoco.selected == request.context.environment["LANGADAPTER_JAVA_TOOLS"]
    assert jacoco.version == _jacoco.VERSION


def test_a_download_the_check_could_not_make_reports_its_failure(controlled, monkeypatch):
    """The check may download, and Maven Central does not answer: the item
    reports the failed download, and nothing starts."""
    import urllib.error

    request, state = controlled
    _central(monkeypatch, urllib.error.URLError("no route to host"))
    report = lb.verify_project("java", replace(_with_coverage(request, cached=False),
                                               allow_acquisition=True))
    jacoco = _item(report, "jacoco")
    assert state.agents == [] and jacoco.tried_by is None
    assert jacoco.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert jacoco.reason.startswith("org.jacoco:")
    assert "could not be fetched from Maven Central" in jacoco.reason
    assert "no route to host" in jacoco.reason
    assert jacoco.action == "fix what the tool cache reports, then run the tool check again"


def test_downloads_turned_off_name_their_variable_even_when_the_check_may_download(
        controlled, monkeypatch):
    """LANGADAPTER_JAVA_TOOLS_OFFLINE keeps the check from downloading: the
    item is missing setup, names the variable and fetches nothing."""
    request, state = controlled
    fetched = _central(monkeypatch, b"jar")
    values = dict(request.context.environment, LANGADAPTER_JAVA_TOOLS_OFFLINE="1")
    context = replace(request.context, environment=lb.Environment(values, inherit=False))
    report = lb.verify_project("java", replace(_with_coverage(request, cached=False),
                                               context=context, allow_acquisition=True))
    jacoco = _item(report, "jacoco")
    assert fetched == [] and state.agents == []
    assert jacoco.outcome is lb.Outcome.MISSING_SETUP
    assert jacoco.reason == ("5 of 5 tool jars are not downloaded, and "
                             "LANGADAPTER_JAVA_TOOLS_OFFLINE turns downloads off")
    assert jacoco.action == ("unset LANGADAPTER_JAVA_TOOLS_OFFLINE, or fill the tool cache "
                             "(LANGADAPTER_JAVA_TOOLS) another way, then run the tool check again")


def test_a_step_that_runs_out_of_time_is_left_to_the_first_run(controlled):
    """The build's time runs out: the first run tries it, and the report's
    outcome is the one the checks before the step give."""
    request, state = controlled
    state.slow = True
    report = lb.verify_project("java", request)
    resolution = _item(report, "build_resolution")
    assert resolution.tried_by == "run" and "ran out of time" in resolution.reason
    assert report.outcome is lb.Outcome.COMPLETED


def _verified(root, home, maven, operations, **options):
    context = lb.ProjectContext(str(root), lb.Interpreter(str(home)), environment=lb.Environment(
        {"LANGADAPTER_MAVEN": maven, **options.pop("values", {})}, inherit=True))
    return lb.verify_project("java", lb.ProjectSetupRequest(
        context, ("src/main/java/demo/Calc.java",), operations, **options))


def test_a_project_whose_runs_succeed_passes_every_new_step(java_project, maven, jdks):
    """With real tools and acquisition allowed: the build resolves, the tests
    compile and are discovered without running, and JaCoCo starts."""
    report = _verified(java_project("maven"), jdks.get(21) or jdks[max(jdks)], maven,
                       ("tests.run_suite",), required_facets={"tests.run_suite": ("coverage",)},
                       allow_acquisition=True)
    resolution = _item(report, "build_resolution")
    assert resolution.required and resolution.outcome is lb.Outcome.COMPLETED and resolution.tried_by is None
    for key in ("test_compilation", "test_discovery"):
        item = _item(report, key)
        assert not item.required and item.outcome is lb.Outcome.COMPLETED and item.tried_by is None
    jacoco = _item(report, "jacoco")
    assert jacoco.outcome is lb.Outcome.COMPLETED and "not yet tried" not in jacoco.reason
    assert report.outcome is lb.Outcome.COMPLETED


@pytest.mark.mutation_tool
def test_pit_starts_once_on_the_resolved_classpath(java_project, maven, jdks):
    report = _verified(java_project("maven"), jdks.get(21) or jdks[max(jdks)], maven,
                       ("mutations.run",), allow_acquisition=True)
    pitest = _item(report, "pitest")
    assert pitest.outcome is lb.Outcome.COMPLETED and pitest.tried_by is None
    assert report.outcome is lb.Outcome.COMPLETED


def test_a_launcher_not_downloaded_leaves_discovery_to_the_first_run(java_project, maven, jdks, tmp_path):
    """An empty tool cache and no acquisition: the build resolves offline from
    the local repository, and discovery waits for the first run's download."""
    root, home = java_project("maven"), jdks.get(21) or jdks[max(jdks)]
    _verified(root, home, maven, ("tests.run_suite",), allow_acquisition=True)
    (tmp_path / "empty").mkdir()
    report = _verified(root, home, maven, ("tests.run_suite",),
                       values={"LANGADAPTER_JAVA_TOOLS": str(tmp_path / "empty")})
    assert _item(report, "build_resolution").outcome is lb.Outcome.COMPLETED
    discovery = _item(report, "test_discovery")
    assert discovery.tried_by == "run" and "not downloaded yet" in discovery.reason
    assert discovery.action == DOWNLOAD_STEP
    assert not any((tmp_path / "empty").iterdir())
    assert report.outcome is lb.Outcome.COMPLETED


def test_a_test_file_that_does_not_compile_is_reported_without_failing_verification(
        java_project, maven, jdks):
    """A run compiles only its suite's files, so a broken file beside the
    tests that compile shows in the report and fails no check."""
    root = java_project("maven")
    (root / "src" / "test" / "java" / "demo" / "BrokenTest.java").write_text(
        "package demo;\nclass BrokenTest { not java }\n", encoding="utf-8")
    report = _verified(root, jdks.get(21) or jdks[max(jdks)], maven, ("tests.run_suite",),
                       allow_acquisition=True)
    compilation = _item(report, "test_compilation")
    assert not compilation.required and compilation.outcome is lb.Outcome.COLLECTION_FAILED
    assert "BrokenTest.java" in compilation.reason
    assert _item(report, "build_resolution").outcome is lb.Outcome.COMPLETED
    assert report.outcome is lb.Outcome.COMPLETED


#: A parent and an imported bill of materials that no repository holds.
PARENT = """  <parent>
    <groupId>org.example.absent</groupId>
    <artifactId>parent</artifactId>
    <version>1.0</version>
    <relativePath/>
  </parent>
"""
BOM = """  <dependencyManagement>
    <dependencies>
      <dependency>
        <groupId>org.example.absent</groupId>
        <artifactId>bom</artifactId>
        <version>1.0</version>
        <type>pom</type>
        <scope>import</scope>
      </dependency>
    </dependencies>
  </dependencyManagement>
"""


@pytest.mark.parametrize("anchor, block", [("  <groupId>demo</groupId>\n", PARENT),
                                           ("  <dependencies>\n", BOM)], ids=["parent", "bom"])
def test_an_uncached_parent_or_bill_of_materials_is_left_to_the_first_run(
        java_project, maven, jdks, anchor, block):
    """Maven reports a model problem with the download below it: offline,
    the first run, which may download, tries the build."""
    root = java_project("maven")
    pom = root / "pom.xml"
    text = pom.read_text(encoding="utf-8")
    assert text.count(anchor) == 1
    pom.write_text(text.replace(anchor, block + anchor), encoding="utf-8")
    report = _verified(root, jdks.get(21) or jdks[max(jdks)], maven, ("tests.run_suite",))
    resolution = _item(report, "build_resolution")
    assert resolution.tried_by == "run" and "needs a download first" in resolution.reason
    assert "org.example.absent" in resolution.reason and "has not been downloaded" in resolution.reason
    assert report.outcome is lb.Outcome.COMPLETED

