"""Mutation sessions of the Java adapter (PIT), and the dotted ids of
PIT's named killers and of tests (``dotted_id``, ``test_dotted_id``).

PIT starts its own minion JVMs per run, so a session starts nothing, and
``workers`` are a run's minion threads (``--threads``). A run compiles the
suite, runs PIT on the target's class with the suite's test classes and
reads PIT's report into a ``MutationRun``. Without a catalogue a run
measures PIT's default mutators. A named one is a PIT group
(``STRONGER``, ``DEFAULTS``). An operator list names PIT mutators or their
reported ids, the ids a mutant's operator uses.
"""
from __future__ import annotations

from langadapter._execution_scope import reraise_unresolved
from contextlib import ExitStack
from pathlib import Path, PurePath, PurePosixPath
import subprocess
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Sequence, Tuple
from langadapter.errors import InvalidRequest
from langadapter.execution import (
    ContainerExecutor, Executor, _retain_scratch, execution_identity,
)
from langadapter.outcomes import ObservationIdentity, ToolchainIdentity, Outcome
from langadapter.java import _jacoco, _tools as _junit, _pit, _tools
from langadapter.java._env import version_of, JavaEnv, require_mounted_output
from langadapter.java._build import compile_suite, tool_environment
from langadapter.mutation import (
    _chosen, _common_settings, _summarize, MutationClass,
    FROM_DEFAULT, FROM_REQUEST, EffectiveSetting,
    MutationRequest, TimeoutRule, FAILED_INITIALIZATION, FAILED_INSTRUMENT_CHECK,
    FAILED_SESSION, FAILED_SESSION_READ, Mutant, MutationIdentity, MutationRun,
    mutant_key, MutationSession as CommonMutationSession, UNTESTED_MUTANTS_IN_CATALOGUE,
    _require_engine,
)
from langadapter.suites import TestId


ENGINE = "pit"
#: PIT's timeout rule: a test may take this factor times its time plus the constant.
PIT_TIMEOUT_FACTOR = 1.25
PIT_TIMEOUT_CONSTANT_S = 4
#: PIT runs a unit of mutants per minion thread, by default a class's all, so
#: one or two threads work. With several workers a unit holds at most this
#: many (mutationUnitSize), fixed as PIT counts mutants only in the run. Each
#: unit starts a JVM. One worker keeps PIT's default, 0.
PIT_UNIT_SIZE = 10


def pit_unit_size(workers: int) -> int:
    """PIT's ``mutationUnitSize`` for a run with ``workers`` threads."""
    return PIT_UNIT_SIZE if workers > 1 else 0


