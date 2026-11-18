"""Source operations of the Java adapter on test sources: the structural
check, test metrics, static analysis, validation, pruning tests, merging
two test classes, normalizing duplicate declarations and finalizing a
source, each reporting whether it applied (``langadapter.sources``).

Structural operations use tree-sitter-java in the calling process. The
project's JDK decides what parses (``validate_source``: javac's parser,
syntax only, at the project's release). Merging, collision resolution,
normalization and the generated-suite rewrites refuse a source it rejects.
Names and types are checked only when a class compiles. A validator that
cannot run is an infrastructure failure, never a syntax verdict.
"""
from __future__ import annotations

from langadapter.adapter import LangAdapter

from langadapter._execution_scope import reraise_unresolved
import hashlib
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from langadapter import (
    _lizard, _treesitter, templates, sources as _lines, treesitter,
)
from langadapter.sources import (
    apply_edits, APPLIED, INFRASTRUCTURE_FAILURE, NOT_REPAIRED, PARSE_ERROR, REFUSED,
    UNCHANGED, PolicyReport, SourceResult, SourceValidation, StructureCheck,
    TestMetrics,
)
from langadapter.errors import InvalidRequest
from langadapter.execution import ContainerExecutor, Executor, _retain_scratch
from langadapter.java import _tools
from langadapter.java._java_source import GRAMMAR, TEST_ANNOTATIONS, _annotations, _text
from langadapter.java._build import compile_tests, tool_environment
from langadapter.java._env import JavaEnv
from langadapter.outcomes import Outcome
from langadapter.suites import TestId

COMMENT_KINDS = frozenset({"line_comment", "block_comment"})
FUNC_KINDS = frozenset({"method_declaration", "constructor_declaration"})
CLASS_KINDS = frozenset({
    "class_declaration", "interface_declaration", "enum_declaration",
    "record_declaration", "annotation_type_declaration",
})
CALL_KINDS = frozenset({"method_invocation"})
#: The annotations that skip a whole class when they sit on it: @Disabled,
#: JUnit 4's @Ignore, and JUnit Jupiter's conditions (``org.junit.jupiter.api
#: .condition``), which skip it on a system, JRE, property or variable.
CLASS_SKIPS = frozenset({
    "Disabled", "Ignore",
    "EnabledOnOs", "DisabledOnOs", "EnabledOnJre", "DisabledOnJre",
    "EnabledForJreRange", "DisabledForJreRange", "EnabledIf", "DisabledIf",
    "EnabledIfSystemProperty", "DisabledIfSystemProperty",
    "EnabledIfSystemProperties", "DisabledIfSystemProperties",
    "EnabledIfEnvironmentVariable", "DisabledIfEnvironmentVariable",
    "EnabledIfEnvironmentVariables", "DisabledIfEnvironmentVariables",
    "EnabledInNativeImage", "DisabledInNativeImage",
})
#: The setup methods whose failed assumption skips every test of the class.
SETUP_ANNOTATIONS = frozenset({"BeforeAll", "BeforeEach", "BeforeClass", "Before"})
#: The assumption calls of JUnit Jupiter's Assumptions and JUnit 4's Assume
#: that skip a test. assumingThat only runs its block when its condition holds.
ASSUMPTIONS = frozenset({
    "assumeTrue", "assumeFalse", "assumeThat", "assumeNotNull",
    "assumeNoException", "abort",
})
#: The classes an assumption call may name: abort on another object is its own.
ASSUMPTION_CLASSES = frozenset({
    "Assumptions", "Assume", "org.junit.jupiter.api.Assumptions", "org.junit.Assume",
})
LOOP_KINDS = frozenset({
    "for_statement", "enhanced_for_statement", "while_statement",
    "do_statement",
})
#: Stream, socket and JDBC methods specific enough to read as IO.
IO_CALL_NAMES = frozenset({
    "readLine", "openConnection", "openStream", "getInputStream",
    "getOutputStream", "accept", "executeQuery", "executeUpdate",
})
#: The annotations that mark a JUnit 4 to 6 fixture.
LIFECYCLE_ANNOTATIONS = frozenset({
    "BeforeEach", "AfterEach", "BeforeAll", "AfterAll", "Before", "After",
    "BeforeClass", "AfterClass",
})
PARAMETER_SOURCES = frozenset({"ValueSource", "CsvSource"})
#: The attributes of those sources that hold the cases: ``@CsvSource``'s
#: ``value`` and ``@ValueSource``'s typed arrays (``textBlock`` counts rows).
CASE_ATTRIBUTES = frozenset({"value", "shorts", "bytes", "ints", "longs", "floats",
                             "doubles", "chars", "booleans", "strings", "classes"})
#: JUnit, AssertJ and Hamcrest assertion calls.
ASSERTION_CALLS = frozenset({
    "assertEquals", "assertNotEquals", "assertTrue", "assertFalse",
    "assertNull", "assertNotNull", "assertSame", "assertNotSame",
    "assertArrayEquals", "assertIterableEquals", "assertLinesMatch",
    "assertThrows", "assertThrowsExactly", "assertDoesNotThrow",
    "assertTimeout", "assertTimeoutPreemptively", "assertInstanceOf",
    "assertThat", "assertAll", "fail",
})
#: Test file names by JUnit's conventions and Maven's and Gradle's filters.
TEST_FILE_SUFFIXES = ("Test.java", "Tests.java", "TestCase.java", "IT.java")
#: javac's raw diagnostics, ``File.java:LINE:COLUMN: compiler.err.KEY:
#: ARGUMENTS``, carry the kind in the key, in every locale.
RAW_DIAGNOSTICS = "-XDrawDiagnostics"
#: The removals of ``prune_collection_breakers``, so at most six compilations.
BREAKER_ROUNDS = 5


def parser_identity() -> str:
    """The structural parser and its grammar, by their distributions."""
    return _treesitter.parser_identity("tree-sitter-java")


def _parameter_cases(annotation: Any) -> int:
    """The literal cases of a ``@ValueSource`` or ``@CsvSource``: the
    elements of its ``value`` or typed array (``ints``, ``strings`` and the
    others), or the rows of a ``textBlock`` but its blank and ``#`` comment
    lines. A dynamic source (``@MethodSource``, ``@EnumSource``) counts
    none."""
    name = annotation.child_by_field_name("name")
    if name is None or _text(name).rsplit(".", 1)[-1] not in PARAMETER_SOURCES:
        return 0
    arguments = next((c for c in annotation.named_children
                      if c.type == "annotation_argument_list"), None)
    if arguments is None:
        return 0
    for child in arguments.named_children:
        if child.type in COMMENT_KINDS:
            continue
        if child.type != "element_value_pair":
            return _elements(child)
        key = _text(child.child_by_field_name("key"))
        value = child.child_by_field_name("value")
        if key == "textBlock":
            text = _text(value)
            body = text[3:-3] if text.startswith('"""') else text[1:-1].replace("\\n", "\n")
            return sum(1 for line in body.splitlines()
                       if line.strip() and not line.strip().startswith("#"))
        if key in CASE_ATTRIBUTES:
            return _elements(value)
    return 0


