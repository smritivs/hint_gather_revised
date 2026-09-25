# llvm/

Out-of-tree LLVM pass plugin (`libHintGather.so`) that finds `A[B[i]]` gather
loops, filters out affine accesses with ScalarEvolution, and emits the
`HINT.GATHER` instruction. Spec: [DESIGN.md §4.1](../docs/DESIGN.md).

## Layout

| Path | Description |
|---|---|
| `HintGatherPass.cpp` | The pass: detection, SCEV filter, ranking, `analyze` / `profile` / `emit` modes |
| `include/hint_gather.h` | C macros that emit the instruction via `.insn r 0x0b` (NOP on non-RISC-V) |
| `runtime/hgprof_runtime.c` | Profiling runtime; writes `hg_profile.json` |
| `CMakeLists.txt`, `Makefile` | CMake build and an `llvm-config` fallback |
| `test/` | Detection test used as a gate by the CHIA loop |

## Build

Requires a prebuilt LLVM 17–20 (`./run_all.sh setup` installs 17.0.6 into `~/hg-tools`).

```bash
cmake -S llvm -B llvm/build -DLLVM_DIR=$LLVM_HOME/lib/cmake/llvm -DCMAKE_BUILD_TYPE=Release
cmake --build llvm/build -j$(nproc)
# or: ./run_all.sh llvm
```

## Usage

```bash
clang --target=riscv64-unknown-elf -O2 \
      -fpass-plugin=llvm/build/libHintGather.so \
      -mllvm -hg-mode=emit \
      -mllvm -hg-genome=genomes/default.json \
      -mllvm -hg-report=hint_sites.json \
      bench.c -o bench.hint.elf
```

| Option | Default | Description |
|---|---|---|
| `-hg-mode` | `analyze` | `analyze` (report only), `profile` (instrument), or `emit` (insert hints) |
| `-hg-genome` | — | Genome JSON; missing keys use DESIGN.md §3 defaults |
| `-hg-report` | `hint_sites.json` | Per-site report, written in every mode |
| `-hg-profile` | — | `hg_profile.json` from a `profile` run; enables entropy ranking |
| `-hg-verbose` | off | Print candidate counts to stderr |

Every rejected site gets a `skip_reason` in `hint_sites.json`, which is the
first place to look when the pass emits fewer hints than expected.

## Test

```bash
HG_PLUGIN=llvm/build/libHintGather.so bash llvm/test/run_analyze_test.sh
```

Compiles `test/gather_test.c` and checks that exactly one gather site is
accepted and the affine access is rejected. Exits non-zero on failure.

## Known limitations

- `variant=value` passes the current index `B[i]` rather than `B[i+d]`, so
  it only gets lookahead on unrolled loops. `chase` does not have this issue.
- Only innermost loops with a constant AddRec step are handled.
- Site IDs are positional, so `profile` and `emit` must run on the same source
  with the same flags.
