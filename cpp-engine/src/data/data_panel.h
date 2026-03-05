#pragma once

#include <string>
#include <vector>
#include <unordered_map>
#include <cstdint>

struct DataPanel {
    unsigned T;  // number of timestamps
    unsigned N;  // number of symbols
    std::vector<int64_t> timestamps;    // epoch_ms, UTC sorted ascending
    std::vector<std::string> symbols;   // alphabetically sorted
    std::unordered_map<std::string, std::vector<float>> arrays;  // "close" -> [T*N] row-major

    float& at(const std::string& col, unsigned t, unsigned n);
    const float& at(const std::string& col, unsigned t, unsigned n) const;
    const float* row(const std::string& col, unsigned t) const;  // pointer to N floats at timestamp t

    // Load from parquet
    static DataPanel load(const std::string& parquet_path,
                          const std::vector<std::string>& columns,
                          const std::string& time_col,
                          const std::string& symbol_col);
};
