"""langadapter tutorial: the Toy's steps play and check, neutrally and for a
consumer, a learner's own version is checked, and the command runs."""
from __future__ import annotations

import io
import json
import sys

import pytest

from langadapter import cli, consumers
from langadapter.tutorial import INSTANT, CheckResult, Step, Tutorial, play
from langadapter.tutorial import language

PROFILE = {"schema": 1, "consumer": "tool", "title": "Tool", "levels": [
    {"level": 1, "title": "Run, coverage, mutation", "families": [
        {"family": "run tests", "tier": "required"},
        {"family": "coverage", "tier": "required"},
        {"family": "mutation", "tier": "required"}]},
    {"level": 2, "title": "Load repair", "families": [
        {"family": "load repair", "tier": "optional",
         "improves": "keeps test files that fail to load",
         "fallback": "a test file that does not load is discarded"}]}]}


def _installed(root):
    """An environment with one consumer, ``tool``, and its profile."""
    info = root / "tool-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: tool\nVersion: 1.0\n",
                                   encoding="utf-8")
    (info / "RECORD").write_text("tool/langadapter_profile.json,,\n", encoding="utf-8")
    (root / "tool").mkdir()
    (root / "tool" / "langadapter_profile.json").write_text(json.dumps(PROFILE), encoding="utf-8")
    return consumers.Installed([root])


def test_every_step_plays_and_its_check_passes(tmp_path):
    out = io.StringIO()
    project_dir = tmp_path / "toy"
    assert play(language.tutorial(), project_dir, mode=INSTANT, out=out) == 0, out.getvalue()
    text = out.getvalue()
    steps = language._step_files()
    assert text.count("Check: done.") == len(steps)
    assert "Step 1 of 6 (run tests): Run the tests" in text
    assert "Step 6 of 6 (versions): Declare versions" in text
    assert "toy.py, the changes from the previous step" in text
    assert (project_dir / "toy.py").read_text(encoding="utf-8") == steps[-1].read_text(
        encoding="utf-8")
    assert (project_dir / "project" / "test_calc.toy").is_file()
    # The texts come from the hook table.
    assert "Run mutation testing (open_mutation_session):" in text
    assert "The check tests it:" in text


def test_for_a_consumer_each_step_says_what_it_needs(tmp_path):
    environment = _installed(tmp_path / "site")
    tutorial = language.tutorial(["tool"], environment)
    labels = [step.label for step in tutorial.steps]
    assert labels[:4] == ["required by Tool"] * 4
    assert labels[4] == "optional for Tool"
    assert labels[5] == "not used by Tool"
    out = io.StringIO()
    assert play(tutorial, tmp_path / "toy", mode=INSTANT, out=out) == 0, out.getvalue()
    text = " ".join(out.getvalue().split())
    assert "Required by Tool, in Tool level 1." in text
    assert "required by Tool, via" not in text
    assert ("Optional for Tool, in Tool level 2, where it keeps test files that fail to "
            "load. Without it, a test file that does not load is discarded.") in text
    assert "Not used by Tool." in text
    assert "Tool 1.0: level 0 of 2 complete, 3 required families missing." in text
    assert "Tool 1.0: level 1 of 2 complete, ready." in text
    assert "Tool 1.0: ready, all 2 levels complete." in text


RUN = {"title": "Run Tool on the Toy",
       "text": ["Tool plays a recorded run of the Toy.", "Its records go to tool-run."],
       "command": ["toolrun", "--demo", "toy", "--output-dir", "tool-run"],
       "install": "Tool is a separate package: python -m pip install tool"}


def _installed_with_run(root, run=RUN):
    """An environment whose one consumer, ``tool``, gives a closing run."""
    environment = _installed(root)
    (root / "tool" / "langadapter_profile.json").write_text(
        json.dumps({**PROFILE, "tutorial": run}), encoding="utf-8")
    return environment


def test_a_consumers_closing_run_ends_the_tutorial_for_it(tmp_path):
    tutorial = language.tutorial(["tool"], _installed_with_run(tmp_path / "site"))
    last = tutorial.steps[-1]
    assert last.title == "Run Tool on the Toy"
    assert last.text == ("Tool plays a recorded run of the Toy.", "Its records go to tool-run.")
    assert last.optional and not last.files and last.check is None
    assert last.command == ("toolrun", "--demo", "toy", "--output-dir", "tool-run")
    assert last.install_hint == RUN["install"]
    # Without the block, or without a consumer, no step runs a command.
    without = language.tutorial(["tool"], _installed(tmp_path / "other"))
    assert all(step.command == () for step in without.steps)
    assert all(step.command == () for step in language.tutorial().steps)


def test_a_closing_run_whose_command_is_missing_says_how_to_get_it(tmp_path, monkeypatch):
    from langadapter import tutorial as engine

    monkeypatch.setattr(engine, "_find_program", lambda name: None)
    tutorial = language.tutorial(["tool"], _installed_with_run(tmp_path / "site"))
    out = io.StringIO()
    assert play(tutorial, tmp_path / "toy", mode=INSTANT, out=out) == 0, out.getvalue()
    text = out.getvalue()
    assert "Step 7 of 7 (optional): Run Tool on the Toy" in text
    assert "toolrun is not installed here, so this step does not run it." in text
    assert "python -m pip install tool" in text
    assert "The command: toolrun --demo toy --output-dir tool-run" in text


