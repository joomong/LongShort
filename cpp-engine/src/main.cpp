#include <drogon/drogon.h>
#include <iostream>
#include <filesystem>
#include <ctime>
#include <cctype>
#include <cstdlib>
#include <memory>
#include <atomic>
#include <mutex>
#include <algorithm>
#include <limits>
#include <cmath>
#include "data/config_loader.h"
#include "data/data_panel.h"
#include "data/parquet_reader.h"
#include "compiler/dag_builder.h"
#include "engine/simulator.h"
#include "metrics/metrics_engine.h"
#include "api/json_serializer.h"
#include "api/combined_signals.h"
#include "api/alpha_storage.h"
#include "ga/search_space.h"
#include "ga/ga_engine.h"
#include <thread>
#include <fstream>
#include <future>
#include <set>

namespace fs = std::filesystem;

// Global shared state (loaded once at startup)
static BacktestConfig g_config;
static Json::Value g_symbol_labels;  // optional {panel symbol: display label}
static DataPanel g_data;
static fs::path g_alphas_dir;
static MissingClosePolicy g_missing_close_policy = MissingClosePolicy::Carry;

static bool supports_exact_fields(std::initializer_list<const char*> names) {
    for (const char* name : names) {
        if (!g_data.has_column(name)) return false;
        bool exact = false;
        for (const auto& dataset : g_config.datasets)
            if (dataset.enabled && dataset.fields.count(name))
                exact = dataset.alignment == FieldAlignment::Exact;
        if (!exact) return false;
    }
    return true;
}

static bool supports_ohlc4() { return supports_exact_fields({"open", "high", "low"}); }
static bool supports_vwap() { return supports_exact_fields({"vwap", "high", "low"}); }

static bool parse_missing_close_policy(const std::string& name, MissingClosePolicy* out)
{
    if (name == "carry") { *out = MissingClosePolicy::Carry; return true; }
    if (name == "error") { *out = MissingClosePolicy::Error; return true; }
    return false;
}

static const char* missing_close_policy_name(MissingClosePolicy policy)
{
    return policy == MissingClosePolicy::Error ? "error" : "carry";
}

static unsigned parse_env_uint(const char* name, unsigned def, unsigned min_v, unsigned max_v)
{
    const char* raw = std::getenv(name);
    if (raw == nullptr || *raw == '\0')
        return def;

    char* end = nullptr;
    long v = std::strtol(raw, &end, 10);
    if (end == nullptr || *end != '\0')
        return def;
    if (v < static_cast<long>(min_v) || v > static_cast<long>(max_v))
        return def;
    return static_cast<unsigned>(v);
}

// jsoncpp's own range error leaks an internal message, so bound the value first.
static unsigned parse_uint_option(const Json::Value& body, const char* key, unsigned fallback)
{
    const Json::Value value = body.get(key, fallback);
    const std::string limits =
        std::string(key) + " must be an integer between 0 and 4294967295";
    if (!value.isIntegral() || !value.isInt64())
        throw std::invalid_argument(limits);
    const Json::Int64 raw = value.asInt64();
    if (raw < 0 || raw > static_cast<Json::Int64>(std::numeric_limits<unsigned>::max()))
        throw std::invalid_argument(limits);
    return static_cast<unsigned>(raw);
}

static std::string iso_date_of(int64_t epoch_ms)
{
    const time_t seconds = static_cast<time_t>(epoch_ms / 1000);
    struct tm parts;
    gmtime_r(&seconds, &parts);
    char text[11];
    strftime(text, sizeof(text), "%Y-%m-%d", &parts);
    return text;
}

static bool parse_iso_date_ms(const std::string& s, int64_t* out_ms)
{
    struct tm t = {};
    char* end = strptime(s.c_str(), "%Y-%m-%d", &t);
    if (end == nullptr || *end != '\0')
        return false;

    time_t sec = timegm(&t);

    struct tm normalized = {};
    gmtime_r(&sec, &normalized);
    char buf[11];
    strftime(buf, sizeof(buf), "%Y-%m-%d", &normalized);
    if (s != buf)
        return false;

    *out_ms = static_cast<int64_t>(sec) * 1000;
    return true;
}

// Long evaluations run off the HTTP event loops so one request cannot stall the
// other connections that share its loop. Workers stay owned until completion
// and are joined before shared data is destroyed at shutdown. Completed workers
// are reaped when another run starts.
class BackgroundWorkers {
public:
    explicit BackgroundWorkers(const char* name) : name_(name) {}
    ~BackgroundWorkers() { stop_and_join(); }

    void stop_and_join() {
        stopping_.store(true, std::memory_order_release);
        std::vector<Task> tasks;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            tasks = std::move(tasks_);
        }
        for (auto& task : tasks)
            if (task.thread.joinable()) task.thread.join();
    }

    bool stopping() const { return stopping_.load(std::memory_order_acquire); }

    template<class Work>
    void launch(Work work) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stopping()) throw std::runtime_error("Server is stopping");
        for (auto it = tasks_.begin(); it != tasks_.end();) {
            if (it->done->load(std::memory_order_acquire)) {
                it->thread.join();
                it = tasks_.erase(it);
            } else ++it;
        }
        auto done = std::make_shared<std::atomic<bool>>(false);
        tasks_.push_back(Task{std::thread{}, done});
        try {
            tasks_.back().thread = std::thread([work = std::move(work), done, name = name_]() mutable {
                try { work(); }
                catch (const std::exception& error) { LOG_ERROR << name << " worker: " << error.what(); }
                catch (...) { LOG_ERROR << name << " worker failed"; }
                done->store(true, std::memory_order_release);
            });
        } catch (...) {
            tasks_.pop_back();
            throw;
        }
    }

private:
    struct Task { std::thread thread; std::shared_ptr<std::atomic<bool>> done; };
    const char* name_;
    std::vector<Task> tasks_;
    std::mutex mutex_;
    std::atomic<bool> stopping_{false};
};
static BackgroundWorkers g_ga_workers("GA");
static BackgroundWorkers g_simulate_workers("Simulate");
static BackgroundWorkers g_alpha_workers("Alpha");

using ClientDisconnected = EvaluationCancelled;

struct RunOptions {
    int64_t is_ms = 0;
    int64_t os_ms = 0;
    unsigned sim_start_index = 0;
    unsigned sim_end_index = 0; // exclusive panel row; request simEnd is inclusive
    unsigned threads = 20;
    bool collect_signals = false;
    bool metrics_only = false;
    bool signal_series_only = false;
    unsigned signal_top_k = 1;
    std::size_t signal_max_cells = 4000000;
    std::vector<std::size_t> signal_symbol_subset;  // non-empty: full series for these columns only
    MissingClosePolicy missing_close_policy = MissingClosePolicy::Carry;
    std::string universe;                          // selected universe.names entry; empty = default
    const std::vector<uint8_t>* universe_mask = nullptr; // resolved from g_data
    std::string execution_price;
};

