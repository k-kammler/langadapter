"""The Java generated-suite operations: syntax salvage, unresolvable
imports, class-level skips and the validity policy and screening.
test_collection_breakers.py covers collection-breaker pruning."""
from __future__ import annotations

import os

import pytest

import langadapter as lb

CLEAN = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcGeneratedTest {
    @Test
    void positive() {
        assertEquals(1, Calc.sign(5));
    }
}
"""


def _adapter(project, jdks, maven, feature=21):
    if feature not in jdks:
        pytest.skip(f"needs a JDK {feature}")
    java = jdks[feature] / "bin" / ("java.exe" if os.name == "nt" else "java")
    return lb.create("java", lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=str(java)),
        environment={"LANGADAPTER_MAVEN": maven}))


@pytest.fixture
def adapter(java_project, jdks, maven):
    return _adapter(java_project("maven"), jdks, maven)


def test_the_salvage_blanks_bad_lines_and_closes_a_truncated_class(adapter):
    assert adapter.salvage_syntax(CLEAN).status == "unchanged"
    garbled = CLEAN.replace("        assertEquals(1, Calc.sign(5));\n",
                            "        assertEquals(1, Calc.sign(5));\n"
                            "        here the model wrote prose\n")
    repaired = adapter.salvage_syntax(garbled)
    assert repaired.status == "applied", repaired.detail
    assert "prose" not in repaired.source
    assert adapter.validate_source(repaired.source).valid
    truncated = CLEAN.rstrip().rstrip("}")
    closed = adapter.salvage_syntax(truncated)
    assert closed.status == "applied" and adapter.validate_source(closed.source).valid
    assert adapter.skeleton(closed.source) == adapter.skeleton(CLEAN)


@pytest.mark.parametrize("char", ["\x0c", "\u2028"])
def test_the_salvage_blanks_the_line_javac_numbered(adapter, char):
    # str.splitlines ends a line at the character, javac does not.
    garbled = CLEAN.replace("        assertEquals(1, Calc.sign(5));\n",
                            f"        // page{char}break\n"
                            "        assertEquals(1, Calc.sign(5));\n"
                            "        here the model wrote prose\n")
    repaired = adapter.salvage_syntax(garbled)
    assert repaired.status == "applied", repaired.detail
    assert repaired.source == garbled.replace("        here the model wrote prose\n", "\n")


def test_imports_javac_cannot_resolve_are_pruned(adapter):
    source = CLEAN.replace(
        "import org.junit.jupiter.api.Test;\n",
        "import org.junit.jupiter.api.Test;\nimport org.example.Missing;\n"
        "import static org.nowhere.Util.helper;\n")
    pruned = adapter.prune_unresolvable_imports(source)
    assert pruned.status == "applied", pruned.detail
    assert pruned.removed == ("org.example.Missing", "org.nowhere.Util.helper")
    assert pruned.source == CLEAN
    assert adapter.prune_unresolvable_imports(CLEAN).status == "unchanged"
    assert adapter.prune_unresolvable_imports(
        "package demo;\nclass X { void f( }\n").status == "refused"


def test_imports_are_pruned_by_the_line_javac_reports(adapter):
    # A lone carriage return ends a line for javac, not a tree-sitter row.
    clean = CLEAN.replace("package demo;\n\n", "package demo;\r\r")
    source = clean.replace(
        "import org.junit.jupiter.api.Test;\n",
        "import org.junit.jupiter.api.Test;\nimport org.example.Missing;\n")
    pruned = adapter.prune_unresolvable_imports(source)
    assert pruned.status == "applied", pruned.detail
    assert pruned.removed == ("org.example.Missing",)
    assert pruned.source == clean


def test_class_level_skips_are_removed(adapter):
    skipped = CLEAN.replace(
        "class CalcGeneratedTest {",
        "@org.junit.jupiter.api.Disabled(\"later\")\nclass CalcGeneratedTest {")
    stripped = adapter.strip_module_level_skips(skipped)
    assert stripped.status == "applied" and stripped.source == CLEAN
    assert adapter.strip_module_level_skips(CLEAN).status == "unchanged"


POLICY = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.lang.reflect.Method;
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.jupiter.api.Test;

class CalcPolicyTest {
    @Test
    void readsSource() throws Exception {
        String text = Files.readString(Path.of("src/main/java/demo/Calc.java"));
        assertTrue(text.contains("sign"));
    }

    @Test
    void readsClass() throws Exception {
        assertTrue(CalcPolicyTest.class.getResourceAsStream("Calc.class") != null);
    }

    @Test
    void opensPrivates() throws Exception {
        Method method = Calc.class.getDeclaredMethod("sign", int.class);
        method.setAccessible(true);
        assertEquals(1, method.invoke(null, 5));
    }

    @Test
    void behaves() {
        assertEquals(1, Calc.sign(5));
    }

    static final class Calc {
        static int sign(int x) {
            return 1;
        }
    }
}
"""


