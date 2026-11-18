#pragma once

#include "simulation_data.h"
#include <map>
#include <cstdint>
#include <string>
#include <vector>

struct UniverseConfig {
    std::string path;
    std::vector<std::string> symbols;
    std::string name;
    std::vector<std::string> names;
};

struct BacktestConfig {
    std::string base_cache;
    std::string base_groups;
    std::string afl_path;
    std::string afl_catalog;
    std::size_t afl_cache_budget_bytes = std::size_t{32} << 30;  // afl.cache_budget_gib: run-panel AFL arrays
    UniverseConfig universe;
    std::string config_dir;
    std::string start_date;
    std::string end_date = "latest";
    double initial_capital = 1000000.0;
    double transaction_fee_rate = 1.5 * 0.0001;
    unsigned bars_per_year = 365;
    std::string missing_close_policy = "carry";
    std::string symbol_labels_path;
    std::string execution_price = "close";
    std::string alphas_dir;
    std::string meta_dir;
    std::string alpha_pool;
    std::string alpha_pool_universe;

    static BacktestConfig from_yaml(const std::string& yaml_path);
};

int64_t config_date_ms(const std::string& value);

namespace loader {
DataPanel load_config(const BacktestConfig& config,
                      const std::vector<std::string>& selected_fields = {});
std::map<std::string, std::string> load_thin_symbol_labels(
    const BacktestConfig& config, const std::vector<std::string>& panel_symbols);
}
