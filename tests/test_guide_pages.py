"""The guide's table regions and the hook reference are rendered from the
hook table (tools/guide_pages.py): every record has a place in the guide,
every region a record, each region's heading the table's title, and the
pages match the table."""
from __future__ import annotations

import importlib.util
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "guide_pages.py"
_spec = importlib.util.spec_from_file_location("guide_pages", _TOOL)
guide_pages = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guide_pages)


@dataclass(frozen=True)
class Record:
    name: str
    kind: str
    family: str = ""
    order: int = 0
    title: str = ""
    what: str = ""
    enables: str = ""
    useful_for: str = ""
    default: str = ""
    versions: str = ""
    test: str = ""
    operations: tuple = ()
    guide: str = ""
    step: str = ""
    methods: tuple = ()


RECORDS = (
    Record("facts", "family", order=0, title="Language facts",
           enables="naming the language's files", guide="add-a-language.md#language-facts"),
    Record("language", "fact", family="facts", title="The name", what="The language's name.",
           default="none: set it", guide="add-a-language.md#language-facts"),
    Record("code_fence", "fact", family="facts", title="The fence", what="A | fence.",
           default="the name", guide="add-a-language.md#language-facts"),
    Record("coverage", "family", order=10, title="Measure coverage",
           enables="Measuring line and branch coverage.", useful_for="Any test tool.",
           guide="add-a-language.md#measure-coverage"),
    Record("coverage_target", "hook", family="coverage", order=11,
           title="Measure coverage", what="Coverage of one file in a suite run.",
           enables="Coverage goals.", test="Run a suite with a coverage target.",
           operations=("tests.run_suite",), guide="add-a-language.md#measure-coverage"),
    Record("check_syntax", "hook", family="coverage", order=12, title="Check the syntax",
           what="Whether a source parses.", default="The grammar's parse.",
           versions="A grammar per runtime version.", step="02_test_units.py",
           guide="add-a-language.md#check-syntax"),
)

GUIDE = """# Guide

## Language facts {#language-facts}

<!-- table:language-facts -->
<!-- /table:language-facts -->

## Measure coverage {#measure-coverage}

<!-- table:measure-coverage -->
old text
<!-- /table:measure-coverage -->

### Check the syntax {#check-syntax}

<!-- table:check-syntax -->
<!-- /table:check-syntax -->
"""


def test_each_region_is_filled_from_its_records():
    text = guide_pages.rewrite_guide(GUIDE, RECORDS)
    assert "old text" not in text
    assert "**It makes possible:** Measuring line and branch coverage." in text
    assert "**Useful for:** Any test tool." in text
    assert "`coverage_target` (hook)" in text
    assert "**Test it:** Run a suite with a coverage target." in text
    assert "**Default:** The grammar's parse." in text
    assert "**Versions:** A grammar per runtime version." in text
    assert ("**In the Toy:** step 2 of `langadapter tutorial`, after which `toy.py` in the "
            "tutorial's directory holds it.") in text
    # A region keeps its markers, and rendering again changes nothing.
    assert text.count("<!-- table:measure-coverage -->") == 1
    assert guide_pages.rewrite_guide(text, RECORDS) == text


def test_several_facts_form_one_table():
    text = guide_pages.rewrite_guide(GUIDE, RECORDS)
    assert "| Fact | What | Default |" in text
    assert "| `language` | The language's name. | none: set it |" in text
    assert "| `code_fence` | A \\| fence. | the name |" in text


def test_a_record_without_a_region_is_refused():
    extra = RECORDS + (Record("timing", "family", guide="add-a-language.md#timing"),)
    with pytest.raises(ValueError, match="no region for: timing"):
        guide_pages.rewrite_guide(GUIDE, extra)


def test_a_region_without_a_record_is_refused():
    with pytest.raises(ValueError, match="without a record in the hook table: check-syntax"):
        guide_pages.rewrite_guide(GUIDE, RECORDS[:5])


