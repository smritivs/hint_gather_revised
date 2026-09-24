# Copyright 2026 Google LLC
#
# HINT.GATHER -- gem5 SE-mode configuration script.
#
# Role
# ----
# This is the single entry point the CHIA loop uses to run a gem5 experiment
# for the HINT.GATHER project. It builds a RISC-V syscall-emulation system
# with a two-level classic cache hierarchy, an O3 core, and a Prefetch Hint
# Queue (PHQ) attached to the core as `system.cpu.phq`.
#
# The normative spec for everything here is `docs/DESIGN.md`. In particular:
#   * DESIGN.md 4.2 fixes the command-line flag names. They are reproduced
#     verbatim below; DO NOT rename them, the loop constructs the argv.
#   * DESIGN.md 4.2 fixes the required stat names. The PHQ is attached to the
#     Python attribute `phq` of the CPU precisely because gem5 derives stat
#     paths from the SimObject attribute name, which is what yields the
#     required `system.cpu.phq.<stat>` spellings.
#   * DESIGN.md 3 fixes the genome key names and their legal ranges; the
#     flags here are a 1:1 mirror with `_` replaced by `-`.
#
# Style
# -----
# This is deliberately written in the *classic* hand-rolled `m5.objects`
# style rather than the modern `gem5.components` stdlib. Reason: the stdlib's
# cache-hierarchy and processor APIs changed between gem5 v23 and v24
# (`CacheHierarchy`/`BaseCPUProcessor` signatures, `set_se_binary_workload`
# vs `set_workload`), and this project must build against whichever of the two
# the user has checked out. The classic API (`System`, `Cache`, `SystemXBar`,
# `SEWorkload.init_compatible`, `Process`) has been stable since v20.
#
# Usage (flags per DESIGN.md 4.2):
#
#   build/RISCV/gem5.opt --outdir=m5out \
#       gem5/configs/hint_gather_se.py \
#       --binary PATH --max-insts N --cpu-type {atomic,o3} \
#       --phq-entries N --phq-dispatch-width N --phq-poll-limit N \
#       --wakeup-policy {poll_rf,tag_snoop} --tlb-miss-policy {drop,walk} \
#       --prefetch-level {L1D,L2C} --mshr-pressure-threshold F \
#       --l1d-prefetcher {none,stride} --disable-phq \
#       --options "ARGS"

import argparse
import os
import shlex
import sys

import m5
from m5.objects import *
from m5.util import fatal


# --------------------------------------------------------------------------
# Command line -- DESIGN.md 4.2. Names are normative.
# --------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="hint_gather_se.py",
        description="HINT.GATHER RISC-V SE-mode O3 configuration "
        "(see docs/DESIGN.md 4.2)",
    )

    # ---- workload -------------------------------------------------------
    p.add_argument(
        "--binary",
        required=True,
        help="Path to the statically linked RISC-V binary to execute.",
    )
    p.add_argument(
        "--options",
        default="",
        help='Arguments passed to the binary, as one shell-quoted string, '
        'e.g. --options "--n 4096 --iters 10".',
    )
    p.add_argument(
        "--max-insts",
        type=int,
        default=0,
        help="Stop after this many committed instructions on any thread. "
        "0 means run to completion.",
    )

    # ---- core -----------------------------------------------------------
    p.add_argument(
        "--cpu-type",
        choices=("atomic", "o3"),
        default="o3",
        help="'o3' is the timing out-of-order model that the PHQ plugs "
        "into. 'atomic' is the fast functional reference used by "
        "tests/check_arch_equiv.py; it has no PHQ and no caches.",
    )
    p.add_argument("--cpu-clock", default="2GHz")
    p.add_argument("--mem-size", default="2GB")

    # ---- cache hierarchy ------------------------------------------------
    p.add_argument("--l1i-size", default="32kB")
    p.add_argument("--l1d-size", default="32kB")
    p.add_argument("--l2-size", default="1MB")
    p.add_argument("--l1d-mshrs", type=int, default=32)
    p.add_argument("--cacheline-size", type=int, default=64)
    p.add_argument(
        "--l1d-prefetcher",
        choices=("none", "stride"),
        default="none",
        help="Baseline hardware prefetcher on the L1D. The point of the "
        "experiment is usually HINT.GATHER *versus* this.",
    )

    # ---- PHQ genome (DESIGN.md 3) ---------------------------------------
    p.add_argument("--phq-entries", type=int, default=32, help="2..32")
    p.add_argument("--phq-dispatch-width", type=int, default=4, help="1..4")
    p.add_argument("--phq-poll-limit", type=int, default=32, help="1..64")
    p.add_argument(
        "--wakeup-policy", choices=("poll_rf", "tag_snoop"), default="poll_rf"
    )
    p.add_argument(
        "--tlb-miss-policy", choices=("drop", "walk"), default="drop"
    )
    p.add_argument(
        "--prefetch-level", choices=("L1D", "L2C"), default="L1D"
    )
    p.add_argument(
        "--mshr-pressure-threshold", type=float, default=0.85, help="0.0..1.0"
    )
    p.add_argument(
        "--disable-phq",
        action="store_true",
        help="Instantiate the PHQ but hold it inert. The SimObject is "
        "still created so that every `system.cpu.phq.*` stat required "
        "by DESIGN.md 4.2 appears in stats.txt (reading zero). This "
        "keeps the loop's stat parser from having to special-case the "
        "baseline run.",
    )

    # ---- non-genome microarchitectural knobs ----------------------------
    # Not in DESIGN.md 4.2; additive only, with defaults that reproduce the
    # DESIGN.md behaviour exactly. Documented in gem5/README.md.
    p.add_argument("--phq-scoreboard-read-ports", type=int, default=2)
    p.add_argument("--phq-adder-throughput", type=int, default=8)

    return p


