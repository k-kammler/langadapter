"""A Java project as its build tool resolved it, and the compilation of
test sources against it.

The build tool only resolves, so a project measures alike whichever tool
builds it: the build compiles the project as configured and reports the
source and test roots, compiled classes, test runtime classpath, Java
release and test JVM settings. Every measurement runs on that model with
the same tools.
"""
from __future__ import annotations

from langadapter._execution_scope import reraise_unresolved
import json
import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple, Union
from langadapter.sources import bounded
from langadapter._environment import captured, launch_environment
from langadapter.context import Environment
from langadapter.execution import ContainerExecutor, Executor
from langadapter.java import _env as jdk_module
from langadapter.java._env import Jdk, JavaEnv, feature_of, require_mounted_output, remaining
from langadapter.java._profile import JvmProfile
from langadapter.outcomes import Outcome
from langadapter.suites import Suite


#: Launcher jars a run leaves out of the test classpath: it supplies its own.
LAUNCHER_JARS = ("junit-platform-launcher-", "junit-platform-console-")
#: The JDK a build tool needing a newer Java runs on (Gradle 9: 17). Tests
#: still compile and run on the context's JDK.
BUILD_JDK = "LANGADAPTER_BUILD_JAVA_HOME"
_NEEDS_NEWER_JVM = re.compile(r"requires (?:JVM|Java) (\d+) or (?:later|newer)")
#: A JVM flag switching assertions for every class (not ``-da:demo.Calc``).
_ASSERTION_FLAG = re.compile(r"-(?:ea|da|enableassertions|disableassertions)")


def with_assertions(jvm_args: Sequence[str], enabled: bool) -> Tuple[str, ...]:
    """A test JVM's arguments as the build gives them: ``-ea`` first when
    the build enables assertions (Surefire's and Gradle's default) and no
    argument switches them for every class. A package or class flag stays
    after it, keeping its scope, as the JVM reads these flags in order."""
    if enabled and not any(_ASSERTION_FLAG.fullmatch(arg) for arg in jvm_args):
        return ("-ea", *jvm_args)
    return tuple(jvm_args)


@dataclass(frozen=True)
class JavaProject:
    """What the build tool built, with absolute paths. ``test_classpath`` is
    the test runtime classpath in its order, its own outputs included."""

    root: str
    build_tool: str
    release: int
    main_sources: Tuple[str, ...]
    test_sources: Tuple[str, ...]
    main_classes: Tuple[str, ...]
    test_classes: Tuple[str, ...]
    test_classpath: Tuple[str, ...]
    encoding: str = "UTF-8"
    test_jvm_args: Tuple[str, ...] = ()
    test_system_properties: Mapping[str, str] = field(default_factory=dict)
    #: The tests' release where it differs from the main code's (Maven's
    #: ``testRelease``, Gradle's ``compileTestJava``).
    test_release: Optional[int] = None

    def release_of_tests(self) -> int:
        """The release test sources compile for."""
        return self.test_release or self.release

    def run_classpath(self) -> Tuple[str, ...]:
        """The test classpath without a launcher of the project's own."""
        return tuple(entry for entry in self.test_classpath
                     if not Path(entry).name.startswith(LAUNCHER_JARS))


@dataclass(frozen=True)
class Resolution:
    """The outcome of resolving a project: ``completed`` with the
    ``project``, or why not, with the build tool's bounded output."""

    outcome: Outcome
    project: Optional[JavaProject] = None
    reason: str = ""
    output: str = ""


@dataclass(frozen=True)
class Compilation:
    """Compiling test sources or a helper: ``completed`` with the class
    directory, ``collection_failed`` with diagnostics,
    ``infrastructure_failure`` when the compiler did not run or a helper did
    not compile, ``timed_out`` when the deadline ended it."""

    outcome: Outcome
    classes: Optional[str] = None
    diagnostics: str = ""


#: The build files by which the adapter knows each build system it reads.
BUILD_FILES = (("maven", ("pom.xml",)),
               ("gradle", ("build.gradle", "build.gradle.kts", "settings.gradle",
                           "settings.gradle.kts")))
#: Why a project with both build systems' files is not run, and what to do.
BOTH_BUILDS = "both Maven and Gradle build files were found"
BOTH_BUILDS_ACTION = "remove the build files of the build system the project does not use"


def build_systems(present: Sequence[str]) -> Tuple[str, ...]:
    """The build systems whose build files are among the file names
    ``present``, Maven first. The setup check and the runs both ask it."""
    names = set(present)
    return tuple(tool for tool, files in BUILD_FILES if names.intersection(files))


def build_tools(root: str, executor: Optional[Executor] = None, *,
                deadline: float | None = None) -> Tuple[str, ...]:
    """The build systems of the project at ``root``: none, one, or both."""
    files = JavaEnv(executor, deadline=deadline)
    base = files.path(root)
    return build_systems([name for _, names in BUILD_FILES for name in names
                          if files.is_file(base / name)])


