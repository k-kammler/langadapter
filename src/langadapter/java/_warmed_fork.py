"""Independent warmed fork inputs from an idle, completed private build copy."""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, replace
import os
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List
import xml.etree.ElementTree as ET
from langadapter._owned_directory import OwnershipRefused
from langadapter.context import ContainerExecution, ProjectContext, LocalExecution
from langadapter.errors import InvalidRequest
from langadapter.execution import (
    Executor,
    container_path,
    executor_for,
    ContainerExecutor,
    LocalExecutor,
)
from langadapter.java._env import remaining, JavaEnv, require_mounted_output
from langadapter.preparation import prepare_project
import math
import subprocess
import sys
import time
from langadapter import _linux_container, _process_tree
from langadapter._linux_command import STARTUP_BOUND_S
from langadapter._execution_scope import operation, reraise_unresolved
from langadapter.java import (
    _warmed as _warmed_controller, _build as projects, _env as jdks, _tools as _junit, _tools,
)
from langadapter.java._build import tool_environment
from langadapter.java._profile import JvmProfile
from langadapter.outcomes import Outcome


# Selected preparation for an owned warmed operation. It has no public
# entry point.

@dataclass(frozen=True)
class PreparedSetup:
    project: projects.JavaProject
    jdk: jdks.Jdk
    tools: _junit.Toolset
    classes: str
    helper_classes: str
    class_names: tuple[str, ...]


@dataclass(frozen=True)
class SetupObservation:
    outcome: Outcome
    reason: str = ""
    output: str = ""
    prepared: PreparedSetup | None = None


