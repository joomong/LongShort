#include "costs.h"
#include "../data/config_loader.h"
#include "../data/data_panel.h"
#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/reader.h>
#include <algorithm>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <limits>
#include <map>
#include <sstream>
#include <stdexcept>
#include <unordered_map>

namespace {
constexpr int64_t day_ms = 86400000;
constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

// Last millisecond of the calendar day holding `value` (floor for pre-epoch stamps).
int64_t day_end(int64_t value) {
    int64_t days = value / day_ms;
    if (value % day_ms < 0) --days;
    return days * day_ms + (day_ms - 1);
}

std::string trimmed(const std::string& value) {
    const auto first = value.find_first_not_of(" \t\r\n");
    if (first == std::string::npos) return {};
    return value.substr(first, value.find_last_not_of(" \t\r\n") - first + 1);
}

// One table row before it is mapped onto the panel. `to_ms` is INT64_MAX when open-ended.
struct RateRow {
    std::string symbol;
    int64_t from_ms;
    int64_t to_ms;
    double rate;
};

int64_t timestamp_ms(const std::shared_ptr<arrow::Array>& chunk, int64_t i, const std::string& what) {
    switch (chunk->type_id()) {
        case arrow::Type::TIMESTAMP: {
            const auto unit = std::static_pointer_cast<arrow::TimestampType>(chunk->type())->unit();
            const int64_t value = std::static_pointer_cast<arrow::TimestampArray>(chunk)->Value(i);
            if (unit == arrow::TimeUnit::SECOND) return value * 1000;
            if (unit == arrow::TimeUnit::MILLI) return value;
            if (unit == arrow::TimeUnit::MICRO) return value / 1000;
            return value / 1000000;
        }
        case arrow::Type::DATE32:
            return static_cast<int64_t>(std::static_pointer_cast<arrow::Date32Array>(chunk)->Value(i)) * day_ms;
        case arrow::Type::DATE64:
            return std::static_pointer_cast<arrow::Date64Array>(chunk)->Value(i);
        case arrow::Type::STRING:
            return config_date_ms(trimmed(std::static_pointer_cast<arrow::StringArray>(chunk)->GetString(i)));
        case arrow::Type::LARGE_STRING:
            return config_date_ms(trimmed(std::static_pointer_cast<arrow::LargeStringArray>(chunk)->GetString(i)));
        default:
            throw std::runtime_error("Borrow rate table " + what + " column must be a timestamp, date or "
                                     "YYYY-MM-DD string, not " + chunk->type()->ToString());
    }
}

std::string symbol_text(const std::shared_ptr<arrow::Array>& chunk, int64_t i) {
    switch (chunk->type_id()) {
        case arrow::Type::INT64:
            return std::to_string(std::static_pointer_cast<arrow::Int64Array>(chunk)->Value(i));
        case arrow::Type::INT32:
            return std::to_string(std::static_pointer_cast<arrow::Int32Array>(chunk)->Value(i));
        case arrow::Type::STRING:
            return trimmed(std::static_pointer_cast<arrow::StringArray>(chunk)->GetString(i));
        case arrow::Type::LARGE_STRING:
            return trimmed(std::static_pointer_cast<arrow::LargeStringArray>(chunk)->GetString(i));
        default:
            throw std::runtime_error("Borrow rate table symbol column must be int or string, not " +
                                     chunk->type()->ToString());
    }
}

double rate_value(const std::shared_ptr<arrow::Array>& chunk, int64_t i) {
    switch (chunk->type_id()) {
        case arrow::Type::DOUBLE: return std::static_pointer_cast<arrow::DoubleArray>(chunk)->Value(i);
        case arrow::Type::FLOAT: return std::static_pointer_cast<arrow::FloatArray>(chunk)->Value(i);
        case arrow::Type::INT64: return static_cast<double>(std::static_pointer_cast<arrow::Int64Array>(chunk)->Value(i));
        case arrow::Type::INT32: return static_cast<double>(std::static_pointer_cast<arrow::Int32Array>(chunk)->Value(i));
        default:
            throw std::runtime_error("Borrow rate table rate column must be numeric, not " +
                                     chunk->type()->ToString());
    }
}

std::vector<RateRow> read_parquet_rows(const BorrowCostConfig& config, const std::string& path) {
    auto source = arrow::io::ReadableFile::Open(path);
    if (!source.ok())
        throw std::runtime_error("Cannot open borrow rate table " + path + ": " + source.status().ToString());
    parquet::arrow::FileReaderBuilder builder;
    if (auto status = builder.Open(*source); !status.ok())
        throw std::runtime_error("Cannot read borrow rate table " + path + ": " + status.ToString());
    builder.memory_pool(arrow::default_memory_pool());
    auto built = builder.Build();
    if (!built.ok())
        throw std::runtime_error("Cannot read borrow rate table " + path + ": " + built.status().ToString());
    auto reader = std::move(*built);
    reader->set_use_threads(false);
    std::shared_ptr<arrow::Schema> schema;
    if (auto status = reader->GetSchema(&schema); !status.ok())
        throw std::runtime_error("Cannot read borrow rate table schema: " + status.ToString());
    const auto column_index = [&](const std::string& name) {
        const int index = schema->GetFieldIndex(name);
        if (index < 0) throw std::runtime_error("Borrow rate table column not found: " + name + " in " + path);
        return index;
    };
    std::shared_ptr<arrow::Table> table;
    if (auto status = reader->ReadTable({column_index(config.symbol_col), column_index(config.from_col),
                                         column_index(config.to_col), column_index(config.rate_col)}, &table);
        !status.ok())
        throw std::runtime_error("Cannot read borrow rate table columns: " + status.ToString());
    const auto symbols = table->GetColumnByName(config.symbol_col);
    const auto froms = table->GetColumnByName(config.from_col);
    const auto tos = table->GetColumnByName(config.to_col);
    const auto rates = table->GetColumnByName(config.rate_col);
    std::vector<RateRow> rows;
    rows.reserve(table->num_rows());
    for (int64_t row = 0; row < table->num_rows(); ++row) {
        // Chunked columns: locate the chunk holding this row for each column.
        const auto cell = [&](const std::shared_ptr<arrow::ChunkedArray>& column, int64_t index,
                              std::shared_ptr<arrow::Array>& chunk, int64_t& offset) {
            for (const auto& candidate : column->chunks()) {
                if (index < candidate->length()) { chunk = candidate; offset = index; return; }
                index -= candidate->length();
            }
            throw std::runtime_error("Borrow rate table row is out of range");
        };
        std::shared_ptr<arrow::Array> chunk;
        int64_t offset = 0;
        RateRow entry;
        cell(symbols, row, chunk, offset);
        if (chunk->IsNull(offset)) throw std::runtime_error("Borrow rate table symbol column contains null");
        entry.symbol = symbol_text(chunk, offset);
        cell(froms, row, chunk, offset);
        if (chunk->IsNull(offset)) throw std::runtime_error("Borrow rate table from column contains null");
        entry.from_ms = timestamp_ms(chunk, offset, "from");
        cell(tos, row, chunk, offset);
        entry.to_ms = chunk->IsNull(offset) ? std::numeric_limits<int64_t>::max()
                                            : timestamp_ms(chunk, offset, "to");
        cell(rates, row, chunk, offset);
        if (chunk->IsNull(offset)) throw std::runtime_error("Borrow rate table rate column contains null");
        entry.rate = rate_value(chunk, offset);
        rows.push_back(std::move(entry));
    }
    return rows;
}

// Plain comma-separated text with a header naming the four columns; an empty `to`
// cell is open-ended. Cells are split on every comma: quoted cells and embedded
// commas are not supported (symbols, dates and rates never need them).
std::vector<RateRow> read_csv_rows(const BorrowCostConfig& config, const std::string& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("Cannot open borrow rate table " + path);
    std::string line;
    if (!std::getline(input, line)) throw std::runtime_error("Borrow rate table is empty: " + path);
    const auto split = [](const std::string& text) {
        std::vector<std::string> cells;
        std::stringstream stream(text);
        std::string cell;
        while (std::getline(stream, cell, ',')) cells.push_back(trimmed(cell));
        if (!text.empty() && text.back() == ',') cells.emplace_back();
        return cells;
    };
    const auto header = split(line);
    std::map<std::string, std::size_t> columns;
    for (std::size_t i = 0; i < header.size(); ++i) columns.emplace(header[i], i);
    const auto column_index = [&](const std::string& name) {
        const auto found = columns.find(name);
        if (found == columns.end())
            throw std::runtime_error("Borrow rate table column not found: " + name + " in " + path);
        return found->second;
    };
    const std::size_t symbol_at = column_index(config.symbol_col), from_at = column_index(config.from_col),
                      to_at = column_index(config.to_col), rate_at = column_index(config.rate_col);
    std::vector<RateRow> rows;
    std::size_t number = 1;
    while (std::getline(input, line)) {
        ++number;
        if (trimmed(line).empty()) continue;
        const auto cells = split(line);
        const std::size_t needed = std::max(std::max(symbol_at, from_at), std::max(to_at, rate_at));
        if (cells.size() <= needed)
            throw std::runtime_error("Borrow rate table line " + std::to_string(number) + " has too few cells");
        RateRow entry;
        entry.symbol = cells[symbol_at];
        if (entry.symbol.empty())
            throw std::runtime_error("Borrow rate table line " + std::to_string(number) + " has an empty symbol");
        entry.from_ms = config_date_ms(cells[from_at]);
        entry.to_ms = cells[to_at].empty() ? std::numeric_limits<int64_t>::max() : config_date_ms(cells[to_at]);
        try {
            std::size_t consumed = 0;
            entry.rate = std::stod(cells[rate_at], &consumed);
            if (consumed != cells[rate_at].size()) throw std::invalid_argument("trailing text");
        } catch (const std::exception&) {
            throw std::runtime_error("Borrow rate table line " + std::to_string(number) +
                                     " has a non-numeric rate: " + cells[rate_at]);
        }
        rows.push_back(std::move(entry));
    }
    return rows;
}
}  // namespace

