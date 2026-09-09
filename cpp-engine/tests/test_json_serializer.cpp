#include <gtest/gtest.h>
#include "api/json_serializer.h"

#include <limits>

TEST(JsonSerializer, SummaryPreservesLegacyKeysForPositionCounts) {
    SummaryMetrics metrics;
    metrics.avg_position_count = 3.0;
    metrics.min_position_count = 2.0;
    metrics.active_bar_count = 3;

    const Json::Value json = summary_to_json(metrics);
    EXPECT_DOUBLE_EQ(json["minBet"].asDouble(), 3.0);
    EXPECT_DOUBLE_EQ(json["minBetMin"].asDouble(), 2.0);
    EXPECT_EQ(json["nonzeroPosition"].asInt(), 3);
    EXPECT_FALSE(json.isMember("avg_position_count"));
    EXPECT_FALSE(json.isMember("min_position_count"));
}

TEST(JsonSerializer, TimeseriesPreservesLegacyPositionCountKey) {
    TimeseriesOutput timeseries;
    timeseries.position_count = {2, 4, 3};

    const Json::Value json = timeseries_to_json(timeseries);
    ASSERT_EQ(json["nonzeroCount"].size(), 3u);
    EXPECT_EQ(json["nonzeroCount"][0].asInt(), 2);
    EXPECT_EQ(json["nonzeroCount"][1].asInt(), 4);
    EXPECT_EQ(json["nonzeroCount"][2].asInt(), 3);
    EXPECT_FALSE(json.isMember("position_count"));
}

TEST(JsonSerializer, PlotOffsetKeepsDatesAndSignalRowsAligned) {
    const float nan = std::numeric_limits<float>::quiet_NaN();
    const float inf = std::numeric_limits<float>::infinity();
    const std::vector<std::vector<float>> signals = {{10, 20}, {nan, inf}, {-1, 0}};
    const std::vector<int64_t> timestamps = {0, 86400000, 172800000};
    const std::vector<std::string> symbols = {"AAA", "BBB"};

    const Json::Value json = signal_plot_to_json(signals, timestamps, symbols, 1);
    ASSERT_EQ(json["timestamps"].size(), 2u);
    EXPECT_EQ(json["timestamps"][0].asString(), "1970-01-02");
    EXPECT_EQ(json["timestamps"][1].asString(), "1970-01-03");
    ASSERT_EQ(json["symbols"].size(), 2u);
    EXPECT_EQ(json["symbols"][0].asString(), "AAA");
    EXPECT_EQ(json["symbols"][1].asString(), "BBB");
    ASSERT_EQ(json["values"].size(), 2u);
    EXPECT_TRUE(json["values"][0][0].isNull());
    EXPECT_TRUE(json["values"][0][1].isNull());
    EXPECT_DOUBLE_EQ(json["values"][1][0].asDouble(), -1.0);
    EXPECT_DOUBLE_EQ(json["values"][1][1].asDouble(), 0.0);
}

TEST(JsonSerializer, EmptyPlotRangeRetainsSymbols) {
    const std::vector<std::vector<float>> signals = {{1}};
    const std::vector<int64_t> timestamps = {0};
    const std::vector<std::string> symbols = {"AAA"};

    for (std::size_t start : {signals.size(), signals.size() + 1}) {
        const Json::Value json = signal_plot_to_json(signals, timestamps, symbols, start);
        EXPECT_TRUE(json["timestamps"].isArray());
        EXPECT_TRUE(json["values"].isArray());
        EXPECT_TRUE(json["timestamps"].empty());
        EXPECT_TRUE(json["values"].empty());
        ASSERT_EQ(json["symbols"].size(), 1u);
        EXPECT_EQ(json["symbols"][0].asString(), "AAA");
    }
}

TEST(JsonSerializer, HistogramOffsetExcludesWarmupAndNonfiniteOrTinySignals) {
    const float nan = std::numeric_limits<float>::quiet_NaN();
    const float inf = std::numeric_limits<float>::infinity();
    const std::vector<std::vector<float>> signals = {
        {-100, 100}, {-2, 0, nan, inf}, {2, -2, 1e-13f, 1e-12f, -1e-12f}
    };

    const Json::Value json = signal_distribution_to_json(signals, 1);
    EXPECT_DOUBLE_EQ(json["min"].asDouble(), -2.0);
    EXPECT_DOUBLE_EQ(json["max"].asDouble(), 2.0);
    ASSERT_EQ(json["bins"].size(), 50u);
    ASSERT_EQ(json["counts"].size(), 50u);
    EXPECT_EQ(json["counts"][0].asInt(), 2);
    EXPECT_EQ(json["counts"][49].asInt(), 1);
    int total = 0;
    for (const auto& count : json["counts"])
        total += count.asInt();
    EXPECT_EQ(total, 3);
}

TEST(JsonSerializer, ConstantHistogramKeepsAllValuesInFirstOfFiftyBins) {
    const Json::Value json = signal_distribution_to_json({{3, 3}, {3}});
    EXPECT_DOUBLE_EQ(json["min"].asDouble(), 3.0);
    EXPECT_DOUBLE_EQ(json["max"].asDouble(), 3.0);
    ASSERT_EQ(json["bins"].size(), 50u);
    ASSERT_EQ(json["counts"].size(), 50u);
    EXPECT_NEAR(json["bins"][0].asDouble(), 3.01, 1e-6);
    EXPECT_EQ(json["counts"][0].asInt(), 3);
    for (Json::ArrayIndex i = 1; i < json["counts"].size(); ++i)
        EXPECT_EQ(json["counts"][i].asInt(), 0);
}

TEST(JsonSerializer, HistogramWithNoSelectedValuesKeepsEmptyContract) {
    const float nan = std::numeric_limits<float>::quiet_NaN();
    const float inf = std::numeric_limits<float>::infinity();
    const std::vector<std::vector<float>> signals = {{2}, {0, nan, inf, 1e-13f}};

    for (std::size_t start : {std::size_t{1}, signals.size(), signals.size() + 1}) {
        const Json::Value json = signal_distribution_to_json(signals, start);
        EXPECT_TRUE(json["bins"].isArray());
        EXPECT_TRUE(json["counts"].isArray());
        EXPECT_TRUE(json["bins"].empty());
        EXPECT_TRUE(json["counts"].empty());
        EXPECT_DOUBLE_EQ(json["min"].asDouble(), 0.0);
        EXPECT_DOUBLE_EQ(json["max"].asDouble(), 0.0);
    }
}
