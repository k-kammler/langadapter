"""The texts the Python runs write for pytest and the project's interpreter:
the plugins they load with ``-p`` (import origin, journal, leak observer,
run record, eviction shield, evidence times, the run as delivered, paths)
and the import probe, with the names they read (``ENV_PREFIX`` and the
other constants) and the functions that render them, and the identity of
the determinism instrument a delivered suite carries. A change to these bytes changes what a
measurement runs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
import hashlib
from langadapter.execution import ENV_PREFIX
from langadapter.outcomes import InstrumentIdentity
from langadapter.python.project import _INSTRUMENT_MARKER
from langadapter.suites import TestId


# Where a measured run executed the module under test from. An editable
# install's import hook, an installed copy ahead of the copy's source directory,
# or a compiled module beside the target makes the tests execute the module
# from another file, and the copy reads as never run (0% coverage, every
# mutant surviving). The suite run and the mutation session's check run load
# ``ORIGIN_PLUGIN``, which records the file each of the module's names
# executes from, and ``origin_refusal`` refuses a measurement whose tests
# never executed the module from the copy. A module the tests never imported
# is no refusal: its 0% is the measurement. A package that needs compiled
# modules the copy does not build is not supported.

#: The module name of the plugin.
ORIGIN_PLUGIN = "langadapter_origin"

#: The plugin texts' one rule for where a file lies: ``_norm`` and ``_below``
#: and the standard library's and the installed packages' directories, inserted
#: at each text's ``@LOCATIONS@``.
_LOCATIONS = r'''
def _norm(path):
    return os.path.normcase(os.path.realpath(path))


def _below(path, directory):
    return path == directory or path.startswith(directory.rstrip(os.sep) + os.sep)


_PATHS = sysconfig.get_paths()
_STDLIB = [_norm(_PATHS[key]) for key in ("stdlib", "platstdlib") if _PATHS.get(key)]
_SITE = [_norm(_PATHS[key]) for key in ("purelib", "platlib") if _PATHS.get(key)]
try:
    import site
    _SITE += [_norm(path) for path in site.getsitepackages()]
    _SITE.append(_norm(site.getusersitepackages()))
except Exception:
    pass
'''


#: The plugin. The module's names are its dotted path from the outer end of
#: its ``__init__`` chain, then from each directory between the project root
#: and that package. A name counts as the module's when its directory is the
#: project root, a directory directly in it or on the import path. A deeper
#: directory's (such as the library an instrumentation package in namespace
#: directories is named after) is another module's. A finder put first asks the
#: others in turn for each such name, records the file the found module
#: executes from and returns their answer unchanged. For the target's
#: packages it records compiled modules loaded from outside the copy and
#: those missing from the copy but built elsewhere on the import path. A
#: module imported without the finder (by path, or by a finder put ahead of
#: it) is read from its spec in ``sys.modules`` after collection, after each
#: test and at the end, when the record is written. Standard-library and
#: built-in modules are not the project's.
_ORIGIN_PLUGIN_TEXT = r'''
import importlib.machinery
import json
import os
import sys
import sysconfig
import types

_TARGET = os.environ.get("@PREFIX@IMPORT_ORIGIN_TARGET") or ""
_ROOT = os.environ.get("@PREFIX@IMPORT_ORIGIN_ROOT") or ""
_RECORD = os.environ.get("@PREFIX@IMPORT_ORIGIN_RECORD") or ""
_COMPILED_SUFFIXES = tuple(importlib.machinery.EXTENSION_SUFFIXES)
_STANDARD_LOADERS = ("_frozen_importlib", "_frozen_importlib_external", "importlib.machinery",
                     "importlib.util")


@LOCATIONS@


def _names(target, root):
    """Each name of the module, with the directory it is taken from."""
    module, root = os.path.realpath(target), os.path.realpath(root)
    stem = os.path.splitext(os.path.basename(module))[0]
    parts = [] if stem == "__init__" else [stem]
    directory = os.path.dirname(module)

    def inside(path):
        return _below(_norm(path), _norm(root)) and _norm(path) != _norm(root)

    while inside(directory) and os.path.isfile(os.path.join(directory, "__init__.py")):
        parts.insert(0, os.path.basename(directory))
        directory = os.path.dirname(directory)
    names = {".".join(parts): directory} if parts else {}
    while inside(directory):
        parts.insert(0, os.path.basename(directory))
        directory = os.path.dirname(directory)
        names[".".join(parts)] = directory
    return {name: base for name, base in names.items()
            if all(part.isidentifier() for part in name.split("."))}


_NAMES = _names(_TARGET, _ROOT) if _TARGET and _ROOT else {}
# The packages holding the module, each with the directory its name is taken
# from: their modules are the target's package's.
_PACKAGES = {}
for _name, _base in _NAMES.items():
    _parts = _name.split(".")
    for _end in range(1, len(_parts)):
        _PACKAGES.setdefault(".".join(_parts[:_end]), _base)
    if os.path.basename(_TARGET).startswith("__init__."):
        _PACKAGES.setdefault(_name, _base)
_SEEN = {}
_LOOKED = set()
_MISSING = {}
_COMPILED = {}
# The target's packages an import hook provided.
_HOOKED = set()


def _installed(path):
    return any(_below(path, directory) for directory in _SITE)


def _counted(base):
    """Whether a name taken from ``base`` is the module's own: ``base`` is
    the project root, a directory directly in it, or on the import path (as
    the copy's directory, or the original tree's of an editable install)."""
    directory, root = _norm(base), _norm(_ROOT)
    if directory == root or _norm(os.path.dirname(os.path.realpath(base))) == root:
        return True
    relative = os.sep + os.path.relpath(directory, root)
    for entry in list(sys.path):
        try:
            seen = _norm(entry or os.getcwd())
        except (OSError, TypeError, ValueError):
            continue
        if seen == directory or seen.endswith(relative):
            return True
    return False


def _event(spec, base, hook):
    """What executing ``spec`` for a name taken from ``base`` runs, None for
    a standard-library or built-in module."""
    origin = spec.origin
    if origin in ("built-in", "frozen"):
        return None
    file = origin if isinstance(origin, str) and os.path.isfile(origin) else ""
    if not file:
        # A namespace package, or a hook's module that names no file.
        locations = list(spec.submodule_search_locations or ())
        file = str(locations[0]) if locations else str(origin or "")
        return {"file": file, "copy": False, "counted": _counted(base), "installed": False,
                "compiled": False, "beside": False, "hook": hook}
    seen = _norm(file)
    if any(_below(seen, directory) for directory in _STDLIB) and not _installed(seen):
        return None
    return {"file": os.path.realpath(file), "copy": seen == _norm(_TARGET),
            "counted": _counted(base), "installed": _installed(seen),
            "compiled": file.endswith(_COMPILED_SUFFIXES),
            "beside": os.path.dirname(seen) == os.path.dirname(_norm(_TARGET)),
            "hook": hook}


def _note(name, event):
    if event is not None:
        known = _SEEN.setdefault(name, [])
        if event not in known:
            known.append(event)


def _built(name):
    """A compiled module ``name`` on the import path, or ""."""
    parts = name.split(".")
    for entry in list(sys.path):
        try:
            files = os.listdir(os.path.join(entry or os.getcwd(), *parts[:-1]))
        except (OSError, TypeError, ValueError):
            continue
        for file in sorted(files):
            if file.startswith(parts[-1] + ".") and file.endswith(_COMPILED_SUFFIXES):
                return os.path.realpath(os.path.join(entry or os.getcwd(), *parts[:-1], file))
    return ""


def _hooked(name):
    """Whether a package holding ``name`` came from an import hook: the
    path finder then finds ``name`` where the hook's package lies."""
    parts = name.split(".")
    return any(".".join(parts[:end]) in _HOOKED for end in range(1, len(parts)))