def test_a_heading_must_carry_the_tables_title():
    renamed = GUIDE.replace("### Check the syntax {#check-syntax}", "### Check syntax {#check-syntax}")
    with pytest.raises(ValueError, match="must read 'Check the syntax {#check-syntax}'"):
        guide_pages.rewrite_guide(renamed, RECORDS)


def test_a_record_for_another_page_has_no_region_here():
    other = RECORDS + (Record("hints", "fact", guide="hints.md#hints"),)
    assert guide_pages.rewrite_guide(GUIDE, other) == guide_pages.rewrite_guide(GUIDE, RECORDS)


def test_the_reference_lists_every_hook_under_its_family():
    text = guide_pages.render_reference(RECORDS)
    assert text.index("## Language facts") < text.index("## Measure coverage")
    assert text.index("## Measure coverage") < text.index("### `coverage_target`") < text.index(
        "### `check_syntax`")
    assert "- Operations built on it: tests.run_suite" in text
    assert "- Guide: [Measure coverage](../extending/add-a-language.md#measure-coverage)" in text
    assert text.endswith("\n") and not text.endswith("\n\n")


def test_a_hooks_write_block_shows_what_the_scaffold_writes():
    syntax =guide_pages.render_region([next(r for r in RECORDS if r.name == "check_syntax")])
    assert "**Write:**" in syntax
    assert "def validate_source(self, source: str) -> SourceValidation:" in syntax
    # The skeleton's docstring stays out, and its ... body stays, so the block
    # compiles as a stub.
    assert '"""' not in syntax and "parser reads" not in syntax
    assert "-> SourceValidation:\n    ...\n" in syntax
    coverage = guide_pages.render_region([Record("coverage", "hook", what="Coverage.")])
    assert "# Coverage is part of run_suite" in coverage
    plain = guide_pages.render_region([Record("no_skeleton", "hook", what="No skeleton.")])
    assert "Write" not in plain
    # In the reference the block follows the entry's whole list.
    reference = guide_pages.render_reference(RECORDS)
    entry = reference[reference.index("### `check_syntax`"):]
    assert entry.index("- Guide:") < entry.index("Write:")


def test_each_reference_entry_names_what_its_skeleton_defines():
    """A hook's entry lists the methods and attributes its Write block
    defines, so a writer sees the names without reading the code."""
    reference = guide_pages.render_reference(guide_pages.table_records())

    def entry(name):
        return reference[reference.index(f"### `{name}`"):].split("\n### ", 1)[0]

    assert "- Methods to write: `validate_source`" in entry("check_syntax")
    assert ("- Methods to write: `structural_parser_identity` and `optimization_metrics`"
            in entry("structural_metrics"))
    assert "- Attributes to write: `mutation_evidence_granularity`" in entry("kill_attribution")
    assert ("- Methods to write: `mutation_operators` and `open_mutation_session`"
            in entry("open_mutation_session"))
    assert "- Attributes to write: `native_mutation_engines`" in entry("open_mutation_session")
    # Coverage is part of run_suite: its Write block is a comment only.
    assert "to write:" not in entry("coverage")
    core = guide_pages.CORE_PAGE + "#langadapter.LangAdapter."
    assert ("- The check counts these names as the hook's: `run_suite`, "
            f"[`collection_text`]({core}collection_text), "
            f"[`listed_failures`]({core}listed_failures) and `collect_suite`") in entry("run_suite")
    assert "`own_mutant_timeout_rule`" in entry("open_mutation_session")
    assert "- The check counts these names as the fact's: `is_test_file`" in entry(
        "test_file_patterns")


