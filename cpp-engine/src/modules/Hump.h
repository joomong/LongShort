#pragma once
#include "../data/data_panel.h"

// MHump: BRAIN hump turnover limiter (stateful per instrument). limit =
// hump * sum|x| over today's finite masked inputs. Changes with |x - prev| <=
// limit are ignored (out = prev); larger ones move prev by exactly limit.
// Missing or excluded instruments output NaN and reset prev, so a re-entering
// instrument starts from its own value.
struct MHump : public Module {
    void init()
    {
        prev.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
    }
    void calculate(unsigned di, SignalVector &output)
    {
        const auto* selected = universe ? universe : panel ? &panel->universe_mask : nullptr;
        const auto* mask = selected && !selected->empty()
            ? selected->data() + static_cast<size_t>(di) * numAssets : nullptr;
        double abs_sum = 0.0;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if ((mask && !mask[ii]) || !std::isfinite(output[ii]))
                output[ii] = std::numeric_limits<float>::quiet_NaN();
            else
                abs_sum += std::fabs(static_cast<double>(output[ii]));
        }
        const double limit = static_cast<double>(hump) * abs_sum;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            const float x = output[ii];
            if (!std::isfinite(x)) {
                prev[ii] = std::numeric_limits<float>::quiet_NaN();
                continue;
            }
            if (std::isfinite(prev[ii])) {
                const double d = static_cast<double>(x) - prev[ii];
                if (std::fabs(d) > limit)
                    output[ii] = static_cast<float>(prev[ii] + (d > 0.0 ? limit : -limit));
                else
                    output[ii] = prev[ii];
            }
            prev[ii] = output[ii];
        }
    }

    StarVector<float> prev;
    const DataPanel* panel = nullptr;  // optional; null preserves standalone module behavior
    const std::vector<uint8_t>* universe = nullptr; // run-selected mask (universe.names); null = panel default
    FloatParameter hump{0.01f};
};
