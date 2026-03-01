#include <gtest/gtest.h>
#include <cmath>
#include <vector>
#include "metrics/summary_metrics.h"

TEST(SummaryMetrics, BasicComputation) {
    // 5 bars: first is 0 (dropped), then 4 return bars
    std::vector<double> ret = {0.0, 0.01, -0.005, 0.02, 0.015};
    std::vector<double> turnover = {0.0, 0.5, 0.3, 0.4, 0.2};
    std::vector<double> min_bet = {0.0, 0.1, 0.1, 0.1, 0.1};
    std::vector<double> max_weight = {0.0, 0.3, 0.3, 0.3, 0.3};
    std::vector<double> long_weight = {0.0, 0.5, 0.5, 0.5, 0.5};
    std::vector<double> short_weight = {0.0, 0.5, 0.5, 0.5, 0.5};
    std::vector<int> nonzero_count = {0, 3, 3, 3, 3};

    auto m = compute_summary(ret, turnover, min_bet, max_weight,
                             long_weight, short_weight, nonzero_count);

    // PnL should be positive (cumulative return of +1%, -0.5%, +2%, +1.5%)
    EXPECT_GT(m.pnl, 0.0);

    // Sharpe should be positive
    EXPECT_GT(m.sharpe, 0.0);

    // MDD should be negative or zero
    EXPECT_LE(m.mdd, 0.0);

    // Nonzero position: 4 bars (excluding first)
    EXPECT_EQ(m.nonzero_position, 4);

    // Win rate: 3 positive out of 4 = 0.75
    EXPECT_NEAR(m.win_rate, 0.75, 1e-6);
}

TEST(SummaryMetrics, EmptyReturns) {
    std::vector<double> ret = {0.0};
    std::vector<double> turnover = {0.0};
    std::vector<double> min_bet = {0.0};
    std::vector<double> max_weight = {0.0};
    std::vector<double> long_weight = {0.0};
    std::vector<double> short_weight = {0.0};
    std::vector<int> nonzero_count = {0};

    auto m = compute_summary(ret, turnover, min_bet, max_weight,
                             long_weight, short_weight, nonzero_count);

    EXPECT_DOUBLE_EQ(m.pnl, 0.0);
    EXPECT_DOUBLE_EQ(m.sharpe, 0.0);
}
