"""The Java toolchain's jars: fetched once from Maven Central into a cache,
checked against the repository's SHA-1, and used offline afterwards.

The cache is the directory the context's environment names in
``LANGADAPTER_JAVA_TOOLS``, else ``~/.cache/langadapter/java``. Nothing is
written into a project or its build files. ``LANGADAPTER_JAVA_TOOLS_OFFLINE``
forbids downloads: runs then use the cache only.
"""
from __future__ import annotations

import hashlib
import http.client
import io
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple, List
from langadapter._environment import captured, launch_environment
from langadapter.execution import ContainerExecutor, Executor
from langadapter.java._env import JavaEnv, require_mounted_output, remaining
from langadapter.java._profile import JvmProfile, _jar_version as _version
from langadapter._execution_scope import reraise_unresolved
import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from langadapter.suites import TestId


MAVEN_CENTRAL = "https://repo1.maven.org/maven2"


class ToolUnavailable(RuntimeError):
    """A tool jar is neither cached nor obtainable."""


def host_environment(context) -> Mapping[str, str]:
    """The values that locate the host tool cache for ``context``. Tools are
    cached on the host and staged for container launches, so a container context
    uses its captured host control values, never the container's own (a container
    HOME is no host path), else this process's environment. A local context
    uses its own. Setup inspection and runs share this choice."""
    from langadapter.context import ContainerExecution

    execution = context.execution
    if isinstance(execution, ContainerExecution):
        return execution.control_environment if execution.control_environment is not None else {}
    return context.environment


#: 1 (true, yes, on) in the run's values: an uncached jar is unavailable, not fetched.
OFFLINE = "LANGADAPTER_JAVA_TOOLS_OFFLINE"


def offline(environment: Mapping[str, str]) -> bool:
    """Whether the run's values, read as a launched command reads them,
    forbid downloads."""
    value = launch_environment(environment).get(OFFLINE)
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def cache_dir(environment: Mapping[str, str]) -> Path:
    if captured(environment):
        named = environment.get("LANGADAPTER_JAVA_TOOLS")
        if named:
            return Path(named)
        # The documented default under the captured home, never a later ambient one.
        home = environment.get("USERPROFILE" if os.name == "nt" else "HOME")
        if not home:
            raise ToolUnavailable("with a captured environment, the Java tool cache needs "
                                  "LANGADAPTER_JAVA_TOOLS or a home directory (HOME, or "
                                  "USERPROFILE on Windows) in that environment")
        return Path(home) / ".cache" / "langadapter" / "java"
    named = environment.get("LANGADAPTER_JAVA_TOOLS") or os.environ.get(
        "LANGADAPTER_JAVA_TOOLS")
    return Path(named) if named else Path.home() / ".cache" / "langadapter" / "java"