class MutationSession(CommonMutationSession):
    """A PIT session for an adapter's project, ended by ``close`` or ``with``."""

    def __init__(self, adapter: Any, *, workers: int = 1) -> None:
        self.adapter = adapter
        reason = adapter._context_issue() or adapter._runtime_refusal()
        setup_issue = adapter.execution_setup_issue()
        missing = (adapter.context.interpreter is None or adapter.context.interpreter.path is None
                   or bool(setup_issue))
        outcome = Outcome.UNSUPPORTED if reason else Outcome.MISSING_SETUP if missing else Outcome.COMPLETED
        super().__init__(workers_requested=workers,
                         workers_started=workers if outcome is Outcome.COMPLETED else 0,
                         outcome=outcome, reason=reason or setup_issue or ("no project runtime is available" if missing else ""))
        self._main_classes: Tuple[str, ...] = ()
        self._prepared = None
        self._target_failure = None

    def release(self) -> None:
        self._prepared = None
        self._target_failure = None
        self._main_classes = ()

    # Runs

    def _failed_result(self, request: MutationRequest, outcome: Outcome, reason: str) -> MutationRun:
        return MutationRun.failed(request.target, outcome, reason,
                                  failure=FAILED_SESSION, settings=self._effective_settings(request))

    def _validate_request(self, request: MutationRequest) -> None:
        request.validate()
        adapter = self.adapter
        files = adapter._suite_files(request.suite)
        settings = self._effective_settings(request)
        workers = settings["workers"].value
        if workers > self.workers_started:
            raise InvalidRequest(
                "workers", f"the session has {self.workers_started} "
                f"worker(s). A run can use them or fewer")
        self._target_failure = None
        try:
            target = adapter._project_file("target", request.target)
        except (OSError, subprocess.SubprocessError) as exc:
            reraise_unresolved(exc)
            target = None
            self._target_failure = f"the target path could not be read: {exc}"
        self._prepared = target, files, settings, self._mutators(request)

    def measure(self, request: MutationRequest) -> MutationRun:
        """Measure with PIT (the base class guards run and close)."""
        target, files, settings, mutators = self._prepared
        if self._target_failure is not None:
            return MutationRun.failed(request.target, Outcome.INFRASTRUCTURE_FAILURE,
                                      self._target_failure, failure=FAILED_INITIALIZATION,
                                      settings=settings)
        adapter = self.adapter
        name = target.name
        workers = settings["workers"].value
        resolution = adapter.project()
        if resolution.outcome is not Outcome.COMPLETED:
            return MutationRun.failed(
                name, resolution.outcome, resolution.reason,
                failure=FAILED_INITIALIZATION, settings=settings)
        project = resolution.project
        self._main_classes = project.main_classes
        jdk = adapter._jdk()
        environment = adapter.context.environment
        host = _tools.host_environment(adapter.context)
        container_options = {"executor": adapter._executor}
        env = tool_environment(jdk, environment, executor=adapter._executor)
        scope = None

        def failed(outcome: Outcome, reason: str, *, failure: str,
                   identity: Optional[MutationIdentity] = None) -> MutationRun:
            if scope is not None and outcome in (Outcome.INFRASTRUCTURE_FAILURE, Outcome.TIMED_OUT):
                _retain_scratch(scope, reason)
            return MutationRun.failed(name, outcome, reason, failure=failure,
                                      identity=identity, settings=settings)

        with ExitStack() as stack:
            try:
                allocated = stack.enter_context(adapter._scratch("mutation"))
                scratch = Path(allocated)
                require_mounted_output(str(scratch), adapter._executor)
                scope = allocated
                operators = _pit.mutator_ids(
                    jdk.tool("java"), mutators, scratch,
                    environment=host, env=env, **container_options)
                order = _pit.test_order(jdk.tool("javac"), host, env, **container_options,
                                       scratch=scratch / "order")
                tools = _junit.toolset(project.run_classpath(), host, **container_options,
                                       scratch=scratch / "tools")
                target_identity = JavaEnv(adapter._executor).identity(target, adapter.context.root)
            except _pit.UnknownMutator as exc:
                # The pinned registry admitted the names, so PIT and the pin differ.
                reraise_unresolved(exc)
                return failed(Outcome.INFRASTRUCTURE_FAILURE,
                              f"PIT {_pit.VERSION} does not know a name its pinned registry "
                              f"holds: {exc}", failure=FAILED_INITIALIZATION)
            except _tools.ToolUnavailable as exc:
                reraise_unresolved(exc)
                return failed(Outcome.MISSING_SETUP, str(exc), failure=FAILED_INITIALIZATION)
            except (OSError, subprocess.SubprocessError) as exc:
                reraise_unresolved(exc)
                return failed(Outcome.INFRASTRUCTURE_FAILURE,
                              f"PIT preparation failed: {exc}", failure=FAILED_INITIALIZATION)
            try:
                compiled = compile_suite(project, jdk, request.suite,
                                         str(scratch / "classes"),
                                         adapter._executor,
                                         environment=environment)
            except (OSError, subprocess.SubprocessError) as exc:
                reraise_unresolved(exc)
                return failed(Outcome.INFRASTRUCTURE_FAILURE,
                              f"PIT suite preparation failed: {exc}", failure=FAILED_INITIALIZATION)
            if compiled.outcome is not Outcome.COMPLETED:
                return failed(
                    compiled.outcome,
                    "the suite does not compile: "
                    + compiled.diagnostics[-1000:],
                    failure=FAILED_INITIALIZATION)
            try:
                classes = adapter._class_names(files)
                target_classes = _jacoco.class_names(target, **container_options)
            except (OSError, subprocess.SubprocessError) as exc:
                reraise_unresolved(exc)
                return failed(Outcome.INFRASTRUCTURE_FAILURE,
                              f"PIT test classes could not be read: {exc}", failure=FAILED_INITIALIZATION)
            # The normal run's classpath order (suite, tools, project), without
            # the console launcher PIT does not use. PIT's own jars precede it.
            launcher = [j for j in tools.jars
                        if not Path(j).name.startswith("junit-platform-console-")]
            try:
                report = _pit.run(
                    jdk.tool("java"), scratch=scratch,
                    target_classes=target_classes,
                    test_classes=list(classes),
                    source_dirs=project.main_sources,
                    test_classpath=[compiled.classes, *launcher,
                                    *project.run_classpath()],
                    mutable_paths=project.main_classes,
                    mutators=mutators,
                    all_killers=settings["attribution"].value == "all",
                    threads=workers,
                    unit_size=settings["mutation_unit_size"].value,
                    timeout_factor=settings["per_mutant_timeout"].inputs.get(
                        "factor", 0.0),
                    timeout_constant_s=settings["per_mutant_timeout"].value,
                    jvm_args=[*project.test_jvm_args, *adapter.context.interpreter_flags],
                    system_properties=project.test_system_properties,
                    encoding=project.encoding, cwd=project.root, env=env,
                    environment=host,
                    wall_clock_s=settings["wall_clock_limit"].value,
                    plugins=[str(order)], **container_options)
            except (_tools.ToolUnavailable, OSError, subprocess.SubprocessError) as exc:
                reraise_unresolved(exc)
                return failed(Outcome.INFRASTRUCTURE_FAILURE,
                              f"PIT could not execute: {exc}", failure=FAILED_SESSION)
            identity = MutationIdentity(
                observation=ObservationIdentity(
                    target=target_identity,
                    suite=request.suite.identity(),
                    toolchain=ToolchainIdentity.of(version_of(jdk), (
                        ("junit-platform", tools.platform),
                        *((("junit4", tools.junit4),) if tools.junit4 else ()),
                        ("pitest", _pit.VERSION),
                        ("pitest-junit5-plugin", _pit.JUNIT5_PLUGIN_VERSION))),
                    instrument=None,
                    execution=execution_identity(adapter.context)),
                catalogue=request.catalogue.name or request.catalogue.kind,
                operators=operators,
                per_mutant_timeout_s=settings["per_mutant_timeout"].value,
                attribution="first" if report.note else settings["attribution"].value,
                workers=workers)
            try:
                result = self._read(report, target, classes, request, settings,
                                    operators, identity)
            except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError) as exc:
                reraise_unresolved(exc)
                return failed(Outcome.INFRASTRUCTURE_FAILURE,
                              f"PIT's report could not be read: {exc}",
                              failure=FAILED_SESSION_READ, identity=identity)
            if (result.outcome in (Outcome.INFRASTRUCTURE_FAILURE, Outcome.TIMED_OUT)
                    or result.incomplete is not None):
                _retain_scratch(scope, result.reason or "PIT returned an incomplete catalogue")
            return result

    def _read(self, report: _pit.Report, target: PurePath,
              classes: Dict[str, Path], request: MutationRequest,
              settings: Dict[str, EffectiveSetting],
              operators: Tuple[str, ...],
              identity: MutationIdentity) -> MutationRun:
        name = target.name
        catalogue = request.catalogue.kind
        if report.timed_out:
            wall = settings["wall_clock_limit"].value
            return MutationRun(
                target_file=name, mutants=(), summary=_summarize([]),
                requested_catalogue=catalogue, requested_operators=operators,
                incomplete={"reason": "wall_clock_limit_before_report",
                            "untested": None, "total": None},
                outcome=Outcome.TIMED_OUT,
                reason=(f"the wall-clock limit of {wall} s ended PIT before "
                        f"it wrote its report; no mutant has a result"),
                identity=identity, settings=settings)
        if report.returncode != 0:
            red = _pit.RED_SUITE in report.output
            stopping = (_placed(_pit.red_tests(report.output), classes,
                                Path(request.suite.root).resolve())[0] if red else ())
            return MutationRun.failed(
                name, Outcome.TEST_FAILURES if red
                else Outcome.INFRASTRUCTURE_FAILURE,
                ("the suite fails without a mutant: PIT measures a green "
                 "suite" if red else
                 f"PIT exited {report.returncode}: "
                 + report.output.strip()[-1500:]),
                failure=FAILED_INITIALIZATION, identity=identity,
                settings=settings, stopping_tests=stopping)
        if report.mutations is None and _pit.NO_MUTATIONS in report.output:
            if not _compiled(target, self._main_classes,
                             **({"executor": self.adapter._executor}
                                if isinstance(self.adapter._executor, ContainerExecutor) else {})):
                return MutationRun.failed(
                    name, Outcome.INFRASTRUCTURE_FAILURE,
                    f"the target's class is not among the project's compiled "
                    f"classes ({', '.join(self._main_classes)})",
                    failure=FAILED_INITIALIZATION, identity=identity,
                    settings=settings)
            return MutationRun(
                target_file=name, mutants=(), summary=_summarize([]),
                requested_catalogue=catalogue, requested_operators=operators,
                outcome=Outcome.COMPLETED,
                reason="the catalogue has no mutant for this target",
                identity=identity, settings=settings)
        if report.mutations is None:
            return MutationRun.failed(
                name, Outcome.INFRASTRUCTURE_FAILURE,
                "PIT wrote no report: " + report.output.strip()[-1500:],
                failure=FAILED_SESSION_READ, identity=identity,
                settings=settings)
        relative = identity.observation.target.path
        source = (JavaEnv(self.adapter._executor).read(target)
                  if isinstance(self.adapter._executor, ContainerExecutor)
                  else target.read_text(encoding="utf-8", errors="replace")).splitlines()
        suite_root = Path(request.suite.root).resolve()
        mutants: List[Mutant] = []
        records = _pit.read(report.mutations)
        for record, occurrence in zip(records, _pit.occurrences(records)):
            tests, killers, unmatched = _placed(record.killers, classes,
                                                suite_root)
            classification = _pit.classify(record.status, killers)
            line = (source[record.line - 1].strip()
                    if 0 < record.line <= len(source) else "")
            # The source line first, so a reader that keeps only the head
            # sees it before a long method descriptor.
            diff = (f"{relative}:{record.line}: {line}".rstrip() + "\n"
                    f"    {record.description} "
                    f"({record.mutator.rsplit('.', 1)[-1]} in "
                    f"{record.method}{record.description_of_method})")
            row: Dict[str, Any] = {
                "id": record.engine_id, "mutant_id": record.engine_id,
                "line": record.line,
                "operator": record.mutator, "status": classification.status,
                "native_status": record.status,
                "killer": killers[0] if killers else None,
                "killers": list(killers), "diff": diff,
                "tests_run": record.tests_run}
            if record.status == "KILLED" and not killers:
                row["unresolved"] = True
            if not record.killers_complete:
                # Every killer was asked for, but only the first is known
                # (_pit.with_every_killer).
                row["killers_incomplete"] = True
            mutants.append(Mutant(
                key=mutant_key(relative, record.mutator, occurrence,
                               record.line, None, diff),
                engine_id=record.engine_id, test_outcome=record.status,
                worker_outcome=None, classification=classification,
                killers=killers, diff=diff, row=row, killing_tests=tests,
                unmatched_killers=unmatched))
        summary = _summarize([dict(m.row) for m in mutants])
        if settings["fail_on_unresolved"].value and summary["unresolved"]:
            return MutationRun.failed(
                name, Outcome.INFRASTRUCTURE_FAILURE,
                f"{summary['unresolved']} kill(s) name no killing test",
                failure=FAILED_INSTRUMENT_CHECK, identity=identity,
                settings=settings)
        stamp = None
        if summary["untested"]:
            stamp = {"reason": UNTESTED_MUTANTS_IN_CATALOGUE,
                     "untested": summary["untested"],
                     "total": summary["total"]}
        tested = [m for m in mutants
                  if m.classification.mutation_class is not MutationClass.NOT_RUN]
        reason = ("the catalogue has no mutant for this target" if not mutants
                  else "no mutant was tested" if not tested else report.note)
        return MutationRun(
            target_file=name, mutants=tuple(mutants), summary=summary,
            requested_catalogue=catalogue, requested_operators=operators,
            incomplete=stamp, outcome=Outcome.COMPLETED, reason=reason,
            identity=identity, settings=settings)

    @staticmethod
    def _mutators(request: MutationRequest) -> List[str]:
        """The ``--mutators`` names for the catalogue, a reported id
        translated, each checked against PIT's pinned registry
        (``_pit.MUTATORS``, ``_pit.GROUPS``) before the run starts: a name
        PIT does not know refuses the request and leaves the session open."""
        catalogue = request.catalogue
        if catalogue.kind == "named":
            given = [catalogue.name]
        elif catalogue.kind == "operators":
            given = list(catalogue.operators)
        else:
            return []
        named = {identifier: name for name, identifier in _pit.MUTATORS.items()}
        names = [named.get(item, item) for item in given]
        unknown = [item for item, name in zip(given, names)
                   if name not in _pit.MUTATORS and name not in _pit.GROUPS]
        if unknown:
            raise InvalidRequest("catalogue", f"PIT {_pit.VERSION} knows no mutator or "
                                 f"group {', '.join(unknown)}")
        return names

    def _effective_settings(
        self, request: MutationRequest,
    ) -> Dict[str, EffectiveSetting]:
        if request.timeout_journal:
            raise InvalidRequest(
                "timeout_journal", "PIT keeps no per-test journal. It bounds "
                "each test of a mutant's run")

        # PIT's rule: a caller's rule gives its factor and, as the constant
        # (the setting's value), its minimum. PIT has no cap.
        timeout = request.per_mutant_timeout
        if isinstance(timeout, TimeoutRule):
            if timeout.cap_s is not None:
                raise InvalidRequest(
                    "per_mutant_timeout.cap_s", "PIT caps no per-mutant timeout: "
                    "its timeout is the factor times each test's time plus the minimum")
            per_mutant = EffectiveSetting(int(timeout.minimum_s), FROM_REQUEST, {
                "rule": "pit", "factor": float(timeout.factor),
                "constant_s": int(timeout.minimum_s)})
        elif timeout is not None:
            per_mutant = EffectiveSetting(int(timeout), FROM_REQUEST, {
                "rule": "fixed", "factor": 0.0, "constant_s": int(timeout)})
        else:
            per_mutant = EffectiveSetting(PIT_TIMEOUT_CONSTANT_S, FROM_DEFAULT, {
                "rule": "pit", "factor": PIT_TIMEOUT_FACTOR,
                "constant_s": PIT_TIMEOUT_CONSTANT_S})
        workers = _chosen(request.workers, self.workers_requested)
        unit_size = EffectiveSetting(
            pit_unit_size(workers.value), FROM_DEFAULT,
            {"rule": "workers", "workers": workers.value, "size": PIT_UNIT_SIZE})
        return _common_settings(request, request.catalogue.operators or (), per_mutant, workers,
                                False, after_workers={"mutation_unit_size": unit_size})


