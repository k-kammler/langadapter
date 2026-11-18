"""The check of a language adapter: which facts and hooks it provides,
neutrally in the order of the hook table (``langadapter.hooks``), or by the
levels of consumers (``langadapter.consumers``).

Without an example the check reads the adapter class. With an example
(``conformance.AdapterExample``) it also runs the suite and measures its
coverage target, so a hook that returns the wrong thing reads ``failing``
with what it returned. Messages use, besides the language's words, fact,
hook, level and fallback.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple, Union

from langadapter import consumers as _consumers, hooks as _hooks
from langadapter._words import listed
from langadapter.adapter import _unknown_setting_kinds
from langadapter.capabilities import Availability
from langadapter.context import ContainerExecution, ProjectContext
from langadapter.outcomes import Outcome
from langadapter.registry import create, registration
from langadapter.runs import SuiteRunRequest

#: A fact or hook the adapter provides, or a family whose hooks are all present.
PRESENT = "present"
#: A hook the adapter leaves to its default, which works as it is.
DEFAULT_USED = "default used"
#: A fact or hook the adapter does not provide.
MISSING = "missing"
#: A hook whose run on the example returned the wrong thing.
FAILING = "failing"
#: A hook only a run on an example shows, run without one.
NOT_CHECKED = "not checked"
DEFAULT_NOTE = "A fact or hook with its default used works as it is. Write it to change the default."
"""The line that says once what a default means."""


@dataclass(frozen=True)
class Item:
    """One fact or hook: its status, and for consumers its tier, level and
    sources, with one plain message and the guide section."""

    family: str
    name: str
    kind: str
    title: str
    status: str
    message: str
    guide: str
    tier: str = ""
    level: str = ""
    sources: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Readiness:
    """How far a language is for one installed consumer.
    ``missing_required`` counts the required families that are missing."""

    consumer: str
    title: str
    version: str
    ready: Optional[bool]
    reached: int
    levels: int
    missing_required: int
    #: The required hooks only a check with an example confirms (``ready`` None).
    not_checked: Tuple[str, ...] = ()

    def line(self, examined: bool = False) -> str:
        """The readiness as one line of the check's report, ``examined`` when
        a run on an example gave it."""
        name = f"{self.title} {self.version}".strip()
        if self.ready is None:
            return (f"{name}: everything required is there. "
                    f"{_confirmed(self.not_checked, examined)}")
        if self.ready and self.reached == self.levels:
            return f"{name}: ready, all {self.levels} levels complete."
        state = "ready" if self.ready else (
            f"{self.missing_required} required "
            f"{'family' if self.missing_required == 1 else 'families'} missing")
        return f"{name}: level {self.reached} of {self.levels} complete, {state}."


@dataclass(frozen=True)
class Report:
    """The check's result. For consumers, ``ready`` is True (every required family
    present), False (one missing or failing) or None (``not_checked`` names required
    hooks only an example confirms). ``reached`` is the highest complete level.
    ``readiness`` covers the named installed consumers, or every installed one.
    ``example`` says in one line what the run on the example gave, empty
    without an example. ``notes`` hold the consumers' notes and what the run on
    the example noted, such as an example on which every mutant survived."""

    language: str
    consumers: Tuple[str, ...]
    items: Tuple[Item, ...]
    ready: Optional[bool] = None
    reached: Optional[int] = None
    levels: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = ()
    readiness: Tuple[Readiness, ...] = ()
    notes: Tuple[str, ...] = ()
    problems: Tuple[str, ...] = ()
    not_checked: Tuple[str, ...] = ()
    example: str = ""
    #: Each family whose runs on the example did not start, since the
    #: capability report reads its tool missing setup, with the report's
    #: reason, for the command's refusal. report() sets it, and the
    #: signature leaves it out.
    _unavailable: Dict[str, str] = field(default_factory=dict, init=False, repr=False,
                                         compare=False)

    def lines(self) -> List[str]:
        """The report as plain lines, as ``langadapter check`` prints it."""
        out = [f"Language: {self.language}"]
        if self.example:
            out.append(self.example)
        facts = [i for i in self.items if i.family == "facts"]
        out.append("Facts:")
        out.extend(f"  {i.message}" for i in facts)
        out.extend(self._consumer_lines() if self.consumers else self._neutral_lines())
        if any(item.status == DEFAULT_USED for item in self.items):
            out.append(DEFAULT_NOTE)
        if self.notes:
            out.append("Notes:")
            out.extend(f"  {n}" for n in self.notes)
        if self.problems:
            out.append("Problems:")
            out.extend(f"  {p}" for p in self.problems)
        out.append(self._summary())
        return out

    def _consumer_lines(self) -> List[str]:
        """The hooks by the levels of the consumers asked for, each unused
        family once, and whether the language is ready for them."""
        out = [f"For {', '.join(self.consumers)}:"]
        for label, title, families in self.levels:
            out.append(f"{label}: {title}" if title else label)
            out.extend(f"  {item.message} Guide: {item.guide}" for item in self.items
                       if item.family in families)
        rest = [i for i in self.items if i.family != "facts"
                and not any(i.family in f for _, _, f in self.levels)]
        unused = {item.family: item for item in reversed(rest)}
        if rest:
            out.append("Not used:")
            out.extend(f"  {item.message} Guide: {item.guide}"
                       for item in rest if unused[item.family] is item)
        return out + self._ready_lines()

    def _ready_lines(self) -> List[str]:
        """Whether the language is ready for the consumers asked for, as one
        line, or none when that is not known."""
        if self.ready is not None:
            return ["Ready: yes." if self.ready else "Ready: no."]
        if self.not_checked:
            return [f"Ready: everything required is there. "
                    f"{_confirmed(self.not_checked, bool(self.example))}"]
        return []

    def _neutral_lines(self) -> List[str]:
        """Every hook in the hook table's order, and a line per installed
        consumer."""
        out = ["Hooks, in the order of the reference page Hooks and facts:"]
        out.extend(f"  [{item.family}] {item.message} Guide: {item.guide}"
                   for item in self.items if item.family != "facts")
        if self.readiness:
            out.append("Installed consumers:")
            out.extend(f"  {r.line(bool(self.example))}" for r in self.readiness)
        return out

    def _summary(self) -> str:
        """The facts and hooks counted by status, and for the consumers asked
        for, how many of the missing ones they require."""
        counts = Counter(item.status for item in self.items)
        required = sum(1 for item in self.items
                       if item.status == MISSING and item.tier == _consumers.REQUIRED)
        notes = ({MISSING: f" ({required or 'none'} required by {', '.join(self.consumers)})"}
                 if self.consumers else {})
        return "Summary: " + ", ".join(
            f"{counts[status]} {status}{notes.get(status, '')}"
            for status in (PRESENT, DEFAULT_USED, MISSING, FAILING, NOT_CHECKED)
            if counts[status]) + "."


def _confirmed(not_checked: Sequence[str], examined: bool = False) -> str:
    """What only a run on an example confirms, and how to run it, or, when
    the run on an example could not show it (``examined``), where it says
    why."""
    names = listed(not_checked)
    if examined:
        why = "its line says" if len(not_checked) == 1 else "their lines say"
        return f"The run on this example leaves {names} not checked, as {why}."
    return (f"Only a run on an example confirms {names}: run this check again with "
            "--example DIR.")


def _family_status(items: Sequence[Item]) -> str:
    if any(i.status == FAILING for i in items):
        return FAILING
    if any(i.status == MISSING for i in items):
        return MISSING
    if any(i.status == NOT_CHECKED for i in items):
        return NOT_CHECKED
    return PRESENT


def _adapter_class(language: Union[str, type]) -> Tuple[str, type]:
    if isinstance(language, type):
        return getattr(language, "language", "") or language.__name__, language
    found = registration(language)
    factory = found.factory
    return found.name, factory if isinstance(factory, type) else type(factory)


def _probe(language: str, example: Any, cls: Optional[type] = None
           ) -> Tuple[Dict[str, Tuple[str, str]], str, Dict[str, str]]:
    """Live ``(status, message)`` by hook name from the example's suite, breaker,
    coverage and mutation runs, the line on what the suite's run gave
    (``_example_line``), and the families the adapter's capability report
    reads unavailable (``_unavailable_families``), whose runs it skips. With
    ``cls``, the adapter needs no registration."""
    def make(context: Any) -> Any:
        if cls is not None:
            context.validate()
            return cls(context)
        return create(language, context)

    results: Dict[str, Tuple[str, str]] = dict.fromkeys(
        _RUN_ONLY, (NOT_CHECKED, "the example's suite did not run"))
    unavailable: Dict[str, str] = {}
    try:
        adapter = make(example.context)
        unavailable = _unavailable_families(adapter)
        if "run tests" in unavailable:
            results["run_suite"] = (NOT_CHECKED, _unavailable_text("run tests", unavailable))
            return results, _example_line(example, None, False), unavailable
        run = adapter.tests.run_suite(SuiteRunRequest(suite=example.suite,
                                                      timeout_s=example.timeout_s))
    except Exception as exc:  # a learner's unfinished adapter is the normal case
        results["run_suite"] = (FAILING, f"hook run_suite could not run: "
                                f"{type(exc).__name__}: {exc}.")
        return results, _example_line(example, None, False), unavailable
    if run.outcome is Outcome.UNSUPPORTED:
        results["run_suite"] = (MISSING, "")
    elif run.outcome in (Outcome.COMPLETED, Outcome.TEST_FAILURES) and run.tests:
        results["run_suite"] = _breaker_probe(make, adapter, example) or (PRESENT, "")
    else:
        results["run_suite"] = (FAILING, (
            f"hook run_suite returned {run.outcome.value} with {len(run.tests)} test "
            "outcomes. For the example's suite it should give each test's outcome."))
    results["unsound_test_patterns"] = _patterns_probe(adapter)
    if not getattr(example, "coverage_target", None):
        results["coverage"] = (NOT_CHECKED, _no_coverage_target(adapter, example))
    elif "coverage" in unavailable:
        results["coverage"] = (NOT_CHECKED, _unavailable_text("coverage", unavailable))
    else:
        results.update(_coverage_probe(adapter, example))
    if getattr(example, "coverage_target", None):
        mutation = _mutation_status(adapter, example, run, unavailable)
        if mutation is not None:
            results["open_mutation_session"] = mutation
    return (results, _example_line(example, run, results["coverage"][0] == PRESENT),
            unavailable)


#: The families a run on an example tries, whose capability entries can
#: tell before it that their tool is not set up.
_TRIED = ("run tests", "coverage", "mutation")


def _unavailable_families(adapter: Any) -> Dict[str, str]:
    """The families of ``_TRIED`` whose entry in the adapter's capability
    report (``Family.capabilities``) reads missing setup, with the report's
    reason: their tool is not set up. An unsupported entry is a hook the
    adapter lacks, which its line reads missing. An entry the report lacks,
    or a report that did not complete or could not be made, as an unfinished
    adapter's, makes no family unavailable."""
    try:
        report = adapter.probe_capabilities()
    except Exception:  # a learner's unfinished capabilities hook is the normal case
        return {}
    if getattr(report, "outcome", None) is not Outcome.COMPLETED:
        return {}
    found: Dict[str, str] = {}
    for family in _TRIED:
        for name in _family_capabilities(family):
            try:
                capability = report.capability(name)
            except KeyError:
                continue
            if capability.availability is Availability.MISSING_SETUP:
                found[family] = capability.reason or f"{name} needs setup"
                break
    return found


