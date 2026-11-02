#pragma once

// MTsStdDev: rolling standard deviation of the finite values in the window, divisor n - ddof
// (O(1) incremental per instrument; variance clamped at 0 like MTsZscore).
// NaN if n < min_count or n - ddof <= 0. A window whose finite values are all equal returns exactly 0:
// the rolling sums keep absolute error from past large values, so the run of equal finite values
// (NaNs skipped) is tracked and checked against the window start.
struct MTsStdDev : public Module {
    void init()
    {
        if (min_count < 0)
            min_count = IntParameter(days);
        input.init(days, numAssets);
        running_sum.assign(numAssets, 0.0);
        running_sum_sq.assign(numAssets, 0.0);
        running_count.assign(numAssets, 0);
        last_finite.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
        run_start.assign(numAssets, 0u);
        bar_count = 0;
    }
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        input.push(output);
        unsigned w = (unsigned)days;
        ++bar_count;
        const unsigned bar = bar_count - 1;
        const unsigned window_start = (bar_count > w) ? bar_count - w : 0u;

        const float* curr = input().data();
        const float* old = (bar_count > w) ? input(w).data() : nullptr;

        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float new_val = curr[ii];
            if (std::isfinite(new_val)) {
                running_sum[ii] += (double)new_val;
                running_sum_sq[ii] += (double)new_val * new_val;
                running_count[ii] += 1;
                if (!(new_val == last_finite[ii]))  // NaN last_finite (none yet) starts a run too
                    run_start[ii] = bar;
                last_finite[ii] = new_val;
            }
            if (old != nullptr) {
                float old_val = old[ii];
                if (std::isfinite(old_val)) {
                    running_sum[ii] -= (double)old_val;
                    running_sum_sq[ii] -= (double)old_val * old_val;
                    running_count[ii] -= 1;
                }
            }
            int n = running_count[ii];
            int denom = n - ddof;
            if (n < min_count || denom <= 0) {
                output[ii] = std::numeric_limits<float>::quiet_NaN();
                continue;
            }
            if (run_start[ii] <= window_start) {
                output[ii] = 0.0f;
                continue;
            }
            double variance = (running_sum_sq[ii] - running_sum[ii] * running_sum[ii] / n) / denom;
            if (variance < 0.0) variance = 0.0;
            output[ii] = (float)std::sqrt(variance);
        }
    }

    StarVector<double> running_sum;
    StarVector<double> running_sum_sq;
    StarVector<int> running_count;
    std::vector<float> last_finite;
    std::vector<unsigned> run_start;
    unsigned bar_count = 0;
    ModuleInput input;
    IntParameter days{20};
    IntParameter ddof{1};
    IntParameter min_count{-1};
};
