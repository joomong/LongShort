#pragma once
#include "SignalInputs.h"

// MSignalBinary: element-wise arithmetic on saved signals (stateless).
//   <add|sub|mul|div|min|max signal_1="a" signal_2="b"/>   exactly two inputs; NaN if either is not finite.
//                                                            sub = a - b, div = a / b (±inf becomes NaN).
//   <mean signal_1=".." signal_2=".." .../>                 two or more; NaN unless every input is finite.
//   <nanmean signal_1=".." ... min_count="1"/>              mean of the finite inputs; NaN below min_count.
// min/max with signal_N are binary; without them the tags are the unary <min value>/<max value>.
// Arithmetic runs in double and is rounded to float once.
struct MSignalBinary : public Module, public SignalInputs {
    enum class Op { Add, Sub, Mul, Div, Min, Max, Mean, NanMean };
    Op op = Op::Add;
    int min_count = 1;

    unsigned min_signal_inputs() const override { return 2; }
    unsigned max_signal_inputs() const override { return op == Op::Mean || op == Op::NanMean ? 0 : 2; }
    void init() override {}
    void calculate(unsigned, SignalVector& output) override
    {
        const float nan = std::numeric_limits<float>::quiet_NaN();
        const size_t k = signal_count();
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (op == Op::Mean || op == Op::NanMean) {
                double sum = 0.0;
                int finite = 0;
                for (size_t j = 0; j < k; ++j) {
                    const double x = signal_row(j)[ii];
                    if (std::isfinite(x)) { sum += x; ++finite; }
                }
                if (op == Op::Mean) output[ii] = finite == static_cast<int>(k) ? static_cast<float>(sum / finite) : nan;
                else output[ii] = finite >= min_count && finite > 0 ? static_cast<float>(sum / finite) : nan;
                continue;
            }
            const double a = signal_row(0)[ii], b = signal_row(1)[ii];
            if (!std::isfinite(a) || !std::isfinite(b)) { output[ii] = nan; continue; }
            double r = 0.0;
            switch (op) {
                case Op::Add: r = a + b; break;
                case Op::Sub: r = a - b; break;
                case Op::Mul: r = a * b; break;
                case Op::Div: r = a / b; break;
                case Op::Min: r = std::min(a, b); break;
                case Op::Max: r = std::max(a, b); break;
                default: break;
            }
            output[ii] = static_cast<float>(r);
        }
    }
};