# --------------------------------------------------------------------------
# Cache types
# --------------------------------------------------------------------------
class L1ICache(Cache):
    assoc = 8
    tag_latency = 2
    data_latency = 2
    response_latency = 2
    mshrs = 4
    tgts_per_mshr = 8
    is_read_only = True
    writeback_clean = True


class L1DCache(Cache):
    assoc = 8
    tag_latency = 2
    data_latency = 2
    response_latency = 2
    mshrs = 32
    tgts_per_mshr = 8


class L2Cache(Cache):
    assoc = 16
    tag_latency = 8
    data_latency = 8
    response_latency = 4
    mshrs = 32
    tgts_per_mshr = 12
    write_buffers = 8


# --------------------------------------------------------------------------
# System construction
# --------------------------------------------------------------------------
def build_system(args):
    if not os.path.isfile(args.binary):
        fatal("--binary does not exist or is not a file: %s" % args.binary)

    system = System()

    system.clk_domain = SrcClockDomain()
    system.clk_domain.clock = args.cpu_clock
    system.clk_domain.voltage_domain = VoltageDomain()

    system.mem_mode = "atomic" if args.cpu_type == "atomic" else "timing"
    system.mem_ranges = [AddrRange(args.mem_size)]
    system.cache_line_size = args.cacheline_size

    system.membus = SystemXBar()

    if args.cpu_type == "atomic":
        # Functional reference for the architectural-equivalence gate
        # (DESIGN.md 1.3: HINT.GATHER must be a pure NOP architecturally).
        # No caches and no PHQ: the fastest thing that still produces a
        # committed instruction stream and program output.
        system.cpu = RiscvAtomicSimpleCPU()
        system.cpu.icache_port = system.membus.cpu_side_ports
        system.cpu.dcache_port = system.membus.cpu_side_ports
        system.cpu.mmu.connectWalkerPorts(
            system.membus.cpu_side_ports, system.membus.cpu_side_ports
        )
    else:
        system.cpu = RiscvO3CPU()

        system.cpu.icache = L1ICache(size=args.l1i_size)
        system.cpu.dcache = L1DCache(
            size=args.l1d_size, mshrs=args.l1d_mshrs
        )
        if args.l1d_prefetcher == "stride":
            system.cpu.dcache.prefetcher = StridePrefetcher()

        system.cpu.icache.cpu_side = system.cpu.icache_port
        system.cpu.dcache.cpu_side = system.cpu.dcache_port

        system.l2bus = L2XBar()
        system.cpu.icache.mem_side = system.l2bus.cpu_side_ports
        system.cpu.dcache.mem_side = system.l2bus.cpu_side_ports

        # Page-table walker ports go straight at the L2 bus. In SE mode
        # RISC-V uses a bare (no-translation) MMU, so these carry no traffic
        # in practice, but leaving them unconnected is a fatal config error.
        system.cpu.mmu.connectWalkerPorts(
            system.l2bus.cpu_side_ports, system.l2bus.cpu_side_ports
        )

        system.l2cache = L2Cache(size=args.l2_size)
        system.l2cache.cpu_side = system.l2bus.mem_side_ports
        system.l2cache.mem_side = system.membus.cpu_side_ports

        attach_phq(system, args)

    system.cpu.createInterruptController()

    system.system_port = system.membus.cpu_side_ports

    system.mem_ctrl = MemCtrl()
    system.mem_ctrl.dram = DDR3_1600_8x8()
    system.mem_ctrl.dram.range = system.mem_ranges[0]
    system.mem_ctrl.port = system.membus.mem_side_ports

    # ---- workload -------------------------------------------------------
    system.workload = SEWorkload.init_compatible(args.binary)

    process = Process()
    process.cmd = [args.binary] + shlex.split(args.options)
    system.cpu.workload = process
    system.cpu.createThreads()

    if args.max_insts > 0:
        system.cpu.max_insts_any_thread = args.max_insts

    return system


