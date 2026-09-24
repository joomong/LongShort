#include <gtest/gtest.h>
#include <json/json.h>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <limits>
#include <string>
#include <vector>
#include "data/data_panel.h"
#include "engine/simulator.h"
#include "modules/Data.h"
#include "modules/Rank.h"
#include "modules/Delay.h"

// Simple test: run simulator with close -> rank pipeline
TEST(Simulator, BasicD0Run) {
    // Create test panel: 10 bars, 3 symbols
    DataPanel dp;
    dp.T = 10;
    dp.N = 3;
    dp.timestamps.resize(10);
    for (unsigned t = 0; t < 10; ++t) dp.timestamps[t] = (int64_t)t * 86400000;
    dp.symbols = {"A", "B", "C"};

    auto& close = dp.arrays["close"];
    close.resize(10 * 3);
    for (unsigned t = 0; t < 10; ++t) {
        close[t * 3 + 0] = 100.0f + (float)t;       // A: 100..109
        close[t * 3 + 1] = 200.0f + (float)t * 2;   // B: 200..218
        close[t * 3 + 2] = 150.0f - (float)t;       // C: 150..141
    }

    // Pipeline: close -> rank
    MData data_mod;
    data_mod.column_name = "close";
    data_mod.panel = &dp;
    MRank rank_mod;
    rank_mod.pct = IntParameter(1);

    std::vector<Module*> pipeline = {&data_mod, &rank_mod};

    SimulatorConfig cfg;
    cfg.T = dp.T;
    cfg.N = dp.N;
    cfg.fee_rate = 0.0005;
    cfg.mode = 0;  // D0

    Simulator sim(dp, cfg);
    SimulatorOutput out = sim.run(pipeline);

    // Basic checks
    EXPECT_EQ(out.ret.size(), 10u);
    EXPECT_EQ(out.turnover.size(), 10u);
    // First bar: no asset return, but turnover fee is charged (0 -> initial weights)
    EXPECT_LT(out.ret[0], 0.0);  // Negative from fee only
    EXPECT_EQ(out.timestamps.size(), 10u);

    // Turnover should be non-zero on first signal bar (di=0 creates positions)
    EXPECT_GT(out.turnover[0], 0.0);

    // All bars should have nonzero positions
    for (unsigned t = 0; t < 10; ++t)
        EXPECT_GT(out.position_count[t], 0);
}

TEST(Simulator, D1DelayedExecution) {
    DataPanel dp;
    dp.T = 5;
    dp.N = 2;
    dp.timestamps = {0, 86400000, 172800000, 259200000, 345600000};
    dp.symbols = {"A", "B"};

    auto& close = dp.arrays["close"];
    close = {100.0f, 200.0f, 110.0f, 210.0f, 120.0f, 220.0f, 130.0f, 230.0f, 140.0f, 240.0f};

    MData data_mod;
    data_mod.column_name = "close";
    data_mod.panel = &dp;
    MRank rank_mod;
    rank_mod.pct = IntParameter(1);
    std::vector<Module*> pipeline = {&data_mod, &rank_mod};

    // D1 mode: target = prev_target, execution delayed by 1 bar
    SimulatorConfig cfg;
    cfg.T = 5; cfg.N = 2; cfg.fee_rate = 0.0; cfg.mode = 1;

    Simulator sim(dp, cfg);
    SimulatorOutput out = sim.run(pipeline);

    // First 2 bars: no positions executed in D1 (prev_target is 0 at di=0)
    EXPECT_DOUBLE_EQ(out.ret[0], 0.0);
    // di=1: exec_w = prev_target (from di=0 signal), but prev_weights was 0
    // turnover should be > 0 at di=1
    EXPECT_GT(out.turnover[1], 0.0);
}


namespace {
DataPanel timing_panel() {
    DataPanel p;
    p.T = 5; p.N = 2; p.symbols = {"A", "B"};
    p.timestamps = {0, 86400000, 172800000, 259200000, 345600000};
    p.arrays["close"] = {100,100,110,90,121,81,110,90,100,100};
    return p;
}
}

TEST(Simulator, CloseToCloseTimingAndOneWayFeesMatchHandCalculation) {
    auto p = timing_panel();
    const std::vector<std::vector<float>> signals = {{1,-1},{1,-1},{-1,1},{-1,1},{0,0}};
    SimulatorConfig cfg{p.T,p.N,0.001,1,false,{}};
    auto d1 = Simulator(p,cfg).run_from_signals(signals);
    EXPECT_DOUBLE_EQ(d1.ret[0],0.0);
    EXPECT_NEAR(d1.ret[1],-0.001,1e-12);  // Enter at day 1 close.
    EXPECT_NEAR(d1.ret[2],0.10,1e-12);    // Day 0 signal earns day 2 holding return.
    const double down = 0.5*(110.0/121.0-1.0)-0.5*(90.0/81.0-1.0);
    EXPECT_NEAR(d1.ret[3],down-0.002,1e-12);  // Flip both 50% positions: turnover=2.
    EXPECT_NEAR(d1.turnover[3],2.0,1e-12);
    cfg.mode = 0;
    auto d0 = Simulator(p,cfg).run_from_signals(signals);
    EXPECT_NEAR(d0.ret[0],-0.001,1e-12);
    EXPECT_NEAR(d0.ret[1],0.10,1e-12);
    EXPECT_NEAR(d0.ret[2],0.10-0.002,1e-12);
}

// The error policy keeps the original fail-fast contract unchanged.
TEST(Simulator, InvalidHeldCloseIsAnErrorInBothExecutionPaths) {
    for (float bad : {std::numeric_limits<float>::quiet_NaN(),
                      std::numeric_limits<float>::infinity(),0.0f,-1.0f}) {
        auto p = timing_panel(); p.arrays["close"][4] = bad;
        for (int mode : {0,1}) {
            SimulatorConfig cfg{p.T,p.N,0.001,mode,false,{}};
            cfg.missing_close_policy = MissingClosePolicy::Error;
            MData data; data.column_name="close"; data.panel=&p;
            EXPECT_THROW(Simulator(p,cfg).run({&data}),std::runtime_error);
            const std::vector<std::vector<float>> signals(p.T,{1,-1});
            EXPECT_THROW(Simulator(p,cfg).run_from_signals(signals),std::runtime_error);
        }
    }
}

TEST(Simulator, MissingExecutionCloseCannotCreateAPosition) {
    auto p=timing_panel(); p.arrays["close"][2]=std::numeric_limits<float>::quiet_NaN();
    SimulatorConfig cfg{p.T,p.N,0.001,1,false,{}};
    cfg.missing_close_policy = MissingClosePolicy::Error;
    const std::vector<std::vector<float>> signals(p.T,{1,-1});
    EXPECT_THROW(Simulator(p,cfg).run_from_signals(signals),std::runtime_error);
}

TEST(Simulator, MissingUnheldAssetDoesNotPoisonReturns) {
    auto p=timing_panel();
    for (unsigned t=0;t<p.T;++t) p.arrays["close"][t*2]=std::numeric_limits<float>::infinity();
    SimulatorConfig cfg{p.T,p.N,0.0,1,false,{}};
    cfg.missing_close_policy = MissingClosePolicy::Error;
    const std::vector<std::vector<float>> signals(p.T,{1,-1});
    auto out=Simulator(p,cfg).run_from_signals(signals);
    for (double r:out.ret) EXPECT_TRUE(std::isfinite(r));
    EXPECT_NEAR(out.ret[2],0.1,1e-12);
}

TEST(Simulator, RejectsMismatchedSignalDimensions) {
    auto p=timing_panel();
    SimulatorConfig cfg{p.T,p.N,0.0,1,false,{}};
    EXPECT_THROW(Simulator(p,cfg).run_from_signals({{1,-1}}),std::invalid_argument);
    std::vector<std::vector<float>> signals(p.T,{1});
    EXPECT_THROW(Simulator(p,cfg).run_from_signals(signals),std::invalid_argument);
}

TEST(Simulator, RawSignalObserverAndCollectionKeepTheirExecutionPathContract) {
    auto panel = timing_panel();
    SimulatorConfig cfg{panel.T, panel.N, 0.001, 1, true, {}};
    std::vector<std::vector<float>> observed;
    cfg.signal_row_callback = [&](unsigned di, const float* row, unsigned size) {
        EXPECT_EQ(di, observed.size());
        EXPECT_EQ(size, panel.N);
        observed.emplace_back(row, row + size);
    };
    MData data;
    data.column_name = "close";
    data.panel = &panel;
    MDelay delay;
    delay.days = IntParameter(1);
    auto generated = Simulator(panel, cfg).run({&data, &delay});
    ASSERT_EQ(observed.size(), panel.T);
    ASSERT_EQ(generated.signals.size(), panel.T);
    for (unsigned di = 0; di < panel.T; ++di) {
        for (unsigned ii = 0; ii < panel.N; ++ii) {
            if (di == 0) {
                EXPECT_TRUE(std::isnan(observed[di][ii]));
                EXPECT_TRUE(std::isnan(generated.signals[di][ii]));
            } else {
                EXPECT_FLOAT_EQ(observed[di][ii], panel.at("close", di - 1, ii));
                EXPECT_FLOAT_EQ(generated.signals[di][ii], observed[di][ii]);
            }
        }
    }
    auto replay = Simulator(panel, cfg).run_from_signals(generated.signals);
    EXPECT_EQ(observed.size(), panel.T);  // Precomputed input is not observed a second time.
    EXPECT_TRUE(replay.signals.empty());
    EXPECT_EQ(replay.ret, generated.ret);
    EXPECT_EQ(replay.turnover, generated.turnover);
}

TEST(Simulator, UniverseExitSharesSignalDelayAndPreservesHoldingLossAndFees) {
    auto panel = timing_panel();
    panel.arrays["close"] = {100,100,100,100,90,100,81,100,72,100};
    panel.universe_mask = {1,1,1,1,0,1,0,1,0,1};
    const std::vector<std::vector<float>> signals(panel.T, {1,-1});
    panel.arrays["test_signal"] = {1,-1,1,-1,1,-1,1,-1,1,-1};
    const auto original_close = panel.arrays.at("close");
    for (int mode : {0,1}) {
        SimulatorConfig cfg{panel.T,panel.N,0.001,mode,false,{}};
        auto out = Simulator(panel,cfg).run_from_signals(signals);
        MData data; data.column_name = "test_signal"; data.panel = &panel;
        auto generated = Simulator(panel,cfg).run({&data});
        EXPECT_EQ(out.ret, generated.ret);
        EXPECT_EQ(out.turnover, generated.turnover);
        if (mode == 0) {
            EXPECT_NEAR(out.ret[2], -0.05-0.001, 1e-12);
            EXPECT_EQ(out.position_count[2], 1);
            EXPECT_DOUBLE_EQ(out.ret[3], 0.0);
        } else {
            EXPECT_NEAR(out.ret[2], -0.05, 1e-12);
            EXPECT_DOUBLE_EQ(out.turnover[2], 0.0);
            EXPECT_EQ(out.position_count[2], 2);
            EXPECT_NEAR(out.ret[3], -0.05-0.001, 1e-12);
            EXPECT_NEAR(out.turnover[3], 1.0, 1e-12);
            EXPECT_EQ(out.position_count[3], 1);
            EXPECT_DOUBLE_EQ(out.ret[4], 0.0);
        }
    }
    EXPECT_EQ(panel.arrays.at("close"), original_close);
}

