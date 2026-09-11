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
    double transaction_fee_rate = 0.0005;

    std::vector<DatasetConfig> datasets;
    UniverseConfig universe;
    std::string config_dir;
    unsigned warmup_bars = 0;
    unsigned bars_per_year = 365;
    int mode = 1; // existing close execution: 0=D0, 1=D1
    std::string combined_mode = "normalized";

    static BacktestConfig from_yaml(const std::string& yaml_path);
};

// Strict YYYY-MM-DD conversion; end dates can use the last millisecond of a day.
int64_t config_date_ms(const std::string& value);
