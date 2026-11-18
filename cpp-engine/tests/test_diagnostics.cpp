// Diagnostics output (output="diagnostics"): pure functions and the daily accumulator on
// hand-computed panels. Oracles are direct formulas written out per case.
#include <gtest/gtest.h>
#include <cmath>
#include <limits>
#include <random>
#include <string>
#include <vector>
#include <json/json.h>
#include "diagnostics.h"

using namespace diagnostics;

static const float NaNf = std::numeric_limits<float>::quiet_NaN();
static const double NaN = std::numeric_limits<double>::quiet_NaN();

static Json::Value parse(const std::string& text) {
    Json::Value v;
    Json::CharReaderBuilder b;
    std::string err;
    std::unique_ptr<Json::CharReader> r(b.newCharReader());
    EXPECT_TRUE(r->parse(text.data(), text.data() + text.size(), &v, &err)) << err;
    return v;
}

static double plain_pearson(const std::vector<double>& x, const std::vector<double>& y) {
    double mx = 0, my = 0;
    for (size_t i = 0; i < x.size(); ++i) { mx += x[i]; my += y[i]; }
    mx /= x.size(); my /= y.size();
    double a = 0, b = 0, c = 0;
    for (size_t i = 0; i < x.size(); ++i) {
        a += (x[i] - mx) * (y[i] - my); b += (x[i] - mx) * (x[i] - mx); c += (y[i] - my) * (y[i] - my);
    }
    return a / std::sqrt(b * c);
}

TEST(DiagnosticsPure, AverageRanksWithTies) {
    const auto r = average_ranks({3.0, 1.0, 3.0, 2.0, 3.0});
    const std::vector<double> want{4.0, 1.0, 4.0, 2.0, 4.0};
    EXPECT_EQ(r, want);
}

TEST(DiagnosticsPure, PearsonAndDegenerate) {
    EXPECT_NEAR(pearson({1, 2, 3, 4}, {2, 4, 6, 8}), 1.0, 1e-15);
    EXPECT_NEAR(pearson({1, 2, 3, 4}, {1, 3, 2, 4}), 0.8, 1e-15);
    EXPECT_TRUE(std::isnan(pearson({1, 1, 1}, {1, 2, 3})));
    EXPECT_TRUE(std::isnan(pearson({1}, {1})));
}

TEST(DiagnosticsPure, QuantileBuckets) {
    // 6 values, Q=3: ranks 1..6, p = r/6, q = ceil(3p) -> buckets {1,2},{3,4},{5,6}
    const auto m = quantile_means({10, 20, 30, 40, 50, 60}, {1, 2, 3, 4, 5, 6}, 3);
    ASSERT_EQ(m.size(), 3u);
    EXPECT_DOUBLE_EQ(m[0], 1.5);
    EXPECT_DOUBLE_EQ(m[1], 3.5);
    EXPECT_DOUBLE_EQ(m[2], 5.5);
    // Ties: values {1,1,1,1} -> ranks 2.5, p = 0.625, q = ceil(1.875) = 2 for Q=3; buckets 1 and 3 empty.
    const auto t = quantile_means({1, 1, 1, 1}, {1, 2, 3, 4}, 3);
    EXPECT_TRUE(std::isnan(t[0]));
    EXPECT_DOUBLE_EQ(t[1], 2.5);
    EXPECT_TRUE(std::isnan(t[2]));
}

TEST(DiagnosticsPure, NeweyWestKnownSeries) {
    // x = {1,2,3,4}: m = 2.5, gamma0 = 5/4, gamma1 = (-1.5*-0.5 + -0.5*0.5 + 0.5*1.5)/4 = 1.25/4,
    // lag 1 weight 1/2: var = 1.25 + 2*0.5*0.3125 = 1.5625; t = 2.5 / sqrt(1.5625/4) = 4.
    EXPECT_NEAR(newey_west_t({1, 2, 3, 4}, 1), 4.0, 1e-12);
    // lag 0: var = gamma0 = 1.25, t = 2.5/sqrt(1.25/4)
    EXPECT_NEAR(newey_west_t({1, 2, 3, 4}, 0), 2.5 / std::sqrt(1.25 / 4), 1e-12);
    // lag 5 on n=4 truncates at n-1 = 3 with weights 1 - l/6.
    const double m = 2.5;
    std::vector<double> x{1, 2, 3, 4};
    double var = 0;
    for (int l = 0; l <= 3; ++l) {
        double g = 0;
        for (int t = l; t < 4; ++t) g += (x[t] - m) * (x[t - l] - m);
        g /= 4;
        var += l == 0 ? g : 2 * (1 - l / 6.0) * g;
    }
    EXPECT_NEAR(newey_west_t(x, 5), m / std::sqrt(var / 4), 1e-12);
}

