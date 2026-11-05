// A run on its compact panel is bit-identical to the same run on the whole panel.
#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <map>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include "alpha_search.h"
#include "meta.h"
#include "module.h"
#include "modules/MetaSource.h"
#include "simulation_data.h"
#include "simulator_engine.h"
#include "xml_compiler.h"

namespace {

constexpr unsigned kT = 500, kN = 40;
constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();
constexpr double kFee = 0.0005;
constexpr unsigned kStart = 300, kEnd = 470;

DataPanel::MoveEvent move_event(unsigned bar, unsigned source, int32_t destination, double ratio, bool ends) {
    DataPanel::MoveEvent move;
    move.bar = bar; move.source = source; move.destination = destination;
    move.value_ratio = ratio; move.source_ends = ends;
    return move;
}

// Members are the even columns from bar 50 with some churn; odd columns are outside the universe and
// carry extreme values that would show any leak. 11 and 13 receive positions through successor moves.
DataPanel make_panel(bool thin) {
    DataPanel dp;
    dp.T = kT; dp.N = kN;
    for (unsigned t = 0; t < kT; ++t) dp.timestamps.push_back(static_cast<int64_t>(t) * 86400000);
    for (unsigned i = 0; i < kN; ++i) dp.symbols.push_back("S" + std::to_string(100 + i));
    std::mt19937 rng(7);
    std::normal_distribution<float> noise(0.0f, 0.02f);
    std::vector<float> close(size_t(kT) * kN), open, high, low, vwap, sector, cap, cheap;
    open = high = low = vwap = sector = cap = cheap = close;
    dp.universe_mask.assign(size_t(kT) * kN, 0);
    for (unsigned i = 0; i < kN; ++i) {
        float price = 50.0f + static_cast<float>(i);
        for (unsigned t = 0; t < kT; ++t) {
            const size_t at = size_t(t) * kN + i;
            price *= std::exp(noise(rng));
            const float scale = i % 2 ? 1000.0f : 1.0f;
            close[at] = price * scale;
            open[at] = close[at] * 1.001f; vwap[at] = close[at] * 0.999f;
            high[at] = close[at] * 1.01f; low[at] = close[at] * 0.99f;
            sector[at] = static_cast<float>(i % 3);
            cap[at] = price * 1e6f;
            cheap[at] = price;
            // Column 6 leaves for a stretch; 38 joins late.
            dp.universe_mask[at] = i % 2 == 0 && t >= 50 && !(i == 6 && t >= 350 && t < 400) && !(i == 38 && t < 380);
        }
    }
    dp.arrays["close"] = close;
    dp.arrays["open"] = open; dp.arrays["high"] = high; dp.arrays["low"] = low; dp.arrays["vwap"] = vwap;
    dp.arrays["sector"] = sector;
    dp.lazy_columns["cap"] = std::make_shared<LazyColumn>([cap] { return cap; });
    dp.lazy_columns["cheap"] = std::make_shared<LazyColumn>([cheap] { return cheap; });
    dp.lazy_columns["cheap_alias"] = dp.lazy_columns["cheap"];
    dp.default_universe = "U";
    if (thin) {
        dp.thin_layout = true;
        dp.returns_total.assign(size_t(kT) * kN, kNaN);
        for (unsigned t = 1; t < kT; ++t)
            for (unsigned i = 0; i < kN; ++i)
                dp.returns_total[size_t(t) * kN + i] = close[size_t(t) * kN + i] / close[size_t(t - 1) * kN + i] - 1.0;
        dp.move_events = {
            move_event(0, 8, -1, kNaN, true),             // retired before the run yet a member in it
            move_event(320, 12, 11, 0.6, false),          // distribution to a successor outside the universe
            move_event(340, 14, 13, 0.9, true),           // ending source into an outsider ...
            move_event(360, 13, 15, 1.1, true),           // ... which itself moves on
            move_event(380, 16, -1, kNaN, true),          // cash out
            move_event(400, 17, 19, 1.0, true),           // source outside the universe: dropped
            move_event(490, 18, 21, 1.0, true),           // after the run
        };
    }
    return dp;
}

template <class Value>
bool same_bits(const Value& a, const Value& b) { return std::memcmp(&a, &b, sizeof(Value)) == 0; }

template <class Value>
void expect_series(const std::vector<Value>& a, const std::vector<Value>& b, const char* what, const std::string& xml) {
    ASSERT_EQ(a.size(), b.size()) << what;
    for (size_t t = 0; t < a.size(); ++t)
        ASSERT_TRUE(same_bits(a[t], b[t])) << what << " bar " << t << ": " << a[t] << " vs " << b[t] << "\n" << xml;
}

SimulatorConfig config_for(const DataPanel& dp, const std::vector<uint8_t>& mask, ExecutionPrice price) {
    SimulatorConfig config;
    config.T = dp.T; config.N = dp.N; config.fee_rate = kFee;
    config.sim_start_index = kStart; config.sim_end_index = kEnd;
    config.universe_mask = &mask;
    config.execution_price = price;
    return config;
}

void expect_outputs(const SimulatorOutput& full, const SimulatorOutput& compact, const std::string& xml) {
    expect_series(full.ret, compact.ret, "ret", xml);
    expect_series(full.gross_ret, compact.gross_ret, "gross", xml);
    expect_series(full.execution_ret, compact.execution_ret, "execution", xml);
    expect_series(full.turnover, compact.turnover, "turnover", xml);
    expect_series(full.max_weight, compact.max_weight, "max weight", xml);
    expect_series(full.long_weight, compact.long_weight, "long", xml);
    expect_series(full.short_weight, compact.short_weight, "short", xml);
    expect_series(full.position_count, compact.position_count, "count", xml);
    ASSERT_EQ(full.missing_data.size(), compact.missing_data.size());
    for (size_t t = 0; t < full.missing_data.size(); ++t) {
        const auto& a = full.missing_data[t];
        const auto& b = compact.missing_data[t];
        EXPECT_EQ(a.missing_held_count, b.missing_held_count);
        EXPECT_EQ(a.blocked_order_count, b.blocked_order_count);
        EXPECT_TRUE(same_bits(a.frozen_gross, b.frozen_gross));
        EXPECT_TRUE(same_bits(a.blocked_trade_weight, b.blocked_trade_weight));
        EXPECT_EQ(a.resumed_holding_count, b.resumed_holding_count);
        EXPECT_EQ(a.missing_entry_count, b.missing_entry_count);
        EXPECT_EQ(a.successor_conversions, b.successor_conversions);
        EXPECT_TRUE(same_bits(a.converted_gross, b.converted_gross));
    }
}

using Weights = std::map<unsigned, std::vector<double>>;

SimulatorOutput run_alpha(const DataPanel& dp, const std::vector<uint8_t>& mask, const std::string& xml,
                          const MetaResolver* resolver, ExecutionPrice price, Weights& weights) {
    auto alphas = compile_xml(xml, &dp, &mask, resolver);
    EXPECT_EQ(alphas.size(), 1u);
    auto config = config_for(dp, mask, price);
    config.collect_signals = true;
    config.accounting_row_callback = [&](const AccountingRowView& row) { weights[row.bar] = row.weights; };
    MetaRunConfig run{config, 2, MissingClosePolicy::Carry, &mask};
    run.member_config.collect_signals = false;
    run.member_config.accounting_row_callback = {};
    MetaSetCache cache(2);
    MemberReturnsCache returns;
    std::vector<std::shared_ptr<const MetaMatrix>> held;
    for (auto& set : alphas[0].meta_sets) {
        auto matrix = std::make_shared<const MetaMatrix>(set.combine.is_ew()
            ? materialize_meta_set(dp, set, run, [] { return false; }, &cache.budget())
            : materialize_combined(MetaCombineContext{dp, run, "settings", returns, cache, [] { return false; }}, set));
        set.source->matrix = matrix.get();
        held.push_back(std::move(matrix));
    }
    return Simulator(dp, config).run(alphas[0].pipeline);
}

void expect_identical(const DataPanel& dp, const std::string& xml, const MetaResolver* resolver = nullptr,
                      ExecutionPrice price = ExecutionPrice::Close) {
    const auto compact = make_run_panel(dp, "", kStart - 1, kEnd);
    ASSERT_LT(compact->N, dp.N);
    Weights full_weights, compact_weights;
    const auto full_out = run_alpha(dp, dp.universe_mask, xml, resolver, price, full_weights);
    const auto compact_out = run_alpha(*compact, compact->universe_mask, xml, resolver, price, compact_weights);
    expect_outputs(full_out, compact_out, xml);
    const auto columns = run_panel_columns(dp, dp.universe_mask, kStart - 1, kEnd);
    double held = 0.0;
    ASSERT_EQ(full_weights.size(), compact_weights.size());
    for (unsigned t = kStart - 1; t < kEnd; ++t) {
        std::vector<uint8_t> is_kept(dp.N, 0);
        for (size_t k = 0; k < columns.size(); ++k) {
            is_kept[columns[k]] = 1;
            ASSERT_TRUE(same_bits(full_out.signals[t][columns[k]], compact_out.signals[t][k])) << xml << " bar " << t;
            ASSERT_TRUE(same_bits(full_weights[t][columns[k]], compact_weights[t][k])) << xml << " bar " << t;
            held += std::fabs(compact_weights[t][k]);
        }
        for (unsigned n = 0; n < dp.N; ++n)
            if (!is_kept[n]) ASSERT_EQ(full_weights[t][n], 0.0) << xml;
    }
    EXPECT_GT(held, 0.0) << xml;
}

const std::vector<std::string> kAlphas = {
    "<data expr=\"close\" delay=\"1\"/><rank/>",
    "<data expr=\"close\" delay=\"1\"/><rank/><neutralize group=\"sector\"/>",
    "<data expr=\"cheap\" delay=\"1\"/><tsmean days=\"10\"/><zscore/>",
    "<data expr=\"cheap\" delay=\"1\"/><tsrank days=\"20\"/>",
    "<data expr=\"cheap\" delay=\"1\"/><tszscore days=\"20\"/><winsorize/>",
    "<data expr=\"cheap\" delay=\"1\"/><decay days=\"21\"/><normalize/>",
    "<data expr=\"cap\" delay=\"1\"/><tsdelta days=\"5\"/><group_rank group=\"sector\"/><truncate max=\"0.1\"/>",
    "<data expr=\"cheap\" delay=\"1\"/><tsstddev days=\"30\"/><hump hump=\"0.01\"/>",
    "<data expr=\"cap\" delay=\"1\"/><group_mean group=\"sector\"/>",
    "<data expr=\"cap\" delay=\"1\"/><group_mean group=\"sector\" weight=\"cap\"/>",
    "<data expr=\"cheap\" delay=\"1\"/><ffd d=\"0.5\" thres=\"0.01\"/>",
    "<data expr=\"cheap\" delay=\"1\"/><ffd d=\"0.5\" thres=\"0.01\"/><pcaresid train=\"60\" refit=\"21\" k=\"3\"/>",
    "<data expr=\"cheap\" delay=\"1\"/><pcaresid train=\"60\" refit=\"21\" group=\"sector\"/><rank/>",
};

const MetaResolver kMembers = [](const std::string&) {
    return std::vector<std::pair<std::string, std::string>>{
        {"a", "<data expr=\"cheap\" delay=\"1\"/><tsmean days=\"5\"/><rank/>"},
        {"b", "<data expr=\"close\" delay=\"1\"/><tsdelta days=\"3\"/><zscore/>"},
        {"c", "<data expr=\"cheap\" delay=\"1\"/><tsrank days=\"10\"/>"},
    };
};

}  // namespace

