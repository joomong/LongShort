#include "http_server.h"
#include <cmath>
#include <algorithm>
#include <ctime>
#include <stdexcept>
#include <drogon/drogon.h>
#include <iostream>
#include <filesystem>
#include <cctype>
#include <cstdlib>
#include <memory>
#include <atomic>
#include <mutex>
#include <numeric>
#include <iomanip>
#include <chrono>
#include <limits>
#include "loader.h"
#include "simulation_data.h"
#include "xml_compiler.h"
#include "simulator_engine.h"
#include "pool_correlation.h"
#include "meta.h"
#include "alpha_storage.h"
#include "modules/MetaSource.h"
#include "alpha_search.h"
#include "ga_run_store.h"
#include <thread>
#include <fstream>
#include <future>
#include <set>
#include <regex>

constexpr double kReportNotional = 20000000.0;

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
    j["annualReturn"] = m.ann_ret;
    j["sharpe"] = m.sharpe;
    j["maxDrawdown"] = m.mdd;
    j["turnover"] = m.turnover;
    j["maxWeight"] = m.max_weight;
    j["avgPositionCount"] = m.avg_position_count;
    j["minPositionCount"] = m.min_position_count;
    j["pnl"] = m.pnl;
    j["winRate"] = m.win_rate;
    j["margin"] = m.margin;
    j["longRatio"] = m.long_ratio;
    j["shortRatio"] = m.short_ratio;
    j["activeBarCount"] = m.active_bar_count;
    j["avgMaxWeight"] = m.avg_max_bet;
    j["quality"] = m.quality;
    j["sharpe400"] = m.sharpe400;
    j["sharpe80"] = m.sharpe80;
    j["minGrossExposure"] = m.minsoc;
    return j;
}

Json::Value yearly_to_json(const std::vector<YearlyMetrics>& yearly) {
    Json::Value arr(Json::arrayValue);
    for (const auto& y : yearly) {
        Json::Value j;
        j["year"] = y.year;
        j["longRatio"] = y.long_sm;
        j["shortRatio"] = y.short_sm;
        j["pnl"] = y.pnl_pct / 100.0;
        j["annualReturn"] = y.ann_ret / 100.0;
        j["sharpe"] = y.sharpe;
        j["informationRatio"] = y.ir;
        j["maxDrawdown"] = y.max_dd / 100.0;
        j["winRate"] = y.win_lose / 100.0;
        j["turnover"] = y.daily_tvr;
        j["margin"] = y.margin;
        j["fitness"] = y.fitness;
        arr.append(std::move(j));
    }
    return arr;
}

Json::Value performance_to_json(const PerformanceSeries& series) {
    Json::Value j;
    const auto doubles = [](const std::vector<double>& values) {
        Json::Value output(Json::arrayValue);
        for (double value : values) output.append(std::isfinite(value) ? Json::Value(value) : Json::Value());
        return output;
    };
    j["timestamps"] = Json::Value(Json::arrayValue);
    for (int64_t timestamp : series.timestamps) j["timestamps"].append(Json::Int64(timestamp));
    j["cumulativeNet"] = doubles(series.cumulative_net);
    j["cumulativeGross"] = doubles(series.cumulative_gross);
    j["drawdown"] = doubles(series.drawdown);
    j["turnover"] = doubles(series.turnover);
    j["maxWeight"] = doubles(series.max_weight);
    if (!series.frozen_gross.empty()) j["frozenGross"] = doubles(series.frozen_gross);
    j["positionCount"] = Json::Value(Json::arrayValue);
    for (int value : series.position_count) j["positionCount"].append(value);
    j["osStartIndex"] = Json::UInt64(series.os_start_index);
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
    const std::vector<std::size_t>* symbol_subset,
    const std::vector<uint8_t>* universe_mask)
{
    if (max_cells == 0)
        throw std::invalid_argument("Signal plot cell budget must be positive");
    if (universe_mask && !universe_mask->empty() &&
        universe_mask->size() != signals.size() * symbols.size())
        throw std::invalid_argument("Signal plot universe mask must be T*N");
    if (signals.size() != timestamps.size())
        throw std::invalid_argument("Signal plot dates must match signal rows");
    SampledIndices columns;
    SampledIndices rows;
    std::size_t original_symbols = symbols.size();
    if (symbol_subset) {
        for (std::size_t ii : *symbol_subset) {
            if (ii >= symbols.size())
                throw std::invalid_argument("Signal plot symbol subset is out of range");
            columns.indices.push_back(ii);
        }
        rows = sample_indices(start_index, signals.size(), signals.size());  // every row
    } else {
        // With a universe mask, columns are the symbols that are members on at least one
        // displayed row (panel order); the cell budget then samples over that set.
        std::vector<std::size_t> candidates;
        const bool restrict = universe_mask && !universe_mask->empty();
        if (restrict) {
            const std::size_t n = symbols.size();
            std::vector<uint8_t> ever(n, 0);
            for (std::size_t di = start_index; di < signals.size(); ++di)
                for (std::size_t ii = 0; ii < n; ++ii)
                    if ((*universe_mask)[di * n + ii]) ever[ii] = 1;
            for (std::size_t ii = 0; ii < n; ++ii)
                if (ever[ii]) candidates.push_back(ii);
        }
        const std::size_t column_count = restrict ? candidates.size() : symbols.size();
        columns = sample_indices(0, column_count, max_cells);
        if (restrict)
            for (std::size_t& k : columns.indices) k = candidates[k];
        original_symbols = column_count;
        const std::size_t row_limit = std::max<std::size_t>(
            1, max_cells / std::max<std::size_t>(columns.indices.size(), 1));
        rows = sample_indices(start_index, signals.size(), row_limit);
    }

    Json::Value j;
    Json::Value ts_arr(Json::arrayValue);
    for (std::size_t di : rows.indices)
        ts_arr.append(Json::Int64(timestamps[di]));
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
    Json::Value centers(Json::arrayValue), scales(Json::arrayValue);
    const auto median = [](const std::vector<double>& sorted) {
        const auto middle = sorted.size() / 2;
        return sorted.size() % 2 ? sorted[middle] : sorted[middle - 1] / 2.0 + sorted[middle] / 2.0;
    };
    for (std::size_t di : rows.indices) {
        std::vector<double> finite;
        finite.reserve(columns.indices.size());
        for (std::size_t ii : columns.indices)
            if (std::isfinite(signals[di][ii])) finite.push_back(signals[di][ii]);
        if (finite.empty()) { centers.append(Json::nullValue); scales.append(Json::nullValue); continue; }
        std::sort(finite.begin(), finite.end());
        const double center = median(finite);
        std::vector<double> deviations;
        deviations.reserve(finite.size());
        for (double value : finite) deviations.push_back(std::fabs(value - center));
        std::sort(deviations.begin(), deviations.end());
        double scale = median(deviations) * 1.4826;
        if (scale == 0 && finite.front() != finite.back()) {
            double mean = 0, squared = 0;
            for (std::size_t i = 0; i < finite.size(); ++i) {
                const double delta = finite[i] - mean;
                mean += delta / (i + 1);
                squared += delta * (finite[i] - mean);
            }
            scale = std::sqrt(squared / finite.size());
        }
        centers.append(center); scales.append(scale);
    }
    j["centers"] = std::move(centers);
    j["scales"] = std::move(scales);
    j["values"] = std::move(values);
    if (universe_mask && !universe_mask->empty()) {
        // One '1'/'0' string per sampled row, aligned with values: signal-date universe membership.
        Json::Value membership(Json::arrayValue);
        for (std::size_t di : rows.indices) {
            std::string row(columns.indices.size(), '0');
            for (std::size_t k = 0; k < columns.indices.size(); ++k)
                if ((*universe_mask)[di * symbols.size() + columns.indices[k]]) row[k] = '1';
            membership.append(std::move(row));
        }
        j["membership"] = std::move(membership);
    }
    const std::size_t original_rows = signals.size() - std::min(start_index, signals.size());
    j["originalRows"] = static_cast<Json::UInt64>(original_rows);
    j["originalSymbols"] = static_cast<Json::UInt64>(original_symbols);
    j["rowStride"] = static_cast<Json::UInt64>(rows.stride);
    j["symbolStride"] = static_cast<Json::UInt64>(columns.stride);
    j["sampled"] = rows.indices.size() < original_rows || columns.indices.size() < original_symbols;
    return j;
}

Json::Value signal_subset_plot_to_json(
    const std::vector<std::vector<float>>& signals,
    const std::vector<int64_t>& timestamps,
    const std::vector<std::string>& symbols,
    const std::vector<std::size_t>& columns,
    std::size_t panel_symbols,
    std::size_t whole_panel_symbols,
    std::size_t start_index,
    std::size_t max_cells,
    const std::vector<uint8_t>& universe_mask)
{
    const std::size_t count = symbols.size();
    std::vector<std::vector<float>> values(signals.size());
    std::vector<uint8_t> membership;
    if (!universe_mask.empty()) membership.assign(signals.size() * count, 0);
    for (std::size_t di = 0; di < signals.size(); ++di) {
        if (!signals[di].empty()) values[di].resize(count, std::numeric_limits<float>::quiet_NaN());
        for (std::size_t k = 0; k < count; ++k) {
            if (columns[k] == kSymbolNotInPanel) continue;
            if (!signals[di].empty()) values[di][k] = signals[di][columns[k]];
            if (!universe_mask.empty()) membership[di * count + k] = universe_mask[di * panel_symbols + columns[k]];
        }
    }
    std::vector<std::size_t> own(count);
    std::iota(own.begin(), own.end(), std::size_t{0});
    Json::Value plot = signal_plot_to_json(values, timestamps, symbols, start_index, max_cells, &own,
                                           universe_mask.empty() ? nullptr : &membership);
    plot["originalSymbols"] = static_cast<Json::UInt64>(whole_panel_symbols);
    plot["sampled"] = count < whole_panel_symbols || plot["sampled"].asBool();
    return plot;
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
                ex_date.append(Json::Int64((*timestamps)[ex_row[i]]));
            }
        }
        j["exampleSymbol"] = ex_sym;
        j["exampleTimestamp"] = ex_date;
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
        for (const char* key : {"exampleSymbol", "exampleTimestamp", "exampleValue"})
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
            j["exampleTimestamp"].append(Json::nullValue);
            j["exampleValue"].append(Json::nullValue);
        } else {
            j["exampleSymbol"].append((*symbols)[example_col[bin]]);
            j["exampleTimestamp"].append(Json::Int64((*timestamps)[example_row[bin]]));
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
    // Both views use the same full raw population.
    result["linear"] = full_signal_distribution_to_json(signals, start_index, timestamps, symbols, false);
    return result;
}


