#pragma once
#include <vector>
#include <memory>
#include <cstdint>
#include <functional>
#include <string>

class BorrowCostModel;

enum class MissingClosePolicy { Carry, Error };
// A child that is in the panel but has no quote on its ex-date either waits for
// its first observed quote (Pending) or is abandoned as unpriced (Drop).
enum class UnpricedChildPolicy { Pending, Drop };
// What to do when a held position's P&L cannot be confirmed (an unresolved ledger
// leg, a settlement priced from the default return, an estimated value, a child the
// engine gave up pricing): report it (Warn) or fail the run on the first bar that has
// one (Strict), listing the first events in the error message.
enum class UnresolvedPolicy { Warn, Strict };

// Counts refer to affected nonzero bookkeeping weights, not share quantities.
struct MissingDataDiagnostics {
    unsigned missing_held_count = 0;
    unsigned blocked_order_count = 0;
    double frozen_gross = 0.0;
    double blocked_trade_weight = 0.0;
    unsigned resumed_holding_count = 0;
    unsigned retirement_count = 0; // every retirement, held or not, event or stale
    unsigned delisting_count = 0;  // held positions settled on this bar
    double delisting_pnl = 0.0;
    double settled_gross = 0.0;
    unsigned default_delisting_count = 0;
    double default_delisting_gross = 0.0;
    unsigned stale_settlement_count = 0;  // settled by a stale quote, not by an event
    unsigned missing_entry_count = 0;
    unsigned pending_delisting_entry_count = 0;
    unsigned post_end_quote_bars = 0;      // valid quotes still printing after the end date
    unsigned pending_settlement_count = 0; // last bar only: events that never stopped quoting
    unsigned successor_conversions = 0;    // settlements carried into a surviving security
    double converted_gross = 0.0;          // exposure handed to successors, not cashed out
    unsigned computed_settlement_count = 0; // returns derived from a successor quote and ratio
    unsigned unheld_settlement_count = 0;   // settled while the book held nothing
};

// One settled holding, recorded only when SimulatorConfig::collect_settlements is
// set. Every field is the value the accounting actually used on that bar.
enum class SettlementSource { Computed, Explicit, Default };

struct SettlementRecord {
    unsigned bar;
    unsigned asset;
    double exposure;            // weight carried into the settlement
    double settlement_return;
    SettlementSource source;
    double cash_share;          // per-weight cash proceeds, 0 without a cash leg
    double stock_share;         // per-weight successor proceeds, 0 without a transfer
    int32_t successor;          // -1 when nothing was carried forward
    double transferred;         // weight handed to the successor
};

// Corporate-action ledger accounting (docs/corporate_actions_engine_2026-09-17.md).
// Cash legs are realized on the ex-date bar against the parent's previous mark;
// stock legs create a child weight valued at the child's ex-date close. Counts
// refer to held parent weights, like the settlement counts above.
struct CorporateActionDiagnostics {
    unsigned dividend_count = 0;        // cash legs booked on held parents
    double dividend_pnl = 0.0;          // sum of w * cash / mark, sign of w
    unsigned distribution_count = 0;    // stock legs that reached a held parent
    double distribution_pnl = 0.0;      // value received: sum of created child weights
    unsigned child_positions_created = 0; // child weights added to the book this bar
    unsigned distribution_estimated = 0;  // stock legs booked (position or value-only) from a row the ledger flagged as an estimate
    unsigned distribution_pending = 0;  // stock legs still waiting for a first child quote
    unsigned distribution_unpriced = 0; // stock legs abandoned: child not in the panel
    unsigned distribution_value_only = 0; // stock legs whose child is not in the panel, booked as P&L from the
                                          // ledger's per-share value (P10 (2)(3)(4)); no child position
    unsigned legs_unresolved = 0;       // legs on held parents the ledger marked unresolved: status
                                        // unresolved, no cash amount, or no child. A marked leg that
                                        // still carries an amount is booked as usual and counted here.
    // Rights that lapsed (docs/rights_warrants_design_2026-09-17.md section 3): pending
    // legs closed at value 0 on the first bar after the expiry day, and held rights with
    // no quote there settled at -1 through the settlement path (no fee).
    unsigned rights_expired = 0;
    unsigned rights_expired_held = 0;
    double rights_expired_gross = 0.0;  // |weight| of the held rights settled at -1
    // Ledger legs dated on the bar their parent settles (events file): they are
    // booked against the pre-settlement exposure and mark, so the bar totals
    // (cash + ratio_successor * successor + ratio_child * child) / mark - 1. A cash
    // leg is left to the events row when that row already carries cash, and a stock
    // leg naming the successor itself is left to the transfer (count-only, no double
    // counting).
    unsigned legs_on_settlement_bar = 0;
    unsigned legs_deferred_to_events = 0;
    unsigned dividend_estimated = 0;    // cash legs booked from an amount the ledger flagged as an estimate
    // Unconfirmed P&L on this bar (docs/corporate_actions_engine_2026-09-17.md section 9):
    // the number of UnconfirmedRecord entries dated on the bar and the sum of |weight
    // before| over the distinct parents among them (one parent with two legs on the bar
    // counts its weight once). Every entry names a held position whose P&L the inputs
    // could not confirm; the accounting itself is unchanged by the recording.
    unsigned unconfirmed_count = 0;
    double unconfirmed_abs_weight = 0.0;
};