TEST(RunPanelColumns, MembershipWindowEdgesAreBarsInRange) {
    DataPanel dp;
    dp.T = 10; dp.N = 4;
    std::vector<uint8_t> mask(40, 0);
    mask[2 * 4 + 0] = 1;   // only on the row before the window
    mask[3 * 4 + 1] = 1;   // first row of the window
    mask[7 * 4 + 2] = 1;   // last row of the window
    mask[8 * 4 + 3] = 1;   // the exclusive end row
    EXPECT_EQ(run_panel_columns(dp, mask, 3, 8), (std::vector<unsigned>{1, 2}));
    EXPECT_EQ(run_panel_columns(dp, {}, 3, 8), (std::vector<unsigned>{0, 1, 2, 3}));
}

TEST(RunPanelColumns, ClosureFollowsChainsAndIgnoresOutOfRangeMoves) {
    DataPanel dp;
    dp.T = 10; dp.N = 7;
    std::vector<uint8_t> mask(70, 0);
    mask[4 * 7 + 3] = 1;
    // Listed out of chain order on purpose: 3 -> 5 -> 1 needs a second pass.
    dp.move_events = {move_event(4, 5, 1, 1.0, true), move_event(5, 3, 5, 1.0, true),
                      move_event(2, 3, 0, 1.0, false),   // before the range
                      move_event(8, 3, 2, 1.0, false),   // the exclusive end bar
                      move_event(5, 4, 6, 1.0, true),    // source not kept
                      move_event(5, 3, -1, kNaN, false)};
    EXPECT_EQ(run_panel_columns(dp, mask, 3, 8), (std::vector<unsigned>{1, 3, 5}));
}

