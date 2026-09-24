# HINT.GATHER -- gem5 microarchitecture component

This directory contains the gem5 implementation of **HINT.GATHER**, a RISC-V
`custom-0` prefetch-hint instruction, plus the Prefetch Hint Queue (PHQ) it
feeds.

The normative specification is [`../docs/DESIGN.md`](../docs/DESIGN.md).
Everything in this directory is downstream of that document; where this README
and DESIGN.md disagree, DESIGN.md wins and this README is a bug.

> [!IMPORTANT]
> The central structural claim of the design is that HINT.GATHER **allocates a
> ROB entry and nothing else** -- no issue-queue entry, no LSQ entry, no
> functional-unit port. That claim is not a comment; it is enforced by the
> patch and measured by four counters
> (`iqEntriesAllocated`, `lsqEntriesAllocated`, `fuPortCycles` must be zero,
> `robEntriesAllocated` must be non-zero).

---

## Contents

| Path | Role |
|---|---|
| `src/cpu/o3/prefetch_hint_queue.hh` | PHQ entry model, policies, public API, stats group |
| `src/cpu/o3/prefetch_hint_queue.cc` | PHQ implementation (DESIGN.md sec 2) |
| `src/cpu/o3/PrefetchHintQueue.py` | SimObject declaration; params mirror the genome (DESIGN.md sec 3) |
| `src/arch/riscv/isa/formats/hint_gather.isa` | `HintGatherOp` instruction format |
| `apply_phq.py` | Idempotent / reversible / checkable patcher |
| `configs/hint_gather_se.py` | SE-mode config; flags per DESIGN.md sec 4.2 |
| `tests/check_arch_equiv.py` | Correctness gate; prints `GATE: PASS` / `GATE: FAIL <reason>` |
| `docs/AREA.md` | SRAM and area estimate for the PHQ |

---

## Apply, build, run, revert

### 1. Apply

```bash
python3 apply_phq.py --gem5-root /path/to/gem5
```

The patcher is safe to re-run: every insertion is wrapped in

```
// BEGIN HINT.GATHER (apply_phq.py) <REGION_ID>
...
// END HINT.GATHER (apply_phq.py) <REGION_ID>
```

(`#` instead of `//` in Python/SCons/ISA files), so a second run is a no-op.

```bash
python3 apply_phq.py --gem5-root /path/to/gem5 --check      # applied? partial?
python3 apply_phq.py --gem5-root /path/to/gem5 --dry-run    # show, change nothing
python3 apply_phq.py --gem5-root /path/to/gem5 --revert     # remove exactly those regions
```

`--check` exits 0 when fully applied, 1 when not applied, 2 when partially
applied (i.e. some regions present and some absent -- the state you land in if a
previous run hit a missing anchor).

### 2. Build

```bash
cd /path/to/gem5
scons build/RISCV/gem5.opt -j"$(nproc)"
```

The patch adds one `Source()`, one `SimObject()` and one `DebugFlag()` to
`src/cpu/o3/SConscript`, and one `##include` to
`src/arch/riscv/isa/formats/formats.isa`. Nothing else in the build system is
touched; gem5's `ISADesc` follows `##include` transitively, so the new
`.isa` file needs no separate registration.

### 3. Run

```bash
build/RISCV/gem5.opt --outdir=m5out \
    /path/to/hint_gather/gem5/configs/hint_gather_se.py \
    --binary /path/to/bench/build/gups_hinted \
    --options "--n 65536" \
    --max-insts 50000000 \
    --cpu-type o3 \
    --phq-entries 8 --phq-dispatch-width 2 --phq-poll-limit 16 \
    --wakeup-policy poll_rf --tlb-miss-policy drop \
    --prefetch-level L1D --mshr-pressure-threshold 0.75 \
    --l1d-prefetcher none
```

Add `--disable-phq` for the baseline arm. The PHQ SimObject is still
instantiated in that case, so all `system.cpu.phq.*` stats still appear
(reading zero) and the loop's stat parser needs no special case.

### 4. Gate

```bash
python3 tests/check_arch_equiv.py \
    --gem5-binary /path/to/gem5/build/RISCV/gem5.opt \
    --binary /path/to/bench/build/gups_hinted \
    --options "--n 65536" --max-insts 20000000
```

Last line is always exactly `GATE: PASS` or `GATE: FAIL <reason>`; exit status
0 or 1. The gate runs three simulations (O3 + PHQ, the architectural
reference, and the same binary on the atomic CPU) and checks:

1. every stat name in DESIGN.md sec 4.2 is present in `stats.txt`;
2. `iqEntriesAllocated == lsqEntriesAllocated == fuPortCycles == 0`;
3. `robEntriesAllocated > 0` and `hintsDispatched > 0` -- **the positive
   control**, without which the three zeroes above are also what a build that
   silently drops hints at decode would produce;
4. program output is byte-identical across all three runs;
5. committed instruction count matches between the O3 and atomic runs.

### 5. Revert

```bash
python3 apply_phq.py --gem5-root /path/to/gem5 --revert
scons build/RISCV/gem5.opt -j"$(nproc)"
```

`--revert` deletes the marked regions and the four copied files, restoring the
tree to a byte-identical pristine state.

---

## Files the patcher modifies, and the anchor used for each

Fifteen existing files, nineteen regions. Anchors are searched as **literal
text that must occur exactly once**; where two candidates are listed the first
one that occurs exactly once wins, which is how the patch spans both gem5 v23
and gem5 develop. Line numbers are never used.

| Region ID | File | Anchor (first candidate) | Where |
|---|---|---|---|
| `STATIC_INST_FLAG` | `src/cpu/StaticInstFlags.py` | `"IsHtmCancel",  # Explicitely aborts a HTM transaction` | after |
| `STATIC_INST_ACCESSORS` | `src/cpu/static_inst.hh` | `bool isHtmCancel() const { return flags[IsHtmCancel]; }` | after |
| `CPU_INCLUDE` | `src/cpu/o3/cpu.hh` | `#include "cpu/o3/scoreboard.hh"` | after |
| `CPU_MEMBERS` | `src/cpu/o3/cpu.hh` | `BaseMMU *mmu;\n    using LSQRequest = LSQ::LSQRequest;` | after |
| `CPU_INIT` | `src/cpu/o3/cpu.cc` | `fetch.setActiveThreads(&activeThreads);` | before |
| `CPU_PARAM_IMPORT` | `src/cpu/o3/BaseO3CPU.py` | `from m5.objects.FUPool import *` | after |
| `CPU_PARAM` | `src/cpu/o3/BaseO3CPU.py` | `needsTSO = Param.Bool(False, "Enable TSO Memory model")` | after |
| `SCONS` | `src/cpu/o3/SConscript` | `Source('rob.cc')` | after |
| `IEW_DISPATCH` | `src/cpu/o3/iew.cc` | `} else if (inst->isNop()) {` | before |
| `IEW_SQUASH` | `src/cpu/o3/iew.cc` | `ldstQueue.squash(fromCommit->commitInfo[tid].doneSeqNum, tid);` | after |
| `IQ_ALLOC` | `src/cpu/o3/inst_queue.cc` | `InstructionQueue::insert(const DynInstPtr &new_inst)\n{` | after |
| `IQ_ALLOC_NONSPEC` | `src/cpu/o3/inst_queue.cc` | `InstructionQueue::insertNonSpec(const DynInstPtr &new_inst)\n{` | after |
| `FU_GRANT` | `src/cpu/o3/inst_queue.cc` | `int idx = FUPool::NoCapableFU;` | before |
| `LSQ_ALLOC` | `src/cpu/o3/lsq_unit.cc` | `LSQUnit::insert(const DynInstPtr &inst)\n{` | after |
| `LSQ_RESP_INTERCEPT` | `src/cpu/o3/lsq.cc` | `LSQ::DcachePort::recvTimingResp(PacketPtr pkt)\n{` | after |
| `RENAME_INVARIANTS` | `src/cpu/o3/rename.cc` | `renameDestRegs(inst, inst->threadNumber);` | after |
| `ROB_ALLOC` | `src/cpu/o3/rob.cc` | `ROB::insertInst(const DynInstPtr &inst)\n{` | after |
| `ISA_FORMAT_INCLUDE` | `src/arch/riscv/isa/formats/formats.isa` | `##include "vector_mem.isa"` | after |
| `ISA_DECODE` | `src/arch/riscv/isa/decoder.isa` | `    0x3: decode OPCODE5 {` | after |

Plus four files copied in verbatim and never edited:

```
src/cpu/o3/prefetch_hint_queue.hh
src/cpu/o3/prefetch_hint_queue.cc
src/cpu/o3/PrefetchHintQueue.py
src/arch/riscv/isa/formats/hint_gather.isa
```

