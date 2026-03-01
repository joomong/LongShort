#include "metrics_engine.h"
#include <algorithm>
#include <cstddef>

// Helper: slice a vector by index range [start, end)
template<typename T>
static std::vector<T> slice(const std::vector<T>& v, size_t start, size_t end) {
    if (start >= v.size()) return {};
    size_t e = std::min(end, v.size());
    return std::vector<T>(v.begin() + start, v.begin() + e);
}

FullMetricsResult compute_full_metrics(
    const std::vector<int64_t>& timestamps,
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& min_bet,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& nonzero_count,
    int64_t split_timestamp_ms,
    double fee_rate,
    unsigned bars_per_year)
{
    FullMetricsResult result{};

    const size_t T = timestamps.size();
    if (T == 0) return result;

    // Find split index: first index where timestamps[i] >= split_timestamp_ms
    // IS period: [0, split_idx)
    // OS period: [split_idx, T)
    size_t split_idx = T;
    for (size_t i = 0; i < T; ++i) {
        if (timestamps[i] >= split_timestamp_ms) {
            split_idx = i;
            break;
        }
    }

    // IS slices
    auto is_ts  = slice(timestamps,    0, split_idx);
    auto is_ret = slice(ret,           0, split_idx);
    auto is_tvr = slice(turnover,      0, split_idx);
    auto is_mb  = slice(min_bet,       0, split_idx);
    auto is_mw  = slice(max_weight,    0, split_idx);
    auto is_lw  = slice(long_weight,   0, split_idx);
    auto is_sw  = slice(short_weight,  0, split_idx);
    auto is_nz  = slice(nonzero_count, 0, split_idx);

    // OS slices
    auto os_ts  = slice(timestamps,    split_idx, T);
    auto os_ret = slice(ret,           split_idx, T);
    auto os_tvr = slice(turnover,      split_idx, T);
    auto os_mb  = slice(min_bet,       split_idx, T);
    auto os_mw  = slice(max_weight,    split_idx, T);
    auto os_lw  = slice(long_weight,   split_idx, T);
    auto os_sw  = slice(short_weight,  split_idx, T);
    auto os_nz  = slice(nonzero_count, split_idx, T);

    // IS metrics
    if (!is_ts.empty()) {
        result.is_summary   = compute_summary(is_ret, is_tvr, is_mb, is_mw, is_lw, is_sw, is_nz, bars_per_year);
        result.is_yearly    = compute_yearly(is_ts, is_ret, is_tvr, is_lw, is_sw, bars_per_year);
        result.is_timeseries = compute_timeseries(is_ts, is_ret, is_tvr, is_mw, is_nz, fee_rate);
    }

    // OS metrics
    // For OS, the first bar of the OS slice may not be a "fresh start" bar in the
    // original series. We prepend a zero-return bar so that the skip-first-bar
    // convention in compute_summary/compute_yearly is applied consistently.
    if (!os_ts.empty()) {
        // Prepend a synthetic zero bar for the OS period so that index-0 skip
        // aligns with the IS/OS boundary.
        std::vector<int64_t> os_ts_pad;
        std::vector<double>  os_ret_pad, os_tvr_pad, os_mb_pad, os_mw_pad, os_lw_pad, os_sw_pad;
        std::vector<int>     os_nz_pad;

        // Use the last IS timestamp as the padding bar, or os_ts[0] - 1 if IS is empty.
        int64_t pad_ts = is_ts.empty() ? (os_ts[0] - 86400000LL) : is_ts.back();
        os_ts_pad.push_back(pad_ts);
        os_ret_pad.push_back(0.0);
        os_tvr_pad.push_back(0.0);
        os_mb_pad.push_back(0.0);
        os_mw_pad.push_back(0.0);
        os_lw_pad.push_back(0.0);
        os_sw_pad.push_back(0.0);
        os_nz_pad.push_back(0);

        for (size_t i = 0; i < os_ts.size(); ++i) {
            os_ts_pad.push_back(os_ts[i]);
            os_ret_pad.push_back(i < os_ret.size() ? os_ret[i] : 0.0);
            os_tvr_pad.push_back(i < os_tvr.size() ? os_tvr[i] : 0.0);
            os_mb_pad.push_back(i < os_mb.size()   ? os_mb[i]  : 0.0);
            os_mw_pad.push_back(i < os_mw.size()   ? os_mw[i]  : 0.0);
            os_lw_pad.push_back(i < os_lw.size()   ? os_lw[i]  : 0.0);
            os_sw_pad.push_back(i < os_sw.size()   ? os_sw[i]  : 0.0);
            os_nz_pad.push_back(i < os_nz.size()   ? os_nz[i]  : 0);
        }

        result.os_summary    = compute_summary(os_ret_pad, os_tvr_pad, os_mb_pad, os_mw_pad,
                                               os_lw_pad, os_sw_pad, os_nz_pad, bars_per_year);
        result.os_yearly     = compute_yearly(os_ts_pad, os_ret_pad, os_tvr_pad,
                                              os_lw_pad, os_sw_pad, bars_per_year);
        result.os_timeseries = compute_timeseries(os_ts_pad, os_ret_pad, os_tvr_pad,
                                                  os_mw_pad, os_nz_pad, fee_rate);
    }

    return result;
}