TEST(RunPanelColumns, NoMemberStillKeepsOneColumnAndTheRunIsFlat) {
    DataPanel dp = make_panel(false);
    dp.universe_mask.assign(size_t(kT) * kN, 0);
    const auto compact = make_run_panel(dp, "", kStart - 1, kEnd);
    ASSERT_EQ(compact->N, 1u);
    Weights weights;
    const auto out = run_alpha(*compact, compact->universe_mask, kAlphas[0], nullptr, ExecutionPrice::Close, weights);
    for (double value : out.ret) EXPECT_EQ(value, 0.0);
}

TEST(RunPanel, RemapsAndRestrictsMoves) {
    const DataPanel dp = make_panel(true);
    const auto compact = make_run_panel(dp, "", kStart - 1, kEnd);
    const auto position = [&](const std::string& symbol) {
        return static_cast<int>(std::find(compact->symbols.begin(), compact->symbols.end(), symbol) - compact->symbols.begin());
    };
    ASSERT_EQ(compact->move_events.size(), 5u);
    const auto& moves = compact->move_events;
    EXPECT_EQ(moves[0].bar, 0u);
    EXPECT_EQ(moves[0].source, unsigned(position("S108")));
    EXPECT_EQ(moves[0].destination, -1);
    EXPECT_TRUE(moves[0].source_ends);
    EXPECT_EQ(moves[1].source, unsigned(position("S112")));
    EXPECT_EQ(moves[1].destination, position("S111"));
    EXPECT_EQ(moves[1].value_ratio, 0.6);
    EXPECT_EQ(moves[2].destination, position("S113"));
    EXPECT_EQ(moves[3].source, unsigned(position("S113")));
    EXPECT_EQ(moves[3].destination, position("S115"));
    EXPECT_EQ(moves[4].destination, -1);
    EXPECT_TRUE(std::is_sorted(moves.begin(), moves.end(), [](const auto& a, const auto& b) { return a.bar < b.bar; }));
    EXPECT_EQ(position("S117"), int(compact->N));  // dropped source and destination
    EXPECT_EQ(position("S119"), int(compact->N));
    EXPECT_EQ(position("S121"), int(compact->N));
}

