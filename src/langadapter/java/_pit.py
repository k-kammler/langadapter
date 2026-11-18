"""Mutation through PIT's command line: the jars, the mutator catalogue,
one run over a target class, and its XML report read as mutants.

PIT mutates the compiled target class and its nested classes in minion
JVMs on the project's JDK and runs each mutant's covering tests through the
JUnit Platform (``pitest-junit5-plugin``, JUnit 4 on Vintage), naming them
by unique ids (``_junit.test_name``). It bounds each test, not the run, by
its normal time times a factor plus a constant (``--timeoutFactor``,
``--timeoutConst``, 1.25 and 4 s by default), and writes its report at the
end, so a run the wall clock ends has no mutant results.

PIT runs covering tests fastest first and stops at the first kill, so the
first killer could change with timing. A ``TestPrioritiserFactory`` plugin
(compiled once into the tool cache, or a container run's scratch) keeps PIT's
choice of tests but runs them in name order.
"""
from __future__ import annotations

from langadapter._execution_scope import reraise_unresolved
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from langadapter.execution import ContainerExecutor, Executor
from langadapter.java import _tools
from langadapter.java._env import JavaEnv, require_mounted_output
from langadapter.mutation import (
    BY_FAILING_TEST,
    BY_TIMEOUT,
    KILLED_STATUS,
    NON_VIABLE,
    NON_VIABLE_STATUS,
    SURVIVED,
    SURVIVED_STATUS,
    TIMEOUT_STATUS,
    UNTESTED,
    UNTESTED_STATUS,
    WITHOUT_NAMED_TEST,
    Classification,
    MutationClass,
)


VERSION = "1.30.0"
JUNIT5_PLUGIN_VERSION = "1.2.3"
#: PIT's mutator names at ``VERSION``, as its registry lists them: each name
#: that selects one mutator, with that mutator's id, and the groups, which
#: select several. tests/java/test_pit_registry.py lists the registry and
#: compares, so a new VERSION shows a difference.
_MUTATORS_PACKAGE = "org.pitest.mutationtest.engine.gregor.mutators."
MUTATORS = {
    "CONDITIONALS_BOUNDARY": _MUTATORS_PACKAGE + "ConditionalsBoundaryMutator",
    "CONSTRUCTOR_CALLS": _MUTATORS_PACKAGE + "ConstructorCallMutator",
    "EMPTY_RETURNS": _MUTATORS_PACKAGE + "returns.EmptyObjectReturnValsMutator",
    "EXPERIMENTAL_ARGUMENT_PROPAGATION": _MUTATORS_PACKAGE + "experimental.ArgumentPropagationMutator",
    "EXPERIMENTAL_BIG_DECIMAL": _MUTATORS_PACKAGE + "experimental.BigDecimalMutator",
    "EXPERIMENTAL_BIG_INTEGER": _MUTATORS_PACKAGE + "experimental.BigIntegerMutator",
    "EXPERIMENTAL_MEMBER_VARIABLE": _MUTATORS_PACKAGE + "experimental.MemberVariableMutator",
    "EXPERIMENTAL_NAKED_RECEIVER": _MUTATORS_PACKAGE + "experimental.NakedReceiverMutator",
    "EXPERIMENTAL_SWITCH": _MUTATORS_PACKAGE + "experimental.SwitchMutator",
    "FALSE_RETURNS": _MUTATORS_PACKAGE + "returns.BooleanFalseReturnValsMutator",
    "INCREMENTS": _MUTATORS_PACKAGE + "IncrementsMutator",
    "INLINE_CONSTS": _MUTATORS_PACKAGE + "InlineConstantMutator",
    "INVERT_NEGS": _MUTATORS_PACKAGE + "InvertNegsMutator",
    "MATH": _MUTATORS_PACKAGE + "MathMutator",
    "NEGATE_CONDITIONALS": _MUTATORS_PACKAGE + "NegateConditionalsMutator",
    "NON_VOID_METHOD_CALLS": _MUTATORS_PACKAGE + "NonVoidMethodCallMutator",
    "NULL_RETURNS": _MUTATORS_PACKAGE + "returns.NullReturnValsMutator",
    "PRIMITIVE_RETURNS": _MUTATORS_PACKAGE + "returns.PrimitiveReturnsMutator",
    "REMOVE_CONDITIONALS_EQUAL_ELSE": _MUTATORS_PACKAGE + "RemoveConditionalMutator_EQUAL_ELSE",
    "REMOVE_CONDITIONALS_EQUAL_IF": _MUTATORS_PACKAGE + "RemoveConditionalMutator_EQUAL_IF",
    "REMOVE_CONDITIONALS_ORDER_ELSE": _MUTATORS_PACKAGE + "RemoveConditionalMutator_ORDER_ELSE",
    "REMOVE_CONDITIONALS_ORDER_IF": _MUTATORS_PACKAGE + "RemoveConditionalMutator_ORDER_IF",
    "REMOVE_INCREMENTS": _MUTATORS_PACKAGE + "experimental.RemoveIncrementsMutator",
    "TRUE_RETURNS": _MUTATORS_PACKAGE + "returns.BooleanTrueReturnValsMutator",
    "VOID_METHOD_CALLS": _MUTATORS_PACKAGE + "VoidMethodCallMutator",
}
GROUPS = ("DEFAULTS", "REMOVE_CONDITIONALS", "REMOVE_SWITCH", "RETURNS", "STRONGER")
JARS = (
    f"org.pitest:pitest-command-line:{VERSION}",
    f"org.pitest:pitest-entry:{VERSION}",
    f"org.pitest:pitest:{VERSION}",
    f"org.pitest:pitest-html-report:{VERSION}",
    f"org.pitest:pitest-junit5-plugin:{JUNIT5_PLUGIN_VERSION}",
    "net.sf.jopt-simple:jopt-simple:4.9",
    "org.ow2.asm:asm:9.10.1",
    "org.ow2.asm:asm-commons:9.10.1",
    "org.ow2.asm:asm-tree:9.10.1",
    "org.ow2.asm:asm-util:9.10.1",
    "org.ow2.asm:asm-analysis:9.10.1",
    "org.apache.commons:commons-text:1.14.0",
    "org.apache.commons:commons-lang3:3.18.0",
)
REPORT_CLASS = "org.pitest.mutationtest.commandline.MutationCoverageReport"

