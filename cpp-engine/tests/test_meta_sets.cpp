#include <gtest/gtest.h>
#include <atomic>
#include <chrono>
#include <iostream>
#include <memory>
#include <cstring>
#include <stdexcept>
#include <thread>
#include "api/combined_signals.h"
#include "api/meta_returns.h"
#include "api/meta_sets.h"
#include "compiler/dag_builder.h"
#include "data/data_panel.h"
#include "engine/simulator.h"

static DataPanel make_panel(unsigned T = 40, unsigned N = 5) {
    DataPanel dp;
    dp.T = T;
    dp.N = N;
    dp.timestamps.resize(T);
    for (unsigned t = 0; t < T; ++t) dp.timestamps[t] = static_cast<int64_t>(t) * 86400000;
    dp.symbols.resize(N);
    for (unsigned i = 0; i < N; ++i) dp.symbols[i] = "SYM" + std::to_string(i);
    auto& close = dp.arrays["close"];
    close.resize(T * N);
    for (unsigned t = 0; t < T; ++t)
        for (unsigned i = 0; i < N; ++i)
            close[t * N + i] = 100.0f + static_cast<float>((t * 7 + i * 13) % 11) + static_cast<float>(i);
    return dp;
}

static const std::vector<std::pair<std::string, std::string>> kMembers = {
    {"a.xml", R"(<data expr="close" delay="1"/><rank/>)"},
    {"b.xml", R"(<data expr="close" delay="1"/><tsmean days="5"/><zscore/>)"},
    {"sub/c.xml", R"(<data expr="close" delay="1"/><tsrank days="10"/>)"},
};

static MetaSet compile_set(const DataPanel& dp, const std::vector<std::pair<std::string, std::string>>& members) {
    MetaResolver resolver = [&](const std::string&) { return members; };
    auto alphas = compile_xml(R"(<meta expr="s"/>)", &dp, nullptr, &resolver);
    EXPECT_EQ(alphas.size(), 1u);
    EXPECT_EQ(alphas[0].meta_sets.size(), 1u);
    return std::move(alphas[0].meta_sets[0]);
}

static MetaRunConfig run_config(const DataPanel& dp, unsigned threads) {
    SimulatorConfig sim;
    sim.T = dp.T;
    sim.N = dp.N;
    sim.fee_rate = 0.0;
    return MetaRunConfig{sim, threads, MissingClosePolicy::Carry, nullptr};
}

static bool same_bits(const MetaMatrix& a, const MetaMatrix& b) {
    if (a.size() != b.size()) return false;
    for (std::size_t t = 0; t < a.size(); ++t)
        if (a[t].size() != b[t].size() ||
            std::memcmp(a[t].data(), b[t].data(), a[t].size() * sizeof(float)) != 0)
            return false;
    return true;
}

TEST(MetaSets, MaterializeEqualsOrderedAccumulator) {
    const auto dp = make_panel();
    const auto never = [] { return false; };

    // Hand-driven: each member's rows enter the accumulator in member order.
    OrderedSignalAccumulator accumulator(dp, MissingClosePolicy::Carry, nullptr);
    std::vector<double> scratch;
    for (std::size_t i = 0; i < kMembers.size(); ++i) {
        auto alphas = compile_xml(kMembers[i].second, &dp);
        Simulator(dp, run_config(dp, 1).member_config).run_signal_rows(alphas[0].pipeline,
            [&](unsigned di, const float* row, unsigned n) { accumulator.add(i, di, row, n, scratch); });
    }
    const MetaMatrix expected = accumulator.take_signals();

    auto serial_set = compile_set(dp, kMembers);
    const MetaMatrix serial = materialize_meta_set(dp, serial_set, run_config(dp, 1), never);
    auto parallel_set = compile_set(dp, kMembers);
    const MetaMatrix parallel = materialize_meta_set(dp, parallel_set, run_config(dp, 3), never);
    EXPECT_TRUE(same_bits(serial, expected));
    EXPECT_TRUE(same_bits(parallel, expected));
    std::size_t finite = 0;
    for (const auto& row : expected)
        for (float v : row) finite += std::isfinite(v);
    EXPECT_GT(finite, 0u);
}

