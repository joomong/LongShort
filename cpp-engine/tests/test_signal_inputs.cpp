// Saved-signal inputs (signal_1 .. signal_K): <add|sub|mul|div|min|max|mean|nanmean>, <gate>, <csresid>,
// <bucketdemean>, and the single-input <csdemedian>/<cswinsorq>. Oracles are plain per-row loops in double.
#include <gtest/gtest.h>
#include <algorithm>
#include <cmath>
#include <limits>
#include <map>
#include <random>
#include <string>
#include <vector>
#include <Eigen/Dense>
#include "module.h"
#include "xml_compiler.h"
#include "simulation_data.h"
#include "signal_vector.h"

static const float NaN = std::numeric_limits<float>::quiet_NaN();
using Row = std::vector<float>;
using Panel = std::vector<Row>;

static DataPanel make_panel(unsigned T, unsigned N, unsigned seed)
{
    DataPanel dp;
    dp.T = T;
    dp.N = N;
    dp.timestamps.resize(T);
    for (unsigned t = 0; t < T; ++t) dp.timestamps[t] = static_cast<int64_t>(t) * 86400000LL;
    dp.symbols.resize(N);
    for (unsigned n = 0; n < N; ++n) dp.symbols[n] = "S" + std::to_string(n);
    std::mt19937 rng(seed);
    std::uniform_real_distribution<float> u(-3.0f, 3.0f);
    std::uniform_int_distribution<int> pick(0, 99);
    for (const char* col : {"a", "b", "c", "close", "open", "high", "low", "volume"}) {
        auto& arr = dp.arrays[col];
        arr.resize(static_cast<size_t>(T) * N);
        for (auto& v : arr) {
            const int p = pick(rng);
            v = p < 8 ? NaN : p < 20 ? static_cast<float>(pick(rng) % 3) : p < 22 ? 0.0f : u(rng);
        }
    }
    auto& sector = dp.arrays["sector"];
    sector.resize(static_cast<size_t>(T) * N);
    for (auto& v : sector) {
        const int c = pick(rng);
        v = c >= 95 ? NaN : c >= 90 ? -1.0f : static_cast<float>(c % 4);
    }
    return dp;
}

static std::vector<uint8_t> make_mask(unsigned T, unsigned N, unsigned seed)
{
    std::mt19937 rng(seed);
    std::uniform_int_distribution<int> pick(0, 99);
    std::vector<uint8_t> m(static_cast<size_t>(T) * N);
    for (auto& v : m) v = pick(rng) < 85;
    return m;
}

static Panel run(const std::string& xml, const DataPanel& dp, const std::vector<uint8_t>* universe = nullptr)
{
    auto alphas = compile_xml(xml, &dp, universe);
    EXPECT_EQ(alphas.size(), 1u);
    auto& pipe = alphas[0].pipeline;
    for (Module* m : pipe) { m->numAssets = dp.N; m->init(); }
    Panel out;
    Row buf(dp.N);
    SignalVector sv{buf.data(), dp.N};
    for (unsigned t = 0; t < dp.T; ++t) {
        if (!pipe.empty() && !pipe[0]->skips_nan_init()) sv.fill_nan();
        for (Module* m : pipe) { m->calculate(t, sv); sv.inf_to_nan(); }
        out.push_back(buf);
    }
    return out;
}

static float at(const DataPanel& dp, const char* col, unsigned t, unsigned n)
{
    return dp.arrays.at(col)[static_cast<size_t>(t) * dp.N + n];
}

static void expect_same(float got, double want, const std::string& what, float tol = 0.0f)
{
    if (std::isnan(want)) { EXPECT_TRUE(std::isnan(got)) << what << " got " << got; return; }
    if (tol == 0.0f) EXPECT_EQ(got, static_cast<float>(want)) << what;
    else EXPECT_NEAR(got, want, tol) << what;
}

static const char* kSaved =
    R"(<data expr="a" delay="0"/><signal id="A"/><data expr="b" delay="0"/><signal id="B"/><data expr="c" delay="0"/><signal id="C"/>)";

