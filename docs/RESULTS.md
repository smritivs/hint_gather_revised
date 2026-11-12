# HINT.GATHER: gem5 results

These are the numbers reported in the A³ Workshop (MICRO 2026) hackathon spotlight paper.

## 1. Setup

| Item | Value |
|---|---|
| Simulator | gem5 v24.0.0.1, syscall-emulation mode, RISC-V O3 CPU at 2 GHz |
| Core | 8-wide fetch, dispatch and commit; ROB 192; IQ 64; LQ/SQ 32/32 |
| L1I, L1D | 32 KiB, 8-way; 32 L1D MSHRs |
| L2 | 256 KiB, 16-way, 32 MSHRs, stride prefetcher (degree 4) |
| Memory | DDR4-2400 (DDR3-1600 as a sensitivity check) |
| Compiler | Clang 17.0.6, `-O2`, RV64GC, static; every variant unrolled ×8 |
| PHQ | 32 entries, 4 hints per cycle, 8 addresses per cycle, port retry limit 64, chase mode, fanout 8 |
| Genomes | gathers: `genomes/default.json` (distance 64, poll limit 32); BFS: `genomes/bfs_tuned.json` (distance 2, poll limit 64) |

Variants compared for every workload:

| Variant | Meaning |
|---|---|
| **Base** | No software prefetching; only the L2 stride prefetcher. Same ×8 unrolling. |
| **SWPF** | Software prefetching of `&A[B[i+D]]` and `&B[i+2D]`. `D` swept over {2, 4, 8, 16, 32, 64}; the fastest is reported. Each prefetch is emulated with a byte load to `x0`, because gem5 executes `prefetch.r` as a no-op. |
| **HINT** | The `HINT.GATHER` binary with the PHQ on. |
| **PHQ-off** | The same HINT binary with `--disable-phq`. |

Workloads:

| Workload | Arguments | Working set |
|---|---|---|
| Gather 8K | `gather --size 8192 --iters 8` | 96 KiB (fits in the L2) |
| Gather 64K | `gather --size 65536 --iters 4` | 768 KiB (spills past the L2) |
| Gather 1M | `gather --size 1048576 --iters 1` | 12 MiB |
| BFS | `bfs --size 8192 --iters 8` | 8,192 vertices, 8 roots |
| BFS-L | `bfs --size 65536 --iters 2` | 65,536 vertices, 2 roots |

The BFS graphs are synthetic CSR graphs with vertex degrees in {8, 16, 64} (mean 14.8).

Every O3 number is the mean over five binary-path layouts (paths padded by 1, 4, 8, 12 and 16 characters). In syscall-emulation mode the path is copied onto the guest stack, so its length changes cycle counts. There are 804 runs in total, and every run prints the same checksum as the atomic CPU.

## 2. Main results

Cycles are whole-program. Speedup is the other variant's cycles divided by HINT's cycles.

| Workload | Base | SWPF (best D) | HINT | PHQ-off | HINT vs. SWPF | HINT vs. Base |
|---|---:|---:|---:|---:|---:|---:|
| Gather 8K (96 KiB) | 399,211 | 498,170 (D=2) | 400,071 | 399,434 | 1.245× | **0.998×** |
| Gather 64K (768 KiB) | 5,872,791 | 7,424,008 (D=4) | 2,633,746 | 5,875,854 | 2.819× | **2.230×** |
| Gather 1M (12 MiB) | 113,015,783 | 152,963,307 (D=2) | 101,147,964 | 114,410,597 | 1.512× | **≈1.12×** |
| BFS (8,192 v.) | 22,717,436 | 22,822,183 (D=2) | 21,392,935 | 22,724,763 | 1.067× | **1.062×** |
| BFS-L (65,536 v.) | 132,156,757 | 125,418,282 (D=2) | 106,804,675 | 132,202,097 | 1.174× | **1.237×** |

HINT relative to Base:

| Workload | Instructions | L1D demand misses | Demand MSHR misses | All MSHR misses | Hints dropped |
|---|---:|---:|---:|---:|---:|
| Gather 8K | +0.8% | −57.7% | −86.1% | −0.5% | 0.3% |
| Gather 64K | +0.6% | −48.8% | −77.0% | −0.2% | 0.4% |
| Gather 1M | +0.3% | −20.9% | −43.5% | −0.0% | 5.7% |
| BFS | +6.5% | −15.2% | −52.8% | +0.5% | 16.4% |
| BFS-L | +4.1% | −25.6% | −57.9% | +0.1% | 37.8% |

