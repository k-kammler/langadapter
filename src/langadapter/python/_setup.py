"""The Python adapter's setup checks. The inspection starts the selected
interpreter in isolation and reads its version and installed tools, without
importing the project or the tools. The verification imports the needed tools
in a prepared copy and, once they import and the requested operations need
pytest, collects the project's tests there with the copy's source directories
first and the origin check of a run that names no module, installing
nothing and running no tests."""
from __future__ import annotations

from dataclasses import replace
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from langadapter import _host, capabilities as _actions
from langadapter.capabilities import Availability
from langadapter._environment import captured, selected_executable
from langadapter.context import ContainerExecution, Interpreter, ProjectInputSelection
from langadapter.errors import InvalidRequest
from langadapter.execution import executor_for
from langadapter.outcomes import Outcome
from langadapter.setup import (
    ProjectSetupItem,
    ProjectSetupReport,
    ProjectSetupRequest,
    _closing,
    _add_missing_input_item,
    _preserve_owner,
)
from langadapter.python.project import (
    _discover_environments,
    _environment_addresses,
    environment_dir,
)
from langadapter.python._profile import SUPPORT, TOOL_MODULES, TOOL_NAMES, PythonProfile
from langadapter.python._provision import tool_pins
from langadapter.versions import python_version, setup_runtime
import os
import posixpath
from langadapter.preparation import prepare_project


_PROBE = """\
import json, os, platform, site, sys, sysconfig
from importlib import metadata
from pathlib import Path
prefix = sys.prefix
environment = include_base = False
executable = Path(os.path.abspath(sys.executable))
for home in (executable.parent, executable.parent.parent):
    config = home / 'pyvenv.cfg'
    if config.is_file():
        # site.py's prefix is the directory above the executable's, wherever the file is.
        prefix, environment, include_base = str(executable.parent.parent), True, True
        # As site.py reads it: the last such line decides, and only true includes.
        with open(config, encoding='utf-8') as lines:
            for line in lines:
                key, separator, value = line.partition('=')
                if separator and key.strip().lower() == 'include-system-site-packages':
                    include_base = value.strip().lower() == 'true'
        break
paths = []
if environment:
    # Debian's site lists an environment's own directory only after site has
    # run, which -S prevents. sysconfig names that directory on every system.
    scheme = ('venv' if 'venv' in sysconfig.get_scheme_names() else
              'nt' if os.name == 'nt' else 'posix_prefix')
    named = sysconfig.get_paths(scheme, vars={'base': prefix, 'platbase': prefix})
    paths += [named['purelib'], named['platlib']]
paths += site.getsitepackages([prefix])
if include_base and sys.base_prefix != prefix:
    paths += site.getsitepackages([sys.base_prefix])
if not environment or include_base:
    # Runs import without -s: site.py adds the user's site directory outside a
    # virtual environment and in one that includes the system's packages.
    paths.append(site.getusersitepackages())
paths = list(dict.fromkeys(paths))
tools = {name: None for name in TOOL_NAMES}
for name in tools:
    distribution = next(metadata.distributions(name=name, path=paths), None)
    tools[name] = None if distribution is None else distribution.version
print(json.dumps({'implementation': platform.python_implementation(),
                  'version': platform.python_version(), 'tools': tools}))
""".replace("TOOL_NAMES", repr(TOOL_NAMES))

# Names the version of every Python since 2.6, including those the probe above
# cannot run on (Python 2 has no -I, 3.7 and older no importlib.metadata).
_VERSION_PROBE = ("import platform, sys; print('%s %d.%d.%d' % "
                  "((platform.python_implementation(),) + tuple(sys.version_info[:3])))")


def _dependencies(operation, facets=()):
    """The tools an operation and its required facets use."""
    group, method = operation.name.split(".")
    if group == "tests":
        return ("pytest", "coverage") if method == "per_test_evidence" or "coverage" in facets else ("pytest",)
    if group == "mutations" and operation.execution_backed:
        return ("cosmic-ray",) if method == "operators" else ("pytest", "cosmic-ray")
    if group == "sources" and method == "prune_collection_breakers":
        return ("pytest",)
    return ()


def _install(names, version):
    """The installation step for tools missing from the selected environment,
    at the versions LangAdapter pins for its Python ``version``."""
    return f"install {' and '.join(tool_pins(version, names))} into the selected environment with pip"


