#pragma once

// Copy this file to <config directory>/modules/Affine.h. No registry edits.
namespace research_examples {
struct MAffine : public Module {
    FloatParameter factor{1.0};
    FloatParameter offset{0.0};

    void init() {}
    void calculate(unsigned, SignalVector& output) {
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (std::isfinite(output[ii]))
                output[ii] = static_cast<float>(output[ii] * static_cast<double>(factor)
                                                + static_cast<double>(offset));
        }
    }
};
}