def _build_directories(root: str, tool: str, files: JavaEnv) -> tuple:
    """The project's directory and, for a module of a multi-module build,
    its ancestors up to the build's top: where the build's wrapper is."""
    base = files.path(root)
    if tool == "maven":
        top = maven_build_top(base, files)
    else:
        top = next((d for d in (base, *base.parents) if any(
            files.is_file(d / name) for name in ("settings.gradle",
                                              "settings.gradle.kts"))), None)
    if top is None:
        return (base,)
    return tuple(d for d in (base, *base.parents) if d == top or top in d.parents)


def wrapper_name(tool: str, *, windows: bool) -> str:
    """The file name of ``tool``'s build wrapper in the platform's form: a
    batch file on Windows, else a shell script."""
    return {"maven": "mvnw.cmd" if windows else "mvnw",
            "gradle": "gradlew.bat" if windows else "gradlew"}[tool]


def launcher(root: str, tool: str, environment: Mapping[str, str],
             executor: Optional[Executor] = None, *,
             deadline: float | None = None) -> Optional[str]:
    """The launcher of ``tool`` for the project: its build's own wrapper,
    else the one the context's environment names (``LANGADAPTER_MAVEN``,
    ``LANGADAPTER_GRADLE``), else the one on the path (the context's
    ``PATH`` when it sets one)."""
    files = JavaEnv(executor, deadline=deadline)
    wrapper = wrapper_name(tool, windows=os.name == "nt" and not files.container)
    for directory in _build_directories(root, tool, files):
        if files.is_file(directory / wrapper):
            return str(directory / wrapper)
    named = environment.get(f"LANGADAPTER_{tool.upper()}")
    if named and files.is_file(named):
        return named
    if files.container:
        command = named or ("mvn" if tool == "maven" else "gradle")
        done = files.run(["sh", "-c", 'command -v "$1"', "sh", command], 60,
                         "container build launcher", env=environment)
        if done.returncode not in (0, 1):
            raise OSError(bounded(f"container launcher lookup failed: {done.stderr}"))
        return (done.stdout.strip() or None) if done.returncode == 0 else None
    if captured(environment):
        return _captured_launcher(tool, environment)
    return shutil.which("mvn" if tool == "maven" else "gradle",
                        path=environment.get("PATH"))


#: The build's own wrappers on a POSIX system: shell scripts.
_POSIX_WRAPPERS = frozenset({"mvnw", "gradlew"})

#: Starts script ``$0`` as the system would but without executing the file:
#: by its ``#!`` line (a final carriage return dropped), else ``sh``. It runs
#: where the build does, so a container's wrapper is read there.
_FOLLOW_SHEBANG = (
    'set -f; first=; IFS= read -r first < "$0"; '
    'first=${first%"$(printf "\\r")"}; '
    'case "$first" in "#!"*[A-Za-z0-9/]*) exec ${first#"#!"} "$0" "$@";; esac; '
    'exec sh "$0" "$@"')


def launch_argv(command: str) -> List[str]:
    """The start of the command line that runs the build launcher
    ``command``. A POSIX wrapper (``mvnw``, ``gradlew``) runs through the
    interpreter its first line names, else ``sh``, without being executed
    itself: a directory mounted without execution (``/dev/shm``, many
    ``/tmp``) or a checkout without the executable bit cannot start it, and
    a bash wrapper fails under a dash ``sh``. Windows wrappers and other
    launchers start directly."""
    name = PurePosixPath(str(command).replace("\\", "/")).name
    if name in _POSIX_WRAPPERS:
        return ["sh", "-c", _FOLLOW_SHEBANG, str(command)]
    return [str(command)]


def _captured_launcher(tool: str, environment: Mapping[str, str]) -> Optional[str]:
    command = "mvn" if tool == "maven" else "gradle"
    file_extensions = ([part for part in environment.get("PATHEXT", "").split(os.pathsep) if part]
                  if os.name == "nt" else [""])
    for directory in environment.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        for file_extension in file_extensions:
            candidate = Path(directory) / (command + file_extension)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    return None


def start_failure(tool: str, command: str, exc: BaseException) -> str:
    """Why ``command``, the launcher of ``tool``, did not start, naming a
    visible cause for an existing one: a first line ending in a carriage
    return (Windows line ends, which POSIX cannot run) or no executable
    permission."""
    reason = f"{tool} did not run: {type(exc).__name__}: {exc}"
    path = Path(command)
    if not isinstance(exc, OSError) or not path.is_file():
        return reason
    try:
        first = path.read_bytes()[:512].split(b"\n", 1)[0]
    except OSError:
        return reason
    if first.startswith(b"#!") and first.endswith(b"\r"):
        return (f"{reason}; {path.name} has Windows line ends (its first "
                "line ends in a carriage return), which this system cannot "
                "run: check the project out here or convert the script")
    if os.name != "nt" and not os.access(path, os.X_OK):
        return f"{reason}; {path.name} is not executable"
    return reason