def _operations(adapter, tools=None, runtime_missing=False, request=None, selection=None, refused=None,
                version=None):
    """Operation readiness. ``selection`` is the action of an interpreter
    choice still to be made, which holds back process-backed work. ``refused``
    is the runtime item of an unsupported project Python."""
    result = []
    for operation in adapter.operation_capabilities():
        if not operation.supported:
            result.append(operation)
            continue
        if operation.execution_backed and refused is not None:
            result.append(replace(operation, readiness=Availability.UNSUPPORTED,
                                  reason=refused.reason, action=refused.action))
            continue
        facets = () if request is None else request.facets(operation.name)
        missing = tuple(name for name in _dependencies(operation, facets)
                        if tools is not None and tools.get(name) is None)
        if operation.execution_backed and runtime_missing:
            result.append(replace(operation, readiness=Availability.MISSING_SETUP,
                                  reason="no usable interpreter is selected",
                                  action="select the project's interpreter"))
        elif operation.execution_backed and selection is not None:
            result.append(replace(operation, readiness=None,
                                  reason="waits for an interpreter to be selected", action=selection))
        elif operation.execution_backed and missing:
            result.append(replace(operation, readiness=Availability.MISSING_SETUP,
                                  reason="needs " + " and ".join(missing) + ", not installed in the selected environment",
                                  action=_install(missing, version)))
        else:
            # The tool check tries the tools of process-backed
            # operations. A run is the first to try in-process reads.
            result.append(replace(operation, readiness=None, reason="not yet tried on this project",
                                  action=_actions.VERIFY if operation.execution_backed else _actions.RUN,
                                  tried_by="verification" if operation.execution_backed else "run"))
    return tuple(result)


def _candidates(context):
    """Unattempted addresses, not a runtime choice or host existence claim."""
    return _environment_addresses(context.root, context.environment,
                                 posix=isinstance(context.execution, ContainerExecution))


def _no_runtime(adapter, request, required, reason, action, candidates):
    """The report without an interpreter: missing setup when the operations
    need one, else (sources only) completed with the selected context."""
    return ProjectSetupReport("inspection", Outcome.MISSING_SETUP if required else Outcome.COMPLETED,
        reason, None if required else request.context, request.inputs,
        (ProjectSetupItem("runtime", required, Availability.MISSING_SETUP, Outcome.MISSING_SETUP,
                          reason=reason, action=action, candidates=candidates),),
        _operations(adapter, runtime_missing=True))


