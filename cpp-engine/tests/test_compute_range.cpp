// Computation starts at history_start: modules and the portfolio never see an earlier bar.
#include <gtest/gtest.h>

#include <atomic>
#include <cmath>
#include <limits>
#include <map>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include "meta.h"
#include "module.h"
#include "modules/MetaSource.h"
#include "simulation_data.h"
#include "simulator_engine.h"
#include "xml_compiler.h"

namespace {

constexpr unsigned kT = 420, kN = 12;
constexpr float kNaNf = std::numeric_limits<float>::quiet_NaN();
constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();
constexpr double kFee = 0.001;

DataPanel make_panel() {
    DataPanel dp;
    dp.T = kT;
    dp.N = kN;
    dp.timestamps.resize(kT);
    for (unsigned t = 0; t < kT; ++t) dp.timestamps[t] = static_cast<int64_t>(t) * 86400000;
    for (unsigned i = 0; i < kN; ++i) dp.symbols.push_back("S" + std::to_string(i));
    std::mt19937 rng(11);
    std::normal_distribution<float> noise(0.0f, 1.0f);
    auto& close = dp.arrays["close"];
    auto& sector = dp.arrays["sector"];
    close.resize(size_t(kT) * kN);
    sector.resize(size_t(kT) * kN);
    for (unsigned t = 0; t < kT; ++t)
        for (unsigned i = 0; i < kN; ++i) {
            close[size_t(t) * kN + i] = std::exp(2.0f + 0.2f * noise(rng));
            sector[size_t(t) * kN + i] = static_cast<float>(i % 2);
        }
    return dp;
}

const std::string kClose = "<data expr=\"close\"/>";

struct AlphaRun {
    SimulatorOutput out;
    std::vector<std::vector<double>> weights;
    unsigned first_valid = 0;   // first_valid_bar at the run's history start
};

AlphaRun run_xml(const DataPanel& dp, const std::string& xml, unsigned sim_start) {
    auto alphas = compile_xml(xml, &dp);
    EXPECT_EQ(alphas.size(), 1u) << xml;
    SimulatorConfig config;
    config.T = dp.T; config.N = dp.N; config.fee_rate = kFee;
    config.sim_start_index = sim_start;
    config.collect_signals = true;
    AlphaRun run;
    run.first_valid = first_valid_bar(alphas[0], config.history_start());
    run.weights.assign(dp.T, {});
    config.accounting_row_callback = [&](const AccountingRowView& row) { run.weights[row.bar] = row.weights; };
    run.out = Simulator(dp, config).run(alphas[0].pipeline);
    EXPECT_EQ(run.out.signals.size(), dp.T);
    EXPECT_EQ(run.out.ret.size(), dp.T);
    return run;
}

unsigned first_finite(const std::vector<std::vector<float>>& signals) {
    for (unsigned t = 0; t < signals.size(); ++t)
        for (float v : signals[t])
            if (std::isfinite(v)) return t;
    return static_cast<unsigned>(signals.size());
}

// Records every bar it is called for.
struct Probe : Module {
    std::vector<unsigned>* bars = nullptr;
    void init() override {}
    void calculate(unsigned di, SignalVector& output) override {
        bars->push_back(di);
        for (unsigned ii = 0; ii < numAssets; ++ii) output[ii] = 1.0f + static_cast<float>(ii);
    }
};

SimulatorConfig plain_config(const DataPanel& dp, unsigned sim_start) {
    SimulatorConfig config;
    config.T = dp.T; config.N = dp.N; config.fee_rate = kFee;
    config.sim_start_index = sim_start;
    return config;
}

struct Thin {
    DataPanel panel;
    std::vector<std::vector<float>> signals;
    Thin(unsigned T, unsigned N) {
        panel.T = T; panel.N = N;
        panel.timestamps.resize(T);
        for (unsigned t = 0; t < T; ++t) panel.timestamps[t] = static_cast<int64_t>(t) * 86400000;
        for (unsigned ii = 0; ii < N; ++ii) panel.symbols.push_back("S" + std::to_string(ii));
        panel.arrays["close"].assign(size_t(T) * N, 1.0f);
        panel.thin_layout = true;
        panel.returns_total.assign(size_t(T) * N, 0.0);
        signals.assign(T, std::vector<float>(N, 1.0f));
    }
    void move(unsigned bar, unsigned source, int32_t destination, double ratio, bool ends) {
        DataPanel::MoveEvent event;
        event.bar = bar; event.source = source; event.destination = destination;
        event.value_ratio = ratio; event.source_ends = ends;
        panel.move_events.push_back(event);
    }
    std::vector<std::vector<double>> weights;
    SimulatorOutput run(unsigned sim_start) {
        SimulatorConfig config = plain_config(panel, sim_start);
        weights.assign(panel.T, {});
        config.accounting_row_callback = [this](const AccountingRowView& row) { weights[row.bar] = row.weights; };
        return Simulator(panel, config).run_from_signals(signals);
    }
};

}  // namespace

