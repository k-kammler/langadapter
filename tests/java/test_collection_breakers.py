"""Java collection-breaker pruning removes the declarations that keep a test
class from compiling or initializing, so its other tests run. The line
mapping and diagnostics are checked offline, the operation with real javac."""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter.java import _sources
from langadapter.outcomes import Outcome

HEAD = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcGeneratedTest {
    @Test
    void positive() {
        assertEquals(1, Calc.sign(5));
    }
"""
TAIL = """
    @Test
    void negative() {
        assertEquals(-1, Calc.sign(-5));
    }
}
"""
KEPT = HEAD + TAIL

DECOMPILES = """\
    @Test
    void decompiles() {
        assertEquals(1, Calc.decompile(5));
    }
"""
MISSING_IS_NULL = """\
    @Test
    void missingIsNull() {
        assertEquals("", "text".missingIsNull(true));
    }
"""
HELPER = """\
    private int helper(int x) {
        return Calc.decompile(x);
    }
"""
USES_HELPER = """\
    @Test
    void usesHelper() {
        assertEquals(1, helper(1));
    }
"""
FIELD = """\
    private final int value = Calc.decompile(2), other = 3;
"""
USES_FIELD = """\
    @Test
    void usesField() {
        assertEquals(2, value);
    }
"""
STATIC_FAILS = """\
    static final int BASE = Integer.parseInt("not a number");
"""
STATIC_VIA_HELPER = """\
    static final int BASE = parse();
"""
PARSE = """\
    static int parse() {
        return Integer.parseInt("not a number");
    }
"""
STATIC_BLOCK = """\
    static {
        Integer.parseInt("not a number");
    }
