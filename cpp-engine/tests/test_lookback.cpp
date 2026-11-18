// Module::lookback against the modules' real output, and first_valid_bar over compiled pipelines.
// Modules run from di = first_bar, as computation will start at the history start.
#include <gtest/gtest.h>

#include <cmath>
#include <map>
#include <memory>
#include <random>
#include <string>
#include <vector>

#include "module.h"
#include "module_registration.h"
#include "signal_vector.h"
#include "simulation_data.h"
#include "xml_compiler.h"
#include "modules/TsEwma.h"

namespace {

constexpr unsigned kT = 400, kN = 12;
using Attribs = std::map<std::string, std::string>;

DataPanel make_panel() {
    DataPanel dp;
    dp.T = kT;
    dp.N = kN;
    dp.timestamps.resize(kT);
    for (unsigned t = 0; t < kT; ++t) dp.timestamps[t] = static_cast<int64_t>(t) * 86400000;
    dp.symbols.resize(kN);
    for (unsigned i = 0; i < kN; ++i) dp.symbols[i] = "S" + std::to_string(i);
    std::mt19937 rng(11);
    std::normal_distribution<float> noise(0.0f, 1.0f);
    auto& close = dp.arrays["close"];
    auto& sector = dp.arrays["sector"];
    close.resize(size_t(kT) * kN);
    sector.resize(size_t(kT) * kN);
    for (unsigned t = 0; t < kT; ++t)
        for (unsigned i = 0; i < kN; ++i) {
            close[size_t(t) * kN + i] = 2.0f + 0.2f * noise(rng);
            sector[size_t(t) * kN + i] = static_cast<float>(i % 2);
        }
    return dp;
}

std::vector<std::vector<float>> run_from(Module& m, const DataPanel& dp, unsigned first_bar) {
    m.numAssets = kN;
    m.init();
    std::vector<std::vector<float>> out(kT, std::vector<float>(kN, std::numeric_limits<float>::quiet_NaN()));
    const auto& close = dp.arrays.at("close");
    for (unsigned t = first_bar; t < kT; ++t) {
        out[t].assign(close.begin() + size_t(t) * kN, close.begin() + size_t(t + 1) * kN);
        SignalVector sv{out[t].data(), kN};
        m.calculate(t, sv);
    }
    return out;
}

// First bar with a finite output, kT if none.
unsigned first_finite_bar(const std::vector<std::vector<float>>& out) {
    for (unsigned t = 0; t < kT; ++t)
        for (float v : out[t])
            if (std::isfinite(v)) return t;
    return kT;
}

std::unique_ptr<Module> make(const std::string& tag, const Attribs& attribs, const DataPanel& dp) {
    return ModuleRegistry::default_registry().create(tag, attribs, &dp);
}

void expect_oracle(const std::string& tag, const Attribs& attribs, std::vector<unsigned> first_bars) {
    const DataPanel dp = make_panel();
    for (unsigned first_bar : first_bars) {
        auto m = make(tag, attribs, dp);
        const unsigned lookback = m->lookback(first_bar);
        ASSERT_LT(first_bar + lookback, kT) << tag;  // the fixture must reach the boundary
        EXPECT_EQ(first_finite_bar(run_from(*m, dp, first_bar)), first_bar + lookback)
            << tag << " first_bar=" << first_bar << " days=" << (attribs.count("days") ? attribs.at("days") : "");
    }
}

std::string days(unsigned d) { return std::to_string(d); }

// Decay with min_count below days emits finite values on a partly filled window (partial weights), so the
// boundary is where a run from first_bar first equals a run with longer history.
void expect_full_window_at_lookback(const std::string& tag, unsigned window, Attribs attribs = {}) {
    const DataPanel dp = make_panel();
    const unsigned first_bar = 50;
    attribs["days"] = days(window);
    auto late = make(tag, attribs, dp);
    auto early = make(tag, attribs, dp);
    EXPECT_EQ(late->lookback(first_bar), window - 1) << tag;
    const auto short_run = run_from(*late, dp, first_bar);
    const auto long_run = run_from(*early, dp, 0);
    const unsigned valid = first_bar + window - 1;
    EXPECT_GT(std::fabs(short_run[valid - 1][0] - long_run[valid - 1][0]), 1e-4f) << tag;
    for (unsigned t = valid; t < valid + 5; ++t)
        for (unsigned i = 0; i < kN; ++i)
            EXPECT_NEAR(short_run[t][i], long_run[t][i], 1e-2f) << tag << " " << t;
}

unsigned first_valid(const std::string& xml, const DataPanel& dp, unsigned start) {
    auto alphas = compile_xml(xml, &dp);
    EXPECT_EQ(alphas.size(), 1u);
    return first_valid_bar(alphas[0], start);
}

}  // namespace

