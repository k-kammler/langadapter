"""The Java adapter's runs: native JUnit discovery without executing test
bodies or test factories, independent runs of each declared test file with
its helpers (``run_as_delivered``), and per-test evidence.

Per-test evidence: in one JaCoCo-instrumented run a JUnit Platform listener
writes each test's execution data to its own file, whose analysis gives the
target lines and branches (``BranchItem(file, line, ordinal)``, as in suite
runs) the test covered. ``@BeforeAll``, ``@AfterAll`` and static
initializer lines (run by whichever test first uses the class) belong to no
test but count towards the suite's coverage. A parameterized method is one
test, its invocations' items merged and times summed. A test's time is the
listener's, in nanoseconds without its writes. The target and its member
classes load before the tests, outside every test's time. A requested
warm-up pass is discarded: each pass is a fresh JVM, so no JIT warms.
"""
from __future__ import annotations

from langadapter._execution_scope import reraise_unresolved
from langadapter.java._profile import JvmProfile
import base64
import json
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from langadapter.sources import bounded, whole
from langadapter.execution import ContainerExecutor, _retain_scratch, Executor
from langadapter.java import _tools as _junit, _tools, _jacoco
from langadapter.java._env import JavaEnv, remaining, require_mounted_output
from langadapter.java._build import Compilation, compile_suite, tool_environment
from langadapter.outcomes import Outcome, FileIdentity, ObservationIdentity
from langadapter.runs import (
    CollectionRequest,
    CollectionRun,
    RunCounts,
    SideEffects,
    SuiteRun,
    SuiteRunRequest,
    KILLS_NOT_REQUESTED,
    KILLS_UNUSABLE,
    KILLS_USABLE,
    ContextEvidence,
    PerTestEvidence,
    PerTestEvidenceRequest,
    RunDiagnostic,
    TestEvidence,
    items_outside_tests,
)
import hashlib
import shutil
from typing import Mapping, Optional, Any, Dict, List, Set, Tuple
from langadapter.errors import InvalidRequest
from langadapter.suites import (
    Suite, BranchItem, EvidenceItem, KillItem, LineItem, TestId,
)
from contextlib import ExitStack
import xml.etree.ElementTree as ET
from langadapter.mutation import DETECTED_STATUSES


_DISCOVER = """\
import java.nio.file.Files;
import java.nio.file.Path;
import org.junit.platform.engine.discovery.DiscoverySelectors;
import org.junit.platform.engine.support.descriptor.MethodSource;
import org.junit.platform.launcher.TestPlan;
import org.junit.platform.launcher.core.LauncherDiscoveryRequestBuilder;
import org.junit.platform.launcher.core.LauncherFactory;

public class LangAdapterDiscover {
    public static void main(String[] args) throws Exception {
        Path report = Path.of(args[0]);
        String outcome;
        try {
            LauncherDiscoveryRequestBuilder request = LauncherDiscoveryRequestBuilder.request();
            for (int i = 1; i < args.length; i++) {
                request.selectors(DiscoverySelectors.selectClass(args[i]));
            }
            TestPlan plan = LauncherFactory.create().discover(request.build());
            // Parameterized tests and factories acquire their children only at execution.
            boolean found = plan.containsTests() || plan.getRoots().stream()
                .flatMap(root -> plan.getDescendants(root).stream())
                .anyMatch(test -> test.getSource().filter(MethodSource.class::isInstance).isPresent());
            outcome = found ? "completed" : "no_tests";
        } catch (Throwable error) {
            error.printStackTrace(System.err);
            outcome = "collection_failed";
        }
        Files.writeString(report, "{\\"outcome\\":\\"" + outcome + "\\"}");
    }
}
"""


