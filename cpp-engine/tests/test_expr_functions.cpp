#include <gtest/gtest.h>
#include <cmath>
#include <cstring>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>
#include "module.h"
#include "signal_vector.h"
#include "data/data_panel.h"
#include "compiler/expr_parser.h"
#include "modules/Data.h"
#include "modules/Unary.h"

// Panel with negatives, zeros, NaN and inf so every edge case of the ops is exercised.
static DataPanel make_panel(unsigned T = 8, unsigned N = 6) {
    DataPanel dp;
    dp.T = T;
    dp.N = N;
    dp.timestamps.resize(T);
    for (unsigned t = 0; t < T; ++t) dp.timestamps[t] = (int64_t)t * 86400000LL;
    dp.symbols.resize(N);
    for (unsigned n = 0; n < N; ++n) dp.symbols[n] = "SYM" + std::to_string(n);
    const float nan = std::numeric_limits<float>::quiet_NaN();
    const float inf = std::numeric_limits<float>::infinity();
    const float special[6] = {0.0f, -0.0f, nan, inf, -2.5f, 0.3f};
    unsigned k = 0;
    for (const char* col : {"open", "close", "volume", "log", "abs"}) {
        auto& arr = dp.arrays[col];
        arr.resize(T * N);
        for (unsigned t = 0; t < T; ++t)
            for (unsigned n = 0; n < N; ++n)
                arr[t * N + n] = (t == 0) ? special[(n + k) % 6]
                                          : (n % 2 ? -1.0f : 1.0f) * (0.7f + 1.3f * t + 0.11f * n + 0.05f * k);
        ++k;
    }
    return dp;
}

static std::vector<std::vector<float>> run(Module* root, const DataPanel& dp) {
    root->numAssets = dp.N;
    root->init();
    std::vector<std::vector<float>> out;
    for (unsigned t = 0; t < dp.T; ++t) {
        std::vector<float> buf(dp.N, 0.0f);
        SignalVector sv{buf.data(), dp.N};
        root->calculate(t, sv);
        out.push_back(buf);
    }
    return out;
}

static std::vector<std::vector<float>> run_expr(const std::string& expr, const DataPanel& dp) {
    std::vector<std::unique_ptr<Module>> owned;
    return run(parse_expr(expr, &dp, owned), dp);
}

// Expected: evaluate `inner` through the parser, then apply MUnary<Op> directly.
template <class Op>
static std::vector<std::vector<float>> run_direct(const std::string& inner, float param, const DataPanel& dp) {
    std::vector<std::unique_ptr<Module>> owned;
    Module* src = parse_expr(inner, &dp, owned);
    MUnary<Op> u;
    u.param = FloatParameter(param);
    src->numAssets = dp.N;
    src->init();
    u.numAssets = dp.N;
    u.init();
    std::vector<std::vector<float>> out;
    for (unsigned t = 0; t < dp.T; ++t) {
        std::vector<float> buf(dp.N, 0.0f);
        SignalVector sv{buf.data(), dp.N};
        src->calculate(t, sv);
        u.calculate(t, sv);
        out.push_back(buf);
    }
    return out;
}

static void expect_bits_equal(const std::vector<std::vector<float>>& a,
                              const std::vector<std::vector<float>>& b) {
    ASSERT_EQ(a.size(), b.size());
    for (size_t t = 0; t < a.size(); ++t) {
        ASSERT_EQ(a[t].size(), b[t].size());
        for (size_t n = 0; n < a[t].size(); ++n) {
            uint32_t x, y;
            std::memcpy(&x, &a[t][n], 4);
            std::memcpy(&y, &b[t][n], 4);
            EXPECT_EQ(x, y) << "t=" << t << " n=" << n << " got " << a[t][n] << " want " << b[t][n];
        }
    }
}

static void expect_error(const std::string& expr, const std::string& msg) {
    auto dp = make_panel();
    std::vector<std::unique_ptr<Module>> owned;
    try {
        (void)parse_expr(expr, &dp, owned);
        FAIL() << "expected error for " << expr;
    } catch (const std::runtime_error& e) {
        EXPECT_EQ(std::string(e.what()), msg) << expr;
    }
}

TEST(ExprFunctions, LeadingDotLiteralParses) {
    auto dp = make_panel();
    std::vector<std::unique_ptr<Module>> owned;
    EXPECT_NO_THROW((void)parse_expr(".5 * close", &dp, owned));
    EXPECT_NO_THROW((void)parse_expr("5. * close", &dp, owned));
}

TEST(ExprFunctions, OneArgMatchesDirectModule) {
    auto dp = make_panel();
    expect_bits_equal(run_expr("abs(close)", dp), run_direct<unary_ops::Abs>("close", 0.0, dp));
    expect_bits_equal(run_expr("log(close)", dp), run_direct<unary_ops::Log>("close", 0.0, dp));
    expect_bits_equal(run_expr("sqrt(close)", dp), run_direct<unary_ops::Sqrt>("close", 0.0, dp));
    expect_bits_equal(run_expr("sign(close)", dp), run_direct<unary_ops::Sign>("close", 0.0, dp));
}

TEST(ExprFunctions, PowerMatchesDirectModule) {
    auto dp = make_panel();
    expect_bits_equal(run_expr("power(close, 2)", dp), run_direct<unary_ops::Power>("close", 2.0, dp));
    expect_bits_equal(run_expr("power(close, 0.5)", dp), run_direct<unary_ops::Power>("close", 0.5, dp));
    expect_bits_equal(run_expr("power(close, -1)", dp), run_direct<unary_ops::Power>("close", -1.0, dp));
    expect_bits_equal(run_expr("signedpower(close, 2)", dp), run_direct<unary_ops::SignedPower>("close", 2.0, dp));
    expect_bits_equal(run_expr("signedpower(close, 0.5)", dp), run_direct<unary_ops::SignedPower>("close", 0.5, dp));
    expect_bits_equal(run_expr("signedpower(close,-1)", dp), run_direct<unary_ops::SignedPower>("close", -1.0, dp));
}

