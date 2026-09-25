# bench/

Benchmark suite for `HINT.GATHER`. Each benchmark is plain C99 (no libm,
threads, or file I/O) so it runs in gem5 SE mode. Spec: [DESIGN.md §4.4](../docs/DESIGN.md).

## Benchmarks

| Benchmark | Kernel | Purpose |
|---|---|---|
| `gather.c` | `sum += A[B[i]]`, `B` a random permutation | Cleanest indirect-miss signal |
| `bfs.c` | GAP-style top-down BFS over CSR | Data-dependent control flow, variable trip counts |
| `pagerank.c` | Pull PageRank (Q16 fixed point) | Long, stable trip counts |
| `listchase.c` | Pointer chase, one cache line per node | Zero MLP; only `chase` mode can help |

`common.h` has argument parsing, the RNG, and checksum/ROI helpers.
`graphgen.{c,h}` builds the CSR graph inside the binary from a seed.

## Variants

| Variant | Description |
|---|---|
| `base` | No software prefetching |
| `swpf` | `__builtin_prefetch` on the same targets — the baseline `HINT.GATHER` must beat |
| `hint` | Same source as `base`; hints inserted by the LLVM pass |

## Build

```bash
make -C bench riscv \
    CC=$LLVM_HOME/bin/clang SYSROOT=$RISCV_SYSROOT \
    HG_PLUGIN=../llvm/build/libHintGather.so HG_GENOME=../genomes/default.json
# -> build/<bench>.{base,swpf,hint}.elf

make -C bench check     # native x86 build; checks all three variants give the same checksum
make -C bench help      # all targets and variables
```

`./run_all.sh bench` does all of this with the paths set automatically.

## Running

Every benchmark accepts `--size N`, `--iters N`, and `--seed N`, prints
`ROI_BEGIN`/`ROI_END`, and ends with one `CHECKSUM=0x...` line. A failed
self-check prints `CHECK=FAIL` and exits 1 without a checksum.

Suggested gem5 sizes (the runs used in `docs/RESULTS.md`):

| Benchmark | Arguments |
|---|---|
| `gather` | `--size 8192 --iters 8` (L2-resident), `--size 65536 --iters 4` (spills to DRAM) |
| `bfs`, `pagerank` | `--size 131072 --iters 2` |
| `listchase` | `--size 262144 --iters 8` |

Same `--size`/`--iters`/`--seed` gives the same checksum on x86 and RISC-V
across all three variants, so any `base` vs `hint` difference is a real bug.