namespace fs = std::filesystem;

// Global shared state (loaded once at startup)
static BacktestConfig g_config;
static Json::Value g_symbol_labels;  // optional {panel symbol: display label}
static DataPanel g_data;
static RunPanelCache g_run_panels(g_data);
static fs::path g_alphas_dir;
static fs::path g_meta_dir;  // optional; never created by the engine
static MemberReturnsCache g_member_returns;  // member gross returns of non-EW sets, shared by requests
static MissingClosePolicy g_missing_close_policy = MissingClosePolicy::Carry;
static std::unique_ptr<alpha_pool::Cache> g_alpha_pool;  // set when the config names an alpha_pool
static GaRunStore g_ga_runs(std::size_t{2} << 30);  // answers GA row clicks without simulating again

static bool supports_exact_fields(std::initializer_list<const char*> names) {
    return std::all_of(names.begin(), names.end(), [](const char* name) { return g_data.has_column(name); });
}

static bool supports_ohlc4() { return supports_exact_fields({"open", "high", "low"}); }
static bool supports_vwap() { return supports_exact_fields({"vwap", "high", "low"}); }

static bool parse_missing_close_policy(const std::string& name, MissingClosePolicy* out)
{
    if (name == "carry") { *out = MissingClosePolicy::Carry; return true; }
    if (name == "error") { *out = MissingClosePolicy::Error; return true; }
    return false;
}

static const char* missing_close_policy_name(MissingClosePolicy policy)
{
    return policy == MissingClosePolicy::Error ? "error" : "carry";
}

static unsigned parse_env_uint(const char* name, unsigned def, unsigned min_v, unsigned max_v)
{
    const char* raw = std::getenv(name);
    if (raw == nullptr || *raw == '\0')
        return def;

    char* end = nullptr;
    long v = std::strtol(raw, &end, 10);
    if (end == nullptr || *end != '\0')
        return def;
    if (v < static_cast<long>(min_v) || v > static_cast<long>(max_v))
        return def;
    return static_cast<unsigned>(v);
}

// jsoncpp's own range error leaks an internal message, so bound the value first.
static unsigned parse_uint_option(const Json::Value& body, const char* key, unsigned fallback)
{
    const Json::Value value = body.get(key, fallback);
    const std::string limits =
        std::string(key) + " must be an integer between 0 and 4294967295";
    if (!value.isIntegral() || !value.isInt64())
        throw std::invalid_argument(limits);
    const Json::Int64 raw = value.asInt64();
    if (raw < 0 || raw > static_cast<Json::Int64>(std::numeric_limits<unsigned>::max()))
        throw std::invalid_argument(limits);
    return static_cast<unsigned>(raw);
}

static std::string iso_date_of(int64_t epoch_ms)
{
    const time_t seconds = static_cast<time_t>(epoch_ms / 1000);
    struct tm parts;
    gmtime_r(&seconds, &parts);
    char text[11];
    strftime(text, sizeof(text), "%Y-%m-%d", &parts);
    return text;
}

static bool parse_iso_date_ms(const std::string& s, int64_t* out_ms)
{
    struct tm t = {};
    char* end = strptime(s.c_str(), "%Y-%m-%d", &t);
    if (end == nullptr || *end != '\0')
        return false;

    time_t sec = timegm(&t);

    struct tm normalized = {};
    gmtime_r(&sec, &normalized);
    char buf[11];
    strftime(buf, sizeof(buf), "%Y-%m-%d", &normalized);
    if (s != buf)
        return false;

    *out_ms = static_cast<int64_t>(sec) * 1000;
    return true;
}

// Long evaluations run off the HTTP event loops so one request cannot stall the
// other connections that share its loop. Workers stay owned until completion
// and are joined before shared data is destroyed at shutdown. Completed workers
// are reaped when another run starts.
class BackgroundWorkers {
public:
    explicit BackgroundWorkers(const char* name) : name_(name) {}
    ~BackgroundWorkers() { stop_and_join(); }

    void stop_and_join() {
        stopping_.store(true, std::memory_order_release);
        std::vector<Task> tasks;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            tasks = std::move(tasks_);
        }
        for (auto& task : tasks)
            if (task.thread.joinable()) task.thread.join();
    }

    bool stopping() const { return stopping_.load(std::memory_order_acquire); }

    template<class Work>
    void launch(Work work) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (stopping()) throw std::runtime_error("Server is stopping");
        for (auto it = tasks_.begin(); it != tasks_.end();) {
            if (it->done->load(std::memory_order_acquire)) {
                it->thread.join();
                it = tasks_.erase(it);
            } else ++it;
        }
        auto done = std::make_shared<std::atomic<bool>>(false);
        tasks_.push_back(Task{std::thread{}, done});
        try {
            tasks_.back().thread = std::thread([work = std::move(work), done, name = name_]() mutable {
                try { work(); }
                catch (const std::exception& error) { LOG_ERROR << name << " worker: " << error.what(); }
                catch (...) { LOG_ERROR << name << " worker failed"; }
                done->store(true, std::memory_order_release);
            });
        } catch (...) {
            tasks_.pop_back();
            throw;
        }
    }

private:
    struct Task { std::thread thread; std::shared_ptr<std::atomic<bool>> done; };
    const char* name_;
    std::vector<Task> tasks_;
    std::mutex mutex_;
    std::atomic<bool> stopping_{false};
};
static BackgroundWorkers g_ga_workers("GA");
static BackgroundWorkers g_simulate_workers("Simulate");
static BackgroundWorkers g_alpha_workers("Alpha");

using ClientDisconnected = EvaluationCancelled;

struct RunOptions {
    int64_t is_ms = 0;
    int64_t os_ms = 0;
    unsigned sim_start_index = 0;
    unsigned sim_end_index = 0; // exclusive panel row; request simEnd is inclusive
    unsigned threads = 20;
    std::string output = "full";
    std::size_t signal_max_cells = 4000000;
    std::vector<std::string> signal_symbols;       // non-empty: full series for these symbols only
    std::vector<std::size_t> signal_columns;       // their run panel columns; kSymbolNotInPanel = never touched
    MissingClosePolicy missing_close_policy = MissingClosePolicy::Carry;
    std::string universe;                          // selected universe.names entry; empty = default
    std::shared_ptr<const DataPanel> panel;        // compact run panel, held for the whole request
    const std::vector<uint8_t>* universe_mask = nullptr; // the panel's mask
    std::string execution_price;
};

static RunOptions parse_run_options(const Json::Value& body) {
    RunOptions options;
    const auto execution = body.get("executionPrice", g_config.execution_price);
    if (!execution.isString() || (execution.asString() != "close" && execution.asString() != "ohlc4" &&
                                  execution.asString() != "vwap"))
        throw std::invalid_argument("executionPrice must be 'close', 'ohlc4' or 'vwap'");
    options.execution_price = execution.asString();
    if (options.execution_price == "ohlc4" && !supports_ohlc4())
        throw std::invalid_argument("OHLC4 execution requires available exact-alignment open/high/low fields");
    if (options.execution_price == "vwap" && !supports_vwap())
        throw std::invalid_argument("VWAP execution requires available exact-alignment vwap/high/low fields");
    const std::string is_start = body.get("isStart", "2021-01-01").asString();
    const std::string os_start = body.get("osStart", "2024-01-01").asString();
    if (!parse_iso_date_ms(is_start, &options.is_ms) || !parse_iso_date_ms(os_start, &options.os_ms))
        throw std::invalid_argument("Invalid date format; expected YYYY-MM-DD");
    if (options.is_ms >= options.os_ms)
        throw std::invalid_argument("isStart must be earlier than osStart");
    const std::string sim_start = body.get("simStart", "").asString();
    if (!sim_start.empty()) {
        int64_t sim_ms = 0;
        if (!parse_iso_date_ms(sim_start, &sim_ms))
            throw std::invalid_argument("Invalid simStart; expected YYYY-MM-DD");
        options.sim_start_index = static_cast<unsigned>(std::lower_bound(
            g_data.timestamps.begin(), g_data.timestamps.end(), sim_ms) - g_data.timestamps.begin());
    }
    if (options.sim_start_index >= g_data.T)
        throw std::invalid_argument("simStart is after the available trading period");
    options.sim_end_index = g_data.T;
    const Json::Value end_value = body.get("simEnd", "");
    if (!end_value.isString())
        throw std::invalid_argument("simEnd must be a YYYY-MM-DD string");
    const std::string sim_end = end_value.asString();
    if (!sim_end.empty()) {
        int64_t end_ms = 0;
        if (!parse_iso_date_ms(sim_end, &end_ms))
            throw std::invalid_argument("Invalid simEnd; expected YYYY-MM-DD");
        options.sim_end_index = static_cast<unsigned>(std::upper_bound(
            g_data.timestamps.begin(), g_data.timestamps.end(), end_ms + 86399999) - g_data.timestamps.begin());
    }
    if (options.sim_start_index >= options.sim_end_index)
        throw std::invalid_argument("Sim period has no trading bars: simEnd must be on or after simStart");
    if (body.isMember("threads")) {
        const Json::Value& threads = body["threads"];
        if (!threads.isIntegral() || !threads.isInt64() || threads.asInt64() < 1 || threads.asInt64() > 32)
            throw std::invalid_argument("threads must be between 1 and 32");
        options.threads = static_cast<unsigned>(threads.asInt64());
    }
    options.output = body.get("output", "full").asString();
    if (options.output != "performance" && options.output != "signals" &&
        options.output != "signalSeries" && options.output != "full")
        throw std::invalid_argument("output must be performance, signals, signalSeries or full");
    const unsigned requested_cells = parse_uint_option(body, "signalPlotMaxCells", 4000000);
    if (requested_cells == 0)
        throw std::invalid_argument("signalPlotMaxCells must be positive");
    options.signal_max_cells = std::min<unsigned>(requested_cells, 8000000);
    if (body.isMember("signalPlotSymbols")) {
        const Json::Value& subset = body["signalPlotSymbols"];
        if (!subset.isArray())
            throw std::invalid_argument("signalPlotSymbols must be an array of panel symbols");
        for (const Json::Value& item : subset) {
            const std::string name = item.asString();
            if (std::find(g_data.symbols.begin(), g_data.symbols.end(), name) == g_data.symbols.end())
                throw std::invalid_argument("signalPlotSymbols: unknown symbol " + name);
            options.signal_symbols.push_back(name);
        }
        if (options.signal_symbols.empty())
            throw std::invalid_argument("signalPlotSymbols must not be empty");
    }
    if (options.output == "signalSeries" && options.signal_symbols.empty())
        throw std::invalid_argument("signalSeries output requires signalPlotSymbols");
    options.missing_close_policy = g_missing_close_policy;
    if (body.isMember("missingClose") &&
        !parse_missing_close_policy(body["missingClose"].asString(), &options.missing_close_policy))
        throw std::invalid_argument("missingClose must be 'carry' or 'error'");
    if (body.isMember("universe") && !body["universe"].isNull()) {
        if (!body["universe"].isString())
            throw std::invalid_argument("universe must be a string");
        options.universe = body["universe"].asString();
    }
    (void)g_data.mask_for(options.universe);
    if (options.universe.empty()) options.universe = g_data.default_universe;
    return options;
}