def collect_suite(adapter, request: CollectionRequest) -> CollectionRun:
    request.validate()
    issue = adapter._request_issue(request)
    identity = adapter._observation_identity(request.suite)

    scope = None

    def result(outcome, reason="", output="", returncode=None, wall=None):
        if scope is not None and outcome in (Outcome.INFRASTRUCTURE_FAILURE, Outcome.TIMED_OUT):
            _retain_scratch(scope, f"discovery execution failed: {reason}")
        return CollectionRun(outcome, reason, identity, whole(output, "collection output"),
                             diagnostic=bounded(output) if outcome is Outcome.COLLECTION_FAILED else "",
                             returncode=returncode, wall_time_s=wall)

    if issue:
        return result(Outcome.UNSUPPORTED, issue)
    setup_issue = adapter.execution_setup_issue()
    if setup_issue:
        return result(Outcome.MISSING_SETUP, setup_issue)
    resolution = adapter.project()
    if resolution.outcome is not Outcome.COMPLETED:
        return result(resolution.outcome, resolution.reason, resolution.output)
    project, jdk = resolution.project, adapter._jdk()
    environment = adapter.context.environment
    executor = adapter._executor
    with adapter._scratch("collect") as scratch:
        allocated = scratch
        scratch = Path(scratch)
        try:
            require_mounted_output(str(scratch), executor)
        except OSError as exc:
            reraise_unresolved(exc)
            return result(Outcome.INFRASTRUCTURE_FAILURE, str(exc))
        scope = allocated
        try:
            compiled = compile_suite(project, jdk, request.suite, str(scratch / "classes"),
                                     executor, environment=environment)
        except (OSError, subprocess.SubprocessError) as exc:
            reraise_unresolved(exc)
            return result(Outcome.INFRASTRUCTURE_FAILURE, f"discovery suite preparation failed: {exc}")
        if compiled.outcome is not Outcome.COMPLETED:
            return result(compiled.outcome, "the suite does not compile", compiled.diagnostics)
        try:
            tools = _junit.toolset(project.run_classpath(), _tools.host_environment(adapter.context),
                                   executor=executor, scratch=scratch / "tools")
        except _tools.ToolUnavailable as exc:
            reraise_unresolved(exc)
            return result(Outcome.MISSING_SETUP, str(exc))
        except OSError as exc:
            reraise_unresolved(exc)
            return result(Outcome.INFRASTRUCTURE_FAILURE, str(exc))
        identity = replace(identity, toolchain=adapter.toolchain_identity(tools.platform, tools.junit4))
        try:
            classes = adapter._class_names(adapter._suite_files(request.suite))
        except OSError as exc:
            reraise_unresolved(exc)
            return result(Outcome.INFRASTRUCTURE_FAILURE, f"discovery classes could not be read: {exc}")
        return result(*discover(adapter, scratch, compiled.classes, classes, tools, request.timeout_s))


def compile_helper_source(place: JavaEnv, javac: str, source: Path, text: str, classpath, stage: str,
                          **options) -> subprocess.CompletedProcess:
    """The finished ``javac`` that compiled helper ``text``, written to
    ``source``, into the source's directory against ``classpath``, where
    ``place`` runs with ``options`` (``cwd``, ``env``). Two minutes and
    ``place``'s deadline bound it, a timeout naming ``stage``."""
    source.write_text(text, encoding="utf-8")
    return place.run(
        [javac, "--release", str(JvmProfile.helper_release), "-nowarn", "-d", place.translate(source.parent),
         "-cp", place.path_list(classpath), place.translate(source)],
        120, stage, **options)


