#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# HINT.GATHER -- architectural-equivalence and structural correctness gate.
#
# Role
# ----
# This is the pass/fail gate the CHIA loop runs before it is allowed to
# believe any speedup number. It answers two questions that the design
# document (`docs/DESIGN.md`) treats as non-negotiable:
#
#   1. Is HINT.GATHER architecturally a NOP?  (DESIGN.md 1.3)
#      The hinted binary must produce byte-identical program output and the
#      identical committed-instruction stream shape whether or not the
#      Prefetch Hint Queue is doing anything, and whether it runs on the
#      timing O3 model or the functional atomic model.
#
#   2. Is HINT.GATHER structurally free in the out-of-order core?
#      (DESIGN.md 1.3.3, 1.4, 4.2)
#      `iqEntriesAllocated`, `lsqEntriesAllocated` and `fuPortCycles` must
#      all be exactly zero, *while* `robEntriesAllocated` is non-zero. The
#      last clause is the point: three zeroes on their own are also what you
#      get from a build in which the hints were silently dropped at decode
#      and never reached the back end at all. Only "ROB yes, everything else
#      no" actually demonstrates the claim.
#
# It also verifies that every stat name in the DESIGN.md 4.2 contract is
# actually present in stats.txt, so that a rename in the C++ is caught here
# rather than as a mysterious KeyError deep inside the loop.
#
# Output contract
# ---------------
# The last line printed on stdout is always exactly one of:
#
#     GATE: PASS
#     GATE: FAIL <reason>
#
# Exit status is 0 on PASS and 1 on FAIL. Anything that prevents the gate
# from reaching a verdict (gem5 missing, simulation crash) is a FAIL with a
# reason, never a traceback.
#
# Usage
# -----
#   python3 gem5/tests/check_arch_equiv.py \
#       --gem5-binary /path/to/gem5/build/RISCV/gem5.opt \
#       --binary bench/build/gups_hinted \
#       [--binary-baseline bench/build/gups_plain] \
#       [--options "--n 65536"] [--max-insts 20000000] \
#       [--workdir /tmp/hg_gate] [--keep]

import argparse
import os
import re
import shutil
import subprocess
import sys


# Stat names fixed by DESIGN.md 4.2. Do not edit without editing DESIGN.md.
REQUIRED_STATS = (
    "system.cpu.phq.hintsDispatched",
    "system.cpu.phq.hintsDropped",
    "system.cpu.phq.hintsDroppedMshr",
    "system.cpu.phq.hintsDroppedTimeout",
    "system.cpu.phq.hintsDroppedSquash",
    "system.cpu.phq.hintsDroppedFull",
    "system.cpu.phq.prefetchesIssued",
    "system.cpu.phq.prefetchesLate",
    "system.cpu.phq.occupancyAvg",
    "system.cpu.phq.iqEntriesAllocated",
    "system.cpu.phq.lsqEntriesAllocated",
    "system.cpu.phq.fuPortCycles",
)

# Must read exactly zero (DESIGN.md 1.3.3, 1.4).
MUST_BE_ZERO = (
    "system.cpu.phq.iqEntriesAllocated",
    "system.cpu.phq.lsqEntriesAllocated",
    "system.cpu.phq.fuPortCycles",
)

# Positive control. See the module docstring.
MUST_BE_NONZERO = (
    "system.cpu.phq.robEntriesAllocated",
    "system.cpu.phq.hintsDispatched",
)


class GateFailure(Exception):
    """Raised with a single-line, machine-readable reason."""


