/* ===========================================================================
 * bench/bfs.c -- GAP-style top-down BFS over a CSR graph.
 *
 * ROLE
 *   The realistic version of the idiom: the canonical
 *     for (e = off[u]; e < off[u+1]; ++e) { v = edges[e]; if (!visited[v]) ... }
 *   indirection from the GAP benchmark suite. Unlike bench/gather.c this one
 *   has data-dependent control flow (the `if`), a variable inner trip count,
 *   and a frontier whose size changes by orders of magnitude between levels --
 *   so it exercises the parts of docs/DESIGN.md that gather.c cannot:
 *     - min_trip_count (sec 3): short inner loops must be rejected by the pass;
 *     - droppable / mshr_pressure_threshold (sec 2 step 4): the first few levels
 *       burst enormously and will saturate the MSHRs;
 *     - wasted prefetches: the `if` means many gathered lines are not used.
 *
 *   Contract: docs/DESIGN.md sec 4.4.
 *
 * WORKING SET (default --size 262144 vertices, average degree 8)
 *   edges     ~2.1 Mi x 4 B = ~8.0 MiB
 *   offsets   262145  x 4 B =  1.0 MiB
 *   dist      262144  x 4 B =  1.0 MiB
 *   frontier + next         =  2.0 MiB
 *   total                  ~= 12 MiB  -- far beyond a 32 KiB L1D / 1 MiB L2.
 * ===========================================================================
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "common.h"
#include "graphgen.h"

#define BFS_DEFAULT_SIZE (1u << 18) /* vertices */
#define BFS_DEFAULT_ITERS 4u        /* BFS runs, from different roots */

#ifndef BFS_AVG_DEGREE
#define BFS_AVG_DEGREE 8u
#endif

#define BFS_UNVISITED 0xFFFFFFFFu

/* dist[] is uint32_t, so the index scale is 1 << 2 (docs/DESIGN.md sec 1.1). */
#define BFS_ELEM_SHIFT 2

/* --------------------------------------------------------------------------
 * One BFS level.  Returns the size of the next frontier.
 * ------------------------------------------------------------------------*/
static uint32_t bfs_level(const hg_csr *graph, uint32_t *dist,
                          const uint32_t *frontier, uint32_t frontier_size,
                          uint32_t *next, uint32_t level) {
  const uint32_t *offsets = graph->offsets;
  const uint32_t *edges = graph->edges;
  const uint32_t num_edges = graph->num_edges;
  uint32_t next_size = 0;
  uint32_t f;

  for (f = 0; f < frontier_size; ++f) {
    const uint32_t u = frontier[f];
    const uint32_t begin = offsets[u];
    const uint32_t end = offsets[u + 1u];
    uint32_t e;

#if HG_BUILD_HINT && defined(__clang__)
#pragma clang loop unroll_count(8)
#endif
    for (e = begin; e < end; ++e) {
#if HG_BUILD_SWPF
      /* Honest baseline: the index must be *loaded* before its target can be
       * prefetched, and that load is an ordinary instruction consuming an
       * issue-queue slot and an LSQ entry (docs/DESIGN.md sec 1.4). */
      if (e + HG_SWPF_DISTANCE < num_edges) {
        HG_PREFETCH_R(&dist[edges[e + HG_SWPF_DISTANCE]]);
        HG_PREFETCH_R(&edges[e + 2 * HG_SWPF_DISTANCE]);
      }
#endif
#if HG_BUILD_HINT && HG_MANUAL_HINTS
      if (e + HG_HINT_DISTANCE < num_edges) {
        HG_HINT_SITE_C(dist, &edges[e + HG_HINT_DISTANCE], BFS_ELEM_SHIFT);
      }
#endif

      {
        const uint32_t v = edges[e];
        const uint32_t dv = dist[v]; /* <-- the hint site */
        const uint32_t unvisited = (dv == BFS_UNVISITED);
        dist[v] = unvisited ? level : dv;
        next[next_size] = v;
        next_size += unvisited;
      }
    }
  }
  (void)num_edges; /* unused in the base build */
  return next_size;
}