TEST(MetaSets, MemberFailureNamesMember) {
    const auto dp = make_panel();
    // An unbound meta source throws at runtime; use it as the second member.
    MetaResolver inner = [&](const std::string&) { return kMembers; };
    for (unsigned threads : {1u, 3u}) {
        auto copy_set = compile_set(dp, kMembers);
        auto failing = compile_xml(R"(<meta expr="inner"/>)", &dp, nullptr, &inner);
        copy_set.members[1] = std::move(failing[0]);
        copy_set.member_paths[1] = "sub/broken.xml";
        try {
            (void)materialize_meta_set(dp, copy_set, run_config(dp, threads), [] { return false; });
            FAIL() << "expected a member failure";
        } catch (const std::runtime_error& error) {
            const std::string what = error.what();
            EXPECT_NE(what.find("meta 's' member sub/broken.xml: "), std::string::npos) << what;
            EXPECT_NE(what.find("was not materialized"), std::string::npos) << what;
        }
    }
}

TEST(MetaSets, CacheComputesOnce) {
    MetaSetCache cache(1);
    std::atomic<int> calls{0};
    std::vector<std::shared_ptr<const MetaMatrix>> got(8);
    std::vector<std::thread> threads;
    for (int t = 0; t < 8; ++t)
        threads.emplace_back([&, t] {
            got[t] = cache.get("s", [&] {
                ++calls;
                std::this_thread::sleep_for(std::chrono::milliseconds(20));
                return MetaMatrix{{1.0f, 2.0f}};
            });
        });
    for (auto& thread : threads) thread.join();
    EXPECT_EQ(calls.load(), 1);
    for (const auto& matrix : got) EXPECT_EQ(matrix.get(), got[0].get());

    std::atomic<int> failing_calls{0};
    std::atomic<int> seen{0};
    threads.clear();
    for (int t = 0; t < 8; ++t)
        threads.emplace_back([&] {
            try {
                (void)cache.get("bad", [&]() -> MetaMatrix {
                    ++failing_calls;
                    std::this_thread::sleep_for(std::chrono::milliseconds(20));
                    throw std::runtime_error("boom");
                });
            } catch (const std::runtime_error& error) {
                if (std::string(error.what()) == "boom") ++seen;
            }
        });
    for (auto& thread : threads) thread.join();
    EXPECT_EQ(failing_calls.load(), 1);
    EXPECT_EQ(seen.load(), 8);
    // A later caller gets the stored failure; compute is not retried.
    try {
        (void)cache.get("bad", [&]() -> MetaMatrix { ++failing_calls; return {}; });
        ADD_FAILURE() << "expected the stored failure";
    } catch (const std::runtime_error& error) {
        EXPECT_STREQ(error.what(), "boom");
    }
    EXPECT_EQ(failing_calls.load(), 1);
}

// Writes the member index on every bar and counts how many member pipelines run at once.
struct CountingModule : public Module {
    float value = 0.0f;
    std::atomic<int>* active = nullptr;
    std::atomic<int>* peak = nullptr;
    void init() override {}
    void calculate(unsigned, SignalVector& output) override {
        const int now = ++*active;
        int seen = peak->load();
        while (now > seen && !peak->compare_exchange_weak(seen, now)) {}
        std::this_thread::sleep_for(std::chrono::microseconds(200));
        for (unsigned ii = 0; ii < numAssets; ++ii) output[ii] = value + static_cast<float>(ii);
        --*active;
    }
};

static MetaSet counting_set(const std::string& name, unsigned members, std::atomic<int>& active,
                            std::atomic<int>& peak) {
    MetaSet set;
    set.name = name;
    for (unsigned k = 0; k < members; ++k) {
        CompiledAlpha alpha;
        auto module = std::make_unique<CountingModule>();
        module->value = static_cast<float>(k);
        module->active = &active;
        module->peak = &peak;
        alpha.pipeline.push_back(module.get());
        alpha.output = module.get();
        alpha.modules.push_back(std::move(module));
        set.members.push_back(std::move(alpha));
        set.member_paths.push_back(name + std::to_string(k) + ".xml");
    }
    return set;
}