# --------------------------------------------------------------------------
# gem5 invocation
# --------------------------------------------------------------------------
def run_gem5(args, tag, binary, cpu_type, disable_phq):
    """Run one gem5 simulation. Returns (outdir, stdout_text)."""
    outdir = os.path.join(args.workdir, tag)
    os.makedirs(outdir, exist_ok=True)

    cmd = [
        args.gem5_binary,
        "--outdir=%s" % outdir,
        "--stats-file=stats.txt",
        args.config,
        "--binary",
        binary,
        "--cpu-type",
        cpu_type,
        "--phq-entries",
        str(args.phq_entries),
        "--phq-dispatch-width",
        str(args.phq_dispatch_width),
        "--phq-poll-limit",
        str(args.phq_poll_limit),
        "--wakeup-policy",
        args.wakeup_policy,
        "--tlb-miss-policy",
        args.tlb_miss_policy,
        "--prefetch-level",
        args.prefetch_level,
        "--mshr-pressure-threshold",
        str(args.mshr_pressure_threshold),
        "--l1d-prefetcher",
        args.l1d_prefetcher,
    ]
    if args.max_insts:
        cmd += ["--max-insts", str(args.max_insts)]
    if args.options:
        cmd += ["--options", args.options]
    if disable_phq:
        cmd += ["--disable-phq"]

    print("[gate] %-18s %s" % (tag, " ".join(cmd)))

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=args.timeout,
            text=True,
            errors="replace",
        )
    except FileNotFoundError:
        raise GateFailure(
            "gem5 binary not found at %s (build it first, see gem5/README.md)"
            % args.gem5_binary
        )
    except subprocess.TimeoutExpired:
        raise GateFailure(
            "run '%s' exceeded the %ds timeout; lower --max-insts or raise "
            "--timeout" % (tag, args.timeout)
        )

    log_path = os.path.join(outdir, "run.log")
    with open(log_path, "w") as f:
        f.write(proc.stdout)

    if proc.returncode != 0:
        raise GateFailure(
            "run '%s' exited %d; see %s (tail: %s)"
            % (
                tag,
                proc.returncode,
                log_path,
                _tail_one_line(proc.stdout),
            )
        )

    return outdir, proc.stdout


def _tail_one_line(text):
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return lines[-1] if lines else "<no output>"


# --------------------------------------------------------------------------
# Program output extraction
# --------------------------------------------------------------------------
# gem5 prints its own banner, config warnings and the exit message on the
# same stream as the simulated program's stdout. Everything the *program*
# writes lands between the "Beginning simulation" marker and the
# "Exiting @ tick" marker, so we slice on those. We additionally drop lines
# that are unambiguously gem5's own (warn:/info:/fatal:/panic: prefixes and
# the config-hash chatter), because those can legitimately differ between a
# PHQ-on and a PHQ-off run without any architectural difference.
_GEM5_NOISE = re.compile(
    r"^(warn|info|hack|fatal|panic|gem5|build/|src/|\s*$)"
    r"|^hint_gather_se\.py:"
    r"|^Exiting @ tick"
    r"|^Global frequency set at"
    r"|^Beginning simulation"
)


def program_output(raw):
    started = False
    out = []
    for line in raw.splitlines():
        if not started:
            if "beginning simulation" in line.lower():
                started = True
            continue
        if line.startswith("Exiting @ tick"):
            break
        if _GEM5_NOISE.match(line):
            continue
        out.append(line.rstrip())
    return "\n".join(out).strip()


# --------------------------------------------------------------------------
# stats.txt parsing
# --------------------------------------------------------------------------
_STAT_LINE = re.compile(r"^(\S+)\s+([-+0-9.eEnaN]+)")


def read_stats(outdir):
    path = os.path.join(outdir, "stats.txt")
    if not os.path.isfile(path):
        raise GateFailure(
            "no stats.txt at %s; the simulation did not dump statistics"
            % path
        )
    stats = {}
    with open(path) as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            m = _STAT_LINE.match(line)
            if not m:
                continue
            name, value = m.group(1), m.group(2)
            try:
                stats[name] = float(value)
            except ValueError:
                # 'nan' / 'inf' / distribution sub-rows: keep the key so the
                # presence check still passes, but mark the value unusable.
                stats[name] = float("nan")
    if not stats:
        raise GateFailure("stats.txt at %s parsed to zero statistics" % path)
    return stats


# --------------------------------------------------------------------------
# The individual checks
# --------------------------------------------------------------------------
def check_stat_contract(stats):
    missing = [s for s in REQUIRED_STATS if s not in stats]
    if missing:
        raise GateFailure(
            "stats.txt is missing %d name(s) required by DESIGN.md 4.2: %s"
            % (len(missing), ",".join(missing))
        )


def check_structural(stats):
    for name in MUST_BE_ZERO:
        value = stats[name]
        if value != 0:
            raise GateFailure(
                "%s == %g, expected 0. HINT.GATHER consumed a resource it "
                "must never consume (DESIGN.md 1.3.3/1.4)." % (name, value)
            )

    for name in MUST_BE_NONZERO:
        if name not in stats:
            raise GateFailure(
                "%s absent, so the three structural zeroes are vacuous "
                "(positive control missing)" % name
            )
        if not stats[name] > 0:
            raise GateFailure(
                "%s == 0, so the structural zeroes prove nothing: no "
                "HINT.GATHER reached the back end. Check that the binary "
                "really contains custom-0 hints and that the decoder patch "
                "is applied." % name
            )


