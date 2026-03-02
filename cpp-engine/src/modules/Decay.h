#pragma once

// MDecay: linear decay weighted average (O(1) incremental per instrument).
// out[ii] = (in[di]*days + in[di-1]*(days-1) + ... + in[di-days+1]*1) / weighted_cnt
struct MDecay : public Module
{
    void init()
    {
        input.maxLookBack = days;
        weights.resize(numAssets, 0);
        num.resize(numAssets, 0);
        sum.resize(numAssets, 0.0);
        diff.resize(numAssets, 0.0);
        int n = days*(days+1)/2 + 1;
        div.resize(n, 0.0);
        for (int i = 1; i < n; ++i)
            div[i] = 1.0/(double)i;
        setting_output_to_nan_is_not_necessary();
    }
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        input.push(output);
        const float* past = input(days).data();
        const float* curr = input().data();
        #ifdef __AVX2__
            unsigned ii = 0;
            unsigned ii_aligned_end = numAssets/8*8;
            for(; ii < ii_aligned_end; ii+=8)
                process_8instruments(ii, curr, past, output);
            for(; ii < (unsigned)numAssets; ++ii)
                process_1instrument(ii, curr, past, output);
        #else
            unsigned ii = 0;
            unsigned ii_aligned_end = numAssets/2*2;
            for(; ii < ii_aligned_end; ii+=2)
            {
                process_1instrument(ii+0, curr, past, output);
                process_1instrument(ii+1, curr, past, output);
            }
            for(; ii < (unsigned)numAssets; ++ii)
                process_1instrument(ii, curr, past, output);
        #endif
    }
    void checkpoint(boost::archive::binary_iarchive &ar)
    {
        ar & input & weights & num & sum & diff;
    }
    void checkpoint(boost::archive::binary_oarchive &ar)
    {
        ar & input & weights & num & sum & diff;
    }

    std::vector<double> div;
    StarVector<int> weights, num;
    StarVector<double> sum, diff;
    ModuleInput input;
    IntParameter days;
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

    void process_8instruments(unsigned ii, const float* curr, const float* past, SignalVector &output)
    {
        for (unsigned k = 0; k < 8; ++k)
            process_1instrument(ii + k, curr, past, output);
    }
};