def _part(name, spec):
    """A module of the target's packages: a compiled one loaded from outside
    the copy, or one the copy lacks that is built elsewhere."""
    package = name.rpartition(".")[0]
    if not _counted(_PACKAGES[package]):
        return
    if spec is None:
        built = _built(name)
        if built:
            _MISSING[name] = built
    elif (isinstance(spec.origin, str) and spec.origin.endswith(_COMPILED_SUFFIXES)
          and not _below(_norm(spec.origin), _norm(_ROOT))):
        _COMPILED[name] = os.path.realpath(spec.origin)


class _Watch:
    """Finds nothing itself: for a name of the module under test or a
    module of its packages it asks the finders after it, records what they
    found and returns their answer unchanged."""

    def __init__(self):
        self._asking = set()

    def find_spec(self, name, path=None, target=None):
        package = name.rpartition(".")[0]
        if name in self._asking or (name not in _NAMES and name not in _PACKAGES
                                    and package not in _PACKAGES):
            return None
        finders = list(sys.meta_path)
        if self not in finders:
            return None
        self._asking.add(name)
        try:
            spec, finder = None, None
            for finder in finders[finders.index(self) + 1:]:
                find = getattr(finder, "find_spec", None)
                if find is None:
                    # A finder of the old protocol: the import system asks it.
                    return None
                # A finder's error reaches the import as it would without
                # this finder.
                spec = find(name, path, target)
                if spec is not None:
                    break
        finally:
            self._asking.discard(name)
        try:
            hook = spec is not None and finder is not importlib.machinery.PathFinder
            if hook and name in _PACKAGES:
                _HOOKED.add(name)
            if name in _NAMES:
                if spec is not None:
                    _note(name, _event(spec, _NAMES[name], hook or _hooked(name)))
            elif package in _PACKAGES:
                _part(name, spec)
        except Exception:
            pass
        return spec


def _look():
    """The names the finder never saw, from the spec of the module in
    ``sys.modules``."""
    for name, base in _NAMES.items():
        module = sys.modules.get(name)
        if name in _SEEN or name in _LOOKED or not isinstance(module, types.ModuleType):
            continue
        _LOOKED.add(name)
        # The module's own namespace, read without its attribute lookup: a
        # lazily loaded module is executed by its first attribute access.
        spec = object.__getattribute__(module, "__dict__").get("__spec__")
        if spec is None:
            continue
        hook = type(spec.loader).__module__ not in _STANDARD_LOADERS or _hooked(name)
        _note(name, _event(spec, base, hook))


if _NAMES:
    sys.meta_path.insert(0, _Watch())


def pytest_collection_finish(session):
    _look()


def pytest_runtest_logfinish(nodeid, location):
    _look()


def pytest_unconfigure(config):
    _look()
    if _RECORD:
        with open(_RECORD, "w", encoding="utf-8") as handle:
            json.dump({"target": _TARGET, "names": sorted(_NAMES), "seen": _SEEN,
                       "missing": _MISSING, "compiled": _COMPILED}, handle)
'''.replace("@LOCATIONS@", _LOCATIONS)

#: The module name of the plugin of a run in a prepared copy that names no
#: module under test.
COPY_ORIGIN_PLUGIN = "langadapter_copy_origin"

#: The directories directly in a project's root that hold its importable code
#: beside the root itself, as runs and the copy's origin plugin take them.
SOURCE_DIRS = ("src", "lib", "python")

#: That plugin. At start it takes the copy's import roots: the root, its
#: ``SOURCE_DIRS`` and the import path's directories inside the copy, none of
#: them a package or inside a directory it is told to skip (the suite's and the
#: run's own). After collection and at the end it reads ``sys.modules`` once
#: and records each module executed from a file outside the copy whose full
#: name the copy holds under one of those roots, in the form of
#: ``ORIGIN_PLUGIN``'s record. Standard-library modules are left out. A module
#: whose top-level package the import path does not lead to came through an
#: import hook, and its event says ``first`` when a finder ahead of the path
#: finder gives that package's location. It installs no import hook itself.
_COPY_ORIGIN_PLUGIN_TEXT = r'''
import importlib.machinery
import json
import os
import sys
import sysconfig
import types

_ROOT = os.environ.get("@PREFIX@IMPORT_ORIGIN_ROOT") or ""
_RECORD = os.environ.get("@PREFIX@IMPORT_ORIGIN_RECORD") or ""
_COMPILED_SUFFIXES = tuple(importlib.machinery.EXTENSION_SUFFIXES)


