#include "checkpoint_manager.h"
#include "../../include/module.h"

#include <fstream>
#include <stdexcept>
#include <cstdint>

static void write_u32(std::ostream& os, uint32_t v) {
    os.write(reinterpret_cast<const char*>(&v), sizeof(v));
}

static void write_f64_vec(std::ostream& os, const std::vector<double>& vec) {
    os.write(reinterpret_cast<const char*>(vec.data()),
             static_cast<std::streamsize>(vec.size() * sizeof(double)));
}

static uint32_t read_u32(std::istream& is) {
    uint32_t v = 0;
    is.read(reinterpret_cast<char*>(&v), sizeof(v));
    return v;
}

static void read_f64_vec(std::istream& is, std::vector<double>& vec) {
    is.read(reinterpret_cast<char*>(vec.data()),
            static_cast<std::streamsize>(vec.size() * sizeof(double)));
}

void CheckpointManager::save(const std::string& path,
                              const SimulatorState& sim_state,
                              const std::vector<Module*>& modules)
{
    std::ofstream os(path, std::ios::binary | std::ios::trunc);
    if (!os)
        throw std::runtime_error("CheckpointManager::save: cannot open " + path);

    write_u32(os, static_cast<uint32_t>(sim_state.di));
    write_u32(os, static_cast<uint32_t>(sim_state.prev_weights.size()));
    write_f64_vec(os, sim_state.prev_weights);
    write_f64_vec(os, sim_state.prev_target);

    // Save each module's state in pipeline order.
    // Stateless modules write 0 bytes (no-op), which is fine.
    for (Module* m : modules)
        m->save_state(os);

    if (!os)
        throw std::runtime_error("CheckpointManager::save: write error on " + path);
}

SimulatorState CheckpointManager::load(const std::string& path,
                                        std::vector<Module*>& modules)
{
    std::ifstream is(path, std::ios::binary);
    if (!is)
        throw std::runtime_error("CheckpointManager::load: cannot open " + path);

    SimulatorState state;
    state.di = static_cast<unsigned>(read_u32(is));

    uint32_t N = read_u32(is);
    state.prev_weights.resize(N);
    state.prev_target.resize(N);
    read_f64_vec(is, state.prev_weights);
    read_f64_vec(is, state.prev_target);

    // Load each module's state in pipeline order.
    for (Module* m : modules)
        m->load_state(is);

    if (!is)
        throw std::runtime_error("CheckpointManager::load: read error on " + path);

    return state;
}
