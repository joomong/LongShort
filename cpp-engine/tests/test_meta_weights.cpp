#include <gtest/gtest.h>
#include <atomic>
#include <chrono>
#include <future>
#include <cmath>
#include <limits>
#include <random>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>
#include "api/meta_returns.h"
#include "api/meta_weights.h"
#include "compiler/dag_builder.h"
#include "data/data_panel.h"
#include "engine/simulator.h"

namespace {

const double kNaN = std::numeric_limits<double>::quiet_NaN();

MemberReturns random_returns(unsigned T, unsigned K, unsigned sim_start, unsigned seed = 7) {
    std::mt19937 rng(seed);
    std::normal_distribution<double> noise(0.0, 0.01);
    MemberReturns r;
    r.T = T;
    r.K = K;
    r.sim_start = sim_start;
    for (unsigned k = 0; k < K; ++k) r.paths.push_back("m" + std::to_string(k) + ".xml");
    r.rows.assign(T, std::vector<double>(K, kNaN));
    for (unsigned t = sim_start; t < T; ++t) {
        const double common = noise(rng);
        for (unsigned k = 0; k < K; ++k)
            r.rows[t][k] = 0.0005 * (1.0 + k) * (k % 3 == 2 ? -1.0 : 1.0) + 0.5 * common +
                           noise(rng) * (1.0 + 0.3 * k);
    }
    return r;
}

CombineSpec spec_of(CombineMethod method, unsigned days, unsigned rebalance = 1) {
    CombineSpec s;
    s.method = method;
    s.days = days;
    s.rebalance = rebalance;
    s.k = 1;
    s.alpha = 0.1f;
    return s;
}

std::vector<CombineSpec> all_specs(unsigned days) {
    std::vector<CombineSpec> out;
    for (CombineMethod m : {CombineMethod::EwSign, CombineMethod::Ivol, CombineMethod::Erc, CombineMethod::Spca,
                            CombineMethod::Glasso})
        out.push_back(spec_of(m, days));
    for (CombineMethod m : {CombineMethod::Mvo, CombineMethod::MinVar})
        for (ShrinkMode s : {ShrinkMode::Diag, ShrinkMode::LedoitWolf, ShrinkMode::ConstantCorrelation, ShrinkMode::Oas}) {
            CombineSpec spec = spec_of(m, days);
            spec.shrink = s;
            out.push_back(spec);
        }
    return out;
}

}  // namespace

TEST(MetaWeights, EwBeforeWindowAndFallbackCount) {
    MemberReturns r = random_returns(60, 3, 5);
    r.rows[30][1] = kNaN;
    const CombineWeights w = compute_combine_weights(r, spec_of(CombineMethod::Mvo, 10));
    EXPECT_EQ(w.first_estimated_bar, 15u);
    for (unsigned t = 0; t < 15; ++t)
        for (double v : w.rows[t]) EXPECT_EQ(v, 1.0 / 3.0);
    // Windows [t-10, t-1] containing bar 30: t = 31..40.
    EXPECT_EQ(w.fallback_bars, 10u);
    for (unsigned t = 31; t <= 40; ++t)
        for (double v : w.rows[t]) EXPECT_EQ(v, 1.0 / 3.0);
    EXPECT_NE(w.rows[41], w.rows[0]);
}

TEST(MetaWeights, NoEstimableBarWhenWindowFillsTradingPeriod) {
    const MemberReturns r = random_returns(30, 2, 10);
    const CombineWeights w = compute_combine_weights(r, spec_of(CombineMethod::Ivol, 20));
    EXPECT_EQ(w.first_estimated_bar, 30u);
    EXPECT_EQ(w.fallback_bars, 0u);
}

TEST(MetaWeights, FlatMemberFallsBackForEveryEstimator) {
    MemberReturns r = random_returns(80, 5, 0);
    for (auto& row : r.rows) row[0] = 0.0;
    std::vector<CombineSpec> specs = all_specs(30);
    for (const CombineSpec& spec : specs) {
        const CombineWeights w = compute_combine_weights(r, spec);
        if (spec.method == CombineMethod::EwSign) {
            EXPECT_EQ(w.fallback_bars, 0u);
            for (unsigned t = 30; t < 80; ++t) EXPECT_EQ(w.rows[t][0], 0.0);
            continue;
        }
        EXPECT_EQ(w.fallback_bars, 50u) << spec.key();
        for (unsigned t = 30; t < 80; ++t)
            for (double v : w.rows[t]) EXPECT_EQ(v, 0.2) << spec.key();
    }
}