@LOCATIONS@


def _entries(directory):
    try:
        return os.listdir(directory)
    except OSError:
        return []


def _where(spec):
    # The files or directories a module's spec leads to.
    origin = getattr(spec, "origin", None)
    if isinstance(origin, str) and os.path.isfile(origin):
        return {_norm(origin)}
    return {_norm(path) for path in (getattr(spec, "submodule_search_locations", None) or ())}


def _spec(module):
    # Read without the module's attribute lookup, which may load a lazy module.
    if not isinstance(module, types.ModuleType):
        return None
    return object.__getattribute__(module, "__dict__").get("__spec__")


_COPY = _norm(_ROOT) if _ROOT else ""
_SKIPPED = [_norm(path) for path in json.loads(os.environ.get("@PREFIX@IMPORT_ORIGIN_SKIP") or "[]")]
_ROOTS = []
if _COPY:
    for _dir in [_ROOT, *(os.path.join(_ROOT, name) for name in @SOURCE_DIRS@), *sys.path]:
        try:
            _dir = _norm(_dir or os.getcwd())
        except (OSError, TypeError, ValueError):
            continue
        if (_dir not in _ROOTS and _below(_dir, _COPY) and os.path.isdir(_dir)
                and not os.path.isfile(os.path.join(_dir, "__init__.py"))
                and not any(_below(_dir, skipped) for skipped in _SKIPPED)):
            _ROOTS.append(_dir)
# The top-level names the roots provide, which every held module starts with.
_NAMES = {entry.split(".")[0] for directory in _ROOTS for entry in _entries(directory)}
_SEEN = {}


def _held(name):
    # Whether the copy holds a file for the full name under one of its roots.
    parts = name.split(".")
    for directory in _ROOTS:
        base = os.path.join(directory, *parts)
        if os.path.isfile(base + ".py") or os.path.isfile(os.path.join(base, "__init__.py")):
            return True
        if any(entry.startswith(parts[-1] + ".") and entry.endswith(_COMPILED_SUFFIXES)
               for entry in _entries(os.path.dirname(base))):
            return True
    return False


_FIRST = {}


def _first(top, imported):
    # Whether a finder ahead of PathFinder gives the location ``top`` was
    # imported from: a hook placed first, which wins over any import path.
    # Each finder is asked once for each top-level package.
    if top not in _FIRST:
        where = _where(imported)
        _FIRST[top] = False
        for finder in sys.meta_path:
            if finder is importlib.machinery.PathFinder:
                break
            try:
                spec = finder.find_spec(top, None, None)
            except Exception:
                continue
            if spec is not None and where & _where(spec):
                _FIRST[top] = True
                break
    return _FIRST[top]


def _scan():
    for name, module in list(sys.modules.items()):
        top = name.partition(".")[0]
        if name in _SEEN or top not in _NAMES:
            continue
        origin = getattr(_spec(module), "origin", None)
        if not isinstance(origin, str) or not os.path.isfile(origin):
            continue
        seen = _norm(origin)
        installed = any(_below(seen, directory) for directory in _SITE)
        if (_below(seen, _COPY) or any(_below(seen, directory) for directory in _SKIPPED)
                or (any(_below(seen, directory) for directory in _STDLIB) and not installed)
                or not _held(name)):
            continue
        try:
            found = importlib.machinery.PathFinder.find_spec(top)
        except Exception:
            found = None
        imported = _spec(sys.modules.get(top))
        hook = found is None or imported is None or not (_where(found) & _where(imported))
        _SEEN[name] = [{"file": os.path.realpath(origin), "copy": False, "counted": True,
                        "installed": installed, "compiled": origin.endswith(_COMPILED_SUFFIXES),
                        "beside": False, "hook": hook,
                        "first": hook and imported is not None and _first(top, imported)}]


def pytest_collection_finish(session):
    _scan()


def pytest_unconfigure(config):
    _scan()
    if _RECORD:
        with open(_RECORD, "w", encoding="utf-8") as handle:
            json.dump({"target": "", "names": sorted(_NAMES), "seen": _SEEN,
                       "missing": {}, "compiled": {}}, handle)