def discover(adapter, scratch: Path, classes: str, names, tools, timeout: float, *,
             deadline: Optional[float] = None) -> Tuple[Outcome, str, str, Optional[int], Optional[float]]:
    """JUnit discovery of the test classes ``names`` compiled into
    ``classes``, with the launcher ``tools``, in ``scratch``, on the
    adapter's resolved project and JDK, without executing a test body or
    test factory: the outcome, its reason, the output, the exit code and the
    time, as a collection reports them. ``timeout`` bounds the discovery and
    ``deadline`` every step."""
    project, jdk, executor = adapter.project().project, adapter._jdk(), adapter._executor
    place = JavaEnv(executor, deadline=deadline)
    env = tool_environment(jdk, adapter.context.environment, executor=executor)
    try:
        helper = compile_helper_source(
            place, jdk.tool("javac"), scratch / "LangAdapterDiscover.java", _DISCOVER,
            (*tools.jars, *project.run_classpath()), "discovery helper compilation", cwd=project.root, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        reraise_unresolved(exc)
        return Outcome.INFRASTRUCTURE_FAILURE, f"discovery helper compilation failed: {exc}", "", None, None
    if helper.returncode:
        return (Outcome.INFRASTRUCTURE_FAILURE, "discovery helper did not compile",
                helper.stdout + helper.stderr, None, None)
    report = scratch / "discovery.json"
    args = JavaEnv(executor).java_arguments(
        (scratch, classes, *tools.jars, *project.run_classpath()),
        "LangAdapterDiscover", place.translate(report), *names,
        jvm_args=(*project.test_jvm_args, *adapter.context.interpreter_flags),
        properties=project.test_system_properties)
    try:
        argfile = _junit.argfile(scratch / "discover.args", args)
    except OSError as exc:
        reraise_unresolved(exc)
        return Outcome.INFRASTRUCTURE_FAILURE, f"discovery arguments could not be written: {exc}", "", None, None
    started = time.perf_counter()
    try:
        done = JavaEnv(executor).launch([jdk.tool("java"), f"@{place.translate(argfile)}"],
                                        remaining(deadline, timeout, "discovery"), env=env, cwd=project.root)
    except subprocess.TimeoutExpired as expired:
        reraise_unresolved(expired)
        output = "".join(value.decode("utf-8", "replace")
                         if isinstance(value, bytes) else (value or "")
                         for value in (expired.output, expired.stderr))
        return (Outcome.TIMED_OUT, "the collection timeout ended discovery", output, None,
                time.perf_counter() - started)
    except OSError as exc:
        reraise_unresolved(exc)
        return Outcome.INFRASTRUCTURE_FAILURE, f"discovery could not start: {exc}", "", None, None
    wall, output = time.perf_counter() - started, done.stdout + done.stderr
    if done.returncode:
        return Outcome.INFRASTRUCTURE_FAILURE, "discovery exited without a verdict", output, done.returncode, wall
    try:
        outcome = _verdict(report)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reraise_unresolved(exc)
        return Outcome.INFRASTRUCTURE_FAILURE, f"invalid discovery report: {exc}", output, done.returncode, wall
    reason = ("JUnit discovery failed" if outcome is Outcome.COLLECTION_FAILED
              else "JUnit discovered no tests" if outcome is Outcome.NO_TESTS else "")
    return outcome, reason, output, done.returncode, wall


def _verdict(report: Path) -> Outcome:
    """The outcome the discovery helper wrote into ``report``."""
    outcome = Outcome(json.loads(report.read_text(encoding="utf-8"))["outcome"])
    if outcome not in (Outcome.COMPLETED, Outcome.NO_TESTS, Outcome.COLLECTION_FAILED):
        raise ValueError("unexpected discovery outcome")
    return outcome


def run_as_delivered(adapter, request: SuiteRunRequest, *,
                     sources: Optional[Mapping[str, str]] = None) -> SuiteRun:
    request.validate()
    for field in ("coverage_target", "module_under_test"):
        if getattr(request, field):
            raise InvalidRequest(field, "not used by a run as delivered")
    issue = adapter._request_issue(request)
    setup_issue = adapter.execution_setup_issue()
    identity = adapter._observation_identity(request.suite)
    root = Path(request.suite.root).resolve()
    paths = [path.resolve() for path in adapter._suite_files(request.suite)]
    setups = [request.suite._resolve(path).resolve() for path in request.suite.setup_files]
    replacements = {}
    for path, text in (sources or {}).items():
        resolved = request.suite._resolve(path).resolve()
        if resolved not in paths or not isinstance(text, str):
            raise InvalidRequest("sources", f"not a listed test source: {path}")
        replacements[resolved] = text
    identities = tuple(FileIdentity(
        path.relative_to(root).as_posix(),
        hashlib.sha256(replacements[path].encode("utf-8")).hexdigest()
        if path in replacements else FileIdentity.of(path, root).sha256)
        for path in (path.resolve() for path in request.suite.files()))
    identity = replace(identity, suite=identities)
    tests, outputs, counts, walls, unmatched, diagnostics = [], [], [], [], [], []
    removed = []
    outcome, reason = ((Outcome.UNSUPPORTED, issue) if issue else
                       (Outcome.MISSING_SETUP, setup_issue) if setup_issue else (Outcome.COMPLETED, ""))
    journal, active = False, None
    # The timeout bounds the whole run: each file runs in what is left of it.
    deadline = time.monotonic() + request.timeout_s
    if not issue and not setup_issue:
        for path in paths:
            try:
                with adapter._scratch("delivered") as directory:
                    stage = Path(directory)
                    require_mounted_output(str(stage), adapter._executor)
                    for original in (path, *setups):
                        destination = stage / original.relative_to(root)
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if original in replacements:
                            destination.write_bytes(replacements[original].encode("utf-8"))
                        else:
                            shutil.copyfile(original, destination)
                    remaining = deadline - time.monotonic()
                    run = None
                    if remaining > 0:
                        run = adapter.run_suite(replace(request, suite=Suite(
                            str(stage), (path.relative_to(root).as_posix(),),
                            tuple(setup.relative_to(root).as_posix() for setup in setups)),
                            timeout_s=remaining))
                        if run.outcome in (Outcome.TIMED_OUT, Outcome.INFRASTRUCTURE_FAILURE):
                            _retain_scratch(directory,
                                            f"delivered-suite execution failed: {run.reason}")
            except (OSError, subprocess.SubprocessError) as exc:
                reraise_unresolved(exc)
                outcome, reason = Outcome.INFRASTRUCTURE_FAILURE, str(exc)
                counts.append(RunCounts())
                walls.append(None)
                break
            if run is None:
                outcome, reason = Outcome.TIMED_OUT, (
                    f"the timeout of {request.timeout_s:g} s ended before {path.name} ran")
                break
            tests.extend(run.tests)
            outputs.append(run.output)
            counts.append(run.counts)
            walls.append(run.wall_time_s)
            unmatched.extend(run.unmatched)
            diagnostics.extend(run.diagnostics)
            active = run.active_at_end
            journal = journal or run.side_effects.timeout_journal
            # Each file's run removes what the runs before it created.
            removed.extend(name for name in run.side_effects.files_removed if name not in removed)
            identity = replace(identity, toolchain=run.identity.toolchain)
            if run.unmatched:
                outcome, reason = Outcome.INFRASTRUCTURE_FAILURE, (
                    f"{path.name}: some reported tests could not be matched to the suite")
                break
            if run.outcome not in (Outcome.COMPLETED, Outcome.TEST_FAILURES):
                outcome, reason = run.outcome, f"{path.name}: {run.reason}"
                break
            if run.outcome is Outcome.TEST_FAILURES:
                outcome, reason = Outcome.TEST_FAILURES, "one or more delivered tests failed"
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
        identity=identity,
        side_effects=SideEffects(files_removed=tuple(removed), timeout_journal=journal),
        active_at_end=active,
        diagnostics=tuple(diagnostics))


