# ===========================================================================
# bench/Makefile -- builds the HINT.GATHER benchmark suite.
#
# ROLE
#   Produces, for every benchmark, the three variants required by
#   docs/DESIGN.md sec 4.4:
#
#     build/<b>.base.elf   RISC-V, no software prefetching at all
#     build/<b>.swpf.elf   RISC-V, Zicbop-style software prefetch baseline
#                          (ordinary instructions -- the HONEST baseline we
#                          have to beat)
#     build/<b>.hint.elf   RISC-V, HINT.GATHER inserted by the pass plugin
#
#   plus a native x86 build used for smoke-testing and for ChampSim tracing:
#
#     build/x86/<b>.base   build/x86/<b>.swpf   build/x86/<b>.hint
#
#   (no .elf suffix -- scripts/gen_traces.sh and
#   champsim_nodes.resolve_hint_pcs expect exactly this layout).
#
# QUICK START
#   make x86                        # native, no RISC-V toolchain needed
#   make check                      # run the native binaries, compare checksums
#   make                            # all RISC-V variants (needs CC + sysroot)
#   make analyze                    # emit hint_sites.json for the ChampSim node
#   make print-config               # show every resolved variable
#
# Normative spec: ../docs/DESIGN.md (sec 4.1 pass invocation, sec 4.4 this contract).
# ===========================================================================

# --- toolchain -------------------------------------------------------------
# Auto-discover HG_TOOLS_ROOT, CONDA_PREFIX, and HG_CLANG if not already set in
# the environment so `make -C bench` works out-of-the-box in a fresh terminal.
HG_TOOLS_ROOT ?= $(if $(HG_TOOLS_DIR),$(HG_TOOLS_DIR),$(HOME)/hg-tools)
ifeq ($(strip $(CONDA_PREFIX)),)
  ifneq ($(wildcard $(HG_TOOLS_ROOT)/miniconda3/envs/hint-gather),)
    CONDA_PREFIX := $(HG_TOOLS_ROOT)/miniconda3/envs/hint-gather
  else ifneq ($(wildcard $(HOME)/miniconda3/envs/hint-gather),)
    CONDA_PREFIX := $(HOME)/miniconda3/envs/hint-gather
  endif
endif

ifneq ($(wildcard $(HG_TOOLS_ROOT)/llvm-17/bin/clang),)
  DEFAULT_CLANG := $(HG_TOOLS_ROOT)/llvm-17/bin/clang
else ifneq ($(wildcard $(HG_TOOLS_ROOT)/llvm/bin/clang),)
  DEFAULT_CLANG := $(HG_TOOLS_ROOT)/llvm/bin/clang
else ifneq ($(wildcard $(CONDA_PREFIX)/bin/clang),)
  DEFAULT_CLANG := $(CONDA_PREFIX)/bin/clang
else
  DEFAULT_CLANG := clang
endif

HG_CLANG      ?= $(DEFAULT_CLANG)
CC_X86        ?= $(HG_CLANG)
TARGET        ?= riscv64-unknown-elf
X86_TARGET    ?= $(shell uname -m)-unknown-linux-gnu

# RISC-V GCC: auto-detect from HG_TOOLS_ROOT or CONDA_PREFIX.
# For base/swpf variants we use GCC directly (handles multilib correctly).
# For the hint variant we compile with clang (for the pass plugin) and link
# with GCC -- clang's bare-metal driver doesn't handle xpack multilib.
ifneq ($(wildcard $(HG_TOOLS_ROOT)/riscv/bin/riscv-none-elf-gcc),)
  CC_RISCV      ?= $(HG_TOOLS_ROOT)/riscv/bin/riscv-none-elf-gcc
else ifneq ($(wildcard $(CONDA_PREFIX)/bin/riscv64-unknown-elf-gcc),)
  CC_RISCV      ?= $(CONDA_PREFIX)/bin/riscv64-unknown-elf-gcc
else
  CC_RISCV      ?= riscv64-unknown-elf-gcc
endif

# Ensure clang-17 and libHintGather.so find Conda's libxml2.so.2 / libtinfo.so.6 / libstdc++.so.6
export LD_LIBRARY_PATH := $(CONDA_PREFIX)/lib:$(HG_TOOLS_ROOT)/llvm-17/lib:$(LD_LIBRARY_PATH)
export PATH := $(CONDA_PREFIX)/bin:$(HG_TOOLS_ROOT)/llvm-17/bin:$(PATH)

# Out-of-tree pass plugin and the genome it reads.
HG_BUILD      ?= ../llvm/build
HG_PLUGIN     ?= $(HG_BUILD)/libHintGather.so
HG_GENOME     ?= ../genomes/default.json

# RISC-V codegen flags.
RISCV_ARCH    ?= rv64gc
RISCV_ABI     ?= lp64d

