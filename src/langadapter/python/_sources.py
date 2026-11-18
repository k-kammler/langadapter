"""Source operations of the Python adapter (``langadapter.sources``), each
reporting whether it applied, in ``PythonSources``, the adapter's base class,
with ruff's finding count, a target's import line and pooled staging.

Structural operations use tree-sitter (``langadapter._treesitter``) in the
calling process. Merging, normalization, collision resolution, import and
collection pruning and skip stripping first ask the project interpreter and
refuse a source it rejects. Pruning tests and finalizing check with
tree-sitter only. A validator that cannot run is an infrastructure failure,
never a syntax verdict. Pooled staging puts each file's suite slot in its
name, since pytest imports a test file by its name, and stages the text
unchanged. The static analysis that is the same in every language once the
node kinds are fixed comes from ``langadapter._treesitter``.
"""
from __future__ import annotations

from langadapter.python._plugins import TEMPDIR_PREFIX, SOURCE_LABEL
from langadapter.adapter import LangAdapter

import re
from typing import (
    TYPE_CHECKING, FrozenSet, List, Optional, Set, Tuple, Any, Dict, Sequence, Union,
    Iterator, NamedTuple,
)
from langadapter.errors import InvalidRequest, SourceReadError
from langadapter._treesitter import parser_identity, structure_message, StructureResult
from langadapter.python._project_files import import_root, project_file
from langadapter.sources import (
    APPLIED,
    INFRASTRUCTURE_FAILURE,
    NOT_REPAIRED,
    PARSE_ERROR,
    REFUSED,
    UNCHANGED,
    PolicyReport,
    SourceResult,
    SourceValidation,
    StructureCheck,
    TestMetrics,
    SourceObservation,
    _collision_answer,
    SourceStatus,
    PoolMember,
    StagedFile,
    StagedPool,
    check_members,
    check_setup,
    unique_paths,
    SuiteAuditFlag,
    SuiteValidityFinding,
    TargetIdentity,
)
from langadapter.suites import TestId
from langadapter.python import _syntax
import json
import subprocess
import sys
from pathlib import Path, PurePosixPath
import ast
import io
import tokenize
from langadapter.python._profile import library_parser, parser_rejects
import posixpath
from langadapter.python._collisions import _node_line_span, _py_parse_import
import hashlib
from dataclasses import replace
from langadapter.execution import _retain_scratch
from langadapter.python._run_definition import (
    DISABLED_PLUGINS, pytest_arguments, pytest_command,
)
from langadapter.python._validity import (
    POLICY_VERSION as GENERATED_TEST_VALIDITY_POLICY_VERSION,
    analyze_python_generated_test_validity, screen_python_generated_test_source,
)
from langadapter.python.source_analysis import count_code_lines

if TYPE_CHECKING:
    import tree_sitter

from langadapter import _treesitter, templates, sources as _lines

from langadapter._treesitter import (  # the substrate's operations
    _cut_byte_spans,
    _ordered_union,
    check_structure,
    diff_skeletons,
    function_fingerprints,
    non_comment_leaf_fingerprint,
    optimization_metrics,
    parse,
    skeleton,
    walk,
)

#: The node kinds of a module-level statement that only binds names to
#: objects that exist or cannot change: imports, assignments of names,
#: attributes and constants without a call, and the ``if`` and ``try`` blocks
#: around them. Run again, such a statement binds the same objects unless
#: something between changed what it binds or reads. A statement with any
#: other kind can change state each time it runs (a call, a loop, a
#: definition, a deletion, an augmented assignment, a star import) or binds a
#: new object each time (a list, dict or set display, a slice, an operator's
#: result), which a statement between may already hold.
_BINDING_ONLY_KINDS = frozenset({
    "if_statement", "elif_clause", "else_clause", "try_statement", "except_clause",
    "finally_clause", "block", "import_statement", "import_from_statement",
    "aliased_import", "dotted_name", "relative_import", "import_prefix",
    "expression_statement", "assignment", "type", "pass_statement", "identifier",
    "attribute", "comparison_operator", "boolean_operator", "not_operator",
    "unary_operator", "conditional_expression", "parenthesized_expression", "integer",
    "float", "string", "string_start", "string_content", "string_end",
    "escape_sequence", "concatenated_string", "true", "false", "none", "ellipsis",
    "tuple", "expression_list", "pattern_list", "tuple_pattern", "list_pattern",
    "as_pattern", "as_pattern_target", "comment",
})

#: Where an identifier is bound or its object may be changed: assignment
#: targets, loop, ``as`` and ``match`` targets, imports, type aliases,
#: deletions and scope declarations.
_STORE_KINDS = frozenset({
    "assignment", "augmented_assignment", "for_statement", "for_in_clause",
    "as_pattern_target", "import_statement", "import_from_statement",
    "delete_statement", "global_statement", "nonlocal_statement", "named_expression",
    "match_statement", "type_alias_statement",
})


def _named_identifiers(node: "tree_sitter.Node") -> Set[str]:
    """The names ``node`` mentions, attribute members left out."""
    return {n.text.decode("utf-8", "replace") for n in walk(node)
            if n.type == "identifier" and not _attribute_member(n)}


def _attribute_member(node: "tree_sitter.Node") -> bool:
    parent = node.parent
    if parent is None or parent.type != "attribute":
        return False
    member = parent.child_by_field_name("attribute")
    return member is not None and member.id == node.id


def _module_level_parts(node: "tree_sitter.Node") -> Iterator["tree_sitter.Node"]:
    """The nodes of a module-level statement that run when the module runs:
    everything except the bodies of functions and lambdas (decorators,
    defaults, annotations and class bodies run)."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        body = (current.child_by_field_name("body")
                if current.type in ("function_definition", "lambda") else None)
        stack.extend(child for child in current.children
                     if body is None or child.id != body.id)


def _imported_root(node: Optional["tree_sitter.Node"], imported: Set[str]) -> bool:
    """Whether the callee ``node`` (``a.b.c``) is reached from an imported name."""
    while node is not None and node.type == "attribute":
        node = node.child_by_field_name("object")
    return (node is not None and node.type == "identifier"
            and node.text.decode("utf-8", "replace") in imported)


def _can_change(statement: "tree_sitter.Node", names: Set[str], imported: Set[str]) -> bool:
    """Whether running ``statement`` can rebind or change one of ``names``:
    it binds one, deletes one or assigns to its attribute or item, passes one
    to a call or calls on it, or runs code that is no import's (the module's
    own functions, decorators and base classes, a builtin such as
    ``setattr``). Reads (an attribute, an item, an operator) are taken to
    change nothing, as in the copy itself."""
    for node in _module_level_parts(statement):
        if node.type in ("function_definition", "class_definition"):
            name = node.child_by_field_name("name")
            if name is not None and name.text.decode("utf-8", "replace") in names:
                return True
            bases = node.child_by_field_name("superclasses")
            if bases is not None and any(not _imported_root(base, imported)
                                         for base in bases.named_children):
                return True
        elif node.type == "decorator":
            applied = node.named_children[0] if node.named_children else None
            if applied is not None and applied.type != "call" and not _imported_root(
                    applied, imported):
                return True
        elif node.type in _STORE_KINDS:
            target = (node.child_by_field_name("left")
                      if node.type in ("assignment", "augmented_assignment", "for_statement",
                                       "for_in_clause") else node)
            if target is not None and _named_identifiers(target) & names:
                return True
        elif node.type == "call":
            if (_named_identifiers(node) & names
                    or not _imported_root(node.child_by_field_name("function"), imported)):
                return True
    return False


def _shared_statement_spans(first: "tree_sitter.Node", second: "tree_sitter.Node",
                            imports: Tuple[str, ...]) -> List[Tuple[int, int]]:
    """The byte spans of ``second``'s module-level statements to leave out:
    each a copy of one of ``first``'s that only binds names
    (``_BINDING_ONLY_KINDS``), when no statement run between the two copies
    can change what the copy binds or reads (``_can_change``). Each of
    ``first``'s statements stands for one copy. A span runs to the next
    statement, so no blank lines are added, and takes a comment that ends
    the copy's last line."""
    def statements(root):
        return [child for child in root.named_children
                if child.type not in ("comment", "import_statement", "import_from_statement",
                                      "future_import_statement")]

    imported: Set[str] = set()
    for text in imports:
        imported |= _named_identifiers(parse("tree_sitter_python", text).root_node)
    ran = statements(first)
    available: Dict[bytes, int] = {}
    for statement in ran:
        available[statement.text] = available.get(statement.text, 0) + 1
    spans: List[Tuple[int, int]] = []
    siblings = second.named_children
    for statement in statements(second):
        text = statement.text
        if available.get(text, 0) and all(n.type in _BINDING_ONLY_KINDS
                                          for n in walk(statement) if n.is_named):
            last = max(index for index, earlier in enumerate(ran) if earlier.text == text)
            names = _named_identifiers(statement)
            if not any(_can_change(between, names, imported) for between in ran[last + 1:]):
                available[text] -= 1
                following = next(index for index, sibling in enumerate(siblings)
                                 if sibling.id == statement.id) + 1
                if (following < len(siblings) and siblings[following].type == "comment"
                        and siblings[following].start_point[0] == statement.end_point[0]):
                    following += 1
                end = (siblings[following].start_byte if following < len(siblings)
                       else second.end_byte)
                spans.append((statement.start_byte, end))
                continue
        ran.append(statement)
    return spans


def merge_python_modules(
    coverage_src: str, mutation_src: str, *, sentinel: str,
) -> Optional[str]:
    """The two test modules as one: the imports of both merged and hoisted
    (future imports first), then the first body, the ``sentinel`` comment and
    the second body, other module-level code kept verbatim. A statement of
    the second that copies one of the first is left out where running it
    again could change nothing (``_shared_statement_spans``). None when
    either source does not parse or the result does not compile in the
    calling process."""
    cov_root = parse("tree_sitter_python", coverage_src).root_node
    mut_root = parse("tree_sitter_python", mutation_src).root_node
    if cov_root.has_error or mut_root.has_error:
        return None
    cov_bytes = coverage_src.encode("utf-8")
    mut_bytes = mutation_src.encode("utf-8")
    # tree-sitter-python parses ``from __future__ import`` as its own node kind.
    # Left out here, it would stay in the body below the hoisted imports, which
    # is a SyntaxError.
    import_kinds = (
        "import_statement",
        "import_from_statement",
        "future_import_statement",
    )

    def _imports(root: "tree_sitter.Node") -> Tuple[List[str], List[Tuple[int, int]]]:
        texts: List[str] = []
        spans: List[Tuple[int, int]] = []
        for child in root.named_children:
            if child.type in import_kinds:
                texts.append(child.text.decode("utf-8", "replace"))
                spans.append((child.start_byte, child.end_byte))
        return texts, spans

    cov_imports, cov_spans = _imports(cov_root)
    mut_imports, mut_spans = _imports(mut_root)
    union_imports = _ordered_union(cov_imports, mut_imports)
    # Future imports must lead the file, wherever they sat in either source.
    future_imports = [
        i for i in union_imports if i.lstrip().startswith("from __future__")
    ]
    other_imports = [
        i for i in union_imports if not i.lstrip().startswith("from __future__")
    ]
    union_imports = future_imports + other_imports
    shared = _shared_statement_spans(cov_root, mut_root, tuple(union_imports))
    cov_body = _cut_byte_spans(cov_bytes, cov_spans).strip("\n")
    mut_body = _cut_byte_spans(mut_bytes, mut_spans + shared).strip("\n")

    parts: List[str] = []
    if union_imports:
        parts.append("\n".join(union_imports))
    if cov_body:
        parts.append(cov_body)
    parts.append(sentinel)
    if mut_body:
        parts.append(mut_body)
    merged = "\n\n\n".join(parts).rstrip() + "\n"
    # tree-sitter accepts some modules CPython rejects.
    try:
        _syntax.compile_module(merged, "<merged-test-module>")
    except SyntaxError:
        return None
    return merged


