"""The tutorial engine: steps explain, write their prepared files, check the
directory, keep a learner's own version on request and stop cleanly."""
from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from langadapter import tutorial as engine
from langadapter.tutorial import (
    AUTO,
    INSTANT,
    CheckResult,
    Step,
    Tutorial,
    main,
    play,
)


def _has(name, text):
    def check(project_dir):
        path = project_dir / name
        found = path.is_file() and text in path.read_text(encoding="utf-8")
        return CheckResult(found, (f"{name} {'holds' if found else 'lacks'} {text!r}.",))
    return check


def _tutorial(*steps):
    return Tutorial("A small tutorial", tuple(steps), intro=("Before the steps.",),
                    closing=("After the steps.",))


FIRST = Step("Write the first file", ("What and why.",), {"pkg/one.txt": "alpha\n"},
             show=("pkg/one.txt",), check=_has("pkg/one.txt", "alpha"))
SECOND = Step("Write the second file", ("Optional.",), {"two.txt": "beta\n"},
              check=_has("two.txt", "beta"), optional=True)


def _answers(*answers):
    pending = list(answers)
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return pending.pop(0)
    return ask, asked


def test_instant_mode_writes_every_step_and_checks_it(tmp_path):
    out = io.StringIO()
    project_dir = tmp_path / "work"
    assert play(_tutorial(FIRST, SECOND), project_dir, mode=INSTANT, out=out) == 0
    assert (project_dir / "pkg" / "one.txt").read_bytes() == b"alpha\n"
    assert (project_dir / "two.txt").read_text(encoding="utf-8") == "beta\n"
    text = out.getvalue()
    assert "Step 1 of 2 (required): Write the first file" in text
    assert "Step 2 of 2 (optional): Write the second file" in text
    assert "   1  alpha" in text
    assert text.count("Check: done.") == 2
    assert "Before the steps." in text and "After the steps." in text


def test_a_check_that_is_not_done_sets_the_status(tmp_path):
    failing = Step("Check something else", files={"x.txt": "x\n"},
                   check=_has("x.txt", "missing"))
    out = io.StringIO()
    assert play(_tutorial(failing), tmp_path / "w", mode=INSTANT, out=out) == 1
    assert "Check: not yet." in out.getvalue()
    assert "x.txt lacks 'missing'." in out.getvalue()


def test_interactive_mode_writes_on_enter_and_skips_on_s(tmp_path):
    ask, asked = _answers("", "s")
    out = io.StringIO()
    project_dir = tmp_path / "w"
    assert play(_tutorial(FIRST, SECOND), project_dir, out=out, ask=ask) == 0
    assert (project_dir / "pkg" / "one.txt").is_file()
    assert not (project_dir / "two.txt").exists()
    assert "Skipped. This step is optional." in out.getvalue()
    assert all("[Enter] write the prepared solution" in prompt for prompt in asked)
    # Skipping a required step leaves the tutorial not done.
    ask, _ = _answers("s")
    assert play(_tutorial(FIRST), tmp_path / "w2", out=io.StringIO(), ask=ask) == 1


def test_a_learners_own_version_is_kept_and_checked(tmp_path):
    later = Step("Extend the first file", files={"pkg/one.txt": "alpha\ngamma\n"},
                 check=_has("pkg/one.txt", "mine"))
    project_dir = tmp_path / "w"

    def learner_edits(project_dir):
        # Stands for the learner editing the file between two steps.
        (project_dir / "pkg" / "one.txt").write_text("alpha\nmine\n", encoding="utf-8")
        return CheckResult(True)

    ask, asked = _answers("", "", "k")
    out = io.StringIO()
    assert play(_tutorial(FIRST, Step("Edit it yourself", check=learner_edits), later),
                project_dir, out=out, ask=ask) == 0
    assert "You changed pkg/one.txt." in asked[-1]
    assert (project_dir / "pkg" / "one.txt").read_text(encoding="utf-8") == "alpha\nmine\n"
    assert "Kept your version of pkg/one.txt. The check runs on it." in out.getvalue()
    # Only a closing run's own text speaks of a recorded run.
    assert "recorded run" not in out.getvalue()


def test_quit_and_ctrl_c_stop_and_keep_the_files(tmp_path):
    ask, _ = _answers("", "q")
    out = io.StringIO()
    project_dir = tmp_path / "w"
    assert play(_tutorial(FIRST, SECOND), project_dir, out=out, ask=ask) == 130
    assert (project_dir / "pkg" / "one.txt").is_file()
    assert "Tutorial stopped." in out.getvalue()

    def interrupted(prompt):
        raise KeyboardInterrupt
    assert play(_tutorial(FIRST), tmp_path / "w2", out=io.StringIO(),
                ask=interrupted) == 130


