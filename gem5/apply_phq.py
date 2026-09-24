#!/usr/bin/env python3
# Copyright 2026 Google LLC
#
# HINT.GATHER -- deterministic, idempotent, reversible gem5 patcher.
#
# Role
#   Installs the HINT.GATHER microarchitecture into an unmodified gem5
#   checkout. It does two things and nothing else:
#
#     1. COPIES four new files into the tree (they are never edited, so they
#        carry no markers).
#     2. INSERTS a small number of clearly delimited REGIONS into existing
#        gem5 sources. Every inserted region is bracketed by
#
#             BEGIN HINT.GATHER (apply_phq.py) <REGION_ID>
#             ...
#             END HINT.GATHER (apply_phq.py) <REGION_ID>
#
#        using the comment syntax of the target file. That gives us, for
#        free:
#           (a) re-running is a no-op (a region whose BEGIN marker is
#               already present is skipped),
#           (b) --revert removes exactly those regions and nothing else,
#           (c) --check reports applied / not-applied / partially-applied,
#           (d) an LLM repair agent can locate and rewrite exactly ONE
#               region without touching anything else -- every region id is
#               unique across the whole tree.
#
# Usage
#     python apply_phq.py --gem5-root /path/to/gem5
#     python apply_phq.py --gem5-root /path/to/gem5 --check
#     python apply_phq.py --gem5-root /path/to/gem5 --revert
#     python apply_phq.py --gem5-root /path/to/gem5 --dry-run
#
# Exit codes
#     0   success (or, for --check, "fully applied")
#     1   failure: an anchor was not found, or was found more than once, or
#         a prerequisite was missing. The message names the file, the region
#         id, and every anchor candidate that was tried -- that message is
#         the context handed to the repair agent.
#     2   --check only: NOT APPLIED
#     3   --check only: PARTIALLY APPLIED
#
# Normative spec
#   docs/DESIGN.md, especially 1.1, 1.3, 1.4, 2 and 4.2.
#
# Target versions
#   Written against the gem5 v23.x source layout and cross-checked against
#   gem5 develop (~v24). Every anchor below was read verbatim out of a real
#   checkout; where the two trees differ, multiple anchor candidates are
#   listed and tried in order. Anchors are matched by TEXT, never by line
#   number.

from __future__ import annotations

import argparse
import difflib
import os
import shutil
import sys
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

MARKER_PREFIX = "BEGIN HINT.GATHER (apply_phq.py)"
MARKER_SUFFIX = "END HINT.GATHER (apply_phq.py)"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# --------------------------------------------------------------------------
# New files. (source relative to this script, destination relative to
# --gem5-root). These are copied verbatim and never edited.
# --------------------------------------------------------------------------
NEW_FILES: List[Tuple[str, str]] = [
    ("src/cpu/o3/prefetch_hint_queue.hh", "src/cpu/o3/prefetch_hint_queue.hh"),
    ("src/cpu/o3/prefetch_hint_queue.cc", "src/cpu/o3/prefetch_hint_queue.cc"),
    ("src/cpu/o3/PrefetchHintQueue.py", "src/cpu/o3/PrefetchHintQueue.py"),
    (
        "src/arch/riscv/isa/formats/hint_gather.isa",
        "src/arch/riscv/isa/formats/hint_gather.isa",
    ),
]


# --------------------------------------------------------------------------
# Region model
# --------------------------------------------------------------------------
@dataclass
class Region:
    """One delimited insertion into an existing gem5 source file."""

    # Unique across the whole patch. Appears in both markers. A repair agent
    # is expected to address regions by this id.
    rid: str
    # Path relative to --gem5-root.
    path: str
    # Anchor candidates, tried in order. Each must appear EXACTLY ONCE in
    # the file; appearing zero or many times moves on to the next candidate
    # (and, if all fail, aborts with a message listing all of them).
    anchors: Sequence[str]
    # "before" or "after" the anchor text.
    where: str
    # The region body, without markers. Already indented for its site.
    body: str
    # "//" for C/C++/ISA files, "#" for Python/SCons.
    comment: str = "//"
    # Indentation applied to the two marker lines.
    indent: str = ""
    # Human-readable reason, printed by --check -v and on failure.
    why: str = ""
    # Substrings that must be present in the file for the patch to make
    # sense. Checked before anything is written.
    requires: Sequence[str] = field(default_factory=tuple)

    def begin_marker(self) -> str:
        return f"{self.indent}{self.comment} {MARKER_PREFIX} {self.rid}"

    def end_marker(self) -> str:
        return f"{self.indent}{self.comment} {MARKER_SUFFIX} {self.rid}"

    def block(self) -> str:
        return (
            self.begin_marker()
            + "\n"
            + self.body.rstrip("\n")
            + "\n"
            + self.end_marker()
            + "\n"
        )


