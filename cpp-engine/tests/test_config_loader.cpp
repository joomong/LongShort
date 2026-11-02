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
    EXPECT_EQ(config.warmup_bars, 0u);
    EXPECT_TRUE(config.universe.symbols.empty());
}
TEST(ConfigLoader, WideCatalogRegistersAliasesLazilyAndSkipsDisabledDatasets) {
    ConfigFiles files;
    files.file("catalog.json", R"({"schema_version":2,"fields":{"EPS":"963.parquet","EPS_alias":"963.parquet"}})");
    auto config = BacktestConfig::from_yaml(files.file("config.yaml", R"(datasets:
  - {name: prices, path: prices.parquet, use_col: [close]}
  - {name: afl, format: wide, path: AflData_M, catalog: catalog.json, alignment: asof}
  - {name: inactive, enabled: false}
universe: {path: Top500.parquet, symbols: ['10', '2']}
backtest: {start_date: '2020-01-01', end_date: '2020-02-01', warmup_bars: 21, bars_per_year: 252}
)"));
    ASSERT_EQ(config.datasets.size(), 3u);
    EXPECT_TRUE(config.datasets[1].lazy);
    EXPECT_EQ(config.datasets[1].alignment, FieldAlignment::AsOf);
    EXPECT_EQ(config.datasets[1].time_col, "__index_level_0__");
    EXPECT_EQ(config.datasets[1].fields.at("afl.EPS"), config.datasets[1].fields.at("afl.EPS_alias"));
    EXPECT_EQ(config.use_columns, (std::vector<std::string>{"close", "afl.EPS", "afl.EPS_alias"}));
    EXPECT_EQ(config.use_columns.size(), 3u);
    EXPECT_EQ(config.warmup_bars, 21u);
    EXPECT_EQ(config.bars_per_year, 252u);
}
TEST(ConfigLoader, RejectsAmbiguousFieldsAndInvalidSettings) {
    ConfigFiles files;
    files.file("catalog.json", R"({"schema_version":2,"fields":{"close":"first.parquet"}})");
    const std::vector<std::string> invalid = {
        "datasets: [{path: p, use_col: [close]}, {path: q, use_col: [close]}]\nbacktest: {}",
        "datasets: [{path: p, use_col: [close, close]}]\nbacktest: {}",
        "datasets: [{path: p, use_col: [close, returns]}]\nbacktest: {}",
        "datasets: [{path: p, fields: {close: close, returns: HoldingReturn}}]\nbacktest: {}",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {start_date: '2020-02-30'}",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {bars_per_year: 0}",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {warmup_bars: -1}",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {transaction_fee: -1}",
        "datasets: [{path: p, use_col: [close]}]\nbacktest: {combined_mode: normalized}",  // removed key is now unknown
        "datasets: [{path: p, use_col: [close]}]\nuniverse: [all, A]\nbacktest: {}"
    };
    for (const auto& config : invalid)
        EXPECT_THROW(BacktestConfig::from_yaml(files.file("invalid.yaml", config)), std::exception) << config;
    // A catalog name and a fields: entry of the same wide dataset must agree (close comes from a long dataset).
    try {
        BacktestConfig::from_yaml(files.file("conflict.yaml", "datasets: [{path: q, use_col: [close]}, "
            "{name: w, path: p, format: wide, catalog: catalog.json, fields: {close: other.parquet}}]\nbacktest: {}"));
        ADD_FAILURE() << "accepted a catalog/fields conflict";
    } catch (const std::exception& error) {
        EXPECT_EQ(std::string(error.what()), "Conflicting field mapping: close");
    }
}

TEST(ConfigLoader, ExecutionPriceAcceptsCloseOhlc4AndVwap) {
    ConfigFiles files;
    for (const std::string price : {"close", "ohlc4", "vwap"}) {
        const auto config = BacktestConfig::from_yaml(files.file("price.yaml",
            "datasets: [{path: p, use_col: [close]}]\nbacktest: {execution_price: " + price + "}"));
        EXPECT_EQ(config.execution_price, price);
    }
    for (const std::string price : {"VWAP", "open", "typo"})
        EXPECT_THROW(BacktestConfig::from_yaml(files.file("bad_price.yaml",
            "datasets: [{path: p, use_col: [close]}]\nbacktest: {execution_price: " + price + "}")), std::exception);
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

TEST(ConfigLoader, AlphasDirDefaultsBesideTheConfigAndResolvesRelativePaths) {
    ConfigFiles files;
    const auto dir = std::filesystem::path(files.dir).lexically_normal();
    const auto defaults = BacktestConfig::from_yaml(files.file("default.yaml", legacy));
    EXPECT_EQ(defaults.alphas_dir, (dir / "alphas").string());
    const auto relative = BacktestConfig::from_yaml(files.file("relative.yaml",
        "alphas_dir: saved/../my_alphas/\n" + legacy));
    EXPECT_EQ(relative.alphas_dir, (dir / "my_alphas").string());
    const auto absolute = BacktestConfig::from_yaml(files.file("absolute.yaml",
        "alphas_dir: /var/tmp/longshort_alphas\n" + legacy));
    EXPECT_EQ(absolute.alphas_dir, "/var/tmp/longshort_alphas");
    EXPECT_THROW(BacktestConfig::from_yaml(files.file("empty.yaml", "alphas_dir: ''\n" + legacy)),
                 std::exception);
}

TEST(ConfigLoader, MetaDirDefaultsBesideTheConfigAndResolvesRelativePaths) {
    ConfigFiles files;
    const auto dir = std::filesystem::path(files.dir).lexically_normal();
    EXPECT_EQ(BacktestConfig::from_yaml(files.file("default.yaml", legacy)).meta_dir, (dir / "meta").string());
    EXPECT_EQ(BacktestConfig::from_yaml(files.file("relative.yaml", "meta_dir: sets/../my_meta/\n" + legacy)).meta_dir,
              (dir / "my_meta").string());
    EXPECT_EQ(BacktestConfig::from_yaml(files.file("absolute.yaml", "meta_dir: /var/tmp/longshort_meta\n" + legacy)).meta_dir,
              "/var/tmp/longshort_meta");
    EXPECT_THROW(BacktestConfig::from_yaml(files.file("empty.yaml", "meta_dir: ''\n" + legacy)), std::exception);
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
            EXPECT_NE(std::string(error.what()).find("Unknown config key"), std::string::npos)
                << setting << " threw: " << error.what();
        }
    }
}
TEST(ConfigLoader, RejectsUnknownKeys) {
    ConfigFiles files;
    const std::string dataset = "datasets: [{path: p, use_col: [close]}]\n";
    const std::vector<std::pair<std::string, std::string>> cases = {
        {dataset + "backtest: {missing_close_polcy: error}", "backtest.missing_close_polcy"},
        {dataset + "backtest: {}\nalpha_dir: alphas", "alpha_dir"},
        {dataset + "backtest: {meta_dir: meta}", "backtest.meta_dir"},
        {"datasets: [{path: p, use_col: [close], use_cols: [open]}]\nbacktest: {}", "datasets[0].use_cols"},
        {dataset + "universe: {path: u.parquet, nmae: TOP500}\nbacktest: {}", "universe.nmae"},
    };
    for (const auto& [body, key] : cases) {
        try {
            BacktestConfig::from_yaml(files.file("unknown.yaml", body));
            ADD_FAILURE() << "accepted unknown key: " << key;
        } catch (const std::exception& error) {
            EXPECT_EQ(std::string(error.what()), "Unknown config key '" + key + "'");
        }
    }
}
// Parsing alone touches no cache file except an optional manifest.json probe (exists() check).
TEST(ConfigLoader, AcceptsRepositoryExampleConfig) {
    // The root config.example.yaml is what `longshort start` copies into a new workspace.
    const auto path = std::filesystem::path(__FILE__).parent_path().parent_path().parent_path() / "config.example.yaml";
    ASSERT_TRUE(std::filesystem::exists(path)) << path;
    const auto config = BacktestConfig::from_yaml(path.string());
    EXPECT_EQ(config.universe.name, "TOP3000");
    EXPECT_EQ(config.bars_per_year, 252u);
}

