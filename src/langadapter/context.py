"""The explicit description of a project, its toolchain and its inputs.

An adapter instance belongs to one language and one project context and has
no configuration file: the context says where the project, its interpreter
and tools are and where its commands run, and its environment holds the
adapter's own ``LANGADAPTER_`` settings. A local project command starts from
the calling process's environment overlaid with ``environment``, unless that
is ``Environment(values, inherit=False)``. ``ProjectInputSelection`` names the
project's inputs as exact paths, with no rule that finds them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Iterator, Mapping, Optional, Tuple, Union
from langadapter import _host
from langadapter._input_copy import derived
from langadapter.errors import InvalidRequest
from langadapter.outcomes import _relative_posix_path


class Environment(Mapping[str, str]):
    """Immutable command variables over the caller's environment, or alone with
    ``inherit=False``. An Environment copied without a mode keeps its mode."""

    def __init__(self, values: Optional[Mapping[str, str]] = None, *,
                 inherit: Optional[bool] = None) -> None:
        if inherit is None:
            inherit = values.inherit if isinstance(values, Environment) else True
        if type(inherit) is not bool:
            raise InvalidRequest("environment.inherit", "must be a boolean")
        self._items = tuple(sorted(dict(values or {}).items()))
        self._map = MappingProxyType(dict(self._items))
        self._inherit = inherit

    @property
    def inherit(self) -> bool:
        """Whether the variables extend the caller's environment, False
        when they stand alone."""
        return self._inherit

    def __getitem__(self, key: str) -> str:
        return self._map[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._map)

    def __len__(self) -> int:
        return len(self._map)

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __hash__(self) -> int:
        return hash(self._items) if self.inherit else hash((self._items, False))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Environment):
            return self.inherit == other.inherit and self._items == other._items
        if isinstance(other, Mapping):
            return self.inherit and self._map == dict(other)
        return NotImplemented

    def __repr__(self) -> str:
        if not self.inherit:
            return f"Environment(keys={tuple(self._map)!r}, inherit=False)"
        return f"Environment({dict(self._map)!r})"


@dataclass(frozen=True)
class ProvisioningRequest:
    """An interpreter the adapter provisions only when asked to: a language
    version and an optional requirements file for the environment.
    ``version`` is that version, and ``requirements`` the path of that
    file."""

    version: str
    requirements: Optional[str] = None


@dataclass(frozen=True)
class Interpreter:
    """The project's interpreter: a path (in a container, a path there) or a provisioning request."""

    path: Optional[str] = None
    provisioning: Optional[ProvisioningRequest] = None


@dataclass(frozen=True)
class LocalExecution:
    """Project commands run as processes of this machine. ``work_dir`` holds
    the adapter's temporary directories (None: the system's default)."""

    work_dir: Optional[str] = None

    @property
    def kind(self) -> str:
        """The execution's kind, ``local``."""
        return "local"


@dataclass(frozen=True)
class ContainerExecution:
    """Project commands run inside a running container through its runner.

    ``work_dir`` is the shared work directory: the host directory shared with the
    container, holding every file handed to a command there. The container sees
    it at ``container_work_dir``: by default the same path, or ``/workzone`` for
    a Windows host path. One under the host path, or containing it, is refused.
    ``repository_dir`` is the project's source root in the container, ``image``
    its image for observation identity. ``helper_python`` selects the helper
    Python, the container's Python 3.10 or newer that supervises commands. The
    project's interpreter can serve, since the helper runs isolated and without
    site (no .pth files or sitecustomize). Its Linux facilities are checked
    before it starts a command, and with it none runs unsupervised. LangAdapter
    installs no helper. Without one, commands run unsupervised through the
    runner's ``exec``, which captured environment variables, Java operations
    and ``prepare_project`` refuse."""

    container: str
    work_dir: str
    runner: str = "docker"
    repository_dir: Optional[str] = None
    image: Optional[str] = None
    container_work_dir: Optional[str] = None
    helper_python: Optional[str] = None
    #: The host runner's captured environment, never the container's target values.
    control_environment: Optional[Environment] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.control_environment is not None:
            object.__setattr__(self, "control_environment",
                               Environment(self.control_environment, inherit=False))

    @property
    def kind(self) -> str:
        """The execution's kind, ``container``."""
        return "container"


