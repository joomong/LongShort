#include <gtest/gtest.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <numeric>
#include <vector>

#include "simulator_engine.h"

namespace {

struct OracleSummary {
    double pnl = 0.0;
    double ann_ret = 0.0;
    double sharpe = 0.0;
    double mdd = 0.0;
    double turnover = 0.0;
    double avg_position_count = 0.0;
    double max_weight = 0.0;
    int active_bar_count = 0;
    double win_rate = 0.0;
    double margin = 0.0;
    double long_ratio = 0.0;
    double short_ratio = 0.0;
    double avg_max_bet = 0.0;
    double min_position_count = 0.0;
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
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& position_count,
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
    // avg_position_count: average number of symbols with position
    {
        double sum = 0.0;
        for (int c : position_count) sum += c;
        out.avg_position_count = position_count.empty() ? 0.0 : sum / static_cast<double>(position_count.size());
    }
    out.max_weight = max_finite(max_weight);

    int nz = 0;
    for (int c : position_count) {
        if (c > 0)
            ++nz;
    }
    out.active_bar_count = nz;

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
    // min_position_count: minimum number of symbols with position
    {
        int mn = std::numeric_limits<int>::max();
        bool found = false;
        for (int c : position_count) {
            if (c < mn) { mn = c; found = true; }
        }
        out.min_position_count = found ? static_cast<double>(mn) : 0.0;
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
    EXPECT_NEAR(actual.avg_position_count, expected.avg_position_count, tol);
    EXPECT_NEAR(actual.max_weight, expected.max_weight, tol);
    EXPECT_EQ(actual.active_bar_count, expected.active_bar_count);
    EXPECT_NEAR(actual.win_rate, expected.win_rate, tol);
    EXPECT_NEAR(actual.margin, expected.margin, tol);
    EXPECT_NEAR(actual.long_ratio, expected.long_ratio, tol);
    EXPECT_NEAR(actual.short_ratio, expected.short_ratio, tol);
    EXPECT_NEAR(actual.avg_max_bet, expected.avg_max_bet, tol);
    EXPECT_NEAR(actual.min_position_count, expected.min_position_count, tol);
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

    std::vector<double> max_weight(T, 0.0);
    std::vector<double> long_weight(T, 0.0);
    std::vector<double> short_weight(T, 0.0);
    std::vector<int> position_count(T, 0);

    for (size_t i = 0; i < T; ++i) {
        ret[i] = (((static_cast<int>(i) % 7) - 3) * 0.0015) + ((i % 5 == 0) ? 0.0007 : -0.0002);
        if (i % 17 == 0)
            ret[i] = nan;

        turnover[i] = 0.10 + 0.02 * static_cast<double>(i % 4);
        if (i % 19 == 0)
            turnover[i] = nan;


        max_weight[i] = 0.15 + 0.03 * static_cast<double>(i % 5);
        if (i % 11 == 0)
            max_weight[i] = nan;

        long_weight[i] = 0.48 + 0.06 * std::sin(static_cast<double>(i) * 0.2);
        short_weight[i] = 0.52 + 0.04 * std::cos(static_cast<double>(i) * 0.1);
        position_count[i] = (i % 6 == 0) ? 0 : 3;
    }

    const auto actual = compute_summary(
        ret, turnover, max_weight, long_weight, short_weight, position_count, 365);
    const auto expected = oracle_summary(
        ret, turnover, max_weight, long_weight, short_weight, position_count, 365);

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

    // Gross returns drive every performance metric; net = gross - turnover * fee.
    std::vector<double> ret = {0.0, 0.01, -0.002, 0.004, -0.003, 0.006, 0.001, -0.005, 0.008, 0.002, -0.001, 0.003};
    std::vector<double> turnover(T, 0.2);
    const double fee = 0.0005;
    std::vector<double> net(T);
    for (size_t i = 0; i < T; ++i) net[i] = ret[i] - turnover[i] * fee;

    std::vector<double> max_weight(T, 0.3);
    std::vector<double> long_weight(T, 0.55);
    std::vector<double> short_weight(T, 0.45);
    std::vector<int> position_count(T, 3);

    const int64_t is_ms = timestamps[2];
    const int64_t os_ms = timestamps[8];

    const auto actual = compute_full_metrics(
        timestamps, net, ret, turnover, max_weight, long_weight, short_weight, position_count,
        is_ms, os_ms, 365, 1);

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
        slice(max_weight, is_for_summary, os_idx),
        slice(long_weight, is_for_summary, os_idx),
        slice(short_weight, is_for_summary, os_idx),
        slice(position_count, is_for_summary, os_idx),
        365);

