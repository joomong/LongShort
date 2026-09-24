#include "json_serializer.h"
#include <cmath>
#include <algorithm>
#include <ctime>
#include <stdexcept>

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
        j["winLose"]  = y.win_lose; // already percentage points
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

    Json::Value frozen(Json::arrayValue);
    for (double v : ts.frozen_gross)
        frozen.append(v);
    j["frozenGross"] = frozen;

    Json::Value settled(Json::arrayValue);
    for (double v : ts.settled_gross)
        settled.append(v);
    j["settledGross"] = settled;

    Json::Value dividends(Json::arrayValue);
    for (double v : ts.dividend_pnl)
        dividends.append(v);
    j["dividendPnl"] = dividends;

    Json::Value distributions(Json::arrayValue);
    for (double v : ts.distribution_pnl)
        distributions.append(v);
    j["distributionPnl"] = distributions;

    Json::Value borrow(Json::arrayValue);
    for (double v : ts.borrow_cost)
        borrow.append(v);
    j["borrowCost"] = borrow;

    Json::Value unconfirmed(Json::arrayValue);
    for (double v : ts.unconfirmed_weight)
        unconfirmed.append(v);
    j["unconfirmedWeight"] = unconfirmed;

    return j;
}

namespace {
struct SampledIndices {
    std::vector<std::size_t> indices;
    std::size_t stride = 1;
};

SampledIndices sample_indices(std::size_t begin, std::size_t end, std::size_t limit) {
    SampledIndices result;
    if (begin >= end)
        return result;
    const std::size_t count = end - begin;
    if (limit == 1) {
        result.indices.push_back(end - 1);  // always retain the latest observation
        result.stride = count;
        return result;
    }
    if (count > limit)
        result.stride = 1 + (count - 2) / (limit - 1);
    for (std::size_t i = begin; i < end - 1; i += result.stride)
        result.indices.push_back(i);
    result.indices.push_back(end - 1);
    return result;
}
}  // namespace

Json::Value signal_plot_to_json(
    const std::vector<std::vector<float>>& signals,
    const std::vector<int64_t>& timestamps,
    const std::vector<std::string>& symbols,
    std::size_t start_index,
    std::size_t max_cells,
    const std::vector<std::size_t>* symbol_subset)
{
    if (max_cells == 0)
        throw std::invalid_argument("Signal plot cell budget must be positive");
    if (signals.size() != timestamps.size())
        throw std::invalid_argument("Signal plot dates must match signal rows");
    SampledIndices columns;
    SampledIndices rows;
    if (symbol_subset) {
        for (std::size_t ii : *symbol_subset) {
            if (ii >= symbols.size())
                throw std::invalid_argument("Signal plot symbol subset is out of range");
            columns.indices.push_back(ii);
        }
        rows = sample_indices(start_index, signals.size(), signals.size());  // every row
    } else {
        columns = sample_indices(0, symbols.size(), max_cells);
        const std::size_t row_limit = std::max<std::size_t>(
            1, max_cells / std::max<std::size_t>(columns.indices.size(), 1));
        rows = sample_indices(start_index, signals.size(), row_limit);
    }

    Json::Value j;
    Json::Value ts_arr(Json::arrayValue);
    for (std::size_t di : rows.indices)
        ts_arr.append(ms_to_date(timestamps[di]));
    j["timestamps"] = std::move(ts_arr);

    Json::Value sym_arr(Json::arrayValue);
    for (std::size_t ii : columns.indices)
        sym_arr.append(symbols[ii]);
    j["symbols"] = std::move(sym_arr);

    Json::Value values(Json::arrayValue);
    for (std::size_t di : rows.indices) {
        if (signals[di].size() != symbols.size())
            throw std::invalid_argument("Signal plot symbols must match signal columns");
        Json::Value row_arr(Json::arrayValue);
        for (std::size_t ii : columns.indices) {
            const float v = signals[di][ii];
            row_arr.append(std::isfinite(v) ? Json::Value(v) : Json::Value(Json::nullValue));
        }
        values.append(std::move(row_arr));
    }
    j["values"] = std::move(values);
    const std::size_t original_rows = signals.size() - std::min(start_index, signals.size());
    j["originalRows"] = static_cast<Json::UInt64>(original_rows);
    j["originalSymbols"] = static_cast<Json::UInt64>(symbols.size());
    j["rowStride"] = static_cast<Json::UInt64>(rows.stride);
    j["symbolStride"] = static_cast<Json::UInt64>(columns.stride);
    j["sampled"] = rows.indices.size() < original_rows || columns.indices.size() < symbols.size();
    return j;
}