/* A full BFS from `root`.  Returns the number of visited vertices and mixes
 * the level structure into *checksum. */
static uint32_t bfs_run(const hg_csr *graph, uint32_t root, uint32_t *dist,
                        uint32_t *frontier, uint32_t *next,
                        uint64_t *checksum) {
  const uint32_t num_vertices = graph->num_vertices;
  uint32_t frontier_size = 1u;
  uint32_t visited = 1u;
  uint32_t level = 1u;
  uint32_t i;

  for (i = 0; i < num_vertices; ++i) dist[i] = BFS_UNVISITED;
  dist[root] = 0u;
  frontier[0] = root;

  while (frontier_size != 0u) {
    const uint32_t next_size =
        bfs_level(graph, dist, frontier, frontier_size, next, level);
    uint32_t *const old_frontier = frontier;

    *checksum = hg_mix(*checksum, ((uint64_t)level << 32) | next_size);
    visited += next_size;

    /* Double-buffer: both arrays are caller-owned scratch of num_vertices
     * entries, so rotating the pointers avoids any copy and keeps the kernel
     * allocation-free (required for gem5 SE mode). */
    frontier = next;
    next = old_frontier;
    frontier_size = next_size;
    ++level;
  }
  return visited;
}

int main(int argc, char **argv) {
  const hg_args args =
      hg_parse_args(argc, argv, BFS_DEFAULT_ITERS, BFS_DEFAULT_SIZE);
  const uint32_t num_vertices =
      (args.size > 0x7FFFFFF0ull) ? 0x7FFFFFF0u : (uint32_t)args.size;
  hg_csr graph;
  uint32_t *dist;
  uint32_t *frontier;
  uint32_t *next;
  uint64_t checksum = 0;
  uint64_t iter;
  hg_time_t t0;

  hg_csr_build(&graph, num_vertices, BFS_AVG_DEGREE, args.seed);

  dist = (uint32_t *)hg_alloc((size_t)graph.num_vertices * sizeof(uint32_t),
                              "dist[]");
  frontier = (uint32_t *)hg_alloc(
      ((size_t)graph.num_vertices + 1u) * sizeof(uint32_t), "frontier[]");
  next = (uint32_t *)hg_alloc(
      ((size_t)graph.num_vertices + 1u) * sizeof(uint32_t), "next[]");

  hg_banner("bfs", &args,
            hg_csr_bytes(&graph) +
                3ull * (uint64_t)graph.num_vertices * sizeof(uint32_t));
  if (!args.quiet) {
    printf("GRAPH_VERTICES=%u GRAPH_EDGES=%u AVG_DEGREE=%u\n",
           graph.num_vertices, graph.num_edges, (unsigned)BFS_AVG_DEGREE);
    fflush(stdout);
  }
  hg_check(hg_csr_validate(&graph), "generated CSR is malformed");

  t0 = hg_now();
  hg_roi_begin();
  for (iter = 0; iter < args.iters; ++iter) {
    /* Distinct, deterministic roots; the multiplier is coprime with any
     * power-of-two vertex count, so the roots spread out. */
    const uint32_t root =
        (uint32_t)((iter * 2654435761ull) % (uint64_t)graph.num_vertices);
    const uint32_t visited =
        bfs_run(&graph, root, dist, frontier, next, &checksum);
    /* Self-checks: the root is at level 0, at least the root was visited, and
     * no vertex can have been visited more than once (visited counts frontier
     * insertions, which happen exactly when dist[] transitions away from
     * UNVISITED). */
    hg_check(dist[root] == 0u, "root not at level 0");
    hg_check(visited >= 1u && visited <= graph.num_vertices,
             "visited count out of range");
    checksum = hg_mix(checksum, ((uint64_t)root << 32) | visited);
  }
  hg_roi_end();
  hg_report_time("roi", t0, hg_now());

  free(dist);
  free(frontier);
  free(next);
  hg_csr_free(&graph);
  hg_finish(checksum);
  return 0;
}
