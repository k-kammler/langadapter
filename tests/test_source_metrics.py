"""Public structural metrics: an empty source and invalid syntax told apart, and
the counts of known sources."""
import subprocess
import sys
from pathlib import Path

import pytest

import langadapter as lb


FIELDS = {
    "cyclomatic_sum", "loop_count", "loop_max_nesting",
    "recursive_function_count", "function_count", "class_count",
    "io_call_count", "nloc",
}


@pytest.fixture(params=[
    ("python", "def f():\n    return 1\n", "def broken(:\n    pass\n"),
    ("java", "class Calc { int f() { return 1; } }\n", "class Broken { void f( }\n"),
], ids=["python", "java"])
def sources(request, tmp_path, monkeypatch):
    language, valid, broken = request.param

    def refuse(*args, **kwargs):
        pytest.fail("source metrics must not execute the project")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable))
    return lb.create(language, context), valid, broken


def test_invalid_source_has_the_same_keys_with_unknown_counts(sources):
    adapter, valid, broken = sources
    for source in (broken, valid + broken):
        assert not adapter.check_structure(source).ok
        result = adapter.optimization_metrics(source)
        assert set(result) == FIELDS
        assert all(value is None for value in result.values())


def test_empty_source_still_has_observed_zero_counts(sources):
    adapter, _valid, _broken = sources
    result = adapter.optimization_metrics("")
    assert set(result) == FIELDS
    assert all(value == 0 for value in result.values())


def test_valid_source_keeps_observed_counts(sources):
    adapter, valid, _broken = sources
    result = adapter.optimization_metrics(valid)
    assert set(result) == FIELDS
    assert result["function_count"] == 1
    assert result["cyclomatic_sum"] == 1
    assert result["nloc"] > 0
    assert all(type(value) is int for value in result.values())


def _adapter(language):
    """An adapter for metrics alone: they read the source and run nothing."""
    return lb.create(language, lb.ProjectContext(str(Path(__file__).resolve().parent),
                                                 lb.Interpreter(path=sys.executable)))


PY = _adapter("python")


@pytest.mark.parametrize("lang", ["python", "java"])
def test_optimization_metrics_returns_full_shape_for_every_language(lang):
    """Every adapter gives every metric, 0 for an empty source."""
    m = _adapter(lang).optimization_metrics("")
    assert set(m) == FIELDS
    for k in m:
        assert m[k] == 0


def test_optimization_metrics_python_pins_counts_on_known_fixture():
    """Hand-counted on a fixture with a recursive function, a doubly nested
    loop and a trivial helper."""
    src = (
        "import functools\n"
        "\n"
        "def fib(n):\n"
        "    if n < 2:\n"
        "        return n\n"
        "    return fib(n - 1) + fib(n - 2)\n"
        "\n"
        "def busy(xs):\n"
        "    out = []\n"
        "    for x in xs:\n"
        "        for y in xs:\n"
        "            if x == y:\n"
        "                out.append((x, y))\n"
        "    return out\n"
        "\n"
        "def simple():\n"
        "    return 1\n"
    )
    m = PY.optimization_metrics(src)
    assert m["recursive_function_count"] == 1
    assert m["loop_count"] == 2
    assert m["loop_max_nesting"] == 2
    assert m["cyclomatic_sum"] == 7
    # NLOC: 14 non-blank, non-comment-only lines in the fixture.
    assert m["nloc"] == 14


def test_optimization_metrics_io_call_count_matches_specific_names_only():
    """io_call_count counts calls whose last name is in the IO vocabulary
    (file, socket and subprocess primitives), not generic names such as
    ``dict.get``."""
    src = (
        "import socket, subprocess\n"
        "def f(p):\n"
        "    s = socket.socket()\n"   # socket
        "    s.connect(p)\n"          # connect
        "    data = open('x').read()\n"   # open (read is NOT in the vocab)
        "    d = {}\n"
        "    d.get('k')\n"            # dict.get -- excluded
        "    return subprocess.Popen(['ls'])\n"  # Popen
    )
    m = PY.optimization_metrics(src)
    assert m["io_call_count"] == 4  # socket, connect, open, Popen