TEST(Lookback, RollingModulesFullWindow) {
    for (const char* tag : {"tsmean", "decay", "tsrank", "tsstddev", "tssum", "tsargmax", "tsargmin", "tsmax", "tsmin", "tszscore"})
        for (unsigned d : {2u, 5u, 20u}) expect_oracle(tag, {{"days", days(d)}}, {0u, 13u});
}

TEST(Lookback, RollingLookbackIgnoresMinCount) {
    const DataPanel dp = make_panel();
    for (const char* tag : {"tsmean", "decay", "tsrank", "tsstddev", "tssum", "tsargmax", "tsargmin", "tsmax", "tsmin", "tszscore"})
        EXPECT_EQ(make(tag, {{"days", "20"}, {"min_count", "2"}}, dp)->lookback(0), 19u) << tag;
}

TEST(Lookback, DecayPartialWindowBoundaryIsTheFullWindow) {
    for (unsigned d : {2u, 5u, 20u}) expect_full_window_at_lookback("decay", d, {{"min_count", "1"}});
}

TEST(Lookback, DelayAndDelta) {
    for (const char* tag : {"delay", "tsdelta"})
        for (unsigned d : {1u, 3u, 30u}) expect_oracle(tag, {{"days", days(d)}}, {0u, 13u});
}

TEST(Lookback, Ffd) {
    for (const char* d : {"0.24", "0.5", "0.8"})
        for (const char* thres : {"0.01", "0.001"}) expect_oracle("ffd", {{"d", d}, {"thres", thres}}, {0u, 13u});
    const DataPanel dp = make_panel();
    EXPECT_GT(make("ffd", {{"d", "0.24"}, {"thres", "0.001"}}, dp)->lookback(0), 5u);
}

TEST(Lookback, TsHoldWaitsForTheNextRefresh) {
    for (unsigned d : {1u, 3u, 7u, 20u}) expect_oracle("tshold", {{"days", days(d)}}, {0u, 2u, 7u, 20u, 21u});
    const DataPanel dp = make_panel();
    auto m = make("tshold", {{"days", "7"}}, dp);
    EXPECT_EQ(m->lookback(0), 0u);
    EXPECT_EQ(m->lookback(7), 0u);
    EXPECT_EQ(m->lookback(8), 6u);
}

// tsfill outputs finite values from its first bar, so its lookback is where a run from first_bar equals a run
// from bar 0: a value finite at first_bar - 1 is held through first_bar - 1 + days, which a late run cannot see.
TEST(Lookback, TsFillMatchesLongerHistoryAfterDays) {
    const DataPanel dp = make_panel();
    const unsigned first_bar = 50;
    for (unsigned d : {1u, 3u, 7u, 20u}) {
        auto late = make("tsfill", {{"days", days(d)}}, dp);
        auto early = make("tsfill", {{"days", days(d)}}, dp);
        EXPECT_EQ(late->lookback(first_bar), d);
        EXPECT_EQ(late->lookback(0), d);
        // Instrument 0 is finite at first_bar - 1 and NaN from first_bar for 2*d bars; the others are finite.
        std::vector<std::vector<float>> in(kT, std::vector<float>(kN));
        const auto& close = dp.arrays.at("close");
        for (unsigned t = 0; t < kT; ++t)
            for (unsigned i = 0; i < kN; ++i) {
                in[t][i] = close[size_t(t) * kN + i];
                if (i == 0 && t >= first_bar && t < first_bar + 2 * d) in[t][i] = std::numeric_limits<float>::quiet_NaN();
            }
        auto run = [&](Module& m, unsigned from) {
            m.numAssets = kN;
            m.init();
            std::vector<std::vector<float>> out(kT, std::vector<float>(kN, std::numeric_limits<float>::quiet_NaN()));
            for (unsigned t = from; t < kT; ++t) {
                out[t] = in[t];
                SignalVector sv{out[t].data(), kN};
                m.calculate(t, sv);
            }
            return out;
        };
        const auto short_run = run(*late, first_bar);
        const auto long_run = run(*early, 0);
        const unsigned valid = first_bar + d;
        EXPECT_TRUE(std::isnan(short_run[valid - 1][0])) << d;
        EXPECT_TRUE(std::isfinite(long_run[valid - 1][0])) << d;
        for (unsigned t = valid; t < kT; ++t)
            for (unsigned i = 0; i < kN; ++i) {
                EXPECT_EQ(std::isnan(short_run[t][i]), std::isnan(long_run[t][i])) << d << " " << t << " " << i;
                if (!std::isnan(long_run[t][i])) EXPECT_EQ(short_run[t][i], long_run[t][i]) << d << " " << t;
            }
    }
}

