"""Collision resolution among Java test classes before ``merge_sources``
merges them into the first source's class, where methods, fields and types
each have a namespace. Per namespace, a name several sources declare is
resolved as Python's module scope would:

- Identical declarations (apart from comments and layout, and for a method
  all its overloads) are shared: later sources drop their copy. A static
  non-final field is renamed instead, since a shared copy would carry state
  between the sources' tests. Instance fields are shared, as JUnit gives
  every test its own instance.
- A differing declaration of a later source is renamed, with its
  references, to the first free ``<name>_<k>``, k from 2. A method's
  references are its declarations, unqualified, ``this.`` and ``Class.``
  calls, method references and annotation strings naming it
  (``@MethodSource``), a field's its declarator and uses, and a type is
  renamed by ``source_analysis``'s type rename.
- A declaration that cannot be renamed safely (a field another declaration
  of the name shadows, a method a nested type also declares, a type whose
  name is also a variable's, method's or annotation element's) is a
  conflict: the caller's preference names the source that keeps it, and the
  others drop it with every member using it. Without one nothing is dropped.

Imports of one simple name from different classes conflict too: the later
source drops its import and names the class (or a static member's class)
fully.

A merge widens per-test lifecycle methods (``@BeforeEach``, ``@AfterEach``,
JUnit 4's ``@Before``, ``@After``) to the other sources' tests. ``localize``
keeps them on their own tests, which call them instead (setup first,
teardown in a ``finally``). A lifecycle method with parameters rejects its
source. ``@BeforeAll`` and ``@AfterAll`` are not localized.

Merged tests run under the first class's header, so these are rejected: a
source javac rejects, one without a top-level class, and a later class with
a constructor, another package, or other annotations (``@SuppressWarnings``
aside, kept from both), superclass or interfaces.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from langadapter import _treesitter
from langadapter.errors import InvalidRequest
from langadapter.sources import Choose, apply_edits as _apply, fresh_name
from langadapter.treesitter import cut
from langadapter.java import source_analysis as _rename
from langadapter.java.source_analysis import _declared_variables
from langadapter.java._sources import (
    CLASS_KINDS, COMMENT_KINDS, FUNC_KINDS, GRAMMAR, TEST_ANNOTATIONS,
    _annotations, _text, _top_class, header_difference,
)
from langadapter.sources import INFRASTRUCTURE_FAILURE, SourceObservation, _collision_answer

BEFORE = frozenset({"BeforeEach", "Before"})
AFTER = frozenset({"AfterEach", "After"})


@dataclass
class _Source:
    """One source being resolved: its text and what happened to it."""

    index: int
    text: str
    renamed: List[Tuple[str, str]] = field(default_factory=list)
    shared: List[str] = field(default_factory=list)
    dropped: List[str] = field(default_factory=list)
    localized: List[str] = field(default_factory=list)

    @property
    def root(self) -> Any:
        return _treesitter.parse(GRAMMAR, self.text).root_node

    def top(self) -> Any:
        return _top_class(self.root)


@dataclass
class _Binding:
    namespace: str
    name: str
    nodes: List[Any]
    fingerprint: str


def _fingerprint(nodes: Sequence[Any]) -> str:
    return "\n".join(_treesitter._fingerprint(n, COMMENT_KINDS) for n in nodes)


def _remove(text: str, nodes: Sequence[Any]) -> str:
    """``text`` without ``nodes``, whole lines where only whitespace remains."""
    return cut(text, [(node.start_byte, node.end_byte) for node in nodes])


def _bindings(source: _Source) -> Dict[Tuple[str, str], _Binding]:
    """The top class's members and the source's other top-level types, by
    namespace and name, in source order."""
    root = source.root
    top = _top_class(root)
    out: Dict[Tuple[str, str], _Binding] = {}

    def add(namespace: str, name: str, node: Any) -> None:
        key = (namespace, name)
        if key in out:
            out[key].nodes.append(node)
        else:
            out[key] = _Binding(namespace, name, [node], "")

    for member in top.child_by_field_name("body").named_children:
        if member.type == "method_declaration":
            add("method", _treesitter.name(member), member)
        elif member.type == "field_declaration":
            for declarator in member.named_children:
                if declarator.type == "variable_declarator":
                    add("field", _treesitter.name(declarator), member)
        elif member.type in CLASS_KINDS:
            add("type", _treesitter.name(member), member)
    for node in root.named_children:
        if node.type in CLASS_KINDS and node != top:
            add("type", _treesitter.name(node), node)
    for binding in out.values():
        binding.fingerprint = _fingerprint(binding.nodes)
    return out


def _static_state(binding: _Binding) -> bool:
    """Whether a field is static and not final."""
    if binding.namespace != "field":
        return False
    modifiers = next((c for c in binding.nodes[0].children
                      if c.type == "modifiers"), None)
    words = _text(modifiers).split() if modifiers is not None else []
    return "static" in words and "final" not in words


def _is_self(node: Optional[Any], class_name: str) -> bool:
    """Whether a call's object names the class itself (none, ``this``, its name)."""
    return node is None or node.type == "this" or (
        node.type == "identifier" and _text(node) == class_name)


