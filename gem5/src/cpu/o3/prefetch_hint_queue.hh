/*
 * HINT.GATHER -- Prefetch Hint Queue (PHQ)
 * ----------------------------------------
 *
 * Role
 *   Declares gem5::o3::PrefetchHintQueue, the microarchitectural structure
 *   that executes the RISC-V custom-0 `HINT.GATHER` / `HINT.GATHER.C`
 *   instruction *outside* the out-of-order issue path.
 *
 *   The whole point of the structure is negative space: a HINT.GATHER
 *   allocates a ROB entry and nothing else. It NEVER allocates an
 *   issue-queue entry, NEVER allocates an LSQ entry, NEVER occupies a
 *   functional-unit / AGU port, and NEVER allocates a physical destination
 *   register. The three counters `iqEntriesAllocated`,
 *   `lsqEntriesAllocated` and `fuPortCycles` exist purely so that the
 *   correctness gate can assert all three are exactly zero. They are
 *   incremented at the *allocation sites themselves* (InstructionQueue,
 *   LSQUnit, FUPool grant), so a zero reading is evidence and not merely
 *   assertion-by-construction.
 *
 * Normative spec
 *   ../../../docs/DESIGN.md
 *     1.1  encoding
 *     1.3  architectural semantics (no faults, no LSQ, no arch state)
 *     1.4  pipeline treatment
 *     2    the Prefetch Hint Queue (entry layout + per-cycle behaviour)
 *     3    the genome (parameter names)
 *     4.2  the gem5 contract (exact stat names -- do not rename)
 *
 * Placement
 *   Copied into a gem5 checkout at <gem5-root>/src/cpu/o3/
 *   prefetch_hint_queue.hh by ../../apply_phq.py. This is a NEW file:
 *   apply_phq.py copies it verbatim and never edits it, so it carries no
 *   BEGIN/END HINT.GATHER markers.
 *
 * API-verification policy
 *   Written against the gem5 v23.x source layout read out of
 *   //third_party/gem5v23 (and cross-checked against //third_party/gem5/
 *   develop, ~v24). Symbols that could not be read directly out of a
 *   checkout carry a `// TODO(verify):` comment naming the exact symbol.
 *   See ../../README.md, section "If the build fails".
 */

#ifndef __CPU_O3_PREFETCH_HINT_QUEUE_HH__
#define __CPU_O3_PREFETCH_HINT_QUEUE_HH__

#include <cstdint>
#include <string>
#include <unordered_map>
#include <vector>

#include "base/statistics.hh"
#include "base/types.hh"
#include "cpu/inst_seq.hh"
#include "cpu/o3/dyn_inst_ptr.hh"
#include "cpu/reg_class.hh"
#include "mem/packet.hh"
#include "mem/request.hh"
#include "params/PrefetchHintQueue.hh"
#include "sim/clocked_object.hh"
#include "sim/eventq.hh"

namespace gem5
{

class BaseMMU;
class ThreadContext;

namespace o3
{

class CPU;

/**
 * The Prefetch Hint Queue.
 *
 * Lives next to the LSQ (DESIGN.md 2) but is deliberately *not* part of it:
 * it borrows the LSQ's D-cache RequestPort to put traffic on the bus and
 * borrows nothing else. In particular it does not participate in
 * store-to-load forwarding, memory disambiguation, or ordering of any kind
 * (DESIGN.md 1.3.3).
 *
 * Per-cycle behaviour (DESIGN.md 2):
 *   1. accept up to `phqDispatchWidth` new entries from dispatch;
 *   2. advance waiting entries per `wakeupPolicy`, dropping any entry whose
 *      poll count exceeds `phqPollLimit`;
 *   3. for READY entries, compute base + (index << shift) on the dedicated
 *      adder and issue `fanout + 1` prefetches, stepping by (1 << shift);
 *   4. drop droppable entries when the L1D is above
 *      `mshrPressureThreshold`.
 *
 * The PHQ is self-clocked: it schedules its own event whenever it has work,
 * so no edit to CPU::tick() is required.
 */
class PrefetchHintQueue : public ClockedObject
{
  public:
    PARAMS(PrefetchHintQueue);

    explicit PrefetchHintQueue(const Params &params);
    ~PrefetchHintQueue() override;

