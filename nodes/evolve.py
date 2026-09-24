"""Node 5: evolutionary search with a self-correcting fitness function.

The outer loop is a small elitist genetic algorithm over the genome, with two
mutation operators: a cheap random one that explores, and an LLM operator that
reads the run history and proposes directed changes.

The interesting part is not the GA -- it is the **fitness**.  The fast
evaluator (ChampSim) is structurally blind to the cost this design exists to
eliminate: issue-queue and load-store-queue occupancy.  Optimising raw ChampSim
IPC would therefore reward hint spam, and the search would confidently converge
on a design that is worse on real hardware.

Two countermeasures, both implemented here:

1. A **density penalty**: fitness is docked ``lambda * hints_per_1k_insts``, so
   coverage bought with instruction bandwidth is not free.
2. **Re-anchoring**: every ``REANCHOR_EVERY`` generations the top candidates are
   run in gem5 O3, and ``lambda`` is refitted so that the fast proxy ranks
   candidates the way the slow, trustworthy model does.

See ``docs/DESIGN.md`` sections 5.1 and 6.
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field

import config
from genome import Genome, crossover, mutate, random_genome

log = logging.getLogger("hg.evolve")


# --------------------------------------------------------------------------
# Per-candidate evaluation record
# --------------------------------------------------------------------------

@dataclass
class CandidateResult:
    """Everything known about one genome after a generation."""
    genome: Genome
    fitness: float = float("-inf")
    ok: bool = False
    failure_stage: str = ""          # build | gate | champsim | ""
    failure_reason: str = ""

    # Per-benchmark fast metrics, keyed by benchmark name.
    speedup_vs_swpf: dict[str, float] = field(default_factory=dict)
    speedup_vs_base: dict[str, float] = field(default_factory=dict)
    hints_per_1k: dict[str, float] = field(default_factory=dict)
    wasted_rate: dict[str, float] = field(default_factory=dict)
    l1d_mpki: dict[str, float] = field(default_factory=dict)

    # Slow validation, filled only for re-anchored candidates.
    o3_speedup_vs_swpf: dict[str, float] = field(default_factory=dict)
    o3_hints_per_1k: dict[str, float] = field(default_factory=dict)

    repairs_used: int = 0

    def mean(self, values: dict[str, float]) -> float:
        return (sum(values.values()) / len(values)) if values else 0.0

    @property
    def mean_speedup(self) -> float:
        return self.mean(self.speedup_vs_swpf)

    @property
    def mean_hint_density(self) -> float:
        return self.mean(self.hints_per_1k)

    @property
    def mean_o3_speedup(self) -> float:
        return self.mean(self.o3_speedup_vs_swpf)

    def summary(self) -> str:
        if not self.ok:
            return (f"{self.genome.genome_id} FAILED at {self.failure_stage}: "
                    f"{self.failure_reason[:120]}")
        return (f"{self.genome.genome_id} fit={self.fitness:+.4f} "
                f"spd={self.mean_speedup:.3f}x "
                f"dens={self.mean_hint_density:.2f}/kI "
                f"waste={self.mean(self.wasted_rate):.2f} "
                f"[{self.genome.short()}]")


# --------------------------------------------------------------------------
# Fitness
# --------------------------------------------------------------------------

# A candidate that fails to build, fails the gate, or crashes the simulator is
# not scored on a continuum -- it is simply out.  We use a large negative
# sentinel rather than -inf so that selection can still order failures (a build
# failure is "less bad" than a correctness violation, which tells the LLM
# operator something useful).
FAILURE_SCORES = {
    "build": -10.0,
    "champsim": -20.0,
    "gate": -50.0,          # architectural or structural violation: worst
}


def compute_fitness(result: CandidateResult, *, lambda_value: float,
                    mu_value: float) -> float:
    """Penalised speedup over the *software-prefetch* baseline.

    Measuring against ``swpf`` rather than ``base`` is deliberate and is the
    honest comparison: beating "no prefetching at all" only proves prefetching
    works, which nobody doubts.  The claim under test is that the hint beats the
    same prefetches expressed as ordinary instructions.
    """
    if not result.ok:
        return FAILURE_SCORES.get(result.failure_stage, -30.0)

    speedup = result.mean_speedup
    if speedup <= 0 or math.isnan(speedup):
        return FAILURE_SCORES["champsim"]

    density_penalty = lambda_value * result.mean_hint_density
    waste_penalty = mu_value * result.mean(result.wasted_rate)
    return speedup - density_penalty - waste_penalty


def recalibrate_lambda(results: list[CandidateResult], current: float,
                       *, min_points: int = 3) -> tuple[float, str]:
    """Refit the density penalty against real gem5 O3 outcomes.

    Given candidates for which we have both a fast speedup and a slow O3
    speedup, we solve for the ``lambda`` that makes the fast score best explain
    the slow one:

        o3_speedup ~= champsim_speedup - lambda * hint_density

    i.e. ``lambda`` is the slope of (champsim_speedup - o3_speedup) against
    hint density -- literally "how much IPC does each hint per 1k instructions
    actually cost once the pipeline is modelled".  That is exactly the quantity
    ChampSim cannot see, which is why we measure it instead of guessing it.

    Returns ``(lambda, explanation)``.  Falls back to the current value when
    there is not enough signal, because a badly-fit penalty is worse than a
    conservative one.
    """
    points = [
        (r.mean_hint_density, r.mean_speedup - r.mean_o3_speedup)
        for r in results
        if r.ok and r.o3_speedup_vs_swpf and r.mean_hint_density > 0
    ]
    if len(points) < min_points:
        return current, (f"kept lambda={current:.4f}: only {len(points)} "
                         f"anchored point(s), need {min_points}")

    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    var_x = sum((x - mean_x) ** 2 for x, _ in points)
    if var_x < 1e-12:
        return current, (f"kept lambda={current:.4f}: hint density is constant "
                         f"across anchored candidates, slope undefined")

    slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / var_x

    # Clamp: a negative slope would mean hints are free or beneficial in the
    # pipeline, which we refuse to encode (it would license unbounded spam);
    # an enormous slope usually means one outlier, not a law of nature.
    fitted = min(max(slope, 0.0), 0.5)
    # Damp the update so one noisy generation cannot swing the search.
    updated = 0.5 * current + 0.5 * fitted
    return updated, (f"lambda {current:.4f} -> {updated:.4f} "
                     f"(fitted slope {slope:+.4f} over {n} anchored candidates)")


# --------------------------------------------------------------------------
# Selection and reproduction
# --------------------------------------------------------------------------

def select_elites(results: list[CandidateResult], count: int
                  ) -> list[CandidateResult]:
    ranked = sorted(results, key=lambda r: r.fitness, reverse=True)
    return ranked[:max(1, count)]


def tournament(results: list[CandidateResult], rng: random.Random, k: int = 3
               ) -> CandidateResult:
    pool = rng.sample(results, min(k, len(results)))
    return max(pool, key=lambda r: r.fitness)


def next_generation(results: list[CandidateResult], *, generation: int,
                    rng: random.Random, llm=None, lambda_value: float,
                    population_size: int | None = None,
                    elite_count: int | None = None,
                    llm_fraction: float | None = None) -> list[Genome]:
    """Build the next population: elites, LLM proposals, mutations, crossover.

    Composition is deliberately mixed.  The LLM operator is good at noticing
    "everything with fanout>4 is losing" and acting on it; the random operator
    is good at not believing that too hard.
    """
    population_size = population_size or config.POPULATION_SIZE
    elite_count = min(elite_count or config.ELITE_COUNT,
                      max(1, (population_size - 1) if population_size > 1 else 1))
    llm_fraction = (config.LLM_MUTATION_FRACTION if llm_fraction is None
                    else llm_fraction)

    if not results:
        from genome import seed_population
        return seed_population(population_size)

    elites = select_elites(results, elite_count)
    nxt: list[Genome] = []
    seen: set[str] = set()

    def _add(g: Genome) -> bool:
        g = g.repaired()
        if g.genome_id in seen:
            return False
        seen.add(g.genome_id)
        nxt.append(g)
        return True

    # 1. Carry the elites forward unchanged so progress can never regress.
    for e in elites:
        _add(Genome(**{**e.genome.search_dict(),
                       "origin": "elite",
                       "parent_id": e.genome.genome_id,
                       "generation": generation,
                       "notes": f"elite (fitness {e.fitness:+.4f})"}))

    # 2. LLM-proposed candidates.
    if llm is not None and llm_fraction > 0 and len(nxt) < population_size:
        want = max(1, int(round((population_size - len(nxt)) * llm_fraction)))
        if want > 0:
            proposals = _llm_proposals(results, elites, llm, want,
                                       generation, lambda_value)
            for p in proposals:
                if len(nxt) >= population_size:
                    break
                _add(p)

    # 3. Fill the rest with mutation and crossover of the survivors.
    viable = [r for r in results if r.ok] or results
    guard = 0
    while len(nxt) < population_size and guard < population_size * 20:
        guard += 1
        if len(viable) >= 2 and rng.random() < 0.3:
            a = tournament(viable, rng)
            b = tournament(viable, rng)
            child = crossover(a.genome, b.genome, rng, generation=generation)
        else:
            parent = tournament(viable, rng)
            child = mutate(parent.genome, rng, generation=generation)
        _add(child)

    # 4. Last resort: inject randomness rather than return a short population.
    while len(nxt) < population_size:
        _add(random_genome(rng, generation=generation))

    return nxt[:population_size]


def _llm_proposals(results: list[CandidateResult], elites: list[CandidateResult],
                   llm, count: int, generation: int, lambda_value: float
                   ) -> list[Genome]:
    """Ask the model for directed mutations, then sanitise hard."""
    from nodes.agents import propose_genomes

    history = format_history(results, limit=12)
    best = elites[0] if elites else None
    current_best = (f"{best.genome.to_json(search_only=True)}\n"
                    f"# fitness {best.fitness:+.4f}, mean speedup "
                    f"{best.mean_speedup:.3f}x vs software prefetch, "
                    f"{best.mean_hint_density:.2f} hints/1k insts"
                    if best else "none yet")

    raw = propose_genomes(llm, history=history, current_best=current_best,
                          count=count, lambda_value=lambda_value)

    out: list[Genome] = []
    for item in raw:
        try:
            g = Genome.from_dict({
                **item,
                "origin": "llm",
                "generation": generation,
                "parent_id": best.genome.genome_id if best else "",
            })
        except (TypeError, ValueError) as e:
            log.warning("discarding malformed LLM proposal: %s", e)
            continue
        repaired = g.repaired()
        problems = repaired.validate()
        if problems:
            log.warning("discarding LLM proposal that survives repair badly: %s",
                        "; ".join(problems[:3]))
            continue
        out.append(repaired)
    log.info("LLM proposed %d genome(s), %d usable", len(raw), len(out))
    return out


def format_history(results: list[CandidateResult], *, limit: int = 12) -> str:
    """Compact, model-readable table of what has been tried.

    Includes failures: knowing that ``variant=value`` with ``phq_entries=2``
    fails the gate is at least as useful as knowing what scored well.
    """
    ranked = sorted(results, key=lambda r: r.fitness, reverse=True)[:limit]
    lines = [
        "| fitness | speedup_vs_swpf | hints/1k | waste | genome |",
        "|---------|-----------------|----------|-------|--------|",
    ]
    for r in ranked:
        if r.ok:
            lines.append(
                f"| {r.fitness:+.4f} | {r.mean_speedup:.3f} | "
                f"{r.mean_hint_density:.2f} | {r.mean(r.wasted_rate):.2f} | "
                f"{r.genome.short()} |")
        else:
            lines.append(
                f"| FAILED({r.failure_stage}) | - | - | - | "
                f"{r.genome.short()} -- {r.failure_reason[:80]} |")
    return "\n".join(lines)


def generation_stats(results: list[CandidateResult]) -> tuple[float, float]:
    """(best, mean) fitness over the scored candidates."""
    if not results:
        return float("nan"), float("nan")
    scores = [r.fitness for r in results]
    return max(scores), sum(scores) / len(scores)