_ORDER = """\
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Properties;
import org.pitest.classpath.CodeSource;
import org.pitest.coverage.CoverageDatabase;
import org.pitest.coverage.TestInfo;
import org.pitest.mutationtest.build.DefaultTestPrioritiser;
import org.pitest.mutationtest.build.TestPrioritiser;
import org.pitest.mutationtest.build.TestPrioritiserFactory;

/** PIT's choice of the tests that cover a mutant, in the order of their
 *  names instead of their measured times. */
public class LangAdapterTestOrder implements TestPrioritiserFactory {
    @Override
    public String description() {
        return "langadapter: covering tests in the order of their names";
    }

    @Override
    public TestPrioritiser makeTestPrioritiser(Properties properties,
            CodeSource code, CoverageDatabase coverage) {
        DefaultTestPrioritiser pit = new DefaultTestPrioritiser(coverage);
        return mutation -> {
            List<TestInfo> tests = new ArrayList<>(pit.assignTests(mutation));
            tests.sort(Comparator.comparing(TestInfo::getName));
            return tests;
        };
    }
}
"""
# No mutators named measures ``Mutator.newDefaults()``, not the ``DEFAULTS``
# group: the former negates conditionals where the group removes them.

_LISTER = """\
import java.util.Arrays;
import org.pitest.mutationtest.engine.gregor.MethodMutatorFactory;
import org.pitest.mutationtest.engine.gregor.config.Mutator;

public class LangAdapterPitMutators {
    public static void main(String[] args) {
        java.util.Collection<MethodMutatorFactory> chosen;
        try {
            chosen = args.length == 0
                    ? Mutator.newDefaults() : Mutator.fromStrings(Arrays.asList(args));
        } catch (org.pitest.help.PitHelpError e) {
            System.err.println(e.getMessage());
            System.exit(UNKNOWN);
            return;
        }
        for (MethodMutatorFactory factory : chosen) {
            System.out.println(factory.getGloballyUniqueId());
        }
    }

    static final int UNKNOWN = 2;
}
"""

#: The lister's exit status for a name PIT does not know (``UNKNOWN`` in
#: ``_LISTER``). Any other failure is the listing's own.
_UNKNOWN_EXIT = 2


