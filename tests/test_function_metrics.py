"""Native grammar vocabularies share the scoped structural counter mechanism."""
from dataclasses import FrozenInstanceError

import pytest

from langadapter import _treesitter as ts
from langadapter.java import _sources as java
from langadapter.python._sources import PythonSources as Python


def binding(language):
    if language == "python":
        return Python._TS_GRAMMAR, dict(
            func_kinds=Python._TS_FUNC_KINDS, loop_kinds=Python._TS_LOOP_KINDS,
            call_kinds=Python._TS_CALL_KINDS, lizard_suffix=Python._LIZARD_SUFFIX,
            class_kinds=Python._TS_CLASS_KINDS,
            io_call_names=Python._IO_CALL_NAMES), "def broken(:"
    return java.GRAMMAR, dict(
        func_kinds=java.FUNC_KINDS, loop_kinds=java.LOOP_KINDS,
        call_kinds=java.CALL_KINDS, lizard_suffix=".java", class_kinds=java.CLASS_KINDS,
        io_call_names=java.IO_CALL_NAMES), "class {"


@pytest.fixture(params=["python", "java"])
def grammar(request):
    return binding(request.param)


def scoped(grammar, source, **kwargs):
    module, options, _ = grammar
    return ts.scoped_optimization_metrics(module, source, **options, **kwargs)


def test_empty_source_is_zero_with_no_function_scopes(grammar):
    result = scoped(grammar, "")
    assert result is not None and result.functions == ()
    assert len(result.file) == 8 and all(value == 0 for _, value in result.file)


def test_parse_failure_is_not_an_empty_success(grammar):
    assert scoped(grammar, grammar[2]) is None


def test_comments_and_module_class_code_are_not_invented_functions(grammar):
    source = ("# comment\nvalue = 1\n" if grammar[0].endswith("python")
              else "// comment\nclass C { int value = 1; }\n")
    result = scoped(grammar, source)
    assert result.functions == ()
    assert dict(result.file)["nloc"] == 1


def test_python_nested_repeated_and_class_definitions_keep_original_spans():
    grammar = binding("python")
    source = '''
def outer(x):
    def inner(y):
        return y if y else 0
    if x:
        return inner(x)
    return 0

class C:
    def f(self):
        return 1
    def f(self):
        return 2
'''
    result = scoped(grammar, source)
    assert [(f.name, f.start_line, f.end_line) for f in result.functions] == [
        ("outer", 2, 7), ("outer.inner", 3, 4), ("C.f", 10, 11), ("C.f", 12, 13)]
    assert [f.signature for f in result.functions] == [None] * 4
    outer, inner, _, _ = result.functions
    assert outer.cyclomatic_complexity == inner.cyclomatic_complexity == 2
    assert dict(outer.counts)["cyclomatic_sum"] == 4
    assert dict(outer.counts)["function_count"] == 2
    assert dict(result.file)["cyclomatic_sum"] == 6


def test_java_overloads_constructors_and_local_classes_keep_native_signatures():
    grammar = binding("java")
    source = '''class C {
    C() {}
    C(int n) {}
    int f(int n) { return n; }
    int f(String s) { return s.length(); }
    int outer() {
        class Local { int inner() { return 1; } }
        return 0;
    }
}'''
    result = scoped(grammar, source, signature=java._signature)
    assert [(f.name, f.signature) for f in result.functions] == [
        ("C.C", ""), ("C.C", "int"), ("C.f", "int"), ("C.f", "String"),
        ("C.outer", ""), ("C.outer.Local.inner", "")]
    assert dict(result.functions[4].counts)["class_count"] == 1
    assert dict(result.functions[4].counts)["function_count"] == 2
    assert result.functions[4].cyclomatic_complexity == 1


def test_same_line_occurrences_are_retained_not_collapsed():
    grammar = binding("java")
    result = scoped(grammar, "class C { int f() { return 1; } int f() { return 2; } }")
    assert len(result.functions) == 2
    first, second = result.functions
    assert (first.name, first.start_line, first.end_line, first.signature) == (
        second.name, second.start_line, second.end_line, second.signature)
    assert first.start_column != second.start_column


def test_unicode_names_and_original_utf8_columns_are_retained():
    source = "class Caf\u00e9 { int f() { return 1; } }"
    result = scoped(binding("java"), source)
    assert result.functions[0].name == "Caf\u00e9.f"
    assert result.functions[0].start_column == len("class Caf\u00e9 { ".encode("utf-8")) + 1


def test_boolean_loop_recursion_io_and_code_line_counts_use_existing_vocabulary(grammar):
    source = ('''def f(n):
    # comment
    for x in n:
        while x and n:
            open("x")
            f(n)
    return n
''' if grammar[0].endswith("python") else '''class C {
    int f(int n) {
        // comment
        for (int x = 0; x < n; x++) {
            while (x > 0 && n > 0) {
                openStream();
                f(n);
            }
        }
        return n;
    }
}''')
    result = scoped(grammar, source)
    counts = dict(result.functions[0].counts)
    assert counts["loop_count"] == counts["loop_max_nesting"] == 2
    assert counts["recursive_function_count"] == counts["io_call_count"] == 1
    assert counts["cyclomatic_sum"] == result.functions[0].cyclomatic_complexity == 4
    assert counts["nloc"] == (6 if grammar[0].endswith("python") else 9)
    assert dict(result.file) == ts.optimization_metrics(grammar[0], source, **grammar[1])