def _elements(value: Any) -> int:
    """The elements of an annotation's array value, 1 for a single value."""
    if value.type != "element_value_array_initializer":
        return 1
    return sum(1 for e in value.named_children if e.type not in COMMENT_KINDS)


def _members(source: str) -> Optional[List[Tuple[str, Any]]]:
    """Every type's declarations in ``source`` with qualified names
    (``Class.Nested.member``), in source order. None if it does not parse."""
    return _UNITS.members(source)


def _top_class(root: Any) -> Optional[Any]:
    """The public top-level class, else the first top-level class."""
    classes = [c for c in root.named_children if c.type == "class_declaration"]
    for candidate in classes:
        modifiers = next((c for c in candidate.children
                          if c.type == "modifiers"), None)
        if modifiers is not None and "public" in _text(modifiers).split():
            return candidate
    return classes[0] if classes else None


def _setup_assumptions(declaration: Any) -> List[Tuple[int, int]]:
    """The spans of the assumption statements (``ASSUMPTIONS``, a call by
    its simple name or on ``ASSUMPTION_CLASSES``) directly in the bodies of
    ``declaration``'s setup methods (``SETUP_ANNOTATIONS``)."""
    body = declaration.child_by_field_name("body")
    spans: List[Tuple[int, int]] = []
    for method in (body.named_children if body is not None else ()):
        if (method.type != "method_declaration"
                or not SETUP_ANNOTATIONS.intersection(_annotations(method))):
            continue
        block = method.child_by_field_name("body")
        for statement in (block.named_children if block is not None else ()):
            call = statement.named_children[0] if (
                statement.type == "expression_statement" and statement.named_children) else None
            name = call.child_by_field_name("name") if call is not None else None
            receiver = call.child_by_field_name("object") if call is not None else None
            if (call is not None and call.type in CALL_KINDS and name is not None
                    and _text(name) in ASSUMPTIONS
                    and (receiver is None or _text(receiver) in ASSUMPTION_CLASSES)):
                spans.append((statement.start_byte, statement.end_byte))
    return spans


def _cut(source: str, spans: List[Tuple[int, int]]) -> str:
    return treesitter.cut(source, spans)


_PARSE_HELPER = """\
import com.sun.source.util.JavacTask;
import java.io.File;
import java.nio.charset.StandardCharsets;
import java.util.Arrays;
import java.util.Locale;
import javax.tools.Diagnostic;
import javax.tools.DiagnosticCollector;
import javax.tools.JavaCompiler;
import javax.tools.JavaFileObject;
import javax.tools.StandardJavaFileManager;
import javax.tools.ToolProvider;

/** Parses each file named after the release on its own and prints one
 *  verdict a line: valid, or the first error's line, column and message. */
public class LangAdapterJavaParse {
    public static void main(String[] args) throws Exception {
        JavaCompiler compiler = ToolProvider.getSystemJavaCompiler();
        StandardJavaFileManager files = compiler.getStandardFileManager(
                null, Locale.ROOT, StandardCharsets.UTF_8);
        for (int i = 1; i < args.length; i++) {
            DiagnosticCollector<JavaFileObject> diagnostics = new DiagnosticCollector<>();
            JavacTask task = (JavacTask) compiler.getTask(null, files, diagnostics,
                    Arrays.asList("--release", args[0], "-proc:none"), null,
                    files.getJavaFileObjects(new File(args[i])));
            task.parse();
            String verdict = "valid";
            for (Diagnostic<? extends JavaFileObject> d : diagnostics.getDiagnostics()) {
                if (d.getKind() == Diagnostic.Kind.ERROR) {
                    verdict = "error\\t" + d.getLineNumber() + "\\t"
                            + d.getColumnNumber() + "\\t"
                            + d.getMessage(Locale.ROOT).replace('\\n', ' ');
                    break;
                }
            }
            System.out.println(verdict);
        }
    }
}
"""


def _parse_helper(javac: str, environment: Dict[str, str],
                  env: Dict[str, str], *, executor: Optional[Executor] = None,
                  scratch: Optional[Path] = None) -> Path:
    """The directory of the compiled parse helper (``_tools.compiled_helper``)."""
    return _tools.compiled_helper(javac, "LangAdapterJavaParse", _PARSE_HELPER, environment, env,
                                  prefix="parse", what="the parse helper",
                                  executor=executor, scratch=scratch)[0]