// Separate from parsing, which also runs on the I/O loop: a first build copies the panel's returns.
static void attach_run_panel(RunOptions& options) {
    SimulatorConfig range;
    range.sim_start_index = options.sim_start_index;
    options.panel = g_run_panels.get(options.universe, range.history_start(), options.sim_end_index);
    options.universe_mask = &options.panel->universe_mask;
    options.signal_columns.clear();
    const auto& symbols = options.panel->symbols;
    for (const auto& name : options.signal_symbols) {
        const auto it = std::find(symbols.begin(), symbols.end(), name);
        options.signal_columns.push_back(it == symbols.end() ? kSymbolNotInPanel : static_cast<std::size_t>(it - symbols.begin()));
    }
}

static Json::Value run_context(const RunOptions& options) {
    Json::Value context;
    context["simStart"] = iso_date_of(g_data.timestamps[options.sim_start_index]);
    context["simEnd"] = iso_date_of(g_data.timestamps[options.sim_end_index - 1]);
    context["isStart"] = iso_date_of(options.is_ms);
    context["osStart"] = iso_date_of(options.os_ms);
    context["universe"] = options.universe;
    context["executionPrice"] = options.execution_price;
    context["threads"] = options.threads;
    context["feeRate"] = g_config.transaction_fee_rate;
    context["reportNotional"] = kReportNotional;
    return context;
}

static SimulatorConfig simulator_config(const RunOptions& options, bool collect) {
    SimulatorConfig config;
    config.T = options.panel->T;
    config.N = options.panel->N;
    config.fee_rate = g_config.transaction_fee_rate;
    config.sim_start_index = options.sim_start_index;
    config.sim_end_index = options.sim_end_index;
    config.collect_signals = collect;
    config.missing_close_policy = options.missing_close_policy;
    config.universe_mask = options.universe_mask;
    config.execution_price = options.execution_price == "ohlc4" ? ExecutionPrice::Ohlc4
        : options.execution_price == "vwap" ? ExecutionPrice::Vwap : ExecutionPrice::Close;
    return config;
}

// Members are listed per compile, so a meta folder change is picked up by the next request.
static const MetaResolver g_meta_resolver = [](const std::string& name) {
    std::vector<std::pair<std::string, std::string>> pairs;
    for (auto& member : meta_dir::members(g_meta_dir, name))
        pairs.emplace_back(std::move(member.path), std::move(member.xml));
    return pairs;
};

static std::vector<CompiledAlpha> compile_request_xml(const std::string& xml, const RunOptions& options) {
    return compile_xml(xml, options.panel.get(), options.universe_mask, &g_meta_resolver);
}

// Rejects an alpha whose signal is not valid in time for the first IS bar, whose position comes from
// the previous bar's signal, or never valid in the run. Computation starts one bar before simStart.
static void require_history(const CompiledAlpha& alpha, const RunOptions& options) {
    const unsigned sim_start = options.sim_start_index;
    const auto is_index = static_cast<unsigned>(std::lower_bound(
        g_data.timestamps.begin(), g_data.timestamps.end(), options.is_ms) - g_data.timestamps.begin());
    unsigned required = std::max(is_index, sim_start);
    if (required > 0) --required;
    const unsigned first_valid = first_valid_bar(alpha, simulator_config(options, false).history_start());
    if (first_valid > required || first_valid >= options.sim_end_index)
        throw std::invalid_argument("Insufficient history");
}

using MetaMatrices = std::vector<std::shared_ptr<const MetaMatrix>>;

static MetaCombineContext meta_context(const RunOptions& options, MetaSetCache& cache,
                                       const std::function<bool()>& cancelled) {
    // Member returns depend on these request settings only (is/os split excluded).
    const std::string settings = options.universe + "\n" + std::to_string(options.sim_start_index) + "\n" +
        std::to_string(options.sim_end_index) + "\n" + options.execution_price + "\n" +
        missing_close_policy_name(options.missing_close_policy);
    return MetaCombineContext{*options.panel,
                              MetaRunConfig{simulator_config(options, false), options.threads,
                                            options.missing_close_policy, options.universe_mask},
                              settings, g_member_returns, cache, cancelled};
}

// Points every <meta> source of `alpha` at its request-cached matrix (EW, or the set's combine
// spec). Keep the result alive for the duration of the run.
static MetaMatrices bind_meta_sets(CompiledAlpha& alpha, const RunOptions& options, MetaSetCache& cache,
                                   const std::function<bool()>& cancelled) {
    MetaMatrices held;
    for (auto& set : alpha.meta_sets) {
        // The listing is part of the key: a folder change mid-request never reuses a stale matrix.
        auto matrix = cache.get(meta_set_key(set, set.combine), [&] {
            const auto context = meta_context(options, cache, cancelled);
            if (!set.combine.is_ew()) return materialize_combined(context, set);
            cache.record(1, set.members.size());
            return materialize_meta_set(*options.panel, set, context.config, cancelled, &cache.budget());
        });
        set.source->matrix = matrix.get();
        set.members.clear();  // compiled for this alpha; the cached matrix replaces them
        held.push_back(std::move(matrix));
    }
    return held;
}

static Json::Value meta_payload(const CompiledAlpha& alpha, MetaSetCache& cache) {
    Json::Value sets(Json::arrayValue);
    for (const auto& set : alpha.meta_sets) {
        Json::Value entry;
        entry["name"] = set.name;
        entry["members"] = static_cast<Json::UInt64>(set.member_paths.size());
        entry["combine"] = set.combine.key();
        MetaCombineInfo info;
        if (!set.combine.is_ew() && cache.info(meta_set_key(set, set.combine), &info)) {
            entry["fallbackBars"] = info.fallback_bars;
            entry["firstEstimatedBar"] = info.first_estimated_bar;
        }
        sets.append(std::move(entry));
    }
    return sets;
}

static SimulatorOutput execute_alpha(
    CompiledAlpha& alpha, const RunOptions& options, bool collect,
    const SimulatorConfig::SignalRowCallback& callback,
    const SimulatorConfig::AccountingRowCallback& accounting,
    MetaSetCache& cache, const std::function<bool()>& cancelled, unsigned module_threads = 1) {
    const auto meta_matrices = bind_meta_sets(alpha, options, cache, cancelled);
    auto config = simulator_config(options, collect);
    config.module_threads = module_threads;
    config.signal_row_callback = callback;
    config.accounting_row_callback = accounting;
    return Simulator(*options.panel, config).run(alpha.pipeline);
}

static FullMetricsResult full_metrics(const SimulatorOutput& output, const RunOptions& options) {
    const std::size_t first_metric_bar = options.sim_start_index;
    std::vector<double> frozen_gross(output.missing_data.size());
    for (std::size_t di = 0; di < output.missing_data.size(); ++di)
        frozen_gross[di] = output.missing_data[di].frozen_gross;
    return compute_full_metrics(
        output.timestamps, output.ret, output.gross_ret, output.turnover, output.max_weight,
        output.long_weight, output.short_weight, output.position_count,
        options.is_ms, options.os_ms, g_config.bars_per_year, first_metric_bar, &frozen_gross);
}

// The universe the pool is simulated with: config alpha_pool_universe, else the engine default.
static std::string pool_universe() {
    return g_config.alpha_pool_universe.empty() ? g_data.default_universe : g_config.alpha_pool_universe;
}

// Pool alphas run with the pool's standard settings: alpha_pool_universe and the config execution price,
// the whole panel, no metrics. Returns the daily net return by panel row (zero before the first position).
static std::vector<double> pool_net_returns(const std::string& xml) {
    RunOptions options;
    options.execution_price = g_config.execution_price;
    if (options.execution_price == "ohlc4" && !supports_ohlc4())
        throw std::runtime_error("alpha_pool: OHLC4 execution needs exact open/high/low fields");
    if (options.execution_price == "vwap" && !supports_vwap())
        throw std::runtime_error("alpha_pool: VWAP execution needs exact vwap/high/low fields");
    options.sim_end_index = g_data.T;
    options.threads = 1;
    options.output = "performance";
    options.missing_close_policy = g_missing_close_policy;
    options.universe = pool_universe();
    // The pool stays on the whole panel; correlations read its daily returns by absolute bar.
    options.panel = std::shared_ptr<const DataPanel>(std::shared_ptr<const DataPanel>{}, &g_data);
    options.universe_mask = &g_data.mask_for(options.universe);
    auto alphas = compile_request_xml(xml, options);
    if (alphas.size() != 1) throw std::runtime_error("alpha.xml must compile to exactly one alpha");
    MetaSetCache cache(1);
    auto output = execute_alpha(alphas[0], options, false, [](unsigned, const float*, unsigned) {}, {}, cache,
                                [] { return false; });
    return std::move(output.ret);
}

// Picks up pool alphas added or changed since the last call; a no-op when the listing is unchanged.
static void refresh_alpha_pool(unsigned threads) {
    if (!g_alpha_pool) return;
    std::vector<std::string> failures;
    const std::size_t added = g_alpha_pool->refresh(pool_net_returns, threads, &failures);
    for (const auto& failure : failures) std::cerr << "alpha_pool: skipped " << failure << std::endl;
    if (added)
        std::cout << "alpha_pool: " << added << " alpha(s) simulated, " << g_alpha_pool->size() << " cached" << std::endl;
}

