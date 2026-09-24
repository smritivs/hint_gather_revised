# Repair: the HINT.GATHER LLVM pass plugin will not build

Repair attempt {attempt} of {max_attempts}.

You have a bash tool rooted at `{workdir}`. The normative spec is at
`{design_doc}` -- read section 4.1 before changing anything.

## What failed

Building `libHintGather.so` (an out-of-tree LLVM pass plugin) failed with:

```
{error}
```

## What to do

1. Find out which LLVM version is actually installed (`llvm-config --version`,
   and look at the headers under the install prefix). The most common cause of
   this failure by far is an API that moved between LLVM releases.
2. Fix the source under `{workdir}` so it builds against *that* version. Prefer
   `#if LLVM_VERSION_MAJOR >= N` guards over unconditional rewrites, so the
   plugin keeps working across versions.
3. Rebuild and confirm:
   `cmake --build <build dir> -j$(nproc)` and check `libHintGather.so` exists.

## Constraints

- Do not change the pass's behaviour or its CLI contract (`-hg-genome`,
  `-hg-report`, `-hg-mode`, `-hg-profile`). The loop depends on those exact
  option names and on the `hint_sites.json` schema in the spec.
- Do not disable or stub out the ScalarEvolution affine filter to make things
  compile. That filter is the intellectual core of the project: it is what
  makes a hint "spent" only on accesses a stride prefetcher provably cannot
  cover. If it is what is broken, fix it properly.
- Do not delete tests.

When you are done, state in one paragraph what the root cause was and what you
changed.
