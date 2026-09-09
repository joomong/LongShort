#include "simulator.h"
#include "../../include/module.h"
#include "../data/data_panel.h"
#include <cmath>
#include <algorithm>
#include <stdexcept>
#include <string>

namespace {
bool valid_close(float value) {
    return std::isfinite(value) && value > 0.0f;
}

double holding_return(const DataPanel& data, unsigned di,
                      const std::vector<double>& previous_weights,
                      const std::vector<double>& execution_weights) {
    const float* close = data.row("close", di);
    const float* previous_close = di > 0 ? data.row("close", di - 1) : nullptr;
    double result = 0.0;
    for (unsigned ii = 0; ii < data.N; ++ii) {
        // Missing valuation cannot stand for a zero return or free liquidation.
        if ((previous_weights[ii] != 0.0 || execution_weights[ii] != 0.0) &&
            !valid_close(close[ii])) {
            throw std::runtime_error("Cannot value or execute position: invalid close at bar " +
                                     std::to_string(di) + ", asset " + std::to_string(ii));
        }
        if (di > 0 && previous_weights[ii] != 0.0) {
            if (!valid_close(previous_close[ii])) {
                throw std::runtime_error("Cannot value position: invalid previous close at bar " +
                                         std::to_string(di) + ", asset " + std::to_string(ii));
            }
            result += previous_weights[ii] *
                (static_cast<double>(close[ii]) / previous_close[ii] - 1.0);
        }
    }
    return result;
}

SimulatorOutput make_output(const DataPanel& data) {
    SimulatorOutput output;
    output.ret.assign(data.T, 0.0);
    output.turnover.assign(data.T, 0.0);
    output.max_weight.assign(data.T, 0.0);
    output.long_weight.assign(data.T, 0.0);
    output.short_weight.assign(data.T, 0.0);
    output.position_count.assign(data.T, 0);
    output.timestamps = data.timestamps;
    return output;
}

// One portfolio transition shared by generated and precomputed signals.
class PortfolioSimulation {
public:
    PortfolioSimulation(const DataPanel& data, const SimulatorConfig& config)
        : output(make_output(data)), data_(data), config_(config),
          previous_weights_(data.N, 0.0),
          previous_targets_(config.mode == 1 ? data.N : 0, 0.0),
          targets_(data.N, 0.0) {}

    void advance(unsigned di, const float* signal) {
        const float* close = data_.row("close", di);
        double absolute_signal_sum = 0.0;
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            if (valid_close(close[ii]) && std::isfinite(signal[ii]))
                absolute_signal_sum += std::fabs(static_cast<double>(signal[ii]));
        }

        std::fill(targets_.begin(), targets_.end(), 0.0);
        if (absolute_signal_sum > 1e-12) {
            for (unsigned ii = 0; ii < data_.N; ++ii) {
                if (valid_close(close[ii]) && std::isfinite(signal[ii]))
                    targets_[ii] = static_cast<double>(signal[ii]) / absolute_signal_sum;
            }
        }

        const auto& execution_weights = config_.mode == 0 ? targets_ : previous_targets_;
        output.ret[di] = holding_return(data_, di, previous_weights_, execution_weights);

        double turnover = 0.0;
        for (unsigned ii = 0; ii < data_.N; ++ii)
            turnover += std::fabs(execution_weights[ii] - previous_weights_[ii]);
        output.turnover[di] = turnover;
        output.ret[di] -= turnover * config_.fee_rate;

        double max_weight = 0.0, long_weight = 0.0, short_weight = 0.0;
        int position_count = 0;
        for (double weight : execution_weights) {
            const double absolute_weight = std::fabs(weight);
            if (absolute_weight > max_weight) max_weight = absolute_weight;
            if (weight > 0.0) long_weight += weight;
            if (weight < 0.0) short_weight += -weight;
            if (absolute_weight > 1e-12) ++position_count;
        }
        output.max_weight[di] = max_weight;
        output.long_weight[di] = long_weight;
        output.short_weight[di] = short_weight;
        output.position_count[di] = position_count;

        if (config_.mode == 1) {
            previous_weights_.swap(previous_targets_);
            previous_targets_.swap(targets_);
        } else {
            previous_weights_.swap(targets_);
        }
    }

    SimulatorOutput output;

private:
    const DataPanel& data_;
    const SimulatorConfig& config_;
    std::vector<double> previous_weights_;
    std::vector<double> previous_targets_;
    std::vector<double> targets_;
};
} // namespace

Simulator::Simulator(const DataPanel& data, const SimulatorConfig& config)
    : data_(data), config_(config)
{
    if (config_.T != data_.T || config_.N != data_.N || config_.N == 0)
        throw std::invalid_argument("Simulator dimensions must match a nonempty asset panel");
    if (config_.mode != 0 && config_.mode != 1)
        throw std::invalid_argument("Simulator mode must be 0 or 1");
    if (!std::isfinite(config_.fee_rate) || config_.fee_rate < 0.0)
        throw std::invalid_argument("Simulator fee_rate must be finite and nonnegative");
}

SimulatorOutput Simulator::run(const std::vector<Module*>& pipeline) {
    PortfolioSimulation simulation(data_, config_);
    std::vector<float> signal_buffer(data_.N, 0.0f);
    SignalVector signal{signal_buffer.data(), data_.N};
    if (config_.collect_signals)
        simulation.output.signals.resize(data_.T);

    for (Module* module : pipeline) {
        module->numAssets = data_.N;
        module->init();
    }
    for (unsigned di = 0; di < data_.T; ++di) {
        if (!pipeline.empty() && !pipeline[0]->skips_nan_init())
            signal.fill_nan();
        for (Module* module : pipeline)
            module->calculate(di, signal);

        if (config_.collect_signals)
            simulation.output.signals[di].assign(signal_buffer.begin(), signal_buffer.end());
        if (config_.signal_row_callback)
            config_.signal_row_callback(di, signal_buffer.data(), data_.N);
        simulation.advance(di, signal_buffer.data());
    }
    return std::move(simulation.output);
}

SimulatorOutput Simulator::run_from_signals(const std::vector<std::vector<float>>& signals) {
    if (signals.size() != config_.T ||
        std::any_of(signals.begin(), signals.end(), [this](const auto& row) {
            return row.size() != config_.N;
        }))
        throw std::invalid_argument("Signal matrix dimensions must match the data panel");

    PortfolioSimulation simulation(data_, config_);
    for (unsigned di = 0; di < data_.T; ++di)
        simulation.advance(di, signals[di].data());
    return std::move(simulation.output);
}
