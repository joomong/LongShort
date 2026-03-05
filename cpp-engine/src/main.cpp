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
#include "data/config_loader.h"
#include "data/data_panel.h"
#include "data/parquet_reader.h"
#include "compiler/dag_builder.h"
#include "engine/simulator.h"
#include "metrics/metrics_engine.h"
#include "api/json_serializer.h"
#include "ga/search_space.h"
#include "ga/ga_engine.h"
#include <thread>
#include <fstream>

namespace fs = std::filesystem;

// Global shared state (loaded once at startup)
static BacktestConfig g_config;
static DataPanel g_data;
static fs::path g_alphas_dir;

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

int main(int argc, char* argv[])
{
    // Config path: first arg or default
    std::string config_path = (argc > 1) ? argv[1] : "../../config.yaml";

    // Load config + data
    std::cout << "Loading config from " << config_path << " ..." << std::endl;
    g_config = BacktestConfig::from_yaml(config_path);

    // Resolve parquet path relative to config.yaml's directory
    fs::path cfg_dir = fs::path(config_path).parent_path();
    std::string parquet_path = (cfg_dir / g_config.parquet_path).string();

    std::cout << "Loading data from " << parquet_path << " ..." << std::endl;
    g_data = DataPanel::load(
        parquet_path,
        g_config.use_columns,
        g_config.time_col,
        g_config.symbol_col);
    std::cout << "Data loaded: T=" << g_data.T << " N=" << g_data.N << std::endl;

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

            json["simStart"] = ms_to_date(g_data.timestamps.front());
            json["simEnd"]   = ms_to_date(g_data.timestamps.back());
            json["feeRate"]  = g_config.transaction_fee_rate;
            json["barsPerYear"] = 365;
            json["T"] = g_data.T;
            json["N"] = g_data.N;

            // Available columns
            Json::Value columns(Json::arrayValue);
            for (const auto& c : g_config.use_columns)
                columns.append(c);
            json["columns"] = columns;

            callback(drogon::HttpResponse::newHttpJsonResponse(json));
        },
        {drogon::Get});

    // POST /api/simulate — single alpha
    drogon::app().registerHandler(
        "/api/simulate",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {

            auto body = req->getJsonObject();
            if (!body) {
                Json::Value err;
                err["error"] = "Invalid JSON body";
                auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(drogon::k400BadRequest);
                callback(resp);
                return;
            }

            std::string xml = (*body)["xml"].asString();
            std::string is_start_str = (*body).get("isStart", "2021-01-01").asString();
            std::string os_start_str = (*body).get("osStart", "2024-01-01").asString();
            unsigned req_threads = (*body).get("threads", 0).asUInt();

            if (xml.empty()) {
                Json::Value err;
                err["error"] = "Missing 'xml' field";
                auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(drogon::k400BadRequest);
                callback(resp);
                return;
            }

            int64_t is_ms = 0;
            int64_t os_ms = 0;
            if (!parse_iso_date_ms(is_start_str, &is_ms) || !parse_iso_date_ms(os_start_str, &os_ms)) {
                Json::Value err;
                err["error"] = "Invalid date format; expected YYYY-MM-DD";
                auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(drogon::k400BadRequest);
                callback(resp);
                return;
            }
            if (is_ms >= os_ms) {
                Json::Value err;
                err["error"] = "isStart must be earlier than osStart";
                auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(drogon::k400BadRequest);
                callback(resp);
                return;
            }

            try {
                // 1. Compile XML -> module pipelines
                auto alphas = compile_xml(xml, &g_data);
                if (alphas.empty()) {
                    Json::Value err;
                    err["error"] = "No alpha compiled from XML";
                    auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                    resp->setStatusCode(drogon::k400BadRequest);
                    callback(resp);
                    return;
                }

                bool single = (alphas.size() == 1);

                // Helper: run one alpha through simulator + metrics
                auto run_one = [&](CompiledAlpha& alpha, bool collect) {
                    const auto& pipeline = alpha.pipeline;

                    SimulatorConfig sim_cfg;
                    sim_cfg.T = g_data.T;
                    sim_cfg.N = g_data.N;
                    sim_cfg.fee_rate = g_config.transaction_fee_rate;
                    sim_cfg.mode = 1;  // D1
                    sim_cfg.bars_per_year = 365;
                    sim_cfg.collect_signals = collect;

                    Simulator sim(g_data, sim_cfg);
                    auto sim_out = sim.run(pipeline);

                    auto metrics = compute_full_metrics(
                        sim_out.timestamps, sim_out.ret, sim_out.turnover,
                        sim_out.min_bet, sim_out.max_weight,
                        sim_out.long_weight, sim_out.short_weight,
                        sim_out.nonzero_count,
                        is_ms, os_ms,
                        g_config.transaction_fee_rate, 365);

                    return std::make_pair(std::move(sim_out), std::move(metrics));
                };

                // Unified path: always build batches[]
                struct BatchResult {
                    int index;
                    std::string xml;
                    FullMetricsResult metrics;
                };

                std::vector<BatchResult> batches(alphas.size());
                SimulatorOutput single_sim_out;  // kept only for signal data
                std::vector<std::vector<std::vector<float>>> all_signals;

                auto assign_batch = [&](size_t i, FullMetricsResult&& metrics) {
                    BatchResult br;
                    br.index = static_cast<int>(i);
                    br.xml = alphas[i].source_xml;
                    br.metrics = std::move(metrics);
                    batches[i] = std::move(br);
                };

                if (single) {
                    auto [sim_out, metrics] = run_one(alphas[0], true);
                    single_sim_out = std::move(sim_out);
                    assign_batch(0, std::move(metrics));
                } else {
                    unsigned hw = std::thread::hardware_concurrency();
                    if (hw == 0)
                        hw = 1;
                    unsigned n_threads = (req_threads == 0) ? hw : req_threads;
                    n_threads = std::max(1u, std::min<unsigned>(n_threads, static_cast<unsigned>(alphas.size())));

                    all_signals.resize(alphas.size());

                    std::atomic<size_t> next_idx{0};
                    std::atomic<bool> has_err{false};
                    std::mutex err_mu;
                    std::string first_err;

                    auto worker = [&]() {
                        while (true) {
                            if (has_err.load(std::memory_order_acquire))
                                return;

                            const size_t i = next_idx.fetch_add(1);
                            if (i >= alphas.size())
                                return;
                            try {
                                auto [sim_out, metrics] = run_one(alphas[i], true);
                                all_signals[i] = std::move(sim_out.signals);
                                assign_batch(i, std::move(metrics));
                            } catch (const std::exception& e) {
                                std::lock_guard<std::mutex> lock(err_mu);
                                if (first_err.empty()) {
                                    first_err = e.what();
                                    has_err.store(true, std::memory_order_release);
                                }
                                return;
                            }
                        }
                    };

                    std::vector<std::thread> workers;
                    workers.reserve(n_threads);
                    for (unsigned t = 0; t < n_threads; ++t)
                        workers.emplace_back(worker);
                    for (auto& th : workers)
                        th.join();

                    if (!first_err.empty())
                        throw std::runtime_error(first_err);
                }

                auto sharpe_key = [](const BatchResult& b) {
                    const double s = b.metrics.is_summary.sharpe;
                    return std::isfinite(s) ? s : -std::numeric_limits<double>::infinity();
                };

                std::stable_sort(
                    batches.begin(), batches.end(),
                    [&](const BatchResult& a, const BatchResult& b) {
                        const double sa = sharpe_key(a);
                        const double sb = sharpe_key(b);
                        if (sa == sb)
                            return a.index < b.index;
                        return sa > sb;
                    });

                if (batches.empty()) {
                    Json::Value err;
                    err["error"] = "No batch results";
                    auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                    resp->setStatusCode(drogon::k400BadRequest);
                    callback(resp);
                    return;
                }

                for (const auto& b : batches) {
                    if (b.xml.empty()) {
                        Json::Value err;
                        err["error"] = "Invalid batch result";
                        auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                        resp->setStatusCode(drogon::k400BadRequest);
                        callback(resp);
                        return;
                    }
                }

                // Combined alpha: sum signals + run separate simulation
                FullMetricsResult combined_metrics;
                if (!single) {
                    const unsigned T = g_data.T;
                    const unsigned N = g_data.N;
                    std::vector<std::vector<float>> combined_signals(T,
                        std::vector<float>(N, std::numeric_limits<float>::quiet_NaN()));

                    for (size_t a = 0; a < all_signals.size(); ++a) {
                        const auto& alpha_sigs = all_signals[a];
                        for (unsigned di = 0; di < T; ++di) {
                            for (unsigned ii = 0; ii < N; ++ii) {
                                float s = alpha_sigs[di][ii];
                                if (!std::isfinite(s)) continue;
                                float& c = combined_signals[di][ii];
                                c = std::isfinite(c) ? c + s : s;
                            }
                        }
                    }

                    SimulatorConfig combined_cfg;
                    combined_cfg.T = T;
                    combined_cfg.N = N;
                    combined_cfg.fee_rate = g_config.transaction_fee_rate;
                    combined_cfg.mode = 1;
                    combined_cfg.bars_per_year = 365;
                    combined_cfg.collect_signals = false;

                    Simulator combined_sim(g_data, combined_cfg);
                    auto combined_out = combined_sim.run_from_signals(combined_signals);

                    combined_metrics = compute_full_metrics(
                        combined_out.timestamps, combined_out.ret, combined_out.turnover,
                        combined_out.min_bet, combined_out.max_weight,
                        combined_out.long_weight, combined_out.short_weight,
                        combined_out.nonzero_count,
                        is_ms, os_ms, g_config.transaction_fee_rate, 365);
                }

                // Build JSON
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

                for (const auto& b : batches)
                    append_batch(b);
                json["batches"] = batches_arr;

                // Top-level: combined metrics for multi-alpha, best for single
                const auto& top_metrics = single ? batches[0].metrics : combined_metrics;
                json["yearly"] = yearly_to_json(top_metrics.yearly);
                json["timeseries"] = timeseries_to_json(top_metrics.timeseries);
                json["isStartIndex"] = static_cast<Json::UInt64>(top_metrics.is_start_index);
                json["osStartIndex"] = static_cast<Json::UInt64>(top_metrics.os_start_index);

                if (!single) {
                    Json::Value cj;
                    Json::Value cs;
                    cs["is"] = summary_to_json(combined_metrics.is_summary);
                    cs["os"] = summary_to_json(combined_metrics.os_summary);
                    cj["summary"] = cs;
                    cj["yearly"] = yearly_to_json(combined_metrics.yearly);
                    cj["timeseries"] = timeseries_to_json(combined_metrics.timeseries);
                    cj["isStartIndex"] = static_cast<Json::UInt64>(combined_metrics.is_start_index);
                    cj["osStartIndex"] = static_cast<Json::UInt64>(combined_metrics.os_start_index);
                    json["combined"] = cj;
                }

                // Single alpha: add signal data at top level
                if (single && !single_sim_out.signals.empty()) {
                    json["signalPlot"] = signal_plot_to_json(
                        single_sim_out.signals, single_sim_out.timestamps, g_data.symbols);

                    float sig_min = 0, sig_max = 0;
                    bool first = true;
                    for (const auto& row : single_sim_out.signals) {
                        for (float v : row) {
                            if (!std::isfinite(v)) continue;
                            if (first) { sig_min = sig_max = v; first = false; }
                            else { if (v < sig_min) sig_min = v; if (v > sig_max) sig_max = v; }
                        }
                    }
                    Json::Value sr;
                    sr["min"] = sig_min;
                    sr["max"] = sig_max;
                    json["signalRange"] = sr;

                    json["distribution"] = signal_distribution_to_json(single_sim_out.signals);
                }

                callback(drogon::HttpResponse::newHttpJsonResponse(json));

            } catch (const std::exception& e) {
                Json::Value err;
                err["error"] = e.what();
                auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(drogon::k400BadRequest);
                callback(resp);
            }
        },
        {drogon::Post});

    // POST /api/ga/stream — NDJSON streaming GA optimization
    drogon::app().registerHandler(
        "/api/ga/stream",
        [](const drogon::HttpRequestPtr &req,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {

            auto body = req->getJsonObject();
            if (!body) {
                Json::Value err;
                err["error"] = "Invalid JSON body";
                auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(drogon::k400BadRequest);
                callback(resp);
                return;
            }

            std::string xml = (*body)["xml"].asString();
            std::string is_start_str = (*body).get("isStart", "2021-01-01").asString();
            std::string os_start_str = (*body).get("osStart", "2024-01-01").asString();
            unsigned pop_size = (*body).get("popSize", 50).asUInt();
            unsigned generations = (*body).get("generations", 20).asUInt();
            unsigned n_threads = (*body).get("threads", 4).asUInt();

            if (xml.empty()) {
                Json::Value err;
                err["error"] = "Missing 'xml' field";
                auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(drogon::k400BadRequest);
                callback(resp);
                return;
            }

            int64_t is_ms = 0;
            int64_t os_ms = 0;
            if (!parse_iso_date_ms(is_start_str, &is_ms) || !parse_iso_date_ms(os_start_str, &os_ms)) {
                Json::Value err;
                err["error"] = "Invalid date format; expected YYYY-MM-DD";
                auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(drogon::k400BadRequest);
                callback(resp);
                return;
            }
            if (is_ms >= os_ms) {
                Json::Value err;
                err["error"] = "isStart must be earlier than osStart";
                auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(drogon::k400BadRequest);
                callback(resp);
                return;
            }

            // Async stream response for NDJSON
            auto resp = drogon::HttpResponse::newAsyncStreamResponse(
                [xml, pop_size, generations, n_threads, is_ms, os_ms](drogon::ResponseStreamPtr stream) {

                    auto write_json = [&stream](const Json::Value& j) {
                        Json::StreamWriterBuilder wb;
                        wb["indentation"] = "";
                        std::string line = Json::writeString(wb, j) + "\n";
                        stream->send(line);
                    };

                    try {
                        auto space = extract_search_space(xml);

                        if (space.genes.empty() || space.total_combinations() <= 1) {
                            Json::Value err;
                            err["type"] = "error";
                            err["error"] = "No search dimensions found in XML";
                            write_json(err);
                            stream->close();
                            return;
                        }

                        // Eval function: compile XML -> simulate -> return IS sharpe
                        auto eval_fn = [&](const std::string& candidate_xml) -> double {
                            try {
                                auto alphas = compile_xml(candidate_xml, &g_data);
                                if (alphas.empty()) return -1e18;

                                const auto& pipeline = alphas[0].pipeline;

                                SimulatorConfig sim_cfg;
                                sim_cfg.T = g_data.T;
                                sim_cfg.N = g_data.N;
                                sim_cfg.fee_rate = g_config.transaction_fee_rate;
                                sim_cfg.mode = 1;
                                sim_cfg.bars_per_year = 365;
                                sim_cfg.collect_signals = false;

                                Simulator sim(g_data, sim_cfg);
                                auto sim_out = sim.run(pipeline);

                                auto metrics = compute_full_metrics(
                                    sim_out.timestamps, sim_out.ret, sim_out.turnover,
                                    sim_out.min_bet, sim_out.max_weight,
                                    sim_out.long_weight, sim_out.short_weight,
                                    sim_out.nonzero_count,
                                    is_ms, os_ms,
                                    g_config.transaction_fee_rate, 365);

                                return metrics.is_summary.sharpe;
                            } catch (...) {
                                return -1e18;
                            }
                        };

                        GAConfig ga_cfg;
                        ga_cfg.population_size = pop_size;
                        ga_cfg.generations = generations;
                        ga_cfg.n_threads = n_threads;

                        GAEngine engine(xml, space, ga_cfg);

                        // Progress callback: emit NDJSON per generation
                        auto progress_cb = [&](unsigned gen, const std::string& best_xml, double fitness) {
                            Json::Value ev;
                            ev["type"] = "generation_end";
                            ev["generation"] = gen;
                            ev["totalGenerations"] = generations;
                            ev["bestSharpe"] = fitness;
                            ev["bestXml"] = best_xml;
                            write_json(ev);
                        };

                        auto result = engine.run_streaming(eval_fn, progress_cb);

                        // Final: run best through full metrics for complete response
                        auto alphas = compile_xml(result.best_xml, &g_data);
                        if (!alphas.empty()) {
                            const auto& pipeline = alphas[0].pipeline;

                            SimulatorConfig sim_cfg;
                            sim_cfg.T = g_data.T;
                            sim_cfg.N = g_data.N;
                            sim_cfg.fee_rate = g_config.transaction_fee_rate;
                            sim_cfg.mode = 1;
                            sim_cfg.bars_per_year = 365;
                            sim_cfg.collect_signals = false;

                            Simulator sim(g_data, sim_cfg);
                            auto sim_out = sim.run(pipeline);

                            auto metrics = compute_full_metrics(
                                sim_out.timestamps, sim_out.ret, sim_out.turnover,
                                sim_out.min_bet, sim_out.max_weight,
                                sim_out.long_weight, sim_out.short_weight,
                                sim_out.nonzero_count,
                                is_ms, os_ms,
                                g_config.transaction_fee_rate, 365);

                            Json::Value complete;
                            complete["type"] = "complete";
                            complete["bestXml"] = result.best_xml;

                            // Wrap in batches[] for unified format
                            Json::Value batch;
                            batch["index"] = 0;
                            batch["xml"] = result.best_xml;
                            Json::Value summary;
                            summary["is"] = summary_to_json(metrics.is_summary);
                            summary["os"] = summary_to_json(metrics.os_summary);
                            batch["summary"] = summary;
                            batch["yearly"] = yearly_to_json(metrics.yearly);
                            batch["timeseries"] = timeseries_to_json(metrics.timeseries);
                            batch["isStartIndex"] = static_cast<Json::UInt64>(metrics.is_start_index);
                            batch["osStartIndex"] = static_cast<Json::UInt64>(metrics.os_start_index);

                            Json::Value batches_arr(Json::arrayValue);
                            batches_arr.append(batch);
                            complete["batches"] = batches_arr;

                            complete["yearly"] = yearly_to_json(metrics.yearly);
                            complete["timeseries"] = timeseries_to_json(metrics.timeseries);
                            complete["isStartIndex"] = static_cast<Json::UInt64>(metrics.is_start_index);
                            complete["osStartIndex"] = static_cast<Json::UInt64>(metrics.os_start_index);

                            write_json(complete);
                        }

                    } catch (const std::exception& e) {
                        Json::Value err;
                        err["type"] = "error";
                        err["error"] = e.what();
                        Json::StreamWriterBuilder wb;
                        wb["indentation"] = "";
                        stream->send(Json::writeString(wb, err) + "\n");
                    }

                    stream->close();
                },
                true);  // disableKickoffTimeout = true (long-running)

            resp->addHeader("Content-Type", "application/x-ndjson");
            callback(resp);
        },
        {drogon::Post});

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

    return 0;
}
