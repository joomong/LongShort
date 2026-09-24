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
#include "engine/costs.h"
#include "metrics/metrics_engine.h"
#include "api/json_serializer.h"
#include "api/combined_signals.h"
#include "ga/search_space.h"
#include "ga/ga_engine.h"
#include <thread>
#include <fstream>

namespace fs = std::filesystem;

// Global shared state (loaded once at startup)
static BacktestConfig g_config;
static Json::Value g_symbol_labels;  // optional {panel symbol: display label}
static DataPanel g_data;
static fs::path g_alphas_dir;
static MissingClosePolicy g_missing_close_policy = MissingClosePolicy::Carry;
static BorrowCostModel g_borrow;   // backtest.costs.borrow, loaded once with the data

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

static bool is_valid_alpha_name(const std::string& name)
{
    if (name.empty())
        return false;
    for (unsigned char ch : name) {
        if (std::isalnum(ch) || ch == '_' || ch == '-' || ch == '.')
            continue;
        return false;
    }
    return true;
}

static bool is_under_dir(const fs::path& base, const fs::path& target)
{
    const fs::path canonical_base = fs::weakly_canonical(base);
    const fs::path canonical_target = fs::weakly_canonical(target);

    auto base_it = canonical_base.begin();
    auto base_end = canonical_base.end();
    auto target_it = canonical_target.begin();
    auto target_end = canonical_target.end();

    for (; base_it != base_end && target_it != target_end; ++base_it, ++target_it) {
        if (*base_it != *target_it)
            return false;
    }
    return base_it == base_end;
}

static bool resolve_alpha_path(const std::string& name, fs::path* out_path)
{
    if (!is_valid_alpha_name(name))
        return false;

    const fs::path candidate = g_alphas_dir / (name + ".xml");
    if (!is_under_dir(g_alphas_dir, candidate))
        return false;

    *out_path = candidate;
    return true;
}

// Keep GA workers owned until completion, and join them before shared data is
// destroyed at shutdown. Completed workers are reaped when another run starts.
class GaWorkers {
public:
    ~GaWorkers() { stop_and_join(); }

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
            tasks_.back().thread = std::thread([work = std::move(work), done]() mutable {
                try { work(); }
                catch (const std::exception& error) { LOG_ERROR << "GA worker: " << error.what(); }
                catch (...) { LOG_ERROR << "GA worker failed"; }
                done->store(true, std::memory_order_release);
            });
        } catch (...) {
            tasks_.pop_back();
            throw;
        }
    }

private:
    struct Task { std::thread thread; std::shared_ptr<std::atomic<bool>> done; };
    std::vector<Task> tasks_;
    std::mutex mutex_;
    std::atomic<bool> stopping_{false};
};
static GaWorkers g_ga_workers;

class ClientDisconnected : public std::runtime_error {
public:
    ClientDisconnected() : std::runtime_error("Client disconnected") {}
};

struct RunOptions {
    int64_t is_ms = 0;
    int64_t os_ms = 0;
    unsigned sim_start_index = 0;
    int mode = 1;
    unsigned threads = 0;
    bool collect_signals = false;
    unsigned signal_top_k = 1;
    std::size_t signal_max_cells = 4000000;
    std::vector<std::size_t> signal_symbol_subset;  // non-empty: full series for these columns only
    std::string combined_mode;
    MissingClosePolicy missing_close_policy = MissingClosePolicy::Carry;
};

