#include "metrics_engine.h"
#include <algorithm>
#include <cstddef>

FullMetricsResult compute_full_metrics(
    const std::vector<int64_t>& timestamps,
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& position_count,
    int64_t is_timestamp_ms,
    int64_t os_timestamp_ms,
    double fee_rate,
    unsigned bars_per_year,
    std::size_t start_index,
    const std::vector<double>* frozen_gross,
    const std::vector<double>* borrow_cost)
{
    FullMetricsResult result{};

    const size_t T = timestamps.size();
    if (T == 0) return result;

    const size_t begin = std::min(start_index, T);
    const size_t is_idx = std::max(begin, static_cast<size_t>(
        std::lower_bound(timestamps.begin(), timestamps.end(), is_timestamp_ms) - timestamps.begin()));
    const size_t os_idx = std::max(begin, static_cast<size_t>(
        std::lower_bound(timestamps.begin(), timestamps.end(), os_timestamp_ms) - timestamps.begin()));

    result.timeseries = compute_timeseries(
        timestamps, ret, turnover, max_weight, position_count, fee_rate, begin,
        frozen_gross, borrow_cost);
    result.yearly = compute_yearly(
        timestamps, ret, turnover, long_weight, short_weight, bars_per_year, begin);
    result.is_start_index = is_idx - begin;
    result.os_start_index = os_idx - begin;

    if (is_idx < os_idx) {
        result.is_summary = compute_summary_range(
            ret, turnover, max_weight, long_weight, short_weight, position_count,
            is_idx, os_idx, bars_per_year);
    }
    if (os_idx < T) {
        result.os_summary = compute_summary_range(
            ret, turnover, max_weight, long_weight, short_weight, position_count,
            os_idx, T, bars_per_year);
    }
    return result;
}
