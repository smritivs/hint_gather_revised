#!/usr/bin/env bash
#
# Generate ChampSim traces for the HINT.GATHER benchmark suite.
#
#   ./scripts/gen_traces.sh                             # all benchmarks, all variants
#   ./scripts/gen_traces.sh --bench bfs                 # one benchmark
#   ./scripts/gen_traces.sh --skip 200000000 --count 50000000
#
# ---------------------------------------------------------------------------
# Why we trace the x86 build and not the RISC-V one
# ---------------------------------------------------------------------------
# ChampSim consumes x86 traces.  That is not a limitation we can wish away, and
# re-targeting the tracer is far out of scope.  It is also not a problem for
# what Node 4 is actually being asked to do: ChampSim is our *fast* evaluator,
# and what it measures is the memory-level behaviour of the hint schedule --
# which addresses get requested, how early, and whether they survive to use.
# That behaviour is a property of the algorithm and the hint placement, not of
# the ISA.  The pipeline-level claims (no IQ slot, no LSQ entry, no FU port)
# are ISA- and microarchitecture-specific, and they are validated in gem5 O3
# on RISC-V at Node 6 -- never here.  See docs/DESIGN.md sec 6.3.
#
# Concretely: the same pass runs over the same source with an x86 triple, in
# `analyze` mode plus the site-label emission, so every hinted load carries an
# `__hg_site_<id>` label.  champsim_nodes.resolve_hint_pcs reads those labels
# out of this exact ELF with llvm-nm, which is what makes the PCs in the trace
# and the PCs in the hint map the same numbers by construction.
#
# ---------------------------------------------------------------------------
# One trace per (benchmark, variant)?  No -- one per benchmark.
# ---------------------------------------------------------------------------
# The ChampSim prefetcher module *is* the HINT.GATHER model, so the hint
# schedule is applied at simulation time, not baked into the trace.  We
# therefore trace the `base` build only: one trace per benchmark, reused across
# every genome in the search.  Tracing `swpf` too is optional and off by
# default (--with-swpf) -- it is useful as a sanity check that the ChampSim
# prefetcher model reproduces what real prefetch instructions do, but it is not
# on the critical path.

set -euo pipefail

HG_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HG_TOOLS_ROOT="${HG_TOOLS_ROOT:-$HOME/hg-tools}"
PIN_ROOT="${PIN_ROOT:-$HG_TOOLS_ROOT/pin}"
CHAMPSIM_ROOT="${HG_CHAMPSIM_ROOT:-$HG_TOOLS_ROOT/ChampSim}"
TRACE_DIR="${HG_TRACE_DIR:-$HG_TOOLS_ROOT/traces}"
BENCH_DIR="$HG_REPO/bench"

# Warmup + simulation must both fit inside the traced window, with headroom:
# config.py defaults to 5M warmup + 25M simulated, and ChampSim silently
# reports a short run if the trace ends early.
SKIP_INSTS="${HG_TRACE_SKIP:-100000000}"
TRACE_INSTS="${HG_TRACE_COUNT:-60000000}"
BENCHES="${HG_TRACE_BENCHES:-gather bfs pagerank listchase}"
VARIANTS="base"
JOBS="${HG_JOBS:-$(nproc)}"
FORCE=0

c_info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
c_ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
c_skip() { printf '\033[1;33m --\033[0m %s\n' "$*"; }
c_die()  { printf '\033[1;31mERROR\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --bench)     BENCHES="$2"; shift 2 ;;
        --skip)      SKIP_INSTS="$2"; shift 2 ;;
        --count)     TRACE_INSTS="$2"; shift 2 ;;
        --with-swpf) VARIANTS="base swpf"; shift ;;
        --jobs|-j)   JOBS="$2"; shift 2 ;;
        --force)     FORCE=1; shift ;;
        -h|--help)
            sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//;$d'; exit 0 ;;
        *)
            c_die "unknown argument: $1" ;;
    esac
done

TRACER="$CHAMPSIM_ROOT/tracer/pin/obj-intel64/champsim_tracer.so"
[[ -x "$PIN_ROOT/pin" ]] || c_die "pin not found at $PIN_ROOT/pin (scripts/setup_env.sh --only pin)"
[[ -f "$TRACER" ]] || c_die "champsim_tracer.so not built (scripts/setup_env.sh --only traces)"
[[ -d "$BENCH_DIR" ]] || c_die "benchmark directory missing: $BENCH_DIR"
command -v xz >/dev/null || c_die "xz not on PATH (conda install -c conda-forge xz)"

mkdir -p "$TRACE_DIR"

# Native x86 builds.  'make x86' is part of the bench contract (docs/DESIGN.md
# sec 4.4): it must produce build/x86/<bench>.<variant> with the hint-site
# labels present but no custom-0 instructions -- x86 cannot execute those, and
# the ChampSim model supplies the hint behaviour itself.
c_info "building native x86 benchmarks"
make -C "$BENCH_DIR" -j"$JOBS" x86

for bench in $BENCHES; do
    for variant in $VARIANTS; do
        bin="$BENCH_DIR/build/x86/${bench}.${variant}"
        [[ -x "$bin" ]] || { c_skip "no binary for $bench/$variant -- skipping"; continue; }

        if [[ "$variant" == "base" ]]; then
            out="$TRACE_DIR/${bench}.champsimtrace"
        else
            out="$TRACE_DIR/${bench}.${variant}.champsimtrace"
        fi

        if [[ -f "$out.xz" && "$FORCE" != 1 ]]; then
            c_skip "$(basename "$out.xz") exists (--force to regenerate)"
            continue
        fi

        c_info "tracing $bench/$variant: skip=$SKIP_INSTS count=$TRACE_INSTS"
        # -t champsim_tracer.so -o <out> -s <skip> -t <count>
        # (ChampSim's tracer uses -t for trace instruction count; the tool
        # itself is selected by pin's own -t, hence the two.)
        "$PIN_ROOT/pin" -t "$TRACER" \
            -o "$out" -s "$SKIP_INSTS" -t "$TRACE_INSTS" \
            -- "$bin" --iters 0 --size 0 > "$out.stdout" 2>&1 \
            || c_die "tracing failed for $bench/$variant; see $out.stdout"

        # ChampSim reads .xz directly and the uncompressed traces are ~10x
        # larger, which matters once you have a dozen of them.
        c_info "compressing $(basename "$out")"
        xz -T "$JOBS" -3 -f "$out"
        c_ok "$(du -h "$out.xz" | cut -f1) $out.xz"
    done
done

echo
c_ok "traces in $TRACE_DIR"
ls -lh "$TRACE_DIR"/*.xz 2>/dev/null || true