def prepare_setup(context: ProjectContext, executor, request, directory: Path, *,
            total_deadline: float,
            class_names: Callable[[List[Path]], Dict[str, Path]]) -> SetupObservation:
    """Build once on the selected context and compile outside timed
    intervals, with the same project and executor code as the other Java
    operations. The caller supplies an owned writable project copy and
    scratch and keeps failed artifacts. Inputs are neither copied nor checked
    for isolation. ``class_names`` gives each test file's class name."""
    request.validate()
    if request.warmup_runs + request.runs > 2**31 - 1 or request.forks > 2**31 - 1:
        raise InvalidRequest("runs", "native Java iteration/fork counts exceed integer range")
    container = isinstance(context.execution, ContainerExecution)
    expected = ContainerExecutor if container else LocalExecutor
    if (not isinstance(context.execution, (ContainerExecution, LocalExecution))
            or not isinstance(executor, expected) or executor._context is not context):
        raise OwnershipRefused("Warmed setup requires its selected context-bound executor")
    context.validate()
    with operation(context):
        executor._check_context()
        if container and executor.helper_python is None:
            return SetupObservation(Outcome.MISSING_SETUP, "warmed timing in a container needs the "
                                    "helper Python (execution.helper_python)")
        if context.interpreter is None or context.interpreter.path is None:
            return SetupObservation(Outcome.MISSING_SETUP, "Warmed setup requires an explicitly selected JDK")
        require_mounted_output(str(directory), executor)
        try:
            remaining(total_deadline, None, "warmed setup admission")
            names = tuple(class_names([path for path in request.suite.files() if path.suffix == ".java"]))
            if not names:
                return SetupObservation(Outcome.COLLECTION_FAILED, "The declared suite has no Java classes to launch")
            if HELPER_CLASS in names:
                raise InvalidRequest("suite", "a suite class conflicts with the warmed helper")
            directory.mkdir()
            environment = context.environment
            selected = (jdks.read_in_container(context.interpreter.path, executor,
                        environment=environment, deadline=total_deadline) if container
                        else jdks.read(context.interpreter.path))
            remaining(total_deadline, None, "warmed JDK selection")
            if selected is None:
                return SetupObservation(Outcome.MISSING_SETUP, "the selected runtime is not a JDK")
            refusal = JvmProfile.preparation_refusal(selected.feature)
            if refusal:
                return SetupObservation(Outcome.MISSING_SETUP, refusal)
            resolution = projects.resolve(context.root, selected, executor,
                str(directory / "build"), environment=environment, deadline=total_deadline)
            if resolution.outcome is not Outcome.COMPLETED:
                return SetupObservation(resolution.outcome, resolution.reason, resolution.output)
            project = resolution.project
            if project is None:
                return SetupObservation(Outcome.INFRASTRUCTURE_FAILURE, "Native build returned no project model")
            tools = _junit.toolset(project.run_classpath(), _tools.host_environment(context),
                                   executor=executor,
                                   scratch=directory / "tools", deadline=total_deadline)
            refusal = JvmProfile.run_refusal(selected.feature, project.release_of_tests(), tools.platform)
            if refusal:
                return SetupObservation(Outcome.MISSING_SETUP, refusal)
            compiled = projects.compile_suite(project, selected, request.suite,
                str(directory / "classes"), executor, environment=environment,
                deadline=total_deadline)
            if compiled.outcome is not Outcome.COMPLETED:
                return SetupObservation(compiled.outcome, "The suite could not be compiled", compiled.diagnostics)
            source = directory / f"{HELPER_CLASS}.java"
            with source.open("x", encoding="utf-8") as stream:
                stream.write(HELPER_SOURCE)
            # The helper needs the launcher jars the run classpath leaves out.
            helper = projects.compile_tests(replace(project, test_release=JvmProfile.helper_release,
                                                    test_sources=()),
                selected, (str(source),), str(directory / "helper"), executor,
                environment=environment, deadline=total_deadline,
                classpath=(*tools.jars, *project.run_classpath()))
            if helper.outcome is not Outcome.COMPLETED:
                return SetupObservation(helper.outcome, "The warmed helper could not be compiled",
                    helper.diagnostics + "\nLauncher jars on the classpath: " + ", ".join(tools.jars))
            remaining(total_deadline, None, "warmed setup completion")
            if compiled.classes is None or helper.classes is None:
                return SetupObservation(Outcome.INFRASTRUCTURE_FAILURE, "Native compiler returned no class directory")
            return SetupObservation(Outcome.COMPLETED, output=resolution.output,
                prepared=PreparedSetup(project, selected, tools, compiled.classes, helper.classes, names))
        except InvalidRequest:
            raise
        except ImportError as error:
            return SetupObservation(Outcome.MISSING_SETUP, f"Java source discovery is unavailable: {error}")
        except _tools.ToolUnavailable as error:
            reraise_unresolved(error)
            return SetupObservation(Outcome.MISSING_SETUP, str(error))
        except subprocess.TimeoutExpired as error:
            reraise_unresolved(error)
            return SetupObservation(Outcome.TIMED_OUT, str(error))
        except (OSError, subprocess.SubprocessError, ET.ParseError, ValueError) as error:
            reraise_unresolved(error)
            return SetupObservation(Outcome.INFRASTRUCTURE_FAILURE, str(error))


@dataclass(frozen=True)
class ForkSetup:
    context: ProjectContext
    executor: Executor
    prepared: PreparedSetup


class PreparedFork:
    """The fork's prepared project copy (``owner``). Commands run only in ``use``."""

    def __init__(self, owner, context, prepared, built_root, deadline):
        self.owner = owner
        self._context = context
        self._prepared = prepared
        self._built_root = built_root
        self._deadline = deadline

    @contextlib.contextmanager
    def use(self):
        remaining(self._deadline, None, "warmed fork input admission")
        with self.owner.use() as context:
            remaining(self._deadline, None, "warmed fork input restoration")
            model = _mapped(self._context, self._prepared, self._built_root,
                            context, self.owner._prepared_copy.root)
            yield ForkSetup(context, executor_for(context), model)

    def close(self):
        self.owner.close()


def _mapper(context, built_root, target_context, target_root, *, host=False):
    container = isinstance(context.execution, ContainerExecution)
    if container and not host:
        original, target = PurePosixPath(context.root), PurePosixPath(target_context.root)
    else:
        original, target = Path(built_root), Path(target_root)

    def path(value, *, required=False):
        selected = type(original)(value)
        if selected.is_absolute() and ".." not in selected.parts:
            try:
                relative = selected.relative_to(original)
            except ValueError:
                pass
            else:
                return str(target / relative)
        if required:
            raise InvalidRequest("project", f"warmed writable input lies outside the built project: {value}")
        return value

    return path