class JavaSources(LangAdapter):
    """The Java adapter's source operations: parsing and validating test
    sources, their metrics and skeletons, merges, collisions and repairs.
    ``JavaAdapter`` adds the project's runs. The operations that compile
    or run a source use the adapter's JDK and executor."""

    def check_structure(self, source: str) -> Any:
        """Whether ``source`` parses, with its first error (``StructureCheck``)."""
        result = _treesitter.check_structure(GRAMMAR, source)
        return StructureCheck(result.ok, result.message, result.line,
                              result.col, self.structural_parser_identity(), result.snippet)

    def structural_parser_identity(self) -> str:
        """The structural parser and grammar used by source operations."""
        return parser_identity()

    def test_metrics(self, source: str) -> Any:
        """A test source's counts (``TestMetrics``), absent if it does not parse."""
        error = self._parse_error(source)
        if error:
            return TestMetrics(parsed=False, parser=self.structural_parser_identity(), detail=error)
        root = _treesitter.parse(GRAMMAR, source).root_node
        tests = fixtures = assertions = cases = 0
        for node in _treesitter.walk(root):
            if node.type == "method_declaration":
                annotations = _annotations(node)
                if TEST_ANNOTATIONS.intersection(annotations):
                    tests += 1
                if LIFECYCLE_ANNOTATIONS.intersection(annotations):
                    fixtures += 1
                if "ParameterizedTest" in annotations:
                    for modifiers in node.children:
                        if modifiers.type == "modifiers":
                            cases += sum(_parameter_cases(a)
                                         for a in modifiers.named_children
                                         if a.type == "annotation")
            elif node.type == "assert_statement":
                assertions += 1
            elif node.type in CALL_KINDS:
                name = node.child_by_field_name("name")
                if name is not None and _text(name) in ASSERTION_CALLS:
                    assertions += 1
        return TestMetrics(
            parsed=True, parser=self.structural_parser_identity(),
            code_lines=_lizard.code_lines_and_complexities(source, ".java")[0],
            tests=tests, assertions=assertions, fixtures=fixtures,
            parametrized_cases=cases)

    def skeleton(self, source: str) -> Tuple[str, ...]:
        """Qualified type and method names in source order."""
        return _treesitter.skeleton(GRAMMAR, source, func_kinds=FUNC_KINDS,
                                    class_kinds=CLASS_KINDS)

    def same_skeleton(self, first: str, second: str) -> Tuple[bool, str]:
        """Whether sources declare the same types and methods, and why not."""
        return _treesitter.diff_skeletons(self.skeleton(first),
                                          self.skeleton(second))

    def function_fingerprints(self, source: str) -> Dict[str, str]:
        """Static per-method fingerprints, excluding comments."""
        return _treesitter.function_fingerprints(
            GRAMMAR, source, func_kinds=FUNC_KINDS, class_kinds=CLASS_KINDS,
            ignore_kinds=COMMENT_KINDS)

    def optimization_metrics(self, source: str) -> Dict[str, Any]:
        """Static structural metrics. These are not execution measurements."""
        return _treesitter.optimization_metrics(
            GRAMMAR, source, func_kinds=FUNC_KINDS, loop_kinds=LOOP_KINDS,
            call_kinds=CALL_KINDS, lizard_suffix=".java", class_kinds=CLASS_KINDS,
            io_call_names=IO_CALL_NAMES)

    def scoped_optimization_metrics(self, source: str) -> Any:
        """The structural metrics of ``source`` per declaration."""
        return _treesitter.scoped_optimization_metrics(
            GRAMMAR, source, func_kinds=FUNC_KINDS, loop_kinds=LOOP_KINDS,
            call_kinds=CALL_KINDS, lizard_suffix=".java", class_kinds=CLASS_KINDS,
            io_call_names=IO_CALL_NAMES,
            signature=lambda node: f"({_signature(node)})")

    def call_statistics(self, source: str) -> Optional[Dict[str, int]]:
        """The call sites of ``source`` by called method name, None when it
        does not parse: the call search of ``sources.call_statistics``."""
        return _treesitter.called_names(GRAMMAR, source, call_kinds=CALL_KINDS)

    def imports(self, source: str) -> Tuple[str, ...]:
        """Imported names in source order, none for an unparseable source."""
        root = _treesitter.parse(GRAMMAR, source).root_node
        if root.has_error:
            return ()
        out: List[str] = []
        for node in root.named_children:
            if node.type != "import_declaration":
                continue
            text = _text(node).strip()
            text = re.sub(r"^import\s+(static\s+)?", "", text).rstrip(";").strip()
            text = re.sub(r"\s+", "", text)
            if text and text not in out:
                out.append(text)
        return tuple(out)

    def import_path_candidates(self, name: str) -> Tuple[str, ...]:
        """The source path of an imported class, without resolving it."""
        if not name or name.endswith(".*"):
            return ()
        parts = name.split(".")
        for index in range(len(parts) - 1, -1, -1):
            if parts[index][:1].isupper():
                return ("/".join(parts[:index + 1]) + ".java",)
        return ("/".join(parts) + ".java",)

    def duplicate_test_names(self, source: str) -> List[str]:
        """Test methods a class declares twice."""
        members = _members(source)
        if members is None:
            return []
        seen: Set[Tuple[str, str]] = set()
        out: List[str] = []
        for qualified, node in members:
            if node.type != "method_declaration":
                continue
            if not TEST_ANNOTATIONS.intersection(_annotations(node)):
                continue
            key = (qualified, _signature(node))
            if key in seen and qualified not in out:
                out.append(qualified)
            seen.add(key)
        return out

    def merge_collisions(self, first: str, second: str) -> Tuple[str, ...]:
        """Member names two test classes share, without rewriting."""
        return tuple(sorted(_top_member_names(first)
                            & _top_member_names(second)))

    def stage_pool(self, members: Any, *, setup: Any = ()) -> Any:
        """Several suites' files as one ``StagedPool``, types named for their suite."""
        from langadapter.java.source_analysis import _stage_pool
        return _stage_pool(members, setup)

    def is_test_file(self, path: Any) -> bool:
        """Whether ``path`` is named as JUnit and the build tools name test
        files: a .java file ending in Test, Tests, TestCase or IT."""
        name = Path(path).name
        return (Path(name).suffix.lower() == ".java"
                and name.endswith(TEST_FILE_SUFFIXES))

    def validate_source(self, source: str) -> Any:
        """Whether javac's parser of the project's JDK accepts ``source``
        at the project's release (``SourceValidation``)."""
        return self._validate_sources([source])[0]

    def prune_tests(self, source: str, tests: Any) -> Any:
        """``source`` without the named tests (``SourceResult``)."""
        names: List[str] = []
        cased: List[str] = []
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
            if qualname not in names:
                names.append(qualname)
            if params is not None and qualname not in cased:
                cased.append(qualname)
        members = _members(source)
        if members is None:
            return SourceResult(PARSE_ERROR, source, self.structural_parser_identity(),
                                unmatched=tuple(names),
                                detail=self._parse_error(source))
        methods: Dict[str, List[Any]] = {}
        for qualified, node in members:
            if node.type == "method_declaration":
                methods.setdefault(qualified, []).append(node)
        removed: List[str] = []
        unmatched: List[str] = []
        spans: List[Tuple[int, int]] = []
        for name in names:
            match = templates.named_unit(name, methods)
            if match is None:
                unmatched.append(name)
            elif match not in removed:
                removed.append(match)
                spans += [(n.start_byte, n.end_byte) for n in _test_overloads(methods[match])]
        if not spans:
            return SourceResult(UNCHANGED, source, self.structural_parser_identity(),
                                unmatched=tuple(unmatched))
        return SourceResult(APPLIED, _cut(source, spans), self.structural_parser_identity(),
                            unmatched=tuple(unmatched), removed=tuple(removed),
                            detail=". ".join(
                                f"{name} is removed whole: JUnit names its cases "
                                "by their position" for name in cased
                                if name not in unmatched))

    def merge_sources(self, first: str, second: str, *,
                      separator: str) -> Any:
        """Two test classes as one (``SourceResult``)."""
        if not separator.startswith("//") or "\n" in separator:
            raise InvalidRequest("separator", "must be one // comment line")
        for label, text in (("the first source", first),
                            ("the second source", second)):
            error = self._parse_error(text)
            if error:
                return SourceResult(PARSE_ERROR, first, self.structural_parser_identity(),
                                    detail=f"{label} does not parse: {error}")
        merged, problem = _merge(first, second, separator)
        items = [(first, "the first source"), (second, "the second source")]
        if merged is not None:
            items.append((merged, "the merged source"))
        for refusal in self._refusals(items):
            if refusal is not None:
                return SourceResult(refusal.status, first, self.structural_parser_identity(),
                                    detail=refusal.detail)
        if merged is None:
            return SourceResult(REFUSED, first, self.structural_parser_identity(), detail=problem)
        return SourceResult(APPLIED, merged, self.structural_parser_identity())

    def normalize_shadowed(self, source: str) -> Any:
        """``source`` without declarations a class makes twice (``SourceResult``)."""
        refusal = self._refusal(source, "the source")
        if refusal is not None:
            return refusal
        from langadapter.java.source_analysis import (
            normalize_shadowed_definitions,
        )
        normalization = normalize_shadowed_definitions(source)
        if not normalization.parse_ok:
            return SourceResult(PARSE_ERROR, source, self.structural_parser_identity(),
                                detail=self._parse_error(source))
        if not normalization.changed:
            return SourceResult(UNCHANGED, source, self.structural_parser_identity())
        return SourceResult(
            APPLIED, normalization.source, self.structural_parser_identity(),
            removed=tuple(".".join((*b.scope, b.name))
                          for b in normalization.removed_bindings))

    def normalize_bindings(self, source: str) -> Any:
        """Static full declaration report, without a JDK or build."""
        from langadapter.java.source_analysis import normalize_shadowed_definitions
        return normalize_shadowed_definitions(source)

    def resolve_collisions(self, sources: Any, *, preference: Any = None,
                           localize_autouse: bool = False) -> Any:
        """Collisions among test classes resolved before they merge into the
        first one's class, as a ``SourceObservation`` of a
        ``CollisionResolution``. Without a ``preference`` a
        conflict is reported and nothing dropped. ``localize_autouse`` keeps
        each source's per-test lifecycle methods on its own tests."""
        from langadapter.java._collisions import resolve
        return resolve(self, sources, preference=preference,
                       localize=localize_autouse)

    def salvage_syntax(self, source: str) -> Any:
        """A bounded repair of a source javac's parser rejects
        (``SourceResult``, a generated-suite operation)."""
        parser = "javac's parser"
        result = templates.repair_syntax(source, self.validate_source, _local_fix)
        verdict = result.validation
        if result.status == templates.VALID:
            return SourceResult(UNCHANGED, result.source, parser)
        if result.status == templates.REPAIRED:
            return SourceResult(APPLIED, result.source, parser,
                                detail=f"{result.repairs} repair(s)", repairs=result.repairs)
        if result.status == templates.VALIDATOR_FAILED:
            return SourceResult(INFRASTRUCTURE_FAILURE, source, parser,
                                detail=f"the validator could not run: {verdict.message}")
        if result.status == templates.EXHAUSTED:
            return SourceResult(NOT_REPAIRED, source, parser,
                                detail=(f"still does not parse after "
                                        f"{templates._REPAIR_ROUNDS} repairs"))
        return SourceResult(
            NOT_REPAIRED, source, parser,
            detail=(f"still does not parse after {result.repairs} repair(s): "
                    f"{verdict.message} (line {verdict.line})"))

    def prune_unresolvable_imports(self, source: str) -> Any:
        """``source`` without imports javac cannot resolve on the project's
        test classpath (``SourceResult``, a generated-suite operation)."""
        refusal = self._refusal(source, "the source")
        if refusal is not None:
            return refusal

        def failing(text: str) -> templates.Failing:
            root = _treesitter.parse(GRAMMAR, text).root_node
            imports = {_javac_line(text, node): node for node in root.named_children
                       if node.type == "import_declaration"}
            errors = self._compile_errors(text)
            if errors is None:
                return templates.Failing(stop="javac could not run")
            return templates.Failing([imports[line] for line in sorted(set(errors))
                                      if line in imports])

        def remove(text: str, nodes: Sequence[Any]) -> Tuple[str, List[str]]:
            return (_cut(text, [(n.start_byte, n.end_byte) for n in nodes]),
                    [re.sub(r"^import\s+(static\s+)?|;$", "", _text(n).strip()) for n in nodes])

        pruning = templates.prune_units(source, failing, remove, rounds=5)
        if pruning.status == templates.STOPPED:
            return SourceResult(INFRASTRUCTURE_FAILURE, source, self.structural_parser_identity(),
                                detail=pruning.stop)
        if pruning.status == templates.UNCHANGED:
            return SourceResult(UNCHANGED, source, self.structural_parser_identity())
        return SourceResult(APPLIED, pruning.source, self.structural_parser_identity(), removed=pruning.removed)

    def prune_collection_breakers(self, source: str, *,
                                  timeout_s: float) -> Any:
        """``source`` without the declarations that keep a test class from
        compiling or initializing (``SourceResult``, a generated-suite operation)."""
        refusal = self._refusal(source, "the source")
        if refusal is not None:
            return refusal

        def load(text: str) -> templates.Load:
            compiled = self._compilation(text)
            if compiled is None:
                return templates.Load(False, failure="javac could not run",
                                      stop="infrastructure")
            compiles, lines, diagnostics = compiled
            if not compiles:
                return templates.Load(False, tuple(lines),
                                      f"the class does not compile: {diagnostics[-500:]}")
            if not _static_initialization_can_throw(text):
                return templates.Load(True)
            state, failure, lines = self._run_check(text, timeout_s)
            if state == "infrastructure":
                return templates.Load(False, failure=failure, stop="infrastructure")
            if state == "runs":
                return templates.Load(True)
            return templates.Load(False, tuple(lines), failure)

        def remove(text: str, lines: Sequence[int]):
            breakers = _breaking_declarations(text, lines)
            if breakers is None:
                return None
            return (_cut(text, [(node.start_byte, node.end_byte) for _, node in breakers]),
                    [label for label, _ in breakers])

        result = templates.repair_loading(source, load, remove, loads=BREAKER_ROUNDS + 1,
                                          keeps_a_test=lambda text: bool(_test_methods(text)),
                                          report_unmappable=False)
        if result.status == templates.STOPPED:
            return SourceResult(INFRASTRUCTURE_FAILURE, source, self.structural_parser_identity(),
                                detail=result.failure)
        if result.status == templates.LOADS:
            if not result.removed:
                return SourceResult(UNCHANGED, source, self.structural_parser_identity())
            return SourceResult(APPLIED, result.source, self.structural_parser_identity(), removed=result.removed,
                                detail=f"{len(result.removed)} declaration(s) removed")
        if result.status == templates.EXHAUSTED:
            detail = f"still after {BREAKER_ROUNDS} rounds of removal, {result.failure}"
        elif result.status == templates.NOT_REMOVABLE:
            detail = ("a reported line lies in no declaration that can be removed, "
                      f"{result.failure}")
        elif result.status == templates.NO_TEST_LEFT:
            detail = ("no test is left once the declarations that keep the tests "
                      f"from running are removed: {', '.join(result.removed)}")
        else:
            detail = result.failure
        return SourceResult(NOT_REPAIRED, source, self.structural_parser_identity(), detail=detail)

    def strip_module_level_skips(self, source: str) -> Any:
        """``source`` without the annotations that skip a whole class
        (``SourceResult``, a generated-suite operation)."""
        refusal = self._refusal(source, "the source")
        if refusal is not None:
            return refusal
        root = _treesitter.parse(GRAMMAR, source).root_node
        spans = []
        for node in root.named_children:
            if node.type not in CLASS_KINDS:
                continue
            for modifiers in node.children:
                if modifiers.type != "modifiers":
                    continue
                for annotation in modifiers.named_children:
                    name = annotation.child_by_field_name("name")
                    if (annotation.type in ("marker_annotation", "annotation")
                            and name is not None and _text(name).rsplit(".", 1)[-1]
                            in CLASS_SKIPS):
                        spans.append((annotation.start_byte, annotation.end_byte))
            spans.extend(_setup_assumptions(node))
        if not spans:
            return SourceResult(UNCHANGED, source, self.structural_parser_identity())
        data = source.encode("utf-8")
        return SourceResult(
            APPLIED, _cut(source, spans), self.structural_parser_identity(),
            removed=tuple(data[s:e].decode("utf-8") for s, e in spans),
            detail=f"{len(spans)} annotation(s) removed")

    def target_identity(self, target: str) -> Any:
        """The ``TargetIdentity`` of a file under test, for the validity policy."""
        from langadapter.java import _validity
        container = (self.context.execution.kind == "container"
                 or isinstance(self._executor, ContainerExecutor))
        issue = (self._context_issue() or self.execution_setup_issue()) if container else ""
        if issue:
            raise InvalidRequest("execution", issue)
        path = self._project_file("target", target)
        root = self.context.root
        try:
            return _validity.target_identity(path, root, executor=self._executor)
        except ValueError as exc:
            reraise_unresolved(exc)
            raise InvalidRequest("target", str(exc)) from None

    def analyze_validity(self, source: str, target: Any) -> Any:
        """The validity policy's findings on a generated test source
        (``PolicyReport``, a generated-suite operation)."""
        from langadapter.java import _validity
        try:
            findings = _validity.analyze(source, target)
        except SyntaxError as exc:
            reraise_unresolved(exc)
            check = self.check_structure(source)
            return PolicyReport(PARSE_ERROR, (), _validity.POLICY_VERSION,
                                self.structural_parser_identity(), detail=str(exc),
                                error_line=check.line, error_column=check.column)
        return PolicyReport("completed", findings, _validity.POLICY_VERSION,
                            self.structural_parser_identity())

    def screen_source(self, source: str, target: Any) -> Any:
        """The policy's review-only screening signals on a generated test
        source (``PolicyReport``, a generated-suite operation)."""
        from langadapter.java import _validity
        flags = _validity.screen(source, target)
        if len(flags) == 1 and flags[0].rule_id == "unparseable_source":
            return PolicyReport(PARSE_ERROR, (), _validity.POLICY_VERSION,
                                self.structural_parser_identity(), detail=flags[0].diagnostic,
                                error_line=flags[0].start_line,
                                error_column=flags[0].start_col)
        return PolicyReport("completed", flags, _validity.POLICY_VERSION,
                            self.structural_parser_identity())

    def finalize_source(self, source: str) -> Any:
        """``source`` without detached comment blocks (``SourceResult``)."""
        error = self._parse_error(source)
        if error:
            return SourceResult(PARSE_ERROR, source, self.structural_parser_identity(), detail=error)
        finalized = _strip_detached_comments(source)
        return SourceResult(APPLIED if finalized != source else UNCHANGED,
                            finalized, self.structural_parser_identity())

    # Helpers

    def _parse_error(self, source: str) -> str:
        check = self.check_structure(source)
        if check.ok:
            return ""
        return f"{_treesitter.structure_message(check)} (line {check.line}, column {check.column})"

    def _validate_sources(self, sources: Sequence[str]) -> List[SourceValidation]:
        """javac's verdict on each source, judged in one JVM for those
        without a verdict cached per adapter by source hash."""
        adapter = self
        cache: Dict[str, SourceValidation] = adapter._parse_verdicts
        digests = [hashlib.sha256(s.encode("utf-8")).hexdigest() for s in sources]
        issue = adapter._context_issue() or adapter.execution_setup_issue()
        if issue:
            return [SourceValidation(INFRASTRUCTURE_FAILURE, issue, 0, 0,
                                     "javac not executed", digest) for digest in digests]
        resolution = adapter.project()
        jdk = adapter._jdk()
        if resolution.outcome is Outcome.UNSUPPORTED:
            # No build at the root: the JDK's own release, named as such.
            release = jdk.feature
            identity = (f"javac {jdk.version} --release {release} "
                        f"(no project build at the root)")
        elif resolution.outcome is not Outcome.COMPLETED:
            return [SourceValidation(
                INFRASTRUCTURE_FAILURE,
                f"the project does not resolve: {resolution.reason}", 0, 0,
                f"javac {jdk.version}", digest) for digest in digests]
        else:
            release = resolution.project.release_of_tests()
            identity = f"javac {jdk.version} --release {release}"
        todo = list(dict.fromkeys(
            (d, s) for d, s in zip(digests, sources) if d not in cache))
        if todo:
            for digest, verdict in zip([d for d, _ in todo], self._parse(
                    [s for _, s in todo], release, identity, jdk)):
                if verdict.status in ("valid", "syntax_error"):
                    cache[digest] = verdict
                else:
                    cache.setdefault(f"failed:{digest}", verdict)
        return [cache.get(d) or cache[f"failed:{d}"] for d in digests]

    def _parse(self, sources: Sequence[str], release: int, identity: str,
               jdk: Any) -> List[SourceValidation]:
        """javac's parser on ``sources`` in one JVM."""
        adapter = self
        digests = [hashlib.sha256(s.encode("utf-8")).hexdigest() for s in sources]
        environment = adapter.context.environment
        executor = adapter._executor
        container = isinstance(executor, ContainerExecutor)
        env = tool_environment(jdk, environment, executor=executor)

        def failure(status: str, message: str) -> List[SourceValidation]:
            return [SourceValidation(status, message, 0, 0, identity, d)
                    for d in digests]

        try:
            host = _tools.host_environment(adapter.context)
            helper = (None if container else _parse_helper(jdk.tool("javac"), host, env))
            with adapter._scratch("parse") as scratch:
                if container:
                    helper = _parse_helper(jdk.tool("javac"), host, env,
                                           executor=executor, scratch=Path(scratch))
                files = []
                for index, source in enumerate(sources):
                    directory = Path(scratch) / str(index)
                    directory.mkdir()
                    file = directory / (_public_class(source) + ".java")
                    file.write_text(source, encoding="utf-8")
                    files.append(str(file))
                argv = [jdk.tool("java"), "-cp", str(helper),
                        "LangAdapterJavaParse", str(release), *files]
                done = JavaEnv(executor).launch(argv, 120 + 10 * len(files), env=env)
                verdicts = done.stdout.strip().splitlines()
                if done.returncode != 0 or len(verdicts) != len(sources):
                    _retain_scratch(scratch, "javac's parser produced no complete verdict")
                    return failure(INFRASTRUCTURE_FAILURE,
                                   f"javac's parser exited {done.returncode}: "
                                   + (done.stdout + done.stderr).strip()[-500:])
                out = []
                for digest, verdict in zip(digests, verdicts):
                    if verdict == "valid":
                        out.append(SourceValidation("valid", "", 0, 0, identity, digest))
                        continue
                    _, line, column, message = (verdict.split("\t", 3) + ["", "", ""])[:4]
                    out.append(SourceValidation("syntax_error", message, int(line or 0),
                                                int(column or 0), identity, digest))
                return out
        except subprocess.TimeoutExpired as exc:
            reraise_unresolved(exc)
            return failure("timeout", "javac's parser timed out")
        except (OSError, RuntimeError) as exc:
            reraise_unresolved(exc)
            return failure(INFRASTRUCTURE_FAILURE, str(exc))

    def _refusal(self, source: str, label: str) -> Optional[SourceResult]:
        """A result refusing to rewrite ``source`` when javac's parser
        rejects it or cannot run, or None when it accepts it."""
        return self._refusals([(source, label)])[0]

    def _refusals(self, items: Sequence[Tuple[str, str]]) -> List[Optional[SourceResult]]:
        """``_refusal`` for each ``(source, label)``, judged in one JVM."""
        out: List[Optional[SourceResult]] = []
        for (source, label), verdict in zip(
                items, self._validate_sources([s for s, _ in items])):
            if verdict.valid:
                out.append(None)
            elif verdict.status == "syntax_error":
                out.append(SourceResult(
                    REFUSED, source, self.structural_parser_identity(),
                    detail=(f"javac's parser rejects {label}: {verdict.message} "
                            f"(line {verdict.line}, column {verdict.column})")))
            else:
                out.append(SourceResult(
                    INFRASTRUCTURE_FAILURE, source, self.structural_parser_identity(),
                    detail=(f"javac's parser could not validate {label}: "
                            f"{verdict.message}")))
        return out

    def _compiled(self, source: str, options: Sequence[str] = ()
                  ) -> Optional[Tuple[str, Any]]:
        """The file name ``source`` compiles as and javac's ``Compilation``
        of it against the test classpath, or None when javac did not run."""
        adapter = self
        resolution = adapter.project()
        if resolution.outcome is not Outcome.COMPLETED:
            return None
        project, jdk = resolution.project, adapter._jdk()
        with adapter._scratch("compile") as scratch:
            file = _place(Path(scratch) / "src", source)
            compiled = compile_tests(project, jdk, [str(file)],
                                     str(Path(scratch) / "classes"),
                                     adapter._executor,
                                     environment=adapter.context.environment,
                                     options=options)
            if compiled.outcome in (Outcome.INFRASTRUCTURE_FAILURE, Outcome.TIMED_OUT):
                _retain_scratch(scratch, f"source compilation failed: {compiled.diagnostics}")
        if compiled.outcome in (Outcome.INFRASTRUCTURE_FAILURE, Outcome.TIMED_OUT):
            return None
        return file.name, compiled

    def _compile_errors(self, source: str) -> Optional[List[int]]:
        """The lines javac reports errors on compiling ``source``, or None."""
        compiled = self._compiled(source)
        if compiled is None:
            return None
        name, compilation = compiled
        pattern = re.compile(re.escape(name) + r":(\d+): ")
        return [int(m.group(1)) for m in pattern.finditer(compilation.diagnostics)]

    def _compilation(self, source: str) -> Optional[Tuple[bool, List[int], str]]:
        """Whether ``source`` compiles on the test classpath, its error lines (by
        locale-independent raw diagnostics) and the diagnostics, or None."""
        compiled = self._compiled(source, (RAW_DIAGNOSTICS,))
        if compiled is None:
            return None
        name, compilation = compiled
        pattern = re.compile(r"(?m)^(?:.*[\\/])?" + re.escape(name)
                             + r":(\d+):(?:\d+:)? compiler\.err\.")
        lines = sorted({int(m.group(1)) for m in pattern.finditer(compilation.diagnostics)})
        return (compilation.outcome is Outcome.COMPLETED, lines,
                compilation.diagnostics)

    def _run_check(self, source: str, timeout_s: float) -> Tuple[str, str, List[int]]:
        """Whether a compiling class's tests run: ``("runs", "", [])``,
        ``("failed", why, lines)`` with a throwing static initialization's
        lines, or ``("infrastructure", why, [])`` without a verdict."""
        from langadapter.runs import SuiteRunRequest
        from langadapter.suites import Suite
        adapter = self
        with adapter._scratch("collect") as scratch:
            root = Path(scratch) / "suite"
            file = _place(root, source)
            run = adapter.run_suite(SuiteRunRequest(
                suite=Suite(str(root), (file.relative_to(root).as_posix(),)),
                timeout_s=timeout_s))
            if run.outcome in (Outcome.INFRASTRUCTURE_FAILURE, Outcome.TIMED_OUT):
                _retain_scratch(scratch, f"collection validation failed: {run.reason}")
        if run.outcome is Outcome.TIMED_OUT:
            return "infrastructure", f"the run reached the timeout of {timeout_s:g} s", []
        if run.outcome in (Outcome.INFRASTRUCTURE_FAILURE, Outcome.MISSING_SETUP,
                           Outcome.UNSUPPORTED):
            return "infrastructure", run.reason, []
        if run.outcome is Outcome.COLLECTION_FAILED:
            return "failed", f"the class does not compile: {run.output[-500:]}", []
        # The first test meets the exception, every later one the class
        # left uninitialized ("Could not initialize class" and its name).
        initialization = ("ExceptionInInitializerError", "NoClassDefFoundError",
                          "Could not initialize class")
        if run.tests and all(t.outcome == "error" and any(
                marker in t.message for marker in initialization)
                for t in run.tests):
            return ("failed", f"the class fails before its tests run: {run.tests[0].message}",
                    _initializer_lines(file.name, [*(t.message for t in run.tests), run.output]))
        return "runs", "", []


