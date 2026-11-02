// Oracle tests for the time-series modules tsdelta, tssum, tsstddev, tsargmax, tsargmin, tsfill,
// tsmax, tsmin, tsewma.
// Expectations are recomputed by brute force from the stored input history.
#include <gtest/gtest.h>

#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "module.h"
#include "signal_vector.h"
#include "compiler/dag_builder.h"
#include "data/data_panel.h"
#include "engine/simulator.h"
#include "modules/Delay.h"
#include "modules/TsDelta.h"
#include "modules/TsSum.h"
#include "modules/TsStdDev.h"
#include "modules/TsArgExtreme.h"
#include "modules/TsFill.h"
#include "modules/TsEwma.h"

namespace {

constexpr float kNaN = std::numeric_limits<float>::quiet_NaN();
constexpr float kInf = std::numeric_limits<float>::infinity();
using Hist = std::vector<std::vector<float>>;  // [t][n]

// Columns: 0 all-NaN, 1 constant, 2 long NaN gap (bars 100..179), 3 small integers (ties),
// 4.. random floats with ~15% NaN and some +-inf.
Hist make_history(unsigned T, unsigned N, unsigned seed) {
    std::mt19937 rng(seed);
    std::uniform_real_distribution<float> u(-50.0f, 50.0f);
    std::uniform_int_distribution<int> small(0, 3);
    std::uniform_real_distribution<float> p(0.0f, 1.0f);
    Hist h(T, std::vector<float>(N));
    for (unsigned t = 0; t < T; ++t) {
        for (unsigned n = 0; n < N; ++n) {
            float v;
            if (n == 0) v = kNaN;
            else if (n == 1) v = 7.25f;
            else if (n == 2) v = (t >= 100 && t < 180) ? kNaN : u(rng);
            else if (n == 3) v = (float)small(rng);
            else {
                float r = p(rng);
                v = r < 0.15f ? kNaN : r < 0.17f ? kInf : r < 0.19f ? -kInf : u(rng);
            }
            h[t][n] = v;
        }
    }
    return h;
}

Hist run_module(Module& m, const Hist& h) {
    const unsigned N = (unsigned)h[0].size();
    m.numAssets = N;
    m.init();
    Hist out;
    std::vector<float> buf(N);
    for (unsigned t = 0; t < h.size(); ++t) {
        buf = h[t];
        SignalVector sv{buf.data(), N};
        m.calculate(t, sv);
        out.push_back(buf);
    }
    return out;
}

// Finite values in the window [t-days+1, t], in order of increasing lag.
std::vector<std::pair<unsigned, double>> window(const Hist& h, unsigned t, unsigned n, unsigned days) {
    std::vector<std::pair<unsigned, double>> w;
    for (unsigned lag = 0; lag < days && lag <= t; ++lag) {
        float v = h[t - lag][n];
        if (std::isfinite(v)) w.push_back({lag, (double)v});
    }
    return w;
}

void expect_same(float got, float want, double tol, const char* what, unsigned t, unsigned n) {
    if (std::isnan(want)) {
        EXPECT_TRUE(std::isnan(got)) << what << " t=" << t << " n=" << n << " got " << got;
    } else if (tol == 0.0) {
        EXPECT_EQ(got, want) << what << " t=" << t << " n=" << n;
    } else {
        EXPECT_NEAR(got, want, tol * std::max(1.0, std::fabs((double)want))) << what << " t=" << t << " n=" << n;
    }
}

const unsigned kT = 400, kN = 12;
const unsigned kDays[] = {1, 2, 5, 37};

}  // namespace

TEST(TsModules, TsDeltaMatchesOracle) {
    auto h = make_history(kT, kN, 1);
    for (unsigned days : kDays) {
        MTsDelta m; m.days = IntParameter((int)days);
        auto out = run_module(m, h);
        for (unsigned t = 0; t < kT; ++t)
            for (unsigned n = 0; n < kN; ++n) {
                float want = kNaN;
                if (t >= days && std::isfinite(h[t][n]) && std::isfinite(h[t - days][n]))
                    want = h[t][n] - h[t - days][n];
                expect_same(out[t][n], want, 0.0, "tsdelta", t, n);
            }
    }
}

TEST(TsModules, TsDeltaDays1EqualsXMinusDelay1BitForBit) {
    auto h = make_history(kT, kN, 2);
    MTsDelta d; d.days = IntParameter(1);
    MDelay dl; dl.days = IntParameter(1);
    auto out = run_module(d, h);
    auto lag = run_module(dl, h);
    for (unsigned t = 0; t < kT; ++t)
        for (unsigned n = 0; n < kN; ++n) {
            float x = h[t][n], y = lag[t][n];
            float want = (std::isfinite(x) && std::isfinite(y)) ? x - y : kNaN;  // MSub rule
            if (std::isnan(want)) EXPECT_TRUE(std::isnan(out[t][n]));
            else EXPECT_EQ(std::memcmp(&out[t][n], &want, sizeof(float)), 0) << t << " " << n;
        }
}