def python_top_level_names(
    grammar_module: str,
    source: str,
    *,
    func_kinds: "FrozenSet[str]",
    class_kinds: "FrozenSet[str]",
) -> Set[str]:
    """The names that would collide if two Python modules were concatenated:
    top-level functions and classes and module-level assignment targets. Empty
    on a parse error."""
    root = parse(grammar_module, source).root_node
    if root.has_error:
        return set()
    names: Set[str] = set()
    for child in root.named_children:
        node = child
        if node.type == "decorated_definition":
            inner = node.child_by_field_name("definition")
            node = inner if inner is not None else node
        if node.type in func_kinds or node.type in class_kinds:
            names.add(_treesitter.name(node))
        elif node.type == "expression_statement":
            for assign in node.named_children:
                if assign.type == "assignment":
                    target = assign.child_by_field_name("left")
                    if target is not None and target.type == "identifier":
                        names.add(target.text.decode("utf-8", "replace"))
    names.discard("<anonymous>")
    return names


def prune_python_tests(source: str, remove_ids: "FrozenSet[str]") -> str:
    """``source`` without the named test definitions, unchanged on a parse error.
    Each id (``test_x``, ``Class.test_x`` or a dotted id ending in one) names the
    definition whose qualified name is its longest dot-bounded suffix, so a
    method id never matches a namesake function. A function goes with its
    decorators. A method leaves its class, which goes whole when no other
    statement is left."""
    root = parse("tree_sitter_python", source).root_node
    if root.has_error:
        return source
    src_bytes = source.encode("utf-8")
    func_kinds = {"function_definition"}
    class_kinds = {"class_definition"}

    def _unwrap(node: "tree_sitter.Node") -> "tree_sitter.Node":
        if node.type == "decorated_definition":
            inner = node.child_by_field_name("definition")
            return inner if inner is not None else node
        return node

    def _line_span(node: "tree_sitter.Node") -> Tuple[int, int]:
        # Back over the line's indentation, so a removed method leaves none.
        start = node.start_byte
        while start > 0 and src_bytes[start - 1:start] in (b" ", b"\t"):
            start -= 1
        return start, node.end_byte

    def _definition_span(
        node: "tree_sitter.Node",
        siblings: List["tree_sitter.Node"],
        *,
        preserve_module_header: bool,
    ) -> Tuple[int, int]:
        """The removed definition's span with the comments directly above it."""
        start, end = _line_span(node)
        try:
            index = siblings.index(node)
        except ValueError:
            return start, end
        row = node.start_point[0]
        col = node.start_point[1]
        first = index
        while first > 0:
            previous = siblings[first - 1]
            if previous.type != "comment":
                break
            if previous.start_point[1] != col or previous.end_point[0] != row - 1:
                break
            first -= 1
            row = previous.start_point[0]
        if first == index:
            return start, end
        comment = siblings[first]
        if preserve_module_header and all(
            not line.strip()
            for line in _lines.rows(source)[:comment.start_point[0]]
        ):
            return start, end
        cstart = comment.start_byte
        while cstart > 0 and src_bytes[cstart - 1:cstart] in (b" ", b"\t"):
            cstart -= 1
        return cstart, end

    # Prunable definitions by qualified name: ``function`` or ``Class.method``.
    top_funcs: List[Tuple[str, "tree_sitter.Node"]] = []
    classes: List[Tuple["tree_sitter.Node", "tree_sitter.Node",
                        List[Tuple[str, "tree_sitter.Node"]]]] = []
    quals: set = set()
    for child in root.named_children:
        inner = _unwrap(child)
        if inner.type in func_kinds:
            q = _treesitter.name(inner)
            quals.add(q)
            top_funcs.append((q, child))
        elif inner.type in class_kinds:
            cls_name = _treesitter.name(inner)
            body = inner.child_by_field_name("body")
            if body is None:
                continue
            methods: List[Tuple[str, "tree_sitter.Node"]] = []
            for m in body.named_children:
                mi = _unwrap(m)
                if mi.type in func_kinds:
                    q = f"{cls_name}.{_treesitter.name(mi)}"
                    quals.add(q)
                    methods.append((q, m))
            classes.append((child, body, methods))

    removed = {q for q in (templates.named_unit(rid, quals) for rid in remove_ids)
               if q is not None}
    if not removed:
        return source

    spans: List[Tuple[int, int]] = []
    root_siblings = list(root.named_children)
    for q, child in top_funcs:
        if q in removed:
            spans.append(_definition_span(
                child, root_siblings, preserve_module_header=True,
            ))
    for cls_node, body, methods in classes:
        matched = [m for (q, m) in methods if q in removed]
        if not matched:
            continue
        # Only statements count: a class left with only comments has an empty
        # body, which does not parse, so it goes whole with its comments.
        members = [m for m in body.named_children if m.type != "comment"]
        if all(m in matched for m in members):
            spans.append(_definition_span(
                cls_node, root_siblings, preserve_module_header=True,
            ))
        else:
            body_siblings = list(body.named_children)
            spans.extend(
                _definition_span(
                    m, body_siblings, preserve_module_header=False,
                )
                for m in matched
            )
    if not spans:
        return source
    # At most two blank lines stay where definitions were cut.
    return _treesitter.apply_edits_collapsing(source, [(s, e, "") for s, e in spans], 2)


#: The parser of the operations that read a source with Python's ast.
_HOST_AST = "python ast of the calling process"


