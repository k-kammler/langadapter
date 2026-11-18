"""Static analysis of Java test sources that needs no JDK: the
normalization of declarations a class makes twice, the top-level class
name, type renames, the static source operations and pooled staging.

A generated source can declare a method signature, a field or a nested
type twice (a rewritten test that kept its old version), which Java
rejects. The normalization keeps the last declaration, as Python keeps the
last binding, and records each removal with the lines removed and kept.

A type rename changes the type's declaration, constructors and every use of
its name, as a type or as the qualifier of a call, field access or method
or constructor reference. Package and import declarations, strings, method
names, annotation element names and labels keep their text. A source that
also declares a method, variable, parameter, field or annotation element of
the name cannot be renamed safely. ``rename_class``, ``merge_sources``, the
merge repair and pooled staging share this rename.

Pooled staging (the adapter's ``stage_pool`` operation) appends its suite's
slot to every top-level type a suite declares, since suites usually declare
the same test class in one package. Uses in the suite's files follow, and
each file takes its public class's new name. Strings and package and import
declarations keep the old names, so a test that reads its own class name or
statically imports its own class's member can differ or fail to compile in
the pool. ``self_references`` lists such references per staged file.
"""
from __future__ import annotations

from typing import Any, List, Optional, Tuple, Dict, Sequence, Union
from langadapter import _treesitter
from langadapter.sources import (
    Edit,
    apply_edits,
    RemovedBinding,
    SourceObservation,
    SourceStatus,
    SuiteNormalization,
    PoolMember,
    StagedFile,
    StagedPool,
    check_members,
    check_setup,
    unique_paths,
)
from langadapter.java._sources import (
    FUNC_KINDS,
    _text,
    GRAMMAR,
    _cut,
    _key,
    _members,
    CLASS_KINDS,
)
import hashlib
import re
from pathlib import PurePosixPath
from langadapter.java._java_source import declared_package


#: Declarations whose ``name`` field binds a variable.
_VARIABLE_DECLARATIONS = frozenset({
    "variable_declarator", "formal_parameter", "catch_formal_parameter",
    "enhanced_for_statement", "resource", "enum_constant",
})


def _declared_variables(root: Any) -> List[Any]:
    """Every name node that declares a variable, parameter or constant."""
    out = []
    for node in _treesitter.walk(root):
        if node.type in _VARIABLE_DECLARATIONS:
            name = node.child_by_field_name("name")
            if name is not None:
                out.append(name)
        elif node.type == "lambda_expression":
            parameters = node.child_by_field_name("parameters")
            if parameters is not None and parameters.type == "identifier":
                out.append(parameters)
            elif parameters is not None:
                out += [c for c in parameters.named_children
                        if c.type == "identifier"]
        elif node.type == "spread_parameter":
            out += [d.child_by_field_name("name") for d in node.named_children
                    if d.type == "variable_declarator"]
    return [n for n in out if n is not None]


def _refusal(root: Any, name: str) -> str:
    """Why the type ``name`` of ``root`` cannot be renamed safely, or ""."""
    if any(_text(node) == name for node in _declared_variables(root)):
        return f"a variable, parameter or field is also named {name}"
    for node in _treesitter.walk(root):
        if (node.type in FUNC_KINDS and node.type != "constructor_declaration"
                and _treesitter.name(node) == name):
            return f"a method is also named {name}"
        if node.type == "annotation_type_element_declaration" and _treesitter.name(node) == name:
            return f"an annotation element is also named {name}"
    return ""


def _declarations(node: Any) -> bool:
    """Whether ``node`` lies in a package or import declaration."""
    while node is not None:
        if node.type in ("package_declaration", "import_declaration"):
            return True
        node = node.parent
    return False


def _is_referenced_method(node: Any) -> bool:
    """Whether the identifier ``node`` of a method reference names its
    method. The qualifier is a type or an expression, the rest a method."""
    return node.parent.named_children[0] != node


def _keeps_text(node: Any) -> bool:
    """Whether the identifier ``node`` names something other than a type:
    a method (except a constructor), an annotation element or a label."""
    parent = node.parent
    if parent is None:
        return False
    if parent.type == "method_invocation":
        return parent.child_by_field_name("name") == node
    if parent.type in FUNC_KINDS:
        return (parent.type != "constructor_declaration"
                and parent.child_by_field_name("name") == node)
    if parent.type == "method_reference":
        return _is_referenced_method(node)
    if parent.type == "element_value_pair":
        return parent.child_by_field_name("key") == node
    return parent.type in ("annotation_type_element_declaration", "labeled_statement",
                           "break_statement", "continue_statement")


