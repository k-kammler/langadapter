"""Cosmic Ray's settings from a mutation configuration (operators, distributor
and workers, session settings, session directory), the durable backup of a
mutation target (``SourceBackup``), and timeout and crash evidence from
per-test journals.

A per-mutant run the mutation clock kills leaves no pytest report, so the
engine records a kill without a killer. The instrument, or else the journal
plugin, streams a flushed journal for every per-mutant run (a header with the
mutated module's SHA-1, a line per test start and report, an end line on a
normal exit), so the killed run's unended block still shows what it was
doing. Blocks pair with mutants by the SHA-1 of the module's text read as
UTF-8 with universal newlines: the header hashes the file the run imported,
which Cosmic Ray wrote as UTF-8, and the capture applies each job's diff to
the module as Cosmic Ray read it (``read_as_cosmic_ray``), so neither line
ends nor the declared encoding matter.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, List, Mapping, Optional, Tuple, Dict
from langadapter.mutation import (
    CUMULATIVE, HANG, KILLED_STATUS, NAMED_KILLER, TIMEOUT_STATUS, Catalogue,
)
from langadapter.python.cosmic_ray import (
    CosmicRaySettings, operator_set, operator_set_names, _resolve_distributor,
)
import json
import stat
import tempfile
from langadapter._owned_directory import OwnedDirectory, OwnershipRefused
import hashlib
import re


def _flag(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def configured_catalogue(config: Mapping[str, Any]) -> Catalogue:
    """``cosmic_ray_operators``, read strictly: empty for the full
    catalogue, a named operator set, or a comma list of operators."""
    configured = str(config["cosmic_ray_operators"] or "").strip()
    if not configured:
        return Catalogue()
    if "," not in configured and configured in operator_set_names():
        return Catalogue(name=configured)
    return Catalogue(operators=tuple(
        op.strip() for op in configured.split(",") if op.strip()))


def named_operators(name: str) -> List[str]:
    """The operators of the named operator set ``name``."""
    return list(operator_set(name))


def cosmic_ray_distributor(configured: Any, workers: int) -> Optional[str]:
    """The distributor for ``workers`` workers: ``local`` or ``http`` as
    configured, for ``auto`` (the default) http with more than one worker, else
    local. None for local with more than one (``distributor_refusal``)."""
    distributor = str(configured or "auto").strip().lower()
    if distributor == "local" and workers > 1:
        return None
    return _resolve_distributor(distributor, workers)


def distributor_refusal(workers: int) -> str:
    """Why a configured local distributor cannot run ``workers`` workers."""
    return (f"cosmic_ray_distributor = local runs one mutant at a time, but "
            f"engine_workers = {workers}: set cosmic_ray_distributor to "
            f"auto or http, or engine_workers to 1")


def _catalogue_cache_dir(config: Mapping[str, Any], base: str) -> Optional[str]:
    """``cosmic_ray_catalogue_cache_dir`` resolved: ``auto`` and a relative path
    under the caller's directory ``base``, an absolute one as given, None if empty."""
    raw = str(config.get("cosmic_ray_catalogue_cache_dir", "") or "").strip()
    if not raw:
        return None
    root = Path(base)
    path = (root / "cache" / "mutation" / "cosmic_ray" if raw.lower() == "auto"
            else Path(raw))
    if not path.is_absolute():
        path = root / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


