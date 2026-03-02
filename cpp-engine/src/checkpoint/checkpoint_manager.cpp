#include "checkpoint_manager.h"
#include "../../include/module.h"

#include <fstream>
#include <stdexcept>

void CheckpointManager::save(const std::string& path,
                              const SimulatorState& sim_state,
                              const std::vector<Module*>& modules)
{
    std::ofstream ofs(path, std::ios::binary | std::ios::trunc);
    if (!ofs)
        throw std::runtime_error("CheckpointManager::save: cannot open " + path);

    boost::archive::binary_oarchive ar(ofs);
    ar & const_cast<SimulatorState&>(sim_state);

    for (Module* m : modules)
        m->checkpoint(ar);
}

SimulatorState CheckpointManager::load(const std::string& path,
                                        std::vector<Module*>& modules)
{
    std::ifstream ifs(path, std::ios::binary);
    if (!ifs)
        throw std::runtime_error("CheckpointManager::load: cannot open " + path);

    boost::archive::binary_iarchive ar(ifs);
    SimulatorState state;
    ar & state;

    for (Module* m : modules)
        m->checkpoint(ar);

    return state;
}
