#include <gtest/gtest.h>
#include "data/config_loader.h"
#include "data/data_panel.h"
#include "engine/simulator.h"
#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/writer.h>
#include <atomic>
#include <cmath>
#include <ctime>
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
    for (double bad : {0.0,-1.0,std::numeric_limits<double>::infinity()}) {
        files.write("close.parquet", {"A","__index_level_0__"},
            {numeric({10,bad}),dates({0,day})});
        auto config = base(files); config.end_date = "latest";
        EXPECT_EQ(DataPanel::load(config).T,2u);
    }
}

namespace {
// Synthetic settlement-event fixtures. `present=false` writes a null end date.
struct EndDate { bool present; int64_t epoch_ms; };
std::string iso_day(int64_t epoch_ms) {
    const std::time_t seconds = static_cast<std::time_t>(epoch_ms / 1000);
    std::tm utc{};
    char text[16] = {0};
    gmtime_r(&seconds, &utc);
    std::strftime(text, sizeof(text), "%Y-%m-%d", &utc);
    return text;
}
std::shared_ptr<arrow::Array> end_dates(const std::vector<EndDate>& input, arrow::Type::type type,
                                        arrow::TimeUnit::type unit = arrow::TimeUnit::NANO) {
    const auto check = [](const arrow::Status& status) { if (!status.ok()) throw std::runtime_error(status.ToString()); };
    if (type == arrow::Type::TIMESTAMP) {
        arrow::TimestampBuilder builder(arrow::timestamp(unit), arrow::default_memory_pool());
        for (const auto& value : input) {
            if (!value.present) { check(builder.AppendNull()); continue; }
            const int64_t scaled = unit == arrow::TimeUnit::SECOND ? value.epoch_ms / 1000
                : unit == arrow::TimeUnit::MILLI ? value.epoch_ms
                : unit == arrow::TimeUnit::MICRO ? value.epoch_ms * 1000 : value.epoch_ms * 1000000;
            check(builder.Append(scaled));
        }
        return builder.Finish().ValueOrDie();
    }
    if (type == arrow::Type::DATE32) {
        arrow::Date32Builder builder;
        for (const auto& value : input)
            check(value.present ? builder.Append(static_cast<int32_t>(value.epoch_ms / day)) : builder.AppendNull());
        return builder.Finish().ValueOrDie();
    }
    if (type == arrow::Type::DATE64) {
        arrow::Date64Builder builder;
        for (const auto& value : input) check(value.present ? builder.Append(value.epoch_ms) : builder.AppendNull());
        return builder.Finish().ValueOrDie();
    }
    arrow::StringBuilder builder;
    for (const auto& value : input)
        check(value.present ? builder.Append(iso_day(value.epoch_ms)) : builder.AppendNull());
    return builder.Finish().ValueOrDie();
}
std::shared_ptr<arrow::Array> text_ids(const std::vector<std::string>& input) {
    return values<arrow::StringBuilder>(input);
}
std::shared_ptr<arrow::Array> int_ids(const std::vector<int64_t>& input) {
    return values<arrow::Int64Builder>(input);
}
// Four daily bars and five symbols; every symbol keeps a valid close so events are the only signal.
BacktestConfig four_bar_panel(DataFiles& files) {
    files.write("close.parquet", {"A", "B", "C", "D", "E", "__index_level_0__"},
        {numeric({10,10,10,10}), numeric({20,20,20,20}), numeric({30,30,30,30}),
         numeric({40,40,40,40}), numeric({50,50,50,50}), dates({0,day,2*day,3*day})});
    auto config = base(files);
    config.events.path = "events.parquet";
    return config;
}
}

TEST(ParquetLoader, EventsWithoutConfigurationLeaveTheSettlementVectorsEmpty) {
    DataFiles files;
    auto config = four_bar_panel(files);
    config.events.path.clear();
    const auto panel = DataPanel::load(config);
    EXPECT_TRUE(panel.settlement_bar.empty());
    EXPECT_TRUE(panel.settlement_return.empty());
    // A configured file that does not exist is a load failure, never silently "no events".
    config.events.path = "absent_master.parquet";
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);
}

TEST(ParquetLoader, EventsSettleOnTheBarAfterTheEndDateForEverySupportedDateType) {
    const std::vector<EndDate> ends{
        {true, day},        // A: settles on the next bar
        {false, 0},         // B: null end date means no event
        {true, -day},       // C: ended before the panel starts
        {true, 10 * day},   // D: ends after the last bar
        {true, 3 * day},    // E: ends on the last bar, so settlement falls outside the panel
        {true, 0}           // Z: not part of the price panel
    };
    const std::vector<std::string> ids{"A", "B", "C", "D", "E", "Z"};
    struct Variant { arrow::Type::type type; arrow::TimeUnit::type unit; };
    for (const Variant variant : {Variant{arrow::Type::TIMESTAMP, arrow::TimeUnit::SECOND},
                                  Variant{arrow::Type::TIMESTAMP, arrow::TimeUnit::MILLI},
                                  Variant{arrow::Type::TIMESTAMP, arrow::TimeUnit::MICRO},
                                  Variant{arrow::Type::TIMESTAMP, arrow::TimeUnit::NANO},
                                  Variant{arrow::Type::DATE32, arrow::TimeUnit::NANO},
                                  Variant{arrow::Type::DATE64, arrow::TimeUnit::NANO},
                                  Variant{arrow::Type::STRING, arrow::TimeUnit::NANO}}) {
        DataFiles files;
        auto config = four_bar_panel(files);
        files.write("events.parquet", {"tradingItemId", "securityEndDate"},
                    {text_ids(ids), end_dates(ends, variant.type, variant.unit)});
        const auto panel = DataPanel::load(config);
        ASSERT_EQ(panel.symbols, (std::vector<std::string>{"A","B","C","D","E"}));
        EXPECT_EQ(panel.settlement_bar, (std::vector<int32_t>{2,-1,0,-1,-1})) << variant.type;
        ASSERT_EQ(panel.settlement_return.size(), 5u);
        for (float value : panel.settlement_return) EXPECT_TRUE(std::isnan(value));
    }
}