Execution = Union[LocalExecution, ContainerExecution]
"""Where a context's commands run: on this machine (``LocalExecution``) or in
a container (``ContainerExecution``), the type of ``ProjectContext.execution``."""

#: Where a test framework's configuration comes from.
_TEST_FRAMEWORK_SOURCES = ("project", "isolated")


@dataclass(frozen=True)
class TestFrameworkConfig:
    """Which configuration the test framework uses: ``project`` (the project's
    own, found in the framework's order or at ``path``) or ``isolated`` (its defaults)."""

    __test__ = False  # not a test class for pytest's collection

    source: str = "project"
    path: Optional[str] = None


@dataclass(frozen=True)
class _Derivation:
    """The variables and the work directory a derived context adds to the one
    it was made from: ``added`` as they run, an extended variable with the
    context's own value after the given one, and ``given`` as the caller
    gave them, which observations record."""

    added: Tuple[Tuple[str, str], ...]
    work_dir: str
    given: Tuple[Tuple[str, str], ...]

    def fields(self, base: "ProjectContext") -> dict:
        """The fields of the context derived from ``base``."""
        environment = Environment({**base.environment, **dict(self.added)},
                                  inherit=base.environment.inherit)
        return {"environment": environment,
                "execution": replace(base.execution, work_dir=self.work_dir),
                "_derivation": self}

    def record(self) -> Tuple[Tuple[str, str], ...]:
        """The given variables as an observation records them: the user's
        own part of an extended variable stays out."""
        return tuple((name, value.replace(self.work_dir, "<work_dir>"))
                     for name, value in self.given)


def _variable_key(name: str) -> str:
    """A variable's name as the system compares it."""
    return name.upper() if os.name == "nt" else name


def _added_variables(environment: Mapping[str, str]) -> dict:
    if not isinstance(environment, Mapping) or not environment:
        raise InvalidRequest("environment", "must name at least one variable")
    added = dict(environment)
    for name, value in added.items():
        if (not isinstance(name, str) or not name or "=" in name
                or not isinstance(value, str) or "\0" in value):
            raise InvalidRequest("environment", f"not a variable and its text: {name!r}")
    return added


def _derived_variables(environment: Mapping[str, str], own: Mapping[str, str],
                       extend: Optional[Mapping[str, str]]) -> Tuple[dict, dict]:
    """``environment``'s variables as given and as a context derived from one
    that sets ``own`` sets them (``ProjectContext.derived``): a variable
    ``own`` sets is refused unless ``extend`` names it with its separator,
    and then the given value comes first and ``own``'s follows."""
    given = _added_variables(environment)
    if not all(isinstance(value, str) and value for value in (extend or {}).values()):
        raise InvalidRequest("extend", "must map each variable to its separator")
    separators = {_variable_key(name): value for name, value in (extend or {}).items()}
    own = {_variable_key(name): value for name, value in own.items()}
    clash = sorted(name for name in given
                   if _variable_key(name) in own and _variable_key(name) not in separators)
    if clash:
        raise InvalidRequest("environment", f"the context already sets {', '.join(clash)}")
    added = {name: (value + separators[_variable_key(name)] + own[_variable_key(name)]
                    if _variable_key(name) in separators and _variable_key(name) in own
                    else value)
             for name, value in given.items()}
    return given, added


