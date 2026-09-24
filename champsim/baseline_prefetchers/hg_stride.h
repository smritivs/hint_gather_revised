// ===========================================================================
// hg_stride.h -- IP-stride hardware baseline for the HINT.GATHER project.
//
// ROLE
//   The "hardware already does this" baseline of docs/DESIGN.md: a classic
//   per-PC (IP) stride prefetcher. It is the control that makes the central
//   claim falsifiable -- HINT.GATHER is only interesting on access streams a
//   stride prefetcher *cannot* follow, which is exactly why the LLVM pass
//   rejects sites whose SCEV is an affine AddRecExpr (docs/DESIGN.md sec 4.1).
//   On the ‘gather‘ / ‘bfs‘ / ‘pagerank‘ / ‘listchase‘ indirections this
//   module should recover very little; on the sequential index array it
//   should recover nearly everything.
//
//   Build it exactly like the candidate module -- copy to
//     <champsim_root>/prefetcher/hg_stride/hg_stride.h
//   (pass as ‘prefetcher_src‘ with ‘module_name="hg_stride"‘).
//
//   Algorithm: 256-entry direct-mapped IP table of {last_addr, stride,
//   confidence}. A prefetch is issued only after the same stride has been
//   confirmed twice, for HG_STRIDE_DEGREE lines ahead.
//
//   Same API assumptions and defensive style as hint_gather_prefetcher.h.in
//   (see that file’s header for the targeted DPC4 API revision).
//
// Normative spec: ../../docs/DESIGN.md
// ===========================================================================

#ifndef HG_STRIDE_H_
#define HG_STRIDE_H_

#include <cstdint>
#include <iostream>
#include <type_traits>

#if defined(__has_include)
#if __has_include("cache.h")
#include "cache.h"
#elif __has_include(<champsim/cache.h>)
#include <champsim/cache.h>
#endif
#if __has_include("modules.h")
#include "modules.h"
#define HG_STRIDE_HAS_MODULES 1
#elif __has_include(<champsim/modules.h>)
#include <champsim/modules.h>
#define HG_STRIDE_HAS_MODULES 1
#endif
#endif

#ifndef HG_STRIDE_HAS_MODULES
#define HG_STRIDE_HAS_MODULES 0
#endif

#ifndef HG_STRIDE_TABLE_BITS
#define HG_STRIDE_TABLE_BITS 8
#endif
#ifndef HG_STRIDE_DEGREE
#define HG_STRIDE_DEGREE 2
#endif
#ifndef HG_STRIDE_LOG2_BLOCK
#define HG_STRIDE_LOG2_BLOCK 6
#endif

namespace hg_stride_detail {

template <int N>
struct prio : prio<N - 1> {};
template <>
struct prio<0> {};

template <typename T>
inline auto to_u64_impl(const T& a, prio<3>) -> decltype(a.template to<uint64_t>()) {



  return a.template to<uint64_t>();
}
template <typename T>
inline auto to_u64_impl(const T& a, prio<2>)
    -> decltype(static_cast<uint64_t>(a.to_underlying())) {
  return static_cast<uint64_t>(a.to_underlying());
}
template <typename T>
inline typename std::enable_if<std::is_integral<T>::value, uint64_t>::type
to_u64_impl(const T& a, prio<1>) {
  return static_cast<uint64_t>(a);
}
template <typename T>
inline uint64_t to_u64(const T& a) {
  return to_u64_impl(a, prio<3>{});
}

template <typename T>
inline auto from_u64_impl(uint64_t v, prio<3>) -> decltype(T{v}) {
  return T{v};
}
template <typename T>
inline auto from_u64_impl(uint64_t v, prio<2>)
    -> decltype(T{typename T::underlying_type{v}}) {
  return T{typename T::underlying_type{v}};
}
template <typename T>
inline T from_u64_impl(uint64_t v, prio<1>) {
  return static_cast<T>(v);
}
template <typename T>
inline T from_u64(uint64_t v) {
  return from_u64_impl<T>(v, prio<3>{});
}

template <typename T>
inline typename std::enable_if<std::is_enum<T>::value, int>::type to_int(T v) {
  return static_cast<int>(v);
}
template <typename T>
inline typename std::enable_if<std::is_integral<T>::value, int>::type to_int(T v) {
  return static_cast<int>(v);
}

}   // namespace hg_stride_detail

#if HG_STRIDE_HAS_MODULES

struct hg_stride_prefetcher : public champsim::modules::prefetcher {
  using champsim::modules::prefetcher::prefetcher;

    struct ip_entry {
      uint64_t tag = 0;
      uint64_t last_addr = 0;
      int64_t stride = 0;
      uint8_t confidence = 0;
      bool valid = false;
    };