TEST(RunPanel, RetirementBeforeTheRunDropsTheDestination) {
    DataPanel dp = make_panel(true);
    dp.move_events = {move_event(100, 8, 9, 1.0, true)};
    const auto compact = make_run_panel(dp, "", kStart - 1, kEnd);
    ASSERT_EQ(compact->move_events.size(), 1u);
    EXPECT_EQ(compact->move_events[0].destination, -1);
    EXPECT_TRUE(compact->move_events[0].source_ends);
    EXPECT_EQ(std::find(compact->symbols.begin(), compact->symbols.end(), "S109"), compact->symbols.end());
    expect_identical(dp, kAlphas[0]);
}

TEST(RunPanel, ProjectsEveryFieldLazilyAndKeepsAliases) {
    int parent_loads = 0;
    DataPanel dp;
    dp.T = 2; dp.N = 3;
    dp.timestamps = {0, 86400000};
    dp.symbols = {"A", "B", "C"};
    dp.arrays["close"] = {1, 2, 3, 4, 5, 6};
    dp.lazy_columns["x"] = std::make_shared<LazyColumn>([&parent_loads] {
        ++parent_loads;
        return std::vector<float>{10, 20, 30, 40, 50, 60};
    });
    dp.lazy_columns["x_alias"] = dp.lazy_columns["x"];
    dp.universe_mask = {1, 0, 0, 0, 0, 1};
    dp.default_universe = "U";
    dp.thin_layout = true;
    dp.returns_total = {.1, .2, .3, .4, .5, .6};
    const auto compact = make_run_panel(dp, "U", 0, 2);
    EXPECT_EQ(compact->symbols, (std::vector<std::string>{"A", "C"}));
    EXPECT_EQ(compact->universe_mask, (std::vector<uint8_t>{1, 0, 0, 1}));
    EXPECT_EQ(compact->returns_total, (std::vector<double>{.1, .3, .4, .6}));
    EXPECT_EQ(compact->timestamps, dp.timestamps);
    EXPECT_TRUE(compact->thin_layout);
    EXPECT_EQ(compact->column_names(), dp.column_names());
    EXPECT_EQ(parent_loads, 0);
    EXPECT_FALSE(compact->lazy_columns.at("x")->loaded());
    EXPECT_EQ(compact->lazy_columns.at("x"), compact->lazy_columns.at("x_alias"));
    EXPECT_EQ(compact->row("x_alias", 1)[1], 60.0f);
    EXPECT_EQ(compact->row("x", 0)[1], 30.0f);
    EXPECT_EQ(parent_loads, 1);
    EXPECT_EQ(compact->row("close", 1)[0], 4.0f);
}

