#include "timeseries_output.h"
#include <cmath>
#include <algorithm>
#include <stdexcept>

TimeseriesOutput compute_timeseries(
    const std::vector<int64_t>& timestamps,
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<int>& position_count,
    double fee_rate)
{
    TimeseriesOutput out;

    if (timestamps.empty()) return out;

    // Skip first bar (index 0); output starts from index 1
    const size_t T = timestamps.size();
    if (ret.size() < T || turnover.size() < T || max_weight.size() < T || position_count.size() < T) {
        throw std::invalid_argument("compute_timeseries: input vectors must be at least timestamps.size()");
    }
    const size_t n = (T > 1) ? T - 1 : 0;

    out.timestamps.reserve(n);
    out.cum_pnl.reserve(n);
    out.cum_pnl_gross.reserve(n);
    out.turnover_pct.reserve(n);
    out.position_count.reserve(n);
    out.max_weight_pct.reserve(n);
    out.drawdown.reserve(n);

    double cp_net = 0.0;
    double cp_gross = 0.0;
    double peak_net = 0.0;

    for (size_t i = 1; i < T; ++i) {
        double r = ret[i];
        double tvr = turnover[i];
        double mw = max_weight[i];
        int nz = position_count[i];

        // Net cumulative PnL (additive)
        cp_net += r;

        // Gross cumulative PnL: restore fee to get gross return
        double r_gross = r + tvr * fee_rate;
        cp_gross += r_gross;

        // Drawdown (additive: peak - current)
        if (cp_net > peak_net) peak_net = cp_net;
        double dd = cp_net - peak_net;

        out.timestamps.push_back(timestamps[i]);
        out.cum_pnl.push_back(cp_net);
        out.cum_pnl_gross.push_back(cp_gross);
        out.turnover_pct.push_back(tvr * 100.0);
        out.position_count.push_back(nz);
        out.max_weight_pct.push_back(mw * 100.0);
        out.drawdown.push_back(dd);
    }

    return out;
}
