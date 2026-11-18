"""Checks of a project's setup, reported as typed results, that build and
install nothing: the common admission of a request's project and inputs,
then the registered adapter's own native checks."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import math
import re
import subprocess
import time
from typing import Optional
from langadapter.capabilities import (
    TRIED_BY, Availability, OPERATION_FACETS, OPERATION_NAMES, OperationCapability,
)
from langadapter.context import ProjectContext, ContainerExecution, ProjectInputSelection, _paths
from langadapter.errors import InvalidRequest
from langadapter.outcomes import Outcome
from langadapter.versions import Version, setup_runtime
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat
from langadapter import _host, capabilities as _actions
from langadapter._execution_scope import operation, reraise_unresolved, unresolved_command
from langadapter._input_copy import LinkRefused, ordinary
from langadapter._linux_job import SupervisionUnavailable
from langadapter.execution import executor_for
from langadapter.preparation import _framework_required
from langadapter.registry import create, registration


def _texts(values, name):
    if isinstance(values, (str, bytes)):
        raise InvalidRequest(name, "must be a sequence of distinct non-empty strings")
    try:
        result = tuple(values)
    except TypeError:
        raise InvalidRequest(name, "must be a sequence of distinct non-empty strings") from None
    if (any(not isinstance(value, str) or not value.strip() or "\0" in value for value in result)
            or len(set(result)) != len(result)):
        raise InvalidRequest(name, "must be a sequence of distinct non-empty strings")
    return result


def _text(value, name, *, optional=False, empty=False):
    if value is None and optional:
        return
    if not isinstance(value, str) or "\0" in value or (not empty and not value.strip()):
        raise InvalidRequest(name, "must be text" if empty else "must be non-empty text")


def _facets(values, operations):
    """Normalize required facets to sorted, hashable (operation, names) pairs."""
    try:
        pairs = tuple(values.items()) if isinstance(values, Mapping) else tuple(values)
        pairs = tuple((operation, facets) for operation, facets in pairs)
    except (TypeError, ValueError):
        raise InvalidRequest("required_facets", "must map required operations to facet names") from None
    result = {}
    for operation, facets in pairs:  # noqa: F402
        if operation not in operations:
            raise InvalidRequest("required_facets", f"{operation!r} is not a required operation")
        names = _texts(facets, "required_facets")
        unknown = set(names) - set(OPERATION_FACETS.get(operation, ()))
        if not names or unknown or operation in result:
            raise InvalidRequest("required_facets",
                                 f"{operation!r} offers only {', '.join(OPERATION_FACETS.get(operation, ())) or 'no facets'}")
        result[operation] = tuple(sorted(names))
    return tuple(sorted(result.items()))


def _positive(value, name):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise InvalidRequest(name, "must be a finite positive number")


_SENSITIVE = re.compile(r"password|passwd|secret|token|api[-_]?key|credential|authorization", re.I)
_ASSIGNMENT = re.compile(
    r"\b((?:[\w.-]*(?:password|passwd|secret|token|api[-_]?key|credential|authorization)[\w.-]*)"
    r"\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)", re.I)
_URL_USER = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/@]+@", re.I)
_BEARER = re.compile(r"\b(Bearer\s+)[^\s,;]+", re.I)
_AUTHORIZATION = re.compile(r"(\bAuthorization\s*[=:]\s*)(?:Basic|Bearer)\s+"
                            r"(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)", re.I)
#: The shortest sensitive value redacted wherever it occurs. A shorter one,
#: such as "false", "cache" or "1", is redacted only where a pattern above
#: finds it, so paths and versions that contain it stay readable.
_REDACTED_ANYWHERE = 6


def _safe(text, secrets=()):
    if text is None:
        return None
    for secret in sorted(set(secrets), key=len, reverse=True):
        if len(secret) >= _REDACTED_ANYWHERE:
            text = text.replace(secret, "[redacted]")
    text = _URL_USER.sub(r"\1[redacted]@", text)
    text = _AUTHORIZATION.sub(r"\1[redacted]", text)
    text = _BEARER.sub(r"\1[redacted]", text)
    return _ASSIGNMENT.sub(r"\1[redacted]", text)


@dataclass(frozen=True, repr=False)
class ProjectSetupRequest:
    """One explicit project selection and finite setup assessment scope.

    Source paths are normalized project-relative inputs. Inspection never
    acquires tools. Verification does only when ``allow_acquisition`` asks. The
    deadline starts at each assessment, so a request is reusable.
    ``required_facets`` names measurements required of a required operation
    (``OPERATION_FACETS``), such as ``{"tests.run_suite": ("coverage",)}``.

    - ``context``: the project context to assess.
    - ``source_files``: the project-relative source files to assess, which
      the input selection requires.
    - ``required_operations``: the operations the project needs, by their
      names in ``OPERATION_NAMES``. The prerequisites they depend on are
      required items, and the report states each of these operations.
    - ``inputs``: the entries to leave out of the project's inputs and the
      paths that must be present, None for none.
    - ``probe_timeout_s``: the time limit of one probe in seconds, capped by
      what is left of ``total_timeout_s``.
    - ``total_timeout_s``: the time limit of one assessment in seconds,
      counted from its start.
    """

    context: ProjectContext = field(repr=False)
    source_files: tuple[str, ...]
    required_operations: tuple[str, ...]
    inputs: Optional[ProjectInputSelection] = None
    probe_timeout_s: float = 10.0
    total_timeout_s: float = 120.0
    allow_acquisition: bool = False
    required_facets: tuple[tuple[str, tuple[str, ...]], ...] = ()
    _deadline: Optional[float] = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self):
        if not isinstance(self.context, ProjectContext):
            raise InvalidRequest("context", "must be a ProjectContext")
        object.__setattr__(self, "source_files", _paths(self.source_files, "source_files"))
        operations = _texts(self.required_operations, "required_operations")
        unknown = set(operations) - set(OPERATION_NAMES)
        if unknown:
            raise InvalidRequest("required_operations", f"unknown operations: {', '.join(sorted(unknown))}")
        object.__setattr__(self, "required_operations", operations)
        object.__setattr__(self, "required_facets", _facets(self.required_facets, operations))
        if self.inputs is not None and not isinstance(self.inputs, ProjectInputSelection):
            raise InvalidRequest("inputs", "must be a ProjectInputSelection or None")
        _positive(self.probe_timeout_s, "probe_timeout_s")
        _positive(self.total_timeout_s, "total_timeout_s")
        if type(self.allow_acquisition) is not bool:
            raise InvalidRequest("allow_acquisition", "must be a boolean")

    def facets(self, operation: str) -> tuple[str, ...]:
        """The measurements required of ``operation``, empty when none are."""
        return dict(self.required_facets).get(operation, ())

    def remaining_timeout(self, stage="project setup", *, probe=True) -> float:
        """The per-probe bound capped by this assessment's remaining deadline,
        asked before each scan, probe, build or copy. ``probe=False`` gives all
        the remaining time (for a build or copy) instead of a version probe's
        short limit. Cleanup after a confirmed stop has its own allowance."""
        if type(probe) is not bool:
            raise InvalidRequest("probe", "must be a boolean")
        if self._deadline is None:
            raise InvalidRequest("request", "an assessment entry must establish its deadline")
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(stage, self.total_timeout_s)
        return min(float(self.probe_timeout_s), remaining) if probe else remaining

    def _for_assessment(self, *, inspection):
        result = replace(self, allow_acquisition=False if inspection else self.allow_acquisition)
        object.__setattr__(result, "_deadline", time.monotonic() + float(self.total_timeout_s))
        return result

    def __repr__(self):
        return (f"ProjectSetupRequest(source_files={len(self.source_files)}, "
                f"required_operations={self.required_operations!r}, "
                f"required_facets={dict(self.required_facets)!r}, probe_timeout_s={self.probe_timeout_s!r}, "
                f"total_timeout_s={self.total_timeout_s!r}, allow_acquisition={self.allow_acquisition!r})")


@dataclass(frozen=True, repr=False)
class ProjectSetupItem:
    """One prerequisite the check assessed. An outcome or readiness of None
    means not tried or unknown. A selected path shows neither readiness nor
    version. Reasons describe the assessment, never raw tool output.
    ``tried_by`` names what first tries an untried item: ``"verification"``,
    ``"run"`` (only a real run), or None."""

    key: str
    required: bool
    readiness: Optional[Availability]
    outcome: Optional[Outcome]
    selected: Optional[str] = None
    version: Optional[str] = None
    reason: str = ""
    action: Optional[str] = None
    candidates: tuple[str, ...] = ()
    """What the user selects from: when ``ambiguous`` is True, the choices found
    (interpreters, build systems), else interpreter addresses the check did not
    find or did not try."""
    ambiguous: bool = False
    """Whether the check found more than one choice, which ``candidates`` holds,
    and the user selects one."""
    tried_by: Optional[str] = None
    version_record: Optional[Version] = None
    """A runtime item's version, which ``version`` is the text of
    (``versions.setup_runtime``), or None. A tool's version is its text."""

    def __post_init__(self):
        _text(self.key, "item.key")
        if self.version_record is not None:
            if not isinstance(self.version_record, Version):
                raise InvalidRequest("item.version_record", "must be a Version or None")
            if self.version != setup_runtime(self.version_record):
                raise InvalidRequest("item.version", "must be the text of its version record")
        if type(self.required) is not bool or type(self.ambiguous) is not bool:
            raise InvalidRequest("item", "required and ambiguous must be booleans")
        if self.readiness is not None and not isinstance(self.readiness, Availability):
            raise InvalidRequest("item.readiness", "must be an Availability or None")
        if self.outcome is not None and not isinstance(self.outcome, Outcome):
            raise InvalidRequest("item.outcome", "must be an Outcome or None")
        for name in ("selected", "version", "action"):
            _text(getattr(self, name), f"item.{name}", optional=True)
        _text(self.reason, "item.reason", empty=True)
        object.__setattr__(self, "candidates", _texts(self.candidates, "item.candidates"))
        if self.readiness is Availability.AVAILABLE and self.outcome is not Outcome.COMPLETED:
            raise InvalidRequest("item.readiness", "available requires a completed observation")
        if self.version is not None and (self.selected is None or self.outcome is not Outcome.COMPLETED):
            raise InvalidRequest("item.version", "an observed version requires a selected, completed probe")
        if self.ambiguous and (self.selected is not None or self.version is not None
                               or self.readiness is Availability.AVAILABLE):
            raise InvalidRequest("item.ambiguous", "ambiguous candidates cannot be selected or available")
        if self.tried_by is not None and (
                self.tried_by not in TRIED_BY or self.readiness is not None or self.ambiguous
                or self.outcome not in (None, Outcome.COMPLETED)):
            raise InvalidRequest("item.tried_by", "only an item not tried yet names 'verification' or 'run'")

    def _export(self, secrets=()):
        return {"key": _safe(self.key, secrets), "required": self.required,
                "readiness": None if self.readiness is None else self.readiness.value,
                "outcome": None if self.outcome is None else self.outcome.value,
                "selected": _safe(self.selected, secrets), "version": _safe(self.version, secrets),
                "reason": _safe(self.reason, secrets), "action": _safe(self.action, secrets),
                "candidates": [_safe(value, secrets) for value in self.candidates],
                "ambiguous": self.ambiguous, "tried_by": self.tried_by}

    def __repr__(self):
        return (f"ProjectSetupItem(key={_safe(self.key)!r}, required={self.required!r}, "
                f"readiness={self.readiness!r}, outcome={self.outcome!r}, ambiguous={self.ambiguous!r})")