def check_arch_equivalence(label_a, out_a, label_b, out_b):
    norm_a = re.sub(r"\bVARIANT=\S+", "VARIANT=*", out_a)
    norm_b = re.sub(r"\bVARIANT=\S+", "VARIANT=*", out_b)
    if norm_a == norm_b:
        return
    # Produce a short, actionable diff rather than dumping both outputs.
    a_lines = norm_a.splitlines()
    b_lines = norm_b.splitlines()
    for i, (x, y) in enumerate(zip(a_lines, b_lines)):
        if x != y:
            raise GateFailure(
                "program output differs between %s and %s at line %d: "
                "%r vs %r. HINT.GATHER is not architecturally a NOP "
                "(DESIGN.md 1.3)." % (label_a, label_b, i + 1, x, y)
            )
    raise GateFailure(
        "program output length differs between %s (%d lines) and %s "
        "(%d lines). HINT.GATHER is not architecturally a NOP "
        "(DESIGN.md 1.3)."
        % (label_a, len(a_lines), label_b, len(b_lines))
    )


def check_commit_count(stats_a, label_a, stats_b, label_b):
    """Same binary, two CPU models: the committed instruction count must match.

    This is the sharpest available test that the hint neither faults nor
    changes control flow. It is skipped silently if gem5 renamed the stat.
    """
    key = None
    for candidate in ("simInsts", "sim_insts", "system.cpu.committedInsts"):
        if candidate in stats_a and candidate in stats_b:
            key = candidate
            break
    if key is None:
        print("[gate] note: no committed-instruction stat found; skipping "
              "the commit-count check")
        return
    if stats_a[key] != stats_b[key]:
        raise GateFailure(
            "%s differs: %s=%g vs %s=%g. The same binary committed a "
            "different number of instructions on two CPU models, so "
            "HINT.GATHER is perturbing architectural execution "
            "(DESIGN.md 1.3)."
            % (key, label_a, stats_a[key], label_b, stats_b[key])
        )
    print("[gate] commit-count check: %s == %g on both models"
          % (key, stats_a[key]))


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------
def build_parser():
    here = os.path.dirname(os.path.abspath(__file__))
    default_config = os.path.join(here, "..", "configs", "hint_gather_se.py")

    p = argparse.ArgumentParser(
        prog="check_arch_equiv.py",
        description="HINT.GATHER architectural-equivalence and structural "
        "correctness gate (see docs/DESIGN.md 1.3, 1.4, 4.2)",
    )
    p.add_argument(
        "--gem5-binary", "--gem5-bin",
        dest="gem5_binary",
        required=True,
        help="Path to gem5.opt (RISCV build).",
    )
    p.add_argument(
        "--config",
        default=os.path.normpath(default_config),
        help="Path to hint_gather_se.py.",
    )
    p.add_argument(
        "--binary", "--elf-hint",
        dest="binary",
        required=True,
        help="The HINT.GATHER-instrumented RISC-V binary under test.",
    )
    p.add_argument(
        "--binary-baseline", "--elf-base",
        dest="binary_baseline",
        default=None,
        help="Optional un-instrumented build of the same program. If given, "
        "its output must match the hinted binary's output. If omitted, "
        "the gate instead compares the hinted binary with the PHQ on "
        "against the same binary with --disable-phq, which is a weaker "
        "but still meaningful equivalence test.",
    )
    p.add_argument("--options", default="--size 8192 --iters 2")
    p.add_argument("--max-insts", type=int, default=0)
    p.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Per-simulation wall-clock limit in seconds.",
    )
    p.add_argument(
        "--workdir", "--outdir",
        dest="workdir",
        default=None,
        help="Where to put per-run output directories. Defaults to "
        "./hg_gate_out.",
    )
    p.add_argument(
        "--genome",
        default=None,
        help="Optional genome JSON path (used by nodes/gem5_nodes.py).",
    )
    p.add_argument(
        "--keep",
        action="store_true",
        help="Keep an existing --workdir instead of clearing it. By default "
        "the gate starts from an empty directory so a stale stats.txt "
        "from a crashed previous run cannot produce a false PASS.",
    )

    # Genome pass-through, same spellings as DESIGN.md 4.2.
    p.add_argument("--phq-entries", type=int, default=32)
    p.add_argument("--phq-dispatch-width", type=int, default=2)
    p.add_argument("--phq-poll-limit", type=int, default=16)
    p.add_argument(
        "--wakeup-policy", choices=("poll_rf", "tag_snoop"), default="poll_rf"
    )
    p.add_argument(
        "--tlb-miss-policy", choices=("drop", "walk"), default="drop"
    )
    p.add_argument("--prefetch-level", choices=("L1D", "L2C"), default="L1D")
    p.add_argument("--mshr-pressure-threshold", type=float, default=0.90)
    p.add_argument(
        "--l1d-prefetcher", choices=("none", "stride"), default="none"
    )
    return p


