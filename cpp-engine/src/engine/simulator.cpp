#include "simulator.h"
#include "costs.h"
#include "../../include/module.h"
#include "../data/data_panel.h"
#include <algorithm>
#include <cmath>
#include <limits>
#include <ctime>
#include <sstream>
#include <stdexcept>
#include <string>

namespace {
constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

std::string iso_day(int64_t epoch_ms) {
    const time_t seconds = static_cast<time_t>(epoch_ms / 1000);
    struct tm parts;
    gmtime_r(&seconds, &parts);
    char text[11];
    std::strftime(text, sizeof(text), "%Y-%m-%d", &parts);
    return text;
}

const char* unconfirmed_kind_name(UnconfirmedKind kind) {
    switch (kind) {
        case UnconfirmedKind::UnresolvedLeg: return "unresolved_leg";
        case UnconfirmedKind::DefaultSettlement: return "default_settlement";
        case UnconfirmedKind::EstimateValue: return "estimate_value";
        case UnconfirmedKind::PendingDropped: return "pending_dropped";
    }
    return "unknown";
}

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

SimulatorOutput make_output(const DataPanel& data) {
    SimulatorOutput output;
    output.ret.assign(data.T, 0.0);
    output.turnover.assign(data.T, 0.0);
    output.max_weight.assign(data.T, 0.0);
    output.long_weight.assign(data.T, 0.0);
    output.short_weight.assign(data.T, 0.0);
    output.position_count.assign(data.T, 0);
    output.missing_data.assign(data.T, MissingDataDiagnostics{});
    output.corporate_actions.assign(data.T, CorporateActionDiagnostics{});
    output.borrow_cost.assign(data.T, 0.0);
    output.borrow_unrated.assign(data.T, 0u);
    output.timestamps = data.timestamps;
    return output;
}

// One portfolio transition shared by generated and precomputed signals.
// Weights stay fixed-book notional. An observable quote is a finite positive
// close; while one is missing the weight and its valuation mark freeze instead
// of being valued, traded or liquidated at an invented price.
class PortfolioSimulation {
public:
    PortfolioSimulation(const DataPanel& data, const SimulatorConfig& config)
        : output(make_output(data)), data_(data), config_(config),
          previous_weights_(data.N, 0.0),
          previous_targets_(config.mode == 1 ? data.N : 0, 0.0),
          targets_(data.N, 0.0),
          carried_(data.N, 0.0),
          weights_(data.N, 0.0),
          marks_(data.N, kNaN),
          retired_(data.N, 0),
          quote_state_(data.N, kQuoteObserved),
          missing_bars_(data.N, 0),
          settles_now_(data.N, 0),
          stale_flag_(data.N, 0),
          expired_flag_(data.N, 0),
          pending_transfer_(data.N, 0.0),
          unconfirmed_flag_(data.N, 0),
          records_rows_(static_cast<bool>(config.accounting_row_callback)),
          has_actions_(!data.corporate_actions.empty()) {
        if (records_rows_) {
            marks_before_.assign(data.N, kNaN);
            market_trades_.assign(data.N, 0.0);
            settled_gross_.assign(data.N, 0.0);
            delisting_returns_.assign(data.N, kNaN);
        }
        if (has_actions_ || data.thin_layout)
            received_.assign(data.N, 0.0);
        if (has_actions_) {
            received_.assign(data.N, 0.0);
            cash_share_.assign(data.N, 0.0);
            pre_settlement_mark_.assign(data.N, kNaN);
            // A right's expiry belongs to the security: the earliest expiry among the
            // legs that deliver it settles a held, unquoted position at -1.
            rights_expiry_.assign(data.N, -1);
            for (const auto& leg : data.corporate_actions)
                if (leg.rights && leg.child >= 0 && leg.expiry_bar >= 0 &&
                    (rights_expiry_[leg.child] < 0 || leg.expiry_bar < rights_expiry_[leg.child]))
                    rights_expiry_[leg.child] = leg.expiry_bar;
        }
    }

