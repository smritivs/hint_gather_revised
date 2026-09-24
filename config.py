"""Site configuration for the HINT.GATHER CHIA loop.

Every site-specific path and default knob lives here so that
``hint_gather_loop.py`` stays free of hard-coded constants.  Everything can be
overridden with an environment variable, which is how the cluster YAML and the
setup script inject per-worker paths.

See ``docs/DESIGN.md`` for the normative interface contract.
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# Repository layout
# --------------------------------------------------------------------------

# Resolve from this file rather than cwd: the loop may be executed from a
# ``chia job submit --working-dir`` snapshot whose cwd is a temp directory.
HG_ROOT = Path(__file__).resolve().parent

LLVM_DIR = HG_ROOT / "llvm"
GEM5_DIR = HG_ROOT / "gem5"
CHAMPSIM_DIR = HG_ROOT / "champsim"
BENCH_DIR = HG_ROOT / "bench"
PROMPTS_DIR = HG_ROOT / "prompts"
DOCS_DIR = HG_ROOT / "docs"


def _env_path(name: str, default: Path | str) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --------------------------------------------------------------------------
# External tool checkouts (on the workers)
# --------------------------------------------------------------------------

# Root of the scratch area the setup script populates.  Everything below
# defaults to a subdirectory of this, so a single env var relocates the world.
TOOLS_ROOT = _env_path(
    "HG_TOOLS_ROOT",
    os.environ.get("HG_TOOLS_DIR", str(Path.home() / "hg-tools")),
)

# Conda environment containing Python 3.10, SCons, GCC 12, lld, and the
# riscv64-unknown-elf sysroot/toolchain.
_default_conda = (
    TOOLS_ROOT / "miniconda3" / "envs" / "hint-gather"
    if (TOOLS_ROOT / "miniconda3" / "envs" / "hint-gather").exists()
    else (Path.home() / "miniconda3" / "envs" / "hint-gather")
)
_raw_conda = _env_path("CONDA_PREFIX", _default_conda)
if (_raw_conda / "envs" / "hint-gather").exists():
    CONDA_PREFIX = _raw_conda / "envs" / "hint-gather"
else:
    CONDA_PREFIX = _raw_conda

import sys as _sys
for _sp in sorted((CONDA_PREFIX / "lib").glob("python3.*/site-packages")):
    if str(_sp) not in _sys.path:
        _sys.path.insert(0, str(_sp))

# Prebuilt LLVM release or Conda LLVM 17 (lib/cmake/llvm must exist under it).
_default_llvm = (
    TOOLS_ROOT / "llvm-17"
    if (TOOLS_ROOT / "llvm-17" / "bin" / "clang").exists()
    else (
        TOOLS_ROOT / "llvm"
        if (TOOLS_ROOT / "llvm" / "bin" / "clang").exists()
        else CONDA_PREFIX
    )
)
LLVM_INSTALL = _env_path("HG_LLVM_INSTALL", _default_llvm)
LLVM_CMAKE_DIR = _env_path(
    "HG_LLVM_CMAKE_DIR", LLVM_INSTALL / "lib" / "cmake" / "llvm"
)
CLANG = _env_path("HG_CLANG", LLVM_INSTALL / "bin" / "clang")

# Where the pass plugin is built (prefer in-tree llvm/build/libHintGather.so).
_in_tree_build = HG_ROOT / "llvm" / "build"
_default_pass_build = (
    _in_tree_build
    if (_in_tree_build / "libHintGather.so").exists() or not (TOOLS_ROOT / "hint-gather-pass-build").exists()
    else (TOOLS_ROOT / "hint-gather-pass-build")
)
PASS_BUILD_DIR = _env_path("HG_PASS_BUILD", _default_pass_build)
PASS_PLUGIN = _env_path(
    "HG_PASS_PLUGIN",
    os.environ.get("HG_PLUGIN", str(PASS_BUILD_DIR / "libHintGather.so")),
)

# RISC-V sysroot / toolchain for building the benchmark ELFs.
_default_riscv = (
    CONDA_PREFIX
    if (CONDA_PREFIX / "bin" / "riscv64-unknown-elf-gcc").exists()
    else (TOOLS_ROOT / "riscv")
)
RISCV_TOOLCHAIN = _env_path("HG_RISCV_TOOLCHAIN", _default_riscv)
RISCV_TARGET = os.environ.get("HG_RISCV_TARGET", "riscv64-unknown-elf")

GEM5_ROOT = _env_path("HG_GEM5_ROOT", TOOLS_ROOT / "gem5")
GEM5_ISA = os.environ.get("HG_GEM5_ISA", "RISCV")
GEM5_VARIANT = os.environ.get("HG_GEM5_VARIANT", "opt")
GEM5_BIN = _env_path(
    "HG_GEM5_BIN", GEM5_ROOT / "build" / GEM5_ISA / f"gem5.{GEM5_VARIANT}"
)
GEM5_CONFIG = _env_path(
    "HG_GEM5_CONFIG", GEM5_DIR / "configs" / "hint_gather_se.py"
)

CHAMPSIM_ROOT = _env_path("HG_CHAMPSIM_ROOT", TOOLS_ROOT / "ChampSim")
# Directory of ChampSim traces, one per benchmark: <TRACE_DIR>/<bench>.champsimtrace.xz
TRACE_DIR = _env_path("HG_TRACE_DIR", TOOLS_ROOT / "traces")


def tool_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a subprocess environment with Conda, LLVM, and RISC-V paths set."""
    env = dict(os.environ)
    env["CONDA_PREFIX"] = str(CONDA_PREFIX)
    env["HG_TOOLS_ROOT"] = str(TOOLS_ROOT)
    env["HG_PLUGIN"] = str(PASS_PLUGIN)
    env["HG_PASS_PLUGIN"] = str(PASS_PLUGIN)

    ld_parts = [str(CONDA_PREFIX / "lib"), str(LLVM_INSTALL / "lib")]
    if env.get("LD_LIBRARY_PATH"):
        ld_parts.append(env["LD_LIBRARY_PATH"])
    env["LD_LIBRARY_PATH"] = ":".join(ld_parts)

    path_parts = [
        str(LLVM_INSTALL / "bin"),
        str(CONDA_PREFIX / "bin"),
        str(RISCV_TOOLCHAIN / "bin"),
    ]
    if env.get("PATH"):
        path_parts.append(env["PATH"])
    env["PATH"] = ":".join(path_parts)

    if extra:
        env.update(extra)
    return env


