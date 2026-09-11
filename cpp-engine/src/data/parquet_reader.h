#pragma once
#include "data_panel.h"
#include "config_loader.h"

namespace parquet_reader {
    DataPanel load_config(const BacktestConfig& config,
                          const std::vector<std::string>& selected_fields = {});
    DataPanel load_parquet(const std::string& parquet_path,
                           const std::vector<std::string>& columns,
                           const std::string& time_col,
                           const std::string& symbol_col);
}
