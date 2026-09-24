# ===========================================================================
# Top-Level Makefile for HINT.GATHER Revised
#
# Delegates every target to ./run_all.sh so the hermetic environment
# (CONDA_PREFIX, LD_LIBRARY_PATH, HG_PLUGIN, OS/CPU detection) is always set.
# ===========================================================================

.PHONY: all setup llvm bench gem5 champsim verify run test evolve help

all:
	./run_all.sh all

setup:
	./run_all.sh setup

llvm:
	./run_all.sh llvm

bench:
	./run_all.sh bench

gem5:
	./run_all.sh gem5

champsim:
	./run_all.sh champsim

verify run test:
	./run_all.sh verify

evolve:
	./run_all.sh evolve

help:
	./run_all.sh --help