def test_lizard_counts_the_lines_of_code_and_each_functions_own_complexity(grammar):
    """lizard's counts: a docstring is no line of code, a nested function's
    decisions count toward it alone, and a declaration without a body, in
    which lizard finds no function, has no complexity and adds none."""
    python = grammar[0].endswith("python")
    source = ('''def outer(x):
    """Two lines
    of docstring."""
    def inner(y):
        return y or 0
    return inner(x) if x else 0
''' if python else '''abstract class C {
    /** Two lines
        of comment. */
    abstract int declared(int n);
    int outer(int x) {
        Runnable r = new Runnable() { public void run() { if (x > 0 || x < -1) {} } };
        return x > 0 ? x : 0;
    }
}''')
    result = scoped(grammar, source)
    own = {f.name: f.cyclomatic_complexity for f in result.functions}
    if python:
        assert own == {"outer": 2, "outer.inner": 2}
        assert dict(result.functions[0].counts)["nloc"] == 4
        assert dict(result.file)["nloc"] == 4
    else:
        assert own == {"C.declared": None, "C.outer": 2, "C.outer.run": 3}
        assert dict(result.functions[1].counts)["cyclomatic_sum"] == 5
        assert dict(result.file)["nloc"] == 7
    assert dict(result.file)["cyclomatic_sum"] == sum(v for v in own.values() if v is not None)


#: The shape of dacite's data.py: a Protocol whose methods are
#: stubs, in which lizard counts no function.
STUBS = """from typing import Any, Protocol


class Data(Protocol):
    def keys(self) -> Any: ...
    def __getitem__(self, *args, **kwargs) -> Any: ...
    def __contains__(self, *args, **kwargs) -> bool: ...
"""


def test_the_file_complexity_is_lizard_s_own_sum_and_a_stub_has_none():
    """A stub, in which lizard counts no function, adds nothing to the
    file's complexity, which is lizard's own sum, and has no complexity of
    its own; it still counts as a function."""
    from lizard import analyze_file

    result = scoped(binding("python"), STUBS)
    assert analyze_file.analyze_source_code("data.py", STUBS).function_list == []
    assert dict(result.file)["cyclomatic_sum"] == 0
    assert [f.cyclomatic_complexity for f in result.functions] == [None, None, None]
    assert dict(result.file)["function_count"] == 3


def test_a_declaration_takes_only_the_function_lizard_reports_for_it():
    """A declaration's complexity is the one lizard reports under its own
    name, never a nested function's: lizard reads a Python def whose body
    is on its signature's line as no function, and loses the function that
    holds one, so both have none. A normal nested function and an annotated
    Java method keep their own."""
    python, java = binding("python"), binding("java")
    one_line = "def g(x):\n    def h(y): return 1 if y else 2\n    return h(x)\n"
    own = {f.name: f.cyclomatic_complexity for f in scoped(python, one_line).functions}
    assert own == {"g": None, "g.h": None}
    assert dict(scoped(python, one_line).file)["cyclomatic_sum"] == 2
    alone = "def f(x): return 1 if x else 2\n"
    assert [f.cyclomatic_complexity for f in scoped(python, alone).functions] == [None]
    nested = "def g(x):\n    def h():\n        return 1\n    return 2 if x else h()\n"
    own = {f.name: f.cyclomatic_complexity for f in scoped(python, nested).functions}
    assert own == {"g": 2, "g.h": 1}
    annotated = "class C {\n    @Test\n    void t(int x) { if (x > 0) {} }\n}"
    assert [f.cyclomatic_complexity for f in scoped(java, annotated).functions] == [2]


def test_function_snapshot_is_frozen_and_declaration_is_not_reparsed(grammar, monkeypatch):
    source = "def f():\n    return 1\n" if grammar[0].endswith("python") else "class C { int f() { return 1; } }"
    real = ts.parse
    calls = []

    def parse(module, text):
        calls.append(text)
        return real(module, text)

    monkeypatch.setattr(ts, "parse", parse)
    result = scoped(grammar, source)
    assert calls == [source]
    with pytest.raises(FrozenInstanceError):
        result.functions[0].name = "changed"
    assert isinstance(result.file, tuple) and isinstance(result.functions[0].counts, tuple)


def test_the_lines_of_code_and_complexities_are_counted_for_any_suffix():
    """langadapter.sources.code_lines_and_complexities counts a source of a
    language lizard reads by its file name's suffix, here C: a comment is no
    line of code, and each function has its own complexity, in order."""
    from langadapter.sources import code_lines_and_complexities

    source = ("/* two lines\n   of comment */\n"
              "int sign(int x) {\n    if (x > 0) return 1;\n    return x < 0 ? -1 : 0;\n}\n"
              "int twice(int x) { return 2 * x; }\n")
    assert code_lines_and_complexities(source, ".c") == (5, (3, 1))