def inspect(adapter, request):
    if not isinstance(request, ProjectSetupRequest):
        raise InvalidRequest("request", "must be a ProjectSetupRequest")
    context = request.context
    static = adapter.operation_capabilities()
    runtime_required = any(item.execution_backed for item in static if item.name in request.required_operations)
    runtime = context.interpreter
    discovered = None
    if runtime is None and not isinstance(context.execution, ContainerExecution):
        # Without a selection: the project's .venv or the captured activated
        # environment, never the library's own interpreter. One is selected,
        # several need a choice, and the caller's own environment outside the
        # project is not chosen: beside another it is left aside, alone it is
        # refused (as in discover_runtime).
        discovery = _discover_environments(context.root, context.environment)
        candidates, existing = discovery.candidates, discovery.existing
        if len(existing) > 1:
            reason = "more than one project environment was found"
            return ProjectSetupReport("inspection", Outcome.COMPLETED, reason,
                None if runtime_required else context, request.inputs,
                (ProjectSetupItem("runtime", runtime_required, None, Outcome.COMPLETED, reason=reason,
                                  action="select one interpreter", candidates=existing, ambiguous=True),),
                _operations(adapter, selection="select one interpreter"))
        if not existing and discovery.callers_own is not None:
            reason = ("the only environment found is the one the caller runs in, activated in "
                      "the shell, and a project runs with it only when it is selected")
            return _no_runtime(adapter, request, runtime_required, reason,
                               "select the project's interpreter, or this one on purpose, or "
                               "create the project's .venv", candidates)
        if not existing:
            reason = "no interpreter is selected, and neither a project .venv nor an activated environment was found"
            return _no_runtime(adapter, request, runtime_required, reason,
                               "select an interpreter or create the project's .venv", candidates)
        discovered = existing[0]
        runtime = Interpreter(path=discovered)
        context = replace(context, interpreter=runtime)
    if runtime is None:
        return _no_runtime(adapter, request, runtime_required,
                           "no interpreter is selected for the container, and the check does not search "
                           "the container for one",
                           "select the container's interpreter", _candidates(context))
    if runtime.path is None:
        reason = "a Python version was requested instead of an interpreter, and the check does not install Python"
        if not runtime_required:
            return ProjectSetupReport("inspection", Outcome.COMPLETED, reason, context, request.inputs,
                (ProjectSetupItem("runtime", False, None, None, reason=reason,
                                  action="select an installed interpreter by its path"),),
                _operations(adapter, selection="select an installed interpreter by its path"))
        return ProjectSetupReport("inspection", Outcome.UNSUPPORTED, reason, None, request.inputs,
            (ProjectSetupItem("runtime", runtime_required, None, None, reason=reason,
                              action="select an installed interpreter by its path", candidates=_candidates(context)),),
            _operations(adapter, selection="select an installed interpreter by its path"))
    if (not isinstance(context.execution, ContainerExecution) and captured(context.environment)
            and not Path(runtime.path).is_absolute()):
        address = selected_executable(runtime.path, context.environment)
        if address is None:
            return _failure(adapter, request, runtime_required, Outcome.MISSING_SETUP,
                            "the captured environment has no explicitly selected interpreter address")
        runtime = replace(runtime, path=address)
        context = replace(context, interpreter=runtime)
    if (not isinstance(context.execution, ContainerExecution) and os.path.dirname(runtime.path)
            and _host.started_file(runtime.path) is None):
        # A path names one file: the check looks for the file its start runs.
        return _failure(adapter, request, runtime_required, Outcome.MISSING_SETUP, _NO_FILE,
                        selected=runtime.path)
    # -S disables site startup and .pth files, -I project imports and the
    # user's site directory, which the probe lists itself where runs see it. The
    # flags and imports of real project execution are verified separately.
    try:
        completed = executor_for(context).run([runtime.path, "-I", "-S", "-c", _PROBE],
            timeout=request.remaining_timeout("isolated Python metadata probe"))
    except (OSError, subprocess.SubprocessError) as error:
        _preserve_owner(error)
        outcome = (Outcome.TIMED_OUT if isinstance(error, subprocess.TimeoutExpired) else
                   Outcome.MISSING_SETUP if isinstance(error, FileNotFoundError) else
                   Outcome.UNSUPPORTED if not _host.runs_commands() else Outcome.INFRASTRUCTURE_FAILURE)
        return _failure(adapter, request, runtime_required, outcome, _start_failure(error), selected=runtime.path)
    chosen = request.context if discovered is None else context
    if completed.returncode != 0:
        named = _named_version(context, runtime, request)
        profile = PythonProfile(named, SUPPORT)
        if profile.refusal():
            return _refused(adapter, request, runtime_required, profile, runtime.path, chosen)
        return _failure(adapter, request, runtime_required, Outcome.INFRASTRUCTURE_FAILURE,
                        f"the selected program exited with code {completed.returncode} instead of reporting "
                        "its Python version, so it may not be a Python interpreter" if named is None else
                        f"the selected {named.implementation} {named.text} exited with code "
                        f"{completed.returncode} when asked for its installed packages",
                        selected=runtime.path)
    try:
        observation = json.loads(completed.stdout)
        if (not isinstance(observation, dict) or not isinstance(observation.get("implementation"), str)
                or not observation["implementation"].strip() or not isinstance(observation.get("version"), str)
                or not observation["version"].strip() or not isinstance(observation.get("tools"), dict)
                or set(observation["tools"]) != set(TOOL_NAMES)
                or any(value is not None and (not isinstance(value, str) or not value.strip())
                       for value in observation["tools"].values())):
            raise ValueError("invalid metadata observation")
    except (ValueError, TypeError):
        return _failure(adapter, request, runtime_required, Outcome.INFRASTRUCTURE_FAILURE,
                        "the selected program ran but did not answer as a Python interpreter does",
                        selected=runtime.path)
    tools = observation["tools"]
    needed = {name for operation in static if operation.name in request.required_operations
              for name in _dependencies(operation, request.facets(operation.name))}
    untried = "the project's own imports and tests are not tried yet"
    observed = f"the interpreter starts, and {untried}"
    if discovered is not None:
        observed = f"the only project environment found was selected, the interpreter starts, and {untried}"
    found = python_version(observation["implementation"], observation["version"], "probe")
    profile = PythonProfile(found, SUPPORT)
    if profile.refusal():
        return _refused(adapter, request, runtime_required, profile, runtime.path, chosen)
    note = profile.note()
    items = [ProjectSetupItem("runtime", runtime_required, Availability.AVAILABLE, Outcome.COMPLETED,
                              selected=runtime.path, version=setup_runtime(found), version_record=found,
                              reason=observed + (f". {note}" if note else ""))]
    for name, version in tools.items():
        # Package records only: importing the tool is the tool check.
        items.append(ProjectSetupItem(name, name in needed,
            None if version is not None else Availability.MISSING_SETUP,
            Outcome.COMPLETED if version is not None else Outcome.MISSING_SETUP,
            selected=name if version is not None else None, version=version,
            reason="installed, not yet tried" if version is not None else
                   "not installed in the selected environment",
            action=_actions.VERIFY if version is not None else
                   f"install it into the selected environment: {runtime.path} -m pip install "
                   f"{tool_pins(found.text, [name])[0]}",
            tried_by="verification" if version is not None else None))
    # The returned selection names a discovered runtime and keeps an explicit one.
    return ProjectSetupReport("inspection", Outcome.COMPLETED,
                              "started the selected interpreter in isolation and read its installed "
                              "packages, and ran nothing in the project",
                              chosen, request.inputs, tuple(items),
                              _operations(adapter, tools, request=request, version=found.text))


