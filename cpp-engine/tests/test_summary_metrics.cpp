#include <gtest/gtest.h>
#include <cmath>
#include <vector>
#include "metrics/summary_metrics.h"

TEST(SummaryMetrics, BasicComputation) {
    // 4 return bars (no dummy bar-0 — compute_summary processes all elements)
    std::vector<double> ret = {0.01, -0.005, 0.02, 0.015};
    std::vector<double> turnover = {0.5, 0.3, 0.4, 0.2};
    std::vector<double> min_bet = {0.1, 0.1, 0.1, 0.1};
    std::vector<double> max_weight = {0.3, 0.3, 0.3, 0.3};
    std::vector<double> long_weight = {0.5, 0.5, 0.5, 0.5};
    std::vector<double> short_weight = {0.5, 0.5, 0.5, 0.5};
    std::vector<int> nonzero_count = {3, 3, 3, 3};

    auto m = compute_summary(ret, turnover, min_bet, max_weight,
                             long_weight, short_weight, nonzero_count);

    // PnL should be positive (cumulative return of +1%, -0.5%, +2%, +1.5%)
    EXPECT_GT(m.pnl, 0.0);

    // Sharpe should be positive
    EXPECT_GT(m.sharpe, 0.0);

    // MDD should be negative or zero
    EXPECT_LE(m.mdd, 0.0);

    // Nonzero position: all 4 bars have nonzero_count > 0
    EXPECT_EQ(m.nonzero_position, 4);

    // Win rate: 3 positive out of 4 = 0.75
    EXPECT_NEAR(m.win_rate, 0.75, 1e-6);
}

TEST(SummaryMetrics, EmptyReturns) {
    std::vector<double> ret = {};
    std::vector<double> turnover = {};
    std::vector<double> min_bet = {};
    std::vector<double> max_weight = {};
    std::vector<double> long_weight = {};
    std::vector<double> short_weight = {};
    std::vector<int> nonzero_count = {};

    auto m = compute_summary(ret, turnover, min_bet, max_weight,
                             long_weight, short_weight, nonzero_count);

    EXPECT_DOUBLE_EQ(m.pnl, 0.0);
    EXPECT_DOUBLE_EQ(m.sharpe, 0.0);
}

TEST(SummaryMetrics, AllBarsProcessed) {
    // Verify a 3-element slice produces metrics using all 3 elements
    std::vector<double> ret = {0.01, 0.02, 0.03};
    std::vector<double> turnover = {0.1, 0.2, 0.3};
    std::vector<double> min_bet = {0.05, 0.06, 0.07};
    std::vector<double> max_weight = {0.4, 0.5, 0.6};
    std::vector<double> long_weight = {0.6, 0.6, 0.6};
    std::vector<double> short_weight = {0.4, 0.4, 0.4};
    std::vector<int> nonzero_count = {2, 3, 3};

    auto m = compute_summary(ret, turnover, min_bet, max_weight,
                             long_weight, short_weight, nonzero_count);

    // PnL = sum of all 3 returns = 0.06
    EXPECT_NEAR(m.pnl, 0.06, 1e-10);

    // ann_ret = mean(0.01, 0.02, 0.03) * 365 = 0.02 * 365 = 7.3
    EXPECT_NEAR(m.ann_ret, 7.3, 1e-10);

    // turnover = mean(0.1, 0.2, 0.3) = 0.2
    EXPECT_NEAR(m.turnover, 0.2, 1e-10);

    // min_bet = mean(0.05, 0.06, 0.07) = 0.06
    EXPECT_NEAR(m.min_bet, 0.06, 1e-10);

    // max_weight = max(0.4, 0.5, 0.6) = 0.6
    EXPECT_NEAR(m.max_weight, 0.6, 1e-10);

    // nonzero_position = count(>0) = 3
    EXPECT_EQ(m.nonzero_position, 3);

    // win_rate = 3/3 = 1.0
    EXPECT_NEAR(m.win_rate, 1.0, 1e-10);
}

TEST(SummaryMetrics, AnnRetMatchesFormula) {
    // ann_ret = mean(daily_ret) * 365
    // returns: {0.02, -0.01, 0.03, 0.01, 0.005}
    // mean = (0.02 + -0.01 + 0.03 + 0.01 + 0.005) / 5 = 0.055 / 5 = 0.011
    // ann_ret = 0.011 * 365 = 4.015
    std::vector<double> ret = {0.02, -0.01, 0.03, 0.01, 0.005};
    std::vector<double> zeros(5, 0.0);
    std::vector<int> nz(5, 1);

    auto m = compute_summary(ret, zeros, zeros, zeros, zeros, zeros, nz);

    EXPECT_NEAR(m.ann_ret, 4.015, 1e-10);
}

TEST(SummaryMetrics, SharpeMatchesFormula) {
    // sharpe = (mean / std_ddof1) * sqrt(365)
    // returns: {0.02, -0.01, 0.03, 0.01, 0.005}
    // mean = 0.011
    // var_ddof1 = ((0.009^2 + 0.021^2 + 0.019^2 + 0.001^2 + 0.006^2)) / 4
    //           = (0.000081 + 0.000441 + 0.000361 + 0.000001 + 0.000036) / 4
    //           = 0.000920 / 4 = 0.000230
    // std_ddof1 = sqrt(0.000230) = 0.01516575...
    // sharpe = (0.011 / 0.01516575) * sqrt(365) = 0.72536... * 19.1049... = 13.856...
    std::vector<double> ret = {0.02, -0.01, 0.03, 0.01, 0.005};
    std::vector<double> zeros(5, 0.0);
    std::vector<int> nz(5, 1);

    auto m = compute_summary(ret, zeros, zeros, zeros, zeros, zeros, nz);

    double mean_r = 0.011;
    double sq_sum = 0.0;
    for (double v : ret) { double d = v - mean_r; sq_sum += d * d; }
    double std_r = std::sqrt(sq_sum / 4.0);  // ddof=1, n-1=4
    double expected_sharpe = (mean_r / (std_r + 1e-12)) * std::sqrt(365.0);

    EXPECT_NEAR(m.sharpe, expected_sharpe, 1e-6);
}

TEST(SummaryMetrics, MddAdditive) {
    // MDD uses additive peak-trough (not multiplicative)
    // returns: {0.05, -0.08, 0.02}
    // cumPnl: {0.05, -0.03, -0.01}
    // peak:   {0.05,  0.05,  0.05}
    // dd:     {0.0,  -0.08, -0.06}
    // mdd = -0.08
    std::vector<double> ret = {0.05, -0.08, 0.02};
    std::vector<double> zeros(3, 0.0);
    std::vector<int> nz(3, 1);

    auto m = compute_summary(ret, zeros, zeros, zeros, zeros, zeros, nz);

    EXPECT_NEAR(m.mdd, -0.08, 1e-10);
}

TEST(SummaryMetrics, MarginMatchesFormula) {
    // margin = mean_ret / turnover
    // returns: {0.01, 0.02, 0.03}, mean = 0.02
    // turnover: {0.1, 0.2, 0.3}, mean_tvr = 0.2
    // margin = 0.02 / 0.2 = 0.1
    std::vector<double> ret = {0.01, 0.02, 0.03};
    std::vector<double> turnover = {0.1, 0.2, 0.3};
    std::vector<double> zeros(3, 0.0);
    std::vector<int> nz(3, 1);

    auto m = compute_summary(ret, turnover, zeros, zeros, zeros, zeros, nz);

    EXPECT_NEAR(m.margin, 0.1, 1e-6);
}
