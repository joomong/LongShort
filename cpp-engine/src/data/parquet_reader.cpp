#include "parquet_reader.h"
#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/reader.h>
#include <parquet/file_reader.h>
#include <algorithm>
#include <cmath>
#include <ctime>
#include <filesystem>
#include <limits>
#include <map>
#include <mutex>
#include <numeric>
#include <set>
#include <stdexcept>
#include <iostream>
#include <unordered_map>
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
                // int64 ids (thin-v1 tradingItemId) become their decimal string.
                if (chunk->type_id() != arrow::Type::STRING && chunk->type_id() != arrow::Type::LARGE_STRING &&
                    chunk->type_id() != arrow::Type::INT64)
                    throw std::runtime_error("Long symbol column must be string/large_string/int64");
                for (int64_t i = 0; i < chunk->length(); ++i) {
                    if (chunk->IsNull(i)) throw std::runtime_error("Long symbol column contains null");
                    std::string symbol = chunk->type_id() == arrow::Type::INT64 ?
                        std::to_string(std::static_pointer_cast<arrow::Int64Array>(chunk)->Value(i)) :
                        chunk->type_id() == arrow::Type::STRING ?
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
        // Validate the declared source even when no source rows align to this panel.
        const int value_column = file.index(source.column);
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
            auto values = numbers(file.read({value_column}, selection)->column(0));
            for (size_t i = 0; i < rows.size(); ++i) output[destinations[i]] = values[file.selected_index(rows[i], selection)];
        }
    }
    return output;
}
// ---------------------------------------------------------------------------
// Shared ID and value readers for thin events and universe membership.
// ---------------------------------------------------------------------------
constexpr int64_t day_ms = 86400000;
std::string iso_date(int64_t epoch_ms) {
    const std::time_t seconds = static_cast<std::time_t>(epoch_ms / 1000);
    std::tm parts{};
    gmtime_r(&seconds, &parts);
    char text[11];
    std::strftime(text, sizeof(text), "%Y-%m-%d", &parts);
    return text;
}
std::string trimmed(const std::string& value) {
    const auto first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    return value.substr(first, value.find_last_not_of(" \t\r\n") - first + 1);
}
// These readers also serve thin events and universe membership.
std::vector<std::string> read_event_ids(const std::shared_ptr<arrow::ChunkedArray>& column,
                                        const std::string& what = "ID") {
    std::vector<std::string> ids;
    ids.reserve(column->length());
    for (const auto& chunk : column->chunks()) {
        for (int64_t i = 0; i < chunk->length(); ++i) {
            if (chunk->IsNull(i)) throw std::runtime_error(what + " id column contains null");
            std::string id;
            switch (chunk->type_id()) {
                case arrow::Type::INT64:
                    id = std::to_string(std::static_pointer_cast<arrow::Int64Array>(chunk)->Value(i)); break;
                case arrow::Type::STRING:
                    id = trimmed(std::static_pointer_cast<arrow::StringArray>(chunk)->GetString(i)); break;
                case arrow::Type::LARGE_STRING:
                    id = trimmed(std::static_pointer_cast<arrow::LargeStringArray>(chunk)->GetString(i)); break;
                default:
                    throw std::runtime_error(what + " id column must be int64 or string, not " + chunk->type()->ToString());
            }
            if (id.empty()) throw std::runtime_error(what + " id column contains an empty ID");
            ids.push_back(std::move(id));
        }
    }
    return ids;
}
// Destination ids are optional per row, so nulls become empty.
std::vector<std::string> read_optional_ids(const std::shared_ptr<arrow::ChunkedArray>& column,
                                           const std::string& what = "Optional ID") {
    std::vector<std::string> ids;
    ids.reserve(column->length());
    for (const auto& chunk : column->chunks()) {
        for (int64_t i = 0; i < chunk->length(); ++i) {
            if (chunk->IsNull(i)) { ids.emplace_back(); continue; }
            switch (chunk->type_id()) {
                case arrow::Type::INT64:
                    ids.push_back(std::to_string(std::static_pointer_cast<arrow::Int64Array>(chunk)->Value(i))); break;
                case arrow::Type::INT32:
                    ids.push_back(std::to_string(std::static_pointer_cast<arrow::Int32Array>(chunk)->Value(i))); break;
                case arrow::Type::STRING:
                    ids.push_back(trimmed(std::static_pointer_cast<arrow::StringArray>(chunk)->GetString(i))); break;
                case arrow::Type::LARGE_STRING:
                    ids.push_back(trimmed(std::static_pointer_cast<arrow::LargeStringArray>(chunk)->GetString(i))); break;
                case arrow::Type::NA:
                    ids.emplace_back(); break;
                default:
                    throw std::runtime_error(what + " id column must be int or string, not " +
                                             chunk->type()->ToString());
            }
        }
    }
    return ids;
}
// Optional boolean columns; a null cell means "not declared".
std::vector<uint8_t> read_flags(const std::shared_ptr<arrow::ChunkedArray>& column,
                                const std::string& what = "Boolean") {
    std::vector<uint8_t> flags;
    flags.reserve(column->length());
    for (const auto& chunk : column->chunks()) {
        for (int64_t i = 0; i < chunk->length(); ++i) {
            if (chunk->IsNull(i)) { flags.push_back(0); continue; }
            switch (chunk->type_id()) {
                case arrow::Type::BOOL:
                    flags.push_back(std::static_pointer_cast<arrow::BooleanArray>(chunk)->Value(i) ? 1 : 0); break;
                case arrow::Type::INT64:
                    flags.push_back(std::static_pointer_cast<arrow::Int64Array>(chunk)->Value(i) != 0 ? 1 : 0); break;
                case arrow::Type::INT32:
                    flags.push_back(std::static_pointer_cast<arrow::Int32Array>(chunk)->Value(i) != 0 ? 1 : 0); break;
                case arrow::Type::NA:
                    flags.push_back(0); break;
                default:
                    throw std::runtime_error(what + " column must be boolean, not " +
                                             chunk->type()->ToString());
            }
        }
    }
    return flags;
}
// The successor ratio multiplies a price, so it keeps double precision.
std::vector<double> read_doubles(const std::shared_ptr<arrow::ChunkedArray>& column,
                                 const std::string& what = "ID") {
    std::vector<double> values;
    values.reserve(column->length());
    const double absent = std::numeric_limits<double>::quiet_NaN();
    for (const auto& chunk : column->chunks()) {
        for (int64_t i = 0; i < chunk->length(); ++i) {
            if (chunk->IsNull(i)) { values.push_back(absent); continue; }
            switch (chunk->type_id()) {
                case arrow::Type::DOUBLE:
                    values.push_back(std::static_pointer_cast<arrow::DoubleArray>(chunk)->Value(i)); break;
                case arrow::Type::FLOAT:
                    values.push_back(std::static_pointer_cast<arrow::FloatArray>(chunk)->Value(i)); break;
                case arrow::Type::INT64:
                    values.push_back(static_cast<double>(std::static_pointer_cast<arrow::Int64Array>(chunk)->Value(i))); break;
                case arrow::Type::INT32:
                    values.push_back(static_cast<double>(std::static_pointer_cast<arrow::Int32Array>(chunk)->Value(i))); break;
                case arrow::Type::NA:
                    values.push_back(absent); break;
                default:
                    throw std::runtime_error(what + " numeric column must be float, double or int, not " +
                                             chunk->type()->ToString());
            }
        }
    }
    return values;
}
struct EventDates {
    std::vector<int64_t> epoch_ms;
    std::vector<uint8_t> present; // 0 = null cell, i.e. no event for that security
};
EventDates read_event_dates(const std::shared_ptr<arrow::ChunkedArray>& column,
                            const std::string& what = "date") {
    EventDates dates;
    dates.epoch_ms.reserve(column->length());
    dates.present.reserve(column->length());
    for (const auto& chunk : column->chunks()) {
        const auto type = chunk->type_id();
        int64_t multiply = 1, divide = 1;
        if (type == arrow::Type::TIMESTAMP) {
            const auto unit = std::static_pointer_cast<arrow::TimestampType>(chunk->type())->unit();
            if (unit == arrow::TimeUnit::SECOND) multiply = 1000;
            else if (unit == arrow::TimeUnit::MICRO) divide = 1000;
            else if (unit == arrow::TimeUnit::NANO) divide = 1000000;
        } else if (type != arrow::Type::DATE32 && type != arrow::Type::DATE64 && type != arrow::Type::NA &&
                   type != arrow::Type::STRING && type != arrow::Type::LARGE_STRING)
            throw std::runtime_error("Unsupported " + what + " type: " + chunk->type()->ToString());
        for (int64_t i = 0; i < chunk->length(); ++i) {
            if (chunk->IsNull(i)) { dates.epoch_ms.push_back(0); dates.present.push_back(0); continue; }
            int64_t value = 0;
            switch (type) {
                case arrow::Type::TIMESTAMP:
                    value = time_value(std::static_pointer_cast<arrow::TimestampArray>(chunk)->Value(i), multiply, divide); break;
                case arrow::Type::DATE32:
                    value = time_value(std::static_pointer_cast<arrow::Date32Array>(chunk)->Value(i), day_ms, 1); break;
                case arrow::Type::DATE64:
                    value = std::static_pointer_cast<arrow::Date64Array>(chunk)->Value(i); break;
                case arrow::Type::STRING:
                    value = config_date_ms(trimmed(std::static_pointer_cast<arrow::StringArray>(chunk)->GetString(i))); break;
                case arrow::Type::LARGE_STRING:
                    value = config_date_ms(trimmed(std::static_pointer_cast<arrow::LargeStringArray>(chunk)->GetString(i))); break;
                default: break; // null-typed chunks have no values
            }
            dates.epoch_ms.push_back(value);
            dates.present.push_back(1);
        }
    }
    return dates;
}
// Nullable universe names; a null cell becomes empty.
std::vector<std::string> read_strings(const std::shared_ptr<arrow::ChunkedArray>& column,
                                      const std::string& what) {
    std::vector<std::string> values;
    values.reserve(column->length());
    for (const auto& chunk : column->chunks()) {
        for (int64_t i = 0; i < chunk->length(); ++i) {
            if (chunk->IsNull(i)) { values.emplace_back(); continue; }
            switch (chunk->type_id()) {
                case arrow::Type::STRING:
                    values.push_back(trimmed(std::static_pointer_cast<arrow::StringArray>(chunk)->GetString(i))); break;
                case arrow::Type::LARGE_STRING:
                    values.push_back(trimmed(std::static_pointer_cast<arrow::LargeStringArray>(chunk)->GetString(i))); break;
                case arrow::Type::NA:
                    values.emplace_back(); break;
                default:
                    throw std::runtime_error(what + " column must be string, not " + chunk->type()->ToString());
            }
        }
    }
    return values;
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
// ---------------------------------------------------------------------------
// Thin-v1 cache (docs/data_contract.md). Every file is
// long and keyed by int64 tradingItemId; ids become their decimal string like the prices.
// ---------------------------------------------------------------------------
int64_t day_of(int64_t epoch_ms) { return epoch_ms / day_ms - (epoch_ms % day_ms < 0 ? 1 : 0); }
std::shared_ptr<arrow::Table> read_table(const std::string& path, const std::vector<std::string>& columns) {
    auto source = arrow::io::ReadableFile::Open(path);
    if (!source.ok()) throw std::runtime_error("Cannot open parquet " + path + ": " + source.status().ToString());
    parquet::arrow::FileReaderBuilder builder;
    require_status(builder.Open(*source), "Cannot create parquet reader for " + path);
    auto built = builder.Build();
    if (!built.ok()) throw std::runtime_error("Cannot build parquet reader: " + built.status().ToString());
    auto reader = std::move(*built);
    reader->set_use_threads(false);
    std::shared_ptr<arrow::Schema> schema;
    require_status(reader->GetSchema(&schema), "Cannot read parquet schema of " + path);
    std::vector<int> projection;
    for (const auto& name : columns) {
        const int index = schema->GetFieldIndex(name);
        if (index < 0) throw std::runtime_error("Parquet column not found: " + name + " in " + path);
        projection.push_back(index);
    }
    std::shared_ptr<arrow::Table> table;
    require_status(reader->ReadTable(projection, &table), "Cannot read columns from " + path);
    return table;
}
bool interval_membership(const std::string& path) {
    auto source = arrow::io::ReadableFile::Open(path);
    if (!source.ok()) throw std::runtime_error("Cannot open universe parquet " + path + ": " + source.status().ToString());
    parquet::arrow::FileReaderBuilder builder;
    require_status(builder.Open(*source), "Cannot create parquet reader for " + path);
    auto built = builder.Build();
    if (!built.ok()) throw std::runtime_error("Cannot build parquet reader: " + built.status().ToString());
    std::shared_ptr<arrow::Schema> schema;
    require_status((*built)->GetSchema(&schema), "Cannot read parquet schema of " + path);
    for (const char* name : {"universe", "tradingItemId", "fromDate", "toDate"})
        if (schema->GetFieldIndex(name) < 0) return false;
    return true;
}
// Interval membership (universe, tradingItemId, fromDate, toDate; both ends inclusive, null
// toDate = open) for each universe named in the config (universe.names, else universe.name).
std::map<std::string, std::map<std::string, std::vector<uint8_t>>> read_interval_universe(const BacktestConfig& config,
    const std::vector<int64_t>& calendar, const std::map<std::string, int>& price_symbols) {
    const std::vector<std::string> wanted = config.universe.names.empty()
        ? std::vector<std::string>{config.universe.name} : config.universe.names;
    const std::string path = resolved(config, config.universe.path);
    auto table = read_table(path, {"universe", "tradingItemId", "fromDate", "toDate"});
    auto names = read_strings(table->GetColumnByName("universe"), "Universe name");
    auto ids = read_event_ids(table->GetColumnByName("tradingItemId"), "Universe");
    auto from = read_event_dates(table->GetColumnByName("fromDate"), "universe fromDate");
    auto to = read_event_dates(table->GetColumnByName("toDate"), "universe toDate");
    std::set<std::string> requested(config.universe.symbols.begin(), config.universe.symbols.end());
    std::map<std::string, std::map<std::string, std::vector<uint8_t>>> sets;
    std::set<std::string> named;
    for (size_t row = 0; row < ids.size(); ++row) {
        if (std::find(wanted.begin(), wanted.end(), names[row]) == wanted.end()) continue;
        named.insert(names[row]);
        auto& result = sets[names[row]];
        if (!from.present[row]) throw std::runtime_error("Universe interval has no fromDate: " + ids[row]);
        if (!requested.empty() && !requested.count(ids[row])) continue;
        const int64_t first = day_of(from.epoch_ms[row]);
        const int64_t last = to.present[row] ? day_of(to.epoch_ms[row]) : std::numeric_limits<int64_t>::max();
        for (size_t t = 0; t < calendar.size(); ++t) {
            const int64_t day = day_of(calendar[t]);
            if (day < first || day > last) continue;
            auto& mask = result[ids[row]];
            if (mask.empty()) mask.assign(calendar.size(), 0);
            mask[t] = 1;
        }
    }
    for (const auto& name : wanted)
        if (!named.count(name)) throw std::runtime_error("Universe '" + name + "' has no rows in " + path);
    for (const auto& set : sets)
        for (const auto& member : set.second)
            if (!price_symbols.count(member.first))
                throw std::runtime_error("Universe contains active symbol absent from close: " + member.first);
    return sets;
}
struct ThinEvents {
    std::vector<int64_t> dates;
    std::vector<std::string> sources, destinations; // empty destination = none
    std::vector<double> ratios;
    std::vector<uint8_t> ends;
};
ThinEvents read_thin_events(const std::string& path) {
    auto table = read_table(path, {"date", "sourceId", "destinationId", "valueRatio", "sourceEnds"});
    ThinEvents events;
    events.dates = read_times(table->GetColumnByName("date"));
    events.sources = read_event_ids(table->GetColumnByName("sourceId"), "Thin events source");
    events.destinations = read_optional_ids(table->GetColumnByName("destinationId"), "Thin events destination");
    events.ratios = read_doubles(table->GetColumnByName("valueRatio"), "Thin events");
    events.ends = read_flags(table->GetColumnByName("sourceEnds"), "Thin events sourceEnds");
    const size_t rows = events.dates.size();
    if (events.sources.size() != rows || events.destinations.size() != rows ||
        events.ratios.size() != rows || events.ends.size() != rows)
        throw std::runtime_error("Thin events columns have inconsistent lengths in " + path);
    return events;
}
// Long (date, tradingItemId, column) file onto the panel coordinates in double; no row = NaN.
std::vector<double> read_long_doubles(const std::string& path, const std::string& column,
                                      const std::vector<int64_t>& timestamps, const std::vector<std::string>& symbols) {
    const size_t T = timestamps.size(), N = symbols.size();
    std::vector<double> output(cell_count(T, N), std::numeric_limits<double>::quiet_NaN());
    ParquetFile file(path, "date", "tradingItemId", false);
    std::map<int64_t, size_t> dates;
    for (size_t t = 0; t < T; ++t) dates.emplace(timestamps[t], t);
    std::map<int, size_t> columns;
    for (size_t n = 0; n < N; ++n) {
        auto symbol = file.symbols.find(symbols[n]);
        if (symbol != file.symbols.end()) columns.emplace(symbol->second, n);
    }
    std::vector<size_t> rows, destinations;
    for (size_t row : file.order) {
        auto t = dates.find(file.times[row]);
        auto n = columns.find(file.row_symbols[row]);
        if (t != dates.end() && n != columns.end()) {
            rows.push_back(row);
            destinations.push_back(t->second * N + n->second);
        }
    }
    auto selection = file.select(rows);
    if (!selection.groups.empty()) {
        auto values = read_doubles(file.read({file.index(column)}, selection)->column(0), column);
        for (size_t i = 0; i < rows.size(); ++i) output[destinations[i]] = values[file.selected_index(rows[i], selection)];
    }
    return output;
}
// Split factor intervals [validFrom, validTo) by calendar day (validTo null = open), as in
// tools/data/cache_v21/loader.py. Cells without an interval stay NaN.
std::vector<float> read_adj_factor(const std::string& path, const std::vector<int64_t>& timestamps,
                                   const std::vector<std::string>& symbols) {
    const size_t T = timestamps.size(), N = symbols.size();
    std::vector<float> output(cell_count(T, N), missing_value);
    auto table = read_table(path, {"tradingItemId", "validFrom", "validTo", "factor"});
    auto ids = read_event_ids(table->GetColumnByName("tradingItemId"), "adj_factor");
    auto from = read_event_dates(table->GetColumnByName("validFrom"), "adj_factor validFrom");
    auto to = read_event_dates(table->GetColumnByName("validTo"), "adj_factor validTo");
    auto factors = read_doubles(table->GetColumnByName("factor"), "adj_factor");
    std::unordered_map<std::string, size_t> index;
    for (size_t n = 0; n < N; ++n) index.emplace(symbols[n], n);
    for (size_t row = 0; row < ids.size(); ++row) {
        auto n = index.find(ids[row]);
        if (n == index.end()) continue;
        if (!from.present[row]) throw std::runtime_error("adj_factor interval has no validFrom: " + ids[row]);
        const int64_t first = day_of(from.epoch_ms[row]);
        const int64_t end = to.present[row] ? day_of(to.epoch_ms[row]) : std::numeric_limits<int64_t>::max();
        for (size_t t = 0; t < T; ++t) {
            const int64_t day = day_of(timestamps[t]);
            if (day < first || day >= end) continue;
            float& cell = output[t * N + n->second];
            if (!std::isnan(cell)) throw std::runtime_error("adj_factor intervals overlap for " + ids[row]);
            cell = static_cast<float>(factors[row]);
        }
    }
    return output;
}
}

