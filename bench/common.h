/* ===========================================================================
 * bench/common.h -- shared harness for the HINT.GATHER benchmark suite.
 *
 * ROLE
 *   Everything the four benchmarks (gather, bfs, pagerank, listchase) share:
 *   the docs/DESIGN.md sec 4.4 contract (--iters / --size, determinism,
 *   self-checking, exactly one CHECKSUM= line, ROI_BEGIN / ROI_END, exit 0),
 *   a seeded RNG, a checksum mixer, allocation helpers, and the three
 *   build-variant macros (base / swpf / hint).
 *
 *   Normative spec: ../docs/DESIGN.md -- sec 1.1 (encoding), sec 4.1 (the
 *   HINT_GATHER macro contract), sec 4.4 (this contract).
 *
 * CONSTRAINTS (gem5 SE mode)
 *   Plain C99, no external libraries, no libm, no threads, no file I/O, no
 *   mmap tricks.  Only malloc/free, printf and (optionally) clock().
 *
 * STYLE NOTE
 *   Every helper is `static inline`: the header is included by several
 *   translation units (e.g. graphgen.c uses only a few of them), and
 *   `static inline` keeps each copy private without provoking
 *   -Wunused-function for the helpers a given benchmark does not call.
 * ===========================================================================
 */

#ifndef EXPERIMENTAL_USERS_SVSOOLEBHAVI_HINT_GATHER_BENCH_COMMON_H_
#define EXPERIMENTAL_USERS_SVSOOLEBHAVI_HINT_GATHER_BENCH_COMMON_H_

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---------------------------------------------------------------------------
 * 1. The HINT.GATHER macro contract (docs/DESIGN.md sec 4.1).
 *
 * The canonical header is owned by the LLVM node.  We include it when it is
 * present and fall back to an equivalent local definition otherwise, so that
 * bench/ can be compiled and smoke-tested before llvm/ exists.  The canonical
 * header always wins.
 *
 * TODO(verify): once llvm/include/hint_gather.h lands, confirm that (a) the
 * argument order of HG_FUNCT3/HG_FUNCT7 matches the fallback below and
 * (b) HG_FUNCT7's `fanout` argument is the genome-level count (1..8), which
 * the encoding stores as fanout-1 (docs/DESIGN.md sec 1.1).  If the canonical
 * header disagrees, delete the fallback rather than "fixing" both.
 * -------------------------------------------------------------------------*/
#if defined(__has_include)
#if __has_include("../llvm/include/hint_gather.h")
#include "../llvm/include/hint_gather.h"
#define HG_HAVE_CANONICAL_HINT_HEADER 1
#elif __has_include(<hint_gather.h>)
#include <hint_gather.h>
#define HG_HAVE_CANONICAL_HINT_HEADER 1
#endif
#endif

#ifndef HG_HAVE_CANONICAL_HINT_HEADER
#define HG_HAVE_CANONICAL_HINT_HEADER 0
#endif

#define HG_LEVEL_L1D 0
#define HG_LEVEL_L2C 1
#define HG_DROP_NO 0
#define HG_DROP_YES 1

#if !HG_HAVE_CANONICAL_HINT_HEADER

/* funct3: [0] variant, [1] level, [2] droppable   (docs/DESIGN.md sec 1.1) */
#define HG_FUNCT3(variant, level, drop) \
  ((((variant) & 1) << 0) | (((level) & 1) << 1) | (((drop) & 1) << 2))

/* funct7: [1:0] shift, [4:2] fanout-1, [6:5] reserved (must be 0) */
#define HG_FUNCT7(shift, fanout) \
  ((((shift) & 3) << 0) | (((((fanout) - 1)) & 7) << 2))

#if defined(__riscv) && (__riscv_xlen == 64)

/* Variant 1 -- chase form: rs2 is &B[i+d] (affine, ready at dispatch). */
#define HINT_GATHER_C(base, idx_addr, shift, fanout, level, drop)   \
  __asm__ volatile(".insn r 0x0b, %2, %3, x0, %0, %1"               \
                   :                                                \
                   : "r"(base), "r"(idx_addr),                      \
                     "i"(HG_FUNCT3(1, level, drop)),                \
                     "i"(HG_FUNCT7(shift, fanout)))

/* Variant 0 -- value form: rs2 is the already-loaded index B[i+d]. */
#define HINT_GATHER(base, idx_val, shift, fanout, level, drop)      \
  __asm__ volatile(".insn r 0x0b, %2, %3, x0, %0, %1"               \
                   :                                                \
                   : "r"(base), "r"(idx_val),                       \
                     "i"(HG_FUNCT3(0, level, drop)),                \
                     "i"(HG_FUNCT7(shift, fanout)))

