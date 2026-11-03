#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <limits>
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
    std::vector<uint8_t> universe_mask; // T*N; row t = membership known at the previous bar close; empty means all
    // Selectable named sets (universe.names): universe_mask holds default_universe, the others
    // live here. universe_names is empty when the config has a single universe.
    std::vector<std::string> universe_names;
    std::string default_universe;
    std::unordered_map<std::string, std::vector<uint8_t>> universe_masks;
    // Mask for a run: empty name = default; unknown name throws std::invalid_argument.
    const std::vector<uint8_t>& mask_for(const std::string& name) const;
    // Thin cache (docs/data_contract.md§6). The loader
    // sets these when base.cache is a thin-v1 layout; the simulator then books holding P&L
    // as carried weight x returns_total and applies move_events, and nothing else.
    // returns_total is read from the thin cache's returns.parquet (total return; also the
    // `returns` signal field); adj_returns.parquet (split-only) is a signal field only.
    struct MoveEvent {
        unsigned bar = 0;          // panel bar of the event date
        unsigned source = 0;       // panel index of sourceId
        int32_t destination = -1;  // panel index of destinationId; -1 = no stock destination
        double value_ratio = std::numeric_limits<double>::quiet_NaN(); // destination value / source previous value
        bool source_ends = false;  // the source position ends on this bar
    };
    bool thin_layout = false;
    std::vector<double> returns_total;  // T*N row-major; NaN = no row (no P&L on that bar)
    std::vector<MoveEvent> move_events; // sorted by bar, then source

    float& at(const std::string& col, unsigned t, unsigned n);
    const float& at(const std::string& col, unsigned t, unsigned n) const;
    const float* row(const std::string& col, unsigned t) const;
    bool has_column(const std::string& col) const;
    std::vector<std::string> column_names() const;
    size_t loaded_field_count() const;
    size_t loaded_bytes() const;

    static DataPanel load(const BacktestConfig& config,
                          const std::vector<std::string>& selected_fields = {});

};
