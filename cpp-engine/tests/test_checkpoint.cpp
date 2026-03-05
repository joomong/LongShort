#include <gtest/gtest.h>
#include <cstdio>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <vector>

#include "checkpoint/checkpoint_manager.h"
#include "data/data_panel.h"
#include "engine/simulator.h"
#include "modules/Data.h"
#include "modules/Rank.h"
#include "modules/TsMean.h"

// ---------------------------------------------------------------------------
// Minimal stateful module for unit testing.
// ---------------------------------------------------------------------------
struct CounterModule : public Module {
    double counter = 0.0;

    void init() {}

    void calculate(unsigned /*di*/, SignalVector &output)
    {
        counter += 1.0;
        for (unsigned i = 0; i < numAssets; ++i)
            output[i] = (float)counter;
    }

    void checkpoint(boost::archive::binary_iarchive &ar)
    {
        ar & counter;
    }
    void checkpoint(boost::archive::binary_oarchive &ar)
    {
        ar & counter;
    }
};

// ---------------------------------------------------------------------------
// Helper: build a small DataPanel
// ---------------------------------------------------------------------------
static DataPanel make_panel(unsigned T, unsigned N) {
    DataPanel dp;
    dp.T = T;
    dp.N = N;
    dp.timestamps.resize(T);
    for (unsigned t = 0; t < T; ++t)
        dp.timestamps[t] = static_cast<int64_t>(t) * 86400000LL;
    dp.symbols.resize(N);
    for (unsigned i = 0; i < N; ++i)
        dp.symbols[i] = "S" + std::to_string(i);

    auto& close = dp.arrays["close"];
    close.resize(T * N);
    for (unsigned t = 0; t < T; ++t)
        for (unsigned i = 0; i < N; ++i)
            close[t * N + i] = 100.0f + static_cast<float>(t * N + i);
    return dp;
}

static std::filesystem::path find_fixture(const std::string& name)
{
    const std::vector<std::filesystem::path> candidates = {
        std::filesystem::current_path() / "tests" / "fixtures" / name,
        std::filesystem::current_path() / ".." / "tests" / "fixtures" / name,
        std::filesystem::current_path() / ".." / ".." / "cpp-engine" / "tests" / "fixtures" / name,
    };
    for (const auto& p : candidates) {
        if (std::filesystem::exists(p))
            return std::filesystem::weakly_canonical(p);
    }
    return {};
}

// ---------------------------------------------------------------------------
// TEST: SimulatorState round-trip through save/load
// ---------------------------------------------------------------------------
TEST(Checkpoint, SaveLoadSimulatorState) {
    const char* tmp = "/tmp/test_checkpoint_state.bin";

    SimulatorState orig;
    orig.di = 42u;
    orig.prev_weights = {0.1, -0.2, 0.3, 0.4};
    orig.prev_target  = {0.05, -0.15, 0.25, 0.35};

    std::vector<Module*> no_modules;
    CheckpointManager::save(tmp, orig, no_modules);

    SimulatorState loaded = CheckpointManager::load(tmp, no_modules);

    EXPECT_EQ(loaded.di, orig.di);
    ASSERT_EQ(loaded.prev_weights.size(), orig.prev_weights.size());
    ASSERT_EQ(loaded.prev_target.size(),  orig.prev_target.size());
    for (size_t i = 0; i < orig.prev_weights.size(); ++i) {
        EXPECT_DOUBLE_EQ(loaded.prev_weights[i], orig.prev_weights[i]);
        EXPECT_DOUBLE_EQ(loaded.prev_target[i],  orig.prev_target[i]);
    }

    std::remove(tmp);
}