def session_settings(config: Mapping[str, Any], log: Callable[..., None],
                     cache_base: str) -> CosmicRaySettings:
    """Cosmic Ray's settings from the mutation configuration, each absent
    key at ``CosmicRaySettings``' default."""
    defaults = CosmicRaySettings()
    clone = str(config.get("cosmic_ray_clone_dir", "") or "").strip()
    return CosmicRaySettings(
        init_timeout_s=int(config.get("cosmic_ray_init_timeout_s", defaults.init_timeout_s)),
        worker_ready_timeout_s=int(
            config.get("cosmic_ray_worker_ready_timeout_s", defaults.worker_ready_timeout_s)),
        catalogue_cache_dir=_catalogue_cache_dir(config, cache_base),
        cache_max_entries=int(
            config.get("cosmic_ray_cache_max_entries", defaults.cache_max_entries)),
        cache_max_age_days=int(
            config.get("cosmic_ray_cache_max_age_days", defaults.cache_max_age_days)),
        clone_dir=clone if clone and clone.lower() != "auto" else None,
        clone_sweep_hours=float(
            config.get("cosmic_ray_clone_sweep_hours", defaults.clone_sweep_hours)),
        session_dir=str(config.get("cosmic_ray_session_dir", "") or "")
        or None,
        local_session_dir=str(
            config.get("cosmic_ray_local_session_dir", "") or "").strip()
        or None,
        progress_interval_s=int(
            config.get("cosmic_ray_progress_interval_s", defaults.progress_interval_s) or 0),
        per_file_adjudication=_flag(
            config.get("cosmic_ray_per_file_adjudication"), defaults.per_file_adjudication),
        keep_output=_flag(config.get("cosmic_ray_keep_output"), defaults.keep_output),
        verbose=_flag(config.get("cosmic_ray_verbose"), defaults.verbose),
        log=log,
    )


#: The warning before the local fallback, ``{reason}`` the failed session's.
FALLBACK_WARNING = (
    "[mutation-session] WARNING: HTTP worker setup failed "
    "({reason}). Falling back to the sequential local "
    "distributor (allow_sequential_fallback = true): "
    "mutation runs in this session will be about {effective} times "
    "slower than the configured parallelism.")


def session_attempts(config: Mapping[str, Any], *, configured_workers: int,
                     effective_workers: int, cache_base: str,
                     log: Callable[..., None]) -> Tuple[Tuple[dict, int, bool, str], ...]:
    """The sessions to try in order, as ``(open_session options, workers,
    parallel, warning)``. An explicit http runs the configured workers, one
    included, but locally when a recovery lowered the count to one. With
    ``allow_sequential_fallback`` a local session follows a failed http
    one. A configured local with more than one worker raises ``ValueError``."""
    configured = int(configured_workers or 1)
    effective = effective_workers
    distributor = cosmic_ray_distributor(config.get("cosmic_ray_distributor"), configured)
    if distributor is None:
        raise ValueError(distributor_refusal(configured))
    use_http = distributor == "http" and (effective > 1 or configured == 1)
    settings = session_settings(config, log, cache_base)
    attempts = [({"workers": effective if use_http else 1,
                  "distributor": "http" if use_http else "local",
                  "allow_fewer_workers": True, "settings": settings},
                 effective if use_http else 1, use_http, "")]
    if use_http and _flag(config.get("allow_sequential_fallback"), False):
        attempts.append(({"workers": 1, "distributor": "local", "settings": settings}, 1, False,
                         FALLBACK_WARNING.replace("{effective}", str(effective))))
    return tuple(attempts)


def session_dir_refusal(config: Mapping[str, Any], *, create: bool) -> Optional[str]:
    """Why the configured ``cosmic_ray_local_session_dir`` cannot hold a local
    session, by the engine's own rule and words, or None when it can or none is
    set. ``create`` makes a missing directory, as a run does. Without it, a missing
    directory a run could create gives ``"missing"``."""
    from langadapter.python.cosmic_ray import SessionFolderRefused, check_local_session_dir

    configured = str(config.get("cosmic_ray_local_session_dir") or "").strip()
    if not configured:
        return None
    if not create and not Path(configured).exists():
        # A run creates it only below a writable directory. Otherwise the engine's
        # check fails, creating nothing, also when no ancestor exists.
        existing = next((parent for parent in Path(configured).absolute().parents
                         if parent.exists()), None)
        if existing is not None and existing.is_dir() and os.access(existing, os.W_OK):
            return "missing"
    try:
        check_local_session_dir(configured)
    except SessionFolderRefused as refused:
        return str(refused)
    return None


def workers_refusal(config: Mapping[str, Any]) -> Optional[str]:
    """Why the configured distributor cannot run the configured workers
    (local with more than one), or None."""
    workers = int(config.get("engine_workers") or 1)
    if cosmic_ray_distributor(config.get("cosmic_ray_distributor"), workers) is None:
        return distributor_refusal(workers)
    return None