std::map<std::string, std::string> load_thin_symbol_labels(
    const BacktestConfig& config, const std::vector<std::string>& panel_symbols) {
    const auto path = (std::filesystem::path(config.config_dir) / config.base_cache / "master.parquet")
                          .lexically_normal().string();
    auto table = read_table(path, {"tradingItemId", "symbol", "companyName"});
    auto ids = read_event_ids(table->GetColumnByName("tradingItemId"), "Master");
    auto symbols = read_strings(table->GetColumnByName("symbol"), "Master symbol");
    auto companies = read_strings(table->GetColumnByName("companyName"), "Master companyName");
    const std::unordered_set<std::string> selected(panel_symbols.begin(), panel_symbols.end());
    std::map<std::string, std::string> labels;
    for (size_t row = 0; row < ids.size(); ++row) {
        if (!selected.count(ids[row])) continue;
        const auto& label = symbols[row].empty() ? companies[row] : symbols[row];
        if (!label.empty()) labels.emplace(ids[row], label);
    }
    return labels;
}

DataPanel load_config(const BacktestConfig& config, const std::vector<std::string>& selected_fields) {
    if (config.execution_price != "close" && config.execution_price != "ohlc4" && config.execution_price != "vwap")
        throw std::runtime_error("backtest.execution_price must be 'close', 'ohlc4' or 'vwap'");
    std::map<std::string, std::pair<Source, bool>> sources;
    for (const auto& dataset : config.datasets) if (dataset.enabled) {
        for (const auto& field : dataset.fields) {
            if (field.first == "returns" && !(config.base_thin && dataset.name == "base_returns"))
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
        if (!sources.count(field) && !(config.base_thin && field == "adj_factor")) throw std::runtime_error("Requested data field is not registered: " + field);
    selected.insert("close");
    std::set<std::string> execution_fields;
    if (config.execution_price != "close") {
        const bool vwap = config.execution_price == "vwap";
        const std::string label = vwap ? "VWAP" : "OHLC4";
        for (const char* name : {vwap ? "vwap" : "open", "high", "low"}) {
            const auto source = sources.find(name);
            if (source == sources.end())
                throw std::runtime_error(label + " execution requires data field: " + name);
            if (source->second.first.alignment != FieldAlignment::Exact)
                throw std::runtime_error(label + " execution requires exact alignment for " + name);
            selected.insert(name);
            execution_fields.insert(name);
        }
    }
    const auto& close_source = close->second.first;
    auto master = std::make_unique<ParquetFile>(close_source.path, close_source.time_col, close_source.symbol_col,
                                                close_source.format == DatasetFormat::Wide);
    const auto start = config.start_date.empty() ? std::numeric_limits<int64_t>::min() : config_date_ms(config.start_date);
    const bool latest_end = config.end_date.empty() || config.end_date == "latest";
    const auto end = latest_end ? std::numeric_limits<int64_t>::max() : config_date_ms(config.end_date) + 86399999;
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
    std::map<std::string, std::map<std::string, std::vector<uint8_t>>> named_sets; // universe.names
    if (!config.universe.path.empty()) {
        const bool intervals = interval_membership(resolved(config, config.universe.path));
        if (intervals && config.universe.name.empty())
            throw std::runtime_error("universe.path is an interval membership file: universe.name is required");
        if (!intervals && !config.universe.name.empty())
            throw std::runtime_error("universe.name applies only to an interval membership file");
        if (!intervals && !config.universe.names.empty())
            throw std::runtime_error("universe.names applies only to an interval membership file");
        if (intervals) {
            named_sets = read_interval_universe(config, panel.timestamps, master->symbols);
            membership = named_sets.at(config.universe.name);
            std::set<std::string> all;
            for (const auto& set : named_sets)
                for (const auto& member : set.second) all.insert(member.first);
            panel.symbols.assign(all.begin(), all.end());
        } else {
            membership = read_universe(config, panel.timestamps, master->symbols);
            for (const auto& member : membership) panel.symbols.push_back(member.first);
        }
    } else if (!config.universe.symbols.empty()) {
        panel.symbols = config.universe.symbols;
        std::sort(panel.symbols.begin(), panel.symbols.end());
    } else for (const auto& symbol : master->symbols) panel.symbols.push_back(symbol.first);
    if (panel.symbols.empty()) throw std::runtime_error("Selected universe is empty in the configured period");
    if (std::adjacent_find(panel.symbols.begin(), panel.symbols.end()) != panel.symbols.end()) throw std::runtime_error("Duplicate selected symbols");
    for (const auto& symbol : panel.symbols)
        if (!master->symbols.count(symbol)) throw std::runtime_error("Selected symbol is absent from close: " + symbol);
    // Thin cache: positions an event moves into must be in the panel, so the destinations of
    // loaded sources (transitively) are loaded too; they may lack prices and stay out of the mask.
    const std::string thin_dir = config.base_thin ? resolved(config, config.base_cache) : "";
    ThinEvents thin_events;
    if (config.base_thin) {
        thin_events = read_thin_events((std::filesystem::path(thin_dir) / "events.parquet").string());
        std::set<std::string> loaded(panel.symbols.begin(), panel.symbols.end());
        for (bool added = true; added;) {
            added = false;
            for (size_t row = 0; row < thin_events.dates.size(); ++row)
                if (thin_events.dates[row] >= panel.timestamps.front() && thin_events.dates[row] <= panel.timestamps.back() &&
                    !thin_events.destinations[row].empty() && loaded.count(thin_events.sources[row]) &&
                    loaded.insert(thin_events.destinations[row]).second) added = true;
        }
        panel.symbols.assign(loaded.begin(), loaded.end());
    }
    if (panel.symbols.size() > std::numeric_limits<unsigned>::max()) throw std::runtime_error("Too many symbols");
    panel.N = static_cast<unsigned>(panel.symbols.size());
    auto build_mask = [&](const std::map<std::string, std::vector<uint8_t>>& members) {
        std::vector<uint8_t> mask(cell_count(panel.T, panel.N));
        for (size_t n = 0; n < panel.N; ++n) {
            auto member = members.find(panel.symbols[n]);
            if (member == members.end()) continue; // added event destination / other set: never a member
            for (size_t t = 0; t < panel.T; ++t) mask[t * panel.N + n] = member->second[t];
        }
        return mask;
    };
    if (!membership.empty() || !named_sets.empty()) panel.universe_mask = build_mask(membership);
    if (!config.universe.names.empty()) {
        panel.universe_names = config.universe.names;
        panel.default_universe = config.universe.name;
        for (const auto& set : named_sets)
            if (set.first != config.universe.name) panel.universe_masks[set.first] = build_mask(set.second);
    }
    named_sets.clear();
    // Only coordinates survive into value loaders; release the wide master footer/schema first.
    master.reset();
    auto close_values = read_field(close_source, Coordinates{panel.timestamps, panel.symbols});
    if (latest_end) {
        // Only trim an entirely missing tail. Keep interior gaps, partial rows,
        // invalid non-null quotes, and all explicitly requested date ranges.
        size_t rows = panel.T;
        while (rows > panel.trade_start_index) {
            const auto begin = close_values.begin() + (rows - 1) * panel.N;
            if (!std::all_of(begin, begin + panel.N, [](float value) { return std::isnan(value); }))
                break;
            --rows;
        }
        if (rows <= panel.trade_start_index)
            throw std::runtime_error("No close prices in the configured trading period");
        panel.T = static_cast<unsigned>(rows);
        panel.timestamps.resize(rows);
        close_values.resize(cell_count(rows, panel.N));
        if (!panel.universe_mask.empty()) panel.universe_mask.resize(cell_count(rows, panel.N));
        for (auto& named : panel.universe_masks) named.second.resize(cell_count(rows, panel.N));
    }
    panel.arrays.emplace("close", std::move(close_values));
    const auto coordinates = std::make_shared<const Coordinates>(Coordinates{panel.timestamps, panel.symbols});
    std::map<std::string, std::shared_ptr<LazyColumn>> shared_sources;
    for (const auto& field : sources) {
        if (field.first == "close") continue; // Already loaded to resolve the latest end date.
        const bool requested = selected_fields.empty() || selected.count(field.first);
        if (!requested) continue;
        const Source source = field.second.first;
        const std::string key = source.path + "\n" + source.time_col + "\n" + source.symbol_col + "\n" +
            (source.format == DatasetFormat::Long ? source.column : "") + "\n" + std::to_string(static_cast<int>(source.alignment));
        if (!field.second.second || execution_fields.count(field.first)) {
            panel.arrays.emplace(field.first, read_field(source, *coordinates));
        } else {
            auto& column = shared_sources[key];
            if (!column) column = std::make_shared<LazyColumn>([source, coordinates] { return read_field(source, *coordinates); });
            panel.lazy_columns.emplace(field.first, column);
        }
    }
    // A group panel built for another cache (or left behind when the price cache
    // grew) aligns to nothing on the affected bars and turns every code into NaN.
    // `neutralize` would then emit an all-NaN row and the book would quietly empty
    // over that stretch, so refuse the start.
    if (!config.base_groups.empty()) {
        const auto& close_array = panel.arrays.at("close");
        for (const char* field : {"sector", "industry"}) {
            const auto codes = panel.arrays.find(field);
            if (codes == panel.arrays.end()) continue; // not selected for this run
            for (unsigned t = 0; t < panel.T; ++t) {
                const size_t offset = static_cast<size_t>(t) * panel.N;
                bool quoted = false, classified = false;
                for (unsigned n = 0; n < panel.N && !(quoted && classified); ++n) {
                    quoted = quoted || std::isfinite(close_array[offset + n]);
                    classified = classified || std::isfinite(codes->second[offset + n]);
                }
                if (quoted && !classified)
                    throw std::runtime_error(
                        std::string("Group panel '") + field + "' has no codes on " +
                        iso_date(panel.timestamps[t]) +
                        ": base.groups is stale or was built for another cache;"
                        " regenerate it with tools/data/build_group_panels.py");
            }
        }
    }
    if (config.base_thin) {
        panel.thin_layout = true;
        const std::filesystem::path dir(thin_dir);
        if (selected_fields.empty() || selected.count("adj_factor"))
            panel.arrays.emplace("adj_factor", read_adj_factor((dir / "adj_factor.parquet").string(),
                                                               panel.timestamps, panel.symbols));
        panel.returns_total = read_long_doubles((dir / "returns.parquet").string(), "returns",
                                                panel.timestamps, panel.symbols);
        std::unordered_map<std::string, unsigned> index;
        for (unsigned n = 0; n < panel.N; ++n) index.emplace(panel.symbols[n], n);
        for (size_t row = 0; row < thin_events.dates.size(); ++row) {
            // A source that ended before the first bar is retired from bar 0;
            // its return and any move belong to dates the panel does not have.
            if (thin_events.dates[row] < panel.timestamps.front()) {
                auto source = index.find(thin_events.sources[row]);
                if (!thin_events.ends[row] || source == index.end()) continue;
                DataPanel::MoveEvent event;
                event.bar = 0;
                event.source = source->second;
                event.source_ends = true;
                panel.move_events.push_back(event);
                continue;
            }
            auto bar = std::lower_bound(panel.timestamps.begin(), panel.timestamps.end(), thin_events.dates[row]);
            if (bar == panel.timestamps.end() || *bar != thin_events.dates[row]) {
                // Outside the panel's dates the row simply does not apply. Inside them a date that is not
                // a bar would drop a move (an ending source would stay frozen), so the cache is refused.
                if (thin_events.dates[row] > panel.timestamps.front() && thin_events.dates[row] < panel.timestamps.back())
                    throw std::runtime_error("Thin events date " + iso_date(thin_events.dates[row]) +
                                             " is inside the panel but not a panel bar - rebuild the thin cache");
                continue;
            }
            auto source = index.find(thin_events.sources[row]);
            if (source == index.end()) continue; // source not loaded
            DataPanel::MoveEvent event;
            event.bar = static_cast<unsigned>(bar - panel.timestamps.begin());
            event.source = source->second;
            auto destination = index.find(thin_events.destinations[row]);
            if (destination != index.end()) event.destination = static_cast<int32_t>(destination->second);
            event.value_ratio = thin_events.ratios[row];
            event.source_ends = thin_events.ends[row] != 0;
            panel.move_events.push_back(event);
        }
        std::stable_sort(panel.move_events.begin(), panel.move_events.end(),
            [](const DataPanel::MoveEvent& a, const DataPanel::MoveEvent& b) {
                return a.bar != b.bar ? a.bar < b.bar : a.source < b.source;
            });
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
