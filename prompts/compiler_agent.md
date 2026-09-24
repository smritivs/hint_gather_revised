# Node 2: compiler agent

You are bringing up the HINT.GATHER LLVM pass in this environment.

- Pass source: `{llvm_dir}`
- LLVM install: `{llvm_install}`
- Normative spec: `{design_doc}` (read section 4.1 in full)

## Task

Make the out-of-tree pass plugin build and behave correctly against the LLVM
version installed here.

1. Determine the installed LLVM version and adjust the source for its API.
2. Build `libHintGather.so`.
3. Run the tests in `llvm/test/` and make them pass. The key test asserts that
   an affine access (`A[i]`) is **rejected** and a genuine gather (`A[B[i]]`) is
   **accepted** -- that discrimination is the point of the pass, so a test that
   passes because everything is accepted is worse than a failing test.

## What the pass must do

Detect `load -> getelementptr -> load` gather idioms, reject any site whose
gathered address ScalarEvolution can prove affine, rank the survivors by
measured stride entropy, and emit `HINT.GATHER` in `custom-0` space via a
`.insn` directive -- no LLVM backend modification, no rebuild of clang.

## Constraints

- Keep the CLI contract exactly: `-hg-genome`, `-hg-report`, `-hg-mode`,
  `-hg-profile`.
- Keep the `hint_sites.json` schema exactly as specified; a downstream node
  parses it and resolves `pc_symbol` against the ELF symbol table.
- Never emit a hint that could fault. By design the instruction cannot -- but do
  not rely on that to justify computing wild addresses where a bounded one is
  just as easy.

Report what you changed and what the tests now show.