#else /* not RISC-V: the hint is architecturally a NOP (docs/DESIGN.md sec 1.3) */

#define HINT_GATHER_C(base, idx_addr, shift, fanout, level, drop) \
  do {                                                            \
    (void)(base);                                                 \
    (void)(idx_addr);                                             \
  } while (0)
#define HINT_GATHER(base, idx_val, shift, fanout, level, drop) \
  do {                                                         \
    (void)(base);                                              \
    (void)(idx_val);                                           \
  } while (0)

#endif /* __riscv */
#endif /* !HG_HAVE_CANONICAL_HINT_HEADER */

/* ---------------------------------------------------------------------------
 * 2. Build variants (docs/DESIGN.md sec 4.4)
 *
 *   HG_BUILD_BASE : no software prefetching at all.
 *   HG_BUILD_SWPF : Zicbop-style software prefetch baseline -- ordinary
 *                   instructions, including the *real* cost of loading the
 *                   index ahead of time.  This is the honest baseline that
 *                   HINT.GATHER has to beat.
 *   HG_BUILD_HINT : HINT.GATHER.  Hints are normally inserted by the LLVM
 *                   pass plugin, so the source is identical to the base
 *                   build; define HG_MANUAL_HINTS=1 to emit them from the
 *                   source instead (useful for testing the encoding without
 *                   the plugin, and for eyeballing what the pass should do).
 * -------------------------------------------------------------------------*/
#if !defined(HG_BUILD_BASE) && !defined(HG_BUILD_SWPF) && \
    !defined(HG_BUILD_HINT)
#define HG_BUILD_BASE 1
#endif
#ifndef HG_BUILD_BASE
#define HG_BUILD_BASE 0
#endif
#ifndef HG_BUILD_SWPF
#define HG_BUILD_SWPF 0
#endif
#ifndef HG_BUILD_HINT
#define HG_BUILD_HINT 0
#endif
#ifndef HG_MANUAL_HINTS
#define HG_MANUAL_HINTS 0
#endif

/* Lookahead, in elements, for the software-prefetch baseline.  Keep this in
 * step with the genome's hint_distance when comparing the two. */
#ifndef HG_SWPF_DISTANCE
#define HG_SWPF_DISTANCE 32
#endif

/* Lookahead used by manual hints (only when HG_MANUAL_HINTS=1). */
#ifndef HG_HINT_DISTANCE
#define HG_HINT_DISTANCE 32
#endif
#ifndef HG_HINT_FANOUT
#define HG_HINT_FANOUT 1
#endif

#if HG_BUILD_SWPF
/* rw=0 (read), locality=3 (keep in all levels), i.e. an ordinary
 * prefetch.r / prefetchnta-class instruction. */
#define HG_PREFETCH_R(p) __builtin_prefetch((const void *)(p), 0, 3)
#else
#define HG_PREFETCH_R(p) ((void)(p))
#endif

/* Emit a manual HINT.GATHER.C at a gather site.  A no-op unless this is the
 * hint build *and* manual hints were requested; in the normal hint build the
 * LLVM pass inserts the instruction itself (docs/DESIGN.md sec 4.1). */
#if HG_BUILD_HINT && HG_MANUAL_HINTS
#define HG_HINT_SITE_C(base, idx_addr, shift)                        \
  HINT_GATHER_C((base), (idx_addr), (shift), HG_HINT_FANOUT,         \
                HG_LEVEL_L1D, HG_DROP_YES)
#define HG_HINT_SITE_V(base, idx_val, shift)                         \
  HINT_GATHER((base), (idx_val), (shift), HG_HINT_FANOUT,            \
              HG_LEVEL_L1D, HG_DROP_YES)
#else
#define HG_HINT_SITE_C(base, idx_addr, shift) \
  do {                                        \
    (void)(base);                             \
    (void)(idx_addr);                         \
  } while (0)
#define HG_HINT_SITE_V(base, idx_val, shift) \
  do {                                       \
    (void)(base);                            \
    (void)(idx_val);                         \
  } while (0)
#endif

static inline const char *hg_variant_name(void) {
#if HG_BUILD_HINT
  return "hint";
#elif HG_BUILD_SWPF
  return "swpf";
#else
  return "base";
#endif
}

/* ---------------------------------------------------------------------------
 * 3. Timing (optional, never part of the correctness contract)
 *
 * gem5 SE mode implements the clock syscalls, but a bare-metal newlib
 * riscv64-unknown-elf target may not.  Off by default on RISC-V.
 * -------------------------------------------------------------------------*/
