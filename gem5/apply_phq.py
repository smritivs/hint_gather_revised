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
    Region(
        rid="CPU_INCLUDE",
        path="src/cpu/o3/cpu.hh",
        anchors=[
            '#include "cpu/o3/scoreboard.hh"',
            '#include "cpu/o3/rob.hh"',
        ],
        where="after",
        body=BODY_CPU_INCLUDE,
        comment="//",
        indent="",
        why="Make PrefetchHintQueue visible to every O3 translation unit.",
    ),
    Region(
        rid="CPU_MEMBERS",
        path="src/cpu/o3/cpu.hh",
        anchors=[
            "    BaseMMU *mmu;\n    using LSQRequest = LSQ::LSQRequest;",
            "    BaseMMU *mmu;",
        ],
        where="after",
        body=BODY_CPU_MEMBERS,
        comment="//",
        indent="    ",
        why=(
            "Add the phq member plus the four accessors the PHQ needs "
            "(scoreboard read port, register read, thread context, "
            "D-cache port)."
        ),
        requires=("class CPU : public BaseCPU",),
    ),
    Region(
        rid="CPU_INIT",
        path="src/cpu/o3/cpu.cc",
        anchors=[
            "    fetch.setActiveThreads(&activeThreads);",
        ],
        where="before",
        body=BODY_CPU_INIT,
        comment="//",
        indent="    ",
        why="Wire the PHQ to its CPU in the o3::CPU constructor.",
        requires=("CPU::CPU(const BaseO3CPUParams &params)",),
    ),
    Region(
        rid="CPU_PARAM_IMPORT",
        path="src/cpu/o3/BaseO3CPU.py",
        anchors=[
            "from m5.objects.FUPool import *",
        ],
        where="after",
        body=BODY_CPU_PARAM_IMPORT,
        comment="#",
        indent="",
        why="Import PrefetchHintQueue into the BaseO3CPU param namespace.",
    ),
    Region(
        rid="CPU_PARAM",
        path="src/cpu/o3/BaseO3CPU.py",
        anchors=[
            '    needsTSO = Param.Bool(False, "Enable TSO Memory model")',
        ],
        where="after",
        body=BODY_CPU_PARAM,
        comment="#",
        indent="    ",
        why="Declare the cpu.phq parameter (this is what names the stat group).",
        requires=("class BaseO3CPU(BaseCPU):",),
    ),
    Region(
        rid="SCONS",
        path="src/cpu/o3/SConscript",
        anchors=[
            "    Source('rob.cc')",
            '    Source("rob.cc")',
        ],
        where="after",
        body=BODY_SCONS,
        comment="#",
        indent="    ",
        why="Register the new SimObject, source file and debug flag.",
    ),
    Region(
        rid="IEW_DISPATCH",
        path="src/cpu/o3/iew.cc",
        anchors=[
            "        } else if (inst->isNop()) {",
        ],
        where="before",
        body=BODY_IEW_DISPATCH,
        comment="//",
        indent="        ",
        why=(
            "THE core edit: route HINT.GATHER to the PHQ and mark it "
            "complete, without touching the IQ, the LSQ or the FU pool."
        ),
        requires=("IEW::dispatchInsts(ThreadID tid)", "add_to_iq"),
    ),
    Region(
        rid="IEW_SQUASH",
        path="src/cpu/o3/iew.cc",
        anchors=[
            "    ldstQueue.squash(fromCommit->commitInfo[tid].doneSeqNum, tid);",
        ],
        where="after",
        body=BODY_IEW_SQUASH,
        comment="//",
        indent="    ",
        why="Invalidate PHQ entries younger than the squash point.",
        requires=("IEW::squash(ThreadID tid)",),
    ),
    Region(
        rid="IQ_ALLOC",
        path="src/cpu/o3/inst_queue.cc",
        anchors=[
            "InstructionQueue::insert(const DynInstPtr &new_inst)\n{",
        ],
        where="after",
        body=BODY_IQ_ALLOC,
        comment="//",
        indent="    ",
        why="Structural assertion: count IQ entries taken by a HINT.GATHER.",
    ),
    Region(
        rid="IQ_ALLOC_NONSPEC",
        path="src/cpu/o3/inst_queue.cc",
        anchors=[
            "InstructionQueue::insertNonSpec(const DynInstPtr &new_inst)\n{",
        ],
        where="after",
        body=BODY_IQ_ALLOC,
        comment="//",
        indent="    ",
        why="Structural assertion: the non-speculative IQ path as well.",
    ),
    Region(
        rid="FU_GRANT",
        path="src/cpu/o3/inst_queue.cc",
        anchors=[
            "        int idx = FUPool::NoCapableFU;",
        ],
        where="before",
        body=BODY_FU_GRANT,
        comment="//",
        indent="        ",
        why="Structural assertion: count FU port cycles taken by a HINT.GATHER.",
        requires=("issuing_inst",),
    ),
    Region(
        rid="LSQ_ALLOC",
        path="src/cpu/o3/lsq_unit.cc",
        anchors=[
            "LSQUnit::insert(const DynInstPtr &inst)\n{",
        ],
        where="after",
        body=BODY_LSQ_ALLOC,
        comment="//",
        indent="    ",
        why="Structural assertion: count LSQ entries taken by a HINT.GATHER.",
    ),
    Region(
        rid="LSQ_RESP_INTERCEPT",
        path="src/cpu/o3/lsq.cc",
        anchors=[
            "LSQ::DcachePort::recvTimingResp(PacketPtr pkt)\n{",
        ],
        where="after",
        body=BODY_LSQ_RESP_INTERCEPT,
        comment="//",
        indent="    ",
        why=(
            "Claim PHQ responses before LSQ::recvTimingResp() panics on "
            "their sender state, and observe demand responses for "
            "prefetchesLate."
        ),
    ),
    Region(
        rid="RENAME_INVARIANTS",
        path="src/cpu/o3/rename.cc",
        anchors=[
            "        renameDestRegs(inst, inst->threadNumber);",
        ],
        where="after",
        body=BODY_RENAME_INVARIANTS,
        comment="//",
        indent="        ",
        why="Assert zero destination registers and non-memory-ref at rename.",
        requires=("Rename::renameInsts(ThreadID tid)",),
    ),
    Region(
        rid="ROB_ALLOC",
        path="src/cpu/o3/rob.cc",
        anchors=[
            "ROB::insertInst(const DynInstPtr &inst)\n{",
        ],
        where="after",
        body=BODY_ROB_ALLOC,
        comment="//",
        indent="    ",
        why=(
            "Positive control: count the one structure a HINT.GATHER is "
            "supposed to occupy."
        ),
    ),
    Region(
        rid="ISA_FORMAT_INCLUDE",
        path="src/arch/riscv/isa/formats/formats.isa",
        anchors=[
            '##include "vector_mem.isa"',
            '##include "compressed.isa"',
        ],
        where="after",
        body=BODY_ISA_FORMAT_INCLUDE,
        comment="//",
        indent="",
        why="Pull the new HintGatherOp format into the RISC-V ISA description.",
    ),
    Region(
        rid="ISA_DECODE",
        path="src/arch/riscv/isa/decoder.isa",
        anchors=[
            "    0x3: decode OPCODE5 {",
        ],
        where="after",
        body=BODY_ISA_DECODE,
        comment="//",
        indent="        ",
        why="Decode opcode 0x0B (custom-0) as HINT.GATHER.",
        requires=("decode QUADRANT default Unknown::unknown() {",),
    ),
]


