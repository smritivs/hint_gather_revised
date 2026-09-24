# HINT.GATHER — Design & Interface Contract

This is the **normative spec** for the implementation. Every component (LLVM pass,
gem5 model, ChampSim model, benchmarks, CHIA loop) must conform to the contracts
here. If you are implementing one component, treat this file as the source of
truth for anything that crosses a component boundary.

Project: agentic co-design of a zero-issue-queue prefetch hint for irregular
(gather / pointer-chasing) access patterns, driven by a CHIA loop.

---

## 1. The instruction

### 1.1 Encoding

`HINT.GATHER` lives in RISC-V **custom-0** opcode space (`opcode = 0x0B`,
`0b0001011`). It is emitted as an R-type instruction via the assembler's
`.insn` directive, so **no LLVM backend rebuild is required**:

```
.insn r 0x0b, <funct3>, <funct7>, x0, <rs1>, <rs2>
```

| Field | Value | Meaning |
|---|---|---|
| `opcode` | `0x0B` | custom-0 |
| `rd` | `x0` | no destination register — never allocates a physical register |
| `rs1` | GPR | **base pointer** of the gathered array `A` |
| `rs2` | GPR | variant-dependent (see below) |
| `funct3` | 3 bits | `[0]` variant, `[1]` prefetch level, `[2]` droppable |
| `funct7` | 7 bits | `[1:0]` index scale shift, `[4:2]` fan-out, `[6:5]` reserved (0) |

`funct3` bit assignment (LSB = bit 0):

| Bit | Name | 0 | 1 |
|---|---|---|---|
| 0 | `VARIANT` | `HINT.GATHER` (value form) | `HINT.GATHER.C` (chase form) |
| 1 | `LEVEL` | prefetch into L1D | prefetch into L2 |
| 2 | `DROP` | non-droppable (still best-effort) | droppable under MSHR pressure |

`funct7` bit assignment:

| Bits | Name | Meaning |
|---|---|---|
| 1:0 | `SHIFT` | index scale: element size is `1 << SHIFT` bytes (0..3 → 1,2,4,8) |
| 4:2 | `FANOUT` | issue `FANOUT + 1` prefetches, stepping the index address by the element stride |
| 6:5 | — | reserved, must be 0 |

### 1.2 The two variants (this is the core design axis)

The proposal's central risk is **operand readiness**: a hint that consumes the
result of a load cannot be "free" if it needs issue-queue wake-up logic. We
implement both answers and let the evolutionary search pick.

**Variant 0 — `HINT.GATHER` (value form).**
`rs2` holds the *already-loaded index value* `B[i+d]`.
Target address = `rs1 + (rs2 << SHIFT)`.
The PHQ must wait for `rs2` to be ready. Two sub-policies, selected by the
model's `wakeup_policy` parameter (not encoded in the instruction — it is a
microarchitectural configuration):

* `poll_rf` — the PHQ head polls the physical register file scoreboard. If the
  operand is not ready after `phq_poll_limit` cycles, the hint is **dropped**.
  Cost: one scoreboard read port. No wake-up CAM. This is the honest
  "zero-IQ" design.
* `tag_snoop` — the PHQ snoops *only* load-writeback destination tags
  (`phq_entries` comparators, not the full broadcast network).

**Variant 1 — `HINT.GATHER.C` (chase form).**
`rs2` holds the *address* `&B[i+d]`, which is affine and therefore always ready
at dispatch. The PHQ itself issues the load of `B[i+d]`, and on its return
computes `rs1 + (val << SHIFT)` and prefetches that. The PHQ performs the
pointer chase; the core never sees either access.
This variant has **no operand-readiness problem at all** and is the strongest
form of the idea; it costs one extra small state machine per PHQ entry.

### 1.3 Architectural semantics (invariant — the correctness gate checks this)

1. Writes no architectural register (`rd = x0`), writes no memory.
2. **Never raises an exception.** On TLB miss the behaviour is set by
   `tlb_miss_policy`: `drop` (default) or `walk` (speculative page-table walk,
   faults suppressed).
3. Never orders against any other memory operation. It does **not** enter the
   LSQ and takes no part in store-to-load forwarding or memory disambiguation.
4. Removing every `HINT.GATHER` from a program must not change its
   architectural result. Equivalently: a correct implementation may treat the
   instruction as `NOP`.

### 1.4 Pipeline treatment (the contribution)