def _javac_line(source: str, node: Any) -> int:
    """The line javac reports ``node`` of ``source`` at. A carriage return
    alone ends a line for javac, not a tree-sitter row."""
    before = source.encode("utf-8")[:node.start_byte].decode("utf-8")
    return _lines.line_number(before, len(before))


def _local_fix(lines: List[str], verdict: Any) -> bool:
    """Fix one syntax error: a closing brace for a source ending inside a
    block, else blank the reported line. False when beyond repair."""
    if "reached end of file" in verdict.message:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append("}\n")
        return True
    number = verdict.line
    if not number or number > len(lines) or not lines[number - 1].strip():
        return False
    ended = lines[number - 1] != _lines.bare(lines[number - 1])
    lines[number - 1] = "\n" if ended else ""
    return True


def _initializer_lines(file_name: str, texts: Iterable[str]) -> List[int]:
    """The lines of ``file_name`` that the static initialization frames
    (``<clinit>``) of the stack traces in ``texts`` name."""
    pattern = re.compile(r"\.<clinit>\(" + re.escape(file_name) + r":(\d+)\)")
    return sorted({int(m.group(1)) for text in texts for m in pattern.finditer(text)})


#: The values a static field can take without code that could throw.
_CONSTANT_VALUES = frozenset({
    "decimal_integer_literal", "hex_integer_literal", "octal_integer_literal",
    "binary_integer_literal", "decimal_floating_point_literal",
    "hex_floating_point_literal", "true", "false", "character_literal",
    "string_literal", "null_literal", "class_literal",
})