    void advance(unsigned di, const float* signal) {
        const float* close = data_.row("close", di);
        // Warmup bars keep marks and intent current without holdings or costs.
        const bool trading = di >= config_.sim_start_index;
        MissingDataDiagnostics diagnostics;
        double pnl = 0.0;
        double turnover = 0.0;
        double borrow = 0.0;

        if (records_rows_) {
            marks_before_.assign(marks_.begin(), marks_.end());
            std::fill(market_trades_.begin(), market_trades_.end(), 0.0);
            std::fill(settled_gross_.begin(), settled_gross_.end(), 0.0);
            std::fill(delisting_returns_.begin(), delisting_returns_.end(), kNaN);
        }

        bar_ = di;
        bar_unconfirmed_count_ = 0;
        bar_unconfirmed_abs_ = 0.0;
        for (unsigned asset : bar_unconfirmed_assets_) unconfirmed_flag_[asset] = 0;
        bar_unconfirmed_assets_.clear();
        if (data_.thin_layout)
            apply_moves(di, close, trading, pnl, diagnostics);
        else
            settle(di, close, trading, pnl, diagnostics);
        normalize_intent(di, signal);
        if (trading) {
            require_priced_quotes(di);
            require_tradable_quotes(di);
        }
        CorporateActionDiagnostics actions;
        if (has_actions_ && !data_.thin_layout) {
            actions.rights_expired_held = expired_held_count_;
            actions.rights_expired_gross = expired_held_gross_;
            expired_held_count_ = 0;
            expired_held_gross_ = 0.0;
            book_corporate_actions(di, close, trading, pnl, actions);
        }
        if (data_.thin_layout)
            value_holdings_thin(di, close, trading, pnl, diagnostics);
        else
            value_holdings(di, close, trading, pnl, diagnostics);
        apply_successor_transfers();

        if (trading) {
            execute(diagnostics, turnover);
            apply_received_children();
            output.ret[di] = pnl;
            output.turnover[di] = turnover;
            output.ret[di] -= turnover * config_.fee_rate;
            if (config_.borrow) {
                // Financing of the short book carried from the previous close to this
                // one; the gross P&L above is untouched, only the net return pays it.
                unsigned unrated = 0;
                borrow = config_.borrow->charge(di, previous_weights_, unrated);
                output.ret[di] -= borrow;
                output.borrow_cost[di] = borrow;
                output.borrow_unrated[di] = unrated;
            }

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
        // Warmup rows carry no position, so only retirements can be nonzero here.
        // Unconfirmed P&L is recorded from the settlement and the ledger paths alike,
        // so the bar's counters are copied here whether or not a ledger is loaded.
        actions.unconfirmed_count = bar_unconfirmed_count_;
        actions.unconfirmed_abs_weight = bar_unconfirmed_abs_;
        output.missing_data[di] = diagnostics;
        output.corporate_actions[di] = actions;

        if (records_rows_)
            emit_row(di, close, trading, pnl, turnover, borrow, diagnostics, actions);

        // Strict policy: the first bar with unconfirmed P&L ends the run. Everything
        // above already ran, so the message can list the bar's events.
        if (config_.unresolved_policy == UnresolvedPolicy::Strict && bar_unconfirmed_count_ > 0)
            throw std::runtime_error(strict_message(di));

        previous_weights_.swap(weights_);
        if (config_.mode == 1)
            previous_targets_.swap(targets_);
    }

    SimulatorOutput output;

private:
    const std::vector<double>& execution_intent() const {
        return config_.mode == 0 ? targets_ : previous_targets_;
    }

    // Administrative settlement. It replaces the normal quote move for the bar,
    // needs no quote of its own, and is never a market execution. Deciding who
    // settles comes first so a successor that also settles today is never paid.
    void settle(unsigned di, const float* close, bool trading, double& pnl,
                MissingDataDiagnostics& diagnostics) {
        const bool has_events = !data_.settlement_bar.empty();
        const bool last_bar = di + 1 == data_.T;
        bool any_settlement = false;
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            settles_now_[ii] = 0;
            stale_flag_[ii] = 0;
            expired_flag_[ii] = 0;
            pending_transfer_[ii] = 0.0;
            quote_state_[ii] = classify_quote(close[ii]);
            carried_[ii] = retired_[ii] ? 0.0 : previous_weights_[ii];
            if (retired_[ii])
                continue;

            const bool observed = quote_state_[ii] == kQuoteObserved;
            const int32_t event_bar = has_events ? data_.settlement_bar[ii] : -1;
            // The end date opens the settlement, the loss of the quote closes it.
            // A security still printing after its end date is a data conflict, so
            // keep valuing and trading it and settle on the first silent bar.
            const bool event_due = event_bar >= 0 && di >= static_cast<unsigned>(event_bar);
            if (event_due && observed)
                ++diagnostics.post_end_quote_bars;
            const bool event_now = event_due && !observed;
            // A stale quote releases a frozen holding, so it needs a held weight
            // and an observed mark, and only where no event exists. An unheld
            // security is never retired by silence: its quote can still resume.
            const unsigned missing_bars = observed ? 0u : missing_bars_[ii] + 1u;
            // Priority of the three administrative exits: an events settlement, then
            // a right's expiry, then the stale-quote rule. A right with a known expiry
            // has its own end of life, so silence before that day only freezes it
            // (never a default-return settlement); once the expiry day has passed a
            // held right with no quote lapsed at 0. A right still printing after
            // expiry is valued until it goes silent, like a security quoting past
            // its end date.
            const bool has_expiry = has_actions_ && rights_expiry_[ii] >= 0;
            const bool expired_now = !event_now && has_expiry &&
                di >= static_cast<unsigned>(rights_expiry_[ii]) &&
                !observed && carried_[ii] != 0.0 && std::isfinite(marks_[ii]);
            const bool stale_now = !event_now && !has_expiry && event_bar < 0 &&
                config_.missing_close_policy == MissingClosePolicy::Carry &&
                config_.stale_quote_settle_bars > 0 &&
                missing_bars >= config_.stale_quote_settle_bars &&
                carried_[ii] != 0.0 && std::isfinite(marks_[ii]);
            if (!event_now && !stale_now && !expired_now) {
                if (last_bar && event_bar >= 0)
                    ++diagnostics.pending_settlement_count;
                continue;
            }
            settles_now_[ii] = 1;
            stale_flag_[ii] = stale_now ? 1 : 0;
            expired_flag_[ii] = expired_now ? 1 : 0;
            retired_[ii] = 1;
            any_retired_ = true;
            any_settlement = true;
        }
        if (any_settlement)
            book_settlements(close, trading, pnl, diagnostics);
    }

