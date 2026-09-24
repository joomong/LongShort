#pragma once

#include <cstdint>
#include <limits>
#include <map>
#include <string>
#include <vector>

enum class DatasetFormat { Long, Wide };
enum class FieldAlignment { Exact, AsOf };

struct DatasetConfig {
    std::string name;
    std::string path;
    DatasetFormat format = DatasetFormat::Long;
    bool enabled = true;
    bool lazy = false;
    std::string time_col = "open_time";
    std::string symbol_col = "symbol";
    std::map<std::string, std::string> fields; // public name -> column (long) / file (wide)
    FieldAlignment alignment = FieldAlignment::Exact;
};

struct UniverseConfig {
    std::string path; // optional wide membership file; null/absent cells are false
    std::string time_col = "__index_level_0__";
    std::vector<std::string> symbols; // optional fixed subset, intersected with membership
    std::string name; // interval membership (universe, tradingItemId, fromDate, toDate): universe to select
    FieldAlignment alignment = FieldAlignment::Exact;
};

// Optional delisting/settlement events (e.g. the secmaster parquet). One row per security.
struct EventsConfig {
    std::string path;                          // empty = no events
    std::string id_col = "tradingItemId";      // int64 or string; matched against panel symbols
    std::string end_date_col = "securityEndDate"; // timestamp/date/string YYYY-MM-DD; null = no event
    std::string return_col;                    // optional explicit settlement return (fraction); absent/NaN = default
    std::string successor_col;                 // optional surviving security id; absent/null = cash settlement
    std::string successor_ratio_col;           // optional successor shares per old share; absent/NaN = unknown
    std::string cash_col;                      // optional cash per old share; absent/NaN = no cash leg
    // Staleness guard. Cash and ratio are expressed in the price cache's own share
    // units, so the file is only valid for the cache it was built against. These two
    // columns record one successor close the generator saw; the loader re-reads it.
    std::string successor_price_col;           // optional successor close from the cache
    std::string successor_price_date_col;      // optional date of that close
    std::string target_price_col;              // optional last cache mark of the settling security
    std::string target_price_date_col;         // optional date of that mark
    // Structure of the consideration, independent of whether it can be priced here.
    std::string stock_leg_col;                 // optional bool; derived from ratio/successor id when absent
    std::string cash_leg_col;                  // optional bool; derived from the cash amount when absent
};

// Optional corporate-action ledger (backtest.corporate_actions, docs/corporate_actions_engine_2026-09-17.md).
// One row per (parent, ex-date, leg) in the price cache's share basis, as exported by the
// cache loader. `leg_col` names the leg (cash / stock / rights / quantity); a cash row carries
// the amount, a stock or rights row the child. Merger/rename rows belong to the events file
// and are refused here.
struct CorporateActionsConfig {
    std::string path;                                   // empty = no ledger
    std::string id_col = "tradingItemId";               // parent id; int64 or string
    std::string ex_date_col = "exDate";                 // timestamp/date/string YYYY-MM-DD
    std::string type_col = "eventType";                 // string
    std::string leg_col = "legType";                    // cash | stock | rights | quantity
    std::string cash_col = "cashPerShareCache";         // pre-tax cash per parent share; NaN = unresolved amount
    std::string child_col = "childTradingItemId";       // child id; null on a stock row = unresolved child
    std::string child_ratio_col = "childRatioCache";    // child shares per parent share
    std::string child_close_col = "childCloseAtExCache"; // child close on the ex-date; NaN = unknown
    std::string parent_close_col = "parentCloseAtExCache";        // staleness anchor on the ex-date
    std::string parent_close_before_col = "parentCloseBeforeCache"; // staleness anchor: last close before the ex-date
    std::string parent_factor_col = "parentFactorCache"; // must be 1 in a split-adjusted panel
    std::string estimate_col = "isEstimate";            // bool; the ledger's own estimate flag
    std::string value_source_col = "childValueSource";  // child_close_ex | wi_close | event_value | vendor_implied | unknown
    std::string status_col = "status";                  // ok | estimated | unresolved
    // Rights/warrant terms (docs/rights_warrants_design_2026-09-17.md). The three columns
    // are optional in the file: without them no right expires and nothing is exercised.
    std::string expiry_col = "expiryDate";
    std::string subscription_price_col = "subscriptionPriceCache";
    std::string exercise_ratio_col = "exerciseRatioCache";
    // Optional: the leg's value per parent share in cache units (P10 (3)(4) estimates). A stock
    // or rights leg whose child is not in the panel books it as P&L without a child position.
    std::string child_value_col = "childValuePerShareCache";
    // Optional: what the ledger estimated on a flagged row (ratio / date / value, joined by '+').
    // Carried into the unconfirmed detail of quote-priced legs (child_close:<reason>, cash:<reason>).
    std::string estimate_reason_col = "estimateReason";
    bool auto_exercise = false;                         // true is refused until implemented
    bool apply_dividends = true;
    bool apply_distributions = true;
    std::string unpriced_child = "pending";             // "pending" | "drop"
    bool stale_check = true;                            // refuse a ledger whose anchors disagree with the cache
    // Unconfirmed P&L on held positions (unresolved legs, default-return settlements,
    // estimates, dropped children): "warn" reports them, "strict" fails the run on the
    // first bar that has one (HTTP 400 with the first events listed).
    std::string unresolved_policy = "warn";             // "warn" | "strict"
};

