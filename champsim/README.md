# `champsim/` -- the HINT.GATHER memory-system model

Role: everything CHIA's `ChampSimNode` needs in order to evaluate a candidate
genome quickly in a trace-driven simulator. This is the **fast inner loop**
(Node 4 in `docs/DESIGN.md` sec 5); every pipeline claim in the write-up comes
from gem5 (Node 6), never from here.

Normative spec: [`../docs/DESIGN.md`](../docs/DESIGN.md) -- sec 1 (instruction),
sec 2 (PHQ), sec 3 (genome), sec 4.3 (this contract), sec 6 (known limitations).

## Contents

| File | Role |
|---|---|
| `hint_gather_prefetcher.h.in` | Templated, header-only prefetcher module modelling HINT.GATHER + the PHQ |
| `render.py` | Dependency-free `@@KEY@@` renderer; imported by the CHIA loop |
| `baseline_prefetchers/hg_none.h` | "no prefetching" baseline module |
| `baseline_prefetchers/hg_stride.h` | IP-stride "the hardware already does this" baseline |
| `tracing/README.md` | How to produce the ChampSim traces for `bench/` |

## How CHIA builds it

```python
from champsim.render import render

src = render("champsim/hint_gather_prefetcher.h.in", genome, hint_sites)
node.build_champsim(champsim_root, prefetcher_src=src,
                    module_name="hg_hint_gather", cache_level="L1D")
```

`build_champsim` writes `prefetcher_src` to
`<champsim_root>/prefetcher/<module_name>/<module_name>.h` and builds. The
module is therefore **header-only**: every method body in the template is
inline, there are no out-of-line definitions and no globals with external
linkage.

The two baselines go through the *same* call, so that the baseline and the
candidate differ only in the model:

```python
node.build_champsim(champsim_root,
                    prefetcher_src=open("champsim/baseline_prefetchers/hg_none.h").read(),
                    module_name="hg_none", cache_level="L1D")
```

## Rendering manually

```bash
cd champsim
python3 render.py \
    --template   hint_gather_prefetcher.h.in \
    --genome     ../genomes/gen0_cand3.json \
    --hint-sites ../build/reports/bfs.hint_sites.json \
    --out        /tmp/hg_hint_gather.h

# smoke test: the template compiles stand-alone (no ChampSim required)
g++ -std=c++17 -fsyntax-only /tmp/hg_hint_gather.h
```

`render.py` is intentionally strict -- it raises rather than producing a header
that models the wrong design point:

* `UnknownPlaceholderError` -- the template contains a `@@KEY@@` that is not one
  of the eight in sec 4.3.
* `UnrenderedPlaceholderError` -- a placeholder survived substitution.
* `GenomeError` -- a required genome key is missing or out of its documented
  domain (e.g. `fanout=9`, `variant="chase-ish"`).
* `HintSiteError` -- a site has no resolved PC, or an `elem_size` that the
  instruction cannot encode (must be 1/2/4/8, i.e. `1 << SHIFT`).

## The placeholders (docs/DESIGN.md sec 4.3)

| Placeholder | Genome key | Rendered as | Meaning in the model |
|---|---|---|---|
| `@@HINT_SITES@@` | -- (from `hint_sites.json`) | comma-terminated `{pc, base_reg_hint, elem_size, stride_bytes},` initializers | the compiler's hint-site table; the model only reacts to demand loads whose PC is in it |
| `@@HINT_DISTANCE@@` | `hint_distance` | int, 1..512 | elements of lookahead |
| `@@FANOUT@@` | `fanout` | int, 1..8 | prefetches issued per hint, stepping by `elem_size` (sec 2 step 3) |
| `@@PHQ_ENTRIES@@` | `phq_entries` | int, 2..32 | PHQ depth; when full, hints are dropped and counted |
| `@@DROPPABLE@@` | `droppable` | `true` / `false` | may the hint be dropped under MSHR pressure (funct3 bit 2) |
| `@@MSHR_PRESSURE_THRESHOLD@@` | `mshr_pressure_threshold` | double, 0.0..1.0 | occupancy ratio above which droppable hints are discarded |
| `@@PREFETCH_LEVEL@@` | `prefetch_level` | `L1D` / `L2C` | pasted into `HG_LEVEL_<x>`, so a typo is a compile error |
| `@@VARIANT@@` | `variant` | `value` / `chase` | pasted into `HG_VARIANT_<x>`, likewise |

The hint-site PC must already be **resolved to an address**. The LLVM pass
emits `pc_symbol` (e.g. `__hg_site_0`, sec 4.1); the ChampSim node resolves it
with `nm`/`objdump` and puts the numeric value in the `pc` field before calling
`render()`. `render.py` refuses to guess.

Sites with `"emitted": false` are skipped -- the pass considered them and
declined, so the model must not act on them.

## Stats contract

`prefetcher_final_stats()` prints `key: value` lines, which CHIA parses into
`custom_prefetch_stats`. The five required by sec 4.3 are always present:

```
hg_hints_seen: N
hg_prefetches_issued: N
hg_prefetches_dropped: N
hg_phq_full_events: N
hg_late_prefetches: N
```

