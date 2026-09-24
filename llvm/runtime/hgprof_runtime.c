/* hgprof_runtime.c - HINT.GATHER profile runtime.
 *
 * Role: implements __hg_profile_access(site_id, addr), the callback that
 * llvm/HintGatherPass.cpp inserts in -hg-mode=profile.  Per site it keeps a
 * histogram of consecutive address deltas and, at exit, writes
 * hg_profile.json with the normalised Shannon entropy of that histogram.
 * The LLVM pass reads that file back via -hg-profile= to rank candidate
 * sites, and compares the entropy against the genome's entropy_threshold.
 *
 * Normative spec: docs/DESIGN.md section 4.1 ("Profile runtime"):
 *   {"sites": [{"site_id": 0, "accesses": 1048576, "distinct_deltas": 9871,
 *               "entropy": 0.83, "top_delta_share": 0.02}]}
 *   entropy = Shannon entropy of the delta histogram normalised to
 *             log2(min(distinct_deltas, 256)), so it lands in [0, 1].
 *
 * Constraints this file is written to satisfy:
 *   - Plain C (C99), no C++, no libm, no threads, no dynamic format tricks.
 *     Only <stdio.h>, <stdlib.h>, <string.h>, <stdint.h>.
 *   - Must work inside a gem5 SE-mode *static* binary, where the filesystem
 *     may not be usable: therefore the same JSON is also echoed to stdout
 *     between the markers HG_PROFILE_BEGIN / HG_PROFILE_END so a trace-only
 *     harness can scrape it from the console log.
 *   - No floating-point printf: newlib-nano in SE mode often links a printf
 *     without %f support, which would silently emit garbage.  All doubles are
 *     formatted by hand as fixed point with 6 decimals (hg_fmt_fixed).
 *   - No libm: log2 is computed in hg_log2() with a bit trick plus an atanh
 *     series.
 *
 * Build (host):   cc -O2 -c hgprof_runtime.c
 * Build (riscv):  riscv64-unknown-elf-gcc -O2 -c hgprof_runtime.c
 * Link it into the benchmark built with -hg-mode=profile.
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "hgprof_runtime.h"

/* ------------------------------------------------------------------ */
/* Tunables                                                            */
/* ------------------------------------------------------------------ */

#ifndef HG_MAX_SITES
#define HG_MAX_SITES 256
#endif

/* DESIGN.md: cap 4096 distinct deltas per site, everything else counted in an
 * overflow bucket. */
#ifndef HG_MAX_DELTAS
#define HG_MAX_DELTAS 4096
#endif

/* Open-addressing table, power of two, load factor <= 0.5. */
#define HG_SLOTS (HG_MAX_DELTAS * 2)

#ifndef HG_PROFILE_DEFAULT_PATH
#define HG_PROFILE_DEFAULT_PATH "hg_profile.json"
#endif

/* ------------------------------------------------------------------ */
/* State                                                               */
/* ------------------------------------------------------------------ */

struct hg_bucket {
  uint64_t delta; /* valid only when count != 0 */
  uint64_t count;
};

struct hg_site {
  int touched;              /* site has been seen at least once   */
  int oom;                  /* table allocation failed            */
  int have_last;            /* last_addr is valid                 */
  uint64_t last_addr;       /* ring buffer of depth 1             */
  uint64_t accesses;        /* total calls for this site          */
  uint64_t delta_samples;   /* accesses - 1, clamped at 0         */
  uint64_t overflow;        /* deltas dropped once the table filled */
  uint32_t distinct;        /* distinct deltas currently in table */
  struct hg_bucket *table;  /* HG_SLOTS entries, lazily allocated */
};

static struct hg_site hg_sites[HG_MAX_SITES];
static int hg_atexit_registered = 0;
static int hg_dumped = 0;

/* ------------------------------------------------------------------ */
/* Math helpers (no libm)                                              */
/* ------------------------------------------------------------------ */

/* log2 for x > 0.  Splits off the binary exponent with a bit trick, then uses
 * the atanh series for the mantissa in [1,2):
 *   ln(m) = 2 * (t + t^3/3 + t^5/5 + ...),  t = (m-1)/(m+1),  |t| <= 1/3
 * which converges fast enough that 10 terms are well past double precision.
 */