# Auto-populate process environment on import so all child processes inherit it.
os.environ.update(tool_env())

# --------------------------------------------------------------------------
# Loop outputs (head node)
# --------------------------------------------------------------------------

RUN_DIR = _env_path("HG_RUN_DIR", HG_ROOT / "runs")
DB_PATH = _env_path("HG_DB_PATH", RUN_DIR / "hint_gather.db")

# --------------------------------------------------------------------------
# Benchmarks (Node 0: target selection)
# --------------------------------------------------------------------------

# Ordered by how cheap they are to evaluate.  ``FAST_BENCHMARKS`` drive the
# inner evolutionary loop; ``HERO_BENCHMARKS`` are reserved for slow gem5 O3
# validation so we do not burn the budget on them every generation.
ALL_BENCHMARKS = ("gather", "bfs", "pagerank", "listchase")
FAST_BENCHMARKS = tuple(
    os.environ.get("HG_FAST_BENCHMARKS", "gather,bfs").split(",")
)
HERO_BENCHMARKS = tuple(
    os.environ.get("HG_HERO_BENCHMARKS", "gather,bfs").split(",")
)

# The three builds compared in every evaluation.  ``swpf`` is the honest
# baseline: the same prefetches expressed as ordinary instructions.
BUILD_VARIANTS = ("base", "swpf", "hint")

# --------------------------------------------------------------------------
# Simulation budgets
# --------------------------------------------------------------------------

CHAMPSIM_WARMUP_INSTS = _env_int("HG_CHAMPSIM_WARMUP", 5_000_000)
CHAMPSIM_SIM_INSTS = _env_int("HG_CHAMPSIM_SIM", 25_000_000)
CHAMPSIM_TIMEOUT_S = _env_int("HG_CHAMPSIM_TIMEOUT", 900)

