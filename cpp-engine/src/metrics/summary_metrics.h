#pragma once
#include <vector>

constexpr unsigned BARS_PER_YEAR = 365;

struct SummaryMetrics {
    double pnl = 0;
    double ann_ret = 0;
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
    double avg_max_bet = 0;   // mean(max_weight_series)
    double min_bet_min = 0;   // min(min_bet_series)
    double quality = 0;       // sharpe * sqrt(abs(1-maxexp)) * (1-max_weight)
    double sharpe400 = 0;     // sharpe over last 400 bars
    double sharpe80 = 0;      // sharpe over last 80 bars
    double minsoc = 0;        // min(sum_abs_weight_per_bar)
};

SummaryMetrics compute_summary(
    const std::vector<double>& ret,        // pre-sliced, no warmup bar — all elements processed
    const std::vector<double>& turnover,
    const std::vector<double>& min_bet,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& nonzero_count,
    unsigned bars_per_year = BARS_PER_YEAR
);
