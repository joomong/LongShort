#include "loader.h"
#include <yaml-cpp/yaml.h>
#include <algorithm>
#include <cmath>
#include <ctime>
#include <filesystem>
#include <initializer_list>
#include <limits>
#include <regex>
#include <stdexcept>
#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/reader.h>
#include <parquet/file_reader.h>
#include <map>
#include <mutex>
#include <numeric>
#include <set>
#include <unordered_map>
#include <unordered_set>

namespace {
unsigned unsigned_option(const YAML::Node& node, unsigned fallback, bool positive) {
    if (!node) return fallback;
    const auto value = node.as<long long>();
    if (value < (positive ? 1 : 0) || static_cast<unsigned long long>(value) > std::numeric_limits<unsigned>::max())
        throw std::runtime_error("Integer config option is outside the supported range");
    return static_cast<unsigned>(value);
}
void require_known_keys(const YAML::Node& node, const std::string& prefix,
                        std::initializer_list<const char*> allowed) {
    if (!node || !node.IsMap()) return;
    for (const auto& entry : node) {
        const auto key = entry.first.as<std::string>();
        if (std::none_of(allowed.begin(), allowed.end(), [&](const char* name) { return key == name; }))
            throw std::runtime_error("Unknown config key '" + prefix + key + "'");
    }
}
}

int64_t config_date_ms(const std::string& value) {
    if (!std::regex_match(value, std::regex("[0-9]{4}-[0-9]{2}-[0-9]{2}")))
        throw std::runtime_error("Date must use YYYY-MM-DD: " + value);
    std::tm date{};
    date.tm_year = std::stoi(value.substr(0, 4)) - 1900;
    date.tm_mon = std::stoi(value.substr(5, 2)) - 1;
    date.tm_mday = std::stoi(value.substr(8, 2));
    const auto expected = date;
    time_t seconds = timegm(&date);
    if (date.tm_year != expected.tm_year || date.tm_mon != expected.tm_mon || date.tm_mday != expected.tm_mday)
        throw std::runtime_error("Invalid calendar date: " + value);
    return static_cast<int64_t>(seconds) * 1000;
}

