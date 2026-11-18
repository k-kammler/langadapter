"""Java test-source operations: structure, metrics, static analysis, javac's
parse at the project's release, pruning, normalizing, finalizing, and
collision resolution for merging test classes into one that runs."""
from __future__ import annotations

import os

import pytest

import langadapter as lb
from langadapter.runs import SuiteRunRequest
from langadapter.suites import TestId

SEPARATOR = "// --- merged ---"

FIRST = """\
package demo;

import static java.lang.Math.max;
import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

class CalcTest {
    private int base;

    @BeforeEach
    void setUp() {
        base = 5;
    }

    @Test
    void positive() {
        assertEquals(1, Calc.sign(base));
    }

    static int atLeastOne(int x) {
        return max(x, 1);
    }

    static int twice(int x) {
        return 2 * x;
    }
}
"""

SECOND = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.util.List;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;

class CalcMutationTest {
    private int base;
    private static int teardowns;

    @BeforeEach
    void setUp() {
        base = -5;
    }

    @AfterEach
    void tearDown() {
        teardowns++;
    }

    @Test
    void positive() {
        assertEquals(-1, Calc.sign(this.base));
    }

    @ParameterizedTest
    @MethodSource("values")
    void signs(int v) {
        assertEquals(Integer.signum(v), Calc.sign(v));
    }

    static List<Integer> values() {
        return List.of(-2, 0, 2);
    }

    static int twice(int x) {
        return 2 * x;
    }

    @Test
    void doubles() {
        assertEquals(-10, CalcMutationTest.twice(base));
    }
}
"""

THIRD = """\
package demo;

import static java.lang.Integer.max;
import static org.junit.jupiter.api.Assertions.assertEquals;

import java.awt.List;
import org.junit.jupiter.api.Test;

class CalcExtraTest {
    private static int teardowns;

    static int values() {
        teardowns++;
        return 3;
    }

    static int items(List list) {
        return list.getItemCount();
    }

    @Test
    void positive() {
        assertEquals(1, Calc.sign(max(values(), 0)));
    }
}
"""

DUPLICATES = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.ValueSource;

// a note the pruning leaves behind

class CalcDuplicateTest {
    /** Zero has no sign. */
    @Test
    void zero() {
        assertEquals(1, Calc.sign(0));
    }

    @ParameterizedTest
    @ValueSource(ints = {1, 2, 3})
    void positives(int v) {
        assertEquals(1, Calc.sign(v));
    }

    @Test
    void zero() {
        assertEquals(0, Calc.sign(0));
    }

    @Nested
    class Clamping {
        @Test
        void keepsInside() {
            assertEquals(5, Calc.clamp(5, 0, 10));
            assert Calc.clamp(5, 0, 10) == 5;
        }
    }
}
""".replace("import org.junit.jupiter.api.Test;",
            "import org.junit.jupiter.api.Nested;\nimport org.junit.jupiter.api.Test;")

@pytest.mark.parametrize(("source", "cases"), [
    ('@CsvSource(delimiter = \'|\', value = {"a|1", "b|2"})', 2),
    ('@CsvSource(nullValues = {"NIL", "N/A"}, value = {"a,1"})', 1),
    ('@CsvSource(textBlock = """\n        # name, size\n        a, 1\n\n        b, 2\n        """)', 2),
    ('@ValueSource(strings = "x")', 1),
])
def test_a_parameter_source_counts_its_cases(source, cases):
    """The cases are the value array's elements or a text block's rows,
    whatever other attributes come before them."""
    from langadapter.java.source_analysis import static_operations
    text = ("package demo;\n\nclass CasesTest {\n    @ParameterizedTest\n    " + source
            + "\n    void cases(String a) {\n    }\n}\n")
    assert static_operations().test_metrics(text).parametrized_cases == cases


RECORD = "package demo;\n\nclass RecordTest {\n    record Pair(int a, int b) {\n    }\n}\n"


def _adapter(project, jdks, maven, gradle, feature=21):
    if feature not in jdks:
        pytest.skip(f"needs a JDK {feature}")
    java = jdks[feature] / "bin" / ("java.exe" if os.name == "nt" else "java")
    return lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven, "LANGADAPTER_GRADLE": gradle}))


