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
    j["minBet"]    = m.min_bet;            // count (symbols)
    j["pnl"]       = m.pnl * 100;          // fraction → pp
    j["winRate"]   = m.win_rate * 100;     // fraction → pp
    j["margin"]    = m.margin;
    j["longRatio"] = m.long_ratio;
    j["shortRatio"]= m.short_ratio;
    j["nonzeroPosition"] = m.nonzero_position;
    j["avgMaxBet"]  = m.avg_max_bet * 100; // fraction → pp
    j["minBetMin"]  = m.min_bet_min;       // count (symbols)
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
    for (int v : ts.nonzero_positions)
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
    const std::vector<std::string>& symbols)
{
    Json::Value j;

    Json::Value ts_arr(Json::arrayValue);
    for (int64_t ms : timestamps)
        ts_arr.append(ms_to_date(ms));
    j["timestamps"] = ts_arr;

    Json::Value sym_arr(Json::arrayValue);
    for (const auto& s : symbols)
        sym_arr.append(s);
    j["symbols"] = sym_arr;

    // T×N matrix: rows = timestamps, cols = symbols
    Json::Value values(Json::arrayValue);
    for (const auto& row : signals) {
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
    const std::vector<std::vector<float>>& signals)
{
    // Collect all finite signal values
    std::vector<float> all;
    for (const auto& row : signals)
        for (float v : row)
            if (std::isfinite(v) && std::fabs(v) > 1e-12f)
                all.push_back(v);

    Json::Value j;
    if (all.empty()) {
        j["bins"] = Json::Value(Json::arrayValue);
        j["counts"] = Json::Value(Json::arrayValue);
        j["min"] = 0.0;
        j["max"] = 0.0;
        return j;
    }

    float vmin = *std::min_element(all.begin(), all.end());
    float vmax = *std::max_element(all.begin(), all.end());
    j["min"] = vmin;
    j["max"] = vmax;

    const int nbins = 50;
    float range = vmax - vmin;
    if (range < 1e-12f) range = 1.0f;
    float bin_width = range / nbins;

    std::vector<int> counts(nbins, 0);
    for (float v : all) {
        int idx = static_cast<int>((v - vmin) / bin_width);
        if (idx >= nbins) idx = nbins - 1;
        if (idx < 0) idx = 0;
        counts[idx]++;
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