#: The system property naming the directory the listener writes to.
PROPERTY = "langadapter.coverage.dir"
#: The system property naming the target classes the listener loads first.
PRELOAD = "langadapter.coverage.preload"
#: The JUnit Platform API the listener compiles against (unchanged 1.x to 6).
API = (
    f"org.junit.platform:junit-platform-launcher:{_junit.DEFAULT_PLATFORM}",
    f"org.junit.platform:junit-platform-engine:{_junit.DEFAULT_PLATFORM}",
    f"org.junit.platform:junit-platform-commons:{_junit.DEFAULT_PLATFORM}",
    _junit.OPENTEST4J,
    "org.apiguardian:apiguardian-api:1.1.2",
)
_LISTENER = """\
import java.io.OutputStream;
import java.io.Writer;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardOpenOption;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Map;
import java.util.Set;
import org.junit.platform.engine.TestExecutionResult;
import org.junit.platform.launcher.TestExecutionListener;
import org.junit.platform.launcher.TestIdentifier;
import org.junit.platform.launcher.TestPlan;

/** Writes JaCoCo's execution data of every test to a file of its own and
 *  the data recorded outside tests to outside.exec. Each test's time, from
 *  its start to its end without the listener's own writes, goes to
 *  times.tsv in nanoseconds. The classes langadapter.coverage.preload names,
 *  with their member classes, are loaded before the tests, uninitialized,
 *  so no test's time holds their loading and instrumentation. */
public class LangAdapterCoverageListener implements TestExecutionListener {
    private final Path dir;
    private Object agent;
    private Method data;
    private int index;
    private final Set<String> active = new HashSet<>();
    private final Map<String, Long> started = new HashMap<>();

    public LangAdapterCoverageListener() {
        String target = System.getProperty("langadapter.coverage.dir");
        dir = target == null ? null : Paths.get(target);
    }

    private byte[] take() throws Exception {
        if (agent == null) {
            Class<?> rt = Class.forName("org.jacoco.agent.rt.RT");
            agent = rt.getMethod("getAgent").invoke(null);
            data = agent.getClass().getMethod("getExecutionData", boolean.class);
        }
        return (byte[]) data.invoke(agent, true);
    }

    private void outside() throws Exception {
        try (OutputStream out = Files.newOutputStream(dir.resolve("outside.exec"),
                StandardOpenOption.CREATE, StandardOpenOption.APPEND)) {
            out.write(take());
        }
    }

    @Override
    public synchronized void executionStarted(TestIdentifier test) {
        if (dir == null) {
            return;
        }
        try {
            if (!active.isEmpty()) {
                Files.write(dir.resolve("overlap"), new byte[0]);
            }
            if (!test.isTest()) {
                return;
            }
            active.add(test.getUniqueId());
            outside();
            started.put(test.getUniqueId(), System.nanoTime());
        } catch (Exception e) {
            throw new IllegalStateException("JaCoCo's agent is not reachable", e);
        }
    }

    @Override
    public synchronized void executionFinished(TestIdentifier test, TestExecutionResult result) {
        long end = System.nanoTime();
        if (dir == null) {
            return;
        }
        try {
            if (!test.isTest()) {
                if (!active.isEmpty()) {
                    Files.write(dir.resolve("overlap"), new byte[0]);
                }
                return;
            }
            byte[] bytes = take();
            active.remove(test.getUniqueId());
            index++;
            Files.write(dir.resolve(index + ".exec"), bytes);
            try (Writer out = Files.newBufferedWriter(dir.resolve("index.tsv"),
                    StandardCharsets.UTF_8, StandardOpenOption.CREATE,
                    StandardOpenOption.APPEND)) {
                out.write(index + "\\t" + test.getUniqueId() + "\\n");
            }
            Long start = started.remove(test.getUniqueId());
            if (start != null) {
                try (Writer out = Files.newBufferedWriter(dir.resolve("times.tsv"),
                        StandardCharsets.UTF_8, StandardOpenOption.CREATE,
                        StandardOpenOption.APPEND)) {
                    out.write(test.getUniqueId() + "\\t" + (end - start) + "\\n");
                }
            }
        } catch (Exception e) {
            throw new IllegalStateException("a test's coverage could not be written", e);
        }
    }

    @Override
    public void testPlanExecutionStarted(TestPlan plan) {
        String names = System.getProperty("langadapter.coverage.preload");
        if (dir == null || names == null || names.isEmpty()) {
            return;
        }
        ClassLoader loader = Thread.currentThread().getContextClassLoader();
        for (String name : names.split(",")) {
            try {
                Class.forName(name, false, loader).getDeclaredClasses();
            } catch (Throwable e) {
                try {
                    Files.write(dir.resolve("unloaded"),
                            (name + ": " + e).getBytes(StandardCharsets.UTF_8));
                } catch (Exception ignored) {
                    // Without the marker the run's times hold the loading.
                }
            }
        }
    }

    @Override
    public void testPlanExecutionFinished(TestPlan plan) {
        if (dir == null) {
            return;
        }
        try {
            outside();
        } catch (Exception e) {
            throw new IllegalStateException("JaCoCo's agent is not reachable", e);
        }
    }
}
"""