GEM5_GATE_MAX_INSTS = _env_int("HG_GEM5_GATE_INSTS", 20_000_000)
GEM5_O3_MAX_INSTS = _env_int("HG_GEM5_O3_INSTS", 100_000_000)
GEM5_BUILD_TIMEOUT_S = _env_int("HG_GEM5_BUILD_TIMEOUT", 5400)
GEM5_RUN_TIMEOUT_S = _env_int("HG_GEM5_RUN_TIMEOUT", 5400)

# --------------------------------------------------------------------------
# Evolutionary search (Node 5)
# --------------------------------------------------------------------------

POPULATION_SIZE = _env_int("HG_POPULATION", 8)
GENERATIONS = _env_int("HG_GENERATIONS", 6)
ELITE_COUNT = _env_int("HG_ELITES", 2)
# Fraction of each generation produced by the LLM mutation operator rather than
# by the random operator.  The rest are random mutations of the elites.
LLM_MUTATION_FRACTION = _env_float("HG_LLM_MUTATION_FRACTION", 0.5)

# Fitness penalties -- see docs/DESIGN.md sec 5.1.  ``LAMBDA`` punishes hint
# density (the overhead ChampSim structurally cannot see); ``MU`` punishes
# wasted prefetches.
FITNESS_LAMBDA = _env_float("HG_FITNESS_LAMBDA", 0.01)
FITNESS_MU = _env_float("HG_FITNESS_MU", 0.05)
# Re-anchor LAMBDA against real gem5 O3 results every N generations.
REANCHOR_EVERY = _env_int("HG_REANCHOR_EVERY", 3)
REANCHOR_TOP_N = _env_int("HG_REANCHOR_TOP_N", 2)

# --------------------------------------------------------------------------
# Agents (Nodes 2, 3, 5)
# --------------------------------------------------------------------------

# One of: vertex | claude | opencode | codex.  ``vertex`` uses Gemini, which is
# what the hackathon hands out for free.
LLM_BACKEND = os.environ.get("HG_LLM_BACKEND", "vertex")
LLM_MODEL = os.environ.get("HG_LLM_MODEL", "gemini-2.5-pro")
# Maximum autonomous repair attempts per failing node before giving up on a
# candidate.  The success rate over these attempts is a headline metric.
MAX_REPAIR_ATTEMPTS = _env_int("HG_MAX_REPAIR_ATTEMPTS", 3)

# --------------------------------------------------------------------------
# Execution mode
# --------------------------------------------------------------------------

# When true, ChiaFunctions are invoked in-process instead of via
# ``.chia_remote()``.  Same nodes, same profiling, no cluster required -- this
# is how the whole loop runs on a single 48-core workstation.
LOCAL_MODE = _env_bool("HG_LOCAL", False)

# Resource labels the cluster YAML must expose.
RES_LLVM = {"llvm": 1}
RES_GEM5 = {"gem5": 1.0}
RES_CHAMPSIM = {"champsim": 1.0}


def describe() -> str:
    """Human-readable dump of the resolved configuration (logged at startup)."""
    rows = [
        ("HG_ROOT", HG_ROOT),
        ("TOOLS_ROOT", TOOLS_ROOT),
        ("LLVM_INSTALL", LLVM_INSTALL),
        ("PASS_PLUGIN", PASS_PLUGIN),
        ("GEM5_ROOT", GEM5_ROOT),
        ("GEM5_BIN", GEM5_BIN),
        ("CHAMPSIM_ROOT", CHAMPSIM_ROOT),
        ("TRACE_DIR", TRACE_DIR),
        ("RUN_DIR", RUN_DIR),
        ("FAST_BENCHMARKS", ",".join(FAST_BENCHMARKS)),
        ("HERO_BENCHMARKS", ",".join(HERO_BENCHMARKS)),
        ("POPULATION_SIZE", POPULATION_SIZE),
        ("GENERATIONS", GENERATIONS),
        ("LLM_BACKEND", f"{LLM_BACKEND}:{LLM_MODEL}"),
        ("LOCAL_MODE", LOCAL_MODE),
    ]
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"  {k.ljust(width)} = {v}" for k, v in rows)
