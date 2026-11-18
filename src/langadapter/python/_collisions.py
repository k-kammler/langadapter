"""Module-scope collision resolution for test sources merged into one module.

Independently generated test files collide on module-scope names. The
engine rewrites them collision-free on tree-sitter-python's tree.
Its model is one namespace in which every identifier token of a name is a
use (Java's class bodies have their own binder, ``java/_collisions``, and
both apply the policy and edits of ``langadapter.sources``). The validity gate
is the language's own parser, stricter than tree-sitter.

- A definition whose text matches the kept copy, apart from surrounding
  whitespace, is dropped in favor of it, unless it refers to a name bound
  differently in another source. A module-level assignment is shared only
  when one copy cannot alias per-suite state (an immutable scalar, a
  pytest global read by name). ``cache = {}`` is renamed.
- Imports of one name from one origin are no collision. The merge keeps one.
- Any other colliding definition is renamed in all its identifier tokens,
  not in attribute accesses, call keywords or strings. A string resolving a
  module binding by name (``getfixturevalue("name")``, ``globals()["name"]``)
  makes the binding un-renamable. In Python a renamed top-level class name
  is also replaced in its own source's plain string literals.
- A dotted plain import (``import a.b.c``) cannot be alias-renamed. Of
  several un-renamable owners, each that loses the name drops the definition
  and the statements using it instead of the whole suite.

The name stays with its only un-renamable owner, the chosen one of several,
or else the first owner. Every other renamable owner gets the first free
``<name>_<k>``, k from 2.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Sequence, Set, Tuple

from langadapter.python import _sources as _treesitter, _syntax
from langadapter.sources import (
    bare, rows, Choose, Edit, apply_edits, fresh_name, CollisionResolution,
)


@dataclass(frozen=True)
class ImportBound:
    """One name an import binds at module scope. ``origin`` is ``(module,
    original_name)``: equal origins bind the same object."""

    name: str
    origin: Tuple[str, str]
    dotted: bool = False


#: tree-sitter-python's node kinds the engine reads beside the single ones
#: it names in place.
_FUNC_KINDS = frozenset({"function_definition"})
_CLASS_KINDS = frozenset({"class_definition"})
_ASSIGN_KINDS = frozenset({"assignment", "augmented_assignment"})
_IMPORT_KINDS = frozenset({
    "import_statement", "import_from_statement", "future_import_statement",
})
_CONTROL_FLOW_KINDS = frozenset({
    "if_statement", "try_statement", "with_statement",
    "for_statement", "while_statement",
})
#: Declarations binding a name in another scope (global, nonlocal).
_BINDING_DECL_KINDS = frozenset({"global_statement", "nonlocal_statement"})
_GRAMMAR = "tree_sitter_python"


# Generic tree-sitter helpers


def _node_line_span(node) -> Tuple[int, int]:
    """1-based inclusive line span of ``node``, ending at its last
    non-comment token: a block's range reaches over trailing comment lines
    that a parser without comments (such as the Python AST) does not count."""
    start = node.start_point[0] + 1
    end = start
    for d in _treesitter.walk(node):
        if d.child_count == 0 and d.type != "comment":
            end_row, end_col = d.end_point
            line = end_row if end_col == 0 else end_row + 1
            if line > end:
                end = line
    return start, end


def _ident_edit(node, repl: str) -> Edit:
    """The edit (``apply_edits``) replacing ``node``'s bytes by ``repl``."""
    return node.start_byte, node.end_byte, repl


def _is_attribute_member(node) -> bool:
    """Whether ``node`` is the member of an attribute access (``obj.NODE``)."""
    p = node.parent
    if p is None or p.type != "attribute":
        return False
    member = p.child_by_field_name("attribute")
    return member is not None and member.id == node.id


def _is_keyword_name(node) -> bool:
    """Whether ``node`` is a call's keyword (``f(NODE=...)``)."""
    p = node.parent
    if p is None or p.type != "keyword_argument":
        return False
    name = p.child_by_field_name("name")
    return name is not None and name.id == node.id


def _within_import(node) -> bool:
    p = node.parent
    while p is not None:
        if p.type in _IMPORT_KINDS:
            return True
        p = p.parent
    return False


def _contains(root, node) -> bool:
    return any(n.id == node.id for n in _treesitter.walk(root))


def _module_statements(root) -> List["object"]:
    """Module-scope statements, including those in top-level ``if``,
    ``try``, ``with`` and loops, not those in function or class bodies."""
    out: List["object"] = []

    def visit(stmt_nodes):
        for node in stmt_nodes:
            if node.type == "comment":
                continue
            out.append(node)
            if node.type in _CONTROL_FLOW_KINDS:
                for block in _control_blocks(node):
                    visit(list(block.named_children))

    visit(list(root.named_children))
    return out


def _control_blocks(node) -> List["object"]:
    """A control-flow node's own block and those of its ``elif``, ``else``,
    ``except`` and ``finally`` clauses."""
    blocks: List["object"] = []
    for child in node.named_children:
        if child.type == "block":
            blocks.append(child)
        elif child.type.endswith("_clause"):
            for inner in child.named_children:
                if inner.type == "block":
                    blocks.append(inner)
    return blocks