// tssum / tsstddev use rolling double sums (add new, subtract leaving), the oracle sums the
// window directly; the orders differ, so compare with relative tolerance 1e-5 (float output).
TEST(TsModules, TsSumMatchesOracle) {
    auto h = make_history(kT, kN, 3);
    for (unsigned days : kDays)
        for (unsigned mc : {1u, days > 1 ? days - 1 : 1u, days}) {
            MTsSum m; m.days = IntParameter((int)days); m.min_count = IntParameter((int)mc);
            auto out = run_module(m, h);
            for (unsigned t = 0; t < kT; ++t)
                for (unsigned n = 0; n < kN; ++n) {
                    auto w = window(h, t, n, days);
                    float want = kNaN;
                    if (w.size() >= mc) {
                        double s = 0; for (auto& e : w) s += e.second;
                        want = (float)s;
                    }
                    expect_same(out[t][n], want, 1e-5, "tssum", t, n);
                }
        }
}

TEST(TsModules, TsSumDefaultMinCountIsDays) {
    auto h = make_history(kT, kN, 4);
    MTsSum m; m.days = IntParameter(5);
    auto out = run_module(m, h);
    for (unsigned t = 0; t < 4; ++t) EXPECT_TRUE(std::isnan(out[t][1]));
    EXPECT_FLOAT_EQ(out[4][1], 5 * 7.25f);
    // Long gap: NaN while the window holds fewer than 5 finite values.
    for (unsigned t = 100; t < 184; ++t) EXPECT_TRUE(std::isnan(out[t][2])) << t;
    EXPECT_TRUE(std::isfinite(out[184][2]));
    for (unsigned t = 0; t < kT; ++t) EXPECT_TRUE(std::isnan(out[t][0]));
}

TEST(TsModules, TsStdDevMatchesOracle) {
    auto h = make_history(kT, kN, 5);
    double worst = 0.0;
    for (unsigned days : kDays)
        for (unsigned ddof : {0u, 1u})
            for (unsigned mc : {1u, days}) {
                MTsStdDev m; m.days = IntParameter((int)days); m.ddof = IntParameter((int)ddof);
                m.min_count = IntParameter((int)mc);
                auto out = run_module(m, h);
                for (unsigned t = 0; t < kT; ++t)
                    for (unsigned n = 0; n < kN; ++n) {
                        auto w = window(h, t, n, days);
                        float want = kNaN;
                        const int k = (int)w.size();
                        if (k >= (int)mc && k - (int)ddof > 0) {
                            double mean = 0; for (auto& e : w) mean += e.second; mean /= k;
                            double ss = 0; for (auto& e : w) ss += (e.second - mean) * (e.second - mean);
                            want = (float)std::sqrt(ss / (k - (int)ddof));
                        }
                        // Rolling sum_sq - sum^2/n vs two-pass oracle: bound 1e-6 * max(1, want)
                        // (observed max error / max(1, want) is printed below).
                        if (std::isnan(want)) {
                            EXPECT_TRUE(std::isnan(out[t][n])) << t << " " << n;
                        } else {
                            const double scale = std::max(1.0, (double)want);
                            worst = std::max(worst, std::fabs((double)out[t][n] - want) / scale);
                            EXPECT_NEAR(out[t][n], want, 1e-6 * scale) << days << " " << t << " " << n;
                        }
                    }
            }
    std::printf("tsstddev oracle: max |err| / max(1, want) = %.3g\n", worst);
    MTsStdDev c; c.days = IntParameter(10);
    auto out = run_module(c, h);
    EXPECT_EQ(out[50][1], 0.0f);  // constant series
}

