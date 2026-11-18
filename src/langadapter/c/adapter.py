"""The C adapter: a project's C file, tested by tests of four kinds, measured with gcov.

A run builds the target from the project's recorded build, compiles each
test file into a program linked with that build, and runs each program
whole, as delivered. Coverage comes from gcov's record of the target alone,
and mutants from Mull. The project's own tree is never built in.

Its tests are of one of four kinds (``c_tests``):

- ``c``: utest.h tests, each test file a program linked with the target.
- ``python``: Python 3 tests of a target built into a Python extension,
  discovered from the project's build.
- ``programs``: the project's own test programs, each judged by its exit
  status.
- ``ctest``: a CMake project's tests as ctest runs them.

With the three kinds of C tests the project's runtime is its GCC. With
Python tests it is the project's Python,
whose tests LangAdapter's Python adapter runs: the facts,
words and source operations of those tests are Python's, the build, coverage
and mutants C's. The settings a project declares reach the adapter as
variables of the project context (``context_environment``).
"""
from __future__ import annotations

import fnmatch
import importlib.util
import itertools
import os
import re
import shlex
import textwrap
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import langadapter as la
from langadapter.adapter import ProjectResolution, ProjectSetting, RuntimeChoice, SetupQuestion
from langadapter.capabilities import CAPABILITIES
from langadapter.errors import InvalidRequest
from langadapter import _process_tree
from langadapter._words import listed
from langadapter._execution_scope import operation
from langadapter.execution import (LocalExecutor, _as_text, executor_for, execution_identity,
                                   work_dir_tempdir)
from langadapter.mutation import MutationSession, OperatorCatalogue, _require_engine
from langadapter.python.adapter import PythonAdapter
from langadapter.runs import (
    ERROR, FAILED, KILLS_NOT_REQUESTED, KILLS_UNUSABLE, KILLS_USABLE, PASSED, SKIPPED,
    Benchmark, BenchmarkRequest, PerTestEvidence, PerTestEvidenceRequest, RunCounts,
    RunDiagnostic, SuiteRun, SuiteRunRequest,
    TestEvidence, TestOutcome, with_detail,
)
from langadapter.sources import SourceValidation, TestUnit
from langadapter.suites import KillItem, LineItem
from langadapter.treesitter import whole_lines
from langadapter.versions import Version

from langadapter.c import (
    _build, _ctest, _extension, _extension_policy, _gcov, _merge, _mull, _programs,
    _pool, _repair, _structure, _syntax, _timing, _toolchain, _utest, _validity,
)
from langadapter.c._mutation import CMutationSession

#: The variables of the project context that carry the C settings.
_SETTING_VARIABLES = (("c_tests", "LANGADAPTER_C_TESTS"),
                     ("c_compiler", "LANGADAPTER_C_COMPILER"),
                     ("c_build_kind", "LANGADAPTER_C_BUILD_KIND"),
                     ("c_build", "LANGADAPTER_C_BUILD"),
                     ("c_flags", "LANGADAPTER_C_FLAGS"),
                     ("c_link", "LANGADAPTER_C_LINK"),
                     ("c_build_cache", "LANGADAPTER_C_BUILD_CACHE"),
                     ("c_target", "LANGADAPTER_C_TARGET"),
                     ("c_mull", "LANGADAPTER_C_MULL"),
                     ("c_test_prelude", "LANGADAPTER_C_TEST_PRELUDE"),
                     ("c_ctest_arguments", "LANGADAPTER_C_CTEST_ARGUMENTS"),
                     ("c_test_support", "LANGADAPTER_C_TEST_SUPPORT"),
                     ("c_address_space_limit", "LANGADAPTER_C_ADDRESS_SPACE_LIMIT"))
#: What c_address_space_limit takes, as a setup says when a value is no size.
_ADDRESS_SPACE_FORMAT = ("The value must be a number of bytes, with K, M or G for powers "
                         "of 1024, such as 2G.")
#: Why nothing runs on a system other than Linux.
_LINUX_ONLY = "the C adapter runs on Linux only in this version"
#: The modules of LangAdapter's extra for C (``langadapter[c]``), each with
#: its distribution's name.
_EXTRA_MODULES = {"tree_sitter_c": "tree-sitter-c", "gcovr": "gcovr",
                  "setuptools": "setuptools"}
_SIZE = re.compile(r"([0-9]+)([KMG]?)", re.IGNORECASE)
#: A path inside one of the adapter's work directories (``work_dir_tempdir``'s
#: ``langadapter_c_`` ones and the builds' ``langadapter-c-``), up to the
#: name of its file.
_WORK_PATH = re.compile(r"[^\s:'\"`(]*/langadapter[-_]c[-_][^/\s]*/(?:[^/\s:'\"`]+/)*")
_UNITS = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30}
#: The variable that carries Python's python_import_root for Python tests of
#: the target's extension (Python's setting, which C declares, not C's own).
_IMPORT_ROOT_VARIABLE = "LANGADAPTER_C_PYTHON_IMPORT_ROOT"
#: The rounds of removal load repair tries.
_BREAKER_ROUNDS = 5
#: The rounds of include removal import pruning tries.
_INCLUDE_ROUNDS = 10
#: Python's source operations a C adapter with Python tests offers as
#: Python's own: those that read the test file's text alone.
_PYTHON_SOURCE_HOOKS = ("merge_sources", "salvage_syntax", "strip_module_level_skips",
                        "finalize_source", "setup_files", "shared_setup_files", "delivered_case",
                        "prune_tests", "duplicate_test_names", "test_metrics",
                        "function_fingerprints", "check_structure", "skeleton", "same_skeleton",
                        "imports", "stage_pool",
                        "order_dependent_mock_warnings", "normalize_shadowed",
                        "normalize_bindings", "resolve_collisions", "salvage_report",
                        "skip_removal", "collision_dependents", "collision_resolution")
#: Python's source operations that load the test file: they run with the
#: target's extension staged, as a run does. ``test_files`` collects so
#: too (``_extension_test_files``).
_PYTHON_LOADING_HOOKS = ("prune_collection_breakers", "prune_unresolvable_imports")
#: The kinds of a project's tests (``c_tests``).
_TEST_KINDS = ("c", "python", "programs", "ctest")
#: Why ctest's tests are not staged as one pool (``stage_pool``).
_CTEST_NO_POOL = "ctest's tests are its CMake project's, which does not pool"
#: How long the project's own build may run, once per adapter.
_BUILD_TIMEOUT_S = 1800.0
#: How long one compile of a test file or of the target may run.
_COMPILE_TIMEOUT_S = 300.0
#: utest.h's pinned commit, as the toolchain identity names it.
_UTEST_COMMIT = "706f3574fec4915e78d9234870cfcdf22f7af5d9"
#: The names of C test files (``is_test_file``, ``LanguageFacts.test_file_patterns``).
_TEST_FILE_PATTERNS = ("test_*.c",)
#: Why a run finds no target, for C tests and Python tests: every place
#: ``_target_of`` reads it from.
_NO_TARGET = ("no target: name the C file the tests are for (coverage_target, the run's "
             "target, the runner options' target or c_target), or let the test file name "
             "it: test_<stem>.c after one source file, or a quoted include of its header")
_NO_EXTENSION_TARGET = ("no target: name the C file the tests are for (coverage_target, the "
                       "run's target, the runner options' target or c_target), or let the "
                       "test file name it: test_<stem>.py after one source file, or an "
                       "import of its extension module")
#: The request fields this adapter does not provide. A run that asks for one
#: is refused as unsupported, never run without it.
_NOT_PROVIDED = (("the leak record", "record_leaks"),
                ("a session seed", "session_seed"), ("the reverse order", "reverse_order"),
                ("the isolated configuration", "isolation_config"),
                ("the removal of created files", "remove_created_files"))

#: utest.h's checks, each as ``ASSERT_<name>`` and ``EXPECT_<name>``.
_CHECKS = ("TRUE", "FALSE", "EQ", "NE", "LT", "LE", "GT", "GE", "NEAR", "STREQ", "STRNE",
          "STRNEQ", "STRNNE", "MEMEQ")


def _checks_item(head: str, quote: str = "") -> str:
    """A list item of the vocabulary: ``head`` and utest.h's checks, each in
    ``quote``, wrapped as the vocabulary's list items are."""
    names = [f"{quote}{name}{quote}" for name in _CHECKS]
    return textwrap.fill(head + listed(names, "or"), width=72,
                         initial_indent="  - ", subsequent_indent="    ")


#: C's words for its tests (``langadapter.vocabulary``).
_VOCABULARY: Dict[str, str] = {
    "valid_source": "valid C",
    "real_source": "real C",
    "test_declaration": "``UTEST(suite, name) { ... }``",
    "test_declaration_full": "``UTEST(suite, name) { ... }`` test",
    "collection_rule": (
        "utest.h runs every ``UTEST``, ``UTEST_F`` and ``UTEST_I`` in the test "
        "file. The file ends with ``UTEST_MAIN()``."),
    "fixture_term": "``UTEST_F`` fixtures with ``UTEST_F_SETUP`` and ``UTEST_F_TEARDOWN``",
    "parametrize_term": "``UTEST_I`` indexed tests",
    "single_file_rule": (
        "Produce one ``test_<target_stem>.c`` file that includes ``utest.h`` and the "
        "target's header. Do not create helper files, build files or a ``main`` of "
        "your own."),
    "test_file_form": (
        "A test file holds, in this order:\n"
        "  - includes of ``utest.h`` and the target's header\n"
        "  - tests as ``UTEST(suite, name) { ... }``\n"
        "  - a fixture test as ``UTEST_F(fixture, name)``, which needs\n"
        "    ``struct fixture``, ``UTEST_F_SETUP(fixture)`` and\n"
        "    ``UTEST_F_TEARDOWN(fixture)``, and reads its fixture through\n"
        "    ``utest_fixture``\n"
        + _checks_item("checks inside a test only: ``ASSERT_`` or ``EXPECT_`` with ", "``")
        + "\n  - ``UTEST_MAIN()`` as the last line, which is utest.h's ``main``, so\n"
        "    the file has no ``main`` of its own"),
    "test_file": "``test_<target_stem>.c``",
    "import_rule": "includes the target's header and calls its functions",
    "load_error": "compile error",
    "exception_assertion": "a check of the returned error code",
    "timeout_marker": "",
    "parse_error_hints": (
        "unbalanced braces or parentheses, a missing semicolon, an unterminated "
        "string or comment, accidental code-fence remnants"),
    "test_dependencies": (
        "Test toolkit (always available):\n"
        + _checks_item("utest.h: UTEST, UTEST_F and UTEST_I, and inside a test only ASSERT_ "
                       "or EXPECT_ with ")
        + "\n  - the C standard library\n"
        "The target's header and the project's own headers are also available. "
        "Other libraries are available only if the project's build links them."),
    "validity_rule": (
        "Allowed: calling the target's functions and reading their results and "
        "its documented globals. Forbidden: reading the target's source file, "
        "including the target's .c file, or redefining or wrapping its functions "
        "with macros."),
    "load_error_patterns": (
        "  - implicit declaration of ASSERT_NOT_NULL or another check -> utest.h\n"
        "    has only the checks the toolkit lists: use ASSERT_TRUE(p != NULL).\n"
        "  - 'utest_result' undeclared -> a check stands outside a test: make the\n"
        "    helper return a value and check it in the test.\n"
        "  - implicit declaration / undeclared identifier -> include the target's\n"
        "    header, or declare the function as the header does.\n"
        "  - undefined reference -> the function is static or not in the target:\n"
        "    test it through the functions the target exports.\n"
        "  - conflicting types -> do not redeclare the target's functions."),
    "stands_alone_advice": (
        "  1. A test must leave the target's global and static variables as it\n"
        "     found them: set back each one it changes, or change it only in a\n"
        "     ``UTEST_F`` fixture whose ``UTEST_F_TEARDOWN`` restores it.\n"
        "  2. A test must restore the allocation functions it installs in the\n"
        "     target, such as a counting ``malloc``.\n"
        "  3. A test must restore every process limit and signal handler it\n"
        "     changes: save the limit with ``getrlimit`` before it calls\n"
        "     ``setrlimit``, and set it back before the test ends.\n"
        "  4. A test must remove the files it creates and restore the\n"
        "     environment variables and the working directory it changes."),
    "coverage_counts_inconsistent_cause": (
        "Counts that cannot be read usually mean that a test corrupts memory or relies on "
        "undefined behavior."),
    "coverage_counts_inconsistent_fix": (
        "Change the test so that it uses only valid values and memory, or remove it."),
}

#: The entries in which Python tests of a C extension differ from Python's.
#: gcov counts the target's coverage for them too, but a Python test seldom
#: corrupts memory, so the entries on counts that cannot be read are empty.
_EXTENSION_VOCABULARY: Dict[str, str] = {
    "import_rule": "imports the compiled extension module built from the target C file",
    "validity_rule": (
        "Allowed: the module's runtime behaviour and documented metadata "
        "(attributes, __name__, __doc__). Forbidden: reading or analysing the "
        "target's C source file or the compiled module's binary, or loading "
        "another build of it."),
    "coverage_counts_inconsistent_cause": "",
    "coverage_counts_inconsistent_fix": "",
}


@dataclass(frozen=True)
class CRunOptions:
    """The C adapter's runner options: the target the tests are linked with,
    relative to the project root (``request_options`` sets it from a run's
    target)."""

    target: Optional[str] = None
    #: The Python adapter's runner options, for Python tests.
    inner: Any = None


def _address_space_bytes(text: str) -> Optional[int]:
    """``c_address_space_limit``'s bytes: a number above 0, with K, M or G for
    powers of 1024, or None for an empty or unreadable value."""
    match = _SIZE.fullmatch((text or "").strip())
    if match is None or int(match.group(1)) == 0:
        return None
    return int(match.group(1)) * _UNITS[match.group(2).lower()]


def _discover_test_kind(root: Path, selected: str = "") -> str:
    """The kind of a project's tests (``c_tests``): the setting's value, else
    ``python`` for a target built into a Python extension (a ``setup.py``
    that declares one), else ``c``. ``programs``, a project's own test
    programs (``_programs``), and ``ctest``, a CMake project's tests as
    ctest runs them (``_ctest``), are chosen by the setting alone."""
    if selected in _TEST_KINDS:
        return selected
    return "python" if _build.builds_extensions(root) else "c"


