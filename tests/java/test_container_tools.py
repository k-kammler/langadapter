"""Host tool caches are staged explicitly for the selected container."""
from pathlib import Path, PurePosixPath

import pytest

from langadapter.execution import ContainerExecutor
from langadapter.java import _tools as _junit, _tools


def container(zone):
    return ContainerExecutor("selected", work_dir=str(zone), container_work_dir="/workzone")


def test_native_tool_paths_are_preserved_without_copying(tmp_path):
    paths = (str(tmp_path / "nonexistent.jar"), "/other/tool.jar")
    output = tmp_path / "output"
    assert _tools.stage_files(paths, output) == paths
    assert not output.exists()


def test_tools_are_copied_without_aliases_or_filename_collisions(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    files = []
    for name, content in (("first", b"one"), ("second", b"two")):
        source = tmp_path / name / "same.jar"
        source.parent.mkdir()
        source.write_bytes(content)
        files.append(str(source))
    output = _tools.stage_files([*files, files[0]], zone / "tools", executor=container(zone))
    assert output[0] != output[1]
    assert output[0] == output[2]
    host_paths = [zone / PurePosixPath(path).relative_to("/workzone") for path in output]
    assert [path.name for path in host_paths] == ["same.jar"] * 3
    assert [path.read_bytes() for path in host_paths] == [b"one", b"two", b"one"]
    host_paths[0].write_bytes(b"changed")
    assert Path(files[0]).read_bytes() == b"one"
    assert all(path.startswith("/workzone/tools/") for path in output)


def test_mounted_tool_is_kept_without_copying(tmp_path):
    source = tmp_path / "cache" / "a.jar"
    source.parent.mkdir()
    source.write_bytes(b"cached")
    result = _tools.stage_files([str(source)], tmp_path / "tools", executor=container(tmp_path))
    assert result == ("/workzone/cache/a.jar",)
    assert list((tmp_path / "tools").iterdir()) == []


def test_unmounted_staging_is_refused_before_any_copy(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    source = tmp_path / "a.jar"
    source.write_bytes(b"cached")
    output = tmp_path / "foreign"
    with pytest.raises(OSError, match="work folder"):
        _tools.stage_files([str(source)], output, executor=container(zone))
    assert not output.exists()


def test_missing_tool_is_refused_before_output_creation(tmp_path):
    output = tmp_path / "output"
    with pytest.raises(OSError, match="unavailable"):
        _tools.stage_files([str(tmp_path / "missing.jar")], output, executor=container(tmp_path))
    assert not output.exists()


def test_copy_error_remains_an_execution_error(tmp_path, monkeypatch):
    source = tmp_path / "a.jar"
    source.write_bytes(b"cached")
    zone = tmp_path / "zone"
    zone.mkdir()

    def failed_copy(*args):
        raise OSError("copy failed")

    monkeypatch.setattr(_tools.shutil, "copyfile", failed_copy)
    with pytest.raises(OSError, match="copy failed"):
        _tools.stage_files([str(source)], zone / "tools", executor=container(zone))
    assert source.read_bytes() == b"cached"


@pytest.mark.parametrize("unmounted", [False, True])
def test_container_toolset_refuses_bad_scratch_before_fetching(tmp_path, monkeypatch, unmounted):
    zone = tmp_path / "zone"
    zone.mkdir()

    def forbidden_fetch(*args, **options):
        pytest.fail("tool fetched before scratch validation")

    monkeypatch.setattr(_tools, "jar", forbidden_fetch)
    with pytest.raises(OSError, match="scratch|work folder"):
        _junit.toolset([], {}, executor=container(zone),
                       scratch=tmp_path / "outside" if unmounted else None)


@pytest.mark.parametrize("runtime", ["native", "container"])
def test_toolset_keeps_junit4_override_order_and_platform(tmp_path, monkeypatch, runtime):
    cache = tmp_path / "cache"
    cache.mkdir()
    zone = tmp_path / "zone"
    zone.mkdir()

    def jar(coordinate, environment, *, extension="jar"):
        _, artifact, version = coordinate.split(":")
        path = cache / f"{artifact}-{version}.{extension}"
        path.write_text('<project xmlns="http://maven.apache.org/POM/4.0.0"/>'
                        if extension == "pom" else coordinate)
        return path

    monkeypatch.setattr(_tools, "jar", jar)
    executor = container(zone) if runtime == "container" else None
    tools = _junit.toolset(["/project/junit-4.11.jar"], {}, executor=executor,
                          scratch=zone / "tools")
    assert tools.platform == _junit.DEFAULT_PLATFORM
    assert tools.junit4 == "4.13.2 in place of 4.11"
    assert tools.overrides == tools.jars[:2]
    assert [Path(path).name for path in tools.overrides] == ["junit-4.13.2.jar", "hamcrest-core-1.3.jar"]
    if runtime == "container":
        assert all(path.startswith("/workzone/tools/") for path in tools.jars)
    else:
        assert all(Path(path).parent == cache for path in tools.jars)