def _left_identifiers(left) -> List["object"]:
    """Identifier nodes under an assignment target, except attribute
    members: more than the bound names (``d`` and ``k`` of ``d[k] = v``)."""
    if left is None:
        return []
    return [n for n in _treesitter.walk(left)
            if n.type == "identifier" and not _is_attribute_member(n)]


def _referenced_names(text: str) -> Set[str]:
    """Every identifier a snippet mentions, except attribute members. It
    also holds the snippet's own binders (its def name, parameters), which
    only makes deduplication more cautious."""
    root = _treesitter.parse(_GRAMMAR, text).root_node
    return {n.text.decode("utf-8", "replace")
            for n in _treesitter.walk(root)
            if n.type == "identifier" and not _is_attribute_member(n)}


def _is_store(node) -> bool:
    """Whether an identifier occurrence binds the name (the AST's store
    context): a def or class name, a parameter, an assignment or loop
    target, or a scope declaration."""
    p = node.parent
    if p is None:
        return False
    if p.type in _FUNC_KINDS or p.type in _CLASS_KINDS:
        nm = p.child_by_field_name("name")
        if nm is not None and nm.id == node.id:
            return True
    if p.type in _BINDING_DECL_KINDS:
        return True
    anc = node.parent
    while anc is not None and anc.type != "block" \
            and anc.type not in _FUNC_KINDS:
        if anc.type == "parameters":
            return True
        anc = anc.parent
    anc = node
    while anc is not None and anc.type != "block":
        if anc.type in _ASSIGN_KINDS:
            left = anc.child_by_field_name("left")
            return left is not None and _contains(left, node) \
                and not _is_attribute_member(node)
        if anc.type == "for_statement":
            left = anc.child_by_field_name("left")
            return left is not None and _contains(left, node)
        anc = anc.parent
    return False


# Bindings


@dataclass
class _Binding:
    name: str
    stmt_id: int
    span: Tuple[int, int]
    kind: str           # "def" | "class" | "assign" | "import"
    dotted: bool = False
    origin: Optional[Tuple[str, str]] = None
    safe_to_share: bool = True   # may a byte-identical copy be deduplicated?


def _module_bindings(root) -> List[_Binding]:
    out: List[_Binding] = []
    for stmt in _module_statements(root):
        inner = stmt
        if stmt.type == "decorated_definition":
            inner = stmt.child_by_field_name("definition") or stmt
        span = _node_line_span(stmt)
        if inner.type in _FUNC_KINDS:
            out.append(_Binding(_name_of(inner), stmt.id, span, "def"))
        elif inner.type in _CLASS_KINDS:
            out.append(_Binding(_name_of(inner), stmt.id, span, "class"))
        elif inner.type == "expression_statement":
            for assign in inner.named_children:
                if assign.type in _ASSIGN_KINDS:
                    left = assign.child_by_field_name("left")
                    for idn in _left_identifiers(left):
                        nm = idn.text.decode("utf-8", "replace")
                        share = _py_assign_shareable(assign, nm)
                        out.append(_Binding(nm, stmt.id, span, "assign",
                                            safe_to_share=share))
        elif inner.type in _IMPORT_KINDS:
            for ib in _py_parse_import(inner):
                out.append(_Binding(ib.name, stmt.id, span, "import",
                                    dotted=ib.dotted, origin=ib.origin))
    return out


def _name_of(node) -> str:
    nm = node.child_by_field_name("name")
    return nm.text.decode("utf-8", "replace") if nm is not None else "<anonymous>"


# Rename and drop edits


def _class_scopes(node, prefix: str = "") -> Set[str]:
    """Qualified class scopes lost when a dependent class is dropped."""
    if node.type == "decorated_definition":
        node = node.child_by_field_name("definition") or node
    if node.type not in _CLASS_KINDS:
        return set()
    scope = prefix + _name_of(node)
    scopes = {scope}
    body = node.child_by_field_name("body")
    if body is not None:
        for child in _module_statements(body):
            scopes.update(_class_scopes(child, scope + "."))
    return scopes


def _rename_edits(
    root, old: str, new: str,
) -> Optional[List[Edit]]:
    """The edits renaming every binding and reference of ``old`` to ``new``,
    None when an import's binding cannot be alias-renamed (a dotted import)."""
    edits: List[Edit] = []
    for node in _treesitter.walk(root):
        if node.type in _IMPORT_KINDS:
            part = _py_import_rename_edits(node, old, new)
            if part is None:
                return None
            edits.extend(part)
    for node in _treesitter.walk(root):
        if node.type != "identifier":
            continue
        if node.text.decode("utf-8", "replace") != old:
            continue
        if _within_import(node) or _is_attribute_member(node) \
                or _is_keyword_name(node):
            continue
        edits.append(_ident_edit(node, new))
    return edits


def _dependent_spans(
    root, name: str, binding_stmt_ids: Set[int],
) -> List[Tuple[int, int]]:
    """Spans of the module-scope statements, other than its bindings, that
    use ``name``: dropped with the binding, so the merged module imports."""
    spans: List[Tuple[int, int]] = []
    for stmt in _module_statements(root):
        if stmt.id in binding_stmt_ids:
            continue
        for n in _treesitter.walk(stmt):
            if n.type == "identifier" \
                    and n.text.decode("utf-8", "replace") == name \
                    and not _within_import(n) \
                    and not _is_attribute_member(n) \
                    and not _is_keyword_name(n) \
                    and not _is_store(n):
                spans.append(_node_line_span(stmt))
                break
    return spans


