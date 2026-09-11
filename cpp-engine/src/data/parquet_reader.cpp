#include "parquet_reader.h"
#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/reader.h>
#include <parquet/file_reader.h>
#include <algorithm>
#include <cmath>
#include <filesystem>
#include <limits>
#include <map>
#include <mutex>
#include <numeric>
#include <set>
#include <stdexcept>
#include <unordered_set>

namespace parquet_reader {
namespace {
constexpr size_t column_batch_size = 64;
constexpr size_t missing_row = std::numeric_limits<size_t>::max();
const float missing_value = std::numeric_limits<float>::quiet_NaN();
void require_status(const arrow::Status& status, const std::string& context) {
    if (!status.ok()) throw std::runtime_error(context + ": " + status.ToString());
}
std::string resolved(const BacktestConfig& config, const std::string& path) {
    return (std::filesystem::path(config.config_dir) / path).lexically_normal().string();
}
size_t cell_count(size_t rows, size_t columns) {
    if (columns && rows > std::numeric_limits<size_t>::max() / columns / sizeof(float))
        throw std::runtime_error("Requested panel dimensions overflow addressable memory");
    return rows * columns;
}
int64_t time_value(int64_t value, int64_t multiply, int64_t divide) {
    if (value % divide != 0) throw std::runtime_error("Timestamp has sub-millisecond precision; cannot represent exactly");
    value /= divide;
    if (value > std::numeric_limits<int64_t>::max() / multiply || value < std::numeric_limits<int64_t>::min() / multiply)
        throw std::runtime_error("Timestamp overflows epoch milliseconds");
    return value * multiply;
}
std::vector<int64_t> read_times(const std::shared_ptr<arrow::ChunkedArray>& column) {
    std::vector<int64_t> output;
    output.reserve(column->length());
    for (const auto& chunk : column->chunks()) {
        if (chunk->null_count()) throw std::runtime_error("Timestamp column contains nulls");
        int64_t multiply = 1, divide = 1;
        if (chunk->type_id() == arrow::Type::TIMESTAMP) {
            const auto unit = std::static_pointer_cast<arrow::TimestampType>(chunk->type())->unit();
            if (unit == arrow::TimeUnit::SECOND) multiply = 1000;
            else if (unit == arrow::TimeUnit::MICRO) divide = 1000;
            else if (unit == arrow::TimeUnit::NANO) divide = 1000000;
            auto array = std::static_pointer_cast<arrow::TimestampArray>(chunk);
            for (int64_t i = 0; i < array->length(); ++i) output.push_back(time_value(array->Value(i), multiply, divide));
        } else if (chunk->type_id() == arrow::Type::INT64) {
            auto array = std::static_pointer_cast<arrow::Int64Array>(chunk);
            for (int64_t i = 0; i < array->length(); ++i) output.push_back(array->Value(i));
        } else if (chunk->type_id() == arrow::Type::DATE32) {
            auto array = std::static_pointer_cast<arrow::Date32Array>(chunk);
            for (int64_t i = 0; i < array->length(); ++i) output.push_back(time_value(array->Value(i), 86400000, 1));
        } else if (chunk->type_id() == arrow::Type::DATE64) {
            auto array = std::static_pointer_cast<arrow::Date64Array>(chunk);
            for (int64_t i = 0; i < array->length(); ++i) output.push_back(array->Value(i));
        } else throw std::runtime_error("Unsupported timestamp type: " + chunk->type()->ToString());
    }
    return output;
}
template<class Array> void append_numbers(const std::shared_ptr<arrow::Array>& chunk, std::vector<float>& output) {
    auto array = std::static_pointer_cast<Array>(chunk);
    for (int64_t i = 0; i < array->length(); ++i) {
        if (array->IsNull(i)) { output.push_back(missing_value); continue; }
        const double value = static_cast<double>(array->Value(i));
        if (std::isfinite(value) && std::fabs(value) > std::numeric_limits<float>::max())
            throw std::runtime_error("Numeric value cannot be represented as float32");
        output.push_back(static_cast<float>(value));
    }
}
std::vector<float> numbers(const std::shared_ptr<arrow::ChunkedArray>& column) {
    std::vector<float> output;
    output.reserve(column->length());
    for (const auto& chunk : column->chunks()) {
        switch (chunk->type_id()) {
#define NUMERIC_CASE(ID, TYPE) case arrow::Type::ID: append_numbers<arrow::TYPE>(chunk, output); break
            NUMERIC_CASE(BOOL, BooleanArray);
            NUMERIC_CASE(INT8, Int8Array); NUMERIC_CASE(INT16, Int16Array);
            NUMERIC_CASE(INT32, Int32Array); NUMERIC_CASE(INT64, Int64Array);
            NUMERIC_CASE(UINT8, UInt8Array); NUMERIC_CASE(UINT16, UInt16Array);
            NUMERIC_CASE(UINT32, UInt32Array); NUMERIC_CASE(UINT64, UInt64Array);
            NUMERIC_CASE(FLOAT, FloatArray); NUMERIC_CASE(DOUBLE, DoubleArray);
#undef NUMERIC_CASE
            case arrow::Type::NA: output.insert(output.end(), chunk->length(), missing_value); break;
            default: throw std::runtime_error("Unsupported numeric type: " + chunk->type()->ToString());
        }
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
    std::vector<int64_t> times; // original row order
    std::vector<size_t> order;
    std::vector<int64_t> calendar;
    std::map<std::string, int> symbols; // wide schema index, or long stable dictionary ID
    std::vector<int> row_symbols;
    std::vector<size_t> group_offsets;

    ParquetFile(const std::string& path, const std::string& time_col, const std::string& symbol_col, bool wide) {
        auto file = arrow::io::ReadableFile::Open(path);
        if (!file.ok()) throw std::runtime_error("Cannot open parquet " + path + ": " + file.status().ToString());
        parquet::arrow::FileReaderBuilder builder;
        require_status(builder.Open(*file), "Cannot create parquet reader for " + path);
        builder.memory_pool(arrow::default_memory_pool());
        auto built = builder.Build();
        if (!built.ok()) throw std::runtime_error("Cannot build parquet reader: " + built.status().ToString());
        reader = std::move(*built);
        reader->set_use_threads(false); // bound temporary decoded-column memory
        require_status(reader->GetSchema(&schema), "Cannot read parquet schema");
        std::set<std::string> names;
        for (const auto& field : schema->fields()) {
            if (field->name().empty() || !names.insert(field->name()).second)
                throw std::runtime_error("Empty/duplicate parquet column name in " + path);
            if (field->type()->num_fields() != 0)
                throw std::runtime_error("Nested parquet columns are unsupported: " + field->name());
        }
        const int ti = index(time_col);
        std::vector<int> coordinate_columns{ti};
        if (!wide) coordinate_columns.push_back(index(symbol_col));
        std::shared_ptr<arrow::Table> coordinates;
        require_status(reader->ReadTable(coordinate_columns, &coordinates), "Cannot read parquet coordinates");
        times = read_times(coordinates->GetColumnByName(time_col));
        if (wide) {
            for (int i = 0; i < schema->num_fields(); ++i)
                if (i != ti) symbols.emplace(schema->field(i)->name(), i);
        } else {
            auto ids = coordinates->GetColumnByName(symbol_col);
            row_symbols.reserve(ids->length());
            for (const auto& chunk : ids->chunks()) {
                if (chunk->type_id() != arrow::Type::STRING && chunk->type_id() != arrow::Type::LARGE_STRING)
                    throw std::runtime_error("Long symbol column must be string/large_string");
                for (int64_t i = 0; i < chunk->length(); ++i) {
                    if (chunk->IsNull(i)) throw std::runtime_error("Long symbol column contains null");
                    std::string symbol = chunk->type_id() == arrow::Type::STRING ?
                        std::static_pointer_cast<arrow::StringArray>(chunk)->GetString(i) :
                        std::static_pointer_cast<arrow::LargeStringArray>(chunk)->GetString(i);
                    if (symbol.empty()) throw std::runtime_error("Long symbol column contains empty ID");
                    auto inserted = symbols.emplace(symbol, static_cast<int>(symbols.size()));
                    row_symbols.push_back(inserted.first->second);
                }
            }
        }
        order.resize(times.size());
        std::iota(order.begin(), order.end(), 0);
        std::sort(order.begin(), order.end(), [&](size_t a, size_t b) {
            return times[a] != times[b] ? times[a] < times[b] : (!wide && row_symbols[a] < row_symbols[b]);
        });
        for (size_t i = 0; i < order.size(); ++i) {
            const size_t row = order[i];
            if (i && times[row] == times[order[i - 1]] && (wide || row_symbols[row] == row_symbols[order[i - 1]]))
                throw std::runtime_error("Duplicate timestamp/symbol coordinate in " + path);
            if (calendar.empty() || calendar.back() != times[row]) calendar.push_back(times[row]);
        }
        group_offsets.push_back(0);
        auto metadata = reader->parquet_reader()->metadata();
        for (int group = 0; group < metadata->num_row_groups(); ++group)
            group_offsets.push_back(group_offsets.back() + metadata->RowGroup(group)->num_rows());
    }
    int index(const std::string& name) const {
        const int result = schema->GetFieldIndex(name);
        if (result < 0) throw std::runtime_error("Parquet column not found: " + name);
        return result;
    }
    std::vector<size_t> aligned_rows(const std::vector<int64_t>& target, FieldAlignment alignment) const {
        std::vector<size_t> rows;
        rows.reserve(target.size());
        for (int64_t timestamp : target) {
            auto match = std::lower_bound(order.begin(), order.end(), timestamp,
                [&](size_t row, int64_t value) { return times[row] < value; });
            if (match != order.end() && times[*match] == timestamp) rows.push_back(*match);
            else if (alignment == FieldAlignment::AsOf && match != order.begin()) rows.push_back(*std::prev(match));
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
        int group = group_of(row);
        return selection.offsets.at(group) + row - group_offsets[group];
    }
    std::shared_ptr<arrow::Table> read(const std::vector<int>& columns, const RowSelection& selection) {
        std::shared_ptr<arrow::Table> result;
        require_status(reader->ReadRowGroups(selection.groups, columns, &result), "Cannot read projected parquet row groups");
        return result;
    }
};

struct Coordinates { std::vector<int64_t> timestamps; std::vector<std::string> symbols; };
struct Source {
    DatasetFormat format;
    std::string path, time_col, symbol_col, column;
    FieldAlignment alignment;
};
std::vector<float> read_field(const Source& source, const Coordinates& coordinates) {
    // Serialize file reads so decoding wide metadata does not overlap across fields.
    // Cached field access and signal computation do not acquire this mutex.
    static std::mutex cold_read_mutex;
    const std::lock_guard<std::mutex> cold_read_lock(cold_read_mutex);
    const size_t T = coordinates.timestamps.size(), N = coordinates.symbols.size();
    std::vector<float> output(cell_count(T, N), missing_value);
    ParquetFile file(source.path, source.time_col, source.symbol_col, source.format == DatasetFormat::Wide);
    if (source.format == DatasetFormat::Wide) {
        auto rows = file.aligned_rows(coordinates.timestamps, source.alignment);
        auto selection = file.select(rows);
        if (selection.groups.empty()) return output;
        std::vector<std::pair<size_t, int>> columns;
        for (size_t n = 0; n < N; ++n) {
            auto column = file.symbols.find(coordinates.symbols[n]);
            if (column != file.symbols.end()) columns.emplace_back(n, column->second);
        }
        for (size_t begin = 0; begin < columns.size(); begin += column_batch_size) {
            size_t end = std::min(columns.size(), begin + column_batch_size);
            std::vector<int> indices;
            for (size_t i = begin; i < end; ++i) indices.push_back(columns[i].second);
            auto table = file.read(indices, selection);
            for (size_t i = begin; i < end; ++i) {
                auto values = numbers(table->GetColumnByName(coordinates.symbols[columns[i].first]));
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
            auto t = dates.find(file.times[row]);
            auto n = symbols.find(file.row_symbols[row]);
            if (t != dates.end() && n != symbols.end()) {
                rows.push_back(row);
                destinations.push_back(t->second * N + n->second);
            }
        }
        auto selection = file.select(rows);
        if (!selection.groups.empty()) {
            auto values = numbers(file.read({file.index(source.column)}, selection)->column(0));
            for (size_t i = 0; i < rows.size(); ++i) output[destinations[i]] = values[file.selected_index(rows[i], selection)];
        }
    }
    return output;
}
std::map<std::string, std::vector<uint8_t>> read_universe(const BacktestConfig& config,
    const std::vector<int64_t>& calendar, const std::map<std::string, int>& price_symbols) {
    ParquetFile file(resolved(config, config.universe.path), config.universe.time_col, "", true);
    auto rows = file.aligned_rows(calendar, config.universe.alignment);
    auto selection = file.select(rows);
    if (selection.groups.empty()) throw std::runtime_error("Universe has no observations in the selected period");
    std::set<std::string> requested(config.universe.symbols.begin(), config.universe.symbols.end());
    std::vector<std::pair<std::string, int>> columns;
    for (const auto& column : file.symbols)
        if (requested.empty() || requested.count(column.first)) columns.push_back(column);
    for (const auto& symbol : requested)
        if (!file.symbols.count(symbol)) throw std::runtime_error("Selected symbol is absent from universe: " + symbol);
    std::map<std::string, std::vector<uint8_t>> result;
    for (size_t begin = 0; begin < columns.size(); begin += column_batch_size) {
        size_t end = std::min(columns.size(), begin + column_batch_size);
        std::vector<int> indices;
        for (size_t i = begin; i < end; ++i) indices.push_back(columns[i].second);
        auto table = file.read(indices, selection);
        for (size_t i = begin; i < end; ++i) {
            auto values = numbers(table->GetColumnByName(columns[i].first));
            std::vector<uint8_t> mask(calendar.size(), 0);
            bool any = false;
            for (size_t t = 0; t < calendar.size(); ++t) if (rows[t] != missing_row) {
                float value = values[file.selected_index(rows[t], selection)];
                if (!std::isnan(value) && value != 0 && value != 1)
                    throw std::runtime_error("Universe values must be boolean/0/1/null: " + columns[i].first);
                if (value == 1) { mask[t] = 1; any = true; }
            }
            if (any) {
                if (!price_symbols.count(columns[i].first))
                    throw std::runtime_error("Universe contains active symbol absent from close: " + columns[i].first);
                result.emplace(columns[i].first, std::move(mask));
            }
        }
    }
    return result;
}
}

DataPanel load_config(const BacktestConfig& config, const std::vector<std::string>& selected_fields) {
    std::map<std::string, std::pair<Source, bool>> sources;
    for (const auto& dataset : config.datasets) if (dataset.enabled) {
        for (const auto& field : dataset.fields) {
            if (field.first == "returns")
                throw std::runtime_error("Field 'returns' is reserved for close-derived returns; use 'holding_return' or another name");
            Source source{dataset.format, resolved(config, dataset.path), dataset.time_col, dataset.symbol_col, field.second, dataset.alignment};
            if (dataset.format == DatasetFormat::Wide)
                source.path = (std::filesystem::path(source.path) / field.second).lexically_normal().string();
            if (!sources.emplace(field.first, std::make_pair(source, dataset.lazy)).second)
                throw std::runtime_error("Duplicate public data field: " + field.first);
        }
    }
    auto close = sources.find("close");
    if (close == sources.end()) throw std::runtime_error("Data configuration must provide close");
    if (close->second.first.alignment != FieldAlignment::Exact)
        throw std::runtime_error("close must use exact alignment");
    std::set<std::string> selected(selected_fields.begin(), selected_fields.end());
    for (const auto& field : selected)
        if (!sources.count(field)) throw std::runtime_error("Requested data field is not registered: " + field);
    selected.insert("close");
    const auto& close_source = close->second.first;
    auto master = std::make_unique<ParquetFile>(close_source.path, close_source.time_col, close_source.symbol_col,
                                                close_source.format == DatasetFormat::Wide);
    const auto start = config.start_date.empty() ? std::numeric_limits<int64_t>::min() : config_date_ms(config.start_date);
    const auto end = config.end_date.empty() ? std::numeric_limits<int64_t>::max() : config_date_ms(config.end_date) + 86399999;
    auto first = std::lower_bound(master->calendar.begin(), master->calendar.end(), start);
    auto last = std::upper_bound(master->calendar.begin(), master->calendar.end(), end);
    if (first == master->calendar.end() || first >= last) throw std::runtime_error("No close observations in the configured period");
    const size_t begin = first - master->calendar.begin();
    const size_t warmup_begin = begin - std::min<size_t>(begin, config.warmup_bars);
    DataPanel panel;
    panel.timestamps.assign(master->calendar.begin() + warmup_begin, last);
    panel.trade_start_index = static_cast<unsigned>(begin - warmup_begin);
    if (panel.timestamps.size() > std::numeric_limits<unsigned>::max()) throw std::runtime_error("Too many timestamps");
    panel.T = static_cast<unsigned>(panel.timestamps.size());
    std::map<std::string, std::vector<uint8_t>> membership;
    if (!config.universe.path.empty()) {
        membership = read_universe(config, panel.timestamps, master->symbols);
        for (const auto& member : membership) panel.symbols.push_back(member.first);
    } else if (!config.universe.symbols.empty()) {
        panel.symbols = config.universe.symbols;
        std::sort(panel.symbols.begin(), panel.symbols.end());
    } else for (const auto& symbol : master->symbols) panel.symbols.push_back(symbol.first);
    if (panel.symbols.empty()) throw std::runtime_error("Selected universe is empty in the configured period");
    if (std::adjacent_find(panel.symbols.begin(), panel.symbols.end()) != panel.symbols.end()) throw std::runtime_error("Duplicate selected symbols");
    for (const auto& symbol : panel.symbols)
        if (!master->symbols.count(symbol)) throw std::runtime_error("Selected symbol is absent from close: " + symbol);
    if (panel.symbols.size() > std::numeric_limits<unsigned>::max()) throw std::runtime_error("Too many symbols");
    panel.N = static_cast<unsigned>(panel.symbols.size());
    if (!membership.empty()) {
        panel.universe_mask.resize(cell_count(panel.T, panel.N));
        for (size_t n = 0; n < panel.N; ++n)
            for (size_t t = 0; t < panel.T; ++t) panel.universe_mask[t * panel.N + n] = membership.at(panel.symbols[n])[t];
    }
    // Only coordinates survive into value loaders; release the wide master footer/schema first.
    master.reset();
    const auto coordinates = std::make_shared<const Coordinates>(Coordinates{panel.timestamps, panel.symbols});
    std::map<std::string, std::shared_ptr<LazyColumn>> shared_sources;
    for (const auto& field : sources) {
        const bool requested = selected_fields.empty() || selected.count(field.first);
        if (!requested) continue;
        const Source source = field.second.first;
        const std::string key = source.path + "\n" + source.time_col + "\n" + source.symbol_col + "\n" +
            (source.format == DatasetFormat::Long ? source.column : "") + "\n" + std::to_string(static_cast<int>(source.alignment));
        if (field.first == "close" || !field.second.second) {
            panel.arrays.emplace(field.first, read_field(source, *coordinates));
        } else {
            auto& column = shared_sources[key];
            if (!column) column = std::make_shared<LazyColumn>([source, coordinates] { return read_field(source, *coordinates); });
            panel.lazy_columns.emplace(field.first, column);
        }
    }
    return panel;
}
DataPanel load_parquet(const std::string& path, const std::vector<std::string>& columns,
                       const std::string& time_col, const std::string& symbol_col) {
    BacktestConfig config;
    DatasetConfig dataset;
    dataset.path = path;
    dataset.time_col = time_col;
    dataset.symbol_col = symbol_col;
    for (const auto& column : columns) dataset.fields.emplace(column, column);
    config.datasets.push_back(std::move(dataset));
    return load_config(config);
}
} // namespace parquet_reader
