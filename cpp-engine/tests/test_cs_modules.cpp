// Cross-sectional and group modules: winsorize, normalize, hump, group_rank,
// group_mean, group_zscore, rank_sharp, truncate. Oracles are plain per-row/per-group loops.
#include <gtest/gtest.h>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <map>
#include <random>
#include <string>
#include <vector>
#include "module.h"
#include "compiler/dag_builder.h"
#include "data/data_panel.h"
#include "modules/Rank.h"
#include "modules/Neutralize.h"
#include "modules/Winsorize.h"
#include "modules/Normalize.h"
#include "modules/Hump.h"
#include "modules/GroupOps.h"
#include "modules/RankSharp.h"
#include "modules/Truncate.h"
#include "signal_vector.h"

static const float NaN = std::numeric_limits<float>::quiet_NaN();
static const float Inf = std::numeric_limits<float>::infinity();

using Row = std::vector<float>;

static bool same(float a, float b)
{
    if (std::isnan(a) || std::isnan(b)) return std::isnan(a) && std::isnan(b);
    return std::memcmp(&a, &b, sizeof(float)) == 0 || a == b;
}

static void expect_rows_equal(const Row& got, const Row& want, const std::string& what)
{
    ASSERT_EQ(got.size(), want.size());
    for (size_t i = 0; i < got.size(); ++i)
        EXPECT_TRUE(same(got[i], want[i]))
            << what << " col " << i << " got " << got[i] << " want " << want[i];
}

// Random panel: values with NaN/inf/ties, codes with NaN/negative, a mask.
struct Fixture {
    unsigned T, N;
    DataPanel dp;
    std::vector<Row> x;
    std::vector<uint8_t> mask;
};

static Fixture make_fixture(unsigned T, unsigned N, unsigned seed)
{
    Fixture f{T, N, {}, {}, {}};
    std::mt19937 rng(seed);
    std::uniform_real_distribution<float> u(-3.0f, 3.0f);
    std::uniform_int_distribution<int> pick(0, 99);
    f.dp.T = T;
    f.dp.N = N;
    auto& sector = f.dp.arrays["sector"];
    sector.resize(static_cast<size_t>(T) * N);
    f.mask.resize(static_cast<size_t>(T) * N);
    for (unsigned t = 0; t < T; ++t) {
        Row r(N);
        for (unsigned n = 0; n < N; ++n) {
            const int p = pick(rng);
            if (p < 8) r[n] = NaN;
            else if (p < 10) r[n] = (p == 8) ? Inf : -Inf;
            else if (p < 25) r[n] = static_cast<float>(pick(rng) % 3);   // ties
            else r[n] = u(rng);
            if (n == 0) r[n] = 1e6f;                                      // extreme, masked below
            const int c = pick(rng);
            float code = static_cast<float>(c % 5);
            if (c >= 95) code = NaN;
            else if (c >= 90) code = -1.0f;
            if (n == N - 1) code = 7.0f;                                  // singleton group
            sector[static_cast<size_t>(t) * N + n] = code;
            f.mask[static_cast<size_t>(t) * N + n] = (n == 0 || pick(rng) < 5) ? 0 : 1;
        }
        if (t % 50 == 7) std::fill(r.begin(), r.end(), NaN);              // all-NaN row
        f.x.push_back(r);
    }
    return f;
}

template <class M>
static Row run_row(M& m, unsigned di, Row r)
{
    SignalVector sv{r.data(), static_cast<unsigned>(r.size())};
    m.calculate(di, sv);
    return r;
}

// Masked finite values of a row (oracle side).
static std::vector<double> live(const Fixture& f, unsigned t, Row& r)
{
    std::vector<double> v;
    for (unsigned n = 0; n < f.N; ++n) {
        if (!f.mask[static_cast<size_t>(t) * f.N + n] || !std::isfinite(r[n])) r[n] = NaN;
        else v.push_back(r[n]);
    }
    return v;
}

static void mean_sd(const std::vector<double>& v, double& m, double& s)
{
    m = 0.0;
    for (double a : v) m += a;
    m /= v.size();
    double q = 0.0;
    for (double a : v) q += (a - m) * (a - m);
    s = v.size() > 1 ? std::sqrt(q / (v.size() - 1)) : 0.0;
}

// ---------------------------------------------------------------- winsorize
static Row oracle_winsorize(const Fixture& f, unsigned t, double k)
{
    Row r = f.x[t];
    auto v = live(f, t, r);
    if (v.size() < 2) return r;
    double m, s;
    mean_sd(v, m, s);
    for (auto& a : r)
        if (std::isfinite(a)) {
            if (a < m - k * s) a = static_cast<float>(m - k * s);
            else if (a > m + k * s) a = static_cast<float>(m + k * s);
        }
    return r;
}

TEST(MWinsorize, MatchesOracle)
{
    auto f = make_fixture(300, 40, 1);
    for (float k : {0.5f, 1.0f, 4.0f}) {
        MWinsorize m;
        m.numAssets = f.N; m.universe = &f.mask; m.std_mult = FloatParameter(k); m.init();
        for (unsigned t = 0; t < f.T; ++t)
            expect_rows_equal(run_row(m, t, f.x[t]), oracle_winsorize(f, t, k), "winsorize");
    }
}

TEST(MWinsorize, NoClippingIsBitIdenticalAndMaskedExtremeIgnored)
{
    DataPanel dp; dp.T = 1; dp.N = 5;
    std::vector<uint8_t> mask = {0, 1, 1, 1, 1};
    MWinsorize m; m.numAssets = 5; m.universe = &mask; m.init();
    Row in = {1e9f, 0.1f, 0.2f, 0.3f, 0.4f};
    Row out = run_row(m, 0, in);
    EXPECT_TRUE(std::isnan(out[0]));
    for (int i = 1; i < 5; ++i) EXPECT_EQ(std::memcmp(&out[i], &in[i], 4), 0);
    Row one = run_row(m, 0, {NaN, 5.0f, NaN, Inf, NaN});
    expect_rows_equal(one, {NaN, 5.0f, NaN, NaN, NaN}, "single finite passes");
}

