#pragma once
#include <vector>
#include <memory>
#include <cstdint>
#include <functional>
#include <string>

class BorrowCostModel;

enum class MissingClosePolicy { Carry, Error };
// Counts refer to affected nonzero bookkeeping weights, not share quantities.
struct MissingDataDiagnostics {
    unsigned missing_held_count = 0;
    unsigned blocked_order_count = 0;
    double frozen_gross = 0.0;
    double blocked_trade_weight = 0.0;
    unsigned resumed_holding_count = 0;
    unsigned retirement_count = 0; // thin source ending, held or not
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
    AccountingRowCallback accounting_row_callback{}; // opt-in for either execution path
    // Borrow financing of the short book (src/engine/costs.h); null = no cost, bit-identical.
    const BorrowCostModel* borrow = nullptr;
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
