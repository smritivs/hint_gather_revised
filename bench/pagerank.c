/* ===========================================================================
 * bench/pagerank.c -- pull-based PageRank over the same CSR as bench/bfs.c.
 *
 * ROLE
 *   The steady-state counterpart to BFS.  Where BFS's frontier collapses and
 *   explodes, PageRank streams the *entire* edge array every iteration with a
 *   completely regular outer loop and a completely irregular inner gather
 *   (`contrib[edges[e]]`).  That makes it the best benchmark for:
 *     - hint_distance tuning (the trip count is long and stable, so a large
 *       lookahead is actually realisable);
 *     - separating the two streams -- `edges[]` is sequential and the IP-
 *       stride baseline should catch it, `contrib[]` is not and it should not.
 *
 *   Fixed iteration count, no convergence test: the work per run must not
 *   depend on floating-point rounding, or the correctness gate in
 *   docs/DESIGN.md sec 4.4 would compare different amounts of work.
 *
 *   ARITHMETIC IS INTEGER FIXED-POINT (Q16), deliberately:
 *     - no libm, as required by the gem5 SE constraints;
 *     - bit-exact across x86 and RISC-V, and immune to the compiler
 *       reassociating float adds differently in the base and hint builds --
 *       which would otherwise make the base-vs-hint checksum comparison fail
 *       for a reason that has nothing to do with the hint.
 *
 *   Contract: docs/DESIGN.md sec 4.4.
 *
 * WORKING SET (default --size 262144 vertices, average degree 8)
 *   edges     ~2.1 Mi x 4 B = ~8.0 MiB
 *   offsets   262145  x 4 B =  1.0 MiB
 *   score + next    x 8 B   =  4.0 MiB
 *   contrib         x 8 B   =  2.0 MiB
 *   outdeg          x 4 B   =  1.0 MiB
 *   total                  ~= 16 MiB  -- far beyond a 32 KiB L1D / 1 MiB L2.
 * ===========================================================================
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "common.h"
#include "graphgen.h"

#define PR_DEFAULT_SIZE (1u << 18) /* vertices */
#define PR_DEFAULT_ITERS 4u        /* PageRank sweeps */

#ifndef PR_AVG_DEGREE
#define PR_AVG_DEGREE 8u
#endif

/* Q16 fixed point: 1.0 == PR_ONE. */
#define PR_ONE 65536ull
/* damping = 0.85, so base = (1 - 0.85) = 0.15 of a unit of rank. */
#define PR_DAMPING_NUM 85ull
#define PR_DAMPING_DEN 100ull
#define PR_BASE ((PR_ONE * (PR_DAMPING_DEN - PR_DAMPING_NUM)) / PR_DAMPING_DEN)

/* contrib[] is uint64_t, so the index scale is 1 << 3 (docs/DESIGN.md sec 1.1). */
#define PR_ELEM_SHIFT 3

/* --------------------------------------------------------------------------
 * One pull sweep: for each u, sum the contributions of its neighbours.
 *
 * The graph is generated without edge direction, so `edges[]` is read as the
 * in-neighbour list.  That is the standard simplification in prefetching
 * studies and it preserves the only thing being measured here -- the access
 * pattern.
 * ------------------------------------------------------------------------*/
static void pagerank_sweep(const hg_csr *graph, const uint64_t *contrib,
                           uint64_t *next_score) {
  const uint32_t *offsets = graph->offsets;
  const uint32_t *edges = graph->edges;
  const uint32_t num_vertices = graph->num_vertices;
  const uint32_t num_edges = graph->num_edges;
  uint32_t u;

  for (u = 0; u < num_vertices; ++u) {
    const uint32_t begin = offsets[u];
    const uint32_t end = offsets[u + 1u];
    uint64_t sum = 0;
    uint32_t e;

#if HG_BUILD_HINT && defined(__clang__)
  #pragma clang loop unroll_count(8)
#endif
    for (e = begin; e < end; ++e) {
#if HG_BUILD_SWPF
      /* The honest baseline: load the index early, then prefetch its target.
       * Both are ordinary instructions (docs/DESIGN.md sec 1.4). */
      if (e + HG_SWPF_DISTANCE < num_edges) {
        HG_PREFETCH_R(&contrib[edges[e + HG_SWPF_DISTANCE]]);
        HG_PREFETCH_R(&edges[e + 2 * HG_SWPF_DISTANCE]);
      }
#endif
#if HG_BUILD_HINT && HG_MANUAL_HINTS
      if (e + HG_HINT_DISTANCE < num_edges) {
        HG_HINT_SITE_C(contrib, &edges[e + HG_HINT_DISTANCE], PR_ELEM_SHIFT);
      }
#endif

      sum += contrib[edges[e]]; /* <-- the hint site */
    }

    next_score[u] = PR_BASE + (PR_DAMPING_NUM * sum) / PR_DAMPING_DEN;
  }
  (void)num_edges; /* unused in the base build */
}