TEST(MetaWeights, RebalanceCarriesRows) {
    const MemberReturns r = random_returns(80, 4, 0);
    const CombineWeights w = compute_combine_weights(r, spec_of(CombineMethod::Ivol, 20, 5));
    for (unsigned t = 20; t < 80; ++t) {
        if ((t - 20) % 5 == 0) {
            if (t > 20) EXPECT_NE(w.rows[t], w.rows[t - 1]) << t;
        } else {
            EXPECT_EQ(w.rows[t], w.rows[t - 1]) << t;
        }
    }
}

TEST(MetaWeights, IdenticalMembersGiveEw) {
    MemberReturns r = random_returns(80, 4, 0);
    for (auto& row : r.rows) row.assign(4, row[0] + 0.02);  // identical members, positive drift
    for (CombineMethod m : {CombineMethod::Mvo, CombineMethod::MinVar, CombineMethod::Erc, CombineMethod::Ivol,
                            CombineMethod::Spca, CombineMethod::Glasso}) {
        const CombineWeights w = compute_combine_weights(r, spec_of(m, 30));
        EXPECT_EQ(w.fallback_bars, 0u) << CombineSpec::method_name(m);
        // GLasso stops at sklearn's dual-gap tolerance (1e-3), so its cyclic sweep is EW only to ~1e-4.
        const double tolerance = m == CombineMethod::Glasso ? 1e-4 : 1e-9;
        for (unsigned t = 30; t < 80; ++t)
            for (double v : w.rows[t]) EXPECT_NEAR(v, 0.25, tolerance) << CombineSpec::method_name(m) << " t=" << t;
    }
}

TEST(MetaWeights, NoLookAhead) {
    const MemberReturns base = random_returns(90, 5, 3);
    for (const CombineSpec& spec : all_specs(25)) {
        const CombineWeights expected = compute_combine_weights(base, spec);
        for (unsigned cut : {40u, 61u}) {
            MemberReturns changed = base;
            for (unsigned t = cut; t < changed.T; ++t)
                for (auto& v : changed.rows[t]) v = -3.0 * v + 0.01;
            const CombineWeights got = compute_combine_weights(changed, spec);
            for (unsigned t = 0; t <= cut; ++t) EXPECT_EQ(got.rows[t], expected.rows[t]) << spec.key() << " t=" << t;
        }
    }
}

TEST(MetaWeights, ClosedForms) {
    // Two members with exactly uncorrelated centered windows.
    const std::vector<std::vector<double>> window = {
        {0.03, 0.01 + 0.02}, {-0.01, 0.01 + 0.02}, {0.03, 0.01 - 0.02}, {-0.01, 0.01 - 0.02}};
    // means 0.01 / 0.01, sd (ddof 1): a = 0.02*sqrt(4/3), b = 0.02*sqrt(4/3)... make them differ:
    std::vector<std::vector<double>> win = window;
    for (auto& row : win) row[1] = 0.01 + (row[1] - 0.01) * 2.0;  // b deviations doubled, still uncorrelated
    const double sa = std::sqrt(4 * 0.02 * 0.02 / 3.0), sb = 2.0 * sa;
    const double ma = 0.01, mb = 0.01;
    std::vector<double> w;

    ASSERT_TRUE(weights_for_window(win, spec_of(CombineMethod::Ivol, 4), w));
    EXPECT_NEAR(w[0] / w[1], sb / sa, 1e-12);
    ASSERT_TRUE(weights_for_window(win, spec_of(CombineMethod::Erc, 4), w));
    EXPECT_NEAR(w[0] / w[1], sb / sa, 1e-8);
    CombineSpec mvo = spec_of(CombineMethod::Mvo, 4);
    mvo.delta = 1.0f;
    ASSERT_TRUE(weights_for_window(win, mvo, w));
    EXPECT_NEAR(w[0] / w[1], (ma / (sa * sa)) / (mb / (sb * sb)), 1e-10);
    CombineSpec minvar = spec_of(CombineMethod::MinVar, 4);
    minvar.delta = 0.0f;  // the sample covariance itself is diagonal here
    ASSERT_TRUE(weights_for_window(win, minvar, w));
    EXPECT_NEAR(w[0] / w[1], (sb * sb) / (sa * sa), 1e-10);

    std::vector<std::vector<double>> signs = {{0.01, -0.02, 0.0}, {0.02, -0.01, 0.0}};
    ASSERT_TRUE(weights_for_window(signs, spec_of(CombineMethod::EwSign, 2), w));
    EXPECT_EQ(w, (std::vector<double>{0.5, -0.5, 0.0}));

    const std::vector<double> cov = shrink_covariance(win, mvo);
    EXPECT_NEAR(cov[0], sa * sa, 1e-15);
    EXPECT_EQ(cov[1], 0.0);
}

