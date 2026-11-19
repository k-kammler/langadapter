# Add a language

This guide adds a programming language to LangAdapter, step by step. You
write one Python file: an adapter class for your language, which runs the
language's own tools (its test runner, coverage tool and mutation tool) and
reports what they found. Consumers, the programs that use LangAdapter such
as Testuary, then work with your language without code of their own for it.

Each step says what to do, what it makes possible, whether it has a default,
how versions bear on it and how to test it. The steps follow the usual order
of building an adapter. Each consumer says which steps it needs:
[Who needs what](who-needs-what.md) explains how, and
`langadapter check <language> --extension <file> --for <consumer>` shows it for
your adapter. For Testuary,
read [Make a language ready for Testuary](https://k-kammler.github.io/testuary/extending/languages/)
after this guide.

[How the parts fit](architecture.md) shows where your adapter sits, and its
[Words](architecture.md#words) define the four words this guide uses: fact,
hook, family and fallback. A hook's method or attribute usually carries the
hook's name. [Hooks and facts](../reference/hooks.md) names the ones written
under another name and lists each one's methods and attributes to write.
A block's line "The check counts these names as the hook's" also names
methods and attributes you need not write: for a fact or a hook without
operations, the check reads it present when your class defines one of
them.

## Before you start

You need Python 3.10 or newer, and your language's own tools installed where
your projects run them: a test runner that reports each test's outcome, a
coverage tool that reports lines and branches, and a mutation tool that
reports each mutant's status. LangAdapter installs none of them. It runs
them and reads their reports. A language without one of these tools can
bring its own, as the Toy brings its own mutation engine in step 4 of the
tutorial. A tool you write as a script lies beside your adapter's file, and
your adapter finds it from its own place, such as
`Path(__file__).with_name("myrun.py")`: a file that `--extension` loads and a
module of a package both have `__file__`.

To see every step on a tiny example language first, run the tutorial. It
builds an adapter for the Toy in six steps, writes each step's code into the
file `toy.py` in a directory, shows what changed and checks the result. Each step of this guide
that the Toy shows names that Toy step:

```text
langadapter tutorial
langadapter tutorial --for testuary
```

With `--for`, each step also says what that consumer needs. With
`--for testuary` a last step plays a recorded Testuary run on the Toy, which
sends no model request.

## Create the file

Create your language's file with the scaffold, naming your language and its
source file extension:

```text
langadapter new-language javascript --file-extension .js --runtime node
```

`--runtime` names the program that runs your tests. Without it the adapter
marks the runtime as a to-do. The
[command reference](../reference/cli.md#new-language) lists the other
options. The scaffold writes one file, `javascript.py`, in the current directory
or the directory `--output-dir` names. It holds your adapter class, with every
fact and hook of this guide, each hook with a short explanation of what to
return and how to test it and each fact with its default, in the order of
[Hooks and facts](../reference/hooks.md), which differs from this guide's in
a few places. Its last line names the class to LangAdapter:

```python
langadapter_language = JavascriptAdapter
```

Nothing is installed. Name the file whenever you check it:

```text
langadapter check javascript --extension javascript.py
```

Put a small project of your language in a directory, a source file and its
test file whose tests all pass, and give the directory to the check with
`--example`. The check then runs your adapter on it and confirms the hooks
that only a run shows, such as coverage:

```text
langadapter check javascript --extension javascript.py --example example
```

[Check on an example](#check-on-an-example) says what the check runs there,
what it leaves not checked and what it needs.

With `--for <consumer>`, the scaffold marks as a to-do each hook that consumer
requires and that has no working default, and says for every other hook
whether the consumer uses it.
`--for` takes a consumer's module name, such as `testuary`, or its
requirement profile, such as `mytool.json`.

To share your language, put the file in a package as one of its modules and
name that module, such as `langadapter_javascript`, in place of the file. A
shared package can record the versions of the consumers it was written for in
its `pyproject.toml`:

```toml
[tool.langadapter]
for = { testuary = "0.2.0" }
```

After you upgrade a consumer, run `langadapter new-language javascript --diff`
in the package's directory: for a consumer whose installed version differs from
the recorded one, it shows what its profile requires now, and it writes
nothing. Then `--update-record` records the new version.

## Language facts {#language-facts}

<!-- table:language-facts -->
**It makes possible:** everything else to name, find and describe the language's files and tests

**Useful for:** every program that uses the adapter

| Fact | What | Default |
| --- | --- | --- |
| `language` | The registered name, such as javascript. | none: every adapter names its language |
| `file_extensions` | The file extensions of the language's source files, such as .js. | none: every adapter names at least one |
| `display_name` | The language's name in messages and prompts, such as JavaScript. | the consumer's own, such as the name with a capital first letter |
| `runtime` | The program a project's tests run with, such as node. | none: the scaffold marks it to set |
| `test_file_name` | A template for the test file of a source file, with {stem}, {Stem} (the stem with a capital first letter) and {ext}, such as {stem}.test.js. | test_{stem} with the language's file extension |
| `test_file_patterns` | File name patterns every test file matches, such as *.test.js. | none: write is_test_file |
| `test_id_text` | A test's id as the language writes it in kill matrices and records, such as demo.CalcTest.adds. | the TestId's own text: the file and the test's name |
| `test_id_prefix` | The part of the ids of a file's tests before each test's name, such as demo.CalcTest. | the file's stem |
| `file_rule` | The stem a file holding a source must have, such as its public class's name, and the source renamed to fit a given stem. | no rule |
| `scratch_stem` | The stem of a test file written for no particular target, such as Candidate. | the stem "candidate" |
| `code_fence` | The language's name in a fenced code block, such as js. | the consumer's own, such as the language's name |
| `test_framework` | The framework the tests use, such as node:test. | the consumer's own, such as the general words "the test framework" |
| `test_vocabulary` | The method test_vocabulary returns the language's words for its tests, by the entries of langadapter.vocabulary: how a test is declared and collected, fixtures, parametrization, the one-file rule, the import rule, the word for a load failure and more. | generic sentences from the facts above |
| `project_settings` | The settings a project of the language takes, each with its name, purpose, kind (path, executable, choice or text), environment alias, discovery default, check and how a relative host path in it resolves (host_path). | no settings |
| `ignored_dirs` | Directories a search for source files skips, such as node_modules. | the usual version control, environment, build, cache and test directories |
| `non_source_files` | File names a search for source files leaves out although they carry the language's file extension, such as a build script. | no file left out by its name |
| `reuses_bytecode` | Whether a measurement copy of a project may reuse the compiled modules of the runs in earlier copies. | off |
| `copies_external_suite` | Whether a measurement copy of a project takes a suite directory outside the project with it, so that the paths a run prints are the ones a run in place printed. | off |
| `provides_timeout_journal` | Whether run_suite takes timeout_journal, the per-test journal from which a run that a timeout or a crash ended names the test that was running. An adapter that refuses it turns this off. An adapter whose answer depends on the project sets it on the instance, and a consumer reads it on the adapter. | on: run_suite takes the timeout journal |
| `seeds_delivered_runs` | Whether run_as_delivered takes session_seed, so that a consumer may run a suite as delivered with a fixed seed as its normal run. An adapter whose runs as delivered take the seed turns this on. An adapter whose answer depends on the project sets it on the instance, and a consumer reads it on the adapter. | off |
| `source_merge_separator` | The comment prefix of the line merge_sources puts between two folded test files, such as //. | none declared: each merge needs the caller's separator line |
| `validity_policy_version` | The version of the generated-test validity policy whose findings analyze_validity reports, which callers record with each run. | no version: a run records none |

**The check counts these names as the facts':**

- `test_file_patterns`: `is_test_file`
- `file_rule`: [`file_rule`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.file_rule) and [`rename_file_unit`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.rename_file_unit)

**In the Toy:** step 1 of `langadapter tutorial`, after which `toy.py` in the tutorial's directory holds it.

<!-- /table:language-facts -->

Start with the facts. You set the name, the file extensions and the runtime.
Two more facts have no default:

- `test_file_patterns` is written as the method `is_test_file(path)`, which
  says whether a path is a test file. The operations `sources.is_test_file`
  and `sources.project_source_files` need it, and without it they are
  unsupported and the check reads the fact missing. The patterns that
  `language_facts()` reports (`LanguageFacts.test_file_patterns`) are a
  separate field, and they do not make the fact present.
- `source_merge_separator` is the comment that starts the line between two
  merged test files. Set it when your adapter merges test files
  ([Merge test files](#merge-test-files)). Without it the check reads it
  missing, and each merge needs the caller's separator.

The other facts have defaults, so adjust only what differs for your
language. A directory of tests is read by `test_files`, by default with the
`test_file_name` template. Write a fact as a class attribute with its value,
such as `source_merge_separator = "//"` or `validity_policy_version = "1"`.
Four facts are methods: `test_id_text(test)`, `test_id_prefix(source, stem)`,
`file_rule(source)` with `rename_file_unit(source, new_name)`, and
`test_vocabulary()`.

The words that describe your tests are what a consumer such as Testuary shows
a language model when it asks for tests. `test_vocabulary()` returns them by
the entry names `langadapter.vocabulary.ENTRIES` lists. The most important
are `test_declaration`, how a test is written, `import_rule`, how a test
reaches its target, `single_file_rule`, the rule for one test file, and
`load_error`, the word for a test file that does not load. The defaults are
general sentences. Precise words here give better generated tests. Show the
form of a test with one example, as the Toy's one-file rule does:

```python
def test_vocabulary(self):
    return {
        "test_declaration": "test <name>: <expression>",
        "import_rule": "names its target's module on a line use <module>",
        "single_file_rule": ("Write one test per line, such as "
                             "test adds: add(2, 3) == 5."),
        "load_error": "load error",
    }
```

A language whose tests need no import line says so through the hook
`import_statement` ([Name the import line](#import-line)), with a sentence
that starts with `none;`. The Toy's is
`none; a Toy test calls the target's functions by name, without an import line`.

## Run tests

### Run the tests {#run-the-tests}

<!-- table:run-the-tests -->
**It makes possible:** running a project's test suite and reading each test's outcome

**Useful for:** any program that runs a project's tests

`run_suite` (hook)

**What:** Run a suite and return each test's outcome and the run's outcome: it loads, it does not load, it has no tests, it timed out, a tool failed, its setup is missing, or it is unsupported.

**Write:**

```python
def run_suite(self, request: SuiteRunRequest) -> SuiteRun:
    ...
```

**The check counts these names as the hook's:** `run_suite`, [`collection_text`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.collection_text), [`listed_failures`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.listed_failures) and `collect_suite`

**It makes possible:** running a suite and listing its failing tests, and telling a suite that does not load from one that lacks its setup

**Default:** none: write it

**Test it:** A passing and a failing test read passed and failed, and a test file that does not load reads does not load.

**In the Toy:** step 1 of `langadapter tutorial`, after which `toy.py` in the tutorial's directory holds it.

`test_files` (hook)

**What:** Replace a directory by the test files the language's runner collects in it.

**Write:**

```python
def test_files(self, paths) -> list:
    ...
```

**It makes possible:** running a directory of tests as the runner would

**Default:** the files the test file name names

**Test it:** A directory of the example gives its test files.

`project_test_files` (hook)

**What:** The test files the language's runner collects in the project as the project configures them.

**Write:**

```python
def project_test_files(self) -> list:
    ...
```

**It makes possible:** the project's own suite as its runner would run it

**Default:** test_files of the project root

**Test it:** The example's project gives its test files.

`setup_files` (hook)

**What:** The files the runner loads beside a suite's test files, such as pytest's conftest.py, and the setup file of a prepared suite.

**Write:**

```python
def setup_files(self, root: str, test_paths) -> tuple:
    ...
def shared_setup_files(self, directory: str) -> tuple:
    ...
```

**It makes possible:** a copy of a suite that runs with the setup its tests need

**Default:** none

**Test it:** A suite copied with its setup files runs as it did in place.

`run_counts` (hook)

**What:** A run's collected, passed, failed and errored tests as the runner counts them.

**Write:**

```python
def run_counts(self, run) -> dict:
    ...
```

**It makes possible:** records whose counts match the runner's own

**Default:** the run's counts

**Test it:** A run of the example counts its tests.

<!-- /table:run-the-tests -->

`run_suite` takes a `SuiteRunRequest` and returns a `SuiteRun`, both from
`langadapter.runs`. The example below runs a test runner that writes one JSON
line per test to a report file, `myrun --report FILE TEST_FILES`, and turns
the report into the result. Put your runner's command and the reading of its
report in their place. The example refuses a coverage target until you
add coverage, so it passes LangAdapter's conformance check
(`check_adapter`) without a problem.

```python
import json
import subprocess
import tempfile
import time
from pathlib import Path

import langadapter as la
from langadapter.execution import executor_for
from langadapter.runs import (
    ERROR, FAILED, PASSED, SKIPPED, RunCounts, SuiteRun, SuiteRunRequest, TestOutcome,
)
from langadapter.versions import Version

#: The request fields this adapter does not provide. A run that asks for one
#: is refused as unsupported, never run without it.
NOT_PROVIDED = (("coverage", "coverage_target"),
                ("the leak record", "record_leaks"),
                ("a run as delivered", "session_seed"), ("the reverse order", "reverse_order"),
                ("the timeout journal", "timeout_journal"),
                ("the isolated configuration", "isolation_config"),
                ("the removal of created files", "remove_created_files"))
STATUS = {"passed": PASSED, "failed": FAILED, "error": ERROR, "skipped": SKIPPED}


class MyAdapter(la.LangAdapter):
    language = "mylang"
    file_extensions = (".my",)
    runtime = "myrun"
    test_file_name = "test_{stem}.my"
    # run_suite refuses the timeout journal (see NOT_PROVIDED).
    provides_timeout_journal = False

    def _identity(self, request: SuiteRunRequest) -> la.ObservationIdentity:
        # The name, the leading numbers, the text as found, and where it was
        # found (one of langadapter.versions.SOURCES).
        version = Version("myrun", (), "unknown", "unknown")
        return la.ObservationIdentity(
            target=None, suite=request.suite.identity(),
            toolchain=la.ToolchainIdentity.of(version), instrument=None,
            execution=la.ExecutionIdentity("local"))

    @staticmethod
    def _ended(outcome, reason, identity, wall=None, output="") -> SuiteRun:
        return SuiteRun(outcome=outcome, reason=reason, counts=RunCounts(), tests=(),
                        unmatched=(), wall_time_s=wall, output=output, coverage=None,
                        identity=identity)

    def run_suite(self, request: SuiteRunRequest) -> SuiteRun:
        request.validate()
        identity = self._identity(request)
        for name, field in NOT_PROVIDED:
            if getattr(request, field) not in (None, False):
                return self._ended(la.Outcome.UNSUPPORTED,
                                   f"this adapter does not provide {name}", identity)
        # The runtime is the project context's interpreter.
        interpreter = self.context.interpreter
        program = interpreter.path if interpreter is not None else None
        if program is None:
            return self._ended(la.Outcome.MISSING_SETUP,
                               "no runtime in the project context", identity)
        root = Path(request.suite.root)
        files = [path.relative_to(root).as_posix()
                 for path in request.suite.files()[:len(request.suite.test_files)]]
        with tempfile.TemporaryDirectory() as scratch:
            report = Path(scratch) / "report.jsonl"
            started = time.monotonic()
            try:
                done = executor_for(self.context).run(
                    [program, "--project", str(self.context.root), "--report", str(report),
                     *files],
                    cwd=str(root), timeout=request.timeout_s)
            except subprocess.TimeoutExpired:
                return self._ended(la.Outcome.TIMED_OUT,
                                   f"the run reached its timeout of {request.timeout_s} s",
                                   identity, wall=time.monotonic() - started)
            wall = time.monotonic() - started
            output = (done.stdout or "") + (done.stderr or "")
            if not report.exists():
                return self._ended(la.Outcome.INFRASTRUCTURE_FAILURE,
                                   f"the runner wrote no report (exit {done.returncode})",
                                   identity, wall=wall, output=output)
            entries = [json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]
        broken = [entry["file"] for entry in entries if "load_error" in entry]
        tests = tuple(
            TestOutcome(la.TestId(entry["file"], entry["name"],
                                  text=f"{entry['file']}::{entry['name']}"),
                        STATUS[entry["status"]], entry.get("seconds"), entry.get("message", ""))
            for entry in entries if "load_error" not in entry)
        counts = RunCounts(collected=len(tests),
                           passed=sum(t.outcome == PASSED for t in tests),
                           failed=sum(t.outcome == FAILED for t in tests),
                           errors=sum(t.outcome == ERROR for t in tests),
                           skipped=sum(t.outcome == SKIPPED for t in tests))
        if broken:
            outcome, reason = (la.Outcome.COLLECTION_FAILED,
                               "a test file does not load: " + ", ".join(broken))
        elif not tests:
            outcome, reason = la.Outcome.NO_TESTS, "no test"
        elif counts.failed or counts.errors:
            outcome = la.Outcome.TEST_FAILURES
            reason = f"{counts.failed + counts.errors} of {len(tests)} tests did not pass"
        elif done.returncode != 0:
            # The run ended badly with no test to blame.
            outcome = la.Outcome.INFRASTRUCTURE_FAILURE
            reason = f"the runner exited {done.returncode} although no test failed"
        else:
            outcome, reason = la.Outcome.COMPLETED, ""
        return SuiteRun(outcome=outcome, reason=reason, counts=counts, tests=tests,
                        unmatched=(), wall_time_s=wall, output=output, coverage=None,
                        identity=identity, returncode=done.returncode)
```

The result's fields:

- `outcome`, one of `la.Outcome`: `completed` when every test passed,
  `test_failures` when a test did not, `collection_failed` when a test file
  does not load, `no_tests`, `timed_out`, `infrastructure_failure` when the
  runner gave no usable result or ended badly although no test failed,
  `missing_setup` when the runtime or a tool is
  missing, and `unsupported` for a request the adapter refuses. `reason`
  says why in words for every outcome but `completed`.
- `tests`: one `TestOutcome` per test, with its `TestId`, its outcome
  (`PASSED`, `FAILED`, `ERROR` or `SKIPPED` from `langadapter.runs`), its time
  in seconds when the runner reports one, and a failure's message. A `TestId`
  holds the test file relative to the suite's root, the test's qualified
  name within the file and `text`, the language's own form of the id.
- `counts`: a `RunCounts` of the tests by outcome. A count the runner does
  not report stays `None`.
- `unmatched`: report entries that name no test of the suite.
- `wall_time_s`, `output` (the runner's output) and `returncode`.
- `coverage`: the coverage of the request's `coverage_target`, else `None`
  ([Measure coverage](#measure-coverage)).
- `identity`: what ran, namely the suite's files, the toolchain with its
  version, and where it ran.

**Start the runner** with `executor_for(self.context).run(argv, cwd=root,
timeout=seconds)` from `langadapter.execution`. It runs the command for the
project's context, on this machine or in the project's container, with the
context's environment, and returns a `subprocess.CompletedProcess` with the
output. An `env` you pass replaces the context's environment. When the
timeout passes, it ends the command and every process the command started
and raises `subprocess.TimeoutExpired`: the run's outcome is then
`timed_out`. The runtime is the project context's interpreter, as
[Find the runtime](#find-the-runtime) says. The example lets any other
error from the command runner propagate, as the checklist's last rule
says.

**Where the run starts.** `request.suite.files()` lists the suite's test
files first, then its setup files, as absolute paths, and the example keeps
the test files and names them relative to the suite's root, where the run
starts. The suite's root can differ from the project's root
(`self.context.root`), which holds the sources: a consumer may stage a suite
in a directory of its own. A runner that needs the sources finds them from
the project's root, which the example passes as `--project`.

**The request's fields.** A `SuiteRunRequest` holds the `suite` (its test
files and setup files under a root), `timeout_s`, a `coverage_target`, the
`runner_options` your `request_options` hook adds, and side effects that
are off unless a consumer asks for them: `isolation_config`,
`remove_created_files`, `record_leaks` (with `module_under_test`),
`session_seed`, `reverse_order` and `timeout_journal`. The
docstring of `SuiteRunRequest` (`langadapter.runs`) describes each of them,
and [Prepare a suite](#prepare-a-suite) and
[Run as delivered](#run-as-delivered) the ones they use. Carry out each side effect
your adapter provides, and refuse each one it does not, with the outcome
`unsupported` and a reason, as the example does: a run without a side
effect that was asked for would answer another question than the one asked.
An adapter that refuses `timeout_journal` also sets
`provides_timeout_journal = False`, so that consumers do not ask for it.

**Collect without running.** `tests.collect_suite` comes from your adapter's
own method `collect_suite(request)`, which takes a `CollectionRequest` and
returns a `CollectionRun` (both from `langadapter.runs`): the suite's tests
found without running them. Without the method the operation answers
`unsupported`, also when `run_suite` is written.

### Find and remove tests {#find-and-remove-tests}

<!-- table:find-and-remove-tests -->
`test_units` (hook)

**What:** Find each test in a test file, by hand or with the tree-sitter toolkit (langadapter.treesitter), so that one test can be removed, counted or listed.

**Write:**

```python
def test_units(self, source: str):
    ...
```

**The check counts these names as the hook's:** `prune_tests` and `test_metrics`

**It makes possible:** removing a failing test instead of the whole file, and the test count

**Default:** none: write it, by hand or with the tree-sitter toolkit

**Test it:** Removing one named test leaves the others, and the count drops by one.

**In the Toy:** step 2 of `langadapter tutorial`, after which `toy.py` in the tutorial's directory holds it.

<!-- /table:find-and-remove-tests -->

If a tree-sitter grammar exists for your language, the tree-sitter toolkit
finds tests from a few rules instead of code: the node kinds of a test and of
an import. Check whether a grammar package is available:

```text
python -m pip index versions tree-sitter-javascript
```

Add the grammar package to your package's dependencies. The toolkit,
`langadapter.treesitter`, finds the units that its `Rules` name,
and your `test_units` keeps those that are tests. For JavaScript's node:test,
where each test is a top-level `test('name', ...)` or `it('name', ...)`
call:

```python
import langadapter as la
from langadapter.sources import TestUnit
from langadapter.treesitter import Rules, TreeSitterUnits, whole_lines

TEST_CALLS = {"test", "it"}


def _call(node):
    """The call an expression statement makes, or None."""
    call = node.named_children[0] if node.named_children else None
    return call if call is not None and call.type == "call_expression" else None


def _is_test(node) -> bool:
    """A top-level test('name', ...) or it('name', ...) call."""
    call = _call(node)
    function = call.child_by_field_name("function") if call is not None else None
    return function is not None and function.text.decode() in TEST_CALLS


def _label(node) -> str:
    """The test's name: the call's first argument, a string, else ""."""
    call = _call(node)
    arguments = call.child_by_field_name("arguments").named_children if call else ()
    if arguments and arguments[0].type == "string":
        return arguments[0].text.decode()[1:-1]
    return ""


UNITS = TreeSitterUnits(Rules(
    grammar="tree_sitter_javascript", type_kinds=frozenset(), function_kinds=frozenset(),
    top_level_kinds=frozenset({"expression_statement"}), is_test=_is_test,
    signature=lambda node: "", unit_label=_label))


class JavascriptAdapter(la.LangAdapter):
    language = "javascript"
    file_extensions = (".js",)
    runtime = "node"

    def test_units(self, source: str):
        """Each top-level test of ``source`` with its span in characters, or
        None when the file does not parse."""
        members = UNITS.members(source)
        if members is None:
            return None
        data = source.encode("utf-8")

        def char(offset: int) -> int:
            # tree-sitter counts bytes, a TestUnit's span counts characters.
            return len(data[:offset].decode("utf-8"))

        return [TestUnit(label, char(start), char(end))
                for label, node in members if UNITS.rules.is_test(node)
                for start, end in [whole_lines(data, node.start_byte, node.end_byte)]]
```

`Rules.grammar` names the grammar's module, which the toolkit imports:
`tree_sitter_javascript` from the package tree-sitter-javascript.
`top_level_kinds` makes each top-level expression statement a candidate,
`is_test` keeps the test calls and `unit_label` names each by its first
argument. tree-sitter counts bytes, while a `TestUnit`'s span counts the
characters of the source, so the example converts them, and `whole_lines`
widens a span to its whole lines. LangAdapter builds removing, counting and
listing tests on `test_units`: `adapter.sources.prune_tests(source,
("doubles",))` returns a `SourceResult` whose `source` is the file without
that test.

Without a grammar, write the hook yourself: given a test file's text, return
one `TestUnit(name, start, end)` (`langadapter.sources`) for each test, with
its qualified name and the span `source[start:end]` that removing it cuts,
its last line break included, or nothing when the file does not parse.

### Resolve the project {#resolve-the-project}

<!-- table:resolve-the-project -->
`project` (hook)

**What:** Resolve a project's build, such as its dependencies and class path, and say why it does not resolve.

**Write:**

```python
def project(self):
    ...
```

**It makes possible:** refusing a project that does not build before any test runs

**Default:** every project counts as resolved

**Test it:** A project whose build fails reads not resolved, with the reason.

<!-- /table:resolve-the-project -->

### Find the runtime {#find-the-runtime}

<!-- table:find-the-runtime -->
`discover_runtime` (hook)

**What:** Find the program that runs the tests, and the tools, for a project, and describe what its settings select without checking it (describe_runtime).

**Write:**

```python
@classmethod
def discover_runtime(cls, root, values, *, container=False, environment=None,
                     resolve_path=None) -> RuntimeChoice:
    ...
```

**The check counts these names as the hook's:** [`discover_runtime`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.discover_runtime), [`describe_runtime`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.describe_runtime) and [`context_environment`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.context_environment)

**It makes possible:** a project's context without settings the user must write

**Default:** the runtime fact found on PATH

**Versions:** LangAdapter reads no runtime version by default: a run records the context's runtime_version. An adapter whose hooks need the version reads it itself, such as from the runtime's --version.

**Test it:** Your adapter class's discover_runtime(root, {}) finds the example project's runtime.

**In the Toy:** step 1 of `langadapter tutorial`, after which `toy.py` in the tutorial's directory holds it.

`provision_runtime` (hook)

**What:** Make an environment for a requested runtime version, with the project's requirements and the tools the adapter pins, reused while those inputs stay the same.

**Write:**

```python
@classmethod
def provision_runtime(cls, request, *, store, name, environment=None, log=None) -> str:
    ...
```

**It makes possible:** a project run on a runtime version the machine does not have yet

**Default:** none: a run that asks for another runtime version is refused

**Versions:** The pinned tools follow the requested version.

**Test it:** A requested version gives an environment that runs the example once and is reused.

`setup_questions` (hook)

**What:** The questions a consumer's setup asks for a project of the language, with each one's default, reason, choices and check and what is shown after an answer, and the check of a setting set without the questions (setup_problem).

**Write:**

```python
@classmethod
def setup_questions(cls, project_dir, probe, *, consumer="the caller"):
    yield SetupQuestion(f"{cls.language}.runtime", "The program that runs the tests",
                        setting="runtime", default=probe.which(cls.runtime),
                        check=lambda answer: None if probe.is_file(answer) else "no such file")
```

**The check counts these names as the hook's:** [`setup_questions`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.setup_questions) and [`setup_problem`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.setup_problem)

**It makes possible:** a setup that finds the runtime and the build tools and checks them as it asks

**Default:** each declared setting asked with its purpose and checked by its kind

**Test it:** The example project's questions are asked and their answers checked.

<!-- /table:find-the-runtime -->

A project context's interpreter (`context.interpreter.path`) is this program,
the language's runtime, such as node for JavaScript, not the Python that runs
LangAdapter. Your hooks start the project's tests with it.
`langadapter check <language> --extension <file> --example <directory>`
builds the example's context with the runtime fact's program found on PATH.

### Choose run options {#run-options}

<!-- table:run-options -->
`request_options` (hook)

**What:** The request fields the language adds for each kind of run a consumer asks for (RUN_PURPOSES: a run of the tests, failing tests, per-test evidence, a run as delivered, timing, a coverage matrix, a collection, the project's own run), such as its runner's options.

**Write:**

```python
def request_options(self, purpose: str, *, target=None, traceback="short",
                    stop_at_first_failure=False, record_leaks=False,
                    session_seed=None, reverse_order=False,
                    leave_out_timing_plugins=False) -> dict:
    ...
```

**It makes possible:** runs set up the way the language's tools need

**Default:** no options

**Test it:** Each kind of run gets the options it is given.

<!-- /table:run-options -->

### Describe the language {#describe-the-language}

<!-- table:describe-the-language -->
`language_facts` (hook)

**What:** Return the language's LanguageFacts: its name, the test framework, the code fence, the runtime's version and the language level, the test file, test function and test class patterns, the per-test timeout marker, whether a reduction keeps unmatched evidence and the patterns of the warnings that mark a test leaving shared state behind.

**Write:**

```python
def language_facts(self) -> la.LanguageFacts:
    ...
```

**It makes possible:** prompts and records that name the language and its tests

**Default:** none: a program that writes prompts cannot name the language's tests

**Test it:** language_facts names the example's test framework.

**In the Toy:** step 1 of `langadapter tutorial`, after which `toy.py` in the tutorial's directory holds it.

<!-- /table:describe-the-language -->

The eight fields without a default come first, the others are optional
keywords (the docstring of `LanguageFacts` in `langadapter.capabilities`
names each):

```python
def language_facts(self) -> la.LanguageFacts:
    return la.LanguageFacts(
        language=self.language, test_framework="mytest", code_fence="my",
        runtime_version=None, test_file_patterns=("test_*.my",),
        test_function_pattern="test <name>: <expression>", test_class_pattern=None,
        timeout_marker=None)
```

`runtime_version` is the runtime's own version line, `None` where your
adapter does not read it. `test_class_pattern` and `timeout_marker` are
`None` for a language whose tests have no class and no timeout syntax.

## Declare capabilities {#capabilities}

<!-- table:capabilities -->
`capabilities` (hook)

**What:** Report what the project can run: its runtime, its tools and each capability of langadapter.capabilities.CAPABILITIES, available, missing setup or unsupported, with the reason.

**Write:**

```python
def capabilities(self) -> la.CapabilityReport:
    ...
```

**It makes possible:** a readiness report, and programs that run a project only when its tools are there

**Default:** none: the report says readiness probing is not implemented and lists no capability

**Test it:** adapter.probe_capabilities() reports the example's suite runs available.

**In the Toy:** step 1 of `langadapter tutorial`, after which `toy.py` in the tutorial's directory holds it.

`execution_setup_issue` (hook)

**What:** Name a prerequisite of running the project's tools that is known to be missing without starting a tool, such as a setting a container run needs.

**Write:**

```python
def execution_setup_issue(self) -> str:
    ...
```

**It makes possible:** missing setup reported with its reason before any tool starts

**Default:** no prerequisite is known to be missing

**Test it:** With the prerequisite missing, a run of the example reports missing setup with the reason.

`operation_context_issue` (hook)

**What:** Say why an operation cannot run in the project's context, such as a kind of execution or a test framework the adapter does not take.

**Write:**

```python
def operation_context_issue(self, name: str) -> str:
    ...
```

**It makes possible:** an unsupported operation reported with its reason, not as not implemented

**Default:** every operation the adapter implements runs in each context it declares

**Test it:** In the refused context, the operation reads unsupported with the reason.

`operation_readiness` (hook)

**What:** Give an operation's readiness from the probed capability report, as a Capability named like the operation, or None when the probe did not check it.

**Write:**

```python
def operation_readiness(self, name: str, report: la.CapabilityReport):
    ...
```

**It makes possible:** a probed report that says which operations can run

**Default:** the probed readiness of each operation stays unknown

**Test it:** With a tool the probe found missing, probe_capabilities reports the operations that need it as missing setup.

<!-- /table:capabilities -->

Consumers and the conformance check read what your adapter can do from its
capability report. Override `capabilities()` and return a
`CapabilityReport` that names each entry of
`langadapter.capabilities.CAPABILITIES` as available or unsupported.
Without the override, the report says that readiness probing is not
implemented, and the check then expects coverage to be refused:
`check_adapter` reports that an adapter without available coverage must
refuse a coverage request, and that an adapter that measures coverage
reports it available in `capabilities()`. Each time you add a capability,
add it to the report:

```python
from langadapter.capabilities import CAPABILITIES


class MyAdapter(la.LangAdapter):
    #: The capabilities this adapter provides. Add "coverage" once run_suite
    #: measures it, and "per_test_evidence" and "mutation" with their steps.
    _PROVIDED = frozenset({"suite_runs"})

    def capabilities(self) -> la.CapabilityReport:
        return la.CapabilityReport(
            language=self.language, interpreter=getattr(self.context.interpreter, "path", None),
            outcome=la.Outcome.COMPLETED,
            reason="", runtime=None,
            capabilities=tuple(
                la.Capability(name, la.Availability.AVAILABLE) if name in self._PROVIDED
                else la.Capability(name, la.Availability.UNSUPPORTED,
                                   "this adapter does not provide it")
                for name in CAPABILITIES),
            tools=(), branch_coverage="none", mutation_engines=())
```

`CAPABILITIES` names seven entries, and each step below makes one available:

| Entry | The step that makes it available |
| --- | --- |
| `suite_runs` | [Run the tests](#run-the-tests) |
| `coverage` | [Measure coverage](#measure-coverage) |
| `per_test_evidence` | [Collect per-test evidence](#per-test-evidence) |
| `per_test_kill_attribution` | [Attribute kills to tests](#kill-attribution) |
| `mutation` | [Run mutation testing](#run-mutation-testing) |
| `source_merging` | [Merge test files](#merge-test-files) |
| `generated_suite_operations` | the repairs of [Repair loading](#repair-loading), [Remove failing imports](#remove-failing-imports), [Repair syntax](#repair-syntax) and [Remove whole-file skips](#remove-whole-file-skips), and [Screen generated tests](#validity-policy) |

A consumer reads an entry before it uses the step. One that requires the
step refuses a run without it, and one that uses it when present takes its
fallback. [What Testuary requires](consumer-requirements.md) gives
Testuary's for each family.

`branch_coverage` names the kind of branch evidence your coverage tool
records: `arcs` (from one line to another), `counters` (a line's branches,
counted) or `none`, which fits until you add coverage. `mutation_engines`
names your mutation engines once you add them. When the runtime is missing, report the outcome `missing_setup`
with the reason, and each capability you provide as missing setup, such as
`la.Capability("coverage", la.Availability.MISSING_SETUP, "myrun is not on PATH")`.
Callers ask for the report through `probe_capabilities()`.
LangAdapter asks for the report only when the project's context names a
runtime: without an interpreter path, the report's outcome is
`missing_setup`, with the reason "no project runtime is available", and it
lists no capability.

The three hooks after `capabilities` refine what the operation report and
the setup check say. Override them where your language needs it. The base
returns nothing for each:

- `execution_setup_issue()` names a prerequisite you know is missing
  without starting a tool, such as a setting a container run needs. Every
  operation that runs a command then reports it as missing setup.
- `operation_context_issue(name)` says why the operation `name` cannot run
  in this context, in place of "operation is not implemented". A source
  read, such as `sources.stage_pool`, then answers unsupported with the
  same reason.
- `operation_readiness(name, report)` gives the operation's readiness from
  the probed `CapabilityReport`, as a `Capability` named like the operation,
  or None when the probe did not check it.

## One run decides {#one-run-decides}

Read this before you add coverage. Coverage, per-test evidence and mutation
testing each run your tests once more, and this section says how those runs
must stay true to the normal one.

A consumer such as Testuary runs one suite in several ways: the normal test
run, the coverage tool's run, the per-test evidence run and the mutation
tool's runs. Each of them runs the same tests, so each could answer whether a
test passes. When two of them disagree, a test passes in one run and fails in
another, and a consumer cannot tell which answer is right. A user who runs the
suite sees only the normal run's answer.

The differences come from the tools and from the way an adapter starts them.
One real case per language:

- **Java.** JaCoCo and PIT add hidden members to the classes they measure.
  JaCoCo adds the method `$jacocoInit`, and to class files older than Java
  11 also the field `$jacocoData`. PIT adds `$$pitCoverageProbes` and
  `$$pitCoverageProbeSize`. A generated test for a class of the JSONassert
  library counted the class's declared fields by reflection. It passed every
  normal run and failed whenever JaCoCo or PIT measured it, so the suite got
  no coverage evidence and no mutation score. A reflection test that holds
  under both tools skips synthetic members (`Field.isSynthetic()`).
- **C.** gcov counts lines exactly only in an unoptimized build. In an
  optimized build the compiler merges and removes lines, and in a small
  example two covered lines vanish from gcov's report. An adapter for C
  therefore compiles its coverage build at `-O0` and counts coverage and
  each test's evidence on it, while the normal run and the mutants keep the
  project's flags. Code whose behavior depends on the optimization level
  can then pass one build and fail the other.
- **Python.** A test can observe the run's variables. The hash seed decides
  the order of a set of strings, the numeric libraries' thread pools decide
  the last digits of a floating-point sum, and the working directory decides
  which file a relative path opens and which modules import. If the mutation
  tool ran each mutant in a fresh empty directory, with the caller's hash seed
  and its own thread counts, a test that passes the normal run could fail in
  every mutant's run. Every mutant would then count as killed, and the score
  would rise although no test detects a change.

### The rule

- **The normal test run decides** whether a test passes: one command, one
  environment, one working directory, one test selection and order, with the
  suite laid out as it is delivered, the way a user runs it.
- **The tools only measure.** The coverage and mutation tools run the tests
  their own way to add numbers about a suite that passes. A test that fails
  only under a tool still passes.
- **A suite a tool cannot measure gets no number** from that tool, and the
  tool's result names the tests that stopped it.
- **Name the stopping tests while they can still change.** A consumer that
  asks a model for tests tells the model while it can still change them. A
  mutation run names the tests that stopped it (`MutationRun.stopping_tests`),
  and Testuary names them in its reply to the model. At some places where no
  model can still change a suite, Testuary removes the tests a failing
  measurement names and measures the suite again, as Testuary's
  [Measurement and recovery](https://k-kammler.github.io/testuary/explanation/measurement-and-recovery/#tests-removed-after-a-failing-measurement)
  describes.

The reason is the question a user asks: whether the tests pass when the user
runs them. A tool's run is a measuring instrument, and its side effects, such as a
hidden member, an unoptimized build or a fresh directory, are not part of that
question. If a tool's run decided, a suite would be judged by a run the user
never makes. If nothing named the stopping tests, the suite would lose its
numbers and nobody could tell which test to change.

### What your adapter does for it

Each requirement below says what goes wrong without it.

- **Every run in the same environment.** Build the normal run's command and
  environment in one place, and let the coverage run, the evidence run and
  the mutation tool's runs reuse them, adding only what the tool itself needs:
  its launcher, its agent, the mutant. Everything a test can read belongs to
  the environment: the working directory, the import or class path and its
  order, variables such as the hash seed and thread counts, the temporary
  directory and the configuration file the runner reads. Without it, each part
  that differs is a difference of its own, as the Python case shows.
- **Every run from the same start.** Start each run in the same root, with
  the files that earlier runs left there removed and a temporary directory of
  its own. Run no test with more rights than the normal run has, for example
  as the root user of a namespace. Without it, a test that writes a file
  passes its first run and fails the next, and a test can write a read-only
  file under the tool that it cannot write in the normal run.
- **No cleaner between tests in the normal run.** In the normal run the
  tests run one after another, as the language's runner runs them, and
  nothing restores the state one test leaves for the next. A user's run
  has no such cleaner either. A consumer that measures each suite as a
  whole then measures what the user runs, and tests that are independent
  of each other need no cleaner. With one, a suite could pass every
  measurement and fail when the user runs it.
  To show which test leaves state behind in the normal run, an adapter that
  records leaks names the test and restores nothing. Python's runs do so
  with `record_leaks`, through a read-only observer
  that compares, at each test's start and end, the module under test's
  names and, when those libraries are loaded, numpy's error and print
  settings and xarray's options. Java's adapter records no leaks.
- **The tool's check where its mutants run.** A mutation tool first runs the
  unchanged suite. Run that check where the first mutants run, in the same
  copy and with the same environment. Without it, a suite that passes in the
  project and fails in the tool's copy has every mutant killed, and its score
  reads 100.
- **The same order and selection under the mutation tool.** Give the
  mutation tool the normal run's class or import path in its order, and the
  same test classes or files. When the mutation tool runs one test at a time,
  run one test at a time in every run. For Java, PIT reads its classpath from
  a file in the normal run's order and targets the normal run's top-level
  test classes, and Jupiter's parallel execution is off in every run, as
  PIT's JUnit 5 plugin turns it off. Without it, a class that both a tool's
  jar and the project hold, such as a JUnit class, loads from one in the
  normal run and from the other under PIT. PIT also runs a JUnit 4 nested
  class that the normal run skips, and two tests that must not overlap pass
  under PIT and fail in the normal run.
- **Only the tools' own builds.** Where a tool needs a build of
  its own, such as a C coverage build at `-O0` or a mutation tool's own
  compiler, change only what the tool needs and keep the project's other
  flags. Some differences come with the tool and stay: JaCoCo's and PIT's
  members, PIT's own runner, and PIT's jars ahead of the classpath. A test
  whose outcome they change is named, not hidden.
- **The stopping tests named.** When the mutation tool's check of the
  unchanged suite fails, return the tests that did not pass as test ids in
  `MutationRun.stopping_tests`, read from the tool's report: PIT's "did not
  pass without mutation" lines, or a JUnit report of the check. Per-test
  evidence that does not complete returns its tests that did not pass in
  `PerTestEvidence.stopping_tests`, as the suite run's test ids. Without it, a
  consumer knows only that the tool refused the suite, and nobody can be told
  which test to change.
- **Declared facts and hooks.** A consumer reads everything it needs from
  your adapter's facts and hooks, such as `capabilities`, `language_facts`,
  `source_merge_separator` and `non_source_files`, never from your language's
  name. Declare even a fact that looks obvious. A consumer that guessed it for
  Python or Java would fail late for your language, without saying why. The
  scaffold writes every fact and hook, and
  `langadapter check <language> --extension <file> --for <consumer>` names
  each one the consumer requires that your adapter lacks.

### A checklist for measuring runs {#measuring-run-checklist}

Each rule keeps a test that passes the normal run from failing a
measurement, or makes such a failure name its test.

- **Every measuring run is a second way to fail.** Each way your adapter
  runs tests for a measurement is one more run in which a passing test can
  fail. Keep each as close to the normal run as the tool allows, as the
  requirements above say: the same start directory, the same wrapper, the
  same way of starting each test's process, and no files left from earlier
  runs.
- **Name the tests a measurement fails on.** A measuring run that fails
  because of tests names them in its result as the normal run's test ids:
  a failing per-test outcome of a coverage run,
  `PerTestEvidence.stopping_tests` or `MutationRun.stopping_tests`. Map a
  tool's own test names to these ids in your adapter.
  [Run mutation testing](#run-mutation-testing) and
  [Collect per-test evidence](#per-test-evidence) show the outcome that goes
  with the names.
- **Name no test for what no test caused.** A failure that cannot be pinned
  on a test keeps its outcome and names no test, and the consumer then has
  no number for the suite from that measurement. Know these cases for your
  language, such as the tool's compiler rejecting the file, a tool's build
  that does not link, a runner that wrote no report, an unreadable report
  or a whole run's timeout.
- **Read each test's own counts.** This applies where every test's process
  adds to the same coverage counters, as gcov's counter files do, rather
  than a tool writing one report per run, as coverage.py and JaCoCo do. A
  test that leaves a function by a long jump, or through a forked child,
  can leave a negative count that the sum of all tests hides. Read each
  test's own counts from the run that gives the coverage. When the tool
  refuses the counts of the whole run, the run has no coverage: a run whose
  tests all passed is `infrastructure_failure` with the tool's message as
  its reason, a run with failing tests keeps `test_failures`, and each test
  whose own counts the tool refuses is named in a `RunDiagnostic` of kind
  `coverage_counts_inconsistent` in `SuiteRun.diagnostics`. For C, gcovr
  refuses a negative count with "Got negative hit value".
- **A missing or unreadable count is an error, never zero.** This applies
  where each test writes counts of its own, such as a counters file for
  each test's process. A test whose own counts are missing, or that the
  tool refuses, ends per-test evidence with `infrastructure_failure` and
  that test in `PerTestEvidence.stopping_tests`. It ends the mutation
  tool's check of the unchanged suite with `infrastructure_failure`,
  `failure=FAILED_INSTRUMENT_CHECK` and that test in
  `MutationRun.stopping_tests`. Zero coverage would hide it. A process that
  crashes writes no counts, so read counts only from a process that
  completed, and a test that ends its process by `_exit` before the counts
  are written is named. Check that a test which executes nothing of the
  target still writes a count of zeros, as GCC's does, and keep the rule
  to the kinds of test where every test writes counts: a test that only
  runs a script, such as a ctest test whose command is a shell script,
  writes none, and its missing counts read as zero.
- **A test that started after the time ran out is not named.** A run that
  goes on after its deadline gives each later test too little time, and
  such a test failed by the deadline, not by its own fault. The test that
  was running when the time ran out is named, as a test that hangs. When
  every failing test started late, the measurement is `timed_out` and names
  no test. The deadline is a limit that spans several tests, such as a suite
  run's `timeout_s` or a mutation run's wall clock. A suite run names the
  test that was running through the timeout journal
  (`SuiteRun.active_at_end`), and per-test evidence names it in
  `stopping_tests`.
- **A crash blames the running test.** Where the tests of a file share one
  process, a crash is blamed on the test that was running, although an
  earlier test can have damaged the state. Run each test alone where a
  measurement must know which test failed. Only the measurement runs each
  test alone: the normal run keeps the runner's own way of running them, as
  "No cleaner between tests in the normal run" above says.
- **Code outside the tests belongs to no test.** This applies where several
  tests share one process or one program, and code runs before the first
  test or after the last, such as an exit handler. Such code can spoil
  every test's counts. So run every test of the measurement and collect
  each one's failure before you name any: when every test of one program,
  the unit that runs as one process such as a C test file, has missing or
  refused counts, the program is at fault and no test of it is named for
  its counts. The measurement keeps the outcome and reason of the first
  failure it met. A test that fails is named in every case, so the rule on
  missing counts names each test unless every test of the program fails
  its counts. It does not apply where each test runs in a process of its
  own with nothing around it.
- **Judge each test by the runner's result for it.** The test results
  decide each test's outcome. Where a runner writes a report file, as the
  example in [Run the tests](#run-the-tests) does, the report gives each
  test's result. Where a test program prints its own tests' results on its
  output, as C's utest.h programs print one result line per test, give
  outcomes only to the tests the program lists, since a test's output can
  imitate a result line, and read each test's last result line, also
  inside a line that the test's own output began without a line break. In
  both forms the exit status is no test's verdict: a program's own main may
  return any status beside failing tests. The run ended badly only when a
  signal ended it, or when it exited with a nonzero status while no test
  failed. A run that ended badly with no test to blame is
  `infrastructure_failure` with the end as its reason, and names no test.
- **Read a tool's messages in one language.** Run the compiler, the linker
  and the coverage tool with `LC_MESSAGES=C` and without `LC_ALL`, which
  would override it, so that a translated message cannot slip past the
  parser. An `env` you pass to `executor_for(...).run` replaces the
  context's environment, so build it for a local run from that
  environment: the caller's variables when `self.context.environment.inherit`
  is true, with the context's own variables over them, without `LC_ALL` and
  with `LC_MESSAGES` set to `C`. Windows tools read no such variable, and
  the rule does not apply there.
- **Keep a wrapper to what it must do.** A wrapper the runs need is part of
  what a test sees. A line-buffering wrapper such as `stdbuf` works through
  `LD_PRELOAD`, which a test can read and which a sanitizer that must load
  first conflicts with. Buffer standard output only. A line-buffered error
  stream holds a test's last line until the next test's line break and
  moves it into the wrong trace. An adapter needs such a wrapper only where
  the tests' result lines come from a program's output and the language's
  runtime buffers that output in blocks when it is not a terminal, as C's
  standard library does.
- **Keep the verdict lines of cut output.** This applies only where your
  adapter cuts a test program's output itself, to keep its memory bounded:
  `executor_for(...).run` returns the whole output and cuts nothing. When
  you cut, keep from the part you leave out every line that carries a
  test's result, so that every verdict reads as from the whole output. C's
  adapter keeps the first and the last megabyte of each stream and, between
  them, every utest.h RUN and result line. A runner that writes a report
  file needs no such rule: its verdicts are in the report.
- **Name files as the consumer knows them.** A compile or link error names
  the test file as the suite names it, never by the adapter's scratch
  paths.
- **A mutant cut by the wall clock is untested.** The time limit of a
  mutant covers all of its runs. A mutant that the run's wall clock cuts is
  untested, not detected by a timeout, and the time a tool's own failure
  took does not count against the mutant. The run then carries
  `MutationRun.incomplete`, as [Run mutation testing](#run-mutation-testing)
  shows.
- **A failure of the machine is no test's fault.** An error that
  `executor_for(...).run` raises other than `subprocess.TimeoutExpired`,
  such as an `OSError`, means that the command could not be run or
  supervised, not that a test failed. Never turn it into a test's failure
  or a mutant's detection. The examples catch only the command's own
  timeout and let any other error propagate: the operation then raises it,
  and `langadapter.recovery_owners(error)` names what it leaves to clean
  up. C's adapter, which LangAdapter ships, waits until such a command has
  ended, tries a mutant's run once more, since a storage stall can outlast
  the supervisor's time limit, and leaves the mutant untested after a
  second failure.

[The conformance check](conformance.md#find-differences-early) shows how
`compare_runs` finds these differences on your example, before a consumer's
run meets them.

## Measure coverage {#measure-coverage}

<!-- table:measure-coverage -->
**It makes possible:** the line and branch coverage of a source file under a suite

**Useful for:** judging how much of the code the tests reach

`coverage` (hook)

**What:** Coverage is part of run_suite: when the request names a coverage_target, the run returns that file's covered and missed lines and branches.

**Write:**

```python
# Coverage is part of run_suite: when request.coverage_target names a
# source file, the returned SuiteRun's coverage is a CoverageObservation
# of that file's statements and covered lines, branches and taken branches.
```

**It makes possible:** the line and branch coverage of a source file

**Default:** none: write it

**Test it:** A run of the example with its source as the target reports a covered line.

**In the Toy:** step 3 of `langadapter tutorial`, after which `toy.py` in the tutorial's directory holds it.

`coverage_texts` (hook)

**What:** A run's covered lines and missed branches as text, as the coverage tool counts them.

**Write:**

```python
def coverage_texts(self, coverage) -> dict:
    ...
# Where a coverage matrix is measured in the project itself, not in a
# prepared copy:
coverage_matrix_in_place = True
```

**It makes possible:** records that read as the language's coverage tool reports

**Default:** the covered lines, each missed branch as source->destination

**Test it:** A run of the example writes its covered lines.

<!-- /table:measure-coverage -->

Coverage comes from `run_suite`: when `request.coverage_target` names a
source file, relative to the project's root (`self.context.root`), run the
suite under your coverage tool and set the result's `coverage` to a
`CoverageObservation` of that file. Many coverage tools write lcov, which
this function reads (the format is in
[Turning your tools' reports into results](#turning-your-tools-reports-into-results)):

```python
from pathlib import Path
from typing import Optional

from langadapter.outcomes import FileIdentity
from langadapter.runs import CoverageObservation
from langadapter.suites import BranchItem


def coverage_from_lcov(text: str, target: Path, root: Path,
                       ran_in: Path) -> Optional[CoverageObservation]:
    """The coverage of ``target`` in an lcov report's ``text``, None when the
    report holds no record of it. A relative SF path is read against
    ``ran_in``, the directory the tool ran in."""
    relative = target.relative_to(root).as_posix()
    hits, branches, taken, current, found = {}, [], [], False, False
    for line in text.splitlines():
        if line.startswith("SF:"):
            current = (ran_in / line[3:]).resolve() == target.resolve()
            found = found or current
        elif current and line.startswith("DA:"):
            number, count = line[3:].split(",")[:2]
            hits[int(number)] = int(count)
        elif current and line.startswith("BRDA:"):
            number, _block, _branch, count = line[5:].split(",")
            # A line's branches are numbered in the order lcov lists them.
            item = BranchItem(relative, int(number),
                              sum(1 for b in branches if b.source == int(number)))
            branches.append(item)
            if count not in ("-", "0"):
                taken.append(item)
    if not found:
        return None
    statements = tuple(sorted(hits))
    return CoverageObservation(
        target=FileIdentity.of(target, root), statements=statements,
        covered=tuple(n for n in statements if hits[n] > 0),
        branches=tuple(branches), taken=tuple(taken))
```

In `run_suite`, with `target = Path(self.context.root) / request.coverage_target`,
pass `coverage_from_lcov(report_text, target, Path(self.context.root),
Path(request.suite.root))` as the `SuiteRun`'s `coverage`, since the run
starts in the suite's root, and take "coverage" out of the request fields
the adapter refuses and into its capability report. When every test
passed, a report that is missing, cannot be read or holds no record of the
target makes the run's outcome `infrastructure_failure` with the reason,
never zero coverage. A run with failing tests carries its coverage too, and
without a record of the target it keeps `test_failures` and no coverage. A
run that does not load or has no test carries none.

`coverage_texts(coverage)` returns a dict with two keys: `covered_lines`, a
tuple of the covered lines, and `missing_branches`, a tuple of one text per
branch the run did not take. Its default writes a branch as
`source->destination`.

## Run mutation testing {#run-mutation-testing}

<!-- table:run-mutation-testing -->
**It makes possible:** each mutant's status under a suite, with its change and location

**Useful for:** judging whether the tests notice changes to the code

`open_mutation_session` (hook)

**What:** Open a session that runs the mutation tool and returns each mutant's id, location, change text and status.

**Write:**

```python
native_mutation_engines = ("the-tool",)
def mutation_operators(self, engine: str) -> OperatorCatalogue:
    ...
def open_mutation_session(self, engine: str, workers: int = 1, **options) -> MutationSession:
    ...
```

**The check counts these names as the hook's:** `open_mutation_session`, [`mutation_catalogue`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.mutation_catalogue), [`named_operators`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.named_operators), [`mutation_session_attempts`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.mutation_session_attempts), [`mutation_dir_refusal`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.mutation_dir_refusal), [`mutation_workers_refusal`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.mutation_workers_refusal) and [`own_mutant_timeout_rule`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.own_mutant_timeout_rule)

**It makes possible:** each mutant's status, which a consumer reads mutant by mutant, not only as a score

**Default:** none: write it

**Test it:** A session on the example gives a killed and a surviving mutant.

**In the Toy:** step 4 of `langadapter tutorial`, after which `toy.py` in the tutorial's directory holds it.

`validate_mutation_session_options` (hook)

**What:** Check a mutation session's options beyond workers, such as a distributor or settings, raising InvalidRequest for one the engine does not accept.

**Write:**

```python
def validate_mutation_session_options(self, engine, *, workers, distributor,
                                      allow_fewer_workers, settings):
    ...
```

**It makes possible:** engine options beyond workers

**Default:** a session with such an option is refused

**Test it:** An option the engine takes opens a session, and one it does not take is refused.

<!-- /table:run-mutation-testing -->

A mutation session is a `MutationSession` (`langadapter.mutation`)
that `open_mutation_session(engine, workers=1, **options)` returns. It
implements `measure(request)`, which returns a `MutationRun`, and
`release()`, which releases what the session holds. The session's `run`,
`per_suite` and `close` call them, and callers use those.

`request` is a `MutationRequest` (`langadapter.mutation`). Honor its
`target` (a source file relative to the project's root), its `suite`, its
`catalogue` (the operators to use: `catalogue.operators`, or your
catalogue's whole set when it names none), its `per_mutant_timeout` (whole
seconds, a `TimeoutRule` whose `seconds(request.passing_suite_s)` gives
them, or None for your engine's own limit) and its `wall_clock_limit_s`.
Raise `InvalidRequest` naming a field your engine cannot honor, as PIT and
Mull do for `timeout_journal`, since they keep no journal. `attribution`,
`workers`, `fail_on_unresolved` and `progress` take your engine's default
where it does not use them. A session with an
`__init__` of its own calls
`super().__init__(workers_requested=..., workers_started=...)`,
where the base class keeps its state. Declare the engines in the fact
`native_mutation_engines` and their operators in `mutation_operators(engine)`,
which returns an `OperatorCatalogue`. A language without a mutation tool can
bring its own simple engine, as the Toy does in step 4: change the target
one place at a time, run the suite against each mutant in a copy of the
project, and report each mutant.

A whole session for the guide's example language, whose engine swaps each
`+` of the target for `-`. Each run uses a copy of the project as the
project's root, so that the runner reads the mutated source, whether
`run_suite` starts it in the suite's root or hands it the project's root.
The check of the unchanged suite runs in the same copy, and the wall clock
bounds the whole run:

```python
import shutil
import tempfile
import time

from langadapter.mutation import (
    BY_FAILING_TEST, BY_TIMEOUT, DETECTED_STATUSES, FAILED_INITIALIZATION, KILLED_STATUS,
    SURVIVED, SURVIVED_STATUS, TIMEOUT_STATUS, UNTESTED, UNTESTED_STATUS, WITHOUT_NAMED_TEST,
    Classification, Mutant, MutationClass, MutationRun, MutationSession, OperatorCatalogue,
    mutant_key,
)

NOT_RUN = Classification(UNTESTED_STATUS, MutationClass.NOT_RUN, UNTESTED)


def verdict(run) -> Classification:
    """A mutant's classification by the suite run against it."""
    if run.outcome is la.Outcome.TIMED_OUT:
        return Classification(TIMEOUT_STATUS, MutationClass.DETECTED, BY_TIMEOUT)
    if run.failing_tests():
        return Classification(KILLED_STATUS, MutationClass.DETECTED, BY_FAILING_TEST)
    if run.outcome is la.Outcome.COMPLETED:
        return Classification(SURVIVED_STATUS, MutationClass.UNDETECTED, SURVIVED)
    return Classification(KILLED_STATUS, MutationClass.DETECTED, WITHOUT_NAMED_TEST)


def staged(suite, root: Path, copy: Path):
    """The suite in the copy with its setup files, or where it lies outside the project."""
    if not Path(suite.root).is_relative_to(root):
        return suite

    def inside(files):
        return tuple(str(Path(f).relative_to(suite.root)) if Path(f).is_absolute() else f
                     for f in files)

    return la.Suite(str(copy / Path(suite.root).relative_to(root)), inside(suite.test_files),
                    inside(suite.setup_files))


class MySession(MutationSession):
    def __init__(self, adapter, workers=1):
        # The caller asked for workers. The session runs one mutant at a time.
        super().__init__(workers_requested=workers, workers_started=1)
        self.adapter = adapter

    def _in_copy(self, copy: Path):
        """An adapter of the same context, with the copy as the project's root."""
        context = self.adapter.context
        return la.create(self.adapter.language, la.ProjectContext(
            str(copy), context.interpreter, execution=context.execution,
            test_framework=context.test_framework, interpreter_flags=context.interpreter_flags,
            environment=context.environment, runtime_version=context.runtime_version))

    def measure(self, request) -> MutationRun:
        if request.timeout_journal:
            raise la.InvalidRequest("timeout_journal", "myswap keeps no journal")
        if request.catalogue.name not in (None, "swap") or set(
                request.catalogue.operators or ()) - {"swap"}:
            raise la.InvalidRequest("catalogue", "myswap offers the operator swap only")
        root = Path(self.adapter.context.root).resolve()
        # The target as the caller gave it: confine it to the project before any write.
        full = (root / request.target).resolve()
        if not full.is_relative_to(root) or not full.is_file():
            raise la.InvalidRequest("target", f"{request.target} is no file inside the project")
        target = full.relative_to(root)
        wall_s = request.wall_clock_limit_s or 3600
        deadline = time.monotonic() + wall_s
        with tempfile.TemporaryDirectory() as scratch:
            copy = Path(shutil.copytree(root, Path(scratch) / "copy"))
            adapter, suite = self._in_copy(copy), staged(request.suite, root, copy)
            check = adapter.run_suite(SuiteRunRequest(suite, deadline - time.monotonic()))
            if check.outcome is not la.Outcome.COMPLETED:
                return MutationRun.failed(target.as_posix(), check.outcome, check.reason,
                                          failure=FAILED_INITIALIZATION,
                                          stopping_tests=check.failing_tests())
            rule = request.per_mutant_timeout
            own = rule.seconds(request.passing_suite_s) if hasattr(rule, "seconds") else rule or 60
            text = (copy / target).read_text(encoding="utf-8")
            mutants = [self._mutant(adapter, suite, copy / target, target, text, at, n, own,
                                    deadline)
                       for n, at in enumerate(i for i, char in enumerate(text) if char == "+")]
        statuses = [mutant.status for mutant in mutants]
        untested = statuses.count(UNTESTED_STATUS)
        summary = {"total": len(mutants),
                   "detected": sum(status in DETECTED_STATUSES for status in statuses),
                   "survived": statuses.count(SURVIVED_STATUS),
                   "timeout": statuses.count(TIMEOUT_STATUS),
                   "non_viable": 0, "untested": untested, "unresolved": 0}
        cut = {} if not untested else {
            "outcome": la.Outcome.TIMED_OUT,
            "reason": (f"the wall clock limit of {wall_s} s ended the run with mutants left "
                       f"untested: {untested}"),
            "incomplete": {"reason": "wall_clock_limit", "limit_s": wall_s, "untested": untested}}
        return MutationRun(target_file=target.as_posix(), mutants=tuple(mutants), summary=summary,
                           requested_catalogue=request.catalogue.kind,
                           requested_operators=("swap",), **cut)

    def _mutant(self, adapter, suite, path, target, text, at, occurrence, own, deadline):
        """The mutant that swaps the + at offset ``at``, run within the time left."""
        line, diff = text.count("\n", 0, at) + 1, f"+ becomes - at offset {at}"
        cap, run = min(own, deadline - time.monotonic()), None
        if cap > 0:
            path.write_text(text[:at] + "-" + text[at + 1:], encoding="utf-8")
            try:
                run = adapter.run_suite(SuiteRunRequest(suite, cap))
            finally:
                path.write_text(text, encoding="utf-8")
        # A run the wall clock cut, not its own limit, is untested, not a timeout.
        cut = run is None or (run.outcome is la.Outcome.TIMED_OUT and cap < own)
        classification = NOT_RUN if cut else verdict(run)
        killing = () if cut else run.failing_tests()
        row = {"id": f"swap-{occurrence}", "line": line, "status": classification.status,
               "diff": diff, "killers": [test.text for test in killing]}
        return Mutant(
            key=mutant_key(target.as_posix(), "swap", occurrence, line, None, diff),
            engine_id=row["id"], test_outcome=None if run is None else run.outcome.value,
            worker_outcome=None, classification=classification, killers=tuple(row["killers"]),
            diff=diff, row=row, killing_tests=killing)

    def release(self) -> None:
        pass


class MyAdapter(la.LangAdapter):
    # ... the facts and run_suite of "Run the tests"
    native_mutation_engines = ("myswap",)

    def mutation_operators(self, engine: str) -> OperatorCatalogue:
        return OperatorCatalogue(engine, ("swap",))

    def open_mutation_session(self, engine: str, workers: int = 1, **options) -> MySession:
        return MySession(self, workers)
```

LangAdapter hands the request's `target` on as the caller gave it, relative
to the project's root or absolute, so an engine confines it. The example
resolves it against the root and refuses one that is missing or lies
outside the root, naming it as the caller wrote it, before it writes
anything. It then works on the path relative to the root, which
`target_file` reports. The wall clock bounds the whole run, the check of the
unchanged suite included, and each mutant's run takes the smaller of its
own limit and the time left. `timeout_journal` is None unless the caller
asks for the journal, and `catalogue.name` names an operator set of your
engine: refuse one your engine lacks with `la.InvalidRequest(field,
reason)`. `session.run` raises such a refusal to its caller and closes the
session: later runs on it are `infrastructure_failure` with the reason "the
session is closed". `workers_requested` is the number of workers the caller
asked for, and `workers_started` the number the session runs. Once the
session runs, add "mutation" to the `_PROVIDED` of your capability report
and name the engine there, `mutation_engines=("myswap",)`.

Run the session on an example and print each mutant's status. The
example's two files, with the line numbers the output names:

```text
calc.my
 1  def add(a, b):
 2      return a + b
 3
 4
 5  def sign(n):
 6      if n < 0:
 7          return -1
 8      return 1
 9
10
11  def twice(n):
12      return n + n

test_calc.my
 1  test adds: add(2, 3) == 5
 2  test positive: sign(4) == 1
```

Its tests check `add` but not `twice`, so one mutant is killed and one
survives:

```python
import shutil

import langadapter as la
from langadapter.mutation import MutationRequest

la.load_extension("mylang.py")
context = la.ProjectContext("example", la.Interpreter(path=shutil.which("myrun")))
adapter = la.create("mylang", context)
with adapter.mutations.open_session("myswap") as session:
    run = session.run(MutationRequest("calc.my", la.Suite("example", ("test_calc.my",))))
for mutant in run.mutants:
    print(mutant.key.line, mutant.status, [test.text for test in mutant.killing_tests])
print(run.outcome.value, run.score())
```

```text
2 killed ['test_calc.my::adds']
12 survived []
completed 50.0
```

The requests that reach the example's other branches, with `Catalogue` and
`TimeoutRule` from `langadapter.mutation`:

```python
from langadapter.mutation import Catalogue, TimeoutRule

suite = la.Suite("example", ("test_calc.my",))
refused = [MutationRequest("calc.my", suite, catalogue=Catalogue(operators=("other",))),
           MutationRequest("calc.my", suite, catalogue=Catalogue(name="other")),
           MutationRequest("calc.my", suite, timeout_journal=True),
           MutationRequest("../elsewhere/calc.my", suite)]
limited = [MutationRequest("calc.my", suite, per_mutant_timeout=TimeoutRule(minimum_s=1)),
           MutationRequest("calc.my", suite, wall_clock_limit_s=1)]
```

Each request in `refused` makes `session.run` raise `la.InvalidRequest`,
naming the catalogue, the journal or the target. The rule in `limited` gives
each mutant's run the larger of 1 s and 1.5 times the passing time the
request gives (`passing_suite_s`). A wall clock of 1 s ends a run that does
not finish in it as `timed_out`: with the mutants it did not run untested,
or, when the check of the unchanged suite does not finish, as a failed run
without mutants.

`langadapter check` with `--example` runs one mutation run on the example.
It reports only a mutant without a `diff` or a `key.line`, and a note at
the end when every mutant survived: run the script above to see the
verdicts. Read a run's score with `score()`, which is None for a failed run,
a run the wall clock cut and a run without a valid mutant. `complete` alone
is no such test: a failed run whose outcome is `timed_out` reads complete.

Options beyond `workers`, such as Cosmic Ray's `distributor` and `settings`,
reach your hook only when the adapter checks them in
`validate_mutation_session_options(engine, *, workers, distributor,
allow_fewer_workers, settings)`, which raises `InvalidRequest` for one it does
not accept. Without that method, a session with such an option is refused. A
session can answer `per_suite` faster than one run per suite by overriding
`per_suite_fast(request)`, which
returns the runs by suite name and the implementation's name, or None to
measure each suite on its own. `request.suites` holds `BudgetedSuite`s, each
with its `name`, `suite`, `per_mutant_timeout` and `passing_suite_s`, and
`request.standalone(name)` is that suite's `MutationRequest`.
[Conformance](conformance.md) checks that its
runs equal the standalone runs.

A `MutationRun` (`langadapter.mutation`) holds the `target_file`
relative to the project root, its `mutants`, a `summary` of counts, the
`requested_catalogue` and `requested_operators`, and an `outcome` with its
`reason`: `completed`, `timed_out` when the wall clock cut the run, or a
failure. The `summary` is recorded with the run, and no check reads its
keys. The built-in engines and the Toy give `total`, `detected`,
`survived`, `timeout`, `non_viable`, `untested` and `unresolved`, where
`detected` counts every mutant of `DETECTED_STATUSES`: killed, timed out and
not viable.

**When the check of the unchanged suite fails.** A mutation tool first runs
the suite without a mutant. When a test does not pass there, return no
mutants and name those tests:

```python
from langadapter.mutation import FAILED_INITIALIZATION, MutationRun

return MutationRun.failed(
    relative_target, la.Outcome.TEST_FAILURES,
    "2 tests do not pass without a mutant",
    failure=FAILED_INITIALIZATION, stopping_tests=failing_ids)
```

`MutationRun.failed` gives a run without mutants, summary or score.
`failing_ids` are the suite run's `TestId`s of the tests that did not pass,
read from the tool's report of its check. When the tool cannot read a
test's coverage counts in that check, the outcome is
`infrastructure_failure` with `failure=FAILED_INSTRUMENT_CHECK`, naming
that test. `failure` is one of `langadapter.mutation.FAILURES`, whose
docstrings say what failed: the session, the tool's start
(`FAILED_INITIALIZATION`), the instrument's check, the tool's session file,
or the workers. A failure no test caused, such as the tool's own crash,
names no test. A check that does not load or reaches its time limit keeps
that outcome, with `failure=FAILED_INITIALIZATION` and no test named, as
the session example does.

**When the wall clock cuts the run.** Mark each mutant without a result
untested (`UNTESTED_STATUS`), return outcome `timed_out` with the reason,
and set `incomplete`, which keeps the score from reading as complete:

```python
MutationRun(
    target_file=relative_target, mutants=tuple(mutants), summary=summary,
    requested_catalogue=request.catalogue.kind, requested_operators=operators,
    outcome=la.Outcome.TIMED_OUT,
    reason=f"the wall clock limit of {limit} s ended the run with mutants left untested: {left}",
    incomplete={"reason": "wall_clock_limit", "limit_s": limit, "untested": left})
```

A run with `incomplete` set is not `complete`, so `score()` is None, and
`progress_score()` gives the lower bound in which untested mutants count as
not killed. The kill matrix carries the `incomplete` mapping. LangAdapter
reads none of its keys, and the built-in engines write `reason`, `limit_s`
and `untested`. A `wall_clock_limit_s` of None leaves the limit to your
engine: the built-in engines then take one hour.

Each `Mutant` holds its `key`, made with
`mutant_key(file, operator, occurrence, line, column, diff)` so that it is
the same in every run, the tool's own `engine_id`, the tool's
`test_outcome`, its `worker_outcome` (None where the tool has none), its
`classification`, the killing tests as the runner names them (`killers`)
and as `TestId`s (`killing_tests`), its `diff` and its kill-matrix `row`,
which holds at least its `id` and its `status`. `test_outcome` holds the
tool's own word for the mutant's result, such as PIT's `KILLED`, and `diff`
the change as text, a unified diff where the tool writes one.
`langadapter check` with
`--example` reads `open_mutation_session` failing when a mutant of the
example lacks its `diff` or its `key.line`. It runs this mutation only
when the example has a coverage target, one source file beside its tests,
and your adapter declares a mutation engine.

The classification is a `Classification(status, mutation_class,
subcategory)` from `langadapter.mutation`, each status one of its
constants:

| The tool says | status | class | subcategory |
| --- | --- | --- | --- |
| killed by a named failing test | `KILLED_STATUS` | `MutationClass.DETECTED` | `BY_FAILING_TEST` |
| killed, no test named | `KILLED_STATUS` | `MutationClass.DETECTED` | `WITHOUT_NAMED_TEST` |
| timed out | `TIMEOUT_STATUS` | `MutationClass.DETECTED` | `BY_TIMEOUT` |
| not viable, such as a mutant that does not compile | `NON_VIABLE_STATUS` | `MutationClass.DETECTED` | `NON_VIABLE` |
| survived | `SURVIVED_STATUS` | `MutationClass.UNDETECTED` | `SURVIVED` |
| not run | `UNTESTED_STATUS` | `MutationClass.NOT_RUN` | `UNTESTED` |
| could not run the tests for the mutant, such as Cosmic Ray's incompetent | `UNTESTED_STATUS` | `MutationClass.NOT_RUN` | `INCOMPETENT` |
| skipped by a filter | `SKIPPED_STATUS` | `MutationClass.NOT_RUN` | `SKIPPED` |

A mutation score counts detected mutants, those of `DETECTED_STATUSES`, as
killed and an untested mutant as not killed, and it leaves a skipped
mutant out.

## Attribute kills to tests {#kill-attribution}

<!-- table:kill-attribution -->
**It makes possible:** which tests kill each mutant

**Useful for:** reducing a suite while keeping every kill

`kill_attribution` (hook)

**What:** Name, for each killed mutant, the tests that kill it.

**Write:**

```python
# Name, for each killed mutant, the tests that kill it
# (Mutant.killing_tests), and declare it:
mutation_evidence_granularity = ("aggregate", "per_mutant", "per_test")
```

**It makes possible:** reductions that keep every kill

**Default:** no attribution

**Test it:** A mutant killed by one test names that test.

**In the Toy:** step 4 of `langadapter tutorial`, after which `toy.py` in the tutorial's directory holds it.

`kill_tokens` (hook)

**What:** Turn a stored kill matrix's named killers into each test's kill tokens, and a pool's kills into the tokens of its suites' tests.

**Write:**

```python
def kill_tokens(self, kill_matrix, test_ids):
    ...
def pool_kills(self, kill_matrix, file_to_index, test_ids_by_index) -> dict:
    ...
```

**It makes possible:** reductions and pools that keep every kill

**Default:** the killing tests each row records (Mutant.killing_tests)

**Test it:** A killer that names one test gives that test its mutant's token.

<!-- /table:kill-attribution -->

`mutation_evidence_granularity` names what your mutation runs report:
`aggregate` the run's counts, `per_mutant` each mutant's status and
`per_test` the tests that kill each mutant. `kill_tokens(kill_matrix,
test_ids)` takes the tests' ids as `test_id_text` writes them, not `TestId`
objects, and returns each test's kill tokens (`K:<mutant id>`) with the
killers that match no test. `pool_kills` does the same for a pooled suite,
by suite. Both have defaults that read `Mutant.killing_tests`. Add
`per_test_kill_attribution` to your capability report once each killed
mutant names its tests.

## Turning your tools' reports into results

Most test runners, coverage tools and mutation tools can write a report file
in a common format. Reading that file is usually easier and more reliable
than reading what the tool prints.

- **Test outcomes.** Many runners write JUnit XML: one `testcase` element
  per test, with a `failure`, `error` or `skipped` child when it did not
  pass. Each becomes one test outcome: `passed`, `failed`, `error` or
  `skipped`, with the test's time when the report has it. A test the report
  does not list is not counted.
- **Coverage.** Many coverage tools write lcov: per source file (`SF:`),
  one `DA:<line>,<hits>` record per executable line and one
  `BRDA:<line>,<block>,<branch>,<taken>` record per branch. The lines are
  the statements, the lines with hits above zero are covered, the branches
  are the `BRDA` records and the taken branches are those with a count above
  zero. A `-` count means the branch was never reached: it is not taken.
  `BranchItem(file, source, destination)` names a branch. A tool that
  records branches from one line to another, as coverage.py does, gives the
  destination line. A tool that counts a line's branches without a
  destination, as lcov's `BRDA` records and JaCoCo do, numbers the line's
  branches instead, `BranchItem(file, line, ordinal)`, with the same ordinal
  for the same branch in every run, such as its place among the line's
  `BRDA` records. Then override `coverage_texts`, whose default writes each
  missed branch as source->destination, so that it writes them as your tool
  counts them: Java's adapter writes `line->k of n branches untaken`.
- **Mutants.** Mutation tools write a report per mutant: where it is, what
  it changed and its status, such as killed, survived, timed out or not
  covered. Keep each mutant's status. A score alone hides which mutants
  survived. The Toy's mutation session, in step 4 of the tutorial, reports
  each mutant with its id, line, change and status. In its example project
  one mutant survives: `double(x) = x * 2` changed to `x + 2` still passes
  the test `double(2) == 4`, so the tests need another case.

Report what the tool reported. When a report is missing or cannot be read,
say so with the reason, rather than reporting zero tests or zero coverage:
a measured zero and a missing measurement are different results.

## Repair loading {#repair-loading}

<!-- table:repair-loading -->
**It makes possible:** keeping a test file whose loading fails at one unit, by removing that unit

**Useful for:** keeping generated or edited tests that fail to load

`prune_collection_breakers` (hook)

**What:** Remove the units that keep a test file from loading: the shared loop (templates.repair_loading) with the language's load step (collection or compilation, reporting the failing lines) and its removal of the unit around each line, a statement, a member or a line.

**Write:**

```python
def prune_collection_breakers(self, source: str, *, timeout_s: float) -> SourceResult:
    ...
```

**It makes possible:** the shared loop that removes the units that keep a file from loading

**Default:** no load repair

**Test it:** A file with one unit that does not load keeps its other tests.

**In the Toy:** step 5 of `langadapter tutorial`, after which `toy.py` in the tutorial's directory holds it.

`collection_unit` (fact)

**What:** The unit prune_collection_breakers removes, in words, such as the smallest declaration around a line javac reports an error on.

**It makes possible:** a consumer's record of what a repair removed

**Default:** none: an adapter that repairs loading declares it

**Test it:** The example's breaker is removed as that unit.

**In the Toy:** step 5 of `langadapter tutorial`, after which `toy.py` in the tutorial's directory holds it.

<!-- /table:repair-loading -->

The repair examples below work on test files of the guide's example
language, whose lines read `use MODULE`, `test NAME: EXPRESSION` or
`skip file`. A repair returns a `SourceResult(status, source, parser)`
(`langadapter.sources`): `APPLIED` with the new source and what it
`removed`, `UNCHANGED` with the input when nothing needed a repair, or
`NOT_REPAIRED` with the input and the reason in `detail`. Load repair and
import pruning ask the runner which lines fail, through one helper that
loads a text as a test file without running a test (`myrun --collect-only`):

```python
import json
import tempfile

from langadapter import templates
from langadapter.sources import APPLIED, NOT_REPAIRED, UNCHANGED, SourceResult


class MyAdapter(la.LangAdapter):
    # ... the facts and run_suite of "Run the tests"
    collection_unit = "a line of a test file"

    def _load_errors(self, text: str, timeout_s: float = 60) -> list:
        """The runner's load errors for ``text`` as a test file, each with its line,
        or None when the runner wrote no report."""
        with tempfile.TemporaryDirectory() as scratch:
            test, report = Path(scratch) / "test_candidate.my", Path(scratch) / "report.jsonl"
            test.write_text(text, encoding="utf-8")
            executor_for(self.context).run(
                [self.context.interpreter.path, "--project", str(self.context.root), "--report",
                 str(report), "--collect-only", str(test)],
                cwd=str(self.context.root), timeout=timeout_s)
            if not report.exists():
                return None
            entries = report.read_text(encoding="utf-8").splitlines()
        return [entry for entry in map(json.loads, entries) if "load_error" in entry]

    def prune_collection_breakers(self, source: str, *, timeout_s: float) -> SourceResult:
        def load(text):
            errors = self._load_errors(text, timeout_s)
            if errors is None:
                return templates.Load(False, stop="the runner wrote no report")
            if not errors:
                return templates.Load(True)
            return templates.Load(False, (errors[0]["line"],), errors[0]["load_error"])

        def remove(text, lines):
            rows = text.splitlines(keepends=True)
            return "".join(rows[:lines[0] - 1] + rows[lines[0]:]), [rows[lines[0] - 1].strip()]

        def keeps_a_test(text):
            return any(line.startswith("test ") for line in text.splitlines())

        result = templates.repair_loading(source, load, remove, loads=5,
                                          keeps_a_test=keeps_a_test)
        if result.repaired:
            return SourceResult(APPLIED, result.source, "myrun", removed=result.removed)
        if result.status == templates.LOADS:
            return SourceResult(UNCHANGED, source, "myrun")
        return SourceResult(NOT_REPAIRED, source, "myrun", detail=result.failure or result.stop)
```

`load(text)` returns `templates.Load(True)` for a text that loads, else
`templates.Load(False, lines, failure)` with the failing lines, numbered
from 1, and the loader's message. `remove(text, lines)` returns the text
without the units around those lines and their labels, or None when a line
lies in no unit. `templates.repair_loading` loads at most `loads` times and
adopts the result only once it loads. Its `status` is `templates.LOADS`,
with `repaired` true when it removed something, else `NO_LOCATION`,
`NOT_REMOVABLE`, `NO_TEST_LEFT`, `EXHAUSTED` or `STOPPED`, with the last
`failure`. With `keeps_a_test`, a removal that leaves no test ends the
repair as `NO_TEST_LEFT`, and a load that cannot run returns
`templates.Load(False, stop=reason)`, which ends it as `STOPPED`: a missing
report is said, never read as a text that loads. `collection_unit` says in
words what the repair removes. Add
`generated_suite_operations` to your capability report once your adapter
provides the repairs and [Screen generated tests](#validity-policy).

## Remove failing imports {#remove-failing-imports}

<!-- table:remove-failing-imports -->
**It makes possible:** removing the imports of a test file that do not resolve in the project

**Useful for:** keeping generated tests whose fault is an import of something the project lacks

`prune_unresolvable_imports` (hook)

**What:** Remove the imports of a test file that do not resolve in the project: the shared loop (templates.prune_units) with the language's search for the failing imports and their removal.

**Write:**

```python
def prune_unresolvable_imports(self, source: str) -> SourceResult:
    ...
```

**It makes possible:** keeping a test file whose fault is an import of something the project lacks

**Default:** no import pruning

**Test it:** A file that imports a missing module keeps its tests, without that import.

<!-- /table:remove-failing-imports -->

With the helper of [Repair loading](#repair-loading), the imports the runner
cannot resolve are the lines its load errors mark as imports:

```python
    def prune_unresolvable_imports(self, source: str) -> SourceResult:
        def find(text):
            errors = self._load_errors(text)
            if errors is None:
                return templates.Failing(stop="the runner wrote no report")
            return templates.Failing([error["line"] for error in errors if "import" in error])

        def remove(text, lines):
            rows = text.splitlines(keepends=True)
            kept = [row for number, row in enumerate(rows, 1) if number not in lines]
            return "".join(kept), [rows[number - 1].strip() for number in lines]

        result = templates.prune_units(source, find, remove, rounds=3)
        if result.status == templates.PRUNED:
            return SourceResult(APPLIED, result.source, "myrun", removed=result.removed)
        if result.status == templates.UNCHANGED:
            return SourceResult(UNCHANGED, source, "myrun")
        return SourceResult(NOT_REPAIRED, source, "myrun", detail=result.stop)
```

`find(text)` returns `templates.Failing(units)` with the failing units, here
line numbers, or `templates.Failing(stop=reason)` when it cannot look.
`remove(text, units)` returns the text without them and their labels.
`templates.prune_units` keeps every removal although other faults remain:
its `status` is `templates.PRUNED`, `UNCHANGED` when nothing failed, or
`STOPPED` with the reason in `stop`.

## Check and repair syntax

### Check the syntax {#check-syntax}

<!-- table:check-syntax -->
`check_syntax` (hook)

**What:** Tell a source the language parses from one it rejects, with the first error's place.

**Write:**

```python
def validate_source(self, source: str) -> SourceValidation:
    ...
```

**The check counts these names as the hook's:** `validate_source`, `check_structure` and `finalize_source`

**It makes possible:** rejecting a broken file before running it

**Default:** none: write validate_source, which runs the project's runtime, or check_structure, a parse with the grammar alone

**Test it:** A file with a syntax error reads invalid, with its line.

<!-- /table:check-syntax -->

`validate_source(source)` returns a `SourceValidation` (`langadapter.sources`),
whose fields are the status, the message, the first error's line and
column, and the interpreter that judged the source:
`SourceValidation("valid", "", 0, 0, interpreter)` for a source the language
accepts, or `SourceValidation("syntax_error", message, line, column,
interpreter)` with its first error. Its two other statuses, `timeout` and
`infrastructure_failure`, are no verdict on the source. The source may be a
test file or a source file. The alternative, `check_structure(source)`,
parses with the grammar alone and returns a `StructureCheck` with `ok`,
`message`, `line` and `column` (0 when it parses) and `parser`. A consumer
may use both: Testuary judges a generated source with `validate_source` and
each edit with `check_structure`, as
[What Testuary requires](consumer-requirements.md) says.

For the guide's example language, whose expressions Python's parser reads:

```python
import ast

from langadapter.sources import SourceValidation


    def validate_source(self, source: str) -> SourceValidation:
        # A test file's expressions. A source file goes through your parser the same way.
        for number, line in enumerate(source.splitlines(), 1):
            if line.startswith("test ") and ":" in line:
                try:
                    ast.parse(line.split(":", 1)[1].strip(), mode="eval")
                except SyntaxError as error:
                    return SourceValidation("syntax_error", error.msg, number,
                                            line.index(":") + 2, "Python's ast")
        return SourceValidation("valid", "", 0, 0, "Python's ast")
```

`check_structure(source)`, which Testuary calls on each edit, returns a
`StructureCheck` (`langadapter.sources`). With the `validate_source` above:

```python
from langadapter.sources import StructureCheck


    def check_structure(self, source: str) -> StructureCheck:
        verdict = self.validate_source(source)
        if verdict.status == "valid":
            return StructureCheck(True, "", 0, 0, "Python's ast")
        return StructureCheck(False, f"{verdict.message} at line {verdict.line}:{verdict.column}",
                              verdict.line, verdict.column, "Python's ast")
```

`finalize_source(source)` returns the source without detached comment
blocks as a `SourceResult`. A language may leave it out: Testuary then keeps
each source as it is.

### Repair syntax {#repair-syntax}

<!-- table:repair-syntax -->
**It makes possible:** telling a source the language's parser accepts from one it rejects, and a bounded repair of one it rejects

**Useful for:** rejecting broken files early and keeping generated tests with a local syntax error

`salvage_syntax` (hook)

**What:** Repair a source's syntax errors line by line: the shared loop (templates.repair_syntax) with the language's validator and local fix.

**Write:**

```python
def salvage_syntax(self, source: str) -> SourceResult:
    ...
```

**The check counts these names as the hook's:** `salvage_syntax` and [`salvage_report`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.salvage_report)

**It makes possible:** the shared loop that repairs a source line by line

**Default:** no syntax repair

**Test it:** A source with one bad line parses after the repair.

<!-- /table:repair-syntax -->

With that `validate_source`, a bounded repair that closes the parentheses
an expression leaves open:

```python
    def salvage_syntax(self, source: str) -> SourceResult:
        def local_fix(lines, validation):
            # The lines keep their line ends and count from 1, as validation.line does.
            line = lines[validation.line - 1]
            missing = line.count("(") - line.count(")")
            if missing < 1:
                return False
            lines[validation.line - 1] = line.rstrip("\r\n") + ")" * missing + "\n"
            return True

        result = templates.repair_syntax(source, self.validate_source, local_fix)
        if result.status == templates.REPAIRED:
            return SourceResult(APPLIED, result.source, "Python's ast", repairs=result.repairs)
        if result.status == templates.VALID:
            return SourceResult(UNCHANGED, source, "Python's ast")
        return SourceResult(NOT_REPAIRED, source, "Python's ast",
                            detail=getattr(result.validation, "message", ""))
```

`local_fix(lines, validation)` changes the lines at the place the
validation names and returns false when it cannot. `templates.repair_syntax`
validates at most 25 times. Its `status` is `templates.REPAIRED` with the
number of fixes in `repairs`, `VALID` when the source parsed at once, else
`BEYOND_REPAIR`, `VALIDATOR_FAILED` or `EXHAUSTED`. The fix restores
parsing, not the test's meaning: `add(2, (3) == 5` becomes
`add(2, (3) == 5)`, which parses and is another test.

## Remove whole-file skips {#remove-whole-file-skips}

<!-- table:remove-whole-file-skips -->
**It makes possible:** removing statements that skip a whole test file

**Useful for:** making the tests of a file run instead of skipping

`strip_module_level_skips` (hook)

**What:** Remove the statements or annotations that skip a whole test file.

**Write:**

```python
def strip_module_level_skips(self, source: str) -> SourceResult:
    ...
```

**The check counts these names as the hook's:** `strip_module_level_skips` and [`skip_removal`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.skip_removal)

**It makes possible:** removing them so that the file's tests run

**Default:** no skip removal

**Test it:** A file with a whole-file skip runs its tests after the removal.

<!-- /table:remove-whole-file-skips -->

Skip removal needs no shared loop: it returns the source without the
statements that skip the whole file.

```python
    def strip_module_level_skips(self, source: str) -> SourceResult:
        rows = source.splitlines(keepends=True)
        kept = [row for row in rows if row.strip() != "skip file"]
        if len(kept) == len(rows):
            return SourceResult(UNCHANGED, source, "myrun")
        return SourceResult(APPLIED, "".join(kept), "myrun", removed=("skip file",))
```

## Name the import line {#import-line}

<!-- table:import-line -->
**It makes possible:** the line a test of a source file uses to import it

**Useful for:** writing a first test for a source file

`import_statement` (hook)

**What:** The line a test uses to import a given source file, or a sentence that says none is needed (`none; ...`).

**Write:**

```python
@classmethod
def import_statement(cls, target: str, root: str) -> str:
    ...
```

**It makes possible:** a first test that imports its target correctly

**Default:** the vocabulary's import rule only

**Test it:** The line imports the example's source, or the sentence says that no import line is needed.

<!-- /table:import-line -->

## Merge test files {#merge-test-files}

<!-- table:merge-test-files -->
**It makes possible:** folding several test files into one, with their name collisions resolved

**Useful for:** combining suites into one

`merge_sources` (hook)

**What:** Fold one test file into another and resolve the names both declare. `resolve_collisions` returns a `SourceObservation`, completed with a `CollisionResolution` and its parser, or with `infrastructure_failure`, `timeout` or `missing_setup` and a reason.

**Write:**

```python
def merge_sources(self, first: str, second: str, *, separator: str) -> SourceResult:
    ...
```

**The check counts these names as the hook's:** `merge_sources`, `resolve_collisions`, [`collision_resolution`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.collision_resolution), [`collision_dependents`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.collision_dependents), `merge_collisions`, `normalize_bindings`, `normalize_shadowed` and `stage_pool`

**It makes possible:** one suite from several

**Default:** no merging

**Test it:** Two example test files fold into one whose tests all run.

<!-- /table:merge-test-files -->

Before a merge a consumer resolves the name collisions among the sources
with `resolve_collisions(sources, *, preference=None, localize_autouse=False)`,
which returns a `SourceObservation` of a `CollisionResolution` (both from
`langadapter.sources`). For the guide's example language, a test name that
an earlier source holds is renamed:

```python
from langadapter.sources import CollisionResolution


    def resolve_collisions(self, sources, *, preference=None, localize_autouse=False):
        seen, renames, rewritten = set(), [], []
        for index, source in enumerate(sources):
            names = [line[5:].split(":", 1)[0].strip() for line in source.splitlines()
                     if line.startswith("test ")]
            taken, renamed = seen | set(names), {}
            for name in (name for name in names if name in seen):
                number = index + 1
                while f"{name}_{number}" in taken:
                    number += 1
                renamed[name] = f"{name}_{number}"
                taken.add(renamed[name])
            seen = taken
            for old, new in renamed.items():
                source = source.replace(f"test {old}:", f"test {new}:")
            renames.append(renamed)
            rewritten.append(source)
        none = ((),) * len(sources)
        return la.SourceObservation(la.SourceStatus.COMPLETED, CollisionResolution(
            tuple(rewritten), renames=tuple(renames), deduped=none, dropped_defs=none,
            localized_autouse=none), "myrun")
```

Every per-source field, `sources` (the rewritten texts), `renames`,
`deduped`, `dropped_defs` and `localized_autouse`, holds one entry for each
source, in its order. `preference` decides a binding that no rename can
change, and `localize_autouse` concerns Python's autouse fixtures, which a
language without them leaves aside. A new name is one that no source holds
yet, so a renamed test meets no other.

`merge_sources(first, second, *, separator)` returns one test file:

```python
    source_merge_separator = "#"

    def merge_sources(self, first: str, second: str, *, separator: str) -> SourceResult:
        return SourceResult(APPLIED, first.rstrip("\n") + "\n" + separator + "\n" + second,
                            "myrun")
```

`separator` is the comment line to put between the two: the caller's line,
which starts with your `source_merge_separator`, or that fact itself.

The hook table lists further operations of this family and of
[Screen generated tests](#validity-policy). Each comes from a method of its
own name, which the built-in languages write and a language added by this
guide may leave out. Each then answers `unsupported`:

- `sources.merge_collisions(first, second)`, the names both define, and
  `sources.normalize_shadowed(source)`, the source without the definitions
  a later one hides: Testuary does not call them.
- `sources.normalize_bindings(source)`, a `SuiteNormalization`: Testuary
  then keeps each source as it is.
- `sources.stage_pool(members, setup=...)`, a `StagedPool`: Testuary's
  starter phase end then measures each starter suite on its own.
- `sources.screen_source(source, target)`, review-only signals as a
  `PolicyReport`: Testuary then reports none.

Add `source_merging` to your capability report once `merge_sources` and
`resolve_collisions` are in.

## Collect per-test evidence {#per-test-evidence}

<!-- table:per-test-evidence -->
**It makes possible:** each test's own coverage

**Useful for:** reducing a suite to the fewest tests that cover what it covered

`per_test_evidence` (hook)

**What:** Return each test's own coverage.

**Write:**

```python
def per_test_evidence(self, request):
    ...
```

**It makes possible:** the reductions that keep what a suite covered

**Default:** no per-test evidence

**Test it:** Each example test reports the lines it covers.

**In the Toy:** step 3 of `langadapter tutorial`, after which `toy.py` in the tutorial's directory holds it.

`test_evidence_granularity` (fact)

**What:** The granularity of the evidence each test operation reports, as pairs of the operation's name and its granularities: aggregate for the suite as a whole, per_test for each test, such as ("per_test_evidence", ("per_test",)).

**It makes possible:** an operation report that says which operations report evidence per test

**Default:** no granularity: the operation report gives each test operation none

**Test it:** operation_capabilities() gives each declared operation its granularity.

<!-- /table:per-test-evidence -->

`per_test_evidence` takes a `PerTestEvidenceRequest` and returns a
`PerTestEvidence`, both from `langadapter.runs`. The request names the
`suite`, the `target` source file relative to the project's root, a
`timeout_s` and, optionally, a `mutation_run` of the same suite whose kills
each test's evidence then holds. The example below extends the adapter of
[Run the tests](#run-the-tests) once its `run_suite` measures coverage, as
[Measure coverage](#measure-coverage) says: it lists the suite's tests with
a coverage run, then runs each test alone under coverage, with the runner's options
`--select NAME` and `--lcov FILE`, and reads each test's lines with
`coverage_from_lcov` from [Measure coverage](#measure-coverage). Every test
runs until one reaches the timeout, and each that does not pass alone, or
whose coverage is missing, is named.

```python
from langadapter.runs import (
    KILLS_NOT_REQUESTED, KILLS_UNUSABLE, KILLS_USABLE, PerTestEvidence,
    PerTestEvidenceRequest, TestEvidence,
)
from langadapter.suites import KillItem, LineItem


class MyAdapter(la.LangAdapter):
    # ... the facts, _identity, _ended and run_suite of "Run the tests"

    def _run_alone(self, request, test):
        """``test`` run alone under coverage: its status, its time in
        seconds and the lcov text, None when the runner wrote none."""
        root = Path(request.suite.root)
        with tempfile.TemporaryDirectory() as scratch:
            report, lcov = Path(scratch) / "report.jsonl", Path(scratch) / "coverage.lcov"
            executor_for(self.context).run(
                [self.context.interpreter.path, "--project", str(self.context.root), "--report",
                 str(report), "--lcov", str(lcov), "--select", test.qualname, test.file],
                cwd=str(root), timeout=request.timeout_s)
            entries = ([json.loads(line) for line in report.read_text(encoding="utf-8").splitlines()]
                       if report.exists() else [])
            entry = next((e for e in entries if e.get("name") == test.qualname), None)
            lines = lcov.read_text(encoding="utf-8") if lcov.exists() else None
        if entry is None:
            return ERROR, None, lines
        return STATUS[entry["status"]], entry.get("seconds"), lines

    def per_test_evidence(self, request: PerTestEvidenceRequest) -> PerTestEvidence:
        request.validate()
        identity = self._identity(request)
        root = Path(self.context.root)
        target = root / request.target
        kills, unmapped, killed = KILLS_NOT_REQUESTED, (), {}
        if request.mutation_run is not None:
            unmapped = request.mutation_run.unmatched_killers()
            kills = KILLS_UNUSABLE if unmapped else KILLS_USABLE
            for mutant in () if unmapped else request.mutation_run.mutants:
                for test in mutant.killing_tests:
                    killed.setdefault(test, set()).add(KillItem(str(mutant.row["id"])))

        def ended(outcome, reason, stopping=()):
            return PerTestEvidence(outcome, reason, (), (), (), kills, unmapped, None, None,
                                   identity, stopping_tests=tuple(stopping))

        listed = self.run_suite(SuiteRunRequest(request.suite, request.timeout_s,
                                                coverage_target=request.target))
        if listed.outcome is not la.Outcome.COMPLETED:
            # A suite that is not green carries no evidence.
            return ended(listed.outcome, listed.reason,
                         [t.test for t in listed.tests if t.outcome in (FAILED, ERROR)])
        evidence, stopping, first = [], [], None
        for outcome in listed.tests:
            try:
                status, seconds, lines = self._run_alone(request, outcome.test)
            except subprocess.TimeoutExpired:
                return ended(la.Outcome.TIMED_OUT, f"{outcome.test.text} reached the timeout "
                             f"of {request.timeout_s} s", [outcome.test])
            own = (coverage_from_lcov(lines, target, root, Path(request.suite.root))
                   if lines is not None else None)
            if status != PASSED:
                first = first or (la.Outcome.TEST_FAILURES, f"{outcome.test.text} did not pass alone")
                stopping.append(outcome.test)
            elif own is None:
                first = first or (la.Outcome.INFRASTRUCTURE_FAILURE,
                                  f"{outcome.test.text} wrote no coverage of the target")
                stopping.append(outcome.test)
            else:
                items = {LineItem(own.target.path, line) for line in own.covered} | set(own.taken)
                evidence.append(TestEvidence(
                    outcome.test, frozenset(items | killed.get(outcome.test, set())),
                    1000.0 * seconds if seconds is not None else None))
        if stopping:
            return ended(*first, stopping)
        return PerTestEvidence(
            la.Outcome.COMPLETED, "", tuple(evidence),
            tuple(e.test for e in evidence if e.time_ms is None), (), kills, unmapped,
            1000.0 * listed.wall_time_s, listed.coverage, identity)
```

The result's fields, in order:

- `outcome` and `reason`: `completed` when every test gave its evidence.
  A test that does not pass alone makes it `test_failures`, a test whose
  coverage is missing `infrastructure_failure`, and the first failure the
  run met decides the outcome and the reason.
- `tests`: one `TestEvidence` per test, with its `TestId`, the items it
  covers (`LineItem` and `BranchItem` of the target, `KillItem` of each
  mutant it kills) and its time in milliseconds, `None` without one.
- `untimed`: the tests without a time. `unmatched_contexts`: coverage
  contexts that name no test of the suite, for a tool that records them.
- `kills`: `not_requested` without a `mutation_run`, else `usable`, or
  `unusable` with `unmapped_killers` when a killer names no test of the
  suite. A kill item names a mutant by the id its kill-matrix row records.
- `suite_time_ms`, `coverage` (the suite's coverage of the target) and
  `identity`.
- `stopping_tests`: the tests the failure is pinned on, as the suite run's
  test ids, in the order the run met them. Empty when the evidence is
  complete or no test can be named.

Make sure your coverage tool writes a record of the target for every test,
also for one that never loads the target, with zero counts. Otherwise the
example names that test as one without coverage. A consumer asks with
`PerTestEvidenceRequest(suite, "calc.js", timeout_s=120)`, and
`mutation_run=` adds a run's kills. Add `per_test_evidence` to your
capability report once the evidence is in, and declare the granularity of
each test operation's evidence, such as
`test_evidence_granularity = (("run_suite", ("aggregate", "per_test")), ("per_test_evidence", ("per_test",)))`.

The example runs each test in a process of its own, so the checklist's rule
on code outside the tests does not apply to it. An adapter whose tests
share one process reads the rules on counts in
[A checklist for measuring runs](#measuring-run-checklist).

## Suite setup {#suite-preparation}

### Prepare a suite {#prepare-a-suite}

<!-- table:prepare-a-suite -->
**It makes possible:** the setup a suite runs with, and warnings of tests that leave state behind

**Useful for:** isolated measurements and finding tests that leak state

`prepare_suite` (hook)

**What:** Write the setup a suite directory needs before its tests run, such as an import shim, and on request the configuration that isolates it.

**Write:**

```python
def prepare_suite(self, suite_root: str, *,
                  isolation_config: bool = False):
    ...
```

**It makes possible:** isolated, repeatable measurements

**Default:** no setup written

**Test it:** A prepared suite runs.

<!-- /table:prepare-a-suite -->

`prepare_suite(suite_root, *, isolation_config=False)` writes the setup and
returns an object with an `outcome` and a `reason`. The Python adapter
returns a `SuitePreparation` (`langadapter.python.project`), which any
adapter can return:

```python
from langadapter.python.project import SuitePreparation


    def prepare_suite(self, suite_root: str, *, isolation_config: bool = False):
        # The file the runner reads before the suite's tests.
        setup = Path(suite_root) / "setup.my"
        setup.write_text("use calc\n", encoding="utf-8")
        return SuitePreparation(la.Outcome.COMPLETED, setup_files=(str(setup),))
```

`isolation_config` asks for the configuration that keeps the suite from the
project's own test configuration. A language whose runner reads no such
configuration has nothing to write for it.

### Warn of tests that leak state {#unsound-test-warnings}

<!-- table:unsound-test-warnings -->
`unsound_test_patterns` (fact)

**What:** In LanguageFacts.unsound_test_patterns, the regular expressions of the warnings that mark a test leaving shared state behind. A suite run reports each such warning in its diagnostics, naming the test that left it.

**It makes possible:** removing passing tests that leak state

**Default:** no warnings read

**Test it:** A test that leaks state is named.

`order_dependent_mock_warnings` (hook)

**What:** Name the tests that assert on a named element of a mock's side_effect list, whose order a scheduler in the code under test can change.

**Write:**

```python
def order_dependent_mock_warnings(self, source: str) -> list:
    ...
```

**It makes possible:** advice on tests that pass in one scheduling order only

**Default:** no advice

**Test it:** A test that asserts on one element of a mock's side_effect list is named.

<!-- /table:unsound-test-warnings -->

A language without warnings of leaked state or without mock objects leaves
these two hooks out. The check then reads them missing, and a consumer that
uses them when present takes its fallback.

## Run as delivered {#run-as-delivered}

<!-- table:run-as-delivered -->
**It makes possible:** running a suite as a user who copied it would

**Useful for:** checking that a suite works where it is delivered

`run_as_delivered` (hook)

**What:** Run each test file alone, as a user who copied it would, all of them within the request's one timeout.

**Write:**

```python
def run_as_delivered(self, request, *, sources=None):
    ...
```

**The check counts these names as the hook's:** `run_as_delivered` and [`delivered_order_refusal`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.LangAdapter.delivered_order_refusal)

**It makes possible:** the check that a suite works where it is delivered

**Default:** no delivered runs

**Test it:** The example's test file passes as delivered.

`delivered_case` (hook)

**What:** A test of a run as delivered: its id, its name with its parameters, and its outcome.

**Write:**

```python
def delivered_case(self, test, outcome: str) -> tuple:
    ...
```

**It makes possible:** records of delivered runs in the language's own words

**Default:** the test's id, its name and parameters, its outcome

**Test it:** A delivered run of the example names each test once.

<!-- /table:run-as-delivered -->

`run_as_delivered(request, *, sources=None)` takes a `SuiteRunRequest` and
returns one `SuiteRun` (`langadapter.runs`) over all the test files: each
file runs on its own, with its import setup and without your measurement
instrument, in what is left of `request.timeout_s`. `sources` maps a test
file's path relative to the suite's root, with `/` separators, to the text
to run in its place, without changing the file. With the `run_suite` of
[Run the tests](#run-the-tests):

```python
import shutil
import tempfile
import time

from langadapter.runs import ERROR, FAILED, PASSED, SKIPPED, RunCounts, SuiteRun


    def run_as_delivered(self, request, *, sources=None):
        root, started, tests = Path(request.suite.root), time.monotonic(), []

        def inside(files):
            return tuple(Path(f).relative_to(root).as_posix() if Path(f).is_absolute() else f
                         for f in files)

        for relative in inside(request.suite.test_files):
            left = request.timeout_s - (time.monotonic() - started)
            if left <= 0:
                return self._ended(la.Outcome.TIMED_OUT, "the run reached its timeout",
                                   self._identity(request), wall=time.monotonic() - started)
            with tempfile.TemporaryDirectory() as scratch:
                # The suite's directory as delivered, the file's replacement in its place.
                copy = Path(shutil.copytree(root, Path(scratch) / "suite"))
                if sources and relative in sources:
                    (copy / relative).write_text(sources[relative], encoding="utf-8")
                alone = la.Suite(str(copy), (relative,), inside(request.suite.setup_files))
                run = self.run_suite(SuiteRunRequest(alone, left))
            if run.outcome not in (la.Outcome.COMPLETED, la.Outcome.TEST_FAILURES):
                return run
            tests += run.tests
        counts = RunCounts(collected=len(tests),
                           passed=sum(t.outcome == PASSED for t in tests),
                           failed=sum(t.outcome == FAILED for t in tests),
                           errors=sum(t.outcome == ERROR for t in tests),
                           skipped=sum(t.outcome == SKIPPED for t in tests))
        failing = counts.failed + counts.errors
        outcome = (la.Outcome.TEST_FAILURES if failing else
                   la.Outcome.COMPLETED if tests else la.Outcome.NO_TESTS)
        reason = f"{failing} of {len(tests)} tests did not pass" if failing else ""
        return SuiteRun(outcome=outcome, reason=reason, counts=counts, tests=tuple(tests),
                        unmatched=(), wall_time_s=time.monotonic() - started, output="",
                        coverage=None, identity=self._identity(request))
```

A run as delivered measures no coverage.

## Screen generated tests {#validity-policy}

<!-- table:validity-policy -->
**It makes possible:** findings on tests that read the implementation instead of calling it

**Useful for:** keeping generated tests honest

`validity_policy` (hook)

**What:** Report the places where a test reads the implementation's source or internals instead of calling it.

**Write:**

```python
def target_identity(self, target: str) -> TargetIdentity:
    ...
def analyze_validity(self, source: str, target) -> PolicyReport:
    ...
```

**The check counts these names as the hook's:** `analyze_validity`, `target_identity` and `screen_source`

**It makes possible:** a gate that keeps such tests out

**Default:** no screening

**Test it:** A test that reads the source file is reported.

<!-- /table:validity-policy -->

`target_identity(target)` returns a `TargetIdentity`, and
`analyze_validity(source, target)` a `PolicyReport` whose items are
`SuiteValidityFinding`s, all from `langadapter.sources`. For the guide's
example language, a test that names the target's file reads its source:

```python
import hashlib

from langadapter.sources import PolicyReport, SuiteValidityFinding, TargetIdentity


    validity_policy_version = "1"

    def target_identity(self, target: str) -> TargetIdentity:
        text = (Path(self.context.root) / target).read_text(encoding="utf-8")
        return TargetIdentity(target, str(self.context.root), Path(target).stem, self.language,
                              self.validity_policy_version,
                              hashlib.sha256(text.encode("utf-8")).hexdigest(), text)

    def analyze_validity(self, source: str, target: TargetIdentity) -> PolicyReport:
        findings = tuple(
            SuiteValidityFinding("target_source_path", self.validity_policy_version,
                                 line.split(":", 1)[1].strip(), "reads the target's source file",
                                 number, 1, number, len(line), line[5:].split(":", 1)[0].strip())
            for number, line in enumerate(source.splitlines(), 1)
            if line.startswith("test ") and target.source_path in line)
        return PolicyReport("completed", findings, self.validity_policy_version, "myrun")
```

A finding names its rule, the policy's version, the expression that reads
the implementation (`operation`), a diagnostic, its span and the test that
holds it.

## Time a suite {#timing}

<!-- table:timing -->
**It makes possible:** the time a suite takes, repeated, and warmed where the runtime warms up

**Useful for:** choosing between suites by speed and timing records

`benchmark` (hook)

**What:** Run a suite repeatedly and return its times, warmed where the runtime warms up.

**Write:**

```python
def benchmark(self, request):
    ...
```

**The check counts these names as the hook's:** `benchmark` and `benchmark_warmed`

**It makes possible:** choosing between suites by speed

**Default:** no timing

**Test it:** Repeated runs of the example give times.

<!-- /table:timing -->

`benchmark(request)` takes a `BenchmarkRequest`, such as a consumer's
`BenchmarkRequest(suite, runs=3, warmup_runs=1, timeout_s=60)`, and returns a
`Benchmark`, both from `langadapter.runs`. With the `run_suite` of
[Run the tests](#run-the-tests):

```python
from langadapter.runs import WALL_TIME_SCOPE, Benchmark


    def benchmark(self, request) -> Benchmark:
        def ended(outcome, reason, times=()):
            return Benchmark(outcome, reason, tuple(times), request.warmup_runs, request.cpus,
                             WALL_TIME_SCOPE, self._identity(request))

        if request.cpus:
            return ended(la.Outcome.UNSUPPORTED, "this adapter does not pin processors")
        times = []
        for repetition in range(request.warmup_runs + request.runs):
            run = self.run_suite(SuiteRunRequest(request.suite, request.timeout_s))
            if run.outcome is not la.Outcome.COMPLETED:
                return ended(run.outcome, run.reason, times)
            if repetition >= request.warmup_runs:
                times.append(1000.0 * run.wall_time_s)
        return ended(la.Outcome.COMPLETED, "", times)
```

`includes` says what a time covers, here `WALL_TIME_SCOPE`, the runner's
process from start to exit. A benchmark that ends early keeps the times of
the repetitions that passed before it, and only one whose every repetition
passed has statistics.

## Declare versions {#versions}

<!-- table:versions -->
**It makes possible:** notes and refusals by the version of the runtime and the tools

**Useful for:** telling users which versions are tested

`support` (fact)

**What:** The runtime versions the adapter is tested and expected to work with, and the support of named tools. The Python and Java adapters refuse an older version with the reason. Another adapter refuses one only when its version profile's refusal() gives a reason (choose_profile).

**It makes possible:** notes and refusals by version

**Default:** undeclared: every version is untested and never refused

**Versions:** This is the version declaration.

**Test it:** support.classify(version) reads tested for a tested version. The adapter's profile() reads it for the runtime its choose_profile found. Where the profile refuses older versions, an older one is refused with the reason.

**In the Toy:** step 6 of `langadapter tutorial`, after which `toy.py` in the tutorial's directory holds it.

`choose_profile` (hook)

**What:** Return the VersionProfile (langadapter.versions) of the runtime version the adapter found, from a subclass whose refusal() gives the reason for a version the adapter does not support.

**Write:**

```python
def choose_profile(self):
    ...
```

**It makes possible:** refusing a runtime the adapter does not support, with the reason

**Default:** the base profile, which reads no runtime and refuses no version

**Test it:** A runtime older than the adapter supports is refused with the reason.

<!-- /table:versions -->

Without a declaration, every version of your language's runtime counts as
untested, and none is refused. Once you have tested some versions, declare
them: the versions you tested, the ones you expect to work, and the reason
an older one does not. The scaffold fills the declaration from
`--runtime-name`, `--tested-runtime` and `--expected-runtime`:

```python
from langadapter.versions import LanguageSupport, RuntimeSupport, VersionSpan

    support = LanguageSupport(RuntimeSupport(
        "myrun", tested=(VersionSpan((5, 3), (5, 4)),), expected=(VersionSpan((5, 2), (5, 2)),),
        reason="myrun needs 5.2 or newer"))
```

A version in a tested span reads `TESTED`, one in an expected span
`EXPECTED`, one below every span `UNSUPPORTED` with the `reason`, and any
other, such as a newer one, `UNTESTED`.

The declaration classifies a version, but it refuses none by itself: the
base profile reads no runtime. To refuse an older runtime, as the built-in
Python and Java adapters do, override `choose_profile()` to return a
`VersionProfile` (`langadapter.versions`) of the runtime's `Version` that you
found, from a subclass whose `refusal()` gives the reason for a version you
do not support. The Python and Java adapters add its `note()` to their setup
check. The base adapter has no setup check, so yours shows the note only
where it calls `note()` itself. The base profile's `note()` is empty for
every version: a subclass returns its own, such as a sentence on an
untested version. A refusal makes every operation that runs a command
answer `unsupported` with the reason. The capability report does not read
it unless your `capabilities()` does. A profile holds `runtime` (the `Version`
found), `declaration` (your `LanguageSupport`) and `support` (its
classification of the runtime):

```python
from langadapter.versions import Support, VersionProfile


class MyProfile(VersionProfile):
    def refusal(self) -> str:
        if self.support is not Support.UNSUPPORTED:
            return ""
        return f"myrun does not run on {self.runtime.text}: {self.declaration.runtime.reason}"
```

Until your `choose_profile()` reads a version, `adapter.profile()` reads
untested whatever you declared, since the base profile's runtime is None.
The declaration itself classifies a version you give it:
`MyAdapter.support.classify(Version("lua", (5, 4), "5.4", "probe"))` reads
`Support.TESTED` for a version among your tested ones. A profile that reads
the context's runtime version, without refusing any:

```python
from langadapter.versions import Version, VersionProfile, numbers_of


def choose_profile(self) -> VersionProfile:
    text = self.context.runtime_version
    version = Version("lua", numbers_of(text), text, "context") if text else None
    return VersionProfile(version, self.support)
```

`ProjectContext.runtime_version` is the runtime's version text the caller
gives, None when it gives none, as in
`la.ProjectContext(root, interpreter, runtime_version="5.4.6")`. With
`MyProfile` in place of `VersionProfile`, the method refuses a version below
your tested ones.

## Run in containers {#containers}

<!-- table:containers -->
**It makes possible:** runs inside a container the user prepared

**Useful for:** running projects whose tools live in a container

`execution_kinds` (fact)

**What:** Whether source operations, test runs and mutation sessions can run in a container the user prepared, each by its own fact.

**Write:**

```python
# Runs in a container the user prepared:
source_execution_kinds = ("local", "container")
test_execution_kinds = ("local", "container")
mutation_execution_kinds = ("local", "container")
```

**It makes possible:** runs inside a container

**Default:** local runs only

**Test it:** A container context runs the example's suite.

<!-- /table:containers -->

## Structural metrics {#structural-metrics}

<!-- table:structural-metrics -->
**It makes possible:** sizes and complexity of sources and tests, not the test count

**Useful for:** describing sources and suites

`structural_metrics` (hook)

**What:** Sizes and complexity of a source, per file and per function, and the names and fingerprints of its definitions.

**Write:**

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

**The check counts these names as the hook's:** `optimization_metrics`, `structural_parser_identity`, `skeleton`, `same_skeleton` and `function_fingerprints`

**It makes possible:** describing sources and suites

**Default:** no structural metrics

**Test it:** The example's source has a size.

<!-- /table:structural-metrics -->

Testuary also reads `sources.skeleton` and `sources.function_fingerprints`,
and `sources.same_skeleton` compares two sources. They come from three
further methods. For the guide's example language, whose sources define
functions with `def`:

```python
import hashlib


    def skeleton(self, source: str) -> tuple:
        """The names the source defines, in order."""
        return tuple(line.split()[1].split("(")[0] for line in source.splitlines()
                     if line.startswith("def "))

    def function_fingerprints(self, source: str) -> dict:
        """Each definition's fingerprint by its name, the same whatever the layout."""
        bodies, name = {}, None
        for line in source.splitlines():
            name = line.split()[1].split("(")[0] if line.startswith("def ") else name
            if name is not None:
                bodies[name] = bodies.get(name, "") + "".join(line.split())
        return {key: hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
                for key, body in bodies.items()}

    def same_skeleton(self, first: str, second: str) -> tuple:
        """Whether both define the same names in the same order, and why not."""
        names, other = self.skeleton(first), self.skeleton(second)
        return names == other, "" if names == other else f"{names} against {other}"
```

`skeleton` returns the names, `function_fingerprints` a mapping from each
name to its fingerprint, and `same_skeleton` a pair of the verdict and the
reason. The operations hand them on as observations: a tuple of names,
(name, fingerprint) pairs sorted by name, and the pair.

## Metric providers {#metric-providers}

<!-- table:metric-providers -->
**It makes possible:** further metrics from the language package or from other packages, per file and per scope

**Useful for:** analysis tools that add their own metrics

`scoped_metrics` (hook)

**What:** The structural metrics per function, with each function's own complexity, beside the file's. It takes effect with the two structural metrics methods. Metrics of registered providers need no hook.

**Write:**

```python
def scoped_optimization_metrics(self, source: str):
    ...
```

**It makes possible:** analysis tools that add their own metrics

**Default:** no scoped metrics

**Test it:** A function of the example's source has its own complexity.

`metric_providers` (hook)

**What:** The language's own metric providers, which every adapter object of the language registers beside the structural provider. A name already registered is refused.

**Write:**

```python
def metric_providers(self):
    return ()
```

**It makes possible:** metrics a language package adds for all its adapters

**Default:** no metrics of its own

**Test it:** A metric of the language's provider is listed and measured.

<!-- /table:metric-providers -->

A metric provider adds a measurement without changing your adapter.
[A metric provider](metric-provider.md) shows how to write one and how
`metric_providers` registers a language's own providers.

## Code smells {#code-smells}

<!-- table:code-smells -->
**It makes possible:** a count of a linter's findings in sources and tests

**Useful for:** judging the style of generated code

`code_smells` (hook)

**What:** Count a linter's findings in given files.

**Write:**

```python
def code_smells(self, paths):
    ...
```

**It makes possible:** judging the style of generated code

**Default:** no smell count

**Test it:** A file with one finding counts one.

<!-- /table:code-smells -->

`code_smells(paths)` returns the number of findings as a whole number, or
None when the linter could not run. Its observation names your
`structural_parser_identity` as its parser, and `sources.metrics` reports
your structural counts under LangAdapter's structural provider, whatever
parser counted them:

```python
import shutil
from typing import Optional


    def code_smells(self, paths) -> Optional[int]:
        # The program's file by name, which on Windows also finds a .cmd or .bat.
        linter = shutil.which("mylint", path=self.context.environment.get("PATH"))
        if linter is None:
            return None
        done = executor_for(self.context).run([linter, "--count", *paths],
                                              cwd=str(self.context.root), timeout=120)
        return int(done.stdout.strip()) if done.returncode == 0 else None
```

## Call statistics {#call-statistics}

<!-- table:call-statistics -->
**It makes possible:** estimates of how often the rest of the project calls a file's functions

**Useful for:** impact analysis, review priorities and ranking code by its reach

`call_statistics` (hook)

**What:** Count a source's call sites by the called name. The call statistics of a file count them in the project's other source files for each function the scoped metrics report.

**Write:**

```python
def call_statistics(self, source: str):
    ...
```

**It makes possible:** estimating how widely a file's functions are used

**Default:** no call statistics

**Test it:** A call of a function of the example's source in another file counts.

<!-- /table:call-statistics -->

Return a dictionary from each called name, the last segment of a dotted
call, to its count, or None when the source does not parse.
`sources.call_statistics` reads every other source file of the project
through it once per call, and names the functions as the scoped metrics
do.

## Profile a suite {#profiling}

<!-- table:profiling -->
**It makes possible:** where a suite's run spends its time, by function and file

**Useful for:** performance work, ranking code by measured cost and choosing the tests that exercise a function

`profile_suite` (hook)

**What:** Run a suite once under the language's profiler and return the time each function it entered took, with the profiler's name, version and kind.

**Write:**

```python
def profile_suite(self, request):
    ...
```

**It makes possible:** finding the functions a suite spends its time in

**Default:** no suite profile

**Test it:** A profile of the example's suite names a function of its source.

<!-- /table:profiling -->

Return a `SuiteProfile` whose functions carry the profiled run's times and
whose `profiler`, `profiler_version` and `kind` name the tool and how it
measures. [Suite profiles](../guide/timing.md#suite-profiles) says which
runs list functions.

## The check

Run the check with your language's name whenever you finish a step:

```text
langadapter check javascript --extension javascript.py
langadapter check javascript --extension javascript.py --for testuary
```

Without a language, `langadapter check` prints the registered languages
and the installed consumers. With a language and without
`--for`, it lists the facts, then every hook in the order of
[Hooks and facts](../reference/hooks.md): a hook that is there reads present,
and a missing one says what it would make possible. With `--for`, it lists what that
consumer requires, what it uses when present and what it does not use, grouped
into the consumer's levels, and says "Ready: yes", "Ready: no" or "Ready:
everything required is there", followed by what only a run on an example
confirms. For a consumer that is not installed, it lists every family as
without `--for` and names the missing consumer among its problems. Each
fact and hook line names one of five status words after the
name:

- **present**: the hook is there. With an example, it also answered as
  expected.
- **default used**: your adapter leaves the hook out and LangAdapter's
  default does the work. The line says what the default does. A note at the
  end says once that writing the fact or hook changes the default.
- **missing**: the hook is not there. Without `--for` the line says what it
  would make possible. For a consumer, a hook it requires reads "hook <name>
  (<title>): missing, required by <consumer>", and an optional one says what
  the consumer does with it and without it, once for each family. Each links
  this guide's step.
- **failing**: the hook is there but returned something unexpected. Only a
  check with an example (`--example`) finds this. The
  line says what it returned and what was expected.
- **not checked**: the command did not see the hook work. Coverage reads so
  without a run on an example, on an example that does not hold exactly one
  source file, and when your capability report reads coverage unavailable.
  `open_mutation_session` reads so when the example's tests fail and when
  the report reads mutation missing setup. `unsound_test_patterns` shows only
  in the facts the example's adapter returns.

When nothing required is missing but a required hook is not checked, the
check reads, for example, "Testuary 0.2.0: everything required is there.
Only a run on an example confirms coverage: run this check again
with --example DIR." After a run on an example, the line instead names each
hook not checked on it, and that hook's line says why. The
command ends with status 2 when the language is not registered, when an
`--extension` item cannot be loaded, when `--example` names no directory or
one without a test file by your test file rule, when the example's runtime
is not on PATH, or when your capability report says the example's tests
cannot run. It ends with
status 1 when a consumer you named is not ready or cannot be read, for example
because it is not installed, also when the report lists a problem: an
installed consumer's profile that cannot be read, or an adapter that cannot be
made, and with `--example` when a hook the example ran reads failing. It
ends with status 1 too when your adapter's `execution_setup_issue` names
why its operations do not run on this system: a line after the language's
says so, such as "This system: the C adapter runs on Linux only in this
version.", and no consumer reads ready.
Otherwise it ends with status 0, also when only a run on an example
can confirm a hook, and
without `--for` even when hooks are missing.

`langadapter check` neither checks the shared contract nor compares the
runs. Two functions of [the conformance check](conformance.md) do:
`check_adapter` checks your adapter against the rules every adapter follows,
and `compare_runs` compares the example suite's normal run with each tool's
run of the same suite, which can differ although every hook reads present.
Run both once for each example, from a script in the directory that holds
your file and the example:

```python
import shutil

import langadapter as la
from langadapter.conformance import AdapterExample, check_adapter, compare_runs

# Where your adapter prunes collection breakers: the text of a test file
# whose one unit keeps it from loading. None otherwise.
BREAKER = None

la.load_extension("javascript.py")
context = la.ProjectContext("example", la.Interpreter(path=shutil.which("node")))
suite = la.Suite("example", ("calc.test.js",))
example = AdapterExample("javascript", context, suite, coverage_target="calc.js",
                         collection_breaker=BREAKER)
problems = check_adapter(example) + compare_runs(example)
print("\n".join(problems) or "no problem found")
raise SystemExit(1 if problems else 0)
```

`load_extension` registers the file's language in your own script, as
`--extension` does for the check. `coverage_target` names the example's
source file relative to its directory, and `compare_runs` measures it in
each tool's run. The script prints one sentence for each problem, or "no
problem found", and ends with status 1 when it found a problem.

### Check on an example {#check-on-an-example}

Give `--example` a directory with a small project of your language, a
source file and its test file whose tests all pass. The check runs your
adapter's suite on the directory's test files, found by your test file
rule, and measures coverage and runs mutation testing on the source file
where your capability report reads their tools set up, which can take a
minute or more on a small example:

```text
langadapter check javascript --extension javascript.py --example example --for testuary
```

- Coverage is measured only when the directory holds one source file beside
  its test and setup files, outside ignored and dot directories. With more
  than one, coverage reads not checked, and the line says why.
- A tool the capability report reads missing setup is not run, and its line
  reads not checked with the report's reason.
- With a failing test, the mutation line reads not checked and names the
  failing tests.
- When no test fails on any mutant, a note says that every mutant survived.
  A session that runs the tests on the original source instead of each
  mutant gives that, and so do tests too weak to fail, so the hook still
  reads present and the exit status stays the same.

The example's tests run with the program your `runtime` fact names, the
first one on the PATH of the shell that runs the check. For a language
whose runtime is `python`, activate the project's environment first, and
call `langadapter` by its full path or install LangAdapter in that
environment too. The line after the language names that program and what
the run gave, such as
`Example: 1 test file, run with /usr/bin/node, 3 tests: 3 passed.` The
statuses the check ends with, those of an example included, are above.
