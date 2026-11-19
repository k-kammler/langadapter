"""The Java adapter's setup check. Inspection reads the selected JDK, build
files and tool cache without executing a build or wrapper. Verification
also runs the JDK's java and javac version commands in a prepared copy.
When they succeed and an operation needs the build, it then takes a run's
steps there without running a test: it resolves the build, compiles and
discovers the tests and starts each needed measurement tool whose jars are
in the tool cache, PIT only on a resolved build."""
from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path, PurePath, PurePosixPath
import subprocess
import time
from langadapter import capabilities as _actions
from langadapter._environment import selected_executable
from langadapter.setup import (
    _closing,
    _container_inputs,
    _local_inputs,
    _add_missing_input_item,
    _preserve_owner,
    ProjectSetupItem,
    ProjectSetupReport,
    ProjectSetupRequest,
)
from langadapter.capabilities import Availability
from langadapter.context import ContainerExecution, Environment
from langadapter.errors import InvalidRequest
from langadapter.execution import executor_for
from langadapter.java import _build as project, _jacoco, _pit, _runs, _tools, _env as jdk
from langadapter.java._env import JavaEnv
from langadapter.outcomes import Outcome
from langadapter.java._profile import SUPPORT, JavaProfile
from langadapter.java._build import (
    BOTH_BUILDS,
    BOTH_BUILDS_ACTION,
    BUILD_FILES,
    build_systems,
)
from langadapter.versions import java_version, setup_runtime
import posixpath
import re
from langadapter.preparation import prepare_project


_SELECT_JDK = "select the JDK's home directory or the full path of its bin/java"
_SELECT_BUILD = "select the project directory that has the pom.xml or Gradle build file"
#: What the runtime item says inspection left untried.
_UNTRIED = "the project is not compiled or run yet"
#: The next step of an item that needs a download first, which fills the
#: build's local repository and the tool cache.
_DOWNLOAD_RUN = ("a tool check that may download, or a run with network access, downloads what is "
                 "missing once, before any offline run")
#: Each measurement tool's jars in the tool cache and its pinned version.
_TOOL_JARS = {"jacoco": ((_jacoco.AGENT, *_jacoco.CORE), _jacoco.VERSION),
              "pitest": (_pit.JARS, _pit.VERSION)}
#: Each build system's name, wrapper, command and naming variable.
_LAUNCHERS = {"maven": ("Maven", "mvnw", "mvn", "LANGADAPTER_MAVEN"),
              "gradle": ("Gradle", "gradlew", "gradle", "LANGADAPTER_GRADLE")}
_DESCRIPTORS = tuple(name for _, names in BUILD_FILES for name in names)
_WRAPPERS = ("mvnw", "mvnw.cmd", ".mvn/wrapper/maven-wrapper.properties", ".mvn/wrapper/maven-wrapper.jar",
             "gradlew", "gradlew.bat", "gradle/wrapper/gradle-wrapper.properties", "gradle/wrapper/gradle-wrapper.jar")


def _build_required(operation):
    return (operation.execution_backed and operation.name not in (
        "sources.validate_source", "sources.salvage_syntax", "mutations.operators"))


def _file(request, executor, method, path):
    # Every container metadata command gets the remaining per-probe bound.
    deadline = time.monotonic() + request.remaining_timeout("Java metadata observation")
    value = getattr(JavaEnv(executor, deadline=deadline), method)(path)
    request.remaining_timeout("Java metadata observation")
    return value


def _project_inputs(request, executor):
    path = PurePosixPath if isinstance(request.context.execution, ContainerExecution) else Path
    root = path(request.context.root)
    present = tuple(name for name in (*_DESCRIPTORS, *_WRAPPERS)
                    if _file(request, executor, "is_file", root / name))
    plan = request.inputs.effective(present, case_sensitive=path is PurePosixPath or os.name != "nt")
    selected = replace(request, inputs=plan)
    object.__setattr__(selected, "_deadline", request._deadline)
    # Discovery may follow a candidate: admit its lexical path before reading.
    selected = _container_inputs(selected) if path is PurePosixPath else _local_inputs(selected)
    return selected, build_systems(present)


