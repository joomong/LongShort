#pragma once
#include <vector>
#include <memory>
#include <cstdint>

struct SimulatorConfig {
    unsigned T;
    unsigned N;
    double fee_rate;        // fraction (e.g., 0.0005)
    int mode;               // 0=D0, 1=D1
    unsigned bars_per_year; // 365
    bool collect_signals = false;  // true = store T×N raw signal matrix (single alpha)
};

struct SimulatorOutput {
    std::vector<double> ret;
    std::vector<double> turnover;
    std::vector<double> min_bet;
    std::vector<double> max_weight;
    std::vector<double> long_weight;
    std::vector<double> short_weight;
    std::vector<int> nonzero_count;
    std::vector<int64_t> timestamps;
    std::vector<std::vector<float>> signals;  // T×N raw signal (only when collect_signals=true)
};

class Module;
struct DataPanel;

class Simulator {
public:
    Simulator(const DataPanel& data, const SimulatorConfig& config);
    SimulatorOutput run(const std::vector<Module*>& pipeline);
private:
    const DataPanel& data_;
    SimulatorConfig config_;
};
