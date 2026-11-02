#pragma once

struct MDelay : public Module {
    void init()
    {
        input.init(days, numAssets);
    }
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        input.push(output);
        const float* delayed = input(days).data();
        for (unsigned ii = 0; ii < numAssets; ++ii)
            output[ii] = delayed[ii];
    }

    ModuleInput input;
    IntParameter days;
};
