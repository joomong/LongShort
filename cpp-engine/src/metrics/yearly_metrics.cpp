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
    unsigned bars_per_year,
    std::size_t start_index)
{
    if (timestamps.empty()) return {};

    const size_t T = timestamps.size();

    // Warmup bars outside the requested trading span do not affect yearly metrics.
    std::map<int, std::vector<size_t>> year_indices;
    for (size_t i = std::min(start_index, T); i < T; ++i) {
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

        double sum_r = 0.0;
        double peak = 0.0;
        double max_drawdown = 0.0;
        for (double r : yr_ret) {
            sum_r += r;
            if (sum_r > peak) peak = sum_r;
            const double drawdown = sum_r - peak;
            if (drawdown < max_drawdown) max_drawdown = drawdown;
        }
        const double mean_r = yr_ret.empty() ? 0.0 : sum_r / static_cast<double>(yr_ret.size());
        ym.pnl_pct = sum_r * 100.0;
        ym.ann_ret = mean_r * static_cast<double>(bars_per_year) * 100.0;
        ym.max_dd = max_drawdown * 100.0;

        // sharpe
        if (yr_ret.size() >= 2) {
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
        ym.margin = mean_r / (ym.daily_tvr + 1e-12);

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
