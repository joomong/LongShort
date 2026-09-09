#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <numeric>
#include <vector>

#include "metrics/metrics_engine.h"
#include "metrics/summary_metrics.h"

namespace {

struct OracleSummary {
    double pnl = 0.0;
    double ann_ret = 0.0;
    double sharpe = 0.0;
    double mdd = 0.0;
    double turnover = 0.0;
    double min_bet = 0.0;
    double max_weight = 0.0;
    int nonzero_position = 0;
    double win_rate = 0.0;
    double margin = 0.0;
    double long_ratio = 0.0;
    double short_ratio = 0.0;
    double avg_max_bet = 0.0;
    double min_bet_min = 0.0;
    double quality = 0.0;
    double sharpe400 = 0.0;
    double sharpe80 = 0.0;
    double minsoc = 0.0;
};

double mean_finite(const std::vector<double>& v)
{
    double sum = 0.0;
    int cnt = 0;
    for (double x : v) {
        if (std::isfinite(x)) {
            sum += x;
            ++cnt;
        }
    }
    return (cnt > 0) ? (sum / static_cast<double>(cnt)) : 0.0;
}

double max_finite(const std::vector<double>& v)
{
    double mx = 0.0;
    for (double x : v) {
        if (std::isfinite(x) && x > mx)
            mx = x;
    }
    return mx;
}

double min_finite_or_zero(const std::vector<double>& v)
{
    double mn = std::numeric_limits<double>::max();
    bool found = false;
    for (double x : v) {
        if (std::isfinite(x)) {
            mn = std::min(mn, x);
            found = true;
        }
    }
    return found ? mn : 0.0;
}

double sharpe_of(const std::vector<double>& r, unsigned bars_per_year)
{
    if (r.size() < 2)
        return 0.0;
    double mean = std::accumulate(r.begin(), r.end(), 0.0) / static_cast<double>(r.size());
    double sq_sum = 0.0;
    for (double x : r) {
        double d = x - mean;
        sq_sum += d * d;
    }
    double std = std::sqrt(sq_sum / static_cast<double>(r.size() - 1));
    if (std <= 0.0)
        return 0.0;
    return (mean / (std + 1e-12)) * std::sqrt(static_cast<double>(bars_per_year));
}

OracleSummary oracle_summary(
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& min_bet,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& nonzero_count,
    unsigned bars_per_year)
{
    OracleSummary out;

    std::vector<double> finite_r;
    finite_r.reserve(ret.size());
    for (double x : ret) {
        if (std::isfinite(x))
            finite_r.push_back(x);
    }
    if (finite_r.empty())
        return out;

    std::vector<double> cum(finite_r.size(), 0.0);
    cum[0] = finite_r[0];
    for (size_t i = 1; i < finite_r.size(); ++i)
        cum[i] = cum[i - 1] + finite_r[i];

    out.pnl = cum.back();

    const double mean_r = std::accumulate(finite_r.begin(), finite_r.end(), 0.0) / static_cast<double>(finite_r.size());
    out.ann_ret = mean_r * static_cast<double>(bars_per_year);
    out.sharpe = sharpe_of(finite_r, bars_per_year);

    double peak = 0.0;
    double mdd = 0.0;
    for (double c : cum) {
        peak = std::max(peak, c);
        mdd = std::min(mdd, c - peak);
    }
    out.mdd = mdd;

    out.turnover = mean_finite(turnover);
    // min_bet: average number of symbols with position
    {
        double sum = 0.0;
        for (int c : nonzero_count) sum += c;
        out.min_bet = nonzero_count.empty() ? 0.0 : sum / static_cast<double>(nonzero_count.size());
    }
    out.max_weight = max_finite(max_weight);

    int nz = 0;
    for (int c : nonzero_count) {
        if (c > 0)
            ++nz;
    }
    out.nonzero_position = nz;

    int wins = 0;
    for (double x : finite_r) {
        if (x > 0.0)
            ++wins;
    }
    out.win_rate = static_cast<double>(wins) / static_cast<double>(finite_r.size());
    out.margin = mean_r / (out.turnover + 1e-12);

    {
        double sum_ratio = 0.0;
        int cnt = 0;
        const size_t n = std::min(long_weight.size(), short_weight.size());
        for (size_t i = 0; i < n; ++i) {
            double lw = long_weight[i];
            double sw = short_weight[i];
            if (!std::isfinite(lw) || !std::isfinite(sw))
                continue;
            double total = lw + sw;
            if (total <= 0.0)
                continue;
            sum_ratio += lw / total;
            ++cnt;
        }
        if (cnt > 0) {
            out.long_ratio = sum_ratio / static_cast<double>(cnt);
            out.short_ratio = 1.0 - out.long_ratio;
        }
    }

    out.avg_max_bet = mean_finite(max_weight);
    // min_bet_min: minimum number of symbols with position
    {
        int mn = std::numeric_limits<int>::max();
        bool found = false;
        for (int c : nonzero_count) {
            if (c < mn) { mn = c; found = true; }
        }
        out.min_bet_min = found ? static_cast<double>(mn) : 0.0;
    }

    {
        double maxexp = 0.0;
        const size_t n = std::min(long_weight.size(), short_weight.size());
        for (size_t i = 0; i < n; ++i) {
            double lw = long_weight[i];
            double sw = short_weight[i];
            if (!std::isfinite(lw) || !std::isfinite(sw))
                continue;
            maxexp = std::max(maxexp, std::fabs(lw - sw));
        }
        out.quality = out.sharpe * std::sqrt(std::fabs(1.0 - maxexp)) * (1.0 - out.max_weight);
    }

    {
        size_t w = std::min<size_t>(400, finite_r.size());
        std::vector<double> tail(finite_r.end() - static_cast<std::ptrdiff_t>(w), finite_r.end());
        out.sharpe400 = sharpe_of(tail, bars_per_year);
    }

    {
        size_t w = std::min<size_t>(80, finite_r.size());
        std::vector<double> tail(finite_r.end() - static_cast<std::ptrdiff_t>(w), finite_r.end());
        out.sharpe80 = sharpe_of(tail, bars_per_year);
    }

    {
        double mn = std::numeric_limits<double>::max();
        bool found = false;
        const size_t n = std::min(long_weight.size(), short_weight.size());
        for (size_t i = 0; i < n; ++i) {
            double lw = long_weight[i];
            double sw = short_weight[i];
            if (!std::isfinite(lw) || !std::isfinite(sw))
                continue;
            mn = std::min(mn, lw + sw);
            found = true;
        }
        out.minsoc = found ? mn : 0.0;
    }

    return out;
}

template <typename T>
std::vector<T> slice(const std::vector<T>& v, size_t a, size_t b)
{
    if (a >= v.size())
        return {};
    const size_t e = std::min(v.size(), b);
    return std::vector<T>(v.begin() + static_cast<std::ptrdiff_t>(a), v.begin() + static_cast<std::ptrdiff_t>(e));
}

void expect_summary_near(const SummaryMetrics& actual, const OracleSummary& expected, double tol)
{
    EXPECT_NEAR(actual.pnl, expected.pnl, tol);
    EXPECT_NEAR(actual.ann_ret, expected.ann_ret, tol);
    EXPECT_NEAR(actual.sharpe, expected.sharpe, tol);
    EXPECT_NEAR(actual.mdd, expected.mdd, tol);
    EXPECT_NEAR(actual.turnover, expected.turnover, tol);
    EXPECT_NEAR(actual.min_bet, expected.min_bet, tol);
    EXPECT_NEAR(actual.max_weight, expected.max_weight, tol);
    EXPECT_EQ(actual.nonzero_position, expected.nonzero_position);
    EXPECT_NEAR(actual.win_rate, expected.win_rate, tol);
    EXPECT_NEAR(actual.margin, expected.margin, tol);
    EXPECT_NEAR(actual.long_ratio, expected.long_ratio, tol);
    EXPECT_NEAR(actual.short_ratio, expected.short_ratio, tol);
    EXPECT_NEAR(actual.avg_max_bet, expected.avg_max_bet, tol);
    EXPECT_NEAR(actual.min_bet_min, expected.min_bet_min, tol);
    EXPECT_NEAR(actual.quality, expected.quality, tol);
    EXPECT_NEAR(actual.sharpe400, expected.sharpe400, tol);
    EXPECT_NEAR(actual.sharpe80, expected.sharpe80, tol);
    EXPECT_NEAR(actual.minsoc, expected.minsoc, tol);
}

}  // namespace

