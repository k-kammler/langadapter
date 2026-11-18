"""The check of a language adapter: neutral, for a consumer with its three
tiers, with an example's live runs, and its plain lines and command."""
import dataclasses
import importlib.util
import inspect
import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

import langadapter as lb
from langadapter import check, consumers, hooks
from langadapter.conformance import AdapterExample
from langadapter.runs import RunCounts
from tests.test_consumers import TESTUARY, _dist

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


@pytest.fixture
def toy():
    spec = importlib.util.spec_from_file_location("example_toy_check", EXAMPLES / "toy_adapter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.register()
    yield module
    if "toy" in lb.names():
        lb.unregister("toy")


@pytest.fixture
def installed(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    _dist(root, "testuary", "0.2.0", profile=TESTUARY)
    return consumers.Installed([root])


def _item(report, name):
    return next(i for i in report.items if i.name == name)


def test_the_neutral_check_names_each_hook_and_each_consumer(toy, installed):
    class NoRuntime(toy.ToyAdapter):
        runtime = None

    result = check.report(NoRuntime, environment=installed)
    assert _item(result, "language").status == check.PRESENT
    assert _item(result, "runtime").message == (
        "fact runtime (The program that runs the tests): missing. The program a project's "
        "tests run with, such as node.")
    assert _item(check.report(toy.ToyAdapter, environment=installed), "runtime").status == (
        check.PRESENT)
    assert _item(result, "run_suite").status == check.PRESENT
    assert _item(result, "test_units").status == check.MISSING
    # The Toy has no syntax check: a hook with operations has no default.
    assert _item(result, "check_syntax").status == check.MISSING
    assert _item(result, "discover_runtime").status == check.DEFAULT_USED
    assert "default used" in _item(result, "discover_runtime").message
    assert _item(result, "coverage").status == check.NOT_CHECKED
    assert [r.line() for r in result.readiness] == [
        "Testuary 0.2.0: level 0 of 2 complete, 2 required families missing."]
    assert "Installed consumers:" in result.lines()
    assert check.DEFAULT_NOTE in result.lines()


def test_a_check_for_a_consumer_shows_the_three_tiers(toy, installed):
    result = check.report(toy.ToyAdapter, consumer="testuary", environment=installed)
    assert result.consumers == ("Testuary",)
    assert (result.ready, result.reached) == (False, 0)
    assert _item(result, "test_units").message.startswith(
        "hook test_units (Find and remove tests): missing, required by Testuary.")
    assert _item(result, "run_suite").message == (
        "hook run_suite (Run the tests): present, required by Testuary.")
    assert _item(result, "prune_collection_breakers").message == (
        "hook prune_collection_breakers (Repair loading): missing, optional for Testuary, where "
        "it keeps suites that fail to load. Without it, a suite that does not load is discarded.")
    assert _item(result, "merge_sources").message == (
        "Merge test files (merge_sources): not needed by Testuary. "
        "Useful for combining suites into one.")
    assert ";" not in " ".join(i.message for i in result.items if i.tier)
    assert _item(result, "merge_sources").tier == consumers.NOT_USED
    lines = result.lines()
    assert "Testuary level 1: Run, coverage, mutation" in lines and "Ready: no." in lines
    assert [r.line() for r in result.readiness] == [
        "Testuary 0.2.0: level 0 of 2 complete, 2 required families missing."]


def test_readiness_is_not_known_when_only_a_check_with_an_example_can_confirm(installed):
    """With every required family there but coverage, which only an
    example's run shows, readiness is unknown rather than "Ready: no"."""
    import langadapter.tutorial.example as example

    path = Path(example.__file__).parent / "steps" / "06_versions.py"
    spec = importlib.util.spec_from_file_location("toy_step_06_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = check.report(module.ToyAdapter, consumer="testuary", environment=installed)
    assert result.ready is None and result.not_checked == ("coverage",)
    lines = result.lines()
    assert "Ready: no." not in lines
    assert ("Ready: everything required is there. Only a run on an example confirms coverage: "
            "run this check again with --example DIR.") in lines
    neutral = check.report(module.ToyAdapter, environment=installed)
    assert [r.line() for r in neutral.readiness] == [
        "Testuary 0.2.0: everything required is there. Only a run on an example confirms "
        "coverage: run this check again with --example DIR."]


def test_after_a_run_on_an_example_readiness_names_the_hooks_it_left_not_checked(
        toy, tmp_path, installed):
    """The readiness line names each required hook the example's run did not
    check as the hook is written, in a sentence that starts with a word."""
    import langadapter.tutorial.example as tutorial

    path = Path(tutorial.__file__).parent / "steps" / "06_versions.py"
    spec = importlib.util.spec_from_file_location("toy_step_06_examined", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    lb.register("toy", [".toy"], module.ToyAdapter, replace=True)
    (tmp_path / "test_math.toy").write_text("test adds: 1 + 1 == 2\n", encoding="utf-8")
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable))
    example = AdapterExample("toy", context, lb.Suite(str(tmp_path), ("test_math.toy",)))
    result = check.report("toy", example, consumer="testuary", environment=installed)
    assert result.ready is None and result.not_checked == ("coverage",)
    assert ("Ready: everything required is there. The run on this example leaves coverage not "
            "checked, as its line says.") in result.lines()
    readiness = check.Readiness("mutator", "mutator", "", None, 1, 1, 0,
                                ("open_mutation_session", "coverage"))
    assert readiness.line(examined=True) == (
        "mutator: everything required is there. The run on this example leaves "
        "open_mutation_session and coverage not checked, as their lines say.")
    assert readiness.line() == (
        "mutator: everything required is there. Only a run on an example confirms "
        "open_mutation_session and coverage: run this check again with --example DIR.")


def test_a_language_registered_with_a_factory_is_checked_as_the_class_it_makes(toy, installed):
    """A factory function stands for the class it makes, so its hooks read
    as that class's do, not missing."""
    lb.register("toy", [".toy"], lambda context: toy.ToyAdapter(context), replace=True)
    by_factory = check.report("toy", environment=installed)
    by_class = check.report(toy.ToyAdapter, environment=installed)
    assert [(i.name, i.status) for i in by_factory.items] == [(i.name, i.status) for i in by_class.items]
    assert _item(by_factory, "run_suite").status == check.PRESENT


def test_facts_with_a_working_default_read_default_used(toy, installed):
    result = check.report(toy.ToyAdapter, environment=installed)
    assert _item(result, "project_settings").status == check.DEFAULT_USED
    assert _item(result, "ignored_dirs").message == (
        "fact ignored_dirs (Directories that hold no sources): default used: the usual "
        "version control, environment, build, cache and test directories.")


def test_a_mutation_engine_without_a_session_reads_missing(installed):
    """Naming a mutation engine runs no mutant: the hook is present only
    with the session that runs one."""
    class EngineOnly(lb.LangAdapter):
        language = "engineonly"
        file_extensions = (".eo",)
        native_mutation_engines = ("tool",)

    result = check.report(EngineOnly, environment=installed)
    assert _item(result, "open_mutation_session").status == check.MISSING


def test_a_consumer_that_is_not_installed_is_a_problem(toy, installed):
    result = check.report(toy.ToyAdapter, consumer="examplegen", environment=installed)
    assert result.problems and "No installed package provides the consumer examplegen" in result.problems[0]
    assert result.ready is None
    # Without its profile no family is listed as not needed by it.
    assert result.consumers == ()
    assert not any("not needed" in line for line in result.lines())
    assert "Hooks, in the order of the reference page Hooks and facts:" in result.lines()


def test_an_example_runs_the_suite_and_asks_for_coverage(toy, tmp_path, installed):
    (tmp_path / "test_math.toy").write_text("test adds: 1 + 1 == 2\n", encoding="utf-8")
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable))
    example = AdapterExample("toy", context, lb.Suite(str(tmp_path), ("test_math.toy",)),
                             coverage_target="test_math.toy")
    result = check.report("toy", example, consumer="testuary", environment=installed)
    assert _item(result, "run_suite").status == check.PRESENT
    assert _item(result, "coverage").status == check.MISSING
    assert _item(result, "coverage").message.startswith(
        "hook coverage (Measure coverage): missing, required by Testuary.")