TEST(Lookback, PcaResidRoundsUpToARefitBar) {
    for (const auto& [train, refit] : std::vector<std::pair<unsigned, unsigned>>{{20, 1}, {20, 7}, {30, 10}, {25, 25}})
        expect_oracle("pcaresid", {{"train", days(train)}, {"refit", days(refit)}, {"k", "1"}}, {0u, 3u, 7u, 30u});
    const DataPanel dp = make_panel();
    auto m = make("pcaresid", {{"train", "20"}, {"refit", "7"}}, dp);
    EXPECT_EQ(m->lookback(9), 26u);  // 29 -> 35
    EXPECT_EQ(m->lookback(1), 20u);  // 21 is a refit bar
}

TEST(Lookback, StatelessAndPathDependentModulesAreZero) {
    const std::vector<std::pair<std::string, Attribs>> cases = {
        {"rank", {}}, {"zscore", {}}, {"neutralize", {{"group", "sector"}}}, {"rank_sharp", {}}, {"winsorize", {}},
        {"normalize", {}}, {"truncate", {{"max", "0.5"}}}, {"hump", {}}, {"affine", {}}, {"abs", {}},
        {"log", {}}, {"exp", {}}, {"sqrt", {}}, {"sign", {}}, {"inverse", {}}, {"reverse", {}},
        {"power", {{"exp", "2"}}}, {"signedpower", {{"exp", "2"}}}, {"max", {{"value", "1"}}}, {"min", {{"value", "1"}}},
        {"group_rank", {{"group", "sector"}}}, {"group_mean", {{"group", "sector"}}},
        {"group_zscore", {{"group", "sector"}}},
    };
    for (const auto& [tag, attribs] : cases) expect_oracle(tag, attribs, {0u, 13u});
}

TEST(Lookback, TsEwmaFormula) {
    const DataPanel dp = make_panel();
    EXPECT_EQ(make("tsewma", {{"span", "20"}}, dp)->lookback(0), 69u);
    EXPECT_EQ(make("tsewma", {{"span", "20"}, {"min_count", "50"}}, dp)->lookback(0), 69u);
    EXPECT_EQ(make("tsewma", {{"alpha", "0.5"}}, dp)->lookback(0), 9u);     // 0.5^10 < 0.001 < 0.5^9
    EXPECT_EQ(make("tsewma", {{"halflife", "10"}}, dp)->lookback(0), 99u);  // 2^(-100/10) < 0.001 < 2^(-99/10)
    MTsEwma m;
    m.alpha = FloatParameter(1.0f);
    EXPECT_EQ(m.lookback(0), 0u);
    EXPECT_EQ(make("tsewma", {{"alpha", "0.00000000000000001"}}, dp)->lookback(0), kNeverValidBar);
    EXPECT_EQ(make("tsewma", {{"halflife", "1000000000000000000000000000000"}}, dp)->lookback(0), kNeverValidBar);
    // Sums saturate instead of wrapping.
    const std::string never = "<tsewma alpha=\"0.00000000000000001\"/>";
    EXPECT_EQ(first_valid("<data expr=\"close\"/>" + never + never + never, dp, 10), kNeverValidBar);
}

