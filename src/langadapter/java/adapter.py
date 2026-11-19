"""The Java adapter: the public operations for a Java project.

A context names the project's root and its JDK by its ``java`` executable
(``Interpreter.path``). The build tool resolves the project once per adapter
(``project``), and every operation runs on that model with the same tools.
"""
from __future__ import annotations

from typing import Any, Optional, Dict, List, Tuple
from langadapter._execution_scope import reraise_unresolved
import dataclasses
from contextlib import ExitStack
import importlib.util as importlib_util
import subprocess
import time
import xml.etree.ElementTree as ET
from importlib import metadata
from pathlib import Path, PurePath
from langadapter._prepared_copy import CreatedFiles, require_cleanup_prepared_copy
from langadapter.sources import whole
from langadapter.adapter import ProjectSetting
from langadapter.capabilities import (
    CAPABILITIES,
    Availability,
    Capability,
    CapabilityReport,
    LanguageFacts,
    ToolVersion,
)
from langadapter.context import ContainerExecution, LocalExecution, ProjectContext
from langadapter.errors import InvalidRequest
from langadapter.execution import (
    ContainerExecutor,
    Executor,
    _mounted_work_dir,
    _retain_scratch,
    execution_identity,
    executor_for,
    work_dir_tempdir,
)
from langadapter.outcomes import ObservationIdentity, ToolchainIdentity, Outcome
from langadapter.java import (
    _jacoco, _runs, _tools as _junit, _pit, _tools, _env as jdk_module,
)
from langadapter.java._sources import JavaSources
from langadapter.java._profile import COLLECTION_UNIT, SUPPORT, JavaProfile
from langadapter.java._policy import POLICY_VERSION as VALIDITY_POLICY_VERSION
from langadapter.versions import capability_runtime, facts_runtime, unknown
from langadapter.java._env import JavaEnv, require_mounted_output
from langadapter.java._build import Resolution, compile_suite, tool_environment
from langadapter.runs import (
    ERROR,
    FAILED,
    PASSED,
    SKIPPED,
    RunCounts,
    SuiteRun,
    SuiteRunRequest,
    TestOutcome,
)
from langadapter.suites import TestId


#: The names of Java test files (``test_files``,
#: ``LanguageFacts.test_file_patterns``).
_TEST_FILE_PATTERNS = ("*Test.java", "*Tests.java", "*TestCase.java")

#: The tools a Java context uses, by pinned version. The JUnit launcher follows
#: the project's platform, tree-sitter-java is the calling process's.
_PINNED_TOOLS: Dict[str, Optional[str]] = {
    "junit-platform-console": None,
    "jacoco": _jacoco.VERSION,
    "pitest": _pit.VERSION,
    "tree-sitter-java": None,
}


def _vocabulary(framework: str, timeout_marker: str) -> Dict[str, str]:
    """Java's words for its tests (``langadapter.vocabulary``) under JUnit
    version ``framework`` (``JUnit 4``, ``5`` or ``6``) and ``timeout_marker``."""
    junit4 = framework == "JUnit 4"
    before, before_all, after = (("@Before", "@BeforeClass", "@After") if junit4
                                 else ("@BeforeEach", "@BeforeAll", "@AfterEach"))
    test_import = "org.junit.Test" if junit4 else "org.junit.jupiter.api.Test"
    assertions = ("org.junit.Assert" if junit4
                  else "org.junit.jupiter.api.Assertions")
    return {
        "valid_source": "valid Java",
        "real_source": "real Java",
        "test_declaration": "``@Test void name()``",
        "test_declaration_full": f"``@Test`` method (``{test_import}``)",
        "collection_rule": (
            f"{framework} runs the methods annotated with ``@Test`` "
            f"(``{test_import}``) of the test class."),
        "fixture_term": ("lifecycle methods (``@Before``, ``@After``)"
                         if junit4 else
                         "lifecycle methods (``@BeforeEach``, ``@AfterEach``)"),
        "parametrize_term": ("the ``Parameterized`` runner" if junit4
                             else "``@ParameterizedTest``"),
        "single_file_rule": (
            "Produce one test class in one ``.java`` file, named for the "
            "class, in the target's package. Do not create helper files."),
        "test_file": "``<TargetClass>Test.java``",
        "import_rule": ("declares the target's package and uses the "
                        "target class directly"),
        "load_error": "compilation error",
        "fake_builtin_rule": "",
        "exception_assertion": ("``@Test(expected = ...)``" if junit4
                                else "``assertThrows``"),
        "timeout_marker": timeout_marker or "",
        "parse_error_hints": (
            "missing semicolon or closing brace, unbalanced parentheses "
            "or quotes, a statement outside a method, accidental "
            "code-fence remnants"),
        "test_dependencies": (
            f"Test toolkit (always available):\n"
            f"  - {framework} (``{test_import}``, ``{assertions}``)\n"
            "The project's own classes and its declared test dependencies "
            "are also available; other libraries are available only if "
            "the project's build declares them."),
        # The rules java/_validity.py enforces (target_source_read,
        # target_class_read, copied_target). Reflection is only screened.
        "validity_rule": (
            "Allowed: the target's runtime behaviour and its API, including "
            "reflection on its classes (getDeclaredMethods, getAnnotations). "
            "Forbidden: reading its source file (the .java file through Files, "
            "a reader or stream, a Scanner or getResource) or its compiled "
            "class file (the .class bytes through getResource or a file), or "
            "declaring a type named like its class, which would test a copy "
            "in its place."),
        # A measured run has the suite's files in one JVM, a delivered run each
        # alone. Java has no instrument, so no seed or restored state differs.
        "stands_alone_advice": (
            "  1. A test file must compile and run on its own: declare in it\n"
            "     every helper class it uses, not in another test file.\n"
            "  2. A test class must set up what it needs in its own "
            f"``{before}``\n"
            f"     or ``{before_all}`` methods, not rely on what another test "
            "class\n"
            "     leaves behind (static fields, singletons, files it writes).\n"
            "  3. A test must restore what it changes beyond itself in\n"
            f"     ``{after}``: static fields and singletons, System properties,\n"
            "     the default Locale and TimeZone."),
        "load_error_patterns": (
            "  - cannot find symbol -> import the class from its package, or fix\n"
            "    the name of the class, method or field.\n"
            "  - package ... does not exist -> import only packages the project or\n"
            "    its test dependencies provide.\n"
            "  - incompatible types / cannot be applied to given types -> pass the\n"
            "    types the target's signatures declare.\n"
            "  - unreported exception ... must be caught or declared to be thrown\n"
            "    -> declare the test method ``throws Exception``.\n"
            "  - ';' expected / reached end of file while parsing -> check the\n"
            "    working file for unbalanced braces or parentheses and stray\n"
            "    characters from a previous edit."),
    }