TEST(MetricsOracle, SummaryMatchesReferenceImplementation)
{
    const size_t T = 140;
    const double nan = std::numeric_limits<double>::quiet_NaN();

    std::vector<double> ret(T, 0.0);
    std::vector<double> turnover(T, 0.0);
    std::vector<double> min_bet(T, 0.0);
    std::vector<double> max_weight(T, 0.0);
    std::vector<double> long_weight(T, 0.0);
    std::vector<double> short_weight(T, 0.0);
    std::vector<int> nonzero_count(T, 0);

    for (size_t i = 0; i < T; ++i) {
        ret[i] = (((static_cast<int>(i) % 7) - 3) * 0.0015) + ((i % 5 == 0) ? 0.0007 : -0.0002);
        if (i % 17 == 0)
            ret[i] = nan;

        turnover[i] = 0.10 + 0.02 * static_cast<double>(i % 4);
        if (i % 19 == 0)
            turnover[i] = nan;

        min_bet[i] = 0.015 + 0.004 * static_cast<double>(i % 3);
        if (i % 13 == 0)
            min_bet[i] = nan;

        max_weight[i] = 0.15 + 0.03 * static_cast<double>(i % 5);
        if (i % 11 == 0)
            max_weight[i] = nan;

        long_weight[i] = 0.48 + 0.06 * std::sin(static_cast<double>(i) * 0.2);
        short_weight[i] = 0.52 + 0.04 * std::cos(static_cast<double>(i) * 0.1);
        nonzero_count[i] = (i % 6 == 0) ? 0 : 3;
    }

    const auto actual = compute_summary(
        ret, turnover, min_bet, max_weight, long_weight, short_weight, nonzero_count, 365);
    const auto expected = oracle_summary(
        ret, turnover, min_bet, max_weight, long_weight, short_weight, nonzero_count, 365);

    expect_summary_near(actual, expected, 1e-9);
}

