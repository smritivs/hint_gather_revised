# HINT.GATHER — Verified `gem5` O3 Results & Report Guide

> [!IMPORTANT]
> **Status: `GATE: PASS`** — Verified on RISC-V `RiscvO3CPU` + `PrefetchHintQueue` (`PHQ`) + L2 `StridePrefetcher(degree=4)`.
>
> You can generate side-by-side comparison reports (`report.txt` and `runs/report.txt`) for **any array size (`--size`), iteration count (`--iters`), and fanout (`--fanout`)** using:
> ```bash
> ./run_all.sh verify                                   # Default 8K + 64K suite
> ./run_all.sh verify --size 16384 --iters 3            # Custom size & iters
> ./run_all.sh verify --size 16384 --iters 3 --fanout 4 # Custom size, iters & fanout
> ```

---

## 1. Column Definitions in `report.txt`

| Column Name | Binary Executed | Description |
|---|---|---|
| **`base (No PF)`** | `gather.base.elf` | Normal baseline program **without software or `HINT.GATHER` prefetching** (runs with hardware L2 stride prefetcher only). |
| **`swpf (SW PF)`** | `gather.swpf.elf` | Standard software prefetching (`__builtin_prefetch`), which allocates Issue Queue (IQ) and Load-Store Queue (LSQ) entries. |
| **`hint gather`** | `gather.hint.elf` | Co-designed `HINT.GATHER` custom-0 instruction + Prefetch Hint Queue (`PHQ`) with 64B `ChaseLine` buffer, strip-mined `fanout`, and MSHR quota cap. |
| **`Improvement`** | Delta (`hint gather` vs `base` / `swpf`) | Cycle reduction, dynamic instruction savings, L1D MSHR miss reduction, and zero-issue structural check (`iq=0, lsq=0, fu=0`). |

---

## 2. Verified `gem5` O3 Results (`8K`, `64K`, and `GAP-BFS`)

### 2.1 `8K` Elements (`--size 8192 --iters 8`, `96 KiB` L2-Resident Working Set)

| Hardware Metric (`gem5` O3 `stats.txt`) | `base (No PF)` | `swpf (SW PF)` | `hint gather` | Improvement |
|---|---:|---:|---:|---|
| **`simInsts` (Dynamic Instructions)** | `1,208,611` | `1,331,504` | **`1,044,801`** | **`-13.55%` insts (`-163,810` vs `base`, `-21.53%` vs `swpf`)** |
| **`system.cpu.numCycles` (Total Cycles)** | `500,241` | `500,669` | **`445,339`** | **`-54,902` cyc (`+10.98%` / `1.123×` vs `base`, `+11.05%` / `1.124×` vs `swpf`)** |
| **`dcache.demandMisses::total`** | `63,557` | `63,630` | **`27,597`** | **`-35,960` misses (`-56.58%` of all L1D demand misses eliminated)** |
| **`dcache.demandMshrMisses::total`** | `44,280` | `44,260` | **`6,173`** | **`-86.06%` (`-38,107` MSHR misses eliminated)** |
| **`phq.hintsDispatched`** | `0` | `0` | **`8,201`** | **`100%` (`8,201 / 8,201`)** |
| **`phq.chaseLoadsIssued` (Index Loads)** | `0` | `0` | **`4,101`** | **`ChaseLine` buffered (`16.0×` fewer)** |
| **`phq.prefetchesIssued`** | `0` | `0` | **`65,440`** | **Active PHQ stream (`wasted_rate = 0.00003`)** |
| **`PHQ Structural (iq / lsq / fu)`** | `0 / 0 / 0` | `(nonzero)` | **`0 / 0 / 0`** | **`PASS` (Zero-Issue)** |
| **Architectural Checksum** | `0xa279a8f479dab697` | `0xa279a8f479dab697` | `0xa279a8f479dab697` | **`EXACT MATCH`** |

---

### 2.2 `64K` Elements (`--size 65536 --iters 4`, `768 KiB` DRAM-Spilling Working Set)

| Hardware Metric (`gem5` O3 `stats.txt`) | `base (No PF)` | `swpf (SW PF)` | `hint gather` | Improvement |
|---|---:|---:|---:|---|
| **`simInsts` (Dynamic Instructions)** | `5,910,903` | `6,369,664` | **`5,255,561`** | **`-11.09%` insts (`-655,342` vs `base`, `-17.49%` vs `swpf`)** |
| **`system.cpu.numCycles` (Total Cycles)** | `2,716,193` | `2,833,816` | **`2,553,537`** | **`-162,656` cyc (`+5.99%` vs `base`, `-280,279` cyc / `+9.89%` / `1.110×` vs `swpf`)** |
| **`dcache.demandMisses::total`** | `441,350` | `440,570` | **`222,541`** | **`-218,809` misses (`-49.58%` of all L1D demand misses eliminated)** |
| **`dcache.demandMshrMisses::total`** | `321,908` | `321,912` | **`73,782`** | **`-77.08%` (`-248,126` MSHR misses eliminated)** |
| **`phq.hintsDispatched`** | `0` | `0` | **`32,773`** | **`100%` (`32,773 / 32,773`)** |
| **`phq.chaseLoadsIssued` (Index Loads)** | `0` | `0` | **`16,386`** | **`ChaseLine` buffered (`15.6×` fewer)** |
| **`phq.prefetchesIssued`** | `0` | `0` | **`256,089`** | **Active PHQ stream** |
| **`PHQ Structural (iq / lsq / fu)`** | `0 / 0 / 0` | `(nonzero)` | **`0 / 0 / 0`** | **`PASS` (Zero-Issue)** |
| **Architectural Checksum** | `0xa01b22bcd093f9f4` | `0xa01b22bcd093f9f4` | `0xa01b22bcd093f9f4` | **`EXACT MATCH`** |

---

### 2.3 `GAP-BFS` (`bfs`, CSR Graph Frontier Traversal, CHIA Node 6 Validation)

| Hardware Metric (`gem5` O3 `stats.txt`) | `swpf (SW PF)` | `hint gather` | Improvement |
|---|---:|---:|---|
| **`system.cpu.numCycles` (Total Cycles)** | `15,573,824` | **`14,062,272`** | **`-1,511,552` cyc (`+9.71%` cycle reduction / `1.107×` speedup vs `swpf`)** |
| **`dcache.demandMisses::total`** | `826,849` | **`810,992`** | **`-15,857` L1D demand misses eliminated** |
| **`PHQ Structural (iq / lsq / fu)`** | `(nonzero)` | **`0 / 0 / 0`** | **`PASS` (Zero-Issue)** |
| **Architectural Checksum** | `0x168d13b4795d8fb1` | `0x168d13b4795d8fb1` | **`EXACT MATCH`** |
