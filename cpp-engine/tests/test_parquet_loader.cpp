#include <gtest/gtest.h>
#include "data/config_loader.h"
#include "data/data_panel.h"
#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/writer.h>
#include <atomic>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <thread>
#include <unistd.h>

namespace {
constexpr int64_t day = 86400000;
struct DataFiles {
    std::filesystem::path dir;
    DataFiles() {
        static std::atomic<unsigned> sequence{0};
        dir = std::filesystem::temp_directory_path() / ("longshort_parquet_" + std::to_string(getpid()) + "_" + std::to_string(sequence++));
        std::filesystem::create_directories(dir);
    }
    ~DataFiles() { std::error_code error; std::filesystem::remove_all(dir, error); }
    void write(const std::string& name, const std::vector<std::string>& names,
               const std::vector<std::shared_ptr<arrow::Array>>& arrays) {
        std::vector<std::shared_ptr<arrow::Field>> fields;
        for (size_t i = 0; i < names.size(); ++i) fields.push_back(arrow::field(names[i], arrays[i]->type()));
        auto output = arrow::io::FileOutputStream::Open((dir / name).string()).ValueOrDie();
        auto table = arrow::Table::Make(arrow::schema(fields), arrays);
        auto status = parquet::arrow::WriteTable(*table, arrow::default_memory_pool(), output, 2);
        if (!status.ok()) throw std::runtime_error(status.ToString());
        if (!output->Close().ok()) throw std::runtime_error("Cannot close fixture");
    }
};
template<class Builder, class T> std::shared_ptr<arrow::Array> values(const std::vector<T>& input) {
    Builder builder;
    for (auto value : input) if (!builder.Append(value).ok()) throw std::runtime_error("fixture append");
    return builder.Finish().ValueOrDie();
}
std::shared_ptr<arrow::Array> numeric(std::initializer_list<double> input) {
    return values<arrow::DoubleBuilder>(std::vector<double>(input));
}
std::shared_ptr<arrow::Array> dates(std::initializer_list<int64_t> input, arrow::TimeUnit::type unit = arrow::TimeUnit::NANO) {
    arrow::TimestampBuilder builder(arrow::timestamp(unit), arrow::default_memory_pool());
    const int64_t multiplier = unit == arrow::TimeUnit::NANO ? 1000000 : unit == arrow::TimeUnit::MICRO ? 1000 : 1;
    for (auto value : input) {
        const int64_t converted = unit == arrow::TimeUnit::SECOND ? value / 1000 : value * multiplier;
        if (!builder.Append(converted).ok()) throw std::runtime_error("fixture timestamp append");
    }
    return builder.Finish().ValueOrDie();
}
DatasetConfig wide(const std::string& name, const std::map<std::string, std::string>& fields, bool lazy = false,
                   FieldAlignment alignment = FieldAlignment::Exact) {
    DatasetConfig dataset;
    dataset.name = name; dataset.path = "."; dataset.format = DatasetFormat::Wide;
    dataset.time_col = "__index_level_0__"; dataset.fields = fields; dataset.lazy = lazy; dataset.alignment = alignment;
    return dataset;
}
BacktestConfig base(const DataFiles& files) {
    BacktestConfig config;
    config.config_dir = files.dir.string();
    config.datasets.push_back(wide("prices", {{"close", "close.parquet"}}));
    return config;
}
}

