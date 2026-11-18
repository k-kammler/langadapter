"""Timeout evidence: unified-diff application, the journal block parser and
evidence stamping (named killer, hang or cumulative, keeping the status
before attribution)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from langadapter.python import _mutation as TE
from langadapter.python import _cr_session_runner as runner

BASE = "a = 1\nb = 2\nc = 3\n"
DIFF = ("--- a/mod.py\n+++ b/mod.py\n"
        "@@ -1,3 +1,3 @@\n a = 1\n-b = 2\n+b = 99\n c = 3\n")
MUTATED = "a = 1\nb = 99\nc = 3\n"


def test_apply_unified_diff_round_trip():
    assert TE.apply_unified_diff(BASE, DIFF) == MUTATED


def test_apply_unified_diff_mismatch_returns_none():
    assert TE.apply_unified_diff("x = 0\n", DIFF) is None


def test_apply_unified_diff_blank_context_lines_and_banner():
    # Cosmic Ray's diff shape: a banner before the headers, and blank source
    # lines as a single space or an empty diff line.
    base = "def f():\n\n    while a <= b:\n        yield a\n\n    return\n"
    diff = ("--- mutation diff ---\n"
            "--- amod.py\n+++ bmod.py\n"
            "@@ -1,6 +1,6 @@\n"
            " def f():\n"
            " \n"
            "-    while a <= b:\n"
            "+    while a != b:\n"
            "         yield a\n"
            "\n"
            "     return\n")
    out = TE.apply_unified_diff(base, diff)
    assert out == base.replace("a <= b", "a != b")


def _cosmic_ray_diff(original: str, mutated: str) -> str:
    """Cosmic Ray's own diff of the two sources, joined by newlines as its
    work result stores it."""
    mutating = pytest.importorskip("cosmic_ray.mutating")
    return "\n".join(mutating._make_diff(original, mutated, "mod.py"))


@pytest.mark.parametrize("original, mutated", [
    # A mutant at the end of a module ending with a newline: the context
    # names the empty element after it.
    ("def f(n):\n    while n > 0:\n        n = n - 1\n    return n\n",
     "def f(n):\n    while n > 0:\n        n = n + 1\n    return n\n"),
    # A module without a final newline gains none.
    ("a = 1\nb = 2", "a = 1\nb = 3"),
    # A form feed separates lines for str.splitlines, not for Cosmic Ray.
    ("a = 1\n\x0c\nb = 2\nc = 3\n", "a = 1\n\x0c\nb = 5\nc = 3\n"),
], ids=["end-of-module", "no-final-newline", "form-feed"])
def test_a_diff_applies_as_cosmic_ray_split_the_module(original, mutated):
    """The diff rebuilds the mutated module exactly, so its sha matches the
    journal's sha of the file Cosmic Ray wrote."""
    diff = _cosmic_ray_diff(original, mutated)
    assert TE.apply_unified_diff(original, diff) == mutated


@pytest.mark.parametrize("raw", [
    "x = '\u00e9'\n".encode("utf-8-sig"),
    "# -*- coding: latin-1 -*-\nx = '\u00e9'\n".encode("latin-1"),
    b"a = 1\r\nb = 2\r\n",
], ids=["byte-order-mark", "declared-latin-1", "crlf"])
def test_both_snapshots_read_the_module_as_cosmic_ray_does(tmp_path, raw):
    """The host and launcher snapshots hold Cosmic Ray's own reading of
    the module, which its diffs apply to."""
    util = pytest.importorskip("cosmic_ray.util")
    module = tmp_path / "mod.py"
    module.write_bytes(raw)
    expected = util.read_python_source(module)
    assert TE.read_as_cosmic_ray(module) == expected
    assert runner._snapshot_module(tmp_path, "mod.py", tmp_path / "live")
    assert (tmp_path / "live" / "baseline_module.txt").read_text(
        encoding="utf-8") == expected