#ifndef HG_ENABLE_TIMING
#if defined(__riscv)
#define HG_ENABLE_TIMING 0
#else
#define HG_ENABLE_TIMING 1
#endif
#endif

#if HG_ENABLE_TIMING
#include <time.h>
typedef clock_t hg_time_t;
static inline hg_time_t hg_now(void) { return clock(); }
static inline void hg_report_time(const char *what, hg_time_t begin,
                                  hg_time_t end) {
  const double ms = 1000.0 * (double)(end - begin) / (double)CLOCKS_PER_SEC;
  printf("TIME_MS_%s=%.1f\n", what, ms);
}
#else
typedef int hg_time_t;
static inline hg_time_t hg_now(void) { return 0; }
static inline void hg_report_time(const char *what, hg_time_t begin,
                                  hg_time_t end) {
  (void)what;
  (void)begin;
  (void)end;
}
#endif

/* ---------------------------------------------------------------------------
 * 4. Deterministic RNG -- splitmix64, identical on every host.
 * -------------------------------------------------------------------------*/
static inline uint64_t hg_splitmix64(uint64_t *state) {
  uint64_t z = (*state += 0x9E3779B97F4A7C15ull);
  z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
  z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
  return z ^ (z >> 31);
}

/* Per-object seeding, so that two passes over the same object (e.g. the
 * degree-counting and edge-filling passes of the CSR builder) reproduce the
 * same sequence without storing anything. */
static inline uint64_t hg_seed_for(uint64_t seed, uint64_t object_id) {
  uint64_t s = seed ^ (object_id * 0xD6E8FEB86659FD93ull);
  (void)hg_splitmix64(&s); /* decorrelate adjacent object ids */
  return s;
}

static inline uint64_t hg_rand_below(uint64_t *state, uint64_t bound) {
  /* Fast multiply-high reduction (Lemire) avoids non-pipelined 64-bit hardware
   * division (remu) during pre-ROI setup while remaining 100% deterministic. */
  if (bound == 0) return 0;
  if (bound <= 0xFFFFFFFFull) {
    return ((uint64_t)(uint32_t)hg_splitmix64(state) *
            (uint64_t)(uint32_t)bound) >>
           32;
  }
  return hg_splitmix64(state) % bound;
}

/* ---------------------------------------------------------------------------
 * 5. Checksums -- order-sensitive, integer-only (no FP, so bit-exact across
 * hosts and across the base/swpf/hint variants).  The correctness gate in
 * docs/DESIGN.md sec 4.4 compares this value between base and hint.
 * -------------------------------------------------------------------------*/
static inline uint64_t hg_mix(uint64_t accumulator, uint64_t value) {
  accumulator ^= value + 0x9E3779B97F4A7C15ull + (accumulator << 6) +
                 (accumulator >> 2);
  return accumulator;
}

/* ---------------------------------------------------------------------------
 * 6. Allocation -- plain malloc; gem5 SE handles brk/anonymous mmap.
 * -------------------------------------------------------------------------*/
static inline void *hg_alloc(size_t bytes, const char *what) {
  void *p = malloc(bytes);
  if (p == NULL) {
    /* %llu + an explicit cast: the freestanding RISC-V libc may not support
     * %zu, and portability beats the google3 preference for fixed-width
     * printf specifiers here. */
    fprintf(stderr, "FATAL: out of memory allocating %s (%llu bytes)\n", what,
            (unsigned long long)bytes);
    exit(1);
  }
  return p;
}

static inline double hg_mib(uint64_t bytes) {
  return (double)bytes / (1024.0 * 1024.0);
}

/* ---------------------------------------------------------------------------
 * 7. Argument parsing (docs/DESIGN.md sec 4.4: --iters N and --size N)
 * -------------------------------------------------------------------------*/
typedef struct {
  uint64_t iters;
  uint64_t size;
  uint64_t seed;
  int quiet; /* suppress the informational footprint lines */
} hg_args;

static inline void hg_usage(const char *prog, const hg_args *defaults) {
  fprintf(stderr,
          "usage: %s [--iters N] [--size N] [--seed N] [--quiet]\n"
          "  --iters N  repetitions of the kernel      (default %llu)\n"
          "  --size  N  problem size, see README.md    (default %llu)\n"
          "  --seed  N  RNG seed; changes the data,    (default %llu)\n"
          "             and therefore the checksum\n",
          prog, (unsigned long long)defaults->iters,
          (unsigned long long)defaults->size,
          (unsigned long long)defaults->seed);
}

