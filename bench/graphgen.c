/* ===========================================================================
 * bench/graphgen.c -- deterministic in-binary CSR graph generator.
 *
 * ROLE
 *   Builds the CSR consumed by bench/bfs.c and bench/pagerank.c without
 *   touching the filesystem, because the benchmarks must run under gem5 SE
 *   mode (docs/DESIGN.md sec 4.4).  Shipping a data file would also make the
 *   correctness gate depend on something outside the binary.
 *
 *   Everything is derived from (seed, vertex_id) with splitmix64, so the
 *   two passes (count degrees, fill edges) agree by construction and no
 *   intermediate edge list or sort is needed -- generation is O(E) with a
 *   small constant, which matters because it happens *before* ROI_BEGIN and
 *   therefore inside the instruction budget of a trace.
 *
 * Normative spec: ../docs/DESIGN.md
 * ===========================================================================
 */

#include "graphgen.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "common.h"

/* Domain separation constants: the degree draw must not consume entropy from
 * the same stream as the neighbour draws, or the two passes would diverge. */
#define HG_GG_DEGREE_DOMAIN 0x6772617068646567ull /* "graphdeg" */
#define HG_GG_EDGE_DOMAIN 0x6772617068656467ull   /* "graphedg" */

/* 1 vertex in HG_GG_HUB_RATE gets HG_GG_HUB_FACTOR times the average degree. */
#define HG_GG_HUB_RATE 16u
#define HG_GG_HUB_FACTOR 8u
/* 1 edge in HG_GG_LOCAL_RATE points at a near neighbour instead of a random
 * vertex, so the IP-stride baseline has something legitimate to catch. */
#define HG_GG_LOCAL_RATE 8u
#define HG_GG_LOCAL_SPAN 64u

static uint32_t hg_gg_degree_of(uint64_t seed, uint32_t u, uint32_t avg_degree) {
  uint64_t state = hg_seed_for(seed ^ HG_GG_DEGREE_DOMAIN, u);
  uint64_t r = hg_splitmix64(&state);
  uint32_t degree;

  if ((r % HG_GG_HUB_RATE) == 0u) {
    degree = avg_degree * HG_GG_HUB_FACTOR;
  } else {
    /* Spread the rest over [avg/2, 3*avg/2] so the frontier work per vertex
     * varies -- a constant degree would make the inner trip count trivially
     * predictable and let the stride prefetcher win for the wrong reason. */
    const uint32_t half = (avg_degree > 1u) ? (avg_degree / 2u) : 1u;
    degree = half + (uint32_t)((r >> 8) % (2u * half + 1u));
  }
  if (degree < 8u) degree = 8u;
  degree = (degree + 7u) & ~7u;
  return degree;
}

static uint32_t hg_gg_neighbour(uint64_t *state, uint32_t u,
                                uint32_t num_vertices) {
  const uint64_t r = hg_splitmix64(state);
  uint32_t v;

  if ((r % HG_GG_LOCAL_RATE) == 0u) {
    v = (uint32_t)((u + 1u + (uint32_t)((r >> 8) % HG_GG_LOCAL_SPAN)) %
                   num_vertices);
  } else {
    v = (uint32_t)((r >> 8) % num_vertices);
  }
  if (v == u) v = (v + 1u) % num_vertices; /* no self loops */
  return v;
}

void hg_csr_build(hg_csr *graph, uint32_t num_vertices, uint32_t avg_degree,
                  uint64_t seed) {
  uint64_t total = 0;
  uint32_t u;

  if (num_vertices < 2u) num_vertices = 2u;
  if (avg_degree < 1u) avg_degree = 1u;

  graph->num_vertices = num_vertices;
  graph->offsets = (uint32_t *)hg_alloc(
      ((size_t)num_vertices + 1u) * sizeof(uint32_t), "CSR offsets");

  /* Pass 1: degrees -> prefix sum. */
  graph->offsets[0] = 0u;
  for (u = 0; u < num_vertices; ++u) {
    total += hg_gg_degree_of(seed, u, avg_degree);
    if (total > 0xF0000000ull) {
      fprintf(stderr,
              "FATAL: graph too large: %llu edges exceeds the uint32 CSR "
              "limit; reduce --size\n",
              (unsigned long long)total);
      exit(1);
    }
    graph->offsets[u + 1u] = (uint32_t)total;
  }
  graph->num_edges = (uint32_t)total;

  graph->edges = (uint32_t *)hg_alloc((size_t)graph->num_edges *
                                          sizeof(uint32_t),
                                      "CSR edges");

  /* Pass 2: neighbours.  Re-derives the same degrees from the same seed. */
  for (u = 0; u < num_vertices; ++u) {
    uint64_t state = hg_seed_for(seed ^ HG_GG_EDGE_DOMAIN, u);
    const uint32_t begin = graph->offsets[u];
    const uint32_t end = graph->offsets[u + 1u];
    uint32_t e;
    for (e = begin; e < end; ++e) {
      graph->edges[e] = hg_gg_neighbour(&state, u, num_vertices);
    }
  }
}

void hg_csr_free(hg_csr *graph) {
  free(graph->offsets);
  free(graph->edges);
  graph->offsets = NULL;
  graph->edges = NULL;
  graph->num_vertices = 0u;
  graph->num_edges = 0u;
}

uint64_t hg_csr_bytes(const hg_csr *graph) {
  return ((uint64_t)graph->num_vertices + 1ull) * sizeof(uint32_t) +
         (uint64_t)graph->num_edges * sizeof(uint32_t);
}

uint32_t hg_csr_degree(const hg_csr *graph, uint32_t u) {
  return graph->offsets[u + 1u] - graph->offsets[u];
}

int hg_csr_validate(const hg_csr *graph) {
  uint32_t u;
  if (graph->offsets == NULL || graph->edges == NULL) return 0;
  if (graph->offsets[0] != 0u) return 0;
  if (graph->offsets[graph->num_vertices] != graph->num_edges) return 0;
  for (u = 0; u < graph->num_vertices; ++u) {
    uint32_t e;
    if (graph->offsets[u] > graph->offsets[u + 1u]) return 0;
    for (e = graph->offsets[u]; e < graph->offsets[u + 1u]; ++e) {
      if (graph->edges[e] >= graph->num_vertices) return 0;
    }
  }
  return 1;
}