TEST(DiagnosticsPure, SummaryAndQuantile) {
    const std::vector<double> v{0.1, NaN, -0.2, 0.3, 0.4};
    const auto s = summarize(v, {0, 1, 2, 3}, 5);
    EXPECT_EQ(s.n, 3u);
    EXPECT_EQ(s.first, 0u);
    EXPECT_EQ(s.last, 3u);
    EXPECT_NEAR(s.mean, 0.2 / 3, 1e-15);
    EXPECT_NEAR(s.positive, 2.0 / 3, 1e-15);
    EXPECT_NEAR(s.t, s.mean / s.std * std::sqrt(3.0), 1e-12);
    EXPECT_DOUBLE_EQ(sorted_quantile({1, 2, 3, 4}, 0.5), 2.5);
    EXPECT_DOUBLE_EQ(sorted_quantile({1, 2, 3, 4}, 0.0), 1.0);
    EXPECT_DOUBLE_EQ(sorted_quantile({1, 2, 3, 4}, 1.0), 4.0);
}

TEST(DiagnosticsPure, NullReproducibleAndActual) {
    std::mt19937_64 rng(7);
    std::normal_distribution<double> nd;
    const size_t D = 40, N = 50;
    std::vector<std::vector<float>> sig(D, std::vector<float>(N)), ret(D, std::vector<float>(N));
    for (size_t j = 0; j < D; ++j)
        for (size_t i = 0; i < N; ++i) { sig[j][i] = static_cast<float>(nd(rng)); ret[j][i] = sig[j][i] + static_cast<float>(nd(rng)); }
    const auto a = circular_shift_null(sig, ret, 30, 5, 123, 30, 4);
    const auto b = circular_shift_null(sig, ret, 30, 5, 123, 30, 1);
    ASSERT_TRUE(a.ok);
    EXPECT_EQ(a.values, b.values);
    EXPECT_EQ(a.p, b.p);
    double sum = 0;
    for (size_t j = 0; j < D; ++j)
        sum += plain_pearson(std::vector<double>(sig[j].begin(), sig[j].end()), std::vector<double>(ret[j].begin(), ret[j].end()));
    EXPECT_NEAR(a.actual, sum / D, 1e-12);
    EXPECT_GT(a.actual, 0.5);
    EXPECT_NEAR(a.p, 1.0 / 31.0, 1e-15);  // no shift beats a strong same-date relation
    const auto c = circular_shift_null(sig, ret, 30, 5, 124, 30, 2);
    EXPECT_NE(a.values, c.values);
    const auto short_run = circular_shift_null(sig, ret, 30, 21, 1, 30, 1);
    EXPECT_FALSE(short_run.ok);
}

TEST(DiagnosticsOptions, Errors) {
    EXPECT_THROW(parse_options(parse(R"({"isEnd":"2024-12-31"})")), std::invalid_argument);
    EXPECT_THROW(parse_options(parse(R"({"horizons":[1]})")), std::invalid_argument);
    EXPECT_THROW(parse_options(parse(R"({"horizons":[],"isEnd":"2024-12-31"})")), std::invalid_argument);
    EXPECT_THROW(parse_options(parse(R"({"horizons":[0],"isEnd":"2024-12-31"})")), std::invalid_argument);
    EXPECT_THROW(parse_options(parse(R"({"horizons":[253],"isEnd":"2024-12-31"})")), std::invalid_argument);
    EXPECT_THROW(parse_options(parse(R"({"horizons":[1,5,1],"isEnd":"2024-12-31"})")), std::invalid_argument);
    EXPECT_THROW(parse_options(parse(R"({"horizons":[1.5],"isEnd":"2024-12-31"})")), std::invalid_argument);
    EXPECT_THROW(parse_options(parse(R"({"horizons":[1],"isEnd":"2024-12-31","bogus":1})")), std::invalid_argument);
    EXPECT_THROW(parse_options(parse(R"({"horizons":[1],"isEnd":"2024-12-31","groups":["subindustry"]})")), std::invalid_argument);
    EXPECT_THROW(parse_options(parse(R"({"horizons":[1],"isEnd":"2024-12-31","null":{"n":0}})")), std::invalid_argument);
    EXPECT_THROW(parse_options(parse(R"({"horizons":[1],"isEnd":"2024-12-31","distributionQuantiles":[1.5]})")), std::invalid_argument);
    EXPECT_THROW(parse_options(Json::Value()), std::invalid_argument);
    const auto o = parse_options(parse(R"({"horizons":[5,1],"isEnd":"2024-12-31","null":{"seed":3}})"));
    EXPECT_EQ(o.horizons, (std::vector<int>{5, 1}));
    EXPECT_EQ(o.quantiles, 5);
    EXPECT_EQ(o.topk, (std::vector<int>{1, 10}));
    EXPECT_TRUE(o.null_enabled);
    EXPECT_EQ(o.null_n, 200);
    EXPECT_EQ(o.null_seed, 3u);
}

