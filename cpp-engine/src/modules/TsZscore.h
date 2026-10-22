#pragma once

// MTsZscore: rolling z-score (O(1) incremental per instrument).
struct MTsZscore : public Module {
    void init()
    {
        if (min_count < 0)
            min_count = IntParameter(days);
        input.init(days, numAssets);
        running_sum.assign(numAssets, 0.0);
        running_sum_sq.assign(numAssets, 0.0);
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
            float new_val = curr[ii];

            if (std::isfinite(new_val)) {
                running_sum[ii] += (double)new_val;
                running_sum_sq[ii] += (double)new_val * new_val;
                running_count[ii] += 1;
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
            if (n < min_count) {
                output[ii] = 0.0f;
                continue;
            }
            if (!std::isfinite(new_val)) {
                output[ii] = std::numeric_limits<float>::quiet_NaN();
                continue;
            }
            double mean = running_sum[ii] / n;
            int denom = n - ddof;
            double variance = (denom > 0)
                ? (running_sum_sq[ii] - running_sum[ii] * running_sum[ii] / n) / denom
                : 0.0;
            if (variance < 0.0) variance = 0.0;
            double std_dev = std::sqrt(variance);
            if (std_dev < eps)
                output[ii] = 0.0f;
            else
                output[ii] = (float)((new_val - mean) / (std_dev + eps));
        }
    }
    void checkpoint(boost::archive::binary_iarchive &ar)
    {
        ar & input & running_sum & running_sum_sq & running_count & bar_count;
    }
    void checkpoint(boost::archive::binary_oarchive &ar)
    {
        ar & input & running_sum & running_sum_sq & running_count & bar_count;
    }

    StarVector<double> running_sum;
    StarVector<double> running_sum_sq;
    StarVector<int> running_count;
    unsigned bar_count = 0;
    ModuleInput input;
    IntParameter days;
    IntParameter ddof{1};
    IntParameter min_count{-1};
    FloatParameter eps{1e-12f};
};