TEST(ComputeRange, HistoryStartIsOneBarBeforeSimStart) {
    SimulatorConfig config;
    config.sim_start_index = 0; EXPECT_EQ(config.history_start(), 0u);
    config.sim_start_index = 1; EXPECT_EQ(config.history_start(), 0u);
    config.sim_start_index = 37; EXPECT_EQ(config.history_start(), 36u);
}

// Modules whose first finite output is the lookback boundary.
TEST(ComputeRange, FirstFiniteSignalIsHistoryStartPlusLookback) {
    const DataPanel dp = make_panel();
    const std::vector<std::string> ops = {
        "<tsmean days=\"5\"/>", "<tssum days=\"20\"/>", "<tsstddev days=\"7\"/>", "<tsrank days=\"9\"/>",
        "<tsdelta days=\"3\"/>", "<delay days=\"4\"/>", "<tsmax days=\"6\"/>", "<tsmin days=\"6\"/>",
        "<tsargmax days=\"8\"/>", "<tsargmin days=\"8\"/>", "<tsfill days=\"10\"/>", "<tsfill days=\"23\"/>",
        "<ffd d=\"0.5\" thres=\"0.01\"/>", "<tsewma span=\"5\" min_count=\"18\"/>",
        "<pcaresid train=\"100\" refit=\"50\" k=\"2\"/>", "<pcaresid group=\"sector\" train=\"60\" refit=\"33\" k=\"1\"/>",
        "<tsmean days=\"5\"/><rank/><tsdelta days=\"2\"/>", "<tsmean days=\"3\"/><tsfill days=\"7\"/><zscore/>",
        "<ffd d=\"0.5\" thres=\"0.01\"/><pcaresid train=\"40\" refit=\"17\" k=\"1\"/><rank/>"};
    for (const auto& op : ops)
        for (unsigned sim_start : {37u, 101u, 128u}) {
            const AlphaRun run = run_xml(dp, kClose + op, sim_start);
            const unsigned history = sim_start - 1;
            ASSERT_LT(run.first_valid, kT) << op;
            EXPECT_EQ(first_finite(run.out.signals), run.first_valid) << op << " simStart=" << sim_start;
            for (unsigned t = 0; t < history; ++t) EXPECT_TRUE(run.out.signals[t].empty()) << op << " t=" << t;
            EXPECT_EQ(run.out.signals[history].size(), kN) << op;
        }
    // Hand-computed boundaries, independent of the lookback code.
    EXPECT_EQ(first_finite(run_xml(dp, kClose + "<tsmean days=\"5\"/>", 37).out.signals), 40u);       // 36 + 4
    EXPECT_EQ(first_finite(run_xml(dp, kClose + "<delay days=\"4\"/>", 37).out.signals), 40u);        // 36 + 4
    EXPECT_EQ(first_finite(run_xml(dp, kClose + "<tsfill days=\"10\"/>", 37).out.signals), 40u);      // next multiple of 10
    EXPECT_EQ(first_finite(run_xml(dp, kClose + "<tsfill days=\"10\"/>", 41).out.signals), 40u);      // 40 is itself a sample bar
    EXPECT_EQ(first_finite(run_xml(dp, kClose + "<pcaresid train=\"100\" refit=\"50\" k=\"2\"/>", 37).out.signals),
              150u);  // 36 + 100 = 136, next multiple of 50
    EXPECT_EQ(first_finite(run_xml(dp, kClose + "<pcaresid train=\"100\" refit=\"50\" k=\"2\"/>", 51).out.signals),
              150u);  // exactly 100 rows pushed before bar 150
    EXPECT_EQ(first_finite(run_xml(dp, kClose + "<pcaresid train=\"100\" refit=\"50\" k=\"2\"/>", 52).out.signals),
              200u);  // only 99 rows before bar 150
}

