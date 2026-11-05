#include "simulator_engine.h"
#include "module.h"
#include "simulation_data.h"

#include <algorithm>
#include <cmath>
#include <ctime>
#include <limits>
#include <map>
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
        if (data.thin_layout) {
            received_.assign(data.N, 0.0);
            // A source that ended before computation starts stays out of the book and the intent.
            const auto& moves = data.move_events;
            for (; next_move_ < moves.size() && moves[next_move_].bar < config.history_start(); ++next_move_)
                if (moves[next_move_].source_ends) {
                    retired_[moves[next_move_].source] = 1;
                    any_retired_ = true;
                }
        }
    }

    void advance(unsigned di, const float* signal) {
        const float* close = data_.row("close", di);
        // The bar before simStart sets marks and intent without holdings or costs.
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
            if (trading) ++diagnostics.retirement_count;
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
    AssetParallelScope module_workers(config_.module_threads);
    PortfolioSimulation simulation(data_, config_);
    std::vector<float> signal_buffer(data_.N, 0.0f);
    SignalVector signal{signal_buffer.data(), data_.N};
    if (config_.collect_signals)
        simulation.output.signals.resize(simulation.output.ret.size());

    for (Module* module : pipeline) {
        module->numAssets = data_.N;
        module->init();
    }
    for (unsigned di = config_.history_start(); di < simulation.output.ret.size(); ++di) {
        if (!pipeline.empty() && !pipeline[0]->skips_nan_init())
            signal.fill_nan();
        for (Module* module : pipeline) {
            module->calculate(di, signal);
            signal.inf_to_nan();
        }

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
    AssetParallelScope module_workers(config_.module_threads);
    std::vector<float> buffer(data_.N, 0.0f);
    SignalVector signal{buffer.data(), data_.N};
    for (Module* module : pipeline) {
        module->numAssets = data_.N;
        module->init();
    }
    for (unsigned di = config_.history_start(); di < std::min(data_.T, config_.sim_end_index); ++di) {
        if (!pipeline.empty() && !pipeline[0]->skips_nan_init()) signal.fill_nan();
        for (Module* module : pipeline) {
            module->calculate(di, signal);
            signal.inf_to_nan();
        }
        if (callback) callback(di, buffer.data(), data_.N);
    }
}

SimulatorOutput Simulator::run_from_signals(const std::vector<std::vector<float>>& signals) {
    if (signals.size() != config_.T ||
        std::any_of(signals.begin() + config_.history_start(), signals.end(), [this](const auto& row) {
            return row.size() != config_.N;
        }))
        throw std::invalid_argument("Signal matrix dimensions must match the data panel");

    PortfolioSimulation simulation(data_, config_);
    for (unsigned di = config_.history_start(); di < simulation.output.ret.size(); ++di)
        simulation.advance(di, signals[di].data());
    return std::move(simulation.output);
}

SummaryMetrics compute_summary_range(
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& position_count,
    size_t start,
    size_t end,
    unsigned bars_per_year)
{
    SummaryMetrics m{};
    if (start >= end)
        return m;

    // Collect finite returns (caller guarantees no warmup bar in input)
    std::vector<double> finite_r;
    const size_t ret_end = std::min(end, ret.size());
    if (start >= ret_end)
        return m;
    finite_r.reserve(ret_end - start);
    for (size_t i = start; i < ret_end; ++i) {
        if (std::isfinite(ret[i])) finite_r.push_back(ret[i]);
    }
    if (finite_r.empty()) return m;

    const size_t n = finite_r.size();

    // Preserve return addition order while retaining only the running PnL/peak.
    double cumulative_pnl = finite_r[0];
    double peak = 0.0;
    double max_drawdown = 0.0;
    for (size_t i = 0; i < n; ++i) {
        if (i > 0) cumulative_pnl += finite_r[i];
        if (cumulative_pnl > peak) peak = cumulative_pnl;
        const double drawdown = cumulative_pnl - peak;
        if (drawdown < max_drawdown) max_drawdown = drawdown;
    }
    m.pnl = cumulative_pnl;
    m.mdd = max_drawdown;

    // ann_ret = mean(daily_ret) * bars_per_year
    double sum_r = 0.0;
    for (double v : finite_r) sum_r += v;
    double mean_r = sum_r / static_cast<double>(n);
    m.ann_ret = mean_r * static_cast<double>(bars_per_year);

    // sharpe
    double sharpe_val = 0.0;
    if (n >= 2) {
        double sq_sum = 0.0;
        for (double v : finite_r) {
            double d = v - mean_r;
            sq_sum += d * d;
        }
        double std_r = std::sqrt(sq_sum / static_cast<double>(n - 1));  // ddof=1
        if (std_r > 0.0)
            sharpe_val = (mean_r / (std_r + 1e-12)) * std::sqrt(static_cast<double>(bars_per_year));
    }
    m.sharpe = sharpe_val;

    // turnover: mean of finite values
    {
        double sum = 0.0; int cnt = 0;
        const size_t turnover_end = std::min(end, turnover.size());
        for (size_t i = start; i < turnover_end; ++i) {
            if (std::isfinite(turnover[i])) { sum += turnover[i]; ++cnt; }
        }
        m.turnover = (cnt > 0) ? sum / cnt : 0.0;
    }

    // avg_position_count: average number of symbols with position
    {
        double sum = 0.0; int cnt = 0;
        const size_t nz_end = std::min(end, position_count.size());
        for (size_t i = start; i < nz_end; ++i) {
            sum += position_count[i]; ++cnt;
        }
        m.avg_position_count = (cnt > 0) ? sum / cnt : 0.0;
    }

    // max_weight: max of finite values
    {
        double mx = 0.0;
        const size_t max_weight_end = std::min(end, max_weight.size());
        for (size_t i = start; i < max_weight_end; ++i) {
            if (std::isfinite(max_weight[i]) && max_weight[i] > mx)
                mx = max_weight[i];
        }
        m.max_weight = mx;
    }

    // active_bar_count: count(position_count > 0)
    {
        int cnt = 0;
        const size_t nz_end = std::min(end, position_count.size());
        for (size_t i = start; i < nz_end; ++i) {
            if (position_count[i] > 0) ++cnt;
        }
        m.active_bar_count = cnt;
    }

    // win_rate
    {
        int wins = 0, total = 0;
        for (double v : finite_r) {
            ++total;
            if (v > 0.0) ++wins;
        }
        m.win_rate = (total > 0) ? static_cast<double>(wins) / total : 0.0;
    }

    // margin
    m.margin = mean_r / (m.turnover + 1e-12);

    // long_ratio / short_ratio: mean(lw / (lw + sw)) for valid bars where lw+sw > 0
    {
        double sum_ratio = 0.0; int cnt = 0;
        size_t sz = std::min(std::min(long_weight.size(), short_weight.size()), end);
        for (size_t i = start; i < sz; ++i) {
            double lw = long_weight[i];
            double sw = short_weight[i];
            if (std::isfinite(lw) && std::isfinite(sw)) {
                double total_w = lw + sw;
                if (total_w > 0.0) {
                    sum_ratio += lw / total_w;
                    ++cnt;
                }
            }
        }
        if (cnt > 0) {
            m.long_ratio = sum_ratio / cnt;
            m.short_ratio = 1.0 - m.long_ratio;
        }
    }

    // avg_max_bet: mean of max_weight series (finite values)
    {
        double sum = 0.0; int cnt = 0;
        const size_t max_weight_end = std::min(end, max_weight.size());
        for (size_t i = start; i < max_weight_end; ++i) {
            if (std::isfinite(max_weight[i])) { sum += max_weight[i]; ++cnt; }
        }
        m.avg_max_bet = (cnt > 0) ? sum / cnt : 0.0;
    }

    // min_position_count: minimum number of symbols with position
    {
        int mn = std::numeric_limits<int>::max();
        bool found = false;
        const size_t nz_end = std::min(end, position_count.size());
        for (size_t i = start; i < nz_end; ++i) {
            if (position_count[i] < mn) { mn = position_count[i]; found = true; }
        }
        m.min_position_count = found ? static_cast<double>(mn) : 0.0;
    }

    // quality: sharpe * sqrt(abs(1 - maxexp)) * (1 - max_weight)
    // maxexp = max(abs(long_weight - short_weight))
    {
        double maxexp = 0.0;
        size_t sz = std::min(std::min(long_weight.size(), short_weight.size()), end);
        for (size_t i = start; i < sz; ++i) {
            if (std::isfinite(long_weight[i]) && std::isfinite(short_weight[i])) {
                double diff = std::fabs(long_weight[i] - short_weight[i]);
                if (diff > maxexp) maxexp = diff;
            }
        }
        m.quality = m.sharpe * std::sqrt(std::fabs(1.0 - maxexp)) * (1.0 - m.max_weight);
    }

    // sharpe400: sharpe over last min(400, n) bars
    {
        size_t window = std::min<size_t>(400, finite_r.size());
        size_t start = finite_r.size() - window;
        if (window >= 2) {
            double s = 0.0;
            for (size_t i = start; i < finite_r.size(); ++i) s += finite_r[i];
            double mean_w = s / static_cast<double>(window);
            double sq = 0.0;
            for (size_t i = start; i < finite_r.size(); ++i) {
                double d = finite_r[i] - mean_w;
                sq += d * d;
            }
            double std_w = std::sqrt(sq / static_cast<double>(window - 1));
            m.sharpe400 = (std_w > 0.0) ? (mean_w / (std_w + 1e-12)) * std::sqrt(static_cast<double>(bars_per_year)) : 0.0;
        }
    }

    // sharpe80: sharpe over last min(80, n) bars
    {
        size_t window = std::min<size_t>(80, finite_r.size());
        size_t start = finite_r.size() - window;
        if (window >= 2) {
            double s = 0.0;
            for (size_t i = start; i < finite_r.size(); ++i) s += finite_r[i];
            double mean_w = s / static_cast<double>(window);
            double sq = 0.0;
            for (size_t i = start; i < finite_r.size(); ++i) {
                double d = finite_r[i] - mean_w;
                sq += d * d;
            }
            double std_w = std::sqrt(sq / static_cast<double>(window - 1));
            m.sharpe80 = (std_w > 0.0) ? (mean_w / (std_w + 1e-12)) * std::sqrt(static_cast<double>(bars_per_year)) : 0.0;
        }
    }

    // minsoc: minimum of (long_weight + short_weight) across bars
    {
        double mn = std::numeric_limits<double>::max();
        bool found = false;
        size_t sz = std::min(std::min(long_weight.size(), short_weight.size()), end);
        for (size_t i = start; i < sz; ++i) {
            if (std::isfinite(long_weight[i]) && std::isfinite(short_weight[i])) {
                double soc = long_weight[i] + short_weight[i];
                if (soc < mn) mn = soc;
                found = true;
            }
        }
        m.minsoc = found ? mn : 0.0;
    }

    return m;
}

SummaryMetrics compute_summary(
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    const std::vector<int>& position_count,
    unsigned bars_per_year)
{
    return compute_summary_range(
        ret, turnover, max_weight, long_weight, short_weight, position_count,
        0, ret.size(), bars_per_year);
}

static int epoch_ms_to_year(int64_t epoch_ms) {
    time_t t = static_cast<time_t>(epoch_ms / 1000);
    struct tm tm_buf{};
#if defined(_WIN32)
    gmtime_s(&tm_buf, &t);
#else
    gmtime_r(&t, &tm_buf);
#endif
    return tm_buf.tm_year + 1900;
}

std::vector<YearlyMetrics> compute_yearly(
    const std::vector<int64_t>& timestamps,
    const std::vector<double>& ret,
    const std::vector<double>& turnover,
    const std::vector<double>& long_weight,
    const std::vector<double>& short_weight,
    unsigned bars_per_year,
    std::size_t start_index)
{
    if (timestamps.empty()) return {};

    const size_t T = timestamps.size();

    // Warmup bars outside the requested trading span do not affect yearly metrics.
    std::map<int, std::vector<size_t>> year_indices;
    for (size_t i = std::min(start_index, T); i < T; ++i) {
        int yr = epoch_ms_to_year(timestamps[i]);
        year_indices[yr].push_back(i);
    }

    std::vector<YearlyMetrics> result;
    result.reserve(year_indices.size());

    for (auto& [yr, indices] : year_indices) {
        if (indices.empty()) continue;

        YearlyMetrics ym{};
        ym.year = yr;

        const size_t n = indices.size();

        // Collect returns for this year
        std::vector<double> yr_ret;
        yr_ret.reserve(n);
        for (size_t idx : indices) {
            if (idx < ret.size() && std::isfinite(ret[idx]))
                yr_ret.push_back(ret[idx]);
        }

        double sum_r = 0.0;
        double peak = 0.0;
        double max_drawdown = 0.0;
        for (double r : yr_ret) {
            sum_r += r;
            if (sum_r > peak) peak = sum_r;
            const double drawdown = sum_r - peak;
            if (drawdown < max_drawdown) max_drawdown = drawdown;
        }
        const double mean_r = yr_ret.empty() ? 0.0 : sum_r / static_cast<double>(yr_ret.size());
        ym.pnl_pct = sum_r * 100.0;
        ym.ann_ret = mean_r * static_cast<double>(bars_per_year) * 100.0;
        ym.max_dd = max_drawdown * 100.0;

        // IR is the unannualized daily mean/std ratio; Sharpe uses bars_per_year.
        if (yr_ret.size() >= 2) {
            double sq_sum = 0.0;
            for (double v : yr_ret) {
                double d = v - mean_r;
                sq_sum += d * d;
            }
            double std_r = std::sqrt(sq_sum / static_cast<double>(yr_ret.size() - 1));
            ym.ir = (std_r > 0.0) ? mean_r / std_r : 0.0;
            ym.sharpe = (std_r > 0.0)
                ? (mean_r / (std_r + 1e-12)) * std::sqrt(static_cast<double>(bars_per_year))
                : 0.0;
        } else {
            ym.sharpe = 0.0;
        }

        // win_lose (%)
        {
            int wins = 0, total = 0;
            for (double v : yr_ret) {
                ++total;
                if (v > 0.0) ++wins;
            }
            ym.win_lose = (total > 0) ? static_cast<double>(wins) / total * 100.0 : 0.0;
        }

        // daily_tvr
        {
            double sum = 0.0; int cnt = 0;
            for (size_t idx : indices) {
                if (idx < turnover.size() && std::isfinite(turnover[idx])) {
                    sum += turnover[idx]; ++cnt;
                }
            }
            ym.daily_tvr = (cnt > 0) ? sum / cnt : 0.0;
        }

        // margin
        ym.margin = mean_r / (ym.daily_tvr + 1e-12);

        // WorldQuant-style formula on leverage-1, full gross-book returns.
        // ann_ret is percentage points; daily_tvr is already a fraction.
        ym.fitness = ym.sharpe * std::sqrt(
            std::fabs(ym.ann_ret / 100.0) / std::max(ym.daily_tvr, 0.125));

        // long_sm, short_sm
        {
            double lsum = 0.0, ssum = 0.0; int cnt = 0;
            for (size_t idx : indices) {
                bool lv = idx < long_weight.size() && std::isfinite(long_weight[idx]);
                bool sv = idx < short_weight.size() && std::isfinite(short_weight[idx]);
                if (lv && sv) {
                    lsum += long_weight[idx];
                    ssum += short_weight[idx];
                    ++cnt;
                }
            }
            ym.long_sm  = (cnt > 0) ? lsum / cnt : 0.0;
            ym.short_sm = (cnt > 0) ? ssum / cnt : 0.0;
        }

        result.push_back(ym);
    }

    return result;
}

TimeseriesOutput compute_timeseries(
    const std::vector<int64_t>& timestamps,
    const std::vector<double>& net_ret,
    const std::vector<double>& gross_ret,
    const std::vector<double>& turnover,
    const std::vector<double>& max_weight,
    const std::vector<int>& position_count,
    std::size_t start_index,
    const std::vector<double>* frozen_gross)
{
    TimeseriesOutput out;

    if (timestamps.empty()) return out;

    // Start with zero cumulative PnL at the requested trading span.
    const size_t T = timestamps.size();
    if (net_ret.size() < T || gross_ret.size() < T || turnover.size() < T || max_weight.size() < T || position_count.size() < T) {
        throw std::invalid_argument("compute_timeseries: input vectors must be at least timestamps.size()");
    }
    const size_t begin = std::min(start_index, T);
    const size_t n = T - begin;

    out.timestamps.reserve(n);
    out.cum_pnl.reserve(n);
    out.cum_pnl_gross.reserve(n);
    out.turnover_pct.reserve(n);
    out.position_count.reserve(n);
    out.max_weight_pct.reserve(n);
    out.drawdown.reserve(n);
    if (frozen_gross != nullptr) out.frozen_gross.reserve(n);

    double cp_net = 0.0;
    double cp_gross = 0.0;
    double peak_gross = 0.0;

    for (size_t i = begin; i < T; ++i) {
        double r = net_ret[i];
        double r_gross = gross_ret[i];
        double tvr = turnover[i];
        double mw = max_weight[i];
        int nz = position_count[i];

        // Net cumulative PnL (additive)
        cp_net += r;

        // Gross cumulative PnL from the simulator's pre-fee return
        cp_gross += r_gross;

        // Drawdown on gross PnL, matching the gross performance metrics
        if (cp_gross > peak_gross) peak_gross = cp_gross;
        double dd = cp_gross - peak_gross;

        out.timestamps.push_back(timestamps[i]);
        out.cum_pnl.push_back(cp_net);
        out.cum_pnl_gross.push_back(cp_gross);
        out.turnover_pct.push_back(tvr * 100.0);
        out.position_count.push_back(nz);
        out.max_weight_pct.push_back(mw * 100.0);
        out.drawdown.push_back(dd);
        // Bars outside a supplied series keep the neutral zero exposure.
        if (frozen_gross != nullptr)
            out.frozen_gross.push_back(i < frozen_gross->size() ? (*frozen_gross)[i] : 0.0);
    }

    return out;
}

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
    unsigned bars_per_year,
    std::size_t start_index,
    const std::vector<double>* frozen_gross)
{
    FullMetricsResult result{};

    const size_t T = timestamps.size();
    if (T == 0) return result;

    const size_t begin = std::min(start_index, T);
    const size_t is_idx = std::max(begin, static_cast<size_t>(
        std::lower_bound(timestamps.begin(), timestamps.end(), is_timestamp_ms) - timestamps.begin()));
    const size_t os_idx = std::max(begin, static_cast<size_t>(
        std::lower_bound(timestamps.begin(), timestamps.end(), os_timestamp_ms) - timestamps.begin()));

    result.timeseries = compute_timeseries(
        timestamps, net_ret, gross_ret, turnover, max_weight, position_count, begin,
        frozen_gross);
    result.yearly = compute_yearly(
        timestamps, gross_ret, turnover, long_weight, short_weight, bars_per_year, begin);
    result.is_start_index = is_idx - begin;
    result.os_start_index = os_idx - begin;

    if (is_idx < os_idx) {
        result.is_summary = compute_summary_range(
            gross_ret, turnover, max_weight, long_weight, short_weight, position_count,
            is_idx, os_idx, bars_per_year);
    }
    if (os_idx < T) {
        result.os_summary = compute_summary_range(
            gross_ret, turnover, max_weight, long_weight, short_weight, position_count,
            os_idx, T, bars_per_year);
    }
    return result;
}

PerformanceSeries compute_performance_series(const FullMetricsResult& metrics) {
    PerformanceSeries result;
    const auto& source = metrics.timeseries;
    const auto first = std::min(metrics.is_start_index, source.timestamps.size());
    const double net_base = first ? source.cum_pnl[first - 1] : 0.0;
    const double gross_base = first ? source.cum_pnl_gross[first - 1] : 0.0;
    double peak = 0.0;
    result.os_start_index = std::min(source.timestamps.size(), std::max(first, metrics.os_start_index)) - first;
    for (std::size_t i = first; i < source.timestamps.size(); ++i) {
        const double gross = source.cum_pnl_gross[i] - gross_base;
        peak = std::max(peak, gross);
        result.timestamps.push_back(source.timestamps[i]);
        result.cumulative_net.push_back(source.cum_pnl[i] - net_base);
        result.cumulative_gross.push_back(gross);
        result.drawdown.push_back(gross - peak);
        result.turnover.push_back(source.turnover_pct[i] / 100.0);
        result.max_weight.push_back(source.max_weight_pct[i] / 100.0);
        result.position_count.push_back(source.position_count[i]);
        if (!source.frozen_gross.empty()) result.frozen_gross.push_back(source.frozen_gross[i]);
    }
    return result;
}