class PythonSources(LangAdapter):
    """The Python adapter's source operations: parsing and validating test
    sources, their metrics and skeletons, merges, collisions and repairs.
    ``PythonAdapter`` adds the project's runs. The operations that need the
    project's interpreter reach it through the adapter's project handle."""

    _TS_GRAMMAR = "tree_sitter_python"
    _TS_IGNORE_KINDS = frozenset({"comment"})
    _TS_CALL_KINDS = frozenset({"call"})
    _TS_FUNC_KINDS = frozenset({"function_definition"})
    _TS_CLASS_KINDS = frozenset({"class_definition"})
    # ``optimization_metrics`` vocabulary: loop kinds give loop_count and
    # loop_max_nesting. ``for_in_clause`` is each ``for`` of a comprehension
    # (``[y for x in xs for y in x]`` is two loops). lizard reads the
    # complexity and the lines of code as Python, from a file ending in
    # ``_LIZARD_SUFFIX``.
    _TS_LOOP_KINDS = frozenset({
        "for_statement", "while_statement", "for_in_clause",
    })
    _LIZARD_SUFFIX = ".py"
    # IO calls (a noisy hint): file, socket and subprocess primitives. Generic
    # method names (read, write, get, post, execute) match far more non-IO
    # calls and are left out.
    _IO_CALL_NAMES = frozenset({
        "open", "urlopen", "urlretrieve", "create_connection",
        "socket", "connect", "recv", "recvfrom", "recv_into",
        "sendall", "sendto", "Popen", "system", "check_output",
        "check_call",
    })

    def check_structure(self, source: str) -> Any:
        """Whether ``source`` parses, with its first error (``StructureCheck``)."""
        result = self._tree_check(source)
        return StructureCheck(result.ok, result.message, result.line,
                              result.col, self.structural_parser_identity(), getattr(result, "snippet", ""))

    def structural_parser_identity(self) -> str:
        """The structural parser and grammar used by source operations."""
        return parser_identity(self._TS_GRAMMAR.replace("_", "-"))

    def test_metrics(self, source: str) -> Any:
        """The counts of a test source, absent when the structural parser
        or the library's Python parser, which reads its tests, rejects it
        (``TestMetrics``)."""
        error = self._parse_error(source)
        if error:
            return TestMetrics(parsed=False, parser=self.structural_parser_identity(), detail=error)
        try:
            tree = _syntax.parse_quietly(source)
        except SyntaxError as exc:
            return TestMetrics(parsed=False, parser=library_parser(), detail=parser_rejects(
                f"{exc.msg} (line {exc.lineno}, column {exc.offset})"))
        counts = self._test_counts(source, tree)
        return TestMetrics(
            parsed=True, parser=self.structural_parser_identity(), code_lines=counts["loc"],
            tests=counts["test_count"], assertions=counts["assertion_count"],
            fixtures=counts["fixture_count"],
            parametrized_cases=counts["parametrize_case_count"])

    def skeleton(self, source: str) -> Tuple[str, ...]:
        """Qualified function/class names from the calling process's grammar."""
        return skeleton(
            self._TS_GRAMMAR, source,
            func_kinds=self._TS_FUNC_KINDS,
            class_kinds=self._TS_CLASS_KINDS,
        )

    def same_skeleton(self, first: str, second: str) -> Tuple[bool, str]:
        """Whether sources have the same static definition names, with a reason."""
        return self._same_skeleton(first, second)

    def function_fingerprints(self, source: str) -> Dict[str, str]:
        """Static per-definition fingerprints, excluding comments."""
        return function_fingerprints(
            self._TS_GRAMMAR, source,
            func_kinds=self._TS_FUNC_KINDS,
            class_kinds=self._TS_CLASS_KINDS,
            ignore_kinds=self._TS_IGNORE_KINDS,
        )

    def optimization_metrics(self, source: str) -> Dict[str, Any]:
        """Static structural metrics. These are not execution measurements."""
        return optimization_metrics(
            self._TS_GRAMMAR, source,
            func_kinds=self._TS_FUNC_KINDS,
            loop_kinds=self._TS_LOOP_KINDS,
            call_kinds=self._TS_CALL_KINDS,
            lizard_suffix=self._LIZARD_SUFFIX,
            class_kinds=self._TS_CLASS_KINDS,
            io_call_names=self._IO_CALL_NAMES,
        )

    def scoped_optimization_metrics(self, source: str) -> Any:
        """The structural metrics of ``source`` per declaration (scoped metrics)."""
        from langadapter._treesitter import scoped_optimization_metrics
        return scoped_optimization_metrics(
            self._TS_GRAMMAR, source,
            func_kinds=self._TS_FUNC_KINDS,
            loop_kinds=self._TS_LOOP_KINDS,
            call_kinds=self._TS_CALL_KINDS,
            lizard_suffix=self._LIZARD_SUFFIX,
            class_kinds=self._TS_CLASS_KINDS,
            io_call_names=self._IO_CALL_NAMES)

    def call_statistics(self, source: str) -> Optional[Dict[str, int]]:
        """The call sites of ``source`` by called name, None when it does
        not parse: the call search of ``sources.call_statistics``."""
        from langadapter._treesitter import called_names
        return called_names(self._TS_GRAMMAR, source, call_kinds=self._TS_CALL_KINDS)

    def imports(self, source: str) -> Tuple[str, ...]:
        """Static import names in source order, relative ones kept relative. An
        unparseable source gives none (``check_structure`` tells it from a source
        without imports)."""
        root = parse(self._TS_GRAMMAR, source).root_node
        if root.has_error:
            return ()
        out: List[str] = []
        for n in walk(root):
            if n.type == "import_statement":
                # ``import a, b.c``: ``dotted_name`` or ``aliased_import``.
                for child in n.named_children:
                    if child.type == "dotted_name":
                        out.append(child.text.decode("utf-8", "replace"))
                    elif child.type == "aliased_import":
                        name_node = child.child_by_field_name("name")
                        if name_node is not None:
                            out.append(
                                name_node.text.decode("utf-8", "replace")
                            )
            elif n.type == "import_from_statement":
                module = n.child_by_field_name("module_name")
                if module is None:
                    continue
                if module.type == "dotted_name":
                    out.append(module.text.decode("utf-8", "replace"))
                elif module.type == "relative_import":
                    # Kept as written: its dots need the importer's package,
                    # and ``import_path_candidates`` gives no paths for it.
                    text = module.text.decode("utf-8", "replace")
                    out.append(text)
        # Deduplicate while preserving order.
        seen: set = set()
        deduped: List[str] = []
        for name in out:
            if name not in seen:
                seen.add(name)
                deduped.append(name)
        return tuple(deduped)

    def import_path_candidates(self, name: str) -> Tuple[str, ...]:
        """Possible source paths of an absolute import, without resolving it."""
        if not name or name.startswith("."):
            # ``imports`` returns relative imports as written, and resolving
            # one needs the importer's package, which a name alone lacks.
            return ()
        as_path = name.replace(".", "/")
        return (f"{as_path}.py", f"{as_path}/__init__.py")

    def duplicate_test_names(self, source: str) -> list[str]:
        """Test bindings shadowed in the same source scope. A source this
        process's parser cannot read raises ``SourceReadError``."""
        try:
            return self._duplicate_test_names(source)
        except SourceReadError as error:
            raise self._reading_error(error) from None

    def merge_collisions(self, first: str, second: str) -> Tuple[str, ...]:
        """Top-level function and class names and module-level assignment
        names that two sources share, without rewriting."""
        return self._merge_collisions(first, second)

    def stage_pool(self, members: Any, *, setup: Any = ()) -> Any:
        """Several suites' test files as one pool, each file named for its
        suite (``langadapter.sources.StagedPool``)."""
        from langadapter.sources import SourceObservation, SourceStatus
        result = stage_pool(members, setup)
        if isinstance(result, SourceObservation) and result.status is SourceStatus.PARSE_ERROR:
            return replace(result, reason=str(self._reading_error(SourceReadError(result.reason))))
        return result

    def is_test_file(self, path: Any) -> bool:
        """Recognize pytest/unittest filenames, including setup files."""
        return self._is_test_file(path)

    def order_dependent_mock_warnings(self, source: str) -> list[str]:
        """Tests that assert on a named element of a mock's ``side_effect`` list,
        whose order a scheduler in the code under test can change (advisory). A
        source this process's parser cannot read raises ``SourceReadError``."""
        try:
            return self._order_dependent_mock_warnings(source)
        except SourceReadError as error:
            raise self._reading_error(error) from None

    def _reading_error(self, error: SourceReadError) -> SourceReadError:
        """``error``, naming both Pythons when the project's is newer than the
        library's. Without a context interpreter, the library's alone."""
        interpreter = self.context.interpreter
        note = (self.profile().reading_note()
                if interpreter is not None and interpreter.path is not None else "")
        return SourceReadError(f"{error}. {note}", error.parser) if note else error

    def _readable(self, *sources: str) -> None:
        """Raise the reading error naming both Pythons when the project's is
        newer than the library's and this process's parser cannot read one
        of ``sources``, so that no operation gives up on newer syntax
        quietly. Otherwise the operation answers as before."""
        for source in sources:
            try:
                _syntax.parse(source)
            except SyntaxError as error:
                rejected = SourceReadError(parser_rejects(
                    f"{error.msg} (line {error.lineno}, column {error.offset})"), library_parser())
                reading = self._reading_error(rejected)
                if reading is not rejected:
                    raise reading from None

    def validate_source(self, source: str) -> Any:
        """``SourceValidation``: whether the project interpreter compiles ``source``."""
        verdict = self._validate_test_source(source, str(self.context.root))
        return replace(verdict, status=verdict.status if verdict.status in (
            "valid", "syntax_error", "timeout") else INFRASTRUCTURE_FAILURE)

    def prune_tests(self, source: str, tests: Any) -> Any:
        """``source`` without the named tests (``SourceResult``)."""

        names: List[str] = []
        cases: Dict[str, Set[str]] = {}
        for test in tests:
            if isinstance(test, TestId):
                qualname, params = test.qualname, test.params
            else:
                text = str(test)
                head, bracket, rest = text.partition("[")
                qualname, params = ((head, rest[:-1]) if bracket and rest.endswith("]")
                                    else (text, None))
            if not qualname:
                raise InvalidRequest("tests", "a test name is empty")
            if params is None:
                if qualname not in names:
                    names.append(qualname)
            else:
                cases.setdefault(qualname, set()).add(params)
        error = self._parse_error(source)
        if error:
            return SourceResult(PARSE_ERROR, source, self.structural_parser_identity(),
                                unmatched=tuple(names) + tuple(cases), detail=error)
        defined = set(skeleton(
            self._TS_GRAMMAR, source, func_kinds=self._TS_FUNC_KINDS,
            class_kinds=self._TS_CLASS_KINDS))
        partial = {name: ids for name, ids in cases.items() if name not in names}
        removed_cases: Tuple[str, ...] = ()
        whole: Dict[str, str] = {}
        rest_source = source
        if partial:
            # Pruning cases reads the syntax tree: newer syntax is a reading
            # error here, not a reason to remove the tests whole.
            self._readable(source)
            try:
                rest_source, removed_cases, whole = prune_cases(source, partial)
            except SyntaxError:
                whole = dict.fromkeys(partial, "the calling process cannot parse the source")
        for name in whole:
            if name not in names:
                names.append(name)
        pruned = prune_python_tests(rest_source, frozenset(names))
        remaining = set(skeleton(
            self._TS_GRAMMAR, pruned, func_kinds=self._TS_FUNC_KINDS,
            class_kinds=self._TS_CLASS_KINDS))
        matched: List[str] = []
        unmatched: List[str] = []
        for name in names:
            definition = max(
                (q for q in defined if name == q or name.endswith("." + q)),
                key=len, default=None)
            if definition is None or definition in remaining:
                unmatched.append(name)
            elif definition not in matched:
                matched.append(definition)
        return SourceResult(APPLIED if pruned != source else UNCHANGED,
                            pruned, self.structural_parser_identity(), unmatched=tuple(unmatched),
                            removed=tuple(matched) + removed_cases,
                            detail=". ".join(
                                f"{name} is removed whole: {reason}"
                                for name, reason in whole.items()
                                if name not in unmatched))

    def merge_sources(self, first: str, second: str, *,
                      separator: str) -> Any:
        """Two test modules as one (``SourceResult``)."""
        if not separator.startswith("#") or "\n" in separator or "\r" in separator:
            raise InvalidRequest("separator", "must be one comment line")
        self._readable(first, second)
        for label, text in (("the first source", first),
                            ("the second source", second)):
            error = self._parse_error(text)
            if error:
                return SourceResult(PARSE_ERROR, first, self.structural_parser_identity(),
                                    detail=f"{label} does not parse: {error}")
            refusal = self._refusal(text, label)
            if refusal is not None:
                return SourceResult(refusal.status, first, self.structural_parser_identity(),
                                    detail=refusal.detail)
        merged = merge_python_modules(first, second,
                                                  sentinel=separator)
        if merged is None:
            return SourceResult(
                REFUSED, first, self.structural_parser_identity(),
                detail=("the calling process's compiler rejects the merged "
                        "module"))
        refusal = self._refusal(merged, "the merged module")
        if refusal is not None:
            return SourceResult(refusal.status, first, self.structural_parser_identity(),
                                detail=refusal.detail)
        return SourceResult(APPLIED, merged, self.structural_parser_identity())

    def normalize_shadowed(self, source: str) -> Any:
        """``source`` without shadowed definitions (``SourceResult``)."""
        self._readable(source)
        refusal = self._refusal(source, "the source")
        if refusal is not None:
            return refusal
        normalization = self._normalize_shadowed_definitions(source)
        if not normalization.parse_ok:
            return SourceResult(
                PARSE_ERROR, source, _HOST_AST,
                detail=normalization.parse_error or "")
        removed = tuple(".".join((*b.scope, b.name))
                        for b in normalization.removed_bindings)
        return SourceResult(
            APPLIED if normalization.changed else UNCHANGED,
            normalization.source, _HOST_AST,
            removed=removed)

    def normalize_bindings(self, source: str) -> Any:
        """Static full binding report, without target-runtime validation."""
        from langadapter.python.source_analysis import normalize_shadowed_definitions
        self._readable(source)
        return normalize_shadowed_definitions(source)

    def normalization_parser_identity(self) -> str:
        """The calling interpreter's AST grammar used for binding analysis."""
        return library_parser()

    def resolve_collisions(self, sources: Any, *, preference: Any = None,
                           localize_autouse: bool = False) -> Any:
        """Module-level name collisions among test sources resolved before
        they are merged into one module, as a ``SourceObservation`` of a
        ``CollisionResolution``. Without a ``preference`` a conflict is
        reported and nothing dropped."""
        sources = tuple(sources)
        self._readable(*sources)
        kept: List[int] = []
        rejected: List[Tuple[int, str]] = []
        validation_failures: List[Tuple[int, str]] = []
        for index, source in enumerate(sources):
            refusal = self._refusal(source, f"source {index}")
            if refusal is None:
                kept.append(index)
            else:
                rejected.append((index, refusal.detail))
                if refusal.status == INFRASTRUCTURE_FAILURE:
                    validation_failures.append((index, refusal.detail))

        if preference is None:
            def choose(name: str, owners: Tuple[int, ...]) -> Optional[int]:
                return None
        else:
            def choose(name: str, owners: Tuple[int, ...]) -> Optional[int]:
                chosen = preference(name, tuple(kept[i] for i in owners))
                if chosen not in kept or kept.index(chosen) not in owners:
                    raise InvalidRequest(
                        "preference",
                        f"kept source {chosen} for {name!r}, which is not "
                        f"one of {[kept[i] for i in owners]}")
                return kept.index(chosen)

        resolution = self._resolve_test_collisions(
            [sources[i] for i in kept], localize_autouse=localize_autouse,
            choose=choose) if kept else None
        out = list(sources)
        renamed: List[Tuple[Tuple[str, str], ...]] = [() for _ in sources]
        shared: List[Tuple[str, ...]] = [() for _ in sources]
        dropped: List[Tuple[str, ...]] = [() for _ in sources]
        localized: List[Tuple[str, ...]] = [() for _ in sources]
        if resolution is not None:
            for local, index in enumerate(kept):
                if local in resolution.ejected:
                    reason = resolution.eject_reasons[
                        resolution.ejected.index(local)]
                    rejected.append((index, reason))
                    continue
                out[index] = resolution.sources[local]
                renamed[index] = tuple(sorted(
                    resolution.renames[local].items()))
                shared[index] = tuple(resolution.deduped[local])
                lost = self._lost_definitions(
                    sources[index], out[index], resolution.renames[local])
                dropped[index] = tuple(dict.fromkeys(
                    list(resolution.dropped_defs[local]) + lost))
                if resolution.localized_autouse:
                    localized[index] = tuple(
                        resolution.localized_autouse[local])
        choices = tuple(
            (name, kept[holder], tuple(kept[i] for i in losers))
            for name, holder, losers in (resolution.choices
                                         if resolution else ()))
        conflicts = tuple(
            (name, tuple(kept[i] for i in owners))
            for name, owners in (resolution.conflicts if resolution else ()))
        return _collision_answer(
            self.structural_parser_identity(), tuple(validation_failures), rejected, renamed,
            sources=tuple(out), deduped=tuple(shared), dropped_defs=tuple(dropped),
            localized_autouse=tuple(localized), choices=choices, conflicts=conflicts)

    def salvage_syntax(self, source: str) -> Any:
        """A bounded repair of a source the project interpreter does not
        compile (``SourceResult``, a generated-suite operation)."""
        report: Dict[str, Any] = {}
        repaired = self._salvage_syntax(
            source, str(self.context.root), report=report)
        status = report.get("status")
        parser = "the project interpreter"
        if status == "valid":
            return SourceResult(UNCHANGED, source, parser)
        if status == "repaired" and repaired is not None:
            return SourceResult(APPLIED, repaired, parser,
                                detail=f"{report['repairs']} repair(s)", repairs=report["repairs"])
        if status == "validator_failed":
            return SourceResult(
                INFRASTRUCTURE_FAILURE, source, parser,
                detail=f"the validator could not run: {report.get('message')}")
        return SourceResult(
            NOT_REPAIRED, source, parser,
            detail=(f"still does not compile after {report.get('repairs', 0)}"
                    f" repair(s): {report.get('message')}"))

    def prune_unresolvable_imports(self, source: str) -> Any:
        """``source`` without imports that do not resolve in the project
        environment (``SourceResult``, a generated-suite operation)."""
        refusal = self._refusal(source, "the source")
        if refusal is not None:
            return refusal
        report: Dict[str, Any] = {}
        pruned, dropped = self._prune_unresolvable_imports(
            source, str(self.context.root), report=report)
        status = report.get("status")
        if status == "parse_error":
            return SourceResult(PARSE_ERROR, source, self.structural_parser_identity(),
                                detail=self._parse_error(source))
        if status == "probe_failed":
            return SourceResult(
                INFRASTRUCTURE_FAILURE, source, self.structural_parser_identity(),
                detail=f"the import probe could not run: {report.get('message')}")
        if pruned == source:
            return SourceResult(UNCHANGED, source, self.structural_parser_identity())
        return SourceResult(APPLIED, pruned, self.structural_parser_identity(),
                            removed=tuple(dropped))

    def prune_collection_breakers(self, source: str, *,
                                  timeout_s: float) -> Any:
        """``source`` with the module-level or class-level statements that
        break pytest's collection replaced by ``pass`` (``SourceResult``, a
        generated-suite operation)."""
        refusal = self._refusal(source, "the source")
        if refusal is not None:
            return refusal
        report: Dict[str, Any] = {}
        try:
            fixed, removed = self._prune_collection_breakers(
                source, str(self.context.root), timeout=timeout_s,
                report=report)
        except OSError as exc:
            return SourceResult(
                INFRASTRUCTURE_FAILURE, source, self.structural_parser_identity(),
                detail=f"collection could not execute: {exc}")
        status = report.get("status")
        if status == "collects":
            return SourceResult(UNCHANGED, source, self.structural_parser_identity())
        if status == "pruned":
            return SourceResult(APPLIED, fixed, self.structural_parser_identity(),
                                removed=tuple(removed))
        if status == "timed_out":
            return SourceResult(
                INFRASTRUCTURE_FAILURE, source, self.structural_parser_identity(),
                detail=f"collection reached the timeout of {timeout_s:g} s")
        if status == "execution_failed":
            return SourceResult(
                INFRASTRUCTURE_FAILURE, source, self.structural_parser_identity(),
                detail=f"{report.get('reason', 'collection could not execute')}: "
                       f"{report.get('message', '')}".rstrip(": "))
        reasons = {
            "no_location": "the collection error names no line of the file",
            "not_blankable": "the failing line lies in no module-level "
                             "statement that can be removed",
            "exhausted": "collection still fails after five rounds",
        }
        return SourceResult(
            NOT_REPAIRED, source, self.structural_parser_identity(),
            detail=f"{reasons.get(status, status)}: "
                   f"{report.get('message', '')}".rstrip(": "))

    def strip_module_level_skips(self, source: str) -> Any:
        """``source`` without module-level skip guards (``SourceResult``, a
        generated-suite operation)."""
        self._readable(source)
        refusal = self._refusal(source, "the source")
        if refusal is not None:
            return refusal
        parser = _HOST_AST
        try:
            _syntax.parse(source)
        except SyntaxError as exc:
            return SourceResult(PARSE_ERROR, source, parser,
                                detail=f"{exc.msg} (line {exc.lineno})")
        stripped, count = self._strip_module_level_skips(source)
        if not count:
            return SourceResult(UNCHANGED, source, parser)
        return SourceResult(APPLIED, stripped, parser,
                            detail=f"{count} guard(s) removed")

    def target_identity(self, target: str) -> Any:
        """The identity of a file under test for the generated-test validity
        policy (``TargetIdentity``): its module named from the import root
        (``python_import_root`` below the project root), as the tests import
        it."""
        root = import_root(self.context)
        path = project_file(self, "target", target)
        try:
            return self._generated_test_target_identity(
                str(path), str(root))
        except ValueError as exc:
            raise InvalidRequest("target", str(exc)) from None

    def analyze_validity(self, source: str, target: Any) -> Any:
        """The validity policy's findings on a generated test source
        (``PolicyReport``, a generated-suite operation)."""
        from langadapter.python._validity import POLICY_VERSION
        self._readable(source, target.target_source or "")
        try:
            findings = self._analyze_generated_test_validity(
                source, target)
        except SyntaxError as exc:
            return PolicyReport(
                PARSE_ERROR, (), POLICY_VERSION, _HOST_AST,
                detail=str(exc), error_line=int(exc.lineno or 0),
                error_column=int(exc.offset or 0))
        return PolicyReport("completed", tuple(findings), POLICY_VERSION,
                            _HOST_AST)

    def screen_source(self, source: str, target: Any) -> Any:
        """The policy's review-only screening signals on a generated test
        source (``PolicyReport``, a generated-suite operation)."""
        from langadapter.python._validity import POLICY_VERSION
        self._readable(source, target.target_source or "")
        flags = tuple(self._screen_generated_test_source(source, target))
        if len(flags) == 1 and flags[0].rule_id == "unparseable_source":
            return PolicyReport(
                PARSE_ERROR, (), POLICY_VERSION, _HOST_AST,
                detail=flags[0].diagnostic, error_line=flags[0].start_line,
                error_column=flags[0].start_col)
        return PolicyReport("completed", flags, POLICY_VERSION, _HOST_AST)

    def finalize_source(self, source: str) -> Any:
        """``source`` without detached comment blocks (``SourceResult``)."""
        error = self._parse_error(source)
        if error:
            return SourceResult(PARSE_ERROR, source, self.structural_parser_identity(),
                                detail=error)
        finalized = self._strip_detached_comments(source)
        return SourceResult(APPLIED if finalized != source else UNCHANGED,
                            finalized, self.structural_parser_identity())

    # Helpers

    def _parse_error(self, source: str) -> str:
        """The first parse error of ``source`` as text, or ""."""
        check = self._tree_check(source)
        if check.ok:
            return ""
        return f"{structure_message(check)} (line {check.line}, column {check.col})"

    def _refusal(self, source: str, label: str) -> Union[SourceResult, None]:
        """A result refusing to rewrite ``source`` when the project interpreter
        rejects it or cannot be asked, None when it accepts."""
        verdict = self.validate_source(source)
        if verdict.valid:
            return None
        if verdict.status == "syntax_error":
            return SourceResult(
                REFUSED, source, self.structural_parser_identity(),
                detail=(f"the project interpreter rejects {label}: "
                        f"{verdict.message} (line {verdict.line}, column "
                        f"{verdict.column})"))
        return SourceResult(
            INFRASTRUCTURE_FAILURE, source, self.structural_parser_identity(),
            detail=(f"the project interpreter could not validate {label}: "
                    f"{verdict.message}"))

    def _lost_definitions(self, before: str, after: str,
                          renames: Dict[str, str]) -> List[str]:
        """The functions and classes of ``before`` missing from ``after``
        other than by a rename: the dependents a dropped binding took."""
        def names(text: str) -> List[str]:
            return list(skeleton(
                self._TS_GRAMMAR, text, func_kinds=self._TS_FUNC_KINDS,
                class_kinds=self._TS_CLASS_KINDS))

        remaining = set(names(after))
        lost = []
        for name in names(before):
            head, _, rest = name.partition(".")
            renamed = renames.get(head)
            moved = (f"{renamed}.{rest}" if rest else renamed) if renamed \
                else None
            if name not in remaining and moved not in remaining:
                lost.append(name)
        return lost

    def _is_test_file(self, file_path: Path) -> bool:
        """Whether the basename follows pytest and unittest naming, ignoring
        case: ``test_*.py``, ``*_test.py``, ``conftest.py``, ``tests.py``. A
        ``tests/`` directory is the caller's case."""
        name = Path(file_path).name.lower()
        if not name.endswith(".py"):
            return False
        if name in {"conftest.py", "tests.py"}:
            return True
        return name.startswith("test_") or name.endswith("_test.py")

    @staticmethod
    def _unwrap_ctx(node):
        """A ``with``-item's value without its ``as_pattern`` and
        ``parenthesized_expression`` wrappers, as the AST's
        ``withitem.context_expr`` sees it."""
        if node is None:
            return None
        if node.type in ("as_pattern", "parenthesized_expression"):
            kids = node.named_children
            return PythonSources._unwrap_ctx(kids[0]) if kids else None
        return node

    @staticmethod
    def _dotted(expr) -> Optional[tuple]:
        """The dotted path of a callee or decorator, a call unwrapped
        (``("pytest", "mark", "parametrize")``), None for anything but a
        name and attribute chain."""
        if expr is None:
            return None
        if expr.type == "call":
            return PythonSources._dotted(expr.child_by_field_name("function"))
        if expr.type == "identifier":
            return (expr.text.decode("utf-8", "replace"),)
        if expr.type == "attribute":
            base = PythonSources._dotted(expr.child_by_field_name("object"))
            attr = expr.child_by_field_name("attribute")
            if base is None or attr is None:
                return None
            return base + (attr.text.decode("utf-8", "replace"),)
        return None

    @staticmethod
    def _decorator_exprs(func_node) -> list:
        """A ``function_definition``'s decorator expressions, which
        tree-sitter puts on the enclosing ``decorated_definition``."""
        parent = func_node.parent
        if parent is None or parent.type != "decorated_definition":
            return []
        out = []
        for child in parent.children:
            if child.type == "decorator":
                exprs = list(child.named_children)
                if exprs:
                    out.append(exprs[0])
        return out

    @classmethod
    def _parametrize_cases(cls, deco_expr, names: Dict[str, int]) -> Optional[int]:
        """The number of cases of a ``@pytest.mark.parametrize`` or
        ``@mark.parametrize``: its literal case list, in place, as
        ``argvalues=`` or a module-level name in ``names``. None for another
        decorator, 0 for a list that is not literal (no guess)."""
        if deco_expr is None or deco_expr.type != "call":
            return None
        if cls._dotted(deco_expr) not in (("pytest", "mark", "parametrize"),
                                          ("mark", "parametrize")):
            return None
        args = deco_expr.child_by_field_name("arguments")
        if args is None:
            return 0
        positional = [
            c for c in args.named_children if c.type != "keyword_argument"
        ]
        cases = positional[1] if len(positional) >= 2 else next((
            c.child_by_field_name("value") for c in args.named_children
            if c.type == "keyword_argument"
            and (c.child_by_field_name("name") is not None)
            and c.child_by_field_name("name").text == b"argvalues"), None)
        if cases is None:
            return 0
        if cases.type == "identifier":
            return names.get(cases.text.decode("utf-8", "replace"), 0)
        return cls._literal_length(cases)

    @classmethod
    def _literal_length(cls, node) -> int:
        """The number of elements of a literal list or tuple, 0 for anything
        else. Tree-sitter's comment children are no elements."""
        if node is None or node.type not in ("list", "tuple"):
            return 0
        return sum(1 for e in node.named_children if e.type not in cls._TS_IGNORE_KINDS)

    @classmethod
    def _literal_lists(cls, root) -> Dict[str, int]:
        """The module-level names bound to a literal list or tuple, with
        their lengths: a parametrize list a test names."""
        names: Dict[str, int] = {}
        for statement in root.named_children:
            if statement.type != "expression_statement":
                continue
            for assignment in statement.named_children:
                if assignment.type != "assignment":
                    continue
                left = assignment.child_by_field_name("left")
                right = assignment.child_by_field_name("right")
                if left is not None and left.type == "identifier" and right is not None:
                    name = left.text.decode("utf-8", "replace")
                    if right.type in ("list", "tuple"):
                        names[name] = cls._literal_length(right)
                    else:
                        names.pop(name, None)
        return names

    def _test_counts(self, source: str, tree: Any) -> Dict[str, int]:
        """The counts of ``source``, which both parsers read, ``tree`` the
        Python parser's (``test_metrics``)."""
        # loc counts code lines, not comment-only ones: a pruned suite can
        # keep its removed tests' comment blocks.
        out = {
            "loc": count_code_lines(source),
            "test_count": len(_syntax.collected_tests(tree)),
            "assertion_count": 0,
            "fixture_count": 0,
            "parametrize_case_count": 0,
        }
        root = parse(self._TS_GRAMMAR, source).root_node
        literal_lists = self._literal_lists(root)

        for node in walk(root):
            t = node.type
            if t == "function_definition":
                decos = self._decorator_exprs(node)
                for d in decos:
                    dp = self._dotted(d)
                    if dp in (("pytest", "fixture"), ("fixture",)):
                        out["fixture_count"] += 1
                        break
                # pytest runs a test once per combination of its stacked
                # parameter lists.
                counts = [c for c in (self._parametrize_cases(d, literal_lists) for d in decos)
                          if c is not None]
                if counts:
                    product = 1
                    for count in counts:
                        product *= count
                    out["parametrize_case_count"] += product
            elif t == "assert_statement":
                out["assertion_count"] += 1
            elif t == "with_statement":
                for clause in node.children:
                    if clause.type != "with_clause":
                        continue
                    for item in clause.children:
                        if item.type != "with_item":
                            continue
                        ctx = self._unwrap_ctx(
                            item.child_by_field_name("value")
                        )
                        dp = self._dotted(ctx) if ctx is not None else None
                        if dp in (("pytest", "raises"), ("pytest", "warns")):
                            out["assertion_count"] += 1
        return out

    def _resolve_test_collisions(
        self, sources, *, localize_autouse: bool = False, choose=None,
    ):
        """The ``CollisionResolution`` of independently generated test
        suites to merge into one module
        (``langadapter.python._collisions.resolve_python_collisions``): the
        caller leaves its ``ejected`` indices out of the merge. ``choose``
        decides which source keeps a name several bind un-renamably."""
        from langadapter.python._collisions import (
            resolve_python_collisions,
        )
        return resolve_python_collisions(
            sources, localize_autouse=localize_autouse, choose=choose,
        )

    def _normalize_shadowed_definitions(self, source: str):
        from langadapter.python.source_analysis import normalize_shadowed_definitions
        return normalize_shadowed_definitions(source)

    def _generated_test_target_identity(
        self, target_file: str, project_root: str,
    ) -> TargetIdentity:
        source_path = str(target_file)
        root = str(project_root)
        module_name = file_to_module_name(source_path, root, container=self._container())
        if not module_name:
            raise ValueError(
                f"cannot derive an importable module name for target {target_file!r} "
                f"under project root {project_root!r}"
            )
        target_source = self._read_file_content(source_path)
        canonical_path = (
            str(Path(source_path).resolve())
            if self._exec_mode() != "container"
            else source_path
        )
        canonical_root = (
            str(Path(root).resolve())
            if self._exec_mode() != "container"
            else root
        )
        return TargetIdentity(
            source_path=canonical_path,
            project_root=canonical_root,
            module_name=module_name,
            language=self.language,
            policy_version=GENERATED_TEST_VALIDITY_POLICY_VERSION,
            target_source_sha256=hashlib.sha256(
                target_source.encode("utf-8")
            ).hexdigest(),
            target_source=target_source,
        )

    def _analyze_generated_test_validity(
        self, source: str, target_identity: TargetIdentity,
    ) -> Tuple[SuiteValidityFinding, ...]:
        if target_identity.language != self.language:
            raise ValueError(
                "generated-test target identity language mismatch: "
                f"{target_identity.language!r} != {self.language!r}"
            )
        return analyze_python_generated_test_validity(source, target_identity)

    def _screen_generated_test_source(
        self, source: str, target_identity: TargetIdentity,
    ) -> Tuple[SuiteAuditFlag, ...]:
        return screen_python_generated_test_source(source, target_identity)

    def _validate_test_source(
        self, source: str, project_root: str,
    ) -> SourceValidation:
        """Parse ``source`` with the configured target Python interpreter. A
        validator that does not report is ``validator_error``."""
        source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
        validator = (
            "import json,platform,sys\n"
            "src=sys.stdin.read()\n"
            "out={'identity':platform.python_implementation()+' '+"
            "platform.python_version()+' ('+sys.executable+')'}\n"
            "try:\n"
            " compile(src," + repr(SOURCE_LABEL) + ",'exec')\n"
            "except SyntaxError as exc:\n"
            " out.update(ok=False,status='syntax_error',message=exc.msg or "
            "'syntax error',line=int(exc.lineno or 0),col=int(exc.offset or 0))\n"
            "except ValueError as exc:\n"
            " out.update(ok=False,status='syntax_error',message=str(exc),line=0,col=0)\n"
            "else:\n"
            " out.update(ok=True,status='valid',message='',line=0,col=0)\n"
            "print(json.dumps(out,sort_keys=True))\n"
        )
        try:
            run = self._executor().run(
                [self.interpreter, *self._runtime_flags(), "-c", validator],
                cwd=project_root,
                env=self._build_env(project_root),
                timeout=30,
                input=source,
            )
        except subprocess.TimeoutExpired:
            return SourceValidation(
                "timeout", "target parser timed out", 0, 0,
                str(self.interpreter), source_sha256,
            )
        except Exception as exc:
            return SourceValidation(
                "validator_error", str(exc), 0, 0,
                str(self.interpreter), source_sha256,
            )
        try:
            payload = json.loads((run.stdout or "").strip())
        except (TypeError, ValueError):
            detail = ((run.stderr or "") or (run.stdout or "")).strip()[:500]
            return SourceValidation(
                "validator_error",
                detail or f"target parser exited with code {run.returncode}",
                0, 0, str(self.interpreter), source_sha256,
            )
        if run.returncode != 0:
            detail = (run.stderr or "").strip()[:500]
            return SourceValidation(
                "validator_error",
                detail or f"target parser exited with code {run.returncode}",
                0, 0, str(payload.get("identity") or self.interpreter),
                source_sha256,
            )
        return SourceValidation(
            str(payload.get("status") or "validator_error"),
            str(payload.get("message") or ""),
            int(payload.get("line") or 0),
            int(payload.get("col") or 0),
            str(payload.get("identity") or self.interpreter),
            source_sha256,
        )

    def _merge_collisions(
        self, coverage_src: str, mutation_src: str,
    ) -> Tuple[str, ...]:
        cov = python_top_level_names(
            self._TS_GRAMMAR, coverage_src,
            func_kinds=self._TS_FUNC_KINDS, class_kinds=self._TS_CLASS_KINDS,
        )
        mut = python_top_level_names(
            self._TS_GRAMMAR, mutation_src,
            func_kinds=self._TS_FUNC_KINDS, class_kinds=self._TS_CLASS_KINDS,
        )
        return tuple(sorted(cov & mut))

    def _duplicate_test_names(self, source: str) -> List[str]:
        """Tests (``_syntax.collected_tests``) and the classes that hold them
        bound twice in one scope, where Python keeps only the last binding:
        two ``test_x`` at module level or in one class collide,
        ``TestA.test_x`` and ``TestB.test_x`` do not. Raises
        ``SourceReadError`` when the source does not parse."""
        result = self._normalize_shadowed_definitions(source)
        if not result.parse_ok:
            raise SourceReadError(parser_rejects(result.parse_error), library_parser())
        tests = _syntax.collected_tests(_syntax.parse(source))
        names = {name for name, _, _ in tests} | {cls.name for _, cls, _ in tests if cls}
        out: List[str] = []
        for item in result.removed_bindings:
            if ".".join((*item.scope, item.name)) in names and item.name not in out:
                out.append(item.name)
        return out

    def _order_dependent_mock_warnings(self, source: str) -> List[str]:
        """The tests that assign a list to a mock's ``side_effect`` and assert
        on a named element of it: where a scheduler dispatches the calls (dask
        keys tasks by per-call uuid4 tokens), which element lands where is
        not the code's property. Advisory only. Raises ``SourceReadError``
        when the source does not parse."""
        import ast as _ast
        try:
            tree = _syntax.parse(source)
        except SyntaxError as error:
            raise SourceReadError(parser_rejects(
                f"{error.msg} (line {error.lineno}, column {error.offset})"), library_parser()) from None
        flagged: List[str] = []

        def scan(func: _ast.AST, qualname: str) -> None:
            element_names: set = set()
            for node in _ast.walk(func):
                if (isinstance(node, _ast.Assign)
                        and any(isinstance(t, _ast.Attribute)
                                and t.attr == "side_effect"
                                for t in node.targets)
                        and isinstance(node.value, (_ast.List, _ast.Tuple))):
                    element_names.update(
                        e.id for e in node.value.elts
                        if isinstance(e, _ast.Name))
            if not element_names:
                return
            for node in _ast.walk(func):
                if isinstance(node, (_ast.Assert, _ast.Compare)):
                    for name in _ast.walk(node):
                        if (isinstance(name, _ast.Name)
                                and name.id in element_names):
                            flagged.append(qualname)
                            return

        for name, _, function in _syntax.collected_tests(tree):
            scan(function, name)
        return flagged

    def _salvage_syntax(self, source: str, project_root: str, *,
                       report: Optional[Dict[str, Any]] = None,
                       ) -> Optional[str]:
        """``source`` repaired until the configured target interpreter, not
        tree-sitter or the calling one, parses it. None when it is beyond
        repair or the validator fails. In at most 25 validation rounds an
        "expected an indented block" error gets a ``pass`` body under the line
        it names and any other error has its line blanked. ``report``, when
        given, receives ``status`` (``valid``, ``repaired``, ``beyond_repair``
        or ``validator_failed``), the ``repairs`` and the last ``message``."""
        from langadapter.python.source_analysis import salvage_syntax

        result = salvage_syntax(
            source, lambda candidate: self._validate_test_source(candidate, project_root))
        if report is not None:
            report.update(status=result.status, repairs=result.repairs,
                          message=result.validation.message)
        return result.source

    def _strip_detached_comments(self, source: str) -> str:
        """``source`` without the full-line comments not attached to the next
        statement and the extra blank lines they leave. Comments before the
        first statement stay. A rewrite that changes a code token is refused."""
        reduced = self._detached_comment_reduction(source)
        if reduced == source:
            return source
        before = non_comment_leaf_fingerprint(
            self._TS_GRAMMAR, source, comment_kinds=self._TS_IGNORE_KINDS,
        )
        after = non_comment_leaf_fingerprint(
            self._TS_GRAMMAR, reduced, comment_kinds=self._TS_IGNORE_KINDS,
        )
        if not before or before != after:
            return source
        if self._detached_comment_reduction(reduced) != reduced:
            return source
        return reduced

    def _detached_comment_reduction(self, source: str) -> str:
        """A conservative comment-only rewrite of ``source``."""
        tree = parse(self._TS_GRAMMAR, source)
        root = tree.root_node
        if root.has_error:
            return source
        lines = source.splitlines(keepends=True)
        if not lines:
            return source

        first_code_row = min(
            (
                child.start_point[0]
                for child in root.named_children
                if child.type != "comment"
            ),
            default=len(lines),
        )
        comments = []
        for node in walk(root):
            if node.type != "comment":
                continue
            row, col = node.start_point
            end_row, end_col = node.end_point
            if row != end_row or row >= len(lines):
                continue
            line = lines[row].rstrip("\r\n")
            if line[:col].strip() or line[end_col:].strip():
                continue
            comments.append((row, col, node))
        comments.sort(key=lambda item: item[0])

        remove_rows: set[int] = set()
        i = 0
        protected = re.compile(r"^#!|^[ \t]*#.*coding[:=]")
        while i < len(comments):
            row, col, _ = comments[i]
            j = i + 1
            last_row = row
            while (
                j < len(comments)
                and comments[j][0] == last_row + 1
                and comments[j][1] == col
            ):
                last_row = comments[j][0]
                j += 1
            next_row = last_row + 1
            next_line = (
                lines[next_row].rstrip("\r\n")
                if next_row < len(lines) else ""
            )
            next_indent = len(next_line) - len(next_line.lstrip(" \t"))
            attached = bool(next_line.strip()) and next_indent == col
            module_header = row < first_code_row
            if not attached and not module_header:
                for k in range(i, j):
                    candidate_row = comments[k][0]
                    text = lines[candidate_row].rstrip("\r\n")
                    if candidate_row < 2 and protected.match(text):
                        continue
                    remove_rows.add(candidate_row)
            i = j

        if not remove_rows:
            return source
        last_content_row = max(
            (row for row in range(len(lines)) if row not in remove_rows),
            default=-1,
        )
        if remove_rows and max(remove_rows) > last_content_row:
            row = min(remove_rows) - 1
            while row >= 0 and not lines[row].strip():
                remove_rows.add(row)
                row -= 1
        for row in sorted(remove_rows):
            left = row - 1
            right = row + 1
            while left >= 0 and not lines[left].strip():
                left -= 1
            while right < len(lines) and not lines[right].strip():
                right += 1
            blanks = [
                k for k in range(left + 1, right)
                if k not in remove_rows and not lines[k].strip()
            ]
            remove_rows.update(blanks[2:])
        return "".join(
            line for row, line in enumerate(lines) if row not in remove_rows
        )

    def _strip_module_level_skips(self, source: str) -> Tuple[str, int]:
        """``(source, count)`` without the module-scope
        ``pytest.importorskip(...)``, bare or assigned, and
        ``pytest.skip(..., allow_module_level=True)``: each collects no tests
        yet reads as green (``importorskip`` also skips on a ``minversion``
        check of a versionless module such as ``dask.array``), and in a merged
        module skips every test. A skip leaves no traceback for
        ``prune_collection_breakers``. One in a test body guards that test."""
        from langadapter.python.source_analysis import strip_module_level_skips
        result = strip_module_level_skips(source)
        return result.source, result.count

    def _probe_env(self, project_root: str) -> Dict[str, str]:
        """``_build_env`` for a probe of the project's code, which writes no
        bytecode (``PYTHONDONTWRITEBYTECODE``): CPython and pytest reuse a cache
        whose recorded size and time match its source, so neither a later round
        of the probe nor the user's directory meets a cache the probe left."""
        env = self._build_env(project_root)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        return env

    def _prune_collection_breakers(
        self, source: str, project_root: str, *, timeout: float,
        report: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, List[str]]:
        """``(source, removed spans)`` without the module-scope statements
        that raise during pytest collection: each of at most five
        ``--collect-only`` rounds blanks the smallest module- or class-level
        statement enclosing the deepest traceback frame in the file, and the
        rewrite is adopted only once collection succeeds. ``report``, when
        given, receives ``status``: ``collects`` (nothing to do), ``pruned``,
        ``timed_out``, ``no_location`` (the traceback names no line of the
        file), ``not_blankable``, ``exhausted`` or ``execution_failed`` (the
        process gave no collection verdict)."""
        from langadapter.outcomes import Outcome
        from langadapter.python._output import collection_outcome
        from langadapter import templates

        report = report if report is not None else {}
        env = self._probe_env(project_root)
        with self._work_dir_tempdir(
            prefix=f"{TEMPDIR_PREFIX}collectfix_") as td:
            cand = Path(td) / "candidate.py"

            def load(current: str) -> templates.Load:
                cand.write_text(current, encoding="utf-8")
                suite_root = _suite_rootdir(str(cand), container=self._container())
                # pytest's own traceback, which names the failing line.
                cmd = pytest_command(
                    self.interpreter, self._runtime_flags(), [str(cand), "--collect-only"],
                    pytest_arguments(self._suite_config(project_root, suite_root),
                                     suite_root, traceback="auto",
                                     disabled_plugins=DISABLED_PLUGINS))
                try:
                    r = self._executor().run(
                        cmd, cwd=project_root, env=env, timeout=timeout,
                    )
                except subprocess.TimeoutExpired:
                    return templates.Load(False, stop="timed_out")
                if r.returncode == 0:
                    return templates.Load(True)
                out = (r.stdout or "") + (r.stderr or "")
                report["message"] = out.strip()[-500:]
                outcome, reason = collection_outcome(r.returncode, out)
                if outcome is Outcome.INFRASTRUCTURE_FAILURE:
                    return templates.Load(False, stop="execution_failed",
                                          info={"reason": reason, "returncode": r.returncode})
                refs = re.findall(
                    r"candidate\.py(?:\"|, line |:)\s*(\d+)", out,
                )
                # The deepest frame in the file: the last line it names.
                return templates.Load(False, (int(refs[-1]),) if refs else ())

            def remove(current: str, lines: Sequence[int]):
                blanked = _blank_collection_breaker(current, lines[0])
                if blanked is None:
                    return None
                text, (s, e) = blanked
                return text, [f"line {s}-{e}"]

            result = templates.repair_loading(source, load, remove, loads=5)
            if result.status == templates.STOPPED:
                if result.stop == "timed_out":
                    report["status"] = "timed_out"
                    _retain_scratch(td, "collection recovery timed out")
                else:
                    report.update(status="execution_failed", **result.info)
                    _retain_scratch(td, result.info["reason"]
                                    or "collection recovery produced no verdict")
                return source, []
            if result.status == templates.LOADS:
                report["status"] = "pruned" if result.removed else "collects"
                return (result.source, list(result.removed)) if result.removed else (source, [])
            # A last round whose line lies in no statement reports
            # not_blankable, as an earlier round of that kind does.
            report["status"] = {
                templates.NO_LOCATION: "no_location",
                templates.NOT_REMOVABLE: "not_blankable",
                templates.EXHAUSTED: "not_blankable" if result.unmappable else "exhausted",
            }[result.status]
            return source, []

    def _prune_unresolvable_imports(
        self, source: str, project_root: str, *,
        report: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, List[str]]:
        """``(source, dropped names)`` without the top-level imports that fail
        under the project's import root: a plain import of a module that does
        not resolve, each from-imported name its module does not provide (all
        of them when the module fails to import). One probe with the test
        runs' environment finds them all. ``report``, when given, receives
        ``status``: ``parse_error``, ``nothing_to_check``, ``probe_failed``
        (with the ``message``) or ``checked``."""
        from langadapter import templates

        report = report if report is not None else {}
        probed: Dict[str, Any] = {}

        def failing(text: str) -> templates.Failing:
            root = parse("tree_sitter_python", text).root_node
            if root.has_error:
                return templates.Failing(stop="parse_error")
            infos = _top_level_import_infos(root)
            modules = sorted({m for inf in infos if not inf.is_from
                              for m in inf.plain_modules})
            from_specs = sorted({(inf.module, o) for inf in infos
                                 if inf.is_from and not inf.has_star
                                 for (o, _asn) in inf.from_names})
            if not modules and not from_specs:
                return templates.Failing(info={"status": "nothing_to_check"})
            message = self._probe_imports(project_root, modules, from_specs, probed)
            if message is not None:
                return templates.Failing(stop="probe_failed", info={"message": message})
            # The probe ran: the report says so first, and a result that is
            # not an object of the two maps then raises here.
            report["status"] = "checked"
            result = probed["result"]
            probed["modules"], probed["names"] = result["modules"], result["names"]
            return templates.Failing(
                _failing_imports(infos, probed["modules"], probed["names"]),
                info={"status": "checked"})

        def remove(text: str, infos: Sequence[Any]) -> Tuple[str, List[str]]:
            return _rewrite_unresolvable_imports(text, infos, probed["modules"], probed["names"])

        pruning = templates.prune_units(source, failing, remove, rounds=1)
        if pruning.status == templates.STOPPED:
            report.update(status=pruning.stop, **pruning.info)
            return source, []
        report["status"] = pruning.info["status"]
        return pruning.source, list(pruning.removed)

    def _probe_imports(self, project_root: str, modules: Sequence[str],
                       from_specs: Sequence[Tuple[str, str]],
                       found: Dict[str, Any]) -> Optional[str]:
        """Resolve ``modules`` and the from-import names ``from_specs`` in one
        subprocess with the project's environment into ``found["result"]``,
        the probe's JSON. The message when it could not run or printed no
        JSON, else None."""
        env = self._probe_env(project_root)
        probe = (
            "import importlib, importlib.util, json, sys\n"
            "spec = json.loads(sys.argv[1])\n"
            "mods = {}\n"
            "for m in spec['modules']:\n"
            "    try:\n"
            "        mods[m] = importlib.util.find_spec(m) is not None\n"
            "    except Exception:\n"
            "        mods[m] = False\n"
            "names = {}\n"
            "loaded = {}\n"
            "for m, n in spec['names']:\n"
            "    key = m + ':' + n\n"
            "    if m not in loaded:\n"
            "        try:\n"
            "            loaded[m] = importlib.import_module(m)\n"
            "        except Exception:\n"
            "            loaded[m] = None\n"
            "    mod = loaded[m]\n"
            "    if mod is None:\n"
            "        names[key] = False\n"
            "    else:\n"
            "        ok = hasattr(mod, n)\n"
            "        if not ok:\n"
            "            try:\n"
            "                ok = importlib.util.find_spec(\n"
            "                    m + '.' + n) is not None\n"
            "            except Exception:\n"
            "                ok = False\n"
            "        names[key] = ok\n"
            "print(json.dumps({'modules': mods, 'names': names}))\n"
        )
        payload = json.dumps(
            {"modules": modules, "names": [list(t) for t in from_specs]},
        )
        try:
            r = self._executor().run(
                [self.interpreter, *self._runtime_flags(),
                 "-c", probe, payload],
                cwd=project_root, env=env, timeout=120,
            )
            result = json.loads(r.stdout.strip().splitlines()[-1])
        except Exception as exc:
            return str(exc)
        found["result"] = result
        return None

    def _tree_check(self, source: str) -> StructureResult:
        return check_structure(self._TS_GRAMMAR, source)

    def _same_skeleton(
        self, original: str, candidate: str,
    ) -> Tuple[bool, str]:
        return diff_skeletons(
            self.skeleton(original), self.skeleton(candidate),
        )