def _run(adapter, project, source):
    (project / "src/test/java/demo/CalcTest.java").write_text(
        source, encoding="utf-8", newline="\n")
    return adapter.run_suite(SuiteRunRequest(
        suite=lb.Suite(str(project / "src" / "test" / "java"),
                       ("demo/CalcTest.java",)), timeout_s=300))


@pytest.fixture
def adapter(java_project, jdks, maven, gradle):
    project = java_project("maven")
    return _adapter(project, jdks, maven, gradle)


def test_structure_metrics_and_static_analysis(adapter):
    parser = adapter.structural_parser_identity()
    assert parser.startswith("tree-sitter ") and "tree-sitter-java" in parser
    assert adapter.check_structure(FIRST).ok
    broken = adapter.check_structure("class X {\n    void f( }\n")
    assert (broken.ok, broken.line) == (False, 2)
    spanning = adapter.check_structure("class X {\n    void f( int a,\n        int b }\n}\n")
    assert (spanning.message, spanning.snippet) == (
        "syntax error at line 2:5", "void f( int a,\n        int b }")
    metrics = adapter.test_metrics(DUPLICATES)
    assert (metrics.parsed, metrics.tests, metrics.fixtures,
            metrics.parametrized_cases, metrics.assertions) == (True, 4, 0, 3, 5)
    assert not adapter.test_metrics("class X {").parsed
    assert adapter.skeleton(DUPLICATES) == (
        "CalcDuplicateTest", "CalcDuplicateTest.zero",
        "CalcDuplicateTest.positives", "CalcDuplicateTest.zero",
        "CalcDuplicateTest.Clamping", "CalcDuplicateTest.Clamping.keepsInside")
    same, reason = adapter.same_skeleton(FIRST, SECOND)
    assert not same and "removed" in reason
    assert set(adapter.function_fingerprints(FIRST)) == {
        "CalcTest.setUp", "CalcTest.positive", "CalcTest.atLeastOne",
        "CalcTest.twice"}
    assert adapter.optimization_metrics(FIRST)["function_count"] == 4
    assert adapter.imports(SECOND)[:2] == (
        "org.junit.jupiter.api.Assertions.assertEquals", "java.util.List")
    assert adapter.import_path_candidates(
        "org.junit.jupiter.api.Assertions.assertEquals") == (
        "org/junit/jupiter/api/Assertions.java",)
    assert adapter.import_path_candidates("java.util.*") == ()
    assert adapter.is_test_file("src/test/java/demo/CalcTest.java")
    assert not adapter.is_test_file("src/main/java/demo/Calc.java")
    assert adapter.duplicate_test_names(DUPLICATES) == ["CalcDuplicateTest.zero"]
    assert adapter.merge_collisions(FIRST, SECOND) == (
        "base", "positive", "setUp", "twice")


def test_javac_parses_at_the_projects_release(java_project, jdks, maven, gradle):
    old = _adapter(java_project("maven", release=11), jdks, maven, gradle)
    valid = old.validate_source(FIRST)
    assert valid.valid and valid.interpreter.endswith("--release 11")
    error = old.validate_source("package demo;\nclass X { void f( }\n")
    assert (error.status, error.line) == ("syntax_error", 2)
    assert old.validate_source(RECORD).status == "syntax_error"
    new = _adapter(java_project("gradle", release=17), jdks, maven, gradle)
    assert new.validate_source(RECORD).valid


def test_pruning_normalizing_and_finalizing(adapter):
    normalized = adapter.normalize_shadowed(DUPLICATES)
    assert normalized.status == "applied"
    assert normalized.removed == ("CalcDuplicateTest.zero",)
    assert "assertEquals(1, Calc.sign(0))" not in normalized.source
    assert adapter.duplicate_test_names(normalized.source) == []
    pruned = adapter.prune_tests(normalized.source, [
        TestId("demo/CalcDuplicateTest.java", "CalcDuplicateTest.positives", "[2]"),
        "demo.CalcDuplicateTest.Clamping.keepsInside", "CalcDuplicateTest.missing"])
    assert pruned.status == "applied"
    assert pruned.removed == ("CalcDuplicateTest.positives",
                              "CalcDuplicateTest.Clamping.keepsInside")
    assert pruned.unmatched == ("CalcDuplicateTest.missing",)
    assert pruned.detail == ("CalcDuplicateTest.positives is removed whole: "
                             "JUnit names its cases by their position")
    assert adapter.skeleton(pruned.source) == (
        "CalcDuplicateTest", "CalcDuplicateTest.zero", "CalcDuplicateTest.Clamping")
    finalized = adapter.finalize_source(pruned.source)
    assert finalized.status == "applied"
    assert "Zero has no sign" not in finalized.source
    assert "a note the pruning leaves behind" not in finalized.source
    assert adapter.function_fingerprints(finalized.source) == \
        adapter.function_fingerprints(pruned.source)
    assert adapter.finalize_source(finalized.source).status == "unchanged"
    assert adapter.normalize_shadowed("package demo;\nclass X { void f( }\n"
                                      ).status == "refused"