def _unavailable_text(family: str, unavailable: Dict[str, str]) -> str:
    """Why ``family``'s run was skipped: the capability report's reason."""
    return f"the capability report reads {family} unavailable: {unavailable[family]}"


def _mutation_status(adapter: Any, example: Any, run: Any,
                     unavailable: Dict[str, str]) -> Optional[Tuple[str, str]]:
    """The mutation hook's live status on the example's coverage target: not
    checked when the capability report reads mutation unavailable, or when
    the example's suite has failing tests, which a mutation run refuses,
    else the probe's. None for an adapter without a native engine."""
    engines = tuple(getattr(adapter, "native_mutation_engines", ()) or ())
    if not engines or not callable(getattr(adapter, "open_mutation_session", None)):
        return None
    if "mutation" in unavailable:
        return NOT_CHECKED, _unavailable_text("mutation", unavailable)
    failing = [str(test) for test in run.failing_tests()]
    if failing:
        return NOT_CHECKED, (f"the example's suite has failing tests ({listed(failing)}), and "
                             "a mutation run refuses a suite that does not pass. Give an "
                             "example whose tests pass")
    return _mutation_probe(adapter, engines[0], example)


def _example_line(example: Any, run: Any, measured: bool) -> str:
    """What the run on the example gave, as one line: its test files, the
    runtime that ran them, its tests by outcome, how a run that gave no test
    outcomes ended, and whether coverage of its target was ``measured``.
    ``run`` is None when run_suite could not run it."""
    files = len(example.suite.test_files)
    line = f"Example: {files} test file{'' if files == 1 else 's'}"
    runtime = getattr(example.context.interpreter, "path", None)
    if runtime:
        line += f", run with {runtime}"
    if run is None:
        return f"{line}, which run_suite could not run."
    counts = Counter(test.outcome for test in run.tests)
    line += f", {len(run.tests)} test{'' if len(run.tests) == 1 else 's'}"
    if counts:
        line += ": " + ", ".join(f"{counts[outcome]} {outcome}" for outcome in
                                 sorted(counts, key=lambda outcome: (outcome != "passed", outcome)))
    if run.outcome not in (Outcome.COMPLETED, Outcome.TEST_FAILURES):
        line += f", the run ended {run.outcome.value}"
    coverage = f" Coverage of {example.coverage_target} measured." if measured else ""
    return f"{line}.{coverage}"