TEST(Simulator, ExcludedHeldAssetWithMissingLiquidationCloseStillFails) {
    const float nan = std::numeric_limits<float>::quiet_NaN();
    for (int mode : {0,1}) {
        auto panel = timing_panel();
        panel.universe_mask = {1,1,1,1,0,1,0,1,0,1};
        panel.arrays["close"][(2+mode)*panel.N] = nan;
        SimulatorConfig cfg{panel.T,panel.N,0.001,mode,false,{}};
        cfg.missing_close_policy = MissingClosePolicy::Error;
        EXPECT_THROW(Simulator(panel,cfg).run_from_signals(
            std::vector<std::vector<float>>(panel.T,{1,-1})),std::runtime_error);
    }
}

TEST(Simulator, EmptyUniverseLiquidationDoesNotEraseLossOrFees) {
    auto panel = timing_panel();
    panel.arrays["close"] = {100,100,100,100,90,100,81,100,72,100};
    panel.universe_mask = {1,1,1,1,0,0,0,0,0,0};
    SimulatorConfig cfg{panel.T,panel.N,0.001,1,false,{}};
    auto out = Simulator(panel,cfg).run_from_signals(
        std::vector<std::vector<float>>(panel.T,{1,-1}));
    EXPECT_NEAR(out.ret[3], -0.05-0.001, 1e-12);
    EXPECT_NEAR(out.turnover[3], 1.0, 1e-12);
    EXPECT_EQ(out.position_count[3], 0);
}

TEST(Simulator, StartDateWarmsModulesButStartsWithNoHistoricalHoldings) {
    auto panel = timing_panel();
    panel.arrays["close"] = {100,100,120,100,60,100,90,100,90,100};
    MData data; data.column_name = "close"; data.panel = &panel;
    MDelay delay; delay.days = IntParameter(1);
    SimulatorConfig cfg{panel.T,panel.N,0.001,1,true,{}};
    cfg.sim_start_index = 2;
    auto out = Simulator(panel,cfg).run({&data,&delay});
    for (unsigned di = 0; di < 2; ++di) {
        EXPECT_DOUBLE_EQ(out.ret[di], 0.0);
        EXPECT_DOUBLE_EQ(out.turnover[di], 0.0);
        EXPECT_EQ(out.position_count[di], 0);
    }
    // Delay module is warm: day 1 raw values are 100/100, executed at day 2 close.
    EXPECT_FLOAT_EQ(out.signals[1][0], 100.0f);
    EXPECT_NEAR(out.ret[2], -0.001, 1e-12); // no pre-start 120 -> 60 loss
    EXPECT_NEAR(out.turnover[2], 1.0, 1e-12);
    // First holding period is 60 -> 90 at 50% weight. New weights are 120/220,100/220.
    EXPECT_NEAR(out.ret[3], 0.25-(1.0/11.0)*0.001, 1e-12);
    auto replay = Simulator(panel,cfg).run_from_signals(out.signals);
    EXPECT_EQ(out.ret,replay.ret);
    EXPECT_EQ(out.turnover,replay.turnover);
}

TEST(Simulator, StartDateDoesNotValueInvalidUnheldWarmupPrices) {
    auto panel = timing_panel();
    panel.arrays["close"][0] = std::numeric_limits<float>::quiet_NaN();
    panel.arrays["close"][2] = std::numeric_limits<float>::quiet_NaN();
    SimulatorConfig cfg{panel.T,panel.N,0.001,0,false,{}};
    cfg.sim_start_index = 2;
    auto out = Simulator(panel,cfg).run_from_signals(
        std::vector<std::vector<float>>(panel.T,{1,-1}));
    EXPECT_DOUBLE_EQ(out.ret[0],0.0);
    EXPECT_DOUBLE_EQ(out.ret[1],0.0);
    EXPECT_NEAR(out.ret[2],-0.001,1e-12);
}

TEST(Simulator, ValidatesStartAndUniverseDimensions) {
    auto panel = timing_panel();
    SimulatorConfig cfg{panel.T,panel.N,0.0,1,false,{}};
    cfg.sim_start_index = panel.T+1;
    EXPECT_THROW(Simulator(panel,cfg),std::invalid_argument);
    cfg.sim_start_index = panel.T;
    auto out = Simulator(panel,cfg).run_from_signals(
        std::vector<std::vector<float>>(panel.T,{1,-1}));
    for (double value : out.ret) EXPECT_DOUBLE_EQ(value,0.0);
    panel.universe_mask = {1,0};
    EXPECT_THROW(Simulator(panel,cfg),std::invalid_argument);
}


// ── Missing-close and settlement accounting (docs/missing_data_validation_2026-09-11.md) ──

namespace {
const float kMissing = std::numeric_limits<float>::quiet_NaN();
constexpr double kFee = 0.0005;

// Flat 100 prices; each case writes only the quotes it needs to move or remove.
DataPanel flat_panel(unsigned T, unsigned N) {
    DataPanel panel;
    panel.T = T;
    panel.N = N;
    panel.timestamps.resize(T);
    for (unsigned t = 0; t < T; ++t) panel.timestamps[t] = static_cast<int64_t>(t) * 86400000;
    panel.symbols.resize(N);
    for (unsigned ii = 0; ii < N; ++ii) panel.symbols[ii] = "S" + std::to_string(ii);
    panel.arrays["close"].assign(static_cast<size_t>(T) * N, 100.0f);
    return panel;
}

struct AccountingLedger {
    std::vector<unsigned> bar;
    std::vector<char> trading;
    std::vector<std::vector<double>> previous_weights, desired, weights, trades;
    std::vector<std::vector<double>> marks_before, marks_after, delisting_returns, settled_gross;
    std::vector<std::vector<uint8_t>> retired;
    std::vector<double> gross, fee, net, turnover;
    std::vector<MissingDataDiagnostics> diagnostics;

    void record(const AccountingRowView& row) {
        bar.push_back(row.bar);
        trading.push_back(row.trading_enabled ? 1 : 0);
        previous_weights.push_back(row.previous_weights);
        desired.push_back(row.desired_weights);
        weights.push_back(row.weights);
        trades.push_back(row.market_trades);
        marks_before.push_back(row.marks_before);
        marks_after.push_back(row.marks_after);
        delisting_returns.push_back(row.delisting_returns);
        settled_gross.push_back(row.settled_gross);
        retired.push_back(row.retired);
        gross.push_back(row.gross_pnl);
        fee.push_back(row.fee);
        net.push_back(row.net_pnl);
        turnover.push_back(row.turnover);
        diagnostics.push_back(row.diagnostics);
    }
};

SimulatorConfig carry_config(const DataPanel& panel, int mode, AccountingLedger& ledger) {
    SimulatorConfig config;
    config.T = panel.T;
    config.N = panel.N;
    config.fee_rate = kFee;
    config.mode = mode;
    config.accounting_row_callback = [&ledger](const AccountingRowView& row) { ledger.record(row); };
    return config;
}

// Raw signals that make plan[t] the intent executed on bar t under either mode.
// D0 executes the current bar's signal, D1 the previous one; plan[0] must be flat.
std::vector<std::vector<float>> plan_signals(
    const std::vector<std::vector<float>>& plan, int mode) {
    if (mode == 0) return plan;
    std::vector<std::vector<float>> signals(plan.size(),
        std::vector<float>(plan.front().size(), 0.0f));
    for (std::size_t t = 1; t < plan.size(); ++t) signals[t - 1] = plan[t];
    return signals;
}
}  // namespace

TEST(MissingDataAccounting, FrozenHoldingEarnsTheFullGapMoveOnItsRecoveryQuote) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 2);
        panel.at("close", 2, 0) = kMissing;
        panel.at("close", 3, 0) = kMissing;
        panel.at("close", 4, 0) = 80.0f;
        panel.at("close", 5, 0) = 80.0f;
        const std::vector<std::vector<float>> plan = {
            {0, 0}, {1, 0}, {1, 0}, {1, 0}, {0, 1}, {0, 1}};
        AccountingLedger ledger;
        auto out = Simulator(panel, carry_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        for (unsigned di : {2u, 3u}) {
            EXPECT_NEAR(ledger.weights[di][0], 1.0, 1e-12) << "mode=" << mode << " bar=" << di;
            EXPECT_NEAR(ledger.trades[di][0], 0.0, 1e-12);
            EXPECT_NEAR(out.turnover[di], 0.0, 1e-12);
            EXPECT_NEAR(ledger.fee[di], 0.0, 1e-12);
            EXPECT_NEAR(ledger.gross[di], 0.0, 1e-12);
            EXPECT_NEAR(out.ret[di], 0.0, 1e-12);
            EXPECT_NEAR(ledger.marks_after[di][0], 100.0, 1e-12);
            EXPECT_EQ(ledger.diagnostics[di].missing_held_count, 1u);
            EXPECT_NEAR(ledger.diagnostics[di].frozen_gross, 1.0, 1e-12);
        }
        EXPECT_NEAR(ledger.gross[4], -0.2, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(out.turnover[4], 2.0, 1e-12);
        EXPECT_NEAR(out.ret[4], -0.201, 1e-12);
        EXPECT_NEAR(ledger.weights[4][0], 0.0, 1e-12);
        EXPECT_NEAR(ledger.weights[4][1], 1.0, 1e-12);
        EXPECT_NEAR(ledger.marks_after[4][0], 80.0, 1e-12);
        EXPECT_EQ(ledger.diagnostics[4].resumed_holding_count, 1u);
        EXPECT_EQ(out.position_count[4], 1);
    }
}

TEST(MissingDataAccounting, BlockedEntryNeverChargesTheGapMoveToAnUnheldAccount) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(5, 2);
        panel.at("close", 2, 0) = kMissing;
        panel.at("close", 3, 0) = 80.0f;
        panel.at("close", 4, 0) = 80.0f;
        const std::vector<std::vector<float>> plan = {
            {0, 0}, {0, 1}, {1, 0}, {1, 0}, {1, 0}};
        AccountingLedger ledger;
        auto out = Simulator(panel, carry_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        EXPECT_NEAR(ledger.weights[2][0], 0.0, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(ledger.trades[2][0], 0.0, 1e-12);
        EXPECT_EQ(ledger.diagnostics[2].missing_entry_count, 1u);
        EXPECT_EQ(ledger.diagnostics[2].blocked_order_count, 1u);
        EXPECT_NEAR(ledger.diagnostics[2].blocked_trade_weight, 1.0, 1e-12);
        EXPECT_NEAR(ledger.diagnostics[2].frozen_gross, 0.0, 1e-12);
        // Only the sold holding trades; the missing name stays flat.
        EXPECT_NEAR(out.turnover[2], 1.0, 1e-12);
        EXPECT_NEAR(out.ret[2], -kFee, 1e-12);
        // The fresh 80 quote is a first mark for this account, not a -20% loss.
        EXPECT_NEAR(ledger.gross[3], 0.0, 1e-12);
        EXPECT_NEAR(ledger.weights[3][0], 1.0, 1e-12);
        EXPECT_NEAR(out.turnover[3], 1.0, 1e-12);
        EXPECT_NEAR(ledger.marks_before[3][0], 100.0, 1e-12);
        EXPECT_NEAR(ledger.marks_after[3][0], 80.0, 1e-12);
    }
}

