#include <gtest/gtest.h>
#include <cmath>
#include <random>
#include <vector>
#include <limits>
#include "signal_vector.h"
#include "module.h"
#include "simulation_data.h"
#include "modules/Data.h"
#include "modules/Delay.h"
#include "modules/TsMean.h"
#include "modules/TsRank.h"
#include "modules/TsZscore.h"
#include "modules/Decay.h"
#include "modules/Rank.h"
#include "modules/Zscore.h"
#include "modules/Neutralize.h"
#include "modules/Ops.h"
#include "modules/Constant.h"

static const float NaN = std::numeric_limits<float>::quiet_NaN();

// Helper: create a simple DataPanel for testing
static DataPanel make_test_panel(unsigned T, unsigned N) {
    DataPanel dp;
    dp.T = T;
    dp.N = N;
    dp.timestamps.resize(T);
    for (unsigned t = 0; t < T; ++t) dp.timestamps[t] = (int64_t)t * 86400000;
    dp.symbols.resize(N);
    for (unsigned n = 0; n < N; ++n) dp.symbols[n] = "SYM" + std::to_string(n);

    // close: incrementing values
    auto& close = dp.arrays["close"];
    close.resize(T * N);
    for (unsigned t = 0; t < T; ++t)
        for (unsigned n = 0; n < N; ++n)
            close[t * N + n] = 100.0f + (float)t + (float)n * 0.1f;

    return dp;
}

// ======== MData ========
TEST(MData, ReadsFromPanel) {
    auto dp = make_test_panel(5, 3);
    MData mod;
    mod.column_name = "close";
    mod.panel = &dp;
    mod.numAssets = 3;
    mod.init();

    std::vector<float> buf(3);
    SignalVector sv{buf.data(), 3};

    mod.calculate(0, sv);
    EXPECT_FLOAT_EQ(sv[0], 100.0f);
    EXPECT_FLOAT_EQ(sv[1], 100.1f);
    EXPECT_FLOAT_EQ(sv[2], 100.2f);

    mod.calculate(2, sv);
    EXPECT_FLOAT_EQ(sv[0], 102.0f);
}

// ======== MDelay ========
TEST(MDelay, DelayByOne) {
    auto dp = make_test_panel(5, 2);
    MData data_mod;
    data_mod.column_name = "close";
    data_mod.panel = &dp;
    data_mod.numAssets = 2;
    data_mod.init();

    MDelay delay_mod;
    delay_mod.days = IntParameter(1);
    delay_mod.numAssets = 2;
    delay_mod.init();

    std::vector<float> buf(2);
    SignalVector sv{buf.data(), 2};

    // di=0: data_mod outputs close[0], delay should output NaN (no previous)
    data_mod.calculate(0, sv);
    delay_mod.calculate(0, sv);
    EXPECT_TRUE(std::isnan(sv[0]));

    // di=1: should output close[0]
    data_mod.calculate(1, sv);
    delay_mod.calculate(1, sv);
    EXPECT_FLOAT_EQ(sv[0], 100.0f);
    EXPECT_FLOAT_EQ(sv[1], 100.1f);
}

// ======== MTsMean ========
TEST(MTsMean, WarmupNaN) {
    MTsMean mod;
    mod.days = IntParameter(3);
    mod.numAssets = 2;
    mod.init();

    std::vector<float> buf(2);
    SignalVector sv{buf.data(), 2};

    // Feed values: 1, 2 -> still in warmup (count < 3)
    buf = {1.0f, 2.0f};
    mod.calculate(0, sv);
    EXPECT_TRUE(std::isnan(sv[0]));

    buf = {3.0f, 4.0f};
    mod.calculate(1, sv);
    EXPECT_TRUE(std::isnan(sv[0]));

    // Third value -> mean of [1,3,5] = 3.0 for sym0
    buf = {5.0f, 6.0f};
    mod.calculate(2, sv);
    EXPECT_NEAR(sv[0], 3.0f, 1e-5f);
    EXPECT_NEAR(sv[1], 4.0f, 1e-5f);
}