#: The points only a run on an example shows: neither the class nor the
#: operation report can show coverage, or the unsound-test patterns of an
#: adapter's facts.
_RUN_ONLY = ("coverage", "unsound_test_patterns")


def _patterns_probe(adapter: Any) -> Tuple[str, str]:
    """The status of the unsound-test patterns, read from the adapter's
    ``language_facts``: not checked, with the reason, when it cannot give them."""
    if getattr(adapter, "language_facts", None) is None:
        return NOT_CHECKED, "they are read from language_facts, which the adapter does not write"
    try:
        patterns = adapter.language_facts().unsound_test_patterns
    except Exception as exc:  # an unfinished adapter is the normal case
        return NOT_CHECKED, (f"they are read from language_facts, which raised "
                             f"{type(exc).__name__}: {exc}")
    return (PRESENT if patterns else MISSING), ""


def _example_sources(root: Path, adapter: Any, tests: Sequence[str]) -> List[str]:
    """The source files of the example at ``root``, sorted, as paths relative
    to it: the files with one of the language's file extensions that are no
    test file (``tests``, relative to ``root``), setup file or non-source
    file and lie in no ignored or dot directory. Coverage is measured on an
    example with exactly one."""
    setup = {(root / path).resolve()
             for path in adapter.setup_files(str(root), [str(root / test) for test in tests])}
    ignored = set(adapter.ignored_dirs)
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*")
                  if path.is_file() and path.suffix.lower() in adapter.file_extensions
                  and path.name not in adapter.non_source_files
                  and path.relative_to(root).as_posix() not in tests
                  and path.resolve() not in setup
                  and not any(part in ignored or part.startswith(".")
                              for part in path.relative_to(root).parts[:-1]))