def test_a_coverage_tool_that_is_not_set_up_reads_failing_with_its_reason(toy, tmp_path, installed):
    """Coverage that needs setup is a hook that is there and fails, with
    the run's reason, not a missing hook."""
    class Unset(toy.ToyAdapter):
        language = "toy"

        def run_suite(self, request):
            if request.coverage_target is None:
                return super().run_suite(request)
            return self._ended(lb.Outcome.MISSING_SETUP, "the coverage tool is not installed",
                               self._identity(request))

    lb.register("toy", [".toy"], Unset, replace=True)
    (tmp_path / "test_math.toy").write_text("test adds: 1 + 1 == 2\n", encoding="utf-8")
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable))
    example = AdapterExample("toy", context, lb.Suite(str(tmp_path), ("test_math.toy",)),
                             coverage_target="test_math.toy")
    item = _item(check.report("toy", example, environment=installed), "coverage")
    assert item.status == check.FAILING
    assert "needs setup: the coverage tool is not installed" in item.message


def test_a_hook_that_returns_the_wrong_thing_reads_failing(toy, tmp_path, installed):
    class Empty(toy.ToyAdapter):
        language = "toy"

        def run_suite(self, request):
            run = super().run_suite(request)
            return replace(run, tests=(), counts=RunCounts(0, 0, 0, 0, 0))

    lb.register("toy", [".toy"], Empty, replace=True)
    (tmp_path / "test_math.toy").write_text("test adds: 1 + 1 == 2\n", encoding="utf-8")
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable))
    example = AdapterExample("toy", context, lb.Suite(str(tmp_path), ("test_math.toy",)))
    result = check.report("toy", example, consumer="testuary", environment=installed)
    item = _item(result, "run_suite")
    assert item.status == check.FAILING
    assert item.message == ("hook run_suite (Run the tests): failing: it returned completed "
                            "with 0 test outcomes. For the example's suite it should give each "
                            "test's outcome.")


TOY = Path(lb.__file__).parent / "tutorial" / "example"


