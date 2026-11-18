#include <gtest/gtest.h>
#include "fixtures/cache_fixture.h"
#include <cstring>
#include <thread>
#include "module.h"
#include "modules/Data.h"

using namespace cache_fixture;

TEST(ParquetLoader, AflFieldsLoadLazilyAndAliasesShareTheirValues) {
    ThinCache cache;
    std::filesystem::create_directories(cache.dir / "afl");
    cache.write("afl/963.parquet", {"101", "102", "__index_level_0__"},
        {doubles({10.0, kNaN, 30.0}), doubles({20.0, 21.0, 22.0}), stamps({at(0), at(2), at(5)})});
    std::ofstream(cache.dir / "catalog.json") << R"({"fields":{"EPS":"963.parquet","EPS_alias":"963.parquet"}})";
    const auto config = BacktestConfig::from_yaml(cache.config(top500,
        "afl: {path: afl, catalog: catalog.json}\n"));
    const auto panel = DataPanel::load(config);
    EXPECT_TRUE(panel.has_column("AFL.EPS"));
    EXPECT_FALSE(panel.lazy_columns.at("AFL.EPS")->loaded());
    EXPECT_EQ(panel.lazy_columns.at("AFL.EPS"), panel.lazy_columns.at("AFL.EPS_alias"));
    const unsigned a = index_of(panel, "101"), b = index_of(panel, "102");
    const size_t before = panel.loaded_field_count();
    std::thread first([&] { EXPECT_FLOAT_EQ(panel.at("AFL.EPS", 0, a), 10.0f); });
    std::thread second([&] { EXPECT_FLOAT_EQ(panel.at("AFL.EPS_alias", 5, a), 30.0f); });
    first.join(); second.join();
    EXPECT_EQ(panel.loaded_field_count(), before + 1);
    EXPECT_FLOAT_EQ(panel.at("AFL.EPS", 1, a), 10.0f);
    EXPECT_TRUE(std::isnan(panel.at("AFL.EPS", 2, a)));
    EXPECT_TRUE(std::isnan(panel.at("AFL.EPS", 4, a)));
    EXPECT_FLOAT_EQ(panel.at("AFL.EPS", 9, a), 30.0f);
    EXPECT_FLOAT_EQ(panel.at("AFL.EPS", 4, b), 21.0f);
}

TEST(ParquetLoader, RestrictedSelectionStillReadsExecutionFieldsAndHoldingReturns) {
    ThinCache cache;
    auto config = BacktestConfig::from_yaml(cache.config(top500));
    for (const auto& price : {std::string("ohlc4"), std::string("vwap")}) {
        config.execution_price = price;
        const auto panel = DataPanel::load(config, {"close"});
        EXPECT_TRUE(panel.has_column("close"));
        EXPECT_TRUE(panel.has_column(price == "vwap" ? "vwap" : "open"));
        EXPECT_TRUE(panel.has_column("high")); EXPECT_TRUE(panel.has_column("low"));
        EXPECT_FALSE(panel.has_column("sector")); EXPECT_FALSE(panel.has_column("volume"));
        EXPECT_EQ(panel.returns_total.size(), static_cast<size_t>(panel.T) * panel.N);
    }
}

TEST(ParquetLoader, ExplicitPeriodsUseTheCacheCalendar) {
    ThinCache cache;
    auto config = BacktestConfig::from_yaml(cache.config(top500));
    config.start_date = "2020-01-02";
    config.end_date = "2020-01-08";
    const auto panel = DataPanel::load(config);
    EXPECT_EQ(panel.T, 7u);
    EXPECT_EQ(panel.timestamps.front(), at(1));
    EXPECT_EQ(panel.timestamps.back(), at(7));
    EXPECT_TRUE(std::isnan(panel.row("sector", 0)[index_of(panel, "101")]));
    EXPECT_EQ(panel.universe_mask[index_of(panel, "101")], 0);
    EXPECT_EQ(panel.universe_mask[panel.N + index_of(panel, "101")], 1);
}

