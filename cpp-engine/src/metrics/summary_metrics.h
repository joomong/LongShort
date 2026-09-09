#pragma once
#include <cstddef>
#include <vector>

constexpr unsigned BARS_PER_YEAR = 365;

struct SummaryMetrics {
    double pnl = 0;
    double ann_ret = 0;
    double sharpe = 0;
    double mdd = 0;
    double turnover = 0;
    double avg_position_count = 0; // mean number of held symbols; UI avgBet
    double max_weight = 0;
    int active_bar_count = 0;      // bars with at least one held symbol
    double win_rate = 0;
    double margin = 0;
    double long_ratio = 0;
    double short_ratio = 0;
    double avg_max_bet = 0;        // mean of per-bar maximum absolute weight
    double min_position_count = 0; // minimum number of held symbols; UI minBet
    double quality = 0;            // sharpe * sqrt(abs(1-maxexp)) * (1-max_weight)
    double sharpe400 = 0;
    double sharpe80 = 0;
    double minsoc = 0;             // minimum gross exposure
};

// Inputs are pre-sliced; all return bars are processed.
SummaryMetrics compute_summary(
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& position_count,
    unsigned bars_per_year = BARS_PER_YEAR
);

SummaryMetrics compute_summary_range(
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& position_count,
    size_t start,
    size_t end,
    unsigned bars_per_year = BARS_PER_YEAR
);
