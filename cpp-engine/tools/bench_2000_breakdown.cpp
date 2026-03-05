#include <chrono>
#include <cstdint>
#include <iostream>
#include <limits>
#include <string>
#include <vector>

#include <json/json.h>

#include "api/json_serializer.h"
#include "compiler/dag_builder.h"
#include "data/config_loader.h"
#include "data/data_panel.h"
#include "engine/simulator.h"
#include "metrics/metrics_engine.h"

namespace {

struct BatchResult {
    int index = 0;
    std::string xml;
    FullMetricsResult metrics;
};

std::string build_delay_options(unsigned n)
{
    std::string s;
    s.reserve(static_cast<size_t>(n) * 5);
    for (unsigned i = 1; i <= n; ++i) {
        if (i > 1)
            s.push_back('|');
        s += std::to_string(i);
    }
    return s;
}

template <typename Clock = std::chrono::steady_clock>
double elapsed_ms(typename Clock::time_point t0, typename Clock::time_point t1)
{
    return std::chrono::duration_cast<std::chrono::duration<double, std::milli>>(t1 - t0).count();
}

}  // namespace

int main(int argc, char** argv)
{
    if (argc < 2) {
        std::cerr << "Usage: bench_2000_breakdown <config.yaml>\n";
        return 2;
    }

    const std::string config_path = argv[1];
    const unsigned kAlphaCount = 2000;

    const auto t_load0 = std::chrono::steady_clock::now();
    const auto cfg = BacktestConfig::from_yaml(config_path);
    auto data = DataPanel::load(cfg.parquet_path, cfg.use_columns, cfg.time_col, cfg.symbol_col);
    const auto t_load1 = std::chrono::steady_clock::now();

    const std::string xml = "<data expr=\"close\" delay=\"" + build_delay_options(kAlphaCount)
        + "\"/><rank/>";

    const auto t_compile0 = std::chrono::steady_clock::now();
    auto alphas = compile_xml(xml, &data);
    const auto t_compile1 = std::chrono::steady_clock::now();
    if (alphas.size() != kAlphaCount) {
        std::cerr << "Expected " << kAlphaCount << " alphas, got " << alphas.size() << "\n";
        return 3;
    }

    SimulatorConfig sim_cfg;
    sim_cfg.T = data.T;
    sim_cfg.N = data.N;
    sim_cfg.fee_rate = cfg.transaction_fee_rate;
    sim_cfg.mode = 1;
    sim_cfg.bars_per_year = 365;
    sim_cfg.collect_signals = false;

    const int64_t is_ms = 0;
    const int64_t os_ms = std::numeric_limits<int64_t>::max();

    std::vector<BatchResult> batches;
    batches.reserve(alphas.size());
    int best_idx = 0;
    double best_sharpe = -1e18;

    const auto t_sim0 = std::chrono::steady_clock::now();
    for (size_t i = 0; i < alphas.size(); ++i) {
        Simulator sim(data, sim_cfg);
        auto sim_out = sim.run(alphas[i].pipeline);
        auto metrics = compute_full_metrics(
            sim_out.timestamps, sim_out.ret, sim_out.turnover,
            sim_out.min_bet, sim_out.max_weight,
            sim_out.long_weight, sim_out.short_weight,
            sim_out.nonzero_count,
            is_ms, os_ms,
            cfg.transaction_fee_rate, 365);

        BatchResult br;
        br.index = static_cast<int>(i);
        br.xml = alphas[i].source_xml;
        br.metrics = std::move(metrics);
        batches.push_back(std::move(br));

        if (batches.back().metrics.is_summary.sharpe > best_sharpe) {
            best_sharpe = batches.back().metrics.is_summary.sharpe;
            best_idx = static_cast<int>(i);
        }
    }
    const auto t_sim1 = std::chrono::steady_clock::now();

    const auto t_json0 = std::chrono::steady_clock::now();
    Json::Value json;
    Json::Value batches_arr(Json::arrayValue);
    auto append_batch = [&](const BatchResult& batch) {
        Json::Value b;
        b["index"] = batch.index;
        b["xml"] = batch.xml;
        Json::Value summary;
        summary["is"] = summary_to_json(batch.metrics.is_summary);
        summary["os"] = summary_to_json(batch.metrics.os_summary);
        b["summary"] = summary;
        b["yearly"] = yearly_to_json(batch.metrics.yearly);
        b["timeseries"] = timeseries_to_json(batch.metrics.timeseries);
        b["isStartIndex"] = static_cast<Json::UInt64>(batch.metrics.is_start_index);
        b["osStartIndex"] = static_cast<Json::UInt64>(batch.metrics.os_start_index);
        batches_arr.append(b);
    };

    append_batch(batches[best_idx]);
    for (size_t i = 0; i < batches.size(); ++i) {
        if (static_cast<int>(i) == best_idx)
            continue;
        append_batch(batches[i]);
    }
    json["batches"] = batches_arr;

    const auto& best = batches[best_idx].metrics;
    json["yearly"] = yearly_to_json(best.yearly);
    json["timeseries"] = timeseries_to_json(best.timeseries);
    json["isStartIndex"] = static_cast<Json::UInt64>(best.is_start_index);
    json["osStartIndex"] = static_cast<Json::UInt64>(best.os_start_index);
    const auto t_json1 = std::chrono::steady_clock::now();

    const auto t_ser0 = std::chrono::steady_clock::now();
    Json::StreamWriterBuilder wb;
    wb["indentation"] = "";
    const std::string payload = Json::writeString(wb, json);
    const auto t_ser1 = std::chrono::steady_clock::now();

    std::cout << "T=" << data.T << " N=" << data.N << " alphas=" << alphas.size() << "\n";
    std::cout << "load_ms=" << elapsed_ms(t_load0, t_load1) << "\n";
    std::cout << "compile_ms=" << elapsed_ms(t_compile0, t_compile1) << "\n";
    std::cout << "simulate_metrics_ms=" << elapsed_ms(t_sim0, t_sim1) << "\n";
    std::cout << "json_build_ms=" << elapsed_ms(t_json0, t_json1) << "\n";
    std::cout << "json_serialize_ms=" << elapsed_ms(t_ser0, t_ser1) << "\n";
    std::cout << "payload_bytes=" << payload.size() << "\n";

    return 0;
}
