"""The shared repair loops with stand-in steps: every way a syntax repair or
a load repair ends, and the order of the checks in the last round, which the
languages' messages depend on."""
from types import SimpleNamespace

from langadapter import templates


def _verdict(valid, status="syntax_error", line=None, message=""):
    return SimpleNamespace(valid=valid, status=status, line=line, message=message)


def test_a_syntax_repair_fixes_until_the_source_is_valid():
    def validate(text):
        bad = [n for n, line in enumerate(text.splitlines(), 1) if "bad" in line]
        return _verdict(not bad, line=bad[0] if bad else None)

    def blank(lines, verdict):
        lines[verdict.line - 1] = "\n"
        return True

    result = templates.repair_syntax("ok\nbad\nok\nbad\n", validate, blank)
    assert (result.status, result.source, result.repairs) == ("repaired", "ok\n\nok\n\n", 2)
    assert templates.repair_syntax("ok\n", validate, blank).status == "valid"


def test_a_syntax_repair_ends_without_text_otherwise():
    never = templates.repair_syntax("x\n", lambda t: _verdict(False, line=1),
                                    lambda lines, v: True, rounds=3)
    assert (never.status, never.source, never.repairs) == ("exhausted", None, 2)
    stuck = templates.repair_syntax("x\n", lambda t: _verdict(False, line=1),
                                    lambda lines, v: False)
    assert (stuck.status, stuck.repairs) == ("beyond_repair", 0)
    broken = templates.repair_syntax("x\n", lambda t: _verdict(False, status="validator_failed"),
                                     lambda lines, v: True)
    assert broken.status == "validator_failed"


def _loader(failing):
    """Loading fails at the first line that holds a name in ``failing``."""
    def load(text):
        lines = text.splitlines()
        for number, line in enumerate(lines, 1):
            if line in failing:
                return templates.Load(False, (number,), f"fails at {number}")
        return templates.Load(True)
    return load


def _remover(text, lines):
    rows = text.splitlines(keepends=True)
    if rows[lines[0] - 1].startswith("header"):
        return None
    label = rows[lines[0] - 1].strip()
    del rows[lines[0] - 1]
    return "".join(rows), [label]


def test_a_load_repair_adopts_the_source_once_it_loads():
    result = templates.repair_loading("a\nb\nc\nd\n", _loader({"b", "d"}), _remover, loads=5)
    assert (result.status, result.source, result.removed, result.loads) == (
        "loads", "a\nc\n", ("b", "d"), 3)
    assert result.repaired
    unchanged = templates.repair_loading("a\n", _loader(set()), _remover, loads=5)
    assert (unchanged.status, unchanged.source, unchanged.removed) == ("loads", "a\n", ())


def test_the_last_load_says_whether_its_line_had_a_unit():
    result = templates.repair_loading("b\nc\nheader\n", _loader({"b", "c", "header"}), _remover,
                                      loads=3)
    assert (result.status, result.unmappable, result.source) == ("exhausted", True,
                                                                  "b\nc\nheader\n")
    mapped = templates.repair_loading("b\nc\nd\n", _loader({"b", "c", "d"}), _remover, loads=3)
    assert (mapped.status, mapped.unmappable, mapped.failure) == ("exhausted", False, "fails at 1")


def test_a_load_repair_stops_on_a_line_without_unit_or_location():
    unmapped = templates.repair_loading("header\n", _loader({"header"}), _remover, loads=5)
    assert (unmapped.status, unmapped.source) == ("not_removable", "header\n")
    nowhere = templates.repair_loading(
        "a\n", lambda text: templates.Load(False, (), "no line"), _remover, loads=5)
    assert (nowhere.status, nowhere.failure) == ("no_location", "no line")


def test_a_load_repair_stops_when_no_test_is_left_or_the_loader_cannot_run():
    lonely = templates.repair_loading("test\nb\n", _loader({"test"}), _remover, loads=5,
                                      keeps_a_test=lambda text: "test" in text)
    assert (lonely.status, lonely.removed, lonely.source) == ("no_test_left", ("test",),
                                                               "test\nb\n")
    stopped = templates.repair_loading(
        "a\n", lambda text: templates.Load(False, stop="timed_out", info={"after": 3}),
        _remover, loads=5)
    assert (stopped.status, stopped.stop, stopped.info) == ("stopped", "timed_out", {"after": 3})


def test_a_test_name_names_the_longest_unit_it_ends_with():
    units = ("test_x", "Case.test_x", "Outer.Case.test_x", "Case.test_y")
    assert templates.named_unit("pkg.module.Case.test_x", units) == "Case.test_x"
    assert templates.named_unit("Outer.Case.test_x", units) == "Outer.Case.test_x"
    assert templates.named_unit("test_x", units) == "test_x"
    assert templates.named_unit("module.Other.test_x", units) == "test_x"
    assert templates.named_unit("Xtest_x", units) is None
    assert templates.named_unit("Case.test_z", units) is None


def _failing_letters(*stops):
    """A look that finds the capital letters of a text as failing units,
    and stops at the looks numbered in ``stops``."""
    looks = {"count": 0}

    def find(text):
        looks["count"] += 1
        if looks["count"] in stops:
            return templates.Failing(stop="no look", info={"at": looks["count"]})
        return templates.Failing([c for c in text if c.isupper()][:1], info={"looks": looks["count"]})
    return find


def _drop(text, units):
    return "".join(c for c in text if c not in units), list(units)


def test_a_unit_pruning_keeps_its_removals_within_its_rounds():
    pruned = templates.prune_units("aBcDe", _failing_letters(), _drop, rounds=5)
    assert (pruned.status, pruned.source, pruned.removed, pruned.rounds) == (
        "pruned", "ace", ("B", "D"), 3)
    bounded = templates.prune_units("aBcDeF", _failing_letters(), _drop, rounds=2)
    assert (bounded.status, bounded.source, bounded.removed, bounded.rounds) == (
        "pruned", "aceF", ("B", "D"), 2)


def test_a_unit_pruning_without_failures_or_without_a_look():
    clean = templates.prune_units("abc", _failing_letters(), _drop, rounds=5)
    assert (clean.status, clean.source, clean.rounds, clean.info) == (
        "unchanged", "abc", 1, {"looks": 1})
    stopped = templates.prune_units("aBcDe", _failing_letters(2), _drop, rounds=5)
    assert (stopped.status, stopped.source, stopped.removed, stopped.stop, stopped.info) == (
        "stopped", "aBcDe", (), "no look", {"at": 2})
