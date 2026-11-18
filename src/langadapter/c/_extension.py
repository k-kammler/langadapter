"""Extension mode: a C target built into a Python extension, tested by Python.

The tests are Python 3 files that LangAdapter's Python adapter runs with the
project's interpreter: pytest, its isolation and its records stay Python's.
The C adapter adds two things to each run. The extension it built is staged
in a directory of the run, and its pytest plugin (``_pytest_plugin``) loads the
target's module from there, checks that it loaded, and writes the
extension's gcov counters per test when coverage is measured.

A ``Harness`` is one directory's Python adapter with the plugin on its path:
its context is derived from the C adapter's (``ProjectContext.derived``),
also inside a consumer's prepared copy: PYTHONPATH, PYTEST_PLUGINS and the
plugin's configuration file added, the directory as the work directory. One harness
serves every run in its directory. For runs with counters it compiles the adapter's small extension
``langadapter_c_counters`` (``data/counters.c``) against the test
interpreter's headers, beside the plugin.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

#: The plugin's module name in the project's interpreter.
PLUGIN = "langadapter_c_plugin"
#: The variable that names the plugin's configuration file.
CONFIG_VARIABLE = "LANGADAPTER_C_PLUGIN_CONFIG"
#: What the test interpreter says of its headers and extension suffix. A
#: CPython build's own interpreter (or a venv of it) keeps its headers in the
#: source tree and its pyconfig.h in the build directory.
_HEADERS = (
    "import json, os, sysconfig\n"
    "paths = sysconfig.get_paths()\n"
    "if sysconfig.is_python_build():\n"
    "    found = [os.path.join(sysconfig.get_config_var('srcdir'), 'Include'),\n"
    "             sysconfig.get_config_var('abs_builddir')]\n"
    "else:\n"
    "    found = [paths['include'], paths['platinclude']]\n"
    "print(json.dumps({'include': found, 'suffix': sysconfig.get_config_var('EXT_SUFFIX')}))\n")


@dataclass(frozen=True)
class Report:
    """What the plugin saw: the file the target module loaded from, or why it
    did not load, each test's counter directory by its node id, and the tests
    that started, failed and finished, by node id, in order. A report of
    several sessions (a run as delivered, a benchmark) has the file of each
    session that loaded the module in ``loads``, and the first load error."""

    loaded_from: Optional[str] = None
    load_error: Optional[str] = None
    counters: Dict[str, str] = field(default_factory=dict)
    started: Tuple[str, ...] = ()
    failed: Tuple[str, ...] = ()
    finished: Tuple[str, ...] = ()
    loads: Tuple[Optional[str], ...] = ()

    @property
    def unfinished(self) -> Optional[str]:
        """The test that started and did not finish (a crash ended it), or None."""
        if self.started and self.started[-1] not in self.finished:
            return self.started[-1]
        return None

    def loaded(self, extension: Path) -> bool:
        """Whether the module loaded from ``extension``, in every session."""
        return (self.load_error is None and bool(self.loads)
                and all(_same(found, extension) for found in self.loads))

    def problem(self, module: str, extension: Path) -> str:
        """Why the run did not measure the build at ``extension``."""
        if self.load_error is not None:
            return f"the target module {module} did not load: {self.load_error}"
        if not self.loads:
            return f"the target module {module} was not loaded (the run ended before)"
        other = next(found for found in self.loads if not _same(found, extension))
        return (f"the target module {module} loaded from {other}, not from the "
                f"measured build {extension}")


def _same(found: Optional[str], extension: Path) -> bool:
    return found is not None and Path(found).resolve() == extension.resolve()


def read_report(path: Path) -> Report:
    """The plugin's report: lines of a kind and its values, tab-separated."""
    loaded_from = load_error = None
    loads: List[Optional[str]] = []
    counters: Dict[str, str] = {}
    seen: Dict[str, List[str]] = {"started": [], "failed": [], "finished": []}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            kind, _, rest = line.partition("\t")
            if kind == "loaded_from":
                loaded_from = None if rest == "None" else rest
                loads.append(loaded_from)
            elif kind == "load_error":
                load_error = load_error or rest
            elif kind == "test":
                counter_dir, _, nodeid = rest.partition("\t")
                counters[nodeid] = counter_dir
            elif kind in seen and rest not in seen[kind]:
                seen[kind].append(rest)
    return Report(loaded_from, load_error, counters, tuple(seen["started"]),
                  tuple(seen["failed"]), tuple(seen["finished"]), tuple(loads))


def imported_modules(source: str) -> Set[str]:
    """The modules a Python test file imports, with each name a
    ``from ... import`` takes from a package (``from calcext import _calc``
    gives ``calcext`` and ``calcext._calc``), and the empty set when it does
    not parse."""
    import ast

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    found: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