Json::Value signal_distribution_to_json(
    const std::vector<std::vector<float>>& signals,
    std::size_t start_index,
    const std::vector<int64_t>* timestamps,
    const std::vector<std::string>* symbols)
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

    // Histogram edges come from the bulk of the data, not the extremes: bins cover the
    // 0.5%..99.5% quantile range and the first/last bin absorb everything outside it.
    // The bin centres therefore stay readable even when one restated price is 1e9.
    const int nbins = kSignalHistogramBins;
    std::vector<float> sample;
    sample.reserve(1 << 20);
    for (std::size_t di = start_index; di < signals.size(); ++di)
        for (float v : signals[di])
            if (std::isfinite(v) && std::fabs(v) > 1e-12f) sample.push_back(v);
    auto quantile = [&](double q) {
        const std::size_t k = static_cast<std::size_t>(q * (sample.size() - 1));
        std::nth_element(sample.begin(), sample.begin() + k, sample.end());
        return static_cast<double>(sample[k]);
    };
    double lo = quantile(0.005), hi = quantile(0.995);
    if (!(hi > lo)) { lo = vmin; hi = vmax; }
    if (!(hi > lo)) { hi = lo + 1.0; }
    const int inner = nbins - 2;  // bins 0 and nbins-1 are the overflow bins
    const double bin_width = (hi - lo) / inner;
    j["rangeLo"] = lo;
    j["rangeHi"] = hi;

    std::vector<int> counts(nbins, 0);
    // Example cell per bin: the largest |value| seen in that bin (row, column).
    const bool want_examples = timestamps && symbols;
    std::vector<std::size_t> ex_row(nbins, 0), ex_col(nbins, 0);
    std::vector<float> ex_abs(nbins, -1.0f);
    // Second pass: count in place; no flattened copy of T×N signal values.
    for (std::size_t di = start_index; di < signals.size(); ++di) {
        const auto& row = signals[di];
        for (std::size_t ci = 0; ci < row.size(); ++ci) {
            const float v = row[ci];
            if (!std::isfinite(v) || std::fabs(v) <= 1e-12f)
                continue;
            int idx;
            if (v < lo) idx = 0;
            else if (v >= hi) idx = nbins - 1;
            else {
                idx = 1 + static_cast<int>((static_cast<double>(v) - lo) / bin_width);
                if (idx > nbins - 2) idx = nbins - 2;
            }
            counts[idx]++;
            if (want_examples && std::fabs(v) > ex_abs[idx]) {
                ex_abs[idx] = std::fabs(v);
                ex_row[idx] = di;
                ex_col[idx] = ci;
            }
        }
    }

    Json::Value bins_arr(Json::arrayValue);
    Json::Value counts_arr(Json::arrayValue);
    for (int i = 0; i < nbins; ++i) {
        // overflow bins are labelled by the range edge they sit beyond
        bins_arr.append(i == 0 ? lo : i == nbins - 1 ? hi : lo + bin_width * (i - 1 + 0.5));
        counts_arr.append(counts[i]);
    }
    j["bins"] = bins_arr;
    j["counts"] = counts_arr;
    if (want_examples) {
        Json::Value ex_sym(Json::arrayValue), ex_date(Json::arrayValue);
        for (int i = 0; i < nbins; ++i) {
            if (ex_abs[i] < 0 || ex_row[i] >= timestamps->size() || ex_col[i] >= symbols->size()) {
                ex_sym.append(Json::nullValue);
                ex_date.append(Json::nullValue);
            } else {
                ex_sym.append((*symbols)[ex_col[i]]);
                ex_date.append(ms_to_date((*timestamps)[ex_row[i]]));
            }
        }
        j["exampleSymbol"] = ex_sym;
        j["exampleDate"] = ex_date;
    }

    return j;
}