# ==========================================================================
#  Machinery
# ==========================================================================


class PatchError(Exception):
    """Raised with a message intended to be read by a human or an LLM."""


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _abs(root: str, rel: str) -> str:
    return os.path.join(root, rel)


def _is_applied(text: str, region: Region) -> bool:
    return f"{MARKER_PREFIX} {region.rid}" in text


def _has_orphan_end(text: str, region: Region) -> bool:
    return (f"{MARKER_SUFFIX} {region.rid}" in text) and not _is_applied(
        text, region
    )


def _pick_anchor(text: str, region: Region) -> Optional[str]:
    """Return the first anchor candidate occurring exactly once."""
    for candidate in region.anchors:
        if text.count(candidate) == 1:
            return candidate
    return None


def _anchor_diagnosis(text: str, region: Region) -> str:
    """A precise, actionable description of why no anchor matched."""
    lines = [
        f"  region      : {region.rid}",
        f"  file        : {region.path}",
        f"  purpose     : {region.why}",
        "  anchors tried (each must occur EXACTLY once):",
    ]
    for candidate in region.anchors:
        n = text.count(candidate)
        shown = candidate.replace("\n", "\\n")
        verdict = "OK" if n == 1 else ("NOT FOUND" if n == 0 else f"{n} matches")
        lines.append(f"    [{verdict:>10}] {shown!r}")
        if n == 0:
            near = _nearest_line(text, candidate)
            if near:
                lines.append(f"                 closest line in file: {near!r}")
    lines.append(
        "  fix         : edit the `anchors` list for this region in "
        "apply_phq.py so that it matches text that appears exactly once "
        "in this gem5 version, or insert the region body by hand between "
        f"`{region.comment} {MARKER_PREFIX} {region.rid}` and "
        f"`{region.comment} {MARKER_SUFFIX} {region.rid}`."
    )
    return "\n".join(lines)