class SourceBackup:
    def __init__(self, target: Path, *, project_root: Path):
        self.target = target
        self.path = target.absolute()
        # Where the target lies unless a link inside the project leads
        # elsewhere: below the root's physical path, since a link above the
        # root (macOS's /var -> /private/var) aliases nothing in it.
        root = project_root.absolute()
        self._physical = (project_root.resolve() / self.path.relative_to(root)
                          if self.path.is_relative_to(root) else self.path)
        self._check_target()
        self.original = target.read_bytes()
        if Path(tempfile.gettempdir()).resolve().is_relative_to(project_root.resolve()):
            raise OwnershipRefused("Mutation source backup requires a temporary directory outside the project")
        self.owner = OwnedDirectory.create(prefix="langadapter-mutation-source-")
        try:
            for name, data in (
                ("original.bin", self.original),
                ("target.json", json.dumps({"target": str(target.absolute())},
                                          sort_keys=True).encode("utf-8")),
            ):
                with (self.owner.root / name).open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
        except BaseException as error:
            self.retain(error)
            raise

    def restore(self) -> None:
        """Restore the original bytes. The caller must first prove that the
        command's owned writers stopped."""
        if not self.matches():
            self.target.write_bytes(self.original)
            if self.target.read_bytes() != self.original:
                raise OSError("Mutation source restoration did not restore the original bytes")

    def matches(self) -> bool:
        self.owner.verify()
        self._check_target()
        return self.target.read_bytes() == self.original

    def _check_target(self) -> None:
        identity = self.target.lstat()
        if (not stat.S_ISREG(identity.st_mode) or identity.st_nlink != 1
                or self.target.resolve(strict=True) != self._physical):
            raise OwnershipRefused("Mutation target is not an unaliased regular source file")

    def retain(self, error: BaseException) -> None:
        error._langadapter_source_backup = self.owner
        if hasattr(error, "add_note"):
            error.add_note(f"Original mutation source retained at {self.owner.root}; "
                           f"creation receipt: {self.owner.receipt}")

    def remove(self) -> None:
        self.owner.remove()


def read_as_cosmic_ray(path: Path) -> str:
    """A module's source as Cosmic Ray reads it (``tokenize.open``: the
    declared encoding, a byte order mark dropped, universal newlines): the
    text its mutation diffs apply to. Raises ``OSError``, ``SyntaxError``
    (an unknown declared encoding) or ``ValueError`` (undecodable bytes)."""
    import tokenize

    with tokenize.open(path) as handle:
        return handle.read()


def apply_unified_diff(text: str, diff: str) -> Optional[str]:
    """``text`` with a Cosmic Ray unified diff applied (a banner, ``---``/``+++``
    headers, ``@@ -start,count +start,count @@`` hunks), or None when a hunk
    does not match. Cosmic Ray splits the source at newline characters only, so
    ``text`` is split the same way, a final newline giving an empty last element
    a mutant near the end has as context. Inside a hunk every line is body (a
    blank line's context is one space or empty). A diff's final empty line is
    dropped."""
    lines = text.split("\n")
    out: List[str] = []
    pos = 0  # index into lines
    in_hunk = False
    body = diff.split("\n")
    if body and body[-1] == "":
        body.pop()
    for raw in body:
        if raw.startswith("@@"):
            try:
                old = raw.split()[1]           # -start[,count]
                start = int(old.lstrip("-").split(",")[0])
            except (IndexError, ValueError):
                return None
            # hunk start is 1-based. 0 means an insertion at the top
            target = max(start - 1, 0)
            if target < pos or target > len(lines):
                return None
            out.extend(lines[pos:target])
            pos = target
            in_hunk = True
            continue
        if not in_hunk:
            # banner, ---/+++ headers, blank separators before a hunk
            continue
        if raw.startswith("\\"):
            # "\ No newline at end of file"
            continue
        if raw.startswith("+"):
            out.append(raw[1:])
        elif raw.startswith("-"):
            if pos >= len(lines) or lines[pos] != raw[1:]:
                return None
            pos += 1
        else:
            # Context " <text>". A blank source line arrives as " " or "".
            ctx = raw[1:] if raw.startswith(" ") else raw
            if pos >= len(lines) or lines[pos] != ctx:
                return None
            out.append(lines[pos])
            pos += 1
    out.extend(lines[pos:])
    return "\n".join(out)