TEST(SignalBinary, ElementwiseOps)
{
    const auto dp = make_panel(40, 25, 1);
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const std::map<std::string, double (*)(double, double)> ops = {
        {"add", [](double a, double b) { return a + b; }}, {"sub", [](double a, double b) { return a - b; }},
        {"mul", [](double a, double b) { return a * b; }}, {"div", [](double a, double b) { return a / b; }},
        {"min", [](double a, double b) { return std::min(a, b); }}, {"max", [](double a, double b) { return std::max(a, b); }}};
    for (const auto& [tag, f] : ops) {
        const auto got = run(std::string(kSaved) + "<" + tag + R"( signal_1="A" signal_2="B"/>)", dp);
        for (unsigned t = 0; t < dp.T; ++t)
            for (unsigned n = 0; n < dp.N; ++n) {
                const double a = at(dp, "a", t, n), b = at(dp, "b", t, n);
                double want = std::isfinite(a) && std::isfinite(b) ? static_cast<double>(static_cast<float>(f(a, b))) : nan;
                if (!std::isfinite(want)) want = nan;   // ±inf from div by zero becomes NaN
                expect_same(got[t][n], want, tag);
            }
    }
}

TEST(SignalBinary, MeanAndNanMean)
{
    const auto dp = make_panel(40, 25, 2);
    const auto mean = run(std::string(kSaved) + R"(<mean signal_1="A" signal_2="B" signal_3="C"/>)", dp);
    const auto nanm = run(std::string(kSaved) + R"(<nanmean signal_1="A" signal_2="B" signal_3="C" min_count="2"/>)", dp);
    for (unsigned t = 0; t < dp.T; ++t)
        for (unsigned n = 0; n < dp.N; ++n) {
            double sum = 0; int k = 0;
            for (const char* col : {"a", "b", "c"}) {
                const double v = at(dp, col, t, n);
                if (std::isfinite(v)) { sum += v; ++k; }
            }
            const double nan = std::numeric_limits<double>::quiet_NaN();
            expect_same(mean[t][n], k == 3 ? static_cast<float>(sum / 3) : nan, "mean");
            expect_same(nanm[t][n], k >= 2 ? static_cast<float>(sum / k) : nan, "nanmean");
        }
}

TEST(SignalBinary, UnaryMinMaxUnchanged)
{
    const auto dp = make_panel(10, 6, 3);
    const auto got = run(R"(<data expr="a" delay="0"/><max value="0.5"/>)", dp);
    for (unsigned t = 0; t < dp.T; ++t)
        for (unsigned n = 0; n < dp.N; ++n) {
            const double a = at(dp, "a", t, n);
            expect_same(got[t][n], std::isfinite(a) ? std::max(a, 0.5) : std::numeric_limits<double>::quiet_NaN(), "unary max");
        }
}

TEST(SignalBinary, OverwritesCurrentChain)
{
    const auto dp = make_panel(10, 6, 4);
    const auto a = run(std::string(kSaved) + R"(<add signal_1="A" signal_2="B"/>)", dp);
    const auto b = run(std::string(kSaved) + R"(<data expr="close" delay="0"/><rank/><add signal_1="A" signal_2="B"/>)", dp);
    for (unsigned t = 0; t < dp.T; ++t)
        for (unsigned n = 0; n < dp.N; ++n) expect_same(b[t][n], a[t][n], "current chain ignored");
}

TEST(Gate, Bounds)
{
    const auto dp = make_panel(40, 25, 5);
    const auto got = run(std::string(kSaved) + R"(<gate signal_1="A" signal_2="B" gt="0" le="2"/>)", dp);
    const auto any = run(std::string(kSaved) + R"(<gate signal_1="A" signal_2="B"/>)", dp);
    for (unsigned t = 0; t < dp.T; ++t)
        for (unsigned n = 0; n < dp.N; ++n) {
            const float a = at(dp, "a", t, n), b = at(dp, "b", t, n);
            const double nan = std::numeric_limits<double>::quiet_NaN();
            expect_same(got[t][n], std::isfinite(b) && b > 0 && b <= 2 ? a : nan, "gate bounds");
            expect_same(any[t][n], std::isfinite(b) ? a : nan, "gate finite");
        }
}

