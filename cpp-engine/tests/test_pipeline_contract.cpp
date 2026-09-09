#include <gtest/gtest.h>

#include <cmath>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

#include <json/json.h>

#include "api/json_serializer.h"
#include "compiler/dag_builder.h"
#include "data/data_panel.h"
#include "engine/simulator.h"
#include "metrics/metrics_engine.h"

namespace {

DataPanel make_panel(unsigned T = 10, unsigned N = 3, bool same_price = false)
{
    DataPanel dp;
    dp.T = T;
    dp.N = N;

    const int64_t day_ms = 86400000LL;
    const int64_t t0 = 1704067200000LL;  // 2024-01-01 UTC
    dp.timestamps.resize(T);
    for (unsigned t = 0; t < T; ++t)
        dp.timestamps[t] = t0 + static_cast<int64_t>(t) * day_ms;

    dp.symbols.resize(N);
    for (unsigned n = 0; n < N; ++n)
        dp.symbols[n] = "SYM" + std::to_string(n);

    auto& open = dp.arrays["open"];
    auto& high = dp.arrays["high"];
    auto& low = dp.arrays["low"];
    auto& close = dp.arrays["close"];
    auto& volume = dp.arrays["volume"];
    open.resize(T * N);
    high.resize(T * N);
    low.resize(T * N);
    close.resize(T * N);
    volume.resize(T * N);

    for (unsigned t = 0; t < T; ++t) {
        for (unsigned n = 0; n < N; ++n) {
            double px = 100.0 + static_cast<double>(n) * 10.0;
            if (!same_price) {
                if (n == 0)
                    px += 1.5 * static_cast<double>(t);
                else if (n == 1)
                    px += 0.8 * static_cast<double>(t);
                else
                    px += std::sin(static_cast<double>(t) * 0.4) * 2.0 + 0.3 * static_cast<double>(t);
            }
            const size_t idx = static_cast<size_t>(t) * N + n;
            close[idx] = static_cast<float>(px);
            open[idx] = static_cast<float>(px * 1.001);
            high[idx] = static_cast<float>(px * 1.01);
            low[idx] = static_cast<float>(px * 0.99);
            volume[idx] = 1000.0f + static_cast<float>(10 * t + n);
        }
    }
    return dp;
}

size_t count_non_null_values(const Json::Value& signal_plot)
{
    if (!signal_plot.isObject() || !signal_plot.isMember("values"))
        return 0;

    size_t cnt = 0;
    const Json::Value& values = signal_plot["values"];
    for (Json::ArrayIndex i = 0; i < values.size(); ++i) {
        const Json::Value& row = values[i];
        for (Json::ArrayIndex j = 0; j < row.size(); ++j) {
            if (!row[j].isNull())
                ++cnt;
        }
    }
    return cnt;
}

}  // namespace

TEST(PipelineContract, MultiVariantXmlProducesPerAlphaSourceXml)
{
    auto dp = make_panel();
    const std::string xml = R"(<data expr="close" delay="1"/><rank/><tsmean days="2|3"/>)";

    auto alphas = compile_xml(xml, &dp);
    ASSERT_EQ(alphas.size(), 2u);

    EXPECT_NE(alphas[0].source_xml.find("days=\"2\""), std::string::npos);
    EXPECT_NE(alphas[1].source_xml.find("days=\"3\""), std::string::npos);
    EXPECT_EQ(alphas[0].source_xml.find('|'), std::string::npos);
    EXPECT_EQ(alphas[1].source_xml.find('|'), std::string::npos);
    EXPECT_NE(alphas[0].source_xml, alphas[1].source_xml);

    for (const auto& alpha : alphas) {
        ASSERT_FALSE(alpha.pipeline.empty());
        EXPECT_EQ(alpha.pipeline.back(), alpha.output);
    }
}

TEST(PipelineContract, SingleAlphaCollectsSignalsForDetailPlot)
{
    auto dp = make_panel(12, 3);
    const std::string xml = R"(<data expr="returns"/>)";
    auto alphas = compile_xml(xml, &dp);
    ASSERT_EQ(alphas.size(), 1u);
    ASSERT_FALSE(alphas[0].pipeline.empty());

    SimulatorConfig cfg;
    cfg.T = dp.T;
    cfg.N = dp.N;
    cfg.fee_rate = 0.0005;
    cfg.mode = 1;
    cfg.collect_signals = true;

    Simulator sim(dp, cfg);
    auto out = sim.run(alphas[0].pipeline);

    ASSERT_EQ(out.signals.size(), dp.T);
    ASSERT_EQ(out.signals[0].size(), dp.N);

    Json::Value signal_plot = signal_plot_to_json(out.signals, out.timestamps, dp.symbols);
    EXPECT_EQ(signal_plot["timestamps"].size(), static_cast<Json::ArrayIndex>(dp.T));
    EXPECT_EQ(signal_plot["symbols"].size(), static_cast<Json::ArrayIndex>(dp.N));
    EXPECT_GT(count_non_null_values(signal_plot), 0u);
}

TEST(PipelineContract, EmptyAndInvalidXmlFailFast)
{
    auto dp = make_panel();

    EXPECT_THROW(compile_xml("", &dp), std::runtime_error);
    EXPECT_THROW(compile_xml(R"(<data expr="INVALID"/>)", &dp), std::runtime_error);
}

