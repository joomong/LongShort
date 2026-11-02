#pragma once

// MChain: runs two modules in sequence on the same SignalVector.
struct MChain : public Module {
    void init()
    {
        first->numAssets = numAssets;
        first->init();
        second->numAssets = numAssets;
        second->init();
    }
    void calculate(unsigned di, SignalVector &output)
    {
        first->calculate(di, output);
        output.inf_to_nan();
        second->calculate(di, output);
        output.inf_to_nan();
    }

    Module* first = nullptr;
    Module* second = nullptr;
};
