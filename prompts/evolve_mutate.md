# Node 5: propose the next generation

You are the directed-mutation operator in an evolutionary search over a
hardware/software co-design space. Propose **{count}** new genomes.

The full search space, with types and legal ranges, is in `{design_doc}`
section 3. Read it: proposals outside the legal domain get clamped, which
wastes your suggestion.

## What has been tried so far

{history}

## Current best

```json
{current_best}
```

## How candidates are scored

```
fitness = speedup_vs_software_prefetch
          - {lambda_value} * hints_per_1k_instructions
          - mu * wasted_prefetch_rate
```

Two things follow from this, and they are the whole game:

1. The baseline is **software prefetching**, not "no prefetching". Beating
   nothing is uninteresting; the claim under test is that the hint beats the
   same prefetches issued as ordinary instructions.
2. The density term exists because the fast simulator is structurally blind to
   issue-queue and load-store-queue pressure. More hints will usually look free
   to it and will not be free on real hardware. The penalty coefficient is
   periodically refitted against gem5 timing runs, so it reflects a measured
   cost, not a guess.

## What makes a good proposal

- Change a small number of parameters at a time so the result is attributable.
- Reason from the table: if everything with a large fan-out is losing, stop
  proposing large fan-outs. If the chase variant dominates, explore *around* it
  rather than re-testing the value variant unchanged.
- Failures are information. A candidate that failed the correctness gate tells
  you something about a region of the space; do not simply re-propose it.
- Be willing to propose one genuinely exploratory point. A population that only
  interpolates between known-good points converges early and stays there.

## Output format

Reply with a JSON array of exactly {count} objects and nothing else. Each
object contains only keys from the search space. Example shape:

```json
[
  {{"hint_distance": 48, "fanout": 2, "variant": "chase", "prefetch_level": "L1D",
    "entropy_threshold": 0.3, "droppable": true, "phq_entries": 8,
    "phq_dispatch_width": 2, "phq_poll_limit": 16, "wakeup_policy": "poll_rf",
    "tlb_miss_policy": "drop", "mshr_pressure_threshold": 0.75,
    "max_hints_per_loop": 2, "min_trip_count": 64}}
]
```