def _reflective_ref_spans(
    root, name: str,
) -> List[Tuple[int, int]]:
    """Spans of module-scope statements that resolve ``name`` by string,
    dropped with the binding."""
    spans: List[Tuple[int, int]] = []
    for stmt in _module_statements(root):
        if name in _py_reflective_string_names(stmt):
            spans.append(_node_line_span(stmt))
    return spans


# Text machinery (language-agnostic)


def _stmt_text(source_lines: List[str], span: Tuple[int, int]) -> str:
    a, b = span
    return "\n".join(line.rstrip() for line in source_lines[a - 1:b]).strip()


def _drop_spans(
    source_lines: List[str], spans: List[Tuple[int, int]],
) -> List[str]:
    dead: Set[int] = set()
    for a, b in spans:
        if 1 <= a <= len(source_lines):
            line = source_lines[a - 1]
            indent = line[:len(line) - len(line.lstrip())]
            cursor = a - 1
            while cursor > 0:
                previous = source_lines[cursor - 1]
                if not previous.strip() or not previous.startswith(indent):
                    break
                if not previous[len(indent):].startswith("#"):
                    break
                cursor -= 1
            # Preserve a leading module comment block as a module header.
            if cursor > 0 or indent:
                a = cursor + 1
        dead.update(range(a, b + 1))
    # A dropped block with a blank line (or the source's end) after it goes
    # with the blank lines before it: the gap before the next statement
    # stays, not one per dropped block. A block the next line follows
    # directly leaves the gap before it to that line.
    for end in sorted(dead):
        if end + 1 in dead:
            continue
        if end < len(source_lines) and source_lines[end].strip():
            continue
        start = end
        while start - 1 in dead:
            start -= 1
        before = start - 1
        while before >= 1 and before not in dead and not source_lines[before - 1].strip():
            dead.add(before)
            before -= 1
    return [line for i, line in enumerate(source_lines, 1) if i not in dead]


def _dependent_test_names(root, bindings, name, test_names) -> Set[str]:
    """Known test names lost with a binding's static dependents."""
    binds = bindings.get(name, ())
    if not binds:
        return set()
    binding_ids = {binding.stmt_id for binding in binds}
    spans = [binding.span for binding in binds]
    spans += _dependent_spans(root, name, binding_ids)
    spans += _reflective_ref_spans(root, name)
    dependent_names: Set[str] = set()
    dependent_classes: Set[str] = set()
    for stmt in _module_statements(root):
        start, end = _node_line_span(stmt)
        if any(start >= a and end <= b for a, b in spans):
            stmt_name = _name_of(stmt)
            if stmt_name:
                dependent_names.add(stmt_name)
            dependent_classes.update(_class_scopes(stmt))
    result = set()
    for test_name in test_names:
        scope, _, leaf = test_name.rpartition(".")
        if (leaf in dependent_names or test_name in dependent_names
                or any(scope == cls or scope.endswith("." + cls)
                       for cls in dependent_classes)):
            result.add(test_name)
    return result


# The engine