| Structure | Normal load / `prefetch.r` | `HINT.GATHER` |
|---|---|---|
| ROB entry | yes | **yes** (in-order commit, precise exceptions) |
| Issue queue entry | yes | **no** |
| Functional-unit / AGU port | yes | **no** (dedicated PHQ adder) |
| LSQ entry + ordering CAM | yes | **no** |
| Physical destination register | yes | **no** |
| Rename source-map read | yes | yes (cheap; acknowledged cost) |

The hint is marked **complete at dispatch**, so it never blocks the ROB head
and retires the cycle it arrives there.

On a squash, PHQ entries whose sequence number is younger than the squash point
are invalidated. (Failing to do so is not a correctness bug — only a wasted
prefetch — but we implement and measure it.)

---

## 2. The Prefetch Hint Queue (PHQ)

A small structure adjacent to the LSQ.

```
struct PHQEntry {                       // ~82 bits
    uint64_t base;                      // 64b  rs1 value
    uint64_t index_or_addr;             // (shared storage, see below)
    uint16_t seq_num;                   // 16b  for squash
    uint8_t  shift : 2;
    uint8_t  fanout : 3;
    uint8_t  variant : 1;
    uint8_t  level : 1;
    uint8_t  droppable : 1;
    uint8_t  state : 2;                 // EMPTY | WAIT_OPERAND | WAIT_CHASE | READY
    uint8_t  poll_count;
};
```

Behaviour per cycle:
1. Accept up to `phq_dispatch_width` (default 2) new entries from dispatch.
2. Advance waiting entries per `wakeup_policy`; drop on `poll_count >
   phq_poll_limit`.
3. For `READY` entries, compute `base + (index << shift)` on the dedicated
   adder and issue `fanout + 1` prefetch requests (successive entries step the
   index address by `1 << shift`).
4. If the target cache's MSHRs are full (or above `mshr_pressure_threshold`)
   and the hint is droppable, **drop it** and increment `phqHintsDropped`.

Area: `phq_entries × ~82 bits` + one 64-bit adder + `phq_entries` comparators
(only in `tag_snoop` mode). At 8 entries that is 656 bits of state.

---

## 3. The genome (shared search space)

This exact JSON schema is produced by the evolutionary node and consumed by the
LLVM node, the gem5 node, and the ChampSim node. Keys are stable; add new keys
only by updating this section.

```json
{
  "hint_distance":      32,
  "fanout":             1,
  "entropy_threshold":  0.35,
  "variant":            "chase",
  "prefetch_level":     "L1D",
  "droppable":          true,
  "phq_entries":        8,
  "phq_dispatch_width": 2,
  "phq_poll_limit":     16,
  "wakeup_policy":      "poll_rf",
  "tlb_miss_policy":    "drop",
  "mshr_pressure_threshold": 0.75,
  "max_hints_per_loop": 2,
  "min_trip_count":     64
}
```

| Key | Type / domain | Consumed by |
|---|---|---|
| `hint_distance` | int, 1..512 | LLVM, ChampSim |
| `fanout` | int, 1..8 (instruction encodes `fanout-1`) | LLVM, gem5, ChampSim |
| `entropy_threshold` | float, 0.0..1.0 | LLVM |
| `variant` | `"value"` \| `"chase"` | LLVM, gem5, ChampSim |
| `prefetch_level` | `"L1D"` \| `"L2C"` | LLVM, gem5, ChampSim |
| `droppable` | bool | LLVM, gem5, ChampSim |
| `phq_entries` | int, 2..32 | gem5, ChampSim |
| `phq_dispatch_width` | int, 1..4 | gem5 |
| `phq_poll_limit` | int, 1..64 | gem5 |
| `wakeup_policy` | `"poll_rf"` \| `"tag_snoop"` | gem5 |
| `tlb_miss_policy` | `"drop"` \| `"walk"` | gem5 |
| `mshr_pressure_threshold` | float, 0.0..1.0 | gem5, ChampSim |
| `max_hints_per_loop` | int, 1..8 | LLVM |
| `min_trip_count` | int | LLVM |

---

## 4. Component interfaces

### 4.1 LLVM pass plugin (`llvm/`)

Out-of-tree pass plugin, loaded into a **prebuilt** clang:

```
clang --target=riscv64-unknown-elf -O2 \
      -fpass-plugin=$HG_BUILD/libHintGather.so \
      -mllvm -hg-genome=<path/to/genome.json> \
      -mllvm -hg-report=<path/to/hint_sites.json> \
      -mllvm -hg-mode=emit            # or 'profile' or 'analyze'
      bench.c -o bench.hint.elf
```