// ---------------------------------------------------------------- normalize
static Row oracle_normalize(const Fixture& f, unsigned t, bool usestd, double lim)
{
    Row r = f.x[t];
    auto v = live(f, t, r);
    if (v.empty()) return r;
    double m, s;
    mean_sd(v, m, s);
    if (usestd && (v.size() < 2 || s < 1e-12)) return Row(f.N, NaN);
    for (auto& a : r)
        if (std::isfinite(a)) {
            double y = a - m;
            if (usestd) y /= s;
            if (lim > 0) y = std::max(-lim, std::min(lim, y));
            a = static_cast<float>(y);
        }
    return r;
}

TEST(MNormalize, MatchesOracle)
{
    auto f = make_fixture(300, 40, 2);
    for (int usestd : {0, 1})
        for (float lim : {0.0f, 0.7f}) {
            MNormalize m;
            m.numAssets = f.N; m.universe = &f.mask;
            m.usestd = IntParameter(usestd); m.limit = FloatParameter(lim); m.init();
            for (unsigned t = 0; t < f.T; ++t)
                expect_rows_equal(run_row(m, t, f.x[t]), oracle_normalize(f, t, usestd, lim),
                                  "normalize");
        }
}

TEST(MNormalize, DefaultIsDemeanAndConstantRowWithStdIsNaN)
{
    MNormalize m; m.numAssets = 4; m.init();
    Row out = run_row(m, 0, {1.0f, 2.0f, 3.0f, NaN});
    expect_rows_equal(out, {-1.0f, 0.0f, 1.0f, NaN}, "demean");
    MNormalize s; s.numAssets = 3; s.usestd = IntParameter(1); s.init();
    expect_rows_equal(run_row(s, 0, {2.0f, 2.0f, 2.0f}), {NaN, NaN, NaN}, "constant");
    expect_rows_equal(run_row(s, 0, {2.0f, NaN, NaN}), {NaN, NaN, NaN}, "count<2");
}

// ---------------------------------------------------------------- hump
TEST(MHump, MatchesOracleOverManyDays)
{
    auto f = make_fixture(400, 30, 3);
    for (float h : {0.001f, 0.01f, 0.2f}) {
        MHump m; m.numAssets = f.N; m.universe = &f.mask; m.hump = FloatParameter(h); m.init();
        Row prev(f.N, NaN);
        for (unsigned t = 0; t < f.T; ++t) {
            Row r = f.x[t];
            auto v = live(f, t, r);
            double sabs = 0.0;
            for (double a : v) sabs += std::fabs(a);
            const double lim = static_cast<double>(h) * sabs;
            for (unsigned n = 0; n < f.N; ++n) {
                if (std::isnan(r[n])) { prev[n] = NaN; continue; }
                if (!std::isnan(prev[n])) {
                    const double d = static_cast<double>(r[n]) - prev[n];
                    if (std::fabs(d) > lim) r[n] = static_cast<float>(prev[n] + (d > 0 ? lim : -lim));
                    else r[n] = prev[n];
                }
                prev[n] = r[n];
            }
            expect_rows_equal(run_row(m, t, f.x[t]), r, "hump");
        }
    }
}

TEST(MHump, HandSequenceBoundaryAndReset)
{
    // limit = 0.25 * (|a| + |b|); b is a fixed anchor of 2.0 to control the limit.
    MHump m; m.numAssets = 2; m.hump = FloatParameter(0.25f); m.init();
    expect_rows_equal(run_row(m, 0, {2.0f, 2.0f}), {2.0f, 2.0f}, "day0 passes");  // limit 1
    expect_rows_equal(run_row(m, 1, {2.5f, 2.0f}), {2.0f, 2.0f}, "small ignored"); // limit 1.125
    // |d| == limit exactly: a=6 -> limit 0.25*(6+2)=2, but d=4 > 2 -> move by 2
    expect_rows_equal(run_row(m, 2, {6.0f, 2.0f}), {4.0f, 2.0f}, "move by limit");
    // prev 4, x=6: limit 2, d=2 == limit -> prev
    expect_rows_equal(run_row(m, 3, {6.0f, 2.0f}), {4.0f, 2.0f}, "boundary keeps prev");
    expect_rows_equal(run_row(m, 4, {NaN, 2.0f}), {NaN, 2.0f}, "NaN");
    expect_rows_equal(run_row(m, 5, {-3.0f, 2.0f}), {-3.0f, 2.0f}, "restart");
}

// ---------------------------------------------------------------- groups
// Oracle groups: masked, finite value, finite code >= 0.
static std::map<float, std::vector<unsigned>> oracle_groups(const Fixture& f, unsigned t, Row& r)
{
    std::map<float, std::vector<unsigned>> g;
    const float* codes = f.dp.arrays.at("sector").data() + static_cast<size_t>(t) * f.N;
    for (unsigned n = 0; n < f.N; ++n) {
        if (f.mask[static_cast<size_t>(t) * f.N + n] && std::isfinite(r[n]) &&
            std::isfinite(codes[n]) && codes[n] >= 0) g[codes[n]].push_back(n);
        else r[n] = NaN;
    }
    return g;
}

template <class M>
static void init_group(M& m, Fixture& f)
{
    m.numAssets = f.N; m.panel = &f.dp; m.universe = &f.mask;
    m.group = m.column_name = "sector"; m.init();
}

TEST(MGroupRank, MatchesOracle)
{
    auto f = make_fixture(300, 50, 4);
    for (int pct : {0, 1}) {
        MGroupRank m; init_group(m, f); m.pct = IntParameter(pct);
        for (unsigned t = 0; t < f.T; ++t) {
            Row r = f.x[t];
            Row want(f.N, NaN);
            for (auto& [code, mem] : oracle_groups(f, t, r))
                for (unsigned i : mem) {
                    int less = 0, eq = 0;
                    for (unsigned j : mem) { less += r[j] < r[i]; eq += r[j] == r[i]; }
                    const double rank = less + 1 + (eq - 1) / 2.0;
                    want[i] = static_cast<float>(pct ? rank / mem.size() : rank);
                }
            expect_rows_equal(run_row(m, t, f.x[t]), want, "group_rank");
        }
    }
}

TEST(MGroupRank, OneGroupEqualsRankBitForBit)
{
    auto f = make_fixture(200, 60, 5);
    std::fill(f.dp.arrays["sector"].begin(), f.dp.arrays["sector"].end(), 3.0f);
    for (int pct : {0, 1}) {
        MGroupRank g; init_group(g, f); g.pct = IntParameter(pct);
        MRank r; r.numAssets = f.N; r.universe = &f.mask; r.pct = IntParameter(pct); r.init();
        for (unsigned t = 0; t < f.T; ++t)
            expect_rows_equal(run_row(g, t, f.x[t]), run_row(r, t, f.x[t]), "rank identity");
    }
}

