#include "simulator.h"
#include "../../include/module.h"
#include "../data/data_panel.h"
#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <string>

namespace {
constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

// A quote is observable when it is finite and positive. NaN means the venue
// printed nothing, which carry may freeze; any other unusable number (zero,
// negative, infinity) is corrupt data and is never carried by either policy.
enum QuoteState : uint8_t { kQuoteObserved = 0, kQuoteMissing = 1, kQuoteInvalid = 2 };

bool valid_close(float value) {
    return std::isfinite(value) && value > 0.0f;
}

QuoteState classify_quote(float value) {
    if (std::isnan(value)) return kQuoteMissing;
    return valid_close(value) ? kQuoteObserved : kQuoteInvalid;
}

std::string unusable_price_message(unsigned di, unsigned ii) {
    return "Invalid close price (not NaN) at bar " + std::to_string(di) +
           ", asset " + std::to_string(ii);
}

SimulatorOutput make_output(const DataPanel& data, unsigned end) {
    SimulatorOutput output;
    output.ret.assign(end, 0.0);
    output.gross_ret.assign(end, 0.0);
    output.execution_ret.assign(end, 0.0);
    output.turnover.assign(end, 0.0);
    output.max_weight.assign(end, 0.0);
    output.long_weight.assign(end, 0.0);
    output.short_weight.assign(end, 0.0);
    output.position_count.assign(end, 0);
    output.missing_data.assign(end, MissingDataDiagnostics{});
    output.timestamps.assign(data.timestamps.begin(), data.timestamps.begin() + std::min<std::size_t>(end, data.timestamps.size()));
    return output;
}

// One portfolio transition shared by generated and precomputed signals.
// Weights stay fixed-book notional. An observable quote is a finite positive
// close; while one is missing the weight and its valuation mark freeze instead
// of being valued, traded or liquidated at an invented price.
class PortfolioSimulation {
public:
    PortfolioSimulation(const DataPanel& data, const SimulatorConfig& config)
        : output(make_output(data, std::min(data.T, config.sim_end_index))), data_(data), config_(config),
          previous_weights_(data.N, 0.0),
          targets_(data.N, 0.0),
          carried_(data.N, 0.0),
          weights_(data.N, 0.0),
          marks_(data.N, kNaN),
          retired_(data.N, 0),
          quote_state_(data.N, kQuoteObserved),
          execution_prices_(data.N, kNaN),
          execution_available_(data.N, 0),
          missing_bars_(data.N, 0),
          pending_transfer_(data.N, 0.0),
          records_rows_(static_cast<bool>(config.accounting_row_callback)) {
        if (records_rows_) {
            marks_before_.assign(data.N, kNaN);
            market_trades_.assign(data.N, 0.0);
        }
        if (data.thin_layout) received_.assign(data.N, 0.0);
    }

    void advance(unsigned di, const float* signal) {
        const float* close = data_.row("close", di);
        // Warmup bars keep marks and intent current without holdings or costs.
        const bool trading = di >= config_.sim_start_index;
        MissingDataDiagnostics diagnostics;
        double pnl = 0.0;
        double turnover = 0.0;
        double execution_pnl = 0.0;

        if (records_rows_) {
            marks_before_.assign(marks_.begin(), marks_.end());
            std::fill(market_trades_.begin(), market_trades_.end(), 0.0);
        }

        if (data_.thin_layout)
            apply_moves(di, close, trading, pnl, diagnostics);
        else
            prepare_book(close);
        normalize_intent(di, signal);
        if (trading) {
            require_priced_quotes(di);
            require_tradable_quotes(di);
        }
        if (data_.thin_layout)
            value_holdings_thin(di, close, trading, pnl, diagnostics);
        else
            value_holdings(di, close, trading, pnl, diagnostics);
        apply_successor_transfers();

        if (trading) {
            execution_pnl = execute(di, close, diagnostics, turnover);
            pnl += execution_pnl;
            apply_received_moves();
            output.ret[di] = pnl;
            output.gross_ret[di] = pnl;
            output.execution_ret[di] = execution_pnl;
            output.turnover[di] = turnover;
            output.ret[di] -= turnover * config_.fee_rate;

            double max_weight = 0.0, long_weight = 0.0, short_weight = 0.0;
            int position_count = 0;
            for (double weight : weights_) {
                const double absolute_weight = std::fabs(weight);
                if (absolute_weight > max_weight) max_weight = absolute_weight;
                if (weight > 0.0) long_weight += weight;
                if (weight < 0.0) short_weight += -weight;
                if (absolute_weight > 1e-12) ++position_count;
            }
            output.max_weight[di] = max_weight;
            output.long_weight[di] = long_weight;
            output.short_weight[di] = short_weight;
            output.position_count[di] = position_count;
        } else {
            std::fill(weights_.begin(), weights_.end(), 0.0);
        }
        output.missing_data[di] = diagnostics;
        if (records_rows_)
            emit_row(di, close, trading, pnl, turnover, diagnostics, execution_pnl);

        previous_weights_.swap(weights_);
    }