"""


def broken(*members: str) -> str:
    """``KEPT`` with ``members`` between its two tests, a blank line
    before each: removing them gives ``KEPT`` again."""
    return HEAD + "".join("\n" + member for member in members) + TAIL


def line_of(source: str, text: str) -> int:
    return source[:source.index(text)].count("\n") + 1


# ---- the declarations around javac's lines ------------------------------------

def test_each_line_names_the_smallest_declaration_around_it():
    source = broken(DECOMPILES, FIELD, STATIC_BLOCK)
    lines = [line_of(source, "Calc.decompile(5)"), line_of(source, "other = 3"),
             line_of(source, "Integer.parseInt")]
    found = _sources._breaking_declarations(source, lines)
    assert [label for label, _ in found] == [
        "CalcGeneratedTest.decompiles()", "CalcGeneratedTest.value, other",
        "CalcGeneratedTest.<static initializer>"]
    assert _sources._cut(source, [(n.start_byte, n.end_byte) for _, n in found]) == KEPT


def test_an_import_and_a_method_with_parameters_are_named():
    source = KEPT.replace("import org.junit.jupiter.api.Test;\n",
                          "import org.junit.jupiter.api.Test;\nimport static org.nowhere.Util.helper;\n")
    source = source.replace(TAIL, "\n" + HELPER + TAIL)
    found = _sources._breaking_declarations(
        source, [line_of(source, "org.nowhere"), line_of(source, "Calc.decompile(x)")])
    assert [label for label, _ in found] == [
        "import org.nowhere.Util.helper", "CalcGeneratedTest.helper(int)"]


def test_a_member_of_a_nested_type_is_removed_and_not_the_type():
    nested = ("    @org.junit.jupiter.api.Nested\n    class Inner {\n"
              "        @Test\n        void kept() {\n            assertEquals(1, Calc.sign(1));\n        }\n"
              "        @Test\n        void broken() {\n            assertEquals(1, Calc.decompile(1));\n        }\n"
              "    }\n")
    source = broken(nested)
    found = _sources._breaking_declarations(source, [line_of(source, "Calc.decompile(1)")])
    assert [label for label, _ in found] == ["CalcGeneratedTest.Inner.broken()"]
    header = source.replace("class Inner {", "class Inner extends Missing {")
    found = _sources._breaking_declarations(header, [line_of(header, "extends Missing")])
    assert [label for label, _ in found] == ["CalcGeneratedTest.Inner"]


def test_a_declaration_inside_a_chosen_one_goes_with_it():
    nested = ("    class Inner extends Missing {\n"
              "        void broken() {\n            Calc.decompile(1);\n        }\n    }\n")
    source = broken(nested)
    found = _sources._breaking_declarations(
        source, [line_of(source, "extends Missing"), line_of(source, "Calc.decompile(1)")])
    assert [label for label, _ in found] == ["CalcGeneratedTest.Inner"]
    assert _sources._cut(source, [(n.start_byte, n.end_byte) for _, n in found]) == KEPT


@pytest.mark.parametrize("text", ["package demo;", "class CalcGeneratedTest {"])
def test_a_line_outside_every_declaration_that_can_be_removed_is_none(text):
    assert _sources._breaking_declarations(KEPT, [line_of(KEPT, text)]) is None


def test_an_enum_constant_goes_with_its_enum():
    # Removing one constant alone could leave the list without its comma.
    source = broken("    enum Color { RED, GREEN }\n")
    found = _sources._breaking_declarations(source, [line_of(source, "RED")])
    assert [label for label, _ in found] == ["CalcGeneratedTest.Color"]
    top = "package demo;\n\nenum Color {\n    RED,\n    GREEN\n}\n"
    assert _sources._breaking_declarations(top, [4]) is None


def test_lines_are_numbered_as_javac_numbers_them():
    # A carriage return alone ends a line for javac, not a tree-sitter row.
    source = broken(DECOMPILES).replace("package demo;\n\n", "package demo;\r\r")
    found = _sources._breaking_declarations(source, [line_of(source.replace("\r", "\n"),
                                                             "Calc.decompile(5)")])
    assert [label for label, _ in found] == ["CalcGeneratedTest.decompiles()"]


def test_only_javac_errors_count_and_in_every_locale():
    raw = ("CalcGeneratedTest.java:13:29: compiler.err.cant.resolve.location.args: x\n"
           "CalcGeneratedTest.java:20:9: compiler.warn.has.been.deprecated.for.removal: y\n"
           "Other.java:4:1: compiler.err.cant.resolve: z\n"
           "C:\\work\\CalcGeneratedTest.java:7: Fehler: Symbol nicht gefunden\n"
           "- compiler.note.removal.filename: CalcGeneratedTest.java\n1 error\n")
    operations = _sources.JavaSources.__new__(_sources.JavaSources)
    compilation = SimpleNamespace(outcome=Outcome.COLLECTION_FAILED, diagnostics=raw)
    operations._compiled = lambda source, options=(): (
        ("CalcGeneratedTest.java", compilation)
        if options == (_sources.RAW_DIAGNOSTICS,) else None)
    assert operations._compilation(KEPT) == (False, [13], raw)


def test_static_initialization_frames_name_their_lines():
    trace = ("java.lang.ExceptionInInitializerError\n"
             "Caused by: java.lang.NumberFormatException: For input string\n"
             "\tat demo.CalcGeneratedTest.parse(CalcGeneratedTest.java:30)\n"
             "\tat demo.CalcGeneratedTest.<clinit>(CalcGeneratedTest.java:8)\n"
             "\tat demo.Other.<clinit>(Other.java:3)\n")
    assert _sources._initializer_lines("CalcGeneratedTest.java", [trace, ""]) == [8]


@pytest.mark.parametrize("member, throws", [
    ("    static final int A = 1, B = -2;\n    static final String S = \"s\";\n"
     "    static final Class<?> C = CalcGeneratedTest.class;\n    static final Object N = null;\n", False),
    ("    int instance = Calc.sign(1);\n", False),
    ("    static class Inner {\n        static int value = Calc.sign(1);\n    }\n", False),
    (STATIC_FAILS, True),
    (STATIC_BLOCK, True),
    ("    static final int[] VALUES = {1, 2};\n", True),
])
def test_only_a_static_initialization_that_could_throw_needs_a_run(member, throws):
    assert _sources._static_initialization_can_throw(broken(member)) is throws


def _offline(compiles=True):
    """Source operations whose compiler and run are doubles: ``runs``
    counts the runs."""
    operations = _sources.JavaSources.__new__(_sources.JavaSources)
    operations.runs = 0
    operations._refusal = lambda source, label: None
    operations._compilation = lambda source: (compiles, [], "")

    def run_check(source, timeout_s):
        operations.runs += 1
        return "runs", "", []
    operations._run_check = run_check
    return operations


def test_a_class_that_compiles_is_unchanged_without_a_run():
    operations = _offline()
    result = operations.prune_collection_breakers(KEPT, timeout_s=300)
    assert (result.status, result.source, operations.runs) == ("unchanged", KEPT, 0)
    result = operations.prune_collection_breakers(broken(STATIC_FAILS), timeout_s=300)
    assert (result.status, operations.runs) == ("unchanged", 1)


# ---- the operation with real javac ------------------------------------------

def _adapter(project, jdks, maven, feature):
    if feature not in jdks:
        pytest.skip(f"needs a JDK {feature}")
    java = jdks[feature] / "bin" / ("java.exe" if os.name == "nt" else "java")
    return lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven}))


@pytest.fixture
def adapter(java_project, jdks, maven):
    return _adapter(java_project("maven"), jdks, maven, 21)


@pytest.mark.parametrize("feature", [11, 17, 21, 25, 26, 27])
def test_the_members_javac_rejects_and_a_failing_initialization_are_removed(
        java_project, jdks, maven, feature):
    # Calls of missing methods, a helper whose removal breaks its caller,
    # and a static field that throws through a helper that stays.
    adapter = _adapter(java_project("maven"), jdks, maven, feature)
    source = broken(STATIC_VIA_HELPER, PARSE, DECOMPILES, MISSING_IS_NULL, HELPER, USES_HELPER)
    result = adapter.prune_collection_breakers(source, timeout_s=300)
    assert result.status == "applied", result.detail
    assert result.removed == (
        "CalcGeneratedTest.decompiles()", "CalcGeneratedTest.missingIsNull()",
        "CalcGeneratedTest.helper(int)", "CalcGeneratedTest.usesHelper()",
        "CalcGeneratedTest.BASE")
    assert result.source == broken(PARSE)
    assert adapter.prune_collection_breakers(result.source, timeout_s=300).status == "unchanged"


def test_a_fold_of_two_classes_keeps_the_members_that_compile(adapter):
    # Merged members follow the separator comment, renamed where they
    # collide: only the one javac rejects goes.
    second = ("    // --- merged ---\n"
              "    @Test\n    void positive_2() {\n        assertEquals(1, Calc.sign(9));\n    }\n\n"
              "    @Test\n    void decompiles_2() {\n        assertEquals(1, Calc.decompile(9));\n    }\n")
    result = adapter.prune_collection_breakers(KEPT.replace(TAIL, "\n" + second + TAIL), timeout_s=300)
    assert result.status == "applied", result.detail
    assert result.removed == ("CalcGeneratedTest.decompiles_2()",)
    assert "positive_2()" in result.source and "// --- merged ---" in result.source


def test_a_class_that_compiles_and_runs_is_unchanged(adapter):
    result = adapter.prune_collection_breakers(KEPT, timeout_s=300)
    assert (result.status, result.source, result.removed) == ("unchanged", KEPT, ())


def test_a_field_and_its_users_and_a_static_block_are_removed(adapter):
    result = adapter.prune_collection_breakers(
        broken(FIELD, USES_FIELD, STATIC_BLOCK, STATIC_FAILS), timeout_s=300)
    assert result.status == "applied", result.detail
    assert result.removed == ("CalcGeneratedTest.value, other", "CalcGeneratedTest.usesField()",
                              "CalcGeneratedTest.<static initializer>", "CalcGeneratedTest.BASE")
    assert result.source == KEPT


def test_an_unresolvable_import_and_its_use_are_removed(adapter):
    source = broken("    @Test\n    void usesMissing() {\n        new Missing();\n    }\n").replace(
        "import org.junit.jupiter.api.Test;\n",
        "import org.junit.jupiter.api.Test;\nimport org.example.Missing;\n")
    result = adapter.prune_collection_breakers(source, timeout_s=300)
    assert result.status == "applied", result.detail
    assert result.removed == ("import org.example.Missing", "CalcGeneratedTest.usesMissing()")
    assert result.source == KEPT


def test_an_error_flow_analysis_finds_later_is_removed(adapter):
    uninitialized = "    @Test\n    void uninitialized() {\n        int x;\n        assertEquals(1, x);\n    }\n"
    result = adapter.prune_collection_breakers(broken(DECOMPILES, uninitialized), timeout_s=300)
    assert result.status == "applied", result.detail
    assert set(result.removed) == {"CalcGeneratedTest.decompiles()",
                                   "CalcGeneratedTest.uninitialized()"}
    assert result.source == KEPT


def test_an_error_in_the_class_header_is_not_repaired(adapter):
    source = broken(DECOMPILES).replace("class CalcGeneratedTest {",
                                        "class CalcGeneratedTest extends MissingBase {")
    result = adapter.prune_collection_breakers(source, timeout_s=300)
    assert result.status == "not_repaired"
    assert "no declaration that can be removed" in result.detail
    assert (result.source, result.removed) == (source, ())


def test_a_class_left_without_tests_is_not_repaired(adapter):
    source = HEAD.replace("Calc.sign(5)", "Calc.decompile(5)") + "}\n"
    result = adapter.prune_collection_breakers(source, timeout_s=300)
    assert result.status == "not_repaired"
    assert "no test is left" in result.detail
    assert result.source == source


def test_the_removal_stops_after_five_rounds(adapter):
    # Each helper calls the one before, so each round breaks the next one.
    chain = ["    int h1() {\n        return Calc.decompile(1);\n    }\n"] + [
        f"    int h{n}() {{\n        return h{n - 1}();\n    }}\n" for n in range(2, 8)]
    source = broken(*chain)
    result = adapter.prune_collection_breakers(source, timeout_s=300)
    assert result.status == "not_repaired"
    assert "still after 5 rounds of removal" in result.detail
    assert result.source == source
    assert adapter.prune_collection_breakers(broken(*chain[:5]), timeout_s=300).status == "applied"