# ==========================================================================
#  REGION BODIES
#
#  Each body below is the exact text inserted between the markers. They are
#  kept here, in one file, on purpose: the patcher is the single source of
#  truth for everything it writes, so there is no way for a payload file to
#  drift out of sync with the patch.
# ==========================================================================

# -- 1. src/cpu/StaticInstFlags.py -----------------------------------------
BODY_STATIC_INST_FLAG = '''\
        # docs/DESIGN.md 1.4: marks the RISC-V custom-0 HINT.GATHER /
        # HINT.GATHER.C. IEW::dispatchInsts() uses it to route the
        # instruction to the Prefetch Hint Queue instead of the issue
        # queue. Appended at the END of the list so that every existing
        # flag keeps its ordinal.
        "IsHintGather",  # Prefetch hint handled by the PHQ, not the IQ.\
'''

# -- 2. src/cpu/static_inst.hh ---------------------------------------------
BODY_STATIC_INST_ACCESSORS = '''\
    /**
     * docs/DESIGN.md 1.1 / 1.4: RISC-V custom-0 HINT.GATHER.
     *
     * True for an instruction that the O3 pipeline must hand to the
     * Prefetch Hint Queue at dispatch rather than insert into the issue
     * queue, the LSQ, or the functional-unit pool.
     */
    bool isHintGather() const { return flags[IsHintGather]; }

    /**
     * The funct3 / funct7 payload of a HINT.GATHER (docs/DESIGN.md 1.1).
     *
     * Deliberately ISA-agnostic: src/cpu/o3/ must never include a RISC-V
     * header, so the decoded fields are handed across this small struct
     * instead of through an ISA-specific downcast.
     */
    struct HintGatherFields
    {
        /** 0 = HINT.GATHER (value form), 1 = HINT.GATHER.C (chase form). */
        uint8_t variant = 0;
        /** 0 = prefetch into L1D, 1 = prefetch into L2. */
        uint8_t level = 0;
        /** 1 = droppable under MSHR pressure. */
        uint8_t droppable = 0;
        /** Index scale: element size is (1 << shift) bytes. */
        uint8_t shift = 0;
        /** Issue (fanout + 1) prefetches. */
        uint8_t fanout = 0;
    };

    /**
     * Fill in the HINT.GATHER payload.
     *
     * The base implementation returns false, so every instruction in every
     * other ISA is completely unaffected. Only the RISC-V HintGatherOp
     * format (src/arch/riscv/isa/formats/hint_gather.isa) overrides it.
     */
    virtual bool
    hintGatherFields(HintGatherFields &) const
    {
        return false;
    }\
'''

# -- 3. src/cpu/o3/cpu.hh (include) ----------------------------------------
BODY_CPU_INCLUDE = '''\
// docs/DESIGN.md 2. Included here rather than forward-declared so that
// every O3 translation unit that already includes cpu.hh (iew.cc,
// inst_queue.cc, lsq.cc, lsq_unit.cc, rename.cc, rob.cc) can call into the
// PHQ without needing an include region of its own.
#include "cpu/o3/prefetch_hint_queue.hh"\
'''