    SimulatorOutput output;

private:
    const std::vector<double>& execution_intent() const {
        return targets_;
    }

    // Thin layout: move weights using the carried book at the start of the bar.
    // Ending sources book returns_total and retire before intent is normalized.
    void apply_moves(unsigned di, const float* close, bool trading, double& pnl,
                     MissingDataDiagnostics& diagnostics) {
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            pending_transfer_[ii] = 0.0;
            quote_state_[ii] = classify_quote(close[ii]);
            carried_[ii] = retired_[ii] ? 0.0 : previous_weights_[ii];
        }
        const auto& moves = data_.move_events;
        while (next_move_ < moves.size() && moves[next_move_].bar < di) ++next_move_;
        size_t end = next_move_;
        while (end < moves.size() && moves[end].bar == di) ++end;
        for (size_t at = next_move_; at < end; ++at) {
            const auto& move = moves[at];
            const double weight = carried_[move.source];   // bar-start: sources are zeroed only below
            const double amount = move.destination >= 0 ? weight * move.value_ratio : 0.0;
            if (move.destination >= 0 && trading && amount != 0.0) {
                if (move.source_ends) {
                    pending_transfer_[move.destination] += amount;
                    any_transfer_ = true;
                    ++diagnostics.successor_conversions;
                    diagnostics.converted_gross += std::fabs(amount);
                } else {
                    received_[move.destination] += amount;
                    any_received_ = true;
                }
            }
        }
        for (size_t at = next_move_; at < end; ++at) {
            const auto& move = moves[at];
            const unsigned source = move.source;
            if (!move.source_ends || retired_[source])
                continue;
            const double rt = data_.returns_total[static_cast<size_t>(di) * data_.N + source];
            if (trading && carried_[source] != 0.0 && std::isfinite(rt))
                pnl += carried_[source] * rt;
            ++diagnostics.retirement_count;
            retired_[source] = 1;
            any_retired_ = true;
            carried_[source] = 0.0;
            missing_bars_[source] = 0;
        }
        // Weight handed to a security that is gone (earlier or on this bar) would vanish.
        for (size_t at = next_move_; at < end; ++at)
            if (moves[at].destination >= 0 && retired_[moves[at].destination])
                throw std::runtime_error("Move to a retired destination at bar " + std::to_string(di) +
                                         ", asset " + std::to_string(moves[at].destination));
        next_move_ = end;
    }