static Row oracle_resid(const DataPanel& dp, const std::vector<uint8_t>& mask, unsigned t, bool group)
{
    const unsigned N = dp.N;
    Row out(N, NaN);
    std::vector<unsigned> cells;
    for (unsigned n = 0; n < N; ++n) {
        const float y = at(dp, "a", t, n), x1 = at(dp, "b", t, n), x2 = at(dp, "c", t, n), g = at(dp, "sector", t, n);
        if (mask[static_cast<size_t>(t) * N + n] && std::isfinite(y) && std::isfinite(x1) && std::isfinite(x2) &&
            (!group || (std::isfinite(g) && g >= 0)))
            cells.push_back(n);
    }
    if (cells.size() < 2) return out;
    const Eigen::Index k = group ? 2 : 3, m = static_cast<Eigen::Index>(cells.size());
    Eigen::MatrixXd X(m, k);
    Eigen::VectorXd Y(m);
    for (Eigen::Index r = 0; r < m; ++r) {
        const unsigned n = cells[r];
        Y(r) = at(dp, "a", t, n); X(r, 0) = at(dp, "b", t, n); X(r, 1) = at(dp, "c", t, n);
        if (!group) X(r, 2) = 1.0;
    }
    if (group) {
        std::map<float, std::vector<Eigen::Index>> gm;
        for (Eigen::Index r = 0; r < m; ++r) gm[at(dp, "sector", t, cells[r])].push_back(r);
        for (auto& [code, idx] : gm) {
            double yb = 0; Eigen::Vector2d xb = Eigen::Vector2d::Zero();
            for (auto r : idx) { yb += Y(r); xb += X.row(r).transpose(); }
            yb /= idx.size(); xb /= static_cast<double>(idx.size());
            for (auto r : idx) { Y(r) -= yb; X.row(r) -= xb.transpose(); }
        }
    }
    const Eigen::VectorXd e = Y - X * X.completeOrthogonalDecomposition().solve(Y);
    for (Eigen::Index r = 0; r < m; ++r) out[cells[r]] = static_cast<float>(e(r));
    return out;
}

TEST(CsResid, MatchesOls)
{
    const auto dp = make_panel(30, 60, 6);
    const auto mask = make_mask(dp.T, dp.N, 7);
    for (bool group : {false, true}) {
        const auto got = run(std::string(kSaved) + R"(<csresid signal_1="A" signal_2="B" signal_3="C")" +
                             (group ? R"( group="sector")" : "") + "/>", dp, &mask);
        for (unsigned t = 0; t < dp.T; ++t) {
            const Row want = oracle_resid(dp, mask, t, group);
            for (unsigned n = 0; n < dp.N; ++n) expect_same(got[t][n], want[n], group ? "csresid group" : "csresid", 2e-4f);
        }
    }
}

TEST(BucketDemean, MatchesOracle)
{
    const auto dp = make_panel(30, 50, 8);
    const auto mask = make_mask(dp.T, dp.N, 9);
    const auto got = run(std::string(kSaved) + R"(<bucketdemean signal_1="A" signal_2="B" n="4"/>)", dp, &mask);
    for (unsigned t = 0; t < dp.T; ++t) {
        std::vector<unsigned> cells;
        for (unsigned n = 0; n < dp.N; ++n)
            if (mask[static_cast<size_t>(t) * dp.N + n] && std::isfinite(at(dp, "a", t, n)) && std::isfinite(at(dp, "b", t, n)))
                cells.push_back(n);
        std::vector<int> bucket(dp.N, -1);
        for (unsigned n : cells) {
            double less = 0, eq = 0;
            for (unsigned m : cells) { less += at(dp, "b", t, m) < at(dp, "b", t, n); eq += at(dp, "b", t, m) == at(dp, "b", t, n); }
            const double pct = (less + 1 + (eq - 1) / 2) / cells.size();
            bucket[n] = std::min(3, std::max(0, static_cast<int>(std::ceil(pct * 4)) - 1));
        }
        for (unsigned n = 0; n < dp.N; ++n) {
            if (bucket[n] < 0) { EXPECT_TRUE(std::isnan(got[t][n])); continue; }
            double s = 0; int c = 0;
            for (unsigned m : cells) if (bucket[m] == bucket[n]) { s += at(dp, "a", t, m); ++c; }
            expect_same(got[t][n], at(dp, "a", t, n) - s / c, "bucketdemean", 1e-5f);
        }
    }
}

