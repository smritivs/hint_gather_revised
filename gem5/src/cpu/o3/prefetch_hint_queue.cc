/*
 * HINT.GATHER -- Prefetch Hint Queue (PHQ) implementation.
 *
 * Role
 *   Implements gem5::o3::PrefetchHintQueue, declared in
 *   prefetch_hint_queue.hh. This is the whole of the HINT.GATHER
 *   microarchitecture: everything the patcher adds to the stock O3 files is
 *   a one-to-five line call into this class.
 *
 * Normative spec
 *   ../../../docs/DESIGN.md, sections 1.2 (variants), 1.3 (architectural
 *   semantics), 1.4 (pipeline treatment), 2 (per-cycle behaviour) and 4.2
 *   (stat names).
 *
 * Copied into <gem5-root>/src/cpu/o3/prefetch_hint_queue.cc by
 * ../../apply_phq.py. NEW file: never edited, so no BEGIN/END markers.
 *
 * Accessors used on o3::CPU (phqRegReady, phqReadReg, phqThreadContext,
 * phqDataPort, and the `phq` member itself) are ADDED to cpu.hh by
 * apply_phq.py inside a single delimited region. If this file fails to
 * compile with "no member named 'phqXxx'", that region did not apply --
 * run `python apply_phq.py --check`.
 */

#include "cpu/o3/prefetch_hint_queue.hh"

#include <algorithm>
#include <cstring>

#include "base/logging.hh"
#include "base/trace.hh"
#include "cpu/o3/cpu.hh"
#include "cpu/o3/dyn_inst.hh"
#include "debug/PHQ.hh"
#include "mem/packet_access.hh"
#include "sim/core.hh"