TEST(MetaWeights, NormalizationAndNegativeWeights) {
    const MemberReturns r = random_returns(120, 6, 0);
    bool negative = false;
    for (const CombineSpec& spec : all_specs(40)) {
        const CombineWeights w = compute_combine_weights(r, spec);
        EXPECT_EQ(w.fallback_bars, 0u) << spec.key();
        for (unsigned t = w.first_estimated_bar; t < r.T; ++t) {
            double gross = 0.0;
            for (double v : w.rows[t]) {
                gross += std::fabs(v);
                if (v < 0) negative = true;
            }
            EXPECT_NEAR(gross, 1.0, 1e-12) << spec.key();
        }
    }
    EXPECT_TRUE(negative);
}

TEST(MetaWeights, SpecErrors) {
    const MemberReturns small = random_returns(50, 8, 0);
    CombineSpec spca = spec_of(CombineMethod::Spca, 10);
    spca.k = 12;
    try {
        (void)compute_combine_weights(small, spca);
        FAIL();
    } catch (const std::invalid_argument& e) {
        EXPECT_STREQ(e.what(), "<spca>: k=12 exceeds the member count 8");
    }
    MemberReturns longer = random_returns(2700, 2, 10);
    try {
        (void)compute_combine_weights(longer, spec_of(CombineMethod::Mvo, 3000));
        FAIL();
    } catch (const std::invalid_argument& e) {
        EXPECT_STREQ(e.what(), "<mvo>: days=3000 exceeds the trading bars 2690");
    }
}