// One held position whose P&L this bar could not be confirmed. Always recorded (the
// summary of a run needs the list); the accounting is never changed by it.
//   UnresolvedLeg     a ledger leg the ledger marked unresolved (no cash amount, no
//                     child, no ratio, or status unresolved) on a held parent
//   DefaultSettlement a held position settled with default_delisting_return (an events
//                     row without deal terms or an explicit return, or a stale quote)
//   EstimateValue     a cash amount or a child value booked from a ledger estimate
//   PendingDropped    a stock leg on a held parent the engine gave up pricing (child not
//                     in the panel, no ratio, retired, drop policy) or still pending
//                     on the last bar
enum class UnconfirmedKind { UnresolvedLeg, DefaultSettlement, EstimateValue, PendingDropped };

struct UnconfirmedRecord {
    unsigned bar;               // the bar the record is dated on (ex-date, settlement bar, drop bar)
    unsigned asset;             // the held parent (or the settled security)
    double weight_before;       // weight carried into the bar (pre-settlement exposure on a settlement bar)
    UnconfirmedKind kind;
    std::string detail;         // short reason: no_cash_amount, no_child, no_deal_terms, ...
};

// One booked or abandoned stock leg, recorded only when
// SimulatorConfig::collect_distributions is set. `bar` is the bar the child
// weight was created on (the ex-date, or the first later quote for a pending leg).
enum class DistributionSource { ChildClose, LedgerValue, FirstQuote, Unpriced, Expired, LedgerValueCash };

struct DistributionRecord {
    unsigned bar;
    unsigned ex_bar;            // the ledger's ex-date bar
    unsigned parent;
    int32_t child;              // -1 when the child is not in the panel
    double parent_weight;       // parent weight on the ex-date
    double child_weight;        // weight created, 0 when unpriced; the P&L recognised for LedgerValueCash
    double cash_share;          // per-weight cash booked on the same bar for this parent
    DistributionSource source;
    bool estimate;              // the ledger flagged the leg (value, ratio or ex-date estimated), whatever priced it
    bool on_settlement_bar;     // booked against the parent's pre-settlement exposure on its settlement bar
    std::string value_source;   // the ledger's childValueSource, verbatim
    std::string event_type;     // the ledger's eventType, verbatim
};

// Borrowed views: valid only for the duration of accounting_row_callback.
// The last two price views contain bookkeeping marks; an event's final mark
// is its settlement value, and can be zero. Raw close is never filled or changed.
struct AccountingRowView {
    unsigned bar;
    bool trading_enabled;
    const float* close;
    const std::vector<double>& previous_weights;
    const std::vector<double>& desired_weights;
    const std::vector<double>& weights;
    const std::vector<double>& market_trades;
    const std::vector<double>& marks_before;
    const std::vector<double>& marks_after;
    const std::vector<double>& delisting_returns; // NaN=no event, includes explicit/default R
    const std::vector<double>& settled_gross;     // per-asset administratively settled exposure
    const std::vector<uint8_t>& retired;
    double gross_pnl;
    double fee;
    double net_pnl;
    double turnover;
    const MissingDataDiagnostics& diagnostics;
    const CorporateActionDiagnostics& corporate_actions;
    double borrow_cost;         // financing taken from net_pnl on this bar (0 without a model)
};

