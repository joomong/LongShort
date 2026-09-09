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
    void checkpoint(boost::archive::binary_iarchive &ar)
    {
        ar & input;
    }
    void checkpoint(boost::archive::binary_oarchive &ar)
    {
        ar & input;
    }

    ModuleInput input;
    IntParameter days;
};
