"""Interactive tutorials: steps that explain, write prepared files and check.

``langadapter tutorial`` builds an example language step by step
(``langadapter.tutorial.language``), and other packages build their
tutorials on this engine. Each step explains what to do and why, shows the code of its
prepared solution, writes it into the tutorial's directory and runs the step's
check there. A learner who edited a file can keep their version, and the
check runs on it. A step can also run an external command, naming how to
install its program when it is missing.

Three modes: interactive (the learner chooses at each step), auto (no
questions, paced for watching) and instant (no questions, no pauses, for
automated runs). The directory must be new or empty, so a tutorial never
changes files it did not write. Ctrl+C stops the tutorial and leaves the
files written so far.
"""
from __future__ import annotations

import argparse
import difflib
import os
import shutil
import subprocess
import sys
import sysconfig
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping, Optional, Sequence, TextIO, Tuple


INTERACTIVE = "interactive"
"""The mode that shows each step and waits for the learner."""
AUTO = "auto"
"""The mode that plays every step without questions, paced for watching."""
INSTANT = "instant"
"""The mode that plays every step without questions or pauses."""
MODES = (INTERACTIVE, AUTO, INSTANT)
"""The modes ``play`` takes."""

# Seconds at speed 1 in auto mode: after each line of shown code, and after
# each step's text and check.
_LINE_PAUSE = 0.08
_STEP_PAUSE = 1.5
_WIDTH = 88
# Seconds an interrupted command has to stop by itself, then after
# terminate() before kill().
_STOP_GRACE = 10.0
_TERMINATE_GRACE = 5.0


@dataclass(frozen=True)
class CheckResult:
    """What a step's check found: whether the step is done, and the lines to
    show the learner, each a plain sentence. ``ok`` says whether the step is
    done."""

    ok: bool
    lines: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Step:
    """One step of a tutorial.

    ``text`` holds paragraphs: what to do, why, whether it is optional and
    how versions fit. ``files`` maps a path relative to the tutorial's
    directory, with ``/`` separators, to the prepared content the step writes,
    and ``show`` names the files whose code is shown, in order. ``changes``
    maps a shown file to its previous content, so that only the changes are
    shown. ``label`` replaces "required" or "optional" in the heading.
    ``check`` takes the directory and returns a ``CheckResult``. ``command``
    runs in the directory after the check, with ``env`` added, and
    ``install_hint`` says how to get its program. The screen shows each
    ``env`` value, so it never holds a secret. A value replaces the variable
    for the command: to extend a path, include the learner's value, and keep
    names upper case. An invalid path, or a shown or changed file the step
    does not write, raises ValueError.
    """

    title: str
    text: Tuple[str, ...] = ()
    files: Mapping[str, str] = field(default_factory=dict)
    show: Tuple[str, ...] = ()
    check: Optional[Callable[[Path], CheckResult]] = None
    command: Tuple[str, ...] = ()
    install_hint: str = ""
    env: Mapping[str, str] = field(default_factory=dict)
    optional: bool = False
    changes: Mapping[str, str] = field(default_factory=dict)
    label: str = ""

    def __post_init__(self) -> None:
        for name in self.files:
            _relative(name)
        unknown = [name for name in self.show if name not in self.files]
        if unknown:
            raise ValueError(f"step {self.title!r} shows files it does not write: "
                             f"{', '.join(unknown)}")
        unshown = [name for name in self.changes if name not in self.show]
        if unshown:
            raise ValueError(f"step {self.title!r} has changes of files it does not show: "
                             f"{', '.join(unshown)}")


@dataclass(frozen=True)
class Tutorial:
    """A titled list of steps, with paragraphs before the first and after the
    last. ``intro`` holds the paragraphs before the first step."""

    title: str
    steps: Tuple[Step, ...]
    intro: Tuple[str, ...] = ()
    closing: Tuple[str, ...] = ()


def _relative(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (not name or "\\" in name or path.is_absolute() or ".." in path.parts
            or ":" in name):
        raise ValueError(f"a tutorial file must be a relative path inside its "
                         f"directory: {name!r}")
    return path


class _Stopped(Exception):
    """The learner chose to quit."""


class _Screen:
    def __init__(self, out: TextIO, mode: str, speed: float) -> None:
        self.out = out
        self.mode = mode
        self.speed = speed

    def write(self, text: str = "") -> None:
        self.out.write(text + "\n")
        self.out.flush()

    def paragraphs(self, paragraphs: Sequence[str]) -> None:
        for paragraph in paragraphs:
            self.write(textwrap.fill(paragraph, _WIDTH))
            self.write()

    def pause(self, seconds: float) -> None:
        if self.mode == AUTO and seconds > 0:
            time.sleep(seconds / self.speed)

    def code(self, name: str, content: str) -> None:
        self.write(f"--- {name}")
        for number, line in enumerate(content.splitlines(), 1):
            self.write(f"{number:4d}  {line}")
            self.pause(_LINE_PAUSE)
        self.write("---")
        self.write()

    def changes(self, name: str, before: str, after: str) -> None:
        """What changed in ``name`` from ``before`` to ``after``: removed
        lines with ``-``, added lines with ``+``, two lines of context."""
        self.write(f"--- {name}, the changes from the previous step")
        diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=2)
        for line in list(diff)[2:]:
            self.write(line)
            self.pause(_LINE_PAUSE)
        self.write("---")
        self.write()


