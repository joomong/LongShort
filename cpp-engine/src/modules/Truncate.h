#pragma once
#include <algorithm>
#include <vector>
#include "../data/data_panel.h"

// MTruncate: cap each name's weight at `max` of its set (stateless, cross-sectional).
// Universe mask first (excluded/non-finite -> NaN). side=0: one set of |x|; side=1: longs and shorts
// separately, each relative to its own leg sum. Output sign(x) * min(|x|, c); zeros stay 0.
// post=1: c is the water-filling level so that min(|x|,c) / sum min(|x|,c) <= max after renormalization;
//   if infeasible (max * n_nonzero < 1) c = the smallest nonzero magnitude (all nonzero members equal).
// post=0: c = max * sum|x| of the set (desk cs_truncate post_trunc=False).
struct MTruncate : public Module {
    void init() { mags.reserve(numAssets); }

    void calculate(unsigned di, SignalVector &output)
    {
        const auto* selected = universe ? universe : panel ? &panel->universe_mask : nullptr;
        const auto* mask = selected && !selected->empty()
            ? selected->data() + static_cast<size_t>(di) * numAssets : nullptr;
        for (unsigned ii = 0; ii < numAssets; ++ii)
            if ((mask && !mask[ii]) || !std::isfinite(output[ii]))
                output[ii] = std::numeric_limits<float>::quiet_NaN();

        if (static_cast<int>(side) == 0) {
            apply(output, 0);
        } else {
            apply(output, 1);
            apply(output, -1);
        }
    }

    // sgn: 0 = all nonzero members, 1 = x > 0, -1 = x < 0.
    void apply(SignalVector &output, int sgn)
    {
        mags.clear();
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            const float x = output[ii];
            if (!std::isfinite(x) || x == 0.0f) continue;
            if (sgn == 0 || (sgn > 0) == (x > 0.0f)) mags.push_back(std::fabs(static_cast<double>(x)));
        }
        if (mags.empty()) return;
        const double cap = static_cast<double>(maxw);
        double c;
        if (static_cast<int>(post) == 0) {
            double sum = 0.0;
            for (double v : mags) sum += v;
            c = cap * sum;
        } else {
            std::sort(mags.begin(), mags.end());
            const size_t n = mags.size();
            if (cap * static_cast<double>(n) < 1.0) {
                c = mags[0];
            } else {
                c = mags[n - 1];  // no clipping needed unless a level is found below
                double cumsum = 0.0;
                for (size_t i = 0; i < n; ++i) {
                    const double k = static_cast<double>(n - i);  // members clipped when c = mags[i]
                    if (mags[i] > cap * (cumsum + k * mags[i])) {
                        c = cap * cumsum / (1.0 - cap * k);
                        break;
                    }
                    cumsum += mags[i];
                }
            }
        }
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            const float x = output[ii];
            if (!std::isfinite(x) || x == 0.0f) continue;
            if (sgn != 0 && (sgn > 0) != (x > 0.0f)) continue;
            const double a = std::fabs(static_cast<double>(x));
            if (a > c) output[ii] = static_cast<float>(x > 0.0f ? c : -c);
        }
    }

    const DataPanel* panel = nullptr;  // optional; null preserves standalone module behavior
    const std::vector<uint8_t>* universe = nullptr; // run-selected mask (universe.names); null = panel default
    FloatParameter maxw{0.05f};        // XML attribute `max`
    IntParameter side{0};
    IntParameter post{1};

  private:
    std::vector<double> mags;
};
