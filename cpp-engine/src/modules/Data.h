#pragma once

struct MData : public Module {
    std::string column_name;
    const DataPanel* panel = nullptr;

    void init() override {
        setting_output_to_nan_is_not_necessary();
    }

    void calculate(unsigned di, SignalVector& output) override {
        if (di >= panel->T) {
            output.fill_nan();
            return;
        }
        const float* src = panel->row(column_name, di);
        for (unsigned ii = 0; ii < numAssets; ++ii)
            output[ii] = src[ii];
    }
};
