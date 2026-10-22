#pragma once

// MConstant: scalar broadcast (stateless).
struct MConstant : public Module {
    FloatParameter value;

    void init() override {
        setting_output_to_nan_is_not_necessary();
    }

    void calculate(unsigned di, SignalVector& output) override {
        float v = value;
        for (unsigned ii = 0; ii < numAssets; ++ii)
            output[ii] = v;
    }
};