// Missing quotes freeze held weights; thin ending moves retire their sources.
static Json::Value missing_data_payload(const SimulatorOutput& output) {
    MissingDataDiagnostics total;
    for (const auto& bar : output.missing_data) {
        total.retirement_count += bar.retirement_count;
        total.missing_held_count += bar.missing_held_count;
        total.blocked_order_count += bar.blocked_order_count;
        total.frozen_gross += bar.frozen_gross;
        total.blocked_trade_weight += bar.blocked_trade_weight;
        total.resumed_holding_count += bar.resumed_holding_count;
        total.missing_entry_count += bar.missing_entry_count;
        total.successor_conversions += bar.successor_conversions;
        total.converted_gross += bar.converted_gross;
    }
    Json::Value payload;
    payload["missingHeldBars"] = total.missing_held_count;
    payload["frozenGrossSum"] = total.frozen_gross;
    payload["retirements"] = total.retirement_count;
    payload["blockedOrders"] = total.blocked_order_count;
    payload["blockedTradeWeight"] = total.blocked_trade_weight;
    payload["missingEntries"] = total.missing_entry_count;
    payload["resumedHoldings"] = total.resumed_holding_count;
    payload["successorConversions"] = total.successor_conversions;
    payload["convertedGross"] = total.converted_gross;
    payload["terminalFrozenGross"] = output.missing_data.empty()
        ? 0.0 : output.missing_data.back().frozen_gross;
    return payload;
}

static Json::Value diagnostics_payload(const SimulatorOutput& output, const RunOptions&) {
    Json::Value payload;
    payload["missingData"] = missing_data_payload(output);
    return payload;
}

static Json::Value metrics_payload(const FullMetricsResult& metrics) {
    Json::Value payload;
    payload["summary"]["is"] = summary_to_json(metrics.is_summary);
    payload["summary"]["os"] = summary_to_json(metrics.os_summary);
    payload["yearly"] = yearly_to_json(metrics.yearly);
    payload["performance"] = performance_to_json(compute_performance_series(metrics));
    return payload;
}

static void append_signal_payload(
    Json::Value& payload, const std::vector<std::vector<float>>& signals,
    const std::vector<int64_t>& timestamps, const RunOptions& options) {
    const std::size_t begin = options.sim_start_index;
    const DataPanel& panel = *options.panel;
    // Outputs stop at simEnd while the immutable, shared panel retains its full
    // history. The plot serializer requires a mask matching the output prefix.
    const auto* mask = options.universe_mask;
    std::vector<uint8_t> prefix_mask;
    const auto cells = signals.size() * panel.N;
    if (mask && !mask->empty() && mask->size() > cells) {
        prefix_mask.assign(mask->begin(), mask->begin() + cells);
        mask = &prefix_mask;
    }
    const bool parallel = cells >= 1000000;
    const auto policy = parallel ? std::launch::async : std::launch::deferred;
    auto plot = std::async(policy, [&] {
        if (options.signal_symbols.empty())
            return signal_plot_to_json(signals, timestamps, panel.symbols, begin, options.signal_max_cells, nullptr, mask);
        return signal_subset_plot_to_json(signals, timestamps, options.signal_symbols, options.signal_columns,
            panel.N, g_data.N, begin, options.signal_max_cells, *options.universe_mask);
    });
    auto linear = std::async(policy, [&] {
        return signal_distribution_to_json(signals, begin, &timestamps, &panel.symbols);
    });
    auto logarithmic = std::async(policy, [&] {
        return signal_log_distribution_to_json(signals, begin, &timestamps, &panel.symbols);
    });
    payload["plot"] = plot.get();
    payload["distribution"] = linear.get();
    payload["logDistribution"] = logarithmic.get();
    float minimum = 0.0f, maximum = 0.0f;
    bool first = true;
    for (std::size_t di = begin; di < signals.size(); ++di) {
        for (float value : signals[di]) {
            if (!std::isfinite(value)) continue;
            if (first) { minimum = maximum = value; first = false; }
            else { minimum = std::min(minimum, value); maximum = std::max(maximum, value); }
        }
    }
    payload["range"]["min"] = minimum;
    payload["range"]["max"] = maximum;
}

static Json::Value completed_alpha_json(const FullMetricsResult& metrics, const Json::Value& missing_data,
                                        std::size_t id, const std::string& xml, const Json::Value& meta,
                                        const Json::Value& pool_corr) {
    Json::Value entry = metrics_payload(metrics);
    entry["missingData"] = missing_data;
    entry["id"] = static_cast<Json::UInt64>(id);
    entry["xml"] = xml;
    entry["status"] = "completed";
    if (!meta.isNull()) entry["meta"] = meta;
    if (!pool_corr.isNull()) entry["poolCorrelation"] = pool_corr;
    return entry;
}

// Shared by /api/simulate and saved alpha results so both produce the same body.
// `accounting` observes a single-alpha run only (saved positions); it does not change the body.
static Json::Value simulate_response(const Json::Value& body, const std::function<bool()>& cancelled,
                                     const SimulatorConfig::AccountingRowCallback& accounting = {},
                                     MetaSetCache* shared_cache = nullptr) {
    RunOptions options = parse_run_options(body);
    attach_run_panel(options);
    const DataPanel& panel = *options.panel;
    MetaSetCache request_cache(options.threads);  // one per request unless the caller shares one (save rows)
    MetaSetCache& cache = shared_cache ? *shared_cache : request_cache;
    const bool combined_only = body.get("combinedOnly", false).asBool();
    std::vector<CompiledAlpha> alphas;
    if (body.isMember("xmls")) {
        const auto& xmls = body["xmls"];
        if (!xmls.isArray() || xmls.empty() || xmls.size() > MAX_ALPHA_COUNT)
            throw std::invalid_argument("xmls must contain 1..5000 concrete alpha XML strings");
        for (const auto& value : xmls) {
            if (!value.isString() || value.asString().empty())
                throw std::invalid_argument("Every xmls entry must be a nonempty XML string");
            auto compiled = compile_request_xml(value.asString(), options);
            if (compiled.size() != 1)
                throw std::invalid_argument("Every xmls entry must compile to exactly one alpha");
            alphas.push_back(std::move(compiled[0]));
        }
    } else {
        const std::string xml = body["xml"].asString();
        if (xml.empty()) throw std::invalid_argument("Missing 'xml' field");
        alphas = compile_request_xml(xml, options);
    }
    if (alphas.empty()) throw std::invalid_argument("No alpha compiled from XML");
    for (const auto& alpha : alphas) require_history(alpha, options);
    const bool single = alphas.size() == 1;
    if (options.output == "signals" || options.output == "signalSeries") {
        if (!single || combined_only) throw std::invalid_argument("Signal output requires one concrete alpha XML");
        const auto held = bind_meta_sets(alphas[0], options, cache, cancelled);
        auto config = simulator_config(options, false);
        config.module_threads = shared_cache ? 1 : options.threads;
        Json::Value entry;
        entry["id"] = 0;
        entry["xml"] = alphas[0].source_xml;
        entry["status"] = "completed";
        if (options.output == "signalSeries") {
            std::vector<std::vector<float>> values;
            std::vector<int64_t> timestamps;
            std::vector<uint8_t> membership;
            Simulator(panel, config).run_signal_rows(alphas[0].pipeline, [&](unsigned di, const float* row, unsigned n) {
                if (cancelled()) throw EvaluationCancelled();
                if (di < options.sim_start_index) return;
                timestamps.push_back(g_data.timestamps[di]);
                auto& selected = values.emplace_back();
                selected.reserve(options.signal_columns.size());
                for (auto column : options.signal_columns) {
                    const bool kept = column != kSymbolNotInPanel;
                    selected.push_back(kept ? row[column] : std::numeric_limits<float>::quiet_NaN());
                    if (options.universe_mask && !options.universe_mask->empty())
                        membership.push_back(kept && (*options.universe_mask)[static_cast<size_t>(di) * n + column]);
                }
            });
            std::vector<std::size_t> columns;
            for (std::size_t i = 0; i < options.signal_symbols.size(); ++i) columns.push_back(i);
            entry["signals"]["plot"] = signal_plot_to_json(values, timestamps, options.signal_symbols, 0,
                options.signal_max_cells, &columns, membership.empty() ? nullptr : &membership);
        } else {
            std::vector<std::vector<float>> signals(options.sim_end_index);
            Simulator(panel, config).run_signal_rows(alphas[0].pipeline, [&](unsigned di, const float* row, unsigned n) {
                if (cancelled()) throw EvaluationCancelled();
                if (di >= options.sim_start_index) signals[di].assign(row, row + n);
            });
            std::vector<int64_t> timestamps(g_data.timestamps.begin(), g_data.timestamps.begin() + options.sim_end_index);
            append_signal_payload(entry["signals"], signals, timestamps, options);
        }
        Json::Value response;
        response["context"] = run_context(options);
        response["alphas"] = Json::Value(Json::arrayValue);
        response["alphas"].append(std::move(entry));
        response["combined"] = Json::nullValue;
        return response;
    }
    refresh_alpha_pool(options.threads);
    // Non-EW sets of an expanded batch: member returns once, one weighted pass for all specs.
    if (!single) (void)prefetch_meta_sets(meta_context(options, cache, cancelled), alphas);

    struct BatchResult {
        std::size_t index = 0;
        std::string xml;
        FullMetricsResult metrics;
        Json::Value diagnostics;
        Json::Value meta;  // [{name, members}] for alphas with <meta> sets, else null
        Json::Value pool_corr;  // correlation with the alpha pool (null without a pool)
    };
    std::vector<BatchResult> batches(alphas.size());
    SimulatorOutput single_output;
    std::vector<std::vector<float>> combined_signals;
    const auto stop_on_cancel = [&cancelled](unsigned, const float*, unsigned) {
        if (cancelled()) throw EvaluationCancelled();
    };
    if (single && !combined_only) {
        single_output = execute_alpha(alphas[0], options, options.output == "full", stop_on_cancel, accounting,
                                      cache, cancelled, shared_cache ? 1u : options.threads);
        batches[0] = {0, alphas[0].source_xml, full_metrics(single_output, options),
                      diagnostics_payload(single_output, options), {}};
        if (!alphas[0].meta_sets.empty()) batches[0].meta = meta_payload(alphas[0], cache);
        if (g_alpha_pool)
            batches[0].pool_corr = g_alpha_pool->correlation_json(single_output.ret, options.sim_start_index,
                                                                  options.sim_end_index);
    } else {
        OrderedSignalAccumulator accumulator(
            panel, options.missing_close_policy,
            options.universe_mask);
        const unsigned thread_count = std::min<unsigned>(options.threads, static_cast<unsigned>(alphas.size()));
        std::atomic<std::size_t> next_index{0};
        std::atomic<bool> failed{false};
        std::exception_ptr first_error;
        std::mutex error_mutex;
        auto worker = [&] {
            std::vector<double> normalized_scratch;
            while (!failed.load(std::memory_order_acquire)) {
                const std::size_t i = next_index.fetch_add(1);
                if (i >= alphas.size()) return;
                try {
                    std::vector<float> signal_block;
                    signal_block.reserve(static_cast<size_t>(32) * panel.N);
                    unsigned first_bar = 0;
                    auto flush = [&] {
                        if (signal_block.empty()) return;
                        accumulator.add_block(i, first_bar, signal_block, panel.N, normalized_scratch);
                        signal_block.clear();
                    };
                    auto observe = [&](unsigned di, const float* row, unsigned n) {
                        if (failed.load() || cancelled())
                            throw EvaluationCancelled();
                        if (signal_block.empty()) first_bar = di;
                        signal_block.insert(signal_block.end(), row, row + n);
                        if (signal_block.size() == static_cast<size_t>(32) * n) flush();
                    };
                    const auto stop = [&] { return failed.load() || cancelled(); };
                    if (combined_only) {
                        const auto meta_matrices = bind_meta_sets(alphas[i], options, cache, stop);
                        Simulator(panel, simulator_config(options, false)).run_signal_rows(alphas[i].pipeline, observe);
                    } else {
                        auto output = execute_alpha(alphas[i], options, false, observe, {}, cache, stop);
                        batches[i] = {i, alphas[i].source_xml, full_metrics(output, options),
                                      diagnostics_payload(output, options), {}};
                        if (!alphas[i].meta_sets.empty()) batches[i].meta = meta_payload(alphas[i], cache);
                        if (g_alpha_pool)   // computed here so batches correlate in parallel
                            batches[i].pool_corr = g_alpha_pool->correlation_json(
                                output.ret, options.sim_start_index, options.sim_end_index);
                    }
                    flush();
                    // Completed module state is no longer needed; selected plots
                    // are recomputed from the stored expanded XML after ranking.
                    alphas[i] = CompiledAlpha{};
                } catch (...) {
                    {
                        std::lock_guard<std::mutex> lock(error_mutex);
                        if (!first_error) first_error = std::current_exception();
                    }
                    failed.store(true, std::memory_order_release);
                    accumulator.cancel();
                    return;
                }
            }
        };
        std::vector<std::thread> workers;
        try {
            for (unsigned t = 0; t < thread_count; ++t)
                workers.emplace_back(worker);
        } catch (...) {
            failed.store(true, std::memory_order_release);
            accumulator.cancel();
            for (auto& worker_thread : workers) worker_thread.join();
            throw;
        }
        for (auto& worker_thread : workers) worker_thread.join();
        if (first_error) std::rethrow_exception(first_error);
        combined_signals = accumulator.take_signals();
    }

    std::stable_sort(batches.begin(), batches.end(), [](const auto& a, const auto& b) {
        const double a_sharpe = std::isfinite(a.metrics.is_summary.sharpe)
            ? a.metrics.is_summary.sharpe : -std::numeric_limits<double>::infinity();
        const double b_sharpe = std::isfinite(b.metrics.is_summary.sharpe)
            ? b.metrics.is_summary.sharpe : -std::numeric_limits<double>::infinity();
        return a_sharpe == b_sharpe ? a.index < b.index : a_sharpe > b_sharpe;
    });

    Json::Value response;
    response["context"] = run_context(options);
    response["alphas"] = Json::Value(Json::arrayValue);
    response["combined"] = Json::nullValue;
    for (std::size_t i = 0; !combined_only && i < batches.size(); ++i) {
        const auto& batch = batches[i];
        Json::Value entry = completed_alpha_json(batch.metrics, batch.diagnostics["missingData"], batch.index,
                                                 batch.xml, batch.meta, batch.pool_corr);
        if (single && options.output == "full")
            append_signal_payload(entry["signals"], single_output.signals, single_output.timestamps, options);
        response["alphas"].append(std::move(entry));
    }
    if (!single || combined_only) {
        if (cancelled()) throw EvaluationCancelled();
        Simulator combined_simulator(panel, simulator_config(options, false));
        auto combined_output = combined_simulator.run_from_signals(combined_signals);
        Json::Value combined = metrics_payload(full_metrics(combined_output, options));
        combined["missingData"] = missing_data_payload(combined_output);
        combined["id"] = -1;
        combined["status"] = "completed";
        if (g_alpha_pool) {
            const Json::Value correlation = g_alpha_pool->correlation_json(
                combined_output.ret, options.sim_start_index, options.sim_end_index);
            if (!correlation.isNull()) combined["poolCorrelation"] = correlation;
        }
        if (options.output == "full") {
            combined_signals.resize(combined_output.timestamps.size());
            append_signal_payload(combined["signals"], combined_signals, combined_output.timestamps, options);
        }
        response["combined"] = std::move(combined);
    }
    return response;
}