TEST(MGroupMean, MatchesOracleAndOneGroupIsMean)
{
    auto f = make_fixture(300, 50, 6);
    MGroupMean m; init_group(m, f);
    for (unsigned t = 0; t < f.T; ++t) {
        Row r = f.x[t];
        Row want(f.N, NaN);
        for (auto& [code, mem] : oracle_groups(f, t, r)) {
            double s = 0; for (unsigned i : mem) s += r[i];
            for (unsigned i : mem) want[i] = static_cast<float>(s / mem.size());
        }
        expect_rows_equal(run_row(m, t, f.x[t]), want, "group_mean");
    }
    DataPanel dp; dp.T = 1; dp.N = 4; dp.arrays["sector"] = {0, 0, 0, 0};
    MGroupMean one; one.numAssets = 4; one.panel = &dp; one.group = one.column_name = "sector"; one.init();
    expect_rows_equal(run_row(one, 0, {1.0f, 2.0f, NaN, 6.0f}), {3.0f, 3.0f, NaN, 3.0f}, "mean");
}

// Oracle for every weight/mean combination; cap lags one bar.
static Row oracle_group_mean(const Fixture& f, unsigned t, bool cap, bool harm)
{
    Row r = f.x[t];
    Row want(f.N, NaN);
    const float* w = t > 0 ? f.dp.arrays.at("cap").data() + static_cast<size_t>(t - 1) * f.N : nullptr;
    for (auto& [code, mem0] : oracle_groups(f, t, r)) {
        std::vector<unsigned> mem;
        for (unsigned i : mem0) if (!harm || r[i] != 0.0f) mem.push_back(i);
        double num = 0, den = 0;
        for (unsigned i : mem) {
            double wi = 1;
            if (cap) { if (!w || !std::isfinite(w[i]) || w[i] <= 0) continue; wi = w[i]; }
            num += harm ? wi / r[i] : wi * r[i];
            den += wi;
        }
        double m = NaN;
        if (den > 0) m = harm ? (num != 0 ? den / num : NaN) : num / den;
        for (unsigned i : mem) want[i] = std::isfinite(m) ? static_cast<float>(m) : NaN;
    }
    return want;
}

static void add_cap(Fixture& f, unsigned seed)
{
    std::mt19937 rng(seed);
    std::uniform_real_distribution<float> u(0.5f, 100.0f);
    auto& cap = f.dp.arrays["cap"];
    cap.resize(static_cast<size_t>(f.T) * f.N);
    for (auto& c : cap) {
        const unsigned p = rng() % 20;
        c = p == 0 ? NaN : p == 1 ? 0.0f : p == 2 ? -5.0f : u(rng);
    }
}

TEST(MGroupMean, WeightAndMeanVariantsMatchOracle)
{
    auto f = make_fixture(300, 50, 12);
    add_cap(f, 13);
    for (int cap : {0, 1})
        for (int harm : {0, 1}) {
            MGroupMean m; init_group(m, f);
            m.cap_weight = IntParameter(cap); m.harmonic = IntParameter(harm);
            for (unsigned t = 0; t < f.T; ++t)
                expect_rows_equal(run_row(m, t, f.x[t]), oracle_group_mean(f, t, cap, harm),
                                  "group_mean cap=" + std::to_string(cap) + " harm=" + std::to_string(harm));
        }
}

TEST(MGroupMean, CapUsesPreviousBarOnly)
{
    auto f = make_fixture(50, 30, 14);
    add_cap(f, 15);
    MGroupMean a; init_group(a, f); a.cap_weight = IntParameter(1);
    std::vector<Row> base;
    for (unsigned t = 0; t < f.T; ++t) base.push_back(run_row(a, t, f.x[t]));
    EXPECT_TRUE(std::all_of(base[0].begin(), base[0].end(), [](float v) { return std::isnan(v); }));
    auto& cap = f.dp.arrays["cap"];
    for (unsigned n = 0; n < f.N; ++n) cap[static_cast<size_t>(20) * f.N + n] *= 7.0f + n;
    MGroupMean b; init_group(b, f); b.cap_weight = IntParameter(1);
    for (unsigned t = 0; t <= 20; ++t)
        expect_rows_equal(run_row(b, t, f.x[t]), base[t], "cap at di must not matter");
}

TEST(MGroupMean, EqualCapsMatchEqualWeight)
{
    auto f = make_fixture(200, 40, 16);
    f.dp.arrays["cap"].assign(static_cast<size_t>(f.T) * f.N, 3.7f);
    for (int harm : {0, 1}) {
        MGroupMean e; init_group(e, f); e.harmonic = IntParameter(harm);
        MGroupMean c; init_group(c, f); c.harmonic = IntParameter(harm); c.cap_weight = IntParameter(1);
        for (unsigned t = 1; t < f.T; ++t) {
            Row x = run_row(e, t, f.x[t]), y = run_row(c, t, f.x[t]);
            for (unsigned n = 0; n < f.N; ++n) {
                if (std::isnan(x[n])) { EXPECT_TRUE(std::isnan(y[n])); continue; }
                // double reassociation of the scaled sums: relative 1e-6 (float output ulp)
                EXPECT_NEAR(y[n], x[n], 1e-6f * std::max(1.0f, std::fabs(x[n])));
            }
        }
    }
}

