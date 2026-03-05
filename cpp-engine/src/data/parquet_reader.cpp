#include "parquet_reader.h"

#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/reader.h>

#include <algorithm>
#include <cmath>
#include <limits>
#include <map>
#include <stdexcept>
#include <unordered_map>

namespace parquet_reader {

static std::string read_symbol_value(const std::shared_ptr<arrow::Array>& chunk, int64_t i)
{
    if (chunk->IsNull(i))
        return "";

    if (chunk->type_id() == arrow::Type::STRING) {
        auto arr = std::static_pointer_cast<arrow::StringArray>(chunk);
        return arr->GetString(i);
    }
    if (chunk->type_id() == arrow::Type::LARGE_STRING) {
        auto arr = std::static_pointer_cast<arrow::LargeStringArray>(chunk);
        return arr->GetString(i);
    }

    throw std::runtime_error("Unsupported symbol column type: " + chunk->type()->ToString());
}

DataPanel load_parquet(const std::string& parquet_path,
                       const std::vector<std::string>& columns,
                       const std::string& time_col,
                       const std::string& symbol_col) {
    // --- 1. Open parquet file ---
    auto fs_result = arrow::io::ReadableFile::Open(parquet_path);
    if (!fs_result.ok())
        throw std::runtime_error("Cannot open parquet file: " + parquet_path +
                                 " : " + fs_result.status().ToString());
    std::shared_ptr<arrow::io::ReadableFile> infile = *fs_result;

    auto reader_result = parquet::arrow::OpenFile(infile, arrow::default_memory_pool());
    if (!reader_result.ok())
        throw std::runtime_error("Cannot open parquet reader: " + reader_result.status().ToString());
    auto reader = std::move(*reader_result);

    // --- 2. Read entire file into arrow::Table ---
    std::shared_ptr<arrow::Table> table;
    auto read_status = reader->ReadTable(&table);
    if (!read_status.ok())
        throw std::runtime_error("Cannot read parquet table: " + read_status.ToString());

    // --- 3. Validate required columns exist in the table schema ---
    auto schema = table->schema();
    auto check_col = [&](const std::string& name) {
        if (schema->GetFieldIndex(name) < 0)
            throw std::runtime_error("Column not found in parquet: " + name);
    };
    check_col(time_col);
    check_col(symbol_col);
    for (const auto& col : columns)
        check_col(col);

    // Validate that open/high/low/close/volume are all present in requested columns
    for (const auto& required : {"open", "high", "low", "close", "volume"}) {
        bool found = false;
        for (const auto& col : columns)
            if (col == required) { found = true; break; }
        if (!found)
            throw std::runtime_error(std::string("Required column missing from columns list: ") + required);
    }

    // --- 4. Extract raw arrays from the table ---
    // time column (int64, epoch_ms)
    int time_idx = schema->GetFieldIndex(time_col);
    auto time_chunked = table->column(time_idx);

    // symbol column (utf8 / string)
    int sym_idx = schema->GetFieldIndex(symbol_col);
    auto sym_chunked = table->column(sym_idx);

    int64_t total_rows = table->num_rows();

    // Collect timestamps and symbols to build sorted unique sets
    // Use map for sorted order
    std::map<int64_t, unsigned> ts_map;    // epoch_ms -> t_idx
    std::map<std::string, unsigned> sym_map; // symbol -> n_idx

    // First pass: collect unique timestamps and symbols
    {
        int64_t row = 0;
        for (int ci = 0; ci < time_chunked->num_chunks(); ++ci) {
            auto arr = std::static_pointer_cast<arrow::Int64Array>(time_chunked->chunk(ci));
            for (int64_t i = 0; i < arr->length(); ++i, ++row) {
                int64_t ts = arr->Value(i);
                ts_map.emplace(ts, 0); // value assigned in second pass
            }
        }
        row = 0;
        for (int ci = 0; ci < sym_chunked->num_chunks(); ++ci) {
            auto chunk = sym_chunked->chunk(ci);
            for (int64_t i = 0; i < chunk->length(); ++i, ++row) {
                std::string sym = read_symbol_value(chunk, i);
                sym_map.emplace(sym, 0);
            }
        }
    }

    // Assign indices (map is already sorted)
    {
        unsigned idx = 0;
        for (auto& kv : ts_map) kv.second = idx++;
    }
    {
        unsigned idx = 0;
        for (auto& kv : sym_map) kv.second = idx++;
    }

    unsigned T = static_cast<unsigned>(ts_map.size());
    unsigned N = static_cast<unsigned>(sym_map.size());

    // Build result timestamps and symbols vectors
    std::vector<int64_t> timestamps(T);
    for (const auto& kv : ts_map) timestamps[kv.second] = kv.first;

    std::vector<std::string> symbols(N);
    for (const auto& kv : sym_map) symbols[kv.second] = kv.first;

    // --- 5. Allocate [T*N] arrays filled with NaN ---
    std::unordered_map<std::string, std::vector<float>> arrays;
    const float nan_val = std::numeric_limits<float>::quiet_NaN();
    for (const auto& col : columns) {
        arrays[col].assign(static_cast<size_t>(T) * N, nan_val);
    }

    // --- 6. Pre-extract column data arrays for each data column ---
    // Store as chunked arrays for row-by-row access
    struct ColChunks {
        std::shared_ptr<arrow::ChunkedArray> chunked;
        bool is_int64; // true=int64, false=float/double
    };
    std::unordered_map<std::string, ColChunks> col_chunks;
    for (const auto& col : columns) {
        int cidx = schema->GetFieldIndex(col);
        auto ca = table->column(cidx);
        auto type_id = ca->type()->id();
        bool is_int64 = (type_id == arrow::Type::INT64);
        col_chunks[col] = {ca, is_int64};
    }

    // Build row-indexed timestamp/symbol indices once, then fill each column directly.
    // This avoids storing per-row symbol strings and all columns flattened at once.
    std::vector<unsigned> row_t_idx(total_rows);
    std::vector<unsigned> row_n_idx(total_rows);

    {
        int64_t row = 0;
        for (int ci = 0; ci < time_chunked->num_chunks(); ++ci) {
            auto arr = std::static_pointer_cast<arrow::Int64Array>(time_chunked->chunk(ci));
            for (int64_t i = 0; i < arr->length(); ++i, ++row) {
                int64_t ts = arr->Value(i);
                auto it = ts_map.find(ts);
                if (it == ts_map.end())
                    throw std::runtime_error("Internal error: timestamp index not found");
                row_t_idx[row] = it->second;
            }
        }
    }
    {
        int64_t row = 0;
        for (int ci = 0; ci < sym_chunked->num_chunks(); ++ci) {
            auto chunk = sym_chunked->chunk(ci);
            for (int64_t i = 0; i < chunk->length(); ++i, ++row) {
                std::string sym = read_symbol_value(chunk, i);
                auto it = sym_map.find(sym);
                if (it == sym_map.end())
                    throw std::runtime_error("Internal error: symbol index not found");
                row_n_idx[row] = it->second;
            }
        }
    }

    // --- 7. Fill result arrays directly per column ---
    for (const auto& col : columns) {
        auto& cc = col_chunks[col];
        auto& dest = arrays[col];
        int64_t row = 0;

        for (int ci = 0; ci < cc.chunked->num_chunks(); ++ci) {
            auto chunk = cc.chunked->chunk(ci);
            if (cc.is_int64) {
                auto arr = std::static_pointer_cast<arrow::Int64Array>(chunk);
                for (int64_t i = 0; i < arr->length(); ++i, ++row) {
                    size_t offset = static_cast<size_t>(row_t_idx[row]) * N + row_n_idx[row];
                    dest[offset] = arr->IsNull(i) ? nan_val : static_cast<float>(arr->Value(i));
                }
            } else {
                if (chunk->type_id() == arrow::Type::DOUBLE) {
                    auto arr = std::static_pointer_cast<arrow::DoubleArray>(chunk);
                    for (int64_t i = 0; i < arr->length(); ++i, ++row) {
                        size_t offset = static_cast<size_t>(row_t_idx[row]) * N + row_n_idx[row];
                        dest[offset] = arr->IsNull(i) ? nan_val : static_cast<float>(arr->Value(i));
                    }
                } else {
                    auto arr = std::static_pointer_cast<arrow::FloatArray>(chunk);
                    for (int64_t i = 0; i < arr->length(); ++i, ++row) {
                        size_t offset = static_cast<size_t>(row_t_idx[row]) * N + row_n_idx[row];
                        dest[offset] = arr->IsNull(i) ? nan_val : arr->Value(i);
                    }
                }
            }
        }
    }

    // --- 8. Assemble DataPanel ---
    DataPanel panel;
    panel.T = T;
    panel.N = N;
    panel.timestamps = std::move(timestamps);
    panel.symbols = std::move(symbols);
    panel.arrays = std::move(arrays);
    return panel;
}

} // namespace parquet_reader
