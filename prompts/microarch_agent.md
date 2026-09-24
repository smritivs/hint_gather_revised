# Node 3: microarchitecture agent

You are implementing the Prefetch Hint Queue in a gem5 checkout.

- gem5 checkout: `{gem5_root}`
- Project gem5 sources and patcher: `{hg_gem5_dir}`
- Normative spec: `{design_doc}` (read sections 1.3, 2 and 4.2 in full)

## Task

`{hg_gem5_dir}/apply_phq.py` could not apply cleanly -- almost certainly because
this gem5 version's O3 source layout differs from the one the patcher's anchors
were written against.

1. Inspect the checkout and find the real locations of decode, rename, dispatch,
   the issue queue, the LSQ and the ROB.
2. Update the **anchor strings in `apply_phq.py`** so it applies to this
   version. Fix the patcher, not just this one checkout -- otherwise the next
   worker hits the same wall.
3. Apply it, build (`scons build/RISCV/gem5.opt -j$(nproc)`), and report.

## What you are implementing

A RISC-V `custom-0` instruction that:

- allocates a ROB entry (so commit stays in order and exceptions stay precise),
  is marked complete at dispatch, and therefore never blocks the ROB head;
- is routed at dispatch into a small Prefetch Hint Queue instead of the issue
  queue -- **no IQ entry, no LSQ entry, no functional-unit port, no destination
  register**;
- computes its target on a dedicated adder and issues droppable prefetches;
- is invalidated on squash;
- never faults and never changes architectural state.

Both variants must work: the *value* form (waits for a register operand, using
the configured wake-up policy, dropping if it waits too long) and the *chase*
form (takes the index address, performs the indirection itself, and so never
waits on a register at all).

## Non-negotiable

Wire up and increment `phq.iqEntriesAllocated`, `phq.lsqEntriesAllocated` and
`phq.fuPortCycles` at exactly the points where such an allocation would happen.
The loop asserts these are zero. They are the evidence for the project's central
claim, so an implementation that simply never increments them is worthless -- as
is one that routes the hint through the ordinary load path and lets the numbers
come out zero by accident.

Report what you changed, which files the patcher now touches, and the build
result.