def _static_initialization_can_throw(source: str) -> bool:
    """Whether a top-level type's static initialization runs code that
    could throw: a static block, or a static or interface field with a
    non-literal value. Nested types initialize on their own, when used."""
    root = _treesitter.parse(GRAMMAR, source).root_node

    def constant(value: Any) -> bool:
        if value.type == "unary_expression" and len(value.named_children) == 1:
            value = value.named_children[0]
        return value.type in _CONSTANT_VALUES

    for declaration in root.named_children:
        if declaration.type not in CLASS_KINDS:
            continue
        body = declaration.child_by_field_name("body")
        members = list(body.named_children) if body is not None else []
        for member in list(members):
            if member.type == "enum_body_declarations":
                members += member.named_children
        for member in members:
            if member.type == "static_initializer":
                return True
            static = member.type == "constant_declaration" or (
                member.type == "field_declaration" and any(
                    child.type == "modifiers" and "static" in _text(child).split()
                    for child in member.children))
            if static and any(
                    declarator.type == "variable_declarator"
                    and (value := declarator.child_by_field_name("value")) is not None
                    and not constant(value)
                    for declarator in member.named_children):
                return True
    return False


def _test_overloads(overloads: List[Any]) -> List[Any]:
    """The overloads of a test's name that are tests: those with a test
    annotation, else those without parameters (JUnit 3's), else all."""
    return _UNITS.test_overloads(overloads)