TEST(CsDemedianWinsorQ, MatchOracle)
{
    const auto dp = make_panel(30, 41, 10);
    const auto mask = make_mask(dp.T, dp.N, 11);
    const auto med = run(R"(<data expr="a" delay="0"/><csdemedian/>)", dp, &mask);
    const auto win = run(R"(<data expr="a" delay="0"/><cswinsorq lo="0.1" hi="0.8"/>)", dp, &mask);
    for (unsigned t = 0; t < dp.T; ++t) {
        std::vector<double> v;
        for (unsigned n = 0; n < dp.N; ++n)
            if (mask[static_cast<size_t>(t) * dp.N + n] && std::isfinite(at(dp, "a", t, n))) v.push_back(at(dp, "a", t, n));
        std::sort(v.begin(), v.end());
        const size_t k = v.size();
        const double median = k % 2 ? v[k / 2] : 0.5 * (v[k / 2 - 1] + v[k / 2]);
        auto q = [&](double p) { double pos = p * (k - 1); size_t lo = static_cast<size_t>(std::floor(pos));
                                 size_t hi = std::min(lo + 1, k - 1); return v[lo] + (pos - lo) * (v[hi] - v[lo]); };
        for (unsigned n = 0; n < dp.N; ++n) {
            const double a = at(dp, "a", t, n);
            const bool in = mask[static_cast<size_t>(t) * dp.N + n] && std::isfinite(a);
            const double nan = std::numeric_limits<double>::quiet_NaN();
            expect_same(med[t][n], in ? static_cast<float>(a - median) : nan, "csdemedian");
            expect_same(win[t][n], in ? static_cast<float>(std::min(q(static_cast<double>(0.8f)), std::max(q(static_cast<double>(0.1f)), a))) : nan, "cswinsorq");
        }
    }
}

TEST(SignalInputs, FirstValidBarIsLatestInput)
{
    const auto dp = make_panel(60, 4, 12);
    for (const char* xml : {
             R"(<data expr="a" delay="0"/><tsmean days="10"/><signal id="A"/><data expr="b" delay="0"/><signal id="B"/><add signal_1="A" signal_2="B"/>)",
             R"(<data expr="b" delay="0"/><signal id="B"/><data expr="a" delay="0"/><tsmean days="10"/><signal id="A"/><add signal_1="B" signal_2="A"/>)",
             R"(<data expr="a" delay="0"/><tsmean days="10"/><signal id="A"/><data expr="b" delay="0"/><signal id="B"/><gate signal_1="B" signal_2="A"/>)"}) {
        auto alphas = compile_xml(xml, &dp);
        EXPECT_EQ(first_valid_bar(alphas[0], 5), 14u) << xml;
    }
    // A short input does not shorten a long one: chain after the op adds its own lookback.
    auto alphas = compile_xml(R"(<data expr="a" delay="0"/><tsmean days="10"/><signal id="A"/><data expr="b" delay="0"/><signal id="B"/><sub signal_1="A" signal_2="B"/><tsmean days="5"/>)", &dp);
    EXPECT_EQ(first_valid_bar(alphas[0], 5), 18u);
}

static void expect_error(const std::string& xml, const std::string& token)
{
    const auto dp = make_panel(10, 4, 13);
    try {
        (void)compile_xml(xml, &dp);
        FAIL() << "expected CompileError for " << xml;
    } catch (const CompileError& e) {
        EXPECT_NE(std::string(e.what()).find(token), std::string::npos) << e.what();
    }
}

TEST(SignalInputs, CompileErrors)
{
    const std::string s = kSaved;
    expect_error(s + R"(<add signal_1="A" signal_2="Z"/>)", "unknown signal id 'Z'");
    expect_error(s + R"(<add signal_1="A" signal_3="B"/>)", "without gaps");
    expect_error(s + R"(<add signal_0="A" signal_1="B"/>)", "signal_0");
    expect_error(s + R"(<add signal_1="A" signal_01="B"/>)", "signal_01");
    expect_error(s + R"(<add/>)", "requires signal_1 and signal_2");
    expect_error(s + R"(<add signal_1="A" signal_2="B" signal_3="C"/>)", "takes 2 signal_N inputs, got 3");
    expect_error(s + R"(<mean signal_1="A"/>)", "takes 2 or more");
    expect_error(s + R"(<gate signal_1="A"/>)", "takes 2 signal_N inputs, got 1");
    expect_error(s + R"(<rank signal_1="A" signal_2="B"/>)", "does not take signal_N");
    expect_error(s + R"(<add signal_1="A" signal_2="B" input="C"/>)", "cannot be combined with input=");
    expect_error(s + R"(<add signal_1="A" signal_2="B" min_count="1"/>)", "min_count");
    expect_error(s + R"(<nanmean signal_1="A" signal_2="B" min_count="0"/>)", "min_count must be >= 1");
    expect_error(s + R"(<gate signal_1="A" signal_2="B" gt="x"/>)", "gt");
    expect_error(R"(<data expr="a" delay="0"/><cswinsorq lo="0.9" hi="0.1"/>)", "cswinsorq");
}
