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


// Missing-close accounting on wide price panels.

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
    std::vector<std::vector<double>> marks_before, marks_after;
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
            EXPECT_DOUBLE_EQ(observed.missing_data[di].frozen_gross, 0.0);
        }
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
// not move. No missing closes.
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
