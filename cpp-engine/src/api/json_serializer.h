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
    std::size_t max_cells = 4000000,
    const std::vector<std::size_t>* symbol_subset = nullptr,   // subset: every row, only these columns
    const std::vector<uint8_t>* universe_mask = nullptr);      // T*N; non-empty adds "membership"

// Signal distribution histogram (fixed 200 bins, rows from start_index). When
// timestamps/symbols are given, each bin also names one example cell
// (exampleSymbol/exampleDate: the largest |value| in the bin) for navigation.
Json::Value signal_distribution_to_json(
    const std::vector<std::vector<float>>& signals,
    std::size_t start_index = 0,
    const std::vector<int64_t>* timestamps = nullptr,
    const std::vector<std::string>* symbols = nullptr);
constexpr int kSignalHistogramBins = 200;

// Full-population histogram in log (positive), negative-log (negative), or
// symlog (mixed/zero) space. Centers and 201 edges remain in raw signal units.
// Unlike the legacy linear histogram, every finite value, including zero, counts.
// The nested linear object bins the same full population uniformly in raw units.
Json::Value signal_log_distribution_to_json(
    const std::vector<std::vector<float>>& signals,
    std::size_t start_index = 0,
    const std::vector<int64_t>* timestamps = nullptr,
    const std::vector<std::string>* symbols = nullptr);