TEST(MGroupMean, HarmonicEqualsInverseSandwichAndEdgeCases)
{
    auto f = make_fixture(200, 40, 17);
    add_cap(f, 18);
    for (int cap : {0, 1}) {
        MGroupMean h; init_group(h, f); h.harmonic = IntParameter(1); h.cap_weight = IntParameter(cap);
        MGroupMean a; init_group(a, f); a.cap_weight = IntParameter(cap);
        for (unsigned t = 0; t < f.T; ++t) {
            Row inv = f.x[t];
            for (auto& v : inv) v = (std::isfinite(v) && v != 0.0f) ? 1.0f / v : NaN;
            Row s = run_row(a, t, inv);
            for (auto& v : s) v = (std::isfinite(v) && v != 0.0f) ? 1.0f / v : NaN;
            Row got = run_row(h, t, f.x[t]);
            for (unsigned n = 0; n < f.N; ++n) {
                if (std::isnan(s[n]) || std::isnan(got[n])) {
                    EXPECT_EQ(std::isnan(s[n]), std::isnan(got[n])) << t << " " << n; continue;
                }
                // float rounding of 1/x in the sandwich, amplified by cancellation
                // between positive and negative reciprocals
                EXPECT_NEAR(got[n], s[n], 1e-4f * std::fabs(s[n])) << t << " " << n;
            }
        }
    }
    DataPanel dp; dp.T = 1; dp.N = 6; dp.arrays["sector"] = {0, 0, 1, 1, 2, 2};
    MGroupMean m; m.numAssets = 6; m.panel = &dp; m.group = m.column_name = "sector";
    m.harmonic = IntParameter(1); m.init();
    expect_rows_equal(run_row(m, 0, {0.0f, 2.0f, -0.0f, 0.0f, 2.0f, -2.0f}),
                      {NaN, 2.0f, NaN, NaN, NaN, NaN}, "zero member, all-zero group, reciprocal sum 0");
}

TEST(MGroupZscore, MatchesOracle)
{
    auto f = make_fixture(300, 50, 7);
    MGroupZscore m; init_group(m, f);
    for (unsigned t = 0; t < f.T; ++t) {
        Row r = f.x[t];
        Row want(f.N, NaN);
        for (auto& [code, mem] : oracle_groups(f, t, r)) {
            std::vector<double> v; for (unsigned i : mem) v.push_back(r[i]);
            double mu, s; mean_sd(v, mu, s);
            if (mem.size() < 2 || s < 1e-12) continue;
            for (unsigned i : mem)
                want[i] = static_cast<float>((r[i] - mu) / (s + static_cast<double>(1e-12f)));
        }
        expect_rows_equal(run_row(m, t, f.x[t]), want, "group_zscore");
    }
}

TEST(GroupOps, SingletonNegativeNaNCodesAndMaskedExtreme)
{
    DataPanel dp; dp.T = 1; dp.N = 7;
    dp.arrays["sector"] = {1, 1, 1, 2, -1, NaN, 1};
    std::vector<uint8_t> mask = {1, 1, 1, 1, 1, 1, 0};
    Row in = {1.0f, 2.0f, 2.0f, 5.0f, 9.0f, 9.0f, 1e9f};
    MGroupMean gm; gm.numAssets = 7; gm.panel = &dp; gm.universe = &mask;
    gm.group = gm.column_name = "sector"; gm.init();
    expect_rows_equal(run_row(gm, 0, in),
                      {5.0f / 3, 5.0f / 3, 5.0f / 3, 5.0f, NaN, NaN, NaN}, "mean");
    MGroupZscore gz; gz.numAssets = 7; gz.panel = &dp; gz.universe = &mask;
    gz.group = gz.column_name = "sector"; gz.init();
    Row z = run_row(gz, 0, in);
    EXPECT_TRUE(std::isnan(z[3]));  // singleton group
    EXPECT_TRUE(std::isnan(z[4]) && std::isnan(z[5]) && std::isnan(z[6]));
    MGroupRank gr; gr.numAssets = 7; gr.panel = &dp; gr.universe = &mask;
    gr.group = gr.column_name = "sector"; gr.init();
    expect_rows_equal(run_row(gr, 0, in), {1.0f / 3, 2.5f / 3, 2.5f / 3, 1.0f, NaN, NaN, NaN}, "ties");
    expect_rows_equal(run_row(gr, 0, Row(7, NaN)), Row(7, NaN), "all NaN");
}

// ---------------------------------------------------------------- rank_sharp
TEST(MRankSharp, MatchesOracle)
{
    auto f = make_fixture(300, 40, 8);
    for (float e : {0.5f, 1.0f, 2.0f, 3.0f}) {
        MRankSharp m; m.numAssets = f.N; m.universe = &f.mask; m.exp = FloatParameter(e); m.init();
        for (unsigned t = 0; t < f.T; ++t) {
            Row r = f.x[t];
            auto v = live(f, t, r);
            Row want(f.N, NaN);
            for (unsigned i = 0; i < f.N; ++i) {
                if (std::isnan(r[i])) continue;
                int less = 0, eq = 0;
                for (double a : v) { less += a < r[i]; eq += a == r[i]; }
                const float pr = static_cast<float>((less + 1 + (eq - 1) / 2.0) / v.size());
                const double c = static_cast<double>(pr) - 0.5;
                const double p = std::pow(std::fabs(c), static_cast<double>(e));
                want[i] = static_cast<float>(c > 0 ? p : c < 0 ? -p : 0.0);
            }
            expect_rows_equal(run_row(m, t, f.x[t]), want, "rank_sharp");
        }
    }
}

TEST(MRankSharp, ExpOneIsRankMinusHalfAndCenterIsZero)
{
    auto f = make_fixture(200, 40, 9);
    MRankSharp s; s.numAssets = f.N; s.universe = &f.mask; s.exp = FloatParameter(1.0f); s.init();
    MRank r; r.numAssets = f.N; r.universe = &f.mask; r.init();
    for (unsigned t = 0; t < f.T; ++t) {
        Row want = run_row(r, t, f.x[t]);
        for (auto& a : want) if (!std::isnan(a)) a = a - 0.5f;
        expect_rows_equal(run_row(s, t, f.x[t]), want, "exp=1");
    }
    // pct ranks .25 .5 .75 1 -> r = -.25 0 .25 .5; research def sign(r)|r|^3
    MRankSharp c; c.numAssets = 4; c.exp = FloatParameter(3.0f); c.init();
    expect_rows_equal(run_row(c, 0, {1.0f, 2.0f, 3.0f, 4.0f}),
                      {-0.015625f, 0.0f, 0.015625f, 0.125f}, "exp=3");
}

