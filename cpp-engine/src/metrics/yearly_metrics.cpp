#include "yearly_metrics.h"
#include <cmath>
#include <algorithm>
#include <map>
#include <ctime>

static int epoch_ms_to_year(int64_t epoch_ms) {
    time_t t = static_cast<time_t>(epoch_ms / 1000);
    struct tm tm_buf{};
#if defined(_WIN32)
    gmtime_s(&tm_buf, &t);
#else
    gmtime_r(&t, &tm_buf);
#endif
    return tm_buf.tm_year + 1900;
}

std::vector<YearlyMetrics> compute_yearly(
    const std::vector<int64_t>& timestamps,
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    unsigned bars_per_year)
{
    if (timestamps.empty()) return {};

    const size_t T = timestamps.size();

    // Build year -> list of indices, skipping index 0 (first bar convention)
    std::map<int, std::vector<size_t>> year_indices;
    for (size_t i = 1; i < T; ++i) {
        int yr = epoch_ms_to_year(timestamps[i]);
        year_indices[yr].push_back(i);
    }

    std::vector<YearlyMetrics> result;
    result.reserve(year_indices.size());

    for (auto& [yr, indices] : year_indices) {
        if (indices.empty()) continue;

        YearlyMetrics ym{};
        ym.year = yr;

        const size_t n = indices.size();

        // Collect returns for this year
        std::vector<double> yr_ret;
        yr_ret.reserve(n);
        for (size_t idx : indices) {
            if (idx < ret.size() && std::isfinite(ret[idx]))
                yr_ret.push_back(ret[idx]);
        }

        // Cumulative PnL (additive, STAR-style fixed book size)
        std::vector<double> cum_pnl;
        cum_pnl.reserve(yr_ret.size());
        double cp = 0.0;
        for (double r : yr_ret) {
            cp += r;
            cum_pnl.push_back(cp);
        }

        // pnl_pct = sum of daily returns * 100
        double yearly_pnl = cum_pnl.empty() ? 0.0 : cum_pnl.back();
        ym.pnl_pct = yearly_pnl * 100.0;

        // ann_ret = mean(daily_ret) * bars_per_year * 100
        if (!yr_ret.empty()) {
            double sum_r = 0.0;
            for (double v : yr_ret) sum_r += v;
            double mean_r = sum_r / static_cast<double>(yr_ret.size());
            ym.ann_ret = mean_r * static_cast<double>(bars_per_year) * 100.0;
        } else {
            ym.ann_ret = 0.0;
        }

        // sharpe
        if (yr_ret.size() >= 2) {
            double sum_r = 0.0;
            for (double v : yr_ret) sum_r += v;
            double mean_r = sum_r / static_cast<double>(yr_ret.size());
            double sq_sum = 0.0;
            for (double v : yr_ret) {
                double d = v - mean_r;
                sq_sum += d * d;
            }
            double std_r = std::sqrt(sq_sum / static_cast<double>(yr_ret.size() - 1));
            ym.sharpe = (std_r > 0.0)
                ? (mean_r / (std_r + 1e-12)) * std::sqrt(static_cast<double>(bars_per_year))
                : 0.0;
        } else {
            ym.sharpe = 0.0;
        }

        // max_dd (additive: peak cumPnL - current cumPnL)
        {
            double peak = cum_pnl.empty() ? 0.0 : cum_pnl[0];
            double mdd = 0.0;
            for (double c : cum_pnl) {
                if (c > peak) peak = c;
                double dd = c - peak;  // negative when in drawdown
                if (dd < mdd) mdd = dd;
            }
            ym.max_dd = mdd * 100.0;
        }

        // win_lose (%)
        {
            int wins = 0, total = 0;
            for (double v : yr_ret) {
                ++total;
                if (v > 0.0) ++wins;
            }
            ym.win_lose = (total > 0) ? static_cast<double>(wins) / total * 100.0 : 0.0;
        }

        // daily_tvr
        {
            double sum = 0.0; int cnt = 0;
            for (size_t idx : indices) {
                if (idx < turnover.size() && std::isfinite(turnover[idx])) {
                    sum += turnover[idx]; ++cnt;
                }
            }
            ym.daily_tvr = (cnt > 0) ? sum / cnt : 0.0;
        }

        // margin
        {
            double sum_r = 0.0;
            for (double v : yr_ret) sum_r += v;
            double mean_r = yr_ret.empty() ? 0.0 : sum_r / static_cast<double>(yr_ret.size());
            ym.margin = mean_r / (ym.daily_tvr + 1e-12);
        }

        // long_sm, short_sm
        {
            double lsum = 0.0, ssum = 0.0; int cnt = 0;
            for (size_t idx : indices) {
                bool lv = idx < long_weight.size() && std::isfinite(long_weight[idx]);
                bool sv = idx < short_weight.size() && std::isfinite(short_weight[idx]);
                if (lv && sv) {
                    lsum += long_weight[idx];
                    ssum += short_weight[idx];
                    ++cnt;
                }
            }
            ym.long_sm  = (cnt > 0) ? lsum / cnt : 0.0;
            ym.short_sm = (cnt > 0) ? ssum / cnt : 0.0;
        }

        result.push_back(ym);
    }

    return result;
}