    // Thin layout holding P&L: carried weight times returns_total, which already spans
    // any bars the quote was missing. Quotes decide whether a position freezes.
    void value_holdings_thin(unsigned di, const float* close, bool trading, double& pnl,
                             MissingDataDiagnostics& diagnostics) {
        const double* rt = data_.returns_total.data() + static_cast<size_t>(di) * data_.N;
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            if (retired_[ii]) {
                missing_bars_[ii] = 0;
                continue;
            }
            if (trading && carried_[ii] != 0.0 && std::isfinite(rt[ii]))
                pnl += carried_[ii] * rt[ii];
            if (quote_state_[ii] == kQuoteObserved) {
                if (trading && carried_[ii] != 0.0 && missing_bars_[ii] > 0)
                    ++diagnostics.resumed_holding_count;
                marks_[ii] = static_cast<double>(close[ii]);
                missing_bars_[ii] = 0;
            } else {
                ++missing_bars_[ii];
                if (trading && carried_[ii] != 0.0)
                    ++diagnostics.missing_held_count;
            }
        }
    }

    // Successor value arrives after holding revaluation and earns no source return twice.
    void apply_successor_transfers() {
        if (!any_transfer_)
            return;
        for (unsigned ii = 0; ii < data_.N; ++ii)
            if (pending_transfer_[ii] != 0.0)
                carried_[ii] += pending_transfer_[ii];
        any_transfer_ = false;
    }

    // Ordinary panels carry weights forward and classify quotes without events.
    void prepare_book(const float* close) {
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            quote_state_[ii] = classify_quote(close[ii]);
            carried_[ii] = previous_weights_[ii];
        }
    }

    // Distribution moves join after market execution and carry into the next bar.
    void apply_received_moves() {
        if (!any_received_) return;
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            weights_[ii] += received_[ii];
            received_[ii] = 0.0;
        }
        any_received_ = false;
    }

    // Retired securities leave the intent universe; the remaining rule is the
    // existing signal-date mask, and carry additionally admits missing quotes.
    void normalize_intent(unsigned di, const float* signal) {
        const float* source = signal;
        if (any_retired_) {
            masked_signal_.assign(signal, signal + data_.N);
            for (unsigned ii = 0; ii < data_.N; ++ii)
                if (retired_[ii])
                    masked_signal_[ii] = std::numeric_limits<float>::quiet_NaN();
            source = masked_signal_.data();
        }
        normalize_signal_row(data_, di, source, targets_, config_.missing_close_policy,
                             config_.universe_mask);
    }

    // A corrupt price is never frozen or carried. Under carry the intent gate in
    // normalize_signal_row rejects it first; this covers held and executing names.
    void require_priced_quotes(unsigned di) {
        const auto& desired = execution_intent();
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            if (retired_[ii] || quote_state_[ii] != kQuoteInvalid)
                continue;
            if (carried_[ii] != 0.0 || desired[ii] != 0.0)
                throw std::runtime_error(unusable_price_message(di, ii));
        }
    }

    // Error policy rejects held or executing positions without valid closes.
    void require_tradable_quotes(unsigned di) {
        if (config_.missing_close_policy != MissingClosePolicy::Error)
            return;
        const auto& desired = execution_intent();
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            if (retired_[ii] || quote_state_[ii] == kQuoteObserved)
                continue;
            if (carried_[ii] != 0.0 || desired[ii] != 0.0)
                throw std::runtime_error("Cannot value or execute position: invalid close at bar " +
                                         std::to_string(di) + ", asset " + std::to_string(ii));
        }
    }

    // Holding P&L from the last observed mark to this quote. A missing quote
    // contributes no return and leaves the mark where it was last observed.
    void value_holdings(unsigned di, const float* close, bool trading, double& pnl,
                        MissingDataDiagnostics& diagnostics) {
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            if (retired_[ii]) {
                missing_bars_[ii] = 0;
                continue;
            }
            if (carried_[ii] != 0.0 && !std::isfinite(marks_[ii]))
                throw std::runtime_error("Cannot value position: no observed mark at bar " +
                                         std::to_string(di) + ", asset " + std::to_string(ii));
            if (quote_state_[ii] == kQuoteObserved) {
                if (trading && carried_[ii] != 0.0) {
                    pnl += carried_[ii] * (static_cast<double>(close[ii]) / marks_[ii] - 1.0);
                    if (missing_bars_[ii] > 0)
                        ++diagnostics.resumed_holding_count;
                }
                marks_[ii] = static_cast<double>(close[ii]);
                missing_bars_[ii] = 0;
            } else {
                ++missing_bars_[ii];
                if (trading && carried_[ii] != 0.0)
                    ++diagnostics.missing_held_count;
            }
        }
    }

    // OHLC4/VWAP is a fixed-book trading-return convention, matching the team's
    // StatsHandler: market_trade * (close / fill - 1). It does not change targets
    // into share quantities or add price-drift rebalancing to the turnover.
    void prepare_execution_prices(unsigned di, const float* close) {
        const bool vwap = config_.execution_price == ExecutionPrice::Vwap;
        const char* label = vwap ? "VWAP" : "OHLC4";
        const float* fill_source = data_.row(vwap ? "vwap" : "open", di);
        const float* high = data_.row("high", di);
        const float* low = data_.row("low", di);
        const auto& desired = execution_intent();
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            execution_prices_[ii] = kNaN;
            execution_available_[ii] = 0;
            if (retired_[ii]) continue;
            if (quote_state_[ii] == kQuoteObserved) {
                bool missing = false, corrupt = false, inconsistent = false;
                for (float value : {fill_source[ii], high[ii], low[ii]}) {
                    missing = missing || std::isnan(value);
                    corrupt = corrupt || (!std::isnan(value) && !valid_close(value));
                }
                if (std::isfinite(high[ii]) && std::isfinite(low[ii]))
                    inconsistent = high[ii] < low[ii] || close[ii] < low[ii] || close[ii] > high[ii];
                if (!missing)
                    inconsistent = inconsistent || fill_source[ii] < low[ii] || fill_source[ii] > high[ii];
                if (corrupt && (carried_[ii] != 0.0 || desired[ii] != 0.0))
                    throw std::runtime_error(std::string("Invalid ") + label + " execution price at bar " + std::to_string(di) +
                                             ", asset " + std::to_string(ii));
                // Finite but inconsistent OHLC cannot provide a trusted fill.
                // Use the same unfilled-order policy as a missing execution quote.
                if (!missing && !corrupt && !inconsistent) {
                    execution_prices_[ii] = vwap ? static_cast<double>(fill_source[ii])
                        : (static_cast<double>(fill_source[ii]) + high[ii] + low[ii] + close[ii]) / 4.0;
                    execution_available_[ii] = 1;
                }
            }
            if (!execution_available_[ii] && config_.missing_close_policy == MissingClosePolicy::Error &&
                desired[ii] != carried_[ii])
                throw std::runtime_error(std::string("Cannot execute: missing or inconsistent ") + label + " price at bar " + std::to_string(di) +
                                         ", asset " + std::to_string(ii));
        }
    }

    // Only securities with a usable execution quote trade. Frozen weights reduce
    // the other targets' budget; corporate receipts themselves are never trades.
    double execute(unsigned di, const float* close, MissingDataDiagnostics& diagnostics, double& turnover) {
        const auto& desired = execution_intent();
        const bool intraday_fill = config_.execution_price != ExecutionPrice::Close;
        if (intraday_fill) prepare_execution_prices(di, close);
        const auto can_trade = [&](unsigned ii) {
            return intraday_fill ? execution_available_[ii] != 0 : quote_state_[ii] == kQuoteObserved;
        };
        // Frozen budget and the Error gate see the post-transfer book, so
        // successor stock landing on an unfillable name is reserved or rejected.
        diagnostics.frozen_gross = 0.0;
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            if (retired_[ii] || can_trade(ii)) continue;
            if (!intraday_fill && config_.missing_close_policy == MissingClosePolicy::Error && desired[ii] != carried_[ii])
                throw std::runtime_error("Cannot execute: missing or invalid close price at bar " + std::to_string(di) +
                                         ", asset " + std::to_string(ii));
            diagnostics.frozen_gross += std::fabs(carried_[ii]);
        }
        double execution_pnl = 0.0;
        double scale = 1.0;
        if (diagnostics.frozen_gross > 0.0) {
            double tradable_gross = 0.0;
            for (unsigned ii = 0; ii < data_.N; ++ii)
                if (!retired_[ii] && can_trade(ii))
                    tradable_gross += std::fabs(desired[ii]);
            const double budget = std::max(0.0, 1.0 - diagnostics.frozen_gross);
            if (tradable_gross > budget)
                scale = budget / tradable_gross;
        }
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            const double target = desired[ii];
            if (retired_[ii]) {
                weights_[ii] = 0.0;
            } else if (can_trade(ii)) {
                weights_[ii] = target * scale;
            } else {
                weights_[ii] = carried_[ii];
                if (target != carried_[ii]) {
                    ++diagnostics.blocked_order_count;
                    diagnostics.blocked_trade_weight += std::fabs(target - carried_[ii]);
                    if (carried_[ii] == 0.0)
                        ++diagnostics.missing_entry_count;
                }
            }
            const double trade = weights_[ii] - carried_[ii];
            turnover += std::fabs(trade);
            if (intraday_fill && trade != 0.0)
                execution_pnl += trade * (static_cast<double>(close[ii]) / execution_prices_[ii] - 1.0);
            if (records_rows_)
                market_trades_[ii] = trade;
        }
        return execution_pnl;
    }

    void emit_row(unsigned di, const float* close, bool trading, double pnl, double turnover,
                  const MissingDataDiagnostics& diagnostics, double execution_pnl) {
        const double fee = turnover * config_.fee_rate;
        const AccountingRowView view{
            di, trading, close, previous_weights_, execution_intent(), weights_,
            market_trades_, marks_before_, marks_, retired_,
            pnl, fee, pnl - fee, turnover, diagnostics, execution_pnl};
        config_.accounting_row_callback(view);
    }

    const DataPanel& data_;
    const SimulatorConfig& config_;
    std::vector<double> previous_weights_;  // book carried into the bar
    std::vector<double> targets_;
    std::vector<double> carried_;           // book after moves, before execution
    std::vector<double> weights_;
    std::vector<double> marks_;             // last observed close; NaN before the first quote
    std::vector<uint8_t> retired_;
    std::vector<uint8_t> quote_state_;
    std::vector<double> execution_prices_;
    std::vector<uint8_t> execution_available_;
    std::vector<unsigned> missing_bars_;    // consecutive bars without an observable quote
    std::vector<double> pending_transfer_;  // successor exposure waiting for this bar's execution
    std::vector<double> received_;          // distribution moves joining after execution
    std::vector<float> masked_signal_;
    std::vector<double> marks_before_;
    std::vector<double> market_trades_;
    size_t next_move_ = 0;           // cursor into data_.move_events, sorted by bar
    bool records_rows_ = false;
    bool any_retired_ = false;
    bool any_transfer_ = false;
    bool any_received_ = false;
};
} // namespace

