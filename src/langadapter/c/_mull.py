"""Mull, the mutation tool: its runner, its IR frontend and the clang it was built for.

Mull's packages for LLVM ``N`` hold ``bin/mull-runner-N`` and
``lib/mull-ir-frontend-N``, and the frontend is a plugin of clang ``N``. The
adapter takes the runner the ``c_mull`` setting names (a path or a command),
else the newest ``mull-runner-N`` on the context's PATH. It takes the
frontend from the ``lib`` directory beside the runner's ``bin``, and clang
from ``lib/llvm-N/bin`` of the same prefix (the layout of Ubuntu's packages,
installed or unpacked with ``dpkg -x``), else ``clang-N`` on the context's
PATH. clang must be release ``N``, the one the frontend was built for.

``config`` writes the run's ``mull.yml``: the operators by their Mull
names, and the target's file as the only one the frontend mutates.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Tuple

from langadapter.c._toolchain import first_line, which

#: The LLVM releases Mull 0.34 is built for, newest first.
LLVM_RELEASES = tuple(range(23, 13, -1))
#: Mull's default group (``cxx_default``: increment, arithmetic, comparison
#: and boundary), by its operators as Mull 0.34.2 names them, so a newer
#: Mull with a larger group measures the same operators.
DEFAULT = (
    "cxx_pre_inc_to_pre_dec", "cxx_post_inc_to_post_dec",
    "cxx_minus_to_noop", "cxx_add_to_sub", "cxx_sub_to_add", "cxx_mul_to_div",
    "cxx_div_to_mul", "cxx_rem_to_div",
    "cxx_eq_to_ne", "cxx_ne_to_eq", "cxx_le_to_gt", "cxx_lt_to_ge", "cxx_ge_to_lt",
    "cxx_gt_to_le",
    "cxx_le_to_lt", "cxx_lt_to_le", "cxx_ge_to_gt", "cxx_gt_to_ge",
)
#: Mull's whole group (``cxx_all``), its default and the rest.
ALL = DEFAULT + (
    "cxx_add_assign_to_sub_assign", "cxx_sub_assign_to_add_assign",
    "cxx_mul_assign_to_div_assign", "cxx_div_assign_to_mul_assign",
    "cxx_rem_assign_to_div_assign",
    "cxx_and_assign_to_or_assign", "cxx_or_assign_to_and_assign",
    "cxx_xor_assign_to_or_assign", "cxx_lshift_assign_to_rshift_assign",
    "cxx_rshift_assign_to_lshift_assign",
    "cxx_assign_const", "cxx_init_const",
    "cxx_pre_dec_to_pre_inc", "cxx_post_dec_to_post_inc",
    "cxx_bitwise_not_to_noop", "cxx_and_to_or", "cxx_or_to_and", "cxx_xor_to_or",
    "cxx_lshift_to_rshift", "cxx_rshift_to_lshift",
    "cxx_remove_void_call", "cxx_replace_scalar_call",
)
#: The engine's name.
ENGINE = "mull"

_RUNNER = re.compile(r"^mull-runner-(\d+)$")
_VERSION = re.compile(r"mull-runner\s+(\S+)")
_LLVM = re.compile(r"^LLVM:\s*(\S+)", re.MULTILINE)
_CLANG = re.compile(r"clang version (\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class Mull:
    """A usable Mull: the runner, the IR frontend, the clang it plugs into,
    Mull's version and the LLVM release it was built with, and clang's
    version line and version."""

    runner: str
    frontend: str
    clang: str
    version: str
    llvm: str
    clang_line: str
    clang_version: str


@dataclass(frozen=True)
class MullProblem:
    """Why no Mull is usable, in words."""

    reason: str


def find_runner(selected: str, environment: Mapping[str, str]) -> Optional[str]:
    """The runner as an absolute path: the setting's value, else the newest
    ``mull-runner-N`` where a command started with ``environment`` is looked
    for, or None when none is found."""
    selected = (selected or "").strip()
    if selected:
        if os.path.isabs(selected):
            return selected if os.access(selected, os.X_OK) else None
        return which(selected, environment)
    for release in LLVM_RELEASES:
        found = which(f"mull-runner-{release}", environment)
        if found:
            return found
    return None


def probe(selected: str, run: Callable, environment: Mapping[str, str]) -> "Mull | MullProblem":
    """The Mull of the setting ``selected``, its runner and clang looked for
    where a command started with ``environment`` is looked for, its
    versions read with ``run(argv)`` (which returns a completed process),
    or the problem."""
    located = _located(selected, environment)
    if isinstance(located, MullProblem):
        return located
    runner, release, frontend, clang_path = located
    shown = run([runner, "--version"])
    text = shown.stdout or ""
    version, llvm = _VERSION.search(text), _LLVM.search(text)
    if shown.returncode != 0 or version is None or llvm is None:
        return MullProblem(f"{runner} does not report its version and LLVM release "
                           f"(exit {shown.returncode})")
    described = run([clang_path, "--version"])
    line = first_line(described)
    found = _CLANG.search(line)
    if described.returncode != 0 or found is None:
        return MullProblem(f"{clang_path} does not report a clang version "
                           f"(exit {described.returncode})")
    if found.group(1) != release:
        return MullProblem(f"{clang_path} is clang {found.group(1)}, and Mull's frontend "
                           f"{frontend.name} needs clang {release}")
    return Mull(runner=runner, frontend=str(frontend), clang=clang_path,
                version=version.group(1), llvm=llvm.group(1), clang_line=line,
                clang_version=".".join(found.groups()))


def _located(selected: str,
             environment: Mapping[str, str]) -> Tuple[str, str, Path, str] | MullProblem:
    """The runner of the setting ``selected``, its LLVM release, Mull's IR
    frontend beside it and the clang of that release, or why one is
    missing."""
    runner = find_runner(selected, environment)
    if runner is None:
        named = f"the runner {selected}" if selected else "mull-runner-N on PATH"
        return MullProblem(f"Mull was not found ({named}); mutation of C needs Mull's "
                           "runner and IR frontend for an LLVM release from "
                           f"{LLVM_RELEASES[-1]} to {LLVM_RELEASES[0]}, and that clang")
    name = Path(runner).name
    real = Path(os.path.realpath(runner))
    match = _RUNNER.match(name) or _RUNNER.match(real.name)
    if match is None:
        return MullProblem(f"{runner} is not named mull-runner-N, so its LLVM release, "
                           "and the frontend and clang that go with it, are unknown")
    release = match.group(1)
    prefix = real.parent.parent
    frontend = prefix / "lib" / f"mull-ir-frontend-{release}"
    if not frontend.is_file():
        return MullProblem(f"Mull's IR frontend {frontend} is missing beside {runner}")
    clang = prefix / "lib" / f"llvm-{release}" / "bin" / "clang"
    clang_path = (str(clang) if os.access(clang, os.X_OK)
                  else which(f"clang-{release}", environment))
    if clang_path is None:
        return MullProblem(f"no clang {release} was found ({clang} or clang-{release} on "
                           f"PATH); Mull's frontend {frontend.name} plugs into it")
    return runner, release, frontend, clang_path


def config(path: Path, operators: Sequence[str], source: Path) -> Path:
    """Write the ``mull.yml`` at ``path`` that mutates ``source`` alone with
    ``operators``. Its strings are JSON's, which YAML reads as written."""
    pattern = "^" + re.escape(os.path.realpath(source)) + "$"
    path.write_text(f"mutators: {json.dumps(list(operators))}\n"
                    f"includePaths: {json.dumps([pattern])}\n"
                    "quiet: true\n", encoding="utf-8")
    return path
