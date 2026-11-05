#pragma once

// MFfd: fixed-window fractional difference per instrument, the desk's getWeights_FFD +
// save_daily_uss_FFD.calculate_ffd. <ffd d="0.24" thres="0.00001"/>
// Weights w_0 = 1, w_k = -w_{k-1}/k * (d - k + 1) stop at |w_k| < thres and, like getWeights_FFD
// (range(1, 1000)), at 1000 weights. out[t] = sum_k w_k * x~[t-k], x~ = input forward-filled from its
// first finite value; NaN when today's input is not finite or fewer than L = len(w) bars have passed
// since the first finite input. Accepted: computation starts at history_start, so a name finite before it,
// NaN across it and restarting later outputs NaN for up to L-1 bars after the restart, where a run from an
// earlier start is finite. Double accumulation in eight interleaved partial sums, oldest-first.
struct MFfd : public Module {
    // Newest-first: w[0] = 1.
    std::vector<double> newest_first_weights() const
    {
        std::vector<double> w{1.0};
        const double dd = static_cast<double>(d), th = static_cast<double>(thres);
        for (int k = 1; k < 1000; ++k) {
            const double next = -w.back() / k * (dd - k + 1);
            if (std::abs(next) < th) break;
            w.push_back(next);
        }
        return w;
    }
    unsigned lookback(unsigned) const override { return static_cast<unsigned>(newest_first_weights().size()) - 1; }
    void init()
    {
        const std::vector<double> w = newest_first_weights();
        // Oldest-first: weights[0] multiplies x~[t-L+1], weights[L-1] = 1 multiplies x~[t].
        weights.assign(w.rbegin(), w.rend());
        L = static_cast<unsigned>(weights.size());
        input.init(L - 1, numAssets);
        filled.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
        seen.assign(numAssets, 0);
        for (auto& a : acc) a.assign(numAssets, 0.0);
    }
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        // Forward-fill, then push the filled row so that input(back) is x~[t-back].
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            const float x = output[ii];
            if (std::isfinite(x)) filled[ii] = x;
            if ((seen[ii] > 0 || std::isfinite(x)) && seen[ii] < L) ++seen[ii];
        }
        input.push(SignalVector{filled.data(), numAssets});

        // Partial sum j takes the weights k = j mod 8 of the first 8*(L/8) weights; the tail goes to sum 0.
        parallel_for_assets(numAssets, L, [&](unsigned begin, unsigned end) {
            for (auto& a : acc) std::fill(a.begin() + begin, a.begin() + end, 0.0);
            const unsigned main = L / 8 * 8;
            for (unsigned k = 0; k < L; ++k) {
                const double w = weights[k];
                const float* row = input(L - 1 - k).data();
                double* a = acc[k < main ? k % 8 : 0].data();
                for (unsigned ii = begin; ii < end; ++ii) a[ii] += w * row[ii];
            }
            for (unsigned ii = begin; ii < end; ++ii) {
                if (!std::isfinite(output[ii]) || seen[ii] < L) {
                    output[ii] = std::numeric_limits<float>::quiet_NaN();
                    continue;
                }
                output[ii] = static_cast<float>(((acc[0][ii] + acc[1][ii]) + (acc[2][ii] + acc[3][ii])) +
                                                ((acc[4][ii] + acc[5][ii]) + (acc[6][ii] + acc[7][ii])));
            }
        });
    }

    std::vector<double> weights;    // oldest-first, as getWeights_FFD
    unsigned L = 0;
    ModuleInput input;              // forward-filled history, lookback L-1
    StarVector<float> filled;       // forward-fill value per instrument (NaN before the first finite input)
    StarVector<unsigned> seen;      // bars since the first finite input, capped at L
    StarVector<double> acc[8];      // scratch partial sums
    FloatParameter d{0.24f};
    FloatParameter thres{0.00001f};
    // HELP fractional difference: d=0.24 thres=0.00001, out = sum_k w_k * ffill(in)[t-k]
};
