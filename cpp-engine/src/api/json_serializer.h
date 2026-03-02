#pragma once
#include <json/json.h>
#include "../metrics/metrics_engine.h"
#include "../engine/simulator.h"

// Summary metrics -> JSON (STAR field names)
Json::Value summary_to_json(const SummaryMetrics& m);

// Yearly metrics -> JSON array
Json::Value yearly_to_json(const std::vector<YearlyMetrics>& yearly);

// Timeseries -> JSON object
Json::Value timeseries_to_json(const TimeseriesOutput& ts);

// Signal plot -> JSON (T×N matrix with timestamps + symbols)
Json::Value signal_plot_to_json(
    const std::vector<std::vector<float>>& signals,
    const std::vector<int64_t>& timestamps,
    const std::vector<std::string>& symbols);

// Signal distribution histogram (fixed 50 bins)
Json::Value signal_distribution_to_json(
    const std::vector<std::vector<float>>& signals);

// Full single-alpha response
Json::Value single_alpha_response(
    const FullMetricsResult& metrics,
    const SimulatorOutput& sim_out,
    const std::vector<std::string>& symbols);