OVERLOADS = """\
package demo;

import org.junit.jupiter.api.Test;

class ParseTest {
    @Test
    void parse() {
        parse("1");
    }

    void parse(String text) {
        Integer.parseInt(text);
    }

    @Test
    void kept() {
        parse("2");
    }
}
"""

JUNIT3 = """\
package demo;

public class ParseTest extends junit.framework.TestCase {
    public void testParse() {
        testParse("1");
    }

    void testParse(String text) {
        Integer.parseInt(text);
    }
}
"""


def test_pruning_a_test_keeps_a_helper_overload_of_its_name():
    from langadapter.java.source_analysis import static_operations
    operations = static_operations()
    pruned = operations.prune_tests(OVERLOADS, ["ParseTest.parse"])
    assert (pruned.status, pruned.removed) == ("applied", ("ParseTest.parse",))
    assert "void parse(String text)" in pruned.source
    assert "void parse() {" not in pruned.source and "void kept()" in pruned.source
    # A case's id in a string names its method, which goes whole.
    case = operations.prune_tests(OVERLOADS, ["ParseTest.parse[1]"])
    assert (case.removed, case.unmatched) == (("ParseTest.parse",), ())
    assert case.detail == "ParseTest.parse is removed whole: JUnit names its cases by their position"
    pruned = operations.prune_tests(JUNIT3, ["ParseTest.testParse"])
    assert "void testParse(String text)" in pruned.source
    assert "public void testParse() {" not in pruned.source
    helpers = "package demo;\n\nclass H {\n    void f(int a) {\n    }\n\n    void f(long a) {\n    }\n}\n"
    # A name with no test among its overloads removes every overload.
    assert "void f(" not in operations.prune_tests(helpers, ["H.f"]).source


#: A test asserting a text block that holds two blank lines in a row.
BLOCK = '''\
    @Test
    void block() {
        String text = """
            a


            b
            """;
        assertEquals("a\\n\\n\\nb\\n", text);
    }
'''
BLOCKS = ("package demo;\n\nimport static org.junit.jupiter.api.Assertions.assertEquals;\n\n"
          "import org.junit.jupiter.api.Test;\n\nclass BlockTest {\n"
          "    @Test\n    void first() {\n        assertEquals(1, 1);\n    }\n\n"
          "    @Test\n    void gone() {\n        assertEquals(2, 2);\n    }\n\n" + BLOCK + "}\n")


@pytest.mark.parametrize("end", ["\n", "\r\n"])
def test_pruning_keeps_a_text_blocks_blank_lines(end):
    """Only the blank lines a cut leaves collapse, to one: a kept test's text
    block keeps its two, and a CRLF file collapses at the cut too."""
    from langadapter.java.source_analysis import static_operations
    pruned = static_operations().prune_tests(BLOCKS.replace("\n", end), ["BlockTest.gone"])
    assert pruned.removed == ("BlockTest.gone",)
    assert pruned.source == BLOCKS.replace(
        "    @Test\n    void gone() {\n        assertEquals(2, 2);\n    }\n\n", "").replace("\n", end)


