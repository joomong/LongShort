#include <gtest/gtest.h>
#include "fixtures/cache_fixture.h"
#include <thread>

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

TEST(ParquetLoader, WarmupAndExplicitPeriodsUseTheCacheCalendar) {
    ThinCache cache;
    auto config = BacktestConfig::from_yaml(cache.config(top500));
    config.start_date = "2020-01-04";
    config.end_date = "2020-01-08";
    config.warmup_bars = 2;
    const auto panel = DataPanel::load(config);
    EXPECT_EQ(panel.T, 7u);
    EXPECT_EQ(panel.timestamps.front(), at(1));
    EXPECT_EQ(panel.timestamps.back(), at(7));
    EXPECT_EQ(panel.trade_start_index, 2u);
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
