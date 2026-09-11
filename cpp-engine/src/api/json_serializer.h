#pragma once
#include <json/json.h>
#include <cstddef>
#include "../metrics/metrics_engine.h"

// Summary metrics -> JSON (STAR field names)
Json::Value summary_to_json(const SummaryMetrics& m);

// Yearly metrics -> JSON array
Json::Value yearly_to_json(const std::vector<YearlyMetrics>& yearly);

// Timeseries -> JSON object
Json::Value timeseries_to_json(const TimeseriesOutput& ts);

// Raw signal plot with aligned date/symbol downsampling and a hard cell budget.
Json::Value signal_plot_to_json(
    const std::vector<std::vector<float>>& signals,
    const std::vector<int64_t>& timestamps,
    const std::vector<std::string>& symbols,
    std::size_t start_index = 0,
    std::size_t max_cells = 250000);

// Signal distribution histogram (fixed 50 bins, rows from start_index)
Json::Value signal_distribution_to_json(
    const std::vector<std::vector<float>>& signals,
    std::size_t start_index = 0);