    /** DESIGN.md 2: PHQEntry::state. Two bits. */
    enum class EntryState : uint8_t
    {
        /** Slot is free. */
        Empty = 0,
        /** Waiting for rs1/rs2 to become readable. */
        WaitOperand = 1,
        /** Chase form: the PHQ's own index load is in flight. */
        WaitChase = 2,
        /** Address is computable; prefetches still to be issued. */
        Ready = 3,
    };

    /** DESIGN.md 1.2 / genome key `wakeup_policy`. */
    enum class WakeupPolicy : uint8_t
    {
        /** Poll the scoreboard from the PHQ head. One read port. */
        PollRf = 0,
        /** phqEntries destination-tag comparators on load writeback. */
        TagSnoop = 1,
    };

    /** DESIGN.md 1.3.2 / genome key `tlb_miss_policy`. */
    enum class TlbMissPolicy : uint8_t
    {
        /** No translation -> kill the hint. Never faults. */
        Drop = 0,
        /** Speculative page-table walk, faults suppressed. */
        Walk = 1,
    };

    /** DESIGN.md 1.1 LEVEL / genome key `prefetch_level`. */
    enum class PrefetchLevel : uint8_t
    {
        L1D = 0,
        L2C = 1,
    };

    /**
     * One PHQ entry.
     *
     * DESIGN.md 2 costs the *hardware* struct at ~82 bits. The gem5 model
     * carries extra bookkeeping (full-width InstSeqNum, PhysRegIdPtrs,
     * thread id, issue cursor) that real hardware would not need or would
     * narrow. docs/AREA.md accounts for the hardware struct, not this one.
     * Fields that exist ONLY in the model are marked "[model]".
     */
    struct Entry
    {
        /** rs1 value: base pointer of the gathered array A. 64b. */
        uint64_t base = 0;

        /**
         * Shared storage (DESIGN.md 2). Value form: the index value
         * B[i+d]. Chase form: before the chase load returns this holds the
         * *address* &B[i+d]; afterwards it holds the loaded value.
         */
        uint64_t indexOrAddr = 0;

        /**
         * Sequence number of the dispatching instruction, for squash.
         * Hardware narrows this to 16 bits (DESIGN.md 2); the model keeps
         * full width so squash is exact. [partially model]
         */
        InstSeqNum seqNum = 0;

        /** Index scale: element size is (1 << shift) bytes. 2b. */
        uint8_t shift = 0;
        /** Issue (fanout + 1) prefetches. 3b. */
        uint8_t fanout = 0;
        /** 0 = HINT.GATHER (value), 1 = HINT.GATHER.C (chase). 1b. */
        uint8_t variant = 0;
        /** 0 = prefetch into L1D, 1 = prefetch into L2. 1b. */
        uint8_t level = 0;
        /** 1 = may be dropped under MSHR pressure. 1b. */
        uint8_t droppable = 0;
        /** State machine. 2b. */
        EntryState state = EntryState::Empty;
        /** Cycles this entry has actually been polled. 8b. */
        uint8_t pollCount = 0;

        /** Which of the (fanout + 1) prefetches have been issued. 3b. */
        uint8_t issued = 0;

        /** Original index array virtual address &B[i+d] for multi-target fanout. */
        Addr chaseAddr = 0;
        /** Chased index values B[i+d+0 .. i+d+7] resolved via the 64B ChaseLine buffer. */
        uint64_t chasedIndices[8] = {0};

        /** Owning hardware thread. [model] */
        ThreadID tid = 0;
        /**
         * Renamed physical sources. srcRegIdx(0) is rs1 and srcRegIdx(1) is
         * rs2, because RISC-V operands.isa gives Rs1 sort priority 2 and
         * Rs2 sort priority 3 and the ISA parser orders sources by sort
         * priority. [model]
         */
        PhysRegIdPtr baseReg = nullptr;
        PhysRegIdPtr operandReg = nullptr;

        bool valid() const { return state != EntryState::Empty; }
    };

    /**
     * Sender state attached to every packet the PHQ puts on the wire.
     *
     * This exists because we share the LSQ's D-cache RequestPort, and
     * LSQ::recvTimingResp() does
     *     LSQRequest *r = dynamic_cast<LSQRequest*>(pkt->senderState);
     *     panic_if(!r, "Got packet back with unknown sender state\n");
     * (v23 src/cpu/o3/lsq.cc:409). apply_phq.py therefore inserts an
     * interception region in LSQ::DcachePort::recvTimingResp() that offers
     * every response to PrefetchHintQueue::recvTimingResp() *before* the
     * LSQ ever sees it.
     */
    class SenderState : public Packet::SenderState
    {
      public:
        SenderState(uint64_t _req_id, int _entry_idx, InstSeqNum _seq_num,
                    bool _is_chase_load)
            : reqId(_req_id), entryIdx(_entry_idx), seqNum(_seq_num),
              isChaseLoad(_is_chase_load)
        {}