@dataclass(frozen=True)
class ProjectContext:
    """A project and its toolchain.

    ``root`` is the project's root as the executing environment sees it (local,
    or inside the container). ``interpreter_flags`` go to every interpreter the
    adapter starts for the project, except the processes of Cosmic Ray itself,
    Python's mutation tool. The test runs those processes start take them.
    ``environment`` overlays every project command's environment, or replaces
    it as ``Environment(..., inherit=False)``. ``runtime_version``, when given,
    is reported instead of asking the interpreter. ``interpreter=None`` is
    source-only use. Executing operations need one. For local execution a
    relative ``root``, and a relative interpreter path with a directory, are
    made absolute from the current directory when the context is made."""

    root: str
    interpreter: Optional[Interpreter] = None
    execution: Execution = field(default_factory=LocalExecution)
    test_framework: TestFrameworkConfig = field(
        default_factory=TestFrameworkConfig)
    interpreter_flags: Tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=Environment)
    runtime_version: Optional[str] = None
    _prepared_copy_binding: object = field(default=None, repr=False)
    #: What ``derived`` added to the context it was made from, or None.
    _derivation: object = field(default=None, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "interpreter_flags",
                           tuple(self.interpreter_flags))
        # Commands run in other directories, so a relative local root is resolved
        # now, and so is a relative local interpreter path. A bare program name
        # has no directory and stays a name the command's PATH finds.
        local = isinstance(self.execution, LocalExecution)
        if isinstance(self.root, str) and self.root and local and not os.path.isabs(self.root):
            object.__setattr__(self, "root", os.path.abspath(self.root))
        path = getattr(self.interpreter, "path", None)
        if (isinstance(self.interpreter, Interpreter) and isinstance(path, str) and local
                and os.path.dirname(path) and not os.path.isabs(path)):
            object.__setattr__(self, "interpreter",
                               replace(self.interpreter, path=os.path.abspath(path)))
        if not isinstance(self.environment, Environment):
            object.__setattr__(self, "environment",
                               Environment(self.environment))

    def validate(self) -> None:
        """Refuse an invalid or incomplete field, naming it, without starting a
        process: local paths must exist, container paths must be absolute."""
        self._validate(check_paths=True)

    def derived(self, *, environment: Mapping[str, str], work_dir: str,
                extend: Optional[Mapping[str, str]] = None) -> "ProjectContext":
        """A context for an adapter that runs another adapter on this project
        (a C adapter running Python tests through Python's): the same root,
        interpreter, flags and framework, ``environment``'s variables added
        to this context's, and ``work_dir``, a directory the caller made, as the
        local work directory. A variable this context sets itself is refused,
        unless ``extend`` names it with its separator, such as
        ``{"PYTHONPATH": os.pathsep}``: the added value comes first and this
        context's value follows. A variable of the caller's that this context
        inherits is overlaid, as its own variables overlay the caller's. A
        container context is refused. Made from the context a prepared use
        yields, it belongs to that use, as that context does. The owned
        directories made in its work directory keep their creation receipts there
        (``work_dir_tempdir``). Its observations record the variables as
        given (``ExecutionIdentity.environment``), without this context's
        part of an extended one, and with the work directory's path written
        ``<work_dir>``."""
        if not isinstance(self.execution, LocalExecution):
            raise InvalidRequest("execution", "a derived context needs local execution")
        if self._derivation is not None:
            raise InvalidRequest("environment", "a derived context is not derived again")
        given, added = _derived_variables(environment, self.environment, extend)
        _require_text(work_dir, "work_dir")
        absolute_dir = os.path.abspath(work_dir)
        if not Path(absolute_dir).is_dir():
            raise InvalidRequest("work_dir", f"no directory at {absolute_dir}")
        derivation = _Derivation(tuple(sorted(added.items())), absolute_dir,
                                 tuple(sorted(given.items())))
        return replace(self, **derivation.fields(self))

    def _validate(self, *, check_paths):
        """Common structural checks. Setup assesses missing paths separately."""
        container = isinstance(self.execution, ContainerExecution)
        if not isinstance(self.execution, (LocalExecution, ContainerExecution)):
            raise InvalidRequest(
                "execution", "must be LocalExecution or ContainerExecution")
        _require_text(self.root, "root")
        if container:
            _require_posix_absolute(self.root, "root")
        elif check_paths and not Path(self.root).is_dir():
            raise InvalidRequest("root", f"no directory at {self.root}")

        interpreter = self.interpreter
        if interpreter is not None and not isinstance(interpreter, Interpreter):
            raise InvalidRequest("interpreter", "must be an Interpreter or None")
        if interpreter is not None and not _host.runs_commands():
            raise InvalidRequest("execution", _host.refusal())
        if interpreter is not None and not _host.confirms_cleanup():
            from langadapter import _process_tree as _posix_group
            _posix_group.warn_once()
        if (interpreter is not None
                and (interpreter.path is None) == (interpreter.provisioning is None)):
            raise InvalidRequest(
                "interpreter", "give either a path or a provisioning request")
        if interpreter is not None and interpreter.path is not None:
            _require_text(interpreter.path, "interpreter.path")
            if container:
                _require_posix_absolute(interpreter.path, "interpreter.path")
            elif check_paths and _host.started_file(interpreter.path) is None:
                raise InvalidRequest(
                    "interpreter.path", f"no file at {interpreter.path}")
        elif interpreter is not None:
            if not isinstance(interpreter.provisioning, ProvisioningRequest):
                raise InvalidRequest("interpreter.provisioning", "must be a ProvisioningRequest")
            _require_text(interpreter.provisioning.version,
                          "interpreter.provisioning.version")

        if container:
            execution = self.execution
            _require_text(execution.container, "execution.container")
            _require_text(execution.runner, "execution.runner")
            _require_text(execution.work_dir, "execution.work_dir")
            if not os.path.isabs(execution.work_dir):
                raise InvalidRequest(
                    "execution.work_dir", "must be an absolute path")
            if execution.repository_dir is not None:
                _require_posix_absolute(execution.repository_dir,
                                        "execution.repository_dir")
            if execution.container_work_dir is not None:
                _require_posix_absolute(execution.container_work_dir,
                                        "execution.container_work_dir")
                # Some arguments are mapped to the container path twice: with
                # one directory under the other, a mapped path would map again.
                host = execution.work_dir.replace("\\", "/").rstrip("/")
                inside = execution.container_work_dir.rstrip("/")
                if host != inside and (inside.startswith(host + "/") or host.startswith(inside + "/")):
                    raise InvalidRequest(
                        "execution.container_work_dir",
                        "must be the shared work directory's own path or lie outside it, and "
                        "must not contain it")
            if execution.helper_python is not None:
                _require_text(execution.helper_python, "execution.helper_python")
                _require_posix_absolute(execution.helper_python, "execution.helper_python")
                if ("\\" in execution.helper_python or "\0" in execution.helper_python
                        or ".." in PurePosixPath(execution.helper_python).parts):
                    raise InvalidRequest("execution.helper_python",
                                         "must be a normalized absolute path of a Python in the container")
        elif check_paths and self.execution.work_dir is not None and not Path(
                self.execution.work_dir).is_dir():
            raise InvalidRequest(
                "execution.work_dir",
                f"no directory at {self.execution.work_dir}")

        framework = self.test_framework
        if not isinstance(framework, TestFrameworkConfig):
            raise InvalidRequest("test_framework", "must be a TestFrameworkConfig")
        if framework.source not in _TEST_FRAMEWORK_SOURCES:
            raise InvalidRequest(
                "test_framework.source",
                f"must be one of {', '.join(_TEST_FRAMEWORK_SOURCES)}")
        if framework.path is not None and framework.source != "project":
            raise InvalidRequest(
                "test_framework.path",
                "names a configuration file, which only the project "
                "source uses")
        if framework.path is not None:
            _require_text(framework.path, "test_framework.path")
        for flag in self.interpreter_flags:
            if not isinstance(flag, str) or not flag:
                raise InvalidRequest(
                    "interpreter_flags", "must be non-empty strings")
        for key, value in self.environment.items():
            if not isinstance(key, str) or not key or not isinstance(
                    value, str):
                raise InvalidRequest(
                    "environment", "must map non-empty names to strings")
        if container and self.execution.control_environment is not None:
            for key, value in self.execution.control_environment.items():
                if not isinstance(key, str) or not key or not isinstance(value, str):
                    raise InvalidRequest("execution.control_environment",
                                         "must map non-empty names to strings")

    def within_work_dir(self, path: str) -> bool:
        """Whether ``path`` lies in the shared work directory (always true locally)."""
        if not isinstance(self.execution, ContainerExecution):
            return True
        work_dir = Path(self.execution.work_dir).resolve()
        try:
            Path(path).resolve().relative_to(work_dir)
        except ValueError:
            return False
        return True

    def _require_execution(self) -> None:
        from langadapter.preparation import _require_context

        _require_context(self)


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequest(name, "is required")


