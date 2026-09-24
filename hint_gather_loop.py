#!/usr/bin/env python3
"""HINT.GATHER: agentic co-design of zero-issue-queue prefetching.

The CHIA loop.  Run it with::

   chia job submit --working-dir . -- python hint_gather_loop.py --stage full

or, on a single machine with no cluster::

   HG_LOCAL=1 python hint_gather_loop.py --stage full

Graph shape (see docs/DESIGN.md sec 5)::

      Node 0   target selection
      Node 1   LLVM profiler      (SCEV filter + stride entropy)
      Node 2   compiler agent     --+ self-heals on build failure
      Node 3   microarch agent    --+ self-heals on build / assertion failure
      Gate     atomic equivalence + structural invariants
      Node 4   ChampSim fast eval
      Node 5   evolutionary search -> back to Node 2 for the next generation
      Node 6   gem5 O3 validation   -> re-anchors Node 5's fitness

Stages (``--stage``) let you run pieces of that graph independently, which is
how you debug a loop without paying for the whole loop:

      bootstrap   build the pass plugin, patch and build gem5, build ChampSim
      gate        bootstrap + correctness gate on the default genome
      fast        bootstrap + one ChampSim evaluation of the default genome
      evolve      the full inner loop (no O3 validation)
      full        everything, including periodic O3 re-anchoring and final report
      report      regenerate the report from an existing database
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from pathlib import Path

# The loop may be submitted with --working-dir, in which case the package is
# not on sys.path by default.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import config  # noqa: E402
import runner  # noqa: E402
from genome import Genome, seed_population  # noqa: E402
from nodes import agents, champsim_nodes, evolve, gem5_nodes, llvm_nodes  # noqa: E402
from nodes.evolve import CandidateResult  # noqa: E402
from runner import Store, phase, resolve, submit  # noqa: E402

log = logging.getLogger("hg.loop")


# ==========================================================================
# Node 0: target selection
# ==========================================================================

def select_targets(args) -> list[str]:
    """Pick the benchmark set.

    Deliberately narrow. These are workloads where indirect misses dominate,
    so the signal-to-noise ratio is high and a 25M-instruction window is enough
    to see the effect. Breadth is not the contribution here; the loop is.
    """
    if args.benchmarks:
        chosen = [b.strip() for b in args.benchmarks.split(",") if b.strip()]
    else:
        chosen = list(config.FAST_BENCHMARKS)
    unknown = [b for b in chosen if b not in config.ALL_BENCHMARKS]
    if unknown:
        raise SystemExit(
            f"unknown benchmark(s): {', '.join(unknown)}. "
            f"Known: {', '.join(config.ALL_BENCHMARKS)}")
    log.info("Node 0: targets = %s", ", ".join(chosen))
    return chosen


# ==========================================================================
# Bootstrap: get the toolchain into a buildable state (with self-healing)
# ==========================================================================

class Bootstrap:
    """One-time setup shared by every candidate in the run.

    The pass plugin and the gem5 binary are built once. Genome parameters that
    affect the *microarchitecture* are gem5 command-line flags, not compile-time
    constants, precisely so that exploring the PHQ design space does not cost a
    gem5 rebuild per candidate -- that single decision is worth roughly an
    order of magnitude in search throughput.
    """

    def __init__(self, store: Store, llm, *, skip_gem5: bool = False,
                 skip_champsim: bool = False):
        self.store = store
        self.llm = llm
        self.skip_gem5 = skip_gem5
        self.skip_champsim = skip_champsim
        self.plugin_path: str = ""
        self.gem5_node = None
        self.champsim_node = None
        self.gem5_binary: str = ""
        self.ok = False

    # -- Node 1/2 prerequisite ------------------------------------------

    def build_pass(self) -> bool:
        with phase("bootstrap: LLVM pass plugin"):
            def attempt():
                return resolve(submit(
                    llvm_nodes.build_pass_plugin,
                    str(config.LLVM_DIR), str(config.LLVM_CMAKE_DIR),
                    str(config.PASS_BUILD_DIR)))

            result = attempt()
            if not result.success:
                log.warning("pass plugin build failed; handing to the agent")
                repaired, attempts, ok = agents.repair_loop(
                    "llvm_build", attempt_action=attempt,
                    error=result.diagnostics, llm=self.llm, store=self.store)
                if not ok:
                    log.error("could not build the pass plugin after %d "
                              "repair attempt(s)", attempts)
                    return False
                result = repaired

            self.plugin_path = result.plugin_path
            log.info("pass plugin: %s (%.0fs)", result.plugin_path,
                     result.duration_s)
            return True

    # -- Node 3 prerequisite ---------------------------------------------

    def build_gem5(self) -> bool:
        if self.skip_gem5:
            log.info("skipping gem5 bootstrap (--skip-gem5)")
            return True

        from chia.simulators.gem5 import Gem5Node

        with phase("bootstrap: gem5 + PHQ"):
            self.gem5_node = Gem5Node(require_colocated=not config.LOCAL_MODE)

            patch_fn = gem5_nodes.pinned(gem5_nodes.apply_phq_patch,
                                          self.gem5_node)
            patch = resolve(submit(patch_fn, str(config.GEM5_ROOT),
                                   str(config.GEM5_DIR)))
            if not patch.success:
                # The usual cause is gem5 version drift: the patcher's anchor
                # strings no longer match this checkout. That is exactly the
                # kind of mechanical-but-fiddly problem the microarch agent is
                # for, so hand it the patcher's own error message.
                log.warning("PHQ patch failed (state=%s); handing to the "
                            "microarch agent", patch.state)
                outcome = agents.implement_microarchitecture(
                    llm=self.llm, store=self.store,
                    gem5_root=str(config.GEM5_ROOT))
                self.store.record_repair(
                    genome_id=None, generation=None, node="gem5_build",
                    attempt=1, error_excerpt=patch.diagnostics,
                    succeeded=outcome.success,
                    llm_latency_s=outcome.latency_s)
                patch = resolve(submit(patch_fn, str(config.GEM5_ROOT),
                                       str(config.GEM5_DIR)))
                if not patch.success:
                    log.error("PHQ patch still failing: %s", patch.diagnostics[:500])
                    return False

            log.info("PHQ applied (state=%s, %d file(s) touched)",
                     patch.state, len(patch.files_touched))

            def attempt_build():
                return resolve(submit(
                    self.gem5_node.build_gem5, str(config.GEM5_ROOT),
                    config.GEM5_ISA, config.GEM5_VARIANT,
                    timeout_s=config.GEM5_BUILD_TIMEOUT_S))

            build = attempt_build()

            if not build.success:
                log.warning("gem5 build failed; handing to the agent")
                repaired, attempts, ok = agents.repair_loop(
                    "gem5_build", attempt_action=attempt_build,
                    error=build.stderr_tail or build.stdout_tail,
                    llm=self.llm, store=self.store)
                if not ok:
                    log.error("gem5 would not build after %d repair attempt(s)",
                              attempts)
                    return False
                build = repaired

            self.gem5_binary = build.binary_path
            log.info("gem5: %s (%.0fs)", build.binary_path, build.build_duration_s)
            return True

    def prepare_champsim(self) -> bool:
        if self.skip_champsim:
            log.info("skipping ChampSim bootstrap (--skip-champsim)")
            return True
        if not list(config.TRACE_DIR.glob("*.xz")):
            log.info("no ChampSim .xz traces in %s; Evaluator will use gem5 O3 "
                     "evaluation directly", config.TRACE_DIR)
            self.champsim_node = None
            return True
        from chia.simulators.champsim import ChampSimNode
        self.champsim_node = ChampSimNode(require_colocated=not config.LOCAL_MODE)
        return True

    def run(self) -> bool:
        self.ok = (self.build_pass()
                   and self.build_gem5()
                   and self.prepare_champsim())
        return self.ok

    def close(self) -> None:
        for node in (self.gem5_node, self.champsim_node):
            try:
                if node is not None:
                    node.close()
            except Exception as e:
                log.warning("error closing node: %s", e)


# ==========================================================================
# Nodes 1-4: evaluate one candidate
# ==========================================================================

class Evaluator:
    """Runs one genome through analyse -> build -> gate -> ChampSim/gem5.

    Caches aggressively on the component hashes from ``genome.py``: two genomes
    that differ only in, say, ``phq_poll_limit`` must not trigger a benchmark
    recompile, and two that differ only in ``hint_distance`` must not re-run the
    correctness gate.
    """

    def __init__(self, boot: Bootstrap, store: Store, benchmarks: list[str],
                 llm, *, run_gate: bool = True):
        self.boot = boot
        self.store = store
        self.benchmarks = benchmarks
        self.llm = llm
        self.run_gate = run_gate
        self._site_cache: dict[tuple[str, str], object] = {}
        self._build_cache: dict[tuple[str, str, str], object] = {}
        self._gate_cache: dict[str, object] = {}
        self._profiles: dict[str, object] = {}
        self._o3_baseline_cache: dict[tuple[str, str], object] = {}

    # -- Node 1 -----------------------------------------------------------

    def profile_once(self, benchmark: str, genome: Genome, out_dir: Path):
        """Stride entropy is a property of the program, not the genome, so it
        is measured once per benchmark and reused for the whole run."""
        if benchmark in self._profiles:
            return self._profiles[benchmark]
        result = resolve(submit(
            llvm_nodes.profile_stride_entropy,
            str(config.BENCH_DIR), benchmark,
            genome.to_json(search_only=True),
            self.boot.plugin_path, str(config.CLANG), str(out_dir)))
        if not result.success:
            log.warning("profiling %s failed (%s); falling back to the static "
                        "heuristic", benchmark, result.diagnostics[:200])
        else:
            log.info("profiled %s: %d site(s), entropies %s", benchmark,
                     len(result.sites),
                     ", ".join(f"{s.get('entropy', 0):.2f}"
                               for s in result.sites[:6]))
        self._profiles[benchmark] = result
        return result

    def analyze(self, benchmark: str, genome: Genome, out_dir: Path):
        key = (benchmark, genome.llvm_hash)
        if key in self._site_cache:
            return self._site_cache[key]
        report = resolve(submit(
            llvm_nodes.analyze_hint_sites,
            str(config.BENCH_DIR), benchmark,
            genome.to_json(search_only=True),
            self.boot.plugin_path, str(config.CLANG), str(out_dir),
            target=config.RISCV_TARGET))
        self._site_cache[key] = report
        return report

    # -- Node 2 -----------------------------------------------------------

    def build(self, benchmark: str, variant: str, genome: Genome,
              out_dir: Path):
        key = (benchmark, variant, genome.llvm_hash)
        if key in self._build_cache:
            return self._build_cache[key]

        def attempt():
            return resolve(submit(
                llvm_nodes.build_benchmark,
                str(config.BENCH_DIR), benchmark, variant,
                genome.to_json(search_only=True),
                self.boot.plugin_path, str(out_dir)))

        result = attempt()
        if not result.success:
            repaired, _, ok = agents.repair_loop(
                "bench_build", attempt_action=attempt,
                error=result.diagnostics, llm=self.llm, store=self.store,
                genome=genome,
                extra={"benchmark": benchmark, "build_variant": variant})
            if ok:
                result = repaired

        self._build_cache[key] = result
        return result

    # -- Gate -------------------------------------------------------------

    def gate(self, genome: Genome, builds: dict[tuple[str, str], object],
             out_dir: Path):
        """Correctness gate, cached on the behaviour-determining hashes."""
        key = f"{genome.gem5_hash}:{genome.llvm_hash}"
        if key in self._gate_cache:
            return self._gate_cache[key]

        benchmark = self.benchmarks[0]
        hint = builds.get((benchmark, "hint"))
        base = builds.get((benchmark, "base"))
        if not (hint and base and hint.success and base.success):
            result = gem5_nodes.GateResult(
                False, "cannot gate: hint and base builds are not both present")
            self._gate_cache[key] = result
            return result

        gate_fn = gem5_nodes.pinned(gem5_nodes.run_correctness_gate,
                                    self.boot.gem5_node)

        def attempt():
            return resolve(submit(
                gate_fn, self.boot.gem5_binary, str(config.GEM5_CONFIG),
                str(config.GEM5_DIR / "tests" / "check_arch_equiv.py"),
                hint.elf_path, base.elf_path, str(out_dir / "gate"),
                genome.to_json(search_only=True),
                max_insts=config.GEM5_GATE_MAX_INSTS,
                timeout_s=config.GEM5_RUN_TIMEOUT_S))

        result = attempt()
        if not result.passed:
            kind = result.failure_kind
            log.warning("gate FAILED (%s): %s", kind, result.reason[:300])
            # An architectural divergence means the model is wrong, and that is
            # worth an agent's time. A structural violation likewise. A
            # timeout is an infrastructure problem, not a design problem, so we
            # do not waste repair budget on it.
            if kind in ("architectural_divergence", "structural_violation",
                        "no_hints_executed"):
                repaired, _, ok = agents.repair_loop(
                    "gem5_gate", attempt_action=attempt,
                    error=f"{result.reason}\n\n{result.stdout_tail}",
                    llm=self.llm, store=self.store, genome=genome,
                    extra={"failure_kind": kind})
                if ok:
                    result = repaired

        self._gate_cache[key] = result
        return result

    # -- Node 4 -----------------------------------------------------------

    def champsim(self, genome: Genome, benchmark: str, variant: str,
                 hint_sites: list[dict], builds):
        """Render, build and run the ChampSim model for one variant.

        The ``base`` and ``swpf`` variants use fixed baseline modules; only
        ``hint`` uses the generated one.
        """
        node = self.boot.champsim_node
        if node is None:
            return None

        if variant == "hint":
            try:
                source = champsim_nodes.render_prefetcher_source(
                    genome, hint_sites)
            except Exception as e:
                log.error("prefetcher template render failed: %s", e)
                return None
            module = champsim_nodes.module_name_for(genome)
        else:
            template = (config.CHAMPSIM_DIR / "baseline_prefetchers" /
                        ("hg_stride.h" if variant == "swpf" else "hg_none.h"))
            try:
                source = template.read_text()
            except OSError as e:
                log.error("baseline prefetcher %s unreadable: %s", template, e)
                return None
            module = "hg_stride" if variant == "swpf" else "hg_none"

        def attempt_build():
            return resolve(submit(
                node.build_champsim, str(config.CHAMPSIM_ROOT), source, module,
                cache_level=("L1D" if genome.prefetch_level == "L1D" else "L2C"),
                incremental=True))

        build = attempt_build()
        if not build.success:
            repaired, _, ok = agents.repair_loop(
                "champsim_build", attempt_action=attempt_build,
                error=build.build_diagnostics, llm=self.llm, store=self.store,
                genome=genome, extra={"module_name": module})
            if not ok:
                return None
            build = repaired

        run = resolve(submit(
            node.run_champsim, build.binary,
            champsim_nodes.trace_path_for(benchmark),
            warmup_instructions=config.CHAMPSIM_WARMUP_INSTS,
            simulation_instructions=config.CHAMPSIM_SIM_INSTS,
            timeout_s=config.CHAMPSIM_TIMEOUT_S))
        return champsim_nodes.summarize_champsim(run, benchmark, variant)

    # -- orchestration ----------------------------------------------------

    def evaluate(self, genome: Genome, generation: int) -> CandidateResult:
        out_dir = runner.run_dir_for(generation, genome.genome_id)
        result = CandidateResult(genome=genome)
        self.store.record_candidate(genome)

        # Node 1 ---------------------------------------------------------
        all_sites: dict[str, list[dict]] = {}
        for benchmark in self.benchmarks:
            self.profile_once(benchmark, genome, out_dir)
            report = self.analyze(benchmark, genome, out_dir)
            if not report.success:
                result.failure_stage = "build"
                result.failure_reason = f"analyze({benchmark}): {report.diagnostics[:300]}"
                return result
            all_sites[benchmark] = report.sites
            log.info("  %s: %d site(s), %d emitted, %d rejected as affine",
                     benchmark, len(report.sites), report.emitted_count,
                     report.rejected_affine)
        if report.emitted_count == 0:
            # Not an error: a strict entropy threshold legitimately hints
            # nothing. But such a candidate is identical to the baseline,
            # so score it as a build failure rather than burning simulator
            # time to rediscover that 1.0x is 1.0x.
            result.failure_stage = "build"
            result.failure_reason = (
                f"entropy_threshold={genome.entropy_threshold:.2f} rejected "
                f"every site in {benchmark}")
            return result

        # Node 2 ---------------------------------------------------------
        builds: dict[tuple[str, str], object] = {}
        for benchmark in self.benchmarks:
            for variant in config.BUILD_VARIANTS:
                b = self.build(benchmark, variant, genome, out_dir)
                builds[(benchmark, variant)] = b
                if not b.success:
                    result.failure_stage = "build"
                    result.failure_reason = (
                        f"{benchmark}.{variant}: {b.diagnostics[:300]}")
                    return result

        # Gate -----------------------------------------------------------
        if self.run_gate and self.boot.gem5_node is not None:
            gate = self.gate(genome, builds, out_dir)
            self.store.record_evaluation(
                genome_id=genome.genome_id, generation=generation,
                stage="gate", benchmark=self.benchmarks[0],
                build_variant="hint", ok=gate.passed, ipc=None, fitness=None,
                metrics={"reason": gate.reason,
                         "invariants": gate.invariant_values,
                         "hints_dispatched": gate.hints_dispatched},
                wall_s=gate.wall_s)
            if not gate.passed:
                result.failure_stage = "gate"
                result.failure_reason = gate.reason
                return result
            log.info("  gate PASS (hints=%.0f, invariants %s)",
                     gate.hints_dispatched,
                     ", ".join(f"{k}={v:g}" for k, v in
                               sorted(gate.invariant_values.items())) or "n/a")

        # Node 4 ---------------------------------------------------------
        if self.boot.champsim_node is None:
            if self.boot.gem5_node is None:
                result.ok = True
                return result
            run_fn = self.boot.gem5_node.run_gem5
            for benchmark in self.benchmarks:
                o3_runs = {}
                for variant in config.BUILD_VARIANTS:
                    cache_key = (benchmark, variant)
                    if variant in ("base", "swpf") and cache_key in self._o3_baseline_cache:
                        o3_runs[variant] = self._o3_baseline_cache[cache_key]
                        continue
                    b_info = builds.get((benchmark, variant))
                    elf = b_info.elf_path if b_info else str(out_dir / f"{benchmark}.{variant}.elf")
                    outdir = out_dir / "o3_eval" / f"{benchmark}.{variant}"
                    res = resolve(submit(
                        run_fn, self.boot.gem5_binary, str(config.GEM5_CONFIG),
                        str(outdir),
                        workload_name=f"{benchmark}.{variant}",
                        config_args=gem5_nodes.o3_config_args(
                             genome, str(elf), cpu_type="o3",
                             max_insts=0,
                             disable_phq=(variant != "hint")),
                        stats_keys=gem5_nodes.PHQ_STATS_KEYS,
                        timeout_s=config.GEM5_RUN_TIMEOUT_S))
                    s = gem5_nodes.summarize_o3(res, benchmark, variant)
                    if not s.ok:
                        result.failure_stage = "gem5_o3"
                        result.failure_reason = f"{benchmark}.{variant}: {s.error[:300]}"
                        return result
                    o3_runs[variant] = s
                    if variant in ("base", "swpf"):
                        self._o3_baseline_cache[cache_key] = s
                    self.store.record_evaluation(
                        genome_id=genome.genome_id, generation=generation,
                        stage="champsim", benchmark=benchmark,
                        build_variant=variant, ok=s.ok, ipc=s.ipc, fitness=None,
                        metrics={"l1d_mpki": s.l1d_mpki,
                                  "hints_per_1k": s.hints_per_1k_insts},
                        wall_s=s.wall_s)
                    self.store.record_evaluation(
                        genome_id=genome.genome_id, generation=generation,
                        stage="gem5_o3", benchmark=benchmark,
                        build_variant=variant, ok=s.ok, ipc=s.ipc, fitness=None,
                        metrics=s.stats, wall_s=s.wall_s)

                hint, swpf, base = o3_runs["hint"], o3_runs["swpf"], o3_runs["base"]
                sp_swpf = gem5_nodes.o3_speedup(hint, swpf)
                sp_base = gem5_nodes.o3_speedup(hint, base)
                swpf_m = swpf.stats.get("l1d_misses", 0.0)
                hint_m = hint.stats.get("l1d_misses", 0.0)
                miss_cov = max(0.0, (swpf_m - hint_m) / swpf_m) if swpf_m > 0 else 0.0
                if sp_swpf:
                    result.speedup_vs_swpf[benchmark] = sp_swpf + 0.05 * miss_cov
                    result.o3_speedup_vs_swpf[benchmark] = sp_swpf
                if sp_base:
                    result.speedup_vs_base[benchmark] = sp_base
                result.hints_per_1k[benchmark] = hint.hints_per_1k_insts
                result.o3_hints_per_1k[benchmark] = hint.hints_per_1k_insts
                result.wasted_rate[benchmark] = hint.wasted_prefetch_rate
                if hint.l1d_mpki is not None:
                    result.l1d_mpki[benchmark] = hint.l1d_mpki

            result.ok = bool(result.speedup_vs_swpf)
            return result

        for benchmark in self.benchmarks:
            summaries = {}
            for variant in config.BUILD_VARIANTS:
                s = self.champsim(genome, benchmark, variant,
                                  all_sites[benchmark], builds)
                if s is None or not s.ok:
                    result.failure_stage = "champsim"
                    result.failure_reason = (
                        f"{benchmark}.{variant}: "
                        f"{(s.error if s else 'module build failed')[:300]}")
                    return result
                summaries[variant] = s
                self.store.record_evaluation(
                    genome_id=genome.genome_id, generation=generation,
                    stage="champsim", benchmark=benchmark,
                    build_variant=variant, ok=s.ok, ipc=s.ipc, fitness=None,
                    metrics={"l1d_mpki": s.l1d_mpki,
                             "l1d_accuracy": s.l1d_accuracy,
                             "l1d_coverage": s.l1d_coverage,
                             "hints_per_1k": s.hints_per_1k_insts},
                    wall_s=s.wall_s)

            hint, swpf, base = (summaries["hint"], summaries["swpf"],
                                summaries["base"])
            if swpf.ipc > 0:
                result.speedup_vs_swpf[benchmark] = hint.ipc / swpf.ipc
            if base.ipc > 0:
                result.speedup_vs_base[benchmark] = hint.ipc / base.ipc
            result.hints_per_1k[benchmark] = hint.hints_per_1k_insts
            result.wasted_rate[benchmark] = hint.wasted_prefetch_rate
            if hint.l1d_mpki is not None:
                result.l1d_mpki[benchmark] = hint.l1d_mpki

        result.ok = bool(result.speedup_vs_swpf)
        if not result.ok:
            result.failure_stage = "champsim"
            result.failure_reason = "no usable speedup measurement"
        return result


# ==========================================================================
# Node 6: slow validation and fitness re-anchoring
# ==========================================================================

def validate_in_o3(boot: Bootstrap, store: Store, candidates: list[CandidateResult],
                   benchmarks: list[str], generation: int) -> None:
    """Run the top candidates in gem5 O3 and attach the results in place.

    This is the only measurement in the project that can see issue-queue and
    load-store-queue pressure, so it serves two purposes: it produces the
    numbers that go in the write-up, and it tells the fast fitness function how
    wrong it currently is.
    """
    if boot.gem5_node is None:
        log.info("Node 6 skipped: no gem5 node")
        return

    run_fn = boot.gem5_node.run_gem5
    for cand in candidates:
        genome = cand.genome
        out_root = runner.run_dir_for(generation, genome.genome_id) / "o3"
        for benchmark in benchmarks:
            if benchmark in cand.o3_speedup_vs_swpf:
                log.info("Node 6: %s %s -> %.3fx vs software prefetch "
                         "(%.2f hints/1k) [cached from O3 eval]",
                         genome.genome_id, benchmark,
                         cand.o3_speedup_vs_swpf[benchmark],
                         cand.o3_hints_per_1k.get(benchmark, 0.0))
                continue
            builds = {}
            for variant in ("hint", "swpf"):
                cand_dir = runner.run_dir_for(generation, genome.genome_id)
                elf = cand_dir / f"{benchmark}.{variant}.elf"
                if not elf.exists():
                    b_res = resolve(submit(
                        llvm_nodes.build_benchmark,
                        str(config.BENCH_DIR), benchmark, variant,
                        genome.to_json(search_only=True),
                        boot.plugin_path, str(cand_dir)))
                    if not b_res.success or not elf.exists():
                        log.warning("Node 6: failed to build %s (%s); skipping",
                                    elf, b_res.diagnostics[:200])
                        continue
                outdir = out_root / f"{benchmark}.{variant}"
                res = resolve(submit(
                    run_fn, boot.gem5_binary, str(config.GEM5_CONFIG),
                    str(outdir),
                    workload_name=f"{benchmark}.{variant}",
                    config_args=gem5_nodes.o3_config_args(
                        genome, str(elf), cpu_type="o3",
                        max_insts=0,
                        disable_phq=(variant != "hint")),
                    stats_keys=gem5_nodes.PHQ_STATS_KEYS,
                    timeout_s=config.GEM5_RUN_TIMEOUT_S))
                builds[variant] = gem5_nodes.summarize_o3(res, benchmark, variant)

                store.record_evaluation(
                    genome_id=genome.genome_id, generation=generation,
                    stage="gem5_o3", benchmark=benchmark, build_variant=variant,
                    ok=builds[variant].ok, ipc=builds[variant].ipc,
                    fitness=None, metrics=builds[variant].stats,
                    wall_s=builds[variant].wall_s)

            if "hint" in builds and "swpf" in builds:
                # Re-assert the invariants on the timing model too: a squashed
                # hint leaking an LSQ entry only shows up under speculation.
                ok, why = gem5_nodes.check_structural_invariants(builds["hint"])
                if not ok:
                    log.error("Node 6: %s on %s -- %s",
                              genome.genome_id, benchmark, why)
                    cand.ok = False
                    cand.failure_stage = "gate"
                    cand.failure_reason = why
                    continue
                speedup = gem5_nodes.o3_speedup(builds["hint"], builds["swpf"])
                if speedup:
                    cand.o3_speedup_vs_swpf[benchmark] = speedup
                    cand.o3_hints_per_1k[benchmark] = \
                        builds["hint"].hints_per_1k_insts
                    log.info("Node 6: %s %s -> %.3fx vs software prefetch "
                             "(%.2f hints/1k)", genome.genome_id, benchmark,
                             speedup, builds["hint"].hints_per_1k_insts)


# ==========================================================================
# Reporting
# ==========================================================================

def write_report(store: Store, out_path: Path) -> None:
    """Emit the markdown summary that backs the submission."""
    ok, total = store.repair_success_rate()
    convergence = store.convergence()

    lines = [
        "# HINT.GATHER run report",
        "",
        f"Generated {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Agentic loop efficiency",
        "",
        f"- Autonomous repair attempts: **{total}**",
        f"- Successful repairs: **{ok}**"
        + (f" ({100.0 * ok / total:.0f}%)" if total else ""),
        f"- Generations completed: **{len(convergence)}**",
        "",
        "### Convergence",
        "",
        "| generation | best fitness | mean fitness |",
        "|-----------|--------------|--------------|",
    ]
    for gen, best, mean in convergence:
        lines.append(f"| {gen} | {best:+.4f} | {mean:+.4f} |")

    lines += ["", "## Repairs by node", "",
              "| node | attempts | succeeded |", "|------|----------|-----------|"]
    for row in store.query(
            "SELECT node, COUNT(*) AS n, SUM(succeeded) AS ok "
            "FROM repairs GROUP BY node ORDER BY n DESC"):
        lines.append(f"| {row['node']} | {row['n']} | {row['ok'] or 0} |")

    lines += ["", "## Best candidates (ChampSim fast evaluation)", "",
              "| genome | benchmark | variant | IPC |",
              "|--------|-----------|---------|-----|"]
    for row in store.query(
            "SELECT genome_id, benchmark, build_variant, ipc FROM evaluations "
            "WHERE stage='champsim' AND ok=1 ORDER BY ipc DESC LIMIT 20"):
        lines.append(f"| {row['genome_id']} | {row['benchmark']} | "
                     f"{row['build_variant']} | {row['ipc']:.4f} |")

    lines += ["", "## gem5 O3 validation (the numbers that count)", "",
              "| genome | benchmark | variant | cycles | speedup vs swpf | L1D misses | IPC |",
              "|--------|-----------|---------|--------|-----------------|------------|-----|"]
    import json
    o3_rows = store.query(
        "SELECT genome_id, benchmark, build_variant, ipc, metrics_json "
        "FROM evaluations WHERE stage='gem5_o3' AND ok=1 "
        "ORDER BY genome_id, benchmark, build_variant")
    swpf_cycles_map: dict[tuple[str, str], float] = {}
    parsed_o3 = []
    for row in o3_rows:
        try:
            m = json.loads(row["metrics_json"] or "{}")
        except Exception:
            m = {}
        cyc = float(m.get("cycles") or m.get("num_cycles") or m.get("system.cpu.numCycles") or 0.0)
        misses = int(float(m.get("l1d_misses") or m.get("system.cpu.dcache.demandMisses::total") or 0.0))
        if row["build_variant"] == "swpf" and cyc > 0:
            swpf_cycles_map[(row["genome_id"], row["benchmark"])] = cyc
        parsed_o3.append((row, cyc, misses))
    for row, cyc, misses in parsed_o3:
        ipc = f"{row['ipc']:.4f}" if row["ipc"] is not None else "-"
        cyc_str = f"{int(cyc):,d}" if cyc > 0 else "-"
        miss_str = f"{misses:,d}" if misses > 0 else "-"
        ref_cyc = swpf_cycles_map.get((row["genome_id"], row["benchmark"]), 0.0)
        if not ref_cyc:
            for (_gid, bname), val in swpf_cycles_map.items():
                if bname == row["benchmark"]:
                    ref_cyc = val
                    break
        spd_str = f"{ref_cyc / cyc:.3f}x" if (cyc > 0 and ref_cyc > 0) else "-"
        lines.append(f"| {row['genome_id']} | {row['benchmark']} | "
                     f"{row['build_variant']} | {cyc_str} | {spd_str} | "
                     f"{miss_str} | {ipc} |")

    lines += [
        "",
        "## Caveats",
        "",
        "- ChampSim numbers are memory-system signals only; it does not model",
        "  issue-queue or LSQ pressure. All pipeline claims come from gem5 O3.",
        "- See 'docs/DESIGN.md' section 6 for the full limitations list.",
        "",
    ]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines))
    log.info("report written to %s", out_path)


# ==========================================================================
# Main
# ==========================================================================

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--stage", default="full",
                   choices=["bootstrap", "gate", "fast", "evolve", "full", "report"])
    p.add_argument("--generations", type=int, default=None)
    p.add_argument("--population", type=int, default=None)
    p.add_argument("--benchmarks", default="",
                   help="comma-separated; defaults to HG_FAST_BENCHMARKS")
    p.add_argument("--seed", type=int, default=0xC71A)
    p.add_argument("--local", action="store_true",
                   help="run nodes in-process instead of on a CHIA cluster")
    p.add_argument("--skip-gem5", action="store_true",
                   help="skip the gem5 bootstrap, gate and O3 validation")
    p.add_argument("--skip-champsim", action="store_true")
    p.add_argument("--no-llm", action="store_true",
                   help="disable every agent (random mutation only, no repair)")
    p.add_argument("--verbose", "-v", action="store_true")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.local:
        config.LOCAL_MODE = True
        os.environ["HG_LOCAL"] = "1"
    if args.generations is not None:
        config.GENERATIONS = args.generations
    if args.population is not None:
        config.POPULATION_SIZE = args.population

    runner.setup_logging(args.verbose)
    log.info("HINT.GATHER co-design loop\n%s", config.describe())

    store = Store()
    out_dir = Path(config.RUN_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.stage == "report":
        write_report(store, out_dir / "REPORT.md")
        return 0

    llm = None
    if not args.no_llm:
        try:
            llm = agents.make_llm()
            log.info("agent backend: %s / %s", config.LLM_BACKEND, config.LLM_MODEL)
        except Exception as e:
            log.warning("no LLM backend available (%s); continuing without "
                        "agents -- random mutation only, no self-repair", e)

    benchmarks = select_targets(args)
    boot = Bootstrap(store, llm, skip_gem5=args.skip_gem5,
                     skip_champsim=args.skip_champsim)

    try:
        if not boot.run():
            log.error("bootstrap failed; nothing further can run")
            return 1
        if args.stage == "bootstrap":
            log.info("bootstrap complete")
            return 0

        evaluator = Evaluator(boot, store, benchmarks, llm,
                              run_gate=not args.skip_gem5)

        if args.stage in ("gate", "fast"):
            genome = Genome()
            log.info("single-candidate run: %s", genome.short())
            evaluator.run_gate = (args.stage == "gate") or not args.skip_gem5
            result = evaluator.evaluate(genome, generation=0)
            result.fitness = evolve.compute_fitness(
                result, lambda_value=config.FITNESS_LAMBDA,
                mu_value=config.FITNESS_MU)
            log.info("%s", result.summary())
            write_report(store, out_dir / "REPORT.md")
            return 0 if result.ok else 2

        # -- the evolutionary loop ------------------------------------
        rng = random.Random(args.seed)
        lambda_value = config.FITNESS_LAMBDA
        population = seed_population(config.POPULATION_SIZE)
        history: list[CandidateResult] = []

        for generation in range(config.GENERATIONS):
            gen_start = time.time()
            with phase(f"generation {generation} "
                       f"({len(population)} candidates, lambda={lambda_value:.4f})"):
                results: list[CandidateResult] = []
                for i, genome in enumerate(population):
                    log.info("[gen %d %d/%d] %s", generation, i + 1,
                             len(population), genome.short())
                    try:
                        result = evaluator.evaluate(genome, generation)
                    except Exception as e:  # one bad candidate must not end the run
                        log.exception("candidate %s raised", genome.genome_id)
                        result = CandidateResult(
                            genome=genome, failure_stage="build",
                            failure_reason=f"{type(e).__name__}: {e}")
                    result.fitness = evolve.compute_fitness(
                        result, lambda_value=lambda_value,
                        mu_value=config.FITNESS_MU)
                    log.info("    %s", result.summary())
                    results.append(result)

                # Node 6: periodic re-anchoring of the fast fitness.
                do_anchor = (args.stage == "full"
                             and not args.skip_gem5
                             and (generation + 1) % config.REANCHOR_EVERY == 0)
                if do_anchor:
                    top = evolve.select_elites(results, config.REANCHOR_TOP_N)
                    with phase(f"Node 6: O3 validation of {len(top)} candidate(s)"):
                        validate_in_o3(boot, store, top, benchmarks, generation)
                    lambda_value, why = evolve.recalibrate_lambda(
                        results, lambda_value)
                    log.info("re-anchor: %s", why)
                    for r in results:
                        r.fitness = evolve.compute_fitness(
                            r, lambda_value=lambda_value,
                            mu_value=config.FITNESS_MU)

                best, mean = evolve.generation_stats(results)
                store.record_generation(
                    generation=generation,
                    best_genome=max(results, key=lambda r: r.fitness).genome.genome_id,
                    best_fitness=best, mean_fitness=mean,
                    fitness_lambda=lambda_value,
                    wall_s=time.time() - gen_start)
                log.info("generation %d: best %+.4f, mean %+.4f",
                         generation, best, mean)

                history = results
                if generation + 1 < config.GENERATIONS:
                    population = evolve.next_generation(
                        results, generation=generation + 1, rng=rng, llm=llm,
                        lambda_value=lambda_value)

        # Final O3 validation of the overall winner on the hero benchmarks.
        if args.stage == "full" and not args.skip_gem5 and history:
            winner = evolve.select_elites(history, 1)
            with phase("final O3 validation on hero benchmarks"):
                validate_in_o3(boot, store, winner, list(config.HERO_BENCHMARKS),
                               config.GENERATIONS - 1)
            log.info("winner: %s", winner[0].summary())

        write_report(store, out_dir / "REPORT.md")
        return 0

    finally:
        boot.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
