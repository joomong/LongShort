#include <gtest/gtest.h>
#include <cmath>
#include <vector>
#include <limits>
#include "metrics/yearly_metrics.h"

TEST(YearlyMetrics, SingleYear) {
    // 5 bars in 2024, timestamps in epoch_ms
    // 2024-01-01 00:00:00 UTC = 1704067200000 ms
    int64_t base = 1704067200000LL;
    int64_t day_ms = 86400000LL;

    std::vector<int64_t> timestamps = {
        base, base + day_ms, base + 2*day_ms, base + 3*day_ms, base + 4*day_ms
    };
    // Explicit start_index=1 excludes this fixture's warmup bar.
    std::vector<double> ret = {0.0, 0.01, -0.005, 0.02, 0.015};
    std::vector<double> turnover = {0.0, 0.5, 0.3, 0.4, 0.2};
    std::vector<double> long_weight = {0.0, 0.5, 0.5, 0.5, 0.5};
    std::vector<double> short_weight = {0.0, 0.5, 0.5, 0.5, 0.5};

    auto yearly = compute_yearly(timestamps, ret, turnover, long_weight, short_weight, 365, 1);

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

    auto yearly = compute_yearly(timestamps, ret, turnover, long_weight, short_weight, 365, 1);

    ASSERT_EQ(yearly.size(), 2u);
    EXPECT_EQ(yearly[0].year, 2023);
    EXPECT_EQ(yearly[1].year, 2024);
    EXPECT_DOUBLE_EQ(yearly[0].ir, 0.0); // one observation in 2023
    EXPECT_NEAR(yearly[1].ir, 0.005 / std::sqrt(0.00045), 1e-12);
}

TEST(YearlyMetrics, DailyIrUsesSampleStddevWithoutAnnualization) {
    const int64_t base = 1704067200000LL;
    const int64_t day = 86400000LL;
    const std::vector<int64_t> timestamps = {base, base + day, base + 2*day, base + 3*day};
    const std::vector<double> ret = {99.0, 0.01, 0.02, 0.03}; // warmup must be excluded
    const std::vector<double> turnover(4, 0.1), weights(4, 0.5);

    for (unsigned annualization : {252u, 365u}) {
        const auto yearly = compute_yearly(timestamps, ret, turnover, weights, weights, annualization, 1);
        ASSERT_EQ(yearly.size(), 1u);
        // Daily mean=0.02 and sample standard deviation=0.01.
        EXPECT_NEAR(yearly[0].ir, 2.0, 1e-12);
        EXPECT_NEAR(yearly[0].sharpe, (0.02 / (0.01 + 1e-12)) * std::sqrt(annualization), 1e-12);
    }
}

TEST(YearlyMetrics, DailyIrSkipsNonfiniteReturnsAndHandlesDegenerateYears) {
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const double inf = std::numeric_limits<double>::infinity();
    const int64_t base = 1704067200000LL, day = 86400000LL;
    const std::vector<int64_t> timestamps = {base, base + day, base + 2*day, base + 3*day, base + 4*day};
    const std::vector<double> turnover(5, 0.1), weights(5, 0.5);
    const auto filtered = compute_yearly(timestamps, {0.0, 0.01, nan, 0.03, inf}, turnover, weights, weights, 365, 1);
    ASSERT_EQ(filtered.size(), 1u);
    EXPECT_NEAR(filtered[0].ir, std::sqrt(2.0), 1e-12);

    for (const auto& ret : std::vector<std::vector<double>>{
             {0.0, 0.01, 0.01, 0.01, 0.01}, // constant
             {0.0, nan, 0.01, inf, nan},     // one finite return
             {0.0, nan, nan, inf, nan}}) {   // no finite returns
        const auto yearly = compute_yearly(timestamps, ret, turnover, weights, weights, 365, 1);
        ASSERT_EQ(yearly.size(), 1u);
        EXPECT_DOUBLE_EQ(yearly[0].ir, 0.0);
        EXPECT_DOUBLE_EQ(yearly[0].sharpe, 0.0);
        EXPECT_DOUBLE_EQ(yearly[0].fitness, 0.0);
    }
}

