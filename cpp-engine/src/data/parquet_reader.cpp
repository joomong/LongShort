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
// ---------------------------------------------------------------------------
// Delisting/settlement events. One row per security; matched to panel symbols by
// a normalized string ID. The panel keeps prices untouched; only the settlement
// schedule is derived here.
// ---------------------------------------------------------------------------
constexpr int64_t day_ms = 86400000;
int64_t day_end_ms(int64_t value) {
    // The security is held through the whole calendar day of its end date and is
    // settled on the next panel bar, so compare against the last millisecond of that day.
    int64_t days = value / day_ms;
    if (value % day_ms < 0) --days; // floor for pre-epoch end dates
    if (days > (std::numeric_limits<int64_t>::max() - (day_ms - 1)) / day_ms)
        return std::numeric_limits<int64_t>::max(); // a far-future end date never settles inside a panel
    return days * day_ms + (day_ms - 1);
}
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
// The readers below serve the events file and the corporate-action ledger; `what`
// names the file in their messages.
std::vector<std::string> read_event_ids(const std::shared_ptr<arrow::ChunkedArray>& column,
                                        const std::string& what = "Events") {
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
// Successor ids are optional per row, so nulls are allowed and become empty.
std::vector<std::string> read_optional_ids(const std::shared_ptr<arrow::ChunkedArray>& column,
                                           const std::string& what = "Events successor") {
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
                                const std::string& what = "Events leg") {
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
                                 const std::string& what = "Events") {
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
                            const std::string& what = "events end date") {
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
void read_events(const BacktestConfig& config, DataPanel& panel) {
    if (config.events.path.empty()) return;
    const std::string path = resolved(config, config.events.path);
    std::error_code lookup;
    // A configured events file must exist: a silently ignored typo would disable every settlement.
    if (!std::filesystem::exists(path, lookup))
        throw std::runtime_error("Events parquet not found: " + path);
    auto source = arrow::io::ReadableFile::Open(path);
    if (!source.ok()) throw std::runtime_error("Cannot open events parquet " + path + ": " + source.status().ToString());
    parquet::arrow::FileReaderBuilder builder;
    require_status(builder.Open(*source), "Cannot create events parquet reader for " + path);
    builder.memory_pool(arrow::default_memory_pool());
    auto built = builder.Build();
    if (!built.ok()) throw std::runtime_error("Cannot build events parquet reader: " + built.status().ToString());
    auto reader = std::move(*built);
    reader->set_use_threads(false);
    std::shared_ptr<arrow::Schema> schema;
    require_status(reader->GetSchema(&schema), "Cannot read events parquet schema");
    const auto column_index = [&](const std::string& name) {
        const int index = schema->GetFieldIndex(name);
        if (index < 0) throw std::runtime_error("Events parquet column not found: " + name + " in " + path);
        return index;
    };
    const bool explicit_returns = !config.events.return_col.empty();
    const bool has_successors = !config.events.successor_col.empty();
    const bool has_ratios = !config.events.successor_ratio_col.empty();
    const bool has_cash = !config.events.cash_col.empty();
    const bool has_successor_guard = !config.events.successor_price_col.empty() &&
                                     !config.events.successor_price_date_col.empty();
    const bool has_target_guard = !config.events.target_price_col.empty() &&
                                  !config.events.target_price_date_col.empty();
    const bool has_stock_flag = !config.events.stock_leg_col.empty();
    const bool has_cash_flag = !config.events.cash_leg_col.empty();
    std::vector<int> projection{column_index(config.events.id_col), column_index(config.events.end_date_col)};
    if (explicit_returns) projection.push_back(column_index(config.events.return_col));
    if (has_successors) projection.push_back(column_index(config.events.successor_col));
    if (has_ratios) projection.push_back(column_index(config.events.successor_ratio_col));
    if (has_cash) projection.push_back(column_index(config.events.cash_col));
    if (has_successor_guard) {
        projection.push_back(column_index(config.events.successor_price_col));
        projection.push_back(column_index(config.events.successor_price_date_col));
    }
    if (has_target_guard) {
        projection.push_back(column_index(config.events.target_price_col));
        projection.push_back(column_index(config.events.target_price_date_col));
    }
    if (has_stock_flag) projection.push_back(column_index(config.events.stock_leg_col));
    if (has_cash_flag) projection.push_back(column_index(config.events.cash_leg_col));
    std::shared_ptr<arrow::Table> table;
    require_status(reader->ReadTable(projection, &table), "Cannot read events columns from " + path);
    auto ids = read_event_ids(table->GetColumnByName(config.events.id_col));
    auto ends = read_event_dates(table->GetColumnByName(config.events.end_date_col));
    std::vector<float> returns;
    if (explicit_returns) returns = numbers(table->GetColumnByName(config.events.return_col));
    std::vector<std::string> successors;
    if (has_successors) successors = read_optional_ids(table->GetColumnByName(config.events.successor_col));
    std::vector<double> ratios;
    if (has_ratios) ratios = read_doubles(table->GetColumnByName(config.events.successor_ratio_col));
    std::vector<double> cash;
    if (has_cash) cash = read_doubles(table->GetColumnByName(config.events.cash_col));
    std::vector<double> guard_prices, target_prices;
    EventDates guard_dates, target_dates;
    if (has_successor_guard) {
        guard_prices = read_doubles(table->GetColumnByName(config.events.successor_price_col));
        guard_dates = read_event_dates(table->GetColumnByName(config.events.successor_price_date_col));
    }
    if (has_target_guard) {
        target_prices = read_doubles(table->GetColumnByName(config.events.target_price_col));
        target_dates = read_event_dates(table->GetColumnByName(config.events.target_price_date_col));
    }
    std::vector<uint8_t> stock_flags, cash_flags;
    if (has_stock_flag) stock_flags = read_flags(table->GetColumnByName(config.events.stock_leg_col));
    if (has_cash_flag) cash_flags = read_flags(table->GetColumnByName(config.events.cash_leg_col));
    table.reset();
    reader.reset(); // release decoded columns and the parquet footer before the panel is published
    if (ids.size() != ends.present.size() || (explicit_returns && returns.size() != ids.size()) ||
        (has_successors && successors.size() != ids.size()) || (has_ratios && ratios.size() != ids.size()) ||
        (has_cash && cash.size() != ids.size()) ||
        (has_successor_guard && (guard_prices.size() != ids.size() ||
                                 guard_dates.present.size() != ids.size())) ||
        (has_target_guard && (target_prices.size() != ids.size() ||
                              target_dates.present.size() != ids.size())) ||
        (has_stock_flag && stock_flags.size() != ids.size()) ||
        (has_cash_flag && cash_flags.size() != ids.size()))
        throw std::runtime_error("Events columns have inconsistent lengths in " + path);
    std::unordered_map<std::string, size_t> rows;
    rows.reserve(ids.size() * 2);
    for (size_t row = 0; row < ids.size(); ++row)
        if (!rows.emplace(ids[row], row).second)
            throw std::runtime_error("Duplicate events id: " + ids[row]);
    std::unordered_map<std::string, unsigned> panel_index;
    if (has_successors) {
        panel_index.reserve(panel.N * 2);
        for (unsigned n = 0; n < panel.N; ++n) panel_index.emplace(panel.symbols[n], n);
    }
    panel.settlement_bar.assign(panel.N, -1);
    panel.settlement_return.assign(panel.N, missing_value);
    panel.settlement_successor.assign(panel.N, -1);
    panel.settlement_successor_ratio.assign(panel.N, std::numeric_limits<double>::quiet_NaN());
    panel.settlement_cash.assign(panel.N, std::numeric_limits<double>::quiet_NaN());
    panel.settlement_has_stock_leg.assign(panel.N, 0);
    panel.settlement_has_cash_leg.assign(panel.N, 0);
    panel.settlement_successor_named.assign(panel.N, 0);
    std::vector<size_t> matched_row(panel.N, std::numeric_limits<size_t>::max());
    size_t unresolved_successors = 0;
    for (unsigned n = 0; n < panel.N; ++n) {
        auto match = rows.find(panel.symbols[n]);
        if (match == rows.end()) continue; // securities without a row keep -1/NaN
        const size_t row = match->second;
        matched_row[n] = row;
        ++panel.events_rows_matched;
        if (ends.present[row]) {
            const size_t bar = std::upper_bound(panel.timestamps.begin(), panel.timestamps.end(),
                                                day_end_ms(ends.epoch_ms[row])) - panel.timestamps.begin();
            if (bar < panel.T) panel.settlement_bar[n] = static_cast<int32_t>(bar); // >= T settles outside the panel
        }
        // An explicit return overrides the default whenever this security settles.
        if (explicit_returns && std::isfinite(returns[row])) panel.settlement_return[n] = returns[row];
        if (has_ratios) panel.settlement_successor_ratio[n] = ratios[row];
        if (has_cash) panel.settlement_cash[n] = cash[row];
        const bool named = has_successors && !successors[row].empty();
        panel.settlement_successor_named[n] = named ? 1 : 0;
        // The declared structure decides what has to be priced; the file may state
        // it, and otherwise the original terms imply it.
        panel.settlement_has_stock_leg[n] = has_stock_flag
            ? stock_flags[row]
            : ((has_ratios && std::isfinite(ratios[row])) || named ? 1 : 0);
        panel.settlement_has_cash_leg[n] = has_cash_flag
            ? cash_flags[row]
            : (has_cash && std::isfinite(cash[row]) ? 1 : 0);
        if (!named) continue;
        auto successor = panel_index.find(successors[row]);
        // A successor outside the loaded panel cannot receive the holding, so the
        // settlement stays a cash settlement; the count is reported once.
        if (successor == panel_index.end() || successor->second == n) { ++unresolved_successors; continue; }
        panel.settlement_successor[n] = static_cast<int32_t>(successor->second);
    }
    if (unresolved_successors > 0)
        std::cerr << "Events: " << unresolved_successors
                  << " successor id(s) are not in the loaded panel; those settle in cash" << std::endl;

    // ── Staleness guard ───────────────────────────────────────────────────
    // Cash and exchange ratios are stated in the price cache's share units, so a
    // rebuilt cache silently invalidates them. For every event this panel will
    // actually use, re-derive the bar the engine settles on and check the anchors
    // the generator recorded against this cache. Only an event this panel never
    // reaches may go unchecked.
    if ((has_cash || has_ratios) && !has_target_guard)
        throw std::runtime_error("Events config declares cache-unit inputs without "
            "target_price_col/target_price_date_col: the loader cannot verify them");
    if (has_ratios && !has_successor_guard)
        throw std::runtime_error("Events config declares successor_ratio_col without "
            "successor_price_col/successor_price_date_col: the loader cannot verify it");
    if (!has_successor_guard && !has_target_guard) return;
    const auto& close_values = panel.arrays.at("close");
    const auto observed = [&](size_t bar, unsigned asset) {
        const float value = close_values[bar * panel.N + asset];
        return std::isfinite(value) && value > 0.0f;
    };
    const auto same_bar = [&](size_t bar, int64_t stamp) {
        // An exact stamp pins an intraday bar; a midnight stamp is a calendar day.
        if (panel.timestamps[bar] == stamp) return true;
        return stamp % day_ms == 0 && day_end_ms(panel.timestamps[bar]) == day_end_ms(stamp);
    };
    const auto compare = [&](const std::string& symbol, int64_t stamp, double expected, double cached) {
        const double scale = std::max(std::max(std::fabs(cached), std::fabs(expected)), 1e-12);
        if (std::isfinite(cached) && std::fabs(cached - expected) <= 1e-4 * scale) return;
        throw std::runtime_error("Events file is stale against the price cache: " + symbol + " " +
            iso_date(stamp) + " file=" + std::to_string(expected) + " cache=" + std::to_string(cached) +
            " - regenerate build_events.py");
    };
    const auto wrong_date = [&](const std::string& side, const std::string& symbol,
                                int64_t stamp, size_t bar) {
        return std::runtime_error("Events file is stale against the price cache: " + symbol + " " +
            iso_date(stamp) + " " + side + " date is not the settlement reference " +
            iso_date(panel.timestamps[bar]) + " - regenerate build_events.py");
    };
    for (unsigned n = 0; n < panel.N; ++n) {
        const size_t row = matched_row[n];
        if (row == std::numeric_limits<size_t>::max()) continue;
        const double ratio_input = has_ratios ? ratios[row] : std::numeric_limits<double>::quiet_NaN();
        const double cash_input = has_cash ? cash[row] : std::numeric_limits<double>::quiet_NaN();
        const bool evaluable = std::isfinite(ratio_input) || std::isfinite(cash_input);
        const bool successor_anchor = has_successor_guard &&
            guard_dates.present[row] && std::isfinite(guard_prices[row]);
        const bool target_anchor = has_target_guard &&
            target_dates.present[row] && std::isfinite(target_prices[row]);
        // An input the engine will multiply into a price must come with the anchor
        // that proves which cache it was restated against.
        if (evaluable && !target_anchor)
            throw std::runtime_error("Events file has evaluation inputs without price anchors: " +
                panel.symbols[n] + " target anchor missing - regenerate build_events.py");
        if (std::isfinite(ratio_input) && !successor_anchor)
            throw std::runtime_error("Events file has evaluation inputs without price anchors: " +
                panel.symbols[n] + " successor anchor missing - regenerate build_events.py");
        if (!evaluable) { ++panel.events_price_checks_not_evaluable; continue; }

        // The engine settles on the first bar at or after the end date that stops
        // quoting. No such bar means this panel never settles the event.
        size_t settlement = std::numeric_limits<size_t>::max();
        if (panel.settlement_bar[n] >= 0)
            for (size_t bar = static_cast<size_t>(panel.settlement_bar[n]); bar < panel.T; ++bar)
                if (!observed(bar, n)) { settlement = bar; break; }
        if (settlement == std::numeric_limits<size_t>::max()) {
            ++panel.events_price_checks_out_of_range;
            continue;
        }

        bool checked = false;
        const int32_t successor = panel.settlement_successor[n];
        if (successor_anchor && successor >= 0) {
            const unsigned target = static_cast<unsigned>(successor);
            if (!same_bar(settlement, guard_dates.epoch_ms[row]))
                throw wrong_date("successor", panel.symbols[target],
                                 guard_dates.epoch_ms[row], settlement);
            compare(panel.symbols[target], guard_dates.epoch_ms[row], guard_prices[row],
                    close_values[settlement * panel.N + target]);
            checked = true;
        }
        if (target_anchor) {
            // The engine values the settling security at its last observed close.
            size_t mark_bar = std::numeric_limits<size_t>::max();
            for (size_t bar = settlement; bar-- > 0;)
                if (observed(bar, n)) { mark_bar = bar; break; }
            if (mark_bar == std::numeric_limits<size_t>::max()) {
                throw std::runtime_error("Events file is stale against the price cache: " +
                    panel.symbols[n] + " " + iso_date(target_dates.epoch_ms[row]) +
                    " target has no observed close before its settlement - regenerate build_events.py");
            } else {
                if (!same_bar(mark_bar, target_dates.epoch_ms[row]))
                    throw wrong_date("target", panel.symbols[n], target_dates.epoch_ms[row], mark_bar);
                compare(panel.symbols[n], target_dates.epoch_ms[row], target_prices[row],
                        close_values[mark_bar * panel.N + n]);
                checked = true;
            }
        }
        if (checked) ++panel.events_price_checks;
        else ++panel.events_price_checks_not_evaluable;
    }
}
// ---------------------------------------------------------------------------
// Corporate-action ledger. One row per (parent, ex-date, leg) in the price
// cache's share basis (docs/corporate_actions_engine_2026-09-17.md). Rows are
// matched to panel symbols by the same normalized string ID as the events file
// and scheduled on the panel bar of their ex-date; prices stay untouched.
// ---------------------------------------------------------------------------
// Nullable string column; a null cell becomes empty.
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
void read_corporate_actions(const BacktestConfig& config, DataPanel& panel) {
    const CorporateActionsConfig& ledger = config.corporate_actions;
    if (ledger.path.empty()) return;
    const std::string path = resolved(config, ledger.path);
    std::error_code lookup;
    // A configured ledger must exist: a silently ignored typo would drop every dividend.
    if (!std::filesystem::exists(path, lookup))
        throw std::runtime_error("Corporate actions parquet not found: " + path);
    auto source = arrow::io::ReadableFile::Open(path);
    if (!source.ok())
        throw std::runtime_error("Cannot open corporate actions parquet " + path + ": " + source.status().ToString());
    parquet::arrow::FileReaderBuilder builder;
    require_status(builder.Open(*source), "Cannot create corporate actions parquet reader for " + path);
    builder.memory_pool(arrow::default_memory_pool());
    auto built = builder.Build();
    if (!built.ok())
        throw std::runtime_error("Cannot build corporate actions parquet reader: " + built.status().ToString());
    auto reader = std::move(*built);
    reader->set_use_threads(false);
    std::shared_ptr<arrow::Schema> schema;
    require_status(reader->GetSchema(&schema), "Cannot read corporate actions parquet schema");
    const auto column_index = [&](const std::string& name) {
        const int index = schema->GetFieldIndex(name);
        if (index < 0)
            throw std::runtime_error("Corporate actions parquet column not found: " + name + " in " + path);
        return index;
    };
    std::vector<int> projection{
        column_index(ledger.id_col), column_index(ledger.ex_date_col), column_index(ledger.type_col),
        column_index(ledger.leg_col), column_index(ledger.cash_col), column_index(ledger.child_col),
        column_index(ledger.child_ratio_col), column_index(ledger.child_close_col),
        column_index(ledger.parent_close_col), column_index(ledger.parent_close_before_col),
        column_index(ledger.parent_factor_col), column_index(ledger.estimate_col),
        column_index(ledger.value_source_col), column_index(ledger.status_col)};
    // Rights terms are optional columns: an export without them still loads, and no
    // right expires (docs/rights_warrants_design_2026-09-17.md section 2).
    const bool has_expiry = schema->GetFieldIndex(ledger.expiry_col) >= 0;
    const bool has_subscription = schema->GetFieldIndex(ledger.subscription_price_col) >= 0;
    const bool has_exercise = schema->GetFieldIndex(ledger.exercise_ratio_col) >= 0;
    // The per-share value column is optional too: without it a child outside the panel is unpriced.
    const bool has_child_value = schema->GetFieldIndex(ledger.child_value_col) >= 0;
    const bool has_reason = schema->GetFieldIndex(ledger.estimate_reason_col) >= 0;
    if (has_expiry) projection.push_back(column_index(ledger.expiry_col));
    if (has_subscription) projection.push_back(column_index(ledger.subscription_price_col));
    if (has_exercise) projection.push_back(column_index(ledger.exercise_ratio_col));
    if (has_child_value) projection.push_back(column_index(ledger.child_value_col));
    if (has_reason) projection.push_back(column_index(ledger.estimate_reason_col));
    std::shared_ptr<arrow::Table> table;
    require_status(reader->ReadTable(projection, &table), "Cannot read corporate actions columns from " + path);
    const std::string what = "Corporate actions ledger";
    auto ids = read_event_ids(table->GetColumnByName(ledger.id_col), what);
    auto ex_dates = read_event_dates(table->GetColumnByName(ledger.ex_date_col), "corporate actions ex-date");
    auto types = read_strings(table->GetColumnByName(ledger.type_col), what + " event type");
    auto leg_types = read_strings(table->GetColumnByName(ledger.leg_col), what + " leg type");
    auto cash = read_doubles(table->GetColumnByName(ledger.cash_col), what);
    auto children = read_optional_ids(table->GetColumnByName(ledger.child_col), what + " child");
    auto ratios = read_doubles(table->GetColumnByName(ledger.child_ratio_col), what);
    auto child_closes = read_doubles(table->GetColumnByName(ledger.child_close_col), what);
    auto parent_closes = read_doubles(table->GetColumnByName(ledger.parent_close_col), what);
    auto parent_before = read_doubles(table->GetColumnByName(ledger.parent_close_before_col), what);
    auto factors = read_doubles(table->GetColumnByName(ledger.parent_factor_col), what);
    auto estimates = read_flags(table->GetColumnByName(ledger.estimate_col), what + " estimate");
    auto sources = read_strings(table->GetColumnByName(ledger.value_source_col), what + " value source");
    auto statuses = read_strings(table->GetColumnByName(ledger.status_col), what + " status");
    EventDates expiries;
    std::vector<double> subscriptions, exercises, child_values;
    if (has_expiry) expiries = read_event_dates(table->GetColumnByName(ledger.expiry_col), "corporate actions expiry date");
    if (has_subscription) subscriptions = read_doubles(table->GetColumnByName(ledger.subscription_price_col), what);
    if (has_exercise) exercises = read_doubles(table->GetColumnByName(ledger.exercise_ratio_col), what);
    if (has_child_value) child_values = read_doubles(table->GetColumnByName(ledger.child_value_col), what);
    std::vector<std::string> reasons;
    if (has_reason) reasons = read_strings(table->GetColumnByName(ledger.estimate_reason_col), what + " estimate reason");
    table.reset();
    reader.reset(); // release decoded columns and the parquet footer before the panel is published
    const size_t rows = ids.size();
    if (ex_dates.present.size() != rows || types.size() != rows || leg_types.size() != rows ||
        cash.size() != rows || children.size() != rows || ratios.size() != rows ||
        child_closes.size() != rows || parent_closes.size() != rows || parent_before.size() != rows ||
        factors.size() != rows || estimates.size() != rows || sources.size() != rows ||
        statuses.size() != rows || (has_expiry && expiries.present.size() != rows) ||
        (has_subscription && subscriptions.size() != rows) || (has_exercise && exercises.size() != rows) ||
        (has_child_value && child_values.size() != rows) || (has_reason && reasons.size() != rows))
        throw std::runtime_error("Corporate actions columns have inconsistent lengths in " + path);
    panel.corporate_actions_loaded = true;
    panel.corporate_action_rows = rows;
    panel.corporate_action_rights_terms_loaded = has_expiry;

    std::unordered_map<std::string, unsigned> panel_index;
    panel_index.reserve(panel.N * 2);
    for (unsigned n = 0; n < panel.N; ++n) panel_index.emplace(panel.symbols[n], n);
    const auto& close_values = panel.arrays.at("close");
    const auto observed = [&](size_t bar, unsigned asset) {
        const float value = close_values[bar * panel.N + asset];
        return std::isfinite(value) && value > 0.0f;
    };
    const auto row_label = [&](size_t row) {
        return ids[row] + " " + (ex_dates.present[row] ? iso_date(ex_dates.epoch_ms[row]) : "(no ex-date)");
    };
    const auto reject = [&](size_t row, const std::string& why) {
        return std::runtime_error("Corporate actions ledger row " + row_label(row) + " " + why);
    };
    const auto stale = [&](size_t row, const std::string& why) {
        return std::runtime_error("Corporate actions ledger is stale against the price cache: " +
            row_label(row) + " " + why + " - re-export the ledger for this cache");
    };
    const auto compare = [&](const std::string& anchor, size_t row, double expected, double cached) {
        const double scale = std::max(std::max(std::fabs(cached), std::fabs(expected)), 1e-12);
        if (std::fabs(cached - expected) <= 1e-4 * scale) return;
        throw stale(row, anchor + " file=" + std::to_string(expected) + " cache=" + std::to_string(cached));
    };
    // P10 tiers the ledger can price a child from when the panel has no quote on the
    // ex-date: (2) a when-issued close and (3)(4) valued estimates. A child_close_ex
    // value without a panel quote can only come from another cache.
    const auto usable_without_quote = [](const std::string& value_source) {
        return value_source == "wi_close" || value_source == "event_value" ||
               value_source == "vendor_implied";
    };
    const int64_t first_day = day_end_ms(panel.timestamps.front());
    const int64_t last_day = day_end_ms(panel.timestamps.back());
    size_t unresolved_children = 0;
    std::set<std::string> keys; // the ledger's own key: (parent, ex-date, leg type, child)
    for (size_t row = 0; row < rows; ++row) {
        // Basis and scope checks apply to every row: one merger row, one unadjusted
        // factor or one row that carries both legs means the file was built for
        // something else, and a repeated key would book the same leg twice.
        if (types[row].empty()) throw reject(row, "has no event type");
        if (types[row] == "merger_cash" || types[row] == "merger_stock" ||
            types[row] == "rename" || types[row] == "relist")
            throw reject(row, "is a " + types[row] +
                " event, which belongs to the events file (settlements), not the ledger");
        if (!std::isfinite(factors[row]) || std::fabs(factors[row] - 1.0) > 1e-9)
            throw reject(row, "has parentFactorCache " + std::to_string(factors[row]) +
                ": the engine panel is split-adjusted, so the ledger must be exported with basis split_adj");
        if (!ex_dates.present[row]) throw reject(row, "has no ex-date");
        const std::string& leg_type = leg_types[row];
        const bool cash_row = leg_type == "cash";
        const bool stock_row = leg_type == "stock" || leg_type == "rights";
        const bool quantity_row = leg_type == "quantity";
        if (!cash_row && !stock_row && !quantity_row)
            throw reject(row, "has leg type '" + leg_type + "'; expected cash, stock, rights or quantity");
        // Same-day cash and stock are two rows in the ledger; a row that carries both
        // cannot be keyed and would double-book against the row that carries one.
        if (!cash_row && std::isfinite(cash[row]))
            throw reject(row, "is a " + leg_type + " row but carries a cash amount");
        if (!stock_row && !children[row].empty())
            throw reject(row, "is a " + leg_type + " row but names a child");
        if (!stock_row && has_child_value && std::isfinite(child_values[row]))
            throw reject(row, "is a " + leg_type + " row but carries a child value");
        if (std::isfinite(ratios[row]) && ratios[row] < 0.0) throw reject(row, "has a negative child ratio");
        if (!keys.insert(ids[row] + "\n" + std::to_string(ex_dates.epoch_ms[row]) + "\n" + leg_type +
                         "\n" + children[row]).second)
            throw reject(row, "repeats an earlier (parent, ex-date, leg type, child) key");
        auto parent = panel_index.find(ids[row]);
        if (parent == panel_index.end()) continue; // parents outside the panel have nothing to book
        ++panel.corporate_action_rows_matched;
        const bool unresolved = statuses[row] == "unresolved";
        if (unresolved) ++panel.corporate_action_rows_unresolved;
        if (quantity_row) { ++panel.corporate_action_rows_quantity; continue; } // already in the split-adjusted prices
        // The ex-date has to be a bar of this panel; the ledger already moved
        // holiday dates to the next trading day, so a miss inside the range is a
        // calendar mismatch rather than something to shift silently.
        const int64_t ex_day = day_end_ms(ex_dates.epoch_ms[row]);
        if (ex_day < first_day || ex_day > last_day) { ++panel.corporate_action_out_of_range; continue; }
        const size_t bar = std::lower_bound(panel.timestamps.begin(), panel.timestamps.end(),
                                            ex_day - (day_ms - 1)) - panel.timestamps.begin();
        if (bar >= panel.T || day_end_ms(panel.timestamps[bar]) != ex_day)
            throw reject(row, "has an ex-date that is not a bar of this panel");
        DataPanel::CorporateActionLeg leg;
        leg.bar = static_cast<unsigned>(bar);
        leg.parent = parent->second;
        leg.stock_leg = stock_row;
        leg.rights = leg_type == "rights";
        leg.cash = cash[row];
        leg.child_ratio = ratios[row];
        leg.child_close = child_closes[row];
        leg.event_type = types[row];
        leg.value_source = sources[row];
        // Unresolved rows stay visible: a cash row without an amount and a stock row
        // without a child (the export's marker rows) are scheduled so the engine can
        // report them on held parents; they can never book anything.
        leg.child_named = stock_row && !children[row].empty();
        leg.ledger_value_usable = stock_row && usable_without_quote(leg.value_source);
        if (has_child_value && stock_row) leg.child_value = child_values[row];
        // A stock row without a child can still book its value when the ledger carries a
        // usable per-share value (P10 (2)(3)(4)); otherwise it is a marker row.
        const bool valued = leg.ledger_value_usable && std::isfinite(leg.child_value) && leg.child_value > 0.0;
        const bool unbookable = (cash_row && !std::isfinite(cash[row])) ||
                                (stock_row && !leg.child_named && !valued);
        leg.unresolved = unresolved || unbookable;
        if (unbookable) ++panel.corporate_action_legs_unbookable;
        // The ledger's own flag, its status, or a (3)(4) value source mark an estimate:
        // a stock row's child value, or a cash row's amount (vendor-implied cash). A
        // leg that cannot book anything is not an estimate of anything.
        leg.estimate = !unbookable &&
                       (estimates[row] != 0 || statuses[row] == "estimated" ||
                        (stock_row && (leg.value_source == "event_value" ||
                                       leg.value_source == "vendor_implied")));
        if (has_reason && leg.estimate) leg.estimate_reason = reasons[row];
        if (stock_row && !children[row].empty()) {
            auto child = panel_index.find(children[row]);
            if (child == panel_index.end()) {
                ++unresolved_children;
                ++panel.corporate_action_unpriced_children;
            } else if (child->second == leg.parent) {
                throw reject(row, "names the parent as its own child");
            } else {
                leg.child = static_cast<int32_t>(child->second);
            }
        }
        if (leg.rights) {
            // The expiry closes a right that never printed (or froze) at 0 on the
            // first bar after the expiry day; beyond the panel it can never fire.
            if (has_expiry && expiries.present[row]) {
                if (day_end_ms(expiries.epoch_ms[row]) < ex_dates.epoch_ms[row])
                    throw reject(row, "expires before its ex-date");
                const size_t after = std::upper_bound(panel.timestamps.begin(), panel.timestamps.end(),
                                                      day_end_ms(expiries.epoch_ms[row])) - panel.timestamps.begin();
                leg.expiry_bar = static_cast<int32_t>(after);
            } else {
                ++panel.corporate_action_rights_without_expiry;
            }
            if (has_subscription) leg.subscription_price = subscriptions[row];
            if (has_exercise) leg.exercise_ratio = exercises[row];
        }
        ++panel.corporate_action_legs;
        if (leg.estimate) ++panel.corporate_action_legs_estimated;
        if (leg.rights) ++panel.corporate_action_legs_rights;
        if (stock_row && leg.child < 0 && valued) ++panel.corporate_action_legs_value_only;
        // A leg inside the parent's settlement window (the events file's end date has
        // passed) is booked only while the parent still quotes and is held; once the
        // parent retires, the deal terms in the events file must carry it instead.
        if (!panel.settlement_bar.empty() && panel.settlement_bar[leg.parent] >= 0 &&
            bar >= static_cast<size_t>(panel.settlement_bar[leg.parent]))
            ++panel.corporate_action_legs_after_parent_end;

        // ── Staleness guard ───────────────────────────────────────────────
        // Cash and ratios are stated in this cache's share units. The engine
        // multiplies them into the parent's last observed mark before the
        // ex-date, so that close and the ex-date close must be the ones the
        // exporter saw. A child close is checked when both sides quote; without
        // a panel quote it is only accepted from a tier the ledger may value
        // (when-issued or a flagged estimate), never as a cache close.
        bool checked = false;
        if (ledger.stale_check) {
            // A value-only leg divides its per-share value by the mark like a cash leg.
            const bool evaluable = std::isfinite(leg.cash) || std::isfinite(leg.child_ratio) ||
                                   (valued && leg.child < 0);
            const bool parent_quoted = observed(bar, leg.parent);
            if (std::isfinite(parent_closes[row]) != parent_quoted)
                throw stale(row, parent_quoted ? "has no parent close on the ex-date but the cache quotes one"
                                               : "records a parent close on the ex-date the cache does not have");
            if (parent_quoted) {
                compare("parent close on the ex-date", row, parent_closes[row],
                        close_values[bar * panel.N + leg.parent]);
                checked = true;
            }
            // The mark the engine will divide by: the last observed close before the
            // ex-date. A panel that starts at or after the ledger's previous close
            // cannot check it; inside the panel the anchor is required.
            size_t mark_bar = std::numeric_limits<size_t>::max();
            for (size_t before = bar; before-- > 0;)
                if (observed(before, leg.parent)) { mark_bar = before; break; }
            if (mark_bar != std::numeric_limits<size_t>::max()) {
                if (std::isfinite(parent_before[row])) {
                    compare("parent close before the ex-date", row, parent_before[row],
                            close_values[mark_bar * panel.N + leg.parent]);
                    checked = true;
                } else if (evaluable) {
                    throw std::runtime_error("Corporate actions ledger has evaluation inputs without price anchors: " +
                        row_label(row) + " parent close before the ex-date missing - re-export the ledger for this cache");
                }
            }
            if (leg.child >= 0 && std::isfinite(leg.child_close)) {
                if (observed(bar, static_cast<unsigned>(leg.child))) {
                    if (!leg.ledger_value_usable) {   // a cache close must be this cache's close
                        compare("child close on the ex-date", row, leg.child_close,
                                close_values[bar * panel.N + static_cast<unsigned>(leg.child)]);
                        checked = true;
                    }
                } else if (!leg.ledger_value_usable) {
                    throw stale(row, "records a child close (" + leg.value_source +
                                     ") on the ex-date the cache does not have");
                }
            }
        }
        if (checked) ++panel.corporate_action_price_checks;
        else ++panel.corporate_action_price_checks_not_evaluable;
        panel.corporate_actions.push_back(std::move(leg));
    }
    if (unresolved_children > 0)
        std::cerr << "Corporate actions: " << unresolved_children
                  << " child id(s) are not in the loaded panel; those distributions are unpriced" << std::endl;
    std::stable_sort(panel.corporate_actions.begin(), panel.corporate_actions.end(),
        [](const DataPanel::CorporateActionLeg& a, const DataPanel::CorporateActionLeg& b) {
            return a.bar != b.bar ? a.bar < b.bar : a.parent < b.parent;
        });
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
// Thin-v1 cache (docs/backtest_cache_simplification_plan_2026-09-23.md §6). Every file is
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
// toDate = open) for the universe named in the config.
std::map<std::string, std::vector<uint8_t>> read_interval_universe(const BacktestConfig& config,
    const std::vector<int64_t>& calendar, const std::map<std::string, int>& price_symbols) {
    const std::string path = resolved(config, config.universe.path);
    auto table = read_table(path, {"universe", "tradingItemId", "fromDate", "toDate"});
    auto names = read_strings(table->GetColumnByName("universe"), "Universe name");
    auto ids = read_event_ids(table->GetColumnByName("tradingItemId"), "Universe");
    auto from = read_event_dates(table->GetColumnByName("fromDate"), "universe fromDate");
    auto to = read_event_dates(table->GetColumnByName("toDate"), "universe toDate");
    std::set<std::string> requested(config.universe.symbols.begin(), config.universe.symbols.end());
    std::map<std::string, std::vector<uint8_t>> result;
    bool named = false;
    for (size_t row = 0; row < ids.size(); ++row) {
        if (names[row] != config.universe.name) continue;
        named = true;
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
    if (!named) throw std::runtime_error("Universe '" + config.universe.name + "' has no rows in " + path);
    for (const auto& member : result)
        if (!price_symbols.count(member.first))
            throw std::runtime_error("Universe contains active symbol absent from close: " + member.first);
    return result;
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

DataPanel load_config(const BacktestConfig& config, const std::vector<std::string>& selected_fields) {
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
    if (!config.universe.path.empty()) {
        const bool intervals = interval_membership(resolved(config, config.universe.path));
        if (intervals && config.universe.name.empty())
            throw std::runtime_error("universe.path is an interval membership file: universe.name is required");
        if (!intervals && !config.universe.name.empty())
            throw std::runtime_error("universe.name applies only to an interval membership file");
        membership = intervals ? read_interval_universe(config, panel.timestamps, master->symbols)
                               : read_universe(config, panel.timestamps, master->symbols);
        for (const auto& member : membership) panel.symbols.push_back(member.first);
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
    if (!membership.empty()) {
        panel.universe_mask.resize(cell_count(panel.T, panel.N));
        for (size_t n = 0; n < panel.N; ++n) {
            auto member = membership.find(panel.symbols[n]);
            if (member == membership.end()) continue; // added event destination: never a member
            for (size_t t = 0; t < panel.T; ++t) panel.universe_mask[t * panel.N + n] = member->second[t];
        }
    }
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
        if (!field.second.second) {
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
    // over that stretch, so refuse the start the way a stale events file does.
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
    read_events(config, panel);
    read_corporate_actions(config, panel);
    if (config.base_thin) {
        panel.thin_layout = true;
        const std::filesystem::path dir(thin_dir);
        if (selected_fields.empty() || selected.count("adj_factor"))
            panel.arrays.emplace("adj_factor", read_adj_factor((dir / "adj_factor.parquet").string(),
                                                               panel.timestamps, panel.symbols));
        panel.returns_total = read_long_doubles((dir / "returns_total.parquet").string(), "returns_total",
                                                panel.timestamps, panel.symbols);
        std::unordered_map<std::string, unsigned> index;
        for (unsigned n = 0; n < panel.N; ++n) index.emplace(panel.symbols[n], n);
        for (size_t row = 0; row < thin_events.dates.size(); ++row) {
            // A source that ended before the first bar is retired from bar 0, as the events path does
            // (settlement_bar 0); its return and any move belong to dates the panel does not have.
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
