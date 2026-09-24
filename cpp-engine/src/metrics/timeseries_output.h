#pragma once
#include <vector>
#include <cstdint>
#include <cstddef>

struct TimeseriesOutput {
    std::vector<int64_t> timestamps;
    std::vector<double> cum_pnl;
    std::vector<double> cum_pnl_gross;
    std::vector<double> turnover_pct;
    std::vector<int> position_count;
    std::vector<double> max_weight_pct;
    std::vector<double> drawdown;
    // Missing-close accounting, empty unless the caller supplies the series.
    std::vector<double> frozen_gross;   // held exposure with no quote on the bar
    // Borrow financing taken from net on the bar, empty unless supplied; the gross
    // cumulative series adds it back beside the fee.
    std::vector<double> borrow_cost;
};

TimeseriesOutput compute_timeseries(
    const std::vector<int64_t>& timestamps,
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<int>& position_count,
    double fee_rate,
    std::size_t start_index = 1,
    const std::vector<double>* frozen_gross = nullptr,
    const std::vector<double>* borrow_cost = nullptr
);