# -- 4. src/cpu/o3/cpu.hh (members) ----------------------------------------
BODY_CPU_MEMBERS = '''\
    /**
     * The Prefetch Hint Queue (docs/DESIGN.md 2).
     *
     * Attached in the config as `cpu.phq`, which is exactly what gives its
     * statistics the path `system.cpu.phq.*` required by
     * docs/DESIGN.md 4.2 (gem5 derives stat paths from the Python
     * attribute name in _bindStatHierarchy). Every use site null-checks
     * this pointer so that a partially applied patch degrades to "hints
     * behave as NOPs" instead of to a segfault.
     */
    PrefetchHintQueue *phq = nullptr;

    /**
     * Scoreboard read port used by the PHQ's `poll_rf` wake-up policy.
     * docs/DESIGN.md 1.2 costs poll_rf at exactly one scoreboard read port
     * and no wake-up CAM; this accessor IS that port.
     */
    bool
    phqRegReady(PhysRegIdPtr phys_reg) const
    {
        return scoreboard.getReg(phys_reg);
    }

    /** Physical register read on behalf of the PHQ. */
    RegVal
    phqReadReg(PhysRegIdPtr phys_reg, ThreadID tid)
    {
        return getReg(phys_reg, tid);
    }

    /** ThreadContext used for the PHQ's non-faulting translations. */
    ::gem5::ThreadContext *
    phqThreadContext(ThreadID tid)
    {
        return thread[tid]->getTC();
    }

    /**
     * The D-cache RequestPort the PHQ borrows from the LSQ.
     *
     * Sharing the LSQ's port is deliberate. docs/DESIGN.md 2 places the PHQ
     * "adjacent to the LSQ"; giving it a private port would silently grant
     * the core an extra cache port that docs/AREA.md does not pay for, and
     * would make the speedup look better than the design earns.
     */
    RequestPort &phqDataPort() { return iew.ldstQueue.getDataPort(); }\
'''

# -- 5. src/cpu/o3/cpu.cc (constructor) ------------------------------------
BODY_CPU_INIT = '''\
    // docs/DESIGN.md 2. The PHQ is self-clocked (it schedules its own
    // event whenever it has work), so there is deliberately no edit to
    // CPU::tick().
    phq = params.phq;
    if (phq)
        phq->setCPU(this);
\
'''

# -- 6. src/cpu/o3/BaseO3CPU.py (import) -----------------------------------
BODY_CPU_PARAM_IMPORT = '''\
# docs/DESIGN.md 2.
from m5.objects.PrefetchHintQueue import PrefetchHintQueue\
'''

# -- 7. src/cpu/o3/BaseO3CPU.py (param) ------------------------------------
BODY_CPU_PARAM = '''\
    phq = Param.PrefetchHintQueue(
        PrefetchHintQueue(),
        "Prefetch Hint Queue for the RISC-V custom-0 HINT.GATHER "
        "instruction (docs/DESIGN.md 2). Because this parameter is named "
        "'phq', the object becomes the CPU child 'phq' and its statistics "
        "appear as system.cpu.phq.* as required by docs/DESIGN.md 4.2.",
    )\
'''

# -- 8. src/cpu/o3/SConscript ----------------------------------------------
BODY_SCONS = '''\
    # docs/DESIGN.md 2 and 4.2: the Prefetch Hint Queue.
    SimObject('PrefetchHintQueue.py', sim_objects=['PrefetchHintQueue'])
    Source('prefetch_hint_queue.cc')
    DebugFlag('PHQ')\
'''

# -- 9. src/cpu/o3/iew.cc (dispatch) ---------------------------------------
# NOTE: this body opens with `} else if (...) {`, closing the preceding
# barrier branch, and is inserted immediately BEFORE the `} else if
# (inst->isNop()) {` line, which then closes this new branch. Reverting
# removes BEGIN..END inclusive and restores the original chain exactly.
BODY_IEW_DISPATCH = '''\
        } else if (inst->staticInst->isHintGather()) {
            // docs/DESIGN.md 1.4 -- this is the contribution.
            //
            // The hint has already allocated its ROB entry (rename did
            // that). Here it allocates NOTHING else: add_to_iq stays
            // false, ldstQueue is never touched, and because the static
            // instruction carries No_OpClass it could not take a
            // functional unit even if it somehow reached one.
            //
            // It is marked complete right now, so it never blocks the ROB
            // head and retires the cycle it arrives there.
            [[maybe_unused]] const bool hg_accepted =
                cpu->phq && cpu->phq->dispatchHint(inst);

            DPRINTF(IEW, "[tid:%i] Issue: HINT.GATHER [sn:%llu] %s by the "
                    "PHQ; not adding to IQ or LSQ.\\n",
                    tid, inst->seqNum,
                    hg_accepted ? "accepted" : "dropped");

            inst->setIssued();
            inst->setExecuted();
            inst->setCanCommit();

            // Harmless for a zero-destination instruction, but kept for
            // symmetry with the NOP path immediately below.
            instQueue.recordProducer(inst);

            add_to_iq = false;\
'''