def _named_version(context, runtime, request):
    """The implementation and version a program that failed the full probe
    names with the minimal one, or None when it names none."""
    timeout = request.remaining_timeout("minimal Python version probe")
    try:
        completed = executor_for(context).run([runtime.path, "-E", "-s", "-S", "-c", _VERSION_PROBE],
                                              timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        _preserve_owner(error)
        return None
    match = re.fullmatch(r"(\w+) (\d+\.\d+\.\d+)", (completed.stdout or "").strip())
    if completed.returncode != 0 or match is None:
        return None
    return python_version(match.group(1), match.group(2), "probe")


def _refused(adapter, request, required, profile, selected, chosen):
    """The report for an unsupported project Python: its runtime item and every
    process-backed operation are unsupported. When nothing requested needs the
    interpreter, the check completes with the selection."""
    item = ProjectSetupItem("runtime", required, Availability.UNSUPPORTED, Outcome.UNSUPPORTED,
                            selected=selected, reason=profile.refusal(), action=profile.action())
    return ProjectSetupReport("inspection", Outcome.UNSUPPORTED if required else Outcome.COMPLETED,
                              profile.refusal(), None if required else chosen, request.inputs, (item,),
                              _operations(adapter, refused=item))


#: Why a selected interpreter path that names no file cannot be asked.
_NO_FILE = "the selected interpreter file does not exist"


def _start_failure(error):
    """Why the selected interpreter could not be asked, in plain words."""
    if isinstance(error, subprocess.TimeoutExpired):
        seconds = error.timeout
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            return "the selected interpreter did not answer in time"
        return f"the selected interpreter did not answer within {seconds:g} second{'' if seconds == 1 else 's'}"
    if isinstance(error, FileNotFoundError):
        return _NO_FILE
    if isinstance(error, PermissionError):
        return "the selected file cannot be run: permission to execute it is missing"
    if isinstance(error, OSError) and not _host.runs_commands():
        # This host starts no command, so nothing is known about the file.
        return _host.refusal()
    if isinstance(error, OSError):
        return "the selected file could not be started as a program, so it may not be a Python interpreter"
    return "the selected interpreter could not be started"


def _failure(adapter, request, required, outcome, reason, *, selected=None):
    action = ("select an existing interpreter by its absolute path" if outcome is Outcome.MISSING_SETUP else
              _actions.RETRY if outcome is Outcome.TIMED_OUT else
              "check that the selected interpreter starts (try it with --version), or select another one")
    return ProjectSetupReport("inspection", outcome, reason, None, request.inputs,
        (ProjectSetupItem("runtime", required,
            Availability.MISSING_SETUP if outcome is Outcome.MISSING_SETUP else None, outcome,
            selected=request.context.interpreter.path if selected is None else selected, reason=reason,
            action=action),),
        _operations(adapter, runtime_missing=outcome is Outcome.MISSING_SETUP))


_IMPORT = """\
import importlib, json, sys
try:
    importlib.import_module(sys.argv[1])
except ModuleNotFoundError as error:
    status = 'missing' if error.name == sys.argv[1] else 'failed'
except Exception:
    status = 'failed'
else:
    status = 'imported'
print(json.dumps({'status': status}))
"""


def _verified_operations(adapter, items, request):
    by_key = {item.key: item for item in items}
    runtime = by_key.get("runtime")
    # An interpreter still to be chosen: nothing tries process-backed work before it.
    selection = (runtime.action if runtime is not None and (runtime.ambiguous or (
        runtime.readiness is None and runtime.outcome is None and runtime.selected is None)) else None)
    result = []
    for operation in adapter.operation_capabilities():
        if not operation.supported:
            result.append(operation)
            continue
        if operation.execution_backed and runtime is not None and runtime.outcome is Outcome.UNSUPPORTED:
            result.append(replace(operation, readiness=Availability.UNSUPPORTED,
                                  reason=runtime.reason, action=runtime.action))
            continue
        if operation.execution_backed and selection is not None:
            result.append(replace(operation, readiness=None,
                                  reason="waits for an interpreter to be selected", action=selection))
            continue
        uses = tuple(name for name in _dependencies(operation, request.facets(operation.name)) if name in by_key)
        tools = tuple(name for name in uses if by_key[name].outcome is Outcome.MISSING_SETUP)
        failed = tuple(name for name in uses if by_key[name].outcome not in (
            None, Outcome.COMPLETED, Outcome.MISSING_SETUP))
        inputs = operation.execution_backed and any(
            by_key[key].outcome is Outcome.MISSING_SETUP
            for key in ("runtime", "preparation_input") if key in by_key)
        missing = ("execution input",) * inputs + tools
        result.append(replace(operation,
            readiness=Availability.MISSING_SETUP if missing else None,
            reason="missing: " + ", ".join(missing) if missing else
                   "needs " + " and ".join(failed) + ", which did not load" if failed else
                   "its tools load, and the operation itself is not tried yet"
                   if uses and all(by_key[name].readiness is Availability.AVAILABLE for name in uses) else
                   "not yet tried on this project",
            action=("select an existing interpreter and project directory" if inputs else
                    _install(tools, _python_text(runtime)) if tools else
                    f"fix {failed[0]} first, then run the tool check again" if failed else _actions.RUN),
            # Only an operation waiting for nothing but a run is run-only.
            tried_by=None if missing or failed else "run"))
    return tuple(result)


def _report(adapter, request, outcome, items, reason, *, context=None):
    return ProjectSetupReport("verification", outcome, reason, context,
                              request.inputs, _scoped(items), _verified_operations(adapter, items, request))


def _scoped(items):
    """The items with no action to run the verification: a tool it stopped
    before points at what stopped it."""
    missing = [item.key for item in items if item.key in TOOL_MODULES and item.required
               and item.outcome is Outcome.MISSING_SETUP]
    action = (f"install {' and '.join(missing)} first, then run the tool check again" if missing
              else "fix the other problems reported first, then run the tool check again")
    return tuple(replace(item, action=action) if item.action == _actions.VERIFY else item for item in items)


def _python_text(runtime):
    """The probed Python version of the ``runtime`` item, or None."""
    record = None if runtime is None else runtime.version_record
    return None if record is None else record.text


def _tool_item(name, metadata, outcome, interpreter, version):
    action = (None if outcome is Outcome.COMPLETED else
              f"install it into the selected environment: {interpreter} -m pip install "
              f"{tool_pins(version, [name])[0]}"
              if outcome is Outcome.MISSING_SETUP else _actions.RETRY if outcome is Outcome.TIMED_OUT else
              f"run {interpreter} -c \"import {TOOL_MODULES[name]}\" in the project directory and fix what it reports")
    return ProjectSetupItem(name, metadata.required,
        Availability.AVAILABLE if outcome is Outcome.COMPLETED else
        Availability.MISSING_SETUP if outcome is Outcome.MISSING_SETUP else None,
        outcome, selected=metadata.selected if outcome is Outcome.COMPLETED else None,
        version=metadata.version if outcome is Outcome.COMPLETED else None,
        reason="loads in the selected environment, not yet run on the project"
               if outcome is Outcome.COMPLETED else "importing it in the selected environment did not succeed",
        action=action)


def _observe(context, request, items, needed):
    executor = executor_for(context)
    for name in needed:
        metadata = next(item for item in items if item.key == name)
        try:
            completed = executor.run(
                [context.interpreter.path, *context.interpreter_flags, "-B", "-c", _IMPORT, TOOL_MODULES[name]],
                cwd=context.root,
                timeout=request.remaining_timeout(f"{name} import verification"))
            request.remaining_timeout(f"{name} import completion", probe=False)
        except (OSError, subprocess.SubprocessError) as error:
            _preserve_owner(error)
            outcome = (Outcome.TIMED_OUT if isinstance(error, subprocess.TimeoutExpired) else
                       Outcome.MISSING_SETUP if isinstance(error, FileNotFoundError) else
                       Outcome.INFRASTRUCTURE_FAILURE)
        else:
            outcome = Outcome.INFRASTRUCTURE_FAILURE
            if completed.returncode == 0:
                try:
                    value = json.loads(completed.stdout)
                except (ValueError, TypeError):
                    pass
                else:
                    if (isinstance(value, dict) and set(value) == {"status"}
                            and isinstance(value["status"], str)):
                        outcome = {"imported": Outcome.COMPLETED,
                                   "missing": Outcome.MISSING_SETUP,
                                   "failed": Outcome.INFRASTRUCTURE_FAILURE}.get(
                                       value["status"], Outcome.INFRASTRUCTURE_FAILURE)
        items[items.index(metadata)] = _tool_item(name, metadata, outcome, context.interpreter.path,
                                                  _python_text(next(i for i in items if i.key == "runtime")))
        if outcome is not Outcome.COMPLETED:
            for later in needed[needed.index(name) + 1:]:
                item = next(item for item in items if item.key == later)
                if item.action == _actions.VERIFY:
                    items[items.index(item)] = replace(
                        item, action=f"fix {name} first, then run the tool check again")
            return outcome
    return Outcome.COMPLETED


def _collection_items(adapter, request):
    """The collection of the copy's tests (``_runs.verification_collection``)
    as two items: ``test_collection``, reported without being required,
    since a run collects only the tests it selects, and ``project_origin``,
    where the tests imported a module of the copy from outside it. That is
    required only where the module came through a finder placed ahead of
    the import path, behind which a run that names the module is refused
    too. Another source is reported only, since such a run checks it itself
    and puts the module's directory first. The collection puts the copy's
    source directories first, as such a run does for the common layouts.
    Without a source file, or when the check has no time left for the
    collection or it runs out of time, both items are left to the first run,
    and the report's outcome stays the one without them."""
    from langadapter.python._runs import verification_collection

    collected = refusal = None
    first = False
    reason = "no source file is selected, so the check collected no test"
    if request.source_files:
        reason = "the check ran out of time before it collected the tests"
        try:
            timeout = request.remaining_timeout("test collection", probe=False)
            collected, refusal, first = verification_collection(
                adapter, request.source_files[0], timeout)
        except subprocess.TimeoutExpired:
            pass
    if collected is None or collected.outcome is Outcome.TIMED_OUT:
        return [ProjectSetupItem(key, False, None, None, reason=reason, action=_actions.RUN,
                                 tried_by="run")
                for key in ("test_collection", "project_origin")]
    outcome = collected.outcome
    detail = f": {collected.diagnostic}" if collected.diagnostic else ""
    collection = ProjectSetupItem(
        "test_collection", False, Availability.AVAILABLE if outcome is Outcome.COMPLETED else None,
        outcome, reason={
            Outcome.COMPLETED: "the project's tests collect in the copy, and none was run",
            Outcome.NO_TESTS: "pytest found no test of the project in the copy",
        }.get(outcome, collected.reason + detail),
        action=None if outcome in (Outcome.COMPLETED, Outcome.NO_TESTS) else
        "fix the test module that does not collect, or leave it out of the runs"
        if outcome is Outcome.COLLECTION_FAILED else
        "fix what the reason names, then run the tool check again")
    if outcome not in (Outcome.COMPLETED, Outcome.COLLECTION_FAILED):
        origin = ProjectSetupItem("project_origin", False, None, None,
                                  reason="the copy's collection gave no verdict, so the tests' imports "
                                  "are not checked yet",
                                  action=_actions.RUN, tried_by="run")
    elif refusal is None:
        origin = ProjectSetupItem("project_origin", False, Availability.AVAILABLE, Outcome.COMPLETED,
                                  reason="with the copy's source directories first, the tests' imports "
                                  "give no refusal")
    else:
        origin = ProjectSetupItem("project_origin", first, None, Outcome.INFRASTRUCTURE_FAILURE,
                                  reason=refusal, action="fix what the reason names, then run the "
                                  "tool check again")
    return [collection, origin]


#: Why a tool check that stopped early loaded nothing: an item names the cause.
_NOT_COPIED = "the check stopped before it copied the project, so no tool was loaded"


def verify(adapter, request):
    metadata = inspect(adapter, request)
    items = list(metadata.items)
    if metadata.context is None:
        return _report(adapter, request, metadata.outcome, items, metadata.reason)
    needed = tuple(name for name in TOOL_MODULES if any(
        name in _dependencies(operation, request.facets(operation.name))
        for operation in adapter.operation_capabilities()
        if operation.name in request.required_operations))
    # Only the tools the requested operations use are imported.
    items = [replace(item, action=None, tried_by=None) if item.key in TOOL_MODULES and item.key not in needed
             and item.action == _actions.VERIFY else item for item in items]
    runtime_required = next(item.required for item in items if item.key == "runtime")
    items.append(ProjectSetupItem("native_execution", runtime_required, None, None,
        reason="the project's tests and measurements are not run yet", action=_actions.RUN,
        tried_by="run"))
    if not needed:
        return _report(adapter, request, Outcome.COMPLETED, items,
                       "the requested operations need no measurement tool, and the project's "
                       "tests were not run", context=metadata.context)
    if any(item.required and item.outcome is Outcome.MISSING_SETUP for item in items):
        return _report(adapter, request, Outcome.MISSING_SETUP, items, _NOT_COPIED)
    address = next(item.selected for item in items if item.key == "runtime")
    container = isinstance(request.context.execution, ContainerExecution)
    path = PurePosixPath if container else Path
    normalize = posixpath.normpath if container else os.path.abspath
    if not path(address).is_absolute():
        items.append(ProjectSetupItem("runtime_role", True, None, Outcome.UNSUPPORTED,
            reason="the tool check needs the interpreter's full path",
            action="select the interpreter by its absolute path"))
        return _report(adapter, request, Outcome.UNSUPPORTED, items, _NOT_COPIED)
    inputs = request.inputs
    try:
        path(normalize(address)).relative_to(path(normalize(request.context.root)))
    except ValueError:
        pass
    else:
        if container:
            # The container's environment directory cannot be named from the host.
            items.append(ProjectSetupItem("runtime_role", True, None, Outcome.UNSUPPORTED,
                reason="the tool check does not try an interpreter inside a container's project directory",
                action="start a run to try it, or select an interpreter outside the project directory"))
            return _report(adapter, request, Outcome.UNSUPPORTED, items, _NOT_COPIED)
        # As runs do: the interpreter is used in place and its environment
        # directory is left out of the copy. The imports write no bytecode (-B).
        env_dir = environment_dir(address, request.context.root)
        if env_dir is not None:
            inputs = ProjectInputSelection(
                (*(() if inputs is None else inputs.exclude), env_dir),
                () if inputs is None else inputs.required)
    # The inspected selection: an explicit runtime, or the one discovered environment.
    selected = replace(metadata.context, interpreter=Interpreter(path=address))
    try:
        owner = prepare_project(selected, inputs=inputs,
                                timeout_s=request.remaining_timeout("verification preparation", probe=False))
    except InvalidRequest as error:
        if not _add_missing_input_item(error, items, "interpreter"):
            raise
        return _report(adapter, request, Outcome.MISSING_SETUP, items,
                       "the project could not be copied, so no tool was loaded")
    with _closing(owner, "Verification"):
        with owner.use() as context:
            outcome = _observe(context, request, items, needed)
            if outcome is Outcome.COMPLETED and "pytest" in needed:
                items += _collection_items(type(adapter)(context), request)
        return _report(adapter, request, outcome, items,
                       "loaded the needed tools in a project copy, and the project's tests "
                       "were not run", context=metadata.context if outcome is Outcome.COMPLETED else None)
