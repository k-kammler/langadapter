# Prepared copies

`prepare_project` creates a disposable copy of a project and a separate
snapshot of its inputs. An adapter created on the original context still runs
in place: only adapters created inside `use()` run in the copy. Preparing
gives no permission to clean up the existing project. Use it when tests may write,
remove or change project files. [Getting started](../getting-started.md) shows
the basic use, and [Containers](../guide/containers.md) the use in a
container.

`prepare_project` installs no interpreters, build tools or dependencies. Each
language operation keeps its own setup and capability checks, so preparing a
copy does not make an unsupported operation available.

## Inputs and outputs

Besides the project, `prepare_project(companions=(...))` copies directories
outside it that its runs use, such as a suite beside the project. `bytecode=`
takes a `langadapter.python.project.BytecodeStore`, which writes stored compiled
modules into each use's copy and stores what the runs compiled.

Do not change the original project while it is being copied. A copy holds
ordinary files and directories, never symbolic links, junctions or special files.
Entries left out with `exclude`, and Python bytecode caches, are skipped
before this check. A link or special file anywhere else makes the project
refused rather than copied in part. The project directory itself must not be a
link either, while the directories above it may be links.
`PreparedProject.stage_suite` applies the same rule to a suite it copies
into the copy: no link inside the suite's directory or as that directory. The
parent directory of the copy, the parent directory of the receipts (the records
of each copy's creation) and the system's temporary directory, which holds the
snapshot, must each lie outside the original project. For a project on this
machine `prepare_project` refuses one that lies inside. In a container it
checks that the work directory, which holds the copy, lies outside the project.
For a project inside the work directory it also refuses a receipts' directory or
temporary directory inside the project, and for every project one inside a
companion directory. For a project outside the work directory the caller keeps
those two directories outside the project.
The two parent directories may be the same directory.
`prepare_project(parent=..., receipt_parent=...)` chooses them. The copy
goes to `parent`, else to the context's work directory when it has one, else to
the system's temporary directory. In a container it always goes to the work
directory, and another `parent` is refused. The receipts, and the snapshot a
copy is restored from, go to the system's temporary directory unless
`receipt_parent` names another for the receipts. The system's temporary
directory is used by its physical path: links in it are resolved (macOS's
`/var` becomes `/private/var`), and on Windows short names are expanded. A
directory named by `parent`, `receipt_parent` or the work directory is used as
named.

`prepare_project(context, inputs=ProjectInputSelection(exclude=(...)))` leaves
named entries out of the copy. A program uses this for files that are not
project inputs, such as a virtual environment directory inside the project or the
program's own configuration. Paths named in `required=` and the test
framework configuration file selected inside the project cannot be left out.
Nothing else is protected, so name the suite's files in `required=` when an
exclusion could cover them.

Each `use()` restores the whole copy from its snapshot, including changed and
deleted input files. Paths to test framework configuration that belongs to
the project point to the copy. Project resources and build wrappers keep their
relative locations. Tools and configuration chosen explicitly outside the
project stay as chosen. Environment variable values that contain the original
project directory's path are refused, never silently rewritten, whether alone, in
a path list or in an option. A letter, digit, `.`, `_` or `-` directly before
or after the path makes it part of another directory's name. For a project at a
drive root such as `P:\`, or at `/` in a container, every path below it counts
as inside. So for a project at `/app` the values `/app-env`, `/app2` and
`/opt/app` are outside, while `/app/src`, `--rootdir=/app -x`, `/app env` and
`/app (2)` count as inside. `PWD` and `OLDPWD` are exempt: they name the
caller's shell location. Choose the toolchain and the environment variables
before preparing: changing them on a copied context gives it no new
permission to run commands.

An environment inside the project is the exception on the host. A value
whose every path into the project lies in a directory holding `pyvenv.cfg` or
`conda-meta`, such as `VIRTUAL_ENV=/app/.venv` or `/app/.venv/bin` on `PATH`,
is admitted when nothing in that environment's site directories leads into the
project outside it. The check reads them without running the environment:
`.pth` path entries and the import hooks they load, `.egg-link` files,
`sitecustomize` and `usercustomize`, package directories linked in, and the
records of editable installs. When one leads into the project, as an editable
install of the project does, the refusal names the file and the path. An
environment that includes the base interpreter's packages
(`--system-site-packages`) is refused, since their directories are not read. An
admitted environment's directory is left out of the copy, and the copy's
commands use it in place. In a container such values stay refused.

One change is allowed. An adapter that runs another adapter on the same
project, as the C adapter runs Python tests through Python's, makes the context
it gives that adapter with `context.derived(environment=..., work_dir=...)`. The
derived context keeps the root, the interpreter, its flags and the test
framework. It adds variables and a work directory the caller made. A variable
the context sets itself is refused, unless `extend` names it with its
separator, as in `extend={"PYTHONPATH": os.pathsep}`: the added value then
comes first and the context's value follows. An added variable overlays the
caller's environment as the context's own variables do. Made from the context
`use()` yields, it belongs to that use and stops with it. The directories
LangAdapter makes in its work directory keep their creation receipts there, so
removing the work directory removes them too. Its observations record the
variables as given, with the work directory's path written `<work_dir>`.

A use runs commands from one thread at a time. An engine that runs its units
of work side by side, as the C adapter runs its mutants, does so with
`langadapter.execution.run_parallel(function, items, workers=...)`. Inside a
use, the use lends itself to those threads for their calls. Every command
they run is checked as the use's own, and the call returns only once every
thread has ended, raising the first error a call raised. Each call must
write only in a directory of its own, never in the copy's tree.

Build requests from the root that `use()` yields. Paths and strings in
requests are not rewritten to point into the copy. Write delivered suites,
logs and other lasting outputs outside the copy and the snapshot. A successful
`close()` removes both directories it created and their ownership records. Results
already returned do not depend on that cleanup.

`remove_created_files=True` needs this prepared context, in Python and in
Java. It refuses an ordinary shared project with `OwnershipRefused`, a
`LangAdapterError`. Between completed runs in one use, only files that the
copy recorded as its own outputs are removed. Files created later are left
alone, and directories are removed only when empty. Cleanup is refused for output
directories that contain links and while a command has not been confirmed to have
ended. Repeated uses also restore changed or deleted inputs from the snapshot.
Ordinary runs without cleanup still work without preparation.

## Python imports in a copy

A Python run in a copy measures the copy's module under test. An installed
copy of the project, or an editable install that points to the original
project, would give the tests another file, and the copy would read as never
run. When another location provides the project's package, the run puts the
copy's directory first on the import path. This works for a package in the
project directory or in any directory directly inside it, and for a module in the
project directory or in its `src`, `lib` or `python` directory. An interpreter inside
the project, such as the project's own `.venv`, runs in place from the original
directory, and its installs are checked the same way.

During the run, the adapter records the file the tests execute the module
under test from. A run that executed it only from another file is refused
with `infrastructure_failure` and a reason that names that file:

- An import hook of an editable install, as meson-python and setuptools in
  strict mode write one, takes precedence over the import path. Reinstall
  the project without editable mode (`pip install .`), and the copy's directory
  then goes first.
- An installed copy, or another directory, that the copy's directory could not be
  put ahead of, such as a package more than one directory deep in the project,
  is not supported.
- A compiled module built beside the module under test (an extension built
  in place) is loaded instead of the source. This is not supported. Remove
  the built module to measure the source.

A project whose package needs compiled modules that the copy does not hold,
because they were built only into an installed copy or a build directory, is not
supported either. A run that could not collect its tests without them is
refused, and the reason names the module. A module that the tests never import
is no refusal: its coverage is 0, and every mutant survives.

A run that names no module under test, such as a run of the whole suite,
keeps its import path. It records each of the copy's modules that the tests
executed from a file outside the copy, and it is refused the same way when
there is one. It checks the modules in the project directory, in its `src`,
`lib` or `python` directory and in a directory of the import path inside the copy.
Modules elsewhere in the project are not checked. Naming the module under
test has the copy's directory put first.
A benchmark checks only through its first warmup repetition, so a benchmark
without one is not checked. Its measured repetitions run without the check.

## Lifetime and recovery

Only one `use()` or `close()` may hold a prepared project at a time. A scoped
context belongs to its current use and thread. Cached adapters, copied
contexts and sessions cannot run anything after that use, even during a later
use of the same copy, and use from a thread that was not registered is
refused. In a worker, create and use a separate prepared project rather than
sharing a scoped context whose use has ended.

Close mutation sessions inside `use()`. Started commands and open sessions
must confirm that they ended before the lease, the lock that reserves the copy
for running commands, is released. An exception, an interruption, an owner
mismatch or uncertain cleanup keeps the copy, the snapshot, the receipts and
the lease. The owner is the object whose `close()` cleans them up. Keep the
original owner and the session's cleanup handles, recover those resources and
only then call `close()` on the same owner again. A process ID that
disappeared does not make cleanup safe, and the lease must not be removed by
hand.

`root` names the disposable copy. It is for diagnosis, not for taking over a
copy: a context built on the copy, or on the snapshot it is restored from, is
refused. A closed owner cannot be reused. Calling its `close()` again does no
harm, and after a failed removal a new call retries only the directories that
remain.

A prepared copy owns and restores its own directories only. Processes a run
starts can still reach other files, and nothing records which process wrote
which file. Keep unrelated processes and valuable files outside the disposable
directories. Extension adapters must run commands through
the context's executor and keep their cleanup handles. Subprocesses started
outside that executor are not managed.

If an operation fails while it is uncertain whether a command has ended, it
raises the original exception with its recovery handle instead of returning a
failure result without one. Failures after a confirmed stop may return their
normal typed outcome. Keep exceptions and owners until recovery completes. A
failure result or a missing process ID alone does not permit discarding a
copy that was kept.

## Worker threads and separate measurements

Use `prepared.worker(scoped)` when an existing worker thread needs the current
use's context. Enter it on that worker with the exact context `use()` yielded.
It lends the context to that worker, one loan at a time. It is not a copied
context or another use. Operations of the parent and competing loans are
refused while it is active. An idle mutation session may stay open across
loans. Close it before leaving the parent use. A loan neither restores the
snapshot nor restarts the session. A mutation session whose cleanup failed
or was interrupted may have left the target mutated or its workers running.
Until its `close()`, retried on the parent's thread, succeeds, the use
refuses every other operation, new loans and separate measurements included,
and the refusal names the session as its `cleanup_handle`. Any other
resource a loan leaves unresolved ends the use's work.

For a separate measurement, call `prepare_project(original_context)` inside the
parent use, then enter and close that child's own `use()`. The original
project must stay unchanged and outside the parent's copy and snapshot, and
the directories and receipts of parent and child must not overlap. Children in a
container need the same explicit container mapping and helper Python. Parent operations are refused
while a child operation is active. An idle parent session and changed parent
inputs stay intact. A child can be prepared and used only inside the `use()`
of another prepared project, and it stays tied to that use.

Close every child before leaving the use that contains it. A child's error or
a failed `prepare_project` call can keep the directories and commands it acquired
even when no usable context was returned. Keep the original exception and
pass it to `langadapter.recovery_owners(error)`. Resolve the sessions, commands
and scopes it returns first. Then call `close()` again on the owner it returns:
the child's `PreparedProject` or, for a preparation that failed before one
existed, the owner of the directories it allocated. An incomplete preparation
cannot run anything. Once a child's `use()` or preparation ends with an
exception, the parent's current use runs nothing more, even if you catch the
error. New adapters, operations, children and worker loans are refused. The
only call it still accepts is `close()` on the child's owner. Leaving the
parent's `use()` fails, and keeps the parent's copy, until that `close()`
succeeds. To run more, start a new `use()` of the parent.