def classpath(environment: Mapping[str, str], *,
              executor: Optional[Executor] = None,
              scratch: Optional[Path] = None) -> Tuple[str, ...]:
    """PIT's jars, fetched into the tool cache when missing."""
    if isinstance(executor, ContainerExecutor):
        if scratch is None:
            raise OSError("container PIT tools require mounted scratch")
        require_mounted_output(str(scratch), executor)
        return _tools.stage_files([_tools.jar(c, environment) for c in JARS],
                                  scratch / "tools", executor=executor)
    return tuple(str(_tools.jar(c, environment)) for c in JARS)


def test_order(javac: str, environment: Mapping[str, str],
               env: Dict[str, str], *, executor: Optional[Executor] = None,
               scratch: Optional[Path] = None) -> Path:
    """The compiled test-order plugin's directory, its service registered."""
    return _tools.compiled_helper(
        javac, "LangAdapterTestOrder", _ORDER, environment, env,
        prefix="pit-order", what="PIT's test-order plugin",
        classpath=lambda: classpath(environment),
        service="org.pitest.mutationtest.build.TestPrioritiserFactory",
        executor=executor, scratch=scratch)[0]


class UnknownMutator(ValueError):
    """PIT knows no mutator or group of that name."""


#: The time limit, in seconds, of listing PIT's mutators.
_LISTING_TIMEOUT_S = 300


def mutator_ids(java: str, names: Sequence[str], scratch: Path, *,
                environment: Mapping[str, str], env: Dict[str, str],
                executor: Optional[Executor] = None) -> Tuple[str, ...]:
    """The sorted ids PIT reports for the mutators ``names`` select
    (``--mutators`` names, empty for PIT's default)."""
    done = _list_mutators(java, names, scratch, environment=environment,
                          env=env, timeout=_LISTING_TIMEOUT_S, executor=executor)
    if done.returncode == _UNKNOWN_EXIT:
        raise UnknownMutator((done.stdout + done.stderr).strip()[-1000:])
    if done.returncode != 0:
        raise OSError(f"PIT's mutator listing failed (exit {done.returncode}): "
                      + (done.stdout + done.stderr).strip()[-1000:])
    return tuple(sorted(line.strip() for line in done.stdout.splitlines()
                        if line.strip()))


def _list_mutators(java: str, names: Sequence[str], scratch: Path, *,
                   environment: Mapping[str, str], env: Dict[str, str],
                   timeout: float, executor: Optional[Executor]):
    require_mounted_output(str(scratch), executor)
    source = scratch / "LangAdapterPitMutators.java"
    source.write_text(_LISTER, encoding="utf-8")
    place = JavaEnv(executor)
    jars = classpath(environment, executor=executor, scratch=scratch)
    arguments = [java, "-cp", place.separator.join(jars), place.translate(source), *names]
    return place.launch(arguments, timeout, env=env)


@dataclass(frozen=True)
class Report:
    """What a PIT run did: its exit code (None when the wall clock ended
    it), its output, and its ``mutations.xml`` when it wrote one."""

    returncode: Optional[int]
    output: str
    mutations: Optional[Path]
    timed_out: bool = False
    #: Why only first killers are known although all were asked for, or "".
    note: str = ""


def _arg_line(arguments: Sequence[str]) -> str:
    """Arguments as one PIT ``argLine``, quoted where they hold a space."""
    out = []
    for argument in arguments:
        text = str(argument)
        if not text or any(c.isspace() for c in text):
            text = f'"{text}"'
        out.append(text)
    return " ".join(out)


