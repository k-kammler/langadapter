"""Coverage through JaCoCo: its agent records the run, and a helper on its
analysis API gives the target file's lines and branches.

JaCoCo counts a line's branches without naming them, but a run covers all
its probes cover alone, and a line's branches form a laminar family of
probe sets (each covered by the probes downstream of it until control flow
merges). The helper recovers the family from each probe's and each
same-line pair's coverage and names a branch ``BranchItem(file, line,
ordinal)`` by its place in it, so runs taking different branches differ.
Where these do not reproduce a run's JaCoCo count for a line (a JaCoCo
filter merged duplicated code's branches), the line keeps the counters: its
first ``covered`` branches taken. The check is per run, independent of the
runs analyzed with it. Counts and percentages are JaCoCo's either way. A
class's family is cached by its bytes, in the tool cache or a container run's
scratch.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from langadapter.execution import ContainerExecutor, Executor
from langadapter.java import _tools
from langadapter.java._env import JavaEnv, require_mounted_output
from langadapter.runs import CoverageObservation
from langadapter.suites import BranchItem


VERSION = "0.8.15"
AGENT = f"org.jacoco:org.jacoco.agent:{VERSION}:runtime"
#: JaCoCo's analysis API and the ASM it is built with.
CORE = (
    f"org.jacoco:org.jacoco.core:{VERSION}",
    "org.ow2.asm:asm:9.10.1",
    "org.ow2.asm:asm-commons:9.10.1",
    "org.ow2.asm:asm-tree:9.10.1",
)
#: The version of the branch identity's rule and of its cache entries.
IDENTITY = "probe-family-v2"

_ANALYZER = """\
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.BitSet;
import java.util.Comparator;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import org.jacoco.core.analysis.Analyzer;
import org.jacoco.core.analysis.CoverageBuilder;
import org.jacoco.core.analysis.IClassCoverage;
import org.jacoco.core.analysis.ILine;
import org.jacoco.core.analysis.IMethodCoverage;
import org.jacoco.core.data.ExecutionData;
import org.jacoco.core.data.ExecutionDataStore;
import org.jacoco.core.tools.ExecFileLoader;

/** The target source file's coverage from JaCoCo's execution data, with
 *  each covered branch named by the probes that cover it.
 *
 *  Arguments: the mode ("tests" with the per-test data directory, or "run"
 *  with one execution data file), the data, the identity cache directory
 *  (empty for none), the source file's package path and name, the class
 *  files. Output, tab-separated: in mode "tests" one record per test, its
 *  number and each covered line as line:ordinals (comma-separated covered
 *  branches), without the lines of static initializers; in both modes a
 *  record "suite" for all data, each line with instructions as
 *  line:covered(0 or 1):branches:ordinals. */
public class LangAdapterCoverageAnalyzer {

    /** A class compiled from the target source file. */
    static final class Target {
        final String name;
        final long id;
        final byte[] bytes;
        /** Per line with instructions: {branches}. */
        final Map<Integer, Integer> branches = new TreeMap<>();
        final Set<Integer> initializers = new HashSet<>();
        int probes = -1;
        /** Per line: the probes covering each branch, in the family's order. */
        final Map<Integer, List<BitSet>> families = new HashMap<>();

        Target(String name, long id, byte[] bytes) {
            this.name = name;
            this.id = id;
            this.bytes = bytes;
        }
    }

    /** One analyzed run: per target its probes and per line
     *  {instructions covered, branches covered}. */
    static final class Run {
        final String name;
        final List<BitSet> probes = new ArrayList<>();
        final List<Map<Integer, int[]>> lines = new ArrayList<>();
        ExecutionDataStore store;

        Run(String name) {
            this.name = name;
        }
    }

