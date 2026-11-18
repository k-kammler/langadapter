"""The hosts LangAdapter runs a project's commands on.

Windows (a job object) and Linux (its supervisor) supervise a command and its
descendants and confirm that every process it started has ended. Other POSIX
systems, macOS above all, run a command in its own process group
(``langadapter._process_tree``), ending the group without confirming cleanup.
Hosts without processes (emscripten, wasi) run no command. Importing
LangAdapter and the source operations, which run none, work on every host.
"""
from __future__ import annotations

import os
import sys
from typing import Optional


#: The platforms without processes, where no command runs.
_NO_PROCESSES = ("emscripten", "wasi")


def confirms_cleanup() -> bool:
    """Whether this host confirms that a command's processes all ended (Windows, Linux)."""
    return sys.platform == "win32" or sys.platform.startswith("linux")


def runs_commands() -> bool:
    """Whether this host can run a project's commands: Windows and Linux,
    and the other POSIX systems in a command's process group."""
    return confirms_cleanup() or sys.platform not in _NO_PROCESSES


#: The longest working directory Windows starts a command in, even with long
#: paths enabled: 258 characters start, 259 are refused (WinError 267).
WINDOWS_WORKING_DIR_LIMIT = 258


def working_dir_limit():
    """The longest working directory this host starts a command in, None if unlimited."""
    return WINDOWS_WORKING_DIR_LIMIT if sys.platform == "win32" else None


def working_dir_issue(working_dir) -> str:
    """Why this host starts no command in ``working_dir`` (past its limit), or ""."""
    limit = working_dir_limit()
    if working_dir is None or limit is None or len(str(working_dir)) <= limit:
        return ""
    return (f"the working directory {working_dir} is {len(str(working_dir))} characters long, and Windows "
            f"starts no command in a directory longer than {limit} characters, even with long "
            f"paths enabled: run from a shorter directory")


def refusal() -> str:
    """Why this host runs no command, and what works on it."""
    return (f"LangAdapter runs a project's commands on Windows and Linux, where it "
            f"supervises them and confirms that every process a command started has "
            f"ended, and on other POSIX systems such as macOS in the command's own "
            f"process group, without that confirmation. {sys.platform} has no "
            f"processes to run a command in. Importing LangAdapter and its source "
            f"operations, which run no command, work here, with a context created "
            f"without an interpreter.")


def started_file(path: str) -> Optional[str]:
    """The file a process start runs for ``path``: the path itself, or on
    Windows, for a path with a directory whose file name has no extension,
    the name with .exe, which process creation adds. None when neither is a
    file."""
    name = os.path.basename(path)
    candidates = [path]
    if (os.name == "nt" and os.path.dirname(path) and not os.path.splitext(name)[1]
            and not name.endswith(".")):
        candidates.append(path + ".exe")
    return next((candidate for candidate in candidates if os.path.isfile(candidate)), None)