def resolve_collisions(
    sources: Sequence[str],
    choose: Optional[Choose] = None,
) -> CollisionResolution:
    """Resolve module-scope name collisions across the Python ``sources``.
    A collision costs a test only when two or more owners of a name cannot
    be renamed: those that lose the name lose the definition and its
    dependents. ``choose`` picks the one that keeps it (by default the
    first). A None choice leaves the name unchanged in ``conflicts``, and a
    choice outside those owners raises ValueError. A source is ejected only
    when its parser rejects it or its rewrite does not parse."""
    n = len(sources)
    roots: List[Optional["object"]] = []
    ejected: Dict[int, str] = {}
    for i, src in enumerate(sources):
        ok, message = _py_parse_valid(src)
        if ok:
            roots.append(_treesitter.parse(_GRAMMAR, src).root_node)
        else:
            roots.append(None)
            ejected[i] = message

    # Numbered as tree-sitter numbers rows, not by str.splitlines.
    lines_per = [[bare(row) for row in rows(src)] for src in sources]
    bindings_per: List[Dict[str, List[_Binding]]] = []
    names_by_stmt_per: List[Dict[int, Set[str]]] = []
    reflective_per: List[Set[str]] = []
    for i in range(n):
        per: Dict[str, List[_Binding]] = {}
        by_stmt: Dict[int, Set[str]] = {}
        if roots[i] is not None:
            for b in _module_bindings(roots[i]):
                per.setdefault(b.name, []).append(b)
                by_stmt.setdefault(b.stmt_id, set()).add(b.name)
        bindings_per.append(per)
        names_by_stmt_per.append(by_stmt)
        reflective_per.append(
            _py_reflective_string_names(roots[i]) if roots[i] is not None
            else set())

    all_names: List[str] = []
    seen: Set[str] = set()
    taken: Set[str] = set()
    for per in bindings_per:
        for name in per:
            taken.add(name)
            if name not in seen:
                seen.add(name)
                all_names.append(name)

    renames: List[Dict[str, str]] = [{} for _ in range(n)]
    choices: List[Tuple[str, int, Tuple[int, ...]]] = []
    conflicts: List[Tuple[str, Tuple[int, ...]]] = []
    drops: List[List[Tuple[int, int]]] = [[] for _ in range(n)]
    dedup_names: List[List[str]] = [[] for _ in range(n)]
    # The source whose copy a deduplicated binding uses.
    dedup_holder: Dict[Tuple[int, str], int] = {}
    dropped_def_names: List[List[str]] = [[] for _ in range(n)]

    def next_free(name: str) -> str:
        fresh = fresh_name(name, taken)
        taken.add(fresh)
        return fresh

    def per_def_drop(i: int, name: str) -> None:
        binds = bindings_per[i][name]
        binding_ids = {b.stmt_id for b in binds}
        for b in binds:
            drops[i].append(b.span)
        drops[i].extend(_dependent_spans(roots[i], name, binding_ids))
        drops[i].extend(_reflective_ref_spans(roots[i], name))
        dropped_def_names[i].append(name)

    def unrenamable(i: int, name: str) -> bool:
        binds = bindings_per[i][name]
        if any(b.dotted for b in binds) or name in reflective_per[i]:
            return True
        return _rename_edits(roots[i], name, f"{name}_probe") is None

    # A name bound differently in the sources that share it is renamed. An
    # identical def referring to one is renamed too, not deduplicated: the
    # kept copy would bind the kept source's value for every suite.
    differing_names: Set[str] = set()
    for nm in all_names:
        own = [i for i in range(n)
               if nm in bindings_per[i] and i not in ejected]
        if len(own) < 2:
            continue
        texts = {tuple(_stmt_text(lines_per[i], b.span)
                       for b in bindings_per[i][nm]) for i in own}
        if len(texts) > 1:
            differing_names.add(nm)

    for name in all_names:
        owners = [i for i in range(n)
                  if name in bindings_per[i] and i not in ejected]
        if len(owners) < 2:
            continue
        origins = set()
        all_imports = True
        for i in owners:
            for b in bindings_per[i][name]:
                if b.kind != "import" or b.origin is None:
                    all_imports = False
                origins.add(b.origin)
        if all_imports and len(origins) == 1:
            continue
        # The name goes to an un-renamable owner, so a single one costs
        # nothing. Without one the first keeps it: every body survives.
        unren = [i for i in owners if unrenamable(i, name)]
        if not unren:
            holder = owners[0]
        elif len(unren) == 1:
            holder = unren[0]
        else:
            if choose is None:
                holder = unren[0]
            else:
                chosen = choose(name, tuple(unren))
                if chosen is None:
                    conflicts.append((name, tuple(unren)))
                    continue
                if chosen not in unren:
                    raise ValueError(
                        f"the choice for {name!r} kept source {chosen}, "
                        f"which does not bind it un-renamably ({unren})")
                holder = chosen
            choices.append(
                (name, holder, tuple(i for i in unren if i != holder)))
        holder_binds = bindings_per[holder][name]
        holder_text = (_stmt_text(lines_per[holder], holder_binds[0].span)
                       if len(holder_binds) == 1
                       and holder_binds[0].kind != "import" else None)
        holder_refs_differ = (
            bool(_referenced_names(holder_text) & differing_names)
            if holder_text is not None else False)
        for i in owners:
            if i in ejected or i == holder:
                continue
            binds = bindings_per[i][name]
            if (holder_text is not None and len(binds) == 1
                    and binds[0].kind != "import"
                    and binds[0].safe_to_share
                    and not holder_refs_differ
                    and _stmt_text(lines_per[i], binds[0].span) == holder_text):
                other = names_by_stmt_per[i].get(binds[0].stmt_id, set()) - {name}
                if not other:
                    drops[i].append(binds[0].span)
                    dedup_names[i].append(name)
                    dedup_holder[(i, name)] = holder
                    continue
                holder_other = names_by_stmt_per[holder].get(
                    holder_binds[0].stmt_id, set())
                if other | {name} <= holder_other:
                    drops[i].append(binds[0].span)
                    dedup_names[i].append(name)
                    dedup_holder[(i, name)] = holder
                    continue
            if unrenamable(i, name):
                per_def_drop(i, name)
                continue
            renames[i][name] = next_free(name)

    # A module-level binding of a builtin (id, type) would hide it from the
    # other suites. The suite green alone never uses the builtin it shadows,
    # so its binding is renamed. A shared name is already handled above.
    for i in range(n):
        if i in ejected:
            continue
        for name, binds in bindings_per[i].items():
            if name not in _PY_SHADOWABLE_BUILTINS:
                continue
            owners = [j for j in range(n)
                      if name in bindings_per[j] and j not in ejected]
            if len(owners) != 1:
                continue
            if name in renames[i] or name in dropped_def_names[i]:
                continue
            if any(b.kind == "import" or b.dotted for b in binds) \
                    or name in reflective_per[i]:
                continue
            renames[i][name] = next_free(name)

    # A deduplicated binding whose text refers to a name renamed or dropped in
    # either source (a TypeVar, a list, a shadowed builtin) is renamed with its
    # statement's other names instead, until no deduplication changes.
    def moved(j: int, ref: str) -> bool:
        return ref in renames[j] or ref in dropped_def_names[j]

    undone = True
    while undone:
        undone = False
        for i in range(n):
            for name in list(dedup_names[i]):
                if name not in dedup_names[i]:
                    continue
                holder = dedup_holder[(i, name)]
                stmt = bindings_per[i][name][0]
                refs = _referenced_names(
                    _stmt_text(lines_per[i], stmt.span)) - {name}
                if not any(moved(i, ref) or moved(holder, ref) for ref in refs):
                    continue
                drops[i].remove(stmt.span)
                for member in [m for m in dedup_names[i]
                               if bindings_per[i][m][0].stmt_id == stmt.stmt_id]:
                    dedup_names[i].remove(member)
                    del dedup_holder[(i, member)]
                    if unrenamable(i, member):
                        per_def_drop(i, member)
                    else:
                        renames[i][member] = next_free(member)
                undone = True

    out: List[str] = []
    for i, src in enumerate(sources):
        if i in ejected:
            out.append(src)
            continue
        if not renames[i] and not drops[i]:
            out.append(src)
            continue
        all_edits: List[Edit] = []
        late_drops: List[Tuple[int, int]] = []
        for old, new in list(renames[i].items()):
            edits = _rename_edits(roots[i], old, new)
            if edits is None:
                binds = bindings_per[i][old]
                binding_ids = {b.stmt_id for b in binds}
                for b in binds:
                    late_drops.append(b.span)
                late_drops.extend(
                    _dependent_spans(roots[i], old, binding_ids))
                dropped_def_names[i].append(old)
                del renames[i][old]
                continue
            all_edits.extend(edits)
        lines = [bare(row) for row in rows(apply_edits(src, all_edits))]
        lines = _drop_spans(lines, drops[i] + late_drops)
        rewritten = "\n".join(lines) + ("\n" if src.endswith("\n") else "")
        ok, message = _py_parse_valid(rewritten)
        if not ok:
            ejected[i] = f"rewrite failed to parse: {message}"
            out.append(src)
            continue
        out.append(rewritten)

    ejected_idx = tuple(sorted(ejected))
    return CollisionResolution(
        sources=tuple(out),
        ejected=ejected_idx,
        eject_reasons=tuple(ejected[i] for i in ejected_idx),
        renames=tuple(renames),
        deduped=tuple(tuple(d) for d in dedup_names),
        dropped_defs=tuple(tuple(d) for d in dropped_def_names),
        choices=tuple(choices),
        conflicts=tuple(conflicts),
    )