TEST(MissingDataAccounting, RecoveryExecutesTheLatestTargetInsteadOfQueuedOrders) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 2);
        panel.at("close", 2, 0) = kMissing;
        panel.at("close", 3, 0) = kMissing;
        const std::vector<std::vector<float>> plan = {
            {0, 0}, {1, 0}, {0, 0}, {-1, 0}, {1, 0}, {1, 0}};
        AccountingLedger ledger;
        auto out = Simulator(panel, carry_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        EXPECT_NEAR(ledger.diagnostics[2].blocked_trade_weight, 1.0, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(ledger.diagnostics[3].blocked_trade_weight, 2.0, 1e-12);
        // The queued exit and reversal are dropped; the live target is +1 again.
        EXPECT_NEAR(ledger.weights[4][0], 1.0, 1e-12);
        EXPECT_NEAR(ledger.trades[4][0], 0.0, 1e-12);
        EXPECT_NEAR(out.turnover[4], 0.0, 1e-12);
        EXPECT_NEAR(ledger.fee[4], 0.0, 1e-12);
        EXPECT_NEAR(ledger.gross[4], 0.0, 1e-12);
    }
}

TEST(MissingDataAccounting, FrozenGrossOnlyShrinksTheRemainingTargets) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 3);
        panel.at("close", 2, 0) = kMissing;
        panel.at("close", 3, 0) = kMissing;
        panel.at("close", 4, 0) = 80.0f;
        panel.at("close", 5, 0) = 80.0f;
        const std::vector<std::vector<float>> plan = {
            {0, 0, 0}, {6, 2, 2}, {75, 25, 0}, {0, 50, -50}, {0, 50, -50}, {0, 50, -50}};
        AccountingLedger ledger;
        auto out = Simulator(panel, carry_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        EXPECT_NEAR(ledger.weights[1][0], 0.6, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(ledger.weights[1][1], 0.2, 1e-12);
        EXPECT_NEAR(ledger.weights[1][2], 0.2, 1e-12);
        // Budget 0.4 is not exceeded: the tradable targets execute unscaled.
        EXPECT_NEAR(ledger.weights[2][0], 0.6, 1e-12);
        EXPECT_NEAR(ledger.weights[2][1], 0.25, 1e-12);
        EXPECT_NEAR(ledger.weights[2][2], 0.0, 1e-12);
        EXPECT_NEAR(out.turnover[2], 0.25, 1e-12);
        EXPECT_NEAR(ledger.fee[2], 0.000125, 1e-12);
        // Gross 1.0 over budget 0.4 shrinks proportionally; nothing is refilled.
        EXPECT_NEAR(ledger.weights[3][0], 0.6, 1e-12);
        EXPECT_NEAR(ledger.weights[3][1], 0.2, 1e-12);
        EXPECT_NEAR(ledger.weights[3][2], -0.2, 1e-12);
        EXPECT_NEAR(ledger.diagnostics[3].frozen_gross, 0.6, 1e-12);
        // Recovery values the whole 100 -> 80 move, then rebalances in full.
        EXPECT_NEAR(ledger.gross[4], -0.12, 1e-12);
        EXPECT_NEAR(ledger.weights[4][0], 0.0, 1e-12);
        EXPECT_NEAR(ledger.weights[4][1], 0.5, 1e-12);
        EXPECT_NEAR(ledger.weights[4][2], -0.5, 1e-12);
        EXPECT_NEAR(out.turnover[4], 1.2, 1e-12);
        EXPECT_NEAR(ledger.fee[4], 0.0006, 1e-12);
        EXPECT_NEAR(out.ret[4], -0.1206, 1e-12);
    }
}

TEST(MissingDataAccounting, DefaultZeroSettlementRetiresTheBookWithoutMarketCost) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(4, 2);
        // The end date opens the settlement; the first silent bar closes it, so the
        // event bar has to stop quoting for the settlement to happen there.
        panel.at("close", 2, 0) = kMissing;
        panel.settlement_bar = {2, -1};
        panel.settlement_return = {kMissing, kMissing};
        AccountingLedger ledger;
        auto config = carry_config(panel, mode, ledger);
        auto out = Simulator(panel, config)
                       .run_from_signals(std::vector<std::vector<float>>(panel.T, {1, 0}));

        EXPECT_NEAR(ledger.previous_weights[2][0], 1.0, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(ledger.gross[2], 0.0, 1e-12);
        EXPECT_NEAR(ledger.weights[2][0], 0.0, 1e-12);
        EXPECT_NEAR(ledger.trades[2][0], 0.0, 1e-12);
        EXPECT_NEAR(out.turnover[2], 0.0, 1e-12);
        EXPECT_NEAR(ledger.fee[2], 0.0, 1e-12);
        EXPECT_EQ(ledger.retired[2][0], 1);
        EXPECT_EQ(ledger.diagnostics[2].delisting_count, 1u);
        EXPECT_EQ(ledger.diagnostics[2].default_delisting_count, 1u);
        EXPECT_EQ(ledger.diagnostics[2].stale_settlement_count, 0u);
        EXPECT_NEAR(ledger.diagnostics[2].settled_gross, 1.0, 1e-12);
        EXPECT_NEAR(ledger.settled_gross[2][0], 1.0, 1e-12);
        EXPECT_NEAR(ledger.delisting_returns[2][0], 0.0, 1e-12);
        EXPECT_EQ(ledger.diagnostics[2].pending_delisting_entry_count, mode == 1 ? 1u : 0u);
        // A later quote does not re-enter a retired security.
        EXPECT_NEAR(ledger.weights[3][0], 0.0, 1e-12);
        EXPECT_NEAR(out.turnover[3], 0.0, 1e-12);
        EXPECT_EQ(out.position_count[3], 0);
    }
}

TEST(MissingDataAccounting, MinusOneSettlementSignsWithTheHeldSide) {
    for (int mode : {0, 1}) {
        for (float side : {1.0f, -1.0f}) {
            auto panel = flat_panel(4, 2);
            panel.at("close", 2, 0) = kMissing;   // the end date needs a silent bar
            panel.settlement_bar = {2, -1};
            AccountingLedger ledger;
            auto config = carry_config(panel, mode, ledger);
            config.default_delisting_return = -1.0;
            auto out = Simulator(panel, config).run_from_signals(
                std::vector<std::vector<float>>(panel.T, {side, 0}));

            EXPECT_NEAR(ledger.previous_weights[2][0], side, 1e-12) << "mode=" << mode;
            EXPECT_NEAR(ledger.gross[2], -static_cast<double>(side), 1e-12);
            EXPECT_NEAR(out.ret[2], -static_cast<double>(side), 1e-12);
            EXPECT_NEAR(ledger.diagnostics[2].delisting_pnl, -static_cast<double>(side), 1e-12);
            EXPECT_NEAR(ledger.diagnostics[2].default_delisting_gross, 1.0, 1e-12);
            EXPECT_NEAR(out.turnover[2], 0.0, 1e-12);
            EXPECT_NEAR(ledger.marks_after[2][0], 0.0, 1e-12);
        }
    }
}

TEST(MissingDataAccounting, ExplicitSettlementReturnOverridesEitherDefault) {
    for (int mode : {0, 1}) {
        for (double fallback : {0.0, -1.0}) {
            auto panel = flat_panel(4, 2);
            panel.at("close", 2, 0) = kMissing;   // the end date needs a silent bar
            panel.settlement_bar = {2, -1};
            panel.settlement_return = {-0.25f, kMissing};
            AccountingLedger ledger;
            auto config = carry_config(panel, mode, ledger);
            config.default_delisting_return = fallback;
            auto out = Simulator(panel, config)
                           .run_from_signals(std::vector<std::vector<float>>(panel.T, {1, 0}));

            EXPECT_NEAR(ledger.gross[2], -0.25, 1e-12) << "mode=" << mode;
            EXPECT_NEAR(out.ret[2], -0.25, 1e-12);
            EXPECT_NEAR(ledger.delisting_returns[2][0], -0.25, 1e-12);
            EXPECT_EQ(ledger.diagnostics[2].delisting_count, 1u);
            EXPECT_EQ(ledger.diagnostics[2].default_delisting_count, 0u);
            EXPECT_NEAR(ledger.diagnostics[2].default_delisting_gross, 0.0, 1e-12);
            EXPECT_NEAR(ledger.marks_after[2][0], 75.0, 1e-12);
        }
    }
}

// A security that keeps printing past its end date is valued normally until it
// goes silent; the settlement then replaces that bar's quote move, not an
// earlier one. Modelled on the observed one-day post-end-date quote.
TEST(MissingDataAccounting, AQuoteOnTheEndDateDefersTheSettlementToTheSilentBar) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(5, 2);
        panel.at("close", 2, 0) = 120.0f;      // one quote after the end date
        panel.at("close", 3, 0) = kMissing;
        panel.at("close", 4, 0) = kMissing;
        panel.settlement_bar = {2, -1};
        panel.settlement_return = {-0.5f, kMissing};
        AccountingLedger ledger;
        auto out = Simulator(panel, carry_config(panel, mode, ledger))
                       .run_from_signals(std::vector<std::vector<float>>(panel.T, {1, 0}));

        // Bar 2 is the end date but still quotes: normal valuation, no settlement.
        EXPECT_NEAR(ledger.marks_before[2][0], 100.0, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(ledger.gross[2], 0.2, 1e-12);
        EXPECT_NEAR(ledger.marks_after[2][0], 120.0, 1e-12);
        EXPECT_EQ(ledger.retired[2][0], 0);
        EXPECT_EQ(out.missing_data[2].post_end_quote_bars, 1u);
        EXPECT_EQ(out.missing_data[2].delisting_count, 0u);
        // Bar 3 goes silent: settle against the post-end-date mark of 120.
        EXPECT_NEAR(ledger.gross[3], -0.5, 1e-12);
        EXPECT_NEAR(out.ret[3], -0.5, 1e-12);
        EXPECT_NEAR(ledger.marks_after[3][0], 60.0, 1e-12);
        EXPECT_NEAR(out.turnover[3], 0.0, 1e-12);
        EXPECT_EQ(ledger.retired[3][0], 1);
        EXPECT_EQ(out.missing_data[3].delisting_count, 1u);
        EXPECT_EQ(out.missing_data[3].post_end_quote_bars, 0u);
        EXPECT_EQ(out.missing_data.back().pending_settlement_count, 0u);
    }
}

TEST(MissingDataAccounting, AnEndDateThatNeverStopsQuotingStaysUnsettled) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(5, 2);
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 120.0f;
        panel.settlement_bar = {2, -1};
        panel.settlement_return = {-0.5f, kMissing};
        AccountingLedger ledger;
        auto out = Simulator(panel, carry_config(panel, mode, ledger))
                       .run_from_signals(std::vector<std::vector<float>>(panel.T, {1, 0}));

        unsigned post_end = 0;
        for (unsigned di = 0; di < panel.T; ++di) {
            EXPECT_EQ(ledger.retired[di][0], 0) << "mode=" << mode << " bar=" << di;
            EXPECT_EQ(out.missing_data[di].delisting_count, 0u);
            post_end += out.missing_data[di].post_end_quote_bars;
        }
        EXPECT_EQ(post_end, 3u);                       // bars 2, 3 and 4 still quote
        EXPECT_NEAR(ledger.gross[2], 0.2, 1e-12);      // valued, not settled
        EXPECT_NEAR(ledger.weights.back()[0], 1.0, 1e-12);
        EXPECT_EQ(out.missing_data.back().pending_settlement_count, 1u);
    }
}

