"""Compose a selected command environment without reviving captured absence."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

from langadapter.context import Environment


def captured(values: Optional[Mapping[str, str]]) -> bool:
    return isinstance(values, Environment) and not values.inherit


def launch_environment(values: Optional[Mapping[str, str]] = None, *,
                       native: bool = True) -> dict[str, str]:
    """A command's environment: a native overlay over the caller's environment
    unless ``values`` is captured, a container overlay alone. The returned dict may
    take operation-specific native flags without changing the selection."""
    result = dict(os.environ) if native and not captured(values) else {}
    if values is not None:
        result.update(values)
    return result


#: The variables pytest-xdist sets in a worker for itself.
XDIST_WORKER_VARIABLES = ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT",
                          "PYTEST_XDIST_TESTRUNUID")


def run_launch_environment(values: Optional[Mapping[str, str]]) -> dict[str, str]:
    """A Python run's ``launch_environment``, whose inherited part leaves out
    the variables pytest-xdist sets in a worker for itself: a run started
    inside a worker of the caller is not that worker, though the project's
    plugins and conftest would take it for one. ``values`` may set them."""
    result = launch_environment(values)
    for name in XDIST_WORKER_VARIABLES:
        if name not in (values or {}):
            result.pop(name, None)
    return result


def selected_executable(command: str, values: Mapping[str, str]) -> Optional[str]:
    """The host executable ``command`` binds to through explicit absolute paths
    and ``values`` only, or None: unlike Windows process creation or
    ``shutil.which``, never the process's PATH/PATHEXT or current directory."""
    selected = Path(command)
    if selected.is_absolute():
        candidates = (selected,)
    elif selected.name != command or selected.drive:
        return None
    else:
        def selected_value(name):
            if os.name != "nt":
                return values.get(name, "")
            aliases = [value for key, value in values.items() if key.upper() == name]
            return "" if not aliases else aliases[0] if len(set(aliases)) == 1 else None

        path, file_extensions = selected_value("PATH"), selected_value("PATHEXT")
        if path is None or file_extensions is None:
            return None
        suffixes = [""]
        if os.name == "nt":
            suffixes.extend(suffix for suffix in file_extensions.split(";")
                            if suffix.startswith(".") and "/" not in suffix and "\\" not in suffix)
        candidates = (Path(search_dir) / f"{command}{suffix}"
                      for search_dir in path.split(os.pathsep)
                      if search_dir and Path(search_dir).is_absolute()
                      for suffix in dict.fromkeys(suffixes))
    return next((str(path) for path in candidates if path.is_file()
                 and (os.name == "nt" or os.access(path, os.X_OK))), None)