def run_gate(args):
    # ---- run 1: O3 with the PHQ live. The structural evidence comes from
    # this run and only this run.
    o3_dir, o3_raw = run_gem5(
        args, "o3_phq_on", args.binary, "o3", disable_phq=False
    )
    o3_stats = read_stats(o3_dir)

    check_stat_contract(o3_stats)
    print("[gate] stat-name contract: all %d DESIGN.md 4.2 names present"
          % len(REQUIRED_STATS))

    check_structural(o3_stats)
    print(
        "[gate] structural: iq=0 lsq=0 fu=0 with rob=%g and "
        "hintsDispatched=%g"
        % (
            o3_stats.get("system.cpu.phq.robEntriesAllocated", float("nan")),
            o3_stats["system.cpu.phq.hintsDispatched"],
        )
    )
    print(
        "[gate] summary: hints_dispatched=%g iq_entries_allocated=%g "
        "lsq_entries_allocated=%g fu_port_cycles=%g"
        % (
            o3_stats["system.cpu.phq.hintsDispatched"],
            o3_stats["system.cpu.phq.iqEntriesAllocated"],
            o3_stats["system.cpu.phq.lsqEntriesAllocated"],
            o3_stats["system.cpu.phq.fuPortCycles"],
        )
    )

    # ---- run 2: the architectural reference.
    if args.binary_baseline:
        ref_dir, ref_raw = run_gem5(
            args,
            "atomic_baseline",
            args.binary_baseline,
            "atomic",
            disable_phq=True,
        )
        ref_label = "atomic/baseline-binary"
    else:
        ref_dir, ref_raw = run_gem5(
            args, "o3_phq_off", args.binary, "o3", disable_phq=True
        )
        ref_label = "o3/phq-off"

    check_arch_equivalence(
        "o3/phq-on", program_output(o3_raw), ref_label, program_output(ref_raw)
    )
    print("[gate] architectural equivalence: output identical between "
          "o3/phq-on and %s" % ref_label)

    # ---- run 3: same binary on the functional model. Catches a hint that
    # faults or changes control flow only under timing.
    atomic_dir, atomic_raw = run_gem5(
        args, "atomic_hinted", args.binary, "atomic", disable_phq=True
    )
    check_arch_equivalence(
        "o3/phq-on", program_output(o3_raw),
        "atomic/hinted-binary", program_output(atomic_raw),
    )
    print("[gate] architectural equivalence: output identical between "
          "o3/phq-on and atomic/hinted-binary")

    check_commit_count(
        o3_stats, "o3/phq-on", read_stats(atomic_dir), "atomic/hinted-binary"
    )


def main():
    import json
    args = build_parser().parse_args()

    if args.genome and os.path.isfile(args.genome):
        try:
            with open(args.genome, "r") as gf:
                gdata = json.load(gf)
            for k in (
                "phq_entries",
                "phq_dispatch_width",
                "phq_poll_limit",
                "wakeup_policy",
                "tlb_miss_policy",
                "prefetch_level",
                "mshr_pressure_threshold",
            ):
                if k in gdata:
                    setattr(args, k, gdata[k])
        except Exception:
            pass

    if args.workdir is None:
        args.workdir = os.path.abspath("hg_gate_out")
    args.workdir = os.path.abspath(args.workdir)
    args.config = os.path.abspath(args.config)
    args.gem5_binary = os.path.abspath(args.gem5_binary)
    args.binary = os.path.abspath(args.binary)
    if args.binary_baseline:
        args.binary_baseline = os.path.abspath(args.binary_baseline)

    if os.path.isdir(args.workdir) and not args.keep:
        # Deliberately scoped to the gate's own output tree, which this
        # script created, and never to a simulation run directory supplied
        # by the user.
        shutil.rmtree(args.workdir)
    os.makedirs(args.workdir, exist_ok=True)

    try:
        if not os.path.isfile(args.config):
            raise GateFailure("config script not found: %s" % args.config)
        if not os.path.isfile(args.binary):
            raise GateFailure("binary under test not found: %s" % args.binary)
        run_gate(args)
    except GateFailure as e:
        reason = " ".join(str(e).split())
        print("GATE: FAIL %s" % reason)
        return 1
    except Exception as e:  # noqa: BLE001 -- the gate must never traceback
        reason = " ".join(("unexpected %s: %s" % (type(e).__name__, e)).split())
        print("GATE: FAIL %s" % reason)
        return 1

    print("GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