def test_the_launcher_starts_each_invocation_with_its_own_evidence(tmp_path):
    """A local distributor's clone base outlives an invocation, so the
    journals an earlier one left are removed as no evidence of this one."""
    (tmp_path / "mod.py").write_text("x = 1\n", encoding="utf-8")
    stale = tmp_path / "live" / "local" / "journal.jsonl"
    stale.parent.mkdir(parents=True)
    stale.write_text('{"run": 1, "module_sha": "earlier"}\n',
                     encoding="utf-8")
    assert runner._snapshot_module(tmp_path, "mod.py", tmp_path / "live")
    assert not stale.exists()
    assert (tmp_path / "live" / "baseline_module.txt").is_file()


def test_a_module_the_launcher_cannot_read_goes_without_a_journal(tmp_path):
    """An unknown declared encoding gives no snapshot and no journals,
    not a launcher crash."""
    (tmp_path / "mod.py").write_bytes(b"# -*- coding: no-such-codec -*-\n")
    assert not runner._snapshot_module(tmp_path, "mod.py", tmp_path / "live")
    assert not (tmp_path / "live" / "baseline_module.txt").exists()


def _journal(tmp_path: Path, name: str, events) -> None:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "journal.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def test_parse_journal_blocks_and_hang_detection():
    text = "\n".join(json.dumps(e) for e in [
        {"run": 1, "module_sha": "s1"},
        {"start": "t.py::test_a"}, {"done": "t.py::test_a",
                                    "when": "call", "outcome": "passed"},
        {"start": "t.py::test_b"},                      # never finishes
        {"run": 2, "module_sha": "s2"},
        {"start": "t.py::test_c"},
        {"done": "t.py::test_c", "when": "call", "outcome": "failed"},
        {"end": 2, "exit": 1},
        "NOT JSON",
    ] if not isinstance(e, str)) + "\nTORN LINE"
    blocks = TE.parse_journal(text)
    assert len(blocks) == 2
    assert blocks[0]["hung"] == "t.py::test_b" and not blocks[0]["ended"]
    assert blocks[1]["failed"] == ["t.py::test_c"] and blocks[1]["ended"]
    assert blocks[0]["exit_status"] is None
    assert blocks[1]["exit_status"] == 1


def test_a_journal_that_ends_tests_at_teardown_names_the_test_still_open():
    """Where the session's first line says so, a test ends at its teardown
    report: a test skipped in setup has ended. A carried instrument's
    journal, without that line, ends a test at any report it wrote."""
    def hung(first, *events):
        return TE.parse_journal("\n".join(json.dumps(e) for e in (first, *events)))[0]["hung"]

    assert hung({"run": 1, "module_sha": "s", "ends": "teardown"},
                {"start": "t.py::test_skipped"},
                {"done": "t.py::test_skipped", "when": "teardown", "outcome": "passed"},
                {"start": "t.py::test_open"},
                {"done": "t.py::test_open", "when": "call", "outcome": "failed"}) == "t.py::test_open"
    assert hung({"run": 1, "module_sha": "s"},
                {"start": "t.py::test_skipped"},
                {"start": "t.py::test_open"},
                {"done": "t.py::test_open", "when": "call", "outcome": "passed"}) == "t.py::test_skipped"


def test_a_mutants_timeout_names_the_test_whose_teardown_hung(tmp_path):
    """A real journal of a mutant's run that hangs in a teardown after a test
    skipped in setup: the test whose teardown hung is the killer."""
    import os
    import subprocess
    import sys

    from langadapter.execution import ENV_PREFIX
    from langadapter.python._plugins import JOURNAL_PLUGIN, journal_plugin_text

    project, plugin = tmp_path / "project", tmp_path / "plugin"
    project.mkdir()
    plugin.mkdir()
    (project / "mod.py").write_text(MUTATED, encoding="utf-8")
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (project / "test_hang.py").write_text(
        "import time\n\nimport pytest\n\n\n"
        "@pytest.fixture\ndef hangs_after():\n    yield\n    time.sleep(120)\n\n\n"
        "@pytest.mark.skip(reason='skipped in setup')\n"
        "def test_a_skipped():\n    pass\n\n\n"
        "def test_b_hangs_in_teardown(hangs_after):\n    pass\n", encoding="utf-8")
    (plugin / f"{JOURNAL_PLUGIN}.py").write_text(journal_plugin_text(),
                                                 encoding="utf-8")
    prefix = ENV_PREFIX
    env = dict(os.environ, PYTHONPATH=str(plugin))
    env.update({prefix + "TIMEOUT_EVIDENCE_DIR": str(tmp_path / "evidence" / "r1"),
                prefix + "JOURNAL_MODULE": "mod.py", prefix + "MUTATION_SOURCE_ROOT": str(project)})
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                        "-p", JOURNAL_PLUGIN, "test_hang.py"], cwd=str(project), env=env,
                       capture_output=True, timeout=20)
    m = {"operator": "core/A", "line": 2, "status": "timeout", "diff": DIFF}
    assert TE.attach_timeout_evidence([m], tmp_path / "evidence", BASE) == 1
    assert (m["killer"], m["timeout_evidence"]) == ("test_hang.py::test_b_hangs_in_teardown",
                                                    "hang")


