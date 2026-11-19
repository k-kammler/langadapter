"""The C adapter's setup check: what its operations need on this system.

For C tests that is the compiler with the gcov of the same release, the
modules of LangAdapter's extra for C, stdbuf for utest.h tests, prlimit when
``c_address_space_limit`` is set, and Mull with its clang for mutation. The
inspection starts the compiler and gcov to read their versions, finds the
other tools and runs nothing in the project. The tool check also starts each
tool it found once. Off Linux both read missing setup with the system's
refusal. Python tests of an extension have no C setup check yet."""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from typing import Callable, List, Optional

from langadapter import capabilities as _actions
from langadapter.capabilities import Availability
from langadapter.context import Interpreter
from langadapter.outcomes import Outcome
from langadapter.setup import ProjectSetupItem, ProjectSetupReport
from langadapter.c import _mull, _toolchain, _utest

#: The next step for a tool that is missing.
_INSTALL = {
    "runtime": "install GCC with its gcov, such as with sudo apt-get install gcc, or set "
               "c_compiler or CC",
    "stdbuf": "install coreutils, which has stdbuf",
    "prlimit": "install util-linux, which has prlimit",
    "mull": "install Mull and the clang of its LLVM release, as langadapter toolchains c does, "
            "or set c_mull",
}
_EXTRA = "install LangAdapter's extra for C: pip install \"langadapter[c]\""
_FOUND = "found, not yet tried"
_STARTS = "starts, not yet run on the project"


def assess(adapter, request, scope: str) -> ProjectSetupReport:
    """The setup report of ``request`` for ``scope``, ``inspection`` or
    ``verification``."""
    from langadapter.c.adapter import _linux_only

    required = any(operation.supported and operation.execution_backed
                   and operation.name in request.required_operations
                   for operation in adapter.operation_capabilities())
    if not sys.platform.startswith("linux"):
        action = "run it on Linux" + (", on Windows in WSL with Ubuntu 24.04"
                                      if sys.platform == "win32" else "")
        return _report(adapter, request, scope, [_missing("runtime", required, _linux_only(),
                                                          action)], _linux_only())
    if adapter._test_kind == "python":
        return adapter._unsupported_setup(request, scope)
    run = _runner(adapter, request)
    selected = (request.context.interpreter.path if request.context.interpreter is not None
                else _toolchain.find_compiler(adapter._setting("c_compiler"), adapter._environment()))
    found = _toolchain.probe(selected, run)
    if isinstance(found, _toolchain.ToolchainProblem):
        return _report(adapter, request, scope,
                       [_missing("runtime", required, found.reason, _INSTALL["runtime"])],
                       found.reason)
    items = _items(adapter, request, scope, required, found, run)
    context = (request.context if request.context.interpreter is not None
               else replace(request.context, interpreter=Interpreter(path=found.compiler)))
    reason = ("started the compiler and gcov to read their versions, and ran nothing in the project"
              if scope == "inspection" else
              "started each tool it found once, and ran nothing in the project")
    return _report(adapter, request, scope, items, reason, context)


def _items(adapter, request, scope: str, required: bool, found: "_toolchain.Toolchain",
           run) -> List[ProjectSetupItem]:
    """The items of a usable compiler ``found``: the compiler, the extra's
    modules, stdbuf for utest.h tests, prlimit for an address space limit
    and Mull, required when the request names a mutation operation."""
    from langadapter.c.adapter import _EXTRA_MODULES

    items = [ProjectSetupItem("runtime", required, Availability.AVAILABLE, Outcome.COMPLETED,
                              selected=found.compiler, version=found.version,
                              reason=f"GCC {found.version} and its gcov {found.gcov} answered")]
    items += [ProjectSetupItem(name, required, Availability.AVAILABLE, Outcome.COMPLETED,
                               reason="installed")
              if importlib.util.find_spec(module) is not None else
              _missing(name, required, f"{name} is not installed", _EXTRA)
              for module, name in _EXTRA_MODULES.items()]
    environment = adapter._environment()
    if adapter._test_kind == "c":
        items.append(_tool("stdbuf", required,
                           _toolchain.which(_utest.LINE_BUFFERED[0], environment), scope, run))
    if adapter._setting("c_address_space_limit"):
        items.append(_tool("prlimit", required, _toolchain.which("prlimit", environment), scope,
                           run))
    mutation = any(name.startswith("mutations.") for name in request.required_operations)
    items.append(_mull_item(adapter, mutation, scope, run))
    return items


