"""The per-test evidence run's shield with real coverage.py and pytest.
coverage.py flushes a test's data at the context switch after its teardown.
A built-in replacement that outlives the test (a module-scoped fixture's)
would crash coverage.py's bookkeeping there without the shield. A test's
own replacement is undone by then, and a suite replacing nothing is
unchanged."""
from __future__ import annotations

import subprocess
import sys

import pytest

import langadapter as lb
from langadapter.outcomes import Outcome
from langadapter.python import _plugins
from langadapter.python.adapter import PythonAdapter
from langadapter.runs import PerTestEvidenceRequest

TARGET = ("def add(a, b):\n    return a + b\n\n\n"
          "def is_positive(n):\n    return n > 0\n")
PLAIN = ("from arith import add, is_positive\n\n\n"
         "def test_add():\n    assert add(2, 3) == 5\n\n\n"
         "def test_positive():\n    assert is_positive(1)\n")
_REPLACING = {
    "len_negative_for_lists": (
        "def test_replaces_len(monkeypatch):\n"
        "    original_len = builtins.len\n"
        "\n"
        "    def fake_len(obj):\n"
        "        if isinstance(obj, list):\n"
        "            return -1\n"
        "        return original_len(obj)\n"
        "\n"
        "    monkeypatch.setattr(builtins, 'len', fake_len)\n"
        "    assert is_positive(1) is True\n"
    ),
    "range_with_one_argument": (
        "def test_replaces_range(monkeypatch):\n"
        "    def fake_range(n):\n"
        "        assert n == 2\n"
        "        return iter([-1, 0])\n"
        "\n"
        "    monkeypatch.setattr(builtins, 'range', fake_range)\n"
        "    assert is_positive(1) is True\n"
    ),
    "set_with_one_argument": (
        "def test_replaces_set(monkeypatch):\n"
        "    real_set = builtins.set\n"
        "\n"
        "    class CustomSet:\n"
        "        def __init__(self, data):\n"
        "            self.data = real_set(data)\n"
        "\n"
        "    def fake_set(iterable):\n"
        "        return CustomSet(iterable)\n"
        "\n"
        "    monkeypatch.setattr(builtins, 'set', fake_set)\n"
        "    assert is_positive(1) is True\n"
    ),
}


def _project(tmp_path, tests):
    root = tmp_path / "proj"
    (root / "tests").mkdir(parents=True)
    (root / "arith.py").write_text(TARGET, encoding="utf-8")
    (root / "tests" / "test_arith.py").write_text(tests, encoding="utf-8")
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable)))
    return adapter, root, lb.Suite(str(root / "tests"), ("test_arith.py",))


def _evidence(adapter, suite):
    return adapter.per_test_evidence(PerTestEvidenceRequest(
        suite=suite, target="arith.py", timeout_s=120, warmup=False))


def _replacing(body):
    return ("import builtins\n\nfrom arith import add, is_positive\n\n\n"
            "def test_before():\n    assert add(2, 3) == 5\n\n\n"
            + body
            + "\n\ndef test_after():\n    assert is_positive(-1) is False\n")


#: A module-scoped fixture's replacement of ``set``, in force across the
#: context switch between its two tests.
WIDER = ("import builtins\n\nimport pytest\n\nfrom arith import add, is_positive\n\n\n"
         "@pytest.fixture(scope='module')\ndef replaced():\n"
         "    with pytest.MonkeyPatch.context() as patch:\n"
         "        real_set = builtins.set\n\n"
         "        class CustomSet:\n"
         "            def __init__(self, data=()):\n"
         "                self.data = real_set(data)\n\n"
         "        def fake(iterable=()):\n"
         "            return CustomSet(iterable)\n\n"
         "        patch.setattr(builtins, 'set', fake)\n"
         "        yield\n\n\n"
         "def test_one(replaced):\n    assert add(2, 3) == 5\n\n\n"
         "def test_two(replaced):\n    assert is_positive(1) is True\n")


def _passes_plainly(root):
    plain = subprocess.run(
        [sys.executable, "-m", "pytest", str(root / "tests"), "-q", "-p",
         "no:cacheprovider"],
        cwd=root, capture_output=True, text=True, timeout=120)
    assert plain.returncode == 0, plain.stdout[-500:]


def test_without_the_shield_a_replacement_beyond_a_test_has_no_evidence(
        tmp_path, monkeypatch):
    """The suite passes plainly and yields evidence with the shield, but
    fails with an empty module in the shield's place."""
    adapter, root, suite = _project(tmp_path, WIDER)
    _passes_plainly(root)
    assert _evidence(adapter, suite).outcome is Outcome.COMPLETED
    monkeypatch.setattr(_plugins, "_EVICTION_SHIELD_PLUGIN", "")
    assert _evidence(adapter, suite).outcome is not Outcome.COMPLETED


@pytest.mark.parametrize("shape", sorted(_REPLACING))
def test_a_test_s_own_replacement_is_undone_before_its_context_switches(
        tmp_path, monkeypatch, shape):
    """A test's own replacement ends with its teardown, before the switch,
    so it yields evidence with or without the shield."""
    adapter, root, suite = _project(tmp_path, _replacing(_REPLACING[shape]))
    _passes_plainly(root)
    assert _evidence(adapter, suite).outcome is Outcome.COMPLETED
    monkeypatch.setattr(_plugins, "_EVICTION_SHIELD_PLUGIN", "")
    assert _evidence(adapter, suite).outcome is Outcome.COMPLETED


def test_the_shield_changes_nothing_for_a_suite_replacing_nothing(
        tmp_path, monkeypatch):
    adapter, _root, suite = _project(tmp_path, PLAIN)
    shielded = _evidence(adapter, suite)
    monkeypatch.setattr(_plugins, "_EVICTION_SHIELD_PLUGIN", "")
    plain = _evidence(adapter, suite)
    assert shielded.outcome is plain.outcome is Outcome.COMPLETED
    assert {str(t.test): t.tokens() for t in shielded.tests} == {
        str(t.test): t.tokens() for t in plain.tests}
    assert shielded.coverage.covered == plain.coverage.covered
    assert shielded.coverage.taken == plain.coverage.taken
