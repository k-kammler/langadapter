"""Static analysis and rewriting of Python test sources, running no project.

The operations parse with tree-sitter's Python grammar or ``ast``, check with
the calling interpreter's compiler and start no process. Python binds
definitions in source order, so a later definition hides an earlier one of the
same name and scope, and pytest cannot collect tests held only by the earlier
one: the normalization removes those. The syntax repair judges each candidate
with the caller's validator, which should use the project's interpreter.
``PythonAdapter`` holds the rewriting that also validates with the project's
interpreter. The caller chooses the owner of a conflicting name.
"""
from __future__ import annotations

import ast
import hashlib
from collections import defaultdict
from dataclasses import replace, dataclass
from typing import Dict, List, Sequence, Tuple, Callable, Iterable, Optional
from langadapter import templates, sources as _lines
from langadapter.sources import (
    RemovedBinding,
    SuiteNormalization,
    APPLIED,
    PARSE_ERROR,
    REFUSED,
    CollisionResolution,
    SourceResult,
    SourceValidation,
)
from langadapter.python import _syntax, _collisions, _sources as _treesitter
from langadapter.errors import InvalidRequest
import re


RULE_VERSION = "python-last-binding-v2"
"""The rule ``normalize_shadowed_definitions`` applies, which its records
name."""

_DEFINITION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _span(node: ast.AST) -> Tuple[int, int]:
    start = int(getattr(node, "lineno", 0) or 0)
    decorators = getattr(node, "decorator_list", ())
    if decorators:
        start = min(start, *(int(d.lineno) for d in decorators))
    return start, int(getattr(node, "end_lineno", start) or start)


def _kind(node: ast.AST) -> str:
    if isinstance(node, ast.AsyncFunctionDef):
        return "async_function"
    if isinstance(node, ast.FunctionDef):
        return "function"
    return "class"


def _include_attached_comments(
    source: str, binding: RemovedBinding,
) -> RemovedBinding:
    """The removed definition extended over its directly attached comment block."""
    lines = [_lines.bare(line) for line in _lines.lines(source)]
    start = binding.removed_start_line
    if not lines or start <= 1 or start > len(lines):
        return binding
    definition_line = lines[start - 1]
    indent = definition_line[:len(definition_line) - len(definition_line.lstrip())]
    cursor = start - 1
    while cursor > 0:
        previous = lines[cursor - 1]
        if not previous.strip() or not previous.startswith(indent):
            break
        remainder = previous[len(indent):]
        if not remainder.startswith("#"):
            break
        cursor -= 1
    # A comment block beginning the module is conservatively a module header.
    if cursor == 0 and not binding.scope:
        return binding
    if cursor == start - 1:
        return binding
    return replace(binding, removed_start_line=cursor + 1)


def _shadowed_in_body(
    body: Sequence[ast.stmt],
    scope: Tuple[str, ...],
) -> List[RemovedBinding]:
    by_name: Dict[str, List[ast.AST]] = defaultdict(list)
    for node in body:
        if isinstance(node, _DEFINITION_TYPES):
            by_name[node.name].append(node)

    removed: List[RemovedBinding] = []
    retained_nodes = set()
    for name, nodes in by_name.items():
        retained = nodes[-1]
        retained_nodes.add(id(retained))
        retained_start, retained_end = _span(retained)
        for node in nodes[:-1]:
            removed_start, removed_end = _span(node)
            removed.append(RemovedBinding(
                scope=scope,
                name=name,
                kind=_kind(node),
                removed_start_line=removed_start,
                removed_end_line=removed_end,
                retained_start_line=retained_start,
                retained_end_line=retained_end,
            ))

    for node in body:
        if isinstance(node, ast.ClassDef) and id(node) in retained_nodes:
            removed.extend(_shadowed_in_body(node.body, scope + (node.name,)))
    return removed


