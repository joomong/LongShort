#include "config_loader.h"
#include <yaml-cpp/yaml.h>
#include <algorithm>
#include <cmath>
#include <ctime>
#include <filesystem>
#include <limits>
#include <regex>
#include <set>
#include <stdexcept>

namespace {
FieldAlignment alignment(const YAML::Node& node) {
    const std::string value = node ? node.as<std::string>() : "exact";
    if (value == "exact") return FieldAlignment::Exact;
    if (value == "asof") return FieldAlignment::AsOf;
    throw std::runtime_error("alignment must be 'exact' or 'asof'");
}
unsigned unsigned_option(const YAML::Node& node, unsigned fallback, bool positive) {
    if (!node) return fallback;
    const auto value = node.as<long long>();
    if (value < (positive ? 1 : 0) || static_cast<unsigned long long>(value) > std::numeric_limits<unsigned>::max())
        throw std::runtime_error("Integer config option is outside the supported range");
    return static_cast<unsigned>(value);
}
void merge_fields(std::map<std::string, std::string>& fields, const YAML::Node& node) {
    if (!node) return;
    if (!node.IsMap()) throw std::runtime_error("fields must map public names to source columns/files");
    const std::regex identifier("[A-Za-z_][A-Za-z0-9_]*");
    for (const auto& entry : node) {
        const auto name = entry.first.as<std::string>();
        const auto source = entry.second.as<std::string>();
        if (name == "returns")
            throw std::runtime_error("Field 'returns' is reserved for close-derived returns; use 'holding_return' or another name");
        if (!std::regex_match(name, identifier) || source.empty())
            throw std::runtime_error("Invalid field mapping: " + name);
        auto previous = fields.find(name);
        if (previous != fields.end() && previous->second != source)
            throw std::runtime_error("Conflicting field mapping: " + name);
        fields[name] = source;
    }
}
void parse_symbols(std::vector<std::string>& symbols, const YAML::Node& node) {
    if (!node || !node.IsSequence()) throw std::runtime_error("universe symbols must be a sequence");
    std::set<std::string> seen;
    for (const auto& item : node) {
        auto symbol = item.as<std::string>();
        if (symbol.empty() || !seen.insert(symbol).second)
            throw std::runtime_error("Empty or duplicate universe symbol: " + symbol);
        symbols.push_back(symbol);
    }
    if (seen.count("all")) {
        if (seen.size() != 1) throw std::runtime_error("universe 'all' cannot be combined with symbols");
        symbols.clear();
    }
}
}

int64_t config_date_ms(const std::string& value) {
    if (!std::regex_match(value, std::regex("[0-9]{4}-[0-9]{2}-[0-9]{2}")))
        throw std::runtime_error("Date must use YYYY-MM-DD: " + value);
    std::tm date{};
    date.tm_year = std::stoi(value.substr(0, 4)) - 1900;
    date.tm_mon = std::stoi(value.substr(5, 2)) - 1;
    date.tm_mday = std::stoi(value.substr(8, 2));
    const auto expected = date;
    time_t seconds = timegm(&date);
    if (date.tm_year != expected.tm_year || date.tm_mon != expected.tm_mon || date.tm_mday != expected.tm_mday)
        throw std::runtime_error("Invalid calendar date: " + value);
    return static_cast<int64_t>(seconds) * 1000;
}

