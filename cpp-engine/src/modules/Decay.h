#pragma once
#include "decay_kernel.h"

// MDecay: linear decay weighted average (O(1) incremental per instrument).
// out[ii] = (in[di]*days + in[di-1]*(days-1) + ... + in[di-days+1]*1) / weighted_cnt
struct MDecay : public Module
{
    unsigned lookback(unsigned) const override { return (int)days > 1 ? (int)days - 1 : 0; }
    void init()
    {
        input.init(days, numAssets);
        weights.assign(numAssets, 0);
        num.assign(numAssets, 0);
        sum.assign(numAssets, 0.0);
        diff.assign(numAssets, 0.0);
        int n = days*(days+1)/2 + 1;
        div.resize(n, 0.0);
        for (int i = 1; i < n; ++i)
            div[i] = 1.0/(double)i;
        use_avx2_kernel = mdecay_avx2_enabled();
        setting_output_to_nan_is_not_necessary();
    }
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        input.push(output);
        const float* past = input(days).data();
        const float* curr = input().data();
        unsigned ii = 0;
        if (use_avx2_kernel) {
            const unsigned ii_aligned_end = numAssets / 8 * 8;
            for (; ii < ii_aligned_end; ii += 8)
                process_8instruments_avx2(ii, curr, past, output);
        }

        const unsigned ii_aligned_end = numAssets / 2 * 2;
        for (; ii < ii_aligned_end; ii += 2) {
            process_1instrument(ii + 0, curr, past, output);
            process_1instrument(ii + 1, curr, past, output);
        }
        for (; ii < (unsigned)numAssets; ++ii)
            process_1instrument(ii, curr, past, output);
    }

    std::vector<double> div;
    StarVector<int> weights, num;
    StarVector<double> sum, diff;
    ModuleInput input;
    IntParameter days;
    bool use_avx2_kernel = false;
    //TEST days:"10"
    //TEST days:"50"
    // HELP linear decay, n.g.: days=5, out = (in[di] * 5 + in[di-1] * 4 + ...) / weighted_cnt

    __attribute__((always_inline))
    inline void process_1instrument(unsigned ii, const float* curr, const float* past, SignalVector &output)
    {
        float prevValue = past[ii];
        sum[ii] -= diff[ii];
        weights[ii] -= num[ii];
        if (isfinite(prevValue))
        {
            diff[ii] -= (double)prevValue;
            --num[ii];
        }
        float newValue = curr[ii];
        if (isfinite(newValue))
        {
            sum[ii] += (double)newValue * days;
            diff[ii] += (double)newValue;
            weights[ii] += days;
            ++num[ii];
        }
        if (weights[ii] > 0 && weights[ii] < (int)div.size())
            output[ii] = (float)(sum[ii] * div[weights[ii]]);
        else
            output[ii] = std::numeric_limits<float>::quiet_NaN();
    }

    void process_8instruments_avx2(unsigned ii, const float* curr, const float* past, SignalVector &output)
    {
        mdecay_process_8_avx2(
            ii,
            days,
            curr,
            past,
            weights.data(),
            num.data(),
            sum.data(),
            diff.data(),
            div.data(),
            div.size(),
            output.data);
    }
};