    // Resolve each settlement's return and split it into a cash leg and a stock leg.
    void book_settlements(const float* close, bool trading, double& pnl,
                          MissingDataDiagnostics& diagnostics) {
        const bool has_returns = data_.settlement_return.size() == data_.N;
        const bool has_successors = data_.settlement_successor.size() == data_.N;
        const bool has_ratios = data_.settlement_successor_ratio.size() == data_.N;
        const bool has_cash = data_.settlement_cash.size() == data_.N;
        const bool has_legs = data_.settlement_has_stock_leg.size() == data_.N &&
                              data_.settlement_has_cash_leg.size() == data_.N;
        const bool has_named = data_.settlement_successor_named.size() == data_.N;
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            if (!settles_now_[ii])
                continue;
            // A successor can only receive the holding if it is still listed and
            // quoted today; retired_ is already final for this bar.
            const int32_t successor = has_successors ? data_.settlement_successor[ii] : -1;
            const bool quoted_successor = successor >= 0 &&
                static_cast<unsigned>(successor) < data_.N &&
                static_cast<unsigned>(successor) != ii && !retired_[successor] &&
                quote_state_[successor] == kQuoteObserved;
            const double ratio = has_ratios
                ? data_.settlement_successor_ratio[ii] : std::numeric_limits<double>::quiet_NaN();
            const double cash = has_cash
                ? data_.settlement_cash[ii] : std::numeric_limits<double>::quiet_NaN();
            // Whether a leg exists and whether this panel can value it are two
            // different questions. A stock leg the file declared but could not
            // restate in cache units must not silently become a cash-only deal.
            const bool named_successor = has_named && data_.settlement_successor_named[ii];
            const bool stock_leg = has_legs ? data_.settlement_has_stock_leg[ii] != 0
                                            : (std::isfinite(ratio) || named_successor);
            const bool cash_leg = has_legs ? data_.settlement_has_cash_leg[ii] != 0
                                           : std::isfinite(cash);
            const bool priced_mark = std::isfinite(marks_[ii]) && marks_[ii] > 0.0;
            const bool transferable = quoted_successor && std::isfinite(ratio) && priced_mark;

            // Per-share proceeds priced on the settlement bar itself: no future
            // quote enters the return. The stock leg needs a quoted successor, so
            // a deal whose survivor left the panel falls back to the file's return.
            const double stock_value = transferable
                ? ratio * static_cast<double>(close[successor]) : 0.0;
            const double cash_value = (cash_leg && std::isfinite(cash)) ? cash : 0.0;
            const double proceeds = stock_value + cash_value;
            const double stock_share = priced_mark ? stock_value / marks_[ii] : 0.0;
            const double cash_share = priced_mark ? cash_value / marks_[ii] : 0.0;
            // Every declared leg has to be priceable here, so cash alone only
            // settles a deal that has no stock leg at all.
            const bool computable = priced_mark && (stock_leg || cash_leg) &&
                (!stock_leg || transferable) && (!cash_leg || std::isfinite(cash));

            double settlement_return = config_.default_delisting_return;
            bool explicit_return = false;
            bool computed_return = false;
            if (expired_flag_[ii]) {
                settlement_return = -1.0;   // an unexercised right is worth nothing after expiry
                explicit_return = true;
            } else if (computable) {
                settlement_return = stock_share + cash_share - 1.0;
                computed_return = true;
            } else if (has_returns && std::isfinite(data_.settlement_return[ii])) {
                settlement_return = static_cast<double>(data_.settlement_return[ii]);
                explicit_return = true;
            }

            const double exposure = carried_[ii];
            ++diagnostics.retirement_count;
            if (trading && exposure == 0.0)
                ++diagnostics.unheld_settlement_count;
            if (trading && exposure != 0.0) {
                const double settlement_pnl = exposure * settlement_return;
                pnl += settlement_pnl;
                ++diagnostics.delisting_count;
                diagnostics.delisting_pnl += settlement_pnl;
                if (!explicit_return && !computed_return) {
                    ++diagnostics.default_delisting_count;
                    diagnostics.default_delisting_gross += std::fabs(exposure);
                    // The return is an assumption, not a priced settlement: the P&L
                    // of this exposure is unconfirmed.
                    record_unconfirmed(ii, exposure, UnconfirmedKind::DefaultSettlement,
                                       stale_flag_[ii] ? "stale_quote"
                                       : (stock_leg || cash_leg) ? "deal_terms_not_priced"
                                                                 : "no_deal_terms");
                }
                if (computed_return)
                    ++diagnostics.computed_settlement_count;
                if (stale_flag_[ii])
                    ++diagnostics.stale_settlement_count;
                if (expired_flag_[ii]) {
                    ++expired_held_count_;
                    expired_held_gross_ += std::fabs(exposure);
                }
                if (config_.collect_settlements)
                    output.settlements.push_back(SettlementRecord{
                        bar_, ii, exposure, settlement_return,
                        computed_return ? SettlementSource::Computed
                                        : (explicit_return ? SettlementSource::Explicit
                                                           : SettlementSource::Default),
                        cash_share, 0.0, -1, 0.0});
                if (computable && stock_leg && transferable && proceeds > 0.0) {
                    // Only the stock leg moves. It is not a market trade: it lands
                    // at the successor's close and the next execution rebalances it.
                    // The cash leg is realized and leaves the book here.
                    //
                    // Two bases, on purpose. settled_gross stays on the Phase 1
                    // exposure basis (a share of |h_prev|) so the settled totals
                    // remain comparable across default-return sensitivities, while
                    // converted_gross reports the weight actually handed over.
                    const double transfer = exposure * stock_share;
                    pending_transfer_[successor] += transfer;
                    any_transfer_ = true;
                    ++diagnostics.successor_conversions;
                    diagnostics.converted_gross += std::fabs(transfer);
                    if (config_.collect_settlements) {
                        auto& record = output.settlements.back();
                        record.stock_share = stock_share;
                        record.successor = successor;
                        record.transferred = transfer;
                    }
                    const double cash_exposure = std::fabs(exposure) * (cash_value / proceeds);
                    diagnostics.settled_gross += cash_exposure;
                    if (records_rows_)
                        settled_gross_[ii] = cash_exposure;
                } else {
                    diagnostics.settled_gross += std::fabs(exposure);
                    if (records_rows_)
                        settled_gross_[ii] = std::fabs(exposure);
                }
            }
            if (records_rows_)
                delisting_returns_[ii] = settlement_return;
            // Ledger legs dated on this bar are valued against the mark the settlement
            // itself used, so keep it before the settlement return scales it.
            if (has_actions_)
                pre_settlement_mark_[ii] = marks_[ii];
            if (std::isfinite(marks_[ii]))
                marks_[ii] *= 1.0 + settlement_return;
            carried_[ii] = 0.0;
            missing_bars_[ii] = 0;
        }
    }

