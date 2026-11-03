#include <gtest/gtest.h>
#include <cmath>
#include <limits>
#include <atomic>
#include <future>
#include <thread>
#include "meta.h"
#include "simulation_data.h"
#include "simulator_engine.h"
#include "modules/Data.h"
#include "modules/Rank.h"
#include "xml_compiler.h"

// Helper: build a small DataPanel for tests
static DataPanel make_test_panel(unsigned T, unsigned N) {
    DataPanel dp;
    dp.T = T;
    dp.N = N;
    dp.timestamps.resize(T);
    for (unsigned t = 0; t < T; ++t)
        dp.timestamps[t] = static_cast<int64_t>(t) * 86400000;
    dp.symbols.resize(N);
    for (unsigned i = 0; i < N; ++i)
        dp.symbols[i] = "SYM" + std::to_string(i);

    auto& close = dp.arrays["close"];
    close.resize(T * N);
    for (unsigned t = 0; t < T; ++t)
        for (unsigned i = 0; i < N; ++i)
            close[t * N + i] = 100.0f + static_cast<float>(t) + static_cast<float>(i) * 10.0f;

    return dp;
}

TEST(CombinedAlpha, NaNTolerantSignalSummation) {
    // 2 alphas, 3 timestamps, 2 symbols
    const float NaN = std::numeric_limits<float>::quiet_NaN();

    std::vector<std::vector<float>> alpha0 = {
        {1.0f, 2.0f},
        {NaN,  3.0f},
        {4.0f, NaN},
    };
    std::vector<std::vector<float>> alpha1 = {
        {0.5f, NaN},
        {1.0f, 1.0f},
        {NaN,  2.0f},
    };

    const unsigned T = 3, N = 2;
    std::vector<std::vector<float>> combined(T,
        std::vector<float>(N, std::numeric_limits<float>::quiet_NaN()));

    std::vector<std::vector<std::vector<float>>> all_signals = {alpha0, alpha1};
    for (size_t a = 0; a < all_signals.size(); ++a) {
        const auto& sigs = all_signals[a];
        for (unsigned di = 0; di < T; ++di) {
            for (unsigned ii = 0; ii < N; ++ii) {
                float s = sigs[di][ii];
                if (!std::isfinite(s)) continue;
                float& c = combined[di][ii];
                c = std::isfinite(c) ? c + s : s;
            }
        }
    }

    // [0][0]: 1.0 + 0.5 = 1.5
    EXPECT_FLOAT_EQ(combined[0][0], 1.5f);
    // [0][1]: 2.0 + NaN = 2.0
    EXPECT_FLOAT_EQ(combined[0][1], 2.0f);
    // [1][0]: NaN + 1.0 = 1.0
    EXPECT_FLOAT_EQ(combined[1][0], 1.0f);
    // [1][1]: 3.0 + 1.0 = 4.0
    EXPECT_FLOAT_EQ(combined[1][1], 4.0f);
    // [2][0]: 4.0 + NaN = 4.0
    EXPECT_FLOAT_EQ(combined[2][0], 4.0f);
    // [2][1]: NaN + 2.0 = 2.0
    EXPECT_FLOAT_EQ(combined[2][1], 2.0f);
}

TEST(CombinedAlpha, RunFromSignalsProducesValidOutput) {
    const unsigned T = 10, N = 3;
    auto dp = make_test_panel(T, N);

    // Fixed signals: symbol 0 positive, symbol 1 negative, symbol 2 zero
    std::vector<std::vector<float>> signals(T, std::vector<float>(N, 0.0f));
    for (unsigned di = 0; di < T; ++di) {
        signals[di][0] = 1.0f;
        signals[di][1] = -1.0f;
        signals[di][2] = 0.0f;
    }

    SimulatorConfig cfg;
    cfg.T = T;
    cfg.N = N;
    cfg.fee_rate = 0.0005;

    cfg.collect_signals = false;

    Simulator sim(dp, cfg);
    auto out = sim.run_from_signals(signals);

    EXPECT_EQ(out.ret.size(), T);
    EXPECT_EQ(out.turnover.size(), T);
    EXPECT_EQ(out.timestamps.size(), T);

    // Initial current-row positions pay one-way entry fees.
    EXPECT_DOUBLE_EQ(out.ret[0], -0.0005);

    // From di=2 onward, returns should be finite and non-zero
    for (unsigned di = 2; di < T; ++di) {
        EXPECT_TRUE(std::isfinite(out.ret[di]));
    }

    // The supplied signal executes on its own row.
    EXPECT_GT(out.turnover[0], 0.0);

    // Nonzero count: only 2 symbols have nonzero signal
    for (unsigned di = 0; di < T; ++di)
        EXPECT_EQ(out.position_count[di], 2);
}