def _mapped(context, prepared, built_root, target_context, target_root):
    path = _mapper(context, built_root, target_context, target_root)
    host_path = _mapper(context, built_root, target_context, target_root, host=True)
    project = prepared.project
    original = (context.root, str(built_root))
    for value in (*project.test_jvm_args, *project.test_system_properties.values(),
                  *context.interpreter_flags):
        normalized = value.replace("\\", "/")
        if any((root.replace("\\", "/").casefold() if os.name == "nt"
                and not isinstance(context.execution, ContainerExecution) else root.replace("\\", "/"))
               in (normalized.casefold() if os.name == "nt"
                   and not isinstance(context.execution, ContainerExecution) else normalized)
               for root in original):
            raise InvalidRequest("jvm_args", "a JVM setting refers to the build copy; use a project-relative setting")
    model = replace(project, root=target_context.root,
        main_sources=tuple(path(item, required=True) for item in project.main_sources),
        test_sources=tuple(path(item, required=True) for item in project.test_sources),
        main_classes=tuple(path(item, required=True) for item in project.main_classes),
        test_classes=tuple(path(item, required=True) for item in project.test_classes),
        test_classpath=tuple(path(item) for item in project.test_classpath))
    tools = replace(prepared.tools, jars=tuple(path(item) for item in prepared.tools.jars),
                    overrides=tuple(path(item) for item in prepared.tools.overrides))
    return replace(prepared, project=model, tools=tools,
                   classes=host_path(prepared.classes, required=True),
                   helper_classes=host_path(prepared.helper_classes, required=True))


def prepare(context, prepared: PreparedSetup, *, built_root: Path,
            total_deadline: float) -> PreparedFork:
    """The fork's copy of the completed build's bytes, made without
    restarting the build while the caller keeps its owner alive and idle.
    Typed project paths map to the copy. A JVM argument, system property or
    interpreter flag naming the build copy's path is refused, not rewritten."""
    context.validate()
    context._require_execution()
    if context._prepared_copy_binding is not None:
        raise OwnershipRefused("Warmed fork inputs require an idle unbound private build copy")
    if not isinstance(prepared, PreparedSetup) or prepared.project.root != context.root:
        raise InvalidRequest("project", "prepared build differs from its selected context")
    built_root = Path(built_root).absolute()
    expected = (container_path(context.execution, str(built_root))
                if isinstance(context.execution, ContainerExecution) else str(built_root))
    if expected != context.root:
        raise InvalidRequest("built_root", "host build root differs from the selected executing root")
    # Validate all typed mappings and arbitrary settings before allocating a copy.
    _mapped(context, prepared, built_root, context, built_root)
    remaining(total_deadline, None, "warmed fork copy admission")
    owner = prepare_project(context)
    try:
        remaining(total_deadline, None, "warmed fork copy completion")
        return PreparedFork(owner, context, prepared, built_root, total_deadline)
    except BaseException as error:
        error._langadapter_prepared_project = owner
        raise


# The source of the JUnit Platform Launcher helper, for every supported
# platform version. The controller wiring is separate.

HELPER_CLASS = "LangAdapterWarmed"

