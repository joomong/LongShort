#pragma once
#include "data_panel.h"
#include "config_loader.h"
#include <map>

namespace parquet_reader {
    DataPanel load_config(const BacktestConfig& config,
                          const std::vector<std::string>& selected_fields = {});
    std::map<std::string, std::string> load_thin_symbol_labels(
        const BacktestConfig& config, const std::vector<std::string>& panel_symbols);
    DataPanel load_parquet(const std::string& parquet_path,
                           const std::vector<std::string>& columns,
                           const std::string& time_col,
                           const std::string& symbol_col);
}