def _toy_step(index):
    path = sorted((TOY / "steps").glob("0*.py"))[index]
    spec = importlib.util.spec_from_file_location(f"toy_check_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _toy_example(tmp_path, **fields):
    project = tmp_path / "project"
    shutil.copytree(TOY / "project", project)
    context = lb.ProjectContext(str(project), lb.Interpreter(path=sys.executable))
    return AdapterExample("toy", context, lb.Suite(str(project), ("test_calc.toy",)), **fields)


def test_a_test_file_that_does_not_load_must_read_so(tmp_path, installed):
    """The example's collection breaker runs alone in a copy of the
    project and must read collection_failed, not completed."""
    adapter = _toy_step(-1).ToyAdapter
    example = _toy_example(tmp_path, collection_breaker="test adds: add(2, 3) == 5\nnot a test\n")
    before = sorted(path.name for path in Path(example.context.root).iterdir())
    result = check.report(adapter, example, environment=installed)
    assert _item(result, "run_suite").status == check.PRESENT
    assert sorted(path.name for path in Path(example.context.root).iterdir()) == before

    class Loads(adapter):
        def run_suite(self, request):
            run = super().run_suite(request)
            if run.outcome is lb.Outcome.COLLECTION_FAILED:
                return replace(run, outcome=lb.Outcome.COMPLETED)
            return run

    item = _item(check.report(Loads, example, environment=installed), "run_suite")
    assert item.status == check.FAILING
    assert item.message == ("hook run_suite (Run the tests): failing: it returned completed "
                            "for the example's collection_breaker, a test file that does not "
                            "load. It should return collection_failed.")
    taken = _toy_example(tmp_path / "taken", collection_breaker="test adds: 1 + 1 == 2\nno\n")
    (Path(taken.context.root) / "test_candidate.toy").write_text("test a: 1 == 1\n",
                                                                 encoding="utf-8")
    item = _item(check.report(adapter, taken, environment=installed), "run_suite")
    assert item.status == check.FAILING and "would replace test_candidate.toy" in item.message


def test_a_mutant_without_its_change_or_line_reads_failing(tmp_path, installed, monkeypatch):
    """Each mutant must carry its change text and its line, by which a
    consumer shows the survivors."""
    step = _toy_step(-1)
    example = _toy_example(tmp_path, coverage_target="calc.toy")
    result = check.report(step.ToyAdapter, example, environment=installed)
    assert _item(result, "open_mutation_session").status == check.PRESENT
    measured = step.ToySession.measure

    def bare(self, request):
        run = measured(self, request)
        return replace(run, mutants=(replace(run.mutants[0], diff=None), *run.mutants[1:]))

    monkeypatch.setattr(step.ToySession, "measure", bare)
    item = _item(check.report(step.ToyAdapter, example, environment=installed),
                 "open_mutation_session")
    assert item.status == check.FAILING
    assert item.message == ("hook open_mutation_session (Run mutation testing): failing: it "
                            "returned 1 of 2 mutants for calc.toy without their change text or "
                            "line. Each mutant should carry its change (diff) and its line "
                            "(key.line).")


def test_an_example_on_which_every_mutant_survives_gets_a_note(tmp_path, installed):
    """A run where no test of the example fails on any mutant says so in a
    note of its own, since a session that tests the original source instead
    of each mutant gives the same. The hook stays present: the check cannot
    tell such a session from tests too weak to fail."""
    step = _toy_step(-1)
    example = _toy_example(tmp_path, coverage_target="calc.toy")
    assert check.report(step.ToyAdapter, example, environment=installed).notes == ()
    tests = Path(example.context.root) / "test_calc.toy"
    tests.write_text("test plain: 1 + 1 == 2\n", encoding="utf-8")
    note = ("every mutant of calc.toy survived on the example (2 of 2): no test failed on any "
            "mutant, as when the session runs the tests on the original source instead of "
            "each mutant.")
    result = check.report(step.ToyAdapter, example, environment=installed)
    assert _item(result, "open_mutation_session").status == check.PRESENT
    assert result.notes == (note,) and f"  {note}" in result.lines()
    for_consumer = check.report(step.ToyAdapter, example, consumer="testuary",
                                environment=installed)
    assert note in for_consumer.notes


def test_python_s_collection_breaker_reads_does_not_load(tmp_path, installed):
    (tmp_path / "calc.py").write_text("def sign(x):\n    return 1 if x > 0 else -1\n",
                                      encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "from calc import sign\n\n\ndef test_positive():\n    assert sign(2) == 1\n",
        encoding="utf-8")
    example = AdapterExample(
        "python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable)),
        lb.Suite(str(tmp_path), ("test_calc.py",)),
        collection_breaker="from calc import sign\n\nBROKEN = 1 / 0\n\n\n"
                           "def test_positive():\n    assert sign(2) == 1\n")
    result = check.report("python", example, environment=installed)
    assert _item(result, "run_suite").status == check.PRESENT
    assert _item(result, "unsound_test_patterns").status == check.PRESENT
    assert not (tmp_path / "test_candidate.py").exists()


def _probed(capabilities=None, operations=None):
    """A project's capability report: every report entry available and
    every operation supported and ready, but those given."""
    from langadapter.capabilities import (
        CAPABILITIES, OPERATION_NAMES, Availability, Capability, CapabilityReport,
        OperationCapability)
    from langadapter.outcomes import Outcome

    entries = {name: Capability(name, Availability.AVAILABLE) for name in CAPABILITIES}
    entries.update(capabilities or {})
    ops = {name: OperationCapability(name, True, readiness=Availability.AVAILABLE)
           for name in OPERATION_NAMES}
    ops.update(operations or {})
    return CapabilityReport("toy", "toy", Outcome.COMPLETED, "", None,
                            tuple(entries.values()), (), "arcs", (),
                            tuple(ops[name] for name in OPERATION_NAMES))


def test_one_judge_tells_a_family_from_a_project_s_report():
    """A family is there when its report entries are available and one
    alternative of each judged hook is supported and ready, otherwise the
    reason names the first gap."""
    from langadapter.capabilities import Availability, Capability, OperationCapability

    full = _probed()
    assert all(check.family_issue(full, family.name) is None for family in hooks.families())
    assert check.assessed("run tests") and check.assessed("kill attribution")
    assert not check.assessed("facts") and check.family_issue(full, "facts") is None
    assert check.family_hooks("run tests")[0] == (("tests.run_suite",),)
    without = _probed()
    without = dataclasses.replace(without, capabilities=tuple(
        c for c in without.capabilities if c.name != "coverage"))
    assert check.family_issue(without, "coverage") == "the toy adapter does not report coverage"
    off = {"coverage": Capability("coverage", Availability.MISSING_SETUP)}
    assert check.family_issue(_probed(off), "coverage") == "coverage is missing_setup"
    named = {"coverage": Capability("coverage", Availability.UNSUPPORTED, "no coverage tool")}
    assert check.family_issue(_probed(named), "coverage") == "no coverage tool"
    run = "tests.run_suite"
    assert check.family_issue(_probed(operations={run: OperationCapability(run, False)}),
                              "run tests") == "the hook for tests.run_suite is missing"
    assert check.family_issue(_probed(operations={run: OperationCapability(
        run, False, reason="no runner")}), "run tests") == "no runner"
    assert check.family_issue(_probed(operations={run: OperationCapability(
        run, True, readiness=Availability.MISSING_SETUP)}), "run tests") == (
        "tests.run_suite needs setup")
    # An operation the report does not list is missing, and a report
    # without operations judges no hook.
    listed = _probed()
    absent = dataclasses.replace(listed, operations=tuple(
        o for o in listed.operations if o.name != run))
    assert check.family_issue(absent, "run tests") == "the hook for tests.run_suite is missing"
    assert check.family_issue(dataclasses.replace(listed, operations=()), "run tests") is None
    # One alternative without a problem suffices, else the first one's first.
    problems = {"a": "no a", "c": "no c"}
    assert check.hook_issue((("a",), ("b",)), problems.get) is None
    assert check.hook_issue((("a", "b"), ("c",)), problems.get) == "no a"
    assert check.hook_issue((), problems.get) is None
    assert {"assessed", "family_hooks", "family_issue", "hook_issue"} <= set(check.__all__)


def test_the_command_lists_languages_and_checks_one(toy, capsys):
    from langadapter.cli import main

    assert main(["check"]) == 0
    printed = capsys.readouterr().out
    assert "Registered languages:" in printed and "toy" in printed
    assert main(["check", "toy"]) == 0
    assert "Hooks, in the order of the reference page Hooks and facts:" in capsys.readouterr().out
    assert main(["check", "nolanguage"]) == 2


def test_the_check_ends_with_its_counts_and_what_is_required(toy, tmp_path, monkeypatch, capsys):
    """The last line counts the facts and hooks by status. Without --for it
    says that none is required, and for a consumer how many of the missing
    ones the consumer requires."""
    from collections import Counter

    from langadapter.cli import main

    def last_line(*arguments):
        main(["check", "toy", *arguments])
        return capsys.readouterr().out.splitlines()[-1]

    counts = Counter(item.status for item in check.report("toy").items)
    assert counts[check.MISSING] and not counts[check.FAILING]
    assert last_line() == (
        f"Summary: {counts[check.PRESENT]} present, {counts[check.DEFAULT_USED]} default used, "
        f"{counts[check.MISSING]} missing, "
        f"{counts[check.NOT_CHECKED]} not checked.")
    (tmp_path / "mytool.json").write_text(json.dumps(MYTOOL), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    mine = check.report("toy", profiles=["mytool.json"])
    required = sum(1 for item in mine.items
                   if item.status == check.MISSING and item.tier == consumers.REQUIRED)
    assert required == 1, [item.name for item in mine.items if item.tier == consumers.REQUIRED]
    assert last_line("--for", "mytool.json").endswith(
        f"{Counter(item.status for item in mine.items)[check.MISSING]} missing (1 required by "
        f"mytool), {counts[check.NOT_CHECKED]} not checked.")


def test_kill_tokens_left_to_its_default_reads_default_used(toy, capsys):
    """The Toy does not write kill_tokens, whose default gives each killing
    test its mutant's token, so the check reads it as working by default."""
    from langadapter.cli import main

    assert main(["check", "toy"]) == 0
    line = next(line for line in capsys.readouterr().out.splitlines()
                if "hook kill_tokens (" in line)
    assert ("hook kill_tokens (Attribute stored kills): default used: the killing tests each "
            "row records (Mutant.killing_tests).") in line


@pytest.mark.parametrize("command", ["check", "new-language"])
def test_the_commands_name_the_order_they_list_hooks_in(command, capsys):
    """The check and the scaffold list the facts and hooks in the order of
    the reference page Hooks and facts, and their help says so."""
    from langadapter.cli import main

    with pytest.raises(SystemExit):
        main([command, "--help"])
    shown = " ".join(capsys.readouterr().out.split())
    assert "in the order of the reference page Hooks and facts" in shown and "the guide adds" not in shown


def test_the_command_lines_parser_is_public(toy, monkeypatch):
    """Another program reads the commands, their options and help texts from
    langadapter.cli.parser, the parser the command parses with."""
    from langadapter.cli import parser

    commands = next(action.choices for action in parser()._actions
                    if isinstance(getattr(action, "choices", None), dict))
    assert set(commands) == {"check", "new-language", "tutorial", "toolchains"}
    assert parser("tool").prog == "tool"
    options = parser().parse_args(["check", "python", "--example", "here"])
    assert (options.command, options.language, options.example) == ("check", "python", "here")
    from langadapter import cli

    used = []
    monkeypatch.setattr(cli, "parser", lambda prog="langadapter": used.append(prog) or parser(prog))
    assert cli.main(["check", "toy"], prog="mine") == 0
    assert used == ["mine"]


def test_a_profile_file_is_checked_against(toy, tmp_path):
    mine = tmp_path / "mine.json"
    mine.write_text(json.dumps({"schema": 1, "consumer": "mine", "title": "Mine", "levels": [
        {"level": 1, "title": "Run", "families": [{"family": "run tests", "tier": "required"}]}]}),
        encoding="utf-8")
    result = check.report(toy.ToyAdapter, profiles=[mine],
                          environment=consumers.Installed([tmp_path]))
    assert result.consumers == ("Mine",)
    assert any("is used alone: the requirements of the tools it builds on are not added" in n
               for n in result.notes)
    assert _item(result, "test_units").message.startswith("hook test_units (Find and remove tests): missing")


#: The requirement profile of mytool, as Who needs what shows it.
MYTOOL = {"schema": 1, "consumer": "mytool", "title": "mytool", "levels": [
    {"level": 1, "title": "Timing", "families": [{"family": "timing", "tier": "required"}]},
    {"level": 2, "title": "Faster runs", "families": [
        {"family": "per-test evidence", "tier": "optional",
         "improves": "measures only the tests a change affects",
         "fallback": "every run measures the whole suite"}]}]}


@pytest.mark.parametrize("item", ["mytool/langadapter_profile.json",
                                  "./mytool/langadapter_profile.json"])
def test_a_profile_file_is_checked_against_as_its_path_is_written(item, tmp_path, monkeypatch,
                                                                   capsys):
    """A profile file named with forward slashes, as Who needs what writes
    it, gives the consumer's report on every system: the consumer's name,
    its levels and whether the language is ready."""
    from langadapter.cli import main

    (tmp_path / "mytool").mkdir()
    (tmp_path / "mytool" / "langadapter_profile.json").write_text(json.dumps(MYTOOL),
                                                                  encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "python", "--for", item]) == 0
    printed = capsys.readouterr().out
    lines = printed.splitlines()
    assert ["For mytool:", "mytool level 1: Timing"] == [
        line for line in lines if line in ("For mytool:", "mytool level 1: Timing")]
    assert "Ready: yes." in lines
    assert "not needed by ." not in printed


def test_a_fact_reads_as_a_fact_for_a_consumer(toy, tmp_path):
    """support, collection_unit and execution_kinds are facts in the table and
    the neutral view; for a consumer that uses them they read as facts too."""
    from tests.test_consumers import _profile

    _dist(tmp_path, "factuser", "1.0", profile=_profile(
        "factuser", "Factuser",
        (1, "Run", [("run tests", "required"), ("containers", "required")]),
        (2, "More", [("versions", "optional", "names the tested versions", "none are named"),
                     ("load repair", "optional", "keeps suites", "a suite is discarded")])))
    environment = consumers.Installed([tmp_path])
    toy_result = check.report(toy.ToyAdapter, consumer="factuser", environment=environment)
    assert _item(toy_result, "execution_kinds").message.startswith("fact execution_kinds ")
    assert ": missing, required by Factuser" in _item(toy_result, "execution_kinds").message
    assert _item(toy_result, "support").message.startswith("fact support ")
    assert _item(toy_result, "collection_unit").message.startswith("fact collection_unit ")
    python = check.report("python", consumer="factuser", environment=environment)
    assert _item(python, "collection_unit").message.startswith("fact collection_unit ")
    assert _item(python, "collection_unit").status == check.PRESENT


def test_the_command_points_coverage_to_an_example(capsys):
    """Without --example the check runs nothing, so its line for coverage
    points to --example, which runs one."""
    from langadapter.cli import main

    main(["check", "python"])
    printed = capsys.readouterr().out
    coverage = next(line for line in printed.splitlines() if "hook coverage" in line)
    assert "Give the check an example" not in printed
    assert "Only a run on an example shows it: run this check again with --example DIR." in coverage


def test_coverage_is_not_checked_unless_a_run_measured_it(toy, tmp_path, installed):
    (tmp_path / "test_math.toy").write_text("test adds: 1 + 1 == 2\n", encoding="utf-8")
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable))
    without_target = AdapterExample("toy", context, lb.Suite(str(tmp_path), ("test_math.toy",)))
    assert _item(check.report(toy.ToyAdapter), "coverage").status == check.NOT_CHECKED
    assert _item(check.report("toy", without_target), "coverage").status == check.NOT_CHECKED