int main(int argc, char **argv) {
  const hg_args args =
      hg_parse_args(argc, argv, PR_DEFAULT_ITERS, PR_DEFAULT_SIZE);
  const uint32_t num_vertices =
      (args.size > 0x7FFFFFF0ull) ? 0x7FFFFFF0u : (uint32_t)args.size;
  hg_csr graph;
  uint64_t *score;
  uint64_t *next_score;
  uint64_t *contrib;
  uint32_t *outdeg;
  uint64_t checksum = 0;
  uint64_t iter;
  hg_time_t t0;
  uint32_t u;

  hg_csr_build(&graph, num_vertices, PR_AVG_DEGREE, args.seed);

  score = (uint64_t *)hg_alloc((size_t)graph.num_vertices * sizeof(uint64_t),
                               "score[]");
  next_score = (uint64_t *)hg_alloc(
      (size_t)graph.num_vertices * sizeof(uint64_t), "next_score[]");
  contrib = (uint64_t *)hg_alloc((size_t)graph.num_vertices * sizeof(uint64_t),
                                 "contrib[]");
  outdeg = (uint32_t *)hg_alloc((size_t)graph.num_vertices * sizeof(uint32_t),
                                "outdeg[]");

  for (u = 0; u < graph.num_vertices; ++u) {
    const uint32_t degree = hg_csr_degree(&graph, u);
    score[u] = PR_ONE;
    outdeg[u] = (degree != 0u) ? degree : 1u;
  }

  hg_banner("pagerank", &args,
            hg_csr_bytes(&graph) +
                3ull * (uint64_t)graph.num_vertices * sizeof(uint64_t) +
                (uint64_t)graph.num_vertices * sizeof(uint32_t));
  if (!args.quiet) {
    printf("GRAPH_VERTICES=%u GRAPH_EDGES=%u AVG_DEGREE=%u\n",
           graph.num_vertices, graph.num_edges, (unsigned)PR_AVG_DEGREE);
    fflush(stdout);
  }
  hg_check(hg_csr_validate(&graph), "generated CSR is malformed");

  t0 = hg_now();
  hg_roi_begin();
  for (iter = 0; iter < args.iters; ++iter) {
    uint64_t *swap;

    /* contrib[v] = score[v] / outdeg[v]: a sequential pass, so it is not the
     * interesting one; it exists so that the sweep's gather is a single
     * dependent load rather than two. */
    for (u = 0; u < graph.num_vertices; ++u) {
      contrib[u] = score[u] / outdeg[u];
    }

    pagerank_sweep(&graph, contrib, next_score);

    /* Self-check: every score keeps at least the teleport mass.  With integer
     * division the total mass is not conserved exactly, so a sum-equality
     * assertion would be wrong; this invariant is exact. */
    hg_check(next_score[0] >= PR_BASE, "score below teleport floor");
    swap = score;
    score = next_score;
    next_score = swap;
  }
  hg_roi_end();
  hg_report_time("roi", t0, hg_now());

  for (u = 0; u < graph.num_vertices; ++u) {
    hg_check(score[u] >= PR_BASE, "final score below teleport floor");
    checksum = hg_mix(checksum, score[u]);
  }

  free(score);
  free(next_score);
  free(contrib);
  free(outdeg);
  hg_csr_free(&graph);
  hg_finish(checksum);
  return 0;
}
