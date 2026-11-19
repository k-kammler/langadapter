"""An adapter's operation groups: typed test, source and mutation
observations over its language mechanisms, independent of the caller's
policy. Each group checks a request, answers what the adapter or context
cannot run as ``unsupported`` or ``missing_setup``, and passes the rest to
the adapter's own methods."""
from __future__ import annotations

import contextlib
import copy
import os
from dataclasses import replace
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Optional, Callable, Iterable, Sequence, Tuple
from langadapter.errors import InvalidRequest, SourceReadError
from langadapter.execution import execution_identity
from langadapter.outcomes import ObservationIdentity, ToolchainIdentity, Outcome
from langadapter.versions import described, unknown
from langadapter.runs import (
    Benchmark,
    BenchmarkRequest,
    CollectionRequest,
    CollectionRun,
    KILLS_NOT_REQUESTED,
    KILLS_UNUSABLE,
    PerTestEvidence,
    PerTestEvidenceRequest,
    RunCounts,
    SuiteProfile,
    SuiteProfileRequest,
    SuiteRun,
    SuiteRunRequest,
    WALL_TIME_SCOPE,
    WarmedBenchmarkIdentity,
    WarmedSuiteBenchmark,
    WarmedSuiteBenchmarkRequest,
    _duration,
)
from langadapter._execution_scope import reraise_unresolved
import subprocess
import functools
from langadapter.context import ContainerExecution
from langadapter.capabilities import MUTATION_OPERATIONS, SOURCE_EDITS, SOURCE_READS, TEST_OPERATIONS
from langadapter.sources import (
    MetricDefinition, MetricProvider, MetricRegistry, MetricScope, MetricValue, SourceMetrics,
    STANDARD_METRICS, _StructuralProvider, PoolMember, StagedPool,
    APPLIED, PARSE_ERROR, UNCHANGED, CollisionResolution, SourceObservation, SourceResult,
    SourceStatus,
    SourceValidation, StructureCheck, SOURCE_STATUSES, SuiteNormalization, TargetIdentity,
    TestMetrics,
)
from langadapter.suites import TestId
from langadapter.mutation import OperatorCatalogue, MutationCleanupError, MutationSession


#: The source operations that run the project's tools (``target_identity``
#: too, in a container context).
_SOURCE_RUNS = frozenset({"validate_source", "resolve_collisions", "merge_sources", "normalize_shadowed",
                          "salvage_syntax", "prune_unresolvable_imports", "prune_collection_breakers",
                          "strip_module_level_skips"})


def _scope(adapter, backed: bool):
    """The execution scope of an operation that runs the project's tools,
    else none."""
    if not backed:
        return contextlib.nullcontext()
    from langadapter._execution_scope import operation
    return operation(adapter.context)


def _failure(exc: Exception) -> Tuple[str, str]:
    """The status and detail of an error a hook raised: a timeout, a
    missing import or another failure. One the execution scope must
    resolve is raised again."""
    reraise_unresolved(exc)
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return "timeout", str(exc)
    if isinstance(exc, ImportError):
        return "missing_setup", str(exc)
    return "infrastructure_failure", f"{type(exc).__name__}: {exc}"


