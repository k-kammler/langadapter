"""Benchmarks of the Java adapter: the suite compiled once, then run
repeatedly by the JUnit Platform's console launcher, timed from outside:
discarded warm-up repetitions, then the measured ones, under one hold of
the process's benchmark lock when serialized. Each repetition starts its own
JVM, on a requested processor set through ``taskset``.
"""
from __future__ import annotations

from langadapter._execution_scope import reraise_unresolved
from langadapter._environment import launch_environment, selected_executable
from contextlib import ExitStack
from dataclasses import replace
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from typing import Any, List, Optional, Tuple
from langadapter import _process_tree, placement, resources
from langadapter.execution import ContainerExecutor, Executor, _retain_scratch
from langadapter.java import _tools as _junit, _tools
from langadapter.java._env import require_mounted_output
from langadapter.java._build import compile_suite, tool_environment
from langadapter.outcomes import Outcome
from langadapter.runs import Benchmark, BenchmarkRequest, _report_progress


#: What a repetition's time includes.
INCLUDES = ("the console launcher's JVM on this machine: its start, the "
            "discovery of the suite's classes and the tests")
CONTAINER_INCLUDES = ("the selected guest's console-launcher JVM: its start, the "
                  "discovery of the suite's classes and the tests, including "
                  "the host-to-guest execution wrapper")


def benchmark(adapter: Any, request: BenchmarkRequest) -> Benchmark:
    """The ``Benchmark`` of ``request`` for ``adapter`` (a ``JavaAdapter``)."""
    request.validate()
    issue = adapter._request_issue(request)
    files = adapter._suite_files(request.suite)
    identity = adapter._observation_identity(request.suite)
    container = isinstance(adapter._executor, ContainerExecutor)
    allowed: Optional[Tuple[int, ...]] = None
    returncode: Optional[int] = None
    scope = None
    removed: Tuple[str, ...] = ()

    def result(outcome: Outcome, reason: str, times: List[float]) -> Benchmark:
        if scope is not None and outcome in (Outcome.INFRASTRUCTURE_FAILURE, Outcome.TIMED_OUT):
            _retain_scratch(scope, f"benchmark execution failed: {reason}")
        return adapter._with_removed(Benchmark(
            outcome=outcome, reason=reason, times_ms=tuple(times),
            warmup_runs=request.warmup_runs,
            cpus=request.cpus,
            includes=CONTAINER_INCLUDES if container else INCLUDES,
            identity=identity, allowed_cpus=allowed,
            returncode=returncode), removed)

    if issue:
        return result(Outcome.UNSUPPORTED, issue, [])
    setup_issue = adapter.execution_setup_issue()
    if setup_issue:
        return result(Outcome.MISSING_SETUP, setup_issue, [])
    resolution = adapter.project()
    if resolution.outcome is not Outcome.COMPLETED:
        return result(resolution.outcome, resolution.reason, [])
    project, jdk = resolution.project, adapter._jdk()
    environment = adapter.context.environment
    env = tool_environment(jdk, environment, executor=adapter._executor)
    prefix = []
    times: List[float] = []
    if not container and request.cpus is not None:
        selected = selected_executable("taskset", env)
        if selected is None:
            return result(Outcome.UNSUPPORTED, "a processor set needs taskset, "
                          "which the selected environment does not have", [])
        prefix = [selected, "-c", request.cpus]
    elif container and request.cpus is not None:
        selected, outcome, reason = _container_taskset(adapter._executor, project.root, env)
        if selected is None:
            return result(outcome, reason, [])
        prefix = [selected, "-c", request.cpus]
    with ExitStack() as stack:
        try:
            allocated = stack.enter_context(adapter._scratch("bench"))
            scratch = Path(allocated)
            require_mounted_output(str(scratch), adapter._executor)
            scope = allocated
            compiled = compile_suite(project, jdk, request.suite,
                                     str(scratch / "classes"), adapter._executor,
                                     environment=environment)
        except (OSError, subprocess.SubprocessError) as exc:
            reraise_unresolved(exc)
            return result(Outcome.INFRASTRUCTURE_FAILURE,
                          f"benchmark preparation failed: {exc}", [])
        if compiled.outcome is not Outcome.COMPLETED:
            return result(compiled.outcome, "the suite does not compile: "
                          + compiled.diagnostics[-1000:], [])
        try:
            tools = _junit.toolset(project.run_classpath(), _tools.host_environment(adapter.context),
                                   executor=adapter._executor, scratch=scratch / "tools")
        except _tools.ToolUnavailable as exc:
            reraise_unresolved(exc)
            return result(Outcome.MISSING_SETUP, str(exc), [])
        except (OSError, subprocess.SubprocessError) as exc:
            reraise_unresolved(exc)
            return result(Outcome.INFRASTRUCTURE_FAILURE,
                          f"the launcher tools could not be prepared: {exc}", [])
        identity = replace(identity, toolchain=adapter.toolchain_identity(
            tools.platform, tools.junit4))
        try:
            classes = adapter._class_names(files)
        except OSError as exc:
            reraise_unresolved(exc)
            return result(Outcome.INFRASTRUCTURE_FAILURE,
                          f"the test classes could not be read: {exc}", [])
        allowed = (_container_allowed_cpus(adapter._executor, prefix, project.root, env)
                   if container else _allowed_cpus(prefix, project.root, env))
        removed = adapter._remove_created(request)
        with placement.serialized(request.serialize):
            for index in range(request.warmup_runs + request.runs):
                returncode = None
                reports = scratch / f"reports-{index}"
                try:
                    arguments = _junit.arguments(
                        [compiled.classes, *tools.jars, *project.run_classpath()], list(classes),
                        reports, jvm_args=[*project.test_jvm_args,
                                           *adapter.context.interpreter_flags],
                        system_properties=project.test_system_properties,
                        executor=adapter._executor)
                    argfile = _junit.argfile(scratch / "bench.args", arguments)
                except OSError as exc:
                    reraise_unresolved(exc)
                    return result(Outcome.INFRASTRUCTURE_FAILURE,
                                  f"the launcher arguments could not be prepared: {exc}", times)
                command_file = adapter._executor._to_container(str(argfile)) if container else str(argfile)
                command = [*prefix, jdk.tool("java"), f"@{command_file}"]
                start = time.perf_counter()
                try:
                    run = adapter._executor.run if container else placement.run_timed
                    done = run(command, env=env, cwd=project.root, timeout=request.timeout_s)
                except subprocess.TimeoutExpired as exc:
                    reraise_unresolved(exc)
                    # The stopped JVM's files are the next run's to remove.
                    adapter._record_created(request)
                    return result(Outcome.TIMED_OUT,
                                  f"a repetition reached the timeout of "
                                  f"{request.timeout_s:g} s", times)
                except (OSError, subprocess.SubprocessError) as exc:
                    reraise_unresolved(exc)
                    return result(Outcome.INFRASTRUCTURE_FAILURE,
                                  f"the launcher could not start: {exc}", times)
                elapsed = (time.perf_counter() - start) * 1000
                if index == request.warmup_runs + request.runs - 1:
                    adapter._record_created(request)
                native = getattr(done, "native_elapsed_s", None)
                if native is not None:
                    # On Linux the supervisor times the launcher, excluding itself.
                    elapsed = native * 1000
                returncode = done.returncode
                _report_progress(request, index, elapsed)
                try:
                    launch = _junit.Launch(returncode, done.stdout + done.stderr,
                                           tuple(sorted(reports.glob("TEST-*.xml"))))
                    cases = _junit.read_reports(launch.reports)
                except (OSError, ET.ParseError, ValueError) as exc:
                    reraise_unresolved(exc)
                    adapter._record_created(request)
                    return result(Outcome.INFRASTRUCTURE_FAILURE,
                                  f"the launcher report could not be read: {exc}", times)
                run = adapter._suite_run(launch, cases, classes, request.suite.root,
                                 elapsed / 1000, identity)
                if run.outcome is not Outcome.COMPLETED:
                    adapter._record_created(request)
                    return result(run.outcome, run.reason or run.output[-800:], times)
                if index >= request.warmup_runs:
                    times.append(elapsed)
    return result(Outcome.COMPLETED, "", times)


