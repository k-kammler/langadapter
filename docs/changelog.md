# Changelog

Each release's changes, newest first.

## 0.1.0

First public release. It provides:

- the distribution and import package `langadapter` with the base class
  `LangAdapter`
- Python and Java adapters with suite runs, coverage, per-test evidence,
  collection, delivered-suite checks, benchmarks and mutation sessions
- Java projects built with Maven or Gradle and tested with JUnit 4, 5 or 6,
  with JaCoCo for coverage and PIT for mutation, and Java tests run with
  assertions on unless the build turns them off
- C projects on Linux (`langadapter.c`, with the extra `langadapter[c]`),
  tested with utest.h, pytest for a C file built into a Python extension, the
  project's own test programs or ctest, with gcov for coverage and Mull for
  mutation
- warmed JVM timing for Java suites, locally and in Linux containers
  (not tested in containers)
- prepared project copies (`prepare_project`), locally and in Linux
  containers you run yourself, while an adapter created on the original
  context runs in place
- an environment inside the project, such as its own `.venv`, admitted in a
  prepared copy's values on the host when nothing in its site directories leads
  into the project outside it, and left out of the copy
- a quick check and a tool check, with a next step for every item they could
  not confirm, where the tool check also tries a run's steps in a copy of the
  project without running a test: for Java resolving the build (offline unless
  the request allows acquisition, and only with acquisition where the build's
  launcher is a wrapper, which may first download its build tool), compiling
  and discovering the tests and starting JaCoCo and PIT where an operation
  needs them, and for Python collecting the tests and checking where they
  import the project from
- source operations and structural source metrics for Python, Java and C, which
  report a source their parser cannot read as unreadable (`PARSE_ERROR`)
- merging two test files of a language with `sources.merge_sources`, which
  checks the merged file with the project's own tools and refuses a pair it
  cannot merge
- removal of what keeps a test file from loading, by each language's own
  unit, which the adapter declares: for Java, the declarations javac rejects
  and a static initialization that throws
- per-test evidence that names the tests a measuring run's failure is
  pinned on (`PerTestEvidence.stopping_tests`), and removal of named
  tests from a test file in Python, Java and C (`sources.prune_tests`),
  so that a program can remove the tests a measurement names
- extensions named by a file or an installed module (`load_extension`,
  `resolve_extension`), each adding a language or a metric provider, which
  it names as `langadapter_language` or `langadapter_metric_provider`, with
  no installation or registration step, its file found without running it
  and recorded with its SHA-256 (`Extension`, `Registration.extension`), a
  module that only its package's code makes findable told apart from a
  missing one (`ExtensionFoundOnlyByImport`), and a consumer's requirement
  profile as the file `langadapter_profile.json` in its module's
  directory, found without importing it
  (`consumers.Installed.profile_files`)