struct AlphaRequestError : std::runtime_error {
    AlphaRequestError(drogon::HttpStatusCode code, const std::string& message)
        : std::runtime_error(message), status(code) {}
    drogon::HttpStatusCode status;
};

static drogon::HttpResponsePtr json_error_response(drogon::HttpStatusCode status, const std::string& message) {
    Json::Value json;
    json["error"] = message;
    auto response = drogon::HttpResponse::newHttpJsonResponse(json);
    response->setStatusCode(status);
    return response;
}

// Saved files use the HTTP serializer settings so their bytes can be served as is.
static std::string json_text(const Json::Value& value) {
    Json::StreamWriterBuilder builder;
    builder["commentStyle"] = "None";
    builder["indentation"] = "";
    builder["emitUTF8"] = true;
    return Json::writeString(builder, value);
}

// Alpha storage requests run off the event loop; results saves replay every row.
template<class Work>
static void run_alpha_request(std::function<void(const drogon::HttpResponsePtr&)>&& callback, Work work) {
    auto respond = std::make_shared<std::function<void(const drogon::HttpResponsePtr&)>>(std::move(callback));
    try {
        g_alpha_workers.launch([respond, work = std::move(work)]() mutable {
            drogon::HttpResponsePtr response;
            try {
                response = work();
            } catch (const AlphaRequestError& error) {
                response = json_error_response(error.status, error.what());
            } catch (const std::exception& error) {
                response = json_error_response(drogon::k500InternalServerError, error.what());
            }
            (*respond)(response);
        });
    } catch (const std::exception& error) {
        (*respond)(json_error_response(drogon::k503ServiceUnavailable, error.what()));
    }
}

// The /api/simulate output=performance body of a GA candidate, from what its evaluation kept.
static drogon::HttpResponsePtr ga_candidate_response(const std::string& run_id, const std::string& id) {
    const bool numeric = !id.empty() && id.size() <= 19 &&
        std::all_of(id.begin(), id.end(), [](unsigned char ch) { return std::isdigit(ch); });
    const auto found = numeric ? g_ga_runs.find(run_id, std::stoull(id)) : GaRunStore::Found{};
    if (!found.entry) throw AlphaRequestError(drogon::k404NotFound, "GA candidate result is not available");
    refresh_alpha_pool(found.run->threads);
    const Json::Value pool_corr = g_alpha_pool
        ? g_alpha_pool->correlation_json(found.entry->net_returns, found.run->sim_start_index, found.run->sim_end_index)
        : Json::Value();
    Json::Value response;
    response["context"] = found.run->context;
    response["alphas"].append(completed_alpha_json(found.entry->metrics, found.entry->missing_data, 0,
                                                   found.entry->xml, found.entry->meta, pool_corr));
    response["combined"] = Json::nullValue;
    return drogon::HttpResponse::newHttpJsonResponse(response);
}

static constexpr Json::UInt64 kMaxRowId = (Json::UInt64(1) << 53) - 1; // JavaScript-safe

static const char* const kAlphaMetadataKeys[] = {"simStart", "simEnd", "isStart", "osStart", "universe",
                                                 "executionPrice"};

static Json::Value metadata_json(const alpha_storage::Metadata& metadata) {
    Json::Value json;
    json["simStart"] = metadata.sim_start;
    json["simEnd"] = metadata.sim_end;
    json["isStart"] = metadata.is_start;
    json["osStart"] = metadata.os_start;
    json["universe"] = metadata.universe;
    json["executionPrice"] = metadata.execution_price;
    json["threads"] = metadata.threads;
    return json;
}

static drogon::HttpResponsePtr list_alphas_response() {
    Json::Value list(Json::arrayValue);
    std::vector<std::string> skipped;
    for (const auto& alpha : alpha_storage::list(g_alphas_dir, &skipped)) {
        Json::Value item;
        item["name"] = alpha.name;
        item["xml"] = alpha.xml;
        item["level"] = alpha.metadata.level;
        item["metadata"] = metadata_json(alpha.metadata);
        list.append(std::move(item));
    }
    for (const auto& name : skipped)
        LOG_WARN << "Skipping saved alpha with malformed metadata.yml: " << name;
    return drogon::HttpResponse::newHttpJsonResponse(list);
}

static drogon::HttpResponsePtr saved_result_response(const std::string& name, const std::string& file) {
    if (!alpha_storage::is_valid_name(name))
        throw AlphaRequestError(drogon::k400BadRequest, "Invalid alpha name");
    std::string text;
    if (!alpha_storage::read_file(g_alphas_dir / name / "results" / file, &text))
        throw AlphaRequestError(drogon::k404NotFound, "Saved result not found: " + name + "/results/" + file);
    auto response = drogon::HttpResponse::newHttpResponse();
    response->setContentTypeCode(drogon::CT_APPLICATION_JSON);
    response->setBody(std::move(text));
    return response;
}