class JavaAdapter(JavaSources):
    """A Java project's adapter."""

    language = "java"
    #: Its structural metrics count code lines and complexity with lizard
    #: (``sources._StructuralProvider``).
    _lizard_metrics = True
    # Facts (``langadapter.hooks``). Framework, fence and patterns are language facts.
    display_name = "Java"
    #: The program that runs the tests. The project's JDK is the context's.
    runtime = "java"
    test_file_name = "{stem}Test.java"
    #: A class name, so that a scratch file is CandidateTest.java.
    scratch_stem = "Candidate"
    collection_unit = COLLECTION_UNIT
    #: The version of the generated-test validity policy whose findings
    #: ``analyze_validity`` reports.
    validity_policy_version = VALIDITY_POLICY_VERSION
    #: Per-test evidence leaves out the coverage contexts no reported test
    #: claims, a JUnit id naming no test (``LanguageFacts.unmatched_evidence_kept``).
    unmatched_evidence_kept = False

    def file_rule(self, source: str) -> Optional[str]:
        """The class a Java file is named for: its public top-level class,
        else its first one. None when it declares none or does not parse."""
        from langadapter.java.source_analysis import top_class_name
        return top_class_name(source) or None

    def rename_file_unit(self, source: str, new_name: str) -> Any:
        """The source with its file's class renamed (``rename_class``):
        unsupported when unsafe, unchanged without a class or if already named."""
        from langadapter.java.source_analysis import rename_class
        from langadapter.sources import SourceObservation, SourceStatus

        found = self.file_rule(source)
        if not found or found == new_name:
            return SourceObservation(SourceStatus.COMPLETED, value=source)
        return rename_class(source, found, new_name)

    def test_id_prefix(self, source: str, stem: str) -> str:
        """A test id's start: the file's declared package and class ``stem``."""
        from langadapter.java._java_source import declared_package
        package = declared_package(source)
        return (package + "." if package else "") + stem
    source_merge_separator = "//"
    native_mutation_engines = ("pit",)
    mutation_evidence_granularity = ("aggregate", "per_mutant", "per_test")
    test_evidence_granularity = (
        ("run_suite", ("aggregate", "per_test")), ("run_as_delivered", ("aggregate", "per_test")),
        ("benchmark", ("aggregate",)), ("per_test_evidence", ("per_test",)),
    )
    file_extensions = (".java",)
    source_execution_kinds = ("local", "container")
    test_execution_kinds = ("local", "container")
    mutation_execution_kinds = ("local", "container")
    #: What the Java adapter supports (``LanguageSupport``).
    support = SUPPORT

    def __init__(self, context: ProjectContext, *,
                 executor: Optional[Executor] = None) -> None:
        super().__init__(context)
        self._executor = executor or executor_for(context)
        #: The project's resolution, once resolved (``project``).
        self._resolution: Optional[Resolution] = None
        #: The context's JDK, once found (``_read_jdk``).
        self._found_jdk: Optional[jdk_module.Jdk] = None
        #: javac's parse verdicts by the source's SHA-256.
        self._parse_verdicts: Dict[str, Any] = {}
        #: The files the test runs that asked to remove them created.
        self._created_files = CreatedFiles(context)

    def inspect_project(self, request):
        """Read the selected JDK, the build files and the tool cache,
        running nothing, and report what the project's setup has and lacks."""
        from langadapter.java._setup import inspect
        return inspect(self, request)

    def verify_project(self, request):
        """In a prepared copy, run the JDK's ``java`` and ``javac`` version
        commands and, when a requested operation needs the build, the build's
        resolution, test compilation and test discovery, and report what the
        project's setup has and lacks."""
        from langadapter.java._setup import verify
        return verify(self, request)

    # The JDK and the project

    def _jdk(self) -> jdk_module.Jdk:
        """The context's JDK. A path that is no JDK is a request error. A
        container probe that could not run raises OSError."""
        self.context._require_execution()
        path = self.context.interpreter.path or ""
        found = self._read_jdk(strict=True)
        if found is None:
            raise InvalidRequest("interpreter", f"no JDK at {path!r}")
        return found

    def _read_jdk(self, *, strict: bool = False) -> Optional[jdk_module.Jdk]:
        """The context's JDK, read once, or None when it cannot have one or
        has none. ``strict`` raises when a container probe could not run
        (``read_in_container``)."""
        self.context._require_execution()
        if self._found_jdk is not None:
            return self._found_jdk
        if self._context_issue() or self.execution_setup_issue():
            return None
        path = self.context.interpreter.path or ""
        if isinstance(self.context.execution, ContainerExecution):
            self._found_jdk = jdk_module.read_in_container(
                path, self._executor, environment=self.context.environment, strict=strict)
        else:
            self._found_jdk = jdk_module.read(path)
        return self._found_jdk

    def project(self) -> Resolution:
        """The project as its build tool resolved it, once. A container JDK
        probe that could not run is an infrastructure failure."""
        from langadapter import _process_tree
        from langadapter.java._build import resolve

        issue = self._context_issue()
        if issue:
            return Resolution(Outcome.UNSUPPORTED, reason=issue)
        setup_issue = self.execution_setup_issue()
        if setup_issue:
            return Resolution(Outcome.MISSING_SETUP, reason=setup_issue)
        if self._resolution is None:
            try:
                jdk = self._jdk()
            except OSError as exc:
                reraise_unresolved(exc)
                return Resolution(Outcome.INFRASTRUCTURE_FAILURE, reason=str(exc))
            with self._scratch("resolve") as scratch:
                resolution = resolve(
                    self.context.root, jdk, self._executor, scratch,
                    environment=self.context.environment)
                if resolution.outcome in (Outcome.INFRASTRUCTURE_FAILURE, Outcome.TIMED_OUT):
                    _retain_scratch(scratch, f"project resolution failed: {resolution.reason}")
            if _process_tree.cancelled():
                # A resolution the cancel cut short is not the project's.
                return resolution
            self._resolution = resolution
        return self._resolution

    def toolchain_identity(self, platform: Optional[str] = None,
                           junit4: Optional[str] = None) -> ToolchainIdentity:
        """The context's JDK, with the JUnit Platform version ``platform``
        and the JUnit 4 version ``junit4`` among its tools when they are
        given."""
        jdk = self._jdk()
        tools = (("junit-platform", platform),) if platform else ()
        if junit4:
            tools += (("junit4", junit4),)
        return ToolchainIdentity.of(jdk_module.version_of(jdk), tools)

    def runtime_version(self) -> Optional[str]:
        """The context's JDK version, or None when the path is no JDK."""
        record = self._runtime_record()
        return facts_runtime(record) if record is not None else None

    def _runtime_refusal(self) -> str:
        """Why no operation runs on the context's JDK (one below the supported
        range), or "" (also while a context or setup issue stands)."""
        if self._context_issue() or self.execution_setup_issue():
            return ""
        return self.profile().refusal()

    def choose_profile(self) -> JavaProfile:
        """The context JDK's profile, classified against the Java declaration."""
        return JavaProfile(self._runtime_record(), self.support)

    def _runtime_record(self):
        """The context JDK's ``Version`` from its release file, or None."""
        if self._context_issue():
            return None
        found = self._read_jdk()
        return jdk_module.version_of(found) if found is not None else None

    def test_id_text(self, test) -> str:
        """A test's dotted id: its class's qualified name, nested classes
        after dots, and its method."""
        from langadapter.java._mutation import test_dotted_id
        return test_dotted_id(test)

    def coverage_texts(self, coverage) -> Dict[str, Tuple[Any, ...]]:
        """The covered lines, and per line the untaken branches JaCoCo counts
        (``"line->k of n branches untaken"``): its probes name no source branch."""
        if coverage is None:
            return {"covered_lines": (), "missing_branches": ()}
        total: Dict[int, int] = {}
        for branch in coverage.branches:
            total[branch.source] = total.get(branch.source, 0) + 1
        missing: Dict[int, int] = {}
        for branch in coverage.missing_branches():
            missing[branch.source] = missing.get(branch.source, 0) + 1
        return {"covered_lines": tuple(coverage.covered),
                "missing_branches": tuple(f"{line}->{count} of {total[line]} branches untaken"
                                          for line, count in sorted(missing.items()))}

    def request_options(self, purpose: str, *, target: Optional[str] = None,
                        traceback: str = "short", stop_at_first_failure: bool = False,
                        record_leaks: bool = False, session_seed: Optional[int] = None,
                        reverse_order: bool = False,
                        leave_out_timing_plugins: bool = False) -> Dict[str, Any]:
        """``remove_created_files`` for test, failing-test, evidence and delivered
        runs, else nothing: JUnit takes no runner options, seed or order, and
        has no timing plugins to leave out."""
        super().request_options(purpose)
        if purpose in ("run", "failing", "evidence", "delivered"):
            return {"remove_created_files": True}
        return {}

    def delivered_order_refusal(self, reverse: bool) -> Optional[str]:
        """Why a reverse order is refused: JUnit orders a class's tests itself."""
        if reverse:
            return ("JUnit orders a class's tests itself, so a reverse-order "
                    "run is not offered for Java")
        return None

    def collection_text(self, run) -> str:
        """"" for a completed collection, else its reason and output tail."""
        if run.outcome is Outcome.COMPLETED:
            return ""
        return (run.reason + ": " + run.output.strip()[-600:]).strip(": ")

    def skip_removal(self, source: str):
        """The source after ``strip_module_level_skips`` and how many it removed."""
        result = self.strip_module_level_skips(source)
        return result.source, len(result.removed)

    own_mutant_timeout_rule = True

    #: A Java project's settings.
    project_settings = (
        ProjectSetting("java_home", "A Java project's JDK: its home or its java executable. "
                       "Blank uses JAVA_HOME, except in a container, where it must be "
                       "named.", "path", alias="JAVA_HOME",
                       check="a JDK the release supports, read from its release file",
                       host_path="path"),
        ProjectSetting("build_java_home", "The JDK the build tool runs on when it needs a "
                       "newer Java than the project's (Gradle 9 runs on Java 17 or newer). "
                       "Blank uses java_home.", "path",
                       check="a JDK whose release file names a version", host_path="path"),
        ProjectSetting("maven", "The Maven launcher when the project has no Maven wrapper. "
                       "Blank uses mvn on the path.", "executable",
                       check="an existing launcher file", host_path="command"),
        ProjectSetting("gradle", "The Gradle launcher when the project has no Gradle wrapper. "
                       "Blank uses gradle on the path.", "executable",
                       check="an existing launcher file", host_path="command"),
    )

    @classmethod
    def discover_runtime(cls, root, values, *, container=False, environment=None,
                         resolve_path=None):
        """The JDK of java_home, else (locally) JAVA_HOME, as its java
        executable. In a container: an absolute POSIX path without
        backslashes or parent traversal, its bin/java unless it names java.
        Locally: the path ``resolve_path`` gives when that is a file, else
        the existing java or java.exe in its bin. InvalidRequest names
        java_home when no JDK is selected or the selection breaks a rule."""
        return cls._runtime(root, values, container=container, environment=environment,
                            resolve_path=resolve_path, describe=False)

    @classmethod
    def describe_runtime(cls, root, values, *, container=False, environment=None,
                         resolve_path=None):
        """The JDK as ``discover_runtime`` reads it, without checking that
        its java exists: no runtime when no JDK is selected. A container
        selection that breaks a rule is refused as there."""
        return cls._runtime(root, values, container=container, environment=environment,
                            resolve_path=resolve_path, describe=True)

    @classmethod
    def _runtime(cls, root, values, *, container, environment, resolve_path, describe):
        import os
        from pathlib import PurePosixPath

        from langadapter.adapter import RuntimeChoice
        from langadapter.context import Interpreter

        selected = str(values.get("java_home") or (
            "" if container else (environment or {}).get("JAVA_HOME", ""))).strip()
        if not selected and describe:
            return RuntimeChoice()
        if not selected:
            raise InvalidRequest("java_home", "no JDK is selected for the project in the "
                                 "container: select one" if container else
                                 "no JDK is selected for the project: select one, or set "
                                 "JAVA_HOME")
        if container:
            path = PurePosixPath(selected)
            if not path.is_absolute() or "\\" in selected or ".." in path.parts:
                raise InvalidRequest("java_home", "a container JDK is an absolute POSIX path "
                                     "without backslashes or parent traversal")
            path = PurePosixPath("/") / path.relative_to(path.anchor)
            executable = str(path if path.name == "java" else path / "bin" / "java")
        else:
            path = (resolve_path(selected) if resolve_path is not None else Path(selected))
            path = path.absolute()
            # A description reads the name only: a final java names the executable.
            named = (path.name.lower() in ("java", "java.exe") if describe
                     else path.is_file())
            java = path if named else (
                path / "bin" / ("java.exe" if os.name == "nt" else "java"))
            if not describe and not java.is_file():
                raise InvalidRequest("java_home", f"{selected} names no JDK: {java} does not "
                                     "exist")
            executable = str(java)
        return RuntimeChoice(Interpreter(path=executable))

    @classmethod
    def setup_questions(cls, project_dir, probe, *, consumer="the caller"):
        """The JDK home, then the build tool."""
        from langadapter.java._questions import questions
        return questions(Path(project_dir), probe)

    @classmethod
    def setup_problem(cls, setting, value, project_dir, probe):
        """A JDK must name its version, the project's one ``support`` does
        not refuse, and a build tool's launcher must exist. The other settings
        are checked by their kind."""
        from langadapter.java._questions import problem
        if setting in ("java_home", "build_java_home", "maven", "gradle"):
            return problem(setting, value, Path(project_dir), probe)
        return super().setup_problem(setting, value, project_dir, probe)

    @classmethod
    def context_environment(cls, values, *, container=False):
        """build_java_home, maven and gradle as the variables Java operations read."""
        found = {}
        for key, variable in (("build_java_home", "LANGADAPTER_BUILD_JAVA_HOME"),
                              ("maven", "LANGADAPTER_MAVEN"),
                              ("gradle", "LANGADAPTER_GRADLE")):
            value = str(values.get(key) or "").strip()
            if value:
                found[variable] = value
        return found

    def mutation_catalogue(self, config):
        """``pit_mutators``: empty for PIT's default mutators, one PIT group
        (``DEFAULTS``, ``REMOVE_CONDITIONALS``, ``REMOVE_SWITCH``, ``RETURNS``
        or ``STRONGER``), or a comma list of mutator names."""
        from langadapter.java import _pit
        from langadapter.mutation import Catalogue

        configured = str(config["pit_mutators"] or "").strip()
        if not configured:
            return Catalogue()
        names = tuple(n.strip() for n in configured.split(",") if n.strip())
        if len(names) == 1 and names[0] in _pit.GROUPS:
            return Catalogue(name=names[0])
        return Catalogue(operators=names)

    @classmethod
    def import_statement(cls, target: str, root: str) -> str:
        """The import hint for a test of ``target`` (none: it declares the named
        package and uses the classes directly), or "" for an unreadable file."""
        from langadapter.java._java_source import declared_package, read_source
        try:
            package = declared_package(read_source(target))
        except OSError:
            return ""
        if not package:
            return "none; the test declares no package, as the target, and uses its classes directly"
        return f"none; the test declares the target's package, {package}, and uses its classes directly"

    def test_files(self, paths) -> List[str]:
        """Each path, a directory replaced by its test files at any depth
        (``_TEST_FILE_PATTERNS``)."""
        files: List[str] = []
        for path in paths:
            test_dir = Path(path)
            if not test_dir.is_dir():
                files.append(str(path))
                continue
            matches = {match for pattern in _TEST_FILE_PATTERNS
                       for match in test_dir.rglob(pattern)}
            files.extend(str(match) for match in sorted(matches))
        return files

    def test_vocabulary(self) -> Dict[str, str]:
        """Java's words for its tests under this project's JUnit version."""
        facts = self.language_facts()
        return _vocabulary(facts.test_framework, facts.timeout_marker or "")

    def language_facts(self) -> LanguageFacts:
        """Java's facts for this project: its JUnit version, read from
        the resolved test classpath (JUnit 5 when it cannot be read)."""
        framework = self._framework()
        runtime = self._runtime_record()
        return LanguageFacts(
            language="Java",
            test_framework=framework,
            code_fence="java",
            runtime_version=facts_runtime(runtime) if runtime is not None else None,
            version_record=runtime,
            test_file_patterns=_TEST_FILE_PATTERNS,
            test_function_pattern="@Test",
            test_class_pattern="*Test",
            timeout_marker=JavaProfile.timeout_marker(framework),
            language_level=self._language_level(),
            unmatched_evidence_kept=self.unmatched_evidence_kept,
        )

    def _language_level(self) -> Optional[str]:
        """``Java N`` for the tests' ``--release``, maybe below the JDK's (no
        ``var`` for Java 8), or None when the project does not resolve."""
        try:
            resolution = self.project()
        except InvalidRequest:
            return None
        if resolution.project is None:
            return None
        return f"Java {resolution.project.release_of_tests()}"

    def _framework(self) -> str:
        """``JUnit 4``, ``JUnit 5`` or ``JUnit 6`` by the test classpath."""
        try:
            resolution = self.project()
        except InvalidRequest:
            return "JUnit 5"
        if resolution.project is None:
            return "JUnit 5"
        classpath = resolution.project.run_classpath()
        return JavaProfile.framework_name(_junit._version(classpath, "junit-jupiter-api"),
                                          bool(_junit._version(classpath, "junit")))

    def capabilities(self) -> CapabilityReport:
        """What this adapter can do for its context: the JDK, the resolved
        project and the pinned tools' jars, fetched into the cache if missing."""
        path = self.context.interpreter.path or ""
        issue = self._context_issue()
        if issue:
            return CapabilityReport(
                language=self.language, interpreter=path, outcome=Outcome.COMPLETED,
                reason=issue, runtime=None, tools=(), branch_coverage="counters",
                mutation_engines=(), capabilities=tuple(
                    Capability(name, Availability.UNSUPPORTED, issue) for name in CAPABILITIES))
        setup_issue = self.execution_setup_issue()
        if setup_issue:
            return CapabilityReport(
                language=self.language, interpreter=path, outcome=Outcome.MISSING_SETUP,
                reason=setup_issue, runtime=None, tools=(), branch_coverage="counters",
                mutation_engines=(), capabilities=tuple(
                    Capability(name, Availability.MISSING_SETUP, setup_issue) for name in CAPABILITIES))
        jdk = self._read_jdk()
        structural = all(importlib_util.find_spec(name) is not None
                         for name in ("tree_sitter", "tree_sitter_java"))
        found: Dict[str, Optional[str]] = dict.fromkeys(_PINNED_TOOLS)
        if structural:
            found["tree-sitter-java"] = _sources_version()

        runtime = jdk_module.version_of(jdk) if jdk else None

        def report(outcome: Outcome, reason: str,
                   capabilities: Tuple[Capability, ...]) -> CapabilityReport:
            return CapabilityReport(
                language=self.language, interpreter=path, outcome=outcome,
                reason=reason, runtime=capability_runtime(runtime) if runtime else None,
                version_record=runtime,
                capabilities=capabilities,
                tools=tuple(ToolVersion(name, pinned, found.get(name))
                            for name, pinned in _PINNED_TOOLS.items()),
                branch_coverage="counters",
                mutation_engines=(_pit_engine(),) if found["pitest"] else ())

        def every(availability: Availability, reason: str) -> Tuple[Capability, ...]:
            return tuple(Capability(name, availability, reason)
                         for name in CAPABILITIES)

        if jdk is None:
            reason = f"no JDK at {path!r}"
            return report(Outcome.MISSING_SETUP, reason,
                          every(Availability.MISSING_SETUP, reason))
        resolution = self.project()
        if resolution.outcome is Outcome.UNSUPPORTED:
            return report(Outcome.COMPLETED, "",
                          every(Availability.UNSUPPORTED, resolution.reason))
        if resolution.outcome is Outcome.MISSING_SETUP:
            return report(Outcome.MISSING_SETUP, resolution.reason,
                          every(Availability.MISSING_SETUP, resolution.reason))
        if resolution.outcome is not Outcome.COMPLETED:
            return report(Outcome.INFRASTRUCTURE_FAILURE, resolution.reason,
                          every(Availability.MISSING_SETUP, resolution.reason))
        missing: Dict[str, str] = {}
        failed: Dict[str, str] = {}
        scope = None

        def fetch(name: str, get: Any) -> None:
            """Obtain a tool's jars. ``get`` returns the version found."""
            try:
                found[name] = get()
            except _tools.ToolUnavailable as exc:
                reraise_unresolved(exc)
                missing[name] = str(exc)
            except (OSError, subprocess.SubprocessError) as exc:
                reraise_unresolved(exc)
                failed[name] = f"{type(exc).__name__}: {exc}"
                if scope is not None:
                    _retain_scratch(scope, f"{name} preparation failed: {exc}")

        try:
            with ExitStack() as stack:
                options: Dict[str, Any] = {}
                if isinstance(self._executor, ContainerExecutor):
                    allocated = stack.enter_context(self._scratch("capabilities"))
                    scratch = Path(allocated)
                    require_mounted_output(str(scratch), self._executor)
                    scope = allocated
                    options = {"executor": self._executor, "scratch": scratch}

                host = _tools.host_environment(self.context)
                fetch("junit-platform-console", lambda: _junit.toolset(
                    resolution.project.run_classpath(), host, **options).platform)

                def coverage_tools() -> str:
                    jars = [_tools.jar(c, host) for c in (
                        _jacoco.AGENT, *_jacoco.CORE)]
                    if options:
                        _tools.stage_files(jars, scratch / "tools", executor=self._executor)
                    return _jacoco.VERSION

                fetch("jacoco", coverage_tools)
                fetch("pitest", lambda: _pit.classpath(host, **options) and _pit.VERSION)
        except (OSError, subprocess.SubprocessError) as exc:
            reraise_unresolved(exc)
            reason = f"Java tool preparation failed: {type(exc).__name__}: {exc}"
            failed.update(dict.fromkeys(("junit-platform-console", "jacoco", "pitest"), reason))

        def needs(name: str, *tools: str) -> Capability:
            absent = [missing[tool] for tool in tools if tool in missing]
            absent.extend(failed[tool] for tool in tools if tool in failed)
            if not structural and name in ("source_merging",
                                           "generated_suite_operations"):
                absent.append("tree-sitter and tree-sitter-java are not "
                              "installed in the calling process")
            if absent:
                return Capability(name, Availability.MISSING_SETUP,
                                  ". ".join(absent))
            return Capability(name, Availability.AVAILABLE)

        return report(Outcome.INFRASTRUCTURE_FAILURE if failed else Outcome.COMPLETED,
                      ". ".join(f"{name}: {reason}" for name, reason in failed.items()), (
            needs("suite_runs", "junit-platform-console"),
            needs("coverage", "junit-platform-console", "jacoco"),
            needs("per_test_evidence", "junit-platform-console", "jacoco"),
            needs("per_test_kill_attribution", "junit-platform-console",
                  "pitest"),
            needs("mutation", "junit-platform-console", "pitest"),
            needs("source_merging"),
            needs("generated_suite_operations"),
        ))

    # Suite runs

    def run_suite(self, request: SuiteRunRequest) -> SuiteRun:
        """Run the suite's test files, compiled with the project's JDK for its
        release against its test classpath, by the JUnit Platform's console
        launcher with the project's test JVM settings. A ``coverage_target``
        (a main source file under the project root) is measured with JaCoCo."""
        request.validate()
        issue = self._request_issue(request)
        suite = request.suite
        files = self._suite_files(suite)
        if issue or self.execution_setup_issue():
            return _failed_run(Outcome.UNSUPPORTED if issue else Outcome.MISSING_SETUP,
                               issue or self.execution_setup_issue(), "", self._observation_identity(suite))
        try:
            target = (self._project_file("coverage_target", request.coverage_target)
                      if request.coverage_target else None)
            identity = self._observation_identity(suite, target)
        except (OSError, subprocess.SubprocessError) as exc:
            reraise_unresolved(exc)
            return _failed_run(Outcome.INFRASTRUCTURE_FAILURE,
                               f"the target identity could not be read: {exc}", "",
                               self._observation_identity(suite))
        resolution = self.project()
        if resolution.outcome is not Outcome.COMPLETED:
            return _failed_run(resolution.outcome, resolution.reason,
                               resolution.output, identity)
        project = resolution.project
        jdk = self._jdk()
        environment = self.context.environment
        env = tool_environment(jdk, environment, executor=self._executor)
        with ExitStack() as stack:
            try:
                scope = stack.enter_context(self._scratch("run"))
                scratch = Path(scope)
                require_mounted_output(str(scratch), self._executor)
            except (OSError, subprocess.SubprocessError) as exc:
                reraise_unresolved(exc)
                return _failed_run(Outcome.INFRASTRUCTURE_FAILURE,
                                   f"suite preparation failed: {exc}", "", identity)
            try:
                compiled = compile_suite(project, jdk, suite,
                                         str(scratch / "classes"), self._executor,
                                         environment=environment)
            except (OSError, subprocess.SubprocessError) as exc:
                reraise_unresolved(exc)
                _retain_scratch(scope, f"suite preparation failed: {exc}")
                return _failed_run(Outcome.INFRASTRUCTURE_FAILURE,
                                   f"suite preparation failed: {exc}", "", identity)
            if compiled.outcome is not Outcome.COMPLETED:
                if compiled.outcome in (Outcome.INFRASTRUCTURE_FAILURE, Outcome.TIMED_OUT):
                    _retain_scratch(scope, f"suite compilation failed: {compiled.diagnostics}")
                return _failed_run(compiled.outcome,
                                   "the suite does not compile",
                                   compiled.diagnostics, identity)
            try:
                tools = _junit.toolset(project.run_classpath(), _tools.host_environment(self.context),
                                       executor=self._executor, scratch=scratch / "tools")
            except _tools.ToolUnavailable as exc:
                reraise_unresolved(exc)
                return _failed_run(Outcome.MISSING_SETUP, str(exc), "", identity)
            except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError) as exc:
                reraise_unresolved(exc)
                _retain_scratch(scope, f"launcher tool preparation failed: {exc}")
                return _failed_run(Outcome.INFRASTRUCTURE_FAILURE,
                                   f"the launcher tools could not be prepared: {exc}", "", identity)
            identity = ObservationIdentity(
                target=identity.target, suite=identity.suite,
                toolchain=self.toolchain_identity(tools.platform, tools.junit4),
                instrument=None, execution=identity.execution)
            jvm_args = [*project.test_jvm_args, *self.context.interpreter_flags]
            classpath = [compiled.classes, *tools.jars, *project.run_classpath()]
            journal_path = scratch / "active-tests.json"
            if request.timeout_journal:
                helper = _runs.prepare_journal(self, jdk, classpath, scratch / "journal", env)
                if helper.outcome is not Outcome.COMPLETED:
                    if helper.outcome in (Outcome.INFRASTRUCTURE_FAILURE, Outcome.TIMED_OUT):
                        _retain_scratch(scope, f"journal preparation failed: {helper.diagnostics}")
                    return _failed_run(helper.outcome, "the journal helper could not compile",
                                       helper.diagnostics, identity)
                classpath.append(helper.classes)
                jvm_args.append(f"-D{_runs.JOURNAL_PROPERTY}={journal_path}")
            destfile = scratch / "jacoco.exec"
            if target is not None:
                try:
                    jvm_args.insert(0, _jacoco.agent_argument(
                        destfile, _jacoco.class_names(target, executor=self._executor),
                        _tools.host_environment(self.context),
                        executor=self._executor, scratch=scratch / "tools"))
                except _tools.ToolUnavailable as exc:
                    reraise_unresolved(exc)
                    return _failed_run(Outcome.MISSING_SETUP, str(exc), "",
                                       identity)
                except (OSError, subprocess.SubprocessError) as exc:
                    reraise_unresolved(exc)
                    _retain_scratch(scope, f"coverage agent preparation failed: {exc}")
                    return _failed_run(Outcome.INFRASTRUCTURE_FAILURE,
                                       f"the coverage agent could not be prepared: {exc}", "", identity)
            try:
                classes = _class_names(files)
            except (OSError, subprocess.SubprocessError) as exc:
                reraise_unresolved(exc)
                _retain_scratch(scope, f"test class observation failed: {exc}")
                return _failed_run(Outcome.INFRASTRUCTURE_FAILURE,
                                   f"the test classes could not be read: {exc}", "", identity)
            removed = self._remove_created(request)
            started = time.perf_counter()
            try:
                launch = _junit.launch(
                    jdk.tool("java"),
                    classpath,
                    list(classes), scratch / "reports",
                    jvm_args=jvm_args,
                    system_properties=project.test_system_properties,
                    cwd=project.root, env=env, timeout=request.timeout_s,
                    executor=self._executor)
            except (OSError, subprocess.SubprocessError) as exc:
                reraise_unresolved(exc)
                _retain_scratch(scope, f"launcher execution failed: {exc}")
                return _with_removed(_failed_run(
                    Outcome.INFRASTRUCTURE_FAILURE,
                    f"the launcher could not start: {exc}", "", identity), removed)
            # Timed to the launcher's end: recording created files is no part of it.
            wall = time.perf_counter() - started
            self._record_created(request)
            if launch.timed_out:
                _retain_scratch(scope, "suite launcher timed out")
            journal = (_runs.read_journal(journal_path, classes, suite.root)
                       if request.timeout_journal else None)
            try:
                cases = _junit.read_reports(launch.reports)
            except (OSError, ET.ParseError, ValueError) as exc:
                reraise_unresolved(exc)
                _retain_scratch(scope, f"launcher report failed: {exc}")
                # A timeout during the report's writing is the reason it is unreadable.
                failed = dataclasses.replace(
                    _failed_run(Outcome.TIMED_OUT, "the suite's timeout ended the run (its "
                                f"launcher report could not be read: {exc})",
                                launch.output, identity)
                    if launch.timed_out else
                    _failed_run(Outcome.INFRASTRUCTURE_FAILURE,
                                f"the launcher report could not be read: {exc}",
                                launch.output, identity),
                    wall_time_s=wall, returncode=launch.returncode)
                failed = _runs.apply_journal(failed, journal) if journal is not None else failed
                return _with_removed(failed, removed)
            coverage, coverage_issue, report_s = None, "", None
            if target is not None:
                identity = dataclasses.replace(identity, toolchain=dataclasses.replace(
                    identity.toolchain,
                    tools=(*identity.toolchain.tools, ("jacoco", _jacoco.VERSION))))
                report_started = time.perf_counter()
                try:
                    if destfile.is_file():
                        records, coverage_issue = _jacoco.analyze(
                            jdk, "run", destfile, target, project.main_classes,
                            environment=_tools.host_environment(self.context), env=env,
                            executor=self._executor, scratch=scratch)
                        if records is not None:
                            coverage = _jacoco.observation(records.get("suite"), target,
                                                          self.context.root,
                                                          executor=self._executor)
                        if coverage is None and not coverage_issue:
                            coverage_issue = "JaCoCo did not report the requested target"
                    else:
                        coverage_issue = "JaCoCo did not write execution data"
                except (_tools.ToolUnavailable, OSError, subprocess.SubprocessError,
                        ValueError) as exc:
                    reraise_unresolved(exc)
                    _retain_scratch(scope, f"coverage analysis failed: {exc}")
                    coverage_issue = f"coverage analysis failed: {exc}"
                finally:
                    report_s = time.perf_counter() - report_started
            run = _suite_run(launch, cases, classes, suite.root, wall,
                             identity)
            if coverage is not None or coverage_issue:
                run = dataclasses.replace(
                    run, coverage=coverage, coverage_report_s=report_s,
                    output=run.output + (f"\n{coverage_issue}" if coverage_issue else ""))
            if coverage_issue and run.outcome is Outcome.COMPLETED:
                run = dataclasses.replace(run, outcome=Outcome.INFRASTRUCTURE_FAILURE,
                                          reason=coverage_issue)
            if coverage_issue:
                _retain_scratch(scope, coverage_issue)
            if journal is not None:
                run = _runs.apply_journal(run, journal)
            if run.outcome in (Outcome.INFRASTRUCTURE_FAILURE, Outcome.TIMED_OUT):
                _retain_scratch(scope, run.reason or "suite observation did not complete")
            return _with_removed(run, removed)

    def collect_suite(self, request: Any) -> Any:
        """The suite files' tests as JUnit discovers them without running bodies
        (parameterized methods, factories, not invocations): a collection verdict."""
        from langadapter.java._runs import collect_suite
        return collect_suite(self, request)

    def run_as_delivered(self, request: SuiteRunRequest, *, sources: Any = None) -> SuiteRun:
        """Run each declared test file alone with its declared helpers, in
        JUnit's own lifecycle and order. ``sources`` can replace a listed
        test's text in a private copy. Input files stay unchanged. Session
        seeding and reverse order are not offered for Java."""
        from langadapter.java._runs import run_as_delivered
        return run_as_delivered(self, request, sources=sources)

    def benchmark(self, request: Any) -> Any:
        """The ``Benchmark`` (``BenchmarkRequest``) of a suite over repeated
        runs. Raises ``InvalidRequest`` for an invalid request."""
        from langadapter.java._benchmark import benchmark
        return benchmark(self, request)

    def benchmark_warmed(self, request: Any) -> Any:
        """The ``WarmedSuiteBenchmark`` (``WarmedSuiteBenchmarkRequest``): per
        fork one JVM runs the warm-ups and measured iterations, each
        ``Launcher.execute`` timed. Raises ``InvalidRequest`` when invalid."""
        from langadapter.java._warmed import benchmark_warmed
        return benchmark_warmed(self, request)

    def per_test_evidence(self, request: Any) -> Any:
        """The ``PerTestEvidence`` of a green run (``PerTestEvidenceRequest``):
        each test method's covered target lines and branches, time, and with a
        mutation run its kills. Raises ``InvalidRequest`` when invalid."""
        from langadapter.java._runs import per_test_evidence
        return per_test_evidence(self, request)

    # Mutation

    def mutation_engines(self) -> Tuple[str, ...]:
        """The engines this adapter offers: PIT, its jars fetched into the cache."""
        return (_pit_engine(),)

    def mutation_operators(self, engine: str = "pit") -> Any:
        """PIT's default mutators by their reported ids, sorted, as an
        ``OperatorCatalogue``: ``missing_setup`` when PIT cannot be obtained."""
        from langadapter.mutation import OperatorCatalogue, _require_engine
        _require_engine(engine, _pit_engine(), "Java")
        issue = self._context_issue()
        if issue:
            return OperatorCatalogue(engine, None, Outcome.UNSUPPORTED, issue)
        setup_issue = self.execution_setup_issue()
        if setup_issue:
            return OperatorCatalogue(engine, None, Outcome.MISSING_SETUP, setup_issue)
        environment = self.context.environment
        jdk = self._jdk()
        # A container project's JDK runs in the container, as the session's tools.
        try:
            with self._scratch("mutators") as scratch:
                require_mounted_output(str(scratch), self._executor)
                ids = _pit.mutator_ids(
                    jdk.tool("java"), [], Path(scratch),
                    environment=_tools.host_environment(self.context),
                    env=tool_environment(jdk, environment, executor=self._executor),
                    executor=self._executor)
        except _tools.ToolUnavailable as exc:
            reraise_unresolved(exc)
            return OperatorCatalogue(engine, None, Outcome.MISSING_SETUP,
                                     str(exc))
        except _pit.UnknownMutator as exc:
            reraise_unresolved(exc)
            return OperatorCatalogue(engine, None,
                                     Outcome.INFRASTRUCTURE_FAILURE, str(exc),
                                     tool_version=_pit.VERSION)
        except (OSError, subprocess.SubprocessError) as exc:
            reraise_unresolved(exc)
            return OperatorCatalogue(engine, None, Outcome.INFRASTRUCTURE_FAILURE,
                                     str(exc), tool_version=_pit.VERSION)
        return OperatorCatalogue(engine, ids, tool_version=_pit.VERSION)

    def open_mutation_session(self, engine: str = "pit", **options: Any) -> Any:
        """A PIT session for the project. ``workers`` are a run's minion threads."""
        from langadapter.java._mutation import open_session
        return open_session(self, engine, **options)

    def validate_mutation_session_options(self, engine: str, *, workers: int,
                                          distributor: Any, allow_fewer_workers: bool,
                                          settings: Any) -> None:
        """Refuse a PIT session's ``distributor``, ``allow_fewer_workers``
        and ``settings`` with ``InvalidRequest``: it takes ``workers`` only."""
        if distributor is not None or allow_fewer_workers or settings is not None:
            raise InvalidRequest("options", "PIT sessions take workers only, not distributor, allow_fewer_workers or settings")

    # Request parts

    def _context_issue(self) -> str:
        selected = self.context.execution
        executor = self._executor
        if isinstance(selected, ContainerExecution):
            if (not isinstance(executor, ContainerExecutor)
                    or executor.container != selected.container or executor.runner != selected.runner
                    or executor.helper_python != selected.helper_python
                    or executor._host_work_dir != str(selected.work_dir).replace("\\", "/").rstrip("/")
                    or executor._container_work_dir != _mounted_work_dir(selected)):
                return "Java container executor does not match the selected container, mount and control helper"
        elif isinstance(selected, LocalExecution) and isinstance(executor, ContainerExecutor):
            return "Java local context cannot use a container executor"
        framework = self.context.test_framework
        if framework.source != "project" or framework.path is not None:
            return "Java test_framework overrides are not implemented. Use the project's configuration"
        return ""

    def execution_setup_issue(self) -> str:
        """A container run without the helper Python, which Java's runs in a
        container need, or ""."""
        if (isinstance(self.context.execution, ContainerExecution)
                and self.context.execution.helper_python is None):
            return ("Java in a container needs the helper Python (execution.helper_python, "
                    "Python 3.10 or later in the container)")
        return ""

    def operation_context_issue(self, name: str) -> str:
        """Why an execution-backed operation cannot run in this context, such
        as a local context with a container executor or a test framework
        override, and "" for an operation that runs no tool."""
        group, method = name.split(".")
        return self._context_issue() if getattr(self, group).execution_backed(method) else ""

    def operation_readiness(self, name: str, report: CapabilityReport):
        """An execution-backed operation's readiness from the probe: missing
        setup without a JDK or a completed project probe, PIT for the
        operator catalogue, the tree-sitter-java grammar for the source
        operations, and the probe's capabilities for runs and mutation."""
        group, method = name.split(".")
        if not getattr(self, group).execution_backed(method):
            return None
        if report.runtime is None:
            return Capability(name, Availability.MISSING_SETUP, report.reason or "no project JDK is available")
        if group == "mutations" and method == "operators":
            tool = next((tool for tool in report.tools if tool.name == "pitest"), None)
            if tool is not None and tool.found:
                return Capability(name, Availability.AVAILABLE)
            # An early project-resolution failure did not check the PIT jars.
            mutation = next((item for item in report.capabilities if item.name == "mutation"), None)
            if (report.outcome is Outcome.COMPLETED and mutation is not None
                    and mutation.availability is Availability.MISSING_SETUP and tool is not None):
                return Capability(name, Availability.MISSING_SETUP, "PIT jars could not be obtained")
            return None
        if report.outcome is not Outcome.COMPLETED:
            return Capability(name, Availability.MISSING_SETUP, report.reason or "project probe did not complete")
        if group == "sources":
            parser = next((tool for tool in report.tools if tool.name == "tree-sitter-java"), None)
            if parser is None:
                return None
            if not parser.found:
                return Capability(name, Availability.MISSING_SETUP, "tree-sitter-java is not installed in the calling process")
            if method == "validate_source":
                return Capability(name, Availability.AVAILABLE)
            # Syntax-only validation has a native no-build fallback. These two edits do not.
            if method == "prune_unresolvable_imports":
                return self._capability_readiness(name, report, "generated_suite_operations")
            if method == "prune_collection_breakers":
                return self._capability_readiness(name, report, "suite_runs")
            return Capability(name, Availability.AVAILABLE)
        if group == "tests":
            requirement = "per_test_evidence" if method == "per_test_evidence" else "suite_runs"
            return self._capability_readiness(name, report, requirement)
        return self._capability_readiness(name, report, "mutation")

    def _request_issue(self, request: Any) -> str:
        if request.runner_options is not None:
            raise InvalidRequest("runner_options", "Java suite runs take no runner options")
        issue = self._context_issue() or self._runtime_refusal()
        if issue:
            return issue
        if getattr(request, "remove_created_files", False):
            # Only the prepared copy's files may be removed.
            require_cleanup_prepared_copy(self.context, self.context.root)
        controls = ("module_under_test", "isolation_config",
                    "record_leaks", "reverse_order")
        unsupported = [field for field in controls if getattr(request, field, False)]
        if getattr(request, "session_seed", None) is not None:
            unsupported.append("session_seed")
        return ("Java does not implement: " + ", ".join(unsupported)) if unsupported else ""

    def _observation_identity(self, suite: Any, target: Optional[PurePath] = None) -> ObservationIdentity:
        return ObservationIdentity(
            target=JavaEnv(self._executor).identity(target, self.context.root) if target else None,
            suite=suite.identity(),
            toolchain=(ToolchainIdentity.of(unknown("java")) if self._context_issue() or self.execution_setup_issue()
                       else self.toolchain_identity()),
            instrument=None,
            execution=execution_identity(self.context))

    def _project_file(self, field: str, value: str) -> PurePath:
        """A file of the request named relative to the project root or
        absolute under it. A missing one is a request error."""
        return JavaEnv(self._executor).project_file(field, value, self.context.root)

    @staticmethod
    def _suite_files(suite: Any) -> List[Path]:
        suite.validate()
        return [Path(suite.root) / f if not Path(f).is_absolute() else Path(f)
                for f in suite.test_files]

    @staticmethod
    def _class_names(files: List[Path]) -> Dict[str, Path]:
        return _class_names(files)

    @staticmethod
    def _suite_run(launch: _junit.Launch, cases: List[_junit.Case],
                   classes: Dict[str, Path], root: str,
                   wall: float, identity: ObservationIdentity) -> SuiteRun:
        """The suite run of a launch's report cases, for the JVM toolkit's timing."""
        return _suite_run(launch, cases, classes, root, wall, identity)

    @staticmethod
    def _with_removed(result: Any, removed: Tuple[str, ...]) -> Any:
        """``result`` with the files its request removed (``_with_removed``)."""
        return _with_removed(result, removed)

    # Helpers

    def _scratch(self, name: str):
        """Receipt-backed operation scratch, retained on interruption."""
        return work_dir_tempdir(self.context, self._executor,
                                prefix=f"langadapter_java_{name}_")

    def _remove_created(self, request: Any) -> Tuple[str, ...]:
        """Right before a request's test JVMs run, when it asks
        (``remove_created_files``): remove the files earlier such runs
        created in the prepared copy, returning their project-relative
        paths. Resolution and compilation came before, so no run made theirs."""
        if not request.remove_created_files:
            return ()
        return tuple(self._created_files.begin(self.context.root))

    def _record_created(self, request: Any) -> None:
        """Once the request's test JVMs have ended, record the files they
        created for the next run that asks to remove them."""
        if request.remove_created_files:
            self._created_files.end(self.context.root)