def test_merging_keeps_a_text_blocks_blank_lines(monkeypatch):
    """A merge's shared helper removal and its lifecycle step change no text
    block: each source keeps its test's two blank lines. javac's verdict on
    each source, which needs a JDK, is that it compiles."""
    from langadapter.java._sources import JavaSources
    from langadapter.java.source_analysis import static_operations
    monkeypatch.setattr(JavaSources, "_refusals", lambda self, items: [None] * len(items))
    helper = "    static int one() {\n        return 1;\n    }\n\n"
    first = BLOCKS.replace("class BlockTest {\n", "class BlockTest {\n" + helper + (
        "    @org.junit.jupiter.api.BeforeEach\n    void setUp() {\n    }\n\n"))
    second = (BLOCKS.replace("class BlockTest {\n", "class BlockTest {\n" + helper)
              .replace("void block()", "void block2()").replace("void first()", "void first2()")
              .replace("void gone()", "void gone2()"))
    result = static_operations().resolve_collisions([first, second], localize_autouse=True).value
    assert result.deduped == ((), ("one",)), (result.ejected, result.conflicts)
    assert result.localized_autouse[0] == ("setUp",)
    assert all('            a\n\n\n            b\n' in source for source in result.sources)


NOTES = """\
package demo;

import org.junit.jupiter.api.Test;

class NotesTest {
    // page{c}one, a detached note

    /** about{c}b */
    @Test
    void b() {
        /* {wide} */ int y = 2;
    }
}
"""


def _notes(char, wide):
    return NOTES.replace("{c}", char).replace("{wide}", wide)


@pytest.mark.parametrize("char", ["\x0c", "\x85", "\u2028"])
def test_finalizing_counts_rows_as_the_parser_does(adapter, char):
    # Rows end where the parser ends them, not at every splitlines break.
    plain = adapter.finalize_source(_notes("", "eeeeee"))
    finalized = adapter.finalize_source(_notes(char, "eeeeee"))
    assert plain.status == finalized.status == "applied"
    assert "a detached note" not in finalized.source
    assert finalized.source == plain.source.replace("aboutb", f"about{char}b")


def test_finalizing_keeps_code_after_a_wide_comment_on_its_line(adapter):
    # Tree-sitter columns count bytes: code after a multibyte comment stays.
    plain = adapter.finalize_source(_notes("", "e" * 16))
    finalized = adapter.finalize_source(_notes("", "\u00e9" * 16))
    assert finalized.status == "applied"
    assert finalized.source == plain.source.replace("e" * 16, "\u00e9" * 16)


def test_a_fold_resolves_collisions_and_compiles_and_runs(java_project, jdks,
                                                          maven, gradle):
    project = java_project("maven")
    adapter = _adapter(project, jdks, maven, gradle)
    result = adapter.resolve_collisions([FIRST, SECOND, THIRD],
                                        localize_autouse=True).value
    assert result.ejected == result.conflicts == (), (result.eject_reasons, result.conflicts)
    assert result.renames == (
        {}, {"positive": "positive_2", "setUp": "setUp_2"},
        {"positive": "positive_3", "teardowns": "teardowns_2", "values": "values_2"})
    assert result.deduped == ((), ("base", "twice"), ())
    assert result.localized_autouse == (("setUp",), ("setUp", "tearDown"), ())
    assert "java.lang.Integer.max(values_2(), 0)" in result.sources[2]
    assert "static int items(java.awt.List list)" in result.sources[2]
    assert "import java.awt.List" not in result.sources[2]
    merged = result.sources[0]
    for source in result.sources[1:]:
        step = adapter.merge_sources(merged, source, separator=SEPARATOR)
        assert step.status == "applied", step.detail
        merged = step.source
    assert merged.count(SEPARATOR) == 2
    assert "CalcTest.twice(base)" in merged
    run = _run(adapter, project, merged)
    assert run.outcome is lb.Outcome.COMPLETED, run.output
    assert (run.counts.passed, run.counts.failed) == (7, 0)
    assert {t.test.qualname for t in run.tests} == {
        "CalcTest.positive", "CalcTest.positive_2", "CalcTest.positive_3",
        "CalcTest.signs", "CalcTest.doubles"}


def test_without_localizing_the_fold_widens_each_setup(java_project, jdks,
                                                       maven, gradle):
    project = java_project("maven")
    adapter = _adapter(project, jdks, maven, gradle)
    result = adapter.resolve_collisions([FIRST, SECOND]).value
    assert result.localized_autouse == ((), ())
    merged = adapter.merge_sources(*result.sources, separator=SEPARATOR)
    run = _run(adapter, project, merged.source)
    assert run.outcome is lb.Outcome.TEST_FAILURES
    assert run.counts.failed >= 1