'''.replace("@LOCATIONS@", _LOCATIONS)


def origin_plugin_text() -> str:
    """The plugin's text with the variable names."""
    return render(_ORIGIN_PLUGIN_TEXT)


def copy_origin_plugin_text() -> str:
    """The text of the plugin of a run that names no module, with
    the variable names."""
    return render(_COPY_ORIGIN_PLUGIN_TEXT).replace("@SOURCE_DIRS@",
                                                    repr(SOURCE_DIRS))


def origin_environment(target: str, root: str, record: str,
                       skipped: Sequence[str] = ()) -> Dict[str, str]:
    """The plugins' variables: the module under test, the project root its
    names are taken from and the record file, as paths the run sees. A run
    that names no module passes an empty target and the directories to skip."""
    prefix = ENV_PREFIX
    variables = {prefix + "IMPORT_ORIGIN_TARGET": target, prefix + "IMPORT_ORIGIN_ROOT": root,
                 prefix + "IMPORT_ORIGIN_RECORD": record}
    if skipped:
        variables[prefix + "IMPORT_ORIGIN_SKIP"] = json.dumps(list(skipped))
    return variables


def read_origin(path: Path) -> Optional[Dict[str, Any]]:
    """The record the plugin wrote at ``path``, None when it wrote none."""
    try:
        record = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("seen"), dict):
        return None
    return record


def origin_events(record: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    """The modules of an import record with each of their events."""
    return [(name, event) for name, found in sorted(record["seen"].items())
            if isinstance(found, list) for event in found if isinstance(event, dict)]


#: The end of every refusal for a setup this release does not measure.
UNSUPPORTED = "is not supported in this release"


def origin_refusal(record: Optional[Dict[str, Any]], *, collected: bool = True,
                   named: bool = True) -> Optional[str]:
    """Why a run with ``record`` measured another file than the module
    under test, None when it executed the module from the copy or never
    imported it. A missing record is refused. A run that did not collect its
    tests (``collected`` false), never executed the module from the copy and
    lacked a compiled module of the target's package built elsewhere failed
    for want of it. A package that falls back without it runs from the copy
    and keeps its outcome. The advice names a setup that measures the copy
    where there is one: a regular install, which the copy's source directory
    takes precedence over, in place of an editable install's import hook. A
    run that names no module (``named`` false) keeps its import path, and
    its refusal says so where the advice assumes a module."""
    if record is None:
        return (f"where the tests imported {'the module under test' if named else 'the project'}"
                f" from could not be confirmed: the run wrote no import record")
    events = origin_events(record)
    if any(event.get("copy") for _, event in events):
        return None
    missing = record.get("missing") or {}
    if not collected and missing:
        name = sorted(missing)[0]
        return (f"the module under test's package needs the compiled module {name}, built "
                f"at {missing[name]} but not in the measured copy of the project. "
                f"Measuring a project whose compiled modules the copy does not hold "
                f"{UNSUPPORTED}")
    counted = [(name, event) for name, event in events if event.get("counted")]
    if not counted:
        return None
    # The longest name is the module's own, and a shorter one its tail.
    name, event = max(counted, key=lambda item: item[0].count("."))
    where = event.get("file") or "an import hook that names no file"
    if not named and not event.get("hook"):
        kind = ("the installed copy " if event.get("installed")
                else "the compiled module " if event.get("compiled") else "")
        return (f"the tests imported {name} from {kind}{where}, not from the measured copy "
                f"of the project. A run that names no module under test keeps the import "
                f"path as it is: name the module under test to put the copy's directory first")
    if event.get("compiled") and event.get("beside"):
        return (f"the tests imported {name} from {where}, a compiled module beside the "
                f"module under test that the import system loads in its place, so its "
                f"source never runs. Measuring a module replaced by a compiled build "
                f"{UNSUPPORTED}: remove the compiled module to measure the source")
    if event.get("compiled") and named:
        return (f"the tests imported {name} from the compiled module {where}, not from the "
                f"measured copy of the project. Measuring a compiled module under test "
                f"{UNSUPPORTED}")
    compiled = sorted(record.get("compiled") or {})
    if event.get("hook") and compiled:
        return (f"the tests imported {name} from {where} through an import hook of an "
                f"editable install, not from the measured copy of the project, and the "
                f"project has compiled modules ({', '.join(compiled)}). Measuring a project "
                f"with compiled modules {UNSUPPORTED}")
    if event.get("hook"):
        return (f"the tests imported {name} from {where}, not from the measured copy of "
                f"the project: an import hook of an editable install takes precedence "
                f"over the import path. Reinstall the project without editable mode "
                f"(pip install . instead of pip install -e .) to measure it")
    copy = "the installed copy " if event.get("installed") else ""
    return (f"the tests imported {name} from {copy}{where}, not from the measured copy of "
            f"the project, which could not be put ahead of it on the import path. "
            f"Measuring a project laid out this way {UNSUPPORTED}")


#: The prefix of the adapter's temporary directories.
TEMPDIR_PREFIX = "langadapter_"
#: The file name compile errors name.
SOURCE_LABEL = "<test-source>"
#: The eviction shield's module.
SHIELD_MODULE = "_langadapter_eviction_shield"
#: The module of a run as delivered.
DELIVERED_MODULE = "_langadapter_delivered"
#: The ``pytest.ini`` text of a prepared test directory.
SUITE_INI_TEXT = (
    "[pytest]\n"
    "# Anchor pytest's config-file discovery to this test dir,\n"
    "# so the tests do not pick up a stricter configuration from\n"
    "# an ancestor directory.\n"
)


def render(template: str) -> str:
    """``template`` with the variable prefix in place of ``@PREFIX@``."""
    return template.replace("@PREFIX@", ENV_PREFIX)


def node_parts(nodeid: str) -> Tuple[str, List[str], Optional[str]]:
    """A pytest node id's file, its class and function names, and its case's
    parameters or None: ``file::Class::function[params]``. The parameters
    start at the first ``[`` after the file and may hold ``::``, which pytest
    keeps in an id."""
    file, separator, rest = nodeid.partition("::")
    start = rest.find("[")
    if start >= 0 and rest.endswith("]"):
        rest, params = rest[:start], rest[start + 1:-1]
    else:
        params = None
    return file, (rest.split("::") if separator else []), params


def test_of_nodeid(nodeid: str) -> TestId:
    """The test a pytest node id names, relative to the root directory (the
    suite root) (``PythonAdapter.test_of_nodeid``)."""
    file, names, params = node_parts(nodeid)
    return TestId(file=file, qualname=".".join(names), params=params, text=nodeid)


# The per-test timeout journal, the plugin of a run that asks for it
# (``JOURNAL_PLUGIN``). Its hooks write only while
# @PREFIX@TIMEOUT_EVIDENCE_DIR is set.
_JOURNAL_HOOKS = """# Per-test timeout journal. When the mutation run's time limit kills the
# run of one mutant, pytest reports nothing, so the journal tells which test
# was running, detecting or hanging on the mutant. When
# @PREFIX@TIMEOUT_EVIDENCE_DIR is set (the mutation engine sets it for its
# runs of one mutant), each event is written as one JSON line to an
# append-only journal and flushed at once, so a killed process leaves every
# event up to the kill. It is not synced to disk: an fsync per event can take
# tens of milliseconds on a virtual disk, time the per-mutant limit, set by a
# run without the journal, does not include. A test ends at its teardown
# report, whatever its outcome, so a test skipped in setup has ended, and
# a failed report of any phase names a failure. The session's first line
# says so ("ends": "teardown") and holds the sha1 of the module under
# mutation (named by @PREFIX@JOURNAL_MODULE,
# which names it for the journal only, else by @PREFIX@MUTATION_TARGET,
# which also turns on the instrument's per-test restore; resolved against
# @PREFIX@MUTATION_SOURCE_ROOT, else this conftest's source root, else the
# working directory). The engine derives the same sha1 from a job's
# mutation diff, so it pairs each journal block with its job. The sha1 is
# of the module's text as the engine reads its copy (UTF-8, universal
# newlines), so a module with CRLF line endings pairs too. The hooks exist
# in pytest 3 to 8. Nothing is written when the variable is unset.
import os as _os

_EVID_DIR = _os.environ.get("@PREFIX@TIMEOUT_EVIDENCE_DIR")
if _EVID_DIR:
    import hashlib as _hashlib
    import json as _json

    # The path is resolved once at import, before any test runs. The hooks
    # run while a test's patches are in force, and a test may have replaced
    # functions of os (os.fspath among them), so a write calls no os.path
    # function and ignores whatever a replaced function raises. The journal
    # is best effort: the run's verdict never depends on it.
    _EVID_JOURNAL = _os.path.join(_EVID_DIR, "journal.jsonl")

    def _evid_write(obj):
        try:
            with open(_EVID_JOURNAL, "a", encoding="utf-8") as fh:
                fh.write(_json.dumps(obj) + "\\n")
                fh.flush()
        except Exception:
            pass

    def _evid_module_sha():
        rel = (_os.environ.get("@PREFIX@JOURNAL_MODULE")
               or _os.environ.get("@PREFIX@MUTATION_TARGET"))
        if not rel:
            return "unknown"
        # The module under mutation lies under the source root this
        # conftest imports from, which need not be the working directory.
        root = (_os.environ.get("@PREFIX@MUTATION_SOURCE_ROOT")
                or globals().get("_PROJECT_ROOT") or _os.getcwd())
        try:
            with open(_os.path.join(root, rel), encoding="utf-8") as fh:
                return _hashlib.sha1(fh.read().encode("utf-8")).hexdigest()
        except (OSError, ValueError):
            return "unknown"

    def pytest_sessionstart(session):
        try:
            _os.makedirs(_EVID_DIR, exist_ok=True)
        except OSError:
            pass
        _evid_write({"run": _os.getpid(), "module_sha": _evid_module_sha(),
                     "ends": "teardown"})

    def pytest_runtest_logstart(nodeid, location):
        _evid_write({"start": nodeid, "pid": _os.getpid()})

    def pytest_runtest_logreport(report):
        if report.when == "teardown" or report.outcome == "failed":
            _evid_write({"done": report.nodeid, "when": report.when,
                         "outcome": report.outcome, "pid": _os.getpid()})

    def pytest_sessionfinish(session, exitstatus):
        _evid_write({"end": _os.getpid(), "exit": int(exitstatus)})
"""

#: The module name of the journal loaded as a plugin.
JOURNAL_PLUGIN = "langadapter_journal"


def journal_plugin_text() -> str:
    """The journal hooks alone, with the variable names: the plugin
    of a run that journals without the instrument."""
    return render(_JOURNAL_HOOKS)


# The read-only leak observer: the plugin a run without the instrument loads
# (``-p``) when it asks for the leak record. Before each test it copies the
# names of the module under test (@PREFIX@TARGET_MODULE_FILE) and reads the
# options of the libraries already loaded (numpy's error and print settings,
# xarray's options). Once the test's own fixtures are torn down it compares,
# and a test with a difference appends one line to @PREFIX@LEAK_JOURNAL in the
# instrument record's format, so the same leak reads the same. It imports no
# library, restores nothing and swallows its own errors, so it never changes
# a verdict: each test is compared with the state it found.
LEAK_OBSERVER_PLUGIN = "langadapter_leaks"

_LEAK_OBSERVER = '''"""Records the tests that leave the module under test or a loaded
library's options changed. It compares and never restores."""
import json as _json
import os as _os
import sys as _sys