TEST(ParquetLoader, AlignsSelectedWideCoordinatesKeepsRawPricesAndWarmupMask) {
    DataFiles files;
    files.write("close.parquet", {"B", "A", "C", "__index_level_0__"},
        {numeric({13,10,12,11,14}), numeric({72.9,100,81,90,60}), numeric({3,3,3,3,3}), dates({3*day,0,2*day,day,4*day})});
    files.write("universe.parquet", {"C", "A", "__index_level_0__", "B"},
        {values<arrow::BooleanBuilder>(std::vector<bool>{0,0,0,0,0}),
         values<arrow::BooleanBuilder>(std::vector<bool>{1,0,0,0,1}), dates({0,day,2*day,3*day,4*day}),
         values<arrow::BooleanBuilder>(std::vector<bool>{0,1,1,0,0})});
    files.write("volume.parquet", {"A", "__index_level_0__", "B"},
        {values<arrow::Int32Builder>(std::vector<int32_t>{50,70,90}), dates({0,2*day,4*day}), numeric({5,7,9})});
    auto config = base(files);
    config.datasets[0].fields["volume"] = "volume.parquet";
    config.start_date = "1970-01-02"; config.end_date = "1970-01-04"; config.warmup_bars = 1;
    config.universe.path = "universe.parquet";
    auto panel = DataPanel::load(config);
    EXPECT_EQ(panel.timestamps, (std::vector<int64_t>{0,day,2*day,3*day}));
    EXPECT_EQ(panel.symbols, (std::vector<std::string>{"A", "B"}));
    EXPECT_EQ(panel.trade_start_index, 1u);
    EXPECT_EQ(panel.universe_mask, (std::vector<uint8_t>{1,0,0,1,0,1,0,0}));
    EXPECT_FLOAT_EQ(panel.at("close", 2, 0), 81); // Membership does not erase holding valuation.
    EXPECT_FLOAT_EQ(panel.at("volume", 0, 0), 50); // int32 is converted, not reinterpreted.
    EXPECT_TRUE(std::isnan(panel.at("volume", 1, 0)));
    EXPECT_FLOAT_EQ(panel.at("volume", 2, 1), 7);
    EXPECT_EQ(panel.loaded_field_count(), 2u);
}
TEST(ParquetLoader, LazyAliasesLoadOnceAndAsOfUsesOnlyKnownRowsIncludingNulls) {
    DataFiles files;
    files.write("close.parquet", {"__index_level_0__", "A", "B"}, {dates({0,day,2*day,3*day}), numeric({100,90,81,70}), numeric({10,11,12,13})});
    arrow::DoubleBuilder nullable;
    ASSERT_TRUE(nullable.Append(3).ok()); ASSERT_TRUE(nullable.AppendNull().ok()); ASSERT_TRUE(nullable.Append(999).ok());
    files.write("monthly.parquet", {"B", "__index_level_0__", "A"}, {nullable.Finish().ValueOrDie(), dates({-day,2*day,5*day}), numeric({10,20,99})});
    auto config = base(files);
    config.datasets.push_back(wide("afl", {{"EPS", "monthly.parquet"}, {"afl_963", "monthly.parquet"}}, true, FieldAlignment::AsOf));
    auto panel = DataPanel::load(config);
    EXPECT_TRUE(panel.has_column("EPS"));
    EXPECT_EQ(panel.lazy_columns.at("EPS"), panel.lazy_columns.at("afl_963"));
    EXPECT_EQ(panel.loaded_field_count(), 1u);
    EXPECT_EQ(panel.loaded_bytes(), 4u * 2u * sizeof(float));
    std::vector<std::thread> readers;
    for (int i = 0; i < 8; ++i) readers.emplace_back([&] { EXPECT_FLOAT_EQ(panel.row("EPS", 1)[0], 10); });
    for (auto& reader : readers) reader.join();
    EXPECT_EQ(panel.loaded_field_count(), 2u);
    EXPECT_EQ(panel.loaded_bytes(), 2u * 4u * 2u * sizeof(float));
    EXPECT_EQ(panel.row("EPS", 0), panel.row("afl_963", 0));
    EXPECT_FLOAT_EQ(panel.at("EPS", 2, 0), 20);
    EXPECT_FLOAT_EQ(panel.at("EPS", 3, 0), 20);
    EXPECT_FLOAT_EQ(panel.at("EPS", 1, 1), 3);
    EXPECT_TRUE(std::isnan(panel.at("EPS", 2, 1)));
    EXPECT_TRUE(std::isnan(panel.at("EPS", 3, 1)));
}
TEST(ParquetLoader, ExplicitFieldAndSymbolProjectionDoesNotReadUnusedBadFiles) {
    DataFiles files;
    files.write("close.parquet", {"__index_level_0__", "B", "A"}, {dates({0,day}), numeric({20,21}), numeric({10,11})});
    auto config = base(files);
    config.datasets[0].fields["unused"] = "does_not_exist.parquet";
    config.universe.symbols = {"A"};
    auto panel = DataPanel::load(config, {"close"});
    EXPECT_EQ(panel.N, 1u);
    EXPECT_EQ(panel.column_names(), (std::vector<std::string>{"close"}));
    EXPECT_FLOAT_EQ(panel.at("close", 1, 0), 11);
    EXPECT_THROW(DataPanel::load(config, {"unknown"}), std::runtime_error);
}
TEST(ParquetLoader, ExactAlignmentNeverBackfillsFutureObservations) {
    DataFiles files;
    files.write("close.parquet", {"A", "B", "__index_level_0__"}, {numeric({1,2,3}), numeric({4,5,6}), dates({0,day,2*day})});
    files.write("factor.parquet", {"A", "__index_level_0__"}, {numeric({42}), dates({day})});
    auto config = base(files);
    config.datasets.push_back(wide("factor", {{"factor", "factor.parquet"}}));
    auto panel = DataPanel::load(config);
    EXPECT_TRUE(std::isnan(panel.at("factor", 0, 0)));
    EXPECT_FLOAT_EQ(panel.at("factor", 1, 0), 42);
    EXPECT_TRUE(std::isnan(panel.at("factor", 2, 0)));
    EXPECT_TRUE(std::isnan(panel.at("factor", 1, 1)));
}
TEST(ParquetLoader, LegacyLongFileSupportsReorderingSelectionAndAdditionalNumericTypes) {
    DataFiles files;
    files.write("long.parquet", {"close", "volume", "symbol", "time"},
        {numeric({20,100,10,90}), values<arrow::UInt16Builder>(std::vector<uint16_t>{2,3,4,5}),
         values<arrow::StringBuilder>(std::vector<std::string>{"B","A","B","A"}), dates({day,0,0,day}, arrow::TimeUnit::SECOND)});
    auto panel = DataPanel::load((files.dir / "long.parquet").string(), {"close","volume"}, "time", "symbol");
    EXPECT_EQ(panel.symbols, (std::vector<std::string>{"A","B"}));
    EXPECT_EQ(panel.timestamps, (std::vector<int64_t>{0,day}));
    EXPECT_FLOAT_EQ(panel.at("close", 0, 0), 100);
    EXPECT_FLOAT_EQ(panel.at("close", 1, 0), 90);
    EXPECT_FLOAT_EQ(panel.at("volume", 1, 1), 2);
}
TEST(ParquetLoader, RejectsDuplicateCoordinatesNullTimesAndUnsupportedValues) {
    DataFiles files;
    files.write("close.parquet", {"A", "__index_level_0__"}, {numeric({1,2}), dates({0,0})});
    EXPECT_THROW(DataPanel::load(base(files)), std::runtime_error);
    arrow::TimestampBuilder nullable(arrow::timestamp(arrow::TimeUnit::MILLI), arrow::default_memory_pool());
    ASSERT_TRUE(nullable.AppendNull().ok());
    files.write("close.parquet", {"A", "__index_level_0__"}, {numeric({1}), nullable.Finish().ValueOrDie()});
    EXPECT_THROW(DataPanel::load(base(files)), std::runtime_error);
    files.write("close.parquet", {"A", "__index_level_0__"}, {values<arrow::StringBuilder>(std::vector<std::string>{"bad"}), dates({0})});
    EXPECT_THROW(DataPanel::load(base(files)), std::runtime_error);
    files.write("long.parquet", {"close", "symbol", "time"}, {numeric({1,2}), values<arrow::StringBuilder>(std::vector<std::string>{"A","A"}), dates({0,0})});
    EXPECT_THROW(DataPanel::load((files.dir/"long.parquet").string(), {"close"}, "time", "symbol"), std::runtime_error);
}
TEST(ParquetLoader, ConvertsTimestampUnitsExactlyAndRejectsSubmillisecondLoss) {
    DataFiles files;
    for (auto unit : {arrow::TimeUnit::SECOND, arrow::TimeUnit::MILLI, arrow::TimeUnit::MICRO, arrow::TimeUnit::NANO}) {
        files.write("close.parquet", {"A", "__index_level_0__"}, {numeric({1,2}), dates({0,day}, unit)});
        EXPECT_EQ(DataPanel::load(base(files)).timestamps, (std::vector<int64_t>{0,day}));
    }
    arrow::TimestampBuilder submillis(arrow::timestamp(arrow::TimeUnit::NANO), arrow::default_memory_pool());
    ASSERT_TRUE(submillis.Append(1).ok());
    files.write("close.parquet", {"A", "__index_level_0__"}, {numeric({1}), submillis.Finish().ValueOrDie()});
    EXPECT_THROW(DataPanel::load(base(files)), std::runtime_error);
}
