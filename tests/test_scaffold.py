"""``langadapter new-language``: the one file it writes, neutral and for
consumers, the version flags, the consumer versions a shared package records
with ``--diff`` and ``--update-record``, and its refusals."""
import importlib.util
import json

import pytest

from langadapter import cli, consumers
from langadapter.check import report
from langadapter import cli as writer


def _dist(root, name, version, profile):
    """An installed consumer in ``root`` whose package raises when imported."""
    package = name.replace("-", "_")
    info = root / f"{package}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    (info / "RECORD").write_text(f"{package}/langadapter_profile.json,,\n", encoding="utf-8")
    (root / package).mkdir(exist_ok=True)
    (root / package / "__init__.py").write_text(
        "raise RuntimeError('a consumer was imported')\n", encoding="utf-8")
    (root / package / "langadapter_profile.json").write_text(json.dumps(profile),
                                                            encoding="utf-8")


def _profile(levels):
    return {"schema": 1, "consumer": "tool", "title": "Tool",
            "levels": [{"level": number, "title": title,
                        "families": [dict(zip(("family", "tier", "improves", "fallback"), f))
                                     for f in families]}
                       for number, title, families in levels]}


TOOL_1 = _profile([(1, "Run", [("run tests", "required")]),
                   (2, "Repair", [("load repair", "optional", "keeps suites that fail to load",
                                   "a suite that does not load is discarded")])])
TOOL_2 = _profile([(1, "Run", [("run tests", "required"), ("coverage", "required")]),
                   (2, "Repair", [("load repair", "required")])])


def _environment(root, version="1.0", profile=TOOL_1):
    _dist(root, "tool", version, profile)
    return consumers.Installed([root])


def _adapter_class(project_dir, name):
    path = project_dir / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"scaffolded_{name}_{id(project_dir)}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, f"{name[:1].upper()}{name[1:]}Adapter")


def test_a_neutral_file_names_the_language_with_every_hook_missing(tmp_path):
    project_dir = tmp_path / "pkg"
    written = writer._new_language(writer._Options("lua", "lua", project_dir=project_dir))
    assert written == project_dir / "lua.py" and sorted(project_dir.iterdir()) == [written]
    source = written.read_text(encoding="utf-8")
    assert "\nlangadapter_language = LuaAdapter\n" in source
    assert "`langadapter check lua --extension lua.py`" in source
    assert "TODO: fact runtime" in source and "runtime = None" in source
    assert "TODO (" not in source and "required" not in source
    assert "    # def run_suite(self, request: SuiteRunRequest) -> SuiteRun:" in source
    assert "    # support = LanguageSupport(" in source
    result = report(_adapter_class(project_dir, "lua"))
    status = {item.name: item.status for item in result.items}
    assert status["language"] == status["test_file_name"] == "present"
    assert status["runtime"] == "missing"
    assert status["run_suite"] == status["open_mutation_session"] == "missing"
    assert status["support"] == "default used"


def test_for_a_consumer_the_hooks_carry_its_tiers_and_levels(tmp_path):
    environment = _environment(tmp_path / "site")
    project_dir = tmp_path / "pkg"
    writer._new_language(writer._Options("lua", ".lua", project_dir=project_dir, consumers=("tool",),
                                         runtime="lua", environment=environment))
    source = (project_dir / "lua.py").read_text(encoding="utf-8")
    flat = " ".join(line.strip().lstrip("# ") for line in source.splitlines())
    assert "TODO (Tool level 1): hook run_suite (Run the tests), required by Tool." in flat
    assert ("hook discover_runtime (Find the runtime): works by default (the runtime "
            "fact found on PATH).") in flat
    assert ("hook prune_collection_breakers (Repair loading): optional for Tool, where it "
            "keeps suites that fail to load. Without it, a suite that does not load is "
            "discarded.") in flat
    assert ("hook merge_sources (Merge test files): not needed by Tool. Useful for "
            "combining suites into one.") in flat
    assert 'runtime = "lua"' in source and "TODO: fact runtime" not in source
    assert "check lua --extension lua.py --for tool` reports what is missing for Tool." in flat
    result = report(_adapter_class(project_dir, "lua"), consumer="tool", environment=environment)
    assert result.ready is False and result.reached == 0