class TestOperations:
    """The test operations of an adapter, ``adapter.tests``, each passing its
    request unchanged to the adapter's own method. Making the group and
    asking what it supports run no tools. An unsupported operation or a
    missing setup gives unknown counts, never an empty success. Setup files
    and instruments come from the adapter's own methods."""

    __test__ = False

    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def _method(self, name):
        method = getattr(self.adapter, name, None)
        return method if callable(method) else None

    def supports(self, name: str) -> bool:
        """Whether the operation ``name`` is implemented for this adapter and
        its context's kind of execution. Nothing runs to find out."""
        if name not in TEST_OPERATIONS:
            return False
        return (self._method(name) is not None
                and not self.adapter.operation_context_issue(f"tests.{name}")
                and self.adapter.context.execution.kind in self.execution_kinds(name))

    def execution_backed(self, name: str) -> bool:
        """Whether ``name`` is a test operation, which runs project tools."""
        return name in TEST_OPERATIONS

    def execution_kinds(self, name: str) -> tuple[str, ...]:
        """The kinds of execution (``local``, ``container``) the operation
        ``name`` runs in for this adapter."""
        return tuple(self.adapter.test_execution_kinds) if name in TEST_OPERATIONS else ()

    def evidence_granularity(self, name: str) -> tuple[str, ...]:
        """The granularities of the evidence the operation ``name`` reports
        for this adapter, empty when it reports none."""
        return tuple(dict(self.adapter.test_evidence_granularity).get(name, ()))

    def _identity(self, request) -> ObservationIdentity:
        context = self.adapter.context
        return ObservationIdentity(
            target=None, suite=request.suite.identity(),
            toolchain=ToolchainIdentity.of(described(
                self.adapter.language or "unknown", context.runtime_version, "context")),
            instrument=None, execution=execution_identity(context))

    def _call(self, name, request, request_type, result_type, failed, **kwargs):
        with _scope(self.adapter, True):
            return self._call_owned(name, request, request_type, result_type, failed, **kwargs)

    def _call_owned(self, name, request, request_type, result_type, failed, **kwargs):
        if not isinstance(request, request_type):
            raise InvalidRequest("request", f"must be a {request_type.__name__}")
        request.validate()
        self.adapter.context._require_execution()
        method = self._method(name)
        if method is None or not self.supports(name):
            return failed(Outcome.UNSUPPORTED, self.adapter.operation_context_issue(f"tests.{name}")
                          or f"{name} is not implemented for {self.adapter.context.execution.kind}")
        refused = self.adapter._execution_gate()
        if refused:
            return failed(*refused)
        result = method(request, **kwargs)
        if not isinstance(result, result_type):
            raise TypeError(f"{name} returned {type(result).__name__}, expected {result_type.__name__}")
        return copy.deepcopy(result)

    def _suite_failure(self, request, outcome, reason):
        return SuiteRun(outcome, reason, RunCounts(), (), (), None, "", None, self._identity(request))

    def run_suite(self, request: SuiteRunRequest) -> SuiteRun:
        """Run the request's suite once and report every test, the counts
        and, with a ``coverage_target``, that file's coverage. An operation
        the adapter does not offer is ``unsupported`` and a missing runtime
        or tool is ``missing_setup``, both with unknown counts."""
        return self._call("run_suite", request, SuiteRunRequest, SuiteRun,
                          lambda outcome, reason: self._suite_failure(request, outcome, reason))

    def collect_suite(self, request: CollectionRequest) -> CollectionRun:
        """Collect the request's suite without running its tests. Collection
        still runs imports, setup files and collection hooks."""
        return self._call("collect_suite", request, CollectionRequest, CollectionRun,
                          lambda outcome, reason: CollectionRun(outcome, reason, self._identity(request), ""))

    def run_as_delivered(self, request: SuiteRunRequest, *, sources: Optional[Mapping[str, str]] = None) -> SuiteRun:
        """Run each test file of the suite on its own, as delivered: with its
        import setup and without the adapter's measurement instrument.
        ``sources`` maps a declared test file's relative POSIX path to the
        text to run in its place, without changing the file.
        ``request.timeout_s`` bounds the whole run: each file runs in what is
        left of it, and a run whose time is spent ends timed out before the
        next file."""
        if not isinstance(request, SuiteRunRequest):
            raise InvalidRequest("request", "must be a SuiteRunRequest")
        request.validate()
        if sources is not None:
            if not isinstance(sources, Mapping):
                raise InvalidRequest("sources", "must map declared relative test paths to source text")
            declared = {item.path for item in request.suite.identity()[:len(request.suite.test_files)]}
            replacements = {}
            for path, source in sources.items():
                if (not isinstance(path, str) or "\\" in path or
                        PurePosixPath(path).is_absolute() or path not in declared):
                    raise InvalidRequest("sources", "keys must be declared relative POSIX test paths")
                if not isinstance(source, str):
                    raise InvalidRequest("sources", "replacement source must be text")
                replacements[path] = source
            sources = replacements
        return self._call("run_as_delivered", request, SuiteRunRequest, SuiteRun,
                          lambda outcome, reason: self._suite_failure(request, outcome, reason), sources=sources)

    def benchmark(self, request: BenchmarkRequest) -> Benchmark:
        """Time repeated runs of the request's suite, after discarded
        warm-up runs. The result's ``includes`` states what each time
        covers: the runner's process and, in a container, the container's
        or the execution wrapper's overhead."""
        return self._call("benchmark", request, BenchmarkRequest, Benchmark,
                          lambda outcome, reason: Benchmark(outcome, reason, (), 0, request.cpus,
                                                            WALL_TIME_SCOPE, self._identity(request)))

    def profile_suite(self, request: SuiteProfileRequest) -> SuiteProfile:
        """Run the request's suite once under the language's profiler and
        attribute the run's time to the functions it entered. A language
        without it answers ``unsupported`` and runs nothing."""
        return self._call("profile_suite", request, SuiteProfileRequest, SuiteProfile,
                          lambda outcome, reason: SuiteProfile(outcome, reason, None, None, None, None))

    def benchmark_warmed(self, request: WarmedSuiteBenchmarkRequest) -> WarmedSuiteBenchmark:
        """Time the suite in a warmed runtime. A language without it answers
        ``unsupported`` and runs nothing."""
        return self._call("benchmark_warmed", request, WarmedSuiteBenchmarkRequest,
                          WarmedSuiteBenchmark, lambda outcome, reason: self._warmed_failure(
                              request, outcome, reason))

    def _warmed_failure(self, request, outcome, reason):
        context = self.adapter.context
        # The form a warmed run's identity has before its setup.
        identity = WarmedBenchmarkIdentity(
            suite=request.suite.identity(), project_inputs=(),
            toolchain=ToolchainIdentity.of(unknown(self.adapter.language or "unknown")),
            execution=execution_identity(context),
            build_tool=None, build_version=None, engine_version=None,
            jvm_args=tuple(context.interpreter_flags),
            runner_options=(), mode="warmed_jvm", timing_boundary="junit_launcher_execute",
            state_policy=request.state_policy, forks=request.forks,
            warmup_runs=request.warmup_runs, runs=request.runs,
            iteration_timeout_s=request.iteration_timeout_s,
            total_timeout_s=request.total_timeout_s, startup_timeout_s=request.startup_timeout_s,
            requested_cpus=request.cpus, serialize=request.serialize)
        return WarmedSuiteBenchmark(
            outcome, reason, "no_samples", request.forks, request.warmup_runs, request.runs,
            0, 0, 0, 0, (), (), None, identity, request.cpus, None, False, True)

    def per_test_evidence(self, request: PerTestEvidenceRequest) -> PerTestEvidence:
        """Each test's covered items and time from one instrumented run, and
        with a mutation run, the kills attributed to each test."""
        return self._call("per_test_evidence", request, PerTestEvidenceRequest, PerTestEvidence,
                          lambda outcome, reason: PerTestEvidence(
                              outcome, reason, (), (), (), KILLS_UNUSABLE if request.mutation_run is not None
                              else KILLS_NOT_REQUESTED, (), None, None, self._identity(request)))


#: The statuses of an unfinished answer to ``resolve_collisions``: the
#: validation of some source could not run.
_COLLISION_FAILURES = (SourceStatus.INFRASTRUCTURE_FAILURE, SourceStatus.TIMEOUT,
                       SourceStatus.MISSING_SETUP)


def _index(value: Any, among: Any) -> bool:
    """Whether ``value`` is an integer, not a boolean, in ``among``."""
    return isinstance(value, int) and not isinstance(value, bool) and value in among


def _lines_up(resolution: Any, count: int) -> bool:
    """Whether ``resolution`` is a ``CollisionResolution`` of ``count``
    sources whose fields line up with the input, as the type's docstring
    states."""
    if not isinstance(resolution, CollisionResolution):
        return False
    aligned = (resolution.sources, resolution.renames, resolution.deduped,
               resolution.dropped_defs, resolution.localized_autouse)
    ejected, reasons = resolution.ejected, resolution.eject_reasons
    if not all(isinstance(field, (tuple, list)) for field in (*aligned, ejected, reasons)):
        return False
    return (all(len(field) == count for field in aligned)
            and all(isinstance(source, str) for source in resolution.sources)
            and all(_index(index, range(count)) for index in ejected)
            and len(ejected) == len(reasons) == len(set(ejected)))


