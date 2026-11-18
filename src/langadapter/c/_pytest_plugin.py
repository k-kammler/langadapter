"""The pytest plugin of a C extension's runs, loaded into the project's Python.

The C adapter copies this file alone into a directory of a run, puts that directory
on PYTHONPATH and names the plugin in PYTEST_PLUGINS. It uses the standard
library only, since it runs in the project's interpreter, and not json: a
target such as CPython's ``_json`` is what json itself imports. Its
configuration is the file of ``key<TAB>value`` lines that
LANGADAPTER_C_PLUGIN_CONFIG names, read when the first import asks:

- ``module`` and ``extension``: the target's module name and the build of it
  the session loads, through an import finder ahead of every other, so that
  the rest of the project imports from the project as usual.
- ``counters`` (optional): a directory. With ``per_test``, the extension's
  gcov counters are written and reset around each test, into a directory of the
  test's own, and what ran outside every test (import, collection) into
  ``outside``. Without it, gcov writes them all into ``outside`` when the
  process ends. ``strip`` is the count of directories gcov leaves out of the
  counters' path.
  The adapter's small extension ``langadapter_c_counters``, compiled beside
  this file, calls gcov's dump and reset in the target's library, which the
  coverage build exports under the adapter's names (``gcov_exports.c``): a
  Python built without libffi has no ctypes.
- ``deselect`` (optional): a file of node ids, one per line, that the
  session leaves out: the tests a run that a crash ended had started.
- ``mutant`` (optional): the variable that switches one of Mull's mutants
  on in Mull's build of the target. It is set when the configuration is
  read, before the target's module loads.
- ``report``: a file of lines, each a kind and its values separated by tabs,
  written as they happen, so a crash keeps what came before it.
  ``loaded_from`` holds the file the module was loaded from, and
  ``load_error`` holds the error that kept it from loading. ``started``,
  ``failed`` and ``finished`` hold a test's node id, and ``test`` holds a
  test's counter directory and its node id.

PYTEST_DONT_REWRITE: the sitecustomize imports this module before pytest
loads it from PYTEST_PLUGINS, too late for pytest to rewrite its asserts.
Without this marker pytest warns of that, and a project whose configuration
turns warnings into errors (markupsafe's ``filterwarnings = ["error"]``)
then cannot start its tests.
"""
import importlib.machinery
import importlib.util
import os
import sys

_CONFIG = None
_COUNTERS = None
_COUNT = [0]


def _config():
    global _CONFIG
    if _CONFIG is None:
        found = {}
        with open(os.environ["LANGADAPTER_C_PLUGIN_CONFIG"], encoding="utf-8") as handle:
            for line in handle.read().splitlines():
                key, _, value = line.partition("\t")
                found[key] = value or None
        _CONFIG = found
        if found.get("mutant"):
            os.environ[found["mutant"]] = "1"
    return _CONFIG


def _report(kind, *values):
    text = "\t".join(str(value).replace("\t", " ").replace("\n", " | ") for value in values)
    with open(_config()["report"], "a", encoding="utf-8") as handle:
        handle.write(f"{kind}\t{text}\n")


def _configuration_or_none():
    """The run's configuration, or None in a process without it (one a test
    starts with another environment) or with one it cannot read: such a
    process imports as usual."""
    try:
        return _config()
    except (KeyError, OSError, ValueError):
        return None


class _TargetFinder:
    """Finds the target module in the measured build, and nothing else."""

    def find_spec(self, fullname, path=None, target=None):
        config = _configuration_or_none()
        if config is None or fullname != config["module"]:
            return None
        loader = importlib.machinery.ExtensionFileLoader(fullname, config["extension"])
        return importlib.util.spec_from_file_location(fullname, config["extension"],
                                                      loader=loader)


def _prefix(name):
    os.environ["GCOV_PREFIX"] = os.path.join(_config()["counters"], name)


def _flush():
    """Write the counters gathered since the last flush, then zero them."""
    if _COUNTERS is not None:
        _COUNTERS.flush()


# Ahead of every import of the process: the adapter's sitecustomize imports
# this module while Python starts, and pytest finds it already imported.
if not any(isinstance(finder, _TargetFinder) for finder in sys.meta_path):
    sys.meta_path.insert(0, _TargetFinder())


def pytest_sessionstart(session):
    global _COUNTERS
    settings = _config()
    if settings.get("counters"):
        os.environ["GCOV_PREFIX_STRIP"] = str(settings["strip"])
        _prefix("outside")
    try:
        module = importlib.import_module(settings["module"])
    except BaseException as exc:  # the report names any failure to load
        _report("load_error", f"{type(exc).__name__}: {exc}")
        return
    loaded_from = getattr(module, "__file__", None)
    _report("loaded_from", loaded_from)
    measured = (loaded_from is not None
                and os.path.realpath(loaded_from) == os.path.realpath(settings["extension"]))
    if settings.get("counters") and settings.get("per_test") and measured:
        import langadapter_c_counters

        langadapter_c_counters.bind(settings["extension"])
        _COUNTERS = langadapter_c_counters
        _flush()


def pytest_collection_modifyitems(session, config, items):
    listed = _config().get("deselect")
    if not listed:
        return
    with open(listed, encoding="utf-8") as handle:
        done = set(handle.read().splitlines())
    dropped = [item for item in items if item.nodeid in done]
    if dropped:
        config.hook.pytest_deselected(items=dropped)
        items[:] = [item for item in items if item.nodeid not in done]


def pytest_runtest_protocol(item):
    _report("started", item.nodeid)
    if _COUNTERS is None:
        return None
    _flush()
    name = f"t{_COUNT[0]}"
    _COUNT[0] += 1
    _report("test", name, item.nodeid)
    _prefix(name)
    return None


def pytest_runtest_logreport(report):
    if report.failed:
        _report("failed", report.nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _report("finished", nodeid)
    if _COUNTERS is None:
        return
    _flush()
    _prefix("outside")
