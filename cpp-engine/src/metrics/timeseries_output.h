#pragma once
#include <vector>
#include <cstdint>

struct TimeseriesOutput {
    std::vector<int64_t> timestamps;
    std::vector<double> cum_pnl;
    std::vector<double> cum_pnl_gross;
    std::vector<double> turnover_pct;
    std::vector<int> position_count;
    std::vector<double> max_weight_pct;
    std::vector<double> drawdown;
};

TimeseriesOutput compute_timeseries(
    const std::vector<int64_t>& timestamps,
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<int>& position_count,
    double fee_rate
);