def _method_edits(source: _Source, name: str, new: str) -> Optional[List[Tuple[int, int, str]]]:
    top = source.top()
    class_name = _treesitter.name(top)
    body = top.child_by_field_name("body")
    for member in body.named_children:
        if member.type in CLASS_KINDS and any(
                n.type == "method_declaration" and _treesitter.name(n) == name
                for n in _treesitter.walk(member)):
            return None
    edits = []
    for node in _treesitter.walk(top):
        if node.type == "method_declaration" and node.parent == body and _treesitter.name(node) == name:
            label = node.child_by_field_name("name")
            edits.append((label.start_byte, label.end_byte, new))
        elif node.type == "method_invocation":
            label = node.child_by_field_name("name")
            if (label is not None and _text(label) == name
                    and _is_self(node.child_by_field_name("object"), class_name)):
                edits.append((label.start_byte, label.end_byte, new))
        elif node.type == "method_reference":
            parts = node.named_children
            if (len(parts) == 2 and parts[1].type == "identifier"
                    and _text(parts[1]) == name
                    and (parts[0].type == "this" or _text(parts[0]) == class_name)):
                edits.append((parts[1].start_byte, parts[1].end_byte, new))
        elif node.type == "string_literal" and _in_annotation(node):
            value = _text(node)
            for form in (f'"{name}"', f'"{class_name}#{name}"'):
                if value == form:
                    edits.append((node.start_byte, node.end_byte,
                                  form.replace(name, new)))
    return edits


def _in_annotation(node: Any) -> bool:
    parent = node.parent
    while parent is not None:
        if parent.type in ("annotation", "marker_annotation"):
            return True
        if parent.type in FUNC_KINDS or parent.type in CLASS_KINDS:
            return False
        parent = parent.parent
    return False


def _field_edits(source: _Source, name: str, new: str,
                 binding: _Binding) -> Optional[List[Tuple[int, int, str]]]:
    top = source.top()
    class_name = _treesitter.name(top)
    declaration = binding.nodes[0]
    declarators = [d for d in declaration.named_children
                   if d.type == "variable_declarator"]
    if len(binding.nodes) != 1 or len(declarators) != 1:
        return None
    own = declarators[0].child_by_field_name("name")
    if any(_text(n) == name and n.start_byte != own.start_byte
           for n in _declared_variables(source.root)):
        return None
    edits = []
    for node in _treesitter.walk(top):
        if node.type != "identifier" or _text(node) != name:
            continue
        parent = node.parent
        role = _role(node)
        if role in ("method_name", "type_name", "annotation_key", "label"):
            continue
        if (parent.type == "field_access" and parent.child_by_field_name("field") == node
                and not _is_self(parent.child_by_field_name("object"), class_name)):
            continue
        edits.append((node.start_byte, node.end_byte, new))
    return edits


