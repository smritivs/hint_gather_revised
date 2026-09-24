/* hint_gather.h - HINT.GATHER instruction encoding + emission macros.
 *
 * Role: the single normative C-level definition of the HINT.GATHER custom-0
 * instruction encoding.  Both hand-written benchmark code and the LLVM pass
 * (llvm/HintGatherPass.cpp) emit *exactly* the asm template below; if you
 * change anything here you must change the pass to match.
 *
 * Normative spec: docs/DESIGN.md sections 1.1 (encoding), 1.2 (variants),
 * 1.3 (architectural semantics) and 4.1 (emission macro).
 *
 * Portability contract: on RISC-V targets the macros expand to a single
 * `.insn r` custom-0 instruction.  On every other target they expand to a
 * NOP (`(void)0`, with all operands cast to void so no -Wunused fires), so
 * the *same* benchmark source compiles and runs on an x86 host for
 * functional testing.  This is safe because DESIGN.md section 1.3 item 4
 * makes removing every HINT.GATHER architecturally unobservable.
 *
 * Must compile clean with `clang/gcc --target=riscv64-* -O2 -Wall -Wextra`.
 */

#ifndef HINT_GATHER_INCLUDE_HINT_GATHER_H_
#define HINT_GATHER_INCLUDE_HINT_GATHER_H_

#ifdef __cplusplus
extern "C" {
#endif

/* ------------------------------------------------------------------ */
/* Field values (DESIGN.md section 1.1)                               */
/* ------------------------------------------------------------------ */

/* funct3 bit 0 - VARIANT */
#define HG_VARIANT_VALUE 0 /* HINT.GATHER   : rs2 = loaded index value  */
#define HG_VARIANT_CHASE 1 /* HINT.GATHER.C : rs2 = &B[i+d] (an address) */

/* funct3 bit 1 - LEVEL */
#define HG_LEVEL_L1D 0
#define HG_LEVEL_L2C 1

/* funct3 bit 2 - DROP */
#define HG_DROP_NEVER 0 /* non-droppable (still best-effort) */
#define HG_DROP_OK 1    /* droppable under MSHR pressure     */

/* custom-0 opcode. Kept as a macro for documentation; the asm templates
 * below spell 0x0b literally because `.insn` needs a literal there. */
#define HG_OPCODE_CUSTOM0 0x0b

/* funct3 = [0] variant | [1] level | [2] drop   (LSB = bit 0). */
#define HG_FUNCT3(variant, level, drop)                                    \
  ((((variant) & 0x1) << 0) | (((level) & 0x1) << 1) | (((drop) & 0x1) << 2))

/* funct7 = [1:0] shift | [4:2] fanout | [6:5] reserved (must be 0).
 *
 * NOTE on `fanout`: this macro takes the *encoded* 3-bit field, and the
 * hardware issues `fanout + 1` prefetches (DESIGN.md section 1.1).  The
 * genome key `fanout` is 1..8, so callers pass HG_FANOUT_FIELD(genome_fanout).
 */
#define HG_FUNCT7(shift, fanout)                                           \
  ((((shift) & 0x3) << 0) | (((fanout) & 0x7) << 2))

/* Genome fanout (1..8) -> encoded funct7 FANOUT field (0..7). */
#define HG_FANOUT_FIELD(genome_fanout) (((genome_fanout) - 1) & 0x7)

/* Element size in bytes (1,2,4,8) -> funct7 SHIFT field (0..3). */
#define HG_SHIFT_FOR_SIZE(sz)                                              \
  ((sz) == 8 ? 3 : ((sz) == 4 ? 2 : ((sz) == 2 ? 1 : 0)))

/* ------------------------------------------------------------------ */
/* Emission                                                            */
/* ------------------------------------------------------------------ */

#if defined(__riscv)

/* Variant 0 - value form.  rs2 holds the already-loaded index value
 * B[i+d]; the PHQ computes base + (value << shift).  See DESIGN.md 1.2. */
#define HINT_GATHER_V(base, index_value, shift, fanout, level, drop)       \
  __asm__ volatile(".insn r 0x0b, %2, %3, x0, %0, %1"                      \
                   ::"r"(base), "r"(index_value),                          \
                     "i"(HG_FUNCT3(HG_VARIANT_VALUE, level, drop)),        \
                     "i"(HG_FUNCT7(shift, fanout)))

/* Variant 1 - chase form.  rs2 holds the *address* &B[i+d]; the PHQ loads
 * it itself and then prefetches base + (value << shift).  See DESIGN.md
 * 1.2.  This is the form spelled out verbatim in DESIGN.md section 4.1. */
#define HINT_GATHER_C(base, index_addr, shift, fanout, level, drop)        \
  __asm__ volatile(".insn r 0x0b, %2, %3, x0, %0, %1"                      \
                   ::"r"(base), "r"(index_addr),                           \
                     "i"(HG_FUNCT3(HG_VARIANT_CHASE, level, drop)),        \
                     "i"(HG_FUNCT7(shift, fanout)))

/* Emit a local label immediately before the following statement, so the PC
 * of the demand load can be recovered with nm/objdump.  Mirrors what
 * HintGatherPass.cpp emits in `emit` mode (DESIGN.md section 4.1). */
#define HG_SITE_LABEL(name)                                                \
  __asm__ volatile(".ifndef " #name "\n" #name ":\n.endif\n")

#else /* !__riscv - host build: the hint must be a pure NOP. */

#define HINT_GATHER_V(base, index_value, shift, fanout, level, drop)       \
  ((void)(base), (void)(index_value), (void)(shift), (void)(fanout),       \
   (void)(level), (void)(drop), (void)0)

#define HINT_GATHER_C(base, index_addr, shift, fanout, level, drop)        \
  ((void)(base), (void)(index_addr), (void)(shift), (void)(fanout),        \
   (void)(level), (void)(drop), (void)0)

#define HG_SITE_LABEL(name) ((void)0)

#endif /* __riscv */

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* HINT_GATHER_INCLUDE_HINT_GATHER_H_ */
