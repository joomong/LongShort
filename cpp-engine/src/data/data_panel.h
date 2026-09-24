#pragma once

#include <atomic>
#include <cstdint>
#include <functional>
#include <limits>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

struct BacktestConfig;

struct LazyColumn {
    explicit LazyColumn(std::function<std::vector<float>()> source) : loader(std::move(source)) {}
    const std::vector<float>& get() const;
    bool loaded() const { return ready.load(std::memory_order_acquire); }
    size_t loaded_bytes() const;
private:
    mutable std::once_flag once;
    mutable std::function<std::vector<float>()> loader;
    mutable std::vector<float> values;
    mutable std::atomic<bool> ready{false};
};

struct DataPanel {
    unsigned T = 0;
    unsigned N = 0;
    unsigned trade_start_index = 0; // earlier rows are available for warmup only
    std::vector<int64_t> timestamps;    // exact epoch_ms, UTC sorted ascending
    std::vector<std::string> symbols;   // lexically sorted stable IDs
    std::unordered_map<std::string, std::vector<float>> arrays;
    // Populated before publication; each shared column loads exactly once. Aliases share values.
    std::unordered_map<std::string, std::shared_ptr<LazyColumn>> lazy_columns;
    std::vector<uint8_t> universe_mask; // T*N membership on the signal date; empty means all
    // Delisting/settlement events aligned with `symbols` (empty vectors = no event data loaded).
    // settlement_bar: first panel bar whose timestamp is strictly after the security end date.
    //   The settlement opens on that bar and closes on the first bar without an observed quote
    //   (quotes that continue past the end date are still valued and reported as postEndDateQuoteBars).
    //   -1 = no event inside the panel; 0 = ended before the panel starts.
    // settlement_return: explicit total return from the last observed mark to settlement; NaN = use default.
    // settlement_successor: index into `symbols` of the security that carries the
    //   holding forward (merger/rename); -1 = cash settlement or successor outside the panel.
    // settlement_successor_ratio: successor shares received per old share; NaN = unknown.
    // settlement_cash: cash received per old share; NaN = no cash leg.
    std::vector<int32_t> settlement_bar;
    std::vector<float> settlement_return;
    std::vector<int32_t> settlement_successor;
    std::vector<double> settlement_successor_ratio;
    std::vector<double> settlement_cash;
    // Structure of the consideration, independent of whether this panel can price
    // it: a declared stock leg that cannot be valued here blocks the computation
    // instead of turning the deal into a cash-only one.
    std::vector<uint8_t> settlement_has_stock_leg;
    std::vector<uint8_t> settlement_has_cash_leg;
    std::vector<uint8_t> settlement_successor_named; // the file named a successor, panel or not
    // events_rows_matched: panel symbols that found a row in the events file.
    // The three guard counts below partition exactly that set, one bucket per row.
    std::size_t events_rows_matched = 0;
    std::size_t events_price_checks = 0;            // anchors re-read from this panel
    std::size_t events_price_checks_out_of_range = 0; // this panel never settles the event
    std::size_t events_price_checks_not_evaluable = 0; // no cache-unit inputs to verify