// Panel helper: T bars x N assets, signal s[t][i], returns r[t][i] (bar t total return).
struct Panel {
    unsigned T, N;
    std::vector<std::vector<float>> s;
    std::vector<std::vector<double>> r;
    std::vector<std::vector<uint8_t>> m;
    std::vector<std::vector<float>> codes, cond;
};

static Json::Value run(const Panel& p, const Options& o, Layout layout) {
    layout.N = p.N;
    Accumulator acc(o, layout);
    for (unsigned di = layout.first; di < layout.end; ++di) {
        std::vector<const float*> codes;
        if (!o.groups.empty()) codes.push_back(p.codes[di].data());
        acc.observe(di, p.s[di].data(), p.m.empty() ? nullptr : p.m[di].data(), p.r[di].data(), codes,
                    o.condition_on.empty() ? nullptr : p.cond[di].data());
    }
    std::vector<int64_t> ts(p.T);
    for (unsigned t = 0; t < p.T; ++t) ts[t] = 1000 * t;
    return acc.finish(ts, 2);
}

static Panel random_panel(unsigned T, unsigned N, uint64_t seed) {
    std::mt19937_64 rng(seed);
    std::normal_distribution<double> nd;
    Panel p{T, N, {}, {}, {}, {}, {}};
    p.s.assign(T, std::vector<float>(N));
    p.r.assign(T, std::vector<double>(N));
    p.codes.assign(T, std::vector<float>(N));
    p.cond.assign(T, std::vector<float>(N));
    for (unsigned t = 0; t < T; ++t)
        for (unsigned i = 0; i < N; ++i) {
            p.s[t][i] = static_cast<float>(std::round(nd(rng) * 4) / 4);  // ties
            p.r[t][i] = 0.01 * nd(rng);
            p.codes[t][i] = static_cast<float>(i % 2);
            p.cond[t][i] = static_cast<float>(i);
        }
    return p;
}

static double fwd(const Panel& p, unsigned t, unsigned h, unsigned i) {
    double g = 1;
    for (unsigned k = 1; k <= h; ++k) {
        if (!std::isfinite(p.r[t + k][i])) return NaN;
        g *= 1 + p.r[t + k][i];
    }
    return g - 1;
}

