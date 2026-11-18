"""The tree-sitter substrate of the language-independent static analysis.

One grammar-parameterized implementation: the tree-sitter adapters
(Python's ``PythonSources`` and the Java source operations) pass a grammar module
and their language's node kinds (functions, classes, control flow, calls,
ignored nodes). Nothing here branches on a language name, so a new language
is a grammar binding on its adapter, not new analysis code.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Any, Callable, Dict, FrozenSet, Iterable, List, Optional, Tuple
import bisect
import importlib
import re
from collections import Counter
from dataclasses import dataclass
from importlib import metadata
from functools import lru_cache
from langadapter import _lizard
from langadapter.sources import Edit, _edited, whole

if TYPE_CHECKING:
    import tree_sitter


def parser_identity(grammar_distribution: str) -> str:
    """The structural parser and its grammar, by their distributions' versions,
    such as ``tree-sitter 0.23.2; tree-sitter-java 0.23.5``. A distribution
    that is not installed reads "unknown"."""
    def version(distribution: str) -> str:
        try:
            return metadata.version(distribution)
        except metadata.PackageNotFoundError:
            return "unknown"
    return f"tree-sitter {version('tree-sitter')}; {grammar_distribution} {version(grammar_distribution)}"


class StructureResult(NamedTuple):
    """The result of the structural check of a source, as this substrate
    and the Python implementation return it. ``line`` and ``col`` (1-based,
    0 when ``ok``) let a caller show the error without parsing ``message``,
    which names the error and its position. ``snippet`` is the offending
    node's text, stripped and whole."""

    ok: bool
    message: str
    line: int
    col: int
    snippet: str = ""


@lru_cache(maxsize=None)
def _language(grammar_module: str) -> "tree_sitter.Language":
    """The cached ``tree_sitter.Language`` of a grammar package such as
    ``"tree_sitter_python"``, from its ``language()`` capsule (tree-sitter
    0.22 and later). tree-sitter loads here, at the first parse, not when
    LangAdapter is imported."""
    import tree_sitter

    module = importlib.import_module(grammar_module)
    return tree_sitter.Language(module.language())


@lru_cache(maxsize=None)
def _parser(grammar_module: str) -> "tree_sitter.Parser":
    import tree_sitter

    return tree_sitter.Parser(_language(grammar_module))


def parse(grammar_module: str, source: str) -> "tree_sitter.Tree":
    """The tree of ``source`` in the grammar. Malformed input raises
    nothing: its tree holds ERROR or missing nodes (``check_structure``)."""
    return _parser(grammar_module).parse(source.encode("utf-8"))


def walk(node: "tree_sitter.Node"):
    """Every node under ``node``, named and anonymous, depth first and
    parent before children."""
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.children))


def non_comment_leaf_fingerprint(
    grammar_module: str,
    source: str,
    *,
    comment_kinds: "FrozenSet[str]",
) -> Tuple[Tuple[str, bytes], ...]:
    """The non-comment leaf tokens as (kind, text) pairs, independent of
    whitespace, or () when ``source`` does not parse."""
    root = parse(grammar_module, source).root_node
    if root.has_error:
        return ()
    return tuple(
        (node.type, bytes(node.text))
        for node in walk(root)
        if node.child_count == 0 and node.type not in comment_kinds
    )


def name(node: "tree_sitter.Node") -> str:
    """A definition's name: the text of its ``name`` field, else of the first
    identifier below its ``declarator`` field or below the node itself,
    else ``<anonymous>``."""
    n = node.child_by_field_name("name")
    if n is not None:
        return n.text.decode("utf-8", "replace")
    decl = node.child_by_field_name("declarator")
    search_root = decl if decl is not None else node
    for d in walk(search_root):
        if d is not search_root and d.type == "identifier":
            return d.text.decode("utf-8", "replace")
    return "<anonymous>"


