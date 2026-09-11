#pragma once
#include <vector>
#include <cstdint>
#include <cstddef>

struct YearlyMetrics {
    int year;
    double pnl_pct;
    double ann_ret;
    double sharpe;
    double max_dd;
    double win_lose;
    double daily_tvr;
    double margin;
    double long_sm;
    double short_sm;
};

std::vector<YearlyMetrics> compute_yearly(
    const std::vector<int64_t>& timestamps,  // epoch_ms
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    unsigned bars_per_year = 365,
    std::size_t start_index = 1
);
