"""Units of a source from a tree-sitter grammar and a small rule set.

``TreeSitterUnits(rules)`` finds a source's declarations with qualified names,
its tests and the smallest removable declaration around given lines, and cuts
byte spans out with their whole lines. ``Rules`` says which node kinds are
types, members, imports and comments, how a test is recognized and how a
member is named. The Java adapter gives Java's rules.
"""
from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple
from langadapter import _treesitter
from langadapter._treesitter import diff_skeletons as diff_skeletons
from langadapter._treesitter import name as name


def text(node: Any) -> str:
    """A node's source text."""
    return node.text.decode("utf-8", "replace")


@dataclass(frozen=True)
class Rules:
    """What a grammar's node kinds mean for units.

    ``type_kinds`` declare a type whose ``body`` holds members,
    ``function_kinds`` a method or constructor, ``field_kinds`` a field whose
    declarators (``declarator_kind``) are named. ``passthrough_kinds`` group
    members in a body, visited in place. ``skipped_kinds`` are never a unit (a
    line in one selects its type). ``comment_kinds`` are comments, never a
    unit either. ``named_fields`` are the member kinds a removal names by
    their declarators, such as a field of several variables. A top-level
    ``import_kind`` statement is a unit named ``import`` with
    ``import_label`` of its text. ``initializers`` label nameless member kinds
    (Java's static initializer). ``top_level_kinds`` are the only statements
    outside types that are units (a function or call declaring a test), named by
    ``unit_label``. ``is_test`` tells a test, ``signature`` a function's
    parameter types."""

    grammar: str
    type_kinds: FrozenSet[str]
    function_kinds: FrozenSet[str]
    is_test: Callable[[Any], bool]
    signature: Callable[[Any], str]
    field_kinds: FrozenSet[str] = frozenset()
    named_fields: FrozenSet[str] = frozenset()
    declarator_kind: str = "variable_declarator"
    passthrough_kinds: FrozenSet[str] = frozenset()
    skipped_kinds: FrozenSet[str] = frozenset()
    comment_kinds: FrozenSet[str] = frozenset()
    import_kind: str = ""
    import_label: Callable[[str], str] = field(default=lambda source: source)
    initializers: Mapping[str, str] = field(default_factory=dict)
    top_level_kinds: FrozenSet[str] = frozenset()
    unit_label: Callable[[Any], str] = field(default=lambda node: "")


def whole_lines(data: bytes, start: int, end: int) -> Tuple[int, int]:
    """``(start, end)`` widened to whole lines when only whitespace shares them."""
    line_start = data.rfind(b"\n", 0, start) + 1
    if data[line_start:start].strip():
        return start, end
    line_end = data.find(b"\n", end)
    line_end = len(data) if line_end < 0 else line_end + 1
    if data[end:line_end].strip():
        return start, end
    return line_start, line_end


def cut(source: str, spans: List[Tuple[int, int]]) -> str:
    """``source`` without the byte ``spans``, each widened to its whole lines,
    and with each run of blank lines they leave shortened to one."""
    data = source.encode("utf-8")
    return _treesitter.apply_edits_collapsing(
        source, [(*whole_lines(data, s, e), "") for s, e in spans], 1)