def _no_coverage_target(adapter: Any, example: Any) -> str:
    """Why the example's coverage is not checked: it names no coverage
    target, as when it holds no source file or several."""
    try:
        sources = _example_sources(Path(example.suite.root), adapter, example.suite.test_files)
    except Exception:  # an unfinished adapter is the normal case
        return "the example names no coverage target"
    if len(sources) > 1:
        shown = sources[:3] + ([f"{len(sources) - 3} more"] if len(sources) > 3 else [])
        return (f"the example holds {len(sources)} source files, {listed(shown)}. Coverage is "
                "measured on an example with one")
    if not sources:
        return ("the example holds no source file beside its tests and setup files. Coverage "
                "is measured on an example with one")
    return "the example names no coverage target"


def _coverage_probe(adapter: Any, example: Any) -> Dict[str, Tuple[str, str]]:
    """Live ``(status, message)`` of coverage from a run with the example's
    ``coverage_target``."""
    try:
        measured = adapter.tests.run_suite(SuiteRunRequest(
            suite=example.suite, timeout_s=example.timeout_s,
            coverage_target=example.coverage_target))
    except Exception as exc:
        return {"coverage": (FAILING, f"hook coverage could not run: "
                             f"{type(exc).__name__}: {exc}.")}
    results: Dict[str, Tuple[str, str]] = {}
    if measured.outcome is Outcome.UNSUPPORTED:
        results["coverage"] = (MISSING, "")
    elif measured.outcome is Outcome.MISSING_SETUP:
        # The hook is there and its tool is not set up.
        results["coverage"] = (FAILING, (
            f"a run with the coverage_target {example.coverage_target} needs setup: "
            f"{measured.reason}"))
    elif measured.coverage is None:
        results["coverage"] = (FAILING, (
            f"a run with the coverage_target {example.coverage_target} returned "
            f"{measured.outcome.value} without coverage. It should return that file's "
            "covered and missed lines and branches."))
    else:
        results["coverage"] = (PRESENT, "")
    return results