    const auto os_expected = oracle_summary(
        slice(ret, os_idx, T),
        slice(turnover, os_idx, T),
        slice(max_weight, os_idx, T),
        slice(long_weight, os_idx, T),
        slice(short_weight, os_idx, T),
        slice(position_count, os_idx, T),
        365);

    expect_summary_near(actual.is_summary, is_expected, 1e-9);
    expect_summary_near(actual.os_summary, os_expected, 1e-9);

    // The net series would give different metrics; the engine must not use it.
    const auto is_net = oracle_summary(
        slice(net, is_for_summary, os_idx), slice(turnover, is_for_summary, os_idx),
        slice(max_weight, is_for_summary, os_idx), slice(long_weight, is_for_summary, os_idx),
        slice(short_weight, is_for_summary, os_idx), slice(position_count, is_for_summary, os_idx),
        365);
    EXPECT_GT(std::fabs(actual.is_summary.pnl - is_net.pnl), 1e-6);
    EXPECT_GT(std::fabs(actual.is_summary.sharpe - is_net.sharpe), 1e-6);

    // Timeseries carries both cumulative series; drawdown follows gross.
    double cum_net = 0.0, cum_gross = 0.0, peak = 0.0, reported_net = 0.0, reported_gross = 0.0;
    ASSERT_EQ(actual.timeseries.cum_pnl.size(), T - 1);
    for (size_t i = 1; i < T; ++i) {
        cum_net += net[i];
        cum_gross += ret[i];
        if (i >= 2) { reported_net += net[i]; reported_gross += ret[i]; }
        peak = std::max(peak, cum_gross);
        EXPECT_NEAR(actual.timeseries.cum_pnl[i - 1], cum_net, 1e-12);
        EXPECT_NEAR(actual.timeseries.cum_pnl_gross[i - 1], cum_gross, 1e-12);
        EXPECT_NEAR(actual.timeseries.drawdown[i - 1], cum_gross - peak, 1e-12);
    }
    // Yearly (single year 2024, from IS) pnl is gross in percent.
    ASSERT_EQ(actual.yearly.size(), 1u);
    EXPECT_NEAR(actual.yearly[0].pnl_pct, reported_gross * 100.0, 1e-9);
    EXPECT_GT(std::fabs(actual.yearly[0].pnl_pct - reported_net * 100.0), 1e-6);
}

TEST(MetricsOracle, SingleDayInputIncludesItsTradingBar)
{
    const int64_t t0 = 1704067200000LL;
    std::vector<int64_t> timestamps = {t0};
    std::vector<double> ret = {0.0};
    std::vector<double> turnover = {0.0};

    std::vector<double> max_weight = {0.0};
    std::vector<double> long_weight = {0.0};
    std::vector<double> short_weight = {0.0};
    std::vector<int> position_count = {0};

    const auto result = compute_full_metrics(
        timestamps, ret, ret, turnover, max_weight, long_weight, short_weight, position_count,
        t0, t0 + 86400000LL, 365);

    EXPECT_EQ(result.timeseries.timestamps.size(), 1u);
    EXPECT_EQ(result.yearly.size(), 1u);
    EXPECT_DOUBLE_EQ(result.is_summary.pnl, 0.0);
    EXPECT_DOUBLE_EQ(result.os_summary.pnl, 0.0);
}


TEST(MetricsOracle, DrawdownIncludesLossFromTheInitialBook) {
    const std::vector<int64_t> dates={0,86400000,172800000,259200000};
    const std::vector<double> ret={0,-0.10,0.05,-0.10}, zeros(4,0.0);
    const std::vector<int> counts(4,0);
    auto m=compute_full_metrics(dates,ret,ret,zeros,zeros,zeros,zeros,counts,
                                0,10LL*86400000,365);
    EXPECT_NEAR(m.is_summary.mdd,-0.15,1e-12);
    ASSERT_EQ(m.yearly.size(),1u);
    EXPECT_NEAR(m.yearly[0].max_dd,-15.0,1e-12);
    ASSERT_EQ(m.timeseries.drawdown.size(),4u);
    EXPECT_DOUBLE_EQ(m.timeseries.drawdown[0],0.0);
    EXPECT_NEAR(m.timeseries.drawdown[1],-0.10,1e-12);
    EXPECT_NEAR(*std::min_element(m.timeseries.drawdown.begin(),m.timeseries.drawdown.end()),
                m.is_summary.mdd,1e-12);
}

TEST(MetricsOracle, InSampleStartAfterDataDoesNotEvaluateTheWholeHistory) {
    const std::vector<int64_t> dates={0,86400000,172800000};
    const std::vector<double> ret={0,0.1,-0.2}, zeros(3,0.0);
    const std::vector<int> counts(3,0);
    auto m=compute_full_metrics(dates,ret,ret,zeros,zeros,zeros,zeros,counts,
                                10LL*86400000,20LL*86400000,365);
    EXPECT_DOUBLE_EQ(m.is_summary.pnl,0.0);
    EXPECT_DOUBLE_EQ(m.is_summary.sharpe,0.0);
    EXPECT_EQ(m.is_start_index,m.timeseries.timestamps.size());
    EXPECT_EQ(m.os_start_index,m.timeseries.timestamps.size());
}


TEST(MetricsOracle, TradingSpanExcludesWarmupAndIncludesFirstEntryFee) {
    const int64_t day = 86400000LL;
    const int64_t dec30 = 1703894400000LL;
    const std::vector<int64_t> dates = {dec30, dec30+day, dec30+2*day, dec30+3*day, dec30+4*day};
    const std::vector<double> ret = {0.7, -0.9, -0.001, 0.1, -0.2};     // net
    const std::vector<double> gross = {0.8, -0.8, 0.0, 0.1, -0.2};
    const std::vector<double> turnover = {99, 99, 1, 0, 0};
    const std::vector<double> weights(5, 1), shorts(5, 0);
    const std::vector<int> counts(5, 1);
    const auto metrics = compute_full_metrics(dates, ret, gross, turnover, weights, weights, shorts, counts,
        dates[0], dates[4], 252, 2);
    EXPECT_EQ(metrics.is_start_index, 0u);
    EXPECT_EQ(metrics.os_start_index, 2u);
    EXPECT_NEAR(metrics.is_summary.pnl, 0.1, 1e-12);        // gross; net would be 0.099
    EXPECT_NEAR(metrics.is_summary.ann_ret, 0.05 * 252, 1e-12);
    EXPECT_NEAR(metrics.os_summary.pnl, -0.2, 1e-12);
    ASSERT_EQ(metrics.timeseries.timestamps.size(), 3u);
    EXPECT_EQ(metrics.timeseries.timestamps.front(), dates[2]);
    EXPECT_DOUBLE_EQ(metrics.timeseries.cum_pnl.front(), -0.001);
    EXPECT_DOUBLE_EQ(metrics.timeseries.cum_pnl_gross.front(), 0.0);
    EXPECT_NEAR(metrics.timeseries.cum_pnl.back(), -0.101, 1e-12);
    ASSERT_EQ(metrics.yearly.size(), 1u);
    EXPECT_EQ(metrics.yearly[0].year, 2024);
    EXPECT_NEAR(metrics.yearly[0].pnl_pct, -10.0, 1e-12);   // gross; net would be -10.1
}

TEST(MetricsOracle, OutOfSampleStartBeforeTradingSpanDoesNotIncludeWarmup) {
    const std::vector<int64_t> dates = {0, 86400000, 172800000, 259200000};
    const std::vector<double> ret = {5, 5, -0.1, 0.2}, zeros(4, 0);
    const std::vector<int> counts(4, 0);
    const auto metrics = compute_full_metrics(dates, ret, ret, zeros, zeros, zeros, zeros, counts,
        -86400000, 0, 365, 2);
    EXPECT_EQ(metrics.is_start_index, 0u);
    EXPECT_EQ(metrics.os_start_index, 0u);
    EXPECT_DOUBLE_EQ(metrics.is_summary.pnl, 0.0);
    EXPECT_NEAR(metrics.os_summary.pnl, 0.1, 1e-12);
}
