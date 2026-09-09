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
        // A held or executing position needs a valid close. Missing valuation
        // cannot be silently treated as a zero return or a free liquidation.
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
    const unsigned T = config_.T;
    const unsigned N = config_.N;
    const double fee_rate = config_.fee_rate;
    const int mode = config_.mode;

    SimulatorOutput out;
    out.ret.assign(T, 0.0);
    out.turnover.assign(T, 0.0);
    out.min_bet.assign(T, 0.0);
    out.max_weight.assign(T, 0.0);
    out.long_weight.assign(T, 0.0);
    out.short_weight.assign(T, 0.0);
    out.nonzero_count.assign(T, 0);
    out.timestamps = data_.timestamps;

    std::vector<float> signal_buf(N, 0.0f);
    SignalVector sv{ signal_buf.data(), N };

    std::vector<double> prev_weights(N, 0.0);
    std::vector<double> prev_target(N, 0.0);
    std::vector<double> target(N, 0.0);

    const bool collect = config_.collect_signals;
    if (collect)
        out.signals.resize(T);

    // STAR-style init: set numAssets before calling init()
    for (Module* mod : pipeline) {
        mod->numAssets = N;
        mod->init();
    }

    for (unsigned di = 0; di < T; ++di) {
        // 1. NaN init + execute module pipeline
        if (!pipeline.empty() && !pipeline[0]->skips_nan_init())
            sv.fill_nan();
        for (Module* mod : pipeline)
            mod->calculate(di, sv);

        // Collect raw signal (before normalization)
        if (collect)
            out.signals[di].assign(signal_buf.begin(), signal_buf.end());
        if (config_.signal_row_callback)
            config_.signal_row_callback(di, signal_buf.data(), N);

        // 2. Normalize signal -> target weights
        const float* close_row = data_.row("close", di);

        double abs_sum = 0.0;
        for (unsigned ii = 0; ii < N; ++ii) {
            if (valid_close(close_row[ii]) && std::isfinite(signal_buf[ii]))
                abs_sum += std::fabs(static_cast<double>(signal_buf[ii]));
        }

        for (unsigned ii = 0; ii < N; ++ii)
            target[ii] = 0.0;
        if (abs_sum > 1e-12) {
            for (unsigned ii = 0; ii < N; ++ii) {
                if (valid_close(close_row[ii]) && std::isfinite(signal_buf[ii]))
                    target[ii] = static_cast<double>(signal_buf[ii]) / abs_sum;
            }
        }

        // 3. Execution weight (mode dependent)
        const std::vector<double>& exec_w = (mode == 0) ? target : prev_target;

        // 4. Portfolio return (prev_weights based -> no-lookahead)
        out.ret[di] = holding_return(data_, di, prev_weights, exec_w);

        // 5. Turnover + fee
        double tvr = 0.0;
        for (unsigned ii = 0; ii < N; ++ii)
            tvr += std::fabs(exec_w[ii] - prev_weights[ii]);
        out.turnover[di] = tvr;
        out.ret[di] -= tvr * fee_rate;

        // 6. Per-bar stats
        double max_w = 0.0, lw = 0.0, sw = 0.0, abs_sum_exec = 0.0;
        int nz = 0;
        for (unsigned ii = 0; ii < N; ++ii) {
            double ew = exec_w[ii];
            double aew = std::fabs(ew);
            abs_sum_exec += aew;
            if (aew > max_w) max_w = aew;
            if (ew > 0.0) lw += ew;
            if (ew < 0.0) sw += -ew;
            if (aew > 1e-12) ++nz;
        }
        out.min_bet[di] = abs_sum_exec / static_cast<double>(N);
        out.max_weight[di] = max_w;
        out.long_weight[di] = lw;
        out.short_weight[di] = sw;
        out.nonzero_count[di] = nz;

        // 7. State update
        if (mode == 1) {
            prev_weights.swap(prev_target);
            prev_target.swap(target);
        } else {
            prev_weights.swap(target);
        }
    }

    return out;
}

SimulatorOutput Simulator::run_from_signals(const std::vector<std::vector<float>>& signals) {
    if (signals.size() != config_.T ||
        std::any_of(signals.begin(), signals.end(), [this](const auto& row) {
            return row.size() != config_.N;
        }))
        throw std::invalid_argument("Signal matrix dimensions must match the data panel");
    const unsigned T = config_.T;
    const unsigned N = config_.N;
    const double fee_rate = config_.fee_rate;
    const int mode = config_.mode;

    SimulatorOutput out;
    out.ret.assign(T, 0.0);
    out.turnover.assign(T, 0.0);
    out.min_bet.assign(T, 0.0);
    out.max_weight.assign(T, 0.0);
    out.long_weight.assign(T, 0.0);
    out.short_weight.assign(T, 0.0);
    out.nonzero_count.assign(T, 0);
    out.timestamps = data_.timestamps;

    std::vector<float> signal_buf(N, 0.0f);
    std::vector<double> prev_weights(N, 0.0);
    std::vector<double> prev_target(N, 0.0);
    std::vector<double> target(N, 0.0);

    for (unsigned di = 0; di < T; ++di) {
        // Copy pre-computed signals
        for (unsigned ii = 0; ii < N; ++ii)
            signal_buf[ii] = signals[di][ii];

        // Normalize signal -> target weights
        const float* close_row = data_.row("close", di);

        double abs_sum = 0.0;
        for (unsigned ii = 0; ii < N; ++ii) {
            if (valid_close(close_row[ii]) && std::isfinite(signal_buf[ii]))
                abs_sum += std::fabs(static_cast<double>(signal_buf[ii]));
        }

        for (unsigned ii = 0; ii < N; ++ii)
            target[ii] = 0.0;
        if (abs_sum > 1e-12) {
            for (unsigned ii = 0; ii < N; ++ii) {
                if (valid_close(close_row[ii]) && std::isfinite(signal_buf[ii]))
                    target[ii] = static_cast<double>(signal_buf[ii]) / abs_sum;
            }
        }

        // Execution weight (mode dependent)
        const std::vector<double>& exec_w = (mode == 0) ? target : prev_target;

        // Portfolio return (prev_weights based -> no-lookahead)
        out.ret[di] = holding_return(data_, di, prev_weights, exec_w);

        // Turnover + fee
        double tvr = 0.0;
        for (unsigned ii = 0; ii < N; ++ii)
            tvr += std::fabs(exec_w[ii] - prev_weights[ii]);
        out.turnover[di] = tvr;
        out.ret[di] -= tvr * fee_rate;

        // Per-bar stats
        double max_w = 0.0, lw = 0.0, sw = 0.0, abs_sum_exec = 0.0;
        int nz = 0;
        for (unsigned ii = 0; ii < N; ++ii) {
            double ew = exec_w[ii];
            double aew = std::fabs(ew);
            abs_sum_exec += aew;
            if (aew > max_w) max_w = aew;
            if (ew > 0.0) lw += ew;
            if (ew < 0.0) sw += -ew;
            if (aew > 1e-12) ++nz;
        }
        out.min_bet[di] = abs_sum_exec / static_cast<double>(N);
        out.max_weight[di] = max_w;
        out.long_weight[di] = lw;
        out.short_weight[di] = sw;
        out.nonzero_count[di] = nz;

        // State update
        if (mode == 1) {
            prev_weights.swap(prev_target);
            prev_target.swap(target);
        } else {
            prev_weights.swap(target);
        }
    }

    return out;
}