def test_attach_named_killer_upgrades_status(tmp_path):
    _journal(tmp_path, "cr_worker_0", [
        {"run": 5, "module_sha": _sha(MUTATED)},
        {"start": "test_candidate.py::test_x"},
        {"done": "test_candidate.py::test_x", "when": "call",
         "outcome": "failed"},
        {"start": "test_candidate.py::test_y"},        # then it hung too
    ])
    m = {"operator": "core/A", "line": 2, "status": "timeout",
         "diff": DIFF}
    n = TE.attach_timeout_evidence([m], tmp_path, BASE)
    assert n == 1
    assert m["status"] == "killed"
    assert m["pre_attribution_status"] == "timeout"
    assert m["killer"] == "test_candidate.py::test_x"
    assert m["timeout_evidence"] == "named_killer"


def test_attach_hang_stays_timeout_with_named_test(tmp_path):
    _journal(tmp_path, "cr_worker_1", [
        {"run": 6, "module_sha": _sha(MUTATED)},
        {"start": "test_candidate.py::test_ok"},
        {"done": "test_candidate.py::test_ok", "when": "call",
         "outcome": "passed"},
        {"start": "test_candidate.py::test_hang"},
    ])
    m = {"operator": "core/A", "line": 2, "status": "timeout",
         "diff": DIFF}
    assert TE.attach_timeout_evidence([m], tmp_path, BASE) == 1
    assert m["status"] == "timeout"
    assert m["killer"] == "test_candidate.py::test_hang"
    assert m["timeout_evidence"] == "hang"


def _hang(name, sha=None):
    return [{"run": 1, "module_sha": sha or _sha(MUTATED)}, {"start": name}]


def test_an_earlier_executions_journals_survive_the_next_execution(tmp_path):
    """Earlier executions' journals stay beside the next one's, so a
    timeout verified in the first of several batches keeps its hang
    evidence."""
    TE.next_execution(tmp_path, BASE, keep=False)
    _journal(tmp_path, "r100", _hang("t.py::test_first_batch"))
    TE.next_execution(tmp_path, BASE, keep=True)
    _journal(tmp_path, "r100", _hang("t.py::other", sha=_sha("x = 0\n")))
    TE.next_execution(tmp_path, BASE, keep=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "baseline_module.txt", "x0001", "x0002"]
    m = {"operator": "core/A", "line": 2, "status": "timeout", "diff": DIFF}
    assert TE.attach_timeout_evidence([m], tmp_path, BASE) == 1
    assert m["killer"] == "t.py::test_first_batch"
    TE.next_execution(tmp_path, BASE, keep=False)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["baseline_module.txt"]


def test_a_mutants_last_run_gives_its_evidence(tmp_path):
    """A sweep timeout run again in a verification batch pairs with the
    batch's run, which gave its verdict."""
    TE.next_execution(tmp_path, BASE, keep=False)
    _journal(tmp_path, "r7", _hang("t.py::in_the_sweep"))
    TE.next_execution(tmp_path, BASE, keep=True)
    _journal(tmp_path, "r7", _hang("t.py::in_the_batch"))
    m = {"operator": "core/A", "line": 2, "status": "timeout", "diff": DIFF}
    assert TE.attach_timeout_evidence([m], tmp_path, BASE) == 1
    assert m["killer"] == "t.py::in_the_batch"