def _runtime(request, executor, required):
    context = request.context
    runtime = context.interpreter
    if runtime is None:
        # Missing setup, required only when an operation needs a JDK.
        return ProjectSetupItem("runtime", required, Availability.MISSING_SETUP, Outcome.MISSING_SETUP,
            reason="no JDK is selected, and the check does not search for one", action=_SELECT_JDK)
    if runtime.path is None:
        return ProjectSetupItem("runtime", required, None, None,
            reason="a Java version was requested instead of a JDK, and the check does not install JDKs",
            action=_SELECT_JDK)
    container = isinstance(context.execution, ContainerExecution)
    path = PurePosixPath if container else Path
    selected = runtime.path
    address = path(selected)
    if not address.is_absolute():
        # Only explicit captured host values, never ambient/current-directory lookup.
        bound = None if container else selected_executable(selected, context.environment)
        if bound is None:
            return ProjectSetupItem("runtime", required, Availability.MISSING_SETUP, Outcome.MISSING_SETUP,
                selected=selected, reason="the selected JDK was found neither at a full path nor on the PATH",
                action=_SELECT_JDK)
        address = path(bound)
        selected = bound
    windows = os.name == "nt" and not container
    name = address.name.lower() if windows else address.name
    parent = address.parent.name.lower() if windows else address.parent.name
    home = address.parent.parent if name in (("java", "java.exe") if windows else ("java",)) and parent == "bin" else address
    java = home / "bin" / ("java.exe" if os.name == "nt" and not container else "java")
    compiler = home / "bin" / ("javac.exe" if os.name == "nt" and not container else "javac")
    try:
        if not all(_file(request, executor, "is_file", value) for value in (home / "release", java, compiler)):
            return ProjectSetupItem("runtime", required, Availability.MISSING_SETUP, Outcome.MISSING_SETUP,
                selected=selected, reason="the selected directory is not a full JDK (release, bin/java or bin/javac is missing)",
                action="select a JDK home, not a JRE, that has release, bin/java and bin/javac")
        version = jdk._version(_file(request, executor, "read", home / "release"))
        if version is None:
            return ProjectSetupItem("runtime", required, None, Outcome.INFRASTRUCTURE_FAILURE,
                selected=selected, reason="the selected JDK's release file cannot be read",
                action="select another JDK, or repair this one's release file")
    except (OSError, UnicodeError, subprocess.SubprocessError) as error:
        _preserve_owner(error)
        outcome = (Outcome.TIMED_OUT if isinstance(error, subprocess.TimeoutExpired) else
                   Outcome.MISSING_SETUP if isinstance(error, FileNotFoundError) else Outcome.INFRASTRUCTURE_FAILURE)
        return ProjectSetupItem("runtime", required,
            Availability.MISSING_SETUP if outcome is Outcome.MISSING_SETUP else None, outcome,
            selected=selected, reason="reading the selected JDK's files did not complete",
            action=(_SELECT_JDK if outcome is Outcome.MISSING_SETUP else _actions.RETRY
                    if outcome is Outcome.TIMED_OUT else "check that the JDK directory is readable, or select another JDK"))
    found = java_version(version, "guest probe" if container else "release file")
    profile = JavaProfile(found, SUPPORT)
    if profile.refusal():
        return ProjectSetupItem("runtime", required, Availability.UNSUPPORTED, Outcome.UNSUPPORTED,
            selected=selected, reason=profile.refusal(),
            action=f"select a JDK {JavaProfile.minimum_jdk} or newer")
    note = profile.note()
    return ProjectSetupItem("runtime", required, Availability.AVAILABLE, Outcome.COMPLETED,
        selected=selected, version=setup_runtime(found), version_record=found,
        reason=f"JDK found, and {_UNTRIED}"
               + (f". {note}" if note else ""))


_MUTATION_EXECUTION = ("mutations.open_session", "mutations.run", "mutations.per_suite")


def _tool_uses(request, operation):
    """The cached measurement tools an operation uses, its facets included."""
    if operation == "tests.per_test_evidence" or "coverage" in request.facets(operation):
        return ("jacoco",)
    return ("pitest",) if operation in _MUTATION_EXECUTION else ()


def _tools_item(request, key, required, coordinates, version):
    """One tool's jars in the host cache, observed without downloading. An
    uncached jar is tried by the first run, which fetches it, or is missing
    setup when downloads are off (LANGADAPTER_JAVA_TOOLS_OFFLINE), as is a
    cache location that cannot be found."""
    # The cache is a host path. Runs read it from the same values.
    environment = _tools.host_environment(request.context)
    request.remaining_timeout("Java tool cache observation")
    try:
        directory = _tools.cache_dir(environment)
        absent = _tools.uncached(coordinates, environment)
    except _tools.ToolUnavailable as error:
        return ProjectSetupItem(key, required, Availability.MISSING_SETUP, Outcome.MISSING_SETUP,
                                reason=str(error), action="set LANGADAPTER_JAVA_TOOLS to the Java tool cache directory")
    if absent and _tools.offline(environment):
        return ProjectSetupItem(key, required, Availability.MISSING_SETUP, Outcome.MISSING_SETUP,
            reason=(f"{len(absent)} of {len(coordinates)} tool jars are not downloaded, and "
                    f"{_tools.OFFLINE} turns downloads off"),
            action=(f"unset {_tools.OFFLINE}, or fill the tool cache (LANGADAPTER_JAVA_TOOLS) another "
                    "way, then run the tool check again"))
    if absent:
        return ProjectSetupItem(key, required, None, None,
            reason=(f"{len(absent)} of {len(coordinates)} tool jars are not downloaded yet, and the first "
                    "run downloads them, which needs network access"),
            action=_DOWNLOAD_RUN, tried_by="run")
    return ProjectSetupItem(key, required, None, Outcome.COMPLETED, selected=str(directory), version=version,
                            reason="downloaded, not yet tried", action=_actions.RUN, tried_by="run")