// Volatile random walk, then a long constant stretch with interleaved NaNs: every bar whose window
// holds only the constant must give exactly 0 (rolling sums alone leave ~1e-4 residue at days=252).
TEST(TsModules, TsStdDevConstantWindowIsExactlyZero) {
    const unsigned T = 1200;
    std::mt19937 rng(21);
    std::normal_distribution<float> step(0.0f, 50.0f);
    std::vector<float> x(T);
    float px = 1000.0f;
    for (unsigned t = 0; t < 400; ++t) { px += step(rng); x[t] = px; }
    const float c = 1234.5678f;
    for (unsigned t = 400; t < T; ++t) x[t] = (t % 7 == 3) ? kNaN : c;
    // One tiny change inside the constant stretch (0.01; a 1-ulp change is below the rolling-sum
    // resolution at days=252 and clamps to 0 through the variance path, not the constant-run path).
    x[1000] = c + 0.01f;
    Hist h(T, std::vector<float>(1));
    for (unsigned t = 0; t < T; ++t) h[t][0] = x[t];
    for (unsigned days : {20u, 252u}) {
        MTsStdDev m; m.days = IntParameter((int)days); m.min_count = IntParameter(1);
        auto out = run_module(m, h);
        unsigned zeros = 0, nonzero = 0;
        for (unsigned t = 0; t < T; ++t) {
            auto w = window(h, t, 0, days);
            bool constant = w.size() >= 2;
            for (auto& e : w) constant = constant && e.second == (double)c;
            bool tiny = false;
            for (auto& e : w) tiny = tiny || e.second == (double)x[1000];
            if (constant) {
                EXPECT_EQ(out[t][0], 0.0f) << days << " " << t;
                ++zeros;
            } else if (tiny && w.size() >= 2) {
                EXPECT_GT(out[t][0], 0.0f) << days << " " << t;  // not forced to 0
                ++nonzero;
            }
        }
        EXPECT_GT(zeros, 300u);
        EXPECT_GT(nonzero, days / 2);
    }
}

template <bool IsMax>
void check_argextreme(unsigned seed) {
    auto h = make_history(kT, kN, seed);
    for (unsigned days : kDays)
        for (unsigned mc : {1u, days}) {
            MTsArgExtreme<IsMax> m; m.days = IntParameter((int)days); m.min_count = IntParameter((int)mc);
            auto out = run_module(m, h);
            for (unsigned t = 0; t < kT; ++t)
                for (unsigned n = 0; n < kN; ++n) {
                    auto w = window(h, t, n, days);
                    float want = kNaN;
                    if (w.size() >= mc && !w.empty()) {
                        // Increasing lag order; strict comparison keeps the smallest lag on ties.
                        auto best = w[0];
                        for (auto& e : w)
                            if (IsMax ? e.second > best.second : e.second < best.second) best = e;
                        want = (float)best.first;
                    }
                    expect_same(out[t][n], want, 0.0, IsMax ? "tsargmax" : "tsargmin", t, n);
                }
            if (days > 1)
                for (unsigned t = days; t < kT; ++t) {
                    EXPECT_EQ(out[t][1], 0.0f);  // constant -> 0
                }
        }
}

TEST(TsModules, TsArgMaxMatchesOracle) { check_argextreme<true>(6); }
TEST(TsModules, TsArgMinMatchesOracle) { check_argextreme<false>(7); }

// tsmax / tsmin: plain window scan over finite values.
template <bool IsMax>
void check_extreme_value(unsigned seed) {
    auto h = make_history(kT, kN, seed);
    for (unsigned days : {1u, 2u, 5u, 37u, 100u})
        for (unsigned mc : {1u, 2u, days}) {
            if (mc > days) continue;
            MTsArgExtreme<IsMax, true> m; m.days = IntParameter((int)days); m.min_count = IntParameter((int)mc);
            auto out = run_module(m, h);
            for (unsigned t = 0; t < kT; ++t)
                for (unsigned n = 0; n < kN; ++n) {
                    unsigned cnt = 0;
                    double best = 0.0;
                    for (unsigned lag = 0; lag < days && lag <= t; ++lag) {
                        float v = h[t - lag][n];
                        if (!std::isfinite(v)) continue;
                        if (cnt == 0 || (IsMax ? v > best : v < best)) best = v;
                        ++cnt;
                    }
                    float want = (cnt >= mc && cnt > 0) ? (float)best : kNaN;
                    expect_same(out[t][n], want, 0.0, IsMax ? "tsmax" : "tsmin", t, n);
                }
            // Gap 100..179 (80 bars) is longer than days<=37: NaN once the window holds no finite value.
            if (days <= 37) {
                for (unsigned t = 100 + days; t < 180; ++t) {
                    EXPECT_TRUE(std::isnan(out[t][2])) << days << " " << t;
                }
            }
            // A NaN today does not force NaN when mc=1 and the window still has a finite value.
            if (days >= 2 && mc == 1) {
                EXPECT_TRUE(std::isfinite(out[100][2]));
            }
            for (unsigned t = days - 1; t < kT; ++t) EXPECT_EQ(out[t][1], 7.25f);
        }
}

TEST(TsModules, TsMaxMatchesOracle) { check_extreme_value<true>(16); }
TEST(TsModules, TsMinMatchesOracle) { check_extreme_value<false>(17); }