# Python's operations (validity gate: the calling interpreter's ``ast``)

import ast  # noqa: E402  (CPython parser: the Python validity gate)


def _py_parse_valid(src: str) -> Tuple[bool, str]:
    try:
        _syntax.parse(src)
        return True, ""
    except SyntaxError as exc:
        return False, f"syntax error: {exc.msg} (line {exc.lineno})"


def _py_parse_import(node) -> List[ImportBound]:
    out: List[ImportBound] = []
    if node.type == "import_statement":
        for nm in node.children_by_field_name("name"):
            if nm.type == "aliased_import":
                alias = nm.child_by_field_name("alias")
                origin = nm.child_by_field_name("name")
                if alias is not None and origin is not None:
                    out.append(ImportBound(
                        alias.text.decode("utf-8", "replace"),
                        origin=("", origin.text.decode("utf-8", "replace"))))
            elif nm.type == "dotted_name":
                ids = [c for c in nm.named_children if c.type == "identifier"]
                if ids:
                    out.append(ImportBound(
                        ids[0].text.decode("utf-8", "replace"),
                        origin=("", nm.text.decode("utf-8", "replace")),
                        dotted=len(ids) > 1))
    else:  # import_from_statement / future_import_statement
        module = ""
        mn = node.child_by_field_name("module_name")
        if mn is not None:
            module = mn.text.decode("utf-8", "replace")
        elif node.type == "future_import_statement":
            module = "__future__"
        for nm in node.children_by_field_name("name"):
            if nm.type == "aliased_import":
                alias = nm.child_by_field_name("alias")
                origin = nm.child_by_field_name("name")
                if alias is not None and origin is not None:
                    out.append(ImportBound(
                        alias.text.decode("utf-8", "replace"),
                        origin=(module, origin.text.decode("utf-8", "replace"))))
            elif nm.type == "dotted_name":
                out.append(ImportBound(
                    nm.text.decode("utf-8", "replace"),
                    origin=(module, nm.text.decode("utf-8", "replace"))))
    return out


def _py_import_rename_edits(
    node, old: str, new: str,
) -> Optional[List[Edit]]:
    edits: List[Edit] = []
    for nm in node.children_by_field_name("name"):
        if nm.type == "aliased_import":
            alias = nm.child_by_field_name("alias")
            if alias is not None and alias.text.decode("utf-8", "replace") == old:
                edits.append(_ident_edit(alias, new))
        elif nm.type == "dotted_name":
            ids = [c for c in nm.named_children if c.type == "identifier"]
            if not ids or ids[0].text.decode("utf-8", "replace") != old:
                continue
            if node.type == "import_statement" and len(ids) > 1:
                # ``import a.b.c`` binds ``a`` inside the module path. An
                # alias rename would corrupt the path, so drop the definition.
                return None
            full = nm.text.decode("utf-8", "replace")
            edits.append(_ident_edit(nm, f"{full} as {new}"))
    return edits


