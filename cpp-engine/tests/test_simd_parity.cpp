#include <gtest/gtest.h>
#include <cstdint>
#include <cstddef>
#include <cmath>
#include <limits>
#include <vector>

#include "simd/simd_utils.h"
#include "simd/m_decay_avx2.h"

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

TEST(SIMD, AlignedAllocatorThrowsBadAllocOnOverflow) {
    AlignedAllocator<float> alloc;
    const size_t too_large = std::numeric_limits<size_t>::max() / sizeof(float) + 1u;
    EXPECT_THROW(
        {
            const auto* p = alloc.allocate(too_large);
            (void)p;
        },
        std::bad_alloc);
}

TEST(SIMD, MDecayProcess8Avx2Parity) {
    if (!mdecay_runtime_avx2_enabled())
        GTEST_SKIP() << "AVX2 kernel not enabled on this host/toolchain";

    const int days = 5;
    const size_t n = 8;

    std::vector<float> curr = {
        1.0f, -2.0f, std::numeric_limits<float>::quiet_NaN(), 4.5f,
        std::numeric_limits<float>::infinity(), -1.25f, 0.0f, 3.0f
    };
    std::vector<float> past = {
        0.5f, std::numeric_limits<float>::quiet_NaN(), -3.0f, 2.0f,
        -std::numeric_limits<float>::infinity(), 7.0f, 1.0f, 0.0f
    };

    std::vector<int> weights_a = {10, 9, 8, 7, 6, 5, 4, 3};
    std::vector<int> num_a     = { 2, 2, 2, 2, 2, 2, 2, 2};
    std::vector<double> sum_a  = {1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5};
    std::vector<double> diff_a = {0.5, 0.4, 0.3, 0.2, 0.1, 0.0, -0.1, -0.2};
    std::vector<float> out_a(n, 0.0f);

    std::vector<int> weights_b = weights_a;
    std::vector<int> num_b = num_a;
    std::vector<double> sum_b = sum_a;
    std::vector<double> diff_b = diff_a;
    std::vector<float> out_b(n, 0.0f);

    std::vector<double> div(256, 0.0);
    for (size_t i = 1; i < div.size(); ++i)
        div[i] = 1.0 / static_cast<double>(i);

    mdecay_process_8_scalar(
        0, days, curr.data(), past.data(),
        weights_a.data(), num_a.data(),
        sum_a.data(), diff_a.data(), div.data(), div.size(), out_a.data());

    mdecay_process_8_avx2(
        0, days, curr.data(), past.data(),
        weights_b.data(), num_b.data(),
        sum_b.data(), diff_b.data(), div.data(), div.size(), out_b.data());

    for (size_t i = 0; i < n; ++i) {
        EXPECT_EQ(weights_a[i], weights_b[i]) << "weights mismatch at lane " << i;
        EXPECT_EQ(num_a[i], num_b[i]) << "num mismatch at lane " << i;
        EXPECT_DOUBLE_EQ(sum_a[i], sum_b[i]) << "sum mismatch at lane " << i;
        EXPECT_DOUBLE_EQ(diff_a[i], diff_b[i]) << "diff mismatch at lane " << i;

        if (std::isnan(out_a[i])) {
            EXPECT_TRUE(std::isnan(out_b[i])) << "output mismatch at lane " << i;
        } else {
            EXPECT_FLOAT_EQ(out_a[i], out_b[i]) << "output mismatch at lane " << i;
        }
    }
}