def _collect_defs(
    node: "tree_sitter.Node",
    *,
    func_kinds: "FrozenSet[str]",
    class_kinds: "FrozenSet[str]",
    prefix: str,
    out: list,
) -> None:
    for child in node.named_children:
        if child.type in class_kinds or child.type in func_kinds:
            qual = f"{prefix}{name(child)}"
            out.append(qual)
            _collect_defs(
                child,
                func_kinds=func_kinds, class_kinds=class_kinds,
                prefix=f"{qual}.", out=out,
            )
        else:
            _collect_defs(
                child,
                func_kinds=func_kinds, class_kinds=class_kinds,
                prefix=prefix, out=out,
            )


def skeleton(
    grammar_module: str,
    source: str,
    *,
    func_kinds: "FrozenSet[str]",
    class_kinds: "FrozenSet[str]",
) -> Tuple[str, ...]:
    """Ordered tuple of the qualified class and function names, or ()
    when the source does not parse. ``diff_skeletons`` compares two of
    them to find a dropped, renamed, added or reordered definition."""
    root = parse(grammar_module, source).root_node
    if root.has_error:
        return ()
    out: list = []
    _collect_defs(
        root,
        func_kinds=func_kinds, class_kinds=class_kinds,
        prefix="", out=out,
    )
    return tuple(out)


def diff_skeletons(
    a: Tuple[str, ...], b: Tuple[str, ...],
) -> Tuple[bool, str]:
    """``(equal, reason)`` for two skeletons, each the ordered qualified names
    of a source's classes and functions: the definitions ``b`` removed,
    else those it added, else the first position where the order differs.
    The reason is the text a structure check reports."""
    if a == b:
        return True, ""
    sa, sb = set(a), set(b)
    removed = [n for n in a if n not in sb]
    added = [n for n in b if n not in sa]
    if removed:
        return False, f"definition(s) removed: {', '.join(removed[:5])}"
    if added:
        return False, f"definition(s) added: {', '.join(added[:5])}"
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return False, f"definition order changed at position {i}: {x!r} vs {y!r}"
    return False, "skeleton differs"


def _fingerprint(node: "tree_sitter.Node", ignore_kinds: "FrozenSet[str]") -> str:
    # Every child, named or not: an operator such as ``+`` is an anonymous
    # child, and a change of it is a change of the code.
    kids = [c for c in node.children if c.type not in ignore_kinds]
    if not kids:
        return f"{node.type}:{node.text.decode('utf-8', 'replace')}"
    return f"({node.type} " + " ".join(
        _fingerprint(c, ignore_kinds) for c in kids
    ) + ")"


def function_fingerprints(
    grammar_module: str,
    source: str,
    *,
    func_kinds: "FrozenSet[str]",
    class_kinds: "FrozenSet[str]",
    ignore_kinds: "FrozenSet[str]" = frozenset(),
) -> "dict":
    """``{qualified_name: fingerprint}`` for every function definition, or
    {} when the source does not parse. A fingerprint is the tree of the
    definition's tokens without ``ignore_kinds``: it ignores formatting (and
    comments, when those are ignored) and changes with any change of code,
    a literal or an operator. Qualified keys (``Class.method``) keep
    same-named methods apart."""
    root = parse(grammar_module, source).root_node
    if root.has_error:
        return {}
    fps: dict = {}

    def _rec(n: "tree_sitter.Node", prefix: str) -> None:
        for child in n.named_children:
            if child.type in class_kinds:
                qual = f"{prefix}{name(child)}"
                _rec(child, f"{qual}.")
            elif child.type in func_kinds:
                qual = f"{prefix}{name(child)}"
                fps[qual] = _fingerprint(child, ignore_kinds)
                _rec(child, f"{qual}.")
            else:
                _rec(child, prefix)

    _rec(root, "")
    return fps


def _is_error(node: "tree_sitter.Node") -> bool:
    return node.is_error or node.is_missing


def _first_error(root: "tree_sitter.Node") -> "tree_sitter.Node | None":
    """The leftmost ERROR or missing node below ``root``, whose line a
    message can name (``has_error`` only says that one exists)."""
    stack = [root]
    while stack:
        node = stack.pop()
        if node is not root and _is_error(node):
            return node
        stack.extend(reversed(node.children))
    return None