def _launcher_item(request, executor, tool, required):
    """The build's launcher as a run finds it, unstarted: the wrapper, else
    LANGADAPTER_MAVEN's or LANGADAPTER_GRADLE's, else the command on PATH."""
    name, wrapper, command, variable = _LAUNCHERS[tool]
    deadline = time.monotonic() + request.remaining_timeout("Java build launcher lookup")
    try:
        found = project.launcher(request.context.root, tool, request.context.environment, executor,
                                 deadline=deadline)
        request.remaining_timeout("Java build launcher lookup")
    except Exception as error:  # a run reports the same lookup as not completed
        _preserve_owner(error)
        outcome = (Outcome.TIMED_OUT if isinstance(error, subprocess.TimeoutExpired)
                   else Outcome.INFRASTRUCTURE_FAILURE)
        return ProjectSetupItem("build_launcher", required, None, outcome,
            reason=f"looking for the {name} launcher did not complete",
            action=(_actions.RETRY if outcome is Outcome.TIMED_OUT
                    else f"check that the project's {name} build files are readable"))
    if found is None:
        return ProjectSetupItem("build_launcher", required, Availability.MISSING_SETUP, Outcome.MISSING_SETUP,
            reason=(f"no {name} launcher: the project has no {wrapper} wrapper, {variable} names none "
                    f"and {command} is not on the PATH"),
            action=(f"add the project's {name} wrapper ({wrapper}), install {name} so {command} is "
                    f"on the PATH, or set {variable} to its launcher"))
    return ProjectSetupItem("build_launcher", required, None, Outcome.COMPLETED, selected=found,
                            reason="found, and the build is not tried yet", action=_actions.RUN, tried_by="run")


def _operations(adapter, runtime, build, request=None, tools=(), *, verification=False, launcher=None):
    missing_tools = {item.key for item in tools if item.readiness is Availability.MISSING_SETUP}
    result = []
    for item in adapter.operation_capabilities():
        uses = () if request is None else _tool_uses(request, item.name)
        if not item.supported:
            result.append(item)
        elif item.execution_backed and runtime.readiness is Availability.UNSUPPORTED:
            result.append(replace(item, readiness=Availability.UNSUPPORTED,
                                  reason=runtime.reason, action=runtime.action))
        elif item.execution_backed and runtime.readiness is Availability.MISSING_SETUP:
            result.append(replace(item, readiness=Availability.MISSING_SETUP,
                                  reason="needs a JDK, which is missing", action=_SELECT_JDK))
        elif item.execution_backed and _build_required(item) and build.readiness is Availability.MISSING_SETUP:
            result.append(replace(item, readiness=Availability.MISSING_SETUP,
                                  reason="needs a Maven or Gradle build file, which is missing",
                                  action=_SELECT_BUILD))
        elif item.execution_backed and _build_required(item) and build.ambiguous:
            # Nothing tries the build before one build system is chosen.
            result.append(replace(item, readiness=None, reason="waits for one build system to be chosen",
                                  action=build.action))
        elif (item.execution_backed and _build_required(item) and launcher is not None
                and launcher.readiness is Availability.MISSING_SETUP):
            result.append(replace(item, readiness=Availability.MISSING_SETUP,
                                  reason=f"needs a {_LAUNCHERS[build.selected][0]} launcher, which is missing",
                                  action=launcher.action))
        elif set(uses) & missing_tools:
            result.append(replace(item, readiness=Availability.MISSING_SETUP,
                                  reason="needs measurement tools that are not downloaded",
                                  action=next(tool.action for tool in tools if tool.key in uses
                                              and tool.readiness is Availability.MISSING_SETUP)))
        else:
            # Verification tries the JDK and the build. A run tries the operations.
            result.append(replace(item, readiness=None, reason="not yet tried on this project",
                                  action=_actions.VERIFY if item.execution_backed and not verification
                                  else _actions.RUN,
                                  tried_by="verification" if item.execution_backed and not verification
                                  else "run"))
    return tuple(result)