def tool_environment(jdk: Jdk, environment: Mapping[str, str], *,
                     executor: Optional[Executor] = None) -> Mapping[str, str]:
    """The tool environment: explicit settings and ``JAVA_HOME`` with its
    ``bin`` first on PATH. Local tools also inherit the host environment
    unless the values are captured. Container tools use only the explicit
    values, with a standard Linux PATH when uncaptured values set none."""
    container = isinstance(executor, ContainerExecutor)
    env = launch_environment(environment, native=not container)
    env["JAVA_HOME"] = jdk.home
    separator = ":" if container else os.pathsep
    base = env.get("PATH", "/usr/local/bin:/usr/bin:/bin"
                   if container and not captured(environment) else "")
    bin_path = str(Path(jdk.tool("java")).parent)
    if container:
        bin_path = bin_path.replace("\\", "/")
    env["PATH"] = bin_path + (
        separator + base if base else "")
    return Environment(env, inherit=False) if captured(environment) else env


#: The time limit, in seconds, of resolving a project with its build tool.
_RESOLVE_TIMEOUT_S = 1800


def resolve(root: str, jdk: Jdk, executor: Executor, scratch: str, *,
            environment: Mapping[str, str] = {},
            deadline: float | None = None, offline: bool = False) -> Resolution:
    """The project at ``root`` resolved by its build tool on ``jdk`` (reports in
    ``scratch``). Only ``environment[BUILD_JDK]`` selects another build JDK.
    ``offline`` keeps the build tool from downloading."""
    try:
        require_mounted_output(scratch, executor)
        budget = {} if deadline is None else {"deadline": deadline}
        tools = build_tools(root, executor, **budget)
        tool = tools[0] if len(tools) == 1 else None
        command = launcher(root, tool, environment, executor, **budget) if tool else None
    except Exception as exc:  # discovery did not complete
        reraise_unresolved(exc)
        outcome = (Outcome.TIMED_OUT if deadline is not None and isinstance(exc, subprocess.TimeoutExpired)
                   else Outcome.INFRASTRUCTURE_FAILURE)
        return Resolution(outcome, reason=(
            f"project discovery did not complete: {type(exc).__name__}: {exc}"))
    if len(tools) > 1:
        return Resolution(Outcome.UNSUPPORTED, reason=f"{BOTH_BUILDS}: {BOTH_BUILDS_ACTION}")
    if tool is None:
        return Resolution(Outcome.UNSUPPORTED, reason=(
            f"no Maven or Gradle build file at {root}; other Java builds are "
            "not supported"))
    if command is None:
        return Resolution(Outcome.MISSING_SETUP, reason=(
            f"no {tool} launcher: the project has no wrapper, "
            f"LANGADAPTER_{tool.upper()} names no file that exists, and none is on "
            f"the path"))
    named = environment.get(BUILD_JDK)
    build = (jdk_module.read_in_container(named, executor, environment=environment, **budget)
             if named and isinstance(executor, ContainerExecutor)
             else jdk_module.read(named) if named else jdk)
    if build is None:
        return Resolution(Outcome.MISSING_SETUP, reason=(
            f"{BUILD_JDK} names no JDK: {named}"))
    env = tool_environment(build, environment, executor=executor)
    try:
        resolution = resolve_with(tool, root, command, env, executor, scratch,
                                  _RESOLVE_TIMEOUT_S, project_jdk=jdk, offline=offline, **budget)
    except Exception as exc:  # resolution did not produce a model
        reraise_unresolved(exc)
        outcome = (Outcome.TIMED_OUT if deadline is not None and isinstance(exc, subprocess.TimeoutExpired)
                   else Outcome.INFRASTRUCTURE_FAILURE)
        return Resolution(outcome, reason=(
            f"{tool} project resolution failed: {type(exc).__name__}: {exc}"))
    newer = _NEEDS_NEWER_JVM.search(resolution.output or "")
    if resolution.outcome is Outcome.INFRASTRUCTURE_FAILURE and newer:
        return Resolution(Outcome.MISSING_SETUP, output=resolution.output,
                          reason=(
            f"the {tool} launcher needs Java {newer.group(1)} or newer to "
            f"run, not {build.feature}; name such a JDK in the build_java_home "
            f"setting, {BUILD_JDK} (the "
            f"project still compiles and runs on the context's JDK)"))
    if resolution.outcome is Outcome.COMPLETED and resolution.project is not None:
        refusal = JvmProfile.framework_refusal(resolution.project.run_classpath())
        if refusal:
            return Resolution(Outcome.UNSUPPORTED, output=resolution.output, reason=refusal)
    return resolution


