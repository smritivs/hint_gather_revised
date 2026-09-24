# Copyright 2026 Google LLC
#
# HINT.GATHER -- SimObject declaration for the Prefetch Hint Queue.
#
# Role
#   Declares the `PrefetchHintQueue` SimObject. Its parameters mirror,
#   one-for-one, the genome keys that gem5 consumes (docs/DESIGN.md 3):
#
#       phq_entries              phq_dispatch_width   phq_poll_limit
#       wakeup_policy            tlb_miss_policy      prefetch_level
#       mshr_pressure_threshold
#
#   plus a small number of microarchitectural parameters that are NOT in the
#   genome (the evolutionary search does not tune them) but that must exist
#   so the structural costs claimed in docs/AREA.md are actually enforced by
#   the model rather than merely asserted.
#
#   The object is attached to the O3 CPU as `cpu.phq`. gem5's
#   _bindStatHierarchy() derives statistic paths from the Python attribute
#   name, so this -- and only this -- is what makes the required stats appear
#   as `system.cpu.phq.<name>` (docs/DESIGN.md 4.2).
#
# Normative spec
#   ../../../docs/DESIGN.md sections 2, 3 and 4.2.
#
# Design note: string parameters instead of gem5 Enums
#   wakeup_policy / tlb_miss_policy / prefetch_level are Param.String rather
#   than Param.<Enum>. gem5 enums require an extra `enums=[...]` argument in
#   the SConscript and generate `enums/<Name>.hh` headers whose exact
#   spelling differs between gem5 releases. Strings are parsed once in the
#   PrefetchHintQueue constructor, which fatal()s with a precise message on
#   anything unexpected. This removes an entire class of build breakage in
#   exchange for one string comparison at construction time.
#
# Copied into <gem5-root>/src/cpu/o3/PrefetchHintQueue.py by
# ../../apply_phq.py. NEW file: never edited, so no BEGIN/END markers.

from m5.objects.ClockedObject import ClockedObject
from m5.params import *
from m5.proxy import *


class PrefetchHintQueue(ClockedObject):
    """The Prefetch Hint Queue (docs/DESIGN.md 2).

    A small structure adjacent to the LSQ that executes HINT.GATHER outside
    the issue path: a hint allocates a ROB entry and nothing else.
    """

    type = "PrefetchHintQueue"
    cxx_class = "gem5::o3::PrefetchHintQueue"
    cxx_header = "cpu/o3/prefetch_hint_queue.hh"

    # ------------------------------------------------------------------ #
    # Master switch
    # ------------------------------------------------------------------ #
    # When False the PHQ still exists (so every stat in DESIGN.md 4.2 is
    # still emitted, reading zero) but IEW treats every HINT.GATHER as a
    # NOP. This is exactly what `--disable-phq` produces, and it is the
    # baseline arm of the correctness gate.
    enabled = Param.Bool(True, "Execute HINT.GATHER; if False, treat it as a NOP")

    # ------------------------------------------------------------------ #
    # Genome keys (docs/DESIGN.md 3) -- the evolutionary search sets these
    # ------------------------------------------------------------------ #
    phq_entries = Param.Unsigned(8, "Number of PHQ entries (genome: phq_entries, 2..32)")

    phq_dispatch_width = Param.Unsigned(
        2, "Hints accepted from dispatch per cycle (genome: phq_dispatch_width, 1..4)"
    )

    phq_poll_limit = Param.Unsigned(
        16,
        "Scoreboard polls an entry may take before the hint is dropped "
        "(genome: phq_poll_limit, 1..64)",
    )

    wakeup_policy = Param.String(
        "poll_rf",
        "How a waiting entry learns its operand is ready (genome: "
        "wakeup_policy). 'poll_rf': the PHQ head polls the scoreboard "
        "through one read port. 'tag_snoop': phq_entries destination-tag "
        "comparators on the load writeback bus.",
    )

    tlb_miss_policy = Param.String(
        "drop",
        "Behaviour when a hint address has no translation (genome: "
        "tlb_miss_policy). 'drop': kill the hint. 'walk': run a "
        "speculative page-table walk with faults suppressed. HINT.GATHER "
        "never raises an exception either way (docs/DESIGN.md 1.3.2).",
    )

    prefetch_level = Param.String(
        "L1D",
        "Cache level named by the instruction's LEVEL bit (genome: "
        "prefetch_level), 'L1D' or 'L2C'. See gem5/README.md: gem5's "
        "classic hierarchy has no core-side mechanism to name a target "
        "level, so this is recorded but advisory.",
    )

    mshr_pressure_threshold = Param.Float(
        0.75,
        "Fraction of l1d_mshrs at or above which droppable hints are "
        "dropped (genome: mshr_pressure_threshold, 0.0..1.0)",
    )

    # ------------------------------------------------------------------ #
    # Microarchitectural parameters -- NOT in the genome
    # ------------------------------------------------------------------ #
    scoreboard_read_ports = Param.Unsigned(
        1,
        "Scoreboard read ports available to the poll_rf policy. "
        "docs/DESIGN.md 1.2 costs poll_rf at exactly one read port, so the "
        "default is 1: only the oldest waiting entry is polled per cycle. "
        "Ignored by tag_snoop.",
    )

    adder_throughput = Param.Unsigned(
        1,
        "Address computations per cycle on the PHQ's dedicated 64-bit "
        "adder. This is what makes fuPortCycles == 0 meaningful: the hint "
        "address arithmetic never touches the functional-unit pool "
        "(docs/DESIGN.md 1.4).",
    )

    l1d_mshrs = Param.Unsigned(
        4,
        "Number of L1D MSHRs, used as the denominator for "
        "mshr_pressure_threshold. BaseCache::mshrQueue is protected and the "
        "PHQ holds only a RequestPort, so the config script MUST keep this "
        "equal to cpu.dcache.mshrs; hint_gather_se.py does so automatically.",
    )
