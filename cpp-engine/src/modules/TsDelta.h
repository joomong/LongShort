#pragma once

// MTsDelta: x[t] - x[t-days]; NaN if either is non-finite or fewer than days+1 bars seen.
struct MTsDelta : public Module {
    void init()
    {
        input.init(days, numAssets);
    }
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        input.push(output);
        const float* curr = input().data();
        const float* old = input(days).data();  // all-NaN row until days+1 bars were pushed
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float c = curr[ii], o = old[ii];
            output[ii] = (std::isfinite(c) && std::isfinite(o))
                ? c - o : std::numeric_limits<float>::quiet_NaN();
        }
    }

    ModuleInput input;
    IntParameter days{1};
};
