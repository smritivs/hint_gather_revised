# gem5/

gem5 implementation of the `HINT.GATHER` instruction and the Prefetch Hint
Queue (PHQ) for the RISC-V O3 CPU. Spec: [DESIGN.md §2, §4.2](../docs/DESIGN.md).

The instruction allocates a ROB entry and nothing else — no issue-queue
entry, no LSQ entry, no functional-unit port. The patch enforces this and
exposes counters that the correctness gate checks.

## Layout

| Path | Description |
|---|---|
| `src/cpu/o3/prefetch_hint_queue.{hh,cc}` | PHQ model and stats |
| `src/cpu/o3/PrefetchHintQueue.py` | SimObject; params mirror the genome |
| `src/arch/riscv/isa/formats/hint_gather.isa` | Decoder format for opcode `0x0B` |
| `apply_phq.py` | Idempotent, reversible patcher for a gem5 checkout |
| `configs/hint_gather_se.py` | SE-mode run script |
| `tests/check_arch_equiv.py` | Correctness gate (`GATE: PASS` / `GATE: FAIL`) |
| `docs/AREA.md` | PHQ storage/area estimate |

## Build

Tested against gem5 v24.0.0.1.

```bash
python3 gem5/apply_phq.py --gem5-root $GEM5_ROOT
cd $GEM5_ROOT && scons build/RISCV/gem5.opt -j$(nproc)
# or: ./run_all.sh gem5
```

Patcher options: `--check` (exit 0 applied / 1 not applied / 2 partial),
`--dry-run`, `--revert`. All edits are wrapped in
`BEGIN/END HINT.GATHER (apply_phq.py)` markers, so re-running is a no-op and
`--revert` restores the original tree.

## Run

```bash
$GEM5_ROOT/build/RISCV/gem5.opt gem5/configs/hint_gather_se.py \
    --binary bench/build/gather.hint.elf --options "--size 8192 --iters 8" \
    --cpu-type o3 --phq-entries 32 --prefetch-level L1D
```

Add `--disable-phq` for the baseline. The PHQ stats still appear (as zeros),
so the stats parser needs no special case.

## Correctness gate

```bash
python3 gem5/tests/check_arch_equiv.py \
    --gem5-binary $GEM5_ROOT/build/RISCV/gem5.opt \
    --binary bench/build/gather.hint.elf --options "--size 8192 --iters 8"
```

Checks that:
1. `iqEntriesAllocated`, `lsqEntriesAllocated`, and `fuPortCycles` are all 0;
2. `robEntriesAllocated > 0` and `hintsDispatched > 0` (so hints weren't just dropped at decode);
3. program output matches across O3 + PHQ, the reference, and the atomic CPU.

## Stats

All under `system.cpu.phq.`. The CHIA loop parses these by exact name, so
don't rename them (see DESIGN.md §4.2 for the full list):

```
hintsDispatched  hintsDropped  prefetchesIssued  prefetchesLate  chaseLoadsIssued
iqEntriesAllocated  lsqEntriesAllocated  fuPortCycles   # must be 0
robEntriesAllocated                                     # must be > 0
```

## Troubleshooting

- **Anchor not found:** the patcher aborts before writing anything and prints
  the closest matching line. Update that region's `anchors` in `apply_phq.py`.
- **`ADD_STAT` undeclared:** newer gem5 uses `GEM5_ADD_STAT`; rename it in
  `prefetch_hint_queue.cc`.
- **`Unknown instruction flag 'IsHintGather'`:** check `apply_phq.py --check`,
  then clean `build/RISCV/arch/riscv/generated/`.
- **Stats not under `system.cpu.phq`:** the SimObject must be attached to the
  CPU as `phq`.
