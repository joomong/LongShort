#pragma once

struct MData : public Module {
    void init()
    {
        setting_output_to_nan_is_not_necessary();
    }
    void calculate(unsigned di, SignalVector &output)
    {
#ifndef NDEBUG
        if (di >= panel->T) {
            output.fill_nan();
            return;
        }
#endif
        const float* src = panel->row(column_name, di);
        for (unsigned ii = 0; ii < numAssets; ++ii)
            output[ii] = src[ii];
    }

    std::string column_name;
    const DataPanel* panel = nullptr;
};