def _py_string_value(node) -> Optional[str]:
    if node is None or node.type != "string":
        return None
    parts = [c.text.decode("utf-8", "replace")
             for c in node.named_children if c.type == "string_content"]
    return "".join(parts)


def _py_is_self_module_ref(node) -> bool:
    """Whether ``node`` addresses this module's namespace (``globals()``,
    ``locals()``, ``sys.modules[__name__]``), so a string lookup on it
    resolves a top-level binding."""
    if node is None:
        return False
    if node.type == "call":
        fn = node.child_by_field_name("function")
        if fn is not None and fn.type == "identifier" \
                and fn.text.decode("utf-8", "replace") in ("globals", "locals"):
            return True
    if node.type == "subscript":
        val = node.child_by_field_name("value")
        sub = node.child_by_field_name("subscript")
        if val is not None and val.type == "attribute":
            obj = val.child_by_field_name("object")
            attr = val.child_by_field_name("attribute")
            if obj is not None and obj.type == "identifier" \
                    and obj.text.decode("utf-8", "replace") == "sys" \
                    and attr is not None \
                    and attr.text.decode("utf-8", "replace") == "modules" \
                    and sub is not None and sub.type == "identifier" \
                    and sub.text.decode("utf-8", "replace") == "__name__":
                return True
    return False


def _py_reflective_string_names(node) -> Set[str]:
    """Names under ``node`` that resolve a top-level binding by string
    literal: ``getfixturevalue("x")``, ``usefixtures("x")``, and a subscript
    or ``getattr``, ``setattr``, ``hasattr`` or ``delattr`` on this module's
    namespace (``globals()["x"]``). Other strings do not block a rename."""
    names: Set[str] = set()
    if node is None:
        return names
    for n in _treesitter.walk(node):
        if n.type == "call":
            fn = n.child_by_field_name("function")
            fname = None
            if fn is not None:
                if fn.type == "identifier":
                    fname = fn.text.decode("utf-8", "replace")
                elif fn.type == "attribute":
                    attr = fn.child_by_field_name("attribute")
                    if attr is not None:
                        fname = attr.text.decode("utf-8", "replace")
            args = n.child_by_field_name("arguments")
            arglist = list(args.named_children) if args is not None else []
            if fname in ("getfixturevalue", "usefixtures"):
                for arg in arglist:
                    s = _py_string_value(arg)
                    if s is not None:
                        names.add(s)
            elif fname in ("getattr", "setattr", "hasattr", "delattr") \
                    and arglist and _py_is_self_module_ref(arglist[0]):
                for arg in arglist[1:]:
                    s = _py_string_value(arg)
                    if s is not None:
                        names.add(s)
        elif n.type == "subscript" \
                and _py_is_self_module_ref(n.child_by_field_name("value")):
            s = _py_string_value(n.child_by_field_name("subscript"))
            if s is not None:
                names.add(s)
    return names


# Module globals pytest reads by name and applies module-wide: an identical
# binding is shared, not renamed to an unread ``pytestmark_2``.
_PYTEST_RESERVED_GLOBALS = frozenset({
    "pytestmark", "pytest_plugins", "collect_ignore", "collect_ignore_glob",
})

# Node kinds of an immutable scalar, safe to share. A list, dict, set, call
# or name builds per-suite state and is renamed.
_PY_IMMUTABLE_LITERAL_KINDS = frozenset({
    "integer", "float", "string", "concatenated_string",
    "true", "false", "none",
})


# Callable builtins a suite may call unqualified, by name, from the calling
# interpreter: one only the project interpreter has is not covered.
import builtins as _builtins  # noqa: E402

_PY_SHADOWABLE_BUILTINS = frozenset(
    n for n in dir(_builtins)
    if not n.startswith("_") and callable(getattr(_builtins, n))
)


def _py_assign_shareable(node, name: str) -> bool:
    """Whether a module-level assignment of ``name`` may be shared across
    suites: a pytest-reserved global, or a plain ``=`` of an immutable scalar
    literal. Anything else (a container, a call, a name, an augmented
    assignment) builds per-suite state."""
    if name in _PYTEST_RESERVED_GLOBALS:
        return True
    if node.type != "assignment":
        return False
    rhs = node.child_by_field_name("right")
    return rhs is not None and rhs.type in _PY_IMMUTABLE_LITERAL_KINDS