# Sysroot for clang (hint compile-to-object step only).
ifneq ($(wildcard $(CONDA_PREFIX)/riscv-tools/riscv64-unknown-elf),)
  SYSROOT       ?= $(CONDA_PREFIX)/riscv-tools/riscv64-unknown-elf
else ifneq ($(wildcard $(HG_TOOLS_ROOT)/riscv-rv64gc-sysroot),)
  SYSROOT       ?= $(HG_TOOLS_ROOT)/riscv-rv64gc-sysroot
else ifneq ($(wildcard $(HG_TOOLS_ROOT)/riscv/riscv-none-elf),)
  SYSROOT       ?= $(HG_TOOLS_ROOT)/riscv/riscv-none-elf
else
  SYSROOT       ?=
endif

# --- layout ----------------------------------------------------------------
BUILD_DIR     ?= build
X86_DIR       ?= $(BUILD_DIR)/x86
REPORT_DIR    ?= $(BUILD_DIR)/reports

# --- flags -----------------------------------------------------------------
# -std=c99, no external libraries, no libm: gem5 SE mode constraints.
OPT           ?= -O2
CFLAGS        ?= $(OPT) -std=c99 -Wall -Wextra -fno-strict-aliasing
# Lookahead of the software-prefetch baseline; keep it in step with the
# genome's hint_distance when you compare the two.
HG_SWPF_DISTANCE ?= 32
CFLAGS        += -DHG_SWPF_DISTANCE=$(HG_SWPF_DISTANCE)

# GCC flags for base/swpf RISC-V builds.
GCC_FLAGS     := -march=$(RISCV_ARCH) -mabi=$(RISCV_ABI) -static

# Clang flags for the hint variant (compile-to-object only, no linking).
CLANG_RV_FLAGS := --target=$(TARGET) -march=$(RISCV_ARCH) -mabi=$(RISCV_ABI)
ifneq ($(strip $(SYSROOT)),)
CLANG_RV_FLAGS += --sysroot=$(SYSROOT) -isystem $(SYSROOT)/include
endif

X86_FLAGS     := --target=$(X86_TARGET)
ifeq ($(HG_X86_STATIC),1)
X86_FLAGS     += -static
endif

# Pass plugin invocations (docs/DESIGN.md sec 4.1).
#   emit    -- insert the .insn; RISC-V only.
#   analyze -- detect sites, write hint_sites.json, change nothing.
#
# The x86 build MUST NOT use emit mode: x86 cannot execute custom-0, and the
# pass refuses it anyway (skip_reason: non_riscv_target).  It must still emit
# the `__hg_site_<id>` labels, because champsim_nodes.resolve_hint_pcs runs
# llvm-nm on the x86 ELF to map hint sites to trace PCs.
#
# TODO(verify): confirm the exact spelling of the label-emission flag with the
# LLVM node.  Override with `make x86 HG_LABEL_FLAGS='-mllvm -hg-...'` if it
# differs; setting HG_LABEL_FLAGS= (empty) disables it.
HG_LABEL_FLAGS ?= -mllvm -hg-emit-site-labels

# -Xclang -load loads the plugin into cc1 (and NOT into ld!) BEFORE llvm
# parses -mllvm flags, while -fpass-plugin registers the NewPM callback.
PLUGIN_BASE    = -Xclang -load -Xclang $(HG_PLUGIN) -fpass-plugin=$(HG_PLUGIN) -mllvm -hg-genome=$(HG_GENOME)
HINT_FLAGS     = $(PLUGIN_BASE) -mllvm -hg-mode=emit \
                 -mllvm -hg-report=$(REPORT_DIR)/$*.riscv.hint_sites.json
ANALYZE_FLAGS  = $(PLUGIN_BASE) -mllvm -hg-mode=analyze \
                 -mllvm -hg-report=$(REPORT_DIR)/$*.riscv.hint_sites.json
X86_PASS_FLAGS = $(PLUGIN_BASE) -mllvm -hg-mode=analyze $(HG_LABEL_FLAGS) \
                 -mllvm -hg-report=$(REPORT_DIR)/$*.x86.hint_sites.json

# --- benchmarks ------------------------------------------------------------
BENCHES        := gather bfs pagerank listchase
VARIANTS       := base swpf hint
# gen_traces.sh only needs base (+ optional swpf); building hint natively is
# free and lets `make check` compare all three checksums.
X86_VARIANTS   ?= base swpf hint

SRCS_gather    := gather.c
SRCS_bfs       := bfs.c graphgen.c
SRCS_pagerank  := pagerank.c graphgen.c
SRCS_listchase := listchase.c
HDRS           := common.h graphgen.h

DEFS_base      := -DHG_BUILD_BASE=1
DEFS_swpf      := -DHG_BUILD_SWPF=1
DEFS_hint      := -DHG_BUILD_HINT=1