void normalize_signal_row(const DataPanel& data, unsigned di, const float* signal,
                          std::vector<double>& targets, MissingClosePolicy policy,
                          const std::vector<uint8_t>* universe) {
    const float* close = data.row("close", di);
    const auto& selected = universe ? *universe : data.universe_mask;
    const auto* mask = selected.empty()
        ? nullptr : selected.data() + static_cast<size_t>(di) * data.N;
    const bool require_quote = policy == MissingClosePolicy::Error;
    double absolute_signal_sum = 0.0;
    for (unsigned ii = 0; ii < data.N; ++ii) {
        if ((!mask || mask[ii]) && (!require_quote || valid_close(close[ii])) &&
            std::isfinite(signal[ii])) {
            // Carry admits a quoteless name, but only when the venue printed
            // nothing. A corrupt price cannot be normalized against.
            if (!require_quote && classify_quote(close[ii]) == kQuoteInvalid)
                throw std::runtime_error(unusable_price_message(di, ii));
            absolute_signal_sum += std::fabs(static_cast<double>(signal[ii]));
        }
    }
    targets.assign(data.N, 0.0);
    if (absolute_signal_sum > 1e-12) {
        for (unsigned ii = 0; ii < data.N; ++ii) {
            if ((!mask || mask[ii]) && (!require_quote || valid_close(close[ii])) &&
                std::isfinite(signal[ii]))
                targets[ii] = static_cast<double>(signal[ii]) / absolute_signal_sum;
        }
    }
}

