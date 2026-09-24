#!/usr/bin/env bash
#
# One-time environment bootstrap for the HINT.GATHER CHIA loop.
#
# This script is deliberately *idempotent* and *stage-wise*: every stage checks
# for its own completion marker first, so re-running after a network hiccup
# costs seconds rather than an hour.  Nothing here is fatal to the rest of the
# pipeline if you already have a given component -- point the matching HG_*
# variable at it and `--skip` the stage.
#
#   ./scripts/setup_env.sh                       # everything, in order
#   ./scripts/setup_env.sh --only llvm           # just one stage
#   ./scripts/setup_env.sh --skip traces --jobs 32
#   ./scripts/setup_env.sh --list                # show stages and their state
#
# Stages
#   conda     miniconda (if absent) + the `hint-gather` env from env.yml
#   chia      clone github.com/ucb-bar/chia (the env installs it editable)
#   llvm      download a PREBUILT LLVM release -- we never build LLVM
#   riscv     verify the bare-metal RISC-V toolchain from the conda env
#   gem5      clone gem5 (the loop builds it, via Gem5Node.build_gem5)
#   champsim  clone ChampSim (the loop builds it, via ChampSimNode)
#   pin       Intel Pin, needed only to *generate* ChampSim traces
#   traces    build the x86 benchmark binaries and trace them
#   env       emit $HG_TOOLS_ROOT/env.sh with every HG_* export
#
# Why no LLVM build?  The pass is an out-of-tree plugin loaded with
# `-fpass-plugin=`, and the custom-0 instruction is emitted with a `.insn`
# directive rather than through SelectionDAG.  That removes a ~90 minute build
# from the critical path and drops the agent's edit->test cycle from minutes to
# ~20 seconds.  See docs/DESIGN.md sec 6.2 for the trade-off this implies.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults (every one overridable from the environment)
# ---------------------------------------------------------------------------

HG_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HG_TOOLS_ROOT="${HG_TOOLS_ROOT:-$HOME/hg-tools}"
HG_CONDA_ENV="${HG_CONDA_ENV:-hint-gather}"

LLVM_VERSION="${HG_LLVM_VERSION:-18.1.8}"
LLVM_TARBALL="clang+llvm-${LLVM_VERSION}-x86_64-linux-gnu-ubuntu-18.04.tar.xz"
LLVM_URL="https://github.com/llvm/llvm-project/releases/download/llvmorg-${LLVM_VERSION}/${LLVM_TARBALL}"

# LLVM release tarballs are NOT interchangeable across distros.  The
# `ubuntu-18.04` builds -- which is all upstream ships for 18.x on x86-64 --
# link against libtinfo.so.5, and Debian rodete (and anything else modern)
# only has libtinfo.so.6.  The symptom is a *runtime* failure of every LLVM
# binary, which looks exactly like "this release has no RISC-V backend" if you
# only check `llc --version`.
#
# So: try candidates in order and keep the first whose `llc` actually RUNS.
# Ordered by preference, and 17.0.6 leads deliberately -- llvm/HintGatherPass.cpp
# has API guards written against 17-20, and 17.0.6's ubuntu-22.04 build is the
# newest one that both links libtinfo.so.6 and stays inside that range.
#   <tag>|<tarball>
LLVM_CANDIDATES=(
    "llvmorg-17.0.6|clang+llvm-17.0.6-x86_64-linux-gnu-ubuntu-22.04.tar.xz"
    "llvmorg-19.1.7|LLVM-19.1.7-Linux-X64.tar.xz"
    "llvmorg-20.1.0|LLVM-20.1.0-Linux-X64.tar.xz"
    "llvmorg-18.1.8|clang+llvm-18.1.8-x86_64-linux-gnu-ubuntu-18.04.tar.xz"
)
# Set HG_LLVM_TARBALL=<tag>|<file> to force one.
[[ -n "${HG_LLVM_TARBALL:-}" ]] && LLVM_CANDIDATES=("$HG_LLVM_TARBALL")

GEM5_REPO="${HG_GEM5_REPO:-https://github.com/gem5/gem5.git}"
GEM5_REF="${HG_GEM5_REF:-v24.0.0.1}"
CHAMPSIM_REPO="${HG_CHAMPSIM_REPO:-https://github.com/ChampSim/ChampSim.git}"
CHAMPSIM_REF="${HG_CHAMPSIM_REF:-master}"
CHIA_REPO="${HG_CHIA_REPO_URL:-https://github.com/ucb-bar/chia.git}"