def run(java: str, *, scratch: Path, target_classes: Sequence[str],
        test_classes: Sequence[str], source_dirs: Sequence[str],
        test_classpath: Sequence[str], mutable_paths: Sequence[str],
        mutators: Sequence[str], all_killers: bool, threads: int,
        timeout_factor: float, timeout_constant_s: int,
        jvm_args: Sequence[str],
        system_properties: Mapping[str, str], encoding: str, cwd: str,
        env: Dict[str, str], environment: Mapping[str, str],
        wall_clock_s: float, plugins: Sequence[str] = (),
        executor: Optional[Executor] = None, unit_size: int = 0) -> Report:
    """Run PIT on ``target_classes`` and their nested classes with the tests
    ``test_classes`` (the normal run's top-level classes: a Jupiter nested
    class runs through its outer one, a JUnit 4 nested class only where
    the normal run runs it) on ``test_classpath`` (in the normal run's
    order) and the named ``mutators`` (none: PIT's default). ``plugins``
    join PIT's classpath. ``unit_size`` is
    ``mutationUnitSize`` (0, PIT's default: one unit per class).

    ``all_killers`` asks for every killing test, which needs every covering
    test run (``fullMutationMatrix``), where a hang ends the minion and
    records a killed mutant as timed out. So the statuses come from a run
    that stops at the first killer, the killers from a second run within
    the same ``wall_clock_s``, merged by ``with_every_killer``."""
    options = dict(target_classes=target_classes, test_classes=test_classes,
                   source_dirs=source_dirs, test_classpath=test_classpath,
                   mutable_paths=mutable_paths, mutators=mutators, threads=threads,
                   timeout_factor=timeout_factor, timeout_constant_s=timeout_constant_s,
                   jvm_args=jvm_args, system_properties=system_properties,
                   encoding=encoding, cwd=cwd, env=env, environment=environment,
                   plugins=plugins, executor=executor, unit_size=unit_size)
    if not all_killers:
        return _launch(java, scratch=scratch, all_killers=False,
                       wall_clock_s=wall_clock_s, **options)
    import time

    started = time.monotonic()
    first = _launch(java, scratch=scratch / "first", all_killers=False,
                    wall_clock_s=wall_clock_s, **options)
    if first.timed_out or first.returncode != 0 or first.mutations is None:
        return first
    left = wall_clock_s - (time.monotonic() - started)
    every = (_launch(java, scratch=scratch / "every", all_killers=True,
                     wall_clock_s=left, **options) if left > 0 else None)
    usable = (every is not None and not every.timed_out and every.returncode == 0
              and every.mutations is not None)
    merged = scratch / "mutations.xml"
    _merge_every(first.mutations, every.mutations if usable else None, merged)
    return Report(first.returncode, first.output, merged, note="" if usable else (
        "PIT's run of every covering test gave no report, so each kill names "
        "its first killing test"))


def _launch(java: str, *, scratch: Path, target_classes: Sequence[str],
            test_classes: Sequence[str], source_dirs: Sequence[str],
            test_classpath: Sequence[str], mutable_paths: Sequence[str],
            mutators: Sequence[str], all_killers: bool, threads: int,
            timeout_factor: float, timeout_constant_s: int,
            jvm_args: Sequence[str],
            system_properties: Mapping[str, str], encoding: str, cwd: str,
            env: Dict[str, str], environment: Mapping[str, str],
            wall_clock_s: float, plugins: Sequence[str] = (),
            executor: Optional[Executor] = None, unit_size: int = 0) -> Report:
    """One PIT launch. ``all_killers`` sets PIT's ``fullMutationMatrix``."""
    require_mounted_output(str(scratch), executor)
    container = isinstance(executor, ContainerExecutor)
    translate = executor._to_container if container else str
    reports = scratch / "pit"
    reports.mkdir(parents=True, exist_ok=True)
    classpath_file = scratch / "pit-classpath.txt"
    classpath_file.write_text("\n".join(translate(str(e)) for e in test_classpath) + "\n",
                              encoding="utf-8")
    child = [*(translate(argument) for argument in jvm_args),
             *(translate(f"-D{key}={value}") for key, value in system_properties.items())]
    jars = classpath(environment, executor=executor, scratch=scratch)
    arguments: List[str] = [
        "-cp", JavaEnv(executor).separator.join(
            [*jars, *(translate(str(path)) for path in plugins)]),
        REPORT_CLASS,
        "--reportDir", translate(str(reports)),
        "--targetClasses", ",".join(f"{name},{name}$*" for name in target_classes),
        "--targetTests", ",".join(test_classes),
        "--sourceDirs", ",".join(translate(str(path)) for path in source_dirs),
        "--classPathFile", translate(str(classpath_file)),
        "--mutableCodePaths", ",".join(translate(str(path)) for path in mutable_paths),
        "--outputFormats", "XML",
        "--fullMutationMatrix", "true" if all_killers else "false",
        "--timestampedReports", "false",
        "--threads", str(threads),
        "--mutationUnitSize", str(int(unit_size)),
        "--timeoutConst", str(int(timeout_constant_s) * 1000),
        "--timeoutFactor", repr(float(timeout_factor)),
        "--jvmPath", translate(java),
        "--failWhenNoMutations", "false",
        "--useClasspathJar", "true",
        "--inputEncoding", encoding,
        "--outputEncoding", "UTF-8",
    ]
    if mutators:
        arguments += ["--mutators", ",".join(mutators)]
    if child:
        arguments += ["--argLine", _arg_line(child)]
    file = _tools.argfile(scratch / "pit.args", arguments)
    xml = reports / "mutations.xml"
    try:
        done = JavaEnv(executor).launch([java, f"@{translate(str(file))}"], wall_clock_s,
                                        cwd=cwd, env=env)
    except subprocess.TimeoutExpired as expired:
        reraise_unresolved(expired)
        output = "".join(value.decode("utf-8", "replace")
                         if isinstance(value, bytes) else (value or "")
                         for value in (expired.output, expired.stderr))
        return Report(None, output,
                      xml if xml.is_file() else None, timed_out=True)
    return Report(done.returncode, (done.stdout or "") + (done.stderr or ""),
                  xml if xml.is_file() else None)