    // Thin layout: the builder resolved every event into returns_total and move rows,
    // so the bar only moves weight. All amounts come from the carried book at the start
    // of the bar. An ending source books its own P&L here, hands its successor amount
    // to the book before execution and retires before the intent is normalized (where
    // settle runs on the old path); a distribution joins after execution.
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
    // any bars the quote was missing. The quote only decides freezing, as on the old path.
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
                if (trading && carried_[ii] != 0.0) {
                    ++diagnostics.missing_held_count;
                    diagnostics.frozen_gross += std::fabs(carried_[ii]);
                }
            }
        }
    }

    // Successor shares arrive valued at today's close, so they are added after the
    // holding revaluation and earn nothing on the settlement bar itself.
    void apply_successor_transfers() {
        if (!any_transfer_)
            return;
        for (unsigned ii = 0; ii < data_.N; ++ii)
            if (pending_transfer_[ii] != 0.0)
                carried_[ii] += pending_transfer_[ii];
        any_transfer_ = false;
    }

    // Corporate-action legs on the ex-date bar. Both legs are measured against
    // the parent's last observed mark m, so with the parent's own quote move
    // the bar books the cash-style return (close_ex + cash + ratio * child)/m - 1:
    // the cash leg is realized as w * cash / m, and the stock leg creates a child
    // weight w * ratio * child_close / m whose value is recognised at once. The
    // child weight joins the book after this bar's execution, so it is valued
    // from the next bar on and the strategy's next target rebalances it (target
    // zero sells it with the normal fee). A child that has no quote yet waits
    // for its first observed quote; no future price is pulled to the ex-date.
    void book_corporate_actions(unsigned di, const float* close, bool trading, double& pnl,
                                CorporateActionDiagnostics& actions) {
        const auto& legs = data_.corporate_actions;
        if (next_leg_ >= legs.size() && pending_distributions_.empty())
            return;
        if (!trading) {
            // Warmup holds nothing, so there is nothing to pay on; pending legs
            // cannot exist yet because they need a held parent.
            while (next_leg_ < legs.size() && legs[next_leg_].bar == di) ++next_leg_;
            return;
        }
        // A leg dated on the bar its parent settles (events file) is measured
        // against the exposure and mark the settlement used, before the parent
        // was retired: split-path deals keep the cash and the successor in the
        // events row and the extra children in the ledger, all against one mark.
        const auto settled_here = [&](unsigned parent) { return settles_now_[parent] != 0; };
        const auto exposure_of = [&](unsigned parent) {
            return settled_here(parent) ? previous_weights_[parent] : carried_[parent];
        };
        const auto mark_of = [&](unsigned parent) {
            return settled_here(parent) ? pre_settlement_mark_[parent] : marks_[parent];
        };
        const bool has_cash_legs = data_.settlement_has_cash_leg.size() == data_.N;
        const bool has_successors = data_.settlement_successor.size() == data_.N;
        // Cash legs first, so a same-day stock leg can report the cash booked beside it.
        const size_t first_leg = next_leg_;
        for (size_t at = first_leg; at < legs.size() && legs[at].bar == di; ++at) {
            const auto& leg = legs[at];
            if (leg.stock_leg || !config_.apply_dividends)
                continue;
            const double weight = exposure_of(leg.parent);
            const double mark = mark_of(leg.parent);
            if (weight == 0.0 || !(std::isfinite(mark) && mark > 0.0))
                continue;
            if (settled_here(leg.parent)) {
                ++actions.legs_on_settlement_bar;
                // The events row realizes its own cash on this bar; a ledger cash leg
                // beside it would pay the same cash twice, so it stays count-only. The
                // events row also settles the P&L, so a marker here is not unconfirmed
                // (same order as the stock loop below).
                if (has_cash_legs && data_.settlement_has_cash_leg[leg.parent]) {
                    ++actions.legs_deferred_to_events;
                    continue;
                }
            }
            if (leg.unresolved) {
                ++actions.legs_unresolved;
                record_unconfirmed(leg.parent, weight, UnconfirmedKind::UnresolvedLeg,
                                   std::isfinite(leg.cash) ? "status_unresolved" : "no_cash_amount");
            }
            if (!std::isfinite(leg.cash))
                continue;   // the ledger could not resolve the amount: visible, never guessed
            const double cash_share = leg.cash / mark;
            pnl += weight * cash_share;
            ++actions.dividend_count;
            actions.dividend_pnl += weight * cash_share;
            cash_share_[leg.parent] += cash_share;
            if (leg.estimate) {
                // Booked as the ledger states it, but the amount (or its date) is the ledger's estimate.
                ++actions.dividend_estimated;
                if (!leg.unresolved)
                    record_unconfirmed(leg.parent, weight, UnconfirmedKind::EstimateValue, "cash:" + estimate_reason_of(leg));
            }
        }
        // Pending legs from earlier ex-dates resolve on the child's first quote.
        for (auto it = pending_distributions_.begin(); it != pending_distributions_.end();) {
            const auto& leg = legs[it->leg];
            if (leg.rights && leg.expiry_bar >= 0 && di >= static_cast<unsigned>(leg.expiry_bar)) {
                // The entitlement lapsed: unlike an unpriced leg it waited for a quote
                // and is closed here, on the first bar after the expiry day, at 0.
                ++actions.rights_expired;
                record_distribution(leg, it->ex_bar, it->parent_weight, 0.0, it->cash_share,
                                    DistributionSource::Expired, it->on_settlement_bar);
                it = pending_distributions_.erase(it);
            } else if (retired_[it->child]) {
                ++actions.distribution_unpriced;
                record_distribution(leg, it->ex_bar, it->parent_weight, 0.0, it->cash_share,
                                    DistributionSource::Unpriced, it->on_settlement_bar);
                if (!leg.unresolved)
                    record_unconfirmed(leg.parent, it->parent_weight, UnconfirmedKind::PendingDropped,
                                       "child_retired_while_pending");
                it = pending_distributions_.erase(it);
            } else if (quote_state_[it->child] == kQuoteObserved) {
                create_child(leg, it->ex_bar, it->parent_weight,
                             child_value(it->parent_weight, leg.child_ratio,
                                         static_cast<double>(close[it->child]), it->mark),
                             it->cash_share, DistributionSource::FirstQuote, it->on_settlement_bar,
                             pnl, actions);
                it = pending_distributions_.erase(it);
            } else {
                ++it;
            }
        }
        for (; next_leg_ < legs.size() && legs[next_leg_].bar == di; ++next_leg_) {
            const auto& leg = legs[next_leg_];
            if (!leg.stock_leg || !config_.apply_distributions)
                continue;
            const double weight = exposure_of(leg.parent);
            const double mark = mark_of(leg.parent);
            if (weight == 0.0 || !(std::isfinite(mark) && mark > 0.0))
                continue;
            if (settled_here(leg.parent)) {
                ++actions.legs_on_settlement_bar;
                // The successor is delivered by the transfer in the events row; a
                // ledger leg naming it would deliver it twice, so it stays count-only.
                if (has_successors && leg.child >= 0 && data_.settlement_successor[leg.parent] == leg.child) {
                    ++actions.legs_deferred_to_events;
                    continue;
                }
            }
            ++actions.distribution_count;
            if (leg.unresolved) {
                ++actions.legs_unresolved;
                record_unconfirmed(leg.parent, weight, UnconfirmedKind::UnresolvedLeg,
                                   !leg.child_named ? "no_child"
                                   : !std::isfinite(leg.child_ratio) ? "ratio_unknown"
                                                                     : "status_unresolved");
            }
            const double cash_share = cash_share_[leg.parent];
            const bool on_settlement_bar = settled_here(leg.parent);
            // A child the panel does not carry but whose value the ledger states (a
            // when-issued close or a (3)(4) estimate, per parent share): the value is
            // recognised as P&L like cash and no position is created. Same path as
            // returns_total (flag 3); an estimate stays visible as unconfirmed.
            if (leg.child < 0 && leg.ledger_value_usable && std::isfinite(leg.child_value) && leg.child_value > 0.0) {
                const double value = weight * leg.child_value / mark;
                pnl += value;
                actions.distribution_pnl += value;
                ++actions.distribution_value_only;
                if (leg.estimate) {
                    ++actions.distribution_estimated;
                    if (!leg.unresolved)
                        record_unconfirmed(leg.parent, weight, UnconfirmedKind::EstimateValue,
                                           "ledger_value_cash:" + leg.value_source);
                }
                record_distribution(leg, di, weight, value, cash_share, DistributionSource::LedgerValueCash,
                                    on_settlement_bar);
                continue;
            }
            // A child outside the panel, without a ratio, or already retired cannot
            // receive anything: the leg is reported and no value is recognised.
            if (leg.child < 0 || !std::isfinite(leg.child_ratio) || retired_[leg.child]) {
                ++actions.distribution_unpriced;
                record_distribution(leg, di, weight, 0.0, cash_share, DistributionSource::Unpriced,
                                    on_settlement_bar);
                if (!leg.unresolved)
                    record_unconfirmed(leg.parent, weight, UnconfirmedKind::PendingDropped,
                                       leg.child < 0 ? "child_not_in_panel"
                                       : !std::isfinite(leg.child_ratio) ? "ratio_unknown"
                                                                         : "child_retired");
                continue;
            }
            const unsigned child = static_cast<unsigned>(leg.child);
            if (quote_state_[child] == kQuoteObserved) {
                create_child(leg, di, weight,
                             child_value(weight, leg.child_ratio, static_cast<double>(close[child]), mark),
                             cash_share, DistributionSource::ChildClose, on_settlement_bar, pnl, actions);
            } else if (leg.ledger_value_usable && !std::isfinite(marks_[child]) &&
                       std::isfinite(leg.child_close) && leg.child_close > 0.0 &&
                       config_.missing_close_policy == MissingClosePolicy::Carry) {
                // A child that has never printed here starts at the ledger's value: a
                // when-issued close (P10 (2)) or a flagged estimate ((3)(4), reported
                // as such). The first real quote then earns the move from it like a
                // frozen holding, and the stale-quote clock starts after this bar.
                // The error policy cannot hold an unquoted name, so it waits instead.
                marks_[child] = leg.child_close;
                clock_reset_.push_back(child);
                create_child(leg, di, weight, child_value(weight, leg.child_ratio, leg.child_close, mark),
                             cash_share, DistributionSource::LedgerValue, on_settlement_bar, pnl, actions);
            } else if (config_.unpriced_child == UnpricedChildPolicy::Pending) {
                pending_distributions_.push_back(PendingDistribution{
                    next_leg_, di, child, weight, mark, cash_share, on_settlement_bar});
            } else {
                ++actions.distribution_unpriced;
                record_distribution(leg, di, weight, 0.0, cash_share, DistributionSource::Unpriced,
                                    on_settlement_bar);
                if (!leg.unresolved)
                    record_unconfirmed(leg.parent, weight, UnconfirmedKind::PendingDropped, "policy_drop");
            }
        }
        for (size_t at = first_leg; at < next_leg_; ++at)
            cash_share_[legs[at].parent] = 0.0;
        actions.distribution_pending = static_cast<unsigned>(pending_distributions_.size());
        // The run ends with legs still waiting for a child quote: their value was
        // never recognised, so the parent's P&L stays unconfirmed.
        if (di + 1 == data_.T)
            for (const auto& waiting : pending_distributions_)
                if (!legs[waiting.leg].unresolved)
                    record_unconfirmed(legs[waiting.leg].parent, waiting.parent_weight,
                                       UnconfirmedKind::PendingDropped, "pending_at_end");
    }

    // One expression for every child valuation, so the ex-date and first-quote
    // paths agree to the last bit.
    static double child_value(double parent_weight, double ratio, double price, double mark) {
        return parent_weight * ratio * price / mark;
    }

    void create_child(const DataPanel::CorporateActionLeg& leg, unsigned ex_bar, double parent_weight,
                      double child_weight, double cash_share, DistributionSource source,
                      bool on_settlement_bar, double& pnl, CorporateActionDiagnostics& actions) {
        pnl += child_weight;
        actions.distribution_pnl += child_weight;
        ++actions.child_positions_created;
        // A flagged leg stays an estimate whatever priced it: a ledger value is an
        // estimated value; a quote-priced child still carries an estimated ratio or
        // ex-date (the export's estimateReason says which).
        if (leg.estimate) {
            ++actions.distribution_estimated;
            if (!leg.unresolved)
                record_unconfirmed(leg.parent, parent_weight, UnconfirmedKind::EstimateValue,
                                   source == DistributionSource::LedgerValue ? "ledger_value:" + leg.value_source
                                   : source == DistributionSource::FirstQuote ? "first_quote:" + estimate_reason_of(leg)
                                                                              : "child_close:" + estimate_reason_of(leg));
        }
        received_[static_cast<unsigned>(leg.child)] += child_weight;
        any_received_ = true;
        record_distribution(leg, ex_bar, parent_weight, child_weight, cash_share, source, on_settlement_bar);
    }

    // `on_settlement_bar` describes the ex-date booking, so a leg that waited for a
    // quote keeps the flag its ex-date had.
    void record_distribution(const DataPanel::CorporateActionLeg& leg, unsigned ex_bar,
                             double parent_weight, double child_weight, double cash_share,
                             DistributionSource source, bool on_settlement_bar) {
        if (config_.collect_distributions)
            output.distributions.push_back(DistributionRecord{
                bar_, ex_bar, leg.parent, leg.child, parent_weight, child_weight, cash_share, source,
                leg.estimate && source != DistributionSource::Unpriced && source != DistributionSource::Expired,
                on_settlement_bar, leg.value_source, leg.event_type});
    }

    // What the ledger estimated on a flagged leg: the export's estimateReason, else
    // 'value' for the valued tiers, else 'unspecified' (a flag without a reason column).
    static std::string estimate_reason_of(const DataPanel::CorporateActionLeg& leg) {
        if (!leg.estimate_reason.empty()) return leg.estimate_reason;
        if (leg.value_source == "event_value" || leg.value_source == "vendor_implied") return "value";
        return "unspecified";
    }

    // One held position whose P&L this bar could not confirm. Recording never
    // changes the accounting; the bar's counters feed CorporateActionDiagnostics.
    void record_unconfirmed(unsigned asset, double weight_before, UnconfirmedKind kind,
                            std::string detail) {
        output.unconfirmed.push_back(UnconfirmedRecord{bar_, asset, weight_before, kind, std::move(detail)});
        ++bar_unconfirmed_count_;
        // The exposure at stake is counted once per (bar, parent): two legs of one
        // parent on the same bar put the same weight in doubt, not twice the weight.
        if (!unconfirmed_flag_[asset]) {
            unconfirmed_flag_[asset] = 1;
            bar_unconfirmed_assets_.push_back(asset);
            bar_unconfirmed_abs_ += std::fabs(weight_before);
        }
    }

    std::string strict_message(unsigned di) const {
        std::ostringstream text;
        text << "Corporate actions: unconfirmed P&L on " << iso_day(data_.timestamps[di])
             << " under unresolved_policy strict (" << bar_unconfirmed_count_ << " event(s) on this bar";
        const unsigned limit = std::min<unsigned>(config_.strict_report_limit, bar_unconfirmed_count_);
        if (limit < bar_unconfirmed_count_) text << ", first " << limit << " listed";
        text << "): ";
        unsigned listed = 0;
        for (auto it = output.unconfirmed.end() - bar_unconfirmed_count_;
             it != output.unconfirmed.end() && listed < limit; ++it, ++listed) {
            if (listed) text << "; ";
            text << data_.symbols[it->asset] << " " << unconfirmed_kind_name(it->kind)
                 << "(" << it->detail << ") weightBefore=" << it->weight_before;
        }
        text << " - resolve the ledger/events rows or set unresolved_policy: warn";
        return text.str();
    }

    // Received child weights are not market trades: they join the book after the
    // bar's execution at the value already recognised, and trade from the next bar.
    void apply_received_children() {
        if (!any_received_)
            return;
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            if (received_[ii] != 0.0) {
                weights_[ii] += received_[ii];
                received_[ii] = 0.0;
            }
        }
        // The ledger value stood in for this bar's quote, so the silence that a
        // stale-quote settlement counts begins on the next bar.
        for (unsigned child : clock_reset_) missing_bars_[child] = 0;
        clock_reset_.clear();
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
        normalize_signal_row(data_, di, source, targets_, config_.missing_close_policy);
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

    // Error policy keeps the original fail-fast contract: a held or executing
    // position without a valid close is a data error, not a frozen weight.
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
                if (trading && carried_[ii] != 0.0) {
                    ++diagnostics.missing_held_count;
                    diagnostics.frozen_gross += std::fabs(carried_[ii]);
                }
            }
        }
    }

    // Only quoted securities execute. Frozen weights reduce the budget of the
    // remaining targets; they never inflate them to refill the book.
    void execute(MissingDataDiagnostics& diagnostics, double& turnover) {
        const auto& desired = execution_intent();
        double scale = 1.0;
        if (diagnostics.frozen_gross > 0.0) {
            double tradable_gross = 0.0;
            for (unsigned ii = 0; ii < data_.N; ++ii)
                if (!retired_[ii] && quote_state_[ii] == kQuoteObserved)
                    tradable_gross += std::fabs(desired[ii]);
            const double budget = std::max(0.0, 1.0 - diagnostics.frozen_gross);
            if (tradable_gross > budget)
                scale = budget / tradable_gross;
        }
        for (unsigned ii = 0; ii < data_.N; ++ii) {
            const double target = desired[ii];
            if (retired_[ii]) {
                if (target != 0.0)
                    ++diagnostics.pending_delisting_entry_count;
                weights_[ii] = 0.0;
            } else if (quote_state_[ii] == kQuoteObserved) {
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
            if (records_rows_)
                market_trades_[ii] = trade;
        }
    }

    void emit_row(unsigned di, const float* close, bool trading, double pnl, double turnover,
                  double borrow, const MissingDataDiagnostics& diagnostics,
                  const CorporateActionDiagnostics& actions) {
        const double fee = turnover * config_.fee_rate;
        const AccountingRowView view{
            di, trading, close, previous_weights_, execution_intent(), weights_,
            market_trades_, marks_before_, marks_, delisting_returns_, settled_gross_,
            retired_, pnl, fee, pnl - fee - borrow, turnover, diagnostics, actions, borrow};
        config_.accounting_row_callback(view);
    }

    const DataPanel& data_;
    const SimulatorConfig& config_;
    std::vector<double> previous_weights_;  // book carried into the bar, before settlement
    std::vector<double> previous_targets_;
    std::vector<double> targets_;
    std::vector<double> carried_;           // book after settlement, before execution
    std::vector<double> weights_;
    std::vector<double> marks_;             // last observed close; NaN before the first quote
    std::vector<uint8_t> retired_;
    std::vector<uint8_t> quote_state_;
    std::vector<unsigned> missing_bars_;    // consecutive bars without an observable quote
    std::vector<uint8_t> settles_now_;      // settles on this bar, decided before returns resolve
    std::vector<uint8_t> stale_flag_;
    std::vector<uint8_t> expired_flag_;     // settles as a lapsed right (-1) on this bar
    std::vector<int32_t> rights_expiry_;    // per asset: first bar after the delivering right's expiry, -1 = none
    unsigned expired_held_count_ = 0;       // lapsed held rights settled on this bar, moved into the bar's actions
    double expired_held_gross_ = 0.0;
    std::vector<double> pending_transfer_;  // successor exposure waiting for this bar's execution
    std::vector<float> masked_signal_;
    std::vector<double> marks_before_;
    std::vector<double> market_trades_;
    std::vector<double> settled_gross_;
    std::vector<double> delisting_returns_;
    // A stock leg whose child had no quote on its ex-date: the parent's weight
    // and mark are fixed there, the child weight is priced at the first quote.
    struct PendingDistribution {
        size_t leg;             // index into data_.corporate_actions
        unsigned ex_bar;
        unsigned child;
        double parent_weight;
        double mark;
        double cash_share;
        bool on_settlement_bar; // the ex-date was the parent's settlement bar
    };
    std::vector<PendingDistribution> pending_distributions_;
    std::vector<double> received_;   // child weights joining the book after this bar's execution
    std::vector<double> cash_share_; // per-parent cash booked on this bar (records only)
    std::vector<unsigned> clock_reset_; // children created from a ledger value on this bar
    std::vector<double> pre_settlement_mark_; // mark a settlement used, before its return scaled it
    size_t next_move_ = 0;           // cursor into data_.move_events, sorted by bar
    size_t next_leg_ = 0;            // cursor into data_.corporate_actions, sorted by bar
    unsigned bar_ = 0;
    unsigned bar_unconfirmed_count_ = 0;   // UnconfirmedRecord entries dated on the current bar
    double bar_unconfirmed_abs_ = 0.0;     // sum of |weight_before| over the distinct parents among them
    std::vector<uint8_t> unconfirmed_flag_;         // per asset: already counted in bar_unconfirmed_abs_ this bar
    std::vector<unsigned> bar_unconfirmed_assets_;  // assets flagged this bar (reset at the next bar)
    bool records_rows_ = false;
    bool has_actions_ = false;
    bool any_retired_ = false;
    bool any_transfer_ = false;
    bool any_received_ = false;
};
} // namespace

