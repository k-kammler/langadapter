"""C sources read with tree-sitter-c: test units, syntax checks and functions.

tree-sitter does not expand macros, so a utest.h test such as
``UTEST(set, name) { ... }`` reads as a call without its semicolon followed
by a block. Before a test file is checked, ``masked`` replaces each such
macro head at the top level, and ``UTEST_MAIN()``, with a plain declaration
of the same length, so that byte offsets and lines stay where they were and
only real syntax errors remain.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator, List, Optional, Set, Tuple

#: The utest.h macros that open a test, with the test's block after them.
TEST_MACROS = ("UTEST", "UTEST_F", "UTEST_I")
#: The utest.h macros that open a fixture's setup or teardown block.
FIXTURE_MACROS = ("UTEST_F_SETUP", "UTEST_F_TEARDOWN", "UTEST_I_SETUP", "UTEST_I_TEARDOWN")
#: The utest.h macros that stand alone at the top level.
STANDALONE_MACROS = ("UTEST_MAIN", "UTEST_STATE")
#: The parser's name in results.
PARSER = "tree-sitter-c"

_parser = None


def parser():
    """The shared tree-sitter-c parser, made on first use."""
    global _parser
    if _parser is None:
        import tree_sitter_c
        from tree_sitter import Language, Parser

        _parser = Parser(Language(tree_sitter_c.language()))
    return _parser


def parse(data: bytes):
    return parser().parse(data)


def walk(node) -> Iterator:
    """``node`` and every node below it, in source order."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def _call_name(statement) -> Optional[str]:
    """The function a top-level expression statement calls, or None."""
    call = statement.named_children[0] if statement.named_children else None
    if call is None or call.type != "call_expression":
        return None
    function = call.child_by_field_name("function")
    return function.text.decode("utf-8", "replace") if function is not None else None


def _arguments(statement) -> List[str]:
    call = statement.named_children[0]
    arguments = call.child_by_field_name("arguments")
    return [child.text.decode("utf-8", "replace") for child in arguments.named_children]


@dataclass(frozen=True)
class MacroUnit:
    """A top-level utest.h test or fixture block: the macro, its arguments
    and the byte span of the macro head and of the whole unit."""

    macro: str
    arguments: Tuple[str, ...]
    head: Tuple[int, int]
    span: Tuple[int, int]

    @property
    def name(self) -> str:
        """The test's name as utest.h lists it, without an index."""
        return ".".join(self.arguments[:2])


def macro_units(data: bytes, tree=None) -> List[MacroUnit]:
    """The top-level utest.h tests and fixture blocks of ``data``."""
    tree = tree if tree is not None else parse(data)
    children = tree.root_node.children
    units = []
    for index, node in enumerate(children):
        if node.type != "expression_statement":
            continue
        name = _call_name(node)
        if name not in TEST_MACROS + FIXTURE_MACROS:
            continue
        block = children[index + 1] if index + 1 < len(children) else None
        if block is None or block.type != "compound_statement":
            continue
        call = node.named_children[0]
        units.append(MacroUnit(name, tuple(_arguments(node)),
                               (call.start_byte, call.end_byte),
                               (node.start_byte, block.end_byte)))
    return units


def call_lines(data: bytes, names) -> List[int]:
    """The lines of ``data`` with a call of a function or macro in
    ``names``, read from the parse of its ``masked`` text."""
    if not names:
        return []
    lines = set()
    for node in walk(parse(masked(data)).root_node):
        function = node.child_by_field_name("function") if node.type == "call_expression" else None
        if function is not None and function.text.decode("utf-8", "replace") in names:
            lines.add(node.start_point[0] + 1)
    return sorted(lines)


def declared_names(data: bytes) -> Set[str]:
    """The names ``data`` declares or defines anywhere: functions, variables
    (a local function pointer too) and parameters, read from the parse of
    its ``masked`` text."""
    names = set()
    for node in walk(parse(masked(data)).root_node):
        if node.type not in ("function_definition", "declaration", "parameter_declaration"):
            continue
        for declarator in node.children_by_field_name("declarator"):
            while declarator is not None and declarator.type != "identifier":
                inner = declarator.child_by_field_name("declarator")
                if inner is None and declarator.type == "parenthesized_declarator":
                    inner = next(iter(declarator.named_children), None)
                declarator = inner
            if declarator is not None:
                names.add(declarator.text.decode("utf-8", "replace"))
    return names


