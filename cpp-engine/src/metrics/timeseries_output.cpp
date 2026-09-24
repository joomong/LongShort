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
    double fee_rate,
    std::size_t start_index,
    const std::vector<double>* frozen_gross,
    const std::vector<double>* borrow_cost)
{
    TimeseriesOutput out;

    if (timestamps.empty()) return out;

    // Start with zero cumulative PnL at the requested trading span.
    const size_t T = timestamps.size();
    if (ret.size() < T || turnover.size() < T || max_weight.size() < T || position_count.size() < T) {
        throw std::invalid_argument("compute_timeseries: input vectors must be at least timestamps.size()");
    }
    const size_t begin = std::min(start_index, T);
    const size_t n = T - begin;

    out.timestamps.reserve(n);
    out.cum_pnl.reserve(n);
    out.cum_pnl_gross.reserve(n);
    out.turnover_pct.reserve(n);
    out.position_count.reserve(n);
    out.max_weight_pct.reserve(n);
    out.drawdown.reserve(n);
    if (frozen_gross != nullptr) out.frozen_gross.reserve(n);
    if (borrow_cost != nullptr) out.borrow_cost.reserve(n);

    double cp_net = 0.0;
    double cp_gross = 0.0;
    double peak_net = 0.0;

    for (size_t i = begin; i < T; ++i) {
        double r = ret[i];
        double tvr = turnover[i];
        double mw = max_weight[i];
        int nz = position_count[i];

        // Net cumulative PnL (additive)
        cp_net += r;

        // Gross cumulative PnL: restore fee (and any financing) to get gross return
        const double borrow = borrow_cost != nullptr && i < borrow_cost->size() ? (*borrow_cost)[i] : 0.0;
        double r_gross = r + tvr * fee_rate;
        if (borrow != 0.0) r_gross += borrow;
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
        // Bars outside a supplied series keep the neutral zero exposure.
        if (frozen_gross != nullptr)
            out.frozen_gross.push_back(i < frozen_gross->size() ? (*frozen_gross)[i] : 0.0);
        if (borrow_cost != nullptr) out.borrow_cost.push_back(borrow);
    }

    return out;
}