    static constexpr unsigned kEntries = 1u << (HG_STRIDE_TABLE_BITS);
    ip_entry table_[kEntries] = {};
    uint64_t issued_ = 0;
    uint64_t accesses_ = 0;
    uint64_t trained_ = 0;

    inline void prefetcher_initialize() {
      for (unsigned i = 0; i < kEntries; ++i) table_[i] = ip_entry();
      issued_ = 0;
      accesses_ = 0;
      trained_ = 0;
    }

    // See hint_gather_prefetcher.h.in: templated so it binds to both the
    // uint64_t-era and champsim::address-era signatures.
    template <typename AddrT>
    inline auto hg_issue(AddrT addr, bool fill, uint32_t md,
                         hg_stride_detail::prio<3>)
        -> decltype(void(this->prefetch_line(addr, fill, md)), bool{}) {
      return static_cast<bool>(this->prefetch_line(addr, fill, md));
    }
    template <typename AddrT>
    inline auto hg_issue(AddrT addr, bool fill, uint32_t md,
                         hg_stride_detail::prio<2>)
        -> decltype(void(this->prefetch_line(addr, addr, addr, fill, md)), bool{}) {
      return static_cast<bool>(this->prefetch_line(addr, addr, addr, fill, md));
    }



  template <typename AddrT>
  inline bool hg_issue(AddrT, bool, uint32_t, hg_stride_detail::prio<1>) {
    return false;
  }

  template <typename AddrT, typename IpT, typename TypeT>
  inline uint32_t prefetcher_cache_operate(AddrT addr, IpT ip,
                                           uint8_t /*cache_hit*/,
                                           bool /*useful_prefetch*/,
                                           TypeT type, uint32_t metadata_in) {
    if (hg_stride_detail::to_int(type) != 0) return metadata_in; // loads only
    ++accesses_;

      const uint64_t ip_u64 = hg_stride_detail::to_u64(ip);
      const uint64_t addr_u64 = hg_stride_detail::to_u64(addr);
      ip_entry& e = table_[(ip_u64 >> 2) & (kEntries - 1u)];

      if (!e.valid || e.tag != ip_u64) {
        e = ip_entry();
        e.valid = true;
        e.tag = ip_u64;
        e.last_addr = addr_u64;
        return metadata_in;
      }

      const int64_t stride = static_cast<int64_t>(addr_u64) -
                             static_cast<int64_t>(e.last_addr);
      e.last_addr = addr_u64;
      if (stride == 0) return metadata_in;

      if (stride == e.stride) {
        if (e.confidence < 3) ++e.confidence;
      } else {
        e.stride = stride;
        e.confidence = 0;
        return metadata_in; // retrain before trusting the new stride
      }
      if (e.confidence < 2) return metadata_in;
      ++trained_;

      for (int d = 1; d <= (HG_STRIDE_DEGREE); ++d) {
        const uint64_t pf =
            static_cast<uint64_t>(static_cast<int64_t>(addr_u64) + stride * d);
        // Do not cross a 4 KB page: a real stride prefetcher has no translation.
        if ((pf >> 12) != (addr_u64 >> 12)) break;
        if (hg_issue(hg_stride_detail::from_u64<AddrT>(pf), true, 0u,
                     hg_stride_detail::prio<3>{})) {
          ++issued_;
        }
      }
      return metadata_in;
  }

  template <typename AddrT, typename SetT, typename WayT, typename EvictT>
  inline uint32_t prefetcher_cache_fill(AddrT /*addr*/, SetT /*set*/,
                                        WayT /*way*/, uint8_t /*prefetch*/,
                                        EvictT /*evicted*/,
                                        uint32_t metadata_in) {
    return metadata_in;
  }

  inline void prefetcher_final_stats() {
    std::cout << "\n*** hg_stride baseline (IP-stride) ***\n";
    std::cout << "hg_hints_seen: 0\n";
    std::cout << "hg_prefetches_issued: " << issued_ << "\n";
    std::cout << "hg_prefetches_dropped: 0\n";
    std::cout << "hg_phq_full_events: 0\n";
    std::cout << "hg_late_prefetches: 0\n";
    std::cout << "hg_stride_demand_loads: " << accesses_ << "\n";
    std::cout << "hg_stride_trained_events: " << trained_ << "\n";
    std::cout << "hg_stride_degree: " << (HG_STRIDE_DEGREE) << "\n";
  }
};

using hg_stride = hg_stride_prefetcher;
#ifdef HG_MODULE_NAME
using HG_MODULE_NAME = hg_stride_prefetcher;
#endif

#endif   // HG_STRIDE_HAS_MODULES

#endif   // HG_STRIDE_H_