RISCV_ELFS := $(foreach b,$(BENCHES),$(foreach v,$(VARIANTS),$(BUILD_DIR)/$(b).$(v).elf))
X86_BINS   := $(foreach b,$(BENCHES),$(foreach v,$(X86_VARIANTS),$(X86_DIR)/$(b).$(v)))
REPORTS    := $(foreach b,$(BENCHES),$(REPORT_DIR)/$(b).riscv.hint_sites.json)

# Default problem sizes for `make check` -- small, so the smoke test is quick.
CHECK_SIZE  ?= 65536
CHECK_ITERS ?= 1

.PHONY: all riscv x86 host check analyze clean help print-config
.DEFAULT_GOAL := all

all: riscv

riscv: $(RISCV_ELFS)

# `host` is an alias for `x86`: same binaries, same layout.
host: x86
x86: $(X86_BINS)

analyze: $(REPORTS)

# ---------------------------------------------------------------------------
# Rules.  .SECONDEXPANSION lets a pattern rule depend on the per-benchmark
# source list SRCS_<bench>.
# ---------------------------------------------------------------------------
.SECONDEXPANSION:

$(BUILD_DIR) $(X86_DIR) $(REPORT_DIR):
	mkdir -p $@

define REQUIRE_PLUGIN
	@test -f "$(HG_PLUGIN)" || { \
	  echo "ERROR: pass plugin not found: $(HG_PLUGIN)"; \
	  echo "       build llvm/ first, or override HG_PLUGIN=..."; \
	  exit 1; }
	@test -f "$(HG_GENOME)" || { \
	  echo "ERROR: genome not found: $(HG_GENOME)"; \
	  echo "       override with HG_GENOME=path/to/genome.json"; \
	  exit 1; }
endef

# --- RISC-V ----------------------------------------------------------------
# base and swpf use GCC directly (handles xpack multilib correctly).
$(BUILD_DIR)/%.base.elf: $$(SRCS_$$*) $(HDRS) | $(BUILD_DIR)
	$(CC_RISCV) $(GCC_FLAGS) $(CFLAGS) $(DEFS_base) $(SRCS_$*) -o $@

$(BUILD_DIR)/%.swpf.elf: $$(SRCS_$$*) $(HDRS) | $(BUILD_DIR)
	$(CC_RISCV) $(GCC_FLAGS) $(CFLAGS) $(DEFS_swpf) $(SRCS_$*) -o $@

# hint: compile with clang (for the pass plugin) then link with GCC.
$(BUILD_DIR)/%.hint.elf: $$(SRCS_$$*) $(HDRS) $(HG_PLUGIN) $(HG_GENOME) | $(BUILD_DIR) $(REPORT_DIR)
	$(REQUIRE_PLUGIN)
	@for src in $(SRCS_$*); do \
	  echo "$(HG_CLANG) ... -c $$src -o $(BUILD_DIR)/$*.$${src%.c}.hint.o"; \
	  $(HG_CLANG) $(CLANG_RV_FLAGS) $(CFLAGS) $(DEFS_hint) $(HINT_FLAGS) -c $$src -o $(BUILD_DIR)/$*.$${src%.c}.hint.o || exit 1; \
	done
	$(CC_RISCV) $(GCC_FLAGS) $(CFLAGS) $(DEFS_hint) $(patsubst %.c,$(BUILD_DIR)/$*.%.hint.o,$(SRCS_$*)) -o $@

$(REPORT_DIR)/%.riscv.hint_sites.json: $$(SRCS_$$*) $(HDRS) $(HG_PLUGIN) $(HG_GENOME) | $(BUILD_DIR) $(REPORT_DIR)
	$(REQUIRE_PLUGIN)
	$(CC) $(TARGET_FLAGS) $(CFLAGS) $(DEFS_base) $(ANALYZE_FLAGS) \
	    -c $(firstword $(SRCS_$*)) -o $(BUILD_DIR)/$*.analyze.o

# --- native x86 ------------------------------------------------------------
# No .elf suffix, and the pass runs in analyze mode only, so no .insn is
# emitted on a target that cannot execute custom-0.  If the plugin is absent
# the binary is still built (without site labels), so `make check` works on a
# bare machine.
X86_PASS = $(if $(wildcard $(HG_PLUGIN)),$(if $(wildcard $(HG_GENOME)),$(X86_PASS_FLAGS),),)

$(X86_DIR)/%.base: $$(SRCS_$$*) $(HDRS) | $(X86_DIR) $(REPORT_DIR)
	$(CC_X86) $(X86_FLAGS) $(CFLAGS) $(DEFS_base) $(X86_PASS) $(SRCS_$*) -o $@

