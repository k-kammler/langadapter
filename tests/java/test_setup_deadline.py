"""The preparation stages share one explicit monotonic deadline."""
from hashlib import sha1
import http.client
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from langadapter.errors import InvalidRequest
from langadapter.execution import ContainerExecutor, LocalExecutor
from langadapter.java import (
    _build as project, _env as _deadline, _tools as _junit, _tools,
    _env as jdk,
)
from langadapter.java._env import JavaEnv
from langadapter.outcomes import Outcome
from langadapter.suites import Suite


class Clock:
    now = 10

    def monotonic(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(_deadline, "time", value)
    return value


@pytest.mark.parametrize("deadline", [True, "30", float("inf"), float("nan"), 10**1000])
def test_deadline_requires_finite_absolute_clock_value(clock, deadline):
    with pytest.raises(InvalidRequest):
        _deadline.remaining(deadline, 120, "native stage")


def test_the_remaining_time_is_capped_and_no_deadline_stays_unbounded(clock):
    assert _deadline.remaining(30, 120, "stage") == 20
    assert _deadline.remaining(30, 2, "stage") == 2
    assert _deadline.remaining(None, 120, "stage") == 120
    assert _deadline.remaining(None, None, "stage") is None
    clock.now = 25
    assert _deadline.remaining(30, None, "stage") == 5
    clock.now = 30
    with pytest.raises(subprocess.TimeoutExpired) as captured:
        _deadline.remaining(30, 120, "stage")
    assert captured.value.cmd == "stage"


@pytest.mark.parametrize("deadline,expected", [(20, [10, 7]), (None, [120, 120])])
def test_actual_container_jdk_probes_use_remaining_not_independent_budgets(clock, deadline, expected):
    calls = []
    executor = ContainerExecutor("selected")

    def run(argv, **options):
        calls.append(options["timeout"])
        clock.now += 3
        return subprocess.CompletedProcess(argv, 0, 'JAVA_VERSION="21.0.11"' if argv[0] == "cat" else "javac 21", "")

    executor.run = run
    result = jdk.read_in_container("/jdk", executor, deadline=deadline)
    assert result.version == "21.0.11" and calls == expected


def test_container_jdk_expiry_prevents_second_probe_and_is_not_missing_setup(clock):
    calls = []
    executor = ContainerExecutor("selected")

    def run(argv, **options):
        calls.append(argv)
        clock.now = 20
        return subprocess.CompletedProcess(argv, 0, 'JAVA_VERSION="21.0.11"', "")

    executor.run = run
    with pytest.raises(subprocess.TimeoutExpired):
        jdk.read_in_container("/jdk", executor, deadline=20)
    assert len(calls) == 1


def test_container_project_metadata_and_identity_share_explicit_budget(clock):
    executor = ContainerExecutor("selected")
    calls = []

    def run(argv, **options):
        calls.append((argv[0], options["timeout"]))
        clock.now += 1
        output = {"cat": "selected source", "test": "", "realpath": "/project\0/project/A.java\0",
                  "sha256sum": f"{'a' * 64}  /project/A.java\0"}[argv[0]]
        return subprocess.CompletedProcess(argv, 0, output, "")

    executor.run = run
    files = JavaEnv(executor, deadline=20)
    assert files.read("/project/A.java") == "selected source"
    assert files.is_file("/project/A.java")
    assert files.identity("/project/A.java", "/project").path == "A.java"
    assert calls == [("cat", 10), ("test", 9), ("realpath", 8), ("sha256sum", 7)]
    clock.now = 20
    with pytest.raises(subprocess.TimeoutExpired):
        files.is_file("/project/A.java")
    assert len(calls) == 4


@pytest.mark.parametrize("tool", ["maven", "gradle"])
@pytest.mark.parametrize("deadline", [None, 20])
def test_actual_resolution_keeps_selected_build_and_post_build_total(clock, tmp_path, tool, deadline):
    root, scratch = tmp_path / "project", tmp_path / "reports"
    root.mkdir()
    descriptor = root / ("pom.xml" if tool == "maven" else "build.gradle")
    descriptor.write_text("<project/>" if tool == "maven" else "", encoding="utf-8")
    launcher = root / ("mvnw.cmd" if project.os.name == "nt" else "mvnw") if tool == "maven" else root / (
        "gradlew.bat" if project.os.name == "nt" else "gradlew")
    launcher.write_text("", encoding="utf-8")
    calls = []
    executor = LocalExecutor()

    def run(argv, **options):
        calls.append((argv, options))
        clock.now += 3
        if tool == "maven":
            (scratch / "maven-effective-pom.xml").write_text("<project/>", encoding="utf-8")
            (scratch / "maven-test-classpath.txt").write_text("", encoding="utf-8")
        else:
            data = dict(projectDir=str(root), mainSources=[], testSources=[], mainClasses=[],
                        testClasses=[], testClasspath=[], release=8)
            (scratch / "gradle-projects" / "root.json").write_text(json.dumps(data), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "native build", "")

    executor.run = run
    result = project.resolve(str(root), jdk.Jdk("/selected-jdk", 21, "21"), executor,
                             str(scratch), deadline=deadline)
    assert result.outcome is Outcome.COMPLETED and result.project.build_tool == tool
    started = project.launch_argv(str(launcher))
    assert len(calls) == 1 and calls[0][0][:len(started)] == started
    assert calls[0][1]["timeout"] == (1800 if deadline is None else 10)
    assert calls[0][1]["env"]["JAVA_HOME"] == "/selected-jdk"


@pytest.mark.parametrize("tool", ["maven", "gradle"])
def test_native_success_after_entry_deadline_is_not_success(clock, tmp_path, tool):
    root = tmp_path / "project"
    root.mkdir()
    (root / "pom.xml").write_text("<project/>", encoding="utf-8")
    executor = LocalExecutor()

    def run(argv, **options):
        assert options["timeout"] == 2
        clock.now = 12
        return subprocess.CompletedProcess(argv, 0, "native done", "")

    executor.run = run
    result = project.resolve_with(tool, str(root), "selected-build", {}, executor,
                                  str(tmp_path / "reports"), 1800, deadline=12)
    assert result.outcome is Outcome.TIMED_OUT


def test_actual_container_resolution_carries_budget_through_discovery_build_jdk_and_reports(clock, tmp_path):
    scratch = tmp_path / "reports"
    executor = ContainerExecutor("selected", work_dir=str(tmp_path), container_work_dir="/zone")
    calls = []

    def run(argv, **options):
        assert 0 < options["timeout"] <= 40 - clock.now
        calls.append((argv, options))
        clock.now += 0.5
        if argv[0] == "test":
            return subprocess.CompletedProcess(argv, 0 if argv[-1] in (
                "/project/pom.xml", "/tools/mvn") else 1, "", "")
        if argv[0] == "cat":
            text = 'JAVA_VERSION="21.0.11"' if argv[-1].endswith("/release") else "<project/>"
            return subprocess.CompletedProcess(argv, 0, text, "")
        if argv[0] == "/build/jdk/bin/javac":
            return subprocess.CompletedProcess(argv, 0, "javac 21", "")
        assert argv[0] == "/tools/mvn"
        (scratch / "maven-effective-pom.xml").write_text("<project/>", encoding="utf-8")
        (scratch / "maven-test-classpath.txt").write_text("/cache/dependency.jar", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "build", "")

    executor.run = run
    result = project.resolve("/project", jdk._ContainerJdk("/runtime/jdk", 21, "21"),
        executor, str(scratch), environment={"LANGADAPTER_MAVEN": "/tools/mvn",
                                            project.BUILD_JDK: "/build/jdk"}, deadline=40)
    assert result.outcome is Outcome.COMPLETED and result.project.root == "/project"
    assert result.project.test_classpath[-1] == "/cache/dependency.jar"
    build = [options for argv, options in calls if argv[0] == "/tools/mvn"]
    assert len(build) == 1 and build[0]["env"]["JAVA_HOME"] == "/build/jdk"
    assert build[0]["timeout"] < calls[0][1]["timeout"]


def test_container_failed_metadata_after_deadline_is_not_a_missing_file(clock):
    executor = ContainerExecutor("selected")

    def run(argv, **options):
        clock.now = 20
        return subprocess.CompletedProcess(argv, 1, "", "missing")

    executor.run = run
    with pytest.raises(subprocess.TimeoutExpired):
        JavaEnv(executor, deadline=20).is_file("/project/pom.xml")


@pytest.mark.parametrize("deadline,expected", [(20, 10), (None, 600)])
def test_actual_suite_compile_and_resources_keep_selected_options(clock, tmp_path, deadline, expected):
    source, resource = tmp_path / "A.java", tmp_path / "fixture.txt"
    source.write_text("class A {}", encoding="utf-8")
    resource.write_text("fixture", encoding="utf-8")
    native = project.JavaProject(str(tmp_path), "gradle", 8, (), (), (), (), ())
    executor, calls = LocalExecutor(), []

    def run(argv, **options):
        calls.append((argv, options))
        clock.now += 2
        return subprocess.CompletedProcess(argv, 0, "", "")

    executor.run = run
    result = project.compile_suite(native, jdk.Jdk("/jdk", 21, "21"),
        Suite(str(tmp_path), (str(source), str(resource))), str(tmp_path / "classes"),
        executor, deadline=deadline)
    assert result.outcome is Outcome.COMPLETED and len(calls) == 1
    assert calls[0][1]["timeout"] == expected and "--release" in calls[0][0]
    assert (Path(result.classes) / "fixture.txt").read_text() == "fixture"


def test_expired_compile_is_timed_out_without_command_or_resource_staging(clock, tmp_path):
    source = tmp_path / "A.java"
    source.write_text("class A {}", encoding="utf-8")
    native = project.JavaProject(str(tmp_path), "maven", 8, (), (), (), (), ())
    result = project.compile_suite(native, jdk.Jdk("/jdk", 21, "21"),
        Suite(str(tmp_path), (str(source),)), str(tmp_path / "classes"), LocalExecutor(), deadline=10)
    assert result.outcome is Outcome.TIMED_OUT


@pytest.mark.parametrize("deadline,outcome", [(None, Outcome.INFRASTRUCTURE_FAILURE),
                                             (20, Outcome.TIMED_OUT)])
def test_a_confirmed_compiler_timeout_is_timed_out_only_under_a_deadline(clock, tmp_path, deadline, outcome):
    native = project.JavaProject(str(tmp_path), "maven", 8, (), (), (), (), ())
    executor = LocalExecutor()

    def run(*args, **options):
        raise subprocess.TimeoutExpired("selected javac", options["timeout"])

    executor.run = run
    result = project.compile_tests(native, jdk.Jdk("/jdk", 21, "21"), (),
                                    str(tmp_path / "classes"), executor, deadline=deadline)
    assert result.outcome is outcome


@pytest.mark.parametrize("stage", ["compiler", "maven", "gradle", "jdk"])
def test_uncertain_timeout_keeps_actual_owner_and_primary_instead_of_typed_failure(clock, tmp_path, stage):
    native = project.JavaProject(str(tmp_path), "maven", 8, (), (), (), (), ())
    (tmp_path / "pom.xml").write_text("<project/>", encoding="utf-8")
    error = subprocess.TimeoutExpired("owned native command", 1)
    error._langadapter_command = object()
    executor = ContainerExecutor("selected", work_dir=str(tmp_path)) if stage == "jdk" else LocalExecutor()

    def run(*args, **options):
        raise error

    executor.run = run
    with pytest.raises(subprocess.TimeoutExpired) as captured:
        if stage == "compiler":
            project.compile_tests(native, jdk.Jdk("/jdk", 21, "21"), (),
                                   str(tmp_path / "classes"), executor, deadline=20)
        elif stage == "jdk":
            jdk.read_in_container("/jdk", executor, deadline=20)
        else:
            project.resolve_with(stage, str(tmp_path), "selected-build", {}, executor,
                                 str(tmp_path / "reports"), 1800, deadline=20)
    assert captured.value is error and captured.value._langadapter_command is error._langadapter_command


@pytest.mark.parametrize("call", [project.maven_modules, project._packaging])
def test_uncertain_container_metadata_error_cannot_fall_back_to_an_empty_model(clock, call):
    executor = ContainerExecutor("selected")
    error = OSError("container metadata command cleanup is unresolved")
    error._langadapter_command = object()

    def run(*args, **options):
        raise error

    executor.run = run
    with pytest.raises(OSError) as captured:
        call(Path("/project/pom.xml"), JavaEnv(executor))
    assert captured.value is error


@pytest.mark.parametrize("call,expected", [(project.maven_modules, []), (project._packaging, "jar")])
@pytest.mark.parametrize("marker", ["none", "disposed"])
def test_fully_cleaned_container_metadata_error_keeps_ordinary_fallback(call, expected, marker):
    executor = ContainerExecutor("selected")
    error = OSError("native metadata unavailable")
    if marker == "disposed":
        error._langadapter_command = SimpleNamespace(_langadapter_command_disposed=True)

    def run(*args, **options):
        raise error

    executor.run = run
    assert call(Path("/project/pom.xml"), JavaEnv(executor)) == expected


def test_container_tool_copy_stops_before_next_copy_when_budget_is_exhausted(clock, tmp_path, monkeypatch):
    sources = [tmp_path / "cache" / name for name in ("first.jar", "second.jar")]
    sources[0].parent.mkdir()
    for source in sources:
        source.write_bytes(b"cached")
    zone = tmp_path / "zone"
    zone.mkdir()
    executor = ContainerExecutor("selected", work_dir=str(zone), container_work_dir="/zone")
    copied = []
    original = _tools.shutil.copyfile

    def copy(source, destination):
        copied.append(source)
        clock.now = 20
        return original(source, destination)

    monkeypatch.setattr(_tools.shutil, "copyfile", copy)
    with pytest.raises(subprocess.TimeoutExpired):
        _tools.stage_files(tuple(map(str, sources)), zone / "tools", executor=executor, deadline=20)
    assert copied == [sources[0]]


@pytest.mark.parametrize("expire", ["none", "body", "checksum"])
def test_tool_download_and_checksum_connection_share_budget(clock, tmp_path, monkeypatch, expire):
    calls = []
    data = b"fixture jar"
    checksum = sha1(data).hexdigest().encode("ascii")

    class Response(io.BytesIO):
        def read1(self, size):
            clock.now += 2
            if ((expire == "body" and self.getvalue() == data)
                    or (expire == "checksum" and self.getvalue() == checksum)):
                clock.now = 20
            return super().read1(size)

    def fetch(url, **options):
        calls.append(options["timeout"])
        return Response(checksum if url.endswith(".sha1") else data)

    monkeypatch.setattr(_tools.urllib.request, "urlopen", fetch)
    environment = {"LANGADAPTER_JAVA_TOOLS": str(tmp_path / "cache")}
    if expire != "none":
        with pytest.raises(subprocess.TimeoutExpired):
            _tools.jar("fixture:artifact:1", environment, deadline=20)
        assert not (tmp_path / "cache" / "fixture/artifact/1/artifact-1.jar").exists()
        assert len(calls) == (1 if expire == "body" else 2)
    else:
        result = _tools.jar("fixture:artifact:1", environment, deadline=20)
        assert result.read_bytes() == data and calls == [10, 6]


def test_expired_cache_hit_and_toolset_do_not_mask_timeout(clock, tmp_path, monkeypatch):
    cached = tmp_path / "fixture/artifact/1/artifact-1.jar"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"cached")
    environment = {"LANGADAPTER_JAVA_TOOLS": str(tmp_path)}
    with pytest.raises(subprocess.TimeoutExpired):
        _tools.jar("fixture:artifact:1", environment, deadline=10)
    with pytest.raises(subprocess.TimeoutExpired):
        _junit.toolset([], environment, deadline=10)