def resolve_python_collisions(
    sources: Sequence[str],
    *,
    localize_autouse: bool = False,
    choose: Optional[Choose] = None,
) -> CollisionResolution:
    """Resolve Python collisions, optionally keeping autouse fixtures local.

    Definitions shadowed in their own scope come out first (``dropped_defs``
    lists them). With ``localize_autouse`` (off by default) a module-level
    autouse fixture, which would apply to the whole merged module, gets
    ``autouse=False`` and is requested with ``pytest.mark.usefixtures`` by
    its source's tests (``_syntax.collected_tests``) and the classes that
    hold them, where there are such tests and the rewrite parses.
    """
    from langadapter.python.source_analysis import normalize_shadowed_definitions

    normalizations = tuple(normalize_shadowed_definitions(source) for source in sources)
    normalized_sources = tuple(item.source for item in normalizations)
    autouse = tuple(
        _py_module_autouse_fixtures(source) for source in normalized_sources
    )
    resolution = resolve_collisions(
        normalized_sources, choose=choose)
    resolution = _py_apply_class_literal_renames(
        normalized_sources, resolution
    )
    dropped = []
    for i, item in enumerate(normalizations):
        normalized_names = [
            binding.name for binding in item.removed_bindings
        ]
        collision_names = (
            list(resolution.dropped_defs[i])
            if resolution.dropped_defs else []
        )
        dropped.append(tuple(dict.fromkeys(
            normalized_names + collision_names
        )))
    resolution = replace(resolution, dropped_defs=tuple(dropped))
    if not localize_autouse or not any(autouse):
        return resolution

    rewritten = list(resolution.sources)
    localized: List[Tuple[str, ...]] = [() for _ in sources]
    ejected = set(resolution.ejected)
    for i, names in enumerate(autouse):
        if not names or i in ejected:
            continue
        dropped = set(resolution.dropped_defs[i])
        mapped = tuple(dict.fromkeys(
            resolution.renames[i].get(name, name)
            for name in names
            if name not in dropped
        ))
        if not mapped:
            continue
        transformed = _py_localize_autouse_fixtures(
            rewritten[i], mapped
        )
        if transformed is None:
            continue
        rewritten[i] = transformed
        localized[i] = mapped

    return replace(
        resolution,
        sources=tuple(rewritten),
        localized_autouse=tuple(localized),
    )


def _py_type_parts(node):
    """The nodes of an annotation in type positions: all but a ``Literal``'s
    values and an ``Annotated``'s metadata."""
    yield node
    if isinstance(node, ast.Subscript):
        head = node.value
        name = (head.id if isinstance(head, ast.Name)
                else head.attr if isinstance(head, ast.Attribute) else None)
        if name in ("Literal", "Annotated"):
            yield from _py_type_parts(head)
            if name == "Annotated":
                args = (node.slice.elts if isinstance(node.slice, ast.Tuple)
                        else [node.slice])
                if args:
                    yield from _py_type_parts(args[0])
            return
    for child in ast.iter_child_nodes(node):
        yield from _py_type_parts(child)


def _py_renamed_reference(value: str, renames: Dict[str, str]) -> Optional[str]:
    """A string annotation's text with the names it refers to as types
    renamed (``_py_type_parts``), not a name after a dot. None when it is no
    expression or names nothing renamed."""
    try:
        tree = _syntax.parse(value, mode="eval")
    except SyntaxError:
        return None
    encoded = value.encode("utf-8")
    edits = sorted(
        ((n.col_offset, n.end_col_offset, renames[n.id])
         for n in _py_type_parts(tree.body)
         if isinstance(n, ast.Name) and n.id in renames and n.lineno == n.end_lineno == 1),
        reverse=True)
    if not edits:
        return None
    for start, end, new in edits:
        encoded = encoded[:start] + new.encode("utf-8") + encoded[end:]
    return encoded.decode("utf-8")


def _py_requoted(segment: str, value: str, new: str) -> str:
    """The string literal ``segment`` of ``value`` holding ``new`` instead,
    unchanged when its text is not its value (escapes, parts)."""
    body = segment.lstrip("rRbBuU")
    prefix = segment[:len(segment) - len(body)]
    quote = body[:3] if body[:3] in ('"""', "'''") else body[:1]
    if not quote or len(body) < 2 * len(quote) or body[len(quote):-len(quote)] != value:
        return segment
    return prefix + quote + new + quote


#: The typing factories whose first argument names the variable they are
#: assigned to.
_TYPE_NAME_FACTORIES = frozenset({"TypeVar", "ParamSpec", "TypeVarTuple", "NewType"})


