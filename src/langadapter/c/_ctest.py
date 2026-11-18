"""A CMake project's own tests as ctest runs them (``c_tests`` ``ctest``), as its baseline.

ctest lists the tests (``ctest --show-only=json-v1`` in the adapter's build
of the project): each its command, its working directory, its environment and
the properties by which ctest judges it. The program a test's command runs
is relinked from the build's record (``TargetBuild.program_command``) with
the run's object of the target (its coverage, plain or Mull build) ahead of
the project's own objects and libraries, which keep the project's build. A
program many tests run is relinked once. Each test then runs in a process
of its own, as ctest runs it: its arguments, its working directory (the
``WORKING_DIRECTORY`` ctest lists) and its ``ENVIRONMENT``, under its
``TIMEOUT``. ctest's rules decide the verdict: exit status 0 passes, unless
pass or fail regular expressions decide, ``SKIP_RETURN_CODE`` and the skip
regular expressions skip, and ``WILL_FAIL`` inverts. A test a signal ends
is an error.

A test the adapter cannot run as ctest would is reported skipped, with the
reason: ctest lists no command or no working directory for it, its command
runs no program the build's record linked (a script, another tool), its
program does not link the target, ctest disables it, it is a fixture step
whose order ctest keeps, or it has an ``ENVIRONMENT_MODIFICATION``.

A named test gets extra arguments after its own (``c_ctest_arguments``):
a test that seeds itself from an argument or else from the clock (libcbor's
fuzz_test) is then the same in every run.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from langadapter.c._build import BUILD_DIR, RECORD
from langadapter.c._recorder import holders_of, read_record
from langadapter.c._utest import TestRun, last_lines, outputs, signal_name
from langadapter.execution import _as_text
from langadapter.outcomes import Outcome

#: The file of ctest's listing in a run's directory, and of the tests' runs.
LISTING = "ctest.json"
SPECS = "tests.json"
#: The properties by which ctest orders fixture steps, which the adapter does not keep.
_FIXTURES = ("FIXTURES_SETUP", "FIXTURES_CLEANUP", "FIXTURES_REQUIRED")
#: What ``{seed}`` in ``c_ctest_arguments`` stands for: a C run carries no
#: session seed (the adapter refuses one).
SEED = 1


def extra_arguments(setting: str) -> Dict[str, List[str]]:
    """``c_ctest_arguments`` by test name: entries ``NAME ARGUMENT...``
    separated by ``;``, each ``{seed}`` replaced by ``SEED``. A name given
    twice gets both entries' arguments."""
    value = str(SEED)
    extra: Dict[str, List[str]] = {}
    for entry in setting.split(";"):
        words = shlex.split(entry)
        if words:
            extra.setdefault(words[0], []).extend(
                word.replace("{seed}", value) for word in words[1:])
    return extra


def build_dir(build) -> Path:
    """The cmake kind's build directory of the recorded ``build``."""
    return build.scratch_dir / BUILD_DIR


def list_suite(build, tests_dir: Path, run, timeout_s: float) -> "Path | Tuple[Outcome, str]":
    """ctest's listing of the build's tests, written into ``tests_dir``, or the
    outcome and why ctest could not list them: missing setup when ctest does
    not start, a timeout, else a failed collection."""
    tests_dir.mkdir(parents=True, exist_ok=True)
    try:
        done = run(["ctest", "--show-only=json-v1"], cwd=str(build_dir(build)),
                   timeout=timeout_s)
    except OSError as exc:
        return Outcome.MISSING_SETUP, f"ctest could not list the project's tests: {exc}"
    except subprocess.TimeoutExpired as exc:
        return Outcome.TIMED_OUT, f"ctest could not list the project's tests: {exc}"
    if done.returncode != 0 or not (done.stdout or "").strip():
        text = ((done.stdout or "") + (done.stderr or "")).strip()
        return (Outcome.COLLECTION_FAILED, f"ctest could not list the project's tests "
                f"(exit {done.returncode}): {text[-300:]}")
    listing = tests_dir / LISTING
    listing.write_text(done.stdout, encoding="utf-8")
    return listing


