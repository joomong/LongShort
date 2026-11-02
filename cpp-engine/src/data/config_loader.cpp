#include "config_loader.h"
#include <yaml-cpp/yaml.h>
#include <algorithm>
#include <cmath>
#include <ctime>
#include <filesystem>
#include <initializer_list>
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
// Every YAML block accepts exactly the keys from_yaml reads; anything else (a typo or a removed
// setting) is an error rather than a silently ignored default.
void require_known_keys(const YAML::Node& node, const std::string& prefix,
                        std::initializer_list<const char*> allowed) {
    if (!node.IsMap()) return;
    for (const auto& entry : node) {
        const auto key = entry.first.as<std::string>();
        if (std::none_of(allowed.begin(), allowed.end(), [&](const char* name) { return key == name; }))
            throw std::runtime_error("Unknown config key '" + prefix + key + "'");
    }
}
// A wide dataset field is published as `<dataset>.<field>`; its field part may start with a
// digit (`AFL.10DMACD`) but is never all digits.
bool valid_field_name(const std::string& name, bool wide) {
    if (!wide) return std::regex_match(name, std::regex("[A-Za-z_][A-Za-z0-9_]*"));
    return std::regex_match(name, std::regex("[A-Za-z0-9_]+")) && !std::regex_match(name, std::regex("[0-9]+"));
}
void merge_fields(std::map<std::string, std::string>& fields, const YAML::Node& node, bool wide) {
    if (!node) return;
    if (!node.IsMap()) throw std::runtime_error("fields must map public names to source columns/files");
    for (const auto& entry : node) {
        const auto name = entry.first.as<std::string>();
        const auto source = entry.second.as<std::string>();
        // Only a bare name can shadow the derived `returns`; a wide field is `<dataset>.returns`.
        if (!wide && name == "returns")
            throw std::runtime_error("Field 'returns' is reserved for close-derived returns; use 'holding_return' or another name");
        if (!valid_field_name(name, wide) || source.empty())
            throw std::runtime_error("Invalid field mapping: " + name);
        auto previous = fields.find(name);
        if (previous != fields.end() && previous->second != source)
            throw std::runtime_error("Conflicting field mapping: " + name);
        fields[name] = source;
    }
}
// `base:` names one price cache and one group-panel directory. It expands into two eager wide
// datasets so every config gets the standard fields without listing them
// (docs/data_contract.md 4.3). Both directories follow the usual path convention:
// relative values are resolved against the config directory by the loader.
DatasetConfig base_dataset(const std::string& name, const std::string& path,
                           std::map<std::string, std::string> fields) {
    DatasetConfig dataset;
    dataset.name = name;
    dataset.path = std::filesystem::path(path).lexically_normal().string();
    dataset.format = DatasetFormat::Wide;
    dataset.enabled = true;
    dataset.lazy = false;
    dataset.time_col = "__index_level_0__";
    dataset.alignment = FieldAlignment::Exact;
    dataset.fields = std::move(fields);
    return dataset;
}
void parse_base(BacktestConfig& cfg, const YAML::Node& node) {
    if (!node.IsMap()) throw std::runtime_error("base must be a mapping with cache and groups");
    require_known_keys(node, "base.", {"cache", "groups"});
    const auto directory = [&](const char* key) {
        if (!node[key] || node[key].IsNull())
            throw std::runtime_error(std::string("base requires ") + key + " (base: {cache, groups})");
        auto value = node[key].as<std::string>();
        if (value.empty()) throw std::runtime_error(std::string("base.") + key + " is empty");
        return value;
    };
    cfg.base_cache = directory("cache");
    cfg.base_groups = directory("groups");
    // A thin-v1 cache (docs/data_contract.md) is identified by
    // its manifest; its raw prices and signal returns are long files keyed by tradingItemId.
    const auto manifest = std::filesystem::path(cfg.config_dir) / cfg.base_cache / "manifest.json";
    if (std::filesystem::exists(manifest)) {
        const YAML::Node layout = YAML::LoadFile(manifest.string())["layout"];
        cfg.base_thin = layout && layout.IsScalar() && layout.as<std::string>() == "thin-v1";
    }
    // 2026-09-28 naming: returns.parquet is the total return (signal default and holding P&L),
    // adj_returns.parquet the split-only return. A thin cache still carrying returns_total.parquet
    // predates that and its returns.parquet is split-only, so it must be rebuilt, not reinterpreted.
    if (cfg.base_thin && std::filesystem::exists(manifest.parent_path() / "returns_total.parquet"))
        throw std::runtime_error("Thin cache " + cfg.base_cache +
                                 " uses the old returns/returns_total naming; rebuild it with thin_build.py");
    if (cfg.base_thin) {
        DatasetConfig prices = base_dataset("base_prices",
            (std::filesystem::path(cfg.base_cache) / "base_data.parquet").string(),
            {{"open", "open"}, {"high", "high"}, {"low", "low"}, {"close", "close"},
             {"volume", "volume"}, {"vwap", "vwap"}, {"cap", "marketCap_USD"}});
        DatasetConfig returns = base_dataset("base_returns",
            (std::filesystem::path(cfg.base_cache) / "returns.parquet").string(), {{"returns", "returns"}});
        DatasetConfig adj_returns = base_dataset("base_adj_returns",
            (std::filesystem::path(cfg.base_cache) / "adj_returns.parquet").string(), {{"adj_returns", "adj_returns"}});
        for (DatasetConfig* dataset : {&prices, &returns, &adj_returns}) {
            dataset->format = DatasetFormat::Long;
            dataset->time_col = "date";
            dataset->symbol_col = "tradingItemId";
        }
        cfg.datasets.push_back(std::move(prices));
        cfg.datasets.push_back(std::move(returns));
        cfg.datasets.push_back(std::move(adj_returns));
    } else cfg.datasets.push_back(base_dataset("base_prices",
        (std::filesystem::path(cfg.base_cache) / "BaseData").string(),
        {{"open", "open.parquet"}, {"high", "high.parquet"}, {"low", "low.parquet"},
         {"close", "close.parquet"}, {"volume", "volume.parquet"}, {"vwap", "VWAP.parquet"},
         {"cap", "marketCap_USD.parquet"}}));
    // Classification is "the most recent known label": as-of alignment carries the last
    // row forward when the price cache grows past the group panel, so a daily cache update
    // does not need an immediate rebuild (new symbols stay unclassified until one).
    DatasetConfig groups = base_dataset("base_groups", cfg.base_groups,
        {{"sector", "sector.parquet"}, {"industry", "industry.parquet"}});
    groups.alignment = FieldAlignment::AsOf;
    cfg.datasets.push_back(std::move(groups));
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
    require_known_keys(root, "", {"base", "datasets", "universe", "backtest", "alphas_dir", "meta_dir", "alpha_pool", "alpha_pool_universe"});
    cfg.config_dir = std::filesystem::absolute(yaml_path).parent_path().lexically_normal().string();
    const auto base = root["base"];
    const auto datasets = root["datasets"];
    // `base:` alone is a complete data configuration; without it `datasets:` stays required.
    const bool has_base = base && !base.IsNull();
    if (!has_base && (!datasets || !datasets.IsSequence() || datasets.size() == 0))
        throw std::runtime_error("config.yaml: datasets must be a non-empty sequence");
    if (datasets && !datasets.IsNull() && !datasets.IsSequence())
        throw std::runtime_error("config.yaml: datasets must be a non-empty sequence");
    if (has_base) parse_base(cfg, base);
    std::set<std::string> all_fields, dataset_names;
    for (const auto& ds : cfg.datasets) dataset_names.insert(ds.name);
    const YAML::Node user_datasets =
        datasets && datasets.IsSequence() ? datasets : YAML::Node(YAML::NodeType::Sequence);
    std::size_t dataset_index = 0;
    for (const auto& ds : user_datasets) {
        if (!ds.IsMap()) throw std::runtime_error("Each dataset must be a mapping");
        require_known_keys(ds, "datasets[" + std::to_string(dataset_index++) + "].",
                           {"name", "enabled", "path", "format", "time_col", "symbol_col", "lazy", "catalog",
                            "alignment", "fields", "use_col"});
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
        const bool wide = dataset.format == DatasetFormat::Wide;
        // Wide dataset fields are registered only as `<dataset name>.<field>`.
        if (wide && (!ds["name"] || !std::regex_match(dataset.name, std::regex("[A-Za-z][A-Za-z0-9_]*"))))
            throw std::runtime_error("Wide dataset requires a name matching [A-Za-z][A-Za-z0-9_]*: " + dataset.name);
        const std::string prefix = wide ? dataset.name + "." : "";
        if (ds["catalog"]) {
            const auto catalog_path = std::filesystem::path(cfg.config_dir) / ds["catalog"].as<std::string>();
            const YAML::Node catalog = YAML::LoadFile(catalog_path.string());
            const YAML::Node version = catalog["schema_version"];
            const std::string seen = version && version.IsScalar() ? version.as<std::string>() : "1";
            if (seen == "1")
                throw std::runtime_error(catalog_path.string() + ": AFL catalog schema_version 1 is no longer supported "
                                         "(rebuild with universe_builder/afl/build_afl_catalog.py)");
            if (seen != "2")
                throw std::runtime_error(catalog_path.string() + ": unsupported AFL catalog schema_version " + seen +
                                         " (expected 2)");
            merge_fields(dataset.fields, catalog["fields"], wide);
        }
        merge_fields(dataset.fields, ds["fields"], wide);
        if (ds["use_col"]) {
            if (!ds["use_col"].IsSequence()) throw std::runtime_error("use_col must be a sequence");
            for (const auto& column : ds["use_col"]) {
                const auto name = column.as<std::string>();
                if (!wide && name == "returns")
                    throw std::runtime_error("Field 'returns' is reserved for close-derived returns; use 'holding_return' or another name");
                const auto source = dataset.format == DatasetFormat::Wide ? name + ".parquet" : name;
                auto previous = dataset.fields.find(name);
                if (previous != dataset.fields.end() && previous->second != source)
                    throw std::runtime_error("use_col conflicts with field mapping: " + name);
                dataset.fields[name] = source;
                if (!valid_field_name(name, wide) || !all_fields.insert(prefix + name).second)
                    throw std::runtime_error("Invalid/duplicate field across datasets: " + prefix + name);
                cfg.use_columns.push_back(prefix + name);
            }
        }
        if (dataset.fields.empty()) throw std::runtime_error("Dataset has no fields: " + dataset.name);
        if (wide) {
            std::map<std::string, std::string> named;
            for (const auto& field : dataset.fields) named.emplace(prefix + field.first, field.second);
            dataset.fields.swap(named);
        }
        for (const auto& field : dataset.fields) {
            bool from_this_use_col = false;
            if (ds["use_col"]) for (const auto& column : ds["use_col"])
                if (prefix + column.as<std::string>() == field.first) from_this_use_col = true;
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
    // base_prices carries close; user datasets must still declare it when there is no base block.
    if (!has_enabled || (!has_base && !all_fields.count("close")))
        throw std::runtime_error("An enabled dataset must provide close");

    const auto universe = root["universe"];
    if (universe && universe.IsSequence()) parse_symbols(cfg.universe.symbols, universe);
    else if (universe && universe.IsMap()) {
        require_known_keys(universe, "universe.", {"path", "time_col", "symbols", "name", "names", "alignment"});
        if (universe["path"]) cfg.universe.path = universe["path"].as<std::string>();
        if (universe["time_col"]) cfg.universe.time_col = universe["time_col"].as<std::string>();
        if (universe["symbols"]) parse_symbols(cfg.universe.symbols, universe["symbols"]);
        if (universe["name"] && !universe["name"].IsNull()) cfg.universe.name = universe["name"].as<std::string>();
        if (universe["names"]) {
            if (!universe["names"].IsSequence() || universe["names"].size() == 0)
                throw std::runtime_error("universe.names must be a nonempty sequence");
            for (const auto& item : universe["names"]) {
                const auto value = item.as<std::string>();
                if (value.empty() || std::find(cfg.universe.names.begin(), cfg.universe.names.end(), value) != cfg.universe.names.end())
                    throw std::runtime_error("Empty or duplicate universe.names entry: " + value);
                cfg.universe.names.push_back(value);
            }
            if (cfg.universe.name.empty()) cfg.universe.name = cfg.universe.names.front();
            else if (std::find(cfg.universe.names.begin(), cfg.universe.names.end(), cfg.universe.name) == cfg.universe.names.end())
                throw std::runtime_error("universe.name must be one of universe.names");
        }
        cfg.universe.alignment = alignment(universe["alignment"]);
    } else if (universe) throw std::runtime_error("universe must be a sequence or mapping");

    const auto bt = root["backtest"];
    if (!bt || !bt.IsMap()) throw std::runtime_error("config.yaml: missing backtest mapping");
    require_known_keys(bt, "backtest.", {"start_date", "end_date", "initial_capital", "transaction_fee",
                                         "bars_per_year", "execution_price", "warmup_bars", "pit_lag",
                                         "symbol_labels_path", "missing_close_policy"});
    cfg.start_date = bt["start_date"] ? bt["start_date"].as<std::string>() : "";
    cfg.end_date = bt["end_date"] && !bt["end_date"].IsNull()
        ? bt["end_date"].as<std::string>() : "latest";
    if (cfg.end_date.empty()) cfg.end_date = "latest";
    if (!cfg.start_date.empty()) config_date_ms(cfg.start_date);
    if (cfg.end_date != "latest") config_date_ms(cfg.end_date);
    if (!cfg.start_date.empty() && cfg.end_date != "latest" && cfg.start_date > cfg.end_date)
        throw std::runtime_error("backtest.start_date must not be after end_date");
    cfg.initial_capital = bt["initial_capital"] ? bt["initial_capital"].as<double>() : cfg.initial_capital;
    if (bt["transaction_fee"]) cfg.transaction_fee_rate = bt["transaction_fee"].as<double>() * 0.0001; // bp
    if (!std::isfinite(cfg.transaction_fee_rate) || cfg.transaction_fee_rate < 0 ||
        !std::isfinite(cfg.initial_capital) || cfg.initial_capital <= 0)
        throw std::runtime_error("Capital must be positive and fees finite/nonnegative");
    cfg.bars_per_year = unsigned_option(bt["bars_per_year"], cfg.bars_per_year, true);
    cfg.execution_price = bt["execution_price"] ? bt["execution_price"].as<std::string>() : cfg.execution_price;
    if (cfg.execution_price != "close" && cfg.execution_price != "ohlc4" && cfg.execution_price != "vwap")
        throw std::runtime_error("backtest.execution_price must be 'close', 'ohlc4' or 'vwap'");
    cfg.warmup_bars = unsigned_option(bt["warmup_bars"], 0, false);
    cfg.pit_lag = unsigned_option(bt["pit_lag"], 1, false);
    if (bt["symbol_labels_path"] && !bt["symbol_labels_path"].IsNull()) {
        std::filesystem::path labels = bt["symbol_labels_path"].as<std::string>();
        if (labels.is_relative()) labels = std::filesystem::path(cfg.config_dir) / labels;
        cfg.symbol_labels_path = labels.lexically_normal().string();
    }
    auto directory_option = [&](const char* key, const char* fallback) {
        std::filesystem::path dir = root[key] && !root[key].IsNull() ? root[key].as<std::string>() : fallback;
        if (dir.empty()) throw std::runtime_error(std::string(key) + " must not be empty");
        if (dir.is_relative()) dir = std::filesystem::path(cfg.config_dir) / dir;
        dir = dir.lexically_normal();
        if (!dir.has_filename() && dir.has_relative_path()) dir = dir.parent_path();
        return dir.string();
    };
    cfg.alphas_dir = directory_option("alphas_dir", "alphas");
    cfg.meta_dir = directory_option("meta_dir", "meta");
    if (root["alpha_pool"] && !root["alpha_pool"].IsNull()) cfg.alpha_pool = directory_option("alpha_pool", "");
    if (root["alpha_pool_universe"] && !root["alpha_pool_universe"].IsNull()) {
        cfg.alpha_pool_universe = root["alpha_pool_universe"].as<std::string>();
        if (cfg.alpha_pool_universe.empty()) throw std::runtime_error("alpha_pool_universe must not be empty");
    }
    // Missing-close policy applies to ordinary and thin panels.
    cfg.missing_close_policy = bt["missing_close_policy"] && !bt["missing_close_policy"].IsNull()
        ? bt["missing_close_policy"].as<std::string>() : cfg.missing_close_policy;
    if (cfg.missing_close_policy != "carry" && cfg.missing_close_policy != "error")
        throw std::runtime_error("backtest.missing_close_policy must be 'carry' or 'error'");
    return cfg;
}