# -- 10. src/cpu/o3/iew.cc (squash) ----------------------------------------
BODY_IEW_SQUASH = '''\
    // docs/DESIGN.md 1.4: PHQ entries whose sequence number is younger than
    // the squash point are invalidated. Failing to do this would only waste
    // a prefetch, never break correctness -- but we implement and measure
    // it (system.cpu.phq.hintsDroppedSquash).
    if (cpu->phq)
        cpu->phq->squash(fromCommit->commitInfo[tid].doneSeqNum, tid);\
'''

# -- 11/12. src/cpu/o3/inst_queue.cc (IQ allocation) -----------------------
BODY_IQ_ALLOC = '''\
    // Structural assertion, docs/DESIGN.md 4.2. Counted at the allocation
    // site itself so that `system.cpu.phq.iqEntriesAllocated == 0` is
    // evidence rather than assertion-by-construction.
    if (cpu->phq)
        cpu->phq->noteIqAllocation(new_inst);
\
'''

# -- 13. src/cpu/o3/inst_queue.cc (FU arbitration) -------------------------
BODY_FU_GRANT = '''\
        // Structural assertion, docs/DESIGN.md 4.2 / 1.4. Placed before
        // the op_class test rather than inside it, because a HINT.GATHER
        // carries No_OpClass and would otherwise slip past the FU
        // arbitration unobserved. Reaching this point at all already means
        // the hint wrongly entered the issue queue.
        if (cpu->phq)
            cpu->phq->noteFuPortCycle(issuing_inst);

\
'''

# -- 14. src/cpu/o3/lsq_unit.cc (LSQ allocation) ---------------------------
BODY_LSQ_ALLOC = '''\
    // Structural assertion, docs/DESIGN.md 4.2 / 1.3.3. A HINT.GATHER
    // takes no part in store-to-load forwarding or memory disambiguation,
    // so it must never reach the single entry point of the LSQ.
    if (cpu->phq)
        cpu->phq->noteLsqAllocation(inst);
\
'''

# -- 15. src/cpu/o3/lsq.cc (response interception) -------------------------
BODY_LSQ_RESP_INTERCEPT = '''\
    // docs/DESIGN.md 2: the PHQ shares this RequestPort with the LSQ.
    //
    // This interception is mandatory, not cosmetic. LSQ::recvTimingResp()
    // does
    //     LSQRequest *request = dynamic_cast<LSQRequest*>(pkt->senderState);
    //     panic_if(!request, "Got packet back with unknown sender state");
    // so a PHQ response reaching it would kill the simulation. Claiming
    // PHQ packets here also lets the PHQ observe ordinary demand responses,
    // which is how system.cpu.phq.prefetchesLate is computed.
    if (cpu->phq && cpu->phq->recvTimingResp(pkt))
        return true;
\
'''

# -- 16. src/cpu/o3/rename.cc (structural invariants) ----------------------
BODY_RENAME_INVARIANTS = '''\
        // docs/DESIGN.md 1.3 / 1.4. Rename is the one stage a HINT.GATHER
        // legitimately uses (the source-map read is the acknowledged cost),
        // so it is also the right place to assert the two properties that
        // make the rest of the design possible: zero destination registers
        // and not a memory reference. A violation panics with a precise
        // message rather than producing quietly wrong numbers.
        if (cpu->phq)
            cpu->phq->checkRenameInvariants(inst);
\
'''