// ---------------------------------------------------------------- truncate
// Oracle level for one set of magnitudes: try k = 0, 1, ... largest members clipped at c (sorted
// descending); c = max * (sum of the rest) / (1 - max * k) must lie between the k-th and (k+1)-th
// largest. post=0: c = max * sum. Infeasible (max * n < 1): every member equal (c = smallest).
static double oracle_level(std::vector<double> a, double mx, bool post)
{
    double sum = 0.0;
    for (double v : a) sum += v;
    if (!post) return mx * sum;
    std::sort(a.begin(), a.end(), std::greater<double>());
    const size_t n = a.size();
    if (mx * n < 1.0) return a[n - 1];
    double rest = sum;
    for (size_t k = 0; k < n; ++k) {
        // k members clipped; candidate c from the remaining n - k
        if (k == 0) {
            if (a[0] <= mx * sum) return a[0];
        } else {
            rest -= a[k - 1];
            const double c = mx * rest / (1.0 - mx * k);
            if (c >= a[k] && c <= a[k - 1]) return c;
        }
    }
    ADD_FAILURE() << "oracle found no level";
    return NaN;
}

static Row oracle_truncate(const Fixture& f, unsigned t, double mx, int side, bool post)
{
    Row r = f.x[t];
    (void)live(f, t, r);
    for (int leg : side ? std::vector<int>{1, -1} : std::vector<int>{0}) {
        std::vector<double> a;
        for (float v : r)
            if (std::isfinite(v) && v != 0.0f && (leg == 0 || (leg > 0) == (v > 0.0f))) a.push_back(std::fabs(v));
        if (a.empty()) continue;
        const double c = oracle_level(a, mx, post);
        for (auto& v : r)
            if (std::isfinite(v) && v != 0.0f && (leg == 0 || (leg > 0) == (v > 0.0f)) && std::fabs(v) > c)
                v = static_cast<float>(v > 0 ? c : -c);
    }
    return r;
}

// Clipped values come from c computed in a different summation order: allow 2 float ulps.
static void expect_rows_near(const Row& got, const Row& want, const std::string& what)
{
    ASSERT_EQ(got.size(), want.size());
    for (size_t i = 0; i < got.size(); ++i) {
        if (std::isnan(want[i])) { EXPECT_TRUE(std::isnan(got[i])) << what << " col " << i; continue; }
        EXPECT_NEAR(got[i], want[i], 2.5e-7 * std::fabs(want[i])) << what << " col " << i;
    }
}

static MTruncate make_truncate(unsigned N, const std::vector<uint8_t>* mask, float mx, int side, int post)
{
    MTruncate m; m.numAssets = N; m.universe = mask;
    m.maxw = FloatParameter(mx); m.side = IntParameter(side); m.post = IntParameter(post); m.init();
    return m;
}

TEST(MTruncate, MatchesOracle)
{
    auto f = make_fixture(300, 40, 23);
    for (float mx : {0.02f, 0.05f, 0.1f, 0.3f, 1.0f})
        for (int side : {0, 1})
            for (int post : {0, 1}) {
                auto m = make_truncate(f.N, &f.mask, mx, side, post);
                for (unsigned t = 0; t < f.T; ++t)
                    expect_rows_near(run_row(m, t, f.x[t]), oracle_truncate(f, t, mx, side, post),
                                     "max=" + std::to_string(mx) + " side=" + std::to_string(side) +
                                     " post=" + std::to_string(post) + " t=" + std::to_string(t));
            }
}

// Final weight after the simulator's renormalization s / sum|s| never exceeds max (side=0) or max of
// its own leg (side=1), on heavy-tailed rows.
TEST(MTruncate, FinalWeightRespectsCap)
{
    std::mt19937 rng(5);
    std::cauchy_distribution<float> cd(0.0f, 1.0f);
    const unsigned N = 300;
    for (int iter = 0; iter < 200; ++iter) {
        Row x(N);
        for (auto& v : x) v = cd(rng);
        x[iter % N] = 1e30f;
        for (float mx : {0.004f, 0.01f, 0.05f}) {
            for (int side : {0, 1}) {
                auto m = make_truncate(N, nullptr, mx, side, 1);
                Row y = run_row(m, 0, x);
                for (int leg : side ? std::vector<int>{1, -1} : std::vector<int>{0}) {
                    double sum = 0.0, top = 0.0, low = INFINITY;
                    int n = 0;
                    for (float v : y)
                        if (v != 0.0f && (leg == 0 || (leg > 0) == (v > 0.0f))) {
                            sum += std::fabs(v); ++n;
                            top = std::max(top, (double)std::fabs(v)); low = std::min(low, (double)std::fabs(v));
                        }
                    if (n == 0) continue;
                    if (mx * n >= 1.0) EXPECT_LE(top / sum, mx + 1e-7) << "iter " << iter << " side " << side;
                    else EXPECT_EQ(top, low) << "infeasible set is equalized";
                }
            }
        }
    }
}

TEST(MTruncate, HandCasesInfeasibleZerosAndMask)
{
    // side=0, max=0.25 on |x| = {1,1,1,1,10}: c with c/(4+c) = .25 -> c = 4/3.
    auto m = make_truncate(5, nullptr, 0.25f, 0, 1);
    Row y = run_row(m, 0, {1.0f, -1.0f, 1.0f, -1.0f, 10.0f});
    EXPECT_EQ(y[0], 1.0f); EXPECT_EQ(y[1], -1.0f);
    EXPECT_FLOAT_EQ(y[4], 4.0f / 3.0f);
    // Infeasible (max * n_nonzero = 0.5 < 1): every nonzero member gets the smallest magnitude, sign kept; 0 stays 0.
    auto inf = make_truncate(4, nullptr, 0.25f, 0, 1);
    expect_rows_equal(run_row(inf, 0, {3.0f, 0.0f, -0.5f, NaN}), {0.5f, 0.0f, -0.5f, NaN}, "infeasible");
    // All-zero and all-NaN rows pass unchanged; inf is NaN.
    expect_rows_equal(run_row(m, 0, {0.0f, 0.0f, 0.0f, 0.0f, 0.0f}), {0.0f, 0.0f, 0.0f, 0.0f, 0.0f}, "zeros");
    expect_rows_equal(run_row(m, 0, {NaN, Inf, -Inf, NaN, NaN}), {NaN, NaN, NaN, NaN, NaN}, "nonfinite");
    // post=0: c = max * sum|x| = 0.25 * 14 = 3.5.
    auto p0 = make_truncate(5, nullptr, 0.25f, 0, 0);
    expect_rows_equal(run_row(p0, 0, {1.0f, -1.0f, 1.0f, -1.0f, 10.0f}), {1.0f, -1.0f, 1.0f, -1.0f, 3.5f}, "post0");
    // side=1: legs capped separately. Longs {1,1,1,1,6} max .25 -> c = 4/3; shorts {-1,-2} feasible? .25*2 < 1 -> both -1.
    auto s1 = make_truncate(7, nullptr, 0.25f, 1, 1);
    Row z = run_row(s1, 0, {1.0f, 1.0f, 1.0f, 1.0f, 6.0f, -1.0f, -2.0f});
    EXPECT_FLOAT_EQ(z[4], 4.0f / 3.0f); EXPECT_EQ(z[5], -1.0f); EXPECT_EQ(z[6], -1.0f);
    // A masked extreme is ignored: without the mask 100 would force clipping of the rest.
    std::vector<uint8_t> mask = {0, 1, 1, 1, 1};
    auto mm = make_truncate(5, &mask, 0.25f, 0, 1);
    expect_rows_equal(run_row(mm, 0, {100.0f, 1.0f, 1.0f, 1.0f, 1.0f}), {NaN, 1.0f, 1.0f, 1.0f, 1.0f}, "mask");
}

