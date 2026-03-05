#pragma once

#include <string>
#include <vector>

struct BacktestConfig {
    std::string parquet_path;
    std::string time_col;
    std::string symbol_col;
    std::vector<std::string> use_columns;
    std::string start_date;
    std::string end_date;
    double initial_capital;
    double transaction_fee_rate;  // fraction (e.g., 0.0005 for 5bps)

    static BacktestConfig from_yaml(const std::string& yaml_path);
};