BacktestConfig BacktestConfig::from_yaml(const std::string& yaml_path) {
    const YAML::Node root = YAML::LoadFile(yaml_path);
    BacktestConfig cfg;
    cfg.config_dir = std::filesystem::absolute(yaml_path).parent_path().lexically_normal().string();
    require_known_keys(root, "", {"base", "afl", "universe", "backtest", "alphas_dir", "meta_dir", "alpha_pool", "alpha_pool_universe"});
    const auto base = root["base"];
    require_known_keys(base, "base.", {"cache", "groups"});
    cfg.base_cache = base["cache"].as<std::string>();
    cfg.base_groups = base["groups"] ? base["groups"].as<std::string>()
        : (std::filesystem::path(cfg.base_cache) / "groups").string();
    if (const auto afl = root["afl"]; afl && !afl.IsNull()) {
        require_known_keys(afl, "afl.", {"path", "catalog"});
        cfg.afl_path = afl["path"] ? afl["path"].as<std::string>()
            : (std::filesystem::path(cfg.base_cache) / "AflData").string();
        cfg.afl_catalog = afl["catalog"] ? afl["catalog"].as<std::string>()
            : (std::filesystem::path(cfg.base_cache) / "afl_catalog.json").string();
    }
    const auto universe = root["universe"];
    require_known_keys(universe, "universe.", {"path", "symbols", "name", "names"});
    cfg.universe.path = universe && universe["path"] ? universe["path"].as<std::string>()
        : (std::filesystem::path(cfg.base_cache) / "universe.parquet").string();
    if (universe && universe["symbols"])
        for (const auto& value : universe["symbols"]) cfg.universe.symbols.push_back(value.as<std::string>());
    if (universe && universe["name"]) cfg.universe.name = universe["name"].as<std::string>();
    if (universe && universe["names"]) {
        for (const auto& value : universe["names"]) cfg.universe.names.push_back(value.as<std::string>());
        if (cfg.universe.name.empty() && !cfg.universe.names.empty()) cfg.universe.name = cfg.universe.names.front();
    }
    const auto bt = root["backtest"];
    if (!bt || !bt.IsMap()) throw std::runtime_error("config.yaml: missing backtest mapping");
    require_known_keys(bt, "backtest.", {"start_date", "end_date", "initial_capital", "transaction_fee",
                                         "bars_per_year", "execution_price", "warmup_bars",
                                         "symbol_labels_path", "missing_close_policy"});
    cfg.start_date = bt["start_date"] ? bt["start_date"].as<std::string>() : "";
    cfg.end_date = bt["end_date"] && !bt["end_date"].IsNull()
        ? bt["end_date"].as<std::string>() : "latest";
    if (cfg.end_date.empty()) cfg.end_date = "latest";
    if (!cfg.start_date.empty()) config_date_ms(cfg.start_date);
    if (cfg.end_date != "latest") config_date_ms(cfg.end_date);
    if (!cfg.start_date.empty() && cfg.end_date != "latest" && cfg.start_date > cfg.end_date)
        throw std::runtime_error("backtest.start_date must not be after end_date");
    cfg.initial_capital = bt["initial_capital"] ? bt["initial_capital"].as<double>() : cfg.initial_capital;
    if (bt["transaction_fee"]) cfg.transaction_fee_rate = bt["transaction_fee"].as<double>() * 0.0001; // bp
    if (!std::isfinite(cfg.transaction_fee_rate) || cfg.transaction_fee_rate < 0 ||
        !std::isfinite(cfg.initial_capital) || cfg.initial_capital <= 0)
        throw std::runtime_error("Capital must be positive and fees finite/nonnegative");
    cfg.bars_per_year = unsigned_option(bt["bars_per_year"], cfg.bars_per_year, true);
    cfg.execution_price = bt["execution_price"] ? bt["execution_price"].as<std::string>() : cfg.execution_price;
    if (cfg.execution_price != "close" && cfg.execution_price != "ohlc4" && cfg.execution_price != "vwap")
        throw std::runtime_error("backtest.execution_price must be 'close', 'ohlc4' or 'vwap'");
    cfg.warmup_bars = unsigned_option(bt["warmup_bars"], 0, false);
    if (bt["symbol_labels_path"] && !bt["symbol_labels_path"].IsNull()) {
        std::filesystem::path labels = bt["symbol_labels_path"].as<std::string>();
        if (labels.is_relative()) labels = std::filesystem::path(cfg.config_dir) / labels;
        cfg.symbol_labels_path = labels.lexically_normal().string();
    }
    auto directory_option = [&](const char* key, const char* fallback) {
        std::filesystem::path dir = root[key] && !root[key].IsNull() ? root[key].as<std::string>() : fallback;
        if (dir.empty()) throw std::runtime_error(std::string(key) + " must not be empty");
        if (dir.is_relative()) dir = std::filesystem::path(cfg.config_dir) / dir;
        dir = dir.lexically_normal();
        if (!dir.has_filename() && dir.has_relative_path()) dir = dir.parent_path();
        return dir.string();
    };
    cfg.alphas_dir = directory_option("alphas_dir", "alphas");
    cfg.meta_dir = directory_option("meta_dir", "meta");
    if (root["alpha_pool"] && !root["alpha_pool"].IsNull()) cfg.alpha_pool = directory_option("alpha_pool", "");
    if (root["alpha_pool_universe"] && !root["alpha_pool_universe"].IsNull()) {
        cfg.alpha_pool_universe = root["alpha_pool_universe"].as<std::string>();
        if (cfg.alpha_pool_universe.empty()) throw std::runtime_error("alpha_pool_universe must not be empty");
    }
    cfg.missing_close_policy = bt["missing_close_policy"] && !bt["missing_close_policy"].IsNull()
        ? bt["missing_close_policy"].as<std::string>() : cfg.missing_close_policy;
    if (cfg.missing_close_policy != "carry" && cfg.missing_close_policy != "error")
        throw std::runtime_error("backtest.missing_close_policy must be 'carry' or 'error'");
    return cfg;
}


