#pragma once
#include "data_panel.h"

// Implements DataPanel::load() using Apache Arrow C++ library
namespace parquet_reader {
    DataPanel load_parquet(const std::string& parquet_path,
                           const std::vector<std::string>& columns,
                           const std::string& time_col,
                           const std::string& symbol_col);
}