import pytest as _pytest

_JOURNAL = _os.environ.get("@PREFIX@LEAK_JOURNAL")
_TARGET_FILE = _os.environ.get("@PREFIX@TARGET_MODULE_FILE")
try:
    _WANTED = _os.path.realpath(_TARGET_FILE) if _TARGET_FILE else None
    if _WANTED is not None and not _os.path.exists(_WANTED):
        _WANTED = None
except Exception:
    _WANTED = None
_TARGET_MOD = None


def _write(obj):
    try:
        with open(_JOURNAL, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(obj, sort_keys=True) + "\\n")
    except Exception:
        pass


def _target():
    global _TARGET_MOD
    if _TARGET_MOD is None and _WANTED is not None:
        for module in list(_sys.modules.values()):
            path = getattr(module, "__file__", None)
            try:
                if path and _os.path.realpath(path) == _WANTED:
                    _TARGET_MOD = module
                    break
            except Exception:
                continue
    return _TARGET_MOD


def _numpy():
    numpy = _sys.modules.get("numpy")
    try:
        if numpy is not None:
            return dict(numpy.geterr()), dict(numpy.get_printoptions())
    except Exception:
        pass
    return None, None


def _xarray():
    table = getattr(_sys.modules.get("xarray.core.options"), "OPTIONS", None)
    try:
        return dict(table) if table is not None else None
    except Exception:
        return None


def _changed(before, after):
    if before is None or after is None:
        return []
    return sorted(k for k in set(before) | set(after)
                  if k not in before or k not in after
                  or before[k] is not after[k] and before[k] != after[k])


def _named(key):
    # Dunder names are the interpreter's bookkeeping (``__warningregistry__``).
    return not (key.startswith("__") and key.endswith("__"))


if _JOURNAL:
    _write({"record": _os.getpid()})

    @_pytest.fixture(autouse=True)
    def _leak_record(request):
        try:
            module = _target()
            names = dict(module.__dict__) if module is not None else None
            options = _xarray()
            numpy = _numpy()
        except Exception:
            module = names = options = None
            numpy = (None, None)
        yield
        try:
            found = {}
            if names is not None:
                now = module.__dict__
                added = sorted(k for k in now if k not in names and _named(k))
                rebound = sorted(k for k in now if k in names and _named(k)
                                 and now[k] is not names[k])
                removed = sorted(k for k in names if k not in now and _named(k))
                if added or rebound or removed:
                    found["module"] = {"added": added, "rebound": rebound,
                                       "removed": removed}
            changed = _changed(options, _xarray())
            if changed:
                found["xarray_options"] = changed
            errors, printing = _numpy()
            for key, before, after in (("numpy_errors", numpy[0], errors),
                                       ("numpy_print", numpy[1], printing)):
                changed = _changed(before, after)
                if changed:
                    found[key] = changed
            if found:
                found["test"] = request.node.nodeid
                _write(found)
        except Exception:
            pass
