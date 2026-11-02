#pragma once

// MSignalSave: pass-through that caches output for later retrieval.
// Inserted at <signal id="..."/> positions in the pipeline.
struct MSignalSave : public Module {
    void init()
    {
        cache.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
    }
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        for (unsigned ii = 0; ii < numAssets; ++ii)
            cache[ii] = output[ii];
    }

    std::vector<float> cache;
    std::string label;
};

// MSignalLoad: restores output from a referenced MSignalSave's cache.
// Inserted before modules with input="..." attribute.
struct MSignalLoad : public Module {
    void init() {}
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        for (unsigned ii = 0; ii < numAssets; ++ii)
            output[ii] = source->cache[ii];
    }

    MSignalSave* source = nullptr;
};