void normalize_signal_row(const DataPanel& data, unsigned di, const float* signal,
                          std::vector<double>& targets, MissingClosePolicy policy) {
    const float* close = data.row("close", di);
    const auto* mask = data.universe_mask.empty()
        ? nullptr : data.universe_mask.data() + static_cast<size_t>(di) * data.N;
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
    if (config_.mode != 0 && config_.mode != 1)
        throw std::invalid_argument("Simulator mode must be 0 or 1");
    if (!std::isfinite(config_.fee_rate) || config_.fee_rate < 0.0)
        throw std::invalid_argument("Simulator fee_rate must be finite and nonnegative");
    if (!std::isfinite(config_.default_delisting_return) ||
        config_.default_delisting_return < -1.0 || config_.default_delisting_return > 1.0)
        throw std::invalid_argument(
            "Simulator default_delisting_return must be a total return in [-1, 1]");
    if (config_.sim_start_index > data_.T)
        throw std::invalid_argument("Simulator start index exceeds the data panel");
    if (!data_.universe_mask.empty() &&
        data_.universe_mask.size() != static_cast<size_t>(data_.T) * data_.N)
        throw std::invalid_argument("Universe mask dimensions must match the data panel");
    if (!data_.settlement_bar.empty() && data_.settlement_bar.size() != data_.N)
        throw std::invalid_argument("Settlement bars must align with the asset panel");
    if (!data_.settlement_return.empty() && data_.settlement_return.size() != data_.N)
        throw std::invalid_argument("Settlement returns must align with the asset panel");
    if (!data_.settlement_successor.empty() && data_.settlement_successor.size() != data_.N)
        throw std::invalid_argument("Settlement successors must align with the asset panel");
    if (!data_.settlement_successor_ratio.empty() &&
        data_.settlement_successor_ratio.size() != data_.N)
        throw std::invalid_argument("Settlement successor ratios must align with the asset panel");
    if (!data_.settlement_cash.empty() && data_.settlement_cash.size() != data_.N)
        throw std::invalid_argument("Settlement cash amounts must align with the asset panel");
    for (const auto* flags : {&data_.settlement_has_stock_leg, &data_.settlement_has_cash_leg,
                              &data_.settlement_successor_named})
        if (!flags->empty() && flags->size() != data_.N)
            throw std::invalid_argument("Settlement leg flags must align with the asset panel");
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
    unsigned last_leg_bar = 0;
    for (const auto& leg : data_.corporate_actions) {
        if (leg.bar >= data_.T || leg.parent >= data_.N || leg.child < -1 ||
            leg.child >= static_cast<int32_t>(data_.N) ||
            leg.child == static_cast<int32_t>(leg.parent))
            throw std::invalid_argument("Corporate action legs must reference the asset panel");
        if (leg.bar < last_leg_bar)
            throw std::invalid_argument("Corporate action legs must be sorted by bar");
        if (std::isfinite(leg.child_ratio) && leg.child_ratio < 0.0)
            throw std::invalid_argument("Corporate action child ratios must be nonnegative");
        if (leg.expiry_bar < -1 || leg.expiry_bar > static_cast<int32_t>(data_.T) ||
            (leg.expiry_bar >= 0 && static_cast<unsigned>(leg.expiry_bar) < leg.bar))
            throw std::invalid_argument("Corporate action expiry bars must lie on or after the ex-date bar");
        last_leg_bar = leg.bar;
    }
}

SimulatorOutput Simulator::run(const std::vector<Module*>& pipeline) {
    PortfolioSimulation simulation(data_, config_);
    std::vector<float> signal_buffer(data_.N, 0.0f);
    SignalVector signal{signal_buffer.data(), data_.N};
    if (config_.collect_signals)
        simulation.output.signals.resize(data_.T);

    for (Module* module : pipeline) {
        module->numAssets = data_.N;
        module->init();
    }
    for (unsigned di = 0; di < data_.T; ++di) {
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

SimulatorOutput Simulator::run_from_signals(const std::vector<std::vector<float>>& signals) {
    if (signals.size() != config_.T ||
        std::any_of(signals.begin(), signals.end(), [this](const auto& row) {
            return row.size() != config_.N;
        }))
        throw std::invalid_argument("Signal matrix dimensions must match the data panel");

    PortfolioSimulation simulation(data_, config_);
    for (unsigned di = 0; di < data_.T; ++di)
        simulation.advance(di, signals[di].data());
    return std::move(simulation.output);
}