def _test_methods(source: str) -> int:
    """The number of test methods ``source`` declares, in every type."""
    return _UNITS.test_count(source)


def _breaking_declarations(source: str, lines: Iterable[int]
                           ) -> Optional[List[Tuple[str, Any]]]:
    """The smallest removable declaration (an import or a type member,
    innermost where types nest, all that share the line) around each of
    javac's ``lines``, labeled with its qualified name (with parameter
    types for a method or constructor). One inside another chosen goes with
    it. None when a line lies in none (the package, a top-level type's
    header, an enum constant) or ``source`` does not parse."""
    return _UNITS.units_at(source, lines)


def _public_class(source: str) -> str:
    root = _treesitter.parse(GRAMMAR, source).root_node
    top = _top_class(root)
    return _treesitter.name(top) if top is not None else "Source"


def _place(root: Path, source: str) -> Path:
    """Write ``source`` to its package's directory under ``root``, named for
    its class, returning the file."""
    match = re.search(r"^\s*package\s+([\w.]+)\s*;", source, re.MULTILINE)
    directory = root.joinpath(*match.group(1).split(".")) if match else root
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / (_public_class(source) + ".java")
    file.write_text(source, encoding="utf-8")
    return file


def _signature(node: Any) -> str:
    """A method's parameter types, as written, without names or spaces."""
    parameters = node.child_by_field_name("parameters")
    if parameters is None:
        return ""
    types = []
    for parameter in parameters.named_children:
        kind = parameter.child_by_field_name("type")
        text = _text(kind) if kind is not None else parameter.type
        if parameter.type == "spread_parameter":
            text += "..."
        types.append(re.sub(r"\s+", "", text))
    return ",".join(types)


