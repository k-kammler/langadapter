"""The compiler recorder: a project's build runs it as its C compiler.

``python -I _recorder.py <record> <compiler> <arguments>`` runs the compiler
with the arguments, then appends one JSON line to the record: the working
directory, the full command and its exit status. The build sees the compiler's
output and exit status unchanged. ``write_wrapper`` writes the small shell
script a build is given as CC, since some build systems take only a path.

The record lists every command a build gave its compiler, compiles and links
alike. ``read_record`` reads it back as ``Command`` entries.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

#: Compiler arguments that take the next argument as their value.
_VALUED = frozenset({
    "-o", "-I", "-D", "-U", "-include", "-imacros", "-isystem", "-iquote", "-idirafter",
    "-MF", "-MT", "-MQ", "-x", "-L", "-l", "-Xlinker", "-u", "-T", "-aux-info",
    "--param", "-arch", "-target",
})
#: The file extensions of C sources and of the files a link takes.
_SOURCES = (".c",)
_OBJECTS = (".o", ".os", ".lo")
_ARCHIVES = (".a",)
_SHARED = re.compile(r"\.so(\.\d+)*$")


def is_link_input(value: str) -> bool:
    """Whether a link command's operand is an object (.o, .os, .lo), an
    archive or a shared library."""
    return not value.startswith("-") and (value.endswith(_OBJECTS + _ARCHIVES)
                                          or _SHARED.search(value) is not None)


@dataclass(frozen=True)
class Command:
    """One recorded compiler command: its working directory, its arguments
    (without the compiler) and its exit status."""

    cwd: str
    compiler: str
    arguments: Tuple[str, ...]
    returncode: int

    def path(self, value: str) -> Path:
        """``value`` as a path, resolved against the command's directory."""
        candidate = Path(value)
        return candidate if candidate.is_absolute() else Path(self.cwd) / candidate

    @property
    def compiles(self) -> bool:
        """Whether the command compiles without linking (``-c``)."""
        return "-c" in self.arguments

    @property
    def archive(self) -> Optional[Path]:
        """The archive the command writes, resolved, when it is an ``ar`` that
        names its archive and members, or None for another command."""
        if Path(self.compiler).name.endswith("ar") and len(self.arguments) >= 2:
            return self.path(self.arguments[1])
        return None

    def sources(self) -> Tuple[Path, ...]:
        """The C sources the command names, resolved."""
        return tuple(self.path(value) for value in self._operands()
                     if value.endswith(_SOURCES))

    def inputs(self) -> Tuple[Path, ...]:
        """The objects and archives a link command takes, resolved, in order."""
        return tuple(self.path(value) for value in self._operands() if is_link_input(value))

    def output(self) -> Optional[Path]:
        """The file the command writes: its ``-o`` value, else the compiler's
        default (the source's stem with ``.o`` for a compile of one source,
        ``a.out`` for a link)."""
        arguments = self.arguments
        for index, value in enumerate(arguments):
            if value == "-o" and index + 1 < len(arguments):
                return self.path(arguments[index + 1])
            if value.startswith("-o") and len(value) > 2:
                return self.path(value[2:])
        if self.compiles:
            sources = self.sources()
            return Path(self.cwd) / (sources[0].stem + ".o") if len(sources) == 1 else None
        return Path(self.cwd) / "a.out"

    def _operands(self) -> List[str]:
        """The arguments that are not options or option values."""
        found, skip = [], False
        for value in self.arguments:
            if skip:
                skip = False
                continue
            if value in _VALUED:
                skip = True
                continue
            if value.startswith("-"):
                continue
            found.append(value)
        return found


def holders_of(commands: Sequence[Command], output: Optional[Path]) -> set:
    """The real paths of ``output`` and of the archives that successful
    recorded ``ar`` commands put it in, directly or through another archive."""
    holders = {os.path.realpath(output)} if output is not None else set()
    grew = True
    while grew:
        grew = False
        for command in commands:
            archive = command.archive if command.returncode == 0 else None
            if archive is None or os.path.realpath(archive) in holders:
                continue
            if any(os.path.realpath(command.path(member)) in holders
                   for member in command.arguments[2:]):
                holders.add(os.path.realpath(archive))
                grew = True
    return holders


def write_wrapper(path: Path, record: Path, compiler: str) -> Path:
    """Write at ``path`` an executable script that records each command into
    ``record`` and runs ``compiler``. Return ``path``."""
    script = Path(__file__).resolve()
    text = ("#!/bin/sh\n"
            f"exec {shlex.quote(sys.executable)} -I {shlex.quote(str(script))} "
            f"{shlex.quote(str(record))} {shlex.quote(compiler)} \"$@\"\n")
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def read_record(record: Path) -> Tuple[Command, ...]:
    """The commands of ``record`` in the order they ran."""
    if not record.is_file():
        return ()
    commands = []
    for line in record.read_text(encoding="utf-8").splitlines():
        if line.strip():
            entry = json.loads(line)
            commands.append(Command(entry["cwd"], entry["argv"][0], tuple(entry["argv"][1:]),
                                    int(entry["returncode"])))
    return tuple(commands)


def main(argv: Sequence[str]) -> int:
    """Run the compiler command ``argv[1:]`` and append it to the record
    ``argv[0]``."""
    import fcntl  # POSIX only: the recorder runs in Linux builds

    record, command = argv[0], list(argv[1:])
    completed = subprocess.run(command)
    line = json.dumps({"cwd": os.getcwd(), "argv": command,
                       "returncode": completed.returncode}) + "\n"
    # Builds run commands in parallel: one locked write per line, appended.
    descriptor = os.open(record, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.write(descriptor, line.encode("utf-8"))
    finally:
        os.close(descriptor)
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