TEST(ParquetLoader, LatestDropsOnlyTheMissingPriceTail) {
    ThinCache cache;
    const std::vector<double> close{100.0, 101.0, kNaN, 102.0, kNaN};
    cache.write("cache/base_data.parquet", {"date", "tradingItemId", "open", "high", "low", "close", "vwap", "volume", "marketCap_USD"},
        {stamps({at(0), at(1), at(2), at(3), at(4)}), ids({101,101,101,101,101}),
         doubles(close), doubles(close), doubles(close), doubles(close), doubles(close), doubles(close), doubles(close)});
    auto config = BacktestConfig::from_yaml(cache.config(top500));
    config.universe.symbols = {"101"};
    const auto latest = DataPanel::load(config, {"close", "sector"});
    EXPECT_EQ(latest.T, 4u);
    EXPECT_TRUE(std::isnan(latest.at("close", 2, 0)));
    EXPECT_EQ(latest.universe_mask.size(), static_cast<size_t>(latest.T) * latest.N);
    config.end_date = "2020-01-05";
    const auto fixed = DataPanel::load(config, {"close"});
    EXPECT_EQ(fixed.T, 5u);
    EXPECT_TRUE(std::isnan(fixed.at("close", 4, 0)));
}

TEST(ParquetLoader, GroupLabelsCarryForwardBeforeThePreviousBarShift) {
    ThinCache cache;
    cache.write("groups/sector.parquet", {"101", "__index_level_0__"},
        {doubles({1,2}), stamps({at(0),at(2)})});
    const auto panel = DataPanel::load(BacktestConfig::from_yaml(cache.config(top500)), {"sector"});
    const unsigned a = index_of(panel, "101");
    EXPECT_TRUE(std::isnan(panel.at("sector", 0, a)));
    EXPECT_FLOAT_EQ(panel.at("sector", 1, a), 1.0f);
    EXPECT_FLOAT_EQ(panel.at("sector", 2, a), 1.0f);
    EXPECT_FLOAT_EQ(panel.at("sector", 3, a), 2.0f);
    EXPECT_FLOAT_EQ(panel.at("sector", 9, a), 2.0f);
}

// ---- V3Loader: schema 3 AFL (observations, exact alignment) read straight into run panels ----
namespace {
// v3 file: id columns in the writer's numeric order (2 before 10), observations on days 0, 2 and 5 only.
// Column "2" holds a stored 0 on day 2 and a null on day 5.
struct V3Cache {
    ThinCache cache;
    V3Cache() {
        std::filesystem::create_directories(cache.dir / "afl");
        cache.write("afl/4.parquet", {"2", "10", "101", "102", "103", "105", "__index_level_0__"},
            {floats({2.0, 0.0, 0.0}), floats({10.0, 11.0, 12.0}),
             doubles({1.0, 0.0, kNaN}), doubles({5.0, kNaN, 7.0}), doubles({kNaN, 3.0, 4.0}), doubles({0.5, 0.25, 0.125}),
             stamps({at(0), at(2), at(5)})});
        cache.write("afl/4n.parquet", {"2", "10", "__index_level_0__"},
            {doubles({2.0, 0.0, kNaN}), doubles({10.0, 11.0, 12.0}), stamps({at(0), at(2), at(5)})});
        std::ofstream(cache.dir / "catalog.json") <<
            R"({"schema_version":3,"value_semantics":"observations","alignment":"exact",)"
            R"("fields":{"ROE":"4.parquet","ROE_alias":"4.parquet","N":"4n.parquet"}})";
    }
    DataPanel load(const std::string& universe) const {
        auto config = BacktestConfig::from_yaml(cache.config(universe, "afl: {path: afl, catalog: catalog.json}\n"));
        config.end_date = "2020-01-10";
        return DataPanel::load(config);
    }
};
const std::string engine_ids = "universe:\n  symbols: ['10', '2', '99']\n";
}

// Every float compared byte for byte, NaN payloads and signs included.
bool equal_values_and_nan_positions(const std::vector<float>& a, const std::vector<float>& b) {
    return a.size() == b.size() && (a.empty() || std::memcmp(a.data(), b.data(), a.size() * sizeof(float)) == 0);
}