// Desk cs_truncate (cppFuncs.cpp calc_trunc_level / cs_truncate_1d_*) equivalence:
//   cs_truncate(x, t)                                == <truncate max="t" side="1" post="0"/>
//   cs_truncate(x, t, post_trunc=True, absolute=True) == <truncate max="t"/>
// including the infeasible case max * n_nonzero < 1: both clip at the smallest nonzero |x| (the desk
// loop breaks with level 0 and returns that element), so the nonzero members become equal.
// Desk side=1/post=0 transcribed (zeros go to the short leg's sum as 0, so no effect).
TEST(MTruncate, DeskEquivalence)
{
    auto desk_level = [](std::vector<double> vec, double th) {
        std::sort(vec.begin(), vec.end());
        double level = 0.0, val = NAN, cumsum = 0.0;
        if (vec.empty()) return 0.0;
        for (size_t i = 0; i < vec.size(); ++i) {
            val = vec[i];
            const double temp = static_cast<double>(vec.size() - i);
            const double denom = cumsum + temp * val;
            if (denom > 0. && val / denom > th) { level = th * cumsum / (1.0 - th * temp); break; }
            cumsum += val;
        }
        if ((level <= 0.0 || !std::isfinite(level)) && std::isfinite(val)) return val;
        return level;
    };
    std::mt19937 rng(9);
    std::cauchy_distribution<double> cd(0.0, 1.0);
    const unsigned N = 200;
    for (int iter = 0; iter < 100; ++iter) {
        Row x(N);
        for (auto& v : x) v = static_cast<float>(cd(rng));
        const double th = 0.02;
        // side=1, post=0
        double ps = 0.0, ns = 0.0;
        for (float v : x) (v > 0 ? ps : ns) += std::fabs(v);
        Row want1(N);
        for (unsigned i = 0; i < N; ++i)
            want1[i] = static_cast<float>(x[i] >= 0 ? std::min(ps * th, (double)x[i]) : -std::min(ns * th, -(double)x[i]));
        auto m1 = make_truncate(N, nullptr, static_cast<float>(th), 1, 0);
        expect_rows_near(run_row(m1, 0, x), want1, "desk side1 post0");
        // abs=True, post=True
        std::vector<double> a;
        for (float v : x) a.push_back(std::fabs(v));
        const double lvl = desk_level(a, th);
        Row want0(N);
        for (unsigned i = 0; i < N; ++i)
            want0[i] = static_cast<float>(x[i] >= 0 ? std::min(lvl, (double)x[i]) : -std::min(lvl, -(double)x[i]));
        auto m0 = make_truncate(N, nullptr, static_cast<float>(th), 0, 1);
        expect_rows_near(run_row(m0, 0, x), want0, "desk abs post");
    }
    // Infeasible rows (max * n_nonzero < 1), bit-exact against the desk.
    const std::vector<std::pair<Row, double>> infeasible = {
        {{3.0f, 0.0f, -0.5f}, 0.25}, {{1.0f, -2.0f, 7.0f}, 0.25}, {{-4.0f, 0.25f, 0.0f, 9.0f, -1.5f}, 0.1}};
    for (const auto& [x, th] : infeasible) {
        std::vector<double> a;
        for (float v : x) a.push_back(std::fabs(v));
        const double lvl = desk_level(a, th);
        Row want(x.size());
        for (size_t i = 0; i < x.size(); ++i)
            want[i] = static_cast<float>(x[i] >= 0 ? std::min(lvl, (double)x[i]) : -std::min(lvl, -(double)x[i]));
        auto m = make_truncate(static_cast<unsigned>(x.size()), nullptr, static_cast<float>(th), 0, 1);
        expect_rows_equal(run_row(m, 0, x), want, "desk infeasible");
    }
    auto ex = make_truncate(3, nullptr, 0.25f, 0, 1);
    expect_rows_equal(run_row(ex, 0, {3.0f, 0.0f, -0.5f}), {0.5f, 0.0f, -0.5f}, "desk infeasible example");
}

// ---------------------------------------------------------------- XML compile
static DataPanel make_panel(unsigned T = 30, unsigned N = 3)
{
    DataPanel dp; dp.T = T; dp.N = N;
    dp.timestamps.resize(T);
    for (unsigned t = 0; t < T; ++t) dp.timestamps[t] = static_cast<int64_t>(t) * 86400000LL;
    dp.symbols.resize(N);
    for (unsigned n = 0; n < N; ++n) dp.symbols[n] = "SYM" + std::to_string(n);
    for (const char* col : {"open", "high", "low", "close", "volume", "sector", "cap"}) {
        auto& arr = dp.arrays[col];
        arr.assign(static_cast<size_t>(T) * N, 1.0f);
    }
    return dp;
}

template <class M>
static M* find_module(CompiledAlpha& a)
{
    for (auto& m : a.modules) if (auto* p = dynamic_cast<M*>(m.get())) return p;
    return nullptr;
}

static void expect_error(const std::string& xml, const DataPanel& dp, const std::string& token)
{
    try {
        (void)compile_xml(xml, &dp);
        ADD_FAILURE() << "expected CompileError for " << xml;
    } catch (const CompileError& e) {
        EXPECT_NE(std::string(e.what()).find(token), std::string::npos) << e.what();
    }
}

