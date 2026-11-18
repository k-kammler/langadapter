"""The C toolchain of a project: its GCC and the gcov of the same release.

Coverage data is read by the gcov of the GCC that wrote it, so the adapter
takes gcov from beside the compiler (``gcc`` gives ``gcov``, ``gcc-13`` gives
``gcov-13``) and refuses one whose version differs. Other compilers, such as
clang, are not supported in this version.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

#: A version in a tool's line, such as 13.3.0.
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?")


@dataclass(frozen=True)
class Toolchain:
    """A usable toolchain: the compiler, its version line and full version,
    and the matching gcov."""

    compiler: str
    version_line: str
    version: str
    gcov: str


@dataclass(frozen=True)
class ToolchainProblem:
    """Why no toolchain is usable, in words."""

    reason: str


def which(name: str, environment: Mapping[str, str]) -> Optional[str]:
    """``name`` where a command started with ``environment`` is looked for:
    on its PATH, else on ``os.defpath``, as subprocess looks."""
    return shutil.which(name, path=environment.get("PATH", os.defpath))


def find_compiler(selected: str = "",
                  environment: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The compiler a project uses, as an absolute path: the setting's value,
    else ``CC`` from ``environment`` (this process's when None), else ``cc``
    or ``gcc`` on its PATH, or None when none is found."""
    environment = os.environ if environment is None else environment
    for candidate in (selected, environment.get("CC", ""), "cc", "gcc"):
        candidate = (candidate or "").strip()
        if not candidate:
            continue
        if os.path.isabs(candidate):
            if os.access(candidate, os.X_OK):
                return candidate
            continue
        found = which(candidate, environment)
        if found:
            return found
    return None


def gcov_beside(compiler: str) -> Optional[str]:
    """The gcov named after ``compiler`` in its directory, following links to
    the real compiler (``cc`` to ``x86_64-linux-gnu-gcc-13``), or None."""
    for path in (Path(compiler), Path(os.path.realpath(compiler))):
        name = path.name
        index = name.rfind("gcc")
        if index < 0:
            continue
        candidate = path.with_name(name[:index] + "gcov" + name[index + 3:])
        if os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def first_line(done) -> str:
    """The first line of the completed process ``done``'s output, stripped,
    or "" when it wrote nothing."""
    return (done.stdout or "").splitlines()[0].strip() if done.stdout else ""


def probe(compiler: Optional[str], run) -> "Toolchain | ToolchainProblem":
    """The toolchain of ``compiler``, its versions read with ``run(argv)``
    (which returns a completed process), or the problem."""
    if not compiler:
        return ToolchainProblem("no C compiler was found (set c_compiler or CC, "
                                "or put gcc on PATH)")
    described = run([compiler, "--version"])
    line = first_line(described)
    if described.returncode != 0 or not line:
        return ToolchainProblem(f"the compiler {compiler} does not run "
                                f"(exit {described.returncode})")
    if "clang" in line.lower():
        return ToolchainProblem(f"the compiler {compiler} is clang ({line}); this version "
                                "of the C adapter supports GCC with gcov only")
    full = run([compiler, "-dumpfullversion"])
    version = (full.stdout or "").strip()
    if full.returncode != 0 or not version:
        return ToolchainProblem(f"the compiler {compiler} does not report its version "
                                f"with -dumpfullversion ({line}); GCC 7 or newer is needed")
    gcov = gcov_beside(compiler)
    if gcov is None:
        return ToolchainProblem(f"no gcov was found beside {compiler}; coverage needs the "
                                "gcov of the same GCC")
    shown = run([gcov, "--version"])
    gcov_line = first_line(shown)
    match = _VERSION.search(gcov_line)
    if shown.returncode != 0 or match is None:
        return ToolchainProblem(f"{gcov} does not report its version")
    # The compiler's version as a word of the line: distributions add a date
    # or a package release after it.
    if not re.search(r"(?<![\w.])" + re.escape(version) + r"(?![\w.])", gcov_line):
        return ToolchainProblem(f"{gcov} is version {match.group(0)}, the compiler "
                                f"{compiler} version {version}; coverage needs both the same")
    return Toolchain(compiler=compiler, version_line=line, version=version, gcov=gcov)