static Json::Value full_signal_distribution_to_json(
    const std::vector<std::vector<float>>& signals,
    std::size_t start_index,
    const std::vector<int64_t>* timestamps,
    const std::vector<std::string>* symbols,
    bool logarithmic)
{
    Json::Value j(Json::objectValue);
    for (const char* key : {"bins", "edges", "counts"}) j[key] = Json::Value(Json::arrayValue);
    const bool want_examples = timestamps && symbols;
    if (want_examples) {
        for (const char* key : {"exampleSymbol", "exampleDate", "exampleValue"})
            j[key] = Json::Value(Json::arrayValue);
    }
    double minimum = 0.0, maximum = 0.0, min_nonzero = 0.0;
    Json::UInt64 total = 0;
    for (std::size_t row = start_index; row < signals.size(); ++row) {
        for (float raw : signals[row]) {
            if (!std::isfinite(raw)) continue;
            const double value = raw;
            if (total == 0) minimum = maximum = value;
            minimum = std::min(minimum, value);
            maximum = std::max(maximum, value);
            const double magnitude = std::fabs(value);
            if (magnitude > 0 && (min_nonzero == 0 || magnitude < min_nonzero)) min_nonzero = magnitude;
            ++total;
        }
    }
    const bool positive = total > 0 && minimum > 0;
    const bool negative = total > 0 && maximum < 0;
    const double linthresh = min_nonzero > 0 ? min_nonzero : 1.0;
    j["scale"] = !logarithmic ? "linear" : positive ? "log" : negative ? "negative-log" : "symlog";
    j["linthresh"] = linthresh;
    j["min"] = minimum;
    j["max"] = maximum;
    j["rangeLo"] = minimum;
    j["rangeHi"] = maximum;
    j["totalCount"] = total;
    if (total == 0) return j;

    const auto transform = [=](double value) {
        if (!logarithmic) return value;
        if (positive) return std::log(value);
        if (negative) return -std::log(-value);
        return std::copysign(std::log1p(std::fabs(value) / linthresh), value);
    };
    const auto inverse = [=](double value) {
        if (!logarithmic) return value;
        if (positive) return std::exp(value);
        if (negative) return -std::exp(-value);
        return std::copysign(linthresh * std::expm1(std::fabs(value)), value);
    };
    double low = transform(minimum), high = transform(maximum);
    const bool constant = minimum == maximum;
    if (constant) {
        const double half_span = logarithmic ? 0.5 : std::max(std::fabs(minimum) * 0.01, 0.5);
        low -= half_span;
        high += half_span;
    }
    const double width = (high - low) / kSignalHistogramBins;
    std::vector<double> edges(kSignalHistogramBins + 1);
    for (int i = 0; i <= kSignalHistogramBins; ++i) {
        edges[i] = inverse(low + (high - low) * (static_cast<double>(i) / kSignalHistogramBins));
    }
    // Preserve exact raw endpoints, including the largest finite float, despite
    // roundoff through log/exp. Constant signals get a finite surrounding range.
    if (!constant) { edges.front() = minimum; edges.back() = maximum; }
    for (double edge : edges) j["edges"].append(edge);
    j["rangeLo"] = edges.front();
    j["rangeHi"] = edges.back();

    std::vector<Json::UInt64> counts(kSignalHistogramBins, 0);
    std::vector<double> example_abs(kSignalHistogramBins, -1.0), example_value(kSignalHistogramBins);
    std::vector<std::size_t> example_row(kSignalHistogramBins), example_col(kSignalHistogramBins);
    for (std::size_t row = start_index; row < signals.size(); ++row) {
        for (std::size_t col = 0; col < signals[row].size(); ++col) {
            const double value = signals[row][col];
            if (!std::isfinite(value)) continue;
            // Assign against the published raw edges so even a boundary cell
            // agrees with the client tooltip interval. The last bin includes max.
            const auto upper = std::upper_bound(edges.begin(), edges.end(), value);
            const int bin = std::clamp(static_cast<int>(upper - edges.begin()) - 1, 0, kSignalHistogramBins - 1);
            ++counts[bin];
            if (want_examples && row < timestamps->size() && col < symbols->size()
                && std::fabs(value) > example_abs[bin]) {
                example_abs[bin] = std::fabs(value);
                example_value[bin] = value;
                example_row[bin] = row;
                example_col[bin] = col;
            }
        }
    }
    for (int bin = 0; bin < kSignalHistogramBins; ++bin) {
        j["bins"].append(std::clamp(inverse(low + width * (bin + 0.5)), edges[bin], edges[bin + 1]));
        j["counts"].append(counts[bin]);
        if (!want_examples) continue;
        if (example_abs[bin] < 0) {
            j["exampleSymbol"].append(Json::nullValue);
            j["exampleDate"].append(Json::nullValue);
            j["exampleValue"].append(Json::nullValue);
        } else {
            j["exampleSymbol"].append((*symbols)[example_col[bin]]);
            j["exampleDate"].append(ms_to_date((*timestamps)[example_row[bin]]));
            j["exampleValue"].append(example_value[bin]);
        }
    }
    return j;
}


Json::Value signal_log_distribution_to_json(
    const std::vector<std::vector<float>>& signals,
    std::size_t start_index,
    const std::vector<int64_t>* timestamps,
    const std::vector<std::string>* symbols)
{
    auto result = full_signal_distribution_to_json(signals, start_index, timestamps, symbols, true);
    // Pair both views over the identical raw population; keep the historical
    // quantile-clipped linear API intact for older clients.
    result["linear"] = full_signal_distribution_to_json(signals, start_index, timestamps, symbols, false);
    return result;
}
