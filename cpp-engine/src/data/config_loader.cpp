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
// Optional top-level events mapping; an absent node leaves the empty (no events) default.
void parse_events(EventsConfig& events, const YAML::Node& node) {
    if (!node || node.IsNull()) return;
    if (!node.IsMap()) throw std::runtime_error("events must be a mapping with a path");
    if (!node["path"] || node["path"].IsNull()) throw std::runtime_error("events requires a path");
    events.path = node["path"].as<std::string>();
    if (events.path.empty()) throw std::runtime_error("events.path is empty");
    if (node["id_col"] && !node["id_col"].IsNull()) events.id_col = node["id_col"].as<std::string>();
    if (node["end_date_col"] && !node["end_date_col"].IsNull()) events.end_date_col = node["end_date_col"].as<std::string>();
    if (node["return_col"] && !node["return_col"].IsNull()) events.return_col = node["return_col"].as<std::string>();
    if (node["successor_col"] && !node["successor_col"].IsNull()) events.successor_col = node["successor_col"].as<std::string>();
    if (node["successor_ratio_col"] && !node["successor_ratio_col"].IsNull())
        events.successor_ratio_col = node["successor_ratio_col"].as<std::string>();
    if (node["cash_col"] && !node["cash_col"].IsNull()) events.cash_col = node["cash_col"].as<std::string>();
    if (node["successor_price_col"] && !node["successor_price_col"].IsNull())
        events.successor_price_col = node["successor_price_col"].as<std::string>();
    if (node["successor_price_date_col"] && !node["successor_price_date_col"].IsNull())
        events.successor_price_date_col = node["successor_price_date_col"].as<std::string>();
    if (node["target_price_col"] && !node["target_price_col"].IsNull())
        events.target_price_col = node["target_price_col"].as<std::string>();
    if (node["target_price_date_col"] && !node["target_price_date_col"].IsNull())
        events.target_price_date_col = node["target_price_date_col"].as<std::string>();
    if (node["stock_leg_col"] && !node["stock_leg_col"].IsNull())
        events.stock_leg_col = node["stock_leg_col"].as<std::string>();
    if (node["cash_leg_col"] && !node["cash_leg_col"].IsNull())
        events.cash_leg_col = node["cash_leg_col"].as<std::string>();
    if (events.id_col.empty() || events.end_date_col.empty())
        throw std::runtime_error("events id_col/end_date_col must not be empty");
    // A ratio without a successor column has nothing to scale.
    if (!events.successor_ratio_col.empty() && events.successor_col.empty())
        throw std::runtime_error("events successor_ratio_col requires successor_col");
    // The staleness guard needs the price, its date and a successor to price.
    if (events.successor_price_col.empty() != events.successor_price_date_col.empty())
        throw std::runtime_error(
            "events successor_price_col and successor_price_date_col must be set together");
    if (!events.successor_price_col.empty() && events.successor_col.empty())
        throw std::runtime_error("events successor_price_col requires successor_col");
    if (events.target_price_col.empty() != events.target_price_date_col.empty())
        throw std::runtime_error(
            "events target_price_col and target_price_date_col must be set together");
    std::set<std::string> named;
    for (const std::string& column : {events.id_col, events.end_date_col, events.return_col,
                                      events.successor_col, events.successor_ratio_col,
                                      events.cash_col, events.successor_price_col,
                                      events.successor_price_date_col, events.target_price_col,
                                      events.target_price_date_col, events.stock_leg_col,
                                      events.cash_leg_col})
        if (!column.empty() && !named.insert(column).second)
            throw std::runtime_error("events columns must be distinct: " + column);
}
// Optional backtest.corporate_actions mapping; an absent node leaves the empty (no ledger) default.
void parse_corporate_actions(CorporateActionsConfig& ledger, const YAML::Node& node) {
    if (!node || node.IsNull()) return;
    if (!node.IsMap()) throw std::runtime_error("backtest.corporate_actions must be a mapping with a path");
    if (!node["path"] || node["path"].IsNull()) throw std::runtime_error("backtest.corporate_actions requires a path");
    ledger.path = node["path"].as<std::string>();
    if (ledger.path.empty()) throw std::runtime_error("backtest.corporate_actions.path is empty");
    const auto column = [&](const char* key, std::string& target) {
        if (node[key] && !node[key].IsNull()) target = node[key].as<std::string>();
        if (target.empty()) throw std::runtime_error(std::string("backtest.corporate_actions.") + key + " must not be empty");
    };
    column("id_col", ledger.id_col);
    column("ex_date_col", ledger.ex_date_col);
    column("type_col", ledger.type_col);
    column("leg_col", ledger.leg_col);
    column("cash_col", ledger.cash_col);
    column("child_col", ledger.child_col);
    column("child_ratio_col", ledger.child_ratio_col);
    column("child_close_col", ledger.child_close_col);
    column("parent_close_col", ledger.parent_close_col);
    column("parent_close_before_col", ledger.parent_close_before_col);
    column("parent_factor_col", ledger.parent_factor_col);
    column("estimate_col", ledger.estimate_col);
    column("value_source_col", ledger.value_source_col);
    column("status_col", ledger.status_col);
    column("expiry_col", ledger.expiry_col);
    column("subscription_price_col", ledger.subscription_price_col);
    column("exercise_ratio_col", ledger.exercise_ratio_col);
    column("child_value_col", ledger.child_value_col);
    column("estimate_reason_col", ledger.estimate_reason_col);
    std::set<std::string> named;
    for (const std::string& name : {ledger.id_col, ledger.ex_date_col, ledger.type_col, ledger.leg_col,
                                    ledger.cash_col, ledger.child_col, ledger.child_ratio_col,
                                    ledger.child_close_col, ledger.parent_close_col,
                                    ledger.parent_close_before_col, ledger.parent_factor_col,
                                    ledger.estimate_col, ledger.value_source_col, ledger.status_col,
                                    ledger.expiry_col, ledger.subscription_price_col,
                                    ledger.exercise_ratio_col, ledger.child_value_col,
                                    ledger.estimate_reason_col})
        if (!named.insert(name).second)
            throw std::runtime_error("backtest.corporate_actions columns must be distinct: " + name);
    // Automatic exercise is designed but not implemented; a silent no-op would misreport
    // the P&L of every in-the-money right, so the key is refused rather than ignored.
    if (node["auto_exercise"] && !node["auto_exercise"].IsNull()) {
        ledger.auto_exercise = node["auto_exercise"].as<bool>();
        if (ledger.auto_exercise)
            throw std::runtime_error("backtest.corporate_actions.auto_exercise is not implemented "
                "(docs/rights_warrants_design_2026-09-17.md section 4): set it to false or remove the key");
    }
    if (node["apply_dividends"] && !node["apply_dividends"].IsNull())
        ledger.apply_dividends = node["apply_dividends"].as<bool>();
    if (node["apply_distributions"] && !node["apply_distributions"].IsNull())
        ledger.apply_distributions = node["apply_distributions"].as<bool>();
    if (node["unpriced_child"] && !node["unpriced_child"].IsNull())
        ledger.unpriced_child = node["unpriced_child"].as<std::string>();
    if (ledger.unpriced_child != "pending" && ledger.unpriced_child != "drop")
        throw std::runtime_error("backtest.corporate_actions.unpriced_child must be 'pending' or 'drop'");
    if (node["stale_check"] && !node["stale_check"].IsNull())
        ledger.stale_check = node["stale_check"].as<bool>();
    if (node["unresolved_policy"] && !node["unresolved_policy"].IsNull())
        ledger.unresolved_policy = node["unresolved_policy"].as<std::string>();
    if (ledger.unresolved_policy != "warn" && ledger.unresolved_policy != "strict")
        throw std::runtime_error("backtest.corporate_actions.unresolved_policy must be 'warn' or 'strict'");
}
// Optional backtest.costs mapping; an absent node keeps every module off.
void parse_costs(CostsConfig& costs, const YAML::Node& node) {
    if (!node || node.IsNull()) return;
    if (!node.IsMap()) throw std::runtime_error("backtest.costs must be a mapping");
    if (node["report_unapplied"] && !node["report_unapplied"].IsNull())
        costs.report_unapplied = node["report_unapplied"].as<bool>();
    const YAML::Node borrow = node["borrow"];
    if (!borrow || borrow.IsNull()) return;
    if (!borrow.IsMap()) throw std::runtime_error("backtest.costs.borrow must be a mapping");
    BorrowCostConfig& config = costs.borrow;
    if (borrow["enabled"] && !borrow["enabled"].IsNull()) config.enabled = borrow["enabled"].as<bool>();
    if (borrow["default_rate"] && !borrow["default_rate"].IsNull()) {
        config.default_rate = borrow["default_rate"].as<double>();
        if (!std::isfinite(config.default_rate) || config.default_rate < 0.0)
            throw std::runtime_error("backtest.costs.borrow.default_rate must be a finite nonnegative annual rate");
    }
    if (borrow["path"] && !borrow["path"].IsNull()) config.path = borrow["path"].as<std::string>();
    config.day_count = unsigned_option(borrow["day_count"], 360, true);
    const auto column = [&](const char* key, std::string& target) {
        if (borrow[key] && !borrow[key].IsNull()) target = borrow[key].as<std::string>();
        if (target.empty()) throw std::runtime_error(std::string("backtest.costs.borrow.") + key + " must not be empty");
    };
    column("symbol_col", config.symbol_col);
    column("from_col", config.from_col);
    column("to_col", config.to_col);
    column("rate_col", config.rate_col);
    std::set<std::string> named;
    for (const std::string& name : {config.symbol_col, config.from_col, config.to_col, config.rate_col})
        if (!named.insert(name).second)
            throw std::runtime_error("backtest.costs.borrow columns must be distinct: " + name);
}
// `base:` names one price cache and one group-panel directory. It expands into two eager wide
// datasets so every config gets the standard fields without listing them
// (docs/neutralize_plan_2026-09-16.md 4.3). Both directories follow the usual path convention:
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
    const auto directory = [&](const char* key) {
        if (!node[key] || node[key].IsNull())
            throw std::runtime_error(std::string("base requires ") + key + " (base: {cache, groups})");
        auto value = node[key].as<std::string>();
        if (value.empty()) throw std::runtime_error(std::string("base.") + key + " is empty");
        return value;
    };
    cfg.base_cache = directory("cache");
    cfg.base_groups = directory("groups");
    // A thin-v1 cache (docs/backtest_cache_simplification_plan_2026-09-23.md §6) is identified by
    // its manifest; its raw prices and signal returns are long files keyed by tradingItemId.
    const auto manifest = std::filesystem::path(cfg.config_dir) / cfg.base_cache / "manifest.json";
    if (std::filesystem::exists(manifest)) {
        const YAML::Node layout = YAML::LoadFile(manifest.string())["layout"];
        cfg.base_thin = layout && layout.IsScalar() && layout.as<std::string>() == "thin-v1";
    }
    if (cfg.base_thin) {
        DatasetConfig prices = base_dataset("base_prices",
            (std::filesystem::path(cfg.base_cache) / "base_data.parquet").string(),
            {{"open", "open"}, {"high", "high"}, {"low", "low"}, {"close", "close"},
             {"volume", "volume"}, {"vwap", "vwap"}, {"cap", "marketCap_USD"}});
        DatasetConfig returns = base_dataset("base_returns",
            (std::filesystem::path(cfg.base_cache) / "returns.parquet").string(), {{"returns", "returns"}});
        for (DatasetConfig* dataset : {&prices, &returns}) {
            dataset->format = DatasetFormat::Long;
            dataset->time_col = "date";
            dataset->symbol_col = "tradingItemId";
        }
        cfg.datasets.push_back(std::move(prices));
        cfg.datasets.push_back(std::move(returns));
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
    for (const auto& ds : user_datasets) {
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
    // base_prices carries close; user datasets must still declare it when there is no base block.
    if (!has_enabled || (!has_base && !all_fields.count("close")))
        throw std::runtime_error("An enabled dataset must provide close");

    const auto universe = root["universe"];
    if (universe && universe.IsSequence()) parse_symbols(cfg.universe.symbols, universe);
    else if (universe && universe.IsMap()) {
        if (universe["path"]) cfg.universe.path = universe["path"].as<std::string>();
        if (universe["time_col"]) cfg.universe.time_col = universe["time_col"].as<std::string>();
        if (universe["symbols"]) parse_symbols(cfg.universe.symbols, universe["symbols"]);
        if (universe["name"] && !universe["name"].IsNull()) cfg.universe.name = universe["name"].as<std::string>();
        cfg.universe.alignment = alignment(universe["alignment"]);
    } else if (universe) throw std::runtime_error("universe must be a sequence or mapping");

    const auto bt = root["backtest"];
    if (!bt || !bt.IsMap()) throw std::runtime_error("config.yaml: missing backtest mapping");
    cfg.start_date = bt["start_date"] ? bt["start_date"].as<std::string>() : "";
    cfg.end_date = bt["end_date"] && !bt["end_date"].IsNull()
        ? bt["end_date"].as<std::string>() : "latest";
    if (cfg.end_date.empty()) cfg.end_date = "latest";
    if (!cfg.start_date.empty()) config_date_ms(cfg.start_date);
    if (cfg.end_date != "latest") config_date_ms(cfg.end_date);
    if (!cfg.start_date.empty() && cfg.end_date != "latest" && cfg.start_date > cfg.end_date)
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
    if (bt["symbol_labels_path"] && !bt["symbol_labels_path"].IsNull()) {
        std::filesystem::path labels = bt["symbol_labels_path"].as<std::string>();
        if (labels.is_relative()) labels = std::filesystem::path(cfg.config_dir) / labels;
        cfg.symbol_labels_path = labels.lexically_normal().string();
    }
    if (cfg.combined_mode != "normalized" && cfg.combined_mode != "raw")
        throw std::runtime_error("combined_mode must be normalized or raw");
    // Missing-close accounting. Defaults keep the documented carry policy and a 0 settlement return.
    cfg.missing_close_policy = bt["missing_close_policy"] && !bt["missing_close_policy"].IsNull()
        ? bt["missing_close_policy"].as<std::string>() : "carry";
    if (cfg.missing_close_policy != "carry" && cfg.missing_close_policy != "error")
        throw std::runtime_error("backtest.missing_close_policy must be 'carry' or 'error'");
    cfg.default_delisting_return = bt["default_delisting_return"] && !bt["default_delisting_return"].IsNull()
        ? bt["default_delisting_return"].as<double>() : 0.0;
    if (!std::isfinite(cfg.default_delisting_return) ||
        cfg.default_delisting_return < -1.0 || cfg.default_delisting_return > 1.0)
        throw std::runtime_error("backtest.default_delisting_return must be a finite total return in [-1, 1]");
    cfg.stale_quote_settle_bars = unsigned_option(bt["stale_quote_settle_bars"], 0, false);
    parse_events(cfg.events, root["events"]);
    parse_corporate_actions(cfg.corporate_actions, bt["corporate_actions"]);
    parse_costs(cfg.costs, bt["costs"]);
    if (cfg.base_thin && (!cfg.events.path.empty() || !cfg.corporate_actions.path.empty()))
        throw std::runtime_error("base.cache is a thin-v1 cache: it already carries returns_total and events, "
                                 "so remove the events: and backtest.corporate_actions blocks");
    return cfg;
}
