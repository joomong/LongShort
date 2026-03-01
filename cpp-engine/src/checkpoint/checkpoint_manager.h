#pragma once

#include <string>
#include <vector>
#include <iosfwd>

class Module;

struct SimulatorState {
    unsigned di;
    std::vector<double> prev_weights;
    std::vector<double> prev_target;
};

// CheckpointManager: raw binary save/load of simulator + module state.
// Uses Module::save_state/load_state virtuals (no ICheckpointable needed).
class CheckpointManager {
public:
    static void save(const std::string& path,
                     const SimulatorState& sim_state,
                     const std::vector<Module*>& modules);

    static SimulatorState load(const std::string& path,
                               std::vector<Module*>& modules);
};
