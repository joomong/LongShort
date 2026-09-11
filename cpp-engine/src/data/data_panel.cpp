#include "data_panel.h"
#include "parquet_reader.h"
#include <algorithm>
#include <stdexcept>
#include <unordered_set>

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
    if (values.size() != static_cast<size_t>(T) * N)
        throw std::runtime_error("DataPanel column has invalid dimensions: " + col);
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
    return parquet_reader::load_config(config, selected_fields);
}
DataPanel DataPanel::load(const std::string& parquet_path, const std::vector<std::string>& columns,
                          const std::string& time_col, const std::string& symbol_col) {
    return parquet_reader::load_parquet(parquet_path, columns, time_col, symbol_col);
}
