"""The neutral hook table: every family and hook with its texts, every public
operation mapped to a hook, the build-up order, the JSON form and what the
adapter classes provide."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from langadapter import hooks
from langadapter.capabilities import OPERATION_NAMES

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def _toy():
    spec = importlib.util.spec_from_file_location("example_toy_hooks", EXAMPLES / "toy_adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ToyAdapter


def test_every_record_has_its_texts_and_a_known_family():
    names = {f.name for f in hooks.families()}
    for entry in hooks.table():
        assert entry.family in names, entry.name
        assert entry.kind in ("fact", "hook"), entry.name
        for text in (entry.title, entry.what, entry.enables, entry.default, entry.test):
            assert text.strip(), (entry.name, text)
        assert entry.guide.startswith("add-a-language.md#"), entry.name
    from langadapter.capabilities import CAPABILITIES

    for family in hooks.families():
        assert family.title and family.enables and family.useful_for, family.name
        assert family.guide.startswith("add-a-language.md#"), family.name
        assert hooks.hooks_of(family.name), family.name
        assert set(family.capabilities) <= set(CAPABILITIES), family.name


def test_every_public_operation_is_built_on_a_hook():
    mapped = {name for entry in hooks.table() for name in entry.operations}
    assert set(OPERATION_NAMES) - mapped == set()
    assert mapped - set(OPERATION_NAMES) == set()


def test_the_table_is_in_build_up_order_and_names_are_unique():
    order = [hooks.family(entry.family).order for entry in hooks.table()]
    assert order == sorted(order)
    names = [entry.name for entry in hooks.table()]
    assert len(names) == len(set(names))
    assert [f.name for f in hooks.families()][:4] == ["facts", "run tests", "coverage", "mutation"]


def test_every_fact_and_hook_names_what_an_adapter_holds():
    """Each name a fact or hook lists is an attribute of LangAdapter or of a
    built-in adapter, so that an adapter that sets it reaches the code."""
    from langadapter.adapter import LangAdapter
    from langadapter.java.adapter import JavaAdapter
    from langadapter.python.adapter import PythonAdapter

    for entry in hooks.table():
        for name in entry.methods:
            assert any(hasattr(cls, name) for cls in (LangAdapter, PythonAdapter, JavaAdapter)), (
                entry.name, name)


def test_lookups_name_what_exists():
    assert hooks.hook("run_suite").family == "run tests"
    assert hooks.family("load repair").title == "Repair loading"
    with pytest.raises(KeyError, match="run_suite"):
        hooks.hook("run_tests")
    with pytest.raises(KeyError, match="run tests"):
        hooks.family("running")


def test_the_json_form_has_its_schema_and_every_record():
    data = hooks.as_json()
    assert data["schema"] == hooks.SCHEMA == 1
    assert [h["name"] for h in data["hooks"]] == [h.name for h in hooks.table()]
    assert {"title", "enables", "default", "works_by_default", "step", "guide"} <= set(data["hooks"][0])
    # Each entry names the methods that provide it, as the table does.
    assert [h["methods"] for h in data["hooks"]] == [list(h.methods) for h in hooks.table()]
    # The tree under test, not an installed copy.
    source = str(Path(hooks.__file__).resolve().parents[1])
    printed = subprocess.run([sys.executable, "-m", "langadapter.hooks", "--json"],
                             capture_output=True, text=True, check=True,
                             env={**os.environ, "PYTHONPATH": source})
    assert json.loads(printed.stdout) == json.loads(json.dumps(data))


def test_what_the_built_in_adapters_provide():
    from langadapter.java.adapter import JavaAdapter
    from langadapter.python.adapter import PythonAdapter

    for cls in (PythonAdapter, JavaAdapter):
        for name in ("run_suite", "test_units", "open_mutation_session",
                     "prune_collection_breakers", "salvage_syntax", "strip_module_level_skips",
                     "merge_sources", "per_test_evidence", "run_as_delivered", "validity_policy",
                     "benchmark", "support", "execution_kinds", "structural_metrics",
                     "kill_attribution", "import_statement"):
            assert hooks._present(cls, hooks.hook(name)), (cls.__name__, name)
    assert hooks._present(PythonAdapter, hooks.hook("code_smells"))
    assert not hooks._present(JavaAdapter, hooks.hook("code_smells"))
    assert hooks._present(PythonAdapter, hooks.hook("prepare_suite"))
    assert not hooks._present(JavaAdapter, hooks.hook("prepare_suite"))


def test_the_toy_example_provides_its_facts_and_suite_runs():
    toy = _toy()
    present = {entry.name for entry in hooks.table() if hooks._present(toy, entry)}
    assert present == {"language", "file_extensions", "display_name", "runtime", "test_file_name",
                        "test_framework", "code_fence", "provides_timeout_journal", "run_suite",
                        "language_facts", "capabilities"}


def test_a_hook_langadapter_defines_is_called_as_its_own_method_takes_it():
    """A hook with a LangAdapter method is called with that method's
    parameters, then its keyword-only ones, so a new option there reaches
    the signature check. The hooks called on the class are its class methods."""
    import inspect

    from langadapter.adapter import LangAdapter
    based = [name for name in hooks._CALLS if inspect.getattr_static(LangAdapter, name, None)]
    assert len(based) == 41
    assert hooks._CALLS["project_test_files"] == ((), ())
    assert hooks._CALLS["call_statistics"] == (("source",), ())
    assert hooks._CALLS["profile_suite"] == (("request",), ())
    assert hooks._CALLS["request_options"] == (("purpose",), (
        "target", "traceback", "stop_at_first_failure", "record_leaks", "session_seed",
        "reverse_order", "leave_out_timing_plugins"))
    assert hooks._CALLS["setup_questions"] == (("project_dir", "probe"), ("consumer",))
    assert hooks._CALLS["test_vocabulary"] == ((), ())
    assert hooks._CLASS_CALLS == {name for name in based if isinstance(
        inspect.getattr_static(LangAdapter, name), (classmethod, staticmethod))}