TEST(RunPanelCache, EvictsLeastRecentByBytes) {
    const DataPanel dp = make_panel(true);
    const size_t one = make_run_panel(dp, "", 10, 100)->universe_mask.size() * 9;  // mask + returns, same K
    RunPanelCache cache(dp, 2 * one + one / 2);
    const auto first = cache.get("", 10, 100);
    EXPECT_EQ(first->N, make_run_panel(dp, "", 10, 101)->N);
    EXPECT_EQ(cache.get("", 10, 100), first);
    const auto second = cache.get("", 10, 101);
    EXPECT_EQ(cache.get("", 10, 100), first);   // two fit; this lookup refreshes it
    (void)cache.get("", 10, 102);               // evicts 101, the least recent
    EXPECT_EQ(cache.get("", 10, 100), first);
    EXPECT_NE(cache.get("", 10, 101), second);
}

TEST(RunPanelCache, ColumnsLoadedSinceTheLastLookupCount) {
    const DataPanel dp = make_panel(true);
    const size_t one = make_run_panel(dp, "", 10, 100)->universe_mask.size() * 9;
    RunPanelCache cache(dp, 2 * one + one / 2);
    const auto loaded = cache.get("", 10, 100);
    const auto other = cache.get("", 10, 101);
    EXPECT_EQ(cache.get("", 10, 100), loaded);  // both fit before any column is read
    EXPECT_EQ(cache.get("", 10, 101), other);
    for (const char* field : {"close", "open", "high"}) (void)loaded->row(field, 0);
    (void)cache.get("", 10, 101);               // now over the cap: `loaded` is least recent
    EXPECT_NE(cache.get("", 10, 100), loaded);
}