def stage_files(paths: Sequence[str], directory: Path, *,
                executor: Optional[Executor] = None,
                deadline: float | None = None) -> Tuple[str, ...]:
    """The tool paths a launch reads: the cache paths locally. For a container,
    mounted files as they are and the others copied, without aliases, into
    distinct scratch directories, keeping jar names and order. A failed copy
    propagates as a preparation error."""
    remaining(deadline, None, "Java tool staging")
    if not isinstance(executor, ContainerExecutor):
        return tuple(str(path) for path in paths)
    require_mounted_output(str(directory), executor)
    sources = tuple(Path(path).resolve() for path in paths)
    for source in sources:
        remaining(deadline, None, "Java tool staging")
        if not source.is_file():
            raise OSError(f"Java tool cache file is unavailable: {source}")
    mounted = Path(executor._host_work_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    copied: dict[Path, Path] = {}
    work: Optional[Path] = None
    staged = []
    for source in sources:
        remaining(deadline, None, "Java tool staging")
        if source.is_relative_to(mounted):
            target = source
        elif source in copied:
            target = copied[source]
        else:
            if work is None:
                work = Path(tempfile.mkdtemp(prefix="java-tools-", dir=directory))
            destination = work / str(len(copied))
            destination.mkdir()
            target = destination / source.name
            shutil.copyfile(source, target)
            copied[source] = target
        staged.append(executor._to_container(str(target)))
    remaining(deadline, None, "Java tool staging")
    return tuple(staged)


def compiled_helper(javac: str, name: str, source: str, environment: Mapping[str, str],
                    env: Dict[str, str], *, prefix: str, what: str,
                    classpath: Callable[[], Sequence[str]] = tuple,
                    service: Optional[str] = None, executor: Optional[Executor] = None,
                    scratch: Optional[Path] = None) -> Tuple[Path, Tuple[str, ...]]:
    """The directory of helper class ``name`` compiled from ``source``, and
    the ``classpath()`` jars it compiled against, as the run sees them. A
    local run compiles it once into the tool cache under ``prefix`` and the
    source's digest, a run in a container into a new directory of its mounted
    ``scratch``, checked before any tool lookup. ``service`` registers it as
    that interface's provider. A helper that does not compile raises
    ``ToolUnavailable`` naming ``what``, whose leading "the" reads "the
    container's" in a container."""
    container = isinstance(executor, ContainerExecutor)
    label = what.replace("the ", "the container's ", 1) if container else what
    if container:
        if scratch is None:
            raise OSError(f"{label} needs mounted host scratch")
        require_mounted_output(str(scratch), executor)
    jars = tuple(str(path) for path in classpath())
    if container:
        if jars:
            jars = stage_files(jars, scratch / "tools", executor=executor)
        scratch.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=scratch))
        out = work / "classes"
    else:
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
        target = cache_dir(environment) / "helpers" / f"{prefix}-{digest}"
        if (target / f"{name}.class").is_file():
            return target, jars
        target.parent.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=f"{prefix}-", dir=target.parent))
        out = work / "out"
    path = work / f"{name}.java"
    path.write_text(source, encoding="utf-8")
    command = [javac, "--release", str(JvmProfile.helper_release), "-nowarn", "-d", str(out),
               *(("-cp", (":" if container else os.pathsep).join(jars)) if jars else ()), str(path)]
    done = JavaEnv(executor).launch(command, 300, env=env)
    if done.returncode != 0:
        if not container:
            shutil.rmtree(work, ignore_errors=True)
        raise ToolUnavailable(f"{label} does not compile: " + (done.stdout + done.stderr)[-500:])
    if service is not None:
        services = out / "META-INF" / "services"
        services.mkdir(parents=True)
        (services / service).write_text(f"{name}\n", encoding="utf-8")
    if container:
        return out, jars
    try:
        os.replace(out, target)
    except OSError:
        if not (target / f"{name}.class").is_file():
            raise
    shutil.rmtree(work, ignore_errors=True)
    return target, jars


def _read(response, deadline, timeout):
    if deadline is None:
        return response.read()
    read = getattr(response, "read1", response.read)
    parts = []
    while True:
        left = remaining(deadline, timeout, "Java tool download")
        # HTTPResponse keeps the first socket timeout: give each read the time left.
        stream = getattr(response, "fp", None)
        sock = getattr(getattr(stream, "raw", None), "_sock", None)
        if sock is not None:
            sock.settimeout(left)
        elif stream is not None or not isinstance(response, (io.BytesIO, http.client.HTTPResponse)):
            raise ToolUnavailable("The tool download response cannot enforce its read deadline")
        part = read(64 * 1024)
        remaining(deadline, timeout, "Java tool download")
        if not part:
            return b"".join(parts)
        parts.append(part)


def _relative(coordinates: str, extension: str = "jar") -> str:
    group, artifact, version, *classifier = coordinates.split(":")
    suffix = f"-{classifier[0]}" if classifier else ""
    return (f"{group.replace('.', '/')}/{artifact}/{version}/"
            f"{artifact}-{version}{suffix}.{extension}")