static drogon::HttpResponsePtr save_alpha(const Json::Value& body) {
    auto invalid = [](const std::string& message) { return AlphaRequestError(drogon::k400BadRequest, message); };
    if (!body.isObject()) throw invalid("Request body must be a JSON object");
    if (!body["name"].isString() || !alpha_storage::is_valid_name(body["name"].asString()))
        throw invalid("Invalid alpha name");
    const std::string name = body["name"].asString();
    if (!body["xml"].isString() || body["xml"].asString().empty()) throw invalid("Missing 'xml' field");
    const std::string xml = body["xml"].asString();
    const Json::Value& level_value = body.isMember("level") ? body["level"] : Json::Value("xml");
    if (!level_value.isString() || !alpha_storage::is_valid_level(level_value.asString()))
        throw invalid("level must be 'xml', 'results' or 'positions'");
    alpha_storage::Metadata metadata;
    metadata.level = level_value.asString();
    const Json::Value& meta = body["metadata"];
    if (!meta.isObject()) throw invalid("Missing 'metadata' object");
    Json::Value run_body(Json::objectValue);
    for (const char* key : kAlphaMetadataKeys) {
        if (!meta[key].isString()) throw invalid(std::string("metadata.") + key + " must be a string");
        if (meta[key].asString().empty() && std::string(key) != "universe")
            throw invalid(std::string("metadata.") + key + " must not be empty");
        run_body[key] = meta[key];
    }
    metadata.sim_start = meta["simStart"].asString();
    metadata.sim_end = meta["simEnd"].asString();
    metadata.is_start = meta["isStart"].asString();
    metadata.os_start = meta["osStart"].asString();
    metadata.universe = meta["universe"].asString();
    metadata.execution_price = meta["executionPrice"].asString();
    if (meta.isMember("threads")) run_body["threads"] = meta["threads"];
    else if (metadata.level != "xml") throw invalid("metadata.threads is required for saved results");
    RunOptions options;
    try {
        options = parse_run_options(run_body);
    } catch (const std::exception& error) {
        throw invalid(std::string("metadata: ") + error.what());
    }

    metadata.threads = options.threads;
    if (metadata.level != "xml") attach_run_panel(options);

    struct Row { Json::UInt64 id; Json::Value round; std::string xml; };
    std::vector<Row> rows;
    if (metadata.level != "xml") {
        const Json::Value& items = body["rows"];
        if (!items.isArray() || items.empty() || items.size() > MAX_ALPHA_COUNT)
            throw invalid("rows must contain 1..5000 entries for results and positions");
        std::set<Json::UInt64> ids;
        for (const auto& item : items) {
            if (!item.isObject() || !item["id"].isIntegral() || !item["id"].isUInt64() ||
                item["id"].asUInt64() > kMaxRowId || !ids.insert(item["id"].asUInt64()).second)
                throw invalid("Every row needs a unique integer id in 0..2^53-1");
            const Json::Value round = item.get("round", 0);
            if (!round.isIntegral() || !round.isUInt())
                throw invalid("Row round must be a nonnegative integer");
            if (!item["xml"].isString() || item["xml"].asString().empty())
                throw invalid("Every row needs a nonempty xml string");
            std::vector<CompiledAlpha> compiled;
            try {
                compiled = compile_request_xml(item["xml"].asString(), options);
                if (compiled.size() != 1)
                    throw std::invalid_argument("must compile to exactly one alpha");
            } catch (const std::exception& error) {
                throw invalid("Row " + std::to_string(item["id"].asUInt64()) + " xml: " + error.what());
            }
            try {
                require_history(compiled[0], options);
            } catch (const std::exception& error) {
                throw invalid(error.what());
            }
            rows.push_back({item["id"].asUInt64(), round, item["xml"].asString()});
        }
    }
    if (alpha_storage::exists(g_alphas_dir, name))
        throw AlphaRequestError(drogon::k409Conflict, "Alpha already exists: " + name);

    std::string formatted_xml;
    try {
        formatted_xml = format_xml(xml);
    } catch (const std::exception& error) {
        throw invalid(error.what());
    }
    alpha_storage::SaveTransaction save(g_alphas_dir, name);
    save.write("alpha.xml", formatted_xml);
    save.write("metadata.yml", alpha_storage::metadata_yaml(metadata));
    if (!rows.empty()) {
        const auto cancelled = [] { return g_alpha_workers.stopping(); };
        const bool positions = metadata.level == "positions";
        MetaSetCache meta_cache(options.threads);  // shared by every row replay and the batch replay of this save
        std::vector<Json::Value> entries(rows.size());
        // The request's threads (1..32, default 20) bound the row replays and the batch replay below.
        const unsigned thread_count = std::min<unsigned>(options.threads, static_cast<unsigned>(rows.size()));
        std::atomic<std::size_t> next_index{0};
        std::atomic<bool> failed{false};
        std::exception_ptr first_error;
        std::mutex error_mutex;
        // Each row writes its own files; the index keeps request order.
        auto worker = [&] {
            while (!failed.load(std::memory_order_acquire)) {
                const std::size_t i = next_index.fetch_add(1);
                if (i >= rows.size()) return;
                try {
                    if (cancelled()) throw EvaluationCancelled();
                    const auto& row = rows[i];
                    Json::Value request = run_body;
                    request["xml"] = row.xml;
                    request["output"] = "performance";
                    const std::string id = std::to_string(row.id);
                    // Positions are the order targets (desired_weights) on trading bars,
                    // captured in the same run that produces the row result.
                    std::unique_ptr<alpha_storage::PositionsWriter> writer;
                    SimulatorConfig::AccountingRowCallback accounting;
                    if (positions) {
                        writer = std::make_unique<alpha_storage::PositionsWriter>(options.panel->symbols);
                        accounting = [&writer](const AccountingRowView& view) {
                            if (view.trading_enabled)
                                writer->add_bar(g_data.timestamps[view.bar], view.desired_weights);
                        };
                    }
                    Json::Value response = simulate_response(
                        request, [&] { return failed.load() || cancelled(); }, accounting, &meta_cache);
                    response["alphas"][0]["id"] = row.id;
                    response["alphas"][0]["round"] = row.round;
                    response["alphas"][0]["origin"] = "saved";
                    save.write(fs::path("results") / "rows" / (id + ".json"), json_text(response));
                    if (writer) writer->write(save.prepare(fs::path("positions") / (id + ".parquet")));
                    Json::Value entry;
                    entry["id"] = row.id;
                    entry["round"] = row.round;
                    entry["xml"] = row.xml;
                    entry["origin"] = "saved";
                    entry["status"] = "completed";
                    entry["summary"] = response["alphas"][0]["summary"];
                    entry["yearly"] = response["alphas"][0]["yearly"];
                    entries[i] = std::move(entry);
                } catch (...) {
                    {
                        std::lock_guard<std::mutex> lock(error_mutex);
                        if (!first_error) first_error = std::current_exception();
                    }
                    failed.store(true, std::memory_order_release);
                    return;
                }
            }
        };
        std::vector<std::thread> workers;
        try {
            for (unsigned t = 0; t < thread_count; ++t) workers.emplace_back(worker);
        } catch (...) {
            failed.store(true, std::memory_order_release);
            for (auto& thread : workers) thread.join();
            throw;
        }
        for (auto& thread : workers) thread.join();
        if (first_error) std::rethrow_exception(first_error);
        if (cancelled()) throw EvaluationCancelled();

        Json::Value index;
        index["context"] = run_context(options);
        index["rows"] = Json::Value(Json::arrayValue);
        Json::Value batch_request = run_body;
        batch_request["xmls"] = Json::Value(Json::arrayValue);
        batch_request["combinedOnly"] = true;
        batch_request["output"] = "performance";
        batch_request["threads"] = options.threads;
        for (std::size_t i = 0; i < rows.size(); ++i) {
            index["rows"].append(std::move(entries[i]));
            batch_request["xmls"].append(rows[i].xml);
        }
        index["hasBatch"] = rows.size() >= 2;
        if (rows.size() >= 2)
            save.write(fs::path("results") / "batch.json", json_text(simulate_response(batch_request, cancelled, {}, &meta_cache)));
        save.write(fs::path("results") / "index.json", json_text(index));
    }
    if (!save.commit())
        throw AlphaRequestError(drogon::k409Conflict, "Alpha already exists: " + name);

    Json::Value json;
    json["name"] = name;
    json["saved"] = true;
    json["level"] = metadata.level;
    json["path"] = (g_alphas_dir / name).string();
    return drogon::HttpResponse::newHttpJsonResponse(json);
}

