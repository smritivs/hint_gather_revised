#ifndef HGPROF_RUNTIME_H
#define HGPROF_RUNTIME_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

void __hg_profile_access(uint32_t site_id, uint64_t addr);
void __hg_profile_reset(void);
void __hg_profile_dump(void);

#ifdef __cplusplus
}
#endif

#endif