BacktestConfig BacktestConfig::from_yaml(const std::string& yaml_path) {
    const YAML::Node root = YAML::LoadFile(yaml_path);
    BacktestConfig cfg;
    cfg.config_dir = std::filesystem::absolute(yaml_path).parent_path().lexically_normal().string();
    const auto datasets = root["datasets"];
    if (!datasets || !datasets.IsSequence() || datasets.size() == 0)
        throw std::runtime_error("config.yaml: datasets must be a non-empty sequence");
    std::set<std::string> all_fields, dataset_names;
    for (const auto& ds : datasets) {
        if (!ds.IsMap()) throw std::runtime_error("Each dataset must be a mapping");
        DatasetConfig dataset;
        dataset.name = ds["name"] ? ds["name"].as<std::string>() : "dataset_" + std::to_string(cfg.datasets.size());
        if (!dataset_names.insert(dataset.name).second) throw std::runtime_error("Duplicate dataset name: " + dataset.name);
        dataset.enabled = ds["enabled"] ? ds["enabled"].as<bool>() : true;
        if (!dataset.enabled) { cfg.datasets.push_back(dataset); continue; }
        dataset.path = ds["path"].as<std::string>();
        if (dataset.path.empty()) throw std::runtime_error("Dataset path is empty: " + dataset.name);
        const auto format = ds["format"] ? ds["format"].as<std::string>() : "long";
        if (format != "long" && format != "wide") throw std::runtime_error("Dataset format must be long or wide");
        dataset.format = format == "wide" ? DatasetFormat::Wide : DatasetFormat::Long;
        dataset.time_col = ds["time_col"] ? ds["time_col"].as<std::string>() :
            (dataset.format == DatasetFormat::Wide ? "__index_level_0__" : "open_time");
        dataset.symbol_col = ds["symbol_col"] ? ds["symbol_col"].as<std::string>() : "symbol";
        dataset.lazy = ds["lazy"] ? ds["lazy"].as<bool>() : static_cast<bool>(ds["catalog"]);
        dataset.alignment = alignment(ds["alignment"]);
        if (dataset.format == DatasetFormat::Long && dataset.alignment != FieldAlignment::Exact)
            throw std::runtime_error("Long datasets currently require exact alignment");
        if (ds["catalog"]) {
            const auto catalog_path = std::filesystem::path(cfg.config_dir) / ds["catalog"].as<std::string>();
            merge_fields(dataset.fields, YAML::LoadFile(catalog_path.string())["fields"]);
        }
        merge_fields(dataset.fields, ds["fields"]);
        if (ds["use_col"]) {
            if (!ds["use_col"].IsSequence()) throw std::runtime_error("use_col must be a sequence");
            for (const auto& column : ds["use_col"]) {
                const auto name = column.as<std::string>();
                if (name == "returns")
                    throw std::runtime_error("Field 'returns' is reserved for close-derived returns; use 'holding_return' or another name");
                const auto source = dataset.format == DatasetFormat::Wide ? name + ".parquet" : name;
                auto previous = dataset.fields.find(name);
                if (previous != dataset.fields.end() && previous->second != source)
                    throw std::runtime_error("use_col conflicts with field mapping: " + name);
                dataset.fields[name] = source;
                if (!std::regex_match(name, std::regex("[A-Za-z_][A-Za-z0-9_]*")) || !all_fields.insert(name).second)
                    throw std::runtime_error("Invalid/duplicate field across datasets: " + name);
                cfg.use_columns.push_back(name);
            }
        }
        if (dataset.fields.empty()) throw std::runtime_error("Dataset has no fields: " + dataset.name);
        for (const auto& field : dataset.fields) {
            bool from_this_use_col = false;
            if (ds["use_col"]) for (const auto& column : ds["use_col"])
                if (column.as<std::string>() == field.first) from_this_use_col = true;
            if (!from_this_use_col && !all_fields.insert(field.first).second)
                throw std::runtime_error("Field occurs in multiple datasets: " + field.first);
            if (!from_this_use_col) cfg.use_columns.push_back(field.first);
        }
        cfg.datasets.push_back(std::move(dataset));
    }
    bool has_enabled = false;
    for (const auto& ds : cfg.datasets) if (ds.enabled) {
        if (!has_enabled) { cfg.parquet_path = ds.path; cfg.time_col = ds.time_col; cfg.symbol_col = ds.symbol_col; }
        has_enabled = true;
    }
    if (!has_enabled || !all_fields.count("close")) throw std::runtime_error("An enabled dataset must provide close");

    const auto universe = root["universe"];
    if (universe && universe.IsSequence()) parse_symbols(cfg.universe.symbols, universe);
    else if (universe && universe.IsMap()) {
        if (universe["path"]) cfg.universe.path = universe["path"].as<std::string>();
        if (universe["time_col"]) cfg.universe.time_col = universe["time_col"].as<std::string>();
        if (universe["symbols"]) parse_symbols(cfg.universe.symbols, universe["symbols"]);
        cfg.universe.alignment = alignment(universe["alignment"]);
    } else if (universe) throw std::runtime_error("universe must be a sequence or mapping");

    const auto bt = root["backtest"];
    if (!bt || !bt.IsMap()) throw std::runtime_error("config.yaml: missing backtest mapping");
    cfg.start_date = bt["start_date"] ? bt["start_date"].as<std::string>() : "";
    cfg.end_date = bt["end_date"] ? bt["end_date"].as<std::string>() : "";
    if (!cfg.start_date.empty()) config_date_ms(cfg.start_date);
    if (!cfg.end_date.empty()) config_date_ms(cfg.end_date);
    if (!cfg.start_date.empty() && !cfg.end_date.empty() && cfg.start_date > cfg.end_date)
        throw std::runtime_error("backtest.start_date must not be after end_date");
    cfg.initial_capital = bt["initial_capital"] ? bt["initial_capital"].as<double>() : cfg.initial_capital;
    const double fee_bps = bt["transaction_fee"] ? bt["transaction_fee"].as<double>() : 5.0;
    if (!std::isfinite(fee_bps) || fee_bps < 0 || !std::isfinite(cfg.initial_capital) || cfg.initial_capital <= 0)
        throw std::runtime_error("Capital must be positive and fees finite/nonnegative");
    cfg.transaction_fee_rate = fee_bps * 0.0001;
    cfg.bars_per_year = unsigned_option(bt["bars_per_year"], 365, true);
    cfg.mode = bt["mode"] ? bt["mode"].as<int>() : 1;
    if (cfg.mode != 0 && cfg.mode != 1) throw std::runtime_error("backtest.mode must be 0 or 1");
    cfg.warmup_bars = unsigned_option(bt["warmup_bars"], 0, false);
    cfg.combined_mode = bt["combined_mode"] ? bt["combined_mode"].as<std::string>() : "normalized";
    if (cfg.combined_mode != "normalized" && cfg.combined_mode != "raw")
        throw std::runtime_error("combined_mode must be normalized or raw");
    return cfg;
}
