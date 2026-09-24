#!/usr/bin/env bash
# ===========================================================================
# run_all.sh -- Zero-Debugging Cross-Platform Runner for HINT.GATHER Revised
#
# Usage:
#   ./run_all.sh                  # Runs EVERYTHING: setup -> llvm -> bench -> gem5 -> champsim -> verify
#   ./run_all.sh all              # Same as above
#   ./run_all.sh setup            # Stage 1: Install/verify Conda, LLVM 17, RISC-V, gem5, ChampSim
#   ./run_all.sh llvm             # Stage 2: Build out-of-tree LLVM pass plugin (libHintGather.so)
#   ./run_all.sh bench            # Stage 3: Build all RISC-V & host benchmarks + verify checksums
#   ./run_all.sh gem5             # Stage 4: Apply PHQ patch + build gem5.opt
#   ./run_all.sh champsim         # Stage 5: Render genome + run standalone ChampSim sidecar tests
#   ./run_all.sh verify [args]    # Stage 6: Run end-to-end verification & write report.txt
#                                 #   e.g.: ./run_all.sh verify --size 16384 --iters 4
#   ./run_all.sh evolve           # Stage 7: Run CHIA evolutionary co-design loop
# ===========================================================================

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
STAGE="${1:-all}"

c_info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
c_ok()   { printf '\033[1;32m [OK]\033[0m %s\n' "$*"; }
c_warn() { printf '\033[1;33m [WARN]\033[0m %s\n' "$*"; }
c_err()  { printf '\033[1;31m [ERR]\033[0m %s\n' "$*" >&2; }

# ---------------------------------------------------------------------------
# 1. macOS (Darwin) Automatic Container Routing (Apple Silicon ARM64 & Intel)
# ---------------------------------------------------------------------------
if [[ ( "$(uname -s)" == "Darwin" || "$(uname -m)" != "x86_64" ) && "${HG_IN_DOCKER:-0}" != "1" ]]; then
    c_info "$(uname -s) ($(uname -m)) detected. Routing execution into linux/amd64 container..."
    if ! command -v docker >/dev/null 2>&1; then
        c_err "Docker / OrbStack is required on $(uname -s)/$(uname -m) to run gem5 & x86_64 ELF toolchains."
        c_err "Install OrbStack or Docker and re-run ./run_all.sh"
        exit 1
    fi
    DOCKER_IMAGE="hint-gather-revised:latest"
    if ! docker image inspect "$DOCKER_IMAGE" >/dev/null 2>&1; then
        c_info "Building $DOCKER_IMAGE (linux/amd64)..."
        docker build --platform linux/amd64 -t "$DOCKER_IMAGE" "$REPO_ROOT"
    fi
    mkdir -p "$HOME/hg-tools"
    DOCKER_TTY_FLAGS="-i"
    if [[ -t 0 && -t 1 ]]; then
        DOCKER_TTY_FLAGS="-it"
    fi
    exec docker run --rm $DOCKER_TTY_FLAGS --platform linux/amd64 \
        -e HG_IN_DOCKER=1 \
        -v "$REPO_ROOT:/workspace/hint_gather_revised" \
        -v "$HOME/hg-tools:/root/hg-tools" \
        -w /workspace/hint_gather_revised \
        "$DOCKER_IMAGE" ./run_all.sh "$@"
fi

# ---------------------------------------------------------------------------
# 2. Hardware-Aware Parallelism (Prevents OOM on 8GB-16GB Laptops)
# ---------------------------------------------------------------------------
compute_safe_jobs() {
    local cores=4
    if command -v nproc >/dev/null 2>&1; then
        cores="$(nproc)"
    elif command -v getconf >/dev/null 2>&1; then
        cores="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
    fi
    local mem_gb=16
    if [[ -r /proc/meminfo ]]; then
        local mem_kb
        mem_kb="$(awk '/MemTotal:/ {print $2}' /proc/meminfo || echo 16777216)"
        mem_gb=$(( mem_kb / 1024 / 1024 ))
    fi
    local max_by_mem=$(( mem_gb / 3 ))
    (( max_by_mem < 1 )) && max_by_mem=1
    if (( cores > max_by_mem )); then
        echo "$max_by_mem"
    else
        echo "$cores"
    fi
}

export JOBS="${HG_JOBS:-$(compute_safe_jobs)}"
export HG_TOOLS_ROOT="${HG_TOOLS_ROOT:-${HG_TOOLS_DIR:-$HOME/hg-tools}}"

