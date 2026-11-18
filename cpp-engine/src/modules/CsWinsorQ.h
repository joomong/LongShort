#pragma once

// MCsWinsorQ: <cswinsorq lo="0.01" hi="0.99"/> (stateless). Clip the current chain to its
// cross-sectional lo/hi quantiles (linear interpolation between order statistics) over finite
// cells in the run universe; other cells NaN. 0 <= lo < hi <= 1.
struct MCsWinsorQ : public Module {
    float lo = 0.01f, hi = 0.99f;
    const DataPanel* panel = nullptr;
    const std::vector<uint8_t>* universe = nullptr;

    void init() override {}
    void calculate(unsigned di, SignalVector& output) override
    {
        const auto* sel = universe ? universe : panel ? &panel->universe_mask : nullptr;
        const uint8_t* m = sel && !sel->empty() ? sel->data() + static_cast<size_t>(di) * numAssets : nullptr;
        std::vector<double> values;
        values.reserve(numAssets);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if ((!m || m[ii]) && std::isfinite(output[ii])) values.push_back(output[ii]);
            else output[ii] = std::numeric_limits<float>::quiet_NaN();
        }
        if (values.empty()) return;
        std::sort(values.begin(), values.end());
        const auto quantile = [&](double q) {
            const double pos = q * static_cast<double>(values.size() - 1);
            const size_t low = static_cast<size_t>(std::floor(pos));
            const size_t high = std::min(low + 1, values.size() - 1);
            return values[low] + (pos - static_cast<double>(low)) * (values[high] - values[low]);
        };
        const double a = quantile(lo), b = quantile(hi);
        for (unsigned ii = 0; ii < numAssets; ++ii)
            if (std::isfinite(output[ii]))
                output[ii] = static_cast<float>(std::min(b, std::max(a, static_cast<double>(output[ii]))));
    }
};