def ruff_smell_count(*paths: Path) -> Optional[int]:
    """The number of ruff findings in ``paths``, by ruff in the calling
    process's environment, or None when ruff is missing or fails. Running ruff
    on the same files gives the findings again."""
    if not paths:
        return 0
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check",
             "--output-format=json", "--no-cache", *[str(p) for p in paths]],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    # Exit 1 is findings with a JSON report, or a missing ruff module with no
    # output. Exit 2 is a ruff error.
    if result.returncode not in (0, 1) or (result.returncode == 1 and not result.stdout.strip()):
        return None
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        return None
    return len(data) if isinstance(data, list) else None


def package_binds(package_init: Path, package: str, name: str) -> Optional[bool]:
    """Whether the package's ``__init__.py`` binds ``name`` at module level,
    read without running it: by a def, class, assignment, import (not of the
    submodule itself) or ``global``. None when undecidable (a star import, a
    module ``__getattr__``, an unreadable file). False without the file."""
    import ast

    if not package_init.is_file():
        return False
    try:
        tree = ast.parse(package_init.read_bytes())
    except (OSError, SyntaxError, ValueError):
        return None
    undecided = False

    def visit(nodes) -> bool:
        nonlocal undecided
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == name:
                    return True
                if node.name == "__getattr__" and not isinstance(node, ast.ClassDef):
                    undecided = True
                if any(isinstance(inner, ast.Global) and name in inner.names
                       for inner in ast.walk(node)):
                    return True
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if (alias.asname or alias.name.partition(".")[0]) == name:
                        return True
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name == "*":
                        undecided = True
                    elif (alias.asname or alias.name) == name:
                        # ``from . import name`` binds the submodule itself.
                        itself = alias.asname in (None, name) and alias.name == name and (
                            (node.level == 1 and not node.module)
                            or (node.level == 0 and node.module == package))
                        if not itself:
                            return True
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(target)):
                        return True
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
                bound = [node.target] if isinstance(node, (ast.For, ast.AsyncFor)) else [
                    item.optional_vars for item in node.items if item.optional_vars is not None]
                for target in bound:
                    if any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(target)):
                        return True
            for field_name in ("body", "orelse", "finalbody", "handlers"):
                inner = getattr(node, field_name, None)
                if isinstance(inner, list) and visit(inner):
                    return True
            if isinstance(node, ast.ExceptHandler) and node.name == name:
                return True
        return False

    if visit(tree.body):
        return True
    return None if undecided else False