TEST(ParquetLoader, EventsWithIntradayEndTimesHoldThroughThatCalendarDay) {
    DataFiles files;
    auto config = four_bar_panel(files);
    // An end date carrying a time of day is floored to its calendar day: the
    // security is held through that whole day and settled on the next bar.
    files.write("events.parquet", {"tradingItemId", "securityEndDate"},
                {text_ids({"A", "B"}),
                 end_dates({{true, day + 12 * 3600 * 1000}, {true, 2 * day + 86399999}},
                           arrow::Type::TIMESTAMP, arrow::TimeUnit::MILLI)});
    const auto panel = DataPanel::load(config);
    EXPECT_EQ(panel.settlement_bar, (std::vector<int32_t>{2,3,-1,-1,-1}));
}

TEST(ParquetLoader, EventsMatchInt64IdsAndAlignWithTheSelectedSortedSymbols) {
    DataFiles files;
    files.write("close.parquet", {"10", "2", "33", "__index_level_0__"},
        {numeric({1,1,1,1}), numeric({2,2,2,2}), numeric({3,3,3,3}), dates({0,day,2*day,3*day})});
    auto config = base(files);
    config.events.path = "events.parquet";
    // File order deliberately differs from the lexical panel order.
    files.write("events.parquet", {"tradingItemId", "securityEndDate"},
        {int_ids({2, 10, 33, 99}), end_dates({{true, day}, {false, 0}, {true, 2 * day}, {true, 0}}, arrow::Type::TIMESTAMP)});
    const auto panel = DataPanel::load(config);
    ASSERT_EQ(panel.symbols, (std::vector<std::string>{"10","2","33"}));
    EXPECT_EQ(panel.settlement_bar, (std::vector<int32_t>{-1,2,3}));

    // A universe subset keeps the vectors aligned with the retained symbols.
    config.universe.symbols = {"33", "10"};
    const auto subset = DataPanel::load(config);
    ASSERT_EQ(subset.symbols, (std::vector<std::string>{"10","33"}));
    EXPECT_EQ(subset.settlement_bar, (std::vector<int32_t>{-1,3}));
}

TEST(ParquetLoader, ExplicitSettlementReturnsOverrideTheDefaultAndNullsStayMissing) {
    DataFiles files;
    auto config = four_bar_panel(files);
    config.events.id_col = "securityId";
    config.events.end_date_col = "endDate";
    config.events.return_col = "settlementReturn";
    arrow::DoubleBuilder returns;
    ASSERT_TRUE(returns.Append(-0.25).ok());   // A: explicit loss
    ASSERT_TRUE(returns.AppendNull().ok());    // B
    ASSERT_TRUE(returns.Append(0.5).ok());     // C: kept even though C retires at bar 0
    ASSERT_TRUE(returns.AppendNull().ok());    // D
    ASSERT_TRUE(returns.Append(-1.0).ok());    // E: no in-panel settlement, value still recorded
    ASSERT_TRUE(returns.Append(0.75).ok());    // Z: not in the panel
    files.write("events.parquet", {"securityId", "endDate", "settlementReturn"},
        {text_ids({"A","B","C","D","E","Z"}),
         end_dates({{true, day}, {false, 0}, {true, -day}, {true, 10 * day}, {true, 3 * day}, {true, 0}}, arrow::Type::DATE32),
         returns.Finish().ValueOrDie()});
    const auto panel = DataPanel::load(config);
    EXPECT_EQ(panel.settlement_bar, (std::vector<int32_t>{2,-1,0,-1,-1}));
    ASSERT_EQ(panel.settlement_return.size(), 5u);
    EXPECT_FLOAT_EQ(panel.settlement_return[0], -0.25f);
    EXPECT_TRUE(std::isnan(panel.settlement_return[1]));
    EXPECT_FLOAT_EQ(panel.settlement_return[2], 0.5f);
    EXPECT_TRUE(std::isnan(panel.settlement_return[3]));
    EXPECT_FLOAT_EQ(panel.settlement_return[4], -1.0f);
}

