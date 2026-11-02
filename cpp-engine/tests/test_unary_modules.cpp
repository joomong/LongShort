#include <gtest/gtest.h>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <functional>
#include <limits>
#include <vector>
#include "module.h"
#include "signal_vector.h"
#include "modules/Unary.h"
#include "modules/Affine.h"

// Expectations are written here from the semantics table (UNARY_COMMON / math_contract), never by calling
// Op::apply, so a wrong Op cannot agree with itself.

namespace {

const float kNaN = std::numeric_limits<float>::quiet_NaN();
const float kInf = std::numeric_limits<float>::infinity();

const std::vector<float> kInputs = {2.5f, -2.5f, 0.0f, -0.0f, kNaN, kInf, -kInf, 3e38f, 1e-38f, 0.25f, -4.0f};

std::uint32_t bits(float v) {
    std::uint32_t b;
    std::memcpy(&b, &v, sizeof b);
    return b;
}

template <class Op>
std::vector<float> run(const std::vector<float>& in, double param = 0.0) {
    MUnary<Op> m;
    m.numAssets = static_cast<unsigned>(in.size());
    m.param = FloatParameter(param);
    m.init();
    std::vector<float> buf = in;
    SignalVector sv{buf.data(), m.numAssets};
    m.calculate(0, sv);
    return buf;
}

// Applies the "non-finite in or out -> NaN" rule around a finite-input expectation.
float wrap(float x, const std::function<double(double)>& f) {
    if (!std::isfinite(x)) return kNaN;
    const float y = static_cast<float>(f(static_cast<double>(x)));
    return std::isfinite(y) ? y : kNaN;
}

void expect_bits(const std::vector<float>& got, const std::vector<float>& in,
                 const std::function<float(float)>& expected, const char* what) {
    ASSERT_EQ(got.size(), in.size());
    for (size_t i = 0; i < in.size(); ++i) {
        const float e = expected(in[i]);
        if (std::isnan(e))
            EXPECT_TRUE(std::isnan(got[i])) << what << " x=" << in[i] << " got " << got[i];
        else
            EXPECT_EQ(bits(got[i]), bits(e)) << what << " x=" << in[i] << " got " << got[i] << " want " << e;
    }
}

bool is_integer(double p) { return std::floor(p) == p; }

}  // namespace

TEST(UnaryModules, Abs) {
    expect_bits(run<unary_ops::Abs>(kInputs), kInputs,
                [](float x) { return wrap(x, [](double v) { return v < 0 ? -v : (v == 0 ? 0.0 : v); }); }, "abs");
}

TEST(UnaryModules, Log) {
    expect_bits(run<unary_ops::Log>(kInputs), kInputs, [](float x) {
        if (!(x > 0)) return kNaN;
        return wrap(x, [](double v) { return std::log(v); });
    }, "log");
}

TEST(UnaryModules, Exp) {
    expect_bits(run<unary_ops::Exp>(kInputs), kInputs, [](float x) {
        return wrap(x, [](double v) { return std::exp(v); });
    }, "exp");
    // e^0 = 1 exactly; float overflow (e^89 > FLT_MAX) and -inf input are NaN; e^-200 underflows to 0.
    const auto out = run<unary_ops::Exp>({0.0f, -0.0f, 1.0f, 88.0f, 89.0f, -kInf, kInf, -200.0f});
    EXPECT_EQ(bits(out[0]), bits(1.0f));
    EXPECT_EQ(bits(out[1]), bits(1.0f));
    EXPECT_EQ(out[2], static_cast<float>(2.718281828459045));
    EXPECT_TRUE(std::isfinite(out[3]));
    EXPECT_TRUE(std::isnan(out[4]));
    EXPECT_TRUE(std::isnan(out[5]));
    EXPECT_TRUE(std::isnan(out[6]));
    EXPECT_EQ(bits(out[7]), bits(0.0f));
}

TEST(UnaryModules, Sqrt) {
    expect_bits(run<unary_ops::Sqrt>(kInputs), kInputs, [](float x) {
        if (x < 0) return kNaN;
        return wrap(x, [](double v) { return std::sqrt(v); });
    }, "sqrt");
    // sqrt(-0) = -0 is allowed (not NaN).
    EXPECT_FALSE(std::isnan(run<unary_ops::Sqrt>({-0.0f})[0]));
}

TEST(UnaryModules, Sign) {
    expect_bits(run<unary_ops::Sign>(kInputs), kInputs, [](float x) {
        if (!std::isfinite(x)) return kNaN;
        return x > 0 ? 1.0f : (x < 0 ? -1.0f : 0.0f);
    }, "sign");
    EXPECT_EQ(bits(run<unary_ops::Sign>({-0.0f})[0]), bits(0.0f));
}

TEST(UnaryModules, Inverse) {
    expect_bits(run<unary_ops::Inverse>(kInputs), kInputs, [](float x) {
        if (x == 0) return kNaN;
        return wrap(x, [](double v) { return 1.0 / v; });
    }, "inverse");
}

TEST(UnaryModules, Reverse) {
    expect_bits(run<unary_ops::Reverse>(kInputs), kInputs, [](float x) {
        if (!std::isfinite(x)) return kNaN;
        return -x;
    }, "reverse");
}

