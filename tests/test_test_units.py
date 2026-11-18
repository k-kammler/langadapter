"""The test units hook: removing, counting and naming duplicate tests built
on it, and the check's judgement of hooks by their operations."""
import importlib.util
import json
from pathlib import Path

import langadapter as lb
from langadapter import check, consumers
from langadapter.adapter import LangAdapter
from langadapter.java.adapter import JavaAdapter
from langadapter.python.adapter import PythonAdapter
from langadapter.sources import TestUnit

EXAMPLE = Path(lb.__file__).parent / "tutorial" / "example"
SOURCE = "test adds\ntest Case.adds\ntest Case.adds\ntest subtracts\n"


class Lines(LangAdapter):
    """Each line ``test <name>`` is a test. A line that is not is a parse
    error."""

    language = "lines"

    def test_units(self, source):
        units, start = [], 0
        for line in source.splitlines(keepends=True):
            if not line.startswith("test "):
                return None
            units.append(TestUnit(line[5:].strip(), start, start + len(line)))
            start += len(line)
        return units


def _sources(tmp_path, cls=Lines):
    return cls(lb.ProjectContext(str(tmp_path))).sources


def test_removing_tests_cuts_every_unit_the_names_name(tmp_path):
    sources = _sources(tmp_path)
    assert sources.supports("prune_tests") and sources.supports("test_metrics")
    pruned = sources.prune_tests(SOURCE, ["module.Case.adds", "missing"])
    assert (pruned.status, pruned.source, pruned.removed, pruned.unmatched) == (
        "applied", "test adds\ntest subtracts\n", ("Case.adds",), ("missing",))
    assert sources.prune_tests(SOURCE, ["other.adds"]).source == (
        "test Case.adds\ntest Case.adds\ntest subtracts\n")
    unchanged = sources.prune_tests(SOURCE, ["nothing"])
    assert (unchanged.status, unchanged.source) == ("unchanged", SOURCE)
    broken = sources.prune_tests("not a test\n", ["adds"])
    assert (broken.status, broken.unmatched) == ("parse_error", ("adds",))


def test_the_count_and_the_duplicates_come_from_the_units(tmp_path):
    sources = _sources(tmp_path)
    metrics = sources.test_metrics(SOURCE)
    assert metrics.completed and metrics.value.tests == 4
    assert sources.test_metrics("not a test\n").status.value == "parse_error"
    assert sources.duplicate_test_names(SOURCE).value == ("Case.adds",)
    assert sources.duplicate_test_names("not a test\n").status.value == "parse_error"


def test_an_adapter_without_units_or_with_its_own_operations_keeps_them(tmp_path):
    class Plain(LangAdapter):
        language = "plain"

    assert not _sources(tmp_path, Plain).supports("prune_tests")
    for cls in (PythonAdapter, JavaAdapter):
        assert not callable(getattr(cls, "test_units", None))
        assert _sources(tmp_path, cls)._method("prune_tests").__self__.__class__ is cls


def _toy_step(name):
    spec = importlib.util.spec_from_file_location(f"units_{name}", EXAMPLE / "steps" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ToyAdapter


def test_a_hook_with_operations_is_judged_by_them():
    toy = _toy_step("05_load_repair.py")

    class ByTableNames(toy):
        """The table's hook names written, without the operations they name."""

        prune_collection_breakers = None

        def load(self, source):
            return None

        def syntax_errors(self, source):
            return []

        def skip_patterns(self):
            return ()

    status = {i.name: i.status for i in check.report(ByTableNames).items}
    assert (status["prune_collection_breakers"] == status["salvage_syntax"]
            == status["strip_module_level_skips"] == check.MISSING)
    assert status["check_syntax"] == check.MISSING
    assert status["test_units"] == check.PRESENT
    assert ({i.name: i.status for i in check.report(toy).items}["prune_collection_breakers"]
            == check.PRESENT)


def test_a_family_not_used_inside_a_level_does_not_block_it(tmp_path):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"schema": 1, "consumer": "tool", "title": "Tool", "levels": [
        {"level": 1, "title": "Run", "families": [
            {"family": "run tests", "tier": "required"},
            {"family": "metric providers", "tier": "not used"}]}]}), encoding="utf-8")
    result = check.report(_toy_step("02_test_units.py"), profiles=[profile],
                          environment=consumers.Installed([tmp_path / "site"]))
    assert (result.ready, result.reached) == (True, 1)
    assert [names for _, _, names in result.levels] == [("run tests",)]