def _shadows(name: str) -> bool:
    """Whether binding ``name`` in a test module hides a standard-library
    module or a built-in the tests may use."""
    import builtins

    return name in sys.stdlib_module_names or hasattr(builtins, name)


def import_line(target_file_path: str, project_root: str) -> str:
    """A best-effort import line for the file under test, by its dotted module
    path from the import root (caller-facing text: changed only on purpose).
    ``import a.b.c as c`` takes ``c`` from ``a.b`` when the package binds that
    name itself (tenacity's ``retry``), so then, or when that is undecidable,
    the module is bound by ``importlib.import_module`` under the same name, on
    two lines. A package's ``__init__.py`` is imported as the package, so it
    never loads twice. A module named like a standard-library module or built-in
    is bound with its package (``import dacite.types as dacite_types``), so the
    tests' own ``import types`` keeps the standard library's."""
    try:
        rel = Path(target_file_path).resolve().relative_to(
            Path(project_root).resolve(),
        )
        parts = rel.with_suffix("").parts
        if len(parts) > 1 and parts[-1] == "__init__":
            parts = parts[:-1]
        dotted = ".".join(parts)
        package, _, stem = dotted.rpartition(".")
        if "." in dotted:
            name = (f"{package.replace('.', '_')}_{stem}" if _shadows(stem) else stem)
            init = Path(project_root).resolve().joinpath(*parts[:-1], "__init__.py")
            if package_binds(init, package, stem) is not False:
                return f'import importlib\n{name} = importlib.import_module("{dotted}")'
            return f"import {dotted} as {name}"
        return f"import {dotted}"
    except ValueError:
        return f"import {Path(target_file_path).stem}"


