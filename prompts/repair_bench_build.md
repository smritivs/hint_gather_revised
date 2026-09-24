# Repair: a benchmark build failed

Repair attempt {attempt} of {max_attempts}.

You have a bash tool rooted at `{workdir}` (the benchmark directory). The
normative spec is at `{design_doc}` -- section 4.4 defines the benchmark
contract.

## Context

- Benchmark: `{benchmark}`
- Build variant: `{build_variant}` (`base` = no prefetching, `swpf` = ordinary
  software prefetch baseline, `hint` = HINT.GATHER)
- Genome in force:

```json
{genome}
```

## What failed

```
{error}
```

## What to do

1. Reproduce with `make` using the same variables the loop passes (`CC`,
   `TARGET`, `HG_PLUGIN`, `HG_GENOME`, `HG_INCLUDE`, `BUILD_DIR`).
2. Fix the Makefile or the benchmark source.

Note one specific failure mode: if the error says the **hint build contains
zero custom-0 instructions**, the build "succeeded" but the pass emitted
nothing. That is treated as a failure on purpose, because such a binary is
indistinguishable from the baseline and would silently poison the search with a
meaningless 1.00x data point. Likely causes, in order of probability:

- the inline asm was dead-code-eliminated (it must be marked as having side
  effects),
- `-fpass-plugin` was not actually passed for this variant,
- `entropy_threshold` in the genome rejected every site (this is legitimate --
  say so rather than forcing an emission),
- the `.insn` directive was rejected by the assembler for this target.

## Constraints

- Every benchmark must keep printing exactly one `CHECKSUM=0x...` line and the
  `ROI_BEGIN`/`ROI_END` markers. The correctness gate compares those checksums;
  changing or removing them defeats the gate.
- The three variants must remain genuinely different. Do not make `swpf` a
  no-op to dodge a build error -- it is the baseline we have to beat, and
  weakening it would invalidate the whole evaluation.

When you are done, state in one paragraph what the root cause was and what you
changed.
