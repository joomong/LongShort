#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

struct BacktestConfig;

struct LazyColumn {
    explicit LazyColumn(std::function<std::vector<float>()> source) : loader(std::move(source)) {}
    const std::vector<float>& get() const;
    bool loaded() const { return ready.load(std::memory_order_acquire); }
    size_t loaded_bytes() const;
private:
    mutable std::once_flag once;
    mutable std::function<std::vector<float>()> loader;
    mutable std::vector<float> values;
    mutable std::atomic<bool> ready{false};
};

struct DataPanel {
    unsigned T = 0;
    unsigned N = 0;
    unsigned trade_start_index = 0; // earlier rows are available for warmup only
    std::vector<int64_t> timestamps;    // exact epoch_ms, UTC sorted ascending
    std::vector<std::string> symbols;   // lexically sorted stable IDs
    std::unordered_map<std::string, std::vector<float>> arrays;
    // Populated before publication; each shared column loads exactly once. Aliases share values.
    std::unordered_map<std::string, std::shared_ptr<LazyColumn>> lazy_columns;
    std::vector<uint8_t> universe_mask; // T*N membership on the signal date; empty means all

    float& at(const std::string& col, unsigned t, unsigned n);
    const float& at(const std::string& col, unsigned t, unsigned n) const;
    const float* row(const std::string& col, unsigned t) const;
    bool has_column(const std::string& col) const;
    std::vector<std::string> column_names() const;
    size_t loaded_field_count() const;
    size_t loaded_bytes() const;

    static DataPanel load(const BacktestConfig& config,
                          const std::vector<std::string>& selected_fields = {});
    static DataPanel load(const std::string& parquet_path,
                          const std::vector<std::string>& columns,
                          const std::string& time_col,
                          const std::string& symbol_col);
};
