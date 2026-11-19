"""The C adapter's setup check, and its refusal on a system other than Linux
in every report: operations, the capability probe, the setup checks and
`langadapter check c`."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

import langadapter as la
from langadapter.capabilities import Availability
from langadapter.runs import SuiteRunRequest

from conftest import needs_gcc

#: The refusal on Windows names WSL, elsewhere only Linux.
LINUX_ONLY = "the C adapter runs on Linux only in this version"
WINDOWS = LINUX_ONLY + ", on Windows in WSL"


@pytest.fixture
def elsewhere(monkeypatch):
    """The refusal this system gives: on Windows itself its own, else as on
    macOS (sys.platform darwin)."""
    if sys.platform == "win32":
        return WINDOWS
    monkeypatch.setattr(sys, "platform", "darwin")
    return LINUX_ONLY


def _project(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "calc.c").write_text("int one(void) { return 1; }\n", encoding="ascii")
    (tmp_path / "test_calc.c").write_text(
        '#include "utest.h"\nint one(void);\nUTEST(one, is_one) { ASSERT_EQ(1, one()); }\n'
        "UTEST_MAIN()\n", encoding="ascii")
    return tmp_path


def test_off_linux_every_run_and_probe_names_the_system_first(tmp_path, elsewhere):
    """Without a compiler off Linux, a run, the capability probe and each
    operation that runs a tool name the system first, not the missing
    runtime."""
    root = _project(tmp_path)
    adapter = la.create("c", la.ProjectContext(str(root)))
    run = adapter.tests.run_suite(SuiteRunRequest(la.Suite(str(root), ("test_calc.c",)),
                                                  timeout_s=60))
    assert (run.outcome, run.reason) == (la.Outcome.MISSING_SETUP, elsewhere)
    probe = adapter.probe_capabilities()
    assert (probe.outcome, probe.reason) == (la.Outcome.MISSING_SETUP, elsewhere)
    backed = [o for o in adapter.operation_capabilities() if o.supported and o.execution_backed]
    assert backed and {o.reason for o in backed} == {elsewhere}


def test_off_linux_the_setup_checks_name_the_system(tmp_path, elsewhere):
    """Off Linux the inspection and the tool check read missing setup with the
    system's refusal on the runtime item, whose next step names Linux, and
    WSL on Windows."""
    root = _project(tmp_path)
    request = la.ProjectSetupRequest(la.ProjectContext(str(root)), ("calc.c",),
                                     ("tests.run_suite",))
    for check in (la.inspect_project, la.verify_project):
        report = check("c", request)
        assert (report.outcome, report.reason) == (la.Outcome.MISSING_SETUP, elsewhere)
        [item] = report.items
        assert (item.key, item.readiness, item.reason) == (
            "runtime", Availability.MISSING_SETUP, elsewhere)
        assert item.action.startswith("run it on Linux")
        assert ("WSL" in item.action) is (sys.platform == "win32")
        run_suite = next(o for o in report.operations if o.name == "tests.run_suite")
        assert (run_suite.readiness, run_suite.reason) == (Availability.MISSING_SETUP, elsewhere)


def test_check_c_names_the_system_off_linux(tmp_path, elsewhere, capsys, monkeypatch):
    """`langadapter check c` off Linux says first that the adapter does not
    run on this system, reads no consumer as ready and ends with status 1."""
    from langadapter.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["check", "c"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[:2] == ["Language: c", f"This system: {elsewhere}."]
    assert not [line for line in lines if "everything required is there" in line]


def _request(root, *operations, path=None, **settings):
    """A setup request for the project at ``root``, with the C ``settings``
    in its environment and ``path`` as its PATH when given."""
    from langadapter.c.adapter import CAdapter

    variables = dict(CAdapter.context_environment(settings), **({"PATH": path} if path else {}))
    context = la.ProjectContext(str(root), environment=la.Environment(variables)
                                if variables else None)
    return la.ProjectSetupRequest(context, ("calc.c",), operations or ("tests.run_suite",))


@needs_gcc
def test_the_inspection_reads_the_compiler_and_finds_the_tools(tmp_path):
    """On Linux the inspection starts the compiler and its gcov for their
    versions and finds the extra's modules and stdbuf. stdbuf is tried by the
    tool check."""
    report = la.inspect_project("c", _request(_project(tmp_path)))
    assert report.outcome is la.Outcome.COMPLETED, report.reason
    items = {item.key: item for item in report.items}
    runtime = items["runtime"]
    assert runtime.readiness is Availability.AVAILABLE and re.fullmatch(r"\d+\.\d+\.\d+",
                                                                        runtime.version)
    assert all(items[name].readiness is Availability.AVAILABLE
               for name in ("tree-sitter-c", "gcovr", "setuptools"))
    assert (items["stdbuf"].readiness, items["stdbuf"].tried_by) == (None, "verification")
    assert report.context.interpreter.path == runtime.selected


@needs_gcc
def test_the_inspection_without_a_compiler_reads_missing_setup(tmp_path):
    """On Linux without a compiler the inspection reads missing setup with the
    toolchain's own reason, and each operation that runs a tool gives it."""
    empty = tmp_path / "empty"
    empty.mkdir()
    report = la.inspect_project("c", _request(_project(tmp_path / "project"), path=str(empty)))
    reason = "no C compiler was found (set c_compiler or CC, or put gcc on PATH)"
    assert (report.outcome, report.reason) == (la.Outcome.MISSING_SETUP, reason)
    assert [(item.key, item.readiness) for item in report.items] == [
        ("runtime", Availability.MISSING_SETUP)]
    run_suite = next(o for o in report.operations if o.name == "tests.run_suite")
    assert (run_suite.readiness, run_suite.reason) == (Availability.MISSING_SETUP, reason)