def inspect(adapter, request, *, verification=False):
    if not isinstance(request, ProjectSetupRequest):
        raise InvalidRequest("request", "must be a ProjectSetupRequest")
    executor = executor_for(request.context)
    request, tools = _project_inputs(request, executor)
    requested = tuple(item for item in adapter.operation_capabilities() if item.name in request.required_operations)
    build_required = any(_build_required(item) for item in requested)
    build = ProjectSetupItem("build_descriptor", build_required,
        Availability.MISSING_SETUP if not tools else None,
        Outcome.MISSING_SETUP if not tools else Outcome.COMPLETED,
        selected=tools[0] if len(tools) == 1 else None, candidates=tools if len(tools) > 1 else (),
        ambiguous=len(tools) > 1,
        reason=(BOTH_BUILDS if len(tools) > 1 else
                "build file found, and the build is not tried yet" if tools else
                "no Maven or Gradle build file was found in the selected project directory"),
        action=(BOTH_BUILDS_ACTION if len(tools) > 1 else
                _actions.RUN if tools else _SELECT_BUILD),
        tried_by="run" if len(tools) == 1 else None)
    launcher = (_launcher_item(request, executor, tools[0], build_required)
                if len(tools) == 1 else None)
    runtime = _runtime(request, executor, any(item.execution_backed for item in requested))
    uses = {tool for item in requested for tool in _tool_uses(request, item.name)}
    tools = (_tools_item(request, "jacoco", "jacoco" in uses, *_TOOL_JARS["jacoco"]),
             _tools_item(request, "pitest", "pitest" in uses, *_TOOL_JARS["pitest"]))
    context = request.context
    if (runtime.required and runtime.outcome is not Outcome.COMPLETED) or (build.required and build.ambiguous):
        context = None
    outcome = runtime.outcome if runtime.required and runtime.outcome not in (None, Outcome.COMPLETED) else Outcome.COMPLETED
    if runtime.outcome is None and runtime.required:
        outcome = Outcome.UNSUPPORTED
    return ProjectSetupReport("inspection", outcome,
                              "read the selected JDK, build files and tool cache, and ran nothing", context,
                              request.inputs, (runtime, build, *(() if launcher is None else (launcher,)), *tools),
                              _operations(adapter, runtime, build, request, tools, verification=verification,
                                          launcher=launcher))


_VERSIONS = {
    "jvm": re.compile(r'^(?:openjdk|java) (?:version "([^"\r\n]+)"|([0-9][^\s]*)(?:\s|$))', re.MULTILINE),
    "compiler": re.compile(r"^javac ([0-9][^\s]*)(?:\s|$)", re.MULTILINE),
}


def _verified_operations(metadata, items):
    missing = [item for item in items if item.required and item.outcome is Outcome.MISSING_SETUP]
    return tuple(replace(operation, readiness=Availability.MISSING_SETUP,
                         reason="needs the missing " + " and ".join(item.key.replace("_", " ") for item in missing),
                         action=missing[0].action, tried_by=None)
                 if operation.supported and operation.execution_backed and missing
                 and operation.readiness is not Availability.MISSING_SETUP else operation
                 for operation in metadata.operations)


#: The reason of a verification that ran the version commands.
_RAN = ("ran the JDK's java and javac version commands in a project copy, and the build and "
        "tests were not run")


def _report(metadata, outcome, items, *, reason, context=None):
    return ProjectSetupReport("verification", outcome, reason, context,
                              metadata.inputs, tuple(items), _verified_operations(metadata, items))


#: Verification's steps after the version commands, as runs take them, and
#: whether each counts for the exit code. A run compiles and discovers only
#: the tests it selects, so compilation and discovery report without being
#: required.
_STEPS = (("build_resolution", True), ("test_compilation", False), ("test_discovery", False))
#: The items whose failure in these steps fails verification when required.
_TRIED_KEYS = frozenset((*(key for key, _ in _STEPS), "jacoco", "pitest"))
#: After an offline resolution failed, the build's output (Maven 3.9.16's
#: and Gradle 9.8.0's own words): pairs of texts on one line that a download
#: may fix, which win, and texts of a failure a run meets too.
_BUILD_FAILURES = ("Some problems were encountered while processing the POMs", "Non-readable POM",
                   "The build could not read", "Could not compile build file",
                   "A problem occurred evaluating", "COMPILATION ERROR", "Compilation failed")
_DOWNLOAD_FIRST = (("in offline mode and the artifact", "has not been downloaded from it before"),
                   ("No cached version of", "available for offline mode"))
#: The reason of a step the check had too little time for.
_NO_TIME = "the check ran out of time before this step ended, so the first run tries it"
#: The reason of a step that waits for the steps before it.
_WAITING = "not tried, since an earlier step did not succeed"
#: The reason of a verification that also tried the build.
_TRIED = ("ran the JDK's version commands and tried the build, the tests' compilation and "
          "discovery and the needed measurement tools it could start in a project copy, without "
          "running a test")
#: The steps' reason when the build's wrapper may fetch its build tool first.
_WRAPPED = ("the build's wrapper may download its build tool before the build starts, "
            "so the first run tries this step")
#: The reason of a verification whose build the project's wrapper leaves to the first run.
_WRAPPER_LEFT = ("ran the JDK's version commands and tried the needed measurement tools it could "
                 "start without the build in a project copy, and left the build to the first run, "
                 "since the build's wrapper may download its build tool")
#: The reason of a verification that left steps to the first run.
_LEFT = ("ran the JDK's version commands, tried in a project copy what it could without running "
         "a test, and left the other steps to the first run")
#: The build items' reason once the check's build did not resolve the project.
_BUILD_UNRESOLVED = "found, and the check's build did not resolve the project"