// base: expands into the standard price/group datasets without listing every field
// (docs/data_contract.md 4.3).
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
    files.file("catalog.json", R"({"schema_version":2,"fields":{"EPS":"963.parquet"}})");
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
    EXPECT_EQ(config.use_columns, (std::vector<std::string>{"AFL.EPS"}));
    // Redeclaring a base field parses here and fails in DataPanel::load with
    // "Duplicate public data field" (see test_parquet_loader.cpp).
    const auto duplicate = BacktestConfig::from_yaml(files.file("duplicate.yaml", R"(base:
  cache: /cache/v1
  groups: groups/v1
datasets:
  - {name: extra, path: /cache/v1/close.parquet, use_col: [close]}
backtest: {}
)"));
    ASSERT_EQ(duplicate.datasets.size(), 3u);
    EXPECT_EQ(duplicate.datasets[0].fields.at("close"), "close.parquet");
    EXPECT_EQ(duplicate.datasets[2].fields.at("close"), "close");
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

// Wide dataset fields are published only as `<dataset name>.<field>` (catalog and fields: alike);
// base and long-format fields stay bare.
TEST(ConfigLoader, WideDatasetFieldsAreRegisteredOnlyDotted) {
    ConfigFiles files;
    files.file("catalog.json", R"({"schema_version":2,"fields":{"EPS":"963.parquet","10DMACD":"377.parquet"}})");
    const auto config = BacktestConfig::from_yaml(files.file("dotted.yaml", R"(base: {cache: /c, groups: g}
datasets:
  - {name: AFL, format: wide, path: AflData, catalog: catalog.json, fields: {x: x.parquet}}
  - {name: B, format: wide, path: BData, fields: {x: other.parquet}}
  - {name: extra, path: extra.parquet, use_col: [volume_long]}
backtest: {}
)"));
    ASSERT_EQ(config.datasets.size(), 5u);
    EXPECT_EQ(config.datasets[2].fields, (std::map<std::string, std::string>{
        {"AFL.EPS", "963.parquet"}, {"AFL.10DMACD", "377.parquet"}, {"AFL.x", "x.parquet"}}));
    EXPECT_EQ(config.datasets[3].fields, (std::map<std::string, std::string>{{"B.x", "other.parquet"}}));
    EXPECT_EQ(config.datasets[4].fields.at("volume_long"), "volume_long");
    EXPECT_EQ(config.use_columns, (std::vector<std::string>{"AFL.10DMACD", "AFL.EPS", "AFL.x", "B.x", "volume_long"}));
    // A wide dataset alone can no longer supply the bare close.
    try {
        BacktestConfig::from_yaml(files.file("wide_close.yaml",
            "datasets: [{name: prices, format: wide, path: BaseData, fields: {close: close.parquet}}]\nbacktest: {}"));
        ADD_FAILURE() << "a wide dataset supplied bare close";
    } catch (const std::exception& error) {
        EXPECT_EQ(std::string(error.what()), "An enabled dataset must provide close");
    }
    // `returns` is reserved only as a bare name; a wide field may be called returns.
    const auto returns = BacktestConfig::from_yaml(files.file("wide_returns.yaml",
        "base: {cache: /c, groups: g}\ndatasets: [{name: AFL, format: wide, path: p, fields: {returns: r.parquet}, "
        "use_col: [x]}]\nbacktest: {}"));
    EXPECT_EQ(returns.datasets[2].fields, (std::map<std::string, std::string>{
        {"AFL.returns", "r.parquet"}, {"AFL.x", "x.parquet"}}));
    EXPECT_THROW(BacktestConfig::from_yaml(files.file("bare_returns.yaml",
        "datasets: [{path: p, use_col: [close, returns]}]\nbacktest: {}")), std::exception);
}
TEST(ConfigLoader, WideDatasetNamesFieldPartsAndCatalogSchemaAreValidated) {
    ConfigFiles files;
    files.file("v1.json", R"({"schema_version":1,"fields":{"afl_963":"963.parquet"}})");
    files.file("none.json", R"({"fields":{"EPS":"963.parquet"}})");
    files.file("v3.json", R"({"schema_version":3,"fields":{"EPS":"963.parquet"}})");
    const std::string base = "base: {cache: /c, groups: g}\ndatasets: [";
    const std::vector<std::pair<std::string, std::string>> invalid = {
        {"{name: AFL, format: wide, path: p, catalog: v1.json}",
         "v1.json: AFL catalog schema_version 1 is no longer supported (rebuild with universe_builder/afl/build_afl_catalog.py)"},
        {"{name: AFL, format: wide, path: p, catalog: none.json}",
         "none.json: AFL catalog schema_version 1 is no longer supported"},
        {"{name: AFL, format: wide, path: p, catalog: v3.json}", "unsupported AFL catalog schema_version 3"},
        {"{format: wide, path: p, fields: {x: x.parquet}}", "Wide dataset requires a name"},
        {"{name: 1AFL, format: wide, path: p, fields: {x: x.parquet}}", "Wide dataset requires a name"},
        {"{name: A_B-C, format: wide, path: p, fields: {x: x.parquet}}", "Wide dataset requires a name"},
        {"{name: _AFL, format: wide, path: p, fields: {x: x.parquet}}", "Wide dataset requires a name"},
        {"{name: AFL, format: wide, path: p, fields: {'377': x.parquet}}", "Invalid field mapping: 377"},
        {"{name: AFL, format: wide, path: p, fields: {PM-style: x.parquet}}", "Invalid field mapping: PM-style"},
        {"{name: AFL, format: wide, path: p, fields: {a.b: x.parquet}}", "Invalid field mapping: a.b"},
        // Long-format names keep the identifier rule (no leading digit).
        {"{name: L, path: p, fields: {10DMACD: c}}", "Invalid field mapping: 10DMACD"},
    };
    for (const auto& [datasets, message] : invalid) {
        try {
            BacktestConfig::from_yaml(files.file("invalid.yaml", base + datasets + "]\nbacktest: {}"));
            ADD_FAILURE() << "accepted: " << datasets;
        } catch (const std::exception& error) {
            EXPECT_NE(std::string(error.what()).find(message), std::string::npos) << datasets << " threw: " << error.what();
        }
    }
}

TEST(ConfigLoader, PitLagDefaultsToOneBar) {
    ConfigFiles files;
    const std::string head = R"(base:
  cache: /cache/cache_v2_cash_1d
  groups: /cache/cache_v2_cash_1d/groups
universe: {path: universe.parquet, name: TOP500_CAP}
)";
    EXPECT_EQ(BacktestConfig::from_yaml(files.file("lag_default.yaml", head + "backtest: {start_date: '2020-01-01'}\n")).pit_lag, 1u);
    EXPECT_EQ(BacktestConfig::from_yaml(files.file("lag_zero.yaml", head + "backtest: {start_date: '2020-01-01', pit_lag: 0}\n")).pit_lag, 0u);
    EXPECT_EQ(BacktestConfig::from_yaml(files.file("lag_two.yaml", head + "backtest: {start_date: '2020-01-01', pit_lag: 2}\n")).pit_lag, 2u);
    EXPECT_THROW(BacktestConfig::from_yaml(files.file("lag_neg.yaml", head + "backtest: {start_date: '2020-01-01', pit_lag: -1}\n")), std::runtime_error);
}

TEST(ConfigLoader, AlphaPoolKeysAreOptionalAndResolveAgainstTheConfigDir) {
    ConfigFiles files;
    const std::string head = R"(base:
  cache: /cache/cache_v2_cash_1d
  groups: /cache/cache_v2_cash_1d/groups
universe: {path: universe.parquet, names: [TOP3000, TOP500_CAP]}
backtest: {start_date: '2020-01-01'}
)";
    const auto none = BacktestConfig::from_yaml(files.file("pool_none.yaml", head));
    EXPECT_TRUE(none.alpha_pool.empty());
    EXPECT_TRUE(none.alpha_pool_universe.empty());
    const auto set = BacktestConfig::from_yaml(files.file("pool_set.yaml", head + "alpha_pool: /home/base/alpha_pool\nalpha_pool_universe: TOP500_CAP\n"));
    EXPECT_EQ(set.alpha_pool, "/home/base/alpha_pool");
    EXPECT_EQ(set.alpha_pool_universe, "TOP500_CAP");
    const auto rel = BacktestConfig::from_yaml(files.file("pool_rel.yaml", head + "alpha_pool: pool\n"));
    EXPECT_EQ(std::filesystem::path(rel.alpha_pool).filename(), "pool");
    EXPECT_TRUE(std::filesystem::path(rel.alpha_pool).is_absolute());
    EXPECT_THROW(BacktestConfig::from_yaml(files.file("pool_bad.yaml", head + "alpha_pool_universe: ''\n")), std::runtime_error);
    EXPECT_THROW(BacktestConfig::from_yaml(files.file("pool_typo.yaml", head + "alpha_pools: /x\n")), std::runtime_error);
}