static RunOptions parse_run_options(const Json::Value& body) {
    RunOptions options;
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
    const Json::Value requested_mode = body.get("mode", g_config.mode);
    if (!requested_mode.isInt() || requested_mode.asInt() < 0 || requested_mode.asInt() > 1)
        throw std::invalid_argument("mode must be 0 (D0) or 1 (D1)");
    options.mode = requested_mode.asInt();
    options.threads = parse_uint_option(body, "threads", 0);
    options.collect_signals = body.get("collectSignals", false).asBool();
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
    options.combined_mode = body.get("combinedMode", g_config.combined_mode).asString();
    if (options.combined_mode != "normalized" && options.combined_mode != "raw")
        throw std::invalid_argument("combinedMode must be 'normalized' or 'raw'");
    options.missing_close_policy = g_missing_close_policy;
    if (body.isMember("missingClose") &&
        !parse_missing_close_policy(body["missingClose"].asString(), &options.missing_close_policy))
        throw std::invalid_argument("missingClose must be 'carry' or 'error'");
    for (const char* key : {"defaultDelistingReturn", "staleQuoteSettleBars",
                            "settlementLog", "distributionLog"})
        if (body.isMember(key))
            throw std::invalid_argument(std::string(key) + " is no longer supported; use a thin-v1 cache");
    return options;
}

static SimulatorConfig simulator_config(const RunOptions& options, bool collect) {
    SimulatorConfig config;
    config.T = g_data.T;
    config.N = g_data.N;
    config.fee_rate = g_config.transaction_fee_rate;
    config.mode = options.mode;
    config.sim_start_index = options.sim_start_index;
    config.collect_signals = collect;
    config.missing_close_policy = options.missing_close_policy;
    config.borrow = g_borrow.enabled() ? &g_borrow : nullptr;
    return config;
}

static SimulatorOutput execute_alpha(
    CompiledAlpha& alpha, const RunOptions& options, bool collect,
    const SimulatorConfig::SignalRowCallback& callback = {}) {
    auto config = simulator_config(options, collect);
    config.signal_row_callback = callback;
    return Simulator(g_data, config).run(alpha.pipeline);
}

static FullMetricsResult full_metrics(const SimulatorOutput& output, const RunOptions& options) {
    // Preserve the global D1 warmup convention when no earlier signal exists.
    const std::size_t first_metric_bar = options.mode == 1
        ? std::max<unsigned>(options.sim_start_index, 1) : options.sim_start_index;
    std::vector<double> frozen_gross(output.missing_data.size());
    for (std::size_t di = 0; di < output.missing_data.size(); ++di)
        frozen_gross[di] = output.missing_data[di].frozen_gross;
    return compute_full_metrics(
        output.timestamps, output.ret, output.turnover, output.max_weight,
        output.long_weight, output.short_weight, output.position_count,
        options.is_ms, options.os_ms, g_config.transaction_fee_rate,
        g_config.bars_per_year, first_metric_bar, &frozen_gross, &output.borrow_cost);
}

// Cost modules: what was charged and, when asked, what was not (P9: a run without
// financing must say so instead of looking free).
static Json::Value costs_payload(const SimulatorOutput& output) {
    double borrow = 0.0;
    unsigned unrated = 0;
    for (double cost : output.borrow_cost) borrow += cost;
    for (unsigned count : output.borrow_unrated) unrated += count;
    Json::Value payload;
    payload["borrowEnabled"] = g_borrow.enabled();
    payload["borrowApplied"] = g_borrow.applied();
    payload["borrowCost"] = borrow;
    if (g_config.costs.report_unapplied) {
        payload["borrowUnratedShortBars"] = unrated;
        if (!g_borrow.applied())
            payload["borrowUnapplied"] = g_borrow.enabled() ? "no rates: neither a table row nor a default rate"
                                                            : "disabled";
    }
    return payload;
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
    payload["costs"] = costs_payload(output);
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
    for (const char* key : {"yearly", "timeseries", "isStartIndex", "osStartIndex", "missingData", "costs"})
        if (metrics.isMember(key)) response[key] = metrics[key];
}