TEST(RunPanelCache, APanelOverTheCapStaysUntilAnotherIsUsedAndAHeldOneStaysValid) {
    const DataPanel dp = make_panel(true);
    RunPanelCache cache(dp, 1);
    const auto held = cache.get("", 10, 100);
    EXPECT_EQ(cache.get("", 10, 100), held);    // most recently used always stays
    const float expected = held->row("close", 60)[3];
    const auto next = cache.get("", 10, 101);
    EXPECT_EQ(cache.get("", 10, 101), next);
    EXPECT_NE(cache.get("", 10, 100), held);    // evicted and rebuilt; the holder is untouched
    EXPECT_EQ(held->row("close", 60)[3], expected);
    EXPECT_EQ(held.use_count(), 1);
}

TEST(RunPanelCache, ConcurrentFirstRequestsShareAConsistentPanel) {
    const DataPanel dp = make_panel(true);
    RunPanelCache cache(dp);
    std::vector<std::shared_ptr<const DataPanel>> panels(8);
    std::vector<std::thread> threads;
    for (size_t i = 0; i < panels.size(); ++i)
        threads.emplace_back([&, i] {
            panels[i] = cache.get("", kStart - 1, kEnd);
            (void)panels[i]->row("cheap", 10);
        });
    for (auto& thread : threads) thread.join();
    for (const auto& panel : panels) {
        EXPECT_EQ(panel->symbols, panels[0]->symbols);
        ASSERT_EQ(panel->returns_total.size(), panels[0]->returns_total.size());
        EXPECT_EQ(std::memcmp(panel->returns_total.data(), panels[0]->returns_total.data(),
                              panel->returns_total.size() * sizeof(double)), 0);
    }
    EXPECT_EQ(cache.get("", kStart - 1, kEnd)->N, panels[0]->N);
}

TEST(RunPanelIdentity, ModulesOnAnOrdinaryPanel) {
    const DataPanel dp = make_panel(false);
    for (const auto& xml : kAlphas) expect_identical(dp, xml);
}

TEST(RunPanelIdentity, ModulesOnAThinPanelWithMoves) {
    const DataPanel dp = make_panel(true);
    for (const auto& xml : kAlphas) expect_identical(dp, xml);
}

TEST(RunPanelIdentity, SuccessorOutsideTheUniverseReceivesTheSameBook) {
    const DataPanel dp = make_panel(true);
    const std::string xml = kAlphas[0];
    Weights full, small;
    const auto compact = make_run_panel(dp, "", kStart - 1, kEnd);
    const auto a = run_alpha(dp, dp.universe_mask, xml, nullptr, ExecutionPrice::Close, full);
    const auto b = run_alpha(*compact, compact->universe_mask, xml, nullptr, ExecutionPrice::Close, small);
    expect_outputs(a, b, xml);
    double conversions = 0.0;
    for (const auto& bar : b.missing_data) conversions += bar.converted_gross;
    EXPECT_GT(conversions, 0.0);  // the move really carried weight
    EXPECT_NE(full.at(320)[11], 0.0);  // the distribution joined a name outside the universe
    const auto& symbols = compact->symbols;
    const auto at = std::find(symbols.begin(), symbols.end(), "S111") - symbols.begin();
    EXPECT_EQ(full.at(320)[11], small.at(320)[at]);
}

TEST(RunPanelIdentity, RetirementsMatchWhenEveryEndingSourceIsKept) {
    DataPanel dp = make_panel(true);
    dp.move_events.erase(std::remove_if(dp.move_events.begin(), dp.move_events.end(), [](const auto& move) {
        return move.source == 17;
    }), dp.move_events.end());
    Weights full, small;
    const auto compact = make_run_panel(dp, "", kStart - 1, kEnd);
    const auto a = run_alpha(dp, dp.universe_mask, kAlphas[0], nullptr, ExecutionPrice::Close, full);
    const auto b = run_alpha(*compact, compact->universe_mask, kAlphas[0], nullptr, ExecutionPrice::Close, small);
    unsigned retired = 0;
    for (size_t t = 0; t < a.missing_data.size(); ++t) {
        EXPECT_EQ(a.missing_data[t].retirement_count, b.missing_data[t].retirement_count) << t;
        retired += b.missing_data[t].retirement_count;
    }
    EXPECT_GT(retired, 0u);
}