Modes:

| Mode | Behaviour |
|---|---|
| `analyze` | detect gather idioms, emit `hint_sites.json`, change nothing |
| `profile` | instrument each candidate with a call to `__hg_profile_access(site_id, addr)`, link `hgprof_runtime` |
| `emit` | insert `HINT.GATHER` inline asm at qualifying sites |

**Detection**: in each loop, match `load` → `getelementptr` → `load` where the
first load's result feeds the GEP index. Reject the site if
`ScalarEvolution::getSCEV(gep_ptr)` is an affine `SCEVAddRecExpr` for the loop
(the hardware stride prefetcher already covers it). Require estimated trip
count ≥ `min_trip_count`. Cap at `max_hints_per_loop` per loop, ranked by
profiled entropy when a profile is available.

**`hint_sites.json`** (written in `analyze`/`profile` mode, read by ChampSim node):

```json
{
  "genome_hash": "ab12cd34",
  "sites": [
    {
      "site_id": 0,
      "function": "bfs_kernel",
      "loop_header": "for.body.i",
      "source": "bench/bfs.c:142",
      "base_value": "g_edges",
      "index_load": "offsets[i]",
      "elem_size": 4,
      "scev_affine": false,
      "estimated_trip_count": 4096,
      "entropy": 0.83,
      "emitted": true,
      "pc_symbol": "__hg_site_0"
    }
  ]
}
```

`pc_symbol` is a local label emitted immediately before the demand load that
the hint targets. The ChampSim node resolves it to a PC with `nm`/`objdump` so
the trace-driven model can key on it.

**Emission** uses this macro (`llvm/include/hint_gather.h`) so the same
encoding is testable from plain C:

```c
#define HINT_GATHER_C(base, idx_addr, shift, fanout, level, drop) \
  __asm__ volatile(".insn r 0x0b, %2, %3, x0, %0, %1" \
                   :: "r"(base), "r"(idx_addr), \
                      "i"(HG_FUNCT3(1, level, drop)), \
                      "i"(HG_FUNCT7(shift, fanout)))
```

**Profile runtime** (`llvm/runtime/hgprof_runtime.c`) accumulates, per site, a
histogram of address deltas and writes `hg_profile.json` at exit:

```json
{"sites": [{"site_id": 0, "accesses": 1048576, "distinct_deltas": 9871,
            "entropy": 0.83, "top_delta_share": 0.02}]}
```

`entropy` is the Shannon entropy of the delta histogram normalised to
`log2(min(distinct_deltas, 256))`, so it lands in `[0, 1]`.

### 4.2 gem5 (`gem5/`)

* `gem5/src/cpu/o3/prefetch_hint_queue.{hh,cc}` — the PHQ.
* `gem5/src/cpu/o3/PrefetchHintQueue.py` — SimObject params mirroring the genome.
* `gem5/apply_phq.py` — deterministic, idempotent patcher:

```
python gem5/apply_phq.py --gem5-root /path/to/gem5 [--revert] [--check]
```

It copies the new files in and applies minimal, clearly-delimited edits to
gem5's O3 decode/dispatch (guarded by `// BEGIN HINT.GATHER` /
`// END HINT.GATHER` markers so the patch is re-appliable and machine-editable
by the repair agent).

* `gem5/configs/hint_gather_se.py` — SE-mode O3 config. Flags:

```
--binary PATH  --max-insts N  --cpu-type {atomic,o3}
--phq-entries N --phq-dispatch-width N --phq-poll-limit N
--wakeup-policy {poll_rf,tag_snoop} --tlb-miss-policy {drop,walk}
--prefetch-level {L1D,L2C} --mshr-pressure-threshold F
--l1d-prefetcher {none,stride} --disable-phq
--options "ARGS"
```

**Required stats** (the loop parses these names; do not rename):

```
system.cpu.phq.hintsDispatched
system.cpu.phq.hintsDropped          # by policy: operand timeout / MSHR / squash / full
system.cpu.phq.hintsDroppedMshr
system.cpu.phq.hintsDroppedTimeout
system.cpu.phq.hintsDroppedSquash
system.cpu.phq.hintsDroppedFull
system.cpu.phq.prefetchesIssued
system.cpu.phq.prefetchesLate
system.cpu.phq.occupancyAvg
system.cpu.phq.iqEntriesAllocated    # MUST be 0 — structural assertion
system.cpu.phq.lsqEntriesAllocated   # MUST be 0 — structural assertion
system.cpu.phq.fuPortCycles          # MUST be 0 — structural assertion
```

