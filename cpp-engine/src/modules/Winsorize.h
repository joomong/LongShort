#pragma once
#include "../data/data_panel.h"

// MWinsorize: clip each value to mean +/- std * sample sd of the masked
// cross-section (stateless). Fewer than 2 finite values pass unchanged.
struct MWinsorize : public Module {
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
        if (count < 2) return;
        const double mean = sum / count;
        double sq_sum = 0.0;
        for (unsigned ii = 0; ii < numAssets; ++ii)
            if (std::isfinite(output[ii])) {
                const double diff = output[ii] - mean;
                sq_sum += diff * diff;
            }
        const double sd = std::sqrt(sq_sum / (count - 1));
        const double lo = mean - static_cast<double>(std_mult) * sd;
        const double hi = mean + static_cast<double>(std_mult) * sd;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            const double v = output[ii];
            if (!std::isfinite(v)) continue;
            if (v < lo) output[ii] = static_cast<float>(lo);
            else if (v > hi) output[ii] = static_cast<float>(hi);
        }
    }

    const DataPanel* panel = nullptr;  // optional; null preserves standalone module behavior
    const std::vector<uint8_t>* universe = nullptr; // run-selected mask (universe.names); null = panel default
    FloatParameter std_mult{4.0f};     // XML attribute `std`
};