def _set(items, key, **fields):
    """Item ``key`` replaced by a copy with ``fields``."""
    index = next(index for index, item in enumerate(items) if item.key == key)
    items[index] = replace(items[index], **fields)


def _tried(items, key, outcome, reason, action=None):
    _set(items, key, outcome=outcome, reason=reason, action=action, tried_by=None,
         readiness=Availability.AVAILABLE if outcome is Outcome.COMPLETED else
         Availability.MISSING_SETUP if outcome is Outcome.MISSING_SETUP else None)


def _for_run(items, key, reason, action=_actions.RUN):
    _set(items, key, reason=reason, action=action, tried_by="run")


def _no_downloads(environment):
    """``environment`` with the tool cache's downloads off, captured or not as it was."""
    values = {**environment, _tools.OFFLINE: "1"}
    return Environment(values, inherit=environment.inherit) if isinstance(environment, Environment) else values


def _download_gap(resolution, offline):
    """For an offline resolution that failed where a run that may download
    may succeed: the output's line naming the download ("" when no marker
    names one) and the reason that leaves it to the first run. None for a
    failure every run meets."""
    output = (resolution.output or "").strip()
    if not offline or resolution.outcome is not Outcome.INFRASTRUCTURE_FAILURE:
        return None
    download = next((line.strip() for line in output.splitlines() for first, second in _DOWNLOAD_FIRST
                     if first in line and second in line), None)
    if download:
        return download, f"the build needs a download first, which the first run tries: {download}"
    if any(text in output for text in _BUILD_FAILURES):
        return None
    return "", (f"the build failed offline for a reason the check does not know, so the first run, "
                f"which may download, tries it: {resolution.reason}: {output[-1000:]}")


def _resolve(copy, request, items, scratch):
    """``build_resolution``: the run's resolution of the copy, offline unless
    the request allows acquisition. The project, when it resolved."""
    offline = not request.allow_acquisition
    deadline = time.monotonic() + request.remaining_timeout("Java build resolution", probe=False)
    resolution = project.resolve(copy.context.root, copy._jdk(), copy._executor, str(scratch / "resolution"),
                                 environment=copy.context.environment, deadline=deadline, offline=offline)
    if resolution.outcome is Outcome.COMPLETED:
        # The copy's runs, discovery among them, use this resolution.
        copy._resolution = resolution
        for key in ("build_descriptor", "build_launcher"):
            _tried(items, key, Outcome.COMPLETED, "found, and the build resolved the project")
        _tried(items, "build_resolution", Outcome.COMPLETED, "the build resolved the project as a run does")
        return resolution.project
    _unresolved(copy, resolution, _download_gap(resolution, offline), items)
    return None


def _unresolved(copy, resolution, gap, items):
    """``build_resolution`` after a resolution that did not complete, and the
    build's items, which take its next step."""
    output = (resolution.output or "").strip()
    # A run that builds offline too fails the same way, so the check reports the gap itself
    # instead of leaving it to the first run.
    tool = next(item for item in items if item.key == "build_descriptor").selected
    runs_offline = gap is not None and project.builds_offline(
        copy.context.root, tool, copy.context.environment, copy._executor)
    if gap and gap[0] and runs_offline:
        _tried(items, "build_resolution", Outcome.MISSING_SETUP,
               f"the build needs a download, and the project's runs build offline: {gap[0]}",
               "add the artifact to the build's local repository, or let the project's runs build "
               "online, then run the tool check again")
    elif gap and not runs_offline:
        _for_run(items, "build_resolution", gap[1], _DOWNLOAD_RUN)
    elif resolution.outcome is Outcome.TIMED_OUT:
        _for_run(items, "build_resolution", _NO_TIME)
    else:
        _tried(items, "build_resolution", resolution.outcome,
               resolution.reason + (f": {output[-1000:]}" if output else ""),
               "fix what the build reports, then run the tool check again")
    # The build's own item says why, and its next step is theirs.
    action = next(item for item in items if item.key == "build_resolution").action
    for key in ("build_descriptor", "build_launcher"):
        _set(items, key, reason=_BUILD_UNRESOLVED, action=action)


def _test_sources(built, executor, deadline):
    """The project's test source files, as its commands see them."""
    files = JavaEnv(executor, deadline=deadline)
    roots = [root for root in built.test_sources if files.is_dir(root)]
    if files.container and roots:
        done = files.run(["find", *roots, "-type", "f", "-name", "*.java"], 60, "test source listing")
        if done.returncode:
            raise OSError(f"cannot list the container's test sources: {done.stderr[-500:]}")
        return sorted(done.stdout.splitlines())
    return sorted(str(path) for root in roots for path in Path(root).rglob("*.java"))