static double hg_log2(double x) {
  union {
    double d;
    uint64_t u;
  } v;
  int e;
  double m, t, t2, term, sum;
  int k;

  if (!(x > 0.0))
    return 0.0;

  v.d = x;
  e = (int)((v.u >> 52) & 0x7ffu) - 1023;
  /* Force the exponent field to 1023 -> mantissa value in [1,2). */
  v.u = (v.u & ~((uint64_t)0x7ff << 52)) | ((uint64_t)1023 << 52);
  m = v.d;

  t = (m - 1.0) / (m + 1.0);
  t2 = t * t;
  term = t;
  sum = 0.0;
  for (k = 1; k <= 21; k += 2) {
    sum += term / (double)k;
    term *= t2;
  }
  /* 2*sum = ln(m); multiply by 1/ln(2). */
  return (double)e + 2.0 * sum * 1.4426950408889634;
}

/* 64-bit mixer (splitmix64 finaliser) used as the hash of a delta. */
static uint64_t hg_mix(uint64_t z) {
  z += 0x9e3779b97f4a7c15ULL;
  z = (z ^ (z >> 30)) * 0xbf58476d1ce4e5b9ULL;
  z = (z ^ (z >> 27)) * 0x94d049bb133111ebULL;
  return z ^ (z >> 31);
}

/* Fixed-point formatting: "%0.6f" without touching the float printf path.
 * `buf` must hold at least 32 bytes.  Values are clamped to a sane range;
 * negative values (only ever -1 sentinels) are printed with a leading '-'. */
static void hg_fmt_fixed(char *buf, size_t n, double v) {
  int neg = 0;
  long long scaled, ip, fp;

  if (v != v) { /* NaN */
    snprintf(buf, n, "0.000000");
    return;
  }
  if (v < 0.0) {
    neg = 1;
    v = -v;
  }
  if (v > 1.0e12)
    v = 1.0e12;

  scaled = (long long)(v * 1000000.0 + 0.5);
  ip = scaled / 1000000;
  fp = scaled % 1000000;
  snprintf(buf, n, "%s%lld.%06lld", neg ? "-" : "", ip, fp);
}

/* ------------------------------------------------------------------ */
/* Instrumentation entry point                                         */
/* ------------------------------------------------------------------ */

static void hg_record_delta(struct hg_site *s, uint64_t delta) {
  uint64_t h;
  uint32_t idx, i;

  if (!s->table) {
    if (s->oom)
      return;
    s->table = (struct hg_bucket *)calloc(HG_SLOTS, sizeof(struct hg_bucket));
    if (!s->table) {
      s->oom = 1;
      s->overflow++;
      return;
    }
  }

  h = hg_mix(delta);
  idx = (uint32_t)(h & (uint64_t)(HG_SLOTS - 1));
  for (i = 0; i < HG_SLOTS; ++i) {
    struct hg_bucket *b = &s->table[idx];
    if (b->count == 0) {
      if (s->distinct >= HG_MAX_DELTAS) {
        s->overflow++; /* table is full: lump this delta together */
        return;
      }
      b->delta = delta;
      b->count = 1;
      s->distinct++;
      return;
    }
    if (b->delta == delta) {
      b->count++;
      return;
    }
    idx = (idx + 1u) & (uint32_t)(HG_SLOTS - 1);
  }
  /* Unreachable while HG_SLOTS > HG_MAX_DELTAS, but stay total. */
  s->overflow++;
}

void __hg_profile_access(uint32_t site_id, uint64_t addr) {
  struct hg_site *s;

  if (site_id >= (uint32_t)HG_MAX_SITES)
    return; /* silently ignore: profiling must never change behaviour */

  if (!hg_atexit_registered) {
    hg_atexit_registered = 1;
    atexit(__hg_profile_dump);
  }

  s = &hg_sites[site_id];
  s->touched = 1;
  s->accesses++;

  if (s->have_last) {
    /* Unsigned wrap-around is well defined and keeps backward strides as a
     * distinct (large) delta value, which is what we want for entropy. */
    hg_record_delta(s, addr - s->last_addr);
    s->delta_samples++;
  }
  s->last_addr = addr;
  s->have_last = 1;
}

