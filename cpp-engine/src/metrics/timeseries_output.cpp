#include "timeseries_output.h"
#include <cmath>
#include <algorithm>

TimeseriesOutput compute_timeseries(
    const std::vector<int64_t>& timestamps,
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<int>& nonzero_count,
    double fee_rate)
{
    TimeseriesOutput out;

    if (timestamps.empty()) return out;

    // Skip first bar (index 0); output starts from index 1
    const size_t T = timestamps.size();
    const size_t n = (T > 1) ? T - 1 : 0;

    out.timestamps.reserve(n);
    out.equity.reserve(n);
    out.pnl_gross.reserve(n);
    out.turnover_pct.reserve(n);
    out.nonzero_positions.reserve(n);
    out.max_weight_pct.reserve(n);
    out.drawdown.reserve(n);

    double eq_net = 1.0;
    double eq_gross = 1.0;
    double peak_net = 1.0;

    for (size_t i = 1; i < T; ++i) {
        double r = (i < ret.size()) ? ret[i] : 0.0;
        double tvr = (i < turnover.size()) ? turnover[i] : 0.0;
        double mw = (i < max_weight.size()) ? max_weight[i] : 0.0;
        int nz = (i < nonzero_count.size()) ? nonzero_count[i] : 0;

        // Net equity
        eq_net *= (1.0 + r);

        // Gross equity: restore fee to get gross return
        double r_gross = r + tvr * fee_rate;
        eq_gross *= (1.0 + r_gross);

        // Drawdown
        if (eq_net > peak_net) peak_net = eq_net;
        double dd = eq_net / (peak_net + 1e-12) - 1.0;

        out.timestamps.push_back(timestamps[i]);
        out.equity.push_back(eq_net);
        out.pnl_gross.push_back(eq_gross);
        out.turnover_pct.push_back(tvr * 100.0);
        out.nonzero_positions.push_back(nz);
        out.max_weight_pct.push_back(mw * 100.0);
        out.drawdown.push_back(dd);
    }

    return out;
}
