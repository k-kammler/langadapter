# C projects

`langadapter.c.adapter.CAdapter` runs the tests of one C file of a project,
measures the file's line and branch coverage with gcov (read through gcovr),
reports each test's own coverage, and measures the file's mutants with Mull.
It works on Linux only. On another system each of its operations and its
capability report read missing setup with "the C adapter runs on Linux only in
this version", which on Windows adds "on Windows in WSL", and
`langadapter check c` names it in its second line and ends with status 1.

## What it needs

- LangAdapter with the extra for C, `langadapter[c]`, which installs
  tree-sitter-c, gcovr and setuptools. The
  [home page](https://k-kammler.github.io/langadapter/#install) gives the
  command.
- The project's GCC with the gcov of the same release, as most Linux
  distributions install them together, of a version
  [Tested environments](../reference/tested-environments.md#c-projects)
  names. The context names that GCC as
  its interpreter, such as
  `ProjectContext(root, Interpreter(path="/usr/bin/gcc"))`. For Python tests
  of a target built into an extension, the context names the project's
  Python, and the adapter builds with `c_compiler`, else `CC`, else `cc` or
  `gcc` on PATH. For C tests, `CAdapter.discover_runtime` finds the GCC for a
  setup in the same order. The adapter reads PATH, and for an extension's
  build `CC`, from the commands' environment: the context's `environment`
  over the calling process's variables, unless the context replaces them.
  It finds cmake, ar, prlimit and taskset on that PATH too, and without a
  PATH it looks in the system's default path (`os.defpath`).
- For utest.h tests (`c_tests` `c`), stdbuf (coreutils) on that PATH. A
  suite run and a run as delivered start each test program under it, which
  keeps the program's output line-buffered, so the lines before a crash or
  the timeout are kept. Without it, every operation that runs the project's
  tools and the capability report give missing setup before anything is
  built.
- For mutation, Mull for an LLVM release `N`, and clang `N`, as
  [Tested environments](../reference/tested-environments.md#c-projects)
  states. The adapter takes `mull-runner-N` from `c_mull`, else the newest on the
  context's PATH, Mull's IR frontend from the `lib` directory beside the
  runner's `bin`, and clang from that prefix's `lib/llvm-N/bin`, else
  `clang-N` on the context's PATH. This is the layout of Mull's and Ubuntu's
  packages, also when they are unpacked with `dpkg -x` instead of installed.

The C tests use utest.h, which LangAdapter ships, so a project needs no test
framework of its own.

The setup check, `langadapter.inspect_project("c", request)`, reports
these needs as items: `runtime` for the compiler with its gcov, which it
starts to read their versions, then `tree-sitter-c`, `gcovr` and
`setuptools`, `stdbuf` for utest.h tests, `prlimit` when
`c_address_space_limit` is set, and `mull` for Mull with its clang, required
when the request names a mutation operation. A missing item gives its next
step. The tool check, `langadapter.verify_project`, also starts each tool it
found once. Without a usable compiler both read missing setup with the
compiler's reason, and on another system than Linux with the system's
refusal. Python tests of an extension have no C setup check yet.

## How it builds

The adapter never builds in the project's own directory.

- **An autotools project, with a generated `configure` in its root:**
  `configure` runs in a build directory of the adapter's, outside the
  project's tree, with the compiler recorder as `CC`, then `make -j4` (or
  the `c_build` command) runs there. The project's tree stays unwritten when
  its `configure` builds out of tree without writing into its source
  directory. Such a project builds this way also when it has a Makefile, a
  `CMakeLists.txt` or a `setup.py`, unless `c_build_kind` names another
  kind. With `c_build_cache`, the build runs on a copy of the project in
  that directory, and later adapters with the same build inputs, build
  command and compiler use it.
- **A project with a Makefile:** the project is copied into a directory of the
  adapter's, and `make` (or the `c_build` command) runs there once, with a
  compiler recorder as its compiler.
- **A CMake project:** `cmake` configures and builds it once in a directory of
  the adapter's, outside the project's tree, with the compiler recorder as
  its C compiler and archiver. `c_build` holds further options of its
  configure step, such as `-DNAME=VALUE`.
- **A project whose `setup.py` declares extensions:** the project is copied
  into a directory of the adapter's, and setuptools builds the target's
  extension there with the project's Python, for Python tests. C tests of
  such a project need another kind, which `c_build_kind` names.
- **A project without a build file:** the target compiles alone with the
  flags in `c_flags` and links with `c_link`. C sources `c_link` names,
  also as patterns such as `src/*.c`, are compiled once with `c_flags`, the
  target left out.

Every later build replays the recorded compile of the target on a copy of its
source, with coverage counters when coverage is measured.

## Tests

A file of C tests is named `test_*.c`, and a new test file for a source
file such as `calc.c` is named `test_calc.c`. Python tests of an extension
(`c_tests` `python`) are named as Python's: `test_*.py` or `*_test.py`, and
a new one `test_calc.py`.

Each test file is one program, and a run runs it whole, all its tests in
one process in their order of definition, as a user who copied the file
runs it. When a test crashes, the run starts the program again without
the tests that crashed, in a new, empty working directory. A test keeps
the outcome of the first process that ran it, and the coverage comes from
the last, complete process. A hang ends the run in the test that was
running. A program that a signal ends after its last test, or that exits
with a status other than 0 although no test failed, as by an exit handler,
ended badly. Beside a failing test the program's exit status does not count, since a main
of the file's own may return any status, and the result lines decide. A
program that ended badly, or that cannot write its coverage counters, as
after a lowered open-files limit, leaves the run without coverage, and so
does a run in which a test the program lists has no outcome. A name in the
output that the program does not list gets no outcome of its own. Each
test of such a run then runs in a process of its own. A test whose own
process ends badly after its result line, or writes no counters, is the
error, and every other test keeps its outcome. When no test's own process
writes counters, the environment keeps them from being written. That
reason, or the program's bad end when no test is to blame, makes a run that
otherwise completed an infrastructure failure, and is added to the reason of
a run with another outcome. A run whose timeout
ends these processes is timed out. A program's stderr goes into its
stdout, so each test's trace holds its own lines of both, and the run's
output holds them in the order they came. Per-test evidence and
mutation runs run each test in a process of its own. A test file
ends with `UTEST_MAIN()`, and the source
operation `finalize_source` appends it to a file that has tests and no main.
A test looks like this:

```c
#include "utest.h"
#include "calc.h"

UTEST(clamp, below_the_range) {
    ASSERT_EQ(0L, clamp(-5, 0, 10));
}

UTEST_MAIN()
```

A test file that does not compile or link ends the run as a collection
failure that carries the diagnostic `compile_error`. Its message is the
step that failed, and the run's reason detail holds the compiler's or
linker's error lines, with the test file named as the suite names it,
utest.h by its name and the project's files by their paths in the project.

A test that corrupts memory or relies on undefined behavior can leave
counters from which gcov reads a negative count or one of 2^32 or more,
and gcovr refuses them. A run that measures coverage then has no coverage,
its tests' outcomes stay as they ran, and it carries the diagnostic
`coverage_counts_inconsistent` with gcovr's message. Both diagnostics are
[`RunDiagnostic`](https://k-kammler.github.io/langadapter/reference/runs/#langadapter.runs.RunDiagnostic)
records. A run whose tests all
passed ends as an infrastructure failure with that message as its reason,
and a run with another outcome, such as failing tests, keeps it. The run
then reads each test file's counts alone and runs each test of a file whose
counts gcovr refuses alone, within the run's remaining time. Each test whose
own counts gcovr refuses gets a `coverage_counts_inconsistent` diagnostic of
its own, with the test and its message. The search changes nothing else of
the run, and when the time runs out first, or one of its reads fails in
another way, it names no test.

Per-test evidence runs every test alone and names in
`PerTestEvidence.stopping_tests` each test that did not pass, each test
whose own counts gcovr refuses and each test whose process wrote no counters
file. A failing test decides its outcome and reason, as the first one met.
Evidence without a failing test, a timeout or a failed listing ends as an
infrastructure failure with the reason of the first test whose counts gcovr
refuses or that wrote none, also when no test is named: gcovr's message, or
"<test>: no coverage counters were written for it". A timeout or a failed
listing keeps its own outcome. A process
that executes nothing of the target still writes a file of zero counts, so a
missing file means a process that ended without writing one, as by `_exit`.
ctest's tests and a project's own programs read a missing file as zero
counts, since a ctest test may run no program of the target. Per-test
evidence of Python tests of an extension names a test whose counters
directory holds no counters file or whose counts gcovr refuses, and a test
whose node id maps to no counters directory of the plugin is an
infrastructure failure that names no test.

### Known limits

- A crash is blamed on the last test whose start reached the output, with
  the outcome `error`, while a test whose result line failed has the
  outcome `failed`. A test that damages memory and passes keeps its pass,
  and a later test that crashes from the damage is blamed. A test that
  switches stdout to full
  buffering and passes can take a later test's crash, and its lines then
  leave the coverage.
- A test that passed in the first process of its program and crashes in a
  restart keeps its pass. The next restart leaves it out, so its lines
  leave the coverage.
- When a restart program does not link, the tests after the crash have no
  outcome and do not run in processes of their own.
- When the counts of every test of a program are refused or missing, the
  failure is the program's, as an exit handler's, and no test is named. So
  a program with a single test never names it for its counts. A file whose
  counts gcovr refuses only when its tests run together, while each test's
  own counts read, names no test either.
- A function ends at its last line that gcov lists, so excluded lines
  shorten it, and a function that gcov excludes stays listed. A mutant on
  such a line maps to no function, runs every test and is never reported
  as not executed.
- In a suite run and in a run as delivered, each test program starts
  through `sh` under `stdbuf`. `stdbuf` works through `LD_PRELOAD`, so the
  program's environment holds `LD_PRELOAD`, which a test can read. `sh`
  writes the program's stderr into its stdout and can add `PWD` to that
  environment.

### A project's own test programs

With `c_tests` set to `programs`, the suite is the project's own tests in
the form some projects use: one program per module whose `main` runs a
fixed list of test functions. A program cannot run one of its tests by
name, so each program is one test, named `main`, judged by its exit
status. An `assert` that fails ends it by SIGABRT and fails it. Test files
compile with the target's shared flags. The suite's setup files (the C
sources of a framework, such as `framework.c`) compile with them too and
are linked into every program. This is how a program measures a project's
own suite as a baseline.

### A CMake project's tests as ctest runs them

With `c_tests` set to `ctest`, the suite is a CMake project's whole own
suite as its test runner defines it. The suite names the file that defines
the tests (its `CMakeLists.txt`). ctest lists the tests from the adapter's
build of the project. The program each test runs is relinked from the
build's record with the target's build of the run, and the test runs as
ctest runs it: its arguments, working directory, environment and timeout,
judged by ctest's rules (exit status, pass, fail and skip patterns,
`SKIP_RETURN_CODE`, `WILL_FAIL`). A test the adapter cannot run that way
(one without a command or a working directory, a script, a program that
does not link the target, a test ctest disables, a fixture step, a test
with `ENVIRONMENT_MODIFICATION`) is reported skipped with the reason.

## Mutation

Mull's frontend compiles every mutant of the target into one build, each
switched on by an environment variable. The adapter builds the target that
way once, links it as the project links it, and runs the tests with one
mutant on at a time, so each mutant has its own killing tests. A mutant in
a function no test executed survives without a run. The default operators
are Mull's default group (`cxx_default`), and the set `all` is Mull's whole
group.

Mull's build uses clang where the project uses GCC, and leaves out the
flags clang cannot take: link-time optimization, the debug prefix maps and
any flag clang refuses. A run records each difference in its `mull_build`
setting, and refuses a suite that passes on the project's build but not on
Mull's, naming them.

Before the first mutant, the run checks each test alone on the coverage
build and on Mull's build. A C test that does not pass on either makes
the suite red, `test_failures` with every such test in
`MutationRun.stopping_tests`. A test whose own counts gcovr refuses, or whose
process wrote no counters, ends the run as an instrument check
(`instrument_check`) that names it there too, and so do Python tests of an
extension that do not pass on either build. The check goes on within the
run's wall clock: once a hanging test has used it up, each later test has
one second. Such a test did not stop the run, is not named and does not
count toward every test of its program. A check whose every failing or
refused test started after the time was spent ends as `timed_out`, names
no test, and is marked incomplete (`wall_clock_limit`).

The check also times the suite as its mutants run it: each C test alone
on Mull's build, or an extension's Python tests in one process. A
`TimeoutRule` as `per_mutant_timeout` applies to that time, and the run
records it as the rule's `passing_suite_s`. A request that gives
`passing_suite_s` itself is refused. Whole seconds are a fixed time for
each mutant. A mutant's tests that executed its function run one after
another within that time. A test the time cuts while it runs makes the
mutant a timeout. When the time ends between two tests, no test failed or
ran out of time, so the mutant is untested with the reason in its row,
and the run is marked incomplete (`untested_mutants_in_catalogue`).

A storage stall can keep LangAdapter's process layer from confirming the
end of a test process within its bounds. When a C test's process of a
mutant's run fails that way, the adapter disposes of the process again once
the stall has passed and runs the mutant once more. A second such failure
leaves the mutant untested with the reason in its row, and the run
completes, marked incomplete (`untested_mutants_in_catalogue`). Each retry
and each mutant left untested is written to the process's stderr as a
`RuntimeWarning` that names the mutant and the test. The summary lists
each retried run's mutant and test (`retried`) and counts them
(`retried_runs`). A process whose end is not confirmed by the run's wall
clock limit ends the run with the failure, since it may still run.

## What it does not provide

The C adapter reports these operations as not implemented
(`operation_capabilities()`), and a request for one is unsupported:

- the source operations `check_structure`, `imports`,
  `import_path_candidates`, `merge_collisions`, `resolve_collisions`,
  `normalize_bindings`, `normalize_shadowed`, `code_smells` and
  `call_statistics`
- the test operations `collect_suite`, `benchmark_warmed` and
  `profile_suite`

Python tests of an extension (`c_tests` `python`) have Python's
`check_structure`, `imports`, `resolve_collisions`, `normalize_bindings` and
`normalize_shadowed`, and ctest's tests (`c_tests` `ctest`) have no
`stage_pool` either.

A run of C tests refuses as unsupported a request for a side effect the
adapter does not provide: `record_leaks`, `session_seed` (0 included),
`reverse_order`, `isolation_config` or `remove_created_files`. C runs
locally only, never in a container, as [Containers](containers.md) says.

## Settings

| Setting | What |
| --- | --- |
| `c_compiler` | the GCC that builds the project and its tests (alias `CC`) |
| `c_build_kind` | `make`, `cmake`, `configure`, `setuptools` or `flags`, else discovered from the project |
| `c_build` | the build command, when not plain `make`: for `make` it runs in the adapter's copy of the project, for `configure` in the adapter's build directory after `configure`, for `setuptools` in the adapter's copy in place of `setup.py build_ext`, and for `cmake` it holds the configure step's options |
| `c_flags` | the target's compile flags, for a project without a build file |
| `c_tests` | `c` (utest.h), `python` (a target built into a Python extension), `programs` (the project's own test programs) or `ctest` (a CMake project's tests as ctest runs them), else discovered |
| `c_link` | the objects, libraries and C sources (also patterns) the target links with, for a project without a build file |
| `c_build_cache` | a directory that keeps a `configure` build for later runs, instead of building once per adapter |
| `c_target` | the C file the tests are for, when a request does not name it |
| `c_mull` | Mull's runner (`mull-runner-N`), when not the newest on PATH |
| `c_test_prelude` | headers every test file includes first, each passed as `-include`, as the project's own tests include them (such as `stdlib.h test/alloc-testing.h`). Setup files do not take them |
| `c_ctest_arguments` | extra arguments for named ctest tests, after their own: `NAME ARGUMENT...` entries separated by `;` (such as `fuzz_test {seed}`). `{seed}` is 1, since a C run carries no session seed, so a test that seeds itself from the clock runs the same each time. A name ctest does not list is refused |
| `c_test_support` | the sources the project's own test programs build with besides the target (`c_tests` `programs`), such as `test/framework.c test/alloc-testing.c`: the adapter gives them as the suite's setup files |
| `c_test_environment` | variables every process of the adapter's runs gets, Python tests of an extension among them, as `NAME=value` words such as `CK_FORK=no`. Unlike the other settings, they reach the processes as variables of their own, not as one `LANGADAPTER_C_` variable |
| `c_address_space_limit` | the most address space each test process may reserve, such as `2G` or a number of bytes. Each test process the adapter starts runs under `prlimit --as` with it (Python tests of an extension under the same `RLIMIT_AS`), while the build and the compiler do not. Empty, the default, sets no limit. The run's identity names the limit (`address_space_limit`) |
| `python_executable`, `python_version`, `requirements`, `python_import_root`, `frozen_module_strategy`, `pytest_config` | Python's settings for Python tests of the target's extension: they choose and set up the Python that runs those tests as for [Python projects](python.md), and `python_import_root` names the directory the tests import the project's package from. `c_test_environment` gives those tests' processes their variables, so the adapter takes no `python_test_environment` |

Programs that use LangAdapter pass C's own settings to the adapter as context
variables (`LANGADAPTER_C_TESTS`, `LANGADAPTER_C_COMPILER`, `LANGADAPTER_C_BUILD_KIND`,
`LANGADAPTER_C_BUILD`, `LANGADAPTER_C_FLAGS`, `LANGADAPTER_C_LINK`,
`LANGADAPTER_C_BUILD_CACHE`,
`LANGADAPTER_C_TARGET`, `LANGADAPTER_C_MULL`, `LANGADAPTER_C_TEST_PRELUDE`,
`LANGADAPTER_C_CTEST_ARGUMENTS`, `LANGADAPTER_C_TEST_SUPPORT`,
`LANGADAPTER_C_ADDRESS_SPACE_LIMIT`). `LANGADAPTER_C_COMPILER` chooses the
compiler only for Python tests of an extension. Of Python's settings only
`python_import_root` has a variable, `LANGADAPTER_C_PYTHON_IMPORT_ROOT`. The
others choose the Python that runs the tests when the context is made.

A test process that reaches `c_address_space_limit` can reserve no more
address space: an allocation fails (`malloc` returns NULL, and Python raises
`MemoryError`), and a process that does not handle the failure, or grows its
stack past the limit, crashes. The run reports that test as failed or
errored, as any test that fails or crashes. In a process that runs several
tests (a run as delivered, or the pytest of an extension's tests), the
crash also leaves the tests after it without an outcome. In a mutation run
it kills the mutant, so a mutant that makes a test allocate without end is
killed instead of exhausting the machine's memory. Without a limit, the
default, a test process reserves what the system grants.

A test process's output is kept whole up to 2 MiB of each of its streams,
whatever the limit. A stream that writes more keeps its first and its last
MiB, each cut at a line break, and between them the line
`[... N bytes of the test output omitted ...]`, followed by utest's RUN and
result lines from the part left out, so every verdict reads as from the whole
output. A utest line longer than 64 Ki characters in that part is not read.
A ctest test that has `PASS_REGULAR_EXPRESSION`, `FAIL_REGULAR_EXPRESSION` or
`SKIP_REGULAR_EXPRESSION` keeps its whole output, which those expressions
judge, and so does a run in a container.
