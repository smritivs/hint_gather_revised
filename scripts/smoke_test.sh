#!/usr/bin/env bash
#
# Pre-flight checks for the HINT.GATHER loop.
#
#   ./scripts/smoke_test.sh            # everything
#   ./scripts/smoke_test.sh --quick    # python-only checks, no toolchain
#
# Run this after scripts/setup_env.sh and before the first real loop.  It is
# ordered cheapest-first and by dependency depth, so the FIRST failure you see
# is the root cause rather than a downstream symptom.
#
# Every check is non-fatal: the script runs all of them and prints a summary,
# because knowing that four things are broken is more useful than discovering
# them one re-run at a time.  The exit code is the number of hard failures.
#
# WARN vs FAIL: a WARN means a stage of the pipeline will be unavailable (e.g.
# no traces -> no ChampSim evaluation) but the rest still works; a FAIL means
# the loop cannot start at all.

set -uo pipefail

HG_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HG_TOOLS_ROOT="${HG_TOOLS_ROOT:-$HOME/hg-tools}"
if [[ -f "$HG_TOOLS_ROOT/env.sh" ]]; then
    # shellcheck disable=SC1091
    source "$HG_TOOLS_ROOT/env.sh"
fi
QUICK=0
[[ "${1:-}" == "--quick" ]] && QUICK=1

PASS=0; WARN=0; FAIL=0

ok()   { printf '\033[1;32m PASS\033[0m %s\n' "$*"; PASS=$((PASS+1)); }
warn() { printf '\033[1;33m WARN\033[0m %s\n' "$*"; WARN=$((WARN+1)); }
bad()  { printf '\033[1;31m FAIL\033[0m %s\n' "$*"; FAIL=$((FAIL+1)); }
sect() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }

cd "$HG_REPO"

# ---------------------------------------------------------------------------
sect "1. Python environment"
# ---------------------------------------------------------------------------

PYV="$(python -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null || echo none)"
case "$PYV" in
    3.10.*) ok "python $PYV" ;;
    none)   bad "no python on PATH -- conda activate hint-gather" ;;
    *)      warn "python $PYV (CHIA images are 3.10.19; Ray will refuse to \
deserialise across a minor-version gap, so this only works in --local mode)" ;;
esac

if python -c 'import chia' 2>/dev/null; then
    ok "import chia ($(python -c 'import chia; print(list(chia.__path__)[0])'))"
else
    bad "import chia failed -- ./scripts/setup_env.sh --only conda"
fi

for mod in ray numpy; do
    python -c "import $mod" 2>/dev/null && ok "import $mod" || bad "import $mod failed"
done

# ---------------------------------------------------------------------------
sect "2. Loop modules import and self-describe"
# ---------------------------------------------------------------------------

if python -c 'import config; print(config.describe())' 2>/tmp/hg_cfg_err; then
    ok "config.py imports; resolved paths:"
    python -c 'import config; print(config.describe())' | sed 's/^/     /'
else
    bad "config.py import failed:"; sed 's/^/     /' /tmp/hg_cfg_err
fi

for mod in genome runner nodes.llvm_nodes nodes.gem5_nodes nodes.champsim_nodes \
           nodes.agents nodes.evolve hint_gather_loop; do
    if python -c "import $mod" 2>/tmp/hg_imp_err; then
        ok "import $mod"
    else
        bad "import $mod:  $(tail -2 /tmp/hg_imp_err | tr '\n' ' ')"
    fi
done

# ---------------------------------------------------------------------------
sect "3. Genome invariants"
# ---------------------------------------------------------------------------

python - <<'PY' && ok "genome schema, encoding, mutation and repair are self-consistent" \
              || bad "genome self-check failed (see above)"
import random
import sys
import genome as G

errs = []
pop = G.seed_population(8)
if len(pop) != 8:
    errs.append(f"seed_population(8) returned {len(pop)}")

for g in pop:
    problems = g.validate()
    if problems:
        errs.append(f"seed genome invalid: {problems}")
    # The encodings are what the assembler literally emits; an out-of-range
    # field here is a wrong instruction, not an exception.
    if not 0 <= g.funct3 <= 0b111:
        errs.append(f"funct3 out of range: {g.funct3}")
    if not 0 <= g.funct7(4) <= 0b1111111:
        errs.append(f"funct7 out of range: {g.funct7(4)}")
    for h in (g.genome_id, g.llvm_hash, g.gem5_hash, g.champsim_hash):
        if not h:
            errs.append("empty hash")

# Hash scoping is what makes the evaluator's caches correct: changing a
# ChampSim-only knob must NOT invalidate the gem5 build, and vice versa.
a = pop[0]
if a.llvm_hash == "" or a.gem5_hash == "":
    errs.append("hashes must be non-empty")