def _sources_version() -> Optional[str]:
    try:
        return metadata.version("tree-sitter-java")
    except metadata.PackageNotFoundError:
        return None


def _pit_engine() -> str:
    from langadapter.java._mutation import ENGINE
    return ENGINE


def _class_names(files: List[Path]) -> Dict[str, Path]:
    """Each test file's top-level class name, from its package and the class
    it declares (its file name when none parses), mapped to the file."""
    from langadapter.java.source_analysis import top_class_name
    names: Dict[str, Path] = {}
    for file in files:
        from langadapter.java._java_source import declared_package, read_source
        text = read_source(file)
        package = declared_package(text)
        names[(package + "." if package else "") + (top_class_name(text) or file.stem)] = file
    return names


def _test_id(case: _junit.Case, classes: Dict[str, Path], root: str) -> Optional[TestId]:
    """The suite's test a report case names, or None outside the suite's
    files: the run's and the kills' one builder (``_tools.test_id``)."""
    _, params = _junit.split_name(case.name)
    method = case.name[:-len(params)] if params else case.name
    return _junit.test_id(_junit.TestName(case.class_name, method, params), classes, root)


def _suite_run(launch: _junit.Launch, cases: List[_junit.Case],
               classes: Dict[str, Path], root: str,
               wall: float, identity: ObservationIdentity) -> SuiteRun:
    tests: List[TestOutcome] = []
    unmatched: List[str] = []
    # Tests a JUnit 3 or 4 suite class runs are reported under that class, no
    # file of the suite: listed unmatched, one that fails fails the run.
    unmatched_failing: List[str] = []
    for case in cases:
        test = _test_id(case, classes, root)
        if test is None:
            unmatched.append(f"{case.class_name}#{case.name}")
            if case.outcome in (FAILED, ERROR):
                unmatched_failing.append(f"{case.class_name}#{case.name}")
            continue
        tests.append(TestOutcome(test, case.outcome, case.time_s, case.message,
                                 case.trace, case.exception))
    counts = RunCounts(
        collected=len(tests),
        passed=sum(t.outcome == PASSED for t in tests),
        failed=sum(t.outcome == FAILED for t in tests),
        errors=sum(t.outcome == ERROR for t in tests),
        skipped=sum(t.outcome == SKIPPED for t in tests))
    if launch.timed_out:
        outcome, reason = Outcome.TIMED_OUT, "the suite's timeout ended the run"
    elif not cases and launch.returncode == 2:
        outcome, reason = Outcome.NO_TESTS, "the launcher found no test"
    elif counts.failed or counts.errors:
        outcome, reason = Outcome.TEST_FAILURES, ""
    elif unmatched_failing:
        outcome = Outcome.TEST_FAILURES
        reason = (f"{len(unmatched_failing)} test(s) reported under classes "
                  f"outside the suite failed: "
                  f"{', '.join(unmatched_failing)}")
    elif not cases:
        outcome = Outcome.INFRASTRUCTURE_FAILURE
        reason = f"the launcher exited {launch.returncode} without a test report"
        counts = RunCounts()
    elif launch.returncode == 0:
        outcome, reason = Outcome.COMPLETED, ""
    else:
        outcome = Outcome.INFRASTRUCTURE_FAILURE
        reason = f"the launcher exited {launch.returncode} without a failing test"
    return SuiteRun(outcome=outcome, reason=reason, counts=counts,
                    tests=tuple(tests), unmatched=tuple(unmatched),
                    wall_time_s=wall, output=whole(launch.output, "suite output"),
                    coverage=None, identity=identity,
                    returncode=launch.returncode)


def _with_removed(result: Any, removed: Tuple[str, ...]) -> Any:
    """``result`` with the files its request removed before its tests ran."""
    if not removed:
        return result
    return dataclasses.replace(result, side_effects=dataclasses.replace(
        result.side_effects, files_removed=tuple(removed)))


def _failed_run(outcome: Outcome, reason: str, output: str,
                identity: ObservationIdentity) -> SuiteRun:
    return SuiteRun(outcome=outcome, reason=reason, counts=RunCounts(),
                    tests=(), unmatched=(), wall_time_s=None,
                    output=whole(output, "suite output"), coverage=None, identity=identity)