// ======== MTsRank ========
TEST(MTsRank, BasicRank) {
    MTsRank mod;
    mod.days = IntParameter(3);
    mod.pct = IntParameter(1);
    mod.min_count = IntParameter(3);
    mod.numAssets = 1;
    mod.init();

    std::vector<float> buf(1);
    SignalVector sv{buf.data(), 1};

    // Feed: 10, 20, 30 -> current=30, ascending (always)
    // n_less (values < 30) = 2, n_equal = 1
    // rank = (2 + 1 + 0/2) / 3 = 1.0
    buf = {10.0f}; mod.calculate(0, sv);
    buf = {20.0f}; mod.calculate(1, sv);
    buf = {30.0f}; mod.calculate(2, sv);
    EXPECT_NEAR(sv[0], 1.0f, 1e-5f);
}

// ======== MTsZscore ========
TEST(MTsZscore, BasicZscore) {
    MTsZscore mod;
    mod.days = IntParameter(3);
    mod.ddof = IntParameter(1);
    mod.min_count = IntParameter(3);
    mod.eps = FloatParameter(1e-12);
    mod.numAssets = 1;
    mod.init();

    std::vector<float> buf(1);
    SignalVector sv{buf.data(), 1};

    // Feed: 10, 20, 30 -> mean=20, std=10, z=(30-20)/(10+eps) = 1.0
    buf = {10.0f}; mod.calculate(0, sv);
    buf = {20.0f}; mod.calculate(1, sv);
    buf = {30.0f}; mod.calculate(2, sv);
    EXPECT_NEAR(sv[0], 1.0f, 1e-4f);
}

// ======== MDecay ========
TEST(MDecay, LinearDecay) {
    MDecay mod;
    mod.days = IntParameter(3);
    mod.numAssets = 1;
    mod.init();

    std::vector<float> buf(1);
    SignalVector sv{buf.data(), 1};

    // Feed 3 values: 10, 20, 30
    // weights: newest=3, middle=2, oldest=1 -> (30*3 + 20*2 + 10*1)/(3+2+1) = 140/6 = 23.333
    buf = {10.0f}; mod.calculate(0, sv);
    buf = {20.0f}; mod.calculate(1, sv);
    buf = {30.0f}; mod.calculate(2, sv);
    EXPECT_NEAR(sv[0], 140.0f / 6.0f, 1e-4f);
}

// ======== MRank (cross-sectional) ========
TEST(MRank, CrossSectionalRank) {
    MRank mod;
    mod.pct = IntParameter(1);
    mod.numAssets = 3;
    mod.init();

    std::vector<float> buf = {10.0f, 30.0f, 20.0f};
    SignalVector sv{buf.data(), 3};

    mod.calculate(0, sv);
    // Sorted: 10(rank1), 20(rank2), 30(rank3) -> pct: 1/3, 2/3, 3/3
    EXPECT_NEAR(sv[0], 1.0f / 3.0f, 1e-5f);  // 10 -> rank 1
    EXPECT_NEAR(sv[1], 3.0f / 3.0f, 1e-5f);  // 30 -> rank 3
    EXPECT_NEAR(sv[2], 2.0f / 3.0f, 1e-5f);  // 20 -> rank 2
}

TEST(MRank, TieAndNaNHandling) {
    MRank mod;
    mod.pct = IntParameter(1);
    mod.numAssets = 4;
    mod.init();

    std::vector<float> buf = {10.0f, 10.0f, 30.0f, NaN};
    SignalVector sv{buf.data(), 4};

    mod.calculate(0, sv);
    // finite values are [10, 10, 30]
    // tie group(10,10): average rank=(1+2)/2=1.5 -> pct=0.5
    EXPECT_NEAR(sv[0], 0.5f, 1e-5f);
    EXPECT_NEAR(sv[1], 0.5f, 1e-5f);
    EXPECT_NEAR(sv[2], 1.0f, 1e-5f);
    EXPECT_TRUE(std::isnan(sv[3]));
}

