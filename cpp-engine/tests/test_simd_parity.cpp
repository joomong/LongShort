#include <gtest/gtest.h>
#include <cstdint>
#include <cstddef>
#include <cmath>
#include <vector>

#include "simd/simd_utils.h"

// ---------------------------------------------------------------------------
// TEST: has_avx2() returns without crashing (result depends on CPU)
// ---------------------------------------------------------------------------
TEST(SIMD, HasAvx2Detection) {
    bool result = has_avx2();
    // We can't assert true/false (depends on host CPU), just verify no crash
    // and that it returns a sensible bool.
    EXPECT_TRUE(result == true || result == false);
}

// ---------------------------------------------------------------------------
// TEST: aligned_alloc_32 returns 32-byte-aligned pointer; aligned_free_32 frees it
// ---------------------------------------------------------------------------
TEST(SIMD, AlignedAllocation) {
    void* ptr = aligned_alloc_32(256);
    ASSERT_NE(ptr, nullptr);
    // Pointer must be 32-byte aligned
    EXPECT_EQ(reinterpret_cast<uintptr_t>(ptr) % 32u, 0u);
    aligned_free_32(ptr);
}

TEST(SIMD, AlignedAllocationSmall) {
    void* ptr = aligned_alloc_32(1);
    ASSERT_NE(ptr, nullptr);
    EXPECT_EQ(reinterpret_cast<uintptr_t>(ptr) % 32u, 0u);
    aligned_free_32(ptr);
}

TEST(SIMD, AlignedAllocationLarge) {
    const size_t N = 1024;
    void* ptr = aligned_alloc_32(N * sizeof(double));
    ASSERT_NE(ptr, nullptr);
    EXPECT_EQ(reinterpret_cast<uintptr_t>(ptr) % 32u, 0u);
    // Write and read back to verify the allocation is usable
    double* dp = static_cast<double*>(ptr);
    for (size_t i = 0; i < N; ++i) dp[i] = static_cast<double>(i);
    for (size_t i = 0; i < N; ++i)
        EXPECT_DOUBLE_EQ(dp[i], static_cast<double>(i));
    aligned_free_32(ptr);
}

// ---------------------------------------------------------------------------
// TEST: AlignedVector<float> is 32-byte aligned and works like std::vector
// ---------------------------------------------------------------------------
TEST(SIMD, AlignedVectorUsable) {
    AlignedVector<float> v(16, 0.0f);
    ASSERT_EQ(v.size(), 16u);
    if (!v.empty()) {
        EXPECT_EQ(reinterpret_cast<uintptr_t>(v.data()) % 32u, 0u);
    }
    for (size_t i = 0; i < v.size(); ++i) v[i] = static_cast<float>(i) * 1.5f;
    EXPECT_FLOAT_EQ(v[4], 6.0f);
}

// ---------------------------------------------------------------------------
// TEST: AlignedVector<double> alignment
// ---------------------------------------------------------------------------
TEST(SIMD, AlignedVectorDouble) {
    AlignedVector<double> v(8, 1.0);
    ASSERT_EQ(v.size(), 8u);
    if (!v.empty()) {
        EXPECT_EQ(reinterpret_cast<uintptr_t>(v.data()) % 32u, 0u);
    }
}