TEST(RunPanelAfl, ComparatorRejectsDifferentNanRepresentations) {
    const float quiet = std::numeric_limits<float>::quiet_NaN();
    float payload;
    const uint32_t bits = 0x7fc00001u;
    std::memcpy(&payload, &bits, sizeof(float));
    ASSERT_TRUE(std::isnan(payload));
    EXPECT_FALSE(equal_values_and_nan_positions({quiet}, {payload}));
    EXPECT_FALSE(equal_values_and_nan_positions({quiet}, {-quiet}));
    EXPECT_TRUE(equal_values_and_nan_positions({quiet, 1.0f}, {quiet, 1.0f}));
}

TEST(V3Loader, ColumnOrder) {
    V3Cache v3;
    const auto parent = v3.load(engine_ids);
    ASSERT_EQ(parent.symbols, (std::vector<std::string>{"10", "2", "99"}));
    const auto run = make_run_panel(parent, "", 0, parent.T);
    const std::vector<float> file_values{2.0f, 10.0f};  // day 0 value of file column 0 ("2") and 1 ("10")
    std::vector<int> file_to_engine_columns;
    for (unsigned n = 0; n < run->N; ++n) {
        const float value = run->at("AFL.ROE", 0, n);
        const auto found = std::find(file_values.begin(), file_values.end(), value);
        file_to_engine_columns.push_back(found == file_values.end() ? -1 : static_cast<int>(found - file_values.begin()));
    }
    EXPECT_EQ(file_to_engine_columns, (std::vector<int>{1,0,-1}));
}

TEST(V3Loader, MissingDate) {
    V3Cache v3;
    const auto parent = v3.load(engine_ids);
    const auto run = make_run_panel(parent, "", 0, parent.T);
    const float missing_date_value = run->at("AFL.ROE", 1, index_of(*run, "10"));  // no day-1 observation
    EXPECT_TRUE(std::isnan(missing_date_value));
    EXPECT_TRUE(std::isnan(run->at("AFL.ROE", 9, index_of(*run, "10"))));
    EXPECT_FLOAT_EQ(run->at("AFL.ROE", 5, index_of(*run, "10")), 12.0f);
    EXPECT_TRUE(std::isnan(run->at("AFL.N", 5, index_of(*run, "2"))));   // stored null
}

TEST(V3Loader, Zero) {
    V3Cache v3;
    const auto parent = v3.load(engine_ids);
    const auto run = make_run_panel(parent, "", 0, parent.T);
    const float valid_zero = run->at("AFL.ROE", 2, index_of(*run, "2"));
    EXPECT_FLOAT_EQ(valid_zero, 0.0f);
    EXPECT_FALSE(std::signbit(valid_zero));
    EXPECT_FLOAT_EQ(run->at("AFL.ROE", 5, index_of(*run, "2")), 0.0f);  // repeated value stays stored
}

TEST(V3Loader, ReferenceEquality) {
    V3Cache v3;
    const auto parent = v3.load(top500);
    const auto run = make_run_panel(parent, "", 1, 2);  // members on bar 1 only: 101, 102, 103
    ASSERT_LT(run->N, parent.N);  // a real column subset
    EXPECT_EQ(run->lazy_columns.at("AFL.ROE"), run->lazy_columns.at("AFL.ROE_alias"));
    std::vector<float> selected, reference;
    for (unsigned t = 0; t < run->T; ++t)
        for (unsigned k = 0; k < run->N; ++k) selected.push_back(run->at("AFL.ROE", t, k));
    EXPECT_FALSE(parent.lazy_columns.at("AFL.ROE")->loaded());  // read from the source, not the parent panel
    for (unsigned t = 0; t < run->T; ++t)
        for (unsigned k = 0; k < run->N; ++k) reference.push_back(parent.at("AFL.ROE", t, index_of(parent, run->symbols[k])));
    EXPECT_TRUE(equal_values_and_nan_positions(selected, reference));
    EXPECT_GT(std::count_if(selected.begin(), selected.end(), [](float v) { return std::isnan(v); }), 0);
    EXPECT_GT(std::count_if(selected.begin(), selected.end(), [](float v) { return v == 0.0f; }), 0);
}

TEST(V3Loader, NanSkip) {
    MData data_module;
    data_module.init();
    EXPECT_TRUE(data_module.skips_nan_init());
}