# ---------------------------------------------------------------------------
# 3. Automatic Environment Discovery & Activation
# ---------------------------------------------------------------------------
load_env() {
    if [[ -d "$HG_TOOLS_ROOT/miniconda3/envs/hint-gather" ]]; then
        export CONDA_PREFIX="$HG_TOOLS_ROOT/miniconda3/envs/hint-gather"
    elif [[ -d "$HOME/miniconda3/envs/hint-gather" ]]; then
        export CONDA_PREFIX="$HOME/miniconda3/envs/hint-gather"
    elif [[ -n "${CONDA_PREFIX:-}" && -d "$CONDA_PREFIX" ]]; then
        export CONDA_PREFIX="$CONDA_PREFIX"
    else
        c_info "Conda environment not found; running setup_env.sh..."
        "$REPO_ROOT/scripts/setup_env.sh" --jobs "$JOBS"
        if [[ -d "$HG_TOOLS_ROOT/miniconda3/envs/hint-gather" ]]; then
            export CONDA_PREFIX="$HG_TOOLS_ROOT/miniconda3/envs/hint-gather"
        else
            export CONDA_PREFIX="$HOME/miniconda3/envs/hint-gather"
        fi
    fi

    if [[ -d "$HG_TOOLS_ROOT/llvm-17" ]]; then
        export HG_LLVM_INSTALL="$HG_TOOLS_ROOT/llvm-17"
    elif [[ -d "$HG_TOOLS_ROOT/llvm" ]]; then
        export HG_LLVM_INSTALL="$HG_TOOLS_ROOT/llvm"
    else
        export HG_LLVM_INSTALL="$CONDA_PREFIX"
    fi

    export HG_LLVM_CMAKE_DIR="$HG_LLVM_INSTALL/lib/cmake/llvm"
    export HG_CLANG="$HG_LLVM_INSTALL/bin/clang"
    export HG_PASS_BUILD="$REPO_ROOT/llvm/build"
    export HG_PASS_PLUGIN="$REPO_ROOT/llvm/build/libHintGather.so"
    export HG_PLUGIN="$HG_PASS_PLUGIN"
    export HG_RISCV_TOOLCHAIN="$CONDA_PREFIX"
    export HG_GEM5_ROOT="$HG_TOOLS_ROOT/gem5"
    export HG_CHAMPSIM_ROOT="$HG_TOOLS_ROOT/ChampSim"

    export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:$HG_LLVM_INSTALL/lib:${LD_LIBRARY_PATH:-}"
    export PATH="$HG_LLVM_INSTALL/bin:$CONDA_PREFIX/bin:${PATH:-/usr/bin:/bin}"

    if [[ -x "$CONDA_PREFIX/bin/x86_64-conda-linux-gnu-gcc" ]]; then
        export CC_HOST="$CONDA_PREFIX/bin/x86_64-conda-linux-gnu-gcc"
        export CXX_HOST="$CONDA_PREFIX/bin/x86_64-conda-linux-gnu-g++"
    elif [[ -x "$CONDA_PREFIX/bin/aarch64-conda-linux-gnu-gcc" ]]; then
        export CC_HOST="$CONDA_PREFIX/bin/aarch64-conda-linux-gnu-gcc"
        export CXX_HOST="$CONDA_PREFIX/bin/aarch64-conda-linux-gnu-g++"
    else
        export CC_HOST="${CC:-gcc}"
        export CXX_HOST="${CXX:-g++}"
    fi
}

# ---------------------------------------------------------------------------
# Stage Functions
# ---------------------------------------------------------------------------

do_setup() {
    c_info "[Stage: setup] Ensuring toolchains in $HG_TOOLS_ROOT (jobs=$JOBS)..."
    "$REPO_ROOT/scripts/setup_env.sh" --jobs "$JOBS"
    load_env
    c_ok "Environment ready (CONDA_PREFIX=$CONDA_PREFIX, LLVM=$HG_LLVM_INSTALL)"
}

do_llvm() {
    load_env
    c_info "[Stage: llvm] Building LLVM out-of-tree pass plugin (libHintGather.so)..."
    mkdir -p "$REPO_ROOT/llvm/build"
    "$CONDA_PREFIX/bin/cmake" -S "$REPO_ROOT/llvm" -B "$REPO_ROOT/llvm/build" \
        -DLLVM_DIR="$HG_LLVM_CMAKE_DIR" \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_C_COMPILER="$CC_HOST" \
        -DCMAKE_CXX_COMPILER="$CXX_HOST" >/dev/null
    "$CONDA_PREFIX/bin/cmake" --build "$REPO_ROOT/llvm/build" -j "$JOBS" >/dev/null
    c_ok "Built $HG_PASS_PLUGIN"
}

do_bench() {
    load_env
    [[ -f "$HG_PASS_PLUGIN" ]] || do_llvm
    c_info "[Stage: bench] Cross-compiling RISC-V & native benchmarks..."
    make -C "$REPO_ROOT/bench" riscv check \
        HG_CLANG="$HG_CLANG" \
        HG_PLUGIN="$HG_PASS_PLUGIN" \
        CONDA_PREFIX="$CONDA_PREFIX" \
        -j "$JOBS"
    c_ok "All RISC-V (.elf) and native smoke benchmarks built & checksum-verified"
}