def _require_posix_absolute(value: str, name: str) -> None:
    if not PurePosixPath(value).is_absolute():
        raise InvalidRequest(
            name, f"must be an absolute path inside the container: {value}")


def _paths(values, field):
    if isinstance(values, (str, bytes)):
        raise InvalidRequest(field, "requires a sequence of relative POSIX paths")
    try:
        paths = tuple(values)
    except TypeError:
        raise InvalidRequest(field, "requires a sequence of relative POSIX paths") from None
    for value in paths:
        if (not _relative_posix_path(value) or "\0" in value
                or any(char in value for char in "*?[]")
                or any(part in ("", ".", "..") for part in value.split("/"))):
            raise InvalidRequest(field, "requires normalized relative POSIX paths, not patterns")
    return tuple(sorted(set(paths)))


def _covers(parent, child):
    return child == parent or child.startswith(parent + "/")


@dataclass(frozen=True)
class ProjectInputSelection:
    """Entries to leave out of the project's inputs, and paths that must be present.

    Paths are exact, never globs. An excluded directory leaves out its
    subtree. Python's bytecode caches (``__pycache__``, ``.pyc``, ``.pyo``)
    are always left out, as the copy leaves them out. Excluding a required path,
    or requiring such a cache, raises InvalidRequest. Call ``effective`` (case rule, inputs known to be required)
    before walking or copying the tree, and keep the project unchanged
    meanwhile. The object never reads the file system."""

    exclude: tuple[str, ...] = ()
    required: tuple[str, ...] = ()

    def __post_init__(self):
        excluded, required = _paths(self.exclude, "inputs.exclude"), _paths(self.required, "inputs.required")
        excluded = tuple(value for value in excluded if not any(
            other != value and _covers(other, value) for other in excluded))
        object.__setattr__(self, "exclude", excluded)
        object.__setattr__(self, "required", required)
        self._validate(True)

    def _validate(self, case_sensitive):
        key = (lambda value: value) if case_sensitive else str.casefold
        for required in self.required:
            if any(derived(part) for part in required.split("/")):
                raise InvalidRequest("inputs.required", f"the copy leaves out Python's bytecode "
                                     f"caches: {required}")
            if any(_covers(key(excluded), key(required)) for excluded in self.exclude):
                raise InvalidRequest("inputs.exclude", f"exclusion removes required input: {required}")

    def effective(self, required=(), *, case_sensitive=True):
        """This selection with the project-relative paths ``required`` added
        to its required paths, checked with ``case_sensitive`` as the case
        rule. An exclusion that removes a required path raises
        ``InvalidRequest``."""
        if type(case_sensitive) is not bool:
            raise InvalidRequest("case_sensitive", "must be bool")
        result = ProjectInputSelection(self.exclude, (*self.required, *_paths(required, "inputs.required")))
        result._validate(case_sensitive)
        return result

    def excludes(self, relative, *, case_sensitive=True):
        """Whether the selection leaves out the project-relative path
        ``relative``: a bytecode cache, or a path an excluded entry covers,
        compared with ``case_sensitive`` as the case rule."""
        if type(case_sensitive) is not bool:
            raise InvalidRequest("case_sensitive", "must be bool")
        value, = _paths((relative,), "input path")
        key = (lambda path: path) if case_sensitive else str.casefold
        return (any(derived(part) for part in value.split("/"))
                or any(_covers(key(excluded), key(value)) for excluded in self.exclude))

    def to_dict(self):
        """The selection as plain data: its ``exclude`` and ``required``
        lists."""
        return {"exclude": list(self.exclude), "required": list(self.required)}