def check_structure(grammar_module: str, source: str) -> StructureResult:
    """Whether ``source`` parses: ``ok`` is False when its tree holds an
    ERROR or missing node. ``message`` names the kind and the 1-based
    line:col, also given as ``line`` and ``col`` (0 when ``ok``).
    ``snippet`` is the offending node's text, stripped and whole up to the
    safety bound, possibly the rest of the file: the caller cuts it."""
    root = parse(grammar_module, source).root_node
    if not root.has_error and not _is_error(root):
        return StructureResult(True, "", 0, 0)
    bad = _first_error(root) or root
    line = bad.start_point[0] + 1
    col = bad.start_point[1] + 1
    snippet = whole(bad.text.decode("utf-8", "replace").strip(), "offending source")
    kind = "missing token" if bad.is_missing else "syntax error"
    return StructureResult(False, f"{kind} at line {line}:{col}", line, col, snippet)


def structure_message(result: Any) -> str:
    """A structure check as the adapters' reports write it: the message and
    the offending node's first line, whole, or "" when it parsed."""
    if result.ok:
        return ""
    first = ((result.snippet or "").splitlines() or [""])[0]
    return f"{result.message}: {first!r}"


# Structural metrics


def _direct_callee_name(call_node: "tree_sitter.Node") -> str:
    """A call node's callee name, its trailing dotted segment (``self.f()``
    and ``obj.f()`` give ``f``), from the ``function`` field (Python) or the
    ``name`` field (Java), or "" when it is neither an identifier nor an
    attribute."""
    fn = call_node.child_by_field_name("function")
    if fn is None:
        fn = call_node.child_by_field_name("name")
    if fn is None:
        return ""
    if fn.type == "identifier":
        return fn.text.decode("utf-8", "replace")
    if fn.type == "attribute":
        attr = fn.child_by_field_name("attribute")
        if attr is not None:
            return attr.text.decode("utf-8", "replace")
    return ""


def called_names(grammar_module: str, source: str, *,
                 call_kinds: FrozenSet[str]) -> Optional[Dict[str, int]]:
    """The call sites of ``source`` counted by their callee's trailing name
    (``_direct_callee_name``), or None when ``source`` does not parse."""
    root = parse(grammar_module, source).root_node
    if root.has_error:
        return None
    names = Counter(_direct_callee_name(node) for node in walk(root) if node.type in call_kinds)
    names.pop("", None)
    return dict(names)


def _max_loop_nesting(
    root: "tree_sitter.Node",
    loop_kinds: "FrozenSet[str]",
) -> int:
    """Deepest stack of loop-kind ancestors anywhere under ``root``."""
    deepest = 0

    def _recurse(node: "tree_sitter.Node", depth: int) -> None:
        nonlocal deepest
        if node.type in loop_kinds:
            depth += 1
            if depth > deepest:
                deepest = depth
        for child in node.children:
            _recurse(child, depth)

    _recurse(root, 0)
    return deepest


def _text(node: "tree_sitter.Node") -> str:
    """The source text of ``node``."""
    return node.text.decode("utf-8")


def _own_complexity(node: "tree_sitter.Node", lizard_suffix: str) -> Optional[int]:
    """lizard's cyclomatic complexity of the function ``node`` declares:
    the function lizard reports for the declaration's own text under its
    bare name, never one nested in it, or None where lizard reports no such
    function. lizard reports none for a Java method declared without a
    body (abstract, interface) or a Python def whose body is on its
    signature's line (``def f(): ...``), and loses a Python function that
    holds such a def with a body."""
    wanted = name(node)
    return next((complexity for found, complexity in _lizard.functions(_text(node), lizard_suffix)
                 if found == wanted), None)


