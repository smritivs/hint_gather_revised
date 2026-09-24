# Producing ChampSim traces for the HINT.GATHER benchmarks

Role: how to get from ‘bench/*.elf‘ to something ‘ChampSim‘ can consume in
CHIA Node 4. Normative spec: [‘../../docs/DESIGN.md‘](../../docs/DESIGN.md)
(sec 4.3 ChampSim contract, sec 4.4 benchmark contract, sec 6 limitations).

> **Recommendation for the hackathon: the x86 Pin tracer.** Trace the
> **‘.base.elf‘ host build** of each benchmark, and let
> ‘hint_gather_prefetcher.h‘ model the hint. Everything else is a research
> project in its own right and will not finish in time.

## Why we trace the *base* build, not the hint build

‘HINT.GATHER‘ lives in RISC-V custom-0 space (‘docs/DESIGN.md‘ sec 1.1). An x86
trace cannot contain it, and even a RISC-V trace would carry it only as an
unknown opcode. That is fine, and is exactly how the model is designed: the
ChampSim module is driven by the **hint-site PC table** from the compiler, not
by an instruction in the trace. Tracing the base build gives the clean,
un-perturbed demand stream; the module then acts at the sites the pass chose.

Two consequences, both of which must be respected or the model will silently
see zero hints:

1. **The PCs in ‘hint_sites.json‘ must come from the binary that was traced.**
   Run the pass in ‘analyze‘ mode against the **host/x86** build to emit the
   x86 ‘pc_symbol‘s, then resolve them with ‘nm‘/‘objdump‘ against the same
   ‘.base.elf‘ that produced the trace. Do not mix RISC-V PCs into an x86
   trace run.
2. **The ‘swpf‘ build must be traced separately.** It is the honest baseline of
   the fitness function (‘docs/DESIGN.md‘ sec 5.1), and its software prefetches
   are real instructions that appear in the trace, so its trace differs from
   the base trace.

So, per benchmark, produce two traces:

| Trace | From | Used for |
|---|---|---|
| ‘$HG_TRACE_DIR/<b>.champsimtrace.xz‘ | ‘bench/build/x86/<b>.base‘ | ‘hg_none‘, ‘hg_stride‘, and the HINT.GATHER module |
| ‘$HG_TRACE_DIR/<b>.swpf.champsimtrace.xz‘ | ‘bench/build/x86/<b>.swpf‘ | the software-prefetch baseline IPC (‘gen_traces.sh --with-swpf
     ‘) |

Note the native binaries have **no ‘.elf‘ suffix** -- ‘make -C bench x86‘
produces ‘build/x86/<bench>.<variant>‘, which is what
‘scripts/gen_traces.sh‘ and ‘champsim_nodes.resolve_hint_pcs‘ expect.

Paths come from ‘scripts/setup_env.sh‘ and are all overridable:
‘HG_CHAMPSIM_ROOT‘ (‘$HG_TOOLS_ROOT/ChampSim‘), ‘HG_TRACE_DIR‘
(‘$HG_TOOLS_ROOT/traces‘), ‘PIN_ROOT‘ (‘$HG_TOOLS_ROOT/pin‘).

## Option A (recommended): ChampSim’s Intel Pin tracer, x86

ChampSim ships ‘tracer/pin/champsim_tracer.cpp‘.

‘‘‘bash
# 1. Build the tracer against a Pin kit (Pin 3.x).
cd $HG_CHAMPSIM_ROOT/tracer/pin
make PIN_ROOT=$PIN_ROOT

# 2. Trace a region of the benchmark.
$PIN_ROOT/pin -t obj-intel64/champsim_tracer.so \
    -o $HG_TRACE_DIR/gather.champsimtrace \
    -s 20000000 \
    -t 100000000 \
    -- $HG_BENCH_DIR/build/x86/gather.base --size 1048576 --iters 4

# 3. Compress (ChampSim reads .xz / .gz directly).
xz -T0 -9 $HG_TRACE_DIR/gather.champsimtrace
‘‘‘

* ‘-s‘ (skip) must be large enough to skip *initialisation* -- array
  allocation, the seeded shuffle, CSR construction -- so that the traced window
  is steady-state kernel. Use the ‘ROI_BEGIN‘ marker to calibrate: run once
  under ‘pin -t .../inscount‘ (or just ‘perf stat -e instructions‘) and note how
  many instructions precede ‘ROI_BEGIN‘. ‘bench/README.md‘ gives the
  approximate init cost per benchmark (~20 M for ‘gather‘, ~36 M for ‘bfs‘ and
  ‘pagerank‘, ~10 M for ‘listchase‘).
* ‘-t‘ (trace length) = 100M instructions is the working point in
  ‘docs/DESIGN.md‘; ‘bench/README.md‘ gives per-benchmark sizes chosen so that
  100M instructions comfortably covers steady state.
* Trace the binary **without** ‘HG_ENABLE_TIMING‘ noise if you can
  (‘make x86 CFLAGS=’-O2 -std=c99 -DHG_ENABLE_TIMING=0’‘); ‘clock()‘ syscalls
  add nothing but jitter.
* ‘listchase‘ at the default size executes only ~15 M instructions in total.




  Either raise ‘--iters‘ until the ROI exceeds the window, or trace the whole
  run and accept the shorter trace -- do not pad it with initialisation.

### Trace size

The ChampSim trace record (‘input_instr‘) is **64 bytes per instruction**:

‘‘‘
ip 8 B | is_branch 1 | branch_taken 1 | dest_regs 2 | src_regs 4
       | dest_mem 2x8 | src_mem 4x8                                = 64 B
‘‘‘

| Window | Raw | After ‘xz -9‘ (typical) |
|---|---|---|
| 1 M instructions | 64 MB | 1-5 MB |
| 10 M instructions | 640 MB | 10-50 MB |
| **100 M instructions** | **~6.4 GB** | **~150-600 MB** |

Plan disk accordingly: eight traces (4 benchmarks x {base, swpf}) at 100M is
~50 GB raw. Pipe straight into ‘xz‘ and never keep the raw file:

‘‘‘bash
$PIN_ROOT/pin -t obj-intel64/champsim_tracer.so -o /dev/stdout \
    -s 200000000 -t 100000000 -- ./gather.base.elf | xz -T0 -9 > gather.base.champsim.xz
‘‘‘

(Check your tracer build actually honours ‘/dev/stdout‘; older versions do not,
in which case trace to a scratch disk and compress afterwards.)

## Option B: RISC-V traces

Attractive because the RISC-V build is the one gem5 runs, so the two
simulators would see the *same* binary. Not recommended for the hackathon:

* ChampSim’s shipped tracer is x86/Pin only. RISC-V requires either a
  Spike/QEMU plugin or a gem5 ‘--debug-flags=ExecAll‘ post-processor, both of
  which must be written and validated.
* gem5 -> ChampSim converters exist but are slow (gem5 is ~100-200 KIPS, so
  100M instructions is hours per benchmark per variant, versus minutes under
  Pin).
* The custom-0 instruction would still be opaque to ChampSim, so it buys
  nothing for the model.

If you do go this way: emit one 64-byte ‘input_instr‘ per committed
instruction, treat ‘HINT.GATHER‘ as a NOP with no register or memory operands
(that is its architectural semantics -- ‘docs/DESIGN.md‘ sec 1.3), and validate by
checking that ChampSim’s reported instruction count and branch statistics
match gem5’s for the same window.

## Sanity checks before trusting a trace

1. ChampSim’s own summary reports roughly the instruction count you asked for.
2. ‘hg_none‘ and ‘hg_stride‘ produce **different** L1D MPKI on ‘gather‘
   (if not, the stride prefetcher is not being exercised -- your window is
   probably still in initialisation).
3. The HINT.GATHER module reports ‘hg_hints_seen > 0‘. Zero means the PC table
   does not match the traced binary -- re-resolve ‘pc_symbol‘ (see above).
4. ‘hg_mshr_signal_missing‘ is 0. Non-zero means this ChampSim exposes no MSHR
   occupancy and pressure is being approximated; say so in the write-up.
