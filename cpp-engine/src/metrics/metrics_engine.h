#pragma once
#include "summary_metrics.h"
#include "yearly_metrics.h"
#include "timeseries_output.h"
#include <string>

struct FullMetricsResult {
    SummaryMetrics is_summary;
    SummaryMetrics os_summary;
    std::vector<YearlyMetrics> is_yearly;
    std::vector<YearlyMetrics> os_yearly;
    TimeseriesOutput is_timeseries;
    TimeseriesOutput os_timeseries;
};

// Split SimulatorOutput by date, compute metrics for each period
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
    unsigned bars_per_year = 365
);
