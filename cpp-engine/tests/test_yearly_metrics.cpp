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

TEST(YearlyMetrics, PnlAdditive) {
    // pnl_pct = sum(daily_ret) * 100
    // 4 bars in 2024: returns = {0.01, -0.005, 0.02, 0.015}
    // sum = 0.04, pnl_pct = 4.0
    int64_t base = 1704067200000LL;
    int64_t day_ms = 86400000LL;

    std::vector<int64_t> timestamps = {
        base, base + day_ms, base + 2*day_ms, base + 3*day_ms, base + 4*day_ms
    };
    std::vector<double> ret = {0.0, 0.01, -0.005, 0.02, 0.015};
    std::vector<double> turnover = {0.0, 0.1, 0.1, 0.1, 0.1};
    std::vector<double> lw = {0.0, 0.5, 0.5, 0.5, 0.5};
    std::vector<double> sw = {0.0, 0.5, 0.5, 0.5, 0.5};

    auto yearly = compute_yearly(timestamps, ret, turnover, lw, sw);

    ASSERT_EQ(yearly.size(), 1u);
    // sum(0.01, -0.005, 0.02, 0.015) = 0.04 -> 4.0%
    EXPECT_NEAR(yearly[0].pnl_pct, 4.0, 1e-10);
}

TEST(YearlyMetrics, LongShortSMPerYear) {
    // long_sm/short_sm are per-year means
    int64_t base = 1704067200000LL;
    int64_t day_ms = 86400000LL;

    std::vector<int64_t> timestamps = {
        base, base + day_ms, base + 2*day_ms, base + 3*day_ms
    };
    std::vector<double> ret = {0.0, 0.01, 0.02, 0.03};
    std::vector<double> turnover = {0.0, 0.1, 0.1, 0.1};
    // long_weight: [skip, 0.6, 0.7, 0.8], short_weight: [skip, 0.4, 0.3, 0.2]
    std::vector<double> lw = {0.0, 0.6, 0.7, 0.8};
    std::vector<double> sw = {0.0, 0.4, 0.3, 0.2};

    auto yearly = compute_yearly(timestamps, ret, turnover, lw, sw);

    ASSERT_EQ(yearly.size(), 1u);
    // long_sm = mean(0.6, 0.7, 0.8) = 0.7
    EXPECT_NEAR(yearly[0].long_sm, 0.7, 1e-10);
    // short_sm = mean(0.4, 0.3, 0.2) = 0.3
    EXPECT_NEAR(yearly[0].short_sm, 0.3, 1e-10);
}

TEST(YearlyMetrics, DailyTvr) {
    // daily_tvr is mean turnover for the year
    int64_t base = 1704067200000LL;
    int64_t day_ms = 86400000LL;

    std::vector<int64_t> timestamps = {
        base, base + day_ms, base + 2*day_ms, base + 3*day_ms
    };
    std::vector<double> ret = {0.0, 0.01, 0.02, 0.03};
    // turnover: [skip, 0.1, 0.2, 0.3]
    std::vector<double> turnover = {0.0, 0.1, 0.2, 0.3};
    std::vector<double> lw = {0.0, 0.5, 0.5, 0.5};
    std::vector<double> sw = {0.0, 0.5, 0.5, 0.5};

    auto yearly = compute_yearly(timestamps, ret, turnover, lw, sw);

    ASSERT_EQ(yearly.size(), 1u);
    // daily_tvr = mean(0.1, 0.2, 0.3) = 0.2
    EXPECT_NEAR(yearly[0].daily_tvr, 0.2, 1e-10);
}

TEST(YearlyMetrics, MarginFormula) {
    // margin = mean_ret / daily_tvr
    int64_t base = 1704067200000LL;
    int64_t day_ms = 86400000LL;

    std::vector<int64_t> timestamps = {
        base, base + day_ms, base + 2*day_ms, base + 3*day_ms
    };
    // returns: [skip, 0.01, 0.02, 0.03], mean = 0.02
    std::vector<double> ret = {0.0, 0.01, 0.02, 0.03};
    // turnover: [skip, 0.1, 0.2, 0.3], mean_tvr = 0.2
    std::vector<double> turnover = {0.0, 0.1, 0.2, 0.3};
    std::vector<double> lw = {0.0, 0.5, 0.5, 0.5};
    std::vector<double> sw = {0.0, 0.5, 0.5, 0.5};

    auto yearly = compute_yearly(timestamps, ret, turnover, lw, sw);

    ASSERT_EQ(yearly.size(), 1u);
    // margin = 0.02 / 0.2 = 0.1
    EXPECT_NEAR(yearly[0].margin, 0.1, 1e-6);
}
