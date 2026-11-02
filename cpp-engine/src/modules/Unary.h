#pragma once

// Element-wise arithmetic (stateless, in place). One Op per tag; the chain tag (<log/>) and the expr
// function (log(x)) share the same Op, so both paths give identical bits.
// Rule: a non-finite input or a non-finite result is NaN.
namespace unary_ops {
struct Abs         { static double apply(double x, double)   { return std::fabs(x); } };
struct Log         { static double apply(double x, double)   { return std::log(x); } };
struct Exp         { static double apply(double x, double)   { return std::exp(x); } };
struct Sqrt        { static double apply(double x, double)   { return std::sqrt(x); } };
struct Sign        { static double apply(double x, double)   { return (x > 0.0) - (x < 0.0); } };
struct Power       { static double apply(double x, double p) { return std::pow(x, p); } };
struct SignedPower { static double apply(double x, double p) { return Sign::apply(x, p) * std::pow(std::fabs(x), p); } };
struct Inverse     { static double apply(double x, double)   { return 1.0 / x; } };
struct Reverse     { static double apply(double x, double)   { return -x; } };
struct Max         { static double apply(double x, double v) { return x > v ? x : v; } };
struct Min         { static double apply(double x, double v) { return x < v ? x : v; } };
}  // namespace unary_ops

template <class Op>
struct MUnary : public Module {
    void init() {}
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        const double p = param;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            const float x = output[ii];
            const float y = std::isfinite(x) ? (float)Op::apply(x, p) : x;
            output[ii] = std::isfinite(y) ? y : std::numeric_limits<float>::quiet_NaN();
        }
    }

    FloatParameter param;  // power/signedpower: exp; max/min: value; unused by the other ops
};
