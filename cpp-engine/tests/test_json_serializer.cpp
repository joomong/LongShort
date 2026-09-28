#include <gtest/gtest.h>
#include "api/json_serializer.h"

#include <limits>
#include <cmath>

TEST(JsonSerializer, YearlyWinLoseKeepsPercentagePointsIncludingZero) {
    YearlyMetrics winning_year{};
    winning_year.year = 2024;
    winning_year.win_lose = 62.5;
    YearlyMetrics losing_year{};
    losing_year.year = 2025;
    losing_year.win_lose = 0.0;

    const Json::Value json = yearly_to_json({winning_year, losing_year});
    ASSERT_EQ(json.size(), 2u);
    EXPECT_EQ(json[0]["year"].asInt(), 2024);
    ASSERT_TRUE(json[0]["winLose"].isNumeric());
    EXPECT_DOUBLE_EQ(json[0]["winLose"].asDouble(), 62.5);
    EXPECT_EQ(json[1]["year"].asInt(), 2025);
    ASSERT_TRUE(json[1]["winLose"].isNumeric());
    EXPECT_DOUBLE_EQ(json[1]["winLose"].asDouble(), 0.0);
}

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
    ASSERT_EQ(json["bins"].size(), 200u);
    ASSERT_EQ(json["counts"].size(), 200u);
    // lo == vmin lands the minimum in the first inner bin; v >= hi goes to the top overflow bin
    EXPECT_EQ(json["counts"][1].asInt(), 2);
    EXPECT_EQ(json["counts"][199].asInt(), 1);
    int total = 0;
    for (const auto& count : json["counts"])
        total += count.asInt();
    EXPECT_EQ(total, 3);
}