TEST(UnaryModules, Power) {
    for (double p : {0.5, 2.0, -1.0, 0.0, 1.0, 3.0, -0.5}) {
        expect_bits(run<unary_ops::Power>(kInputs, p), kInputs, [p](float x) {
            if (x < 0 && !is_integer(p)) return kNaN;
            if (x == 0 && p < 0) return kNaN;
            return wrap(x, [p](double v) { return std::pow(v, p); });
        }, "power");
    }
    EXPECT_TRUE(std::isnan(run<unary_ops::Power>({-2.5f}, 0.5)[0]));
    EXPECT_TRUE(std::isnan(run<unary_ops::Power>({0.0f}, -1.0)[0]));
    EXPECT_TRUE(std::isnan(run<unary_ops::Power>({-0.0f}, -1.0)[0]));
    EXPECT_TRUE(std::isnan(run<unary_ops::Power>({3e38f}, 2.0)[0]));  // overflows float
    EXPECT_EQ(run<unary_ops::Power>({0.0f}, 0.0)[0], 1.0f);
    EXPECT_EQ(run<unary_ops::Power>({-2.0f}, 3.0)[0], -8.0f);
}

TEST(UnaryModules, SignedPower) {
    for (double p : {0.5, 2.0, -1.0, 0.0, 1.0, 3.0, -0.5}) {
        expect_bits(run<unary_ops::SignedPower>(kInputs, p), kInputs, [p](float x) {
            if (x == 0) return p < 0 ? kNaN : 0.0f;
            const double s = x > 0 ? 1.0 : -1.0;
            return wrap(x, [p, s](double v) { return s * std::pow(std::fabs(v), p); });
        }, "signedpower");
    }
    EXPECT_TRUE(std::isnan(run<unary_ops::SignedPower>({0.0f}, -1.0)[0]));
    EXPECT_TRUE(std::isnan(run<unary_ops::SignedPower>({-0.0f}, -1.0)[0]));
    EXPECT_EQ(bits(run<unary_ops::SignedPower>({0.0f}, 0.0)[0]), bits(0.0f));
    EXPECT_EQ(run<unary_ops::SignedPower>({-4.0f}, 0.5)[0], -2.0f);
}

TEST(UnaryModules, MaxMin) {
    for (double v : {0.0, 3.0, -1.0}) {
        const float fv = static_cast<float>(v);
        expect_bits(run<unary_ops::Max>(kInputs, v), kInputs, [fv](float x) {
            if (!std::isfinite(x)) return kNaN;
            return x > fv ? x : fv;
        }, "max");
        expect_bits(run<unary_ops::Min>(kInputs, v), kInputs, [fv](float x) {
            if (!std::isfinite(x)) return kNaN;
            return x < fv ? x : fv;
        }, "min");
    }
    // NaN is never replaced by the bound.
    EXPECT_TRUE(std::isnan(run<unary_ops::Max>({kNaN}, 0.0)[0]));
    EXPECT_TRUE(std::isnan(run<unary_ops::Min>({kNaN}, 3.0)[0]));
}

TEST(UnaryModules, IdentitiesBitExactOnFinite) {
    std::vector<float> finite;
    for (float x : kInputs)
        if (std::isfinite(x)) finite.push_back(x);
    const auto twice = run<unary_ops::Reverse>(run<unary_ops::Reverse>(finite));
    const auto pow1 = run<unary_ops::Power>(finite, 1.0);
    const auto spow1 = run<unary_ops::SignedPower>(finite, 1.0);
    for (size_t i = 0; i < finite.size(); ++i) {
        EXPECT_EQ(bits(twice[i]), bits(finite[i])) << "reverse twice x=" << finite[i];
        EXPECT_EQ(bits(pow1[i]), bits(finite[i])) << "power 1 x=" << finite[i];
        if (finite[i] != 0)  // signedpower(±0, 1) = sign(0)*0 = +0 by the sign rule
            EXPECT_EQ(bits(spow1[i]), bits(finite[i])) << "signedpower 1 x=" << finite[i];
        else
            EXPECT_EQ(bits(spow1[i]), bits(0.0f));
    }
}

// ---------------------------------------------------------------- affine (built in 2026-10-29)
TEST(AffineModule, FactorOffsetInDoubleAndNonFiniteToNaN) {
    MAffine m;
    m.numAssets = static_cast<unsigned>(kInputs.size());
    m.factor = FloatParameter(-2.0);
    m.offset = FloatParameter(0.5);
    m.init();
    std::vector<float> buf = kInputs;
    SignalVector sv{buf.data(), m.numAssets};
    m.calculate(0, sv);
    for (size_t i = 0; i < kInputs.size(); ++i) {
        const float x = kInputs[i];
        if (!std::isfinite(x)) { EXPECT_TRUE(std::isnan(buf[i])) << i; continue; }
        const float want = static_cast<float>(-2.0 * static_cast<double>(x) + 0.5);
        if (!std::isfinite(want)) EXPECT_TRUE(std::isnan(buf[i])) << i;   // 3e38 * -2 overflows float
        else EXPECT_EQ(bits(buf[i]), bits(want)) << i;
    }
}

TEST(AffineModule, DefaultIsIdentityOnFiniteInput) {
    MAffine m;
    m.numAssets = static_cast<unsigned>(kInputs.size());
    m.init();
    std::vector<float> buf = kInputs;
    SignalVector sv{buf.data(), m.numAssets};
    m.calculate(0, sv);
    for (size_t i = 0; i < kInputs.size(); ++i) {
        // Value equality: 1 * (-0.0) + 0.0 is +0.0 under IEEE rules, which compares equal to -0.0.
        if (std::isfinite(kInputs[i])) EXPECT_EQ(buf[i], kInputs[i]) << i;
        else EXPECT_TRUE(std::isnan(buf[i])) << i;
    }
}