static RunOptions parse_run_options(const Json::Value& body) {
    RunOptions options;
    const auto execution = body.get("executionPrice", g_config.execution_price);
    if (!execution.isString() || (execution.asString() != "close" && execution.asString() != "ohlc4" &&
                                  execution.asString() != "vwap"))
        throw std::invalid_argument("executionPrice must be 'close', 'ohlc4' or 'vwap'");
    options.execution_price = execution.asString();
    if (options.execution_price == "ohlc4" && !supports_ohlc4())
        throw std::invalid_argument("OHLC4 execution requires available exact-alignment open/high/low fields");
    if (options.execution_price == "vwap" && !supports_vwap())
        throw std::invalid_argument("VWAP execution requires available exact-alignment vwap/high/low fields");
    const std::string is_start = body.get("isStart", "2021-01-01").asString();
    const std::string os_start = body.get("osStart", "2024-01-01").asString();
    if (!parse_iso_date_ms(is_start, &options.is_ms) || !parse_iso_date_ms(os_start, &options.os_ms))
        throw std::invalid_argument("Invalid date format; expected YYYY-MM-DD");
    if (options.is_ms >= options.os_ms)
        throw std::invalid_argument("isStart must be earlier than osStart");
    options.sim_start_index = g_data.trade_start_index;
    const std::string sim_start = body.get("simStart", "").asString();
    if (!sim_start.empty()) {
        int64_t sim_ms = 0;
        if (!parse_iso_date_ms(sim_start, &sim_ms))
            throw std::invalid_argument("Invalid simStart; expected YYYY-MM-DD");
        const auto requested = static_cast<unsigned>(std::lower_bound(
            g_data.timestamps.begin(), g_data.timestamps.end(), sim_ms) - g_data.timestamps.begin());
        options.sim_start_index = std::max(options.sim_start_index, requested);
    }
    if (options.sim_start_index >= g_data.T)
        throw std::invalid_argument("simStart is after the available trading period");
    options.sim_end_index = g_data.T;
    const Json::Value end_value = body.get("simEnd", "");
    if (!end_value.isString())
        throw std::invalid_argument("simEnd must be a YYYY-MM-DD string");
    const std::string sim_end = end_value.asString();
    if (!sim_end.empty()) {
        int64_t end_ms = 0;
        if (!parse_iso_date_ms(sim_end, &end_ms))
            throw std::invalid_argument("Invalid simEnd; expected YYYY-MM-DD");
        options.sim_end_index = static_cast<unsigned>(std::upper_bound(
            g_data.timestamps.begin(), g_data.timestamps.end(), end_ms + 86399999) - g_data.timestamps.begin());
    }
    if (options.sim_start_index >= options.sim_end_index)
        throw std::invalid_argument("Sim period has no trading bars: simEnd must be on or after simStart");
    if (body.isMember("threads")) {
        const Json::Value& threads = body["threads"];
        if (!threads.isIntegral() || !threads.isInt64() || threads.asInt64() < 1 || threads.asInt64() > 32)
            throw std::invalid_argument("threads must be between 1 and 32");
        options.threads = static_cast<unsigned>(threads.asInt64());
    }
    options.collect_signals = body.get("collectSignals", false).asBool();
    options.metrics_only = body.get("metricsOnly", false).asBool();
    options.signal_series_only = body.get("signalSeriesOnly", false).asBool();
    options.signal_top_k = std::clamp(parse_uint_option(body, "collectSignalsTopK", 1), 1u, 5u);
    const unsigned requested_cells = parse_uint_option(body, "signalPlotMaxCells", 4000000);
    if (requested_cells == 0)
        throw std::invalid_argument("signalPlotMaxCells must be positive");
    options.signal_max_cells = std::min<unsigned>(requested_cells, 8000000);
    if (body.isMember("signalPlotSymbols")) {
        const Json::Value& subset = body["signalPlotSymbols"];
        if (!subset.isArray())
            throw std::invalid_argument("signalPlotSymbols must be an array of panel symbols");
        for (const Json::Value& item : subset) {
            const std::string name = item.asString();
            const auto it = std::find(g_data.symbols.begin(), g_data.symbols.end(), name);
            if (it == g_data.symbols.end())
                throw std::invalid_argument("signalPlotSymbols: unknown symbol " + name);
            options.signal_symbol_subset.push_back(static_cast<std::size_t>(it - g_data.symbols.begin()));
        }
        if (options.signal_symbol_subset.empty())
            throw std::invalid_argument("signalPlotSymbols must not be empty");
    }
    if (options.signal_series_only && options.signal_symbol_subset.empty())
        throw std::invalid_argument("signalSeriesOnly requires signalPlotSymbols");
    options.missing_close_policy = g_missing_close_policy;
    if (body.isMember("missingClose") &&
        !parse_missing_close_policy(body["missingClose"].asString(), &options.missing_close_policy))
        throw std::invalid_argument("missingClose must be 'carry' or 'error'");
    for (const char* key : {"defaultDelistingReturn", "staleQuoteSettleBars",
                            "settlementLog", "distributionLog"})
        if (body.isMember(key))
            throw std::invalid_argument(std::string(key) + " is no longer supported; use a thin-v1 cache");
    if (body.isMember("combinedMode"))
        throw std::invalid_argument("combinedMode was removed; combined alphas are always normalized");
    if (body.isMember("universe") && !body["universe"].isNull()) {
        if (!body["universe"].isString())
            throw std::invalid_argument("universe must be a string");
        options.universe = body["universe"].asString();
        if (!options.universe.empty() && g_data.universe_names.empty())
            throw std::invalid_argument("universe selection requires universe.names in the config");
    }
    options.universe_mask = &g_data.mask_for(options.universe);
    if (options.universe.empty()) options.universe = g_data.default_universe;
    return options;
}

static SimulatorConfig simulator_config(const RunOptions& options, bool collect) {
    SimulatorConfig config;
    config.T = g_data.T;
    config.N = g_data.N;
    config.fee_rate = g_config.transaction_fee_rate;
    config.sim_start_index = options.sim_start_index;
    config.sim_end_index = options.sim_end_index;
    config.collect_signals = collect;
    config.missing_close_policy = options.missing_close_policy;
    config.universe_mask = options.universe_mask;
    config.execution_price = options.execution_price == "ohlc4" ? ExecutionPrice::Ohlc4
        : options.execution_price == "vwap" ? ExecutionPrice::Vwap : ExecutionPrice::Close;
    return config;
}

static SimulatorOutput execute_alpha(
    CompiledAlpha& alpha, const RunOptions& options, bool collect,
    const SimulatorConfig::SignalRowCallback& callback = {},
    const SimulatorConfig::AccountingRowCallback& accounting = {}) {
    auto config = simulator_config(options, collect);
    config.signal_row_callback = callback;
    config.accounting_row_callback = accounting;
    return Simulator(g_data, config).run(alpha.pipeline);
}

static FullMetricsResult full_metrics(const SimulatorOutput& output, const RunOptions& options) {
    const std::size_t first_metric_bar = options.sim_start_index;
    std::vector<double> frozen_gross(output.missing_data.size());
    for (std::size_t di = 0; di < output.missing_data.size(); ++di)
        frozen_gross[di] = output.missing_data[di].frozen_gross;
    return compute_full_metrics(
        output.timestamps, output.ret, output.gross_ret, output.turnover, output.max_weight,
        output.long_weight, output.short_weight, output.position_count,
        options.is_ms, options.os_ms, g_config.bars_per_year, first_metric_bar, &frozen_gross);
}