def _compiled(target: PurePath, class_dirs: Sequence[str], *,
              executor: Optional[Executor] = None) -> bool:
    """Whether the target's class file lies in one of ``class_dirs``."""
    if isinstance(executor, ContainerExecutor):
        relative = _jacoco.class_name(target, executor=executor).replace(".", "/") + ".class"
        files = JavaEnv(executor)
        return any(files.is_file(PurePosixPath(d) / relative) for d in class_dirs)
    relative = _jacoco.class_name(target).replace(".", "/") + ".class"
    return any((Path(d) / relative).is_file() for d in class_dirs)


def _placed(names: Sequence[str], classes: Dict[str, Path],
            suite_root: Path) -> Tuple[Tuple[TestId, ...], Tuple[str, ...],
                                       Tuple[str, ...]]:
    """PIT's killers as tests of the suite, their texts (a test's canonical
    text, else PIT's name), and the killers that name no test of it."""
    tests: List[TestId] = []
    texts: List[str] = []
    unmatched: List[str] = []
    for name in names:
        parsed = _junit.test_name(name)
        test = (_junit.test_id(parsed, classes, suite_root)
                if parsed is not None else None)
        if test is None:
            texts.append(name)
            unmatched.append(name)
            continue
        tests.append(test)
        texts.append(test.text)
    return tuple(tests), tuple(texts), tuple(unmatched)