static void append_signal_payload(
    Json::Value& payload, const std::vector<std::vector<float>>& signals,
    const std::vector<int64_t>& timestamps, const RunOptions& options) {
    const std::size_t begin = options.sim_start_index;
    payload["signalPlot"] = signal_plot_to_json(
        signals, timestamps, g_data.symbols, begin, options.signal_max_cells,
        options.signal_symbol_subset.empty() ? nullptr : &options.signal_symbol_subset);
    payload["distribution"] = signal_distribution_to_json(signals, begin, &timestamps, &g_data.symbols);
    payload["signalDistributionLog"] = signal_log_distribution_to_json(signals, begin, &timestamps, &g_data.symbols);
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
        g_borrow = BorrowCostModel::load(g_config.costs, g_data, g_config.config_dir);
        if (g_borrow.enabled())
            std::cout << "Borrow cost: " << (g_borrow.applied() ? "applied" : "enabled but no rates")
                      << ", day count " << g_borrow.day_count() << ", default rate "
                      << (g_borrow.has_default_rate() ? std::to_string(g_borrow.default_rate()) : "none")
                      << ", table rows " << g_borrow.table_rows() << " (" << g_borrow.table_rows_matched()
                      << " on panel symbols, " << g_borrow.symbols_rated() << " symbols rated)" << std::endl;
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
    // Alphas directory (relative to config.yaml)
    g_alphas_dir = cfg_dir / "alphas";
    fs::create_directories(g_alphas_dir);

    // CORS: allow any origin (React frontend)
    drogon::app().registerPreRoutingAdvice(
        [](const drogon::HttpRequestPtr &req,
           drogon::FilterCallback &&stop,
           drogon::FilterChainCallback &&pass) {
            if (req->method() == drogon::Options) {
                auto resp = drogon::HttpResponse::newHttpResponse();
                resp->addHeader("Access-Control-Allow-Origin", "*");
                resp->addHeader("Access-Control-Allow-Methods", "GET, POST, OPTIONS");
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
            json["feeRate"]  = g_config.transaction_fee_rate;
            json["barsPerYear"] = g_config.bars_per_year;
            json["combinedMode"] = g_config.combined_mode;
            json["mode"] = g_config.mode;
            json["missingClosePolicy"] = missing_close_policy_name(g_missing_close_policy);
            json["thinLayout"] = g_data.thin_layout;
            // Cost modules: the borrow block as configured plus the rate table's coverage.
            Json::Value borrow;
            borrow["enabled"] = g_borrow.enabled();
            borrow["applied"] = g_borrow.applied();
            borrow["defaultRate"] = g_borrow.has_default_rate()
                ? Json::Value(g_borrow.default_rate()) : Json::Value(Json::nullValue);
            borrow["dayCount"] = g_borrow.day_count();
            borrow["path"] = g_borrow.table_path().empty()
                ? Json::Value(Json::nullValue) : Json::Value(g_borrow.table_path());
            borrow["tableRows"] = static_cast<Json::UInt64>(g_borrow.table_rows());
            borrow["tableRowsMatched"] = static_cast<Json::UInt64>(g_borrow.table_rows_matched());
            borrow["symbolsRated"] = static_cast<Json::UInt64>(g_borrow.symbols_rated());
            Json::Value costs;
            costs["borrow"] = borrow;
            costs["reportUnapplied"] = g_config.costs.report_unapplied;
            json["costs"] = costs;
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
            try {
                const auto body = req->getJsonObject();
                if (!body) throw std::invalid_argument("Invalid JSON body");
                const std::string xml = (*body)["xml"].asString();
                if (xml.empty()) throw std::invalid_argument("Missing 'xml' field");
                const RunOptions options = parse_run_options(*body);
                auto alphas = compile_xml(xml, &g_data);
                if (alphas.empty()) throw std::invalid_argument("No alpha compiled from XML");
                const bool single = alphas.size() == 1;

                struct BatchResult {
                    std::size_t index = 0;
                    std::string xml;
                    FullMetricsResult metrics;
                    Json::Value diagnostics;
                };
                std::vector<BatchResult> batches(alphas.size());
                SimulatorOutput single_output;
                std::vector<std::vector<float>> combined_signals;
                if (single) {
                    single_output = execute_alpha(alphas[0], options, true);
                    batches[0] = {0, alphas[0].source_xml, full_metrics(single_output, options),
                                  diagnostics_payload(single_output, options)};
                } else {
                    OrderedSignalAccumulator accumulator(
                        g_data, options.combined_mode, options.missing_close_policy);
                    unsigned hardware = std::max(1u, std::thread::hardware_concurrency());
                    const unsigned thread_count = std::max(1u, std::min<unsigned>(
                        options.threads == 0 ? hardware : options.threads,
                        static_cast<unsigned>(alphas.size())));
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
                                auto output = execute_alpha(alphas[i], options, false,
                                    [&](unsigned di, const float* row, unsigned n) {
                                        accumulator.add(i, di, row, n, normalized_scratch);
                                    });
                                batches[i] = {i, alphas[i].source_xml, full_metrics(output, options),
                                              diagnostics_payload(output, options)};
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
                Json::Value batch_array(Json::arrayValue);
                for (std::size_t i = 0; i < batches.size(); ++i) {
                    const auto& batch = batches[i];
                    Json::Value entry = metrics_payload(batch.metrics);
                    for (const auto& key : batch.diagnostics.getMemberNames())
                        entry[key] = batch.diagnostics[key];
                    entry["index"] = static_cast<Json::UInt64>(batch.index);
                    entry["xml"] = batch.xml;
                    if (!single && options.collect_signals && i < options.signal_top_k) {
                        auto selected = compile_xml(batch.xml, &g_data);
                        auto output = execute_alpha(selected.at(0), options, true);
                        append_signal_payload(entry, output.signals, output.timestamps, options);
                    }
                    batch_array.append(std::move(entry));
                }
                response["batches"] = std::move(batch_array);
                if (single) {
                    copy_top_metrics(response, response["batches"][0]);
                    append_signal_payload(response, single_output.signals, single_output.timestamps, options);
                } else {
                    Simulator combined_simulator(g_data, simulator_config(options, false));
                    auto combined_output = combined_simulator.run_from_signals(combined_signals);
                    Json::Value combined = metrics_payload(full_metrics(combined_output, options));
                    const Json::Value combined_diagnostics =
                        diagnostics_payload(combined_output, options);
                    for (const auto& key : combined_diagnostics.getMemberNames())
                        combined[key] = combined_diagnostics[key];
                    combined["index"] = -1;
                    if (options.collect_signals) {
                        append_signal_payload(combined, combined_signals, g_data.timestamps, options);
                        response["signalPlot"] = combined["signalPlot"];
                        response["signalRange"] = combined["signalRange"];
                        response["distribution"] = combined["distribution"];
                        response["signalDistributionLog"] = combined["signalDistributionLog"];
                    }
                    copy_top_metrics(response, combined);
                    response["combined"] = std::move(combined);
                }
                response["combinedMode"] = options.combined_mode;
                response["mode"] = options.mode;
                callback(drogon::HttpResponse::newHttpJsonResponse(response));
            } catch (const std::exception& error) {
                Json::Value json;
                json["error"] = error.what();
                auto response = drogon::HttpResponse::newHttpJsonResponse(json);
                response->setStatusCode(drogon::k400BadRequest);
                callback(response);
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
                ga_config.population_size = parse_uint_option(*body, "popSize", 50);
                ga_config.generations = parse_uint_option(*body, "generations", 20);
                ga_config.n_threads = options.threads == 0 ? 4 : options.threads;
                if (ga_config.population_size == 0 || ga_config.generations == 0)
                    throw std::invalid_argument("GA population and generations must be positive");
                auto response = drogon::HttpResponse::newAsyncStreamResponse(
                    [xml, options, ga_config, req](drogon::ResponseStreamPtr stream) {
                        // The I/O loop must remain free to receive cancellation and
                        // flush generation events while the GA controller is running.
                        try {
                            g_ga_workers.launch([xml, options, ga_config, req, stream = std::move(stream)]() mutable {
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
                                    if (space.genes.empty() || space.total_combinations() <= 1)
                                        throw std::invalid_argument("No search dimensions found in XML");
                                    auto evaluate = [&](const std::string& candidate_xml) {
                                        if (g_ga_workers.stopping() || !req->connected()) return -1e18;
                                        try {
                                            auto alphas = compile_xml(candidate_xml, &g_data);
                                            if (alphas.empty()) return -1e18;
                                            auto output = execute_alpha(alphas[0], options, false,
                                                [&](unsigned, const float*, unsigned) { require_connection(); });
                                            return full_metrics(output, options).is_summary.sharpe;
                                        } catch (...) { return -1e18; }
                                    };
                                    GAEngine engine(xml, space, ga_config);
                                    auto progress = [&](unsigned generation, const std::string& best_xml, double fitness) {
                                        Json::Value event;
                                        event["type"] = "generation_end";
                                        event["generation"] = generation;
                                        event["totalGenerations"] = ga_config.generations;
                                        event["bestSharpe"] = fitness;
                                        event["bestXml"] = best_xml;
                                        write(event);
                                    };
                                    const auto best = engine.run_streaming(evaluate, progress);
                                    require_connection();
                                    auto alphas = compile_xml(best.best_xml, &g_data);
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
                                    complete["combinedMode"] = options.combined_mode;
                                    complete["mode"] = options.mode;
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
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            if (req->method() == drogon::Get) {
                // List all saved alphas
                Json::Value arr(Json::arrayValue);
                if (fs::exists(g_alphas_dir)) {
                    for (const auto& entry : fs::directory_iterator(g_alphas_dir)) {
                        if (entry.path().extension() == ".xml") {
                            std::ifstream f(entry.path());
                            std::string content((std::istreambuf_iterator<char>(f)),
                                                 std::istreambuf_iterator<char>());
                            Json::Value item;
                            item["name"] = entry.path().stem().string();
                            item["xml"] = content;
                            arr.append(item);
                        }
                    }
                }
                callback(drogon::HttpResponse::newHttpJsonResponse(arr));
            } else {
                // POST: save a new alpha
                auto body = req->getJsonObject();
                if (!body || !body->isMember("name") || !body->isMember("xml")) {
                    Json::Value err;
                    err["error"] = "Missing 'name' or 'xml'";
                    auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                    resp->setStatusCode(drogon::k400BadRequest);
                    callback(resp);
                    return;
                }
                std::string name = (*body)["name"].asString();
                std::string xml = (*body)["xml"].asString();
                fs::path fpath;
                if (!resolve_alpha_path(name, &fpath)) {
                    Json::Value err;
                    err["error"] = "Invalid alpha name";
                    auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                    resp->setStatusCode(drogon::k400BadRequest);
                    callback(resp);
                    return;
                }

                std::ofstream f(fpath);
                f << xml;
                f.close();

                Json::Value json;
                json["name"] = name;
                json["saved"] = true;
                callback(drogon::HttpResponse::newHttpJsonResponse(json));
            }
        },
        {drogon::Get, drogon::Post});

    // DELETE /api/alphas/{name}
    drogon::app().registerHandler(
        "/api/alphas/{name}",
        [](const drogon::HttpRequestPtr &,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback,
           const std::string &name) {
            fs::path fpath;
            if (!resolve_alpha_path(name, &fpath)) {
                Json::Value err;
                err["error"] = "Invalid alpha name";
                auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(drogon::k400BadRequest);
                callback(resp);
                return;
            }
            if (!fs::exists(fpath)) {
                Json::Value err;
                err["error"] = "Alpha not found: " + name;
                auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(drogon::k404NotFound);
                callback(resp);
                return;
            }
            fs::remove(fpath);
            Json::Value json;
            json["name"] = name;
            json["deleted"] = true;
            callback(drogon::HttpResponse::newHttpJsonResponse(json));
        },
        {drogon::Delete});

    const unsigned listen_port = parse_env_uint("LONGSHORT_PORT", 8000, 1, 65535);

    std::cout << "LongShort C++ server starting on port " << listen_port << "..." << std::endl;

    drogon::app()
        .setLogLevel(trantor::Logger::kWarn)
        .addListener("0.0.0.0", static_cast<uint16_t>(listen_port))
        .setThreadNum(4)
        .run();

    g_ga_workers.stop_and_join();
    return 0;
}