def optimization_metrics(
    grammar_module: str,
    source: str,
    *,
    func_kinds: "FrozenSet[str]",
    loop_kinds: "FrozenSet[str]",
    call_kinds: "FrozenSet[str]",
    lizard_suffix: str,
    class_kinds: "FrozenSet[str]" = frozenset(),
    io_call_names: "FrozenSet[str]" = frozenset(),
) -> Dict[str, Any]:
    """The eight structural counters of ``source``, each None when it does
    not parse (an empty source has zero counts). ``cyclomatic_sum`` is
    lizard's sum of the complexities of the functions it finds, and
    ``nloc`` lizard's count of the lines of code (``_lizard``, the language
    a file ending in ``lizard_suffix`` has). lizard finds no function in a
    Java method declared without a body or a Python def whose body is on
    its signature's line, and module-level statements count toward none.
    ``loop_count`` counts the loops of ``loop_kinds`` (for, while, do-while,
    Java's enhanced for, Python's comprehension for-clauses) and
    ``loop_max_nesting`` is their deepest nesting.
    ``recursive_function_count`` counts the functions with a call whose
    trailing dotted name is their own (``self.f()`` and ``f()`` both count
    for ``def f``). ``function_count`` counts function and method
    definitions, ``class_count`` class, interface and record definitions.
    ``io_call_count``, a rough hint, counts the calls whose trailing name is
    in ``io_call_names`` (file, socket and subprocess primitives)."""
    root = parse(grammar_module, source).root_node
    if root.has_error:
        return dict.fromkeys((
            "cyclomatic_sum", "loop_count", "loop_max_nesting",
            "recursive_function_count", "function_count", "class_count",
            "io_call_count", "nloc",
        ))
    return _node_metrics(
        root, source, func_kinds=func_kinds, loop_kinds=loop_kinds,
        call_kinds=call_kinds, class_kinds=class_kinds,
        io_call_names=io_call_names, lizard_suffix=lizard_suffix,
    )


def _node_metrics(
    root: "tree_sitter.Node", text: str, *,
    func_kinds: FrozenSet[str], loop_kinds: FrozenSet[str],
    call_kinds: FrozenSet[str], class_kinds: FrozenSet[str],
    io_call_names: FrozenSet[str], lizard_suffix: str,
) -> Dict[str, int]:
    """The eight structural counters over one parse-tree subtree, whose
    source is ``text``: lizard's lines of code and complexity sum of that
    text, the other counters from the tree."""
    out = {
        "cyclomatic_sum": 0,
        "loop_count": 0,
        "loop_max_nesting": 0,
        "recursive_function_count": 0,
        "function_count": 0,
        "class_count": 0,
        "io_call_count": 0,
        "nloc": 0,
    }
    for n in walk(root):
        if n.type in func_kinds:
            out["function_count"] += 1
            fname = name(n)
            if fname and fname != "<anonymous>":
                for d in walk(n):
                    if d is n or d.type not in call_kinds:
                        continue
                    if _direct_callee_name(d) == fname:
                        out["recursive_function_count"] += 1
                        break
        elif class_kinds and n.type in class_kinds:
            out["class_count"] += 1

    out["loop_count"] = sum(1 for n in walk(root) if n.type in loop_kinds)
    out["loop_max_nesting"] = _max_loop_nesting(root, loop_kinds)
    out["nloc"], complexities = _lizard.code_lines_and_complexities(text, lizard_suffix)
    out["cyclomatic_sum"] = sum(complexities)

    # Trailing-name matching over-approximates (an unrelated method named
    # like an IO primitive counts), so the vocabulary keeps to specific,
    # rarely overloaded names. The count is a hint, not a measurement.
    if io_call_names:
        out["io_call_count"] = sum(
            1 for n in walk(root)
            if n.type in call_kinds
            and _direct_callee_name(n) in io_call_names
        )
    return out


@dataclass(frozen=True)
class FunctionMetrics:
    """One function declaration, with lines of the original source.
    ``counts`` are the file counters over its subtree, nested declarations
    included, and ``cyclomatic_complexity`` is lizard's for the function
    alone, its nested functions' decisions left out, or None where lizard
    reports no function for the declaration (``_own_complexity``).
    ``start_column``, a 1-based UTF-8 byte column, tells apart declarations
    whose line-only scope keys would be ambiguous."""

    name: str
    start_line: int
    end_line: int
    start_column: int
    signature: Optional[str]
    counts: Tuple[Tuple[str, int], ...]
    cyclomatic_complexity: Optional[int]


@dataclass(frozen=True)
class ScopedMetrics:
    file: Tuple[Tuple[str, int], ...]
    functions: Tuple[FunctionMetrics, ...]


