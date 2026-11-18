"""The tools a project Python is measured with come from its version profile:
Python 3.14 has pins of its own, a coverage configuration that keeps the C
tracer, and a Cosmic Ray command line that starts with an event loop. Python
3.10 to 3.13 keep theirs."""
import json
import subprocess
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.execution import LocalExecutor
from langadapter.python import _pinned_tools
from langadapter.python._profile import SUPPORT, PythonProfile
from langadapter.versions import Support, python_version

EARLIER = {"pytest": "8.1.1", "coverage": "7.6.8", "cosmic-ray": "8.4.1"}
LATER = {"pytest": "8.4.2", "coverage": "7.16.2", "cosmic-ray": "8.4.1"}
ROOT = Path(__file__).resolve().parents[2]


def _profile(text):
    return PythonProfile(None if text is None else python_version("CPython", text, "probe"),
                         SUPPORT)


@pytest.mark.parametrize("text, tools", [
    ("3.9.18", EARLIER), ("3.10.20", EARLIER), ("3.12.3", EARLIER), ("3.13.15", EARLIER),
    ("3.14.0", LATER), ("3.14.7", LATER), ("3.15.0", LATER), ("unknown", EARLIER), (None, EARLIER)])
def test_each_project_python_has_its_pinned_tools(text, tools):
    assert _profile(text).tools == tools
    assert _pinned_tools(text) == tools


def test_python_3_15_is_tested_and_3_16_is_not():
    assert _profile("3.15.2").support is Support.TESTED
    assert _profile("3.16.0").support is Support.UNTESTED
    assert SUPPORT.runtime.describe() == "3.9 or newer (tested 3.10 to 3.15)"


def test_python_3_14_measures_coverage_with_the_c_tracer():
    """coverage 7.16.2 would choose sys.monitoring on 3.14, whose arcs read
    differently from the C tracer's that every other version records."""
    assert _profile("3.13.15").coverage_settings() == "[run]\nbranch = True\n"
    assert _profile("3.14.7").coverage_settings() == "[run]\nbranch = True\ncore = ctrace\n"


def test_python_3_14_starts_cosmic_ray_with_an_event_loop():
    """Cosmic Ray's HTTP distributor asks asyncio for the current event loop,
    which Python 3.14 no longer creates on request."""
    assert _profile("3.13.15").cosmic_ray_command("/p/python", "/p/cosmic-ray") == ["/p/cosmic-ray"]
    python, flag, program = _profile("3.14.7").cosmic_ray_command("/p/python", "/p/cosmic-ray")
    assert (python, flag) == ("/p/python", "-c")
    assert "asyncio.set_event_loop(loop)" in program
    assert "from cosmic_ray.cli import main" in program


def test_the_setup_probe_names_the_pinned_tools():
    from langadapter.python import _setup
    from langadapter.python._profile import TOOL_NAMES

    assert f"for name in {TOOL_NAMES!r}" in _setup._PROBE


def _metadata(version, tools):
    return json.dumps({"implementation": "CPython", "version": version, "tools": tools})


@pytest.mark.parametrize("version, tools", [("3.12.3", EARLIER), ("3.14.7", LATER)])
def test_the_capability_report_names_the_pins_of_the_project_python(
        tmp_path, monkeypatch, version, tools):
    (tmp_path / "subject.py").write_text("value = 1\n", encoding="ascii")
    (tmp_path / "runtime").write_text("", encoding="ascii")
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(str(tmp_path / "runtime")),
                                environment=lb.Environment({"PATH": "selected"}, inherit=False))
    monkeypatch.setattr(LocalExecutor, "run", lambda executor, argv, **options: (
        subprocess.CompletedProcess(argv, 0, _metadata(version, tools), "")))
    report = lb.create("python", context).capabilities()
    assert {tool.name: tool.pinned for tool in report.tools} == tools
    assert all(tool.pinned in (None, tool.found) for tool in report.tools)


@pytest.mark.parametrize("version", ["3.10", "3.13", "3.14", "3.15"])
def test_the_development_pins_are_the_project_tools_of_each_python(version):
    """The library's own tests read coverage data with the tools a project
    Python of the same version is measured with."""
    try:
        import tomllib
    except ImportError:  # Python 3.10
        import tomli as tomllib
    from packaging.requirements import Requirement

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    selected = {}
    for text in data["project"]["optional-dependencies"]["dev"]:
        requirement = Requirement(text)
        if requirement.marker is None or requirement.marker.evaluate({"python_version": version}):
            selected[requirement.name] = str(requirement.specifier)
    tools = _pinned_tools(version)
    assert {name: selected[name] for name in ("pytest", "coverage")} == {
        name: f"=={tools[name]}" for name in ("pytest", "coverage")}