HELPER_SOURCE = r'''
import java.io.PrintWriter;
import java.io.StringWriter;
import java.lang.management.ManagementFactory;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.junit.platform.engine.TestExecutionResult;
import org.junit.platform.engine.discovery.DiscoverySelectors;
import org.junit.platform.launcher.Launcher;
import org.junit.platform.launcher.TestExecutionListener;
import org.junit.platform.launcher.TestIdentifier;
import org.junit.platform.launcher.TestPlan;
import org.junit.platform.launcher.core.LauncherDiscoveryRequestBuilder;
import org.junit.platform.launcher.core.LauncherFactory;

public final class LangAdapterWarmed {
    private final Path directory;
    private final String token;
    private final int fork;
    private final int warmups;
    private final int runs;
    private final String jvmName = ManagementFactory.getRuntimeMXBean().getName();
    private final long jvmStart = ManagementFactory.getRuntimeMXBean().getStartTime();

    private LangAdapterWarmed(String[] args) {
        directory = Paths.get(args[0]);
        token = args[1];
        fork = Integer.parseInt(args[2]);
        warmups = Integer.parseInt(args[3]);
        runs = Integer.parseInt(args[4]);
        if (!token.matches("[0-9a-f]{32}") || fork < 0 || warmups < 1 || runs < 1) {
            throw new IllegalArgumentException("Invalid native fork admission");
        }
        Math.addExact(warmups, runs);
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 6) {
            throw new IllegalArgumentException("A channel and explicit suite classes are required");
        }
        int status = 0;
        try {
            new LangAdapterWarmed(args).run(args);
        } catch (Throwable error) {
            error.printStackTrace();
            status = 1;
        }
        // Every record is published, or the error printed. A thread a test
        // left running, or a shutdown hook it registered, must not hold the
        // JVM: the controller waits for its exit.
        System.out.flush();
        System.err.flush();
        Runtime.getRuntime().halt(status);
    }

    private Map<String, Object> event(String type) {
        Map<String, Object> value = new LinkedHashMap<String, Object>();
        value.put("schema", 1);
        value.put("token", token);
        value.put("fork", fork);
        value.put("event", type);
        value.put("jvm_name", jvmName);
        value.put("jvm_start_ms", jvmStart);
        return value;
    }

    private Map<String, Object> iterationEvent(String type, int index) {
        Map<String, Object> value = event(type);
        value.put("index", index);
        value.put("kind", index < warmups ? "warmup" : "measurement");
        value.put("repetition", index < warmups ? index : index - warmups);
        return value;
    }

    private void run(String[] args) throws Exception {
        Launcher launcher = LauncherFactory.create();
        Map<String, Object> ready = event("ready");
        ready.put("warmup_runs", warmups);
        ready.put("runs", runs);
        publish("ready.json", ready);
        String outcome = "completed";
        int completed = 0;
        for (int index = 0; index < warmups + runs; index++) {
            awaitPermit(index);
            publish("start-" + index + ".json", iterationEvent("start", index));
            Observation observation = new Observation();
            Long elapsed = null;
            List<String> diagnostics = new ArrayList<String>();
            boolean complete = false;
            String reason = "";
            try {
                LauncherDiscoveryRequestBuilder request = LauncherDiscoveryRequestBuilder.request();
                for (int i = 5; i < args.length; i++) {
                    request.selectors(DiscoverySelectors.selectClass(args[i]));
                }
                // One test at a time, as every other Java run.
                request.configurationParameter("junit.jupiter.execution.parallel.enabled", "false");
                // A consumed TestPlan is never reused; only the launcher/JVM survive.
                TestPlan plan = launcher.discover(request.build());
                observation.discovered(plan);
                long start = System.nanoTime();
                try {
                    launcher.execute(plan, observation);
                } finally {
                    elapsed = System.nanoTime() - start;
                }
                complete = observation.planFinished && observation.issue.isEmpty();
                if (!complete) {
                    outcome = "infrastructure_failure";
                    reason = observation.issue.isEmpty() ? "the test plan did not finish"
                                                        : observation.issue;
                } else if (observation.failedTests() > 0 || observation.failedContainers() > 0) {
                    outcome = "test_failures";
                    reason = "a test or a test container failed";
                } else if (observation.passedTests() == 0) {
                    outcome = "no_tests";
                    reason = "no test passed";
                }
            } catch (Throwable error) {
                outcome = elapsed == null ? "collection_failed" : "infrastructure_failure";
                reason = error.toString();
                StringWriter detail = new StringWriter();
                error.printStackTrace(new PrintWriter(detail));
                diagnostics.add(detail.toString());
            }
            Map<String, Object> record = iterationEvent("iteration", index);
            record.put("elapsed_ns", elapsed);
            record.put("outcome", outcome);
            record.put("report_complete", complete);
            record.put("counts", observation.counts());
            record.put("tests", observation.tests());
            record.put("reason", reason);
            record.put("diagnostics", diagnostics);
            publish("iteration-" + index + ".json", record);
            if (complete) {
                completed++;
            }
            if (!outcome.equals("completed")) {
                break;
            }
        }
        Map<String, Object> terminal = event("complete");
        terminal.put("outcome", outcome);
        terminal.put("iterations_completed", completed);
        publish("complete.json", terminal);
    }

    private void awaitPermit(int index) throws Exception {
        Path permit = directory.resolve("run-" + index + ".permit");
        while (!Files.exists(permit)) {
            Thread.sleep(10);
        }
        String received = new String(readHeld(permit), StandardCharsets.UTF_8);
        if (!received.equals(token)) {
            throw new IllegalStateException("Iteration permit belongs to another channel");
        }
    }

    // How long a file just renamed into place is read again while it is held
    // (on Windows a scanner may hold it briefly), as the controller reads its
    // reports (_DENIED_SECONDS in _warmed.py).
    private static final long DENIED_MILLIS = 1000L;

    private static byte[] readHeld(Path path) throws Exception {
        long first = System.nanoTime();
        while (true) {
            try {
                return Files.readAllBytes(path);
            } catch (java.nio.file.AccessDeniedException held) {
                if ((System.nanoTime() - first) / 1_000_000L >= DENIED_MILLIS) {
                    throw held;
                }
                Thread.sleep(10);
            }
        }
    }

    private void publish(String name, Map<String, Object> value) throws Exception {
        Path temporary = directory.resolve("." + name + ".tmp");
        Path destination = directory.resolve(name);
        // Serialization and transport are outside the measured execute interval.
        Files.write(temporary, json(value).getBytes(StandardCharsets.UTF_8));
        try {
            Files.move(temporary, destination, StandardCopyOption.ATOMIC_MOVE);
        } catch (AtomicMoveNotSupportedException unsupported) {
            Files.move(temporary, destination);
        }
    }

    private static final class Observation implements TestExecutionListener {
        private final Map<String, Map<String, Object>> observed =
            new LinkedHashMap<String, Map<String, Object>>();
        private final Set<String> initialTests = new HashSet<String>();
        private final Set<String> dynamicTests = new HashSet<String>();
        private final Set<String> startedTests = new HashSet<String>();
        private boolean discoveryComplete = false;
        private volatile boolean planFinished = false;
        private volatile String issue = "";

        private synchronized void discovered(TestPlan plan) {
            for (TestIdentifier root : plan.getRoots()) {
                register(root);
                for (TestIdentifier identifier : plan.getDescendants(root)) {
                    register(identifier);
                    if (identifier.isTest()) {
                        initialTests.add(identifier.getUniqueId());
                    }
                }
                if (root.isTest()) {
                    initialTests.add(root.getUniqueId());
                }
            }
            discoveryComplete = true;
        }

        private void register(TestIdentifier identifier) {
            if (identifier.isTest() == identifier.isContainer()) {
                issue = "Native identifier is not exclusively a test or a container";
            }
            if (!observed.containsKey(identifier.getUniqueId())) {
                Map<String, Object> test = new LinkedHashMap<String, Object>();
                test.put("unique_id", identifier.getUniqueId());
                test.put("display_name", identifier.getDisplayName());
                test.put("kind", identifier.isTest() ? "test" : "container");
                test.put("status", "discovered");
                test.put("reason", "");
                observed.put(identifier.getUniqueId(), test);
            }
        }

        public synchronized void dynamicTestRegistered(TestIdentifier identifier) {
            register(identifier);
            if (identifier.isTest()) {
                dynamicTests.add(identifier.getUniqueId());
            }
        }

        public synchronized void executionStarted(TestIdentifier identifier) {
            register(identifier);
            if (identifier.isTest()) {
                startedTests.add(identifier.getUniqueId());
            }
        }

        public synchronized void executionSkipped(TestIdentifier identifier, String reason) {
            register(identifier);
            observed.get(identifier.getUniqueId()).put("status", "skipped");
            observed.get(identifier.getUniqueId()).put("reason", reason);
        }

        public synchronized void executionFinished(TestIdentifier identifier, TestExecutionResult result) {
            register(identifier);
            observed.get(identifier.getUniqueId()).put("status", result.getStatus().name().toLowerCase(
                java.util.Locale.ROOT));
            observed.get(identifier.getUniqueId()).put("reason", result.getThrowable().isPresent()
                ? result.getThrowable().get().toString() : "");
        }

        public synchronized void testPlanExecutionFinished(TestPlan plan) {
            planFinished = true;
        }

        private int count(String kind, String status) {
            int value = 0;
            for (Map<String, Object> test : observed.values()) {
                if (kind.equals(test.get("kind")) && status.equals(test.get("status"))) {
                    value++;
                }
            }
            return value;
        }

        private synchronized int passedTests() { return count("test", "successful"); }
        private synchronized int failedTests() { return count("test", "failed"); }
        private synchronized int failedContainers() { return count("container", "failed"); }

        private synchronized List<Map<String, Object>> tests() {
            return new ArrayList<Map<String, Object>>(observed.values());
        }

        private synchronized Map<String, Object> counts() {
            Map<String, Object> values = new LinkedHashMap<String, Object>();
            Set<String> discovered = new HashSet<String>(initialTests);
            discovered.addAll(dynamicTests);
            values.put("discovered", discoveryComplete ? discovered.size() : null);
            values.put("started", discoveryComplete ? startedTests.size() : null);
            values.put("passed", discoveryComplete ? count("test", "successful") : null);
            values.put("skipped", discoveryComplete ? count("test", "skipped") : null);
            values.put("aborted", discoveryComplete ? count("test", "aborted") : null);
            values.put("failed", discoveryComplete ? count("test", "failed") : null);
            values.put("container_failures", discoveryComplete ? count("container", "failed") : null);
            return values;
        }
    }

    private static String json(Object value) {
        if (value == null) { return "null"; }
        if (value instanceof Number || value instanceof Boolean) { return value.toString(); }
        if (value instanceof String) {
            String text = (String) value;
            StringBuilder escaped = new StringBuilder("\"");
            for (int index = 0; index < text.length(); index++) {
                char character = text.charAt(index);
                if (character == '"' || character == '\\') {
                    escaped.append('\\').append(character);
                } else if (character < 32 || Character.isSurrogate(character)) {
                    escaped.append(String.format(java.util.Locale.ROOT, "\\u%04x", (int) character));
                } else {
                    escaped.append(character);
                }
            }
            return escaped.append('"').toString();
        }
        StringBuilder result = new StringBuilder();
        if (value instanceof Map) {
            result.append('{');
            for (Map.Entry<?, ?> item : ((Map<?, ?>) value).entrySet()) {
                if (result.length() > 1) { result.append(','); }
                result.append(json(item.getKey().toString())).append(':').append(json(item.getValue()));
            }
            return result.append('}').toString();
        }
        if (value instanceof List) {
            result.append('[');
            for (Object item : (List<?>) value) {
                if (result.length() > 1) { result.append(','); }
                result.append(json(item));
            }
            return result.append(']').toString();
        }
        throw new IllegalArgumentException("Unsupported native report value");
    }
}
'''