def _nearest_line(text: str, needle: str) -> Optional[str]:
    """Best-effort: the existing line most similar to the anchor's first line."""
    first = needle.split("\n", 1)[0].strip()
    if not first:
        return None
    best = difflib.get_close_matches(
        first, [ln.strip() for ln in text.splitlines()], n=1, cutoff=0.6
    )
    return best[0] if best else None


def _insert(text: str, region: Region, anchor: str) -> str:
    idx = text.index(anchor)
    block = region.block()
    if region.where == "after":
        end = idx + len(anchor)
        # Consume the rest of the anchor's final line so we insert on a
        # clean line boundary.
        nl = text.find("\n", end)
        if nl == -1:
            return text + "\n" + block
        return text[: nl + 1] + block + text[nl + 1 :]
    elif region.where == "before":
        # Rewind to the start of the anchor's first line.
        bol = text.rfind("\n", 0, idx) + 1
        return text[:bol] + block + text[bol:]
    raise PatchError(f"region {region.rid}: bad `where` value {region.where!r}")


def _remove(text: str, region: Region) -> Tuple[str, bool]:
    """Remove every BEGIN..END block for this region id. Idempotent."""
    begin_tag = f"{MARKER_PREFIX} {region.rid}"
    end_tag = f"{MARKER_SUFFIX} {region.rid}"
    changed = False
    while True:
        b = text.find(begin_tag)
        if b == -1:
            break
        e = text.find(end_tag, b)
        if e == -1:
            raise PatchError(
                f"{region.path}: found `{begin_tag}` with no matching "
                f"`{end_tag}`. The file has been hand-edited; remove the "
                f"dangling marker before reverting."
            )
        bol = text.rfind("\n", 0, b) + 1
        eol = text.find("\n", e)
        eol = len(text) if eol == -1 else eol + 1
        text = text[:bol] + text[eol:]
        changed = True
    return text, changed


# --------------------------------------------------------------------------
# Pre-flight
# --------------------------------------------------------------------------
def validate_root(root: str) -> None:
    if not os.path.isdir(root):
        raise PatchError(f"--gem5-root {root!r} is not a directory")
    sentinel = os.path.join(root, "src", "cpu", "o3", "iew.cc")
    if not os.path.isfile(sentinel):
        raise PatchError(
            f"--gem5-root {root!r} does not look like a gem5 checkout: "
            f"{sentinel} is missing. Point --gem5-root at the directory "
            f"that contains SConstruct, src/ and configs/."
        )
    if not os.path.isfile(os.path.join(root, "SConstruct")):
        raise PatchError(
            f"--gem5-root {root!r} has src/cpu/o3/iew.cc but no SConstruct. "
            f"Point --gem5-root at the top of the gem5 source tree."
        )


def validate_payload() -> None:
    missing = [
        src for src, _ in NEW_FILES if not os.path.isfile(os.path.join(SCRIPT_DIR, src))
    ]
    if missing:
        raise PatchError(
            "apply_phq.py cannot find its own payload files: "
            + ", ".join(missing)
            + f" (looked under {SCRIPT_DIR}). Run the script from inside the "
            "hint_gather/gem5 directory, or restore the missing files."
        )


def validate_region_ids() -> None:
    seen = set()
    for r in REGIONS:
        key = (r.path, r.rid)
        if key in seen:
            raise PatchError(f"duplicate region id {r.rid} for {r.path}")
        seen.add(key)


