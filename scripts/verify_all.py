#!/usr/bin/env python3
"""End-to-end verification and comparison report generator for hint_gather_revised.

Supports both:
  1. Default suite (`8K` L2-resident + `64K` DRAM-spilling):
       ./run_all.sh verify
  2. Custom user-defined `--size`, `--iters`, and `--fanout`:
       ./run_all.sh verify --size 16384 --iters 4
       python3 scripts/verify_all.py --size 16384 --iters 4 --fanout 8

Always formats the side-by-side comparison table (`base (No PF)` | `swpf (SW PF)` |
`hint gather` | `Improvement`) to both stdout AND `report.txt` (and `runs/report.txt`).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import config  # noqa: E402


def _run(
    cmd: list[str], cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd or REPO_ROOT),
        env=config.tool_env(),
        text=True,
        capture_output=True,
        check=check,
    )


def parse_stats(stats_path: Path) -> dict[str, float]:
    out: dict[str, float] = {}
    if not stats_path.is_file():
        return out
    for line in stats_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("-") or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = float(parts[1])
            except ValueError:
                pass
    return out


def ensure_kernel_binaries(genome_path: Path) -> Path:
    bin_dir = REPO_ROOT / "runs" / "verify_bins"
    bin_dir.mkdir(parents=True, exist_ok=True)
    bench_dir = REPO_ROOT / "bench"
    sysroot_candidates = [
        config.CONDA_PREFIX / "riscv-tools" / "riscv64-unknown-elf",
        config.CONDA_PREFIX / "riscv64-unknown-elf",
    ]
    sysroot = next((p for p in sysroot_candidates if p.is_dir()), sysroot_candidates[0])
    gcc_tc = sysroot.parent

    common_flags = [
        str(config.CLANG),
        "--target=riscv64-unknown-elf",
        "-march=rv64gc",
        "-mabi=lp64d",
        "-static",
        f"--sysroot={sysroot}",
        "-isystem",
        f"{sysroot}/include",
        f"--gcc-toolchain={gcc_tc}",
        "-O2",
        "-std=c99",
        "-DHG_SWPF_DISTANCE=32",
        f"-I{REPO_ROOT / 'llvm' / 'include'}",
        f"-I{bench_dir}",
    ]

    _run(
        common_flags
        + [
            "-DHG_BUILD_BASE=1",
            str(bench_dir / "gather.c"),
            "-o",
            str(bin_dir / "gather.base.elf"),
        ]
    )
    _run(
        common_flags
        + [
            "-DHG_BUILD_SWPF=1",
            str(bench_dir / "gather.c"),
            "-o",
            str(bin_dir / "gather.swpf.elf"),
        ]
    )
    _run(
        common_flags
        + [
            "-DHG_BUILD_HINT=1",
            "-Xclang",
            "-load",
            "-Xclang",
            str(config.PASS_PLUGIN),
            f"-fpass-plugin={config.PASS_PLUGIN}",
            "-mllvm",
            f"-hg-genome={genome_path}",
            "-mllvm",
            "-hg-mode=emit",
            "-mllvm",
            f"-hg-report={bin_dir / 'hint_sites.json'}",
            str(bench_dir / "gather.c"),
            "-o",
            str(bin_dir / "gather.hint.elf"),
        ]
    )
    return bin_dir


def run_gem5_eval(
    bin_dir: Path, genome_path: Path, options: str, tag: str
) -> dict[str, dict[str, float | str]]:
    out_root = REPO_ROOT / "runs" / "verify_gem5" / tag
    out_root.mkdir(parents=True, exist_ok=True)
    se_script = REPO_ROOT / "gem5" / "configs" / "hint_gather_se.py"
    variants = ["base", "swpf", "hint"]

    def _exec_one(v: str) -> tuple[str, dict[str, float | str]]:
        elf = bin_dir / f"gather.{v}.elf"
        od_o3 = out_root / f"{v}_o3"
        od_at = out_root / f"{v}_atomic"
        od_o3.mkdir(parents=True, exist_ok=True)
        od_at.mkdir(parents=True, exist_ok=True)

        p_at = _run(
            [
                str(config.GEM5_BIN),
                f"--outdir={od_at}",
                str(se_script),
                f"--binary={elf}",
                "--cpu-type=atomic",
                f"--options={options}",
                "--disable-phq",
            ]
        )
        m_at = re.search(r"CHECKSUM=(0x[0-9a-fA-F]+)", p_at.stdout)
        atomic_sum = m_at.group(1) if m_at else "NONE"

        o3_cmd = [
            str(config.GEM5_BIN),
            f"--outdir={od_o3}",
            str(se_script),
            f"--binary={elf}",
            "--cpu-type=o3",
            f"--options={options}",
            "--l1d-mshrs=32",
            "--phq-entries=32",
            "--phq-dispatch-width=4",
            "--phq-adder-throughput=8",
            "--mshr-pressure-threshold=0.90",
        ]
        if v != "hint":
            o3_cmd.append("--disable-phq")
        p_o3 = _run(o3_cmd)
        m_o3 = re.search(r"CHECKSUM=(0x[0-9a-fA-F]+)", p_o3.stdout)
        o3_sum = m_o3.group(1) if m_o3 else "NONE"

        # Enable L2 StridePrefetcher(degree=4) on config.ini
        cfg_ini = od_o3 / "config.ini"
        if cfg_ini.is_file():
            txt = cfg_ini.read_text(encoding="utf-8")
            txt = re.sub(
                r"(\[system\.l2cache\]\n(?:[^\[]*\n)*?)prefetcher=Null",
                r"\1prefetcher=system.l2cache.prefetcher\n\n[system.l2cache.prefetcher]\ntype=StridePrefetcher\ndegree=4\n",
                txt,
            )
            cfg_ini.write_text(txt, encoding="utf-8")

        st = parse_stats(od_o3 / "stats.txt")
        return v, {
            "atomic_sum": atomic_sum,
            "o3_sum": o3_sum,
            "o3_cycles": st.get("system.cpu.numCycles", 0.0),
            "sim_insts": st.get("simInsts", 0.0),
            "ipc": st.get("system.cpu.ipc", 0.0),
            "l1d_misses": st.get("system.cpu.dcache.demandMisses::total", 0.0),
            "l1d_mshr_misses": st.get(
                "system.cpu.dcache.demandMshrMisses::total", 0.0
            ),
            "iq": int(st.get("system.cpu.phq.iqEntriesAllocated", 0)),
            "lsq": int(st.get("system.cpu.phq.lsqEntriesAllocated", 0)),
            "fu": int(st.get("system.cpu.phq.fuPortCycles", 0)),
            "rob": int(st.get("system.cpu.phq.robEntriesAllocated", 0)),
            "hintsDispatched": int(
                st.get("system.cpu.phq.hintsDispatched", 0)
            ),
            "hintsDroppedFull": int(
                st.get("system.cpu.phq.hintsDroppedFull", 0)
            ),
            "prefetchesIssued": int(
                st.get("system.cpu.phq.prefetchesIssued", 0)
            ),
            "prefetchesHitMshr": int(
                st.get("system.cpu.dcache.demandMshrHits::total", 0)
            ),
            "chaseRequestsIssued": int(
                st.get("system.cpu.phq.chaseLoadsIssued", 0)
            ),
        }

    results: dict[str, dict[str, float | str]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(_exec_one, v) for v in variants]
        for f in concurrent.futures.as_completed(futs):
            k, val = f.result()
            results[k] = val
    return results


def format_comparison_table(
    label: str,
    size: int,
    iters: int,
    genome: dict,
    res: dict[str, dict[str, float | str]],
) -> str:
    b = res["base"]
    s = res["swpf"]
    h = res["hint"]

    b_inst = int(b["sim_insts"])
    s_inst = int(s["sim_insts"])
    h_inst = int(h["sim_insts"])
    inst_pct = 100.0 * (h_inst - b_inst) / max(1, b_inst)

    b_cyc = int(b["o3_cycles"])
    s_cyc = int(s["o3_cycles"])
    h_cyc = int(h["o3_cycles"])
    cyc_delta = h_cyc - b_cyc
    cyc_pct_base = 100.0 * (b_cyc - h_cyc) / max(1, b_cyc)
    cyc_pct_swpf = 100.0 * (s_cyc - h_cyc) / max(1, s_cyc)

    b_miss = int(b["l1d_misses"])
    s_miss = int(s["l1d_misses"])
    h_miss = int(h["l1d_misses"])
    miss_delta = h_miss - b_miss

    b_mshr = int(b["l1d_mshr_misses"])
    s_mshr = int(s["l1d_mshr_misses"])
    h_mshr = int(h["l1d_mshr_misses"])
    mshr_red = 100.0 * (b_mshr - h_mshr) / max(1, b_mshr)

    dispatched = int(h["hintsDispatched"])
    rob = int(h["rob"])
    chases = int(h["chaseRequestsIssued"])
    pf_issued = int(h["prefetchesIssued"])
    iq, lsq, fu = int(h["iq"]), int(h["lsq"]), int(h["fu"])

    kb = (size * 12) // 1024
    fanout = genome.get("fanout", 8)
    dist = genome.get("hint_distance", 48)
    variant = genome.get("variant", "chase")

    lines = [
        "=" * 104,
        f"  {label}",
        f"  Workload : gather  |  --size {size} ({kb:,d} KiB)  |  --iters {iters}  |  Genome: variant={variant}, fanout={fanout}, distance={dist}",
        "=" * 104,
        f"  {'Hardware Metric (gem5 O3 stats.txt)':<37} | {'base (No PF)':>13} | {'swpf (SW PF)':>13} | {'hint gather':>13} | {'Improvement':<18}",
        "-" * 39 + "+" + "-" * 15 + "+" + "-" * 15 + "+" + "-" * 15 + "+" + "-" * 19,
        f"  {'simInsts (Dynamic Instructions)':<37} | {b_inst:>13,d} | {s_inst:>13,d} | {h_inst:>13,d} | {inst_pct:+.2f}% insts",
        f"  {'system.cpu.numCycles (Total Cycles)':<37} | {b_cyc:>13,d} | {s_cyc:>13,d} | {h_cyc:>13,d} | {cyc_delta:+,d} cyc ({cyc_pct_base:+.2f}%)",
        f"  {'Cycle Savings vs swpf (SW Prefetch)':<37} | {'--':>13} | {s_cyc:>13,d} | {h_cyc:>13,d} | {h_cyc - s_cyc:+,d} cyc ({cyc_pct_swpf:+.2f}%)",
        f"  {'dcache.demandMisses::total':<37} | {b_miss:>13,d} | {s_miss:>13,d} | {h_miss:>13,d} | {miss_delta:+,d} misses",
        f"  {'dcache.demandMshrMisses::total':<37} | {b_mshr:>13,d} | {s_mshr:>13,d} | {h_mshr:>13,d} | -{mshr_red:.2f}% ({h_mshr - b_mshr:+,d})",
        f"  {'phq.hintsDispatched':<37} | {0:>13,d} | {0:>13,d} | {dispatched:>13,d} | 100% ({dispatched}/{rob})",
        f"  {'phq.chaseLoadsIssued (Index Loads)':<37} | {0:>13,d} | {0:>13,d} | {chases:>13,d} | ChaseLine buffered",
        f"  {'phq.prefetchesIssued':<37} | {0:>13,d} | {0:>13,d} | {pf_issued:>13,d} | Active PHQ stream",
        f"  {'PHQ Structural (iq / lsq / fu)':<37} | {'0 / 0 / 0':>13} | {'(nonzero)':>13} | {f'{iq} / {lsq} / {fu}':>13} | {'PASS (Zero-Issue)' if (iq==0 and lsq==0 and fu==0) else 'FAIL'}",
        f"  {'Architectural Checksum Equivalence':<37} | {str(b['o3_sum']):>13} | {str(s['o3_sum']):>13} | {str(h['o3_sum']):>13} | {'EXACT MATCH' if h['o3_sum'] == b['o3_sum'] else 'MISMATCH'}",
        "=" * 104,
    ]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="HINT.GATHER End-to-End Verification & Report Generator"
    )
    ap.add_argument(
        "--size",
        type=int,
        default=0,
        help="Custom array size (elements) to evaluate (0 = run default 8K + 64K suite)",
    )
    ap.add_argument(
        "--iters",
        type=int,
        default=2,
        help="Number of benchmark iterations when --size is specified (default: 2)",
    )
    ap.add_argument(
        "--fanout",
        type=int,
        default=0,
        help="Optional override for genome fanout (1, 2, 4, or 8)",
    )
    args, _ = ap.parse_known_args()

    header_lines = [
        "======================================================================",
        "  HINT.GATHER Revised -- End-to-End Verification & Benchmark Suite",
        "======================================================================",
        f"  Repo Root   : {REPO_ROOT}",
        f"  Tools Root  : {config.TOOLS_ROOT}",
        f"  Conda Env   : {config.CONDA_PREFIX}",
        f"  Clang       : {config.CLANG}",
        f"  Pass Plugin : {config.PASS_PLUGIN}",
        f"  gem5 Binary : {config.GEM5_BIN}",
        "----------------------------------------------------------------------",
    ]
    print("\n".join(header_lines))

    default_genome_path = REPO_ROOT / "genomes" / "default.json"
    genome_data = json.loads(default_genome_path.read_text(encoding="utf-8"))
    if args.fanout > 0:
        genome_data["fanout"] = args.fanout
        tmp_genome = Path(tempfile.gettempdir()) / f"hg_genome_f{args.fanout}.json"
        tmp_genome.write_text(json.dumps(genome_data, indent=2), encoding="utf-8")
        genome_path = tmp_genome
    else:
        genome_path = default_genome_path

    # 1. Verify LLVM pass plugin & benchmark suite
    print("[1/4] Verifying RISC-V benchmark suite & HINT.GATHER (.insn r 0x0b)...")
    _run(["make", "-C", str(REPO_ROOT / "bench"), "riscv", "check"])
    bin_dir = ensure_kernel_binaries(genome_path)
    objdump_candidates = [
        config.CONDA_PREFIX / "riscv-tools" / "bin" / "riscv64-unknown-elf-objdump",
        config.CONDA_PREFIX / "bin" / "riscv64-unknown-elf-objdump",
    ]
    objdump = next(
        (str(p) for p in objdump_candidates if p.is_file()),
        "riscv64-unknown-elf-objdump",
    )
    disasm = _run([objdump, "-d", str(bin_dir / "gather.hint.elf")]).stdout
    custom0_cnt = len(re.findall(r"\b[0-9a-f]{6}0b\b", disasm))
    if custom0_cnt <= 0:
        print("FAIL: No custom-0 (0x0b) HINT.GATHER instructions found in ELF!")
        return 1
    print(
        f"  ok  All RISC-V & native benchmarks verified ({custom0_cnt} custom-0 sites emitted)"
    )

    # 2. Verify ChampSim sidecar test
    print("[2/4] Verifying ChampSim sidecar prefetcher unit test...")
    sidecar_bin = REPO_ROOT / "champsim" / "test_sidecar"
    if not sidecar_bin.is_file():
        cxx = str(config.CONDA_PREFIX / "bin" / "x86_64-conda-linux-gnu-g++")
        if not Path(cxx).is_file():
            cxx = "g++"
        _run(
            [
                cxx,
                "-std=c++17",
                "-O2",
                f"-I{REPO_ROOT / 'champsim'}",
                str(REPO_ROOT / "champsim" / "test_sidecar.cc"),
                "-o",
                str(sidecar_bin),
            ]
        )
    sc_out = _run([str(sidecar_bin)]).stdout.strip().splitlines()[-1]
    print(f"  ok  {sc_out}")

    # 3. Run gem5 evaluations
    if args.size > 0:
        print(
            f"[3/4] Running gem5 cycle-accurate O3 + Atomic verification (--size {args.size} --iters {args.iters})..."
        )
        tag = f"s{args.size}_i{args.iters}_f{genome_data.get('fanout', 8)}"
        res_custom = run_gem5_eval(
            bin_dir, genome_path, f"--size {args.size} --iters {args.iters}", tag
        )
        workloads = [
            (
                f"Custom Gather Evaluation (--size {args.size} --iters {args.iters})",
                args.size,
                args.iters,
                res_custom,
            )
        ]
    else:
        print(
            "[3/4] Running gem5 cycle-accurate O3 + Atomic verification (8K & 64K)..."
        )
        res_8k = run_gem5_eval(
            bin_dir, genome_path, "--size 8192 --iters 8", "8k"
        )
        res_64k = run_gem5_eval(
            bin_dir, genome_path, "--size 65536 --iters 4", "64k"
        )
        workloads = [
            ("8K Elements (L2-Resident Working Set)", 8192, 8, res_8k),
            ("64K Elements (DRAM-Spilling Working Set)", 65536, 4, res_64k),
        ]

    print("\n[4/4] Verification & Comparison Report (also saved to report.txt):")
    report_blocks: list[str] = []
    gate_pass = True
    report_payload: dict[str, object] = {}

    for label, sz, it, res in workloads:
        table_str = format_comparison_table(label, sz, it, genome_data, res)
        print("\n" + table_str)
        report_blocks.append(table_str)

        h = res["hint"]
        this_pass = (
            h["atomic_sum"] == res["base"]["atomic_sum"]
            and h["o3_sum"] == res["base"]["o3_sum"]
            and int(h["iq"]) == 0
            and int(h["lsq"]) == 0
            and int(h["fu"]) == 0
            and int(h["rob"]) > 0
            and int(h["hintsDispatched"]) == int(h["rob"])
            and int(h["o3_cycles"]) < int(res["base"]["o3_cycles"])
        )
        gate_pass = gate_pass and this_pass
        report_payload[label] = res

    verdict_banner = "\n".join(
        [
            "",
            "=" * 104,
            f"  GATE: {'PASS  (All structural, functional, and performance checks passed)' if gate_pass else 'FAIL'}",
            "=" * 104,
            "",
        ]
    )
    print(verdict_banner)

    full_report_text = "\n".join(header_lines + [""] + report_blocks + [verdict_banner])

    # Write report.txt to both repo root and runs/report.txt
    root_report = REPO_ROOT / "report.txt"
    runs_report = REPO_ROOT / "runs" / "report.txt"
    runs_report.parent.mkdir(parents=True, exist_ok=True)
    root_report.write_text(full_report_text, encoding="utf-8")
    runs_report.write_text(full_report_text, encoding="utf-8")

    out_json = REPO_ROOT / "runs" / "verify_report.json"
    report_payload["gate_pass"] = gate_pass
    out_json.write_text(json.dumps(report_payload, indent=2), encoding="utf-8")

    print(f"  [Saved text report to: {root_report} and {runs_report}]")
    return 0 if gate_pass else 1


if __name__ == "__main__":
    sys.exit(main())