// Decay and tszscore emit finite partial values: the boundary is where they match a longer history.
TEST(ComputeRange, PartialWindowModulesMatchLongerHistoryAtLookback) {
    const DataPanel dp = make_panel();
    for (const std::string op : {"<decay days=\"5\"/>", "<tszscore days=\"5\"/>"}) {
        const AlphaRun late = run_xml(dp, kClose + op, 51);
        const AlphaRun early = run_xml(dp, kClose + op, 1);
        EXPECT_EQ(late.first_valid, 54u) << op;
        for (unsigned ii = 0; ii < kN; ++ii)
            EXPECT_NEAR(late.out.signals[54][ii], early.out.signals[54][ii], 1e-4f) << op;
    }
}

TEST(ComputeRange, LaterSimStartMatchesEarlierAfterWarmup) {
    const DataPanel dp = make_panel();
    const std::vector<std::string> ops = {
        "<tsmean days=\"5\"/>", "<tssum days=\"20\"/>", "<tsstddev days=\"7\"/>", "<tsrank days=\"9\"/>",
        "<tsdelta days=\"3\"/>", "<delay days=\"4\"/>", "<tsmax days=\"6\"/>", "<tsargmin days=\"8\"/>",
        "<decay days=\"5\"/>", "<tszscore days=\"5\"/>",
        "<tsfill days=\"7\"/>", "<ffd d=\"0.5\" thres=\"0.01\"/>",
        // Fits coincide: from 59 the fits at 120, 150, ... and from 94 the fits at 150, ...
        "<pcaresid train=\"40\" refit=\"30\" k=\"2\"/>",
        "<tsmean days=\"5\"/><rank/><tsdelta days=\"2\"/>", "<delay days=\"1\"/><tsmean days=\"5\"/><rank/>"};
    for (const auto& op : ops) {
        const AlphaRun early = run_xml(dp, kClose + op, 60);
        const AlphaRun late = run_xml(dp, kClose + op, 95);
        const unsigned first = std::max(early.first_valid, late.first_valid);
        ASSERT_LT(first, kT) << op;
        for (unsigned t = first; t < kT; ++t) {
            for (unsigned ii = 0; ii < kN; ++ii) {
                const float a = early.out.signals[t][ii], b = late.out.signals[t][ii];
                ASSERT_EQ(std::isfinite(a), std::isfinite(b)) << op << " t=" << t;
                if (std::isfinite(a)) ASSERT_NEAR(a, b, 1e-5f * std::max(1.0f, std::fabs(a))) << op << " t=" << t;
            }
            if (t >= 95)
                for (unsigned ii = 0; ii < kN; ++ii)
                    ASSERT_NEAR(early.weights[t][ii], late.weights[t][ii], 1e-5) << op << " t=" << t;
            if (t > std::max(first, 95u)) {  // both books held the same weights on the previous bar
                ASSERT_NEAR(early.out.gross_ret[t], late.out.gross_ret[t], 1e-5) << op << " t=" << t;
                ASSERT_NEAR(early.out.ret[t], late.out.ret[t], 1e-5) << op << " t=" << t;
            }
        }
    }
}