TEST(MissingDataAccounting, StaleQuotesSettleOnlyWithoutEventsAndAfterAnObservedMark) {
    auto panel = flat_panel(6, 2);
    for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = kMissing;
    AccountingLedger ledger;
    auto config = carry_config(panel, 0, ledger);
    config.stale_quote_settle_bars = 2;
    config.default_delisting_return = -1.0;
    auto out = Simulator(panel, config)
                   .run_from_signals(std::vector<std::vector<float>>(panel.T, {1, 0}));

    EXPECT_EQ(ledger.diagnostics[2].missing_held_count, 1u);  // first missing bar only freezes
    EXPECT_EQ(ledger.diagnostics[2].delisting_count, 0u);
    EXPECT_EQ(ledger.diagnostics[3].delisting_count, 1u);
    EXPECT_EQ(ledger.diagnostics[3].stale_settlement_count, 1u);
    EXPECT_EQ(ledger.diagnostics[3].default_delisting_count, 1u);
    EXPECT_NEAR(ledger.gross[3], -1.0, 1e-12);
    EXPECT_NEAR(out.turnover[3], 0.0, 1e-12);
    EXPECT_EQ(ledger.retired[3][0], 1);
    EXPECT_NEAR(ledger.diagnostics[4].frozen_gross, 0.0, 1e-12);

    // A security that never printed a quote is not a delisting.
    auto unlisted = flat_panel(6, 2);
    for (unsigned di = 0; di < unlisted.T; ++di) unlisted.at("close", di, 0) = kMissing;
    AccountingLedger never_quoted;
    auto unlisted_config = carry_config(unlisted, 0, never_quoted);
    unlisted_config.stale_quote_settle_bars = 2;
    Simulator(unlisted, unlisted_config)
        .run_from_signals(std::vector<std::vector<float>>(unlisted.T, {1, 1}));
    for (const auto& row : never_quoted.retired) EXPECT_EQ(row[0], 0);

    // An event in the panel keeps the stale rule out of the way.
    auto scheduled = flat_panel(6, 2);
    for (unsigned di = 2; di < scheduled.T; ++di) scheduled.at("close", di, 0) = kMissing;
    scheduled.settlement_bar = {5, -1};
    AccountingLedger event_ledger;
    auto scheduled_config = carry_config(scheduled, 0, event_ledger);
    scheduled_config.stale_quote_settle_bars = 2;
    Simulator(scheduled, scheduled_config)
        .run_from_signals(std::vector<std::vector<float>>(scheduled.T, {1, 0}));
    EXPECT_EQ(event_ledger.retired[4][0], 0);
    EXPECT_EQ(event_ledger.retired[5][0], 1);
    EXPECT_EQ(event_ledger.diagnostics[5].stale_settlement_count, 0u);
}

TEST(MissingDataAccounting, WarmupPricesAndSettlementsMakeNoHistoricalPnl) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(5, 2);
        panel.at("close", 1, 0) = 120.0f;
        panel.at("close", 2, 0) = 60.0f;
        panel.at("close", 3, 0) = 60.0f;
        panel.at("close", 4, 0) = 60.0f;
        panel.at("close", 1, 1) = kMissing;  // the end date's bar stops quoting
        panel.settlement_bar = {-1, 1};   // settles inside warmup
        AccountingLedger ledger;
        auto config = carry_config(panel, mode, ledger);
        config.sim_start_index = 2;
        config.default_delisting_return = -1.0;
        auto out = Simulator(panel, config)
                       .run_from_signals(std::vector<std::vector<float>>(panel.T, {1, 1}));

        for (unsigned di : {0u, 1u}) {
            EXPECT_EQ(ledger.trading[di], 0) << "mode=" << mode << " bar=" << di;
            EXPECT_DOUBLE_EQ(out.ret[di], 0.0);
            EXPECT_DOUBLE_EQ(out.turnover[di], 0.0);
            EXPECT_EQ(out.position_count[di], 0);
            EXPECT_EQ(out.missing_data[di].delisting_count, 0u);
        }
        EXPECT_EQ(ledger.retired[1][1], 1);          // the state transition still happened
        EXPECT_EQ(ledger.trading[2], 1);
        EXPECT_NEAR(ledger.marks_before[2][0], 120.0, 1e-12);
        EXPECT_NEAR(ledger.gross[2], 0.0, 1e-12);    // no pre-start 120 -> 60 loss
        EXPECT_NEAR(out.ret[2], -kFee, 1e-12);
        EXPECT_NEAR(ledger.weights[2][0], 1.0, 1e-12);
        EXPECT_NEAR(ledger.weights[2][1], 0.0, 1e-12);
    }
}

