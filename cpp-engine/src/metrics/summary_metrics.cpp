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

    // Collect finite returns, skipping first bar (index 0)
    std::vector<double> finite_r;
    finite_r.reserve(ret.size());
    for (size_t i = 1; i < ret.size(); ++i) {
        if (std::isfinite(ret[i])) finite_r.push_back(ret[i]);
    }
    if (finite_r.empty()) return m;

    const size_t n = finite_r.size();

    // Equity curve
    std::vector<double> equity(n);
    equity[0] = 1.0 + finite_r[0];
    for (size_t i = 1; i < n; ++i)
        equity[i] = equity[i - 1] * (1.0 + finite_r[i]);

    // pnl
    m.pnl = equity.back() - 1.0;

    // cagr
    m.cagr = std::pow(equity.back(), static_cast<double>(bars_per_year) / static_cast<double>(std::max(n, size_t(1)))) - 1.0;

    // sharpe
    double sum_r = 0.0;
    for (double v : finite_r) sum_r += v;
    double mean_r = sum_r / static_cast<double>(n);

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

    // mdd
    double peak = equity[0];
    double max_dd = 0.0;
    for (size_t i = 0; i < n; ++i) {
        if (equity[i] > peak) peak = equity[i];
        double dd = equity[i] / (peak + 1e-12) - 1.0;
        if (dd < max_dd) max_dd = dd;
    }
    m.mdd = max_dd;

    // turnover: mean of finite values, skipping first bar
    {
        double sum = 0.0; int cnt = 0;
        for (size_t i = 1; i < turnover.size(); ++i) {
            if (std::isfinite(turnover[i])) { sum += turnover[i]; ++cnt; }
        }
        m.turnover = (cnt > 0) ? sum / cnt : 0.0;
    }

    // min_bet: mean of finite values, skipping first bar
    {
        double sum = 0.0; int cnt = 0;
        for (size_t i = 1; i < min_bet.size(); ++i) {
            if (std::isfinite(min_bet[i])) { sum += min_bet[i]; ++cnt; }
        }
        m.min_bet = (cnt > 0) ? sum / cnt : 0.0;
    }

    // max_weight: max of finite values, skipping first bar
    {
        double mx = 0.0;
        for (size_t i = 1; i < max_weight.size(); ++i) {
            if (std::isfinite(max_weight[i]) && max_weight[i] > mx)
                mx = max_weight[i];
        }
        m.max_weight = mx;
    }

    // nonzero_position: count(nonzero_count > 0), skipping first bar
    {
        int cnt = 0;
        for (size_t i = 1; i < nonzero_count.size(); ++i) {
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
        for (size_t i = 1; i < sz; ++i) {
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