#: The module attributes that hold its file or name.
_MODULE_NAMES = frozenset({"__file__", "__name__", "__module__", "__spec__"})


def self_references(source: str, stem: str, suite: Sequence[str] = ()) -> Tuple[str, ...]:
    """Where ``source`` refers to its own module in a way the staged name
    changes at run time: its name ``stem`` or another suite module's name
    (``suite``) in a string, or its file or name read at run time, except the
    ``if __name__ == "__main__"`` guard, which pytest does not run. Empty when
    the source does not tokenize."""
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, SyntaxError):
        return ()
    texts = {tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING)}
    words = [(name, re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)"))
             for name in dict.fromkeys((stem, *suite))]
    found = set()
    for index, token in enumerate(tokens):
        if token.type in texts:
            for name, word in words:
                if word.search(token.string):
                    found.add(f"names its module {stem} in a string" if name == stem
                              else f"names its suite's module {name} in a string")
        elif token.type == tokenize.NAME and token.string in _MODULE_NAMES:
            after = [t.string for t in tokens[index + 1:index + 3]]
            if token.string == "__name__" and after[:1] == ["=="] and after[1:2] in (
                    ['"__main__"'], ["'__main__'"]):
                continue
            found.add(f"reads its module's {token.string}")
    return tuple(sorted(found))