'''


def leak_observer_text() -> str:
    """The read-only leak observer, with the variable names: the
    plugin of a run that records leaks without the instrument."""
    return render(_LEAK_OBSERVER)


# The run record: pytest's own account of a run, loaded as a plugin (``-p``),
# so a reader takes a run's counts, the tests that failed and the files pytest
# collected from pytest's objects, not from its terminal text. At the
# session's end it holds each count of pytest's summary line (its reports by
# category, as the line counts them), the tests the short summary names
# FAILED and ERROR (in its spelling, relative to the working directory) and
# the order its report characters list the two in (``-r``, "fE" by default,
# "Ef" under -ra), the collection nodes that failed and the files of the
# collected tests (POSIX, relative to pytest's rootdir). @PREFIX@RUN_RECORD
# names the file it is written to as JSON. "-" prints it instead as one line
# on the process's standard output after pytest's own (``run_record_mark``
# and the JSON), for a run that hands back only its output (a mutant's run).
# Nothing is written without the variable, and the hooks exist in pytest 3
# to 8. A suite run's plugin also records the tests of unsound warnings
# (``_UNSOUND_RECORD``).
RECORD_PLUGIN = "langadapter_record"
#: The text that starts the run record's line in a run's output, rendered
#: with the run's prefix (``run_record_mark``).
RUN_RECORD_MARK = "@PREFIX@RUN_RECORD "

_RUN_RECORD = '''"""pytest's own account of a run, for LangAdapter's readers. It
records and never changes a verdict."""
import json as _json
import os as _os

_TARGET = _os.environ.get("@PREFIX@RUN_RECORD")
_RECORD = {"broken": [], "files": []}


def pytest_collectreport(report):
    if report.failed:
        _RECORD["broken"].append(report.nodeid)


def pytest_collection_finish(session):
    root = str(getattr(session.config, "rootpath", None) or session.config.rootdir)
    for item in session.items:
        path = str(getattr(item, "path", None) or item.fspath)
        try:
            path = _os.path.relpath(path, root).replace(_os.sep, "/")
        except ValueError:
            pass
        if path not in _RECORD["files"]:
            _RECORD["files"].append(path)


def pytest_sessionfinish(session, exitstatus):
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    stats = getattr(reporter, "stats", None) or {}
    name = session.config.cwd_relative_nodeid
    _RECORD["counts"] = {
        key: sum(1 for report in reports
                 if getattr(report, "count_towards_summary", True))
        for key, reports in stats.items() if key}
    _RECORD["failed"] = [name(report.nodeid) for report in stats.get("failed", ())]
    _RECORD["errors"] = [name(report.nodeid) for report in stats.get("error", ())]
    _RECORD["order"] = "".join(char for char in getattr(reporter, "reportchars", "fE")
                               if char in "fE")
    _RECORD["exit"] = int(exitstatus)
    if _TARGET and _TARGET != "-":
        try:
            with open(_TARGET, "w", encoding="utf-8") as handle:
                _json.dump(_RECORD, handle)
        except Exception:
            pass


def pytest_unconfigure(config):
    if _TARGET == "-" and "exit" in _RECORD:
        try:
            _os.write(1, ("\\n@MARK@" + _json.dumps(_RECORD) + "\\n").encode("utf-8"))
        except Exception:
            pass
'''.replace("@MARK@", RUN_RECORD_MARK)


# The record's unsound warnings, added to a suite run's plugin only: each
# warning of a test's phases (setup, call and teardown, pytest's "runtest")
# whose text matches one of the patterns, as [node id, text] under "unsound",
# in the order pytest reports them. The text is the warning as pytest's
# warnings summary prints it, and the node id the one it prints. pytest has
# the hook from 6.0 on, and an older one records none.
_UNSOUND_RECORD = '''

import re as _re
import warnings as _warnings

import pytest as _pytest

_UNSOUND = [_re.compile(_pattern) for _pattern in @UNSOUND_PATTERNS@]


@_pytest.hookimpl(optionalhook=True)
def pytest_warning_recorded(warning_message, when, nodeid, location):
    if when != "runtest" or not nodeid:
        return
    text = _warnings.formatwarning(
        str(warning_message.message), warning_message.category,
        warning_message.filename, warning_message.lineno, warning_message.line)
    if any(pattern.search(text) for pattern in _UNSOUND):
        _RECORD.setdefault("unsound", []).append([nodeid, text])
'''


def run_record_text(unsound_patterns: Sequence[str] = ()) -> str:
    """The run record plugin (``RECORD_PLUGIN``), with the variable names,
    recording the tests of the warnings that match
    ``unsound_patterns`` when there are any."""
    text = render(_RUN_RECORD)
    if unsound_patterns:
        text += _UNSOUND_RECORD.replace("@UNSOUND_PATTERNS@", repr(tuple(unsound_patterns)))
    return text


#: The module name of the suite profile's plugin (``tests.profile_suite``).
PROFILE_PLUGIN = "langadapter_profile"

_PROFILE_PLUGIN = '''"""Profiles each test's setup, call and teardown with cProfile and writes
the session's statistics as JSON when it ends."""
import cProfile as _cProfile
import json as _json
import os as _os
import platform as _platform

import pytest

_TARGET = _os.environ.get("@PREFIX@PROFILE")
_PROFILE = _cProfile.Profile()


def _profiled():
    _PROFILE.enable()
    try:
        yield
    finally:
        _PROFILE.disable()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    yield from _profiled()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    yield from _profiled()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    yield from _profiled()


