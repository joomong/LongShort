#pragma once
#include "loader.h"
#include "simulation_data.h"
#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/writer.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <optional>
#include <unistd.h>

namespace cache_fixture {
constexpr int64_t day = 86400000;
constexpr int64_t d0 = 18262 * day; // 2020-01-01
inline int64_t at(int k) { return d0 + k * day; }
const double kNaN = std::numeric_limits<double>::quiet_NaN();

inline std::shared_ptr<arrow::Array> ids(const std::vector<std::optional<int64_t>>& input) {
    arrow::Int64Builder builder;
    for (auto value : input)
        if (!(value ? builder.Append(*value) : builder.AppendNull()).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}
inline std::shared_ptr<arrow::Array> doubles(const std::vector<double>& input) {
    arrow::DoubleBuilder builder;
    for (double value : input)
        if (!(std::isnan(value) ? builder.AppendNull() : builder.Append(value)).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}
inline std::shared_ptr<arrow::Array> floats(const std::vector<double>& input) {
    arrow::FloatBuilder builder;
    for (double value : input) if (!builder.Append(static_cast<float>(value)).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}
inline std::shared_ptr<arrow::Array> stamps(const std::vector<std::optional<int64_t>>& input_ms) {
    arrow::TimestampBuilder builder(arrow::timestamp(arrow::TimeUnit::NANO), arrow::default_memory_pool());
    for (auto value : input_ms)
        if (!(value ? builder.Append(*value * 1000000) : builder.AppendNull()).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}
inline std::shared_ptr<arrow::Array> strings(const std::vector<std::string>& input) {
    arrow::StringBuilder builder;
    for (const auto& value : input) if (!builder.Append(value).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}
inline std::shared_ptr<arrow::Array> nullable_strings(const std::vector<std::optional<std::string>>& input) {
    arrow::StringBuilder builder;
    for (const auto& value : input)
        if (!(value ? builder.Append(*value) : builder.AppendNull()).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}
inline std::shared_ptr<arrow::Array> bools(const std::vector<bool>& input) {
    arrow::BooleanBuilder builder;
    for (bool value : input) if (!builder.Append(value).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}

struct ThinCache {
    std::filesystem::path dir;
    ThinCache() {
        static std::atomic<unsigned> sequence{0};
        dir = std::filesystem::temp_directory_path() /
              ("longshort_thin_" + std::to_string(getpid()) + "_" + std::to_string(sequence++));
        std::filesystem::create_directories(dir / "cache");
        std::filesystem::create_directories(dir / "groups");

        // base_data: 101..108 on days 0..9; 101 has no row on day 3 (gap). 101 splits 2:1 on day 5.
        std::vector<std::optional<int64_t>> date, id;
        std::vector<double> close, cap, volume;
        for (int k = 0; k < 10; ++k)
            for (int64_t s = 101; s <= 108; ++s) {
                if (s == 101 && k == 3) continue;
                date.push_back(at(k)); id.push_back(s);
                close.push_back(s == 101 && k >= 5 ? 50.0 + k : (s == 101 ? 100.0 + 2 * k : s + 0.5 * k));
                cap.push_back(1e9 + s); volume.push_back(1000.0 * s);
            }
        write("cache/base_data.parquet", {"date", "tradingItemId", "open", "high", "low", "close", "vwap", "volume", "marketCap_USD"},
              {stamps(date), ids(id), floats(close), floats(close), floats(close), floats(close), floats(close),
               doubles(volume), doubles(cap)});
        // adj_returns (split-only): only 101 and 102 rows, one NaN (row with null value).
        write("cache/adj_returns.parquet", {"date", "tradingItemId", "adj_returns"},
              {stamps({at(1), at(2), at(1)}), ids({101, 101, 102}), doubles({0.02, kNaN, 0.004})});
        // returns (total; signal field and holding P&L): 101 day 1, 103 settlement day 6, 102 day 4.
        write("cache/returns.parquet", {"date", "tradingItemId", "returns"},
              {stamps({at(1), at(6), at(4)}), ids({101, 103, 102}), doubles({0.0125, -0.25, -0.1})});
        // adj_factor: 101 factor 2 before day 5, 1 from day 5 on (open validTo); 102 only from day 2.
        write("cache/adj_factor.parquet", {"tradingItemId", "validFrom", "validTo", "factor"},
              {ids({101, 101, 102}), stamps({at(-100), at(5), at(2)}), stamps({at(5), std::nullopt, std::nullopt}),
               doubles({2.0, 1.0, 1.0})});
        // events (deliberately unsorted): 105 -> 106 on day 8 (transitive), 102 spins off 104 on day 4,
        // 103 ends into 105 on day 6, 106 cash-ends on day 9, 107 -> 108 on day 5,
        // 101 -> 108 before the panel (skipped).
        write("cache/events.parquet", {"eventId", "date", "sourceId", "destinationId", "valueRatio", "sourceEnds"},
              {strings({"e1", "e2", "e3", "e4", "e5", "e6"}),
               stamps({at(8), at(4), at(6), at(9), at(5), at(-50)}),
               ids({105, 102, 103, 106, 107, 101}), ids({106, 104, 105, std::nullopt, 108, 108}),
               doubles({0.5, 0.1, 1.0, kNaN, 1.0, 1.0}), bools({true, false, true, true, true, false})});
        // universe: Top500 = 101 (open), 102, 103 until day 6; Top1000 = 107.
        write("universe.parquet", {"universe", "tradingItemId", "fromDate", "toDate"},
              {strings({"Top500", "Top500", "Top500", "Top1000"}), ids({101, 102, 103, 107}),
               stamps({at(0), at(0), at(0), at(0)}), stamps({std::nullopt, at(9), at(6), at(9)})});
        // groups: wide, every id classified.
        std::vector<std::string> names;
        std::vector<std::shared_ptr<arrow::Array>> columns;
        std::vector<std::optional<int64_t>> days;
        for (int k = 0; k < 10; ++k) days.push_back(at(k));
        for (int64_t s = 101; s <= 108; ++s) { names.push_back(std::to_string(s)); columns.push_back(doubles(std::vector<double>(10, 1.0))); }
        names.push_back("__index_level_0__"); columns.push_back(stamps(days));
        write("groups/sector.parquet", names, columns);
        write("groups/industry.parquet", names, columns);
    }
    ~ThinCache() { std::error_code error; std::filesystem::remove_all(dir, error); }
    void write(const std::string& name, const std::vector<std::string>& names,
               const std::vector<std::shared_ptr<arrow::Array>>& arrays) {
        std::vector<std::shared_ptr<arrow::Field>> fields;
        for (size_t i = 0; i < names.size(); ++i) fields.push_back(arrow::field(names[i], arrays[i]->type()));
        auto output = arrow::io::FileOutputStream::Open((dir / name).string()).ValueOrDie();
        auto table = arrow::Table::Make(arrow::schema(fields), arrays);
        if (!parquet::arrow::WriteTable(*table, arrow::default_memory_pool(), output, 16).ok() || !output->Close().ok())
            throw std::runtime_error("Cannot write fixture " + name);
    }
    std::string config(const std::string& universe, const std::string& extra = "",
                       const std::string& backtest_extra = "") const {
        const auto path = dir / "config.yaml";
        std::ofstream(path) << "base:\n  cache: cache\n  groups: groups\n" << universe << extra
                            << "backtest:\n  start_date: '2020-01-01'\n  end_date: latest\n" << backtest_extra;
        return path.string();
    }
};
const std::string top500 = "universe:\n  path: universe.parquet\n  name: Top500\n";
inline unsigned index_of(const DataPanel& panel, const std::string& symbol) {
    auto found = std::find(panel.symbols.begin(), panel.symbols.end(), symbol);
    if (found == panel.symbols.end()) throw std::runtime_error("symbol not loaded: " + symbol);
    return static_cast<unsigned>(found - panel.symbols.begin());
}
}