TEST(FirstValidBar, ChainAddsAndUsesTheRunningBar) {
    const DataPanel dp = make_panel();
    const std::string d = "<data expr=\"close\"/>";
    EXPECT_EQ(first_valid(d, dp, 9), 9u);
    EXPECT_EQ(first_valid("<data expr=\"close\" delay=\"1\"/><tsmean days=\"5\"/><rank/>", dp, 9), 14u);
    // tshold is aligned to the bar its input becomes valid: 9 + 4 = 13 -> 14.
    EXPECT_EQ(first_valid(d + "<tsmean days=\"5\"/><tshold days=\"7\"/>", dp, 9), 14u);
    // tsfill adds its days: 9 + 4 + 7.
    EXPECT_EQ(first_valid(d + "<tsmean days=\"5\"/><tsfill days=\"7\"/>", dp, 9), 20u);
    EXPECT_EQ(first_valid(d + "<pcaresid train=\"20\" refit=\"7\"/>", dp, 9), 35u);
}

TEST(FirstValidBar, DataRestartBeginsANewChainAndTheMaximumWins) {
    const DataPanel dp = make_panel();
    const std::string restart = "<data expr=\"close\"/>";
    EXPECT_EQ(first_valid(restart + "<tsmean days=\"5\"/>" + restart + "<tsmean days=\"3\"/>", dp, 10), 14u);
    EXPECT_EQ(first_valid(restart + "<tsmean days=\"3\"/>" + restart + "<tsmean days=\"5\"/>", dp, 10), 14u);
    // Without the restart the two windows would add.
    EXPECT_EQ(first_valid(restart + "<tsmean days=\"3\"/><tsmean days=\"5\"/>", dp, 10), 16u);
}

TEST(FirstValidBar, BinaryExprTakesTheLongerOperand) {
    const DataPanel dp = make_panel();  // no returns field: returns = close / Delay(close, 1) - 1.0
    EXPECT_EQ(first_valid("<data expr=\"close + close\"/>", dp, 10), 10u);
    EXPECT_EQ(first_valid("<data expr=\"close - returns\"/>", dp, 10), 11u);
    EXPECT_EQ(first_valid("<data expr=\"abs(returns) + 1\" delay=\"2\"/>", dp, 10), 13u);
}

TEST(FirstValidBar, SavedSignalsKeepTheirBar) {
    const DataPanel dp = make_panel();
    const std::string a = "<data expr=\"close\"/><tsmean days=\"100\"/><signal id=\"a\"/>";
    // A load resumes from the saved bar, not from the current chain.
    EXPECT_EQ(first_valid(a + "<data expr=\"close\"/><tsmean days=\"5\" input=\"a\"/>", dp, 0), 103u);
    // <event> outputs a saved signal: later modules count from the maximum so far.
    EXPECT_EQ(first_valid(a + "<data expr=\"close\"/><signal id=\"b\"/><event signal=\"a\" trigger=\"b\"/>"
                              "<tsmean days=\"5\"/>", dp, 0), 103u);
}

TEST(FirstValidBar, MetaIsTheLongestMemberPlusTheEstimationWindow) {
    const DataPanel dp = make_panel();
    const MetaResolver resolver = [](const std::string&) {
        return std::vector<std::pair<std::string, std::string>>{
            {"a.xml", "<data expr=\"close\" delay=\"1\"/><rank/>"},
            {"b.xml", "<data expr=\"close\" delay=\"1\"/><tsmean days=\"5\"/>"}};
    };
    auto first = [&](const std::string& xml, unsigned start) {
        auto alphas = compile_xml(xml, &dp, nullptr, &resolver);
        EXPECT_EQ(alphas.size(), 1u);
        return first_valid_bar(alphas[0], start);
    };
    EXPECT_EQ(first("<meta expr=\"s\"/>", 10), 15u);
    EXPECT_EQ(first("<meta expr=\"s\"/><ew/>", 10), 15u);
    // meta.cpp: first estimate at sim_start + days, one bar after the history start plus days.
    EXPECT_EQ(first("<meta expr=\"s\"/><mvo days=\"40\"/>", 10), 56u);
    EXPECT_EQ(first("<meta expr=\"s\"/><rank/><tsmean days=\"3\"/>", 10), 17u);
    // Members are walked at the running bar.
    const MetaResolver fill = [](const std::string&) {
        return std::vector<std::pair<std::string, std::string>>{{"f.xml", "<data expr=\"close\"/><tshold days=\"7\"/>"}};
    };
    auto alphas = compile_xml("<meta expr=\"s\"/>", &dp, nullptr, &fill);
    EXPECT_EQ(first_valid_bar(alphas[0], 8), 14u);
    EXPECT_EQ(first_valid_bar(alphas[0], 14), 14u);
}
