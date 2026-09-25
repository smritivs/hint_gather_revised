# champsim/tracing/

How to generate ChampSim traces for the `bench/` binaries. Traces are
optional — the default flow uses `champsim/test_sidecar.cc` and doesn't need them.

## Approach

Trace the native x86 **`base`** build with ChampSim's Pin tracer. The trace
can't contain `HINT.GATHER` (it's a RISC-V instruction); instead the
prefetcher module reacts at the hint-site PCs from `hint_sites.json`.

- Resolve the PCs against the **same x86 binary** you traced, or the model sees zero hints.
- Trace `swpf` separately, since its prefetch instructions change the trace.

## Steps

```bash
make -C bench check                                  # builds bench/build/x86/<bench>.{base,swpf,hint}

cd $HG_CHAMPSIM_ROOT/tracer/pin && make PIN_ROOT=$PIN_ROOT

$PIN_ROOT/pin -t obj-intel64/champsim_tracer.so \
    -o $HG_TRACE_DIR/gather.champsimtrace -s 20000000 -t 100000000 \
    -- bench/build/x86/gather.base --size 1048576 --iters 4
xz -T0 -9 $HG_TRACE_DIR/gather.champsimtrace
```

Or run `scripts/gen_traces.sh` (add `--with-swpf` for the `swpf` traces).
Pin is skipped by default on AMD CPUs; pass `--with-pin` to `setup_env.sh` to install it.

- `-s` skips initialisation. Set it past the `ROI_BEGIN` marker
  (about 20M instructions for `gather`, 36M for `bfs`/`pagerank`).
- `-t 100000000` is a 100M-instruction window, about 6.4 GB raw or a few
  hundred MB after `xz`.

## Sanity checks

1. `hg_none` and `hg_stride` give different L1D MPKI on `gather` (if not, the window is probably still in init).
2. The `HINT.GATHER` module reports `hg_hints_seen > 0` (if not, the PCs don't match the traced binary).
3. `hg_mshr_signal_missing` is 0 (if not, MSHR pressure is being approximated).