def masked(data: bytes, tree=None) -> bytes:
    """``data`` with each top-level utest.h macro head replaced by a plain C
    declaration of the same length (``int u()`` padded with spaces), and each
    standalone macro statement by ``int m(void);`` padded, so that a parser
    reads the file as C without macros."""
    tree = tree if tree is not None else parse(data)
    out = bytearray(data)
    for unit in macro_units(data, tree):
        start, end = unit.head
        out[start:end] = _padded(b"int u()", end - start)
        # The statement that ended after the call holds no semicolon: the
        # block follows the head directly.
    for node in tree.root_node.children:
        if node.type == "expression_statement" and _call_name(node) in STANDALONE_MACROS:
            start, end = node.start_byte, node.end_byte
            out[start:end] = _padded(b"int m(void);", end - start)
    return bytes(out)


def _padded(text: bytes, length: int) -> bytes:
    if length < len(text):
        # A macro head is never shorter than its shortest form, UTEST(a,b).
        return b" " * length
    return text + b" " * (length - len(text))


@dataclass(frozen=True)
class SyntaxProblem:
    """The first syntax error: its line and column, from 1, and a message."""

    line: int
    column: int
    message: str


def first_error(data: bytes, *, test_file: bool) -> Optional[SyntaxProblem]:
    """The first syntax error of ``data``, or None. A test file is read with
    its utest.h macros masked."""
    tree = parse(data)
    if test_file:
        tree = parse(masked(data, tree))
    if not tree.root_node.has_error:
        return None
    for node in walk(tree.root_node):
        if node.type == "ERROR" or node.is_missing:
            line, column = node.start_point[0] + 1, node.start_point[1] + 1
            what = f"missing {node.type}" if node.is_missing else "syntax error"
            return SyntaxProblem(line, column, f"{what} at line {line}:{column}")
    return SyntaxProblem(1, 1, "syntax error at line 1:1")


def top_functions(data: bytes, tree=None) -> List[Tuple[str, object]]:
    """Each top-level function definition of ``data`` with its name."""
    tree = tree if tree is not None else parse(data)
    found = []
    for node in tree.root_node.children:
        if node.type != "function_definition":
            continue
        declarator = node.child_by_field_name("declarator")
        while declarator is not None and declarator.type in ("pointer_declarator",
                                                             "parenthesized_declarator"):
            declarator = declarator.child_by_field_name("declarator") or (
                declarator.named_children[0] if declarator.named_children else None)
        if declarator is None or declarator.type != "function_declarator":
            continue
        name = declarator.child_by_field_name("declarator")
        if name is not None and name.type == "identifier":
            found.append((name.text.decode("utf-8", "replace"), node))
    return found


def defines_function(data: bytes, name: str) -> bool:
    """Whether ``data`` defines the top-level function ``name``."""
    return any(found == name for found, _ in top_functions(data))


def has_main(data: bytes) -> bool:
    """Whether ``data`` has a main: ``UTEST_MAIN()`` anywhere, also inside a
    conditional, or a function named ``main``."""
    return defines_function(data, "main") or any(
        node.type == "expression_statement" and _call_name(node) == "UTEST_MAIN"
        for node in walk(parse(data).root_node))


_INCLUDE = re.compile(rb'^\s*#\s*include\s*"([^"]+)"', re.MULTILINE)


def quoted_includes(data: bytes) -> List[str]:
    """The files ``data`` includes with quotes, in order."""
    return [match.group(1).decode("utf-8", "replace") for match in _INCLUDE.finditer(data)]


def char_offset(data: bytes, offset: int) -> int:
    """The character offset of byte ``offset`` of UTF-8 ``data``."""
    return len(data[:offset].decode("utf-8", "replace"))