@dataclass(frozen=True, repr=False)
class ProjectSetupReport:
    """Facts about an inspection or verification, not a promise of successful tests.

    COMPLETED means the assessment ran. Required missing or unverified items stay
    explicit. ``safe_export`` formats it, leaving out the selected context.
    ``diagnostic_paths`` holds the paths of diagnostics as distinct texts,
    which ``safe_export`` passes on with secrets redacted. The shipped
    adapters leave it empty."""

    scope: str
    outcome: Outcome
    reason: str
    context: Optional[ProjectContext] = field(repr=False)
    inputs: Optional[ProjectInputSelection]
    items: tuple[ProjectSetupItem, ...]
    operations: tuple[OperationCapability, ...]
    diagnostic_paths: tuple[str, ...] = ()
    _redactions: tuple[str, ...] = field(default=(), init=False, repr=False, compare=False)

    def __post_init__(self):
        if self.scope not in ("inspection", "verification"):
            raise InvalidRequest("report.scope", "must be inspection or verification")
        if not isinstance(self.outcome, Outcome):
            raise InvalidRequest("report.outcome", "must be an Outcome")
        _text(self.reason, "report.reason", empty=True)
        if self.context is not None and not isinstance(self.context, ProjectContext):
            raise InvalidRequest("report.context", "must be a ProjectContext or None")
        if self.inputs is not None and not isinstance(self.inputs, ProjectInputSelection):
            raise InvalidRequest("report.inputs", "must be a ProjectInputSelection or None")
        for name, expected, identifier in (("items", ProjectSetupItem, "key"),
                                           ("operations", OperationCapability, "name")):
            try:
                values = tuple(getattr(self, name))
            except TypeError:
                raise InvalidRequest(f"report.{name}", "must be a sequence of typed records") from None
            if (any(not isinstance(value, expected) for value in values)
                    or len({getattr(value, identifier) for value in values}) != len(values)):
                raise InvalidRequest(f"report.{name}", "must be distinct typed records")
            object.__setattr__(self, name, values)
        paths = _texts(self.diagnostic_paths, "report.diagnostic_paths")
        object.__setattr__(self, "diagnostic_paths", paths)
        if self.context is not None and any(item.required and item.ambiguous for item in self.items):
            raise InvalidRequest("report.context", "required ambiguous selections cannot provide a context")

    def safe_export(self):
        """The statuses and paths as plain data, without environment or tool output."""
        secrets = self._redactions + (() if self.context is None else _redactions(self.context))
        plan = None if self.inputs is None else self.inputs.to_dict()
        if plan is not None:
            plan = {key: [_safe(value, secrets) for value in values] for key, values in plan.items()}
        return {"scope": self.scope, "outcome": self.outcome.value, "reason": _safe(self.reason, secrets),
                "has_selection": self.context is not None, "inputs": plan,
                "items": [item._export(secrets) for item in self.items],
                "operations": [{"name": _safe(item.name, secrets), "supported": item.supported,
                                "reason": _safe(item.reason, secrets),
                                "readiness": None if item.readiness is None else item.readiness.value,
                                "execution_backed": item.execution_backed,
                                "execution_kinds": [_safe(value, secrets) for value in item.execution_kinds],
                                "evidence_granularity": [_safe(value, secrets) for value in item.evidence_granularity],
                                "resolution_methods": [_safe(value, secrets) for value in item.resolution_methods],
                                "action": _safe(item.action, secrets),
                                "tried_by": item.tried_by} for item in self.operations],
                "diagnostic_paths": [_safe(value, secrets) for value in self.diagnostic_paths]}

    def __repr__(self):
        return (f"ProjectSetupReport(scope={self.scope!r}, outcome={self.outcome!r}, "
                f"has_selection={self.context is not None!r}, items={len(self.items)}, "
                f"operations={len(self.operations)})")


