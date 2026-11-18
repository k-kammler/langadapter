"""The one rename of a Java type, shared by the merge repair, pooled staging
and ``source_analysis.rename_class``."""
from __future__ import annotations

import pytest

from langadapter.java import _collisions
from langadapter.java.source_analysis import rename_class
from langadapter.sources import PoolMember
from langadapter.sources import SourceStatus

RENAMED = [
    ("class Helper { static int make() { return 1; } Helper() {} }\n"
     "class T { int v = Helper.make(); Object o = new Helper(); Helper[] a; }\n",
     "class Helper_2 { static int make() { return 1; } Helper_2() {} }\n"
     "class T { int v = Helper_2.make(); Object o = new Helper_2(); Helper_2[] a; }\n"),
    # Method and constructor reference qualifiers are renamed too.
    ("class Helper { static int make() { return 1; } }\n"
     "class T { java.util.function.IntSupplier s = Helper::make;"
     " java.util.function.Supplier<Helper> n = Helper::new; }\n",
     "class Helper_2 { static int make() { return 1; } }\n"
     "class T { java.util.function.IntSupplier s = Helper_2::make;"
     " java.util.function.Supplier<Helper_2> n = Helper_2::new; }\n"),
    # Labels, strings, imports, packages and another class's method keep their text.
    ("package p;\nimport q.Helper.Inner;\n"
     "class Helper { }\n"
     "class T { String s = \"Helper\"; void t(Other o) { o.Helper(); "
     "Helper: for (;;) { break Helper; } } }\n",
     "package p;\nimport q.Helper.Inner;\n"
     "class Helper_2 { }\n"
     "class T { String s = \"Helper\"; void t(Other o) { o.Helper(); "
     "Helper: for (;;) { break Helper; } } }\n"),
    # An annotation element of another type keeps its name.
    ("class Helper { }\n@Tag(Helper = 1) class T { Helper h; }\n",
     "class Helper_2 { }\n@Tag(Helper = 1) class T { Helper_2 h; }\n"),
]

REFUSED = [
    ("class Helper { }\nclass T { void Helper() {} }\n",
     "a method is also named Helper"),
    ("class Helper { }\nclass T { void t() { int Helper = 1; } }\n",
     "a variable, parameter or field is also named Helper"),
    ("class Helper { }\nclass T { void t(int Helper) { } }\n",
     "a variable, parameter or field is also named Helper"),
    ("class Helper { }\nclass O { int Helper; }\n",
     "a variable, parameter or field is also named Helper"),
    ("@interface A { int Helper(); }\nclass Helper { }\n",
     "an annotation element is also named Helper"),
]


@pytest.mark.parametrize("source, renamed", RENAMED)
def test_a_type_is_renamed_with_every_use_of_it_as_a_type(source, renamed):
    observed = rename_class(source, "Helper", "Helper_2")
    assert observed.status is SourceStatus.COMPLETED and observed.value == renamed
    edits = _collisions._type_edits(_collisions._Source(0, source), "Helper", "Helper_2")
    assert edits is not None and len(edits) == renamed.count("Helper_2")


@pytest.mark.parametrize("source, reason", REFUSED)
def test_a_type_whose_name_has_another_meaning_is_not_renamed(source, reason):
    """A method, variable, parameter, field or annotation element with the
    type's name makes its uses ambiguous: the rename is refused with the
    reason, and the merge
    repair counts a conflict."""
    observed = rename_class(source, "Helper", "Helper_2")
    assert observed.status is SourceStatus.UNSUPPORTED and observed.reason == reason
    assert _collisions._type_edits(_collisions._Source(0, source), "Helper", "Helper_2") is None


def test_a_source_that_does_not_parse_is_reported_and_kept():
    broken = "class Helper {\n"
    assert rename_class(broken, "Helper", "Helper_2").status is SourceStatus.PARSE_ERROR
    same = rename_class("class Helper { }\n", "Helper", "Helper")
    assert same.completed and same.value == "class Helper { }\n"


def test_a_pool_whose_type_cannot_be_renamed_safely_says_why():
    from langadapter.java.source_analysis import static_operations

    source = "package demo;\nclass CalcTest { void CalcTest() { } }\n"
    observation = static_operations().stage_pool((
        PoolMember("a", "CalcTest.java", source),
        PoolMember("b", "CalcTest.java", source.replace("void CalcTest() { }", ""))))
    assert observation.status is SourceStatus.UNSUPPORTED
    assert observation.reason == ("CalcTest.java of suite a: the type CalcTest cannot be "
                                  "renamed safely: a method is also named CalcTest")


def test_a_merge_renames_the_second_class_by_the_same_rule():
    """``merge_sources`` renames the second class by the same rule: its uses
    and reference qualifiers take the first's name, a label keeps its text,
    and a class whose name another member shares is refused with the reason."""
    from langadapter.java import _sources

    first = "package demo;\n\nclass CalcTest {\n    void a() { }\n}\n"
    second = ("package demo;\n\nclass OtherTest {\n"
              "    static void helper() { }\n"
              "    void b() { Runnable r = OtherTest::helper; OtherTest.helper();\n"
              "        OtherTest: for (;;) { break OtherTest; } }\n}\n")
    merged, reason = _sources._merge(first, second, "// merged")
    assert reason == "" and "OtherTest::" not in merged
    assert "Runnable r = CalcTest::helper; CalcTest.helper();" in merged
    assert "OtherTest: for (;;) { break OtherTest; }" in merged
    clashing = second.replace("static void helper() { }",
                              "static void helper() { }\n    void OtherTest() { }")
    merged, reason = _sources._merge(first, clashing, "// merged")
    assert merged is None
    assert reason == ("the second class OtherTest cannot be renamed safely: "
                      "a method is also named OtherTest")


def test_a_field_rename_reaches_a_method_reference_qualifier():
    """A merge that renames a field renames it where a method reference
    names it as its qualifier: the qualifier is a type or an expression, the
    rest a method."""
    from langadapter.sources import apply_edits

    source = ("class CalcTest {\n"
              "    java.util.Set<String> seen = new java.util.HashSet<>();\n"
              "    void b() { java.util.List.of(\"b\").forEach(seen::add); }\n}\n")
    own = _collisions._Source(0, source)
    binding = next(b for b in _collisions._bindings(own).values()
                   if b.namespace == "field" and b.name == "seen")
    edits = _collisions._field_edits(own, "seen", "seen_2", binding)
    assert apply_edits(source, edits) == source.replace("seen", "seen_2")


def test_a_qualified_import_reaches_a_method_reference_qualifier():
    """An import an earlier source makes ambiguous is dropped and its type
    named fully, in a method reference's qualifier too."""
    source = ("import b.Helper;\n\nclass CalcTest {\n"
              "    java.util.function.IntSupplier s = Helper::make;\n}\n")
    own = _collisions._Source(1, source)
    _collisions._qualify_imports(own, {(False, "Helper"): "a.Helper"})
    assert "import b.Helper" not in own.text
    assert "java.util.function.IntSupplier s = b.Helper::make;" in own.text