class Harness:
    """The Python adapter of runs in ``directory``, with the plugin loaded, for
    the C adapter ``adapter`` (its context, compiler and commands)."""

    def __init__(self, adapter, harness_dir: Path) -> None:
        from langadapter.python.adapter import PythonAdapter

        context = adapter.context
        self.adapter = adapter
        self.harness_dir = harness_dir
        self._helper: Optional[str] = None
        plugin = harness_dir / "plugin"
        self.plugin = plugin
        plugin.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(__file__).with_name("_pytest_plugin.py"), plugin / f"{PLUGIN}.py")
        # Python imports it while it starts, so the target's finder comes first.
        shutil.copyfile(Path(__file__).with_name("_sitecustomize.py"),
                        plugin / "sitecustomize.py")
        self.config = harness_dir / "plugin.cfg"
        self.report = harness_dir / "report.txt"
        # The plugin's variables are added to the C adapter's context, which
        # may be a consumer's prepared copy (ProjectContext.derived). The
        # directory is the work directory: the Python adapter's scratch, and what it
        # keeps of a failed run (a mutant's crash), go with it. The tests
        # import the project's packages from its import root (a src layout's
        # src, a namespace package's directory), the target's module from the
        # plugin's finder.
        root = adapter._import_root()
        path = [str(plugin)] + ([str(Path(context.root) / root)] if root else [])
        self.python = PythonAdapter(context.derived(
            environment={"PYTHONPATH": os.pathsep.join(path), "PYTEST_PLUGINS": PLUGIN,
                         CONFIG_VARIABLE: str(self.config)},
            work_dir=str(harness_dir),
            # A user's own paths and plugins still apply, after the harness's.
            extend={"PYTHONPATH": os.pathsep, "PYTEST_PLUGINS": ","}))

    def counters_helper(self) -> Optional[str]:
        """Compile the counters helper beside the plugin, once: why it could
        not be compiled, or None."""
        if self._helper is not None:
            return self._helper or None
        interpreter = self.adapter.context.interpreter
        asked = self.adapter._run([interpreter.path, "-c", _HEADERS], timeout=120)
        if asked.returncode != 0:
            self._helper = (f"the test interpreter did not name its headers: "
                            f"{(asked.stderr or '').strip()[-300:]}")
            return self._helper
        found = json.loads(asked.stdout)
        output = self.plugin / f"langadapter_c_counters{found['suffix']}"
        source = Path(__file__).with_name("data") / "counters.c"
        done = self.adapter._run(
            [self.adapter._toolchain().compiler, "-shared", "-fPIC", "-O1",
             *(f"-I{include_dir}" for include_dir in found["include"]), str(source), "-o", str(output)],
            timeout=300)
        if done.returncode != 0:
            self._helper = (f"the counters helper does not compile against the test "
                            f"interpreter: {((done.stdout or '') + (done.stderr or '')).strip()[-500:]}")
            return self._helper
        self._helper = ""
        return None

    def run(self, request, *, module: str, extension: Path,
            counters: Optional[Path] = None, strip: Optional[str] = None,
            deselect: Sequence[str] = (), per_test: bool = False, mutant: str = ""):
        """Run ``request`` (a ``SuiteRunRequest`` of the Python adapter) with
        the target module loaded from ``extension``, without the tests whose
        node ids ``deselect`` lists, and with the Mull mutant whose switch is
        ``mutant`` on: the Python adapter's ``SuiteRun`` and the plugin's
        ``Report``."""
        self.configure(module=module, extension=extension, counters=counters, strip=strip,
                       deselect=deselect, per_test=per_test, mutant=mutant)
        run = self.python.run_suite(request)
        return run, read_report(self.report)

    def configure(self, *, module: str, extension: Path, counters: Optional[Path] = None,
                  strip: Optional[str] = None, deselect: Sequence[str] = (),
                  per_test: bool = False, mutant: str = "") -> None:
        """Write the plugin's configuration for the next session, and empty
        the report of the session before. ``per_test`` writes the counters of
        each test apart (it needs ``counters_helper``). Without it the
        session's counters are written once, when it ends."""
        if self.report.exists():
            self.report.unlink()
        listed = self.harness_dir / "deselect.txt"
        listed.write_text("".join(f"{nodeid}\n" for nodeid in deselect), encoding="utf-8")
        settings = {"module": module, "extension": str(extension), "report": str(self.report),
                    "counters": str(counters) if counters is not None else "",
                    "strip": strip or "", "deselect": str(listed) if deselect else "",
                    "per_test": "1" if per_test else "", "mutant": mutant,
                    # The sitecustomize bounds the test interpreter by it.
                    "address_space_limit": str(self.adapter._address_space_limit() or "")}
        self.config.write_text("".join(f"{key}\t{value}\n" for key, value in settings.items()),
                               encoding="utf-8")
