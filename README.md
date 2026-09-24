# HINT.GATHER: Zero-Issue Prefetching for Irregular Workloads

**Hardware-software co-design of a custom RISC-V indirect prefetch instruction, an LLVM 17 compiler pass, and a zero-issue Out-of-Order Prefetch Hint Queue (`PHQ`), tuned via the CHIA evolutionary framework.**

---

## Overview: Why `HINT.GATHER`?

Hardware stride and stream prefetchers are effective on regular strides (`B[i]`), but fail on data-dependent indirect memory accesses (`A[B[i]]`) that dominate graph traversal (`BFS`, `PageRank`), sparse linear algebra, and pointer-chasing workloads. Compiler-inserted software prefetching (`__builtin_prefetch(&A[B[i+d]])`) fixes cache miss coverage, but on an Out-of-Order (OoO) processor a software prefetch is still a **first-class load instruction**: it consumes a Decode/Rename slot, an Issue Queue (`IQ`) entry, a Functional Unit (`FU`) address-generation port, and a Load-Store Queue (`LSQ`) ordering CAM slot. On pointer-chasing loops, auxiliary address-calculation and prefetch instructions inflate dynamic instruction count by >10% and trade memory-latency stalls for structural pipeline stalls.

**`HINT.GATHER`** solves this by co-designing the compiler pass, the ISA encoding, and the OoO dispatch path together:
1. **RISC-V `custom-0` Instruction (`opcode 0x0B`)**: A single 32-bit R-type hint (`rd=x0`) supporting both Value mode (`HINT.GATHER`) and Chase mode (`HINT.GATHER.C`, where `rs2` passes the affine lookahead address `&B[i+d]` that is 100% ready at Rename/Dispatch).
2. **Zero-Issue Dispatch Bypass & Prefetch Hint Queue (`PHQ`)**: At Rename/Dispatch in `gem5`'s `RiscvO3CPU`, `HINT.GATHER` allocates a Reorder Buffer (`ROB`) entry to preserve precise exceptions and in-order commit, hands its descriptor directly to a **0.57 KiB PHQ sidecar** beside L1D, and immediately marks its ROB slot executed—consuming **zero Issue Queue entries (`iq=0`), zero Load-Store Queue entries (`lsq=0`), and zero Functional Unit cycles (`fu=0`)**.
3. **8-Wide Strip-Mined `ChaseLine` Coalescing**: An out-of-tree LLVM 17 pass (`libHintGather.so`) uses `ScalarEvolution` (`SCEV`) to filter out regular affine strides and strip-mines indirect gathers by `fanout=8`. Inside the PHQ, a 64-byte `ChaseLine` buffer reads a single L1D index line and autonomously drains 8 target prefetches at 8 addresses/cycle (`16×` fewer index reads than scalar software prefetching).
4. **6-Node CHIA Evolutionary Loop**: Searches the 13-parameter compiler/microarchitecture `Genome` under a strict two-stage correctness gate (architectural `CHECKSUM` equivalence + `iq == lsq == fu == 0` structural assertions), achieving **1.124×–1.153× speedup** on indirect gathers (`-56.58%` L1D demand misses, `-86.06%` L1D MSHR misses) and **1.107× speedup** on `GAP-BFS`.

---

## Repository Structure

```text
hint_gather_revised/
├── run_all.sh              # Cross-platform 1-command runner & modular stage CLI
├── Makefile                # Wrapper targets (make all, make setup, make verify, etc.)
├── Dockerfile              # Linux container environment for macOS / Docker users
├── env.yml                 # Conda environment specification (Python 3.10, GCC 12, SCons)
├── hint_gather_loop.py     # 6-node CHIA co-design orchestrator
├── config.py               # Automatic toolchain & path discovery
├── genome.py               # 13-parameter compiler + PHQ hardware Genome
├── runner.py               # Subprocess runner & SQLite telemetry store
├── llvm/                   # Out-of-tree LLVM 17 pass plugin (HintGatherPass.cpp)
├── gem5/                   # Idempotent gem5 v24.0 O3 PHQ patcher (apply_phq.py) & C++ sources
├── champsim/               # Sidecar prefetcher template & C++17 unit test (test_sidecar.cc)
├── bench/                  # RISC-V & host benchmarks (gather, bfs, pagerank, listchase)
├── nodes/                  # CHIA pipeline nodes (llvm, gem5, champsim, agents, evolve)
├── prompts/                # Agent implementation & repair prompts
├── scripts/                # setup_env.sh, verify_all.py, smoke_test.sh, gen_traces.sh
└── docs/                   # Design specification (DESIGN.md) & verified results (RESULTS.md)
```

