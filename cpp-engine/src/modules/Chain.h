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
        second->calculate(di, output);
    }
    void checkpoint(boost::archive::binary_iarchive &ar)
    {
        first->checkpoint(ar);
        second->checkpoint(ar);
    }
    void checkpoint(boost::archive::binary_oarchive &ar)
    {
        first->checkpoint(ar);
        second->checkpoint(ar);
    }

    Module* first = nullptr;
    Module* second = nullptr;
};
