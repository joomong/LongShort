#pragma once

// MDecay: linear decay weighted average (brute-force O(days) per instrument).
// O(1) incremental upgrade planned for Phase C.
struct MDecay : public Module {
    ModuleInput input;
    IntParameter days;

    void init() override {
        input.init(static_cast<unsigned>(days.value), numAssets);
        setting_output_to_nan_is_not_necessary();
    }

    void calculate(unsigned di, SignalVector& output) override {
        input.push(output);

        int d = days.value;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            double wsum   = 0.0;
            double wtotal = 0.0;
            for (int back = 0; back < d; ++back) {
                float v = input(static_cast<unsigned>(back))[ii];
                if (std::isfinite(v)) {
                    double w = static_cast<double>(d - back);
                    wsum   += v * w;
                    wtotal += w;
                }
            }
            output[ii] = (wtotal > 0.0)
                ? static_cast<float>(wsum / wtotal)
                : std::numeric_limits<float>::quiet_NaN();
        }
    }

    void save_state(std::ostream& os) const override {
        // TODO: checkpoint state for O(1) incremental
    }

    void load_state(std::istream& is) override {
        // TODO: checkpoint state for O(1) incremental
    }
};