def test_the_validity_policy_finds_what_observes_the_implementation(adapter):
    identity = adapter.target_identity("src/main/java/demo/Calc.java")
    assert (identity.module_name, identity.language) == ("demo.Calc", "java")
    assert identity.policy_version == "java-generated-test-validity-v1"
    report = adapter.analyze_validity(POLICY, identity)
    assert report.status == "completed"
    found = {(f.rule_id, f.enclosing_test) for f in report.items}
    assert found == {
        ("target_source_read", "CalcPolicyTest.readsSource"),
        ("target_class_read", "CalcPolicyTest.readsClass"),
        ("copied_target", None),
    }
    assert adapter.analyze_validity(CLEAN, identity).items == ()
    assert adapter.analyze_validity("class X {", identity).status == "parse_error"
    screened = adapter.screen_source(POLICY, identity)
    assert {f.rule_id for f in screened.items} == {"target_literal",
                                                   "reflective_access"}
    with pytest.raises(lb.InvalidRequest, match="target"):
        adapter.target_identity("src/main/java/demo/Missing.java")


def test_set_accessible_is_allowed_and_flagged_for_review_only(adapter):
    """The rule the model reads allows reflection on the target's classes, so
    opening a private member with setAccessible is no finding. The review
    screen flags it, and its flag says it is allowed and only for review."""
    identity = adapter.target_identity("src/main/java/demo/Calc.java")
    source = (
        "package demo;\n\nimport java.lang.reflect.Method;\n"
        "import org.junit.jupiter.api.Test;\n\n"
        "class CalcReflectionTest {\n"
        "    @Test\n"
        "    void opensAPrivateMethod() throws Exception {\n"
        "        Method sign = Calc.class.getDeclaredMethod(\"sign\", int.class);\n"
        "        sign.setAccessible(true);\n"
        "    }\n"
        "}\n")
    assert adapter.analyze_validity(source, identity).items == ()
    (flag,) = [f for f in adapter.screen_source(source, identity).items
               if f.rule_id == "reflective_access"]
    assert flag.diagnostic == "opens a private member with setAccessible (allowed, for review only)"
    allowed, forbidden = adapter.test_vocabulary()["validity_rule"].split("Forbidden:")
    assert "reflection on its classes" in allowed
    assert "reflection" not in forbidden and "setAccessible" not in forbidden


def test_the_screen_takes_only_file_apis_for_discovery(adapter):
    """A collector's own find, list or walk is no file discovery, but the
    Files methods and a File's listFiles and list are."""
    identity = adapter.target_identity("src/main/java/demo/Calc.java")
    source = (
        "package demo;\n\nimport java.io.File;\nimport java.nio.file.Files;\n"
        "import java.nio.file.Paths;\nimport org.junit.jupiter.api.Test;\n\n"
        "class CalcScreenTest {\n    @Test\n    void collects() throws Exception {\n"
        "        Collector collector = new Collector();\n"
        "        collector.find(\"x\");\n        collector.list();\n"
        "        collector.walk();\n"
        "        Files.walk(Paths.get(\".\"));\n"
        "        new File(\".\").listFiles();\n"
        "        new File(\".\").list();\n    }\n}\n")
    lines = {f.start_line for f in adapter.screen_source(source, identity).items
             if f.rule_id == "source_discovery"}
    assert lines == {15, 16, 17}


def test_the_report_offers_the_generated_suite_operations(adapter):
    report = adapter.capabilities()
    assert report.capability("generated_suite_operations").available
    assert report.capability("per_test_evidence").available


def test_a_root_without_a_build_parses_at_the_jdks_release(tmp_path, jdks):
    if 21 not in jdks:
        pytest.skip("needs a JDK 21")
    java = jdks[21] / "bin" / ("java.exe" if os.name == "nt" else "java")
    adapter = lb.create("java", lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=str(java))))
    verdict = adapter.validate_source(CLEAN)
    assert verdict.valid and "no project build" in verdict.interpreter
    assert adapter.merge_sources(
        CLEAN, CLEAN.replace("class CalcGeneratedTest", "class CalcOtherTest")
        .replace("void positive", "void other"),
        separator="// merged").status == "applied"