PIN_VERSION="${HG_PIN_VERSION:-3.22-98547-g7a303a835-gcc-linux}"
PIN_URL="https://software.intel.com/sites/landingpage/pintool/downloads/pin-${PIN_VERSION}.tar.gz"

HOST_OS="$(uname -s)"
HOST_ARCH="$(uname -m)"
if [[ "$HOST_ARCH" == "aarch64" || "$HOST_ARCH" == "arm64" ]]; then
    MINICONDA_ARCH="aarch64"
else
    MINICONDA_ARCH="x86_64"
fi
MINICONDA_URL="https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-${MINICONDA_ARCH}.sh"

# Auto-scale parallel build jobs based on available RAM (~3 GB per heavy C++
# template / link job) so 16 GB laptops do not hit the Linux OOM killer.
_calc_safe_jobs() {
    local ncpu mem_gb max_by_mem
    ncpu="$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)"
    mem_gb="$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 16)"
    max_by_mem=$(( mem_gb / 3 ))
    (( max_by_mem < 2 )) && max_by_mem=2
    if (( ncpu > max_by_mem )); then
        echo "$max_by_mem"
    else
        echo "$ncpu"
    fi
}
JOBS="${HG_JOBS:-$(_calc_safe_jobs)}"

# Default stages exclude Intel Pin (`pin` and `traces`) because Pin 3.22 is
# x86-Intel-specific and fails on AMD Zen 3/4/5 and ARM64 hosts, and is not
# needed for gem5 or ChampSim sidecar verification. Pass `--with-pin` to include.
ALL_STAGES=(conda chia llvm riscv gem5 champsim env)
ONLY=""
SKIP=""
LIST_ONLY=0

# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------

c_info()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
c_ok()    { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
c_skip()  { printf '\033[1;33m  --\033[0m %s\n' "$*"; }
c_warn()  { printf '\033[1;33m  !!\033[0m %s\n' "$*" >&2; }
c_die()   { printf '\033[1;31mERROR\033[0m %s\n' "$*" >&2; exit 1; }

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//;$d'; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --only)       ONLY="$2"; shift 2 ;;
        --skip)       SKIP="${SKIP},$2"; shift 2 ;;
        --with-pin)   ALL_STAGES=(conda chia llvm riscv gem5 champsim pin traces env); shift ;;
        --jobs|-j)    JOBS="$2"; shift 2 ;;
        --tools-root) HG_TOOLS_ROOT="$2"; shift 2 ;;
        --list)       LIST_ONLY=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            c_die "unknown argument: $1 (try --help)" ;;
    esac
done

want_stage() {
    local s="$1"
    [[ -n "$ONLY" && "$ONLY" != "$s" ]] && return 1
    [[ ",${SKIP}," == *",${s},"* ]] && return 1
    return 0
}

# A stage is "done" when its marker exists.  Markers are written only after the
# stage fully succeeds, so a half-finished download never looks complete.
marker() { echo "$HG_TOOLS_ROOT/.markers/$1.done"; }
is_done() { [[ -f "$(marker "$1")" ]]; }
mark_done() { mkdir -p "$HG_TOOLS_ROOT/.markers"; date -Is > "$(marker "$1")"; }

# Clone or fast-forward, pinned to a ref.  Never destroys local edits: the
# microarch agent patches gem5 in place, and blowing that away mid-loop would
# be catastrophic.
clone_or_update() {
    local url="$1" dest="$2" ref="$3"
    if [[ -d "$dest/.git" ]]; then
        if [[ -n "$(git -C "$dest" status --porcelain)" ]]; then
            c_warn "$dest has local modifications -- leaving it untouched"
            return 0
        fi
        c_info "updating $dest -> $ref"
        git -C "$dest" fetch --tags --depth 1 origin "$ref" 2>/dev/null \
            || git -C "$dest" fetch --tags origin
        git -C "$dest" checkout -q "$ref"
    else
        c_info "cloning $url -> $dest"
        git clone --depth 1 --branch "$ref" "$url" "$dest" 2>/dev/null \
            || { git clone "$url" "$dest" && git -C "$dest" checkout -q "$ref"; }
    fi
}

fetch() {
    local url="$1" dest="$2"
    [[ -f "$dest" ]] && { c_skip "already downloaded: $(basename "$dest")"; return 0; }
    c_info "downloading $(basename "$dest")"
    # --continue so an interrupted 1 GB download resumes instead of restarting.
    curl -fL --retry 3 --continue-at - -o "$dest.part" "$url"
    mv "$dest.part" "$dest"
}