def listener(jdk: Any, environment: Dict[str, str], env: Dict[str, str], *,
             executor: Optional[Executor] = None, scratch: Optional[Path] = None) -> Path:
    """The compiled coverage listener's directory, its service registered."""
    return _tools.compiled_helper(
        jdk.tool("javac"), "LangAdapterCoverageListener", _LISTENER, environment, env,
        prefix="listener", what="the coverage listener",
        classpath=lambda: [_tools.jar(c, environment) for c in API],
        service="org.junit.platform.launcher.TestExecutionListener",
        executor=executor, scratch=scratch)[0]


def per_test_evidence(adapter: Any, request: PerTestEvidenceRequest) -> PerTestEvidence:
    """The per-test evidence of ``request`` for ``adapter`` (a ``JavaAdapter``)."""
    request.validate()
    issue = adapter._request_issue(request)
    setup_issue = adapter.execution_setup_issue()
    if issue or setup_issue:
        return PerTestEvidence(
            outcome=Outcome.UNSUPPORTED if issue else Outcome.MISSING_SETUP,
            reason=issue or setup_issue, tests=(), untimed=(), unmatched_contexts=(),
            kills=KILLS_UNUSABLE if request.mutation_run is not None else KILLS_NOT_REQUESTED,
            unmapped_killers=(), suite_time_ms=None, coverage=None,
            identity=adapter._observation_identity(request.suite))
    files = adapter._suite_files(request.suite)
    suite_root = Path(request.suite.root).resolve()
    try:
        target = adapter._project_file("target", request.target)
        identity = adapter._observation_identity(request.suite, target)
    except (OSError, subprocess.SubprocessError) as exc:
        reraise_unresolved(exc)
        return PerTestEvidence(
            outcome=Outcome.INFRASTRUCTURE_FAILURE,
            reason=f"the target identity could not be read: {exc}",
            tests=(), untimed=(), unmatched_contexts=(),
            kills=KILLS_UNUSABLE if request.mutation_run is not None else KILLS_NOT_REQUESTED,
            unmapped_killers=(), suite_time_ms=None, coverage=None,
            identity=adapter._observation_identity(request.suite))

    scope = None
    removed: Tuple[str, ...] = ()

    def failed(outcome: Outcome, reason: str, stopping=()) -> PerTestEvidence:
        if scope is not None and outcome in (Outcome.INFRASTRUCTURE_FAILURE, Outcome.TIMED_OUT):
            _retain_scratch(scope, f"per-test evidence execution failed: {reason}")
        from langadapter.java.adapter import _with_removed
        return _with_removed(PerTestEvidence(
            outcome=outcome, reason=reason, tests=(), untimed=(),
            unmatched_contexts=(),
            kills=(KILLS_UNUSABLE if request.mutation_run is not None
                   else KILLS_NOT_REQUESTED),
            unmapped_killers=(), suite_time_ms=None, coverage=None,
            identity=identity, stopping_tests=tuple(stopping)), removed)

    jdk = adapter._jdk()
    resolution = adapter.project()
    if resolution.outcome is not Outcome.COMPLETED:
        return failed(resolution.outcome, resolution.reason)
    project = resolution.project
    environment = adapter.context.environment
    host = _tools.host_environment(adapter.context)
    executor = adapter._executor
    container = isinstance(executor, ContainerExecutor)
    env = tool_environment(jdk, environment, executor=executor)
    with ExitStack() as stack:
        try:
            allocated = stack.enter_context(adapter._scratch("evidence"))
            scratch = Path(allocated)
            require_mounted_output(str(scratch), executor)
            scope = allocated
            compiled = compile_suite(project, jdk, request.suite,
                                     str(scratch / "classes"), executor,
                                     environment=environment)
        except (OSError, subprocess.SubprocessError) as exc:
            reraise_unresolved(exc)
            return failed(Outcome.INFRASTRUCTURE_FAILURE,
                          f"evidence suite preparation failed: {exc}")
        if compiled.outcome is not Outcome.COMPLETED:
            return failed(compiled.outcome, "the suite does not compile: "
                          + compiled.diagnostics[-1000:])
        try:
            tools = _junit.toolset(project.run_classpath(), host,
                                   executor=executor, scratch=scratch / "tools")
            helper_dir = listener(jdk, host, env, executor=executor, scratch=scratch)
            _jacoco.analyzer(jdk, host, env, executor=executor, scratch=scratch)
            agent = _tools.jar(_jacoco.AGENT, host)
            if container:
                agent = _tools.stage_files((str(agent),), scratch / "tools", executor=executor)[0]
            target_classes = _jacoco.class_names(target, executor=executor)
            classes = adapter._class_names(files)
        except _tools.ToolUnavailable as exc:
            reraise_unresolved(exc)
            return failed(Outcome.MISSING_SETUP, str(exc))
        except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError) as exc:
            reraise_unresolved(exc)
            return failed(Outcome.INFRASTRUCTURE_FAILURE,
                          f"the coverage tools could not start: {exc}")
        toolchain = adapter.toolchain_identity(tools.platform, tools.junit4)
        identity = replace(identity, toolchain=replace(
            toolchain, tools=(*toolchain.tools, ("jacoco", _jacoco.VERSION))))
        removed = adapter._remove_created(request)
        for pass_index in range(2 if request.warmup else 1):
            data = scratch / f"per-test-{pass_index}"
            try:
                data.mkdir()
                launch = _junit.launch(
                    jdk.tool("java"),
                    [compiled.classes, *tools.jars, *project.run_classpath(),
                     str(helper_dir)],
                    list(classes), scratch / f"reports-{pass_index}",
                    jvm_args=[f"-javaagent:{agent}=includes="
                              f"{_jacoco.includes(target_classes)},output=none",
                              f"-D{PROPERTY}={data}",
                              f"-D{PRELOAD}={','.join(target_classes)}",
                              *project.test_jvm_args,
                              *adapter.context.interpreter_flags],
                    system_properties=project.test_system_properties,
                    cwd=project.root, env=env, timeout=request.timeout_s, executor=executor)
            except (OSError, subprocess.SubprocessError) as exc:
                reraise_unresolved(exc)
                return failed(Outcome.INFRASTRUCTURE_FAILURE,
                              f"the launcher could not start: {exc}")
            if launch.timed_out:
                adapter._record_created(request)
                phase = "warm-up" if request.warmup and pass_index == 0 else "measured"
                return failed(Outcome.TIMED_OUT, f"the suite's timeout ended the {phase} run")
        adapter._record_created(request)
        try:
            cases = _junit.read_reports(launch.reports)
        except (OSError, ET.ParseError, ValueError) as exc:
            reraise_unresolved(exc)
            return failed(Outcome.INFRASTRUCTURE_FAILURE,
                          f"the launcher report could not be read: {exc}")
        from langadapter.java.adapter import _suite_run
        run = _suite_run(launch, cases, classes, request.suite.root,
                         0.0, identity)
        if run.outcome is not Outcome.COMPLETED:
            return failed(run.outcome, run.reason or "the suite is not green", run.failing_tests())
        try:
            if (data / "overlap").exists():
                return failed(Outcome.UNSUPPORTED,
                              "overlapping JUnit execution cannot be separated into "
                              "per-test coverage with process-wide JaCoCo probes")
            if (data / "unloaded").exists():
                return failed(Outcome.INFRASTRUCTURE_FAILURE,
                              "the target's classes could not be loaded before the "
                              "tests: " + (data / "unloaded").read_text(encoding="utf-8"))
            records, problem = _jacoco.analyze(
                jdk, "tests", data, target, project.main_classes,
                environment=host, env=env, executor=executor, scratch=scratch)
        except (_tools.ToolUnavailable, OSError, subprocess.SubprocessError, ValueError) as exc:
            reraise_unresolved(exc)
            return failed(Outcome.INFRASTRUCTURE_FAILURE,
                          f"coverage analysis failed: {exc}")
        if records is None:
            return failed(Outcome.INFRASTRUCTURE_FAILURE, problem)
        index, seen = {}, set()
        try:
            for line in (data / "index.tsv").read_text(encoding="utf-8").splitlines():
                number, separator, unique = line.partition("\t")
                if (not number.isdecimal() or int(number) < 1 or not separator
                        or not unique or number in index or unique in seen):
                    raise ValueError("invalid or duplicate index entry")
                index[number] = unique
                seen.add(unique)
        except (OSError, ValueError) as exc:
            reraise_unresolved(exc)
            return failed(Outcome.INFRASTRUCTURE_FAILURE,
                          f"per-test coverage index could not be read: {exc}")
        nanos: Dict[str, int] = {}
        try:
            times_file = data / "times.tsv"
            for line in (times_file.read_text(encoding="utf-8").splitlines()
                         if times_file.exists() else ()):
                unique, separator, value = line.rpartition("\t")
                if not separator or not value.isdecimal() or unique in nanos:
                    raise ValueError("invalid or duplicate time entry")
                nanos[unique] = int(value)
        except (OSError, ValueError) as exc:
            reraise_unresolved(exc)
            return failed(Outcome.INFRASTRUCTURE_FAILURE,
                          f"per-test times could not be read: {exc}")
        if "suite" not in records:
            return failed(Outcome.INFRASTRUCTURE_FAILURE,
                          "JaCoCo did not report the requested target")
        if (run.counts.passed and not index) or set(records) - {"suite"} != set(index):
            return failed(Outcome.INFRASTRUCTURE_FAILURE,
                          "per-test coverage records are missing or do not match their index")
        recorded_tests = set()
        for unique in index.values():
            name = _junit.test_name(unique)
            if name is not None:
                recorded_tests.add(_junit.test_id(name, classes, suite_root))
        if any(test.outcome == "passed" and test.test not in recorded_tests
               for test in run.tests):
            return failed(Outcome.INFRASTRUCTURE_FAILURE,
                          "per-test coverage is missing for a reported passed test")
        relative = identity.target.path
        covered: Dict[str, Set[EvidenceItem]] = {}
        try:
            for number, entries in records.items():
                if number == "suite":
                    continue
                items: Set[EvidenceItem] = set()
                for entry in entries:
                    text, _, ordinals = entry.partition(":")
                    items.add(LineItem(relative, int(text)))
                    items.update(BranchItem(relative, int(text), int(k))
                                 for k in ordinals.split(",") if k)
                covered.setdefault(index.get(number, number), set()).update(items)
            coverage = _jacoco.observation(records.get("suite"), target,
                                           adapter.context.root, executor=executor)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            reraise_unresolved(exc)
            return failed(Outcome.INFRASTRUCTURE_FAILURE,
                          f"coverage observation could not be read: {exc}")
    from langadapter.java.adapter import _with_removed
    return _with_removed(_evidence(request, run, classes, suite_root,
                                   covered, coverage, identity, nanos), removed)