# repaired() must be a fixed point on already-valid genomes, or the search
# silently drifts every generation.
for g in pop:
    if g.repaired() != g:
        errs.append(f"repaired() is not idempotent on a valid genome: {g.genome_id}")
        break

# ...and must make anything valid, including deliberate garbage.
import dataclasses
bad_kwargs = {}
for key, spec in G.SCHEMA.items():
    lo = getattr(spec, "lo", None)
    if isinstance(lo, (int, float)):
        bad_kwargs[key] = lo - 999
if bad_kwargs:
    try:
        wrecked = dataclasses.replace(pop[0], **bad_kwargs)
        fixed = wrecked.repaired()
        if fixed.validate():
            errs.append(f"repaired() left an invalid genome: {fixed.validate()}")
    except Exception as e:      # noqa: BLE001 - diagnostic path
        errs.append(f"repaired() raised on out-of-range input: {e}")

for i in range(200):
    rng = random.Random(i)
    child = G.mutate(pop[i % len(pop)], rng)
    if child.validate():
        errs.append(f"mutate() produced an invalid genome: {child.validate()}")
        break
    cross = G.crossover(pop[0], pop[1], rng)
    if cross.validate():
        errs.append(f"crossover() produced an invalid genome: {cross.validate()}")
        break

for e in errs:
    print(f"     {e}", file=sys.stderr)
sys.exit(1 if errs else 0)
PY

# ---------------------------------------------------------------------------
sect "4. Prompt templates"
# ---------------------------------------------------------------------------

python - <<'PY' && ok "every prompt renders with the placeholders agents.py supplies" \
              || bad "prompt rendering failed (see above)"
import sys
from pathlib import Path
import nodes.agents as A
import config

# A missing placeholder is a KeyError *inside a Ray worker, mid-generation*,
# hours into a run.  Catch it here instead.
common = dict(attempt=1, max_attempts=3, error="E", workdir="/tmp",
              design_doc="D", genome="{}")
extra = {
    "repair_bench_build": dict(benchmark="bfs", build_variant="hint"),
    "repair_gem5_gate": dict(failure_kind="checksum_mismatch"),
    "repair_champsim_build": dict(module_name="hg_deadbeef"),
    "compiler_agent": dict(llvm_dir="/l", llvm_install="/i"),
    "microarch_agent": dict(gem5_root="/g", hg_gem5_dir="/h"),
    "evolve_mutate": dict(history="H", current_best="B", count=4,
                          lambda_value=0.01),
}

errs = []
for path in sorted(Path(config.PROMPTS_DIR).glob("*.md")):
    name = path.stem
    kwargs = dict(common, **extra.get(name, {}))
    try:
        text = A.load_prompt(name, **kwargs)
    except Exception as e:                     # noqa: BLE001
        errs.append(f"{name}: {type(e).__name__}: {e}")
        continue
    if not text.strip():
        errs.append(f"{name}: rendered empty")
    # load_prompt is deliberately forgiving about stray braces (compiler output
    # is full of them), so an unresolved placeholder will NOT raise -- check.
    for token in ("{error}", "{workdir}", "{genome}", "{attempt}"):
        if token in text:
            errs.append(f"{name}: unresolved {token}")

for e in errs:
    print(f"     {e}", file=sys.stderr)
sys.exit(1 if errs else 0)
PY

# ---------------------------------------------------------------------------
sect "5. ChampSim prefetcher rendering"
# ---------------------------------------------------------------------------

RENDERED=/tmp/hg_rendered_prefetcher.h
python - "$RENDERED" <<'PY' && ok "prefetcher template renders with no placeholders left" \
                            || bad "prefetcher rendering failed (see above)"
import sys
import genome as G
from nodes import champsim_nodes as C

out = sys.argv[1]
g = G.seed_population(1)[0]
try:
    src = C.render_prefetcher_source(
        g,
        [
            {"site_id": 0, "pc": 0x401234, "elem_size": 4, "variant": 1},
            {"site_id": 1, "pc": 0x401288, "elem_size": 8, "variant": 1},
        ],
    )
except Exception as e:                          # noqa: BLE001
    print(f"     render raised {type(e).__name__}: {e}", file=sys.stderr)
    sys.exit(1)

errs = []
import re
unsub = sorted(set(re.findall(r"@@\w+@@", src)))
if unsub:
    errs.append("unsubstituted placeholders: " + ", ".join(unsub))
# DESIGN.md sec 4.3 requires these to exist or summarize_champsim reads zeros
# forever and every candidate looks identical.
for stat in ("hg_hints_seen", "hg_prefetches_issued", "hg_prefetches_dropped",
             "hg_late_prefetches", "hg_phq_full_events"):
    if stat not in src:
        errs.append(f"final_stats is missing {stat}")