        /** Monotonic id; the key into `inFlight`. */
        const uint64_t reqId;
        /** Index into `entries`; only meaningful while reqId is live. */
        const int entryIdx;
        /** Sequence number of the hint that produced this request. */
        const InstSeqNum seqNum;
        /** true: chase index load. false: prefetch. */
        const bool isChaseLoad;
    };

    /* ------------------------------------------------------------------ *
     *  Interface used by the patched O3 pipeline (see ../../apply_phq.py)
     * ------------------------------------------------------------------ */

    /** Wire up the owning CPU. Called from o3::CPU's constructor. */
    void setCPU(CPU *_cpu);

    /** Is the PHQ turned on? `--disable-phq` clears this. */
    bool enabled() const { return phqEnabled; }

    /**
     * Offer a dispatched HINT.GATHER to the PHQ.
     *
     * Called from IEW::dispatchInsts(). The caller has fully discharged its
     * responsibility for the hint once this returns: it must mark the
     * instruction issued/executed/canCommit and must NOT insert it into the
     * IQ or the LSQ, whether or not this returns true.
     *
     * @return true if an entry was allocated; false if the hint was dropped
     *         (queue full, dispatch port busy, MSHR pressure, encoding not
     *         recognised, or PHQ disabled). A false return is never an
     *         error: DESIGN.md 1.3.4 makes dropping always architecturally
     *         legal.
     */
    bool dispatchHint(const DynInstPtr &inst);

    /**
     * Invalidate every entry younger than `squashed_seq_num` on `tid`.
     * Called from IEW::squash(). DESIGN.md 1.4: failing to do this is only
     * a wasted prefetch, never a correctness bug, but we model and measure
     * it.
     */
    void squash(InstSeqNum squashed_seq_num, ThreadID tid);

    /**
     * Offered every response seen on the shared LSQ D-cache port.
     *
     * @return true if the packet belonged to the PHQ, in which case the PHQ
     *         has consumed and deleted it and the LSQ must not touch it
     *         again; false if it is an ordinary LSQ response, in which case
     *         the PHQ has only *observed* it (for `prefetchesLate`).
     */
    bool recvTimingResp(PacketPtr pkt);

    /* ---- structural assertions (DESIGN.md 4.2) ----------------------- */

    /** Called from InstructionQueue::insert() / insertNonSpec(). */
    void noteIqAllocation(const DynInstPtr &inst);
    /** Called from LSQUnit::insert(). */
    void noteLsqAllocation(const DynInstPtr &inst);
    /** Called from InstructionQueue::issueInsts() at the FU grant site. */
    void noteFuPortCycle(const DynInstPtr &inst);
    /**
     * Called from ROB::insertInst().
     *
     * This is the *positive* control for the three counters above. A run in
     * which iqEntriesAllocated == lsqEntriesAllocated == fuPortCycles == 0
     * proves nothing on its own -- it is also what you get if the hints were
     * silently dropped at decode and never entered the pipeline at all.
     * robEntriesAllocated > 0 alongside those three zeroes is the actual
     * structural claim of DESIGN.md 1.3: the instruction *did* flow through
     * rename and *did* take a ROB entry, and took nothing else.
     */
    void noteRobAllocation(const DynInstPtr &inst);
    /** Called from Rename::renameInsts(). Panics if an invariant breaks. */
    void checkRenameInvariants(const DynInstPtr &inst);

  private:
    /* ------------------------------------------------------------------ *
     *  Per-cycle engine
     * ------------------------------------------------------------------ */

    /** One PHQ cycle: wake up, compute, issue, retire. */
    void tick();

    /** Ensure tick() runs next cycle if there is anything to do. */
    void scheduleTick();

    /** Step 2 of DESIGN.md 2: advance waiting entries. */
    void advanceWaitingEntries();

    /** Step 3 of DESIGN.md 2: compute on the dedicated adder and issue. */
    void issueReadyEntries();