def _py_apply_class_literal_renames(
    sources: Sequence[str],
    resolution: CollisionResolution,
) -> CollisionResolution:
    """Follow each source's renames into its own string literals.

    A class's ``__name__`` reaches asserted output through its repr, so the
    source's renamed top-level class names are replaced as whole words in
    its plain string literals (not f-strings, whose constants carry
    unreliable positions on older interpreters). In a string annotation,
    which ``get_type_hints`` evaluates, the names used as types follow every
    rename (not a name after a dot, a ``Literal``'s values or an
    ``Annotated``'s metadata), as does a renamed ``TypeVar``-like factory's
    name argument, unless written with escapes or in parts."""
    import re

    rewritten = list(resolution.sources)
    ejected = set(resolution.ejected)
    changed = False
    for i, original in enumerate(sources):
        if i in ejected:
            continue
        try:
            class_names = {
                node.name for node in _syntax.parse(original).body
                if isinstance(node, ast.ClassDef)
            }
        except SyntaxError:
            continue
        renamed = {
            old: new for old, new in resolution.renames[i].items()
            if old != new
        }
        renames = {
            old: new for old, new in renamed.items() if old in class_names
        }
        if not renamed:
            continue
        source = rewritten[i]
        try:
            tree = _syntax.parse(source)
        except SyntaxError:
            continue

        def _words(names, guard: str):
            return re.compile(guard + r"\b(" + "|".join(
                re.escape(old)
                for old in sorted(names, key=len, reverse=True)) + r")\b")

        pattern = _words(renames, "") if renames else None
        encoded = source.encode("utf-8")
        line_starts = [0]
        for line in encoded.splitlines(keepends=True):
            line_starts.append(line_starts[-1] + len(line))

        def _offset(lineno: int, col: int) -> int:
            return line_starts[lineno - 1] + col

        fstring_spans = [
            (_offset(n.lineno, n.col_offset),
             _offset(n.end_lineno, n.end_col_offset))
            for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
        ]
        # The new value of each annotation's type reference and of each
        # renamed TypeVar-like name argument, by position.
        whole: Dict[Tuple[int, int], str] = {}
        # A Literal's values and an Annotated's metadata keep their text.
        values: Set[Tuple[int, int]] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.AnnAssign, ast.arg)):
                annotation = node.annotation
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotation = node.returns
            else:
                continue
            if annotation is None:
                continue
            types = set()
            for inner in _py_type_parts(annotation):
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str):
                    types.add((inner.lineno, inner.col_offset))
                    value = _py_renamed_reference(inner.value, renamed)
                    if value is not None:
                        whole[(inner.lineno, inner.col_offset)] = value
            values.update(
                (inner.lineno, inner.col_offset) for inner in ast.walk(annotation)
                if isinstance(inner, ast.Constant) and isinstance(inner.value, str)
                and (inner.lineno, inner.col_offset) not in types)
        for node in tree.body:
            if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and isinstance(node.value, ast.Call) and node.value.args):
                continue
            func, first = node.value.func, node.value.args[0]
            factory = (func.id if isinstance(func, ast.Name)
                       else func.attr if isinstance(func, ast.Attribute) else None)
            if (factory in _TYPE_NAME_FACTORIES and isinstance(first, ast.Constant)
                    and renamed.get(first.value) == node.targets[0].id):
                whole[(first.lineno, first.col_offset)] = node.targets[0].id
        edits: List[Edit] = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)):
                continue
            start = _offset(node.lineno, node.col_offset)
            end = _offset(node.end_lineno, node.end_col_offset)
            if any(fs <= start and end <= fe for fs, fe in fstring_spans):
                continue
            segment = encoded[start:end].decode("utf-8")
            if (node.lineno, node.col_offset) in values:
                continue
            if (node.lineno, node.col_offset) in whole:
                replaced = _py_requoted(segment, node.value,
                                        whole[(node.lineno, node.col_offset)])
            elif pattern is not None:
                replaced = pattern.sub(lambda m: renames[m.group(1)], segment)
            else:
                continue
            if replaced != segment:
                edits.append((start, end, replaced))
        if not edits:
            continue
        transformed = apply_edits(source, edits)
        try:
            _syntax.parse(transformed)
        except SyntaxError:
            continue
        rewritten[i] = transformed
        changed = True
    if not changed:
        return resolution
    return replace(resolution, sources=tuple(rewritten))


def _py_fixture_call(decorator):
    """``decorator`` when it is a ``pytest.fixture(...)`` call, else None."""

    if not isinstance(decorator, ast.Call):
        return None
    func = decorator.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "fixture"
        and isinstance(func.value, ast.Name)
        and func.value.id == "pytest"
    ):
        return decorator
    return None


def _py_autouse_value(function):
    """The literal ``True`` node in a module fixture decorator, or None."""

    for decorator in function.decorator_list:
        call = _py_fixture_call(decorator)
        if call is None:
            continue
        for keyword in call.keywords:
            if (
                keyword.arg == "autouse"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is True
            ):
                return keyword.value
    return None


def _py_module_autouse_fixtures(source: str) -> Tuple[str, ...]:
    """Names of literal module-level ``pytest.fixture(autouse=True)`` defs."""

    try:
        tree = _syntax.parse(source)
    except SyntaxError:
        return ()
    return tuple(
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and _py_autouse_value(node) is not None
    )


def _py_localize_autouse_fixtures(
    source: str,
    fixture_names: Sequence[str],
) -> Optional[str]:
    """``source`` with its autouse fixtures off and requested by each of its
    tests, None when it has no tests or it or its rewrite does not parse."""

    try:
        tree = _syntax.parse(source)
    except SyntaxError:
        return None

    autouse_values = [
        value for value in (_py_autouse_value(node) for node in tree.body
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))
        if value is not None]
    # Each test function, and each class that holds tests, once.
    tests = list(dict.fromkeys(cls or function
                               for _, cls, function in _syntax.collected_tests(tree)))
    if not tests:
        return None

    encoded = source.encode("utf-8")
    line_starts = [0]
    for line in encoded.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))

    def _offset(lineno: int, col: int) -> int:
        return line_starts[lineno - 1] + col

    newline = "\r\n" if "\r\n" in source else "\n"
    quoted = ", ".join(repr(name) for name in fixture_names)
    marker = f"@pytest.mark.usefixtures({quoted}){newline}"
    edits: List[Edit] = []
    for value in autouse_values:
        edits.append((
            _offset(value.lineno, value.col_offset),
            _offset(value.end_lineno, value.end_col_offset),
            "False",
        ))
    for node in tests:
        first = node.decorator_list[0] if node.decorator_list else node
        position = _offset(first.lineno, node.col_offset)
        edits.append((position, position, marker))

    transformed = apply_edits(source, edits)
    try:
        _syntax.parse(transformed)
    except SyntaxError:
        return None
    return transformed