def _runner(adapter, request) -> Callable:
    """Run a tool's command once, within the check's remaining time."""
    return lambda argv: adapter._run(argv, env=adapter._tool_environment(),
                                     timeout=request.remaining_timeout("a C tool's version"))


def _missing(key: str, required: bool, reason: str, action: str) -> ProjectSetupItem:
    return ProjectSetupItem(key, required, Availability.MISSING_SETUP, Outcome.MISSING_SETUP,
                            reason=reason, action=action)


def _tool(key: str, required: bool, path: Optional[str], scope: str, run) -> ProjectSetupItem:
    """A tool on the command's PATH: missing, found (inspection) or started
    once with --version (tool check)."""
    if path is None:
        return _missing(key, required, f"{key} is not on PATH", _INSTALL[key])
    if scope == "inspection":
        return ProjectSetupItem(key, required, None, Outcome.COMPLETED, selected=path,
                                reason=_FOUND, action=_actions.VERIFY, tried_by="verification")
    done = run([path, "--version"])
    if done.returncode != 0:
        return ProjectSetupItem(key, required, None, Outcome.INFRASTRUCTURE_FAILURE, selected=path,
                                reason=f"{key} did not start (exit {done.returncode})",
                                action=f"check that {path} --version runs")
    return ProjectSetupItem(key, required, Availability.AVAILABLE, Outcome.COMPLETED,
                            selected=path, reason=_STARTS)


def _mull_item(adapter, required: bool, scope: str, run) -> ProjectSetupItem:
    """Mull with its clang: located (inspection) or started (tool check)."""
    setting, environment = adapter._setting("c_mull"), adapter._environment()
    found = (_mull._located(setting, environment) if scope == "inspection"
             else _mull.probe(setting, run, environment))
    if isinstance(found, _mull.MullProblem):
        return _missing("mull", required, found.reason, _INSTALL["mull"])
    if scope == "inspection":
        return ProjectSetupItem("mull", required, None, Outcome.COMPLETED, selected=found[0],
                                reason=_FOUND, action=_actions.VERIFY, tried_by="verification")
    return ProjectSetupItem("mull", required, Availability.AVAILABLE, Outcome.COMPLETED,
                            selected=found.runner, version=found.version, reason=_STARTS)


def _report(adapter, request, scope: str, items: List[ProjectSetupItem], reason: str,
            context=None) -> ProjectSetupReport:
    """The report: completed with a context, else missing setup, or completed
    without one when the request needs no operation that runs a tool. Each
    such operation reads the first missing item it needs."""
    runtime = items[0]
    outcome = (Outcome.COMPLETED if context is not None or not runtime.required
               else Outcome.MISSING_SETUP)
    operations = []
    for operation in adapter.operation_capabilities():
        if not operation.supported or not operation.execution_backed:
            operations.append(operation)
            continue
        mutation = operation.name.startswith("mutations.")
        missing = next((item for item in items if item.readiness is Availability.MISSING_SETUP
                        and (item.key != "mull" or mutation)), None)
        if missing is not None:
            operations.append(replace(operation, readiness=Availability.MISSING_SETUP,
                                      reason=missing.reason, action=missing.action))
        elif scope == "inspection":
            operations.append(replace(operation, readiness=None,
                                      reason="not yet tried on this project",
                                      action=_actions.VERIFY, tried_by="verification"))
        else:
            operations.append(replace(operation, readiness=None,
                                      reason="its tools start, and the operation itself is not "
                                             "tried yet", action=_actions.RUN, tried_by="run"))
    return ProjectSetupReport(scope, outcome, reason,
                              context if context is not None or runtime.required
                              else request.context,
                              request.inputs, tuple(items), tuple(operations))