Simulator::Simulator(const DataPanel& data, const SimulatorConfig& config)
    : data_(data), config_(config)
{
    if (config_.T != data_.T || config_.N != data_.N || config_.N == 0)
        throw std::invalid_argument("Simulator dimensions must match a nonempty asset panel");
    if (!std::isfinite(config_.fee_rate) || config_.fee_rate < 0.0)
        throw std::invalid_argument("Simulator fee_rate must be finite and nonnegative");
    if (config_.sim_start_index > data_.T)
        throw std::invalid_argument("Simulator start index exceeds the data panel");
    if (config_.sim_end_index != std::numeric_limits<unsigned>::max() && config_.sim_end_index > data_.T)
        throw std::invalid_argument("Simulator end index exceeds the data panel");
    if (config_.sim_start_index > std::min(data_.T, config_.sim_end_index))
        throw std::invalid_argument("Simulator start index exceeds the end index");
    if (!data_.universe_mask.empty() &&
        data_.universe_mask.size() != static_cast<size_t>(data_.T) * data_.N)
        throw std::invalid_argument("Universe mask dimensions must match the data panel");
    if (config_.universe_mask && !config_.universe_mask->empty() &&
        config_.universe_mask->size() != static_cast<size_t>(data_.T) * data_.N)
        throw std::invalid_argument("Universe mask dimensions must match the data panel");
    if (config_.execution_price != ExecutionPrice::Close && config_.execution_price != ExecutionPrice::Ohlc4 &&
        config_.execution_price != ExecutionPrice::Vwap)
        throw std::invalid_argument("Unsupported execution price");
    if (config_.execution_price == ExecutionPrice::Ohlc4)
        for (const char* field : {"open", "high", "low"})
            if (!data_.has_column(field))
                throw std::invalid_argument(std::string("OHLC4 execution requires data field '") + field + "'");
    if (config_.execution_price == ExecutionPrice::Vwap)
        for (const char* field : {"vwap", "high", "low"})
            if (!data_.has_column(field))
                throw std::invalid_argument(std::string("VWAP execution requires data field '") + field + "'");
    if (data_.thin_layout) {
        if (data_.returns_total.size() != static_cast<size_t>(data_.T) * data_.N)
            throw std::invalid_argument("Thin returns_total must align with the data panel");
        unsigned last_move_bar = 0;
        for (const auto& move : data_.move_events) {
            if (move.bar >= data_.T || move.source >= data_.N || move.destination < -1 ||
                move.destination >= static_cast<int32_t>(data_.N) ||
                move.destination == static_cast<int32_t>(move.source) ||
                (move.destination >= 0 && !std::isfinite(move.value_ratio)))
                throw std::invalid_argument("Thin move events must reference the asset panel with a finite ratio");
            if (move.bar < last_move_bar)
                throw std::invalid_argument("Thin move events must be sorted by bar");
            last_move_bar = move.bar;
        }
    }
}