def test_tool_body_caps_each_blocking_read_to_remaining_budget(clock, tmp_path, monkeypatch):
    data = b"fixture jar"
    timeouts = []
    reads = []

    class Response(io.BytesIO):
        def __init__(self, content, body):
            super().__init__(content)
            self.body = body
            self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=SimpleNamespace(
                settimeout=lambda value: timeouts.append((body, value)))))

        def read1(self, size):
            reads.append(self.body)
            if self.body and self.tell() == 0:
                clock.now += 9
            return super().read1(size)

    monkeypatch.setattr(_tools.urllib.request, "urlopen", lambda url, **kw:
        Response(sha1(data).hexdigest().encode() if url.endswith(".sha1") else data,
                 not url.endswith(".sha1")))
    result = _tools.jar("fixture:artifact:1", {"LANGADAPTER_JAVA_TOOLS": str(tmp_path)}, deadline=20)
    assert result.read_bytes() == data
    assert timeouts == [(True, 10), (True, 1), (False, 1), (False, 1)]
    assert reads == [True, True, False, False]


def test_actual_stdlib_http_response_socket_and_consumed_eof_use_remaining_budget(clock):
    timeouts = []
    chunks = [b"HTTP/1.1 200 OK\r\nContent-Length: 6\r\n\r\n", b"jar", b"jar"]

    class Raw(io.RawIOBase):
        def readable(self):
            return True

        def readinto(self, buffer):
            chunk = chunks.pop(0) if chunks else b""
            if chunk == b"jar" and chunks:
                clock.now += 9
            buffer[:len(chunk)] = chunk
            return len(chunk)

    raw = Raw()
    sock = SimpleNamespace(settimeout=timeouts.append, makefile=lambda *args: io.BufferedReader(raw))
    raw._sock = sock
    with http.client.HTTPResponse(sock) as response:
        response.begin()
        assert _tools._read(response, 20, 120) == b"jarjar"
        # Newer Pythons close the consumed response, Python 3.10 leaves it
        # open and reads its end once more.
        assert response.fp is None or response.length == 0
        closed_at_end = response.fp is None
    # Every blocking read got the time left before the deadline.
    assert timeouts == ([10, 1] if closed_at_end else [10, 1, 1])