def parse_journal(text: str) -> List[Dict]:
    """The journal's pytest run blocks, each bound to its module's SHA-1.
    Malformed lines are skipped, since a killed writer can leave a torn last
    line. A completed block keeps ``exit_status``, so a caller can tell pytest's
    success (0) and test failure (1) from its other exits. Timeout attribution
    uses only unended blocks. ``hung`` is the last started test that did not
    end: at its teardown report where the session's first line says so, else
    (the journal of an instrument a suite carries) at any report written."""
    blocks: List[Dict] = []
    cur: Optional[Dict] = None
    for raw in text.splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        if "run" in ev:
            cur = {"module_sha": ev.get("module_sha", "unknown"),
                   "failed": [], "started": [], "finished": set(),
                   "teardown": ev.get("ends") == "teardown",
                   "ended": False, "exit_status": None}
            blocks.append(cur)
        elif cur is None:
            continue
        elif "start" in ev:
            cur["started"].append(ev["start"])
        elif "done" in ev:
            if not cur["teardown"] or ev.get("when") == "teardown":
                cur["finished"].add(ev["done"])
            if ev.get("outcome") == "failed":
                cur["failed"].append(ev["done"])
        elif "end" in ev:
            cur["ended"] = True
            cur["exit_status"] = ev.get("exit")
    for b in blocks:
        hung = [t for t in b["started"] if t not in b["finished"]]
        b["hung"] = hung[-1] if hung else None
        del b["started"], b["finished"], b["teardown"]
    return blocks


#: The directory of an earlier execution's journals (``x0001``, ``x0002`` and on).
_EXECUTION = re.compile(r"x\d{4}")


def keep_earlier_executions(evidence_dir: Path) -> None:
    """Move the run directories in ``evidence_dir`` (a run's ``r<pid>``, a container
    worker's clone name) into the next numbered directory (``x0001``, ``x0002``
    and on). A run directory's name can recur in a later execution, so each
    execution writes fresh directories and ``load_all_blocks`` reads them all in
    order. The container launcher does the same."""
    if not evidence_dir.is_dir():
        return
    sub_dirs = [p for p in evidence_dir.iterdir() if p.is_dir()]
    runs = [p for p in sub_dirs if not _EXECUTION.fullmatch(p.name)]
    if not runs:
        return
    archive = evidence_dir / f"x{len(sub_dirs) - len(runs) + 1:04d}"
    archive.mkdir()
    for run in runs:
        run.rename(archive / run.name)


def next_execution(evidence_dir: Path, baseline_module: Optional[str], *,
                   keep: bool) -> None:
    """Prepare ``evidence_dir`` for the session's next execution: keep the
    journals so far with ``keep``, else (a first execution) remove them, and
    write the baseline module beside the runs."""
    import shutil

    if keep:
        keep_earlier_executions(evidence_dir)
    else:
        shutil.rmtree(evidence_dir, ignore_errors=True)
    if baseline_module is None:
        return
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "baseline_module.txt").write_text(baseline_module, encoding="utf-8")


def load_all_blocks(evidence_dir: Path) -> Dict[str, List[Dict]]:
    """Every journal block with a module hash, by mutated-source SHA-1, in
    execution order (``x0001``, ``x0002`` and on, then the last execution), so
    a mutant's last run, which gave its verdict, comes last."""
    by_sha: Dict[str, List[Dict]] = {}
    journals = (sorted(evidence_dir.glob("x[0-9][0-9][0-9][0-9]/*/journal.jsonl"))
                + sorted(evidence_dir.glob("*/journal.jsonl")))
    for journal in journals:
        try:
            text = journal.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for block in parse_journal(text):
            if block["module_sha"] != "unknown":
                by_sha.setdefault(block["module_sha"], []).append(block)
    return by_sha