SimulatorOutput Simulator::run(const std::vector<Module*>& pipeline) {
    PortfolioSimulation simulation(data_, config_);
    std::vector<float> signal_buffer(data_.N, 0.0f);
    SignalVector signal{signal_buffer.data(), data_.N};
    if (config_.collect_signals)
        simulation.output.signals.resize(simulation.output.ret.size());

    for (Module* module : pipeline) {
        module->numAssets = data_.N;
        module->init();
    }
    for (unsigned di = 0; di < simulation.output.ret.size(); ++di) {
        if (!pipeline.empty() && !pipeline[0]->skips_nan_init())
            signal.fill_nan();
        for (Module* module : pipeline)
            module->calculate(di, signal);

        if (config_.collect_signals)
            simulation.output.signals[di].assign(signal_buffer.begin(), signal_buffer.end());
        if (config_.signal_row_callback)
            config_.signal_row_callback(di, signal_buffer.data(), data_.N);
        simulation.advance(di, signal_buffer.data());
    }
    return std::move(simulation.output);
}

void Simulator::run_signal_rows(const std::vector<Module*>& pipeline,
                               const SimulatorConfig::SignalRowCallback& callback) {
    std::vector<float> buffer(data_.N, 0.0f);
    SignalVector signal{buffer.data(), data_.N};
    for (Module* module : pipeline) {
        module->numAssets = data_.N;
        module->init();
    }
    for (unsigned di = 0; di < std::min(data_.T, config_.sim_end_index); ++di) {
        if (!pipeline.empty() && !pipeline[0]->skips_nan_init()) signal.fill_nan();
        for (Module* module : pipeline) module->calculate(di, signal);
        if (callback) callback(di, buffer.data(), data_.N);
    }
}

SimulatorOutput Simulator::run_from_signals(const std::vector<std::vector<float>>& signals) {
    if (signals.size() != config_.T ||
        std::any_of(signals.begin(), signals.end(), [this](const auto& row) {
            return row.size() != config_.N;
        }))
        throw std::invalid_argument("Signal matrix dimensions must match the data panel");

    PortfolioSimulation simulation(data_, config_);
    for (unsigned di = 0; di < simulation.output.ret.size(); ++di)
        simulation.advance(di, signals[di].data());
    return std::move(simulation.output);
}