    static IClassCoverage analyze(String name, byte[] bytes, long id, boolean[] probes)
            throws IOException {
        ExecutionDataStore store = new ExecutionDataStore();
        if (probes != null) {
            store.put(new ExecutionData(id, name, probes));
        }
        CoverageBuilder builder = new CoverageBuilder();
        new Analyzer(store, builder).analyzeClass(bytes, name);
        for (IClassCoverage c : builder.getClasses()) {
            return c;
        }
        return null;
    }

    /** Per line with instructions: {instructions covered, branches covered}. */
    static Map<Integer, int[]> lines(Target t, boolean[] probes) throws IOException {
        IClassCoverage c = analyze(t.name, t.bytes, t.id, probes);
        Map<Integer, int[]> out = new HashMap<>();
        for (int l = c.getFirstLine(); l > 0 && l <= c.getLastLine(); l++) {
            ILine line = c.getLine(l);
            if (line.getInstructionCounter().getTotalCount() > 0) {
                out.put(l, new int[] {line.getInstructionCounter().getCoveredCount(),
                                      line.getBranchCounter().getCoveredCount()});
            }
        }
        return out;
    }

    /** Appends the sets of a laminar family over the probes members
     *  (indexes into r) below the level sets they all share, outermost
     *  first; false when the counts m (sets holding both of two probes)
     *  are not those of a laminar family. */
    static boolean build(List<Integer> members, int level, int[][] m, List<Integer> r,
                         List<BitSet> out) {
        List<Integer> deeper = new ArrayList<>();
        for (int i : members) {
            if (m[i][i] > level) {
                deeper.add(i);
            }
        }
        boolean[] taken = new boolean[deeper.size()];
        List<List<Integer>> groups = new ArrayList<>();
        for (int x = 0; x < deeper.size(); x++) {
            if (taken[x]) {
                continue;
            }
            List<Integer> group = new ArrayList<>();
            for (int y = x; y < deeper.size(); y++) {
                if (!taken[y] && m[deeper.get(x)][deeper.get(y)] > level) {
                    taken[y] = true;
                    group.add(deeper.get(y));
                }
            }
            groups.add(group);
        }
        for (int a = 0; a < groups.size(); a++) {
            for (int b = 0; b < groups.size(); b++) {
                for (int i : groups.get(a)) {
                    for (int j : groups.get(b)) {
                        if (m[i][j] < level || (m[i][j] > level) != (a == b)) {
                            return false;
                        }
                    }
                }
            }
        }
        for (List<Integer> group : groups) {
            BitSet set = new BitSet();
            for (int i : group) {
                set.set(r.get(i));
            }
            out.add(set);
            if (!build(group, level + 1, m, r, out)) {
                return false;
            }
        }
        return true;
    }

    /** The branch families of a class's lines from the coverage of each
     *  probe alone and of each pair of probes covering branches of a line. */
    static void identify(Target t) throws IOException {
        int n = t.probes;
        List<Map<Integer, int[]>> single = new ArrayList<>();
        for (int p = 0; p < n; p++) {
            boolean[] probes = new boolean[n];
            probes[p] = true;
            single.add(lines(t, probes));
        }
        Map<Long, Map<Integer, int[]>> pairs = new HashMap<>();
        for (Map.Entry<Integer, Integer> entry : t.branches.entrySet()) {
            int line = entry.getKey();
            int total = entry.getValue();
            if (total == 0) {
                continue;
            }
            List<Integer> r = new ArrayList<>();
            for (int p = 0; p < n; p++) {
                if (single.get(p).get(line)[1] > 0) {
                    r.add(p);
                }
            }
            int[][] m = new int[r.size()][r.size()];
            for (int i = 0; i < r.size(); i++) {
                int a = r.get(i);
                m[i][i] = single.get(a).get(line)[1];
                for (int j = i + 1; j < r.size(); j++) {
                    int b = r.get(j);
                    long key = (long) a * n + b;
                    Map<Integer, int[]> union = pairs.get(key);
                    if (union == null) {
                        boolean[] probes = new boolean[n];
                        probes[a] = true;
                        probes[b] = true;
                        union = lines(t, probes);
                        pairs.put(key, union);
                    }
                    m[i][j] = m[j][i] = m[i][i] + single.get(b).get(line)[1]
                            - union.get(line)[1];
                }
            }
            List<Integer> all = new ArrayList<>();
            for (int i = 0; i < r.size(); i++) {
                all.add(i);
            }
            List<BitSet> family = new ArrayList<>();
            if (build(all, 0, m, r, family) && family.size() <= total) {
                while (family.size() < total) {
                    family.add(new BitSet());
                }
                t.families.put(line, family);
            }
        }
    }