// Missing quotes freeze held weights; thin ending moves retire their sources.
static Json::Value missing_data_payload(const SimulatorOutput& output) {
    MissingDataDiagnostics total;
    for (const auto& bar : output.missing_data) {
        total.retirement_count += bar.retirement_count;
        total.missing_held_count += bar.missing_held_count;
        total.blocked_order_count += bar.blocked_order_count;
        total.frozen_gross += bar.frozen_gross;
        total.blocked_trade_weight += bar.blocked_trade_weight;
        total.resumed_holding_count += bar.resumed_holding_count;
        total.missing_entry_count += bar.missing_entry_count;
        total.successor_conversions += bar.successor_conversions;
        total.converted_gross += bar.converted_gross;
    }
    Json::Value payload;
    payload["missingHeldBars"] = total.missing_held_count;
    payload["frozenGrossSum"] = total.frozen_gross;
    payload["retirements"] = total.retirement_count;
    payload["blockedOrders"] = total.blocked_order_count;
    payload["blockedTradeWeight"] = total.blocked_trade_weight;
    payload["missingEntries"] = total.missing_entry_count;
    payload["resumedHoldings"] = total.resumed_holding_count;
    payload["successorConversions"] = total.successor_conversions;
    payload["convertedGross"] = total.converted_gross;
    payload["terminalFrozenGross"] = output.missing_data.empty()
        ? 0.0 : output.missing_data.back().frozen_gross;
    return payload;
}

static Json::Value diagnostics_payload(const SimulatorOutput& output, const RunOptions&) {
    Json::Value payload;
    payload["missingData"] = missing_data_payload(output);
    return payload;
}

static Json::Value metrics_payload(const FullMetricsResult& metrics) {
    Json::Value payload;
    payload["summary"]["is"] = summary_to_json(metrics.is_summary);
    payload["summary"]["os"] = summary_to_json(metrics.os_summary);
    payload["yearly"] = yearly_to_json(metrics.yearly);
    payload["timeseries"] = timeseries_to_json(metrics.timeseries);
    payload["isStartIndex"] = static_cast<Json::UInt64>(metrics.is_start_index);
    payload["osStartIndex"] = static_cast<Json::UInt64>(metrics.os_start_index);
    return payload;
}

static void copy_top_metrics(Json::Value& response, const Json::Value& metrics) {
    for (const char* key : {"yearly", "timeseries", "isStartIndex", "osStartIndex", "missingData"})
        if (metrics.isMember(key)) response[key] = metrics[key];
}

static void append_signal_payload(
    Json::Value& payload, const std::vector<std::vector<float>>& signals,
    const std::vector<int64_t>& timestamps, const RunOptions& options) {
    const std::size_t begin = options.sim_start_index;
    // Outputs stop at simEnd while the immutable, shared panel retains its full
    // history. The plot serializer requires a mask matching the output prefix.
    const auto* mask = options.universe_mask;
    std::vector<uint8_t> prefix_mask;
    const auto cells = signals.size() * g_data.N;
    if (mask && !mask->empty() && mask->size() > cells) {
        prefix_mask.assign(mask->begin(), mask->begin() + cells);
        mask = &prefix_mask;
    }
    const bool parallel = cells >= 1000000;
    if (options.signal_series_only) {
        payload["signalPlot"] = signal_plot_to_json(signals, timestamps, g_data.symbols, begin,
            options.signal_max_cells, &options.signal_symbol_subset, mask);
        return;
    }
    const auto policy = parallel ? std::launch::async : std::launch::deferred;
    auto plot = std::async(policy, [&] {
        return signal_plot_to_json(signals, timestamps, g_data.symbols, begin, options.signal_max_cells,
            options.signal_symbol_subset.empty() ? nullptr : &options.signal_symbol_subset, mask);
    });
    auto linear = std::async(policy, [&] {
        return signal_distribution_to_json(signals, begin, &timestamps, &g_data.symbols);
    });
    auto logarithmic = std::async(policy, [&] {
        return signal_log_distribution_to_json(signals, begin, &timestamps, &g_data.symbols);
    });
    payload["signalPlot"] = plot.get();
    payload["distribution"] = linear.get();
    payload["signalDistributionLog"] = logarithmic.get();
    float minimum = 0.0f, maximum = 0.0f;
    bool first = true;
    for (std::size_t di = begin; di < signals.size(); ++di) {
        for (float value : signals[di]) {
            if (!std::isfinite(value)) continue;
            if (first) { minimum = maximum = value; first = false; }
            else { minimum = std::min(minimum, value); maximum = std::max(maximum, value); }
        }
    }
    payload["signalRange"]["min"] = minimum;
    payload["signalRange"]["max"] = maximum;
}