def resolve_with(tool: str, root: str, command: str, env: Mapping[str, str],
                 executor: Executor, scratch: str, timeout: float, *,
                 project_jdk: Optional[Jdk] = None,
                 deadline: float | None = None, offline: bool = False) -> Resolution:
    """The project at ``root`` resolved by ``tool`` (``maven``, ``gradle``)
    with its launcher ``command``: one run writes the tool's reports into
    ``scratch``, and its reader takes the model from them. ``project_jdk``
    is the context's JDK, which Gradle's toolchain lookup also finds.
    ``offline`` passes the tool's option that forbids downloads."""
    steps = _TOOLS[tool]
    files = JavaEnv(executor, deadline=deadline)
    out = Path(scratch)
    try:
        require_mounted_output(scratch, executor)
        out.mkdir(parents=True, exist_ok=True)
        steps.prepare(out)
    except OSError as exc:
        reraise_unresolved(exc)
        return Resolution(Outcome.INFRASTRUCTURE_FAILURE,
                          reason=f"{steps.name} report directory could not be created: {exc}")
    planned = steps.command_line(root, out, files, project_jdk)
    if isinstance(planned, str):
        return Resolution(Outcome.UNSUPPORTED, reason=planned)
    arguments, cwd = planned
    if offline:
        arguments = [steps.offline, *arguments]
    try:
        done = files.run([*launch_argv(command), *arguments], timeout,
                         f"{steps.name} resolution", cwd=cwd, env=env)
    except Exception as exc:  # the build tool did not run
        reraise_unresolved(exc)
        outcome = (Outcome.TIMED_OUT if deadline is not None and isinstance(exc, subprocess.TimeoutExpired)
                   else Outcome.INFRASTRUCTURE_FAILURE)
        return Resolution(outcome, reason=start_failure(steps.name, command, exc))
    output = bounded(done.stdout + done.stderr)
    if done.returncode != 0:
        return Resolution(Outcome.INFRASTRUCTURE_FAILURE, output=output,
                          reason=f"{steps.name} exited {done.returncode}")
    try:
        reports = steps.read(root, out, files)
    except steps.read_errors as exc:
        reraise_unresolved(exc)
        return Resolution(Outcome.INFRASTRUCTURE_FAILURE, output=output,
                          reason=f"{steps.name}'s {steps.reports} could not be read: {exc}")
    project = steps.model(reports, root, files)
    if isinstance(project, str):
        return Resolution(Outcome.UNSUPPORTED, output=output, reason=project)
    remaining(deadline, timeout, f"{steps.name} model read")
    return Resolution(Outcome.COMPLETED, project=project, output=output)


def compile_tests(project: JavaProject, jdk: Jdk, sources: Sequence[str],
                  out_dir: str, executor: Executor, *,
                  environment: Mapping[str, str] = {},
                  timeout: float = 600, deadline: float | None = None,
                  classpath: Sequence[str] | None = None,
                  options: Sequence[str] = ()) -> Compilation:
    """Compile test ``sources`` into ``out_dir`` with ``jdk``'s ``javac``
    for ``release_of_tests``, against the run classpath or ``classpath``,
    alike for every build tool. The test source roots are the source path,
    so a helper class a source uses compiles with it and an unused test file
    does not. ``options`` are further javac options, before the sources."""
    files = JavaEnv(executor, deadline=deadline)
    try:
        require_mounted_output(out_dir, executor)
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        argv = [jdk.tool("javac"), "--release", str(project.release_of_tests()),
                "-encoding", project.encoding, "-g", "-nowarn",
                "-d", out_dir, "-cp", files.path_list(
                    project.run_classpath() if classpath is None else classpath),
                "-sourcepath", files.path_list(project.test_sources),
                "-implicit:class",
                *options, *sources]
        done = files.run(argv, timeout, "suite compilation", cwd=project.root,
                         env=tool_environment(jdk, environment, executor=executor))
    except Exception as exc:  # the compiler did not run
        reraise_unresolved(exc)
        outcome = (Outcome.TIMED_OUT if deadline is not None and isinstance(exc, subprocess.TimeoutExpired)
                   else Outcome.INFRASTRUCTURE_FAILURE)
        return Compilation(outcome,
                           diagnostics=f"{type(exc).__name__}: {exc}")
    if done.returncode != 0:
        return Compilation(Outcome.COLLECTION_FAILED,
                           diagnostics=bounded(done.stdout + done.stderr))
    return Compilation(Outcome.COMPLETED, classes=out_dir)


#: The time limit, in seconds, of compiling a suite.
_COMPILE_TIMEOUT_S = 600