// tsewma vs independent formulas. adjust=1: direct weights sum_{finite s<=t} (1-a)^(t-s) x_s / sum (1-a)^(t-s).
// adjust=0: recursion y <- (d^k y + a x) / (d^k + a), d = 1-a, k = bars since the previous finite value.
// Tolerance 1e-6 relative (absolute below |want| = 1): the module accumulates in double in a different
// order and rounds the result to float (~6e-8 relative).
TEST(TsModules, TsEwmaMatchesOracle) {
    auto h = make_history(kT, kN, 18);
    // Column 5: leading 30 NaN bars.
    for (unsigned t = 0; t < 30; ++t) h[t][5] = kNaN;
    for (double a : {1.0, 0.5, 2.0 / 21.0, 0.01})
        for (int adj : {0, 1})
            for (int mc : {1, 3, 50}) {
                MTsEwma m; m.span = FloatParameter(-1.0f); m.alpha = FloatParameter((float)a);
                m.adjust = IntParameter(adj); m.min_count = IntParameter(mc);
                auto out = run_module(m, h);
                const double af = (double)(float)a;  // module receives alpha as float
                const double d = 1.0 - af;
                for (unsigned n = 0; n < kN; ++n) {
                    double y = 0.0; int cnt = 0; unsigned last = 0;
                    for (unsigned t = 0; t < kT; ++t) {
                        float x = h[t][n];
                        if (std::isfinite(x)) {
                            if (adj) {
                                double num = 0.0, den = 0.0;
                                for (unsigned s2 = 0; s2 <= t; ++s2)
                                    if (std::isfinite(h[s2][n])) {
                                        double w = std::pow(d, (double)(t - s2));
                                        num += w * h[s2][n]; den += w;
                                    }
                                y = num / den;
                            } else if (cnt == 0) {
                                y = x;
                            } else {
                                double dk = std::pow(d, (double)(t - last));
                                y = (dk * y + af * x) / (dk + af);
                            }
                            ++cnt; last = t;
                        }
                        float want = cnt >= mc ? (float)y : kNaN;
                        expect_same(out[t][n], want, 1e-6, "tsewma", t, n);
                    }
                }
                for (unsigned t = (unsigned)mc - 1; t < kT; ++t) EXPECT_EQ(out[t][1], 7.25f);
                for (unsigned t = 0; t < 30; ++t) EXPECT_TRUE(std::isnan(out[t][5]));
            }
}

TEST(TsModules, TsEwmaAlphaFromSpanAndHalflife) {
    auto h = make_history(200, kN, 19);
    auto run = [&](float span, float hl, float a) {
        MTsEwma m; m.span = FloatParameter(span); m.halflife = FloatParameter(hl); m.alpha = FloatParameter(a);
        return run_module(m, h);
    };
    MTsEwma probe; probe.numAssets = 1; probe.init();
    EXPECT_DOUBLE_EQ(probe.alpha_d, 2.0 / 21.0);  // default span 20
    MTsEwma hl; hl.span = FloatParameter(-1.0f); hl.halflife = FloatParameter(10.0f); hl.numAssets = 1; hl.init();
    EXPECT_DOUBLE_EQ(hl.alpha_d, 1.0 - std::exp(std::log(0.5) / 10.0));
    // Halflife h gives weight 0.5 after h bars.
    EXPECT_NEAR(std::pow(1.0 - hl.alpha_d, 10.0), 0.5, 1e-12);
    // alpha=1 reproduces the input on finite bars and holds the last value on missing ones.
    auto a1 = run(-1.0f, -1.0f, 1.0f);
    for (unsigned t = 0; t < 200; ++t) {
        if (std::isfinite(h[t][4])) {
            EXPECT_EQ(a1[t][4], h[t][4]);
        } else if (t > 0) {
            expect_same(a1[t][4], a1[t - 1][4], 0.0, "hold", t, 4);
        }
    }
}

// A constant input must keep the mean exactly constant: the `weighted != x` guard skips the
// update, which would otherwise drift by rounding (0.1f is not exact in the weighted average).
TEST(TsModules, TsEwmaConstantInputStateIsExact) {
    for (int adj : {0, 1})
        for (float a : {0.3f, 2.0f / 21.0f, 0.01f}) {
            MTsEwma m; m.span = FloatParameter(-1.0f); m.alpha = FloatParameter(a); m.adjust = IntParameter(adj);
            m.numAssets = 1; m.init();
            bool seen = false;
            for (unsigned t = 0; t < 500; ++t) {
                float v = (t < 3 || (t % 7 == 0) || (t >= 200 && t < 260)) ? kNaN : 0.1f;
                SignalVector sv{&v, 1};
                const bool fin = std::isfinite(v);
                m.calculate(t, sv);
                seen = seen || fin;
                if (seen) {
                    EXPECT_EQ(m.weighted[0], (double)0.1f) << adj << " " << a << " " << t;
                    EXPECT_EQ(v, 0.1f) << t;
                }
            }
        }
}