def _evidence(request: PerTestEvidenceRequest, run: Any,
              classes: Dict[str, Path], suite_root: Path, covered: Dict[str, Set[EvidenceItem]],
              coverage: Any, identity: ObservationIdentity,
              nanos: Dict[str, int]) -> PerTestEvidence:
    """The evidence per test method of the run, with the listener's ``nanos``."""
    functions: List[TestId] = []
    for outcome in run.tests:
        function = _function(outcome.test)
        if function not in functions:
            functions.append(function)
    items: Dict[TestId, Set[EvidenceItem]] = {t: set() for t in functions}
    unmatched: List[ContextEvidence] = []
    for unique, found in sorted(covered.items()):
        name = _junit.test_name(unique)
        test = (_junit.test_id(name, classes, suite_root, function=True)
                if name is not None else None)
        if test is None or test not in items:
            unmatched.append(ContextEvidence(unique, frozenset(found)))
            continue
        items[test].update(found)
    times: Dict[TestId, float] = {}
    for unique, elapsed in nanos.items():
        name = _junit.test_name(unique)
        test = (_junit.test_id(name, classes, suite_root, function=True)
                if name is not None else None)
        if test in items:
            times[test] = times.get(test, 0.0) + elapsed / 1e6
    kills, unmapped = KILLS_NOT_REQUESTED, ()
    mutation_run = request.mutation_run
    if mutation_run is not None:
        found_kills: Dict[TestId, Set[EvidenceItem]] = {}
        missing: List[str] = []
        for mutant in mutation_run.mutants:
            if mutant.status not in DETECTED_STATUSES or not mutant.killers:
                continue
            missing += list(mutant.unmatched_killers)
            for test in mutant.killing_tests:
                function = _function(test)
                if function in items:
                    found_kills.setdefault(function, set()).add(
                        KillItem(str(mutant.row.get("id"))))
                else:
                    missing.append(str(test))
        if missing or not found_kills:
            kills, unmapped = KILLS_UNUSABLE, tuple(dict.fromkeys(missing))
        else:
            kills = KILLS_USABLE
            for test, kill_items in found_kills.items():
                items[test].update(kill_items)
    evidence = [TestEvidence(t, frozenset(items[t]), times.get(t))
                for t in functions]
    known = [e.time_ms for e in evidence if e.time_ms is not None]
    return PerTestEvidence(
        outcome=Outcome.COMPLETED, reason="", tests=tuple(evidence),
        untimed=tuple(t for t in functions if t not in times),
        unmatched_contexts=tuple(c.context for c in unmatched),
        kills=kills, unmapped_killers=tuple(unmapped),
        suite_time_ms=sum(known) if known else None, coverage=coverage,
        identity=identity, unmatched_items=tuple(unmatched),
        outside_items=items_outside_tests(coverage, evidence, identity.target.path))