def inspect_project(language: str, request: ProjectSetupRequest) -> ProjectSetupReport:
    """Inspect one selected project without acquiring tools or running a build.
    Metadata and bounded native version probes are adapter-specific. Static
    operation support does not prove that a requested operation will run."""
    return _assess(language, request, scope="inspection")


def verify_project(language: str, request: ProjectSetupRequest) -> ProjectSetupReport:
    """Assess selected prerequisites with the registered adapter, acquiring tools
    only when the request allows it. A completed assessment does not guarantee
    that later tests or mutations succeed."""
    return _assess(language, request, scope="verification")


def _redactions(context):
    values = tuple(value for key, value in context.environment.items() if _SENSITIVE.search(key))
    control = getattr(context.execution, "control_environment", None)
    if control is not None:
        values += tuple(value for key, value in control.items() if _SENSITIVE.search(key))
    return values


def _result(scope, request, outcome, key, reason, action):
    return ProjectSetupReport(scope, outcome, reason, None, request.inputs,
                              (ProjectSetupItem(key, True, Availability.MISSING_SETUP
                                                if outcome is Outcome.MISSING_SETUP else None,
                                                outcome, reason=reason, action=action),), ())


def _preserve_owner(error):
    reraise_unresolved(error)
    for name in ("_langadapter_prepared_copy", "_langadapter_prepared_project",
                 "_langadapter_owned_directories", "_langadapter_mutation_session"):
        owner = getattr(error, name, None)
        if owner is not None and not (name == "_langadapter_owned_directories" and owner == ()):
            raise error


