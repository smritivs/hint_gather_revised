"""The HINT.GATHER search genome.

A genome is the complete, serialisable description of one candidate design
point: what the compiler emits, how the microarchitecture behaves, and how the
memory-system model is configured.  It is the single object that crosses every
node boundary in the loop.

The schema is normative and is documented in ``docs/DESIGN.md`` section 3.  Keys
are stable; add a new key only by updating both this file and DESIGN.md.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Iterable


# --------------------------------------------------------------------------
# Search space
# --------------------------------------------------------------------------
# Each entry is (kind, domain).  ``int``/``float`` domains are (lo, hi)
# inclusive; ``choice`` domains are a tuple of allowed values; ``bool`` has no
# domain.  The mutation operator and the validator both read this table, so
# they can never disagree about what is legal.

SCHEMA: dict[str, tuple[str, Any]] = {
    "hint_distance":            ("int",    (1, 512)),
    "fanout":                   ("int",    (1, 8)),
    "entropy_threshold":        ("float",  (0.0, 1.0)),
    "variant":                  ("choice", ("value", "chase")),
    "prefetch_level":           ("choice", ("L1D", "L2C")),
    "droppable":                ("bool",   None),
    "phq_entries":              ("choice", (2, 4, 8, 16, 32)),
    "phq_dispatch_width":       ("int",    (1, 4)),
    "phq_poll_limit":           ("int",    (1, 64)),
    "wakeup_policy":            ("choice", ("poll_rf", "tag_snoop")),
    "tlb_miss_policy":          ("choice", ("drop", "walk")),
    "mshr_pressure_threshold":  ("float",  (0.0, 1.0)),
    "max_hints_per_loop":       ("int",    (1, 8)),
    "min_trip_count":           ("choice", (16, 32, 64, 128, 256)),
}

# Keys each downstream component actually reads.  Used to compute component
# specific hashes so the loop can skip rebuilds that cannot possibly matter --
# e.g. changing ``phq_poll_limit`` must not trigger a benchmark recompile.
LLVM_KEYS = (
    "hint_distance", "fanout", "entropy_threshold", "variant",
    "prefetch_level", "droppable", "max_hints_per_loop", "min_trip_count",
)
GEM5_KEYS = (
    "fanout", "variant", "prefetch_level", "droppable", "phq_entries",
    "phq_dispatch_width", "phq_poll_limit", "wakeup_policy",
    "tlb_miss_policy", "mshr_pressure_threshold",
)
CHAMPSIM_KEYS = (
    "hint_distance", "fanout", "variant", "prefetch_level", "droppable",
    "phq_entries", "mshr_pressure_threshold",
)


@dataclass(frozen=True)
class Genome:
    """One candidate design point.  Immutable; mutation returns a new Genome."""

    hint_distance: int = 64
    fanout: int = 8
    entropy_threshold: float = 0.35
    variant: str = "chase"
    prefetch_level: str = "L1D"
    droppable: bool = True
    phq_entries: int = 32
    phq_dispatch_width: int = 4
    phq_poll_limit: int = 32
    wakeup_policy: str = "poll_rf"
    tlb_miss_policy: str = "drop"
    mshr_pressure_threshold: float = 0.90
    max_hints_per_loop: int = 1
    min_trip_count: int = 64

    # Provenance -- not part of the search space, never hashed into the
    # component keys, but carried along so the database can reconstruct the
    # lineage of every candidate.
    origin: str = "seed"          # seed | random | llm | crossover | elite
    parent_id: str = ""
    generation: int = 0
    notes: str = ""

    # -- validation --------------------------------------------------------

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty means valid)."""
        problems: list[str] = []
        for key, (kind, domain) in SCHEMA.items():
            value = getattr(self, key)
            if kind == "int":
                lo, hi = domain
                if not isinstance(value, int) or isinstance(value, bool):
                    problems.append(f"{key}: expected int, got {value!r}")
                elif not (lo <= value <= hi):
                    problems.append(f"{key}: {value} outside [{lo}, {hi}]")
            elif kind == "float":
                lo, hi = domain
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    problems.append(f"{key}: expected float, got {value!r}")
                elif not (lo <= float(value) <= hi):
                    problems.append(f"{key}: {value} outside [{lo}, {hi}]")
            elif kind == "bool":
                if not isinstance(value, bool):
                    problems.append(f"{key}: expected bool, got {value!r}")
            elif kind == "choice":
                if value not in domain:
                    problems.append(f"{key}: {value!r} not in {domain}")

        # Cross-field constraints that the schema cannot express.
        if self.variant == "chase" and self.wakeup_policy == "tag_snoop":
            # Harmless but meaningless: the chase variant never waits on a
            # register operand, so a wake-up CAM would be dead silicon.
            problems.append(
                "variant=chase with wakeup_policy=tag_snoop: the chase variant "
                "has no register operand to wake up on; use poll_rf"
            )
        if self.phq_dispatch_width > self.phq_entries:
            problems.append(
                f"phq_dispatch_width ({self.phq_dispatch_width}) > phq_entries "
                f"({self.phq_entries})"
            )
        return problems

    def repaired(self) -> "Genome":
        """Clamp/snap this genome into the legal space.

        The LLM mutation operator occasionally proposes out-of-range values.
        Rejecting the whole candidate would waste a generation, so we clamp and
        record what happened in ``notes``.
        """
        patch: dict[str, Any] = {}
        fixes: list[str] = []
        for key, (kind, domain) in SCHEMA.items():
            value = getattr(self, key)
            if kind in ("int", "float"):
                lo, hi = domain
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    numeric = float(getattr(Genome(), key))
                    fixes.append(f"{key} non-numeric -> default")
                clamped = min(max(numeric, lo), hi)
                clamped = int(round(clamped)) if kind == "int" else float(clamped)
                if clamped != value:
                    patch[key] = clamped
                    fixes.append(f"{key} {value} -> {clamped}")
            elif kind == "bool":
                if not isinstance(value, bool):
                    patch[key] = bool(value)
                    fixes.append(f"{key} {value!r} -> {bool(value)}")
            elif kind == "choice":
                if value not in domain:
                    nearest = _nearest_choice(value, domain)
                    patch[key] = nearest
                    fixes.append(f"{key} {value!r} -> {nearest!r}")

        merged = {**asdict(self), **patch}
        if merged["variant"] == "chase" and merged["wakeup_policy"] == "tag_snoop":
            merged["wakeup_policy"] = "poll_rf"
            fixes.append("wakeup_policy tag_snoop -> poll_rf (chase variant)")
        if merged["phq_dispatch_width"] > merged["phq_entries"]:
            merged["phq_dispatch_width"] = merged["phq_entries"]
            fixes.append("phq_dispatch_width clamped to phq_entries")

        if fixes:
            note = "; ".join(fixes)
            merged["notes"] = f"{merged.get('notes', '')} [repaired: {note}]".strip()
        return Genome(**merged)

    # -- identity ----------------------------------------------------------

    def _hash_over(self, keys: Iterable[str]) -> str:
        payload = json.dumps(
            {k: getattr(self, k) for k in sorted(keys)},
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    @property
    def genome_id(self) -> str:
        """Stable identity over the *search space* only (ignores provenance)."""
        return self._hash_over(SCHEMA.keys())

    @property
    def llvm_hash(self) -> str:
        """Changes iff the benchmarks would compile differently."""
        return self._hash_over(LLVM_KEYS)

    @property
    def gem5_hash(self) -> str:
        """Changes iff the gem5 model would behave differently."""
        return self._hash_over(GEM5_KEYS)

    @property
    def champsim_hash(self) -> str:
        """Changes iff the ChampSim module would behave differently."""
        return self._hash_over(CHAMPSIM_KEYS)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def search_dict(self) -> dict[str, Any]:
        """Only the searchable keys -- this is what DESIGN.md sec 3 specifies
        and what is written to the genome.json consumed by the LLVM pass."""
        return {k: getattr(self, k) for k in SCHEMA}

    def to_json(self, *, search_only: bool = False, indent: int = 2) -> str:
        data = self.search_dict() if search_only else self.to_dict()
        return json.dumps(data, indent=indent, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Genome":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    @classmethod
    def from_json(cls, text: str) -> "Genome":
        return cls.from_dict(json.loads(text))

    # -- instruction encoding ---------------------------------------------
    # Mirrors docs/DESIGN.md sec 1.1 so the loop can cross-check what the pass
    # emitted without parsing the binary.

    @property
    def funct3(self) -> int:
        variant_bit = 1 if self.variant == "chase" else 0
        level_bit = 1 if self.prefetch_level == "L2C" else 0
        drop_bit = 1 if self.droppable else 0
        return variant_bit | (level_bit << 1) | (drop_bit << 2)

    def funct7(self, elem_size_bytes: int) -> int:
        shift = {1: 0, 2: 1, 4: 2, 8: 3}.get(elem_size_bytes)
        if shift is None:
            raise ValueError(f"unsupported element size {elem_size_bytes}")
        return (shift & 0x3) | ((self.fanout - 1) & 0x7) << 2

    def short(self) -> str:
        """Compact one-line description for logs and database rows."""
        return (
            f"{self.genome_id} d={self.hint_distance} f={self.fanout} "
            f"{self.variant}/{self.prefetch_level} phq={self.phq_entries} "
            f"wake={self.wakeup_policy} ent>{self.entropy_threshold:.2f}"
            f"{' drop' if self.droppable else ''}"
        )


def _nearest_choice(value: Any, domain: tuple) -> Any:
    """Snap an illegal choice to the closest legal one.

    Numeric domains snap by distance; string domains fall back to the first
    element (which is the documented default ordering in SCHEMA).
    """
    if all(isinstance(d, (int, float)) for d in domain):
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return domain[0]
        return min(domain, key=lambda d: abs(float(d) - numeric))
    if isinstance(value, str):
        lowered = value.strip().lower()
        for d in domain:
            if isinstance(d, str) and d.lower() == lowered:
                return d
    return domain[0]


# --------------------------------------------------------------------------
# Seeds and mutation
# --------------------------------------------------------------------------

def seed_population(size: int) -> list[Genome]:
    """A deliberately spread-out initial population.

    The first four are hand-picked to cover the corners of the design argument
    (cheap vs aggressive, both variants, both prefetch levels) so that even a
    single-generation run produces an interesting comparison.
    """
    seeds = [
        Genome(origin="seed", notes="conservative chase, L1D"),
        Genome(hint_distance=64, fanout=2, variant="chase",
               prefetch_level="L2C", phq_entries=8,
               origin="seed", notes="deeper, wider, L2"),
        Genome(hint_distance=16, fanout=1, variant="value",
               wakeup_policy="poll_rf", phq_entries=4,
               origin="seed", notes="value form, minimal PHQ"),
        Genome(hint_distance=96, fanout=4, variant="chase",
               prefetch_level="L1D", phq_entries=16, entropy_threshold=0.2,
               origin="seed", notes="aggressive"),
    ]
    rng = random.Random(0xC71A)
    while len(seeds) < size:
        seeds.append(random_genome(rng, origin="seed"))
    return [g.repaired() for g in seeds[:size]]


def random_genome(rng: random.Random, *, origin: str = "random",
                  generation: int = 0) -> Genome:
    values: dict[str, Any] = {}
    for key, (kind, domain) in SCHEMA.items():
        if kind == "int":
            lo, hi = domain
            values[key] = rng.randint(lo, hi)
        elif kind == "float":
            lo, hi = domain
            values[key] = round(rng.uniform(lo, hi), 3)
        elif kind == "bool":
            values[key] = rng.random() < 0.5
        elif kind == "choice":
            values[key] = rng.choice(list(domain))
    return Genome(**values, origin=origin, generation=generation).repaired()


def mutate(parent: Genome, rng: random.Random, *, rate: float = 0.3,
           generation: int = 0) -> Genome:
    """Random mutation: perturb each key with probability ``rate``.

    Numeric keys take a bounded relative step rather than a fresh draw, so the
    operator does local search around a good parent instead of restarting.
    """
    values = parent.search_dict()
    touched: list[str] = []
    for key, (kind, domain) in SCHEMA.items():
        if rng.random() >= rate:
            continue
        touched.append(key)
        if kind == "int":
            lo, hi = domain
            span = max(1, int((hi - lo) * 0.15))
            values[key] = min(max(values[key] + rng.randint(-span, span), lo), hi)
        elif kind == "float":
            lo, hi = domain
            span = (hi - lo) * 0.15
            values[key] = round(
                min(max(values[key] + rng.uniform(-span, span), lo), hi), 3)
        elif kind == "bool":
            values[key] = not values[key]
        elif kind == "choice":
            alternatives = [d for d in domain if d != values[key]]
            if alternatives:
                values[key] = rng.choice(alternatives)

    if not touched:  # guarantee forward progress
        key = rng.choice(list(SCHEMA))
        touched.append(key)
        return mutate(parent, rng, rate=1.0, generation=generation)

    return Genome(
        **values,
        origin="random",
        parent_id=parent.genome_id,
        generation=generation,
        notes=f"mutated {', '.join(touched)}",
    ).repaired()


def crossover(a: Genome, b: Genome, rng: random.Random, *,
               generation: int = 0) -> Genome:
    """Uniform crossover over the searchable keys."""
    values = {
        key: (getattr(a, key) if rng.random() < 0.5 else getattr(b, key))
        for key in SCHEMA
    }
    return Genome(
        **values,
        origin="crossover",
        parent_id=f"{a.genome_id}+{b.genome_id}",
        generation=generation,
        notes="uniform crossover",
    ).repaired()