---

## Prerequisites & Installation

### Complete List of Project Prerequisites

| Prerequisite | Version | Purpose |
| :--- | :--- | :--- |
| **Host Build Utilities** | `git`, `curl`, `wget`, `tar`, `xz`, `make`, `g++` | Downloading archives & compiling host test binaries |
| **Conda (`Miniforge3`) + `env.yml`** | Python 3.10, GCC 12.4, `cmake`, `ninja`, `scons`, `lld` | Isolated build toolchain & `gem5` SCons build environment |
| **LLVM + Clang** | `17.0.6` | Building the out-of-tree `libHintGather.so` pass & cross-compiling RV64GC IR |
| **RISC-V GNU Toolchain** | `riscv64-unknown-elf-gcc 13.2.0` | Assembling `.insn r 0x0b` and linking static RISC-V `.elf` binaries |
| **`gem5` Simulator** | `v24.0.0.1` | Cycle-accurate `RiscvO3CPU` + Prefetch Hint Queue (`PHQ`) simulation |
| **`ChampSim`** | `master` (C++17) | Fast trace-driven & standalone sidecar prefetcher evaluation |

---

### Step 1: Install Host OS Packages (All Operating Systems)

#### Ubuntu / Debian (`20.04` / `22.04` / `24.04`)
```bash
sudo apt update
sudo apt install -y build-essential git curl wget tar xz-utils python3 python3-pip ca-certificates pkg-config zlib1g-dev
```

#### Arch Linux / Manjaro
```bash
sudo pacman -Syu --needed base-devel git curl wget tar xz python python-pip ca-certificates pkgconf zlib
```

#### Fedora / RHEL / Rocky Linux / AlmaLinux
```bash
sudo dnf groupinstall -y "Development Tools"
sudo dnf install -y git curl wget tar xz python3 python3-pip ca-certificates pkgconf-pkg-config zlib-devel
```

#### Windows (`WSL2` — Ubuntu 22.04/24.04)
```powershell
# Run in Administrator PowerShell:
wsl --install -d Ubuntu-22.04
```
Then open the Ubuntu WSL2 terminal and run the **Ubuntu / Debian** `apt` commands above.

#### macOS (Apple Silicon `M1`/`M2`/`M3`/`M4` & Intel)
Because `gem5` and the prebuilt ELF toolchains target Linux `glibc`, install **OrbStack** (or Docker Desktop with Rosetta 2 enabled); `./run_all.sh` automatically mounts the repo inside a transparent `linux/amd64` container:
```bash
brew install git curl wget xz
brew install --cask orbstack
```

---

### Step 2: Install Toolchain & Simulator Prerequisites (`~/hg-tools`)

You can install **all 5 toolchains/simulators automatically** with a single command:
```bash
./run_all.sh setup
# or: make setup
```

Or, if you prefer to install each prerequisite **manually step-by-step**, run the following commands:

#### 1. Conda (`Miniforge3`) & the `hint-gather` Environment (`env.yml`)
```bash
mkdir -p ~/hg-tools
curl -L "https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-$(uname -m).sh" -o /tmp/miniforge.sh
bash /tmp/miniforge.sh -b -p ~/hg-tools/conda
~/hg-tools/conda/bin/conda env create -f env.yml -n hint-gather
conda activate ~/hg-tools/conda/envs/hint-gather
```

#### 2. Prebuilt LLVM + Clang `17.0.6` (with CMake Headers for `libHintGather.so`)
```bash
wget -qO /tmp/llvm17.tar.xz \
  "https://github.com/llvm/llvm-project/releases/download/llvmorg-17.0.6/clang+llvm-17.0.6-x86_64-linux-gnu-ubuntu-22.04.tar.xz"
mkdir -p ~/hg-tools/llvm-17
tar -xJf /tmp/llvm17.tar.xz -C ~/hg-tools/llvm-17 --strip-components=1
```

#### 3. RISC-V 64-Bit Bare-Metal GNU Toolchain (`riscv64-unknown-elf-gcc`)
```bash
wget -qO /tmp/riscv64.tar.gz \
  "https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/v13.2.0-2/xpack-riscv-none-elf-gcc-13.2.0-2-linux-x64.tar.gz"
mkdir -p ~/hg-tools/riscv64-unknown-elf
tar -xzf /tmp/riscv64.tar.gz -C ~/hg-tools/riscv64-unknown-elf --strip-components=1
```

#### 4. `gem5 v24.0.0.1` Simulator
```bash
git clone --branch v24.0.0.1 --depth 1 https://github.com/gem5/gem5.git ~/hg-tools/gem5
```