@needs_gcc
def test_the_inspection_names_a_missing_stdbuf(tmp_path):
    """On Linux the inspection names stdbuf when a C suite needs it and no
    stdbuf is on the command's PATH."""
    from langadapter.c import _toolchain

    gcc = _toolchain.find_compiler()
    empty = tmp_path / "empty"
    empty.mkdir()
    report = la.inspect_project("c", _request(_project(tmp_path / "project"), path=str(empty),
                                              c_compiler=gcc))
    stdbuf = next(item for item in report.items if item.key == "stdbuf")
    assert stdbuf.readiness is Availability.MISSING_SETUP and "coreutils" in stdbuf.action
    run_suite = next(o for o in report.operations if o.name == "tests.run_suite")
    assert (run_suite.readiness, run_suite.reason) == (Availability.MISSING_SETUP, stdbuf.reason)


@needs_gcc
def test_the_tool_check_starts_each_tool_it_found(tmp_path):
    """The tool check starts the compiler, its gcov and stdbuf once, and they
    read available."""
    report = la.verify_project("c", _request(_project(tmp_path)))
    assert report.outcome is la.Outcome.COMPLETED, report.reason
    items = {item.key: item for item in report.items}
    assert items["stdbuf"].readiness is Availability.AVAILABLE
    assert items["stdbuf"].reason == "starts, not yet run on the project"
    assert os.path.isabs(items["stdbuf"].selected) and Path(items["runtime"].selected).is_file()


def test_check_c_reads_no_consumer_ready_off_linux(tmp_path, elsewhere, monkeypatch):
    """Off Linux each installed consumer's line, and the line for a named one,
    reads not on this system instead of what the hooks give."""
    from langadapter import check, consumers
    from tests.test_consumers import TESTUARY, _dist

    site = tmp_path / "site"
    site.mkdir()
    _dist(site, "testuary", "0.2.0", profile=TESTUARY)
    installed = consumers.Installed([site])
    monkeypatch.chdir(tmp_path)
    report = check.report("c", environment=installed)
    assert report.system_issue == elsewhere
    assert "  Testuary 0.2.0: not on this system" in report.lines()
    named = check.report("c", consumer="testuary", environment=installed)
    assert "Ready: not on this system." in named.lines()
