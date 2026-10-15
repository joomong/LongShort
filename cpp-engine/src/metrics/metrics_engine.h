#pragma once
#include "summary_metrics.h"
#include "yearly_metrics.h"
#include "timeseries_output.h"
#include <string>

struct FullMetricsResult {
    SummaryMetrics is_summary;
    SummaryMetrics os_summary;
    std::vector<YearlyMetrics> yearly;       // full period, single table (STAR)
    TimeseriesOutput timeseries;             // full period, single series (STAR)
    size_t is_start_index;                   // index into timeseries where IS starts
    size_t os_start_index;                   // index into timeseries where OS starts
};

// Timeseries/yearly cover [start_index, end); IS/OS summary ranges are clamped
// to that trading span. The default includes the first trading bar.
// is_timestamp_ms: first bar of IS metrics period
// os_timestamp_ms: first bar of OS period
// Performance metrics (summary, yearly, drawdown) use gross_ret; net_ret only
// feeds the net cumulative PnL series.
FullMetricsResult compute_full_metrics(
    const std::vector<int64_t>& timestamps,
    const std::vector<double>& net_ret,
    const std::vector<double>& gross_ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& position_count,
    int64_t is_timestamp_ms,
    int64_t os_timestamp_ms,
    unsigned bars_per_year = 365,
    std::size_t start_index = 0,
    const std::vector<double>* frozen_gross = nullptr
);