#### 5. `ChampSim` Simulator
```bash
git clone --depth 1 https://github.com/ChampSim/ChampSim.git ~/hg-tools/champsim
```

---

## Quick Start

### Run Everything with One Command
```bash
./run_all.sh
# or equivalently:
make all
```
This automatically runs all 6 stages (`setup` $\rightarrow$ `llvm` $\rightarrow$ `bench` $\rightarrow$ `gem5` $\rightarrow$ `champsim` $\rightarrow$ `verify`) and writes the side-by-side `gem5` O3 evaluation report to **`report.txt`** and **`runs/report.txt`**.

### Run Individual Stages

| Command | `make` Target | Description |
| :--- | :--- | :--- |
| `./run_all.sh setup` | `make setup` | Download & configure Conda, LLVM 17, RISC-V GCC, `gem5`, and `ChampSim` in `~/hg-tools` |
| `./run_all.sh llvm` | `make llvm` | Build the out-of-tree LLVM 17 pass plugin (`llvm/build/libHintGather.so`) |
| `./run_all.sh bench` | `make bench` | Cross-compile `base`, `swpf`, and `hint` RISC-V `.elf` and host binaries & check checksums |
| `./run_all.sh gem5` | `make gem5` | Apply the PHQ patch (`gem5/apply_phq.py`) and build `build/RISCV/gem5.opt` |
| `./run_all.sh champsim` | `make champsim` | Render the genome and run the standalone C++17 sidecar test (`champsim/test_sidecar.cc`) |
| `./run_all.sh verify` | `make verify` | Run the two-stage correctness gate + `gem5` O3 evaluation and write `report.txt` |
| `./run_all.sh evolve` | `make evolve` | Launch the 6-node CHIA evolutionary co-design loop (`hint_gather_loop.py --stage full`) |

### Custom Benchmark Sizes & Iterations
```bash
./run_all.sh verify --size 16384 --iters 3
./run_all.sh verify --size 16384 --iters 3 --fanout 4
```

---

## Expected Output (`report.txt`)

Running `./run_all.sh` or `./run_all.sh verify` prints and saves the cycle-accurate `gem5` (`RiscvO3CPU` + `L2 StridePrefetcher(degree=4)`) comparison across `base` (no software prefetch), `swpf` (`__builtin_prefetch`), and `hint gather`:

```text
========================================================================================================
  8K Elements (L2-Resident Working Set)
  Workload : gather  |  --size 8192 (96 KiB)  |  --iters 8  |  Genome: variant=chase, fanout=8, distance=64
========================================================================================================
  Hardware Metric (gem5 O3 stats.txt)   |  base (No PF) |  swpf (SW PF) |   hint gather | Improvement       
---------------------------------------+---------------+---------------+---------------+-------------------
  simInsts (Dynamic Instructions)       |     1,208,611 |     1,331,504 |     1,044,801 | -13.55% insts
  system.cpu.numCycles (Total Cycles)   |       500,241 |       500,669 |       445,339 | -54,902 cyc (+10.98%)
  Cycle Savings vs swpf (SW Prefetch)   |            -- |       500,669 |       445,339 | -55,330 cyc (+11.05%)
  dcache.demandMisses::total            |        63,557 |        63,630 |        27,597 | -35,960 misses
  dcache.demandMshrMisses::total        |        44,280 |        44,260 |         6,173 | -86.06% (-38,107)
  phq.hintsDispatched                   |             0 |             0 |         8,201 | 100% (8201/8201)
  phq.chaseLoadsIssued (Index Loads)    |             0 |             0 |         4,104 | ChaseLine buffered
  phq.prefetchesIssued                  |             0 |             0 |        65,440 | Active PHQ stream
  PHQ Structural (iq / lsq / fu)        |     0 / 0 / 0 |     (nonzero) |     0 / 0 / 0 | PASS (Zero-Issue)
  Architectural Checksum Equivalence    | 0xa279a8f479dab697 | 0xa279a8f479dab697 | 0xa279a8f479dab697 | EXACT MATCH
========================================================================================================
```

---

## Documentation

- **[`docs/DESIGN.md`](docs/DESIGN.md)**: Normative ISA encoding (`0x0B`), PHQ microarchitectural specification, LLVM pass contract, and CHIA fitness formulation.
- **[`docs/RESULTS.md`](docs/RESULTS.md)**: Full `gem5` O3 experimental tables across `gather` (`8K`, `64K`) and `GAP-BFS`, plus evolutionary search progression.