When an anchor is not found, the patcher aborts before writing anything and
prints the region id, the file, the purpose of the region, each anchor
candidate with its match count, and the closest existing line in the file
(via `difflib`). That message is written for a repair agent to act on: it can
fix one `anchors` list and re-run.

---

## Required stat names (DESIGN.md sec 4.2)

Do not rename these. The CHIA loop parses them by exact string.

```
system.cpu.phq.hintsDispatched
system.cpu.phq.hintsDropped
system.cpu.phq.hintsDroppedMshr
system.cpu.phq.hintsDroppedTimeout
system.cpu.phq.hintsDroppedSquash
system.cpu.phq.hintsDroppedFull
system.cpu.phq.prefetchesIssued
system.cpu.phq.prefetchesLate
system.cpu.phq.occupancyAvg
system.cpu.phq.iqEntriesAllocated     # MUST be 0
system.cpu.phq.lsqEntriesAllocated    # MUST be 0
system.cpu.phq.fuPortCycles           # MUST be 0
```

Additional counters this implementation emits, not part of the sec 4.2 contract:

```
system.cpu.phq.robEntriesAllocated    # positive control, MUST be > 0
system.cpu.phq.chaseLoadsIssued
system.cpu.phq.hintsUndecodable
system.cpu.phq.translationFailures
system.cpu.phq.portBlocked
system.cpu.phq.prefetchesRequestedL2
```

The `system.cpu.phq.` prefix is not hard-coded anywhere. It falls out of
attaching the SimObject to the CPU under the Python attribute name `phq`;
gem5's `_bindStatHierarchy()` derives stat paths from attribute names.
Renaming that attribute in `configs/hint_gather_se.py` or in the `CPU_PARAM`
patch region silently renames every stat.

---

## Deviations from DESIGN.md, and interpretations

None of these change an interface DESIGN.md defines. They are places where
DESIGN.md was silent or ambiguous and a choice had to be made.

1. **Fan-out addressing.** DESIGN.md sec 1.1 says the hint "steps the index
   address by the element stride", while sec 2 step 3 gives the explicit formula.
   sec 2 is treated as normative:
   `target_k = base + (index << shift) + k * (1 << shift)` for
   `k ∈ [0, fanout]`, i.e. `fanout + 1` prefetches.

2. **`prefetchesLate` definition.** `BaseCache::mshrQueue` is `protected`, so
   the classic cache-side lateness signal is unreachable from the CPU. Here a
   prefetch counts as late if it is **still in flight when a demand response
   for the same cache block returns on the shared D-cache port**. This is
   observable with zero extra patch surface because the PHQ already inspects
   every response on that port. It is a lower bound on true lateness.

3. **Shared D-cache port, not a private one.** DESIGN.md sec 2 places the PHQ
   "adjacent to the LSQ". It sends on the LSQ's existing `RequestPort` rather
   than owning its own. A private port would need config wiring and would
   silently hand the core a free extra cache port that `docs/AREA.md` does not
   pay for.

4. **No retry queue.** The LSQ owns the retry slot on that shared port; a
   second claimant would corrupt gem5's retry protocol. When
   `sendTimingReq()` returns false the PHQ **drops** the request (legal under
   DESIGN.md sec 1.3.4), counts `portBlocked`, and treats it as the definitive
   in-band MSHR-pressure signal. `recvReqRetry` is therefore not patched.

5. **`Param.String` rather than gem5 `Enum`** for `wakeup_policy`,
   `tlb_miss_policy` and `prefetch_level`. gem5 enums need an `enums=[...]`
   SConscript keyword and generate headers whose spelling drifts between
   releases. The strings are parsed once in the C++ constructor and
   `fatal()` with a message citing DESIGN.md sec 3 on anything unrecognised.

6. **`prefetch_level` is advisory.** The classic memory system has no
   "prefetch into L2 only" request. `L2C` is modelled by issuing the request
   and counting it in `prefetchesRequestedL2`; the block still lands in L1D on
   response. ChampSim models the level faithfully; gem5 does not.

7. **Non-genome parameters added**: `scoreboard_read_ports` (default 1),
   `adder_throughput` (default 1), `l1d_mshrs` (derived from the configured
   L1D, never set independently). Defaults reproduce DESIGN.md behaviour
   exactly, so the genome space is unchanged.

8. **`--cpu-type atomic` has no caches and no PHQ.** It exists only as the
   functional reference for the correctness gate.