TEST(CombinedAlpha, RunFromSignalsMatchesPipelineRun) {
    // Compile a pipeline, run it, collect signals, then run_from_signals
    // and verify they produce the same returns
    const unsigned T = 10, N = 3;
    auto dp = make_test_panel(T, N);

    MData data_mod;
    data_mod.column_name = "close";
    data_mod.panel = &dp;
    MRank rank_mod;
    rank_mod.pct = IntParameter(1);
    std::vector<Module*> pipeline = {&data_mod, &rank_mod};

    SimulatorConfig cfg;
    cfg.T = T;
    cfg.N = N;
    cfg.fee_rate = 0.0005;

    cfg.collect_signals = true;

    Simulator sim1(dp, cfg);
    auto out1 = sim1.run(pipeline);

    // Now run_from_signals with the collected signals
    cfg.collect_signals = false;
    Simulator sim2(dp, cfg);
    auto out2 = sim2.run_from_signals(out1.signals);

    ASSERT_EQ(out1.ret.size(), out2.ret.size());
    for (unsigned di = 0; di < T; ++di) {
        EXPECT_DOUBLE_EQ(out1.ret[di], out2.ret[di]) << "ret mismatch at di=" << di;
        EXPECT_DOUBLE_EQ(out1.turnover[di], out2.turnover[di]) << "tvr mismatch at di=" << di;
        EXPECT_EQ(out1.position_count[di], out2.position_count[di]) << "position count mismatch at di=" << di;
        EXPECT_DOUBLE_EQ(out1.max_weight[di], out2.max_weight[di]) << "max_weight mismatch at di=" << di;
        EXPECT_EQ(out1.position_count[di], out2.position_count[di]) << "nz mismatch at di=" << di;
    }
}

TEST(CombinedAlpha, CombinedMetricsAreFinite) {
    const unsigned T = 20, N = 3;
    auto dp = make_test_panel(T, N);

    // 2 alphas with different signals
    std::vector<std::vector<float>> sig0(T, std::vector<float>(N, 0.0f));
    std::vector<std::vector<float>> sig1(T, std::vector<float>(N, 0.0f));
    for (unsigned di = 0; di < T; ++di) {
        sig0[di][0] = 1.0f;  sig0[di][1] = -0.5f; sig0[di][2] = 0.0f;
        sig1[di][0] = -0.5f; sig1[di][1] = 1.0f;  sig1[di][2] = 0.5f;
    }

    // NaN-tolerant summation
    std::vector<std::vector<float>> combined(T,
        std::vector<float>(N, std::numeric_limits<float>::quiet_NaN()));
    std::vector<std::vector<std::vector<float>>> all_signals = {sig0, sig1};
    for (size_t a = 0; a < all_signals.size(); ++a) {
        const auto& sigs = all_signals[a];
        for (unsigned di = 0; di < T; ++di) {
            for (unsigned ii = 0; ii < N; ++ii) {
                float s = sigs[di][ii];
                if (!std::isfinite(s)) continue;
                float& c = combined[di][ii];
                c = std::isfinite(c) ? c + s : s;
            }
        }
    }

    SimulatorConfig cfg;
    cfg.T = T;  cfg.N = N;
    cfg.fee_rate = 0.0005;

    cfg.collect_signals = false;

    Simulator sim(dp, cfg);
    auto out = sim.run_from_signals(combined);

    int64_t is_ms = dp.timestamps[5];
    int64_t os_ms = dp.timestamps[15];

    auto metrics = compute_full_metrics(
        out.timestamps, out.ret, out.gross_ret, out.turnover, out.max_weight,
        out.long_weight, out.short_weight,
        out.position_count,
        is_ms, os_ms, 365);

    // Combined metrics are gross: IS pnl is the pre-fee bar pnl sum.
    double gross_is = 0.0, net_is = 0.0;
    for (unsigned di = 5; di < 15; ++di) { gross_is += out.gross_ret[di]; net_is += out.ret[di]; }
    EXPECT_NEAR(metrics.is_summary.pnl, gross_is, 1e-12);
    EXPECT_NEAR(net_is, gross_is - [&] {
        double fee = 0.0;
        for (unsigned di = 5; di < 15; ++di) fee += out.turnover[di] * 0.0005;
        return fee;
    }(), 1e-12);
    // Over the whole span the entry fee separates gross from net.
    const auto& ts = metrics.timeseries;
    EXPECT_GT(ts.cum_pnl_gross.back() - ts.cum_pnl.back(), 1e-9);

    EXPECT_TRUE(std::isfinite(metrics.is_summary.sharpe));
    EXPECT_TRUE(std::isfinite(metrics.os_summary.sharpe));
    EXPECT_TRUE(std::isfinite(metrics.is_summary.pnl));
    EXPECT_TRUE(std::isfinite(metrics.os_summary.pnl));
    EXPECT_FALSE(metrics.yearly.empty());
    EXPECT_FALSE(metrics.timeseries.timestamps.empty());
}