TEST(MissingDataAccounting, FirstExecutionFollowsTheModeIntentAndNeedsAStartQuote) {
    auto panel = flat_panel(5, 2);
    const std::vector<std::vector<float>> signals = {{1, 0}, {0, 1}, {1, 0}, {1, 0}, {1, 0}};
    for (int mode : {0, 1}) {
        AccountingLedger ledger;
        auto config = carry_config(panel, mode, ledger);
        config.sim_start_index = 2;
        auto out = Simulator(panel, config).run_from_signals(signals);
        // D0 executes the bar-2 intent, D1 the bar-1 intent.
        EXPECT_NEAR(ledger.weights[2][0], mode == 0 ? 1.0 : 0.0, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(ledger.weights[2][1], mode == 0 ? 0.0 : 1.0, 1e-12);
        EXPECT_NEAR(out.turnover[2], 1.0, 1e-12);
    }
    // Without a quote on the first trading bar neither mode executes.
    auto stalled = flat_panel(5, 2);
    stalled.at("close", 2, 0) = kMissing;
    const std::vector<std::vector<float>> one_sided(stalled.T, {1, 0});
    for (int mode : {0, 1}) {
        AccountingLedger ledger;
        auto config = carry_config(stalled, mode, ledger);
        config.sim_start_index = 2;
        auto out = Simulator(stalled, config).run_from_signals(one_sided);
        EXPECT_NEAR(ledger.weights[2][0], 0.0, 1e-12) << "mode=" << mode;
        EXPECT_DOUBLE_EQ(out.turnover[2], 0.0);
        EXPECT_DOUBLE_EQ(out.ret[2], 0.0);
        EXPECT_EQ(ledger.diagnostics[2].missing_entry_count, 1u);
    }
}

TEST(MissingDataAccounting, CompleteQuotesKeepTheExistingFixedBookResultBitForBit) {
    auto panel = timing_panel();
    const std::vector<std::vector<float>> signals = {{1,-1},{1,-1},{-1,1},{-1,1},{0,0}};
    for (int mode : {0, 1}) {
        SimulatorConfig error_config{panel.T, panel.N, 0.001, mode, false, {}};
        error_config.missing_close_policy = MissingClosePolicy::Error;
        const auto expected = Simulator(panel, error_config).run_from_signals(signals);

        SimulatorConfig carry{panel.T, panel.N, 0.001, mode, false, {}};
        carry.missing_close_policy = MissingClosePolicy::Carry;
        const auto actual = Simulator(panel, carry).run_from_signals(signals);
        EXPECT_EQ(actual.ret, expected.ret) << "mode=" << mode;
        EXPECT_EQ(actual.turnover, expected.turnover);
        EXPECT_EQ(actual.max_weight, expected.max_weight);
        EXPECT_EQ(actual.long_weight, expected.long_weight);
        EXPECT_EQ(actual.short_weight, expected.short_weight);
        EXPECT_EQ(actual.position_count, expected.position_count);

        // Observing the ledger must not change any produced number.
        AccountingLedger ledger;
        auto observed_config = carry;
        observed_config.accounting_row_callback =
            [&ledger](const AccountingRowView& row) { ledger.record(row); };
        const auto observed = Simulator(panel, observed_config).run_from_signals(signals);
        EXPECT_EQ(observed.ret, expected.ret);
        EXPECT_EQ(observed.turnover, expected.turnover);
        ASSERT_EQ(ledger.bar.size(), panel.T);
        for (unsigned di = 0; di < panel.T; ++di) {
            EXPECT_EQ(ledger.bar[di], di);
            EXPECT_NEAR(ledger.net[di], expected.ret[di], 1e-12);
            EXPECT_NEAR(ledger.turnover[di], expected.turnover[di], 1e-12);
            EXPECT_EQ(observed.missing_data[di].missing_held_count, 0u);
            EXPECT_EQ(observed.missing_data[di].delisting_count, 0u);
            EXPECT_DOUBLE_EQ(observed.missing_data[di].frozen_gross, 0.0);
        }
    }
}

TEST(MissingDataAccounting, SettlementEventsStillApplyUnderTheErrorPolicy) {
    auto panel = flat_panel(4, 2);
    panel.at("close", 2, 0) = kMissing;   // the end date needs a silent bar
    panel.settlement_bar = {2, -1};
    AccountingLedger ledger;
    auto config = carry_config(panel, 0, ledger);
    config.missing_close_policy = MissingClosePolicy::Error;
    config.default_delisting_return = -1.0;
    auto out = Simulator(panel, config)
                   .run_from_signals(std::vector<std::vector<float>>(panel.T, {1, 0}));
    EXPECT_NEAR(ledger.gross[2], -1.0, 1e-12);
    EXPECT_EQ(ledger.retired[2][0], 1);
    EXPECT_NEAR(out.turnover[2], 0.0, 1e-12);
    // The error policy still refuses to value a held position without a quote.
    auto broken = flat_panel(4, 2);
    broken.at("close", 2, 0) = kMissing;
    SimulatorConfig strict{broken.T, broken.N, kFee, 0, false, {}};
    strict.missing_close_policy = MissingClosePolicy::Error;
    EXPECT_THROW(Simulator(broken, strict).run_from_signals(
        std::vector<std::vector<float>>(broken.T, {1, 0})), std::runtime_error);
}

TEST(MissingDataAccounting, RejectsSettlementVectorsThatDoNotMatchThePanel) {
    auto panel = flat_panel(4, 2);
    SimulatorConfig config{panel.T, panel.N, kFee, 1, false, {}};
    panel.settlement_bar = {2};
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.settlement_bar = {2, -1};
    panel.settlement_return = {0.0f};
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.settlement_return = {0.0f, kMissing};
    EXPECT_NO_THROW(Simulator(panel, config));
    config.default_delisting_return = std::numeric_limits<double>::quiet_NaN();
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
}


TEST(MissingDataAccounting, StaleSettlementReleasesHoldingsButNeverRetiresAnUnheldName) {
    for (int mode : {0, 1}) {
        // Quoted, then silent for four bars, then quoted again at 80.
        auto panel = flat_panel(8, 2);
        for (unsigned di = 2; di < 6; ++di) panel.at("close", di, 0) = kMissing;
        panel.at("close", 6, 0) = 80.0f;
        panel.at("close", 7, 0) = 80.0f;

        // Unheld through the silence, wanted again once the quote resumes.
        const std::vector<std::vector<float>> unheld_plan = {
            {0, 0}, {0, 1}, {0, 1}, {0, 1}, {0, 1}, {0, 1}, {1, 0}, {1, 0}};
        AccountingLedger unheld;
        auto unheld_config = carry_config(panel, mode, unheld);
        unheld_config.stale_quote_settle_bars = 2;
        unheld_config.default_delisting_return = -1.0;
        auto out = Simulator(panel, unheld_config)
                       .run_from_signals(plan_signals(unheld_plan, mode));
        for (unsigned di = 0; di < panel.T; ++di) {
            EXPECT_EQ(unheld.retired[di][0], 0) << "mode=" << mode << " bar=" << di;
            EXPECT_EQ(out.missing_data[di].delisting_count, 0u) << "bar=" << di;
            EXPECT_EQ(out.missing_data[di].stale_settlement_count, 0u) << "bar=" << di;
            EXPECT_NEAR(out.missing_data[di].frozen_gross, 0.0, 1e-12) << "bar=" << di;
        }
        // Silence never charged the unheld account, and the resumed quote enters.
        EXPECT_NEAR(unheld.gross[6], 0.0, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(unheld.weights[6][0], 1.0, 1e-12);
        EXPECT_NEAR(out.turnover[6], 2.0, 1e-12);
        EXPECT_NEAR(unheld.marks_after[6][0], 80.0, 1e-12);
        EXPECT_EQ(out.position_count[6], 1);

        // The same silence on a held weight still settles on the second bar.
        const std::vector<std::vector<float>> held_plan(panel.T, {1, 0});
        AccountingLedger held;
        auto held_config = carry_config(panel, mode, held);
        held_config.stale_quote_settle_bars = 2;
        held_config.default_delisting_return = -1.0;
        auto settled = Simulator(panel, held_config)
                           .run_from_signals(plan_signals(held_plan, mode));
        EXPECT_EQ(held.retired[2][0], 0) << "mode=" << mode;
        EXPECT_EQ(settled.missing_data[2].missing_held_count, 1u);
        EXPECT_EQ(held.retired[3][0], 1);
        EXPECT_EQ(settled.missing_data[3].delisting_count, 1u);
        EXPECT_EQ(settled.missing_data[3].stale_settlement_count, 1u);
        EXPECT_NEAR(held.gross[3], -1.0, 1e-12);
        EXPECT_NEAR(settled.turnover[3], 0.0, 1e-12);
        // Retired stays retired even after the quote resumes at bar 6.
        EXPECT_EQ(held.retired[6][0], 1);
        EXPECT_NEAR(held.weights[6][0], 0.0, 1e-12);
    }
}


// ── Pre-change bitwise regression fixture ──────────────────────────────────
// This block is byte-identical to the generator compiled against the HEAD
// 2e77af1 engine that produced tests/golden/missing_close_bitwise.json.
// Every value comes from integer arithmetic plus one exact divide, so no
// compiler float contraction can move it. Editing it invalidates the golden.
namespace bitwise_fixture {

inline uint32_t draw(uint32_t& state) {
    state = state * 1664525u + 1013904223u;
    return state >> 8;
}

inline std::string digest(const void* bytes, std::size_t length) {
    const auto* data = static_cast<const unsigned char*>(bytes);
    uint64_t hash = 1469598103934665603ull;
    for (std::size_t i = 0; i < length; ++i) {
        hash ^= data[i];
        hash *= 1099511628211ull;
    }
    char text[19];
    std::snprintf(text, sizeof(text), "0x%016llx", static_cast<unsigned long long>(hash));
    return text;
}

// Complete quotes and a churning universe: exactly the input whose result must
// not move. No missing closes and no settlement events.
inline DataPanel panel() {
    DataPanel p;
    p.T = 96;
    p.N = 14;
    p.timestamps.resize(p.T);
    for (unsigned t = 0; t < p.T; ++t)
        p.timestamps[t] = static_cast<int64_t>(t) * 86400000;
    p.symbols.resize(p.N);
    for (unsigned ii = 0; ii < p.N; ++ii)
        p.symbols[ii] = "B" + std::to_string(ii);
    auto& close = p.arrays["close"];
    close.resize(static_cast<std::size_t>(p.T) * p.N);
    p.universe_mask.resize(close.size());
    uint32_t state = 20260911u;
    for (unsigned t = 0; t < p.T; ++t) {
        for (unsigned ii = 0; ii < p.N; ++ii) {
            const std::size_t at = static_cast<std::size_t>(t) * p.N + ii;
            close[at] = static_cast<float>(20000u + draw(state) % 900000u) / 1000.0f;
            p.universe_mask[at] = ((t / 5 + ii * 3) % 7 == 0) ? 0 : 1;
        }
    }
    return p;
}

inline std::vector<std::vector<float>> signals(unsigned variant, const DataPanel& p) {
    std::vector<std::vector<float>> rows(p.T, std::vector<float>(p.N, 0.0f));
    uint32_t state = 7717u + variant * 101u;
    for (unsigned t = 0; t < p.T; ++t) {
        for (unsigned ii = 0; ii < p.N; ++ii) {
            const uint32_t value = draw(state);
            if (variant == 1 && value % 11u == 0u)
                rows[t][ii] = std::numeric_limits<float>::quiet_NaN();
            else if (variant == 2 && (t + ii) % 4u == 0u)
                rows[t][ii] = 0.0f;
            else
                rows[t][ii] =
                    static_cast<float>(static_cast<int32_t>(value % 2001u) - 1000) / 1000.0f;
        }
    }
    return rows;
}

inline std::string panel_digest(const DataPanel& p) {
    const auto& close = p.arrays.at("close");
    std::string combined = digest(close.data(), close.size() * sizeof(float));
    combined += digest(p.universe_mask.data(), p.universe_mask.size());
    return digest(combined.data(), combined.size());
}

inline std::string signal_digest(const std::vector<std::vector<float>>& rows) {
    std::string combined;
    for (const auto& row : rows)
        combined += digest(row.data(), row.size() * sizeof(float));
    return digest(combined.data(), combined.size());
}

struct Case { unsigned variant; int mode; unsigned sim_start; };
inline std::vector<Case> cases() {
    return {{0, 0, 0}, {0, 1, 0}, {1, 0, 3}, {1, 1, 3}, {2, 0, 6}, {2, 1, 6}};
}

inline std::string row_text(double ret, double turnover) {
    char text[64];
    std::snprintf(text, sizeof(text), "%a %a", ret, turnover);
    return text;
}
}  // namespace bitwise_fixture

namespace {
std::filesystem::path find_golden(const std::string& name) {
    namespace fs = std::filesystem;
    const std::vector<fs::path> candidates = {
        fs::path(__FILE__).parent_path() / "golden" / name,
        fs::current_path() / "tests" / "golden" / name,
        fs::current_path() / "golden" / name,
        fs::current_path() / ".." / "tests" / "golden" / name,
    };
    for (const auto& candidate : candidates)
        if (fs::exists(candidate)) return candidate;
    return {};
}
}  // namespace

// The checked-in dump comes from the engine at HEAD 2e77af1, before any
// missing-close work. Complete quotes must still produce those exact doubles.
TEST(MissingDataAccounting, ReproducesThePreChangeEngineDumpBitForBit) {
    const std::filesystem::path golden = find_golden("missing_close_bitwise.json");
    ASSERT_FALSE(golden.empty()) << "tests/golden/missing_close_bitwise.json not found";
    std::ifstream input(golden);
    ASSERT_TRUE(input.good()) << golden;
    Json::Value expected;
    Json::CharReaderBuilder builder;
    std::string errors;
    ASSERT_TRUE(Json::parseFromStream(builder, input, &expected, &errors)) << errors;

    const DataPanel panel = bitwise_fixture::panel();
    ASSERT_EQ(bitwise_fixture::panel_digest(panel), expected["panelDigest"].asString())
        << "the fixture generator moved; the golden dump no longer describes this input";
    const auto cases = bitwise_fixture::cases();
    ASSERT_EQ(expected["cases"].size(), cases.size());
    for (Json::ArrayIndex c = 0; c < expected["cases"].size(); ++c) {
        const Json::Value& recorded = expected["cases"][c];
        const auto& plan = cases[c];
        ASSERT_EQ(recorded["variant"].asUInt(), plan.variant);
        ASSERT_EQ(recorded["mode"].asInt(), plan.mode);
        ASSERT_EQ(recorded["simStart"].asUInt(), plan.sim_start);
        const auto signals = bitwise_fixture::signals(plan.variant, panel);
        ASSERT_EQ(bitwise_fixture::signal_digest(signals), recorded["signalDigest"].asString());
        ASSERT_EQ(recorded["rows"].size(), panel.T);
        // Complete quotes leave no room for the policies to disagree.
        for (MissingClosePolicy policy : {MissingClosePolicy::Carry, MissingClosePolicy::Error}) {
            SimulatorConfig config;
            config.T = panel.T;
            config.N = panel.N;
            config.fee_rate = 0.0005;
            config.mode = plan.mode;
            config.sim_start_index = plan.sim_start;
            config.missing_close_policy = policy;
            const auto out = Simulator(panel, config).run_from_signals(signals);
            const char* label = policy == MissingClosePolicy::Carry ? "carry" : "error";
            for (unsigned t = 0; t < panel.T; ++t)
                ASSERT_EQ(bitwise_fixture::row_text(out.ret[t], out.turnover[t]),
                          recorded["rows"][t].asString())
                    << "case=" << c << " policy=" << label << " bar=" << t;
        }
    }
}

TEST(MissingDataAccounting, StaleSettlementUsesAnExplicitReturnWhenTheEndDateIsNull) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 2);
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = kMissing;
        panel.settlement_bar = {-1, -1};            // no end date for either name
        panel.settlement_return = {-0.4f, kMissing};
        // The panel stores the return as a float, so that is the contract value.
        const double explicit_return = static_cast<double>(-0.4f);
        AccountingLedger ledger;
        auto config = carry_config(panel, mode, ledger);
        config.stale_quote_settle_bars = 2;
        config.default_delisting_return = -1.0;     // must not be used
        auto out = Simulator(panel, config)
                       .run_from_signals(std::vector<std::vector<float>>(panel.T, {1, 0}));

        EXPECT_NEAR(ledger.previous_weights[3][0], 1.0, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(ledger.gross[3], explicit_return, 1e-12);
        EXPECT_NEAR(out.ret[3], explicit_return, 1e-12);
        EXPECT_NEAR(ledger.delisting_returns[3][0], explicit_return, 1e-12);
        EXPECT_NEAR(out.missing_data[3].delisting_pnl, explicit_return, 1e-12);
        EXPECT_NEAR(explicit_return, -0.4, 1e-7);   // and it is the -0.4 the file asked for
        EXPECT_EQ(out.missing_data[3].delisting_count, 1u);
        EXPECT_EQ(out.missing_data[3].stale_settlement_count, 1u);
        EXPECT_EQ(out.missing_data[3].default_delisting_count, 0u);
        EXPECT_NEAR(out.missing_data[3].default_delisting_gross, 0.0, 1e-12);
        EXPECT_EQ(out.missing_data[3].retirement_count, 1u);
        EXPECT_NEAR(ledger.marks_after[3][0], 100.0 * (1.0 + explicit_return), 1e-12);
        EXPECT_NEAR(out.turnover[3], 0.0, 1e-12);
    }
}

