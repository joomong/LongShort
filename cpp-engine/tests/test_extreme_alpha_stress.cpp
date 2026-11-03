#include <gtest/gtest.h>

#include <chrono>
#include <cmath>
#include <cstdint>
#include <sstream>
#include <string>
#include <vector>

#include "xml_compiler.h"
#include "simulation_data.h"
#include "simulator_engine.h"

namespace {

DataPanel make_panel(unsigned T = 2048, unsigned N = 8)
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
            const double base = 100.0 + static_cast<double>(n) * 0.7 + static_cast<double>(t) * 0.25;
            const double wobble = std::sin(static_cast<double>(t) * 0.17 + static_cast<double>(n) * 0.31);
            const double px = base + wobble;
            const size_t idx = static_cast<size_t>(t) * N + n;
            close[idx] = static_cast<float>(px);
            open[idx] = static_cast<float>(px * 1.001);
            high[idx] = static_cast<float>(px * 1.01);
            low[idx] = static_cast<float>(px * 0.99);
            volume[idx] = 1000.0f + static_cast<float>(5 * t + n);
        }
    }

    return dp;
}

std::string build_delay_options(unsigned n)
{
    std::ostringstream oss;
    for (unsigned i = 1; i <= n; ++i) {
        if (i > 1)
            oss << '|';
        oss << i;
    }
    return oss.str();
}

}  // namespace

TEST(ExtremeAlphaStress, CompileExpandsToTwoThousandAlphas)
{
    auto dp = make_panel(2048, 8);
    const unsigned kAlphaCount = 2000;
    const std::string xml = "<data expr=\"close\" delay=\"" + build_delay_options(kAlphaCount)
        + "\"/><rank/>";

    const auto t0 = std::chrono::steady_clock::now();
    auto alphas = compile_xml(xml, &dp);
    const auto t1 = std::chrono::steady_clock::now();
    const auto compile_ms =
        std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count();

    ASSERT_EQ(alphas.size(), kAlphaCount);
    for (unsigned i = 0; i < kAlphaCount; ++i) {
        EXPECT_EQ(alphas[i].source_xml.find('|'), std::string::npos);
        EXPECT_NE(
            alphas[i].source_xml.find("delay=\"" + std::to_string(i + 1) + "\""),
            std::string::npos);
        ASSERT_FALSE(alphas[i].pipeline.empty());
        EXPECT_EQ(alphas[i].pipeline.back(), alphas[i].output);
    }

    std::cout << "[stress] compile " << kAlphaCount << " alphas took "
              << compile_ms << " ms\n";
}

TEST(ExtremeAlphaStress, SimulateAndScoreTwoThousandAlphasWithoutCrash)
{
    auto dp = make_panel(2048, 8);
    const unsigned kAlphaCount = 2000;
    const std::string xml = "<data expr=\"close\" delay=\"" + build_delay_options(kAlphaCount)
        + "\"/><rank/>";
    auto alphas = compile_xml(xml, &dp);
    ASSERT_EQ(alphas.size(), kAlphaCount);

    SimulatorConfig cfg;
    cfg.T = dp.T;
    cfg.N = dp.N;
    cfg.fee_rate = 0.0005;

    cfg.collect_signals = false;

    const int64_t is_ms = dp.timestamps[1];
    const int64_t os_ms = dp.timestamps[dp.T / 2];

    const auto t0 = std::chrono::steady_clock::now();
    size_t good_batches = 0;
    double checksum = 0.0;
    for (auto& alpha : alphas) {
        Simulator sim(dp, cfg);
        auto out = sim.run(alpha.pipeline);
        const auto m = compute_full_metrics(
            out.timestamps, out.ret, out.gross_ret, out.turnover, out.max_weight,
            out.long_weight, out.short_weight, out.position_count,
            is_ms, os_ms, BARS_PER_YEAR);

        if (std::isfinite(m.is_summary.sharpe) && std::isfinite(m.os_summary.sharpe))
            ++good_batches;

        checksum += m.is_summary.pnl;
        checksum += m.os_summary.pnl;
        checksum += m.is_summary.sharpe;
        checksum += m.os_summary.sharpe;
    }
    const auto t1 = std::chrono::steady_clock::now();
    const auto run_ms = std::chrono::duration_cast<std::chrono::milliseconds>(t1 - t0).count();

    EXPECT_EQ(good_batches, kAlphaCount);
    EXPECT_TRUE(std::isfinite(checksum));
    EXPECT_NEAR(checksum, checksum, 0.0);  // NaN guard for clarity in failure output.

    std::cout << "[stress] simulate+metrics " << kAlphaCount << " alphas took "
              << run_ms << " ms, checksum=" << checksum << "\n";
}
