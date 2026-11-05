#pragma once

struct MDelay : public Module {
    unsigned lookback(unsigned) const override { return (int)days > 0 ? (int)days : 0; }
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