TEST(TsModules, TsFillMatchesOracle) {
    auto h = make_history(kT, kN, 8);
    for (unsigned days : kDays) {
        MTsFill m; m.days = IntParameter((int)days);
        auto out = run_module(m, h);
        for (unsigned t = 0; t < kT; ++t)
            for (unsigned n = 0; n < kN; ++n) {
                float want = h[t - t % days][n];
                if (std::isnan(want)) EXPECT_TRUE(std::isnan(out[t][n])) << days << " " << t << " " << n;
                else EXPECT_EQ(std::memcmp(&out[t][n], &want, sizeof(float)), 0) << days << " " << t << " " << n;
            }
        if (days == 1)
            for (unsigned t = 0; t < kT; ++t)
                for (unsigned n = 0; n < kN; ++n) expect_same(out[t][n], h[t][n], 0.0, "tsfill identity", t, n);
    }
    // Explicit block behaviour, days=3: refresh bars 0, 3, 6.
    Hist x = {{1.0f}, {2.0f}, {kNaN}, {kNaN}, {5.0f}, {6.0f}, {7.0f}};
    MTsFill m; m.days = IntParameter(3);
    auto out = run_module(m, x);
    EXPECT_EQ(out[1][0], 1.0f);  // finite input inside a block is ignored
    EXPECT_EQ(out[2][0], 1.0f);  // NaN input inside a block is ignored
    EXPECT_TRUE(std::isnan(out[3][0]));  // NaN at the refresh bar ...
    EXPECT_TRUE(std::isnan(out[4][0]));  // ... stays NaN for the block despite finite inputs
    EXPECT_TRUE(std::isnan(out[5][0]));
    EXPECT_EQ(out[6][0], 7.0f);
}

// ---------------------------------------------------------------------------
// XML compiler
// ---------------------------------------------------------------------------
namespace {

DataPanel make_panel(unsigned T = 60, unsigned N = 5, bool with_nan = false) {
    DataPanel dp;
    dp.T = T; dp.N = N;
    dp.timestamps.resize(T);
    for (unsigned t = 0; t < T; ++t) dp.timestamps[t] = 1704067200000LL + (int64_t)t * 86400000LL;
    dp.symbols.resize(N);
    for (unsigned n = 0; n < N; ++n) dp.symbols[n] = "SYM" + std::to_string(n);
    for (const char* col : {"open", "high", "low", "close", "volume"}) {
        auto& arr = dp.arrays[col];
        arr.resize(T * N);
        for (unsigned t = 0; t < T; ++t)
            for (unsigned n = 0; n < N; ++n) {
                float px = 100.0f + 10.0f * n + (float)std::sin(0.3 * t + n) * 3.0f + 0.2f * t;
                if (with_nan && std::string(col) == "close" && ((t * 7 + n * 3) % 11 == 0 || (n == 4 && t >= 20 && t < 30)))
                    px = kNaN;
                arr[t * N + n] = px;
            }
    }
    return dp;
}

template <class M>
M* compile_one(const std::string& xml, const DataPanel& dp) {
    static std::vector<std::vector<CompiledAlpha>> keep;  // keep modules alive
    keep.push_back(compile_xml(xml, &dp));
    EXPECT_EQ(keep.back().size(), 1u);
    for (auto& mod : keep.back()[0].modules)
        if (auto* m = dynamic_cast<M*>(mod.get())) return m;
    return nullptr;
}

void expect_error(const std::string& xml, const DataPanel& dp, const std::string& token) {
    try {
        (void)compile_xml(xml, &dp);
        FAIL() << "expected CompileError for " << xml;
    } catch (const CompileError& e) {
        EXPECT_NE(std::string(e.what()).find(token), std::string::npos) << e.what();
    }
}

}  // namespace

