#pragma once

// Test-only discovered module (tests/test_discovered_modules.cpp): binds the
// run-selected universe and a GroupParameter through the generated factory.
namespace discovered_fixture {
struct MUniverseGroup : public Module {
    const DataPanel* panel = nullptr;
    const std::vector<uint8_t>* universe = nullptr;
    GroupParameter group{""};
    IntParameter window{3};

    void init() {}
    void calculate(unsigned, SignalVector&) {}
};
}
