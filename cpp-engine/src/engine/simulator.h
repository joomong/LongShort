#pragma once
#include <vector>
#include <memory>
#include <cstdint>
#include <functional>

struct SimulatorConfig {
    using SignalRowCallback = std::function<void(unsigned di, const float* row, unsigned n)>;

    unsigned T;
    unsigned N;
    double fee_rate;        // fraction (e.g., 0.0005)
    int mode;               // 0=D0, 1=D1
    bool collect_signals = false;  // run() stores the T×N raw signal matrix
    SignalRowCallback signal_row_callback{}; // run() observes raw rows before normalization
};

struct SimulatorOutput {
    std::vector<double> ret;
    std::vector<double> turnover;
    std::vector<double> max_weight;
    std::vector<double> long_weight;
    std::vector<double> short_weight;
    std::vector<int> position_count;
    std::vector<int64_t> timestamps;
    std::vector<std::vector<float>> signals;  // T×N raw signal (only when collect_signals=true)
};

class Module;
struct DataPanel;

class Simulator {
public:
    Simulator(const DataPanel& data, const SimulatorConfig& config);
    SimulatorOutput run(const std::vector<Module*>& pipeline);
    SimulatorOutput run_from_signals(const std::vector<std::vector<float>>& signals);
private:
    const DataPanel& data_;
    SimulatorConfig config_;
};