    /** Index of the oldest entry in `state`, or -1. */
    int oldestInState(EntryState state) const;

    /** True if the physical register is readable right now. */
    bool operandReady(PhysRegIdPtr reg) const;

    /** Read a physical register through the CPU's register file. */
    uint64_t readOperand(PhysRegIdPtr reg, ThreadID tid) const;

    /** Free an entry (no statistic). */
    void releaseEntry(int idx);
    /** Free an entry and charge the drop to `reason`. */
    void dropEntry(int idx, statistics::Scalar &reason);

    /** Kick off the chase load for entries[idx] (variant 1). */
    bool startChaseLoad(int idx);

    /** Issue fan-out prefetch number `k` for entries[idx]. */
    bool issuePrefetch(int idx, uint8_t k);

    /**
     * Build + translate + send one request. Never produces a fault
     * (DESIGN.md 1.3.2): a failed translation is handled by
     * `tlbMissPolicy` and kills the hint instead of raising anything.
     *
     * @return true if the packet left the PHQ.
     */
    bool sendRequest(Addr vaddr, unsigned size, bool is_chase_load,
                     int entry_idx);

    /**
     * Translate `vaddr` with no architectural side effect.
     * @return true on success.
     */
    bool translateNoFault(const RequestPtr &req, ThreadID tid);

    /** True if the L1D looks too busy to accept a droppable hint. */
    bool underMshrPressure() const;

    /** Number of occupied entries. */
    unsigned occupancy() const;

    /** Cache-block-align. */
    Addr blockAlign(Addr a) const { return a & ~(Addr)(blkSize - 1); }

    /** Record a demand response for the prefetchesLate statistic. */
    void observeDemandResponse(PacketPtr pkt);

    /* ------------------------------------------------------------------ *
     *  State
     * ------------------------------------------------------------------ */

    /** Owning CPU. Set by setCPU(); null until then. */
    CPU *cpu = nullptr;

    /** The entry array. Size is `phqEntries` (genome: phq_entries). */
    std::vector<Entry> entries;

    /* -- genome-derived configuration (DESIGN.md 3) -------------------- */
    const bool phqEnabled;
    const unsigned phqEntries;
    const unsigned phqDispatchWidth;
    const unsigned phqPollLimit;
    const double mshrPressureThreshold;
    WakeupPolicy wakeupPolicy;
    TlbMissPolicy tlbMissPolicy;
    PrefetchLevel prefetchLevel;

    /* -- microarchitectural configuration (not in the genome) ---------- *
     * These are plain gem5 params with conservative defaults; the CHIA
     * loop does not tune them. They exist so the structural costs claimed
     * in docs/AREA.md are actually enforced by the model.
     * ------------------------------------------------------------------ */
    /**
     * Scoreboard read ports available to `poll_rf`. DESIGN.md 1.2 costs
     * poll_rf at exactly "one scoreboard read port", so the default is 1:
     * only the oldest waiting entry ("the PHQ head") is polled per cycle.
     */
    const unsigned scoreboardReadPorts;
    /** Adds per cycle on the dedicated adder. DESIGN.md 1.4 / 2. */
    const unsigned adderThroughput;
    /**
     * Number of L1D MSHRs. The config script must keep this equal to
     * `cpu.dcache.mshrs`; BaseCache::mshrQueue is protected and there is no
     * portable way for a CPU-side object holding only a RequestPort to
     * interrogate the cache. See README, "MSHR pressure is approximated".
     */
    const unsigned l1dMshrs;

    /** Cache block size, taken from the CPU in setCPU(). */
    unsigned blkSize = 64;

    /* -- runtime bookkeeping ------------------------------------------- */

    /** Entries accepted from dispatch so far this cycle. */
    unsigned dispatchedThisCycle = 0;
    /** Tick for which `dispatchedThisCycle` is valid. */
    Tick dispatchWindowTick = MaxTick;

    /** Monotonic request id generator. Never reused. */
    uint64_t nextReqId = 1;

    /** A request currently on the wire. */
    struct InFlight
    {
        int entryIdx = -1;
        InstSeqNum seqNum = 0;
        Addr blkAddr = 0;
        bool isChaseLoad = false;
        Tick issueTick = 0;
        bool squashed = false;
    };
    std::unordered_map<uint64_t, InFlight> inFlight;

