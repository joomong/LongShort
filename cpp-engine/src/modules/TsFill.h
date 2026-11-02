#pragma once

// MTsFill: sample-and-hold. Every `days` bars since init (bar % days == 0) take x as is (NaN included),
// otherwise output the held value and ignore x. O(1) per instrument.
struct MTsFill : public Module {
    void init()
    {
        held.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
        bar_count = 0;
    }
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        const bool refresh = bar_count++ % (unsigned long long)(int)days == 0;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (refresh) held[ii] = output[ii];
            output[ii] = held[ii];
        }
    }

    std::vector<float> held;
    unsigned long long bar_count = 0;
    IntParameter days{20};
};