def _compile(copy, built, request, items, scratch):
    """``test_compilation``: every test source compiled as a run compiles a
    suite, into a fresh directory. That directory, when they compiled."""
    deadline = time.monotonic() + request.remaining_timeout("Java test compilation", probe=False)
    try:
        sources = _test_sources(built, copy._executor, deadline)
    except OSError as error:
        _tried(items, "test_compilation", Outcome.INFRASTRUCTURE_FAILURE, str(error), _actions.RETRY)
        return None
    if not sources:
        _tried(items, "test_compilation", Outcome.COMPLETED, "the project has no test sources")
        _tried(items, "test_discovery", Outcome.NO_TESTS, "the project has no test sources")
        return None
    classes = scratch / "test-classes"
    listed = _tools.argfile(scratch / "test-sources.args", sources)
    compiled = project.compile_tests(built, copy._jdk(), [f"@{JavaEnv(copy._executor).translate(listed)}"],
                                     str(classes), copy._executor, environment=copy.context.environment,
                                     deadline=deadline)
    if compiled.outcome is Outcome.TIMED_OUT:
        _for_run(items, "test_compilation", _NO_TIME)
    elif compiled.outcome is Outcome.COMPLETED:
        _tried(items, "test_compilation", Outcome.COMPLETED, "the test sources compile as a run compiles them")
        return classes
    else:
        _tried(items, "test_compilation", compiled.outcome,
               f"the test sources do not compile: {compiled.diagnostics[-1000:]}",
               "fix what the compiler reports, then run the tool check again")
    return None


def _discover(copy, built, request, items, scratch, classes):
    """``test_discovery``: JUnit lists the compiled test classes as a run's
    collection does, without running a test. Its launcher comes from the
    tool cache, which downloads only when the request allows acquisition."""
    names = sorted(path.relative_to(classes).with_suffix("").as_posix().replace("/", ".")
                   for path in classes.rglob("*.class") if "$" not in path.stem and "-" not in path.stem)
    environment = _tools.host_environment(copy.context)
    left = request.remaining_timeout("Java test discovery", probe=False)
    deadline = time.monotonic() + left
    try:
        tools = _tools.toolset(built.run_classpath(),
                               environment if request.allow_acquisition else _no_downloads(environment),
                               executor=copy._executor, scratch=scratch / "junit", deadline=deadline)
    except _tools.ToolUnavailable as error:
        if request.allow_acquisition or _tools.offline(environment):
            _tried(items, "test_discovery", Outcome.MISSING_SETUP, str(error))
        else:
            _for_run(items, "test_discovery",
                     "the JUnit launcher's jars are not downloaded yet, and the first run downloads them",
                     _DOWNLOAD_RUN)
        return
    except OSError as error:
        _preserve_owner(error)
        _tried(items, "test_discovery", Outcome.INFRASTRUCTURE_FAILURE, str(error), _actions.RETRY)
        return
    outcome, reason, output, _, _ = _runs.discover(copy, scratch, str(classes), names, tools, left,
                                                   deadline=deadline)
    if outcome is Outcome.TIMED_OUT:
        _for_run(items, "test_discovery", _NO_TIME)
    elif outcome is Outcome.COMPLETED:
        _tried(items, "test_discovery", outcome, "JUnit discovered the tests without running them")
    else:
        _tried(items, "test_discovery", outcome, reason + (f": {output.strip()[-1000:]}" if output.strip() else ""),
               "fix what JUnit's discovery reports, then run the tool check again")


def _start(copy, built, request, items, scratch, key):
    """A measurement tool an operation needs, started once on the selected
    JDK: JaCoCo's agent on ``java -version``, PIT's entry point with its help
    on the resolved classpath. Its jars come from the tool cache. When the
    request allows acquisition, the missing ones are downloaded first, within
    the check's time."""
    item = next(item for item in items if item.key == key)
    if not (item.required and item.tried_by == "run" and (key == "jacoco" or built is not None)
            and (item.outcome is Outcome.COMPLETED or request.allow_acquisition)):
        return
    jdk_found, executor = copy._jdk(), copy._executor
    environment = _tools.host_environment(copy.context)
    environment = environment if request.allow_acquisition else _no_downloads(environment)
    jars, version = _TOOL_JARS[key]
    start_dir = scratch / key
    try:
        start_dir.mkdir()
        _fetch(request, jars, environment, key)
        if key == "jacoco":
            tool = _jacoco.agent_argument(start_dir / "start.exec", ("LangAdapterStart",), environment,
                                          executor=executor, scratch=start_dir)
            argv = [jdk_found.tool("java"), tool, "-version"]
        else:
            staged = _pit.classpath(environment, executor=executor, scratch=start_dir)
            argv = [jdk_found.tool("java"), "-cp", JavaEnv(executor).path_list((*staged, *built.run_classpath())),
                    _pit.REPORT_CLASS, "-h"]
        done = JavaEnv(executor).run(argv, request.remaining_timeout(f"Java {key} start"), f"{key} start",
                                     cwd=copy.context.root, env=project.tool_environment(
                                         jdk_found, copy.context.environment, executor=executor))
    except subprocess.TimeoutExpired:
        _for_run(items, key, _NO_TIME)
        return
    except _tools.ToolUnavailable as error:
        # A jar the cache does not hold or match, or a download that failed.
        _preserve_owner(error)
        reason, action = str(error), "fix what the tool cache reports, then run the tool check again"
    except OSError as error:
        _preserve_owner(error)
        reason = f"did not start on the selected JDK: {error}"
        action = "check that the tool cache can be read, then run the tool check again"
    else:
        if done.returncode == 0:
            _tried(items, key, Outcome.COMPLETED, "started once on the selected JDK")
            _set(items, key, selected=str(_tools.cache_dir(environment)), version=version)
            return
        reason = f"did not start on the selected JDK: {(done.stdout + done.stderr).strip()[-1000:]}"
        action = "select a JDK the tool supports, then run the tool check again"
    _set(items, key, outcome=Outcome.INFRASTRUCTURE_FAILURE, version=None, tried_by=None,
         reason=reason, action=action)