ONE_FILE = """import langadapter as la


class OneFileAdapter(la.LangAdapter):
    language = "onefile"
    file_extensions = (".one",)
"""


def test_a_first_language_in_one_file_is_checked_by_naming_it(tmp_path, monkeypatch, capsys,
                                                               registry_kept):
    from langadapter.cli import main

    (tmp_path / "onefile.py").write_text(ONE_FILE + "\n\nlangadapter_language = OneFileAdapter\n",
                                         encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "onefile", "--extension", "onefile.py"]) == 0
    printed = capsys.readouterr().out
    assert f"Extension onefile.py: {tmp_path.resolve() / 'onefile.py'}, SHA-256 " in printed
    assert "Language: onefile" in printed


def test_an_extension_that_adds_a_metric_provider_is_named_with_it(tmp_path, monkeypatch,
                                                                    capsys, registry_kept):
    """The line of an extension file names the metric provider it added and
    the metrics it measures, so the check shows that it was loaded."""
    from langadapter.cli import main

    shutil.copy(EXAMPLES / "metric_provider.py", tmp_path / "long_lines.py")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "python", "--extension", "long_lines.py"]) == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first.startswith("Extension long_lines.py: ")
    assert first.endswith(". It adds the metric provider example.long_lines, which measures "
                          "long_lines."), first