TEST(ParquetLoader, EventsRejectDuplicateIdsMissingColumnsAndUnsupportedCells) {
    DataFiles files;
    auto config = four_bar_panel(files);
    const auto timestamps = end_dates({{true, day}, {true, day}}, arrow::Type::TIMESTAMP);
    files.write("events.parquet", {"tradingItemId", "securityEndDate"}, {text_ids({"A", "A"}), timestamps});
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);          // duplicate inside the panel
    files.write("events.parquet", {"tradingItemId", "securityEndDate"}, {text_ids({"Z", "Z"}), timestamps});
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);          // duplicate outside the panel too
    files.write("events.parquet", {"tradingItemId", "endDate"}, {text_ids({"A", "B"}), timestamps});
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);          // end_date_col is absent
    files.write("events.parquet", {"securityId", "securityEndDate"}, {text_ids({"A", "B"}), timestamps});
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);          // id_col is absent
    files.write("events.parquet", {"tradingItemId", "securityEndDate"}, {numeric({1, 2}), timestamps});
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);          // double IDs are not normalizable
    files.write("events.parquet", {"tradingItemId", "securityEndDate"}, {text_ids({"A", "B"}), numeric({1, 2})});
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);          // numeric end dates are unsupported
    arrow::StringBuilder nullable;
    ASSERT_TRUE(nullable.AppendNull().ok());
    ASSERT_TRUE(nullable.Append("B").ok());
    files.write("events.parquet", {"tradingItemId", "securityEndDate"}, {nullable.Finish().ValueOrDie(), timestamps});
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);          // a null ID cannot be matched
    files.write("events.parquet", {"tradingItemId", "securityEndDate"},
        {text_ids({"A", "B"}), values<arrow::StringBuilder>(std::vector<std::string>{"2026/01/02", "1970-01-02"})});
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);          // string end dates must be YYYY-MM-DD
    config.events.return_col = "settlementReturn";
    files.write("events.parquet", {"tradingItemId", "securityEndDate"}, {text_ids({"A", "B"}), timestamps});
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);          // return_col is absent
}

TEST(ParquetLoader, SuccessorIdsResolveToPanelIndicesAndAcceptEitherIdType) {
    DataFiles files;
    auto config = four_bar_panel(files);
    config.events.successor_col = "successorTradingItemId";
    config.events.successor_ratio_col = "successorRatio";
    config.events.successor_price_col = "successorCacheClose";
    config.events.successor_price_date_col = "successorCacheCloseDate";
    config.events.target_price_col = "targetCacheMark";
    config.events.target_price_date_col = "targetCacheMarkDate";
    const auto timestamps = dates({day, day, day});
    // A -> C and B -> C: several securities may merge into the same survivor.
    arrow::StringBuilder successors;
    ASSERT_TRUE(successors.Append("C").ok());
    ASSERT_TRUE(successors.Append("C").ok());
    ASSERT_TRUE(successors.AppendNull().ok());
    arrow::DoubleBuilder ratios;
    ASSERT_TRUE(ratios.Append(2.5).ok());
    ASSERT_TRUE(ratios.Append(1.0).ok());
    ASSERT_TRUE(ratios.AppendNull().ok());
    files.write("events.parquet",
        {"tradingItemId", "securityEndDate", "successorTradingItemId", "successorRatio",
         "successorCacheClose", "successorCacheCloseDate", "targetCacheMark", "targetCacheMarkDate"},
        {text_ids({"A", "B", "D"}), timestamps, successors.Finish().ValueOrDie(),
         ratios.Finish().ValueOrDie(), numeric({30, 30, 30}), dates({day, day, day}),
         numeric({10, 20, 40}), dates({day, day, day})});
    auto panel = DataPanel::load(config);
    ASSERT_EQ(panel.symbols.size(), 5u);   // A B C D E, lexically sorted
    EXPECT_EQ(panel.settlement_successor[0], 2);
    EXPECT_EQ(panel.settlement_successor[1], 2);
    EXPECT_EQ(panel.settlement_successor[3], -1);  // null successor stays a cash settlement
    EXPECT_EQ(panel.settlement_successor[4], -1);  // no event row at all
    EXPECT_DOUBLE_EQ(panel.settlement_successor_ratio[0], 2.5);
    EXPECT_DOUBLE_EQ(panel.settlement_successor_ratio[1], 1.0);
    EXPECT_TRUE(std::isnan(panel.settlement_successor_ratio[3]));

    // Integer successor ids normalize exactly like integer event ids.
    auto numeric_config = base(files);
    numeric_config.events.path = "events.parquet";
    numeric_config.events.successor_col = "successorTradingItemId";
    files.write("close.parquet", {"11", "22", "33", "__index_level_0__"},
        {numeric({10,10,10,10}), numeric({20,20,20,20}), numeric({30,30,30,30}),
         dates({0,day,2*day,3*day})});
    files.write("events.parquet", {"tradingItemId", "securityEndDate", "successorTradingItemId"},
        {int_ids({11}), dates({day}), int_ids({33})});
    panel = DataPanel::load(numeric_config);
    EXPECT_EQ(panel.settlement_successor[0], 2);
    EXPECT_TRUE(panel.settlement_successor_ratio.empty() ||
                std::isnan(panel.settlement_successor_ratio[0]));
}