TEST(MetaSets, BudgetBoundsConcurrentMaterializations) {
    const auto dp = make_panel(60, 4);
    const auto never = [] { return false; };
    std::atomic<int> active{0}, peak{0};
    // Reference: one set alone with one worker.
    auto reference_set = counting_set("r", 8, active, peak);
    const MetaMatrix expected = materialize_meta_set(dp, reference_set, run_config(dp, 1), never);
    peak = 0;

    MetaSetCache cache(3);
    auto a = counting_set("a", 8, active, peak);
    auto b = counting_set("b", 8, active, peak);
    MetaMatrix result_a, result_b;
    std::thread first([&] { result_a = materialize_meta_set(dp, a, run_config(dp, 3), never, &cache.budget()); });
    std::thread second([&] { result_b = materialize_meta_set(dp, b, run_config(dp, 3), never, &cache.budget()); });
    first.join();
    second.join();
    std::cout << "[ budget ] peak concurrent member workers = " << peak.load() << "\n";
    EXPECT_LE(peak.load(), 3);
    EXPECT_GE(peak.load(), 1);
    EXPECT_TRUE(same_bits(result_a, expected));
    EXPECT_TRUE(same_bits(result_b, expected));

    MetaSet empty;
    empty.name = "none";
    try {
        (void)materialize_meta_set(dp, empty, run_config(dp, 1), never);
        ADD_FAILURE() << "expected a logic_error";
    } catch (const std::logic_error& error) {
        EXPECT_STREQ(error.what(), "meta set 'none' has no members");
    }
}

static std::vector<std::vector<double>> executed_weights(const DataPanel& dp, const MetaMatrix& signals) {
    std::vector<std::vector<double>> rows;
    SimulatorConfig sim = run_config(dp, 1).member_config;
    sim.accounting_row_callback = [&](const AccountingRowView& view) { rows.push_back(view.weights); };
    (void)Simulator(dp, sim).run_from_signals(signals);
    return rows;
}

static CombineWeights constant_weights(unsigned T, const std::vector<double>& row) {
    CombineWeights weights;
    weights.rows.assign(T, row);
    weights.first_estimated_bar = T;
    return weights;
}

TEST(MetaSets, WeightedEqualsEwWithUniformWeights) {
    const auto dp = make_panel();
    const auto never = [] { return false; };
    // Four members: 1/K is a power of two, so the weighted rows are the EW rows scaled exactly.
    auto members = kMembers;
    members.push_back({"d.xml", R"(<data expr="close" delay="1"/><tsmean days="3"/><rank/>)"});
    auto ew_set = compile_set(dp, members);
    const MetaMatrix ew = materialize_meta_set(dp, ew_set, run_config(dp, 1), never);
    const auto ew_weights = executed_weights(dp, ew);
    ASSERT_GT(ew_weights.size(), 0u);
    const CombineWeights uniform = constant_weights(dp.T, std::vector<double>(4, 0.25));
    for (unsigned threads : {1u, 3u}) {
        auto set = compile_set(dp, members);
        const auto weighted = materialize_weighted(dp, set, run_config(dp, threads), {&uniform}, never);
        ASSERT_EQ(weighted.size(), 1u);
        for (std::size_t t = 0; t < ew.size(); ++t)
            for (std::size_t i = 0; i < ew[t].size(); ++i) {
                if (std::isnan(ew[t][i])) { EXPECT_TRUE(std::isnan(weighted[0][t][i])); continue; }
                EXPECT_EQ(weighted[0][t][i], ew[t][i] * 0.25f) << t << "," << i;
            }
        const auto got = executed_weights(dp, weighted[0]);
        ASSERT_EQ(got.size(), ew_weights.size());
        for (std::size_t t = 0; t < got.size(); ++t)
            EXPECT_EQ(std::memcmp(got[t].data(), ew_weights[t].data(), got[t].size() * sizeof(double)), 0)
                << "threads " << threads << " bar " << t;
        for (const auto& member : set.members) EXPECT_TRUE(member.pipeline.empty());  // released
    }
}