// ======== MZscore (cross-sectional) ========
TEST(MZscore, CrossSectionalZscore) {
    MZscore mod;
    mod.ddof = IntParameter(1);
    mod.min_count = IntParameter(2);
    mod.eps = FloatParameter(1e-12);
    mod.numAssets = 3;
    mod.init();

    std::vector<float> buf = {10.0f, 20.0f, 30.0f};
    SignalVector sv{buf.data(), 3};

    mod.calculate(0, sv);
    // mean=20, std(ddof=1)=10 -> z = (v-20)/(10+eps)
    EXPECT_NEAR(sv[0], -1.0f, 1e-4f);
    EXPECT_NEAR(sv[1], 0.0f, 1e-4f);
    EXPECT_NEAR(sv[2], 1.0f, 1e-4f);
}

// ======== MConstant ========
TEST(MConstant, FillsConstant) {
    MConstant mod;
    mod.value = FloatParameter(42.0);
    mod.numAssets = 3;
    mod.init();

    std::vector<float> buf(3, 0.0f);
    SignalVector sv{buf.data(), 3};

    mod.calculate(0, sv);
    EXPECT_FLOAT_EQ(sv[0], 42.0f);
    EXPECT_FLOAT_EQ(sv[1], 42.0f);
    EXPECT_FLOAT_EQ(sv[2], 42.0f);
}

// ======== MAdd ========
TEST(MAdd, BasicAdd) {
    MConstant c1, c2;
    c1.value = FloatParameter(10.0);
    c2.value = FloatParameter(20.0);
    MAdd mod;
    mod.lhs = &c1;
    mod.rhs = &c2;
    mod.numAssets = 2;
    mod.init();

    std::vector<float> buf(2, 0.0f);
    SignalVector sv{buf.data(), 2};

    mod.calculate(0, sv);
    EXPECT_FLOAT_EQ(sv[0], 30.0f);
    EXPECT_FLOAT_EQ(sv[1], 30.0f);
}

// ======== MDiv ========
TEST(MDiv, DivByZero) {
    MConstant c1, c2;
    c1.value = FloatParameter(10.0);
    c2.value = FloatParameter(0.0);
    MDiv mod;
    mod.lhs = &c1;
    mod.rhs = &c2;
    mod.numAssets = 2;
    mod.init();

    std::vector<float> buf(2, 0.0f);
    SignalVector sv{buf.data(), 2};

    mod.calculate(0, sv);
    // 10/0 -> NaN per STAR spec (MDiv guards rhs==0)
    EXPECT_TRUE(std::isnan(sv[0]));
}


TEST(Modules, CrossSectionRankUsesTheCurrentUniverseOnly) {
    DataPanel panel;
    panel.T = 2;
    panel.N = 3;
    panel.universe_mask = {1,0,1, 1,1,1};
    MRank rank;
    rank.panel = &panel;
    rank.numAssets = 3;
    rank.init();
    std::vector<float> values = {1, 100, 3};
    SignalVector signal{values.data(), 3};
    rank.calculate(0, signal);
    EXPECT_FLOAT_EQ(values[0], 0.5f);
    EXPECT_TRUE(std::isnan(values[1]));
    EXPECT_FLOAT_EQ(values[2], 1.0f);
    values = {1,100,3};
    signal.data = values.data();
    rank.calculate(1, signal);
    EXPECT_FLOAT_EQ(values[0], 1.0f/3.0f);
    EXPECT_FLOAT_EQ(values[1], 1.0f);
    EXPECT_FLOAT_EQ(values[2], 2.0f/3.0f);
}

TEST(Modules, CrossSectionZscoreExcludesExitedSymbolBeforeComputingMoments) {
    DataPanel panel;
    panel.T = 2;
    panel.N = 3;
    panel.universe_mask = {1,0,1, 1,0,0};
    MZscore zscore;
    zscore.panel = &panel;
    zscore.numAssets = 3;
    zscore.init();
    std::vector<float> values = {1, 100, 3};
    SignalVector signal{values.data(), 3};
    zscore.calculate(0, signal);
    EXPECT_NEAR(values[0], -1.0/std::sqrt(2.0), 1e-6);
    EXPECT_TRUE(std::isnan(values[1]));
    EXPECT_NEAR(values[2], 1.0/std::sqrt(2.0), 1e-6);
    values = {1,100,3};
    signal.data = values.data();
    zscore.calculate(1, signal);
    for (float value : values) EXPECT_TRUE(std::isnan(value));
    EXPECT_EQ(panel.universe_mask, (std::vector<uint8_t>{1,0,1, 1,0,0}));
}