TEST(MetricsOracle, FullMetricsSplitMatchesReferenceSlices)
{
    const size_t T = 12;
    const int64_t day_ms = 86400000LL;
    const int64_t t0 = 1704067200000LL;  // 2024-01-01 UTC

    std::vector<int64_t> timestamps(T, 0);
    for (size_t i = 0; i < T; ++i)
        timestamps[i] = t0 + static_cast<int64_t>(i) * day_ms;

    std::vector<double> ret = {0.0, 0.01, -0.002, 0.004, -0.003, 0.006, 0.001, -0.005, 0.008, 0.002, -0.001, 0.003};
    std::vector<double> turnover(T, 0.2);
    std::vector<double> min_bet(T, 0.02);
    std::vector<double> max_weight(T, 0.3);
    std::vector<double> long_weight(T, 0.55);
    std::vector<double> short_weight(T, 0.45);
    std::vector<int> nonzero_count(T, 3);

    const int64_t is_ms = timestamps[2];
    const int64_t os_ms = timestamps[8];

    const auto actual = compute_full_metrics(
        timestamps, ret, turnover, min_bet, max_weight, long_weight, short_weight, nonzero_count,
        is_ms, os_ms, 0.0005, 365);

    EXPECT_EQ(actual.is_start_index, 1u);
    EXPECT_EQ(actual.os_start_index, 7u);

    size_t is_idx = 0;
    size_t os_idx = T;
    for (size_t i = 0; i < T; ++i) {
        if (timestamps[i] >= is_ms) {
            is_idx = i;
            break;
        }
    }
    for (size_t i = 0; i < T; ++i) {
        if (timestamps[i] >= os_ms) {
            os_idx = i;
            break;
        }
    }
    const size_t is_for_summary = std::max<size_t>(is_idx, 1);

    const auto is_expected = oracle_summary(
        slice(ret, is_for_summary, os_idx),
        slice(turnover, is_for_summary, os_idx),
        slice(min_bet, is_for_summary, os_idx),
        slice(max_weight, is_for_summary, os_idx),
        slice(long_weight, is_for_summary, os_idx),
        slice(short_weight, is_for_summary, os_idx),
        slice(nonzero_count, is_for_summary, os_idx),
        365);

    const auto os_expected = oracle_summary(
        slice(ret, os_idx, T),
        slice(turnover, os_idx, T),
        slice(min_bet, os_idx, T),
        slice(max_weight, os_idx, T),
        slice(long_weight, os_idx, T),
        slice(short_weight, os_idx, T),
        slice(nonzero_count, os_idx, T),
        365);

    expect_summary_near(actual.is_summary, is_expected, 1e-9);
    expect_summary_near(actual.os_summary, os_expected, 1e-9);
}