TEST(JsonSerializer, ConstantHistogramKeepsAllValuesInFirstOfBins) {
    const Json::Value json = signal_distribution_to_json({{3, 3}, {3}});
    EXPECT_DOUBLE_EQ(json["min"].asDouble(), 3.0);
    EXPECT_DOUBLE_EQ(json["max"].asDouble(), 3.0);
    ASSERT_EQ(json["bins"].size(), 200u);
    ASSERT_EQ(json["counts"].size(), 200u);
    // constant data: the range degenerates to [3, 4) and every value lands in the first inner bin
    EXPECT_EQ(json["counts"][1].asInt(), 3);
    for (Json::ArrayIndex i = 0; i < json["counts"].size(); ++i)
        if (i != 1) EXPECT_EQ(json["counts"][i].asInt(), 0);
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


TEST(JsonSerializer, SignalPlotCellBudgetPreservesRawValuesDatesAndLatestRow) {
    std::vector<std::vector<float>> signals(10, std::vector<float>(4));
    std::vector<int64_t> dates(10);
    for (std::size_t di = 0; di < signals.size(); ++di) {
        dates[di] = static_cast<int64_t>(di) * 86400000;
        for (std::size_t ii = 0; ii < 4; ++ii) signals[di][ii] = static_cast<float>(10 * di + ii);
    }
    const auto json = signal_plot_to_json(signals, dates, {"A", "B", "C", "D"}, 0, 12);
    ASSERT_EQ(json["values"].size(), 3u);
    EXPECT_EQ(json["values"].size() * json["symbols"].size(), 12u);
    EXPECT_TRUE(json["sampled"].asBool());
    EXPECT_EQ(json["originalRows"].asUInt(), 10u);
    EXPECT_EQ(json["timestamps"][0].asString(), "1970-01-01");
    EXPECT_EQ(json["timestamps"][2].asString(), "1970-01-10");
    EXPECT_DOUBLE_EQ(json["values"][0][0].asDouble(), 0.0);
    EXPECT_DOUBLE_EQ(json["values"][1][2].asDouble(), 52.0);
    EXPECT_DOUBLE_EQ(json["values"][2][3].asDouble(), 93.0);
}

TEST(JsonSerializer, SignalPlotBudgetAlsoBoundsVeryWidePanels) {
    const std::vector<std::vector<float>> signals = {{0, 1, 2, 3, 4}, {5, 6, 7, 8, 9}};
    const auto json = signal_plot_to_json(signals, {0, 86400000}, {"A", "B", "C", "D", "E"}, 0, 3);
    EXPECT_LE(json["values"].size() * json["symbols"].size(), 3u);
    EXPECT_EQ(json["timestamps"][0].asString(), "1970-01-02");
    EXPECT_EQ(json["symbols"][2].asString(), "E");
    EXPECT_DOUBLE_EQ(json["values"][0][2].asDouble(), 9.0);
    EXPECT_GT(json["symbolStride"].asUInt(), 1u);
}


TEST(JsonSerializer, HistogramKeepsFiniteBinsForOppositeFloatExtremes) {
    const float maximum = std::numeric_limits<float>::max();
    const auto json = signal_distribution_to_json({{-maximum, maximum}});
    ASSERT_EQ(json["bins"].size(), 200u);
    for (const auto& bin : json["bins"]) EXPECT_TRUE(std::isfinite(bin.asDouble()));
    EXPECT_EQ(json["counts"][1].asInt(), 1);
    EXPECT_EQ(json["counts"][199].asInt(), 1);
    EXPECT_DOUBLE_EQ(json["min"].asDouble(), -static_cast<double>(maximum));
    EXPECT_DOUBLE_EQ(json["max"].asDouble(), static_cast<double>(maximum));
}


TEST(JsonSerializer, TimeseriesCarriesFrozenGrossAcrossTheTradingSpan) {
    const std::vector<int64_t> timestamps = {0, 86400000, 172800000};
    const std::vector<double> ret(3, 0.0), turnover(3, 0.0), max_weight(3, 0.0);
    const std::vector<int> position_count(3, 0);
    const std::vector<double> frozen = {9.0, 0.25, 0.5};
    const auto series = compute_timeseries(timestamps, ret, turnover, max_weight,
                                           position_count, 0.0005, 1, &frozen);
    ASSERT_EQ(series.frozen_gross.size(), 2u);
    EXPECT_DOUBLE_EQ(series.frozen_gross[0], 0.25);
    EXPECT_DOUBLE_EQ(series.frozen_gross[1], 0.5);
    const Json::Value json = timeseries_to_json(series);
    ASSERT_EQ(json["frozenGross"].size(), 2u);
    EXPECT_DOUBLE_EQ(json["frozenGross"][0].asDouble(), 0.25);
    EXPECT_DOUBLE_EQ(json["frozenGross"][1].asDouble(), 0.5);
    EXPECT_EQ(series.timestamps.size(), series.frozen_gross.size());
}

TEST(JsonSerializer, SignalPlotSymbolSubsetReturnsEveryRowForThoseColumns) {
    std::vector<std::vector<float>> signals(10, std::vector<float>(4));
    std::vector<int64_t> dates(10);
    for (std::size_t di = 0; di < signals.size(); ++di) {
        dates[di] = static_cast<int64_t>(di) * 86400000;
        for (std::size_t ii = 0; ii < 4; ++ii) signals[di][ii] = static_cast<float>(10 * di + ii);
    }
    const std::vector<std::size_t> subset = {2};
    const auto json = signal_plot_to_json(signals, dates, {"A", "B", "C", "D"}, 1, 4, &subset);
    ASSERT_EQ(json["values"].size(), 9u);  // rows from start_index, no sampling
    ASSERT_EQ(json["symbols"].size(), 1u);
    EXPECT_EQ(json["symbols"][0].asString(), "C");
    EXPECT_EQ(json["rowStride"].asUInt(), 1u);
    EXPECT_DOUBLE_EQ(json["values"][0][0].asDouble(), 12.0);
    EXPECT_DOUBLE_EQ(json["values"][8][0].asDouble(), 92.0);
}

TEST(JsonSerializer, SignalPlotMembershipFollowsSampledCells) {
    const std::vector<std::vector<float>> signals = {{1, 2, 3}, {4, 5, 6}, {7, 8, 9}};
    const std::vector<int64_t> dates = {0, 86400000, 2 * 86400000};
    // B leaves the universe on the last row; C is out on the first row.
    const std::vector<uint8_t> mask = {1, 1, 0, 1, 1, 1, 1, 0, 1};
    const std::vector<std::size_t> subset = {1, 2};
    const auto json = signal_plot_to_json(signals, dates, {"A", "B", "C"}, 1, 9, &subset, &mask);
    ASSERT_EQ(json["membership"].size(), 2u);
    EXPECT_EQ(json["membership"][0].asString(), "11");
    EXPECT_EQ(json["membership"][1].asString(), "01");
    EXPECT_FALSE(signal_plot_to_json(signals, dates, {"A", "B", "C"})
                     .isMember("membership"));
}

TEST(JsonSerializer, HistogramNamesLargestCellPerBin) {
    const std::vector<std::vector<float>> signals = {{-100, 1}, {-2, 2}, {50, 3}};
    const std::vector<int64_t> dates = {0, 86400000, 2 * 86400000};
    const std::vector<std::string> symbols = {"A", "B"};
    const auto json = signal_distribution_to_json(signals, 0, &dates, &symbols);
    ASSERT_EQ(json["exampleSymbol"].size(), 200u);
    // range [-100, 3): -100 is the first inner bin; 3 and 50 spill into the top overflow bin, 50 wins on |value|
    EXPECT_EQ(json["exampleSymbol"][1].asString(), "A");
    EXPECT_EQ(json["exampleDate"][1].asString(), "1970-01-01");
    EXPECT_EQ(json["exampleSymbol"][199].asString(), "A");
    EXPECT_EQ(json["exampleDate"][199].asString(), "1970-01-03");
    EXPECT_TRUE(json["exampleSymbol"][0].isNull());
}

namespace {
void expect_full_log_histogram(const Json::Value& json, Json::UInt64 total) {
    ASSERT_EQ(json["bins"].size(), 200u);
    ASSERT_EQ(json["edges"].size(), 201u);
    ASSERT_EQ(json["counts"].size(), 200u);
    Json::UInt64 sum = 0;
    for (Json::ArrayIndex i = 0; i < 200; ++i) {
        ASSERT_TRUE(json["counts"][i].isUInt64());
        sum += json["counts"][i].asUInt64();
        const double left = json["edges"][i].asDouble();
        const double right = json["edges"][i + 1].asDouble();
        const double center = json["bins"][i].asDouble();
        EXPECT_TRUE(std::isfinite(left));
        EXPECT_TRUE(std::isfinite(right));
        EXPECT_TRUE(std::isfinite(center));
        EXPECT_LT(left, right);
        EXPECT_GE(center, left);
        EXPECT_LE(center, right);
    }
    EXPECT_EQ(sum, total);
    EXPECT_EQ(json["totalCount"].asUInt64(), total);
    EXPECT_DOUBLE_EQ(json["rangeLo"].asDouble(), json["edges"][0].asDouble());
    EXPECT_DOUBLE_EQ(json["rangeHi"].asDouble(), json["edges"][200].asDouble());
}
}

TEST(JsonSerializer, LogHistogramConservesFullPositiveTailAndTinyValues) {
    const std::vector<std::vector<float>> signals = {{1e-6f, 1e-12f, 1e-6f, 1, 100, 3.58e9f}};
    const auto json = signal_log_distribution_to_json(signals);
    expect_full_log_histogram(json, 6);
    EXPECT_EQ(json["scale"].asString(), "log");
    EXPECT_DOUBLE_EQ(json["min"].asDouble(), static_cast<double>(1e-12f));
    EXPECT_DOUBLE_EQ(json["max"].asDouble(), static_cast<double>(3.58e9f));
    EXPECT_DOUBLE_EQ(json["rangeLo"].asDouble(), json["min"].asDouble());
    EXPECT_DOUBLE_EQ(json["rangeHi"].asDouble(), json["max"].asDouble());
    EXPECT_EQ(json["counts"][0].asUInt64(), 1u);
    EXPECT_EQ(json["counts"][199].asUInt64(), 1u);
    const double ratio0 = json["edges"][1].asDouble() / json["edges"][0].asDouble();
    const double ratio100 = json["edges"][101].asDouble() / json["edges"][100].asDouble();
    EXPECT_NEAR(ratio0, ratio100, 1e-12);
}

TEST(JsonSerializer, NegativeLogHistogramIsIncreasingInRawUnits) {
    const auto json = signal_log_distribution_to_json({{-3.58e9f, -100, -1, -1e-6f}});
    expect_full_log_histogram(json, 4);
    EXPECT_EQ(json["scale"].asString(), "negative-log");
    EXPECT_LT(json["bins"][0].asDouble(), json["bins"][199].asDouble());
    EXPECT_EQ(json["counts"][0].asUInt64(), 1u);
    EXPECT_EQ(json["counts"][199].asUInt64(), 1u);
}

TEST(JsonSerializer, SymlogPreservesZerosAndOppositeFloatExtremes) {
    const float maximum = std::numeric_limits<float>::max();
    const float tiny = std::numeric_limits<float>::denorm_min();
    const auto json = signal_log_distribution_to_json({{-maximum, -tiny, 0, tiny, maximum, 0}});
    expect_full_log_histogram(json, 6);
    EXPECT_EQ(json["scale"].asString(), "symlog");
    EXPECT_DOUBLE_EQ(json["linthresh"].asDouble(), static_cast<double>(tiny));
    EXPECT_DOUBLE_EQ(json["min"].asDouble(), -static_cast<double>(maximum));
    EXPECT_DOUBLE_EQ(json["max"].asDouble(), static_cast<double>(maximum));
    EXPECT_EQ(json["counts"][0].asUInt64(), 1u);
    EXPECT_EQ(json["counts"][199].asUInt64(), 1u);
    EXPECT_EQ(json["counts"][99].asUInt64() + json["counts"][100].asUInt64(), 4u);
    const auto zero_positive = signal_log_distribution_to_json({{0, 1e-12f, 3.58e9f}});
    EXPECT_EQ(zero_positive["scale"].asString(), "symlog");
    expect_full_log_histogram(zero_positive, 3);
}

TEST(JsonSerializer, LogHistogramConstantSignalsUseOneFiniteBin) {
    for (float constant : {0.f, 3.f, -3.f, std::numeric_limits<float>::max(), std::numeric_limits<float>::denorm_min()}) {
        const auto json = signal_log_distribution_to_json({{constant, constant}, {constant}});
        expect_full_log_histogram(json, 3);
        int occupied = 0;
        for (const auto& count : json["counts"]) if (count.asUInt64() > 0) {
            ++occupied;
            EXPECT_EQ(count.asUInt64(), 3u);
        }
        EXPECT_EQ(occupied, 1);
        EXPECT_LT(json["rangeLo"].asDouble(), static_cast<double>(constant));
        EXPECT_GT(json["rangeHi"].asDouble(), static_cast<double>(constant));
        if (constant == 0) EXPECT_DOUBLE_EQ(json["linthresh"].asDouble(), 1.0);
    }
}

TEST(JsonSerializer, LogHistogramMissingPopulationAndOffsetAreExplicit) {
    const float nan = std::numeric_limits<float>::quiet_NaN();
    const float inf = std::numeric_limits<float>::infinity();
    const std::vector<std::vector<float>> signals = {{42}, {nan, inf, -inf}};
    for (std::size_t start : {std::size_t{1}, signals.size(), signals.size() + 1}) {
        const auto json = signal_log_distribution_to_json(signals, start);
        EXPECT_TRUE(json["bins"].empty());
        EXPECT_TRUE(json["counts"].empty());
        EXPECT_TRUE(json["edges"].empty());
        EXPECT_EQ(json["totalCount"].asUInt64(), 0u);
        EXPECT_DOUBLE_EQ(json["min"].asDouble(), 0);
        EXPECT_DOUBLE_EQ(json["max"].asDouble(), 0);
    }
    const auto json = signal_log_distribution_to_json({{10000}, {0, nan, inf, -inf, -1e-20f}}, 1);
    expect_full_log_histogram(json, 2);
    EXPECT_DOUBLE_EQ(json["max"].asDouble(), 0);
    EXPECT_DOUBLE_EQ(json["min"].asDouble(), static_cast<double>(-1e-20f));
}

TEST(JsonSerializer, LogHistogramUsesFullDataAndTrueNavigableExamplesDespitePlotSampling) {
    const std::vector<std::vector<float>> signals = {{999, 999}, {1e-6f, 2}, {3.58e9f, 4}, {8, 16}};
    const std::vector<int64_t> dates = {0, 86400000, 172800000, 259200000};
    const std::vector<std::string> symbols = {"A", "B"};
    const auto plot = signal_plot_to_json(signals, dates, symbols, 1, 2);
    ASSERT_TRUE(plot["sampled"].asBool());
    const auto json = signal_log_distribution_to_json(signals, 1, &dates, &symbols);
    expect_full_log_histogram(json, 6);
    EXPECT_DOUBLE_EQ(json["max"].asDouble(), static_cast<double>(3.58e9f));
    EXPECT_EQ(json["exampleSymbol"][199].asString(), "A");
    EXPECT_EQ(json["exampleDate"][199].asString(), "1970-01-03");
    EXPECT_DOUBLE_EQ(json["exampleValue"][199].asDouble(), static_cast<double>(3.58e9f));
    EXPECT_EQ(json["exampleDate"][0].asString(), "1970-01-02");
    for (Json::ArrayIndex i = 0; i < 200; ++i) {
        if (json["counts"][i].asUInt64() == 0) {
            EXPECT_TRUE(json["exampleSymbol"][i].isNull());
            EXPECT_TRUE(json["exampleDate"][i].isNull());
            EXPECT_TRUE(json["exampleValue"][i].isNull());
        } else {
            EXPECT_GE(json["exampleValue"][i].asDouble(), json["edges"][i].asDouble());
            EXPECT_LE(json["exampleValue"][i].asDouble(), json["edges"][i + 1].asDouble());
        }
    }
}

TEST(JsonSerializer, PairedLinearHistogramUsesTheSameFinitePopulationAndFullRange) {
    const float maximum = std::numeric_limits<float>::max();
    const float tiny = std::numeric_limits<float>::denorm_min();
    const float nan = std::numeric_limits<float>::quiet_NaN();
    const std::vector<std::vector<float>> signals = {{999}, {-maximum, -tiny, 0, tiny, maximum, nan}};
    const std::vector<int64_t> dates = {0, 86400000};
    const std::vector<std::string> symbols = {"A", "B", "C", "D", "E", "F"};
    const auto json = signal_log_distribution_to_json(signals, 1, &dates, &symbols);
    const auto& linear = json["linear"];
    expect_full_log_histogram(json, 5);
    expect_full_log_histogram(linear, 5);
    EXPECT_EQ(linear["scale"].asString(), "linear");
    EXPECT_FALSE(linear.isMember("linear"));
    EXPECT_DOUBLE_EQ(linear["min"].asDouble(), json["min"].asDouble());
    EXPECT_DOUBLE_EQ(linear["max"].asDouble(), json["max"].asDouble());
    EXPECT_EQ(linear["counts"][0].asUInt64(), 1u);
    EXPECT_EQ(linear["counts"][199].asUInt64(), 1u);
    EXPECT_EQ(linear["exampleSymbol"][199].asString(), "E");
    EXPECT_EQ(linear["exampleDate"][199].asString(), "1970-01-02");
    EXPECT_DOUBLE_EQ(linear["exampleValue"][199].asDouble(), static_cast<double>(maximum));
    const double span = linear["rangeHi"].asDouble() - linear["rangeLo"].asDouble();
    for (Json::ArrayIndex i = 0; i < 200; ++i) {
        const double width = linear["edges"][i + 1].asDouble() - linear["edges"][i].asDouble();
        EXPECT_NEAR(width / span, 1.0 / 200, 1e-14);
    }
}

TEST(JsonSerializer, PairedLinearHistogramPreservesTinyZerosConstantsAndEmptyData) {
    for (const auto& values : std::vector<std::vector<float>>{
            {0, 0, 0}, {1e-20f, 0, 1e-12f}, {3.58e9f, 3.58e9f},
            {-3.58e9f, -3.58e9f}, {std::numeric_limits<float>::max()}}) {
        const auto json = signal_log_distribution_to_json({values});
        expect_full_log_histogram(json["linear"], values.size());
        EXPECT_EQ(json["totalCount"].asUInt64(), json["linear"]["totalCount"].asUInt64());
    }
    const auto json = signal_log_distribution_to_json({});
    EXPECT_EQ(json["linear"]["scale"].asString(), "linear");
    EXPECT_EQ(json["linear"]["totalCount"].asUInt64(), 0u);
    EXPECT_TRUE(json["linear"]["counts"].empty());
    EXPECT_TRUE(json["linear"]["edges"].empty());
    EXPECT_FALSE(json["linear"].isMember("linear"));
}