@pytest.mark.parametrize("stage", ["body", "checksum", "connection"])
@pytest.mark.parametrize("deadline,expired", [(20, True), (20, False), (None, True)])
def test_tool_socket_timeout_at_deadline_is_not_missing_setup(clock, tmp_path, monkeypatch, stage, deadline, expired):
    data = b"fixture jar"

    class Response(io.BytesIO):
        def read1(self, size=-1):
            if stage == ("checksum" if self.getvalue() != data else "body"):
                clock.now = 20 if expired else 19
                raise TimeoutError("socket read timed out")
            return super().read1(size)

        def read(self, *args):
            return self.read1(*args)

    def fetch(url, **kw):
        if stage == "connection":
            clock.now = 20 if expired else 19
            raise _tools.urllib.error.URLError(TimeoutError("connect timed out"))
        return Response(sha1(data).hexdigest().encode() if url.endswith(".sha1") else data)

    monkeypatch.setattr(_tools.urllib.request, "urlopen", fetch)
    expected = subprocess.TimeoutExpired if deadline is not None and expired else _tools.ToolUnavailable
    with pytest.raises(expected):
        _tools.jar("fixture:artifact:1", {"LANGADAPTER_JAVA_TOOLS": str(tmp_path)}, deadline=deadline)
    assert not (tmp_path / "fixture/artifact/1/artifact-1.jar").exists()


def test_junit_family_and_every_tool_fetch_receive_same_budget(clock, tmp_path, monkeypatch):
    seen = []

    def jar(coordinate, environment, *, extension="jar", deadline=None):
        seen.append((coordinate, deadline, clock.now))
        _deadline.remaining(deadline, 120, "fixture download")
        clock.now += 1
        _, artifact, version = coordinate.split(":")
        target = tmp_path / f"{artifact}-{version}.{extension}"
        target.write_text("<project/>" if extension == "pom" else "jar", encoding="utf-8")
        return target

    monkeypatch.setattr(_tools, "jar", jar)
    result = _junit.toolset(("junit-jupiter-api-5.14.4.jar",), {}, deadline=30)
    assert result.platform == "1.14.4" and len(result.jars) == 7
    assert all(deadline == 30 for _, deadline, _ in seen)
    assert any("junit-jupiter-engine:5.14.4" in coordinate for coordinate, _, _ in seen)