def _function(test: TestId) -> TestId:
    """The test method a (possibly parameterized) test belongs to."""
    if test.params is None:
        return test
    text = test.text
    if text.endswith(test.params):
        text = text[:-len(test.params)]
    return TestId(test.file, test.qualname, None, text)


# The tests still active when a suite's JVM ended, as a JUnit Platform
# listener records them.

JOURNAL_PROPERTY = "langadapter.journal"
_JOURNAL_LISTENER = """\
import java.nio.file.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import org.junit.platform.engine.TestExecutionResult;
import org.junit.platform.launcher.*;

public class LangAdapterJournal implements TestExecutionListener {
    private final Set<String> active = new TreeSet<>();
    private void write() {
        String name = System.getProperty("langadapter.journal");
        if (name == null) return;
        Path target = Path.of(name);
        Path temporary = target.resolveSibling(target.getFileName() + ".tmp");
        List<String> encoded = new ArrayList<>();
        for (String id : active) {
            encoded.add("\\"" + Base64.getEncoder().encodeToString(
                id.getBytes(StandardCharsets.UTF_8)) + "\\"");
        }
        try {
            Files.writeString(temporary, "{\\"active\\":[" + String.join(",", encoded) + "]}");
            try {
                Files.move(temporary, target, StandardCopyOption.ATOMIC_MOVE,
                           StandardCopyOption.REPLACE_EXISTING);
            } catch (AtomicMoveNotSupportedException e) {
                Files.move(temporary, target, StandardCopyOption.REPLACE_EXISTING);
            }
        } catch (Exception e) {
            throw new IllegalStateException("cannot write active-test journal", e);
        }
    }
    @Override public synchronized void testPlanExecutionStarted(TestPlan plan) {
        active.clear(); write();
    }
    @Override public synchronized void executionStarted(TestIdentifier test) {
        if (test.isTest()) { active.add(test.getUniqueId()); write(); }
    }
    @Override public synchronized void executionFinished(TestIdentifier test, TestExecutionResult result) {
        if (test.isTest()) { active.remove(test.getUniqueId()); write(); }
    }
}
"""