// NaN holes straddling the later history start (bar 94) and universe holes, read through rank.
TEST(ComputeRange, IndependenceWithNaNAndMaskHoles) {
    DataPanel nan_panel = make_panel();
    auto hole = [&](unsigned ii, unsigned from, unsigned to) {
        for (unsigned t = from; t <= to; ++t) nan_panel.arrays["close"][size_t(t) * kN + ii] = kNaNf;
    };
    hole(3, 80, 105);
    hole(5, 94, 97);
    hole(7, 0, 120);
    DataPanel mask_panel = make_panel();
    mask_panel.universe_mask.assign(size_t(kT) * kN, 1);
    for (unsigned t = 85; t <= 100; ++t) mask_panel.universe_mask[size_t(t) * kN + 2] = 0;
    mask_panel.universe_mask[size_t(94) * kN + 9] = 0;

    const std::vector<std::string> rolling = {
        "<tsmean days=\"5\"/>", "<tssum days=\"20\"/>", "<tsstddev days=\"7\"/>", "<tsrank days=\"9\"/>",
        "<tsdelta days=\"3\"/>", "<delay days=\"4\"/>", "<tsmax days=\"6\"/>", "<tsargmin days=\"8\"/>",
        "<decay days=\"5\"/>", "<tszscore days=\"5\"/>", "<tsfill days=\"7\"/>",
        "<pcaresid train=\"40\" refit=\"30\" k=\"2\"/>"};
    const auto compare = [](const DataPanel& dp, const std::string& xml, const std::map<unsigned, unsigned>& restart,
                            unsigned carve_bars) -> unsigned {
        const AlphaRun early = run_xml(dp, xml, 60), late = run_xml(dp, xml, 95);
        const unsigned first = std::max(early.first_valid, late.first_valid);
        unsigned carved = 0;
        for (unsigned t = first; t < kT; ++t)
            for (unsigned ii = 0; ii < kN; ++ii) {
                const float a = early.out.signals[t][ii], b = late.out.signals[t][ii];
                const auto found = restart.find(ii);
                if (found != restart.end() && t >= found->second && t < found->second + carve_bars &&
                    std::isfinite(a) && std::isnan(b)) { ++carved; continue; }
                EXPECT_EQ(std::isfinite(a), std::isfinite(b)) << xml << " t=" << t << " ii=" << ii;
                if (std::isfinite(a)) EXPECT_NEAR(a, b, 1e-5f * std::max(1.0f, std::fabs(a))) << xml << " t=" << t;
            }
        return carved;
    };
    const std::map<unsigned, unsigned> none;
    for (const auto& op : rolling) {
        EXPECT_EQ(compare(nan_panel, kClose + op, none, 0), 0u) << op;
        EXPECT_EQ(compare(mask_panel, kClose + "<rank/>" + op, none, 0), 0u) << op;
    }
    // The ffd carve-out: only names finite before bar 94 and restarting after it, only for L-1 bars.
    const std::string ffd = "<ffd d=\"0.5\" thres=\"0.01\"/>";
    auto probe = compile_xml(kClose + ffd, &nan_panel);
    const unsigned bars = probe[0].pipeline.back()->lookback(0);  // L - 1
    ASSERT_GT(bars, 0u);
    EXPECT_GT(compare(nan_panel, kClose + ffd, {{3, 106}, {5, 98}}, bars), 0u);
    EXPECT_GT(compare(mask_panel, kClose + "<rank/>" + ffd, {{2, 101}, {9, 95}}, bars), 0u);
}

TEST(ComputeRange, RetirementsCountOnlyTradingBars) {
    Thin thin(10, 3);
    thin.move(5, 0, -1, kNaN, true);    // the history start of simStart 6
    thin.move(7, 1, -1, kNaN, true);
    const auto out = thin.run(6);
    EXPECT_EQ(out.missing_data[5].retirement_count, 0u);
    EXPECT_EQ(out.missing_data[7].retirement_count, 1u);
}