TEST(MetaSets, WeightedUsesPerBarWeights) {
    const auto dp = make_panel();
    const auto never = [] { return false; };
    const std::vector<std::pair<std::string, std::string>> two(kMembers.begin(), kMembers.begin() + 2);
    MetaMatrix alone[2];
    for (int k = 0; k < 2; ++k) {
        auto set = compile_set(dp, {two[k]});
        alone[k] = materialize_meta_set(dp, set, run_config(dp, 1), never);
    }
    CombineWeights switching = constant_weights(dp.T, {0.75, 0.25});
    for (unsigned t = 5; t < dp.T; ++t) switching.rows[t] = {-0.25, 0.75};
    const CombineWeights first_only = constant_weights(dp.T, {1.0, 0.0});
    auto set = compile_set(dp, two);
    const auto out = materialize_weighted(dp, set, run_config(dp, 2), {&switching, &first_only}, never);
    ASSERT_EQ(out.size(), 2u);
    std::size_t checked = 0;
    for (unsigned t = 0; t < dp.T; ++t)
        for (unsigned i = 0; i < dp.N; ++i) {
            const float a = alone[0][t][i], b = alone[1][t][i];
            if (!std::isfinite(a) || !std::isfinite(b)) continue;
            const double wa = t < 5 ? 0.75 : -0.25, wb = t < 5 ? 0.25 : 0.75;
            EXPECT_NEAR(out[0][t][i], wa * a + wb * b, 1e-6) << t << "," << i;
            EXPECT_EQ(out[1][t][i], a) << t << "," << i;   // weight 1 then + 0
            ++checked;
        }
    EXPECT_GT(checked, 0u);
}

TEST(MetaSets, PrefetchComputesEachSetOnce) {
    const auto dp = make_panel(60, 5);
    const auto never = [] { return false; };
    MetaResolver resolver = [&](const std::string&) { return kMembers; };
    auto alphas = compile_xml(R"(<meta expr="s"/><mvo days="5|6|7"/>)", &dp, nullptr, &resolver);
    ASSERT_EQ(alphas.size(), 3u);
    MetaSetCache cache(2);
    MemberReturnsCache returns;
    const MetaCombineContext context{dp, run_config(dp, 2), "settings", returns, cache, never};
    const auto keys = prefetch_meta_sets(context, alphas);
    ASSERT_EQ(keys.size(), 3u);
    EXPECT_EQ(cache.stats().materializations, 1u);              // one weighted pass for three specs
    EXPECT_EQ(cache.stats().member_runs, 2 * kMembers.size());  // returns pass + weighted pass
    for (std::size_t c = 0; c < keys.size(); ++c) {
        ASSERT_TRUE(cache.contains(keys[c]));
        EXPECT_EQ(keys[c], meta_set_key(alphas[c].meta_sets[0], alphas[c].meta_sets[0].combine));
        MetaCombineInfo info;
        EXPECT_TRUE(cache.info(keys[c], &info));
        EXPECT_EQ(info.first_estimated_bar, 5u + c);
    }
    // Candidates hit the cache; a fresh lazy compute of one spec gives the same bits.
    bool computed = false;
    const auto cached = cache.get(keys[1], [&] { computed = true; return MetaMatrix{}; });
    EXPECT_FALSE(computed);
    auto again = compile_xml(R"(<meta expr="s"/><mvo days="6"/>)", &dp, nullptr, &resolver);
    MetaSetCache lazy_cache(2);
    const MetaCombineContext lazy{dp, run_config(dp, 2), "settings", returns, lazy_cache, never};
    const MetaMatrix lazy_matrix = materialize_combined(lazy, again[0].meta_sets[0]);
    EXPECT_TRUE(same_bits(lazy_matrix, *cached));
    EXPECT_EQ(lazy_cache.stats().member_runs, kMembers.size());  // returns came from the shared cache
    // A second prefetch of the same round finds everything cached.
    auto round_two = compile_xml(R"(<meta expr="s"/><mvo days="5|6|7"/>)", &dp, nullptr, &resolver);
    EXPECT_TRUE(prefetch_meta_sets(context, round_two).empty());
    EXPECT_EQ(cache.stats().materializations, 1u);
    cache.evict(keys);
    EXPECT_FALSE(cache.contains(keys[0]));

    // A spec error is stored for its key only, with the set name prefixed.
    auto bad = compile_xml(R"(<meta expr="s"/><spca days="5" k="1|4"/>)", &dp, nullptr, &resolver);
    const auto bad_keys = prefetch_meta_sets(context, bad);
    ASSERT_EQ(bad_keys.size(), 2u);
    EXPECT_NO_THROW((void)cache.get(bad_keys[0], [] { return MetaMatrix{}; }));
    try {
        (void)cache.get(bad_keys[1], [] { return MetaMatrix{}; });
        ADD_FAILURE() << "expected the spec error";
    } catch (const std::invalid_argument& error) {
        EXPECT_STREQ(error.what(), "meta 's' <spca>: k=4 exceeds the member count 3");
    }
}