def pytest_sessionfinish(session, exitstatus):
    if not _TARGET:
        return
    _PROFILE.create_stats()
    rows = [[filename, line, name, calls, own, inclusive] for (filename, line, name),
            (_, calls, own, inclusive, _) in _PROFILE.stats.items()]
    version = _platform.python_implementation() + " " + _platform.python_version()
    # A pytest-xdist worker writes a file of its own beside the controller's.
    # Its configuration names it. The environment variable also reaches a
    # controller started inside another run's worker.
    worker = getattr(session.config, "workerinput", {}).get("workerid")
    with open(_TARGET + ("." + worker if worker else ""), "w", encoding="utf-8") as handle:
        _json.dump({"version": version, "functions": rows}, handle)
'''


def profile_plugin_text() -> str:
    """The suite profile's plugin (``PROFILE_PLUGIN``), with the variable
    names."""
    return render(_PROFILE_PLUGIN)


# The per-test coverage run's shield: coverage.py flushes a test's data when
# its context switches, possibly while a test's replaced built-in (``len``) is
# in force, which would crash the run. The plugin binds the real built-ins,
# captured at load, into each coverage.py module not defining the name, so its
# global lookups find them while the code under test still sees replacements.
# It binds once, at configure time, outside the per-test times.
_EVICTION_SHIELD_PLUGIN = '''"""Binds coverage.py's modules to the real built-in functions. Loaded
into the per-test coverage run only."""
import builtins as _builtins
import sys as _sys

_REAL = {name: getattr(_builtins, name) for name in dir(_builtins)
         if not name.startswith("__")}


def pytest_configure(config):
    for name, module in list(_sys.modules.items()):
        if module is None or not (
                name == "coverage" or name.startswith("coverage.")):
            continue
        namespace = getattr(module, "__dict__", None)
        if isinstance(namespace, dict):
            for builtin_name, value in _REAL.items():
                namespace.setdefault(builtin_name, value)
'''

#: Records each test's time (setup, call and teardown summed) in the per-test
#: coverage run at the clock's resolution (``_eviction_times``), and switches
#: coverage.py's context to the test's node id for its whole run, so every line
#: it causes, fixtures included, is its own (a wider-scoped fixture's in the
#: first and last test using it). Collection and import keep the empty context.
#: As the outermost wrapper (``tryfirst``) it switches outside the phases
#: pytest times. ``@TIMES@`` is its file, which also says whether coverage ran.
_EVIDENCE_TIMES_PLUGIN = '''"""Records each test's time as pytest measures its phases, and runs each
test under a coverage context of its own. Loaded into the per-test coverage
run only."""
import json as _json

import pytest as _pytest

_TIMES = @TIMES@
_DURATIONS = {}


def _measuring():
    try:
        from coverage import Coverage
    except ImportError:
        return None
    return Coverage.current()


@_pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    measuring = _measuring()
    if measuring is not None:
        measuring.switch_context(item.nodeid)
    try:
        yield
    finally:
        if measuring is not None:
            measuring.switch_context("")


def pytest_runtest_logreport(report):
    _DURATIONS[report.nodeid] = _DURATIONS.get(report.nodeid, 0.0) + report.duration


def pytest_sessionfinish(session, exitstatus):
    with open(_TIMES, "w", encoding="utf-8") as handle:
        _json.dump({"durations": _DURATIONS, "contexts": _measuring() is not None}, handle)
'''

# The run as delivered: a test file run as a user who copied it would, without
# the instrument. Loaded into that run only, the plugin seeds Python's, numpy's
# and sympy's generators once at session start (the instrument reseeds 0 before
# every test), gives hypothesis's plugin the seed when the command gave none,
# and reverses the collected order when asked. The environment carries the
# hash seed. A seed other than the instrument's finds a test that needs it.
_DELIVERED_PLUGIN = '''"""Seeds a run as delivered once, when the session starts, and orders it.
Loaded into the run as delivered only."""
import os as _os

import pytest as _pytest

_SEED = _os.environ.get("@PREFIX@DELIVERED_SEED")
_SEED = int(_SEED) if _SEED else None


@_pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    # hypothesis's pytest plugin reads its seed option in its own
    # pytest_configure, after this one; a missing option is no plugin.
    if (_SEED is not None
            and getattr(config.option, "hypothesis_seed", "") is None):
        config.option.hypothesis_seed = str(_SEED)


def pytest_sessionstart(session):
    if _SEED is None:
        return
    import random
    random.seed(_SEED)
    try:
        import numpy
        numpy.random.seed(_SEED)
    except Exception:
        pass
    try:
        from sympy.core import random as sympy_random
        if hasattr(sympy_random, "seed"):
            sympy_random.seed(_SEED)
    except Exception:
        pass


def pytest_collection_modifyitems(session, config, items):
    if _os.environ.get("@PREFIX@DELIVERED_ORDER") == "reverse":
        items.reverse()