if [[ "$LIST_ONLY" == 1 ]]; then
    printf '%-10s %-8s %s\n' STAGE STATE MARKER
    for s in "${ALL_STAGES[@]}"; do
        printf '%-10s %-8s %s\n' "$s" \
            "$(is_done "$s" && echo done || echo pending)" "$(marker "$s")"
    done
    exit 0
fi

mkdir -p "$HG_TOOLS_ROOT"
c_info "repo       = $HG_REPO"
c_info "tools root = $HG_TOOLS_ROOT"
c_info "jobs       = $JOBS"

ensure_conda_loaded() {
    set +u
    if [[ -x "$HG_TOOLS_ROOT/miniconda3/bin/conda" ]]; then
        export CONDA_ROOT="$HG_TOOLS_ROOT/miniconda3"
        # shellcheck disable=SC1091
        source "$CONDA_ROOT/etc/profile.d/conda.sh" >/dev/null 2>&1 || true
        conda activate "$HG_CONDA_ENV" >/dev/null 2>&1 || true
    elif [[ -x "$HOME/miniconda3/bin/conda" ]]; then
        export CONDA_ROOT="$HOME/miniconda3"
        # shellcheck disable=SC1091
        source "$CONDA_ROOT/etc/profile.d/conda.sh" >/dev/null 2>&1 || true
        conda activate "$HG_CONDA_ENV" >/dev/null 2>&1 || true
    elif command -v conda >/dev/null 2>&1; then
        export CONDA_ROOT="$(conda info --base)"
        # shellcheck disable=SC1091
        source "$CONDA_ROOT/etc/profile.d/conda.sh" >/dev/null 2>&1 || true
        conda activate "$HG_CONDA_ENV" >/dev/null 2>&1 || true
    fi
    if [[ -z "${CONDA_PREFIX:-}" && -n "${CONDA_ROOT:-}" && -d "$CONDA_ROOT/envs/$HG_CONDA_ENV" ]]; then
        export CONDA_PREFIX="$CONDA_ROOT/envs/$HG_CONDA_ENV"
    fi
    if [[ -n "${CONDA_PREFIX:-}" && -d "$CONDA_PREFIX" ]]; then
        export PATH="$CONDA_PREFIX/bin:${CONDA_ROOT:-$CONDA_PREFIX}/bin:${PATH:-/usr/bin:/bin}"
        export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
    fi
    set -u
}
ensure_conda_loaded

# ---------------------------------------------------------------------------
# conda
# ---------------------------------------------------------------------------

stage_conda() {
    set +u
    local conda_root="${CONDA_ROOT:-$HG_TOOLS_ROOT/miniconda3}"
    if [[ -x "$conda_root/bin/conda" ]]; then
        # shellcheck disable=SC1091
        source "$conda_root/etc/profile.d/conda.sh" >/dev/null 2>&1 || true
    elif [[ -x "$HOME/miniconda3/bin/conda" ]]; then
        conda_root="$HOME/miniconda3"
        # shellcheck disable=SC1091
        source "$conda_root/etc/profile.d/conda.sh" >/dev/null 2>&1 || true
    elif command -v conda >/dev/null 2>&1; then
        conda_root="$(conda info --base)"
        # shellcheck disable=SC1091
        source "$conda_root/etc/profile.d/conda.sh" >/dev/null 2>&1 || true
    else
        local installer="$HG_TOOLS_ROOT/miniconda.sh"
        fetch "$MINICONDA_URL" "$installer"
        c_info "installing miniconda to $conda_root"
        bash "$installer" -b -p "$conda_root"
        # shellcheck disable=SC1091
        source "$conda_root/etc/profile.d/conda.sh" >/dev/null 2>&1 || true
        "$conda_root/bin/conda" init bash >/dev/null 2>&1 || true
    fi

    export PATH="$conda_root/bin:${PATH:-/usr/bin:/bin}"
    for ch in https://repo.anaconda.com/pkgs/main https://repo.anaconda.com/pkgs/r; do
        conda tos accept --override-channels --channel "$ch" >/dev/null 2>&1 || true
    done

    if conda env list | awk '{print $1}' | grep -qx "$HG_CONDA_ENV"; then
        c_skip "conda env '$HG_CONDA_ENV' already exists"
        export CONDA_PREFIX="$conda_root/envs/$HG_CONDA_ENV"
        export PATH="$CONDA_PREFIX/bin:$conda_root/bin:$PATH"
        export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
        set -u
        return 0
    fi

    [[ -d "$HG_TOOLS_ROOT/chia" ]] || stage_chia || { set -u; return 1; }
    local resolved="$HG_TOOLS_ROOT/env-resolved.yml"
    sed "s|\${HG_CHIA_REPO}|$HG_TOOLS_ROOT/chia|g" "$HG_REPO/env.yml" > "$resolved" \
        || { set -u; return 1; }
    c_info "creating conda env '$HG_CONDA_ENV' (this takes a few minutes)"
    conda env create -f "$resolved" || { set -u; return 1; }
    export CONDA_PREFIX="$conda_root/envs/$HG_CONDA_ENV"
    export PATH="$CONDA_PREFIX/bin:$conda_root/bin:$PATH"
    export LD_LIBRARY_PATH="$CONDA_PREFIX/lib:${LD_LIBRARY_PATH:-}"
    set -u
    c_ok "conda env ready -- activate with: conda activate $HG_CONDA_ENV"
}

