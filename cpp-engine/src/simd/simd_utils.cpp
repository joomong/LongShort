#include "simd_utils.h"

#ifdef __x86_64__
#include <cpuid.h>
#endif

#include <cstdlib>

bool has_avx2() {
#ifdef __x86_64__
    unsigned int eax = 0, ebx = 0, ecx = 0, edx = 0;
    // CPUID leaf 7, sub-leaf 0: structured extended feature flags
    if (__get_cpuid_count(7, 0, &eax, &ebx, &ecx, &edx)) {
        return (ebx >> 5) & 1u;  // bit 5 of EBX = AVX2
    }
#endif
    return false;
}

void* aligned_alloc_32(size_t bytes) {
    if (bytes == 0) bytes = 1;
    void* ptr = nullptr;
    if (posix_memalign(&ptr, 32, bytes) != 0) return nullptr;
    return ptr;
}

void aligned_free_32(void* ptr) {
    free(ptr);
}