def _imported_modules(tree: ast.AST, name: str) -> Iterator[Tuple[str, ...]]:
    """The module paths, as parts, an import in the file ``name`` can load: an
    absolute import as written, with each name a ``from`` import takes, a
    relative one resolved from the file's directory."""
    file_dir = PurePosixPath(name).parent.parts
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield tuple(alias.name.split("."))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level - 1 > len(file_dir):
                    continue
                base = file_dir[:len(file_dir) - (node.level - 1)]
            else:
                base = ()
            module = base + tuple(node.module.split(".")) if node.module else base
            if node.module:
                yield module
            for alias in node.names:
                if alias.name != "*":
                    yield module + (alias.name,)


def _unreadable_imports(member: PoolMember) -> Optional[str]:
    """Why the imports of ``member``, needed to check that it imports no file
    of its own suite, cannot be read, or None."""
    try:
        _syntax.parse(member.source)
    except SyntaxError as error:
        message = f"{error.msg} (line {error.lineno}, column {error.offset})"
    else:
        return None
    return (f"the imports of {member.name} of suite {member.slot} cannot be read, and the pool "
            f"checks that it imports no file of its own suite: {parser_rejects(message)}")


def _own_suite_import(member: PoolMember, suite: Sequence[PoolMember]) -> Optional[str]:
    """Why ``member`` (which parses) cannot be pooled because it imports a file
    of its own suite, or None. A file is importable by its stem from its directory
    (pytest puts it on the path) and by its path from an enclosing one, an
    ``__init__.py`` by its directory's name."""
    files = {}
    for other in suite:
        parts = PurePosixPath(other.name).with_suffix("").parts
        if parts[-1] == "__init__":
            parts = parts[:-1]
        for start in range(len(parts)):
            files.setdefault(parts[start:], other)
    for module in _imported_modules(_syntax.parse(member.source), member.name):
        other = files.get(module)
        if other is not None:
            return (f"{member.name} of suite {member.slot} imports {'.'.join(module)}, "
                    f"its suite's {other.name}, which the pool renames to "
                    f"{staged_name(other.name, other.slot)}")
    return None


def staged_name(name: str, slot: str) -> str:
    """The pooled name of the file ``name`` of suite ``slot``."""
    path = PurePosixPath(name)
    stem = path.stem
    staged = f"{stem}_{slot}.py" if stem.startswith("test_") else f"test_{stem}_{slot}.py"
    return (path.parent / staged).as_posix()


def stage_pool(members: Sequence[PoolMember],
               setup: Sequence[Tuple[str, str]] = (),
               ) -> Union[StagedPool, SourceObservation]:
    """The pool of ``members`` with the shared ``setup`` files, or an
    observation that says why it cannot be staged: a parse error for a
    member whose imports cannot be read, else unsupported."""
    members = check_members(members)
    setup = check_setup(setup)
    files = []
    for member in members:
        if PurePosixPath(member.name).suffix != ".py":
            return SourceObservation(SourceStatus.UNSUPPORTED,
                                     reason=f"{member.name} is not a Python file")
    for member in members:
        unreadable = _unreadable_imports(member)
        if unreadable is not None:
            return SourceObservation(SourceStatus.PARSE_ERROR, parser=library_parser(), reason=unreadable)
        suite = [m for m in members if m.slot == member.slot]
        issue = _own_suite_import(member, suite)
        if issue is not None:
            return SourceObservation(SourceStatus.UNSUPPORTED, reason=issue)
        stem = PurePosixPath(member.name).stem
        files.append(StagedFile(member.slot, PurePosixPath(member.name).as_posix(),
                                staged_name(member.name, member.slot), member.source,
                                self_references=self_references(
                                    member.source, stem,
                                    [PurePosixPath(m.name).stem for m in suite])))
    pool = StagedPool(tuple(files), setup)
    issue = unique_paths(pool, by_name=True)
    if issue is not None:
        return SourceObservation(SourceStatus.UNSUPPORTED, reason=issue)
    return pool




def file_to_module_name(file_path: str, import_root: str, *,
                        container: bool = False) -> Optional[str]:
    """The dotted module name of the ``.py`` file ``file_path`` below
    ``import_root``, ``__init__`` naming its package (``<root>/pkg/mod.py``
    gives ``pkg.mod``). Host paths are compared resolved, a container's
    (``container``) as normalized POSIX paths. None when the file is not ``.py``,
    not below the root, the root's own ``__init__.py``, or a part of its path
    is no identifier (dashes, leading digits). A directory named like the root
    is a package like any other.
    """
    if Path(str(file_path)).suffix.lower() != ".py":
        return None
    if container:
        path = PurePosixPath(posixpath.normpath(str(file_path)))
        top = PurePosixPath(posixpath.normpath(str(import_root)))
    else:
        path, top = Path(file_path).resolve(), Path(import_root).resolve()
    try:
        relative = path.relative_to(top)
    except ValueError:
        return None
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(part.isidentifier() for part in parts):
        return None
    return ".".join(parts)


class _ImportInfo(NamedTuple):
    """One top-level import: ``plain_modules`` of an ``import a, b.c``, the
    ``(name, asname)`` pairs ``from_names`` of a ``from m import x as y``
    (``asname`` None without an alias)."""

    span: Tuple[int, int]                       # 1-based inclusive line range
    is_from: bool
    module: str                                 # "" for a plain import
    plain_modules: Tuple[str, ...]
    from_names: Tuple[Tuple[str, Optional[str]], ...]
    has_star: bool


def _top_level_import_infos(root) -> List["_ImportInfo"]:
    """A parsed module's absolute top-level imports: the probe resolves
    absolute names, so a relative ``from . import x`` is skipped."""
    infos: List[_ImportInfo] = []
    for child in root.named_children:
        if child.type == "import_statement":
            mods = tuple(ib.origin[1] for ib in _py_parse_import(child))
            infos.append(_ImportInfo(_node_line_span(child), False, "",
                                     mods, (), False))
        elif child.type in ("import_from_statement", "future_import_statement"):
            if child.type == "import_from_statement":
                mn = child.child_by_field_name("module_name")
                if mn is None or mn.type != "dotted_name":
                    continue                     # relative import: level != 0
                module = mn.text.decode("utf-8", "replace")
            else:
                module = "__future__"
            has_star = any(c.type == "wildcard_import"
                           for c in child.named_children)
            names = tuple(
                (ib.origin[1], ib.name if ib.name != ib.origin[1] else None)
                for ib in _py_parse_import(child)
            )
            infos.append(_ImportInfo(_node_line_span(child), True, module,
                                     (), names, has_star))
    return infos


def _failing_imports(infos, mod_ok, name_ok) -> List["_ImportInfo"]:
    """The imports the probe's ``mod_ok`` and ``name_ok`` fail: a plain
    import of an unresolvable module, a non-wildcard from-import of a name
    its module lacks."""
    return [inf for inf in infos
            if (not inf.is_from and any(not mod_ok.get(m, False) for m in inf.plain_modules))
            or (inf.is_from and not inf.has_star
                and any(not name_ok.get(f"{inf.module}:{o}", False)
                        for (o, _asn) in inf.from_names))]


