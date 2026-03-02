#pragma once

// MEvent: event-driven holding mask.
// Composite module: owns signal_module and trigger_module.
struct MEvent : public Module {
    void init()
    {
        hold.assign(numAssets, 0);
        sig_buf.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
        trig_buf.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
        signal_module->numAssets = numAssets;
        signal_module->init();
        trigger_module->numAssets = numAssets;
        trigger_module->init();
    }
    void calculate(unsigned di, SignalVector &output)
    {
        SignalVector sig_sv{ sig_buf.data(), numAssets };
        for (unsigned ii = 0; ii < numAssets; ++ii)
            sig_sv[ii] = output[ii];
        signal_module->calculate(di, sig_sv);

        SignalVector trig_sv{ trig_buf.data(), numAssets };
        for (unsigned ii = 0; ii < numAssets; ++ii)
            trig_sv[ii] = output[ii];
        trigger_module->calculate(di, trig_sv);

        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (hold[ii] > 0) hold[ii] -= 1;
            float tv = trig_sv[ii];
            if (std::isfinite(tv) && tv > (float)threshold)
                hold[ii] = maxHolddays;
            if (betAll)
                output[ii] = (hold[ii] > 0) ? sig_sv[ii] : 0.0f;
            else
                output[ii] = sig_sv[ii];
        }
    }
    void checkpoint(boost::archive::binary_iarchive &ar)
    {
        ar & hold;
        signal_module->checkpoint(ar);
        trigger_module->checkpoint(ar);
    }
    void checkpoint(boost::archive::binary_oarchive &ar)
    {
        ar & hold;
        signal_module->checkpoint(ar);
        trigger_module->checkpoint(ar);
    }

    Module* signal_module = nullptr;
    Module* trigger_module = nullptr;
    FloatParameter threshold;
    IntParameter maxHolddays;
    IntParameter betAll{1};
    StarVector<int> hold;
    std::vector<float> sig_buf;
    std::vector<float> trig_buf;
};