def normalize_shadowed_definitions(source: str) -> SuiteNormalization:
    """Remove definitions hidden by a later one of the same name in the same
    scope, recording each removed span and the rule version. A source that
    does not parse comes back unchanged with its parse error."""
    before = _sha256(source)
    try:
        tree = _syntax.parse(source)
    except SyntaxError as exc:
        message = f"{exc.msg} (line {exc.lineno}, column {exc.offset})"
        return SuiteNormalization(
            source=source,
            changed=False,
            parse_ok=False,
            parse_error=message,
            removed_bindings=(),
            before_sha256=before,
            after_sha256=before,
            rule_version=RULE_VERSION,
        )

    removed = sorted(
        _shadowed_in_body(tree.body, ()),
        key=lambda item: (item.removed_start_line, item.removed_end_line),
    )
    if not removed:
        return SuiteNormalization(
            source=source,
            changed=False,
            parse_ok=True,
            parse_error=None,
            removed_bindings=(),
            before_sha256=before,
            after_sha256=before,
            rule_version=RULE_VERSION,
        )

    removed = [_include_attached_comments(source, item) for item in removed]
    dead_lines = set()
    for item in removed:
        dead_lines.update(range(
            item.removed_start_line,
            item.removed_end_line + 1,
        ))
    # Numbered as ast numbers lines, not by str.splitlines.
    lines = _lines.lines(source)
    normalized = "".join(
        line for number, line in enumerate(lines, 1)
        if number not in dead_lines
    )
    after = _sha256(normalized)
    return SuiteNormalization(
        source=normalized,
        changed=True,
        parse_ok=True,
        parse_error=None,
        removed_bindings=tuple(removed),
        before_sha256=before,
        after_sha256=after,
        rule_version=RULE_VERSION,
    )


def count_code_lines(source: str) -> int:
    """lizard's lines of code: the lines that hold code, without blank lines,
    comments and docstrings, counted without a parse tree, so also for
    incomplete source. It is not a statement count or a coverage
    denominator."""
    from langadapter import _lizard

    return _lizard.code_lines_and_complexities(source, ".py")[0]


@dataclass(frozen=True)
class SkipRemoval:
    """Module-scope guards removed, or an explicit host-grammar parse error."""

    source: str
    count: int
    parsed: bool
    error: str = ""


def strip_module_level_skips(source: str) -> SkipRemoval:
    """Remove each top-level expression or assignment statement that calls an
    ``importorskip`` attribute, or a ``skip`` attribute with
    ``allow_module_level=True``. Guards inside a module-level ``if`` or ``try``
    and calls in tests stay. The result is not validated."""
    try:
        tree = _syntax.parse(source)
    except SyntaxError as exc:
        return SkipRemoval(source, 0, False, str(exc))
    drop = set()
    removed = 0
    for node in tree.body:
        value = node.value if isinstance(node, (ast.Expr, ast.Assign)) else None
        if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Attribute):
            continue
        attr = value.func.attr
        hit = attr == "importorskip" or (
            attr == "skip" and any(kw.arg == "allow_module_level"
                                   and getattr(kw.value, "value", False) is True
                                   for kw in value.keywords))
        if hit:
            removed += 1
            drop.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    if not removed:
        return SkipRemoval(source, 0, True)
    kept = [line for i, line in enumerate(_lines.lines(source), 1)
            if i not in drop]
    return SkipRemoval("".join(kept), removed, True)


def merge_sources(first: str, second: str, *, separator: str) -> SourceResult:
    """Merge two modules: the imports of both, deduplicated and hoisted, then
    the first body, the ``separator`` comment line and the second body. A
    statement both hold identically appears once when it only binds names and
    nothing run between the copies can change them. Parsed with
    tree-sitter and checked with the calling interpreter's compiler. Validation
    with the project's interpreter is the caller's. Raises ``InvalidRequest``
    unless ``separator`` is one comment line."""
    if not separator.startswith("#") or "\n" in separator or "\r" in separator:
        raise InvalidRequest("separator", "must be one comment line")
    parser = "tree-sitter-python and compiler of the calling process"
    for label, text in (("the first source", first), ("the second source", second)):
        if _treesitter.parse("tree_sitter_python", text).root_node.has_error:
            return SourceResult(PARSE_ERROR, first, parser, detail=f"{label} does not parse")
    merged = _treesitter.merge_python_modules(first, second, sentinel=separator)
    if merged is None:
        return SourceResult(REFUSED, first, parser,
                            detail="the calling process's compiler rejects the merged module")
    return SourceResult(APPLIED, merged, parser)


