#include <gtest/gtest.h>
#include <cmath>
#include <vector>
#include "metrics/yearly_metrics.h"

TEST(YearlyMetrics, SingleYear) {
    // 5 bars in 2024, timestamps in epoch_ms
    // 2024-01-01 00:00:00 UTC = 1704067200000 ms
    int64_t base = 1704067200000LL;
    int64_t day_ms = 86400000LL;

    std::vector<int64_t> timestamps = {
        base, base + day_ms, base + 2*day_ms, base + 3*day_ms, base + 4*day_ms
    };
    // First bar is 0 (will be skipped by compute_yearly)
    std::vector<double> ret = {0.0, 0.01, -0.005, 0.02, 0.015};
    std::vector<double> turnover = {0.0, 0.5, 0.3, 0.4, 0.2};
    std::vector<double> long_weight = {0.0, 0.5, 0.5, 0.5, 0.5};
    std::vector<double> short_weight = {0.0, 0.5, 0.5, 0.5, 0.5};

    auto yearly = compute_yearly(timestamps, ret, turnover, long_weight, short_weight);

    ASSERT_EQ(yearly.size(), 1u);
    EXPECT_EQ(yearly[0].year, 2024);
    EXPECT_GT(yearly[0].pnl_pct, 0.0);  // Positive PnL
    EXPECT_GT(yearly[0].sharpe, 0.0);
}

TEST(YearlyMetrics, MultiYear) {
    // Bars spanning 2023 and 2024
    // 2023-12-30 = 1703894400000, 2023-12-31 = 1703980800000
    // 2024-01-01 = 1704067200000, 2024-01-02 = 1704153600000
    std::vector<int64_t> timestamps = {
        1703894400000LL, 1703980800000LL,
        1704067200000LL, 1704153600000LL
    };
    std::vector<double> ret = {0.0, 0.01, 0.02, -0.01};
    std::vector<double> turnover = {0.0, 0.5, 0.5, 0.5};
    std::vector<double> long_weight = {0.0, 0.5, 0.5, 0.5};
    std::vector<double> short_weight = {0.0, 0.5, 0.5, 0.5};

    auto yearly = compute_yearly(timestamps, ret, turnover, long_weight, short_weight);

    ASSERT_EQ(yearly.size(), 2u);
    EXPECT_EQ(yearly[0].year, 2023);
    EXPECT_EQ(yearly[1].year, 2024);
}
