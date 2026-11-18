"""Tool helpers use the same owned-command boundary as suite execution."""
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter import _process_tree
from langadapter.java import (
    _benchmark, _jacoco, _pit, _tools, _sources, _runs as _evidence,
)
from langadapter.python.adapter import PythonAdapter


@pytest.fixture
def tools(monkeypatch, tmp_path):
    calls = []
    state = SimpleNamespace(error=None, code=0, output="", compiler_work=None)
    cache = tmp_path / "cache"
    cache.mkdir()
    selected = {"SELECTED_TOOL_INPUT": "unchanged"}
    jdk = SimpleNamespace(tool=lambda name: "/selected/jdk/bin/" + name)
    monkeypatch.setattr(_tools, "cache_dir", lambda environment: cache)
    monkeypatch.setattr(_tools, "jar", lambda *args: tmp_path / "selected.jar")
    monkeypatch.setattr(_evidence, "API", ("group:artifact:version",))
    monkeypatch.setattr(_pit, "classpath", lambda *args, **kwargs: (str(tmp_path / "pit.jar"),))

    def forbidden(*args, **kwargs):
        pytest.fail("tool helpers must not bypass owned-command execution")

    def owned(args, **options):
        calls.append((list(args), options))
        if "-d" in args:
            output = Path(args[args.index("-d") + 1])
            state.compiler_work = output.parent
            if state.error is None and state.code == 0:
                output.mkdir()
                (output / (Path(args[-1]).stem + ".class")).write_bytes(b"controlled class")
        if state.error is not None:
            raise state.error
        return subprocess.CompletedProcess(args, state.code, state.output, "native diagnostic")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(_process_tree, "run", owned)
    return SimpleNamespace(calls=calls, state=state, cache=cache, env=selected, jdk=jdk,
                           root=tmp_path)


def compile_helper(name, tools):
    if name == "parse":
        return _sources._parse_helper(tools.jdk.tool("javac"), {}, tools.env)
    if name == "analyzer":
        return _jacoco.analyzer(tools.jdk, {}, tools.env)[0]
    if name == "listener":
        return _evidence.listener(tools.jdk, {}, tools.env)
    return _pit.test_order(tools.jdk.tool("javac"), {}, tools.env)


@pytest.mark.parametrize("name", ["parse", "analyzer", "listener", "order"])
def test_compiled_helpers_publish_only_after_owned_command_returns(tools, name):
    result = compile_helper(name, tools)
    assert result.is_dir() and len(tools.calls) == 1
    args, options = tools.calls[0]
    assert args[0] == tools.jdk.tool("javac") and args[1:3] == ["--release", "11"]
    assert options == {"text": True, "encoding": "utf-8", "errors": "replace",
                       "env": tools.env, "timeout": 300}
    assert compile_helper(name, tools) == result and len(tools.calls) == 1


@pytest.mark.parametrize("name", ["parse", "analyzer", "listener", "order"])
def test_uncertain_compiler_disposal_retains_partial_inputs_and_error(tools, name):
    error = OSError("compiler ownership remains uncertain")
    tools.state.error = error
    with pytest.raises(OSError) as caught:
        compile_helper(name, tools)
    assert caught.value is error
    assert tools.state.compiler_work.is_dir()
    assert list(tools.state.compiler_work.glob("*.java"))
    assert not list(tools.cache.glob("helpers/*/*.class"))


def test_pit_operator_listing_keeps_native_argv_and_output(tools):
    tools.state.output = "native.mutator\n"
    result = _pit.mutator_ids(tools.jdk.tool("java"), ["NATIVE_GROUP"], tools.root,
                              environment={}, env=tools.env)
    assert result == ("native.mutator",)
    args, options = tools.calls[0]
    assert args[0] == tools.jdk.tool("java") and args[-1] == "NATIVE_GROUP"
    assert Path(args[-2]).name == "LangAdapterPitMutators.java"
    assert options["timeout"] == _pit._LISTING_TIMEOUT_S and options["env"] is tools.env


def test_coverage_analysis_keeps_native_records_and_timeout(tools, monkeypatch):
    target = tools.root / "Target.java"
    target.write_text("package demo; public class Target {}", encoding="utf-8")
    monkeypatch.setattr(_jacoco, "analyzer", lambda *args, **kwargs: (tools.root, ("core.jar",)))
    monkeypatch.setattr(_jacoco, "class_files", lambda *args, **kwargs: ("native.class",))
    tools.state.output = "suite\tL:1:1\n"
    records, reason = _jacoco.analyze(tools.jdk, "run", tools.root / "data", target, [],
                                    environment={}, env=tools.env)
    assert records == {"suite": ["L:1:1"]} and reason == ""
    args, options = tools.calls[0]
    assert args[0] == tools.jdk.tool("java") and args[-1] == "native.class"
    assert options["timeout"] == _jacoco._ANALYSIS_TIMEOUT_S and options["env"] is tools.env


@pytest.mark.parametrize("language", ["python", "java"])
def test_affinity_observation_uses_owned_command_without_changing_probe(tools, language):
    tools.state.output = "[1, 2]\n"
    prefix = ["selected-affinity", "-c", "1,2"]
    if language == "python":
        adapter = PythonAdapter(lb.ProjectContext(
            str(tools.root), interpreter=lb.Interpreter(path=sys.executable)))
        # The probe starts as the benchmark's repetitions do, with their environment.
        result = adapter._allowed_cpus(prefix, str(tools.root), {"SELECTED": "1"})
    else:
        result = _benchmark._allowed_cpus(prefix, str(tools.root))
    assert result == (1, 2)
    args, options = tools.calls[0]
    assert args[:3] == prefix and args[-4:] == ["-I", "-S", "-", "allowed"]
    assert options["cwd"] == str(tools.root) and options["timeout"] == 60
    assert "sched_getaffinity" in options["input"]
    if language == "python":
        assert options["env"] == {"SELECTED": "1"}
