# Repair: the ChampSim prefetcher module will not build

Repair attempt {attempt} of {max_attempts}.

You have a bash tool rooted at `{workdir}` (the ChampSim checkout). The
normative spec is at `{design_doc}` -- section 4.3 defines this module's
contract.

## Context

- Module name: `{module_name}`
- Genome in force:

```json
{genome}
```

## What failed

```
{error}
```

## What to do

1. Check which DPC4-ChampSim API revision this checkout exposes -- look at an
   existing module under `prefetcher/` and match its hook signatures exactly.
   The module is header-only: every method body must be inline.
2. Fix the generated header. The source of truth is the template
   `champsim/hint_gather_prefetcher.h.in` in the project tree -- fix the
   template, not just the rendered copy, or the next candidate will hit the
   same error.
3. Rebuild and confirm the binary appears at `bin/champsim`.

## Constraints

- `prefetcher_final_stats()` must keep printing the `key: value` lines the loop
  parses: `hg_hints_seen`, `hg_prefetches_issued`, `hg_prefetches_dropped`,
  `hg_phq_full_events`, `hg_late_prefetches`.
- Keep the `@@KEY@@` placeholders intact in the template; the renderer fails
  loudly on unknown or leftover placeholders, which is deliberate.
- Do not silently change what the model does in order to make it compile. If a
  hook this model needs does not exist in this ChampSim version, say so.

When you are done, state in one paragraph what the root cause was and what you
changed.
