#include <gtest/gtest.h>
#include "data/config_loader.h"
#include <cmath>
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

TEST(ConfigLoader, MissingClosePolicyDefaultsToCarryAndAcceptsError) {
    ConfigFiles files;
    const auto defaults = BacktestConfig::from_yaml(files.file("default.yaml", legacy));
    EXPECT_EQ(defaults.missing_close_policy, "carry");
    const auto error = BacktestConfig::from_yaml(files.file("error.yaml",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {missing_close_policy: error}"));
    EXPECT_EQ(error.missing_close_policy, "error");
    for (const std::string policy : {"skip", "''"})
        EXPECT_THROW(BacktestConfig::from_yaml(files.file("bad.yaml",
            "datasets: [{path: p, use_col: [close]}]\nbacktest: {missing_close_policy: " + policy + "}")),
            std::exception);
}

TEST(ConfigLoader, RemovedSettlementAndCorporateActionSettingsAreRejected) {
    ConfigFiles files;
    const std::string base = "datasets: [{path: p, use_col: [close]}]\n";
    for (const std::string& setting : {
             "events: {path: old_events.parquet}\nbacktest: {}",
             "backtest: {corporate_actions: {path: old_ledger.parquet}}",
             "backtest: {default_delisting_return: 0.0}",
             "backtest: {stale_quote_settle_bars: 2}",
         }) {
        try {
            BacktestConfig::from_yaml(files.file("removed.yaml", base + setting));
            ADD_FAILURE() << "accepted removed setting: " << setting;
        } catch (const std::exception& error) {
            EXPECT_NE(std::string(error.what()).find("no longer supported"), std::string::npos)
                << setting << " threw: " << error.what();
        }
    }
}

// base: expands into the standard price/group datasets without listing every field
// (docs/neutralize_plan_2026-09-16.md 4.3).
TEST(ConfigLoader, BaseBlockRegistersNineStandardFieldsWithoutDatasets) {
    ConfigFiles files;
    const auto config = BacktestConfig::from_yaml(files.file("base.yaml", R"(base:
  cache: /cache/cache_v1_cash_1d
  groups: ../../../data/groups/cache_v1_cash_1d
universe: {path: Top500.parquet}
backtest: {start_date: '2020-01-01', warmup_bars: 21}
)"));
    EXPECT_EQ(config.base_cache, "/cache/cache_v1_cash_1d");
    EXPECT_EQ(config.base_groups, "../../../data/groups/cache_v1_cash_1d");
    ASSERT_EQ(config.datasets.size(), 2u);
    const DatasetConfig& prices = config.datasets[0];
    const DatasetConfig& groups = config.datasets[1];
    EXPECT_EQ(prices.name, "base_prices");
    EXPECT_EQ(groups.name, "base_groups");
    // Prices align exactly; group codes are carried forward (as-of) so a cache that grew
    // after the panels were built still classifies the new bars.
    EXPECT_EQ(prices.alignment, FieldAlignment::Exact);
    EXPECT_EQ(groups.alignment, FieldAlignment::AsOf);
    for (const DatasetConfig* dataset : {&prices, &groups}) {
        EXPECT_EQ(dataset->format, DatasetFormat::Wide) << dataset->name;
        EXPECT_FALSE(dataset->lazy) << dataset->name;
        EXPECT_TRUE(dataset->enabled) << dataset->name;
        EXPECT_EQ(dataset->time_col, "__index_level_0__") << dataset->name;
    }
    // Relative base paths stay relative; the loader resolves them against the config directory.
    EXPECT_EQ(prices.path, "/cache/cache_v1_cash_1d/BaseData");
    EXPECT_EQ(groups.path, "../../../data/groups/cache_v1_cash_1d");
    EXPECT_EQ(prices.fields, (std::map<std::string, std::string>{
        {"open", "open.parquet"}, {"high", "high.parquet"}, {"low", "low.parquet"},
        {"close", "close.parquet"}, {"volume", "volume.parquet"}, {"vwap", "VWAP.parquet"},
        {"cap", "marketCap_USD.parquet"}}));
    EXPECT_EQ(groups.fields, (std::map<std::string, std::string>{
        {"sector", "sector.parquet"}, {"industry", "industry.parquet"}}));
    EXPECT_EQ(prices.fields.size() + groups.fields.size(), 9u);
    // The legacy first-dataset view points at the base cache, and base fields stay out of use_columns.
    EXPECT_EQ(config.parquet_path, "/cache/cache_v1_cash_1d/BaseData");
    EXPECT_EQ(config.time_col, "__index_level_0__");
    EXPECT_TRUE(config.use_columns.empty());
    EXPECT_EQ(config.universe.path, "Top500.parquet");
    EXPECT_EQ(config.warmup_bars, 21u);
}

TEST(ConfigLoader, BaseBlockPrecedesUserDatasetsAndLeavesDuplicatesToTheLoader) {
    ConfigFiles files;
    files.file("catalog.json", R"({"fields":{"EPS":"963.parquet"}})");
    const auto config = BacktestConfig::from_yaml(files.file("both.yaml", R"(base:
  cache: /cache/v1
  groups: groups/v1
datasets:
  - {name: AFL, format: wide, path: /cache/v1/AflData_M, catalog: catalog.json, alignment: asof}
backtest: {}
)"));
    ASSERT_EQ(config.datasets.size(), 3u);
    EXPECT_EQ(config.datasets[0].name, "base_prices");
    EXPECT_EQ(config.datasets[1].name, "base_groups");
    EXPECT_EQ(config.datasets[2].name, "AFL");
    EXPECT_TRUE(config.datasets[2].lazy);
    EXPECT_EQ(config.datasets[2].alignment, FieldAlignment::AsOf);
    EXPECT_EQ(config.use_columns, (std::vector<std::string>{"EPS"}));
    // Redeclaring a base field parses here and fails in DataPanel::load with
    // "Duplicate public data field" (see test_parquet_loader.cpp).
    const auto duplicate = BacktestConfig::from_yaml(files.file("duplicate.yaml", R"(base:
  cache: /cache/v1
  groups: groups/v1
datasets:
  - {name: extra, format: wide, path: /cache/v1/BaseData, fields: {close: close.parquet}}
backtest: {}
)"));
    ASSERT_EQ(duplicate.datasets.size(), 3u);
    EXPECT_EQ(duplicate.datasets[0].fields.at("close"), "close.parquet");
    EXPECT_EQ(duplicate.datasets[2].fields.at("close"), "close.parquet");
}

TEST(ConfigLoader, BaseBlockNeedsCacheAndGroupsAndDatasetsRemainRequiredWithoutIt) {
    ConfigFiles files;
    // Each case pins the message so a regression that throws somewhere else is caught.
    const std::vector<std::pair<std::string, std::string>> invalid = {
        {"base: {cache: /cache/v1}\nbacktest: {}", "base requires groups"},
        {"base: {groups: g}\nbacktest: {}", "base requires cache"},
        {"base: {cache: /cache/v1, groups: null}\nbacktest: {}", "base requires groups"},
        {"base: {cache: null, groups: g}\nbacktest: {}", "base requires cache"},
        {"base: {cache: /cache/v1, groups: ''}\nbacktest: {}", "base.groups is empty"},
        {"base: {cache: '', groups: g}\nbacktest: {}", "base.cache is empty"},
        {"base: [/cache/v1, g]\nbacktest: {}", "base must be a mapping with cache and groups"},
        {"base: cache\nbacktest: {}", "base must be a mapping with cache and groups"},
        // Without base:, the missing-datasets message is unchanged.
        {"backtest: {}", "config.yaml: datasets must be a non-empty sequence"},
        {"datasets: []\nbacktest: {}", "config.yaml: datasets must be a non-empty sequence"},
        {"datasets: {path: p}\nbacktest: {}", "config.yaml: datasets must be a non-empty sequence"},
        // datasets: stays a sequence even when base: supplies the standard fields.
        {"base: {cache: /c, groups: g}\ndatasets: {path: p}\nbacktest: {}",
         "config.yaml: datasets must be a non-empty sequence"},
        // The two expanded dataset names are reserved.
        {"base: {cache: /c, groups: g}\ndatasets: [{name: base_groups, path: p, use_col: [x]}]\nbacktest: {}",
         "Duplicate dataset name: base_groups"},
        {"base: {cache: /c, groups: g}\ndatasets: [{name: base_prices, path: p, use_col: [x]}]\nbacktest: {}",
         "Duplicate dataset name: base_prices"},
        // close is still required when there is no base block.
        {"datasets: [{path: p, use_col: [volume]}]\nbacktest: {}", "An enabled dataset must provide close"}
    };
    for (const auto& invalid_case : invalid) {
        try {
            BacktestConfig::from_yaml(files.file("invalid_base.yaml", invalid_case.first));
            ADD_FAILURE() << "expected an error for: " << invalid_case.first;
        } catch (const std::exception& error) {
            EXPECT_NE(std::string(error.what()).find(invalid_case.second), std::string::npos)
                << invalid_case.first << "\n  threw: " << error.what();
        }
    }
    // With base:, an omitted or empty datasets sequence is accepted.
    for (const std::string body : {"base: {cache: /c, groups: g}\nbacktest: {}",
                                   "base: {cache: /c, groups: g}\ndatasets: []\nbacktest: {}",
                                   "base: {cache: /c, groups: g}\ndatasets:\nbacktest: {}"}) {
        const auto config = BacktestConfig::from_yaml(files.file("base_only.yaml", body));
        EXPECT_EQ(config.datasets.size(), 2u) << body;
    }
}

TEST(ConfigLoader, CostsDefaultToEveryModuleOffAndReadEveryKey) {
    ConfigFiles files;
    const auto config = BacktestConfig::from_yaml(files.file("config.yaml", legacy));
    EXPECT_FALSE(config.costs.borrow.enabled);
    EXPECT_TRUE(std::isnan(config.costs.borrow.default_rate));
    EXPECT_TRUE(config.costs.borrow.path.empty());
    EXPECT_EQ(config.costs.borrow.day_count, 360u);
    EXPECT_EQ(config.costs.borrow.symbol_col, "symbol");
    EXPECT_EQ(config.costs.borrow.from_col, "from");
    EXPECT_EQ(config.costs.borrow.to_col, "to");
    EXPECT_EQ(config.costs.borrow.rate_col, "rate");
    EXPECT_TRUE(config.costs.report_unapplied);

    const auto explicit_config = BacktestConfig::from_yaml(files.file("costs.yaml", R"(datasets: [{path: p, use_col: [close]}]
backtest:
  costs:
    report_unapplied: false
    borrow:
      enabled: true
      default_rate: 0.0125
      path: costs/borrow_rates.csv
      day_count: 365
      symbol_col: tradingItemId
      from_col: validFrom
      to_col: validTo
      rate_col: annualRate
)"));
    EXPECT_TRUE(explicit_config.costs.borrow.enabled);
    EXPECT_DOUBLE_EQ(explicit_config.costs.borrow.default_rate, 0.0125);
    EXPECT_EQ(explicit_config.costs.borrow.path, "costs/borrow_rates.csv");
    EXPECT_EQ(explicit_config.costs.borrow.day_count, 365u);
    EXPECT_EQ(explicit_config.costs.borrow.symbol_col, "tradingItemId");
    EXPECT_EQ(explicit_config.costs.borrow.from_col, "validFrom");
    EXPECT_EQ(explicit_config.costs.borrow.to_col, "validTo");
    EXPECT_EQ(explicit_config.costs.borrow.rate_col, "annualRate");
    EXPECT_FALSE(explicit_config.costs.report_unapplied);
    // Null values keep the defaults: no default rate, no table.
    const auto nulls = BacktestConfig::from_yaml(files.file("nulls.yaml",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {costs: {borrow: {enabled: true, default_rate: null, path: null}}}"));
    EXPECT_TRUE(nulls.costs.borrow.enabled);
    EXPECT_TRUE(std::isnan(nulls.costs.borrow.default_rate));
    EXPECT_TRUE(nulls.costs.borrow.path.empty());

    const std::string datasets = "datasets: [{path: p, use_col: [close]}]\n";
    const std::vector<std::pair<std::string, std::string>> invalid = {
        {datasets + "backtest: {costs: [borrow]}", "must be a mapping"},
        {datasets + "backtest: {costs: {borrow: true}}", "must be a mapping"},
        {datasets + "backtest: {costs: {borrow: {day_count: 0}}}", "outside the supported range"},
        {datasets + "backtest: {costs: {borrow: {day_count: -1}}}", "outside the supported range"},
        {datasets + "backtest: {costs: {borrow: {default_rate: -0.01}}}", "nonnegative"},
        {datasets + "backtest: {costs: {borrow: {default_rate: .nan}}}", "nonnegative"},
        {datasets + "backtest: {costs: {borrow: {rate_col: ''}}}", "must not be empty"},
        {datasets + "backtest: {costs: {borrow: {from_col: same, to_col: same}}}", "must be distinct"},
    };
    for (const auto& invalid_case : invalid) {
        try {
            BacktestConfig::from_yaml(files.file("invalid_costs.yaml", invalid_case.first));
            ADD_FAILURE() << "expected an error for: " << invalid_case.first;
        } catch (const std::exception& error) {
            EXPECT_NE(std::string(error.what()).find(invalid_case.second), std::string::npos)
                << invalid_case.first << "\n  threw: " << error.what();
        }
    }
}
