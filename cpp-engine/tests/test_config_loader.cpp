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

TEST(ConfigLoader, MissingCloseAccountingDefaultsToCarryWithoutEvents) {
    ConfigFiles files;
    const auto config = BacktestConfig::from_yaml(files.file("config.yaml", legacy));
    EXPECT_EQ(config.missing_close_policy, "carry");
    EXPECT_DOUBLE_EQ(config.default_delisting_return, 0.0);
    EXPECT_EQ(config.stale_quote_settle_bars, 0u);
    EXPECT_TRUE(config.events.path.empty());
    EXPECT_EQ(config.events.id_col, "tradingItemId");
    EXPECT_EQ(config.events.end_date_col, "securityEndDate");
    EXPECT_TRUE(config.events.return_col.empty());
}

TEST(ConfigLoader, MissingCloseAccountingReadsExplicitOptionsAndEvents) {
    ConfigFiles files;
    const auto config = BacktestConfig::from_yaml(files.file("events.yaml", R"(datasets: [{path: p, use_col: [close]}]
events:
  path: master/secmaster.parquet
  id_col: securityId
  end_date_col: endDate
  return_col: settlementReturn
backtest:
  missing_close_policy: error
  default_delisting_return: -1.0
  stale_quote_settle_bars: 60
)"));
    EXPECT_EQ(config.missing_close_policy, "error");
    EXPECT_DOUBLE_EQ(config.default_delisting_return, -1.0);
    EXPECT_EQ(config.stale_quote_settle_bars, 60u);
    EXPECT_EQ(config.events.path, "master/secmaster.parquet");
    EXPECT_EQ(config.events.id_col, "securityId");
    EXPECT_EQ(config.events.end_date_col, "endDate");
    EXPECT_EQ(config.events.return_col, "settlementReturn");
    EXPECT_TRUE(config.events.successor_col.empty());
    EXPECT_TRUE(config.events.successor_ratio_col.empty());

    // Successor columns are optional and must be distinct from every other name.
    const auto successors = BacktestConfig::from_yaml(files.file("successors.yaml",
        R"(datasets: [{path: p, use_col: [close]}]
events:
  path: data/events/events.parquet
  return_col: settlementReturn
  successor_col: successorTradingItemId
  successor_ratio_col: successorRatio
  cash_col: cashPerShare
  successor_price_col: successorCacheClose
  successor_price_date_col: successorCacheCloseDate
  target_price_col: targetCacheMark
  target_price_date_col: targetCacheMarkDate
  stock_leg_col: hasStockLeg
  cash_leg_col: hasCashLeg
backtest: {}
)"));
    EXPECT_EQ(successors.events.successor_col, "successorTradingItemId");
    EXPECT_EQ(successors.events.successor_ratio_col, "successorRatio");
    EXPECT_EQ(successors.events.cash_col, "cashPerShare");
    EXPECT_EQ(successors.events.successor_price_col, "successorCacheClose");
    EXPECT_EQ(successors.events.successor_price_date_col, "successorCacheCloseDate");
    EXPECT_EQ(successors.events.target_price_col, "targetCacheMark");
    EXPECT_EQ(successors.events.target_price_date_col, "targetCacheMarkDate");
    EXPECT_EQ(successors.events.stock_leg_col, "hasStockLeg");
    EXPECT_EQ(successors.events.cash_leg_col, "hasCashLeg");
    for (const char* broken : {
             "events: {path: e.parquet, successor_ratio_col: r}\nbacktest: {}",
             "events: {path: e.parquet, successor_col: tradingItemId}\nbacktest: {}",
             "events: {path: e.parquet, successor_col: s, successor_ratio_col: s}\nbacktest: {}",
             "events: {path: e.parquet, cash_col: settlementReturn, return_col: settlementReturn}\nbacktest: {}",
             "events: {path: e.parquet, successor_col: s, successor_price_col: p}\nbacktest: {}",
             "events: {path: e.parquet, successor_price_date_col: d}\nbacktest: {}",
             "events: {path: e.parquet, successor_price_col: p, successor_price_date_col: d}\nbacktest: {}",
             "events: {path: e.parquet, target_price_col: m}\nbacktest: {}",
             "events: {path: e.parquet, target_price_date_col: d}\nbacktest: {}",
             "events: {path: e.parquet, stock_leg_col: tradingItemId}\nbacktest: {}"}) {
        EXPECT_THROW(BacktestConfig::from_yaml(files.file("broken_successor.yaml",
            std::string("datasets: [{path: p, use_col: [close]}]\n") + broken)),
            std::runtime_error) << broken;
    }

    // A path alone keeps the vendor-master column defaults.
    const auto defaults = BacktestConfig::from_yaml(files.file("path_only.yaml",
        "datasets: [{path: p, use_col: [close]}]\nevents: {path: /abs/master.parquet}\nbacktest: {missing_close_policy: carry}"));
    EXPECT_EQ(defaults.events.path, "/abs/master.parquet");
    EXPECT_EQ(defaults.events.id_col, "tradingItemId");
    EXPECT_EQ(defaults.events.end_date_col, "securityEndDate");
    EXPECT_TRUE(defaults.events.return_col.empty());
    EXPECT_EQ(defaults.stale_quote_settle_bars, 0u);
}

TEST(ConfigLoader, RejectsInvalidMissingCloseOptionsAndEventMappings) {
    ConfigFiles files;
    const std::string datasets = "datasets: [{path: p, use_col: [close]}]\n";
    const std::vector<std::string> invalid = {
        datasets + "backtest: {missing_close_policy: skip}",
        datasets + "backtest: {missing_close_policy: ''}",
        datasets + "backtest: {default_delisting_return: .nan}",
        datasets + "backtest: {default_delisting_return: .inf}",
        datasets + "backtest: {default_delisting_return: none}",
        datasets + "backtest: {default_delisting_return: -1.5}",   // below a total loss
        datasets + "backtest: {default_delisting_return: 2.0}",    // above the supported range
        datasets + "backtest: {stale_quote_settle_bars: -1}",
        datasets + "backtest: {stale_quote_settle_bars: 1.5}",
        datasets + "events: {id_col: a, end_date_col: b}\nbacktest: {}",       // path is required
        datasets + "events: {path: ''}\nbacktest: {}",
        datasets + "events: [master.parquet]\nbacktest: {}",                   // must be a mapping
        datasets + "events: {path: m.parquet, id_col: ''}\nbacktest: {}",
        datasets + "events: {path: m.parquet, end_date_col: ''}\nbacktest: {}",
        datasets + "events: {path: m.parquet, id_col: same, end_date_col: same}\nbacktest: {}",
        datasets + "events: {path: m.parquet, end_date_col: same, return_col: same}\nbacktest: {}"
    };
    for (const auto& config : invalid)
        EXPECT_THROW(BacktestConfig::from_yaml(files.file("invalid_events.yaml", config)), std::exception) << config;
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

// backtest.corporate_actions: the ledger block (docs/corporate_actions_engine_2026-09-17.md).
TEST(ConfigLoader, CorporateActionsDefaultToNoLedgerWithTheExportedColumnNames) {
    ConfigFiles files;
    const auto config = BacktestConfig::from_yaml(files.file("config.yaml", legacy));
    const CorporateActionsConfig& ledger = config.corporate_actions;
    EXPECT_TRUE(ledger.path.empty());
    EXPECT_EQ(ledger.id_col, "tradingItemId");
    EXPECT_EQ(ledger.ex_date_col, "exDate");
    EXPECT_EQ(ledger.type_col, "eventType");
    EXPECT_EQ(ledger.cash_col, "cashPerShareCache");
    EXPECT_EQ(ledger.child_col, "childTradingItemId");
    EXPECT_EQ(ledger.child_ratio_col, "childRatioCache");
    EXPECT_EQ(ledger.child_close_col, "childCloseAtExCache");
    EXPECT_EQ(ledger.parent_close_col, "parentCloseAtExCache");
    EXPECT_EQ(ledger.parent_close_before_col, "parentCloseBeforeCache");
    EXPECT_EQ(ledger.parent_factor_col, "parentFactorCache");
    EXPECT_EQ(ledger.leg_col, "legType");
    EXPECT_EQ(ledger.estimate_col, "isEstimate");
    EXPECT_EQ(ledger.value_source_col, "childValueSource");
    EXPECT_EQ(ledger.status_col, "status");
    EXPECT_EQ(ledger.expiry_col, "expiryDate");
    EXPECT_EQ(ledger.subscription_price_col, "subscriptionPriceCache");
    EXPECT_EQ(ledger.exercise_ratio_col, "exerciseRatioCache");
    EXPECT_FALSE(ledger.auto_exercise);
    EXPECT_TRUE(ledger.apply_dividends);
    EXPECT_TRUE(ledger.apply_distributions);
    EXPECT_EQ(ledger.unpriced_child, "pending");
    EXPECT_TRUE(ledger.stale_check);
    EXPECT_EQ(ledger.unresolved_policy, "warn");
    // A null block is the same as an absent one.
    const auto null_block = BacktestConfig::from_yaml(files.file("null.yaml",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {corporate_actions: null}"));
    EXPECT_TRUE(null_block.corporate_actions.path.empty());
}

TEST(ConfigLoader, CorporateActionsReadEveryKeyAndRejectInvalidBlocks) {
    ConfigFiles files;
    const auto config = BacktestConfig::from_yaml(files.file("ledger.yaml", R"(datasets: [{path: p, use_col: [close]}]
backtest:
  corporate_actions:
    path: engine/ca_ledger_cache.parquet
    id_col: parentId
    ex_date_col: ex
    type_col: kind
    cash_col: cash
    child_col: child
    child_ratio_col: ratio
    child_close_col: childClose
    parent_close_col: parentClose
    parent_close_before_col: parentBefore
    parent_factor_col: factor
    leg_col: leg
    estimate_col: est
    value_source_col: src
    status_col: st
    expiry_col: expires
    subscription_price_col: strike
    exercise_ratio_col: per_right
    auto_exercise: false
    apply_dividends: false
    apply_distributions: false
    unpriced_child: drop
    stale_check: false
    unresolved_policy: strict
)"));
    const CorporateActionsConfig& ledger = config.corporate_actions;
    EXPECT_EQ(ledger.path, "engine/ca_ledger_cache.parquet");
    EXPECT_EQ(ledger.id_col, "parentId");
    EXPECT_EQ(ledger.ex_date_col, "ex");
    EXPECT_EQ(ledger.type_col, "kind");
    EXPECT_EQ(ledger.cash_col, "cash");
    EXPECT_EQ(ledger.child_col, "child");
    EXPECT_EQ(ledger.child_ratio_col, "ratio");
    EXPECT_EQ(ledger.child_close_col, "childClose");
    EXPECT_EQ(ledger.parent_close_col, "parentClose");
    EXPECT_EQ(ledger.parent_close_before_col, "parentBefore");
    EXPECT_EQ(ledger.parent_factor_col, "factor");
    EXPECT_EQ(ledger.leg_col, "leg");
    EXPECT_EQ(ledger.estimate_col, "est");
    EXPECT_EQ(ledger.value_source_col, "src");
    EXPECT_EQ(ledger.status_col, "st");
    EXPECT_EQ(ledger.expiry_col, "expires");
    EXPECT_EQ(ledger.subscription_price_col, "strike");
    EXPECT_EQ(ledger.exercise_ratio_col, "per_right");
    EXPECT_FALSE(ledger.auto_exercise);
    EXPECT_FALSE(ledger.apply_dividends);
    EXPECT_FALSE(ledger.apply_distributions);
    EXPECT_EQ(ledger.unpriced_child, "drop");
    EXPECT_FALSE(ledger.stale_check);
    EXPECT_EQ(ledger.unresolved_policy, "strict");
    // A path alone keeps the exported column names and the default policies.
    const auto defaults = BacktestConfig::from_yaml(files.file("path_only.yaml",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {corporate_actions: {path: /abs/ledger.parquet}}"));
    EXPECT_EQ(defaults.corporate_actions.path, "/abs/ledger.parquet");
    EXPECT_EQ(defaults.corporate_actions.cash_col, "cashPerShareCache");
    EXPECT_EQ(defaults.corporate_actions.unpriced_child, "pending");
    EXPECT_TRUE(defaults.corporate_actions.stale_check);
    EXPECT_EQ(defaults.corporate_actions.unresolved_policy, "warn");
    const auto warned = BacktestConfig::from_yaml(files.file("warn.yaml",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {corporate_actions: {path: l.parquet, unresolved_policy: warn}}"));
    EXPECT_EQ(warned.corporate_actions.unresolved_policy, "warn");

    const std::string datasets = "datasets: [{path: p, use_col: [close]}]\n";
    const std::vector<std::pair<std::string, std::string>> invalid = {
        {datasets + "backtest: {corporate_actions: {id_col: a}}", "requires a path"},
        {datasets + "backtest: {corporate_actions: {path: ''}}", "path is empty"},
        {datasets + "backtest: {corporate_actions: [ledger.parquet]}", "must be a mapping"},
        {datasets + "backtest: {corporate_actions: ledger.parquet}", "must be a mapping"},
        {datasets + "backtest: {corporate_actions: {path: l.parquet, cash_col: ''}}", "must not be empty"},
        {datasets + "backtest: {corporate_actions: {path: l.parquet, cash_col: same, child_col: same}}",
         "must be distinct"},
        {datasets + "backtest: {corporate_actions: {path: l.parquet, leg_col: status}}", "must be distinct"},
        {datasets + "backtest: {corporate_actions: {path: l.parquet, status_col: ''}}", "must not be empty"},
        {datasets + "backtest: {corporate_actions: {path: l.parquet, unpriced_child: wait}}",
         "must be 'pending' or 'drop'"},
        {datasets + "backtest: {corporate_actions: {path: l.parquet, unpriced_child: ''}}",
         "must be 'pending' or 'drop'"},
        // Automatic exercise is designed but not implemented: refused, never silently ignored.
        {datasets + "backtest: {corporate_actions: {path: l.parquet, auto_exercise: true}}",
         "auto_exercise is not implemented"},
        {datasets + "backtest: {corporate_actions: {path: l.parquet, expiry_col: status}}", "must be distinct"},
        // The unconfirmed-P&L policy has two spellings; anything else is refused, never a silent warn.
        {datasets + "backtest: {corporate_actions: {path: l.parquet, unresolved_policy: fail}}",
         "must be 'warn' or 'strict'"},
        {datasets + "backtest: {corporate_actions: {path: l.parquet, unresolved_policy: ''}}",
         "must be 'warn' or 'strict'"},
    };
    for (const auto& invalid_case : invalid) {
        try {
            BacktestConfig::from_yaml(files.file("invalid_ledger.yaml", invalid_case.first));
            ADD_FAILURE() << "expected an error for: " << invalid_case.first;
        } catch (const std::exception& error) {
            EXPECT_NE(std::string(error.what()).find(invalid_case.second), std::string::npos)
                << invalid_case.first << "\n  threw: " << error.what();
        }
    }
}

// backtest.costs: the borrow module (docs/corporate_actions_engine_2026-09-17.md "costs").
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
