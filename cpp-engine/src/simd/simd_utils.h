#pragma once

#include <cstddef>
#include <limits>
#include <new>
#include <vector>

// Runtime AVX2 detection.
bool has_avx2();

// 32-byte aligned allocation / deallocation.
void* aligned_alloc_32(size_t bytes);
void  aligned_free_32(void* ptr);

// STL-compatible allocator that returns 32-byte aligned memory.
template<typename T>
struct AlignedAllocator {
    using value_type = T;

    AlignedAllocator() = default;

    template<typename U>
    AlignedAllocator(const AlignedAllocator<U>&) noexcept {}

    T* allocate(size_t n) {
        if (n > std::numeric_limits<size_t>::max() / sizeof(T))
            throw std::bad_alloc();
        T* p = static_cast<T*>(aligned_alloc_32(n * sizeof(T)));
        if (p == nullptr)
            throw std::bad_alloc();
        return p;
    }

    void deallocate(T* p, size_t) noexcept {
        aligned_free_32(p);
    }

    template<typename U>
    bool operator==(const AlignedAllocator<U>&) const noexcept { return true; }

    template<typename U>
    bool operator!=(const AlignedAllocator<U>&) const noexcept { return false; }
};

// Convenience alias: std::vector with 32-byte aligned storage.
template<typename T>
using AlignedVector = std::vector<T, AlignedAllocator<T>>;