- a check of a language from the command line (`langadapter check
  LANGUAGE`), with `--extension ITEM` to load the extension that adds it,
  `--for` to check it against a consumer's levels, named by its module or by
  its requirement profile file, with forward slashes or the system's
  separator, and
  `--example DIR` to run the language's suite on an example directory's test
  files, which exits with status 1 when a hook reads failing. Each fact and
  hook reads present, default used (such as `kill_tokens`, whose default
  gives each killing test its mutant's token), missing, failing or not
  checked with the reason, and the check ends with the counts of each
- registration of adapters in code (`register`), with a conformance check
- `langadapter toolchains LANGUAGE...`, which installs the named languages'
  tools on Ubuntu 24.04 on x86_64, also in WSL and in a container, and proves
  each with `langadapter check` on an example
  ([Install the toolchains](guide/toolchains.md))
- metrics requested by name in any language (`STANDARD_METRICS`,
  `sources.metric_definitions`), a language package's own metric providers,
  the project's source files, call statistics estimated by name, and suite
  profiles for Python with cProfile
- a Python run that leaves out the plugins changing its time
  (`PytestOptions.leave_out_timing_plugins`): pytest-cov's tracer,
  pytest-xdist's workers and pytest-benchmark's measurement, where the
  project's pytest loads them, with the project's own options for them
  kept valid
- a Python run and mutation session that remove the empty `.benchmarks`
  directory pytest-benchmark makes in the project root when they made
  it, so neither writes into the project, while a storage that was there
  before or holds saved results stays
- the project's own test files as its runner configures them
  (`project_test_files`), for Python bounded by the configuration's
  `testpaths`
- mutation results for several test suites of one source file, each the
  result a run of that suite alone gives, with a conformance check that a
  faster way of computing them gives the same results
- `CancelScope`: stopping any operation from another thread, with
  `OperationCancelled` in place of a result
- `recovery_owners(error)`: the owners whose cleanup an exception leaves
  unconfirmed, such as a mutation session, a prepared copy or a command still
  unresolved, each once
- commands on macOS and other POSIX systems, expected to work but not
  tested, in a process group without confirmed cleanup (a warning,
  observations record it)

### How results are measured

- When the per-mutant time limit ends a mutant's run in the middle of a
  test, that test is recorded. A test that failed before the limit counts
  as the mutant's killer.
- A Python test's coverage includes its fixtures' setup and teardown. A
  fixture shared by several tests counts for the first of them, and its
  teardown for the last.
- Every saved mutant row carries the mutant's `key`, which names the same
  mutant in every run of one target and catalogue, as
  [Mutation sessions](guide/mutation-sessions.md) describes.
- A Python run that imported the module under test from another file is
  refused, since its coverage would be wrong. In a prepared copy, a run that
  names no module under test is refused too when its tests executed one of
  the copy's modules from a file outside the copy, such as the original
  project's. A mutation run whose suite fails without a mutant is refused as
  well, since every mutant would count as killed.
- A Python run without a bytecode store, such as a run in the project directory
  itself, compiles into a cache directory of its own, so a source rewritten
  within the same second at the same size runs as it is written.
- A run with a collection error or without tests reports its coverage as
  absent (`coverage` is `None`).
- A Python environment provisioned for a requested version is kept per set
  of its inputs, the requirements file's location among them: a
  provisioning with other inputs makes another environment beside it and
  removes none in use. A relative entry in the requirements file resolves
  against the file's directory.
- A Cosmic Ray result without a test outcome counts as untested, and so
  does an incompetent one (status `untested`, subcategory `INCOMPETENT`),
  where running the tests or applying the mutation raised. Cosmic Ray's
  cr-rate counts it as killed. A failed request to an HTTP worker or an
  incompetent result marks the run incomplete, a job waits for its
  worker without a limit of its own, and a run whose session has a stopped
  worker stops before its first mutant.
- In a container, a stopped Cosmic Ray launcher writes the target the local
  distributor mutates back before it exits, and the session restores it
  from the original's copy beside the session when the launcher ended
  first.
- When `cosmic_ray_local_session_dir` (`CosmicRaySettings.local_session_dir`)
  names a node-local directory, a Cosmic Ray session without `session_dir`
  keeps its live database below it, and a directory that cannot hold it
  refuses the run rather than moving the session.
- In a mutation score, an untested mutant counts as not killed. A mutation
  run that measured nothing, because a step failed or the suite does not
  pass, is not complete (`run.complete` is `False`). A kill matrix marked
  incomplete has no score, and
  `langadapter.mutation.matrix_lower_bound_percent` gives its lower bound.
- A kill matrix of schema version 3 names each mutant's status with one
  word and constant: killed, timeout, non_viable, survived, untested or
  skipped (`KILLED_STATUS` and the others). The detected mutants, those of
  `DETECTED_STATUSES`, are the class `MutationClass.DETECTED`, a non-viable
  one with the subcategory `NON_VIABLE`, and the summary counts them under
  `detected`. A suite without a result for a mutant has the per-suite
  verdict `untested`. `upgrade_kill_matrix` reads a matrix of an earlier
  schema version in these words, and every reader of the module reads
  through it. A C mutant under which the Python extension did not load,
  killed in such a matrix, reads as non_viable, as the C engine records it.
- The rules for valid generated tests are each language's own.
- A Python run can record the tests that add, rebind or remove a name of
  the module under test, or that change numpy's error or print settings or
  xarray's options when those are loaded (`record_leaks`), also a run as
  delivered. The record comes from a read-only observer loaded on the
  command line, which compares each test's start and end and restores
  nothing, so a test that undoes an earlier test's change is listed too.
  A run as delivered takes the module under test, the timeout journal, the
  caller's traceback and its first-failure stop.

### Requirements

- Python 3.10 or newer for the library, tested with 3.10 to 3.14, on
  Windows or Linux. macOS is expected to work, but this release has not run
  on a Mac.
- For a Python project: pytest in the project's environment, with coverage
  and Cosmic Ray for coverage and mutation. Tested with pytest 8.1.1,
  coverage 7.6.8 and Cosmic Ray 8.4.1, on Python 3.14 and 3.15 with
  pytest 8.4.2 and coverage 7.16.2, and project Python 3.10 to 3.15.
  Python 3.9 is expected to work. The library itself is not tested on
  Python 3.15, as [Tested environments](reference/tested-environments.md#not-tested)
  describes.
- For a Java project: JDK 11 or newer, tested with 11, 17, 21, 25, 26 and
  27, and the project's Maven or Gradle build. The library downloads the
  JUnit launcher, JaCoCo and PIT on first use, unless
  `LANGADAPTER_JAVA_TOOLS_OFFLINE` is set to 1.
- For a C project: Linux, the extra `langadapter[c]`, GCC 7 or newer with its
  gcov, and for mutation Mull with clang of the same LLVM release, as
  [Tested environments](reference/tested-environments.md#c-projects) states.

[Tested environments](reference/tested-environments.md) lists the tested
combinations in detail.

### Known limitations

- Some Python source operations read the project's code with the Python
  that runs the library, so code written for a newer Python than that one
  can be refused.
- Adding a language takes an adapter with several capability groups, as
  [Add a language](extending/add-a-language.md) describes.
- A Java project with both Maven and Gradle build files is refused.
- C runs have the limits that [C projects](guide/c.md#known-limits)
  lists, such as how a crash is blamed on a test.
- Conda, uv, Poetry and Pipenv environments were not tested beyond their
  interpreter.
- On macOS and other POSIX systems, the library cannot confirm that every
  process a command started has ended.

### Documentation

The guides and the API reference are on the
[documentation site](https://k-kammler.github.io/langadapter/), starting with
[Getting started](https://k-kammler.github.io/langadapter/getting-started/).
They are also in the repository's
[docs directory](https://github.com/k-kammler/langadapter/tree/v0.1.0/docs)
and installed with the package under `share/doc/langadapter/docs`.