    // Corporate-action ledger legs (docs/corporate_actions_engine_2026-09-17.md), one per
    // cash/stock/rights ledger row whose ex-date is a bar of this panel, sorted by bar then
    // parent. Amounts are in the price cache's share basis. Rows the ledger marked unresolved
    // are scheduled too, so the engine can count them on held parents: one without a cash
    // amount or a child books nothing, one that still carries an amount is booked as usual.
    // `child` is the child's panel index or -1 when it is not loaded (the leg is then
    // reported as unpriced).
    struct CorporateActionLeg {
        unsigned bar = 0;
        unsigned parent = 0;
        int32_t child = -1;
        bool stock_leg = false;   // stock or rights row; false = cash row
        bool rights = false;      // rights row (E3 adds expiry; priced like a stock leg)
        bool estimate = false;    // the ledger flagged the value as an estimate: a stock row's child value
                                  // (P10 (3)(4)) or a cash row's amount (isEstimate / status estimated)
        bool ledger_value_usable = false; // child_close may price a child without a panel quote
        bool unresolved = false;  // status unresolved, or no cash amount / no child; counted, and booked when an amount exists
        bool child_named = false; // the row named a child id (in the panel or not); false = marker row without a child
        double cash = std::numeric_limits<double>::quiet_NaN();        // per parent share
        double child_ratio = std::numeric_limits<double>::quiet_NaN(); // child shares per parent share
        double child_close = std::numeric_limits<double>::quiet_NaN(); // ledger child close on the ex-date
        // Rights terms. expiry_bar is the first panel bar after the expiry day (T when the
        // expiry lies beyond the panel); -1 = no expiry known. The price and ratio are
        // carried for the exercise design and are not used by the accounting yet.
        int32_t expiry_bar = -1;
        double subscription_price = std::numeric_limits<double>::quiet_NaN();
        double exercise_ratio = std::numeric_limits<double>::quiet_NaN();
        // Value of the leg per parent share (cache units) from the ledger's estimate tiers; NaN
        // without the optional column. Booked as P&L when the child is not in the panel.
        double child_value = std::numeric_limits<double>::quiet_NaN();
        std::string event_type;
        std::string value_source; // the ledger's childValueSource, verbatim
        std::string estimate_reason; // the export's estimateReason (ratio / date / value ...), empty without the column
    };
    std::vector<CorporateActionLeg> corporate_actions; // empty = no ledger legs inside the panel
    // Ledger load report for /api/config. rows counts every file row; matched the rows
    // whose parent is a panel symbol; quantity the matched split-type rows (nothing to book
    // in a split-adjusted panel); legs the matched value rows scheduled on a panel bar;
    // out_of_range the matched rows outside the panel's dates. The two guard counts
    // partition `legs`: every scheduled leg is either checked against the cache or has
    // no anchor to check.
    bool corporate_actions_loaded = false;
    std::size_t corporate_action_rows = 0;
    std::size_t corporate_action_rows_matched = 0;
    std::size_t corporate_action_rows_quantity = 0;
    std::size_t corporate_action_rows_unresolved = 0;   // matched rows the ledger marked unresolved
    std::size_t corporate_action_legs = 0;
    std::size_t corporate_action_legs_estimated = 0;    // scheduled legs (cash or stock) flagged as estimates
    std::size_t corporate_action_legs_unbookable = 0;   // scheduled legs with no cash amount / no child (marker rows):
                                                        // visible to the accounting, never booked
    std::size_t corporate_action_legs_value_only = 0;   // stock/rights legs whose child is not in the panel but that carry a
                                                        // usable ledger value: booked as P&L, no child position
    std::size_t corporate_action_legs_rights = 0;       // scheduled rights legs
    bool corporate_action_rights_terms_loaded = false;  // the expiry column was present in the file
    std::size_t corporate_action_rights_without_expiry = 0; // scheduled rights legs with no expiry date
    std::size_t corporate_action_legs_after_parent_end = 0; // scheduled inside the parent's settlement window
                                                            // (on or after settlement_bar): booked only while
                                                            // the parent still quotes and is held there
    std::size_t corporate_action_out_of_range = 0;
    std::size_t corporate_action_unpriced_children = 0; // scheduled stock legs whose child is not loaded
    std::size_t corporate_action_price_checks = 0;
    std::size_t corporate_action_price_checks_not_evaluable = 0;

    // Thin cache (docs/backtest_cache_simplification_plan_2026-09-23.md §3, §6). The loader
    // sets these when base.cache is a thin-v1 layout; the simulator then books holding P&L
    // as carried weight x returns_total and applies move_events, and nothing else.
    struct MoveEvent {
        unsigned bar = 0;          // panel bar of the event date
        unsigned source = 0;       // panel index of sourceId
        int32_t destination = -1;  // panel index of destinationId; -1 = no stock destination
        double value_ratio = std::numeric_limits<double>::quiet_NaN(); // destination value / source previous value
        bool source_ends = false;  // the source position ends on this bar
    };
    bool thin_layout = false;
    std::vector<double> returns_total;  // T*N row-major; NaN = no row (no P&L on that bar)
    std::vector<MoveEvent> move_events; // sorted by bar, then source

    float& at(const std::string& col, unsigned t, unsigned n);
    const float& at(const std::string& col, unsigned t, unsigned n) const;
    const float* row(const std::string& col, unsigned t) const;
    bool has_column(const std::string& col) const;
    std::vector<std::string> column_names() const;
    size_t loaded_field_count() const;
    size_t loaded_bytes() const;

    static DataPanel load(const BacktestConfig& config,
                          const std::vector<std::string>& selected_fields = {});
    static DataPanel load(const std::string& parquet_path,
                          const std::vector<std::string>& columns,
                          const std::string& time_col,
                          const std::string& symbol_col);
};
