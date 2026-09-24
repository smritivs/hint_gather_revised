"""Node 4: the fast inner evaluation (ChampSim).

CHIA's ``ChampSimNode`` compiles a *header-only* prefetcher module and runs it
against a trace.  We therefore express HINT.GATHER to ChampSim as a prefetcher
module driven by the compiler's hint-site PC table: on a demand access from a
hinted PC, issue the lookahead prefetches the hint would have issued.

**Read this before trusting any number that comes out of here.**  ChampSim is
trace-driven with a simplified out-of-order model.  It does *not* faithfully
model issue-queue port pressure or load-store-queue CAM bandwidth -- which is
precisely the cost HINT.GATHER exists to avoid.  So this node measures the
*memory-system* half of the story only, and the fitness function penalises hint
density to stop the search from exploiting the blind spot (see
``nodes/evolve.py`` and ``docs/DESIGN.md`` sections 5.1 and 6).  Every pipeline
claim in the write-up must come from gem5 O3, not from here.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field

from chia.base.ChiaFunction import ChiaFunction

import config
from genome import Genome


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

@dataclass
class HintPcMap:
    """Hint-site PCs recovered from the compiled ELF."""
    success: bool
    entries: list[dict] = field(default_factory=list)  # {pc, site_id, elem_size, stride_bytes}
    diagnostics: str = ""


@dataclass
class ChampSimSummary:
    """One ChampSim run, reduced to the numbers the search actually uses."""
    benchmark: str
    build_variant: str
    ok: bool
    ipc: float = 0.0
    instructions: int = 0
    cycles: int = 0
    l1d_mpki: float | None = None
    l1d_accuracy: float | None = None
    l1d_coverage: float | None = None
    l2_mpki: float | None = None
    hints_seen: float = 0.0
    prefetches_issued: float = 0.0
    prefetches_dropped: float = 0.0
    late_prefetches: float = 0.0
    phq_full_events: float = 0.0
    wall_s: float = 0.0
    error: str = ""

    @property
    def hints_per_1k_insts(self) -> float:
        if not self.instructions:
            return 0.0
        return 1000.0 * self.hints_seen / self.instructions

    @property
    def wasted_prefetch_rate(self) -> float:
        """Fraction of issued prefetches that did no good.

        Uses accuracy when ChampSim reports it (useful/issued), and falls back
        to the module's own late counter.  Both are approximations; they are
        used only as a search signal, never as a reported result.
        """
        if self.l1d_accuracy is not None:
            return max(0.0, 1.0 - self.l1d_accuracy)
        if self.prefetches_issued > 0:
            return self.late_prefetches / self.prefetches_issued
        return 0.0


# --------------------------------------------------------------------------
# Rendering the prefetcher module (pure -- runs on the head node)
# --------------------------------------------------------------------------

def _load_renderer():
    """Import ``champsim/render.py`` without requiring it to be a package."""
    champsim_dir = str(config.CHAMPSIM_DIR)
    if champsim_dir not in sys.path:
        sys.path.insert(0, champsim_dir)
    import render  # type: ignore  # noqa: E402  (champsim/render.py)
    return render


def render_prefetcher_source(genome: Genome, hint_sites: list[dict],
                             *, template_path: str | None = None) -> str:
    """Substitute the genome and the hint-site table into the module template.

    ``champsim/render.py`` hard-fails on any unsubstituted ``@@KEY@@``, so a
    template/loop mismatch surfaces here rather than as a mysterious C++ compile
    error twenty minutes later.
    """
    render = _load_renderer()
    path = template_path or str(config.CHAMPSIM_DIR / "hint_gather_prefetcher.h.in")
    return render.render(path, genome.search_dict(), hint_sites)


# --------------------------------------------------------------------------
# Recovering hint PCs from the ELF
# --------------------------------------------------------------------------

@ChiaFunction(resources=config.RES_LLVM)
def resolve_hint_pcs(elf_path: str, hint_sites_json: str, *, nm: str = "",
                     timeout_s: int = 300) -> HintPcMap:
    """Map ``__hg_site_<id>`` symbols in the ELF to addresses.

    The trace-driven model keys on PCs, and the only durable link between "the
    pass decided to hint this load" and "this PC in the trace" is the label the
    pass emits next to the load (DESIGN.md sec 4.1).
    """
    import json

    nm = nm or str(config.LLVM_INSTALL / "bin" / "llvm-nm")
    try:
        sites = json.loads(hint_sites_json).get("sites", [])
    except (ValueError, AttributeError) as e:
        return HintPcMap(False, diagnostics=f"bad hint_sites json: {e}")

    if not os.path.exists(elf_path):
        return HintPcMap(False, diagnostics=f"ELF not found: {elf_path}")

    try:
        proc = subprocess.run([nm, "-n", elf_path], capture_output=True,
                              text=True, timeout=timeout_s)
    except (OSError, subprocess.TimeoutExpired) as e:
        return HintPcMap(False, diagnostics=f"{nm} failed: {e}")
    if proc.returncode != 0:
        return HintPcMap(False, diagnostics=f"{nm} rc={proc.returncode}: "
                                            f"{proc.stderr[-1000:]}")

    symbols: dict[str, int] = {}
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[2].startswith("__hg_site_"):
            try:
                symbols[parts[2]] = int(parts[0], 16)
            except ValueError:
                continue

    entries: list[dict] = []
    missing: list[str] = []
    for site in sites:
        if not site.get("emitted"):
            continue
        sym = site.get("pc_symbol") or f"__hg_site_{site.get('site_id')}"
        addr = symbols.get(sym)
        if addr is None:
            missing.append(sym)
            continue
        elem = int(site.get("elem_size", 8) or 8)
        entries.append({
            "pc": addr,
            "site_id": site.get("site_id"),
            "elem_size": elem,
            "stride_bytes": elem,
            "source": site.get("source", ""),
        })

    diagnostics = ""
    if missing:
        diagnostics = (f"{len(missing)} hint symbol(s) not in the symbol table: "
                       f"{', '.join(missing[:8])}. The pass must emit a *global* "
                       f"or at least non-stripped local label, and the link must "
                       f"not run with -s.")
    return HintPcMap(success=bool(entries), entries=entries,
                     diagnostics=diagnostics)


# --------------------------------------------------------------------------
# Reducing a ChampSim run
# --------------------------------------------------------------------------

_NUMERIC = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")


def _as_float(value, default: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = _NUMERIC.search(value)
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return default
    return default


def summarize_champsim(result, benchmark: str, build_variant: str
                       ) -> ChampSimSummary:
    """Turn CHIA's ``ChampSimRunResult`` into our comparable record."""
    if not getattr(result, "success", False):
        return ChampSimSummary(
            benchmark=benchmark, build_variant=build_variant, ok=False,
            wall_s=getattr(result, "wall_s", 0.0) or 0.0,
            error=(getattr(result, "stdout_tail", "") or "")[-1500:],
        )

    caches = getattr(result, "cache_stats", {}) or {}
    l1d = caches.get("L1D")
    l2 = caches.get("L2C")
    custom = getattr(result, "custom_prefetch_stats", {}) or {}

    def _cache_mpki(cache):
        if cache is None:
            return None
        pf = getattr(cache, "prefetch", None)
        return getattr(pf, "mpki", None) if pf else None

    l1d_pf = getattr(l1d, "prefetch", None) if l1d else None

    return ChampSimSummary(
        benchmark=benchmark,
        build_variant=build_variant,
        ok=True,
        ipc=float(getattr(result, "ipc", 0.0) or 0.0),
        instructions=int(getattr(result, "instructions", 0) or 0),
        cycles=int(getattr(result, "cycles", 0) or 0),
        l1d_mpki=_cache_mpki(l1d),
        l1d_accuracy=getattr(l1d_pf, "accuracy", None) if l1d_pf else None,
        l1d_coverage=getattr(l1d_pf, "coverage", None) if l1d_pf else None,
        l2_mpki=_cache_mpki(l2),
        hints_seen=_as_float(custom.get("hg_hints_seen")),
        prefetches_issued=_as_float(custom.get("hg_prefetches_issued")),
        prefetches_dropped=_as_float(custom.get("hg_prefetches_dropped")),
        late_prefetches=_as_float(custom.get("hg_late_prefetches")),
        phq_full_events=_as_float(custom.get("hg_phq_full_events")),
        wall_s=getattr(result, "wall_s", 0.0) or 0.0,
    )


def trace_path_for(benchmark: str) -> str:
    """Locate the ChampSim trace for a benchmark.

    Accepts the common naming conventions rather than mandating one, because
    trace provenance varies (locally generated vs. downloaded).
    """
    base = config.TRACE_DIR
    candidates = [
        base / f"{benchmark}.champsimtrace.xz",
        base / f"{benchmark}.champsim.xz",
        base / f"{benchmark}.trace.xz",
        base / f"{benchmark}.trace.gz",
        base / f"{benchmark}.champsimtrace",
        base / benchmark / "trace.champsimtrace.xz",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    # Return the canonical name anyway: the run node will report a clean
    # "trace missing" failure, which is more useful than an exception here.
    return str(candidates[0])


def module_name_for(genome: Genome) -> str:
    """ChampSim module names must match ``[a-zA-Z_][a-zA-Z0-9_]*``."""
    return f"hg_{genome.champsim_hash}"
