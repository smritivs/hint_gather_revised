/* ===========================================================================
 * bench/gather.c -- the clean signal: sum += A[B[i]] with a shuffled B.
 *
 * ROLE
 *   The simplest possible expression of the idiom HINT.GATHER targets
 *   (docs/DESIGN.md sec 4.1 detection: load -> getelementptr -> load, where the
 *   first load's result feeds the GEP index).  There is no control flow, no
 *   frontier, no convergence -- every miss is an indirect miss, so this is
 *   where the instruction should look best and where a regression is
 *   unambiguous.
 *
 *   B is a *permutation* of 0..N-1, which buys two things:
 *     - the index stream is maximally entropic (the SCEV of &A[B[i]] is not
 *       an AddRecExpr, so the pass will accept the site and the IP-stride
 *       baseline can do nothing with it), and
 *     - a free, strong self-check: one pass must sum every element of A
 *       exactly once, so the plain sum is known in advance.
 *
 *   Contract: docs/DESIGN.md sec 4.4 (--iters/--size, deterministic,
 *   self-checking, one CHECKSUM= line, ROI markers, exit 0).
 *
 * WORKING SET (default --size 1048576)
 *   A: 1 Mi x 8 B =  8 MiB
 *   B: 1 Mi x 4 B =  4 MiB
 *   total          = 12 MiB  -- far beyond a 32 KiB L1D and a 1 MiB L2.
 * ===========================================================================
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "common.h"

#define GATHER_DEFAULT_SIZE (1u << 20) /* elements of A (and of B) */
#define GATHER_DEFAULT_ITERS 4u

/* A is uint64_t, so the instruction's index scale is 1 << 3 (docs/DESIGN.md
 * sec 1.1 funct7[1:0] = SHIFT). */
#define GATHER_ELEM_SHIFT 3

/* --------------------------------------------------------------------------
 * The kernel.  Kept in its own function so that the LLVM pass sees a clean
 * loop and so that the hint site has a stable name in hint_sites.json
 * ("function": "gather_kernel", docs/DESIGN.md sec 4.1).
 * ------------------------------------------------------------------------*/
static uint64_t gather_kernel(const uint64_t *a, const uint32_t *b, uint32_t n,
                              uint64_t *plain_sum_out) {
  uint64_t mixed = 0;
  uint64_t plain = 0;
  uint32_t i;

#if HG_BUILD_HINT && defined(__clang__)
  #pragma clang loop unroll_count(8)
#endif
  for (i = 0; i < n; ++i) {
#if HG_BUILD_SWPF
    /* The honest software-prefetch baseline: to prefetch the gather target we
     * must *load the index first*.  That extra load is a real instruction
     * occupying a real issue-queue slot and a real LSQ entry -- precisely the
     * cost HINT.GATHER claims to avoid (docs/DESIGN.md sec 1.4). */
    if (i + HG_SWPF_DISTANCE < n) {
      HG_PREFETCH_R(&a[b[i + HG_SWPF_DISTANCE]]);
      HG_PREFETCH_R(&b[i + 2 * HG_SWPF_DISTANCE]);
    }
#endif
#if HG_BUILD_HINT && HG_MANUAL_HINTS
    /* Chase form: rs2 is &B[i+d], which is affine and ready at dispatch; the
     * PHQ loads it and prefetches A[B[i+d]] itself (docs/DESIGN.md sec 1.2). */
    if (i + HG_HINT_DISTANCE < n) {
      HG_HINT_SITE_C(a, &b[i + HG_HINT_DISTANCE], GATHER_ELEM_SHIFT);
    }
#endif

    {
      const uint64_t value = a[b[i]]; /* <-- the hint site */
      plain += value;
      mixed = hg_mix(mixed, value);
    }
  }

  *plain_sum_out = plain;
  return mixed;
}

/* Fisher-Yates over 0..n-1 with the shared deterministic RNG. */
static void build_permutation(uint32_t *b, uint32_t n, uint64_t seed) {
  uint64_t state = hg_seed_for(seed, 0x7368756666ull /* "shuff" */);
  uint32_t i;

  for (i = 0; i < n; ++i) b[i] = i;
  for (i = n; i > 1u; --i) {
    const uint32_t j = (uint32_t)hg_rand_below(&state, i);
    const uint32_t tmp = b[i - 1u];
    b[i - 1u] = b[j];
    b[j] = tmp;
  }
}

/* One-off check that B really is a permutation; runs before ROI_BEGIN. */
static int is_permutation(const uint32_t *b, uint32_t n) {
  const size_t words = ((size_t)n + 63u) / 64u;
  uint64_t *seen = (uint64_t *)hg_alloc(words * sizeof(uint64_t), "perm bitmap");
  uint32_t i;
  int ok = 1;

  for (i = 0; i < words; ++i) seen[i] = 0ull;
  for (i = 0; i < n; ++i) {
    const uint32_t v = b[i];
    const uint64_t bit = 1ull << (v & 63u);
    if (v >= n || (seen[v >> 6] & bit) != 0ull) {
      ok = 0;
      break;
    }
    seen[v >> 6] |= bit;
  }
  free(seen);
  return ok;
}

int main(int argc, char **argv) {
  const hg_args args =
      hg_parse_args(argc, argv, GATHER_DEFAULT_ITERS, GATHER_DEFAULT_SIZE);
  const uint32_t n = (args.size > 0xFFFFFFF0ull) ? 0xFFFFFFF0u
                                                 : (uint32_t)args.size;
  uint64_t *a;
  uint32_t *b;
  uint64_t expected_plain = 0;
  uint64_t checksum = 0;
  uint64_t iter;
  hg_time_t t0;
  uint32_t i;

  a = (uint64_t *)hg_alloc((size_t)n * sizeof(uint64_t), "A[]");
  b = (uint32_t *)hg_alloc((size_t)n * sizeof(uint32_t), "B[]");

  /* Values: cheap, deterministic, and distinct enough that a wrong index
   * shows up in the checksum. */
  for (i = 0; i < n; ++i) {
    a[i] = 0x9E3779B97F4A7C15ull * (uint64_t)i + args.seed;
    expected_plain += a[i];
  }
  build_permutation(b, n, args.seed);

  hg_banner("gather", &args,
            (uint64_t)n * sizeof(uint64_t) + (uint64_t)n * sizeof(uint32_t));
#if defined(HG_VERIFY_PERM) && HG_VERIFY_PERM
  hg_check(is_permutation(b, n), "B is not a permutation");
#else
  (void)is_permutation;
#endif

  t0 = hg_now();
  hg_roi_begin();
  for (iter = 0; iter < args.iters; ++iter) {
    uint64_t plain = 0;
    const uint64_t mixed = gather_kernel(a, b, n, &plain);
    /* Self-check: a permutation touches every element of A exactly once. */
    hg_check(plain == expected_plain, "gather sum mismatch");
    checksum = hg_mix(checksum, mixed);
  }
  hg_roi_end();
  hg_report_time("roi", t0, hg_now());

  free(a);
  free(b);
  hg_finish(checksum);
  return 0;
}
