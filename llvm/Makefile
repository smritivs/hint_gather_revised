# Fallback Makefile using llvm-config
# See README.md

LLVM_CONFIG ?= llvm-config

CXX := $(shell $(LLVM_CONFIG) --bindir)/clang++
CXXFLAGS := $(shell $(LLVM_CONFIG) --cxxflags) -fPIC -O2 -Wall -Wextra -std=c++17 -fno-rtti -fno-exceptions -I./include
LDFLAGS := $(shell $(LLVM_CONFIG) --ldflags) -shared

ifeq ($(shell uname -s),Darwin)
	LDFLAGS += -Wl,-undefined,dynamic_lookup
endif

libHintGather.so: HintGatherPass.cpp
	$(CXX) $(CXXFLAGS) $< $(LDFLAGS) -o $@

clean:
	rm -f libHintGather.so