// ======== MNeutralize (cross-sectional group demean) ========

// Group codes live in the panel field named by `column_name`, one row per date.
static DataPanel make_group_panel(unsigned T, unsigned N, std::vector<float> codes) {
    DataPanel dp;
    dp.T = T;
    dp.N = N;
    dp.arrays["sector"] = std::move(codes);
    return dp;
}

static void init_neutralize(MNeutralize& mod, const DataPanel& panel, unsigned N) {
    mod.panel = &panel;
    mod.group = "sector";
    mod.column_name = "sector";
    mod.numAssets = N;
    mod.init();
}

TEST(MNeutralize, DemeansWithinEachGroup) {
    auto dp = make_group_panel(1, 5, {1, 1, 1, 2, 2});
    MNeutralize mod;
    init_neutralize(mod, dp, 5);

    std::vector<float> buf = {1.0f, 2.0f, 3.0f, 10.0f, 20.0f};
    SignalVector sv{buf.data(), 5};
    mod.calculate(0, sv);

    EXPECT_NEAR(buf[0], -1.0f, 1e-6f);
    EXPECT_NEAR(buf[1], 0.0f, 1e-6f);
    EXPECT_NEAR(buf[2], 1.0f, 1e-6f);
    EXPECT_NEAR(buf[3], -5.0f, 1e-6f);
    EXPECT_NEAR(buf[4], 5.0f, 1e-6f);
}

TEST(MNeutralize, UnclassifiedSymbolIsNaNAndExcludedFromTheMean) {
    auto dp = make_group_panel(1, 3, {1, 1, NaN});
    MNeutralize mod;
    init_neutralize(mod, dp, 3);

    std::vector<float> buf = {1.0f, 3.0f, 100.0f};
    SignalVector sv{buf.data(), 3};
    mod.calculate(0, sv);

    EXPECT_NEAR(buf[0], -1.0f, 1e-6f);
    EXPECT_NEAR(buf[1], 1.0f, 1e-6f);
    EXPECT_TRUE(std::isnan(buf[2]));
}

TEST(MNeutralize, NegativeGroupCodeIsUnclassified) {
    // Team `cs_group_neut` casts NaN codes to a negative int, so a finite
    // negative code means the same thing as a missing one.
    auto dp = make_group_panel(1, 3, {1, 1, -1});
    MNeutralize mod;
    init_neutralize(mod, dp, 3);

    std::vector<float> buf = {1.0f, 3.0f, 100.0f};
    SignalVector sv{buf.data(), 3};
    mod.calculate(0, sv);

    EXPECT_NEAR(buf[0], -1.0f, 1e-6f);
    EXPECT_NEAR(buf[1], 1.0f, 1e-6f);
    EXPECT_TRUE(std::isnan(buf[2]));
}

TEST(MNeutralize, NaNSignalStaysNaNAndLeavesTheMeanAlone) {
    auto dp = make_group_panel(1, 3, {1, 1, 1});
    MNeutralize mod;
    init_neutralize(mod, dp, 3);

    std::vector<float> buf = {1.0f, NaN, 3.0f};
    SignalVector sv{buf.data(), 3};
    mod.calculate(0, sv);

    EXPECT_NEAR(buf[0], -1.0f, 1e-6f);
    EXPECT_TRUE(std::isnan(buf[1]));
    EXPECT_NEAR(buf[2], 1.0f, 1e-6f);
}