# --------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------
def do_apply(root: str, dry_run: bool, verbose: bool) -> int:
    validate_payload()

    # Phase 1: verify EVERY region can be placed before writing anything.
    # A half-applied tree is the worst outcome for the repair loop.
    plan = []
    for region in REGIONS:
        path = _abs(root, region.path)
        if not os.path.isfile(path):
            raise PatchError(
                f"region {region.rid}: {region.path} does not exist under "
                f"{root}. This gem5 version does not have the file this "
                f"patch expects; see gem5/README.md 'If the build fails'."
            )
        text = _read(path)

        if _has_orphan_end(text, region):
            raise PatchError(
                f"region {region.rid}: {region.path} contains an END marker "
                f"with no BEGIN marker. Remove the dangling marker by hand."
            )
        if _is_applied(text, region):
            plan.append((region, path, None))
            continue

        for needed in region.requires:
            if needed not in text:
                raise PatchError(
                    f"region {region.rid}: {region.path} does not contain the "
                    f"expected text {needed!r}. Either this is not the file "
                    f"we think it is, or the gem5 version is unsupported.\n"
                    f"{_anchor_diagnosis(text, region)}"
                )

        anchor = _pick_anchor(text, region)
        if anchor is None:
            raise PatchError(
                "could not place a HINT.GATHER region -- no anchor matched "
                "exactly once.\n" + _anchor_diagnosis(text, region)
            )
        plan.append((region, path, anchor))

    # Phase 2: write.
    n_new, n_skipped = 0, 0
    for region, path, anchor in plan:
        if anchor is None:
            n_skipped += 1
            if verbose:
                print(f"  = {region.path:<42} {region.rid} (already applied)")
            continue
        text = _read(path)
        new_text = _insert(text, region, anchor)
        if not dry_run:
            _write(path, new_text)
        n_new += 1
        print(f"  + {region.path:<42} {region.rid}")

    # Phase 3: new files.
    n_copied = 0
    for src_rel, dst_rel in NEW_FILES:
        src = os.path.join(SCRIPT_DIR, src_rel)
        dst = _abs(root, dst_rel)
        payload = _read(src)
        if os.path.isfile(dst) and _read(dst) == payload:
            if verbose:
                print(f"  = {dst_rel:<42} (identical)")
            continue
        if not dry_run:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
        n_copied += 1
        print(f"  + {dst_rel:<42} (copied)")

    # Phase 4: ensure SConstruct disables -Werror so GCC 12-15 (Debian, Ubuntu,
    # Arch Linux) do not fail on standard library warnings in gem5 v24.0.0.1.
    sconstruct_path = _abs(root, "SConstruct")
    if os.path.isfile(sconstruct_path):
        sc_text = _read(sconstruct_path)
        if "CCFLAGS=['-Werror'," in sc_text:
            if not dry_run:
                _write(
                    sconstruct_path,
                    sc_text.replace("CCFLAGS=['-Werror',", "CCFLAGS=['-Wno-error',"),
                )
            print(f"  + {'SConstruct':<42} (-Wno-error enabled)")

    print(
        f"\nHINT.GATHER applied to {root}\n"
        f"  regions inserted : {n_new}\n"
        f"  regions already present : {n_skipped}\n"
        f"  files copied     : {n_copied}"
    )
    if dry_run:
        print("  (--dry-run: nothing was written)")
    else:
        print(
            "\nNow build:\n"
            "  scons build/RISCV/gem5.opt -j$(nproc)\n"
            "See gem5/README.md for the correctness gate and the stat names."
        )
    return 0


def do_revert(root: str, dry_run: bool, verbose: bool) -> int:
    n_regions, n_files = 0, 0
    # Group by file so each file is read/written once.
    by_path = {}
    for region in REGIONS:
        by_path.setdefault(region.path, []).append(region)

    for rel, regions in by_path.items():
        path = _abs(root, rel)
        if not os.path.isfile(path):
            if verbose:
                print(f"  ? {rel:<42} (absent, skipped)")
            continue
        text = _read(path)
        original = text
        for region in regions:
            text, changed = _remove(text, region)
            if changed:
                n_regions += 1
                print(f"  - {rel:<42} {region.rid}")
        if text != original and not dry_run:
            _write(path, text)

    for _, dst_rel in NEW_FILES:
        dst = _abs(root, dst_rel)
        if os.path.isfile(dst):
            if not dry_run:
                os.remove(dst)
            n_files += 1
            print(f"  - {dst_rel:<42} (deleted)")

    print(
        f"\nHINT.GATHER reverted from {root}\n"
        f"  regions removed : {n_regions}\n"
        f"  files deleted   : {n_files}"
    )
    if dry_run:
        print("  (--dry-run: nothing was written)")
    else:
        print(
            "\nThe tree should now be byte-identical to stock gem5. Verify "
            "with `git diff` / `git status` if it is a git checkout."
        )
    return 0