TEST(CombinedAlpha, CombinedGivesEqualWeightDespiteRawSignalScale) {
    auto panel = make_test_panel(1, 4);
    const float alpha_a[] = {1, -1, 0, 0};
    const float alpha_b[] = {0, 0, 100, -100};
    std::vector<double> scratch, weights;
    OrderedSignalAccumulator normalized(panel, MissingClosePolicy::Error);
    normalized.add(0, 0, alpha_a, 4, scratch);
    normalized.add(1, 0, alpha_b, 4, scratch);
    const auto equal_signals = normalized.take_signals();
    normalize_signal_row(panel, 0, equal_signals[0].data(), weights, MissingClosePolicy::Error);
    EXPECT_DOUBLE_EQ(weights[0], 0.25);
    EXPECT_DOUBLE_EQ(weights[1], -0.25);
    EXPECT_DOUBLE_EQ(weights[2], 0.25);
    EXPECT_DOUBLE_EQ(weights[3], -0.25);
}

TEST(CombinedAlpha, OrderedAccumulationIsExactAcrossWorkerCounts) {
    auto panel = make_test_panel(9, 4);
    const std::vector<float> scales = {1e20f, 3.0f, -1e20f, 7.0f, 0.125f, -0.25f, 1e-8f};
    auto run = [&](unsigned threads) {
        OrderedSignalAccumulator accumulator(panel, MissingClosePolicy::Error);
        std::atomic<std::size_t> next{0};
        std::vector<std::thread> workers;
        for (unsigned worker = 0; worker < threads; ++worker) {
            workers.emplace_back([&] {
                std::vector<double> scratch;
                while (true) {
                    const auto alpha = next.fetch_add(1);
                    if (alpha >= scales.size()) return;
                    for (unsigned di = 0; di < panel.T; ++di) {
                        std::this_thread::yield();
                        const float row[] = {scales[alpha], -scales[alpha], 1.0f, -2.0f};
                        accumulator.add(alpha, di, row, 4, scratch);
                    }
                }
            });
        }
        for (auto& worker : workers) worker.join();
        return accumulator.take_signals();
    };
    const auto expected = run(1);
    EXPECT_EQ(run(2), expected);
    EXPECT_EQ(run(4), expected);
}

TEST(CombinedAlpha, FailedEvaluationWakesWaitingLaterAlpha) {
    auto panel = make_test_panel(1, 1);
    OrderedSignalAccumulator accumulator(panel, MissingClosePolicy::Error);
    auto later = std::async(std::launch::async, [&] {
        std::vector<double> scratch;
        const float row[] = {1};
        accumulator.add(1, 0, row, 1, scratch);
    });
    accumulator.cancel();
    EXPECT_THROW(later.get(), std::runtime_error);
}

