#pragma once
#include "../data/data_panel.h"

// MZscore: cross-sectional z-score (stateless).
struct MZscore : public Module {
    void init() {}
    void calculate(unsigned di, SignalVector &output)
    {
        const auto* selected = universe ? universe : panel ? &panel->universe_mask : nullptr;
        const auto* mask = selected && !selected->empty()
            ? selected->data() + static_cast<size_t>(di) * numAssets : nullptr;
        // Excluded symbols must not influence ranks or cross-sectional moments.
        if (mask) {
            for (unsigned ii = 0; ii < numAssets; ++ii)
                if (!mask[ii]) output[ii] = std::numeric_limits<float>::quiet_NaN();
        }
        double sum   = 0.0;
        int    count = 0;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (std::isfinite(output[ii])) {
                sum += output[ii];
                ++count;
            }
        }

        if (count < min_count) {
            output.fill_nan();
            return;
        }

        double mean = sum / count;
        double sq_sum = 0.0;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (std::isfinite(output[ii])) {
                double diff = output[ii] - mean;
                sq_sum += diff * diff;
            }
        }

        double denom = (double)(count - ddof);
        double scale = (denom > 0.0) ? std::sqrt(sq_sum / denom) : 0.0;

        if (scale < eps) {
            output.fill_nan();
            return;
        }

        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (std::isfinite(output[ii]))
                output[ii] = (float)((output[ii] - mean) / (scale + eps));
            else
                output[ii] = std::numeric_limits<float>::quiet_NaN();
        }
    }

    const DataPanel* panel = nullptr;  // optional; null preserves standalone module behavior
    const std::vector<uint8_t>* universe = nullptr; // run-selected mask (universe.names); null = panel default
    IntParameter ddof{1};
    IntParameter min_count{2};
    FloatParameter eps{1e-12f};
};
