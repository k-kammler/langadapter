"""The grammar-parameterized collision engine in ``_collisions``.

``test_python_collisions`` pins the behavior (dedup, rename, per-def drop,
eject). This module covers the engine's own pieces: line spans that match a
comment-free AST, the spec's validity gate and the text machinery."""
from __future__ import annotations

import ast

import pytest

from langadapter.python import _sources as _treesitter
from langadapter.python._collisions import (
    _drop_spans,
    _module_bindings,
    resolve_collisions,
    resolve_python_collisions,
)


def _root(src: str):
    return _treesitter.parse("tree_sitter_python", src).root_node


def _binding(src: str, name: str):
    return next(b for b in _module_bindings(_root(src)) if b.name == name)


def test_line_span_matches_ast_end_lineno():
    """A definition's span matches the AST's lines, decorators included."""
    src = ("@deco\n"               # 1
           "def f():\n"            # 2
           "    x = 1\n"           # 3
           "    return x\n")       # 4
    span = _binding(src, "f").span
    tree = ast.parse(src)
    assert span == (1, tree.body[0].end_lineno) == (1, 4)


def test_line_span_excludes_trailing_block_comments():
    """tree-sitter puts trailing comment lines in the block, the AST does
    not: a class's span ends at its last statement, as the AST's does."""
    src = ("class C:\n"                       # 1
           "    def m(self):\n"               # 2
           "        return 1\n"               # 3  (last real token)
           "    # trailing comment one\n"     # 4
           "    # trailing comment two\n")     # 5
    assert _binding(src, "C").span == (1, 3)
    assert ast.parse(src).body[0].end_lineno == 3


def test_apply_edits_replaces_token_spans_right_to_left():
    """Both merge repairs apply byte-span edits from the right, so earlier
    spans stay valid and a multibyte character before them moves nothing."""
    from langadapter.sources import apply_edits

    assert apply_edits("a = foo + foo", [(4, 7, "bar"), (10, 13, "bar")]) == "a = bar + bar"
    assert apply_edits("é = foo", [(5, 8, "bar")]) == "é = bar"
    assert apply_edits("x", [(0, 1, "y"), (0, 1, "y")]) == "y"


def test_drop_spans_removes_inclusive_line_ranges():
    lines = ["one", "two", "three", "four"]
    assert _drop_spans(lines, [(2, 3)]) == ["one", "four"]


def test_the_python_entry_point_equals_the_engine():
    """Without shadowed definitions or autouse fixtures, the Python entry
    point gives the engine's own resolution."""
    a = "def helper():\n    return 1\n"
    b = "def helper():\n    return 2\n\ndef test_b():\n    assert helper() == 2\n"
    via_engine = resolve_collisions([a, b])
    via_wrapper = resolve_python_collisions([a, b])
    assert via_engine.sources == via_wrapper.sources
    assert via_engine.renames[1] == {"helper": "helper_2"}


def test_eject_uses_the_spec_validity_gate_not_tree_sitter():
    """Ejection follows the spec's ``parse_valid`` (CPython's parser), not
    the permissive tree-sitter parse, and keeps the parser's message."""
    ok = "def test_a():\n    assert 1\n"
    broken = "def broken(:\n"
    res = resolve_python_collisions([ok, broken])
    assert res.ejected == (1,)
    assert "syntax error" in res.eject_reasons[0]
    assert res.sources[0] == ok


HELPER_A = "def helper():\n    return 1\n\n\ndef test_a():\n    assert helper() == 1\n"


def test_a_form_feed_line_before_a_renamed_binding_is_kept_as_written():
    """Lines are split as tree-sitter counts rows, so a form-feed line
    shifts no later line and stays as written."""
    second = ("\x0c\ndef helper():\n    return 2\n\n\n"
              "def test_b():\n    assert helper() == 2\n")
    result = resolve_python_collisions([HELPER_A, second])
    assert result.ejected == ()
    assert result.sources[1] == second.replace("helper", "helper_2")


@pytest.mark.parametrize("character", ["\x0b", "\x0c", "\u2028"])
@pytest.mark.parametrize("place", ["string", "comment"])
def test_a_line_break_character_in_a_string_or_comment_stays_when_renamed(character, place):
    """A vertical tab, form feed or U+2028 in a string or comment does not
    block a rename and stays as written."""
    if place == "string":
        body = f"def helper():\n    return 'a{character}b'\n"
    else:
        body = f"def helper():\n    # a{character}b\n    return 2\n"
    second = body + "\n\ndef test_b():\n    assert helper()\n"
    result = resolve_python_collisions([HELPER_A, second])
    assert result.ejected == (), result.eject_reasons
    assert result.sources[1] == second.replace("helper", "helper_2")