def test_a_block_names_the_hook_tables_names_beyond_its_write_block():
    """A writer reading a block sees every method and attribute the hook
    table lists for it, also those its Write block does not define."""
    hook = Record("run_suite", "hook", what="Runs the suite.",
                  methods=("run_suite", "collection_text", "own_mutant_timeout_rule"))
    text = guide_pages.render_region([hook])
    core = guide_pages.CORE_PAGE + "#langadapter.LangAdapter."
    # A name the base class defines links to its reference entry.
    assert ("**The check counts these names as the hook's:** `run_suite`, "
            f"[`collection_text`]({core}collection_text) and "
            f"[`own_mutant_timeout_rule`]({core}own_mutant_timeout_rule)") in text
    assert text.index("**Write:**") < text.index("**The check counts these names")
    own = guide_pages.render_region([Record("probe", "hook", what="Probes.", methods=("probe",))])
    assert "The check counts" not in own
    facts = guide_pages.render_region([
        Record("language", "fact", what="The name.", methods=("language",)),
        Record("test_file_patterns", "fact", what="The patterns.", methods=("is_test_file",)),
        Record("file_rule", "fact", what="A rule.", methods=("file_rule", "rename_file_unit"))])
    assert ("**The check counts these names as the facts':**\n\n"
            "- `test_file_patterns`: `is_test_file`\n"
            f"- `file_rule`: [`file_rule`]({core}file_rule) and "
            f"[`rename_file_unit`]({core}rename_file_unit)\n") in facts
    # The reference says what the line means.
    opening = guide_pages.render_reference(guide_pages.table_records()).split("\n## ", 1)[0]
    assert ("you need not write: for a fact or a hook without operations, the check reads it "
            "present") in " ".join(opening.split())


def test_each_guide_block_names_every_name_the_hook_table_lists():
    guide = guide_pages.GUIDE_PAGE.read_text(encoding="utf-8")
    absent = []
    for record in guide_pages.table_records():
        page, _, anchor = (getattr(record, "guide", "") or "").partition("#")
        if page != "add-a-language.md":
            continue
        block = re.search(rf"<!-- table:{anchor} -->(.*?)<!-- /table:{anchor} -->", guide,
                          re.S).group(1)
        absent += [name for name in getattr(record, "methods", None) or ()
                   if f"`{name}`" not in block and not re.search(
                       rf"def {name}\(|^\s*{name} = ", block, re.M)]
    assert absent == []


def test_the_names_other_than_their_own_come_from_the_skeletons():
    """The page's opening names exactly the facts and hooks a writer writes
    under other names, so the list cannot fall behind the scaffold."""
    records = guide_pages.table_records()
    found = guide_pages.other_names(records)
    assert [item.split("`")[1] for item in found] == [
        "test_file_patterns", "kill_attribution", "check_syntax", "validity_policy",
        "execution_kinds", "structural_metrics", "scoped_metrics"]
    # A fact without a skeleton names its method in the hook table.
    assert "`test_file_patterns` is written as the method `is_test_file`" in found
    assert ("`execution_kinds` is written as the attributes `source_execution_kinds`, "
            "`test_execution_kinds` and `mutation_execution_kinds`") in found
    assert ("`validity_policy` is written as the methods `target_identity` and "
            "`analyze_validity`") in found
    opening = guide_pages.render_reference(records).split("\n## ", 1)[0]
    assert "Seven carry another name:\n\n- `test_file_patterns` is written as" in opening
    assert all(item in " ".join(opening.split()) for item in found)


def test_the_pages_match_the_hook_table():
    records =guide_pages.table_records()
    guide = guide_pages.GUIDE_PAGE.read_text(encoding="utf-8")
    assert guide == guide_pages.rewrite_guide(guide, records), (
        "add-a-language.md differs from the hook table: run python tools/guide_pages.py")
    assert guide_pages.REFERENCE_PAGE.read_text(encoding="utf-8") == (
        guide_pages.render_reference(records)), (
        "docs/reference/hooks.md differs from the hook table: run python tools/guide_pages.py")


def _rows(text: str, heading: str) -> list:
    """The name and the second cell of each row of the table under ``heading``."""
    section = text.split(heading + "\n", 1)[1].split("\n#", 1)[0]
    return re.findall(r"^\| `([^`]+)` \| ([^|]*?) \|", section, re.M)


def _settings_page() -> str:
    return (_TOOL.parents[1] / "docs" / "guide" / "settings.md").read_text(encoding="utf-8")


def _field_tables():
    """The settings page's tables of dataclass fields, by heading."""
    from langadapter import TestFrameworkConfig
    from langadapter.python.cosmic_ray import CosmicRaySettings
    from langadapter.python.project import PytestOptions

    return (("### Cosmic Ray settings", CosmicRaySettings), ("### pytest options", PytestOptions),
            ("### Test framework configuration", TestFrameworkConfig))