def uncached(coordinates: Sequence[str], environment: Mapping[str, str]) -> Tuple[str, ...]:
    """The coordinates whose jars are not in the cache. Nothing is fetched."""
    directory = cache_dir(environment)
    return tuple(item for item in coordinates if not (directory / _relative(item)).is_file())


#: Without a deadline, each download connection's and read's timeout, in seconds.
_DOWNLOAD_TIMEOUT_S = 120

#: The SHA-256 of each fixed tool jar the adapter runs: JaCoCo, PIT, the
#: helpers' JUnit API and the JUnit 4 a run substitutes. A fetched jar and a
#: cached one must match it. A jar at a project's own JUnit version has no
#: pin and is checked against Maven Central's published SHA-1 when fetched.
TOOL_SHA256 = {
    "org.jacoco:org.jacoco.agent:0.8.15:runtime": "fb5b0036a0899ea97edfa0fc2c7985b55f3f7c5695028163e5e60d4f3cf6075d",
    "org.jacoco:org.jacoco.core:0.8.15": "ade1e0fdb4cd80d8537b57c6edb70a1360c47fdc518c20c65314517479bc4d0d",
    "org.ow2.asm:asm:9.10.1": "ed825d10ab1399c8c0cb669e688cf0c8c82629b4c8399b58352b68e92ca10fcb",
    "org.ow2.asm:asm-commons:9.10.1": "6d0abefb7cbf972ea16edb37ec14835372505063a45f976ab7ea889ed9497895",
    "org.ow2.asm:asm-tree:9.10.1": "3dfb0d5b6a106cd40b5b250e39935fbf2f927f4477546a5369a3ac609cf0506b",
    "org.ow2.asm:asm-util:9.10.1": "1bb99d091fba2597dc6d51193e9bbcf0d8447e7ed96bd8f0198b18152f09655c",
    "org.ow2.asm:asm-analysis:9.10.1": "dede75a21306b65974ecd8f87114ff6970f09fb794157a4ca09ab25c888c2bfc",
    "org.pitest:pitest-command-line:1.30.0": "a7ee242c8da89be731cb2c32993500aa552049a68735af6928862ed258fed009",
    "org.pitest:pitest-entry:1.30.0": "1902c50bcdd6db79176ee59ad28c4d4c1a13805bdeaaba010cff97d5f0ea7b19",
    "org.pitest:pitest:1.30.0": "73f561066a9b681611356db417cc8e7bdb1deec8e564239ee3ce8720bab64cec",
    "org.pitest:pitest-html-report:1.30.0": "b45949e78f1f691b0d3cee4e29462a187389a0a9e37ad64aa29ada5dafe13f7e",
    "org.pitest:pitest-junit5-plugin:1.2.3": "d3e2fcc8db5fd52c0c4ac2fe0d937e64da3df39b657093a1581b5fd463f9be64",
    "net.sf.jopt-simple:jopt-simple:4.9": "26c5856e954b5f864db76f13b86919b59c6eecf9fd930b96baa8884626baf2f5",
    "org.apache.commons:commons-text:1.14.0": "121fce2282910c8f0c3ba793a5436b31beb710423cbe2d574a3fb7a73c508e92",
    "org.apache.commons:commons-lang3:3.18.0": "4eeeae8d20c078abb64b015ec158add383ac581571cddc45c68f0c9ae0230720",
    "org.junit.platform:junit-platform-launcher:1.14.4": "768d62f1b2a523713b702db53609c230af62bbd645fc2c07a7d794df4da32228",
    "org.junit.platform:junit-platform-engine:1.14.4": "3c7f3f84a6747aef0db6bd5fdd2a6c8fe37132e653c939bd67387377af66d91c",
    "org.junit.platform:junit-platform-commons:1.14.4": "55c8a0c069ac1bc4e1f8bbb26b5eae95cbd10e4ff1b23248441ab61a607381e1",
    "org.opentest4j:opentest4j:1.3.0": "48e2df636cab6563ced64dcdff8abb2355627cb236ef0bf37598682ddf742f1b",
    "org.apiguardian:apiguardian-api:1.1.2": "b509448ac506d607319f182537f0b35d71007582ec741832a1f111e5b5b70b38",
    "org.hamcrest:hamcrest-core:1.3": "66fdef91e9739348df7a096aa384a5685f4e875584cce89386a7a47251c4d8e9",
    "junit:junit:4.13.2": "8e495b634469d64fb8acfa3495a065cbacc8a0fff55ce1e31007be4c16dc57d3",
}


