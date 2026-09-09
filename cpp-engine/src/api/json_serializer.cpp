#include "json_serializer.h"
#include <cmath>
#include <algorithm>
#include <ctime>

static std::string ms_to_date(int64_t ms) {
    time_t sec = ms / 1000;
    struct tm t;
    gmtime_r(&sec, &t);
    char buf[11];
    strftime(buf, sizeof(buf), "%Y-%m-%d", &t);
    return buf;
}

Json::Value summary_to_json(const SummaryMetrics& m) {
    Json::Value j;
    j["return"]    = m.ann_ret * 100;      // fraction → pp
    j["sharpe"]    = m.sharpe;
    j["mdd"]       = m.mdd * 100;          // fraction → pp
    j["turnover"]  = m.turnover * 100;     // fraction → pp
    j["maxWeight"] = m.max_weight * 100;   // fraction → pp
    j["minBet"]    = m.avg_position_count; // legacy key: average held-symbol count
    j["pnl"]       = m.pnl * 100;          // fraction → pp
    j["winRate"]   = m.win_rate * 100;     // fraction → pp
    j["margin"]    = m.margin;
    j["longRatio"] = m.long_ratio;
    j["shortRatio"]= m.short_ratio;
    j["nonzeroPosition"] = m.active_bar_count;
    j["avgMaxBet"]  = m.avg_max_bet * 100; // fraction → pp
    j["minBetMin"]  = m.min_position_count; // legacy key: minimum held-symbol count
    j["quality"]    = m.quality;
    j["sharpe400"]  = m.sharpe400;
    j["sharpe80"]   = m.sharpe80;
    j["minsoc"]     = m.minsoc * 100;      // fraction → pp
    return j;
}

Json::Value yearly_to_json(const std::vector<YearlyMetrics>& yearly) {
    Json::Value arr(Json::arrayValue);
    for (const auto& y : yearly) {
        Json::Value j;
        j["year"]     = y.year;
        j["longSM"]   = y.long_sm;
        j["shortSM"]  = y.short_sm;
        j["pnl"]      = y.pnl_pct;
        j["annRet"]   = y.ann_ret;
        j["sharpe"]   = y.sharpe;
        j["maxDD"]    = y.max_dd;
        j["dailyTvr"] = y.daily_tvr;
        j["margin"]   = y.margin;
        arr.append(j);
    }
    return arr;
}

Json::Value timeseries_to_json(const TimeseriesOutput& ts) {
    Json::Value j;

    Json::Value timestamps(Json::arrayValue);
    for (int64_t ms : ts.timestamps)
        timestamps.append(ms_to_date(ms));
    j["timestamps"] = timestamps;

    Json::Value cum_pnl(Json::arrayValue);
    for (double v : ts.cum_pnl)
        cum_pnl.append(v);
    j["cumPnl"] = cum_pnl;

    Json::Value cum_pnl_gross(Json::arrayValue);
    for (double v : ts.cum_pnl_gross)
        cum_pnl_gross.append(v);
    j["cumPnlGross"] = cum_pnl_gross;

    Json::Value turnover(Json::arrayValue);
    for (double v : ts.turnover_pct)
        turnover.append(v);
    j["turnover"] = turnover;

    Json::Value max_w(Json::arrayValue);
    for (double v : ts.max_weight_pct)
        max_w.append(v);
    j["maxWeight"] = max_w;

    Json::Value nz(Json::arrayValue);
    for (int v : ts.position_count)
        nz.append(v);
    j["nonzeroCount"] = nz;

    Json::Value dd(Json::arrayValue);
    for (double v : ts.drawdown)
        dd.append(v);
    j["drawdown"] = dd;

    return j;
}

Json::Value signal_plot_to_json(
    const std::vector<std::vector<float>>& signals,
    const std::vector<int64_t>& timestamps,
    const std::vector<std::string>& symbols,
    std::size_t start_index)
{
    Json::Value j;

    Json::Value ts_arr(Json::arrayValue);
    for (std::size_t di = start_index; di < timestamps.size(); ++di)
        ts_arr.append(ms_to_date(timestamps[di]));
    j["timestamps"] = ts_arr;

    Json::Value sym_arr(Json::arrayValue);
    for (const auto& s : symbols)
        sym_arr.append(s);
    j["symbols"] = sym_arr;

    // Rows from start_index: rows = timestamps, cols = symbols.
    Json::Value values(Json::arrayValue);
    for (std::size_t di = start_index; di < signals.size(); ++di) {
        const auto& row = signals[di];
        Json::Value row_arr(Json::arrayValue);
        for (float v : row) {
            if (std::isfinite(v))
                row_arr.append(v);
            else
                row_arr.append(Json::nullValue);
        }
        values.append(row_arr);
    }
    j["values"] = values;

    return j;
}

Json::Value signal_distribution_to_json(
    const std::vector<std::vector<float>>& signals,
    std::size_t start_index)
{
    // First pass: bounds of the same finite, nonzero values used by the histogram.
    float vmin = 0.0f;
    float vmax = 0.0f;
    bool has_values = false;
    for (std::size_t di = start_index; di < signals.size(); ++di) {
        for (float v : signals[di]) {
            if (!std::isfinite(v) || std::fabs(v) <= 1e-12f)
                continue;
            if (!has_values) {
                vmin = vmax = v;
                has_values = true;
            } else {
                vmin = std::min(vmin, v);
                vmax = std::max(vmax, v);
            }
        }
    }

    Json::Value j;
    if (!has_values) {
        j["bins"] = Json::Value(Json::arrayValue);
        j["counts"] = Json::Value(Json::arrayValue);
        j["min"] = 0.0;
        j["max"] = 0.0;
        return j;
    }

    j["min"] = vmin;
    j["max"] = vmax;

    const int nbins = 50;
    float range = vmax - vmin;
    if (range < 1e-12f) range = 1.0f;
    float bin_width = range / nbins;

    std::vector<int> counts(nbins, 0);
    // Second pass: count in place; no flattened copy of T×N signal values.
    for (std::size_t di = start_index; di < signals.size(); ++di) {
        for (float v : signals[di]) {
            if (!std::isfinite(v) || std::fabs(v) <= 1e-12f)
                continue;
            int idx = static_cast<int>((v - vmin) / bin_width);
            if (idx >= nbins) idx = nbins - 1;
            if (idx < 0) idx = 0;
            counts[idx]++;
        }
    }

    Json::Value bins_arr(Json::arrayValue);
    Json::Value counts_arr(Json::arrayValue);
    for (int i = 0; i < nbins; ++i) {
        bins_arr.append(static_cast<double>(vmin + bin_width * (i + 0.5f)));
        counts_arr.append(counts[i]);
    }
    j["bins"] = bins_arr;
    j["counts"] = counts_arr;

    return j;
}

