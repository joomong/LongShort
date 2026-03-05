#include "summary_metrics.h"
#include <cmath>
#include <algorithm>
#include <numeric>
#include <limits>

SummaryMetrics compute_summary_range(
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& min_bet,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& nonzero_count,
    size_t start,
    size_t end,
    unsigned bars_per_year)
{
    SummaryMetrics m{};
    if (start >= end)
        return m;

    // Collect finite returns (caller guarantees no warmup bar in input)
    std::vector<double> finite_r;
    const size_t ret_end = std::min(end, ret.size());
    if (start >= ret_end)
        return m;
    finite_r.reserve(ret_end - start);
    for (size_t i = start; i < ret_end; ++i) {
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
        const size_t turnover_end = std::min(end, turnover.size());
        for (size_t i = start; i < turnover_end; ++i) {
            if (std::isfinite(turnover[i])) { sum += turnover[i]; ++cnt; }
        }
        m.turnover = (cnt > 0) ? sum / cnt : 0.0;
    }

    // min_bet: average number of symbols with position
    {
        double sum = 0.0; int cnt = 0;
        const size_t nz_end = std::min(end, nonzero_count.size());
        for (size_t i = start; i < nz_end; ++i) {
            sum += nonzero_count[i]; ++cnt;
        }
        m.min_bet = (cnt > 0) ? sum / cnt : 0.0;
    }

    // max_weight: max of finite values
    {
        double mx = 0.0;
        const size_t max_weight_end = std::min(end, max_weight.size());
        for (size_t i = start; i < max_weight_end; ++i) {
            if (std::isfinite(max_weight[i]) && max_weight[i] > mx)
                mx = max_weight[i];
        }
        m.max_weight = mx;
    }

    // nonzero_position: count(nonzero_count > 0)
    {
        int cnt = 0;
        const size_t nz_end = std::min(end, nonzero_count.size());
        for (size_t i = start; i < nz_end; ++i) {
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
        size_t sz = std::min(std::min(long_weight.size(), short_weight.size()), end);
        for (size_t i = start; i < sz; ++i) {
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

    // avg_max_bet: mean of max_weight series (finite values)
    {
        double sum = 0.0; int cnt = 0;
        const size_t max_weight_end = std::min(end, max_weight.size());
        for (size_t i = start; i < max_weight_end; ++i) {
            if (std::isfinite(max_weight[i])) { sum += max_weight[i]; ++cnt; }
        }
        m.avg_max_bet = (cnt > 0) ? sum / cnt : 0.0;
    }

    // min_bet_min: minimum number of symbols with position
    {
        int mn = std::numeric_limits<int>::max();
        bool found = false;
        const size_t nz_end = std::min(end, nonzero_count.size());
        for (size_t i = start; i < nz_end; ++i) {
            if (nonzero_count[i] < mn) { mn = nonzero_count[i]; found = true; }
        }
        m.min_bet_min = found ? static_cast<double>(mn) : 0.0;
    }

    // quality: sharpe * sqrt(abs(1 - maxexp)) * (1 - max_weight)
    // maxexp = max(abs(long_weight - short_weight))
    {
        double maxexp = 0.0;
        size_t sz = std::min(std::min(long_weight.size(), short_weight.size()), end);
        for (size_t i = start; i < sz; ++i) {
            if (std::isfinite(long_weight[i]) && std::isfinite(short_weight[i])) {
                double diff = std::fabs(long_weight[i] - short_weight[i]);
                if (diff > maxexp) maxexp = diff;
            }
        }
        m.quality = m.sharpe * std::sqrt(std::fabs(1.0 - maxexp)) * (1.0 - m.max_weight);
    }

    // sharpe400: sharpe over last min(400, n) bars
    {
        size_t window = std::min<size_t>(400, finite_r.size());
        size_t start = finite_r.size() - window;
        if (window >= 2) {
            double s = 0.0;
            for (size_t i = start; i < finite_r.size(); ++i) s += finite_r[i];
            double mean_w = s / static_cast<double>(window);
            double sq = 0.0;
            for (size_t i = start; i < finite_r.size(); ++i) {
                double d = finite_r[i] - mean_w;
                sq += d * d;
            }
            double std_w = std::sqrt(sq / static_cast<double>(window - 1));
            m.sharpe400 = (std_w > 0.0) ? (mean_w / (std_w + 1e-12)) * std::sqrt(static_cast<double>(bars_per_year)) : 0.0;
        }
    }

    // sharpe80: sharpe over last min(80, n) bars
    {
        size_t window = std::min<size_t>(80, finite_r.size());
        size_t start = finite_r.size() - window;
        if (window >= 2) {
            double s = 0.0;
            for (size_t i = start; i < finite_r.size(); ++i) s += finite_r[i];
            double mean_w = s / static_cast<double>(window);
            double sq = 0.0;
            for (size_t i = start; i < finite_r.size(); ++i) {
                double d = finite_r[i] - mean_w;
                sq += d * d;
            }
            double std_w = std::sqrt(sq / static_cast<double>(window - 1));
            m.sharpe80 = (std_w > 0.0) ? (mean_w / (std_w + 1e-12)) * std::sqrt(static_cast<double>(bars_per_year)) : 0.0;
        }
    }

    // minsoc: minimum of (long_weight + short_weight) across bars
    {
        double mn = std::numeric_limits<double>::max();
        bool found = false;
        size_t sz = std::min(std::min(long_weight.size(), short_weight.size()), end);
        for (size_t i = start; i < sz; ++i) {
            if (std::isfinite(long_weight[i]) && std::isfinite(short_weight[i])) {
                double soc = long_weight[i] + short_weight[i];
                if (soc < mn) mn = soc;
                found = true;
            }
        }
        m.minsoc = found ? mn : 0.0;
    }

    return m;
}

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
    return compute_summary_range(
        ret, turnover, min_bet, max_weight, long_weight, short_weight, nonzero_count,
        0, ret.size(), bars_per_year);
}