def attach_phq(system, args):
    """Attach the Prefetch Hint Queue.

    The attribute name `phq` is load-bearing: gem5's
    `SimObject._bindStatHierarchy()` builds stat paths from the Python
    attribute a child object is assigned to, so `system.cpu.phq = ...` is
    exactly what produces the `system.cpu.phq.hintsDispatched` spelling that
    DESIGN.md 4.2 mandates. Renaming this attribute silently breaks the
    CHIA loop's stat parser.
    """
    # Range checks here as well as in the C++ constructor. Failing in Python
    # gives the loop a readable error before a multi-minute simulation starts.
    def check(name, value, low, high):
        if not (low <= value <= high):
            fatal(
                "--%s=%s is outside the legal range [%s, %s] fixed by "
                "docs/DESIGN.md 3."
                % (name.replace("_", "-"), value, low, high)
            )

    check("phq_entries", args.phq_entries, 2, 32)
    check("phq_dispatch_width", args.phq_dispatch_width, 1, 4)
    check("phq_poll_limit", args.phq_poll_limit, 1, 64)
    check("mshr_pressure_threshold", args.mshr_pressure_threshold, 0.0, 1.0)

    system.cpu.phq = PrefetchHintQueue(
        enabled=not args.disable_phq,
        phq_entries=args.phq_entries,
        phq_dispatch_width=args.phq_dispatch_width,
        phq_poll_limit=args.phq_poll_limit,
        wakeup_policy=args.wakeup_policy,
        tlb_miss_policy=args.tlb_miss_policy,
        prefetch_level=args.prefetch_level,
        mshr_pressure_threshold=args.mshr_pressure_threshold,
        scoreboard_read_ports=args.phq_scoreboard_read_ports,
        adder_throughput=args.phq_adder_throughput,
        # Derived, never supplied independently: the PHQ's only in-band view
        # of L1D occupancy is a refused sendTimingReq(), so it needs to know
        # how many MSHRs the cache it shares a port with actually has. Two
        # separate flags could disagree; this one cannot.
        l1d_mshrs=system.cpu.dcache.mshrs,
    )


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main():
    args = build_parser().parse_args()

    if args.cpu_type == "atomic" and not args.disable_phq:
        # Not an error: the atomic CPU has no PHQ by construction. Say so
        # loudly, because a silently PHQ-less "experiment" run would be a
        # very confusing result for the loop to interpret.
        print(
            "hint_gather_se.py: note: --cpu-type=atomic has no Prefetch "
            "Hint Queue; HINT.GATHER executes as an architectural NOP and "
            "no system.cpu.phq.* stats will be emitted.",
            file=sys.stderr,
        )

    system = build_system(args)

    root = Root(full_system=False, system=system)
    m5.instantiate()

    print("hint_gather_se.py: beginning simulation (cpu-type=%s, phq=%s)"
          % (args.cpu_type,
             "off" if (args.disable_phq or args.cpu_type == "atomic")
             else "on"))

    exit_event = m5.simulate()

    # Dump explicitly rather than relying on the exit hook: the loop reads
    # m5out/stats.txt unconditionally and an empty file is a worse failure
    # mode than a duplicated dump.
    m5.stats.dump()

    print(
        "hint_gather_se.py: exiting @ tick %d because %s"
        % (m5.curTick(), exit_event.getCause())
    )

    # Propagate a non-zero status for anything other than a clean finish so
    # the loop does not mistake a crashed run for a fast one.
    cause = exit_event.getCause()
    clean = (
        "exiting with last active thread context" in cause
        or "a thread reached the max instruction count" in cause
        or "simulate() limit reached" in cause
        or "target called exit()" in cause
    )
    sys.exit(0 if clean else 1)


main()