do_gem5() {
    load_env
    c_info "[Stage: gem5] Applying HINT.GATHER PHQ + -Wno-error to $HG_GEM5_ROOT..."
    "$CONDA_PREFIX/bin/python3" "$REPO_ROOT/gem5/apply_phq.py" --gem5-root "$HG_GEM5_ROOT"

    c_info "[Stage: gem5] Building $HG_GEM5_ROOT/build/RISCV/gem5.opt (jobs=$JOBS)..."
    if [[ -x "$HG_LLVM_INSTALL/bin/ld.lld" && ! -x "$CONDA_PREFIX/bin/ld.lld" ]]; then
        ln -sf "$HG_LLVM_INSTALL/bin/ld.lld" "$CONDA_PREFIX/bin/ld.lld" 2>/dev/null || true
    fi
    local linker_flag=()
    if echo 'int main(){return 0;}' | "$CXX_HOST" -x c++ - -fuse-ld=lld -o /dev/null >/dev/null 2>&1; then
        linker_flag=("--linker=lld")
    fi
    (
        cd "$HG_GEM5_ROOT"
        "$CONDA_PREFIX/bin/scons" build/RISCV/gem5.opt \
            -j "$JOBS" \
            CC="$HG_TOOLS_ROOT/bin/gcc-wrapper" \
            CXX="$HG_TOOLS_ROOT/bin/g++-wrapper" \
            PYTHON_CONFIG="$CONDA_PREFIX/bin/python3-config" \
            "${linker_flag[@]}"
    )
    c_ok "gem5 binary ready at $HG_GEM5_ROOT/build/RISCV/gem5.opt"
}

do_champsim() {
    load_env
    c_info "[Stage: champsim] Building & testing ChampSim sidecar prefetcher..."
    "$CONDA_PREFIX/bin/python3" - <<PY
import json, sys
sys.path.insert(0, "$REPO_ROOT")
from champsim import render
with open("$REPO_ROOT/genomes/default.json") as f:
    genome = json.load(f)
sites = [{"site_id": 0, "pc": 0x10550, "base_reg_hint": 0, "elem_size": 8, "stride_bytes": 4, "variant": 1}]
render.render_to_file(
    "$REPO_ROOT/champsim/hint_gather_prefetcher.h.in",
    genome,
    sites,
    "$REPO_ROOT/champsim/hint_gather_prefetcher.h",
)
PY

    "$CXX_HOST" -std=c++17 -O2 -I"$REPO_ROOT/champsim" \
        "$REPO_ROOT/champsim/test_sidecar.cc" \
        -o "$REPO_ROOT/champsim/test_sidecar"
    "$REPO_ROOT/champsim/test_sidecar"
    c_ok "ChampSim sidecar unit tests passed"
}

do_verify() {
    load_env
    [[ -f "$HG_PASS_PLUGIN" ]] || do_llvm
    [[ -x "$HG_GEM5_ROOT/build/RISCV/gem5.opt" ]] || do_gem5
    c_info "[Stage: verify] Running full end-to-end verification & generating report.txt..."
    "$CONDA_PREFIX/bin/python3" "$REPO_ROOT/scripts/verify_all.py" "$@"
}

do_evolve() {
    load_env
    [[ -f "$HG_PASS_PLUGIN" ]] || do_llvm
    [[ -x "$HG_GEM5_ROOT/build/RISCV/gem5.opt" ]] || do_gem5
    c_info "[Stage: evolve] Launching CHIA HINT.GATHER evolutionary co-design loop..."
    if [[ $# -eq 0 ]]; then
        HG_LOCAL=1 "$CONDA_PREFIX/bin/python3" "$REPO_ROOT/hint_gather_loop.py" \
            --stage single --genome "$REPO_ROOT/genomes/default.json" --local --no-llm
    else
        HG_LOCAL=1 "$CONDA_PREFIX/bin/python3" "$REPO_ROOT/hint_gather_loop.py" --local "$@"
    fi
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
case "$STAGE" in
    all)
        do_setup
        do_llvm
        do_bench
        do_gem5
        do_champsim
        do_verify "${@:2}"
        ;;
    setup)     do_setup ;;
    llvm)      do_llvm ;;
    bench)     do_bench ;;
    gem5)      do_gem5 ;;
    champsim)  do_champsim ;;
    verify|run|test) do_verify "${@:2}" ;;
    evolve|loop)     do_evolve "${@:2}" ;;
    -h|--help|help)
        sed -n '2,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        ;;
    *)
        c_err "Unknown stage '$STAGE'. Choose one of: all | setup | llvm | bench | gem5 | champsim | verify | evolve"
        exit 1
        ;;
esac