// ---------------------------------------------------------------------------
// TEST: Stateful module state is saved and restored
// ---------------------------------------------------------------------------
TEST(Checkpoint, ModuleStateSavedAndRestored) {
    const char* tmp = "/tmp/test_checkpoint_module.bin";

    CounterModule mod;
    mod.numAssets = 2;
    mod.init();
    // Simulate 5 calculate calls
    std::vector<float> buf(2);
    SignalVector sv{buf.data(), 2};
    for (int i = 0; i < 5; ++i) mod.calculate(0, sv);
    EXPECT_DOUBLE_EQ(mod.counter, 5.0);

    SimulatorState state;
    state.di = 5u;
    state.prev_weights = {0.5, -0.5};
    state.prev_target  = {0.5, -0.5};

    std::vector<Module*> mods = {&mod};
    CheckpointManager::save(tmp, state, mods);

    // Reset counter then reload
    mod.counter = 0.0;
    SimulatorState loaded = CheckpointManager::load(tmp, mods);

    EXPECT_EQ(loaded.di, 5u);
    EXPECT_DOUBLE_EQ(mod.counter, 5.0);  // restored

    std::remove(tmp);
}

// ---------------------------------------------------------------------------
// TEST: ModuleInput serialization round-trip preserves lookback rows
// ---------------------------------------------------------------------------
TEST(Checkpoint, ModuleInputRoundTripPreservesRows) {
    const char* tmp = "/tmp/test_checkpoint_module_input.bin";

    ModuleInput input;
    input.maxLookBack = 2;

    std::vector<float> row0 = {1.0f, 10.0f};
    std::vector<float> row1 = {2.0f, 20.0f};
    std::vector<float> row2 = {3.0f, 30.0f};
    std::vector<float> row3 = {4.0f, 40.0f};
    std::vector<float> row4 = {5.0f, 50.0f};
    SignalVector sv0{row0.data(), 2};
    SignalVector sv1{row1.data(), 2};
    SignalVector sv2{row2.data(), 2};
    SignalVector sv3{row3.data(), 2};
    SignalVector sv4{row4.data(), 2};
    input.push(sv0);
    input.push(sv1);
    input.push(sv2);
    input.push(sv3);
    input.push(sv4);

    {
        std::ofstream ofs(tmp, std::ios::binary | std::ios::trunc);
        ASSERT_TRUE(ofs.good());
        boost::archive::binary_oarchive ar(ofs);
        ar & input;
    }

    ModuleInput loaded;
    {
        std::ifstream ifs(tmp, std::ios::binary);
        ASSERT_TRUE(ifs.good());
        boost::archive::binary_iarchive ar(ifs);
        ar & loaded;
    }

    const auto latest = loaded(0);
    const auto prev1 = loaded(1);
    const auto prev2 = loaded(2);

    ASSERT_EQ(latest.size, 2u);
    ASSERT_EQ(prev1.size, 2u);
    ASSERT_EQ(prev2.size, 2u);

    EXPECT_FLOAT_EQ(latest[0], 5.0f);
    EXPECT_FLOAT_EQ(latest[1], 50.0f);
    EXPECT_FLOAT_EQ(prev1[0], 4.0f);
    EXPECT_FLOAT_EQ(prev1[1], 40.0f);
    EXPECT_FLOAT_EQ(prev2[0], 3.0f);
    EXPECT_FLOAT_EQ(prev2[1], 30.0f);

    std::vector<float> row5 = {6.0f, 60.0f};
    SignalVector sv5{row5.data(), 2};
    loaded.push(sv5);
    const auto after0 = loaded(0);
    const auto after1 = loaded(1);
    const auto after2 = loaded(2);
    EXPECT_FLOAT_EQ(after0[0], 6.0f);
    EXPECT_FLOAT_EQ(after0[1], 60.0f);
    EXPECT_FLOAT_EQ(after1[0], 5.0f);
    EXPECT_FLOAT_EQ(after1[1], 50.0f);
    EXPECT_FLOAT_EQ(after2[0], 4.0f);
    EXPECT_FLOAT_EQ(after2[1], 40.0f);

    std::remove(tmp);
}