    /**
     * Block addresses with a PHQ request in flight, refcounted. This is
     * both the `prefetchesLate` detector and the MSHR-pressure proxy.
     */
    std::unordered_map<Addr, unsigned> inFlightBlocks;

    /**
     * 4-Entry (256-byte) Index Chase Line Buffer: caches 64-byte cache blocks
     * of the index array B[] so consecutive HINT.GATHER.C instructions resolve
     * B[i+d+k] in 0 cycles without issuing 16 separate L1D port requests per
     * 64-byte line.
     */
    struct ChaseLine
    {
        Addr vblk = 0;
        uint8_t data[64] = {0};
        bool valid = false;
        Tick lastUse = 0;
    };
    ChaseLine chaseLines[4];

    /**
     * 128-Entry Recent Prefetch Cache-Block Filter: suppresses redundant
     * SoftPFReq packets to 64B cache lines of A[] that were recently prefetched.
     */
    Addr recentPfRing[128] = {0};
    unsigned recentPfHead = 0;
    bool largeWorkingSet = false;

    /** Self-clocking event; the PHQ is not ticked by CPU::tick(). */
    EventFunctionWrapper tickEvent;

    /** Requestor id used for all PHQ traffic (the CPU's data requestor). */
    RequestorID requestorId = Request::invldRequestorId;

    /* ------------------------------------------------------------------ *
     *  Statistics -- DESIGN.md 4.2. THE NAMES ARE PART OF THE CONTRACT.
     *  The CHIA loop greps for `system.cpu.phq.<name>`; do not rename.
     *  Because PrefetchHintQueue is a SimObject assigned to `cpu.phq` in
     *  the config, gem5's _bindStatHierarchy() gives this group the path
     *  `system.cpu.phq` automatically.
     * ------------------------------------------------------------------ */
    struct PHQStats : public statistics::Group
    {
        explicit PHQStats(statistics::Group *parent);

        /** Hints that allocated a PHQ entry. */
        statistics::Scalar hintsDispatched;

        /** Total drops. Formula over the four reasons below. */
        statistics::Formula hintsDropped;
        /** Dropped because the L1D was above mshr_pressure_threshold. */
        statistics::Scalar hintsDroppedMshr;
        /** Dropped because the operand never became ready in time. */
        statistics::Scalar hintsDroppedTimeout;
        /** Invalidated by a pipeline squash. */
        statistics::Scalar hintsDroppedSquash;
        /** Rejected at dispatch: no free entry or dispatch port busy. */
        statistics::Scalar hintsDroppedFull;

        /** Prefetch requests that actually left the PHQ. */
        statistics::Scalar prefetchesIssued;
        /**
         * Prefetches still in flight when a demand response for the same
         * cache block came back on the shared D-cache port. See README for
         * the precise definition and why it differs from the classic
         * cache-side pfLate.
         */
        statistics::Scalar prefetchesLate;

        /** Time-weighted mean number of occupied entries. */
        statistics::Average occupancyAvg;

        /* ---- structural assertions. MUST all read exactly 0. -------- */
        /** Issue-queue entries allocated by a HINT.GATHER. */
        statistics::Scalar iqEntriesAllocated;
        /** LSQ entries allocated by a HINT.GATHER. */
        statistics::Scalar lsqEntriesAllocated;
        /** Functional-unit port cycles consumed by a HINT.GATHER. */
        statistics::Scalar fuPortCycles;
        /**
         * ROB entries allocated by a HINT.GATHER. Positive control: this
         * MUST be > 0 in any run that executed hints, otherwise the three
         * zeroes above are vacuous.
         */
        statistics::Scalar robEntriesAllocated;

        /* ---- diagnostics (not part of the 4.2 contract) ------------- */
        /** Chase loads issued by the PHQ itself (variant 1). */
        statistics::Scalar chaseLoadsIssued;
        /** Hints observed at dispatch whose ISA payload was missing. */
        statistics::Scalar hintsUndecodable;
        /** Translations that produced no mapping. */
        statistics::Scalar translationFailures;
        /** Times the shared D-cache port refused a PHQ request. */
        statistics::Scalar portBlocked;
        /** Prefetches requested at L2 (see README: level is advisory). */
        statistics::Scalar prefetchesRequestedL2;
    } phqStats;
};

} // namespace o3
} // namespace gem5

#endif // __CPU_O3_PREFETCH_HINT_QUEUE_HH__