def _breaker_probe(make: Any, adapter: Any, example: Any) -> Optional[Tuple[str, str]]:
    """The status of the example's ``collection_breaker`` run alone beside the
    suite's first test file in a project copy, named by the file rule or else
    the scratch stem: failing unless it reads ``collection_failed``. None then,
    or when nothing runs (no breaker, a container, a suite outside the project)."""
    import shutil
    import tempfile

    from langadapter.suites import Suite

    breaker = getattr(example, "collection_breaker", None)
    if not breaker or isinstance(example.context.execution, ContainerExecution):
        return None
    top = Path(example.context.root).resolve()
    try:
        suite_dir = Path(example.suite.root).resolve().relative_to(top)
    except ValueError:
        return None
    file_extension = adapter.file_extensions[0] if adapter.file_extensions else ""
    stem = adapter.file_rule(breaker)
    if stem:
        name = stem + file_extension
    else:
        template = getattr(adapter, "test_file_name", "") or "test_{stem}{ext}"
        stem = adapter.scratch_stem
        name = template.format(stem=stem, Stem=stem[:1].upper() + stem[1:], ext=file_extension)
    relative = (PurePosixPath(example.suite.test_files[0]).parent / name).as_posix()
    with tempfile.TemporaryDirectory(prefix="langadapter-check-",
                                     ignore_cleanup_errors=True) as scratch:
        copy = Path(scratch) / top.name
        shutil.copytree(top, copy, symlinks=True)
        written = copy / suite_dir / relative
        if written.exists():
            return FAILING, (f"the example's collection_breaker would replace {relative} in a copy "
                             "of the project: give it a file of its own.")
        written.write_text(breaker, encoding="utf-8")
        try:
            run = make(replace(example.context, root=str(copy))).tests.run_suite(SuiteRunRequest(
                suite=Suite(str(copy / suite_dir), (relative,)), timeout_s=example.timeout_s))
        except Exception as exc:
            return FAILING, (f"hook run_suite could not run the example's collection_breaker: "
                             f"{type(exc).__name__}: {exc}.")
    if run.outcome is Outcome.COLLECTION_FAILED:
        return None
    return FAILING, (f"hook run_suite returned {run.outcome.value} for the example's "
                     "collection_breaker, a test file that does not load. It should return "
                     "collection_failed.")


def _mutation_probe(adapter: Any, engine: str, example: Any) -> Tuple[str, str]:
    """A mutation run on the coverage target: present if each mutant has status, diff, line,
    with a note when every mutant survived."""
    from langadapter.mutation import SURVIVED_STATUS, MutationRequest

    try:
        with adapter.mutations.open_session(engine) as session:
            run = session.run(MutationRequest(target=example.coverage_target,
                                              suite=example.suite))
    except Exception as exc:
        return FAILING, (f"hook open_mutation_session could not run: "
                         f"{type(exc).__name__}: {exc}.")
    if run.measured and run.mutants and all(m.status for m in run.mutants):
        bare = [m for m in run.mutants if not m.diff or m.key.line is None]
        if not bare:
            if all(m.status == SURVIVED_STATUS for m in run.mutants):
                count = len(run.mutants)
                return PRESENT, (f"every mutant of {example.coverage_target} survived on the "
                                 f"example ({count} of {count}): no test failed on any mutant, "
                                 "as when the session runs the tests on the original source "
                                 "instead of each mutant.")
            return PRESENT, ""
        return FAILING, (f"hook open_mutation_session returned {len(bare)} of {len(run.mutants)} "
                         f"mutants for {example.coverage_target} without their change text or "
                         "line. Each mutant should carry its change (diff) and its line "
                         "(key.line).")
    return FAILING, (f"hook open_mutation_session returned {run.outcome.value} with "
                     f"{len(run.mutants)} mutants for {example.coverage_target}. It should "
                     "return each mutant with its status.")


def _supported(language: str, cls: type, example: Any, from_class: bool
               ) -> Tuple[Optional[Set[str]], str, type]:
    """The operations the adapter supports on the example's context, else the
    current directory's, or None and the reason when the adapter cannot be made,
    and the class of the adapter made (``cls`` when none was): a language
    registered with a factory function is checked as the class it makes."""
    context = example.context if example is not None else ProjectContext(str(Path.cwd()))
    try:
        adapter = cls(context) if from_class else create(language, context)
        return {o.name for o in adapter.operation_capabilities() if o.supported}, "", type(adapter)
    except Exception as exc:  # an unfinished adapter is the normal case
        return None, f"{type(exc).__name__}: {exc}", cls