def test_the_settings_page_lists_what_the_reference_entries_list():
    """settings.md keeps its tables beside the reference entries it links:
    each table lists exactly its entry's settings or fields, in their order."""
    import dataclasses

    from langadapter.java.adapter import JavaAdapter
    from langadapter.python.adapter import PythonAdapter

    text = _settings_page()
    assert [name for name, _ in _rows(text, "### Python settings")] == [
        setting.name for setting in PythonAdapter.project_settings]
    assert [name for name, _ in _rows(text, "### Java settings")] == [
        setting.name for setting in JavaAdapter.project_settings]
    for heading, cls in _field_tables():
        assert [name for name, _ in _rows(text, heading)] == [
            item.name for item in dataclasses.fields(cls) if not item.name.startswith("_")]


def test_the_settings_page_states_each_fields_default():
    """Each fields table's Default column states the field's default: the
    value in code as the field holds it, or Unset or a description where the
    field defaults to None and the run chooses."""
    import dataclasses

    text = _settings_page()
    for heading, cls in _field_tables():
        defaults = {item.name: (item.default if item.default is not dataclasses.MISSING
                                else item.default_factory()) for item in dataclasses.fields(cls)}
        for name, cell in _rows(text, heading):
            if cell.startswith("`"):
                assert cell == f"`{defaults[name]}`", (heading, name, cell)
            else:
                assert defaults[name] is None, (heading, name, cell)


def test_each_module_page_leaves_out_exactly_the_names_core_renders():
    """A name langadapter exports has its entry on Core only: each module
    directive of the reference pages filters out exactly the exported names
    its module defines, so a new export cannot render twice."""
    import importlib

    import langadapter

    directive = re.compile(r"^::: (langadapter\.[\w.]+)\n((?:    .*\n)*)", re.M)
    checked = 0
    for page in sorted((guide_pages.ROOT / "docs" / "reference").glob("*.md")):
        for module, options in directive.findall(page.read_text(encoding="utf-8")):
            try:
                importlib.import_module(module)
            except ImportError:
                continue  # one object, such as langadapter.context.Execution
            defined = {name for name in langadapter.__all__
                       if getattr(getattr(langadapter, name), "__module__", None) == module}
            filters = re.findall(r'"!\^\(?([\w|]+)\)?\$"', options)
            left_out = {name for names in filters for name in names.split("|")}
            assert left_out == defined, (page.name, module, sorted(left_out ^ defined))
            checked += 1
    assert checked >= 20


def test_the_command_page_is_the_command_line_with_every_option():
    """docs/reference/cli.md renders the parser of the ``langadapter``
    command: each command has its section and each argument and option its
    row, also those only ``--help`` showed."""
    from langadapter.cli import parser

    page = guide_pages.COMMANDS_PAGE.read_text(encoding="utf-8")
    assert page == guide_pages.render_commands(parser()), (
        "docs/reference/cli.md differs from the command line: run python tools/guide_pages.py")
    for name in ("check", "new-language", "tutorial", "toolchains"):
        assert f"## `langadapter {name}` {{#{name}}}" in page
    for argument in ("[language]", "--extension ITEM", "--test-pattern PATTERN",
                     "--grammar NAME", "--update-record", "--auto", "--speed SPEED",
                     "LANGUAGE [LANGUAGE ...]", "--list"):
        assert f"| `{argument}` |" in page
    # Each row says what --help says, and nothing more.
    assert ("| `--speed SPEED` | With --auto: a factor of the pace, 1 by default (2 is twice "
            "as fast). |") in page
    assert "Default: " not in page
    # The hook table's own command has its section too.
    assert "## `python -m langadapter.hooks --json` {#hooks-json}" in page
    # Markdown shows the help texts as written: no emphasis, no tag.
    assert ("such as \\*\\_test.lua, with one \\* for the source file's stem. It gives the "
            "adapter's test\\_file\\_name (default test\\_\\*&lt;file extension>).") in page