def test_what_cannot_be_folded_is_rejected_or_refused(adapter):
    constructor = SECOND.replace("    private static int teardowns;\n",
                                 "    private static int teardowns;\n\n"
                                 "    CalcMutationTest() {\n    }\n")
    extension = SECOND.replace(
        "class CalcMutationTest {",
        "@org.junit.jupiter.api.TestInstance("
        "org.junit.jupiter.api.TestInstance.Lifecycle.PER_CLASS)\n"
        "class CalcMutationTest {")
    other_package = SECOND.replace("package demo;", "package demo.other;")
    result = adapter.resolve_collisions(
        [FIRST, constructor, extension, other_package,
         "package demo;\nclass X { void f( }\n"])
    reasons = dict(zip(result.value.ejected, result.value.eject_reasons))
    assert sorted(reasons) == [1, 2, 3, 4]
    assert "constructor" in reasons[1] and "annotations" in reasons[2]
    assert "package" in reasons[3] and "javac" in reasons[4]
    for second in (constructor, extension, other_package):
        refused = adapter.merge_sources(FIRST, second, separator=SEPARATOR)
        assert refused.status == "refused" and refused.source == FIRST
    with pytest.raises(lb.InvalidRequest, match="separator"):
        adapter.merge_sources(FIRST, SECOND, separator="# not java")


def test_a_shadowed_field_is_a_conflict_a_preference_decides(adapter):
    shadowing = SECOND.replace(
        "    @Test\n    void doubles() {",
        "    @Test\n    void shadows() {\n        int base = 3;\n"
        "        assertEquals(1, Calc.sign(base));\n    }\n\n"
        "    @Test\n    void doubles() {").replace(
        "private int base;", "private int base = 0;")
    open_conflict = adapter.resolve_collisions([FIRST, shadowing]).value
    assert open_conflict.conflicts == (("base", (0, 1)),)
    assert open_conflict.dropped_defs == ((), ())
    decided = adapter.resolve_collisions(
        [FIRST, shadowing], preference=lambda name, owners: owners[0]).value
    assert decided.choices == (("base", 0, (1,)),)
    assert set(decided.dropped_defs[1]) == {"base", "setUp_2", "positive_2",
                                       "doubles"}
    assert "void shadows()" in decided.sources[1]
    assert "void signs(int v)" in decided.sources[1]


HELPED = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.util.function.IntSupplier;
import org.junit.jupiter.api.Test;

class CalcTest {
    static class Helper {
        static int make() {
            return VALUE;
        }
    }

    @Test
    void NAME() {
        IntSupplier made = Helper::make;
        assertEquals(VALUE, made.getAsInt());
    }
}
"""


def test_a_renamed_type_is_renamed_in_its_method_references(java_project, jdks,
                                                            maven, gradle):
    """A colliding nested type is renamed in method-reference qualifiers
    (Helper::make) too, so the merged suite passes."""
    project = java_project("maven")
    adapter = _adapter(project, jdks, maven, gradle)
    first = HELPED.replace("VALUE", "1").replace("NAME", "first")
    second = HELPED.replace("VALUE", "2").replace("NAME", "second")
    result = adapter.resolve_collisions([first, second]).value
    assert result.ejected == result.conflicts == (), (result.eject_reasons, result.conflicts)
    assert result.renames == ({}, {"Helper": "Helper_2"})
    assert "IntSupplier made = Helper_2::make;" in result.sources[1]
    step = adapter.merge_sources(result.sources[0], result.sources[1], separator=SEPARATOR)
    assert step.status == "applied", step.detail
    run = _run(adapter, project, step.source)
    assert run.outcome is lb.Outcome.COMPLETED, run.output
    assert (run.counts.passed, run.counts.failed) == (2, 0)


COMBINE = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcCombineTest {
    static int combine(int a, int b) {
        return a + b;
    }

    @Test
    void sums() {
        assertEquals(3, combine(1, 2));
    }
}
"""


def test_a_fold_keeps_apart_helpers_that_differ_by_an_operator(adapter):
    # Fingerprints count operators, so the second helper is not shared.
    second = (COMBINE.replace("a + b", "a - b").replace("void sums()", "void differences()")
              .replace("assertEquals(3, combine(1, 2))", "assertEquals(-1, combine(1, 2))"))
    result = adapter.resolve_collisions([COMBINE, second]).value
    assert result.ejected == result.conflicts == (), (result.eject_reasons, result.conflicts)
    assert result.deduped == ((), ())
    assert result.renames == ({}, {"combine": "combine_2"})
    assert "return a - b;" in result.sources[1]
    same = adapter.resolve_collisions([COMBINE, COMBINE.replace("void sums()", "void again()")]).value
    assert same.deduped == ((), ("combine",))


