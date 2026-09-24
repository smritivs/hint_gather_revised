/* ===========================================================================
 * bench/listchase.c -- pure pointer chase over a randomly permuted list.
 *
 * ROLE
 *   The hardest case, and the one that separates the two variants of
 *   docs/DESIGN.md sec 1.2.  There is exactly one load in flight at a time: the
 *   address of the next access is the *value* of the current one, so
 *   memory-level parallelism is zero and no amount of out-of-order execution
 *   helps.
 *
 *   Why it matters for this project:
 *     - The `swpf` baseline is structurally unable to win here.  To software-
 *       prefetch `p->next->next` you must first load `p->next`, which is the
 *       very load you are waiting for.  Expect swpf ~= base; if it wins, the
 *       measurement is wrong.
 *     - The `value` variant of HINT.GATHER is equally stuck, for exactly the
 *       same reason: its rs2 operand is not ready.
 *     - The `chase` variant (HINT.GATHER.C) is the only form that can do
 *       anything at all, because the PHQ issues the dependent load itself and
 *       the core never sees it.  This is the benchmark where the evolutionary
 *       search should discover `variant: "chase"` on its own.
 *
 *   Node layout is one full cache line, so every step is a guaranteed miss
 *   once the list exceeds the LLC and there is no spatial locality to
 *   accidentally exploit.
 *
 *   Contract: docs/DESIGN.md sec 4.4.
 *
 * WORKING SET (default --size 262144 nodes)
 *   nodes: 262144 x 64 B = 16 MiB -- far beyond a 32 KiB L1D / 1 MiB L2, and
 *          beyond a typical 8 MiB LLC as well, which is the point.
 * ===========================================================================
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "common.h"

#define LC_DEFAULT_SIZE (1u << 18) /* nodes */
#define LC_DEFAULT_ITERS 4u        /* full laps of the cycle */

#ifndef LC_LINE_BYTES
#define LC_LINE_BYTES 64
#endif

/* Exactly one cache line per node: next pointer, payload, padding. */
typedef struct hg_node {
  struct hg_node *next;
  uint64_t value;
  uint64_t pad[(LC_LINE_BYTES - 2 * sizeof(uint64_t)) / sizeof(uint64_t)];
} hg_node;

/* --------------------------------------------------------------------------
 * The chase.  `steps` is exact so that the self-check below (we must land
 * back on the head after exactly n steps) is meaningful.
 * ------------------------------------------------------------------------*/
static hg_node *chase_kernel(hg_node *head, uint64_t steps,
                             uint64_t *checksum) {
  hg_node *p = head;
  uint64_t mixed = *checksum;
  uint64_t i;

  for (i = 0; i < steps; ++i) {
#if HG_BUILD_SWPF
    /* This is as good as a software prefetch can get on a pointer chase, and
     * it is deliberately feeble: to look two nodes ahead we must dereference
     * `p->next`, which is the load we are already blocked on.  Documented in
     * bench/README.md as the expected null result. */
    HG_PREFETCH_R(p->next);
#endif
#if HG_BUILD_HINT
    /* Chase form with base = 0 and shift = 0: the PHQ loads the pointer at
     * rs2 (= &p->next) and prefetches the value it finds, i.e. it performs
     * the next hop itself (docs/DESIGN.md sec 1.2 variant 1).  Unlike the array
     * gather benchmarks (gather, bfs, pagerank) whose hints are injected by
     * the LLVM pass on load->GEP->load idioms, listchase has no GEP (it is a
     * pure load->load pointer chase that the pass intentionally skips), so
     * listchase emits the variant-1 base=x0 hint directly here. */
#if defined(__x86_64__)
    __asm__ volatile(".globl __hg_site_0\n__hg_site_0:\n");
#endif
    HINT_GATHER_C((uintptr_t)0, &p->next, 0, HG_HINT_FANOUT,
                  HG_LEVEL_L1D, HG_DROP_YES);
#endif

    mixed = hg_mix(mixed, p->value);
    p = p->next; /* <-- the hint site */
  }

  *checksum = mixed;
  return p;
}

/* Builds a single cycle through all n nodes via a Fisher-Yates permutation --
 * a single cycle (not a set of cycles), which is what makes "n steps returns
 * to the head" a valid structural check. */
static void build_cycle(hg_node *nodes, uint32_t *order, uint32_t n,
                        uint64_t seed) {
  uint64_t state = hg_seed_for(seed, 0x636861736575ull /* "chaseu" */);
  uint32_t i;

  for (i = 0; i < n; ++i) order[i] = i;
  for (i = n; i > 1u; --i) {
    const uint32_t j = (uint32_t)hg_rand_below(&state, i);
    const uint32_t tmp = order[i - 1u];
    order[i - 1u] = order[j];
    order[j] = tmp;
  }

  for (i = 0; i < n; ++i) {
    const uint32_t current = order[i];
    const uint32_t following = order[(i + 1u) % n];
    nodes[current].next = &nodes[following];
    nodes[current].value = 0x9E3779B97F4A7C15ull * (uint64_t)current + seed;
  }
}

int main(int argc, char **argv) {
  const hg_args args =
      hg_parse_args(argc, argv, LC_DEFAULT_ITERS, LC_DEFAULT_SIZE);
  const uint32_t n =
      (args.size > 0x3FFFFFF0ull) ? 0x3FFFFFF0u : (uint32_t)args.size;
  hg_node *nodes;
  uint32_t *order;
  hg_node *head;
  hg_node *end;
  uint64_t checksum = 0;
  hg_time_t t0;

  if (n < 2u) {
    fprintf(stderr, "FATAL: --size must be >= 2 for listchase\n");
    return 2;
  }

  nodes = (hg_node *)hg_alloc((size_t)n * sizeof(hg_node), "nodes[]");
  order = (uint32_t *)hg_alloc((size_t)n * sizeof(uint32_t), "order[]");

  build_cycle(nodes, order, n, args.seed);
  free(order); /* the permutation is baked into the links; drop the scratch */

  hg_banner("listchase", &args, (uint64_t)n * sizeof(hg_node));
  if (!args.quiet) {
    printf("NODE_BYTES=%u\n", (unsigned)sizeof(hg_node));
    fflush(stdout);
  }
  hg_check(sizeof(hg_node) == (size_t)LC_LINE_BYTES,
           "node is not exactly one cache line");

  head = &nodes[0];
  t0 = hg_now();
  hg_roi_begin();
  end = chase_kernel(head, (uint64_t)n * args.iters, &checksum);
  hg_roi_end();
  hg_report_time("roi", t0, hg_now());

  /* Self-check: an exact multiple of the cycle length must land on the head
   * again.  A broken link, a truncated chase, or a compiler that hoisted the
   * loads out of the loop all show up here. */
  hg_check(end == head, "chase did not return to the head");

  free(nodes);
  hg_finish(checksum);
  return 0;
}