// Shared by /api/simulate and saved alpha results so both produce the same body.
// `accounting` observes a single-alpha run only (saved positions); it does not change the body.
static Json::Value simulate_response(const Json::Value& body, const std::function<bool()>& cancelled,
                                     const SimulatorConfig::AccountingRowCallback& accounting = {}) {
    const RunOptions options = parse_run_options(body);
    const bool combined_only = body.get("combinedOnly", false).asBool();
    std::vector<CompiledAlpha> alphas;
    if (body.isMember("xmls")) {
        const auto& xmls = body["xmls"];
        if (!xmls.isArray() || xmls.empty() || xmls.size() > MAX_ALPHA_COUNT)
            throw std::invalid_argument("xmls must contain 1..5000 concrete alpha XML strings");
        for (const auto& value : xmls) {
            if (!value.isString() || value.asString().empty())
                throw std::invalid_argument("Every xmls entry must be a nonempty XML string");
            auto compiled = compile_xml(value.asString(), &g_data, options.universe_mask);
            if (compiled.size() != 1)
                throw std::invalid_argument("Every xmls entry must compile to exactly one alpha");
            alphas.push_back(std::move(compiled[0]));
        }
    } else {
        const std::string xml = body["xml"].asString();
        if (xml.empty()) throw std::invalid_argument("Missing 'xml' field");
        alphas = compile_xml(xml, &g_data, options.universe_mask);
    }
    if (alphas.empty()) throw std::invalid_argument("No alpha compiled from XML");
    const bool single = alphas.size() == 1;
    if (options.signal_series_only && !single)
        throw std::invalid_argument("signalSeriesOnly requires one concrete alpha XML");

    struct BatchResult {
        std::size_t index = 0;
        std::string xml;
        FullMetricsResult metrics;
        Json::Value diagnostics;
    };
    std::vector<BatchResult> batches(alphas.size());
    SimulatorOutput single_output;
    std::vector<std::vector<float>> combined_signals;
    const auto stop_on_cancel = [&cancelled](unsigned, const float*, unsigned) {
        if (cancelled()) throw EvaluationCancelled();
    };
    if (single && !combined_only) {
        single_output = execute_alpha(alphas[0], options, !options.metrics_only, stop_on_cancel, accounting);
        batches[0] = {0, alphas[0].source_xml, full_metrics(single_output, options),
                      diagnostics_payload(single_output, options)};
    } else {
        OrderedSignalAccumulator accumulator(
            g_data, options.missing_close_policy,
            options.universe_mask);
        const unsigned thread_count = std::min<unsigned>(options.threads, static_cast<unsigned>(alphas.size()));
        std::atomic<std::size_t> next_index{0};
        std::atomic<bool> failed{false};
        std::exception_ptr first_error;
        std::mutex error_mutex;
        auto worker = [&] {
            std::vector<double> normalized_scratch;
            while (!failed.load(std::memory_order_acquire)) {
                const std::size_t i = next_index.fetch_add(1);
                if (i >= alphas.size()) return;
                try {
                    std::vector<float> signal_block;
                    signal_block.reserve(static_cast<size_t>(32) * g_data.N);
                    unsigned first_bar = 0;
                    auto flush = [&] {
                        if (signal_block.empty()) return;
                        accumulator.add_block(i, first_bar, signal_block, g_data.N, normalized_scratch);
                        signal_block.clear();
                    };
                    auto observe = [&](unsigned di, const float* row, unsigned n) {
                        if (failed.load() || cancelled())
                            throw EvaluationCancelled();
                        if (signal_block.empty()) first_bar = di;
                        signal_block.insert(signal_block.end(), row, row + n);
                        if (signal_block.size() == static_cast<size_t>(32) * n) flush();
                    };
                    if (combined_only) {
                        Simulator(g_data, simulator_config(options, false)).run_signal_rows(alphas[i].pipeline, observe);
                    } else {
                        auto output = execute_alpha(alphas[i], options, false, observe);
                        batches[i] = {i, alphas[i].source_xml, full_metrics(output, options),
                                      diagnostics_payload(output, options)};
                    }
                    flush();
                    // Completed module state is no longer needed; selected plots
                    // are recomputed from the stored expanded XML after ranking.
                    alphas[i] = CompiledAlpha{};
                } catch (...) {
                    {
                        std::lock_guard<std::mutex> lock(error_mutex);
                        if (!first_error) first_error = std::current_exception();
                    }
                    failed.store(true, std::memory_order_release);
                    accumulator.cancel();
                    return;
                }
            }
        };
        std::vector<std::thread> workers;
        try {
            for (unsigned t = 0; t < thread_count; ++t)
                workers.emplace_back(worker);
        } catch (...) {
            failed.store(true, std::memory_order_release);
            accumulator.cancel();
            for (auto& worker_thread : workers) worker_thread.join();
            throw;
        }
        for (auto& worker_thread : workers) worker_thread.join();
        if (first_error) std::rethrow_exception(first_error);
        combined_signals = accumulator.take_signals();
    }

    std::stable_sort(batches.begin(), batches.end(), [](const auto& a, const auto& b) {
        const double a_sharpe = std::isfinite(a.metrics.is_summary.sharpe)
            ? a.metrics.is_summary.sharpe : -std::numeric_limits<double>::infinity();
        const double b_sharpe = std::isfinite(b.metrics.is_summary.sharpe)
            ? b.metrics.is_summary.sharpe : -std::numeric_limits<double>::infinity();
        return a_sharpe == b_sharpe ? a.index < b.index : a_sharpe > b_sharpe;
    });

    Json::Value response;
    response["executionPrice"] = options.execution_price;
    response["simStart"] = iso_date_of(g_data.timestamps[options.sim_start_index]);
    response["simEnd"] = iso_date_of(g_data.timestamps[options.sim_end_index - 1]);
    Json::Value batch_array(Json::arrayValue);
    for (std::size_t i = 0; !combined_only && i < batches.size(); ++i) {
        const auto& batch = batches[i];
        Json::Value entry = metrics_payload(batch.metrics);
        for (const auto& key : batch.diagnostics.getMemberNames())
            entry[key] = batch.diagnostics[key];
        entry["index"] = static_cast<Json::UInt64>(batch.index);
        entry["xml"] = batch.xml;
        if (!single && options.collect_signals && !options.metrics_only && i < options.signal_top_k) {
            auto selected = compile_xml(batch.xml, &g_data, options.universe_mask);
            auto output = execute_alpha(selected.at(0), options, true, stop_on_cancel);
            append_signal_payload(entry, output.signals, output.timestamps, options);
        }
        batch_array.append(std::move(entry));
    }
    response["batches"] = std::move(batch_array);
    if (single && !combined_only) {
        copy_top_metrics(response, response["batches"][0]);
        if (!options.metrics_only)
            append_signal_payload(response, single_output.signals, single_output.timestamps, options);
    } else {
        // run_from_signals has no row hook; check once before the portfolio-only replay.
        if (cancelled()) throw EvaluationCancelled();
        Simulator combined_simulator(g_data, simulator_config(options, false));
        auto combined_output = combined_simulator.run_from_signals(combined_signals);
        Json::Value combined = metrics_payload(full_metrics(combined_output, options));
        const Json::Value combined_diagnostics =
            diagnostics_payload(combined_output, options);
        for (const auto& key : combined_diagnostics.getMemberNames())
            combined[key] = combined_diagnostics[key];
        combined["index"] = -1;
        if (options.collect_signals && !options.metrics_only) {
            combined_signals.resize(combined_output.timestamps.size());
            append_signal_payload(combined, combined_signals, combined_output.timestamps, options);
            response["signalPlot"] = combined["signalPlot"];
            response["signalRange"] = combined["signalRange"];
            response["distribution"] = combined["distribution"];
            response["signalDistributionLog"] = combined["signalDistributionLog"];
        }
        copy_top_metrics(response, combined);
        response["combined"] = std::move(combined);
    }
    response["universe"] = options.universe;
    return response;
}

struct AlphaRequestError : std::runtime_error {
    AlphaRequestError(drogon::HttpStatusCode code, const std::string& message)
        : std::runtime_error(message), status(code) {}
    drogon::HttpStatusCode status;
};

static drogon::HttpResponsePtr json_error_response(drogon::HttpStatusCode status, const std::string& message) {
    Json::Value json;
    json["error"] = message;
    auto response = drogon::HttpResponse::newHttpJsonResponse(json);
    response->setStatusCode(status);
    return response;
}

// Saved files use the HTTP serializer settings so their bytes can be served as is.
static std::string json_text(const Json::Value& value) {
    Json::StreamWriterBuilder builder;
    builder["commentStyle"] = "None";
    builder["indentation"] = "";
    builder["emitUTF8"] = true;
    return Json::writeString(builder, value);
}

// Alpha storage requests run off the event loop; results saves replay every row.
template<class Work>
static void run_alpha_request(std::function<void(const drogon::HttpResponsePtr&)>&& callback, Work work) {
    auto respond = std::make_shared<std::function<void(const drogon::HttpResponsePtr&)>>(std::move(callback));
    try {
        g_alpha_workers.launch([respond, work = std::move(work)]() mutable {
            drogon::HttpResponsePtr response;
            try {
                response = work();
            } catch (const AlphaRequestError& error) {
                response = json_error_response(error.status, error.what());
            } catch (const std::exception& error) {
                response = json_error_response(drogon::k500InternalServerError, error.what());
            }
            (*respond)(response);
        });
    } catch (const std::exception& error) {
        (*respond)(json_error_response(drogon::k503ServiceUnavailable, error.what()));
    }
}

static constexpr Json::UInt64 kMaxRowId = (Json::UInt64(1) << 53) - 1; // JavaScript-safe

static const char* const kAlphaMetadataKeys[] = {"simStart", "simEnd", "isStart", "osStart", "universe",
                                                 "executionPrice"};