def load_blocks(evidence_dir: Path) -> Dict[str, List[Dict]]:
    """The unended blocks of ``load_all_blocks``: an ended block is a completed
    run and never explains a timeout verdict."""
    return {
        sha: [block for block in blocks if not block["ended"]]
        for sha, blocks in load_all_blocks(evidence_dir).items()
        if any(not block["ended"] for block in blocks)
    }


def match_unended_blocks(
    mutants: List[dict],
    evidence_dir: Path,
    baseline_text: str,
) -> List[Tuple[dict, Dict]]:
    """Mutants paired with their unended journal blocks, by the SHA-1 of the
    source rebuilt from each mutant's Cosmic Ray ``diff``, the value the journal
    header recorded. A block pairs at most once. The caller decides which
    journal events suffice for its verdict."""
    by_sha = load_blocks(evidence_dir)
    if not by_sha:
        return []
    matched: List[Tuple[dict, Dict]] = []
    for mutant in mutants:
        diff = mutant.get("diff")
        if not diff:
            continue
        mutated = apply_unified_diff(baseline_text, diff)
        if mutated is None:
            continue
        sha = hashlib.sha1(mutated.encode("utf-8")).hexdigest()
        pool = by_sha.get(sha)
        if pool:
            matched.append((mutant, pool.pop()))
    return matched


def attach_crash_observations(
    mutants: List[dict],
    evidence_dir: Path,
    baseline_text: str,
    log: Optional[Callable[[str], None]] = None,
) -> int:
    """Record as ``active_at_end``, on each kill without a named killer whose
    run ended in the middle of a test, the test running when the process ended,
    and return how many rows gained it. It is an observation, not a proven
    killer: it shows neither that this test alone ends the process nor that no
    other test would. The kill's status and killers stay as recorded."""
    candidates = [m for m in mutants
                  if m.get("status") == KILLED_STATUS
                  and not (m.get("killers") or m.get("killer"))]
    observed = 0
    for m, block in match_unended_blocks(
        candidates, evidence_dir, baseline_text,
    ):
        if not block["hung"]:
            continue
        m["active_at_end"] = block["hung"]
        observed += 1
        if log:
            log(f"[crash-evidence] {m.get('operator')}@{m.get('line')}: "
                f"{block['hung']} was running when the process ended")
    return observed


def attach_timeout_evidence(
    mutants: List[dict],
    evidence_dir: Path,
    baseline_text: str,
    log: Optional[Callable[[str], None]] = None,
) -> int:
    """Stamp ``timeout_evidence`` onto every ``timeout`` mutant whose
    journal block is found, and return how many rows were stamped:
    ``named_killer`` when tests failed before the clock fired (they become
    ``killers`` and ``killer``, the status ``killed``, the original kept in
    ``pre_attribution_status``), ``hang`` when one test started and never
    finished (it becomes the killer, the status stays), else ``cumulative``
    (every started test finished: the summed budget ran out, unattributed)."""
    stamped = 0
    candidates = [m for m in mutants if m.get("status") == TIMEOUT_STATUS]
    for m, block in match_unended_blocks(
        candidates, evidence_dir, baseline_text,
    ):
        if block["failed"]:
            m["pre_attribution_status"] = m["status"]
            m["status"] = KILLED_STATUS
            m["killers"] = list(dict.fromkeys(block["failed"]))
            m["killer"] = m["killers"][0]
            m["timeout_evidence"] = NAMED_KILLER
        elif block["hung"]:
            m["killers"] = [block["hung"]]
            m["killer"] = block["hung"]
            m["timeout_evidence"] = HANG
        else:
            m["timeout_evidence"] = CUMULATIVE
        stamped += 1
        if log:
            log(f"[timeout-evidence] {m.get('operator')}@{m.get('line')}: "
                f"{m['timeout_evidence']}"
                + (f" -> {m.get('killer')}" if m.get("killer") else ""))
    return stamped