TEST(DiagnosticsAccumulator, IcRankIcSpreadNanAndPurge) {
    const unsigned T = 12, N = 40;
    Panel p = random_panel(T, N, 11);
    p.s[3][0] = NaNf;              // not a target on bar 3
    p.r[5][1] = NaN;               // breaks asset 1 forward windows through bar 5
    p.m.assign(T, std::vector<uint8_t>(N, 1));
    p.m[3][2] = 0;                 // non-member on bar 3
    Options o;
    o.horizons = {1, 3};
    o.quantiles = 4;
    Layout L;
    L.first = 2; L.end = T; L.is_end = 8; L.os_start = 9;
    const auto j = run(p, o, L);
    ASSERT_EQ(j["timestamps"].size(), T - 2);
    for (unsigned h : {1u, 3u}) {
        const auto& hj = j["horizons"][std::to_string(h)];
        for (unsigned t = 2; t < T; ++t) {
            const unsigned d = t - 2;
            if (t + h >= T) {  // horizon end: no forward window
                EXPECT_TRUE(hj["ic"][d].isNull());
                EXPECT_EQ(hj["n"][d].asInt(), 0);
                continue;
            }
            std::vector<double> s, f;
            for (unsigned i = 0; i < N; ++i) {
                if (!std::isfinite(p.s[t][i]) || !p.m[t][i]) continue;
                const double fr = fwd(p, t, h, i);
                if (!std::isfinite(fr)) continue;
                s.push_back(p.s[t][i]); f.push_back(fr);
            }
            ASSERT_EQ(hj["n"][d].asInt(), static_cast<int>(s.size())) << "t=" << t << " h=" << h;
            EXPECT_NEAR(hj["ic"][d].asDouble(), plain_pearson(s, f), 1e-9);
            EXPECT_NEAR(hj["rankic"][d].asDouble(), plain_pearson(average_ranks(s), average_ranks(f)), 1e-9);
            const auto qm = quantile_means(s, f, 4);
            EXPECT_NEAR(hj["spread"][d].asDouble(), qm[3] - qm[0], 1e-9);
            EXPECT_NEAR(hj["quantileMeans"][0][d].asDouble(), qm[0], 1e-9);
        }
        // IS: t in [2, is_end - h]; OS: t >= 9 with a forward window.
        const unsigned is_n = 8 - h - 2 + 1;
        EXPECT_EQ(hj["is"]["ic"]["n"].asUInt(), is_n) << h;
        EXPECT_EQ(hj["is"]["ic"]["lastTimestamp"].asInt64(), 1000 * (8 - h));
        EXPECT_EQ(hj["is"]["hacLag"].asInt(), 5);
        EXPECT_EQ(hj["os"]["ic"]["n"].asUInt(), (T - h) - 9);
        EXPECT_FALSE(hj["os"].isMember("t"));
    }
    // Asset 1 window through bar 5 excluded for h=1 at t=4.
    EXPECT_EQ(j["horizons"]["1"]["n"][2].asInt(), 39);
    // Bar 3 coverage: 38 targets of 39 members.
    EXPECT_NEAR(j["coverage"][1].asDouble(), 38.0 / 39.0, 1e-15);
}

TEST(DiagnosticsAccumulator, SmallSampleIsNaN) {
    Panel p = random_panel(6, 29, 5);
    Options o;
    o.horizons = {1};
    Layout L;
    L.first = 0; L.end = 6; L.is_end = 5; L.os_start = 6;
    const auto j = run(p, o, L);
    EXPECT_EQ(j["horizons"]["1"]["n"][0].asInt(), 29);
    EXPECT_TRUE(j["horizons"]["1"]["rankic"][0].isNull());
    EXPECT_EQ(j["horizons"]["1"]["is"]["rankic"]["n"].asUInt(), 0u);
}

TEST(DiagnosticsAccumulator, TotalLossForwardReturn) {
    Panel p = random_panel(5, 30, 9);
    p.r[2][0] = -1.0;
    p.r[3][0] = 0.5;
    Options o;
    o.horizons = {2};
    Layout L;
    L.first = 0; L.end = 5; L.is_end = 4; L.os_start = 5;
    const auto j = run(p, o, L);
    std::vector<double> s, f;
    for (unsigned i = 0; i < 30; ++i) { s.push_back(p.s[1][i]); f.push_back(fwd(p, 1, 2, i)); }
    EXPECT_DOUBLE_EQ(f[0], -1.0);
    EXPECT_NEAR(j["horizons"]["2"]["ic"][1].asDouble(), plain_pearson(s, f), 1e-9);
}