# ---------------------------------------------------------------------------
# chia
# ---------------------------------------------------------------------------

stage_chia() {
    clone_or_update "$CHIA_REPO" "$HG_TOOLS_ROOT/chia" "main"
    c_ok "chia at $HG_TOOLS_ROOT/chia"
}

# ---------------------------------------------------------------------------
# llvm (prebuilt or conda-forge fallback)
# ---------------------------------------------------------------------------

stage_llvm() {
    local conda_env_dir="${CONDA_PREFIX:-$HG_TOOLS_ROOT/miniconda3/envs/$HG_CONDA_ENV}"
    export LD_LIBRARY_PATH="$conda_env_dir/lib:${LD_LIBRARY_PATH:-}"

    local dest="$HG_TOOLS_ROOT/llvm"
    if [[ -d "$HG_TOOLS_ROOT/llvm-17" && ! -e "$dest" ]]; then
        ln -s "$HG_TOOLS_ROOT/llvm-17" "$dest"
    fi
    if [[ -x "$dest/bin/clang" ]] && "$dest/bin/clang" --version >/dev/null 2>&1; then
        c_skip "llvm already at $dest ($("$dest/bin/clang" --version | head -1))"
        return 0
    fi

    if [[ "$HOST_ARCH" == "x86_64" ]]; then
        local entry tag tarball url archive
        for entry in "${LLVM_CANDIDATES[@]}"; do
            tag="${entry%%|*}"
            tarball="${entry##*|}"
            url="https://github.com/llvm/llvm-project/releases/download/${tag}/${tarball}"
            archive="$HG_TOOLS_ROOT/$tarball"

            c_info "trying $tarball"
            fetch "$url" "$archive" || { c_warn "download failed"; continue; }

            rm -rf "$dest.staging"
            mkdir -p "$dest.staging"
            tar -xf "$archive" -C "$dest.staging" --strip-components=1 \
                || { c_warn "extract failed"; continue; }

            local why=""
            if [[ ! -x "$dest.staging/bin/llc" ]]; then
                why="no bin/llc"
            elif ! "$dest.staging/bin/llc" --version >/dev/null 2>&1; then
                why="llc will not run: $("$dest.staging/bin/llc" --version 2>&1 | head -1)"
            elif ! "$dest.staging/bin/llc" --version 2>/dev/null | grep -qi riscv; then
                why="no RISC-V target registered"
            elif [[ ! -d "$dest.staging/lib/cmake/llvm" ]]; then
                why="no lib/cmake/llvm -- the pass plugin could not build"
            elif [[ ! -x "$dest.staging/bin/clang" ]]; then
                why="no bin/clang"
            elif ! "$dest.staging/bin/llvm-nm" --version >/dev/null 2>&1; then
                why="no working llvm-nm"
            fi

            if [[ -z "$why" ]]; then
                rm -rf "$dest"
                mv "$dest.staging" "$dest"
                [[ -e "$HG_TOOLS_ROOT/llvm-17" ]] || ln -s "$dest" "$HG_TOOLS_ROOT/llvm-17"
                c_ok "llvm at $dest ($("$dest/bin/clang" --version | head -1))"
                c_ok "  RISC-V backend, lib/cmake/llvm and llvm-nm all verified"
                return 0
            fi

            c_warn "rejected $tarball: $why"
            rm -rf "$dest.staging"
        done
    fi

    c_info "installing hermetic LLVM 17 via conda-forge into $conda_env_dir"
    conda install -y -p "$conda_env_dir" --override-channels -c conda-forge \
        llvmdev=17 clang=17 clangxx=17 lld=17 || return 1
    rm -rf "$dest"
    ln -s "$conda_env_dir" "$dest"
    [[ -e "$HG_TOOLS_ROOT/llvm-17" ]] || ln -s "$conda_env_dir" "$HG_TOOLS_ROOT/llvm-17"
    c_ok "llvm installed via conda-forge at $dest ($("$dest/bin/clang" --version | head -1))"
}