def jar(coordinates: str, environment: Mapping[str, str], *,
        extension: str = "jar",
        deadline: float | None = None) -> Path:
    """The jar (or ``extension`` file, such as ``pom``) of
    ``group:artifact:version[:classifier]``, from the cache or fetched. A
    pinned jar that does not match its ``TOOL_SHA256`` is unavailable."""
    relative = _relative(coordinates, extension)
    target = cache_dir(environment) / relative
    pinned = TOOL_SHA256.get(coordinates) if extension == "jar" else None
    remaining(deadline, _DOWNLOAD_TIMEOUT_S, "Java tool cache")
    if target.is_file():
        if pinned and hashlib.sha256(target.read_bytes()).hexdigest() != pinned:
            raise ToolUnavailable(f"{coordinates} in the tool cache {target.parent} does not match "
                                  f"its pinned SHA-256. Remove {target.name} to fetch it again")
        remaining(deadline, _DOWNLOAD_TIMEOUT_S, "Java tool cache")
        return target
    if offline(environment):
        raise ToolUnavailable(f"{coordinates} is not in the tool cache {target.parent}, and "
                              f"downloads are off ({OFFLINE})")
    url = f"{MAVEN_CENTRAL}/{relative}"
    try:
        with urllib.request.urlopen(url, timeout=remaining(
                deadline, _DOWNLOAD_TIMEOUT_S, "Java tool connection")) as response:
            data = _read(response, deadline, _DOWNLOAD_TIMEOUT_S)
        if not pinned:
            with urllib.request.urlopen(url + ".sha1", timeout=remaining(
                    deadline, _DOWNLOAD_TIMEOUT_S, "Java tool checksum connection")) as response:
                published = _read(response, deadline, _DOWNLOAD_TIMEOUT_S).decode("ascii", "replace").split()
    except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
        # A cut body raises http.client.IncompleteRead, which is no OSError.
        remaining(deadline, _DOWNLOAD_TIMEOUT_S, "Java tool download")
        raise ToolUnavailable(
            f"{coordinates} is not in the tool cache {target.parent} and could "
            f"not be fetched from Maven Central: {exc}") from exc
    if pinned:
        if hashlib.sha256(data).hexdigest() != pinned:
            raise ToolUnavailable(f"{coordinates} from Maven Central does not match its pinned SHA-256")
    elif not published or hashlib.sha1(data).hexdigest() != published[0].lower():
        raise ToolUnavailable(f"{coordinates} from Maven Central does not match "
                              "its published SHA-1")
    remaining(deadline, _DOWNLOAD_TIMEOUT_S, "Java tool cache write")
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=target.parent, suffix=".part")
    with os.fdopen(handle, "wb") as out:
        out.write(data)
    remaining(deadline, _DOWNLOAD_TIMEOUT_S, "Java tool cache promotion")
    os.replace(temporary, target)
    return target


# Running a Java suite with the JUnit Platform's console launcher.
#
# The launcher matches the project's JUnit Platform version, so the project's
# own engines (Jupiter for JUnit 5 and 6, Vintage for JUnit 4) run its tests.
# An engine the project leaves to its build tool (Surefire adds one when only
# the API is declared) is added at the project's version. Launcher, classpath
# and JVM settings do not depend on the build tool.