int run_http_server(int argc, char* argv[])
{
    // Config path is required; there is no repo-root default.
    if (argc < 2) {
        std::cerr << "usage: longshort-server <config.yaml>" << std::endl;
        return 2;
    }
    std::string config_path = argv[1];

    // Load config + data. A bad config or an unreadable dataset is a startup
    // failure with a readable message, not an uncaught exception.
    fs::path cfg_dir;
    try {
        std::cout << "Loading config from " << config_path << " ..." << std::endl;
        g_config = BacktestConfig::from_yaml(config_path);

        cfg_dir = fs::path(config_path).parent_path();
        std::cout << "Loading cache ..." << std::endl;
        g_data = DataPanel::load(g_config);
        std::cout << "Data loaded: T=" << g_data.T << " N=" << g_data.N << std::endl;
        if (!g_config.symbol_labels_path.empty()) {
            std::ifstream in(g_config.symbol_labels_path);
            if (!in) throw std::runtime_error("Cannot open symbol_labels_path: " + g_config.symbol_labels_path);
            Json::Value all;
            in >> all;
            if (!all.isObject()) throw std::runtime_error("symbol_labels_path must hold a JSON object");
            std::size_t matched = 0;
            for (const auto& sym : g_data.symbols)
                if (all.isMember(sym)) { g_symbol_labels[sym] = all[sym]; ++matched; }
            std::cout << "Symbol labels: " << matched << "/" << g_data.N << " panel symbols labeled" << std::endl;
        } else {
            const auto labels = loader::load_thin_symbol_labels(g_config, g_data.symbols);
            for (const auto& [id, label] : labels) g_symbol_labels[id] = label;
            std::cout << "Symbol labels: " << labels.size() << "/" << g_data.N
                      << " panel symbols labeled from thin master" << std::endl;
        }
    } catch (const std::exception& error) {
        std::cerr << "Cannot start: " << error.what() << std::endl;
        return 1;
    }

    // config_loader rejects any other spelling before the panel loads.
    if (!parse_missing_close_policy(g_config.missing_close_policy, &g_missing_close_policy)) {
        std::cerr << "Unsupported missing_close_policy: "
                  << g_config.missing_close_policy << std::endl;
        return 1;
    }
    g_alphas_dir = g_config.alphas_dir;
    g_meta_dir = g_config.meta_dir;
    if (!g_config.alpha_pool.empty()) {
        // Pool alphas are simulated now, in parallel, so the first request already has the Corr column.
        g_alpha_pool = std::make_unique<alpha_pool::Cache>(fs::path(g_config.alpha_pool));
        if (!fs::is_directory(g_alpha_pool->dir()))
            std::cerr << "alpha_pool: directory not found, pool correlation stays empty: " << g_config.alpha_pool << std::endl;
        try {
            (void)g_data.mask_for(pool_universe());
        } catch (const std::exception& error) {
            std::cerr << "Cannot start: alpha_pool_universe: " << error.what() << std::endl;
            return 1;
        }
        const auto started = std::chrono::steady_clock::now();
        try {
            refresh_alpha_pool(std::clamp(std::thread::hardware_concurrency(), 1u, 8u));
        } catch (const std::exception& error) {
            std::cerr << "Cannot start: alpha_pool " << error.what() << std::endl;
            return 1;
        }
        const double seconds = std::chrono::duration<double>(std::chrono::steady_clock::now() - started).count();
        std::cout << "Alpha pool: " << g_alpha_pool->size() << " alpha(s) from " << g_config.alpha_pool << " ("
                  << pool_universe() << ", " << g_config.execution_price << ") in " << std::fixed
                  << std::setprecision(1) << seconds << "s" << std::endl;
    }
    try {
        alpha_storage::prepare_directory(g_alphas_dir);
    } catch (const std::exception& error) {
        std::cerr << "Cannot start: " << error.what() << std::endl;
        return 1;
    }

    // CORS: allow any origin (React frontend)
    drogon::app().registerPreRoutingAdvice(
        [](const drogon::HttpRequestPtr &req,
           drogon::FilterCallback &&stop,
           drogon::FilterChainCallback &&pass) {
            if (req->method() == drogon::Options) {
                auto resp = drogon::HttpResponse::newHttpResponse();
                resp->addHeader("Access-Control-Allow-Origin", "*");
                resp->addHeader("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS");
                resp->addHeader("Access-Control-Allow-Headers", "Content-Type");
                resp->addHeader("Access-Control-Max-Age", "86400");
                stop(resp);
                return;
            }
            pass();
        });

    drogon::app().registerPostHandlingAdvice(
        [](const drogon::HttpRequestPtr &, const drogon::HttpResponsePtr &resp) {
            resp->addHeader("Access-Control-Allow-Origin", "*");
        });

    // GET /api/health
    drogon::app().registerHandler(
        "/api/health",
        [](const drogon::HttpRequestPtr &,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            Json::Value json;
            json["status"] = "ok";
            json["engine"] = "cpp-engine";
            callback(drogon::HttpResponse::newHttpJsonResponse(json));
        },
        {drogon::Get});

    // GET /api/config
    drogon::app().registerHandler(
        "/api/config",
        [](const drogon::HttpRequestPtr &,
           std::function<void(const drogon::HttpResponsePtr &)> &&callback) {
            Json::Value json;

            // Symbols
            Json::Value symbols(Json::arrayValue);
            for (const auto& s : g_data.symbols)
                symbols.append(s);
            json["symbols"] = symbols;
            if (!g_symbol_labels.isNull())
                json["symbolLabels"] = g_symbol_labels;  // {panelSymbol: display label}

            // Date range (epoch_ms -> ISO date string)
            // First and last timestamps from DataPanel
            auto ms_to_date = [](int64_t ms) -> std::string {
                time_t sec = ms / 1000;
                struct tm t;
                gmtime_r(&sec, &t);
                char buf[11];
                strftime(buf, sizeof(buf), "%Y-%m-%d", &t);
                return buf;
            };

            if (g_data.timestamps.empty()) {
                Json::Value err;
                err["error"] = "Data panel has no timestamps";
                auto resp = drogon::HttpResponse::newHttpJsonResponse(err);
                resp->setStatusCode(drogon::k500InternalServerError);
                callback(resp);
                return;
            }

            json["simStart"] = ms_to_date(g_data.timestamps.front());
            json["simEnd"]   = ms_to_date(g_data.timestamps.back());
            json["feeRate"]  = g_config.transaction_fee_rate;
            json["reportNotional"] = kReportNotional;
            json["barsPerYear"] = g_config.bars_per_year;
            json["executionPrice"] = g_config.execution_price;
            json["executionPrices"] = Json::Value(Json::arrayValue);
            json["executionPrices"].append("close");
            if (supports_ohlc4()) json["executionPrices"].append("ohlc4");
            if (supports_vwap()) json["executionPrices"].append("vwap");
            json["missingClosePolicy"] = missing_close_policy_name(g_missing_close_policy);
            json["thinLayout"] = g_data.thin_layout;
            json["alphasDir"] = g_alphas_dir.string();
            json["metaDir"] = g_meta_dir.string();
            json["metaNames"] = Json::Value(Json::arrayValue);
            for (const auto& name : meta_dir::names(g_meta_dir)) json["metaNames"].append(name);
            Json::Value universes(Json::arrayValue);
            for (const auto& name : g_data.universe_names) universes.append(name);
            json["universes"] = universes;  // empty: a single configured universe
            json["defaultUniverse"] = g_data.default_universe;
            json["alphaPool"] = g_alpha_pool ? Json::Value(g_alpha_pool->dir().string()) : Json::Value();
            json["alphaPoolSize"] = static_cast<Json::UInt64>(g_alpha_pool ? g_alpha_pool->size() : 0);
            json["alphaPoolUniverse"] = g_alpha_pool ? Json::Value(pool_universe()) : Json::Value();
            json["T"] = g_data.T;
            json["N"] = g_data.N;

            // Available columns
            Json::Value columns(Json::arrayValue);
            for (const auto& c : g_data.column_names())
                columns.append(c);
            json["columns"] = columns;
            // Resident float-field storage only: excludes masks, modules, plots and process overhead.
            json["loadedFields"] = static_cast<Json::UInt64>(g_data.loaded_field_count());
            json["loadedDataBytes"] = static_cast<Json::UInt64>(g_data.loaded_bytes());

            callback(drogon::HttpResponse::newHttpJsonResponse(json));
        },
        {drogon::Get});

    // POST /api/simulate — one or more expanded alphas
    drogon::app().registerHandler(
        "/api/simulate",
        [](const drogon::HttpRequestPtr& req,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback) {
            // Evaluate off the event loop: a long combined replay must not stall
            // other requests whose connections share this loop.
            auto respond = std::make_shared<std::function<void(const drogon::HttpResponsePtr&)>>(
                std::move(callback));
            try {
                g_simulate_workers.launch([req, respond] {
                    const auto& callback = *respond;
                    try {
                        const auto body = req->getJsonObject();
                        if (!body) throw std::invalid_argument("Invalid JSON body");
                        const Json::Value response = simulate_response(
                            *body, [&req] { return g_simulate_workers.stopping() || !req->connected(); });
                        callback(drogon::HttpResponse::newHttpJsonResponse(response));
                    } catch (const std::exception& error) {
                        Json::Value json;
                        json["error"] = error.what();
                        auto response = drogon::HttpResponse::newHttpJsonResponse(json);
                        response->setStatusCode(drogon::k400BadRequest);
                        callback(response);
                    }
                });
            } catch (const std::exception& error) {
                Json::Value json;
                json["error"] = error.what();
                auto response = drogon::HttpResponse::newHttpJsonResponse(json);
                response->setStatusCode(drogon::k503ServiceUnavailable);
                (*respond)(response);
            }
        }, {drogon::Post});

    // POST /api/ga/stream — final best alpha uses the same period and plot contract
    drogon::app().registerHandler(
        "/api/ga/stream",
        [](const drogon::HttpRequestPtr& req,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback) {
            try {
                const auto body = req->getJsonObject();
                if (!body) throw std::invalid_argument("Invalid JSON body");
                const std::string xml = (*body)["xml"].asString();
                if (xml.empty()) throw std::invalid_argument("Missing 'xml' field");
                const RunOptions options = parse_run_options(*body);
                GAConfig ga_config;
                ga_config.first_search = parse_uint_option(*body, "firstSearch", 200);
                ga_config.population_size = parse_uint_option(*body, "roundSize", 50);
                ga_config.generations = parse_uint_option(*body, "stopRound", 40);
                ga_config.stop_alphas = parse_uint_option(*body, "stopAlphas", 400);
                ga_config.n_threads = options.threads;
                if (!ga_config.first_search || ga_config.first_search > 5000 ||
                    !ga_config.population_size || ga_config.population_size > 5000 ||
                    !ga_config.generations || ga_config.generations > 1000 ||
                    !ga_config.stop_alphas || ga_config.stop_alphas > 5000)
                    throw std::invalid_argument("GA search/round size and stopAlphas must be 1..5000, stopRound 1..1000");
                const bool meta_stats = body->get("metaStats", false).asBool();
                auto response = drogon::HttpResponse::newAsyncStreamResponse(
                    [xml, parsed = options, ga_config, req, meta_stats](drogon::ResponseStreamPtr stream) {
                        // The I/O loop must remain free to receive cancellation and
                        // flush generation events while the GA controller is running.
                        try {
                            g_ga_workers.launch([xml, parsed, ga_config, req, meta_stats, stream = std::move(stream)]() mutable {
                                auto require_connection = [&] {
                                    if (g_ga_workers.stopping() || !req->connected()) throw ClientDisconnected();
                                };
                                auto write = [&stream, &require_connection](const Json::Value& value) {
                                    require_connection();
                                    Json::StreamWriterBuilder builder;
                                    builder["indentation"] = "";
                                    if (!stream->send(Json::writeString(builder, value) + "\n"))
                                        throw ClientDisconnected();
                                };
                                try {
                                    RunOptions options = parsed;
                                    attach_run_panel(options);  // held until the search ends
                                    const auto space = extract_search_space(xml);
                                    std::mutex payload_mutex;
                                    std::unordered_map<std::string, Json::Value> payloads;
                                    std::unordered_map<std::string, GaCandidateEntry> entries;
                                    const std::string run_id = g_ga_runs.begin_run(
                                        {run_context(options), options.sim_start_index, options.sim_end_index, options.threads});
                                    struct FinishRun {
                                        const std::string& id;
                                        ~FinishRun() { g_ga_runs.finish_run(id); }
                                    } finish_run{run_id};
                                    Json::Value start_event;
                                    start_event["type"] = "search_start";
                                    start_event["runId"] = run_id;
                                    start_event["context"] = run_context(options);
                                    write(start_event);
                                    // One set cache for the whole search: the first candidate needing a set
                                    // materializes it with options.threads member workers, the others wait.
                                    MetaSetCache meta_cache(options.threads);
                                    const auto disconnected = [&] { return g_ga_workers.stopping() || !req->connected(); };
                                    auto evaluate = [&](const std::string& candidate_xml) {
                                        require_connection();
                                        auto alphas = compile_request_xml(candidate_xml, options);
                                        if (alphas.size() != 1) throw std::runtime_error("GA candidate must compile to exactly one alpha");
                                        require_history(alphas[0], options);
                                        auto output = execute_alpha(alphas[0], options, false,
                                            [&](unsigned, const float*, unsigned) { require_connection(); }, {},
                                            meta_cache, disconnected);
                                        auto metrics = full_metrics(output, options);
                                        const double sharpe = metrics.is_summary.sharpe;
                                        {
                                            Json::Value payload;
                                            payload["summary"]["is"] = summary_to_json(metrics.is_summary);
                                            payload["summary"]["os"] = summary_to_json(metrics.os_summary);
                                            payload["yearly"] = yearly_to_json(metrics.yearly);
                                            payload["missingData"] = missing_data_payload(output);
                                            if (!alphas[0].meta_sets.empty()) payload["meta"] = meta_payload(alphas[0], meta_cache);
                                            GaCandidateEntry entry;
                                            entry.xml = alphas[0].source_xml;
                                            entry.metrics = std::move(metrics);
                                            entry.missing_data = payload["missingData"];
                                            entry.meta = payload.get("meta", Json::Value());
                                            if (g_alpha_pool) entry.net_returns = std::move(output.ret);
                                            std::lock_guard<std::mutex> lock(payload_mutex);
                                            payloads.emplace(candidate_xml, std::move(payload));
                                            entries.emplace(candidate_xml, std::move(entry));
                                        }
                                        return sharpe;
                                    };
                                    std::mutex outcome_mutex;
                                    bool any_completed = false;
                                    std::size_t first_failed_id = std::numeric_limits<std::size_t>::max();
                                    std::string first_error;
                                    GAEvents events;
                                    events.cancelled = [&] { return g_ga_workers.stopping() || !req->connected(); };
                                    // Non-EW set matrices live for one round: drop the previous round's, then
                                    // compute this round's with one member-return and one weighted pass per set.
                                    // Runs on this controller thread while candidate workers are idle.
                                    std::vector<std::string> round_meta_keys;
                                    events.round_prepare = [&](const std::vector<std::string>& round_xmls) {
                                        meta_cache.evict(round_meta_keys);
                                        round_meta_keys.clear();
                                        std::vector<CompiledAlpha> round_alphas;
                                        for (const auto& round_xml : round_xmls) {
                                            // EW-only candidates stay on the lazy path.
                                            static const std::regex non_ew(
                                                R"(<(mvo|minvar|erc|ivol|spca|glasso)[\s/>]|<ew\s[^>]*sign=)");
                                            if (!std::regex_search(round_xml, non_ew)) continue;
                                            try {
                                                auto compiled = compile_request_xml(round_xml, options);
                                                for (const auto& alpha : compiled) require_history(alpha, options);
                                                for (auto& alpha : compiled) round_alphas.push_back(std::move(alpha));
                                            } catch (const std::exception&) {
                                                // The candidate reports its own compile error.
                                            }
                                        }
                                        round_meta_keys = prefetch_meta_sets(meta_context(options, meta_cache, disconnected),
                                                                             round_alphas);
                                    };
                                    events.round_start = [&](unsigned round, size_t count) {
                                        Json::Value event;
                                        event["type"] = "round_start";
                                        event["round"] = round;
                                        event["size"] = static_cast<Json::UInt64>(count);
                                        write(event);
                                    };
                                    events.candidate = [&](const GACandidate& candidate) {
                                        {
                                            std::lock_guard<std::mutex> lock(outcome_mutex);
                                            if (candidate.error.empty()) any_completed = true;
                                            else if (candidate.id < first_failed_id) {
                                                first_failed_id = candidate.id;
                                                first_error = candidate.error;
                                            }
                                        }
                                        Json::Value payload;
                                        if (candidate.error.empty()) {
                                            GaCandidateEntry entry;
                                            {
                                                std::lock_guard<std::mutex> lock(payload_mutex);
                                                auto found = payloads.find(candidate.xml);
                                                if (found == payloads.end()) throw std::runtime_error("Missing evaluated candidate payload");
                                                payload = std::move(found->second);
                                                payloads.erase(found);
                                                auto kept = entries.find(candidate.xml);
                                                entry = std::move(kept->second);
                                                entries.erase(kept);
                                            }
                                            g_ga_runs.put(run_id, candidate.id, std::move(entry));  // before the row can be clicked
                                        }
                                        payload["id"] = static_cast<Json::UInt64>(candidate.id);
                                        payload["xml"] = candidate.xml;
                                        payload["round"] = candidate.round;
                                        payload["origin"] = candidate.origin;
                                        payload["status"] = candidate.error.empty() ? "completed" : "failed";
                                        if (!candidate.error.empty()) payload["error"] = candidate.error;
                                        Json::Value event;
                                        event["type"] = "candidate";
                                        event["candidate"] = std::move(payload);
                                        write(event);
                                    };
                                    events.round_end = [&](unsigned round, const std::string& best_xml, double score) {
                                        Json::Value event;
                                        event["type"] = "round_end";
                                        event["round"] = round;
                                        event["bestSharpe"] = std::isfinite(score) ? Json::Value(score) : Json::Value(Json::nullValue);
                                        event["bestXml"] = best_xml;
                                        write(event);
                                    };
                                    GAEngine engine(xml, space, ga_config);
                                    const auto best = engine.run_detailed(evaluate, events);
                                    if (!any_completed && !first_error.empty()) throw std::runtime_error(first_error);
                                    Json::Value event;
                                    event["type"] = "search_complete";
                                    event["bestXml"] = best.best_xml;
                                    event["stopReason"] = best.stop_reason;
                                    event["evaluated"] = static_cast<Json::UInt64>(best.evaluated);
                                    if (meta_stats) {
                                        const auto stats = meta_cache.stats();
                                        event["metaStats"]["materializations"] = static_cast<Json::UInt64>(stats.materializations);
                                        event["metaStats"]["memberRuns"] = static_cast<Json::UInt64>(stats.member_runs);
                                    }
                                    write(event);
                                } catch (const ClientDisconnected&) {
                                    // The caller has cancelled; do not evaluate another generation.
                                } catch (const std::exception& error) {
                                    Json::Value event;
                                    event["type"] = "error";
                                    event["error"] = error.what();
                                    try { write(event); } catch (const ClientDisconnected&) {}
                                }
                                stream->close();
                            });
                        } catch (const std::exception& error) {
                            // A failed launch destroys the owned stream and closes it.
                            LOG_ERROR << "Unable to start GA worker: " << error.what();
                        }
                    }, true);
                response->addHeader("Content-Type", "application/x-ndjson");
                callback(response);
            } catch (const std::exception& error) {
                Json::Value json;
                json["error"] = error.what();
                auto response = drogon::HttpResponse::newHttpJsonResponse(json);
                response->setStatusCode(drogon::k400BadRequest);
                callback(response);
            }
        }, {drogon::Post});

    // GET /api/ga/runs/{runId}/candidates/{id}/performance — a GA row click; 404 once the run is evicted
    drogon::app().registerHandler(
        "/api/ga/runs/{runId}/candidates/{id}/performance",
        [](const drogon::HttpRequestPtr&,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback,
           const std::string& run_id, const std::string& id) {
            run_alpha_request(std::move(callback), [run_id, id] { return ga_candidate_response(run_id, id); });
        },
        {drogon::Get});

    // GET/POST /api/alphas — list or save alphas
    drogon::app().registerHandler(
        "/api/alphas",
        [](const drogon::HttpRequestPtr& req,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback) {
            run_alpha_request(std::move(callback), [req] {
                if (req->method() == drogon::Get) return list_alphas_response();
                const auto body = req->getJsonObject();
                if (!body) throw AlphaRequestError(drogon::k400BadRequest, "Invalid JSON body");
                return save_alpha(*body);
            });
        },
        {drogon::Get, drogon::Post});

    // DELETE /api/alphas/{name}
    drogon::app().registerHandler(
        "/api/alphas/{name}",
        [](const drogon::HttpRequestPtr&,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback,
           const std::string& name) {
            run_alpha_request(std::move(callback), [name] {
                if (!alpha_storage::is_valid_name(name))
                    throw AlphaRequestError(drogon::k400BadRequest, "Invalid alpha name");
                if (!alpha_storage::remove(g_alphas_dir, name))
                    throw AlphaRequestError(drogon::k404NotFound, "Alpha not found: " + name);
                Json::Value json;
                json["name"] = name;
                json["deleted"] = true;
                return drogon::HttpResponse::newHttpJsonResponse(json);
            });
        },
        {drogon::Delete});

    // GET /api/alphas/{name}/results[/batch|/rows/{id}] — saved result files
    drogon::app().registerHandler(
        "/api/alphas/{name}/results",
        [](const drogon::HttpRequestPtr&,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback,
           const std::string& name) {
            run_alpha_request(std::move(callback), [name] { return saved_result_response(name, "index.json"); });
        },
        {drogon::Get});
    drogon::app().registerHandler(
        "/api/alphas/{name}/results/batch",
        [](const drogon::HttpRequestPtr&,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback,
           const std::string& name) {
            run_alpha_request(std::move(callback), [name] { return saved_result_response(name, "batch.json"); });
        },
        {drogon::Get});
    drogon::app().registerHandler(
        "/api/alphas/{name}/results/rows/{id}",
        [](const drogon::HttpRequestPtr&,
           std::function<void(const drogon::HttpResponsePtr&)>&& callback,
           const std::string& name, const std::string& id) {
            run_alpha_request(std::move(callback), [name, id] {
                const bool numeric = !id.empty() && id.size() <= 19 &&
                    std::all_of(id.begin(), id.end(), [](unsigned char ch) { return std::isdigit(ch); });
                if (!numeric || std::stoull(id) > kMaxRowId)
                    throw AlphaRequestError(drogon::k400BadRequest, "Invalid row id");
                return saved_result_response(name, "rows/" + std::to_string(std::stoull(id)) + ".json");
            });
        },
        {drogon::Get});

    const unsigned listen_port = parse_env_uint("LONGSHORT_PORT", 8000, 1, 65535);

    std::cout << "LongShort C++ server starting on port " << listen_port << "..." << std::endl;

    drogon::app()
        .setUploadPath((cfg_dir / ".longshort" / "uploads").string())
        .setLogLevel(trantor::Logger::kWarn)
        .addListener("0.0.0.0", static_cast<uint16_t>(listen_port))
        .setThreadNum(4)
        // Long saves and combined replays answer after minutes; keep the connection bounded but open.
        .setIdleConnectionTimeout(parse_env_uint("LONGSHORT_IDLE_TIMEOUT", 3600, 1, 86400))
        .run();

    g_ga_workers.stop_and_join();
    g_simulate_workers.stop_and_join();
    g_alpha_workers.stop_and_join();
    return 0;
}