# -- 17. src/cpu/o3/rob.cc (positive control) ------------------------------
BODY_ROB_ALLOC = '''\
    // docs/DESIGN.md 1.4. The ROB entry is the ONE structure a HINT.GATHER
    // legitimately occupies, and counting it here is the positive control
    // for the three zero-assertions in docs/DESIGN.md 4.2: if
    // iqEntriesAllocated, lsqEntriesAllocated and fuPortCycles are all zero
    // while robEntriesAllocated is large, the hints demonstrably flowed
    // through the pipeline rather than being silently discarded at decode.
    if (cpu->phq)
        cpu->phq->noteRobAllocation(inst);
\
'''

# -- 18. src/arch/riscv/isa/formats/formats.isa ----------------------------
BODY_ISA_FORMAT_INCLUDE = '''\
// docs/DESIGN.md 1.1: RISC-V custom-0 HINT.GATHER / HINT.GATHER.C.
// Must come after basic.isa (it uses BasicDecode).
##include "hint_gather.isa"\
'''

# -- 19. src/arch/riscv/isa/decoder.isa ------------------------------------
# Inserted immediately after `    0x3: decode OPCODE5 {`. Decode case order
# is irrelevant to the ISA parser, so inserting at the top of the block is
# both legal and maximally robust: it does not depend on which neighbouring
# opcodes a given gem5 release happens to implement.
BODY_ISA_DECODE = '''\
        // custom-0. Opcode 0x0B == 0b0001011, i.e. QUADRANT 0x3 (bits
        // <1:0>) and OPCODE5 0x02 (bits <6:2>). Free in gem5 v23 and in
        // gem5 develop. docs/DESIGN.md 1.1.
        //
        // Every funct3 / funct7 combination is a legal HINT.GATHER; the
        // reserved funct7<6:5> bits are ignored rather than faulted,
        // because docs/DESIGN.md 1.3.2 requires that this instruction
        // never raises an exception.
        0x02: HintGatherOp::hint_gather({{
            // Architecturally a NOP (docs/DESIGN.md 1.3.4), which is what
            // makes the AtomicSimpleCPU equivalence gate pass by
            // construction.
            //
            // Rs1 and Rs2 are read so that the ISA parser emits them as
            // source operands. The PHQ needs their renamed physical
            // register ids at dispatch, and docs/DESIGN.md 1.4 explicitly
            // accounts for that rename source-map read as the hint's one
            // extra pipeline cost. Note that Rd is never mentioned, which
            // is precisely how numDestRegs() becomes 0, and that the Mem
            // operand is never mentioned, which is precisely how
            // isLoad()/isStore() stay false and the LSQ never sees it.
            uint64_t hg_base = Rs1;
            uint64_t hg_operand = Rs2;
            (void)hg_base;
            (void)hg_operand;
        }}, IsHintGather, No_OpClass);\
'''


# ==========================================================================
#  THE REGION TABLE
# ==========================================================================
REGIONS: List[Region] = [
    Region(
        rid="STATIC_INST_FLAG",
        path="src/cpu/StaticInstFlags.py",
        anchors=[
            '        "IsHtmCancel",  # Explicitely aborts a HTM transaction',
            '        "IsHtmCancel",',
        ],
        where="after",
        body=BODY_STATIC_INST_FLAG,
        comment="#",
        indent="        ",
        why="Add the IsHintGather static instruction flag.",
        requires=("class StaticInstFlags(Enum):",),
    ),
    Region(
        rid="STATIC_INST_ACCESSORS",
        path="src/cpu/static_inst.hh",
        anchors=[
            "    bool isHtmCancel() const { return flags[IsHtmCancel]; }",
            "    bool isHtmStop() const { return flags[IsHtmStop]; }",
        ],
        where="after",
        body=BODY_STATIC_INST_ACCESSORS,
        comment="//",
        indent="    ",
        why="Add isHintGather() and the ISA-agnostic hintGatherFields() hook.",
        requires=("class StaticInst :",),
    ),
]