class SourceOperations:
    """The source operations of an adapter, ``adapter.sources``. Each calls
    the adapter's method of its name, such as ``adapter.check_structure``,
    and answers ``unsupported`` without one. An adapter implements those
    methods or supplies a subclass. Registered metric providers merge into
    immutable results. Collision validation that could not run is distinct
    from a completed resolution that rejected a source."""

    def __init__(self, adapter) -> None:
        self.adapter = adapter
        self._metric_registry = MetricRegistry()
        if (callable(getattr(adapter, "optimization_metrics", None))
                and callable(getattr(adapter, "structural_parser_identity", None))):
            self._metric_registry.register(_StructuralProvider(adapter))
        for provider in adapter.metric_providers():
            self._metric_registry.register(provider)
        from langadapter.registry import _loaded_metric_providers
        for provider, extension in _loaded_metric_providers():
            try:
                self._metric_registry.register(provider)
            except InvalidRequest as error:
                raise InvalidRequest(error.field, f"{error.message}, from the extension "
                                                  f"{extension.file}") from None

    def _method(self, name):
        if name == "metrics":
            return None
        method = getattr(self.adapter, name, None)
        if callable(method) and name in _BASE_DEFAULTS:
            import inspect

            from langadapter.adapter import LangAdapter
            # The attributes as the classes hold them: a class method read
            # through getattr is a new bound method each time.
            if (inspect.getattr_static(type(self.adapter), name, None)
                    is inspect.getattr_static(LangAdapter, name)):
                return None
        if callable(method):
            return method
        units = getattr(self.adapter, "test_units", None)
        if name in _FROM_TEST_UNITS and callable(units):
            return functools.partial(_FROM_TEST_UNITS[name], self.adapter)
        return None

    def execution_backed(self, name: str) -> bool:
        """Whether the source operation ``name`` runs the project's tools: the
        validation, the edits that validate their result, and
        ``target_identity`` in a container context."""
        return name in _SOURCE_RUNS or (
            name == "target_identity" and isinstance(self.adapter.context.execution, ContainerExecution))

    def supports(self, name: str) -> bool:
        """Whether the source operation ``name`` is implemented for this
        adapter and its context's kind of execution. Nothing runs to find
        out."""
        if name not in SOURCE_READS + SOURCE_EDITS:
            return False
        if name == "metrics" and self._metric_registry.definitions():
            return True
        if name == "project_source_files":
            return self._method("is_test_file") is not None
        if name == "call_statistics" and not (self.supports("project_source_files") and any(
                item.name == "function_count" and "function" in item.scopes
                for item in self.metric_definitions())):
            return False
        return (self._method(name) is not None
                and not self.adapter.operation_context_issue(f"sources.{name}")
                and (not self.execution_backed(name)
                     or self.adapter.context.execution.kind in self.execution_kinds(name)))

    def execution_kinds(self, name: str) -> Tuple[str, ...]:
        """The kinds of execution (``local``, ``container``) the source
        operation ``name`` runs in for this adapter, none for an operation
        that runs no project tool."""
        if not self.execution_backed(name):
            return ()
        return tuple(self.adapter.source_execution_kinds)

    def _parser(self, value=None):
        parser = getattr(value, "parser", None)
        if parser:
            return parser
        get = getattr(self.adapter, "structural_parser_identity", None)
        return get() if callable(get) else None

    def _read(
            self, name, *args, parse=(), transform=None, parser_identity=None, answer=None, **kwargs):
        with _scope(self.adapter, self.execution_backed(name)):
            return self._read_owned(name, *args, parse=parse, transform=transform,
                                    parser_identity=parser_identity, answer=answer, **kwargs)

    def _read_owned(
            self, name, *args, parse=(), transform=None, parser_identity=None, answer=None, **kwargs):
        """``name``'s hook as an observation. ``answer`` sees the hook's
        answer first and raises ``TypeError`` for one of another form."""
        context = self.adapter.context
        if self.execution_backed(name):
            context._require_execution()
        method = self._method(name)
        if method is None:
            return SourceObservation(SourceStatus.UNSUPPORTED, reason=f"{name} is not implemented")
        if not self.supports(name):
            return SourceObservation(SourceStatus.UNSUPPORTED, reason=(
                self.adapter.operation_context_issue(f"sources.{name}")
                or f"{name} does not support {self.adapter.context.execution.kind} execution"))
        refused = self.adapter._execution_gate() if self.execution_backed(name) else None
        if refused:
            return SourceObservation(SourceStatus(refused[0].value), reason=refused[1])
        try:
            check = self._method("check_structure")
            for source in parse if check is not None else ():
                verdict = check(source)
                if not verdict.ok:
                    return SourceObservation(SourceStatus.PARSE_ERROR, parser=verdict.parser,
                                             reason=verdict.message)
            value = method(*args, **kwargs)
            if answer is not None:
                answer(value)
            if isinstance(value, SourceObservation):
                return copy.deepcopy(value)
            parser = (parser_identity() if parser_identity is not None else
                      value.interpreter if isinstance(value, SourceValidation) else self._parser(value))
            if isinstance(value, TestMetrics) and not value.parsed:
                return SourceObservation(SourceStatus.PARSE_ERROR, parser=parser, reason=value.detail)
            if isinstance(value, SourceValidation):
                if value.status not in {"valid", "syntax_error"}:
                    status = (SourceStatus.TIMEOUT if value.status == "timeout"
                              else SourceStatus.INFRASTRUCTURE_FAILURE)
                    return SourceObservation(status, reason=value.message)
                parser = value.interpreter
            if hasattr(value, "items") and hasattr(value, "status"):
                if value.status != "completed":
                    return SourceObservation(SourceStatus(value.status), parser=parser,
                                             reason=value.detail)
                value = tuple(value.items)
            if transform is not None:
                value = transform(value)
                if isinstance(value, SourceObservation):
                    return copy.deepcopy(value)
            return SourceObservation(SourceStatus.COMPLETED, value=copy.deepcopy(value), parser=parser)
        except InvalidRequest:
            raise
        except SourceReadError as exc:
            return SourceObservation(SourceStatus.PARSE_ERROR, parser=exc.parser, reason=str(exc))
        except Exception as exc:
            status, reason = _failure(exc)
            return SourceObservation(SourceStatus(status), reason=reason)

    @staticmethod
    def _source(source: str) -> None:
        if not isinstance(source, str):
            raise InvalidRequest("source", "must be text")

    def check_structure(self, source: str) -> SourceObservation[StructureCheck]:
        """Whether the parser reads ``source`` without an error, with the first
        error's message, place and text (``StructureCheck``)."""
        self._source(source)
        return self._read("check_structure", source)

    def normalize_bindings(self, source: str) -> SourceObservation[SuiteNormalization]:
        """The static normalization of shadowed bindings with every removal.
        A source that does not parse is kept as it is, and the project's
        runtime does not validate the rewrite."""
        self._source(source)

        def report(value):
            if not isinstance(value, SuiteNormalization):
                raise TypeError("native normalization must return SuiteNormalization")
            if not value.parse_ok and (value.changed or value.source != source
                                       or value.removed_bindings):
                raise TypeError("failed normalization must preserve the original source")
            return value

        return self._read("normalize_bindings", source, transform=report,
                          parser_identity=getattr(self.adapter, "normalization_parser_identity", None))

    def test_metrics(self, source: str) -> SourceObservation[TestMetrics]:
        """The counts of the test source ``source`` (``TestMetrics``):
        ``parse_error`` when the parser rejects it."""
        self._source(source)
        return self._read("test_metrics", source)

    def register_metric_provider(self, provider: MetricProvider, *, replace: bool = False) -> None:
        """Register ``provider``'s metric definitions on this adapter object.
        A name already registered raises ``InvalidRequest`` unless
        ``replace`` is true, which rebinds only the provider's own names."""
        self._metric_registry.register(provider, replace=replace)

    def metric_definitions(self) -> Tuple[MetricDefinition, ...]:
        """The definition of every metric ``metrics`` measures on this adapter
        object, by name. ``langadapter.sources.STANDARD_METRICS`` names the
        operation that reports each standard metric. A listed name it does
        not hold is the language's own metric of the source text."""
        return self._metric_registry.definitions()

    def metrics(self, source: str, *, names: Optional[Sequence[str]] = None) -> SourceObservation[SourceMetrics]:
        """The metrics of ``source`` that ``metric_definitions`` lists, or
        those ``names`` names (``SourceMetrics``). ``unsupported`` when no
        metric is registered."""
        self._source(source)
        if names is None and not self._metric_registry.definitions():
            return SourceObservation(SourceStatus.UNSUPPORTED, reason="no metric providers are registered")
        return self._metric_registry.measure(source, language=self.adapter.language, parser=None,
                                             names=names, parser_context=self._parser)

    def project_source_files(self) -> SourceObservation[Tuple[str, ...]]:
        """The project's non-test source files, as sorted POSIX paths relative
        to the context's root: the files with the language's ``file_extensions``
        outside its ``ignored_dirs`` and the directories whose name starts with
        a dot, leaving out its ``non_source_files``, the files
        ``is_test_file`` names and empty files. A file that does not parse
        stays in the list, since no file's content is read. The files are
        read on the host, so a root or directory the host cannot read, such as
        a container context's root outside the host, is an infrastructure
        failure."""
        if not self.supports("project_source_files"):
            return SourceObservation(SourceStatus.UNSUPPORTED,
                                     reason="the adapter tells no test file from a source file (is_test_file)")
        try:
            return SourceObservation(SourceStatus.COMPLETED, _project_source_files(self.adapter))
        except OSError as exc:
            return SourceObservation(SourceStatus.INFRASTRUCTURE_FAILURE, reason=f"{type(exc).__name__}: {exc}")

    def call_statistics(self, target: str) -> SourceObservation[SourceMetrics]:
        """The call statistics of the source file ``target``, a path relative
        to the context's root as ``project_source_files`` lists it. For each
        function the target declares, ``call_site_count_estimate`` counts the
        call sites of its bare name in the files ``project_source_files``
        lists, the target left out, so test files are not searched, and
        ``calling_file_count_estimate`` counts the files that hold one. The
        file scope holds their sums over the functions, each calling file
        counted once. Names are matched, not resolved: a call of another
        function with the same bare name counts, and an import under another
        name, a reference that is not a call and a dynamic call do not. A
        project file that does not parse is left out and named in the values'
        reason. A target outside the root is refused as an invalid request."""
        self._source(target)
        if not self.supports("call_statistics"):
            return SourceObservation(SourceStatus.UNSUPPORTED, reason=(
                "call_statistics needs the call search, is_test_file and the metrics' function scopes"))
        root = Path(self.adapter.context.root).resolve()
        resolved = (root / target).resolve()
        try:
            own = os.path.normcase(resolved.relative_to(root).as_posix())
        except ValueError:
            raise InvalidRequest("target", "must be a file inside the project root") from None
        try:
            declared = self._declared_functions(resolved.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            return SourceObservation(SourceStatus.INFRASTRUCTURE_FAILURE, reason=f"{type(exc).__name__}: {exc}")
        if not declared.completed:
            return declared
        listing = self.project_source_files()
        if not listing.completed:
            return SourceObservation(listing.status, reason=listing.reason)
        found = {path: _called_names(self._method("call_statistics"), root / path)
                 for path in listing.value if os.path.normcase(path) != own}
        return SourceObservation(SourceStatus.COMPLETED, _call_metrics(
            self.adapter.language, declared.value, found, declared.parser), parser=declared.parser)

    def _declared_functions(self, source: str) -> SourceObservation[Tuple[MetricScope, ...]]:
        """The function scopes ``metrics`` reports for ``source``, so the call
        statistics name the functions as the metrics do."""
        measured = self.metrics(source, names=["function_count"]).value
        if measured.unavailable:
            missing = measured.unavailable[0]
            return SourceObservation(missing.status, parser=missing.parser, reason=missing.reason)
        return SourceObservation(SourceStatus.COMPLETED, measured.functions, parser=self._parser())

    def skeleton(self, source: str) -> SourceObservation[Tuple[str, ...]]:
        """The names of the definitions ``source`` declares, such as its
        functions and classes, as the language qualifies them."""
        self._source(source)
        return self._read("skeleton", source, parse=(source,), transform=tuple)

    def same_skeleton(self, first: str, second: str) -> SourceObservation[Tuple[bool, str]]:
        """Whether ``first`` and ``second`` declare the same definition names,
        as a (verdict, reason) pair."""
        self._source(first)
        self._source(second)
        return self._read("same_skeleton", first, second, parse=(first, second), transform=tuple)

    def function_fingerprints(self, source: str) -> SourceObservation[Tuple[Tuple[str, str], ...]]:
        """A fingerprint of each definition ``source`` declares, as (name,
        fingerprint) pairs sorted by name."""
        self._source(source)
        return self._read("function_fingerprints", source, parse=(source,),
                          transform=lambda value: tuple(sorted(value.items())))

    def imports(self, source: str) -> SourceObservation[Tuple[str, ...]]:
        """The names ``source`` imports, in source order."""
        self._source(source)
        return self._read("imports", source, parse=(source,), transform=tuple)

    def import_path_candidates(self, name: str) -> SourceObservation[Tuple[str, ...]]:
        """The source paths an absolute import of ``name`` may refer to, found
        without resolving the import."""
        self._source(name)
        return self._read("import_path_candidates", name, transform=tuple)

    def import_statement(self, target: str) -> SourceObservation[str]:
        """The line a first test of ``target`` uses to import it, or a
        sentence (``none; ...``) where the language's tests need none, for
        this context's project, or "" when the adapter cannot tell."""
        self._source(target)
        return self._read("import_statement", target, str(self.adapter.context.root))

    def code_smells(self, paths: Sequence[str]) -> SourceObservation[int]:
        """The count of the language's linter findings in ``paths``.
        ``missing_setup`` when the linter could not run."""
        if isinstance(paths, (str, bytes)):
            raise InvalidRequest("paths", "must be a sequence of paths")
        return self._read("code_smells", [str(path) for path in paths], transform=lambda count: (
            SourceObservation(SourceStatus.MISSING_SETUP, reason="the linter could not run")
            if count is None else count))

    def duplicate_test_names(self, source: str) -> SourceObservation[Tuple[str, ...]]:
        """The names of the tests ``source`` defines more than once in one
        scope, where the later definition hides the earlier."""
        self._source(source)
        return self._read("duplicate_test_names", source, parse=(source,), transform=tuple)

    def merge_collisions(self, first: str, second: str) -> SourceObservation[Tuple[str, ...]]:
        """The top-level names both ``first`` and ``second`` define, on which a
        merge of the two collides. Nothing is rewritten."""
        self._source(first)
        self._source(second)
        return self._read("merge_collisions", first, second, parse=(first, second), transform=tuple)

    def is_test_file(self, path: str) -> SourceObservation[bool]:
        """Whether the file ``path`` is a test file of the language, by its
        name."""
        self._source(path)
        return self._read("is_test_file", path)

    def validate_source(self, source: str) -> SourceObservation[SourceValidation]:
        """Whether the project's own compiler or interpreter accepts ``source``
        (``SourceValidation``)."""
        self._source(source)
        return self._read("validate_source", source)

    def target_identity(self, target: str) -> SourceObservation[TargetIdentity]:
        """The identity of the file under test ``target`` that the validity
        policy reads (``TargetIdentity``), such as the name its tests import
        it by."""
        self._source(target)
        return self._read("target_identity", target)

    def resolve_collisions(self, sources: Sequence[str], *,
                           preference: Optional[Callable[[str, Tuple[int, ...]], int]] = None,
                           localize_autouse: bool = False) -> SourceObservation[CollisionResolution]:
        """The top-level name collisions among ``sources`` resolved before
        they are merged into one file (``CollisionResolution``). ``preference``
        takes a name that several sources bind in a way no rename can change
        and the indexes of those sources, and returns the index whose
        binding stays. Without it each such name is reported as a conflict
        and nothing is dropped. ``localize_autouse`` keeps each Python
        source's module-level autouse fixtures to that source's own tests."""
        if isinstance(sources, (str, bytes)) or not isinstance(sources, Sequence):
            raise InvalidRequest("sources", "must be a sequence of source texts")
        sources = tuple(sources)
        for source in sources:
            self._source(source)
        if preference is not None and not callable(preference):
            raise InvalidRequest("preference", "must be callable or None")
        if not isinstance(localize_autouse, bool):
            raise InvalidRequest("localize_autouse", "must be boolean")

        def choose(name, owners):
            result = preference(name, owners)
            if not _index(result, owners):
                raise InvalidRequest("preference", "must select an owning source index")
            return result

        def answer(value):
            *first, last = (status.value for status in _COLLISION_FAILURES)
            expected = ("native resolution must return a SourceObservation: a completed one "
                        "with its parser and a CollisionResolution that lines up with the "
                        f"sources, or an unfinished one with the status {', '.join(first)} or "
                        f"{last} and a reason")
            if not isinstance(value, SourceObservation):
                raise TypeError(expected)
            if not value.completed:
                if value.status not in _COLLISION_FAILURES or not value.reason:
                    raise TypeError(expected)
                return
            if not isinstance(value.parser, str) or not _lines_up(value.value, len(sources)):
                raise TypeError(expected)

        return self._read("resolve_collisions", sources, preference=choose if preference is not None else None,
                          localize_autouse=localize_autouse, answer=answer)

    def stage_pool(self, members: Sequence[PoolMember], *,
                   setup: Sequence[Tuple[str, str]] = ()) -> SourceObservation[StagedPool]:
        """Several suites' test files staged as one pooled suite, with the
        way back to each suite's standalone identities (``StagedPool``).
        ``setup`` holds the setup files every suite shares, as (relative
        path, text) pairs. A pool the language cannot stage is an
        observation with its reason."""
        from langadapter.sources import check_members, check_setup
        members = check_members(members)
        setup = check_setup(setup)

        def staged(value):
            if not isinstance(value, StagedPool):
                raise TypeError("native pooled staging must return StagedPool")
            return value

        return self._read("stage_pool", members, setup=setup, transform=staged)

    def analyze_validity(self, source: str, target: TargetIdentity) -> SourceObservation:
        """The validity policy's findings on the test source ``source`` for the
        file under test ``target`` (``PolicyReport``): the places where a
        test reads the implementation's source or internals instead of
        calling it."""
        self._source(source)
        return self._read("analyze_validity", source, target)

    def screen_source(self, source: str, target: TargetIdentity) -> SourceObservation:
        """The validity policy's review-only signals on the test source
        ``source`` for the file under test ``target`` (``PolicyReport``)."""
        self._source(source)
        return self._read("screen_source", source, target)

    def _unsupported_edit(self, name: str, source: str) -> SourceResult:
        self._source(source)
        return SourceResult("unsupported", source, "", detail=(
            self.adapter.operation_context_issue(f"sources.{name}") or f"{name} is not implemented in this group"))

    def _edit(self, name: str, source: str, *args, **kwargs) -> SourceResult:
        with _scope(self.adapter, self.execution_backed(name)):
            return self._edit_owned(name, source, *args, **kwargs)

    def _edit_owned(self, name, source, *args, **kwargs):
        self._source(source)
        issue = self._edit_issue(name, source)
        if issue is not None:
            return issue
        try:
            method = self._method(name)
            result = method(source, *args, **kwargs)
            if (not isinstance(result, SourceResult) or result.status not in SOURCE_STATUSES
                    or not isinstance(result.source, str) or not isinstance(result.parser, str)):
                raise TypeError("native edits must return a known SourceResult")
            return copy.deepcopy(result)
        except InvalidRequest:
            raise
        except Exception as exc:
            status, detail = _failure(exc)
            return SourceResult(status, source, "", detail=detail)

    def _edit_issue(self, name: str, source: str) -> Optional[SourceResult]:
        if self.execution_backed(name):
            self.adapter.context._require_execution()
        if self._method(name) is None or not self.supports(name):
            return self._unsupported_edit(name, source)
        refused = self.adapter._execution_gate() if self.execution_backed(name) else None
        if refused:
            return SourceResult(refused[0].value, source, "", detail=refused[1])
        return None

    def _merge_separator(self) -> Optional[str]:
        """The comment prefix of a merge separator as the adapter declares it
        (``source_merge_separator``), or None."""
        return getattr(self.adapter, "source_merge_separator", None)

    def prune_tests(self, source: str, tests: Iterable[TestId | str]) -> SourceResult:
        """``source`` without the tests ``tests`` names, given as names or
        ``TestId`` values (``SourceResult``)."""
        self._source(source)
        if isinstance(tests, (str, bytes)):
            raise InvalidRequest("tests", "must be an iterable of test names or TestId values")
        try:
            tests = tuple(tests)
            for test in tests:
                name = test.qualname if isinstance(test, TestId) else test
                if not isinstance(name, str) or not name.strip():
                    raise InvalidRequest("tests", "must contain non-empty test names or TestId values")
        except TypeError:
            raise InvalidRequest("tests", "must be an iterable of test names or TestId values") from None
        return self._edit("prune_tests", source, tests)

    def merge_sources(self, first: str, second: str, *, separator: Optional[str] = None) -> SourceResult:
        """``first`` and ``second`` joined as one test file, with the comment
        line ``separator`` between them (``SourceResult``). Without
        ``separator`` the adapter's ``source_merge_separator`` is used, and
        an adapter without one needs it. A given separator must start with
        the adapter's ``source_merge_separator`` where it has one."""
        self._source(first)
        self._source(second)
        if separator is not None and not isinstance(separator, str):
            raise InvalidRequest("separator", "must be text or None")
        if separator is not None and ("\n" in separator or "\r" in separator):
            raise InvalidRequest("separator", "must be one comment line")
        if self._method("merge_sources") is None or not self.supports("merge_sources"):
            return self._unsupported_edit("merge_sources", first)
        try:
            prefix = self._merge_separator()
        except Exception as exc:
            status, detail = _failure(exc)
            return SourceResult(status, first, "", detail=detail)
        if prefix is not None and (not isinstance(prefix, str) or not prefix or "\n" in prefix or "\r" in prefix):
            return SourceResult("infrastructure_failure", first, "", detail="invalid native merge separator metadata")
        separator = prefix if separator is None else separator
        if separator is None:
            raise InvalidRequest("separator", "this adapter needs an explicit comment separator")
        if prefix is not None and not separator.startswith(prefix):
            raise InvalidRequest("separator", f"must be one {prefix} comment line")
        return self._edit("merge_sources", first, second, separator=separator)

    def normalize_shadowed(self, source: str) -> SourceResult:
        """``source`` without the definitions a later one of the same name
        hides (``SourceResult``)."""
        return self._edit("normalize_shadowed", source)

    def salvage_syntax(self, source: str) -> SourceResult:
        """A bounded repair of a source the project's compiler or interpreter
        rejects (``SourceResult``)."""
        return self._edit("salvage_syntax", source)

    def prune_unresolvable_imports(self, source: str) -> SourceResult:
        """``source`` without the imports that do not resolve in the project
        (``SourceResult``)."""
        return self._edit("prune_unresolvable_imports", source)

    def prune_collection_breakers(self, source: str, *, timeout_s: float) -> SourceResult:
        """``source`` without what keeps its tests from loading, so the rest
        run. Each of at most five rounds loads the tests as the language
        does (collection, compilation) and removes the smallest unit the
        failure names (the adapter's ``collection_unit``), and ``removed``
        names each. A pruned source is returned only once its tests load.
        ``unchanged`` when they load as they are, ``not_repaired`` with the
        input when they still do not."""
        _duration(timeout_s, "timeout_s", positive=True)
        return self._edit("prune_collection_breakers", source, timeout_s=timeout_s)

    def strip_module_level_skips(self, source: str) -> SourceResult:
        """``source`` without the statements or annotations that skip the whole
        test file (``SourceResult``)."""
        return self._edit("strip_module_level_skips", source)

    def finalize_source(self, source: str) -> SourceResult:
        """``source`` without detached comment blocks (``SourceResult``)."""
        return self._edit("finalize_source", source)


def _units_parser(adapter) -> str:
    return f"{getattr(adapter, 'language', '') or type(adapter).__name__} test units"


def _units(adapter, source):
    """The adapter's test units of ``source``, or None when it does not parse."""
    units = adapter.test_units(source)
    return None if units is None else tuple(units)


def _prune_by_units(adapter, source: str, tests) -> SourceResult:
    """``source`` without the named tests, from the adapter's test units: a
    name names the longest unit name it ends with after a dot
    (``templates.named_unit``), and every unit of that name is cut."""
    from langadapter import templates

    parser = _units_parser(adapter)
    names = list(dict.fromkeys(t.qualname if isinstance(t, TestId) else str(t) for t in tests))
    units = _units(adapter, source)
    if units is None:
        return SourceResult(PARSE_ERROR, source, parser, unmatched=tuple(names),
                            detail="the source does not parse")
    removed, unmatched = [], []
    for name in names:
        match = templates.named_unit(name, [unit.name for unit in units])
        if match is None:
            unmatched.append(name)
        elif match not in removed:
            removed.append(match)
    spans = sorted((unit.start, unit.end) for unit in units if unit.name in removed)
    if not spans:
        return SourceResult(UNCHANGED, source, parser, unmatched=tuple(unmatched))
    kept, position = [], 0
    for start, end in spans:
        kept.append(source[position:max(position, start)])
        position = max(position, end)
    kept.append(source[position:])
    return SourceResult(APPLIED, "".join(kept), parser, removed=tuple(removed),
                        unmatched=tuple(unmatched))


def _metrics_by_units(adapter, source: str) -> TestMetrics:
    """The test count of ``source`` from the adapter's test units."""
    units = _units(adapter, source)
    if units is None:
        return TestMetrics(False, _units_parser(adapter), detail="the source does not parse")
    return TestMetrics(True, _units_parser(adapter), tests=len(units))


def _duplicates_by_units(adapter, source: str):
    """The test names ``source`` declares more than once, from the
    adapter's test units, in order."""
    units = _units(adapter, source)
    if units is None:
        return SourceObservation(SourceStatus.PARSE_ERROR, parser=_units_parser(adapter),
                                 reason="the source does not parse")
    names = [unit.name for unit in units]
    return tuple(dict.fromkeys(name for name in names if names.count(name) > 1))


#: The hooks the base adapter defines as "not offered": an adapter offers
#: the operation only by writing its own.
_BASE_DEFAULTS = frozenset({"import_statement", "code_smells"})


def _called_names(search, path: Path) -> Optional[Mapping[str, int]]:
    """The adapter's call search of the file ``path``, None when it does not
    parse or cannot be read."""
    try:
        return search(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


def _call_metrics(language: str, functions: Sequence[MetricScope],
                  found: Mapping[str, Optional[Mapping[str, int]]], parser: Optional[str]) -> SourceMetrics:
    """The call statistics of ``functions`` over the call sites ``found`` in
    each other file, None for a file left out."""
    left_out = sorted(path for path, names in found.items() if names is None)
    reason = f"left out, since they do not parse or cannot be read: {', '.join(left_out)}" if left_out else ""
    searched = {path: names for path, names in found.items() if names is not None}

    def values(sites, files):
        return tuple(MetricValue(MetricDefinition(name, STANDARD_METRICS[name].unit, "name-match-1",
                                                  STANDARD_METRICS[name].scopes),
                                 "langadapter.call_statistics", parser, SourceStatus.COMPLETED, count, reason)
                     for name, count in (("call_site_count_estimate", sites),
                                         ("calling_file_count_estimate", files)))

    scopes, sites, callers = [], 0, set()
    for scope in functions:
        bare = scope.name.rsplit(".", 1)[-1]
        calling = {path for path, names in searched.items() if names.get(bare)}
        count = sum(names.get(bare, 0) for names in searched.values())
        scopes.append(replace(scope, metrics=values(count, len(calling))))
        sites, callers = sites + count, callers | calling
    return SourceMetrics(language, MetricScope("file", "", None, None, None, values(sites, len(callers))),
                         tuple(scopes))


def _project_source_files(adapter) -> Tuple[str, ...]:
    """The listing of ``SourceOperations.project_source_files``. A root or
    directory the walk cannot read raises its OSError."""
    root = Path(adapter.context.root)
    skipped = set(adapter.ignored_dirs)
    found = []
    for current_dir, sub_dirs, files in os.walk(root, onerror=_walk_error):
        sub_dirs[:] = [name for name in sub_dirs if name not in skipped and not name.startswith(".")]
        for name in files:
            path = Path(current_dir, name)
            relative = path.relative_to(root)
            if (name.endswith(tuple(adapter.file_extensions)) and name not in adapter.non_source_files
                    and path.stat().st_size and not adapter.is_test_file(relative)):
                found.append(relative.as_posix())
    return tuple(sorted(found))


def _walk_error(error: OSError) -> None:
    """``os.walk``'s error handler that raises the error it is given."""
    raise error


#: The operations the base derives from an adapter's ``test_units`` hook
#: when the adapter does not write them.
_FROM_TEST_UNITS = {"prune_tests": _prune_by_units, "test_metrics": _metrics_by_units,
                    "duplicate_test_names": _duplicates_by_units}


class _UnavailableSession(MutationSession):
    def __init__(self, outcome, reason, workers):
        super().__init__(workers_requested=workers, outcome=outcome, reason=reason)

    def measure(self, request):
        return self._failed_result(request, self.outcome, self.reason)

    def release(self):
        pass


class MutationOperations:
    """The mutation operations of an adapter, ``adapter.mutations``. The
    adapter names its engines and where each runs, provides their catalogue
    and session methods, and checks each engine's options. Listing the
    engines does not check that they are installed. All sessions share one
    lifecycle."""

    def __init__(self, adapter) -> None:
        self.adapter = adapter

    def engines(self) -> tuple[str, ...]:
        """The names of the mutation engines this adapter implements, without
        checking that they are installed."""
        names = tuple(self.adapter.native_mutation_engines)
        if (any(not isinstance(name, str) or not name.strip() for name in names)
                or len(set(names)) != len(names)):
            raise InvalidRequest("engines", "must declare distinct non-empty engine names")
        return names

    def execution_backed(self, name: str) -> bool:
        """Whether the mutation operation ``name`` runs the project's tools.
        Listing the engines does not."""
        return name in MUTATION_OPERATIONS and name != "engines"

    def execution_kinds(self, name: str) -> tuple[str, ...]:
        """The kinds of execution (``local``, ``container``) the mutation
        operation ``name`` runs in for this adapter."""
        if name not in MUTATION_OPERATIONS:
            return ()
        return ("local", "container") if name == "engines" else tuple(self.adapter.mutation_execution_kinds)

    def supports(self, name: str) -> bool:
        """Whether the mutation operation ``name`` is implemented for this
        adapter and its context's kind of execution. Nothing runs to find
        out."""
        if name not in MUTATION_OPERATIONS:
            return False
        if name == "engines":
            return bool(self.engines())
        native = "mutation_operators" if name == "operators" else "open_mutation_session"
        return (bool(self.engines()) and callable(getattr(self.adapter, native, None))
                and not self.adapter.operation_context_issue(f"mutations.{name}")
                and self.adapter.context.execution.kind in self.execution_kinds(name))

    def evidence_granularity(self, name: str) -> tuple[str, ...]:
        """The granularities of the evidence a session's ``run`` or
        ``per_suite`` reports for this adapter, empty for the other
        operations."""
        return (tuple(self.adapter.mutation_evidence_granularity)
                if name in ("run", "per_suite") else ())

    def resolution_methods(self, name: str) -> tuple[str, ...]:
        """How the verdicts of ``run`` and ``per_suite`` are resolved:
        ``measured``, by running the tests."""
        return ("measured",) if name in ("run", "per_suite") else ()

    def _validate_engine(self, engine: str) -> None:
        if not isinstance(engine, str) or not engine.strip():
            raise InvalidRequest("engine", "must be a non-empty engine name")
        known = self.engines()
        if known and engine not in known:
            raise InvalidRequest("engine", f"unknown engine {engine!r}. Known: {', '.join(known)}")

    def operators(self, engine: str) -> OperatorCatalogue:
        """The mutation operators ``engine`` offers in the project's
        environment. An engine the adapter does not offer here is
        ``unsupported``, and a missing runtime or engine is
        ``missing_setup``."""
        with _scope(self.adapter, True):
            return self._operators_owned(engine)

    def _operators_owned(self, engine):
        self.adapter.context._require_execution()
        self._validate_engine(engine)
        if not self.supports("operators"):
            return OperatorCatalogue(engine, None, Outcome.UNSUPPORTED,
                                     self.adapter.operation_context_issue("mutations.operators")
                                     or "operator discovery is not implemented for this context")
        refused = self.adapter._execution_gate()
        if refused:
            return OperatorCatalogue(engine, None, *refused)
        result = self.adapter.mutation_operators(engine)
        if not isinstance(result, OperatorCatalogue) or result.engine != engine:
            raise TypeError("native operator discovery must return the requested OperatorCatalogue")
        return copy.deepcopy(result)

    def open_session(self, engine: str, *, workers: int = 1,
                     distributor: Optional[str] = None, allow_fewer_workers: bool = False,
                     settings: object = None) -> MutationSession:
        """A mutation session of ``engine`` for the project, to run
        measurements in and close (``with`` closes it). The options are
        those the settings guide lists: ``workers``, and for Cosmic Ray
        ``distributor``, ``allow_fewer_workers`` and ``settings``."""
        with _scope(self.adapter, True):
            return self._open_owned(engine, workers=workers, distributor=distributor,
                                    allow_fewer_workers=allow_fewer_workers, settings=settings)

    def _open_owned(self, engine, *, workers, distributor, allow_fewer_workers, settings):
        self.adapter.context._require_execution()
        self._validate_engine(engine)
        if type(workers) is not int or workers < 1:
            raise InvalidRequest("workers", "must be a positive integer")
        if type(allow_fewer_workers) is not bool:
            raise InvalidRequest("allow_fewer_workers", "must be true or false")
        if distributor is not None and (not isinstance(distributor, str) or not distributor.strip()):
            raise InvalidRequest("distributor", "must be a distributor name or None")
        validate = getattr(self.adapter, "validate_mutation_session_options", None)
        if callable(validate):
            validate(engine, workers=workers, distributor=distributor,
                     allow_fewer_workers=allow_fewer_workers, settings=settings)
        elif settings is not None or distributor is not None or allow_fewer_workers:
            raise InvalidRequest("options", "engine-specific options are not implemented")
        if not self.supports("open_session"):
            return _UnavailableSession(Outcome.UNSUPPORTED,
                                       self.adapter.operation_context_issue("mutations.open_session")
                                       or "mutation sessions are not implemented for this context", workers)
        refused = self.adapter._execution_gate()
        if refused:
            return _UnavailableSession(*refused, workers)
        options = {"workers": workers}
        if distributor is not None:
            options["distributor"] = distributor
        if allow_fewer_workers:
            options["allow_fewer_workers"] = allow_fewer_workers
        if settings is not None:
            options["settings"] = settings
        session = self.adapter.open_mutation_session(engine, **options)
        if not isinstance(session, MutationSession):
            close = getattr(session, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:
                    raise MutationCleanupError(session, str(exc)) from exc
            raise TypeError("native opening must return the common MutationSession")
        return session