def prepare_journal(adapter, jdk, classpath, directory: Path, env) -> Compilation:
    try:
        require_mounted_output(str(directory), adapter._executor)
        directory.mkdir()
        done = compile_helper_source(
            JavaEnv(adapter._executor), jdk.tool("javac"), directory / "LangAdapterJournal.java", _JOURNAL_LISTENER,
            classpath, "journal listener compilation", cwd=adapter.context.root, env=env)
        if done.returncode:
            return Compilation(Outcome.INFRASTRUCTURE_FAILURE, diagnostics=done.stdout + done.stderr)
        services = directory / "META-INF/services"
        services.mkdir(parents=True)
        (services / "org.junit.platform.launcher.TestExecutionListener").write_text(
            "LangAdapterJournal\n", encoding="utf-8")
    except (OSError, subprocess.SubprocessError) as exc:
        reraise_unresolved(exc)
        return Compilation(Outcome.INFRASTRUCTURE_FAILURE, diagnostics=str(exc))
    return Compilation(Outcome.COMPLETED, str(directory))


@dataclass(frozen=True)
class JournalObservation:
    active: Optional[TestId] = None
    observed: bool = False
    issue: str = ""
    diagnostics: Tuple[RunDiagnostic, ...] = ()


def read_journal(path: Path, classes, suite_root: str) -> JournalObservation:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))["active"]
        if not isinstance(data, list) or not all(isinstance(item, str) for item in data):
            raise ValueError("active tests must be a list of encoded identifiers")
        names = [base64.b64decode(item, validate=True).decode("utf-8") for item in data]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        reraise_unresolved(exc)
        return JournalObservation(issue=f"active-test journal is unavailable: {exc}")
    diagnostics = tuple(RunDiagnostic("active_at_end", "test active at the last journal update",
                                      native_test=name) for name in names)
    active = None
    if len(names) == 1:
        name = _junit.test_name(names[0])
        if name is not None:
            active = _junit.test_id(name, classes, suite_root)
    return JournalObservation(active, True, diagnostics=diagnostics)


def apply_journal(run, observation: JournalObservation):
    diagnostics = run.diagnostics + observation.diagnostics
    if observation.issue:
        diagnostics += (RunDiagnostic("journal_unavailable", observation.issue),)
    run = replace(run, active_at_end=observation.active, diagnostics=diagnostics,
                  side_effects=replace(run.side_effects, timeout_journal=observation.observed))
    if observation.issue and run.outcome is Outcome.COMPLETED:
        return replace(run, outcome=Outcome.INFRASTRUCTURE_FAILURE, reason=observation.issue)
    if observation.diagnostics and run.outcome is Outcome.COMPLETED:
        return replace(run, outcome=Outcome.INFRASTRUCTURE_FAILURE,
                       reason="the JVM ended while tests were still active")
    return run