BorrowCostModel BorrowCostModel::load(const CostsConfig& config, const DataPanel& panel,
                                      const std::string& config_dir) {
    const BorrowCostConfig& borrow = config.borrow;
    BorrowCostModel model;
    model.enabled_ = borrow.enabled;
    model.day_count_ = borrow.day_count;
    if (model.day_count_ == 0) throw std::runtime_error("backtest.costs.borrow.day_count must be positive");
    model.has_default_ = std::isfinite(borrow.default_rate);
    model.default_rate_ = model.has_default_ ? borrow.default_rate : 0.0;
    model.timestamps_ = panel.timestamps;
    model.intervals_.assign(panel.N, {});
    // A disabled module never opens its table: nothing depends on it.
    if (!model.enabled_ || borrow.path.empty()) return model;
    const std::string path =
        (std::filesystem::path(config_dir) / borrow.path).lexically_normal().string();
    std::error_code lookup;
    // A configured table must exist: a silently ignored typo would finance every short for free.
    if (!std::filesystem::exists(path, lookup))
        throw std::runtime_error("Borrow rate table not found: " + path);
    model.table_path_ = path;
    const std::string extension = std::filesystem::path(path).extension().string();
    auto rows = extension == ".csv" || extension == ".CSV" ? read_csv_rows(borrow, path)
                                                             : read_parquet_rows(borrow, path);
    model.table_rows_ = rows.size();
    std::unordered_map<std::string, unsigned> panel_index;
    panel_index.reserve(panel.N * 2);
    for (unsigned n = 0; n < panel.N; ++n) panel_index.emplace(panel.symbols[n], n);

    // Every row is validated whether or not its symbol is loaded; overlaps are a
    // table error, not a scheduling question.
    std::map<std::string, std::vector<const RateRow*>> by_symbol;
    for (const auto& row : rows) {
        if (!std::isfinite(row.rate) || row.rate < 0.0)
            throw std::runtime_error("Borrow rate table has a negative or non-finite rate for " + row.symbol);
        if (row.to_ms != std::numeric_limits<int64_t>::max() && day_end(row.to_ms) < row.from_ms)
            throw std::runtime_error("Borrow rate table interval ends before it starts for " + row.symbol);
        by_symbol[row.symbol].push_back(&row);
    }
    for (auto& entry : by_symbol) {
        auto& list = entry.second;
        std::sort(list.begin(), list.end(), [](const RateRow* a, const RateRow* b) {
            return a->from_ms < b->from_ms;
        });
        for (std::size_t i = 1; i < list.size(); ++i) {
            const int64_t previous_end = list[i - 1]->to_ms == std::numeric_limits<int64_t>::max()
                ? std::numeric_limits<int64_t>::max() : day_end(list[i - 1]->to_ms);
            if (list[i]->from_ms <= previous_end)
                throw std::runtime_error("Borrow rate table has overlapping intervals for " + entry.first +
                                         ": later rows must not overlap earlier ones");
        }
        const auto asset = panel_index.find(entry.first);
        if (asset == panel_index.end()) continue;   // symbols outside the panel carry no cost here
        model.table_rows_matched_ += list.size();
        auto& intervals = model.intervals_[asset->second];
        for (const RateRow* row : list) {
            // Calendar days, inclusive on both ends, mapped onto the bars they contain.
            const int64_t first_ms = day_end(row->from_ms) - (day_ms - 1);
            const int64_t last_ms = row->to_ms == std::numeric_limits<int64_t>::max()
                ? std::numeric_limits<int64_t>::max() : day_end(row->to_ms);
            const auto first = std::lower_bound(panel.timestamps.begin(), panel.timestamps.end(), first_ms);
            const auto last = std::upper_bound(panel.timestamps.begin(), panel.timestamps.end(), last_ms);
            if (first >= last) continue;   // no bar of this panel inside the interval
            intervals.push_back(RateInterval{
                static_cast<unsigned>(first - panel.timestamps.begin()),
                static_cast<unsigned>(last - panel.timestamps.begin() - 1), row->rate});
        }
        if (!intervals.empty()) ++model.symbols_rated_;
    }
    return model;
}

double BorrowCostModel::rate(unsigned asset, unsigned bar) const {
    const auto& intervals = intervals_[asset];
    // Sorted and disjoint: the last interval starting at or before `bar` is the only candidate.
    auto it = std::upper_bound(intervals.begin(), intervals.end(), bar,
        [](unsigned value, const RateInterval& interval) { return value < interval.first_bar; });
    if (it != intervals.begin()) {
        --it;
        if (bar <= it->last_bar) return it->rate;
    }
    return has_default_ ? default_rate_ : kNaN;
}

double BorrowCostModel::charge(unsigned di, const std::vector<double>& weights, unsigned& unrated) const {
    if (!enabled_ || di == 0 || di >= timestamps_.size()) return 0.0;
    const double days = static_cast<double>(timestamps_[di] - timestamps_[di - 1]) / static_cast<double>(day_ms);
    double cost = 0.0;
    for (unsigned ii = 0; ii < weights.size(); ++ii) {
        const double weight = weights[ii];
        if (!(weight < 0.0)) continue;   // long and flat names borrow nothing
        const double annual = rate(ii, di);
        if (!std::isfinite(annual)) { ++unrated; continue; }
        cost += -weight * annual / static_cast<double>(day_count_) * days;
    }
    return cost;
}
