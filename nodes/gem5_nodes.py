"""Node 3 and Node 6: the microarchitecture half of the loop.

* **Node 3 (microarch agent)** applies the Prefetch Hint Queue to a gem5
  checkout, rebuilds, and runs the **correctness gate** -- atomic-mode
  architectural equivalence plus the structural assertions that prove the hint
  never touched the issue queue, the LSQ, or a functional-unit port.
* **Node 6 (slow validation)** runs the O3 timing model, which is the only
  place in this project where the pipeline claims can actually be measured.

CHIA already ships ``Gem5Node`` with build / run / source-state primitives, so
this module adds only what is specific to HINT.GATHER: the patcher node, the
gate node, the genome-to-CLI translation, and stat extraction.  Everything must
be co-located with the gem5 checkout, hence the ``pinned()`` helper.

See ``docs/DESIGN.md`` sections 1.3, 2 and 4.2 for the contracts, especially
the exact stat names -- they are parsed here by name.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field

from chia.base.ChiaFunction import ChiaFunction

import config
from genome import Genome


# --------------------------------------------------------------------------
# Stats contract (docs/DESIGN.md sec 4.2)
# --------------------------------------------------------------------------
# Logical name -> ordered candidate gem5 stat names.  ``Gem5Node.run_gem5``
# tries each candidate in order and keeps the first that exists, which makes
# this robust to the agent renaming the SimObject instance.

PHQ_STATS_KEYS: dict[str, list[str]] = {
    "cycles": ["system.cpu.numCycles", "board.processor.cores.core.numCycles"],
    "insts": ["simInsts", "system.cpu.commit.committedInsts",
              "system.cpu.committedInsts"],
    "hints_dispatched": ["system.cpu.phq.hintsDispatched"],
    "hints_dropped": ["system.cpu.phq.hintsDropped"],
    "hints_dropped_mshr": ["system.cpu.phq.hintsDroppedMshr"],
    "hints_dropped_timeout": ["system.cpu.phq.hintsDroppedTimeout"],
    "hints_dropped_squash": ["system.cpu.phq.hintsDroppedSquash"],
    "hints_dropped_full": ["system.cpu.phq.hintsDroppedFull"],
    "prefetches_issued": ["system.cpu.phq.prefetchesIssued"],
    "prefetches_late": ["system.cpu.phq.prefetchesLate"],
    "phq_occupancy": ["system.cpu.phq.occupancyAvg"],
    # The three structural invariants.  Non-zero means the implementation is
    # not doing what the paper claims, and the candidate must be rejected.
    "iq_entries": ["system.cpu.phq.iqEntriesAllocated"],
    "lsq_entries": ["system.cpu.phq.lsqEntriesAllocated"],
    "fu_port_cycles": ["system.cpu.phq.fuPortCycles"],
    # Conventional counters used for the write-up.
    "l1d_misses": ["system.cpu.dcache.demandMisses::total",
                   "system.cpu.dcache.overallMisses::total"],
    "l1d_accesses": ["system.cpu.dcache.demandAccesses::total",
                     "system.cpu.dcache.overallAccesses::total"],
    "l2_misses": ["system.l2cache.overallMisses::total",
                  "system.cpu.l2cache.overallMisses::total"],
    "iq_full_events": ["system.cpu.iq.fuBusy", "system.cpu.iew.iqFullEvents"],
    "lsq_full_events": ["system.cpu.iew.lsqFullEvents"],
    "rob_full_events": ["system.cpu.rob.robFullEvents",
                        "system.cpu.iew.robFullEvents"],
}

STRUCTURAL_INVARIANTS = ("iq_entries", "lsq_entries", "fu_port_cycles")


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

@dataclass
class PatchResult:
    """Outcome of ``gem5/apply_phq.py``."""
    success: bool
    action: str                 # apply | revert | check
    state: str                  # applied | not_applied | partial | unknown
    returncode: int
    stdout_tail: str
    diagnostics: str
    files_touched: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    """Outcome of the architectural-equivalence + structural gate."""
    passed: bool
    reason: str
    checksum_base: str = ""
    checksum_hint: str = ""
    hints_dispatched: float = 0.0
    invariant_values: dict[str, float] = field(default_factory=dict)
    stdout_tail: str = ""
    wall_s: float = 0.0

    @property
    def failure_kind(self) -> str:
        """Coarse classification used to pick the right repair prompt."""
        if self.passed:
            return "none"
        r = self.reason.lower()
        if "checksum" in r or "mismatch" in r:
            return "architectural_divergence"
        if "invariant" in r or "iqentries" in r or "lsqentries" in r:
            return "structural_violation"
        if "no hints" in r or "hintsdispatched" in r:
            return "no_hints_executed"
        if "timeout" in r:
            return "timeout"
        return "unknown"


@dataclass
class O3Result:
    """One gem5 O3 timing run."""
    benchmark: str
    build_variant: str
    ok: bool
    ipc: float | None
    cycles: int | None
    insts: int | None
    stats: dict[str, float] = field(default_factory=dict)
    status: str = ""
    error: str = ""
    wall_s: float | None = None

    @property
    def hints_per_1k_insts(self) -> float:
        if not self.insts:
            return 0.0
        return 1000.0 * self.stats.get("hints_dispatched", 0.0) / self.insts

    @property
    def l1d_mpki(self) -> float | None:
        if not self.insts:
            return None
        return 1000.0 * self.stats.get("l1d_misses", 0.0) / self.insts

    @property
    def wasted_prefetch_rate(self) -> float:
        issued = self.stats.get("prefetches_issued", 0.0)
        if issued <= 0:
            return 0.0
        late = self.stats.get("prefetches_late", 0.0)
        return late / issued


# --------------------------------------------------------------------------
# Placement helper
# --------------------------------------------------------------------------

def pinned(fn, gem5_node):
    """Pin one of *our* ChiaFunctions to the same bundle as ``gem5_node``.

    gem5 lives on the worker's filesystem: patching, building and running must
    all land on the same machine.  ``Gem5Node`` exposes its scheduling options
    for exactly this purpose.
    """
    if config.LOCAL_MODE:
        return fn
    opts = getattr(gem5_node, "task_options", {}) or {}
    return fn.options(**opts) if opts else fn


# --------------------------------------------------------------------------
# Worker-side helper
# --------------------------------------------------------------------------

def _run(cmd, cwd, timeout_s, env=None):
    start = time.time()
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, shell=isinstance(cmd, str), capture_output=True,
            text=True, timeout=timeout_s, env=env)
        return proc.returncode, proc.stdout, proc.stderr, False, time.time() - start
    except subprocess.TimeoutExpired as e:
        return -9, e.stdout or "", e.stderr or "", True, time.time() - start
    except OSError as e:
        return -1, "", f"failed to spawn {cmd!r}: {e}", False, time.time() - start


# --------------------------------------------------------------------------
# Node 3a: apply the PHQ to a gem5 checkout
# --------------------------------------------------------------------------

@ChiaFunction(resources=config.RES_GEM5)
def apply_phq_patch(gem5_root: str, hg_gem5_dir: str, *, revert: bool = False,
                    check: bool = False, timeout_s: int = 600) -> PatchResult:
    """Run ``gem5/apply_phq.py`` against a gem5 checkout.

    The patcher is deliberately idempotent and marker-delimited so that (a)
    re-running costs nothing, (b) it can be reverted exactly, and (c) the repair
    agent can be pointed at a single delimited region rather than being let
    loose on the whole O3 model.
    """
    script = os.path.join(hg_gem5_dir, "apply_phq.py")
    if not os.path.exists(script):
        return PatchResult(False, "apply", "unknown", -1, "",
                           f"patcher not found at {script}")

    action = "revert" if revert else ("check" if check else "apply")
    cmd = ["python3", script, "--gem5-root", gem5_root]
    if revert:
        cmd.append("--revert")
    if check:
        cmd.append("--check")

    rc, out, err, timed_out, _ = _run(cmd, cwd=hg_gem5_dir, timeout_s=timeout_s)

    state = "unknown"
    for token in ("applied", "not_applied", "partial"):
        if re.search(rf"\bSTATE:\s*{token}\b", out):
            state = token
            break

    touched = re.findall(r"^\s*(?:patched|copied|reverted):\s*(\S+)", out, re.M)

    return PatchResult(
        success=(rc == 0 and not timed_out),
        action=action,
        state=state,
        returncode=rc,
        stdout_tail=out[-3000:],
        diagnostics=("TIMEOUT applying PHQ patch" if timed_out
                     else "" if rc == 0 else (err or out)[-4000:]),
        files_touched=touched,
    )


# --------------------------------------------------------------------------
# Node 3b: the correctness gate
# --------------------------------------------------------------------------

@ChiaFunction(resources=config.RES_GEM5)
def run_correctness_gate(gem5_bin: str, config_script: str, gate_script: str,
                         elf_hint: str, elf_base: str, outdir: str,
                         genome_json: str, *, max_insts: int = 0,
                         timeout_s: int = 3600) -> GateResult:
    """Prove the hint changed nothing architecturally, and cost nothing
    structurally.

    Two questions, one gate:

    1. *Semantics*: run both ELFs under AtomicSimpleCPU and compare the
       self-check ``CHECKSUM=`` line.  A ``HINT.GATHER`` is by definition a NOP
       as far as architectural state is concerned (DESIGN.md sec 1.3), so any
       divergence is a hard failure.
    2. *Structure*: assert ``iqEntriesAllocated == lsqEntriesAllocated ==
       fuPortCycles == 0`` and ``hintsDispatched > 0``.  Without this, an agent
       can "pass" by quietly implementing the hint as an ordinary prefetch --
       which would be correct, fast, and completely beside the point.
    """
    os.makedirs(outdir, exist_ok=True)
    genome_path = os.path.join(outdir, "genome.json")
    with open(genome_path, "w") as f:
        f.write(genome_json)

    cmd = [
        "python3", gate_script,
        "--gem5-bin", gem5_bin,
        "--config", config_script,
        "--elf-hint", elf_hint,
        "--elf-base", elf_base,
        "--outdir", outdir,
        "--genome", genome_path,
        "--options", "--size 8192 --iters 2",
    ]
    if max_insts and max_insts > 50_000_000:
        cmd += ["--max-insts", str(max_insts)]

    rc, out, err, timed_out, wall = _run(cmd, cwd=None, timeout_s=timeout_s)

    if timed_out:
        return GateResult(False, f"timeout after {wall:.0f}s",
                          stdout_tail=(out + err)[-3000:], wall_s=wall)

    # The gate script's contract: last line is `GATE: PASS` or `GATE: FAIL <why>`.
    verdict_line = ""
    for line in reversed(out.strip().splitlines()):
        if line.startswith("GATE:"):
            verdict_line = line.strip()
            break

    passed = verdict_line.startswith("GATE: PASS")
    reason = "" if passed else (
        verdict_line[len("GATE: FAIL"):].strip() if verdict_line
        else f"gate script produced no verdict (rc={rc})")

    def _grab(pattern: str, default: str = "") -> str:
        m = re.search(pattern, out)
        return m.group(1) if m else default

    invariants: dict[str, float] = {}
    for name in STRUCTURAL_INVARIANTS:
        raw = _grab(rf"{name}\s*[=:]\s*([0-9.eE+\-]+)")
        if raw:
            try:
                invariants[name] = float(raw)
            except ValueError:
                pass

    try:
        hints = float(_grab(r"hints_dispatched\s*[=:]\s*([0-9.eE+\-]+)", "0"))
    except ValueError:
        hints = 0.0

    return GateResult(
        passed=passed,
        reason=reason,
        checksum_base=_grab(r"checksum_base\s*[=:]\s*(\S+)"),
        checksum_hint=_grab(r"checksum_hint\s*[=:]\s*(\S+)"),
        hints_dispatched=hints,
        invariant_values=invariants,
        stdout_tail=(out + "\n" + err)[-3000:],
        wall_s=wall,
    )


# --------------------------------------------------------------------------
# Genome -> gem5 CLI  (pure helpers; no cluster resource needed)
# --------------------------------------------------------------------------

def o3_config_args(genome: Genome, elf_path: str, *, cpu_type: str = "o3",
                   max_insts: int | None = None, disable_phq: bool = False,
                   l1d_prefetcher: str = "none",
                   bench_args: str = "--size 8192 --iters 8") -> list[str]:
    """Translate a genome into arguments for ``gem5/configs/hint_gather_se.py``.

    Mirrors the flag list in DESIGN.md sec 4.2 exactly.
    """
    args = [
        "--binary", elf_path,
        "--cpu-type", cpu_type,
        "--max-insts", str(max_insts if max_insts is not None else 0),
        "--l1d-mshrs", "32",
        "--phq-entries", str(genome.phq_entries),
        "--phq-dispatch-width", str(genome.phq_dispatch_width),
        "--phq-poll-limit", str(genome.phq_poll_limit),
        "--wakeup-policy", genome.wakeup_policy,
        "--tlb-miss-policy", genome.tlb_miss_policy,
        "--prefetch-level", genome.prefetch_level,
        "--mshr-pressure-threshold", str(genome.mshr_pressure_threshold),
        "--l1d-prefetcher", l1d_prefetcher,
    ]
    if disable_phq:
        args.append("--disable-phq")
    if bench_args:
        args += ["--options", bench_args]
    return args


def summarize_o3(result, benchmark: str, build_variant: str) -> O3Result:
    """Turn CHIA's ``Gem5RunResult`` into our narrower, comparable record."""
    ok = getattr(result, "status", "") == "ok"
    cycles = getattr(result, "num_cycles", None)
    insts = getattr(result, "sim_insts", None)
    ipc = (insts / cycles) if (ok and cycles and insts) else None
    return O3Result(
        benchmark=benchmark,
        build_variant=build_variant,
        ok=ok,
        ipc=ipc,
        cycles=cycles,
        insts=insts,
        stats=dict(getattr(result, "stats", {}) or {}),
        status=getattr(result, "status", ""),
        error=getattr(result, "error_messages", ""),
        wall_s=getattr(result, "wall_s", None),
    )