def test_every_skeleton_takes_the_call_a_consumer_makes():
    """Each skeleton takes the call a consumer makes (hooks._CALLS), so its
    class is not refused, and both structural metrics methods are there."""
    import textwrap

    from langadapter import hooks

    body = "\n\n".join(textwrap.indent(skeleton, "    ") for skeleton in writer._SKELETONS.values())
    namespace = {}
    exec(compile("from __future__ import annotations\n\n\nclass Skeletons:\n" + body + "\n",
                 "<skeletons>", "exec"), namespace)
    skeletons = namespace["Skeletons"]
    assert hooks._signature_problems(skeletons) == ()
    for method in ("run_as_delivered", "request_options", "prepare_suite",
                   "order_dependent_mock_warnings",
                   "structural_parser_identity", "optimization_metrics"):
        assert method in vars(skeletons) and method in hooks._CALLS, method


def test_the_skeletons_are_files_named_for_the_table_s_records():
    """Each skeleton is a file of the package, ASCII with LF line ends as the
    scaffold writes it, named for a record of the hook table."""
    from importlib import resources

    from langadapter import hooks

    files = [item for item in (resources.files("langadapter") / "skeletons").iterdir()]
    assert sorted(item.name[:-len(".txt")] for item in files) == sorted(writer._SKELETONS)
    assert set(writer._SKELETONS) <= {entry.name for entry in hooks.table()}
    # Every hook shows what to write, also one that works by default.
    assert {entry.name for entry in hooks.table() if entry.kind == "hook"} <= set(writer._SKELETONS)
    for item in files:
        data = item.read_bytes()
        assert data.isascii() and b"\r" not in data and data.endswith(b"\n"), item.name


def test_the_names_a_skeleton_cites_exist_where_it_says():
    """A skeleton's comment is written into every new adapter: each
    langadapter.module.NAME it cites is in that module."""
    import importlib
    import re

    cited = {match for skeleton in writer._SKELETONS.values()
             for match in re.findall(r"langadapter\.([a-z_]+)\.([A-Z_]{3,})", skeleton)}
    assert ("adapter", "RUN_PURPOSES") in cited
    for module, name in cited:
        assert hasattr(importlib.import_module(f"langadapter.{module}"), name), (module, name)


def test_an_adapter_that_writes_every_skeleton_has_each_of_their_hooks():
    """What a skeleton writes is what the check looks for, so an adapter that
    fills in every skeleton, and the test-file fact the scaffold also offers
    (call statistics search the project's non-test files), reads each of
    their hooks present (coverage shows only when a run measures it), and
    the containers fact names every kind of operation."""
    import textwrap

    import langadapter as la

    body = "\n\n".join(textwrap.indent(skeleton, "    ") for skeleton in writer._SKELETONS.values())
    namespace = {"LangAdapter": la.LangAdapter}
    exec(compile("from __future__ import annotations\n\n\nclass Written(LangAdapter):\n"
                 "    language = 'written'\n    file_extensions = ('.wr',)\n"
                 "    def is_test_file(self, path):\n        return False\n" + body + "\n",
                 "<skeletons>", "exec"), namespace)
    written = namespace["Written"]
    status = {item.name: item.status for item in report(written).items}
    assert {name: status[name] for name in writer._SKELETONS if name != "coverage"
            and status[name] != "present"} == {}
    for fact in ("source_execution_kinds", "test_execution_kinds", "mutation_execution_kinds"):
        assert "container" in getattr(written, fact), fact


def test_the_version_flags_declare_the_tested_and_expected_ranges(tmp_path):
    project_dir = tmp_path / "pkg"
    writer._new_language(writer._Options("lua", "lua", project_dir=project_dir, runtime="lua",
                                         runtime_name="Lua", tested_runtime="5.3-5.4",
                                         expected_runtime="5.1-5.2"))
    source = (project_dir / "lua.py").read_text(encoding="utf-8")
    assert ('    support = LanguageSupport(runtime=RuntimeSupport(\n'
            '        "Lua", tested=(VersionSpan((5, 3), (5, 4)),), '
            'expected=(VersionSpan((5, 1), (5, 2)),),\n') in source
    cls = _adapter_class(project_dir, "lua")
    assert cls.support.runtime.tested[0].low == (5, 3)
    assert {i.name: i.status for i in report(cls).items}["support"] == "present"


@pytest.mark.parametrize("options, message", [
    (dict(name="Lua"), "lower-case letters"),
    (dict(file_extension="l u a"), "a file extension is letters and digits"),
    (dict(tested_runtime="newest"), "--tested-runtime takes a version or a range"),
    (dict(expected_runtime="5.1"), "--expected-runtime needs --tested-runtime"),
    (dict(consumers=("absent",)), "No installed package provides the consumer absent"),
])
def test_what_the_scaffold_refuses(tmp_path, options, message):
    given = dict(name="lua", file_extension="lua", project_dir=tmp_path / "pkg",
                 environment=consumers.Installed([tmp_path / "site"]))
    given.update(options)
    with pytest.raises(writer._ScaffoldError, match=message):
        writer._new_language(writer._Options(**given))
    assert not (tmp_path / "pkg").exists()


