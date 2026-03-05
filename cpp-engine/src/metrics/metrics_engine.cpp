#include "metrics_engine.h"
#include <algorithm>
#include <cstddef>

FullMetricsResult compute_full_metrics(
    const std::vector<int64_t>& timestamps,
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& min_bet,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& nonzero_count,
    int64_t is_timestamp_ms,
    int64_t os_timestamp_ms,
    double fee_rate,
    unsigned bars_per_year)
{
    FullMetricsResult result{};

    const size_t T = timestamps.size();
    if (T == 0) return result;

    // Find IS start index
    size_t is_idx = 0;
    for (size_t i = 0; i < T; ++i) {
        if (timestamps[i] >= is_timestamp_ms) {
            is_idx = i;
            break;
        }
    }

    // Find OS start index
    size_t os_idx = T;
    for (size_t i = 0; i < T; ++i) {
        if (timestamps[i] >= os_timestamp_ms) {
            os_idx = i;
            break;
        }
    }

    // Full-period timeseries (STAR: single series, no IS/OS split)
    result.timeseries = compute_timeseries(timestamps, ret, turnover, max_weight, nonzero_count, fee_rate);

    // Full-period yearly (STAR: single table)
    result.yearly = compute_yearly(timestamps, ret, turnover, long_weight, short_weight, bars_per_year);

    // Timeseries output skips bar 0 -> length T-1, timeseries[k] = raw[k+1]
    // Adjust raw indices to timeseries-relative indices for JSON output
    size_t is_for_summary = std::max(is_idx, size_t(1));  // never include global bar 0
    result.is_start_index = is_for_summary - 1;  // raw 365 -> ts index 364
    result.os_start_index = (os_idx > 0) ? os_idx - 1 : 0;  // raw 730 -> ts index 729

    // IS summary: [is_for_summary, os_idx)  — skip global bar 0
    if (is_for_summary < os_idx) {
        result.is_summary = compute_summary_range(
            ret, turnover, min_bet, max_weight, long_weight, short_weight, nonzero_count,
            is_for_summary, os_idx, bars_per_year);
    }

    // OS summary: [os_idx, T)
    if (os_idx < T) {
        result.os_summary = compute_summary_range(
            ret, turnover, min_bet, max_weight, long_weight, short_weight, nonzero_count,
            os_idx, T, bars_per_year);
    }

    return result;
}
