#include <drogon/drogon.h>
#include <iostream>
#include <filesystem>
#include <ctime>
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

            // Parse ISO date -> epoch_ms
            auto date_to_ms = [](const std::string& s) -> int64_t {
                struct tm t = {};
                strptime(s.c_str(), "%Y-%m-%d", &t);
                return static_cast<int64_t>(timegm(&t)) * 1000;
            };

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

            if (xml.empty()) {
                Json::Value err;
                err["error"] = "Missing 'xml' field";
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

                int64_t is_ms = date_to_ms(is_start_str);
                int64_t os_ms = date_to_ms(os_start_str);

                bool single = (alphas.size() == 1);

                // Helper: run one alpha through simulator + metrics
                auto run_one = [&](CompiledAlpha& alpha, bool collect) {
                    std::vector<Module*> pipeline;
                    pipeline.reserve(alpha.modules.size());
                    for (auto& m : alpha.modules)
                        pipeline.push_back(m.get());

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

                if (single) {
                    // Single alpha: full response with signalPlot
                    auto [sim_out, metrics] = run_one(alphas[0], true);
                    auto json = single_alpha_response(metrics, sim_out, g_data.symbols);
                    callback(drogon::HttpResponse::newHttpJsonResponse(json));
                } else {
                    // Multi-alpha batch (STAR: batches array, best=index 0)
                    // Run all alphas, find best by OS sharpe
                    struct BatchResult {
                        int index;
                        SummaryMetrics is_summary;
                        SummaryMetrics os_summary;
                    };

                    std::vector<BatchResult> batches;
                    int best_idx = 0;
                    double best_sharpe = -1e18;
                    SimulatorOutput best_sim_out;
                    FullMetricsResult best_metrics;

                    for (size_t i = 0; i < alphas.size(); ++i) {
                        auto [sim_out, metrics] = run_one(alphas[i], false);

                        BatchResult br;
                        br.index = static_cast<int>(i);
                        br.is_summary = metrics.is_summary;
                        br.os_summary = metrics.os_summary;
                        batches.push_back(br);

                        if (metrics.is_summary.sharpe > best_sharpe) {
                            best_sharpe = metrics.is_summary.sharpe;
                            best_idx = static_cast<int>(i);
                            best_sim_out = std::move(sim_out);
                            best_metrics = std::move(metrics);
                        }
                    }

                    // Reorder: best first
                    Json::Value json;
                    Json::Value batches_arr(Json::arrayValue);
                    // Best first
                    {
                        Json::Value b;
                        b["index"] = best_idx;
                        Json::Value summary;
                        summary["is"] = summary_to_json(batches[best_idx].is_summary);
                        summary["os"] = summary_to_json(batches[best_idx].os_summary);
                        b["summary"] = summary;
                        batches_arr.append(b);
                    }
                    // Rest
                    for (size_t i = 0; i < batches.size(); ++i) {
                        if (static_cast<int>(i) == best_idx) continue;
                        Json::Value b;
                        b["index"] = batches[i].index;
                        Json::Value summary;
                        summary["is"] = summary_to_json(batches[i].is_summary);
                        summary["os"] = summary_to_json(batches[i].os_summary);
                        b["summary"] = summary;
                        batches_arr.append(b);
                    }
                    json["batches"] = batches_arr;

                    // Yearly + timeseries from best
                    json["yearly"] = yearly_to_json(best_metrics.yearly);
                    json["timeseries"] = timeseries_to_json(best_metrics.timeseries);
                    json["isStartIndex"] = static_cast<Json::UInt64>(best_metrics.is_start_index);
                    json["osStartIndex"] = static_cast<Json::UInt64>(best_metrics.os_start_index);

                    callback(drogon::HttpResponse::newHttpJsonResponse(json));
                }

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

            auto date_to_ms = [](const std::string& s) -> int64_t {
                struct tm t = {};
                strptime(s.c_str(), "%Y-%m-%d", &t);
                return static_cast<int64_t>(timegm(&t)) * 1000;
            };

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

            // Async stream response for NDJSON
            auto resp = drogon::HttpResponse::newAsyncStreamResponse(
                [xml, is_start_str, os_start_str, pop_size, generations,
                 n_threads, date_to_ms](drogon::ResponseStreamPtr stream) {

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

                        int64_t is_ms = date_to_ms(is_start_str);
                        int64_t os_ms = date_to_ms(os_start_str);

                        // Eval function: compile XML -> simulate -> return IS sharpe
                        auto eval_fn = [&](const std::string& candidate_xml) -> double {
                            try {
                                auto alphas = compile_xml(candidate_xml, &g_data);
                                if (alphas.empty()) return -1e18;

                                std::vector<Module*> pipeline;
                                for (auto& m : alphas[0].modules)
                                    pipeline.push_back(m.get());

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
                            std::vector<Module*> pipeline;
                            for (auto& m : alphas[0].modules)
                                pipeline.push_back(m.get());

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

                            Json::Value summary;
                            summary["is"] = summary_to_json(metrics.is_summary);
                            summary["os"] = summary_to_json(metrics.os_summary);
                            complete["summary"] = summary;
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
                fs::path fpath = g_alphas_dir / (name + ".xml");

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
            fs::path fpath = g_alphas_dir / (name + ".xml");
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

    std::cout << "LongShort C++ server starting on port 8000..." << std::endl;

    drogon::app()
        .setLogLevel(trantor::Logger::kWarn)
        .addListener("0.0.0.0", 8000)
        .setThreadNum(4)
        .run();

    return 0;
}
