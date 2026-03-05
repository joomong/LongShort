#include "config_loader.h"
#include <yaml-cpp/yaml.h>
#include <stdexcept>

BacktestConfig BacktestConfig::from_yaml(const std::string& yaml_path) {
    YAML::Node root = YAML::LoadFile(yaml_path);

    BacktestConfig cfg;

    // datasets[0] -> parquet_path, time_col, symbol_col, use_columns
    const YAML::Node& datasets = root["datasets"];
    if (!datasets || !datasets.IsSequence() || datasets.size() == 0) {
        throw std::runtime_error("config.yaml: 'datasets' must be a non-empty sequence");
    }
    const YAML::Node& ds = datasets[0];

    cfg.parquet_path = ds["path"].as<std::string>();
    cfg.time_col     = ds["time_col"].as<std::string>();
    cfg.symbol_col   = ds["symbol_col"].as<std::string>();

    const YAML::Node& use_col = ds["use_col"];
    if (!use_col || !use_col.IsSequence()) {
        throw std::runtime_error("config.yaml: datasets[0].use_col must be a sequence");
    }
    for (const auto& item : use_col) {
        cfg.use_columns.push_back(item.as<std::string>());
    }

    // backtest section
    const YAML::Node& bt = root["backtest"];
    if (!bt) {
        throw std::runtime_error("config.yaml: missing 'backtest' section");
    }

    cfg.start_date      = bt["start_date"].as<std::string>();
    cfg.end_date        = bt["end_date"].as<std::string>();
    cfg.initial_capital = bt["initial_capital"].as<double>();

    // transaction_fee is in bps; convert to fraction
    double fee_bps          = bt["transaction_fee"].as<double>();
    cfg.transaction_fee_rate = fee_bps * 0.0001;

    return cfg;
}
