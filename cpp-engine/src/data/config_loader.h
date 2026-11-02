#pragma once

#include <cstdint>
#include <map>
#include <string>
#include <vector>

enum class DatasetFormat { Long, Wide };
enum class FieldAlignment { Exact, AsOf };

struct DatasetConfig {
    std::string name;
    std::string path;
    DatasetFormat format = DatasetFormat::Long;
    bool enabled = true;
    bool lazy = false;
    std::string time_col = "open_time";
    std::string symbol_col = "symbol";
    std::map<std::string, std::string> fields; // public name -> column (long) / file (wide)
    FieldAlignment alignment = FieldAlignment::Exact;
};

struct UniverseConfig {
    std::string path; // optional wide membership file; null/absent cells are false
    std::string time_col = "__index_level_0__";
    std::vector<std::string> symbols; // optional fixed subset, intersected with membership
    std::string name; // interval membership (universe, tradingItemId, fromDate, toDate): universe to select
    // Optional selectable sets (universe.names); the panel loads their union, `name` is the default.
    std::vector<std::string> names;
    FieldAlignment alignment = FieldAlignment::Exact;
};

struct BacktestConfig {
    // Legacy first-dataset views retained for existing callers/configs.
    std::string parquet_path;
    std::string time_col;
    std::string symbol_col;
    std::vector<std::string> use_columns;
    std::string start_date;
    std::string end_date; // latest (or empty) selects the last nonblank close row on load
    double initial_capital = 1000000.0;
    double transaction_fee_rate = 1.5 * 0.0001; // 1.5 bp, the same product from_yaml forms from transaction_fee

    std::vector<DatasetConfig> datasets;
    UniverseConfig universe;
    std::string config_dir;
    unsigned warmup_bars = 0;
    // Universe membership and group codes known at the close of bar t - pit_lag apply at bar t (PIT).
    unsigned pit_lag = 1;
    unsigned bars_per_year = 365;
    std::string missing_close_policy = "carry"; // "carry" | "error"
    // Optional JSON file {panelSymbol: displayLabel} (e.g. tradingItemId -> ticker) served by /api/config.
    std::string symbol_labels_path;
    // Optional `base:` block (docs/data_contract.md 4.3): one price cache plus one
    // group-panel directory, expanded by from_yaml into the base_prices/base_groups datasets.
    // The two directories are kept verbatim so the API can report them later.
    std::string base_cache;
    std::string base_groups;
    bool base_thin = false; // base.cache/manifest.json declares layout thin-v1
    std::string execution_price = "close"; // "close" | "ohlc4" | "vwap"; stock examples select ohlc4
    std::string alphas_dir; // absolute; top-level `alphas_dir`, default <config dir>/alphas
    std::string meta_dir;   // absolute; top-level `meta_dir`, default <config dir>/meta
    std::string alpha_pool; // absolute; top-level `alpha_pool` (optional); empty = no pool correlation
    std::string alpha_pool_universe; // top-level `alpha_pool_universe` (optional): universe the pool is simulated with; empty = universe.name

    static BacktestConfig from_yaml(const std::string& yaml_path);
};

// Strict YYYY-MM-DD conversion; end dates can use the last millisecond of a day.
int64_t config_date_ms(const std::string& value);
