#pragma once
#include "../data/data_panel.h"

// MNormalize: subtract the masked cross-sectional mean (stateless); with
// usestd divide by the sample sd (row NaN when count < 2 or sd < 1e-12);
// limit > 0 clamps the result to [-limit, +limit].
struct MNormalize : public Module {
    void init() {}
    void calculate(unsigned di, SignalVector &output)
    {
        const auto* selected = universe ? universe : panel ? &panel->universe_mask : nullptr;
        const auto* mask = selected && !selected->empty()
            ? selected->data() + static_cast<size_t>(di) * numAssets : nullptr;
        for (unsigned ii = 0; ii < numAssets; ++ii)
            if ((mask && !mask[ii]) || !std::isfinite(output[ii]))
                output[ii] = std::numeric_limits<float>::quiet_NaN();

        double sum = 0.0;
        int count = 0;
        for (unsigned ii = 0; ii < numAssets; ++ii)
            if (std::isfinite(output[ii])) { sum += output[ii]; ++count; }
        if (count == 0) return;
        const double mean = sum / count;
        double sd = 1.0;
        if (usestd) {
            if (count < 2) { output.fill_nan(); return; }
            double sq_sum = 0.0;
            for (unsigned ii = 0; ii < numAssets; ++ii)
                if (std::isfinite(output[ii])) {
                    const double diff = output[ii] - mean;
                    sq_sum += diff * diff;
                }
            sd = std::sqrt(sq_sum / (count - 1));
            if (sd < 1e-12) { output.fill_nan(); return; }
        }
        const double lim = limit;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (!std::isfinite(output[ii])) continue;
            double v = output[ii] - mean;
            if (usestd) v /= sd;
            if (lim > 0.0) v = std::min(std::max(v, -lim), lim);
            output[ii] = static_cast<float>(v);
        }
    }

    const DataPanel* panel = nullptr;  // optional; null preserves standalone module behavior
    const std::vector<uint8_t>* universe = nullptr; // run-selected mask (universe.names); null = panel default
    IntParameter usestd{0};
    FloatParameter limit{0.0f};
};