def _status(cls: type, entry: _hooks.Hook, live: Dict[str, Tuple[str, str]],
            supported: Optional[Set[str]]) -> Tuple[str, str]:
    """A fact's or hook's status. A hook with operations is present when every
    operation of one alternative is supported, else missing despite a default."""
    if entry.name in live:
        status, message = live[entry.name]
        if status != MISSING:
            return status, message
        return MISSING, ""
    if entry.name in _RUN_ONLY:
        return NOT_CHECKED, ""
    if entry.judged_by and supported is not None:
        unsupported = hook_issue(entry.judged_by,
                                  lambda name: None if name in supported else "unsupported")
        return (MISSING, "") if unsupported else (PRESENT, "")
    if _hooks._present(cls, entry):
        return PRESENT, ""
    return (DEFAULT_USED, "") if entry.works_by_default else (MISSING, "")


def hook_issue(alternatives: Sequence[Sequence[str]],
                issue_of: Callable[[str], Optional[str]]) -> Optional[str]:
    """Why a hook judged by ``alternatives`` of operations is missing, by
    ``issue_of`` (an operation's problem or None): the first alternative's first
    problem, or None when one alternative has none. The rule ``report`` and
    ``family_issue`` apply, which a caller may apply to its own records."""
    problems = [next((issue_of(name) for name in alternative if issue_of(name)), None)
                for alternative in alternatives]
    return problems[0] if problems and all(problems) else None


@lru_cache(maxsize=None)
def family_hooks(family: str) -> Tuple[Tuple[Tuple[str, ...], ...], ...]:
    """The alternatives (tuples of operations) of each hook of ``family`` that
    operations judge (``Hook.judged_by``) and that does not work by default."""
    return tuple(tuple(tuple(alternative) for alternative in hook.judged_by)
                 for hook in _hooks.table()
                 if hook.family == family and hook.kind == "hook"
                 and not hook.works_by_default and hook.judged_by)


def _family_capabilities(family: str) -> Tuple[str, ...]:
    try:
        return _hooks.family(family).capabilities
    except KeyError:
        return ()


def assessed(family: str) -> bool:
    """Whether a capability report can tell if ``family`` is there: an operation
    judges one of its hooks, or the report has an entry (``Family.capabilities``)."""
    return bool(family_hooks(family) or _family_capabilities(family))


def _operation_issue(report: Any, name: str) -> Optional[str]:
    for operation in report.operations or ():
        if operation.name != name:
            continue
        if not operation.supported:
            return operation.reason or f"the hook for {name} is missing"
        if operation.readiness in (Availability.UNSUPPORTED, Availability.MISSING_SETUP):
            return operation.reason or f"{name} needs setup"
        return None
    return None if not report.operations else f"the hook for {name} is missing"


def _capability_issue(report: Any, name: str) -> Optional[str]:
    try:
        capability = report.capability(name)
    except KeyError:
        return f"the {report.language} adapter does not report {name}"
    if capability.available:
        return None
    return capability.reason or f"{name} is {capability.availability.value}"


def family_issue(report: Any, family: str) -> Optional[str]:
    """Why ``family`` is not available in a project's ``CapabilityReport``
    (``LangAdapter.probe_capabilities()``), or None when it is or the report
    cannot tell (``assessed``). A missing or unavailable entry of the family
    (``Family.capabilities``) names it first. Then each operation-judged hook
    needs one alternative whose operations are all supported and, if probed,
    ready. A report without operations judges no hook."""
    for name in _family_capabilities(family):
        issue = _capability_issue(report, name)
        if issue:
            return issue
    for alternatives in family_hooks(family):
        issue = hook_issue(alternatives, lambda name: _operation_issue(report, name))
        if issue:
            return issue
    return None


def _not_checked_text(detail: str) -> str:
    """Why a fact or hook is not checked: ``detail``, the reason a run on the
    example gave, else that only such a run shows it."""
    if detail:
        return f"{detail[:1].upper()}{detail[1:]}."
    return "Only a run on an example shows it: run this check again with --example DIR."


def _failing(named: str, entry: _hooks.Hook, detail: str) -> str:
    """A failing hook's line: what it did, without naming the hook twice."""
    rest = detail.removeprefix(f"{entry.kind} {entry.name} ")
    if rest != detail:
        return f"{named}: it {rest}"
    return f"{named}. {detail[:1].upper()}{detail[1:]}"