def compile_suite(project: JavaProject, jdk: Jdk, suite: Suite,
                  out_dir: str, executor: Executor, *,
                  environment: Mapping[str, str] = {},
                  deadline: float | None = None) -> Compilation:
    """Compile the declared tests and Java helpers, and stage the declared
    resources on the classpath relative to the suite root (nothing else)."""
    suite.validate()
    files = [path.resolve() for path in suite.files()]
    budget = {} if deadline is None else {"deadline": deadline}
    compiled = compile_tests(project, jdk,
                             [str(path) for path in files if path.suffix == ".java"],
                             out_dir, executor, environment=environment,
                             timeout=_COMPILE_TIMEOUT_S, **budget)
    if compiled.outcome is not Outcome.COMPLETED:
        return compiled
    root = Path(suite.root).resolve()
    try:
        for path in files:
            remaining(deadline, _COMPILE_TIMEOUT_S, "suite resource staging")
            if path.suffix == ".java":
                continue
            destination = Path(out_dir) / path.relative_to(root)
            if destination.exists():
                return Compilation(Outcome.COLLECTION_FAILED, diagnostics=(
                    f"suite resource would replace compiled output: {path.relative_to(root)}"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        remaining(deadline, _COMPILE_TIMEOUT_S, "suite resource staging")
    except (OSError, subprocess.TimeoutExpired) as exc:
        reraise_unresolved(exc)
        outcome = (Outcome.TIMED_OUT if deadline is not None and isinstance(exc, subprocess.TimeoutExpired)
                   else Outcome.INFRASTRUCTURE_FAILURE)
        return Compilation(outcome,
                           diagnostics=f"could not stage suite resources: {exc}")
    return compiled


# Maven resolution: one ``mvn`` invocation compiles the main code, copies
# the test resources and writes the test classpath and effective POM, which
# give the model. Tests compile only as a suite's own files when it runs, so
# a broken test file the suite does not use cannot stop the resolution.
#
# A module of a multi-module build is resolved from the build's top
# (``-pl <module> -am``), with the modules it depends on compiled and on its
# classpath. A build top that only aggregates modules has no code to measure
# and is refused, with its modules named.

#: Pinned so a resolution does not depend on the newest plugin release.
DEPENDENCY_PLUGIN = "org.apache.maven.plugins:maven-dependency-plugin:3.11.0"
HELP_PLUGIN = "org.apache.maven.plugins:maven-help-plugin:3.5.2"
_NS = {"m": "http://maven.apache.org/POM/4.0.0"}
#: The start of every POM model version's namespace: 4.0.0's, and Maven 4's
#: 4.1.0.
_POM_NAMESPACES = "{http://maven.apache.org/POM/"
#: A late-resolved (``@{argLine}``) or unset argLine token: no JVM argument.
_PLACEHOLDER = re.compile(r"[@$]\{[^}]*\}")
#: The reports one Maven run writes into the resolution's scratch directory.
_MAVEN_POM = "maven-effective-pom.xml"
_MAVEN_CLASSPATH = "maven-test-classpath.txt"


#: Maven's options that make a build offline.
_MAVEN_OFFLINE = frozenset(("-o", "--offline"))


def builds_offline(root: str, tool: str, environment: Mapping[str, str],
                   executor: Optional[Executor] = None) -> bool:
    """Whether the project's runs of ``tool`` build offline: Maven's ``-o``
    or ``--offline`` in ``MAVEN_ARGS``, read as a launched command reads it,
    or in the build's ``.mvn/maven.config``. LangAdapter's Gradle runs read
    no such setting."""
    if tool != "maven":
        return False
    files = JavaEnv(executor)
    base = files.path(root)
    config = (maven_build_top(base, files) or base) / ".mvn" / "maven.config"
    words = launch_environment(environment, native=not files.container).get("MAVEN_ARGS", "").split()
    if files.is_file(config):
        words += files.read(config).split()
    return bool(_MAVEN_OFFLINE & set(words))


def _in_pom_namespace(node: ET.Element) -> ET.Element:
    """``node`` with each element that has no namespace or the namespace of
    any POM model version put in 4.0.0's, so the ``m:`` paths find it. Maven
    reads a POM without its namespace, and model 4.1.0, which Maven 4
    reads, has a namespace of its own."""
    for element in node.iter():
        if not element.tag.startswith("{") or element.tag.startswith(_POM_NAMESPACES):
            element.tag = f"{{{_NS['m']}}}{element.tag.rpartition('}')[2]}"
    return node


def _own_pom(pom: Path, files: JavaEnv) -> ET.Element:
    """A project's own POM (``_in_pom_namespace``)."""
    return _in_pom_namespace(ET.fromstring(files.read(pom)))


def maven_modules(pom: Path, files: JavaEnv) -> list:
    """The module directories ``pom`` lists, resolved."""
    try:
        node = _own_pom(pom, files)
    except (OSError, ET.ParseError) as exc:
        reraise_unresolved(exc)
        return []
    return [files.path(pom.parent / entry.text.strip())
            for entry in node.findall("m:modules/m:module", _NS)
            if entry.text and entry.text.strip()]


def _module_tree(directory: Path, seen: Set[Path], files: JavaEnv) -> Set[Path]:
    """Every module directory below the build at ``directory``."""
    found: Set[Path] = set()
    for module in maven_modules(directory / "pom.xml", files):
        if module in seen:
            continue
        seen.add(module)
        found.add(module)
        found |= _module_tree(module, seen, files)
    return found


def maven_build_top(root: Path, files: JavaEnv) -> Optional[Path]:
    """The top of the multi-module build listing ``root`` as a module, directly
    or through intermediate modules, or None."""
    root = files.path(root)
    top = None
    for ancestor in root.parents:
        if files.is_file(ancestor / "pom.xml") and root in _module_tree(
                ancestor, set(), files):
            top = ancestor
    return top


def _packaging(pom: Path, files: JavaEnv) -> str:
    try:
        node = _own_pom(pom, files)
    except (OSError, ET.ParseError) as exc:
        reraise_unresolved(exc)
        return "jar"
    return _text(node, "m:packaging") or "jar"


def _maven_command_line(root: str, out: Path, files: JavaEnv,
                        project_jdk: Optional[Jdk]) -> Union[str, Tuple[List[str], str]]:
    """Maven's arguments and directory, or why the project is refused: a
    build top that only aggregates modules."""
    base = files.path(root)
    own = maven_modules(base / "pom.xml", files)
    if own and _packaging(base / "pom.xml", files) == "pom":
        names = ", ".join(sorted(files.relative(m, base) for m in own))
        return (f"the Maven build at {root} only aggregates modules ({names}); "
                "the project is the module that holds the code")
    top = maven_build_top(base, files)
    # The module builds last, so its classpath is the one the plugin leaves.
    if top is not None:
        selection = ["-f", str(top / "pom.xml"),
                     "-pl", files.relative(base, top), "-am"]
    else:
        selection = ["-f", str(base / "pom.xml")] + (["-N"] if own else [])
    # The effective POM comes before the build, whose plugins add properties
    # (the Apache Commons parent one that is no XML name) the report would carry.
    return ["-B", "-q", *selection,
            f"{HELP_PLUGIN}:effective-pom", f"-Doutput={out / _MAVEN_POM}",
            "process-test-resources",
            f"{DEPENDENCY_PLUGIN}:build-classpath",
            "-Dmdep.includeScope=test",
            f"-Dmdep.outputFile={out / _MAVEN_CLASSPATH}"], str(top or base)


def _maven_reports(root: str, out: Path, files: JavaEnv) -> Tuple[ET.Element, List[str]]:
    """The module's effective POM and the jars of its test classpath."""
    pom = _module_pom(_effective_pom(out / _MAVEN_POM), files.path(root) / "pom.xml", files)
    jars = [entry for entry in (out / _MAVEN_CLASSPATH).read_text(
        encoding="utf-8").strip().split(":" if files.container else os.pathsep) if entry]
    return pom, jars


def _maven_model(reports: Tuple[ET.Element, List[str]], root: str,
                 files: JavaEnv) -> JavaProject:
    """The model from the module's effective POM and test classpath."""
    pom, jars = reports
    base = files.path(root)
    build = pom.find("m:build", _NS)
    properties = _properties(pom)
    compiler = _plugin(pom, "maven-compiler-plugin")
    surefire = _plugin(pom, "maven-surefire-plugin")
    release = _maven_release(compiler, properties)
    test_release = _maven_release(compiler, properties, prefix="test")
    main_classes = _path(root, _text(build, "m:outputDirectory"), "target/classes", files)
    test_classes = _path(root, _text(build, "m:testOutputDirectory"),
                         "target/test-classes", files)
    encoding = (_text(compiler, "m:configuration/m:encoding")
                or properties.get("project.build.sourceEncoding") or "UTF-8")
    return JavaProject(
        root=str(base),
        build_tool="maven",
        release=release,
        main_sources=(_path(root, _text(build, "m:sourceDirectory"),
                            "src/main/java", files),),
        test_sources=(_path(root, _text(build, "m:testSourceDirectory"),
                            "src/test/java", files),),
        main_classes=(main_classes,),
        test_classes=(test_classes,),
        test_classpath=(test_classes, main_classes, *jars),
        encoding=encoding,
        test_jvm_args=_maven_jvm_args(surefire, properties),
        test_system_properties=_system_properties(surefire),
        test_release=test_release if test_release != release else None,
    )


def _effective_pom(path: Path) -> ET.Element:
    """The help plugin's report, read without its XML declarations: in some
    builds it writes a second declaration below its header comments (seen
    with the Apache Commons parent), which is not well-formed. Its elements
    are in 4.0.0's namespace (``_in_pom_namespace``)."""
    text = path.read_text(encoding="utf-8")
    return _in_pom_namespace(ET.fromstring(re.sub(r"<\?xml[^>]*\?>", "", text)))


def _module_pom(effective: ET.Element, pom: Path, files: JavaEnv) -> ET.Element:
    """The module's effective POM: the report itself for one project, else
    the project of the report with the module's artifact id."""
    if effective.tag.endswith("}project") or effective.tag == "project":
        return effective
    artifact = _text(_own_pom(pom, files), "m:artifactId")
    for project in effective.iter(f"{{{_NS['m']}}}project"):
        if _text(project, "m:artifactId") == artifact:
            return project
    raise LookupError(f"no project {artifact} in the effective POM")


def _text(node: Optional[ET.Element], path: str) -> Optional[str]:
    if node is None:
        return None
    found = node.find(path, _NS)
    return found.text.strip() if found is not None and found.text else None


def _path(root: str, value: Optional[str], default: str, files: JavaEnv) -> str:
    return str(files.path(files.path(root) / (value or default)))


def _maven_jvm_args(surefire: Optional[ET.Element], properties: Dict[str, str]) -> Tuple[str, ...]:
    """The arguments Surefire gives a test JVM: ``-ea`` unless its
    enableAssertions (the configuration, else the property, default true)
    is false, then its argLine without the placeholders it resolves late."""
    assertions = (_text(surefire, "m:configuration/m:enableAssertions")
                  or properties.get("enableAssertions") or "true")
    tokens = [token for token in (_text(surefire, "m:configuration/m:argLine") or "").split()
              if not _PLACEHOLDER.fullmatch(token)]
    return with_assertions(tokens, assertions.strip().lower() != "false")


def _properties(pom: ET.Element) -> Dict[str, str]:
    node = pom.find("m:properties", _NS)
    if node is None:
        return {}
    return {child.tag.split("}")[-1]: (child.text or "").strip()
            for child in node}


def _plugin(pom: ET.Element, artifact: str) -> Optional[ET.Element]:
    for plugin in pom.findall("m:build/m:plugins/m:plugin", _NS):
        if _text(plugin, "m:artifactId") == artifact:
            return plugin
    return None


def _maven_release(compiler: Optional[ET.Element], properties: Mapping[str, str],
             prefix: str = "") -> int:
    """The release the project compiles for: the compiler's ``release``,
    else ``target``, else ``source``, from the plugin's configuration or the
    properties, else Java 8. With ``prefix`` ``test``, the tests' release
    (``testRelease``, ``testTarget``, ``testSource``), else the main code's."""
    for name in ("release", "target", "source"):
        if prefix:
            name = prefix + name[0].upper() + name[1:]
        value = (_text(compiler, f"m:configuration/m:{name}")
                 or properties.get(f"maven.compiler.{name}"))
        if value:
            return feature_of(value)
    return _maven_release(compiler, properties) if prefix else 8


def _system_properties(surefire: Optional[ET.Element]) -> Dict[str, str]:
    if surefire is None:
        return {}
    node = surefire.find("m:configuration/m:systemPropertyVariables", _NS)
    if node is None:
        return {}
    return {re.sub(r"^\{.*\}", "", child.tag): (child.text or "").strip()
            for child in node}


# Gradle resolution: an init script adds a task that compiles the main
# code, copies the test resources and reports the source sets, their outputs,
# the test runtime classpath, the release and the test task's JVM settings,
# leaving the build files unchanged (tests compile as a suite's files).
#
# Every Java-plugin project of the build gets the task, and Gradle finds the
# build from the project's directory, so a subproject resolves with its
# dependencies built (the task depends on the test runtime configuration,
# not the test classes). A build root without the Java plugin is refused,
# naming its Java projects.

INIT_SCRIPT = """\
// Added by LangAdapter for one invocation: reports each Java
// project as Gradle resolved it. It changes nothing in the build.
allprojects {
    pluginManager.withPlugin('java') {
        tasks.register('langadapterResolve') {
        dependsOn 'classes', 'processTestResources'
        dependsOn project.configurations.testRuntimeClasspath
        def out = providers.gradleProperty('langadapterOut')
        doLast {
            def p = project
            def main = p.sourceSets.main
            def test = p.sourceSets.test
            def compile = p.tasks.named('compileJava').get()
            def testTask = p.tasks.named('test').get()
            def toolchain = null
            try {
                toolchain = p.java.toolchain.languageVersion.getOrNull()?.asInt()
            } catch (ignored) {
            }
            def data = [
                mainSources: main.java.srcDirs.collect { it.absolutePath },
                testSources: test.java.srcDirs.collect { it.absolutePath },
                mainClasses: main.output.classesDirs.files.collect { it.absolutePath },
                testClasses: test.output.classesDirs.files.collect { it.absolutePath },
                testClasspath: test.runtimeClasspath.files.collect { it.absolutePath },
                release: compile.options.release.getOrNull(),
                testRelease: p.tasks.named('compileTestJava').get().options.release.getOrNull(),
                toolchain: toolchain,
                targetCompatibility: p.java.targetCompatibility.majorVersion,
                encoding: compile.options.encoding,
                jvmArgs: testTask.jvmArgs ?: [],
                enableAssertions: testTask.enableAssertions,
                systemProperties: testTask.systemProperties.collectEntries {
                    k, v -> [(k): String.valueOf(v)]
                },
                projectDir: p.projectDir.absolutePath,
                path: p.path,
            ]
            def name = p.path == ':' ? 'root' : p.path.substring(1).replace(':', '_')
            new File(out.get(), name + '.json').text = groovy.json.JsonOutput.toJson(data)
        }
        }
    }
}
"""


#: The init script and the directory its task reports into, in the scratch directory.
_GRADLE_SCRIPT = "langadapter-resolve.gradle"
_GRADLE_REPORTS = "gradle-projects"


def _gradle_prepare(out: Path) -> None:
    (out / _GRADLE_SCRIPT).write_text(INIT_SCRIPT, encoding="utf-8")
    (out / _GRADLE_REPORTS).mkdir(exist_ok=True)


def _gradle_command_line(root: str, out: Path, files: JavaEnv,
                         project_jdk: Optional[Jdk]) -> Tuple[List[str], str]:
    """Gradle's arguments, run in the project's directory. Gradle's
    toolchain lookup also finds ``project_jdk``, so a build asking for the
    project's Java version gets the context's JDK when Gradle runs on
    another."""
    arguments = ["--no-daemon", "-q", "--no-configuration-cache",
                 "--init-script", str(out / _GRADLE_SCRIPT),
                 f"-PlangadapterOut={out / _GRADLE_REPORTS}"]
    if project_jdk is not None:
        arguments.append(f"-Dorg.gradle.java.installations.paths={project_jdk.home}")
    arguments.append("langadapterResolve")
    return arguments, root


def _gradle_reports(root: str, out: Path, files: JavaEnv) -> List[dict]:
    """Each Java project's report."""
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((out / _GRADLE_REPORTS).glob("*.json"))]


def _gradle_model(projects: List[dict], root: str,
                  files: JavaEnv) -> Union[JavaProject, str]:
    """The model from the report of the project at ``root``, or why it is
    refused: a build root without the Java plugin."""
    base = files.path(root)
    data = next((p for p in projects
                 if files.path(p["projectDir"]) == base), None)
    if data is None:
        names = ", ".join(sorted(
            files.relative(p["projectDir"], base)
            for p in projects))
        return (f"the Gradle build at {root} applies no Java plugin"
                + (f"; its Java projects ({names}) hold the code: the project is "
                   "one of them" if names else ""))
    release = _gradle_release(data)
    test_release = (int(data["testRelease"]) if data.get("testRelease")
                    else None)
    return JavaProject(
        root=str(base),
        build_tool="gradle",
        release=release,
        main_sources=_existing(data["mainSources"], files),
        test_sources=_existing(data["testSources"], files),
        main_classes=tuple(data["mainClasses"]),
        test_classes=tuple(data["testClasses"]),
        test_classpath=tuple(data["testClasspath"]),
        encoding=data.get("encoding") or "UTF-8",
        test_jvm_args=_gradle_jvm_args(data),
        test_system_properties=dict(data.get("systemProperties") or {}),
        test_release=test_release if test_release not in (None, release) else None,
    )


def _gradle_jvm_args(data: dict) -> Tuple[str, ...]:
    """The arguments Gradle's test task gives a test JVM: ``-ea`` (not shown
    in jvmArgs) unless enableAssertions is false, then its jvmArgs."""
    assertions = data.get("enableAssertions")
    return with_assertions(list(data.get("jvmArgs") or ()), assertions is None or bool(assertions))


def _gradle_release(data: dict) -> int:
    """The release the build compiles for: the compile task's ``release``,
    else its toolchain's version, else the target compatibility."""
    for value in (data.get("release"), data.get("toolchain"),
                  data.get("targetCompatibility")):
        if value:
            return feature_of(str(value))
    return 8


def _existing(paths: List[str], files: JavaEnv) -> tuple:
    """The source roots that exist, else the first reported one (Gradle
    reports its conventional roots even when absent)."""
    found: List[Optional[str]] = [p for p in paths if files.is_dir(p)]
    return tuple(found) or tuple(paths[:1])


@dataclass(frozen=True)
class _BuildTool:
    """One build tool in ``resolve_with``: its name in reasons, the noun of
    its reports, the errors reading them raises, and its steps: prepare the
    scratch directory, give the arguments and directory (or a refusal), read
    the reports, make the model (or a refusal). ``offline`` is its option
    that forbids downloads."""

    name: str
    reports: str
    read_errors: Tuple[type, ...]
    prepare: Callable[[Path], None]
    command_line: Callable[..., Union[str, Tuple[List[str], str]]]
    read: Callable[..., Any]
    model: Callable[..., Union[JavaProject, str]]
    offline: str


_TOOLS = {
    "maven": _BuildTool("Maven", "reports", (OSError, ET.ParseError, LookupError),
                        lambda out: None, _maven_command_line, _maven_reports,
                        _maven_model, "-o"),
    "gradle": _BuildTool("Gradle", "report", (OSError, ValueError), _gradle_prepare,
                         _gradle_command_line, _gradle_reports, _gradle_model, "--offline"),
}