TEST(ComputeRange, NonEwMetaIsValidOnTheFirstGridBarWithARealWindow) {
    const DataPanel dp = make_panel();
    const MetaResolver resolver = [](const std::string&) {
        return std::vector<std::pair<std::string, std::string>>{
            {"a.xml", "<data expr=\"close\"/><rank/>"},
            {"b.xml", "<data expr=\"close\"/><tsmean days=\"5\"/>"}};
    };
    const MetaResolver flat = [](const std::string&) {
        return std::vector<std::pair<std::string, std::string>>{{"a.xml", "<data expr=\"close\"/><rank/>"}};
    };
    const auto first = [&](const MetaResolver& members, const std::string& spec, unsigned start) {
        auto alphas = compile_xml("<meta expr=\"s\"/>" + spec, &dp, nullptr, &members);
        return first_valid_bar(alphas.at(0), start);
    };
    // simStart 77, lookback 4: earliest window start 81, grid 97 + k * rebalance.
    EXPECT_EQ(first(resolver, "<mvo days=\"20\"/>", 76), 101u);                  // rebalance 1: unchanged
    EXPECT_EQ(first(resolver, "<mvo days=\"20\" rebalance=\"30\"/>", 76), 127u);
    EXPECT_EQ(first(resolver, "<mvo days=\"20\" rebalance=\"4\"/>", 76), 101u);  // the grid hits it exactly
    EXPECT_EQ(first(flat, "<mvo days=\"20\"/>", 76), 97u);                       // lookback 0
    EXPECT_EQ(first(flat, "<mvo days=\"20\" rebalance=\"30\"/>", 76), 97u);
    // History start 0 must hold for simStart 1 (grid 21 + 4k) and simStart 0 (grid 20 + 4k).
    EXPECT_EQ(first(resolver, "<mvo days=\"20\"/>", 0), 25u);
    EXPECT_EQ(first(resolver, "<mvo days=\"20\" rebalance=\"4\"/>", 0), 28u);
    EXPECT_EQ(first(flat, "<mvo days=\"20\" rebalance=\"4\"/>", 0), 21u);

    // At the accepted bar the weights equal those from members warm since earlier, on the same grid.
    auto alphas = compile_xml("<meta expr=\"s\"/><mvo days=\"20\" rebalance=\"30\"/>", &dp, nullptr, &resolver);
    const auto& set = alphas.at(0).meta_sets.at(0);
    const auto returns = [&](unsigned sim_start) {
        MetaRunConfig run;
        run.member_config = plain_config(dp, sim_start);
        MetaSetCache cache(1);
        return collect_member_returns(dp, set, run, [] { return false; }, &cache.budget());
    };
    const MemberReturns cold = returns(77);
    MemberReturns warm = returns(40);
    warm.sim_start = 77;
    for (unsigned t = 0; t < 77; ++t) warm.rows[t].assign(warm.K, kNaN);
    const auto cold_weights = compute_combine_weights(cold, set.combine);
    const auto warm_weights = compute_combine_weights(warm, set.combine);
    EXPECT_GT(std::fabs(cold_weights.rows[126][1] - warm_weights.rows[126][1]), 1e-6);  // zeros in the window of 97
    for (unsigned t = 127; t < kT; ++t)
        for (unsigned k = 0; k < cold.K; ++k) ASSERT_NEAR(cold_weights.rows[t][k], warm_weights.rows[t][k], 1e-9) << t;
}

TEST(ComputeRange, ModulesAreNotCalledBeforeHistoryStart) {
    const DataPanel dp = make_panel();
    const auto expected_bars = [](unsigned history, unsigned end) {
        std::vector<unsigned> bars;
        for (unsigned t = history; t < end; ++t) bars.push_back(t);
        return bars;
    };
    for (unsigned sim_start : {0u, 1u, 2u, 77u}) {
        const unsigned history = sim_start > 0 ? sim_start - 1 : 0;
        std::vector<unsigned> bars;
        Probe probe; probe.bars = &bars;
        std::vector<Module*> pipeline{&probe};
        const auto output = Simulator(dp, plain_config(dp, sim_start)).run(pipeline);
        EXPECT_EQ(bars, expected_bars(history, kT)) << sim_start;
        EXPECT_EQ(output.ret.size(), kT);

        bars.clear();
        std::vector<unsigned> rows;
        Simulator(dp, plain_config(dp, sim_start)).run_signal_rows(
            pipeline, [&](unsigned di, const float*, unsigned) { rows.push_back(di); });
        EXPECT_EQ(bars, expected_bars(history, kT)) << sim_start;
        EXPECT_EQ(rows, bars);
    }
    std::vector<unsigned> bars;
    Probe probe; probe.bars = &bars;
    auto ended = plain_config(dp, 77);
    ended.sim_end_index = 90;
    std::vector<Module*> pipeline{&probe};
    EXPECT_EQ(Simulator(dp, ended).run(pipeline).ret.size(), 90u);
    EXPECT_EQ(bars, expected_bars(76, 90));
}