def _neutral_message(entry: _hooks.Hook, status: str, detail: str) -> str:
    """One line per fact or hook: its name, its title and its status word,
    then what the status means for it."""
    named = f"{entry.kind} {entry.name} ({entry.title}): {status}"
    if status == FAILING:
        return _failing(named, entry, detail)
    if status == PRESENT:
        return f"{named}."
    if status == DEFAULT_USED:
        return f"{named}: {entry.default}."
    if status == NOT_CHECKED:
        return f"{named}. {_not_checked_text(detail)}"
    if entry.kind == "fact":
        return f"{named}. {entry.what}"
    return f"{named}. It makes possible {entry.enables}."


def _consumer_message(entry: _hooks.Hook, status: str, detail: str,
                      need: _consumers.ComposedNeed, family: _hooks.Family,
                      asked: Sequence[str], described: Set[str]) -> str:
    """One line per hook for the consumers asked: its status word, then who
    needs it. The family's description, what it improves and what happens
    without it, stands on its first missing hook only (``described`` holds
    the families already described)."""
    sources = ", and ".join(s.text() for s in need.sources)
    named = f"{entry.kind} {entry.name} ({entry.title}): {status}"
    if status == FAILING:
        return _failing(named, entry, detail)
    if need.tier == _consumers.NOT_USED:
        by = ", ".join(sorted({s.title for s in need.sources})) or ", ".join(asked)
        names = ", ".join(h.name for h in _hooks.hooks_of(family.name))
        return (f"{family.title} ({names}): not needed by {by}. "
                f"Useful for {family.useful_for}.")
    if status == PRESENT:
        return f"{named}, {sources}."
    if status == DEFAULT_USED:
        return f"{named}: {entry.default}, {sources}."
    if status == NOT_CHECKED:
        return f"{named}, {sources}. {_not_checked_text(detail)}"
    if need.tier == _consumers.REQUIRED:
        return f"{named}, {sources}. {entry.what}"
    titles = ", ".join(sorted({s.title for s in need.sources if s.tier == _consumers.OPTIONAL}))
    if family.name in described:
        return f"{named}, optional for {titles}."
    described.add(family.name)
    where = (f"where it {' and '.join(need.improves)}" if need.improves
             else f"where it makes possible {entry.enables}")
    without = f" Without it, {' and '.join(need.fallbacks)}." if need.fallbacks else ""
    return f"{named}, optional for {titles}, {where}.{without}"


def report(language: Union[str, type], example: Any = None,
           consumer: Union[None, str, Sequence[str]] = None, *,
           profiles: Sequence[Any] = (),
           environment: Optional[_consumers.Installed] = None) -> Report:
    """The report on the adapter of ``language`` (a registered name or a class),
    live with an ``example``. With ``consumer`` (one or several names) or
    ``profiles`` (files) it follows their levels with dependencies, else it is
    neutral with one readiness line per installed consumer."""
    name, cls = _adapter_class(language)
    asked = (consumer,) if isinstance(consumer, str) else tuple(consumer or ())
    live, ran, unavailable = (_probe(name, example, cls if isinstance(language, type) else None)
                              if example is not None else ({}, "", {}))
    # A present hook's detail is a note on what the run on the example gave.
    noted = tuple(detail for status, detail in live.values() if status == PRESENT and detail)
    families = {f.name: f for f in _hooks.families()}
    supported, unmade, cls = _supported(name, cls, example, isinstance(language, type))
    statuses = {entry.name: _status(cls, entry, live, supported)
                for entry in _hooks.table()}
    own = (((f"the adapter could not be created: {unmade}",) if unmade else ())
           + _unknown_setting_kinds(getattr(cls, "project_settings", ())))
    facts = [Item("facts", e.name, e.kind, e.title, statuses[e.name][0],
                  _neutral_message(e, *statuses[e.name]), e.guide)
             for e in _hooks.table() if e.family == "facts"]
    discovered = _consumers.discover(environment)
    composition = (_consumers.composed(asked, files=profiles, environment=environment,
                                       discovered=discovered) if asked or profiles else None)
    if composition is None or not composition.profiles:
        # Without a consumer's profile there are no requirements to list.
        items = facts + [
            Item(e.family, e.name, e.kind, e.title, statuses[e.name][0],
                 _neutral_message(e, *statuses[e.name]), e.guide)
            for e in _hooks.table() if e.family != "facts"]
        found = discovered[0]
        problems = discovered[1] if composition is None else composition.problems
        readiness = tuple(_readiness(found[key], statuses, environment, discovered,
                                     unavailable) for key in sorted(found))
        neutral = Report(name, (), tuple(items), readiness=readiness, notes=noted,
                         problems=own + tuple(p.message for p in problems), example=ran)
        object.__setattr__(neutral, "_unavailable", unavailable)
        return neutral
    given = {str(p) for p in profiles}
    named = tuple(dict.fromkeys(
        found.title for found in composition.profiles
        if found.consumer in asked or found.source in given)) or asked
    items = list(facts)
    described: Set[str] = set()
    for entry in _hooks.table():
        if entry.family == "facts":
            continue
        need = composition.need(entry.family)
        status, detail = statuses[entry.name]
        items.append(Item(entry.family, entry.name, entry.kind, entry.title, status,
                          _consumer_message(entry, status, detail, need, families[entry.family],
                                            named, described),
                          entry.guide, need.tier, need.level,
                          tuple(s.text() for s in need.sources)))
    ready, reached = _levels_state(items, composition, unavailable)
    not_checked = _not_checked(items, composition) if ready is None and composition.profiles else ()
    titles = _level_titles(composition)
    levels = tuple((label, titles.get(label, ""), names) for label, names in composition.levels)
    found = discovered[0]
    readiness = tuple(_readiness(found[key], statuses, environment, discovered, unavailable)
                      for key in dict.fromkeys(asked) if key in found)
    result = Report(name, named, tuple(items), ready, reached, levels, readiness,
                    composition.notes + noted,
                    own + tuple(p.message for p in composition.problems), not_checked, ran)
    object.__setattr__(result, "_unavailable", unavailable)
    return result