namespace loader {
namespace {
constexpr int64_t day_ms = 86400000;
constexpr size_t missing_row = std::numeric_limits<size_t>::max();
constexpr float missing_value = std::numeric_limits<float>::quiet_NaN();
void require_status(const arrow::Status& status) {
    if (!status.ok()) throw std::runtime_error(status.ToString());
}
std::string resolved(const BacktestConfig& config, const std::string& path) {
    return (std::filesystem::path(config.config_dir) / path).lexically_normal().string();
}
int64_t day_of(int64_t time) { return time / day_ms - (time % day_ms < 0 ? 1 : 0); }
std::string trimmed(std::string value) {
    const auto begin = value.find_first_not_of(" \t\r\n");
    if (begin == std::string::npos) return {};
    return value.substr(begin, value.find_last_not_of(" \t\r\n") - begin + 1);
}
struct Dates {
    std::vector<int64_t> values;
    std::vector<uint8_t> present;
};
Dates dates(const std::shared_ptr<arrow::ChunkedArray>& column) {
    Dates output;
    for (const auto& chunk : column->chunks()) {
        const auto array = std::static_pointer_cast<arrow::TimestampArray>(chunk);
        for (int64_t i = 0; i < array->length(); ++i) {
            output.values.push_back(array->IsNull(i) ? 0 : array->Value(i) / 1000000);
            output.present.push_back(!array->IsNull(i));
        }
    }
    return output;
}
std::vector<std::string> ids(const std::shared_ptr<arrow::ChunkedArray>& column) {
    std::vector<std::string> output;
    for (const auto& chunk : column->chunks()) {
        const auto array = std::static_pointer_cast<arrow::Int64Array>(chunk);
        for (int64_t i = 0; i < array->length(); ++i)
            output.push_back(array->IsNull(i) ? std::string{} : std::to_string(array->Value(i)));
    }
    return output;
}
std::vector<std::string> strings(const std::shared_ptr<arrow::ChunkedArray>& column) {
    std::vector<std::string> output;
    for (const auto& chunk : column->chunks()) {
        const auto array = std::static_pointer_cast<arrow::StringArray>(chunk);
        for (int64_t i = 0; i < array->length(); ++i)
            output.push_back(array->IsNull(i) ? std::string{} : trimmed(array->GetString(i)));
    }
    return output;
}
template<class Number, class Array>
void append_numbers(const std::shared_ptr<arrow::Array>& chunk, std::vector<Number>& output) {
    const auto array = std::static_pointer_cast<Array>(chunk);
    for (int64_t i = 0; i < array->length(); ++i) {
        const double value = array->IsNull(i) ? std::numeric_limits<double>::quiet_NaN() : array->Value(i);
        if constexpr (std::is_same_v<Number, float>)
            output.push_back(std::isinf(value) ? missing_value : static_cast<float>(value));
        else output.push_back(value);
    }
}
template<class Number>
std::vector<Number> numbers(const std::shared_ptr<arrow::ChunkedArray>& column) {
    std::vector<Number> output;
    output.reserve(column->length());
    for (const auto& chunk : column->chunks()) {
        if (chunk->type_id() == arrow::Type::DOUBLE) append_numbers<Number, arrow::DoubleArray>(chunk, output);
        else append_numbers<Number, arrow::FloatArray>(chunk, output);
    }
    return output;
}
std::vector<uint8_t> flags(const std::shared_ptr<arrow::ChunkedArray>& column) {
    std::vector<uint8_t> output;
    for (const auto& chunk : column->chunks()) {
        const auto array = std::static_pointer_cast<arrow::BooleanArray>(chunk);
        for (int64_t i = 0; i < array->length(); ++i)
            output.push_back(!array->IsNull(i) && array->Value(i));
    }
    return output;
}
struct RowSelection {
    std::vector<int> groups;
    std::map<int, size_t> offsets;
};
class ParquetFile {
public:
    std::unique_ptr<parquet::arrow::FileReader> reader;
    std::shared_ptr<arrow::Schema> schema;
    std::vector<int64_t> times, calendar;
    std::vector<size_t> order, group_offsets;
    std::map<std::string, int> symbols;
    std::vector<int> row_symbols;