$(X86_DIR)/%.swpf: $$(SRCS_$$*) $(HDRS) | $(X86_DIR) $(REPORT_DIR)
	$(CC_X86) $(X86_FLAGS) $(CFLAGS) $(DEFS_swpf) $(X86_PASS) $(SRCS_$*) -o $@

# On x86 the HINT_GATHER macros expand to a NOP (docs/DESIGN.md sec 1.3 and
# bench/common.h), so this variant exists only to prove the hint build path
# compiles and produces the same checksum.
$(X86_DIR)/%.hint: $$(SRCS_$$*) $(HDRS) | $(X86_DIR) $(REPORT_DIR)
	$(CC_X86) $(X86_FLAGS) $(CFLAGS) $(DEFS_hint) $(X86_PASS) $(SRCS_$*) -o $@

# ---------------------------------------------------------------------------
# Smoke test: the native binaries must agree on the checksum across variants.
# This is the local, cheap version of the correctness gate in
# docs/DESIGN.md sec 4.4 (which runs the RISC-V builds under gem5 atomic mode).
# ---------------------------------------------------------------------------
check: x86
	@status=0; \
	for b in $(BENCHES); do \
	  ref=""; \
	  for v in $(X86_VARIANTS); do \
	    bin="$(X86_DIR)/$$b.$$v"; \
	    out=$$("$$bin" --size $(CHECK_SIZE) --iters $(CHECK_ITERS) 2>&1) || { \
	      echo "FAIL $$b.$$v: non-zero exit"; echo "$$out"; status=1; continue; }; \
	    echo "$$out" | grep -q '^ROI_BEGIN$$' || { echo "FAIL $$b.$$v: no ROI_BEGIN"; status=1; }; \
	    echo "$$out" | grep -q '^ROI_END$$'   || { echo "FAIL $$b.$$v: no ROI_END";   status=1; }; \
	    n=$$(echo "$$out" | grep -c '^CHECKSUM='); \
	    [ "$$n" = "1" ] || { echo "FAIL $$b.$$v: $$n CHECKSUM lines (want exactly 1)"; status=1; }; \
	    sum=$$(echo "$$out" | grep '^CHECKSUM=' | head -n 1); \
	    if [ -z "$$ref" ]; then ref="$$sum"; fi; \
	    if [ "$$sum" != "$$ref" ]; then \
	      echo "FAIL $$b.$$v: $$sum != $$ref (base)"; status=1; \
	    else \
	      echo "ok   $$b.$$v  $$sum"; \
	    fi; \
	  done; \
	done; \
	exit $$status

clean:
	rm -rf $(BUILD_DIR)

print-config:
	@echo "HG_CLANG        = $(HG_CLANG)"
	@echo "CC              = $(CC)"
	@echo "CC_X86          = $(CC_X86)"
	@echo "TARGET          = $(TARGET)"
	@echo "X86_TARGET      = $(X86_TARGET)"
	@echo "RISCV_ARCH/ABI  = $(RISCV_ARCH) / $(RISCV_ABI)"
	@echo "SYSROOT         = $(SYSROOT)"
	@echo "GCC_TOOLCHAIN   = $(GCC_TOOLCHAIN)"
	@echo "HG_PLUGIN       = $(HG_PLUGIN)"
	@echo "HG_GENOME       = $(HG_GENOME)"
	@echo "HG_LABEL_FLAGS  = $(HG_LABEL_FLAGS)"
	@echo "CFLAGS          = $(CFLAGS)"
	@echo "TARGET_FLAGS    = $(TARGET_FLAGS)"
	@echo "X86_FLAGS       = $(X86_FLAGS)"
	@echo "BUILD_DIR       = $(BUILD_DIR)"
	@echo "X86_DIR         = $(X86_DIR)"
	@echo "REPORT_DIR      = $(REPORT_DIR)"
	@echo "BENCHES         = $(BENCHES)"
	@echo "X86_VARIANTS    = $(X86_VARIANTS)"

help:
	@echo "targets:"
	@echo "  all | riscv   build/<b>.{base,swpf,hint}.elf   (needs a RISC-V clang)"
	@echo "  x86 | host    build/x86/<b>.{base,swpf,hint}   (native smoke test + tracing)"
	@echo "  analyze       build/reports/<b>.riscv.hint_sites.json"
	@echo "  check         run the native binaries and compare checksums"
	@echo "  print-config  show the resolved toolchain variables"
	@echo "  clean         remove $(BUILD_DIR)"
	@echo ""
	@echo "common overrides:"
	@echo "  make riscv CC=/path/to/clang SYSROOT=/path/to/riscv64-unknown-elf"
	@echo "  make riscv HG_PLUGIN=../llvm/build/libHintGather.so HG_GENOME=g.json"
	@echo "  make x86   HG_LABEL_FLAGS='-mllvm -hg-emit-site-labels'"
	@echo "  make check CHECK_SIZE=4096 CHECK_ITERS=1"
