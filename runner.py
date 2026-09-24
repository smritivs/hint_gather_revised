"""Execution helpers shared by every node in the HINT.GATHER loop.

Two jobs:

1. **Dispatch.** Every node in this project is a ``@ChiaFunction``. In cluster
   mode we call ``fn.chia_remote(...)`` and let Ray place it on a worker
   exposing the right resource; in local mode we call the function directly in
   process. ``submit()`` / ``resolve()`` hide that difference so the loop body
   reads the same either way. Local mode exists because the whole pipeline fits
   comfortably on one 48-core workstation, and because a cluster is one more
   thing to debug the night before a deadline.

2. **Bookkeeping.** A tiny SQLite store recording every candidate, every
   evaluation, and every self-repair attempt. The repair success rate and the
   convergence curve are headline results of this project, so they are recorded
   as first-class data rather than scraped out of logs afterwards.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import config

_LOG_FORMAT = "%(asctime)s %(levelname).1s [%(name)s] %(message)s"


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=_LOG_FORMAT,
        datefmt="%H:%M:%S",
        stream=sys.stdout,
        force=True,
    )
    # Ray is extremely chatty at INFO.
    logging.getLogger("ray").setLevel(logging.WARNING)


log = logging.getLogger("hg.runner")


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------

@dataclass
class Immediate:
    """A resolved value wearing a future's clothes (local mode)."""
    value: Any


def submit(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Schedule ``fn`` on the cluster, or run it here in local mode.

    ``fn`` is either a ``@ChiaFunction``-decorated function or one of the
    per-instance pinned handles that ``Gem5Node`` / ``ChampSimNode`` bind in
    their constructors. Both expose ``chia_remote`` and both are directly
    callable, which is what makes this switch possible.
    """
    if config.LOCAL_MODE:
        return Immediate(fn(*args, **kwargs))
    return fn.chia_remote(*args, **kwargs)


def resolve(ref: Any, *, timeout: float | None = None) -> Any:
    """Collect the result of :func:`submit` (or a list of them)."""
    if isinstance(ref, Immediate):
        return ref.value
    if isinstance(ref, (list, tuple)):
        return [resolve(r, timeout=timeout) for r in ref]
    from chia.base.ChiaFunction import get  # imported lazily: local mode needs no ray
    return get(ref, timeout=timeout)


def submit_all(fn: Any, arg_tuples: Iterable[Sequence[Any]]) -> list[Any]:
    """Fan out one node over many argument tuples."""
    return [submit(fn, *args) for args in arg_tuples]


@contextmanager
def phase(name: str):
    """Log + time a loop phase. Purely cosmetic, but the timings end up in the
    write-up, so they are collected consistently."""
    log.info("=== %s ===", name)
    start = time.time()
    try:
        yield
    finally:
        log.info("=== %s done in %.1fs ===", name, time.time() - start)


# --------------------------------------------------------------------------
# Result store
# --------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS candidates (
    genome_id     TEXT PRIMARY KEY,
    generation    INTEGER NOT NULL,
    origin        TEXT NOT NULL,
    parent_id     TEXT,
    genome_json   TEXT NOT NULL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    genome_id     TEXT NOT NULL,
    generation    INTEGER NOT NULL,
    stage         TEXT NOT NULL,          -- champsim | gem5_o3 | gate
    benchmark     TEXT NOT NULL,
    build_variant TEXT NOT NULL,          -- base | swpf | hint
    ok            INTEGER NOT NULL,
    ipc           REAL,
    fitness       REAL,
    metrics_json TEXT NOT NULL,
    wall_s        REAL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS repairs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    genome_id     TEXT,
    generation    INTEGER,
    node          TEXT NOT NULL,          -- llvm_build | bench_build | gem5_build | gem5_gate | champsim_build
    attempt       INTEGER NOT NULL,
    error_excerpt TEXT NOT NULL,
    succeeded     INTEGER NOT NULL,
    llm_latency_s REAL,
    diff_lines    INTEGER,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS generations (
    generation    INTEGER PRIMARY KEY,
    best_genome   TEXT,
    best_fitness REAL,
    mean_fitness REAL,
    fitness_lambda REAL,
    wall_s        REAL,
    created_at    REAL NOT NULL
);
"""


class Store:
    """Thread-safe SQLite wrapper.

    The loop fans out over Ray but all bookkeeping happens on the head node, so
    a single connection guarded by a lock is sufficient and avoids the
    write-contention foot-guns of multi-connection SQLite.
    """

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- writes ------------------------------------------------------------

    def record_candidate(self, genome) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO candidates "
                "(genome_id, generation, origin, parent_id, genome_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (genome.genome_id, genome.generation, genome.origin,
                 genome.parent_id, genome.to_json(indent=0), time.time()),
            )
            self._conn.commit()

    def record_evaluation(self, *, genome_id: str, generation: int, stage: str,
                          benchmark: str, build_variant: str, ok: bool,
                          ipc: float | None, fitness: float | None,
                          metrics: dict[str, Any], wall_s: float | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO evaluations (genome_id, generation, stage, benchmark, "
                "build_variant, ok, ipc, fitness, metrics_json, wall_s, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (genome_id, generation, stage, benchmark, build_variant,
                 int(ok), ipc, fitness, json.dumps(metrics, default=str),
                 wall_s, time.time()),
            )
            self._conn.commit()

    def record_repair(self, *, genome_id: str | None, generation: int | None,
                      node: str, attempt: int, error_excerpt: str,
                      succeeded: bool, llm_latency_s: float | None = None,
                      diff_lines: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO repairs (genome_id, generation, node, attempt, "
                "error_excerpt, succeeded, llm_latency_s, diff_lines, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (genome_id, generation, node, attempt, error_excerpt[-4000:],
                 int(succeeded), llm_latency_s, diff_lines, time.time()),
            )
            self._conn.commit()

    def record_generation(self, *, generation: int, best_genome: str,
                          best_fitness: float, mean_fitness: float,
                          fitness_lambda: float, wall_s: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO generations (generation, best_genome, "
                "best_fitness, mean_fitness, fitness_lambda, wall_s, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (generation, best_genome, best_fitness, mean_fitness,
                 fitness_lambda, wall_s, time.time()),
            )
            self._conn.commit()

    # -- reads -------------------------------------------------------------

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params))

    def repair_success_rate(self) -> tuple[int, int]:
        rows = self.query(
            "SELECT SUM(succeeded) AS ok, COUNT(*) AS total FROM repairs")
        if not rows or rows[0]["total"] is None:
            return 0, 0
        return int(rows[0]["ok"] or 0), int(rows[0]["total"] or 0)

    def convergence(self) -> list[tuple[int, float, float]]:
        return [
            (r["generation"], r["best_fitness"], r["mean_fitness"])
            for r in self.query(
                "SELECT generation, best_fitness, mean_fitness "
                "FROM generations ORDER BY generation")
        ]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


# --------------------------------------------------------------------------
# Small utilities used across nodes
# --------------------------------------------------------------------------

def run_dir_for(generation: int, genome_id: str) -> Path:
    """Per-candidate scratch directory on the head node."""
    d = Path(config.RUN_DIR) / f"gen{generation:03d}" / genome_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_json(path: Path | str, payload: Any) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    path.write_text(text)
    return text


def read_json(path: Path | str, default: Any = None) -> Any:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return default


def excerpt(text: str, limit: int = 3000) -> str:
    """Trim tool output for an LLM prompt, keeping the tail (where errors live)."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return "...[truncated]...\n" + text[-limit:]


def env_with(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env