#: The platform for a JUnit 4 suite without one: the newest on Java 8.
DEFAULT_PLATFORM = "1.14.4"
OPENTEST4J = "org.opentest4j:opentest4j:1.3.0"
#: The Hamcrest a substituted JUnit 4 needs when the project has none.
HAMCREST = "org.hamcrest:hamcrest-core:1.3"
LAUNCHER_CLASS = "org.junit.platform.console.ConsoleLauncher"


@dataclass(frozen=True)
class Toolset:
    """The launcher's jars for one platform version, placed before the
    project's classpath, and its reported versions. ``overrides`` must
    precede the project's jars wherever those come first (a newer JUnit 4),
    and ``junit4`` says which JUnit 4 replaced the project's."""

    platform: str
    jars: Tuple[str, ...]
    overrides: Tuple[str, ...] = ()
    junit4: Optional[str] = None


def toolset(classpath: Sequence[str], environment: Mapping[str, str], *,
            executor: Optional[Executor] = None,
            scratch: Optional[Path] = None,
            deadline: float | None = None) -> Toolset:
    """The launcher and engines a suite run on ``classpath`` needs beyond it."""
    if isinstance(executor, ContainerExecutor):
        if scratch is None:
            raise OSError("container JUnit tools need mounted host scratch")
        require_mounted_output(str(scratch), executor)
    remaining(deadline, None, "JUnit tool selection")
    budget = {} if deadline is None else {"deadline": deadline}
    jupiter_api = _version(classpath, "junit-jupiter-api")
    platform = (_version(classpath, "junit-platform-engine")
                or (JvmProfile.platform_of_jupiter(jupiter_api) if jupiter_api else None)
                or DEFAULT_PLATFORM)
    family = JvmProfile.family(platform)
    # A substituted JUnit 4 precedes the project's jar on the run classpath.
    junit4 = _version(classpath, "junit")
    overrides: Tuple[str, ...] = ()
    replaced = None
    substitute = JvmProfile.junit4_substitute(junit4)
    if substitute:
        floor = [substitute]
        if not _version(classpath, "hamcrest-core"):
            floor.append(HAMCREST)
        overrides = tuple(str(jar(c, environment, **budget)) for c in floor)
        replaced = f"{substitute.rsplit(':', 1)[1]} in place of {junit4}"
    coordinates = [
        f"org.junit.platform:junit-platform-console:{platform}",
        f"org.junit.platform:junit-platform-reporting:{platform}",
        f"org.junit.platform:junit-platform-launcher:{platform}",
    ]
    spi = _reporting_spi(platform, environment, **budget)
    if spi:
        coordinates.append(spi)
    if not _version(classpath, "junit-platform-engine"):
        coordinates += [f"org.junit.platform:junit-platform-engine:{platform}",
                        f"org.junit.platform:junit-platform-commons:{platform}",
                        OPENTEST4J]
    if jupiter_api and not _version(classpath, "junit-jupiter-engine"):
        coordinates.append(f"org.junit.jupiter:junit-jupiter-engine:{jupiter_api}")
    if _version(classpath, "junit") and not _version(classpath, "junit-vintage-engine"):
        coordinates.append(f"org.junit.vintage:junit-vintage-engine:{family}")
    jars = overrides + tuple(str(jar(c, environment, **budget)) for c in coordinates)
    if isinstance(executor, ContainerExecutor):
        jars = stage_files(jars, scratch, executor=executor, **budget)
        overrides = jars[:len(overrides)]
    remaining(deadline, None, "JUnit tool selection")
    return Toolset(platform, jars, overrides, replaced)