#: PIT's message when the suite fails without a mutant.
RED_SUITE = "did not pass without mutation"


def red_tests(output: str) -> Tuple[str, ...]:
    """The JUnit Platform unique ids of the tests PIT's ``output`` names as
    failing without a mutant, in its order, each once: PIT 1.30 writes
    ``Description [testClass=<class>, name=<unique id>] did not pass
    without mutation.`` for each."""
    names: List[str] = []
    for line in output.splitlines():
        start, end = line.find(", name="), line.find("] " + RED_SUITE)
        if 0 <= start < end:
            name = line[start + len(", name="):end]
            if name not in names:
                names.append(name)
    return tuple(names)
#: PIT's message, without a report, when no mutant exists for the target.
NO_MUTATIONS = "No mutations found"


@dataclass(frozen=True)
class Record:
    """One mutation as PIT's report records it, with PIT's test names."""

    status: str
    mutated_class: str
    method: str
    description_of_method: str
    line: int
    mutator: str
    indexes: Tuple[int, ...]
    description: str
    killers: Tuple[str, ...]
    succeeding: Tuple[str, ...]
    tests_run: int
    #: False when ``killers`` is only the first though all were asked for.
    killers_complete: bool = True

    @property
    def engine_id(self) -> str:
        """The mutation's identity within the run, as PIT identifies it:
        the class, the method, the instructions and the mutator."""
        indexes = "+".join(str(i) for i in self.indexes)
        return (f"{self.mutated_class}.{self.method}{self.description_of_method}"
                f"@{indexes}:{self.mutator.rsplit('.', 1)[-1]}")

    def sort_key(self) -> tuple:
        return (self.line, self.mutated_class, self.method,
                self.description_of_method, self.indexes, self.mutator)


def _names(text: Optional[str]) -> Tuple[str, ...]:
    return tuple(n for n in (text or "").split("|") if n)


def _record(node: Any) -> Record:
    killers = node.findtext("killingTests")
    if killers is None:
        killers = node.findtext("killingTest")
    return Record(
        status=node.get("status", ""),
        mutated_class=node.findtext("mutatedClass", ""),
        method=node.findtext("mutatedMethod", ""),
        description_of_method=node.findtext("methodDescription", ""),
        line=int(node.findtext("lineNumber", "0")),
        mutator=node.findtext("mutator", ""),
        indexes=tuple(int(i.text) for i in node.iter("index") if i.text),
        description=node.findtext("description", ""),
        killers=_names(killers),
        succeeding=_names(node.findtext("succeedingTests")),
        tests_run=int(node.get("numberOfTestsRun", "0")),
        killers_complete=node.get(_INCOMPLETE) != "true")


#: The merged report's mark of a kill that names only its first killer.
_INCOMPLETE = "killersIncomplete"


def read(xml: Path) -> List[Record]:
    """The mutations of PIT's XML report, ordered by line and identity."""
    return sorted((_record(node) for node in ET.parse(xml).getroot().iter("mutation")),
                  key=Record.sort_key)


