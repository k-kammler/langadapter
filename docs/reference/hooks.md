# Hooks and facts

This page is generated from LangAdapter's hook table (`langadapter.hooks`).
[Add a language](../extending/add-a-language.md) explains each step.

A fact's or a hook's name is its label in the table, the check and the guides.
Your adapter defines the methods or attributes its entry's Write block shows,
and they usually carry the label as their name. Seven carry another name:

- `test_file_patterns` is written as the method `is_test_file`
- `kill_attribution` is written as the attribute `mutation_evidence_granularity`
- `check_syntax` is written as the method `validate_source`
- `validity_policy` is written as the methods `target_identity` and `analyze_validity`
- `execution_kinds` is written as the attributes `source_execution_kinds`, `test_execution_kinds` and `mutation_execution_kinds`
- `structural_metrics` is written as the methods `structural_parser_identity` and `optimization_metrics`
- `scoped_metrics` is written as the method `scoped_optimization_metrics`

An entry's line "The check counts these names" also lists methods and
attributes you need not write: for a fact or a hook without operations,
the check reads it present when your class defines one of them.

## Language facts

Name in a profile: `facts`

It makes possible: everything else to name, find and describe the language's files and tests

Useful for: every program that uses the adapter

### `language`

The language's name

- Kind: fact
- What: The registered name, such as javascript.
- It makes possible: naming and finding the language's files and tests
- Default: none: every adapter names its language
- Test it: The registry finds the adapter by this name.
- Guide: [The language's name](../extending/add-a-language.md#language-facts)

### `file_extensions`

The file extensions

- Kind: fact
- What: The file extensions of the language's source files, such as .js.
- It makes possible: naming and finding the language's files and tests
- Default: none: every adapter names at least one
- Test it: language_for(path) names the language.
- Guide: [The file extensions](../extending/add-a-language.md#language-facts)

### `display_name`

The name as people write it

- Kind: fact
- What: The language's name in messages and prompts, such as JavaScript.
- It makes possible: naming and finding the language's files and tests
- Default: the consumer's own, such as the name with a capital first letter
- Test it: The check shows it.
- Guide: [The name as people write it](../extending/add-a-language.md#language-facts)

### `runtime`

The program that runs the tests

- Kind: fact
- What: The program a project's tests run with, such as node.
- It makes possible: naming and finding the language's files and tests
- Default: none: the scaffold marks it to set
- Test it: Your adapter class's discover_runtime(root, {}) finds it on PATH.
- Guide: [The program that runs the tests](../extending/add-a-language.md#language-facts)

### `test_file_name`

How a test file is named

- Kind: fact
- What: A template for the test file of a source file, with {stem}, {Stem} (the stem with a capital first letter) and {ext}, such as {stem}.test.js.
- It makes possible: naming and finding the language's files and tests
- Default: test_{stem} with the language's file extension
- Test it: The check names the file for an example source.
- Guide: [How a test file is named](../extending/add-a-language.md#language-facts)

### `test_file_patterns`

Which files are test files

- Kind: fact
- What: File name patterns every test file matches, such as *.test.js.
- It makes possible: naming and finding the language's files and tests
- Operations built on it: sources.is_test_file, sources.project_source_files
- Default: none: write is_test_file
- Test it: is_test_file tells a test file from a source file.
- The check counts these names as the fact's: `is_test_file`
- Guide: [Which files are test files](../extending/add-a-language.md#language-facts)

### `test_id_text`

A test's id as text

- Kind: fact
- What: A test's id as the language writes it in kill matrices and records, such as demo.CalcTest.adds.
- It makes possible: naming and finding the language's files and tests
- Default: the TestId's own text: the file and the test's name
- Test it: The example's test ids are unique.
- Guide: [A test's id as text](../extending/add-a-language.md#language-facts)

### `test_id_prefix`

The prefix of a file's test ids

- Kind: fact
- What: The part of the ids of a file's tests before each test's name, such as demo.CalcTest.
- It makes possible: naming and finding the language's files and tests
- Default: the file's stem
- Test it: The example's test ids start with it.
- Guide: [The prefix of a file's test ids](../extending/add-a-language.md#language-facts)

### `file_rule`

A rule a file name follows

- Kind: fact
- What: The stem a file holding a source must have, such as its public class's name, and the source renamed to fit a given stem.
- It makes possible: naming and finding the language's files and tests
- Default: no rule
- Test it: A file that breaks the rule is named with the rule.
- The check counts these names as the fact's: [`file_rule`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.file_rule) and [`rename_file_unit`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.rename_file_unit)
- Guide: [A rule a file name follows](../extending/add-a-language.md#language-facts)

### `scratch_stem`

A test file's stem without a target

- Kind: fact
- What: The stem of a test file written for no particular target, such as Candidate.
- It makes possible: naming and finding the language's files and tests
- Default: the stem "candidate"
- Test it: The file is named by the test file name with this stem.
- Guide: [A test file's stem without a target](../extending/add-a-language.md#language-facts)

### `code_fence`

The code fence

- Kind: fact
- What: The language's name in a fenced code block, such as js.
- It makes possible: naming and finding the language's files and tests
- Default: the consumer's own, such as the language's name
- Test it: The check shows it.
- Guide: [The code fence](../extending/add-a-language.md#language-facts)

### `test_framework`

The test framework

- Kind: fact
- What: The framework the tests use, such as node:test.
- It makes possible: naming and finding the language's files and tests
- Default: the consumer's own, such as the general words "the test framework"
- Test it: language_facts names it.
- Guide: [The test framework](../extending/add-a-language.md#language-facts)

### `test_vocabulary`

The words for the language's tests

- Kind: fact
- What: The method test_vocabulary returns the language's words for its tests, by the entries of langadapter.vocabulary: how a test is declared and collected, fixtures, parametrization, the one-file rule, the import rule, the word for a load failure and more.
- It makes possible: naming and finding the language's files and tests
- Default: generic sentences from the facts above
- Test it: langadapter.vocabulary.problems finds no unknown entry and no text that is not a string.
- Guide: [The words for the language's tests](../extending/add-a-language.md#language-facts)

### `project_settings`

The project's settings

- Kind: fact
- What: The settings a project of the language takes, each with its name, purpose, kind (path, executable, choice or text), environment alias, discovery default, check and how a relative host path in it resolves (host_path).
- It makes possible: naming and finding the language's files and tests
- Default: no settings
- Test it: Each setting's check accepts its default.
- Guide: [The project's settings](../extending/add-a-language.md#language-facts)

### `ignored_dirs`

Directories that hold no sources

- Kind: fact
- What: Directories a search for source files skips, such as node_modules.
- It makes possible: naming and finding the language's files and tests
- Default: the usual version control, environment, build, cache and test directories
- Test it: The example's sources are found.
- Guide: [Directories that hold no sources](../extending/add-a-language.md#language-facts)

### `non_source_files`

Files that are not sources

- Kind: fact
- What: File names a search for source files leaves out although they carry the language's file extension, such as a build script.
- It makes possible: naming and finding the language's files and tests
- Default: no file left out by its name
- Test it: The example's sources are found.
- Guide: [Files that are not sources](../extending/add-a-language.md#language-facts)

### `reuses_bytecode`

Compiled modules shared by copies

- Kind: fact
- What: Whether a measurement copy of a project may reuse the compiled modules of the runs in earlier copies.
- It makes possible: naming and finding the language's files and tests
- Default: off
- Test it: The check shows it.
- Guide: [Compiled modules shared by copies](../extending/add-a-language.md#language-facts)

### `copies_external_suite`

A suite outside the project copied with it

- Kind: fact
- What: Whether a measurement copy of a project takes a suite directory outside the project with it, so that the paths a run prints are the ones a run in place printed.
- It makes possible: naming and finding the language's files and tests
- Default: off
- Test it: The check shows it.
- Guide: [A suite outside the project copied with it](../extending/add-a-language.md#language-facts)

### `provides_timeout_journal`

The per-test timeout journal

- Kind: fact
- What: Whether run_suite takes timeout_journal, the per-test journal from which a run that a timeout or a crash ended names the test that was running. An adapter that refuses it turns this off. An adapter whose answer depends on the project sets it on the instance, and a consumer reads it on the adapter.
- It makes possible: naming and finding the language's files and tests
- Default: on: run_suite takes the timeout journal
- Test it: check_adapter runs the example with the journal when this is on.
- Guide: [The per-test timeout journal](../extending/add-a-language.md#language-facts)

### `seeds_delivered_runs`

A session seed for runs as delivered

- Kind: fact
- What: Whether run_as_delivered takes session_seed, so that a consumer may run a suite as delivered with a fixed seed as its normal run. An adapter whose runs as delivered take the seed turns this on. An adapter whose answer depends on the project sets it on the instance, and a consumer reads it on the adapter.
- It makes possible: naming and finding the language's files and tests
- Default: off
- Test it: The check shows it.
- Guide: [A session seed for runs as delivered](../extending/add-a-language.md#language-facts)

### `source_merge_separator`

The separator line of a merge

- Kind: fact
- What: The comment prefix of the line merge_sources puts between two folded test files, such as //.
- It makes possible: naming and finding the language's files and tests
- Default: none declared: each merge needs the caller's separator line
- Test it: Two example test files fold with the separator line between them.
- Guide: [The separator line of a merge](../extending/add-a-language.md#language-facts)

### `validity_policy_version`

The version of the validity policy

- Kind: fact
- What: The version of the generated-test validity policy whose findings analyze_validity reports, which callers record with each run.
- It makes possible: naming and finding the language's files and tests
- Default: no version: a run records none
- Test it: The check shows it.
- Guide: [The version of the validity policy](../extending/add-a-language.md#language-facts)

## Run the tests

Name in a profile: `run tests`

It makes possible: running a project's test suite and reading each test's outcome

Useful for: any program that runs a project's tests

### `run_suite`

Run the tests

- Kind: hook
- What: Run a suite and return each test's outcome and the run's outcome: it loads, it does not load, it has no tests, it timed out, a tool failed, its setup is missing, or it is unsupported.
- It makes possible: running a suite and listing its failing tests, and telling a suite that does not load from one that lacks its setup
- Operations built on it: tests.run_suite, tests.collect_suite
- Default: none: write it
- Test it: A passing and a failing test read passed and failed, and a test file that does not load reads does not load.
- Methods to write: `run_suite`
- The check counts these names as the hook's: `run_suite`, [`collection_text`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.collection_text), [`listed_failures`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.listed_failures) and `collect_suite`
- Guide: [Run the tests](../extending/add-a-language.md#run-the-tests)

Write:

```python
def run_suite(self, request: SuiteRunRequest) -> SuiteRun:
    ...
```

### `test_units`

Find and remove tests

- Kind: hook
- What: Find each test in a test file, by hand or with the tree-sitter toolkit (langadapter.treesitter), so that one test can be removed, counted or listed.
- It makes possible: removing a failing test instead of the whole file, and the test count
- Operations built on it: sources.prune_tests, sources.duplicate_test_names, sources.test_metrics
- Default: none: write it, by hand or with the tree-sitter toolkit
- Test it: Removing one named test leaves the others, and the count drops by one.
- Methods to write: `test_units`
- The check counts these names as the hook's: `prune_tests` and `test_metrics`
- Guide: [Find and remove tests](../extending/add-a-language.md#find-and-remove-tests)

Write:

```python
def test_units(self, source: str):
    ...
```

### `project`

Resolve the project

- Kind: hook
- What: Resolve a project's build, such as its dependencies and class path, and say why it does not resolve.
- It makes possible: refusing a project that does not build before any test runs
- Default: every project counts as resolved
- Test it: A project whose build fails reads not resolved, with the reason.
- Methods to write: `project`
- Guide: [Resolve the project](../extending/add-a-language.md#resolve-the-project)

Write:

```python
def project(self):
    ...
```

### `discover_runtime`

Find the runtime

- Kind: hook
- What: Find the program that runs the tests, and the tools, for a project, and describe what its settings select without checking it (describe_runtime).
- It makes possible: a project's context without settings the user must write
- Default: the runtime fact found on PATH
- Versions: LangAdapter reads no runtime version by default: a run records the context's runtime_version. An adapter whose hooks need the version reads it itself, such as from the runtime's --version.
- Test it: Your adapter class's discover_runtime(root, {}) finds the example project's runtime.
- Methods to write: `discover_runtime`
- The check counts these names as the hook's: [`discover_runtime`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.discover_runtime), [`describe_runtime`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.describe_runtime) and [`context_environment`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.context_environment)
- Guide: [Find the runtime](../extending/add-a-language.md#find-the-runtime)

Write:

```python
@classmethod
def discover_runtime(cls, root, values, *, container=False, environment=None,
                     resolve_path=None) -> RuntimeChoice:
    ...
```

### `provision_runtime`

Provision the runtime

- Kind: hook
- What: Make an environment for a requested runtime version, with the project's requirements and the tools the adapter pins, reused while those inputs stay the same.
- It makes possible: a project run on a runtime version the machine does not have yet
- Default: none: a run that asks for another runtime version is refused
- Versions: The pinned tools follow the requested version.
- Test it: A requested version gives an environment that runs the example once and is reused.
- Methods to write: `provision_runtime`
- Guide: [Provision the runtime](../extending/add-a-language.md#find-the-runtime)

Write:

```python
@classmethod
def provision_runtime(cls, request, *, store, name, environment=None, log=None) -> str:
    ...
```

### `setup_questions`

Ask the setup's questions

- Kind: hook
- What: The questions a consumer's setup asks for a project of the language, with each one's default, reason, choices and check and what is shown after an answer, and the check of a setting set without the questions (setup_problem).
- It makes possible: a setup that finds the runtime and the build tools and checks them as it asks
- Default: each declared setting asked with its purpose and checked by its kind
- Test it: The example project's questions are asked and their answers checked.
- Methods to write: `setup_questions`
- The check counts these names as the hook's: [`setup_questions`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.setup_questions) and [`setup_problem`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.setup_problem)
- Guide: [Ask the setup's questions](../extending/add-a-language.md#find-the-runtime)

Write:

```python
@classmethod
def setup_questions(cls, project_dir, probe, *, consumer="the caller"):
    yield SetupQuestion(f"{cls.language}.runtime", "The program that runs the tests",
                        setting="runtime", default=probe.which(cls.runtime),
                        check=lambda answer: None if probe.is_file(answer) else "no such file")
```

### `language_facts`

Describe the language

- Kind: hook
- What: Return the language's LanguageFacts: its name, the test framework, the code fence, the runtime's version and the language level, the test file, test function and test class patterns, the per-test timeout marker, whether a reduction keeps unmatched evidence and the patterns of the warnings that mark a test leaving shared state behind.
- It makes possible: prompts and records that name the language and its tests
- Default: none: a program that writes prompts cannot name the language's tests
- Test it: language_facts names the example's test framework.
- Methods to write: `language_facts`
- Guide: [Describe the language](../extending/add-a-language.md#describe-the-language)

Write:

```python
def language_facts(self) -> la.LanguageFacts:
    ...
```

### `capabilities`

Declare capabilities

- Kind: hook
- What: Report what the project can run: its runtime, its tools and each capability of langadapter.capabilities.CAPABILITIES, available, missing setup or unsupported, with the reason.
- It makes possible: a readiness report, and programs that run a project only when its tools are there
- Default: none: the report says readiness probing is not implemented and lists no capability
- Test it: adapter.probe_capabilities() reports the example's suite runs available.
- Methods to write: `capabilities`
- Guide: [Declare capabilities](../extending/add-a-language.md#capabilities)

Write:

```python
def capabilities(self) -> la.CapabilityReport:
    ...
```

### `execution_setup_issue`

Name missing setup

- Kind: hook
- What: Name a prerequisite of running the project's tools that is known to be missing without starting a tool, such as a setting a container run needs.
- It makes possible: missing setup reported with its reason before any tool starts
- Default: no prerequisite is known to be missing
- Test it: With the prerequisite missing, a run of the example reports missing setup with the reason.
- Methods to write: `execution_setup_issue`
- Guide: [Name missing setup](../extending/add-a-language.md#capabilities)

Write:

```python
def execution_setup_issue(self) -> str:
    ...
```

### `operation_context_issue`

Refuse an operation in a context

- Kind: hook
- What: Say why an operation cannot run in the project's context, such as a kind of execution or a test framework the adapter does not take.
- It makes possible: an unsupported operation reported with its reason, not as not implemented
- Default: every operation the adapter implements runs in each context it declares
- Test it: In the refused context, the operation reads unsupported with the reason.
- Methods to write: `operation_context_issue`
- Guide: [Refuse an operation in a context](../extending/add-a-language.md#capabilities)

Write:

```python
def operation_context_issue(self, name: str) -> str:
    ...
```

### `operation_readiness`

Give each operation's readiness

- Kind: hook
- What: Give an operation's readiness from the probed capability report, as a Capability named like the operation, or None when the probe did not check it.
- It makes possible: a probed report that says which operations can run
- Default: the probed readiness of each operation stays unknown
- Test it: With a tool the probe found missing, probe_capabilities reports the operations that need it as missing setup.
- Methods to write: `operation_readiness`
- Guide: [Give each operation's readiness](../extending/add-a-language.md#capabilities)

Write:

```python
def operation_readiness(self, name: str, report: la.CapabilityReport):
    ...
```

### `request_options`

Choose run options

- Kind: hook
- What: The request fields the language adds for each kind of run a consumer asks for (RUN_PURPOSES: a run of the tests, failing tests, per-test evidence, a run as delivered, timing, a coverage matrix, a collection, the project's own run), such as its runner's options.
- It makes possible: runs set up the way the language's tools need
- Default: no options
- Test it: Each kind of run gets the options it is given.
- Methods to write: `request_options`
- Guide: [Choose run options](../extending/add-a-language.md#run-options)

Write:

```python
def request_options(self, purpose: str, *, target=None, traceback="short",
                    stop_at_first_failure=False, record_leaks=False,
                    session_seed=None, reverse_order=False,
                    leave_out_timing_plugins=False) -> dict:
    ...
```

### `test_files`

Collect a directory's test files

- Kind: hook
- What: Replace a directory by the test files the language's runner collects in it.
- It makes possible: running a directory of tests as the runner would
- Default: the files the test file name names
- Test it: A directory of the example gives its test files.
- Methods to write: `test_files`
- Guide: [Collect a directory's test files](../extending/add-a-language.md#run-the-tests)

Write:

```python
def test_files(self, paths) -> list:
    ...
```

### `project_test_files`

List the project's test files

- Kind: hook
- What: The test files the language's runner collects in the project as the project configures them.
- It makes possible: the project's own suite as its runner would run it
- Default: test_files of the project root
- Test it: The example's project gives its test files.
- Methods to write: `project_test_files`
- Guide: [List the project's test files](../extending/add-a-language.md#run-the-tests)

Write:

```python
def project_test_files(self) -> list:
    ...
```

### `setup_files`

Setup files beside the tests

- Kind: hook
- What: The files the runner loads beside a suite's test files, such as pytest's conftest.py, and the setup file of a prepared suite.
- It makes possible: a copy of a suite that runs with the setup its tests need
- Default: none
- Test it: A suite copied with its setup files runs as it did in place.
- Methods to write: `setup_files` and `shared_setup_files`
- Guide: [Setup files beside the tests](../extending/add-a-language.md#run-the-tests)

Write:

```python
def setup_files(self, root: str, test_paths) -> tuple:
    ...
def shared_setup_files(self, directory: str) -> tuple:
    ...
```

### `run_counts`

Count a run

- Kind: hook
- What: A run's collected, passed, failed and errored tests as the runner counts them.
- It makes possible: records whose counts match the runner's own
- Default: the run's counts
- Test it: A run of the example counts its tests.
- Methods to write: `run_counts`
- Guide: [Count a run](../extending/add-a-language.md#run-the-tests)

Write:

```python
def run_counts(self, run) -> dict:
    ...
```

## Measure coverage

Name in a profile: `coverage`

It makes possible: the line and branch coverage of a source file under a suite

Useful for: judging how much of the code the tests reach

### `coverage`

Measure coverage

- Kind: hook
- What: Coverage is part of run_suite: when the request names a coverage_target, the run returns that file's covered and missed lines and branches.
- It makes possible: the line and branch coverage of a source file
- Operations built on it: tests.run_suite
- Default: none: write it
- Test it: A run of the example with its source as the target reports a covered line.
- Guide: [Measure coverage](../extending/add-a-language.md#measure-coverage)

Write:

```python
# Coverage is part of run_suite: when request.coverage_target names a
# source file, the returned SuiteRun's coverage is a CoverageObservation
# of that file's statements and covered lines, branches and taken branches.
```

### `coverage_texts`

Coverage as text for records

- Kind: hook
- What: A run's covered lines and missed branches as text, as the coverage tool counts them.
- It makes possible: records that read as the language's coverage tool reports
- Default: the covered lines, each missed branch as source->destination
- Test it: A run of the example writes its covered lines.
- Methods to write: `coverage_texts`
- Attributes to write: `coverage_matrix_in_place`
- Guide: [Coverage as text for records](../extending/add-a-language.md#measure-coverage)

Write:

```python
def coverage_texts(self, coverage) -> dict:
    ...
# Where a coverage matrix is measured in the project itself, not in a
# prepared copy:
coverage_matrix_in_place = True
```

## Run mutation testing

Name in a profile: `mutation`

It makes possible: each mutant's status under a suite, with its change and location

Useful for: judging whether the tests notice changes to the code

### `open_mutation_session`

Run mutation testing

- Kind: hook
- What: Open a session that runs the mutation tool and returns each mutant's id, location, change text and status.
- It makes possible: each mutant's status, which a consumer reads mutant by mutant, not only as a score
- Operations built on it: mutations.engines, mutations.operators, mutations.open_session, mutations.run, mutations.per_suite
- Default: none: write it
- Test it: A session on the example gives a killed and a surviving mutant.
- Methods to write: `mutation_operators` and `open_mutation_session`
- Attributes to write: `native_mutation_engines`
- The check counts these names as the hook's: `open_mutation_session`, [`mutation_catalogue`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.mutation_catalogue), [`named_operators`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.named_operators), [`mutation_session_attempts`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.mutation_session_attempts), [`mutation_dir_refusal`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.mutation_dir_refusal), [`mutation_workers_refusal`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.mutation_workers_refusal) and [`own_mutant_timeout_rule`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.own_mutant_timeout_rule)
- Guide: [Run mutation testing](../extending/add-a-language.md#run-mutation-testing)

Write:

```python
native_mutation_engines = ("the-tool",)
def mutation_operators(self, engine: str) -> OperatorCatalogue:
    ...
def open_mutation_session(self, engine: str, workers: int = 1, **options) -> MutationSession:
    ...
```

### `validate_mutation_session_options`

Check engine options

- Kind: hook
- What: Check a mutation session's options beyond workers, such as a distributor or settings, raising InvalidRequest for one the engine does not accept.
- It makes possible: engine options beyond workers
- Default: a session with such an option is refused
- Test it: An option the engine takes opens a session, and one it does not take is refused.
- Methods to write: `validate_mutation_session_options`
- Guide: [Check engine options](../extending/add-a-language.md#run-mutation-testing)

Write:

```python
def validate_mutation_session_options(self, engine, *, workers, distributor,
                                      allow_fewer_workers, settings):
    ...
```

## Attribute kills to tests

Name in a profile: `kill attribution`

It makes possible: which tests kill each mutant

Useful for: reducing a suite while keeping every kill

### `kill_attribution`

Attribute kills to tests

- Kind: hook
- What: Name, for each killed mutant, the tests that kill it.
- It makes possible: reductions that keep every kill
- Default: no attribution
- Test it: A mutant killed by one test names that test.
- Attributes to write: `mutation_evidence_granularity`
- Guide: [Attribute kills to tests](../extending/add-a-language.md#kill-attribution)

Write:

```python
# Name, for each killed mutant, the tests that kill it
# (Mutant.killing_tests), and declare it:
mutation_evidence_granularity = ("aggregate", "per_mutant", "per_test")
```

### `kill_tokens`

Attribute stored kills

- Kind: hook
- What: Turn a stored kill matrix's named killers into each test's kill tokens, and a pool's kills into the tokens of its suites' tests.
- It makes possible: reductions and pools that keep every kill
- Default: the killing tests each row records (Mutant.killing_tests)
- Test it: A killer that names one test gives that test its mutant's token.
- Methods to write: `kill_tokens` and `pool_kills`
- Guide: [Attribute stored kills](../extending/add-a-language.md#kill-attribution)

Write:

```python
def kill_tokens(self, kill_matrix, test_ids):
    ...
def pool_kills(self, kill_matrix, file_to_index, test_ids_by_index) -> dict:
    ...
```

## Repair loading

Name in a profile: `load repair`

It makes possible: keeping a test file whose loading fails at one unit, by removing that unit

Useful for: keeping generated or edited tests that fail to load

### `prune_collection_breakers`

Repair loading

- Kind: hook
- What: Remove the units that keep a test file from loading: the shared loop (templates.repair_loading) with the language's load step (collection or compilation, reporting the failing lines) and its removal of the unit around each line, a statement, a member or a line.
- It makes possible: the shared loop that removes the units that keep a file from loading
- Operations built on it: sources.prune_collection_breakers
- Default: no load repair
- Test it: A file with one unit that does not load keeps its other tests.
- Methods to write: `prune_collection_breakers`
- Guide: [Repair loading](../extending/add-a-language.md#repair-loading)

Write:

```python
def prune_collection_breakers(self, source: str, *, timeout_s: float) -> SourceResult:
    ...
```

### `collection_unit`

The unit load repair removes

- Kind: fact
- What: The unit prune_collection_breakers removes, in words, such as the smallest declaration around a line javac reports an error on.
- It makes possible: a consumer's record of what a repair removed
- Default: none: an adapter that repairs loading declares it
- Test it: The example's breaker is removed as that unit.
- Guide: [The unit load repair removes](../extending/add-a-language.md#repair-loading)

## Remove failing imports

Name in a profile: `import pruning`

It makes possible: removing the imports of a test file that do not resolve in the project

Useful for: keeping generated tests whose fault is an import of something the project lacks

### `prune_unresolvable_imports`

Remove failing imports

- Kind: hook
- What: Remove the imports of a test file that do not resolve in the project: the shared loop (templates.prune_units) with the language's search for the failing imports and their removal.
- It makes possible: keeping a test file whose fault is an import of something the project lacks
- Operations built on it: sources.prune_unresolvable_imports, sources.imports, sources.import_path_candidates
- Default: no import pruning
- Test it: A file that imports a missing module keeps its tests, without that import.
- Methods to write: `prune_unresolvable_imports`
- Guide: [Remove failing imports](../extending/add-a-language.md#remove-failing-imports)

Write:

```python
def prune_unresolvable_imports(self, source: str) -> SourceResult:
    ...
```

## Check and repair syntax

Name in a profile: `syntax repair`

It makes possible: telling a source the language's parser accepts from one it rejects, and a bounded repair of one it rejects

Useful for: rejecting broken files early and keeping generated tests with a local syntax error

### `check_syntax`

Check the syntax

- Kind: hook
- What: Tell a source the language parses from one it rejects, with the first error's place.
- It makes possible: rejecting a broken file before running it
- Operations built on it: sources.check_structure, sources.validate_source, sources.finalize_source
- Default: none: write validate_source, which runs the project's runtime, or check_structure, a parse with the grammar alone
- Test it: A file with a syntax error reads invalid, with its line.
- Methods to write: `validate_source`
- The check counts these names as the hook's: `validate_source`, `check_structure` and `finalize_source`
- Guide: [Check the syntax](../extending/add-a-language.md#check-syntax)

Write:

```python
def validate_source(self, source: str) -> SourceValidation:
    ...
```

### `salvage_syntax`

Repair syntax

- Kind: hook
- What: Repair a source's syntax errors line by line: the shared loop (templates.repair_syntax) with the language's validator and local fix.
- It makes possible: the shared loop that repairs a source line by line
- Operations built on it: sources.salvage_syntax
- Default: no syntax repair
- Test it: A source with one bad line parses after the repair.
- Methods to write: `salvage_syntax`
- The check counts these names as the hook's: `salvage_syntax` and [`salvage_report`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.salvage_report)
- Guide: [Repair syntax](../extending/add-a-language.md#repair-syntax)

Write:

```python
def salvage_syntax(self, source: str) -> SourceResult:
    ...
```

## Remove whole-file skips

Name in a profile: `skip removal`

It makes possible: removing statements that skip a whole test file

Useful for: making the tests of a file run instead of skipping

### `strip_module_level_skips`

Remove whole-file skips

- Kind: hook
- What: Remove the statements or annotations that skip a whole test file.
- It makes possible: removing them so that the file's tests run
- Operations built on it: sources.strip_module_level_skips
- Default: no skip removal
- Test it: A file with a whole-file skip runs its tests after the removal.
- Methods to write: `strip_module_level_skips`
- The check counts these names as the hook's: `strip_module_level_skips` and [`skip_removal`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.skip_removal)
- Guide: [Remove whole-file skips](../extending/add-a-language.md#remove-whole-file-skips)

Write:

```python
def strip_module_level_skips(self, source: str) -> SourceResult:
    ...
```

## Name the import line

Name in a profile: `import guidance`

It makes possible: the line a test of a source file uses to import it

Useful for: writing a first test for a source file

### `import_statement`

Name the import line

- Kind: hook
- What: The line a test uses to import a given source file, or a sentence that says none is needed (`none; ...`).
- It makes possible: a first test that imports its target correctly
- Operations built on it: sources.import_statement
- Default: the vocabulary's import rule only
- Test it: The line imports the example's source, or the sentence says that no import line is needed.
- Methods to write: `import_statement`
- Guide: [Name the import line](../extending/add-a-language.md#import-line)

Write:

```python
@classmethod
def import_statement(cls, target: str, root: str) -> str:
    ...
```

## Merge test files

Name in a profile: `merging`

It makes possible: folding several test files into one, with their name collisions resolved

Useful for: combining suites into one

### `merge_sources`

Merge test files

- Kind: hook
- What: Fold one test file into another and resolve the names both declare. `resolve_collisions` returns a `SourceObservation`, completed with a `CollisionResolution` and its parser, or with `infrastructure_failure`, `timeout` or `missing_setup` and a reason.
- It makes possible: one suite from several
- Operations built on it: sources.merge_sources, sources.resolve_collisions, sources.merge_collisions, sources.normalize_bindings, sources.normalize_shadowed, sources.stage_pool
- Default: no merging
- Test it: Two example test files fold into one whose tests all run.
- Methods to write: `merge_sources`
- The check counts these names as the hook's: `merge_sources`, `resolve_collisions`, [`collision_resolution`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.collision_resolution), [`collision_dependents`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.collision_dependents), `merge_collisions`, `normalize_bindings`, `normalize_shadowed` and `stage_pool`
- Guide: [Merge test files](../extending/add-a-language.md#merge-test-files)

Write:

```python
def merge_sources(self, first: str, second: str, *, separator: str) -> SourceResult:
    ...
```

## Collect per-test evidence

Name in a profile: `per-test evidence`

It makes possible: each test's own coverage

Useful for: reducing a suite to the fewest tests that cover what it covered

### `per_test_evidence`

Collect per-test evidence

- Kind: hook
- What: Return each test's own coverage.
- It makes possible: the reductions that keep what a suite covered
- Operations built on it: tests.per_test_evidence
- Default: no per-test evidence
- Test it: Each example test reports the lines it covers.
- Methods to write: `per_test_evidence`
- Guide: [Collect per-test evidence](../extending/add-a-language.md#per-test-evidence)

Write:

```python
def per_test_evidence(self, request):
    ...
```

### `test_evidence_granularity`

The granularity of test evidence

- Kind: fact
- What: The granularity of the evidence each test operation reports, as pairs of the operation's name and its granularities: aggregate for the suite as a whole, per_test for each test, such as ("per_test_evidence", ("per_test",)).
- It makes possible: an operation report that says which operations report evidence per test
- Default: no granularity: the operation report gives each test operation none
- Test it: operation_capabilities() gives each declared operation its granularity.
- Guide: [The granularity of test evidence](../extending/add-a-language.md#per-test-evidence)

## Prepare a suite

Name in a profile: `instrumentation`

It makes possible: the setup a suite runs with, and warnings of tests that leave state behind

Useful for: isolated measurements and finding tests that leak state

### `prepare_suite`

Prepare a suite

- Kind: hook
- What: Write the setup a suite directory needs before its tests run, such as an import shim, and on request the configuration that isolates it.
- It makes possible: isolated, repeatable measurements
- Default: no setup written
- Test it: A prepared suite runs.
- Methods to write: `prepare_suite`
- Guide: [Prepare a suite](../extending/add-a-language.md#prepare-a-suite)

Write:

```python
def prepare_suite(self, suite_root: str, *,
                  isolation_config: bool = False):
    ...
```

### `unsound_test_patterns`

Warn of tests that leak state

- Kind: fact
- What: In LanguageFacts.unsound_test_patterns, the regular expressions of the warnings that mark a test leaving shared state behind. A suite run reports each such warning in its diagnostics, naming the test that left it.
- It makes possible: removing passing tests that leak state
- Default: no warnings read
- Test it: A test that leaks state is named.
- Guide: [Warn of tests that leak state](../extending/add-a-language.md#unsound-test-warnings)

### `order_dependent_mock_warnings`

Warn of order-dependent mocks

- Kind: hook
- What: Name the tests that assert on a named element of a mock's side_effect list, whose order a scheduler in the code under test can change.
- It makes possible: advice on tests that pass in one scheduling order only
- Default: no advice
- Test it: A test that asserts on one element of a mock's side_effect list is named.
- Methods to write: `order_dependent_mock_warnings`
- Guide: [Warn of order-dependent mocks](../extending/add-a-language.md#unsound-test-warnings)

Write:

```python
def order_dependent_mock_warnings(self, source: str) -> list:
    ...
```

## Run as delivered

Name in a profile: `delivered runs`

It makes possible: running a suite as a user who copied it would

Useful for: checking that a suite works where it is delivered

### `run_as_delivered`

Run as delivered

- Kind: hook
- What: Run each test file alone, as a user who copied it would, all of them within the request's one timeout.
- It makes possible: the check that a suite works where it is delivered
- Operations built on it: tests.run_as_delivered
- Default: no delivered runs
- Test it: The example's test file passes as delivered.
- Methods to write: `run_as_delivered`
- The check counts these names as the hook's: `run_as_delivered` and [`delivered_order_refusal`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.delivered_order_refusal)
- Guide: [Run as delivered](../extending/add-a-language.md#run-as-delivered)

Write:

```python
def run_as_delivered(self, request, *, sources=None):
    ...
```

### `delivered_case`

A delivered test as text for records

- Kind: hook
- What: A test of a run as delivered: its id, its name with its parameters, and its outcome.
- It makes possible: records of delivered runs in the language's own words
- Default: the test's id, its name and parameters, its outcome
- Test it: A delivered run of the example names each test once.
- Methods to write: `delivered_case`
- Guide: [A delivered test as text for records](../extending/add-a-language.md#run-as-delivered)

Write:

```python
def delivered_case(self, test, outcome: str) -> tuple:
    ...
```

## Screen generated tests

Name in a profile: `validity screening`

It makes possible: findings on tests that read the implementation instead of calling it

Useful for: keeping generated tests honest

### `validity_policy`

Screen generated tests

- Kind: hook
- What: Report the places where a test reads the implementation's source or internals instead of calling it.
- It makes possible: a gate that keeps such tests out
- Operations built on it: sources.target_identity, sources.analyze_validity, sources.screen_source
- Default: no screening
- Test it: A test that reads the source file is reported.
- Methods to write: `target_identity` and `analyze_validity`
- The check counts these names as the hook's: `analyze_validity`, `target_identity` and `screen_source`
- Guide: [Screen generated tests](../extending/add-a-language.md#validity-policy)

Write:

```python
def target_identity(self, target: str) -> TargetIdentity:
    ...
def analyze_validity(self, source: str, target) -> PolicyReport:
    ...
```

## Time a suite

Name in a profile: `timing`

It makes possible: the time a suite takes, repeated, and warmed where the runtime warms up

Useful for: choosing between suites by speed and timing records

### `benchmark`

Time a suite

- Kind: hook
- What: Run a suite repeatedly and return its times, warmed where the runtime warms up.
- It makes possible: choosing between suites by speed
- Operations built on it: tests.benchmark, tests.benchmark_warmed
- Default: no timing
- Test it: Repeated runs of the example give times.
- Methods to write: `benchmark`
- The check counts these names as the hook's: `benchmark` and `benchmark_warmed`
- Guide: [Time a suite](../extending/add-a-language.md#timing)

Write:

```python
def benchmark(self, request):
    ...
```

## Declare versions

Name in a profile: `versions`

It makes possible: notes and refusals by the version of the runtime and the tools

Useful for: telling users which versions are tested

### `support`

Declare versions

- Kind: fact
- What: The runtime versions the adapter is tested and expected to work with, and the support of named tools. The Python and Java adapters refuse an older version with the reason. Another adapter refuses one only when its version profile's refusal() gives a reason (choose_profile).
- It makes possible: notes and refusals by version
- Default: undeclared: every version is untested and never refused
- Versions: This is the version declaration.
- Test it: support.classify(version) reads tested for a tested version. The adapter's profile() reads it for the runtime its choose_profile found. Where the profile refuses older versions, an older one is refused with the reason.
- Guide: [Declare versions](../extending/add-a-language.md#versions)

### `choose_profile`

Refuse runtime versions

- Kind: hook
- What: Return the VersionProfile (langadapter.versions) of the runtime version the adapter found, from a subclass whose refusal() gives the reason for a version the adapter does not support.
- It makes possible: refusing a runtime the adapter does not support, with the reason
- Default: the base profile, which reads no runtime and refuses no version
- Test it: A runtime older than the adapter supports is refused with the reason.
- Methods to write: `choose_profile`
- Guide: [Refuse runtime versions](../extending/add-a-language.md#versions)

Write:

```python
def choose_profile(self):
    ...
```

## Run in containers

Name in a profile: `containers`

It makes possible: runs inside a container the user prepared

Useful for: running projects whose tools live in a container

### `execution_kinds`

Run in containers

- Kind: fact
- What: Whether source operations, test runs and mutation sessions can run in a container the user prepared, each by its own fact.
- It makes possible: runs inside a container
- Default: local runs only
- Test it: A container context runs the example's suite.
- Attributes to write: `source_execution_kinds`, `test_execution_kinds` and `mutation_execution_kinds`
- Guide: [Run in containers](../extending/add-a-language.md#containers)

Write:

```python
# Runs in a container the user prepared:
source_execution_kinds = ("local", "container")
test_execution_kinds = ("local", "container")
mutation_execution_kinds = ("local", "container")
```

## Structural metrics

Name in a profile: `structural metrics`

It makes possible: sizes and complexity of sources and tests, not the test count

Useful for: describing sources and suites

### `structural_metrics`

Structural metrics

- Kind: hook
- What: Sizes and complexity of a source, per file and per function, and the names and fingerprints of its definitions.
- It makes possible: describing sources and suites
- Operations built on it: sources.metrics, sources.skeleton, sources.same_skeleton, sources.function_fingerprints
- Default: no structural metrics
- Test it: The example's source has a size.
- Methods to write: `structural_parser_identity` and `optimization_metrics`
- The check counts these names as the hook's: `optimization_metrics`, `structural_parser_identity`, `skeleton`, `same_skeleton` and `function_fingerprints`
- Guide: [Structural metrics](../extending/add-a-language.md#structural-metrics)

Write:

```python
# The metrics register only with both methods. optimization_metrics returns
# exactly the keys cyclomatic_sum, loop_count, loop_max_nesting,
# recursive_function_count, function_count, class_count, io_call_count and
# nloc, each a whole number, or None where the parser cannot read the source.
def structural_parser_identity(self) -> str:
    ...
def optimization_metrics(self, source: str) -> dict:
    ...
```

## Metric providers

Name in a profile: `metric providers`

It makes possible: further metrics from the language package or from other packages, per file and per scope

Useful for: analysis tools that add their own metrics

### `scoped_metrics`

Metrics per function

- Kind: hook
- What: The structural metrics per function, with each function's own complexity, beside the file's. It takes effect with the two structural metrics methods. Metrics of registered providers need no hook.
- It makes possible: analysis tools that add their own metrics
- Default: no scoped metrics
- Test it: A function of the example's source has its own complexity.
- Methods to write: `scoped_optimization_metrics`
- Guide: [Metrics per function](../extending/add-a-language.md#metric-providers)

Write:

```python
def scoped_optimization_metrics(self, source: str):
    ...
```

### `metric_providers`

Metric providers

- Kind: hook
- What: The language's own metric providers, which every adapter object of the language registers beside the structural provider. A name already registered is refused.
- It makes possible: metrics a language package adds for all its adapters
- Default: no metrics of its own
- Test it: A metric of the language's provider is listed and measured.
- Methods to write: `metric_providers`
- Guide: [Metric providers](../extending/add-a-language.md#metric-providers)

Write:

```python
def metric_providers(self):
    return ()
```

## Code smells

Name in a profile: `code smells`

It makes possible: a count of a linter's findings in sources and tests

Useful for: judging the style of generated code

### `code_smells`

Code smells

- Kind: hook
- What: Count a linter's findings in given files.
- It makes possible: judging the style of generated code
- Operations built on it: sources.code_smells
- Default: no smell count
- Test it: A file with one finding counts one.
- Methods to write: `code_smells`
- Guide: [Code smells](../extending/add-a-language.md#code-smells)

Write:

```python
def code_smells(self, paths):
    ...
```

## Call statistics

Name in a profile: `call statistics`

It makes possible: estimates of how often the rest of the project calls a file's functions

Useful for: impact analysis, review priorities and ranking code by its reach

### `call_statistics`

Call statistics

- Kind: hook
- What: Count a source's call sites by the called name. The call statistics of a file count them in the project's other source files for each function the scoped metrics report.
- It makes possible: estimating how widely a file's functions are used
- Operations built on it: sources.call_statistics
- Default: no call statistics
- Test it: A call of a function of the example's source in another file counts.
- Methods to write: `call_statistics`
- Guide: [Call statistics](../extending/add-a-language.md#call-statistics)

Write:

```python
def call_statistics(self, source: str):
    ...
```

## Profile a suite

Name in a profile: `profiling`

It makes possible: where a suite's run spends its time, by function and file

Useful for: performance work, ranking code by measured cost and choosing the tests that exercise a function

### `profile_suite`

Profile a suite

- Kind: hook
- What: Run a suite once under the language's profiler and return the time each function it entered took, with the profiler's name, version and kind.
- It makes possible: finding the functions a suite spends its time in
- Operations built on it: tests.profile_suite
- Default: no suite profile
- Test it: A profile of the example's suite names a function of its source.
- Methods to write: `profile_suite`
- Guide: [Profile a suite](../extending/add-a-language.md#profiling)

Write:

```python
def profile_suite(self, request):
    ...
```