def test_the_dir_must_be_new_or_empty(tmp_path):
    (tmp_path / "used.txt").write_text("keep\n", encoding="utf-8")
    out = io.StringIO()
    assert play(_tutorial(FIRST), tmp_path, mode=INSTANT, out=out) == 2
    assert "new or empty directory" in out.getvalue()
    assert (tmp_path / "used.txt").read_text(encoding="utf-8") == "keep\n"
    assert not (tmp_path / "pkg").exists()


@pytest.mark.parametrize("name", ["../out.txt", "/abs.txt", "C:/x.txt", "a\\b.txt", ""])
def test_a_step_writes_only_inside_its_dir(name):
    with pytest.raises(ValueError, match="relative path inside its directory"):
        Step("Escape", files={name: "x"})


def test_a_step_can_show_only_what_changed(tmp_path):
    before = "one\ntwo\nthree\nfour\n"
    after = "one\ntwo\nTHREE\nfour\nfive\n"
    step = Step("Change a file", files={"a.txt": after}, show=("a.txt",),
                changes={"a.txt": before})
    out = io.StringIO()
    assert play(_tutorial(step), tmp_path / "w", mode=INSTANT, out=out) == 0
    text = out.getvalue()
    assert "--- a.txt, the changes from the previous step" in text
    assert "-three" in text and "+THREE" in text and "+five" in text
    assert "   1  one" not in text
    with pytest.raises(ValueError, match="changes of files it does not show"):
        Step("Hidden", files={"a.txt": after}, changes={"a.txt": before})


def test_a_step_shows_only_files_it_writes():
    with pytest.raises(ValueError, match="shows files it does not write"):
        Step("Show", files={"a.txt": "a"}, show=("b.txt",))


def test_a_missing_program_is_named_with_its_install_hint(tmp_path):
    step = Step("Run the program", command=("no-such-program-for-tutorials", "run"),
                install_hint="Install it with: pip install the-program")
    out = io.StringIO()
    assert play(_tutorial(step), tmp_path / "w", mode=INSTANT, out=out) == 0
    text = out.getvalue()
    assert "no-such-program-for-tutorials is not installed here" in text
    assert "pip install the-program" in text
    assert "The command: no-such-program-for-tutorials run" in text


def test_an_installed_program_runs_in_the_dir(tmp_path):
    project_dir = tmp_path / "w"
    step = Step("Run Python", files={"probe.py": "open('ran.txt', 'w').write('yes')\n"},
                command=(sys.executable, "probe.py"))
    assert play(_tutorial(step), project_dir, mode=INSTANT, out=io.StringIO()) == 0
    assert (project_dir / "ran.txt").read_text(encoding="utf-8") == "yes"
    failing = Step("Fail", command=(sys.executable, "-c", "raise SystemExit(3)"))
    out = io.StringIO()
    assert play(_tutorial(failing), tmp_path / "w2", mode=INSTANT, out=out) == 1
    assert "ended with status 3" in out.getvalue()


def test_a_command_gets_the_steps_environment(tmp_path):
    project_dir = tmp_path / "w"
    step = Step("Run with a setting", command=(
        sys.executable, "-c",
        "import os; open('seen.txt', 'w').write(os.environ['TUTORIAL_SETTING'])"),
        env={"TUTORIAL_SETTING": "on"})
    out = io.StringIO()
    assert play(_tutorial(step), project_dir, mode=INSTANT, out=out) == 0
    assert (project_dir / "seen.txt").read_text(encoding="utf-8") == "on"
    assert "With TUTORIAL_SETTING=on:" in out.getvalue()
    assert "TUTORIAL_SETTING" not in os.environ


def test_auto_mode_asks_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)

    def no_questions(prompt):
        raise AssertionError("auto mode asked: " + prompt)
    assert play(_tutorial(FIRST, SECOND), tmp_path / "w", mode=AUTO, out=io.StringIO(),
                ask=no_questions) == 0


def test_a_check_that_raises_reports_not_yet(tmp_path):
    def broken(project_dir):
        raise ImportError("adapter.py has an error")
    out = io.StringIO()
    assert play(_tutorial(Step("Check a broken adapter", check=broken)), tmp_path / "w",
                mode=INSTANT, out=out) == 1
    assert "Check: not yet." in out.getvalue()
    assert "the check could not run: ImportError: adapter.py has an error" in out.getvalue()

    def interrupted(project_dir):
        raise KeyboardInterrupt
    assert play(_tutorial(Step("Interrupt the check", check=interrupted)),
                tmp_path / "w2", mode=INSTANT, out=io.StringIO()) == 130