# ---------------------------------------------------------------------------
# riscv toolchain
# ---------------------------------------------------------------------------

stage_riscv() {
    local conda_env_dir="${CONDA_PREFIX:-$HG_TOOLS_ROOT/miniconda3/envs/$HG_CONDA_ENV}"
    export PATH="$conda_env_dir/bin:$PATH"
    local gcc_bin
    gcc_bin="$(command -v riscv64-unknown-elf-gcc || true)"
    if [[ -z "$gcc_bin" && -x "$conda_env_dir/bin/riscv64-unknown-elf-gcc" ]]; then
        gcc_bin="$conda_env_dir/bin/riscv64-unknown-elf-gcc"
    fi
    if [[ -z "$gcc_bin" && -x "$HG_TOOLS_ROOT/riscv/bin/riscv64-unknown-elf-gcc" ]]; then
        gcc_bin="$HG_TOOLS_ROOT/riscv/bin/riscv64-unknown-elf-gcc"
    fi
    if [[ -z "$gcc_bin" ]]; then
        c_info "installing gcc-riscv64-unknown-elf via conda-forge"
        conda install -y -p "$conda_env_dir" --override-channels -c conda-forge \
            gcc-riscv64-unknown-elf binutils-riscv64-unknown-elf || return 1
        gcc_bin="$conda_env_dir/bin/riscv64-unknown-elf-gcc"
    fi
    local prefix
    prefix="$(dirname "$(dirname "$gcc_bin")")"
    echo "$prefix" > "$HG_TOOLS_ROOT/.riscv_prefix"
    c_ok "riscv toolchain at $prefix ($("$gcc_bin" -dumpversion))"
}

# ---------------------------------------------------------------------------
# gem5
# ---------------------------------------------------------------------------

stage_gem5() {
    clone_or_update "$GEM5_REPO" "$HG_TOOLS_ROOT/gem5" "$GEM5_REF"
    local conda_env_dir="${CONDA_PREFIX:-$HG_TOOLS_ROOT/miniconda3/envs/$HG_CONDA_ENV}"
    local py_bin="$conda_env_dir/bin/python3"
    [[ -x "$py_bin" ]] || py_bin="python3"
    "$py_bin" "$HG_REPO/gem5/apply_phq.py" --gem5-root "$HG_TOOLS_ROOT/gem5"
    c_ok "gem5 source at $HG_TOOLS_ROOT/gem5 ($GEM5_REF) with HINT.GATHER PHQ + -Wno-error applied"
}

# ---------------------------------------------------------------------------
# champsim
# ---------------------------------------------------------------------------

stage_champsim() {
    local dest="$HG_TOOLS_ROOT/ChampSim"
    clone_or_update "$CHAMPSIM_REPO" "$dest" "$CHAMPSIM_REF"
    git -C "$dest" submodule update --init --recursive
    c_ok "ChampSim source at $dest; the loop builds it per-genome"
}

# ---------------------------------------------------------------------------
# pin (trace generation only)
# ---------------------------------------------------------------------------

stage_pin() {
    local dest="$HG_TOOLS_ROOT/pin"
    if [[ -x "$dest/pin" ]]; then
        c_skip "pin already at $dest"
        return 0
    fi
    local tarball="$HG_TOOLS_ROOT/pin-${PIN_VERSION}.tar.gz"
    if ! fetch "$PIN_URL" "$tarball"; then
        c_warn "Pin download failed.  Intel occasionally moves these URLs."
        c_warn "Download pin-${PIN_VERSION}.tar.gz manually into $HG_TOOLS_ROOT"
        c_warn "and re-run:  ./scripts/setup_env.sh --only pin"
        return 1
    fi
    mkdir -p "$dest"
    tar -xzf "$tarball" -C "$dest" --strip-components=1
    c_ok "pin at $dest"
}