def _level_titles(composition: _consumers.Composed) -> Dict[str, str]:
    titles: Dict[str, str] = {}
    for found in composition.profiles:
        for level in found.levels:
            titles.setdefault(f"{found.title} level {level.level}", level.title)
    return titles


def _levels_state(items: Sequence[Item], composition: _consumers.Composed,
                  unavailable: Dict[str, str]) -> Tuple[Optional[bool], Optional[int]]:
    """Whether the required families are there (None when only an example
    can tell), as a consumer's preflight decides, a family the capability
    report reads ``unavailable`` counting as missing, and how many levels
    are complete."""
    if not composition.profiles:
        return None, None
    by_family: Dict[str, List[Item]] = {}
    for item in items:
        by_family.setdefault(item.family, []).append(item)
    required = [MISSING if name in unavailable else _family_status(by_family.get(name, []))
                for name, need in composition.needs.items() if need.tier == _consumers.REQUIRED]
    # Unknown (None) when the only gaps are hooks a check with an example confirms.
    ready = (False if any(s in (MISSING, FAILING) for s in required)
             else None if any(s == NOT_CHECKED for s in required) else True)
    reached = 0
    for _, names in composition.levels:
        if all(_family_status(by_family.get(name, [])) == PRESENT for name in names):
            reached += 1
        else:
            break
    return ready, reached


def _readiness(found: _consumers.Profile, statuses: Dict[str, Tuple[str, str]],
               environment: Optional[_consumers.Installed], discovered: Any,
               unavailable: Dict[str, str]) -> Readiness:
    composition = _consumers.composed((found.consumer,), environment=environment,
                                      discovered=discovered)
    items = [Item(e.family, e.name, e.kind, e.title, statuses[e.name][0], "", e.guide)
             for e in _hooks.table() if e.family != "facts"]
    ready, reached = _levels_state(items, composition, unavailable)
    by_family: Dict[str, List[Item]] = {}
    for item in items:
        by_family.setdefault(item.family, []).append(item)
    missing = sum(1 for name, need in composition.needs.items()
                  if need.tier == _consumers.REQUIRED
                  and (name in unavailable
                       or _family_status(by_family.get(name, [])) in (MISSING, FAILING)))
    return Readiness(found.consumer, found.title, found.version, ready, reached or 0,
                     len(composition.levels), missing,
                     _not_checked(items, composition) if ready is None else ())


def _not_checked(items: Sequence[Item], composition: _consumers.Composed) -> Tuple[str, ...]:
    """The hooks of required families that only a check with an example confirms."""
    required = {name for name, need in composition.needs.items()
                if need.tier == _consumers.REQUIRED}
    return tuple(dict.fromkeys(i.name for i in items
                               if i.family in required and i.status == NOT_CHECKED))


__all__ = ["PRESENT", "DEFAULT_USED", "MISSING", "FAILING", "NOT_CHECKED", "DEFAULT_NOTE",
           "Item", "Readiness",
           "Report", "report", "assessed", "family_hooks", "family_issue", "hook_issue"]
