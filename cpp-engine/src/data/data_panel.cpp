#include "data_panel.h"
#include "parquet_reader.h"

float& DataPanel::at(const std::string& col, unsigned t, unsigned n) {
    return arrays.at(col)[t * N + n];
}

const float& DataPanel::at(const std::string& col, unsigned t, unsigned n) const {
    return arrays.at(col)[t * N + n];
}

const float* DataPanel::row(const std::string& col, unsigned t) const {
    return arrays.at(col).data() + t * N;
}

DataPanel DataPanel::load(const std::string& parquet_path,
                          const std::vector<std::string>& columns,
                          const std::string& time_col,
                          const std::string& symbol_col) {
    return parquet_reader::load_parquet(parquet_path, columns, time_col, symbol_col);
}