# The launch of one prepared fork. Project setup and the assembly of the
# public result are separate.

def _remaining(deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise subprocess.TimeoutExpired("warmed fork admission", 0)
    return remaining


def execute(context, executor, project, jdk, tools, *, request, protocol,
            classes: str, helper_classes: str, class_names, channel: Path,
            total_deadline: float, on_event=None,
            processor_prefix=()) -> _warmed_controller.ForkExecution:
    """Launch one prepared fork through the selected command owner. The
    caller prepares the channel and inputs, compiles the suite, serializes
    and compares forks and keeps the evidence. Native output goes to
    stdout.log and stderr.log in the channel while the controller polls
    reports. ``processor_prefix``, required with ``cpus``, is the taskset
    command (by its address) the caller checked the affinity with."""
    request.validate()
    if request.cpus is not None and not processor_prefix:
        raise InvalidRequest("processor_prefix", "a processor set requires the checked taskset prefix")
    if (protocol.warmup_runs, protocol.runs) != (request.warmup_runs, request.runs):
        raise InvalidRequest("protocol", "iteration budgets differ from the request")
    if protocol.fork >= request.forks:
        raise InvalidRequest("protocol", "fork exceeds the request")
    try:
        finite_deadline = (not isinstance(total_deadline, bool)
                           and isinstance(total_deadline, (int, float))
                           and math.isfinite(total_deadline))
    except OverflowError:
        finite_deadline = False
    if not finite_deadline:
        raise InvalidRequest("total_deadline", "requires the finite operation deadline")
    if isinstance(class_names, (str, bytes)):
        raise InvalidRequest("class_names", "requires a sequence of native class names")
    names = tuple(class_names)
    if not names or any(not isinstance(name, str) or not name for name in names):
        raise InvalidRequest("class_names", "explicit nonempty native class names are required")
    if HELPER_CLASS in names:
        raise InvalidRequest("class_names", "a suite class conflicts with the warmed helper")
    container = isinstance(context.execution, ContainerExecution)
    expected_executor = ContainerExecutor if container else LocalExecutor
    if (not isinstance(context.execution, (LocalExecution, ContainerExecution))
            or not isinstance(executor, expected_executor) or executor._context is not context):
        raise OwnershipRefused("Warmed fork requires its selected context-bound executor")
    files = JavaEnv(executor)
    if files.path(project.root) != files.path(context.root):
        raise InvalidRequest("project", "resolved project differs from the selected fork root")
    require_mounted_output(str(channel), executor)
    for path in (classes, helper_classes):
        require_mounted_output(path, executor)
    if not channel.is_dir():
        raise InvalidRequest("channel", "requires an existing fork-owned channel")

    with operation(context):
        executor._check_context()
        if container and executor.helper_python is None:
            raise InvalidRequest("helper_python", "a warmed launch in the container needs the "
                                 "helper Python")
        remaining = _remaining(total_deadline)
        startup_deadline = min(total_deadline, time.monotonic() + request.startup_timeout_s)
        translate = executor._to_container if container else str
        arguments = JavaEnv(executor).java_arguments(
            (helper_classes, classes, *tools.jars, *project.run_classpath()),
            HELPER_CLASS, translate(str(channel)), protocol.token,
            str(protocol.fork), str(protocol.warmup_runs), str(protocol.runs), *names,
            jvm_args=(*project.test_jvm_args, *context.interpreter_flags),
            properties=project.test_system_properties)
        arguments_path = channel / "launch.args"
        stdout_path, stderr_path = channel / "stdout.log", channel / "stderr.log"
        # Never overwrite earlier raw fork evidence, even after a confirmed stop.
        with arguments_path.open("x", encoding="utf-8"):
            pass
        _junit.argfile(arguments_path, arguments)
        command = [jdk.tool("java"), f"@{translate(str(arguments_path))}"]
        if request.cpus is not None:
            command = [*processor_prefix, *command]
        environment = tool_environment(jdk, context.environment, executor=executor)
        with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
            remaining = min(remaining, _remaining(total_deadline),
                            _remaining(startup_deadline))
            if container:
                owner = _linux_container.start(
                    executor, command, helper_python=executor.helper_python,
                    cwd=project.root, env=environment,
                    startup=min(STARTUP_BOUND_S, remaining), stdout=stdout, stderr=stderr)
            else:
                options = ({"startup": min(STARTUP_BOUND_S, remaining)}
                           if sys.platform == "linux" else {})
                owner = _process_tree.start(command, cwd=project.root, env=environment,
                                             stdout=stdout, stderr=stderr, **options)
            return _warmed_controller.drive(
                owner, channel, protocol, total_deadline=total_deadline,
                startup_deadline=startup_deadline,
                iteration_timeout_s=request.iteration_timeout_s, on_event=on_event)