static inline uint64_t hg_parse_u64(const char *text, const char *flag) {
  char *end = NULL;
  unsigned long long value;
  if (text == NULL || *text == '\0') {
    fprintf(stderr, "FATAL: %s requires a value\n", flag);
    exit(2);
  }
  /* strtoull, not a google3 helper: this must build freestanding for RISC-V
   * with nothing but the C99 library. */
  value = strtoull(text, &end, 0);
  if (end == text || (end != NULL && *end != '\0')) {
    fprintf(stderr, "FATAL: %s: cannot parse '%s' as an integer\n", flag, text);
    exit(2);
  }
  return (uint64_t)value;
}

/* Accepts both "--flag N" and "--flag=N". */
static inline hg_args hg_parse_args(int argc, char **argv,
                                    uint64_t default_iters,
                                    uint64_t default_size) {
  hg_args args;
  int i;

  args.iters = default_iters;
  args.size = default_size;
  args.seed = 12345u;
  args.quiet = 0;

  for (i = 1; i < argc; ++i) {
    const char *a = argv[i];
    const char *eq = strchr(a, '=');
    const char *value = NULL;
    char name[32];

    if (strcmp(a, "--help") == 0 || strcmp(a, "-h") == 0) {
      hg_args defaults;
      defaults.iters = default_iters;
      defaults.size = default_size;
      defaults.seed = 12345u;
      defaults.quiet = 0;
      hg_usage(argv[0], &defaults);
      exit(0);
    }
    if (strcmp(a, "--quiet") == 0) {
      args.quiet = 1;
      continue;
    }

    if (eq != NULL) {
      size_t n = (size_t)(eq - a);
      if (n >= sizeof(name)) n = sizeof(name) - 1;
      memcpy(name, a, n);
      name[n] = '\0';
      value = eq + 1;
    } else {
      size_t n = strlen(a);
      if (n >= sizeof(name)) n = sizeof(name) - 1;
      memcpy(name, a, n);
      name[n] = '\0';
      if (i + 1 < argc) value = argv[i + 1];
    }

    if (strcmp(name, "--iters") == 0) {
      args.iters = hg_parse_u64(value, "--iters");
    } else if (strcmp(name, "--size") == 0) {
      args.size = hg_parse_u64(value, "--size");
    } else if (strcmp(name, "--seed") == 0) {
      args.seed = hg_parse_u64(value, "--seed");
    } else {
      fprintf(stderr, "FATAL: unknown option '%s'\n", a);
      exit(2);
    }
    if (eq == NULL) ++i; /* consumed the separate value argument */
  }

  if (args.iters == 0) {
    fprintf(stderr, "FATAL: --iters must be >= 1\n");
    exit(2);
  }
  if (args.size == 0) {
    fprintf(stderr, "FATAL: --size must be >= 1\n");
    exit(2);
  }
  return args;
}

/* ---------------------------------------------------------------------------
 * 8. The output contract (docs/DESIGN.md sec 4.4)
 *
 * Exactly one CHECKSUM= line, ROI markers around the measured region, exit 0.
 * Everything is flushed immediately: under gem5 a truncated stdout buffer at
 * the instruction limit would otherwise lose the markers.
 * -------------------------------------------------------------------------*/
static inline void hg_banner(const char *name, const hg_args *args,
                             uint64_t footprint_bytes) {
  if (args->quiet) return;
  printf("BENCH=%s VARIANT=%s ITERS=%llu SIZE=%llu SEED=%llu\n", name,
         hg_variant_name(), (unsigned long long)args->iters,
         (unsigned long long)args->size, (unsigned long long)args->seed);
  printf("FOOTPRINT_MIB=%.2f\n", hg_mib(footprint_bytes));
  fflush(stdout);
}

static inline void hg_roi_begin(void) {
  printf("ROI_BEGIN\n");
  fflush(stdout);
}

static inline void hg_roi_end(void) {
  printf("ROI_END\n");
  fflush(stdout);
}

/* Self-check failure: report and exit non-zero, WITHOUT printing a checksum
 * (a candidate that corrupts the computation must not look like a pass). */
static inline void hg_check_failed(const char *what) {
  printf("CHECK=FAIL %s\n", what);
  fflush(stdout);
  exit(1);
}

static inline void hg_check(int condition, const char *what) {
  if (!condition) hg_check_failed(what);
}

/* The single CHECKSUM= line.  Call exactly once, last. */
static inline void hg_finish(uint64_t checksum) {
  printf("CHECK=PASS\n");
  printf("CHECKSUM=0x%016llx\n", (unsigned long long)checksum);
  fflush(stdout);
}

#endif /* EXPERIMENTAL_USERS_SVSOOLEBHAVI_HINT_GATHER_BENCH_COMMON_H_ */
