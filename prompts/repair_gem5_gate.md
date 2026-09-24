# Repair: the correctness gate failed

Repair attempt {attempt} of {max_attempts}.

You have a bash tool rooted at `{workdir}` (the gem5 checkout). The normative
spec is at `{design_doc}` -- sections 1.3 and 2 define the semantics you must
implement.

## Failure classification: `{failure_kind}`

## Gate output

```
{error}
```

## Genome in force

```json
{genome}
```

## How to interpret each failure kind

**`architectural_divergence`** -- the hinted binary produced a different
`CHECKSUM=` than the unhinted one. This is the most serious failure available:
`HINT.GATHER` is defined to be architecturally a NOP. Something is writing
state. Look for: a destination register being written (rd must be x0), the
chase-variant load being committed instead of discarded, an exception or fault
escaping the PHQ, or the prefetch being issued as a demand access.

**`structural_violation`** -- one of `iqEntriesAllocated`, `lsqEntriesAllocated`
or `fuPortCycles` is non-zero. The instruction is consuming a resource it is
supposed to bypass. Find where it is being allocated and route it to the PHQ
instead. Do not "fix" this by not counting it.

**`no_hints_executed`** -- `hintsDispatched` is zero, so the gate proved nothing.
Either decode is not recognising opcode 0x0B, or dispatch is not routing to the
PHQ, or the binary genuinely contains no hints (check with objdump first, so you
do not go hunting in gem5 for a compiler problem).

## Constraints

- Never edit `gem5/tests/check_arch_equiv.py`, the benchmark checksums, or the
  stat definitions in order to pass. The gate is the only thing standing
  between this project and a meaningless result.
- If you conclude the *spec* is wrong rather than the implementation, stop and
  say so explicitly in your reply instead of changing behaviour.

When you are done, state in one paragraph what the root cause was and what you
changed.
