"""Suite runs of the Python adapter (pytest in the project interpreter, under
coverage.py when a coverage target is named), per-test evidence, benchmarks
and runs as delivered.

A run executes the suite's files as they are and writes nothing into the
project or the suite's directories: Python compiles into a cache directory of
the run's own, unless a prepared copy's bytecode store seeds the copy, pytest's
cache is off, report and coverage data go to the adapter's temporary directory, and the
configuration is the context's (the project's file with ``-c``, else pytest's
defaults), with the suite root as root directory and setup-discovery limit. The
result reports the side effects a request asks for and the determinism
instrument a setup file carries: the isolating
configuration of a suite staged outside the project, the removal of files
earlier runs created, and the timeout journal. In a container context the
project's files are checked and identified inside the container, and the
suite lies in the work directory it shares with this host. Outcomes come from
pytest's ``xunit1`` JUnit report. A test is identified by its file relative to
the suite root. A node id, printed relative to the directory pytest ran in, is
matched to the suite's files by its trailing components.

Per-test evidence is one branch-coverage run, after a discarded warm-up
unless turned off, in which a plugin gives each test its own coverage context
for setup, call and teardown: every line a test causes is its own, import and
collection are left out, and a wider-scoped fixture belongs to the first and
the last test using it, which set it up and tear it down. A shield plugin
keeps coverage.py's bookkeeping on the real built-ins. A test function merges
its cases' items and sums their times, which the evidence-times plugin
records for setup, call and teardown. A benchmark times pytest repetitions from outside the test process,
warm-ups discarded (they leave the bytecode cache), optionally under the
process's benchmark lock (``serialize``) and on a processor set (``taskset``).
A run as delivered runs each test file alone with its import setup, without
the instrument.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import posixpath
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import (
    Any, Dict, FrozenSet, List, Optional, Sequence, Tuple, Set, Mapping, NamedTuple,
)
from langadapter._execution_scope import reraise_unresolved
from langadapter.sources import bounded, whole
from langadapter.errors import InvalidRequest
from langadapter.execution import (
    _retain_scratch, container_path, execution_identity, rebase_path,
)
from langadapter.outcomes import (
    FileIdentity, InstrumentIdentity, ObservationIdentity, Outcome,
)
from langadapter.python._mutation import parse_journal
from langadapter.python._plugins import (
    _DELIVERED_PLUGIN, JOURNAL_PLUGIN, LEAK_OBSERVER_PLUGIN, RECORD_PLUGIN, RUN_RECORD_MARK,
    journal_plugin_text, leak_observer_text, run_record_text, _delivered_setup,
    COPY_ORIGIN_PLUGIN, ORIGIN_PLUGIN, SOURCE_DIRS, copy_origin_plugin_text,
    origin_environment, origin_events, origin_plugin_text, origin_refusal, read_origin,
    _PATHS_PLUGIN, _PATHS_PLUGIN_TEXT, _WHERE_IMPORTED, instrument_identity,
    PROFILE_PLUGIN, node_parts, profile_plugin_text, test_of_nodeid,
    TEMPDIR_PREFIX, SHIELD_MODULE, DELIVERED_MODULE, render,
)
from langadapter.python._project_files import (
    ProjectPath, file_identity, import_root_name, in_container, project_file, project_root,
    remove_created_storage, storage_exists,
)
from langadapter._prepared_copy import (
    _path_spellings, _rewrite_paths, require_cleanup_prepared_copy,
)
from langadapter.python._run_definition import DISABLED_PLUGINS, pytest_arguments, pytest_command
from langadapter.python._output import (
    UNSOUND_TEST_PATTERNS, _compact_pytest_output, _pytest_failure_kind,
    collection_diagnostic, collection_outcome, recorded_unsound_tests,
    unsound_test_warnings, without_report_line,
)
from langadapter.python.project import (
    _PytestMissing, _INSTRUMENT_MARKER, PytestOptions, loaded_setup_files,
)
from langadapter.runs import (
    ERROR, FAILED, PASSED, SKIPPED, XFAILED, CollectionRequest, CollectionRun,
    CoverageObservation, Leak, RunCounts, RunDiagnostic, SideEffects, SuiteRun,
    SuiteRunRequest, TestOutcome, with_detail, KILLS_NOT_REQUESTED, KILLS_UNUSABLE,
    KILLS_USABLE, ContextEvidence, PerTestEvidence, PerTestEvidenceRequest,
    TestEvidence, items_outside_tests, Benchmark, BenchmarkRequest, _report_progress,
    ProfiledFunction, SuiteProfile, SuiteProfileRequest,
)
from langadapter.suites import (
    BranchItem, TestId, CrashItem, EvidenceItem, KillItem, parse_token, Suite,
)
import shutil
from langadapter import placement
from dataclasses import replace
from langadapter.python._sources import _parent_dir


def carried_instrument(setup_files: List[Path],
                       ) -> Optional[InstrumentIdentity]:
    """The instrument the first of ``setup_files`` that carries one carries,
    which runs whether or not a request asks for it."""
    for path in setup_files:
        identity = instrument_identity(
            path.read_text(encoding="utf-8", errors="replace"))
        if identity is not None:
            return identity
    return None


def failing_tests(report: Path, files: Sequence[str] = ()) -> Tuple[TestId, ...]:
    """The tests a pytest JUnit report (xunit1) names failed or in error, in
    its order, as test ids relative to the suite's directory (the run's
    rootdir): ``files`` are the suite's test files there, else the files the
    report names. An entry naming no test (a collection error) is left out,
    and an unreadable report names none."""
    try:
        cases = list(ET.parse(str(report)).getroot().iter("testcase"))
    except (OSError, ET.ParseError):
        return ()
    names = dict.fromkeys(files) or dict.fromkeys(
        (case.get("file") or "").replace("\\", "/") for case in cases if case.get("file"))
    found: List[TestId] = []
    for case in cases:
        if not case.get("classname") or not {child.tag for child in case} & {"failure", "error"}:
            continue
        test = _SuiteRunner._test_id((case.get("file") or "").replace("\\", "/"),
                                     case.get("classname") or "", case.get("name") or "", names)
        if test is not None and test not in found:
            found.append(test)
    return tuple(found)


# Coverage and eviction readers of the project's runs.


def _coverage_run_command(adapter, target_file: str, data: str,
                          rc_file: Path, pytest_args: List[str]) -> List[str]:
    """``coverage run`` of pytest with ``pytest_args``, tracing
    ``target_file`` alone into ``data`` under ``rc_file`` (beside
    ``--source`` coverage.py would ignore ``--include`` and trace the whole
    directory). A target never imported is added at report time."""
    return [
        adapter.interpreter, *adapter._runtime_flags(),
        "-m", "coverage", "run", f"--rcfile={rc_file}",
        f"--include={target_file}",
        f"--data-file={data}",
        "-m", "pytest", *pytest_args,
    ]


#: coverage.py's ``json`` command over a run's data (the arguments after
#: the data file and the target), in the project interpreter. A target the
#: run never imported is first added as measured with nothing executed, as
#: ``--source`` adds its directories' unexecuted files, so it reports 0 %.
_COVERAGE_JSON = (
    "import os, sys\n"
    "from coverage import CoverageData\n"
    "from coverage.cmdline import main\n"
    "from coverage.files import canonical_filename\n"
    "df, tf = sys.argv[1], sys.argv[2]\n"
    "d = CoverageData(basename=df)\n"
    "d.read()\n"
    "want = os.path.normcase(os.path.realpath(tf))\n"
    "if os.path.isfile(tf) and not any(\n"
    "        os.path.normcase(os.path.realpath(f)) == want for f in d.measured_files()):\n"
    "    if not (d.has_arcs() or d.has_lines()):\n"
    "        d.add_arcs({})\n"
    "    d.touch_file(canonical_filename(tf))\n"
    "sys.exit(main(['json', '--data-file=' + df] + sys.argv[3:]))\n"
)


def _coverage_json_command(adapter, data: str, report: str, target_file: str,
                           *, include: bool = True) -> List[str]:
    """The command that writes coverage.py's JSON report of ``data`` to
    ``report``, for ``target_file`` alone with ``include``."""
    return [adapter.interpreter, *adapter._runtime_flags(), "-c", _COVERAGE_JSON,
            data, target_file,
            "-o", report, *([f"--include={target_file}"] if include else [])]


def _coverage_json(adapter, data: str, report: str, target_file: str,
                   env: Dict[str, str], project_root: str) -> Dict[str, Any]:
    """coverage.py's JSON report of ``data`` for ``target_file``,
    written to ``report`` and read back. Raises when the tool writes no
    readable report."""
    adapter._executor().run(
        _coverage_json_command(adapter, data, report, target_file),
        cwd=project_root, env=env, timeout=60,
    )
    return json.loads(Path(report).read_text(encoding="utf-8"))


def _covered_statements(adapter, data_file: str, target_file: str,
                        source_parent: str,
                        env: Optional[Dict[str, str]] = None,
                        cwd: Optional[str] = None) -> dict:
    """coverage.py's covered statements (statements minus missing, not
    ``executed_lines``), statement total and traced lines (every traced
    line at its statement's first line, excluded lines included) for
    ``target_file``, read with
    the run's settings (branch, source) so they equal its report. The file
    is matched by resolved path, never by name. ``cwd`` must be the project
    root: coverage reads its excludes from the working directory, which
    apptainer takes from the host. Data not measuring an existing target
    gives no covered line and the file's statement count. Raises
    ``MeasurementDataUnreadable`` when the read or the analysis fails."""
    snippet = (
        "import sys, os, json\n"
        "from coverage import Coverage, CoverageData\n"
        "df, tf, parent = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "if not os.path.isfile(df):\n"
        "    sys.exit('no coverage data file ' + df)\n"
        "d = CoverageData(basename=df); d.read()\n"
        "want = os.path.normcase(os.path.realpath(tf))\n"
        # The exact target path, else the file that is the target once
        # links, case and separators are resolved, in sorted order (a set
        # has none). A file of the same name elsewhere is never the target.
        "mfs = sorted(d.measured_files())\n"
        "mf = (next((f for f in mfs if f == tf), None) "
        "or next((f for f in mfs if os.path.normcase(os.path.realpath(f)) == want), tf))\n"
        "c = Coverage(data_file=df, branch=True, source=[parent]); c.load()\n"
        "_, stmts, _, miss, _ = c.analysis2(mf)\n"
        "cov = sorted(set(int(x) for x in stmts) - set(int(x) for x in miss))\n"
        # The executed lines of coverage 7.6.8's reports. From 7.13.1 on
        # those hold statements only.
        "from coverage.python import PythonFileReporter\n"
        "traced = sorted(PythonFileReporter(mf, c).translate_lines(d.lines(mf) or []))\n"
        "print(json.dumps({'covered_lines': cov, "
        "'num_statements': len(stmts), 'traced_lines': traced}))\n"
    )
    return _read_in_project(adapter,
        [adapter.interpreter, *adapter._runtime_flags(), "-c", snippet, data_file,
         target_file, source_parent],
        "the target's statements in the coverage data",
        env=env, timeout=120, cwd=cwd)


def _read_in_project(adapter, argv: List[str], what: str, **options: Any) -> Any:
    """The JSON value a reader of a run's data prints last, run by the
    executor (the project interpreter reads what its tools wrote). Raises
    ``MeasurementDataUnreadable`` naming ``what`` when the reader cannot
    run, fails or prints no JSON: an unread measurement is no empty data."""
    try:
        done = adapter._executor().run(argv, **options)
    except Exception as exc:  # the reader did not run
        reraise_unresolved(exc)
        raise MeasurementDataUnreadable(f"{what} could not be read: {exc}") from exc
    lines = (done.stdout or "").strip().splitlines()
    if done.returncode != 0 or not lines:
        detail = (done.stderr or "").strip().splitlines()
        raise MeasurementDataUnreadable(
            f"{what} could not be read (exit {done.returncode})"
            + (f": {detail[-1]}" if detail else ""))
    try:
        return json.loads(lines[-1])
    except ValueError as exc:
        raise MeasurementDataUnreadable(
            f"{what} could not be read: {lines[-1][:200]}") from exc


#: Reads the per-test contexts of a coverage data file in the project
#: interpreter, whose coverage wrote it (``_eviction_tokens_from_coverage``).
_EVICTION_TOKENS_READER = (
    "import json, os, sys\n"
    "from coverage import CoverageData\n"
    "df, tf = sys.argv[1], sys.argv[2]\n"
    "if not os.path.isfile(df):\n"
    "    sys.exit('no coverage data file ' + df)\n"
    "d = CoverageData(basename=df)\n"
    "d.read()\n"
    "want = os.path.basename(tf)\n"
    # The target's file by path (links, case and separators resolved),
    # never another of the same name. The tokens keep its name as label.
    "real = os.path.normcase(os.path.realpath(tf))\n"
    "mfs = sorted(d.measured_files())\n"
    "mf = (next((f for f in mfs if f == tf), None) "
    "or next((f for f in mfs if os.path.normcase(os.path.realpath(f)) == real), None))\n"
    "out = {}\n"
    "if mf is not None:\n"
    "    arcs = d.has_arcs()\n"
    "    for ctx in sorted(d.measured_contexts()):\n"
    "        if not ctx:\n"
    "            continue\n"
    "        d.set_query_context(ctx)\n"
    "        toks = {'L:%s:%d' % (want, n) for n in (d.lines(mf) or [])}\n"
    "        if arcs:\n"
    "            toks |= {'B:%s:%d->%d' % (want, a, b)\n"
    "                     for (a, b) in (d.arcs(mf) or [])}\n"
    "        if toks:\n"
    "            out[ctx] = sorted(toks)\n"
    "print(json.dumps(out))\n"
)


def _eviction_tokens_from_coverage(adapter, data_file: str, target_file: str,
) -> Dict[str, FrozenSet[str]]:
    """Per-test token sets for ``target_file`` from branch-mode coverage
    data with a context per test, its node id: ``L:<name>:<line>`` and
    ``B:<name>:<src>-><dst>``, ``<name>`` the target's basename. The empty
    (import-time) context is skipped: any kept test reproduces it. The
    project interpreter, which wrote the file, reads it, so the calling
    process needs no coverage. Empty when the data does not measure the
    target. Raises ``MeasurementDataUnreadable`` when it cannot be read."""
    raw = _read_in_project(adapter,
        [adapter.interpreter, *adapter._runtime_flags(), "-c", _EVICTION_TOKENS_READER,
         data_file, target_file],
        "the per-test coverage", timeout=600)
    return {ctx: frozenset(tokens) for ctx, tokens in raw.items()}


def _eviction_times(times_path: str) -> Dict[str, float]:
    """Per-test time in milliseconds at the clock's resolution, from the
    setup, call and teardown durations the evidence run's plugin recorded,
    by test function (``node_function``): cases are summed, as their
    coverage is. Raises ``MeasurementDataUnreadable`` when the record
    cannot be read or says coverage was not measuring, so no test had a
    context of its own."""
    try:
        record = json.loads(Path(times_path).read_text(encoding="utf-8"))
        durations = record["durations"]
        measured = record["contexts"]
        times: Dict[str, float] = {}
        for nodeid, seconds in durations.items():
            tid = node_function(nodeid)
            times[tid] = times.get(tid, 0.0) + float(seconds) * 1000.0
    except (OSError, ValueError, AttributeError, TypeError, KeyError) as exc:
        raise MeasurementDataUnreadable(
            f"the per-test times could not be read: {exc}") from exc
    if measured is not True:
        raise MeasurementDataUnreadable(
            "the per-test run found no coverage measurement to give each test its "
            "context")
    return times


def _match_context_to_test_id(
    ctx: str, test_ids: "set[str]",
) -> Optional[str]:
    """The JUnit test id (``path.module.qualname``) equal to a test
    function's dotted id (``node_function``), else the only one ending in
    it after a dot, else None (the caller keeps the context id)."""
    from langadapter.python.cosmic_ray import _match_test_context
    return _match_test_context(ctx, test_ids)


def run_suite(adapter: Any, request: SuiteRunRequest, *,
              path_pairs: Sequence[Tuple[str, str]] = ()) -> SuiteRun:
    """Run ``request`` for ``adapter`` (a ``PythonAdapter``). ``path_pairs``,
    (staged, original) spellings, write a suite staged outside the project
    as its original files, ahead of an owned copy's own pairs."""
    return _SuiteRunner(adapter, request, path_pairs=path_pairs).run()


def collect_suite(adapter: Any, request: CollectionRequest) -> CollectionRun:
    """Collect the suite with a run's path, setup, configuration and executor."""
    request.validate()
    runner = _SuiteRunner(adapter, SuiteRunRequest(
        request.suite, request.timeout_s, runner_options=request.runner_options,
        isolation_config=request.isolation_config))
    return runner.collect()


class _SuiteRunner:

    def __init__(self, adapter: Any, request: SuiteRunRequest, *,
                 path_pairs: Sequence[Tuple[str, str]] = ()) -> None:
        request.validate()
        self.path_pairs = tuple(path_pairs)
        options = request.runner_options or PytestOptions()
        if not isinstance(options, PytestOptions):
            raise InvalidRequest(
                "runner_options", "the Python adapter takes PytestOptions")
        options.validate()
        if request.remove_created_files:
            require_cleanup_prepared_copy(adapter.context, adapter.context.root)
        self.adapter = adapter
        self.request = request
        self.options = options
        self.container = in_container(adapter.context)
        self.root = project_root(adapter.context)
        self.suite_root = Path(request.suite.root).resolve()
        self.test_files = [request.suite._resolve(p).resolve()
                           for p in request.suite.test_files]
        self.setup_files = [request.suite._resolve(p).resolve()
                            for p in request.suite.setup_files]
        self._check_setup_files()
        self._check_suite_location()
        self._check_observations()
        self.target = self._project_file("coverage_target",
                                         request.coverage_target)
        self.module = self._project_file(
            "module_under_test", request.module_under_test) or self.target
        self.written: List[str] = []
        self.removed: Tuple[str, ...] = ()
        self.instrument: Optional[InstrumentIdentity] = None
        self.source: Optional[str] = None
        self.journal: Optional[Path] = None
        self.record: Optional[Path] = None
        self.leak_record = False
        self.timeout_journal: Optional[Path] = None
        self.timeout_journal_taken = False
        self._target_file: Optional[FileIdentity] = None
        #: Where the run records the files it imported the copy's modules
        #: from, when it runs in an owned copy that holds the module under
        #: test or names none.
        self.origin: Optional[Path] = None
        #: Whether the owned copy the run is in holds the module under test.
        self.holds_module = False
        #: The seconds of the coverage report steps (``SuiteRun``).
        self.coverage_report_s: Optional[float] = None
        self.coverage_statements_s: Optional[float] = None
        #: Whether pytest-benchmark's storage was in the working directory,
        #: the project root, when the run started (``storage_exists``), None
        #: before then and after the run's end.
        self.storage_existed: Optional[bool] = None

    # The request

    def _project_file(self, field: str,
                      path: Optional[str]) -> Optional[ProjectPath]:
        if path is None:
            return None
        return project_file(self.adapter, field, path)

    def _check_suite_location(self) -> None:
        """In a container context the suite lies in the work directory, the
        directory the container shares with this host."""
        if not self.container:
            return
        work_dir = Path(self.adapter.context.execution.work_dir).resolve()
        if self.suite_root != work_dir and work_dir not in self.suite_root.parents:
            raise InvalidRequest(
                "suite.root", f"{self.suite_root} does not lie in the work "
                f"directory {work_dir}, which the container shares")

    def _check_setup_files(self) -> None:
        """Require the setup files to be exactly the ``conftest.py`` files
        pytest loads for the test files (in the suite root or a directory between
        it and a test file), so the identity names every file that took part."""
        loaded = set(loaded_setup_files(self.suite_root, self.test_files))
        listed = set(self.setup_files)
        for path in sorted(listed):
            if path.name != "conftest.py":
                raise InvalidRequest(
                    "suite.setup_files",
                    f"{path.name}: pytest's setup files are conftest.py")
            if path not in loaded:
                raise InvalidRequest(
                    "suite.setup_files",
                    f"{self._relative(path)} lies on no test file's path "
                    f"from the suite root. pytest would not load it")
        unlisted = sorted(loaded - listed)
        if unlisted:
            raise InvalidRequest(
                "suite.setup_files",
                f"pytest loads {', '.join(self._relative(p) for p in unlisted)}"
                f" for the suite's test files. List it among the setup files")

    def _check_observations(self) -> None:
        """A run as delivered runs without the instrument, so no setup file
        of the suite may carry it."""
        carried = self._suite_instrument()[0] is not None
        if self.request.session_seed is not None and carried:
            raise InvalidRequest(
                "session_seed", "a setup file of the suite carries the "
                "instrument, so the suite cannot run as delivered")

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.suite_root).as_posix()

    # The run

    def _prepare(self, scratch: Path, *, report: bool = True, record: bool = False,
                 ) -> Tuple[Dict[str, str], List[str], Path]:
        """Apply the requested side effects and return the environment, the
        pytest arguments and the report's path of a run in ``scratch``. With
        ``report`` false the run writes no report. With ``record`` it writes
        the run record (``RECORD_PLUGIN``) to ``self.record``, which only
        observes. A timed or evidence run leaves it out."""
        if self.request.remove_created_files:
            self.removed = tuple(self.adapter._created_files.begin(str(self.root)))
        self.storage_existed = storage_exists(self.adapter)
        self.instrument, self.source = self._suite_instrument()
        env = self.adapter._build_env(str(self.root))
        # A temporary directory of the run's own, as each mutant's run has.
        run_tmp = scratch / "tmp"
        run_tmp.mkdir(exist_ok=True)
        env["TMPDIR"] = str(run_tmp)
        # A bytecode cache of the run's own: CPython and pytest reuse a cached
        # module whose recorded size and modification time match its source,
        # so a same-size rewrite within one second would run the old code. A
        # prepared copy with a bytecode store keeps reading its seeds, which
        # the store checks against the sources.
        binding = self.adapter.context._prepared_copy_binding
        if binding is None or not binding.seeds_bytecode:
            env["PYTHONPYCACHEPREFIX"] = str(scratch / "pycache")
        if self.adapter.context._prepared_copy_binding is not None:
            # Generated setup already honors this root selector for native
            # mutation clones. Keep its bytes while selecting this owned copy.
            env[self.adapter._env_name("MUTATION_SOURCE_ROOT")] = str(self.root)
            self.holds_module = self._import_from_the_copy(env)
        # A carried instrument restores the module under test before each
        # test, and the leak record compares it, when they know the module.
        # A parent's value never leaks in.
        name = self.adapter._env_name("TARGET_MODULE_FILE")
        if ((self.instrument is not None or self.request.record_leaks)
                and self.module is not None):
            env[name] = str(self.module)
        else:
            env.pop(name, None)
        args = self._pytest_args(scratch)
        # Write the journal only on request, never into a directory a parent names.
        evidence = self.adapter._env_name("TIMEOUT_EVIDENCE_DIR")
        env.pop(evidence, None)
        env.pop(self.adapter._env_name("JOURNAL_MODULE"), None)
        if self.request.timeout_journal:
            self.timeout_journal = scratch / "journal"
            env[evidence] = str(self.timeout_journal)
            if self.instrument is None:
                journal_dir = scratch / "journal_plugin"
                journal_dir.mkdir()
                (journal_dir / f"{JOURNAL_PLUGIN}.py").write_text(
                    journal_plugin_text(), encoding="utf-8")
                self.adapter._prepend_pythonpath(env, str(journal_dir))
                args += ["-p", JOURNAL_PLUGIN]
        if self.request.record_leaks:
            self.journal = scratch / "leaks.jsonl"
            env[self.adapter._env_name("LEAK_JOURNAL")] = str(self.journal)
            if self.instrument is None:
                # Without the instrument the read-only observer takes the
                # record, from the command line, beside nothing of the suite.
                observer_dir = scratch / "leak_plugin"
                observer_dir.mkdir()
                (observer_dir / f"{LEAK_OBSERVER_PLUGIN}.py").write_text(
                    leak_observer_text(), encoding="utf-8")
                self.adapter._prepend_pythonpath(env, str(observer_dir))
                args += ["-p", LEAK_OBSERVER_PLUGIN]
        # pytest's own account of the run, which its summary counts
        # (``runner_counts``) come from. A parent's value never leaks in.
        self.record = scratch / "record.json" if record else None
        name = self.adapter._env_name("RUN_RECORD")
        env.pop(name, None)
        if self.record is not None:
            env[name] = str(self.record)
            record_dir = scratch / "record_plugin"
            record_dir.mkdir()
            (record_dir / f"{RECORD_PLUGIN}.py").write_text(
                run_record_text(UNSOUND_TEST_PATTERNS),
                encoding="utf-8")
            self.adapter._prepend_pythonpath(env, str(record_dir))
            args += ["-p", RECORD_PLUGIN]
        seed, reverse = self.request.session_seed, self.request.reverse_order
        if seed is not None or reverse:
            delivered_dir = scratch / "delivered_plugin"
            delivered_dir.mkdir()
            (delivered_dir / f"{DELIVERED_MODULE}.py").write_text(
                render(_DELIVERED_PLUGIN), encoding="utf-8")
            self.adapter._prepend_pythonpath(env, str(delivered_dir))
            args += ["-p", DELIVERED_MODULE]
            if seed is not None:
                env["PYTHONHASHSEED"] = str(seed)
                env[self.adapter._env_name("DELIVERED_SEED")] = str(seed)
            if reverse:
                env[self.adapter._env_name("DELIVERED_ORDER")] = "reverse"
        report_path = scratch / "report.xml"
        if report:
            args += [f"--junit-xml={report_path}", "-o",
                     "junit_family=xunit1"]
            binding = self.adapter.context._prepared_copy_binding
            if binding is not None or self.path_pairs:
                # An owned copy's or staged suite's paths print as the originals.
                pairs, cwd = list(self.path_pairs), str(self.root)
                if binding is not None:
                    pairs += list(binding.prepared_copy.text_map())
                    cwd = binding.prepared_copy.original_root
                paths_dir = scratch / "paths_plugin"
                paths_dir.mkdir()
                (paths_dir / f"{_PATHS_PLUGIN}.py").write_text(
                    render(_PATHS_PLUGIN_TEXT), encoding="utf-8")
                self.adapter._prepend_pythonpath(env, str(paths_dir))
                env[self.adapter._env_name("ORIGINAL_PATHS")] = json.dumps(
                    {"cwd": cwd, "pairs": pairs})
                args += ["-p", _PATHS_PLUGIN]
            args += self._origin_plugin(scratch, env)
        return env, args, report_path

    def _source_dirs_first(self, env: Dict[str, str]) -> None:
        """Put the copy's ``src``, ``lib`` and ``python`` directories that hold a
        package first on the import path, ahead of the root, so the tests
        execute the copy's files where an editable install or an installed
        copy of the project would provide the original's. A context that
        names its import root (``python_import_root``) has it first already."""
        if import_root_name(self.adapter.context):
            return
        host = Path(self.adapter.context.root)
        # Each goes first, so the last, src, ends up ahead of the others.
        for name in reversed(SOURCE_DIRS):
            if any((host / name).glob("*/__init__.py")):
                self.adapter._prepend_pythonpath(
                    env, posixpath.join(str(self.root), name) if self.container else str(host / name))

    def _origin_plugin(self, scratch: Path, env: Dict[str, str]) -> List[str]:
        """Load the plugin that records where the tests imported the copy's
        modules from into ``env``, and return its pytest arguments: the
        module's plugin when the owned copy holds the module under test, the
        copy's when the run names no module, else none."""
        record = scratch / "origin.json"
        if self.holds_module:
            name, text = ORIGIN_PLUGIN, origin_plugin_text()
            variables = origin_environment(str(self.module), str(self.root), str(record))
        elif self.module is None and self.adapter.context._prepared_copy_binding is not None:
            name, text = COPY_ORIGIN_PLUGIN, copy_origin_plugin_text()
            # The suite's directory holds test data, unless it is or holds one of
            # the copy's import roots: its root and its source directories.
            root = Path(str(self.root)).resolve()
            holds = any(import_root_dir == self.suite_root or self.suite_root in import_root_dir.parents
                        for import_root_dir in (root, *(root / name for name in SOURCE_DIRS)))
            suite = () if holds else (str(self.suite_root),)
            variables = origin_environment("", str(self.root), str(record),
                                           skipped=(*suite, str(scratch)))
        else:
            return []
        origin_dir = scratch / "origin_plugin"
        origin_dir.mkdir()
        (origin_dir / f"{name}.py").write_text(text, encoding="utf-8")
        self.adapter._prepend_pythonpath(env, str(origin_dir))
        self.origin = record
        env.update(variables)
        return ["-p", name]

    def collect(self, targets: Optional[Sequence[str]] = None, *,
                configured: bool = False) -> CollectionRun:
        """Collect the suite's test files, or ``targets`` in their place (a
        directory, whose tests pytest finds with the run's settings), naming
        the files from the run record (``CollectionRun.files``).
        ``configured`` names no file or directory to pytest, which then collects
        what the configuration's ``testpaths`` name, else the working
        directory's tests."""
        files: Tuple[str, ...] = ()

        def result(outcome, reason, output="", diagnostic="", returncode=None, wall=None):
            self._end_run()
            text = self._original_text
            return CollectionRun(outcome, text(reason), self._identity(), text(output),
                                 text(diagnostic), returncode, wall, self._side_effects(),
                                 files=files)

        try:
            with self.adapter._work_dir_tempdir(
                    prefix=f"{TEMPDIR_PREFIX}collect_") as scratch:
                scope = scratch
                scratch = Path(scratch)
                env, args, _ = self._prepare(scratch, report=False, record=True)
                # Resolved, as the test files and the rootdir are: a target
                # spelled through a link (macOS's /var) beside the resolved
                # rootdir sends pytest's walk up through the link.
                named = ([] if configured else
                         [Path(target).resolve() for target in targets] if targets
                         else self.test_files)
                command = pytest_command(
                    self.adapter.interpreter, self.adapter._runtime_flags(),
                    [*map(str, named), "--collect-only"], args)
                start = time.perf_counter()
                completed = self.adapter._executor().run(
                    command, cwd=str(self.root), env=env,
                    timeout=self.request.timeout_s)
                wall = time.perf_counter() - start
                output = (completed.stdout or "") + "\n" + (completed.stderr or "")
                code = completed.returncode
                outcome, reason = collection_outcome(code, output)
                if outcome is Outcome.INFRASTRUCTURE_FAILURE:
                    _retain_scratch(scope, reason or "collection produced no verdict")
                files = self._collected_files()
        except subprocess.TimeoutExpired:
            return result(Outcome.TIMED_OUT,
                          f"collection reached the timeout of {self.request.timeout_s:g} s",
                          diagnostic="collection timed out")
        except (OSError, subprocess.SubprocessError) as exc:
            reraise_unresolved(exc)
            return result(Outcome.INFRASTRUCTURE_FAILURE,
                          f"collection could not execute: {exc}", diagnostic=str(exc))
        diagnostic = "" if code == 0 else bounded(collection_diagnostic(output))
        return result(outcome, reason, whole(output, "collection output"), diagnostic, code,
                      wall)

    def _pytest_missing(self) -> Optional[str]:
        """Why the project's interpreter has no pytest, from its configuration
        probe before a scratch directory exists, or None. The answer is cached,
        so the run's own lookup does not probe again."""
        try:
            self.adapter._pytest_config_path(str(self.root))
        except _PytestMissing as missing:
            return str(missing)
        return None

    def run(self) -> SuiteRun:
        missing = self._pytest_missing()
        if missing is not None:
            return self._result(Outcome.MISSING_SETUP, missing, counts=RunCounts(),
                                tests=(), unmatched=(), wall=None, output="",
                                coverage=None)
        with self.adapter._work_dir_tempdir(
                prefix=f"{TEMPDIR_PREFIX}run_") as scratch:
            scope = scratch
            scratch = Path(scratch)
            env, args, report = self._prepare(scratch, record=True)
            data = str(scratch / "coverage.data")
            if self.target is not None:
                rc_file = scratch / "cov.rc"
                rc_file.write_text(self.adapter.profile().coverage_settings(), encoding="utf-8")
                command = _coverage_run_command(self.adapter,
                    str(self.target), data, rc_file,
                    [*map(str, self.test_files), *args])
            else:
                command = pytest_command(
                    self.adapter.interpreter, self.adapter._runtime_flags(),
                    list(map(str, self.test_files)), args)
            start = time.perf_counter()
            try:
                completed = self.adapter._executor().run(
                    command, cwd=str(self.root), env=env,
                    timeout=self.request.timeout_s)
            except subprocess.TimeoutExpired:
                _retain_scratch(scope, "suite execution timed out")
                tests, unmatched, _, _ = self._read_report(report)
                return self._result(
                    Outcome.TIMED_OUT,
                    f"the runner reached the timeout of "
                    f"{self.request.timeout_s:g} s",
                    counts=RunCounts(), tests=tests or (),
                    unmatched=unmatched or (), wall=None, output="",
                    coverage=None, leaks=self._leaks(),
                    active_at_end=self._active_at_end())
            except (OSError, subprocess.SubprocessError) as exc:
                reraise_unresolved(exc)
                _retain_scratch(scope, f"suite execution failed: {type(exc).__name__}: {exc}")
                return self._result(
                    Outcome.INFRASTRUCTURE_FAILURE,
                    f"the runner could not execute: {exc}", counts=RunCounts(),
                    tests=(), unmatched=(), wall=None, output="", coverage=None)
            wall = time.perf_counter() - start
            # On Linux the supervisor times the test process itself, leaving
            # its own start and stop out. A caller passes this time as
            # passing_suite_s to the per-mutant timeout rule.
            native = getattr(completed, "native_elapsed_s", None)
            if native is not None:
                wall = native
            # The whole output, without the line naming the run's own report file.
            output = whole(without_report_line(
                (completed.stdout or "") + "\n" + (completed.stderr or ""), str(report)),
                "suite output")
            outcome, reason, detail, counts, tests, unmatched = self._classify(
                completed.returncode, report, output)
            coverage = None
            # A run whose collection failed or that had no test would report
            # import-time coverage as the suite's.
            if self.target is not None and outcome in (
                    Outcome.COMPLETED, Outcome.TEST_FAILURES):
                coverage, failure = self._coverage(data, scratch, env)
                if coverage is None:
                    _retain_scratch(scope, f"coverage observation failed: {failure}")
                if coverage is None and outcome is Outcome.COMPLETED:
                    outcome = Outcome.INFRASTRUCTURE_FAILURE
                    reason = (f"every test passed, but the coverage tool "
                              f"failed: {failure}")
            refusal = self._origin_refusal(outcome)
            if refusal is not None:
                # The tests ran another file than the copy's module: their
                # outcomes and coverage measure nothing of the copy.
                outcome, reason, detail = Outcome.INFRASTRUCTURE_FAILURE, refusal, ()
                counts, tests, unmatched, coverage = RunCounts(), (), (), None
            if outcome is Outcome.INFRASTRUCTURE_FAILURE:
                _retain_scratch(scope, reason or "suite observation did not complete")
            return self._result(
                outcome, reason, counts=counts, tests=tests,
                unmatched=unmatched, wall=wall, output=output,
                coverage=coverage, leaks=self._leaks(),
                active_at_end=self._active_at_end(), returncode=completed.returncode,
                diagnostics=self._diagnostics(output, tests), reason_detail=detail)

    def _import_from_the_copy(self, env: Dict[str, str]) -> bool:
        """Put the copy's directory holding the module under test first on the
        import path of a run in an owned copy when another tree would provide
        it, and return whether the copy holds the module. An editable install
        (with a src layout) or an installed copy of the package would make the
        tests import the original while coverage reads the copy, measuring 0.
        An import hook ahead of the path (meson-python's) still wins, and the
        run's import record then refuses the run (``_origin_refusal``)."""
        if self.module is None:
            return False
        try:
            if self.container:
                PurePosixPath(self.module).relative_to(self.root)
            else:
                Path(self.module).relative_to(Path(self.root).resolve())
        except ValueError:
            return False
        found = self._where_imported(env)
        if found is None:
            return True
        if found["verdict"] == "absent" or not self._source_dir(found["root"], found["top"]):
            # Not importable by its own name, or named from higher up (a package
            # in a namespace directory a library may share): prepend the directory
            # above the first one another tree provides.
            shadowed = [path for _, path, verdict in found["candidates"]
                        if verdict not in ("copy", "other", "absent")]
            if shadowed:
                self.adapter._prepend_pythonpath(env, posixpath.dirname(shadowed[0])
                                                if self.container
                                                else os.path.dirname(shadowed[0]))
        elif found["verdict"] != "copy":
            self.adapter._prepend_pythonpath(env, found["root"])
        return True

    def _source_dir(self, parent_dir: str, top: str) -> bool:
        """Whether ``top``, the module or outer package directly in ``parent_dir``,
        is imported by its own name: ``parent_dir`` is the project root or its
        ``src``, ``lib`` or ``python`` directory (as the mutation session's probe
        takes them), or ``top`` is a package in any directory directly in the root.
        A module there may share its name with a library the tests import."""
        if self.container:
            path, root = PurePosixPath(parent_dir), PurePosixPath(self.root)
            package = PurePosixPath(top) != PurePosixPath(self.module)
        else:
            path = Path(os.path.normcase(os.path.realpath(parent_dir)))
            root = Path(os.path.normcase(os.path.realpath(self.root)))
            package = (os.path.normcase(os.path.realpath(top))
                       != os.path.normcase(os.path.realpath(self.module)))
        return path == root or (path.parent == root and (
            package or path.name in SOURCE_DIRS))

    def _origin_refusal(self, outcome: Outcome) -> Optional[str]:
        """Why the run measured another file than the module under test, by
        the record its tests left. A run that measured nothing and left no
        record keeps its own outcome."""
        if self.origin is None:
            return None
        record = read_origin(self.origin)
        if record is None and outcome not in (Outcome.COMPLETED, Outcome.TEST_FAILURES):
            return None
        return origin_refusal(record, collected=outcome is not Outcome.COLLECTION_FAILED,
                              named=self.module is not None)

    def _where_imported(self, env: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """``_WHERE_IMPORTED``'s answer for the module under test, None when
        the probe gives none."""
        command = [self.adapter.interpreter, *self.adapter._runtime_flags(), "-c",
                   _WHERE_IMPORTED, str(self.root), str(self.module)]
        try:
            done = self.adapter._executor().run(command, cwd=str(self.root), env=env,
                                               timeout=60)
            found = json.loads((done.stdout or "").strip().splitlines()[-1])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
            # A launch that may have left a command running is no probe
            # answer: its owner must resolve it.
            reraise_unresolved(exc)
            return None
        if done.returncode != 0 or not isinstance(found, dict) or not {
                "name", "top", "root", "verdict", "candidates"} <= set(found):
            return None
        return found

    def _leaks(self) -> Optional[Tuple[Leak, ...]]:
        """The tests the leak record names, when it was requested and taken."""
        if self.journal is None:
            return None
        records = read_leak_journal(str(self.journal))
        if records is None:
            return None
        self.leak_record = True
        return tuple(
            Leak(test=test_of_nodeid(record.nodeid), added=record.added,
                 rebound=record.rebound, removed=record.removed,
                 xarray_options=record.xarray_options,
                 numpy_errors=record.numpy_errors,
                 numpy_print=record.numpy_print)
            for record in records)

    def _active_at_end(self) -> Optional[TestId]:
        """The test the requested timeout journal shows running when the run
        ended before it finished. Notes whether the journal was taken."""
        if self.timeout_journal is None:
            return None
        try:
            text = (self.timeout_journal / "journal.jsonl").read_text(
                encoding="utf-8", errors="replace")
        except OSError:
            return None
        blocks = parse_journal(text)
        if not blocks:
            return None
        self.timeout_journal_taken = True
        block = blocks[-1]
        if block["ended"] or not block["hung"]:
            return None
        return test_of_nodeid(block["hung"])

    def _suite_instrument(self) -> Tuple[Optional[InstrumentIdentity],
                                         Optional[str]]:
        """The instrument a listed setup file carries, which runs whether
        or not the request asks for it."""
        identity = carried_instrument(self.setup_files)
        return (identity, "suite") if identity is not None else (None, None)

    def _pytest_args(self, scratch: Path) -> List[str]:
        options = self.options
        suite_root = str(self.suite_root)
        if self.request.isolation_config:
            # As a suite staged outside the project runs: an anchoring
            # pytest.ini, and any project configuration copied beside it (-c).
            before = self._snapshot()
            config = self.adapter._suite_config(str(self.root), suite_root)
            self.written += self._changed(before)
        else:
            config = self.adapter._pytest_config_path(str(self.root))
            if config is None:
                config = str(scratch / "pytest.ini")
                Path(config).write_text("[pytest]\n", encoding="utf-8")
        args = pytest_arguments(config, suite_root, traceback=options.traceback,
                                stop_at_first_failure=options.stop_at_first_failure,
                                disabled_plugins=options.disabled_plugins)
        if options.leave_out_timing_plugins:
            # After the project's addopts, so the last -n wins and --no-cov
            # follows the project's --cov.
            args += self.adapter._timing_plugin_flags(str(self.root), options.disabled_plugins)
        return args

    def _snapshot(self) -> Dict[str, Tuple[int, int]]:
        state = {}
        for path in self.suite_root.iterdir():
            if path.is_file():
                stat = path.stat()
                state[path.name] = (stat.st_size, stat.st_mtime_ns)
        return state

    def _changed(self, before: Dict[str, Tuple[int, int]]) -> List[str]:
        after = self._snapshot()
        return sorted(name for name, state in after.items()
                      if before.get(name) != state)

    # The report

    def _classify(self, returncode: int, report: Path, output: str):
        tests, unmatched, collection_errors, unmatched_failures = (
            self._read_report(report))
        if tests is None:
            return (Outcome.INFRASTRUCTURE_FAILURE,
                    *self._detailed(f"the runner's report {report.name} could not be "
                                    f"read (exit status {returncode})", output),
                    RunCounts(), (), ())
        counts = RunCounts(
            collected=len(tests),
            passed=sum(t.outcome == PASSED for t in tests),
            failed=sum(t.outcome == FAILED for t in tests),
            errors=sum(t.outcome == ERROR for t in tests),
            skipped=sum(t.outcome in (SKIPPED, XFAILED) for t in tests))
        if collection_errors:
            first_file, first_message = collection_errors[0]
            return (Outcome.COLLECTION_FAILED,
                    f"collecting {first_file} failed: {first_message}", (),
                    counts, tests, unmatched)
        if returncode == 5 or (returncode == 0 and not tests and not unmatched):
            return (Outcome.NO_TESTS, "the runner collected no test", (),
                    counts, tests, unmatched)
        if returncode == 1 and (counts.failed or counts.errors or unmatched_failures):
            unmapped = (f"; {unmatched_failures} of them not mapped to a "
                        f"test of the suite" if unmatched_failures else "")
            return (Outcome.TEST_FAILURES,
                    f"{counts.failed + counts.errors + unmatched_failures} "
                    f"test(s) failed or raised an error{unmapped}", (),
                    counts, tests, unmatched)
        if returncode != 0:
            return (Outcome.INFRASTRUCTURE_FAILURE,
                    *self._detailed(f"the runner exited with status {returncode}", output),
                    counts, tests, unmatched)
        return Outcome.COMPLETED, "", (), counts, tests, unmatched

    def _diagnostics(self, output, tests):
        """The run's ``unsound_test`` entries: each warning the run record
        names a test for (``recorded_unsound_tests``), then each match in
        ``output`` that no recorded warning holds, without a test."""
        known = {test.test for test in tests}
        sources = recorded_unsound_tests((self._run_record() or {}).get("unsound", ()))
        attributed = {warning for _, warning in sources}
        result = []
        for native, warning in sources:
            try:
                test = test_of_nodeid(native)
            except InvalidRequest:
                test = None
            result.append(RunDiagnostic("unsound_test", warning,
                                        test if test in known else None, native))
        result.extend(RunDiagnostic("unsound_test", warning)
                      for warning in unsound_test_warnings(output)
                      if warning not in attributed)
        return tuple(result)

    @staticmethod
    def _detailed(head: str, output: str) -> Tuple[str, Tuple[str, ...]]:
        """A reason and its detail, every non-blank line of ``output``, of which
        the caller shows what it chooses (``SuiteRun.reason_text``). Without
        such a line the reason says so."""
        lines = tuple(line for line in output.strip().splitlines() if line.strip())
        return (head, lines) if lines else (f"{head}: (no output)", ())

    def _read_report(self, report: Path):
        """The tests the report names, the entries it could not map, the
        collection errors and how many unmapped entries failed. None for the
        tests without a readable report."""
        try:
            root = ET.parse(str(report)).getroot()
        except (OSError, ET.ParseError):
            return None, None, None, 0
        names = {self._relative(path): path for path in self.test_files}
        tests: List[TestOutcome] = []
        unmatched: List[str] = []
        unmatched_failures = 0
        collection_errors: List[Tuple[str, str]] = []
        for case in root.iter("testcase"):
            file = (case.get("file") or "").replace("\\", "/")
            classname = case.get("classname") or ""
            name = case.get("name") or ""
            children = {child.tag: child for child in case}
            error = children.get("error")
            if (not classname and error is not None
                    and (error.get("message") or "") == "collection failure"):
                collection_errors.append(
                    (file or name,
                     (error.text or "").strip().splitlines()[-1:][0]
                     if (error.text or "").strip() else "collection failure"))
                continue
            test = self._test_id(file, classname, name, names)
            if test is None:
                unmatched.append(f"{classname}::{name}" if classname else name)
                if "failure" in children or error is not None:
                    unmatched_failures += 1
                continue
            if "failure" in children:
                outcome, detail = FAILED, children["failure"]
            elif error is not None:
                outcome, detail = ERROR, error
            elif "skipped" in children:
                detail = children["skipped"]
                outcome = (XFAILED if detail.get("type") == "pytest.xfail"
                           else SKIPPED)
            else:
                outcome, detail = PASSED, None
            try:
                time_s: Optional[float] = float(case.get("time"))
            except (TypeError, ValueError):
                time_s = None
            failure = detail is not None and outcome in (FAILED, ERROR)
            tests.append(TestOutcome(
                test, outcome, time_s,
                (detail.get("message") or "") if detail is not None else "",
                (detail.text or "").strip() if failure else "",
                (detail.get("type") or "") if failure else ""))
        return (tuple(tests), tuple(unmatched), collection_errors,
                unmatched_failures)

    @staticmethod
    def _test_id(file: str, classname: str, name: str,
                 names: Dict[str, Path]) -> Optional[TestId]:
        """The suite's test the report entry names: ``file`` relative to
        the root directory, ``classname`` the dotted module path followed
        by the classes, ``name`` the function with its parameters."""
        def module_of(path: str) -> str:
            return PurePosixPath(path).with_suffix("").as_posix().replace("/", ".")

        def owns(path: str) -> bool:
            return classname == module_of(path) or classname.startswith(module_of(path) + ".")

        if file not in names or not owns(file):
            # An inherited or imported test's code lives in another file. Its
            # class name still names the suite's module, whose file identifies it.
            owners = [path for path in names if owns(path)]
            if not owners:
                return None
            file = max(owners, key=len)
        module = module_of(file)
        classes = classname[len(module) + 1:] if classname != module else ""
        params: Optional[str] = None
        function = name
        if name.endswith("]") and "[" in name:
            function, params = name[:name.index("[")], name[name.index("[") + 1:-1]
        qualname = f"{classes}.{function}" if classes else function
        node = "::".join([file, *classes.split(".")] if classes else [file])
        text = f"{node}::{function}" + (f"[{params}]" if params is not None
                                        else "")
        return TestId(file=file, qualname=qualname, params=params, text=text)

    # Coverage

    def _coverage(self, data: str, scratch: Path, env: Dict[str, str]):
        """The target's coverage from the run's data, or None with the
        reason when the tool gives none."""
        target = str(self.target)
        started = time.perf_counter()
        try:
            report = _coverage_json(self.adapter,
                data, str(scratch / "coverage.json"), target, env,
                str(self.root))
        except Exception as exc:
            reraise_unresolved(exc)
            return None, f"its report could not be read ({exc})"
        finally:
            self.coverage_report_s = time.perf_counter() - started
        entry = _file_entry(report.get("files") or {}, target, str(self.root))
        if entry is None:
            return None, "its report does not name the target"
        started = time.perf_counter()
        try:
            statements = _covered_statements(self.adapter,
                data, target, _parent_dir(target), env, cwd=str(self.root))
        except MeasurementDataUnreadable as exc:
            return None, f"its data could not be read ({exc})"
        finally:
            self.coverage_statements_s = time.perf_counter() - started
        covered = statements.get("covered_lines")
        if covered is None:
            return None, "its data gives no statements of the target"
        return self._observation(entry, covered, statements.get("traced_lines")), ""

    def _observation(self, entry, covered, traced) -> CoverageObservation:
        """The target's coverage from its coverage.py report ``entry``, the
        ``covered`` statements and the ``traced`` lines, its branch items
        named by the target's path from the root. The suite run and the
        evidence run both build theirs here."""
        identity = self._target_identity()
        missing = [int(n) for n in entry.get("missing_lines") or []]

        def arcs(key: str) -> Tuple[BranchItem, ...]:
            return tuple(BranchItem(identity.path, int(a[0]), int(a[1]))
                         for a in entry.get(key) or [] if len(a) >= 2)

        taken = arcs("executed_branches")
        return CoverageObservation(
            target=identity,
            statements=tuple(sorted({int(n) for n in covered}
                                    | set(missing))),
            covered=tuple(sorted(int(n) for n in covered)),
            branches=tuple(sorted(set(taken) | set(arcs("missing_branches")),
                                  key=lambda b: (b.source, b.destination))),
            taken=tuple(sorted(taken,
                               key=lambda b: (b.source, b.destination))),
            traced_lines=tuple(int(n) for n in traced or ()))

    # The result

    def _target_identity(self) -> FileIdentity:
        """The coverage target's identity, read once where the project runs."""
        if self._target_file is None:
            self._target_file = file_identity(self.adapter, self.target)
        return self._target_file

    def _identity(self) -> ObservationIdentity:
        return ObservationIdentity(
            target=(self._target_identity()
                    if self.target is not None else None),
            suite=self.request.suite.identity(),
            toolchain=self.adapter.toolchain_identity(),
            instrument=self.instrument,
            execution=execution_identity(self.adapter.context))

    def _end_run(self) -> None:
        """At the run's end: the empty pytest-benchmark storage the run
        created goes (``remove_created_storage``), and the files it created
        are recorded, which the next run that asks for it removes
        (``CreatedFiles.end``)."""
        existed, self.storage_existed = self.storage_existed, None
        remove_created_storage(self.adapter, existed)
        if self.request.remove_created_files:
            self.adapter._created_files.end(str(self.root))

    def _side_effects(self) -> SideEffects:
        return SideEffects(
            instrument=self.instrument, instrument_source=self.source,
            files_written=tuple(self.written),
            files_removed=tuple(self.removed),
            leak_record=self.leak_record,
            session_seed=self.request.session_seed,
            reverse_order=self.request.reverse_order,
            timeout_journal=self.timeout_journal_taken)

    def _result(self, outcome: Outcome, reason: str, *, counts: RunCounts,
                tests, unmatched, wall, output: str,
                coverage: Optional[CoverageObservation],
                leaks: Optional[Tuple[Leak, ...]] = None,
                active_at_end: Optional[TestId] = None,
                returncode: Optional[int] = None, diagnostics=(),
                reason_detail=()) -> SuiteRun:
        self._end_run()
        text = self._original_text
        return SuiteRun(
            reason_detail=tuple(text(line) for line in reason_detail),
            outcome=outcome, reason=text(reason), counts=counts,
            tests=tuple(dataclasses.replace(item, message=text(item.message),
                                            trace=text(item.trace))
                        if item.message or item.trace else item for item in tests),
            unmatched=tuple(unmatched), wall_time_s=wall, output=text(output),
            coverage=coverage, identity=self._identity(),
            side_effects=self._side_effects(), leaks=leaks,
            active_at_end=active_at_end, returncode=returncode,
            diagnostics=tuple(dataclasses.replace(item, message=text(item.message))
                              for item in diagnostics),
            coverage_report_s=self.coverage_report_s,
            coverage_statements_s=self.coverage_statements_s,
            runner_counts=self._runner_counts())

    def _run_record(self) -> Optional[Dict[str, Any]]:
        """The run record of a pytest session that ended and counted, or
        None: no record was asked for, or the run ended before its session
        did (a timeout, a crash)."""
        if self.record is None:
            return None
        try:
            record = json.loads(self.record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) and "counts" in record else None

    def _collected_files(self) -> Tuple[str, ...]:
        """The test files of the run record: those pytest collected tests
        from, then those it could not collect, once each."""
        record = self._run_record() or {}
        found: List[str] = []
        for name in [*record.get("files", ()), *record.get("broken", ())]:
            if name.endswith(".py") and "::" not in name and name not in found:
                found.append(name)
        return tuple(found)

    def _runner_counts(self) -> Optional[RunCounts]:
        """The counts of pytest's own summary (``record_counts``), from the
        run record, and its skipped tests. None without a record."""
        record = self._run_record()
        if record is None:
            return None
        counts = record_counts(record)
        return RunCounts(collected=counts["collected"], passed=counts["passed"],
                         failed=counts["failed"], errors=counts["errors"],
                         skipped=int((record.get("counts") or {}).get("skipped", 0)))

    def _original_text(self, text: str) -> str:
        """A run's text with the paths of an owned copy and of a suite staged
        elsewhere written as the originals, as an in-place run prints them."""
        from langadapter.preparation import original_text

        # Windows paths ignore case, a container's do not.
        return original_text(self.adapter.context, _rewrite_paths(
            text, self.path_pairs, ignore_case=os.name == "nt" and not self.container))


def stage_pairs(context: Any, staged: Path, original: Path) -> Tuple[Tuple[str, str], ...]:
    """(staged, original) spellings for a directory ``staged`` standing for
    ``original`` (in the project or its owned copy, written as the original
    project's): as created and resolved, with either slash, and in a container
    as the container sees it."""
    from langadapter.preparation import original_text

    if in_container(context):
        execution = context.execution
        seen = container_path(execution, Path(staged).as_posix())
        return ((seen, original_text(context, container_path(execution, Path(original).as_posix()))),)
    return _path_spellings(((staged, original_text(context, str(original))),))


def _same_file(path: str, root: str) -> str:
    """``path`` in one spelling for comparing files: a relative path taken from
    the project ``root`` (as coverage.py reports a file below its working
    directory), links resolved, in the platform's case and separators."""
    full = path if os.path.isabs(path) else os.path.join(root, path)
    return os.path.normcase(os.path.realpath(full))


def _file_entry(files: Dict[str, Any], target: str, root: str) -> Optional[Dict[str, Any]]:
    """A coverage report's entry for ``target``: the only one whose path, read
    from the project ``root``, is the target's (never a namesake in another
    package), else None."""
    if target in files:
        return files[target]
    wanted = _same_file(target, root)
    found = [entry for path, entry in files.items() if _same_file(path, root) == wanted]
    return found[0] if len(found) == 1 else None


#: The test name the per-file adjudication writes for a kill it attributes
#: to a file rather than to a test.
FILE_ATTRIBUTION = "per_file_adjudication"


def identify_test(node_id: str, suite_files: Sequence[str]) -> Optional[TestId]:
    """The suite's test that ``node_id`` names, or None when none of
    ``suite_files`` (POSIX paths relative to the suite root) matches its
    file. The longest matching file wins."""
    path, names, params = node_parts(node_id)
    if not any(names):
        return None
    parts = PurePosixPath(path.replace("\\", "/")).parts
    match: Optional[str] = None
    for name in suite_files:
        candidate = PurePosixPath(name).parts
        if len(candidate) <= len(parts) and parts[-len(candidate):] == candidate and (
                match is None or len(candidate) > len(PurePosixPath(match).parts)):
            match = name
    if match is None:
        return None
    text = f"{match}::{'::'.join(names)}" + (f"[{params}]" if params is not None else "")
    return TestId(file=match, qualname=".".join(names), params=params, text=text)


class _ProfiledRun(_SuiteRunner):
    """A suite run under cProfile (``profile_suite``): the profile plugin
    loaded beside the run's own, its statistics read before the run's
    scratch directory goes."""

    output: Optional[Path] = None
    data: Optional[Dict[str, Any]] = None

    def _prepare(self, scratch: Path, **options: Any) -> Tuple[Dict[str, str], List[str], Path]:
        env, args, report = super()._prepare(scratch, **options)
        if self.adapter.context._prepared_copy_binding is not None:
            self._source_dirs_first(env)
        plugin_dir = scratch / "profile_plugin"
        plugin_dir.mkdir()
        (plugin_dir / f"{PROFILE_PLUGIN}.py").write_text(
            profile_plugin_text(), encoding="utf-8")
        self.adapter._prepend_pythonpath(env, str(plugin_dir))
        self.output = scratch / "profile.json"
        env[self.adapter._env_name("PROFILE")] = str(self.output)
        return env, [*args, "-p", PROFILE_PLUGIN], report

    def _result(self, outcome: Outcome, reason: str, **fields: Any) -> SuiteRun:
        if self.output is not None:
            parts = []
            for path in sorted(self.output.parent.glob(self.output.name + "*")):
                try:
                    parts.append(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    continue  # a part cut while it was written, as by a timeout, counts as missing
            self.data = _merged(parts) if parts else None
        return super()._result(outcome, reason, **fields)


def _merged(parts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The statistics of every pytest process of a profiled run, summed per
    function: the controller's and, under pytest-xdist, each worker's."""
    rows: Dict[Tuple[str, int, str], List[float]] = {}
    for part in parts:
        for filename, line, name, *values in part["functions"]:
            rows[(filename, line, name)] = [a + b for a, b in zip(
                rows.get((filename, line, name), (0, 0.0, 0.0)), values)]
    return {"version": parts[0]["version"],
            "functions": [[*key, *values] for key, values in rows.items()]}


class _VerificationCollection(_SuiteRunner):
    """The tool check's collection (``verification_collection``): a
    collect-only run with the copy origin plugin, its record read before the
    collection's scratch directory goes. It writes no bytecode, so an
    interpreter used in place, inside the project directory, stays as it was,
    and puts the copy's source directories first, as a run that names the module
    puts its directory first for the common layouts."""

    import_record: Optional[Dict[str, Any]] = None

    def _prepare(self, scratch: Path, **options: Any) -> Tuple[Dict[str, str], List[str], Path]:
        env, args, report = super()._prepare(scratch, **options)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        self._source_dirs_first(env)
        return env, [*args, *self._origin_plugin(scratch, env)], report

    def _collected_files(self) -> Tuple[str, ...]:
        if self.origin is not None:
            self.import_record = read_origin(self.origin)
        return super()._collected_files()


def verification_collection(adapter: Any, selected: str, timeout_s: float
                            ) -> Tuple[CollectionRun, Optional[str], bool]:
    """The project's tests collected in a prepared copy (``adapter``'s
    context), as pytest finds them from the copy's root with a run's
    settings and the plugins a run turns off, without running a test, with
    the copy origin plugin. The collection, for one that collected or
    failed to, the refusal a run without a module gets, else None, and
    whether a module came through a finder placed ahead of the import
    path, which no placement of a run gets past. ``selected`` is one of the
    request's files, which the suite needs to name."""
    root = Path(adapter.context.root)
    setup = loaded_setup_files(root, [root / selected])
    runner = _VerificationCollection(adapter, SuiteRunRequest(
        Suite(str(root), (selected,), tuple(map(str, setup))), timeout_s,
        runner_options=PytestOptions(disabled_plugins=DISABLED_PLUGINS)))
    run = runner.collect(targets=[str(root)])
    if run.outcome not in (Outcome.COMPLETED, Outcome.COLLECTION_FAILED):
        return run, None, False
    record = runner.import_record
    first = record is not None and any(event.get("first") for _, event in origin_events(record))
    return run, origin_refusal(record, collected=run.outcome is Outcome.COMPLETED,
                               named=False), first


def profile_suite(adapter: Any, request: SuiteProfileRequest) -> SuiteProfile:
    """``request``'s suite run once with cProfile around each test's setup,
    call and teardown, its functions read from the statistics
    (``TestOperations.profile_suite``). cProfile's built-in functions,
    which it names ``~``, are not listed, and their time stays in the
    total."""
    request.validate()
    runner = _ProfiledRun(adapter, SuiteRunRequest(request.suite, request.timeout_s,
                                                   runner_options=request.runner_options))
    run = runner.run()
    if run.outcome is not Outcome.COMPLETED or not (runner.data or {}).get("functions"):
        return SuiteProfile(run.outcome if run.outcome is not Outcome.COMPLETED
                            else Outcome.INFRASTRUCTURE_FAILURE,
                            with_detail(run.reason, run.reason_detail)
                            or "every test passed, but the profile recorded no function",
                            None, None, None, None)
    rows = runner.data["functions"]
    total = sum(row[4] for row in rows)
    functions = sorted((ProfiledFunction(_profiled_path(filename, runner.root, runner.container),
                                         name, line, calls,
                                         own, inclusive, own / total if total else 0.0)
                        for filename, line, name, calls, own, inclusive in rows if filename != "~"),
                       key=lambda item: (-item.own_time_s, item.path, item.start_line, item.name))
    return SuiteProfile(Outcome.COMPLETED, "", "cProfile", runner.data["version"], "deterministic",
                        total, tuple(functions))


def _profiled_path(filename: str, root: Any, container: bool) -> str:
    """A profiled function's file relative to ``root`` in POSIX form when it
    lies inside it, else as the profiler names it: in a ``container``, both
    compared as the container's POSIX paths, locally with links resolved on
    both sides."""
    if container:
        name, base = PurePosixPath(filename), PurePosixPath(root)
    elif os.path.isabs(filename):
        name, base = Path(os.path.realpath(filename)), Path(os.path.realpath(root))
    else:
        return filename
    try:
        return name.relative_to(base).as_posix()
    except ValueError:
        return filename


def per_test_evidence(adapter: Any,
                      request: PerTestEvidenceRequest) -> PerTestEvidence:
    """Measure ``request`` for ``adapter`` (a ``PythonAdapter``)."""
    request.validate()
    return _EvidenceRunner(adapter, request).measure()


def _function_test(test: TestId) -> TestId:
    """The test function a (possibly parametrized) test belongs to."""
    if test.params is None:
        return test
    node = "::".join([test.file, *test.qualname.split(".")])
    return TestId(test.file, test.qualname, None, text=node)


def _dotted(test: TestId) -> str:
    """The report's id of a test function: its file's dotted path, then its
    qualified name (``classname.name`` without parameters)."""
    module = PurePosixPath(test.file).with_suffix("").as_posix().replace(
        "/", ".")
    return f"{module}.{test.qualname}"


class _EvidenceRunner(_SuiteRunner):

    def __init__(self, adapter: Any, request: PerTestEvidenceRequest) -> None:
        try:
            super().__init__(adapter, SuiteRunRequest(
                suite=request.suite, timeout_s=request.timeout_s,
                coverage_target=request.target,
                runner_options=request.runner_options,
                module_under_test=request.module_under_test,
                isolation_config=request.isolation_config,
                remove_created_files=request.remove_created_files))
        except InvalidRequest as refused:
            if refused.field != "coverage_target":
                raise
            raise InvalidRequest("target", refused.message) from None
        self.evidence_request = request

    def measure(self) -> PerTestEvidence:
        target = str(self.target)
        with self.adapter._work_dir_tempdir(
                prefix=f"{TEMPDIR_PREFIX}evidence_") as td:
            scratch = Path(td)
            env, args, report = self._prepare(scratch)
            rc_file = scratch / "cov.rc"
            rc_file.write_text(self.adapter.profile().coverage_settings(), encoding="utf-8")
            shield = scratch / "shield"
            shield.mkdir()
            from langadapter.python._plugins import _EVICTION_SHIELD_PLUGIN, _EVIDENCE_TIMES_PLUGIN
            shield_module = SHIELD_MODULE
            times_module = f"{shield_module}_times"
            times_file = scratch / "times.json"
            (shield / f"{shield_module}.py").write_text(
                _EVICTION_SHIELD_PLUGIN, encoding="utf-8")
            # The plugin writes the times where the run sees the file.
            seen = (container_path(self.adapter.context.execution, times_file.as_posix())
                    if in_container(self.adapter.context) else str(times_file))
            (shield / f"{times_module}.py").write_text(
                _EVIDENCE_TIMES_PLUGIN.replace("@TIMES@", repr(seen)), encoding="utf-8")
            self.adapter._prepend_pythonpath(env, str(shield))
            data = str(scratch / "coverage.data")
            command = _coverage_run_command(self.adapter,
                target, data, rc_file,
                [*map(str, self.test_files), *args,
                 "-p", shield_module, "-p", times_module])

            def run() -> Optional[subprocess.CompletedProcess]:
                try:
                    return self.adapter._executor().run(
                        command, cwd=str(self.root), env=env,
                        timeout=self.request.timeout_s)
                except subprocess.TimeoutExpired:
                    _retain_scratch(td, "per-test evidence execution timed out")
                    return None

            try:
                if self.evidence_request.warmup and run() is None:
                    return self._evidence(
                        Outcome.TIMED_OUT,
                        f"the warm-up run reached the timeout of "
                        f"{self.request.timeout_s:g} s")
                completed = run()
            except (OSError, subprocess.SubprocessError) as exc:
                reraise_unresolved(exc)
                _retain_scratch(td, f"per-test evidence execution failed: {type(exc).__name__}: {exc}")
                return self._evidence(
                    Outcome.INFRASTRUCTURE_FAILURE,
                    f"the runner could not execute: {exc}")
            if completed is None:
                return self._evidence(
                    Outcome.TIMED_OUT,
                    f"the measured run reached the timeout of "
                    f"{self.request.timeout_s:g} s")
            output = (completed.stdout or "") + "\n" + (completed.stderr or "")
            outcome, reason, detail, _counts, tests, _unmatched = self._classify(
                completed.returncode, report, output)
            reason = with_detail(reason, detail)
            refusal = self._origin_refusal(outcome)
            if refusal is not None:
                # The tests ran another file than the copy's module.
                _retain_scratch(td, refusal)
                return self._evidence(Outcome.INFRASTRUCTURE_FAILURE, refusal)
            if outcome is not Outcome.COMPLETED:
                return self._not_completed(td, outcome, reason, tests)
            try:
                tokens_by_context = _eviction_tokens_from_coverage(self.adapter,
                    data, target)
                times = _eviction_times(str(times_file))
                coverage = self._suite_coverage(data, scratch, env)
            except MeasurementDataUnreadable as exc:
                # Unread evidence is no evidence: empty tokens would read as
                # tests that cover nothing.
                _retain_scratch(td, str(exc))
                return self._evidence(Outcome.INFRASTRUCTURE_FAILURE, str(exc))
            if coverage is None:
                _retain_scratch(td, "per-test evidence has no complete coverage observation")
        return self._build(tests, tokens_by_context, times, coverage)

    # The evidence

    def _build(self, tests, tokens_by_context: Dict[str, frozenset],
               times: Dict[str, float],
               coverage: Optional[CoverageObservation]) -> PerTestEvidence:
        functions: List[TestId] = []
        for outcome in tests:
            function = _function_test(outcome.test)
            if function not in functions:
                functions.append(function)
        by_dotted = {_dotted(t): t for t in functions}
        items: Dict[TestId, Set[Any]] = {t: set() for t in functions}
        unmatched: List[str] = []
        unmatched_items = []
        for context, tokens in sorted(tokens_by_context.items()):
            function = node_function(context)
            match = (function if function in by_dotted else
                     _match_context_to_test_id(
                         function, set(by_dotted)))
            if match is None:
                unmatched.append(context)
                unmatched_items.append(ContextEvidence(
                    context, frozenset(parse_token(t) for t in tokens)))
                continue
            items[by_dotted[match]].update(parse_token(t) for t in tokens)
        kills, unmapped, kill_items = self._kills(set(functions))
        if kills == KILLS_USABLE:
            for test, found in kill_items.items():
                items[test].update(found)
        evidence = []
        untimed = []
        for test in functions:
            time_ms = times.get(_dotted(test))
            if time_ms is None:
                untimed.append(test)
            evidence.append(TestEvidence(test, frozenset(items[test]),
                                         time_ms))
        known = [e.time_ms for e in evidence if e.time_ms is not None]
        suite_time = sum(known) if known else None
        return self._evidence(
            Outcome.COMPLETED, "", tests=tuple(evidence),
            untimed=tuple(untimed), unmatched=tuple(unmatched), kills=kills,
            unmapped=tuple(unmapped), suite_time=suite_time,
            coverage=coverage, unmatched_items=tuple(unmatched_items),
            outside_items=items_outside_tests(coverage, evidence, Path(str(self.target)).name))

    def _kills(self, functions: Set[TestId]):
        """Kill items per test from the mutation run: a ``KillItem`` for each
        named killing test, and a ``CrashItem`` for the test running when a kill
        without a named killer ended its run (a reason to keep that test, not a
        killer). Usable only when every named killer maps to a test of this
        evidence and some item exists. An unmapped observation is no gap."""
        run = self.evidence_request.mutation_run
        if run is None:
            return KILLS_NOT_REQUESTED, (), {}
        found: Dict[TestId, Set[EvidenceItem]] = {}
        unmapped: List[str] = []
        files = [self._relative(path) for path in self.test_files]
        for mutant in run.mutants:
            mutant_id = mutant.row.get("id")
            active = mutant.row.get("active_at_end")
            if not mutant.killers and active and mutant_id is not None:
                test = identify_test(active, files)
                if test is not None and _function_test(test) in functions:
                    found.setdefault(_function_test(test), set()).add(
                        CrashItem(str(mutant_id)))
                continue
            if not mutant.killers or mutant_id is None:
                continue
            # A killer the run named no test for: unmatched, attributed to a
            # file, or every killer of a run that knows no suite.
            unplaced = list(mutant.unmatched_killers) + [
                k for k in mutant.killers
                if k.endswith(f"::{FILE_ATTRIBUTION}")]
            if not mutant.killing_tests and not unplaced:
                unplaced = list(mutant.killers)
            unmapped += unplaced
            for test in mutant.killing_tests:
                function = _function_test(test)
                if function in functions:
                    found.setdefault(function, set()).add(
                        KillItem(str(mutant_id)))
                else:
                    unmapped.append(str(test))
        if unmapped or not found:
            return KILLS_UNUSABLE, tuple(dict.fromkeys(unmapped)), {}
        return KILLS_USABLE, (), found

    def _suite_coverage(self, data: str, scratch: Path,
                        env: Dict[str, str]) -> Optional[CoverageObservation]:
        """The suite's coverage of the target from a report without a path
        filter (a large package records the file under another path) and
        coverage.py's covered statements. None when the report does not name
        the target. Raises ``MeasurementDataUnreadable`` for unreadable data."""
        target = str(self.target)
        report = str(scratch / "suite_coverage.json")
        try:
            self.adapter._executor().run(
                _coverage_json_command(self.adapter,
                    data, report, target, include=False),
                cwd=str(self.root), env=env, timeout=120)
            import json
            files = json.loads(Path(report).read_text(
                encoding="utf-8")).get("files") or {}
        except Exception as exc:  # no readable report
            reraise_unresolved(exc)
            raise MeasurementDataUnreadable(
                f"the suite's coverage report could not be read: {exc}") from exc
        entry = _file_entry(files, target, str(self.root))
        statements = _covered_statements(self.adapter,
            data, target, _parent_dir(target), env, cwd=str(self.root))
        covered = statements.get("covered_lines")
        if entry is None or covered is None:
            return None
        return self._observation(entry, covered, statements.get("traced_lines"))

    def _evidence(self, outcome: Outcome, reason: str, *, tests=(),
                  untimed=(), unmatched=(), kills: Optional[str] = None,
                  unmapped=(), suite_time: Optional[float] = None,
                  coverage: Optional[CoverageObservation] = None,
                  unmatched_items=(), outside_items=frozenset(),
                  ) -> PerTestEvidence:
        self._end_run()
        requested = self.evidence_request.mutation_run is not None
        return PerTestEvidence(
            outcome=outcome, reason=self._original_text(reason), tests=tuple(tests),
            untimed=tuple(untimed), unmatched_contexts=tuple(unmatched),
            kills=kills or (KILLS_UNUSABLE if requested
                            else KILLS_NOT_REQUESTED),
            unmapped_killers=tuple(unmapped), suite_time_ms=suite_time,
            coverage=coverage, identity=self._identity(),
            side_effects=self._side_effects(), unmatched_items=tuple(unmatched_items),
            outside_items=frozenset(outside_items))

    def _not_completed(self, td, outcome: Outcome, reason: str, tests) -> PerTestEvidence:
        """The evidence of a run that did not complete, ``tests`` its per-test
        outcomes, naming each test that failed or raised an error. The scratch
        of an infrastructure failure is kept."""
        if outcome is Outcome.INFRASTRUCTURE_FAILURE:
            _retain_scratch(td, reason or "per-test evidence produced no verdict")
        return dataclasses.replace(self._evidence(outcome, reason), stopping_tests=tuple(
            dict.fromkeys(t.test for t in tests if t.outcome in (FAILED, ERROR))))


_COLLECTION_ERRORS = re.compile(r"error[s]? during collection")

#: What a repetition's time includes, on this machine and in a container.
INCLUDES_LOCAL = ("the runner's process on this machine: interpreter start, "
                  "collection and the tests")

INCLUDES_CONTAINER = ("the runner's process in the container: interpreter "
                      "start, collection and the tests, and the container "
                      "runtime's execution overhead")


def benchmark(adapter: Any, request: BenchmarkRequest) -> Benchmark:
    """Time ``request`` for ``adapter`` (a ``PythonAdapter``)."""
    request.validate()
    return _BenchmarkRunner(adapter, request).measure()


class _BenchmarkRunner(_SuiteRunner):

    def __init__(self, adapter: Any, request: BenchmarkRequest) -> None:
        super().__init__(adapter, SuiteRunRequest(
            suite=request.suite, timeout_s=request.timeout_s,
            runner_options=(request.runner_options
                            or PytestOptions(traceback="no")),
            module_under_test=request.module_under_test,
            isolation_config=request.isolation_config,
            remove_created_files=request.remove_created_files))
        self.benchmark_request = request
        self.allowed_cpus = None
        self.returncode = None

    def measure(self) -> Benchmark:
        request = self.benchmark_request
        if (request.cpus is not None and not self.container
                and shutil.which("taskset") is None):
            return self._benchmark(
                Outcome.UNSUPPORTED, "a processor set needs taskset, which "
                "this machine does not have", ())
        missing = self._pytest_missing()
        if missing is not None:
            return self._benchmark(Outcome.MISSING_SETUP, missing, ())
        with self.adapter._work_dir_tempdir(
                prefix=f"{TEMPDIR_PREFIX}bench_") as td:
            env, args, _ = self._prepare(Path(td), report=False)
            prefix = ([] if request.cpus is None
                      else ["taskset", "-c", request.cpus])

            def command_with(arguments):
                return [*prefix, *pytest_command(
                    self.adapter.interpreter, self.adapter._runtime_flags(),
                    list(map(str, self.test_files)), arguments)]

            command = command_with(args)
            check_args, check_env = self._warmup_check(Path(td), env, args)
            check = command_with(check_args)
            # Outside the timed repetitions: the processors a process started
            # like them may run on.
            self.allowed_cpus = self.adapter._allowed_cpus(prefix, str(self.root), env)
            times = []
            with placement.serialized(request.serialize):
                for index in range(request.warmup_runs + request.runs):
                    start = time.perf_counter()
                    self.returncode = None
                    run = (check, check_env) if index == 0 else (command, env)
                    try:
                        completed = self._repetition(*run)
                    except subprocess.TimeoutExpired:
                        _retain_scratch(td, "benchmark repetition timed out")
                        # The stopped run's files are the next run's to remove.
                        self._end_run()
                        return self._benchmark(
                            Outcome.TIMED_OUT,
                            f"a repetition reached the timeout of "
                            f"{request.timeout_s:g} s", tuple(times))
                    except (OSError, subprocess.SubprocessError) as exc:
                        reraise_unresolved(exc)
                        _retain_scratch(td, f"benchmark repetition failed: {type(exc).__name__}: {exc}")
                        self._end_run()
                        return self._benchmark(
                            Outcome.INFRASTRUCTURE_FAILURE,
                            f"a repetition could not execute: {exc}", tuple(times))
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    native = getattr(completed, "native_elapsed_s", None)
                    if native is not None:
                        # On Linux the supervisor times the test process
                        # itself, leaving its own start and stop out.
                        elapsed_ms = native * 1000
                    self.returncode = completed.returncode
                    _report_progress(self.benchmark_request, index, elapsed_ms)
                    refusal = self._warmup_refusal(index, completed)
                    if refusal is not None:
                        self._end_run()
                        return self._benchmark(Outcome.INFRASTRUCTURE_FAILURE, refusal, ())
                    if completed.returncode != 0:
                        outcome, reason = self._failure(completed)
                        self._end_run()
                        return self._benchmark(outcome, reason,
                                               tuple(times))
                    if index >= request.warmup_runs:
                        times.append(elapsed_ms)
            # As a suite run records them: the files the next run removes.
            self._end_run()
        return self._benchmark(Outcome.COMPLETED, "", tuple(times))

    def _warmup_check(self, scratch: Path, env: Dict[str, str], args: List[str]
                      ) -> Tuple[List[str], Dict[str, str]]:
        """The pytest arguments and environment of the first repetition. Only
        a first warmup repetition records where the tests imported the copy's
        modules from, so the measured repetitions run without the plugin.
        Without a warmup they are the measured ones."""
        if not self.benchmark_request.warmup_runs:
            return args, env
        check_env = dict(env)
        return [*args, *self._origin_plugin(scratch, check_env)], check_env

    def _warmup_refusal(self, index: int, completed) -> Optional[str]:
        """Why the first repetition, when it carried the plugin, ran another
        file than the copy's, read after its timer stopped, else None."""
        if index or self.origin is None:
            return None
        return self._origin_refusal(Outcome.COMPLETED if completed.returncode == 0
                                    else self._failure(completed)[0])

    def _repetition(self, command, env) -> subprocess.CompletedProcess:
        """One timed repetition: through the executor in a container, else
        on this machine."""
        request = self.benchmark_request
        if self.container:
            return self.adapter._executor().run(
                command, cwd=str(self.root), env=env,
                timeout=request.timeout_s)
        return placement.run_timed(
            command, env=env, cwd=str(self.root), timeout=request.timeout_s)

    def _failure(self, completed) -> Tuple[Outcome, str]:
        """The outcome of a repetition that did not pass, from the runner's
        exit status and summary, since a repetition writes no report."""
        output = (completed.stdout or "") + "\n" + (completed.stderr or "")
        reason = _compact_pytest_output(output,
                                                    completed.returncode)
        if completed.returncode == 5:
            return Outcome.NO_TESTS, "the runner collected no test"
        if completed.returncode == 2 and _COLLECTION_ERRORS.search(output):
            return Outcome.COLLECTION_FAILED, reason
        kind = _pytest_failure_kind(output, completed.returncode)
        if kind == "test_failure":
            return Outcome.TEST_FAILURES, reason
        return Outcome.INFRASTRUCTURE_FAILURE, (
            f"the runner exited with status {completed.returncode}: {reason}")

    def _benchmark(self, outcome: Outcome, reason: str,
                   times: Tuple[float, ...]) -> Benchmark:
        request = self.benchmark_request
        return Benchmark(
            outcome=outcome, reason=self._original_text(reason), times_ms=tuple(times),
            warmup_runs=request.warmup_runs,
            cpus=request.cpus,
            includes=INCLUDES_CONTAINER if self.container else INCLUDES_LOCAL,
            identity=self._identity(), side_effects=self._side_effects(),
            allowed_cpus=self.allowed_cpus, returncode=self.returncode)


def run_as_delivered(adapter, request: SuiteRunRequest, *,
                     sources: Optional[Mapping[str, str]] = None) -> SuiteRun:
    request.validate()
    if request.remove_created_files:
        require_cleanup_prepared_copy(adapter.context, adapter.context.root)
    if request.session_seed is None:
        raise InvalidRequest("session_seed", "is required for a run as delivered")
    if request.coverage_target:
        raise InvalidRequest("coverage_target", "not used by a run as delivered")
    root = Path(request.suite.root).resolve()
    paths = [request.suite._resolve(path).resolve() for path in request.suite.test_files]
    setups = [request.suite._resolve(path).resolve() for path in request.suite.setup_files]
    loaded = set(loaded_setup_files(root, paths))
    if set(setups) != loaded:
        raise InvalidRequest("suite.setup_files", "must list every loaded conftest.py")
    # This operation preserves the generated single-file layout, not arbitrary
    # hierarchical pytest setup whose ancestor hooks would need different staging.
    if any(path not in {test.parent / "conftest.py" for test in paths} for path in setups):
        raise InvalidRequest("suite.setup_files", "each setup must be beside a test file")
    replacements = {}
    for path, text in (sources or {}).items():
        resolved = request.suite._resolve(path).resolve()
        if resolved not in paths or not isinstance(text, str):
            raise InvalidRequest("sources", f"not a listed test source: {path}")
        replacements[resolved] = text
    container = in_container(adapter.context)
    if container:
        work_dir = Path(adapter.context.execution.work_dir).resolve()
        if root != work_dir and work_dir not in root.parents:
            raise InvalidRequest("suite.root", "must lie in the shared work folder (execution.work_dir)")
    identities = []
    for path in request.suite.files():
        path = path.resolve()
        identities.append(FileIdentity(
            path.relative_to(root).as_posix(),
            hashlib.sha256(replacements[path].encode("utf-8")).hexdigest()
            if path in replacements else FileIdentity.of(path, root).sha256))
    tests, outputs, counts, removed, unmatched = [], [], [], [], []
    walls, leaks, recorded, journaled, active = [], [], [], [], None
    diagnostics: List[RunDiagnostic] = []
    # The toolchain the file runs recorded. The result records the same one
    # without a further probe.
    toolchain = None
    outcome, reason, detail = Outcome.COMPLETED, "", ()
    # The timeout bounds the whole run: each file runs in what is left of it.
    deadline = time.monotonic() + request.timeout_s
    for path in paths:
        text = replacements.get(path)
        if text is None:
            text = path.read_text(encoding="utf-8")
        setup = path.parent / "conftest.py"
        with adapter._work_dir_tempdir(
                prefix=f"{TEMPDIR_PREFIX}delivered_") as td:
            stage = Path(td) / "delivered_suite"
            stage.mkdir()
            staged_test = stage / path.name
            staged_test.write_text(text, encoding="utf-8")
            staged_setups = ()
            if setup in setups:
                head = setup.read_text(encoding="utf-8").partition(_INSTRUMENT_MARKER)[0]
                if head.strip():
                    origin = (rebase_path(setup.as_posix(), adapter._work_dir(),
                                          adapter._container_work_dir())
                              if container else str(setup))
                    staged_setup = stage / "conftest.py"
                    staged_setup.write_text(
                        _delivered_setup(head, origin), encoding="utf-8")
                    staged_setups = (str(staged_setup),)
            remaining = deadline - time.monotonic()
            run = None
            if remaining > 0:
                # The staged file prints as the file it stands for.
                run = run_suite(adapter, replace(
                    request, suite=Suite(str(stage), (str(staged_test),), staged_setups),
                    timeout_s=remaining),
                    path_pairs=stage_pairs(adapter.context, stage, path.parent))
                if run.outcome in (Outcome.TIMED_OUT, Outcome.INFRASTRUCTURE_FAILURE):
                    _retain_scratch(td, f"delivered-suite execution failed: {run.reason_text()}")
        if run is None:
            outcome, reason = Outcome.TIMED_OUT, (
                f"the timeout of {request.timeout_s:g} s ended before {path.name} ran")
            break
        toolchain = run.identity.toolchain
        relative = path.relative_to(root).as_posix()

        def delivered(test: TestId) -> TestId:
            nodeid = "::".join([relative, *test.qualname.split(".")])
            if test.params is not None:
                nodeid += f"[{test.params}]"
            return TestId(relative, test.qualname, test.params, nodeid)

        for test in run.tests:
            tests.append(replace(test, test=delivered(test.test)))
        # The leak record and the journal of each staged file, named as the
        # delivered files' tests.
        leaks.extend(replace(leak, test=delivered(leak.test)) for leak in run.leaks or ())
        recorded.append(run.side_effects.leak_record)
        journaled.append(run.side_effects.timeout_journal)
        if run.active_at_end is not None:
            active = delivered(run.active_at_end)
        diagnostics.extend(item if item.test is None else replace(item, test=delivered(item.test))
                           for item in run.diagnostics)
        unmatched.extend(f"{relative}: {entry}" for entry in run.unmatched)
        outputs.append(run.output)
        counts.append(run.counts)
        walls.append(run.wall_time_s)
        removed.extend(run.side_effects.files_removed)
        if run.unmatched:
            outcome, reason = Outcome.INFRASTRUCTURE_FAILURE, (
                f"{path.name}: some reported tests could not be matched to the suite")
            break
        if run.outcome not in (Outcome.COMPLETED, Outcome.TEST_FAILURES):
            # The file's own limit was what was left of the run's.
            outcome, reason = run.outcome, f"{path.name}: " + (
                f"the run reached the timeout of {request.timeout_s:g} s"
                if run.outcome == Outcome.TIMED_OUT else run.reason)
            detail = run.reason_detail
            break
        if run.outcome == Outcome.TEST_FAILURES:
            outcome = Outcome.TEST_FAILURES
    if outcome == Outcome.TEST_FAILURES:
        reason = "one or more tests failed without the measurement instrument"
    totals = {}
    for field in RunCounts.__dataclass_fields__:
        values = [getattr(count, field) for count in counts]
        totals[field] = (sum(values) if values and all(value is not None for value in values)
                         else None)
    return SuiteRun(
        outcome=outcome, reason=reason, counts=RunCounts(**totals), tests=tuple(tests),
        unmatched=tuple(unmatched),
        wall_time_s=sum(walls) if walls and all(wall is not None for wall in walls) else None,
        output=whole("\n".join(outputs), "as-delivered output"), coverage=None,
        reason_detail=detail,
        identity=ObservationIdentity(
            target=None, suite=tuple(identities),
            toolchain=adapter.toolchain_identity() if toolchain is None else toolchain,
            instrument=None, execution=execution_identity(adapter.context)),
        side_effects=SideEffects(
            files_removed=tuple(removed), session_seed=request.session_seed,
            reverse_order=request.reverse_order,
            leak_record=bool(recorded) and all(recorded),
            timeout_journal=any(journaled)),
        leaks=(tuple(leaks) if request.record_leaks and recorded and all(recorded)
               else None),
        active_at_end=active, diagnostics=tuple(diagnostics),
    )


class MeasurementDataUnreadable(RuntimeError):
    """A run's data its reader could not read (coverage data, a test report):
    the measurement fails with this cause instead of reading it as empty."""


class LeakRecord(NamedTuple):
    """A test that left shared state the instrument restores changed, as
    recorded once its own fixtures were torn down: the test in the
    failing-test listing's form (``classname.name`` without ``[param]``) and
    as pytest's node id, the names of the module under test it added,
    rebound and removed, and the xarray options and numpy error and print
    settings it changed."""

    test: str
    nodeid: str
    added: Tuple[str, ...] = ()
    rebound: Tuple[str, ...] = ()
    removed: Tuple[str, ...] = ()
    xarray_options: Tuple[str, ...] = ()
    numpy_errors: Tuple[str, ...] = ()
    numpy_print: Tuple[str, ...] = ()


def node_function(nodeid: str) -> str:
    """The dotted id of the test function a node id names, as the JUnit
    report names it: the file's path dotted without ``.py``, its classes and
    function, no case ``[id]`` (``tests/test_a.py::TestX::test_b[1]`` gives
    ``tests.test_a.TestX.test_b``)."""
    file, names, _params = node_parts(nodeid)
    return ".".join([re.sub(r"\.py$", "", file.replace("\\", "/").replace("/", ".")),
                     *(name for name in names if name)])


def run_record_mark() -> str:
    """The text that starts the run record's line in a run's output."""
    return render(RUN_RECORD_MARK)


def run_record_in(output: Optional[str]) -> Optional[Dict[str, Any]]:
    """The run record a run printed on its output (its last record line), or
    None when the run printed none: it ended before pytest's session did, or
    ran without the plugin."""
    mark = run_record_mark()
    for line in reversed((output or "").splitlines()):
        if line.startswith(mark):
            try:
                record = json.loads(line[len(mark):])
            except ValueError:
                return None
            return record if isinstance(record, dict) else None
    return None


def record_killers(record: Optional[Dict[str, Any]]) -> Tuple[str, ...]:
    """The tests a run record names as failed and as erroneous, once each, in
    the order pytest's short summary lists them: the two groups as its
    report characters order them (``order``, failures first where they list
    neither, as under -rN). These are the tests that kill a mutant. A
    collection node (no ``::``) names no test, and a run that stopped at
    its first failure (``-x``) names only the earliest killer."""
    groups = {"f": (record or {}).get("failed", ()), "E": (record or {}).get("errors", ())}
    order = [char for char in (record or {}).get("order", "fE") if char in groups]
    order += [char for char in "fE" if char not in order]
    ids: List[str] = []
    for char in order:
        for nodeid in groups[char]:
            if "::" in nodeid and nodeid not in ids:
                ids.append(nodeid)
    return tuple(ids)


def record_counts(record: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """The test counts of a run record as pytest's quiet summary line gives
    them: its passed, failed and error counts, and as collected their sum
    less one per file pytest could not collect (a collection error counts
    as an error, not a collected test). A failing test whose teardown also
    fails counts as a failure and an error, and an unexpected pass of a
    non-strict xfail as neither. All zero without a record."""
    counts = (record or {}).get("counts") or {}
    passed, failed, errors = (int(counts.get(key, 0)) for key in ("passed", "failed", "error"))
    broken = len(set((record or {}).get("broken", ())))
    return {"collected": max(passed + failed + errors - broken, 0),
            "passed": passed, "failed": failed, "errors": errors}


def record_reported(record: Optional[Dict[str, Any]]) -> bool:
    """Whether a run record shows a run whose pytest session ended and
    reported, rather than a process that died before measuring anything."""
    return record is not None and "counts" in record


def record_collected_nothing(record: Optional[Dict[str, Any]]) -> bool:
    """Whether a run record shows a run whose summary had nothing to count
    (pytest's "no tests ran"): no test, no error and no warning."""
    return record_reported(record) and not any((record.get("counts") or {}).values())


def read_leak_journal(path: str) -> Optional[Tuple[LeakRecord, ...]]:
    """The leak records a run wrote to ``path``, in the order the tests
    finished. None when the journal holds no mark that the record was taken
    (the instrument had no record, or the run ended before loading it)."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    taken = False
    records: List[LeakRecord] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if "record" in entry:
            taken = True
            continue
        nodeid = entry.get("test")
        if not isinstance(nodeid, str):
            continue
        module = entry.get("module") or {}
        records.append(LeakRecord(
            test=node_function(nodeid), nodeid=nodeid,
            added=tuple(module.get("added") or ()),
            rebound=tuple(module.get("rebound") or ()),
            removed=tuple(module.get("removed") or ()),
            xarray_options=tuple(entry.get("xarray_options") or ()),
            numpy_errors=tuple(entry.get("numpy_errors") or ()),
            numpy_print=tuple(entry.get("numpy_print") or ())))
    return tuple(records) if taken else None
