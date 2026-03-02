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

// STAR-style: full-period timeseries/yearly, IS/OS split for summary only
// is_timestamp_ms: first bar of IS metrics period
// os_timestamp_ms: first bar of OS period
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
    unsigned bars_per_year = 365
);
