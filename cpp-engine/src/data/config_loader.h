#pragma once

#include <cstdint>
#include <limits>
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
    FieldAlignment alignment = FieldAlignment::Exact;
};

// Optional cost modules (backtest.costs, docs/corporate_actions_engine_2026-09-17.md "costs").
// Borrow: an annual rate on the short book, accrued per calendar day between bars and
// taken from net P&L only. Rates come from an interval table (symbol, from, to, rate),
// else `default_rate`; with neither the cost is 0 and the run reports borrowApplied=false.
struct BorrowCostConfig {
    bool enabled = false;
    double default_rate = std::numeric_limits<double>::quiet_NaN(); // annual fraction; NaN = none
    std::string path;                    // empty = no table; .csv or parquet
    unsigned day_count = 360;
    std::string symbol_col = "symbol";
    std::string from_col = "from";       // first calendar day the rate applies (inclusive)
    std::string to_col = "to";           // last calendar day (inclusive); null = open-ended
    std::string rate_col = "rate";       // annual fraction
};

struct CostsConfig {
    BorrowCostConfig borrow;
    bool report_unapplied = true;        // say in the results when a cost was not applied
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
    double transaction_fee_rate = 0.00015;

    std::vector<DatasetConfig> datasets;
    UniverseConfig universe;
    std::string config_dir;
    unsigned warmup_bars = 0;
    unsigned bars_per_year = 365;
    int mode = 1; // existing close execution: 0=D0, 1=D1
    std::string combined_mode = "normalized";
    std::string missing_close_policy = "carry"; // "carry" | "error"
    CostsConfig costs;                            // backtest.costs
    // Optional JSON file {panelSymbol: displayLabel} (e.g. tradingItemId -> ticker) served by /api/config.
    std::string symbol_labels_path;
    // Optional `base:` block (docs/neutralize_plan_2026-09-16.md 4.3): one price cache plus one
    // group-panel directory, expanded by from_yaml into the base_prices/base_groups datasets.
    // The two directories are kept verbatim so the API can report them later.
    std::string base_cache;
    std::string base_groups;
    bool base_thin = false; // base.cache/manifest.json declares layout thin-v1

    static BacktestConfig from_yaml(const std::string& yaml_path);
};

// Strict YYYY-MM-DD conversion; end dates can use the last millisecond of a day.
int64_t config_date_ms(const std::string& value);
