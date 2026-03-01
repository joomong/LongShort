#include <gtest/gtest.h>
#include <cmath>
#include "data/data_panel.h"
#include "engine/simulator.h"
#include "modules/Data.h"
#include "modules/Rank.h"

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
    cfg.bars_per_year = 365;

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
        EXPECT_GT(out.nonzero_count[t], 0);
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
    cfg.T = 5; cfg.N = 2; cfg.fee_rate = 0.0; cfg.mode = 1; cfg.bars_per_year = 365;

    Simulator sim(dp, cfg);
    SimulatorOutput out = sim.run(pipeline);

    // First 2 bars: no positions executed in D1 (prev_target is 0 at di=0)
    EXPECT_DOUBLE_EQ(out.ret[0], 0.0);
    // di=1: exec_w = prev_target (from di=0 signal), but prev_weights was 0
    // turnover should be > 0 at di=1
    EXPECT_GT(out.turnover[1], 0.0);
}