def test_optimization_metrics_io_call_count_zero_without_io():
    """A pure-compute function has io_call_count 0."""
    m = PY.optimization_metrics(
        "def f(xs):\n    return sum(x * x for x in xs)\n"
    )
    assert m["io_call_count"] == 0


def test_optimization_metrics_nloc_excludes_blank_and_comment_lines():
    """NLOC counts lines with a non-comment token: blank and comment-only
    lines are left out, a line with a trailing comment counts once."""
    src = (
        "import os\n"      # code (1)
        "\n"               # blank
        "# a comment\n"    # comment-only
        "def f(x):\n"      # code (2)
        "    y = x + 1  # trailing comment\n"  # code + trailing comment (3)
        "    return y\n"   # code (4)
    )
    m = PY.optimization_metrics(src)
    assert m["nloc"] == 4


def test_optimization_metrics_python_counts_comprehension_loops():
    """A comprehension or generator ``for`` is a loop: ``[y for x in xs for
    y in x]`` counts two, as the statement form does."""
    comp = PY.optimization_metrics(
        "def f(xs):\n    return [y for x in xs for y in x]\n"
    )
    explicit = PY.optimization_metrics(
        "def f(xs):\n    out = []\n    for x in xs:\n"
        "        for y in x:\n            out.append(y)\n    return out\n"
    )
    assert comp["loop_count"] == 2
    assert explicit["loop_count"] == 2
    # A comprehension inside a statement loop is nested in it.
    nested = PY.optimization_metrics(
        "def f(xs):\n    for x in xs:\n        z = [y for y in x]\n    return z\n"
    )
    assert nested["loop_count"] == 2
    assert nested["loop_max_nesting"] == 2


def test_optimization_metrics_python_excludes_nested_function_branches():
    """A nested ``def``'s decisions count for it alone: an outer ``if``
    around an inner function with its own ``if`` sums to 4 (two functions,
    one decision each), not 5."""
    src = (
        "def outer(x):\n"
        "    if x:\n"
        "        def inner(y):\n"
        "            if y:\n"
        "                return 1\n"
        "        return inner\n"
    )
    m = PY.optimization_metrics(src)
    assert m["cyclomatic_sum"] == 4
    assert m["nloc"] == 6


def test_optimization_metrics_python_counts_boolean_operators():
    """Each ``and`` and ``or`` adds a McCabe path: ``if x and y or z`` is
    1 (base) + 1 (if) + 1 (and) + 1 (or) = 4."""
    m = PY.optimization_metrics(
        "def f(x, y, z):\n    if x and y or z:\n        return 1\n    return 0\n"
    )
    assert m["cyclomatic_sum"] == 4


def test_optimization_metrics_python_recursive_via_method_call():
    """``self.step()`` inside ``def step`` counts as recursive: a callee is
    matched by the last part of its dotted name."""
    src = (
        "class A:\n"
        "    def step(self, n):\n"
        "        if n <= 0: return 0\n"
        "        return self.step(n - 1) + 1\n"
    )
    m = PY.optimization_metrics(src)
    assert m["recursive_function_count"] == 1


def test_optimization_metrics_loop_max_nesting_triple_depth():
    """Three nested loops have max-nesting 3."""
    src = (
        "def t(a, b, c):\n"
        "    for x in a:\n"
        "        for y in b:\n"
        "            for z in c:\n"
        "                pass\n"
    )
    m = PY.optimization_metrics(src)
    assert m["loop_count"] == 3
    assert m["loop_max_nesting"] == 3


def test_optimization_metrics_loop_max_nesting_with_siblings():
    """Two sibling loops, the first with a nested loop: max-nesting 2 and
    count 3, so siblings add no depth."""
    src = (
        "def t(a):\n"
        "    for x in a:\n"
        "        for y in a:\n"
        "            pass\n"
        "    for z in a:\n"
        "        pass\n"
    )
    m = PY.optimization_metrics(src)
    assert m["loop_count"] == 3
    assert m["loop_max_nesting"] == 2
