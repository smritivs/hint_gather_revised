"""Nodes 1 and 2: the compiler half of the loop.

* **Node 1 (memory profiler)** builds the out-of-tree LLVM pass plugin, runs it
  in ``analyze`` mode to find gather idioms that ``ScalarEvolution`` cannot
  prove affine, and runs it in ``profile`` mode on a native host build to
  measure per-site stride entropy.
* **Node 2 (compiler agent)** compiles the benchmark suite in three flavours --
  ``base`` (nothing), ``swpf`` (ordinary software prefetch: the honest
  baseline) and ``hint`` (``HINT.GATHER``) -- and reports build diagnostics in a
  form the repair agent can act on.

Everything here is a ``@ChiaFunction`` so it can be scheduled onto a worker
exposing the ``llvm`` resource.  See ``docs/DESIGN.md`` sections 4.1 and 4.4 for
the normative contracts (pass CLI, ``hint_sites.json`` schema, benchmark build
targets).
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from dataclasses import dataclass, field

from chia.base.ChiaFunction import ChiaFunction

import config


# --------------------------------------------------------------------------
# Result types (module level so Ray can pickle them on the worker)
# --------------------------------------------------------------------------

@dataclass
class PassBuildResult:
    """Outcome of building ``libHintGather.so``."""
    success: bool
    plugin_path: str
    returncode: int
    duration_s: float
    stdout_tail: str
    diagnostics: str          # filtered compiler errors, empty on success


@dataclass
class HintSiteReport:
    """Node 1 output: which gather sites survived the SCEV filter."""
    success: bool
    sites: list[dict] = field(default_factory=list)
    raw_json: str = ""
    diagnostics: str = ""

    @property
    def emitted_count(self) -> int:
        return sum(
            1 for s in self.sites
            if s.get("emitted") or (not s.get("scev_affine") and not s.get("skip_reason"))
        )

    @property
    def rejected_affine(self) -> int:
        return sum(1 for s in self.sites if s.get("scev_affine"))


@dataclass
class ProfileReport:
    """Node 1 dynamic output: per-site stride entropy."""
    success: bool
    sites: list[dict] = field(default_factory=list)
    diagnostics: str = ""

    def entropy_for(self, site_id: int) -> float | None:
        for s in self.sites:
            if s.get("site_id") == site_id:
                return s.get("entropy")
        return None


@dataclass
class BenchBuildResult:
    """Node 2 output for one (benchmark, variant) pair."""
    benchmark: str
    build_variant: str            # base | swpf | hint
    success: bool
    elf_path: str
    returncode: int
    duration_s: float
    command: str
    diagnostics: str
    hint_count: int = 0           # HINT.GATHER instructions found in the ELF


# --------------------------------------------------------------------------
# Worker-side helpers
# --------------------------------------------------------------------------

_ERROR_MARKERS = (
    "error:", "fatal error", "undefined reference", "ld: ",
    "Assertion", "cannot find", "No such file",
)


def _run(cmd: list[str] | str, cwd: str | None, timeout_s: int,
         env: dict[str, str] | None = None) -> tuple[int, str, str, bool, float]:
    """Run a command, never raise, always return something printable."""
    start = time.time()
    shell = isinstance(cmd, str)
    try:
        proc = subprocess.run(
            cmd, cwd=cwd, shell=shell, capture_output=True, text=True,
            timeout=timeout_s, env=env,
        )
        return (proc.returncode, proc.stdout, proc.stderr, False,
                time.time() - start)
    except subprocess.TimeoutExpired as e:
        return (-9, e.stdout or "", e.stderr or "", True, time.time() - start)
    except OSError as e:
        return (-1, "", f"failed to spawn {cmd!r}: {e}", False,
                time.time() - start)


def _plugin_env(plugin_path: str) -> dict[str, str]:
    """Return environment for clang pass-plugin invocations."""
    _ = plugin_path
    return dict(os.environ)


def _filter_diagnostics(stdout: str, stderr: str, max_bytes: int = 4000) -> str:
    """Keep only the lines a repair agent can act on, newest last.

    Build logs are mostly noise; handing an LLM 4 MB of ``make`` output wastes
    context and buries the actual error.
    """
    keep: list[str] = []
    lines = (stderr + "\n" + stdout).splitlines()
    for i, line in enumerate(lines):
        if any(marker in line for marker in _ERROR_MARKERS):
            # One line of context above, two below -- enough to see the
            # offending expression without dragging in the whole file.
            lo = max(0, i - 1)
            hi = min(len(lines), i + 3)
            keep.extend(lines[lo:hi])
    if not keep:
        keep = lines[-40:]
    text = "\n".join(keep)
    return text[-max_bytes:]


def _count_hint_instructions(elf_path: str, objdump: str) -> int:
    """Count custom-0 instructions in the ELF.

    This is the cheap, independent confirmation that the pass actually emitted
    something -- the loop should never trust ``hint_sites.json`` alone, because
    a site can be reported as emitted and then dead-code-eliminated.
    """
    if not os.path.exists(elf_path):
        return 0
    rc, out, _, _, _ = _run([objdump, "-d", elf_path], cwd=None, timeout_s=300)
    if rc != 0:
        return 0
    count = 0
    for line in out.splitlines():
        # GNU objdump prints 32-bit words (`0ca6900b`); llvm-objdump prints
        # little-endian byte sequences (`0b 90 a6 0c`).  Handle both.
        parts = line.split(":", 1)
        if len(parts) != 2:
            continue
        body = parts[1].strip()
        tokens = body.split()
        if not tokens:
            continue
        token = tokens[0]
        if len(token) == 8:
            try:
                word = int(token, 16)
            except ValueError:
                continue
            if (word & 0x7F) == 0x0B:
                count += 1
        elif len(token) == 2 and len(tokens) >= 4:
            try:
                b0 = int(tokens[0], 16)
                int(tokens[1], 16)
                int(tokens[2], 16)
                int(tokens[3], 16)
            except ValueError:
                continue
            if (b0 & 0x7F) == 0x0B:
                count += 1
    return count


# --------------------------------------------------------------------------
# Node 1a: build the pass plugin
# --------------------------------------------------------------------------

@ChiaFunction(resources=config.RES_LLVM)
def build_pass_plugin(llvm_dir: str, llvm_cmake_dir: str, build_dir: str,
                      *, timeout_s: int = 1800, jobs: int | None = None,
                      force_reconfigure: bool = False) -> PassBuildResult:
    """Configure and build ``libHintGather.so`` against a prebuilt LLVM.

    We deliberately build a *plugin* rather than patching LLVM: a stock release
    tarball plus ``-fpass-plugin`` gives the same emitted encoding with a
    ~20-second edit/build cycle instead of ~40 minutes.  See DESIGN.md sec 6.2.
    """
    os.makedirs(build_dir, exist_ok=True)
    njobs = jobs or max(1, (os.cpu_count() or 4))

    cache = os.path.join(build_dir, "CMakeCache.txt")
    if force_reconfigure and os.path.exists(cache):
        os.remove(cache)

    total_out, total_err = "", ""
    duration = 0.0

    if not os.path.exists(cache):
        rc, out, err, timed_out, wall = _run(
            ["cmake", "-S", llvm_dir, "-B", build_dir,
             f"-DLLVM_DIR={llvm_cmake_dir}",
             "-DCMAKE_BUILD_TYPE=Release",
             "-GUnix Makefiles"],
            cwd=None, timeout_s=timeout_s,
        )
        total_out, total_err, duration = out, err, wall
        if rc != 0 or timed_out:
            return PassBuildResult(
                success=False, plugin_path="", returncode=rc, duration_s=wall,
                stdout_tail=out[-3000:],
                diagnostics=("TIMEOUT during cmake configure"
                             if timed_out else _filter_diagnostics(out, err)),
            )

    rc, out, err, timed_out, wall = _run(
        ["cmake", "--build", build_dir, "-j", str(njobs)],
        cwd=None, timeout_s=timeout_s,
    )
    total_out += out
    total_err += err
    duration += wall

    plugin = os.path.join(build_dir, "libHintGather.so")
    success = (rc == 0) and not timed_out and os.path.exists(plugin)
    return PassBuildResult(
        success=success,
        plugin_path=plugin if success else "",
        returncode=rc,
        duration_s=duration,
        stdout_tail=total_out[-3000:],
        diagnostics=(
            "TIMEOUT during build" if timed_out
            else "" if success
            else _filter_diagnostics(total_out, total_err)
            or f"build reported success but {plugin} is missing"
        ),
    )


# --------------------------------------------------------------------------
# Node 1b: static analysis (the SCEV filter)
# --------------------------------------------------------------------------

@ChiaFunction(resources=config.RES_LLVM)
def analyze_hint_sites(bench_dir: str, benchmark: str, genome_json: str,
                       plugin_path: str, clang: str, out_dir: str,
                       *, profile_path: str = "", target: str = "",
                       extra_cflags: str = "", timeout_s: int = 600,
                       ) -> HintSiteReport:
    """Run the pass in ``analyze`` mode: find sites, change nothing.

    This is where the intellectual claim lives -- a site only qualifies if
    ``ScalarEvolution`` *cannot* prove the gathered address affine, i.e. the
    existing stride prefetcher provably will not cover it.
    """
    os.makedirs(out_dir, exist_ok=True)
    genome_path = os.path.join(out_dir, "genome.json")
    with open(genome_path, "w") as f:
        f.write(genome_json)
    report_path = os.path.join(out_dir, f"{benchmark}.hint_sites.json")
    src = os.path.join(bench_dir, f"{benchmark}.c")

    if not os.path.exists(src):
        return HintSiteReport(
            success=False,
            diagnostics=f"benchmark source not found: {src}")

    cmd = [clang]
    if target:
        cmd += [f"--target={target}"]
        if "riscv" in target:
            for cand in (
                config.CONDA_PREFIX / "riscv-tools" / "riscv64-unknown-elf",
                config.CONDA_PREFIX / "riscv64-unknown-elf",
            ):
                if cand.is_dir():
                    cmd += [
                        "-march=rv64gc",
                        "-mabi=lp64d",
                        f"--sysroot={cand}",
                        "-isystem",
                        f"{cand}/include",
                        f"--gcc-toolchain={cand.parent}",
                    ]
                    break
    cmd += [
        "-O2", "-std=c99", f"-I{bench_dir}",
        f"-I{os.path.join(config.LLVM_DIR, 'include')}",
        "-Xclang", "-load", "-Xclang", plugin_path,
        f"-fpass-plugin={plugin_path}",
        "-mllvm", f"-hg-genome={genome_path}",
        "-mllvm", f"-hg-report={report_path}",
        "-mllvm", "-hg-mode=analyze",
    ]
    if profile_path:
        cmd += ["-mllvm", f"-hg-profile={profile_path}"]
    if extra_cflags:
        cmd += shlex.split(extra_cflags)
    cmd += ["-c", src, "-o", os.path.join(out_dir, f"{benchmark}.analyze.o")]

    rc, out, err, timed_out, _ = _run(cmd, cwd=bench_dir, timeout_s=timeout_s,
                                      env=_plugin_env(plugin_path))

    if rc != 0 or timed_out:
        return HintSiteReport(
            success=False,
            diagnostics=("TIMEOUT in analyze mode" if timed_out
                         else _filter_diagnostics(out, err)))

    try:
        with open(report_path) as f:
            raw = f.read()
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        return HintSiteReport(
            success=False,
            diagnostics=f"pass did not produce a readable {report_path}: {e}\n"
                        + _filter_diagnostics(out, err))

    return HintSiteReport(
        success=True,
        sites=payload.get("sites", []),
        raw_json=raw,
    )


# --------------------------------------------------------------------------
# Node 1c: dynamic profile (stride entropy)
# --------------------------------------------------------------------------

@ChiaFunction(resources=config.RES_LLVM)
def profile_stride_entropy(bench_dir: str, benchmark: str, genome_json: str,
                           plugin_path: str, clang: str, out_dir: str,
                           *, bench_args: str = "", timeout_s: int = 900,
                           ) -> ProfileReport:
    """Build an instrumented *native* binary and run it to measure entropy.

    Deliberately native (x86), not RISC-V: the hint macro compiles to nothing
    off RISC-V, the access pattern is identical, and a native run takes seconds
    where a simulated run takes minutes.  Entropy is a property of the program's
    address stream, not of the ISA.
    """
    os.makedirs(out_dir, exist_ok=True)
    genome_path = os.path.join(out_dir, "genome.json")
    with open(genome_path, "w") as f:
        f.write(genome_json)

    src = os.path.join(bench_dir, f"{benchmark}.c")
    extra_srcs = []
    if benchmark in ("bfs", "pagerank"):
        extra_srcs.append(os.path.join(bench_dir, "graphgen.c"))
    runtime = os.path.join(config.LLVM_DIR, "runtime", "hgprof_runtime.c")
    exe = os.path.join(out_dir, f"{benchmark}.profile")
    report_path = os.path.join(out_dir, f"{benchmark}.hint_sites.profile.json")
    profile_json = os.path.join(out_dir, "hg_profile.json")

    cmd = [
        clang, "-O2", "-std=c99", f"-I{bench_dir}",
        f"-I{os.path.join(config.LLVM_DIR, 'include')}",
        "-Xclang", "-load", "-Xclang", plugin_path,
        f"-fpass-plugin={plugin_path}",
        "-mllvm", f"-hg-genome={genome_path}",
        "-mllvm", f"-hg-report={report_path}",
        "-mllvm", "-hg-mode=profile",
        src, *extra_srcs, runtime, "-o", exe,
    ]
    rc, out, err, timed_out, _ = _run(cmd, cwd=bench_dir, timeout_s=timeout_s,
                                      env=_plugin_env(plugin_path))
    if rc != 0 or timed_out:
        return ProfileReport(
            success=False,
            diagnostics=("TIMEOUT building profile binary" if timed_out
                         else _filter_diagnostics(out, err)))

    env = dict(os.environ)
    env["HG_PROFILE_OUT"] = profile_json
    run_args = bench_args or "--size 8192 --iters 2"
    rc, out, err, timed_out, _ = _run(
        [exe] + shlex.split(run_args), cwd=out_dir, timeout_s=timeout_s, env=env)
    if timed_out:
        return ProfileReport(success=False, diagnostics="profile run timed out")

    # The runtime writes a file *and* echoes the JSON between markers, so we can
    # still recover the profile from a sandbox with no writable filesystem.
    payload = None
    try:
        with open(profile_json) as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        begin = out.find("HG_PROFILE_BEGIN")
        end = out.find("HG_PROFILE_END")
        if begin != -1 and end > begin:
            blob = out[begin + len("HG_PROFILE_BEGIN"):end].strip()
            try:
                payload = json.loads(blob)
            except json.JSONDecodeError:
                payload = None

    if payload is None:
        return ProfileReport(
            success=False,
            diagnostics="no profile produced (neither file nor stdout markers)\n"
                        + _filter_diagnostics(out, err))

    return ProfileReport(success=True, sites=payload.get("sites", []))


# --------------------------------------------------------------------------
# Node 2: build the benchmark ELFs
# --------------------------------------------------------------------------

@ChiaFunction(resources=config.RES_LLVM)
def build_benchmark(bench_dir: str, benchmark: str, build_variant: str,
                    genome_json: str, plugin_path: str, out_dir: str,
                    *, clang: str = "", target: str = "",
                    objdump: str = "", profile_path: str = "",
                    timeout_s: int = 900, make_jobs: int = 4,
                    ) -> BenchBuildResult:
    """Build one ``(benchmark, variant)`` ELF via the benchmark Makefile.

    Delegating to ``make`` rather than open-coding the compiler invocation keeps
    one definition of how a benchmark is built -- the repair agent edits the
    Makefile or the source, never a command line buried in Python.
    """
    os.makedirs(out_dir, exist_ok=True)
    genome_path = os.path.join(out_dir, "genome.json")
    with open(genome_path, "w") as f:
        f.write(genome_json)

    clang = clang or str(config.CLANG)
    target = target or config.RISCV_TARGET
    if not objdump:
        for cand in (
            config.CONDA_PREFIX / "riscv-tools" / "bin" / "riscv64-unknown-elf-objdump",
            config.CONDA_PREFIX / "bin" / "riscv64-unknown-elf-objdump",
            config.LLVM_INSTALL / "bin" / "llvm-objdump",
        ):
            if cand.is_file():
                objdump = str(cand)
                break
        else:
            objdump = "riscv64-unknown-elf-objdump"

    elf_path = os.path.join(out_dir, f"{benchmark}.{build_variant}.elf")
    make_vars = [
        f"HG_CLANG={clang}",
        f"CONDA_PREFIX={config.CONDA_PREFIX}",
        f"HG_PLUGIN={plugin_path}",
        f"HG_GENOME={genome_path}",
        f"HG_INCLUDE={os.path.join(config.LLVM_DIR, 'include')}",
        f"BUILD_DIR={out_dir}",
    ]
    if profile_path:
        make_vars.append(f"HG_PROFILE={profile_path}")

    cmd = ["make", "-j", str(make_jobs), *make_vars, elf_path]
    rc, out, err, timed_out, wall = _run(cmd, cwd=bench_dir, timeout_s=timeout_s)

    success = (rc == 0) and not timed_out and os.path.exists(elf_path)
    hint_count = 0
    if success and build_variant == "hint":
        hint_count = _count_hint_instructions(elf_path, objdump)
        if hint_count == 0:
            # A "successful" hint build with no hints is a silent failure: the
            # candidate would score identically to the baseline and pollute the
            # search with a meaningless data point.  Fail it loudly instead.
            success = False

    diagnostics = ""
    if timed_out:
        diagnostics = f"TIMEOUT after {wall:.0f}s building {benchmark}.{build_variant}"
    elif rc != 0:
        diagnostics = _filter_diagnostics(out, err)
    elif not os.path.exists(elf_path):
        diagnostics = (f"make succeeded but {elf_path} is missing -- check the "
                       f"BUILD_DIR/target naming in bench/Makefile")
    elif build_variant == "hint" and hint_count == 0:
        diagnostics = ("hint build contains zero custom-0 instructions: the pass "
                       "ran but emitted nothing (check -hg-mode=emit wiring, the "
                       "entropy_threshold, and that the asm is marked "
                       "hasSideEffects so it survives DCE)")

    return BenchBuildResult(
        benchmark=benchmark,
        build_variant=build_variant,
        success=success,
        elf_path=elf_path if success else "",
        returncode=rc,
        duration_s=wall,
        command=" ".join(shlex.quote(c) for c in cmd),
        diagnostics=diagnostics,
        hint_count=hint_count,
    )