TEST(ParquetLoader, SuccessorsOutsideThePanelAndSelfReferencesStayCashSettlements) {
    DataFiles files;
    auto config = four_bar_panel(files);
    config.events.successor_col = "successorTradingItemId";
    files.write("events.parquet", {"tradingItemId", "securityEndDate", "successorTradingItemId"},
        {text_ids({"A", "B"}), dates({day, day}), text_ids({"ZZZ", "B"})});
    const auto panel = DataPanel::load(config);
    EXPECT_EQ(panel.settlement_successor[0], -1);   // not in the loaded panel
    EXPECT_EQ(panel.settlement_successor[1], -1);   // a security cannot succeed itself
    EXPECT_EQ(panel.settlement_bar[0], 2);          // the settlement schedule is unaffected
    EXPECT_EQ(panel.settlement_bar[1], 2);
    // A missing successor column is still a load failure, like every other named column.
    config.events.successor_col = "absentColumn";
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);
    config.events.successor_col = "successorTradingItemId";
    config.events.successor_ratio_col = "absentRatio";
    config.events.successor_price_col = "successorCacheClose";
    config.events.successor_price_date_col = "successorCacheCloseDate";
    config.events.target_price_col = "targetCacheMark";
    config.events.target_price_date_col = "targetCacheMarkDate";
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);
}

TEST(ParquetLoader, CashPerShareLoadsBesideTheSuccessorColumns) {
    DataFiles files;
    auto config = four_bar_panel(files);
    config.events.successor_col = "successorTradingItemId";
    config.events.successor_ratio_col = "successorRatio";
    config.events.cash_col = "cashPerShare";
    config.events.successor_price_col = "successorCacheClose";
    config.events.successor_price_date_col = "successorCacheCloseDate";
    config.events.target_price_col = "targetCacheMark";
    config.events.target_price_date_col = "targetCacheMarkDate";
    arrow::StringBuilder successors;
    ASSERT_TRUE(successors.Append("C").ok());
    ASSERT_TRUE(successors.AppendNull().ok());
    arrow::DoubleBuilder ratios;
    ASSERT_TRUE(ratios.Append(1.5).ok());
    ASSERT_TRUE(ratios.AppendNull().ok());
    arrow::DoubleBuilder cash;
    ASSERT_TRUE(cash.Append(12.25).ok());   // mixed deal
    ASSERT_TRUE(cash.Append(105.0).ok());   // cash only
    files.write("events.parquet",
        {"tradingItemId", "securityEndDate", "successorTradingItemId", "successorRatio",
         "cashPerShare", "successorCacheClose", "successorCacheCloseDate",
         "targetCacheMark", "targetCacheMarkDate"},
        {text_ids({"A", "B"}), dates({day, day}), successors.Finish().ValueOrDie(),
         ratios.Finish().ValueOrDie(), cash.Finish().ValueOrDie(),
         numeric({30, 30}), dates({day, day}), numeric({10, 20}), dates({day, day})});
    const auto panel = DataPanel::load(config);
    EXPECT_DOUBLE_EQ(panel.settlement_cash[0], 12.25);
    EXPECT_DOUBLE_EQ(panel.settlement_cash[1], 105.0);
    EXPECT_TRUE(std::isnan(panel.settlement_cash[2]));   // no event row
    EXPECT_EQ(panel.settlement_successor[0], 2);
    EXPECT_EQ(panel.settlement_successor[1], -1);
    // A named cash column that is absent from the file is a load failure.
    config.events.cash_col = "absentCash";
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);
}

TEST(ParquetLoader, CacheUnitInputsMayNotBeDeclaredWithoutTheirAnchors) {
    DataFiles files;
    auto config = four_bar_panel(files);
    files.write("events.parquet",
        {"tradingItemId", "securityEndDate", "successorTradingItemId", "successorRatio",
         "cashPerShare", "successorCacheClose", "successorCacheCloseDate",
         "targetCacheMark", "targetCacheMarkDate"},
        {text_ids({"A"}), dates({day}), text_ids({"C"}), numeric({2.0}), numeric({5.0}),
         numeric({30.0}), dates({2 * day}), numeric({10.0}), dates({day})});
    config.events.successor_col = "successorTradingItemId";
    // Cache-unit inputs without the anchors that prove the cache are refused.
    config.events.cash_col = "cashPerShare";
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);
    config.events.target_price_col = "targetCacheMark";
    config.events.target_price_date_col = "targetCacheMarkDate";
    EXPECT_NO_THROW(DataPanel::load(config));
    config.events.successor_ratio_col = "successorRatio";
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);   // a ratio needs its own anchor
    config.events.successor_price_col = "successorCacheClose";
    config.events.successor_price_date_col = "successorCacheCloseDate";
    EXPECT_NO_THROW(DataPanel::load(config));
    // A named anchor column the file does not carry is still a load failure.
    config.events.target_price_col = "absentMark";
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);
}