TEST(MNeutralize, SingletonGroupBecomesZero) {
    auto dp = make_group_panel(1, 3, {2, 1, 1});
    MNeutralize mod;
    init_neutralize(mod, dp, 3);

    std::vector<float> buf = {7.0f, 1.0f, 3.0f};
    SignalVector sv{buf.data(), 3};
    mod.calculate(0, sv);

    EXPECT_NEAR(buf[0], 0.0f, 1e-6f);
    EXPECT_NEAR(buf[1], -1.0f, 1e-6f);
    EXPECT_NEAR(buf[2], 1.0f, 1e-6f);
}

TEST(MNeutralize, ExcludedUniverseSymbolDoesNotEnterTheGroupMean) {
    auto dp = make_group_panel(1, 3, {1, 1, 1});
    dp.universe_mask = {1, 1, 0};
    MNeutralize mod;
    init_neutralize(mod, dp, 3);

    std::vector<float> buf = {1.0f, 3.0f, 1000.0f};
    SignalVector sv{buf.data(), 3};
    mod.calculate(0, sv);

    EXPECT_NEAR(buf[0], -1.0f, 1e-6f);
    EXPECT_NEAR(buf[1], 1.0f, 1e-6f);
    EXPECT_TRUE(std::isnan(buf[2]));
    EXPECT_EQ(dp.universe_mask, (std::vector<uint8_t>{1, 1, 0}));
}

TEST(MNeutralize, AllNaNRowStaysNaN) {
    auto dp = make_group_panel(1, 2, {1, 1});
    MNeutralize mod;
    init_neutralize(mod, dp, 2);

    std::vector<float> buf = {NaN, NaN};
    SignalVector sv{buf.data(), 2};
    mod.calculate(0, sv);

    EXPECT_TRUE(std::isnan(buf[0]));
    EXPECT_TRUE(std::isnan(buf[1]));
}

TEST(MNeutralize, UsesTheGroupRowOfTheRequestedDate) {
    // Day 0: {A,B} | {C}.  Day 1: {A} | {B,C}.
    auto dp = make_group_panel(2, 3, {1, 1, 2,
                                      1, 2, 2});
    MNeutralize mod;
    init_neutralize(mod, dp, 3);

    std::vector<float> buf = {1.0f, 3.0f, 8.0f};
    SignalVector sv{buf.data(), 3};
    mod.calculate(0, sv);
    EXPECT_NEAR(buf[0], -1.0f, 1e-6f);
    EXPECT_NEAR(buf[1], 1.0f, 1e-6f);
    EXPECT_NEAR(buf[2], 0.0f, 1e-6f);

    buf = {1.0f, 3.0f, 8.0f};
    sv.data = buf.data();
    mod.calculate(1, sv);
    EXPECT_NEAR(buf[0], 0.0f, 1e-6f);
    EXPECT_NEAR(buf[1], -2.5f, 1e-6f);
    EXPECT_NEAR(buf[2], 2.5f, 1e-6f);
}

TEST(MNeutralize, EveryGroupSumsToZeroOnAWideRow) {
    constexpr unsigned N = 200;
    constexpr int kGroups = 11;
    std::mt19937 rng(20260916u);
    std::uniform_real_distribution<float> value_dist(-5.0f, 5.0f);

    std::vector<float> codes(N);
    for (unsigned n = 0; n < N; ++n)
        codes[n] = static_cast<float>(1 + static_cast<int>(n) % kGroups);
    auto dp = make_group_panel(1, N, codes);
    MNeutralize mod;
    init_neutralize(mod, dp, N);

    std::vector<float> buf(N);
    for (unsigned n = 0; n < N; ++n) buf[n] = value_dist(rng);
    SignalVector sv{buf.data(), N};
    mod.calculate(0, sv);

    std::vector<double> sums(kGroups, 0.0);
    std::vector<int> counts(kGroups, 0);
    for (unsigned n = 0; n < N; ++n) {
        ASSERT_TRUE(std::isfinite(buf[n])) << "n=" << n;
        const int g = static_cast<int>(codes[n]) - 1;
        sums[g] += buf[n];
        ++counts[g];
    }
    for (int g = 0; g < kGroups; ++g) {
        EXPECT_GT(counts[g], 1);
        EXPECT_LT(std::fabs(sums[g]), 1e-5) << "group " << (g + 1);
    }
}