TEST(YearlyMetrics, FitnessUsesLeverageOneReturnsAndDailyTurnoverFloor) {
    const int64_t base = 1704067200000LL, day = 86400000LL;
    const std::vector<int64_t> timestamps = {base, base + day, base + 2*day, base + 3*day};
    const std::vector<double> weights(4, 0.5);
    for (double sign : {1.0, -1.0}) {
        // Mean gross daily return = +/-0.002; sample stddev = 0.001.
        const std::vector<double> ret = {99.0, sign*0.001, sign*0.002, sign*0.003};
        for (double daily_tvr : {0.0, 0.10, 0.125, 0.25}) {
            const std::vector<double> turnover = {99.0, daily_tvr, daily_tvr, daily_tvr};
            const auto yearly = compute_yearly(timestamps, ret, turnover, weights, weights, 252, 1);
            ASSERT_EQ(yearly.size(), 1u);
            // Annual return on the full book is +/-50.4%, not +/-100.8%.
            EXPECT_NEAR(yearly[0].ann_ret, sign*50.4, 1e-12);
            const double sharpe = (sign*0.002 / (0.001 + 1e-12)) * std::sqrt(252.0);
            const double denominator = daily_tvr <= 0.125 ? 0.125 : daily_tvr;
            EXPECT_NEAR(yearly[0].fitness, sharpe * std::sqrt(0.504 / denominator), 1e-10);
        }
    }
}

TEST(YearlyMetrics, FitnessUsesEachYearsOwnReturnsAndTurnover) {
    const std::vector<int64_t> timestamps = {
        1703808000000LL, 1703894400000LL, 1703980800000LL, // December 29-31, 2023
        1704067200000LL, 1704153600000LL                 // January 1-2, 2024
    };
    const std::vector<double> ret = {99.0, 0.001, 0.003, -0.001, -0.003};
    const std::vector<double> turnover = {99.0, 0.1, 0.1, 0.2, 0.3};
    const std::vector<double> weights(5, 0.5);
    const auto yearly = compute_yearly(timestamps, ret, turnover, weights, weights, 252, 1);
    ASSERT_EQ(yearly.size(), 2u);
    const double sharpe = (0.002 / (std::sqrt(0.000002) + 1e-12)) * std::sqrt(252.0);
    EXPECT_NEAR(yearly[0].fitness, sharpe * std::sqrt(0.504 / 0.125), 1e-10);
    EXPECT_NEAR(yearly[1].fitness, -sharpe * std::sqrt(0.504 / 0.25), 1e-10);
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

    auto yearly = compute_yearly(timestamps, ret, turnover, lw, sw, 365, 1);

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

    auto yearly = compute_yearly(timestamps, ret, turnover, lw, sw, 365, 1);

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

    auto yearly = compute_yearly(timestamps, ret, turnover, lw, sw, 365, 1);

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

    auto yearly = compute_yearly(timestamps, ret, turnover, lw, sw, 365, 1);

    ASSERT_EQ(yearly.size(), 1u);
    // margin = 0.02 / 0.2 = 0.1
    EXPECT_NEAR(yearly[0].margin, 0.1, 1e-6);
}

TEST(YearlyMetrics, DefaultIncludesTheFirstTradingBar) {
    const std::vector<int64_t> dates = {1704067200000LL};
    const auto yearly = compute_yearly(dates, {0.02}, {1.0}, {1.0}, {0.0});
    ASSERT_EQ(yearly.size(), 1u);
    EXPECT_DOUBLE_EQ(yearly[0].pnl_pct, 2.0);
    EXPECT_DOUBLE_EQ(yearly[0].daily_tvr, 1.0);
}
