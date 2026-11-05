#pragma once

// MTsEwma: exponentially weighted mean, identical to pandas
// Series.ewm(alpha, adjust, ignore_na=False, min_periods=min_count).mean() per instrument
// (kernel of pandas/_libs/window/aggregations.pyx `ewm` with deltas = 1).
// Alpha from exactly one of span (2/(span+1)), halflife (1-exp(ln 0.5/halflife)) or alpha;
// the unused ones stay <= 0. Infinite memory: the value depends on the panel start; the
// difference to a longer history is bounded by (1-alpha)^warmup. O(1) per instrument.
struct MTsEwma : public Module {
    double resolved_alpha() const
    {
        const double s = span, h = halflife, a = alpha;
        if (a > 0.0) return a;
        if (h > 0.0) return 1.0 - std::exp(std::log(0.5) / h);
        return 2.0 / (s + 1.0);
    }
    // Bars after first_bar until at most 0.1% of the weight lies before first_bar: the output at
    // first_bar + b holds b + 1 observations, so b + 1 = ceil(ln(0.001) / ln(1 - alpha)).
    unsigned lookback(unsigned) const override
    {
        const double a = resolved_alpha();
        if (a >= 1.0) return 0;
        if (!(a > 0.0)) return kNeverValidBar;
        const double bars = std::ceil(std::log(0.001) / std::log(1.0 - a));
        if (!std::isfinite(bars) || bars - 1.0 >= static_cast<double>(kNeverValidBar)) return kNeverValidBar;
        return bars > 1.0 ? static_cast<unsigned>(bars) - 1 : 0;
    }
    void init()
    {
        alpha_d = resolved_alpha();
        weighted.assign(numAssets, std::numeric_limits<double>::quiet_NaN());
        old_wt.assign(numAssets, 0.0);
        nobs.assign(numAssets, 0);
    }
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        const bool adj = (int)adjust != 0;
        const double new_wt = adj ? 1.0 : alpha_d;
        const double old_wt_factor = 1.0 - alpha_d;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            const float v = output[ii];
            const bool fin = std::isfinite(v);
            if (nobs[ii] == 0) {
                if (fin) {
                    weighted[ii] = (double)v;
                    old_wt[ii] = 1.0;
                    nobs[ii] = 1;
                }
            } else {
                old_wt[ii] *= old_wt_factor;  // also on missing bars (ignore_na=False)
                if (fin) {
                    const double x = (double)v;
                    nobs[ii] += 1;
                    if (weighted[ii] != x)
                        weighted[ii] = (old_wt[ii] * weighted[ii] + new_wt * x) / (old_wt[ii] + new_wt);
                    if (adj)
                        old_wt[ii] += new_wt;
                    else
                        old_wt[ii] = 1.0;
                }
            }
            output[ii] = (nobs[ii] >= (int)min_count && nobs[ii] > 0)
                ? (float)weighted[ii] : std::numeric_limits<float>::quiet_NaN();
        }
    }

    std::vector<double> weighted;
    std::vector<double> old_wt;
    std::vector<int> nobs;
    double alpha_d = 0.0;
    FloatParameter span{20.0f};
    FloatParameter halflife{-1.0f};
    FloatParameter alpha{-1.0f};
    IntParameter adjust{1};
    IntParameter min_count{1};
};
