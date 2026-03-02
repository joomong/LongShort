#pragma once

#include <string>
#include <vector>
#include <boost/archive/binary_iarchive.hpp>
#include <boost/archive/binary_oarchive.hpp>
#include <boost/serialization/vector.hpp>

class Module;

struct SimulatorState {
    unsigned di = 0;
    std::vector<double> prev_weights;
    std::vector<double> prev_target;

    template<class Archive>
    void serialize(Archive& ar, const unsigned int /*version*/) {
        ar & di & prev_weights & prev_target;
    }
};

// CheckpointManager: boost::archive binary save/load of simulator + module state.
class CheckpointManager {
public:
    static void save(const std::string& path,
                     const SimulatorState& sim_state,
                     const std::vector<Module*>& modules);

    static SimulatorState load(const std::string& path,
                               std::vector<Module*>& modules);
};