TEST(ParquetLoader, RowsWithEvaluationInputsMustCarryTheirAnchors) {
    const auto write_row = [](DataFiles& files, bool successor_anchor, bool target_anchor, double ratio) {
        arrow::DoubleBuilder successor_price, target_price, ratios;
        arrow::TimestampBuilder successor_date(arrow::timestamp(arrow::TimeUnit::NANO),
                                               arrow::default_memory_pool());
        arrow::TimestampBuilder target_date(arrow::timestamp(arrow::TimeUnit::NANO),
                                            arrow::default_memory_pool());
        if (successor_anchor) {
            EXPECT_TRUE(successor_price.Append(30.0).ok());
            EXPECT_TRUE(successor_date.Append(2 * day * 1000000).ok());
        } else {
            EXPECT_TRUE(successor_price.AppendNull().ok());
            EXPECT_TRUE(successor_date.AppendNull().ok());
        }
        if (target_anchor) {
            EXPECT_TRUE(target_price.Append(10.0).ok());
            EXPECT_TRUE(target_date.Append(day * 1000000).ok());
        } else {
            EXPECT_TRUE(target_price.AppendNull().ok());
            EXPECT_TRUE(target_date.AppendNull().ok());
        }
        if (std::isnan(ratio)) { EXPECT_TRUE(ratios.AppendNull().ok()); }
        else { EXPECT_TRUE(ratios.Append(ratio).ok()); }
        files.write("events.parquet",
            {"tradingItemId", "securityEndDate", "successorTradingItemId", "successorRatio",
             "cashPerShare", "successorCacheClose", "successorCacheCloseDate",
             "targetCacheMark", "targetCacheMarkDate"},
            {text_ids({"A"}), dates({day}), text_ids({"C"}), ratios.Finish().ValueOrDie(),
             numeric({5.0}), successor_price.Finish().ValueOrDie(), successor_date.Finish().ValueOrDie(),
             target_price.Finish().ValueOrDie(), target_date.Finish().ValueOrDie()});
    };
    const auto configure = [](DataFiles& files) {
        auto config = four_bar_panel(files);
        config.events.successor_col = "successorTradingItemId";
        config.events.successor_ratio_col = "successorRatio";
        config.events.cash_col = "cashPerShare";
        config.events.successor_price_col = "successorCacheClose";
        config.events.successor_price_date_col = "successorCacheCloseDate";
        config.events.target_price_col = "targetCacheMark";
        config.events.target_price_date_col = "targetCacheMarkDate";
        return config;
    };
    {   // Cash without a target anchor cannot be verified against this cache.
        DataFiles files;
        auto config = configure(files);
        write_row(files, true, false, 2.0);
        try {
            DataPanel::load(config);
            ADD_FAILURE() << "an unanchored cash amount must be refused";
        } catch (const std::runtime_error& error) {
            EXPECT_NE(std::string(error.what()).find(
                "Events file has evaluation inputs without price anchors"), std::string::npos)
                << error.what();
        }
    }
    {   // A finite cache ratio also needs the successor anchor.
        DataFiles files;
        auto config = configure(files);
        write_row(files, false, true, 2.0);
        EXPECT_THROW(DataPanel::load(config), std::runtime_error);
    }
    {   // Without a cache ratio the successor anchor is not required.
        DataFiles files;
        auto config = configure(files);
        write_row(files, false, true, std::numeric_limits<double>::quiet_NaN());
        const auto panel = DataPanel::load(config);
        EXPECT_EQ(panel.events_price_checks + panel.events_price_checks_out_of_range +
                  panel.events_price_checks_not_evaluable, 1u);
    }
}

// ── Guard v2: every event this panel settles is checked ────────────────────

namespace {
constexpr double kNoValue = std::numeric_limits<double>::quiet_NaN();

// One settling security A and one successor B, mirroring the team review's
// loader reproductions in /tmp/longshort_p2d_loader_review_20260914/cases.py.
struct GuardFixture {
    std::vector<double> target{10, 10, kNoValue, kNoValue};
    std::vector<double> successor{30, 30, 30, 30};
    std::vector<int64_t> stamps{0, day, 2 * day, 3 * day};
    double reference = 30.0;
    int64_t reference_date = 2 * day;     // negative means a null cell
    int64_t end_date = day;
    std::string start = "1970-01-01";
    std::string end = "1970-01-04";
    unsigned warmup = 0;
    std::string successor_id = "B";
    double ratio = 2.0;
    double cash = 12.0;
    double target_mark = 10.0;        // A's last observed close before its settlement
    int64_t target_mark_date = day;
};

std::shared_ptr<arrow::Array> optional_date(int64_t value) {
    arrow::TimestampBuilder builder(arrow::timestamp(arrow::TimeUnit::NANO), arrow::default_memory_pool());
    if (value < 0) { EXPECT_TRUE(builder.AppendNull().ok()); }
    else { EXPECT_TRUE(builder.Append(value * 1000000).ok()); }
    return builder.Finish().ValueOrDie();
}

BacktestConfig guard_config(DataFiles& files, const GuardFixture& fixture) {
    arrow::TimestampBuilder calendar(arrow::timestamp(arrow::TimeUnit::NANO), arrow::default_memory_pool());
    for (int64_t stamp : fixture.stamps) EXPECT_TRUE(calendar.Append(stamp * 1000000).ok());
    files.write("close.parquet", {"A", "B", "__index_level_0__"},
        {values<arrow::DoubleBuilder>(fixture.target), values<arrow::DoubleBuilder>(fixture.successor),
         calendar.Finish().ValueOrDie()});
    files.write("events.parquet",
        {"id", "end", "successor", "ratioCache", "cashCache", "guardPrice", "guardDate",
         "targetMark", "targetMarkDate"},
        {text_ids({"A"}), dates({fixture.end_date}), text_ids({fixture.successor_id}),
         values<arrow::DoubleBuilder>(std::vector<double>{fixture.ratio}),
         values<arrow::DoubleBuilder>(std::vector<double>{fixture.cash}),
         values<arrow::DoubleBuilder>(std::vector<double>{fixture.reference}),
         optional_date(fixture.reference_date),
         values<arrow::DoubleBuilder>(std::vector<double>{fixture.target_mark}),
         optional_date(fixture.target_mark_date)});
    auto config = base(files);
    config.start_date = fixture.start;
    config.end_date = fixture.end;
    config.warmup_bars = fixture.warmup;
    config.events.path = "events.parquet";
    config.events.id_col = "id";
    config.events.end_date_col = "end";
    config.events.successor_col = "successor";
    config.events.successor_ratio_col = "ratioCache";
    config.events.cash_col = "cashCache";
    config.events.successor_price_col = "guardPrice";
    config.events.successor_price_date_col = "guardDate";
    config.events.target_price_col = "targetMark";
    config.events.target_price_date_col = "targetMarkDate";
    return config;
}
}  // namespace