def _fetch(request, jars, environment, key):
    """``jars`` from the tool cache, those missing downloaded first when
    ``environment`` allows downloads, all within the check's remaining time."""
    deadline = time.monotonic() + request.remaining_timeout(f"Java {key} download", probe=False)
    for coordinates in jars:
        _tools.jar(coordinates, environment, deadline=deadline)


def _after_build(items, tried):
    """The texts that the steps make untrue, replaced, those about the build
    only when it was ``tried``, and the outcome: the first required step's
    failure, else completed."""
    if tried:
        for key in _VERSIONS:
            _set(items, key, reason="runs")
        runtime = next(item for item in items if item.key == "runtime")
        _set(items, "runtime",
             reason=runtime.reason.replace(f", and {_UNTRIED}", ", and the build was tried in a project copy"))
    _set(items, "native_execution", reason="the project's tests and measurements are not run yet")
    failed = [item for item in items if item.key in _TRIED_KEYS and item.required
              and item.tried_by is None and item.outcome is not Outcome.COMPLETED]
    return failed[0].outcome if failed else Outcome.COMPLETED


def _untried(runtime, build):
    """The items verification adds, none tried yet: the version commands,
    the build's steps when an operation needs the build, and the run."""
    # Not attempted while an earlier problem stands. Fixing it lets the check try them.
    versions = [ProjectSetupItem(key, runtime.required, None, None,
        reason="its version command was not run",
        action="fix the problems reported, then run the tool check again" if runtime.required else None,
        tried_by="verification" if runtime.required else None) for key in _VERSIONS]
    # Each step waits for the ones before it. The first run tries what the check does not.
    steps = [ProjectSetupItem(key, required, None, None, reason=_WAITING, action=_actions.RUN, tried_by="run")
             for key, required in _STEPS if build.required]
    return (*versions, *steps, ProjectSetupItem("native_execution", runtime.required, None, None,
        reason="the project's build, tests and measurement tools are not tried yet", action=_actions.RUN,
        tried_by="run"))


def _wrapped(items, request):
    """Whether the build's launcher is a wrapper (``mvnw``, ``gradlew``), which may
    fetch its build tool before the offline option applies, while the
    request allows no acquisition."""
    launcher = next((item for item in items if item.key == "build_launcher"), None)
    wrappers = {wrapper for _, wrapper, _, _ in _LAUNCHERS.values()}
    return (not request.allow_acquisition and launcher is not None and launcher.selected is not None
            and PurePath(launcher.selected).stem in wrappers)


def _try_build(adapter, context, request, items):
    """Verification's steps in the prepared ``context``, as runs take them:
    the build's resolution, the tests' compilation and discovery and the
    start of each required measurement tool. The outcome is the first
    required step's failure, else completed, and the report's reason. A
    step left to the first run, for a download or for want of time, does
    not count."""
    copy, built, wrapped = type(adapter)(context), None, _wrapped(items, request)
    with copy._scratch("verify") as scratch:
        scratch = Path(scratch)
        try:
            if wrapped:
                for key, _ in _STEPS:
                    _for_run(items, key, _WRAPPED)
            else:
                built = _resolve(copy, request, items, scratch)
            classes = built and _compile(copy, built, request, items, scratch)
            if classes:
                _discover(copy, built, request, items, scratch, classes)
        except subprocess.TimeoutExpired:
            for key, _ in _STEPS:
                if next(item for item in items if item.key == key).reason == _WAITING:
                    _for_run(items, key, _NO_TIME)
        for key in ("jacoco", "pitest"):
            _start(copy, built, request, items, scratch, key)
    return (_after_build(items, not wrapped),
            _WRAPPER_LEFT if wrapped else _LEFT if _left_to_run(items) else _TRIED)


def _left_to_run(items):
    """Whether verification left a build step or a required tool to the first run."""
    steps = dict(_STEPS)
    return any(item.key in _TRIED_KEYS and item.tried_by == "run" and (item.required or item.key in steps)
               for item in items)


def _home(address, *, container):
    path = PurePosixPath(address) if container else Path(address)
    windows = os.name == "nt" and not container
    name = path.name.lower() if windows else path.name
    parent = path.parent.name.lower() if windows else path.parent.name
    return path.parent.parent if name in (("java", "java.exe") if windows else ("java",)) and parent == "bin" else path