def _choose(ask: Callable[[str], str], prompt: str, allowed: str) -> str:
    while True:
        answer = ask(prompt).strip().lower()[:1]
        if answer in allowed:
            return answer
        if answer == "":
            return allowed[0]


def _differs(project_dir: Path, step: Step) -> Tuple[str, ...]:
    """The step's files the learner already has with other content. A file
    that is not UTF-8 text counts as changed."""
    changed = []
    for name, content in step.files.items():
        target = project_dir / name
        if not target.is_file():
            continue
        try:
            same = target.read_text(encoding="utf-8") == content
        except UnicodeDecodeError:
            same = False
        if not same:
            changed.append(name)
    return tuple(changed)


def _write(project_dir: Path, step: Step) -> None:
    for name, content in step.files.items():
        target = project_dir.joinpath(*_relative(name).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")


def _find_program(name: str) -> Optional[str]:
    """``name`` in the running Python's scripts directory first, as in a virtual
    environment that is not on PATH, then next to the Python, then on PATH."""
    for search_dir in (sysconfig.get_path("scripts"), str(Path(sys.executable).parent)):
        found = shutil.which(name, path=search_dir) if search_dir else None
        if found:
            return found
    return shutil.which(name)


def _wait_after_interrupt(process: subprocess.Popen) -> None:
    """Give a program interrupted with Ctrl+C time to stop its own work, then
    end it. A second Ctrl+C skips the wait."""
    try:
        process.wait(timeout=_STOP_GRACE)
        return
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        pass
    process.terminate()
    try:
        process.wait(timeout=_TERMINATE_GRACE)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        process.kill()
        process.wait()


def _run_command(screen: _Screen, project_dir: Path, step: Step) -> bool:
    program = _find_program(step.command[0])
    if program is None:
        screen.write(f"{step.command[0]} is not installed here, so this step does not "
                     f"run it.")
        if step.install_hint:
            screen.write(step.install_hint)
        screen.write(f"The command: {' '.join(step.command)}")
        screen.write()
        return True
    for name, value in step.env.items():
        screen.write(f"With {name}={value}:")
    screen.write(f"$ {' '.join(step.command)}")
    environment = {**os.environ, **step.env} if step.env else None
    process = subprocess.Popen([program, *step.command[1:]], cwd=str(project_dir),
                               env=environment)
    try:
        status = process.wait()
    except KeyboardInterrupt:
        # Ctrl+C reaches the program too. It gets time to clean up after
        # itself before the tutorial stops it.
        _wait_after_interrupt(process)
        raise
    screen.write()
    if status != 0:
        screen.write(f"{step.command[0]} ended with status {status}.")
        screen.write()
    return status == 0


def _play_step(screen: _Screen, project_dir: Path, step: Step, index: int, total: int,
               ask: Callable[[str], str]) -> bool:
    label = step.label or ("optional" if step.optional else "required")
    screen.write("=" * _WIDTH)
    screen.write(f"Step {index} of {total} ({label}): {step.title}")
    screen.write("=" * _WIDTH)
    screen.paragraphs(step.text)
    for name in step.show:
        if name in step.changes:
            screen.changes(name, step.changes[name], step.files[name])
        else:
            screen.code(name, step.files[name])
    screen.pause(_STEP_PAUSE)
    own = ()
    if screen.mode == INTERACTIVE and step.files:
        changed = _differs(project_dir, step)
        if changed:
            choice = _choose(ask, f"You changed {', '.join(changed)}. [Enter] use the "
                             "prepared solution, [k] keep yours and check it, "
                             "[s] skip, [q] quit: ", "pksq")
        else:
            choice = _choose(ask, "[Enter] write the prepared solution, [s] skip, "
                             "[q] quit: ", "psq")
        screen.write()
        if choice == "q":
            raise _Stopped()
        if choice == "s":
            screen.write("Skipped. This step is optional." if step.optional else
                         "Skipped. Later checks may report what this step adds.")
            screen.write()
            return step.optional
        if choice == "k":
            own = changed
    elif screen.mode == INTERACTIVE and (step.command or step.check):
        if _choose(ask, "[Enter] continue, [q] quit: ", "pq") == "q":
            raise _Stopped()
        screen.write()
    if own:
        keep = dict(step.files)
        for name in own:
            del keep[name]
        _write(project_dir, Step(step.title, files=keep))
        screen.write(f"Kept your version of {', '.join(own)}. The check runs on it.")
        screen.write()
    elif step.files:
        _write(project_dir, step)
        screen.write(f"Wrote {', '.join(step.files)}.")
        screen.write()
    ok = True
    if step.check is not None:
        try:
            result = step.check(project_dir)
        except Exception as exc:
            # A learner's unfinished code is the normal case, not a crash.
            result = CheckResult(False, (f"the check could not run: "
                                         f"{type(exc).__name__}: {exc}",))
        screen.write("Check: " + ("done." if result.ok else "not yet."))
        for line in result.lines:
            screen.write(textwrap.fill(line, _WIDTH, initial_indent="  ",
                                       subsequent_indent="  "))
        screen.write()
        ok = result.ok
    if step.command:
        ok = _run_command(screen, project_dir, step) and ok
    screen.pause(_STEP_PAUSE)
    return ok


def play(tutorial: Tutorial, project_dir, *, mode: str = INTERACTIVE, speed: float = 1.0,
         out: Optional[TextIO] = None,
         ask: Optional[Callable[[str], str]] = None) -> int:
    """Play ``tutorial`` in ``project_dir``, which must be new or empty.

    Returns 0 when every step that ran was done, 1 when a check reported a
    step not done, a command failed or the learner skipped a required step,
    2 when the directory cannot be used and 130 when the learner stopped or
    pressed Ctrl+C. ``ask`` reads the learner's choice in interactive mode
    (``input`` by default). An unknown mode or a speed that is not positive
    raises ValueError.
    """
    if mode not in MODES:
        raise ValueError(f"unknown tutorial mode {mode!r}: choose {', '.join(MODES)}")
    if not speed > 0:
        raise ValueError("the tutorial speed must be positive")
    out = out if out is not None else sys.stdout
    ask = ask if ask is not None else input
    screen = _Screen(out, mode, speed)
    project_dir = Path(project_dir)
    if project_dir.exists() and (not project_dir.is_dir() or any(project_dir.iterdir())):
        screen.write(f"The tutorial writes only into a new or empty directory: {project_dir}")
        return 2
    project_dir.mkdir(parents=True, exist_ok=True)
    screen.write(tutorial.title)
    screen.write()
    screen.paragraphs(tutorial.intro)
    screen.write(f"Files go to {project_dir.resolve()}")
    screen.write()
    all_done = True
    try:
        for index, step in enumerate(tutorial.steps, 1):
            all_done = _play_step(screen, project_dir, step, index, len(tutorial.steps),
                                  ask) and all_done
    except (_Stopped, KeyboardInterrupt, EOFError) as stop:
        screen.write()
        if isinstance(stop, EOFError):
            screen.write("No input: use --auto to play without questions.")
        screen.write(f"Tutorial stopped. The files written so far are in {project_dir}.")
        return 130
    screen.paragraphs(tutorial.closing)
    return 0 if all_done else 1


def _add_options(result: argparse.ArgumentParser, default: str) -> argparse.ArgumentParser:
    """Add the options every tutorial command shares to ``result``, whose
    files go to ./``default`` without --output-dir."""
    result.epilog = f"Without --output-dir the files go to ./{default}, which must be new or empty."
    result.add_argument("--output-dir", default=default,
                        help="A new or empty directory for the tutorial's files.")
    result.add_argument("--auto", action="store_true",
                        help="Play every step without questions, paced for watching.")
    result.add_argument("--speed", type=float, default=1.0,
                        help="With --auto: a factor of the pace, 1 by default (2 is twice as fast).")
    return result


def parser(prog: str, description: str) -> argparse.ArgumentParser:
    """The command line every tutorial command shares."""
    return _add_options(argparse.ArgumentParser(prog=prog, description=description,
                                                allow_abbrev=False), prog.replace(" ", "-"))


def main(argv: Optional[Sequence[str]], tutorial: Tutorial, *, prog: str,
         description: Optional[str] = None) -> int:
    """Run ``tutorial`` from a command line. ``description`` is the command's
    help text, by default the tutorial's title."""
    options = parser(prog, description or tutorial.title).parse_args(argv)
    if options.speed <= 0:
        print(f"{prog}: error: --speed must be positive", file=sys.stderr)
        return 2
    return play(tutorial, options.output_dir,
                mode=AUTO if options.auto else INTERACTIVE, speed=options.speed)


__all__ = [
    "AUTO",
    "INSTANT",
    "INTERACTIVE",
    "MODES",
    "CheckResult",
    "Step",
    "Tutorial",
    "main",
    "parser",
    "play",
]
