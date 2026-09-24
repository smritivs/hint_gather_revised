# HINT.GATHER -- PHQ area and storage estimate

Companion to [`../../docs/DESIGN.md`](../../docs/DESIGN.md) sec 2 ("Area:
`phq_entries x ~82 bits` + one 64-bit adder + `phq_entries` comparators") and
sec 1.4 (the pipeline-treatment table). This document does the arithmetic behind
that one-line claim, at `phq_entries` ∈ {4, 8, 16}, and sets it against what
the same memory-level parallelism would cost if you bought it by widening the
out-of-order core instead.

The purpose is to answer one question honestly: **is a Prefetch Hint Queue a
cheaper way to hold N outstanding irregular accesses than N more issue-queue
and load-queue entries?**

The short answer is that it is *not* dramatically cheaper in raw flip-flop
count -- it is cheaper in the dimensions that actually constrain an OoO core:
it adds nothing to the wakeup-select loop, consumes no LSQ ordering CAM, and
burns no physical register. See sec 5.

---

## 1. Method and its limits

> [!NOTE]
> No synthesis was run. These are structural estimates from field counts and
> standard-cell equivalents, which is the appropriate precision for a design
> exploration. Treat every number as ±30%, and treat the *ratios* in sec 5 as
> more trustworthy than the absolute µm².

Area is quoted primarily in **NAND2 gate equivalents (GE)**, which is
process-independent. Conversion assumptions where absolute numbers are given:

| Quantity | Value | Basis |
|---|---:|---|
| Scan flip-flop | 7 GE/bit | Typical for a 7nm-class HD library incl. scan and local clock gating |
| 8-bit equality comparator | 12 GE | 8 XNOR + 7-input AND tree |
| 64-bit adder (1/cycle, not critical path) | 750 GE | Carry-select, relaxed timing |
| NAND2 cell area (7nm-class) | 0.027 µm² | TSMC N7-class 6-track HD cell |
| Placement utilization | 70% | -> 0.039 µm² per placed GE |

The PHQ is **implemented in flip-flops, not SRAM.** At 4-16 entries of
~180 bits the array is far below the point where an SRAM macro is efficient
(the smallest practical single-port macro is ~64 words x 32 bits, and it would
be nearly all periphery), and the structure needs a random-access read of an
arbitrary entry plus a parallel squash-compare across all entries, which an
SRAM cannot do. Every "storage" number below is therefore flops.

---

## 2. Bits per PHQ entry

DESIGN.md sec 2 gives the entry struct and annotates it `~82 bits`. That figure
is inconsistent with its own field list: `base` and `index_or_addr` are both
64 bits, so the listed fields already sum to 161 bits. The `~82` appears to
count `base` (64) + `seq_num` (16) + the bitfields, omitting
`index_or_addr`.

> [!IMPORTANT]
> This document uses the honest accounting below (179 bits), not the `~82`
> figure. DESIGN.md is normative for *interfaces*; it is not normative for
> its own arithmetic, and quoting the low number would make the design look
> better than it is.

| Field | Bits | Notes |
|---|---:|---|
| `base` | 64 | rs1 value, captured when the operand becomes readable |
| `index_or_addr` | 64 | shared: index value (value form) or `&B[i+d]` then the loaded value (chase form) |
| `seq_num` | 16 | squash comparison; 16 bits wraps safely for any realistic ROB |
| src phys tags (rs1, rs2) | 16 | 2 x 8 b for a 256-entry PRF. Needed by **both** wakeup policies -- `poll_rf` to index the scoreboard, `tag_snoop` to compare against writeback |
| `shift` | 2 | element size `1 << shift` |
| `fanout` | 3 | issue `fanout + 1` prefetches |
| `issued` cursor | 3 | how many of the `fanout + 1` have gone out |
| `variant` | 1 | value / chase |
| `level` | 1 | L1D / L2C |
| `droppable` | 1 | |
| `state` | 2 | EMPTY / WAIT_OPERAND / WAIT_CHASE / READY (encodes valid) |
| `poll_count` | 6 | `phq_poll_limit <= 64` per DESIGN.md sec 3 |
| **Total** | **179** | |

The `issued` cursor and the source tags are absent from DESIGN.md sec 2's struct
but are unavoidable in an implementation: the former because a fan-out of up
to 8 cannot be issued in one cycle through one adder, the latter because an
entry that cannot name its sources cannot wake up.

**Possible optimisation, not assumed here:** the source tags are dead once
both operands have been read, and `base` is meaningless until then. Overlaying
the tags on the low 16 bits of `base` gives 163 bits/entry, a 9% saving, at
the cost of a mux on the `base` write path. All tables below use the
unoptimised 179.

---

## 3. Fixed (entry-count-independent) logic

| Block | GE | Notes |
|---|---:|---|
| 64-bit adder | 750 | `base + (index << shift)`, one result per cycle |
| 64-bit shifter | 200 | shift by 0..3 -> one 4:1 mux per bit |
| Address stepper | 0 | reuses the same adder on successive cycles; this is exactly why `adder_throughput` defaults to 1 and fan-out costs cycles, not hardware |
| Dispatch accept / allocate | 150 | free-slot priority encoder + write muxing for `phq_dispatch_width = 2` |
| Request formatting, MSHR-pressure gate | 100 | |
| `poll_rf` head pointer + scoreboard read mux | 50 | one scoreboard read port total, independent of occupancy |
| **Fixed subtotal** | **1250** | |

Per-entry control (state machine, squash comparator against the squash
sequence number, timeout increment) adds **40 GE/entry**.

`tag_snoop` mode adds, per entry, a comparison of both source tags against
every writeback port. At 4 writeback ports: 2 x 4 x 12 GE = **96 GE/entry**,
and unlike the `poll_rf` logic it toggles every cycle on every occupied entry.

---

## 4. Totals at 4, 8 and 16 entries

### `wakeup_policy = poll_rf` (the default)

| `phq_entries` | Storage (bits) | Storage (GE) | Control (GE) | Fixed (GE) | **Total (GE)** | Placed area (µm²) | mm² |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 4 | 716 | 5 012 | 160 | 1 250 | **6 422** | 250 | 0.00025 |
| 8 | 1 432 | 10 024 | 320 | 1 250 | **11 594** | 452 | 0.00045 |
| 16 | 2 864 | 20 048 | 640 | 1 250 | **21 938** | 855 | 0.00086 |

### `wakeup_policy = tag_snoop`

| `phq_entries` | Extra comparators (GE) | **Total (GE)** | Placed area (µm²) | mm² |
|---:|---:|---:|---:|---:|
| 4 | 384 | **6 806** | 265 | 0.00027 |
| 8 | 768 | **12 362** | 482 | 0.00048 |
| 16 | 1 536 | **23 474** | 915 | 0.00092 |

`tag_snoop` costs 6-7% more area than `poll_rf` at the same entry count, and
substantially more dynamic power, in exchange for removing the polling
latency and the `phq_poll_limit` timeout drops. Which one wins is an empirical
question for the CHIA loop, which is exactly why both are in the genome; the
relevant gem5 stats are `hintsDroppedTimeout` (should collapse to zero under
`tag_snoop`) and `prefetchesLate`.

**For scale:** a 7nm-class OoO core, excluding L2, is roughly 1-2 mm². An
8-entry PHQ at 0.00045 mm² is therefore **0.02-0.05% of core area**, or about
one part in 2 500. It is, in absolute terms, negligible. The interesting
question is not whether we can afford it but whether the same money spent on
the existing structures would buy more.

---

## 5. Versus buying the same MLP in the out-of-order core

This is the comparison that matters, and the one DESIGN.md sec 1.4 is really
making.

### 5.1 What one in-flight irregular access costs each way

To keep one dependent gather access outstanding across the index-load latency,
an ordinary load must remain resident in **every** back-end structure for the
whole duration. A HINT.GATHER does not.

| Structure | Ordinary dependent load | HINT.GATHER |
|---|---:|---:|
| ROB entry | 1, held for full latency | 1, marked complete at dispatch -- retires the cycle it reaches the head |
| Issue-queue entry | 1, held until issue | **0** |
| LSQ entry + ordering CAM | 1, held until commit | **0** |
| Physical register | 1 | **0** |
| PHQ entry | 0 | 1 |

Per-structure cost estimates, at a 32-entry IQ and a 32-entry LQ:

| Structure | GE per entry | Breakdown |
|---|---:|---|
| IQ entry | ~1 200 | 60 flops of control/tags (420) + 4-port wakeup CAM (96) + age-matrix row/column, 2N bits at N=32 (448) + select-tree share (80) + payload array share (160) |
| LQ entry | ~800 | 71 flops (500) + address ordering CAM, ~40 b x 2 compare ports (120) + forwarding-mux and disambiguation share (180) |
| PRF entry (64 b, heavily ported) | ~600 | 64 flops (448) + read/write port share |
| ROB entry | ~500 | |
| **Ordinary load, total** | **~3 100** | all four, held for the full access latency |
| **PHQ entry** | **~1 290** | 179 flops (1 253) + 40 control |

So per unit of sustained memory-level parallelism the PHQ is roughly
**2.4x cheaper in area** than the set of structures an ordinary dependent load
occupies -- and the ROB entry it does take is released almost immediately
rather than being pinned for the duration.

### 5.2 Why the raw ratio understates the case

Three effects that the GE numbers above do not capture, all of them in the
PHQ's favour:

1. **Superlinearity.** The IQ age matrix is N² bits. Growing a 32-entry IQ to
   40 entries costs 8 x 756 GE of per-entry logic *plus* 576 extra age bits
   (~= 4 000 GE) -- about 10 100 GE, not 8 x 1 200 = 9 600. Growing the LQ
   widens an address CAM that every store must search. The PHQ is strictly
   linear in `phq_entries`: no entry ever compares against another.

2. **Timing.** The IQ wakeup-select-bypass loop is the classic
   frequency-limiting path in an OoO core; widening the IQ is routinely paid
   for in MHz or in an extra pipeline stage. The PHQ sits entirely off that
   loop. It advances one entry per cycle with a single scoreboard read port
   and issues into a port the LSQ already owns. Its critical path is the
   64-bit add, which has a full cycle. Area you can buy without touching
   `Fmax` is qualitatively cheaper than area you cannot.

3. **Dynamic energy.** Every IQ entry is CAM-matched against every writeback
   port every cycle regardless of occupancy. An 8-entry PHQ in `poll_rf` mode
   performs exactly one scoreboard bit read per cycle in total. Under
   `tag_snoop` the PHQ's energy profile becomes IQ-like, which is a second
   reason the loop should be expected to prefer `poll_rf` unless timeout
   drops are hurting coverage.

### 5.3 Where the comparison is unfair to the IQ

Stated plainly, so the reader can discount appropriately:

* An IQ entry is general-purpose; a PHQ entry can only hold a gather hint. If
  the workload has no gather sites, the PHQ is 100% wasted area and the IQ
  entries would not have been.
* The PHQ entry is *fatter* than an IQ entry in bits (179 vs ~60 of control)
  because it stores two 64-bit data values rather than tags. Per *bit* the PHQ
  is the more expensive structure; it wins on bits-needed, not bits-cost.
* This accounting ignores the decoder, the rename-stage source-map read that
  DESIGN.md sec 1.4 explicitly concedes, and the verification cost of a new
  architectural instruction -- which, for a real product, would dwarf 0.0005
  mm².

---

## 6. Choosing `phq_entries`

`phq_entries` is a genome key with range 2..32 (DESIGN.md sec 3). Area is linear
and tiny across that whole range, so area should not drive the choice --
utilisation should. The two gem5 stats that answer it:

* `system.cpu.phq.occupancyAvg` -- the time-weighted mean number of occupied
  entries. If this sits well below `phq_entries`, the extra entries are dead
  area.
* `system.cpu.phq.hintsDroppedFull` -- hints rejected at dispatch because no
  slot was free. If this is non-trivial, the queue is the binding constraint
  and more entries will convert directly into coverage.

The expected shape is a knee: `hintsDroppedFull` falls steeply up to roughly
the number of hints that can be in flight within one index-load latency
(~= latency / hint-issue-interval), then flattens, while `occupancyAvg` tracks
it and then saturates. Entries past the knee cost ~1 290 GE each and buy
nothing.

Given that 16 entries is still under 0.001 mm², the honest recommendation is
to let the loop pick from the whole range and to treat area as a tie-breaker
only.

---

## 7. Summary

| | 4 entries | 8 entries | 16 entries |
|---|---:|---:|---:|
| State | 716 b | 1 432 b | 2 864 b |
| Total (`poll_rf`) | 6.4 kGE | 11.6 kGE | 21.9 kGE |
| Total (`tag_snoop`) | 6.8 kGE | 12.4 kGE | 23.5 kGE |
| Placed area, 7nm-class | 0.00025 mm² | 0.00045 mm² | 0.00086 mm² |
| Share of a 1.5 mm² core | 0.017% | 0.030% | 0.057% |
| Equivalent IQ+LSQ+PRF+ROB cost for the same MLP | 12.4 kGE | 24.8 kGE | 49.6 kGE |
| Area advantage | 1.9x | 2.1x | 2.3x |

Storage is flip-flops throughout; no SRAM macro is warranted at any point in
the parameter range.