// Optional cost modules (backtest.costs, docs/corporate_actions_engine_2026-09-17.md "costs").
// Borrow: an annual rate on the short book, accrued per calendar day between bars and
// taken from net P&L only. Rates come from an interval table (symbol, from, to, rate),
// else `default_rate`; with neither the cost is 0 and the run reports borrowApplied=false.
struct BorrowCostConfig {
    bool enabled = false;
    double default_rate = std::numeric_limits<double>::quiet_NaN(); // annual fraction; NaN = none
    std::string path;                    // empty = no table; .csv or parquet
    unsigned day_count = 360;
    std::string symbol_col = "symbol";
    std::string from_col = "from";       // first calendar day the rate applies (inclusive)
    std::string to_col = "to";           // last calendar day (inclusive); null = open-ended
    std::string rate_col = "rate";       // annual fraction
};

struct CostsConfig {
    BorrowCostConfig borrow;
    bool report_unapplied = true;        // say in the results when a cost was not applied
};

struct BacktestConfig {
    // Legacy first-dataset views retained for existing callers/configs.
    std::string parquet_path;
    std::string time_col;
    std::string symbol_col;
    std::vector<std::string> use_columns;
    std::string start_date;
    std::string end_date; // latest (or empty) selects the last nonblank close row on load
    double initial_capital = 1000000.0;
    double transaction_fee_rate = 0.0005;

    std::vector<DatasetConfig> datasets;
    UniverseConfig universe;
    std::string config_dir;
    unsigned warmup_bars = 0;
    unsigned bars_per_year = 365;
    int mode = 1; // existing close execution: 0=D0, 1=D1
    std::string combined_mode = "normalized";
    // Missing-close accounting (see docs/missing_data_validation_2026-09-11.md)
    std::string missing_close_policy = "carry";   // "carry" | "error" (error = legacy fail-fast)
    double default_delisting_return = 0.0;        // settlement return when no explicit return (sensitivity: -1.0)
    unsigned stale_quote_settle_bars = 0;         // >0: settle a frozen holding with the default return after N consecutive missing bars when no event exists; 0 = never
    EventsConfig events;
    CorporateActionsConfig corporate_actions;     // backtest.corporate_actions
    CostsConfig costs;                            // backtest.costs
    // Optional JSON file {panelSymbol: displayLabel} (e.g. tradingItemId -> ticker) served by /api/config.
    std::string symbol_labels_path;
    // Optional `base:` block (docs/neutralize_plan_2026-09-16.md 4.3): one price cache plus one
    // group-panel directory, expanded by from_yaml into the base_prices/base_groups datasets.
    // The two directories are kept verbatim so the API can report them later.
    std::string base_cache;
    std::string base_groups;
    bool base_thin = false; // base.cache/manifest.json declares layout thin-v1

    static BacktestConfig from_yaml(const std::string& yaml_path);
};

// Strict YYYY-MM-DD conversion; end dates can use the last millisecond of a day.
int64_t config_date_ms(const std::string& value);