def test_it_writes_no_file_over_an_existing_one(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "lua.py").write_text("mine", encoding="utf-8")
    with pytest.raises(writer._ScaffoldError, match="exists already"):
        writer._new_language(writer._Options("lua", "lua", project_dir=tmp_path / "pkg"))
    assert (tmp_path / "pkg" / "lua.py").read_text(encoding="utf-8") == "mine"


def test_diff_shows_a_newer_profile_and_update_record_records_it(tmp_path):
    """A shared language package records the consumer versions it was
    written for in its pyproject.toml."""
    project_dir = tmp_path / "pkg"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text(
        '[project]\nname = "langadapter-lua"\n\n[tool.langadapter]\nfor = { tool = "1.0" }\n',
        encoding="utf-8")
    newer = _environment(tmp_path / "new", "2.0", TOOL_2)
    before = (project_dir / "pyproject.toml").read_text(encoding="utf-8")
    lines = writer._diff(project_dir, environment=newer)
    assert lines[0] == "Tool 2.0 (recorded: 1.0) requires:"
    assert "  level 1, required: hook coverage (Measure coverage), write it. " \
           "Guide: add-a-language.md#measure-coverage" in lines
    assert any(line.startswith("  level 2, required: hook prune_collection_breakers (Repair loading)")
               for line in lines)
    assert (project_dir / "pyproject.toml").read_text(encoding="utf-8") == before
    assert writer._update_record(project_dir, environment=newer) == ["Recorded tool 2.0."]
    assert '[tool.langadapter]\nfor = { tool = "2.0" }' in (
        project_dir / "pyproject.toml").read_text(encoding="utf-8")
    assert writer._diff(project_dir, environment=newer) == ["Tool 2.0: as recorded."]
    assert writer._diff(project_dir, environment=consumers.Installed([tmp_path / "none"])) == [
        consumers.not_installed("tool").message]


def test_the_command_writes_the_file_and_names_the_next_step(tmp_path, capsys):
    project_dir = tmp_path / "pkg"
    assert cli.main(["new-language", "lua", "--file-extension", ".lua", "--runtime", "lua",
                     "--output-dir", str(project_dir)]) == 0
    out = capsys.readouterr().out
    assert f"Wrote {project_dir / 'lua.py'}." in out
    assert f"Next: write run_suite, then langadapter check lua --extension {project_dir / 'lua.py'}." in out
    assert cli.main(["new-language", "lua", "--output-dir", str(tmp_path / "x")]) == 2
    assert "--file-extension is required" in capsys.readouterr().err
    assert cli.main(["new-language", "lua", "--file-extension", "lua",
                     "--output-dir", str(project_dir)]) == 2
    assert "exists already" in capsys.readouterr().err
    (project_dir / "pyproject.toml").write_text('[project]\nname = "langadapter-lua"\n',
                                           encoding="utf-8")
    assert cli.main(["new-language", "lua", "--diff", "--output-dir", str(project_dir)]) == 0
    assert "No consumer is recorded" in capsys.readouterr().out


def test_the_file_follows_the_language_given_and_the_next_step_names_where_it_is(
        tmp_path, monkeypatch, capsys, registry_kept):
    """The next step names the file where --output-dir put it, relative to
    the current directory, the samples name the runtime given and no other
    language's, and a hook's line says what it makes possible, as the guide
    and the check do."""
    from pathlib import Path

    monkeypatch.chdir(tmp_path)
    assert cli.main(["new-language", "tally", "--file-extension", ".tly", "--runtime", "python",
                     "--output-dir", "scaffold-try"]) == 0
    assert (f"Next: write run_suite, then langadapter check tally --extension "
            f"{Path('scaffold-try', 'tally.py')}.") in capsys.readouterr().out
    text = (tmp_path / "scaffold-try" / "tally.py").read_text(encoding="utf-8")
    assert "lua" not in text and "(18,)" not in text, [
        line for line in text.splitlines() if "lua" in line or "(18,)" in line]
    assert "probe.which(cls.runtime)" in text and '#     "python", tested=' in text
    assert "It enables" not in text and "It makes possible" in text
    assert cli.main(["check", "tally", "--extension", str(Path("scaffold-try", "tally.py"))]) == 0
    shown = capsys.readouterr().out
    assert "It enables" not in shown and "It makes possible" in shown