def _merge_every(first: Path, every: Optional[Path], out: Path) -> None:
    """Write ``first``'s report with ``every``'s killers to ``out``
    (``with_every_killer``). Without ``every`` each kill is incomplete."""
    tree = ET.parse(first)
    merged = {r.engine_id: r for r in with_every_killer(
        [_record(n) for n in tree.getroot().iter("mutation")],
        read(every) if every is not None else [])}
    for node in tree.getroot().iter("mutation"):
        record = merged[_record(node).engine_id]
        if not record.killers_complete:
            node.set(_INCOMPLETE, "true")
            continue
        for name in ("killingTest", "killingTests", "succeedingTests"):
            for child in node.findall(name):
                node.remove(child)
        ET.SubElement(node, "killingTests").text = "|".join(record.killers)
        ET.SubElement(node, "succeedingTests").text = "|".join(record.succeeding)
        node.set("numberOfTestsRun", str(record.tests_run))
    tree.write(out, encoding="utf-8", xml_declaration=True)


def with_every_killer(first: Sequence[Record], every: Sequence[Record]) -> List[Record]:
    """The records of a run that stops at each first killing test
    (``first``) with the killers of a run of every covering test
    (``every``). Each mutation keeps the first run's status, and a kill
    takes the full run's killers. Where the full run did not record the
    kill (a later covering test hung and PIT ended the minion), the kill
    keeps its first killer, marked incomplete, so a hang after a kill
    neither turns it into a timeout nor drops its killer."""
    by_id = {record.engine_id: record for record in every}
    merged = []
    for record in first:
        full = by_id.get(record.engine_id)
        if record.status != "KILLED":
            merged.append(record)
        elif full is not None and full.status == "KILLED" and full.killers:
            merged.append(replace(record, killers=full.killers, succeeding=full.succeeding,
                                  tests_run=full.tests_run))
        else:
            merged.append(replace(record, killers_complete=False))
    return merged


def occurrences(records: Sequence[Record]) -> List[int]:
    """Each record's occurrence among its mutator's records in ``read``'s
    order, which follows the mutations' identities, so runs over the same
    mutations agree, even for one mutator applied twice on a line."""
    counted: Dict[str, int] = {}
    result = []
    for record in records:
        result.append(counted.get(record.mutator, 0))
        counted[record.mutator] = result[-1] + 1
    return result


def classify(status: str, killers: Sequence[str]) -> Classification:
    """PIT's status of one mutation mapped to the kill-matrix status, the
    common class and its subcategory:

    =========================  =============  ===========================
    PIT status                 Status         Class, subcategory
    =========================  =============  ===========================
    KILLED, a killer named     killed         detected, failing_test
    KILLED, no killer named    killed         detected, no_named_test
    SURVIVED, NO_COVERAGE      survived       undetected, survived
    TIMED_OUT                  timeout        detected, timeout
    NON_VIABLE                 non_viable     detected, non_viable
    MEMORY_ERROR, RUN_ERROR    killed         detected, as KILLED
    any other (not started)    untested       not run, untested
    =========================  =============  ===========================

    No test reaches a NO_COVERAGE mutant, so the suite lets it live, as a
    Cosmic Ray run of every test would. A mutant that ran a minion out of
    memory or ended it (MEMORY_ERROR, RUN_ERROR) is detected, as PIT counts
    it: a kill, named by its killers if PIT lists any, never a timeout."""
    if status in ("KILLED", "MEMORY_ERROR", "RUN_ERROR"):
        return Classification(
            KILLED_STATUS, MutationClass.DETECTED,
            BY_FAILING_TEST if killers else WITHOUT_NAMED_TEST)
    if status in ("SURVIVED", "NO_COVERAGE"):
        return Classification(SURVIVED_STATUS, MutationClass.UNDETECTED,
                              SURVIVED)
    if status == "TIMED_OUT":
        return Classification(TIMEOUT_STATUS, MutationClass.DETECTED,
                              BY_TIMEOUT)
    if status == "NON_VIABLE":
        return Classification(NON_VIABLE_STATUS, MutationClass.DETECTED,
                              NON_VIABLE)
    return Classification(UNTESTED_STATUS, MutationClass.NOT_RUN, UNTESTED)