class CAdapter(la.LangAdapter):
    """C projects: the project's GCC build replayed for the target, its tests
    of the kind ``c_tests`` names (utest.h tests, Python tests of an
    extension, the project's own programs or ctest's tests), gcov coverage."""

    language = "c"
    file_extensions = (".c",)
    display_name = "C"
    #: The program that builds and so runs the tests: the project's GCC.
    runtime = "gcc"
    test_file_name = "test_{stem}.c"
    test_framework = "utest.h"
    code_fence = "c"
    #: The comment a merge's separator line starts with.
    source_merge_separator = "//"
    test_evidence_granularity = (("run_suite", ("aggregate", "per_test")),
                                 ("per_test_evidence", ("per_test",)))
    native_mutation_engines = (_mull.ENGINE,)
    mutation_evidence_granularity = ("aggregate", "per_mutant", "per_test")

    project_settings = (
        ProjectSetting("c_tests", "The kind of the project's tests: c (utest.h tests), python "
                       "(Python tests of a target built into a Python extension), programs "
                       "(the project's own test programs, each judged by its exit status) or "
                       "ctest (a CMake project's tests as ctest runs them).", "choice",
                       choices=_TEST_KINDS),
        ProjectSetting("c_compiler", "The GCC that builds the project's C code and its tests.",
                       "executable", alias="CC"),
        ProjectSetting("c_build_kind", "How the project builds: make (its Makefile, run in a "
                       "copy of the project), cmake (its CMakeLists.txt, built outside its "
                       "tree), configure, setuptools, or flags (one file, with c_flags and "
                       "c_link).", "choice", choices=_build.KINDS),
        ProjectSetting("c_build", "The command that builds the project in its copy, when not "
                       "plain make. For a CMake project it holds the options of its configure "
                       "step (-DNAME=VALUE).", "text"),
        ProjectSetting("c_flags", "The compile flags of the target, for a project without a "
                       "build file.", "text"),
        ProjectSetting("c_link", "The objects, libraries and C sources (also patterns, "
                       "such as src/*.c) the target links with, for a project without a "
                       "build file.", "text"),
        ProjectSetting("c_build_cache", "A directory that keeps a configure build for later "
                       "runs, instead of building once per adapter.",
                       "path"),
        ProjectSetting("c_target", "The C file the tests are for, when a request does not "
                       "name it. Load repair and import pruning compile against it.", "path"),
        ProjectSetting("c_mull", "Mull's runner (mull-runner-N), whose IR frontend and clang "
                       "N build the target's mutants.", "executable"),
        ProjectSetting("c_test_prelude", "The headers every test file includes first, as the "
                       "project's own tests include them (such as stdlib.h "
                       "test/alloc-testing.h), each passed as -include.", "text"),
        ProjectSetting("c_ctest_arguments", "Extra arguments for named ctest tests (c_tests "
                       "ctest), given after their own. The entries are separated by "
                       "semicolons, each a test name followed by its arguments, such as "
                       "fuzz_test {seed}. The value of {seed} is 1, since a C run carries no "
                       "session seed.", "text"),
        ProjectSetting("c_test_support", "The sources the project's own test programs build "
                       "with besides the target (c_tests programs), such as test/framework.c "
                       "test/alloc-testing.c. They are the suite's setup files.", "text"),
        ProjectSetting("c_test_environment", "Variables every process of the C adapter's "
                       "runs gets, Python tests of the target's extension among them, as "
                       "NAME=value words such as CK_FORK=no.", "text",
                       check="NAME=value words"),
        ProjectSetting("c_address_space_limit", "The most address space each test process "
                       "may reserve (RLIMIT_AS, set with prlimit), such as 2G or a number "
                       "of bytes. Empty: no limit.", "text", check="a size such as 2G"),
        # Python's own, which choose the Python that runs Python tests
        # (``discover_runtime``), but python_test_environment:
        # c_test_environment gives those tests' processes their variables.
        *(declared for declared in PythonAdapter.project_settings
          if declared.name != "python_test_environment"),
    )
    #: The unit load repair removes.
    collection_unit = ("the utest.h test, fixture block or top-level declaration around a "
                       "line GCC reports an error on")

    def __init__(self, context: la.ProjectContext) -> None:
        super().__init__(context)
        self._lock = threading.RLock()
        self._toolchain_found: Any = None
        self._mull_found: Any = None
        self._data_objects: Dict[str, Any] = {}
        self._builds_dir: Optional[Path] = None
        self._builds: Dict[str, Any] = {}
        self._python_adapter = None
        self._test_kind = _discover_test_kind(Path(context.root),
                                             self._setting("c_tests"))
        self._source_harnesses: Dict[str, Any] = {}
        if self._extension_mode:
            # The facts of the tests are Python's (the class keeps C's).
            self.display_name = "Python"
            self.runtime = "python"
            self.test_file_name = "test_{stem}.py"
            self.test_framework = "pytest"
            self.code_fence = "python"
            self.source_merge_separator = getattr(self._python(), "source_merge_separator",
                                                  None)
            # Python's runs as delivered take the session seed, which the
            # extension's runs pass on to them (``_inner_request``).
            self.seeds_delivered_runs = self._python().seeds_delivered_runs
            self.collection_unit = getattr(self._python(), "collection_unit", "")
            # The source operations of the tests are Python's, on this
            # instance only: LangAdapter finds a hook on the instance.
            for name in _PYTHON_SOURCE_HOOKS:
                hook = getattr(self._python(), name, None)
                if callable(hook):
                    setattr(self, name, hook)
            for name in _PYTHON_LOADING_HOOKS:
                setattr(self, name, self._loading_hook(name))
            self.test_files = self._extension_test_files
            # So is a run as delivered, with the target's extension staged.
            self.run_as_delivered = self._extension_delivered
            # Python finds and removes its tests by its own operations above.
            self.test_units = None

    @property
    def _runner(self):
        """The module that lists and runs a test program's tests: utest.h's,
        a project's own programs' (``c_tests`` ``programs``), or ctest's
        (``ctest``)."""
        return {"programs": _programs, "ctest": _ctest}.get(self._test_kind, _utest)

    @property
    def _extension_mode(self) -> bool:
        """Whether the tests are Python tests of the target's extension."""
        return self._test_kind == "python"

    def _python(self):
        """A Python adapter of this context, for the Python tests' facts,
        words and source operations."""
        with self._lock:
            if self._python_adapter is None:
                self._python_adapter = PythonAdapter(self.context)
            return self._python_adapter

    # Facts and words.

    def test_vocabulary(self) -> Dict[str, str]:
        """C's words for its tests, and for Python tests Python's words with
        the import and validity rules of an extension. A project's own programs
        and ctest's tests
        are measured, not generated: for those kinds the words are refused
        (InvalidRequest on ``c_tests``), so generation stops at its start."""
        if self._extension_mode:
            return {**dict(self._python().test_vocabulary()), **_EXTENSION_VOCABULARY}
        if self._test_kind in ("programs", "ctest"):
            raise InvalidRequest("c_tests", f"c_tests {self._test_kind} measures the "
                                 "project's own suite, and generated tests are utest.h files, "
                                 "which need c_tests c")
        return dict(_VOCABULARY)

    def language_facts(self) -> la.LanguageFacts:
        """The facts of C tests: utest.h, the ``c`` fence and GNU C17, the
        language every test file compiles as. For Python tests, Python's, with
        ``python`` as the tests' language and ``c`` as the target's fence."""
        if self._extension_mode:
            from dataclasses import replace
            return replace(self._python().language_facts(), language=CAdapter.display_name,
                           test_language="python", target_code_fence=CAdapter.code_fence)
        found = self._toolchain()
        version = found.version if isinstance(found, _toolchain.Toolchain) else None
        return la.LanguageFacts(
            language=self.display_name, test_framework=self.test_framework,
            code_fence=self.code_fence,
            runtime_version=found.version_line if version else None,
            test_file_patterns=_TEST_FILE_PATTERNS,
            test_function_pattern="UTEST(suite, name) { ... }", test_class_pattern=None,
            timeout_marker=None,
            language_level=f"GNU C17 (GCC {version})" if version else "GNU C17")

    @classmethod
    def import_statement(cls, target: str, root: str) -> Optional[str]:
        """The import of the target's module when ``setup.py`` builds the
        target into an extension, else ``#include "<stem>.h"`` when a header
        of its stem lies beside it, else None."""
        module = _build.declared_module(Path(root), target)
        if module is not None:
            package, _, name = module.rpartition(".")
            return f"from {package} import {name}" if package else f"import {name}"
        header = (Path(root) / target).with_suffix(".h")
        return f'#include "{header.name}"' if header.is_file() else None

    def test_id_text(self, test) -> str:
        """``file::set.name``, with ``/index`` for an indexed test. Python
        tests have Python's dotted id."""
        if self._extension_mode:
            return self._python().test_id_text(test)
        index = f"/{test.params}" if test.params is not None else ""
        return f"{test.file}::{test.qualname}{index}"

    # The runtime and the settings.

    @classmethod
    def discover_runtime(cls, root, values, *, container=False, environment=None,
                         resolve_path=None) -> RuntimeChoice:
        """The project's GCC: ``c_compiler``, else ``CC``, else ``cc`` or ``gcc``
        on PATH. For Python tests, the project's Python as the Python adapter
        finds it."""
        if _discover_test_kind(Path(root), str(values.get("c_tests") or "").strip()) == "python":
            return PythonAdapter.discover_runtime(root, values, container=container,
                                                  environment=environment,
                                                  resolve_path=resolve_path)
        found = _toolchain.find_compiler(str(values.get("c_compiler") or ""), environment)
        return RuntimeChoice(la.Interpreter(path=found) if found else None)

    @classmethod
    def setup_questions(cls, project_dir, probe, *, consumer="the caller"):
        """C's own settings, each asked as by default. For a project whose
        tests are Python's (a target built into an extension), Python's own
        questions follow C's. They choose the Python that runs those tests.
        The Python settings C declares for that case are Python's to ask."""
        own = ({declared.name for declared in cls.project_settings}
               - {declared.name for declared in PythonAdapter.project_settings})
        for item in super().setup_questions(project_dir, probe, consumer=consumer):
            if not isinstance(item, SetupQuestion) or item.setting in own:
                yield item
        if _discover_test_kind(Path(project_dir)) == "python":
            yield from PythonAdapter.setup_questions(project_dir, probe, consumer=consumer)

    @classmethod
    def setup_problem(cls, setting, value, project_dir, probe):
        """Python's check for the Python settings C declares, C's own by
        their kind (the default), and c_address_space_limit's size."""
        if setting == "c_address_space_limit" and value and _address_space_bytes(value) is None:
            return _ADDRESS_SPACE_FORMAT
        if setting in {declared.name for declared in PythonAdapter.project_settings}:
            return PythonAdapter.setup_problem(setting, value, project_dir, probe)
        return super().setup_problem(setting, value, project_dir, probe)

    @classmethod
    def context_environment(cls, values, *, container=False) -> Dict[str, str]:
        """The C settings as the context variables the adapter reads, with
        Python's import root for Python tests of the target's extension, and
        the variables ``c_test_environment`` gives its runs' processes, Python
        tests of the extension among them. The import root, the setting
        ``python_import_root``, follows Python's rule: a directory outside the
        project is refused, and . or none, the project itself, stays a choice
        apart from no setting. Without a setting the adapter takes the directory setup.py's
        extensions are built in below their package, else src when a package
        lies below it, else the project itself. C does not declare Python's
        ``python_test_environment``, which is the Python adapter's alone."""
        from langadapter.adapter import environment_assignments
        from langadapter.python._project_files import checked_import_root

        found = {}
        for key, variable in _SETTING_VARIABLES:
            value = str(values.get(key) or "").strip()
            if value:
                found[variable] = value
        stated = str(values.get("python_import_root") or "").strip()
        root = checked_import_root(stated)
        if stated:
            found[_IMPORT_ROOT_VARIABLE] = root or "."
        found.update(environment_assignments(values.get("c_test_environment"),
                                             "c_test_environment"))
        return found

    def _setting(self, key: str) -> str:
        variable = dict(_SETTING_VARIABLES)[key]
        return str(self.context.environment.get(variable, "") or "").strip()

    def _import_root(self) -> str:
        """The directory below the project from which Python tests import the
        project's packages: ``python_import_root`` (. and none name the
        project itself), else the directory setup.py's extensions are built in
        below their package (markupsafe._speedups from
        src/markupsafe/_speedups.c: src), else src when a package lies below
        it, else the project itself ("")."""
        stated = str(self.context.environment.get(_IMPORT_ROOT_VARIABLE, "") or "").strip()
        if stated:
            return "" if stated in (".", "none") else stated.strip("/")
        root = Path(self.context.root)
        implied = set()
        for module, source in _build.extension_modules(root).items():
            package = tuple(module.split(".")[:-1])
            dir_parts = PurePosixPath(source).parent.parts
            if package and dir_parts[len(dir_parts) - len(package):] == package:
                implied.add("/".join(dir_parts[:len(dir_parts) - len(package)]))
        if len(implied) == 1:
            return implied.pop()
        below = root / "src"
        if any(below.glob("*/__init__.py")) or any(below.glob("*/*/__init__.py")):
            return "src"
        return ""

    def request_options(self, purpose: str, *, target: Optional[str] = None,
                        traceback: str = "short", stop_at_first_failure: bool = False,
                        record_leaks: bool = False, session_seed: Optional[int] = None,
                        reverse_order: bool = False,
                        leave_out_timing_plugins: bool = False) -> Dict[str, Any]:
        """The run's target as ``CRunOptions``: the tests are linked with it.
        A target inside the project is named by its path relative to the
        project, so the options hold in a copy of it (a consumer's prepared
        copy). For Python tests, the Python adapter's fields of the run, its
        runner options carried inside. C tests have no timing plugins to
        leave out."""
        super().request_options(purpose)
        if target and Path(target).is_absolute():
            root = Path(self.context.root).resolve()
            path = Path(target).resolve()
            if root in path.parents:
                target = path.relative_to(root).as_posix()
        if self._extension_mode:
            fields = dict(self._python().request_options(
                purpose, target=None, traceback=traceback,
                stop_at_first_failure=stop_at_first_failure, record_leaks=record_leaks,
                session_seed=session_seed, reverse_order=reverse_order,
                leave_out_timing_plugins=leave_out_timing_plugins))
            fields["runner_options"] = CRunOptions(target=target,
                                                   inner=fields.get("runner_options"))
            return fields
        return {"runner_options": CRunOptions(target=target)} if target else {}

    # Commands.

    def _executor(self):
        return executor_for(self.context)

    def _includes(self, local: str) -> str:
        """What a repetition's time covers: ``local`` on this machine, its
        text in a container (``_timing.CONTAINER_INCLUDES``) there."""
        if isinstance(self._executor(), LocalExecutor):
            return local
        return _timing.CONTAINER_INCLUDES[local]

    def _environment(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        """A command's variables: the context's over this process's (unless
        the context replaces them), then ``extra``."""
        values = dict(os.environ) if self.context.environment.inherit else {}
        values.update(self.context.environment)
        values.update(extra or {})
        return values

    def _tool_environment(self, extra: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
        """The variables of a compiler, linker, gcov or Mull run whose text the
        adapter reads: the command's, with GCC's English messages
        (``LC_MESSAGES=C``) and without ``LC_ALL``, which would override it."""
        values = self._environment({**(extra or {}), "LC_MESSAGES": "C"})
        values.pop("LC_ALL", None)
        return values

    def _run(self, argv: Sequence[str], *, cwd: Optional[str] = None,
             env: Optional[Mapping[str, str]] = None, timeout: Optional[float] = None):
        return self._executor().run(list(argv), cwd=cwd,
                                    env=dict(env) if env is not None else self._environment(),
                                    timeout=timeout)

    def _toolchain(self):
        """The context's toolchain, probed once, or the problem."""
        with self._lock:
            if self._toolchain_found is None and not sys.platform.startswith("linux"):
                self._toolchain_found = _toolchain.ToolchainProblem(_LINUX_ONLY)
            elif self._toolchain_found is None:
                if self._extension_mode:
                    compiler = _toolchain.find_compiler(self._setting("c_compiler"),
                                                        self._environment())
                else:
                    interpreter = self.context.interpreter
                    compiler = interpreter.path if interpreter is not None else None
                self._toolchain_found = _toolchain.probe(
                    compiler, lambda argv: self._run(argv, env=self._tool_environment(),
                                                     timeout=60))
            return self._toolchain_found

    def _mull_tool(self):
        """The context's Mull, probed once, or the problem."""
        with self._lock:
            if self._mull_found is None:
                self._mull_found = _mull.probe(self._setting("c_mull"),
                                               lambda argv: self._run(
                                                   argv, env=self._tool_environment(),
                                                   timeout=60),
                                               self._environment())
            return self._mull_found

    def execution_setup_issue(self) -> str:
        """A system other than Linux, a module of LangAdapter's extra for C
        that is not installed, a ``c_address_space_limit`` that does not
        parse or finds no prlimit on PATH, or utest.h tests without stdbuf on
        PATH, which a suite run and a run as delivered start each test
        program under, or ""."""
        if not sys.platform.startswith("linux"):
            return _LINUX_ONLY
        missing = [name for module, name in _EXTRA_MODULES.items()
                   if importlib.util.find_spec(module) is None]
        if missing:
            return ("the C adapter needs LangAdapter's extra for C, which installs "
                    f"{', '.join(missing)}: pip install \"langadapter[c]\"")
        limit = self._setting("c_address_space_limit")
        if limit and _address_space_bytes(limit) is None:
            return f"c_address_space_limit {limit!r}: {_ADDRESS_SPACE_FORMAT}"
        if limit and _toolchain.which("prlimit", self._environment()) is None:
            return "c_address_space_limit needs prlimit (util-linux) on PATH"
        if (self._test_kind == "c"
                and _toolchain.which(_utest.LINE_BUFFERED[0], self._environment()) is None):
            return ("a suite run or a run as delivered of C tests needs stdbuf (coreutils) on "
                    "PATH, which keeps their output line-buffered")
        return ""

    def _address_space_limit(self) -> Optional[int]:
        """The bytes of address space each test process may reserve, or None
        for no limit (``c_address_space_limit``)."""
        return _address_space_bytes(self._setting("c_address_space_limit"))

    def _run_test(self, argv: Sequence[str], *, cwd: Optional[str] = None,
                  env: Optional[Mapping[str, str]] = None, timeout: Optional[float] = None,
                  whole: bool = False):
        """``_run`` for a test process: under the address space limit
        ``c_address_space_limit`` sets (prlimit), else as it is. On this
        machine its output is kept as ``_utest.KeptOutput`` keeps it, unless
        ``whole`` (output its reader reads whole). Elsewhere the executor
        captures it whole."""
        limit = self._address_space_limit()
        argv = [*(["prlimit", f"--as={limit}", "--"] if limit else []), *map(str, argv)]
        if whole or not isinstance(self._executor(), LocalExecutor):
            return self._run(argv, cwd=cwd, env=env, timeout=timeout)
        with operation(self.context):
            done = _process_tree.run(
                argv, timeout=timeout, cwd=cwd,
                env=dict(env) if env is not None else self._environment(),
                into=(_utest.KeptOutput(), _utest.KeptOutput()))
        done.stdout, done.stderr = _as_text(done.stdout), _as_text(done.stderr)
        return done

    def _gate(self) -> Optional[Tuple[la.Outcome, str]]:
        """Why nothing can be built or run here, or None."""
        gate = self._execution_gate()
        if gate is not None:
            return gate
        found = self._toolchain()
        if isinstance(found, _toolchain.ToolchainProblem):
            return la.Outcome.MISSING_SETUP, found.reason
        return None

    def _toolchain_identity(self) -> la.ToolchainIdentity:
        found = self._toolchain()
        if not isinstance(found, _toolchain.Toolchain):
            return la.ToolchainIdentity.of(Version("gcc", (), "unknown", "unknown"))
        numbers = tuple(int(part) for part in found.version.split(".") if part.isdigit())
        tools = (("gcov", found.version),) if self._test_kind in ("programs", "ctest") \
            else (("gcov", found.version), ("utest.h", _UTEST_COMMIT))
        limit = self._address_space_limit()
        if limit:
            tools += (("address_space_limit", str(limit)),)
        return la.ToolchainIdentity.of(Version("gcc", numbers, found.version, "probe"), tools,
                                       structural_parser=_syntax.PARSER)

    def _mutation_toolchain_identity(self, mull: _mull.Mull) -> la.ToolchainIdentity:
        """The toolchain identity of a mutation run: the project's, with
        Mull's version, the LLVM release it was built with and its clang's."""
        identity = self._toolchain_identity()
        return la.ToolchainIdentity.recorded(
            identity.implementation, identity.version,
            (*identity.tools, ("mull", mull.version), ("mull_llvm", mull.llvm),
             ("clang", mull.clang_version)),
            structural_parser=identity.structural_parser)

    # The build.

    def _build_dir(self) -> Path:
        """This adapter's build directory, made once and removed with it."""
        with self._lock:
            if self._builds_dir is None:
                builds_dir = Path(tempfile.mkdtemp(prefix="langadapter-c-"))
                self._builds_dir = builds_dir
                weakref.finalize(self, shutil.rmtree, str(builds_dir), True)
            return self._builds_dir

    def _relative_target(self, target: str) -> str:
        """``target`` relative to the project root, as POSIX text. An absolute
        target outside the project raises InvalidRequest."""
        path = Path(target)
        root = Path(self.context.root)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(root.resolve())
            except ValueError:
                raise InvalidRequest("target", f"not a file of the project: {target}") from None
        return path.as_posix()

    def _target_build(self, target: str):
        """How the project's build makes ``target``: recorded once per target,
        a failure kept with it, but a build that timed out, which may finish
        under less load, made again."""
        relative = self._relative_target(target)
        with self._lock:
            held = self._builds.get(relative)
            if held is not None and not (isinstance(held, _build.BuildProblem)
                                         and held.outcome is la.Outcome.TIMED_OUT):
                return held
            found = self._toolchain()
            if isinstance(found, _toolchain.ToolchainProblem):
                self._builds[relative] = _build.BuildProblem(la.Outcome.MISSING_SETUP,
                                                             found.reason)
                return self._builds[relative]
            root = Path(self.context.root)
            kind = _build.discover_kind(root, self._setting("c_build_kind"))
            if kind == "setuptools" and not self._extension_mode:
                self._builds[relative] = _build.BuildProblem(la.Outcome.MISSING_SETUP, (
                    "C tests need a build kind other than setuptools, which builds a "
                    "Python extension for Python tests: c_build_kind names it (make, "
                    "cmake, configure or flags)"))
                return self._builds[relative]
            scratch_dir = self._kept_build(root, kind, found.compiler)
            kept = scratch_dir is not None
            if scratch_dir is None:
                scratch_dir = Path(tempfile.mkdtemp(prefix="build-", dir=self._build_dir()))
            interpreter = self.context.interpreter
            result = _build.record_build(_build.BuildRequest(
                root, relative, found.compiler, scratch_dir, self._run, kind=kind,
                env=self._environment(),
                build_command=self._setting("c_build"), flags=self._setting("c_flags"),
                link=self._setting("c_link"),
                python=interpreter.path if interpreter is not None else None, kept=kept,
                timeout_s=_BUILD_TIMEOUT_S))
            self._builds[relative] = result
            return result

    def _kept_build(self, root: Path, kind: str, compiler: str) -> Optional[Path]:
        """The directory of a configure build kept in ``c_build_cache``, named by
        the tree's build inputs (``source_fingerprint``), its build command and
        its compiler, so every copy of the tree (a consumer's prepared copies)
        uses one build. It is None without the setting and for a build of
        another kind. A build there that did not finish is completed by the
        next run."""
        cache = self._setting("c_build_cache")
        if not cache or kind != "configure":
            return None
        import hashlib
        import json

        key = hashlib.sha256(json.dumps([_build.source_fingerprint(root), self._setting("c_build"),
                                         compiler]).encode("utf-8")).hexdigest()[:16]
        scratch_dir = Path(cache) / f"build-{key}"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        return scratch_dir

    def project(self) -> ProjectResolution:
        """Whether the project builds: its build kind is known and the
        compiler runs. The build itself is recorded on the first run."""
        gate = self._gate()
        if gate is not None:
            return ProjectResolution(*gate)
        return ProjectResolution(la.Outcome.COMPLETED)

    def _target_of(self, request) -> Optional[str]:
        """The target a request's tests are linked with: its coverage target,
        else the runner options' target, else ``c_target``, else the one
        source file whose stem the test file names (``test_<stem>.c``), else
        the one the test files name by what they import or include."""
        target = getattr(request, "coverage_target", None) or getattr(request, "target", None)
        options = request.runner_options
        if not target and isinstance(options, CRunOptions):
            target = options.target
        if target:
            return target
        if self._setting("c_target"):
            return self._setting("c_target")
        return self._found_target(request.suite)

    def _found_target(self, suite) -> Optional[str]:
        """The one source file whose stem ``suite``'s test files name
        (``test_<stem>.c``), else the one they name by what they import or
        include."""
        stems = {Path(name).stem.removeprefix("test_") for name in suite.test_files
                 if Path(name).suffix in (".c", ".py")}
        found = self._sources_named(stems)
        if len(found) == 1:
            return found[0]
        texts = []
        for name in suite.test_files:
            path = Path(name) if Path(name).is_absolute() else Path(suite.root) / name
            try:
                texts.append(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                return None
        return self._named_target("\n".join(texts)) if texts else None

    # Running tests.

    def run_suite(self, request: SuiteRunRequest) -> SuiteRun:
        """Run the suite against a build of the target, with the target's
        coverage when ``coverage_target`` names it. A utest.h test file's
        program runs whole, as delivered, and starts again without a test
        that crashed. A project's own programs and ctest's tests run each
        test in its own process. Python tests run in one pytest process
        against the target's extension."""
        request.validate()
        started = time.monotonic()
        if self._extension_mode:
            return self._extension_suite(request, started)
        identity = self._identity(request, coverage=bool(request.coverage_target))
        refusal = self._refusal(request)
        if refusal is not None:
            return self._ended(*refusal, identity)
        target = self._target_of(request)
        if target is None:
            return self._ended(la.Outcome.UNSUPPORTED, _NO_TARGET, identity)
        coverage = bool(request.coverage_target)
        deadline = started + request.timeout_s
        with work_dir_tempdir(self.context, self._executor(), prefix="langadapter_c_") as scratch:
            scratch = Path(scratch)
            prepared = self._prepare(target, request.suite, scratch, coverage=coverage,
                                     deadline=deadline)
            if isinstance(prepared, _Failure):
                return self._ended(prepared.outcome, prepared.reason, identity,
                                   output=prepared.shown(), detail=prepared.detail,
                                   diagnostics=prepared.diagnostics)
            prefix = scratch / "counters"
            env = (self._environment(self._counter_variables(prefix, prepared.target_object))
                   if coverage else None)
            tests_started = time.monotonic()
            ran = self._suite_programs(prepared, env, deadline, scratch, coverage)
            if isinstance(ran, _Failure):
                return self._ended(ran.outcome, ran.reason, identity, output=ran.shown(),
                                   detail=ran.detail)
            runs, timed_out, counters, whole = ran
            tests_s = time.monotonic() - tests_started
            observed, inconsistent = (
                self._suite_coverage(prepared, counters, scratch, target)
                if coverage and not timed_out and counters is not None else (None, None))
            refused = self._refused_tests(prepared, whole, scratch, inconsistent)
        run = self._suite_run(runs, timed_out, observed, identity, tests_s, request.timeout_s)
        if whole is not None:
            run = _with_programs(run, whole)
        if request.timeout_journal:
            from dataclasses import replace

            # The line-buffered output names the test a timeout or crash ended.
            run = replace(run, side_effects=replace(run.side_effects, timeout_journal=True))
        return run if inconsistent is None else _counts_inconsistent(run, inconsistent, refused)

    def _refused_tests(self, prepared: "_Prepared", whole: Optional["_WholeRuns"],
                       scratch: Path, inconsistent) -> Tuple[Tuple[la.TestId, str], ...]:
        """After gcovr refused the counts of a suite run of whole programs
        (``inconsistent``): each test whose own counts it refuses, with its
        message. Each file's counts are read alone, and each test of a file
        whose counts it refuses runs alone on the counters build
        (``_alone``). The search takes only the run's remaining time, checked
        before each read, and changes nothing of the run: when that time is
        spent before it ends, or one of its reads fails in another way, it
        names no test (``_search_read``). A program every one of whose tests
        it finds names none (``_gcov.named``)."""
        if inconsistent is None or whole is None:
            return ()
        stops: List[_gcov.Stop] = []
        listed: Dict[str, Tuple[str, ...]] = {}
        for index, (file, counters) in enumerate(whole.counters.items()):
            search = scratch / "search" / str(index)
            read = self._search_read(prepared, whole.deadline, counters, search / "file")
            if read is None:
                return ()
            if not read:
                continue
            kept = {one.name: one for ran, one in whole.runs if ran == file}
            probe = _WholeRuns(coverage=True, deadline=whole.deadline)
            alone = self._alone(prepared, file, list(kept), kept, probe)
            if probe.timed_out:
                return ()
            listed[file] = tuple(alone)
            for number, (name, (_, _, own)) in enumerate(alone.items()):
                read = self._search_read(prepared, whole.deadline, own,
                                         search / "tests" / str(number))
                if read is None:
                    return ()
                if read:
                    stops.append(_gcov.Stop(file, name, read, counts=True))
        by_name = {(stop.file, stop.name): stop.what for stop in stops}
        return tuple((self._test_id(file, name), by_name[(file, name)])
                     for file, name in _gcov.named(stops, listed))

    def _search_read(self, prepared: "_Prepared", deadline: float, counters: Path,
                     counts_dir: Path) -> Optional[str]:
        """One read of the search for refused counts, of the counters under
        ``counters`` in the empty ``counts_dir``: gcovr's message when it
        refuses them, "" when it reads them or finds none, and None when the
        run's time is spent or the read fails in another way."""
        if time.monotonic() >= deadline:
            return None
        try:
            self._merged_counts(prepared.build, prepared.target_object, [counters], counts_dir)
        except _gcov.CountsInconsistent as refused:
            return str(refused)
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            return None
        return ""

    def benchmark(self, request: BenchmarkRequest) -> Benchmark:
        """The times of repeated runs of the suite after discarded warm-up
        runs, the target and the tests built once before them: each test
        program runs once a repetition, all its tests in one process.
        ctest's tests each run as a suite run runs them. Python tests are
        timed by the Python adapter, with the target's extension staged."""
        request.validate()
        started = time.monotonic()
        identity = self._identity(request, coverage=False)
        if self._extension_mode:
            return self._extension_benchmark(request, identity, started)
        ctest = self._test_kind == "ctest"
        includes = self._includes(_timing.CTEST_INCLUDES if ctest else _timing.INCLUDES)
        refusal = self._refusal(request)
        if refusal is not None:
            return _timing.unmeasured(request, identity, *refusal, includes=includes)
        if request.cpus is not None and _toolchain.which("taskset", self._environment()) is None:
            return _timing.unmeasured(request, identity, la.Outcome.UNSUPPORTED,
                                      "a processor set needs taskset, which this machine "
                                      "does not have", includes=includes)
        target = self._target_of(request)
        if target is None:
            return _timing.unmeasured(request, identity, la.Outcome.UNSUPPORTED, _NO_TARGET,
                                      includes=includes)
        with work_dir_tempdir(self.context, self._executor(), prefix="langadapter_c_") as scratch:
            scratch = Path(scratch)
            prepared = self._prepare(target, request.suite, scratch, coverage=False,
                                     deadline=started + request.timeout_s)
            if isinstance(prepared, _Failure):
                return _timing.unmeasured(request, identity, prepared.outcome, prepared.text(),
                                          includes=includes)
            # ctest's "program" is the directory of its relinked programs (``_link``).
            steps = ([step for programs_dir in prepared.programs.values()
                      for step in _timing.ctest_steps(programs_dir, self._environment())]
                     if ctest else _timing.program_steps(
                         prepared.programs, str(scratch),
                         own_programs=self._test_kind == "programs"))
            return _timing.measure(request, steps, self._run_test, cwd=str(scratch),
                                   identity=identity, includes=includes)

    def run_as_delivered(self, request: SuiteRunRequest, *,
                         sources: Optional[Mapping[str, str]] = None) -> SuiteRun:
        """Each test file alone, as a user who copied it runs it: built
        without counters, its program run once by utest.h's own main, all its
        tests in one process in their order of definition.
        A crash or the timeout ends the program in the test it ran, and the
        tests after it have no outcome. A project's own program, or ctest's
        test, is one program run whole, so it runs as in a suite run.
        ``sources`` replaces a test file's text, compiled from a copy that
        includes as the file does. Python tests of an extension: Python's run
        as delivered."""
        request.validate()
        started = time.monotonic()
        identity = self._identity(request, coverage=False)
        refusal = self._refusal(request)
        if refusal is not None:
            return self._ended(*refusal, identity)
        if sources and self._test_kind == "ctest":
            return self._ended(la.Outcome.UNSUPPORTED, "ctest's tests are its CMake project's: "
                               "a test file's replacement text is not run", identity)
        target = self._target_of(request)
        if target is None:
            return self._ended(la.Outcome.UNSUPPORTED, _NO_TARGET, identity)
        deadline = started + request.timeout_s
        with work_dir_tempdir(self.context, self._executor(), prefix="langadapter_c_") as scratch:
            scratch = Path(scratch)
            prepared = self._prepare(target, request.suite, scratch, coverage=False,
                                     deadline=deadline, sources=sources)
            if isinstance(prepared, _Failure):
                return self._ended(prepared.outcome, prepared.reason, identity,
                                   output=prepared.shown(), detail=prepared.detail,
                                   diagnostics=prepared.diagnostics)
            tests_started = time.monotonic()
            if self._test_kind != "c":
                runs, timed_out = self._run_programs(prepared.programs, None, deadline, scratch)
                return self._suite_run(runs, timed_out, None, identity,
                                       time.monotonic() - tests_started, request.timeout_s)
            runs: List[Tuple[str, _utest.TestRun]] = []
            timed_out = False
            for file, program in prepared.programs.items():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                whole = _utest.run_program(program, self._run_test, cwd=str(scratch),
                                           env=self._environment(), timeout_s=remaining)
                if whole.problem:
                    return self._ended(la.Outcome.COLLECTION_FAILED,
                                       f"the test program of {file} ended before its first "
                                       f"test: {whole.problem}", identity,
                                       wall=time.monotonic() - tests_started, output=whole.output)
                runs += [(file, one) for one in whole.tests]
                if whole.timed_out:
                    timed_out = True
                    break
            tests_s = time.monotonic() - tests_started
        return self._suite_run(runs, timed_out, None, identity, tests_s, request.timeout_s)

    def delivered_order_refusal(self, reverse: bool) -> Optional[str]:
        """Why a run as delivered in the reverse order is refused for C tests:
        a test program runs its tests in its own order. Python tests of an
        extension: Python's answer."""
        if self._extension_mode:
            return self._python().delivered_order_refusal(reverse)
        if reverse:
            return ("a C test program runs its tests in its own order; a reverse-order run is "
                    "not offered for C tests")
        return None

    def _refusal(self, request) -> Optional[Tuple[la.Outcome, str]]:
        """Why a run of C tests cannot run: a side effect the adapter does not
        provide, or the gate."""
        for name, field_name in _NOT_PROVIDED:
            # A session seed of 0 asks for one too: only None and False do not.
            value = getattr(request, field_name, None)
            if value is not None and value is not False:
                return la.Outcome.UNSUPPORTED, f"the C adapter does not provide {name}"
        return self._gate()

    def _identity(self, request, *, coverage: bool) -> la.ObservationIdentity:
        target = None
        if coverage:
            name = getattr(request, "coverage_target", None) or getattr(request, "target", None)
            path = Path(name) if Path(name).is_absolute() else Path(self.context.root) / name
            if path.is_file():
                target = la.FileIdentity.of(path, Path(self.context.root))
        return la.ObservationIdentity(target=target, suite=request.suite.identity(),
                                      toolchain=self._toolchain_identity(), instrument=None,
                                      execution=execution_identity(self.context))

    @staticmethod
    def _ended(outcome, reason, identity, *, wall=None, output="", detail=(),
               diagnostics=()) -> SuiteRun:
        return SuiteRun(outcome=outcome, reason=reason, counts=RunCounts(), tests=(),
                        unmatched=(), wall_time_s=wall, output=output, coverage=None,
                        identity=identity, reason_detail=tuple(detail),
                        diagnostics=tuple(diagnostics))

    def _prepare(self, target: str, suite, scratch: Path, *, coverage: bool,
                 deadline: float, sources: Optional[Mapping[str, str]] = None
                 ) -> "_Prepared | _Failure":
        """The target built (with coverage counters when ``coverage``), and
        each test file compiled and linked with it, a file named in
        ``sources`` from its replacement text."""
        build = self._target_build(target)
        if isinstance(build, _build.BuildProblem):
            return _Failure(build.outcome, build.reason, detail=build.detail)
        content = (Path(self.context.root) / target).read_bytes()
        variant = _build.COVERAGE if coverage else _build.PLAIN
        target_object = self._compile_target(build, content, scratch / "target", variant,
                                             deadline)
        if isinstance(target_object, _Failure):
            return target_object
        tests = self._compile_tests(build, suite, scratch / "tests", deadline, sources)
        if isinstance(tests, _Failure):
            return tests
        programs: Dict[str, Path] = {}
        for index, (file, test_object) in enumerate(tests.items()):
            program = scratch / "programs" / f"program-{index}"
            failed = self._link(build, (test_object, target_object), program, coverage,
                                deadline, suite.root)
            if failed is not None:
                return failed
            programs[file] = program
        return _Prepared(build, target_object, programs, dict(tests), suite.root)

    def _compile_target(self, build, content: bytes, compile_dir: Path, variant: str,
                        deadline: float, mull: Optional[Tuple[_mull.Mull, Sequence[str]]] = None
                        ) -> "Path | _Failure":
        """The object of the target's source ``content``, compiled in
        ``compile_dir`` with the recorded command. With ``mull``, Mull and the
        flags its clang refused, it is Mull's variant without those flags,
        configured by the ``mull.yml`` in ``compile_dir``."""
        clang, refused = mull if mull is not None else (None, ())
        stage = build.stage_dir(compile_dir)
        staged = build.staged_source(stage)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(content)
        objects = compile_dir / "obj"
        objects.mkdir(parents=True, exist_ok=True)
        output = objects / "target.o"
        command = build.compile_command(stage, output, variant, mull=clang, refused=refused)
        extra = {"MULL_CONFIG": str(compile_dir / "mull.yml")} if clang is not None else None
        done = self._compile(command, str(stage), deadline, extra)
        if done is None:
            return _Failure(la.Outcome.TIMED_OUT, "the run reached its timeout while compiling "
                            "the target")
        text = (done.stdout or "") + (done.stderr or "")
        if done.returncode != 0:
            flags = "Mull's clang" if clang is not None else "its build's flags"
            return self._step_failure(la.Outcome.INFRASTRUCTURE_FAILURE,
                                      f"the target {build.operand} does not compile with "
                                      f"{flags}", text, {str(stage) + os.sep: ""})
        if clang is not None:
            # What clang said of the flags it ignored, for the run's record.
            (compile_dir / "compile.log").write_text(text, encoding="utf-8")
        return output

    def _compile_tests(self, build, suite, tests_dir: Path, deadline: float,
                       sources: Optional[Mapping[str, str]] = None
                       ) -> "Dict[str, Path] | _Failure":
        """Each test file's object, by the file relative to the suite's root.
        A project's own test programs compile with the target's shared flags
        alone, and each object holds the suite's C setup files (a framework's
        sources), joined by a relocatable link. A file named in ``sources``
        compiles from a copy of its replacement text, whose quoted includes
        find the file's directory."""
        tests_dir.mkdir(parents=True, exist_ok=True)
        root = Path(suite.root)
        if self._test_kind == "ctest":
            return self._ctest_listing(build, suite, tests_dir, deadline)
        programs = self._test_kind == "programs"

        def compile_file(test_file: Path, output: Path, own: bool, shown: str, *,
                         setup: bool = False, quote_from: Optional[Path] = None
                         ) -> "Path | _Failure":
            """The object of a test or setup file, compiled with
            ``_test_compile_flags``. A copy compiled in a file's place is
            ``shown`` by the file's name."""
            command = [build.compile.compiler,
                       *self._test_compile_flags(build, own=own, setup=setup,
                                                 quote_from=quote_from),
                       "-O0", "-g", "-c", str(test_file), "-o", str(output)]
            done = self._compile(command, str(quote_from or test_file.parent), deadline)
            if done is None:
                return _Failure(la.Outcome.TIMED_OUT, "the run reached its timeout while "
                                "compiling the tests")
            if done.returncode != 0:
                text = (done.stdout or "") + (done.stderr or "")
                return self._step_failure(la.Outcome.COLLECTION_FAILED,
                                          f"the test file {shown} does not compile", text,
                                          {str(test_file): shown})
            return output

        setup = self._compile_setup(suite, tests_dir, compile_file) if programs else []
        if isinstance(setup, _Failure):
            return setup
        objects: Dict[str, Path] = {}
        for index, relative in enumerate(suite.test_files):
            test_file = Path(relative) if Path(relative).is_absolute() else root / relative
            shown = self._relative_test(root, test_file)
            compiled_file, quote_from = test_file, None
            if sources and shown in sources:
                compiled_file = tests_dir / f"replaced-{index}" / test_file.name
                compiled_file.parent.mkdir(parents=True, exist_ok=True)
                compiled_file.write_text(sources[shown], encoding="utf-8")
                quote_from = test_file.parent
            compiled = compile_file(compiled_file, tests_dir / f"test-{index}.o", programs, shown,
                                    quote_from=quote_from)
            if isinstance(compiled, _Failure):
                return compiled
            if setup:
                compiled = self._with_setup(build, compiled, setup,
                                            tests_dir / f"test-{index}-with-setup.o", shown, deadline)
                if isinstance(compiled, _Failure):
                    return compiled
            objects[shown] = compiled
        return objects

    def _compile_setup(self, suite, tests_dir: Path, compile_file) -> List[Path] | _Failure:
        """The objects of the suite's C setup files (a framework's sources),
        compiled by ``_compile_tests``' ``compile_file`` with the target's
        shared flags."""
        root = Path(suite.root)
        setup: List[Path] = []
        for index, relative in enumerate(suite.setup_files):
            setup_file = Path(relative) if Path(relative).is_absolute() else root / relative
            if setup_file.suffix != ".c":
                continue
            compiled = compile_file(setup_file, tests_dir / f"setup-{index}.o", True,
                                    self._relative_test(root, setup_file), setup=True)
            if isinstance(compiled, _Failure):
                return compiled
            setup.append(compiled)
        return setup

    def _with_setup(self, build, compiled: Path, setup: Sequence[Path], joined: Path,
                    shown: str, deadline: float) -> Path | _Failure:
        """The test file ``shown``'s object ``compiled`` with the setup files'
        objects, joined by a relocatable link into ``joined``."""
        done = self._compile([build.compile.compiler, "-r", "-nostdlib", str(compiled),
                              *map(str, setup), "-o", str(joined)],
                             str(joined.parent), deadline)
        if done is None:
            return _Failure(la.Outcome.TIMED_OUT, "the run reached its timeout while "
                            "linking the tests")
        if done.returncode != 0:
            text = (done.stdout or "") + (done.stderr or "")
            return self._step_failure(la.Outcome.COLLECTION_FAILED,
                                      f"the test file {shown} "
                                      "does not join with the suite's setup files", text)
        return joined

    def _test_prelude(self) -> List[str]:
        """The project's test prelude (``c_test_prelude``) as compile flags:
        each header as ``-include``, in the order given, one of the project's
        by its absolute path, any other by its name for the include path.
        The project's own tests include such headers first (c-algorithms'
        stdlib.h and alloc-testing.h). With them first, a test's own later
        include is a no-op under the header's guard, whatever its order."""
        root = Path(self.context.root)
        flags: List[str] = []
        for header in shlex.split(self._setting("c_test_prelude")):
            path = root / header
            flags += ["-include", str(path) if path.is_file() else header]
        return flags

    def _test_compile_flags(self, build, *, own: bool = False, setup: bool = False,
                            quote_from: Optional[Path] = None) -> List[str]:
        """The flags a test or setup file compiles with, after the compiler: a
        project's ``own`` file with the target's shared flags, else a utest.h
        test with its header and the standard and declarations a generated
        test is held to. A test file, not a ``setup`` file, includes the
        project's prelude first. A copy compiled in a file's place finds its
        quoted includes in ``quote_from`` too."""
        flags = [] if own else ["-std=gnu17", _repair.IMPLICIT_DECLARATION,
                                _repair.MACRO_EXPANSION_LINES, "-I", str(_utest.header_dir())]
        prelude = [] if setup else self._test_prelude()
        quoted = ["-iquote", str(quote_from)] if quote_from is not None else []
        return [*build.test_flags, *flags, *prelude, *quoted, "-iquote", str(build.source.parent)]

    def _ctest_listing(self, build, suite, tests_dir: Path,
                       deadline: float) -> "Dict[str, Path] | _Failure":
        """ctest's listing of a CMake project's tests, as the one test file's
        object: the suite names the file that defines them (its
        ``CMakeLists.txt``)."""
        if build.kind != "cmake":
            return _Failure(la.Outcome.UNSUPPORTED, "c_tests ctest needs a CMake project "
                            f"(c_build_kind cmake), not a {build.kind} build")
        listing = _ctest.list_suite(build, tests_dir, self._run,
                                    max(1.0, min(_COMPILE_TIMEOUT_S, deadline - time.monotonic())))
        if isinstance(listing, tuple):
            return _Failure(*listing)
        root = Path(suite.root)
        first = Path(suite.test_files[0])
        return {self._relative_test(root, first if first.is_absolute() else root / first):
                listing}

    def _link(self, build, objects: Sequence[Path], program: Path, coverage: bool,
              deadline: float, suite_root: str) -> "Optional[_Failure]":
        """Link the test program ``program`` from ``objects``, the test file's
        and the target's, and for the program a crash restarts the test
        filter's: None when it linked. For ctest's
        tests, ``program`` is the directory of their relinked programs. ld's
        lines name a test file by its path under ``suite_root``, as the suite
        names it."""
        test_object, target_object = objects[0], objects[1]
        program.parent.mkdir(parents=True, exist_ok=True)
        if self._test_kind == "ctest":
            # A C run carries no session seed (_NOT_PROVIDED): {seed} is _ctest.SEED.
            failed = _ctest.link_programs(
                build, test_object, target_object, program, coverage,
                lambda argv, cwd: self._compile(argv, cwd, deadline),
                _ctest.extra_arguments(self._setting("c_ctest_arguments")))
            if failed is None:
                return None
            outcome, reason, text = failed
            return self._step_failure(outcome, reason, text)
        done = self._compile(build.link_command(objects, program,
                                                coverage=coverage),
                             str(program.parent), deadline)
        if done is None:
            return _Failure(la.Outcome.TIMED_OUT, "the run reached its timeout while linking "
                            "the tests")
        if done.returncode != 0:
            text = (done.stdout or "") + (done.stderr or "")
            return self._step_failure(la.Outcome.COLLECTION_FAILED,
                                      "a test file does not link with the target", text,
                                      {str(suite_root) + os.sep: ""})
        return None

    def _linked_program(self, program: Path) -> Optional[Path]:
        """A linked test program of ``program``: itself, or for ctest's tests
        the first program relinked into its directory, None when none was."""
        return _ctest.first_program(program) if self._test_kind == "ctest" else program

    @staticmethod
    def _relative_test(root: Path, test_file: Path) -> str:
        try:
            return test_file.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return test_file.name

    def _compile(self, argv: Sequence[str], cwd: str, deadline: float,
                 extra: Optional[Mapping[str, str]] = None):
        """Run a compile or link with the remaining time, or None at the
        deadline. ``extra`` are variables added to the command's, which runs
        with English messages (``_tool_environment``)."""
        remaining = min(_COMPILE_TIMEOUT_S, deadline - time.monotonic())
        if remaining <= 0:
            return None
        try:
            return self._run(argv, cwd=cwd, env=self._tool_environment(extra), timeout=remaining)
        except subprocess.TimeoutExpired:
            return None

    def _step_failure(self, outcome: la.Outcome, reason: str, output: str,
                      named: Optional[Mapping[str, str]] = None) -> "_Failure":
        """A failed compile or link step, its ``output`` and error lines
        (``_utest.compile_errors``) written as the model knows the files: each
        path of ``named`` (a staged copy) by its name there, utest.h by its
        name, a file of the project or of a build's copy of it by its
        project-relative path, and any other file
        of a work directory by its own name. A test file that does not compile
        or link (``collection_failed``) carries the step as its
        ``compile_error`` diagnostic."""
        root = Path(self.context.root)
        replaced = {**(named or {}), str(_utest.header_dir()) + os.sep: "",
                    str(root) + os.sep: "", str(root.resolve()) + os.sep: ""}
        text = output
        for path in sorted(replaced, key=len, reverse=True):
            text = text.replace(path, replaced[path])
        if self._builds_dir is not None:
            # A build's copy of the project, as the project.
            text = re.sub(re.escape(str(self._builds_dir)) + r"/[^/\s]+/project/", "", text)
        text = _WORK_PATH.sub("", text)
        marks = ((RunDiagnostic("compile_error", reason),)
                 if outcome is la.Outcome.COLLECTION_FAILED else ())
        return _Failure(outcome, reason, text, _utest.compile_errors(text), marks)

    @staticmethod
    def _counter_variables(prefix: Path, target_object: Path) -> Dict[str, str]:
        """gcov's variables that write the target's counters under ``prefix``."""
        return {"GCOV_PREFIX": str(prefix),
                "GCOV_PREFIX_STRIP": str(len(target_object.parent.parts) - 1)}

    def _read_counts(self, build, target_object: Path, counters: Optional[Path],
                     counts_dir: Path) -> _gcov.Counts:
        """The counts of the coverage build ``target_object`` from the
        counters file ``counters`` (None: nothing of the target ran), read
        in ``counts_dir``. ``_compile_target`` compiled it in the directory's
        stage (``stage_dir``), beside its ``obj``."""
        return _gcov.read_counts(self._toolchain().gcov, target_object.with_suffix(".gcno"),
                                 counters, build.operand, counts_dir,
                                 lambda argv, **kw: self._run(
                                     argv, env=self._tool_environment(), **kw),
                                 root=build.stage_dir(target_object.parent.parent))

    def _listed(self, program: Path, env, cwd: str, timeout_s: float
                ) -> Tuple[Tuple[str, ...], Optional[_utest.TestRun]]:
        """The tests ``program`` lists, else none and why: its listing failed
        (``error``) or reached ``timeout_s`` (``timed_out``)."""
        try:
            return self._runner.list_tests(program, self._run_test, cwd=cwd, env=env,
                                           timeout_s=timeout_s), None
        except subprocess.TimeoutExpired:
            return (), _utest.TestRun("", "timed_out", None,
                                      f"{program.name} --list-tests reached its timeout "
                                      f"of {timeout_s:g} s", "")
        except RuntimeError as exc:
            return (), _utest.TestRun("", "error", None, str(exc), "")

    def _run_programs(self, programs: Mapping[str, Path], env, deadline: float, scratch: Path):
        """Each test of each program in its own process, until the deadline:
        the ``(file, TestRun)`` pairs and whether the deadline ended the run."""
        runs: List[Tuple[str, _utest.TestRun]] = []
        for file, program in programs.items():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return runs, True
            names, failed = self._listed(program, env or self._environment(), str(scratch),
                                         remaining)
            if failed is not None and failed.outcome == "timed_out":
                return runs, True
            if failed is not None:
                runs.append((file, failed))
                continue
            for name in names:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return runs, True
                one = self._runner.run_one(program, name, self._run_test, cwd=str(scratch),
                                           env=env or self._environment(), timeout_s=remaining)
                runs.append((file, one))
                if one.outcome == "timed_out":
                    return runs, True
        return runs, False

    def _suite_programs(self, prepared: "_Prepared", env, deadline: float, scratch: Path,
                        coverage: bool):
        """The suite's runs: ``(runs, timed_out, counters directories, whole
        runs)``, the counters None when they are not the run's, or the
        failure that ended it. utest.h tests run each file whole
        (``_whole_runs``), a project's own programs and ctest's tests each
        test alone (``_run_programs``), counting into ``scratch/counters``
        with ``env``."""
        if self._test_kind != "c":
            runs, timed_out = self._run_programs(prepared.programs, env, deadline, scratch)
            return runs, timed_out, [scratch / "counters"], None
        whole = self._whole_runs(prepared, deadline, scratch, coverage)
        if isinstance(whole, _Failure):
            return whole
        return (whole.runs, whole.timed_out,
                list(whole.counters.values()) if whole.measured else None, whole)

    def _suite_coverage(self, prepared: "_Prepared", counters: Sequence[Path], scratch: Path,
                        target: str):
        """The target's coverage over the counters files under ``counters``,
        and the inconsistency for which gcovr refused them (else None)."""
        root = Path(self.context.root)
        try:
            return _gcov.observation(self._merged_counts(
                prepared.build, prepared.target_object, counters, scratch / "read"),
                root / target, root), None
        except _gcov.CountsInconsistent as problem:
            return None, problem

    def _whole_runs(self, prepared: "_Prepared", deadline: float, scratch: Path,
                    coverage: bool) -> "_WholeRuns | _Failure":
        """Each utest.h test file's program run whole, as delivered, all its
        tests in one process in their order of definition
        (``_utest.run_program``, ``_whole_run``), in the work directory
        ``scratch/whole/<index>``. A program that ended before its first test
        ends the run, as a run as delivered says it."""
        whole = _WholeRuns(coverage, deadline)
        for index, file in enumerate(prepared.programs):
            failed = self._whole_run(prepared, file, scratch / "whole" / str(index), whole)
            if failed is not None:
                return failed
            if whole.timed_out:
                break
        return whole

    def _whole_run(self, prepared: "_Prepared", file: str, work: Path,
                   whole: "_WholeRuns") -> "Optional[_Failure]":
        """``file``'s program run whole until a run ends without a crash, each
        run in a new, empty working directory and, with coverage, a new
        counters directory, so that the counters are one process's.

        - A crash in a test, the process ended in it, restarts the program
          without the tests that crashed (``_restart_program``). Only that
          program's environment names them (``_utest.SKIP_TESTS``). A test
          keeps the outcome of the first process that ran it, so a restart
          supplies the outcomes of the tests after the crash and the
          counters. A test that ends the program again although the restart
          left it out, or a restart program that cannot be built, ends the
          restarts: the tests keep their outcomes and the run has no
          coverage.
        - A hang ends the run in the test that was running.
        - A run without a crash is checked and, when it ended badly, named
          (``_complete``).

        Known limits: a crash is blamed on the last test whose RUN line
        arrived, so a test that switches stdout to full buffering and passes
        can take a later test's crash, and its lines leave the coverage. A
        test that passed in the first process and crashes in a restart keeps
        its pass, and the next restart leaves it out, so its lines leave the
        coverage. After a restart program that does not link, the tests after
        the crash have no outcome and do not run alone."""
        program, kept, crashed = prepared.programs[file], {}, []
        for attempt in itertools.count():
            remaining = whole.deadline - time.monotonic()
            if remaining <= 0:
                return whole.end(file, kept, timed_out=True)
            counters = work / f"counters-{attempt}" if whole.coverage else None
            run = self._run_whole(prepared, program, work / f"run-{attempt}", counters, crashed,
                                  remaining)
            whole.output.append(run.output)
            for one in run.tests:
                kept.setdefault(one.name, one)
            if run.timed_out:
                if run.tests:
                    kept[run.tests[-1].name] = run.tests[-1]
                return whole.end(file, kept, timed_out=True)
            if run.problem:
                return _Failure(la.Outcome.COLLECTION_FAILED, f"the test program of {file} ended "
                                f"before its first test: {run.problem}", run.output)
            if not run.tests or run.tests[-1].outcome != "error":
                return self._complete(prepared, file, run, counters, kept, whole)
            last = run.tests[-1]
            if last.name in crashed:
                names, _ = self._keep_listed(prepared, file, kept, whole)
                self._name_bad_end(prepared, file, kept, names or tuple(kept),
                                   f"the test {last.name} ended its program again although "
                                   "the restart left it out", whole)
                return whole.end(file, kept, measured=False)
            crashed.append(last.name)
            if attempt == 0:
                program = self._restart_program(prepared, file, whole.coverage, whole.deadline)
                if isinstance(program, _Failure):
                    whole.problems.append(program.text())
                    return whole.end(file, kept, measured=False)
        return None

    def _run_whole(self, prepared: "_Prepared", program: Path, directory: Path,
                   counters: Optional[Path], crashed: Sequence[str], remaining: float):
        """One run of ``program`` (``_utest.run_program``) in the new, empty
        ``directory``, its counters in the new ``counters`` (None without
        coverage), with the skip variable only when tests ``crashed``."""
        directory.mkdir(parents=True)
        extra = (self._counter_variables(counters, prepared.target_object)
                 if counters is not None else {})
        if crashed:
            extra[_utest.SKIP_TESTS] = "\n".join(crashed)
        return _utest.run_program(program, self._run_test, cwd=str(directory),
                                  env=self._environment(extra), timeout_s=remaining)

    def _complete(self, prepared: "_Prepared", file: str, run: "_utest.ProgramRun",
                  counters: Optional[Path], kept: Dict[str, "_utest.TestRun"],
                  whole: "_WholeRuns") -> None:
        """The end of ``file``'s run without a crash. A run that ended badly
        (``_bad_end``) is named (``_name_bad_end``) and has no counters, else
        its counters are the file's."""
        names, unlisted = self._keep_listed(prepared, file, kept, whole)
        ended = _bad_end(run, counters, prepared.target_object) or unlisted
        if ended:
            self._name_bad_end(prepared, file, kept, names or tuple(kept), ended, whole)
            return whole.end(file, kept, measured=False)
        if counters is not None:
            whole.counters[file] = counters
        return whole.end(file, kept)

    def _keep_listed(self, prepared: "_Prepared", file: str, kept: Dict[str, "_utest.TestRun"],
                     whole: "_WholeRuns") -> Tuple[Tuple[str, ...], str]:
        """The tests ``file``'s program lists (``--list-tests``), with the
        outcome of every name it does not list taken out of ``kept``: a test
        that prints a RUN line of another name gives that name no outcome.
        Also why the run is not complete: a listed test without an outcome,
        or a listing that failed ("" when neither)."""
        program = prepared.programs[file]
        names, failed = self._listed(program, self._environment(), str(program.parent),
                                     max(1.0, whole.deadline - time.monotonic()))
        if failed is not None:
            return (), f"the program did not list its tests: {failed.message}"
        for name in [name for name in kept if name not in names]:
            del kept[name]
        missing = [name for name in names if name not in kept]
        return names, (f"the program gave no outcome for {', '.join(missing)}, which it lists"
                       if missing else "")

    def _name_bad_end(self, prepared: "_Prepared", file: str, kept: Dict[str, "_utest.TestRun"],
                      names: Sequence[str], ended: str, whole: "_WholeRuns") -> None:
        """Each test of ``names``, the tests ``file``'s program lists, that is
        not an error yet, run in a process of its own (``_alone``). A listed
        test without an outcome takes its own process's. A test whose own
        process ends badly after its result line, or with coverage writes no
        counters, becomes the error with that process's message, and any
        other test keeps the outcome it has. When no process writes counters,
        the environment keeps them from being written, and no test is to
        blame. When no test is to blame, the program's bad end ``ended`` is
        the run's problem. A timeout that cuts the processes short leaves
        the outcomes found so far and times the run out."""
        from dataclasses import replace

        alone = self._alone(prepared, file, names, kept, whole)
        for name, (one, _, _) in alone.items():
            kept.setdefault(name, one)
        if whole.timed_out:
            return
        checked = [unwritten for one, unwritten, _ in alone.values() if one.outcome != "error"]
        if checked and all(checked):
            whole.problems.append(f"the test program of {file}: no test's own process wrote "
                                  "coverage counters, so the environment keeps them from being "
                                  "written")
            return
        named = False
        for name, (one, unwritten, _) in alone.items():
            # A time is utest's result line: the process ended after it.
            if unwritten or (one.outcome == "error" and one.time_s is not None):
                kept[name] = replace(one, outcome="error", message=(
                    "its process wrote no coverage counters" if unwritten else one.message))
                named = True
        if not named:
            whole.problems.append(f"the test program of {file}: {ended}")

    def _alone(self, prepared: "_Prepared", file: str, names: Sequence[str],
               kept: Mapping[str, "_utest.TestRun"], whole: "_WholeRuns"):
        """Each test of ``names`` that is not an error in ``kept``, run in a
        process of its own (``_utest.run_one``) as per-test evidence runs it,
        but line-buffered as a whole program is, so that its result line
        reaches the output before an exit handler ends it badly. Each with
        whether it exited without writing counters where coverage is asked,
        and the directory of its counters (None without coverage), by name.
        When the run's timeout cuts the search short, the run is timed out
        and the tests run so far are returned."""
        program = prepared.programs[file]
        notes = prepared.target_object.with_suffix(".gcno")
        alone = {}
        for number, name in enumerate([name for name in names
                                       if name not in kept or kept[name].outcome != "error"]):
            remaining = whole.deadline - time.monotonic()
            if remaining <= 0:
                whole.timed_out = True
                break
            directory = program.with_name(f"{program.name}-alone-{number}")
            directory.mkdir(parents=True)
            counters = directory / "counters" if whole.coverage else None
            env = self._environment(self._counter_variables(counters, prepared.target_object)
                                    if counters is not None else None)
            one = self._runner.run_one(
                program, name, lambda argv, **options: self._run_test(
                    [*_utest.LINE_BUFFERED, *argv], **options),
                cwd=str(directory), env=env, timeout_s=remaining)
            alone[name] = (one, counters is not None and one.outcome != "error"
                           and _gcov.counters_file(counters, notes) is None, counters)
        return alone

    def _restart_program(self, prepared: "_Prepared", file: str, coverage: bool,
                         deadline: float) -> "Path | _Failure":
        """``file``'s program linked again with ``data/utest_skip.c``, whose
        test filter leaves out the tests ``_utest.SKIP_TESTS`` names: the
        program a crash restarts. The delivered program holds no such
        object."""
        skip = self._data_object("utest_skip", "the adapter's test filter",
                                 "the adapter's test filter does not compile", deadline)
        if isinstance(skip, _Failure):
            return skip
        program = prepared.programs[file]
        restart = program.with_name(f"{program.name}-restart")
        failed = self._link(prepared.build, (prepared.objects[file], prepared.target_object, skip),
                            restart, coverage, deadline, prepared.suite_root)
        return restart if failed is None else failed

    @staticmethod
    def _test_id(file: str, name: str) -> la.TestId:
        qualname, _, index = name.partition("/")
        return la.TestId(file, qualname, params=index or None,
                         text=f"{file}::{name}")

    def _suite_run(self, runs, timed_out, coverage, identity, tests_s, timeout_s) -> SuiteRun:
        """The run of ``runs``, its time ``tests_s`` the test processes' own,
        from before the first to after the last."""
        outcomes = []
        active = None
        listing_errors = []
        for file, one in runs:
            if not one.name:
                listing_errors.append(f"{file}: {one.message}")
                continue
            test = self._test_id(file, one.name)
            if one.outcome == "timed_out":
                active = test
                continue
            outcomes.append(TestOutcome(test, {"passed": PASSED, "failed": FAILED,
                                               "skipped": SKIPPED}.get(one.outcome, ERROR),
                                        one.time_s, one.message,
                                        trace=(_utest.line_ended(one.output)
                                               if one.outcome != "passed" else "")))
        counts = RunCounts(collected=len(outcomes),
                           passed=sum(t.outcome == PASSED for t in outcomes),
                           failed=sum(t.outcome == FAILED for t in outcomes),
                           errors=sum(t.outcome == ERROR for t in outcomes),
                           skipped=sum(t.outcome == SKIPPED for t in outcomes))
        outcome, reason = _suite_outcome(timed_out, listing_errors, outcomes, counts, timeout_s)
        output = "".join(_utest.line_ended(one.output) for _, one in runs)
        return SuiteRun(outcome=outcome, reason=reason, counts=counts, tests=tuple(outcomes),
                        unmatched=(), wall_time_s=tests_s, output=output,
                        coverage=coverage, identity=identity, active_at_end=active)

    def coverage_texts(self, coverage) -> Dict[str, Tuple[Any, ...]]:
        """The covered lines, and per line the untaken branches gcov counts
        (``"line->k of n branches untaken"``), as Java's adapter writes them."""
        if coverage is None:
            return {"covered_lines": (), "missing_branches": ()}
        return {"covered_lines": tuple(coverage.covered),
                "missing_branches": _gcov.missed_branch_texts(coverage)}

    # Per-test evidence.

    def per_test_evidence(self, request: PerTestEvidenceRequest) -> PerTestEvidence:
        """Each test's own coverage of the target: the process that ran the test
        alone wrote its own counters. With a mutation run, the mutants each
        test kills. A suite that is not green carries no evidence. Python
        tests write their counters per test inside one process."""
        request.validate()
        started = time.monotonic()
        if self._extension_mode:
            return self._extension_evidence(request, started)
        identity = self._identity(request, coverage=True)
        refused = KILLS_UNUSABLE if request.mutation_run is not None else KILLS_NOT_REQUESTED

        def ended(outcome, reason, stopping=()) -> PerTestEvidence:
            return PerTestEvidence(outcome, reason, (), (), (), refused, (), None, None, identity,
                                   stopping_tests=tuple(stopping))

        refusal = self._refusal(request)
        if refusal is not None:
            return ended(*refusal)
        target = self._relative_target(request.target)
        root = Path(self.context.root)
        deadline = started + request.timeout_s
        with work_dir_tempdir(self.context, self._executor(), prefix="langadapter_c_") as scratch:
            scratch = Path(scratch)
            prepared = self._prepare(target, request.suite, scratch, coverage=True,
                                     deadline=deadline)
            if isinstance(prepared, _Failure):
                return ended(prepared.outcome, prepared.text())
            if request.warmup:
                _, timed_out = self._run_programs(prepared.programs, None, deadline, scratch)
                if timed_out:
                    return ended(la.Outcome.TIMED_OUT,
                                 f"the warmup reached the timeout of {request.timeout_s:g} s")
            kills, unmapped, killed = self._kill_items(request.mutation_run)
            measured = time.monotonic()
            runs = self._evidence_runs(request, target, prepared, scratch, deadline, killed)
            if isinstance(runs[0], la.Outcome):
                return ended(*runs)
            evidence, suite_counts = runs
            if not evidence:
                return ended(la.Outcome.NO_TESTS, "no test")
            coverage = _gcov.observation(suite_counts, root / target, root)
        untimed = tuple(e.test for e in evidence if e.time_ms is None)
        return PerTestEvidence(la.Outcome.COMPLETED, "", tuple(evidence), untimed, (), kills,
                               unmapped, 1000.0 * (time.monotonic() - measured), coverage,
                               identity)

    def _evidence_runs(self, request: PerTestEvidenceRequest, target: str, prepared,
                       scratch: Path, deadline: float, killed):
        """Each C test alone with its own counters: the tests' evidence and the
        suite's summed counts, else the outcome, the reason and the tests the
        failure is pinned on (``_evidence_end``). Every test runs, so that each
        test that does not pass, and each whose own counts gcovr refuses
        (``_gcov.CountsInconsistent``) or whose process wrote none, is
        named."""
        root = Path(self.context.root)
        evidence: List[TestEvidence] = []
        suite_counts: Optional[_gcov.Counts] = None
        stops: List[_gcov.Stop] = []
        listed: Dict[str, Tuple[str, ...]] = {}
        timeout = (la.Outcome.TIMED_OUT, f"the run reached its timeout of {request.timeout_s:g} s")
        for file, program in prepared.programs.items():
            names, failed = self._listed(program, self._environment(), str(scratch),
                                         max(1.0, deadline - time.monotonic()))
            if failed is not None:
                return self._evidence_end(stops, listed, timeout if failed.outcome == "timed_out"
                                          else (la.Outcome.COLLECTION_FAILED,
                                                f"{file} did not list its tests: {failed.message}"))
            listed[file] = names
            for name in names:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._evidence_end(stops, listed, timeout)
                test = self._test_id(file, name)
                prefix = scratch / "counters" / str(len(evidence) + len(stops))
                env = self._environment(self._counter_variables(prefix,
                                                                prepared.target_object))
                one = self._runner.run_one(program, name, self._run_test, cwd=str(scratch), env=env,
                                           timeout_s=remaining)
                if one.outcome not in ("passed", "skipped"):
                    stops.append(_gcov.Stop(file, name, f"{file}::{name} did not pass: "
                                                        f"{one.message or one.outcome}"))
                    continue
                try:
                    counts = self._own_counts(prepared.build, prepared.target_object, prefix,
                                              scratch / "read" / prefix.name, test)
                except RuntimeError:
                    if not stops:
                        raise
                    # A failure met before keeps its outcome.
                    return self._evidence_end(stops, listed)
                if isinstance(counts, str):
                    stops.append(_gcov.Stop(file, name, counts, counts=True))
                    continue
                suite_counts = counts if suite_counts is None else suite_counts.merged(counts)
                own = _gcov.observation(counts, root / target, root)
                evidence.append(_test_evidence(test, own, killed, one.time_s))
        return self._evidence_end(stops, listed) or (evidence, suite_counts)

    def _evidence_end(self, stops: Sequence[_gcov.Stop], listed: Mapping[str, Sequence[str]],
                      ended: Optional[Tuple[la.Outcome, str]] = None):
        """How C per-test evidence ends, with the outcome and reason a run
        that stopped at its first failure gives: a test that did not pass,
        else ``ended`` (a timeout or a listing that failed), else the first
        test whose counts gcovr refuses or none were written for. Then every
        test the failure is pinned on (``_gcov.named``). None when nothing
        ended it."""
        if not stops and ended is None:
            return None
        named = tuple(self._test_id(file, name) for file, name in _gcov.named(stops, listed))
        failing = [stop.what for stop in stops if not stop.counts]
        if failing:
            return la.Outcome.TEST_FAILURES, failing[0], named
        if ended is not None:
            return (*ended, named)
        return la.Outcome.INFRASTRUCTURE_FAILURE, stops[0].what, named

    def _own_counts(self, build, target_object: Path, prefix: Path, counts_dir: Path,
                    test: la.TestId) -> "_gcov.Counts | str":
        """One test's own counts from the counters its process wrote under
        ``prefix``, else why there are none: the reason of a missing counters
        file (``_gcov.no_counters``), or gcovr's message on counts it refuses
        (``_gcov.CountsInconsistent``). A missing file is such a reason for
        utest.h tests and Python tests of an extension, whose every test
        writes one. ctest's tests and a project's own programs read it as
        zero counts, since a ctest test may run no program of the target,
        such as a script."""
        counters = _gcov.counters_file(prefix, target_object.with_suffix(".gcno"))
        if counters is None and self._test_kind in ("c", "python"):
            return _gcov.no_counters(test.text)
        try:
            return self._read_counts(build, target_object, counters, counts_dir)
        except _gcov.CountsInconsistent as inconsistent:
            return str(inconsistent)

    @staticmethod
    def _kill_items(mutation_run):
        """The kill items per test from a mutation run of the same suite."""
        if mutation_run is None:
            return KILLS_NOT_REQUESTED, (), {}
        unmapped = mutation_run.unmatched_killers()
        if unmapped:
            return KILLS_UNUSABLE, unmapped, {}
        killed: Dict[la.TestId, set] = {}
        for mutant in mutation_run.mutants:
            for test in mutant.killing_tests:
                killed.setdefault(test, set()).add(KillItem(str(mutant.row.get("id"))))
        return KILLS_USABLE, (), killed

    # Sources.

    def test_units(self, source: str):
        """Each utest.h test of ``source`` with its span in characters, or None
        when the file does not parse. An indexed test is one unit. An adapter
        with Python tests has no units: Python's own operations find and
        remove its tests."""
        data = source.encode("utf-8")
        if _syntax.first_error(data, test_file=True) is not None:
            return None
        units = []
        for unit in _syntax.macro_units(data):
            if unit.macro in _syntax.TEST_MACROS:
                start, end = whole_lines(data, *unit.span)
                units.append(TestUnit(unit.name, _syntax.char_offset(data, start),
                                      _syntax.char_offset(data, end)))
        return units

    def prune_tests(self, source: str, tests):
        """``source`` without the named tests, each named by its unit
        (``set.name``) or by an id: ``file::set.name``, with ``/index`` or a
        case's ``[params]`` after it (``test_id_text``, a consumer's case
        id). A TestId is named by its id text. An indexed test's id removes
        its whole ``UTEST_I`` block, every index of it, since one index is
        no unit of its own, and the detail says so. A name that matches
        nothing is reported as named, a TestId by its qualname, as Python
        and Java report it."""
        from dataclasses import replace
        from langadapter.operations import _prune_by_units

        # Each test by its id text: its reported name, its unit, whether it names an index.
        given: Dict[str, Tuple[str, str, bool]] = {}
        for test in tests:
            named = isinstance(test, la.TestId)
            text = self.test_id_text(test) if named else test
            tail = text.rpartition("::")[2]
            unit = re.split(r"[/\[]", tail, maxsplit=1)[0]
            given.setdefault(text, (test.qualname if named else test, unit, unit != tail))
        result = _prune_by_units(self, source, [unit for _, unit, _ in given.values()])
        unmatched = dict.fromkeys(shown for shown, unit, _ in given.values()
                                  if unit in result.unmatched)
        whole = dict.fromkeys(unit for _, unit, indexed in given.values()
                              if indexed and unit in result.removed)
        detail = ". ".join(f"{unit} is removed whole: one index of a UTEST_I "
                           "test is no unit of its own" for unit in whole)
        return replace(result, unmatched=tuple(unmatched), detail=detail or result.detail)

    def validate_source(self, source: str) -> SourceValidation:
        """Whether ``source`` parses as C, utest.h's macros read as the
        declarations they expand to, with the first error's place. Python
        tests: as the project's Python parses them."""
        if self._extension_mode:
            return self._python().validate_source(source)
        import hashlib

        data = source.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        problem = _syntax.first_error(data, test_file=True)
        if problem is None:
            return SourceValidation("valid", "", 0, 0, _syntax.PARSER, digest)
        return SourceValidation("syntax_error", problem.message, problem.line, problem.column,
                                _syntax.PARSER, digest)

    def finalize_source(self, source: str):
        """``source`` with ``UTEST_MAIN()`` appended when it has utest.h tests
        and no main, as a reply cut off after its last test lacks it
        (``SourceResult``). Python tests: Python's."""
        from langadapter.sources import APPLIED, SourceResult, UNCHANGED

        data = source.encode("utf-8")
        if _syntax.has_main(data) or not any(unit.macro in _syntax.TEST_MACROS
                                             for unit in _syntax.macro_units(data)):
            return SourceResult(UNCHANGED, source, _syntax.PARSER)
        ended = source if source.endswith("\n") else source + "\n"
        return SourceResult(APPLIED, ended + "\nUTEST_MAIN()\n", _syntax.PARSER,
                            detail="UTEST_MAIN() appended: the file has tests and no main")

    def merge_sources(self, first: str, second: str, *, separator: str):
        """The second test file folded into the first: the
        first as it is, after the ``separator`` line the second's items, the
        names both declare shared or renamed, then one ``UTEST_MAIN()``.
        Refused when the merge does not parse, loses a test or leaves two of
        one name (``SourceResult``)."""
        from langadapter.sources import APPLIED, PARSE_ERROR, REFUSED, SourceResult

        for label, text in (("the first source", first), ("the second source", second)):
            problem = _syntax.first_error(text.encode("utf-8"), test_file=True)
            if problem is not None:
                return SourceResult(PARSE_ERROR, first, _syntax.PARSER,
                                    detail=f"{label} does not parse: {problem.message}")
        merged = _merge.merge(first, second, separator)
        if merged.source is None:
            return SourceResult(REFUSED, first, _syntax.PARSER, detail=merged.problem)
        problem = _syntax.first_error(merged.source.encode("utf-8"), test_file=True)
        if problem is not None:
            return SourceResult(REFUSED, first, _syntax.PARSER,
                                detail=f"the merged source does not parse: {problem.message}")
        names = [_test_names(text) for text in (first, second, merged.source)]
        if len(names[2]) != len(names[0]) + len(names[1]) or len(set(names[2])) != len(names[2]):
            return SourceResult(REFUSED, first, _syntax.PARSER,
                                detail="the merge would lose a test or name two tests alike")
        detail = "; ".join(part for part in (
            "renamed " + ", ".join(f"{old} -> {new}" for old, new in merged.renamed)
            if merged.renamed else "",
            "shared " + ", ".join(merged.shared) if merged.shared else "") if part)
        return SourceResult(APPLIED, merged.source, _syntax.PARSER, detail=detail)

    def salvage_syntax(self, source: str):
        """A repair of a test file the parser rejects, in at most 25 rounds
        (``templates.repair_syntax``): a token the parser finds missing (``;``,
        a closing bracket or brace) is inserted where it is missing, any
        other error blanks its line. ``unchanged`` when it parses,
        ``applied`` when repairs made it parse, else ``not_repaired``."""
        from langadapter import templates
        from langadapter.sources import APPLIED, NOT_REPAIRED, SourceResult, UNCHANGED

        result = templates.repair_syntax(source, self.validate_source, _repair.syntax_fix)
        if result.status == templates.VALID:
            return SourceResult(UNCHANGED, result.source, _syntax.PARSER)
        if result.status == templates.REPAIRED:
            return SourceResult(APPLIED, result.source, _syntax.PARSER,
                                detail=f"{result.repairs} repair(s)")
        if result.status == templates.EXHAUSTED:
            return SourceResult(NOT_REPAIRED, source, _syntax.PARSER,
                                detail=(f"still does not parse after "
                                        f"{templates._REPAIR_ROUNDS} repairs"))
        verdict = result.validation
        return SourceResult(NOT_REPAIRED, source, _syntax.PARSER,
                            detail=(f"still does not parse after {result.repairs} repair(s): "
                                    f"{verdict.message}"))

    def strip_module_level_skips(self, source: str):
        """``source`` as it is (``unchanged``), or ``parse_error`` when it does
        not parse. Python's rule removes only the statements outside any test
        that skip the whole file, and utest.h has none. Its ``UTEST_SKIP`` stands
        in a test or a fixture's setup and skips that test or that fixture's
        tests, as a skip in a Python test or fixture does, which the rule keeps."""
        from langadapter.sources import PARSE_ERROR, SourceResult, UNCHANGED

        problem = _syntax.first_error(source.encode("utf-8"), test_file=True)
        if problem is not None:
            return SourceResult(PARSE_ERROR, source, _syntax.PARSER, detail=problem.message)
        return SourceResult(UNCHANGED, source, _syntax.PARSER)

    # Structure (``_structure``).

    #: LangAdapter's structural metric provider counts the lines of code and
    #: the complexity sum with lizard, as for Python and Java.
    _lizard_metrics = True

    def structural_parser_identity(self) -> str:
        """tree-sitter-c's and lizard's versions. With Python tests, Python's
        parser, which reads the tests, and C's, which reads the target."""
        if self._extension_mode:
            return (f"tests: {self._python().structural_parser_identity()}; "
                    f"target: {_structure.parser_identity()}")
        return _structure.parser_identity()

    def optimization_metrics(self, source: str) -> Dict[str, Any]:
        """The eight structural counters of a C source, each None when it does
        not parse. The target's are C's, also with Python tests."""
        return _structure.metrics(source)

    def skeleton(self, source: str) -> Tuple[str, ...]:
        """The names of a C source's functions, utest.h tests and fixture
        blocks in source order, () when it does not parse."""
        return _structure.skeleton(source)

    def same_skeleton(self, first: str, second: str) -> Tuple[bool, str]:
        """Whether two C sources define the same functions in the same order,
        and why not."""
        return _structure.same_skeleton(first, second)

    def function_fingerprints(self, source: str) -> Dict[str, str]:
        """Each function's, test's and fixture block's fingerprint by its
        name, the same whatever the layout and comments."""
        return _structure.function_fingerprints(source)

    def stage_pool(self, members: Any, *, setup: Any = ()) -> Any:
        """Several suites' C test files as one pool, each file named for its
        suite. ctest's tests are its CMake project's, which does
        not pool: the operations layer refuses it (``operation_context_issue``).
        Python tests of an extension: Python's."""
        return _pool.stage(members, setup)

    def operation_context_issue(self, name: str) -> str:
        """Why ``name`` cannot run for these tests: ctest's tests do not
        pool, which the operation report says."""
        if name == "sources.stage_pool" and self._test_kind == "ctest":
            return _CTEST_NO_POOL
        return super().operation_context_issue(name)

    # The validity policy.

    def target_identity(self, target: str):
        """The ``TargetIdentity`` of a C file under test for the validity
        policy. Raises ``InvalidRequest`` for a missing file or one outside
        the project. For Python tests, the identity Python's policy takes: the
        extension's module and the C file's path and text. Its version is
        Python's (``PythonAdapter.validity_policy_version``) followed by the
        name of the rules for a test that reads or copies the C file."""
        root = Path(self.context.root).resolve()
        path = (Path(target) if Path(target).is_absolute() else root / target).resolve()
        if not path.is_file() or root not in path.parents:
            raise InvalidRequest("target", f"not a file of the project: {target}")
        if not self._extension_mode:
            return _validity.target_identity(path, root)
        import hashlib

        from langadapter.sources import TargetIdentity

        module = _build.declared_module(root, path.relative_to(root).as_posix())
        if module is None:
            raise InvalidRequest("target", f"the project builds no extension module from "
                                           f"{target}")
        text = path.read_text(encoding="utf-8", errors="replace")
        return TargetIdentity(
            source_path=str(path), project_root=str(root), module_name=module,
            language="python",
            policy_version=_extension_policy.version(self._python().validity_policy_version),
            target_source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            target_source=text)

    def analyze_validity(self, source: str, target):
        """The validity policy's findings on a C test ``source`` for ``target``
        (``PolicyReport``): reading, copying or redefining the target. Python
        tests: Python's findings for the extension's identity, and those of a
        test that reads or copies the C file."""
        from dataclasses import replace

        from langadapter.sources import PolicyReport

        self._policy_target(target)
        if self._extension_mode:
            report = self._python().analyze_validity(source, target)
            if report.status != "completed":
                return replace(report, policy_version=target.policy_version)
            added = tuple(f for f in _extension_policy.analyze(source, target,
                                                               target.policy_version)
                          if not _extension_policy.overlaps(f, report.items))
            return PolicyReport("completed", report.items + added, target.policy_version,
                                report.parser)
        try:
            findings = _validity.analyze(source, target)
        except SyntaxError:
            return self._unparsed_policy(source)
        return PolicyReport("completed", findings, _validity.POLICY_VERSION, _syntax.PARSER)

    def screen_source(self, source: str, target):
        """The validity policy's review-only signals on a C test ``source`` for
        ``target`` (``PolicyReport``). Python tests: Python's."""
        from dataclasses import replace

        from langadapter.sources import PolicyReport

        self._policy_target(target)
        if self._extension_mode:
            return replace(self._python().screen_source(source, target),
                           policy_version=target.policy_version)
        flags = _validity.screen(source, target)
        if len(flags) == 1 and flags[0].rule_id == "unparseable_source":
            return self._unparsed_policy(source)
        return PolicyReport("completed", flags, _validity.POLICY_VERSION, _syntax.PARSER)

    def _policy_target(self, target) -> None:
        """Refuse a target identity this adapter's tests are not screened for."""
        language = "python" if self._extension_mode else "c"
        if getattr(target, "language", None) != language:
            raise InvalidRequest("target", f"must be the TargetIdentity of this adapter's "
                                           f"target ({language})")

    @staticmethod
    def _unparsed_policy(source: str):
        from langadapter.sources import PARSE_ERROR, PolicyReport

        problem = _syntax.first_error(source.encode("utf-8"), test_file=True)
        return PolicyReport(PARSE_ERROR, (), _validity.POLICY_VERSION, _syntax.PARSER,
                            detail=problem.message if problem else "",
                            error_line=problem.line if problem else 0,
                            error_column=problem.column if problem else 0)

    # Load repair and import pruning.

    def _configured_target(self, source: Optional[str] = None) -> Optional[str]:
        """The target a source operation on the test file ``source`` compiles
        against or stages: ``c_target``, else the one the file names (the
        extension module a Python test imports, the source file of the header
        a C test includes), else the only target this adapter has built."""
        if self._setting("c_target"):
            return self._relative_target(self._setting("c_target"))
        named = self._named_target(source) if source is not None else None
        if named is not None:
            return named
        with self._lock:
            built = [name for name, held in self._builds.items()
                     if not isinstance(held, _build.BuildProblem)]
        return built[0] if len(built) == 1 else None

    def _named_target(self, source: str) -> Optional[str]:
        """The target the test file ``source`` names by what it imports or
        includes, when it names exactly one of the project's."""
        root = Path(self.context.root)
        if self._extension_mode:
            imported = _extension.imported_modules(source)
            found = {target for module, target in _build.extension_modules(root).items()
                     if module in imported}
        else:
            found = set(self._sources_named(
                {Path(name).stem for name in _syntax.quoted_includes(source.encode("utf-8"))}))
        return found.pop() if len(found) == 1 else None

    def _sources_named(self, stems) -> List[str]:
        """The project's C source files whose stem is one of ``stems``, test
        files and ignored directories left out, as sorted POSIX paths below
        its root."""
        root = Path(self.context.root)
        return sorted(path.relative_to(root).as_posix() for path in root.rglob("*.c")
                      if path.stem in stems and not path.name.startswith("test_")
                      and not set(path.relative_to(root).parts[:-1]) & set(self.ignored_dirs))

    def _compile_text(self, source: str, timeout_s: float):
        """Compile and link a test file's text as a run builds it
        (``_prepare``, without coverage): ``(loads, the compiler's or the
        linker's output, file name)``, or a ``SourceResult`` that ends the
        operation."""
        from langadapter.sources import INFRASTRUCTURE_FAILURE, SourceResult, UNSUPPORTED

        target = self._configured_target(source)
        if target is None:
            return SourceResult(UNSUPPORTED, source, _syntax.PARSER,
                                detail="no target: name the C file the tests are for "
                                       "(c_target)")
        gate = self._gate()
        if gate is not None:
            return SourceResult(INFRASTRUCTURE_FAILURE, source, _syntax.PARSER, detail=gate[1])
        name = f"test_{Path(target).stem}.c"
        with work_dir_tempdir(self.context, self._executor(), prefix="langadapter_c_") as scratch:
            suite_dir = Path(scratch) / "suite"
            suite_dir.mkdir()
            (suite_dir / name).write_text(source, encoding="utf-8")
            prepared = self._prepare(target, la.Suite(str(suite_dir), (name,)),
                                     Path(scratch) / "build", coverage=False,
                                     deadline=time.monotonic() + timeout_s)
        if not isinstance(prepared, _Failure):
            return (True, "", name)
        if prepared.outcome is la.Outcome.COLLECTION_FAILED:
            return (False, prepared.output, name)
        detail = (f"the compile reached its timeout of {timeout_s:g} s"
                  if prepared.outcome is la.Outcome.TIMED_OUT else prepared.text())
        return SourceResult(INFRASTRUCTURE_FAILURE, source, _syntax.PARSER, detail=detail)

    def prune_collection_breakers(self, source: str, *, timeout_s: float):
        """``source`` without the units that keep it from compiling and linking
        as a run builds it: each round removes the utest.h test, fixture block or
        top-level declaration around a line GCC or ld reports an error on, at most
        five rounds. ``unchanged`` when it compiles, ``applied`` with the
        removed units, ``not_repaired`` with the input when a line lies in no
        removable unit (an include, ``UTEST_MAIN()``), no test is left or the
        rounds do not suffice."""
        from langadapter import templates
        from langadapter.sources import (
            APPLIED, INFRASTRUCTURE_FAILURE, NOT_REPAIRED, SourceResult, UNCHANGED)

        def load(text: str) -> templates.Load:
            compiled = self._compile_text(text, timeout_s)
            if isinstance(compiled, SourceResult):
                return templates.Load(False, failure=compiled.detail, stop=compiled.status,
                                      info={"result": compiled})
            compiles, output, name = compiled
            if compiles:
                return templates.Load(True)
            return templates.Load(False, _repair.error_lines(output, name, text),
                                  f"the test file does not load: {output.strip()[-500:]}")

        result = templates.repair_loading(source, load, _repair.without_units,
                                          loads=_BREAKER_ROUNDS + 1,
                                          keeps_a_test=_repair.has_a_test,
                                          report_unmappable=False)
        if result.status == templates.STOPPED:
            held = result.info.get("result")
            return held if held is not None else SourceResult(
                INFRASTRUCTURE_FAILURE, source, _syntax.PARSER, detail=result.failure)
        if result.status == templates.LOADS:
            if not result.removed:
                return SourceResult(UNCHANGED, source, _syntax.PARSER)
            return SourceResult(APPLIED, result.source, _syntax.PARSER, removed=result.removed,
                                detail=f"{len(result.removed)} unit(s) removed")
        return SourceResult(NOT_REPAIRED, source, _syntax.PARSER,
                            detail=f"{result.status}: {result.failure}")

    def prune_unresolvable_imports(self, source: str):
        """``source`` without the includes of files that do not exist, one per
        compile, at most ten (``SourceResult``)."""
        from langadapter import templates
        from langadapter.sources import APPLIED, SourceResult, UNCHANGED

        def find(text: str) -> templates.Failing:
            compiled = self._compile_text(text, 120.0)
            if isinstance(compiled, SourceResult):
                return templates.Failing(stop=compiled.status, info={"result": compiled})
            _, output, name = compiled
            return templates.Failing(units=_repair.missing_includes(output, name))

        def remove(text: str, units):
            return (_repair.without_lines(text, [line for line, _ in units]),
                    [header for _, header in units])

        result = templates.prune_units(source, find, remove, rounds=_INCLUDE_ROUNDS)
        if result.status == templates.STOPPED:
            return result.info["result"]
        if result.status == templates.UNCHANGED:
            return SourceResult(UNCHANGED, source, _syntax.PARSER)
        return SourceResult(APPLIED, result.source, _syntax.PARSER, removed=result.removed,
                            detail=f"{len(result.removed)} include(s) removed")

    def _loading_hook(self, name: str):
        """Python's ``name`` for a Python test file, run with the target's
        extension staged so that importing it loads, as in a run."""
        def hook(source: str, **options):
            from langadapter.sources import SourceResult, UNSUPPORTED

            python = self._staged_python(source)
            if isinstance(python, str):
                return SourceResult(UNSUPPORTED, source, _syntax.PARSER, detail=python)
            return getattr(python, name)(source, **options)
        hook.__name__ = name
        return hook

    def _staged_python(self, source: str):
        """The Python adapter whose runs load the extension of the target the
        test file ``source`` is for, built once per target for the source
        operations, or why there is none."""
        target = self._configured_target(source)
        if target is None:
            return ("no target: the test file imports no extension module of the project's, "
                    "so name the C file the tests are for (c_target)")
        return self._target_python(target)

    def _target_python(self, target: str):
        """The Python adapter whose runs load ``target``'s extension, built
        once per target for the source operations, or why it did not build."""
        with self._lock:
            if target not in self._source_harnesses:
                stage_dir = self._build_dir() / "source-harness" / str(len(self._source_harnesses))
                content = (Path(self.context.root) / target).read_bytes()
                staged = self._stage_extension(target, content, stage_dir, coverage=False,
                                              deadline=time.monotonic() + _BUILD_TIMEOUT_S)
                if isinstance(staged, _Failure):
                    return staged.text()
                harness = _extension.Harness(self, stage_dir / "python")
                harness.configure(module=staged.build.module, extension=staged.extension)
                self._source_harnesses[target] = harness
            return self._source_harnesses[target].python

    def _extension_test_files(self, paths):
        """Python's ``test_files``, collected as the tests run: from the
        import root, with the target's extension staged, so a conftest.py
        that imports the project's package collects. The target is
        ``c_target``, else the only one built, else the only extension
        setup.py names. Without one (several extensions, no ``c_target``)
        the tests collect from the project directory, and a collection that
        fails says to set ``c_target``. Raises RuntimeError when the
        target's extension does not build, as when pytest gives no account
        of a directory."""
        from langadapter.python.project import _collected_test_files

        target = self._configured_target()
        if target is None:
            declared = set(_build.extension_modules(Path(self.context.root)).values())
            target = declared.pop() if len(declared) == 1 else None
        if target is None:
            try:
                return _collected_test_files(self._python(), paths)
            except RuntimeError as exc:
                raise RuntimeError(f"{exc}. With several extensions, set c_target to the "
                                   "C file the tests are for") from None
        python = self._target_python(target)
        if isinstance(python, str):
            raise RuntimeError(f"cannot collect the Python tests of {target}: {python}")
        return _collected_test_files(python, paths)

    def is_test_file(self, path: str) -> bool:
        """Whether ``path`` names a C test file (``test_*.c``), or for Python
        tests of an extension, a test file by Python's rule."""
        if self._extension_mode:
            return self._python().is_test_file(path)
        return any(fnmatch.fnmatchcase(Path(path).name, pattern)
                   for pattern in _TEST_FILE_PATTERNS)

    def setup_files(self, root: str, test_paths) -> Tuple[str, ...]:
        """The sources the project's own test programs build with
        (``c_test_support``, relative to the project's root), as their
        suite's setup files, and none in another mode. They are those of the tree
        ``root`` lies in, the project or a copy of it: the nearest directory at
        or above ``root`` that holds them, else the project's root. Python
        tests have Python's (set on the instance in extension mode)."""
        if self._test_kind != "programs":
            return ()
        names = shlex.split(self._setting("c_test_support"))
        start = Path(root).absolute()
        project = next((parent_dir for parent_dir in (start, *start.parents)
                        if names and all((parent_dir / name).is_file() for name in names)),
                       Path(self.context.root))
        return tuple(str(project / name) for name in names)

    # Python tests of the target's extension.

    def _extension_suite(self, request: SuiteRunRequest, started: float) -> SuiteRun:
        """A run of Python tests against the target's extension: the Python
        adapter's run of the suite, with the extension staged and checked to
        have loaded, and the target's coverage when ``coverage_target``
        names it."""
        from dataclasses import replace

        identity = self._identity(request, coverage=bool(request.coverage_target))
        gate = self._gate()
        if gate is not None:
            return self._ended(*gate, identity)
        target = self._target_of(request)
        if target is None:
            return self._ended(la.Outcome.UNSUPPORTED, _NO_EXTENSION_TARGET, identity)
        coverage = bool(request.coverage_target)
        deadline = started + request.timeout_s
        root = Path(self.context.root)
        with work_dir_tempdir(self.context, self._executor(), prefix="langadapter_c_") as scratch:
            scratch = Path(scratch)
            staged = self._stage_extension(target, (root / target).read_bytes(), scratch,
                                          coverage=coverage, deadline=deadline)
            if isinstance(staged, _Failure):
                return self._ended(staged.outcome, staged.reason, identity,
                                   output=staged.output, detail=staged.detail)
            counters = scratch / "counters" if coverage else None
            harness = _extension.Harness(self, scratch / "python")
            run, report = harness.run(
                self._inner_request(request, deadline), module=staged.build.module,
                extension=staged.extension, counters=counters,
                strip=self._counter_variables(scratch, staged.target_object)["GCOV_PREFIX_STRIP"])
            identity = self._extension_identity(run, identity)
            problem = self._load_problem(report, staged, run.outcome)
            if problem is not None:
                return replace(run, outcome=la.Outcome.INFRASTRUCTURE_FAILURE, reason=problem,
                               coverage=None, identity=identity)
            observed = None
            if coverage and run.outcome in (la.Outcome.COMPLETED, la.Outcome.TEST_FAILURES):
                try:
                    counts = self._merged_counts(staged.build, staged.target_object, [counters],
                                                 scratch / "read")
                except _gcov.CountsInconsistent as inconsistent:
                    return _counts_inconsistent(replace(run, identity=identity), inconsistent)
                observed = _gcov.observation(counts, root / target, root)
        return replace(run, coverage=observed, identity=identity)

    def _extension_delivered(self, request: SuiteRunRequest, *,
                             sources: Optional[Mapping[str, str]] = None) -> SuiteRun:
        """Python tests of the target's extension, each test file alone as
        delivered: the Python adapter's run as delivered, with the project's
        extension staged without counters and checked to have loaded in each
        file's session."""
        from dataclasses import replace

        started = time.monotonic()
        identity = self._identity(request, coverage=False)
        gate = self._gate()
        if gate is not None:
            return self._ended(*gate, identity)
        target = self._target_of(request)
        if target is None:
            return self._ended(la.Outcome.UNSUPPORTED, _NO_EXTENSION_TARGET, identity)
        deadline = started + request.timeout_s
        root = Path(self.context.root)
        with work_dir_tempdir(self.context, self._executor(), prefix="langadapter_c_") as scratch:
            scratch = Path(scratch)
            staged = self._stage_extension(target, (root / target).read_bytes(), scratch,
                                          coverage=False, deadline=deadline)
            if isinstance(staged, _Failure):
                return self._ended(staged.outcome, staged.reason, identity,
                                   output=staged.output, detail=staged.detail)
            harness = _extension.Harness(self, scratch / "python")
            harness.configure(module=staged.build.module, extension=staged.extension)
            run = harness.python.run_as_delivered(self._inner_request(request, deadline),
                                                  sources=sources)
            problem = self._load_problem(_extension.read_report(harness.report), staged,
                                         run.outcome)
        identity = self._extension_identity(run, identity)
        if problem is not None:
            return replace(run, outcome=la.Outcome.INFRASTRUCTURE_FAILURE, reason=problem,
                           reason_detail=(), identity=identity)
        return replace(run, identity=identity)

    def _extension_benchmark(self, request: BenchmarkRequest, identity,
                             started: float) -> Benchmark:
        """The Python adapter's timing of Python tests, with the project's
        extension staged without counters before the repetitions and checked
        to have loaded in each of them."""
        from dataclasses import replace

        includes = self._includes(_timing.EXTENSION_INCLUDES)
        gate = self._gate()
        if gate is not None:
            return _timing.unmeasured(request, identity, *gate, includes=includes)
        target = self._target_of(request)
        if target is None:
            return _timing.unmeasured(request, identity, la.Outcome.UNSUPPORTED,
                                      _NO_EXTENSION_TARGET, includes)
        root = Path(self.context.root)
        with work_dir_tempdir(self.context, self._executor(), prefix="langadapter_c_") as scratch:
            scratch = Path(scratch)
            staged = self._stage_extension(target, (root / target).read_bytes(), scratch,
                                          coverage=False, deadline=started + request.timeout_s)
            if isinstance(staged, _Failure):
                return _timing.unmeasured(request, identity, staged.outcome, staged.text(),
                                          includes)
            harness = _extension.Harness(self, scratch / "python")
            harness.configure(module=staged.build.module, extension=staged.extension)
            options = request.runner_options
            timed = harness.python.benchmark(replace(
                request, runner_options=options.inner if isinstance(options, CRunOptions)
                else options))
            problem = self._load_problem(_extension.read_report(harness.report), staged,
                                         timed.outcome)
        identity = self._extension_identity(timed, identity)
        if problem is not None:
            return replace(timed, outcome=la.Outcome.INFRASTRUCTURE_FAILURE, reason=problem,
                           times_ms=(), includes=includes, identity=identity)
        return replace(timed, includes=includes, identity=identity)

    @staticmethod
    def _load_problem(report: _extension.Report, staged: "_Extension",
                      outcome: la.Outcome) -> Optional[str]:
        """Why sessions of a run did not test the staged extension, or None
        when they did. It is also None for a run that ended, by its own
        outcome, before any session began."""
        if report.loaded(staged.extension):
            return None
        if (report.load_error is None and not report.loads
                and outcome is not la.Outcome.COMPLETED):
            return None
        return report.problem(staged.build.module, staged.extension)

    def _stage_extension(self, target: str, content: bytes, stage_dir: Path, *, coverage: bool,
                        deadline: float) -> "_Extension | _Failure":
        """The target's source ``content`` (the project's, or a mutant)
        compiled with the recorded command and linked into its extension in
        ``stage_dir``, with gcov's counters when ``coverage``."""
        build = self._target_build(target)
        if isinstance(build, _build.BuildProblem):
            return _Failure(build.outcome, build.reason, detail=build.detail)
        if build.module is None or build.consumer is None:
            return _Failure(la.Outcome.UNSUPPORTED,
                            f"the project's build does not make {target} into a Python "
                            "extension, so Python tests cannot load it")
        variant = _build.COVERAGE if coverage else _build.PLAIN
        target_object = self._compile_target(build, content, stage_dir / "target", variant,
                                             deadline)
        if isinstance(target_object, _Failure):
            return target_object
        extension = self._link_extension(build, target_object, stage_dir, coverage=coverage,
                                        deadline=deadline)
        if isinstance(extension, _Failure):
            return extension
        return _Extension(build, target_object, extension)

    def _link_extension(self, build, target_object: Path, stage_dir: Path, *, coverage: bool,
                       deadline: float) -> "Path | _Failure":
        """The extension linked in ``stage_dir`` by the recorded link from
        ``target_object``. With ``coverage`` the link adds gcov's runtime and
        its exported dump and reset (``_gcov_exports``)."""
        exports = self._gcov_exports(deadline) if coverage else None
        if isinstance(exports, _Failure):
            return exports
        extension = stage_dir / "ext" / build.artifact.name
        extension.parent.mkdir(parents=True, exist_ok=True)
        done = self._compile(build.artifact_command(target_object, extension, coverage=coverage,
                                                    exports=exports),
                             build.consumer.cwd, deadline)
        if done is None:
            return _Failure(la.Outcome.TIMED_OUT, "the run reached its timeout while linking "
                            "the extension")
        if done.returncode != 0:
            text = (done.stdout or "") + (done.stderr or "")
            return self._step_failure(la.Outcome.INFRASTRUCTURE_FAILURE,
                                      f"the extension {build.module} does not link", text)
        return extension

    def _gcov_exports(self, deadline: float) -> "Path | _Failure":
        """The object of ``data/gcov_exports.c``: gcov's dump and reset under
        the adapter's own names. A link that keeps an archive's symbols local,
        as python-build-standalone's ``-Wl,--exclude-libs,ALL`` keeps
        libgcov's, would otherwise hide them from the counters helper."""
        return self._data_object("gcov_exports", "the adapter's gcov exports",
                                 "the adapter's gcov exports do not compile", deadline)

    def _data_object(self, name: str, shown: str, failed: str,
                     deadline: float) -> "Path | _Failure":
        """The object of the adapter's ``data/<name>.c``, compiled once with the
        context's compiler: ``shown`` names it in a timeout's reason, and
        ``failed`` is the reason of a compile that fails."""
        with self._lock:
            if name not in self._data_objects:
                source = Path(__file__).with_name("data") / f"{name}.c"
                output = self._build_dir() / f"{name}.o"
                done = self._compile([self._toolchain().compiler, "-fPIC", "-O0", "-c",
                                      str(source), "-o", str(output)], str(output.parent),
                                     deadline)
                if done is None:
                    return _Failure(la.Outcome.TIMED_OUT, "the run reached its timeout while "
                                    f"compiling {shown}")
                self._data_objects[name] = output if done.returncode == 0 else (
                    self._step_failure(la.Outcome.INFRASTRUCTURE_FAILURE, failed,
                                       (done.stdout or "") + (done.stderr or "")))
            return self._data_objects[name]

    def _inner_request(self, request, deadline: float):
        """The Python adapter's request of a run: its own runner options, no
        coverage target (the C adapter measures the target), the time left."""
        from dataclasses import replace

        options = request.runner_options
        inner = options.inner if isinstance(options, CRunOptions) else options
        fields = {"runner_options": inner,
                  "timeout_s": max(1.0, deadline - time.monotonic())}
        if hasattr(request, "coverage_target"):
            fields["coverage_target"] = None
        return replace(request, **fields)

    def _merged_counts(self, build, target_object: Path, directories: Sequence[Path],
                       counts_dir: Path) -> _gcov.Counts:
        """The target's counts summed over every counters file under
        ``directories``: a C run's complete processes, or a Python run's
        tests and what ran outside them. Zeros where none ran."""
        notes = target_object.with_suffix(".gcno")
        found = sorted(path for directory in directories if directory.is_dir()
                       for path in directory.rglob(notes.stem + ".gcda"))
        if not found:
            return _gcov.empty(self._read_counts(build, target_object, None,
                                                 counts_dir / "notes"))
        total = self._read_counts(build, target_object, found[0], counts_dir / "0")
        for index, path in enumerate(found[1:], 1):
            total = total.merged(self._read_counts(build, target_object, path,
                                                   counts_dir / str(index)))
        return total

    def _extension_identity(self, run: SuiteRun, identity) -> la.ObservationIdentity:
        """The Python run's identity, with the target and the C toolchain."""
        from dataclasses import replace

        found = self._toolchain()
        version = found.version if isinstance(found, _toolchain.Toolchain) else "unknown"
        inner = run.identity
        toolchain = replace(inner.toolchain, tools=(*inner.toolchain.tools, ("gcc", version),
                                                    ("gcov", version)))
        return la.ObservationIdentity(target=identity.target, suite=inner.suite,
                                      toolchain=toolchain, instrument=inner.instrument,
                                      execution=inner.execution)

    def _extension_evidence(self, request: PerTestEvidenceRequest,
                            started: float) -> PerTestEvidence:
        """Each Python test's own coverage of the target, from the counters the
        plugin wrote around it. What ran outside every test (import,
        collection) belongs to no test."""
        from langadapter.runs import items_outside_tests

        identity = self._identity(request, coverage=True)
        refused = KILLS_UNUSABLE if request.mutation_run is not None else KILLS_NOT_REQUESTED

        def ended(outcome, reason, stopping=()) -> PerTestEvidence:
            return PerTestEvidence(outcome, reason, (), (), (), refused, (), None, None, identity,
                                   stopping_tests=tuple(stopping))

        gate = self._gate()
        if gate is not None:
            return ended(*gate)
        target = self._relative_target(request.target)
        root = Path(self.context.root)
        deadline = started + request.timeout_s
        inner = SuiteRunRequest(
            suite=request.suite, timeout_s=request.timeout_s,
            runner_options=(request.runner_options.inner
                            if isinstance(request.runner_options, CRunOptions)
                            else request.runner_options),
            module_under_test=request.module_under_test,
            isolation_config=request.isolation_config,
            remove_created_files=request.remove_created_files)
        with work_dir_tempdir(self.context, self._executor(), prefix="langadapter_c_") as scratch:
            scratch = Path(scratch)
            staged = self._stage_extension(target, (root / target).read_bytes(), scratch,
                                          coverage=True, deadline=deadline)
            if isinstance(staged, _Failure):
                return ended(staged.outcome, staged.text())
            harness = _extension.Harness(self, scratch / "python")
            problem = harness.counters_helper()
            if problem is not None:
                return ended(la.Outcome.INFRASTRUCTURE_FAILURE, problem)
            if request.warmup:
                harness.run(self._inner_request(inner, deadline), module=staged.build.module,
                            extension=staged.extension)
            counters = scratch / "counters"
            measured = time.monotonic()
            run, report = harness.run(
                self._inner_request(inner, deadline), module=staged.build.module,
                extension=staged.extension, counters=counters,
                strip=self._counter_variables(scratch, staged.target_object)["GCOV_PREFIX_STRIP"],
                per_test=True)
            problem = self._load_problem(report, staged, run.outcome)
            if problem is not None:
                return ended(la.Outcome.INFRASTRUCTURE_FAILURE, problem)
            if run.outcome is not la.Outcome.COMPLETED:
                return ended(run.outcome, run.reason_text(), run.failing_tests())
            kills, unmapped, killed = self._kill_items(request.mutation_run)
            counter_dirs = {PythonAdapter.test_of_nodeid(nodeid): name
                       for nodeid, name in report.counters.items()}
            own = self._extension_own_counts(staged, run, counter_dirs, counters, scratch)
            if not isinstance(own, list):
                return ended(*own)
            evidence = [_test_evidence(test, _gcov.observation(counts, root / target, root),
                                       killed, time_s) for test, counts, time_s in own]
            try:
                coverage = _gcov.observation(
                    self._merged_counts(staged.build, staged.target_object, [counters],
                                        scratch / "suite"), root / target,
                    root)
            except _gcov.CountsInconsistent as inconsistent:
                return ended(la.Outcome.INFRASTRUCTURE_FAILURE, str(inconsistent))
            identity = self._extension_identity(run, identity)
        untimed = tuple(e.test for e in evidence if e.time_ms is None)
        return PerTestEvidence(
            la.Outcome.COMPLETED, "", tuple(evidence), untimed, (), kills, unmapped,
            1000.0 * (time.monotonic() - measured), coverage, identity,
            outside_items=items_outside_tests(coverage, evidence, coverage.target.path))

    def _extension_own_counts(self, staged, run: SuiteRun, counter_dirs: Mapping[la.TestId, str],
                              counters: Path, scratch: Path):
        """Each Python test's own counts, from the folder the plugin wrote at
        the test's end, as ``(test, counts, time)``, else how per-test
        evidence ends. A test whose node id maps to no folder is a gap in the
        adapter's mapping, an infrastructure failure that names no test. A
        test whose folder holds no counters file did not reach the plugin's
        write, and one whose counts gcovr refuses is named too
        (``_gcov.named``, the run's tests one program)."""
        found, stops = [], []
        for index, outcome in enumerate(run.tests):
            name = counter_dirs.get(outcome.test)
            if name is None:
                return (la.Outcome.INFRASTRUCTURE_FAILURE,
                        f"{outcome.test.text}: no counters folder of the plugin belongs to it")
            counts = self._own_counts(staged.build, staged.target_object, counters / name,
                                      scratch / "read" / str(index), outcome.test)
            if isinstance(counts, str):
                stops.append(_gcov.Stop("", outcome.test.text, counts, counts=True))
            else:
                found.append((outcome.test, counts, outcome.time_s))
        if not stops:
            return found
        tests = {outcome.test.text: outcome.test for outcome in run.tests}
        return (la.Outcome.INFRASTRUCTURE_FAILURE, stops[0].what,
                tuple(tests[name] for _, name in _gcov.named(stops, {"": tuple(tests)})))

    # Mutation.

    def mutation_engines(self) -> Tuple[str, ...]:
        """Mull, with a usable GCC and Mull."""
        usable = (isinstance(self._toolchain(), _toolchain.Toolchain)
                  and isinstance(self._mull_tool(), _mull.Mull))
        return (_mull.ENGINE,) if usable else ()

    def mutation_operators(self, engine: str = _mull.ENGINE) -> OperatorCatalogue:
        """The operators a run measures by default: Mull's default group. The
        set ``all`` is Mull's whole group."""
        _require_engine(engine, _mull.ENGINE, "C")
        return OperatorCatalogue(engine, tuple(sorted(_mull.DEFAULT)))

    def open_mutation_session(self, engine: str = _mull.ENGINE, workers: int = 1,
                              **options: Any) -> MutationSession:
        """A session of Mull that measures ``workers`` mutants at once, each
        in a directory of its own."""
        _require_engine(engine, _mull.ENGINE, "C")
        if options:
            raise InvalidRequest("options", "the C engine takes no options beyond workers: "
                                 + ", ".join(sorted(options)))
        if type(workers) is not int or workers < 1:
            raise InvalidRequest("workers", "must be a positive integer")
        return CMutationSession(self, workers)

    # Capabilities.

    _PROVIDED = frozenset({"suite_runs", "coverage", "per_test_evidence", "mutation",
                           "per_test_kill_attribution", "source_merging",
                           "generated_suite_operations"})

    def capabilities(self) -> la.CapabilityReport:
        """What this context provides: runs, coverage, per-test evidence,
        merging and the generated suite operations with a usable GCC and
        gcov, mutation also with a usable Mull, and missing setup without
        them."""
        found = self._toolchain()
        interpreter = self.context.interpreter.path if self.context.interpreter else ""
        reason = self.execution_setup_issue() or (
            found.reason if isinstance(found, _toolchain.ToolchainProblem) else "")
        if reason:
            return la.CapabilityReport(
                language=self.language, interpreter=interpreter or "",
                outcome=la.Outcome.MISSING_SETUP, reason=reason, runtime=None,
                capabilities=tuple(
                    la.Capability(name, la.Availability.MISSING_SETUP, reason)
                    if name in self._PROVIDED else
                    la.Capability(name, la.Availability.UNSUPPORTED,
                                  "the C adapter does not provide it")
                    for name in CAPABILITIES),
                tools=(), branch_coverage="counters", mutation_engines=())
        mull = self._mull_tool()
        missing = {} if isinstance(mull, _mull.Mull) else dict.fromkeys(
            ("mutation", "per_test_kill_attribution"), mull.reason)

        def capability(name: str) -> la.Capability:
            if name not in self._PROVIDED:
                return la.Capability(name, la.Availability.UNSUPPORTED,
                                     "the C adapter does not provide it")
            if name in missing:
                return la.Capability(name, la.Availability.MISSING_SETUP, missing[name])
            return la.Capability(name, la.Availability.AVAILABLE)

        tools = (la.ToolVersion("gcov", None, found.version),)
        if isinstance(mull, _mull.Mull):
            tools += (la.ToolVersion("mull", None, mull.version),
                      la.ToolVersion("clang", None, mull.clang_version))
        return la.CapabilityReport(
            language=self.language, interpreter=found.compiler, outcome=la.Outcome.COMPLETED,
            reason="", runtime=found.version_line,
            capabilities=tuple(capability(name) for name in CAPABILITIES),
            tools=tools, branch_coverage="counters",
            mutation_engines=self.mutation_engines())


def _counts_inconsistent(run: SuiteRun, inconsistent: _gcov.CountsInconsistent,
                         refused: Sequence[Tuple[la.TestId, str]] = ()) -> SuiteRun:
    """``run`` whose coverage counters gcovr refused
    (``_gcov.CountsInconsistent``): no coverage, its tests as they ran, and
    gcovr's message in its ``coverage_counts_inconsistent`` diagnostic, then
    one such diagnostic with its test and message for each test ``refused``
    names, whose own counts gcovr refuses. The run's own verdict decides: a
    run that completed becomes an infrastructure failure with gcovr's
    message as its reason, and a run with another outcome, such as failing
    tests, keeps it."""
    from dataclasses import replace

    message = str(inconsistent)
    run = replace(run, coverage=None, diagnostics=(
        *run.diagnostics, RunDiagnostic("coverage_counts_inconsistent", message),
        *(RunDiagnostic("coverage_counts_inconsistent", own, test=test)
          for test, own in refused)))
    if run.outcome is not la.Outcome.COMPLETED:
        return run
    return replace(run, outcome=la.Outcome.INFRASTRUCTURE_FAILURE, reason=message,
                   reason_detail=())


def _suite_outcome(timed_out: bool, listing_errors: Sequence[str],
                   outcomes: Sequence[TestOutcome], counts: RunCounts,
                   timeout_s: float) -> Tuple[la.Outcome, str]:
    """A C run's outcome and reason: timed out, a test program that did not
    list its tests, no test, failing tests, else completed."""
    failing = (counts.failed or 0) + (counts.errors or 0)
    if timed_out:
        return la.Outcome.TIMED_OUT, f"the run reached its timeout of {timeout_s:g} s"
    if listing_errors:
        return (la.Outcome.COLLECTION_FAILED,
                "a test program did not list its tests: " + "; ".join(listing_errors))
    if not outcomes:
        return la.Outcome.NO_TESTS, "no test"
    if failing:
        return la.Outcome.TEST_FAILURES, f"{failing} of {len(outcomes)} tests did not pass"
    return la.Outcome.COMPLETED, ""


def _test_evidence(test: la.TestId, own, killed, time_s: Optional[float]) -> TestEvidence:
    """A test's evidence: the lines and branches its own counters ``own``
    covered, the mutants it ``killed`` and its time."""
    items = ({LineItem(own.target.path, line) for line in own.covered}
             | set(own.taken) | killed.get(test, set()))
    return TestEvidence(test, frozenset(items),
                        1000.0 * time_s if time_s is not None else None)


def _test_names(source: str) -> List[str]:
    """The names of the utest.h tests of a test file, in order."""
    return [unit.name for unit in _syntax.macro_units(source.encode("utf-8"))
            if unit.macro in _syntax.TEST_MACROS]


@dataclass(frozen=True)
class _Failure:
    """Why a build, compile or link ended a run: its outcome, reason, the
    tool's output and the lines that matter."""

    outcome: la.Outcome
    reason: str
    output: str = ""
    detail: Tuple[str, ...] = ()
    #: ``compile_error`` for a test file that does not compile or link.
    diagnostics: Tuple[RunDiagnostic, ...] = ()

    def text(self) -> str:
        return with_detail(self.reason, self.detail)

    def shown(self) -> str:
        """The failed run's output: the reason and the lines that matter
        first, so the output's start names the error, then the tool's whole
        output. Without either, empty."""
        if not (self.output or self.detail):
            return ""
        return "".join(f"{line}\n" for line in (self.reason, *self.detail)) + self.output


@dataclass(frozen=True)
class _Prepared:
    """A run's build: the target's recorded build, its object, and each test
    file's program and object."""

    build: Any
    target_object: Path
    programs: Mapping[str, Path]
    #: Each test file's object, by the file.
    objects: Mapping[str, Path]
    #: The suite's root, under which ld's lines name a test file.
    suite_root: str


@dataclass
class _WholeRuns:
    """The whole runs of a suite's test files (``CAdapter._whole_runs``): each
    test's outcome by file, whether the timeout ended the run, the counters
    directory of each file's complete process and whether they are the run's,
    the problems of programs that ended badly with no test to blame, and
    every program's output in the order they ran."""

    coverage: bool
    deadline: float
    runs: List[Tuple[str, Any]] = field(default_factory=list)
    timed_out: bool = False
    measured: bool = True
    counters: Dict[str, Path] = field(default_factory=dict)
    problems: List[str] = field(default_factory=list)
    output: List[str] = field(default_factory=list)

    def end(self, file: str, kept: Mapping[str, Any], *, timed_out: bool = False,
            measured: bool = True) -> None:
        """``file``'s tests with their outcomes ``kept``, in the order they
        were first known."""
        self.runs += [(file, one) for one in kept.values()]
        self.timed_out = self.timed_out or timed_out
        self.measured = self.measured and measured and not timed_out


def _bad_end(run: "_utest.ProgramRun", counters: Optional[Path], target_object: Path) -> str:
    """Why a run without a crash is not complete, or "": a signal ended the
    program, or its status is not 0 although no test failed, as after an
    exit handler. A status beside failing tests is no bad end, since a main
    of the file's own may return any status: the result lines decide. With
    coverage asked, a program that wrote no counters could not write them,
    as after a lowered open-files limit."""
    failed = any(one.outcome == "failed" for one in run.tests)
    if run.code is not None and (run.code < 0 or (run.code != 0 and not failed)):
        return _utest.program_ended(run.code)
    if counters is not None and _gcov.counters_file(
            counters, target_object.with_suffix(".gcno")) is None:
        return "the program wrote no coverage counters"
    return ""


def _with_programs(run: SuiteRun, whole: _WholeRuns) -> SuiteRun:
    """``run`` with the output of every program its whole runs started, in the
    order they ran, and the problems of programs that ended badly with no
    test to blame: a run that completed becomes an infrastructure failure
    with them as its reason, and a run with another outcome adds them to its
    reason."""
    from dataclasses import replace

    run = replace(run, output="".join(_utest.line_ended(text) for text in whole.output))
    if not whole.problems:
        return run
    message = "; ".join(whole.problems)
    if run.outcome is la.Outcome.COMPLETED:
        return replace(run, outcome=la.Outcome.INFRASTRUCTURE_FAILURE, reason=message)
    return replace(run, reason=f"{run.reason}; {message}" if run.reason else message)


@dataclass(frozen=True)
class _Extension:
    """A build of the target's extension for one run: the recorded build, the
    target's object and the extension linked from it."""

    build: Any
    target_object: Path
    extension: Path
