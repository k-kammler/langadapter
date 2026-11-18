"""The toolchain recipe, ``langadapter toolchains``: the part of each named
language for this system. The command is the interface, so every name here
is private.

A part is a POSIX shell file ``<language>.<system>.sh`` in this directory, or
an entry of a loaded extension's ``langadapter_toolchain`` mapping from a
system name to the part's text. The command finds a part by its language and
its system, so a new language or a new system adds a part and changes nothing
here. A part takes the install directory, the Python the command runs in and
the extension item its check loads, installs below the directory, writes
``<language>.env`` there and ends with ``langadapter check`` on an example it
wrote, run by that Python as ``"$PYTHON" -m langadapter.cli check``. Its header
lines ``# download <kind>: <text>`` name its downloads.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

#: The built-in parts' directory.
_PARTS = Path(__file__).parent
#: Where the running system's name and version are read.
_OS_RELEASE = Path("/etc/os-release")
#: The guide page with the steps for a system without a part.
_GUIDE = "https://k-kammler.github.io/langadapter/guide/toolchains/"
#: The start of a header line that names a download.
_DOWNLOAD = "# download "
#: The characters an install directory may not hold, by their names: a part
#: writes the directory into the environment file, which a shell sources, and
#: into its PATH line, which a colon splits.
_UNSAFE = {'"': '"', "$": "$", "`": "`", "\\": "\\", "\n": "a line break", ":": "a colon"}

#: A part: where it comes from, its text and the extension item its check loads.
_Part = Tuple[str, str, str]


def _default_directory() -> Path:
    """Where the parts install unless ``--into`` names another place: the
    user's data directory, since the runs start the installed tools."""
    data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(data) / "langadapter" / "toolchains"


def _current_system() -> Optional[str]:
    """``<ID>-<VERSION_ID>`` of the running system, such as ``ubuntu-24.04``,
    or None when ``_OS_RELEASE`` is missing or names not both."""
    try:
        text = _OS_RELEASE.read_text(encoding="utf-8")
    except OSError:
        return None
    fields = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
    name, version = (fields.get(key, "").strip().strip("\"'") for key in ("ID", "VERSION_ID"))
    return f"{name}-{version}" if name and version else None


def _parts(system: str, extensions: Sequence[Any] = ()) -> Dict[str, _Part]:
    """The parts for ``system`` by language. Names are compared, never
    joined into a path, so a system that names a directory, such as ../x,
    names no part."""
    found = {}
    suffix = f".{system}.sh"
    for path in sorted(_PARTS.iterdir()):
        if path.name.endswith(suffix) and len(path.name) > len(suffix) and path.is_file():
            found[path.name[:-len(suffix)]] = ("built in", path.read_text(encoding="utf-8"), "")
    for extension in extensions:
        mapping = getattr(sys.modules.get(extension.module), "langadapter_toolchain", None)
        if extension.languages and isinstance(mapping, dict) and system in mapping:
            item = extension.file if extension.kind == "file" else extension.item
            found[extension.languages[0]] = (extension.file, mapping[system], item)
    return found


def _listed(name: str, system: str, part: _Part) -> str:
    origin, text, _ = part
    downloads = [line[len(_DOWNLOAD):] for line in text.splitlines() if line.startswith(_DOWNLOAD)]
    return "\n".join([f"{name} on {system} ({origin}):", *(f"  {line}" for line in downloads)])


def _run_part(part: _Part, into: Path) -> int:
    """Run a part's text from a file in a new temporary directory, which goes
    afterwards, with the install directory, this interpreter and the
    extension item its check loads."""
    _, text, extension = part
    with tempfile.TemporaryDirectory(prefix="langadapter-part-") as scratch:
        path = Path(scratch) / "part.sh"
        path.write_text(text, encoding="utf-8", newline="\n")
        return subprocess.run(["sh", str(path), into.as_posix(), sys.executable,
                               extension]).returncode


def _refusal(system: Optional[str], found: Dict[str, _Part], names: Sequence[str],
             into: Optional[Path]) -> Optional[str]:
    """Why the command cannot run these parts, or None. ``into`` is None for
    a listing, which installs nothing."""
    if system is None and not sys.platform.startswith("linux"):
        here = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, sys.platform)
        return (f"this system is {here}, and the parts run on Ubuntu 24.04, also in WSL. "
                "Run the command there, or install the tools by hand as the guide's section "
                f"Windows and macOS says: {_GUIDE}#windows-and-macos")
    if system is None:
        return (f"{_OS_RELEASE.as_posix()} names no ID and VERSION_ID here. The guide lists the "
                f"steps: {_GUIDE}")
    missing = [name for name in names if name not in found] or ([] if names else ["any language"])
    if missing:
        return f"no part for {', '.join(missing)} on {system}. The guide lists the steps: {_GUIDE}"
    # A part writes the directory as its shell's pwd names it, which holds
    # no drive such as C:.
    written = "" if into is None else into.as_posix()[len(into.drive):]
    unsafe = [label for character, label in _UNSAFE.items() if character in written]
    if unsafe:
        return (f"the install directory {into} holds {' and '.join(unsafe)}, which a shell "
                "would read in its environment file. Name another with --into.")
    return None


def _run(languages: Sequence[str], into: Path, *, list_only: bool = False,
         extensions: Sequence[Any] = ()) -> int:
    """Run the parts of ``languages`` (``all`` names every language with a
    part) for this system in the order named, and return the status of the
    first that fails, else 0 after the quoted source line of each environment
    file. With ``list_only`` print each part's downloads and run none. A
    system or language without a part returns 2 and names the guide page, as
    does an install directory holding a character in ``_UNSAFE``."""
    system = _current_system()
    found = _parts(system, extensions) if system is not None else {}
    names = sorted(found) if list(languages) == ["all"] else list(dict.fromkeys(languages))
    refusal = _refusal(system, found, names, None if list_only else into)
    if refusal is not None:
        print(f"langadapter toolchains: {refusal}", file=sys.stderr)
        return 2
    if list_only:
        print("\n".join(_listed(name, system, found[name]) for name in names))
        return 0
    into.mkdir(parents=True, exist_ok=True)
    for name in names:
        status = _run_part(found[name], into)
        if status:
            return status
    for name in names:
        if (into / f"{name}.env").exists():
            print(f". {shlex.quote(str(into / f'{name}.env'))}")
    return 0
