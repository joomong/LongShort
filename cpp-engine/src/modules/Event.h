#pragma once

// MEvent: event-driven holding mask.
// Uses cached signal/trigger rows (or current chain output for implicit signal).
struct MEvent : public Module {
    void init()
    {
        hold.assign(numAssets, 0);
    }
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        const float* sig = useCurrentSignal
            ? output.data
            : signal_cache->data();
        const float* trig = trigger_cache->data();

        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (hold[ii] > 0) hold[ii] -= 1;
            float tv = trig[ii];
            if (std::isfinite(tv) && tv > (float)threshold)
                hold[ii] = maxHolddays;
            if (betAll)
                output[ii] = (hold[ii] > 0) ? sig[ii] : 0.0f;
            else
                output[ii] = sig[ii];
        }
    }
    void checkpoint(boost::archive::binary_iarchive &ar)
    {
        ar & hold;
    }
    void checkpoint(boost::archive::binary_oarchive &ar)
    {
        ar & hold;
    }

    const std::vector<float>* signal_cache = nullptr;
    const std::vector<float>* trigger_cache = nullptr;
    IntParameter useCurrentSignal{0};
    FloatParameter threshold;
    IntParameter maxHolddays;
    IntParameter betAll{1};
    StarVector<int> hold;
};