def _add_missing_input_item(error, items, selected: str) -> bool:
    """Add to ``items`` the missing-setup item for the ``selected`` runtime
    (such as "JDK") when ``error``, a project copy refused on its inputs,
    names a missing input, and say whether it did. An error that holds an
    owner raises."""
    _preserve_owner(error)
    if error.field not in {"root", "interpreter.path", "execution.work_dir"}:
        return False
    items.append(ProjectSetupItem(
        "preparation_input", True, Availability.MISSING_SETUP, Outcome.MISSING_SETUP,
        reason=f"the selected {selected} or project directory was not found when the "
               "project was copied",
        action=f"check that the selected {selected} and project directory exist"))
    return True


@contextmanager
def _closing(owner, label: str):
    """``owner`` for the block, closed after it. When the block failed, a
    failed cleanup joins that failure: its owner, and a note naming the
    ``label`` copy."""
    primary = None
    try:
        yield owner
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            owner.close()
        except BaseException as cleanup:
            if primary is None:
                raise
            primary._langadapter_prepared_project = owner
            if hasattr(primary, "add_note"):
                try:
                    primary.add_note(f"{label} copy cleanup failed: {cleanup}")
                except BaseException:
                    pass


def _root_dirs(root):
    """The directories of ``root`` admitted before canonicalization can hide a
    link: each directory a later ".." climbs out of, which reads differently
    through a link than in the selected text, then the root itself. A directory
    above the root may otherwise be a link, as for a run."""
    parts, entered, climbed = root.parts, [], []
    for index, part in enumerate(parts[1:], 1):
        if part != "..":
            entered.append(index)
        elif entered:
            climbed.append(type(root)(*parts[:entered.pop() + 1]))
    return [*climbed, root]