plus useful extras: `hg_useful_prefetches`, `hg_prefetch_accuracy`,
`hg_hints_dropped_full`, `hg_hints_dropped_mshr`, `hg_hints_no_prediction`,
`hg_hint_drop_rate`, `hg_phq_occupancy_avg`, `hg_effective_distance`,
`hg_mshr_signal_missing`, and per-site `hg_site<N>_{pc,accesses,issued}`.

The baseline modules print the same five keys (as zeros) so the parser never
has to special-case them.

## Which ChampSim API this targets

Primary target: **ChampSim master after the 2024 module rewrite** -- the
revision used for DPC4: modules are classes deriving from
`champsim::modules::prefetcher`, addresses are `champsim::address`, the access
kind is the scoped enum `access_type`, and prefetches are injected with
`prefetch_line(addr, fill_this_level, metadata)`.

Because the local checkout's exact revision is not known at render time, the
module is written defensively:

1. every hook is a **member template** whose address/set/way/type parameters
   are deduced, so it binds to both the `uint64_t` era and the
   `champsim::address` era and still satisfies ChampSim's `decltype`-based
   hook detection;
2. all contact with ChampSim (address conversion, `prefetch_line`, MSHR
   occupancy) goes through SFINAE shims that degrade to an inert fallback
   instead of failing to compile;
3. if `modules.h` is not found at all the file still compiles -- the model
   (`hg::engine`) is defined and simply has no hooks attached, which makes
   `g++ -fsyntax-only` a valid smoke test and keeps the repair agent's failure
   signal clean;
4. `prefetch_line()` and `intern_` are *protected* in the module base, so the
   shims that touch them are members of the derived class, not free functions.

For a pre-module (DPC3-style) ChampSim, build with `-DHG_CHAMPSIM_LEGACY_API`
to get the `CACHE::prefetcher_*` adaptor at the bottom of the template.

If the build glue insists on a particular class name, the template already
provides the aliases `hint_gather`, `hint_gather_prefetcher`, `hg_hint_gather`,
and honours `-DHG_MODULE_NAME=<name>`.

## Limitations -- read this before quoting any number from here

These are not caveats bolted on afterwards; they are the reason
`docs/DESIGN.md` sec 5.1 penalises hint density and re-anchors the fitness
against gem5.

1. **No pipeline.** ChampSim is trace-driven: no issue queue, no LSQ, no
   rename, no ROB. The entire contribution of HINT.GATHER -- that it consumes
   no IQ entry, no AGU port and no LSQ slot (sec 1.4) -- is *invisible* here. A
   hint is free in this model by construction, so raw ChampSim IPC rewards
   hint spam. Use it for memory-system tuning only.
2. **No data values.** ChampSim traces carry addresses, not contents. The
   model therefore cannot load `B[i+d]` and cannot know the true target
   `A[B[i+d]]`. It recovers the index stream from the last 8 demand addresses
   per site and extrapolates `hint_distance` elements ahead, wrapping the
   result into the address footprint observed for that site. That is exact for
   affine and clustered index streams and deliberately imprecise for fully
   shuffled ones. **`hg_prefetch_accuracy` from this model is a tuning signal,
   not a result.**
3. **No operand readiness.** The `value` vs `chase` distinction (sec 1.2) is a
   dispatch-time property. Here it appears only as effective lookahead:
   `chase` realises the full `hint_distance` (its `rs2` is affine and ready at
   dispatch), `value` realises half of it (it must wait for the core's load of
   `B[i+d]`). That halving is a modelling choice, not a measurement; the real
   cost -- PHQ poll timeouts, scoreboard pressure -- is measured in gem5.
4. **No TLB.** `tlb_miss_policy` is a gem5-only knob; page crossings do not
   drop hints here.
5. **No squashes.** There is no speculation in a trace-driven model, so
   `hintsDroppedSquash` has no counterpart.
6. **Approximate clock.** The PHQ uses the cache clock when
   `prefetcher_cycle_operate()` is available and a pseudo-clock derived from
   the access count otherwise (`hg_cycle_hook_available` reports which).
7. **Approximate pressure signal.** If the local API exposes no MSHR occupancy
   (`get_mshr_occupancy_ratio` / `get_mshr_occupancy` + `get_mshr_size` /
   `get_occupancy` + `get_size`), pressure is approximated by the model's own
   in-flight prefetch count over `HG_MSHR_PROXY_CAPACITY` (default 16).
   `hg_mshr_signal_missing` counts how often that fallback was used -- if it is
   non-zero, say so in the write-up.

## Tuning knobs that are *not* genome keys

Override with `-D` at ChampSim build time if you need to:
`HG_PHQ_SERVICE_CYCLES` (4), `HG_PSEUDO_CYCLES_PER_ACCESS` (4),
`HG_MSHR_PROXY_CAPACITY` (16), `HG_PF_TABLE_BITS` (10),
`HG_LOG2_BLOCK_SIZE` (6), `HG_FORCE_FILL_THIS_LEVEL`,
`HG_STRIDE_DEGREE` / `HG_STRIDE_TABLE_BITS` (baseline).