TEST(MissingDataAccounting, RetirementCountSeesUnheldAndWarmupSettlements) {
    auto panel = flat_panel(5, 3);
    panel.at("close", 1, 0) = kMissing;   // each end date needs a silent bar
    panel.at("close", 3, 1) = kMissing;
    panel.settlement_bar = {1, 3, -1};  // one inside warmup, one on an unheld name
    AccountingLedger ledger;
    auto config = carry_config(panel, 0, ledger);
    config.sim_start_index = 2;
    auto out = Simulator(panel, config)
                   .run_from_signals(std::vector<std::vector<float>>(panel.T, {1, 0, 1}));

    EXPECT_EQ(out.missing_data[1].retirement_count, 1u);   // warmup retirement is still visible
    EXPECT_EQ(out.missing_data[1].delisting_count, 0u);    // but no holding was settled
    EXPECT_EQ(ledger.retired[1][0], 1);
    EXPECT_EQ(out.missing_data[3].retirement_count, 1u);   // unheld name retires at bar 3
    EXPECT_EQ(out.missing_data[3].delisting_count, 0u);
    EXPECT_EQ(ledger.retired[3][1], 1);
    EXPECT_NEAR(out.ret[3], 0.0, 1e-12);
    unsigned retirements = 0, delistings = 0;
    for (const auto& bar : out.missing_data) {
        retirements += bar.retirement_count;
        delistings += bar.delisting_count;
    }
    EXPECT_EQ(retirements, 2u);
    EXPECT_EQ(delistings, 0u);
}

TEST(MissingDataAccounting, TwoSimultaneousFreezesShareOneShrunkBudget) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 4);
        for (unsigned ii : {0u, 1u})
            for (unsigned di : {2u, 3u}) panel.at("close", di, ii) = kMissing;
        panel.at("close", 4, 0) = 80.0f;
        panel.at("close", 5, 0) = 80.0f;
        panel.at("close", 4, 1) = 50.0f;
        panel.at("close", 5, 1) = 50.0f;
        const std::vector<std::vector<float>> plan = {
            {0, 0, 0, 0}, {4, 2, 2, 2}, {0, 0, 75, 25}, {0, 0, 50, -50},
            {0, 0, 50, -50}, {0, 0, 50, -50}};
        AccountingLedger ledger;
        auto out = Simulator(panel, carry_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        EXPECT_NEAR(ledger.weights[1][0], 0.4, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(ledger.weights[1][1], 0.2, 1e-12);
        // Both frozen weights count once toward the same 0.4 budget.
        EXPECT_EQ(out.missing_data[2].missing_held_count, 2u);
        EXPECT_NEAR(out.missing_data[2].frozen_gross, 0.6, 1e-12);
        EXPECT_NEAR(ledger.weights[2][0], 0.4, 1e-12);
        EXPECT_NEAR(ledger.weights[2][1], 0.2, 1e-12);
        EXPECT_NEAR(ledger.weights[2][2], 0.3, 1e-12);
        EXPECT_NEAR(ledger.weights[2][3], 0.1, 1e-12);
        EXPECT_NEAR(out.turnover[2], 0.2, 1e-12);
        EXPECT_NEAR(ledger.fee[2], 0.0001, 1e-12);
        EXPECT_NEAR(ledger.weights[3][2], 0.2, 1e-12);
        EXPECT_NEAR(ledger.weights[3][3], -0.2, 1e-12);
        EXPECT_NEAR(out.turnover[3], 0.4, 1e-12);
        // Both recoveries book their full gap move on the same bar.
        EXPECT_NEAR(ledger.gross[4], -0.18, 1e-12);
        EXPECT_EQ(out.missing_data[4].resumed_holding_count, 2u);
        EXPECT_NEAR(out.turnover[4], 1.2, 1e-12);
        EXPECT_NEAR(out.ret[4], -0.1806, 1e-12);
        for (unsigned di = 0; di < panel.T; ++di) {
            double gross = 0.0;
            for (double weight : ledger.weights[di]) gross += std::fabs(weight);
            EXPECT_LE(gross, 1.0 + 1e-12) << "mode=" << mode << " bar=" << di;
        }
    }
}

TEST(MissingDataAccounting, AFullyFrozenBookLeavesNoBudgetAndFlattensTheRest) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(5, 3);
        for (unsigned ii : {0u, 1u}) panel.at("close", 2, ii) = kMissing;
        panel.at("close", 3, 1) = kMissing;   // only A recovers on bar 3
        const std::vector<std::vector<float>> plan = {
            {0, 0, 0}, {1, 1, 0}, {0, 0, 1}, {0, 0, 1}, {0, 0, 1}};
        AccountingLedger ledger;
        auto out = Simulator(panel, carry_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        EXPECT_NEAR(ledger.weights[1][0], 0.5, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(ledger.weights[1][1], 0.5, 1e-12);
        // Budget max(0, 1-1) = 0: the quoted name is flattened, never inflated.
        EXPECT_NEAR(out.missing_data[2].frozen_gross, 1.0, 1e-12);
        EXPECT_NEAR(ledger.desired[2][2], 1.0, 1e-12);
        EXPECT_NEAR(ledger.weights[2][2], 0.0, 1e-12);
        EXPECT_NEAR(ledger.weights[2][0], 0.5, 1e-12);
        EXPECT_NEAR(ledger.weights[2][1], 0.5, 1e-12);
        EXPECT_DOUBLE_EQ(out.turnover[2], 0.0);
        EXPECT_EQ(out.position_count[2], 2);
        // A recovers: half the budget returns and C takes exactly that half.
        EXPECT_NEAR(out.missing_data[3].frozen_gross, 0.5, 1e-12);
        EXPECT_NEAR(ledger.weights[3][0], 0.0, 1e-12);
        EXPECT_NEAR(ledger.weights[3][1], 0.5, 1e-12);
        EXPECT_NEAR(ledger.weights[3][2], 0.5, 1e-12);
        EXPECT_NEAR(out.turnover[3], 1.0, 1e-12);
    }
}

TEST(MissingDataAccounting, UniverseExitDuringAGapFreezesTheHoldingPermanently) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 2);
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = kMissing;
        panel.universe_mask.assign(static_cast<size_t>(panel.T) * panel.N, 1);
        for (unsigned di = 2; di < panel.T; ++di)
            panel.universe_mask[static_cast<size_t>(di) * panel.N] = 0;   // A leaves the universe
        const std::vector<std::vector<float>> plan(panel.T, {1, 1});
        AccountingLedger ledger;
        auto out = Simulator(panel, carry_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        // The old engine treated this as a fatal liquidation; carry freezes it.
        for (unsigned di = 3; di < panel.T; ++di) {
            EXPECT_NEAR(ledger.weights[di][0], 0.5, 1e-12) << "mode=" << mode << " bar=" << di;
            EXPECT_NEAR(ledger.weights[di][1], 0.5, 1e-12);
            EXPECT_NEAR(ledger.trades[di][0], 0.0, 1e-12);
            EXPECT_DOUBLE_EQ(out.turnover[di], 0.0);
            EXPECT_DOUBLE_EQ(out.ret[di], 0.0);
            EXPECT_EQ(out.missing_data[di].missing_held_count, 1u);
            EXPECT_NEAR(out.missing_data[di].frozen_gross, 0.5, 1e-12);
            EXPECT_EQ(ledger.retired[di][0], 0);      // an exit is not a retirement
            EXPECT_EQ(out.position_count[di], 2);
        }
        EXPECT_NEAR(out.missing_data.back().frozen_gross, 0.5, 1e-12);
        // The same panel under the error policy is still a hard failure.
        SimulatorConfig strict{panel.T, panel.N, kFee, mode, false, {}};
        strict.missing_close_policy = MissingClosePolicy::Error;
        EXPECT_THROW(Simulator(panel, strict).run_from_signals(plan_signals(plan, mode)),
                     std::runtime_error);
    }
}

TEST(MissingDataAccounting, RejectsASettlementReturnOutsideATotalReturn) {
    auto panel = flat_panel(4, 2);
    SimulatorConfig config{panel.T, panel.N, kFee, 1, false, {}};
    for (double accepted : {-1.0, -0.25, 0.0, 1.0}) {
        config.default_delisting_return = accepted;
        EXPECT_NO_THROW(Simulator(panel, config)) << accepted;
    }
    for (double rejected : {-1.0000001, -5.0, 3.0, std::numeric_limits<double>::infinity(),
                            std::numeric_limits<double>::quiet_NaN()}) {
        config.default_delisting_return = rejected;
        EXPECT_THROW(Simulator(panel, config), std::invalid_argument) << rejected;
    }
}

// Carry freezes a quote the venue never printed. Any other unusable number is
// corrupt data and must stop the run under either policy.
TEST(MissingDataAccounting, CarryFreezesOnlyNaNAndRejectsEveryOtherUnusablePrice) {
    const std::vector<float> unusable = {0.0f, -1.0f,
                                         std::numeric_limits<float>::infinity(),
                                         -std::numeric_limits<float>::infinity()};
    for (int mode : {0, 1}) {
        auto missing = flat_panel(5, 2);
        missing.at("close", 2, 0) = kMissing;
        AccountingLedger frozen;
        auto out = Simulator(missing, carry_config(missing, mode, frozen))
                       .run_from_signals(std::vector<std::vector<float>>(missing.T, {1, 0}));
        EXPECT_EQ(out.missing_data[2].missing_held_count, 1u) << "mode=" << mode;
        EXPECT_NEAR(frozen.weights[2][0], 1.0, 1e-12);

        for (float price : unusable) {
            auto held = flat_panel(5, 2);
            held.at("close", 2, 0) = price;
            const std::vector<std::vector<float>> long_only(held.T, {1, 0});
            for (MissingClosePolicy policy :
                     {MissingClosePolicy::Carry, MissingClosePolicy::Error}) {
                SimulatorConfig config{held.T, held.N, kFee, mode, false, {}};
                config.missing_close_policy = policy;
                EXPECT_THROW(Simulator(held, config).run_from_signals(long_only),
                             std::runtime_error)
                    << "mode=" << mode << " price=" << price;
            }
            // Never held, but carry still normalizes the name into the intent.
            auto unheld = flat_panel(5, 2);
            for (unsigned di = 0; di < unheld.T; ++di) unheld.at("close", di, 0) = price;
            SimulatorConfig carry{unheld.T, unheld.N, kFee, mode, false, {}};
            carry.missing_close_policy = MissingClosePolicy::Carry;
            EXPECT_THROW(Simulator(unheld, carry).run_from_signals(
                             std::vector<std::vector<float>>(unheld.T, {1, 1})),
                         std::runtime_error) << "mode=" << mode << " price=" << price;
        }
    }
    // The message names the corrupt price, the bar and the asset.
    auto panel = flat_panel(5, 2);
    panel.at("close", 2, 0) = 0.0f;
    SimulatorConfig config{panel.T, panel.N, kFee, 0, false, {}};
    config.missing_close_policy = MissingClosePolicy::Carry;
    try {
        Simulator(panel, config).run_from_signals(
            std::vector<std::vector<float>>(panel.T, {1, 0}));
        ADD_FAILURE() << "a zero close must not be carried";
    } catch (const std::runtime_error& error) {
        EXPECT_EQ(std::string(error.what()),
                  "Invalid close price (not NaN) at bar 2, asset 0");
    }
}