def link_programs(build, listing: Path, target_object: Path, programs_dir: Path, coverage: bool,
                  compile_: Callable[[List[str], str], object],
                  extra: Optional[Dict[str, List[str]]] = None,
                  ) -> Optional[Tuple[Outcome, str, str]]:
    """Relink into ``programs_dir`` each program the listed tests run, with
    ``target_object``, and write the tests' runs (``SPECS``), a test named
    in ``extra`` with those arguments after its own. Return None, or the
    outcome and reason with the output: a relink that reached the run's
    timeout, one that failed, or ``extra`` naming a test ctest does not
    list."""
    extra = extra or {}
    programs_dir.mkdir(parents=True, exist_ok=True)
    record = read_record(build.scratch_dir / RECORD)
    holders = holders_of(record, build.compile.output())
    links = _links(record)
    data = json.loads(listing.read_text(encoding="utf-8"))
    relinked: Dict[str, Path] = {}
    specs: Dict[str, dict] = {}
    for test in data.get("tests", ()):
        name = test["name"]
        command = list(test.get("command") or ())
        properties = {item["name"]: item["value"] for item in test.get("properties", ())}
        reason = _unrunnable(command, properties)
        program = None
        if reason is None:
            executable = os.path.realpath(command[0])
            link = links.get(executable)
            if link is None:
                reason = (f"its command runs {Path(command[0]).name}, which no link of the "
                          "build's record wrote")
            elif not any(os.path.realpath(path) in holders for path in link.inputs()):
                reason = f"its program {Path(command[0]).name} does not link the target"
            elif executable not in relinked:
                output = programs_dir / f"{len(relinked)}-{Path(executable).name}"
                done = compile_(build.program_command(link, target_object, output,
                                                      coverage=coverage), link.cwd)
                if done is None:
                    return (Outcome.TIMED_OUT,
                            "the run reached its timeout while relinking the tests", "")
                if done.returncode != 0:
                    return (Outcome.COLLECTION_FAILED,
                            f"{Path(executable).name} does not relink with the target",
                            (done.stdout or "") + (done.stderr or ""))
                relinked[executable] = output
            program = relinked.get(executable)
        specs[name] = _spec(program, command[1:] + extra.get(name, []), properties, reason)
    unknown = sorted(set(extra) - set(specs))
    if unknown:
        return (Outcome.COLLECTION_FAILED,
                f"c_ctest_arguments names a test ctest does not list: {', '.join(unknown)}", "")
    (programs_dir / SPECS).write_text(json.dumps(specs), encoding="utf-8")
    return None


def _links(record) -> Dict[str, object]:
    """The record's successful links of programs and libraries, by the real
    path of what each wrote."""
    return {os.path.realpath(command.output()): command for command in record
            if command.returncode == 0 and command.compiler and not command.compiles
            and command.archive is None and command.output() is not None}


def _spec(program: Optional[Path], arguments: List[str], properties: dict,
          reason: Optional[str]) -> dict:
    """A listed test's run as ``SPECS`` holds it: its relinked program, its
    arguments and ctest's properties of it, and the reason it cannot run."""
    return {
        "program": str(program) if program is not None else None,
        "arguments": arguments,
        "cwd": properties.get("WORKING_DIRECTORY"),
        "environment": list(properties.get("ENVIRONMENT") or ()),
        "timeout": properties.get("TIMEOUT"),
        "will_fail": bool(properties.get("WILL_FAIL")),
        "skip_code": properties.get("SKIP_RETURN_CODE"),
        "pass": list(properties.get("PASS_REGULAR_EXPRESSION") or ()),
        "fail": list(properties.get("FAIL_REGULAR_EXPRESSION") or ()),
        "skip": list(properties.get("SKIP_REGULAR_EXPRESSION") or ()),
        "reason": reason,
    }


def _unrunnable(command, properties) -> Optional[str]:
    if not command:
        return "ctest lists no command for it"
    if not properties.get("WORKING_DIRECTORY"):
        return "ctest lists no working folder for it"
    if properties.get("DISABLED"):
        return "ctest disables it"
    fixtures = [name for name in _FIXTURES if properties.get(name)]
    if fixtures:
        return f"it is a fixture step ({', '.join(fixtures)}), whose order ctest keeps"
    if properties.get("ENVIRONMENT_MODIFICATION"):
        return "it has ENVIRONMENT_MODIFICATION, which the adapter does not apply"
    return None