TEST(ParquetLoader, GuardChecksEveryEventThisPanelActuallySettles) {
    {   // A reference on the settlement bar that matches this cache.
        DataFiles files;
        const auto panel = DataPanel::load(guard_config(files, GuardFixture{}));
        EXPECT_EQ(panel.events_price_checks, 1u);
        EXPECT_EQ(panel.events_price_checks_out_of_range, 0u);
        EXPECT_EQ(panel.events_price_checks_not_evaluable, 0u);
    }
    {   // Rounding inside the 1e-4 tolerance still loads.
        DataFiles files;
        GuardFixture fixture;
        fixture.reference = 30.0 * (1.0 + 9e-5);
        EXPECT_NO_THROW(DataPanel::load(guard_config(files, fixture)));
    }
    for (double cached : {kNoValue, 0.0, std::numeric_limits<double>::infinity(), 7.5}) {
        DataFiles files;   // the cache no longer agrees with the recorded anchor
        GuardFixture fixture;
        fixture.successor = {30, 30, cached, 30};
        if (cached == 0.0) continue;   // a zero reference and a zero cache still agree
        EXPECT_THROW(DataPanel::load(guard_config(files, fixture)), std::runtime_error) << cached;
    }
    for (double reference : {kNoValue, std::numeric_limits<double>::infinity()}) {
        DataFiles files;   // no usable anchor, but the ratio it would price is finite
        GuardFixture fixture;
        fixture.reference = reference;
        EXPECT_THROW(DataPanel::load(guard_config(files, fixture)), std::runtime_error);
    }
    {   // A null anchor date is refused for the same reason.
        DataFiles files;
        GuardFixture fixture;
        fixture.reference_date = -1;
        EXPECT_THROW(DataPanel::load(guard_config(files, fixture)), std::runtime_error);
    }
    {   // An anchor taken on any other bar is not the settlement reference.
        DataFiles files;
        GuardFixture fixture;
        fixture.reference = 999.0;
        fixture.reference_date = 32 * day;
        try {
            DataPanel::load(guard_config(files, fixture));
            ADD_FAILURE() << "a reference off the settlement bar must be refused";
        } catch (const std::runtime_error& error) {
            const std::string message = error.what();
            EXPECT_NE(message.find("successor date is not the settlement reference"), std::string::npos)
                << message;
        }
    }
}

// The review's R2: a warmup bar must not decide whether a used event is checked.
TEST(ParquetLoader, GuardVerdictDoesNotDependOnTheLoadedWindow) {
    for (unsigned warmup : {0u, 1u}) {
        DataFiles files;
        GuardFixture fixture;
        fixture.reference = 999.0;
        fixture.reference_date = 0;        // before the trading window
        fixture.start = "1970-01-02";
        fixture.warmup = warmup;
        EXPECT_THROW(DataPanel::load(guard_config(files, fixture)), std::runtime_error)
            << "warmup=" << warmup;
    }
}

TEST(ParquetLoader, GuardSkipsOnlyEventsThisPanelNeverSettles) {
    {   // The end date is past the last bar, so nothing settles here.
        DataFiles files;
        GuardFixture fixture;
        fixture.reference = 999.0;
        fixture.end_date = 3 * day;
        const auto panel = DataPanel::load(guard_config(files, fixture));
        EXPECT_EQ(panel.events_price_checks, 0u);
        EXPECT_EQ(panel.events_price_checks_out_of_range, 1u);
    }
    {   // The all-missing tail is trimmed away before the settlement bar exists.
        DataFiles files;
        GuardFixture fixture;
        fixture.reference = 999.0;
        fixture.target = {10, kNoValue, kNoValue, kNoValue};
        fixture.successor = {30, 30, kNoValue, kNoValue};
        fixture.end = "latest";
        const auto panel = DataPanel::load(guard_config(files, fixture));
        EXPECT_EQ(panel.events_price_checks_out_of_range, 1u);
    }
    {   // The same data with an explicit end keeps the bar, and the check fails.
        DataFiles files;
        GuardFixture fixture;
        fixture.target = {10, kNoValue, kNoValue, kNoValue};
        fixture.successor = {30, 30, kNoValue, kNoValue};
        EXPECT_THROW(DataPanel::load(guard_config(files, fixture)), std::runtime_error);
    }
    for (const char* successor : {"NOT_LOADED", "A"}) {
        DataFiles files;   // no successor to price against: that side is skipped
        GuardFixture fixture;
        fixture.reference = 999.0;
        fixture.successor_id = successor;
        const auto panel = DataPanel::load(guard_config(files, fixture));
        // The target anchor still proves the cache, so the row is checked.
        EXPECT_EQ(panel.events_price_checks, 1u) << successor;
        EXPECT_EQ(panel.events_price_checks_out_of_range, 0u) << successor;
    }
}