def test_a_closing_run_runs_the_consumers_command_in_the_dir(tmp_path):
    """The command runs as a separate process in the tutorial's directory, where
    the prepared adapter and project are."""
    script = tmp_path / "bin" / "record.py"
    script.parent.mkdir()
    script.write_text("import os, pathlib\n"
                      "pathlib.Path('ran.txt').write_text(str(sorted(os.listdir('.'))))\n",
                      encoding="utf-8")
    run = {**RUN, "command": [sys.executable, str(script)]}
    tutorial = language.tutorial(["tool"], _installed_with_run(tmp_path / "site", run))
    out = io.StringIO()
    project_dir = tmp_path / "toy"
    assert play(tutorial, project_dir, mode=INSTANT, out=out) == 0, out.getvalue()
    assert "'toy.py'" in (project_dir / "ran.txt").read_text(encoding="utf-8")


def _installed_chain(root):
    """``app`` requires ``tool``. app names merging as not used and timing as
    optional, which tool requires."""
    def dist(name, profile, requires=()):
        info = root / f"{name}-1.0.dist-info"
        info.mkdir(parents=True)
        lines = ["Metadata-Version: 2.1", f"Name: {name}", "Version: 1.0"]
        lines += [f"Requires-Dist: {requirement}" for requirement in requires]
        (info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (info / "RECORD").write_text(f"{name}/langadapter_profile.json,,\n", encoding="utf-8")
        (root / name).mkdir()
        (root / name / "langadapter_profile.json").write_text(json.dumps(profile),
                                                             encoding="utf-8")

    dist("tool", {"schema": 1, "consumer": "tool", "title": "Tool", "levels": [
        {"level": 1, "title": "Run", "families": [
            {"family": "run tests", "tier": "required"},
            {"family": "timing", "tier": "required"}]}]})
    dist("app", {"schema": 1, "consumer": "app", "title": "App", "levels": [
        {"level": 1, "title": "Extras", "families": [
            {"family": "merging", "tier": "not used"},
            {"family": "timing", "tier": "optional", "improves": "faster reports",
             "fallback": "slower reports"}]}]}, requires=("tool",))
    return consumers.Installed([root])


def test_each_tier_reads_as_itself_with_the_sources_of_the_winning_tier(tmp_path):
    environment = _installed_chain(tmp_path / "site")
    composed = consumers.composed(["app"], environment=environment)

    class Record:
        def __init__(self, family):
            self.family = family

    assert language._need_label(Record("merging"), composed, "App") == "not used by App"
    assert language._need_text(Record("merging"), composed, "App") == "Not used by App."
    # tool requires timing and app makes it optional: required wins, and only
    # its source is named.
    assert language._need_label(Record("timing"), composed, "App") == (
        "required by Tool, via App")
    assert language._need_label(Record("load repair"), composed, "App") == "not used by App"


def test_a_learners_broken_adapter_is_checked_and_reported(tmp_path):
    def learner_breaks_it(project_dir):
        # Stands for the learner leaving toy.py half written.
        (project_dir / "toy.py").write_text("raise RuntimeError('half written')\n",
                                           encoding="utf-8")
        return CheckResult(True)

    tutorial = language.tutorial()
    steps = (tutorial.steps[0], Step("Edit it yourself", check=learner_breaks_it),
             tutorial.steps[1])
    answers = iter(["", "", "k"])
    out = io.StringIO()
    assert play(Tutorial(tutorial.title, steps), tmp_path / "toy", out=out,
                ask=lambda prompt: next(answers)) == 1
    text = out.getvalue()
    assert "Kept your version of toy.py." in text
    assert "the check could not run: RuntimeError: half written" in text


def test_the_command_refuses_a_consumer_that_is_not_installed(capsys):
    assert cli.main(["tutorial", "--for", "no-such-consumer-here"]) == 2
    assert "No installed package provides the consumer no-such-consumer-here" in (
        capsys.readouterr().err)


def test_the_command_plays_the_tutorial(tmp_path, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    project_dir = tmp_path / "toy"
    assert cli.main(["tutorial", "--auto", "--output-dir", str(project_dir)]) == 0
    assert (project_dir / "toy.py").is_file()


def test_the_check_command_does_not_load_the_tutorial(monkeypatch, capsys):
    monkeypatch.delitem(sys.modules, "langadapter.tutorial.language", raising=False)
    assert cli.main(["check"]) == 0
    assert "langadapter.tutorial.language" not in sys.modules
    capsys.readouterr()


@pytest.mark.parametrize("argv", [["tutorial", "--speed", "0", "--auto"]])
def test_a_speed_that_is_not_positive_is_refused(argv, capsys):
    assert cli.main(argv) == 2
    assert "--speed must be positive" in capsys.readouterr().err


def test_each_step_s_file_loads_as_an_extension(tmp_path, registry_kept):
    """Each step's toy.py names its class as langadapter_language, so
    langadapter check toy --extension toy.py reads the learner's file."""
    import langadapter as la

    for step in language._step_files():
        target = tmp_path / step.stem / "toy.py"
        target.parent.mkdir()
        target.write_text(step.read_text(encoding="utf-8"), encoding="utf-8")
        loaded = la.load_extension(str(target))
        assert loaded.languages == ("toy",), step.name
        la.unregister("toy")


def test_the_first_step_names_the_facts_without_a_default_that_the_toy_leaves_out():
    """Step 1 says which facts without a default the Toy sets, and which it
    leaves out, since the check then reads those missing."""
    text = " ".join(language.tutorial().steps[0].text)
    assert "language, file_extensions and runtime have no default and are set here." in text
    assert ("test_file_patterns and source_merge_separator have no default either. The Toy "
            "leaves them out, and the check reads them missing.") in text
