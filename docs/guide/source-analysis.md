# Source analysis

Source operations read and edit source text. Some need only the calling
process, others validate with the project's runtime.

## Source operations

`adapter.sources` gives access to the language's own analysis and edits
without reimplementing them. `prune_tests` and `finalize_source` use parsers in the calling
process and work in contexts without a runtime. Merging, `normalize_shadowed`,
repair and pruning of imports or of collection breakers (what keeps a test
file's tests from loading) use the project's validator and need its runtime.
`normalize_bindings` is static and works without one. A failed validation does not prove the source invalid, and
an edit that is unavailable returns the input with an explicit status. The
adapter's own edit methods, which these operations call, can also be used
directly.

`check_structure(source)` says whether the parser reads the source without an
error. For the first error, `message` names it and its position ("syntax
error at line 3:5"), and `snippet` holds the source text the error covers,
stripped and whole, which can span several lines. The caller decides how much
of it to show.

A source that the library's own parser cannot read is reported, never read as
empty. The typed source operations report `PARSE_ERROR`, `test_metrics`
reports the source as not parsed, and the adapter methods
`duplicate_test_names` and `order_dependent_mock_warnings` raise
`langadapter.SourceReadError`, each naming the parser. `stage_pool` refuses a
member whose imports cannot be read. For Python, that parser is the one of the
Python the library runs on. When the project's Python is newer, the setup
check adds a note, and a reading error names both versions.

For Python, a test is what pytest's default collection finds: a module-level
function whose name starts with `test`, and such a method of a module-level
class whose name starts with `Test` and that has no `__init__` or `__new__`,
or of a class based on unittest's `TestCase` or `IsolatedAsyncioTestCase`,
whatever its name. A pytest fixture, a nested function, a method of another
class and a class that sets `__test__ = False` are no tests. The rule reads
the module's own text, so the methods a class inherits, tests imported into
the module, `Test` classes nested in another and a `TestCase` reached through
another base are not counted. The test count of `test_metrics`,
`duplicate_test_names`, `order_dependent_mock_warnings`, a validity finding's
enclosing test and the fixtures a merge requests for each test follow this
rule.

`merge_sources(first, second)` joins two sources with a separator comment line,
the adapter's `source_merge_separator` (`#` for Python, `//` for Java and C). An
adapter from another package may set its own separator or require the caller
to pass one, and its merge checks it. The library never guesses the separator
from the language name or by running a tool. The Python merge puts the imports
of both sources first, each once. Of any other top-level statement both sources
hold identically, it keeps one copy when the statement only binds names to
objects that exist or cannot change and nothing between the two copies can
change what it binds. A statement that calls something, such as
`sys.path.insert`, or builds a new list or dict stays twice.

`resolve_collisions` keeps native renames, sharing, conflicts and the caller's
preferences. A completed resolution is a `CollisionResolution`, which may
leave out a source the validator judged invalid, naming it in `ejected` with
its reason in `eject_reasons`. If validation did not run, the observation has
no resolution value: its status says why, and its reason names each source
whose validation failed. The adapter's own `resolve_collisions` method
returns that `SourceObservation`: completed with the `CollisionResolution`
and the parser that read the sources, or with the status
`infrastructure_failure`, `timeout` or `missing_setup` and a reason. Any other
answer is refused.

## Pooled suites

`stage_pool(members, setup=...)` stages the test files of several suites as
one pooled suite, so one run measures all of them. Each `PoolMember` names
its suite's slot (letters, digits and underscores), the file's path as the
suite's own standalone run stages it, and its text. `setup` lists the setup
files every suite shares, as (path, text) pairs, staged once and unchanged.
The observation's value is a `StagedPool`:

```python
from langadapter.sources import PoolMember

pool = adapter.sources.stage_pool((
    PoolMember("suite_000", "test_candidate.py", first_source),
    PoolMember("suite_001", "test_candidate.py", second_source),
), setup=(("conftest.py", conftest_text),)).value
suite = pool.write(empty_dir)
```

A Python file keeps its text and gets the slot in its name
(`test_candidate_suite_000.py`), since pytest imports two files of one name
as one module. A Java file's top-level types get the slot in their names,
with every use of them in that suite's files, and the file takes its public
class's new name (`CalcTest_suite_000.java`), since two suites usually
declare the same test class. Names in string literals and in package and
import declarations stay as they are. The rename is the one the Java merge
repair applies, and `langadapter.java.source_analysis.rename_class`
applies it to one source: a type's uses include the qualifier of a method
or constructor reference (`Helper::new`), while method names, annotation
element names and labels keep their text. A source that also declares a
method, a variable, a parameter, a field or an annotation element named
like the type cannot be renamed safely, and the observation says which.

`pool.original_test(test)` returns the slot of a pooled run's test and the
test as the suite's standalone run identifies it. `pool.slot_of_killer` and
`pool.original_text` do the same for a killer as the engine printed it.
Each `StagedFile` keeps its staged text, its hash and the renamed types.
Its `self_references` lists where the file refers to its own module or
class name in a way the staged name can change at run time: the name in a
string, the name or file read at run time, or an import of its own class.
A Python file also lists the name of another of its suite's modules in a
string, as for `importlib`. Such a suite's verdicts in the pool may differ
for that reason alone.
`pool.derive(kill_matrix, slot, covered_lines=...)` derives a suite's kill
matrix from the pooled run's saved matrix, by the rule
[Mutation sessions](mutation-sessions.md#measured-and-derived-results)
describes, and `pool.killed_by` lists the mutants the suite's tests killed
there.

A pool that cannot be staged is an observation with a reason: a Java file
that does not parse (`parse_error`), one not named for its public class or
whose types cannot be renamed safely, a Python file that imports another
file of its own suite, whose module the pool renames, or names that would
still collide (`unsupported`). A pooled run is not a standalone run of each
suite: a test that depends on another suite's tests, on their order, on the
process they share or on its own file or class name can give another
verdict in the pool.

## Static analysis

`langadapter.python.source_analysis` provides normalization, collision
analysis and merging without starting project tools. Normalization keeps the
rule, the removed and retained spans and the parse errors. When two sources
define the same name, collision rewriting needs the caller to say whose
definition wins. Without that, conflicts stay unresolved.
Priorities based on measured coverage, kills or other goals belong to the
caller.

These operations use the calling process's grammar. Rewritten sources need
validation with the project's interpreter before delivery, and the
context-bound source operations on `PythonAdapter` include that validation.
Read-only skeletons, fingerprints, imports and structural metrics start no
tools. Source that does not parse has unknown structural metrics.
`count_code_lines` counts code lines as `nloc` does, with lizard, which also
accepts incomplete source. It is not a count of coverage statements.

## Metrics

`adapter.sources.metrics(source, names=["nloc", "cyclomatic_complexity"])`
returns immutable file and function scopes, with the provider, parser,
definition version and units on each value. Python and Java report eight
structural counters for the whole file and for each function, and C for the
whole file only: `nloc` (lines
that hold code, not counting blank lines, comments or Python's docstrings),
`function_count`, `class_count`, `loop_count`, `loop_max_nesting`,
`cyclomatic_sum`, `recursive_function_count` (functions that call their own
name) and `io_call_count` (calls to the language's file, socket and process
functions). For a function, the counters cover its whole text, nested functions
included. A ninth value, `cyclomatic_complexity`, is reported only for
functions and leaves nested function bodies out. `nloc`, `cyclomatic_sum` and
`cyclomatic_complexity` are [lizard](https://github.com/terryyin/lizard)'s
count of code lines and its McCabe complexity, and their definition version
names lizard's version: `lizard-<version>-1`, and `lizard-<version>-own-body-1`
for `cyclomatic_complexity`. Read alone, a Java method declared without a
body, such as an abstract or interface method, or a Python function whose
body follows its signature on the same line, such as the stub `def f(): ...`,
holds no function for lizard. Its `cyclomatic_complexity` is a value with
status `unsupported` and no number, and where it is not nested in a function,
it adds nothing to `cyclomatic_sum`. A function that lizard does not report
when it reads the function's own text, such as a Python function holding a
one-line `def` with a body or a Java method holding an anonymous class with a
field, also gets an `unsupported` `cyclomatic_complexity`. The other six
counters follow the language's tree-sitter grammar
(`tree-sitter-structural-1`). These are static hints, not execution
measurements or checks of semantic equivalence.

`adapter.sources.metric_definitions()` lists the definition of every metric
the adapter's `metrics` operation measures. `langadapter.sources.STANDARD_METRICS`
names the metrics a tool may request from any language, each a
`StandardMetric` with its meaning, unit, the scopes a provider may report
and the operation of `adapter.sources` that reports it: `metrics` for a
metric of a file's source text, `call_statistics` for the two
[call statistics](#call-statistics). A tool that requests metrics in
several languages sends each name to the operation the table names. A
standard metric a language does not measure is unavailable, with the status
`unsupported`, the provider `langadapter.standard` and a reason that names
the language and the metric's operation, never a number. A name that is
neither standard nor measured is refused as an invalid request. A listed
name the table does not hold is the language's own metric of the source
text.

Qualified names and the original inclusive line spans identify occurrences.
Java also keeps the parameter types as written. Declarations with the same
name on different lines stay separate. Declarations that only their column
could tell apart are refused rather than merged. Parse or setup failures appear
as unavailable values, never zero, and valid source without functions has an
empty function tuple. Read the `unavailable` entries as well as the successful
scopes. More metrics can be registered on one adapter with
`adapter.sources.register_metric_provider`, which
[A metric provider](../extending/metric-provider.md) describes.

## Project source files

`adapter.sources.project_source_files()` lists the project's source files.
The paths are relative to the
context's root, in POSIX form and sorted. They are the files with the
language's file extensions, outside the directories the adapter ignores
(`ignored_dirs`, by default the usual version control, environment,
build, cache and test directories) and the directories whose name starts with a
dot. The list leaves out the file names the adapter declares as no sources
(`non_source_files`, such as Python's `setup.py`), the test files
(`is_test_file`) and empty files. No file's content is read, so a file that
does not parse stays in the list.

## Call statistics

`adapter.sources.call_statistics(target)` estimates how widely the
functions of the source file `target` are used. `target`
is a path relative to the context's root, as `project_source_files` lists
it. The result is `SourceMetrics` with the function scopes `metrics`
reports: for each function, `call_site_count_estimate` counts the call
sites of its bare name, `name(...)` or `anything.name(...)`, in the
project's other source files, and `calling_file_count_estimate` counts the
files that hold one. The file scope holds their sums over the functions,
each calling file counted once.

The search covers the files `project_source_files` lists, the target left
out, so calls in test files are not counted. Names are matched, not
resolved, so the values are estimates and their definition version is
`name-match-1`. A call of another function with the same bare name counts.
An import under another name, a reference that is not a call (a function
passed as a value, a decorator without arguments) and a call through
`getattr` or reflection do not. A project file that does not parse is left
out, and the values' reason names it. A call site of a bare name that two of
the target's functions share counts once for each of them in the file's sum.
A target outside the context's root is refused as an invalid request, and
another spelling of the target's path still leaves the target out of the
search. A target that does not parse gives
the status `parse_error`, and a language without the call search
`unsupported`, never zero. Python and Java search with their tree-sitter
grammars. C has no call search, so its result is `unsupported`.

## Collection breakers

`prune_collection_breakers(source, timeout_s=...)` removes what keeps a test
source's tests from loading, so that the rest of them run. Each round loads
the tests as the language does, by collection or compilation, and removes
the smallest unit the failure names. Python loads the source at most five
times, so it removes at most four units. Java and C remove at most five units
and load once more to confirm. The result is
adopted only once the tests load, and `removed` names each unit removed. The
unit is the language's own, and each adapter declares it in
`collection_unit`:

- Python: the smallest module-level or class-level statement around the line
  that pytest's collection traceback names, replaced by `pass`. A test that
  fails when it runs is not a collection breaker.
- Java: the smallest declaration around a line javac reports an error on, or
  around the line of a static initialization that throws: an import, or a
  member of a type (a method, constructor, field, initializer or nested
  type). A test method that does not compile is removed with it, as a helper
  or field whose removal breaks the methods that use it is in the next round.
  A class that compiles runs once only when its static initialization could
  throw (a static block, or a static field whose value is not a literal).
- C: the utest.h test, fixture block or top-level declaration around a line
  GCC reports an error on, as the entries of
  [`CAdapter.collection_unit`](https://k-kammler.github.io/langadapter/reference/c/#langadapter.c.adapter.CAdapter.collection_unit)
  and
  [`CAdapter.prune_collection_breakers`](https://k-kammler.github.io/langadapter/reference/c/#langadapter.c.adapter.CAdapter.prune_collection_breakers)
  state.

The result is `not_repaired`, with the input, when the failure names no unit
that can be removed (for Java, an error in the package or a class's header),
when no test is left, or when the tests still do not load after the last round.

## Syntax repair

`langadapter.python.source_analysis.salvage_syntax` takes a compilation
validator and applies a bounded repair at the locations it reports. Its
result keeps the last validation and tells invalid source from a validator
that did not run. It starts no process beyond those the validator starts.
`PythonAdapter.salvage_syntax` supplies the project's validator.