def _role(node: Any) -> str:
    """What an identifier names in its parent."""
    parent = node.parent
    if parent is None:
        return ""
    if parent.type == "method_invocation" and parent.child_by_field_name("name") == node:
        return "method_name"
    if parent.type in FUNC_KINDS and parent.child_by_field_name("name") == node:
        return "method_name"
    if parent.type in CLASS_KINDS and parent.child_by_field_name("name") == node:
        return "type_name"
    if parent.type == "element_value_pair" and parent.child_by_field_name("key") == node:
        return "annotation_key"
    if parent.type in ("labeled_statement", "break_statement", "continue_statement"):
        return "label"
    if parent.type == "method_reference" and _rename._is_referenced_method(node):
        return "method_name"
    return ""


def _type_edits(source: _Source, name: str, new: str) -> Optional[List[Tuple[int, int, str]]]:
    """The type rename's edits, or None when unsafe (a conflict)."""
    return _rename._type_edits(source.root, name, new)[0]


def _ancestors(node: Any):
    parent = node.parent
    while parent is not None:
        yield parent
        parent = parent.parent


def _rename_edits(source: _Source, binding: _Binding,
                  new: str) -> Optional[List[Tuple[int, int, str]]]:
    if binding.namespace == "method":
        return _method_edits(source, binding.name, new)
    if binding.namespace == "field":
        return _field_edits(source, binding.name, new, binding)
    return _type_edits(source, binding.name, new)


def _references(node: Any, binding: _Binding, class_name: str) -> bool:
    """Whether ``node``'s subtree uses ``binding``. Where it declares a variable
    of a field's name, only ``this.`` and ``Class.`` accesses use the field."""
    local = binding.namespace == "field" and any(
        _text(n) == binding.name for n in _declared_variables(node))
    for inner in _treesitter.walk(node):
        if local and inner.type == "field_access":
            field = inner.child_by_field_name("field")
            if (field is not None and _text(field) == binding.name
                    and inner.child_by_field_name("object") is not None
                    and _is_self(inner.child_by_field_name("object"), class_name)):
                return True
            continue
        if local:
            continue
        if binding.namespace == "method" and inner.type == "method_invocation":
            label = inner.child_by_field_name("name")
            if (label is not None and _text(label) == binding.name
                    and _is_self(inner.child_by_field_name("object"), class_name)):
                return True
        elif binding.namespace == "field" and inner.type == "identifier":
            if _text(inner) == binding.name and not _role(inner):
                return True
        elif binding.namespace == "type" and inner.type in ("type_identifier", "identifier"):
            if _text(inner) == binding.name and _role(inner) != "method_name":
                return True
    return False


def _drop_with_dependents(source: _Source, binding: _Binding) -> List[str]:
    """Remove ``binding`` and every member using it, transitively, and return
    their names."""
    bindings = _bindings(source)
    class_name = _treesitter.name(source.top())
    gone = {(binding.namespace, binding.name)}
    changed = True
    while changed:
        changed = False
        for key, other in bindings.items():
            if key in gone:
                continue
            if any(_references(n, bindings[g], class_name)
                   for g in gone if g in bindings for n in other.nodes):
                gone.add(key)
                changed = True
    nodes = [n for key in gone if key in bindings for n in bindings[key].nodes]
    source.text = _remove(source.text, list({n.start_byte: n for n in nodes}.values()))
    return [name for (_, name) in sorted(gone)]


# Imports


def _imports(root: Any) -> List[Tuple[Any, bool, str]]:
    """Each single-type import: its node, whether static, its qualified name."""
    out = []
    for node in root.named_children:
        if node.type != "import_declaration":
            continue
        text = _text(node)
        if text.rstrip(";").rstrip().endswith("*"):
            continue
        static = bool(re.match(r"import\s+static\b", text))
        name = re.sub(r"^import\s+(static\s+)?", "", text).rstrip(";").strip()
        out.append((node, static, re.sub(r"\s+", "", name)))
    return out