TEST(RunPanelIdentity, ExecutionPrices) {
    const DataPanel dp = make_panel(true);
    expect_identical(dp, kAlphas[1], nullptr, ExecutionPrice::Ohlc4);
    expect_identical(dp, kAlphas[1], nullptr, ExecutionPrice::Vwap);
}

TEST(RunPanelIdentity, MetaEwAndMvo) {
    const DataPanel dp = make_panel(true);
    expect_identical(dp, "<meta expr=\"s\"/>", &kMembers);
    expect_identical(dp, "<meta expr=\"s\"/><rank/>", &kMembers);
    expect_identical(dp, "<meta expr=\"s\"/><mvo days=\"60\"/>", &kMembers);
}

TEST(RunPanelIdentity, CombinedBatch) {
    const DataPanel dp = make_panel(true);
    const auto compact = make_run_panel(dp, "", kStart - 1, kEnd);
    const auto combined = [&](const DataPanel& panel, const std::vector<uint8_t>& mask) {
        OrderedSignalAccumulator accumulator(panel, MissingClosePolicy::Carry, &mask);
        std::vector<double> scratch;
        for (size_t i = 0; i < kAlphas.size(); ++i) {
            auto alphas = compile_xml(kAlphas[i], &panel, &mask);
            Simulator(panel, config_for(panel, mask, ExecutionPrice::Close)).run_signal_rows(alphas[0].pipeline,
                [&](unsigned di, const float* row, unsigned n) { accumulator.add(i, di, row, n, scratch); });
        }
        auto signals = accumulator.take_signals();
        return std::make_pair(Simulator(panel, config_for(panel, mask, ExecutionPrice::Close)).run_from_signals(signals), signals);
    };
    const auto [full_out, full_signals] = combined(dp, dp.universe_mask);
    const auto [compact_out, compact_signals] = combined(*compact, compact->universe_mask);
    expect_outputs(full_out, compact_out, "combined");
    const auto columns = run_panel_columns(dp, dp.universe_mask, kStart - 1, kEnd);
    for (unsigned t = kStart - 1; t < kEnd; ++t)
        for (size_t k = 0; k < columns.size(); ++k)
            ASSERT_TRUE(same_bits(full_signals[t][columns[k]], compact_signals[t][k])) << t;
}

TEST(RunPanelIdentity, GeneticSearchScoresAndChoosesTheSame) {
    const DataPanel dp = make_panel(true);
    const auto compact = make_run_panel(dp, "", kStart - 1, kEnd);
    const std::string xml = "<data expr=\"cheap\" delay=\"1\"/><tsmean days=\"3|5|8|13|21\"/><rank/>";
    const auto search = [&](const DataPanel& panel, const std::vector<uint8_t>& mask) {
        GAConfig config;
        config.population_size = 6; config.first_search = 6; config.generations = 3; config.stop_alphas = 20;
        config.seed = 42; config.n_threads = 1;
        std::map<std::string, SimulatorOutput> runs;
        GAEngine engine(xml, extract_search_space(xml), config);
        const auto result = engine.run([&](const std::string& candidate) {
            Weights weights;
            runs[candidate] = run_alpha(panel, mask, candidate, nullptr, ExecutionPrice::Close, weights);
            double total = 0.0;
            for (double value : runs[candidate].ret) total += value;
            return total;
        });
        return std::make_pair(result.best_xml, std::move(runs));
    };
    const auto [full_best, full_runs] = search(dp, dp.universe_mask);
    const auto [compact_best, compact_runs] = search(*compact, compact->universe_mask);
    EXPECT_EQ(full_best, compact_best);
    ASSERT_EQ(full_runs.size(), compact_runs.size());
    ASSERT_GT(full_runs.size(), 1u);
    for (const auto& [candidate, output] : full_runs) expect_outputs(output, compact_runs.at(candidate), candidate);
}