def do_check(root: str, verbose: bool) -> int:
    applied, missing, broken = [], [], []

    for region in REGIONS:
        path = _abs(root, region.path)
        if not os.path.isfile(path):
            broken.append((region, "file does not exist"))
            continue
        text = _read(path)
        if _has_orphan_end(text, region):
            broken.append((region, "END marker without BEGIN marker"))
        elif _is_applied(text, region):
            if f"{MARKER_SUFFIX} {region.rid}" not in text:
                broken.append((region, "BEGIN marker without END marker"))
            else:
                applied.append(region)
        else:
            missing.append(region)

    files_ok, files_missing, files_stale = [], [], []
    for src_rel, dst_rel in NEW_FILES:
        src = os.path.join(SCRIPT_DIR, src_rel)
        dst = _abs(root, dst_rel)
        if not os.path.isfile(dst):
            files_missing.append(dst_rel)
        elif not os.path.isfile(src):
            files_stale.append(f"{dst_rel} (payload source missing)")
        elif _read(dst) != _read(src):
            files_stale.append(dst_rel)
        else:
            files_ok.append(dst_rel)

    total_regions = len(REGIONS)
    total_files = len(NEW_FILES)

    print(f"HINT.GATHER status for {root}")
    print(f"  regions applied : {len(applied)}/{total_regions}")
    print(f"  files installed : {len(files_ok)}/{total_files}")

    if verbose or missing or broken or files_missing or files_stale:
        for region in applied:
            print(f"    [ OK      ] {region.path:<42} {region.rid}")
        for region in missing:
            print(f"    [ MISSING ] {region.path:<42} {region.rid}")
        for region, reason in broken:
            print(
                f"    [ BROKEN  ] {region.path:<42} {region.rid}  -- {reason}"
            )
        for f in files_ok:
            if verbose:
                print(f"    [ OK      ] {f}")
        for f in files_missing:
            print(f"    [ MISSING ] {f}")
        for f in files_stale:
            print(f"    [ STALE   ] {f}  -- differs from payload; re-apply")

    fully = (
        len(applied) == total_regions
        and len(files_ok) == total_files
        and not broken
    )
    none_at_all = (
        not applied and not broken and len(files_missing) == total_files
    )

    if fully:
        print("\nGATE: APPLIED")
        return 0
    if none_at_all:
        print("\nGATE: NOT-APPLIED")
        return 2
    print(
        "\nGATE: PARTIALLY-APPLIED\n"
        "Re-run without --check to install the missing regions (existing "
        "regions are left alone), or --revert first for a clean slate."
    )
    return 3


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="apply_phq.py",
        description=(
            "Install, check or remove the HINT.GATHER Prefetch Hint Queue "
            "in a gem5 checkout. Idempotent and fully reversible; see "
            "docs/DESIGN.md 4.2."
        ),
    )
    p.add_argument(
        "--gem5-root",
        required=True,
        help="Top of the gem5 source tree (the directory with SConstruct).",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--revert",
        action="store_true",
        help="Remove every HINT.GATHER region and delete the copied files.",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help=(
            "Report applied / not-applied / partially-applied. "
            "Exit 0 / 2 / 3 respectively."
        ),
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Say what would change without writing anything.",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    root = os.path.abspath(os.path.expanduser(args.gem5_root))

    try:
        validate_region_ids()
        validate_root(root)
        if args.check:
            return do_check(root, args.verbose)
        if args.revert:
            return do_revert(root, args.dry_run, args.verbose)
        return do_apply(root, args.dry_run, args.verbose)
    except PatchError as e:
        print(f"\napply_phq.py: ERROR\n{e}\n", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