class _Process:
    """A stand-in for a started program: ``stops_after`` waits until it ends
    by itself, None for one that never does."""

    def __init__(self, stops_after=None):
        self.stops_after = stops_after
        self.calls = []

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        if len(self.calls) == 1:
            raise KeyboardInterrupt
        if self.stops_after is not None and len(self.calls) - 1 >= self.stops_after:
            return 0
        if "kill" in self.calls:
            return -9
        if timeout is not None:
            raise subprocess.TimeoutExpired("program", timeout)
        return 0

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")


@pytest.mark.parametrize("stops_after,expected", [
    (1, ["wait"]),
    (None, ["wait", "terminate", "wait", "kill", "wait"]),
])
def test_ctrl_c_gives_the_command_time_then_ends_it(tmp_path, monkeypatch,
                                                     stops_after, expected):
    process = _Process(stops_after)
    monkeypatch.setattr(engine.subprocess, "Popen", lambda *args, **kwargs: process)
    step = Step("Run Python", command=(sys.executable, "-c", "pass"))
    assert play(_tutorial(step), tmp_path / "w", mode=INSTANT, out=io.StringIO()) == 130
    after = [call if isinstance(call, str) else call[0] for call in process.calls[1:]]
    assert after == expected
    assert process.calls[1] == ("wait", engine._STOP_GRACE)


def test_a_second_ctrl_c_ends_the_command_at_once(tmp_path, monkeypatch):
    class Impatient(_Process):
        def wait(self, timeout=None):
            self.calls.append(("wait", timeout))
            if len(self.calls) <= 2:
                raise KeyboardInterrupt
            return 0

    process = Impatient()
    monkeypatch.setattr(engine.subprocess, "Popen", lambda *args, **kwargs: process)
    step = Step("Run Python", command=(sys.executable, "-c", "pass"))
    assert play(_tutorial(step), tmp_path / "w", mode=INSTANT, out=io.StringIO()) == 130
    assert [call if isinstance(call, str) else call for call in process.calls] == [
        ("wait", None), ("wait", engine._STOP_GRACE), "terminate",
        ("wait", engine._TERMINATE_GRACE)]


def _probe_program(program_dir, name):
    program_dir.mkdir()
    program = program_dir / (name + (".bat" if os.name == "nt" else ""))
    program.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n",
                       encoding="utf-8")
    program.chmod(0o755)


def test_a_program_next_to_the_running_python_is_found(tmp_path, monkeypatch):
    environment = tmp_path / "venv-not-on-path"
    name = "tutorial-probe-program"
    _probe_program(environment, name)
    monkeypatch.setattr(engine.sys, "executable", str(environment / "python"))
    assert Path(engine._find_program(name)).parent == environment


def test_a_program_in_the_scripts_dir_is_found(tmp_path, monkeypatch):
    # A base Windows Python keeps its scripts in <prefix>\Scripts, not beside
    # python.exe.
    scripts = tmp_path / "Scripts"
    name = "tutorial-probe-script"
    _probe_program(scripts, name)
    monkeypatch.setattr(engine.sysconfig, "get_path",
                        lambda key: str(scripts) if key == "scripts" else None)
    monkeypatch.setattr(engine.sys, "executable", str(tmp_path / "python"))
    assert Path(engine._find_program(name)).parent == scripts


def test_an_undecodable_file_counts_as_changed(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"\xff\xfe\x00not text")
    assert engine._differs(tmp_path, Step("Overwrite", files={"a.txt": "a\n"})) == (
        "a.txt",)


def test_closed_input_names_auto_mode(tmp_path):
    def closed(prompt):
        raise EOFError
    out = io.StringIO()
    assert play(_tutorial(FIRST), tmp_path / "w", out=out, ask=closed) == 130
    assert "No input: use --auto to play without questions." in out.getvalue()


def test_the_help_names_the_default_dir_once(capsys):
    with pytest.raises(SystemExit):
        main(["--help"], _tutorial(FIRST), prog="example tutorial",
             description="Learn the example step by step.")
    text = " ".join(capsys.readouterr().out.split())
    assert "Learn the example step by step." in text
    assert text.count("./example-tutorial") == 1


def test_the_command_line_has_a_default_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    monkeypatch.chdir(tmp_path)
    assert main(["--auto"], _tutorial(FIRST), prog="example tutorial") == 0
    assert (tmp_path / "example-tutorial" / "pkg" / "one.txt").is_file()


def test_the_command_line_runs_a_tutorial(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    project_dir = tmp_path / "w"
    assert main(["--output-dir", str(project_dir), "--auto"], _tutorial(FIRST),
                prog="example tutorial") == 0
    assert (project_dir / "pkg" / "one.txt").is_file()
    assert main(["--output-dir", str(tmp_path / "w2"), "--auto", "--speed", "0"],
                _tutorial(FIRST), prog="example tutorial") == 2
    assert "--speed must be positive" in capsys.readouterr().err
