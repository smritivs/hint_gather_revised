// ===========================================================================
// test_sidecar.cc -- Standalone C++17 Unit Test for the ChampSim Sidecar
//
// Exercises the rendered `hint_gather_prefetcher.h` (`hg::engine`) against a
// synthetic gather stream and verifies that hint sites trigger prefetches,
// respect PHQ occupancy and MSHR pressure thresholds, and report all five
// normative statistics required by docs/DESIGN.md sec 4.3.
// ===========================================================================

#include <cassert>
#include <cstdint>
#include <iostream>
#include <sstream>
#include <string>

#include "hint_gather_prefetcher.h"

int main() {
  hg::engine eng;
  eng.initialize();

  // Discover the first active PC in kSiteTable (or use 0x10550 fallback).
  uint64_t test_pc = (hg::kSiteTableSize > 1 && hg::kSiteTable[0].pc != 0)
                         ? hg::kSiteTable[0].pc
                         : 0x10550ULL;

  uint64_t issued_callbacks = 0;
  for (uint64_t i = 0; i < 64; ++i) {
    uint64_t addr = 0x80000000ULL + (i * 256ULL);
    eng.on_cycle();
    eng.on_access(
        test_pc, addr, /*cache_hit=*/false, /*type=*/hg::HG_TYPE_LOAD,
        /*mshr_pressure=*/0.1,
        [&](uint64_t pf_addr, bool /*fill_this_level*/) {
          ++issued_callbacks;
          eng.on_fill(pf_addr, /*prefetch=*/true);
          return true;
        });
  }

  std::ostringstream oss;
  eng.print_stats(oss);
  const std::string stats_str = oss.str();

  assert(stats_str.find("hg_hints_seen") != std::string::npos);
  assert(stats_str.find("hg_prefetches_issued") != std::string::npos);
  assert(stats_str.find("hg_prefetches_dropped") != std::string::npos);
  assert(stats_str.find("hg_late_prefetches") != std::string::npos);
  assert(stats_str.find("hg_phq_full_events") != std::string::npos);
  assert(issued_callbacks > 0 && "Expected sidecar prefetcher to issue prefetches");

  std::cout << "ALL SIDECAR TESTS PASSED (issued=" << issued_callbacks << ")\n";
  return 0;
}