TEST(ComputeRange, FirstEntryAndFeeAreOnSimStart) {
    const DataPanel dp = make_panel();
    const unsigned sim_start = 77;
    const auto check = [&](const SimulatorOutput& out) {
        for (unsigned t = 0; t < sim_start; ++t) {
            EXPECT_EQ(out.turnover[t], 0.0) << t;
            EXPECT_EQ(out.ret[t], 0.0) << t;
            EXPECT_EQ(out.position_count[t], 0) << t;
        }
        EXPECT_NEAR(out.turnover[sim_start], 1.0, 1e-12);       // full book entered from an unfunded book
        EXPECT_EQ(out.gross_ret[sim_start], 0.0);               // bought at the close
        EXPECT_NEAR(out.ret[sim_start], -kFee, 1e-12);          // first fee on the same bar
        EXPECT_EQ(out.position_count[sim_start], static_cast<int>(kN));
    };
    std::vector<unsigned> bars;
    Probe probe; probe.bars = &bars;
    std::vector<Module*> pipeline{&probe};
    check(Simulator(dp, plain_config(dp, sim_start)).run(pipeline));

    std::vector<std::vector<float>> signals(kT);
    for (unsigned t = sim_start - 1; t < kT; ++t) signals[t].assign(kN, 1.0f);
    check(Simulator(dp, plain_config(dp, sim_start)).run_from_signals(signals));
}

TEST(ComputeRange, RunFromSignalsNeedsRowsOnlyFromHistoryStart) {
    const DataPanel dp = make_panel();
    std::vector<std::vector<float>> signals(kT);
    for (unsigned t = 76; t < kT; ++t) signals[t].assign(kN, 1.0f);
    EXPECT_NO_THROW(Simulator(dp, plain_config(dp, 77)).run_from_signals(signals));
    EXPECT_THROW(Simulator(dp, plain_config(dp, 76)).run_from_signals(signals), std::invalid_argument);
    signals.pop_back();
    EXPECT_THROW(Simulator(dp, plain_config(dp, 77)).run_from_signals(signals), std::invalid_argument);
}

// A quote missing at history_start and observed at simStart: no invented price, no throw.
TEST(ComputeRange, MissingQuoteAtHistoryStartEntersAtTheObservedSimStartClose) {
    DataPanel dp;
    dp.T = 6; dp.N = 2;
    dp.timestamps = {0, 1, 2, 3, 4, 5};
    dp.symbols = {"A", "B"};
    dp.arrays["close"] = {kNaNf, kNaNf,   // bar 0, never touched
                          100.f, 50.f,    // bar 1
                          kNaNf, 50.f,    // history start: A has no quote
                          120.f, 50.f,    // simStart
                          132.f, 50.f,
                          132.f, 50.f};
    std::vector<std::vector<float>> signals(6, std::vector<float>{1.0f, 1.0f});
    for (MissingClosePolicy policy : {MissingClosePolicy::Carry, MissingClosePolicy::Error}) {
        auto config = plain_config(dp, 3);
        config.missing_close_policy = policy;
        const auto out = Simulator(dp, config).run_from_signals(signals);
        EXPECT_NEAR(out.turnover[3], 1.0, 1e-12);
        EXPECT_EQ(out.gross_ret[3], 0.0);   // nothing held into simStart, so the bar 1 price is unused
        EXPECT_NEAR(out.gross_ret[4], 0.5 * (132.0 / 120.0 - 1.0), 1e-12);  // marked at the simStart close
    }
}