#: Java's units: types, members, imports and tests, for the toolkit.
_UNITS = treesitter.TreeSitterUnits(treesitter.Rules(
    grammar=GRAMMAR,
    type_kinds=CLASS_KINDS,
    function_kinds=FUNC_KINDS,
    is_test=lambda node: (node.type == "method_declaration"
                          and bool(TEST_ANNOTATIONS.intersection(_annotations(node)))),
    signature=_signature,
    field_kinds=frozenset({"field_declaration"}),
    named_fields=frozenset({"field_declaration", "constant_declaration"}),
    passthrough_kinds=frozenset({"enum_body_declarations"}),
    skipped_kinds=frozenset({"enum_constant"}),
    comment_kinds=COMMENT_KINDS,
    import_kind="import_declaration",
    import_label=lambda text: re.sub(r"^import\s+(static\s+)?|;$", "", text),
    initializers={"static_initializer": "<static initializer>", "block": "<initializer>"},
))


def _key(qualified: str, node: Any) -> Tuple[str, str, str]:
    """What makes two declarations of a class the same declaration."""
    if node.type in FUNC_KINDS:
        return (qualified, "method", _signature(node))
    if node.type == "field_declaration":
        return (qualified, "field", "")
    return (qualified, "type", "")


def _top_member_names(source: str) -> Set[str]:
    root = _treesitter.parse(GRAMMAR, source).root_node
    if root.has_error:
        return set()
    top = _top_class(root)
    body = top.child_by_field_name("body") if top is not None else None
    if body is None:
        return set()
    names: Set[str] = set()
    for member in body.named_children:
        if member.type in FUNC_KINDS or member.type in CLASS_KINDS:
            names.add(_treesitter.name(member))
        elif member.type == "field_declaration":
            names.update(_treesitter.name(d) for d in member.named_children
                         if d.type == "variable_declarator")
    names.discard("<anonymous>")
    return names


def _header(cls: Any) -> Tuple[str, str, str]:
    """A class's annotations, superclass and interfaces, as written."""
    modifiers = next((c for c in cls.children if c.type == "modifiers"), None)
    annotations = " ".join(
        re.sub(r"\s+", "", _text(a)) for a in (modifiers.named_children
                                               if modifiers is not None else ())
        if a.type in ("marker_annotation", "annotation"))
    superclass = cls.child_by_field_name("superclass")
    interfaces = cls.child_by_field_name("interfaces")
    return (annotations,
            re.sub(r"\s+", "", _text(superclass)) if superclass else "",
            re.sub(r"\s+", "", _text(interfaces)) if interfaces else "")


#: Only silences warnings: classes differing in it alone merge, keeping both's.
_SUPPRESS = ("SuppressWarnings", "java.lang.SuppressWarnings")


def _class_annotations(cls: Any) -> List[Any]:
    modifiers = next((c for c in cls.children if c.type == "modifiers"), None)
    return [a for a in (modifiers.named_children if modifiers is not None else ())
            if a.type in ("marker_annotation", "annotation")]


def _suppresses(annotation: Any) -> bool:
    name = annotation.child_by_field_name("name")
    return name is not None and _text(name) in _SUPPRESS


def _suppressed(cls: Any) -> List[str]:
    """The warnings a class's @SuppressWarnings names, as written, in order."""
    found: List[str] = []
    for annotation in _class_annotations(cls):
        if not _suppresses(annotation):
            continue
        stack = [annotation.child_by_field_name("arguments")]
        while stack:
            node = stack.pop()
            if node is None:
                continue
            if node.type == "string_literal":
                found.append(_text(node))
                continue
            stack.extend(reversed(node.named_children))
    return list(dict.fromkeys(found))


def header_difference(first: Any, second: Any) -> str:
    """Which part of two classes' headers differs: "annotations" (bar
    @SuppressWarnings), "superclass", "interfaces", or ""."""
    def parts(cls: Any) -> Tuple[str, str, str]:
        _, superclass, interfaces = _header(cls)
        return (" ".join(re.sub(r"\s+", "", _text(a)) for a in _class_annotations(cls)
                         if not _suppresses(a)), superclass, interfaces)

    for label, mine, theirs in zip(("annotations", "superclass", "interfaces"),
                                   parts(first), parts(second)):
        if mine != theirs:
            return label
    return ""


