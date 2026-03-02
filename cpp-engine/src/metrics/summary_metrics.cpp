#include "summary_metrics.h"
#include <cmath>
#include <algorithm>
#include <numeric>

SummaryMetrics compute_summary(
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& min_bet,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& nonzero_count,
    unsigned bars_per_year)
{
    SummaryMetrics m{};

    // Collect finite returns (caller guarantees no warmup bar in input)
    std::vector<double> finite_r;
    finite_r.reserve(ret.size());
    for (size_t i = 0; i < ret.size(); ++i) {
        if (std::isfinite(ret[i])) finite_r.push_back(ret[i]);
    }
    if (finite_r.empty()) return m;

    const size_t n = finite_r.size();

    // Cumulative PnL (additive, STAR-style fixed book size)
    std::vector<double> cum_pnl(n);
    cum_pnl[0] = finite_r[0];
    for (size_t i = 1; i < n; ++i)
        cum_pnl[i] = cum_pnl[i - 1] + finite_r[i];

    // pnl = sum of daily returns
    m.pnl = cum_pnl.back();

    // ann_ret = mean(daily_ret) * bars_per_year
    double sum_r = 0.0;
    for (double v : finite_r) sum_r += v;
    double mean_r = sum_r / static_cast<double>(n);
    m.ann_ret = mean_r * static_cast<double>(bars_per_year);

    // sharpe
    double sharpe_val = 0.0;
    if (n >= 2) {
        double sq_sum = 0.0;
        for (double v : finite_r) {
            double d = v - mean_r;
            sq_sum += d * d;
        }
        double std_r = std::sqrt(sq_sum / static_cast<double>(n - 1));  // ddof=1
        if (std_r > 0.0)
            sharpe_val = (mean_r / (std_r + 1e-12)) * std::sqrt(static_cast<double>(bars_per_year));
    }
    m.sharpe = sharpe_val;

    // mdd (additive: peak cumPnL - current cumPnL)
    double peak = cum_pnl[0];
    double max_dd = 0.0;
    for (size_t i = 0; i < n; ++i) {
        if (cum_pnl[i] > peak) peak = cum_pnl[i];
        double dd = cum_pnl[i] - peak;  // negative when in drawdown
        if (dd < max_dd) max_dd = dd;
    }
    m.mdd = max_dd;

    // turnover: mean of finite values
    {
        double sum = 0.0; int cnt = 0;
        for (size_t i = 0; i < turnover.size(); ++i) {
            if (std::isfinite(turnover[i])) { sum += turnover[i]; ++cnt; }
        }
        m.turnover = (cnt > 0) ? sum / cnt : 0.0;
    }

    // min_bet: mean of finite values
    {
        double sum = 0.0; int cnt = 0;
        for (size_t i = 0; i < min_bet.size(); ++i) {
            if (std::isfinite(min_bet[i])) { sum += min_bet[i]; ++cnt; }
        }
        m.min_bet = (cnt > 0) ? sum / cnt : 0.0;
    }

    // max_weight: max of finite values
    {
        double mx = 0.0;
        for (size_t i = 0; i < max_weight.size(); ++i) {
            if (std::isfinite(max_weight[i]) && max_weight[i] > mx)
                mx = max_weight[i];
        }
        m.max_weight = mx;
    }

    // nonzero_position: count(nonzero_count > 0)
    {
        int cnt = 0;
        for (size_t i = 0; i < nonzero_count.size(); ++i) {
            if (nonzero_count[i] > 0) ++cnt;
        }
        m.nonzero_position = cnt;
    }

    // win_rate
    {
        int wins = 0, total = 0;
        for (double v : finite_r) {
            ++total;
            if (v > 0.0) ++wins;
        }
        m.win_rate = (total > 0) ? static_cast<double>(wins) / total : 0.0;
    }

    // margin
    m.margin = mean_r / (m.turnover + 1e-12);

    // long_ratio / short_ratio: mean(lw / (lw + sw)) for valid bars where lw+sw > 0
    {
        double sum_ratio = 0.0; int cnt = 0;
        size_t sz = std::min(long_weight.size(), short_weight.size());
        for (size_t i = 0; i < sz; ++i) {
            double lw = long_weight[i];
            double sw = short_weight[i];
            if (std::isfinite(lw) && std::isfinite(sw)) {
                double total_w = lw + sw;
                if (total_w > 0.0) {
                    sum_ratio += lw / total_w;
                    ++cnt;
                }
            }
        }
        if (cnt > 0) {
            m.long_ratio = sum_ratio / cnt;
            m.short_ratio = 1.0 - m.long_ratio;
        }
    }

    return m;
}
