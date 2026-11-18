"""The base of every language adapter.

An instance belongs to one language and one project context and shares no
mutable state with instances for other contexts. An operation the adapter
does not provide is unsupported in its capability report and returns the
``unsupported`` outcome, never a neutral default.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union
from dataclasses import dataclass, replace
import functools
import re
import threading

from langadapter import _host, capabilities as _actions
from langadapter.context import ContainerExecution, Interpreter, ProjectContext
from langadapter.capabilities import (
    OPERATION_NAMES, Availability, Capability, CapabilityReport, OperationCapability,
)
from langadapter._words import SETTING_KINDS, listed
from langadapter.errors import InvalidRequest
from langadapter.outcomes import Outcome
from langadapter.versions import UNDECLARED, LanguageSupport, VersionProfile, declare_wording


#: The kinds of run ``request_options`` serves: a run of the tests, one that
#: lists the failing tests, per-test evidence, a run as delivered, timing, a
#: coverage matrix, a collection of the tests and the project's own run, as
#: its configuration and plugins give it.
RUN_PURPOSES = ("run", "failing", "evidence", "delivered", "benchmark", "coverage_matrix",
                "collect", "project")


@dataclass(frozen=True)
class ProjectSetting:
    """A setting a project of the language takes: its name, purpose and kind
    (``path``, ``executable``, ``choice`` or ``text``), its standard
    environment variable (``alias``) if any, its default (empty: discovered),
    the values a choice allows, how a value is checked, and how a caller
    resolves a relative host path in it (``host_path``): ``path`` under the
    caller's configuration directory on this machine and as given in a
    container, ``command`` the same but only a value naming a directory (a bare
    name stays a command on PATH), ``host file`` a file of this machine
    also for a container, its links followed. Empty for none."""

    name: str
    purpose: str
    kind: str
    default: str = ""
    alias: str = ""
    choices: Tuple[str, ...] = ()
    check: str = ""
    host_path: str = ""


def _unknown_setting_kinds(settings: Any) -> Tuple[str, ...]:
    """A problem for each of the project ``settings`` whose kind is none of
    ``SETTING_KINDS``, as the check and the conformance check name it."""
    return tuple(f"project_settings: the kind {setting.kind!r} of {setting.name} is not "
                 f"{listed(SETTING_KINDS, 'or')}"
                 for setting in settings if setting.kind not in SETTING_KINDS)


#: A variable's name in a test environment setting.
_VARIABLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def environment_assignments(value: Any, field: str) -> Dict[str, str]:
    """The variables a test environment setting gives: ``NAME=value``
    words, quoted as a shell quotes them, with each backslash kept as
    written (a Windows path). A word that sets no variable raises
    InvalidRequest naming ``field``."""
    import shlex

    lexer = shlex.shlex(str(value or ""), posix=True)
    lexer.whitespace_split, lexer.escape, lexer.commenters = True, "", ""
    try:
        words = list(lexer)
    except ValueError as exc:
        raise InvalidRequest(field, f"cannot be read as NAME=value words: {exc}") from None
    found: Dict[str, str] = {}
    for word in words:
        name, equals, assigned = word.partition("=")
        if not equals or not _VARIABLE_NAME.fullmatch(name):
            raise InvalidRequest(field, f"{word!r} is no NAME=value word")
        found[name] = assigned
    return found


@dataclass(frozen=True)
class SetupQuestion:
    """One question of a language's setup (``setup_questions``): its stable
    ``key`` and the text shown, the project ``setting`` its answer is
    written to (empty: none), the default Enter takes and the reason shown
    with it, its choices (a tuple, or a function giving them when the
    question is asked) and whether another answer is allowed (``open``).
    ``check`` names why an answer is refused, or None (None: the setting's
    kind decides). ``written`` gives the value written (empty: nothing,
    None: the answer itself). ``after`` gives the note shown once the
    answer is taken."""

    key: str
    prompt: str
    setting: str = ""
    default: str = ""
    reason: str = ""
    choices: Union[Tuple[str, ...], Callable[[], Tuple[str, ...]]] = ()
    open: bool = False
    check: Optional[Callable[[str], Optional[str]]] = None
    written: Optional[Callable[[str], str]] = None
    after: Optional[Callable[[str], "SetupNote"]] = None


@dataclass(frozen=True)
class SetupNote:
    """Lines a language's setup shows, and what it names as missing for a
    run (a tool to install, a build file)."""

    lines: Tuple[str, ...] = ()
    missing: Tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeChoice:
    """What ``discover_runtime`` found: the interpreter or provisioning
    request of a project context, its interpreter flags, its test framework
    configuration, and the interpreter that can supervise a container's
    commands when none is selected."""

    interpreter: Any = None
    interpreter_flags: Tuple[str, ...] = ()
    test_framework: Any = None
    helper_python: Optional[str] = None


@dataclass(frozen=True)
class SalvageReport:
    """A syntax repair as the caller records it: the repaired source (None
    when it could not be made valid), the status (``valid``, ``repaired``,
    ``beyond_repair`` or ``validator_failed``), the number of repairs and the
    last validator message."""

    source: Optional[str]
    status: str
    repairs: int
    message: str


@dataclass(frozen=True)
class MutationAttempt:
    """One way to open a mutation session, tried in order: the keyword
    arguments of ``mutations.open_session``, the workers as the caller
    records them, whether they run in parallel, and a warning to give before
    this attempt, or ""."""

    options: Mapping[str, Any]
    workers: int
    parallel: bool
    warning: str = ""


@dataclass(frozen=True)
class ProjectResolution:
    """Whether a project resolves for its language: ``completed``, or the
    outcome and the reason it does not (a build that fails, for example)."""

    outcome: Outcome
    reason: str = ""


def _setup_entry(hook, scope):
    """``hook`` (``inspect_project`` or ``verify_project``) as callers call
    it: a request no assessment has prepared yet runs the assessment
    (``langadapter.inspect_project`` or ``verify_project`` for the adapter's
    language), which calls the hook with the prepared request."""
    @functools.wraps(hook)
    def entry(self, request):
        from langadapter.setup import ProjectSetupRequest, inspect_project, verify_project

        if isinstance(request, ProjectSetupRequest) and request._deadline is None:
            if not self.language:
                raise InvalidRequest("language", "the adapter names no registered language")
            assess = inspect_project if scope == "inspection" else verify_project
            return assess(self.language, request)
        return hook(self, request)

    entry._langadapter_setup_entry = True
    return entry


def _killing_tests(row: Mapping[str, Any]) -> List[Any]:
    """The tests a stored kill matrix's row records as its killers."""
    from langadapter.suites import TestId

    return [TestId(**test) for test in row.get("killing_tests") or ()]


