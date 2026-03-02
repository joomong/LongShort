#include "simulator.h"
#include "../../include/module.h"
#include "../data/data_panel.h"
#include <cmath>
#include <algorithm>

Simulator::Simulator(const DataPanel& data, const SimulatorConfig& config)
    : data_(data), config_(config)
{}

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

        // 2. Normalize signal -> target weights
        const float* close_row = data_.row("close", di);

        double abs_sum = 0.0;
        for (unsigned ii = 0; ii < N; ++ii) {
            if (std::isfinite(close_row[ii]) && std::isfinite(signal_buf[ii]))
                abs_sum += std::fabs(static_cast<double>(signal_buf[ii]));
        }

        for (unsigned ii = 0; ii < N; ++ii)
            target[ii] = 0.0;
        if (abs_sum > 1e-12) {
            for (unsigned ii = 0; ii < N; ++ii) {
                if (std::isfinite(close_row[ii]) && std::isfinite(signal_buf[ii]))
                    target[ii] = static_cast<double>(signal_buf[ii]) / abs_sum;
            }
        }

        // 3. Execution weight (mode dependent)
        const std::vector<double>& exec_w = (mode == 0) ? target : prev_target;

        // 4. Portfolio return (prev_weights based -> no-lookahead)
        if (di > 0) {
            const float* prev_close = data_.row("close", di - 1);
            for (unsigned ii = 0; ii < N; ++ii) {
                if (prev_close[ii] > 0.0f && close_row[ii] > 0.0f) {
                    out.ret[di] += prev_weights[ii] *
                        (static_cast<double>(close_row[ii]) / static_cast<double>(prev_close[ii]) - 1.0);
                }
            }
        }

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
        prev_weights = exec_w;
        prev_target = target;
    }

    return out;
}
