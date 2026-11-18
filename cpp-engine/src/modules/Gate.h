#pragma once
#include "SignalInputs.h"

// MGate: <gate signal_1="x" signal_2="c" gt= ge= lt= le=/> (stateless).
// Output x where c is finite and passes every given bound, else NaN. No bound: c finite.
struct MGate : public Module, public SignalInputs {
    bool has_gt = false, has_ge = false, has_lt = false, has_le = false;
    float gt = 0, ge = 0, lt = 0, le = 0;

    unsigned min_signal_inputs() const override { return 2; }
    unsigned max_signal_inputs() const override { return 2; }
    void init() override {}
    void calculate(unsigned, SignalVector& output) override
    {
        const float* x = signal_row(0);
        const float* c = signal_row(1);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            const float v = c[ii];
            const bool pass = std::isfinite(v) && (!has_gt || v > gt) && (!has_ge || v >= ge) &&
                              (!has_lt || v < lt) && (!has_le || v <= le);
            output[ii] = pass ? x[ii] : std::numeric_limits<float>::quiet_NaN();
        }
    }
};
