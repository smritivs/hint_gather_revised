// ===========================================================================
// hg_none.h -- "no prefetching" baseline module for the HINT.GATHER project.
//
// ROLE
//   The floor of the evaluation in docs/DESIGN.md sec 5.1: a ChampSim prefetcher
//   module that observes everything and issues nothing. Build it the same way



//   CHIA builds the real module -- copy this file to
//     <champsim_root>/prefetcher/hg_none/hg_none.h
//   (i.e. pass it as ‘prefetcher_src‘ with ‘module_name="hg_none"‘).
//
//   It exists as a *module* rather than as ChampSim’s built-in "no"
//   prefetcher so that the baseline and the candidate travel through exactly
//   the same build and stats-parsing path; any difference in the numbers is
//   then attributable to the model, not to the harness.
//
//   Same API assumptions and defensive style as hint_gather_prefetcher.h.in
//   (see that file’s header for the targeted DPC4 API revision).
//
// Normative spec: ../../docs/DESIGN.md
// ===========================================================================

#ifndef HG_NONE_H_
#define HG_NONE_H_

#include <cstdint>
#include <iostream>

#if defined(__has_include)
#if __has_include("cache.h")
#include "cache.h"
#elif __has_include(<champsim/cache.h>)
#include <champsim/cache.h>
#endif
#if __has_include("modules.h")
#include "modules.h"
#define HG_NONE_HAS_MODULES 1
#elif __has_include(<champsim/modules.h>)
#include <champsim/modules.h>
#define HG_NONE_HAS_MODULES 1
#endif
#endif

#ifndef HG_NONE_HAS_MODULES
#define HG_NONE_HAS_MODULES 0
#endif

#if HG_NONE_HAS_MODULES

struct hg_none_prefetcher : public champsim::modules::prefetcher {
  using champsim::modules::prefetcher::prefetcher;

  uint64_t accesses_ = 0;
  uint64_t hits_ = 0;

  inline void prefetcher_initialize() {
    accesses_ = 0;
    hits_ = 0;
  }

  // Templated for the same reason as the main module: it binds to both the
  // uint64_t-era and the champsim::address-era signatures.
  template <typename AddrT, typename IpT, typename TypeT>
  inline uint32_t prefetcher_cache_operate(AddrT /*addr*/, IpT /*ip*/,
                                           uint8_t cache_hit,
                                           bool /*useful_prefetch*/,
                                           TypeT /*type*/,
                                           uint32_t metadata_in) {
    ++accesses_;
    if (cache_hit != 0) ++hits_;
    return metadata_in; // no prefetch is ever issued
  }

  template <typename AddrT, typename SetT, typename WayT, typename EvictT>
  inline uint32_t prefetcher_cache_fill(AddrT /*addr*/, SetT /*set*/,
                                        WayT /*way*/, uint8_t /*prefetch*/,
                                        EvictT /*evicted*/,
                                        uint32_t metadata_in) {
    return metadata_in;
  }

  inline void prefetcher_final_stats() {
    std::cout << "\n*** hg_none baseline (no prefetching) ***\n";
    std::cout << "hg_hints_seen: 0\n";
    std::cout << "hg_prefetches_issued: 0\n";
    std::cout << "hg_prefetches_dropped: 0\n";
    std::cout << "hg_phq_full_events: 0\n";
    std::cout << "hg_late_prefetches: 0\n";
    std::cout << "hg_demand_accesses: " << accesses_ << "\n";
    std::cout << "hg_demand_hits: " << hits_ << "\n";
  }
};




using hg_none = hg_none_prefetcher;
#ifdef HG_MODULE_NAME
using HG_MODULE_NAME = hg_none_prefetcher;
#endif

#endif   // HG_NONE_HAS_MODULES

#endif   // HG_NONE_H_