ORDERED = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcTwiceTest {
    static int twice() {
        return 2 * base();
    }

    static int base() {
        return 1;
    }

    @Test
    void doubles() {
        assertEquals(2, twice());
    }
}
"""


def test_a_fold_shares_a_declaration_only_as_renamed(adapter):
    """Declarations are compared after renames: ``twice`` reads the same in
    both sources but calls ``base``, renamed in the second, so the second
    ``twice`` is renamed too."""
    second = (ORDERED.replace("return 1;", "return 3;").replace("void doubles()", "void sixes()")
              .replace("assertEquals(2, twice())", "assertEquals(6, twice())"))
    result = adapter.resolve_collisions([ORDERED, second]).value
    assert result.ejected == result.conflicts == (), (result.eject_reasons, result.conflicts)
    assert result.deduped == ((), ())
    assert result.renames == ({}, {"base": "base_2", "twice": "twice_2"})
    assert "return 2 * base_2();" in result.sources[1]
    assert "assertEquals(6, twice_2());" in result.sources[1]
    same = adapter.resolve_collisions([ORDERED, ORDERED.replace("void doubles()", "void again()")]).value
    assert same.deduped == ((), ("twice", "base"))


CONDITIONED = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.condition.EnabledOnOs;
import org.junit.jupiter.api.condition.OS;

@EnabledOnOs(OS.LINUX)
@org.junit.jupiter.api.condition.DisabledIfEnvironmentVariable(named = "CI", matches = "true")
class CalcConditionedTest {
    @BeforeAll
    static void onlyWhenReady() {
        assumeTrue(System.getenv("READY") != null);
        System.out.println("ready");
    }

    @BeforeEach
    void eachWhenReady() {
        org.junit.Assume.assumeTrue(System.getenv("READY") != null);
    }

    @Test
    @EnabledOnOs(OS.WINDOWS)
    void positive() {
        assumeTrue(true);
        assertEquals(1, Calc.sign(5));
    }
}
"""


def test_class_level_skip_conditions_and_setup_assumptions_are_removed(adapter):
    """Besides @Disabled and @Ignore, the conditions that skip a whole class
    (JUnit Jupiter's @EnabledOn... and @Disabled... annotations on it) and the
    assumptions of its setup methods, which skip every test, go. A condition
    or an assumption of one test stays: it skips that test only."""
    stripped = adapter.strip_module_level_skips(CONDITIONED)
    assert stripped.status == "applied", stripped.detail
    assert stripped.removed == (
        "@EnabledOnOs(OS.LINUX)",
        '@org.junit.jupiter.api.condition.DisabledIfEnvironmentVariable(named = "CI", '
        'matches = "true")',
        'assumeTrue(System.getenv("READY") != null);',
        'org.junit.Assume.assumeTrue(System.getenv("READY") != null);')
    assert "class CalcConditionedTest {" in stripped.source
    assert '    @EnabledOnOs(OS.WINDOWS)\n    void positive() {\n        assumeTrue(true);' in (
        stripped.source)
    assert 'System.out.println("ready");' in stripped.source
    assert adapter.strip_module_level_skips(stripped.source).status == "unchanged"


SETUP_CALLS = """package demo;

import static org.junit.jupiter.api.Assumptions.assumingThat;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;

class CalcSetupTest {
    private final Request request = new Request();

    @BeforeEach
    void prepare() {
        assumingThat(System.getenv("READY") != null, () -> System.out.println("ready"));
        request.abort();
        org.junit.jupiter.api.Assumptions.abort("not ready");
    }

    @Test
    void positive() {
    }
}
"""


def test_setup_calls_that_skip_nothing_stay(adapter):
    """assumingThat runs its block when its condition holds and skips
    nothing, and abort on another object is that object's method: both stay
    in the setup method. Assumptions.abort skips every test and goes."""
    stripped = adapter.strip_module_level_skips(SETUP_CALLS)
    assert stripped.removed == ('org.junit.jupiter.api.Assumptions.abort("not ready");',)