TEST(MetricsOracle, SingleDayInputProducesEmptyTimeseries)
{
    const int64_t t0 = 1704067200000LL;
    std::vector<int64_t> timestamps = {t0};
    std::vector<double> ret = {0.0};
    std::vector<double> turnover = {0.0};
    std::vector<double> min_bet = {0.0};
    std::vector<double> max_weight = {0.0};
    std::vector<double> long_weight = {0.0};
    std::vector<double> short_weight = {0.0};
    std::vector<int> nonzero_count = {0};

    const auto result = compute_full_metrics(
        timestamps, ret, turnover, min_bet, max_weight, long_weight, short_weight, nonzero_count,
        t0, t0 + 86400000LL, 0.0, 365);

    EXPECT_TRUE(result.timeseries.timestamps.empty());
    EXPECT_TRUE(result.yearly.empty());
    EXPECT_DOUBLE_EQ(result.is_summary.pnl, 0.0);
    EXPECT_DOUBLE_EQ(result.os_summary.pnl, 0.0);
}


TEST(MetricsOracle, DrawdownIncludesLossFromTheInitialBook) {
    const std::vector<int64_t> dates={0,86400000,172800000,259200000};
    const std::vector<double> ret={0,-0.10,0.05,-0.10}, zeros(4,0.0);
    const std::vector<int> counts(4,0);
    auto m=compute_full_metrics(dates,ret,zeros,zeros,zeros,zeros,zeros,counts,
                                0,10LL*86400000,0,365);
    EXPECT_NEAR(m.is_summary.mdd,-0.15,1e-12);
    ASSERT_EQ(m.yearly.size(),1u);
    EXPECT_NEAR(m.yearly[0].max_dd,-15.0,1e-12);
    ASSERT_EQ(m.timeseries.drawdown.size(),3u);
    EXPECT_NEAR(m.timeseries.drawdown[0],-0.10,1e-12);
    EXPECT_NEAR(*std::min_element(m.timeseries.drawdown.begin(),m.timeseries.drawdown.end()),
                m.is_summary.mdd,1e-12);
}

TEST(MetricsOracle, InSampleStartAfterDataDoesNotEvaluateTheWholeHistory) {
    const std::vector<int64_t> dates={0,86400000,172800000};
    const std::vector<double> ret={0,0.1,-0.2}, zeros(3,0.0);
    const std::vector<int> counts(3,0);
    auto m=compute_full_metrics(dates,ret,zeros,zeros,zeros,zeros,zeros,counts,
                                10LL*86400000,20LL*86400000,0,365);
    EXPECT_DOUBLE_EQ(m.is_summary.pnl,0.0);
    EXPECT_DOUBLE_EQ(m.is_summary.sharpe,0.0);
    EXPECT_EQ(m.is_start_index,m.timeseries.timestamps.size());
    EXPECT_EQ(m.os_start_index,m.timeseries.timestamps.size());
}