// ── Successor events: a merger or rename carries the holding forward ───────

namespace {
// Old name A settles into successor B. A quotes through bar 2 and goes silent
// on bar 3, which is where the event closes.
DataPanel successor_panel() {
    auto panel = flat_panel(6, 3);
    for (unsigned di = 3; di < panel.T; ++di) panel.at("close", di, 0) = kMissing;
    for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = 50.0f;
    panel.settlement_bar = {2, -1, -1};
    panel.settlement_return = {kMissing, kMissing, kMissing};
    panel.settlement_successor = {1, -1, -1};
    panel.settlement_successor_ratio = {2.0, std::nan(""), std::nan("")};
    return panel;
}
}  // namespace

TEST(MissingDataAccounting, SuccessorQuoteCarriesTheHoldingInsteadOfCashingOut) {
    for (int mode : {0, 1}) {
        auto panel = successor_panel();
        // Hold only the old name, then want only the successor once it arrives.
        const std::vector<std::vector<float>> plan = {
            {0, 0, 0}, {1, 0, 0}, {1, 0, 0}, {0, 1, 0}, {0, 1, 0}, {0, 1, 0}};
        AccountingLedger ledger;
        auto out = Simulator(panel, carry_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        // Bar 3 is the first silent bar: 1 old share becomes 2 shares at 50.
        // R = 2 * 50 / 100 - 1 = 0, so the book value carries over unchanged.
        EXPECT_NEAR(ledger.previous_weights[3][0], 1.0, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(ledger.gross[3], 0.0, 1e-12);
        EXPECT_EQ(out.missing_data[3].successor_conversions, 1u);
        EXPECT_NEAR(out.missing_data[3].converted_gross, 1.0, 1e-12);
        EXPECT_EQ(out.missing_data[3].computed_settlement_count, 1u);
        EXPECT_NEAR(out.missing_data[3].settled_gross, 0.0, 1e-12);  // nothing was cashed out
        EXPECT_EQ(out.missing_data[3].delisting_count, 1u);
        EXPECT_EQ(ledger.retired[3][0], 1);
        EXPECT_EQ(ledger.retired[3][1], 0);   // only the old name retires
        // The received shares already sit at the wanted target, so nothing trades.
        EXPECT_NEAR(ledger.weights[3][1], 1.0, 1e-12);
        EXPECT_NEAR(ledger.trades[3][1], 0.0, 1e-12);
        EXPECT_DOUBLE_EQ(out.turnover[3], 0.0);
        EXPECT_DOUBLE_EQ(ledger.fee[3], 0.0);
        EXPECT_EQ(out.position_count[3], 1);
        // From there the successor is an ordinary holding.
        EXPECT_NEAR(ledger.marks_after[3][1], 50.0, 1e-12);
        EXPECT_DOUBLE_EQ(out.ret[4], 0.0);
    }
}

TEST(MissingDataAccounting, ConvertedSharesRebalanceAtTheNextExecution) {
    for (int mode : {0, 1}) {
        auto panel = successor_panel();
        // The successor is not wanted at all: the received shares are sold.
        const std::vector<std::vector<float>> plan = {
            {0, 0, 0}, {1, 0, 0}, {1, 0, 0}, {0, 0, 1}, {0, 0, 1}, {0, 0, 1}};
        AccountingLedger ledger;
        auto out = Simulator(panel, carry_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        EXPECT_EQ(out.missing_data[3].successor_conversions, 1u) << "mode=" << mode;
        EXPECT_NEAR(ledger.weights[3][1], 0.0, 1e-12);
        EXPECT_NEAR(ledger.weights[3][2], 1.0, 1e-12);
        // Selling the converted shares and buying C is a real market trade.
        EXPECT_NEAR(ledger.trades[3][1], -1.0, 1e-12);
        EXPECT_NEAR(out.turnover[3], 2.0, 1e-12);
        EXPECT_NEAR(ledger.fee[3], 2.0 * kFee, 1e-12);
        EXPECT_NEAR(ledger.gross[3], 0.0, 1e-12);
        EXPECT_NEAR(out.ret[3], -2.0 * kFee, 1e-12);
    }
}

TEST(MissingDataAccounting, SuccessorRatioAndExplicitReturnPriceTheExchange) {
    for (int mode : {0, 1}) {
        // Ratio 1.5 at 50 against a mark of 100: R = 0.75 - 1 = -0.25.
        auto ratio_panel = successor_panel();
        ratio_panel.settlement_successor_ratio = {1.5, std::nan(""), std::nan("")};
        const std::vector<std::vector<float>> plan = {
            {0, 0, 0}, {1, 0, 0}, {1, 0, 0}, {0, 1, 0}, {0, 1, 0}, {0, 1, 0}};
        AccountingLedger ratio_ledger;
        auto ratio_out = Simulator(ratio_panel, carry_config(ratio_panel, mode, ratio_ledger))
                             .run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(ratio_ledger.gross[3], -0.25, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(ratio_out.missing_data[3].converted_gross, 0.75, 1e-12);
        EXPECT_EQ(ratio_out.missing_data[3].computed_settlement_count, 1u);
        // The target is a full book, so the 0.75 carried in is topped up to 1.
        EXPECT_NEAR(ratio_ledger.weights[3][1], 1.0, 1e-12);
        EXPECT_NEAR(ratio_out.turnover[3], 0.25, 1e-12);

        // The settlement bar's own successor quote outranks the file's return,
        // which was computed from prices the engine must not see yet.
        auto stale_file = successor_panel();
        stale_file.settlement_return = {-0.1f, kMissing, kMissing};
        AccountingLedger stale_ledger;
        auto stale_out = Simulator(stale_file, carry_config(stale_file, mode, stale_ledger))
                             .run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(stale_ledger.gross[3], 0.0, 1e-12);   // 2.0 * 50 / 100 - 1, not -0.1
        EXPECT_EQ(stale_out.missing_data[3].computed_settlement_count, 1u);
        EXPECT_EQ(stale_out.missing_data[3].successor_conversions, 1u);
        EXPECT_NEAR(stale_out.missing_data[3].converted_gross, 1.0, 1e-12);
        EXPECT_NEAR(stale_ledger.weights[3][1], 1.0, 1e-12);

        // A file return only survives where the proceeds cannot be priced here.
        auto unpriceable = successor_panel();
        unpriceable.settlement_successor = {-1, -1, -1};   // survivor outside the panel
        unpriceable.settlement_return = {-0.1f, kMissing, kMissing};
        AccountingLedger fallback_ledger;
        auto fallback = Simulator(unpriceable, carry_config(unpriceable, mode, fallback_ledger))
                            .run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(fallback_ledger.gross[3], static_cast<double>(-0.1f), 1e-12);
        EXPECT_EQ(fallback.missing_data[3].computed_settlement_count, 0u);
        EXPECT_EQ(fallback.missing_data[3].successor_conversions, 0u);
        EXPECT_NEAR(fallback.missing_data[3].settled_gross, 1.0, 1e-12);
        EXPECT_EQ(fallback.missing_data[3].default_delisting_count, 0u);
    }
}

// A cash-and-stock deal realizes the cash and carries only the shares.
TEST(MissingDataAccounting, MixedDealRealizesTheCashLegAndCarriesOnlyTheStockLeg) {
    for (int mode : {0, 1}) {
        auto panel = successor_panel();
        panel.settlement_successor_ratio = {1.0, std::nan(""), std::nan("")};
        panel.settlement_cash = {50.0, std::nan(""), std::nan("")};
        // Per old share: $50 cash plus one successor share worth 50, against a
        // mark of 100. The deal is flat, so the exposure and value bases of the
        // two gross counters happen to agree here; the next test separates them.
        const std::vector<std::vector<float>> plan = {
            {0, 0, 0}, {1, 0, 0}, {1, 0, 0}, {0, 1, 0}, {0, 1, 0}, {0, 1, 0}};
        AccountingLedger ledger;
        auto out = Simulator(panel, carry_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        EXPECT_NEAR(ledger.gross[3], 0.0, 1e-12) << "mode=" << mode;
        EXPECT_EQ(out.missing_data[3].computed_settlement_count, 1u);
        EXPECT_EQ(out.missing_data[3].successor_conversions, 1u);
        EXPECT_NEAR(out.missing_data[3].converted_gross, 0.5, 1e-12);  // stock leg only
        EXPECT_NEAR(out.missing_data[3].settled_gross, 0.5, 1e-12);    // cash leg realized
        // The received shares are half the wanted position, so only half trades.
        EXPECT_NEAR(ledger.weights[3][1], 1.0, 1e-12);
        EXPECT_NEAR(ledger.trades[3][1], 0.5, 1e-12);
        EXPECT_NEAR(out.turnover[3], 0.5, 1e-12);
        EXPECT_NEAR(ledger.fee[3], 0.5 * kFee, 1e-12);
        EXPECT_NEAR(out.ret[3], -0.5 * kFee, 1e-12);
        EXPECT_EQ(ledger.retired[3][0], 1);
    }
}

// A pure cash deal needs no return in the file at all.
TEST(MissingDataAccounting, CashOnlyDealsArePricedFromTheCashAmountAndTheMark) {
    for (int mode : {0, 1}) {
        auto panel = successor_panel();
        panel.settlement_successor = {-1, -1, -1};
        panel.settlement_successor_ratio = {std::nan(""), std::nan(""), std::nan("")};
        panel.settlement_cash = {120.0, std::nan(""), std::nan("")};   // $120 against a mark of 100
        const std::vector<std::vector<float>> plan = {
            {0, 0, 0}, {1, 0, 0}, {1, 0, 0}, {0, 0, 1}, {0, 0, 1}, {0, 0, 1}};
        AccountingLedger ledger;
        auto config = carry_config(panel, mode, ledger);
        config.default_delisting_return = -1.0;        // must not be reached
        auto out = Simulator(panel, config).run_from_signals(plan_signals(plan, mode));

        EXPECT_NEAR(ledger.gross[3], 0.2, 1e-12) << "mode=" << mode;
        EXPECT_EQ(out.missing_data[3].computed_settlement_count, 1u);
        EXPECT_EQ(out.missing_data[3].default_delisting_count, 0u);
        EXPECT_EQ(out.missing_data[3].successor_conversions, 0u);
        EXPECT_NEAR(out.missing_data[3].converted_gross, 0.0, 1e-12);
        EXPECT_NEAR(out.missing_data[3].settled_gross, 1.0, 1e-12);
        EXPECT_NEAR(ledger.marks_after[3][0], 120.0, 1e-12);
        // The cash leaves the book; the next target is funded by a market trade.
        EXPECT_NEAR(ledger.weights[3][0], 0.0, 1e-12);
        EXPECT_NEAR(ledger.weights[3][2], 1.0, 1e-12);
        EXPECT_NEAR(out.turnover[3], 1.0, 1e-12);
    }
}

TEST(MissingDataAccounting, SettlementReturnsPreferPricedProceedsThenTheFileThenTheDefault) {
    const std::vector<std::vector<float>> plan = {
        {0, 0, 0}, {1, 0, 0}, {1, 0, 0}, {0, 0, 1}, {0, 0, 1}, {0, 0, 1}};
    for (int mode : {0, 1}) {
        // Priced here: cash plus stock, both known on the settlement bar.
        auto priced = successor_panel();
        priced.settlement_successor_ratio = {1.0, std::nan(""), std::nan("")};
        priced.settlement_cash = {30.0, std::nan(""), std::nan("")};
        priced.settlement_return = {0.9f, kMissing, kMissing};
        AccountingLedger priced_ledger;
        auto priced_out = Simulator(priced, carry_config(priced, mode, priced_ledger))
                              .run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(priced_ledger.gross[3], -0.2, 1e-12) << "mode=" << mode;  // (30+50)/100-1
        EXPECT_EQ(priced_out.missing_data[3].computed_settlement_count, 1u);

        // Not priceable: a stock leg whose survivor never quotes here.
        auto filed = successor_panel();
        filed.at("close", 3, 1) = kMissing;
        filed.settlement_return = {0.9f, kMissing, kMissing};
        AccountingLedger filed_ledger;
        auto filed_out = Simulator(filed, carry_config(filed, mode, filed_ledger))
                             .run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(filed_ledger.gross[3], static_cast<double>(0.9f), 1e-12);
        EXPECT_EQ(filed_out.missing_data[3].computed_settlement_count, 0u);
        EXPECT_EQ(filed_out.missing_data[3].default_delisting_count, 0u);

        // Neither priceable nor filed: the configured default is the last resort.
        auto bare = successor_panel();
        bare.at("close", 3, 1) = kMissing;
        AccountingLedger bare_ledger;
        auto bare_config = carry_config(bare, mode, bare_ledger);
        bare_config.default_delisting_return = -1.0;
        auto bare_out = Simulator(bare, bare_config).run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(bare_ledger.gross[3], -1.0, 1e-12);
        EXPECT_EQ(bare_out.missing_data[3].default_delisting_count, 1u);
        EXPECT_EQ(bare_out.missing_data[3].computed_settlement_count, 0u);
    }
}

TEST(MissingDataAccounting, AnUnavailableSuccessorFallsBackToCashSettlement) {
    for (int mode : {0, 1}) {
        const std::vector<std::vector<float>> plan = {
            {0, 0, 0}, {1, 0, 0}, {1, 0, 0}, {0, 1, 0}, {0, 1, 0}, {0, 1, 0}};
        // The successor has no quote on the settlement bar.
        auto silent = successor_panel();
        silent.at("close", 3, 1) = kMissing;
        AccountingLedger silent_ledger;
        auto silent_out = Simulator(silent, carry_config(silent, mode, silent_ledger))
                              .run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(silent_out.missing_data[3].successor_conversions, 0u) << "mode=" << mode;
        EXPECT_NEAR(silent_out.missing_data[3].settled_gross, 1.0, 1e-12);
        EXPECT_NEAR(silent_ledger.gross[3], 0.0, 1e-12);   // default return 0
        EXPECT_EQ(silent_out.missing_data[3].default_delisting_count, 1u);
        EXPECT_NEAR(silent_ledger.weights[3][1], 0.0, 1e-12);

        // The successor is outside the loaded panel.
        auto outside = successor_panel();
        outside.settlement_successor = {-1, -1, -1};
        AccountingLedger outside_ledger;
        auto outside_out = Simulator(outside, carry_config(outside, mode, outside_ledger))
                               .run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(outside_out.missing_data[3].successor_conversions, 0u);
        EXPECT_NEAR(outside_out.missing_data[3].settled_gross, 1.0, 1e-12);
        EXPECT_EQ(outside_out.missing_data[3].computed_settlement_count, 0u);
        EXPECT_NEAR(outside_ledger.weights[3][1], 1.0, 1e-12);   // bought on the market instead
        EXPECT_NEAR(outside_out.turnover[3], 1.0, 1e-12);

        // A successor that retires on the same bar cannot receive anything.
        auto chained = successor_panel();
        for (unsigned di = 3; di < chained.T; ++di) chained.at("close", di, 1) = kMissing;
        chained.settlement_bar = {2, 2, -1};
        AccountingLedger chained_ledger;
        auto chained_out = Simulator(chained, carry_config(chained, mode, chained_ledger))
                               .run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(chained_out.missing_data[3].successor_conversions, 0u);
        EXPECT_EQ(chained_ledger.retired[3][0], 1);
        EXPECT_EQ(chained_ledger.retired[3][1], 1);
        EXPECT_NEAR(chained_out.missing_data[3].settled_gross, 1.0, 1e-12);
    }
}

TEST(MissingDataAccounting, RejectsSuccessorVectorsThatDoNotMatchThePanel) {
    auto panel = flat_panel(4, 2);
    SimulatorConfig config{panel.T, panel.N, kFee, 1, false, {}};
    panel.settlement_successor = {1};
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.settlement_successor = {1, -1};
    panel.settlement_successor_ratio = {1.0};
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.settlement_successor_ratio = {1.0, std::nan("")};
    EXPECT_NO_THROW(Simulator(panel, config));
    panel.settlement_cash = {10.0};
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.settlement_cash = {10.0, std::nan("")};
    EXPECT_NO_THROW(Simulator(panel, config));
}

// settled_gross keeps the Phase 1 exposure basis so the default-return
// sensitivities stay comparable; converted_gross reports the weight handed over.
TEST(MissingDataAccounting, MixedDealGrossCountersUseExposureAndTransferredWeight) {
    for (int mode : {0, 1}) {
        auto panel = successor_panel();
        panel.settlement_successor_ratio = {1.0, std::nan(""), std::nan("")};
        panel.settlement_cash = {30.0, std::nan(""), std::nan("")};
        // Proceeds per old share are 30 cash + 50 stock = 80 against a mark of 100.
        const std::vector<std::vector<float>> plan = {
            {0, 0, 0}, {1, 0, 0}, {1, 0, 0}, {0, 1, 0}, {0, 1, 0}, {0, 1, 0}};
        AccountingLedger ledger;
        auto out = Simulator(panel, carry_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        EXPECT_NEAR(ledger.gross[3], -0.2, 1e-12) << "mode=" << mode;   // 80/100 - 1
        // Exposure basis: the cash leg is 30/80 of the proceeds, so 0.375 of |h_prev|.
        EXPECT_NEAR(out.missing_data[3].settled_gross, 0.375, 1e-12);
        EXPECT_NEAR(ledger.settled_gross[3][0], 0.375, 1e-12);
        // Value basis: the successor actually receives 1.0 * 50 / 100 = 0.5 of weight.
        EXPECT_NEAR(out.missing_data[3].converted_gross, 0.5, 1e-12);
        EXPECT_NEAR(ledger.weights[3][1] - ledger.trades[3][1], 0.5, 1e-12);
        // The two bases are deliberately different and do not have to sum to 1.
        EXPECT_NEAR(out.missing_data[3].settled_gross + 1.0 * (50.0 / 80.0), 1.0, 1e-12);
        EXPECT_NEAR(out.turnover[3], 0.5, 1e-12);   // topped up from 0.5 to the full target
    }
}

// A stock-only settlement still reports no cash exposure at all.
TEST(MissingDataAccounting, StockOnlyConversionsReportNoSettledExposure) {
    auto panel = successor_panel();
    const std::vector<std::vector<float>> plan(panel.T, {1, 0, 0});
    AccountingLedger ledger;
    auto out = Simulator(panel, carry_config(panel, 0, ledger)).run_from_signals(plan);
    EXPECT_NEAR(out.missing_data[3].settled_gross, 0.0, 1e-12);
    EXPECT_NEAR(out.missing_data[3].converted_gross, 1.0, 1e-12);
    EXPECT_EQ(out.missing_data[3].successor_conversions, 1u);
}

// A declared leg that this panel cannot price blocks the computation; the file
// return takes over. Cash alone only settles a deal with no stock leg at all.
TEST(MissingDataAccounting, DeclaredLegsMustAllBePriceableBeforeAReturnIsComputed) {
    for (int mode : {0, 1}) {
        const std::vector<std::vector<float>> plan = {
            {0, 0, 0}, {1, 0, 0}, {1, 0, 0}, {0, 0, 1}, {0, 0, 1}, {0, 0, 1}};
        // Cash 50 against a mark of 100 would be -0.5 on its own, but the deal
        // also has a stock leg this panel cannot restate.
        auto blocked = successor_panel();
        blocked.settlement_successor_ratio = {std::nan(""), std::nan(""), std::nan("")};
        blocked.settlement_cash = {50.0, std::nan(""), std::nan("")};
        blocked.settlement_return = {-0.02f, kMissing, kMissing};
        blocked.settlement_has_stock_leg = {1, 0, 0};
        blocked.settlement_has_cash_leg = {1, 0, 0};
        AccountingLedger blocked_ledger;
        auto blocked_out = Simulator(blocked, carry_config(blocked, mode, blocked_ledger))
                               .run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(blocked_ledger.gross[3], static_cast<double>(-0.02f), 1e-12) << "mode=" << mode;
        EXPECT_EQ(blocked_out.missing_data[3].computed_settlement_count, 0u);
        EXPECT_EQ(blocked_out.missing_data[3].successor_conversions, 0u);
        EXPECT_NEAR(blocked_out.missing_data[3].settled_gross, 1.0, 1e-12);

        // A named successor alone declares the stock leg when no flags are given.
        auto derived = successor_panel();
        derived.settlement_successor_ratio = {std::nan(""), std::nan(""), std::nan("")};
        derived.settlement_cash = {50.0, std::nan(""), std::nan("")};
        derived.settlement_return = {-0.02f, kMissing, kMissing};
        derived.settlement_successor_named = {1, 0, 0};
        AccountingLedger derived_ledger;
        auto derived_out = Simulator(derived, carry_config(derived, mode, derived_ledger))
                               .run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(derived_ledger.gross[3], static_cast<double>(-0.02f), 1e-12);
        EXPECT_EQ(derived_out.missing_data[3].computed_settlement_count, 0u);

        // Declaring only a cash leg keeps the cash-only computation available.
        auto cash_only = successor_panel();
        cash_only.settlement_successor = {-1, -1, -1};
        cash_only.settlement_successor_ratio = {std::nan(""), std::nan(""), std::nan("")};
        cash_only.settlement_cash = {50.0, std::nan(""), std::nan("")};
        cash_only.settlement_return = {-0.02f, kMissing, kMissing};
        cash_only.settlement_has_stock_leg = {0, 0, 0};
        cash_only.settlement_has_cash_leg = {1, 0, 0};
        AccountingLedger cash_ledger;
        auto cash_out = Simulator(cash_only, carry_config(cash_only, mode, cash_ledger))
                            .run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(cash_ledger.gross[3], -0.5, 1e-12);
        EXPECT_EQ(cash_out.missing_data[3].computed_settlement_count, 1u);
    }
}

TEST(MissingDataAccounting, RejectsLegFlagsThatDoNotMatchThePanel) {
    auto panel = flat_panel(4, 2);
    SimulatorConfig config{panel.T, panel.N, kFee, 1, false, {}};
    panel.settlement_has_stock_leg = {1};
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.settlement_has_stock_leg = {1, 0};
    panel.settlement_has_cash_leg = {1};
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.settlement_has_cash_leg = {1, 0};
    panel.settlement_successor_named = {1};
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.settlement_successor_named = {1, 0};
    EXPECT_NO_THROW(Simulator(panel, config));
}