TEST(ParquetLoader, GuardPinsIntradayAnchorsToTheExactSettlementBar) {
    const int64_t hour = 3600000;
    GuardFixture fixture;
    fixture.stamps = {9 * hour + 1800000, 16 * hour, day + 9 * hour + 1800000, day + 16 * hour};
    fixture.end_date = 0;
    fixture.end = "1970-01-02";
    fixture.reference_date = day + 16 * hour;   // the close of the day, not the settling bar
    for (double successor_close : {10.0, 30.0}) {
        DataFiles files;   // the stale-accepting and the false-rejecting variants
        fixture.successor = {30, 30, successor_close, 30};
        EXPECT_THROW(DataPanel::load(guard_config(files, fixture)), std::runtime_error)
            << successor_close;
    }
}

// R1 regression on the real event row: a declared stock leg the file could not
// restate in cache units must fall back to the file return, never to cash only.
TEST(ParquetLoader, DeclaredStockLegWithoutACacheRatioUsesTheFileReturn) {
    const double file_return = -0.010883554172237075;
    const double cash_only = 7.9 / 16.66 - 1.0;   // -0.5258, the wrong answer
    for (int mode : {0, 1}) {
        DataFiles files;
        arrow::TimestampBuilder calendar(arrow::timestamp(arrow::TimeUnit::NANO), arrow::default_memory_pool());
        for (int64_t stamp : std::vector<int64_t>{0, day, 2 * day, 3 * day})
            ASSERT_TRUE(calendar.Append(stamp * 1000000).ok());
        // 12678069 (IWA) marks at 16.66 and stops quoting; 28090336 keeps trading.
        files.write("close.parquet", {"12678069", "28090336", "__index_level_0__"},
            {values<arrow::DoubleBuilder>(std::vector<double>{16.66, 16.66, kNoValue, kNoValue}),
             values<arrow::DoubleBuilder>(std::vector<double>{11.0, 11.0, 11.0, 11.0}),
             calendar.Finish().ValueOrDie()});
        arrow::DoubleBuilder ratio_cache;   // the generator could not restate the stock leg
        ASSERT_TRUE(ratio_cache.AppendNull().ok());
        arrow::DoubleBuilder successor_close;    // the real row leaves both cells empty
        ASSERT_TRUE(successor_close.AppendNull().ok());
        arrow::TimestampBuilder successor_date(arrow::timestamp(arrow::TimeUnit::NANO),
                                               arrow::default_memory_pool());
        ASSERT_TRUE(successor_date.AppendNull().ok());
        files.write("events.parquet",
            {"tradingItemId", "endDate", "settlementReturn", "successorTradingItemId",
             "successorRatioCache", "cashPerShareCache", "targetCacheMark", "targetCacheMarkDate",
             "successorCacheClose", "successorCacheCloseDate"},
            {text_ids({"12678069"}), dates({day}),
             values<arrow::DoubleBuilder>(std::vector<double>{file_return}),
             text_ids({"28090336"}), ratio_cache.Finish().ValueOrDie(),
             values<arrow::DoubleBuilder>(std::vector<double>{7.9}),
             values<arrow::DoubleBuilder>(std::vector<double>{16.66}), dates({day}),
             successor_close.Finish().ValueOrDie(), successor_date.Finish().ValueOrDie()});
        auto config = base(files);
        config.events.path = "events.parquet";
        config.events.end_date_col = "endDate";
        config.events.return_col = "settlementReturn";
        config.events.successor_col = "successorTradingItemId";
        config.events.successor_ratio_col = "successorRatioCache";
        config.events.cash_col = "cashPerShareCache";
        config.events.target_price_col = "targetCacheMark";
        config.events.target_price_date_col = "targetCacheMarkDate";
        config.events.successor_price_col = "successorCacheClose";
        config.events.successor_price_date_col = "successorCacheCloseDate";
        const auto panel = DataPanel::load(config);
        EXPECT_EQ(panel.events_price_checks, 1u);          // the target anchor is verified
        EXPECT_EQ(panel.settlement_has_stock_leg[0], 1);   // named successor, no cache ratio
        EXPECT_EQ(panel.settlement_has_cash_leg[0], 1);
        EXPECT_EQ(panel.settlement_successor[0], 1);

        SimulatorConfig simulator{panel.T, panel.N, 0.0, mode, false, {}};
        simulator.missing_close_policy = MissingClosePolicy::Carry;
        const auto out = Simulator(panel, simulator)
            .run_from_signals(std::vector<std::vector<float>>(panel.T, {1, 0}));
        EXPECT_NEAR(out.ret[2], static_cast<double>(static_cast<float>(file_return)), 1e-12)
            << "mode=" << mode;
        EXPECT_GT(std::fabs(out.ret[2] - cash_only), 0.5);
        EXPECT_EQ(out.missing_data[2].computed_settlement_count, 0u);
        EXPECT_EQ(out.missing_data[2].default_delisting_count, 0u);
        EXPECT_EQ(out.missing_data[2].successor_conversions, 0u);
        EXPECT_EQ(out.missing_data[2].delisting_count, 1u);
        EXPECT_NEAR(out.missing_data[2].settled_gross, 1.0, 1e-12);
    }
}