class TreeSitterUnits:
    """A grammar's units by ``rules``."""

    def __init__(self, rules: Rules) -> None:
        self.rules = rules

    def parse(self, source: str) -> Any:
        """The root node of ``source``'s syntax tree in the rules' grammar."""
        return _treesitter.parse(self.rules.grammar, source).root_node

    def members(self, source: str) -> Optional[List[Tuple[str, Any]]]:
        """Every declaration in each type, and every top-level unit, of ``source`` in
        order with its qualified name (``Type.Nested.member``), or None if unparsable."""
        root = self.parse(source)
        if root.has_error:
            return None
        rules = self.rules
        out: List[Tuple[str, Any]] = []

        def visit(node: Any, prefix: str) -> None:
            for child in node.named_children:
                if child.type in rules.type_kinds:
                    qualified = f"{prefix}{name(child)}"
                    out.append((qualified, child))
                    body = child.child_by_field_name("body")
                    if body is not None:
                        visit(body, f"{qualified}.")
                elif child.type in rules.function_kinds:
                    out.append((f"{prefix}{name(child)}", child))
                elif child.type in rules.field_kinds:
                    for declarator in child.named_children:
                        if declarator.type == rules.declarator_kind:
                            out.append((f"{prefix}{name(declarator)}", child))
                elif child.type in rules.passthrough_kinds:
                    visit(child, prefix)
                elif not prefix and child.type in rules.top_level_kinds:
                    out.append((self.label(child), child))

        visit(root, "")
        return out

    def test_count(self, source: str) -> int:
        """The tests ``source`` declares, in every type."""
        return sum(1 for _, node in self.members(source) or () if self.rules.is_test(node))

    def test_overloads(self, overloads: List[Any]) -> List[Any]:
        """The overloads of a test's name that are tests: those the rules
        call tests, else those without parameters, else all."""
        tests = [n for n in overloads if self.rules.is_test(n)]
        if tests:
            return tests
        bare = [n for n in overloads if not self.rules.signature(n)]
        return bare or overloads

    def label(self, member: Any) -> str:
        """A member's name as a removal names it: a field's declarators, an
        initializer's name, a function with its parameter types."""
        rules = self.rules
        if member.type in rules.named_fields:
            return ", ".join(name(d) for d in member.named_children
                             if d.type == rules.declarator_kind)
        if member.type in rules.initializers:
            return rules.initializers[member.type]
        if member.type in rules.function_kinds:
            return f"{name(member)}({rules.signature(member)})"
        if member.type in rules.top_level_kinds:
            return rules.unit_label(member) or name(member)
        return name(member)

    def units_at(self, source: str, lines: Iterable[int]) -> Optional[List[Tuple[str, Any]]]:
        """The smallest removable declaration around each of ``lines`` (ended by
        LF, CR or both) with its qualified name: an import, a type's member (the
        innermost where types nest) or a top-level unit, all where several share
        a line. One inside another chosen one goes with it. None when a line
        lies in no such declaration or ``source`` does not parse."""
        rules = self.rules
        data = source.encode("utf-8")
        starts = [0] + [m.end() for m in re.finditer(rb"\r\n|\r|\n", data)]

        def span(node: Any) -> Tuple[int, int]:
            return (bisect.bisect_right(starts, node.start_byte),
                    bisect.bisect_right(starts, max(node.start_byte, node.end_byte - 1)))

        def members(body: Any) -> Iterable[Any]:
            for child in body.named_children:
                if child.type in rules.passthrough_kinds:
                    yield from members(child)
                elif child.type not in rules.comment_kinds and child.type not in rules.skipped_kinds:
                    yield child

        def inner(declaration: Any, prefix: str, number: int) -> List[Tuple[str, Any]]:
            qualified = f"{prefix}{name(declaration)}"
            body = declaration.child_by_field_name("body")
            found: List[Tuple[str, Any]] = []
            for member in members(body) if body is not None else ():
                first, last = span(member)
                if not first <= number <= last:
                    continue
                if member.type in rules.type_kinds:
                    found += (inner(member, f"{qualified}.", number)
                              or [(f"{qualified}.{name(member)}", member)])
                else:
                    found.append((f"{qualified}.{self.label(member)}", member))
            return found

        root = self.parse(source)
        if root.has_error:
            return None
        chosen: Dict[Tuple[int, int], Tuple[str, Any]] = {}
        for number in sorted(set(lines)):
            found: List[Tuple[str, Any]] = []
            for child in root.named_children:
                first, last = span(child)
                if not first <= number <= last:
                    continue
                if rules.import_kind and child.type == rules.import_kind:
                    found.append(("import " + rules.import_label(text(child).strip()), child))
                elif child.type in rules.type_kinds:
                    found += inner(child, "", number)
                elif child.type in rules.top_level_kinds:
                    found.append((self.label(child), child))
            if not found:
                return None
            for label, node in found:
                chosen[(node.start_byte, node.end_byte)] = (label, node)
        out: List[Tuple[str, Any]] = []
        for (start, _), (label, node) in sorted(chosen.items(),
                                                key=lambda item: (item[0][0], -item[0][1])):
            if out and start < out[-1][1].end_byte:
                continue
            out.append((label, node))
        return out
