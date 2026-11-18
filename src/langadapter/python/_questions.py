"""Python's setup questions (``PythonAdapter.setup_questions``): the directory
the project's packages are imported from and the project's interpreter,
the interpreters offered being the ones that run and that this release
supports, and the tools a run needs that the chosen one lacks."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Union

from langadapter.adapter import SetupNote, SetupQuestion
from langadapter.python._profile import TOOL_MODULES
from langadapter.python._project_files import import_root_setting
from langadapter.python._provision import tool_pins
from langadapter.python.project import _discover_environments

#: The scripts the setup runs in an interpreter: its version, the tools it lacks.
VERSION_SCRIPT = "import sys; print('%d.%d.%d' % sys.version_info[:3])"
TOOLS_SCRIPT = ("import importlib.util, sys; print(' '.join(m for m in sys.argv[1:] "
                "if importlib.util.find_spec(m) is None))")
#: The modules a run imports in the project's interpreter, by their packages.
_TOOLS = {module: name for name, module in TOOL_MODULES.items()}
_SCRIPT = "Scripts/python.exe" if os.name == "nt" else "bin/python"


def python_version(probe: Any, executable: str) -> Optional[str]:
    """The version ``executable`` reports (``3.12.4``), or None when it does
    not run."""
    done = probe.run([executable, "-c", VERSION_SCRIPT], 30)
    return done[1].strip() if done is not None and done[0] == 0 else None


def missing_tools(probe: Any, executable: str) -> List[str]:
    """The packages a run needs that ``executable`` cannot import."""
    done = probe.run([executable, "-c", TOOLS_SCRIPT, *_TOOLS], 60)
    if done is None or done[0] != 0:
        return []
    return [_TOOLS[name] for name in done[1].split() if name in _TOOLS]


def interpreter_problem(adapter: Any, version: Optional[str]) -> Optional[str]:
    """Why a setup refuses an interpreter that reported ``version`` (None: it
    did not run): the profile's refusal under the adapter's declared
    support, or None."""
    from langadapter.python._profile import PythonProfile
    from langadapter.versions import python_line

    if not version:
        return "this interpreter does not run"
    return PythonProfile(python_line(version, "probe"), adapter.support).refusal() or None


def candidates(probe: Any, project_dir: Path) -> List[str]:
    """The interpreters offered for ``project_dir``: the project's own
    environments (.venv, venv), then the Pythons on PATH, the calling
    process's last, each once by its absolute path. Links are not followed:
    an environment's python links to the Python it was made from, but it is
    another environment, with its own packages."""
    found = [str(project_dir / name / _SCRIPT) for name in (".venv", "venv")
             if probe.is_file(project_dir / name / _SCRIPT)]
    found += [probe.which(name) for name in ("python3", "python")]
    found.append(sys.executable)
    unique: Dict[str, str] = {}
    for path in found:
        if path:
            unique.setdefault(os.path.normcase(os.path.abspath(path)), path)
    # PATH can name the calling interpreter otherwise spelled (python.EXE
    # through PATHEXT): it keeps its own spelling.
    unique[os.path.normcase(os.path.abspath(sys.executable))] = sys.executable
    return list(unique.values())


def problem(adapter: Any, setting: str, value: str, project_dir: Path, probe: Any) -> Optional[str]:
    """Why the setup refuses ``value`` for ``setting``: the import directory must
    exist below the project (or be . or none), the interpreter must run and
    be supported. Python's other settings are not checked here."""
    if setting == "python_import_root":
        try:
            root = import_root_setting(value)
        except ValueError as exc:
            return str(exc)
        return None if not root or probe.is_dir(project_dir / root) else "no such directory"
    if setting == "python_executable":
        return interpreter_problem(adapter, python_version(probe, value))
    return None


def questions(adapter: Any, project_dir: Path, probe: Any, consumer: str
              ) -> Iterator[Union[SetupQuestion, SetupNote]]:
    """The import directory, then the interpreter, as ``consumer``'s setup asks."""
    default_root = "src" if probe.is_dir(project_dir / "src") else ""
    yield SetupQuestion(
        "python.import_root",
        "The directory the project's Python packages are in (. for the project's directory, "
        "or one inside it such as src)",
        setting="python_import_root", default=default_root or ".",
        reason=f"{consumer} imports the source files from there: calc/ops.py in it is "
               "imported as calc.ops",
        check=lambda answer: problem(adapter, "python_import_root", answer, project_dir, probe),
        written=lambda answer: "" if answer in (".", "none") else answer)
    # The default follows inspection's rule: the only environment found.
    found = _discover_environments(str(project_dir), {name: probe.environment(name) for name in (
        "VIRTUAL_ENV", "CONDA_PREFIX") if probe.environment(name)})
    versions: Dict[str, Optional[str]] = {}

    def check(answer: str) -> Optional[str]:
        versions[answer] = python_version(probe, answer)
        return interpreter_problem(adapter, versions[answer])

    def after(answer: str) -> SetupNote:
        lines = [f"  Python {versions[answer]}"]
        missing = []
        lacking = missing_tools(probe, answer)
        if lacking:
            lines.append(f"  Not installed in this interpreter: {', '.join(lacking)}. "
                         "A run needs them there.")
            missing.append(f"install {', '.join(tool_pins(versions[answer], lacking))} in {answer}")
        return SetupNote(tuple(lines), tuple(missing))

    yield SetupQuestion(
        "python.interpreter", "The project's Python interpreter",
        setting="python_executable", default=found.selected or "",
        reason="" if found.selected else (
            f"several environments were found ({', '.join(found.existing)}): choose one"
            if found.existing else
            ("the only environment found is the one activated in the shell, the interpreter "
             f"{consumer} runs in: choose the project's interpreter, or, on purpose, this one, "
             "which then needs the project's dependencies") if found.callers_own else
            "no project .venv or activated environment was found: choose the project's "
            f"interpreter, or, on purpose, the interpreter {consumer} runs in, which then needs "
            "the project's dependencies"),
        # Offered: the interpreters that run and that the release supports.
        choices=lambda: tuple(path for path in candidates(probe, project_dir) if check(path) is None),
        open=True, check=check, after=after)
