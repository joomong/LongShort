#include <gtest/gtest.h>
#include "loader.h"
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
    std::string file(const std::string& name, const std::string& text) {
        std::ofstream(dir / name) << text;
        return (dir / name).string();
    }
};
const std::string config_text = R"(base: {cache: cache, groups: groups}
afl: {path: afl, catalog: catalog.json}
universe: {names: [TOP500_CAP, TOP1000_CAP], symbols: ['101', '102']}
backtest:
  start_date: '2020-01-01'
  end_date: '2020-12-31'
  initial_capital: 1000000
  transaction_fee: 1.5
  bars_per_year: 252
  warmup_bars: 63
  execution_price: vwap
)";
}

TEST(ConfigLoader, ReadsCachePathsAndRuntimeSettings) {
    ConfigFiles files;
    const auto config = BacktestConfig::from_yaml(files.file("config.yaml", config_text));
    EXPECT_EQ(config.base_cache, "cache"); EXPECT_EQ(config.base_groups, "groups");
    EXPECT_EQ(config.afl_path, "afl"); EXPECT_EQ(config.afl_catalog, "catalog.json");
    EXPECT_EQ(config.universe.path, "cache/universe.parquet");
    EXPECT_EQ(config.universe.names, (std::vector<std::string>{"TOP500_CAP", "TOP1000_CAP"}));
    EXPECT_EQ(config.universe.name, "TOP500_CAP");
    EXPECT_EQ(config.universe.symbols, (std::vector<std::string>{"101", "102"}));
    EXPECT_DOUBLE_EQ(config.initial_capital, 1000000);
    EXPECT_NEAR(config.transaction_fee_rate, 0.00015, 1e-15);
    EXPECT_EQ(config.bars_per_year, 252u); EXPECT_EQ(config.warmup_bars, 63u);
    EXPECT_EQ(config.execution_price, "vwap");
}

TEST(ConfigLoader, DefaultsCacheSubdirectoriesAndOptionalAfl) {
    ConfigFiles files;
    const auto plain = BacktestConfig::from_yaml(files.file("plain.yaml", "base: {cache: /cache}\nbacktest: {}\n"));
    EXPECT_EQ(plain.base_groups, "/cache/groups");
    EXPECT_TRUE(plain.afl_path.empty()); EXPECT_TRUE(plain.afl_catalog.empty());
    EXPECT_EQ(plain.end_date, "latest"); EXPECT_EQ(plain.missing_close_policy, "carry");
    const auto afl = BacktestConfig::from_yaml(files.file("afl.yaml", "base: {cache: /cache}\nafl: {}\nbacktest: {}\n"));
    EXPECT_EQ(afl.afl_path, "/cache/AflData"); EXPECT_EQ(afl.afl_catalog, "/cache/afl_catalog.json");
}

TEST(ConfigLoader, ResolvesOutputDirectoriesBesideTheConfig) {
    ConfigFiles files;
    const auto config = BacktestConfig::from_yaml(files.file("config.yaml", config_text +
        "alphas_dir: saved\nmeta_dir: portfolios\nalpha_pool: approved\nalpha_pool_universe: TOP500_CAP\n"));
    EXPECT_EQ(config.alphas_dir, (files.dir / "saved").string());
    EXPECT_EQ(config.meta_dir, (files.dir / "portfolios").string());
    EXPECT_EQ(config.alpha_pool, (files.dir / "approved").string());
    EXPECT_EQ(config.alpha_pool_universe, "TOP500_CAP");
}

TEST(ConfigLoader, RuntimeOptionsValidateTheirInputs) {
    ConfigFiles files;
    for (const std::string option : {"execution_price: unknown", "missing_close_policy: unknown", "warmup_bars: -1",
                                     "bars_per_year: 0", "transaction_fee: -1", "initial_capital: 0",
                                     "start_date: '2020-02-30'", "end_date: 'wrong'"})
        EXPECT_THROW(BacktestConfig::from_yaml(files.file("bad.yaml", "base: {cache: cache}\nbacktest: {" + option + "}\n")), std::exception);
    EXPECT_THROW(BacktestConfig::from_yaml(files.file("dates.yaml",
        "base: {cache: cache}\nbacktest: {start_date: '2021-01-01', end_date: '2020-01-01'}\n")), std::exception);
}

TEST(ConfigLoader, RepositoryExampleConfigUsesTheSharedCache) {
    const auto path = std::filesystem::path(__FILE__).parent_path().parent_path().parent_path() / "config.example.yaml";
    const auto config = BacktestConfig::from_yaml(path.string());
    EXPECT_FALSE(config.base_cache.empty()); EXPECT_FALSE(config.afl_catalog.empty());
    EXPECT_EQ(config.execution_price, "vwap");
}

TEST(ConfigLoader, EmptyUniverseNamesSelectAllPriceSymbols) {
    ConfigFiles files;
    const auto config = BacktestConfig::from_yaml(files.file("all.yaml",
        "base: {cache: /cache}\nuniverse: {names: []}\nbacktest: {}\n"));
    EXPECT_TRUE(config.universe.name.empty());
    EXPECT_TRUE(config.universe.names.empty());
}
