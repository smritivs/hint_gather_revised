# HINT.GATHER: Zero-Issue Prefetching for Irregular Workloads

**Hardware-software co-design of a custom RISC-V indirect prefetch instruction, an LLVM 17 compiler pass, and a zero-issue Out-of-Order Prefetch Hint Queue (`PHQ`), with a tuning loop built on the CHIA framework.**

---

## Overview: Why `HINT.GATHER`?

Hardware stride and stream prefetchers are effective on regular strides (`B[i]`), but fail on data-dependent indirect memory accesses (`A[B[i]]`) that dominate graph traversal (`BFS`, `PageRank`), sparse linear algebra, and pointer-chasing workloads. Compiler-inserted software prefetching (`__builtin_prefetch(&A[B[i+d]])`) fixes cache miss coverage, but on an Out-of-Order (OoO) processor a software prefetch is still a **first-class load instruction**: it consumes a Decode/Rename slot, an Issue Queue (`IQ`) entry, a Functional Unit (`FU`) address-generation port, and a Load-Store Queue (`LSQ`) ordering CAM slot. In our `gem5` runs, software prefetching executes 32-91% more instructions than the baseline and is slower than it on the gathers (software prefetches are emulated with byte loads; see [`docs/RESULTS.md`](docs/RESULTS.md)).

**`HINT.GATHER`** solves this by co-designing the compiler pass, the ISA encoding, and the OoO dispatch path together:
1. **RISC-V `custom-0` Instruction (`opcode 0x0B`)**: A single 32-bit R-type hint (`rd=x0`) supporting both Value mode (`HINT.GATHER`) and Chase mode (`HINT.GATHER.C`, where `rs2` passes the lookahead address `&B[i+d]`, computed from the loop counter).
2. **Zero-Issue Dispatch Bypass & Prefetch Hint Queue (`PHQ`)**: At dispatch in `gem5`'s `RiscvO3CPU`, `HINT.GATHER` takes a Reorder Buffer (`ROB`) entry, so it is squashed on mispredicted paths and commits in order, and is marked executed at once. Its operands go to a 32-entry PHQ beside the L1D (about 3.33 KiB of storage, an analytical estimate). It uses **zero Issue Queue entries (`iq=0`), zero Load-Store Queue entries (`lsq=0`), and zero Functional Unit cycles (`fu=0`)**, which is checked in every run.
3. **LLVM Pass & Index-Line Buffer**: An out-of-tree LLVM 17 pass (`libHintGather.so`) uses `ScalarEvolution` (`SCEV`) to keep only non-affine gather sites and emits one `fanout=8` hint per loop body (the benchmark loops are unrolled x8 by a source pragma in every build variant). In CSR edge loops it clamps the lookahead to the current vertex's edges. Inside the PHQ, a 4-line buffer holds 64-byte index lines (16 indices each; a zero-latency read in the model): 8,201 hints needed 4,104 line reads on the 8K gather. The PHQ sends up to 8 prefetches per cycle and retries the ones a busy L1D refuses.
4. **CHIA Co-Design Loop**: `hint_gather_loop.py` (nodes N0-N6) builds, gates and scores candidates over a 14-knob compiler/microarchitecture `Genome`. The two-stage correctness gate requires output identical to `gem5`'s atomic CPU and `iq == lsq == fu == 0`. Our run was small (population 3, two generations) and did not beat the hand-written default, so the reported parameters come from declared sweeps.

**Results (`gem5`, means over five binary layouts):** `HINT.GATHER` is **2.23x** faster than an equally unrolled baseline on a 768 KiB gather, about 1.12x faster on a 12 MiB gather, and 1.06x / 1.24x faster on BFS with 8,192 / 65,536 vertices. It gives no gain (0.998x) when the data fits in the L2. Details, ablations and limitations are in [`docs/RESULTS.md`](docs/RESULTS.md).

---

## Repository Structure

```text
hint_gather_revised/
├── run_all.sh              # Cross-platform 1-command runner & modular stage CLI
├── Makefile                # Wrapper targets (make all, make setup, make verify, etc.)
├── Dockerfile              # Linux container environment for macOS / Docker users
├── env.yml                 # Conda environment specification (Python 3.10, GCC 12, SCons)
├── hint_gather_loop.py     # CHIA co-design loop (nodes N0-N6)
├── config.py               # Automatic toolchain & path discovery
├── genome.py               # 14-knob compiler + PHQ hardware Genome
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
| `./run_all.sh evolve` | `make evolve` | Launch the CHIA co-design loop, nodes N0-N6 (`hint_gather_loop.py --stage full`) |

### Custom Benchmark Sizes & Iterations
```bash
./run_all.sh verify --size 16384 --iters 3
./run_all.sh verify --size 16384 --iters 3 --fanout 4
```

---

## Expected Output (`report.txt`)

Running `./run_all.sh` or `./run_all.sh verify` prints and saves a side-by-side `gem5` comparison (`RiscvO3CPU` + `L2 StridePrefetcher(degree=4)`) of `base` (no software prefetch), `swpf` (software prefetching, emulated with byte loads) and `hint gather` for the 8K and 64K gathers. For each variant it lists dynamic instructions, cycles, L1D demand misses and demand MSHR misses, the PHQ counters, the `iq / lsq / fu` check and the output checksum.

The paper's numbers for these two workloads (means over five binary layouts, fastest software-prefetch distance):

| Workload | Base cycles | SWPF cycles | HINT cycles | HINT vs. Base | Demand MSHR misses |
|---|---:|---:|---:|---:|---:|
| Gather 8K (96 KiB) | 399,211 | 498,170 | 400,071 | 0.998x | -86.1% |
| Gather 64K (768 KiB) | 5,872,791 | 7,424,008 | 2,633,746 | 2.230x | -77.0% |

A single `verify` run uses one binary layout and one software-prefetch distance, so its numbers are close to these but not equal to them.

---

## Documentation

- **[`docs/DESIGN.md`](docs/DESIGN.md)**: Normative ISA encoding (`0x0B`), PHQ microarchitectural specification, LLVM pass contract, and CHIA fitness formulation.
- **[`docs/RESULTS.md`](docs/RESULTS.md)**: `gem5` O3 results for the three gather sizes and two BFS graphs, with ablations, parameter sweeps and limitations.
