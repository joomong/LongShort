#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>
#include <memory>
#include <string>
#include <vector>

enum class MissingClosePolicy { Carry, Error };
enum class ExecutionPrice { Close, Ohlc4, Vwap };
// Counts refer to affected nonzero bookkeeping weights, not share quantities.
struct MissingDataDiagnostics {
    unsigned missing_held_count = 0;
    unsigned blocked_order_count = 0;
    double frozen_gross = 0.0;
    double blocked_trade_weight = 0.0;
    unsigned resumed_holding_count = 0;
    unsigned retirement_count = 0; // thin source ending on a trading bar, held or not
    unsigned missing_entry_count = 0;
    unsigned successor_conversions = 0;    // ending thin moves carried into a destination
    double converted_gross = 0.0;          // exposure handed to destinations
};

// Borrowed views: valid only for the duration of accounting_row_callback.
// Marks are the last observed quotes; raw close is never filled or changed.
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
    const std::vector<uint8_t>& retired;
    double gross_pnl;
    double fee;
    double net_pnl;
    double turnover;
    const MissingDataDiagnostics& diagnostics;
    double execution_pnl = 0.0; // actual market trades * (close / execution price - 1)
};

struct SimulatorConfig {
    using SignalRowCallback = std::function<void(unsigned di, const float* row, unsigned n)>;
    using AccountingRowCallback = std::function<void(const AccountingRowView&)>;

    unsigned T;
    unsigned N;
    double fee_rate;        // fraction (e.g., 0.0005)
    bool collect_signals = false;  // run() stores the T×N raw signal matrix
    SignalRowCallback signal_row_callback{}; // run() observes raw rows before normalization
    unsigned sim_start_index = 0; // first trading bar; computation starts at history_start()
    MissingClosePolicy missing_close_policy = MissingClosePolicy::Carry;
    AccountingRowCallback accounting_row_callback{}; // opt-in for either execution path
    // Run-selected signal-date mask (universe.names); null = data.universe_mask.
    const std::vector<uint8_t>* universe_mask = nullptr;
    ExecutionPrice execution_price = ExecutionPrice::Close;
    unsigned sim_end_index = std::numeric_limits<unsigned>::max(); // exclusive; default = panel end
    unsigned module_threads = 1; // includes caller; outer alpha/member workers use one
    // First bar computed: one bar before sim_start_index sets intent and marks without trading.
    unsigned history_start() const { return sim_start_index > 0 ? sim_start_index - 1 : 0; }
};

struct SimulatorOutput {
    std::vector<double> ret;        // net: gross pnl - turnover * fee_rate
    std::vector<double> gross_ret;  // gross pnl before fees; performance metrics use this
    std::vector<double> execution_ret; // trading-price contribution, included in gross_ret
    std::vector<double> turnover;
    std::vector<double> max_weight;
    std::vector<double> long_weight;
    std::vector<double> short_weight;
    std::vector<int> position_count;
    std::vector<int64_t> timestamps;
    std::vector<std::vector<float>> signals;  // T rows of raw signal, empty before history_start (only when collect_signals=true)
    std::vector<MissingDataDiagnostics> missing_data; // T bars; last bar exposes terminal frozen holdings
};

class Module;
struct DataPanel;

// Normalize finite signals over the signal-date universe, excluding retired assets.
// Carry permits NaN quotes in intent and rejects a non-NaN unusable price;
// Error retains the original valid-close gate.
// Signal NaN still means target zero; this function never preserves old orders.
void normalize_signal_row(const DataPanel& data, unsigned di, const float* signal,
                          std::vector<double>& targets, MissingClosePolicy policy,
                          const std::vector<uint8_t>* universe = nullptr);

class Simulator {
public:
    Simulator(const DataPanel& data, const SimulatorConfig& config);
    SimulatorOutput run(const std::vector<Module*>& pipeline);
    // Generate the same raw rows without running an individual portfolio.
    void run_signal_rows(const std::vector<Module*>& pipeline,
                         const SimulatorConfig::SignalRowCallback& callback);
    SimulatorOutput run_from_signals(const std::vector<std::vector<float>>& signals);
private:
    const DataPanel& data_;
    SimulatorConfig config_;
};

