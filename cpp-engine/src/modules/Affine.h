#pragma once

// MAffine: element-wise affine map, output = factor * x + offset (stateless, no universe mask).
// <affine factor="1" offset="0"/>
// Computed in double and stored as float. A non-finite input or result becomes NaN, the same rule as
// the unary chain tags (abs, log, power, ...). Built in since 2026-10-29; before that it was the
// examples/modules sample that users copied into their workspace.
struct MAffine : public Module {
    void init() {}

    void calculate(unsigned /*di*/, SignalVector& output)
    {
        const double a = static_cast<double>(factor);
        const double b = static_cast<double>(offset);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            const float x = output[ii];
            if (!std::isfinite(x)) {
                output[ii] = std::numeric_limits<float>::quiet_NaN();
                continue;
            }
            const float y = static_cast<float>(a * static_cast<double>(x) + b);
            output[ii] = std::isfinite(y) ? y : std::numeric_limits<float>::quiet_NaN();
        }
    }

    FloatParameter factor{1.0f};
    FloatParameter offset{0.0f};
};