// A quote missing on both bars: the entry is blocked, then entered at the first observed close.
TEST(ComputeRange, MissingQuoteThroughSimStartEntersLater) {
    DataPanel dp;
    dp.T = 6; dp.N = 2;
    dp.timestamps = {0, 1, 2, 3, 4, 5};
    dp.symbols = {"A", "B"};
    dp.arrays["close"] = {100.f, 50.f, kNaNf, 50.f, kNaNf, 50.f, 120.f, 50.f, 132.f, 50.f, 132.f, 50.f};
    std::vector<std::vector<float>> signals(6, std::vector<float>{1.0f, 1.0f});
    const auto out = Simulator(dp, plain_config(dp, 2)).run_from_signals(signals);
    EXPECT_EQ(out.missing_data[2].missing_entry_count, 1u);
    EXPECT_EQ(out.position_count[2], 1);
    EXPECT_EQ(out.position_count[3], 2);
    EXPECT_EQ(out.gross_ret[3], 0.0);   // A's first holding return starts from bar 3's close
    EXPECT_NEAR(out.gross_ret[4], 0.5 * (132.0 / 120.0 - 1.0), 1e-12);
}

TEST(ComputeRange, ThinMissingQuoteAtHistoryStartHasNoInventedReturn) {
    Thin thin(6, 2);
    thin.panel.arrays["close"][2 * 2 + 0] = kNaNf;
    thin.panel.returns_total[3 * 2 + 0] = 0.5;   // spans the missing bar, before anything is held
    thin.panel.returns_total[4 * 2 + 0] = 0.1;
    const auto out = thin.run(3);
    EXPECT_NEAR(out.turnover[3], 1.0, 1e-12);
    EXPECT_EQ(out.gross_ret[3], 0.0);
    EXPECT_NEAR(out.gross_ret[4], 0.5 * 0.1, 1e-12);
}

// Sources that ended before computation starts stay retired without replaying those bars.
TEST(ComputeRange, ThinSourceEndedBeforeHistoryStartStaysRetired) {
    for (unsigned end_bar : {1u, 4u, 5u}) {  // 5 is the history start of simStart 6
        Thin thin(10, 3);
        thin.move(end_bar, 0, -1, kNaN, true);
        const auto out = thin.run(6);
        ASSERT_EQ(thin.weights[6].size(), 3u);
        EXPECT_EQ(thin.weights[6][0], 0.0) << end_bar;
        EXPECT_NEAR(thin.weights[6][1], 0.5, 1e-12) << end_bar;
        EXPECT_NEAR(thin.weights[6][2], 0.5, 1e-12) << end_bar;
        EXPECT_EQ(out.position_count[6], 2) << end_bar;
    }
}

TEST(ComputeRange, ThinMovesBeforeTheStartAreSkippedAndLaterOnesApply) {
    Thin thin(10, 3);
    thin.move(1, 1, 2, 2.0, false);          // ignored: nothing is held and it is out of range
    thin.move(7, 0, 2, 0.5, false);          // first in-range event, half of it joins the destination
    thin.signals.assign(10, std::vector<float>{1.0f, 0.0f, 1.0f});
    const auto out = thin.run(6);
    EXPECT_NEAR(thin.weights[6][0], 0.5, 1e-12);
    // The received 0.25 joins after execution on bar 7.
    EXPECT_NEAR(thin.weights[7][2], 0.5 + 0.25, 1e-12);
    EXPECT_EQ(out.turnover[7], 0.0);   // a corporate receipt is not a trade
}

TEST(ComputeRange, ThinMoveIntoSourceRetiredBeforeTheStartIsRejected) {
    Thin thin(10, 3);
    thin.move(2, 1, -1, kNaN, true);
    thin.move(7, 0, 1, 1.0, false);
    EXPECT_THROW(thin.run(6), std::runtime_error);
}