TEST(CsCompile, DefaultsAndExplicitAttributes)
{
    auto dp = make_panel();
    auto c = [&](const std::string& body) { return compile_xml("<data expr=\"close\"/>" + body, &dp); };
    { auto a = c("<winsorize/>"); EXPECT_EQ(find_module<MWinsorize>(a[0])->std_mult, 4.0f); }
    { auto a = c("<winsorize std=\"2.5\"/>"); EXPECT_EQ(find_module<MWinsorize>(a[0])->std_mult, 2.5f); }
    { auto a = c("<normalize/>"); auto* m = find_module<MNormalize>(a[0]);
      EXPECT_EQ(int(m->usestd), 0); EXPECT_EQ(m->limit, 0.0f); }
    { auto a = c("<normalize usestd=\"true\" limit=\"3\"/>"); auto* m = find_module<MNormalize>(a[0]);
      EXPECT_EQ(int(m->usestd), 1); EXPECT_EQ(m->limit, 3.0f); }
    { auto a = c("<hump/>"); EXPECT_EQ(find_module<MHump>(a[0])->hump, 0.01f); }
    { auto a = c("<hump hump=\"0.1\"/>"); EXPECT_EQ(find_module<MHump>(a[0])->hump, 0.1f); }
    { auto a = c("<group_rank group=\"sector\"/>"); auto* m = find_module<MGroupRank>(a[0]);
      ASSERT_NE(m, nullptr); EXPECT_EQ(int(m->pct), 1); EXPECT_EQ(m->column_name, "sector"); }
    { auto a = c("<group_rank group=\" sector \" pct=\"0\"/>");
      EXPECT_EQ(int(find_module<MGroupRank>(a[0])->pct), 0); }
    { auto a = c("<group_mean group=\"sector\"/>"); ASSERT_NE(find_module<MGroupMean>(a[0]), nullptr); }
    { auto a = c("<group_mean group=\"sector\" weight=\"cap\" mean=\"harmonic\"/>");
      auto* m = find_module<MGroupMean>(a[0]);
      EXPECT_EQ(int(m->cap_weight), 1); EXPECT_EQ(int(m->harmonic), 1); }
    { auto a = c("<group_mean group=\"sector\" weight=\"equal\" mean=\"arithmetic\"/>");
      auto* m = find_module<MGroupMean>(a[0]);
      EXPECT_EQ(int(m->cap_weight), 0); EXPECT_EQ(int(m->harmonic), 0); }
    { auto a = c("<group_zscore group=\"sector\"/>"); ASSERT_NE(find_module<MGroupZscore>(a[0]), nullptr); }
    { auto a = c("<rank_sharp/>"); EXPECT_EQ(find_module<MRankSharp>(a[0])->exp, 2.0f); }
    { auto a = c("<rank_sharp exp=\"3\"/>"); EXPECT_EQ(find_module<MRankSharp>(a[0])->exp, 3.0f); }
    { auto a = c("<truncate max=\"0.05\"/>"); auto* m = find_module<MTruncate>(a[0]);
      ASSERT_NE(m, nullptr); EXPECT_EQ(m->maxw, 0.05f); EXPECT_EQ(int(m->side), 0); EXPECT_EQ(int(m->post), 1); }
    { auto a = c("<truncate max=\"1\" side=\"1\" post=\"0\"/>"); auto* m = find_module<MTruncate>(a[0]);
      EXPECT_EQ(m->maxw, 1.0f); EXPECT_EQ(int(m->side), 1); EXPECT_EQ(int(m->post), 0); }
}

TEST(CsCompile, Errors)
{
    auto dp = make_panel();
    const std::string d = "<data expr=\"close\"/>";
    expect_error(d + "<winsorize days=\"3\"/>", dp, "days");
    expect_error(d + "<winsorize std=\"0\"/>", dp, "<winsorize> attribute 'std'");
    expect_error(d + "<winsorize std=\"x\"/>", dp, "std");
    expect_error(d + "<normalize limit=\"-1\"/>", dp, "<normalize> attribute 'limit'");
    expect_error(d + "<normalize usestd=\"2\"/>", dp, "usestd");
    expect_error(d + "<normalize foo=\"1\"/>", dp, "foo");
    expect_error(d + "<hump hump=\"0\"/>", dp, "<hump> attribute 'hump'");
    expect_error(d + "<hump days=\"1\"/>", dp, "days");
    expect_error(d + "<rank_sharp exp=\"0\"/>", dp, "<rank_sharp> attribute 'exp'");
    expect_error(d + "<rank_sharp exp=\"-1\"/>", dp, "<rank_sharp> attribute 'exp'");
    expect_error(d + "<rank_sharp pct=\"1\"/>", dp, "pct");
    expect_error(d + "<truncate/>", dp, "<truncate> requires attribute 'max'");
    expect_error(d + "<truncate max=\"0\"/>", dp, "<truncate> attribute 'max'");
    expect_error(d + "<truncate max=\"1.5\"/>", dp, "<truncate> attribute 'max'");
    expect_error(d + "<truncate max=\"x\"/>", dp, "max");
    expect_error(d + "<truncate max=\"0.1\" side=\"2\"/>", dp, "<truncate> attribute 'side'");
    expect_error(d + "<truncate max=\"0.1\" post=\"2\"/>", dp, "post");
    expect_error(d + "<truncate max=\"0.1\" foo=\"1\"/>", dp, "foo");
    for (const std::string tag : {"group_rank", "group_mean", "group_zscore"}) {
        expect_error(d + "<" + tag + "/>", dp, "<" + tag + "> requires attribute 'group' (sector|industry)");
        expect_error(d + "<" + tag + " group=\"country\"/>", dp,
                     "Unsupported " + tag + " group 'country' (supported: sector, industry)");
        expect_error(d + "<" + tag + " group=\"industry\"/>", dp,
                     tag + " group 'industry' needs data field 'industry'");
        expect_error(d + "<" + tag + " group=\"sector\" foo=\"1\"/>", dp, "foo");
        try {
            (void)ModuleRegistry::default_registry().create(tag, {{"group", "sector"}}, nullptr);
            ADD_FAILURE();
        }
        catch (const CompileError& e) {
            EXPECT_NE(std::string(e.what()).find("DataPanel is required for <" + tag + ">"),
                      std::string::npos) << e.what();
        }
    }
    expect_error(d + "<group_mean group=\"sector\" pct=\"1\"/>", dp, "pct");
    expect_error(d + "<group_mean group=\"sector\" weight=\"vol\"/>", dp,
                 "Unsupported group_mean weight 'vol' (supported: equal, cap)");
    expect_error(d + "<group_mean group=\"sector\" mean=\"geometric\"/>", dp,
                 "Unsupported group_mean mean 'geometric' (supported: arithmetic, harmonic)");
    auto nocap = dp;
    nocap.arrays.erase("cap");
    expect_error(d + "<group_mean group=\"sector\" weight=\"cap\"/>", nocap,
                 "group_mean weight 'cap' needs data field 'cap'");
}

