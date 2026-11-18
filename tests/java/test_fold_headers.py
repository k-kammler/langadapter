"""A merged class's tests run under the first class's header, so classes
merge only when their annotations, superclass and interfaces agree, in
either direction. @SuppressWarnings alone is no difference: the merged
class suppresses what either did."""
import pytest

from langadapter import _treesitter
from langadapter.java import _collisions
from langadapter.java._sources import GRAMMAR, _merge, _suppressed, _top_class, header_difference

SEPARATOR = "// --- merged ---"


def _suite(header="", extends="", method="adds"):
    return (f"package demo;\n\nimport org.junit.jupiter.api.Test;\n\n{header}"
            f"public class CalcTest{extends} {{\n    @Test\n    void {method}() {{\n    }}\n}}\n")


PLAIN = _suite()
DEPRECATION = _suite('@SuppressWarnings("deprecation")\n', method="subtracts")
UNCHECKED = _suite('@SuppressWarnings("unchecked")\n', method="multiplies")
PER_CLASS = _suite("@org.junit.jupiter.api.TestInstance("
                   "org.junit.jupiter.api.TestInstance.Lifecycle.PER_CLASS)\n", method="divides")
EXTENDS = _suite(extends=" extends BaseTest", method="negates")
IMPLEMENTS = _suite(extends=" implements Lifecycle", method="squares")


def _top(source):
    return _top_class(_treesitter.parse(GRAMMAR, source).root_node)


class _Operations:
    """The resolution's javac check, accepting every source."""

    @staticmethod
    def structural_parser_identity():
        return "stub"

    @staticmethod
    def _refusals(items):
        return [None] * len(items)


@pytest.mark.parametrize("first, second, difference", [
    (PLAIN, DEPRECATION, ""), (DEPRECATION, PLAIN, ""), (UNCHECKED, DEPRECATION, ""),
    (PLAIN, PER_CLASS, "annotations"), (PER_CLASS, PLAIN, "annotations"),
    (PLAIN, EXTENDS, "superclass"), (EXTENDS, PLAIN, "superclass"),
    (PLAIN, IMPLEMENTS, "interfaces"), (IMPLEMENTS, PLAIN, "interfaces"),
])
def test_headers_differ_in_either_direction_but_for_suppressed_warnings(first, second, difference):
    assert header_difference(_top(first), _top(second)) == difference


@pytest.mark.parametrize("order", [(PLAIN, DEPRECATION, DEPRECATION), (DEPRECATION, PLAIN, PLAIN)])
def test_suppressed_warnings_fold_in_either_order(order):
    result = _collisions.resolve(_Operations(), list(order), localize=True).value
    assert result.ejected == ()
    merged = result.sources[0]
    for source in result.sources[1:]:
        merged, reason = _merge(merged, source, SEPARATOR)
        assert merged is not None, reason
    assert _suppressed(_top(merged)) == ['"deprecation"']
    assert merged.count("@SuppressWarnings") == 1


def test_the_folded_class_suppresses_what_either_class_did():
    merged, reason = _merge(UNCHECKED, DEPRECATION, SEPARATOR)
    assert merged is not None, reason
    assert '@SuppressWarnings({"unchecked", "deprecation"})\npublic class CalcTest {' in merged
    plain_first, reason = _merge(PLAIN, DEPRECATION, SEPARATOR)
    assert '@SuppressWarnings("deprecation")\npublic class CalcTest {' in plain_first, reason
    unchanged, _ = _merge(DEPRECATION, PLAIN, SEPARATOR)
    assert unchanged.count('@SuppressWarnings("deprecation")') == 1


@pytest.mark.parametrize("first, second, label", [
    (PLAIN, PER_CLASS, "annotations"), (PER_CLASS, PLAIN, "annotations"),
    (PLAIN, EXTENDS, "superclass"), (EXTENDS, PLAIN, "superclass"),
    (PLAIN, IMPLEMENTS, "interfaces"), (IMPLEMENTS, PLAIN, "interfaces"),
])
def test_a_header_that_changes_how_tests_run_is_rejected_in_either_order(first, second, label):
    result = _collisions.resolve(_Operations(), [first, second], localize=True).value
    assert dict(zip(result.ejected, result.eject_reasons)) == {
        1: f"its class's {label} differ from the first source's"}
    merged, reason = _merge(first, second, SEPARATOR)
    assert merged is None and f"the classes' {label} differ" in reason
