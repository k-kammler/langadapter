"""The neutral hook table: every fact and hook a language adapter can
provide, grouped into capability families in the usual order of building a
language up.

For each, the table says what it is, what it enables, its default when an
adapter does not write it, how versions bear on it and how to test it, but
not whether it is required: that is in the requirement profile of the
program that uses the adapter (``langadapter.consumers``). The operation
report, the check, the scaffold, the reference and the tutorial read it.
``python -m langadapter.hooks --json`` prints it as JSON (schema 1).
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from langadapter._words import SETTING_KINDS, listed

#: The JSON schema version of ``as_json``.
SCHEMA = 1

#: The guide every ``guide`` value points into.
GUIDE = "add-a-language.md"


@dataclass(frozen=True)
class Family:
    """A capability family: what it enables, what it serves where a consumer does
    not use it, and its guide section. ``capabilities`` names the capability
    report entries that also judge it (``check.family_issue``). ``useful_for``
    is what it serves, which the check names for a family a consumer does not
    need."""

    name: str
    title: str
    order: int
    enables: str
    useful_for: str
    guide: str
    capabilities: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Hook:
    """A fact (a class attribute) or a hook (a method) of a language adapter.

    ``works_by_default`` says the adapter works without it, as ``default``
    describes. ``operations`` are the public operations built on it. A hook with
    operations is present when every operation of one ``judged_by`` alternative
    is supported. Otherwise ``detect``, when set, decides from the class, or any
    one of the ``methods`` attributes shows it. ``step`` is the tutorial solution
    file that first adds it."""

    name: str
    kind: str
    family: str
    title: str
    what: str
    enables: str
    default: str
    test: str
    guide: str
    versions: str = ""
    step: str = ""
    operations: Tuple[str, ...] = ()
    methods: Tuple[str, ...] = ()
    detect: Optional[Callable[[type], bool]] = field(default=None, compare=False, repr=False)
    works_by_default: bool = False
    present_when: Tuple[Tuple[str, ...], ...] = ()

    @property
    def judged_by(self) -> Tuple[Tuple[str, ...], ...]:
        """The alternatives of operations that make this hook present, each when
        all its operations are supported: ``present_when``, else its first
        operation. Empty for a fact or a hook without operations."""
        if self.kind != "hook" or not self.operations:
            return ()
        return self.present_when or (self.operations[:1],)


def _anchor(anchor: str) -> str:
    return f"{GUIDE}#{anchor}"


def _executes_in_container(cls: type) -> bool:
    kinds = set(getattr(cls, "test_execution_kinds", ()) or ()) | set(
        getattr(cls, "source_execution_kinds", ()) or ())
    return "container" in kinds


def _declares_support(cls: type) -> bool:
    from langadapter.versions import UNDECLARED
    return getattr(cls, "support", UNDECLARED) is not UNDECLARED


_FAMILIES: Tuple[Family, ...] = (
    Family("facts", "Language facts", 0,
           "everything else to name, find and describe the language's files and tests",
           "every program that uses the adapter", _anchor("language-facts")),
    Family("run tests", "Run the tests", 1,
           "running a project's test suite and reading each test's outcome",
           "any program that runs a project's tests", _anchor("run-the-tests"),
           capabilities=("suite_runs",)),
    Family("coverage", "Measure coverage", 2,
           "the line and branch coverage of a source file under a suite",
           "judging how much of the code the tests reach", _anchor("measure-coverage"),
           capabilities=("coverage",)),
    Family("mutation", "Run mutation testing", 3,
           "each mutant's status under a suite, with its change and location",
           "judging whether the tests notice changes to the code",
           _anchor("run-mutation-testing"), capabilities=("mutation",)),
    Family("kill attribution", "Attribute kills to tests", 4,
           "which tests kill each mutant",
           "reducing a suite while keeping every kill", _anchor("kill-attribution"),
           capabilities=("per_test_kill_attribution",)),
    Family("load repair", "Repair loading", 5,
           "keeping a test file whose loading fails at one unit, by removing that unit",
           "keeping generated or edited tests that fail to load", _anchor("repair-loading")),
    Family("import pruning", "Remove failing imports", 6,
           "removing the imports of a test file that do not resolve in the project",
           "keeping generated tests whose fault is an import of something the project lacks",
           _anchor("remove-failing-imports")),
    Family("syntax repair", "Check and repair syntax", 7,
           "telling a source the language's parser accepts from one it rejects, and a "
           "bounded repair of one it rejects",
           "rejecting broken files early and keeping generated tests with a local syntax error",
           _anchor("repair-syntax")),
    Family("skip removal", "Remove whole-file skips", 8,
           "removing statements that skip a whole test file",
           "making the tests of a file run instead of skipping", _anchor("remove-whole-file-skips")),
    Family("import guidance", "Name the import line", 9,
           "the line a test of a source file uses to import it",
           "writing a first test for a source file", _anchor("import-line")),
    Family("merging", "Merge test files", 10,
           "folding several test files into one, with their name collisions resolved",
           "combining suites into one", _anchor("merge-test-files"),
           capabilities=("source_merging",)),
    Family("per-test evidence", "Collect per-test evidence", 11,
           "each test's own coverage",
           "reducing a suite to the fewest tests that cover what it covered",
           _anchor("per-test-evidence"), capabilities=("per_test_evidence",)),
    Family("instrumentation", "Prepare a suite", 12,
           "the setup a suite runs with, and warnings of tests that leave state behind",
           "isolated measurements and finding tests that leak state", _anchor("prepare-a-suite")),
    Family("delivered runs", "Run as delivered", 13,
           "running a suite as a user who copied it would",
           "checking that a suite works where it is delivered", _anchor("run-as-delivered")),
    Family("validity screening", "Screen generated tests", 14,
           "findings on tests that read the implementation instead of calling it",
           "keeping generated tests honest", _anchor("validity-policy")),
    Family("timing", "Time a suite", 15,
           "the time a suite takes, repeated, and warmed where the runtime warms up",
           "choosing between suites by speed and timing records", _anchor("timing")),
    Family("versions", "Declare versions", 16,
           "notes and refusals by the version of the runtime and the tools",
           "telling users which versions are tested", _anchor("versions")),
    Family("containers", "Run in containers", 17,
           "runs inside a container the user prepared",
           "running projects whose tools live in a container", _anchor("containers")),
    Family("structural metrics", "Structural metrics", 18,
           "sizes and complexity of sources and tests, not the test count",
           "describing sources and suites", _anchor("structural-metrics")),
    Family("metric providers", "Metric providers", 19,
           "further metrics from the language package or from other packages, per file and "
           "per scope",
           "analysis tools that add their own metrics", _anchor("metric-providers")),
    Family("code smells", "Code smells", 20,
           "a count of a linter's findings in sources and tests",
           "judging the style of generated code", _anchor("code-smells")),
    Family("call statistics", "Call statistics", 21,
           "estimates of how often the rest of the project calls a file's functions",
           "impact analysis, review priorities and ranking code by its reach",
           _anchor("call-statistics")),
    Family("profiling", "Profile a suite", 22,
           "where a suite's run spends its time, by function and file",
           "performance work, ranking code by measured cost and choosing the tests that "
           "exercise a function", _anchor("profiling")),
)


def _fact(name, title, what, default, test, *, methods=(), operations=(), step="01_run_tests.py",
          versions=""):
    return Hook(name, "fact", "facts", title, what, "naming and finding the language's files and tests",
                default, test, _anchor("language-facts"), versions, step, operations,
                methods or (name,), works_by_default=not default.startswith("none"))


_HOOKS: Tuple[Hook, ...] = (
    _fact("language", "The language's name",
          "The registered name, such as javascript.", "none: every adapter names its language",
          "The registry finds the adapter by this name.", methods=("language",)),
    _fact("file_extensions", "The file extensions",
          "The file extensions of the language's source files, such as .js.",
          "none: every adapter names at least one", "language_for(path) names the language.",
          methods=("file_extensions",)),
    _fact("display_name", "The name as people write it",
          "The language's name in messages and prompts, such as JavaScript.",
          "the consumer's own, such as the name with a capital first letter",
          "The check shows it."),
    _fact("runtime", "The program that runs the tests",
          "The program a project's tests run with, such as node.",
          "none: the scaffold marks it to set",
          "Your adapter class's discover_runtime(root, {}) finds it on PATH."),
    _fact("test_file_name", "How a test file is named",
          "A template for the test file of a source file, with {stem}, {Stem} (the stem "
          "with a capital first letter) and {ext}, such as {stem}.test.js.",
          "test_{stem} with the language's file extension", "The check names the file for an example source."),
    _fact("test_file_patterns", "Which files are test files",
          "File name patterns every test file matches, such as *.test.js.",
          "none: write is_test_file", "is_test_file tells a test file from a source file.",
          operations=("sources.is_test_file", "sources.project_source_files"),
          methods=("is_test_file",)),
    _fact("test_id_text", "A test's id as text",
          "A test's id as the language writes it in kill matrices and records, such as "
          "demo.CalcTest.adds.", "the TestId's own text: the file and the test's name",
          "The example's test ids are unique."),
    _fact("test_id_prefix", "The prefix of a file's test ids",
          "The part of the ids of a file's tests before each test's name, such as "
          "demo.CalcTest.", "the file's stem", "The example's test ids start with it."),
    _fact("file_rule", "A rule a file name follows",
          "The stem a file holding a source must have, such as its public class's name, "
          "and the source renamed to fit a given stem.",
          "no rule", "A file that breaks the rule is named with the rule.",
          methods=("file_rule", "rename_file_unit")),
    _fact("scratch_stem", "A test file's stem without a target",
          "The stem of a test file written for no particular target, such as Candidate.",
          'the stem "candidate"', "The file is named by the test file name with this stem."),
    _fact("code_fence", "The code fence",
          "The language's name in a fenced code block, such as js.",
          "the consumer's own, such as the language's name",
          "The check shows it."),
    _fact("test_framework", "The test framework",
          "The framework the tests use, such as node:test.",
          "the consumer's own, such as the general words \"the test framework\"",
          "language_facts names it."),
    _fact("test_vocabulary", "The words for the language's tests",
          "The method test_vocabulary returns the language's words for its tests, by the "
          "entries of langadapter.vocabulary: how a test is declared and collected, fixtures, "
          "parametrization, the one-file rule, the import rule, the word for a load failure "
          "and more.",
          "generic sentences from the facts above",
          "langadapter.vocabulary.problems finds no unknown entry and no text that is not "
          "a string."),
    _fact("project_settings", "The project's settings",
          "The settings a project of the language takes, each with its name, purpose, kind "
          f"({listed(SETTING_KINDS, 'or')}), environment alias, discovery default, check "
          "and how a relative host path in it resolves (host_path).",
          "no settings", "Each setting's check accepts its default."),
    _fact("ignored_dirs", "Directories that hold no sources",
          "Directories a search for source files skips, such as node_modules.",
          "the usual version control, environment, build, cache and test directories",
          "The example's sources are found."),
    _fact("non_source_files", "Files that are not sources",
          "File names a search for source files leaves out although they carry the "
          "language's file extension, such as a build script.",
          "no file left out by its name", "The example's sources are found."),
    _fact("reuses_bytecode", "Compiled modules shared by copies",
          "Whether a measurement copy of a project may reuse the compiled modules of the runs "
          "in earlier copies.", "off",
          "The check shows it."),
    _fact("copies_external_suite", "A suite outside the project copied with it",
          "Whether a measurement copy of a project takes a suite directory outside the project "
          "with it, so that the paths a run prints are the ones a run in place printed.",
          "off", "The check shows it."),
    _fact("provides_timeout_journal", "The per-test timeout journal",
          "Whether run_suite takes timeout_journal, the per-test journal from which a run "
          "that a timeout or a crash ended names the test that was running. An adapter "
          "that refuses it turns this off. An adapter whose answer depends on the project "
          "sets it on the instance, and a consumer reads it on the adapter.",
          "on: run_suite takes the timeout journal",
          "check_adapter runs the example with the journal when this is on."),
    _fact("seeds_delivered_runs", "A session seed for runs as delivered",
          "Whether run_as_delivered takes session_seed, so that a consumer may run a suite "
          "as delivered with a fixed seed as its normal run. An adapter whose runs as "
          "delivered take the seed turns this on. An adapter whose answer depends on the "
          "project sets it on the instance, and a consumer reads it on the adapter.", "off",
          "The check shows it."),
    _fact("source_merge_separator", "The separator line of a merge",
          "The comment prefix of the line merge_sources puts between two folded test "
          "files, such as //.", "none declared: each merge needs the caller's separator line",
          "Two example test files fold with the separator line between them."),
    _fact("validity_policy_version", "The version of the validity policy",
          "The version of the generated-test validity policy whose findings "
          "analyze_validity reports, which callers record with each run.",
          "no version: a run records none", "The check shows it."),

    Hook("run_suite", "hook", "run tests", "Run the tests",
         "Run a suite and return each test's outcome and the run's outcome: it loads, it does "
         "not load, it has no tests, it timed out, a tool failed, its setup is missing, or it "
         "is unsupported.",
         "running a suite and listing its failing tests, and telling a suite that does not load "
         "from one that lacks its setup",
         "none: write it", "A passing and a failing test read passed and failed, and a test file "
         "that does not load reads does not load.",
         _anchor("run-the-tests"), step="01_run_tests.py",
         operations=("tests.run_suite", "tests.collect_suite"),
         methods=("run_suite", "collection_text", "listed_failures", "collect_suite")),
    Hook("test_units", "hook", "run tests", "Find and remove tests",
         "Find each test in a test file, by hand or with the tree-sitter toolkit "
         "(langadapter.treesitter), so that one test can be removed, counted or listed.",
         "removing a failing test instead of the whole file, and the test count",
         "none: write it, by hand or with the tree-sitter toolkit",
         "Removing one named test leaves the others, and the count drops by one.",
         _anchor("find-and-remove-tests"), step="02_test_units.py",
         operations=("sources.prune_tests", "sources.duplicate_test_names", "sources.test_metrics"),
         methods=("prune_tests", "test_metrics"),
         present_when=(("sources.prune_tests", "sources.test_metrics"),)),
    Hook("check_syntax", "hook", "syntax repair", "Check the syntax",
         "Tell a source the language parses from one it rejects, with the first error's place.",
         "rejecting a broken file before running it",
         "none: write validate_source, which runs the project's runtime, or check_structure, "
         "a parse with the grammar alone",
         "A file with a syntax error reads invalid, with its line.",
         _anchor("check-syntax"),
         operations=("sources.check_structure", "sources.validate_source",
                     "sources.finalize_source"),
         methods=("validate_source", "check_structure", "finalize_source"),
         present_when=(("sources.validate_source",), ("sources.check_structure",))),
    Hook("project", "hook", "run tests", "Resolve the project",
         "Resolve a project's build, such as its dependencies and class path, and say why it "
         "does not resolve.",
         "refusing a project that does not build before any test runs",
         "every project counts as resolved",
         "A project whose build fails reads not resolved, with the reason.",
         _anchor("resolve-the-project"), methods=("project",), works_by_default=True),
    Hook("discover_runtime", "hook", "run tests", "Find the runtime",
         "Find the program that runs the tests, and the tools, for a project, and "
         "describe what its settings select without checking it (describe_runtime).",
         "a project's context without settings the user must write",
         "the runtime fact found on PATH",
         "Your adapter class's discover_runtime(root, {}) finds the example project's runtime.",
         _anchor("find-the-runtime"), "LangAdapter reads no runtime version by default: a run "
         "records the context's runtime_version. An adapter whose hooks need the version reads "
         "it itself, such as from the runtime's --version.",
         step="01_run_tests.py", methods=("discover_runtime", "describe_runtime",
                                          "context_environment"), works_by_default=True),
    Hook("provision_runtime", "hook", "run tests", "Provision the runtime",
         "Make an environment for a requested runtime version, with the project's "
         "requirements and the tools the adapter pins, reused while those inputs stay the "
         "same.",
         "a project run on a runtime version the machine does not have yet",
         "none: a run that asks for another runtime version is refused",
         "A requested version gives an environment that runs the example once and is reused.",
         _anchor("find-the-runtime"), "The pinned tools follow the requested version.",
         methods=("provision_runtime",), works_by_default=True),
    Hook("setup_questions", "hook", "run tests", "Ask the setup's questions",
         "The questions a consumer's setup asks for a project of the language, with each "
         "one's default, reason, choices and check and what is shown after an answer, and "
         "the check of a setting set without the questions (setup_problem).",
         "a setup that finds the runtime and the build tools and checks them as it asks",
         "each declared setting asked with its purpose and checked by its kind",
         "The example project's questions are asked and their answers checked.",
         _anchor("find-the-runtime"),
         methods=("setup_questions", "setup_problem"), works_by_default=True),
    Hook("language_facts", "hook", "run tests", "Describe the language",
         "Return the language's LanguageFacts: its name, the test framework, the code fence, "
         "the runtime's version and the language level, the test file, test function and test "
         "class patterns, the per-test timeout marker, whether a reduction keeps unmatched "
         "evidence and the patterns of the warnings that mark a test leaving shared state "
         "behind.",
         "prompts and records that name the language and its tests",
         "none: a program that writes prompts cannot name the language's tests",
         "language_facts names the example's test framework.",
         _anchor("describe-the-language"), step="01_run_tests.py",
         methods=("language_facts",)),
    Hook("capabilities", "hook", "run tests", "Declare capabilities",
         "Report what the project can run: its runtime, its tools and each capability of "
         "langadapter.capabilities.CAPABILITIES, available, missing setup or unsupported, "
         "with the reason.",
         "a readiness report, and programs that run a project only when its tools are there",
         "none: the report says readiness probing is not implemented and lists no capability",
         "adapter.probe_capabilities() reports the example's suite runs available.",
         _anchor("capabilities"), step="01_run_tests.py", methods=("capabilities",)),
    Hook("execution_setup_issue", "hook", "run tests", "Name missing setup",
         "Name a prerequisite of running the project's tools that is known to be missing "
         "without starting a tool, such as a setting a container run needs.",
         "missing setup reported with its reason before any tool starts",
         "no prerequisite is known to be missing",
         "With the prerequisite missing, a run of the example reports missing setup with the "
         "reason.",
         _anchor("capabilities"), methods=("execution_setup_issue",), works_by_default=True),
    Hook("operation_context_issue", "hook", "run tests", "Refuse an operation in a context",
         "Say why an operation cannot run in the project's context, such as a kind of "
         "execution or a test framework the adapter does not take.",
         "an unsupported operation reported with its reason, not as not implemented",
         "every operation the adapter implements runs in each context it declares",
         "In the refused context, the operation reads unsupported with the reason.",
         _anchor("capabilities"), methods=("operation_context_issue",), works_by_default=True),
    Hook("operation_readiness", "hook", "run tests", "Give each operation's readiness",
         "Give an operation's readiness from the probed capability report, as a Capability "
         "named like the operation, or None when the probe did not check it.",
         "a probed report that says which operations can run",
         "the probed readiness of each operation stays unknown",
         "With a tool the probe found missing, probe_capabilities reports the operations that "
         "need it as missing setup.",
         _anchor("capabilities"), methods=("operation_readiness",), works_by_default=True),
    Hook("request_options", "hook", "run tests", "Choose run options",
         "The request fields the language adds for each kind of run a consumer asks for "
         "(RUN_PURPOSES: a run of the tests, failing tests, per-test evidence, a run as "
         "delivered, timing, a coverage matrix, a collection, the project's own run), such as "
         "its runner's options.",
         "runs set up the way the language's tools need", "no options",
         "Each kind of run gets the options it is given.", _anchor("run-options"),
         methods=("request_options",), works_by_default=True),
    Hook("test_files", "hook", "run tests", "Collect a directory's test files",
         "Replace a directory by the test files the language's runner collects in it.",
         "running a directory of tests as the runner would", "the files the test file name names",
         "A directory of the example gives its test files.", _anchor("run-the-tests"),
         methods=("test_files",), works_by_default=True),
    Hook("project_test_files", "hook", "run tests", "List the project's test files",
         "The test files the language's runner collects in the project as the project "
         "configures them.",
         "the project's own suite as its runner would run it", "test_files of the project root",
         "The example's project gives its test files.", _anchor("run-the-tests"),
         methods=("project_test_files",), works_by_default=True),
    Hook("setup_files", "hook", "run tests", "Setup files beside the tests",
         "The files the runner loads beside a suite's test files, such as pytest's "
         "conftest.py, and the setup file of a prepared suite.",
         "a copy of a suite that runs with the setup its tests need", "none",
         "A suite copied with its setup files runs as it did in place.", _anchor("run-the-tests"),
         methods=("setup_files", "shared_setup_files"), works_by_default=True),
    Hook("run_counts", "hook", "run tests", "Count a run",
         "A run's collected, passed, failed and errored tests as the runner counts them.",
         "records whose counts match the runner's own", "the run's counts",
         "A run of the example counts its tests.", _anchor("run-the-tests"),
         methods=("run_counts",), works_by_default=True),
    Hook("coverage", "hook", "coverage", "Measure coverage",
         "Coverage is part of run_suite: when the request names a coverage_target, the run "
         "returns that file's covered and missed lines and branches.",
         "the line and branch coverage of a source file",
         "none: write it", "A run of the example with its source as the target reports a "
         "covered line.", _anchor("measure-coverage"), step="03_coverage.py",
         operations=("tests.run_suite",)),
    Hook("coverage_texts", "hook", "coverage", "Coverage as text for records",
         "A run's covered lines and missed branches as text, as the coverage tool counts them.",
         "records that read as the language's coverage tool reports",
         "the covered lines, each missed branch as source->destination",
         "A run of the example writes its covered lines.", _anchor("measure-coverage"),
         methods=("coverage_texts", "coverage_matrix_in_place"), works_by_default=True),
    Hook("open_mutation_session", "hook", "mutation", "Run mutation testing",
         "Open a session that runs the mutation tool and returns each mutant's id, location, "
         "change text and status.",
         "each mutant's status, which a consumer reads mutant by mutant, not only as a score",
         "none: write it", "A session on the example gives a killed and a surviving mutant.",
         _anchor("run-mutation-testing"), step="04_mutation.py",
         operations=("mutations.engines", "mutations.operators", "mutations.open_session",
                     "mutations.run", "mutations.per_suite"),
         methods=("open_mutation_session", "mutation_catalogue", "named_operators",
                  "mutation_session_attempts", "mutation_dir_refusal",
                  "mutation_workers_refusal", "own_mutant_timeout_rule"),
         present_when=(("mutations.open_session",),)),
    Hook("validate_mutation_session_options", "hook", "mutation", "Check engine options",
         "Check a mutation session's options beyond workers, such as a distributor or "
         "settings, raising InvalidRequest for one the engine does not accept.",
         "engine options beyond workers", "a session with such an option is refused",
         "An option the engine takes opens a session, and one it does not take is refused.",
         _anchor("run-mutation-testing"), methods=("validate_mutation_session_options",),
         works_by_default=True),
    Hook("kill_attribution", "hook", "kill attribution", "Attribute kills to tests",
         "Name, for each killed mutant, the tests that kill it.",
         "reductions that keep every kill", "no attribution",
         "A mutant killed by one test names that test.", _anchor("kill-attribution"),
         step="04_mutation.py", methods=("mutation_evidence_granularity",)),
    Hook("kill_tokens", "hook", "kill attribution", "Attribute stored kills",
         "Turn a stored kill matrix's named killers into each test's kill tokens, and a "
         "pool's kills into the tokens of its suites' tests.",
         "reductions and pools that keep every kill",
         "the killing tests each row records (Mutant.killing_tests)",
         "A killer that names one test gives that test its mutant's token.",
         _anchor("kill-attribution"), methods=("kill_tokens", "pool_kills"),
         works_by_default=True),
    Hook("prune_collection_breakers", "hook", "load repair", "Repair loading",
         "Remove the units that keep a test file from loading: the shared loop "
         "(templates.repair_loading) with the language's load step (collection or "
         "compilation, reporting the failing lines) and its removal of the unit around each "
         "line, a statement, a member or a line.",
         "the shared loop that removes the units that keep a file from loading",
         "no load repair", "A file with one unit that does not load keeps its other tests.",
         _anchor("repair-loading"), step="05_load_repair.py",
         operations=("sources.prune_collection_breakers",),
         methods=("prune_collection_breakers",)),
    Hook("collection_unit", "fact", "load repair", "The unit load repair removes",
         "The unit prune_collection_breakers removes, in words, such as the smallest "
         "declaration around a line javac reports an error on.",
         "a consumer's record of what a repair removed",
         "none: an adapter that repairs loading declares it",
         "The example's breaker is removed as that unit.", _anchor("repair-loading"),
         step="05_load_repair.py", methods=("collection_unit",)),
    Hook("prune_unresolvable_imports", "hook", "import pruning", "Remove failing imports",
         "Remove the imports of a test file that do not resolve in the project: the shared "
         "loop (templates.prune_units) with the language's search for the failing imports "
         "and their removal.",
         "keeping a test file whose fault is an import of something the project lacks",
         "no import pruning", "A file that imports a missing module keeps its tests, "
         "without that import.", _anchor("remove-failing-imports"),
         operations=("sources.prune_unresolvable_imports", "sources.imports",
                     "sources.import_path_candidates"),
         methods=("prune_unresolvable_imports",)),
    Hook("salvage_syntax", "hook", "syntax repair", "Repair syntax",
         "Repair a source's syntax errors line by line: the shared loop "
         "(templates.repair_syntax) with the language's validator and local fix.",
         "the shared loop that repairs a source line by line", "no syntax repair",
         "A source with one bad line parses after the repair.", _anchor("repair-syntax"),
         operations=("sources.salvage_syntax",), methods=("salvage_syntax", "salvage_report")),
    Hook("strip_module_level_skips", "hook", "skip removal", "Remove whole-file skips",
         "Remove the statements or annotations that skip a whole test file.",
         "removing them so that the file's tests run", "no skip removal",
         "A file with a whole-file skip runs its tests after the removal.",
         _anchor("remove-whole-file-skips"), operations=("sources.strip_module_level_skips",),
         methods=("strip_module_level_skips", "skip_removal")),
    Hook("import_statement", "hook", "import guidance", "Name the import line",
         "The line a test uses to import a given source file, or a sentence that "
         "says none is needed (`none; ...`).",
         "a first test that imports its target correctly", "the vocabulary's import rule only",
         "The line imports the example's source, or the sentence says that no "
         "import line is needed.", _anchor("import-line"),
         operations=("sources.import_statement",), methods=("import_statement",)),
    Hook("merge_sources", "hook", "merging", "Merge test files",
         "Fold one test file into another and resolve the names both declare. "
         "`resolve_collisions` returns a `SourceObservation`, completed with a "
         "`CollisionResolution` and its parser, or with `infrastructure_failure`, `timeout` "
         "or `missing_setup` and a reason.",
         "one suite from several", "no merging",
         "Two example test files fold into one whose tests all run.", _anchor("merge-test-files"),
         operations=("sources.merge_sources", "sources.resolve_collisions",
                     "sources.merge_collisions", "sources.normalize_bindings",
                     "sources.normalize_shadowed", "sources.stage_pool"),
         methods=("merge_sources", "resolve_collisions", "collision_resolution",
                  "collision_dependents", "merge_collisions", "normalize_bindings",
                  "normalize_shadowed", "stage_pool")),
    Hook("per_test_evidence", "hook", "per-test evidence", "Collect per-test evidence",
         "Return each test's own coverage.", "the reductions that keep what a suite covered",
         "no per-test evidence", "Each example test reports the lines it covers.",
         _anchor("per-test-evidence"), step="03_coverage.py",
         operations=("tests.per_test_evidence",), methods=("per_test_evidence",)),
    Hook("test_evidence_granularity", "fact", "per-test evidence",
         "The granularity of test evidence",
         "The granularity of the evidence each test operation reports, as pairs of the "
         "operation's name and its granularities: aggregate for the suite as a whole, "
         "per_test for each test, such as (\"per_test_evidence\", (\"per_test\",)).",
         "an operation report that says which operations report evidence per test",
         "no granularity: the operation report gives each test operation none",
         "operation_capabilities() gives each declared operation its granularity.",
         _anchor("per-test-evidence"), methods=("test_evidence_granularity",),
         works_by_default=True),
    Hook("prepare_suite", "hook", "instrumentation", "Prepare a suite",
         "Write the setup a suite directory needs before its tests run, such as an import "
         "shim, and on request the configuration that isolates it.",
         "isolated, repeatable measurements", "no setup written",
         "A prepared suite runs.", _anchor("prepare-a-suite"),
         methods=("prepare_suite",)),
    Hook("unsound_test_patterns", "fact", "instrumentation", "Warn of tests that leak state",
         "In LanguageFacts.unsound_test_patterns, the regular expressions of the warnings "
         "that mark a test leaving shared state behind. A suite run reports each such "
         "warning in its diagnostics, naming the test that left it.",
         "removing passing tests that leak state", "no warnings read",
         "A test that leaks state is named.", _anchor("unsound-test-warnings")),
    Hook("order_dependent_mock_warnings", "hook", "instrumentation",
         "Warn of order-dependent mocks",
         "Name the tests that assert on a named element of a mock's side_effect list, "
         "whose order a scheduler in the code under test can change.",
         "advice on tests that pass in one scheduling order only", "no advice",
         "A test that asserts on one element of a mock's side_effect list is named.",
         _anchor("unsound-test-warnings"), methods=("order_dependent_mock_warnings",)),
    Hook("run_as_delivered", "hook", "delivered runs", "Run as delivered",
         "Run each test file alone, as a user who copied it would, "
         "all of them within the request's one timeout.",
         "the check that a suite works where it is delivered", "no delivered runs",
         "The example's test file passes as delivered.", _anchor("run-as-delivered"),
         operations=("tests.run_as_delivered",),
         methods=("run_as_delivered", "delivered_order_refusal")),
    Hook("delivered_case", "hook", "delivered runs", "A delivered test as text for records",
         "A test of a run as delivered: its id, its name with its parameters, and its outcome.",
         "records of delivered runs in the language's own words",
         "the test's id, its name and parameters, its outcome",
         "A delivered run of the example names each test once.", _anchor("run-as-delivered"),
         methods=("delivered_case",), works_by_default=True),
    Hook("validity_policy", "hook", "validity screening", "Screen generated tests",
         "Report the places where a test reads the implementation's source or internals "
         "instead of calling it.",
         "a gate that keeps such tests out", "no screening",
         "A test that reads the source file is reported.", _anchor("validity-policy"),
         operations=("sources.target_identity", "sources.analyze_validity",
                     "sources.screen_source"),
         methods=("analyze_validity", "target_identity", "screen_source")),
    Hook("benchmark", "hook", "timing", "Time a suite",
         "Run a suite repeatedly and return its times, warmed where the runtime warms up.",
         "choosing between suites by speed", "no timing",
         "Repeated runs of the example give times.", _anchor("timing"),
         operations=("tests.benchmark", "tests.benchmark_warmed"),
         methods=("benchmark", "benchmark_warmed")),
    Hook("support", "fact", "versions", "Declare versions",
         "The runtime versions the adapter is tested and expected to work with, and the "
         "support of named tools. The Python and Java adapters refuse an older version "
         "with the reason. Another adapter refuses one only when its version profile's "
         "refusal() gives a reason (choose_profile).",
         "notes and refusals by version",
         "undeclared: every version is untested and never refused",
         "support.classify(version) reads tested for a tested version. The adapter's "
         "profile() reads it for the runtime its choose_profile found. Where the profile "
         "refuses older versions, an older one is refused with the reason.",
         _anchor("versions"), "This is the version declaration.", step="06_versions.py",
         detect=_declares_support, works_by_default=True),
    Hook("choose_profile", "hook", "versions", "Refuse runtime versions",
         "Return the VersionProfile (langadapter.versions) of the runtime version the adapter "
         "found, from a subclass whose refusal() gives the reason for a version the adapter "
         "does not support.",
         "refusing a runtime the adapter does not support, with the reason",
         "the base profile, which reads no runtime and refuses no version",
         "A runtime older than the adapter supports is refused with the reason.",
         _anchor("versions"), methods=("choose_profile",), works_by_default=True),
    Hook("execution_kinds", "fact", "containers", "Run in containers",
         "Whether source operations, test runs and mutation sessions can run in a container "
         "the user prepared, each by its own fact.",
         "runs inside a container", "local runs only",
         "A container context runs the example's suite.", _anchor("containers"),
         detect=_executes_in_container),
    Hook("structural_metrics", "hook", "structural metrics", "Structural metrics",
         "Sizes and complexity of a source, per file and per function, and the names and "
         "fingerprints of its definitions.",
         "describing sources and suites", "no structural metrics",
         "The example's source has a size.", _anchor("structural-metrics"),
         operations=("sources.metrics", "sources.skeleton", "sources.same_skeleton",
                     "sources.function_fingerprints"),
         methods=("optimization_metrics", "structural_parser_identity", "skeleton",
                  "same_skeleton", "function_fingerprints")),
    Hook("scoped_metrics", "hook", "metric providers", "Metrics per function",
         "The structural metrics per function, with each function's own complexity, beside "
         "the file's. It takes effect with the two structural metrics methods. Metrics of "
         "registered providers need no hook.",
         "analysis tools that add their own metrics", "no scoped metrics",
         "A function of the example's source has its own complexity.",
         _anchor("metric-providers"),
         methods=("scoped_optimization_metrics",)),
    Hook("metric_providers", "hook", "metric providers", "Metric providers",
         "The language's own metric providers, which every adapter object of the language "
         "registers beside the structural provider. A name already registered is refused.",
         "metrics a language package adds for all its adapters", "no metrics of its own",
         "A metric of the language's provider is listed and measured.",
         _anchor("metric-providers"), methods=("metric_providers",)),
    Hook("code_smells", "hook", "code smells", "Code smells",
         "Count a linter's findings in given files.",
         "judging the style of generated code", "no smell count",
         "A file with one finding counts one.", _anchor("code-smells"),
         operations=("sources.code_smells",), methods=("code_smells",)),
    Hook("call_statistics", "hook", "call statistics", "Call statistics",
         "Count a source's call sites by the called name. The call statistics of a file "
         "count them in the project's other source files for each function the scoped "
         "metrics report.",
         "estimating how widely a file's functions are used", "no call statistics",
         "A call of a function of the example's source in another file counts.",
         _anchor("call-statistics"), operations=("sources.call_statistics",),
         methods=("call_statistics",)),
    Hook("profile_suite", "hook", "profiling", "Profile a suite",
         "Run a suite once under the language's profiler and return the time each function "
         "it entered took, with the profiler's name, version and kind.",
         "finding the functions a suite spends its time in", "no suite profile",
         "A profile of the example's suite names a function of its source.",
         _anchor("profiling"), operations=("tests.profile_suite",), methods=("profile_suite",)),
)


def families() -> Tuple[Family, ...]:
    """Every family, in build-up order."""
    return tuple(sorted(_FAMILIES, key=lambda f: f.order))


def family(name: str) -> Family:
    """The family ``name``. KeyError names the families when there is none."""
    for found in _FAMILIES:
        if found.name == name:
            return found
    raise KeyError(f"no family {name!r}: the families are "
                   + ", ".join(f.name for f in families()))


def table() -> Tuple[Hook, ...]:
    """Every fact and hook, in build-up order: by family, then as listed."""
    order = {f.name: f.order for f in _FAMILIES}
    return tuple(sorted(_HOOKS, key=lambda h: order[h.family]))


def hook(name: str) -> Hook:
    """The fact or hook ``name``. KeyError names the hooks when there is none."""
    for found in _HOOKS:
        if found.name == name:
            return found
    raise KeyError(f"no fact or hook {name!r}: they are " + ", ".join(h.name for h in table()))


def hooks_of(name: str) -> Tuple[Hook, ...]:
    """The facts and hooks of the family ``name``, in table order."""
    family(name)
    return tuple(h for h in table() if h.family == name)


def _base_method(name: str) -> bool:
    """Whether LangAdapter defines ``name`` as a method, not a value."""
    from langadapter.adapter import LangAdapter
    return callable(getattr(LangAdapter, name, None))


def _base_call(name: str) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """The call of a hook method LangAdapter defines, as its signature reads."""
    import inspect

    from langadapter.adapter import LangAdapter
    parameters = list(inspect.signature(getattr(LangAdapter, name)).parameters.values())
    if not isinstance(inspect.getattr_static(LangAdapter, name), (classmethod, staticmethod)):
        parameters = parameters[1:]
    return (tuple(p.name for p in parameters if p.kind is p.POSITIONAL_OR_KEYWORD),
            tuple(p.name for p in parameters if p.kind is p.KEYWORD_ONLY))


#: How each hook method is called: positional, then keyword arguments. A hook
#: method listed below is called as listed, every other one LangAdapter
#: defines as its own method takes them. A class defining one with a signature
#: that cannot take that call is refused on creation.
_CALLS: Dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {
    "run_suite": (("request",), ()),
    "run_as_delivered": (("request",), ("sources",)),
    "prune_tests": (("source", "tests"), ()),
    "test_units": (("source",), ()),
    "test_metrics": (("source",), ()),
    "validate_source": (("source",), ()),
    "check_structure": (("source",), ()),
    "prune_collection_breakers": (("source",), ("timeout_s",)),
    "prune_unresolvable_imports": (("source",), ()),
    "salvage_syntax": (("source",), ()),
    "strip_module_level_skips": (("source",), ()),
    "merge_sources": (("first", "second"), ("separator",)),
    "prepare_suite": (("suite_root",), ("isolation_config",)),
    "profile_suite": (("request",), ()),
    "order_dependent_mock_warnings": (("source",), ()),
    "structural_parser_identity": ((), ()),
    "optimization_metrics": (("source",), ()),
    "scoped_optimization_metrics": (("source",), ()),
    "is_test_file": (("path",), ()),
    "language_facts": ((), ()),
    "capabilities": ((), ()),
    "call_statistics": (("source",), ()),
    "validate_mutation_session_options": (
        ("engine",), ("workers", "distributor", "allow_fewer_workers", "settings")),
}
_CALLS.update({name: _base_call(name) for name in sorted({m for h in _HOOKS for m in h.methods} - set(_CALLS))
               if _base_method(name)})
#: Hook methods called on the class before a context exists: class or static methods.
_CLASS_CALLS = frozenset({"discover_runtime", "describe_runtime", "context_environment",
                          "import_statement", "provision_runtime", "setup_questions",
                          "setup_problem"})


def _signature_problems(cls: type) -> Tuple[str, ...]:
    """A problem, naming the hook, for each hook method ``cls`` defines itself
    whose signature cannot take its call (``_CALLS``), or that is not a class or
    static method where the class is called (``_CLASS_CALLS``)."""
    import inspect

    found = []
    for method, (positional, keywords) in _CALLS.items():
        raw = cls.__dict__.get(method)
        if raw is None or not callable(getattr(cls, method, None)):
            continue
        function = getattr(cls, method)
        owner = next((h.name for h in _HOOKS if method in h.methods), method)
        bound_to_class = isinstance(raw, (staticmethod, classmethod))
        if method in _CLASS_CALLS and not bound_to_class:
            found.append(f"hook {owner}: {cls.__name__}.{method} is called on the class, "
                         "before a project context exists: make it a classmethod.")
            continue
        try:
            signature = inspect.signature(function)
        except (TypeError, ValueError):
            continue
        receiver = () if bound_to_class else (None,)
        try:
            signature.bind(*receiver, *positional, **dict.fromkeys(keywords))
        except TypeError:
            call = ", ".join((*positional, *(f"{name}=..." for name in keywords)))
            found.append(f"hook {owner}: {cls.__name__}.{method}{signature} cannot be "
                         f"called as {method}({call}).")
    return tuple(found)


def _present(cls: type, entry: Hook) -> bool:
    """Whether ``entry`` is present in the class ``cls``, from the class alone: its
    ``detect`` rule, else one of its ``methods`` defined beyond LangAdapter's
    own. A hook only a run shows (coverage) is missing by this measure."""
    import inspect

    from langadapter.adapter import LangAdapter
    if entry.detect is not None:
        return bool(entry.detect(cls))
    for name in entry.methods:
        # Static lookup: getattr makes a new bound class method each time.
        value = inspect.getattr_static(cls, name, None)
        if value is None:
            continue
        base = inspect.getattr_static(LangAdapter, name, None)
        if value is base:
            continue
        if (callable(value) or isinstance(value, (classmethod, staticmethod))
                or (value not in ((), "") and value is not None)):
            return True
    return False


def as_json() -> Dict[str, Any]:
    """The families and the table as JSON-ready data."""
    def entry(h: Hook) -> Dict[str, Any]:
        return {"name": h.name, "kind": h.kind, "family": h.family, "title": h.title,
                "what": h.what, "enables": h.enables, "default": h.default,
                "versions": h.versions, "test": h.test, "guide": h.guide, "step": h.step,
                "works_by_default": h.works_by_default, "operations": list(h.operations),
                "methods": list(h.methods)}
    return {"schema": SCHEMA,
            "families": [{"name": f.name, "title": f.title, "order": f.order,
                          "enables": f.enables, "useful_for": f.useful_for, "guide": f.guide}
                         for f in families()],
            "hooks": [entry(h) for h in table()]}


def main(argv: Optional[Sequence[str]] = None) -> int:
    """``python -m langadapter.hooks --json``: print the hook table as JSON
    (``as_json``) and return 0, or print the usage and return 2 for other
    arguments."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["--json"]:
        print("usage: python -m langadapter.hooks --json", file=sys.stderr)
        return 2
    json.dump(as_json(), sys.stdout, indent=1)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
