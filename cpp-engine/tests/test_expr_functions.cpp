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
    expect_bits_equal(run_expr("exp(close)", dp), run_direct<unary_ops::Exp>("close", 0.0, dp));
    expect_bits_equal(run_expr("exp(close * 0.1)", dp), run_direct<unary_ops::Exp>("close * 0.1", 0.0, dp));
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
    expect_error("exp(close, 2)", "expr_parser: 'exp' expects 1 argument");
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

// Wide dataset fields are `<dataset>.<field>` tokens (one dot, no whitespace, field part may
// start with a digit); they are always data fields, never functions.
static DataPanel make_dotted_panel() {
    auto dp = make_panel();
    unsigned k = 0;
    for (const char* col : {"AFL.a", "AFL.b", "AFL.c", "AFL.10DMACD", "AFL.x", "B.x", "AFL.only"}) {
        auto& arr = dp.arrays[col];
        arr.resize(dp.T * dp.N);
        for (unsigned i = 0; i < dp.T * dp.N; ++i) arr[i] = 1.0f + 0.25f * i + 3.0f * k;
        ++k;
    }
    return dp;
}
static void expect_dotted_error(const std::string& expr, const std::string& msg) {
    auto dp = make_dotted_panel();
    std::vector<std::unique_ptr<Module>> owned;
    try {
        (void)parse_expr(expr, &dp, owned);
        FAIL() << "expected error for " << expr;
    } catch (const std::runtime_error& e) {
        EXPECT_EQ(std::string(e.what()), msg) << expr;
    }
}
TEST(ExprFunctions, DottedNamesAreDataFields) {
    auto dp = make_dotted_panel();
    const auto& a = dp.arrays.at("AFL.a");
    const auto& b = dp.arrays.at("AFL.b");
    const auto& c = dp.arrays.at("AFL.c");
    const auto& m = dp.arrays.at("AFL.10DMACD");
    auto mix = run_expr("AFL.a - 2*AFL.b + exp(AFL.c/100)", dp);
    auto split = run_expr("AFL.10DMACD/2.5", dp);
    auto spaced = run_expr("AFL.a-AFL.b", dp);
    for (unsigned t = 0; t < dp.T; ++t)
        for (unsigned n = 0; n < dp.N; ++n) {
            const unsigned i = t * dp.N + n;
            EXPECT_FLOAT_EQ(mix[t][n], a[i] - 2 * b[i] + std::exp(c[i] / 100));
            EXPECT_FLOAT_EQ(split[t][n], m[i] / 2.5f);
            EXPECT_FLOAT_EQ(spaced[t][n], a[i] - b[i]);
        }
    expect_bits_equal(run_expr("abs(AFL.x)", dp), run_direct<unary_ops::Abs>("AFL.x", 0.0, dp));
    expect_bits_equal(run_expr("B.x", dp), run_expr("B.x * 1", dp));
}
TEST(ExprFunctions, DottedNameErrorsAndHint) {
    expect_dotted_error("AFL.", "expr_parser: expected a field name after 'AFL.'");
    expect_dotted_error("AFL. + close", "expr_parser: expected a field name after 'AFL.'");
    expect_dotted_error("AFL..X", "expr_parser: expected a field name after 'AFL.'");
    expect_dotted_error("AFL.X.Y", "expr_parser: a data name has exactly one dot ('AFL.X.')");
    expect_dotted_error(".X", "expr_parser: invalid numeric literal '.'");
    expect_dotted_error("AFL . a", "expr_parser: unknown or disabled data field 'AFL'");
    expect_dotted_error("AFL.377", "expr_parser: unknown or disabled data field 'AFL.377'");
    expect_dotted_error("AFL.exp(close)", "expr_parser: 'AFL.exp' is a data field, not a function");
    expect_dotted_error("AFL.a(close)", "expr_parser: 'AFL.a' is a data field, not a function");
    expect_dotted_error("AFL.returns", "expr_parser: unknown or disabled data field 'AFL.returns'");
    // A bare name carried by exactly one wide dataset gets the dotted spelling as a hint ...
    expect_dotted_error("only", "expr_parser: unknown or disabled data field 'only' (did you mean 'AFL.only'?)");
    expect_dotted_error("10DMACD + 1", "expr_parser: unexpected tokens after expression");
    expect_dotted_error("1 + abs(a)", "expr_parser: unknown or disabled data field 'a' (did you mean 'AFL.a'?)");
    // ... but not when two datasets carry it, nor for names nobody carries.
    expect_dotted_error("x", "expr_parser: unknown or disabled data field 'x'");
    expect_dotted_error("nothing", "expr_parser: unknown or disabled data field 'nothing'");
}
// `AFL.returns` is an ordinary data field, never the close-derived returns.
TEST(ExprFunctions, DottedReturnsIsADataField) {
    auto dp = make_dotted_panel();
    auto& r = dp.arrays["AFL.returns"];
    r.assign(dp.T * dp.N, 0.0f);
    for (unsigned i = 0; i < r.size(); ++i) r[i] = 7.0f + 0.5f * i;
    auto out = run_expr("AFL.returns", dp);
    for (unsigned t = 0; t < dp.T; ++t)
        for (unsigned n = 0; n < dp.N; ++n) EXPECT_EQ(out[t][n], r[t * dp.N + n]);
    std::vector<std::unique_ptr<Module>> owned;
    (void)parse_expr("AFL.returns", &dp, owned);
    ASSERT_EQ(owned.size(), 1u);  // one MData, not the close / delay(close) - 1 tree
    EXPECT_EQ(static_cast<MData*>(owned[0].get())->column_name, "AFL.returns");
}