static Json::Value metadata_json(const alpha_storage::Metadata& metadata) {
    Json::Value json;
    json["simStart"] = metadata.sim_start;
    json["simEnd"] = metadata.sim_end;
    json["isStart"] = metadata.is_start;
    json["osStart"] = metadata.os_start;
    json["universe"] = metadata.universe;
    json["executionPrice"] = metadata.execution_price;
    return json;
}

static drogon::HttpResponsePtr list_alphas_response() {
    Json::Value list(Json::arrayValue);
    std::vector<std::string> skipped;
    for (const auto& alpha : alpha_storage::list(g_alphas_dir, &skipped)) {
        Json::Value item;
        item["name"] = alpha.name;
        item["xml"] = alpha.xml;
        item["level"] = alpha.metadata.level;
        item["metadata"] = metadata_json(alpha.metadata);
        list.append(std::move(item));
    }
    for (const auto& name : skipped)
        LOG_WARN << "Skipping saved alpha with malformed metadata.yml: " << name;
    return drogon::HttpResponse::newHttpJsonResponse(list);
}

static drogon::HttpResponsePtr saved_result_response(const std::string& name, const std::string& file) {
    if (!alpha_storage::is_valid_name(name))
        throw AlphaRequestError(drogon::k400BadRequest, "Invalid alpha name");
    std::string text;
    if (!alpha_storage::read_file(g_alphas_dir / name / "results" / file, &text))
        throw AlphaRequestError(drogon::k404NotFound, "Saved result not found: " + name + "/results/" + file);
    auto response = drogon::HttpResponse::newHttpResponse();
    response->setContentTypeCode(drogon::CT_APPLICATION_JSON);
    response->setBody(std::move(text));
    return response;
}

static drogon::HttpResponsePtr save_alpha(const Json::Value& body) {
    auto invalid = [](const std::string& message) { return AlphaRequestError(drogon::k400BadRequest, message); };
    if (!body.isObject()) throw invalid("Request body must be a JSON object");
    if (!body["name"].isString() || !alpha_storage::is_valid_name(body["name"].asString()))
        throw invalid("Invalid alpha name");
    const std::string name = body["name"].asString();
    if (!body["xml"].isString() || body["xml"].asString().empty()) throw invalid("Missing 'xml' field");
    const std::string xml = body["xml"].asString();
    const Json::Value& level_value = body.isMember("level") ? body["level"] : Json::Value("xml");
    if (!level_value.isString() || !alpha_storage::is_valid_level(level_value.asString()))
        throw invalid("level must be 'xml', 'results' or 'positions'");
    alpha_storage::Metadata metadata;
    metadata.level = level_value.asString();
    const Json::Value& meta = body["metadata"];
    if (!meta.isObject()) throw invalid("Missing 'metadata' object");
    Json::Value run_body(Json::objectValue);
    for (const char* key : kAlphaMetadataKeys) {
        if (!meta[key].isString()) throw invalid(std::string("metadata.") + key + " must be a string");
        if (meta[key].asString().empty() && std::string(key) != "universe")
            throw invalid(std::string("metadata.") + key + " must not be empty");
        run_body[key] = meta[key];
    }
    metadata.sim_start = meta["simStart"].asString();
    metadata.sim_end = meta["simEnd"].asString();
    metadata.is_start = meta["isStart"].asString();
    metadata.os_start = meta["osStart"].asString();
    metadata.universe = meta["universe"].asString();
    metadata.execution_price = meta["executionPrice"].asString();
    RunOptions options;
    try {
        options = parse_run_options(run_body);
    } catch (const std::exception& error) {
        throw invalid(std::string("metadata: ") + error.what());
    }

    struct Row { Json::UInt64 id; Json::Value round; std::string xml; };
    std::vector<Row> rows;
    if (metadata.level != "xml") {
        const Json::Value& items = body["rows"];
        if (!items.isArray() || items.empty() || items.size() > MAX_ALPHA_COUNT)
            throw invalid("rows must contain 1..5000 entries for results and positions");
        std::set<Json::UInt64> ids;
        for (const auto& item : items) {
            if (!item.isObject() || !item["id"].isIntegral() || !item["id"].isUInt64() ||
                item["id"].asUInt64() > kMaxRowId || !ids.insert(item["id"].asUInt64()).second)
                throw invalid("Every row needs a unique integer id in 0..2^53-1");
            const Json::Value round = item.get("round", 0);
            if (!round.isIntegral() || !round.isUInt())
                throw invalid("Row round must be a nonnegative integer");
            if (!item["xml"].isString() || item["xml"].asString().empty())
                throw invalid("Every row needs a nonempty xml string");
            try {
                if (compile_xml(item["xml"].asString(), &g_data, options.universe_mask).size() != 1)
                    throw std::invalid_argument("must compile to exactly one alpha");
            } catch (const std::exception& error) {
                throw invalid("Row " + std::to_string(item["id"].asUInt64()) + " xml: " + error.what());
            }
            rows.push_back({item["id"].asUInt64(), round, item["xml"].asString()});
        }
    }
    if (alpha_storage::exists(g_alphas_dir, name))
        throw AlphaRequestError(drogon::k409Conflict, "Alpha already exists: " + name);

    alpha_storage::SaveTransaction save(g_alphas_dir, name);
    save.write("alpha.xml", xml);
    save.write("metadata.yml", alpha_storage::metadata_yaml(metadata));
    if (!rows.empty()) {
        const auto cancelled = [] { return g_alpha_workers.stopping(); };
        const bool positions = metadata.level == "positions";
        std::vector<Json::Value> entries(rows.size());
        // The request's threads (1..32, default 20) bound the row replays and the batch replay below.
        const unsigned thread_count = std::min<unsigned>(options.threads, static_cast<unsigned>(rows.size()));
        std::atomic<std::size_t> next_index{0};
        std::atomic<bool> failed{false};
        std::exception_ptr first_error;
        std::mutex error_mutex;
        // Each row writes its own files; the index keeps request order.
        auto worker = [&] {
            while (!failed.load(std::memory_order_acquire)) {
                const std::size_t i = next_index.fetch_add(1);
                if (i >= rows.size()) return;
                try {
                    if (cancelled()) throw EvaluationCancelled();
                    const auto& row = rows[i];
                    Json::Value request = run_body;
                    request["xml"] = row.xml;
                    request["metricsOnly"] = true;
                    const std::string id = std::to_string(row.id);
                    // Positions are the order targets (desired_weights) on trading bars,
                    // captured in the same run that produces the row result.
                    std::unique_ptr<alpha_storage::PositionsWriter> writer;
                    SimulatorConfig::AccountingRowCallback accounting;
                    if (positions) {
                        writer = std::make_unique<alpha_storage::PositionsWriter>(g_data.symbols);
                        accounting = [&writer](const AccountingRowView& view) {
                            if (view.trading_enabled)
                                writer->add_bar(g_data.timestamps[view.bar], view.desired_weights);
                        };
                    }
                    const Json::Value response = simulate_response(
                        request, [&] { return failed.load() || cancelled(); }, accounting);
                    save.write(fs::path("results") / "rows" / (id + ".json"), json_text(response));
                    if (writer) writer->write(save.prepare(fs::path("positions") / (id + ".parquet")));
                    Json::Value entry;
                    entry["index"] = row.id;
                    entry["round"] = row.round;
                    entry["xml"] = row.xml;
                    entry["origin"] = "saved";
                    entry["status"] = "completed";
                    entry["summary"] = response["batches"][0]["summary"];
                    entry["yearly"] = response["yearly"];
                    entries[i] = std::move(entry);
                } catch (...) {
                    {
                        std::lock_guard<std::mutex> lock(error_mutex);
                        if (!first_error) first_error = std::current_exception();
                    }
                    failed.store(true, std::memory_order_release);
                    return;
                }
            }
        };
        std::vector<std::thread> workers;
        try {
            for (unsigned t = 0; t < thread_count; ++t) workers.emplace_back(worker);
        } catch (...) {
            failed.store(true, std::memory_order_release);
            for (auto& thread : workers) thread.join();
            throw;
        }
        for (auto& thread : workers) thread.join();
        if (first_error) std::rethrow_exception(first_error);
        if (cancelled()) throw EvaluationCancelled();

        Json::Value index;
        index["rows"] = Json::Value(Json::arrayValue);
        Json::Value batch_request = run_body;
        batch_request["xmls"] = Json::Value(Json::arrayValue);
        batch_request["combinedOnly"] = true;
        batch_request["metricsOnly"] = true;
        batch_request["threads"] = thread_count;
        for (std::size_t i = 0; i < rows.size(); ++i) {
            index["rows"].append(std::move(entries[i]));
            batch_request["xmls"].append(rows[i].xml);
        }
        index["hasBatch"] = rows.size() >= 2;
        if (rows.size() >= 2)
            save.write(fs::path("results") / "batch.json", json_text(simulate_response(batch_request, cancelled)));
        save.write(fs::path("results") / "index.json", json_text(index));
    }
    if (!save.commit())
        throw AlphaRequestError(drogon::k409Conflict, "Alpha already exists: " + name);

    Json::Value json;
    json["name"] = name;
    json["saved"] = true;
    json["level"] = metadata.level;
    json["path"] = (g_alphas_dir / name).string();
    return drogon::HttpResponse::newHttpJsonResponse(json);
}