// ---------------------------------------------------------------------------
// TEST: ModuleInput loads checkpoint fixture created by pre-flat-ring format
// ---------------------------------------------------------------------------
TEST(Checkpoint, ModuleInputLoadsLegacyFixtureV0) {
    const std::filesystem::path fixture = find_fixture("module_input_legacy_v0.bin");
    ASSERT_FALSE(fixture.empty()) << "missing module_input_legacy_v0.bin fixture";

    ModuleInput loaded;
    {
        std::ifstream ifs(fixture, std::ios::binary);
        ASSERT_TRUE(ifs.good());
        boost::archive::binary_iarchive ar(ifs);
        ar & loaded;
    }

    EXPECT_EQ(loaded.maxLookBack, 2u);
    const auto latest = loaded(0);
    const auto prev1 = loaded(1);
    const auto prev2 = loaded(2);
    ASSERT_EQ(latest.size, 2u);
    ASSERT_EQ(prev1.size, 2u);
    ASSERT_EQ(prev2.size, 2u);

    EXPECT_FLOAT_EQ(latest[0], 5.0f);
    EXPECT_FLOAT_EQ(latest[1], 50.0f);
    EXPECT_FLOAT_EQ(prev1[0], 4.0f);
    EXPECT_FLOAT_EQ(prev1[1], 40.0f);
    EXPECT_FLOAT_EQ(prev2[0], 3.0f);
    EXPECT_FLOAT_EQ(prev2[1], 30.0f);
}

// ---------------------------------------------------------------------------
// TEST: Stateless modules write zero bytes
// ---------------------------------------------------------------------------
TEST(Checkpoint, StatelessModulesNoOp) {
    const char* tmp = "/tmp/test_checkpoint_skip.bin";

    DataPanel dp = make_panel(5, 2);
    MData data_mod;
    data_mod.column_name = "close";
    data_mod.panel = &dp;
    data_mod.numAssets = 2;
    data_mod.init();
    CounterModule counter_mod;
    counter_mod.numAssets = 2;
    counter_mod.init();

    std::vector<float> buf(2);
    SignalVector sv{buf.data(), 2};
    counter_mod.calculate(0, sv);  // counter = 1

    SimulatorState state;
    state.di = 1u;
    state.prev_weights = {0.0, 0.0};
    state.prev_target  = {0.0, 0.0};

    std::vector<Module*> mods = {&data_mod, &counter_mod};
    CheckpointManager::save(tmp, state, mods);

    counter_mod.counter = 99.0;
    SimulatorState loaded = CheckpointManager::load(tmp, mods);

    EXPECT_EQ(loaded.di, 1u);
    EXPECT_DOUBLE_EQ(counter_mod.counter, 1.0);  // restored, not 99

    std::remove(tmp);
}

// ---------------------------------------------------------------------------
// TEST: Round-trip with stateless pipeline (close -> rank)
// ---------------------------------------------------------------------------
TEST(Checkpoint, RoundTrip) {
    const char* tmp = "/tmp/test_checkpoint_roundtrip.bin";

    DataPanel dp = make_panel(10, 3);

    SimulatorConfig cfg;
    cfg.T = dp.T;
    cfg.N = dp.N;
    cfg.fee_rate = 0.001;
    cfg.mode = 1;
    cfg.bars_per_year = 365;

    // --- Full run (reference) ---
    std::vector<double> ref_ret;
    {
        MData dm;
        dm.column_name = "close";
        dm.panel = &dp;
        MRank rm;
        rm.pct = IntParameter(1);
        std::vector<Module*> pipeline = {&dm, &rm};
        Simulator sim(dp, cfg);
        SimulatorOutput out = sim.run(pipeline);
        ref_ret = out.ret;
    }

    // --- Save/Load path ---
    {
        SimulatorState state;
        state.di = 5u;
        state.prev_weights = std::vector<double>(3, 0.0);
        state.prev_target  = std::vector<double>(3, 0.0);

        std::vector<Module*> no_mods;
        CheckpointManager::save(tmp, state, no_mods);
        SimulatorState loaded = CheckpointManager::load(tmp, no_mods);
        EXPECT_EQ(loaded.di, 5u);
    }

    bool any_nonzero = false;
    for (double r : ref_ret) if (r != 0.0) { any_nonzero = true; break; }
    EXPECT_TRUE(any_nonzero);

    std::remove(tmp);
}