# ---------------------------------------------------------------------------
# traces
# ---------------------------------------------------------------------------

stage_traces() {
    local tracer="$HG_TOOLS_ROOT/ChampSim/tracer/pin"
    [[ -d "$tracer" ]] || c_die "ChampSim tracer not found at $tracer (run --only champsim)"
    [[ -x "$HG_TOOLS_ROOT/pin/pin" ]] || c_die "pin missing (run --only pin)"

    c_info "building the ChampSim pin tracer"
    make -C "$tracer" PIN_ROOT="$HG_TOOLS_ROOT/pin" obj-intel64/champsim_tracer.so

    mkdir -p "$HG_TOOLS_ROOT/traces"
    "$HG_REPO/scripts/gen_traces.sh" --jobs "$JOBS"
    c_ok "traces in $HG_TOOLS_ROOT/traces"
}

# ---------------------------------------------------------------------------
# env
# ---------------------------------------------------------------------------

stage_env() {
    local out="$HG_TOOLS_ROOT/env.sh"
    local conda_env_dir="${CONDA_PREFIX:-$HG_TOOLS_ROOT/miniconda3/envs/$HG_CONDA_ENV}"
    local riscv_prefix
    riscv_prefix="$(cat "$HG_TOOLS_ROOT/.riscv_prefix" 2>/dev/null || echo "$conda_env_dir")"
    local llvm_dir="$HG_TOOLS_ROOT/llvm-17"
    [[ -d "$llvm_dir" ]] || llvm_dir="$HG_TOOLS_ROOT/llvm"

    cat > "$out" <<EOF
# Generated by scripts/setup_env.sh on $(date -Is).  Source this before running
# the loop, or use ./run_all.sh which loads it automatically.

export HG_TOOLS_ROOT="$HG_TOOLS_ROOT"
export CONDA_PREFIX="$conda_env_dir"
export HG_LLVM_INSTALL="$llvm_dir"
export HG_LLVM_CMAKE_DIR="$llvm_dir/lib/cmake/llvm"
export HG_CLANG="$llvm_dir/bin/clang"
export HG_PASS_BUILD="$HG_REPO/llvm/build"
export HG_PASS_PLUGIN="$HG_REPO/llvm/build/libHintGather.so"
export HG_PLUGIN="$HG_REPO/llvm/build/libHintGather.so"
export HG_RISCV_TOOLCHAIN="$riscv_prefix"
export HG_RISCV_TARGET="riscv64-unknown-elf"
export HG_GEM5_ROOT="$HG_TOOLS_ROOT/gem5"
export HG_CHAMPSIM_ROOT="$HG_TOOLS_ROOT/ChampSim"
export HG_TRACE_DIR="$HG_TOOLS_ROOT/traces"
export PIN_ROOT="$HG_TOOLS_ROOT/pin"

export LD_LIBRARY_PATH="\$CONDA_PREFIX/lib:\$HG_LLVM_INSTALL/lib:\${LD_LIBRARY_PATH:-}"
export PATH="\$HG_LLVM_INSTALL/bin:\$CONDA_PREFIX/bin:\$HG_RISCV_TOOLCHAIN/bin:\$PATH"
EOF
    c_ok "wrote $out"
    echo
    c_info "Next steps:"
    echo "    ./run_all.sh          # builds & verifies everything in one command"
    echo "    ./run_all.sh bench    # or run any single stage (setup|llvm|bench|gem5|champsim|verify)"
}

# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------

failed=()
for stage in "${ALL_STAGES[@]}"; do
    want_stage "$stage" || { c_skip "stage $stage (filtered out)"; continue; }
    if is_done "$stage" && [[ "$ONLY" != "$stage" ]]; then
        c_skip "stage $stage (already done; force with --only $stage)"
        continue
    fi
    echo
    c_info "STAGE: $stage"
    if "stage_$stage"; then
        mark_done "$stage"
    else
        # Keep going: a missing Pin should not stop you from getting gem5.
        c_warn "stage $stage did not complete"
        failed+=("$stage")
    fi
done

echo
if [[ ${#failed[@]} -eq 0 ]]; then
    c_ok "all requested stages complete"
else
    c_warn "incomplete stages: ${failed[*]}"
    c_warn "re-run individually, e.g.  ./scripts/setup_env.sh --only ${failed[0]}"
    exit 1
fi
