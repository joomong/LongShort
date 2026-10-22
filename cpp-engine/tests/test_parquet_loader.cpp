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
#include <limits>
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

TEST(ParquetLoader, Ohlc4RequiresExactOhlEvenWithRestrictedLazySelection) {
    DataFiles files;
    files.write("close.parquet", {"A", "__index_level_0__"},
                {numeric({100, 101, 102}), dates({0, day, 2*day})});
    files.write("ohl.parquet", {"A", "__index_level_0__"},
                {numeric({100, 102}), dates({0, 2*day})});
    auto config = base(files);
    config.datasets.push_back(wide("ohl", {{"open", "ohl.parquet"}, {"high", "ohl.parquet"},
                                           {"low", "ohl.parquet"}}, true));
    const auto close_only = DataPanel::load(config, {"close"});
    EXPECT_FALSE(close_only.has_column("open"));

    config.execution_price = "ohlc4";
    const auto panel = DataPanel::load(config, {"close"});
    EXPECT_EQ(panel.loaded_field_count(), 4u);
    for (const char* field : {"open", "high", "low"}) {
        EXPECT_FLOAT_EQ(panel.at(field, 0, 0), 100);
        EXPECT_TRUE(std::isnan(panel.at(field, 1, 0))); // Never forward-fill a fill price.
        EXPECT_FLOAT_EQ(panel.at(field, 2, 0), 102);
        EXPECT_EQ(panel.lazy_columns.count(field), 0u);
    }
    config.datasets.back().alignment = FieldAlignment::AsOf;
    EXPECT_THROW(DataPanel::load(config, {"close"}), std::runtime_error);
    config.datasets.back().alignment = FieldAlignment::Exact;
    config.datasets.back().fields.erase("high");
    EXPECT_THROW(DataPanel::load(config, {"close"}), std::runtime_error);
}
TEST(ParquetLoader, VwapRequiresExactVwapHighLowEvenWithRestrictedLazySelection) {
    DataFiles files;
    files.write("close.parquet", {"A", "__index_level_0__"},
                {numeric({100, 101, 102}), dates({0, day, 2*day})});
    files.write("vhl.parquet", {"A", "__index_level_0__"},
                {numeric({100, 102}), dates({0, 2*day})});
    auto config = base(files);
    config.datasets.push_back(wide("vhl", {{"vwap", "vhl.parquet"}, {"high", "vhl.parquet"},
                                           {"low", "vhl.parquet"}}, true));
    config.execution_price = "vwap";
    const auto panel = DataPanel::load(config, {"close"});
    EXPECT_EQ(panel.loaded_field_count(), 4u);
    for (const char* field : {"vwap", "high", "low"}) {
        EXPECT_FLOAT_EQ(panel.at(field, 0, 0), 100);
        EXPECT_TRUE(std::isnan(panel.at(field, 1, 0)));
        EXPECT_EQ(panel.lazy_columns.count(field), 0u);
    }
    config.datasets.back().alignment = FieldAlignment::AsOf;
    EXPECT_THROW(DataPanel::load(config, {"close"}), std::runtime_error);
    config.datasets.back().alignment = FieldAlignment::Exact;
    config.datasets.back().fields.erase("vwap");
    EXPECT_THROW(DataPanel::load(config, {"close"}), std::runtime_error);
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

TEST(ParquetLoader, LatestTrimsOnlyMissingTailAndAlignsEagerLazyAndUniverseData) {
    DataFiles files;
    const double nan = std::numeric_limits<double>::quiet_NaN();
    files.write("close.parquet", {"A", "B", "__index_level_0__"},
        {numeric({10,nan,11,nan}), numeric({20,nan,nan,nan}), dates({0,day,2*day,3*day})});
    files.write("factor.parquet", {"A", "B", "__index_level_0__"},
        {numeric({1,2,3,4}), numeric({5,6,7,8}), dates({0,day,2*day,3*day})});
    files.write("universe.parquet", {"A", "B", "__index_level_0__"},
        {numeric({1,1,1,1}), numeric({1,0,1,1}), dates({0,day,2*day,3*day})});
    auto config = base(files);
    config.start_date = "1970-01-02"; config.end_date = "latest"; config.warmup_bars = 1;
    config.universe.path = "universe.parquet";
    config.datasets[0].fields["eager_factor"] = "factor.parquet";
    config.datasets.push_back(wide("afl", {{"lazy_factor", "factor.parquet"}}, true));
    auto panel = DataPanel::load(config);
    EXPECT_EQ(panel.timestamps, (std::vector<int64_t>{0,day,2*day}));
    EXPECT_EQ(panel.T, 3u);
    EXPECT_EQ(panel.trade_start_index, 1u);
    EXPECT_EQ(panel.universe_mask, (std::vector<uint8_t>{1,1,1,0,1,1}));
    EXPECT_TRUE(std::isnan(panel.at("close",1,0))); // An interior market-wide gap remains.
    EXPECT_TRUE(std::isnan(panel.at("close",1,1)));
    EXPECT_TRUE(std::isnan(panel.at("close",2,1))); // A partially missing last row remains.
    EXPECT_FLOAT_EQ(panel.at("eager_factor",2,0),3);
    EXPECT_FLOAT_EQ(panel.at("lazy_factor",2,1),7); // Lazy coordinates use the final T.
    EXPECT_EQ(panel.loaded_bytes(),3u*2u*3u*sizeof(float));

    config.end_date = "1970-01-04";
    auto fixed = DataPanel::load(config);
    EXPECT_EQ(fixed.T,4u);
    EXPECT_EQ(fixed.timestamps.back(),3*day);
    EXPECT_TRUE(std::isnan(fixed.at("close",3,0)));
    EXPECT_FLOAT_EQ(fixed.at("lazy_factor",3,0),4);
}

TEST(ParquetLoader, LatestReloadSeesAppendedPricesWhileExistingPanelStaysUnchanged) {
    DataFiles files;
    const double nan = std::numeric_limits<double>::quiet_NaN();
    files.write("close.parquet", {"A","__index_level_0__"},
        {numeric({10,11,nan}),dates({0,day,2*day})});
    auto config = base(files); config.end_date = "latest";
    const auto first = DataPanel::load(config);
    ASSERT_EQ(first.T,2u);
    files.write("close.parquet", {"A","__index_level_0__"},
        {numeric({10,11,12,13,nan}),dates({0,day,2*day,3*day,4*day})});
    const auto restarted = DataPanel::load(config);
    EXPECT_EQ(first.T,2u);
    EXPECT_FLOAT_EQ(first.at("close",1,0),11);
    EXPECT_EQ(restarted.T,4u);
    EXPECT_EQ(restarted.timestamps.back(),3*day);
    EXPECT_FLOAT_EQ(restarted.at("close",3,0),13);
}

TEST(ParquetLoader, LatestUsesSelectedSymbolsAndRejectsACompletelyMissingTradingPeriod) {
    DataFiles files;
    const double nan = std::numeric_limits<double>::quiet_NaN();
    files.write("close.parquet", {"A","B","__index_level_0__"},
        {numeric({10,nan,nan}),numeric({20,21,22}),dates({0,day,2*day})});
    auto config = base(files); config.end_date = "latest";
    config.universe.symbols = {"A"};
    EXPECT_EQ(DataPanel::load(config).timestamps,(std::vector<int64_t>{0}));
    config.start_date = "1970-01-02"; config.warmup_bars = 1;
    EXPECT_THROW(DataPanel::load(config),std::runtime_error); // Warmup alone is not a trading span.
    files.write("close.parquet", {"A","__index_level_0__"},
        {numeric({nan,nan}),dates({0,day})});
    config.start_date.clear();
    EXPECT_THROW(DataPanel::load(config),std::runtime_error);
}

TEST(ParquetLoader, LatestDoesNotHideInvalidNonmissingPrices) {
    DataFiles files;
    for (double bad : {0.0,-1.0}) {
        files.write("close.parquet", {"A","__index_level_0__"},
            {numeric({10,bad}),dates({0,day})});
        auto config = base(files); config.end_date = "latest";
        EXPECT_EQ(DataPanel::load(config).T,2u);
    }
    // inf is never a value: it loads as missing, so latest trims it like a missing tail.
    files.write("close.parquet", {"A","__index_level_0__"},
        {numeric({10,std::numeric_limits<double>::infinity()}),dates({0,day})});
    auto config = base(files); config.end_date = "latest";
    EXPECT_EQ(DataPanel::load(config).T,1u);
}

// base: expands one cache directory plus one group directory into the nine standard fields
// (docs/data_contract.md 4.3). Group panels are wide float codes, NaN = unclassified.
TEST(ParquetLoader, BaseBlockLoadsPricesAndRealignsShuffledGroupPanels) {
    DataFiles files;
    std::filesystem::create_directories(files.dir / "BaseData");
    std::filesystem::create_directories(files.dir / "groups");
    for (const std::string& name : {"open", "high", "low", "close", "volume", "VWAP", "marketCap_USD"})
        files.write("BaseData/" + name + ".parquet", {"__index_level_0__", "A", "B", "C"},
            {dates({0, day, 2 * day}), numeric({10, 11, 12}), numeric({20, 21, 22}), numeric({30, 31, 32})});
    // Stored in a different symbol order than the panel; C is unclassified and A changes code.
    files.write("groups/sector.parquet", {"C", "B", "__index_level_0__", "A"},
        {numeric({std::nan(""), std::nan(""), std::nan("")}), numeric({11, 11, 11}),
         dates({0, day, 2 * day}), numeric({2, 2, 7})});
    files.write("groups/industry.parquet", {"B", "A", "C", "__index_level_0__"},
        {numeric({74, 74, 74}), numeric({3, 3, 3}),
         numeric({std::nan(""), std::nan(""), std::nan("")}), dates({0, day, 2 * day})});
    std::ofstream(files.dir / "config.yaml") << "base: {cache: ., groups: groups}\nbacktest: {}\n";
    const auto config = BacktestConfig::from_yaml((files.dir / "config.yaml").string());
    const auto panel = DataPanel::load(config);
    ASSERT_EQ(panel.symbols, (std::vector<std::string>{"A", "B", "C"}));
    for (const std::string& field : {"open", "high", "low", "close", "volume", "vwap", "cap", "sector", "industry"})
        EXPECT_TRUE(panel.has_column(field)) << field;
    EXPECT_EQ(panel.loaded_field_count(), 9u); // every base field is eager
    EXPECT_TRUE(panel.lazy_columns.empty());
    EXPECT_FLOAT_EQ(panel.at("vwap", 0, 0), 10);
    EXPECT_FLOAT_EQ(panel.at("cap", 2, 2), 32);
    const float* sector = panel.row("sector", 0);
    EXPECT_FLOAT_EQ(sector[0], 2);   // A, read back in panel order, not file order
    EXPECT_FLOAT_EQ(sector[1], 11);  // B
    EXPECT_TRUE(std::isnan(sector[2])); // C stays unclassified
    EXPECT_FLOAT_EQ(panel.row("sector", 2)[0], 7); // the code on that bar, not the first row's
    EXPECT_FLOAT_EQ(panel.at("industry", 1, 0), 3);
    EXPECT_FLOAT_EQ(panel.at("industry", 1, 1), 74);
    EXPECT_TRUE(std::isnan(panel.at("industry", 1, 2)));
}

TEST(ParquetLoader, MissingBaseGroupPanelFailsAtLoadWithTheFilePath) {
    DataFiles files;
    std::filesystem::create_directories(files.dir / "BaseData");
    std::filesystem::create_directories(files.dir / "groups");
    for (const std::string& name : {"open", "high", "low", "close", "volume", "VWAP", "marketCap_USD"})
        files.write("BaseData/" + name + ".parquet", {"__index_level_0__", "A", "B"},
            {dates({0, day}), numeric({10, 11}), numeric({20, 21})});
    files.write("groups/sector.parquet", {"__index_level_0__", "A", "B"},
        {dates({0, day}), numeric({1, 1}), numeric({2, 2})}); // industry.parquet is absent
    std::ofstream(files.dir / "config.yaml") << "base: {cache: ., groups: groups}\nbacktest: {}\n";
    const auto config = BacktestConfig::from_yaml((files.dir / "config.yaml").string());
    try {
        DataPanel::load(config);
        ADD_FAILURE() << "expected the missing group panel to fail the load";
    } catch (const std::exception& error) {
        const std::string message = error.what();
        EXPECT_NE(message.find((files.dir / "groups" / "industry.parquet").string()), std::string::npos) << message;
    }
}

TEST(ParquetLoader, BaseFieldRedeclaredInDatasetsIsADuplicatePublicField) {
    DataFiles files;
    std::filesystem::create_directories(files.dir / "BaseData");
    std::filesystem::create_directories(files.dir / "groups");
    for (const std::string& name : {"open", "high", "low", "close", "volume", "VWAP", "marketCap_USD"})
        files.write("BaseData/" + name + ".parquet", {"__index_level_0__", "A", "B"},
            {dates({0, day}), numeric({10, 11}), numeric({20, 21})});
    for (const std::string& name : {"sector", "industry"})
        files.write("groups/" + name + ".parquet", {"__index_level_0__", "A", "B"},
            {dates({0, day}), numeric({1, 1}), numeric({2, 2})});
    std::ofstream(files.dir / "config.yaml")
        << "base: {cache: ., groups: groups}\n"
        << "datasets: [{name: extra, format: wide, path: BaseData, fields: {close: close.parquet}}]\n"
        << "backtest: {}\n";
    const auto config = BacktestConfig::from_yaml((files.dir / "config.yaml").string());
    ASSERT_EQ(config.datasets.size(), 3u);
    try {
        DataPanel::load(config);
        ADD_FAILURE() << "expected a duplicate public data field error";
    } catch (const std::exception& error) {
        const std::string message = error.what();
        EXPECT_NE(message.find("Duplicate public data field: close"), std::string::npos) << message;
    }
}

// Group codes use as-of alignment: when the price cache grows after the panels were
// built, the last known classification is carried forward instead of turning into an
// all-NaN row (docs/data_contract.md 4.3).
TEST(ParquetLoader, GroupPanelCarriesTheLastClassificationForwardWhenTheCacheGrows) {
    DataFiles files;
    std::filesystem::create_directories(files.dir / "BaseData");
    std::filesystem::create_directories(files.dir / "groups");
    for (const std::string& name : {"open", "high", "low", "close", "volume", "VWAP", "marketCap_USD"})
        files.write("BaseData/" + name + ".parquet", {"__index_level_0__", "A", "B"},
            {dates({0, day, 2 * day}), numeric({10, 11, 12}), numeric({20, 21, 22})});
    files.write("groups/industry.parquet", {"__index_level_0__", "A", "B"},
        {dates({0, day, 2 * day}), numeric({3, 3, 3}), numeric({4, 4, 4})});
    // The cache grew by one bar after the group panels were built.
    files.write("groups/sector.parquet", {"__index_level_0__", "A", "B"},
        {dates({0, day}), numeric({1, 1}), numeric({2, 2})});
    std::ofstream(files.dir / "config.yaml") << "base: {cache: ., groups: groups}\nbacktest: {}\n";
    const auto config = BacktestConfig::from_yaml((files.dir / "config.yaml").string());
    const DataPanel panel = DataPanel::load(config);
    ASSERT_EQ(panel.T, 3u);
    ASSERT_EQ(panel.N, 2u);
    const float* last = panel.row("sector", 2);
    EXPECT_FLOAT_EQ(last[0], 1.0f);
    EXPECT_FLOAT_EQ(last[1], 2.0f);
    EXPECT_FLOAT_EQ(panel.row("industry", 2)[1], 4.0f);
}

// As-of never fills backwards: a group panel that starts after the first price bar leaves
// the earlier bars without codes, and the loader refuses to start rather than let
// `neutralize` empty the book over that stretch.
TEST(ParquetLoader, GroupPanelStartingAfterTheFirstPriceBarFailsTheLoad) {
    DataFiles files;
    std::filesystem::create_directories(files.dir / "BaseData");
    std::filesystem::create_directories(files.dir / "groups");
    for (const std::string& name : {"open", "high", "low", "close", "volume", "VWAP", "marketCap_USD"})
        files.write("BaseData/" + name + ".parquet", {"__index_level_0__", "A", "B"},
            {dates({0, day, 2 * day}), numeric({10, 11, 12}), numeric({20, 21, 22})});
    files.write("groups/industry.parquet", {"__index_level_0__", "A", "B"},
        {dates({0, day, 2 * day}), numeric({3, 3, 3}), numeric({4, 4, 4})});
    files.write("groups/sector.parquet", {"__index_level_0__", "A", "B"},
        {dates({day, 2 * day}), numeric({1, 1}), numeric({2, 2})});
    std::ofstream(files.dir / "config.yaml") << "base: {cache: ., groups: groups}\nbacktest: {}\n";
    const auto config = BacktestConfig::from_yaml((files.dir / "config.yaml").string());
    try {
        DataPanel::load(config);
        ADD_FAILURE() << "expected the late-starting group panel to fail the load";
    } catch (const std::exception& error) {
        const std::string message = error.what();
        EXPECT_NE(message.find("Group panel 'sector' has no codes on 1970-01-01"), std::string::npos) << message;
        EXPECT_NE(message.find("build_group_panels.py"), std::string::npos) << message;
    }
}

TEST(ParquetLoader, GroupPanelBuiltForAnotherSymbolSetFailsTheLoad) {
    DataFiles files;
    std::filesystem::create_directories(files.dir / "BaseData");
    std::filesystem::create_directories(files.dir / "groups");
    for (const std::string& name : {"open", "high", "low", "close", "volume", "VWAP", "marketCap_USD"})
        files.write("BaseData/" + name + ".parquet", {"__index_level_0__", "A", "B"},
            {dates({0, day}), numeric({10, 11}), numeric({20, 21})});
    files.write("groups/sector.parquet", {"__index_level_0__", "A", "B"},
        {dates({0, day}), numeric({1, 1}), numeric({2, 2})});
    // Same calendar, another cache's symbols: every requested column is absent.
    files.write("groups/industry.parquet", {"__index_level_0__", "X", "Y"},
        {dates({0, day}), numeric({3, 3}), numeric({4, 4})});
    std::ofstream(files.dir / "config.yaml") << "base: {cache: ., groups: groups}\nbacktest: {}\n";
    const auto config = BacktestConfig::from_yaml((files.dir / "config.yaml").string());
    try {
        DataPanel::load(config);
        ADD_FAILURE() << "expected the mismatched group panel to fail the load";
    } catch (const std::exception& error) {
        const std::string message = error.what();
        EXPECT_NE(message.find("Group panel 'industry' has no codes on 1970-01-01"), std::string::npos) << message;
        EXPECT_NE(message.find("was built for another cache"), std::string::npos) << message;
    }
}

TEST(ParquetLoader, InfinityLoadsAsMissingLikeANullCell) {
    DataFiles files;
    const double inf = std::numeric_limits<double>::infinity();
    arrow::DoubleBuilder doubles;
    ASSERT_TRUE(doubles.Append(inf).ok()); ASSERT_TRUE(doubles.Append(-inf).ok());
    ASSERT_TRUE(doubles.AppendNull().ok()); ASSERT_TRUE(doubles.Append(1.5).ok());
    arrow::FloatBuilder floats;
    ASSERT_TRUE(floats.Append(-std::numeric_limits<float>::infinity()).ok()); ASSERT_TRUE(floats.Append(2.0f).ok());
    ASSERT_TRUE(floats.Append(std::numeric_limits<float>::infinity()).ok()); ASSERT_TRUE(floats.AppendNull().ok());
    files.write("long.parquet", {"close", "y", "symbol", "time"},
        {doubles.Finish().ValueOrDie(), floats.Finish().ValueOrDie(),
         values<arrow::StringBuilder>(std::vector<std::string>{"A","A","A","A"}), dates({0,day,2*day,3*day})});
    auto panel = DataPanel::load((files.dir / "long.parquet").string(), {"close","y"}, "time", "symbol");
    EXPECT_TRUE(std::isnan(panel.at("close", 0, 0)));
    EXPECT_TRUE(std::isnan(panel.at("close", 1, 0)));
    EXPECT_TRUE(std::isnan(panel.at("close", 2, 0)));
    EXPECT_FLOAT_EQ(panel.at("close", 3, 0), 1.5f);
    EXPECT_TRUE(std::isnan(panel.at("y", 0, 0)));
    EXPECT_FLOAT_EQ(panel.at("y", 1, 0), 2.0f);
    EXPECT_TRUE(std::isnan(panel.at("y", 2, 0)));
    EXPECT_TRUE(std::isnan(panel.at("y", 3, 0)));
}