void __hg_profile_reset(void) {
  int i;
  for (i = 0; i < HG_MAX_SITES; ++i) {
    struct hg_site *s = &hg_sites[i];
    if (s->table)
      memset(s->table, 0, HG_SLOTS * sizeof(struct hg_bucket));
    s->touched = 0;
    s->have_last = 0;
    s->last_addr = 0;
    s->accesses = 0;
    s->delta_samples = 0;
    s->overflow = 0;
    s->distinct = 0;
    /* s->oom and s->table are kept: the allocation is still good. */
  }
  hg_dumped = 0;
}

/* ------------------------------------------------------------------ */
/* Reporting                                                           */
/* ------------------------------------------------------------------ */

struct hg_summary {
  uint64_t accesses;
  uint64_t distinct;
  double entropy;
  double top_delta_share;
};

static void hg_summarise(const struct hg_site *s, struct hg_summary *out) {
  uint64_t total = 0, top = 0;
  uint32_t i;
  uint64_t effective_distinct;
  double h = 0.0, denom;

  out->accesses = s->accesses;
  out->distinct = 0;
  out->entropy = 0.0;
  out->top_delta_share = 0.0;

  if (s->table) {
    for (i = 0; i < HG_SLOTS; ++i) {
      uint64_t c = s->table[i].count;
      if (!c)
        continue;
      total += c;
      if (c > top)
        top = c;
    }
  }
  /* Each overflow sample is a distinct delta that did not fit in the 4096-slot
   * table (count = 1 each), pushing entropy toward 1.0 for irregular sites. */
  if (s->overflow) {
    total += s->overflow;
    if (top == 0)
      top = 1;
  }

  effective_distinct = (uint64_t)s->distinct + s->overflow;
  out->distinct = effective_distinct;

  if (total == 0 || effective_distinct <= 1) {
    /* One symbol (or none): zero entropy by definition. */
    out->entropy = 0.0;
    out->top_delta_share = (total == 0) ? 0.0 : 1.0;
    return;
  }

  if (s->table) {
    for (i = 0; i < HG_SLOTS; ++i) {
      uint64_t c = s->table[i].count;
      double p;
      if (!c)
        continue;
      p = (double)c / (double)total;
      h -= p * hg_log2(p);
    }
  }
  if (s->overflow) {
    double p1 = 1.0 / (double)total;
    h -= (double)s->overflow * (p1 * hg_log2(p1));
  }

  /* Normalise to log2(min(distinct_deltas, 256)) per DESIGN.md section 4.1. */
  denom = hg_log2((double)(effective_distinct < 256 ? effective_distinct : 256));
  if (denom > 0.0)
    h /= denom;
  if (h < 0.0)
    h = 0.0;
  if (h > 1.0)
    h = 1.0;

  out->entropy = h;
  out->top_delta_share = (double)top / (double)total;
}

static void hg_write_json(FILE *f) {
  int i;
  int first = 1;
  char ebuf[32];
  char tbuf[32];

  fprintf(f, "{\"sites\": [");
  for (i = 0; i < HG_MAX_SITES; ++i) {
    struct hg_site *s = &hg_sites[i];
    struct hg_summary sum;
    if (!s->touched)
      continue;
    hg_summarise(s, &sum);
    hg_fmt_fixed(ebuf, sizeof(ebuf), sum.entropy);
    hg_fmt_fixed(tbuf, sizeof(tbuf), sum.top_delta_share);
    fprintf(f, "%s\n  {\"site_id\": %d, \"accesses\": %llu, "
               "\"distinct_deltas\": %llu, \"entropy\": %s, "
               "\"top_delta_share\": %s}",
            first ? "" : ",", i, (unsigned long long)sum.accesses,
            (unsigned long long)sum.distinct, ebuf, tbuf);
    first = 0;
  }
  fprintf(f, "%s]}\n", first ? "" : "\n");
}

void __hg_profile_dump(void) {
  const char *path;
  FILE *f;

  if (hg_dumped)
    return; /* idempotent: harness call + atexit must not double-print */
  hg_dumped = 1;

  path = getenv("HG_PROFILE_OUT");
  if (!path || !*path)
    path = HG_PROFILE_DEFAULT_PATH;

  f = fopen(path, "w");
  if (f) {
    hg_write_json(f);
    fclose(f);
  }

  /* gem5 SE mode may have no writable filesystem, so always echo as well.
   * The markers let a trace-only harness scrape the JSON out of the log. */
  printf("HG_PROFILE_BEGIN\n");
  hg_write_json(stdout);
  printf("HG_PROFILE_END\n");
  fflush(stdout);
}
