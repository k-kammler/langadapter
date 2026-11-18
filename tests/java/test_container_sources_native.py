"""Opt-in selected-helper Java source substrate, not public container admission."""
import hashlib
import os
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.java import _tools
from langadapter.java._build import Resolution


@pytest.mark.parametrize("operation", ["validate", "identity"])
def test_offline_supervised_container_source_operations(tmp_path, monkeypatch, operation):
    names = ("LANGADAPTER_TEST_CONTAINER", "LANGADAPTER_TEST_CONTAINER_JAVA",
             "LANGADAPTER_TEST_WORK_DIR", "LANGADAPTER_TEST_CONTAINER_HELPER_PYTHON")
    container, java, zone, helper = (os.environ.get(name) for name in names)
    if not all((container, java, zone, helper)):
        pytest.skip("requires a scheduled borrowed container, mapped work directory, JDK and control helper")

    def forbidden(*args, **kwargs):
        pytest.fail("offline source fixture attempted a tool download or host helper cache")

    monkeypatch.setattr(_tools.urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(_tools, "cache_dir", forbidden)
    root = tmp_path / "subject"
    root.mkdir()
    source = "package demo; public class Calc { public static int value() { return 1; } }\n"
    target = root / "Calc.java"
    target.write_text(source, encoding="utf-8", newline="\n")
    execution = lb.ContainerExecution(container, work_dir=zone, container_work_dir="/workzone",
                                      helper_python=helper)
    context = lb.ProjectContext("/workzone", lb.Interpreter(path=java), execution=execution)
    from langadapter.execution import executor_for

    executor = executor_for(context)
    context = lb.ProjectContext(executor._to_container(str(root)), lb.Interpreter(path=java),
                                execution=execution)
    adapter = lb.create("java", context)
    # Real selected tools. Project resolution and public admission are separate.
    monkeypatch.setattr(adapter, "project", lambda: Resolution(lb.Outcome.UNSUPPORTED, reason="no build"))
    if operation == "validate":
        valid = adapter.validate_source(source)
        invalid = adapter.validate_source("public class Calc { int value = ; }")
        assert valid.status == "valid", valid
        assert invalid.status == "syntax_error" and invalid.line == 1, invalid
        assert valid.source_sha256 == hashlib.sha256(source.encode()).hexdigest()
        assert "no project build at the root" in valid.interpreter
        assert adapter.validate_source(source) == valid
    else:
        identity = adapter.target_identity("Calc.java")
        assert identity.module_name == "demo.Calc" and identity.source_path == "Calc.java"
        assert identity.project_root == context.root and identity.target_source == source
        assert identity.target_source_sha256 == hashlib.sha256(source.encode()).hexdigest()
    assert target.read_text(encoding="utf-8") == source
    assert not list(Path(zone).glob("langadapter_java_parse_*"))