'''


def _delivered_setup(head: str, origin: str) -> str:
    """The setup file a run as delivered stages beside a test file: the
    suite's import part ``head``, executed as if at ``origin``, the setup
    file it comes from, so a path it computes from ``__file__`` (the project
    root, a ``../src``) is the one beside the suite. Its names become the
    staged file's, which pytest takes as fixtures and hooks."""
    helper = f"{DELIVERED_MODULE}_setup"
    return (
        f"def {helper}(scope):\n"
        f"    namespace = {{key: value for key, value in scope.items()\n"
        f"                 if key.startswith('__') and key.endswith('__')}}\n"
        f"    namespace['__file__'] = {origin!r}\n"
        f"    exec(compile({head!r}, {origin!r}, 'exec', dont_inherit=True),\n"
        f"         namespace)\n"
        f"    scope.update({{key: value for key, value in namespace.items()\n"
        f"                  if not (key.startswith('__')\n"
        f"                          and key.endswith('__'))}})\n"
        f"\n\n"
        f"{helper}(globals())\n"
        f"del {helper}\n")


#: The instrument's name in an observation's identity.
INSTRUMENT_NAME = "determinism"

#: The module name of the plugin that prints an owned copy's traceback paths
#: as a run in the original project prints them.
_PATHS_PLUGIN = "langadapter_paths"

#: Its text. pytest prints a frame's file relative to the working directory when
#: that is shorter, so the plugin applies that rule from the original's directory
#: to the frame's original path, and writes the copy's paths in each report's
#: message as the original's before the one-line summary cuts them to the
#: terminal width. Only how paths print changes.
_PATHS_PLUGIN_TEXT = r"""
import json
import os
import re
from pathlib import Path

import pytest

_SETTINGS = json.loads(os.environ.get("@PREFIX@ORIGINAL_PATHS") or "{}")
_FLAGS = re.IGNORECASE if os.name == "nt" else 0


def _original(text):
    folded = os.path.normcase(text)
    for copied, original in _SETTINGS.get("pairs", ()):
        if folded.startswith(os.path.normcase(copied)) and (
                len(text) == len(copied) or text[len(copied)] in "\\/"):
            rest = text[len(copied):]
            # An original that is a root ("/", "C:\\") ends in the separator
            # the rest begins with.
            return original.rstrip("\\/") + rest if rest else original
    return text


def _original_text(text):
    for copied, original in _SETTINGS.get("pairs", ()):
        spellings = [(copied, original)]
        if "\\" in copied:
            # A string's repr writes each backslash twice.
            spellings.append((copied.replace("\\", "\\\\"), original.replace("\\", "\\\\")))
        for copied_form, original_form in spellings:
            below = original_form.rstrip("\\/")
            pattern = re.escape(copied_form)
            text = re.sub(pattern + r"(?=[\\/])", lambda _: below, text, flags=_FLAGS)
            text = re.sub(pattern, lambda _: original_form, text, flags=_FLAGS)
    return text


def _message(report):
    crash = getattr(getattr(report, "longrepr", None), "reprcrash", None)
    message = getattr(crash, "message", None)
    if isinstance(message, str):
        try:
            crash.message = _original_text(message)
        except AttributeError:
            pass


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_logreport(report):
    _message(report)


@pytest.hookimpl(tryfirst=True)
def pytest_collectreport(report):
    _message(report)


def pytest_configure(config):
    try:
        from _pytest._code.code import FormattedExcinfo
        from _pytest.pathlib import bestrelpath
    except ImportError:
        return
    if "cwd" not in _SETTINGS:
        return
    cwd = Path(_SETTINGS["cwd"])

    def _makepath(self, path):
        if not self.abspath and isinstance(path, Path):
            path = Path(_original(str(path)))
            try:
                relative = bestrelpath(cwd, path)
            except (OSError, ValueError):
                return str(path)
            if len(relative) < len(str(path)):
                return relative
        return str(path)

    FormattedExcinfo._makepath = _makepath
    own = config.cwd_relative_nodeid

    def cwd_relative_nodeid(nodeid):
        # The summary line names a test by its file from the working directory:
        # the original's file from the original's directory.
        path, *rest = nodeid.split("::")
        full = str(Path(str(config.rootpath)) / path)
        original = _original(full)
        if original == full:
            return own(nodeid)
        try:
            relative = bestrelpath(cwd, Path(original))
        except (OSError, ValueError):
            return own(nodeid)
        return "::".join([relative, *rest])

    config.cwd_relative_nodeid = cwd_relative_nodeid
"""

#: Where the run's interpreter would import the module under test from, by its
#: import machinery without importing: for the top package (the outer end of
#: the ``__init__`` chain) "copy", "absent" or its location (an editable
#: install's original, an installed copy). For a module in no package and each
#: directory from the copy's root to the top package also a namesake's location
#: outside the copy or installed, else "other".
_WHERE_IMPORTED = r'''
import importlib.util, json, os, sys, sysconfig
@LOCATIONS@
def installed(path):
    return any(_below(path, directory) for directory in _SITE)
def place(name):
    # The first existing search directory, else the origin: an import hook may
    # name a search location that is no directory (meson-python 0.16+).
    try:
        spec = importlib.util.find_spec(name)
    except Exception:
        return ""
    if spec is None:
        return ""
    for location in spec.submodule_search_locations or ():
        if os.path.isdir(location):
            return os.path.realpath(location)
    origin = spec.origin or ""
    if os.path.basename(origin).startswith("__init__."):
        origin = os.path.dirname(origin)
    return os.path.realpath(origin) if origin and os.path.exists(origin) else ""
def verdict(name, expected, strict):
    found = place(name)
    seen, wanted = _norm(found), _norm(expected)
    if not found:
        return "absent"
    if seen == wanted:
        return "copy"
    if strict or installed(seen) or (not seen.startswith(root + os.sep)
                                     and seen.endswith(wanted[len(root):])):
        return found
    return "other"
root, real = _norm(sys.argv[1]), os.path.realpath(sys.argv[1])
top = module = os.path.realpath(sys.argv[2])
if os.path.basename(module).startswith("__init__."):
    top = os.path.dirname(module)
directory = os.path.dirname(top)
while (os.path.normcase(directory).startswith(root + os.sep)
       and os.path.isfile(os.path.join(directory, "__init__.py"))):
    top, directory = directory, os.path.dirname(directory)
name = os.path.splitext(os.path.basename(top))[0]
candidates, step = [], real
inner = directory[len(real) + 1:] if os.path.normcase(directory).startswith(root + os.sep) else ""
for part in inner.split(os.sep) if inner else ():
    step = os.path.join(step, part)
    if part.isidentifier():
        candidates.append([part, step, verdict(part, step, False)])
print(json.dumps({"name": name, "top": top, "root": directory,
                  "verdict": verdict(name, top, os.path.isdir(top)),
                  "candidates": candidates}))
'''.replace("@LOCATIONS@", _LOCATIONS)


def _instrument_tail(text: str) -> Optional[str]:
    start = text.find(_INSTRUMENT_MARKER)
    return text[start:] if start >= 0 else None


def instrument_identity(text: str) -> Optional[InstrumentIdentity]:
    """The instrument ``text`` carries from its header on, identified by the
    first 16 hex digits of that text's SHA-256. None when it carries none."""
    tail = _instrument_tail(text)
    if tail is None:
        return None
    return InstrumentIdentity(
        INSTRUMENT_NAME, hashlib.sha256(tail.encode("utf-8")).hexdigest()[:16])
