/* gather_test.c - detection fixture for the HINT.GATHER LLVM pass.
 *
 * Role: input for llvm/test/run_analyze_test.sh, the gate the CHIA loop runs
 * after (re)building libHintGather.so.  It contains exactly one site that must
 * be ACCEPTED and several that must be REJECTED.
 *
 * Normative spec: docs/DESIGN.md section 4.1 (detection rule and SCEV filter).
 *
 * Deliberately has NO main(): a main would let the inliner clone these loops
 * and produce extra candidate sites, which would make the "exactly one
 * accepted site" assertion flaky.
 *
 * Expected classification (see run_analyze_test.sh):
 *
 *   affine_stream()         no load -> gep -> load chain at all; not even a
 *                           candidate.
 *   affine_indirect()       IS a load -> gep -> load chain, but LICM hoists
 *                           the index load so the dependent address becomes an
 *                           affine SCEVAddRec -> rejected, the stride
 *                           prefetcher already covers it.  (If the hoist does
 *                           not happen on your LLVM, it is instead rejected as
 *                           index_pointer_not_affine_addrec -- still rejected,
 *                           which is all the gate asserts.)
 *   gather_sum()            the genuine A[B[i]] gather -> ACCEPTED.
 */

/* ---------------------------------------------------------------------- */
/* REJECT: two affine streams, no indirection.                            */
/* ---------------------------------------------------------------------- */
long affine_stream(const int *a, const int *b, long n) {
  long s = 0;
  for (long i = 0; i < n; i++)
    s += (long)a[i] + (long)b[i];
  return s;
}

/* ---------------------------------------------------------------------- */
/* REJECT: the index is loaded, but it is loop invariant, so the dependent
 * address is still an affine AddRec (p advances by a constant stride).    */
/* ---------------------------------------------------------------------- */
long affine_indirect(const int *a, const long *k, long n) {
  long s = 0;
  const int *p = a;
  for (long i = 0; i < n; i++) {
    s += p[k[0]];
    p += 16;
  }
  return s;
}

/* ---------------------------------------------------------------------- */
/* ACCEPT: the real gather. b[] is an affine stream (so &b[i+d] is
 * computable ahead of time) and a[b[i]] is not an affine AddRec.          */
/* ---------------------------------------------------------------------- */
long gather_sum(const int *a, const int *b, long n) {
  long s = 0;
  for (long i = 0; i < n; i++)
    s += a[b[i]];
  return s;
}
