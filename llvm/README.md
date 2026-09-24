<!-- README.md - build and invocation guide for the HINT.GATHER LLVM node.
     Role: everything a human or a repair agent needs to build libHintGather.so
     and drive it. Normative spec: ../docs/DESIGN.md (section 1.1 encoding,
     section 3 genome, section 4.1 this component's contract). -->

# HINT.GATHER -- LLVM node

Out-of-tree LLVM pass plugin that finds gather / pointer-chasing idioms,
profiles their address-delta entropy, and emits the `HINT.GATHER` custom-0
instruction.

Normative spec: [`../docs/DESIGN.md`](../docs/DESIGN.md) -- sec 1.1 (encoding),
sec 3 (genome), sec 4.1 (this component's contract). **DESIGN.md wins over this
file in any disagreement.**

## Contents

| File | Role |
|---|---|
| `include/hint_gather.h` | pure-C encoding + `.insn` emission macros (sec 1.1, sec 4.1); NOP on non-RISC-V |
| `HintGatherPass.cpp` | the pass plugin: detection, SCEV filter, ranking, `analyze`/`profile`/`emit` |
| `runtime/hgprof_runtime.c` | `__hg_profile_access`, delta histogram, `hg_profile.json` (sec 4.1) |
| `runtime/hgprof_runtime.h` | declarations for the harness (`__hg_profile_dump`, `__hg_profile_reset`) |
| `CMakeLists.txt` | the contract build |
| `Makefile` | `llvm-config`-only fallback build |
| `test/` | detection fixture + the CHIA gate script |

> [!IMPORTANT]
> **`StrideEntropyPass.cpp` does not exist -- this is deliberate.** The
> profile-mode instrumentation was folded into `HintGatherPass.cpp`
> (`HintGatherImpl::instrumentSite`). A separate pass would have had to
> duplicate the whole detection pipeline, because `emit`-mode ranking consumes
> exactly the site ids that `profile` mode assigns, and two independent
> detectors would eventually disagree about those ids. The task list allows
> this fold explicitly.

## 1. Get a prebuilt LLVM

No LLVM rebuild is needed -- the instruction is emitted through the assembler's
`.insn` directive (DESIGN.md sec 1.1), and the pass is a plugin.

```bash
export HG_ROOT=$PWD                      # .../hint_gather
mkdir -p "$HG_ROOT/toolchain" && cd "$HG_ROOT/toolchain"

# Any LLVM 17..20 release works. 18.1.8 is the reference.
VER=18.1.8
TARBALL=clang+llvm-${VER}-x86_64-linux-gnu-ubuntu-18.04.tar.xz
curl -L -O https://github.com/llvm/llvm-project/releases/download/llvmorg-${VER}/${TARBALL}
tar xf "${TARBALL}"
export LLVM_HOME="$PWD/clang+llvm-${VER}-x86_64-linux-gnu-ubuntu-18.04"
export PATH="$LLVM_HOME/bin:$PATH"
```

The release tarball's clang already has the RISC-V target enabled, so
`--target=riscv64-unknown-elf` works for `-c`/`-S`. Linking a full ELF also
needs a RISC-V sysroot (newlib); use the toolchain the `bench/` node uses.

## 2. Build the plugin

```bash
cmake -S "$HG_ROOT/llvm" -B "$HG_ROOT/llvm/build" \
      -DLLVM_DIR="$LLVM_HOME/lib/cmake/llvm" \
      -DCMAKE_BUILD_TYPE=Release
cmake --build "$HG_ROOT/llvm/build" -j"$(nproc)"

export HG_BUILD="$HG_ROOT/llvm/build"    # contains libHintGather.so
```

Fallback without CMake:

```bash
make -C "$HG_ROOT/llvm" LLVM_CONFIG="$LLVM_HOME/bin/llvm-config"
```

## 3. Run it -- the three modes

The command line is the one in DESIGN.md sec 4.1:

```bash
clang --target=riscv64-unknown-elf -O2 \
      -fpass-plugin=$HG_BUILD/libHintGather.so \
      -mllvm -hg-genome=<path/to/genome.json> \
      -mllvm -hg-report=<path/to/hint_sites.json> \
      -mllvm -hg-mode=emit            # or 'profile' or 'analyze'
      bench.c -o bench.hint.elf
```

Full option list:

| Option | Default | Meaning |
|---|---|---|
| `-hg-genome=<path>` | *(none)* | genome JSON (DESIGN.md sec 3). Missing keys use the sec 3 defaults. |
| `-hg-report=<path>` | `hint_sites.json` | where to write the site report (sec 4.1). Written in **all** modes. |
| `-hg-mode=<m>` | `analyze` | `analyze` \| `profile` \| `emit`. Unknown values fall back to `analyze`. |
| `-hg-profile=<path>` | *(none)* | `hg_profile.json` from a previous `profile` run; enables entropy ranking. |
| `-hg-verbose` | off | genome echo + per-module candidate counts on stderr. |

### analyze

```bash
clang --target=riscv64-unknown-elf -O2 -c bench.c -o /dev/null \
      -fpass-plugin=$HG_BUILD/libHintGather.so \
      -mllvm -hg-mode=analyze \
      -mllvm -hg-genome=genome.json \
      -mllvm -hg-report=hint_sites.json
```

No IR changes. Produces `hint_sites.json` (sec 4.1 schema).

### profile

```bash
clang --target=riscv64-unknown-elf -O2 \
      -fpass-plugin=$HG_BUILD/libHintGather.so \
      -mllvm -hg-mode=profile \
      -mllvm -hg-genome=genome.json \
      -mllvm -hg-report=hint_sites.json \
      bench.c $HG_ROOT/llvm/runtime/hgprof_runtime.c -o bench.prof.elf

./bench.prof.elf            # or: gem5 ... --binary bench.prof.elf
# -> writes hg_profile.json AND echoes it between
#    HG_PROFILE_BEGIN / HG_PROFILE_END on stdout
```

Set `HG_PROFILE_OUT=/path/hg_profile.json` to redirect the file. Under gem5
SE mode where the filesystem may be unusable, scrape the markers from stdout
instead -- the JSON between them is byte-identical to the file.

### emit

```bash
clang --target=riscv64-unknown-elf -O2 \
      -fpass-plugin=$HG_BUILD/libHintGather.so \
      -mllvm -hg-mode=emit \
      -mllvm -hg-genome=genome.json \
      -mllvm -hg-profile=hg_profile.json \
      -mllvm -hg-report=hint_sites.json \
      bench.c -o bench.hint.elf

nm bench.hint.elf | grep __hg_site_     # PCs for the ChampSim node
```

## 4. What the pass does

Detection (DESIGN.md sec 4.1): in every **innermost** loop, match
`load -> getelementptr -> load` where the first load's result -- possibly through
`sext`/`zext`/`trunc` -- is a GEP index of the second load's pointer.

Filters, in order; the first one that fires sets `skip_reason`:

| `skip_reason` | Meaning |
|---|---|
| `affine_addrec_covered_by_stride_prefetcher` | `SE.getSCEV(gep_ptr)` is an affine `SCEVAddRecExpr` for the loop -- the stride prefetcher already has it. |
| `index_load_outside_loop` | the index load was hoisted; there is no per-iteration index stream. |
| `index_load_not_simple` | volatile/atomic index load. |
| `index_pointer_not_affine_addrec` | `&B[i]` is not an affine AddRec, so `&B[i+d]` is not computable ahead of time (DESIGN.md sec 1.2). |
| `non_constant_addrec_step` | affine, but the step is not a compile-time constant (see *Known restrictions*). |
| `addrec_step_too_wide` | step does not fit in 64 bits. |
| `scalable_element_type` / `unsupported_elem_size` | element size is not 1/2/4/8, so it cannot be encoded in `funct7[1:0]`. |
| `index_load_not_integer` | the index value cannot be widened to XLEN. |
| `base_not_pointer` | malformed IR shape. |
| `trip_count_below_min` | `SE.getSmallConstantTripCount(L) < min_trip_count`. |
| `entropy_below_threshold` | profiled entropy `< entropy_threshold` (only with `-hg-profile`). |
| `exceeds_max_hints_per_loop` | lost the per-loop ranking. |
| `non_riscv_target` | `emit` mode on a non-RISC-V triple; `.insn r 0x0b` would not assemble. |

Unknown trip count is **not** a rejection: the site stays eligible and
`estimated_trip_count` is reported as `-1`.

Ranking: profiled entropy when `-hg-profile` is given, otherwise a
deterministic static heuristic (long/unknown trip counts and >=4-byte elements
score higher). The top `max_hints_per_loop` sites per loop survive.

### `hint_sites.json`

Exactly the sec 4.1 schema, plus one additive key:

```json
{"site_id": 0, "function": "...", "loop_header": "...", "source": "f.c:142",
 "base_value": "...", "index_load": "...[i]", "elem_size": 4,
 "scev_affine": false, "estimated_trip_count": -1, "entropy": -1.0,
 "emitted": true, "pc_symbol": "__hg_site_0", "skip_reason": ""}
```

* `skip_reason` -- **additive**, required so the repair loop can tell *why* a
  site was dropped. Empty string ? the site passed every filter. Consumers of
  the sec 4.1 schema can ignore it.
* `entropy` is `-1.0` when no profile was supplied (parallel to
  `estimated_trip_count: -1`), never a fabricated value.
* `emitted` is true only in `emit` mode, and only for sites where the
  instruction was actually inserted.

### What `emit` inserts

Inside the loop body, immediately before the demand load:

```
%hg.lookahead = getelementptr i8, ptr %B_i, i64 (hint_distance * step_bytes)
call void asm sideeffect ".insn r 0x0b, $2, $3, x0, $0, $1", "r,r,i,i"
     (ptr %A, ptr %hg.lookahead, i32 <funct3>, i32 <funct7>)
call void asm sideeffect ".ifndef __hg_site_0\n__hg_site_0:\n.endif\n", ""()
```

The asm template and operand order are byte-for-byte what
`include/hint_gather.h` expands to. Notes:

* **No bounds guard, on purpose.** Near the end of the array the lookahead
  address runs past the object. That is architecturally harmless *by design*:
  the hint never faults, writes no register and no memory, and removing it
  cannot change the program (DESIGN.md sec 1.3 items 1-2 and 4). A guard would
  cost a branch in the hot loop to protect against something the ISA defines
  as safe; the worst case is one wasted prefetch. The GEP is deliberately
  **not** `inbounds` so the out-of-range address is not poison.
* **`sideeffect` but no `~{memory}`.** `sideeffect` is the only thing keeping
  the hint alive against DCE. A memory clobber is *not* claimed because the
  hint does not order against any memory operation (sec 1.3 item 3), and a bogus
  clobber would block surrounding optimisation.
* **`pc_symbol`.** `__hg_site_<id>` is a local (`t`) symbol landing in the
  symbol table, recoverable with `nm`/`objdump`, guarded by `.ifndef` so block
  duplication cannot cause a duplicate-symbol assembly error.

## 5. Known restrictions

1. **`variant: "value"` passes `B[i]`, not `B[i+d]`.** The architecturally
   intended operand is the *lookahead* index value, but materialising it would
   need an extra architectural load -- precisely the IQ/LSQ cost the proposal
   exists to avoid, and it would contaminate the measurement. So variant 0
   only buys lookahead when the loop is unrolled or software-pipelined. Expect
   the evolutionary search to prefer `"chase"`. This is a measured handicap of
   variant 0, not a bug.
2. **Constant AddRec step only.** `&B[i+d]` is materialised as a byte offset
   from `&B[i]`. A non-constant step would need a full `SCEVExpander`;
   such sites are skipped with `non_constant_addrec_step`.
3. **Innermost loops only** -- otherwise the same site is counted twice.
4. **One report per TU.** The pass is a module pass; compiling several
   translation units with the same `-hg-report` makes the last one win. Give
   each TU its own report path and merge in the CHIA loop, remembering that
   site ids restart at 0 per module.
5. **Site ids are positional.** `analyze`, `profile` and `emit` must run over
   the same source with the same flags for `-hg-profile` to map onto the right
   sites. A mismatch only degrades ranking; it can never produce wrong code.

## 6. Test / gate

```bash
HG_PLUGIN=$HG_BUILD/libHintGather.so bash llvm/test/run_analyze_test.sh
# or, from the build dir:
ctest --test-dir "$HG_BUILD" --output-on-failure
```

The script compiles [`test/gather_test.c`](test/gather_test.c) in analyze mode
and asserts: exactly one accepted site, that it is in `gather_sum`, that no
site is marked `emitted`, and that `genome_hash` is 8 hex characters. It exits
non-zero on any failure, so the CHIA loop can use it directly as a gate.

Cross-check against a RISC-V target as well:

```bash
HG_TARGET_FLAGS="--target=riscv64-unknown-elf" \
HG_PLUGIN=$HG_BUILD/libHintGather.so bash llvm/test/run_analyze_test.sh
```

## 7. Using the macros directly from C

```c
#include "hint_gather.h"

for (long i = 0; i < n; i++) {
  HINT_GATHER_C(a, &b[i + 32], HG_SHIFT_FOR_SIZE(sizeof(int)),
                HG_FANOUT_FIELD(1), HG_LEVEL_L1D, HG_DROP_OK);
  HG_SITE_LABEL(__hg_site_0);
  s += a[b[i]];
}
```

On a non-RISC-V host the macros expand to `(void)0` with every operand cast to
void, so the identical source compiles and runs for functional testing.