def _reporting_spi(platform: str, environment: Mapping[str, str], *,
                    deadline: float | None = None) -> Optional[str]:
    """The open-test-reporting library the platform's reporting module
    needs at run time, read from its POM. None when it needs none."""
    pom = jar(f"org.junit.platform:junit-platform-reporting:{platform}",
                     environment, extension="pom", **({} if deadline is None else {"deadline": deadline}))
    root = ET.parse(pom).getroot()
    ns = {"m": "http://maven.apache.org/POM/4.0.0"}
    for dependency in root.findall("m:dependencies/m:dependency", ns):
        artifact = dependency.findtext("m:artifactId", default="", namespaces=ns)
        if artifact == "open-test-reporting-tooling-spi":
            group = dependency.findtext("m:groupId", default="", namespaces=ns)
            version = dependency.findtext("m:version", default="", namespaces=ns)
            return f"{group}:{artifact}:{version}"
    return None


@dataclass(frozen=True)
class Launch:
    """What the launcher did: exit code (None on timeout), output, XML reports."""

    returncode: Optional[int]
    output: str
    reports: Tuple[Path, ...]
    timed_out: bool = False


#: The configuration parameter of every Java test run: one test at a time,
#: as PIT's JUnit 5 plugin runs them.
SEQUENTIAL = "junit.jupiter.execution.parallel.enabled=false"


def argfile(path: Path, arguments: Sequence[str]) -> Path:
    """Write ``arguments`` as a ``java`` or ``javac`` argument file, so a
    long classpath fits any command line: one quoted argument a line, its
    backslashes and quotes escaped, so each reads back as given. PIT
    compares the paths it is given with its classpath's as written, and a
    system property may hold a backslash."""
    lines = ['"' + str(a).replace("\\", "\\\\").replace('"', '\\"') + '"'
             for a in arguments]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def arguments(classpath: Sequence[str], class_names: Sequence[str],
              reports: Path, *, jvm_args: Sequence[str] = (),
              system_properties: Mapping[str, str] = {},
              executor: Optional[Executor] = None) -> List[str]:
    """The ``java`` arguments that run the classes ``class_names`` with the
    console launcher, writing its XML reports to ``reports``. Jupiter's
    parallel execution is off, as under PIT, whatever the project sets."""
    require_mounted_output(str(reports), executor)
    reports.mkdir(parents=True, exist_ok=True)
    place = JavaEnv(executor)
    return place.java_arguments(
        classpath, LAUNCHER_CLASS, "execute", "--disable-banner",
        "--disable-ansi-colors", "--details=none", "--fail-if-no-tests",
        f"--reports-dir={place.translate(reports)}", f"--config={SEQUENTIAL}",
        *(f"--select-class={name}" for name in class_names),
        jvm_args=jvm_args, properties=system_properties)