for hook in ("prefetcher_initialize", "prefetcher_final_stats"):
    if hook not in src:
        errs.append(f"missing ChampSim hook {hook}")
open(out, "w").write(src)
for e in errs:
    print(f"     {e}", file=sys.stderr)
sys.exit(1 if errs else 0)
PY

if [[ "$QUICK" == 0 ]] && command -v g++ >/dev/null && [[ -f "$RENDERED" ]]; then
    # ChampSim modules are header-only and depend on ChampSim's own headers, so
    # a bare syntax check will not link -- but it does catch the errors an LLM
    # actually makes (unbalanced braces, bad templates, C++ version misuse).
    if g++ -std=c++17 -fsyntax-only -x c++ "$RENDERED" 2>/tmp/hg_cxx_err; then
        ok "rendered prefetcher passes a standalone g++ -fsyntax-only"
    else
        if grep -qE "fatal error: .*(champsim|cache|ooo_cpu)" /tmp/hg_cxx_err; then
            warn "prefetcher needs ChampSim headers to syntax-check (expected; \
the real check is the build node)"
        else
            bad "rendered prefetcher has syntax errors:"; head -5 /tmp/hg_cxx_err | sed 's/^/     /'
        fi
    fi
fi
[[ "$QUICK" == 1 ]] && { sect "Summary (quick mode)"; \
    printf '  %d passed, %d warnings, %d failures\n' "$PASS" "$WARN" "$FAIL"; exit "$FAIL"; }

# ---------------------------------------------------------------------------
sect "6. Toolchain"
# ---------------------------------------------------------------------------
CLANG="${HG_CLANG:-$HG_TOOLS_ROOT/llvm/bin/clang}"
if [[ -x "$CLANG" ]]; then
    ok "clang: $($CLANG --version | head -1)"
    "$CLANG" -print-targets 2>/dev/null | grep -qi riscv \
        && ok "clang has a RISC-V backend" \
        || bad "clang has no RISC-V backend -- wrong LLVM release"
else
    bad "clang not found at $CLANG -- ./scripts/setup_env.sh --only llvm"
fi
[[ -d "${HG_LLVM_CMAKE_DIR:-$HG_TOOLS_ROOT/llvm/lib/cmake/llvm}" ]] \
    && ok "LLVM cmake package present (pass plugin can build)" \
    || bad "lib/cmake/llvm missing -- the pass plugin cannot build"
if command -v riscv64-unknown-elf-gcc >/dev/null; then
    ok "riscv64-unknown-elf-gcc $(riscv64-unknown-elf-gcc -dumpversion)"
else
    bad "no RISC-V toolchain -- conda activate hint-gather"
fi
for tool in cmake ninja scons; do
    command -v "$tool" >/dev/null && ok "$tool $($tool --version 2>&1 | head -1 | tr -d '\n')" \
        || bad "$tool missing"
done

# ---------------------------------------------------------------------------
sect "7. External checkouts and inputs"
# ---------------------------------------------------------------------------
[[ -d "${HG_GEM5_ROOT:-$HG_TOOLS_ROOT/gem5}/src" ]] \
    && ok "gem5 source present" || bad "gem5 source missing"
[[ -f "${HG_CHAMPSIM_ROOT:-$HG_TOOLS_ROOT/ChampSim}/config.sh" ]] \
    && ok "ChampSim source present" || bad "ChampSim source missing"
TRACES=$(ls "${HG_TRACE_DIR:-$HG_TOOLS_ROOT/traces}"/*.xz 2>/dev/null | wc -l)
if [[ "$TRACES" -gt 0 ]]; then
    ok "$TRACES ChampSim trace(s) available"
else
    warn "no ChampSim traces -- Node 4 will be skipped. ./scripts/gen_traces.sh"
fi

# ---------------------------------------------------------------------------
sect "8. Loop dry run"
# ---------------------------------------------------------------------------
# --no-llm --skip-gem5 --skip-champsim exercises argument parsing, the store
# schema and target selection without touching a simulator or spending a token.
if HG_LOCAL=1 timeout 120 python hint_gather_loop.py --stage report --local \
    --no-llm >/tmp/hg_dry.log 2>&1; then
    ok "hint_gather_loop.py --stage report runs"
else
    warn "loop dry run returned non-zero (expected before the first real run):"
    tail -3 /tmp/hg_dry.log | sed 's/^/     /'
fi

# ---------------------------------------------------------------------------
sect "Summary"
# ---------------------------------------------------------------------------
printf '  %d passed, %d warnings, %d failures\n' "$PASS" "$WARN" "$FAIL"
if [[ "$FAIL" -eq 0 ]]; then
    printf '\n  Ready. Next: HG_LOCAL=1 python hint_gather_loop.py --stage bootstrap\n'
else
    printf '\n  Fix the FAILs above, then re-run.\n'
fi
exit "$FAIL"