def test_the_file_ends_with_the_toolchain_part_commented_out(tmp_path, capsys):
    """After the line that names the adapter, the file shows the name that
    gives the language's toolchain part, commented out like a hook."""
    assert cli.main(["new-language", "lua", "--file-extension", ".lua",
                     "--output-dir", str(tmp_path)]) == 0
    lines = (tmp_path / "lua.py").read_text(encoding="utf-8").splitlines()
    named = lines.index("langadapter_language = LuaAdapter")
    assert lines[named + 1:] == [
        "",
        "# langadapter toolchains reads this name for the part that installs the",
        "# language's tools, by system: Install the toolchains, section A language from",
        "# an extension.",
        '# langadapter_toolchain = {"ubuntu-24.04": "#!/bin/sh\\n..."}']


def test_a_profile_file_named_as_its_path_is_written_marks_its_hooks(tmp_path, monkeypatch,
                                                                     capsys):
    """A profile file named with forward slashes and a leading ./ marks the
    hooks it requires with its levels."""
    (tmp_path / "mytool").mkdir()
    (tmp_path / "mytool" / "langadapter_profile.json").write_text(json.dumps(
        {"schema": 1, "consumer": "mytool", "title": "mytool", "levels": [
            {"level": 1, "title": "Timing",
             "families": [{"family": "timing", "tier": "required"}]}]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["new-language", "lua", "--file-extension", ".lua",
                     "--for", "./mytool/langadapter_profile.json"]) == 0
    text = " ".join(line.strip().lstrip("#").strip()
                    for line in (tmp_path / "lua.py").read_text(encoding="utf-8").splitlines())
    assert "TODO (mytool level 1): hook benchmark (Time a suite), required by mytool." in text
    assert "Run mutation testing" in text and "not needed by ." not in text
    assert "not needed by mytool. Useful for" in text


def test_the_record_commands_refuse_what_they_cannot_compare(tmp_path, capsys):
    """--diff compares installed consumers, so a profile file is refused, and
    --update-record updates the consumers pyproject.toml records, so it takes no
    --for."""
    project_dir = tmp_path / "pkg"
    project_dir.mkdir()
    (project_dir / "pyproject.toml").write_text('[project]\nname = "langadapter-lua"\n',
                                               encoding="utf-8")
    assert cli.main(["new-language", "lua", "--diff", "--for", "mytool.json",
                     "--output-dir", str(project_dir)]) == 2
    refusal = capsys.readouterr().err
    assert "mytool.json" in refusal and "--for names a consumer's module" in refusal
    assert cli.main(["new-language", "lua", "--update-record", "--for", "mytool",
                     "--output-dir", str(project_dir)]) == 2
    assert "pyproject.toml records. Leave out --for." in capsys.readouterr().err


def test_the_module_runs_the_scaffold_as_the_console_script_does(tmp_path):
    """``python -m langadapter.cli new-language`` writes the file: the
    module defines every name the command uses before it runs."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    import langadapter

    environment = dict(os.environ, PYTHONPATH=str(Path(langadapter.__file__).parents[1]))
    done = subprocess.run(
        [sys.executable, "-m", "langadapter.cli", "new-language", "lua", "--file-extension",
         ".lua", "--output-dir", str(tmp_path / "pkg")],
        env=environment, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert (tmp_path / "pkg" / "lua.py").is_file()


def test_the_test_pattern_gives_the_test_file_name(tmp_path):
    project_dir = tmp_path / "pkg"
    writer._new_language(writer._Options("javascript", "js", project_dir=project_dir,
                                         test_pattern="*.test.js"))
    assert _adapter_class(project_dir, "javascript").test_file_name == "{stem}.test.js"


@pytest.mark.parametrize("pattern", ["*", "test_*_*.js", "test_?.js", "*.test.ts"])
def test_a_test_pattern_without_one_stem_and_the_extension_is_refused(tmp_path, pattern):
    with pytest.raises(writer._ScaffoldError, match="--test-pattern takes one"):
        writer._new_language(writer._Options("javascript", "js", project_dir=tmp_path / "pkg",
                                             test_pattern=pattern))
    assert not (tmp_path / "pkg").exists()


def test_a_grammar_names_its_module_and_the_toolkit_s_members(tmp_path):
    """The toolkit imports the grammar's module, and test_units is written
    from its members: the comment names both, not a method the toolkit
    lacks."""
    project_dir = tmp_path / "pkg"
    writer._new_language(writer._Options("lua", "lua", project_dir=project_dir, grammar="tree-sitter-lua"))
    source = (project_dir / "lua.py").read_text(encoding="utf-8")
    assert 'Rules(grammar="tree_sitter_lua", ...)' in source
    assert "TreeSitterUnits(rules).members(source)" in source
    assert "grammar package tree-sitter-lua" in source
    assert "delegate prune_tests" not in source
