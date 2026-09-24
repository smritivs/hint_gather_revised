# CMakeLists.txt - builds libHintGather.so, the out-of-tree HINT.GATHER pass
# plugin, against an external (prebuilt) LLVM.
#
# Role: this is the *contract* build for the LLVM node of the HINT.GATHER CHIA
# project.  See ../docs/DESIGN.md section 4.1 for how the resulting plugin is
# invoked, and README.md in this directory for the exact commands.
#
#   cmake -S llvm -B llvm/build -DLLVM_DIR=$LLVM_HOME/lib/cmake/llvm \
#         -DCMAKE_BUILD_TYPE=Release
#   cmake --build llvm/build -j
#
# Produces:
#   llvm/build/libHintGather.so   the pass plugin (-fpass-plugin=...)
#   llvm/build/libhgprof.a        host build of the profile runtime (optional)

cmake_minimum_required(VERSION 3.20)
project(HintGather LANGUAGES C CXX)

find_package(LLVM REQUIRED CONFIG)

message(STATUS "HintGather: using LLVM ${LLVM_PACKAGE_VERSION}")
message(STATUS "HintGather: LLVMConfig.cmake in ${LLVM_DIR}")

if(LLVM_VERSION_MAJOR LESS 17)
  message(WARNING
    "HintGather targets the LLVM 17-20 API surface; ${LLVM_VERSION_MAJOR} is "
    "older and is not expected to compile (opaque pointers, TypeSize API).")
endif()
if(LLVM_VERSION_MAJOR GREATER 20)
  message(WARNING
    "HintGather targets the LLVM 17-20 API surface; ${LLVM_VERSION_MAJOR} is "
    "newer. Expect Module::getTargetTriple() to return Triple instead of "
    "std::string (see the TODO in HintGatherPass.cpp).")
endif()

list(APPEND CMAKE_MODULE_PATH "${LLVM_CMAKE_DIR}")

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "" FORCE)
endif()

# LLVM is normally built with RTTI off; a plugin that disagrees fails to load
# with confusing dynamic-linker errors, so mirror whatever LLVM used.
if(NOT LLVM_ENABLE_RTTI)
  add_compile_options(-fno-rtti)
endif()
if(NOT LLVM_ENABLE_EH)
  add_compile_options(-fno-exceptions)
endif()

add_library(HintGather MODULE
  HintGatherPass.cpp
)

target_include_directories(HintGather SYSTEM PRIVATE ${LLVM_INCLUDE_DIRS})
target_include_directories(HintGather PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)

separate_arguments(HG_LLVM_DEFINITIONS NATIVE_COMMAND "${LLVM_DEFINITIONS}")
target_compile_definitions(HintGather PRIVATE ${HG_LLVM_DEFINITIONS})

# Always libHintGather.so, on every platform, because DESIGN.md section 4.1
# hard-codes that name in the clang command line.
set_target_properties(HintGather PROPERTIES
  PREFIX "lib"
  SUFFIX ".so"
  CXX_VISIBILITY_PRESET hidden
)

# A pass plugin resolves LLVM symbols from the host process (clang/opt), so it
# must NOT link libLLVM. On macOS the linker needs to be told explicitly.
if(APPLE)
  target_link_options(HintGather PRIVATE -Wl,-undefined,dynamic_lookup)
endif()

# ---------------------------------------------------------------------------
# Profile runtime.  This host build exists so the runtime is at least compiled
# and warning-checked by CI; the benchmarks normally cross-compile the .c
# directly into the RISC-V binary (see bench/ and README.md).
# ---------------------------------------------------------------------------
option(HG_BUILD_RUNTIME "Build a host copy of the profile runtime" ON)
if(HG_BUILD_RUNTIME)
  add_library(hgprof STATIC runtime/hgprof_runtime.c)
  target_include_directories(hgprof PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/runtime)
  set_target_properties(hgprof PROPERTIES C_STANDARD 99)
  target_compile_options(hgprof PRIVATE -Wall -Wextra)
endif()

# ---------------------------------------------------------------------------
# Test gate: `ctest` runs the analyze-mode assertion used by the CHIA loop.
# ---------------------------------------------------------------------------
enable_testing()
add_test(NAME hg_analyze_gate
         COMMAND ${CMAKE_COMMAND} -E env
                 HG_PLUGIN=$<TARGET_FILE:HintGather>
                 bash ${CMAKE_CURRENT_SOURCE_DIR}/test/run_analyze_test.sh)
