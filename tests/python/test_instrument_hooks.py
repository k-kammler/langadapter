"""Timeout-evidence hooks: the journal plugin (``JOURNAL_PLUGIN``), and the
same hooks in the setup file of a suite delivered with the determinism
instrument. With LANGADAPTER_TIMEOUT_EVIDENCE_DIR set, each pytest run
streams a flushed JSON journal (a header with the mutated module's sha, a
line per test start and report, an end line on normal exit), so a
clock-killed run leaves its unfinished test on disk. Without it no hook or
file exists."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import langadapter as lb
from langadapter.outcomes import InstrumentIdentity
from langadapter.python._plugins import (
    _JOURNAL_HOOKS, JOURNAL_PLUGIN, journal_plugin_text, instrument_identity, render,
)
from langadapter.python.adapter import PythonAdapter
from langadapter.python.project import _setup_test_env

EVIDENCE_TESTS = ("def test_ok():\n    assert True\n\n"
                  "def test_red():\n    assert False\n")


def _stage(tmp_path: Path, tests: str = EVIDENCE_TESTS) -> Path:
    """A suite directory, with the journal plugin in a directory beside it."""
    d = tmp_path / "suite"
    d.mkdir()
    (d / "test_evid.py").write_text(tests, encoding="utf-8")
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / f"{JOURNAL_PLUGIN}.py").write_text(
        journal_plugin_text(), encoding="utf-8")
    return d


def _run(d: Path, env_extra: dict, args=(), *, plugin: bool = True) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.update(env_extra)
    path = [str(d.parent / "plugins"), *filter(None, [env.get("PYTHONPATH")])]
    env["PYTHONPATH"] = os.pathsep.join(path)
    return subprocess.run(
        [sys.executable, "-m", "pytest", str(d), "--rootdir", str(d),
         "-q", "--tb=no", "-p", "no:cacheprovider",
         *(["-p", JOURNAL_PLUGIN] if plugin else []), *args],
        capture_output=True, text=True, env=env, timeout=120)


@pytest.mark.parametrize("line_end", [b"\n", b"\r\n"])
def test_journal_streams_header_starts_reports_and_end(tmp_path, line_end):
    d = _stage(tmp_path)
    target = tmp_path / "mod.py"
    target.write_bytes(b"VALUE = 1" + line_end + b"OTHER = 2" + line_end)
    evid = tmp_path / "evid"
    _run(d, {
        "LANGADAPTER_TIMEOUT_EVIDENCE_DIR": str(evid),
        "LANGADAPTER_MUTATION_TARGET": "mod.py",
        "LANGADAPTER_MUTATION_SOURCE_ROOT": str(tmp_path),
    })
    lines = [json.loads(line) for line in
             (evid / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    header = lines[0]
    # The text's sha, as the capture derives it from its snapshot: the
    # module's line ends do not matter.
    assert header["module_sha"] == hashlib.sha1(
        b"VALUE = 1\nOTHER = 2\n").hexdigest()
    assert header["ends"] == "teardown"
    starts = [line["start"] for line in lines if "start" in line]
    assert any(s.endswith("test_ok") for s in starts)
    assert any(s.endswith("test_red") for s in starts)
    # Each test ends at its teardown report, and a failed report of any
    # phase names a failure.
    ends = [line["done"] for line in lines if line.get("when") == "teardown"]
    failed = [line["done"] for line in lines if line.get("outcome") == "failed"]
    assert [name.rsplit("::", 1)[1] for name in ends] == ["test_ok", "test_red"]
    assert [name.rsplit("::", 1)[1] for name in failed] == ["test_red"]
    assert "end" in lines[-1]                      # normal exit terminates


def test_consecutive_runs_append_blocks(tmp_path):
    d = _stage(tmp_path)
    evid = tmp_path / "evid"
    env = {"LANGADAPTER_TIMEOUT_EVIDENCE_DIR": str(evid)}
    _run(d, env)
    _run(d, env)
    lines = [json.loads(line) for line in
             (evid / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert sum(1 for line in lines if "run" in line) == 2   # two headers
    assert sum(1 for line in lines if "end" in line) == 2   # two ends
    # missing target env -> identity is recorded as unknown, never a crash
    assert all(line["module_sha"] == "unknown" for line in lines if "run" in line)


def test_hooks_survive_in_test_sabotage_of_os_globals(tmp_path):
    # The report hooks run while a test's patches of os globals such as
    # os.fspath are still active. The journal write must neither raise an
    # INTERNALERROR, which aborts the session, nor lose the entry, so it
    # avoids os.path at hook time.
    d = _stage(tmp_path, "import os\n\n"
                         "def test_sabotage(monkeypatch):\n"
                         "    monkeypatch.setattr(os, 'fspath',\n"
                         "                        lambda x: (_ for _ in ()).throw(TypeError))\n"
                         "    assert True\n\n"
                         "def test_after():\n    assert True\n")
    evid = tmp_path / "evid"
    r = _run(d, {"LANGADAPTER_TIMEOUT_EVIDENCE_DIR": str(evid)})
    assert "INTERNALERROR" not in r.stdout
    assert "2 passed" in r.stdout
    lines = [json.loads(line) for line in
             (evid / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    dones = {line["done"]: line["outcome"] for line in lines if "done" in line}
    assert any(k.endswith("test_sabotage") and v == "passed"
               for k, v in dones.items())
    assert "end" in lines[-1]


#: A plugin loaded before the journal: it counts the run's calls of os.fsync.
FSYNC_COUNTER = ("import os\n\n"
                 "_REAL = os.fsync\n\n\n"
                 "def _counted(fd):\n"
                 "    with open(os.environ['FSYNC_COUNT_FILE'], 'a') as fh:\n"
                 "        fh.write('1\\n')\n"
                 "    return _REAL(fd)\n\n\n"
                 "os.fsync = _counted\n")


@pytest.mark.parametrize("carrier", ["carried", "plugin"])
def test_the_journal_flushes_each_event_and_syncs_none_to_disk(tmp_path, carrier,
                                                               carried_instrument):
    """The journal flushes each event, so a killed run leaves every event up
    to the kill, and syncs none to disk. An fsync per event took about 40 ms
    on a virtual disk: a mutant's run of a few hundred tests then exceeded
    the per-mutant limit its journal-free green run had set."""
    d = _stage(tmp_path)
    (d.parent / "plugins" / "fsync_counter.py").write_text(FSYNC_COUNTER, encoding="utf-8")
    if carrier == "carried":
        (d / "conftest.py").write_text(carried_instrument, encoding="utf-8")
    evid, count = tmp_path / "evid", tmp_path / "fsyncs"
    run = _run(d, {"LANGADAPTER_TIMEOUT_EVIDENCE_DIR": str(evid),
                   "FSYNC_COUNT_FILE": str(count)},
               ["-p", "fsync_counter"], plugin=carrier == "plugin")
    assert "1 failed, 1 passed" in run.stdout, run.stdout + run.stderr
    lines = [json.loads(line) for line in
             (evid / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    # The plugin ends each test at its teardown, after test_red's failed
    # call. The carried instrument writes its own, earlier form.
    assert [next(iter(line)) for line in lines] == (
        ["run", "start", "done", "start", "done", "done", "end"] if carrier == "plugin"
        else ["run", "start", "done", "start", "done", "end"])
    assert not count.exists(), f"{count.read_text(encoding='utf-8').count('1')} fsync calls"


def test_no_env_no_file_no_hooks(tmp_path):
    d = _stage(tmp_path)
    r = _run(d, {})
    assert "1 failed, 1 passed" in r.stdout         # both tests ran
    assert not list(tmp_path.rglob("journal.jsonl"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_texts_written_into_a_suite_are_these_bytes(tmp_path, carried_instrument):
    """The instrument a delivered suite carries and its identity, the journal
    hooks, and the conftest.py and pytest.ini of a test directory one level
    below the project are these bytes: recordings can key on them, and run
    records name the identity."""
    assert _sha256(carried_instrument) == (
        "7d734e0476199fb931031f0d702b260dd6b0915ea7cd93d0dfa94bf5be2fe2a5")
    assert instrument_identity(carried_instrument) == InstrumentIdentity(
        "determinism", "e008f7e1cb7cf20b")
    assert _sha256(render(_JOURNAL_HOOKS)) == (
        "5ed4cdbdf069c5efcdd0a11160434b4246e26ef192c62b6425687975f40e7765")
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    _setup_test_env(PythonAdapter(lb.ProjectContext(str(root), lb.Interpreter(path=sys.executable))),
                   str(root / "tests"), str(root))
    assert _sha256((root / "tests" / "conftest.py").read_text(encoding="utf-8")) == (
        "80dcc735891ac9d2984347c7345e7793d96b9653beb606b5a00ebcebcd44550f")
    assert _sha256((root / "tests" / "pytest.ini").read_text(encoding="utf-8")) == (
        "cef23620de0ede73959185ff2502128b60d4534877378bcedf891daa7d80ff5b")
