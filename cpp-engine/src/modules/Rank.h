#pragma once
#include "../data/data_panel.h"

// MRank: cross-sectional percentile rank (stateless).
struct MRank : public Module {
    void init()
    {
        finite_values.reserve(numAssets);
    }
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
        finite_values.clear();
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            const float v = output[ii];
            if (std::isfinite(v)) {
                finite_values.emplace_back(v, ii);
            } else {
                output[ii] = std::numeric_limits<float>::quiet_NaN();
            }
        }

        const size_t n_finite = finite_values.size();
        if (n_finite == 0) {
            output.fill_nan();
            return;
        }

        std::sort(
            finite_values.begin(), finite_values.end(),
            [](const auto& lhs, const auto& rhs) {
                if (lhs.first != rhs.first)
                    return lhs.first < rhs.first;
                return lhs.second < rhs.second;
            });

        size_t begin = 0;
        while (begin < n_finite) {
            size_t end = begin + 1;
            const float value = finite_values[begin].first;
            while (end < n_finite && finite_values[end].first == value)
                ++end;

            const double rank =
                static_cast<double>(begin) + 1.0 +
                static_cast<double>(end - begin - 1) / 2.0;
            const float out = static_cast<float>(
                pct ? rank / static_cast<double>(n_finite) : rank);

            for (size_t i = begin; i < end; ++i) {
                output[finite_values[i].second] = out;
            }
            begin = end;
        }
    }

    std::vector<std::pair<float, unsigned>> finite_values;
    const DataPanel* panel = nullptr;  // optional; null preserves standalone module behavior
    const std::vector<uint8_t>* universe = nullptr; // run-selected mask (universe.names); null = panel default
    IntParameter pct{1};
};