def launch(java: str, classpath: Sequence[str], class_names: Sequence[str],
           reports: Path, *, jvm_args: Sequence[str] = (),
           system_properties: Mapping[str, str] = {}, cwd: str,
           env: Dict[str, str], timeout: float,
           executor: Optional[Executor] = None) -> Launch:
    """Run the classes ``class_names`` with the console launcher."""
    command_file_path = reports.parent / f"{reports.name}.args"
    require_mounted_output(str(command_file_path), executor)
    file = argfile(command_file_path, arguments(
        classpath, class_names, reports, jvm_args=jvm_args,
        system_properties=system_properties, executor=executor))
    command_file = (executor._to_container(str(file))
                    if isinstance(executor, ContainerExecutor) else str(file))
    try:
        done = JavaEnv(executor).launch([java, f"@{command_file}"], timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired as expired:
        reraise_unresolved(expired)
        output = "".join(value.decode("utf-8", "replace")
                         if isinstance(value, bytes) else (value or "")
                         for value in (expired.output, expired.stderr))
        return Launch(None, output,
                      tuple(sorted(reports.glob("TEST-*.xml"))), timed_out=True)
    return Launch(done.returncode, (done.stdout or "") + (done.stderr or ""),
                  tuple(sorted(reports.glob("TEST-*.xml"))))


@dataclass(frozen=True)
class Case:
    """One test case of a report: class, launcher name, outcome, time and
    message, and a failure's or error's stack text and exception type."""

    class_name: str
    name: str
    outcome: str
    time_s: Optional[float]
    message: str
    trace: str = ""
    exception: str = ""


def read_reports(paths: Sequence[Path]) -> List[Case]:
    """The test cases of the launcher's XML reports."""
    cases: List[Case] = []
    for path in paths:
        root = ET.parse(path).getroot()
        for case in root.iter("testcase"):
            outcome, message, trace, exception = "passed", "", "", ""
            for child in case:
                if child.tag in ("failure", "error", "skipped"):
                    outcome = {"failure": "failed", "error": "error",
                               "skipped": "skipped"}[child.tag]
                    message = (child.get("message") or child.text or "").strip()
                    if child.tag != "skipped":
                        trace = (child.text or "").strip()
                        exception = child.get("type") or ""
                    break
            time = case.get("time")
            cases.append(Case(case.get("classname", ""), case.get("name", ""),
                              outcome, float(time) if time else None, message,
                              trace, exception))
    return cases


def split_name(name: str) -> Tuple[str, Optional[str]]:
    """A launcher test name as method and parameters: ``value(int)[2]`` is
    ``("value", "[2]")``, ``positive()`` and ``positive`` ``("positive", None)``."""
    match = re.match(r"^([^(\[]+)(?:\([^)]*\))?(\[.*\])?$", name)
    if not match:
        return name, None
    return match.group(1), match.group(2)


@dataclass(frozen=True)
class TestName:
    """A test's unique id read as the suite runs name tests: the class
    (nested classes after ``$``), the method as the launcher names it, and
    the invocation of a parameterized or dynamic test (``[2]``)."""

    __test__ = False

    class_name: str
    method: str
    params: Optional[str]


def test_name(name: str) -> Optional[TestName]:
    """The test a JUnit Platform unique id names (PIT prefixes the test
    class), or None for no Jupiter or Vintage test. Segment values are
    decoded after the split, since the platform percent-encodes their
    brackets, colons, slashes and percent signs (``signs[0: sign of 2]``)."""
    from urllib.parse import unquote

    start = name.find("[engine:")
    if start < 0 or not name.endswith("]"):
        return None
    segments = []
    for part in name[start + 1:-1].split("]/["):
        kind, _, value = part.partition(":")
        segments.append((kind, unquote(value)))
    engine = segments[0][1]
    if engine == "junit-jupiter":
        cls, method, params = "", None, ""
        for kind, value in segments[1:]:
            if kind == "class":
                cls = value
            elif kind == "nested-class":
                cls += "$" + value
            elif kind in ("method", "test-template", "test-factory"):
                method = value
            elif kind in ("test-template-invocation", "dynamic-test",
                          "dynamic-container"):
                params += "[" + value.lstrip("#") + "]"
        if not cls or method is None:
            return None
        return TestName(cls, method, params or None)
    if engine == "junit-vintage":
        tests = [value for kind, value in segments if kind == "test"]
        runner = next((value for kind, value in segments if kind == "runner"),
                      "")
        if not tests:
            return None
        test = tests[-1]
        cls = runner
        if test.endswith(")") and "(" in test:
            test, _, cls = test[:-1].rpartition("(")
        method, params = split_name(test)
        if not cls:
            return None
        return TestName(cls, method, params)
    return None


def test_id(name: TestName, classes: Dict[str, Path], root: str | Path, *,
            function: bool = False) -> Optional[TestId]:
    """The suite's test ``name`` names, found by its top-level class's file
    among ``classes`` (relative to ``root``, both resolved). With
    ``function``, without its invocation. None when its class is none of
    the suite's files."""
    top = name.class_name.split("$")[0]
    file = classes.get(top)
    if file is None:
        return None
    nested = name.class_name[len(top):].replace("$", ".")
    method, _ = split_name(name.method)
    params = None if function else name.params
    text = f"{name.class_name}#{name.method}{params or ''}"
    return TestId(file.resolve().relative_to(Path(root).resolve()).as_posix(),
                  f"{top.rsplit('.', 1)[-1]}{nested}.{method}", params, text)