def _matched(text: str, qualname: str, ids, *, by_name: bool = True) -> Optional[str]:
    """The test of ``ids`` that ``text`` names: itself, else the only one
    that is a longer or shorter dotted id of it, else with ``by_name`` the
    only one ending in its qualified name."""
    if text in ids:
        return text
    for found in ([test for test in ids if test.endswith("." + text) or text.endswith("." + test)],
                  [test for test in ids if by_name and (test == qualname
                                                        or test.endswith("." + qualname))]):
        if len(found) == 1:
            return found[0]
    return None


class LangAdapter:
    """A language adapter bound to ``context``. Subclasses set ``language``
    and ``file_extensions`` and implement the operations they support."""

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Refuse, by name, a hook whose signature callers could not call.
        from langadapter.hooks import _signature_problems
        problems = _signature_problems(cls)
        if problems:
            raise TypeError(" ".join(problems))
        # A subclass's own setup hooks are entered as the base's are.
        for name, scope in (("inspect_project", "inspection"),
                            ("verify_project", "verification")):
            hook = cls.__dict__.get(name)
            if callable(hook) and not getattr(hook, "_langadapter_setup_entry", False):
                setattr(cls, name, _setup_entry(hook, scope))
        # The declared wording names the runtime's versions in report records.
        support = cls.__dict__.get("support")
        if isinstance(support, LanguageSupport):
            for implementation, wording in support.wording.items():
                declare_wording(implementation, wording)

    #: The language this adapter handles, as it is registered.
    language: str = ""
    #: What the language supports (``langadapter.versions``): its runtime's
    #: tested and expected versions and their wording. Without a
    #: declaration every version is untested.
    support: LanguageSupport = UNDECLARED
    #: The file extensions of the language's sources, with the leading dot.
    file_extensions: Tuple[str, ...] = ()
    #: The execution kinds the execution-backed source operations support.
    source_execution_kinds: Tuple[str, ...] = ("local",)
    test_execution_kinds: Tuple[str, ...] = ("local",)
    #: The granularity of the evidence each test operation reports, as pairs of
    #: the operation's name and its granularities: ``aggregate`` for the suite
    #: as a whole, ``per_test`` for each test, such as
    #: ``(("run_suite", ("aggregate", "per_test")), ("per_test_evidence",
    #: ("per_test",)))``. The operation report gives each test operation's
    #: granularity (``OperationCapability.evidence_granularity``), empty for an
    #: operation the pairs leave out, beside ``mutation_evidence_granularity``
    #: for the mutation operations.
    test_evidence_granularity: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    native_mutation_engines: Tuple[str, ...] = ()
    mutation_execution_kinds: Tuple[str, ...] = ("local",)
    mutation_evidence_granularity: Tuple[str, ...] = ()

    #: The stem of a test file written for no particular target, as the
    #: ``test_file_name`` template names it.
    scratch_stem: str = "candidate"
    #: The unit ``prune_collection_breakers`` removes: the smallest part of a
    #: test source that the language's loading of tests (collection,
    #: compilation) names as the cause of its failure.
    collection_unit: str = ""
    #: The directories a search for the project's source files skips: the usual
    #: version control, environment, build, cache and test directories.
    ignored_dirs: Tuple[str, ...] = (
        ".git", ".venv", "venv", "env", "node_modules", "build", "target", "dist",
        "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", "tests", "test")
    #: File names a search for source files leaves out although they carry
    #: the language's file extension: build scripts, not sources to test.
    non_source_files: Tuple[str, ...] = ()
    #: Whether a prepared copy may reuse the modules compiled by runs in
    #: earlier copies.
    reuses_bytecode: bool = False
    #: Whether a prepared copy takes a suite directory outside the project along,
    #: beside it, so a run prints the paths a run in place prints.
    copies_external_suite: bool = False
    #: Whether ``run_suite`` takes ``SuiteRunRequest.timeout_journal``, the
    #: per-test journal that names the test running when a timeout or crash
    #: ended the run. False tells callers not to ask for it. An adapter whose
    #: answer depends on the project sets it on the instance, so a consumer
    #: reads it on the adapter, not on its class.
    provides_timeout_journal: bool = True
    #: Whether ``run_as_delivered`` takes ``SuiteRunRequest.session_seed``,
    #: so a consumer may run a suite as delivered with a fixed seed as its
    #: normal run. False for one that refuses the seed or states nothing. An
    #: adapter whose answer depends on the project sets it on the instance,
    #: so a consumer reads it on the adapter, not on its class.
    seeds_delivered_runs: bool = False
    #: The comment prefix of the separator line ``merge_sources`` puts
    #: between two folded test files (``#``, ``//``), None for no line.
    source_merge_separator: Optional[str] = None
    #: The version of the generated-test validity policy whose findings
    #: ``analyze_validity`` reports, which callers record with a run, or
    #: None without a policy.
    validity_policy_version: Optional[str] = None

    def test_vocabulary(self) -> Mapping[str, str]:
        """The language's words for its tests (``langadapter.vocabulary``) in
        this project. Empty by default: callers then build plain sentences."""
        return {}

    def file_rule(self, source: str) -> Optional[str]:
        """The stem a file holding ``source`` must have, or None when the
        language puts no rule on file names (the default)."""
        return None

    def rename_file_unit(self, source: str, new_name: str) -> Any:
        """``source`` with what ``file_rule`` names in it renamed to
        ``new_name`` to fit a file of that stem, as a ``SourceObservation``:
        completed with the new source, or with the reason it cannot be
        renamed safely. Completed and unchanged by default."""
        from langadapter.sources import SourceObservation, SourceStatus

        return SourceObservation(SourceStatus.COMPLETED, value=source)

    def test_id_prefix(self, source: str, stem: str) -> str:
        """The prefix of the ids of the tests that ``source``, a file named
        ``stem``, holds: the stem by default."""
        return stem

    def project(self) -> ProjectResolution:
        """Whether this adapter's project resolves (by default it does)."""
        return ProjectResolution(Outcome.COMPLETED)

    def test_id_text(self, test) -> str:
        """A test's id as the language writes it in kill matrices and
        records, such as ``demo.CalcTest.adds``: the ``TestId``'s own text
        by default."""
        return str(test)

    def kill_tokens(self, kill_matrix, test_ids):
        """Each test's kill tokens (``K:<mutant id>``) from the tests a
        stored kill matrix records as each kill's killers (``killing_tests``,
        ``Mutant.killing_tests``), for the tests ``test_ids`` as
        ``test_id_text`` writes them, and the killers that match none: a
        killing test none of them is, a killer its run placed in no test,
        and the killers of a row stored without killing tests."""
        found: Dict[str, set] = {}
        unmatched: List[str] = []
        for row in kill_matrix.get("mutants") or ():
            if row.get("id") is None:
                continue
            if "killing_tests" not in row:
                unmatched.extend(str(k) for k in row.get("killers") or (
                    [row["killer"]] if row.get("killer") else ()))
                continue
            for test in _killing_tests(row):
                text = _matched(self.test_id_text(test), test.qualname, test_ids, by_name=False)
                if text is None:
                    unmatched.append(str(test))
                else:
                    found.setdefault(text, set()).add(f"K:{row['id']}")
            unmatched.extend(row.get("unmatched_killers") or ())
        return ({test: frozenset(items) for test, items in found.items()},
                tuple(dict.fromkeys(unmatched)))

    def pool_kills(self, kill_matrix, file_to_index, test_ids_by_index):
        """Kill tokens by suite and test from a stored kill matrix's killing
        tests: each goes to the suite ``file_to_index`` names for its file's
        name, matched there among ``test_ids_by_index`` as ``kill_tokens``
        matches, or by its qualified name when the pooled file has another
        name."""
        from pathlib import PurePosixPath

        attributed: Dict[int, Dict[str, set]] = {}
        for row in kill_matrix.get("mutants") or ():
            for test in _killing_tests(row) if row.get("id") is not None else ():
                index = file_to_index.get(PurePosixPath(test.file).name)
                text = None if index is None else _matched(
                    self.test_id_text(test), test.qualname, test_ids_by_index.get(index, set()))
                if text is not None:
                    attributed.setdefault(index, {}).setdefault(text, set()).add(f"K:{row['id']}")
        return attributed

    def request_options(self, purpose: str, *, target: Optional[str] = None,
                        traceback: str = "short", stop_at_first_failure: bool = False,
                        record_leaks: bool = False, session_seed: Optional[int] = None,
                        reverse_order: bool = False,
                        leave_out_timing_plugins: bool = False) -> Dict[str, Any]:
        """The fields this language adds to a request for one kind of run
        (``RUN_PURPOSES``, else ``InvalidRequest``): ``target`` is the module
        under test. ``traceback`` (None for the purpose's own),
        ``stop_at_first_failure`` and ``record_leaks`` apply to a run of the
        tests and to a run as delivered, ``session_seed`` and ``reverse_order``
        to a run as delivered. ``leave_out_timing_plugins`` asks any run to
        leave out the plugins that change its time, where the language has
        such plugins. Empty by default."""
        if purpose not in RUN_PURPOSES:
            raise InvalidRequest("purpose", f"must be one of {', '.join(RUN_PURPOSES)}")
        return {}

    def delivered_order_refusal(self, reverse: bool) -> Optional[str]:
        """Why a run as delivered in the asked order is not offered, or None."""
        return None

    def collection_text(self, run) -> str:
        """What a failed collection says, for the caller's record: the run's
        diagnostic, else its reason."""
        return run.diagnostic or run.reason

    def salvage_report(self, source: str) -> SalvageReport:
        """The language's syntax repair of ``source``
        (``sources.salvage_syntax``) as a ``SalvageReport``. Without one the
        source is judged as it stands: ``valid`` when the language's
        validator, else its structure check, accepts it or it has neither,
        ``validator_failed`` with its reason when that check could not run,
        else ``beyond_repair``. A repair that could not run, for want of
        time, setup or an allowed runtime, is ``validator_failed`` too."""
        if not self.sources.supports("salvage_syntax"):
            valid, checked = True, None
            if self.sources.supports("validate_source"):
                checked = self.sources.validate_source(source)
                valid = checked.completed and checked.value.valid
            elif self.sources.supports("check_structure"):
                checked = self.sources.check_structure(source)
                valid = checked.completed and checked.value.ok
            if checked is not None and not checked.completed:
                return SalvageReport(None, "validator_failed", 0, checked.reason)
            return SalvageReport(source if valid else None, "valid" if valid else "beyond_repair",
                                 0, "")
        result = self.sources.salvage_syntax(source)
        status = {"unchanged": "valid", "applied": "repaired", "not_repaired": "beyond_repair",
                  "parse_error": "beyond_repair", "refused": "beyond_repair"}.get(
                      result.status, "validator_failed")
        repairs = result.repairs if result.status == "applied" else 0
        return SalvageReport(result.source if status in ("valid", "repaired") else None,
                             status, repairs, result.detail)

    def skip_removal(self, source: str) -> Tuple[str, int]:
        """``source`` without its whole-file skips and how many there were
        (``sources.strip_module_level_skips``)."""
        result = self.sources.strip_module_level_skips(source)
        return result.source, len(result.removed)

    def collision_dependents(self, source: str, name: str, measured: Any) -> Optional[set]:
        """The ``measured`` tests of ``source`` that depend on its binding
        ``name``, or None when every test counts (the default)."""
        return None

    def collision_resolution(self, sources: Sequence[str], *, localize_autouse: bool = False,
                             preference: Any = None) -> Any:
        """The test sources' name collisions resolved before they are merged
        into one, as a ``SourceObservation`` of a ``CollisionResolution``. By
        default ``sources.resolve_collisions``' observation."""
        return self.sources.resolve_collisions(
            list(sources), preference=preference, localize_autouse=localize_autouse)

    #: Whether the mutation tool sets each mutant's timeout itself, so that a
    #: request leaves ``per_mutant_timeout`` to it unless one is fixed.
    own_mutant_timeout_rule: bool = False
    #: Whether a coverage matrix is measured in the project itself rather
    #: than in a prepared copy.
    coverage_matrix_in_place: bool = False

    def mutation_catalogue(self, config: Mapping[str, Any]) -> Any:
        """The mutation operators the caller's ``config`` selects, read by the
        language's own keys: the full catalogue by default."""
        from langadapter.mutation import Catalogue
        return Catalogue()

    def named_operators(self, name: str) -> List[str]:
        """The operators of the named operator set ``name``: the name itself
        by default, which the tool expands."""
        return [name]

    def mutation_session_attempts(self, config: Mapping[str, Any], *, configured_workers: int,
                                  effective_workers: int, cache_base: str,
                                  log: Any = None) -> Tuple[MutationAttempt, ...]:
        """The ways to open a mutation session for ``config``, in order: the
        caller tries the next when one does not open. ``cache_base`` is the
        directory for the tool's caches. By default one session with the
        configured workers. ``ValueError`` says why ``config`` cannot run."""
        workers = max(1, int(configured_workers or 1))
        return (MutationAttempt({"workers": workers}, workers, False),)

    def mutation_dir_refusal(self, config: Mapping[str, Any], *, create: bool) -> Optional[str]:
        """Why the mutation tool's configured session directory cannot hold a
        session, or None (the default)."""
        return None

    def mutation_workers_refusal(self, config: Mapping[str, Any]) -> Optional[str]:
        """Why the configured workers cannot run, or None (the default)."""
        return None

    def setup_files(self, root: str, test_paths: Sequence[str]) -> Tuple[str, ...]:
        """The files the language's runner loads beside ``test_paths`` of the
        suite at ``root``, such as pytest's conftest.py. Empty by default."""
        return ()

    def shared_setup_files(self, directory: str) -> Tuple[str, ...]:
        """The setup files a prepared suite in ``directory`` holds. Empty by
        default."""
        return ()

    def test_files(self, paths: Sequence[str]) -> List[str]:
        """Each path, with a directory replaced by the test files the
        language's runner collects in it, at any depth: by default the files
        the ``test_file_name`` template names."""
        import fnmatch

        template = getattr(self, "test_file_name", "") or "test_{stem}{ext}"
        patterns = {template.format(stem="*", Stem="*", ext=file_extension)
                    for file_extension in self.file_extensions}
        found: List[str] = []
        for path in paths:
            test_dir = Path(path)
            if not test_dir.is_dir():
                found.append(str(path))
                continue
            found.extend(str(item) for item in sorted(test_dir.rglob("*"))
                         if item.is_file() and any(fnmatch.fnmatchcase(item.name, pattern)
                                                   for pattern in patterns))
        return found

    def project_test_files(self) -> List[str]:
        """The test files the language's runner collects in the project as
        the project configures them, in ``test_files``' order: by default
        ``test_files`` of the context's root."""
        return self.test_files([self.context.root])

    def coverage_texts(self, coverage) -> Dict[str, Tuple[Any, ...]]:
        """A run's coverage as text for the caller's records: the covered
        lines and each missed branch as ``"source->destination"``, both empty
        without coverage."""
        if coverage is None:
            return {"covered_lines": (), "missing_branches": ()}
        return {"covered_lines": tuple(coverage.covered),
                "missing_branches": tuple(f"{branch.source}->{branch.destination}"
                                          for branch in coverage.missing_branches())}

    def delivered_case(self, test, outcome: str) -> Tuple[str, str, str]:
        """A test of a run as delivered, as text for the caller's records: its
        id (``test_id_text``), its name with its parameters, and its outcome."""
        return (self.test_id_text(test),
                test.qualname.split(".")[-1] + (test.params or ""), outcome)

    @classmethod
    def import_statement(cls, target: str, root: str) -> Optional[str]:
        """The line a first test of the source file ``target`` in the project
        at ``root`` uses to import it, or, where the language's tests need
        none, a sentence that says so (``none; ...``, as Java's and the
        tutorial Toy's), which a consumer shows in place of its own import
        advice. It is "" when it cannot tell, or None when the adapter offers none
        (the default, and then ``sources.import_statement`` is unsupported).
        A class method, asked before a project context exists. It probes
        nothing of the project."""
        return None

    def code_smells(self, paths: Sequence[str]) -> Optional[int]:
        """The count of the language's linter findings in ``paths``, or None
        when the linter could not run or the adapter offers none (the
        default, and then ``sources.code_smells`` is unsupported)."""
        return None

    def metric_providers(self) -> Sequence[Any]:
        """The language's own metric providers (``langadapter.MetricProvider``),
        which every adapter object registers when its source operations are
        created, after the structural provider. No providers by default."""
        return ()

    #: The settings a project of the language takes (``ProjectSetting``).
    project_settings: Tuple[ProjectSetting, ...] = ()

    @classmethod
    def discover_runtime(cls, root: str, values: Mapping[str, Any], *, container: bool = False,
                         environment: Optional[Mapping[str, str]] = None,
                         resolve_path: Any = None) -> RuntimeChoice:
        """The runtime of a project at ``root`` from its setting ``values``
        (``project_settings``), for a container (``container``) or this machine,
        with the caller's captured ``environment`` and its ``resolve_path``
        for a host path. A class method, asked before a project context and
        an adapter exist. By default the ``runtime`` fact found on PATH."""
        import shutil

        runtime = getattr(cls, "runtime", None)
        if not runtime:
            return RuntimeChoice()
        if Path(str(runtime)).is_absolute():
            found = str(runtime) if _host.started_file(str(runtime)) else None
        else:
            found = shutil.which(str(runtime))
        return RuntimeChoice(Interpreter(path=found) if found else None)

    @classmethod
    def describe_runtime(cls, root: str, values: Mapping[str, Any], *, container: bool = False,
                         environment: Optional[Mapping[str, str]] = None,
                         resolve_path: Any = None) -> RuntimeChoice:
        """The runtime the setting ``values`` select, described without
        checking or discovering it: for a setup that states the selection
        before anything runs. A language that has nothing to check
        describes what ``discover_runtime`` finds (the default)."""
        return cls.discover_runtime(root, values, container=container, environment=environment,
                                    resolve_path=resolve_path)

    @classmethod
    def context_environment(cls, values: Mapping[str, Any], *, container: bool = False
                            ) -> Dict[str, str]:
        """The variables the language's tools read, from the setting
        ``values``, for a project context's environment. A class method like
        ``discover_runtime``. Empty by default."""
        return {}

    @classmethod
    def provision_runtime(cls, request: Any, *, store: Path, name: str,
                          environment: Optional[Mapping[str, str]] = None,
                          log: Any = None) -> str:
        """The runtime of an environment made for ``request`` (a
        ``ProvisioningRequest``: a version and a requirements file), kept as
        ``name`` under the caller's ``store`` and reused while its inputs
        stay the same, made with the caller's ``environment``. ``log``
        receives progress lines. A class method like ``discover_runtime``.
        By default the language provisions nothing: InvalidRequest."""
        raise InvalidRequest("runtime", f"the {cls.language} adapter provisions no runtime: "
                             "select one")

    @classmethod
    def setup_questions(cls, project_dir: Path, probe: Any, *, consumer: str = "the caller"
                        ) -> Iterator[Union[SetupQuestion, SetupNote]]:
        """The questions ``consumer``'s setup asks for a project of the
        language in ``project_dir``, in order, and the notes between them, the
        texts naming ``consumer`` where they name the program. ``probe``
        looks at the machine the caller runs on, whose platform decides the
        file names a language looks for, such as a build wrapper's or a
        virtual environment's interpreter: ``is_dir(path)``,
        ``is_file(path)``, ``which(name)``, ``environment(name)``,
        ``read_text(path)`` (None when unreadable) and
        ``run(argv, timeout)`` (its exit code and output, or None when it
        does not start). By default each declared setting
        (``project_settings``) is asked with its purpose, its default and
        its choices, and checked by its kind."""
        for setting in cls.project_settings:
            yield SetupQuestion(f"{cls.language}.{setting.name}", setting.purpose,
                                setting=setting.name, default=setting.default,
                                choices=tuple(setting.choices))

    @classmethod
    def setup_problem(cls, setting: str, value: str, project_dir: Path, probe: Any
                      ) -> Optional[str]:
        """Why a setup refuses ``value`` for the declared ``setting`` in the
        project ``project_dir``, as a sentence, or None. A language that provides
        it checks its settings itself. By default the setting's kind decides:
        a choice must be one of its values, a path an existing file or directory,
        an executable a file or a program ``probe.which`` finds, a relative
        path read from ``project_dir``. An empty value leaves the setting to the
        language's discovery."""
        declared = next((s for s in cls.project_settings if s.name == setting), None)
        if declared is None or not value:
            return None
        if declared.kind == "choice":
            return (None if value in declared.choices
                    else f"The value must be one of {', '.join(declared.choices)}.")
        path = Path(value) if Path(value).is_absolute() else Path(project_dir) / value
        if declared.kind == "path":
            return None if probe.is_file(path) or probe.is_dir(path) else "No such file or directory."
        if declared.kind == "executable":
            return None if probe.is_file(path) or probe.which(value) else "No such program."
        return None

    def run_counts(self, run) -> Dict[str, Optional[int]]:
        """A run's collected, passed, failed and errored tests as the
        language's runner counts them: the run's counts by default, with
        collected the passed, failed, errored and skipped tests together. A
        count the run did not observe is None, never 0."""
        counts = run.counts
        parts = (counts.passed, counts.failed, counts.errors, counts.skipped)
        return {"collected": None if None in parts else sum(parts), "passed": counts.passed,
                "failed": counts.failed, "errors": counts.errors}

    def listed_failures(self, run) -> Optional[Tuple[Any, ...]]:
        """The failed and errored tests (``TestId``) a suite run's report
        lists, possibly none, or None when the report cannot be trusted, by
        default when the run neither completed nor had test failures."""
        if run.outcome not in (Outcome.COMPLETED, Outcome.TEST_FAILURES):
            return None
        return tuple(case.test for case in run.tests if case.outcome in ("failed", "error"))

    def __init__(self, context: ProjectContext) -> None:
        self.context = context
        self._source_operations = None
        self._test_operations = None
        self._mutation_operations = None
        self._operations_lock = threading.RLock()
        self._version_profile = None

    def profile(self) -> VersionProfile:
        """This instance's version profile (``langadapter.versions``), chosen
        once by ``choose_profile()`` from the versions it found, which every
        version-dependent question asks."""
        with self._operations_lock:
            if getattr(self, "_version_profile", None) is None:
                self._version_profile = self.choose_profile()
            return self._version_profile

    def choose_profile(self) -> VersionProfile:
        """The ``VersionProfile`` of the versions this adapter found, which
        ``profile()`` asks for once. An adapter that refuses an older runtime
        returns a subclass whose ``refusal()`` gives the reason, and every
        execution-backed operation then answers unsupported with it. The
        base profile reads no runtime and refuses no version."""
        return VersionProfile(None, self.support)

    def _runtime_refusal(self) -> str:
        """Why no execution-backed operation runs on the context's runtime,
        one its language's declaration does not support, or ""."""
        return self.profile().refusal()

    @property
    def sources(self):
        """Stable typed source operations, created without probing tools."""
        from langadapter.operations import SourceOperations
        with self._operations_lock:
            if self._source_operations is None:
                self._source_operations = SourceOperations(self)
            return self._source_operations

    @property
    def tests(self):
        """Stable native test operations, created without probing tools."""
        from langadapter.operations import TestOperations
        with self._operations_lock:
            if self._test_operations is None:
                self._test_operations = TestOperations(self)
            return self._test_operations

    def operation_capabilities(self) -> Tuple[OperationCapability, ...]:
        """Every operation as the adapter implements it, found without
        probing tools. An unimplemented one is unsupported, with its reason
        and no next step. ``probe_capabilities()`` adds probed readiness."""
        result = []
        for name in OPERATION_NAMES:
            group, method = name.split(".")
            operations = getattr(self, group, None)
            supported = operations is not None and operations.supports(method)
            backed = operations is not None and operations.execution_backed(method)
            setup_issue = self.execution_setup_issue() if backed else ""
            missing_runtime = backed and self._runtime_missing()
            reason = ("no project runtime is available" if missing_runtime else setup_issue) if supported else (
                self.operation_context_issue(name) or "operation is not implemented")
            result.append(OperationCapability(
                name, supported, reason,
                Availability.MISSING_SETUP if supported and (missing_runtime or setup_issue) else None,
                execution_backed=backed,
                execution_kinds=operations.execution_kinds(method) if operations is not None else (),
                evidence_granularity=(operations.evidence_granularity(method)
                                      if group in ("tests", "mutations") and supported else ()),
                resolution_methods=(operations.resolution_methods(method)
                                    if group == "mutations" and supported else ()),
                # A setup issue names its own remedy in the reason, and an
                # operation the adapter does not implement has no next step.
                action="select the project's runtime" if supported and missing_runtime else None))
        return tuple(result)

    @property
    def mutations(self):
        """Stable mutation operations, created without importing an engine."""
        from langadapter.operations import MutationOperations
        with self._operations_lock:
            if self._mutation_operations is None:
                self._mutation_operations = MutationOperations(self)
            return self._mutation_operations

    def inspect_project(self, request):
        """What the project's setup has and lacks for the operations of
        ``request`` (a ``ProjectSetupRequest`` naming the project), as
        ``langadapter.inspect_project(language, request)`` reports it,
        provisioning and building nothing. An override receives the request
        the assessment prepared. The default reports no setup check
        (``Outcome.UNSUPPORTED``)."""
        return self._unsupported_setup(request, "inspection")

    def verify_project(self, request):
        """The bounded native verification of the request's selected inputs,
        as ``langadapter.verify_project(language, request)`` reports it. An
        adapter overrides it as it does ``inspect_project``."""
        return self._unsupported_setup(request, "verification")

    inspect_project = _setup_entry(inspect_project, "inspection")
    verify_project = _setup_entry(verify_project, "verification")

    def _unsupported_setup(self, request, scope):
        from langadapter.setup import ProjectSetupRequest, ProjectSetupItem, ProjectSetupReport

        if not isinstance(request, ProjectSetupRequest):
            raise InvalidRequest("request", "must be a ProjectSetupRequest")
        operations = tuple(replace(item, readiness=None if item.supported else Availability.UNSUPPORTED,
                                   reason=("not checked: this language's adapter has no setup check"
                                           if item.supported else item.reason),
                                   action=_actions.RUN if item.supported else item.action,
                                   tried_by="run" if item.supported else None)
                           for item in self.operation_capabilities())
        return ProjectSetupReport(scope, Outcome.UNSUPPORTED, "this language's adapter has no setup check",
                                  None, request.inputs,
                                  (ProjectSetupItem("provider", bool(request.required_operations),
                                                    Availability.UNSUPPORTED, Outcome.UNSUPPORTED,
                                                    reason="this language's adapter has no setup check",
                                                    action=f"none for now: the {self.language or 'language'} "
                                                           "adapter has no setup check"),), operations)

    def probe_capabilities(self) -> CapabilityReport:
        """The capability report with each supported operation's probed
        readiness added to ``operation_capabilities()``. A context without a
        runtime, or a container context whose runner is not on PATH, reports
        missing setup without starting a process, an adapter without a
        ``capabilities`` probe reports unsupported, and the readiness of a pure
        read stays unknown until it is tried."""
        operations = self.operation_capabilities()
        interpreter = self.context.interpreter
        if interpreter is None or interpreter.path is None:
            return CapabilityReport(self.language, "", Outcome.MISSING_SETUP,
                                    "no project runtime is available", self.context.runtime_version,
                                    (), (), "none", (), operations=operations)
        runner_issue = self._container_runner_issue()
        if runner_issue:
            return CapabilityReport(self.language, interpreter.path, Outcome.MISSING_SETUP,
                                    runner_issue, self.context.runtime_version,
                                    (), (), "none", (), operations=operations)
        probe = getattr(self, "capabilities", None)
        if not callable(probe):
            return CapabilityReport(self.language, interpreter.path, Outcome.UNSUPPORTED,
                                    "readiness probing is not implemented", self.context.runtime_version,
                                    (), (), "none", (), operations=operations)
        report = probe()
        if not isinstance(report, CapabilityReport):
            raise TypeError("capabilities must return a CapabilityReport")
        resolved = []
        for operation in operations:
            readiness = self.operation_readiness(operation.name, report) if operation.supported else None
            if readiness is not None:
                if not isinstance(readiness, Capability) or readiness.name != operation.name:
                    raise InvalidRequest("readiness", "must identify the requested operation")
                operation = replace(operation, readiness=readiness.availability, reason=readiness.reason)
            resolved.append(operation)
        return replace(report, operations=tuple(resolved))

    def operation_context_issue(self, name: str) -> str:
        """Why the operation ``name``, such as ``sources.stage_pool``, cannot
        run in this context, found without starting a tool, or "". With a
        reason, the operation is unsupported, and the operation report and
        the operation's own answer give that reason in place of "operation
        is not implemented"."""
        return ""

    def execution_setup_issue(self) -> str:
        """A prerequisite of running the project's tools that is known to be
        missing without starting a tool, such as a setting a container run
        needs, or "". With a reason, every execution-backed operation reports
        missing setup with it."""
        return ""

    def _runtime_missing(self) -> bool:
        """Whether the context names no project runtime."""
        runtime = self.context.interpreter
        return runtime is None or runtime.path is None

    def _container_runner_issue(self) -> str:
        """Why a container context's runner cannot start, found without
        starting it: it is on no PATH the executor looks on (the control
        environment's when the context names one, else this process's). ""
        for a runner that is there and for a local context."""
        execution = self.context.execution
        if not isinstance(execution, ContainerExecution):
            return ""
        import shutil

        from langadapter._environment import selected_executable
        found = (shutil.which(execution.runner) if execution.control_environment is None
                 else selected_executable(execution.runner, execution.control_environment))
        return "" if found else f"the container runner {execution.runner} is not on PATH"

    def _execution_gate(self) -> Optional[Tuple[Outcome, str]]:
        """Why an operation that runs the project's tools cannot run here, as
        outcome and reason, or None: no runtime, a container runner that is
        not on PATH or a setup issue is ``missing_setup``, a runtime the
        language does not support is ``unsupported``."""
        if self._runtime_missing():
            return Outcome.MISSING_SETUP, "no project runtime is available"
        runner_issue = self._container_runner_issue()
        if runner_issue:
            return Outcome.MISSING_SETUP, runner_issue
        setup_issue = self.execution_setup_issue()
        if setup_issue:
            return Outcome.MISSING_SETUP, setup_issue
        refusal = self._runtime_refusal()
        if refusal:
            return Outcome.UNSUPPORTED, refusal
        return None

    def operation_readiness(self, name: str, report: CapabilityReport):
        """The readiness of the supported operation ``name`` from the probe's
        ``report``, as a ``Capability`` named like the operation, or None when
        the probe did not check its native prerequisites.
        ``probe_capabilities()`` reports it and refuses a ``Capability`` named
        otherwise."""
        return None

    @staticmethod
    def _capability_readiness(name: str, report: CapabilityReport, *required: str):
        checks = [next((item for item in report.capabilities if item.name == requirement), None)
                  for requirement in required]
        for status in (Availability.UNSUPPORTED, Availability.MISSING_SETUP):
            failures = [item for item in checks if item is not None and item.availability is status]
            if failures:
                return Capability(name, status, "; ".join(item.reason or item.name for item in failures))
        if checks and all(item is not None and item.available for item in checks):
            return Capability(name, Availability.AVAILABLE)
        return None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(language={self.language!r}, " \
               f"root={self.context.root!r})"