def _local_inputs(request):
    root = Path(request.context.root).absolute()
    for current in _root_dirs(root):
        request.remaining_timeout("project root admission")
        if not stat.S_ISDIR(ordinary(current).st_mode):
            raise NotADirectoryError(str(current))
    root = root.resolve()
    plan = (request.inputs or ProjectInputSelection()).effective(
        (*request.source_files, *_framework_required(request.context, root)), case_sensitive=os.name != "nt")
    selected = replace(request, inputs=plan)
    object.__setattr__(selected, "_deadline", request._deadline)
    for relative in plan.required:
        current = root
        for part in relative.split("/"):
            selected.remaining_timeout("required input admission")
            current /= part
            ordinary(current)
    return selected


_CONTAINER_INPUTS = """\
import json, os, stat, sys
from pathlib import Path
root = Path(sys.argv[1])
required = json.loads(sys.argv[2])
root_dirs = json.loads(sys.argv[3])
class Link(OSError):
    pass
def ordinary(path):
    value = path.lstat()
    if stat.S_ISLNK(value.st_mode):
        raise Link(str(path))
    if not (stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode)):
        raise ValueError('selected input is not an ordinary entry')
    return value
try:
    for root_dir in root_dirs:
        if not stat.S_ISDIR(ordinary(Path(root_dir)).st_mode):
            raise NotADirectoryError()
    for relative in required:
        current = root
        for part in relative.split('/'):
            current /= part
            ordinary(current)
except (OSError, ValueError) as error:
    print(json.dumps({'ok': False, 'error': type(error).__name__, 'path': str(error)}))
else:
    print(json.dumps({'ok': True}))
"""