// The three guard buckets must partition the matched rows even when the panel
// ends between two events, which is when the review found them overlapping.
TEST(ParquetLoader, GuardBucketsPartitionTheMatchedRowsAcrossAShortPanel) {
    DataFiles files;
    auto config = base(files);
    // A settles on bar 2, B settles on bar 5, C has a row with no cache inputs,
    // D quotes throughout and never settles, E has no event row at all.
    files.write("close.parquet", {"A", "B", "C", "D", "E", "__index_level_0__"},
        {values<arrow::DoubleBuilder>(std::vector<double>{10, 10, kNoValue, kNoValue, kNoValue, kNoValue}),
         values<arrow::DoubleBuilder>(std::vector<double>{20, 20, 20, 20, 20, kNoValue}),
         values<arrow::DoubleBuilder>(std::vector<double>{30, 30, 30, kNoValue, kNoValue, kNoValue}),
         values<arrow::DoubleBuilder>(std::vector<double>{40, 40, 40, 40, 40, 40}),
         values<arrow::DoubleBuilder>(std::vector<double>{50, 50, 50, 50, 50, 50}),
         dates({0, day, 2 * day, 3 * day, 4 * day, 5 * day})});
    arrow::DoubleBuilder ratios, cash, successor_price, target_price;
    arrow::TimestampBuilder successor_date(arrow::timestamp(arrow::TimeUnit::NANO), arrow::default_memory_pool());
    arrow::TimestampBuilder target_date(arrow::timestamp(arrow::TimeUnit::NANO), arrow::default_memory_pool());
    // A: priced here. B: priced but settles past a short panel. C/D: no inputs.
    ASSERT_TRUE(ratios.AppendNull().ok());        ASSERT_TRUE(cash.Append(5.0).ok());
    ASSERT_TRUE(successor_price.AppendNull().ok()); ASSERT_TRUE(successor_date.AppendNull().ok());
    ASSERT_TRUE(target_price.Append(10.0).ok());  ASSERT_TRUE(target_date.Append(day * 1000000).ok());
    ASSERT_TRUE(ratios.AppendNull().ok());        ASSERT_TRUE(cash.Append(7.0).ok());
    ASSERT_TRUE(successor_price.AppendNull().ok()); ASSERT_TRUE(successor_date.AppendNull().ok());
    ASSERT_TRUE(target_price.Append(20.0).ok());  ASSERT_TRUE(target_date.Append(4 * day * 1000000).ok());
    for (int i = 0; i < 2; ++i) {
        ASSERT_TRUE(ratios.AppendNull().ok());        ASSERT_TRUE(cash.AppendNull().ok());
        ASSERT_TRUE(successor_price.AppendNull().ok()); ASSERT_TRUE(successor_date.AppendNull().ok());
        ASSERT_TRUE(target_price.AppendNull().ok());  ASSERT_TRUE(target_date.AppendNull().ok());
    }
    files.write("events.parquet",
        {"tradingItemId", "securityEndDate", "successorRatio", "cashPerShare",
         "successorCacheClose", "successorCacheCloseDate", "targetCacheMark", "targetCacheMarkDate"},
        {text_ids({"A", "B", "C", "D"}), dates({day, 4 * day, day, day}),
         ratios.Finish().ValueOrDie(), cash.Finish().ValueOrDie(),
         successor_price.Finish().ValueOrDie(), successor_date.Finish().ValueOrDie(),
         target_price.Finish().ValueOrDie(), target_date.Finish().ValueOrDie()});
    config.events.path = "events.parquet";
    config.events.cash_col = "cashPerShare";
    config.events.target_price_col = "targetCacheMark";
    config.events.target_price_date_col = "targetCacheMarkDate";

    const auto whole = DataPanel::load(config);
    EXPECT_EQ(whole.events_rows_matched, 4u);
    EXPECT_EQ(whole.events_price_checks, 2u);            // A and B both settle here
    EXPECT_EQ(whole.events_price_checks_out_of_range, 0u);
    EXPECT_EQ(whole.events_price_checks_not_evaluable, 2u);
    EXPECT_EQ(whole.events_price_checks + whole.events_price_checks_out_of_range +
              whole.events_price_checks_not_evaluable, whole.events_rows_matched);

    // Cut the panel before B settles: exactly one row moves to out of range.
    config.end_date = "1970-01-04";
    const auto shortened = DataPanel::load(config);
    EXPECT_EQ(shortened.events_rows_matched, 4u);
    EXPECT_EQ(shortened.events_price_checks, 1u);
    EXPECT_EQ(shortened.events_price_checks_out_of_range, 1u);
    EXPECT_EQ(shortened.events_price_checks_not_evaluable, 2u);
    EXPECT_EQ(shortened.events_price_checks + shortened.events_price_checks_out_of_range +
              shortened.events_price_checks_not_evaluable, shortened.events_rows_matched);
}

// base: expands one cache directory plus one group directory into the nine standard fields
// (docs/neutralize_plan_2026-09-16.md 4.3). Group panels are wide float codes, NaN = unclassified.
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
// all-NaN row (docs/neutralize_plan_2026-09-16.md 4.3).
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