- In every run the hints used 0 issue-queue entries, 0 load-store-queue entries and 0 functional-unit cycles.
- **Gather 8K:** the data fits in the L2 and the out-of-order core already hides L2 latency, so there is nothing to gain.
- **Gather 1M:** this run is sensitive to binary layout. HINT vs. Base ranges from 1.06× to 1.17× across layouts (mean 1.117×). HINT vs. PHQ-off for the same binary is a stable 1.13×.
- **BFS-L** was not used to choose any parameter.
- **All MSHR misses** (demand plus prefetch) stay within 0.5% of Base, so the prefetches replace demand misses and add almost no traffic.

## 3. Cost of software prefetching

| | 8K | 64K | 1M | BFS | BFS-L |
|---|---:|---:|---:|---:|---:|
| SWPF instructions vs. Base | +90.9% | +73.4% | +31.7% | +66.5% | +41.8% |
| SWPF speed vs. Base | 0.80× | 0.79× | 0.74× | 1.00× | 1.05× |

The byte-load emulation is pessimistic for software prefetching: a load must complete before it retires, and a real prefetch does not. Base is therefore the fairer reference.

## 4. Ablations

| Change | Workload | Effect |
|---|---|---|
| PHQ on vs. off (same binary) | Gather 64K | 2.231× |
| Port retry, limit 64 vs. 0 | Gather 64K | vs. Base 1.865× → 2.230×; hints losing prefetches to a busy L1D 20.4% → 0.4% |
| Port retry, limit 64 vs. 0 | Gather 1M | vs. Base 1.077× → 1.117×; 36.8% → 5.7% |
| Lookahead clamp on vs. off | BFS | vs. Base 1.042× → 1.062×; all MSHR misses +2.7% → +0.5% |
| Lookahead clamp on vs. off | BFS-L | vs. Base 1.134× → 1.237×; all MSHR misses +6.3% → +0.1% |
| BFS genome vs. gather genome | BFS / BFS-L | 1.025× / 1.132× |

The clamp costs instructions: BFS and BFS-L go from +1.2% and +0.7% to +6.5% and +4.1% over Base.

## 5. Why hints are dropped (% of hints)

| Reason | 8K | 64K | 1M | BFS | BFS-L |
|---|---:|---:|---:|---:|---:|
| L1D stayed busy after retries | 0 | 0.4 | 5.7 | 0.4 | 0.7 |
| Operand not ready within the poll limit | 0 | 0 | 0 | 1.0 | 28.7 |
| Squashed on a mispredicted path | 0 | 0 | 0 | 15.0 | 8.4 |

In BFS the hint's address `&edges[e+d]` depends on the loaded `offsets[u]`, which explains the timeouts. The 0.3% of hints dropped on the 8K gather are failed address translations at the end of the array.

## 6. Parameter sweeps

- **BFS:** 12 points, distance {2, 4, 8, 16, 32, 64} × poll limit {32, 64}, on the 8,192-vertex BFS. The rule was fewest cycles with matching output. The winner is distance 2 with poll limit 64: 1.061× over Base, against 1.037× for the gather settings.
- **Gathers:** the same 12 points. The default (distance 64, poll limit 32) stayed best; every shorter distance was slower and had more demand misses.
- Both chosen distances sit at the edge of the grid. Values beyond them were not tested.
- **CHIA loop run:** population 3, two generations. The hand-written default genome stayed best, so the parameters above come from these sweeps.

## 7. DDR3-1600 sensitivity

HINT vs. Base: 0.997× (8K), 2.007× (64K), 1.086× (1M), 1.059× (BFS), 1.254× (BFS-L).

## 8. Reproducing

```bash
./run_all.sh setup && ./run_all.sh llvm && ./run_all.sh gem5
./run_all.sh verify          # 8K and 64K gather, one layout
```

`./run_all.sh verify` runs one binary layout and one software-prefetch distance, so its numbers are close to the table but not equal to it. From a Makefile build we measured 2.10× vs. Base on the 64K gather and 0.99× on the 8K gather, with the same instruction counts and miss reductions as above.

## 9. Limitations

1. Software prefetching is emulated with byte loads (see section 3).
2. The PHQ's index-line reads take zero time in the model, and PHQ requests and retries are not arbitrated against the core's load ports. Both favour HINT.GATHER.
3. The `level` bit is advisory; every prefetch fills the L1D.
4. Synthetic kernels, single core, syscall-emulation mode, whole-program cycles.
5. PHQ storage (about 3.33 KiB) is an analytical estimate; there is no RTL.
6. The BFS genome was chosen on the BFS input.
7. Binary layout changes cycles by up to 0.54% for Base and HINT, and by 4–10% on the 12 MiB gather.
