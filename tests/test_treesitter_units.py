"""The tree-sitter toolkit with another language's rules than Java's: its
units come from the grammar and the rule set alone."""
import dataclasses

from langadapter import treesitter

SOURCE = '''import missing_module


class TestCalc:
    def test_adds(self):
        assert 1 + 1 == 2

    def test_breaks(self):
        assert missing_module.value == 3

    def helper(self, x):
        return x


def test_top():
    assert True
'''

RULES = treesitter.Rules(
    grammar="tree_sitter_python",
    type_kinds=frozenset({"class_definition"}),
    function_kinds=frozenset({"function_definition"}),
    is_test=lambda node: (node.type == "function_definition"
                          and treesitter.name(node).startswith("test")),
    signature=lambda node: treesitter.text(node.child_by_field_name("parameters")),
    comment_kinds=frozenset({"comment"}),
    import_kind="import_statement",
    import_label=lambda text: text.replace("import ", "", 1),
)
UNITS = treesitter.TreeSitterUnits(RULES)


def _line(text):
    return SOURCE[:SOURCE.index(text)].count("\n") + 1


def test_members_and_tests_come_from_the_rules():
    assert [name for name, _ in UNITS.members(SOURCE)] == [
        "TestCalc", "TestCalc.test_adds", "TestCalc.test_breaks", "TestCalc.helper", "test_top"]
    assert UNITS.test_count(SOURCE) == 3


def test_the_unit_around_a_line_is_the_innermost_member_or_the_import():
    found = UNITS.units_at(SOURCE, [_line("missing_module.value"), _line("import missing")])
    assert [label for label, _ in found] == ["import missing_module",
                                             "TestCalc.test_breaks((self))"]
    remaining = treesitter.cut(SOURCE, [(n.start_byte, n.end_byte) for _, n in found])
    assert "missing_module" not in remaining and "test_adds" in remaining
    assert UNITS.units_at(SOURCE, [_line("class TestCalc")]) is None


def test_whole_lines_widen_only_a_span_alone_on_its_lines():
    data = b"keep\n    drop me\nkeep\n"
    start = data.index(b"drop")
    assert treesitter.whole_lines(data, start, start + 7) == (5, 17)
    assert treesitter.whole_lines(b"x = 1  # drop", 7, 13) == (7, 13)


def test_a_top_level_unit_is_opt_in_by_its_kind():
    assert UNITS.units_at(SOURCE, [_line("assert True")]) is None
    tops = treesitter.TreeSitterUnits(dataclasses.replace(
        RULES, top_level_kinds=frozenset({"function_definition"})))
    found = tops.units_at(SOURCE, [_line("assert True")])
    assert [label for label, _ in found] == ["test_top(())"]
    calls = treesitter.TreeSitterUnits(treesitter.Rules(
        grammar="tree_sitter_python", type_kinds=frozenset(), function_kinds=frozenset(),
        is_test=lambda node: treesitter.text(node).startswith("test("),
        signature=lambda node: "", top_level_kinds=frozenset({"expression_statement"}),
        unit_label=lambda node: treesitter.text(node).split(",", 1)[0] + ")"))
    source = "test('adds', lambda: 1 + 1 == 2)\ntest('breaks', lambda: missing)\n"
    assert [label for label, _ in calls.members(source)] == ["test('adds')", "test('breaks')"]
    assert calls.test_count(source) == 2
    assert [label for label, _ in calls.units_at(source, [2])] == ["test('breaks')"]


def test_the_python_and_java_adapters_name_their_parser_the_same_way():
    """Both adapters' parser identity is the shared one: the tree-sitter
    distribution and their grammar's, each with its installed version."""
    from importlib import metadata

    from langadapter import _treesitter
    from langadapter.java._sources import parser_identity as java_identity
    from langadapter.python.adapter import PythonAdapter

    assert java_identity() == _treesitter.parser_identity("tree-sitter-java")
    python = PythonAdapter.__new__(PythonAdapter).structural_parser_identity()
    assert python == _treesitter.parser_identity("tree-sitter-python")
    assert python.startswith(f"tree-sitter {metadata.version('tree-sitter')}; tree-sitter-python ")
    assert _treesitter.parser_identity("no-such-grammar").endswith("no-such-grammar unknown")