TEST(PipelineContract, SamePriceReturnProducesZeroSharpe)
{
    auto dp = make_panel(8, 3, true);
    const std::string xml = R"(<data expr="returns"/>)";
    auto alphas = compile_xml(xml, &dp);
    ASSERT_EQ(alphas.size(), 1u);

    SimulatorConfig cfg;
    cfg.T = dp.T;
    cfg.N = dp.N;
    cfg.fee_rate = 0.0;
    cfg.mode = 1;
    cfg.collect_signals = false;

    Simulator sim(dp, cfg);
    auto out = sim.run(alphas[0].pipeline);

    const auto metrics = compute_full_metrics(
        out.timestamps, out.ret, out.turnover, out.max_weight,
        out.long_weight, out.short_weight, out.position_count,
        dp.timestamps[1], dp.timestamps[5], 0.0, 365);

    EXPECT_NEAR(metrics.is_summary.pnl, 0.0, 1e-12);
    EXPECT_NEAR(metrics.os_summary.pnl, 0.0, 1e-12);
    EXPECT_DOUBLE_EQ(metrics.is_summary.sharpe, 0.0);
    EXPECT_DOUBLE_EQ(metrics.os_summary.sharpe, 0.0);
}

TEST(PipelineContract, SingleDayLikeSplitDoesNotCrashAndKeepsIndicesValid)
{
    auto dp = make_panel(3, 2);
    const std::string xml = R"(<data expr="close"/><rank/>)";
    auto alphas = compile_xml(xml, &dp);
    ASSERT_EQ(alphas.size(), 1u);

    SimulatorConfig cfg;
    cfg.T = dp.T;
    cfg.N = dp.N;
    cfg.fee_rate = 0.0005;
    cfg.mode = 1;
    cfg.collect_signals = false;

    Simulator sim(dp, cfg);
    auto out = sim.run(alphas[0].pipeline);

    // IS starts on bar 1, OS starts on bar 2 -> very short windows.
    const auto metrics = compute_full_metrics(
        out.timestamps, out.ret, out.turnover, out.max_weight,
        out.long_weight, out.short_weight, out.position_count,
        dp.timestamps[1], dp.timestamps[2], 0.0005, 365);

    EXPECT_LE(metrics.is_start_index, metrics.os_start_index);
    EXPECT_LE(metrics.os_start_index, metrics.timeseries.timestamps.size());
}

TEST(PipelineContract, SignalPlotPreservesNullForWarmupNaN)
{
    auto dp = make_panel(8, 3);
    const std::string xml = R"(<data expr="close" delay="1"/>)";
    auto alphas = compile_xml(xml, &dp);
    ASSERT_EQ(alphas.size(), 1u);

    SimulatorConfig cfg;
    cfg.T = dp.T;
    cfg.N = dp.N;
    cfg.fee_rate = 0.0;
    cfg.mode = 1;
    cfg.collect_signals = true;

    Simulator sim(dp, cfg);
    auto out = sim.run(alphas[0].pipeline);

    Json::Value signal_plot = signal_plot_to_json(out.signals, out.timestamps, dp.symbols);
    ASSERT_TRUE(signal_plot["values"].isArray());
    ASSERT_EQ(signal_plot["values"].size(), static_cast<Json::ArrayIndex>(dp.T));

    // delay=1 warmup -> first row should be all null
    const Json::Value first_row = signal_plot["values"][0];
    ASSERT_EQ(first_row.size(), static_cast<Json::ArrayIndex>(dp.N));
    for (Json::ArrayIndex j = 0; j < first_row.size(); ++j)
        EXPECT_TRUE(first_row[j].isNull());

    // later rows should contain non-null values
    size_t non_null_after_warmup = 0;
    for (Json::ArrayIndex i = 1; i < signal_plot["values"].size(); ++i) {
        const Json::Value& row = signal_plot["values"][i];
        for (Json::ArrayIndex j = 0; j < row.size(); ++j) {
            if (!row[j].isNull())
                ++non_null_after_warmup;
        }
    }
    EXPECT_GT(non_null_after_warmup, 0u);
}

TEST(PipelineContract, VeryDeepPipelineCompilesAndRuns)
{
    auto dp = make_panel(64, 6);
    const std::string xml =
        R"(<data expr="close"/><tsmean days="5"/><tsmean days="10"/><zscore/><rank/>)";
    auto alphas = compile_xml(xml, &dp);
    ASSERT_EQ(alphas.size(), 1u);
    ASSERT_FALSE(alphas[0].pipeline.empty());

    SimulatorConfig cfg;
    cfg.T = dp.T;
    cfg.N = dp.N;
    cfg.fee_rate = 0.0005;
    cfg.mode = 1;
    cfg.collect_signals = true;

    Simulator sim(dp, cfg);
    auto out = sim.run(alphas[0].pipeline);
    ASSERT_EQ(out.signals.size(), dp.T);
    ASSERT_EQ(out.signals.back().size(), dp.N);

    size_t finite_count = 0;
    for (const auto& row : out.signals) {
        for (float v : row) {
            if (std::isfinite(v))
                ++finite_count;
        }
    }
    EXPECT_GT(finite_count, 0u);
}