def test_a_setting_of_an_unknown_kind_is_a_problem_of_the_check(tmp_path, monkeypatch, capsys,
                                                                registry_kept):
    """The check names a project setting whose kind is none of the four and
    exits 1."""
    from langadapter.cli import main

    (tmp_path / "onefile.py").write_text(
        "from langadapter.adapter import ProjectSetting\n" + ONE_FILE
        + '    project_settings = (ProjectSetting("node", "The node program", "program"),)\n'
          '\n\nlangadapter_language = OneFileAdapter\n', encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "onefile", "--extension", "onefile.py"]) == 1
    lines = capsys.readouterr().out.splitlines()
    assert lines[lines.index("Problems:") + 1] == (
        "  project_settings: the kind 'program' of node is not path, executable, choice or "
        "text")


def test_an_adapter_class_not_named_is_said_with_the_line_it_lacks(tmp_path, monkeypatch, capsys,
                                                                    registry_kept):
    from langadapter.cli import main

    (tmp_path / "onefile.py").write_text(ONE_FILE, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "onefile", "--extension", "onefile.py"]) == 2
    assert ("onefile.py defines OneFileAdapter for onefile but names no langadapter_language: "
            "add the line langadapter_language = OneFileAdapter.") in capsys.readouterr().err
    assert main(["check", "nolanguage"]) == 2
    assert ("Name the file or installed module that adds nolanguage with --extension, such as "
            "langadapter check nolanguage --extension nolanguage.py.") in capsys.readouterr().err


