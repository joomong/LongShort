#pragma once

// MTsSum: rolling sum of the finite values in the window (O(1) incremental per instrument).
// NaN if fewer than min_count finite values.
struct MTsSum : public Module {
    unsigned lookback(unsigned) const override { return (int)days > 1 ? (int)days - 1 : 0; }
    void init()
    {
        if (min_count < 0)
            min_count = IntParameter(days);
        input.init(days, numAssets);
        running_sum.assign(numAssets, 0.0);
        running_count.assign(numAssets, 0);
        bar_count = 0;
    }
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        input.push(output);
        unsigned w = (unsigned)days;
        ++bar_count;

        const float* curr = input().data();
        const float* old = (bar_count > w) ? input(w).data() : nullptr;

        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (std::isfinite(curr[ii])) {
                running_sum[ii] += (double)curr[ii];
                running_count[ii] += 1;
            }
            if (old != nullptr && std::isfinite(old[ii])) {
                running_sum[ii] -= (double)old[ii];
                running_count[ii] -= 1;
            }
            if (running_count[ii] < min_count)
                output[ii] = std::numeric_limits<float>::quiet_NaN();
            else
                output[ii] = (float)running_sum[ii];
        }
    }

    StarVector<double> running_sum;
    StarVector<int> running_count;
    unsigned bar_count = 0;
    ModuleInput input;
    IntParameter days{20};
    IntParameter min_count{-1};
};
