#pragma once
#include <vector>

constexpr unsigned BARS_PER_YEAR = 365;

struct SummaryMetrics {
    double pnl = 0;
    double cagr = 0;
    double sharpe = 0;
    double mdd = 0;
    double turnover = 0;
    double min_bet = 0;
    double max_weight = 0;
    int nonzero_position = 0;
    double win_rate = 0;
    double margin = 0;
    double long_ratio = 0;
    double short_ratio = 0;
};

SummaryMetrics compute_summary(
    const std::vector<double>& ret,        // length T, first bar is 0
    const std::vector<double>& turnover,
    const std::vector<double>& min_bet,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& nonzero_count,
    unsigned bars_per_year = BARS_PER_YEAR
);