def check_structural_invariants(res: O3Result) -> tuple[bool, str]:
    """Re-assert the zero-occupancy invariants on every O3 run, not just the gate.

    The gate runs once per candidate; an O3 run can still surface a violation
    that only manifests under speculation (e.g. a squashed hint that leaked an
    LSQ entry).  Cheap to check, embarrassing to miss.
    """
    for name in STRUCTURAL_INVARIANTS:
        value = res.stats.get(name)
        if value is None:
            return False, f"{name} missing from stats.txt (is the PHQ built in?)"
        if value > 0:
            return False, (f"structural invariant violated: {name}={value:g} "
                           f"(must be 0 -- the hint must never occupy an IQ/LSQ "
                           f"entry or an FU port)")
    return True, ""


def o3_speedup(hint: O3Result, baseline: O3Result) -> float | None:
    """Wall-clock cycle speedup (baseline.cycles / hint.cycles).

    Because HINT.GATHER with fanout > 1 reduces dynamic instruction count
    (simInsts) as well as total cycles, IPC (insts/cycles) understates speedup.
    Cycle ratio is the true execution-time speedup per the Iron Law.
    """
    if not (hint.ok and baseline.ok):
        return None
    if hint.cycles and baseline.cycles and hint.cycles > 0:
        return baseline.cycles / hint.cycles
    if not hint.ipc or not baseline.ipc:
        return None
    return hint.ipc / baseline.ipc