    static String hex(byte[] bytes) throws Exception {
        StringBuilder out = new StringBuilder();
        for (byte b : MessageDigest.getInstance("SHA-256").digest(bytes)) {
            out.append(String.format("%02x", b));
        }
        return out.toString();
    }

    /** The class's families from the cache, else identified and cached. */
    static void families(Target t, Path cache) throws Exception {
        Path file = cache == null ? null : cache.resolve(hex(t.bytes) + ".txt");
        if (file != null && Files.isRegularFile(file)) {
            for (String row : Files.readAllLines(file, StandardCharsets.UTF_8)) {
                String[] parts = row.split("\\t", -1);
                List<BitSet> family = new ArrayList<>();
                for (String set : parts[1].split(";", -1)) {
                    BitSet bits = new BitSet();
                    for (String p : set.split(",")) {
                        if (!p.isEmpty()) {
                            bits.set(Integer.parseInt(p));
                        }
                    }
                    family.add(bits);
                }
                t.families.put(Integer.parseInt(parts[0]), family);
            }
            return;
        }
        identify(t);
        if (file == null) {
            return;
        }
        StringBuilder text = new StringBuilder();
        for (Map.Entry<Integer, List<BitSet>> e : new TreeMap<>(t.families).entrySet()) {
            text.append(e.getKey()).append('\\t');
            List<BitSet> family = e.getValue();
            for (int i = 0; i < family.size(); i++) {
                if (i > 0) {
                    text.append(';');
                }
                BitSet bits = family.get(i);
                StringBuilder set = new StringBuilder();
                for (int p = bits.nextSetBit(0); p >= 0; p = bits.nextSetBit(p + 1)) {
                    if (set.length() > 0) {
                        set.append(',');
                    }
                    set.append(p);
                }
                text.append(set);
            }
            text.append('\\n');
        }
        Files.createDirectories(cache);
        Path temp = Files.createTempFile(cache, "family-", ".tmp");
        Files.write(temp, text.toString().getBytes(StandardCharsets.UTF_8));
        try {
            Files.move(temp, file, StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException e) {
            Files.deleteIfExists(temp);
        }
    }

    static Run run(String name, List<Path> files, List<Target> targets) throws IOException {
        ExecFileLoader loader = new ExecFileLoader();
        for (Path file : files) {
            loader.load(file.toFile());
        }
        ExecutionDataStore store = loader.getExecutionDataStore();
        Run run = new Run(name);
        run.store = store;
        for (Target t : targets) {
            ExecutionData data = store.get(t.id);
            boolean[] probes = data == null ? null : data.getProbes();
            BitSet bits = new BitSet();
            for (int p = 0; probes != null && p < probes.length; p++) {
                if (probes[p]) {
                    bits.set(p);
                }
            }
            run.probes.add(bits);
            run.lines.add(lines(t, probes));
        }
        return run;
    }

    /** The record of a run: per line its covered branches' ordinals, the
     *  lines of several classes numbered one after the other. */
    static String record(Run run, List<Target> targets, boolean suite) {
        TreeMap<Integer, int[]> totals = new TreeMap<>();
        TreeMap<Integer, StringBuilder> ordinals = new TreeMap<>();
        Set<Integer> initializers = new HashSet<>();
        for (int k = 0; k < targets.size(); k++) {
            Target t = targets.get(k);
            initializers.addAll(t.initializers);
            BitSet probes = run.probes.get(k);
            for (Map.Entry<Integer, Integer> e : t.branches.entrySet()) {
                int line = e.getKey();
                int[] total = totals.computeIfAbsent(line, x -> new int[2]);
                int offset = total[1];
                int[] found = run.lines.get(k).get(line);
                total[0] += found[0];
                total[1] += e.getValue();
                StringBuilder out = ordinals.computeIfAbsent(line, x -> new StringBuilder());
                List<BitSet> family = t.families.get(line);
                if (family != null) {
                    int predicted = 0;
                    for (BitSet set : family) {
                        if (set.intersects(probes)) {
                            predicted++;
                        }
                    }
                    if (predicted != found[1]) {
                        family = null;
                    }
                }
                for (int i = 0; i < e.getValue(); i++) {
                    boolean covered = family != null ? family.get(i).intersects(probes)
                                                     : i < found[1];
                    if (covered) {
                        if (out.length() > 0) {
                            out.append(',');
                        }
                        out.append(offset + i);
                    }
                }
            }
        }
        StringBuilder out = new StringBuilder(run.name);
        for (Map.Entry<Integer, int[]> e : totals.entrySet()) {
            int line = e.getKey();
            boolean covered = e.getValue()[0] > 0;
            if (suite) {
                out.append('\\t').append(line).append(':').append(covered ? 1 : 0)
                   .append(':').append(e.getValue()[1]).append(':')
                   .append(ordinals.get(line));
            } else if (covered && !initializers.contains(line)) {
                out.append('\\t').append(line).append(':').append(ordinals.get(line));
            }
        }
        return out.toString();
    }

    public static void main(String[] args) throws Exception {
        String mode = args[0];
        Path data = Paths.get(args[1]);
        Path cache = args[2].isEmpty() ? null : Paths.get(args[2]);
        String packagePath = args[3];
        String sourceName = args[4];
        List<Target> targets = new ArrayList<>();
        for (int i = 5; i < args.length; i++) {
            byte[] bytes = Files.readAllBytes(Paths.get(args[i]));
            CoverageBuilder builder = new CoverageBuilder();
            new Analyzer(new ExecutionDataStore(), builder).analyzeClass(bytes, args[i]);
            for (IClassCoverage c : builder.getClasses()) {
                if (!sourceName.equals(c.getSourceFileName())
                        || !packagePath.equals(c.getPackageName())) {
                    continue;
                }
                Target t = new Target(c.getName(), c.getId(), bytes);
                for (int l = c.getFirstLine(); l > 0 && l <= c.getLastLine(); l++) {
                    ILine line = c.getLine(l);
                    if (line.getInstructionCounter().getTotalCount() > 0) {
                        t.branches.put(l, line.getBranchCounter().getTotalCount());
                    }
                }
                for (IMethodCoverage m : c.getMethods()) {
                    if (!m.getName().equals("<clinit>")) {
                        continue;
                    }
                    for (int l = m.getFirstLine(); l > 0 && l <= m.getLastLine(); l++) {
                        if (m.getLine(l).getInstructionCounter().getTotalCount() > 0) {
                            t.initializers.add(l);
                        }
                    }
                }
                targets.add(t);
            }
        }
        if (targets.isEmpty()) {
            return;
        }
        targets.sort(Comparator.comparing(t -> t.name));
        List<Run> runs = new ArrayList<>();
        List<Path> all = new ArrayList<>();
        if (mode.equals("tests")) {
            try (DirectoryStream<Path> files = Files.newDirectoryStream(data, "*.exec")) {
                for (Path file : files) {
                    all.add(file);
                }
            }
            all.sort(Comparator.naturalOrder());
            Path index = data.resolve("index.tsv");
            if (Files.exists(index)) {
                for (String entry : Files.readAllLines(index, StandardCharsets.UTF_8)) {
                    String number = entry.split("\\t", 2)[0];
                    List<Path> one = new ArrayList<>();
                    one.add(data.resolve(number + ".exec"));
                    runs.add(run(number, one, targets));
                }
            }
        } else {
            all.add(data);
        }
        if (all.isEmpty()) {
            return;
        }
        Run suite = run("suite", all, targets);
        runs.add(suite);
        for (int k = 0; k < targets.size(); k++) {
            Target t = targets.get(k);
            ExecutionData known = suite.store.get(t.id);
            if (known == null) {
                continue;
            }
            t.probes = known.getProbes().length;
            families(t, cache);
        }
        for (Run r : runs) {
            System.out.println(record(r, targets, r == suite));
        }
    }
}
"""


def _source_of(target: Path, executor: Optional[Executor]) -> Tuple[Path, str]:
    """The target's path as the executing side sees it, and its text."""
    from langadapter.java._java_source import read_source
    if isinstance(executor, ContainerExecutor):
        files = JavaEnv(executor)
        path = files.path(target.as_posix())
        return path, files.read(path)
    return target, read_source(target)


def _qualified(text: str, name: str) -> str:
    from langadapter.java._java_source import declared_package
    package = declared_package(text)
    return (package + "." if package else "") + name


def class_name(target: Path, *, executor: Optional[Executor] = None) -> str:
    """The class a Java source file is named for, with its package."""
    path, text = _source_of(target, executor)
    return _qualified(text, path.stem)


def class_names(target: Path, *, executor: Optional[Executor] = None) -> Tuple[str, ...]:
    """The top-level types a Java source file declares, with its package,
    in their order: every class compiled from the file, which the analysis
    keeps. The file's own class alone when the source does not parse."""
    from langadapter.java.source_analysis import _types
    path, text = _source_of(target, executor)
    return tuple(_qualified(text, name) for name in (_types(text) or [path.stem]))


def includes(names: Sequence[str]) -> str:
    """JaCoCo's include pattern of the classes ``names`` and their nested
    classes."""
    return ":".join(f"{name}:{name}$*" for name in names)


def agent_argument(destfile: Path, target_classes: Sequence[str],
                   environment: Mapping[str, str], *,
                   executor: Optional[Executor] = None,
                   scratch: Optional[Path] = None) -> str:
    """The JVM argument recording the coverage of ``target_classes``, nested
    classes included, into ``destfile``."""
    if isinstance(executor, ContainerExecutor):
        require_mounted_output(str(destfile), executor)
        scratch = scratch if scratch is not None else destfile.parent / "tools"
        require_mounted_output(str(scratch), executor)
    agent = str(_tools.jar(AGENT, environment))
    if isinstance(executor, ContainerExecutor):
        agent = _tools.stage_files((agent,), scratch, executor=executor)[0]
        destfile = executor._to_container(str(destfile))
    return (f"-javaagent:{agent}=destfile={destfile},includes={includes(target_classes)},"
            "output=file,append=false")


def class_files(main_classes: Sequence[str], target: Path, *,
                executor: Optional[Executor] = None) -> List[str]:
    """The class files of the target's package. The analysis keeps those
    compiled from the target's source file. A class directory without the
    package, such as a Gradle project's directory of another language, has
    none."""
    package = class_name(target, executor=executor).rpartition(".")[0].replace(".", "/")
    if isinstance(executor, ContainerExecutor):
        files = JavaEnv(executor)
        found = []
        for directory in main_classes:
            path = files.path(directory) / package
            if not files.is_dir(path):
                continue
            done = executor.run(["find", str(path), "-maxdepth", "1", "-type", "f",
                                 "-name", "*.class"], timeout=60)
            if done.returncode != 0:
                raise OSError(f"cannot list container class files at {path}: {done.stderr[-500:]}")
            found.extend(done.stdout.splitlines())
        return sorted(found)
    return sorted(str(p) for directory in main_classes
                  for p in (Path(directory) / package).glob("*.class"))


def analyzer(jdk: Any, environment: Mapping[str, str],
             env: Dict[str, str], *, executor: Optional[Executor] = None,
             scratch: Optional[Path] = None) -> Tuple[Path, Tuple[str, ...]]:
    """The directory of the compiled analysis helper and JaCoCo's analysis
    jars as the run sees them (``_tools.compiled_helper``)."""
    return _tools.compiled_helper(
        jdk.tool("javac"), "LangAdapterCoverageAnalyzer", _ANALYZER, environment, env,
        prefix="analyzer", what="the coverage analyzer",
        classpath=lambda: [_tools.jar(c, environment) for c in CORE],
        executor=executor, scratch=scratch)


#: The time limit, in seconds, of one coverage analysis.
_ANALYSIS_TIMEOUT_S = 600


def analyze(jdk: Any, mode: str, data: Path, target: Path,
            main_classes: Sequence[str], *, environment: Mapping[str, str],
            env: Dict[str, str],
            executor: Optional[Executor] = None, scratch: Optional[Path] = None
            ) -> Tuple[Optional[Dict[str, List[str]]], str]:
    """The helper's records of ``data`` by name (mode ``tests``: the per-test
    directory, ``run``: one execution data file), or None and why it failed."""
    helper, core = analyzer(jdk, environment, env, executor=executor, scratch=scratch)
    package = class_name(target, executor=executor).rpartition(".")[0].replace(".", "/")
    if isinstance(executor, ContainerExecutor):
        cache = scratch / "identity" / f"jacoco-{VERSION}-{IDENTITY}"
        files = JavaEnv(executor)
        command = [jdk.tool("java"), "-cp", ":".join([str(files.path(helper)), *core]),
                   "LangAdapterCoverageAnalyzer", mode, str(files.path(data)),
                   str(files.path(cache)), package, files.path(target.as_posix()).name,
                   *class_files(main_classes, target, executor=executor)]
    else:
        cache = _tools.cache_dir(environment) / "identity" / f"jacoco-{VERSION}-{IDENTITY}"
        command = [jdk.tool("java"), "-cp", os.pathsep.join([str(helper), *core]),
                   "LangAdapterCoverageAnalyzer", mode, str(data), str(cache), package,
                   target.name, *class_files(main_classes, target)]
    done = JavaEnv(executor).launch(command, _ANALYSIS_TIMEOUT_S, env=env)
    if done.returncode != 0:
        return None, ("JaCoCo's analysis failed: "
                      + (done.stdout + done.stderr)[-1000:])
    records: Dict[str, List[str]] = {}
    for line in done.stdout.splitlines():
        name, *entries = line.split("\t")
        records[name] = entries
    return records, ""


def observation(entries: Optional[List[str]], target: Path,
                project_root: str, *, executor: Optional[Executor] = None
                ) -> Optional[CoverageObservation]:
    """The coverage of ``target`` from the analysis's ``suite`` record, or
    None without one (no class of the target's source file was found)."""
    if entries is None:
        return None
    identity = JavaEnv(executor).identity(target, project_root)
    statements, covered, branches, taken = [], [], [], []
    for entry in entries:
        number, flag, total, ordinals = entry.split(":")
        line = int(number)
        statements.append(line)
        if flag == "1":
            covered.append(line)
        hit = {int(o) for o in ordinals.split(",") if o}
        for ordinal in range(int(total)):
            item = BranchItem(identity.path, line, ordinal)
            branches.append(item)
            if ordinal in hit:
                taken.append(item)
    return CoverageObservation(
        target=identity, statements=tuple(statements), covered=tuple(covered),
        branches=tuple(branches), taken=tuple(taken))