TEST(DiagnosticsAccumulator, TopkGroupsConditionDistributionAutocorr) {
    const unsigned T = 10, N = 120;
    Panel p = random_panel(T, N, 21);
    Options o;
    o.horizons = {5};             // h=1 added internally for groups / condition
    o.topk = {1, 3};
    o.groups = {"sector"};
    o.condition_on = "c";
    o.condition_buckets = 2;
    o.quantiles = 2;
    o.autocorr_lags = {1};
    o.distribution_quantiles = {0.0, 0.5, 1.0};
    Layout L;
    L.first = 1; L.end = T; L.is_end = 6; L.os_start = 7;
    const auto j = run(p, o, L);
    EXPECT_FALSE(j["horizons"].isMember("1"));
    // top-k on bar 1
    std::vector<double> a;
    double tot = 0;
    for (unsigned i = 0; i < N; ++i) { a.push_back(std::fabs(p.s[1][i])); tot += a.back(); }
    std::sort(a.rbegin(), a.rend());
    EXPECT_NEAR(j["topk"]["1"][0].asDouble(), a[0] / tot, 1e-12);
    EXPECT_NEAR(j["topk"]["3"][0].asDouble(), (a[0] + a[1] + a[2]) / tot, 1e-12);
    // group IC: code 1 (odd assets), h = 1, IS t in [1, 5]
    double gsum = 0;
    std::vector<double> gx;
    for (unsigned t = 1; t <= 5; ++t) {
        std::vector<double> s, f;
        for (unsigned i = 1; i < N; i += 2) { s.push_back(p.s[t][i]); f.push_back(fwd(p, t, 1, i)); }
        gx.push_back(plain_pearson(average_ranks(s), average_ranks(f)));
        gsum += gx.back();
    }
    const auto& g1 = j["groups"]["sector"]["rankicH1"]["1"];
    EXPECT_EQ(g1["n"].asUInt(), 5u);
    EXPECT_NEAR(g1["mean"].asDouble(), gsum / 5, 1e-12);
    EXPECT_NEAR(g1["hacT"].asDouble(), newey_west_t(gx, 5), 1e-9);
    EXPECT_DOUBLE_EQ(g1["meanSize"].asDouble(), 60.0);
    // condition on asset index: bucket 1 = assets 0..59, bucket 2 = 60..119
    {
        const unsigned t = 3;
        std::vector<double> s, f;
        for (unsigned i = 60; i < N; ++i) { s.push_back(p.s[t][i]); f.push_back(fwd(p, t, 1, i)); }
        EXPECT_NEAR(j["condition"]["rankic"][1][t - 1].asDouble(), plain_pearson(average_ranks(s), average_ranks(f)), 1e-12);
        const auto qm = quantile_means(s, f, 2);
        EXPECT_NEAR(j["condition"]["spread"][1][t - 1].asDouble(), qm[1] - qm[0], 1e-12);
    }
    EXPECT_EQ(j["condition"]["is"][0]["rankic"]["n"].asUInt(), 5u);
    // distribution: IS bars 1..6, min/median/max mean
    double mn = 0, md = 0;
    for (unsigned t = 1; t <= 6; ++t) {
        std::vector<double> v(p.s[t].begin(), p.s[t].end());
        std::sort(v.begin(), v.end());
        mn += v.front(); md += sorted_quantile(v, 0.5);
    }
    EXPECT_NEAR(j["distribution"]["isMean"][0].asDouble(), mn / 6, 1e-12);
    EXPECT_NEAR(j["distribution"]["isMean"][1].asDouble(), md / 6, 1e-12);
    EXPECT_EQ(j["distribution"]["days"].asUInt(), 6u);
    EXPECT_NEAR(j["distribution"]["missingRatioIsMean"].asDouble(), 0.0, 1e-15);
    // autocorr lag 1: IS t in [2, 6]
    double ac = 0;
    for (unsigned t = 2; t <= 6; ++t) {
        std::vector<double> x(p.s[t].begin(), p.s[t].end()), y(p.s[t - 1].begin(), p.s[t - 1].end());
        ac += plain_pearson(average_ranks(x), average_ranks(y));
    }
    EXPECT_EQ(j["autocorr"]["1"]["n"].asUInt(), 5u);
    EXPECT_NEAR(j["autocorr"]["1"]["isMean"].asDouble(), ac / 5, 1e-12);
}

TEST(DiagnosticsAccumulator, NullFromAccumulatorIsReproducible) {
    Panel p = random_panel(80, 40, 31);
    for (unsigned t = 0; t + 1 < 80; ++t)
        for (unsigned i = 0; i < 40; ++i) p.s[t][i] = static_cast<float>(p.r[t + 1][i] * 100);  // perfect h=1 signal
    Options o;
    o.horizons = {1};
    o.null_enabled = true;
    o.null_n = 20;
    o.null_min_shift = 10;
    o.null_seed = 5;
    Layout L;
    L.first = 0; L.end = 80; L.is_end = 70; L.os_start = 71;
    const auto a = run(p, o, L), b = run(p, o, L);
    EXPECT_EQ(a["null"]["dates"].asUInt(), 70u);  // t in [0, 69]
    EXPECT_NEAR(a["null"]["actual"].asDouble(), 1.0, 1e-6);
    EXPECT_EQ(a["null"]["nullMean"].asDouble(), b["null"]["nullMean"].asDouble());
    EXPECT_NEAR(a["null"]["p"].asDouble(), 1.0 / 21.0, 1e-15);
}