TEST(CombinedAlpha, BlockAccumulationExactlyMatchesRowsAcrossWorkerCounts) {
    auto panel = make_test_panel(70, 4);
    panel.universe_mask.assign(panel.T * panel.N, 1);
    panel.universe_mask[4] = 0;
    auto run = [&](unsigned threads, bool blocks) {
        OrderedSignalAccumulator accumulator(panel, MissingClosePolicy::Error);
        std::atomic<size_t> next{0};
        std::vector<std::thread> workers;
        for (unsigned worker = 0; worker < threads; ++worker) workers.emplace_back([&] {
            std::vector<double> scratch;
            while (true) {
                const size_t alpha = next.fetch_add(1);
                if (alpha >= 48) return;
                for (unsigned start = 0; start < panel.T; start += 32) {
                    std::vector<float> buffer;
                    const unsigned end = std::min(panel.T, start + 32);
                    for (unsigned di = start; di < end; ++di) {
                        const float scale = (alpha % 3 == 0 ? 1e20f : alpha % 3 == 1 ? -1e20f : 0.125f);
                        const float row[] = {scale, -scale, static_cast<float>(alpha + di),
                            std::numeric_limits<float>::quiet_NaN()};
                        if (blocks) buffer.insert(buffer.end(), row, row + 4);
                        else accumulator.add(alpha, di, row, 4, scratch);
                    }
                    if (blocks) accumulator.add_block(alpha, start, buffer, 4, scratch);
                }
            }
        });
        for (auto& worker : workers) worker.join();
        return accumulator.take_signals();
    };
    const auto expected = run(1, false);
    for (unsigned threads : {1u, 4u, 20u}) {
        const auto actual = run(threads, true);
        for (unsigned di = 0; di < panel.T; ++di) for (unsigned ii = 0; ii < panel.N; ++ii)
            if (std::isnan(expected[di][ii])) EXPECT_TRUE(std::isnan(actual[di][ii]));
            else EXPECT_EQ(actual[di][ii], expected[di][ii]) << " threads=" << threads;
    }
}

TEST(CombinedAlpha, BlockCancellationWakesWaitingWorker) {
    auto panel = make_test_panel(3, 2);
    OrderedSignalAccumulator accumulator(panel, MissingClosePolicy::Error);
    auto later = std::async(std::launch::async, [&] {
        std::vector<double> scratch;
        std::vector<float> rows(6, 1);
        accumulator.add_block(1, 0, rows, 2, scratch);
    });
    accumulator.cancel();
    EXPECT_THROW(later.get(), std::runtime_error);
}

TEST(CombinedAlpha, SignalOnlyEvaluationPreservesWarmupAndEndBoundary) {
    auto panel = make_test_panel(12, 3);
    SimulatorConfig config;
    config.T = panel.T; config.N = panel.N;

    config.sim_start_index = 4; config.sim_end_index = 9;
    config.collect_signals = true;
    auto evaluate = [&](bool signal_only) {
        MData data; data.column_name = "close"; data.panel = &panel;
        MRank rank; rank.pct = IntParameter(1);
        std::vector<Module*> pipeline = {&data, &rank};
        Simulator simulator(panel, config);
        if (!signal_only) return simulator.run(pipeline).signals;
        std::vector<std::vector<float>> rows;
        simulator.run_signal_rows(pipeline, [&](unsigned di, const float* row, unsigned n) {
            EXPECT_EQ(di, rows.size()); rows.emplace_back(row, row + n);
        });
        return rows;
    };
    EXPECT_EQ(evaluate(true), evaluate(false));
    EXPECT_EQ(evaluate(true).size(), 9u);
}

TEST(CombinedAlpha, NormalizedContributionsUseTheSignalDateUniverse) {
    auto panel = make_test_panel(1, 3);
    panel.universe_mask = {1, 0, 1};
    OrderedSignalAccumulator accumulator(panel, MissingClosePolicy::Error);
    const float row[] = {1, 100, -1};
    std::vector<double> scratch;
    accumulator.add(0, 0, row, 3, scratch);
    const auto signals = accumulator.take_signals();
    EXPECT_EQ(signals[0], (std::vector<float>{0.5f, 0.0f, -0.5f}));
}