namespace {

DataPanel make_panel(unsigned T = 40, unsigned N = 5) {
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

}  // namespace

TEST(MetaWeights, ReturnsCollectionMatchesSimulatorRun) {
    const DataPanel dp = make_panel();
    const std::vector<std::pair<std::string, std::string>> members = {
        {"a.xml", R"(<data expr="close" delay="1"/><rank/>)"},
        {"b.xml", R"(<data expr="close" delay="1"/><tsmean days="5"/><zscore/>)"},
    };
    MetaResolver resolver = [&](const std::string&) { return members; };
    auto alphas = compile_xml(R"(<meta expr="s"/>)", &dp, nullptr, &resolver);
    ASSERT_EQ(alphas.size(), 1u);
    ASSERT_EQ(alphas[0].meta_sets.size(), 1u);
    const MetaSet& set = alphas[0].meta_sets[0];

    SimulatorConfig sim;
    sim.T = dp.T;
    sim.N = dp.N;
    sim.fee_rate = 0.001;
    sim.sim_start_index = 3;
    const MetaRunConfig config{sim, 2, MissingClosePolicy::Carry, nullptr};
    const MemberReturns got = collect_member_returns(dp, set, config, [] { return false; });
    ASSERT_EQ(got.T, dp.T);
    ASSERT_EQ(got.K, 2u);
    EXPECT_EQ(got.sim_start, 3u);
    EXPECT_EQ(got.paths, (std::vector<std::string>{"a.xml", "b.xml"}));
    for (std::size_t k = 0; k < members.size(); ++k) {
        auto own = compile_xml(members[k].second, &dp);
        const SimulatorOutput out = Simulator(dp, sim).run(own[0].pipeline);
        for (unsigned t = 0; t < dp.T; ++t) {
            if (t < 3)
                EXPECT_TRUE(std::isnan(got.rows[t][k]));
            else
                EXPECT_EQ(got.rows[t][k], out.gross_ret[t]) << k << " " << t;
        }
    }
    // Members are kept for the weighted pass: a second collection gives the same rows.
    const MemberReturns again = collect_member_returns(dp, set, config, [] { return false; });
    for (unsigned t = 3; t < dp.T; ++t) EXPECT_EQ(again.rows[t], got.rows[t]);

    // Trading period ending before the panel end: rows stop at sim_end_index.
    {
        const DataPanel wide = make_panel(150, 5);
        auto wide_alphas = compile_xml(R"(<meta expr="s"/>)", &wide, nullptr, &resolver);
        SimulatorConfig ended = sim;
        ended.T = wide.T;
        ended.N = wide.N;
        ended.sim_start_index = 0;
        ended.sim_end_index = 120;
        const MetaRunConfig ended_config{ended, 2, MissingClosePolicy::Carry, nullptr};
        const MemberReturns cut = collect_member_returns(wide, wide_alphas[0].meta_sets[0], ended_config,
                                                         [] { return false; });
        EXPECT_EQ(cut.T, 120u);
        EXPECT_EQ(cut.rows.size(), 120u);
        const CombineWeights iw = compute_combine_weights(cut, spec_of(CombineMethod::Ivol, 60));
        EXPECT_EQ(iw.fallback_bars, 0u);
        EXPECT_EQ(iw.rows.size(), 120u);
        try {
            (void)compute_combine_weights(cut, spec_of(CombineMethod::Ivol, 125));
            FAIL();
        } catch (const std::invalid_argument& e) {
            EXPECT_STREQ(e.what(), "<ivol>: days=125 exceeds the trading bars 120");
        }
    }

    MemberReturnsCache cache;
    int computes = 0;
    auto first = cache.get("key", [&] { ++computes; return got; });
    auto second = cache.get("key", [&] { ++computes; return got; });
    EXPECT_EQ(computes, 1);
    EXPECT_EQ(first.get(), second.get());
    EXPECT_THROW(cache.get("bad", [&]() -> MemberReturns { ++computes; throw std::runtime_error("boom"); }),
                 std::runtime_error);
    auto retried = cache.get("bad", [&] { ++computes; return got; });
    EXPECT_EQ(computes, 3);
    ASSERT_TRUE(retried);
    // LRU: 16 newer keys evict "key".
    for (int i = 0; i < 16; ++i) cache.get("k" + std::to_string(i), [&] { return got; });
    cache.get("key", [&] { ++computes; return got; });
    EXPECT_EQ(computes, 4);
}

TEST(MetaWeights, ReturnsCacheConcurrentCallers) {
    const MemberReturns value = random_returns(10, 2, 0);
    MemberReturnsCache cache;
    // The first compute blocks until the second caller is inside cache.get, so the second caller
    // must wait on the in-flight entry instead of computing (no timing assumptions on the compute).
    std::atomic<bool> second_computed{false};
    auto run_case = [&](const std::string& key, bool fail, std::shared_ptr<const MemberReturns>& a,
                        std::shared_ptr<const MemberReturns>& b, int& errors) {
        second_computed = false;
        std::promise<void> release;
        std::shared_future<void> released = release.get_future().share();
        std::atomic<bool> started{false}, entering{false};
        std::atomic<int> error_count{0};
        std::thread first([&] {
            try {
                a = cache.get(key, [&]() -> MemberReturns {
                    started = true;
                    released.wait();
                    if (fail) throw std::runtime_error("boom");
                    return value;
                });
            } catch (const std::runtime_error&) { ++error_count; }
        });
        while (!started) std::this_thread::yield();
        std::thread second([&] {
            entering = true;
            try {
                b = cache.get(key, [&]() -> MemberReturns { second_computed = true; return value; });
            } catch (const std::runtime_error&) { ++error_count; }
        });
        while (!entering) std::this_thread::yield();
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
        release.set_value();
        first.join();
        second.join();
        errors = error_count.load();
    };
    std::shared_ptr<const MemberReturns> a, b;
    int errors = 0;
    run_case("k", false, a, b, errors);
    EXPECT_FALSE(second_computed.load()) << "the waiter computed instead of waiting";
    EXPECT_EQ(errors, 0);
    ASSERT_TRUE(a);
    EXPECT_EQ(a.get(), b.get());

    // A failed compute (e.g. the computing request was cancelled) is the first caller's error only:
    // the waiter recomputes with its own callback and succeeds; the result is then cached.
    std::shared_ptr<const MemberReturns> c, d;
    run_case("f", true, c, d, errors);
    EXPECT_EQ(errors, 1);
    EXPECT_FALSE(c);
    EXPECT_TRUE(second_computed.load()) << "the waiter did not recompute after the failure";
    ASSERT_TRUE(d);
    bool recomputed = false;
    auto again = cache.get("f", [&] { recomputed = true; return value; });
    EXPECT_FALSE(recomputed);
    EXPECT_EQ(again.get(), d.get());
    EXPECT_TRUE(again);
}
