#pragma once

// MChain: runs two modules in sequence on the same SignalVector.
struct MChain : public Module {
    Module* first = nullptr;
    Module* second = nullptr;

    void init() override {
        first->numAssets = numAssets;
        first->init();
        second->numAssets = numAssets;
        second->init();
    }

    void calculate(unsigned di, SignalVector& output) override {
        first->calculate(di, output);
        second->calculate(di, output);
    }

    void save_state(std::ostream& os) const override {
        first->save_state(os);
        second->save_state(os);
    }

    void load_state(std::istream& is) override {
        first->load_state(is);
        second->load_state(is);
    }
};
