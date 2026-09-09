#pragma once

// MTsMean: rolling mean over a window (O(1) incremental per instrument).
struct MTsMean : public Module {
    void init()
    {
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
            if (old != nullptr) {
                float old_val = old[ii];
                if (std::isfinite(old_val)) {
                    running_sum[ii] -= (double)old_val;
                    running_count[ii] -= 1;
                }
            }
            if (bar_count < w)
                output[ii] = std::numeric_limits<float>::quiet_NaN();
            else if (running_count[ii] > 0)
                output[ii] = (float)(running_sum[ii] / running_count[ii]);
            else
                output[ii] = std::numeric_limits<float>::quiet_NaN();
        }
    }
    void checkpoint(boost::archive::binary_iarchive &ar)
    {
        ar & input & running_sum & running_count & bar_count;
    }
    void checkpoint(boost::archive::binary_oarchive &ar)
    {
        ar & input & running_sum & running_count & bar_count;
    }

    StarVector<double> running_sum;
    StarVector<int> running_count;
    unsigned bar_count = 0;
    ModuleInput input;
    IntParameter days;
};