def test_an_example_runs_the_language_s_suite_and_measures_coverage(tmp_path, monkeypatch, capsys,
                                                                     registry_kept):
    """The tutorial's Toy in one file, its runtime the test's own Python,
    checked on the tutorial's project: the run confirms coverage of the one
    source file, its setup file and dependency directories aside."""
    from langadapter.cli import main

    _toy_file(tmp_path, "ToyAdapter.setup_files = lambda self, root, tests: ('helpers.toy',)\n")
    # A setup file beside the source is no target either.
    (tmp_path / "project" / "helpers.toy").write_text("helper(x) = x\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    # Sources in dependency and dot directories are no target.
    for dir_name in ("node_modules", ".venv"):
        (tmp_path / "project" / dir_name).mkdir()
        (tmp_path / "project" / dir_name / "dep.toy").write_text("dep(x) = x\n", encoding="utf-8")
    assert main(["check", "toy", "--extension", "toy.py", "--example", "project"]) == 0
    coverage = next(line for line in capsys.readouterr().out.splitlines()
                    if "hook coverage (" in line)
    assert "not checked" not in coverage and "present" in coverage
    # With a second source the check cannot tell which one the tests cover,
    # and its line says so.
    (tmp_path / "project" / "other.toy").write_text("other(x) = x\n", encoding="utf-8")
    assert main(["check", "toy", "--extension", "toy.py", "--example", "project"]) == 0
    printed = capsys.readouterr().out
    assert ("hook coverage (Measure coverage): not checked. The example holds 2 source files, "
            "calc.toy and other.toy. Coverage is measured on an example with one.") in next(
        line for line in printed.splitlines() if "hook coverage (" in line)
    assert "Only a run on an example shows it" not in printed
    assert "Coverage is checked only when" not in printed
    assert main(["check", "toy", "--example", "absent"]) == 2
    assert "--example names absent, which is not a directory." in capsys.readouterr().err


def _toy_file(tmp_path, extra=""):
    """The tutorial's Toy as toy.py in ``tmp_path``, its runtime the test's
    own Python, with ``extra`` lines before the line that names it, and its
    project beside it."""
    toy = Path(lb.__file__).parent / "tutorial" / "example"
    shutil.copytree(toy / "project", tmp_path / "project")
    source = (toy / "steps" / "05_load_repair.py").read_text(encoding="utf-8")
    (tmp_path / "toy.py").write_text(
        source + "\n\nToyAdapter.runtime = __import__('sys').executable\n" + extra
        + "langadapter_language = ToyAdapter\n", encoding="utf-8")


def test_an_unfinished_capabilities_hook_leaves_the_example_s_runs_to_run(tmp_path, monkeypatch,
                                                                          capsys, registry_kept):
    """A learner's capabilities hook that still returns None, as the
    scaffold's body does, makes no family unavailable: the example's runs
    run as they would without the hook, with no traceback."""
    from langadapter.cli import main

    _toy_file(tmp_path, "ToyAdapter.capabilities = lambda self: None\n")
    monkeypatch.chdir(tmp_path)
    assert main(["check", "toy", "--extension", "toy.py", "--example", "project"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert ("  [run tests] hook run_suite (Run the tests): present. Guide: "
            "add-a-language.md#run-the-tests") in lines
    assert lines[lines.index("Language: toy") + 1].endswith(
        "4 tests: 4 passed. Coverage of calc.toy measured.")


def test_a_check_with_an_example_reads_the_capability_report_once(tmp_path, monkeypatch, capsys,
                                                                  registry_kept):
    """The check reads the capability report once per example, for its
    refusal and for the runs it skips, since for Python each reading starts
    the interpreter. Report keeps it out of its signature, which the
    reference renders."""
    from langadapter.adapter import LangAdapter
    from langadapter.cli import main

    _toy_file(tmp_path)
    monkeypatch.chdir(tmp_path)
    probed = []
    original = LangAdapter.probe_capabilities

    def counted(self):
        probed.append(self.language)
        return original(self)

    monkeypatch.setattr(LangAdapter, "probe_capabilities", counted)
    assert main(["check", "toy", "--extension", "toy.py", "--example", "project"]) == 0
    assert probed == ["toy"]
    capsys.readouterr()
    assert [name for name in inspect.signature(check.Report).parameters
            if name.startswith("_")] == []


def test_a_check_with_an_example_says_what_the_run_on_it_gave(tmp_path, monkeypatch, capsys,
                                                             registry_kept):
    """With --example, the line after the language says how many test files
    and tests the run on the example had, their outcomes and whether it
    measured coverage. Without an example there is no such line."""
    from langadapter.cli import main

    _toy_file(tmp_path)
    monkeypatch.chdir(tmp_path)

    def example_line(*example):
        main(["check", "toy", "--extension", "toy.py", *example])
        lines = capsys.readouterr().out.splitlines()
        following = lines[lines.index("Language: toy") + 1]
        return following if following.startswith("Example:") else None

    assert example_line("--example", "project") == (
        f"Example: 1 test file, run with {shutil.which(sys.executable)}, 4 tests: 4 passed. "
        "Coverage of calc.toy measured.")
    tests = tmp_path / "project" / "test_calc.toy"
    tests.write_text(tests.read_text(encoding="utf-8") + "test wrong: double(2) == 5\n",
                     encoding="utf-8")
    assert example_line("--example", "project").startswith(
        f"Example: 1 test file, run with {shutil.which(sys.executable)}, 5 tests: 4 passed, "
        "1 failed.")
    assert example_line() is None


def test_an_example_without_its_runtime_on_path_is_refused_in_a_sentence(tmp_path, monkeypatch,
                                                                         capsys):
    """--example runs the tests with the runtime the language names, found on
    PATH. Without it on PATH the command says so and starts nothing, so no
    scratch directory is left behind."""
    from langadapter.cli import main

    example = tmp_path / "example"
    example.mkdir()
    (example / "calc.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    (example / "test_calc.py").write_text(
        "from calc import one\n\n\ndef test_one():\n    assert one() == 1\n", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    (tmp_path / "scratch").mkdir()
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    for name in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(name, str(tmp_path / "scratch"))
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.chdir(tmp_path)
    assert main(["check", "python", "--example", "example"]) == 2
    assert ("langadapter check: --example: the example's tests run with python, the language's "
            "runtime, and no python is on PATH. Put the project's python first on PATH, such as "
            "by activating its environment.") in capsys.readouterr().err
    assert not list((tmp_path / "scratch").iterdir())


def test_an_example_without_test_files_is_refused_with_the_test_file_rule(
        tmp_path, monkeypatch, capsys, registry_kept):
    """An example directory without a test file by the language's test file
    rule is refused before anything runs, and the refusal names the rule,
    instead of reading run_suite as failing."""
    from langadapter.cli import main

    _toy_file(tmp_path)
    (tmp_path / "empty").mkdir()
    monkeypatch.chdir(tmp_path)
    assert main(["check", "toy", "--extension", "toy.py", "--example", "empty"]) == 2
    assert ("langadapter check: --example: empty holds no test file of toy, whose test files "
            "are named test_*.toy.") in capsys.readouterr().err


#: A sitecustomize that hides installed distributions from metadata.version,
#: which the Python capability probe reads.
HIDE = '''from importlib import metadata as _metadata

_version = _metadata.version
_hidden = NAMES


def version(name):
    if name.lower().replace("_", "-") in _hidden:
        raise _metadata.PackageNotFoundError(name)
    return _version(name)


_metadata.version = version
'''


def _python_example(tmp_path, monkeypatch, hidden):
    """A passing Python example in tmp_path/example, the tests' own Python
    first on PATH with the distributions ``hidden`` invisible to its
    capability probe, and the temporary directory tmp_path/scratch."""
    import os

    example = tmp_path / "example"
    example.mkdir()
    (example / "calc.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    (example / "test_calc.py").write_text(
        "from calc import one\n\n\ndef test_one():\n    assert one() == 1\n", encoding="utf-8")
    (tmp_path / "hide").mkdir()
    (tmp_path / "hide" / "sitecustomize.py").write_text(HIDE.replace("NAMES", repr(set(hidden))),
                                                        encoding="utf-8")
    inherited = os.environ.get("PYTHONPATH")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "hide")
                       + (os.pathsep + inherited if inherited else ""))
    monkeypatch.setenv("PATH", os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"])
    (tmp_path / "scratch").mkdir()
    for name in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(name, str(tmp_path / "scratch"))
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.chdir(tmp_path)


def test_a_family_the_capability_report_reads_unavailable_is_not_checked_and_not_ready(
        tmp_path, monkeypatch, capsys):
    """With pytest and coverage and no Cosmic Ray for the project's
    interpreter, the check runs no mutation probe: the line names the
    capability report's reason, and a consumer that requires mutation is
    not ready, as its own preflight decides from the same report."""
    from langadapter.cli import main

    _python_example(tmp_path, monkeypatch, ["cosmic-ray"])
    (tmp_path / "mutator.json").write_text(json.dumps({
        "schema": 1, "consumer": "mutator", "title": "mutator", "levels": [
            {"level": 1, "title": "Mutation", "families": [
                {"family": "run tests", "tier": "required"},
                {"family": "mutation", "tier": "required"}]}]}), encoding="utf-8")
    assert main(["check", "python", "--example", "example", "--for", "mutator.json"]) == 1
    lines = capsys.readouterr().out.splitlines()
    line = next(line for line in lines if "hook open_mutation_session (" in line)
    assert ("open_mutation_session (Run mutation testing): not checked, required by mutator. The "
            "capability report reads mutation unavailable: cosmic-ray not installed for the "
            "interpreter ") in line, line
    assert "Ready: no." in lines


def test_an_example_whose_tests_cannot_run_is_refused_and_leaves_nothing(tmp_path, monkeypatch,
                                                                          capsys):
    """Without pytest for the project's interpreter, the check stops before
    anything starts with one sentence from the capability report, and the
    temporary directory stays empty."""
    from langadapter.cli import main

    _python_example(tmp_path, monkeypatch, ["pytest"])
    assert main(["check", "python", "--example", "example"]) == 2
    error = capsys.readouterr().err
    assert "langadapter check: --example: the example's tests cannot run with " in error, error
    assert "pytest not installed for the interpreter " in error, error
    assert not list((tmp_path / "scratch").iterdir())


def test_a_failing_example_names_its_failing_tests_instead_of_blaming_mutation(
        tmp_path, monkeypatch, capsys, registry_kept):
    """A mutation run refuses a suite that fails, so with a failing test in
    the example the mutation line names that test and is not checked,
    instead of reading the hook as failing."""
    from langadapter.cli import main

    _toy_file(tmp_path)
    tests = tmp_path / "project" / "test_calc.toy"
    tests.write_text(tests.read_text(encoding="utf-8") + "test wrong: double(2) == 5\n",
                     encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    main(["check", "toy", "--extension", "toy.py", "--example", "project"])
    line = next(line for line in capsys.readouterr().out.splitlines()
                if "hook open_mutation_session (" in line)
    assert ("not checked. The example's suite has failing tests (test_calc.toy::wrong), and a "
            "mutation run refuses a suite that does not pass. Give an example whose tests "
            "pass.") in line, line


def test_without_test_file_patterns_the_refusal_names_is_test_file(
        tmp_path, monkeypatch, capsys, registry_kept):
    """Where the language's facts cannot be read, the refusal of an example
    without test files names the adapter's is_test_file as the rule."""
    from langadapter.cli import main

    _toy_file(tmp_path, extra="ToyAdapter.language_facts = lambda self: 1 / 0\n")
    (tmp_path / "empty").mkdir()
    monkeypatch.chdir(tmp_path)
    assert main(["check", "toy", "--extension", "toy.py", "--example", "empty"]) == 2
    assert ("langadapter check: --example: empty holds no test file of toy, as its "
            "is_test_file names them.") in capsys.readouterr().err


def test_the_project_setting_docstring_names_every_setting_kind():
    """ProjectSetting's docstring, which the reference renders, names the
    kinds the check accepts and no other, so the two cannot drift apart."""
    from langadapter._words import SETTING_KINDS, listed
    from langadapter.adapter import ProjectSetting

    kinds = listed([f"``{kind}``" for kind in SETTING_KINDS], "or")
    assert f"kind ({kinds})," in " ".join(ProjectSetting.__doc__.split())


def test_unsound_test_patterns_without_language_facts_name_why_they_are_not_checked(
        tmp_path, monkeypatch, capsys, registry_kept):
    """With --example the unsound test patterns are read from the adapter's
    language_facts, so without that hook their line says so instead of
    asking for an example."""
    from langadapter.cli import main

    _toy_file(tmp_path, "del ToyAdapter.language_facts\n")
    monkeypatch.chdir(tmp_path)
    main(["check", "toy", "--extension", "toy.py", "--example", "project"])
    line = next(line for line in capsys.readouterr().out.splitlines()
                if "fact unsound_test_patterns (" in line)
    assert ("not checked. They are read from language_facts, which the adapter does not "
            "write.") in line, line


def test_the_unsound_test_patterns_are_read_from_an_example_s_adapter(toy, tmp_path, installed):
    """The class cannot show a fact of LanguageFacts: without an example it is
    not checked, with one the adapter's facts tell."""
    assert _item(check.report("python", environment=installed),
                 "unsound_test_patterns").status == check.NOT_CHECKED
    (tmp_path / "test_math.toy").write_text("test adds: 1 + 1 == 2\n", encoding="utf-8")
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(path=sys.executable))
    example = AdapterExample("toy", context, lb.Suite(str(tmp_path), ("test_math.toy",)))
    assert _item(check.report("toy", example, environment=installed),
                 "unsound_test_patterns").status == check.MISSING


def test_a_python_example_names_its_one_source_file_for_coverage(tmp_path, monkeypatch):
    """The check's example of a Python project finds its one source file,
    with the test files named below the example. The example runs the
    python on PATH, here the tests' own."""
    import os

    from langadapter.cli import _example

    monkeypatch.setenv("PATH", os.path.dirname(sys.executable) + os.pathsep + os.environ["PATH"])
    (tmp_path / "calc.py").write_text("def one():\n    return 1\n", encoding="utf-8")
    (tmp_path / "test_calc.py").write_text(
        "from calc import one\n\n\ndef test_one():\n    assert one() == 1\n", encoding="utf-8")
    assert _example("python", tmp_path).coverage_target == "calc.py"


def test_a_check_whose_example_fails_exits_1(toy, tmp_path, monkeypatch):
    """A hook that reads failing on the example makes the command fail, also
    without a consumer, and a working example passes. The Toy's runtime is
    the test's own Python, so the command finds it on any PATH."""
    from langadapter.cli import main

    monkeypatch.setattr(toy.ToyAdapter, "runtime", sys.executable)
    failing = tmp_path / "failing"
    failing.mkdir()
    (failing / "test_calc.toy").write_text("not a test\n", encoding="utf-8")
    assert main(["check", "toy", "--example", str(failing)]) == 1
    working = tmp_path / "working"
    shutil.copytree(TOY / "project", working)
    assert main(["check", "toy", "--example", str(working)]) == 0