    explicit ParquetFile(const std::string& path, bool wide = false) {
        auto source = arrow::io::ReadableFile::Open(path);
        if (!source.ok()) throw std::runtime_error(source.status().ToString());
        parquet::arrow::FileReaderBuilder builder;
        require_status(builder.Open(*source));
        auto built = builder.Build();
        if (!built.ok()) throw std::runtime_error(built.status().ToString());
        reader = std::move(*built);
        reader->set_use_threads(false);
        require_status(reader->GetSchema(&schema));
        const std::string time_column = wide ? "__index_level_0__" : "date";
        std::vector<int> projection{index(time_column)};
        if (!wide) projection.push_back(index("tradingItemId"));
        std::shared_ptr<arrow::Table> coordinates;
        require_status(reader->ReadTable(projection, &coordinates));
        times = dates(coordinates->GetColumnByName(time_column)).values;
        if (wide) {
            for (int i = 0; i < schema->num_fields(); ++i)
                if (schema->field(i)->name() != time_column) symbols.emplace(schema->field(i)->name(), i);
        } else {
            for (const auto& id : ids(coordinates->GetColumnByName("tradingItemId"))) {
                auto found = symbols.emplace(id, static_cast<int>(symbols.size()));
                row_symbols.push_back(found.first->second);
            }
        }
        order.resize(times.size());
        std::iota(order.begin(), order.end(), 0);
        std::sort(order.begin(), order.end(), [&](size_t a, size_t b) {
            return times[a] != times[b] ? times[a] < times[b] : (!wide && row_symbols[a] < row_symbols[b]);
        });
        for (size_t row : order)
            if (calendar.empty() || calendar.back() != times[row]) calendar.push_back(times[row]);
        group_offsets.push_back(0);
        const auto metadata = reader->parquet_reader()->metadata();
        for (int i = 0; i < metadata->num_row_groups(); ++i)
            group_offsets.push_back(group_offsets.back() + metadata->RowGroup(i)->num_rows());
    }
    int index(const std::string& name) const { return schema->GetFieldIndex(name); }
    std::vector<size_t> aligned_rows(const std::vector<int64_t>& target, bool asof) const {
        std::vector<size_t> rows;
        rows.reserve(target.size());
        for (int64_t time : target) {
            auto match = std::lower_bound(order.begin(), order.end(), time,
                [&](size_t row, int64_t value) { return times[row] < value; });
            if (match != order.end() && times[*match] == time) rows.push_back(*match);
            else if (asof && match != order.begin()) rows.push_back(*std::prev(match));
            else rows.push_back(missing_row);
        }
        return rows;
    }
    int group_of(size_t row) const {
        return static_cast<int>(std::upper_bound(group_offsets.begin(), group_offsets.end(), row) - group_offsets.begin()) - 1;
    }
    RowSelection select(const std::vector<size_t>& rows) const {
        std::set<int> groups;
        for (size_t row : rows) if (row != missing_row) groups.insert(group_of(row));
        RowSelection selection;
        size_t offset = 0;
        for (int group : groups) {
            selection.groups.push_back(group);
            selection.offsets[group] = offset;
            offset += group_offsets[group + 1] - group_offsets[group];
        }
        return selection;
    }
    size_t selected_index(size_t row, const RowSelection& selection) const {
        const int group = group_of(row);
        return selection.offsets.at(group) + row - group_offsets[group];
    }
    std::shared_ptr<arrow::Table> read(const std::vector<int>& columns, const RowSelection& selection) {
        std::shared_ptr<arrow::Table> result;
        require_status(reader->ReadRowGroups(selection.groups, columns, &result));
        return result;
    }
};
std::shared_ptr<arrow::Table> read_table(const std::string& path, const std::vector<std::string>& columns) {
    auto source = arrow::io::ReadableFile::Open(path);
    if (!source.ok()) throw std::runtime_error(source.status().ToString());
    parquet::arrow::FileReaderBuilder builder;
    require_status(builder.Open(*source));
    auto built = builder.Build();
    if (!built.ok()) throw std::runtime_error(built.status().ToString());
    auto reader = std::move(*built);
    reader->set_use_threads(false);
    std::shared_ptr<arrow::Schema> schema;
    require_status(reader->GetSchema(&schema));
    std::vector<int> projection;
    for (const auto& name : columns) projection.push_back(schema->GetFieldIndex(name));
    std::shared_ptr<arrow::Table> table;
    require_status(reader->ReadTable(projection, &table));
    return table;
}
struct Coordinates {
    std::vector<int64_t> timestamps;
    std::vector<std::string> symbols;
};
struct Source {
    std::string path, column;
    bool wide = false, asof = false, lazy = false;
};
template<class Number>
std::vector<Number> read_field(const Source& source, const Coordinates& coordinates) {
    static std::mutex cold_read_mutex;
    const std::lock_guard<std::mutex> lock(cold_read_mutex);
    const size_t T = coordinates.timestamps.size(), N = coordinates.symbols.size();
    std::vector<Number> output(T * N, std::numeric_limits<Number>::quiet_NaN());
    ParquetFile file(source.path, source.wide);
    if (source.wide) {
        const auto rows = file.aligned_rows(coordinates.timestamps, source.asof);
        const auto selection = file.select(rows);
        if (selection.groups.empty()) return output;
        std::vector<std::pair<size_t, int>> columns;
        for (size_t n = 0; n < N; ++n) {
            auto column = file.symbols.find(coordinates.symbols[n]);
            if (column != file.symbols.end()) columns.emplace_back(n, column->second);
        }
        for (size_t begin = 0; begin < columns.size(); begin += 64) {
            const size_t end = std::min(columns.size(), begin + 64);
            std::vector<int> indices;
            for (size_t i = begin; i < end; ++i) indices.push_back(columns[i].second);
            const auto table = file.read(indices, selection);
            for (size_t i = begin; i < end; ++i) {
                const auto values = numbers<Number>(table->GetColumnByName(coordinates.symbols[columns[i].first]));
                for (size_t t = 0; t < T; ++t)
                    if (rows[t] != missing_row) output[t * N + columns[i].first] = values[file.selected_index(rows[t], selection)];
            }
        }
    } else {
        std::map<int64_t, size_t> dates;
        for (size_t t = 0; t < T; ++t) dates.emplace(coordinates.timestamps[t], t);
        std::map<int, size_t> symbols;
        for (size_t n = 0; n < N; ++n) {
            auto symbol = file.symbols.find(coordinates.symbols[n]);
            if (symbol != file.symbols.end()) symbols.emplace(symbol->second, n);
        }
        std::vector<size_t> rows, destinations;
        for (size_t row : file.order) {
            const auto t = dates.find(file.times[row]);
            const auto n = symbols.find(file.row_symbols[row]);
            if (t != dates.end() && n != symbols.end()) {
                rows.push_back(row);
                destinations.push_back(t->second * N + n->second);
            }
        }
        const auto selection = file.select(rows);
        if (!selection.groups.empty()) {
            const auto values = numbers<Number>(file.read({file.index(source.column)}, selection)->column(0));
            for (size_t i = 0; i < rows.size(); ++i) output[destinations[i]] = values[file.selected_index(rows[i], selection)];
        }
    }
    return output;
}
using Membership = std::map<std::string, std::map<std::string, std::vector<uint8_t>>>;
Membership read_universe(const BacktestConfig& config, const std::vector<int64_t>& calendar) {
    const auto wanted = config.universe.names.empty()
        ? std::vector<std::string>{config.universe.name} : config.universe.names;
    const auto table = read_table(resolved(config, config.universe.path), {"universe", "tradingItemId", "fromDate", "toDate"});
    const auto names = strings(table->GetColumnByName("universe"));
    const auto members = ids(table->GetColumnByName("tradingItemId"));
    const auto from = dates(table->GetColumnByName("fromDate"));
    const auto to = dates(table->GetColumnByName("toDate"));
    const std::set<std::string> requested(config.universe.symbols.begin(), config.universe.symbols.end());
    Membership output;
    for (const auto& name : wanted) output[name];
    for (size_t row = 0; row < members.size(); ++row) {
        if (std::find(wanted.begin(), wanted.end(), names[row]) == wanted.end()) continue;
        if (!requested.empty() && !requested.count(members[row])) continue;
        const int64_t first = day_of(from.values[row]);
        const int64_t last = to.present[row] ? day_of(to.values[row]) : std::numeric_limits<int64_t>::max();
        for (size_t t = 0; t < calendar.size(); ++t) {
            const auto day = day_of(calendar[t]);
            if (day < first || day > last) continue;
            auto& mask = output[names[row]][members[row]];
            if (mask.empty()) mask.assign(calendar.size(), 0);
            mask[t] = 1;
        }
    }
    return output;
}
struct Events {
    std::vector<int64_t> dates;
    std::vector<std::string> sources, destinations;
    std::vector<double> ratios;
    std::vector<uint8_t> ends;
};
Events read_events(const std::string& path) {
    const auto table = read_table(path, {"date", "sourceId", "destinationId", "valueRatio", "sourceEnds"});
    return {dates(table->GetColumnByName("date")).values,
            ids(table->GetColumnByName("sourceId")), ids(table->GetColumnByName("destinationId")),
            numbers<double>(table->GetColumnByName("valueRatio")), flags(table->GetColumnByName("sourceEnds"))};
}
std::vector<float> read_factors(const std::string& path, const Coordinates& coordinates) {
    const auto table = read_table(path, {"tradingItemId", "validFrom", "validTo", "factor"});
    const auto members = ids(table->GetColumnByName("tradingItemId"));
    const auto from = dates(table->GetColumnByName("validFrom"));
    const auto to = dates(table->GetColumnByName("validTo"));
    const auto factors = numbers<double>(table->GetColumnByName("factor"));
    std::unordered_map<std::string, size_t> index;
    for (size_t n = 0; n < coordinates.symbols.size(); ++n) index.emplace(coordinates.symbols[n], n);
    std::vector<float> output(coordinates.timestamps.size() * coordinates.symbols.size(), missing_value);
    for (size_t row = 0; row < members.size(); ++row) {
        const auto n = index.find(members[row]);
        if (n == index.end()) continue;
        const int64_t begin = day_of(from.values[row]);
        const int64_t end = to.present[row] ? day_of(to.values[row]) : std::numeric_limits<int64_t>::max();
        for (size_t t = 0; t < coordinates.timestamps.size(); ++t) {
            const auto day = day_of(coordinates.timestamps[t]);
            if (day >= begin && day < end) output[t * coordinates.symbols.size() + n->second] = static_cast<float>(factors[row]);
        }
    }
    return output;
}
}

std::map<std::string, std::string> load_thin_symbol_labels(
    const BacktestConfig& config, const std::vector<std::string>& panel_symbols) {
    const auto table = read_table(resolved(config, (std::filesystem::path(config.base_cache) / "master.parquet").string()),
                                  {"tradingItemId", "symbol", "companyName"});
    const auto members = ids(table->GetColumnByName("tradingItemId"));
    const auto labels = strings(table->GetColumnByName("symbol"));
    const auto companies = strings(table->GetColumnByName("companyName"));
    const std::unordered_set<std::string> selected(panel_symbols.begin(), panel_symbols.end());
    std::map<std::string, std::string> output;
    for (size_t row = 0; row < members.size(); ++row) {
        if (!selected.count(members[row])) continue;
        const auto label = labels[row].empty() ? companies[row] : labels[row];
        if (!label.empty()) output.emplace(members[row], label);
    }
    return output;
}

DataPanel load_config(const BacktestConfig& config, const std::vector<std::string>& selected_fields) {
    const std::filesystem::path cache(resolved(config, config.base_cache));
    std::map<std::string, Source> sources;
    for (const char* field : {"open", "high", "low", "close", "vwap", "volume", "cap"})
        sources[field] = {(cache / "base_data.parquet").string(), std::string(field) == "cap" ? "marketCap_USD" : field};
    sources["returns"] = {(cache / "returns.parquet").string(), "returns"};
    sources["adj_returns"] = {(cache / "adj_returns.parquet").string(), "adj_returns"};
    for (const char* field : {"sector", "industry"})
        sources[field] = {resolved(config, (std::filesystem::path(config.base_groups) / (std::string(field) + ".parquet")).string()), "", true, true};
    if (!config.afl_catalog.empty()) {
        const auto catalog = YAML::LoadFile(resolved(config, config.afl_catalog));
        for (const auto& field : catalog["fields"])
            sources["AFL." + field.first.as<std::string>()] = {
                resolved(config, (std::filesystem::path(config.afl_path) / field.second.as<std::string>()).string()), "", true, true, true};
    }
    auto master = std::make_unique<ParquetFile>(sources.at("close").path);
    const int64_t start = config.start_date.empty() ? std::numeric_limits<int64_t>::min() : config_date_ms(config.start_date);
    const bool latest_end = config.end_date.empty() || config.end_date == "latest";
    const int64_t end = latest_end ? std::numeric_limits<int64_t>::max() : config_date_ms(config.end_date) + day_ms - 1;
    const auto first = std::lower_bound(master->calendar.begin(), master->calendar.end(), start);
    const auto last = std::upper_bound(master->calendar.begin(), master->calendar.end(), end);
    if (first == master->calendar.end() || first >= last) throw std::runtime_error("No close observations in the configured period");
    const size_t begin = first - master->calendar.begin();
    const size_t warmup_begin = begin - std::min<size_t>(begin, config.warmup_bars);
    DataPanel panel;
    panel.timestamps.assign(master->calendar.begin() + warmup_begin, last);
    panel.trade_start_index = static_cast<unsigned>(begin - warmup_begin);
    panel.T = static_cast<unsigned>(panel.timestamps.size());
    auto membership = config.universe.name.empty() && config.universe.names.empty()
        ? Membership{} : read_universe(config, panel.timestamps);
    const auto events = read_events((cache / "events.parquet").string());
    std::set<std::string> loaded;
    if (!membership.empty()) {
        for (const auto& set : membership)
            for (const auto& member : set.second) loaded.insert(member.first);
    } else if (!config.universe.symbols.empty()) loaded.insert(config.universe.symbols.begin(), config.universe.symbols.end());
    else for (const auto& member : master->symbols) loaded.insert(member.first);
    for (bool added = true; added;) {
        added = false;
        for (size_t row = 0; row < events.dates.size(); ++row)
            if (events.dates[row] >= panel.timestamps.front() && events.dates[row] <= panel.timestamps.back() &&
                loaded.count(events.sources[row]) && !events.destinations[row].empty() && loaded.insert(events.destinations[row]).second) added = true;
    }
    panel.symbols.assign(loaded.begin(), loaded.end());
    panel.N = static_cast<unsigned>(panel.symbols.size());
    auto mask = [&](const std::map<std::string, std::vector<uint8_t>>& members) {
        std::vector<uint8_t> output(static_cast<size_t>(panel.T) * panel.N);
        for (size_t n = 0; n < panel.N; ++n) {
            const auto member = members.find(panel.symbols[n]);
            if (member != members.end())
                for (size_t t = 1; t < panel.T; ++t) output[t * panel.N + n] = member->second[t - 1];
        }
        return output;
    };
    if (!membership.empty()) panel.universe_mask = mask(membership.at(config.universe.name));
    panel.universe_names = config.universe.names;
    panel.default_universe = config.universe.name;
    for (const auto& set : membership)
        if (set.first != config.universe.name) panel.universe_masks.emplace(set.first, mask(set.second));
    membership.clear();
    master.reset();
    auto close_values = read_field<float>(sources.at("close"), {panel.timestamps, panel.symbols});
    if (latest_end) {
        size_t rows = panel.T;
        while (rows > panel.trade_start_index && std::all_of(close_values.begin() + (rows - 1) * panel.N,
               close_values.begin() + rows * panel.N, [](float value) { return std::isnan(value); })) --rows;
        if (rows <= panel.trade_start_index) throw std::runtime_error("No close prices in the configured trading period");
        panel.T = static_cast<unsigned>(rows);
        panel.timestamps.resize(rows);
        close_values.resize(rows * panel.N);
        if (!panel.universe_mask.empty()) panel.universe_mask.resize(rows * panel.N);
        for (auto& named : panel.universe_masks) named.second.resize(rows * panel.N);
    }
    panel.arrays.emplace("close", std::move(close_values));
    const auto coordinates = std::make_shared<const Coordinates>(Coordinates{panel.timestamps, panel.symbols});
    const std::set<std::string> selected(selected_fields.begin(), selected_fields.end());
    const std::set<std::string> execution = config.execution_price == "vwap"
        ? std::set<std::string>{"vwap", "high", "low"} : config.execution_price == "ohlc4"
        ? std::set<std::string>{"open", "high", "low"} : std::set<std::string>{};
    std::map<std::string, std::shared_ptr<LazyColumn>> lazy;
    for (const auto& [name, source] : sources) {
        if (name == "close" || (!selected.empty() && !selected.count(name) && !execution.count(name))) continue;
        if (source.lazy && !execution.count(name)) {
            auto& column = lazy[source.path];
            if (!column) column = std::make_shared<LazyColumn>([source, coordinates] { return read_field<float>(source, *coordinates); });
            panel.lazy_columns.emplace(name, column);
        } else panel.arrays.emplace(name, read_field<float>(source, *coordinates));
    }
    for (const char* name : {"sector", "industry"}) {
        auto found = panel.arrays.find(name);
        if (found == panel.arrays.end()) continue;
        auto& values = found->second;
        for (size_t t = panel.T; t-- > 1;)
            std::copy_n(values.begin() + static_cast<std::ptrdiff_t>((t - 1) * panel.N), panel.N,
                        values.begin() + static_cast<std::ptrdiff_t>(t * panel.N));
        if (panel.T) std::fill_n(values.begin(), panel.N, missing_value);
    }
    panel.thin_layout = true;
    if (selected.empty() || selected.count("adj_factor")) panel.arrays.emplace("adj_factor", read_factors((cache / "adj_factor.parquet").string(), *coordinates));
    panel.returns_total = read_field<double>(sources.at("returns"), *coordinates);
    std::unordered_map<std::string, unsigned> index;
    for (unsigned n = 0; n < panel.N; ++n) index.emplace(panel.symbols[n], n);
    for (size_t row = 0; row < events.dates.size(); ++row) {
        const auto source = index.find(events.sources[row]);
        if (source == index.end()) continue;
        DataPanel::MoveEvent event;
        event.source = source->second;
        event.source_ends = events.ends[row];
        if (events.dates[row] < panel.timestamps.front()) {
            if (event.source_ends) panel.move_events.push_back(event);
            continue;
        }
        const auto bar = std::lower_bound(panel.timestamps.begin(), panel.timestamps.end(), events.dates[row]);
        if (bar == panel.timestamps.end() || *bar != events.dates[row]) continue;
        event.bar = static_cast<unsigned>(bar - panel.timestamps.begin());
        const auto destination = index.find(events.destinations[row]);
        if (destination != index.end()) event.destination = static_cast<int32_t>(destination->second);
        event.value_ratio = events.ratios[row];
        panel.move_events.push_back(event);
    }
    std::stable_sort(panel.move_events.begin(), panel.move_events.end(), [](const auto& a, const auto& b) {
        return a.bar != b.bar ? a.bar < b.bar : a.source < b.source;
    });
    return panel;
}
}