def collision_dependents(
    source: str, binding: str, test_names: Iterable[str],
) -> Tuple[str, ...]:
    """The tests of ``test_names`` that depend on ``binding``, also through its
    class methods, by the analysis collision rewriting uses. It does not prove
    a dynamic dependency. Raises ``InvalidRequest`` for an invalid source."""
    normalized = normalize_shadowed_definitions(source)
    valid, message = _collisions._py_parse_valid(normalized.source)
    if not valid:
        raise InvalidRequest("source", message)
    root = _treesitter.parse(_collisions._GRAMMAR, normalized.source).root_node
    bindings = {}
    for found in _collisions._module_bindings(root):
        bindings.setdefault(found.name, []).append(found)
    return tuple(sorted(_collisions._dependent_test_names(
        root, bindings, binding, test_names)))


def resolve_collisions(
    sources: Sequence[str], *, localize_autouse: bool = False,
    preference: Optional[Callable[[str, Tuple[int, ...]], Optional[int]]] = None,
) -> CollisionResolution:
    """Rewrite static collisions between ``sources`` (left unmodified), leaving
    undecided conflicts intact. Each source is first cleared of definitions
    shadowed in its own scope, listed in ``dropped_defs``, so a source without
    collisions can still be rewritten. ``preference(name, owners)`` gets the
    original source indices and names the owner of an unrenamable binding, or
    None to leave it unresolved. Another index raises ``InvalidRequest``.
    Validate a rewrite with the interpreter it is delivered to."""
    def choose(name, owners):
        selected = preference(name, owners) if preference is not None else None
        if selected is not None and selected not in owners:
            raise InvalidRequest("preference", f"{selected!r} is not an owner of {name!r}")
        return selected

    return _collisions.resolve_python_collisions(
        sources, localize_autouse=localize_autouse, choose=choose)


@dataclass(frozen=True)
class SyntaxRepair:
    """The repaired ``source``, or None when repair or validation failed.
    ``status`` is valid, repaired, beyond_repair or validator_failed,
    ``validation`` the last validation (possibly an execution failure that
    judges no code) and ``repairs`` the edits applied before it."""

    source: Optional[str]
    status: str
    repairs: int
    validation: SourceValidation


def salvage_syntax(
    source: str, validate: Callable[[str], SourceValidation],
) -> SyntaxRepair:
    """Repair ``source`` in at most 25 validation rounds, guided only by syntax
    errors: an "expected an indented block" error gets a ``pass`` body under the
    line it names, another error blanks its line. A validator execution failure
    stops the repair without a syntax verdict."""
    result = templates.repair_syntax(source, validate, _local_fix)
    status = "beyond_repair" if result.status == templates.EXHAUSTED else result.status
    return SyntaxRepair(result.source, status, result.repairs, result.validation)


def _local_fix(lines: List[str], validation: SourceValidation) -> bool:
    """Apply the fix of one syntax error (``salvage_syntax``) to ``lines``,
    numbered as the interpreter numbers them. False when it cannot."""
    lineno, message = validation.line, validation.message
    if not lineno or lineno > len(lines):
        return False
    match = re.search(r"expected an indented block after .* on line (\d+)",
                      message or "")
    if match:
        opener = int(match.group(1))
        if opener > len(lines):
            return False
        head = lines[opener - 1]
        indent = head[:len(head) - len(head.lstrip())]
        lines.insert(opener, f"{indent}    pass\n")
    else:
        lines[lineno - 1] = "\n"
    return True
