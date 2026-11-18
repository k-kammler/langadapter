"""Per-test coverage listener preparation uses the selected container and owned scratch."""
import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from langadapter.execution import ContainerExecutor
from langadapter.java import _tools, _runs as _evidence


class EvidenceContainer(ContainerExecutor):
    def __init__(self, zone):
        super().__init__("selected", work_dir=str(zone), container_work_dir="/workzone")
        self.calls = []
        self.returncode = 0

    def run(self, argv, **options):
        self.calls.append((argv, options))
        Path(argv[argv.index("-d") + 1]).mkdir()
        return subprocess.CompletedProcess(argv, self.returncode, "compiler output", "compiler error")


@pytest.fixture
def tools(tmp_path, monkeypatch):
    zone = tmp_path / "zone"
    zone.mkdir()
    cache = tmp_path / "cache"
    cache.mkdir()
    jars = []
    for index, coordinate in enumerate(_evidence.API):
        path = cache / f"api-{index}.jar"
        path.write_bytes(coordinate.encode())
        jars.append(path)
    lookups = []

    def jar(coordinate, environment):
        lookups.append(coordinate)
        return jars[_evidence.API.index(coordinate)]

    monkeypatch.setattr(_tools, "jar", jar)
    monkeypatch.setattr(_evidence.subprocess, "run", lambda *args, **kwargs: pytest.fail(
        "container coverage listener launched a host compiler"))
    container = EvidenceContainer(zone)
    jdk = SimpleNamespace(tool=lambda name: "/opt/jdk/bin/" + name)
    return zone, cache, jars, lookups, container, jdk


def test_listener_api_jars_are_staged_and_compiled_with_selected_container(tools):
    zone, cache, jars, lookups, container, jdk = tools
    out = _evidence.listener(jdk, {}, {"JAVA_EXTRA": "selected"}, executor=container, scratch=zone)
    assert out.is_relative_to(zone)
    command, options = container.calls[0]
    assert command[0] == "/opt/jdk/bin/javac"
    assert options == {"env": {"JAVA_EXTRA": "selected"}, "timeout": 300}
    classpath = command[command.index("-cp") + 1]
    assert len(classpath.split(":")) == len(jars)
    assert all(entry.startswith("/workzone/tools/") for entry in classpath.split(":"))
    assert {path.name: path.read_bytes() for path in (zone / "tools").rglob("*.jar")} == {
        jar.name: jar.read_bytes() for jar in jars}
    assert lookups == list(_evidence.API)
    service = out / "META-INF/services/org.junit.platform.launcher.TestExecutionListener"
    assert service.read_text(encoding="utf-8") == "LangAdapterCoverageListener\n"
    assert set(cache.iterdir()) == set(jars)


def cached_listener(cache):
    digest = hashlib.sha256(_evidence._LISTENER.encode("utf-8")).hexdigest()[:16]
    helper = cache / "helpers" / f"listener-{digest}"
    helper.mkdir(parents=True)
    (helper / "LangAdapterCoverageListener.class").write_bytes(b"native compiled listener")
    return helper


def test_native_listener_keeps_its_existing_cached_helper(tools):
    _, cache, _, lookups, container, jdk = tools
    helper = cached_listener(cache)
    assert _evidence.listener(jdk, {"LANGADAPTER_JAVA_TOOLS": str(cache)}, {}) == helper
    assert lookups == list(_evidence.API)
    assert not container.calls


def test_container_listener_does_not_reuse_native_compiled_helper(tools):
    zone, cache, _, _, container, jdk = tools
    helper = cached_listener(cache)
    out = _evidence.listener(jdk, {"LANGADAPTER_JAVA_TOOLS": str(cache)}, {},
                             executor=container, scratch=zone)
    assert out.is_relative_to(zone) and out != helper
    assert len(container.calls) == 1
    assert (helper / "LangAdapterCoverageListener.class").read_bytes() == b"native compiled listener"


@pytest.mark.parametrize("missing", [True, False], ids=["absent", "unmounted"])
def test_bad_listener_scratch_is_refused_before_tool_lookup_or_writes(tools, missing):
    zone, _, _, lookups, container, jdk = tools
    scratch = None if missing else zone.parent / "foreign"
    with pytest.raises(OSError, match="scratch|work folder"):
        _evidence.listener(jdk, {}, {}, executor=container, scratch=scratch)
    assert not lookups and not container.calls
    assert not (zone.parent / "foreign").exists()


def test_listener_compiler_failure_does_not_publish_a_registered_helper(tools):
    zone, _, _, _, container, jdk = tools
    container.returncode = 1
    with pytest.raises(_tools.ToolUnavailable, match="compiler outputcompiler error"):
        _evidence.listener(jdk, {}, {}, executor=container, scratch=zone)
    out = Path(container.calls[0][0][container.calls[0][0].index("-d") + 1])
    assert not (out / "META-INF").exists()
