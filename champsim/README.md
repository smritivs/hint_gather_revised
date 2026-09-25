# champsim/

ChampSim prefetcher module that models `HINT.GATHER` and the PHQ. Used as the
fast fitness function in the CHIA loop (Node 4). Spec: [DESIGN.md §4.3](../docs/DESIGN.md).

> [!NOTE]
> ChampSim has no issue queue, LSQ, or ROB, so it cannot see the pipeline
> savings that are the point of `HINT.GATHER`. Use it for ranking candidates
> only. All reported results come from gem5.

## Layout

| Path | Description |
|---|---|
| `hint_gather_prefetcher.h.in` | Header-only prefetcher template with `@@KEY@@` placeholders |
| `render.py` | Fills the template from a genome and `hint_sites.json` |
| `test_sidecar.cc` | Standalone C++17 test of the rendered module (no ChampSim needed) |
| `baseline_prefetchers/` | `hg_none.h` (no prefetch) and `hg_stride.h` (IP-stride) baselines |
| `tracing/` | How to generate traces for `bench/` |

## Usage

```bash
python3 champsim/render.py \
    --template   champsim/hint_gather_prefetcher.h.in \
    --genome     genomes/default.json \
    --hint-sites hint_sites.json \
    --out        champsim/hint_gather_prefetcher.h

# or: ./run_all.sh champsim   (renders and runs test_sidecar.cc)
```

`render.py` raises instead of guessing when a placeholder is unknown, a
genome value is out of range, or a hint site has no resolved PC.

## Placeholders

| Placeholder | Genome key |
|---|---|
| `@@HINT_SITES@@` | from `hint_sites.json` (PCs must already be resolved) |
| `@@HINT_DISTANCE@@` | `hint_distance` |
| `@@FANOUT@@` | `fanout` |
| `@@PHQ_ENTRIES@@` | `phq_entries` |
| `@@DROPPABLE@@` | `droppable` |
| `@@MSHR_PRESSURE_THRESHOLD@@` | `mshr_pressure_threshold` |
| `@@PREFETCH_LEVEL@@` | `prefetch_level` (`L1D` / `L2C`) |
| `@@VARIANT@@` | `variant` (`value` / `chase`) |

## Stats

`prefetcher_final_stats()` prints `hg_hints_seen`, `hg_prefetches_issued`,
`hg_prefetches_dropped`, `hg_phq_full_events`, and `hg_late_prefetches`. The
baselines print the same keys as zeros.

## Limitations

- Traces carry addresses, not data, so the model extrapolates the index
  stream instead of reading `B[i+d]`. Accuracy numbers are approximate.
- `value` vs `chase` is modelled only as half vs full lookahead distance.
- No TLB and no speculation, so `tlb_miss_policy` and squash drops have no effect.
- Targets ChampSim master (post-2024 module API). For older DPC3-style
  ChampSim, build with `-DHG_CHAMPSIM_LEGACY_API`.