GENERIC = '''from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class Box(Generic[T]):
    value: T
    tag: str = "{label}"


@dataclass
class Wrapper(Generic[T]):
    inner: "Box[T]"
    label: "str" = "{label}"


def test_{label}():
    assert Wrapper(Box(1)).inner.value == 1
'''


def test_a_string_annotation_and_a_typevars_name_follow_the_renames():
    """A string annotation, which ``get_type_hints`` evaluates in the merged
    module, follows every rename of its source, the ``TypeVar`` included,
    and the ``TypeVar``'s name argument follows its assignment."""
    import typing

    result = resolve_python_collisions(
        [GENERIC.format(label="a"), GENERIC.format(label="b")])
    assert result.ejected == (), result.eject_reasons
    second = result.sources[1]
    assert 'T_2 = TypeVar("T_2")' in second
    assert 'inner: "Box_2[T_2]"' in second and 'label: "str"' in second
    namespace: dict = {}
    exec(compile("\n".join(result.sources), "fold.py", "exec"), namespace)
    hints = typing.get_type_hints(namespace["Wrapper_2"], namespace)
    assert hints["inner"] == namespace["Box_2"][namespace["T_2"]]
    assert repr(namespace["T_2"]) == "~T_2"


REGISTRY = '''SEEN = []


def remember(value):
    SEEN.append(value)


def test_{label}():
    remember("{label}")
    assert SEEN == ["{label}"]
'''


def test_a_binding_shares_only_as_renamed():
    """A binding is shared by its renamed text: ``remember`` reads the same
    in both sources but appends to ``SEEN``, which the second renames, so
    ``remember`` is renamed too and the merged tests pass together."""
    result = resolve_python_collisions(
        [REGISTRY.format(label="a"), REGISTRY.format(label="b")])
    assert result.ejected == (), result.eject_reasons
    assert (result.renames[1]["SEEN"], result.renames[1]["remember"]) == (
        "SEEN_2", "remember_2")
    assert result.deduped == ((), ())
    namespace: dict = {}
    exec(compile("\n".join(result.sources), "fold.py", "exec"), namespace)
    namespace["test_a"]()
    namespace["test_b"]()


def test_a_generic_class_shares_only_with_its_own_typevar():
    """An identical generic class over a renamed ``TypeVar`` is the other
    source's class with the other ``TypeVar``: it is renamed with it."""
    plain = GENERIC.replace('    tag: str = "{label}"\n', "")
    result = resolve_python_collisions([plain.format(label="a"), plain.format(label="b")])
    assert result.ejected == (), result.eject_reasons
    second = result.sources[1]
    assert "class Box_2(Generic[T_2]):" in second and 'inner: "Box_2[T_2]"' in second
    assert "Box" not in result.deduped[1]


VALUES = '''from typing import Annotated, Literal


def mode():
    return "{label}"


class Box:
    label = "{label}"


def pick(choice: Literal["mode", "Box"], tagged: Annotated[int, "mode"],
         spelled: "Literal['mode']", box: "Box") -> "Box":
    return Box()


def test_{label}():
    assert mode() == "{label}" and isinstance(pick("mode", 1, "mode", Box()), Box)
'''


def test_only_type_references_in_an_annotation_follow_the_renames():
    """Literal values and Annotated metadata are run-time values and keep
    their text when ``mode`` is renamed, while a string naming a renamed type
    follows its rename."""
    result = resolve_python_collisions(
        [VALUES.format(label="a"), VALUES.format(label="b")])
    assert result.ejected == (), result.eject_reasons
    second = result.sources[1]
    assert 'choice: Literal["mode", "Box"]' in second
    assert 'tagged: Annotated[int, "mode"]' in second
    assert "spelled: \"Literal['mode']\"" in second
    assert 'box: "Box_2") -> "Box_2":' in second and "def pick_2(" in second
    namespace: dict = {}
    exec(compile("\n".join(result.sources), "fold.py", "exec"), namespace)
    namespace["test_a"]()
    namespace["test_b"]()