def _observe(context, request, items, commands):
    executor = executor_for(context)
    for key, command in commands:
        try:
            completed = executor.run([command, "-version"], cwd=context.root,
                                     timeout=request.remaining_timeout(f"Java {key} version verification"))
            request.remaining_timeout(f"Java {key} version completion", probe=False)
        except (OSError, subprocess.SubprocessError) as error:
            _preserve_owner(error)
            outcome = (Outcome.TIMED_OUT if isinstance(error, subprocess.TimeoutExpired) else
                       Outcome.MISSING_SETUP if isinstance(error, FileNotFoundError) else Outcome.INFRASTRUCTURE_FAILURE)
            version = None
        else:
            text = (completed.stdout + "\n" + completed.stderr
                    if isinstance(completed.stdout, str) and isinstance(completed.stderr, str) else "")
            match = _VERSIONS[key].search(text)
            version = next((group for group in match.groups() if group), None) if match else None
            if version is not None and re.fullmatch(r"[0-9][0-9A-Za-z._+-]*", version) is None:
                version = None
            outcome = Outcome.COMPLETED if completed.returncode == 0 and version is not None else Outcome.INFRASTRUCTURE_FAILURE
        item = next(item for item in items if item.key == key)
        items[items.index(item)] = replace(item, outcome=outcome,
            readiness=Availability.AVAILABLE if outcome is Outcome.COMPLETED else
                      Availability.MISSING_SETUP if outcome is Outcome.MISSING_SETUP else None,
            selected=command, version=version if outcome is Outcome.COMPLETED else None,
            reason=f"runs, and {_UNTRIED}"
                   if outcome is Outcome.COMPLETED else f"{command} -version did not succeed",
            tried_by=None,
            action=(None if outcome is Outcome.COMPLETED else _SELECT_JDK if outcome is Outcome.MISSING_SETUP
                    else _actions.RETRY if outcome is Outcome.TIMED_OUT
                    else f"run {command} -version and fix what it reports, or select another JDK"))
        if outcome is not Outcome.COMPLETED:
            for later in commands[[name for name, _ in commands].index(key) + 1:]:
                pending = next(item for item in items if item.key == later[0])
                items[items.index(pending)] = replace(
                    pending, action=f"fix {command} first, then run the tool check again")
            return outcome
    return Outcome.COMPLETED


def verify(adapter, request):
    metadata = inspect(adapter, request, verification=True)
    items = list(metadata.items)
    runtime = next(item for item in items if item.key == "runtime")
    build = next(item for item in items if item.key == "build_descriptor")
    items.extend(_untried(runtime, build))
    if metadata.context is None:
        return _report(metadata, metadata.outcome, items, reason=metadata.reason)
    if not runtime.required:
        return _report(metadata, Outcome.COMPLETED, items, context=request.context,
                       reason="no requested operation needs the JDK, so no version command ran")
    if any(item.required and item.outcome is Outcome.MISSING_SETUP for item in items):
        return _report(metadata, Outcome.MISSING_SETUP, items,
                       reason="a required item is missing, so no version command ran")
    container = isinstance(request.context.execution, ContainerExecution)
    path = PurePosixPath if container else Path
    normalize = posixpath.normpath if container else os.path.abspath
    home = _home(runtime.selected, container=container)
    try:
        path(normalize(str(home))).relative_to(path(normalize(request.context.root)))
    except ValueError:
        pass
    else:
        items.append(ProjectSetupItem("runtime_role", True, None, Outcome.UNSUPPORTED,
            reason="the tool check does not try a JDK inside the project directory",
            action="select a JDK outside the project directory"))
        return _report(metadata, Outcome.UNSUPPORTED, items,
                       reason="the JDK lies inside the project directory, so no version command ran")
    suffix = ".exe" if os.name == "nt" and not container else ""
    commands = (("jvm", str(home / "bin" / ("java" + suffix))),
                ("compiler", str(home / "bin" / ("javac" + suffix))))
    selected = replace(request.context, interpreter=replace(request.context.interpreter, path=commands[0][1]))
    try:
        owner = prepare_project(selected, inputs=metadata.inputs,
                                timeout_s=request.remaining_timeout("Java verification preparation", probe=False))
    except InvalidRequest as error:
        if not _add_missing_input_item(error, items, "JDK"):
            raise
        return _report(metadata, Outcome.MISSING_SETUP, items,
                       reason="the selected JDK or the project directory was not found, so no "
                              "version command ran")
    with _closing(owner, "Java verification"):
        with owner.use() as context:
            outcome, reason = _observe(context, request, items, commands), _RAN
            if outcome is Outcome.COMPLETED and build.required:
                outcome, reason = _try_build(adapter, context, request, items)
        return _report(metadata, outcome, items, reason=reason,
                       context=request.context if outcome is Outcome.COMPLETED else None)