def test_attach_cumulative_and_ended_blocks_never_pair(tmp_path):
    _journal(tmp_path, "cr_worker_2", [
        # A completed run of the same sha never pairs.
        {"run": 7, "module_sha": _sha(MUTATED)},
        {"start": "test_candidate.py::test_a"},
        {"done": "test_candidate.py::test_a", "when": "call",
         "outcome": "passed"},
        {"end": 7, "exit": 0},
        # Every test finished, the run died on the summed budget.
        {"run": 8, "module_sha": _sha(MUTATED)},
        {"start": "test_candidate.py::test_a"},
        {"done": "test_candidate.py::test_a", "when": "call",
         "outcome": "passed"},
    ])
    m = {"operator": "core/A", "line": 2, "status": "timeout",
         "diff": DIFF}
    assert TE.attach_timeout_evidence([m], tmp_path, BASE) == 1
    assert m["status"] == "timeout"
    assert "killer" not in m
    assert m["timeout_evidence"] == "cumulative"


def test_no_matching_block_leaves_row_untouched(tmp_path):
    _journal(tmp_path, "cr_worker_3", [
        {"run": 9, "module_sha": "somethingelse"},
        {"start": "test_candidate.py::test_a"},
    ])
    m = {"operator": "core/A", "line": 2, "status": "timeout",
         "diff": DIFF}
    assert TE.attach_timeout_evidence([m], tmp_path, BASE) == 0
    assert m["status"] == "timeout"
    assert "timeout_evidence" not in m


def test_match_unended_blocks_consumes_each_exact_source_once(tmp_path):
    _journal(tmp_path, "cr_worker_4", [
        {"run": 10, "module_sha": _sha(MUTATED)},
        {"start": "test_candidate.py::test_x"},
        {"done": "test_candidate.py::test_x", "when": "call",
         "outcome": "failed"},
    ])
    first = {"id": "a", "diff": DIFF}
    duplicate = {"id": "b", "diff": DIFF}
    mismatched = {"id": "c", "diff": DIFF.replace("b = 2", "b = 0")}

    matched = TE.match_unended_blocks(
        [first, duplicate, mismatched], tmp_path, BASE,
    )

    assert len(matched) == 1
    assert matched[0][0] is first
    assert matched[0][1]["failed"] == ["test_candidate.py::test_x"]


def test_a_crash_kill_records_the_test_running_when_the_process_ended(
        tmp_path):
    _journal(tmp_path, "local", [
        {"run": 11, "module_sha": _sha(MUTATED)},
        {"start": "test_x.py::test_ok"},
        {"done": "test_x.py::test_ok", "when": "call", "outcome": "passed"},
        {"start": "test_x.py::test_crash"},       # the process ended here
    ])
    crash = {"id": "c", "operator": "core/A", "line": 2,
             "status": "killed", "diff": DIFF}
    named = {"id": "n", "operator": "core/A", "line": 2,
             "status": "killed", "killer": "t.py::test_x",
             "killers": ["t.py::test_x"], "diff": DIFF}
    logs = []
    assert TE.attach_crash_observations([crash, named], tmp_path, BASE,
                                        log=logs.append) == 1
    assert crash["active_at_end"] == "test_x.py::test_crash"
    assert crash["status"] == "killed" and "killers" not in crash
    assert "active_at_end" not in named
    assert logs == ["[crash-evidence] core/A@2: test_x.py::test_crash was "
                    "running when the process ended"]


def test_a_run_that_ended_normally_or_between_tests_records_nothing(
        tmp_path):
    _journal(tmp_path, "local", [
        {"run": 12, "module_sha": _sha(MUTATED)},
        {"start": "test_x.py::test_ok"},
        {"done": "test_x.py::test_ok", "when": "call", "outcome": "passed"},
        {"end": 12, "exit": 1},
        {"run": 13, "module_sha": _sha(MUTATED)},
        {"start": "test_x.py::test_ok"},
        {"done": "test_x.py::test_ok", "when": "call", "outcome": "passed"},
    ])
    crash = {"id": "c", "status": "killed", "diff": DIFF}
    assert TE.attach_crash_observations([crash], tmp_path, BASE) == 0
    assert "active_at_end" not in crash