constexpr unsigned BARS_PER_YEAR = 365;

struct SummaryMetrics {
    double pnl = 0;
    double ann_ret = 0;
    double sharpe = 0;
    double mdd = 0;
    double turnover = 0;
    double avg_position_count = 0; // mean number of held symbols; UI avgBet
    double max_weight = 0;
    int active_bar_count = 0;      // bars with at least one held symbol
    double win_rate = 0;
    double margin = 0;
    double long_ratio = 0;
    double short_ratio = 0;
    double avg_max_bet = 0;        // mean of per-bar maximum absolute weight
    double min_position_count = 0; // minimum number of held symbols; UI minBet
    double quality = 0;            // sharpe * sqrt(abs(1-maxexp)) * (1-max_weight)
    double sharpe400 = 0;
    double sharpe80 = 0;
    double minsoc = 0;             // minimum gross exposure
};

// Inputs are pre-sliced; all return bars are processed.
SummaryMetrics compute_summary(
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& position_count,
    unsigned bars_per_year = BARS_PER_YEAR
);

SummaryMetrics compute_summary_range(
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& position_count,
    size_t start,
    size_t end,
    unsigned bars_per_year = BARS_PER_YEAR
);

struct YearlyMetrics {
    int year;
    double pnl_pct;
    double ann_ret;
    double sharpe;
    double max_dd;
    double win_lose;
    double daily_tvr;
    double margin;
    double long_sm;
    double short_sm;
    double ir = 0.0; // mean daily gross return / sample stddev, without annualization
    double fitness = 0.0; // Sharpe * sqrt(abs(full-book annual return) / max(daily_tvr, 0.125))
};

std::vector<YearlyMetrics> compute_yearly(
    const std::vector<int64_t>& timestamps,  // epoch_ms
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    unsigned bars_per_year = 365,
    std::size_t start_index = 0
);

struct TimeseriesOutput {
    std::vector<int64_t> timestamps;
    std::vector<double> cum_pnl;
    std::vector<double> cum_pnl_gross;
    std::vector<double> turnover_pct;
    std::vector<int> position_count;
    std::vector<double> max_weight_pct;
    std::vector<double> drawdown;
    // Missing-close accounting, empty unless the caller supplies the series.
    std::vector<double> frozen_gross;   // held exposure with no quote on the bar
};

TimeseriesOutput compute_timeseries(
    const std::vector<int64_t>& timestamps,
    const std::vector<double>& net_ret,
    const std::vector<double>& gross_ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<int>& position_count,
    std::size_t start_index = 0,
    const std::vector<double>* frozen_gross = nullptr
);

struct FullMetricsResult {
    SummaryMetrics is_summary;
    SummaryMetrics os_summary;
    std::vector<YearlyMetrics> yearly;       // full period, single table (STAR)
    TimeseriesOutput timeseries;             // full period, single series (STAR)
    size_t is_start_index;                   // index into timeseries where IS starts
    size_t os_start_index;                   // index into timeseries where OS starts
};

// Timeseries/yearly cover [start_index, end); IS/OS summary ranges are clamped
// to that trading span. The default includes the first trading bar.
// is_timestamp_ms: first bar of IS metrics period
// os_timestamp_ms: first bar of OS period
// Performance metrics (summary, yearly, drawdown) use gross_ret; net_ret only
// feeds the net cumulative PnL series.
FullMetricsResult compute_full_metrics(
    const std::vector<int64_t>& timestamps,
    const std::vector<double>& net_ret,
    const std::vector<double>& gross_ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& position_count,
    int64_t is_timestamp_ms,
    int64_t os_timestamp_ms,
    unsigned bars_per_year = 365,
    std::size_t start_index = 0,
    const std::vector<double>* frozen_gross = nullptr
);

struct PerformanceSeries {
    std::vector<int64_t> timestamps;
    std::vector<double> cumulative_net, cumulative_gross, drawdown, turnover, max_weight, frozen_gross;
    std::vector<int> position_count;
    std::size_t os_start_index = 0;
};

PerformanceSeries compute_performance_series(const FullMetricsResult& metrics);