TEST(TsModulesXml, DefaultsAndExplicitAttributes) {
    auto dp = make_panel();
    const std::string d = R"(<data expr="close"/>)";
    auto* m1 = compile_one<MTsDelta>(d + "<tsdelta/>", dp); ASSERT_TRUE(m1); EXPECT_EQ((int)m1->days, 1);
    m1 = compile_one<MTsDelta>(d + R"(<tsdelta days="5"/>)", dp); ASSERT_TRUE(m1); EXPECT_EQ((int)m1->days, 5);

    auto* m2 = compile_one<MTsSum>(d + "<tssum/>", dp); ASSERT_TRUE(m2);
    EXPECT_EQ((int)m2->days, 20); EXPECT_EQ((int)m2->min_count, 20);
    m2 = compile_one<MTsSum>(d + R"(<tssum days="10" min_count="3"/>)", dp); ASSERT_TRUE(m2);
    EXPECT_EQ((int)m2->days, 10); EXPECT_EQ((int)m2->min_count, 3);

    auto* m3 = compile_one<MTsStdDev>(d + "<tsstddev/>", dp); ASSERT_TRUE(m3);
    EXPECT_EQ((int)m3->days, 20); EXPECT_EQ((int)m3->ddof, 1); EXPECT_EQ((int)m3->min_count, 20);
    m3 = compile_one<MTsStdDev>(d + R"(<tsstddev days="8" ddof="0" min_count="2"/>)", dp); ASSERT_TRUE(m3);
    EXPECT_EQ((int)m3->days, 8); EXPECT_EQ((int)m3->ddof, 0); EXPECT_EQ((int)m3->min_count, 2);
    m3 = compile_one<MTsStdDev>(d + R"(<tsstddev days="1"/>)", dp); ASSERT_TRUE(m3);
    EXPECT_EQ((int)m3->ddof, 1);

    auto* m4 = compile_one<MTsArgMax>(d + "<tsargmax/>", dp); ASSERT_TRUE(m4);
    EXPECT_EQ((int)m4->days, 20); EXPECT_EQ((int)m4->min_count, 20);
    m4 = compile_one<MTsArgMax>(d + R"(<tsargmax days="4" min_count="1"/>)", dp); ASSERT_TRUE(m4);
    EXPECT_EQ((int)m4->days, 4); EXPECT_EQ((int)m4->min_count, 1);
    auto* m5 = compile_one<MTsArgMin>(d + "<tsargmin/>", dp); ASSERT_TRUE(m5);
    EXPECT_EQ((int)m5->days, 20);
    m5 = compile_one<MTsArgMin>(d + R"(<tsargmin days="6" min_count="6"/>)", dp); ASSERT_TRUE(m5);
    EXPECT_EQ((int)m5->days, 6); EXPECT_EQ((int)m5->min_count, 6);

    auto* m6 = compile_one<MTsFill>(d + "<tsfill/>", dp); ASSERT_TRUE(m6); EXPECT_EQ((int)m6->days, 20);
    m6 = compile_one<MTsFill>(d + R"(<tsfill days="3"/>)", dp); ASSERT_TRUE(m6); EXPECT_EQ((int)m6->days, 3);
}

TEST(TsModulesXml, Errors) {
    auto dp = make_panel(60);
    const std::string d = R"(<data expr="close"/>)";
    for (const char* tag : {"tsdelta", "tssum", "tsstddev", "tsargmax", "tsargmin", "tsfill"}) {
        expect_error(d + "<" + tag + R"( foo="1"/>)", dp, "foo");
        expect_error(d + "<" + tag + R"( days="0"/>)", dp, "days");
        expect_error(d + "<" + tag + R"( days="61"/>)", dp, "days");
        expect_error(d + "<" + tag + R"( days="abc"/>)", dp, "days");
    }
    for (const char* tag : {"tssum", "tsstddev", "tsargmax", "tsargmin"}) {
        expect_error(d + "<" + tag + R"( days="5" min_count="0"/>)", dp, "min_count");
        expect_error(d + "<" + tag + R"( days="5" min_count="6"/>)", dp, "min_count");
    }
    expect_error(d + R"(<tsstddev days="5" ddof="5"/>)", dp, "ddof");
    expect_error(d + R"(<tsstddev days="5" ddof="-1"/>)", dp, "ddof");
    expect_error(d + R"(<tsfill min_count="2"/>)", dp, "min_count");
    expect_error(d + R"(<tsdelta min_count="2"/>)", dp, "min_count");
    // No module here has a required attribute; a required one elsewhere still errors through the
    // same compile path (power needs exp).
    expect_error(d + R"(<tsdelta/><power/>)", dp, "exp");
}

TEST(TsModulesXml, PipeExpansion) {
    auto dp = make_panel();
    const std::string d = R"(<data expr="close"/>)";
    for (const char* tag : {"tsdelta", "tssum", "tsstddev", "tsargmax", "tsargmin", "tsfill"}) {
        auto alphas = compile_xml(d + "<" + tag + R"( days="3|7"/>)", &dp);
        EXPECT_EQ(alphas.size(), 2u) << tag;
    }
    EXPECT_EQ(compile_xml(d + R"(<tssum days="7" min_count="2|7"/>)", &dp).size(), 2u);
}