def _with_suppressed(source: str, warnings: Sequence[str]) -> str:
    """``source`` whose top-level class also suppresses ``warnings``."""
    top = _top_class(_treesitter.parse(GRAMMAR, source).root_node)
    if top is None:
        return source
    union = list(dict.fromkeys([*_suppressed(top), *warnings]))
    if union == _suppressed(top):
        return source
    text = ("@SuppressWarnings(" + union[0] + ")" if len(union) == 1
            else "@SuppressWarnings({" + ", ".join(union) + "})")
    existing = next((a for a in _class_annotations(top) if _suppresses(a)), None)
    if existing is not None:
        return apply_edits(source, [(existing.start_byte, existing.end_byte, text)])
    data = source.encode("utf-8")
    line = data.rfind(b"\n", 0, top.start_byte) + 1
    indent = data[line:top.start_byte].decode("utf-8")
    return apply_edits(source, [(top.start_byte, top.start_byte,
                                 text + "\n" + (indent if not indent.strip() else ""))])


def _merge(first: str, second: str, separator: str) -> Tuple[Optional[str], str]:
    """The merged source, or None with the reason it cannot be merged."""
    first_root = _treesitter.parse(GRAMMAR, first).root_node
    second_root = _treesitter.parse(GRAMMAR, second).root_node
    anchor, other = _top_class(first_root), _top_class(second_root)
    if anchor is None or other is None:
        return None, "a source declares no top-level class"

    def package(root: Any) -> str:
        node = next((c for c in root.named_children
                     if c.type == "package_declaration"), None)
        return re.sub(r"\s+", " ", _text(node)).strip() if node else ""

    if package(first_root) != package(second_root):
        return None, (f"the sources' packages differ ({package(first_root)!r},"
                      f" {package(second_root)!r})")
    difference = header_difference(anchor, other)
    if difference:
        part = ("annotations", "superclass", "interfaces").index(difference)
        return None, (f"the classes' {difference} differ (the first's "
                      f"{_header(anchor)[part]!r}, the second's {_header(other)[part]!r})")
    suppressed = _suppressed(other)
    other_body = other.child_by_field_name("body")
    anchor_body = anchor.child_by_field_name("body")
    if any(m.type == "constructor_declaration"
           for m in other_body.named_children):
        return None, "the second class declares a constructor"
    first_types = {_treesitter.name(c) for c in first_root.named_children
                   if c.type in CLASS_KINDS}
    extra = [c for c in second_root.named_children
             if c.type in CLASS_KINDS and c != other]
    clash = sorted(first_types.intersection(_treesitter.name(c) for c in extra))
    if clash:
        return None, f"both sources declare the top-level type(s) {', '.join(clash)}"
    old, new = _treesitter.name(other), _treesitter.name(anchor)
    if old != new:
        # Imported here: source_analysis imports this module.
        from langadapter.java import source_analysis as _rename

        renames, reason = _rename._type_edits(second_root, old, new)
        if renames is None:
            return None, f"the second class {old} cannot be renamed safely: {reason}"
        second = apply_edits(second, renames)
        second_root = _treesitter.parse(GRAMMAR, second).root_node
        other = _top_class(second_root)
        extra = [c for c in second_root.named_children
                 if c.type in CLASS_KINDS and c != other]
        other_body = other.child_by_field_name("body")

    def imports(root: Any) -> List[str]:
        return [_text(c) for c in root.named_children
                if c.type == "import_declaration"]

    # The first source stays as written. Edits apply from its end backwards.
    data_first = first.encode("utf-8")
    edits: List[Tuple[int, int, str]] = []
    tail = "".join("\n\n" + _text(c) for c in extra)
    if tail:
        edits.append((len(data_first), len(data_first),
                      ("" if first.endswith("\n") else "\n")
                      + tail.lstrip("\n") + "\n"))
    close = anchor_body.end_byte - 1
    body_end = len(data_first[:close].rstrip())
    inner = _text(other_body)[1:-1].strip("\n")
    edits.append((body_end, close,
                  "\n\n    " + separator + "\n" + inner + "\n"))
    first_imports = [c for c in first_root.named_children
                     if c.type == "import_declaration"]
    added = list(_treesitter._ordered_union(
        imports(first_root), imports(second_root))[len(first_imports):])
    if added:
        text = "\n".join(added)
        if first_imports:
            edits.append((first_imports[-1].end_byte, first_imports[-1].end_byte,
                          "\n" + text))
        else:
            declaration = next((c for c in first_root.named_children
                                if c.type == "package_declaration"), None)
            at = declaration.end_byte if declaration is not None else 0
            edits.append((at, at, ("\n\n" if declaration is not None else "")
                          + text + ("\n" if declaration is not None else "\n\n")))
    return _with_suppressed(apply_edits(first, edits), suppressed), ""


def _strip_detached_comments(source: str) -> str:
    """``source`` without full-line comment runs, at one indentation, that
    no code at that indentation follows on the next line. A file header
    before the first declaration stays, and the result is kept only when
    its code tokens equal the source's."""
    reduced = _detached_reduction(source)
    if reduced == source:
        return source
    before = _treesitter.non_comment_leaf_fingerprint(
        GRAMMAR, source, comment_kinds=COMMENT_KINDS)
    after = _treesitter.non_comment_leaf_fingerprint(
        GRAMMAR, reduced, comment_kinds=COMMENT_KINDS)
    if not before or before != after:
        return source
    return reduced


def _detached_reduction(source: str) -> str:
    root = _treesitter.parse(GRAMMAR, source).root_node
    if root.has_error:
        return source
    # Tree-sitter's rows of the encoded source, as its columns count bytes.
    lines = _lines.rows(source.encode("utf-8"))
    if not lines:
        return source
    first_code = min((c.start_point[0] for c in root.named_children
                      if c.type not in COMMENT_KINDS), default=len(lines))
    units = []
    for node in _treesitter.walk(root):
        if node.type not in COMMENT_KINDS:
            continue
        (row, col), (end_row, end_col) = node.start_point, node.end_point
        if node.type == "line_comment" and end_col == 0:
            end_row -= 1  # tree-sitter ends a line comment after its newline
            end_col = len(lines[end_row].rstrip(b"\r\n"))
        if end_row >= len(lines):
            continue
        if lines[row][:col].strip() or lines[end_row].rstrip(b"\r\n")[end_col:].strip():
            continue
        units.append((row, end_row, col))
    units.sort()
    remove: Set[int] = set()
    index = 0
    while index < len(units):
        row, end_row, col = units[index]
        last = end_row
        nxt = index + 1
        while (nxt < len(units) and units[nxt][0] == last + 1
               and units[nxt][2] == col):
            last = units[nxt][1]
            nxt += 1
        following = lines[last + 1].rstrip(b"\r\n") if last + 1 < len(lines) else b""
        indent = len(following) - len(following.lstrip(b" \t"))
        attached = bool(following.strip()) and indent == col
        if not attached and row >= first_code:
            for unit in units[index:nxt]:
                remove.update(range(unit[0], unit[1] + 1))
        index = nxt
    if not remove:
        return source
    kept = [line for number, line in enumerate(lines) if number not in remove]
    text = b"".join(kept).decode("utf-8")
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", text)