def _rewrite_unresolvable_imports(source, infos, mod_ok, name_ok):
    """``(source, sorted dropped names)`` without the imports the probe could
    not resolve: a plain import goes whole when any of its modules fails, a
    from-import keeps its surviving names, a wildcard import stays.
    ``(source, [])`` when nothing is dropped."""
    dropped: List[str] = []
    dead_lines: set = set()
    rewrites: Dict[int, str] = {}
    for inf in infos:
        if not inf.is_from:
            bad = [m for m in inf.plain_modules if not mod_ok.get(m, False)]
            if bad:
                a, b = inf.span
                dead_lines.update(range(a, b + 1))
                dropped.extend(bad)
            continue
        if inf.has_star:
            continue
        dead = [(o, asn) for (o, asn) in inf.from_names
                if not name_ok.get(f"{inf.module}:{o}", False)]
        if not dead:
            continue
        dropped.extend(f"{inf.module}.{o}" for (o, _asn) in dead)
        a, b = inf.span
        dead_lines.update(range(a, b + 1))
        alive = [(o, asn) for (o, asn) in inf.from_names if (o, asn) not in dead]
        if alive:
            parts = ", ".join(o if asn is None else f"{o} as {asn}"
                              for (o, asn) in alive)
            rewrites[a] = f"from {inf.module} import {parts}\n"
    if not dropped:
        return source, []
    lines = source.splitlines(keepends=True)
    out: List[str] = []
    for i, line in enumerate(lines, 1):
        if i in rewrites:
            out.append(rewrites[i])
        elif i not in dead_lines:
            out.append(line)
    return "".join(out), sorted(set(dropped))


def _find_collection_stmt(stmt_nodes, target_line):
    """The smallest module- or class-level statement spanning
    ``target_line``: a class body runs at collection, a function body not."""
    for node in stmt_nodes:
        if node.type == "comment":
            # tree-sitter carries comments as nodes. The AST does not, so a
            # comment-only line never encloses a statement to blank.
            continue
        s, e = _node_line_span(node)
        if not (s <= target_line <= e):
            continue
        real = node
        if node.type == "decorated_definition":
            inner = node.child_by_field_name("definition")
            if inner is not None:
                real = inner
        if real.type == "class_definition" \
                and target_line > real.start_point[0] + 1:
            body = real.child_by_field_name("body")
            if body is not None:
                deeper = _find_collection_stmt(body.named_children, target_line)
                if deeper is not None:
                    return deeper
        return node
    return None


def _blank_collection_breaker(source, target_line):
    """``(source, (start, end))`` with the smallest statement enclosing
    ``target_line`` replaced by ``pass`` at its indentation and the rest of
    its span blank, so the block stays valid. None when the source does not
    parse or nothing encloses the line."""
    root = parse("tree_sitter_python", source).root_node
    if root.has_error:
        return None
    node = _find_collection_stmt(root.named_children, target_line)
    if node is None:
        return None
    s, e = _node_line_span(node)
    lines = source.splitlines(keepends=True)
    if s - 1 >= len(lines):
        return None
    lines[s - 1] = " " * node.start_point[1] + "pass\n"
    for i in range(s, min(e, len(lines))):
        lines[i] = "\n"
    return "".join(lines), (s, e)


def _suite_rootdir(test_file: str, *, container: bool = False) -> str:
    """The directory of ``test_file``, pinned as pytest's rootdir and anchored
    as its configuration. The pin keeps JUnit classnames module-relative (as
    the per-test eviction ids join them) and pytest's rootdir walk out of
    shared temp space, where other processes delete directories. An empty
    ``pytest.ini``, written when absent, stops configuration discovery, so
    an enclosing project's ``filterwarnings = error`` cannot fail collection.
    The project's own configuration applies through ``isolation_args``."""
    # A container path's parent is taken as POSIX, so a Windows host does not
    # turn it into a drive path pytest cannot find.
    root = (_parent_dir(test_file) if container
            else str(Path(test_file).resolve().parent))
    # In container mode the suite root is the bind-mounted work directory. A host
    # that cannot write the path (a translated container path) skips it.
    anchor = Path(root) / "pytest.ini"
    if not anchor.exists():
        try:
            anchor.write_text("[pytest]\n", encoding="utf-8")
        except OSError:
            pass
    return root


def _parent_dir(p) -> str:
    """The parent directory of a path, kept POSIX for a container path on a
    Windows host, where ``Path`` would turn its ``/`` into ``\\``."""
    s = str(p)
    return str(PurePosixPath(s).parent) if s.startswith("/") else str(Path(s).parent)


# Removing the named cases of a parametrized test whose parameter list is
# literal and whose case ids can be read as pytest forms them. Any other test
# is reported for removal whole, with the reason.

#: The types whose values pytest writes as themselves in a case's id.
_SCALARS = (int, float, bool, complex, type(None))


def _is_parametrize(node: ast.AST) -> bool:
    """Whether ``node`` is ``pytest.mark.parametrize`` or ``mark.parametrize``."""
    if not (isinstance(node, ast.Attribute) and node.attr == "parametrize"):
        return False
    base = node.value
    return ((isinstance(base, ast.Attribute) and base.attr == "mark")
            or (isinstance(base, ast.Name) and base.id == "mark"))


def _is_param(node: ast.AST) -> bool:
    """Whether ``node`` is a ``pytest.param(...)`` or ``param(...)`` case."""
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Attribute) and node.func.attr == "param")
        or (isinstance(node.func, ast.Name) and node.func.id == "param"))


def _argument(call: ast.Call, index: int, name: str) -> Optional[ast.AST]:
    if len(call.args) > index:
        return call.args[index]
    return next((k.value for k in call.keywords if k.arg == name), None)


def _literal(node: ast.AST):
    """``node``'s value, or the marker ``_UNREAD`` when it is no literal."""
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return _UNREAD


_UNREAD = object()


def _value_id(value, argname: str, index: int) -> Optional[str]:
    """The part of a case's id pytest forms from one value: printable ASCII
    text without backslashes as written, a number, bool or None by ``str``,
    any other value as its argument's name and the case's index. None when
    the value is not literal, is empty text or pytest would escape it."""
    if value is _UNREAD:
        return None
    if isinstance(value, (str, bytes)):
        text = value if isinstance(value, str) else value.decode("latin-1")
        if not text or not text.isascii() or not text.isprintable() or "\\" in text:
            return None
        return text
    if isinstance(value, _SCALARS):
        return str(value)
    return f"{argname}{index}"


def _case_ids(call: ast.Call) -> Tuple[Optional[List[str]], List[bool], str]:
    """The case ids of one ``parametrize`` in order and whether each id
    holds its case's index (``<argname><index>``), or None with the reason
    they cannot be read."""
    names_node = _argument(call, 0, "argnames")
    values_node = _argument(call, 1, "argvalues")
    if names_node is None or not isinstance(values_node, (ast.List, ast.Tuple)):
        return None, [], "its parameter list is not literal"
    names = _literal(names_node)
    if isinstance(names, str):
        argnames = [n.strip() for n in names.split(",") if n.strip()]
    elif isinstance(names, (list, tuple)) and all(isinstance(n, str) for n in names):
        argnames = list(names)
    else:
        return None, [], "its parameter names are not literal"
    given: Optional[List] = None
    ids_node = next((k.value for k in call.keywords if k.arg == "ids"), None)
    if ids_node is not None:
        given = _literal(ids_node)
        if given is _UNREAD or not isinstance(given, (list, tuple)) or not all(
                i is None or isinstance(i, str) for i in given):
            return None, [], "its ids are not a literal list"
        if len(given) != len(values_node.elts):
            return None, [], "its ids do not match its cases"
    ids: List[str] = []
    positional: List[bool] = []
    for index, case in enumerate(values_node.elts):
        # pytest's order: the case's own id, the ids list, its values.
        id_node = (next((k.value for k in case.keywords if k.arg == "id"), None)
                   if _is_param(case) else None)
        if id_node is not None:
            explicit = _literal(id_node)
            if not isinstance(explicit, str):
                return None, [], "a case's id is not literal"
            ids.append(explicit)
            positional.append(False)
            continue
        if given is not None and given[index] is not None:
            ids.append(given[index])
            positional.append(False)
            continue
        values: Sequence[ast.AST]
        if _is_param(case):
            values = case.args
        elif len(argnames) == 1:
            values = [case]
        elif isinstance(case, (ast.Tuple, ast.List)):
            values = case.elts
        else:
            return None, [], "a case is not a literal tuple"
        if len(values) != len(argnames):
            return None, [], "a case does not give every parameter"
        literals = [_literal(v) for v in values]
        parts = [_value_id(value, name, index) for value, name in zip(literals, argnames)]
        if any(p is None for p in parts):
            return None, [], "a case's id cannot be read from its values"
        ids.append("-".join(parts))
        positional.append(any(not isinstance(value, (str, bytes) + _SCALARS)
                              for value in literals))
    if len(set(ids)) != len(ids):
        return None, [], "two of its cases have the same id"
    return ids, positional, ""


def _functions(tree: ast.Module) -> Dict[str, ast.AST]:
    """The module's functions and methods by qualified name."""
    found: Dict[str, ast.AST] = {}

    def visit(body, prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found[prefix + node.name] = node
            elif isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".")

    visit(tree.body, "")
    return found


def prune_cases(source: str, cases: Dict[str, Set[str]]
                ) -> Tuple[str, Tuple[str, ...], Dict[str, str]]:
    """``source`` without the named cases of its parametrized tests, the
    cases removed (``name[id]``) and, for each test to remove whole instead,
    the reason. ``cases`` maps a test's qualified name, or a dotted id ending
    in it, to the ids of the cases to remove."""
    tree = _syntax.parse(source)
    functions = _functions(tree)
    encoded = source.encode("utf-8")
    starts = [0]
    for line in encoded.splitlines(keepends=True):
        starts.append(starts[-1] + len(line))

    def offset(lineno: int, col: int) -> int:
        return starts[lineno - 1] + col

    edits: List[Tuple[int, int, str]] = []
    removed: List[str] = []
    whole: Dict[str, str] = {}
    for name, failing in cases.items():
        qualname = max((q for q in functions if name == q or name.endswith("." + q)),
                       key=len, default=None)
        if qualname is None:
            whole[name] = "no such test"
            continue
        calls = [d for d in functions[qualname].decorator_list
                 if isinstance(d, ast.Call) and _is_parametrize(d.func)]
        if len(calls) != 1:
            whole[name] = ("its cases combine several parameter lists" if calls
                           else "it is not parametrized")
            continue
        call = calls[0]
        ids, positional, reason = _case_ids(call)
        if ids is None:
            whole[name] = reason
            continue
        if not set(failing) <= set(ids):
            whole[name] = "a named case is none of its cases"
            continue
        if set(failing) == set(ids):
            whole[name] = "every case is named"
            continue
        keep = [i for i, case_id in enumerate(ids) if case_id not in failing]
        first = min(i for i, case_id in enumerate(ids) if case_id in failing)
        if any(positional[i] for i in keep if i > first):
            # pytest names such a case by its index, which would move.
            whole[name] = "removing a case would change the ids of the cases after it"
            continue
        lists = [_argument(call, 1, "argvalues")]
        ids_node = next((k.value for k in call.keywords if k.arg == "ids"), None)
        if ids_node is not None:
            lists.append(ids_node)
        for node in lists:
            parts = [encoded[offset(e.lineno, e.col_offset):
                             offset(e.end_lineno, e.end_col_offset)].decode("utf-8")
                     for e in (node.elts[i] for i in keep)]
            if isinstance(node, ast.Tuple):
                text = "(" + ", ".join(parts) + ("," if len(parts) == 1 else "") + ")"
            else:
                text = "[" + ", ".join(parts) + "]"
            edits.append((offset(node.lineno, node.col_offset),
                          offset(node.end_lineno, node.end_col_offset), text))
        removed.extend(f"{qualname}[{case_id}]" for case_id in ids if case_id in failing)
    for start, end, text in sorted(edits, reverse=True):
        encoded = encoded[:start] + text.encode("utf-8") + encoded[end:]
    return encoded.decode("utf-8"), tuple(removed), whole