TEST(TsModulesXml, MaxMinEwma) {
    auto dp = make_panel(60);
    const std::string d = R"(<data expr="close"/>)";
    auto* a = compile_one<MTsMax>(d + "<tsmax/>", dp); ASSERT_TRUE(a);
    EXPECT_EQ((int)a->days, 20); EXPECT_EQ((int)a->min_count, 20);
    a = compile_one<MTsMax>(d + R"(<tsmax days="4" min_count="2"/>)", dp); ASSERT_TRUE(a);
    EXPECT_EQ((int)a->days, 4); EXPECT_EQ((int)a->min_count, 2);
    auto* b = compile_one<MTsMin>(d + "<tsmin/>", dp); ASSERT_TRUE(b);
    EXPECT_EQ((int)b->days, 20); EXPECT_EQ((int)b->min_count, 20);
    b = compile_one<MTsMin>(d + R"(<tsmin days="6" min_count="1"/>)", dp); ASSERT_TRUE(b);
    EXPECT_EQ((int)b->days, 6); EXPECT_EQ((int)b->min_count, 1);
    // tsmax must not be the argmax module (and vice versa).
    EXPECT_EQ(compile_one<MTsArgMax>(d + "<tsmax/>", dp), nullptr);

    auto* e = compile_one<MTsEwma>(d + "<tsewma/>", dp); ASSERT_TRUE(e);
    EXPECT_EQ((float)e->span, 20.0f); EXPECT_LT((float)e->halflife, 0.0f); EXPECT_LT((float)e->alpha, 0.0f);
    EXPECT_EQ((int)e->adjust, 1); EXPECT_EQ((int)e->min_count, 1);
    e = compile_one<MTsEwma>(d + R"(<tsewma span="10" adjust="0" min_count="5"/>)", dp); ASSERT_TRUE(e);
    EXPECT_EQ((float)e->span, 10.0f); EXPECT_EQ((int)e->adjust, 0); EXPECT_EQ((int)e->min_count, 5);
    e = compile_one<MTsEwma>(d + R"(<tsewma halflife="3.5"/>)", dp); ASSERT_TRUE(e);
    EXPECT_EQ((float)e->halflife, 3.5f); EXPECT_LT((float)e->span, 0.0f); EXPECT_LT((float)e->alpha, 0.0f);
    e = compile_one<MTsEwma>(d + R"(<tsewma alpha="1"/>)", dp); ASSERT_TRUE(e);
    EXPECT_EQ((float)e->alpha, 1.0f); EXPECT_LT((float)e->span, 0.0f);
    e = compile_one<MTsEwma>(d + R"(<tsewma span="1"/>)", dp); ASSERT_TRUE(e);

    for (const char* tag : {"tsmax", "tsmin"}) {
        expect_error(d + "<" + tag + R"( foo="1"/>)", dp, "foo");
        expect_error(d + "<" + tag + R"( days="0"/>)", dp, "days");
        expect_error(d + "<" + tag + R"( days="61"/>)", dp, "days");
        expect_error(d + "<" + tag + R"( days="5" min_count="0"/>)", dp, "min_count");
        expect_error(d + "<" + tag + R"( days="5" min_count="6"/>)", dp, "min_count");
    }
    expect_error(d + R"(<tsewma span="5" halflife="2"/>)", dp, "at most one");
    expect_error(d + R"(<tsewma span="5" alpha="0.2"/>)", dp, "at most one");
    expect_error(d + R"(<tsewma halflife="2" alpha="0.2"/>)", dp, "at most one");
    expect_error(d + R"(<tsewma span="0.5"/>)", dp, "span");
    expect_error(d + R"(<tsewma alpha="0"/>)", dp, "alpha");
    expect_error(d + R"(<tsewma alpha="1.5"/>)", dp, "alpha");
    expect_error(d + R"(<tsewma halflife="0"/>)", dp, "halflife");
    expect_error(d + R"(<tsewma min_count="0"/>)", dp, "min_count");
    expect_error(d + R"(<tsewma adjust="2"/>)", dp, "adjust");
    expect_error(d + R"(<tsewma days="5"/>)", dp, "days");
    expect_error(d + R"(<tsewma span="abc"/>)", dp, "span");

    EXPECT_EQ(compile_xml(d + R"(<tsmax days="3|7"/>)", &dp).size(), 2u);
    EXPECT_EQ(compile_xml(d + R"(<tsmin days="3|7"/>)", &dp).size(), 2u);
    EXPECT_EQ(compile_xml(d + R"(<tsewma span="5|20"/>)", &dp).size(), 2u);
    EXPECT_EQ(compile_xml(d + R"(<tsewma halflife="5" adjust="0|1"/>)", &dp).size(), 2u);
}

// ---------------------------------------------------------------------------
// Simulator path
// ---------------------------------------------------------------------------
namespace {
std::vector<std::vector<float>> collect(const DataPanel& dp, const std::string& xml, unsigned sim_start = 0) {
    auto alphas = compile_xml(xml, &dp);
    EXPECT_EQ(alphas.size(), 1u);
    SimulatorConfig cfg;
    cfg.T = dp.T; cfg.N = dp.N; cfg.fee_rate = 0.0005;
    cfg.collect_signals = true;
    cfg.sim_start_index = sim_start;
    Simulator sim(dp, cfg);
    auto out = sim.run(alphas[0].pipeline);
    EXPECT_EQ(out.signals.size(), dp.T);
    return out.signals;
}
}  // namespace

