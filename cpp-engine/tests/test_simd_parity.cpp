#include <gtest/gtest.h>
#include <cstdint>
#include <cstddef>
#include <cmath>
#include <limits>
#include <vector>

#include "modules/decay_kernel.h"

// ---------------------------------------------------------------------------
TEST(SIMD, Avx2SelectionMatchesBuild) {
#if defined(__AVX2__)
    EXPECT_TRUE(mdecay_avx2_enabled());
#else
    EXPECT_FALSE(mdecay_avx2_enabled());
#endif
}

TEST(SIMD, MDecayProcess8KernelParity) {
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