TEST(ExprFunctions, EdgeValuesAreNaN) {
    auto dp = make_panel();
    auto out = run_expr("log(close)", dp);
    // bar 0 of close: {-0, nan, inf, -2.5, 0.3, 0}
    EXPECT_TRUE(std::isnan(out[0][0]));
    EXPECT_TRUE(std::isnan(out[0][1]));
    EXPECT_TRUE(std::isnan(out[0][2]));
    EXPECT_TRUE(std::isnan(out[0][3]));
    EXPECT_FLOAT_EQ(out[0][4], (float)std::log(0.3f));
    auto inv = run_expr("power(close, -1)", dp);
    EXPECT_TRUE(std::isnan(inv[0][0]));
    EXPECT_TRUE(std::isnan(inv[0][5]));
}

TEST(ExprFunctions, NestingAndMixing) {
    auto dp = make_panel();
    expect_bits_equal(run_expr("abs(log(volume))", dp), run_direct<unary_ops::Abs>("log(volume)", 0.0, dp));
    expect_bits_equal(run_expr("-abs(close)", dp), run_expr("-1 * abs(close)", dp));
    auto neg = run_expr("-abs(close)", dp);
    for (unsigned t = 0; t < dp.T; ++t)
        for (unsigned n = 0; n < dp.N; ++n) {
            const float c = dp.arrays.at("close")[t * dp.N + n];
            if (std::isfinite(c)) EXPECT_EQ(neg[t][n], -std::fabs(c)) << "t=" << t << " n=" << n;
        }
    expect_bits_equal(run_expr("signedpower(returns, 0.5)", dp),
                      run_direct<unary_ops::SignedPower>("returns", 0.5, dp));
    expect_bits_equal(run_expr("abs(returns)", dp), run_direct<unary_ops::Abs>("returns", 0.0, dp));
    expect_bits_equal(run_expr("log (close)", dp), run_expr("log(close)", dp));

    // power(close - open, 2) / close, checked against scalar arithmetic
    auto got = run_expr("power(close - open, 2) / close", dp);
    const auto& c = dp.arrays.at("close");
    const auto& o = dp.arrays.at("open");
    for (unsigned t = 1; t < dp.T; ++t)
        for (unsigned n = 0; n < dp.N; ++n) {
            float d = c[t * dp.N + n] - o[t * dp.N + n];
            float sq = (float)std::pow((double)d, 2.0);
            EXPECT_FLOAT_EQ(got[t][n], sq / c[t * dp.N + n]);
        }
}

TEST(ExprFunctions, FunctionNamedColumnStaysField) {
    auto dp = make_panel();
    auto raw = run_expr("log", dp);
    for (unsigned t = 0; t < dp.T; ++t)
        for (unsigned n = 0; n < dp.N; ++n) {
            const float want = dp.arrays.at("log")[t * dp.N + n];
            if (std::isnan(want)) EXPECT_TRUE(std::isnan(raw[t][n]));
            else EXPECT_EQ(raw[t][n], want);
        }
    auto out = run_expr("log + abs", dp);
    const auto& l = dp.arrays.at("log");
    const auto& a = dp.arrays.at("abs");
    for (unsigned n = 0; n < dp.N; ++n)
        EXPECT_FLOAT_EQ(out[3][n], l[3 * dp.N + n] + a[3 * dp.N + n]);
    // ... while log(...) is still the function
    expect_bits_equal(run_expr("log(log)", dp), run_direct<unary_ops::Log>("log", 0.0, dp));
}

TEST(ExprFunctions, Errors) {
    expect_error("max(close, 0)", "expr_parser: unknown function 'max'");
    expect_error("min(close)", "expr_parser: unknown function 'min'");
    expect_error("inverse(close)", "expr_parser: unknown function 'inverse'");
    expect_error("reverse(close)", "expr_parser: unknown function 'reverse'");
    expect_error("close(open)", "expr_parser: unknown function 'close'");
    expect_error("log(close, 2)", "expr_parser: 'log' expects 1 argument");
    expect_error("abs(close, open)", "expr_parser: 'abs' expects 1 argument");
    expect_error("log()", "expr_parser: 'log' expects 1 argument");
    expect_error("power()", "expr_parser: 'power' expects 2 arguments");
    expect_error("power(,2)", "expr_parser: 'power' expects 2 arguments");
    expect_error("power(close)", "expr_parser: 'power' expects 2 arguments");
    expect_error("signedpower(close, 2, 3)", "expr_parser: 'signedpower' expects 2 arguments");
    expect_error("power(close, volume)", "expr_parser: 'power' exponent must be a number");
    expect_error("power(close, 2 + 1)", "expr_parser: 'power' exponent must be a number");
    expect_error("power(close, 1.2.3)", "expr_parser: invalid numeric literal '1.2.3'");
    expect_error("abs(close", "expr_parser: expected ')'");
    // The tokenizer rejects scientific notation with a dedicated message; '+' exponents stay errors.
    expect_error("close * 1e-3",
                 "expr_parser: scientific notation is not allowed ('1e-3'); write it as a plain decimal");
    expect_error("power(close, 1e-3)",
                 "expr_parser: scientific notation is not allowed ('1e-3'); write it as a plain decimal");
    expect_error("power(close, +2)", "expr_parser: 'power' exponent must be a number");
    expect_error("power(close, 2", "expr_parser: expected ')'");
}