int main(int argc, char* argv[])
{
    // Config path: first arg or default
    std::string config_path = (argc > 1) ? argv[1] : "../../config.yaml";

    // Load config + data. A bad config or an unreadable dataset is a startup
    // failure with a readable message, not an uncaught exception.
    fs::path cfg_dir;
    try {
        std::cout << "Loading config from " << config_path << " ..." << std::endl;
        g_config = BacktestConfig::from_yaml(config_path);

        cfg_dir = fs::path(config_path).parent_path();
        std::cout << "Loading configured datasets ..." << std::endl;
        g_data = DataPanel::load(g_config);
        std::cout << "Data loaded: T=" << g_data.T << " N=" << g_data.N << std::endl;
        if (!g_config.symbol_labels_path.empty()) {
            std::ifstream in(g_config.symbol_labels_path);
            if (!in) throw std::runtime_error("Cannot open symbol_labels_path: " + g_config.symbol_labels_path);
            Json::Value all;
            in >> all;
            if (!all.isObject()) throw std::runtime_error("symbol_labels_path must hold a JSON object");
            std::size_t matched = 0;
            for (const auto& sym : g_data.symbols)
                if (all.isMember(sym)) { g_symbol_labels[sym] = all[sym]; ++matched; }
            std::cout << "Symbol labels: " << matched << "/" << g_data.N << " panel symbols labeled" << std::endl;
        } else if (g_config.base_thin) {
            const auto labels = parquet_reader::load_thin_symbol_labels(g_config, g_data.symbols);
            for (const auto& [id, label] : labels) g_symbol_labels[id] = label;
            std::cout << "Symbol labels: " << labels.size() << "/" << g_data.N
                      << " panel symbols labeled from thin master" << std::endl;
        }
    } catch (const std::exception& error) {
        std::cerr << "Cannot start: " << error.what() << std::endl;
        return 1;
    }

    // config_loader rejects any other spelling before the panel loads.
    if (!parse_missing_close_policy(g_config.missing_close_policy, &g_missing_close_policy)) {
        std::cerr << "Unsupported missing_close_policy: "
                  << g_config.missing_close_policy << std::endl;
        return 1;
    }
    g_alphas_dir = g_config.alphas_dir;
    try {
        alpha_storage::prepare_directory(g_alphas_dir);
    } catch (const std::exception& error) {
        std::cerr << "Cannot start: " << error.what() << std::endl;
        return 1;
    }

    // CORS: allow any origin (React frontend)
    drogon::app().registerPreRoutingAdvice(
        [](const drogon::HttpRequestPtr &req,
           drogon::FilterCallback &&stop,
           drogon::FilterChainCallback &&pass) {
            if (req->method() == drogon::Options) {
                auto resp = drogon::HttpResponse::newHttpResponse();
                resp->addHeader("Access-Control-Allow-Origin", "*");
                resp->addHeader("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
                resp->addHeader("Access-Control-Allow-Headers", "Content-Type");
                resp->addHeader("Access-Control-Max-Age", "86400");
                stop(resp);
                return;
            }
            pass();
        });

    drogon::app().registerPostHandlingAdvice(
        [](const drogon::HttpRequestPtr &, const drogon::HttpResponsePtr &resp) {
            resp->addHeader("Access-Control-Allow-Origin", "*");
        });

    // GET /api/health
    drogon::app().registerHandler(
        "/api/health",
        [](const drogon::HttpRequestPtr &,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            Json::Value json;
            json["status"] = "ok";
            json["engine"] = "cpp-engine";
            callback(drogon::HttpResponse::newHttpJsonResponse(json));
        },
        {drogon::Get});

    // GET /api/config
    drogon::app().registerHandler(
        "/api/config",
        [](const drogon::HttpRequestPtr &,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            Json::Value json;

            // Symbols
            Json::Value symbols(Json::arrayValue);
            for (const auto& s : g_data.symbols)
                symbols.append(s);
            json["symbols"] = symbols;
            if (!g_symbol_labels.isNull())
                json["symbolLabels"] = g_symbol_labels;  // {panelSymbol: display label}

            // Date range (epoch_ms -> ISO date string)
            // First and last timestamps from DataPanel
            auto ms_to_date = [](int64_t ms) -> std::string {
                time_t sec = ms / 1000;
                struct tm t;
                gmtime_r(&sec, &t);
                char buf[11];
                strftime(buf, sizeof(buf), "%Y-%m-%d", &t);
                return buf;
            };

            if (g_data.timestamps.empty()) {
                Json::Value err;
                err["error"] = "Data panel has no timestamps";
                auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(drogon::k500InternalServerError);
                callback(resp);
                return;
            }

            json["simStart"] = ms_to_date(g_data.timestamps.at(g_data.trade_start_index));
            json["simEnd"]   = ms_to_date(g_data.timestamps.back());
            json["supportsSimEnd"] = true;
            json["feeRate"]  = g_config.transaction_fee_rate;
            json["barsPerYear"] = g_config.bars_per_year;
            json["executionPrice"] = g_config.execution_price;
            json["executionPrices"] = Json::Value(Json::arrayValue);
            json["executionPrices"].append("close");
            if (supports_ohlc4()) json["executionPrices"].append("ohlc4");
            if (supports_vwap()) json["executionPrices"].append("vwap");
            json["missingClosePolicy"] = missing_close_policy_name(g_missing_close_policy);
            json["thinLayout"] = g_data.thin_layout;
            json["alphasDir"] = g_alphas_dir.string();
            Json::Value universes(Json::arrayValue);
            for (const auto& name : g_data.universe_names) universes.append(name);
            json["universes"] = universes;  // empty: a single configured universe
            json["defaultUniverse"] = g_data.default_universe;
            json["T"] = g_data.T;
            json["N"] = g_data.N;

            // Available columns
            Json::Value columns(Json::arrayValue);
            for (const auto& c : g_data.column_names())
                columns.append(c);
            json["columns"] = columns;
            // Resident float-field storage only: excludes masks, modules, plots and process overhead.
            json["loadedFields"] = static_cast<Json::UInt64>(g_data.loaded_field_count());
            json["loadedDataBytes"] = static_cast<Json::UInt64>(g_data.loaded_bytes());

            callback(drogon::HttpResponse::newHttpJsonResponse(json));
        },
        {drogon::Get});

    // POST /api/simulate — one or more expanded alphas
    drogon::app().registerHandler(
        "/api/simulate",
        [](const drogon::HttpRequestPtr& req,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback) {
            // Evaluate off the event loop: a long combined replay must not stall
            // other requests whose connections share this loop.
            auto respond = std::make_shared<std::function<void(const drogon::HttpResponsePtr&)>>(
                std::move(callback));
            try {
                g_simulate_workers.launch([req, respond] {
                    const auto& callback = *respond;
                    try {
                        const auto body = req->getJsonObject();
                        if (!body) throw std::invalid_argument("Invalid JSON body");
                        const Json::Value response = simulate_response(
                            *body, [&req] { return g_simulate_workers.stopping() || !req->connected(); });
                        callback(drogon::HttpResponse::newHttpJsonResponse(response));
                    } catch (const std::exception& error) {
                        Json::Value json;
                        json["error"] = error.what();
                        auto response = drogon::HttpResponse::newHttpJsonResponse(json);
                        response->setStatusCode(drogon::k400BadRequest);
                        callback(response);
                    }
                });
            } catch (const std::exception& error) {
                Json::Value json;
                json["error"] = error.what();
                auto response = drogon::HttpResponse::newHttpJsonResponse(json);
                response->setStatusCode(drogon::k503ServiceUnavailable);
                (*respond)(response);
            }
        }, {drogon::Post});

    // POST /api/ga/stream — final best alpha uses the same period and plot contract
    drogon::app().registerHandler(
        "/api/ga/stream",
        [](const drogon::HttpRequestPtr& req,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback) {
            try {
                const auto body = req->getJsonObject();
                if (!body) throw std::invalid_argument("Invalid JSON body");
                const std::string xml = (*body)["xml"].asString();
                if (xml.empty()) throw std::invalid_argument("Missing 'xml' field");
                const RunOptions options = parse_run_options(*body);
                GAConfig ga_config;
                ga_config.first_search = parse_uint_option(*body, "firstSearch", parse_uint_option(*body, "popSize", 200));
                ga_config.population_size = parse_uint_option(*body, "roundSize", 50);
                ga_config.generations = parse_uint_option(*body, "stopRound", parse_uint_option(*body, "generations", 40));
                ga_config.stop_alphas = parse_uint_option(*body, "stopAlphas", 400);
                ga_config.n_threads = options.threads;
                if (!ga_config.first_search || ga_config.first_search > 5000 ||
                    !ga_config.population_size || ga_config.population_size > 5000 ||
                    !ga_config.generations || ga_config.generations > 1000 ||
                    !ga_config.stop_alphas || ga_config.stop_alphas > 5000)
                    throw std::invalid_argument("GA search/round size and stopAlphas must be 1..5000, stopRound 1..1000");
                const bool stream_candidates = body->get("streamCandidates", false).asBool();
                auto response = drogon::HttpResponse::newAsyncStreamResponse(
                    [xml, options, ga_config, req, stream_candidates](drogon::ResponseStreamPtr stream) {
                        // The I/O loop must remain free to receive cancellation and
                        // flush generation events while the GA controller is running.
                        try {
                            g_ga_workers.launch([xml, options, ga_config, req, stream_candidates, stream = std::move(stream)]() mutable {
                                auto require_connection = [&] {
                                    if (g_ga_workers.stopping() || !req->connected()) throw ClientDisconnected();
                                };
                                auto write = [&stream, &require_connection](const Json::Value& value) {
                                    require_connection();
                                    Json::StreamWriterBuilder builder;
                                    builder["indentation"] = "";
                                    if (!stream->send(Json::writeString(builder, value) + "\n"))
                                        throw ClientDisconnected();
                                };
                                try {
                                    const auto space = extract_search_space(xml);
                                    std::mutex payload_mutex;
                                    std::unordered_map<std::string, Json::Value> payloads;
                                    auto metadata = [&] {
                                        Json::Value value;
                                        value["simStart"] = iso_date_of(g_data.timestamps[options.sim_start_index]);
                                        value["simEnd"] = iso_date_of(g_data.timestamps[options.sim_end_index - 1]);
                                        value["executionPrice"] = options.execution_price;
                                        value["universe"] = options.universe;
                                        return value;
                                    };
                                    if (stream_candidates) {
                                        auto event = metadata();
                                        event["type"] = "search_start";
                                        write(event);
                                    }
                                    auto evaluate = [&](const std::string& candidate_xml) {
                                        require_connection();
                                        auto alphas = compile_xml(candidate_xml, &g_data, options.universe_mask);
                                        if (alphas.size() != 1) throw std::runtime_error("GA candidate must compile to exactly one alpha");
                                        auto output = execute_alpha(alphas[0], options, false,
                                            [&](unsigned, const float*, unsigned) { require_connection(); });
                                        const auto metrics = full_metrics(output, options);
                                        if (stream_candidates) {
                                            Json::Value payload;
                                            payload["summary"]["is"] = summary_to_json(metrics.is_summary);
                                            payload["summary"]["os"] = summary_to_json(metrics.os_summary);
                                            payload["yearly"] = yearly_to_json(metrics.yearly);
                                            payload["missingData"] = missing_data_payload(output);
                                            std::lock_guard<std::mutex> lock(payload_mutex);
                                            payloads.emplace(candidate_xml, std::move(payload));
                                        }
                                        return metrics.is_summary.sharpe;
                                    };
                                    GAEvents events;
                                    events.cancelled = [&] { return g_ga_workers.stopping() || !req->connected(); };
                                    events.round_start = [&](unsigned round, size_t count) {
                                        if (!stream_candidates) return;
                                        Json::Value event;
                                        event["type"] = "round_start";
                                        event["round"] = round;
                                        event["size"] = static_cast<Json::UInt64>(count);
                                        write(event);
                                    };
                                    events.candidate = [&](const GACandidate& candidate) {
                                        if (!stream_candidates) return;
                                        Json::Value payload;
                                        if (candidate.error.empty()) {
                                            std::lock_guard<std::mutex> lock(payload_mutex);
                                            auto found = payloads.find(candidate.xml);
                                            if (found == payloads.end()) throw std::runtime_error("Missing evaluated candidate payload");
                                            payload = std::move(found->second);
                                            payloads.erase(found);
                                        }
                                        payload["index"] = static_cast<Json::UInt64>(candidate.id);
                                        payload["xml"] = candidate.xml;
                                        payload["round"] = candidate.round;
                                        payload["origin"] = candidate.origin;
                                        payload["status"] = candidate.error.empty() ? "completed" : "failed";
                                        if (!candidate.error.empty()) payload["error"] = candidate.error;
                                        Json::Value event;
                                        event["type"] = "candidate";
                                        event["candidate"] = std::move(payload);
                                        write(event);
                                    };
                                    events.round_end = [&](unsigned round, const std::string& best_xml, double score) {
                                        Json::Value event;
                                        event["type"] = stream_candidates ? "round_end" : "generation_end";
                                        event["round"] = round;
                                        event["generation"] = round - 1;
                                        event["totalGenerations"] = ga_config.generations;
                                        event["bestSharpe"] = std::isfinite(score) ? Json::Value(score) : Json::Value(Json::nullValue);
                                        event["bestXml"] = best_xml;
                                        write(event);
                                    };
                                    GAEngine engine(xml, space, ga_config);
                                    const auto best = engine.run_detailed(evaluate, events);
                                    if (stream_candidates) {
                                        auto event = metadata();
                                        event["type"] = "search_complete";
                                        event["bestXml"] = best.best_xml;
                                        event["stopReason"] = best.stop_reason;
                                        event["evaluated"] = static_cast<Json::UInt64>(best.evaluated);
                                        write(event);
                                        stream->close();
                                        return;
                                    }
                                    require_connection();
                                    auto alphas = compile_xml(best.best_xml, &g_data, options.universe_mask);
                                    if (alphas.empty()) throw std::runtime_error("Best GA alpha did not compile");
                                    auto output = execute_alpha(alphas[0], options, true,
                                        [&](unsigned, const float*, unsigned) { require_connection(); });
                                    Json::Value batch = metrics_payload(full_metrics(output, options));
                                    const Json::Value diagnostics = diagnostics_payload(output, options);
                                    for (const auto& key : diagnostics.getMemberNames())
                                        batch[key] = diagnostics[key];
                                    batch["index"] = 0;
                                    batch["xml"] = best.best_xml;
                                    append_signal_payload(batch, output.signals, output.timestamps, options);
                                    Json::Value complete;
                                    complete["type"] = "complete";
                                    complete["bestXml"] = best.best_xml;
                                    complete["universe"] = options.universe;
                                    complete["executionPrice"] = options.execution_price;
                                    complete["simStart"] = iso_date_of(g_data.timestamps[options.sim_start_index]);
                                    complete["simEnd"] = iso_date_of(g_data.timestamps[options.sim_end_index - 1]);
                                    copy_top_metrics(complete, batch);
                                    complete["signalPlot"] = batch["signalPlot"];
                                    complete["signalRange"] = batch["signalRange"];
                                    complete["distribution"] = batch["distribution"];
                                    complete["signalDistributionLog"] = batch["signalDistributionLog"];
                                    complete["batches"] = Json::Value(Json::arrayValue);
                                    complete["batches"].append(std::move(batch));
                                    write(complete);
                                } catch (const ClientDisconnected&) {
                                    // The caller has cancelled; do not evaluate another generation.
                                } catch (const std::exception& error) {
                                    Json::Value event;
                                    event["type"] = "error";
                                    event["error"] = error.what();
                                    try { write(event); } catch (const ClientDisconnected&) {}
                                }
                                stream->close();
                            });
                        } catch (const std::exception& error) {
                            // A failed launch destroys the owned stream and closes it.
                            LOG_ERROR << "Unable to start GA worker: " << error.what();
                        }
                    }, true);
                response->addHeader("Content-Type", "application/x-ndjson");
                callback(response);
            } catch (const std::exception& error) {
                Json::Value json;
                json["error"] = error.what();
                auto response = drogon::HttpResponse::newHttpJsonResponse(json);
                response->setStatusCode(drogon::k400BadRequest);
                callback(response);
            }
        }, {drogon::Post});

    // GET/POST /api/alphas — list or save alphas
    drogon::app().registerHandler(
        "/api/alphas",
        [](const drogon::HttpRequestPtr& req,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback) {
            run_alpha_request(std::move(callback), [req] {
                if (req->method() == drogon::Get) return list_alphas_response();
                const auto body = req->getJsonObject();
                if (!body) throw AlphaRequestError(drogon::k400BadRequest, "Invalid JSON body");
                return save_alpha(*body);
            });
        },
        {drogon::Get, drogon::Post});

    // DELETE /api/alphas/{name}
    drogon::app().registerHandler(
        "/api/alphas/{name}",
        [](const drogon::HttpRequestPtr&,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback,
           const std::string& name) {
            run_alpha_request(std::move(callback), [name] {
                if (!alpha_storage::is_valid_name(name))
                    throw AlphaRequestError(drogon::k400BadRequest, "Invalid alpha name");
                if (!alpha_storage::remove(g_alphas_dir, name))
                    throw AlphaRequestError(drogon::k404NotFound, "Alpha not found: " + name);
                Json::Value json;
                json["name"] = name;
                json["deleted"] = true;
                return drogon::HttpResponse::newHttpJsonResponse(json);
            });
        },
        {drogon::Delete});

    // GET /api/alphas/{name}/results[/batch|/rows/{id}] — saved result files
    drogon::app().registerHandler(
        "/api/alphas/{name}/results",
        [](const drogon::HttpRequestPtr&,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback,
           const std::string& name) {
            run_alpha_request(std::move(callback), [name] { return saved_result_response(name, "index.json"); });
        },
        {drogon::Get});
    drogon::app().registerHandler(
        "/api/alphas/{name}/results/batch",
        [](const drogon::HttpRequestPtr&,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback,
           const std::string& name) {
            run_alpha_request(std::move(callback), [name] { return saved_result_response(name, "batch.json"); });
        },
        {drogon::Get});
    drogon::app().registerHandler(
        "/api/alphas/{name}/results/rows/{id}",
        [](const drogon::HttpRequestPtr&,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback,
           const std::string& name, const std::string& id) {
            run_alpha_request(std::move(callback), [name, id] {
                const bool numeric = !id.empty() && id.size() <= 19 &&
                    std::all_of(id.begin(), id.end(), [](unsigned char ch) { return std::isdigit(ch); });
                if (!numeric || std::stoull(id) > kMaxRowId)
                    throw AlphaRequestError(drogon::k400BadRequest, "Invalid row id");
                return saved_result_response(name, "rows/" + std::to_string(std::stoull(id)) + ".json");
            });
        },
        {drogon::Get});

    const unsigned listen_port = parse_env_uint("LONGSHORT_PORT", 8000, 1, 65535);

    std::cout << "LongShort C++ server starting on port " << listen_port << "..." << std::endl;

    drogon::app()
        .setUploadPath((cfg_dir / ".longshort" / "uploads").string())
        .setLogLevel(trantor::Logger::kWarn)
        .addListener("0.0.0.0", static_cast<uint16_t>(listen_port))
        .setThreadNum(4)
        // Long saves and combined replays answer after minutes; keep the connection bounded but open.
        .setIdleConnectionTimeout(parse_env_uint("LONGSHORT_IDLE_TIMEOUT", 3600, 1, 86400))
        .run();

    g_ga_workers.stop_and_join();
    g_simulate_workers.stop_and_join();
    g_alpha_workers.stop_and_join();
    return 0;
}