9. **`rob.cc` is patched** even though the ROB is not part of the PHQ. It
   provides `robEntriesAllocated`, the positive control described above.

---

## If the build fails

These are the five things most likely to break first, in order of
probability, with the fix for each.

### 1. `ADD_STAT` is undeclared -> you are on gem5 develop, not v23

gem5 renamed `ADD_STAT` to `GEM5_ADD_STAT` after v23. Symptom:

```
error: 'ADD_STAT' was not declared in this scope
```

Fix: in `src/cpu/o3/prefetch_hint_queue.cc`, replace `ADD_STAT(` with
`GEM5_ADD_STAT(` throughout the `PHQStats` constructor initialiser list. There
are 18 occurrences and nothing else changes.

### 2. `LSQ::LSQRequest` / `LSQ::DcachePort` moved

gem5 develop hoists `LSQRequest` out of `LSQ` to namespace scope and the
`CPU_MEMBERS` anchor
`BaseMMU *mmu;\n    using LSQRequest = LSQ::LSQRequest;` will not match. The
patcher falls back to the second candidate (`BaseMMU *mmu;`) automatically, so
apply will succeed -- but if `LSQ::DcachePort::recvTimingResp` was also renamed
or re-signatured, region `LSQ_RESP_INTERCEPT` will fail to find its anchor and
the patcher will abort with the exact candidate list. Fix: update that one
`anchors` entry to the local spelling.

Related runtime symptom if the intercept is missing or mis-placed:

```
panic: Got packet back with unknown sender state
```

That is `LSQ::recvTimingResp` seeing a PHQ packet. It means the intercept was
inserted after, rather than at the top of, `DcachePort::recvTimingResp`.

### 3. `Unknown instruction flag 'IsHintGather'` from the ISA parser

The ISA parser validates flag names against the generated
`StaticInstFlags` enum. If region `STATIC_INST_FLAG` did not apply (check with
`--check`), or if your tree generates that enum from a different file, the
parse of `hint_gather.isa` fails. Fix: confirm `"IsHintGather",` is present in
`src/cpu/StaticInstFlags.py`, then `scons --clean` the ISA-generated
directory (`build/RISCV/arch/riscv/generated/`) because the parser output is
cached aggressively and will not regenerate on a header-only change.

### 4. Decode conflict on `OPCODE5 0x02`

Symptom at build time:

```
error: duplicate case value in decode block
```

`OPCODE5 == 0x02` inside `QUADRANT == 0x3` (i.e. opcode `0x0B`, custom-0) is
free in both v23 and develop, but a local patch or a vendor tree may already
use it. Fix: pick another free custom opcode (`custom-1` is
`OPCODE5 0x0A`), change the `0x02:` case in the `ISA_DECODE` body in
`apply_phq.py`, and change the matching encoding in the LLVM component --
DESIGN.md sec 1.1 fixes the encoding, so this requires a DESIGN.md amendment and
coordination with the other agents, not a unilateral edit.

### 5. `Param.PrefetchHintQueue` unresolved, or stats appear at the wrong path

Two variants:

* `NameError: name 'PrefetchHintQueue' is not defined` when elaborating the
  config -- the `SimObject('PrefetchHintQueue.py', ...)` line did not make it
  into `src/cpu/o3/SConscript` (region `SCONS`), or the `CPU_PARAM_IMPORT`
  region did not apply. Run `--check`.
* Stats appear as `system.cpu.prefetch_hint_queue.*` or at the top level
  instead of `system.cpu.phq.*` -- the SimObject was attached under a
  different attribute name. The attribute must be exactly `phq`, in both
  `BaseO3CPU.py` (region `CPU_PARAM`) and `configs/hint_gather_se.py`.

### Honourable mentions

* Every inferred API in the C++ is marked with a `// TODO(verify):` comment
  naming the exact symbol. Grep for `TODO(verify)` before the first build;
  that list is deliberately the complete set of places where the code was
  written against documented behaviour rather than a compiler.
* `statistics::units::Cycle::get()` is spelled `Cycle` (singular) in v23. Some
  trees use `Tick`.
* If the RISC-V build reports the new format file as unparsed, confirm
  `##include "hint_gather.isa"` landed in `formats/formats.isa` and not in
  `templates/`. The `Basic*` templates live in `formats/basic.isa`, not
  `templates/`, which is a common source of confusion when adding a format.