def scoped_optimization_metrics(
    grammar_module: str, source: str, *,
    func_kinds: FrozenSet[str], loop_kinds: FrozenSet[str],
    call_kinds: FrozenSet[str], lizard_suffix: str,
    class_kinds: FrozenSet[str] = frozenset(),
    io_call_names: FrozenSet[str] = frozenset(),
    signature: Optional[Callable[["tree_sitter.Node"], Optional[str]]] = None,
) -> Optional[ScopedMetrics]:
    """The file's and each function's counters from one tree-sitter parse,
    or None when ``source`` does not parse (a source without functions has
    an empty tuple). lizard reads each function's own text for its
    complexity and lines of code. Names
    are qualified by the enclosing classes and functions, repeated names
    stay separate, and ``signature`` gives a language's native signature.
    The counters establish neither semantic validity nor call-graph
    recursion."""
    root = parse(grammar_module, source).root_node
    if root.has_error:
        return None
    options = dict(
        func_kinds=func_kinds, loop_kinds=loop_kinds,
        call_kinds=call_kinds, class_kinds=class_kinds,
        io_call_names=io_call_names, lizard_suffix=lizard_suffix,
    )
    functions = []

    def collect(node, prefix):
        for child in node.named_children:
            if child.type in class_kinds or child.type in func_kinds:
                qual = f"{prefix}{name(child)}"
                if child.type in func_kinds:
                    functions.append(FunctionMetrics(
                        qual, child.start_point[0] + 1,
                        child.end_point[0] + (1 if child.end_point[1] else 0),
                        child.start_point[1] + 1,
                        signature(child) if signature is not None else None,
                        tuple(sorted(_node_metrics(child, _text(child), **options).items())),
                        _own_complexity(child, lizard_suffix),
                    ))
                collect(child, qual + ".")
            else:
                collect(child, prefix)

    collect(root, "")
    return ScopedMetrics(tuple(sorted(_node_metrics(root, source, **options).items())),
                         tuple(functions))


# Source-span helpers of the Python and Java source edits.


#: A run of blank lines, a line of spaces and tabs counted as blank.
_BLANK_RUN = re.compile(rb"(?m)^(?:[ \t]*\r?\n)+")


def apply_edits_collapsing(text: str, edits: Iterable[Edit], blank_lines: int) -> str:
    """``text`` with the byte-range ``edits`` applied (``sources.apply_edits``),
    and each run of more than ``blank_lines`` blank lines that an edit's
    start or end meets shortened to that many empty lines, with the run's
    line end. A run no edit meets, such as one inside a string literal,
    stays as it is. A cut is an edit with an empty replacement."""
    data, junctions = _edited(text, edits)
    out = bytearray()
    position = 0
    for run in _BLANK_RUN.finditer(data):
        start, end = run.span()
        nearest = bisect.bisect_left(junctions, start)
        lines = data[start:end].splitlines(keepends=True)
        if nearest == len(junctions) or junctions[nearest] > end or len(lines) <= blank_lines:
            continue
        ending = b"\r\n" if lines[0].endswith(b"\r\n") else b"\n"
        out += data[position:start] + ending * blank_lines
        position = end
    out += data[position:]
    return out.decode("utf-8")


def _cut_byte_spans(src_bytes: bytes, spans: List[Tuple[int, int]]) -> str:
    """``src_bytes`` without the ``(start, end)`` byte ranges, decoded: how
    imports are lifted out of a body and definitions removed."""
    if not spans:
        return src_bytes.decode("utf-8", "replace")
    out = bytearray()
    pos = 0
    for start, end in sorted(spans):
        out += src_bytes[pos:start]
        pos = end
    out += src_bytes[pos:]
    return out.decode("utf-8", "replace")


def _ordered_union(first: List[str], second: List[str]) -> List[str]:
    """``first`` followed by the members of ``second`` not already present
    (compared on stripped text), preserving order and dropping duplicates."""
    seen = {s.strip() for s in first}
    out = list(first)
    for item in second:
        if item.strip() not in seen:
            out.append(item)
            seen.add(item.strip())
    return out