def open_session(adapter: Any, engine: str, **options: Any) -> MutationSession:
    """A mutation session of ``engine`` for ``adapter``'s project."""
    _require_engine(engine, ENGINE, "Java")
    unknown = sorted(set(options) - {"workers"})
    if unknown:
        raise InvalidRequest(
            "options", f"PIT sessions take workers only, not "
                       f"{', '.join(unknown)}")
    return MutationSession(adapter, **options)


def dotted_id(text: str) -> Optional[str]:
    """The dotted id of a test named ``class#method`` (a ``TestId``'s text
    or a killer): nested classes after dots, without parameters or
    invocation index, so ``demo.CalcTest$Clamping#keepsInside()[2]`` gives
    ``demo.CalcTest.Clamping.keepsInside``. None if no method is named."""
    cls, sep, method = str(text).partition("#")
    if not sep or not cls or not method:
        return None
    name = method.split("(", 1)[0].split("[", 1)[0]
    return f"{cls.replace('$', '.')}.{name}" if name else None


def test_dotted_id(test: TestId) -> str:
    """The dotted id of a suite's test, from its text when it names its
    class, else from its file and qualified name."""
    found = dotted_id(test.text) if test.text else None
    if found is not None:
        return found
    package = test.file.rsplit("/", 1)[0].replace("/", ".") if "/" in test.file else ""
    return f"{package}.{test.qualname}" if package else test.qualname
