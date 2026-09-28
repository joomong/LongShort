// Thin-v1 cache loader (docs/backtest_cache_simplification_plan_2026-09-23.md §6). Each test
// writes a small synthetic thin cache (6 securities + 2 unrelated, 10 days) into a temp dir.
#include <gtest/gtest.h>
#include "module.h"
#include "modules/Data.h"
#include "compiler/expr_parser.h"
#include "data/config_loader.h"
#include "data/data_panel.h"
#include "data/parquet_reader.h"
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

namespace {
constexpr int64_t day = 86400000;
constexpr int64_t d0 = 18262 * day; // 2020-01-01
int64_t at(int k) { return d0 + k * day; }
const double nan = std::numeric_limits<double>::quiet_NaN();

std::shared_ptr<arrow::Array> ids(const std::vector<std::optional<int64_t>>& input) {
    arrow::Int64Builder builder;
    for (auto value : input)
        if (!(value ? builder.Append(*value) : builder.AppendNull()).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}
std::shared_ptr<arrow::Array> doubles(const std::vector<double>& input) {
    arrow::DoubleBuilder builder;
    for (double value : input)
        if (!(std::isnan(value) ? builder.AppendNull() : builder.Append(value)).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}
std::shared_ptr<arrow::Array> floats(const std::vector<double>& input) {
    arrow::FloatBuilder builder;
    for (double value : input) if (!builder.Append(static_cast<float>(value)).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}
std::shared_ptr<arrow::Array> stamps(const std::vector<std::optional<int64_t>>& input_ms) {
    arrow::TimestampBuilder builder(arrow::timestamp(arrow::TimeUnit::NANO), arrow::default_memory_pool());
    for (auto value : input_ms)
        if (!(value ? builder.Append(*value * 1000000) : builder.AppendNull()).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}
std::shared_ptr<arrow::Array> strings(const std::vector<std::string>& input) {
    arrow::StringBuilder builder;
    for (const auto& value : input) if (!builder.Append(value).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}
std::shared_ptr<arrow::Array> nullable_strings(const std::vector<std::optional<std::string>>& input) {
    arrow::StringBuilder builder;
    for (const auto& value : input)
        if (!(value ? builder.Append(*value) : builder.AppendNull()).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}
std::shared_ptr<arrow::Array> bools(const std::vector<bool>& input) {
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
        std::ofstream(dir / "cache" / "manifest.json") << R"({"layout": "thin-v1", "source_build_id": "test"})";

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
              {stamps({at(1), at(2), at(1)}), ids({101, 101, 102}), doubles({0.02, nan, 0.004})});
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
               doubles({0.5, 0.1, 1.0, nan, 1.0, 1.0}), bools({true, false, true, true, true, false})});
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
unsigned index_of(const DataPanel& panel, const std::string& symbol) {
    auto found = std::find(panel.symbols.begin(), panel.symbols.end(), symbol);
    if (found == panel.symbols.end()) throw std::runtime_error("symbol not loaded: " + symbol);
    return static_cast<unsigned>(found - panel.symbols.begin());
}
}

TEST(ThinLoader, LabelsPanelSymbolsFromThinMaster) {
    ThinCache cache;
    cache.write("cache/master.parquet", {"tradingItemId", "symbol", "companyName"},
                {ids({101, 102, 103, 999}),
                 nullable_strings({" AAA ", std::nullopt, " ", "OTHER"}),
                 nullable_strings({"Alpha Corp", "Beta Corp", std::nullopt, "Unselected Corp"})});
    const auto config = BacktestConfig::from_yaml(cache.config(top500));
    ASSERT_TRUE(config.base_thin);
    const auto labels = parquet_reader::load_thin_symbol_labels(
        config, {"101", "102", "103", "104"});
    EXPECT_EQ(labels.size(), 2u);
    EXPECT_EQ(labels.at("101"), "AAA");
    EXPECT_EQ(labels.at("102"), "Beta Corp");
    EXPECT_EQ(labels.count("103"), 0u);
    EXPECT_EQ(labels.count("999"), 0u);
}

TEST(ThinLoader, DetectsLayoutAndLoadsRawPricesAndFactors) {
    ThinCache cache;
    const auto config = BacktestConfig::from_yaml(cache.config(top500));
    EXPECT_TRUE(config.base_thin);
    const auto panel = DataPanel::load(config);
    EXPECT_TRUE(panel.thin_layout);
    ASSERT_EQ(panel.T, 10u);
    EXPECT_EQ(panel.timestamps.front(), at(0));
    const unsigned a = index_of(panel, "101"), b = index_of(panel, "102");
    // Raw close: no split adjustment; the day-3 gap is NaN.
    EXPECT_FLOAT_EQ(panel.at("close", 4, a), 108.0f);
    EXPECT_TRUE(std::isnan(panel.at("close", 3, a)));
    EXPECT_FLOAT_EQ(panel.at("close", 5, a), 55.0f);
    EXPECT_FLOAT_EQ(panel.at("cap", 0, a), static_cast<float>(1e9 + 101));
    EXPECT_FLOAT_EQ(panel.at("volume", 0, b), 102000.0f);
    // adj_factor: [validFrom, validTo) by day, open validTo, NaN without an interval.
    EXPECT_FLOAT_EQ(panel.at("adj_factor", 4, a), 2.0f);
    EXPECT_FLOAT_EQ(panel.at("adj_factor", 5, a), 1.0f);
    EXPECT_FLOAT_EQ(panel.at("adj_factor", 9, a), 1.0f);
    EXPECT_TRUE(std::isnan(panel.at("adj_factor", 1, b)));
    EXPECT_FLOAT_EQ(panel.at("adj_factor", 2, b), 1.0f);
    EXPECT_TRUE(std::isnan(panel.at("adj_factor", 0, index_of(panel, "103"))));
    // adj_returns: a normal field; null value and absent rows are NaN.
    EXPECT_FLOAT_EQ(panel.at("adj_returns", 1, a), 0.02f);
    EXPECT_TRUE(std::isnan(panel.at("adj_returns", 2, a)));
    EXPECT_FLOAT_EQ(panel.at("adj_returns", 1, b), 0.004f);
    EXPECT_TRUE(std::isnan(panel.at("adj_returns", 5, b)));
    // returns: the total return field, the same file as holding P&L.
    EXPECT_FLOAT_EQ(panel.at("returns", 1, a), 0.0125f);
    EXPECT_FLOAT_EQ(panel.at("returns", 4, b), -0.1f);
    EXPECT_TRUE(std::isnan(panel.at("returns", 2, a)));
    // Groups keep the current wide panel.
    EXPECT_FLOAT_EQ(panel.at("sector", 0, a), 1.0f);
}

TEST(ThinLoader, ReturnsTotalIsDoubleWithNaNForMissingRows) {
    ThinCache cache;
    const auto panel = DataPanel::load(BacktestConfig::from_yaml(cache.config(top500)));
    ASSERT_EQ(panel.returns_total.size(), static_cast<size_t>(panel.T) * panel.N);
    const auto rt = [&](unsigned t, const std::string& s) { return panel.returns_total[t * panel.N + index_of(panel, s)]; };
    EXPECT_DOUBLE_EQ(rt(1, "101"), 0.0125);
    EXPECT_DOUBLE_EQ(rt(6, "103"), -0.25);
    EXPECT_DOUBLE_EQ(rt(4, "102"), -0.1);
    EXPECT_TRUE(std::isnan(rt(2, "101")));
    EXPECT_TRUE(std::isnan(rt(6, "104")));
    size_t finite = 0;
    for (double value : panel.returns_total) finite += std::isfinite(value);
    EXPECT_EQ(finite, 3u);
}

TEST(ThinLoader, LoadsDestinationsTransitivelyAndMapsSortedMoveEvents) {
    ThinCache cache;
    const auto panel = DataPanel::load(BacktestConfig::from_yaml(cache.config(top500)));
    // 101..103 from the universe, 104 (spin-off of 102), 105 (successor of 103) and 106
    // (successor of 105). 107 is not loaded, so its destination 108 is not either; the
    // pre-panel 101 -> 108 row adds nothing.
    EXPECT_EQ(panel.symbols, (std::vector<std::string>{"101", "102", "103", "104", "105", "106"}));
    ASSERT_EQ(panel.move_events.size(), 4u);
    const auto& e = panel.move_events;
    const unsigned s102 = index_of(panel, "102"), s103 = index_of(panel, "103"), s105 = index_of(panel, "105"),
                   s104 = index_of(panel, "104"), s106 = index_of(panel, "106");
    EXPECT_EQ(e[0].bar, 4u); EXPECT_EQ(e[0].source, s102); EXPECT_EQ(e[0].destination, static_cast<int32_t>(s104));
    EXPECT_DOUBLE_EQ(e[0].value_ratio, 0.1); EXPECT_FALSE(e[0].source_ends);
    EXPECT_EQ(e[1].bar, 6u); EXPECT_EQ(e[1].source, s103); EXPECT_EQ(e[1].destination, static_cast<int32_t>(s105));
    EXPECT_TRUE(e[1].source_ends);
    EXPECT_EQ(e[2].bar, 8u); EXPECT_EQ(e[2].source, s105); EXPECT_EQ(e[2].destination, static_cast<int32_t>(s106));
    EXPECT_DOUBLE_EQ(e[2].value_ratio, 0.5);
    EXPECT_EQ(e[3].bar, 9u); EXPECT_EQ(e[3].source, s106); EXPECT_EQ(e[3].destination, -1);
    EXPECT_TRUE(std::isnan(e[3].value_ratio)); EXPECT_TRUE(e[3].source_ends);
}

TEST(ThinLoader, IntervalUniverseBuildsInclusiveMask) {
    ThinCache cache;
    const auto panel = DataPanel::load(BacktestConfig::from_yaml(cache.config(top500)));
    ASSERT_EQ(panel.universe_mask.size(), static_cast<size_t>(panel.T) * panel.N);
    const auto member = [&](unsigned t, const std::string& s) { return panel.universe_mask[t * panel.N + index_of(panel, s)]; };
    EXPECT_EQ(member(0, "101"), 1); EXPECT_EQ(member(9, "101"), 1);
    EXPECT_EQ(member(9, "102"), 1);                       // toDate inclusive
    EXPECT_EQ(member(6, "103"), 1); EXPECT_EQ(member(7, "103"), 0);
    EXPECT_EQ(member(0, "104"), 0); EXPECT_EQ(member(9, "106"), 0); // added destinations are never members
    // Another name selects another set: 107, plus 108 it moves into on day 5.
    ThinCache other;
    const auto panel2 = DataPanel::load(BacktestConfig::from_yaml(
        other.config("universe:\n  path: universe.parquet\n  name: Top1000\n")));
    EXPECT_EQ(panel2.symbols, (std::vector<std::string>{"107", "108"}));
    EXPECT_EQ(panel2.universe_mask[0 * panel2.N + 1], 0);
}

TEST(ThinLoader, ReturnsExpressionUsesTheFieldOnThinPanels) {
    ThinCache cache;
    const auto panel = DataPanel::load(BacktestConfig::from_yaml(cache.config(top500)));
    std::vector<std::unique_ptr<Module>> owned;
    auto* root = dynamic_cast<MData*>(parse_expr("returns", &panel, owned));
    ASSERT_NE(root, nullptr);
    EXPECT_EQ(root->column_name, "returns");
    // A panel without a returns field keeps the close-derived expression.
    DataPanel wide;
    wide.T = 1; wide.N = 1;
    wide.arrays["close"] = {1.0f};
    std::vector<std::unique_ptr<Module>> owned2;
    Module* derived = parse_expr("returns", &wide, owned2);
    EXPECT_EQ(dynamic_cast<MData*>(derived), nullptr);
    EXPECT_GT(owned2.size(), 3u);
}

TEST(ThinLoader, MissingUniverseNameIsRejected) {
    ThinCache cache;
    const auto config = BacktestConfig::from_yaml(cache.config("universe:\n  path: universe.parquet\n"));
    try {
        DataPanel::load(config);
        FAIL() << "interval universe accepted without a name";
    } catch (const std::runtime_error& error) {
        EXPECT_NE(std::string(error.what()).find("universe.name"), std::string::npos);
    }
}

TEST(ThinLoader, OldReturnsTotalNamingIsRejected) {
    ThinCache cache;
    std::ofstream(cache.dir / "cache" / "returns_total.parquet") << "old";
    try {
        BacktestConfig::from_yaml(cache.config(top500));
        FAIL() << "thin cache with returns_total.parquet accepted";
    } catch (const std::runtime_error& error) {
        EXPECT_NE(std::string(error.what()).find("rebuild"), std::string::npos);
    }
}

TEST(ThinLoader, OtherManifestLayoutKeepsTheWidePath) {
    ThinCache cache;
    std::ofstream(cache.dir / "cache" / "manifest.json") << R"({"layout": "wide"})";
    const auto config = BacktestConfig::from_yaml(cache.config(top500));
    EXPECT_FALSE(config.base_thin);
    EXPECT_EQ(config.datasets.front().path, (std::filesystem::path("cache") / "BaseData").string());
}

TEST(ThinLoader, RetiresSourcesThatEndedBeforeThePanelAtBarZero) {
    ThinCache cache;
    // 102 ended before the panel (no destination row is needed: nothing can be held yet);
    // 101's pre-panel row does not end it and stays skipped.
    cache.write("cache/events.parquet", {"eventId", "date", "sourceId", "destinationId", "valueRatio", "sourceEnds"},
                {strings({"p1", "p2"}), stamps({at(-20), at(-50)}), ids({102, 101}), ids({std::nullopt, 108}),
                 doubles({std::numeric_limits<double>::quiet_NaN(), 1.0}), bools({true, false})});
    const auto panel = DataPanel::load(BacktestConfig::from_yaml(cache.config(top500)));
    ASSERT_EQ(panel.move_events.size(), 1u);
    const auto& e = panel.move_events.front();
    EXPECT_EQ(e.bar, 0u);
    EXPECT_EQ(e.source, index_of(panel, "102"));
    EXPECT_EQ(e.destination, -1);
    EXPECT_TRUE(std::isnan(e.value_ratio));
    EXPECT_TRUE(e.source_ends);
}