def _qualify_imports(source: _Source, earlier: Dict[Tuple[bool, str], str]) -> None:
    """Drop the source's imports whose simple name an earlier source
    imports from elsewhere, naming the class or member fully instead."""
    root = source.root
    edits: List[Tuple[int, int, str]] = []
    for node, static, qualified in _imports(root):
        simple = qualified.rsplit(".", 1)[-1]
        known = earlier.get((static, simple))
        if known is None or known == qualified:
            continue
        edits.append((node.start_byte, node.end_byte, ""))
        for use in _treesitter.walk(root):
            if any(a.type == "import_declaration" for a in _ancestors(use)):
                continue
            if static and use.type == "method_invocation":
                label = use.child_by_field_name("name")
                if (label is not None and _text(label) == simple
                        and use.child_by_field_name("object") is None):
                    edits.append((label.start_byte, label.end_byte, qualified))
            elif not static and use.type in ("type_identifier", "identifier") \
                    and _text(use) == simple and _role(use) not in (
                        "method_name", "annotation_key", "label"):
                if use.parent is not None and use.parent.type == "scoped_type_identifier" \
                        and use.parent.named_children[0] is not use:
                    continue
                edits.append((use.start_byte, use.end_byte, qualified))
    if edits:
        source.text = _apply(source.text, edits)


# Lifecycle localization


def _localize(source: _Source) -> Optional[str]:
    """Move the top class's per-test lifecycle methods into its tests,
    returning the reason when that is not possible."""
    top = source.top()
    body = top.child_by_field_name("body")
    befores, afters = [], []
    for member in body.named_children:
        if member.type != "method_declaration":
            continue
        annotations = set(_annotations(member))
        if annotations & (BEFORE | AFTER):
            parameters = member.child_by_field_name("parameters")
            if parameters is not None and parameters.named_children:
                return (f"its lifecycle method {_treesitter.name(member)} takes "
                        f"parameters and cannot be called from its tests")
            (befores if annotations & BEFORE else afters).append(member)
    if not befores and not afters:
        return None
    names = [_treesitter.name(m) for m in befores + afters]
    for member in body.named_children:
        if member.type in CLASS_KINDS and any(
                n.type == "method_declaration" and _treesitter.name(n) in names
                for n in _treesitter.walk(member)):
            return "a nested class declares a method of a lifecycle method's name"
    clauses = [_text(c) for m in befores + afters for c in m.children
               if c.type == "throws"]
    throws = "Throwable" if any("Throwable" in c for c in clauses) else "Exception"
    lifecycle_throws = bool(clauses)
    edits: List[Tuple[int, int, str]] = []
    for member in befores + afters:
        for modifiers in member.children:
            if modifiers.type != "modifiers":
                continue
            for annotation in modifiers.named_children:
                if annotation.type in ("marker_annotation", "annotation") and \
                        _text(annotation.child_by_field_name("name")).rsplit(".", 1)[-1] \
                        in BEFORE | AFTER:
                    edits.append((annotation.start_byte, annotation.end_byte, ""))
    data = source.text.encode("utf-8")
    for test in _treesitter.walk(top):
        if test.type != "method_declaration" or not TEST_ANNOTATIONS.intersection(
                _annotations(test)):
            continue
        owner = next((a for a in _ancestors(test) if a.type in CLASS_KINDS), None)
        if owner != top and _is_static(owner):
            continue
        block = test.child_by_field_name("body")
        if block is None:
            continue
        line_start = data.rfind(b"\n", 0, test.start_byte) + 1
        margin = " " * (test.start_byte - line_start)
        indent = margin + "    "
        inner = data[block.start_byte + 1:block.end_byte - 1].decode("utf-8").rstrip()
        setup = "".join(f"\n{indent}{_treesitter.name(m)}();" for m in befores)
        if afters:
            teardown = "".join(f"\n{indent}    {_treesitter.name(m)}();" for m in afters)
            text = (f"{{{setup}\n{indent}try {{{_reindent(inner)}\n{indent}}} "
                    f"finally {{{teardown}\n{indent}}}\n{margin}}}")
        else:
            text = f"{{{setup}{inner}\n{margin}}}"
        edits.append((block.start_byte, block.end_byte, text))
        if lifecycle_throws:
            clause = next((c for c in test.children if c.type == "throws"), None)
            if clause is None:
                parameters = test.child_by_field_name("parameters")
                edits.append((parameters.end_byte, parameters.end_byte,
                              f" throws {throws}"))
            elif "Throwable" not in _text(clause):
                edits.append((clause.start_byte, clause.end_byte,
                              f"throws {throws}"))
    source.text = _treesitter.apply_edits_collapsing(source.text, edits, 1)
    source.text = re.sub(r"(?m)^[ \t]+\n", "\n", source.text)
    source.localized = names
    return None


