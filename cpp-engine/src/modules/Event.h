#pragma once

// MEvent: event-driven holding mask.
// Composite module: owns signal_module and trigger_module.
struct MEvent : public Module {
    Module* signal_module = nullptr;
    Module* trigger_module = nullptr;
    FloatParameter threshold;
    IntParameter maxHolddays;
    IntParameter betAll{1};   // 1=hold-gated, 0=always output

    void init() override {
        hold_.assign(numAssets, 0);
        sig_buf_.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
        trig_buf_.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
        signal_module->numAssets = numAssets;
        signal_module->init();
        trigger_module->numAssets = numAssets;
        trigger_module->init();
    }

    void calculate(unsigned di, SignalVector& output) override {
        SignalVector sig_sv{ sig_buf_.data(), numAssets };
        for (unsigned ii = 0; ii < numAssets; ++ii)
            sig_sv[ii] = output[ii];
        signal_module->calculate(di, sig_sv);

        SignalVector trig_sv{ trig_buf_.data(), numAssets };
        for (unsigned ii = 0; ii < numAssets; ++ii)
            trig_sv[ii] = output[ii];
        trigger_module->calculate(di, trig_sv);

        double thresh = threshold.value;
        int    maxh   = maxHolddays.value;
        bool   all    = betAll.value != 0;

        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (hold_[ii] > 0)
                hold_[ii] -= 1;

            float tv = trig_sv[ii];
            if (std::isfinite(tv) && tv > static_cast<float>(thresh))
                hold_[ii] = maxh;

            if (all) {
                output[ii] = (hold_[ii] > 0) ? sig_sv[ii] : 0.0f;
            } else {
                output[ii] = sig_sv[ii];
            }
        }
    }

    void save_state(std::ostream& os) const override {
        // hold array
        uint32_t n = static_cast<uint32_t>(hold_.size());
        os.write(reinterpret_cast<const char*>(&n), sizeof(n));
        os.write(reinterpret_cast<const char*>(hold_.data()),
                 static_cast<std::streamsize>(n * sizeof(int)));
        // recurse into sub-modules
        signal_module->save_state(os);
        trigger_module->save_state(os);
    }

    void load_state(std::istream& is) override {
        uint32_t n = 0;
        is.read(reinterpret_cast<char*>(&n), sizeof(n));
        hold_.resize(n);
        is.read(reinterpret_cast<char*>(hold_.data()),
                static_cast<std::streamsize>(n * sizeof(int)));
        signal_module->load_state(is);
        trigger_module->load_state(is);
    }

private:
    StarVector<int> hold_;
    std::vector<float> sig_buf_;
    std::vector<float> trig_buf_;
};