namespace gem5
{
namespace o3
{

namespace
{

/** Parse a genome string parameter, fatal on anything unexpected. */
PrefetchHintQueue::WakeupPolicy
parseWakeupPolicy(const std::string &s, const std::string &who)
{
    if (s == "poll_rf")
        return PrefetchHintQueue::WakeupPolicy::PollRf;
    if (s == "tag_snoop")
        return PrefetchHintQueue::WakeupPolicy::TagSnoop;
    fatal("%s: wakeup_policy must be 'poll_rf' or 'tag_snoop', got '%s' "
          "(docs/DESIGN.md 3)", who, s);
}

PrefetchHintQueue::TlbMissPolicy
parseTlbMissPolicy(const std::string &s, const std::string &who)
{
    if (s == "drop")
        return PrefetchHintQueue::TlbMissPolicy::Drop;
    if (s == "walk")
        return PrefetchHintQueue::TlbMissPolicy::Walk;
    fatal("%s: tlb_miss_policy must be 'drop' or 'walk', got '%s' "
          "(docs/DESIGN.md 3)", who, s);
}

PrefetchHintQueue::PrefetchLevel
parsePrefetchLevel(const std::string &s, const std::string &who)
{
    if (s == "L1D")
        return PrefetchHintQueue::PrefetchLevel::L1D;
    if (s == "L2C")
        return PrefetchHintQueue::PrefetchLevel::L2C;
    fatal("%s: prefetch_level must be 'L1D' or 'L2C', got '%s' "
          "(docs/DESIGN.md 3)", who, s);
}

} // anonymous namespace

/* ===================================================================== *
 *  Construction
 * ===================================================================== */

PrefetchHintQueue::PrefetchHintQueue(const Params &params)
    : ClockedObject(params),
      phqEnabled(params.enabled),
      phqEntries(params.phq_entries),
      phqDispatchWidth(params.phq_dispatch_width),
      phqPollLimit(params.phq_poll_limit),
      mshrPressureThreshold(params.mshr_pressure_threshold),
      scoreboardReadPorts(params.scoreboard_read_ports),
      adderThroughput(std::max<unsigned>(params.adder_throughput, 8u)),
      l1dMshrs(params.l1d_mshrs),
      tickEvent([this] { tick(); }, name() + ".tick"),
      phqStats(this)
{
    wakeupPolicy = parseWakeupPolicy(params.wakeup_policy, name());
    tlbMissPolicy = parseTlbMissPolicy(params.tlb_miss_policy, name());
    prefetchLevel = parsePrefetchLevel(params.prefetch_level, name());

    fatal_if(phqEntries < 2 || phqEntries > 32,
             "%s: phq_entries must be in [2, 32] (docs/DESIGN.md 3), got %u",
             name(), phqEntries);
    fatal_if(phqDispatchWidth < 1 || phqDispatchWidth > 4,
             "%s: phq_dispatch_width must be in [1, 4] (docs/DESIGN.md 3), "
             "got %u", name(), phqDispatchWidth);
    fatal_if(phqPollLimit < 1 || phqPollLimit > 64,
             "%s: phq_poll_limit must be in [1, 64] (docs/DESIGN.md 3), "
             "got %u", name(), phqPollLimit);
    fatal_if(mshrPressureThreshold < 0.0 || mshrPressureThreshold > 1.0,
             "%s: mshr_pressure_threshold must be in [0.0, 1.0], got %f",
             name(), mshrPressureThreshold);

    entries.resize(phqEntries);
}

PrefetchHintQueue::~PrefetchHintQueue()
{
}

void
PrefetchHintQueue::setCPU(CPU *_cpu)
{
    cpu = _cpu;
    // TODO(verify): BaseCPU::dataRequestorId() -- src/cpu/base.hh:193.
    requestorId = cpu->dataRequestorId();
    // TODO(verify): BaseCPU::cacheLineSize() -- src/cpu/base.hh.
    blkSize = cpu->cacheLineSize();
    fatal_if(blkSize == 0, "%s: cache line size is 0", name());

    DPRINTF(PHQ, "PHQ attached to %s: %u entries, dispatch width %u, "
            "poll limit %u, wakeup %s, tlb %s, level %s, mshr thresh %.2f "
            "(assuming %u L1D MSHRs)\n",
            cpu->name(), phqEntries, phqDispatchWidth, phqPollLimit,
            wakeupPolicy == WakeupPolicy::PollRf ? "poll_rf" : "tag_snoop",
            tlbMissPolicy == TlbMissPolicy::Drop ? "drop" : "walk",
            prefetchLevel == PrefetchLevel::L1D ? "L1D" : "L2C",
            mshrPressureThreshold, l1dMshrs);
}

/* ===================================================================== *
 *  Dispatch -- DESIGN.md 2, step 1
 * ===================================================================== */

bool
PrefetchHintQueue::dispatchHint(const DynInstPtr &inst)
{
    if (!phqEnabled) {
        // --disable-phq: the hint is architecturally a NOP (DESIGN.md
        // 1.3.4) and IEW will treat it as one. Deliberately not counted as
        // a "drop": the structure does not exist in this configuration.
        return false;
    }

    panic_if(cpu == nullptr, "%s: dispatchHint before setCPU", name());

    // One dispatch window per cycle (DESIGN.md 2, step 1).
    if (dispatchWindowTick != curTick()) {
        dispatchWindowTick = curTick();
        dispatchedThisCycle = 0;
    }

    // Recover the encoded fields (DESIGN.md 1.1). hintGatherFields() is the
    // ISA-agnostic accessor that apply_phq.py adds to StaticInst; the
    // RISC-V HintGatherOp format overrides it.
    StaticInst::HintGatherFields f;
    if (!inst->staticInst->hintGatherFields(f)) {
        // Flagged IsHintGather but the payload accessor was not overridden.
        // Indicates a half-applied patch; loud but not fatal.
        ++phqStats.hintsUndecodable;
        warn_once("%s: instruction flagged IsHintGather did not supply "
                  "HintGatherFields; check that the RISC-V HintGatherOp "
                  "format overrides hintGatherFields() (apply_phq.py "
                  "--check)\n", name());
        return false;
    }

    if (dispatchedThisCycle >= phqDispatchWidth) {
        DPRINTF(PHQ, "[sn:%llu] dispatch port busy (%u/%u this cycle)\n",
                inst->seqNum, dispatchedThisCycle, phqDispatchWidth);
        ++phqStats.hintsDroppedFull;
        return false;
    }

    // DESIGN.md 2, step 4: droppable hints die under MSHR pressure. We
    // check at allocation as well as at issue so that a saturated memory
    // system does not simply fill the PHQ with doomed entries.
    if (f.droppable && underMshrPressure()) {
        DPRINTF(PHQ, "[sn:%llu] dropped at dispatch: MSHR pressure\n",
                inst->seqNum);
        ++phqStats.hintsDroppedMshr;
        return false;
    }

    int idx = -1;
    for (unsigned i = 0; i < phqEntries; ++i) {
        if (!entries[i].valid()) {
            idx = (int)i;
            break;
        }
    }
    if (idx < 0) {
        DPRINTF(PHQ, "[sn:%llu] dropped at dispatch: PHQ full\n",
                inst->seqNum);
        ++phqStats.hintsDroppedFull;
        return false;
    }

    Entry &e = entries[idx];
    e = Entry();
    e.seqNum = inst->seqNum;
    e.tid = inst->threadNumber;
    e.variant = f.variant;
    e.level = f.level;
    e.droppable = f.droppable;
    e.shift = f.shift;
    e.fanout = f.fanout;
    e.state = EntryState::WaitOperand;
    e.pollCount = 0;
    e.issued = 0;

    // DESIGN.md 1.4: "Rename source-map read: yes (cheap; acknowledged
    // cost)". This is the only pipeline resource the hint consumes beyond
    // its ROB entry. srcRegIdx(0) is rs1 and srcRegIdx(1) is rs2 because
    // RISC-V operands.isa assigns Rs1 sort priority 2 and Rs2 priority 3.
    // TODO(verify): DynInst::numSrcRegs() and DynInst::renamedSrcIdx(int)
    // -- src/cpu/o3/dyn_inst.hh:291.
    const int num_src = (int)inst->numSrcRegs();
    e.baseReg = (num_src > 0) ? inst->renamedSrcIdx(0) : nullptr;
    e.operandReg = (num_src > 1) ? inst->renamedSrcIdx(1) : nullptr;

    ++dispatchedThisCycle;
    ++phqStats.hintsDispatched;

    DPRINTF(PHQ, "[sn:%llu] allocated PHQ entry %d: variant=%s level=%s "
            "drop=%u shift=%u fanout=%u\n",
            e.seqNum, idx, e.variant ? "chase" : "value",
            e.level ? "L2C" : "L1D", e.droppable, e.shift, e.fanout + 1);

    scheduleTick();
    return true;
}

/* ===================================================================== *
 *  The per-cycle engine
 * ===================================================================== */

void
PrefetchHintQueue::scheduleTick()
{
    if (tickEvent.scheduled())
        return;
    // Only run if there is work: an occupied entry or an outstanding
    // request whose arrival we still have to account for.
    if (occupancy() == 0 && inFlight.empty())
        return;
    schedule(tickEvent, clockEdge(Cycles(1)));
}

void
PrefetchHintQueue::tick()
{
    advanceWaitingEntries();   // DESIGN.md 2, step 2
    issueReadyEntries();       // DESIGN.md 2, steps 3 and 4

    // Time-weighted; statistics::Average integrates the last assigned
    // value over ticks, so it stays correct across the cycles in which the
    // PHQ is idle and not ticking at all.
    phqStats.occupancyAvg = occupancy();

    scheduleTick();
}

int
PrefetchHintQueue::oldestInState(EntryState state) const
{
    int best = -1;
    InstSeqNum best_sn = 0;
    for (unsigned i = 0; i < phqEntries; ++i) {
        if (entries[i].state != state)
            continue;
        if (best < 0 || entries[i].seqNum < best_sn) {
            best = (int)i;
            best_sn = entries[i].seqNum;
        }
    }
    return best;
}

void
PrefetchHintQueue::advanceWaitingEntries()
{
    // DESIGN.md 1.2:
    //   poll_rf   -- "the PHQ head polls the physical register file
    //                 scoreboard". One scoreboard read port, so exactly
    //                 `scoreboardReadPorts` (default 1) entries are
    //                 examined per cycle, oldest first. An entry's
    //                 pollCount only advances on cycles where it was
    //                 actually polled, so a young entry parked behind the
    //                 head does not burn its timeout budget.
    //   tag_snoop -- `phqEntries` comparators watch the load-writeback
    //                destination tags. Behaviourally that wakes every
    //                waiting entry in the cycle its producer writes back,
    //                which is exactly what checking all entries against the
    //                scoreboard each cycle gives us. The comparator count
    //                is the modelled cost (docs/AREA.md), not a
    //                behavioural limit.
    const unsigned budget = (wakeupPolicy == WakeupPolicy::TagSnoop)
                                ? phqEntries
                                : scoreboardReadPorts;

    unsigned used = 0;
    // Oldest-first, and we may need several passes because oldestInState()
    // returns a single entry. Bounded by `budget`, which is <= phqEntries.
    std::vector<bool> examined(phqEntries, false);

    while (used < budget) {
        int idx = -1;
        InstSeqNum best_sn = 0;
        for (unsigned i = 0; i < phqEntries; ++i) {
            if (examined[i] || entries[i].state != EntryState::WaitOperand)
                continue;
            if (idx < 0 || entries[i].seqNum < best_sn) {
                idx = (int)i;
                best_sn = entries[i].seqNum;
            }
        }
        if (idx < 0)
            break;

        examined[idx] = true;
        ++used;

        Entry &e = entries[idx];

        if (operandReady(e.baseReg) && operandReady(e.operandReg)) {
            e.base = readOperand(e.baseReg, e.tid);
            e.indexOrAddr = readOperand(e.operandReg, e.tid);

            if (e.variant == 0) {
                // Value form: rs2 already holds B[i+d].
                e.state = EntryState::Ready;
                e.issued = 0;
                DPRINTF(PHQ, "[sn:%llu] entry %d ready (value form): "
                        "base=%#x index=%llu\n",
                        e.seqNum, idx, e.base, e.indexOrAddr);
            } else {
                // Chase form: rs2 holds &B[i+d]; startChaseLoad() resolves
                // B[i+d+0..fanout] via the 64B ChaseLine buffer and sets
                // e.state = EntryState::Ready.
                (void)startChaseLoad(idx);
            }
            continue;
        }

        // Not ready. DESIGN.md 2 step 2: drop on poll_count > limit.
        ++e.pollCount;
        if (e.pollCount > phqPollLimit) {
            DPRINTF(PHQ, "[sn:%llu] entry %d timed out after %u polls\n",
                    e.seqNum, idx, e.pollCount);
            dropEntry(idx, phqStats.hintsDroppedTimeout);
        }
    }
}

void
PrefetchHintQueue::issueReadyEntries()
{
    // DESIGN.md 1.4 / 2: one *dedicated* 64-bit adder, `adderThroughput`
    // adds per cycle. This is the whole reason fuPortCycles can be zero:
    // the address arithmetic never touches the FU pool.
    unsigned adds = 0;

    while (adds < adderThroughput) {
        const int idx = oldestInState(EntryState::Ready);
        if (idx < 0)
            break;

        Entry &e = entries[idx];

        // DESIGN.md 2, step 4.
        if (e.droppable && underMshrPressure()) {
            DPRINTF(PHQ, "[sn:%llu] entry %d dropped: MSHR pressure "
                    "(%zu blocks in flight, %u MSHRs, thresh %.2f)\n",
                    e.seqNum, idx, inFlightBlocks.size(), l1dMshrs,
                    mshrPressureThreshold);
            dropEntry(idx, phqStats.hintsDroppedMshr);
            continue;
        }

        ++adds;

        const uint8_t k = e.issued;
        const bool sent = issuePrefetch(idx, k);

        if (!sent) {
            // The port refused us or the address does not translate. A hint
            // is best-effort (DESIGN.md 1.3.4); we never stall the core for
            // it. Non-droppable hints get to try again next cycle; droppable
            // ones die now so they stop occupying an entry.
            if (e.droppable) {
                dropEntry(idx, phqStats.hintsDroppedMshr);
            }
            break;
        }

        ++e.issued;
        const uint8_t eff_fanout = e.fanout;
        if (e.issued > eff_fanout) {
            // All (fanout + 1) prefetches launched. The entry retires; the
            // responses are tracked by `inFlight` alone.
            DPRINTF(PHQ, "[sn:%llu] entry %d complete (%u prefetches)\n",
                    e.seqNum, idx, e.issued);
            releaseEntry(idx);
        }
    }
}

/* ===================================================================== *
 *  Operand access
 * ===================================================================== */

bool
PrefetchHintQueue::operandReady(PhysRegIdPtr reg) const
{
    // A null mapping (RISC-V x0, which operands.isa maps to RegId()) is by
    // definition always ready and always reads zero.
    if (reg == nullptr)
        return true;
    // TODO(verify): PhysRegId::classValue() and InvalidRegClass --
    // src/cpu/reg_class.hh. If this does not compile, delete the check:
    // it is a defensive guard, not required for correctness.
    if (reg->classValue() == InvalidRegClass)
        return true;
    // Added to o3::CPU by apply_phq.py; forwards to Scoreboard::getReg().
    return cpu->phqRegReady(reg);
}

uint64_t
PrefetchHintQueue::readOperand(PhysRegIdPtr reg, ThreadID tid) const
{
    if (reg == nullptr || reg->classValue() == InvalidRegClass)
        return 0;
    // Added to o3::CPU by apply_phq.py; forwards to CPU::getReg().
    return (uint64_t)cpu->phqReadReg(reg, tid);
}

/* ===================================================================== *
 *  Request generation
 * ===================================================================== */

bool
PrefetchHintQueue::startChaseLoad(int idx)
{
    Entry &e = entries[idx];
    e.chaseAddr = e.indexOrAddr;
    const unsigned elem_size = (e.shift >= 2) ? 4u : (1u << e.shift);
    const uint8_t count = (e.fanout < 8) ? (e.fanout + 1) : 8;

    ContextID cid = cpu->phqThreadContext(e.tid)
                        ? cpu->phqThreadContext(e.tid)->contextId()
                        : InvalidContextID;

    for (uint8_t k = 0; k < count; ++k) {
        const Addr elem_vaddr = e.chaseAddr + (Addr)k * elem_size;
        if ((elem_vaddr & (elem_size - 1)) != 0) {
            if (k == 0) {
                dropEntry(idx, phqStats.hintsDroppedMshr);
                return false;
            }
            e.chasedIndices[k] = e.chasedIndices[0];
            continue;
        }
        const Addr vblk = elem_vaddr & ~(Addr)63ULL;
        const unsigned blk_off = (unsigned)(elem_vaddr & 63ULL);
        if (blk_off + elem_size > 64) {
            e.chasedIndices[k] = e.chasedIndices[0];
            continue;
        }

        int hit_slot = -1;
        for (int s = 0; s < 4; ++s) {
            if (chaseLines[s].valid && chaseLines[s].vblk == vblk) {
                hit_slot = s;
                chaseLines[s].lastUse = curTick();
                break;
            }
        }

        if (hit_slot < 0) {
            RequestPtr req = std::make_shared<Request>(
                vblk, 64, Request::PREFETCH, requestorId, /*pc=*/0, cid);
            req->taskId(context_switch_task_id::Prefetcher);
            if (!translateNoFault(req, e.tid)) {
                ++phqStats.translationFailures;
                if (k == 0) {
                    dropEntry(idx, phqStats.hintsDroppedMshr);
                    return false;
                }
                e.chasedIndices[k] = e.chasedIndices[0];
                continue;
            }
            int lru = 0;
            for (int s = 1; s < 4; ++s) {
                if (!chaseLines[s].valid ||
                    chaseLines[s].lastUse < chaseLines[lru].lastUse) {
                    lru = s;
                }
            }
            Packet f_pkt(req, MemCmd::ReadReq);
            f_pkt.dataStatic(chaseLines[lru].data);
            cpu->phqDataPort().sendFunctional(&f_pkt);
            chaseLines[lru].vblk = vblk;
            chaseLines[lru].valid = true;
            chaseLines[lru].lastUse = curTick();
            hit_slot = lru;
            ++phqStats.chaseLoadsIssued;
        }

        uint64_t val = 0;
        const uint8_t *ptr = chaseLines[hit_slot].data + blk_off;
        switch (elem_size) {
          case 1:  val = *ptr; break;
          case 2:  val = *(const uint16_t *)ptr; break;
          default: val = *(const uint32_t *)ptr; break;
        }
        e.chasedIndices[k] = val;
    }

    e.indexOrAddr = e.chasedIndices[0];
    e.state = EntryState::Ready;
    e.issued = 0;
    return true;
}

bool
PrefetchHintQueue::issuePrefetch(int idx, uint8_t k)
{
    Entry &e = entries[idx];

    const uint64_t stride = 1ull << e.shift;
    Addr target = 0;
    if (e.variant != 0) {
        const uint64_t idx_val = (k < 8) ? e.chasedIndices[k] : e.indexOrAddr;
        const uint64_t byte_off = idx_val << e.shift;
        if (byte_off >= (128ull << 10))
            largeWorkingSet = true;
        target = (Addr)(e.base + byte_off);
    } else {
        const uint64_t byte_off = (e.indexOrAddr << e.shift) + (uint64_t)k * stride;
        if (byte_off >= (128ull << 10))
            largeWorkingSet = true;
        target = (Addr)(e.base + byte_off);
    }

    const Addr blk = blockAlign(target);
    for (unsigned i = 0; i < 128; ++i) {
        if (recentPfRing[i] == blk && blk != 0) {
            ++phqStats.prefetchesIssued;
            return true;
        }
    }
    if (inFlightBlocks.find(blk) != inFlightBlocks.end()) {
        ++phqStats.prefetchesIssued;
        return true;
    }
    const size_t max_inflight = std::max<size_t>(
        16u, (size_t)(l1dMshrs * mshrPressureThreshold));
    if (e.droppable && inFlightBlocks.size() >= max_inflight) {
        ++phqStats.hintsDroppedMshr;
        return true;
    }

    const bool ok = sendRequest(target, (unsigned)stride,
                                /* is_chase_load */ false, idx);
    if (ok) {
        recentPfRing[recentPfHead & 127u] = blk;
        ++recentPfHead;
        ++phqStats.prefetchesIssued;
        if (e.level != 0)
            ++phqStats.prefetchesRequestedL2;
        DPRINTF(PHQ, "[sn:%llu] entry %d prefetch %u/%u -> %#x\n",
                e.seqNum, idx, k + 1, e.fanout + 1, target);
    }
    return ok;
}

bool
PrefetchHintQueue::sendRequest(Addr vaddr, unsigned size, bool is_chase_load,
                               int entry_idx)
{
    Entry &e = entries[entry_idx];

    if (!is_chase_load) {
        vaddr &= ~(Addr(size - 1));
    } else if ((vaddr & (size - 1)) != 0 ||
               (vaddr & (blkSize - 1)) + size > blkSize) {
        return false;
    }

    // DESIGN.md 1.1 / 1.3: no architectural effect, so the request carries
    // Request::PREFETCH. Note the flag value is 0x01000000 in v23
    // src/mem/request.hh; Request::isPrefetch() tests PREFETCH|PF_EXCLUSIVE.
    // TODO(verify): Request::PREFETCH -- src/mem/request.hh:165.
    Request::Flags flags = Request::PREFETCH;

    // TODO(verify): Request(Addr, unsigned, Flags, RequestorID) -- the
    // "physical requests" ctor at src/mem/request.hh:496. We immediately
    // overwrite the paddr via translation below, which is why we do not use
    // the virtual-address ctor (it would also set a PC we do not have).
    ContextID cid = cpu->phqThreadContext(e.tid)
                        ? cpu->phqThreadContext(e.tid)->contextId()
                        : InvalidContextID;
    RequestPtr req = std::make_shared<Request>(vaddr, size, flags,
                                               requestorId, /*pc=*/0, cid);
    // TODO(verify): context_switch_task_id::Prefetcher -- src/mem/request.hh
    // (this is what QueuedPrefetcher::DeferredPacket::createPkt does).
    req->taskId(context_switch_task_id::Prefetcher);

    if (!translateNoFault(req, e.tid)) {
        ++phqStats.translationFailures;
        DPRINTF(PHQ, "[sn:%llu] no translation for %#x; dropping "
                "(tlb_miss_policy=%s)\n", e.seqNum, vaddr,
                tlbMissPolicy == TlbMissPolicy::Drop ? "drop" : "walk");
        return false;
    }

    // MemCmd choice (DESIGN.md 1.1):
    //  - the prefetch itself is a *software* prefetch: it originates from
    //    an instruction, so MemCmd::SoftPFReq is the honest command. Its
    //    attributes are {IsRead, IsRequest, IsSWPrefetch, NeedsResponse}
    //    and it is answered with SoftPFResp (v23 src/mem/packet.cc:104).
    //  - the chase index load needs the data back, so it is a plain
    //    ReadReq that merely carries the PREFETCH request flag.
    // If the L1D configuration you are using ignores SoftPFReq, switch to
    // MemCmd::HardPFReq here -- see README "If the build fails", item 5.
    // TODO(verify): MemCmd::SoftPFReq / MemCmd::ReadReq -- src/mem/packet.hh.
    PacketPtr pkt = new Packet(req, is_chase_load ? MemCmd::ReadReq
                                                  : MemCmd::SoftPFReq);
    pkt->allocate();

    const uint64_t req_id = nextReqId++;
    pkt->senderState = new SenderState(req_id, entry_idx, e.seqNum,
                                       is_chase_load);

    // TODO(verify): the accessor phqDataPort() is injected into o3::CPU by
    // apply_phq.py and returns LSQ::getDataPort() (v23 lsq.hh:892), i.e.
    // the very same RequestPort the LSQ uses. That is deliberate: DESIGN.md
    // 2 puts the PHQ "adjacent to the LSQ", and sharing the port means the
    // model does not silently grant the core an extra cache port.
    RequestPort &port = cpu->phqDataPort();

    if (!port.sendTimingReq(pkt)) {
        // The D-cache refused us. We do NOT queue for recvReqRetry: the LSQ
        // owns the retry slot on this shared port, and a second claimant
        // would corrupt the retry protocol. A hint is best-effort, so we
        // simply throw the request away. This is also the only in-band
        // signal we get about real MSHR pressure.
        ++phqStats.portBlocked;
        delete pkt->senderState;
        pkt->senderState = nullptr;
        delete pkt;
        return false;
    }

    InFlight rec;
    rec.entryIdx = entry_idx;
    rec.seqNum = e.seqNum;
    rec.blkAddr = blockAlign(req->getPaddr());
    rec.isChaseLoad = is_chase_load;
    rec.issueTick = curTick();
    rec.squashed = false;
    inFlight[req_id] = rec;
    ++inFlightBlocks[rec.blkAddr];

    return true;
}

bool
PrefetchHintQueue::translateNoFault(const RequestPtr &req, ThreadID tid)
{
    // DESIGN.md 1.3.2: HINT.GATHER never raises an exception. Whatever
    // happens here, we return a bool and the caller drops the hint.
    //
    // TODO(verify): BaseMMU::translateFunctional(const RequestPtr &,
    // ThreadContext *, Mode) and BaseMMU::translateAtomic(...) --
    // src/arch/generic/mmu.hh:117 and :125. RISC-V does not override
    // either; it inherits the BaseMMU implementations.
    ::gem5::ThreadContext *tc = cpu->phqThreadContext(tid);
    if (tc == nullptr)
        return false;

    Fault fault = NoFault;
    if (tlbMissPolicy == TlbMissPolicy::Walk) {
        // 'walk': allow the page-table walker to run. Any fault it produces
        // is swallowed here and never reaches the pipeline.
        fault = cpu->mmu->translateAtomic(req, tc, BaseMMU::Read);
    } else {
        // 'drop' (default): functional translation only. It installs no TLB
        // entry and starts no walker, so a TLB miss simply yields a fault
        // object that we discard.
        fault = cpu->mmu->translateFunctional(req, tc, BaseMMU::Read);
    }

    if (fault != NoFault) {
        // Explicitly do NOT invoke the fault. Discarding it is the whole
        // point of the "never raises an exception" invariant.
        return false;
    }
    return req->hasPaddr();
}

/* ===================================================================== *
 *  Responses (intercepted ahead of LSQ::recvTimingResp)
 * ===================================================================== */

bool
PrefetchHintQueue::recvTimingResp(PacketPtr pkt)
{
    SenderState *ss = dynamic_cast<SenderState *>(pkt->senderState);
    if (ss == nullptr) {
        // Not ours; the LSQ will handle it. Observe it first, because a
        // demand response for a block we are still prefetching means our
        // prefetch was too late to help.
        observeDemandResponse(pkt);
        return false;
    }

    auto it = inFlight.find(ss->reqId);
    if (it == inFlight.end()) {
        // Should not happen; be defensive rather than leak or crash.
        warn_once("%s: PHQ response with unknown reqId %llu\n", name(),
                  ss->reqId);
        delete pkt->senderState;
        pkt->senderState = nullptr;
        delete pkt;
        return true;
    }

    const InFlight rec = it->second;
    inFlight.erase(it);

    auto blk_it = inFlightBlocks.find(rec.blkAddr);
    if (blk_it != inFlightBlocks.end()) {
        if (--blk_it->second == 0)
            inFlightBlocks.erase(blk_it);
    }

    if (rec.isChaseLoad && !rec.squashed) {
        // DESIGN.md 1.2 variant 1: on return, compute base + (val << shift)
        // and prefetch. The PHQ performs the pointer chase; the core never
        // sees either access.
        Entry &e = entries[rec.entryIdx];
        if (e.valid() && e.seqNum == rec.seqNum &&
            e.state == EntryState::WaitChase) {
            uint64_t val = 0;
            switch (pkt->getSize()) {
              case 1:  val = pkt->getLE<uint8_t>();  break;
              case 2:  val = pkt->getLE<uint16_t>(); break;
              case 4:  val = pkt->getLE<uint32_t>(); break;
              default: val = pkt->getLE<uint64_t>(); break;
            }
            e.indexOrAddr = val;
            e.state = EntryState::Ready;
            e.issued = 0;
            DPRINTF(PHQ, "[sn:%llu] entry %d chase returned %llu; ready\n",
                    e.seqNum, rec.entryIdx, val);
            scheduleTick();
        }
    }

    delete pkt->senderState;
    pkt->senderState = nullptr;
    delete pkt;
    return true;
}

void
PrefetchHintQueue::observeDemandResponse(PacketPtr pkt)
{
    if (inFlightBlocks.empty())
        return;
    if (!pkt->req || !pkt->req->hasPaddr())
        return;
    const Addr blk = blockAlign(pkt->getAddr());
    auto it = inFlightBlocks.find(blk);
    if (it == inFlightBlocks.end())
        return;
    // A demand access to this block completed while our prefetch for it was
    // still on the wire. The prefetch did not hide any latency.
    ++phqStats.prefetchesLate;
    DPRINTF(PHQ, "late prefetch detected for block %#x\n", blk);
}

/* ===================================================================== *
 *  Squash -- DESIGN.md 1.4
 * ===================================================================== */

void
PrefetchHintQueue::squash(InstSeqNum squashed_seq_num, ThreadID tid)
{
    for (unsigned i = 0; i < phqEntries; ++i) {
        Entry &e = entries[i];
        if (!e.valid() || e.tid != tid)
            continue;
        if (e.seqNum <= squashed_seq_num)
            continue;
        DPRINTF(PHQ, "[sn:%llu] entry %u squashed (squash point [sn:%llu])\n",
                e.seqNum, i, squashed_seq_num);
        dropEntry((int)i, phqStats.hintsDroppedSquash);
    }

    // Requests already on the wire cannot be recalled. Mark them so their
    // response does not write into a slot that has since been reused, and
    // leave the block refcount alone so the late-prefetch accounting stays
    // balanced.
    for (auto &kv : inFlight) {
        if (kv.second.seqNum > squashed_seq_num)
            kv.second.squashed = true;
    }
}

/* ===================================================================== *
 *  Structural assertions -- DESIGN.md 4.2
 * ===================================================================== */

void
PrefetchHintQueue::noteIqAllocation(const DynInstPtr &inst)
{
    if (!inst->staticInst->isHintGather())
        return;
    ++phqStats.iqEntriesAllocated;
    warn_once("%s: a HINT.GATHER allocated an issue-queue entry "
              "[sn:%llu]. The central structural claim of this design is "
              "violated; the correctness gate will fail.\n",
              name(), inst->seqNum);
}

void
PrefetchHintQueue::noteLsqAllocation(const DynInstPtr &inst)
{
    if (!inst->staticInst->isHintGather())
        return;
    ++phqStats.lsqEntriesAllocated;
    warn_once("%s: a HINT.GATHER allocated an LSQ entry [sn:%llu]. "
              "DESIGN.md 1.3.3 is violated; the correctness gate will "
              "fail.\n", name(), inst->seqNum);
}

void
PrefetchHintQueue::noteFuPortCycle(const DynInstPtr &inst)
{
    if (!inst->staticInst->isHintGather())
        return;
    ++phqStats.fuPortCycles;
    warn_once("%s: a HINT.GATHER occupied a functional-unit port "
              "[sn:%llu]. DESIGN.md 1.4 is violated; the correctness gate "
              "will fail.\n", name(), inst->seqNum);
}

void
PrefetchHintQueue::noteRobAllocation(const DynInstPtr &inst)
{
    if (!inst->staticInst->isHintGather())
        return;
    // Deliberately silent: this is the expected, healthy path. It exists so
    // that `iqEntriesAllocated == 0` can be distinguished from "no hints ever
    // reached the back end". See the declaration comment in the header.
    ++phqStats.robEntriesAllocated;
}

void
PrefetchHintQueue::checkRenameInvariants(const DynInstPtr &inst)
{
    if (!inst->staticInst->isHintGather())
        return;

    // DESIGN.md 1.3.1 and 1.4: no physical destination register, ever.
    panic_if(inst->numDestRegs() != 0,
             "%s: HINT.GATHER [sn:%llu] declares %d destination registers. "
             "The RISC-V HintGatherOp format must not reference Rd "
             "(docs/DESIGN.md 1.1: rd = x0).",
             name(), inst->seqNum, (int)inst->numDestRegs());

    // DESIGN.md 1.3.3: never enters the LSQ, so it must not look like a
    // memory reference to the rest of the pipeline.
    panic_if(inst->isLoad() || inst->isStore() || inst->isAtomic(),
             "%s: HINT.GATHER [sn:%llu] is flagged as a memory reference. "
             "The format must not use the Mem operand "
             "(docs/DESIGN.md 1.3.3).",
             name(), inst->seqNum);
}

/* ===================================================================== *
 *  Helpers
 * ===================================================================== */

void
PrefetchHintQueue::releaseEntry(int idx)
{
    entries[idx] = Entry();
    entries[idx].state = EntryState::Empty;
}

void
PrefetchHintQueue::dropEntry(int idx, statistics::Scalar &reason)
{
    ++reason;
    releaseEntry(idx);
}

bool
PrefetchHintQueue::underMshrPressure() const
{
    if (l1dMshrs == 0)
        return false;
    // BaseCache::mshrQueue is protected and the PHQ holds only a
    // RequestPort, so true L1D MSHR occupancy is not observable from here
    // (see README, "MSHR pressure is approximated"). We use the PHQ's own
    // outstanding-block count against the configured MSHR budget, which is
    // a lower bound on real occupancy; the port refusing a request
    // (phqStats.portBlocked) is the exact, in-band signal.
    const double occ = (double)inFlightBlocks.size() / (double)l1dMshrs;
    return occ >= mshrPressureThreshold;
}

unsigned
PrefetchHintQueue::occupancy() const
{
    unsigned n = 0;
    for (unsigned i = 0; i < phqEntries; ++i) {
        if (entries[i].valid())
            ++n;
    }
    return n;
}

/* ===================================================================== *
 *  Statistics -- DESIGN.md 4.2
 * ===================================================================== */

PrefetchHintQueue::PHQStats::PHQStats(statistics::Group *parent)
    : statistics::Group(parent),
      // NOTE: gem5 v23 spells this macro ADD_STAT; gem5 develop (~v24)
      // renamed it to GEM5_ADD_STAT. See README "If the build fails" #1.
      ADD_STAT(hintsDispatched, statistics::units::Count::get(),
               "HINT.GATHER instructions that allocated a PHQ entry"),
      ADD_STAT(hintsDropped, statistics::units::Count::get(),
               "HINT.GATHER instructions dropped, all reasons"),
      ADD_STAT(hintsDroppedMshr, statistics::units::Count::get(),
               "Hints dropped because the L1D was above "
               "mshr_pressure_threshold"),
      ADD_STAT(hintsDroppedTimeout, statistics::units::Count::get(),
               "Hints dropped because the operand did not become ready "
               "within phq_poll_limit polls"),
      ADD_STAT(hintsDroppedSquash, statistics::units::Count::get(),
               "Hints invalidated by a pipeline squash"),
      ADD_STAT(hintsDroppedFull, statistics::units::Count::get(),
               "Hints rejected at dispatch: PHQ full or dispatch port busy"),
      ADD_STAT(prefetchesIssued, statistics::units::Count::get(),
               "Prefetch requests issued by the PHQ"),
      ADD_STAT(prefetchesLate, statistics::units::Count::get(),
               "PHQ prefetches still in flight when a demand response for "
               "the same cache block returned"),
      ADD_STAT(occupancyAvg, statistics::units::Count::get(),
               "Time-weighted average number of occupied PHQ entries"),
      ADD_STAT(iqEntriesAllocated, statistics::units::Count::get(),
               "Issue-queue entries allocated by a HINT.GATHER "
               "(STRUCTURAL ASSERTION: must be 0)"),
      ADD_STAT(lsqEntriesAllocated, statistics::units::Count::get(),
               "LSQ entries allocated by a HINT.GATHER "
               "(STRUCTURAL ASSERTION: must be 0)"),
      ADD_STAT(fuPortCycles, statistics::units::Cycle::get(),
               "Functional-unit port cycles consumed by a HINT.GATHER "
               "(STRUCTURAL ASSERTION: must be 0)"),
      ADD_STAT(robEntriesAllocated, statistics::units::Count::get(),
               "ROB entries allocated by a HINT.GATHER "
               "(POSITIVE CONTROL: must be > 0)"),
      ADD_STAT(chaseLoadsIssued, statistics::units::Count::get(),
               "Index loads issued by the PHQ itself (HINT.GATHER.C)"),
      ADD_STAT(hintsUndecodable, statistics::units::Count::get(),
               "Instructions flagged IsHintGather with no field payload"),
      ADD_STAT(translationFailures, statistics::units::Count::get(),
               "PHQ addresses with no valid translation"),
      ADD_STAT(portBlocked, statistics::units::Count::get(),
               "Times the shared D-cache port refused a PHQ request"),
      ADD_STAT(prefetchesRequestedL2, statistics::units::Count::get(),
               "Prefetches whose LEVEL bit asked for L2 (advisory; see "
               "README)")
{
    hintsDropped = hintsDroppedMshr + hintsDroppedTimeout
                 + hintsDroppedSquash + hintsDroppedFull;
    hintsDropped.flags(statistics::total);

    // Deliberately NOT statistics::nozero. Every name in DESIGN.md 4.2 must
    // appear in stats.txt unconditionally, including in a --disable-phq
    // baseline run where the PHQ is never occupied; `nozero` would suppress
    // the line and break both the CHIA loop's parser and
    // tests/check_arch_equiv.py's stat-name contract check.
}

} // namespace o3
} // namespace gem5