def first_program(programs_dir: Path) -> Optional[Path]:
    """The first program relinked into ``programs_dir``, or None."""
    found = [spec["program"] for spec in _load(programs_dir).values() if spec["program"]]
    return Path(found[0]) if found else None


def list_tests(program: Path, run, *, cwd: str, env, timeout_s: float) -> Tuple[str, ...]:
    """The tests ctest listed, in its order."""
    return tuple(_load(program))


def runnable(program: Path) -> Tuple[Tuple[str, dict], ...]:
    """The tests ctest listed that the adapter runs as ctest would, each
    with its run, in ctest's order."""
    return tuple((name, spec) for name, spec in _load(program).items() if not spec["reason"])


def command(spec: dict, env) -> Tuple[List[str], Dict[str, str], Optional[float]]:
    """A runnable test's command, its variables (``env`` with its
    ``ENVIRONMENT``) and its own ``TIMEOUT``, None without one."""
    environment = dict(env)
    for entry in spec["environment"]:
        key, _, value = entry.partition("=")
        environment[key] = value
    own = spec["timeout"]
    return [spec["program"], *spec["arguments"]], environment, float(own) if own else None


def run_one(program: Path, name: str, run, *, cwd: str, env, timeout_s: float) -> TestRun:
    """Run the ctest test ``name`` as ctest runs it and judge it by ctest's rules."""
    spec = _load(program)[name]
    if spec["reason"]:
        return TestRun(name, "skipped", None, f"not run: {spec['reason']}", "")
    argv, environment, own = command(spec, env)
    limit = timeout_s if own is None else min(timeout_s, own)
    try:
        done = run(argv, cwd=spec["cwd"], env=environment, timeout=limit,
                   whole=judged_by_output(spec))
    except subprocess.TimeoutExpired as expired:
        output = outputs(_as_text(expired.stdout), _as_text(expired.stderr))[1]
        if own is not None and own <= timeout_s:
            return TestRun(name, "failed", None, f"ctest's TIMEOUT of {spec['timeout']} s ended it",
                           output)
        return TestRun(name, "timed_out", None, f"the test reached its timeout of {limit:g} s",
                       output)
    return verdict(name, spec, done)


def judged_by_output(spec: dict) -> bool:
    """Whether ctest's regular expressions judge the test by its output,
    which the test's run then keeps whole."""
    return bool(spec["pass"] or spec["fail"] or spec["skip"])


def verdict(name: str, spec: dict, done) -> TestRun:
    """ctest's verdict on the finished run ``done`` of the test ``name``."""
    judged, output = outputs(done.stdout, done.stderr)
    code = done.returncode
    if code is not None and code < 0:
        ended = f"the test process ended by {signal_name(-code)}"
        return TestRun(name, "error", None, last_lines(ended, judged), output)
    if spec["skip_code"] is not None and code == int(spec["skip_code"]):
        return TestRun(name, "skipped", None, f"exit {code}, its SKIP_RETURN_CODE", output)
    if any(re.search(pattern, judged) for pattern in spec["skip"]):
        return TestRun(name, "skipped", None, "its output matches its SKIP_REGULAR_EXPRESSION",
                       output)
    if _passed(spec, judged, code):
        return TestRun(name, "passed", None, "", output)
    return TestRun(name, "failed", None, last_lines(f"the test ended with exit {code}", judged),
                   output)


def _passed(spec: dict, output: str, code) -> bool:
    """Whether ctest counts a run that ended with ``code`` and wrote
    ``output`` as passed: by its PASS_REGULAR_EXPRESSION, else by exit 0,
    failed by a match of its FAIL_REGULAR_EXPRESSION, inverted by
    WILL_FAIL."""
    if spec["pass"]:
        passed = any(re.search(pattern, output) for pattern in spec["pass"])
    else:
        passed = code == 0
    if any(re.search(pattern, output) for pattern in spec["fail"]):
        passed = False
    if spec["will_fail"]:
        passed = not passed
    return passed


def _load(program: Path) -> Dict[str, dict]:
    return json.loads((program / SPECS).read_text(encoding="utf-8"))
