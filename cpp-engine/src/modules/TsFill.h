#pragma once

// MTsFill: sample-and-hold. At panel bars di % days == 0 take x as is (NaN included),
// otherwise output the held value and ignore x. O(1) per instrument.
struct MTsFill : public Module {
    unsigned lookback(unsigned first_bar) const override
    {
        const unsigned d = static_cast<unsigned>((int)days);
        return d > 0 ? (d - first_bar % d) % d : 0;
    }
    void init()
    {
        held.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
    }
    void calculate(unsigned di, SignalVector &output)
    {
        const bool refresh = di % static_cast<unsigned>((int)days) == 0;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (refresh) held[ii] = output[ii];
            output[ii] = held[ii];
        }
    }

    std::vector<float> held;
    IntParameter days{20};
};