const std::vector<float>& LazyColumn::get() const {
    std::call_once(once, [this] {
        auto loaded_values = loader(); // exceptions leave once_flag retryable
        values = std::move(loaded_values);
        loader = {};
        ready.store(true, std::memory_order_release);
    });
    return values;
}
size_t LazyColumn::loaded_bytes() const { return loaded() ? values.size() * sizeof(float) : 0; }

const float& DataPanel::at(const std::string& col, unsigned t, unsigned n) const {
    if (t >= T || n >= N) throw std::out_of_range("DataPanel coordinate out of range");
    return row(col, t)[n];
}
float& DataPanel::at(const std::string& col, unsigned t, unsigned n) {
    return const_cast<float&>(static_cast<const DataPanel&>(*this).at(col, t, n));
}
const float* DataPanel::row(const std::string& col, unsigned t) const {
    if (t >= T) throw std::out_of_range("DataPanel row out of range");
    auto eager = arrays.find(col);
    const auto& values = eager != arrays.end() ? eager->second : lazy_columns.at(col)->get();
    return values.data() + static_cast<size_t>(t) * N;
}
bool DataPanel::has_column(const std::string& col) const {
    return arrays.count(col) || lazy_columns.count(col);
}
std::vector<std::string> DataPanel::column_names() const {
    std::vector<std::string> names;
    for (const auto& field : arrays) names.push_back(field.first);
    for (const auto& field : lazy_columns) names.push_back(field.first);
    std::sort(names.begin(), names.end());
    names.erase(std::unique(names.begin(), names.end()), names.end());
    return names;
}
size_t DataPanel::loaded_field_count() const {
    size_t count = arrays.size();
    std::unordered_set<const LazyColumn*> seen;
    for (const auto& field : lazy_columns)
        if (seen.insert(field.second.get()).second && field.second->loaded()) ++count;
    return count;
}
size_t DataPanel::loaded_bytes() const {
    size_t bytes = 0;
    for (const auto& field : arrays) bytes += field.second.size() * sizeof(float);
    std::unordered_set<const LazyColumn*> seen;
    for (const auto& field : lazy_columns)
        if (seen.insert(field.second.get()).second) bytes += field.second->loaded_bytes();
    return bytes;
}
DataPanel DataPanel::load(const BacktestConfig& config, const std::vector<std::string>& selected_fields) {
    return loader::load_config(config, selected_fields);
}

const std::vector<uint8_t>& DataPanel::mask_for(const std::string& name) const {
    if (name.empty() || name == default_universe) return universe_mask;
    auto it = universe_masks.find(name);
    if (it == universe_masks.end()) throw std::invalid_argument("Unknown universe: " + name);
    return it->second;
}
