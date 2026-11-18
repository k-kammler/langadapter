"""The structure of a C source: its functions, their fingerprints and its
eight structural counters, as LangAdapter reads Python's and Java's.

The counters follow LangAdapter's rules. lizard counts the lines of code and
the complexity sum: the sum of the cyclomatic complexity of the functions it
finds, as the screening measured the C targets. tree-sitter-c's tree gives
the others: the loops and their deepest nesting, the functions that call
themselves, the functions, the record types defined with a body (struct,
union, enum), and the calls of file, socket and process primitives. A source
that does not parse has no counters, no functions and no fingerprints.

A test file's top-level utest.h macros are read as the functions they expand
to (``_syntax.masked``), so a test is a function named as utest.h names it
(``suite.name``), and a fixture block by its macro (``UTEST_F_SETUP(buffer)``).
"""
from __future__ import annotations

from importlib import metadata
from typing import Dict, List, Optional, Tuple

from langadapter.c import _syntax

#: The counters, as LangAdapter's structural metric provider names them.
NAMES = ("cyclomatic_sum", "loop_count", "loop_max_nesting", "recursive_function_count",
         "function_count", "class_count", "io_call_count", "nloc")
LOOP_KINDS = frozenset({"for_statement", "while_statement", "do_statement"})
#: The record types, counted where they are defined with a body.
RECORD_KINDS = frozenset({"struct_specifier", "union_specifier", "enum_specifier"})
#: File, socket and process primitives specific enough to read as IO. Generic
#: names (read, write, close, printf) match too much else and are left out.
IO_CALL_NAMES = frozenset({
    "fopen", "freopen", "fdopen", "open", "openat", "opendir", "socket", "connect",
    "accept", "recv", "recvfrom", "send", "sendto", "popen", "system", "fork",
    "execl", "execlp", "execle", "execv", "execvp", "execve",
})
#: The suffix that makes lizard read a text as C.
LIZARD_SUFFIX = ".c"


def parser_identity() -> str:
    """The parsers and their versions: tree-sitter with its C grammar, and
    lizard, whose counts are part of the counters' definition."""
    def version(distribution: str) -> str:
        try:
            return metadata.version(distribution)
        except metadata.PackageNotFoundError:
            return "unknown"
    return (f"tree-sitter {version('tree-sitter')}; tree-sitter-c {version('tree-sitter-c')}; "
            f"lizard {version('lizard')}")


def _read(source: str):
    """The source with its utest.h macros masked, its tree, and the names of
    the functions that stand for a macro, by their first byte."""
    data = source.encode("utf-8")
    tree = _syntax.parse(data)
    named = {}
    for unit in _syntax.macro_units(data, tree):
        named[unit.span[0]] = (unit.name if unit.macro in _syntax.TEST_MACROS
                               else f"{unit.macro}({', '.join(unit.arguments)})")
    masked = _syntax.masked(data, tree)
    if masked != data:
        data, tree = masked, _syntax.parse(masked)
    return data, tree, named


def _name(node, named: Dict[int, str]) -> Optional[str]:
    """A function definition's name: a utest.h macro's, else its declarator's
    identifier, else None when it has neither."""
    if node.start_byte in named:
        return named[node.start_byte]
    declarator = node.child_by_field_name("declarator")
    kind = node.child_by_field_name("type")
    if (kind is not None and declarator is not None
            and declarator.type == "parenthesized_declarator"
            and kind.text.decode("utf-8", "replace") in _syntax.FIXTURE_MACROS):
        # A fixture block with one argument, ``UTEST_F_SETUP(buffer) { ... }``,
        # parses as a definition typed by the macro, so masking leaves it.
        return kind.text.decode("utf-8", "replace") + declarator.text.decode("utf-8", "replace")
    while declarator is not None and declarator.type != "function_declarator":
        declarator = declarator.child_by_field_name("declarator") or (
            declarator.named_children[0] if declarator.named_children else None)
    if declarator is None:
        return None
    inner = declarator.child_by_field_name("declarator")
    return inner.text.decode("utf-8", "replace") if inner is not None else None


def _functions(source: str) -> Optional[List[Tuple[str, object]]]:
    """The named function definitions in source order, or None when the source
    does not parse."""
    _, tree, named = _read(source)
    if tree.root_node.has_error:
        return None
    found = []
    for node in _syntax.walk(tree.root_node):
        if node.type == "function_definition":
            name = _name(node, named)
            if name is not None:
                found.append((name, node))
    return found


def skeleton(source: str) -> Tuple[str, ...]:
    """The names of the functions, tests and fixture blocks in source order,
    or () when the source does not parse."""
    return tuple(name for name, _ in _functions(source) or ())


def same_skeleton(first: str, second: str) -> Tuple[bool, str]:
    """Whether two sources define the same functions in the same order, and
    why not, in LangAdapter's words for a skeleton's difference."""
    from langadapter.treesitter import diff_skeletons

    return diff_skeletons(skeleton(first), skeleton(second))


def _fingerprint(node) -> str:
    # Every child, named or not, without comments: an operator is an
    # anonymous child, and a change of it is a change of the code.
    kids = [child for child in node.children if child.type != "comment"]
    if not kids:
        return f"{node.type}:{node.text.decode('utf-8', 'replace')}"
    return f"({node.type} " + " ".join(_fingerprint(child) for child in kids) + ")"


def function_fingerprints(source: str) -> Dict[str, str]:
    """Each function's, test's and fixture block's fingerprint by its name:
    the tree of its tokens without comments, the same whatever the layout,
    and changed by any change of code, a literal or an operator. Empty when
    the source does not parse."""
    return {name: _fingerprint(node) for name, node in _functions(source) or ()}


def _callee(call) -> str:
    """A call's callee name, its trailing field (``s->f()`` and ``s.f()`` give
    ``f``), or "" for a call through an expression."""
    function = call.child_by_field_name("function")
    if function is None:
        return ""
    if function.type == "identifier":
        return function.text.decode("utf-8", "replace")
    if function.type == "field_expression":
        field = function.child_by_field_name("field")
        return field.text.decode("utf-8", "replace") if field is not None else ""
    return ""


def _loop_nesting(node, depth: int = 0) -> int:
    depth += node.type in LOOP_KINDS
    return max([depth, *(_loop_nesting(child, depth) for child in node.children)])


def metrics(source: str) -> Dict[str, Optional[int]]:
    """The eight counters of ``source``, each None when it does not parse."""
    from langadapter.sources import code_lines_and_complexities

    data, tree, named = _read(source)
    if tree.root_node.has_error:
        return dict.fromkeys(NAMES)
    nodes = list(_syntax.walk(tree.root_node))
    recursive = 0
    for node in nodes:
        if node.type != "function_definition" or node.start_byte in named:
            continue
        name = _name(node, named)
        if name and any(inner.type == "call_expression" and _callee(inner) == name
                        for inner in _syntax.walk(node)):
            recursive += 1
    nloc, complexities = code_lines_and_complexities(data.decode("utf-8", "replace"),
                                                     LIZARD_SUFFIX)
    return {
        "cyclomatic_sum": sum(complexities),
        "loop_count": sum(node.type in LOOP_KINDS for node in nodes),
        "loop_max_nesting": _loop_nesting(tree.root_node),
        "recursive_function_count": recursive,
        "function_count": sum(node.type == "function_definition" for node in nodes),
        "class_count": sum(node.type in RECORD_KINDS
                           and node.child_by_field_name("body") is not None for node in nodes),
        "io_call_count": sum(node.type == "call_expression" and _callee(node) in IO_CALL_NAMES
                             for node in nodes),
        "nloc": nloc,
    }