TEST(TsModulesPipeline, TsSumIsFiveTimesTsMeanAndTsFillHoldsBlocks) {
    auto dp = make_panel(60, 5, true);
    const std::string base = R"(<data expr="close" delay="1"/>)";
    auto sum = collect(dp, base + R"(<tssum days="5"/>)");
    auto mean = collect(dp, base + R"(<tsmean days="5"/>)");
    auto fill = collect(dp, base + R"(<tsfill days="3"/>)");
    auto raw = collect(dp, base);
    // Blocks stay anchored at panel index 0 regardless of simStart.
    auto fill7 = collect(dp, base + R"(<tsfill days="3"/>)", 7);
    ASSERT_EQ(fill7.size(), fill.size());
    for (unsigned t = 0; t < dp.T; ++t)
        for (unsigned n = 0; n < dp.N; ++n)
            EXPECT_TRUE(std::isnan(fill[t][n]) ? std::isnan(fill7[t][n])
                                               : std::memcmp(&fill[t][n], &fill7[t][n], sizeof(float)) == 0)
                << "sim_start 7 t=" << t << " n=" << n;
    const auto& close = dp.arrays.at("close");
    unsigned complete = 0;
    for (unsigned t = 0; t < dp.T; ++t)
        for (unsigned n = 0; n < dp.N; ++n) {
            bool full = t >= 5;  // delay 1: inputs are close[t-1-lag], lag 0..4
            for (unsigned lag = 0; full && lag < 5; ++lag) full = std::isfinite(close[(t - 1 - lag) * dp.N + n]);
            if (full) {
                ++complete;
                ASSERT_TRUE(std::isfinite(sum[t][n]) && std::isfinite(mean[t][n]));
                // Both from float-rounded double sums; relative 1e-6 covers the float rounding of mean.
                EXPECT_NEAR(sum[t][n], 5.0 * mean[t][n], 1e-6 * std::fabs(sum[t][n])) << t << " " << n;
            }
            // Hold of the delayed close on blocks of 3 bars from panel index 0.
            expect_same(fill[t][n], raw[t - t % 3][n], 0.0, "tsfill", t, n);
        }
    EXPECT_GT(complete, 100u);
    // Gap n=4, close NaN for t in [20,30) -> signal NaN for t in [21,31). Refresh bars 21 and 30 are NaN,
    // so fill is NaN on [21,33) although raw is finite at 31, 32; block [18,21) holds raw[18].
    for (unsigned t = 18; t < 21; ++t) EXPECT_EQ(fill[t][4], raw[18][4]) << t;
    for (unsigned t = 21; t < 33; ++t) EXPECT_TRUE(std::isnan(fill[t][4])) << t;
    EXPECT_TRUE(std::isfinite(raw[31][4]));
    EXPECT_TRUE(std::isfinite(fill[33][4]));
}

// ---------------------------------------------------------------------------
// Micro-benchmark (printout only; enable with LONGSHORT_TS_BENCH=1)
// ---------------------------------------------------------------------------
TEST(TsModules, MicroBenchmark) {
    if (!std::getenv("LONGSHORT_TS_BENCH")) GTEST_SKIP() << "set LONGSHORT_TS_BENCH=1";
    const unsigned T = 3000, N = 6000;
    std::mt19937 rng(11);
    std::uniform_real_distribution<float> u(-1.0f, 1.0f);
    std::vector<float> data((size_t)T * N);
    for (auto& v : data) v = u(rng);
    auto bench = [&](const char* name, Module& m, int days) {
        m.numAssets = N; m.init();
        std::vector<float> buf(N);
        auto t0 = std::chrono::steady_clock::now();
        for (unsigned t = 0; t < T; ++t) {
            std::copy(data.begin() + (size_t)t * N, data.begin() + (size_t)(t + 1) * N, buf.begin());
            SignalVector sv{buf.data(), N};
            m.calculate(t, sv);
        }
        double s = std::chrono::duration<double>(std::chrono::steady_clock::now() - t0).count();
        std::printf("BENCH %-9s days=%3d  %.3f s\n", name, days, s);
    };
    for (int days : {20, 252}) {
        MTsDelta a; a.days = IntParameter(days); bench("tsdelta", a, days);
        MTsSum b; b.days = IntParameter(days); bench("tssum", b, days);
        MTsStdDev c; c.days = IntParameter(days); bench("tsstddev", c, days);
        MTsArgMax d; d.days = IntParameter(days); bench("tsargmax", d, days);
        MTsArgMin e; e.days = IntParameter(days); bench("tsargmin", e, days);
        MTsFill f; f.days = IntParameter(days); bench("tsfill", f, days);
        MTsMax g; g.days = IntParameter(days); bench("tsmax", g, days);
        MTsMin i; i.days = IntParameter(days); bench("tsmin", i, days);
    }
    {
        MTsEwma e; bench("tsewma", e, 0);
    }
}
