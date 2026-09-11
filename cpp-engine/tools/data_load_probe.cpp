#include "data/config_loader.h"
#include "data/data_panel.h"
#include <json/json.h>
#include <chrono>
#include <cmath>
#include <iostream>
#include <stdexcept>
#include <sys/resource.h>

int main(int argc, char** argv) {
    try {
        if (argc < 2) throw std::runtime_error("usage: data_load_probe CONFIG [--summary] [FIELD ...]");
        bool summary = false;
        std::vector<std::string> fields;
        for (int i = 2; i < argc; ++i) {
            if (std::string(argv[i]) == "--summary") summary = true;
            else fields.push_back(argv[i]);
        }
        if (fields.empty()) fields.push_back("close");
        const auto started = std::chrono::steady_clock::now();
        const auto config = BacktestConfig::from_yaml(argv[1]);
        auto panel = DataPanel::load(config);
        const auto eager_done = std::chrono::steady_clock::now();
        Json::Value result;
        result["T"] = panel.T;
        result["N"] = panel.N;
        result["tradeStartIndex"] = panel.trade_start_index;
        result["registeredFields"] = Json::UInt64(panel.column_names().size());
        result["eagerLoadedFields"] = Json::UInt64(panel.loaded_field_count());
        result["eagerLoadedBytes"] = Json::UInt64(panel.loaded_bytes());
        for (const auto& field : fields) {
            if (!panel.has_column(field)) throw std::runtime_error("Unknown probe field: " + field);
            for (unsigned t = 0; t < panel.T; ++t) {
                const float* row = panel.row(field, t);
                if (!summary) for (unsigned n = 0; n < panel.N; ++n)
                    result["values"][field].append(std::isfinite(row[n]) ? Json::Value(row[n]) : Json::Value(Json::nullValue));
            }
        }
        if (!summary) {
            for (auto timestamp : panel.timestamps) result["timestamps"].append(Json::Int64(timestamp));
            for (const auto& symbol : panel.symbols) result["symbols"].append(symbol);
            for (auto member : panel.universe_mask) result["universeMask"].append(static_cast<unsigned>(member));
        }
        const auto finished = std::chrono::steady_clock::now();
        result["loadedFields"] = Json::UInt64(panel.loaded_field_count());
        result["loadedBytes"] = Json::UInt64(panel.loaded_bytes());
        result["eagerLoadMs"] = std::chrono::duration<double, std::milli>(eager_done-started).count();
        result["totalLoadMs"] = std::chrono::duration<double, std::milli>(finished-started).count();
        rusage usage{};
        if (getrusage(RUSAGE_SELF, &usage) == 0) result["peakRssBytes"] = Json::UInt64(usage.ru_maxrss) * 1024;
        Json::StreamWriterBuilder writer;
        writer["indentation"] = "";
        std::cout << Json::writeString(writer, result) << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "data_load_probe: " << error.what() << '\n';
        return 1;
    }
}
