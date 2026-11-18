"""The Python adapter bound to one project context. Its language facts and
capability report come from the context's own interpreter, locally or in the
container, never from the calling process.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from importlib import util as importlib_util
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Tuple

from langadapter import _process_tree
from langadapter.adapter import ProjectSetting
from langadapter.capabilities import (
    Availability, Capability, CapabilityReport, LanguageFacts, ToolVersion,
)
from langadapter._environment import run_launch_environment
from langadapter._execution_scope import reraise_unresolved
from langadapter._prepared_copy import CreatedFiles
from langadapter.context import ContainerExecution, ProjectContext
from langadapter.execution import (
    ENV_PREFIX, Executor, _mounted_work_dir, executor_for, path_separator,
    prepend_path, utf8_environment, work_dir_tempdir,
)
from langadapter.outcomes import ToolchainIdentity, Outcome
from langadapter.versions import (
    capability_runtime, facts_runtime, python_line, python_version,
)
from langadapter.python._output import UNSOUND_TEST_PATTERNS
from langadapter.python._plugins import SUITE_INI_TEXT, test_of_nodeid
from langadapter.python._project_files import (IMPORT_ROOT_VARIABLE, checked_import_root,
                                               import_root_name)
from langadapter.python._sources import PythonSources
from langadapter.python._run_definition import DISABLED_PLUGINS, run_environment
from langadapter.python.project import (
    _TIMING_PLUGINS, _loaded_timing_plugins, isolation_args, project_pytest_config,
)
from langadapter.python._profile import (
    COLLECTION_UNIT, SUPPORT, TOOL_NAMES, PythonProfile,
)
from langadapter.python._validity import (
    POLICY_VERSION as VALIDITY_POLICY_VERSION,
)

# Runs in the project interpreter: its implementation, version and the
# installed versions of the pinned tools, as one JSON line.
_PROBE = """\
import json, platform
from importlib import metadata
tools = {}
for name in NAMES:
    try:
        tools[name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        tools[name] = None
print(json.dumps({"implementation": platform.python_implementation(),
                  "version": platform.python_version(), "tools": tools}))
"""

# Runs in the project interpreter: whether its pytest has the timeout
# mark, from the plugins pytest loads (the pytest11 entry points).
_TIMEOUT_PROBE = """\
from importlib import metadata
points = metadata.entry_points()
group = (points.select(group="pytest11") if hasattr(points, "select")
         else points.get("pytest11", ()))
print(any(point.name == "timeout" for point in group))
"""

#: The toolkit text's timeout mark, which only pytest-timeout provides.
_TIMEOUT_MARK = "@pytest.mark.timeout, "

#: Python's words for its tests (``langadapter.vocabulary``), for a project
#: whose pytest has the timeout mark (``PythonAdapter.test_vocabulary``).
#: Caller-facing text: changed only on purpose.
VOCABULARY: Dict[str, str] = {
    "valid_source": "valid Python",
    "real_source": "real Python",
    "test_declaration": "``def test_*(...)``",
    "test_declaration_full": "``def test_*(...):`` function",
    "collection_rule": (
        "Pytest only collects functions whose names start with "
        "``test_`` (or methods on classes starting with ``Test``)."
    ),
    "fixture_term": "fixtures",
    "parametrize_term": "``@pytest.mark.parametrize``",
    "single_file_rule": (
        "Produce one ``test_*.py`` file. Do not create helper files "
        "or runner scripts."
    ),
    "test_file": "``test_<target_stem>.py``",
    "import_rule": "imports the target module",
    "load_error": "collection error",
    # A fake installed in ``builtins`` itself would also reach pytest and the
    # measurement tools running in the same process.
    "fake_builtin_rule": (
        "- When a test must fake a built-in function, replace it only in the\n"
        "  module under test, for example\n"
        "  ``monkeypatch.setattr(module, \"len\", fake, raising=False)``, not in\n"
        "  ``builtins``. To simulate a missing optional package, use\n"
        "  ``monkeypatch.setitem(sys.modules, \"name\", None)``."
    ),
    "exception_assertion": "``pytest.raises``",
    "timeout_marker": "``@pytest.mark.timeout({seconds})``",
    "parse_error_hints": (
        "missing colon at end of def/if/for, unbalanced "
        "brackets/parens/quotes, mismatched indentation, "
        "accidental code-fence remnants"
    ),
    "test_dependencies": (
        "Test toolkit (always available):\n"
        "  - pytest (the runner; @pytest.fixture, "
        "@pytest.mark.parametrize, @pytest.mark.timeout, "
        "pytest.raises)\n"
        "  - unittest.mock (stdlib; Mock, MagicMock, patch, "
        "patch.object, sentinel)\n"
        "The project's own ``import`` graph is also available "
        "(import the module under test directly). Third-party "
        "libraries beyond pytest + unittest.mock are only "
        "available if the project's ``requirements.txt`` / "
        "``pyproject.toml`` declares them; check before "
        "importing unfamiliar names."
    ),
    # The policy python/_validity.py enforces on a test's use of its target.
    "validity_rule": (
        "Allowed: the target's runtime behaviour and documented metadata "
        "(attributes, __dict__ and vars(), __name__, __doc__, annotations, "
        "inspect.signature). Forbidden: reading or analysing its source text "
        "(inspect.getsource, its file) or bytecode (__code__, dis), executing "
        "copied source, or attributing synthetic code to its source locations."
    ),
    # What no run does per test, so a test must: restore module state and
    # library settings, and seed or configure what it uses (the run as
    # delivered, Python's normal run, seeds the global generators once per
    # session, no other run seeds them, and dask keeps its default scheduler).
    "stands_alone_advice": (
        '  1. A test must leave the module under test as it found it. Change\n'
        '     it only through monkeypatch, which restores what it set, or\n'
        '     restore it in a fixture; reloading the module in place replaces\n'
        '     every function and class the later tests use. To run import-time\n'
        '     code (an ``if TYPE_CHECKING:`` block, an ``except ImportError:``\n'
        '     fallback), reload it in a fixture that first saves\n'
        '     ``dict(module.__dict__)`` as ``saved`` and afterwards restores it\n'
        '     with ``module.__dict__.clear()`` and ``module.__dict__.update(saved)``.\n'
        '  2. A test must restore a library setting it changes: use the\n'
        "     library's context manager (np.errstate, np.printoptions,\n"
        '     xr.set_options) or save and restore the setting in a fixture.\n'
        "  3. A test must not rely on the measurement's fixed random seed or on\n"
        "     dask's default scheduler: seed or configure what it uses."
    ),
    "load_error_patterns": (
        '  - ImportError -> verify the name actually exists in the\n'
        '    target module / fix the import path.\n'
        '  - SyntaxError / IndentationError -> check the working file for\n'
        '    unbalanced brackets, bad indentation, stray characters from\n'
        '    a previous edit.\n'
        '  - NameError at module top-level -> ensure the name is\n'
        "    imported / defined before it's used."
    ),
}


class PythonAdapter(PythonSources):
    """Python projects: pytest, coverage.py and Cosmic Ray in the project
    environment, tree-sitter and the Python parser in this process."""

    language = "python"
    file_extensions = (".py",)
    #: Its structural metrics count code lines and complexity with lizard
    #: (``sources._StructuralProvider``).
    _lizard_metrics = True
    source_execution_kinds = ("local", "container")
    source_merge_separator = "#"
    test_execution_kinds = ("local", "container")
    native_mutation_engines = ("cosmic_ray",)
    mutation_execution_kinds = ("local", "container")
    mutation_evidence_granularity = ("aggregate", "per_mutant", "per_test")
    test_evidence_granularity = (
        ("run_suite", ("aggregate", "per_test")), ("run_as_delivered", ("aggregate", "per_test")),
        ("benchmark", ("aggregate",)), ("per_test_evidence", ("per_test",)),
    )
    #: What the Python adapter supports (``LanguageSupport``).
    support = SUPPORT
    #: The version of the generated-test validity policy whose findings
    #: ``analyze_validity`` and ``screen_source`` report.
    validity_policy_version = VALIDITY_POLICY_VERSION
    # Facts (``langadapter.hooks``).
    display_name = "Python"
    #: The program that runs the tests. The project's interpreter is the
    #: context's, found as the project setup finds it.
    runtime = "python"
    test_file_name = "test_{stem}.py"
    test_framework = "pytest"
    code_fence = "python"
    collection_unit = COLLECTION_UNIT
    reuses_bytecode = True
    copies_external_suite = True
    #: Its run as delivered takes a session seed (``run_as_delivered``).
    seeds_delivered_runs = True
    #: Per-test evidence keeps the coverage contexts no reported test
    #: claims, a test run under its own context but missing from the report
    #: (``LanguageFacts.unmatched_evidence_kept``).
    unmatched_evidence_kept = True

    def test_vocabulary(self) -> Dict[str, str]:
        """Python's words for its tests (``VOCABULARY``), without the timeout
        mark where the project's pytest lacks it."""
        words = dict(VOCABULARY)
        if not self._timeout_mark():
            words["timeout_marker"] = ""
            words["test_dependencies"] = words["test_dependencies"].replace(_TIMEOUT_MARK, "")
        return words

    def _timeout_mark(self) -> bool:
        """Whether pytest-timeout's plugin is registered in the project
        environment, read once. False when the environment cannot say, so no
        text names a mark that may not exist."""
        if self._timeout_plugin is None:
            try:
                done = self.executor.run(
                    [self.interpreter, *self.context.interpreter_flags, "-c", _TIMEOUT_PROBE],
                    timeout=60)
                self._timeout_plugin = (done.returncode == 0
                                        and (done.stdout or "").strip().endswith("True"))
            except Exception as exc:  # the interpreter did not run
                reraise_unresolved(exc)
                self._timeout_plugin = False
        return self._timeout_plugin

    def test_id_text(self, test) -> str:
        """A test's dotted id, as coverage's contexts and the kill matrices
        write it: the file's path without its suffix, then the qualified
        name (``tests.test_calc.TestCalc.test_adds``)."""
        from pathlib import PurePosixPath
        return ".".join((*PurePosixPath(test.file).with_suffix("").parts, test.qualname))

    def kill_tokens(self, kill_matrix, test_ids):
        """The kill tokens of the recorded killing tests, and a crash token
        (``C:<mutant id>``) for the test a kill without a named killer ended
        in (``active_at_end``), matched as coverage's contexts are."""
        from pathlib import PurePosixPath
        from langadapter.python.cosmic_ray import _match_test_context

        tokens, unmatched = super().kill_tokens(kill_matrix, test_ids)
        found = {test: set(items) for test, items in tokens.items()}
        for row in kill_matrix.get("mutants") or ():
            ended = str(row.get("active_at_end") or "")
            file, *names = ended.split("::")
            if row.get("id") is None or not file or row.get("killers") or row.get("killer"):
                continue
            if names:
                names[-1] = names[-1].split("[", 1)[0]
            test = _match_test_context(".".join((PurePosixPath(file.replace("\\", "/")).stem,
                                                 *names)), set(test_ids))
            if test is not None:
                found.setdefault(test, set()).add(f"C:{row['id']}")
        return {test: frozenset(items) for test, items in found.items()}, unmatched

    def request_options(self, purpose: str, *, target: Optional[str] = None,
                        traceback: Optional[str] = None, stop_at_first_failure: bool = False,
                        record_leaks: bool = False, session_seed: Optional[int] = None,
                        reverse_order: bool = False,
                        leave_out_timing_plugins: bool = False) -> Dict[str, Any]:
        """The isolated pytest configuration, module under test and pytest
        options of each kind of run, logfire and anyio plugins off: a test run
        with the caller's traceback (short by default), first-failure stopping
        and leak record, a run as delivered with the caller's traceback
        (pytest's own by default), first-failure stopping, session seed,
        order and leak record and, when the caller names a target, the module
        under test, which in a prepared project also decides which file the
        run imports (the copy's) and is the module the leak record compares.
        Timing and a coverage matrix without a traceback. Test runs,
        failing-tests runs, per-test evidence and runs as delivered remove
        the files tests create. The project's own run keeps its configuration
        and disables no plugin. ``leave_out_timing_plugins`` reaches every
        purpose's pytest options."""
        from langadapter.python.project import PytestOptions

        super().request_options(purpose)
        plugins = {"disabled_plugins": DISABLED_PLUGINS,
                   "leave_out_timing_plugins": leave_out_timing_plugins}
        if purpose == "run":
            return {"isolation_config": True, "module_under_test": target,
                    "remove_created_files": True,
                    "record_leaks": record_leaks,
                    "runner_options": PytestOptions(
                        traceback=traceback or "short",
                        stop_at_first_failure=stop_at_first_failure, **plugins)}
        if purpose in ("failing", "evidence"):
            return {"isolation_config": True, "module_under_test": target,
                    "remove_created_files": True,
                    "runner_options": PytestOptions(**plugins)}
        if purpose == "delivered":
            module = {"module_under_test": target} if target else {}
            leaks = {"record_leaks": True} if record_leaks else {}
            return {"isolation_config": True, "remove_created_files": True,
                    "session_seed": session_seed, "reverse_order": reverse_order,
                    **leaks, **module,
                    "runner_options": PytestOptions(
                        traceback=traceback or "auto",
                        stop_at_first_failure=stop_at_first_failure, **plugins)}
        if purpose == "collect":
            return {"isolation_config": True,
                    "runner_options": PytestOptions(**plugins)}
        if purpose == "project":
            # The project's configuration and every plugin it loads.
            return {"runner_options": PytestOptions(leave_out_timing_plugins=leave_out_timing_plugins)}
        return {"isolation_config": True, "module_under_test": target,
                "runner_options": PytestOptions(traceback="no", **plugins)}

    def collection_text(self, run) -> str:
        """pytest's collection exception summary (``CollectionRun.diagnostic``)."""
        return run.diagnostic

    def salvage_report(self, source: str):
        """``python.source_analysis.salvage_syntax`` with the project
        interpreter's verdicts through ``sources.validate_source``. A verdict it
        could not give reads ``timeout`` or ``validator_error`` with its reason."""
        import hashlib

        from langadapter.adapter import SalvageReport
        from langadapter.python.source_analysis import salvage_syntax
        from langadapter.sources import SourceStatus, SourceValidation as Validation

        def validate(candidate):
            observation = self.sources.validate_source(candidate)
            if not observation.completed:
                return Validation(
                    "timeout" if observation.status is SourceStatus.TIMEOUT else "validator_error",
                    observation.reason, 0, 0,
                    observation.parser or "validator identity unavailable",
                    hashlib.sha256(candidate.encode("utf-8")).hexdigest())
            verdict = observation.value
            return Validation(verdict.status, verdict.message, verdict.line, verdict.column,
                              verdict.interpreter, verdict.source_sha256)

        result = salvage_syntax(source, validate)
        return SalvageReport(result.source, result.status, result.repairs,
                             result.validation.message)

    def skip_removal(self, source: str):
        """``python.source_analysis.strip_module_level_skips``, by the
        calling process's parser."""
        from langadapter.python import source_analysis
        result = source_analysis.strip_module_level_skips(source)
        return result.source, result.count

    def collision_dependents(self, source: str, name: str, measured):
        """``python.source_analysis.collision_dependents``."""
        from langadapter.python import source_analysis
        return source_analysis.collision_dependents(source, name, measured)

    def collision_resolution(self, sources, *, localize_autouse: bool = False,
                             preference=None):
        """``python.source_analysis.resolve_collisions``, by the calling
        process's parser, always completed."""
        from langadapter.python import source_analysis
        from langadapter.sources import SourceObservation, SourceStatus
        return SourceObservation(SourceStatus.COMPLETED, value=source_analysis.resolve_collisions(
            sources, localize_autouse=localize_autouse, preference=preference))

    coverage_matrix_in_place = True

    #: The build script a project's directory may hold beside its sources.
    non_source_files = ("setup.py",)

    #: A Python project's settings.
    project_settings = (
        ProjectSetting("python_executable", "The project's Python interpreter. Empty: the "
                       "environment uv provisions for python_version, else the project's "
                       "only environment, its .venv or the one activated in the shell. The "
                       "environment the caller runs in counts only inside the project. Two "
                       "environments, or none, refuse the run.", "executable",
                       check="an interpreter that starts", host_path="path"),
        ProjectSetting("python_version", "A Python version, for example 3.12, for which uv "
                       "provisions an environment when python_executable is empty.", "text",
                       check="a version uv can provision"),
        ProjectSetting("requirements", "Requirements file for the environment provisioned by "
                       "python_version.", "path", check="a file", host_path="host file"),
        ProjectSetting("python_import_root", "The directory below the project from which its "
                       "Python package is imported, for example src. Empty imports from the "
                       "project directory itself.", "path", check="a directory below the project"),
        ProjectSetting("python_test_environment", "Variables the processes of the Python "
                       "adapter's own runs get, as NAME=value words, for example "
                       "PURE_PYTHON=1. Python tests of a C extension do not get them.", "text",
                       check="NAME=value words"),
        ProjectSetting("frozen_module_strategy", "unfreeze runs the project's Python with -X "
                       "frozen_modules=off, so coverage and mutation treat the standard "
                       "library's frozen modules as ordinary files. skip leaves them frozen.",
                       "choice", default="unfreeze", choices=("unfreeze", "skip"),
                       check="one of the choices"),
        ProjectSetting("pytest_config", "auto uses the target project's pytest configuration "
                       "when present. An explicit path selects a configuration file, and none "
                       "selects pytest defaults.", "path", default="auto",
                       check="auto, none or a file"),
    )

    @classmethod
    def context_environment(cls, values, *, container=False):
        """The variables ``python_test_environment`` gives the processes of
        this adapter's runs, and ``python_import_root`` as
        ``IMPORT_ROOT_VARIABLE``: the directory below the project root, ``src``
        for ``src/`` (. and none name the project itself, which needs no
        variable). A directory outside the project is refused."""
        from langadapter.adapter import environment_assignments

        found = environment_assignments(values.get("python_test_environment"),
                                        "python_test_environment")
        root = checked_import_root(values.get("python_import_root"))
        return {**found, IMPORT_ROOT_VARIABLE: root} if root else found

    test_of_nodeid = staticmethod(test_of_nodeid)
    """``test_of_nodeid(nodeid)``: the test a pytest node id names, relative to
    the suite's root, ``file::Class::function[params]``, the parameters apart.
    Another adapter whose Python tests pytest runs reads their node ids this
    way."""

    @classmethod
    def discover_runtime(cls, root, values, *, container=False, environment=None,
                         resolve_path=None):
        """The selected interpreter, else a provisioning request for
        python_version (with its requirements), else the project's only
        environment. InvalidRequest
        names python_executable when none can be chosen. On this machine a
        selection is the path ``resolve_path`` gives and must exist. In a
        container it is required, taken as given, and as an absolute POSIX path
        can supervise the container's commands. Under unfreeze the interpreter runs
        with -X frozen_modules=off. The test framework follows pytest_config."""
        return cls._runtime(root, values, container=container, environment=environment,
                            resolve_path=resolve_path, describe=False)

    @classmethod
    def describe_runtime(cls, root, values, *, container=False, environment=None,
                         resolve_path=None):
        """The selection as ``discover_runtime`` reads it, without checking
        that a selected interpreter exists and without discovering one: no
        interpreter when none is selected and no python_version asks for
        one."""
        return cls._runtime(root, values, container=container, environment=environment,
                            resolve_path=resolve_path, describe=True)

    @classmethod
    def _runtime(cls, root, values, *, container, environment, resolve_path, describe):
        from pathlib import Path, PurePosixPath

        from langadapter.adapter import RuntimeChoice
        from langadapter.context import Interpreter, ProvisioningRequest, TestFrameworkConfig
        from langadapter.errors import InvalidRequest
        from langadapter.python.project import _discovered_interpreter

        selected = str(values.get("python_executable") or "").strip()
        version = str(values.get("python_version") or "").strip()
        if selected and container:
            interpreter = Interpreter(path=selected)
        elif selected:
            path = (resolve_path(selected) if resolve_path is not None else Path(selected))
            path = path.absolute()
            if not describe and not path.exists():
                raise InvalidRequest("python_executable",
                                     f"points at {path}, which does not exist")
            interpreter = Interpreter(path=str(path))
        elif version:
            interpreter = Interpreter(provisioning=ProvisioningRequest(
                version, values.get("requirements") or None))
        elif describe:
            interpreter = None
        elif container:
            raise InvalidRequest("python_executable", "no interpreter is selected for the "
                                 "project in the container: select one")
        else:
            interpreter = Interpreter(path=_discovered_interpreter(root, environment or {}))
        flags = (("-X", "frozen_modules=off")
                 if values.get("frozen_module_strategy") == "unfreeze" else ())
        config = str(values.get("pytest_config", "auto")).strip()
        framework = (TestFrameworkConfig(source="isolated") if config.lower() == "none" else
                     TestFrameworkConfig(path=config if config.lower() not in ("", "auto")
                                         else None))
        supervisor = (selected if container and selected and "\\" not in selected
                      and PurePosixPath(selected).is_absolute()
                      and ".." not in PurePosixPath(selected).parts else None)
        return RuntimeChoice(interpreter, flags, framework, supervisor)

    @classmethod
    def setup_questions(cls, project_dir, probe, *, consumer="the caller"):
        """The import directory and the interpreter."""
        from pathlib import Path
        from langadapter.python._questions import questions
        return questions(cls, Path(project_dir), probe, consumer)

    @classmethod
    def setup_problem(cls, setting, value, project_dir, probe):
        """The import directory must exist, the interpreter run and be
        supported. The other settings are checked by their kind."""
        from pathlib import Path
        from langadapter.python._questions import problem
        if setting in ("python_import_root", "python_executable"):
            return problem(cls, setting, value, Path(project_dir), probe)
        return super().setup_problem(setting, value, project_dir, probe)

    @classmethod
    def provision_runtime(cls, request, *, store, name, environment=None, log=None):
        """The interpreter of the environment uv makes for the requested
        Python version, with its requirements file and the tools LangAdapter
        pins for that Python."""
        from langadapter.python._provision import provision
        return provision(request.version, request.requirements, store=store, name=name,
                         environment=environment, log=log or (lambda line: None))

    def mutation_catalogue(self, config):
        """The catalogue ``cosmic_ray_operators`` names."""
        from langadapter.python._mutation import configured_catalogue
        return configured_catalogue(config)

    def named_operators(self, name: str):
        """Cosmic Ray's named operator set."""
        from langadapter.python._mutation import named_operators
        return named_operators(name)

    def mutation_session_attempts(self, config, *, configured_workers: int,
                                  effective_workers: int, cache_base: str, log=None):
        """Cosmic Ray's distributor and worker rule and its settings."""
        from langadapter.adapter import MutationAttempt
        from langadapter.python._mutation import session_attempts
        return tuple(MutationAttempt(options, workers, parallel, warning)
                     for options, workers, parallel, warning in session_attempts(
                         config, configured_workers=configured_workers,
                         effective_workers=effective_workers, cache_base=cache_base,
                         log=log))

    def mutation_dir_refusal(self, config, *, create: bool):
        """``cosmic_ray_local_session_dir``'s check."""
        from langadapter.python._mutation import session_dir_refusal
        return session_dir_refusal(config, create=create)

    def mutation_workers_refusal(self, config):
        """Why the distributor cannot run the configured workers, or None."""
        from langadapter.python._mutation import workers_refusal
        return workers_refusal(config)

    def setup_files(self, root: str, test_paths) -> Tuple[str, ...]:
        """The conftest files pytest loads between ``root`` and each test
        file (``python.project.loaded_setup_files``)."""
        from pathlib import Path

        from langadapter.python.project import loaded_setup_files
        return tuple(str(path) for path in loaded_setup_files(
            Path(root), [Path(path) for path in test_paths]))

    def shared_setup_files(self, directory: str) -> Tuple[str, ...]:
        """A prepared suite's conftest.py (``python.project.setup_files``)."""
        from langadapter.python.project import setup_files
        return setup_files(directory)

    def test_files(self, paths):
        """Each path, a directory replaced by the test files pytest collects in
        it, asked with a run's settings."""
        from langadapter.python.project import _collected_test_files
        return _collected_test_files(self, paths)

    def project_test_files(self):
        """The test files pytest collects in the project as its configuration
        bounds them, its ``testpaths`` included: a collection from the
        context's root with no file or directory named."""
        from langadapter.python.project import _collected_test_files
        return _collected_test_files(self, [self.context.root], configured=True)

    def coverage_texts(self, coverage) -> Dict[str, Tuple[Any, ...]]:
        """The covered statements, as Java's are (not the lines coverage.py
        traced, which include a docstring's line), and each missed arc, as
        ``exit:N`` for one that leaves the code object starting at line ``N`` (a
        function's ``def`` or first decorator), so a reader can name the function
        it leaves, also one never called. Caller-facing text."""
        if coverage is None:
            return {"covered_lines": (), "missing_branches": ()}
        return {"covered_lines": coverage.covered,
                "missing_branches": tuple(
                    f"{b.source}->{b.destination if b.destination > 0 else f'exit:{-b.destination}'}"
                    for b in coverage.missing_branches())}

    def delivered_case(self, test, outcome: str) -> Tuple[str, str, str]:
        """The module's stem and qualified name, the name with its parameters in
        brackets, and the outcome, an expected failure that failed as skipped."""
        from pathlib import Path
        return (".".join((Path(test.file).stem, test.qualname)),
                test.qualname.split(".")[-1] + (
                    f"[{test.params}]" if test.params is not None else ""),
                "skipped" if outcome == "xfailed" else outcome)

    @classmethod
    def import_statement(cls, target: str, root: str) -> str:
        """The dotted module of ``target`` from the project's ``root``, bound
        through importlib where the package binds that name itself.
        Caller-facing text."""
        from langadapter.python._sources import import_line
        return import_line(target, root)

    def code_smells(self, paths) -> Optional[int]:
        """The number of ruff's findings in ``paths``, or None when ruff is
        missing or fails."""
        from pathlib import Path

        from langadapter.python._sources import ruff_smell_count
        return ruff_smell_count(*[Path(path) for path in paths])

    def run_counts(self, run) -> Dict[str, Optional[int]]:
        """The counts of pytest's own summary, from the run record
        (``SuiteRun.runner_counts``), with collected the passed, failed,
        errored and skipped tests, less one per file pytest could not
        collect. All None for a run that ended before its session did."""
        counts = run.runner_counts
        if counts is None:
            return dict.fromkeys(("collected", "passed", "failed", "errors"))
        return {"collected": counts.collected + counts.skipped, "passed": counts.passed,
                "failed": counts.failed, "errors": counts.errors}

    def listed_failures(self, run):
        """The report's failed and errored cases, whatever the exit status (after
        pytest's internal error, the tests that ran before it. Its synthetic and
        nameless records are unmatched entries). None for a timeout, a collection
        error, no tests, a run that could not start or a report naming no test."""
        from langadapter.outcomes import Outcome

        if (run.outcome in (Outcome.TIMED_OUT, Outcome.COLLECTION_FAILED, Outcome.NO_TESTS,
                            Outcome.MISSING_SETUP, Outcome.UNSUPPORTED)
                or not run.tests):
            return None
        return tuple(case.test for case in run.tests if case.outcome in ("failed", "error"))

    def __init__(self, context: ProjectContext,
                 executor: Optional[Executor] = None) -> None:
        super().__init__(context)
        self.executor = executor or executor_for(context)
        self._lock = threading.Lock()
        self._runtime_record = None
        self._runtime_resolved = False
        self._timeout_plugin: Optional[bool] = None
        self._toolchain: Optional[ToolchainIdentity] = None
        self._created_files = CreatedFiles(context)
        self._pytest_config_cache: Dict[str, Optional[str]] = {}
        self._timing_plugins_cache: Dict[str, Tuple[str, ...]] = {}

    def inspect_project(self, request):
        """Start the selected interpreter in isolation and read its installed
        tool versions, without site or project imports. Without a selection the
        only environment found (the project's ``.venv`` or the activated one) is
        selected. Several need a choice. Installed metadata does not prove
        readiness: ``verify_project`` imports the tools."""
        from langadapter.python._setup import inspect
        return inspect(self, request)

    def verify_project(self, request):
        """Import the needed tools in a prepared copy, installing nothing. It
        does not certify project collection or measurements."""
        from langadapter.python._setup import verify
        return verify(self, request)

    @property
    def interpreter(self) -> str:
        """The project interpreter's path as the executing side sees it.
        A provisioning request must be fulfilled first."""
        self.context._require_execution()
        path = self.context.interpreter.path
        if path is None:
            raise RuntimeError(
                "the context's interpreter is a provisioning request that "
                "has not been fulfilled")
        return path

    def runtime_version(self) -> Optional[str]:
        """The interpreter's version line (``Python 3.10.16``), read once per
        instance: the context's stated version, else ``--version`` run where the
        project runs. None when it cannot be read, and in a source-only
        context, which has no interpreter."""
        record = self._runtime_line()
        return facts_runtime(record) if record is not None else None

    def choose_profile(self) -> PythonProfile:
        """The project interpreter's profile against the Python declaration, from
        versions this instance reads anyway, so it starts no process: the context's
        stated version, the library's own for its own interpreter, else the
        toolchain probe every run's identity carries (read before the first
        operation's timed interval). Only a program that cannot run that probe
        (Python 2, 3.7 and older) is asked for its version line."""
        if self.context.runtime_version or (
                not isinstance(self.context.execution, ContainerExecution)
                and self.interpreter == sys.executable):
            return PythonProfile(self._runtime_line(), self.support)
        toolchain = self.toolchain_identity()
        if toolchain.version != "unknown":
            return PythonProfile(python_version(toolchain.implementation, toolchain.version, "probe"),
                                 self.support)
        return PythonProfile(self._runtime_line(), self.support)

    def _runtime_line(self):
        """The ``Version`` of the interpreter's version line, or None."""
        with self._lock:
            if self._runtime_resolved:
                return self._runtime_record
            stated = self.context.runtime_version
            if stated:
                record = python_line(stated, "context")
            elif self.context.interpreter is None:
                # Source-only use: no interpreter to ask.
                record = None
            elif (not isinstance(self.context.execution, ContainerExecution)
                  and self.interpreter == sys.executable):
                # This process's own interpreter: its --version line, unstarted.
                record = python_line(f"Python {sys.version.split()[0]}", "library")
            else:
                try:
                    completed = self.executor.run(
                        [self.interpreter, *self._runtime_flags(), "--version"], timeout=30)
                    line = (completed.stdout or completed.stderr).strip()
                    record = python_line(line, "probe") if line else None
                except Exception as error:
                    # Every operation asks for the profile first. A command
                    # whose end is not confirmed is still reported.
                    reraise_unresolved(error)
                    record = None
            self._runtime_record = record
            self._runtime_resolved = True
            return record

    def language_facts(self) -> LanguageFacts:
        """Python's facts for this project: pytest, its test name patterns,
        the interpreter's version, and pytest-timeout's mark when its plugin
        is registered in the project environment."""
        record = self._runtime_line()
        return LanguageFacts(
            language="Python",
            test_framework="pytest",
            code_fence="python",
            runtime_version=self.runtime_version(),
            version_record=self._runtime_line(),
            test_file_patterns=("test_*.py", "*_test.py"),
            test_function_pattern="test_*",
            test_class_pattern="Test*",
            timeout_marker=("@pytest.mark.timeout({seconds})" if self._timeout_mark()
                            else None),
            language_level=("Python " + ".".join(str(n) for n in record.numbers[:2])
                            if record is not None else None),
            unmatched_evidence_kept=self.unmatched_evidence_kept,
            unsound_test_patterns=UNSOUND_TEST_PATTERNS,
        )

    def _probe(self) -> Tuple[Optional[dict], str]:
        """The project environment's interpreter and tool versions, or None
        with the reason the probe could not run."""
        code = _PROBE.replace("NAMES", repr(TOOL_NAMES))
        try:
            completed = self.executor.run(
                [self.interpreter, *self.context.interpreter_flags, "-c", code],
                timeout=60)
        except Exception as exc:  # the interpreter could not be started
            reraise_unresolved(exc)
            return None, f"the interpreter {self.interpreter} could not run: {exc}"
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip()[-500:]
            return None, (f"the interpreter {self.interpreter} exited with "
                          f"{completed.returncode}: {tail}")
        try:
            lines = (completed.stdout or "").strip().splitlines()
            return json.loads(lines[-1]), ""
        except (IndexError, ValueError):
            return None, (f"the interpreter {self.interpreter} printed no "
                          f"readable probe result")

    def capabilities(self) -> CapabilityReport:
        """What this adapter can do for its context, by a project probe."""
        where = self._where()
        structural = (importlib_util.find_spec("tree_sitter") is not None
                      and importlib_util.find_spec("tree_sitter_python")
                      is not None)
        source_merging = (
            Capability("source_merging", Availability.AVAILABLE)
            if structural else Capability(
                "source_merging", Availability.MISSING_SETUP,
                "tree-sitter and tree-sitter-python are not installed in "
                "the calling process"))
        probe, failure = self._probe()
        if probe is None:
            needs_environment = [
                Capability(name, Availability.MISSING_SETUP, failure)
                for name in ("suite_runs", "coverage", "per_test_evidence",
                             "per_test_kill_attribution", "mutation",
                             "generated_suite_operations")]
            return CapabilityReport(
                language=self.language, interpreter=self.interpreter,
                outcome=Outcome.INFRASTRUCTURE_FAILURE, reason=failure,
                runtime=None,
                capabilities=tuple(needs_environment) + (source_merging,),
                tools=tuple(ToolVersion(name, pinned, None)
                            for name, pinned in PythonProfile(None, self.support).tools.items()),
                branch_coverage="arcs", mutation_engines=())
        found = dict(probe.get("tools") or {})
        # The pins of the probed project Python, as the capability report
        # gives each found version beside its pin.
        pinned = PythonProfile(python_version(str(probe.get("implementation")),
                                              str(probe.get("version")), "probe"),
                               self.support).tools
        tools = tuple(ToolVersion(name, version, found.get(name))
                      for name, version in pinned.items())

        def needs(name: str, *required: str) -> Capability:
            absent = [tool for tool in required if not found.get(tool)]
            if not absent:
                return Capability(name, Availability.AVAILABLE)
            return Capability(
                name, Availability.MISSING_SETUP,
                f"{', '.join(absent)} not installed for {where}")

        capabilities = (
            needs("suite_runs", "pytest"),
            needs("coverage", "pytest", "coverage"),
            needs("per_test_evidence", "pytest", "coverage"),
            needs("per_test_kill_attribution", "pytest", "cosmic-ray"),
            needs("mutation", "pytest", "cosmic-ray"),
            source_merging,
            Capability("generated_suite_operations", Availability.AVAILABLE),
        )
        runtime = python_version(str(probe.get("implementation")), str(probe.get("version")), "probe")
        return CapabilityReport(
            language=self.language, interpreter=self.interpreter,
            outcome=Outcome.COMPLETED, reason="",
            runtime=capability_runtime(runtime), version_record=runtime,
            capabilities=capabilities, tools=tools, branch_coverage="arcs",
            mutation_engines=(("cosmic_ray",) if found.get("cosmic-ray")
                              else ()))

    def toolchain_identity(self) -> ToolchainIdentity:
        """The interpreter's implementation and version and the versions of
        the project environment's tools, probed once per instance. A probe
        that failed is asked again by the next call."""
        with self._lock:
            if self._toolchain is not None:
                return self._toolchain
            probe, _failure = self._probe()
            tools = tuple((name, version) for name, version
                          in sorted(((probe or {}).get("tools") or {}).items())
                          if version)
            identity = ToolchainIdentity.of(python_version(
                (probe or {}).get("implementation") or "unknown",
                (probe or {}).get("version") or "unknown", "probe"), tools)
            if probe is not None:
                self._toolchain = identity
            return identity

    def operation_readiness(self, name: str, report: CapabilityReport):
        """An execution-backed operation's readiness from the probe: missing
        setup when the probe did not complete, available for the syntax check
        and the target's identity, Cosmic Ray for the operator catalogue, and
        the probe's capabilities for the rest."""
        group, method = name.split(".")
        operations = getattr(self, group)
        if not operations.execution_backed(method):
            return None
        if report.outcome is not Outcome.COMPLETED:
            return Capability(name, Availability.MISSING_SETUP, report.reason or "runtime probe did not complete")
        if group == "sources":
            if method in {"validate_source", "target_identity"}:
                return Capability(name, Availability.AVAILABLE)
            requirements = ("source_merging", "suite_runs") if method == "prune_collection_breakers" else ("source_merging",)
            return self._capability_readiness(name, report, *requirements)
        if group == "tests":
            requirement = "per_test_evidence" if method == "per_test_evidence" else "suite_runs"
            return self._capability_readiness(name, report, requirement)
        if method == "operators":
            tool = next((tool for tool in report.tools if tool.name == "cosmic-ray"), None)
            return (Capability(name, Availability.AVAILABLE if tool.found else Availability.MISSING_SETUP,
                               "" if tool.found else "cosmic-ray is not installed for the project runtime")
                    if tool is not None else None)
        return self._capability_readiness(name, report, "mutation")

    # The project handle: each operation reads its interpreter, executor,
    # pytest configuration and work directory through these.

    def _executor(self) -> Executor:
        self.context._require_execution()
        return self.executor

    def _runtime_flags(self) -> List[str]:
        return list(self.context.interpreter_flags)

    def _exec_mode(self) -> str:
        return self.context.execution.kind

    def _container(self) -> bool:
        return self._exec_mode() == "container"

    def _env_name(self, name: str) -> str:
        return f"{ENV_PREFIX}{name}"

    def _read_project_file(self, path: str) -> Optional[str]:
        """A project file's text, read where the project lives. None when
        it is absent or unreadable."""
        try:
            if self._container():
                completed = self._executor().run(["cat", path], timeout=60)
                ok = getattr(completed, "returncode", 0) == 0
                return (getattr(completed, "stdout", "") or "") if ok else None
            candidate = Path(path)
            return (candidate.read_text(encoding="utf-8", errors="replace")
                    if candidate.is_file() else None)
        except Exception as exc:
            reraise_unresolved(exc)
            return None

    def _read_file_content(self, path: str) -> str:
        """A project source's text, read where the project lives: its bytes
        as UTF-8, an undecodable byte replaced, with universal newlines, the
        same text locally and from a container. Raises when it cannot be
        read."""
        if self._container():
            import base64
            completed = self._executor().run(
                [self.interpreter, *self._runtime_flags(), "-c",
                 "import base64,sys;sys.stdout.write(base64.b64encode("
                 "open(sys.argv[1],'rb').read()).decode())", str(path)],
                timeout=60)
            if completed.returncode != 0:
                raise OSError(
                    f"reading {path} in the container failed "
                    f"(rc={completed.returncode}): "
                    f"{(completed.stderr or '')[-300:]}")
            data = base64.b64decode(completed.stdout.strip())
        else:
            candidate = Path(path)
            if not candidate.exists():
                raise FileNotFoundError(f"File not found: {path}")
            data = candidate.read_bytes()
        return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")

    def _probe_dir(self, project_root: Optional[str]) -> Optional[str]:
        """Where the probes start the project's pytest: the repository
        inside a container, else ``project_root``."""
        execution = self.context.execution
        return (execution.repository_dir
                if isinstance(execution, ContainerExecution) else project_root)

    def _pytest_config_path(self, project_root: Optional[str]) -> Optional[str]:
        framework = self.context.test_framework
        if framework.source == "isolated":
            return None
        if framework.path:
            return framework.path
        return project_pytest_config(
            self._executor(), self.interpreter, self._probe_dir(project_root),
            self._pytest_config_cache, flags=self._runtime_flags())

    def _timing_plugin_flags(self, project_root: str, disabled) -> List[str]:
        """The flags that leave out the timing plugins the project's pytest
        loads and a run with the plugins ``disabled`` keeps
        (``PytestOptions.leave_out_timing_plugins``). The plugins are asked
        once per directory."""
        key = str(self._probe_dir(project_root)).rstrip("/")
        if key not in self._timing_plugins_cache:
            self._timing_plugins_cache[key] = _loaded_timing_plugins(
                self._executor(), self.interpreter, key, flags=self._runtime_flags())
        loaded = self._timing_plugins_cache[key]
        return [flag for name, flags in _TIMING_PLUGINS
                if name in loaded and name not in disabled for flag in flags]

    def _pytest_isolation_args(
        self,
        project_root: Optional[str] = None,
        suite_root: Optional[str] = None,
        *,
        execution_suite_root: Optional[str] = None,
    ) -> List[str]:
        return isolation_args(
            self._pytest_config_path(project_root), self._read_project_file,
            suite_root, execution_suite_root=execution_suite_root)

    def _suite_config(self, project_root: Optional[str], suite_root: str, *,
                      execution_suite_root: Optional[str] = None) -> Optional[str]:
        """The configuration every run of a suite names with ``-c``: the
        copy of the project's pytest configuration beside the suite
        (``isolation_args``), else the anchoring ``pytest.ini`` there, which
        stops pytest's search for a configuration above the suite. The
        anchor is written when absent, as a suite staged outside the
        project has one. Named under ``execution_suite_root`` (as a worker
        clone or a container sees the suite's directory) when given. None for a
        directory this host does not hold (a container's path outside the work
        directory), which it cannot anchor: pytest searches from there. The
        project's configuration is asked for before an anchor is written, and
        no anchor goes into the project's own directory when the project has a
        configuration, since it would hide that configuration from every later
        run's question."""
        # isolation_args begins with the cache provider's switch.
        project_copy = self._pytest_isolation_args(
            project_root, suite_root, execution_suite_root=execution_suite_root)[2:]
        anchor = Path(suite_root) / "pytest.ini"
        in_project = project_root is not None and Path(suite_root).resolve() == Path(project_root).resolve()
        if Path(suite_root).is_dir() and not anchor.exists() and not (project_copy and in_project):
            anchor.write_text(SUITE_INI_TEXT, encoding="utf-8")
        if project_copy:
            return project_copy[1]
        if not Path(suite_root).is_dir():
            return None
        if execution_suite_root is None:
            return str(anchor)
        return f"{str(execution_suite_root).replace(chr(92), '/').rstrip('/')}/pytest.ini"

    def _work_dir_tempdir(self, prefix: str):
        return work_dir_tempdir(self.context, self._executor(), prefix=prefix)

    def _prepend_pythonpath(self, env: Dict[str, str], entry: str) -> None:
        prepend_path(env, "PYTHONPATH", str(entry),
                     path_separator(self.context))

    def _import_root_name(self) -> str:
        """``python_import_root`` as this context names it
        (``_project_files.import_root_name``)."""
        return import_root_name(self.context)

    def _prepend_import_root(self, env: Dict[str, str], project_root: Any) -> None:
        """Put ``python_import_root`` below ``project_root`` first on
        ``env``'s PYTHONPATH, ahead of the root: the project's package is
        imported from there, while the runs' working directory, pytest
        configuration and copy stay the project's."""
        name = self._import_root_name()
        if not name:
            return
        if self._container():
            entry = (PurePosixPath(str(project_root).replace("\\", "/")) / name).as_posix()
        else:
            entry = str(Path(project_root) / name)
        self._prepend_pythonpath(env, entry)

    def _allowed_cpus(self, prefix: List[str], cwd: str,
                      env: Dict[str, str]) -> Optional[Tuple[int, ...]]:
        """The processors a process started with ``prefix`` where the project
        runs may use (its affinity, not those used), read by a short probe
        started as a benchmark's repetitions are, with their environment
        ``env``. None where the platform reports no affinity or the probe
        fails."""
        from langadapter import resources

        command = [*prefix, self.interpreter, *self._runtime_flags(), "-I", "-S", "-", "allowed"]
        source = Path(resources.__file__).read_text(encoding="utf-8")
        try:
            if self._container():
                completed = self._executor().run(
                    command, cwd=cwd, env=env, timeout=60, input=source)
            else:
                completed = _process_tree.run(
                    command, cwd=cwd, env=env, input=source,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            reraise_unresolved(exc)
            return None
        return resources._probed_allowed_cpus(completed)

    def _work_dir(self) -> Optional[str]:
        """The host directory shared with a container, or None. Cosmic Ray's
        engine reads it, and ``_container_work_dir``, through its adapter."""
        return getattr(self.context.execution, "work_dir", None)

    def _container_work_dir(self) -> Optional[str]:
        """Where a command in the container sees the work directory: the
        context's ``container_work_dir``, else ``container_work_dir_for``'s,
        as the executor rebases its arguments. None without a work directory."""
        if not self._work_dir():
            return None
        return _mounted_work_dir(self.context.execution)

    def _build_env(self, project_root: str) -> Dict[str, str]:
        """The environment of this adapter's Python subprocesses (test,
        coverage and benchmark runs): the context's environment
        (``run_launch_environment``), project_root
        first on PYTHONPATH (after ``python_import_root``'s directory below it),
        UTF-8, and the variables every test run has
        (``_run_definition.RUN_VARIABLES``). In container mode PYTHONPATH
        holds those two directories alone: the host's, forwarded past
        ``--cleanenv``, names host directories.
        """
        selected = self.context.environment
        env = run_launch_environment(selected)
        if self._exec_mode() == "container":
            env["PYTHONPATH"] = str(project_root)
        else:
            self._prepend_pythonpath(env, project_root)
        self._prepend_import_root(env, project_root)
        utf8_environment(env)
        run_environment(env)
        # Set only by the runs that ask for them: a parent process's value
        # never reaches another run (a per-test evidence run must not record).
        for name in ("LEAK_JOURNAL", "DELIVERED_SEED", "DELIVERED_ORDER"):
            env.pop(self._env_name(name), None)
        return env

    def run_suite(self, request: Any) -> Any:
        """Run a suite (``langadapter.runs.SuiteRunRequest``) and return
        its ``SuiteRun``: the outcome, the counts, every test's outcome by
        identifier, the wall time, the whole output, the coverage of the
        target when one is named, the identity and the side effects applied.
        An invalid request raises ``InvalidRequest``."""
        from langadapter.python._runs import run_suite
        return run_suite(self, request)

    def collect_suite(self, request: Any) -> Any:
        """Collect explicit suite files without running their test bodies
        (``CollectionRequest`` to ``CollectionRun``). A collection failure, an
        empty collection and failed execution stay distinct."""
        from langadapter.python._runs import collect_suite
        return collect_suite(self, request)

    def run_as_delivered(self, request: Any, *, sources: Any = None) -> Any:
        """Run each test file alone, without the measurement instrument, for a
        ``SuiteRunRequest`` with ``session_seed``. ``sources`` may replace a
        listed test file's text without overwriting it. The ``SuiteRun``'s test
        IDs refer to the input suite."""
        from langadapter.python._runs import run_as_delivered
        return run_as_delivered(self, request, sources=sources)

    def prepare_suite(self, suite_root: str, *, isolation_config: bool = False) -> Any:
        """Write setup for a generated suite (``SuitePreparation``): the import
        shim, and a configuration anchor only when requested. Ordinary suite
        runs never call it implicitly."""
        from langadapter.python.project import prepare_suite
        return prepare_suite(self, suite_root, isolation_config=isolation_config)

    def benchmark(self, request: Any) -> Any:
        """Time a suite (``langadapter.runs.BenchmarkRequest``) over
        repeated runs and return the measured repetitions' times
        (``Benchmark``). An invalid request raises ``InvalidRequest``."""
        from langadapter.python._runs import benchmark
        return benchmark(self, request)

    def profile_suite(self, request: Any) -> Any:
        """A suite profile (``langadapter.runs.SuiteProfileRequest``): the
        suite run once with the standard library's cProfile around each
        test's setup, call and teardown, and the time of each function it
        entered (``SuiteProfile``). Interpreter start, imports and
        collection are not profiled."""
        from langadapter.python._runs import profile_suite
        return profile_suite(self, request)

    def per_test_evidence(self, request: Any) -> Any:
        """Per-test evidence (``langadapter.runs.PerTestEvidenceRequest``) of a
        green run: each test's covered lines and arcs of the target, its time,
        and the mutants it kills when a mutation run is given
        (``PerTestEvidence``). An invalid request raises ``InvalidRequest``."""
        from langadapter.python._runs import per_test_evidence
        return per_test_evidence(self, request)

    def mutation_engines(self) -> Tuple[str, ...]:
        """The mutation engines this adapter offers for its context: Cosmic
        Ray when it is installed in the project environment."""
        return self.capabilities().mutation_engines

    def mutation_operators(self, engine: str = "cosmic_ray") -> Any:
        """The operators ``engine`` registers in the project environment (an
        ``OperatorCatalogue``), read through the project interpreter and sorted:
        the full catalogue a run measures by default. Cosmic Ray absent there is
        ``missing_setup``, an unreadable registry ``infrastructure_failure``."""
        from langadapter.mutation import OperatorCatalogue, _require_engine
        from langadapter.python._cosmic_ray import (
            _project_cosmic_ray_metadata,
        )
        _require_engine(engine, "cosmic_ray", "Python")
        version, operators, _ = _project_cosmic_ray_metadata(
            self._executor(), self.interpreter, flags=self._runtime_flags())
        known = None if version == "unknown" else version
        if operators is not None:
            return OperatorCatalogue(engine, tuple(sorted(set(operators))),
                                     tool_version=known)
        if engine not in self.mutation_engines():
            return OperatorCatalogue(
                engine, None, Outcome.MISSING_SETUP,
                f"Cosmic Ray is not installed for {self._where()}")
        return OperatorCatalogue(
            engine, None, Outcome.INFRASTRUCTURE_FAILURE,
            f"Cosmic Ray's operator registry could not be read through "
            f"{self._where()}", tool_version=known)

    def open_mutation_session(self, engine: str = "cosmic_ray",
                              **options: Any) -> Any:
        """A mutation session of ``engine`` for this adapter's project.
        ``workers`` is how many mutants are measured at once (1 by default),
        ``distributor`` ``auto`` (the default), ``local`` or ``http``.
        ``allow_fewer_workers`` opens the session with the HTTP workers that
        started when fewer than ``workers`` did. ``settings`` is a
        ``python.cosmic_ray.CosmicRaySettings``."""
        from langadapter.python._cosmic_ray import open_session
        return open_session(self, engine, **options)

    def validate_mutation_session_options(self, engine: str, **options: Any) -> None:
        """Refuse a Cosmic Ray session option it does not accept, such as an
        unknown distributor, with ``InvalidRequest``."""
        from langadapter.python.cosmic_ray import _validate_session_options
        if options.get("distributor") is None:
            options["distributor"] = "auto"
        _validate_session_options(**options)

    def _where(self) -> str:
        if isinstance(self.context.execution, ContainerExecution):
            return (f"the interpreter {self.interpreter} in the container "
                    f"{self.context.execution.container}")
        return f"the interpreter {self.interpreter}"