def _type_edits(root: Any, name: str, new: str) -> Tuple[Optional[List[Edit]], str]:
    """The edits, as (start byte, end byte, text), that rename the type
    ``name`` to ``new`` in the parsed ``root``, or None and why not."""
    reason = _refusal(root, name)
    if reason:
        return None, reason
    edits = []
    for node in _treesitter.walk(root):
        if node.type not in ("identifier", "type_identifier") or _text(node) != name:
            continue
        if _declarations(node):
            continue
        if node.type == "identifier" and _keeps_text(node):
            continue
        edits.append((node.start_byte, node.end_byte, new))
    return edits, ""


RULE_VERSION = "java-last-declaration-v1"
"""The rule ``normalize_shadowed_definitions`` applies, which its records
name."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lines(node: Any) -> Tuple[int, int]:
    return node.start_point[0] + 1, node.end_point[0] + 1


def normalize_shadowed_definitions(source: str) -> SuiteNormalization:
    """``source`` without the earlier of two declarations a class makes
    twice. Unchanged, with the error, when tree-sitter cannot parse it. A
    field declaration of several variables is left to the compiler."""
    members = _members(source)
    if members is None:
        check = _treesitter.check_structure(GRAMMAR, source)
        return SuiteNormalization(
            source=source, changed=False, parse_ok=False,
            parse_error=_treesitter.structure_message(check), removed_bindings=(),
            before_sha256=_sha(source), after_sha256=_sha(source),
            rule_version=RULE_VERSION)
    last: Dict[Tuple[str, str, str], Any] = {}
    for qualified, node in members:
        last[_key(qualified, node)] = node
    spans: List[Tuple[int, int]] = []
    removed: List[RemovedBinding] = []
    for qualified, node in members:
        kept = last[_key(qualified, node)]
        if kept == node:
            continue
        span = (node.start_byte, node.end_byte)
        if any(s <= span[0] and span[1] <= e for s, e in spans):
            continue
        if node.type == "field_declaration" and len(
                [d for d in node.named_children
                 if d.type == "variable_declarator"]) > 1:
            continue
        spans.append(span)
        *scope, name = qualified.split(".")
        removed_start, removed_end = _lines(node)
        kept_start, kept_end = _lines(kept)
        removed.append(RemovedBinding(
            scope=tuple(scope), name=name, kind=_key(qualified, node)[1],
            removed_start_line=removed_start, removed_end_line=removed_end,
            retained_start_line=kept_start, retained_end_line=kept_end))
    if not spans:
        return SuiteNormalization(
            source=source, changed=False, parse_ok=True, parse_error=None,
            removed_bindings=(), before_sha256=_sha(source),
            after_sha256=_sha(source), rule_version=RULE_VERSION)
    result = _cut(source, spans)
    return SuiteNormalization(
        source=result, changed=True, parse_ok=True, parse_error=None,
        removed_bindings=tuple(removed), before_sha256=_sha(source),
        after_sha256=_sha(result), rule_version=RULE_VERSION)


def top_class_name(source: str) -> str:
    """The public top-level class's name, else the first's, or "" (none, unparsable)."""
    from langadapter.java._sources import _top_class
    root = _treesitter.parse(GRAMMAR, source).root_node
    if root.has_error:
        return ""
    top = _top_class(root)
    return _treesitter.name(top) if top is not None else ""


def rename_class(source: str, old: str, new: str) -> SourceObservation:
    """``source`` with the type ``old`` renamed to ``new`` (the module's type
    rename) as a completed observation, for example to match a copied class
    to its file name. ``parse_error`` when it does not parse, ``unsupported``
    with the reason when the rename is unsafe."""
    root = _treesitter.parse(GRAMMAR, source).root_node
    if root.has_error:
        return SourceObservation(SourceStatus.PARSE_ERROR, reason="the source does not parse")
    if old == new:
        return SourceObservation(SourceStatus.COMPLETED, value=source)
    edits, reason = _type_edits(root, old, new)
    if edits is None:
        return SourceObservation(SourceStatus.UNSUPPORTED, reason=reason)
    return SourceObservation(SourceStatus.COMPLETED, value=apply_edits(source, edits))


def static_operations() -> Any:
    """The source operations that need no JDK and no project: structure
    check, test metrics, skeletons, fingerprints, optimization metrics,
    imports, duplicate names, merge collisions, test-file names, pruning and
    finalization. The others judge or compile a source: a ``JavaAdapter``."""
    from langadapter.java._sources import JavaSources
    # Made without a context, as no static operation reads one.
    return object.__new__(JavaSources)


__all__ = ["RULE_VERSION", "normalize_shadowed_definitions", "rename_class",
           "static_operations", "top_class_name"]


def _types(source: str) -> Optional[List[str]]:
    """The top-level types ``source`` declares, or None if it does not parse."""
    root = _treesitter.parse(GRAMMAR, source).root_node
    if root.has_error:
        return None
    return [_treesitter.name(node) for node in root.named_children if node.type in CLASS_KINDS]


#: The methods that read a class's name at run time.
_NAME_READERS = frozenset({"getSimpleName", "getCanonicalName", "getTypeName"})


def _self_references(source: str, names: Sequence[str]) -> Tuple[str, ...]:
    """Where ``source`` refers to its suite's type ``names`` in a way the
    rename does not follow: a name in a string, a class name read at run
    time, or an import of its own class (which can fail to compile)."""
    root = _treesitter.parse(GRAMMAR, source).root_node
    words = {name: re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)") for name in names}
    found = set()
    for node in _treesitter.walk(root):
        text = node.text.decode("utf-8", "replace")
        if node.type == "string_literal":
            found.update(f"names its class {name} in a string"
                         for name, word in words.items() if word.search(text))
        elif node.type == "import_declaration":
            found.update(f"imports its own class {name}" for name in names
                         if re.search(r"\." + re.escape(name) + r"(?!\w)", text))
        elif node.type == "method_invocation":
            called = node.child_by_field_name("name")
            called = called.text.decode("utf-8", "replace") if called is not None else ""
            owner = node.child_by_field_name("object")
            if called in _NAME_READERS or (called == "getName" and owner is not None and (
                    owner.type == "class_literal" or (
                        owner.type == "method_invocation"
                        and owner.child_by_field_name("name") is not None
                        and owner.child_by_field_name("name").text == b"getClass"))):
                found.add("reads a class name at run time")
    return tuple(sorted(found))


def _stage_pool(members: Sequence[PoolMember],
               setup: Sequence[Tuple[str, str]] = (),
               ) -> Union[StagedPool, SourceObservation]:
    """The pool of ``members`` with the shared ``setup`` files, or an
    observation of why it cannot be staged: a file that is not Java, does
    not parse, is not named for its public class or has types that cannot
    be renamed safely, or names or staged paths that would still collide."""
    members = check_members(members)
    setup = check_setup(setup)
    declared: Dict[int, List[str]] = {}
    for index, member in enumerate(members):
        if PurePosixPath(member.name).suffix != ".java":
            return SourceObservation(SourceStatus.UNSUPPORTED,
                                     reason=f"{member.name} is not a Java file")
        types = _types(member.source)
        if types is None:
            return SourceObservation(
                SourceStatus.PARSE_ERROR,
                reason=f"{member.name} of suite {member.slot} does not parse")
        top = top_class_name(member.source)
        if not top or PurePosixPath(member.name).stem != top:
            return SourceObservation(
                SourceStatus.UNSUPPORTED,
                reason=f"{member.name} of suite {member.slot} is not named for its "
                       f"public class ({top or 'none'})")
        declared[index] = types
    renames: Dict[str, Dict[str, str]] = {}
    for index, member in enumerate(members):
        for name in declared[index]:
            renames.setdefault(member.slot, {})[name] = f"{name}_{member.slot}"
    files = []
    staged_types: List[str] = []
    for index, member in enumerate(members):
        pairs = renames.get(member.slot, {})
        own = {name for i, m in enumerate(members) if m.slot == member.slot
               for name in declared[i]}
        clash = sorted(new for new in pairs.values() if new in own)
        if clash:
            return SourceObservation(
                SourceStatus.UNSUPPORTED,
                reason=f"suite {member.slot} already declares {', '.join(clash)}")
        text = member.source
        for old, new in pairs.items():
            edits, reason = _type_edits(
                _treesitter.parse(GRAMMAR, text).root_node, old, new)
            if edits is None:
                return SourceObservation(
                    SourceStatus.UNSUPPORTED,
                    reason=f"{member.name} of suite {member.slot}: the type {old} "
                           f"cannot be renamed safely: {reason}")
            text = apply_edits(text, edits)
        package = declared_package(member.source)
        staged_types.extend(f"{package}.{pairs[name]}" if package else pairs[name]
                            for name in declared[index])
        path = PurePosixPath(member.name)
        top = top_class_name(member.source)
        files.append(StagedFile(member.slot, path.as_posix(),
                                (path.parent / f"{pairs[top]}.java").as_posix(), text,
                                tuple(sorted(pairs.items())),
                                _self_references(member.source, sorted(pairs))))
    repeated = sorted({name for name in staged_types if staged_types.count(name) > 1})
    if repeated:
        return SourceObservation(
            SourceStatus.UNSUPPORTED,
            reason=f"the pooled types would collide: {', '.join(repeated)}")
    pool = StagedPool(tuple(files), setup)
    issue = unique_paths(pool, by_name=False)
    if issue is not None:
        return SourceObservation(SourceStatus.UNSUPPORTED, reason=issue)
    return pool