def _reindent(inner: str) -> str:
    return "\n".join(("    " + line) if line.strip() else line
                     for line in inner.split("\n"))


def _is_static(node: Optional[Any]) -> bool:
    if node is None:
        return False
    modifiers = next((c for c in node.children if c.type == "modifiers"), None)
    return modifiers is not None and "static" in _text(modifiers).split()


# The resolution


def resolve(operations: Any, sources: Sequence[str], *,
            preference: Optional[Choose] = None,
            localize: bool = False) -> SourceObservation:
    """Resolve the collisions of ``sources`` merged in order into the first
    one's class (``JavaSources.resolve_collisions``)."""
    rejected: List[Tuple[int, str]] = []
    validation_failures: List[Tuple[int, str]] = []
    kept: List[_Source] = []
    anchor: Optional[_Source] = None
    refusals = operations._refusals(
        [(text, f"source {index}") for index, text in enumerate(sources)])
    for index, text in enumerate(sources):
        refusal = refusals[index]
        if refusal is not None:
            rejected.append((index, refusal.detail))
            if refusal.status == INFRASTRUCTURE_FAILURE:
                validation_failures.append((index, refusal.detail))
            continue
        source = _Source(index, text)
        top = source.top()
        if top is None:
            rejected.append((index, "it declares no top-level class"))
            continue
        if anchor is not None:
            reason = _unfoldable(anchor, source)
            if reason:
                rejected.append((index, reason))
                continue
        else:
            anchor = source
        kept.append(source)
    if localize and len(kept) > 1:
        for source in list(kept):
            reason = _localize(source)
            if reason:
                rejected.append((source.index, reason))
                kept.remove(source)
    imported: Dict[Tuple[bool, str], str] = {}
    for source in kept:
        _qualify_imports(source, imported)
        for _, static, qualified in _imports(source.root):
            imported.setdefault((static, qualified.rsplit(".", 1)[-1]), qualified)
    taken: Dict[str, Set[str]] = {"method": set(), "field": set(), "type": set()}
    for source in kept:
        for namespace, name in _bindings(source):
            taken[namespace].add(name)
    # Sharing compares texts after the renames, and a rename changes every
    # declaration naming it, which may then differ in turn: rename until none
    # follows, then share.
    conflicts: Dict[Tuple[str, str], List[_Source]] = {}
    conflicting: Set[Tuple[int, Tuple[str, str]]] = set()

    def same(first: _Source, source: _Source, key: Tuple[str, str]) -> bool:
        current = _bindings(source)[key]
        return (current.fingerprint == _bindings(first)[key].fingerprint
                and not _static_state(current))

    renaming = True
    while renaming:
        renaming = False
        owners: Dict[Tuple[str, str], _Source] = {}
        for source in kept:
            for key in list(_bindings(source)):
                if (source.index, key) in conflicting:
                    continue
                if key not in owners:
                    owners[key] = source
                    continue
                first = owners[key]
                if same(first, source, key):
                    continue
                current = _bindings(source)[key]
                fresh = fresh_name(current.name, taken[current.namespace])
                edits = _rename_edits(source, current, fresh)
                if edits is None:
                    conflicts.setdefault(key, [first]).append(source)
                    conflicting.add((source.index, key))
                    continue
                source.text = _apply(source.text, edits)
                taken[current.namespace].add(fresh)
                source.renamed.append((current.name, fresh))
                renaming = True
    for source in kept:
        for key in list(_bindings(source)):
            first = owners.get(key)
            if (first is None or first is source
                    or (source.index, key) in conflicting
                    or not same(first, source, key)):
                continue
            current = _bindings(source)[key]
            source.text = _remove(source.text, current.nodes)
            source.shared.append(current.name)
    choices: List[Tuple[str, int, Tuple[int, ...]]] = []
    open_conflicts: List[Tuple[str, Tuple[int, ...]]] = []
    for (namespace, name), group in conflicts.items():
        indices = tuple(s.index for s in group)
        chosen = preference(name, indices) if preference is not None else None
        if chosen is None:
            open_conflicts.append((name, indices))
            continue
        if chosen not in indices:
            raise InvalidRequest(
                "preference", f"kept source {chosen} for {name!r}, which is "
                f"not one of {list(indices)}")
        for source in group:
            if source.index == chosen:
                continue
            binding = _bindings(source).get((namespace, name))
            if binding is not None:
                source.dropped += _drop_with_dependents(source, binding)
        choices.append((name, chosen, tuple(i for i in indices if i != chosen)))
    out = list(sources)
    renamed: List[Tuple[Tuple[str, str], ...]] = [() for _ in sources]
    shared: List[Tuple[str, ...]] = [() for _ in sources]
    dropped: List[Tuple[str, ...]] = [() for _ in sources]
    localized: List[Tuple[str, ...]] = [() for _ in sources]
    rewritten = [s for s in kept if s.text != sources[s.index]]
    verdicts = dict(zip((s.index for s in rewritten), operations._refusals(
        [(s.text, f"the rewrite of source {s.index}") for s in rewritten])))
    for source in kept:
        refusal = verdicts.get(source.index)
        if refusal is not None:
            rejected.append((source.index, refusal.detail))
            if refusal.status == INFRASTRUCTURE_FAILURE:
                validation_failures.append((source.index, refusal.detail))
            continue
        out[source.index] = source.text
        renamed[source.index] = tuple(sorted(source.renamed))
        shared[source.index] = tuple(source.shared)
        dropped[source.index] = tuple(dict.fromkeys(source.dropped))
        localized[source.index] = tuple(source.localized)
    return _collision_answer(
        operations.structural_parser_identity(), tuple(validation_failures), rejected, renamed,
        sources=tuple(out), deduped=tuple(shared), dropped_defs=tuple(dropped),
        localized_autouse=tuple(localized), choices=tuple(choices), conflicts=tuple(open_conflicts))


def _unfoldable(anchor: _Source, source: _Source) -> str:
    """Why ``source``'s class cannot be merged into ``anchor``'s, or ""."""
    def package(root: Any) -> str:
        node = next((c for c in root.named_children
                     if c.type == "package_declaration"), None)
        return re.sub(r"\s+", " ", _text(node)).strip() if node else ""

    if package(anchor.root) != package(source.root):
        return "its package differs from the first source's"
    top = source.top()
    if any(m.type == "constructor_declaration"
           for m in top.child_by_field_name("body").named_children):
        return "its class declares a constructor"
    difference = header_difference(anchor.top(), top)
    if difference:
        return f"its class's {difference} differ from the first source's"
    return ""
