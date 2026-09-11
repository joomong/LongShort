#include <gtest/gtest.h>
#include "data/config_loader.h"
#include <atomic>
#include <filesystem>
#include <fstream>
#include <unistd.h>

namespace {
struct ConfigFiles {
    std::filesystem::path dir;
    ConfigFiles() {
        static std::atomic<unsigned> sequence{0};
        dir = std::filesystem::temp_directory_path() / ("longshort_config_" + std::to_string(getpid()) + "_" + std::to_string(sequence++));
        std::filesystem::create_directories(dir);
    }
    ~ConfigFiles() { std::error_code error; std::filesystem::remove_all(dir, error); }
    std::string file(const std::string& name, const std::string& body) {
        std::ofstream(dir / name) << body;
        return (dir / name).string();
    }
};
const std::string legacy = R"(datasets:
  - name: prices
    path: data.parquet
    time_col: time
    symbol_col: symbol
    use_col: [open, high, low, close, volume]
universe: [all]
backtest:
  start_date: '2020-01-01'
  end_date: '2020-12-31'
  initial_capital: 1000000
  transaction_fee: 1.5
)";
}
TEST(ConfigLoader, LegacyConfigKeepsColumnOrderAndUnits) {
    ConfigFiles files;
    const auto config = BacktestConfig::from_yaml(files.file("config.yaml", legacy));
    EXPECT_EQ(config.use_columns, (std::vector<std::string>{"open", "high", "low", "close", "volume"}));
    ASSERT_EQ(config.datasets.size(), 1u);
    EXPECT_EQ(config.datasets[0].format, DatasetFormat::Long);
    EXPECT_FALSE(config.datasets[0].lazy);
    EXPECT_EQ(config.datasets[0].fields.at("close"), "close");
    EXPECT_NEAR(config.transaction_fee_rate, 0.00015, 1e-15);
    EXPECT_EQ(config.bars_per_year, 365u);
    EXPECT_EQ(config.mode, 1);
    EXPECT_EQ(config.warmup_bars, 0u);
    EXPECT_EQ(config.combined_mode, "normalized");
    EXPECT_TRUE(config.universe.symbols.empty());
}
TEST(ConfigLoader, WideCatalogRegistersAliasesLazilyAndSkipsDisabledDatasets) {
    ConfigFiles files;
    files.file("catalog.json", R"({"fields":{"EPS":"963.parquet","afl_963":"963.parquet"}})");
    auto config = BacktestConfig::from_yaml(files.file("config.yaml", R"(datasets:
  - {name: prices, format: wide, path: BaseData, fields: {close: close.parquet}}
  - {name: afl, format: wide, path: AflData_M, catalog: catalog.json, alignment: asof}
  - {name: inactive, enabled: false}
universe: {path: Top500.parquet, symbols: ['10', '2']}
backtest: {start_date: '2020-01-01', end_date: '2020-02-01', warmup_bars: 21, bars_per_year: 252, combined_mode: raw}
)"));
    ASSERT_EQ(config.datasets.size(), 3u);
    EXPECT_TRUE(config.datasets[1].lazy);
    EXPECT_EQ(config.datasets[1].alignment, FieldAlignment::AsOf);
    EXPECT_EQ(config.datasets[1].time_col, "__index_level_0__");
    EXPECT_EQ(config.datasets[1].fields.at("EPS"), config.datasets[1].fields.at("afl_963"));
    EXPECT_EQ(config.use_columns.size(), 3u);
    EXPECT_EQ(config.warmup_bars, 21u);
    EXPECT_EQ(config.bars_per_year, 252u);
    EXPECT_EQ(config.combined_mode, "raw");
}
TEST(ConfigLoader, RejectsAmbiguousFieldsAndInvalidSettings) {
    ConfigFiles files;
    files.file("catalog.json", R"({"fields":{"close":"first.parquet"}})");
    const std::vector<std::string> invalid = {
        "datasets: [{path: p, use_col: [close]}, {path: q, use_col: [close]}]\nbacktest: {}",
        "datasets: [{path: p, use_col: [close, close]}]\nbacktest: {}",
        "datasets: [{path: p, use_col: [close, returns]}]\nbacktest: {}",
        "datasets: [{path: p, fields: {close: close, returns: HoldingReturn}}]\nbacktest: {}",
        "datasets: [{path: p, format: wide, catalog: catalog.json, fields: {close: other.parquet}}]\nbacktest: {}",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {start_date: '2020-02-30'}",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {bars_per_year: 0}",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {warmup_bars: -1}",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {transaction_fee: -1}",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {combined_mode: other}",
        "datasets: [{path: p, use_col: [close]}]\nuniverse: [all, A]\nbacktest: {}"
    };
    for (const auto& config : invalid)
        EXPECT_THROW(BacktestConfig::from_yaml(files.file("invalid.yaml", config)), std::exception) << config;
}

TEST(ConfigLoader, ExecutionModeAcceptsOnlyD0AndD1) {
    ConfigFiles files;
    for (int mode : {0, 1}) {
        const auto config = BacktestConfig::from_yaml(files.file("mode.yaml",
            "datasets: [{path: p, use_col: [close]}]\nbacktest: {mode: " + std::to_string(mode) + "}"));
        EXPECT_EQ(config.mode, mode);
    }
    for (const std::string mode : {"-1", "2", "1.5", "true", "99999999999999999999"}) {
        EXPECT_THROW(BacktestConfig::from_yaml(files.file("bad_mode.yaml",
            "datasets: [{path: p, use_col: [close]}]\nbacktest: {mode: " + mode + "}")), std::exception);
    }
}

TEST(ConfigLoader, LatestEndDateIsDefaultAndExplicitDatesRemainFixed) {
    ConfigFiles files;
    for (const std::string option : {"{}", "{end_date: latest}", "{end_date: ''}", "{end_date: null}"}) {
        const auto config = BacktestConfig::from_yaml(files.file("latest.yaml",
            "datasets: [{path: p, use_col: [close]}]\nbacktest: " + option));
        EXPECT_EQ(config.end_date, "latest");
    }
    const auto fixed = BacktestConfig::from_yaml(files.file("fixed.yaml", legacy));
    EXPECT_EQ(fixed.end_date, "2020-12-31");
    EXPECT_THROW(BacktestConfig::from_yaml(files.file("bad.yaml",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {end_date: '2020-02-30'}")), std::exception);
    EXPECT_THROW(BacktestConfig::from_yaml(files.file("reversed.yaml",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {start_date: '2021-01-01', end_date: '2020-12-31'}")), std::exception);
}