struct SimulatorConfig {
    using SignalRowCallback = std::function<void(unsigned di, const float* row, unsigned n)>;
    using AccountingRowCallback = std::function<void(const AccountingRowView&)>;

    unsigned T;
    unsigned N;
    double fee_rate;        // fraction (e.g., 0.0005)
    int mode;               // 0=D0, 1=D1
    bool collect_signals = false;  // run() stores the T×N raw signal matrix
    SignalRowCallback signal_row_callback{}; // run() observes raw rows before normalization
    unsigned sim_start_index = 0; // earlier bars warm modules/targets, with no positions or fees
    MissingClosePolicy missing_close_policy = MissingClosePolicy::Carry;
    double default_delisting_return = 0.0;
    unsigned stale_quote_settle_bars = 0; // >0: settle frozen holdings with default return after N missing bars when no event
    AccountingRowCallback accounting_row_callback{}; // opt-in for either execution path
    bool collect_settlements = false; // run() records every settled holding
    // Corporate-action ledger legs from DataPanel::corporate_actions. Both flags
    // default on; a panel without legs is unaffected either way.
    bool apply_dividends = true;      // book cash legs
    bool apply_distributions = true;  // book stock legs
    UnpricedChildPolicy unpriced_child = UnpricedChildPolicy::Pending;
    bool collect_distributions = false; // run() records every stock leg it resolved
    // Borrow financing of the short book (src/engine/costs.h); null = no cost, bit-identical.
    const BorrowCostModel* borrow = nullptr;
    // Unconfirmed P&L: Warn records it, Strict throws on the first bar that has any
    // (the message lists up to strict_report_limit records of that bar).
    UnresolvedPolicy unresolved_policy = UnresolvedPolicy::Warn;
    unsigned strict_report_limit = 10;
};

struct SimulatorOutput {
    std::vector<double> ret;
    std::vector<double> turnover;
    std::vector<double> max_weight;
    std::vector<double> long_weight;
    std::vector<double> short_weight;
    std::vector<int> position_count;
    std::vector<int64_t> timestamps;
    std::vector<std::vector<float>> signals;  // T×N raw signal (only when collect_signals=true)
    std::vector<MissingDataDiagnostics> missing_data; // T bars; last bar exposes terminal frozen holdings
    std::vector<SettlementRecord> settlements; // only when collect_settlements=true
    std::vector<CorporateActionDiagnostics> corporate_actions; // T bars; last bar exposes terminal pending legs
    std::vector<DistributionRecord> distributions; // only when collect_distributions=true
    std::vector<UnconfirmedRecord> unconfirmed;    // always; sorted by bar (see CorporateActionDiagnostics)
    std::vector<double> borrow_cost;      // T bars; financing subtracted from ret (net) on each bar
    std::vector<unsigned> borrow_unrated; // T bars; short names carried without any rate (charged 0)
};

class Module;
struct DataPanel;

// Normalize finite signals over the signal-date universe, excluding retired assets.
// Carry permits NaN quotes in intent and rejects a non-NaN unusable price;
// Error retains the original valid-close gate.
// Signal NaN still means target zero; this function never preserves old orders.
void normalize_signal_row(const DataPanel& data, unsigned di, const float* signal,
                          std::vector<double>& targets, MissingClosePolicy policy);

class Simulator {
public:
    Simulator(const DataPanel& data, const SimulatorConfig& config);
    SimulatorOutput run(const std::vector<Module*>& pipeline);
    SimulatorOutput run_from_signals(const std::vector<std::vector<float>>& signals);
private:
    const DataPanel& data_;
    SimulatorConfig config_;
};
