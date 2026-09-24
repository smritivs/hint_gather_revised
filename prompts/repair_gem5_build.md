# Repair: gem5 will not build with the Prefetch Hint Queue applied

Repair attempt {attempt} of {max_attempts}.

You have a bash tool rooted at `{workdir}` (the gem5 checkout). The normative
spec is at `{design_doc}` -- read sections 1.3, 2 and 4.2 before changing
anything.

## What failed

```
{error}
```

## What to do

1. Identify the gem5 version (`git -C {workdir} describe --tags`, check
   `src/base/version.cc` or the RELEASE notes). The patcher was written against
   the v23/v24 O3 layout; source drift is the most likely cause.
2. Fix the compilation error. All HINT.GATHER code lives inside regions
   delimited by `// BEGIN HINT.GATHER (apply_phq.py)` and
   `// END HINT.GATHER (apply_phq.py)` -- keep your edits inside those markers so
   the patch stays revertible and re-appliable.
3. Rebuild: `scons build/RISCV/gem5.opt -j$(nproc)`.

## The invariants you must not break

The entire point of this instruction is what it does *not* consume. Your fix
must preserve all of these:

- `HINT.GATHER` allocates a ROB entry and nothing else.
- It never allocates an issue-queue entry, an LSQ entry, or a functional-unit
  port. The counters `phq.iqEntriesAllocated`, `phq.lsqEntriesAllocated` and
  `phq.fuPortCycles` exist to prove this and must remain wired up and
  incremented at the points where such an allocation would occur.
- It never faults, never writes architectural state, and never participates in
  memory ordering.

If the easiest way to make it compile is to route the instruction through the
normal load path, that is precisely the wrong fix -- it would pass the build,
pass the equivalence check, produce a nice speedup, and measure nothing of
interest.

When you are done, state in one paragraph what the root cause was and what you
changed.