TEST(ComputeRange, OrderedAccumulatorAcceptsBlocksStartingMidPanel) {
    const DataPanel dp = make_panel();
    OrderedSignalAccumulator accumulator(dp, MissingClosePolicy::Carry, nullptr);
    const unsigned first = 200, alphas = 4;
    std::vector<std::thread> workers;
    for (unsigned a = 0; a < alphas; ++a)
        workers.emplace_back([&, a] {
            std::vector<double> scratch;
            for (unsigned t = first; t < kT; t += 32) {
                const unsigned rows = std::min(32u, kT - t);
                std::vector<float> block(size_t(rows) * kN, 1.0f);
                accumulator.add_block(a, t, block, kN, scratch);
            }
        });
    for (auto& worker : workers) worker.join();
    const auto signals = accumulator.take_signals();
    for (unsigned t = 0; t < first; ++t) EXPECT_TRUE(std::isnan(signals[t][0]));
    for (unsigned t = first; t < kT; ++t) EXPECT_NEAR(signals[t][0], float(alphas) / kN, 1e-5f);
}

TEST(ComputeRange, MetaMembersRunFromHistoryStart) {
    const DataPanel dp = make_panel();
    const MetaResolver resolver = [](const std::string&) {
        return std::vector<std::pair<std::string, std::string>>{
            {"a.xml", "<data expr=\"close\"/><rank/>"},
            {"b.xml", "<data expr=\"close\"/><tsmean days=\"5\"/>"}};
    };
    const unsigned sim_start = 77;
    auto alphas = compile_xml("<meta expr=\"s\"/><rank/>", &dp, nullptr, &resolver);
    ASSERT_EQ(alphas.size(), 1u);
    const unsigned valid = first_valid_bar(alphas[0], sim_start - 1);
    EXPECT_EQ(valid, 80u);
    MetaRunConfig run;
    run.member_config = plain_config(dp, sim_start);
    run.threads = 2;
    MetaSetCache cache(2);
    auto& set = alphas[0].meta_sets.at(0);
    const auto matrix = materialize_meta_set(dp, set, run, [] { return false; }, &cache.budget());
    ASSERT_EQ(matrix.size(), kT);
    for (unsigned t = 0; t < sim_start - 1; ++t) EXPECT_TRUE(std::isnan(matrix[t][0])) << t;
    EXPECT_TRUE(std::isfinite(matrix[sim_start - 1][0]));   // rank member is valid at the history start
    set.source->matrix = &matrix;
    auto config = plain_config(dp, sim_start);
    config.collect_signals = true;
    const auto out = Simulator(dp, config).run(alphas[0].pipeline);
    EXPECT_NEAR(out.turnover[sim_start], 1.0, 1e-5);
    EXPECT_TRUE(out.signals[sim_start - 2].empty());
}

TEST(ComputeRange, NonEwMemberReturnsStartAtSimStart) {
    const DataPanel dp = make_panel();
    const MetaResolver resolver = [](const std::string&) {
        return std::vector<std::pair<std::string, std::string>>{
            {"a.xml", "<data expr=\"close\"/><rank/>"}, {"b.xml", "<data expr=\"close\"/><zscore/>"}};
    };
    auto alphas = compile_xml("<meta expr=\"s\"/><mvo days=\"20\"/>", &dp, nullptr, &resolver);
    ASSERT_EQ(alphas.size(), 1u);
    MetaRunConfig run;
    run.member_config = plain_config(dp, 77);
    run.threads = 2;
    MetaSetCache cache(2);
    const auto returns = collect_member_returns(dp, alphas[0].meta_sets.at(0), run, [] { return false; }, &cache.budget());
    for (unsigned t = 0; t < 77; ++t) EXPECT_TRUE(std::isnan(returns.rows[t][0])) << t;
    for (unsigned t = 77; t < kT; ++t) EXPECT_TRUE(std::isfinite(returns.rows[t][0])) << t;
    EXPECT_EQ(compute_combine_weights(returns, alphas[0].meta_sets.at(0).combine).first_estimated_bar, 97u);
}