TEST(MetaSets, WeightedStopsAtSimEnd) {
    const auto dp = make_panel();
    const auto never = [] { return false; };
    auto config = run_config(dp, 2);
    config.member_config.sim_end_index = 30;
    auto ew_set = compile_set(dp, kMembers);
    const MetaMatrix ew = materialize_meta_set(dp, ew_set, config, never);
    // Weight rows cover the trading period only (T = min(data.T, sim_end)).
    const CombineWeights ones = constant_weights(30, std::vector<double>(3, 1.0));
    auto set = compile_set(dp, kMembers);
    const auto out = materialize_weighted(dp, set, config, {&ones}, never);
    ASSERT_EQ(out.size(), 1u);
    EXPECT_TRUE(same_bits(out[0], ew));  // weight 1 everywhere: the EW sum, rows >= sim_end as EW leaves them
    std::size_t finite_after = 0;
    for (unsigned t = 30; t < dp.T; ++t)
        for (float v : out[0][t]) finite_after += std::isfinite(v);
    EXPECT_EQ(finite_after, 0u);
}

TEST(MetaSets, WeightedNearEwWithThirdWeights) {
    const auto dp = make_panel();
    const auto never = [] { return false; };
    auto ew_set = compile_set(dp, kMembers);
    const auto ew = executed_weights(dp, materialize_meta_set(dp, ew_set, run_config(dp, 1), never));
    const CombineWeights third = constant_weights(dp.T, std::vector<double>(3, 1.0 / 3.0));
    auto set = compile_set(dp, kMembers);
    const auto got = executed_weights(dp, materialize_weighted(dp, set, run_config(dp, 3), {&third}, never)[0]);
    ASSERT_EQ(got.size(), ew.size());
    ASSERT_GT(got.size(), 0u);
    for (std::size_t t = 0; t < got.size(); ++t)
        for (std::size_t i = 0; i < got[t].size(); ++i) EXPECT_NEAR(got[t][i], ew[t][i], 1e-6) << t << "," << i;
}

TEST(MetaSets, PrefetchTwentySpecsInOnePass) {
    const auto dp = make_panel(60, 5);
    const auto never = [] { return false; };
    MetaResolver resolver = [&](const std::string&) { return kMembers; };
    std::string days;
    for (int d = 5; d < 25; ++d) days += (days.empty() ? "" : "|") + std::to_string(d);
    auto alphas = compile_xml("<meta expr=\"s\"/><mvo days=\"" + days + "\"/>", &dp, nullptr, &resolver);
    ASSERT_EQ(alphas.size(), 20u);
    MetaSetCache cache(2);
    MemberReturnsCache returns;
    const MetaCombineContext context{dp, run_config(dp, 2), "settings", returns, cache, never};
    const auto keys = prefetch_meta_sets(context, alphas);
    ASSERT_EQ(keys.size(), 20u);
    EXPECT_EQ(cache.stats().materializations, 1u);
    EXPECT_EQ(cache.stats().member_runs, 2 * kMembers.size());  // returns + one weighted pass
    // The last spec's matrix equals a lazy compute of the same spec.
    auto again = compile_xml(R"(<meta expr="s"/><mvo days="24"/>)", &dp, nullptr, &resolver);
    MetaSetCache lazy_cache(2);
    const MetaCombineContext lazy{dp, run_config(dp, 2), "settings", returns, lazy_cache, never};
    const auto cached = cache.get(keys[19], [] { return MetaMatrix{}; });
    EXPECT_TRUE(same_bits(materialize_combined(lazy, again[0].meta_sets[0]), *cached));
}