### 4.3 ChampSim (`champsim/`)

CHIA's `ChampSimNode.build_champsim()` compiles a **header-only prefetcher
module**. We therefore model HINT.GATHER as a prefetcher module driven by the
compiler's hint-site PC table — which is exactly what the software hint does,
minus the pipeline cost that ChampSim cannot model anyway (see §6).

`champsim/hint_gather_prefetcher.h.in` is a template with `@@KEY@@`
placeholders substituted by the loop:

```
@@HINT_SITES@@          // C++ initializer list of {pc, base_reg_hint, elem_size, stride_bytes}
@@HINT_DISTANCE@@ @@FANOUT@@ @@PHQ_ENTRIES@@ @@DROPPABLE@@
@@MSHR_PRESSURE_THRESHOLD@@ @@PREFETCH_LEVEL@@ @@VARIANT@@
```

The module must print a `prefetcher_final_stats()` block of
`key: value` lines (CHIA parses these into `custom_prefetch_stats`), including
at minimum:

```
hg_hints_seen: N
hg_prefetches_issued: N
hg_prefetches_dropped: N
hg_phq_full_events: N
hg_late_prefetches: N
```

### 4.4 Benchmarks (`bench/`)

`make` produces, for every benchmark `<b>` and every variant:

```
build/<b>.base.elf     # no software prefetching at all
build/<b>.swpf.elf     # Zicbop-style software prefetch baseline (ordinary instructions)
build/<b>.hint.elf     # HINT.GATHER
```

Every benchmark must:
* accept `--iters N` and `--size N`,
* be deterministic and self-checking,
* print exactly one line `CHECKSUM=0x%016llx` before exit,
* print `ROI_BEGIN` / `ROI_END` markers on stdout,
* exit 0 on success.

The correctness gate compares the `CHECKSUM=` line across `base` and `hint`
builds under gem5 atomic mode. Any difference fails the candidate.

Benchmarks: `gather` (synthetic `A[B[i]]`), `bfs` (CSR, GAP-style),
`pagerank` (CSR, pull), `listchase` (pure pointer chase).

---

## 5. The CHIA loop

```
Node 0  select targets            -> benchmark set
Node 1  LLVM analyze + profile    -> hint_sites.json, hg_profile.json
Node 2  compiler agent / emit     -> *.hint.elf          [self-heals on build failure]
Node 3  microarch agent / gem5    -> patched+built gem5  [self-heals on build/assert failure]
Gate    atomic-mode equivalence   -> pass/fail (+ structural assertions)
Node 4  ChampSim fast eval        -> IPC, L1D MPKI, prefetch accuracy/coverage
Node 5  evolutionary search       -> next genome population
Node 6  gem5 O3 validation        -> the numbers that go in the write-up
```

Cycles: Node 2 → Node 2 (compile repair), Node 3 → Node 3 (gem5 repair),
Node 5 → Node 2 (next generation), Node 6 → Node 5 (re-anchor the fitness).

### 5.1 Fitness

ChampSim cannot observe issue-queue or LSQ pressure, so raw ChampSim IPC would
reward hint spam. The inner fitness is penalised and periodically re-anchored
against gem5:

```
fitness = (ipc_hint / ipc_swpf_baseline) - lambda * hints_per_1k_insts
                                          - mu  * wasted_prefetch_rate
```

`lambda` starts at `0.01` and is recalibrated every `reanchor_every` generations
by regressing observed gem5 O3 IPC against ChampSim IPC and hint density for the
top-N candidates.

---

## 6. Known limitations to state in the write-up

1. ChampSim is trace-driven and does not faithfully model IQ port pressure or
   LSQ CAM bandwidth. It is used only for memory-system tuning; **all pipeline
   claims come from gem5 O3**. The fitness penalty and re-anchoring exist
   specifically because of this.
2. We emit the instruction via `.insn` rather than a first-class LLVM intrinsic
   with SelectionDAG lowering. The encoding is identical; the intrinsic is
   future work.
3. RISC-V Zicbop already standardises prefetch hints. Our novelty is the
   **dispatch-path treatment**, not the encoding; `custom-0` is used because
   Zicbop cannot express the distance/fan-out/variant operands.
