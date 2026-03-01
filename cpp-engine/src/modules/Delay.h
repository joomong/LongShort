#pragma once

struct MDelay : public Module {
    ModuleInput input;
    IntParameter days;

    void init() override {
        input.init(static_cast<unsigned>(days.value), numAssets);
    }

    void calculate(unsigned di, SignalVector& output) override {
        input.push(output);
        const SignalVector& delayed = input(static_cast<unsigned>(days.value));
        for (unsigned ii = 0; ii < numAssets; ++ii)
            output[ii] = delayed[ii];
    }

    void save_state(std::ostream& os) const override {
        input.save(os);
    }

    void load_state(std::istream& is) override {
        input.load(is);
    }
};
