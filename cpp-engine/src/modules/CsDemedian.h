#pragma once

// MCsDemedian: <csdemedian/> (stateless). The current chain minus its cross-sectional median over
// finite cells in the run universe; other cells NaN. The median of an even count is the mean of the middle two.
struct MCsDemedian : public Module {
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
        const size_t n = values.size();
        const double median = n % 2 ? values[n / 2] : 0.5 * (values[n / 2 - 1] + values[n / 2]);
        for (unsigned ii = 0; ii < numAssets; ++ii)
            if (std::isfinite(output[ii])) output[ii] = static_cast<float>(output[ii] - median);
    }
};