TEST(CsCompile, PipeExpansion)
{
    auto dp = make_panel();
    dp.arrays["industry"] = dp.arrays["sector"];
    const std::string d = "<data expr=\"close\"/>";
    for (const std::string body : {"<winsorize std=\"2|3\"/>", "<normalize limit=\"1|2\"/>",
                                   "<hump hump=\"0.01|0.1\"/>", "<group_rank group=\"sector|industry\"/>",
                                   "<group_mean group=\"sector|industry\"/>",
                                   "<group_mean group=\"sector\" weight=\"equal|cap\"/>",
                                   "<group_mean group=\"sector\" mean=\"arithmetic|harmonic\"/>",
                                   "<group_zscore group=\"sector|industry\"/>",
                                   "<rank_sharp exp=\"2|3\"/>", "<truncate max=\"0.05|0.1\"/>",
                                   "<truncate max=\"0.05\" side=\"0|1\"/>"})
        EXPECT_EQ(compile_xml(d + body, &dp).size(), 2u) << body;
    auto a = compile_xml(d + "<rank_sharp exp=\"2|3\"/>", &dp);
    EXPECT_EQ(find_module<MRankSharp>(a[0])->exp, 2.0f);
    EXPECT_EQ(find_module<MRankSharp>(a[1])->exp, 3.0f);
}

TEST(CsCompile, ExplicitDefaultGroupMeanIsBitIdentical)
{
    auto f = make_fixture(200, 40, 19);
    MGroupMean a; init_group(a, f);
    auto dp = make_panel();
    auto alphas = compile_xml("<data expr=\"close\"/><group_mean group=\"sector\" weight=\"equal\" mean=\"arithmetic\"/>", &dp);
    MGroupMean* b = find_module<MGroupMean>(alphas[0]);
    b->panel = &f.dp; b->universe = &f.mask; b->numAssets = f.N; b->init();
    for (unsigned t = 0; t < f.T; ++t)
        expect_rows_equal(run_row(*b, t, f.x[t]), run_row(a, t, f.x[t]), "explicit defaults");
}

TEST(CsCompile, UniverseInjectionReachesEveryModule)
{
    auto dp = make_panel();
    std::vector<uint8_t> uni(static_cast<size_t>(dp.T) * dp.N, 1);
    auto a = compile_xml("<data expr=\"close\"/><winsorize/><normalize/><hump/>"
                         "<group_rank group=\"sector\"/><group_mean group=\"sector\"/>"
                         "<group_zscore group=\"sector\"/><rank_sharp/><truncate max=\"0.1\"/>", &dp, &uni);
    ASSERT_EQ(a.size(), 1u);
    EXPECT_EQ(find_module<MWinsorize>(a[0])->universe, &uni);
    EXPECT_EQ(find_module<MNormalize>(a[0])->universe, &uni);
    EXPECT_EQ(find_module<MHump>(a[0])->universe, &uni);
    EXPECT_EQ(find_module<MGroupRank>(a[0])->universe, &uni);
    EXPECT_EQ(find_module<MGroupMean>(a[0])->universe, &uni);
    EXPECT_EQ(find_module<MGroupZscore>(a[0])->universe, &uni);
    EXPECT_EQ(find_module<MRankSharp>(a[0])->universe, &uni);
    EXPECT_EQ(find_module<MTruncate>(a[0])->universe, &uni);
}

// ---------------------------------------------------------------- benchmark
// Printout only; enable with LONGSHORT_CS_BENCH=1 (Release build).
TEST(CsBench, Print)
{
    if (!std::getenv("LONGSHORT_CS_BENCH")) GTEST_SKIP() << "set LONGSHORT_CS_BENCH=1";
    const unsigned T = 3000, N = 6000;
    std::mt19937 rng(11);
    std::normal_distribution<float> nd;
    DataPanel dp; dp.T = T; dp.N = N;
    auto& sec = dp.arrays["sector"]; sec.resize(static_cast<size_t>(T) * N);
    for (auto& c : sec) c = static_cast<float>(rng() % 11);
    std::vector<float> data(static_cast<size_t>(T) * N);
    for (auto& v : data) v = nd(rng);
    auto bench = [&](const char* name, Module& m) {
        m.numAssets = N; m.init();
        Row buf(N);
        auto t0 = std::chrono::steady_clock::now();
        for (unsigned t = 0; t < T; ++t) {
            std::copy(data.begin() + static_cast<size_t>(t) * N,
                      data.begin() + static_cast<size_t>(t + 1) * N, buf.begin());
            SignalVector sv{buf.data(), N};
            m.calculate(t, sv);
        }
        const double s = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
        std::printf("BENCH %-12s T=%u N=%u %.3f s\n", name, T, N, s);
    };
    MRank rk; bench("rank", rk);
    MWinsorize w; bench("winsorize", w);
    MNormalize no; no.usestd = IntParameter(1); no.limit = FloatParameter(3.0f); bench("normalize", no);
    MHump h; bench("hump", h);
    MGroupRank gr; gr.panel = &dp; gr.column_name = "sector"; bench("group_rank", gr);
    MGroupMean gm; gm.panel = &dp; gm.column_name = "sector"; bench("group_mean", gm);
    MGroupZscore gz; gz.panel = &dp; gz.column_name = "sector"; bench("group_zscore", gz);
    MNeutralize ne; ne.panel = &dp; ne.column_name = "sector"; bench("neutralize", ne);
    MRankSharp rs; bench("rank_sharp", rs);
    for (int side : {0, 1})
        for (int post : {0, 1}) {
            MTruncate tr; tr.maxw = FloatParameter(0.01f); tr.side = IntParameter(side); tr.post = IntParameter(post);
            const std::string name = "truncate s" + std::to_string(side) + "p" + std::to_string(post);
            bench(name.c_str(), tr);
        }
}