class _NoContainerControl(FileNotFoundError):
    """The container has no helper Python selected, so its inputs cannot be admitted."""


def _container_inputs(request):
    context = request.context
    root = PurePosixPath(posixpath.normpath(context.root))
    plan = (request.inputs or ProjectInputSelection()).effective(
        (*request.source_files, *_framework_required(context, root)))
    selected = replace(request, inputs=plan)
    object.__setattr__(selected, "_deadline", request._deadline)
    if context.execution.helper_python is None:
        raise _NoContainerControl("select the container's helper Python (execution.helper_python) before setup assessment")
    completed = executor_for(context).run(
        [context.execution.helper_python, "-I", "-S", "-c", _CONTAINER_INPUTS, context.root,
         json.dumps(plan.required), json.dumps([str(path) for path in _root_dirs(PurePosixPath(context.root))])],
        cwd="/",
        timeout=selected.remaining_timeout("input admission in the container"))
    if completed.returncode != 0:
        raise OSError("the container's input admission helper failed")
    try:
        value = json.loads(completed.stdout)
    except (ValueError, TypeError):
        raise OSError("input admission in the container returned no readable report") from None
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        raise OSError("input admission in the container returned an invalid report")
    if not value["ok"]:
        error = value.get("error")
        if error == "Link":
            raise LinkRefused(f"selected project input cannot be a link: {value.get('path')}")
        if error in ("FileNotFoundError", "NotADirectoryError"):
            raise FileNotFoundError("a selected input is missing in the container")
        raise OSError("a selected input cannot be admitted in the container")
    return selected


def _validate_report(report, request, scope):
    if not isinstance(report, ProjectSetupReport) or report.scope != scope:
        raise InvalidRequest("report", "the adapter must return the requested ProjectSetupReport scope")
    if report.inputs is None or (report.inputs.exclude != request.inputs.exclude
                                or not set(request.inputs.required) <= set(report.inputs.required)):
        raise InvalidRequest("report.inputs", "the adapter must preserve exclusions and protected inputs")
    report.inputs.effective(case_sensitive=isinstance(request.context.execution, ContainerExecution) or os.name != "nt")
    if report.context is not None:
        report.context._validate(check_paths=False)
        if request.context.interpreter is not None and report.context.interpreter != request.context.interpreter:
            raise InvalidRequest("report.context.interpreter", "the adapter must preserve an explicitly selected runtime")
        if replace(report.context, interpreter=request.context.interpreter) != request.context:
            raise InvalidRequest("report.context", "the adapter may select an absent runtime, not replace project execution or inputs")
    if not set(request.required_operations) <= {item.name for item in report.operations}:
        raise InvalidRequest("report.operations", "the adapter must report all requested operations")
    return report


