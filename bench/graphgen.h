/* ===========================================================================
 * bench/graphgen.h -- deterministic in-binary CSR graph generator (interface).
 *
 * ROLE
 *   Shared by bench/bfs.c and bench/pagerank.c.  The graph is generated *in
 *   the binary* from a seed rather than loaded from disk, because the
 *   benchmarks must run in gem5 SE mode with no filesystem
 *   (docs/DESIGN.md sec 4.4 and the gem5 SE constraints in bench/README.md).
 *
 * Normative spec: ../docs/DESIGN.md
 * ===========================================================================
 */

#ifndef EXPERIMENTAL_USERS_SVSOOLEBHAVI_HINT_GATHER_BENCH_GRAPHGEN_H_
#define EXPERIMENTAL_USERS_SVSOOLEBHAVI_HINT_GATHER_BENCH_GRAPHGEN_H_

#include <stdint.h>

/* Compressed sparse row.  `offsets` has num_vertices+1 entries; the
 * neighbours of u are edges[offsets[u] .. offsets[u+1]-1].
 *
 * Both arrays are uint32_t: the default sizes keep E well under 2^32, and
 * 4-byte indices are what the gather idiom in GAP-style kernels actually
 * uses -- it also makes elem_size=4 the interesting case for the
 * instruction's SHIFT field (docs/DESIGN.md sec 1.1). */
typedef struct {
  uint32_t num_vertices;
  uint32_t num_edges;
  uint32_t *offsets; /* num_vertices + 1 entries */
  uint32_t *edges;   /* num_edges entries        */
} hg_csr;

/* Builds a deterministic, skewed-degree random CSR.
 *
 * Determinism: every value is derived from (seed, vertex_id) via splitmix64,
 * so the degree-counting pass and the edge-filling pass agree without storing
 * an intermediate edge list, and two runs on two machines produce bit-identical
 * graphs.
 *
 * Structure: mostly uniform-random destinations (the irregular, prefetcher-
 * hostile part) with ~1 edge in 8 pointing at a near neighbour (a little
 * locality, so that the IP-stride baseline is not strawmanned), and a
 * heavy-tailed degree distribution (1 vertex in 16 has 8x the average degree),
 * which is the RMAT-ish property that matters for frontier behaviour.
 *
 * Exits(1) on allocation failure. */
void hg_csr_build(hg_csr *graph, uint32_t num_vertices, uint32_t avg_degree,
                  uint64_t seed);

void hg_csr_free(hg_csr *graph);

/* Total bytes of the two arrays (for the FOOTPRINT_MIB report). */
uint64_t hg_csr_bytes(const hg_csr *graph);

/* Out-degree of u. */
uint32_t hg_csr_degree(const hg_csr *graph, uint32_t u);

/* Cheap structural self-check: offsets monotonic, terminator correct, every
 * edge in range.  Returns 1 on success. */
int hg_csr_validate(const hg_csr *graph);

#endif /* EXPERIMENTAL_USERS_SVSOOLEBHAVI_HINT_GATHER_BENCH_GRAPHGEN_H_ */