def _allowed_cpus(prefix: List[str], cwd: str, env=None) -> Optional[Tuple[int, ...]]:
    """The processors a process started with ``prefix`` may run on, read
    by one short probe. None where the platform reports no affinity."""
    source = Path(resources.__file__).read_text(encoding="utf-8")
    try:
        done = _process_tree.run([*prefix, sys.executable, "-I", "-S", "-", "allowed"],
                              cwd=cwd, input=source,
                              text=True, encoding="utf-8", errors="replace",
                              timeout=60, **({"env": launch_environment(env)} if env is not None else {}))
    except (OSError, subprocess.SubprocessError) as exc:
        reraise_unresolved(exc)
        return None
    return resources._probed_allowed_cpus(done)


def _container_taskset(executor: Executor, cwd: str, env) -> Tuple[Optional[str], Outcome, str]:
    """The selected container's taskset, by its absolute address, or None with
    the outcome and the reason that refuse a processor set."""
    try:
        available = executor.run(["sh", "-c", "command -v taskset"], cwd=cwd, env=env, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        reraise_unresolved(exc)
        return (None, Outcome.INFRASTRUCTURE_FAILURE,
                f"the container's processor-set tool could not be probed: {exc}")
    selected = (available.stdout or "").strip()
    if available.returncode != 0 or not selected:
        return (None, Outcome.UNSUPPORTED,
                "a processor set needs taskset, which the selected container does not have")
    address = PurePosixPath(selected)
    if not address.is_absolute() or ".." in address.parts or "\n" in selected or "\r" in selected:
        return (None, Outcome.INFRASTRUCTURE_FAILURE,
                "the container's processor-set probe returned no absolute tool address")
    return str(address), Outcome.COMPLETED, ""


def _container_allowed_cpus(executor: Executor, prefix: List[str], cwd: str,
                        env: dict[str, str]) -> Optional[Tuple[int, ...]]:
    """The selected Linux container's allowed processors (affinity, not use)."""
    try:
        done = executor.run([*prefix, "cat", "/proc/self/status"],
                            cwd=cwd, env=env, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        reraise_unresolved(exc)
        return None
    return resources._status_allowed_cpus(done)