def _assess(language, request, *, scope):
    if not isinstance(request, ProjectSetupRequest):
        raise InvalidRequest("request", "must be a ProjectSetupRequest")
    request.context._validate(check_paths=False)
    request.context._require_execution()
    if request.context._prepared_copy_binding is not None:
        raise InvalidRequest("context", "setup selects an original project, not a disposable work context")
    found = registration(language)
    selected = request._for_assessment(inspection=scope == "inspection")
    with operation(request.context):
        try:
            selected = _container_inputs(selected) if isinstance(request.context.execution, ContainerExecution) else _local_inputs(selected)
            selected.remaining_timeout("adapter construction")
            adapter = create(found.name, replace(request.context, interpreter=None))
            selected.remaining_timeout("native hook admission")
            report = (adapter.inspect_project(selected) if scope == "inspection" else adapter.verify_project(selected))
            selected.remaining_timeout("assessment completion")
            report = _validate_report(report, selected, scope)
        except subprocess.TimeoutExpired as error:
            _preserve_owner(error)
            report = _result(scope, selected, Outcome.TIMED_OUT, "assessment", "the check ran out of time",
                             _actions.RETRY)
        except _NoContainerControl as error:
            _preserve_owner(error)
            report = _result(scope, selected, Outcome.MISSING_SETUP, "guest_control",
                             "no Python control helper is selected for the container",
                             "select the container's Python 3.10 or newer as the control helper "
                             "(execution.helper_python)")
        except SupervisionUnavailable as error:
            # The control helper (helper_python in a container, else the
            # library's Python) refused before any command ran, as below 3.10 or
            # without procfs child accounting. A refusal not confirming that the
            # helper has no child keeps its owner unresolved on the report.
            unresolved = unresolved_command(error)
            if unresolved is None:
                _preserve_owner(error)
            if isinstance(request.context.execution, ContainerExecution):
                report = _result(scope, selected, Outcome.MISSING_SETUP, "guest_control",
                                 f"the container's control helper cannot supervise: {error}",
                                 "select the container's Python 3.10 or newer as the control helper "
                                 "(execution.helper_python), on a kernel with procfs child "
                                 "accounting")
            else:
                # On this host the library's own Python supervises: no helper to select.
                report = _result(scope, selected, Outcome.MISSING_SETUP, "supervision",
                                 f"this host cannot supervise the project's commands: {error}",
                                 "run the library with Python 3.10 or newer on a Linux kernel "
                                 "with procfs child accounting (/proc/<pid>/task/<tid>/children) "
                                 "and child adoption (PR_SET_CHILD_SUBREAPER)")
            if unresolved is not None:
                object.__setattr__(report, "_langadapter_linux_command", unresolved)
        except LinkRefused as error:
            _preserve_owner(error)
            report = _result(scope, selected, Outcome.MISSING_SETUP, "inputs", str(error),
                             "select the directory the link points to, or put the file or directory itself "
                             "in place of the link, then run the check again")
        except (FileNotFoundError, NotADirectoryError) as error:
            _preserve_owner(error)
            report = _result(scope, selected, Outcome.MISSING_SETUP, "inputs", "a selected project input is missing",
                             "check that the selected project directory and files exist, then run the check again")
        except OSError as error:
            _preserve_owner(error)
            if not _host.runs_commands():
                report = _result(scope, selected, Outcome.UNSUPPORTED, "assessment", _host.refusal(),
                                 "run the check on a system that runs processes, such as "
                                 "Windows, Linux or macOS")
            else:
                report = _result(scope, selected, Outcome.INFRASTRUCTURE_FAILURE, "assessment",
                                 f"the check stopped on a system error ({type(error).__name__})",
                                 "check that the project directory is readable, then run the check again")
    object.__setattr__(report, "_redactions", _redactions(request.context))
    return report
