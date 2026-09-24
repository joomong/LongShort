#include <gtest/gtest.h>
#include <json/json.h>
#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/writer.h>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <limits>
#include <string>
#include <vector>
#include <unistd.h>
#include "api/json_serializer.h"
#include "data/config_loader.h"
#include "data/data_panel.h"
#include "engine/simulator.h"
#include "metrics/timeseries_output.h"

// ── Corporate-action ledger accounting (docs/corporate_actions_engine_2026-09-17.md) ──
//
// Cash legs are realized against the parent's previous mark; stock legs create a
// child weight valued at the child's ex-date close. Every expectation below is a
// hand calculation from the float32 closes the engine sees.

namespace {
const float kMissing = std::numeric_limits<float>::quiet_NaN();
const double kNaN = std::numeric_limits<double>::quiet_NaN();
constexpr double kFee = 0.0005;
constexpr int64_t kDay = 86400000;

// Flat 100 prices; each case writes only the quotes it needs to move or remove.
DataPanel flat_panel(unsigned T, unsigned N) {
    DataPanel panel;
    panel.T = T;
    panel.N = N;
    panel.timestamps.resize(T);
    for (unsigned t = 0; t < T; ++t) panel.timestamps[t] = static_cast<int64_t>(t) * kDay;
    panel.symbols.resize(N);
    for (unsigned ii = 0; ii < N; ++ii) panel.symbols[ii] = "S" + std::to_string(ii);
    panel.arrays["close"].assign(static_cast<size_t>(T) * N, 100.0f);
    return panel;
}

DataPanel::CorporateActionLeg cash_leg(unsigned bar, unsigned parent, double cash) {
    DataPanel::CorporateActionLeg leg;
    leg.bar = bar;
    leg.parent = parent;
    leg.cash = cash;
    leg.event_type = "cash_dividend";
    return leg;
}

// `value_source` is the ledger's childValueSource: wi_close may price a child without
// a quote (P10 (2)); event_value / vendor_implied may too but are estimates ((3)(4)).
DataPanel::CorporateActionLeg stock_leg(unsigned bar, unsigned parent, int32_t child, double ratio,
                                        double child_close = kNaN,
                                        const std::string& value_source = "wi_close") {
    DataPanel::CorporateActionLeg leg;
    leg.bar = bar;
    leg.parent = parent;
    leg.child = child;
    leg.stock_leg = true;
    leg.child_ratio = ratio;
    leg.child_close = child_close;
    leg.event_type = "spin_off";
    leg.value_source = value_source;
    leg.ledger_value_usable = value_source == "wi_close" || value_source == "event_value" ||
                              value_source == "vendor_implied";
    leg.estimate = value_source == "event_value" || value_source == "vendor_implied";
    return leg;
}

struct AccountingLedger {
    std::vector<std::vector<double>> weights, trades, marks_before, marks_after;
    std::vector<std::vector<uint8_t>> retired;
    std::vector<double> gross, fee, net, turnover;
    std::vector<MissingDataDiagnostics> missing;
    std::vector<CorporateActionDiagnostics> actions;

    void record(const AccountingRowView& row) {
        weights.push_back(row.weights);
        trades.push_back(row.market_trades);
        marks_before.push_back(row.marks_before);
        marks_after.push_back(row.marks_after);
        retired.push_back(row.retired);
        gross.push_back(row.gross_pnl);
        fee.push_back(row.fee);
        net.push_back(row.net_pnl);
        turnover.push_back(row.turnover);
        missing.push_back(row.diagnostics);
        actions.push_back(row.corporate_actions);
    }
};

SimulatorConfig ledger_config(const DataPanel& panel, int mode, AccountingLedger& ledger) {
    SimulatorConfig config;
    config.T = panel.T;
    config.N = panel.N;
    config.fee_rate = kFee;
    config.mode = mode;
    config.accounting_row_callback = [&ledger](const AccountingRowView& row) { ledger.record(row); };
    config.collect_distributions = true;
    return config;
}

// Raw signals that make plan[t] the intent executed on bar t under either mode.
// D0 executes the current bar's signal, D1 the previous one; plan[0] must be flat.
std::vector<std::vector<float>> plan_signals(
    const std::vector<std::vector<float>>& plan, int mode) {
    if (mode == 0) return plan;
    std::vector<std::vector<float>> signals(plan.size(),
        std::vector<float>(plan.front().size(), 0.0f));
    for (std::size_t t = 1; t < plan.size(); ++t) signals[t - 1] = plan[t];
    return signals;
}

double close_of(const DataPanel& panel, unsigned bar, unsigned asset) {
    return static_cast<double>(panel.at("close", bar, asset));
}
}  // namespace

TEST(CorporateActions, LongAndShortCashDividendsBookAgainstThePreviousMark) {
    for (int mode : {0, 1}) {
        for (float side : {1.0f, -1.0f}) {
            // The $5 ex-dividend drop and the $5 cash leg cancel: cash-style return 0.
            auto panel = flat_panel(5, 2);
            for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 95.0f;
            panel.corporate_actions = {cash_leg(2, 0, 5.0)};
            AccountingLedger ledger;
            auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                           .run_from_signals(plan_signals(
                               std::vector<std::vector<float>>(panel.T, {side, 0}), mode));

            const double w = static_cast<double>(side);
            const double price_move = w * (close_of(panel, 2, 0) / 100.0 - 1.0);
            const double cash = w * 5.0 / 100.0;
            EXPECT_NEAR(ledger.gross[2], price_move + cash, 1e-12) << "mode=" << mode << " side=" << side;
            EXPECT_NEAR(ledger.gross[2], 0.0, 1e-12);
            EXPECT_NEAR(ledger.actions[2].dividend_pnl, cash, 1e-12);
            EXPECT_EQ(ledger.actions[2].dividend_count, 1u);
            EXPECT_EQ(ledger.actions[2].distribution_count, 0u);
            EXPECT_EQ(ledger.actions[2].child_positions_created, 0u);
            // No quantity change, no trade, no fee: the dividend is cash only (P3).
            EXPECT_NEAR(ledger.weights[2][0], w, 1e-12);
            EXPECT_NEAR(ledger.trades[2][0], 0.0, 1e-12);
            EXPECT_DOUBLE_EQ(out.turnover[2], 0.0);
            EXPECT_DOUBLE_EQ(ledger.fee[2], 0.0);
            EXPECT_NEAR(ledger.marks_after[2][0], close_of(panel, 2, 0), 1e-12);
            EXPECT_NEAR(out.ret[2], 0.0, 1e-12);
            EXPECT_NEAR(out.ret[3], 0.0, 1e-12);
            EXPECT_EQ(ledger.actions[3].dividend_count, 0u);
            EXPECT_TRUE(out.distributions.empty());   // cash legs are not distribution records
            EXPECT_EQ(out.corporate_actions[2].dividend_count, 1u);
        }
    }
}

// DD 2025-11-03: DD 81.65 -> 34.69, Q 97.00, 0.5 Q per DD share. Cash-style
// return (34.69 + 0.5 * 97.00) / 81.65 - 1 = +1.89% (the GS mark).
TEST(CorporateActions, SpinOffLongMatchesTheDdOracle) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 3);
        for (unsigned di = 0; di < 2; ++di) panel.at("close", di, 0) = 81.65f;
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 34.69f;
        for (unsigned di = 0; di < 2; ++di) panel.at("close", di, 1) = kMissing;  // Q lists on the ex-date
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 1) = 97.0f;
        panel.corporate_actions = {stock_leg(2, 0, 1, 0.5, 97.0)};
        std::vector<std::vector<float>> plan(panel.T, {1, 0, 0});
        plan[0] = {0, 0, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        const double prev = close_of(panel, 1, 0), ex = close_of(panel, 2, 0), q = close_of(panel, 2, 1);
        const double expected = (ex + 0.5 * q) / prev - 1.0;
        const double child_weight = 0.5 * q / prev;
        EXPECT_NEAR(ledger.gross[2], expected, 1e-9) << "mode=" << mode;
        EXPECT_NEAR(ledger.gross[2], 0.0189, 5e-5);           // the GS oracle, +1.89%
        EXPECT_NEAR(ledger.actions[2].distribution_pnl, child_weight, 1e-12);
        EXPECT_EQ(ledger.actions[2].distribution_count, 1u);
        EXPECT_EQ(ledger.actions[2].child_positions_created, 1u);
        EXPECT_EQ(ledger.actions[2].distribution_pending, 0u);
        EXPECT_EQ(ledger.actions[2].distribution_unpriced, 0u);
        // The child arrives without a trade and is marked at its ex-date close.
        EXPECT_NEAR(ledger.weights[2][1], child_weight, 1e-12);
        EXPECT_NEAR(ledger.trades[2][1], 0.0, 1e-12);
        EXPECT_DOUBLE_EQ(out.turnover[2], 0.0);
        EXPECT_NEAR(ledger.marks_after[2][1], q, 1e-12);
        EXPECT_EQ(out.position_count[2], 2);
        // No target for the child: sold at the next execution with the normal fee.
        EXPECT_NEAR(ledger.gross[3], 0.0, 1e-12);
        EXPECT_NEAR(ledger.trades[3][1], -child_weight, 1e-12);
        EXPECT_NEAR(out.turnover[3], child_weight, 1e-12);
        EXPECT_NEAR(ledger.fee[3], child_weight * kFee, 1e-12);
        EXPECT_NEAR(out.ret[3], -child_weight * kFee, 1e-12);
        EXPECT_NEAR(ledger.weights[3][1], 0.0, 1e-12);
        EXPECT_EQ(out.position_count[3], 1);
        ASSERT_EQ(out.distributions.size(), 1u);
        const auto& record = out.distributions[0];
        EXPECT_EQ(record.bar, 2u);
        EXPECT_EQ(record.ex_bar, 2u);
        EXPECT_EQ(record.parent, 0u);
        EXPECT_EQ(record.child, 1);
        EXPECT_NEAR(record.parent_weight, 1.0, 1e-12);
        EXPECT_NEAR(record.child_weight, child_weight, 1e-12);
        EXPECT_NEAR(record.cash_share, 0.0, 1e-12);
        EXPECT_EQ(record.source, DistributionSource::ChildClose);
    }
}

// HON 2026-06-29: -204 HON became -102 HON and -102 HONA, HON 232.21 -> 227.80,
// HONA 220.19. In raw terms 1 old share -> 0.5 HON + 0.5 HONA; the split-adjusted
// panel restates HON's history for the halving (232.21 -> 464.42 per new share),
// so in cache units the ratio is 1.0 HONA per adjusted share and the parent
// factor is 1. Cash-style: (227.80 + 220.19) / 464.42 - 1 = -3.54%, +3.54% short.
TEST(CorporateActions, SpinOffShortMatchesTheHonOracle) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 3);
        const float prev_adjusted = 2.0f * 232.21f;    // exact in float
        for (unsigned di = 0; di < 2; ++di) panel.at("close", di, 0) = prev_adjusted;
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 227.80f;
        for (unsigned di = 0; di < 2; ++di) panel.at("close", di, 1) = kMissing;
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 1) = 220.19f;
        panel.corporate_actions = {stock_leg(2, 0, 1, 1.0, 220.19)};
        // -204 HON against a 796 long filler: HON is -0.204 of the book.
        std::vector<std::vector<float>> plan(panel.T, {-204, 0, 796});
        plan[0] = {0, 0, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        const double w = -204.0 / 1000.0;
        const double prev = close_of(panel, 1, 0), ex = close_of(panel, 2, 0), hona = close_of(panel, 2, 1);
        const double cash_style = (ex + 1.0 * hona) / prev - 1.0;
        EXPECT_NEAR(ledger.gross[2], w * cash_style, 1e-9) << "mode=" << mode;
        // Same number in raw terms: 0.5 HON + 0.5 HONA per old share against 232.21.
        const double raw_style = (0.5 * ex + 0.5 * hona) / static_cast<double>(232.21f) - 1.0;
        EXPECT_NEAR(ledger.gross[2] / w, raw_style, 1e-9);
        EXPECT_NEAR(ledger.gross[2] / std::fabs(w), 0.0354, 1e-4);   // the GS oracle, +3.5% short
        EXPECT_GT(ledger.gross[2], 0.0);
        const double child_weight = w * hona / prev;                  // negative: -102 HONA
        EXPECT_NEAR(ledger.weights[2][1], child_weight, 1e-12);
        EXPECT_LT(ledger.weights[2][1], 0.0);
        EXPECT_NEAR(ledger.weights[2][0], w, 1e-12);
        EXPECT_NEAR(ledger.actions[2].distribution_pnl, child_weight, 1e-12);
        EXPECT_EQ(ledger.actions[2].child_positions_created, 1u);
        EXPECT_DOUBLE_EQ(out.turnover[2], 0.0);
        // The short child is bought back at the next execution with the normal fee.
        EXPECT_NEAR(ledger.trades[3][1], -child_weight, 1e-12);
        EXPECT_NEAR(out.turnover[3], std::fabs(child_weight), 1e-12);
        EXPECT_NEAR(out.ret[3], -std::fabs(child_weight) * kFee, 1e-12);
        ASSERT_EQ(out.distributions.size(), 1u);
        EXPECT_NEAR(out.distributions[0].parent_weight, w, 1e-12);
        EXPECT_NEAR(out.distributions[0].child_weight, child_weight, 1e-12);
    }
}

TEST(CorporateActions, ReceivedSharesNetAgainstAnExistingChildWeight) {
    for (int mode : {0, 1}) {
        // Long parent 0.5, short child 0.5; 0.2 child shares at 50 per parent share at 100.
        auto panel = flat_panel(5, 2);
        for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = 50.0f;
        panel.corporate_actions = {stock_leg(2, 0, 1, 0.2)};
        std::vector<std::vector<float>> plan(panel.T, {1, -1});
        plan[0] = {0, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        const double child_weight = 0.5 * 0.2 * 50.0 / 100.0;   // +0.05
        EXPECT_NEAR(ledger.gross[2], child_weight, 1e-12) << "mode=" << mode;
        // The strategy's own -0.5 executes as usual; the received +0.05 nets on top.
        EXPECT_NEAR(ledger.weights[2][1], -0.5 + child_weight, 1e-12);
        EXPECT_NEAR(ledger.trades[2][1], 0.0, 1e-12);
        EXPECT_DOUBLE_EQ(out.turnover[2], 0.0);
        EXPECT_EQ(out.position_count[2], 2);
        // The next execution restores the -0.5 target by selling the received shares.
        EXPECT_NEAR(ledger.trades[3][1], -child_weight, 1e-12);
        EXPECT_NEAR(ledger.weights[3][1], -0.5, 1e-12);
        EXPECT_NEAR(out.turnover[3], child_weight, 1e-12);
        EXPECT_NEAR(ledger.fee[3], child_weight * kFee, 1e-12);
        EXPECT_NEAR(ledger.gross[3], 0.0, 1e-12);
    }
}

TEST(CorporateActions, FractionalRatioAndSameDayCashPlusStockAddUpToTheCashStyleReturn) {
    for (int mode : {0, 1}) {
        for (bool cash_first : {true, false}) {
            // $4 cash plus 0.3 child shares at 20 (= $6) explain a 100 -> 90 ex-date close.
            auto panel = flat_panel(5, 2);
            for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 90.0f;
            for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = 20.0f;
            if (cash_first)
                panel.corporate_actions = {cash_leg(2, 0, 4.0), stock_leg(2, 0, 1, 0.3)};
            else
                panel.corporate_actions = {stock_leg(2, 0, 1, 0.3), cash_leg(2, 0, 4.0)};
            std::vector<std::vector<float>> plan(panel.T, {1, 0});
            plan[0] = {0, 0};
            AccountingLedger ledger;
            auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                           .run_from_signals(plan_signals(plan, mode));

            const double child_weight = 0.3 * 20.0 / 100.0;   // 0.06
            EXPECT_NEAR(ledger.gross[2], (90.0 + 4.0 + 0.3 * 20.0) / 100.0 - 1.0, 1e-12)
                << "mode=" << mode << " cash_first=" << cash_first;
            EXPECT_NEAR(ledger.gross[2], 0.0, 1e-12);
            EXPECT_NEAR(ledger.actions[2].dividend_pnl, 0.04, 1e-12);
            EXPECT_NEAR(ledger.actions[2].distribution_pnl, child_weight, 1e-12);
            EXPECT_EQ(ledger.actions[2].dividend_count, 1u);
            EXPECT_EQ(ledger.actions[2].distribution_count, 1u);
            EXPECT_NEAR(ledger.weights[2][1], child_weight, 1e-12);
            ASSERT_EQ(out.distributions.size(), 1u);
            // The record carries the cash booked beside it whatever the file order.
            EXPECT_NEAR(out.distributions[0].cash_share, 0.04, 1e-12);
            EXPECT_NEAR(out.distributions[0].child_weight, child_weight, 1e-12);
        }
    }
}

TEST(CorporateActions, ChildTargetsAndLiquidationFollowTheNormalExecutionRules) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 2);
        for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = 50.0f;
        panel.corporate_actions = {stock_leg(2, 0, 1, 0.4)};   // 0.4 * 50 / 100 = 0.2 of the book
        // Not wanted: the received 0.2 is sold on the next bar with a fee.
        std::vector<std::vector<float>> unwanted(panel.T, {1, 0});
        unwanted[0] = {0, 0};
        AccountingLedger sold;
        auto sold_out = Simulator(panel, ledger_config(panel, mode, sold))
                            .run_from_signals(plan_signals(unwanted, mode));
        EXPECT_NEAR(sold.weights[2][1], 0.2, 1e-12) << "mode=" << mode;
        EXPECT_DOUBLE_EQ(sold_out.turnover[2], 0.0);
        EXPECT_NEAR(sold_out.turnover[3], 0.2, 1e-12);
        EXPECT_NEAR(sold.fee[3], 0.2 * kFee, 1e-12);
        EXPECT_NEAR(sold_out.ret[3], -0.2 * kFee, 1e-12);
        EXPECT_NEAR(sold.weights[3][1], 0.0, 1e-12);
        EXPECT_DOUBLE_EQ(sold_out.turnover[4], 0.0);

        // Wanted at exactly the received size from bar 3 on: only the parent trims.
        std::vector<std::vector<float>> wanted = unwanted;
        for (unsigned di = 3; di < panel.T; ++di) wanted[di] = {80, 20};
        AccountingLedger kept;
        auto kept_out = Simulator(panel, ledger_config(panel, mode, kept))
                            .run_from_signals(plan_signals(wanted, mode));
        EXPECT_NEAR(kept.weights[3][0], 0.8, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(kept.weights[3][1], 0.2, 1e-12);
        EXPECT_NEAR(kept.trades[3][1], 0.0, 1e-12);
        EXPECT_NEAR(kept_out.turnover[3], 0.2, 1e-12);   // parent 1.0 -> 0.8 only
        EXPECT_NEAR(kept.fee[3], 0.2 * kFee, 1e-12);

        // A missing child quote after the ex-date freezes the received weight
        // (carry): no liquidation is assumed, and like any frozen holding it
        // takes 0.2 out of the budget, so the parent target shrinks to 0.8.
        auto frozen_panel = panel;
        frozen_panel.at("close", 3, 1) = kMissing;
        AccountingLedger frozen;
        auto frozen_out = Simulator(frozen_panel, ledger_config(frozen_panel, mode, frozen))
                              .run_from_signals(plan_signals(unwanted, mode));
        EXPECT_NEAR(frozen.weights[3][1], 0.2, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(frozen.trades[3][1], 0.0, 1e-12);
        EXPECT_EQ(frozen_out.missing_data[3].missing_held_count, 1u);
        EXPECT_EQ(frozen_out.missing_data[3].blocked_order_count, 1u);
        EXPECT_NEAR(frozen_out.missing_data[3].frozen_gross, 0.2, 1e-12);
        EXPECT_NEAR(frozen.weights[3][0], 0.8, 1e-12);
        EXPECT_NEAR(frozen_out.turnover[3], 0.2, 1e-12);
        EXPECT_NEAR(frozen_out.turnover[4], 0.4, 1e-12);   // child sold, parent refilled
        EXPECT_NEAR(frozen.weights[4][1], 0.0, 1e-12);
    }
}

TEST(CorporateActions, PendingChildIsCreatedAtItsFirstQuoteWithTheExDateEntitlement) {
    for (int mode : {0, 1}) {
        // The child prints nothing on the ex-date and 40 from the next bar on.
        auto panel = flat_panel(6, 2);
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 80.0f;
        for (unsigned di = 0; di < 3; ++di) panel.at("close", di, 1) = kMissing;
        for (unsigned di = 3; di < panel.T; ++di) panel.at("close", di, 1) = 40.0f;
        panel.corporate_actions = {stock_leg(2, 0, 1, 0.5)};
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        // Ex-date: the parent's drop is booked, the child's value is not guessed.
        EXPECT_NEAR(ledger.gross[2], -0.2, 1e-12) << "mode=" << mode;
        EXPECT_EQ(ledger.actions[2].distribution_count, 1u);
        EXPECT_EQ(ledger.actions[2].distribution_pending, 1u);
        EXPECT_EQ(ledger.actions[2].child_positions_created, 0u);
        EXPECT_NEAR(ledger.weights[2][1], 0.0, 1e-12);
        // First quote: 0.5 * 40 / 100 against the ex-date parent weight and mark.
        const double child_weight = 0.5 * 40.0 / 100.0;
        EXPECT_NEAR(ledger.gross[3], child_weight, 1e-12);
        EXPECT_EQ(ledger.actions[3].distribution_pending, 0u);
        EXPECT_EQ(ledger.actions[3].child_positions_created, 1u);
        EXPECT_NEAR(ledger.actions[3].distribution_pnl, child_weight, 1e-12);
        EXPECT_NEAR(ledger.weights[3][1], child_weight, 1e-12);
        EXPECT_NEAR(ledger.trades[3][1], 0.0, 1e-12);
        EXPECT_NEAR(ledger.marks_after[3][1], 40.0, 1e-12);
        EXPECT_NEAR(out.turnover[4], child_weight, 1e-12);   // liquidated on the bar after
        ASSERT_EQ(out.distributions.size(), 1u);
        EXPECT_EQ(out.distributions[0].bar, 3u);
        EXPECT_EQ(out.distributions[0].ex_bar, 2u);
        EXPECT_EQ(out.distributions[0].source, DistributionSource::FirstQuote);

        // The entitlement belongs to the ex-date holding even if the parent is sold before.
        std::vector<std::vector<float>> exit = plan;
        for (unsigned di = 3; di < panel.T; ++di) exit[di] = {0, 0};
        AccountingLedger exited;
        auto exited_out = Simulator(panel, ledger_config(panel, mode, exited))
                              .run_from_signals(plan_signals(exit, mode));
        EXPECT_NEAR(exited.actions[3].distribution_pnl, child_weight, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(exited.weights[3][0], 0.0, 1e-12);
        EXPECT_NEAR(exited.weights[3][1], child_weight, 1e-12);
        EXPECT_NEAR(exited_out.turnover[3], 1.0, 1e-12);   // the parent sale only
    }
}

TEST(CorporateActions, PendingLegsThatNeverQuoteStayPendingOrDropByPolicy) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 2);
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 80.0f;
        for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = kMissing;
        panel.corporate_actions = {stock_leg(2, 0, 1, 0.5)};
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        AccountingLedger waiting;
        auto out = Simulator(panel, ledger_config(panel, mode, waiting))
                       .run_from_signals(plan_signals(plan, mode));
        for (unsigned di = 2; di < panel.T; ++di) {
            EXPECT_EQ(out.corporate_actions[di].distribution_pending, 1u) << "mode=" << mode << " bar=" << di;
            EXPECT_EQ(out.corporate_actions[di].child_positions_created, 0u);
            EXPECT_NEAR(waiting.weights[di][1], 0.0, 1e-12);
        }
        EXPECT_EQ(out.corporate_actions.back().distribution_pending, 1u);   // terminal pending
        EXPECT_TRUE(out.distributions.empty());
        EXPECT_NEAR(waiting.gross[2], -0.2, 1e-12);

        // drop: the same leg is abandoned on the ex-date and reported as unpriced.
        AccountingLedger dropped;
        auto drop_config = ledger_config(panel, mode, dropped);
        drop_config.unpriced_child = UnpricedChildPolicy::Drop;
        auto drop_out = Simulator(panel, drop_config).run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(drop_out.corporate_actions[2].distribution_unpriced, 1u) << "mode=" << mode;
        EXPECT_EQ(drop_out.corporate_actions[2].distribution_pending, 0u);
        EXPECT_EQ(drop_out.corporate_actions.back().distribution_pending, 0u);
        ASSERT_EQ(drop_out.distributions.size(), 1u);
        EXPECT_EQ(drop_out.distributions[0].source, DistributionSource::Unpriced);
        EXPECT_NEAR(drop_out.distributions[0].child_weight, 0.0, 1e-12);

        // A child that retires while the leg waits can no longer receive anything.
        auto retiring = panel;
        retiring.settlement_bar = {-1, 3};
        AccountingLedger retired;
        auto retired_out = Simulator(retiring, ledger_config(retiring, mode, retired))
                               .run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(retired_out.corporate_actions[2].distribution_pending, 1u) << "mode=" << mode;
        EXPECT_EQ(retired_out.corporate_actions[3].distribution_unpriced, 1u);
        EXPECT_EQ(retired_out.corporate_actions[3].distribution_pending, 0u);
        EXPECT_EQ(retired_out.missing_data[3].retirement_count, 1u);
    }
}

TEST(CorporateActions, UnpricedChildrenAreReportedAndRecogniseNoValue) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(5, 2);
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 80.0f;
        // Outside the panel (child -1) and without a ratio: both stay unpriced.
        for (auto leg : {stock_leg(2, 0, -1, 0.5), stock_leg(2, 0, 1, kNaN)}) {
            panel.corporate_actions = {leg};
            std::vector<std::vector<float>> plan(panel.T, {1, 0});
            plan[0] = {0, 0};
            AccountingLedger ledger;
            auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                           .run_from_signals(plan_signals(plan, mode));
            EXPECT_NEAR(ledger.gross[2], -0.2, 1e-12) << "mode=" << mode;
            EXPECT_EQ(ledger.actions[2].distribution_count, 1u);
            EXPECT_EQ(ledger.actions[2].distribution_unpriced, 1u);
            EXPECT_EQ(ledger.actions[2].distribution_pending, 0u);
            EXPECT_EQ(ledger.actions[2].child_positions_created, 0u);
            EXPECT_NEAR(ledger.weights[2][1], 0.0, 1e-12);
            EXPECT_EQ(out.position_count[2], 1);
            ASSERT_EQ(out.distributions.size(), 1u);
            EXPECT_EQ(out.distributions[0].source, DistributionSource::Unpriced);
            EXPECT_EQ(out.distributions[0].child, leg.child);
            EXPECT_NEAR(out.distributions[0].parent_weight, 1.0, 1e-12);
        }
    }
}

// A when-issued value in the ledger prices a child that has never printed; the
// first real quote then earns the move from that value like a resumed holding.
TEST(CorporateActions, LedgerValuePricesAChildThatHasNeverQuoted) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 2);
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 80.0f;
        for (unsigned di = 0; di < 3; ++di) panel.at("close", di, 1) = kMissing;
        for (unsigned di = 3; di < panel.T; ++di) panel.at("close", di, 1) = 44.0f;
        panel.corporate_actions = {stock_leg(2, 0, 1, 0.5, 40.0)};
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        const double child_weight = 0.5 * 40.0 / 100.0;
        EXPECT_NEAR(ledger.gross[2], -0.2 + child_weight, 1e-12) << "mode=" << mode;
        EXPECT_EQ(ledger.actions[2].child_positions_created, 1u);
        EXPECT_EQ(ledger.actions[2].distribution_pending, 0u);
        EXPECT_NEAR(ledger.weights[2][1], child_weight, 1e-12);
        EXPECT_NEAR(ledger.marks_after[2][1], 40.0, 1e-12);
        ASSERT_EQ(out.distributions.size(), 1u);
        EXPECT_EQ(out.distributions[0].source, DistributionSource::LedgerValue);
        // 40 -> 44 on the first real quote, then sold at the next execution.
        EXPECT_NEAR(ledger.gross[3], child_weight * (44.0 / 40.0 - 1.0), 1e-12);
        EXPECT_NEAR(out.turnover[3], child_weight, 1e-12);
        EXPECT_NEAR(ledger.weights[3][1], 0.0, 1e-12);
    }
}

TEST(CorporateActions, LegsOnUnheldWarmupOrRetiredParentsBookNothing) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 3);
        for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = 50.0f;
        panel.corporate_actions = {cash_leg(1, 0, 5.0), stock_leg(1, 0, 1, 0.5),   // warmup
                                   cash_leg(3, 2, 5.0), stock_leg(3, 2, 1, 0.5)};  // unheld parent
        std::vector<std::vector<float>> plan(panel.T, {1, 0, 0});
        plan[0] = {0, 0, 0};
        AccountingLedger ledger;
        auto config = ledger_config(panel, mode, ledger);
        config.sim_start_index = 2;
        auto out = Simulator(panel, config).run_from_signals(plan_signals(plan, mode));
        for (unsigned di = 0; di < panel.T; ++di) {
            EXPECT_EQ(out.corporate_actions[di].dividend_count, 0u) << "mode=" << mode << " bar=" << di;
            EXPECT_EQ(out.corporate_actions[di].distribution_count, 0u);
            EXPECT_EQ(out.corporate_actions[di].distribution_pending, 0u);
            EXPECT_NEAR(out.corporate_actions[di].dividend_pnl, 0.0, 1e-12);
            EXPECT_NEAR(out.corporate_actions[di].distribution_pnl, 0.0, 1e-12);
        }
        EXPECT_TRUE(out.distributions.empty());
        EXPECT_NEAR(out.ret[2], -kFee, 1e-12);   // the first entry fee only
        EXPECT_NEAR(out.ret[3], 0.0, 1e-12);

        // A parent settled on its own ex-date bar pays against the pre-settlement
        // exposure and mark (the events row carries no cash of its own here); a leg
        // dated after the retirement finds no exposure and books nothing.
        auto settled = flat_panel(5, 2);
        settled.at("close", 2, 0) = kMissing;
        settled.at("close", 3, 0) = kMissing;
        settled.settlement_bar = {2, -1};
        settled.corporate_actions = {cash_leg(2, 0, 5.0), cash_leg(3, 0, 5.0)};
        AccountingLedger settled_ledger;
        auto settled_out = Simulator(settled, ledger_config(settled, mode, settled_ledger))
                               .run_from_signals(plan_signals(
                                   std::vector<std::vector<float>>(settled.T, {1, 0}), mode));
        EXPECT_EQ(settled_out.missing_data[2].delisting_count, 1u) << "mode=" << mode;
        EXPECT_EQ(settled_out.corporate_actions[2].dividend_count, 1u);
        EXPECT_EQ(settled_out.corporate_actions[2].legs_on_settlement_bar, 1u);
        EXPECT_EQ(settled_out.corporate_actions[2].legs_deferred_to_events, 0u);
        EXPECT_NEAR(settled_out.corporate_actions[2].dividend_pnl, 5.0 / 100.0, 1e-12);
        EXPECT_NEAR(settled_ledger.gross[2], 0.0 + 0.05, 1e-12);   // default R 0 plus the ledger cash
        EXPECT_EQ(settled_out.corporate_actions[3].dividend_count, 0u);
        EXPECT_EQ(settled_out.corporate_actions[3].legs_on_settlement_bar, 0u);
    }
}

TEST(CorporateActions, ApplyFlagsSwitchEachLegOffAndLeaveTheRestBitForBit) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 3);
        for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = 50.0f;
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 90.0f;
        std::vector<std::vector<float>> plan(panel.T, {1, 0, 1});
        plan[0] = {0, 0, 0};
        const auto signals = plan_signals(plan, mode);
        SimulatorConfig bare{panel.T, panel.N, kFee, mode, false, {}};
        const auto expected = Simulator(panel, bare).run_from_signals(signals);

        auto with_legs = panel;
        with_legs.corporate_actions = {cash_leg(2, 0, 5.0), stock_leg(2, 0, 1, 0.1)};
        SimulatorConfig off = bare;
        off.apply_dividends = false;
        off.apply_distributions = false;
        const auto disabled = Simulator(with_legs, off).run_from_signals(signals);
        EXPECT_EQ(disabled.ret, expected.ret) << "mode=" << mode;
        EXPECT_EQ(disabled.turnover, expected.turnover);
        EXPECT_EQ(disabled.position_count, expected.position_count);
        for (const auto& bar : disabled.corporate_actions) {
            EXPECT_EQ(bar.dividend_count, 0u);
            EXPECT_EQ(bar.distribution_count, 0u);
        }

        SimulatorConfig cash_only = bare;
        cash_only.apply_distributions = false;
        const auto dividends = Simulator(with_legs, cash_only).run_from_signals(signals);
        EXPECT_NEAR(dividends.ret[2] - expected.ret[2], 0.5 * 5.0 / 100.0, 1e-12);
        EXPECT_EQ(dividends.corporate_actions[2].distribution_count, 0u);

        SimulatorConfig stock_only = bare;
        stock_only.apply_dividends = false;
        const auto spins = Simulator(with_legs, stock_only).run_from_signals(signals);
        EXPECT_NEAR(spins.ret[2] - expected.ret[2], 0.5 * 0.1 * 50.0 / 100.0, 1e-12);
        EXPECT_EQ(spins.corporate_actions[2].dividend_count, 0u);
    }
}

TEST(CorporateActions, RejectsLegsThatDoNotReferenceThePanelInOrder) {
    auto panel = flat_panel(4, 2);
    SimulatorConfig config{panel.T, panel.N, kFee, 1, false, {}};
    panel.corporate_actions = {cash_leg(4, 0, 1.0)};             // bar out of range
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.corporate_actions = {cash_leg(1, 2, 1.0)};             // parent out of range
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.corporate_actions = {stock_leg(1, 0, 2, 1.0)};         // child out of range
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.corporate_actions = {stock_leg(1, 0, 0, 1.0)};         // child is the parent
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.corporate_actions = {stock_leg(1, 0, 1, -1.0)};        // negative ratio
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.corporate_actions = {cash_leg(2, 0, 1.0), cash_leg(1, 0, 1.0)};   // unsorted
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.corporate_actions = {cash_leg(1, 0, 1.0), cash_leg(1, 1, 1.0), stock_leg(2, 0, 1, 0.5)};
    EXPECT_NO_THROW(Simulator(panel, config));
}

// P10: a ledger value that is a flagged estimate ((3)(4)) is booked as such and
// counted separately; a when-issued close ((2)) is a price. A child_close_ex value
// cannot price a child the panel does not quote, so that leg waits instead.
TEST(CorporateActions, EstimatedLedgerValuesAreBookedWithTheFlagAndCacheClosesAreNot) {
    for (int mode : {0, 1}) {
        for (const char* source : {"vendor_implied", "event_value", "wi_close", "child_close_ex"}) {
            auto panel = flat_panel(6, 2);
            for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 80.0f;
            for (unsigned di = 0; di < 3; ++di) panel.at("close", di, 1) = kMissing;
            for (unsigned di = 3; di < panel.T; ++di) panel.at("close", di, 1) = 44.0f;
            panel.corporate_actions = {stock_leg(2, 0, 1, 0.5, 40.0, source)};
            std::vector<std::vector<float>> plan(panel.T, {1, 0});
            plan[0] = {0, 0};
            AccountingLedger ledger;
            auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                           .run_from_signals(plan_signals(plan, mode));
            const bool usable = std::string(source) != "child_close_ex";
            const bool estimate = std::string(source) == "vendor_implied" ||
                                  std::string(source) == "event_value";
            ASSERT_EQ(out.distributions.size(), 1u) << source;
            const auto& record = out.distributions[0];
            EXPECT_EQ(record.value_source, source);
            EXPECT_EQ(record.event_type, "spin_off");
            if (usable) {
                EXPECT_EQ(record.source, DistributionSource::LedgerValue) << source << " mode=" << mode;
                EXPECT_EQ(record.bar, 2u);
                EXPECT_EQ(record.estimate, estimate);
                EXPECT_EQ(ledger.actions[2].distribution_estimated, estimate ? 1u : 0u);
                EXPECT_NEAR(ledger.gross[2], -0.2 + 0.5 * 40.0 / 100.0, 1e-12);
            } else {
                // Not a ledger value: pending, created at the first real quote.
                EXPECT_EQ(record.source, DistributionSource::FirstQuote) << source << " mode=" << mode;
                EXPECT_EQ(record.bar, 3u);
                EXPECT_FALSE(record.estimate);
                EXPECT_EQ(ledger.actions[2].distribution_pending, 1u);
                EXPECT_EQ(ledger.actions[2].distribution_estimated, 0u);
                EXPECT_NEAR(ledger.gross[2], -0.2, 1e-12);
                EXPECT_NEAR(ledger.gross[3], 0.5 * 44.0 / 100.0, 1e-12);
            }
            EXPECT_EQ(ledger.actions[3].distribution_estimated, 0u);
        }
        // A quoted child is priced from the quote even when the leg is flagged: the
        // booked value comes from the panel, but the flag (an estimated ratio or date
        // behind the row) stays on the record and the leg is reported as an estimate.
        auto quoted = flat_panel(5, 2);
        for (unsigned di = 0; di < quoted.T; ++di) quoted.at("close", di, 1) = 50.0f;
        quoted.corporate_actions = {stock_leg(2, 0, 1, 0.5, 48.0, "vendor_implied")};
        std::vector<std::vector<float>> plan(quoted.T, {1, 0});
        plan[0] = {0, 0};
        AccountingLedger ledger;
        auto out = Simulator(quoted, ledger_config(quoted, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));
        ASSERT_EQ(out.distributions.size(), 1u);
        EXPECT_EQ(out.distributions[0].source, DistributionSource::ChildClose);
        EXPECT_TRUE(out.distributions[0].estimate);
        EXPECT_EQ(out.distributions[0].value_source, "vendor_implied");
        EXPECT_NEAR(out.distributions[0].child_weight, 0.5 * 50.0 / 100.0, 1e-12);   // the quote, not 48
        EXPECT_EQ(ledger.actions[2].distribution_estimated, 1u);
        ASSERT_EQ(out.unconfirmed.size(), 1u);
        EXPECT_EQ(out.unconfirmed[0].kind, UnconfirmedKind::EstimateValue);
        EXPECT_EQ(out.unconfirmed[0].detail, "child_close:value");   // no reason column: the valued tier
    }
}

// Unresolved ledger rows are visible on held parents and book nothing.
TEST(CorporateActions, UnresolvedLegsAreCountedAndNeverBooked) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(5, 2);
        auto no_amount = cash_leg(2, 0, kNaN);
        no_amount.unresolved = true;
        auto no_child = stock_leg(2, 0, -1, kNaN);
        no_child.unresolved = true;
        panel.corporate_actions = {no_amount, no_child};
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(ledger.actions[2].legs_unresolved, 2u) << "mode=" << mode;
        EXPECT_EQ(ledger.actions[2].dividend_count, 0u);
        EXPECT_NEAR(ledger.actions[2].dividend_pnl, 0.0, 1e-12);
        EXPECT_EQ(ledger.actions[2].distribution_count, 1u);
        EXPECT_EQ(ledger.actions[2].distribution_unpriced, 1u);
        EXPECT_NEAR(ledger.gross[2], 0.0, 1e-12);
        EXPECT_NEAR(out.ret[2], 0.0, 1e-12);
        // An unheld parent leaves the counters at zero.
        std::vector<std::vector<float>> unheld(panel.T, {0, 1});
        unheld[0] = {0, 0};
        AccountingLedger none;
        Simulator(panel, ledger_config(panel, mode, none)).run_from_signals(plan_signals(unheld, mode));
        EXPECT_EQ(none.actions[2].legs_unresolved, 0u);
    }
}

// The error policy cannot hold an unquoted name, so a when-issued child waits for
// its first quote instead of being created from the ledger value; the run goes on.
TEST(CorporateActions, ErrorPolicyWaitsForTheFirstQuoteInsteadOfHoldingALedgerValue) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 2);
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 80.0f;
        for (unsigned di = 0; di < 4; ++di) panel.at("close", di, 1) = kMissing;   // two silent bars
        for (unsigned di = 4; di < panel.T; ++di) panel.at("close", di, 1) = 44.0f;
        panel.corporate_actions = {stock_leg(2, 0, 1, 0.5, 40.0, "wi_close")};
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        AccountingLedger ledger;
        auto config = ledger_config(panel, mode, ledger);
        config.missing_close_policy = MissingClosePolicy::Error;
        SimulatorOutput out;
        ASSERT_NO_THROW(out = Simulator(panel, config).run_from_signals(plan_signals(plan, mode)))
            << "mode=" << mode;
        EXPECT_EQ(ledger.actions[2].distribution_pending, 1u);
        EXPECT_EQ(ledger.actions[3].distribution_pending, 1u);
        EXPECT_EQ(ledger.actions[4].child_positions_created, 1u);
        EXPECT_NEAR(ledger.gross[4], 0.5 * 44.0 / 100.0, 1e-12);
        ASSERT_EQ(out.distributions.size(), 1u);
        EXPECT_EQ(out.distributions[0].source, DistributionSource::FirstQuote);
        // Under carry the same leg is created from the value and frozen until the quote.
        AccountingLedger carried;
        auto carry_out = Simulator(panel, ledger_config(panel, mode, carried))
                             .run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(carried.actions[2].child_positions_created, 1u);
        EXPECT_EQ(carry_out.missing_data[3].missing_held_count, 1u);
        EXPECT_NEAR(carried.gross[4], 0.2 * (44.0 / 40.0 - 1.0), 1e-12);
    }
}

// The silence a stale-quote settlement counts starts after the creation bar.
TEST(CorporateActions, LedgerValueChildStartsTheStaleQuoteClockAfterItsCreation) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(7, 2);
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 80.0f;
        for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = kMissing;   // never quotes
        panel.corporate_actions = {stock_leg(2, 0, 1, 0.5, 40.0, "wi_close")};
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        AccountingLedger ledger;
        auto config = ledger_config(panel, mode, ledger);
        config.stale_quote_settle_bars = 2;
        config.default_delisting_return = -1.0;
        auto out = Simulator(panel, config).run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(ledger.actions[2].child_positions_created, 1u) << "mode=" << mode;
        EXPECT_NEAR(ledger.weights[2][1], 0.2, 1e-12);
        // Bars 3 and 4 are the two silent bars; the settlement lands on bar 4, not 3.
        EXPECT_EQ(out.missing_data[3].stale_settlement_count, 0u);
        EXPECT_EQ(out.missing_data[3].missing_held_count, 1u);
        EXPECT_EQ(out.missing_data[4].stale_settlement_count, 1u);
        EXPECT_NEAR(ledger.gross[4], -0.2, 1e-12);   // the -1 default return on 0.2
    }
}

// Cases the independent review probed: several parents feeding one child, the
// child's own same-day cash, a child target on the ex-date, a pending leg that
// outlives its parent, and a parent with no quote on the ex-date.
TEST(CorporateActions, SeveralParentsAndTheChildsOwnCashOnTheSameBar) {
    for (int mode : {0, 1}) {
        // Long 0.6 of S0 and short 0.4 of S1 both distribute S2 (0.5 per share at 50).
        auto panel = flat_panel(5, 3);
        for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 2) = 50.0f;
        panel.corporate_actions = {stock_leg(2, 0, 2, 0.5), stock_leg(2, 1, 2, 0.5)};
        std::vector<std::vector<float>> plan(panel.T, {6, -4, 0});
        plan[0] = {0, 0, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));
        const double received = (0.6 - 0.4) * 0.5 * 50.0 / 100.0;   // net +0.05
        EXPECT_NEAR(ledger.weights[2][2], received, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(ledger.gross[2], received, 1e-12);
        EXPECT_EQ(ledger.actions[2].child_positions_created, 2u);
        EXPECT_EQ(out.distributions.size(), 2u);

        // The child pays its own cash on the bar it is received: only the child weight
        // held into the bar (-0.5 here) is paid, never the shares received that day.
        auto same_day = flat_panel(5, 2);
        for (unsigned di = 0; di < same_day.T; ++di) same_day.at("close", di, 1) = 50.0f;
        same_day.corporate_actions = {cash_leg(2, 1, 1.0), stock_leg(2, 0, 1, 0.2)};
        std::vector<std::vector<float>> pair(same_day.T, {1, -1});
        pair[0] = {0, 0};
        AccountingLedger both;
        Simulator(same_day, ledger_config(same_day, mode, both)).run_from_signals(plan_signals(pair, mode));
        EXPECT_NEAR(both.actions[2].dividend_pnl, -0.5 * 1.0 / 50.0, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(both.actions[2].distribution_pnl, 0.5 * 0.2 * 50.0 / 100.0, 1e-12);
        EXPECT_NEAR(both.gross[2], -0.01 + 0.05, 1e-12);
    }
}

TEST(CorporateActions, ExDateChildTargetsAndParentSettlementsKeepTheEntitlement) {
    for (int mode : {0, 1}) {
        // The strategy wants 0.3 of the child on the ex-date: it trades that, and the
        // received 0.2 (1.0 held into the bar * 0.4 * 50 / 100) rides on top until the
        // next execution.
        auto panel = flat_panel(5, 2);
        for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = 50.0f;
        panel.corporate_actions = {stock_leg(2, 0, 1, 0.4)};
        std::vector<std::vector<float>> plan(panel.T, {7, 3});
        plan[0] = {0, 0};
        plan[1] = {1, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(ledger.trades[2][1], 0.3, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(ledger.weights[2][1], 0.3 + 0.2, 1e-12);
        EXPECT_NEAR(out.turnover[2], 0.3 + 0.3, 1e-12);      // parent 1 -> 0.7, child 0 -> 0.3
        EXPECT_NEAR(ledger.trades[3][1], -0.2, 1e-12);
        EXPECT_NEAR(ledger.weights[3][1], 0.3, 1e-12);

        // The parent settles (events) after the ex-date while the child is still
        // unquoted: the entitlement was earned on the ex-date and is created later.
        auto settled = flat_panel(6, 2);
        for (unsigned di = 3; di < settled.T; ++di) settled.at("close", di, 0) = kMissing;
        for (unsigned di = 0; di < 4; ++di) settled.at("close", di, 1) = kMissing;
        for (unsigned di = 4; di < settled.T; ++di) settled.at("close", di, 1) = 40.0f;
        settled.settlement_bar = {3, -1};
        settled.corporate_actions = {stock_leg(2, 0, 1, 0.5)};
        std::vector<std::vector<float>> hold(settled.T, {1, 0});
        hold[0] = {0, 0};
        AccountingLedger outlived;
        auto settled_out = Simulator(settled, ledger_config(settled, mode, outlived))
                               .run_from_signals(plan_signals(hold, mode));
        EXPECT_EQ(settled_out.missing_data[3].delisting_count, 1u) << "mode=" << mode;
        EXPECT_EQ(outlived.actions[3].distribution_pending, 1u);
        EXPECT_EQ(outlived.actions[4].child_positions_created, 1u);
        EXPECT_NEAR(outlived.actions[4].distribution_pnl, 0.5 * 40.0 / 100.0, 1e-12);
        EXPECT_NEAR(outlived.weights[4][1], 0.2, 1e-12);
    }
}

TEST(CorporateActions, AParentWithoutAQuoteOnTheExDateStillPaysAgainstItsLastMark) {
    for (int mode : {0, 1}) {
        // Frozen on the ex-date: the cash is booked against the last mark, the drop
        // is recognised when the quote resumes, and the two add up to the cash-style return.
        auto panel = flat_panel(6, 2);
        panel.at("close", 2, 0) = kMissing;
        for (unsigned di = 3; di < panel.T; ++di) panel.at("close", di, 0) = 95.0f;
        panel.corporate_actions = {cash_leg(2, 0, 5.0)};
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(ledger.gross[2], 0.05, 1e-12) << "mode=" << mode;
        EXPECT_EQ(out.missing_data[2].missing_held_count, 1u);
        EXPECT_NEAR(ledger.gross[3], 95.0 / 100.0 - 1.0, 1e-12);
        EXPECT_NEAR(ledger.gross[2] + ledger.gross[3], (95.0 + 5.0) / 100.0 - 1.0, 1e-12);

        // Frozen for two bars before the ex-date: the mark is the last observed 100.
        auto frozen = flat_panel(6, 2);
        frozen.at("close", 2, 0) = kMissing;
        frozen.at("close", 3, 0) = kMissing;
        for (unsigned di = 4; di < frozen.T; ++di) frozen.at("close", di, 0) = 90.0f;
        frozen.corporate_actions = {cash_leg(4, 0, 5.0)};
        AccountingLedger thawed;
        Simulator(frozen, ledger_config(frozen, mode, thawed)).run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(thawed.gross[4], (90.0 + 5.0) / 100.0 - 1.0, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(thawed.actions[4].dividend_pnl, 0.05, 1e-12);
    }
}

// ── Rights and warrants (docs/rights_warrants_design_2026-09-17.md) ─────────

namespace {
DataPanel::CorporateActionLeg rights_leg(unsigned bar, unsigned parent, int32_t child, double ratio,
                                         int32_t expiry_bar) {
    auto leg = stock_leg(bar, parent, child, ratio, kNaN, "unknown");
    leg.rights = true;
    leg.event_type = "warrant";
    leg.expiry_bar = expiry_bar;
    return leg;
}
}  // namespace

// A quoted right is an ordinary stock leg: created at its close, sold at the next execution.
TEST(CorporateActions, QuotedRightsFollowTheStockLegPath) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 2);
        for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = 5.0f;
        panel.corporate_actions = {rights_leg(2, 0, 1, 0.1, 5)};
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger)).run_from_signals(plan_signals(plan, mode));
        const double child_weight = 0.1 * 5.0 / 100.0;
        EXPECT_NEAR(ledger.gross[2], child_weight, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(ledger.weights[2][1], child_weight, 1e-12);
        EXPECT_NEAR(out.turnover[3], child_weight, 1e-12);
        EXPECT_EQ(out.corporate_actions[5].rights_expired, 0u);
        EXPECT_EQ(out.corporate_actions[5].rights_expired_held, 0u);
        ASSERT_EQ(out.distributions.size(), 1u);
        EXPECT_EQ(out.distributions[0].source, DistributionSource::ChildClose);
        EXPECT_EQ(out.distributions[0].event_type, "warrant");
    }
}

// A right that never prints lapses at 0 on the first bar after its expiry day: unlike
// an unpriced leg it waited (pending) and is closed later, with its own record source.
TEST(CorporateActions, PendingRightsExpireAtZeroOnTheBarAfterExpiry) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(7, 2);
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 95.0f;
        for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = kMissing;
        panel.corporate_actions = {rights_leg(2, 0, 1, 0.1, 5)};   // expiry day = bar 4, closes on bar 5
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger)).run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(ledger.gross[2], -0.05, 1e-12) << "mode=" << mode;
        for (unsigned di = 2; di < 5; ++di)
            EXPECT_EQ(out.corporate_actions[di].distribution_pending, 1u) << "bar=" << di;
        EXPECT_EQ(out.corporate_actions[4].rights_expired, 0u);
        EXPECT_EQ(out.corporate_actions[5].rights_expired, 1u);
        EXPECT_EQ(out.corporate_actions[5].distribution_pending, 0u);
        EXPECT_EQ(out.corporate_actions[5].distribution_unpriced, 0u);
        EXPECT_NEAR(ledger.gross[5], 0.0, 1e-12);
        EXPECT_DOUBLE_EQ(out.turnover[5], 0.0);
        EXPECT_EQ(out.corporate_actions.back().distribution_pending, 0u);
        ASSERT_EQ(out.distributions.size(), 1u);
        EXPECT_EQ(out.distributions[0].source, DistributionSource::Expired);
        EXPECT_EQ(out.distributions[0].bar, 5u);
        EXPECT_EQ(out.distributions[0].ex_bar, 2u);
        EXPECT_NEAR(out.distributions[0].child_weight, 0.0, 1e-12);
        EXPECT_NEAR(out.distributions[0].parent_weight, 1.0, 1e-12);
        // The same right delivered to a child outside the panel is unpriced on the ex-date instead.
        auto outside = panel;
        outside.corporate_actions = {rights_leg(2, 0, -1, 0.1, 5)};
        AccountingLedger unpriced;
        auto unpriced_out = Simulator(outside, ledger_config(outside, mode, unpriced))
                                .run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(unpriced_out.corporate_actions[2].distribution_unpriced, 1u);
        EXPECT_EQ(unpriced_out.corporate_actions[5].rights_expired, 0u);
        EXPECT_EQ(unpriced_out.distributions[0].source, DistributionSource::Unpriced);
        EXPECT_EQ(unpriced_out.distributions[0].bar, 2u);
    }
}

// A quote on the last bar before expiry still creates the right; the expiry does not fire.
TEST(CorporateActions, AFirstQuoteBeforeExpiryCreatesTheRight) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(7, 2);
        for (unsigned di = 0; di < 4; ++di) panel.at("close", di, 1) = kMissing;
        for (unsigned di = 4; di < panel.T; ++di) panel.at("close", di, 1) = 4.0f;
        panel.corporate_actions = {rights_leg(2, 0, 1, 0.1, 5)};   // first quote on bar 4, expiry closes on 5
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger)).run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(out.corporate_actions[4].child_positions_created, 1u) << "mode=" << mode;
        EXPECT_NEAR(ledger.gross[4], 0.1 * 4.0 / 100.0, 1e-12);
        EXPECT_EQ(out.corporate_actions[5].rights_expired, 0u);
        EXPECT_EQ(out.corporate_actions[5].rights_expired_held, 0u);   // still quoting: sold, not lapsed
        EXPECT_NEAR(out.turnover[5], 0.1 * 4.0 / 100.0, 1e-12);
        ASSERT_EQ(out.distributions.size(), 1u);
        EXPECT_EQ(out.distributions[0].source, DistributionSource::FirstQuote);
    }
}

// A held right whose quote is gone once the expiry day has passed settles at -1 through
// the settlement path: no fee, reported both as a delisting and as a lapsed right.
TEST(CorporateActions, HeldRightsWithoutAQuoteAfterExpirySettleAtMinusOne) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(8, 2);
        for (unsigned di = 0; di < 2; ++di) panel.at("close", di, 1) = kMissing;
        panel.at("close", 2, 1) = 4.0f;                               // quoted on the ex-date only
        for (unsigned di = 3; di < panel.T; ++di) panel.at("close", di, 1) = kMissing;
        panel.corporate_actions = {rights_leg(2, 0, 1, 0.1, 5)};      // expiry day bar 4
        // The strategy keeps the right (target 0.004 = the received weight) so it stays held.
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        for (unsigned di = 3; di < panel.T; ++di) plan[di] = {996, 4};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger)).run_from_signals(plan_signals(plan, mode));
        const double child_weight = 0.1 * 4.0 / 100.0;
        EXPECT_NEAR(ledger.weights[2][1], child_weight, 1e-12) << "mode=" << mode;
        // Frozen (no quote) through bar 4; bar 5 is the first bar after the expiry day.
        EXPECT_EQ(out.missing_data[3].missing_held_count, 1u);
        EXPECT_EQ(out.missing_data[4].delisting_count, 0u);
        EXPECT_EQ(out.missing_data[5].delisting_count, 1u);
        EXPECT_EQ(out.corporate_actions[5].rights_expired_held, 1u);
        EXPECT_NEAR(out.corporate_actions[5].rights_expired_gross, child_weight, 1e-12);
        EXPECT_NEAR(out.missing_data[5].delisting_pnl, -child_weight, 1e-12);
        EXPECT_NEAR(ledger.gross[5], -child_weight, 1e-12);
        EXPECT_EQ(out.missing_data[5].default_delisting_count, 0u);   // an explicit -1, not the default
        EXPECT_EQ(ledger.retired[5][1], 1);
        EXPECT_NEAR(ledger.marks_after[5][1], 0.0, 1e-12);
        EXPECT_EQ(out.corporate_actions[5].rights_expired, 0u);        // no pending leg lapsed
        // The lapse is a settlement, not a trade: the right itself trades nothing. What
        // does trade is the parent renormalizing to a full book once the right has left
        // the intent universe (D0 sees that on bar 5, D1 one bar later).
        EXPECT_NEAR(ledger.trades[5][1], 0.0, 1e-12);
        EXPECT_NEAR(out.missing_data[5].settled_gross, child_weight, 1e-12);
        const double renormalized = mode == 0 ? child_weight : 0.0;
        EXPECT_NEAR(out.turnover[5], renormalized, 1e-12);
        EXPECT_NEAR(out.ret[5], -child_weight - renormalized * kFee, 1e-12);
    }
}

// Without an expiry the E1 behaviour stays: pending to the end; an expiry beyond the
// panel never fires; a spin-off leg with an expiry is not a right and never expires.
TEST(CorporateActions, RightsWithoutOrBeyondExpiryStayPendingAndOnlyRightsExpire) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 2);
        for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = kMissing;
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        const auto signals = plan_signals(plan, mode);
        auto no_expiry = panel;
        no_expiry.corporate_actions = {rights_leg(2, 0, 1, 0.1, -1)};
        auto reference = panel;
        reference.corporate_actions = {stock_leg(2, 0, 1, 0.1, kNaN, "unknown")};
        auto beyond = panel;
        beyond.corporate_actions = {rights_leg(2, 0, 1, 0.1, static_cast<int32_t>(panel.T))};
        auto spin_with_expiry = panel;
        spin_with_expiry.corporate_actions = {stock_leg(2, 0, 1, 0.1, kNaN, "unknown")};
        spin_with_expiry.corporate_actions[0].expiry_bar = 4;
        AccountingLedger reference_ledger;
        const auto ref_out = Simulator(reference, ledger_config(reference, mode, reference_ledger))
                                 .run_from_signals(signals);
        for (DataPanel* variant : {&no_expiry, &beyond, &spin_with_expiry}) {
            AccountingLedger ledger;
            const auto out = Simulator(*variant, ledger_config(*variant, mode, ledger)).run_from_signals(signals);
            EXPECT_EQ(out.ret, ref_out.ret) << "mode=" << mode;
            EXPECT_EQ(out.corporate_actions.back().distribution_pending, 1u);
            for (const auto& bar : out.corporate_actions) {
                EXPECT_EQ(bar.rights_expired, 0u);
                EXPECT_EQ(bar.rights_expired_held, 0u);
            }
            EXPECT_TRUE(out.distributions.empty());
        }
    }
}

// Reviewer probe D-1: with stale_quote_settle_bars set, a right that goes silent
// before its expiry must not be settled at the default return by the stale rule.
// Priority: events settlement > expiry > stale quote.
TEST(CorporateActions, ExpiryOutranksTheStaleQuoteRuleForHeldRights) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(8, 2);
        for (unsigned di = 0; di < 2; ++di) panel.at("close", di, 1) = kMissing;
        panel.at("close", 2, 1) = 4.0f;                               // quoted on the ex-date only
        for (unsigned di = 3; di < panel.T; ++di) panel.at("close", di, 1) = kMissing;
        panel.corporate_actions = {rights_leg(2, 0, 1, 0.1, 5)};      // expiry day bar 4, lapses on bar 5
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        for (unsigned di = 3; di < panel.T; ++di) plan[di] = {996, 4};   // keep the right
        AccountingLedger ledger;
        auto config = ledger_config(panel, mode, ledger);
        config.stale_quote_settle_bars = 2;      // would fire on bar 4 without the priority rule
        config.default_delisting_return = 0.0;
        auto out = Simulator(panel, config).run_from_signals(plan_signals(plan, mode));
        const double child_weight = 0.1 * 4.0 / 100.0;
        for (unsigned di : {3u, 4u}) {
            EXPECT_EQ(out.missing_data[di].missing_held_count, 1u) << "mode=" << mode << " bar=" << di;
            EXPECT_EQ(out.missing_data[di].stale_settlement_count, 0u);
            EXPECT_EQ(out.missing_data[di].delisting_count, 0u);
        }
        EXPECT_EQ(out.missing_data[5].delisting_count, 1u);
        EXPECT_EQ(out.missing_data[5].stale_settlement_count, 0u);
        EXPECT_EQ(out.corporate_actions[5].rights_expired_held, 1u);
        EXPECT_NEAR(out.missing_data[5].delisting_pnl, -child_weight, 1e-12);
        // Over the run the right earned its value on creation and lost it at expiry: net 0.
        double gross = 0.0;
        for (double value : ledger.gross) gross += value;
        EXPECT_NEAR(gross, 0.0, 1e-12) << "mode=" << mode;
        // A plain frozen name in the same book still follows the stale rule.
        auto plain = flat_panel(8, 2);
        for (unsigned di = 3; di < plain.T; ++di) plain.at("close", di, 1) = kMissing;
        plain.corporate_actions = {stock_leg(2, 0, 1, 0.1)};   // a spin-off child, no expiry
        AccountingLedger plain_ledger;
        auto plain_config = ledger_config(plain, mode, plain_ledger);
        plain_config.stale_quote_settle_bars = 2;
        auto plain_out = Simulator(plain, plain_config).run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(plain_out.missing_data[4].stale_settlement_count, 1u) << "mode=" << mode;
    }
}

// A right that keeps printing past its expiry is valued until it goes silent, and a
// first quote on the expiry bar itself no longer creates it: the entitlement lapsed.
TEST(CorporateActions, QuotesAfterExpiryAreValuedUntilSilentAndAFirstQuoteOnTheExpiryBarLapses) {
    for (int mode : {0, 1}) {
        // Quoted 4, 4, 4 on bars 2..6 (past the bar-5 expiry), silent from bar 7.
        auto panel = flat_panel(9, 2);
        for (unsigned di = 0; di < 2; ++di) panel.at("close", di, 1) = kMissing;
        for (unsigned di = 2; di < 7; ++di) panel.at("close", di, 1) = 4.0f;
        panel.at("close", 6, 1) = 5.0f;                               // a real move after expiry
        for (unsigned di = 7; di < panel.T; ++di) panel.at("close", di, 1) = kMissing;
        panel.corporate_actions = {rights_leg(2, 0, 1, 0.1, 5)};
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        for (unsigned di = 3; di < panel.T; ++di) plan[di] = {996, 4};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger)).run_from_signals(plan_signals(plan, mode));
        const double child_weight = 0.1 * 4.0 / 100.0;
        for (unsigned di : {5u, 6u}) {
            EXPECT_EQ(out.missing_data[di].delisting_count, 0u) << "mode=" << mode << " bar=" << di;
            EXPECT_EQ(out.corporate_actions[di].rights_expired_held, 0u);
        }
        EXPECT_NEAR(ledger.gross[6], child_weight * (5.0 / 4.0 - 1.0), 1e-12);   // still valued past expiry
        EXPECT_EQ(out.missing_data[7].delisting_count, 1u);                        // first silent bar
        EXPECT_EQ(out.corporate_actions[7].rights_expired_held, 1u);
        // Fixed book: the weight held into bar 7 is the 0.004 target, so that is what lapses.
        EXPECT_NEAR(out.missing_data[7].delisting_pnl, -child_weight, 1e-12);
        EXPECT_NEAR(ledger.marks_after[7][1], 0.0, 1e-12);

        // Pending right whose first quote lands on the expiry bar: lapsed, not created.
        auto late = flat_panel(8, 2);
        for (unsigned di = 0; di < 5; ++di) late.at("close", di, 1) = kMissing;
        for (unsigned di = 5; di < late.T; ++di) late.at("close", di, 1) = 4.0f;
        late.corporate_actions = {rights_leg(2, 0, 1, 0.1, 5)};
        std::vector<std::vector<float>> hold(late.T, {1, 0});
        hold[0] = {0, 0};
        AccountingLedger late_ledger;
        auto late_out = Simulator(late, ledger_config(late, mode, late_ledger)).run_from_signals(plan_signals(hold, mode));
        EXPECT_EQ(late_out.corporate_actions[5].rights_expired, 1u) << "mode=" << mode;
        EXPECT_EQ(late_out.corporate_actions[5].child_positions_created, 0u);
        EXPECT_NEAR(late_ledger.weights[5][1], 0.0, 1e-12);
        EXPECT_NEAR(late_ledger.gross[5], 0.0, 1e-12);
        ASSERT_EQ(late_out.distributions.size(), 1u);
        EXPECT_EQ(late_out.distributions[0].source, DistributionSource::Expired);
    }
}

// Two rights legs from two parents deliver the same security with different
// expiries. A held position lapses at the earliest one (the security's expiry);
// pending legs each lapse at their own expiry bar.
TEST(CorporateActions, SeveralLegsIntoOneRightUseTheMinimumExpiryWhenHeldAndTheirOwnWhenPending) {
    for (int mode : {0, 1}) {
        // Held: S2 quoted on the ex-date only, both parents deliver it, expiries 5 and 7.
        auto panel = flat_panel(9, 3);
        for (unsigned di = 0; di < 2; ++di) panel.at("close", di, 2) = kMissing;
        panel.at("close", 2, 2) = 4.0f;
        for (unsigned di = 3; di < panel.T; ++di) panel.at("close", di, 2) = kMissing;
        panel.corporate_actions = {rights_leg(2, 0, 2, 0.1, 5), rights_leg(2, 1, 2, 0.1, 7)};
        std::vector<std::vector<float>> plan(panel.T, {1, 1, 0});
        plan[0] = {0, 0, 0};
        for (unsigned di = 3; di < panel.T; ++di) plan[di] = {498, 498, 4};   // keep the right
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger)).run_from_signals(plan_signals(plan, mode));
        const double received = 2.0 * 0.5 * 0.1 * 4.0 / 100.0;   // both halves deliver 0.002
        EXPECT_NEAR(ledger.weights[2][2], received, 1e-12) << "mode=" << mode;
        EXPECT_EQ(out.corporate_actions[2].child_positions_created, 2u);
        EXPECT_EQ(out.missing_data[4].delisting_count, 0u);
        EXPECT_EQ(out.missing_data[5].delisting_count, 1u);          // the earliest expiry wins
        EXPECT_EQ(out.corporate_actions[5].rights_expired_held, 1u);
        EXPECT_NEAR(out.missing_data[5].delisting_pnl, -received, 1e-12);
        EXPECT_EQ(out.corporate_actions[7].rights_expired_held, 0u); // nothing left to lapse later
        EXPECT_EQ(ledger.retired[7][2], 1);

        // Pending: S2 never quotes; each leg lapses on its own expiry bar.
        auto pending = flat_panel(9, 3);
        for (unsigned di = 0; di < pending.T; ++di) pending.at("close", di, 2) = kMissing;
        pending.corporate_actions = {rights_leg(2, 0, 2, 0.1, 5), rights_leg(2, 1, 2, 0.1, 7)};
        std::vector<std::vector<float>> hold(pending.T, {1, 1, 0});
        hold[0] = {0, 0, 0};
        AccountingLedger pending_ledger;
        auto pending_out = Simulator(pending, ledger_config(pending, mode, pending_ledger))
                               .run_from_signals(plan_signals(hold, mode));
        EXPECT_EQ(pending_out.corporate_actions[4].distribution_pending, 2u) << "mode=" << mode;
        EXPECT_EQ(pending_out.corporate_actions[5].rights_expired, 1u);
        EXPECT_EQ(pending_out.corporate_actions[5].distribution_pending, 1u);
        EXPECT_EQ(pending_out.corporate_actions[6].rights_expired, 0u);
        EXPECT_EQ(pending_out.corporate_actions[7].rights_expired, 1u);
        EXPECT_EQ(pending_out.corporate_actions[7].distribution_pending, 0u);
        ASSERT_EQ(pending_out.distributions.size(), 2u);
        EXPECT_EQ(pending_out.distributions[0].bar, 5u);
        EXPECT_EQ(pending_out.distributions[0].parent, 0u);
        EXPECT_EQ(pending_out.distributions[1].bar, 7u);
        EXPECT_EQ(pending_out.distributions[1].parent, 1u);
        for (const auto& record : pending_out.distributions)
            EXPECT_EQ(record.source, DistributionSource::Expired);
        for (const auto& bar : pending_out.missing_data) EXPECT_EQ(bar.delisting_count, 0u);
    }
}

TEST(CorporateActions, RejectsExpiryBarsBeforeTheExDate) {
    auto panel = flat_panel(6, 2);
    SimulatorConfig config{panel.T, panel.N, kFee, 1, false, {}};
    panel.corporate_actions = {rights_leg(3, 0, 1, 0.1, 2)};
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.corporate_actions = {rights_leg(3, 0, 1, 0.1, 7)};   // beyond T
    EXPECT_THROW(Simulator(panel, config), std::invalid_argument);
    panel.corporate_actions = {rights_leg(3, 0, 1, 0.1, 6)};   // == T is "never"
    EXPECT_NO_THROW(Simulator(panel, config));
}

// ── Legs dated on the parent's events settlement bar (V1 P0, split-path deals) ─
//
// The events row settles the cash and the successor; the ledger keys the extra
// children to the old id on the same bar. Both are measured against the exposure
// and mark the settlement used, so the bar totals
// (cash + ratio_successor * successor + ratio_child * child) / mark - 1.

namespace {
// P quotes on bars 0..3 and goes silent on bar 4, where its end date (bar 3) settles it.
DataPanel split_path_panel(unsigned N, float parent_mark) {
    auto panel = flat_panel(8, N);
    for (unsigned ii = 1; ii < N; ++ii)
        for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, ii) = kMissing;
    for (unsigned di = 0; di < 4; ++di) panel.at("close", di, 0) = parent_mark;
    for (unsigned di = 4; di < panel.T; ++di) panel.at("close", di, 0) = kMissing;
    panel.settlement_bar.assign(N, -1);
    panel.settlement_bar[0] = 4;
    panel.settlement_return.assign(N, kMissing);
    panel.settlement_successor.assign(N, -1);
    panel.settlement_successor_ratio.assign(N, kNaN);
    panel.settlement_cash.assign(N, kNaN);
    panel.settlement_has_stock_leg.assign(N, 0);
    panel.settlement_has_cash_leg.assign(N, 0);
    return panel;
}
}  // namespace

// V1 C2: cash 14.5 from the events row plus 0.5 child at 40 from the ledger, mark 100.
TEST(CorporateActions, SettlementBarLegsBookAgainstThePreSettlementExposureWithEventsCash) {
    for (int mode : {0, 1}) {
        for (float side : {1.0f, -1.0f}) {
            auto panel = split_path_panel(2, 100.0f);
            for (unsigned di = 4; di < panel.T; ++di) panel.at("close", di, 1) = di == 4 ? 40.0f : 42.0f;
            panel.settlement_cash[0] = 14.5;
            panel.settlement_has_cash_leg[0] = 1;
            panel.corporate_actions = {stock_leg(4, 0, 1, 0.5, 40.0, "child_close_ex")};
            std::vector<std::vector<float>> plan(panel.T, {side, 0});
            plan[0] = {0, 0};
            AccountingLedger ledger;
            auto out = Simulator(panel, ledger_config(panel, mode, ledger)).run_from_signals(plan_signals(plan, mode));
            const double w = static_cast<double>(side);
            const double child_weight = w * 0.5 * 40.0 / 100.0;      // 0.2 of the book
            // Contract: (14.5 + 0.5 * 40) / 100 - 1 = -0.655 on the exposure.
            EXPECT_NEAR(ledger.gross[4], w * ((14.5 + 0.5 * 40.0) / 100.0 - 1.0), 1e-12)
                << "mode=" << mode << " side=" << side;
            EXPECT_NEAR(out.missing_data[4].delisting_pnl, w * (14.5 / 100.0 - 1.0), 1e-12);
            EXPECT_EQ(out.missing_data[4].computed_settlement_count, 1u);
            EXPECT_EQ(out.missing_data[4].default_delisting_count, 0u);
            EXPECT_EQ(out.corporate_actions[4].legs_on_settlement_bar, 1u);
            EXPECT_EQ(out.corporate_actions[4].legs_deferred_to_events, 0u);
            EXPECT_EQ(out.corporate_actions[4].child_positions_created, 1u);
            EXPECT_NEAR(out.corporate_actions[4].distribution_pnl, child_weight, 1e-12);
            EXPECT_NEAR(ledger.weights[4][1], child_weight, 1e-12);
            EXPECT_EQ(ledger.retired[4][0], 1);
            EXPECT_DOUBLE_EQ(out.turnover[4], 0.0);                     // received, not traded
            // The child trades the next bar: 40 -> 42 recognised, then sold with a fee.
            EXPECT_NEAR(ledger.gross[5], child_weight * (42.0 / 40.0 - 1.0), 1e-12);
            EXPECT_NEAR(out.turnover[5], std::fabs(child_weight), 1e-12);
            ASSERT_EQ(out.distributions.size(), 1u);
            EXPECT_TRUE(out.distributions[0].on_settlement_bar);
            EXPECT_EQ(out.distributions[0].source, DistributionSource::ChildClose);
        }
    }
}

// V1 C2 (DISC.A form): successor 0.5 S at 13.81 from the events row plus children
// 0.5 C at 16 and 0.05 D at 27.21 from the ledger, mark 18.30.
TEST(CorporateActions, SettlementBarLegsBookBesideASuccessorTransfer) {
    for (int mode : {0, 1}) {
        for (float side : {1.0f, -1.0f}) {
            auto panel = split_path_panel(4, 18.30f);   // P, S, C, D
            for (unsigned di = 4; di < panel.T; ++di) {
                panel.at("close", di, 1) = di == 4 ? 13.81f : 14.0f;
                panel.at("close", di, 2) = 16.0f;
                panel.at("close", di, 3) = di == 4 ? 27.21f : 27.0f;
            }
            panel.settlement_successor[0] = 1;
            panel.settlement_successor_ratio[0] = 0.5;
            panel.settlement_has_stock_leg[0] = 1;
            panel.corporate_actions = {stock_leg(4, 0, 2, 0.5, 16.0, "child_close_ex"),
                                       stock_leg(4, 0, 3, 0.05, 27.21, "child_close_ex")};
            std::vector<std::vector<float>> plan(panel.T, {side, 0, 0, 0});
            plan[0] = {0, 0, 0, 0};
            AccountingLedger ledger;
            auto out = Simulator(panel, ledger_config(panel, mode, ledger)).run_from_signals(plan_signals(plan, mode));
            const double w = static_cast<double>(side);
            const double mark = close_of(panel, 3, 0), s = close_of(panel, 4, 1), c = close_of(panel, 4, 2),
                         d = close_of(panel, 4, 3);
            const double contract = (0.5 * s + 0.5 * c + 0.05 * d) / mark - 1.0;   // -0.11117
            EXPECT_NEAR(ledger.gross[4], w * contract, 1e-9) << "mode=" << mode << " side=" << side;
            EXPECT_NEAR(ledger.gross[4], w * -0.11117, 1e-5);
            EXPECT_EQ(out.missing_data[4].successor_conversions, 1u);
            EXPECT_NEAR(out.missing_data[4].converted_gross, std::fabs(w) * 0.5 * s / mark, 1e-12);
            EXPECT_EQ(out.corporate_actions[4].legs_on_settlement_bar, 2u);
            EXPECT_EQ(out.corporate_actions[4].child_positions_created, 2u);
            EXPECT_NEAR(ledger.weights[4][2], w * 0.5 * c / mark, 1e-12);
            EXPECT_NEAR(ledger.weights[4][3], w * 0.05 * d / mark, 1e-12);
            // Deliberate 1-bar difference: the successor transfer is rebalanced on the
            // settlement bar itself (sold here, target 0), the spun-off children on the next.
            EXPECT_NEAR(ledger.weights[4][1], 0.0, 1e-12);
            EXPECT_NEAR(out.turnover[4], std::fabs(w) * 0.5 * s / mark, 1e-12);
            EXPECT_NEAR(out.turnover[5], std::fabs(w) * (0.5 * c / mark + 0.05 * d / mark), 1e-12);
        }
    }
}

// V1 C3, SLM 2014-05-01: 1 NAVI at 17.00 through the events row and 1 new SLM at 9.02
// through the ledger keyed to the old id, against the old SLM mark 25.75: +1.05%.
TEST(CorporateActions, SlmSplitPathBooksTheContractReturn) {
    for (int mode : {0, 1}) {
        for (float side : {1.0f, -1.0f}) {
            auto panel = split_path_panel(3, 25.75f);   // old SLM, NAVI, new SLM
            for (unsigned di = 4; di < panel.T; ++di) {
                panel.at("close", di, 1) = 17.0f;
                panel.at("close", di, 2) = 9.02f;
            }
            panel.settlement_successor[0] = 1;
            panel.settlement_successor_ratio[0] = 1.0;
            panel.settlement_has_stock_leg[0] = 1;
            panel.corporate_actions = {stock_leg(4, 0, 2, 1.0, 9.02, "child_close_ex")};
            std::vector<std::vector<float>> plan(panel.T, {side, 0, 0});
            plan[0] = {0, 0, 0};
            AccountingLedger ledger;
            auto out = Simulator(panel, ledger_config(panel, mode, ledger)).run_from_signals(plan_signals(plan, mode));
            const double w = static_cast<double>(side);
            const double mark = close_of(panel, 3, 0), navi = close_of(panel, 4, 1), slm = close_of(panel, 4, 2);
            EXPECT_NEAR(ledger.gross[4], w * ((navi + slm) / mark - 1.0), 1e-9) << "mode=" << mode << " side=" << side;
            EXPECT_NEAR(ledger.gross[4], w * 0.0104855, 1e-6);   // +1.05% long, not -33.98%
            EXPECT_NEAR(out.missing_data[4].delisting_pnl, w * (navi / mark - 1.0), 1e-9);
            EXPECT_NEAR(ledger.weights[4][2], w * slm / mark, 1e-9);
            EXPECT_EQ(out.corporate_actions[4].legs_on_settlement_bar, 1u);
            EXPECT_EQ(out.corporate_actions[4].legs_deferred_to_events, 0u);
        }
    }
}

// A settlement-bar leg whose child has no quote yet waits with the pre-settlement
// exposure and mark, and its later record still says where it came from.
TEST(CorporateActions, PendingSettlementBarLegsKeepTheirExposureAndFlag) {
    for (int mode : {0, 1}) {
        auto panel = split_path_panel(2, 100.0f);
        for (unsigned di = 6; di < panel.T; ++di) panel.at("close", di, 1) = 40.0f;   // first quote on bar 6
        panel.settlement_cash[0] = 14.5;
        panel.settlement_has_cash_leg[0] = 1;
        panel.corporate_actions = {stock_leg(4, 0, 1, 0.5, kNaN, "unknown")};
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger)).run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(ledger.gross[4], 14.5 / 100.0 - 1.0, 1e-12) << "mode=" << mode;   // events cash only
        EXPECT_EQ(out.corporate_actions[4].legs_on_settlement_bar, 1u);
        EXPECT_EQ(out.corporate_actions[4].distribution_pending, 1u);
        EXPECT_EQ(out.corporate_actions[6].child_positions_created, 1u);
        EXPECT_NEAR(ledger.gross[6], 0.5 * 40.0 / 100.0, 1e-12);   // pre-settlement w=1, mark 100
        ASSERT_EQ(out.distributions.size(), 1u);
        EXPECT_EQ(out.distributions[0].source, DistributionSource::FirstQuote);
        EXPECT_EQ(out.distributions[0].bar, 6u);
        EXPECT_EQ(out.distributions[0].ex_bar, 4u);
        EXPECT_TRUE(out.distributions[0].on_settlement_bar);
        EXPECT_NEAR(out.distributions[0].parent_weight, 1.0, 1e-12);
    }
}

// Double counting guards on the settlement bar: a ledger cash leg beside an events
// cash leg, and a ledger stock leg naming the successor, stay count-only; a ledger
// cash leg beside a successor-only events row is booked once.
TEST(CorporateActions, SettlementBarLegsNeverPayWhatTheEventsRowAlreadyPays) {
    for (int mode : {0, 1}) {
        std::vector<std::vector<float>> plan(8, {1, 0});
        plan[0] = {0, 0};
        // Events cash 14.5 and a ledger cash 14.5 on the same bar: paid once.
        auto twice = split_path_panel(2, 100.0f);
        twice.settlement_cash[0] = 14.5;
        twice.settlement_has_cash_leg[0] = 1;
        twice.corporate_actions = {cash_leg(4, 0, 14.5)};
        AccountingLedger once;
        auto once_out = Simulator(twice, ledger_config(twice, mode, once)).run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(once.gross[4], 14.5 / 100.0 - 1.0, 1e-12) << "mode=" << mode;
        EXPECT_EQ(once_out.corporate_actions[4].dividend_count, 0u);
        EXPECT_EQ(once_out.corporate_actions[4].legs_on_settlement_bar, 1u);
        EXPECT_EQ(once_out.corporate_actions[4].legs_deferred_to_events, 1u);

        // A successor-only events row with a ledger cash leg: the cash is booked once, here.
        auto successor_only = split_path_panel(2, 100.0f);
        for (unsigned di = 4; di < successor_only.T; ++di) successor_only.at("close", di, 1) = 50.0f;
        successor_only.settlement_successor[0] = 1;
        successor_only.settlement_successor_ratio[0] = 1.0;
        successor_only.settlement_has_stock_leg[0] = 1;
        successor_only.corporate_actions = {cash_leg(4, 0, 10.0)};
        AccountingLedger booked;
        auto booked_out = Simulator(successor_only, ledger_config(successor_only, mode, booked))
                              .run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(booked.gross[4], (50.0 + 10.0) / 100.0 - 1.0, 1e-12) << "mode=" << mode;
        EXPECT_EQ(booked_out.corporate_actions[4].dividend_count, 1u);
        EXPECT_EQ(booked_out.corporate_actions[4].legs_deferred_to_events, 0u);

        // A ledger stock leg naming the successor itself would deliver it twice: count-only.
        auto duplicate = successor_only;
        duplicate.corporate_actions = {stock_leg(4, 0, 1, 1.0, 50.0, "child_close_ex")};
        AccountingLedger deferred;
        auto deferred_out = Simulator(duplicate, ledger_config(duplicate, mode, deferred))
                                .run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(deferred.gross[4], 50.0 / 100.0 - 1.0, 1e-12) << "mode=" << mode;
        EXPECT_EQ(deferred_out.corporate_actions[4].child_positions_created, 0u);
        EXPECT_EQ(deferred_out.corporate_actions[4].distribution_count, 0u);
        EXPECT_EQ(deferred_out.corporate_actions[4].legs_deferred_to_events, 1u);
        EXPECT_TRUE(deferred_out.distributions.empty());
    }
}

// The per-bar series follow the trading span like frozenGross and are always
// present in the JSON, empty when no series was supplied.
TEST(CorporateActions, TimeseriesCarryTheDividendAndDistributionSeries) {
    const std::vector<int64_t> timestamps = {0, kDay, 2 * kDay};
    const std::vector<double> ret(3, 0.0), turnover(3, 0.0), max_weight(3, 0.0);
    const std::vector<int> position_count(3, 0);
    const std::vector<double> dividends = {9.0, 0.01, 0.0};
    const std::vector<double> distributions = {9.0, 0.0, 0.25};
    const auto series = compute_timeseries(timestamps, ret, turnover, max_weight, position_count,
                                           kFee, 1, nullptr, nullptr, &dividends, &distributions);
    ASSERT_EQ(series.dividend_pnl.size(), 2u);   // the warmup bar is dropped with the rest
    EXPECT_DOUBLE_EQ(series.dividend_pnl[0], 0.01);
    EXPECT_DOUBLE_EQ(series.distribution_pnl[1], 0.25);
    EXPECT_TRUE(series.frozen_gross.empty());
    const Json::Value json = timeseries_to_json(series);
    ASSERT_EQ(json["dividendPnl"].size(), 2u);
    EXPECT_DOUBLE_EQ(json["dividendPnl"][0].asDouble(), 0.01);
    ASSERT_EQ(json["distributionPnl"].size(), 2u);
    EXPECT_DOUBLE_EQ(json["distributionPnl"][1].asDouble(), 0.25);
    const Json::Value bare = timeseries_to_json(compute_timeseries(
        timestamps, ret, turnover, max_weight, position_count, kFee, 1));
    ASSERT_TRUE(bare.isMember("dividendPnl"));
    ASSERT_TRUE(bare.isMember("distributionPnl"));
    EXPECT_TRUE(bare["dividendPnl"].isArray());
    EXPECT_TRUE(bare["dividendPnl"].empty());
    EXPECT_TRUE(bare["distributionPnl"].empty());
}


// ── Unconfirmed P&L (docs/corporate_actions_engine_2026-09-17.md section 9) ──
//
// Marker rows of the export (no cash amount / no child), settlements priced from
// the default return, estimated values and children the engine gave up pricing
// are recorded on held parents as UnconfirmedRecord entries. Recording never
// changes the accounting; the strict policy turns the first such bar into a failure.

namespace {
std::vector<UnconfirmedRecord> records_on(const SimulatorOutput& out, unsigned bar) {
    std::vector<UnconfirmedRecord> found;
    for (const auto& record : out.unconfirmed)
        if (record.bar == bar) found.push_back(record);
    return found;
}
}  // namespace

// 47: a cash marker and a child marker on held parents are listed with their reason
// and weight; the P&L is the price-only P&L, bit for bit; an unheld parent lists nothing.
TEST(CorporateActions, MarkerLegsOnHeldParentsAreListedAsUnconfirmedAndUnheldOnesAreNot) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(5, 3);
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 95.0f;
        auto no_amount = cash_leg(2, 0, kNaN);          // export marker: no_cash_amount
        no_amount.unresolved = true;
        auto no_child = stock_leg(2, 1, -1, kNaN);       // export marker: no_child (nothing named)
        no_child.unresolved = true;
        no_child.child_named = false;
        auto flagged = cash_leg(3, 0, 1.0);              // status unresolved, amount present: booked
        flagged.unresolved = true;
        DataPanel bare = panel;
        panel.corporate_actions = {no_amount, no_child, flagged};
        std::vector<std::vector<float>> plan(panel.T, {0.5f, -0.5f, 0});
        plan[0] = {0, 0, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));

        const auto ex = records_on(out, 2);
        ASSERT_EQ(ex.size(), 2u) << "mode=" << mode;
        EXPECT_EQ(ex[0].asset, 0u);
        EXPECT_EQ(ex[0].kind, UnconfirmedKind::UnresolvedLeg);
        EXPECT_EQ(ex[0].detail, "no_cash_amount");
        EXPECT_NEAR(ex[0].weight_before, 0.5, 1e-12);
        EXPECT_EQ(ex[1].asset, 1u);
        EXPECT_EQ(ex[1].kind, UnconfirmedKind::UnresolvedLeg);
        EXPECT_EQ(ex[1].detail, "no_child");
        EXPECT_NEAR(ex[1].weight_before, -0.5, 1e-12);   // the sign of the held weight is kept
        EXPECT_EQ(out.corporate_actions[2].unconfirmed_count, 2u);
        EXPECT_NEAR(out.corporate_actions[2].unconfirmed_abs_weight, 1.0, 1e-12);
        EXPECT_EQ(out.corporate_actions[2].legs_unresolved, 2u);
        EXPECT_EQ(out.corporate_actions[2].distribution_unpriced, 1u);   // the marker child is unpriced as before
        EXPECT_EQ(out.corporate_actions[2].dividend_count, 0u);
        // A flagged row that still carries its amount is booked and listed as status_unresolved.
        const auto next = records_on(out, 3);
        ASSERT_EQ(next.size(), 1u);
        EXPECT_EQ(next[0].detail, "status_unresolved");
        EXPECT_EQ(out.corporate_actions[3].dividend_count, 1u);
        EXPECT_NEAR(out.corporate_actions[3].dividend_pnl, 0.5 * 1.0 / 95.0, 1e-12);
        EXPECT_EQ(out.unconfirmed.size(), 3u);

        // Markers change nothing: the run without them is bit-identical except for the
        // one booked dividend on bar 3.
        AccountingLedger reference;
        auto bare_out = Simulator(bare, ledger_config(bare, mode, reference))
                            .run_from_signals(plan_signals(plan, mode));
        for (unsigned di = 0; di < panel.T; ++di) {
            const double booked = di == 3 ? 0.5 * 1.0 / 95.0 : 0.0;
            EXPECT_EQ(out.ret[di], bare_out.ret[di] + booked) << "mode=" << mode << " bar=" << di;
            EXPECT_EQ(out.turnover[di], bare_out.turnover[di]);
            EXPECT_EQ(ledger.weights[di], reference.weights[di]);
        }
        EXPECT_TRUE(bare_out.unconfirmed.empty());
        for (const auto& bar : bare_out.corporate_actions) EXPECT_EQ(bar.unconfirmed_count, 0u);

        // Unheld parents: the markers are visible to the loader but list nothing here.
        std::vector<std::vector<float>> unheld(panel.T, {0, 0, 1});
        unheld[0] = {0, 0, 0};
        AccountingLedger none;
        auto unheld_out = Simulator(panel, ledger_config(panel, mode, none))
                              .run_from_signals(plan_signals(unheld, mode));
        EXPECT_TRUE(unheld_out.unconfirmed.empty()) << "mode=" << mode;
        for (const auto& bar : unheld_out.corporate_actions) {
            EXPECT_EQ(bar.unconfirmed_count, 0u);
            EXPECT_DOUBLE_EQ(bar.unconfirmed_abs_weight, 0.0);
        }
    }
}

// 48: strict fails on the first bar with unconfirmed P&L, naming the events; warn runs
// on; strict with nothing unconfirmed is the same run as warn.
TEST(CorporateActions, StrictPolicyFailsOnTheFirstUnconfirmedBarAndListsItsEvents) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 2);
        auto no_amount = cash_leg(2, 0, kNaN);
        no_amount.unresolved = true;
        auto no_child = stock_leg(2, 0, -1, kNaN);
        no_child.unresolved = true;
        no_child.child_named = false;
        panel.corporate_actions = {no_amount, no_child, cash_leg(4, 0, 2.0)};
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        AccountingLedger ledger;
        auto strict = ledger_config(panel, mode, ledger);
        strict.unresolved_policy = UnresolvedPolicy::Strict;
        try {
            Simulator(panel, strict).run_from_signals(plan_signals(plan, mode));
            ADD_FAILURE() << "strict must fail on the marker bar, mode=" << mode;
        } catch (const std::runtime_error& error) {
            const std::string what = error.what();
            EXPECT_NE(what.find("unresolved_policy strict"), std::string::npos) << what;
            EXPECT_NE(what.find("1970-01-03"), std::string::npos) << what;        // bar 2
            EXPECT_NE(what.find("2 event(s) on this bar"), std::string::npos) << what;
            EXPECT_NE(what.find("S0 unresolved_leg(no_cash_amount) weightBefore=1"), std::string::npos) << what;
            EXPECT_NE(what.find("S0 unresolved_leg(no_child) weightBefore=1"), std::string::npos) << what;
            EXPECT_EQ(what.find("first"), std::string::npos) << what;              // both fit the limit
        }
        // The bar's accounting ran before the failure: the callback saw bars 0..2 only.
        EXPECT_EQ(ledger.gross.size(), 3u);
        // The report limit trims the list and says so.
        AccountingLedger trimmed;
        auto limited = ledger_config(panel, mode, trimmed);
        limited.unresolved_policy = UnresolvedPolicy::Strict;
        limited.strict_report_limit = 1;
        try {
            Simulator(panel, limited).run_from_signals(plan_signals(plan, mode));
            ADD_FAILURE() << "strict must fail, mode=" << mode;
        } catch (const std::runtime_error& error) {
            const std::string what = error.what();
            EXPECT_NE(what.find("first 1 listed"), std::string::npos) << what;
            EXPECT_NE(what.find("no_cash_amount"), std::string::npos) << what;
            EXPECT_EQ(what.find("no_child"), std::string::npos) << what;
        }
        // Warn (the default) records and continues.
        AccountingLedger warned;
        auto warn = ledger_config(panel, mode, warned);
        EXPECT_EQ(warn.unresolved_policy, UnresolvedPolicy::Warn);
        auto out = Simulator(panel, warn).run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(warned.gross.size(), panel.T);
        EXPECT_EQ(out.unconfirmed.size(), 2u);
        EXPECT_EQ(out.corporate_actions[4].dividend_count, 1u);
        // Nothing unconfirmed: strict and warn are the same run.
        DataPanel clean = panel;
        clean.corporate_actions = {cash_leg(4, 0, 2.0)};
        AccountingLedger strict_clean, warn_clean;
        auto strict_config = ledger_config(clean, mode, strict_clean);
        strict_config.unresolved_policy = UnresolvedPolicy::Strict;
        auto strict_out = Simulator(clean, strict_config).run_from_signals(plan_signals(plan, mode));
        auto warn_out = Simulator(clean, ledger_config(clean, mode, warn_clean)).run_from_signals(plan_signals(plan, mode));
        EXPECT_TRUE(strict_out.unconfirmed.empty());
        for (unsigned di = 0; di < clean.T; ++di) {
            EXPECT_EQ(strict_out.ret[di], warn_out.ret[di]) << "mode=" << mode << " bar=" << di;
            EXPECT_EQ(strict_out.turnover[di], warn_out.turnover[di]);
        }
        // An unheld parent under strict never fails either.
        std::vector<std::vector<float>> unheld(panel.T, {0, 1});
        unheld[0] = {0, 0};
        AccountingLedger none;
        auto strict_unheld = ledger_config(panel, mode, none);
        strict_unheld.unresolved_policy = UnresolvedPolicy::Strict;
        EXPECT_NO_THROW(Simulator(panel, strict_unheld).run_from_signals(plan_signals(unheld, mode)));
    }
}

// 49: the other three kinds. A settlement without deal terms or an explicit return is
// a default_settlement (a stale-quote settlement too); an explicit or computed return
// is not. A ledger estimate that is booked is an estimate_value (cash and child). A
// child the engine gives up on is a pending_dropped, with the reason, and a leg still
// pending on the last bar is listed there. Each leg yields at most one record.
TEST(CorporateActions, DefaultSettlementsEstimatesAndDroppedChildrenAreListedByKind) {
    for (int mode : {0, 1}) {
        // a. default settlement (events row, no terms) vs explicit return vs stale quote.
        {
            auto panel = flat_panel(6, 3);
            for (unsigned di = 3; di < panel.T; ++di) {
                panel.at("close", di, 0) = kMissing;   // S0: settlement opens on bar 3, no quote
                panel.at("close", di, 1) = kMissing;   // S1: same, but with an explicit return
            }
            panel.settlement_bar = {3, 3, -1};
            panel.settlement_return = {kMissing, -0.25f, kMissing};
            std::vector<std::vector<float>> plan(panel.T, {0.5f, 0.25f, 0.25f});
            plan[0] = {0, 0, 0};
            AccountingLedger ledger;
            auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                           .run_from_signals(plan_signals(plan, mode));
            ASSERT_EQ(out.unconfirmed.size(), 1u) << "mode=" << mode;
            EXPECT_EQ(out.unconfirmed[0].bar, 3u);
            EXPECT_EQ(out.unconfirmed[0].asset, 0u);
            EXPECT_EQ(out.unconfirmed[0].kind, UnconfirmedKind::DefaultSettlement);
            EXPECT_EQ(out.unconfirmed[0].detail, "no_deal_terms");
            EXPECT_NEAR(out.unconfirmed[0].weight_before, 0.5, 1e-12);
            EXPECT_EQ(out.missing_data[3].default_delisting_count, 1u);
            EXPECT_EQ(out.missing_data[3].delisting_count, 2u);   // S1 settled at -0.25, confirmed
            EXPECT_EQ(out.corporate_actions[3].unconfirmed_count, 1u);   // reported without any ledger
            EXPECT_NEAR(out.corporate_actions[3].unconfirmed_abs_weight, 0.5, 1e-12);
            // Declared terms the panel cannot price fall back to the default: still unconfirmed.
            DataPanel declared = panel;
            declared.settlement_successor = {-1, -1, -1};
            declared.settlement_successor_ratio = {kNaN, kNaN, kNaN};
            declared.settlement_cash = {kNaN, kNaN, kNaN};
            declared.settlement_has_stock_leg = {1, 0, 0};
            declared.settlement_has_cash_leg = {0, 0, 0};
            declared.settlement_successor_named = {1, 0, 0};
            AccountingLedger unpriced;
            auto declared_out = Simulator(declared, ledger_config(declared, mode, unpriced))
                                    .run_from_signals(plan_signals(plan, mode));
            ASSERT_EQ(declared_out.unconfirmed.size(), 1u);
            EXPECT_EQ(declared_out.unconfirmed[0].detail, "deal_terms_not_priced");
            // A stale-quote settlement uses the default return as well.
            DataPanel stale = flat_panel(6, 2);
            for (unsigned di = 2; di < stale.T; ++di) stale.at("close", di, 0) = kMissing;
            AccountingLedger silent;
            auto stale_config = ledger_config(stale, mode, silent);
            stale_config.stale_quote_settle_bars = 2;
            std::vector<std::vector<float>> hold(stale.T, {1, 0});
            hold[0] = {0, 0};
            auto stale_out = Simulator(stale, stale_config).run_from_signals(plan_signals(hold, mode));
            ASSERT_EQ(stale_out.unconfirmed.size(), 1u) << "mode=" << mode;
            EXPECT_EQ(stale_out.unconfirmed[0].kind, UnconfirmedKind::DefaultSettlement);
            EXPECT_EQ(stale_out.unconfirmed[0].detail, "stale_quote");
            EXPECT_EQ(stale_out.unconfirmed[0].bar, 3u);
            EXPECT_EQ(stale_out.missing_data[3].stale_settlement_count, 1u);
            // Priced settlements (computed from the successor) are confirmed.
            DataPanel priced = flat_panel(6, 2);
            for (unsigned di = 3; di < priced.T; ++di) priced.at("close", di, 0) = kMissing;
            priced.settlement_bar = {3, -1};
            priced.settlement_successor = {1, -1};
            priced.settlement_successor_ratio = {0.5, kNaN};
            priced.settlement_cash = {kNaN, kNaN};
            AccountingLedger converted;
            auto priced_out = Simulator(priced, ledger_config(priced, mode, converted))
                                  .run_from_signals(plan_signals(hold, mode));
            EXPECT_TRUE(priced_out.unconfirmed.empty()) << "mode=" << mode;
            EXPECT_EQ(priced_out.missing_data[3].computed_settlement_count, 1u);
        }
        // b. estimates: a flagged cash amount and a vendor-implied child value; a
        // when-issued close is a price and lists nothing.
        {
            auto panel = flat_panel(6, 4);
            for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 80.0f;
            for (unsigned di = 0; di < panel.T; ++di) {
                panel.at("close", di, 1) = kMissing;   // S1, S3: never quote
                panel.at("close", di, 3) = kMissing;
            }
            auto flagged_cash = cash_leg(2, 2, 1.0);
            flagged_cash.estimate = true;
            auto implied = stock_leg(3, 0, 1, 0.5, 40.0, "vendor_implied");
            auto when_issued = stock_leg(4, 2, 3, 0.1, 40.0, "wi_close");
            panel.corporate_actions = {flagged_cash, implied, when_issued};
            std::vector<std::vector<float>> plan(panel.T, {0.5f, 0, 0.5f, 0});
            plan[0] = {0, 0, 0, 0};
            AccountingLedger ledger;
            auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                           .run_from_signals(plan_signals(plan, mode));
            ASSERT_EQ(out.unconfirmed.size(), 2u) << "mode=" << mode;
            EXPECT_EQ(out.unconfirmed[0].bar, 2u);
            EXPECT_EQ(out.unconfirmed[0].asset, 2u);
            EXPECT_EQ(out.unconfirmed[0].kind, UnconfirmedKind::EstimateValue);
            EXPECT_EQ(out.unconfirmed[0].detail, "cash:unspecified");   // flagged leg without a reason
            EXPECT_NEAR(out.unconfirmed[0].weight_before, 0.5, 1e-12);
            EXPECT_EQ(out.corporate_actions[2].dividend_estimated, 1u);
            EXPECT_EQ(out.corporate_actions[2].dividend_count, 1u);   // booked as usual
            EXPECT_NEAR(out.corporate_actions[2].dividend_pnl, 0.5 * 1.0 / 100.0, 1e-12);
            EXPECT_EQ(out.unconfirmed[1].bar, 3u);
            EXPECT_EQ(out.unconfirmed[1].asset, 0u);
            EXPECT_EQ(out.unconfirmed[1].kind, UnconfirmedKind::EstimateValue);
            EXPECT_EQ(out.unconfirmed[1].detail, "ledger_value:vendor_implied");
            EXPECT_NEAR(out.unconfirmed[1].weight_before, 0.5, 1e-12);
            EXPECT_EQ(out.corporate_actions[3].distribution_estimated, 1u);
            EXPECT_EQ(out.corporate_actions[4].dividend_estimated, 0u);
            EXPECT_EQ(out.corporate_actions[4].distribution_estimated, 0u);
            EXPECT_EQ(out.corporate_actions[4].child_positions_created, 1u);   // wi_close is a price
        }
        // c. children the engine gives up on, each with its reason; one record per leg.
        {
            auto panel = flat_panel(6, 3);
            for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = kMissing;
            auto outside = stock_leg(2, 0, -1, 0.5);          // named, not in the panel
            outside.child_named = true;
            auto no_ratio = stock_leg(2, 2, 1, kNaN);
            no_ratio.child_named = true;
            auto waiting = stock_leg(3, 0, 1, 0.5);           // S1 never quotes: pending to the end
            waiting.child_named = true;
            panel.corporate_actions = {outside, no_ratio, waiting};
            std::vector<std::vector<float>> plan(panel.T, {0.5f, 0, 0.5f});
            plan[0] = {0, 0, 0};
            AccountingLedger ledger;
            auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                           .run_from_signals(plan_signals(plan, mode));
            ASSERT_EQ(out.unconfirmed.size(), 3u) << "mode=" << mode;
            EXPECT_EQ(out.unconfirmed[0].kind, UnconfirmedKind::PendingDropped);
            EXPECT_EQ(out.unconfirmed[0].detail, "child_not_in_panel");
            EXPECT_EQ(out.unconfirmed[0].asset, 0u);
            EXPECT_EQ(out.unconfirmed[1].detail, "ratio_unknown");
            EXPECT_EQ(out.unconfirmed[1].asset, 2u);
            EXPECT_EQ(out.unconfirmed[2].kind, UnconfirmedKind::PendingDropped);
            EXPECT_EQ(out.unconfirmed[2].detail, "pending_at_end");
            EXPECT_EQ(out.unconfirmed[2].bar, panel.T - 1);
            EXPECT_NEAR(out.unconfirmed[2].weight_before, 0.5, 1e-12);
            EXPECT_EQ(out.corporate_actions.back().distribution_pending, 1u);
            EXPECT_EQ(out.corporate_actions.back().unconfirmed_count, 1u);
            // Drop policy: the waiting leg is abandoned on its ex-date instead.
            AccountingLedger dropped;
            auto drop = ledger_config(panel, mode, dropped);
            drop.unpriced_child = UnpricedChildPolicy::Drop;
            auto drop_out = Simulator(panel, drop).run_from_signals(plan_signals(plan, mode));
            ASSERT_EQ(drop_out.unconfirmed.size(), 3u);
            EXPECT_EQ(drop_out.unconfirmed[2].detail, "policy_drop");
            EXPECT_EQ(drop_out.unconfirmed[2].bar, 3u);
            // The child retires while the leg waits: listed on the bar it is dropped.
            DataPanel retiring = panel;
            retiring.corporate_actions = {waiting};
            retiring.settlement_bar = {-1, 4, -1};
            AccountingLedger retired;
            auto retired_out = Simulator(retiring, ledger_config(retiring, mode, retired))
                                   .run_from_signals(plan_signals(plan, mode));
            ASSERT_EQ(retired_out.unconfirmed.size(), 1u);
            EXPECT_EQ(retired_out.unconfirmed[0].detail, "child_retired_while_pending");
            EXPECT_EQ(retired_out.unconfirmed[0].bar, 4u);
            EXPECT_EQ(retired_out.corporate_actions[4].distribution_unpriced, 1u);
            // A marker leg is an unresolved_leg only, never a second pending_dropped record.
            DataPanel marker = panel;
            auto no_child = stock_leg(2, 0, -1, kNaN);
            no_child.unresolved = true;
            no_child.child_named = false;
            marker.corporate_actions = {no_child};
            AccountingLedger once;
            auto marker_out = Simulator(marker, ledger_config(marker, mode, once))
                                  .run_from_signals(plan_signals(plan, mode));
            ASSERT_EQ(marker_out.unconfirmed.size(), 1u);
            EXPECT_EQ(marker_out.unconfirmed[0].kind, UnconfirmedKind::UnresolvedLeg);
            EXPECT_EQ(marker_out.corporate_actions[2].distribution_unpriced, 1u);
        }
    }
}

// 50: the per-bar unconfirmed exposure travels with the other series into the JSON.
TEST(CorporateActions, TimeseriesCarryTheUnconfirmedWeightSeries) {
    const std::vector<int64_t> timestamps = {0, kDay, 2 * kDay};
    const std::vector<double> ret(3, 0.0), turnover(3, 0.0), max_weight(3, 0.0);
    const std::vector<int> position_count(3, 0);
    const std::vector<double> unconfirmed = {9.0, 0.5, 0.0};
    const auto series = compute_timeseries(timestamps, ret, turnover, max_weight, position_count,
                                           kFee, 1, nullptr, nullptr, nullptr, nullptr, nullptr, &unconfirmed);
    ASSERT_EQ(series.unconfirmed_weight.size(), 2u);   // the warmup bar is dropped with the rest
    EXPECT_DOUBLE_EQ(series.unconfirmed_weight[0], 0.5);
    EXPECT_DOUBLE_EQ(series.unconfirmed_weight[1], 0.0);
    const Json::Value json = timeseries_to_json(series);
    ASSERT_EQ(json["unconfirmedWeight"].size(), 2u);
    EXPECT_DOUBLE_EQ(json["unconfirmedWeight"][0].asDouble(), 0.5);
    const Json::Value bare = timeseries_to_json(compute_timeseries(
        timestamps, ret, turnover, max_weight, position_count, kFee, 1));
    ASSERT_TRUE(bare.isMember("unconfirmedWeight"));
    EXPECT_TRUE(bare["unconfirmedWeight"].isArray());
    EXPECT_TRUE(bare["unconfirmedWeight"].empty());
}

// 52: (Y1-B) a stock leg whose child is not in the panel but whose ledger states a usable
// per-share value books that value as P&L like cash (the child cannot be held), with no
// position, no turnover and no fee; (3)(4) sources are estimates and stay visible as
// unconfirmed, a when-issued close is a price. Children inside the panel keep their paths.
TEST(CorporateActions, LedgerValuesForChildrenOutsideThePanelAreBookedAsCashWithoutAPosition) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(5, 2);
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 97.0f;
        auto implied = stock_leg(2, 0, -1, kNaN, kNaN, "vendor_implied");
        implied.child_named = true;          // an id the panel does not carry
        implied.child_value = 3.0;           // per parent share, cache units
        panel.corporate_actions = {implied};
        std::vector<std::vector<float>> plan(panel.T, {0.5f, 0.5f});
        plan[0] = {0, 0};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));
        const double value = 0.5 * 3.0 / 100.0;
        EXPECT_NEAR(ledger.gross[2], 0.5 * (97.0 / 100.0 - 1.0) + value, 1e-12) << "mode=" << mode;   // cash-style: (97 + 3)/100 - 1 = 0
        EXPECT_NEAR(ledger.gross[2], 0.0, 1e-12);
        EXPECT_NEAR(ledger.actions[2].distribution_pnl, value, 1e-12);
        EXPECT_EQ(ledger.actions[2].distribution_count, 1u);
        EXPECT_EQ(ledger.actions[2].distribution_value_only, 1u);
        EXPECT_EQ(ledger.actions[2].distribution_estimated, 1u);
        EXPECT_EQ(ledger.actions[2].child_positions_created, 0u);
        EXPECT_EQ(ledger.actions[2].distribution_unpriced, 0u);
        EXPECT_EQ(ledger.actions[2].distribution_pending, 0u);
        EXPECT_EQ(ledger.actions[2].legs_unresolved, 0u);
        EXPECT_EQ(out.position_count[2], 2);                       // no third position
        EXPECT_DOUBLE_EQ(out.turnover[2], 0.0);
        EXPECT_DOUBLE_EQ(ledger.fee[2], 0.0);
        EXPECT_NEAR(ledger.weights[2][0], 0.5, 1e-12);
        ASSERT_EQ(out.distributions.size(), 1u);
        EXPECT_EQ(out.distributions[0].source, DistributionSource::LedgerValueCash);
        EXPECT_EQ(out.distributions[0].child, -1);
        EXPECT_NEAR(out.distributions[0].child_weight, value, 1e-12);   // the P&L recognised
        EXPECT_TRUE(out.distributions[0].estimate);
        ASSERT_EQ(out.unconfirmed.size(), 1u);
        EXPECT_EQ(out.unconfirmed[0].kind, UnconfirmedKind::EstimateValue);
        EXPECT_EQ(out.unconfirmed[0].detail, "ledger_value_cash:vendor_implied");
        EXPECT_NEAR(out.unconfirmed[0].weight_before, 0.5, 1e-12);
        // A short parent pays the value away.
        std::vector<std::vector<float>> short_plan(panel.T, {-0.5f, 0.5f});
        short_plan[0] = {0, 0};
        AccountingLedger shorted;
        auto short_out = Simulator(panel, ledger_config(panel, mode, shorted))
                             .run_from_signals(plan_signals(short_plan, mode));
        EXPECT_NEAR(shorted.actions[2].distribution_pnl, -value, 1e-12) << "mode=" << mode;
        EXPECT_NEAR(short_out.unconfirmed[0].weight_before, -0.5, 1e-12);
        // A when-issued close is a price: booked the same way, not an estimate, not unconfirmed.
        auto when_issued = implied;
        when_issued.value_source = "wi_close";
        when_issued.estimate = false;
        panel.corporate_actions = {when_issued};
        AccountingLedger priced;
        auto priced_out = Simulator(panel, ledger_config(panel, mode, priced))
                              .run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(priced.actions[2].distribution_pnl, value, 1e-12) << "mode=" << mode;
        EXPECT_EQ(priced.actions[2].distribution_value_only, 1u);
        EXPECT_EQ(priced.actions[2].distribution_estimated, 0u);
        EXPECT_TRUE(priced_out.unconfirmed.empty());
        ASSERT_EQ(priced_out.distributions.size(), 1u);
        EXPECT_FALSE(priced_out.distributions[0].estimate);
        // A source the ledger may not value from (a cache close the panel does not have) stays unpriced.
        auto foreign = implied;
        foreign.value_source = "child_close_ex";
        foreign.ledger_value_usable = false;
        foreign.estimate = false;
        panel.corporate_actions = {foreign};
        AccountingLedger unpriced;
        auto unpriced_out = Simulator(panel, ledger_config(panel, mode, unpriced))
                                .run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(unpriced.actions[2].distribution_unpriced, 1u) << "mode=" << mode;
        EXPECT_EQ(unpriced.actions[2].distribution_value_only, 0u);
        EXPECT_NEAR(unpriced.actions[2].distribution_pnl, 0.0, 1e-12);
        ASSERT_EQ(unpriced_out.unconfirmed.size(), 1u);
        EXPECT_EQ(unpriced_out.unconfirmed[0].detail, "child_not_in_panel");
        // A no-child marker (no value) is unchanged: unresolved, nothing booked.
        auto no_child = stock_leg(2, 0, -1, kNaN, kNaN, "vendor_implied");
        no_child.unresolved = true;
        no_child.child_named = false;
        no_child.estimate = false;
        panel.corporate_actions = {no_child};
        AccountingLedger marker;
        auto marker_out = Simulator(panel, ledger_config(panel, mode, marker))
                              .run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(marker.actions[2].legs_unresolved, 1u) << "mode=" << mode;
        EXPECT_EQ(marker.actions[2].distribution_value_only, 0u);
        EXPECT_NEAR(marker.actions[2].distribution_pnl, 0.0, 1e-12);
        EXPECT_EQ(marker_out.unconfirmed[0].kind, UnconfirmedKind::UnresolvedLeg);
        // Switched off with the other stock legs.
        panel.corporate_actions = {implied};
        AccountingLedger off_ledger;
        auto off = ledger_config(panel, mode, off_ledger);
        off.apply_distributions = false;
        Simulator(panel, off).run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(off_ledger.actions[2].distribution_value_only, 0u);
        EXPECT_NEAR(off_ledger.actions[2].distribution_pnl, 0.0, 1e-12);
        // A child inside the panel keeps the position path: never quoted + ledger close -> LedgerValue.
        auto inside = stock_leg(2, 0, 1, 0.5, 40.0, "vendor_implied");
        inside.child_value = 20.0;
        for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = kMissing;
        panel.corporate_actions = {inside};
        std::vector<std::vector<float>> hold(panel.T, {1, 0});
        hold[0] = {0, 0};
        AccountingLedger created;
        auto created_out = Simulator(panel, ledger_config(panel, mode, created))
                               .run_from_signals(plan_signals(hold, mode));
        EXPECT_EQ(created.actions[2].child_positions_created, 1u) << "mode=" << mode;
        EXPECT_EQ(created.actions[2].distribution_value_only, 0u);
        ASSERT_EQ(created_out.distributions.size(), 1u);
        EXPECT_EQ(created_out.distributions[0].source, DistributionSource::LedgerValue);
    }
}

// 54 (R-Y1 P1-1): a marker cash leg dated on the parent's settlement bar beside an events
// cash leg is count-only (the events row settles the P&L), so it is not unconfirmed and
// strict does not fire; beside a successor-only events row the marker is booked by nobody
// and stays unconfirmed. Two legs of one parent on one bar count the exposure once.
TEST(CorporateActions, MarkerCashLegsDeferredToTheEventsRowAreNotUnconfirmed) {
    for (int mode : {0, 1}) {
        std::vector<std::vector<float>> plan(8, {1, 0});
        plan[0] = {0, 0};
        auto marker = cash_leg(4, 0, kNaN);          // no_cash_amount marker on the settlement bar
        marker.unresolved = true;
        // Events cash 14.5 settles the parent on bar 4: the marker is deferred, confirmed.
        auto deferred = split_path_panel(2, 100.0f);
        deferred.settlement_cash[0] = 14.5;
        deferred.settlement_has_cash_leg[0] = 1;
        deferred.corporate_actions = {marker};
        AccountingLedger ledger;
        auto strict = ledger_config(deferred, mode, ledger);
        strict.unresolved_policy = UnresolvedPolicy::Strict;
        SimulatorOutput out;
        EXPECT_NO_THROW(out = Simulator(deferred, strict).run_from_signals(plan_signals(plan, mode))) << "mode=" << mode;
        EXPECT_NEAR(ledger.gross[4], 14.5 / 100.0 - 1.0, 1e-12) << "mode=" << mode;
        EXPECT_TRUE(out.unconfirmed.empty());
        EXPECT_EQ(out.corporate_actions[4].legs_deferred_to_events, 1u);
        EXPECT_EQ(out.corporate_actions[4].legs_on_settlement_bar, 1u);
        EXPECT_EQ(out.corporate_actions[4].legs_unresolved, 0u);       // count-only legs are not the ledger's to resolve
        EXPECT_EQ(out.corporate_actions[4].unconfirmed_count, 0u);
        // A successor-only events row leaves the cash to the ledger: the marker is unconfirmed.
        auto successor_only = split_path_panel(2, 100.0f);
        for (unsigned di = 4; di < successor_only.T; ++di) successor_only.at("close", di, 1) = 50.0f;
        successor_only.settlement_successor[0] = 1;
        successor_only.settlement_successor_ratio[0] = 1.0;
        successor_only.settlement_has_stock_leg[0] = 1;
        successor_only.corporate_actions = {marker};
        AccountingLedger warned;
        auto warn_out = Simulator(successor_only, ledger_config(successor_only, mode, warned))
                            .run_from_signals(plan_signals(plan, mode));
        ASSERT_EQ(warn_out.unconfirmed.size(), 1u) << "mode=" << mode;
        EXPECT_EQ(warn_out.unconfirmed[0].bar, 4u);
        EXPECT_EQ(warn_out.unconfirmed[0].detail, "no_cash_amount");
        EXPECT_NEAR(warn_out.unconfirmed[0].weight_before, 1.0, 1e-12);   // pre-settlement exposure
        EXPECT_EQ(warn_out.corporate_actions[4].legs_unresolved, 1u);
        EXPECT_NEAR(warned.gross[4], 50.0 / 100.0 - 1.0, 1e-12);
        AccountingLedger refused;
        auto strict_successor = ledger_config(successor_only, mode, refused);
        strict_successor.unresolved_policy = UnresolvedPolicy::Strict;
        EXPECT_THROW(Simulator(successor_only, strict_successor).run_from_signals(plan_signals(plan, mode)), std::runtime_error);
        // Two markers of one parent on one bar: two records, the exposure counted once.
        auto panel = flat_panel(5, 2);
        auto no_amount = cash_leg(2, 0, kNaN);
        no_amount.unresolved = true;
        auto no_child = stock_leg(2, 0, -1, kNaN);
        no_child.unresolved = true;
        no_child.child_named = false;
        panel.corporate_actions = {no_amount, no_child};
        std::vector<std::vector<float>> hold(panel.T, {0.5f, 0.5f});
        hold[0] = {0, 0};
        AccountingLedger twice;
        auto twice_out = Simulator(panel, ledger_config(panel, mode, twice)).run_from_signals(plan_signals(hold, mode));
        EXPECT_EQ(twice_out.corporate_actions[2].unconfirmed_count, 2u) << "mode=" << mode;
        EXPECT_NEAR(twice_out.corporate_actions[2].unconfirmed_abs_weight, 0.5, 1e-12);
        EXPECT_EQ(twice_out.unconfirmed.size(), 2u);
    }
}

// 55 (user review 09-21): a stock leg priced from the child's quote is still an estimate when
// the ledger flagged the row (the ratio or the ex-date was estimated): the accounting is the
// quote's, bit for bit, but the record carries the flag, distribution_estimated counts it and
// the parent is reported unconfirmed with the export's reason; strict refuses. Cash legs alike.
TEST(CorporateActions, QuotePricedLegsFlaggedByTheLedgerStayUnconfirmedWithTheirReason) {
    for (int mode : {0, 1}) {
        auto panel = flat_panel(6, 2);
        for (unsigned di = 2; di < panel.T; ++di) panel.at("close", di, 0) = 80.0f;
        for (unsigned di = 0; di < panel.T; ++di) panel.at("close", di, 1) = 40.0f;
        auto plain = stock_leg(2, 0, 1, 0.5, 40.0, "child_close_ex");
        auto flagged = plain;
        flagged.estimate = true;
        flagged.estimate_reason = "ratio";
        std::vector<std::vector<float>> plan(panel.T, {1, 0});
        plan[0] = {0, 0};
        panel.corporate_actions = {plain};
        AccountingLedger reference;
        auto plain_out = Simulator(panel, ledger_config(panel, mode, reference)).run_from_signals(plan_signals(plan, mode));
        panel.corporate_actions = {flagged};
        AccountingLedger ledger;
        auto out = Simulator(panel, ledger_config(panel, mode, ledger)).run_from_signals(plan_signals(plan, mode));
        // Same numbers: (80 + 0.5 x 40)/100 - 1 = 0 on the ex bar, child weight 0.2, sold next bar.
        for (unsigned di = 0; di < panel.T; ++di) {
            EXPECT_EQ(out.ret[di], plain_out.ret[di]) << "mode=" << mode << " bar=" << di;
            EXPECT_EQ(out.turnover[di], plain_out.turnover[di]);
            EXPECT_EQ(ledger.weights[di], reference.weights[di]);
        }
        EXPECT_NEAR(ledger.gross[2], 0.0, 1e-12);
        EXPECT_TRUE(plain_out.unconfirmed.empty());
        ASSERT_EQ(plain_out.distributions.size(), 1u);
        EXPECT_FALSE(plain_out.distributions[0].estimate);
        EXPECT_EQ(plain_out.corporate_actions[2].distribution_estimated, 0u);
        ASSERT_EQ(out.distributions.size(), 1u);
        EXPECT_EQ(out.distributions[0].source, DistributionSource::ChildClose);
        EXPECT_TRUE(out.distributions[0].estimate);
        EXPECT_EQ(out.corporate_actions[2].distribution_estimated, 1u);
        EXPECT_EQ(out.corporate_actions[2].child_positions_created, 1u);
        ASSERT_EQ(out.unconfirmed.size(), 1u);
        EXPECT_EQ(out.unconfirmed[0].bar, 2u);
        EXPECT_EQ(out.unconfirmed[0].kind, UnconfirmedKind::EstimateValue);
        EXPECT_EQ(out.unconfirmed[0].detail, "child_close:ratio");
        EXPECT_NEAR(out.unconfirmed[0].weight_before, 1.0, 1e-12);
        AccountingLedger refused;
        auto strict = ledger_config(panel, mode, refused);
        strict.unresolved_policy = UnresolvedPolicy::Strict;
        try {
            Simulator(panel, strict).run_from_signals(plan_signals(plan, mode));
            ADD_FAILURE() << "strict must refuse the flagged quote-priced leg, mode=" << mode;
        } catch (const std::runtime_error& error) {
            EXPECT_NE(std::string(error.what()).find("S0 estimate_value(child_close:ratio)"), std::string::npos) << error.what();
        }
        // A pending flagged leg keeps the flag at its first quote, with the first_quote path named.
        auto waiting = panel;
        for (unsigned di = 0; di < 3; ++di) waiting.at("close", di, 1) = kMissing;
        auto pending = flagged;
        pending.estimate_reason = "ratio+date";
        waiting.corporate_actions = {pending};
        AccountingLedger later;
        auto later_out = Simulator(waiting, ledger_config(waiting, mode, later)).run_from_signals(plan_signals(plan, mode));
        ASSERT_EQ(later_out.unconfirmed.size(), 1u) << "mode=" << mode;
        EXPECT_EQ(later_out.unconfirmed[0].bar, 3u);
        EXPECT_EQ(later_out.unconfirmed[0].detail, "first_quote:ratio+date");
        EXPECT_EQ(later_out.corporate_actions[3].distribution_estimated, 1u);
        ASSERT_EQ(later_out.distributions.size(), 1u);
        EXPECT_EQ(later_out.distributions[0].source, DistributionSource::FirstQuote);
        EXPECT_TRUE(later_out.distributions[0].estimate);
        // A cash leg flagged for its date: booked as stated, reported as cash:date.
        auto dated = flat_panel(5, 2);
        auto cash = cash_leg(2, 0, 1.0);
        cash.estimate = true;
        cash.estimate_reason = "date";
        dated.corporate_actions = {cash};
        std::vector<std::vector<float>> short_plan(dated.T, {1, 0});
        short_plan[0] = {0, 0};
        AccountingLedger paid;
        auto paid_out = Simulator(dated, ledger_config(dated, mode, paid)).run_from_signals(plan_signals(short_plan, mode));
        EXPECT_NEAR(paid.actions[2].dividend_pnl, 1.0 / 100.0, 1e-12) << "mode=" << mode;
        EXPECT_EQ(paid.actions[2].dividend_estimated, 1u);
        ASSERT_EQ(paid_out.unconfirmed.size(), 1u);
        EXPECT_EQ(paid_out.unconfirmed[0].detail, "cash:date");
        // Unflagged legs, whatever their source text, list nothing.
        auto unflagged = flat_panel(5, 2);
        unflagged.corporate_actions = {cash_leg(2, 0, 1.0), stock_leg(3, 0, 1, 0.5, 100.0, "child_close_ex")};
        AccountingLedger quiet;
        auto quiet_out = Simulator(unflagged, ledger_config(unflagged, mode, quiet)).run_from_signals(plan_signals(short_plan, mode));
        EXPECT_TRUE(quiet_out.unconfirmed.empty()) << "mode=" << mode;
        EXPECT_EQ(quiet.actions[3].distribution_estimated, 0u);
    }
}


// ── No ledger: the pre-change engine dump is reproduced bit for bit ─────────
// This generator is byte-identical to bitwise_fixture in test_simulator.cpp,
// whose golden dump was produced by the HEAD 2e77af1 engine. A panel with no
// ledger legs, and a panel whose legs are switched off, must both match it.
namespace no_ledger_fixture {

inline uint32_t draw(uint32_t& state) {
    state = state * 1664525u + 1013904223u;
    return state >> 8;
}

inline std::string digest(const void* bytes, std::size_t length) {
    const auto* data = static_cast<const unsigned char*>(bytes);
    uint64_t hash = 1469598103934665603ull;
    for (std::size_t i = 0; i < length; ++i) {
        hash ^= data[i];
        hash *= 1099511628211ull;
    }
    char text[19];
    std::snprintf(text, sizeof(text), "0x%016llx", static_cast<unsigned long long>(hash));
    return text;
}

inline DataPanel panel() {
    DataPanel p;
    p.T = 96;
    p.N = 14;
    p.timestamps.resize(p.T);
    for (unsigned t = 0; t < p.T; ++t)
        p.timestamps[t] = static_cast<int64_t>(t) * 86400000;
    p.symbols.resize(p.N);
    for (unsigned ii = 0; ii < p.N; ++ii)
        p.symbols[ii] = "B" + std::to_string(ii);
    auto& close = p.arrays["close"];
    close.resize(static_cast<std::size_t>(p.T) * p.N);
    p.universe_mask.resize(close.size());
    uint32_t state = 20260911u;
    for (unsigned t = 0; t < p.T; ++t) {
        for (unsigned ii = 0; ii < p.N; ++ii) {
            const std::size_t at = static_cast<std::size_t>(t) * p.N + ii;
            close[at] = static_cast<float>(20000u + draw(state) % 900000u) / 1000.0f;
            p.universe_mask[at] = ((t / 5 + ii * 3) % 7 == 0) ? 0 : 1;
        }
    }
    return p;
}

inline std::vector<std::vector<float>> signals(unsigned variant, const DataPanel& p) {
    std::vector<std::vector<float>> rows(p.T, std::vector<float>(p.N, 0.0f));
    uint32_t state = 7717u + variant * 101u;
    for (unsigned t = 0; t < p.T; ++t) {
        for (unsigned ii = 0; ii < p.N; ++ii) {
            const uint32_t value = draw(state);
            if (variant == 1 && value % 11u == 0u)
                rows[t][ii] = std::numeric_limits<float>::quiet_NaN();
            else if (variant == 2 && (t + ii) % 4u == 0u)
                rows[t][ii] = 0.0f;
            else
                rows[t][ii] =
                    static_cast<float>(static_cast<int32_t>(value % 2001u) - 1000) / 1000.0f;
        }
    }
    return rows;
}

inline std::string panel_digest(const DataPanel& p) {
    const auto& close = p.arrays.at("close");
    std::string combined = digest(close.data(), close.size() * sizeof(float));
    combined += digest(p.universe_mask.data(), p.universe_mask.size());
    return digest(combined.data(), combined.size());
}

inline std::string signal_digest(const std::vector<std::vector<float>>& rows) {
    std::string combined;
    for (const auto& row : rows)
        combined += digest(row.data(), row.size() * sizeof(float));
    return digest(combined.data(), combined.size());
}

struct Case { unsigned variant; int mode; unsigned sim_start; };
inline std::vector<Case> cases() {
    return {{0, 0, 0}, {0, 1, 0}, {1, 0, 3}, {1, 1, 3}, {2, 0, 6}, {2, 1, 6}};
}

inline std::string row_text(double ret, double turnover) {
    char text[64];
    std::snprintf(text, sizeof(text), "%a %a", ret, turnover);
    return text;
}
}  // namespace no_ledger_fixture

namespace {
std::filesystem::path find_golden(const std::string& name) {
    namespace fs = std::filesystem;
    const std::vector<fs::path> candidates = {
        fs::path(__FILE__).parent_path() / "golden" / name,
        fs::current_path() / "tests" / "golden" / name,
        fs::current_path() / "golden" / name,
        fs::current_path() / ".." / "tests" / "golden" / name,
    };
    for (const auto& candidate : candidates)
        if (fs::exists(candidate)) return candidate;
    return {};
}
}  // namespace

TEST(CorporateActions, InputsWithoutALedgerReproduceThePreChangeDumpBitForBit) {
    const std::filesystem::path golden = find_golden("missing_close_bitwise.json");
    ASSERT_FALSE(golden.empty()) << "tests/golden/missing_close_bitwise.json not found";
    std::ifstream input(golden);
    ASSERT_TRUE(input.good()) << golden;
    Json::Value expected;
    Json::CharReaderBuilder builder;
    std::string errors;
    ASSERT_TRUE(Json::parseFromStream(builder, input, &expected, &errors)) << errors;

    const DataPanel panel = no_ledger_fixture::panel();
    ASSERT_EQ(no_ledger_fixture::panel_digest(panel), expected["panelDigest"].asString());
    // The same input with legs that are switched off runs the booking path with
    // nothing to book, which must not disturb the accumulation order either.
    DataPanel disabled = panel;
    disabled.corporate_actions = {cash_leg(10, 3, 1.0), stock_leg(20, 4, 5, 0.5)};
    const auto cases = no_ledger_fixture::cases();
    ASSERT_EQ(expected["cases"].size(), cases.size());
    for (Json::ArrayIndex c = 0; c < expected["cases"].size(); ++c) {
        const Json::Value& recorded = expected["cases"][c];
        const auto& plan = cases[c];
        const auto signals = no_ledger_fixture::signals(plan.variant, panel);
        ASSERT_EQ(no_ledger_fixture::signal_digest(signals), recorded["signalDigest"].asString());
        ASSERT_EQ(recorded["rows"].size(), panel.T);
        for (int with_legs : {0, 1}) {
            SimulatorConfig config;
            config.T = panel.T;
            config.N = panel.N;
            config.fee_rate = 0.0005;
            config.mode = plan.mode;
            config.sim_start_index = plan.sim_start;
            if (with_legs) {
                config.apply_dividends = false;
                config.apply_distributions = false;
            }
            const auto out = Simulator(with_legs ? disabled : panel, config).run_from_signals(signals);
            for (unsigned t = 0; t < panel.T; ++t)
                ASSERT_EQ(no_ledger_fixture::row_text(out.ret[t], out.turnover[t]),
                          recorded["rows"][t].asString())
                    << "case=" << c << " with_legs=" << with_legs << " bar=" << t;
            for (const auto& bar : out.corporate_actions) {
                EXPECT_EQ(bar.dividend_count, 0u);
                EXPECT_EQ(bar.distribution_count, 0u);
            }
        }
    }
}


// ── Ledger parquet -> DataPanel ──────────────────────────────────────────────

namespace {
struct DataFiles {
    std::filesystem::path dir;
    DataFiles() {
        static std::atomic<unsigned> sequence{0};
        dir = std::filesystem::temp_directory_path() /
              ("longshort_ca_" + std::to_string(getpid()) + "_" + std::to_string(sequence++));
        std::filesystem::create_directories(dir);
    }
    ~DataFiles() { std::error_code error; std::filesystem::remove_all(dir, error); }
    void write(const std::string& name, const std::vector<std::string>& names,
               const std::vector<std::shared_ptr<arrow::Array>>& arrays) {
        std::vector<std::shared_ptr<arrow::Field>> fields;
        for (size_t i = 0; i < names.size(); ++i) fields.push_back(arrow::field(names[i], arrays[i]->type()));
        auto output = arrow::io::FileOutputStream::Open((dir / name).string()).ValueOrDie();
        auto table = arrow::Table::Make(arrow::schema(fields), arrays);
        auto status = parquet::arrow::WriteTable(*table, arrow::default_memory_pool(), output, 2);
        if (!status.ok()) throw std::runtime_error(status.ToString());
        if (!output->Close().ok()) throw std::runtime_error("Cannot close fixture");
    }
};
std::shared_ptr<arrow::Array> numeric(const std::vector<double>& input) {
    arrow::DoubleBuilder builder;
    for (double value : input) {
        if (std::isnan(value)) { if (!builder.AppendNull().ok()) throw std::runtime_error("append"); }
        else if (!builder.Append(value).ok()) throw std::runtime_error("append");
    }
    return builder.Finish().ValueOrDie();
}
std::shared_ptr<arrow::Array> dates(const std::vector<int64_t>& input) {
    arrow::TimestampBuilder builder(arrow::timestamp(arrow::TimeUnit::NANO), arrow::default_memory_pool());
    for (int64_t value : input)
        if (!builder.Append(value * 1000000).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}
// Empty strings become nulls so the child column can name "no child".
std::shared_ptr<arrow::Array> text(const std::vector<std::string>& input) {
    arrow::StringBuilder builder;
    for (const auto& value : input) {
        if (value.empty()) { if (!builder.AppendNull().ok()) throw std::runtime_error("append"); }
        else if (!builder.Append(value).ok()) throw std::runtime_error("append");
    }
    return builder.Finish().ValueOrDie();
}
std::shared_ptr<arrow::Array> int_ids(const std::vector<int64_t>& input) {
    arrow::Int64Builder builder;
    for (int64_t value : input)
        if (!builder.Append(value).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}

std::shared_ptr<arrow::Array> flags(const std::vector<bool>& input) {
    arrow::BooleanBuilder builder;
    for (bool value : input)
        if (!builder.Append(value).ok()) throw std::runtime_error("append");
    return builder.Finish().ValueOrDie();
}

// One ledger row in the exported column layout (BRIEF 3-3). The leg type follows
// the event type unless given: cash_dividend/special_dividend -> cash, split -> quantity,
// rights -> rights, everything else -> stock.
struct Row {
    std::string id, ex, type, child;
    double cash = kNaN, ratio = kNaN, child_close = kNaN, parent_close = kNaN, parent_before = kNaN;
    double factor = 1.0;
    std::string leg, status = "ok", value_source = "unknown";
    bool estimate = false;
};
const std::vector<std::string> kLedgerColumns = {
    "tradingItemId", "exDate", "eventType", "legType", "cashPerShareCache", "childTradingItemId",
    "childRatioCache", "childCloseAtExCache", "parentCloseAtExCache", "parentCloseBeforeCache",
    "parentFactorCache", "isEstimate", "status", "childValueSource"};
int64_t day_of(const std::string& iso) { return config_date_ms(iso); }
std::string leg_of(const Row& row) {
    if (!row.leg.empty()) return row.leg;
    if (row.type == "cash_dividend" || row.type == "special_dividend") return "cash";
    if (row.type == "split") return "quantity";
    if (row.type == "rights") return "rights";
    return "stock";
}
void write_ledger(DataFiles& files, const std::vector<Row>& rows, const std::string& name = "ledger.parquet") {
    std::vector<std::string> ids, types, legs, children, statuses, value_sources;
    std::vector<int64_t> ex;
    std::vector<double> cash, ratio, child_close, parent_close, parent_before, factor;
    std::vector<bool> estimates;
    for (const auto& row : rows) {
        ids.push_back(row.id); ex.push_back(day_of(row.ex)); types.push_back(row.type);
        legs.push_back(leg_of(row)); children.push_back(row.child); cash.push_back(row.cash);
        ratio.push_back(row.ratio); child_close.push_back(row.child_close);
        parent_close.push_back(row.parent_close); parent_before.push_back(row.parent_before);
        factor.push_back(row.factor); estimates.push_back(row.estimate);
        statuses.push_back(row.status); value_sources.push_back(row.value_source);
    }
    files.write(name, kLedgerColumns,
        {text(ids), dates(ex), text(types), text(legs), numeric(cash), text(children), numeric(ratio),
         numeric(child_close), numeric(parent_close), numeric(parent_before), numeric(factor),
         flags(estimates), text(statuses), text(value_sources)});
}
// Four daily bars from 1970-01-01, five symbols A..E at 10/20/30/40/50 (E missing on day 2).
BacktestConfig four_bar_panel(DataFiles& files) {
    files.write("close.parquet", {"A", "B", "C", "D", "E", "__index_level_0__"},
        {numeric({10,10,10,10}), numeric({20,20,20,20}), numeric({30,30,30,30}),
         numeric({40,40,40,40}), numeric({50,50,kNaN,50}), dates({0,kDay,2*kDay,3*kDay})});
    BacktestConfig config;
    config.config_dir = files.dir.string();
    DatasetConfig dataset;
    dataset.name = "prices"; dataset.path = "."; dataset.format = DatasetFormat::Wide;
    dataset.time_col = "__index_level_0__"; dataset.fields = {{"close", "close.parquet"}};
    config.datasets.push_back(dataset);
    config.corporate_actions.path = "ledger.parquet";
    return config;
}
}  // namespace

TEST(CorporateActionsLoader, RowsScheduleLegsOnTheirExDateBarsAndResolveChildren) {
    DataFiles files;
    auto config = four_bar_panel(files);
    write_ledger(files, {
        {"A", "1970-01-02", "cash_dividend", "", 1.0, kNaN, kNaN, 10, 10},
        {"B", "1970-01-03", "spin_off", "D", kNaN, 0.5, 40, 20, 20},
        {"C", "1970-01-03", "spin_off", "Z", kNaN, 0.25, kNaN, 30, 30},   // child not loaded
        {"D", "1970-01-10", "cash_dividend", "", 2.0, kNaN, kNaN, 40, 40},  // after the panel
        {"E", "1970-01-02", "split", "", kNaN, kNaN, kNaN, 50, 50},        // nothing to book
        {"Q", "1970-01-02", "cash_dividend", "", 1.0, kNaN, kNaN, 1, 1},   // parent not loaded
        {"A", "1969-12-20", "cash_dividend", "", 1.0, kNaN, kNaN, 10, 10}, // before the panel
    });
    const auto panel = DataPanel::load(config);
    EXPECT_TRUE(panel.corporate_actions_loaded);
    EXPECT_EQ(panel.corporate_action_rows, 7u);
    EXPECT_EQ(panel.corporate_action_rows_matched, 6u);
    EXPECT_EQ(panel.corporate_action_legs, 3u);
    EXPECT_EQ(panel.corporate_action_out_of_range, 2u);
    EXPECT_EQ(panel.corporate_action_unpriced_children, 1u);
    EXPECT_EQ(panel.corporate_action_price_checks, 3u);
    EXPECT_EQ(panel.corporate_action_price_checks_not_evaluable, 0u);
    ASSERT_EQ(panel.corporate_actions.size(), 3u);
    const auto& cash = panel.corporate_actions[0];
    EXPECT_EQ(cash.bar, 1u);
    EXPECT_EQ(cash.parent, 0u);
    EXPECT_FALSE(cash.stock_leg);
    EXPECT_EQ(cash.child, -1);
    EXPECT_DOUBLE_EQ(cash.cash, 1.0);
    EXPECT_EQ(cash.event_type, "cash_dividend");
    const auto& spin = panel.corporate_actions[1];
    EXPECT_EQ(spin.bar, 2u);
    EXPECT_EQ(spin.parent, 1u);
    EXPECT_TRUE(spin.stock_leg);
    EXPECT_EQ(spin.child, 3);
    EXPECT_TRUE(std::isnan(spin.cash));
    EXPECT_DOUBLE_EQ(spin.child_ratio, 0.5);
    EXPECT_DOUBLE_EQ(spin.child_close, 40.0);
    const auto& unpriced = panel.corporate_actions[2];
    EXPECT_EQ(unpriced.bar, 2u);
    EXPECT_EQ(unpriced.parent, 2u);
    EXPECT_TRUE(unpriced.stock_leg);
    EXPECT_EQ(unpriced.child, -1);
    EXPECT_DOUBLE_EQ(unpriced.child_ratio, 0.25);
    // The legs drive the simulator directly.
    SimulatorConfig sim{panel.T, panel.N, kFee, 0, false, {}};
    EXPECT_NO_THROW(Simulator(panel, sim));

    // Without a configured ledger nothing is loaded or reported.
    config.corporate_actions.path.clear();
    const auto bare = DataPanel::load(config);
    EXPECT_FALSE(bare.corporate_actions_loaded);
    EXPECT_TRUE(bare.corporate_actions.empty());
    EXPECT_EQ(bare.corporate_action_rows, 0u);
}

TEST(CorporateActionsLoader, IntegerIdsAndWhenIssuedValuesAreAccepted) {
    DataFiles files;
    files.write("close.parquet", {"11", "22", "__index_level_0__"},
        {numeric({10,10,10,10}), numeric({kNaN,kNaN,kNaN,25}), dates({0,kDay,2*kDay,3*kDay})});
    BacktestConfig config;
    config.config_dir = files.dir.string();
    DatasetConfig dataset;
    dataset.name = "prices"; dataset.path = "."; dataset.format = DatasetFormat::Wide;
    dataset.time_col = "__index_level_0__"; dataset.fields = {{"close", "close.parquet"}};
    config.datasets.push_back(dataset);
    config.corporate_actions.path = "ledger.parquet";
    // The child has no panel quote on the ex-date; the ledger's 24 is the when-issued value.
    files.write("ledger.parquet", kLedgerColumns,
        {int_ids({11}), dates({2*kDay}), text({"spin_off"}), text({"stock"}), numeric({kNaN}), int_ids({22}),
         numeric({0.5}), numeric({24}), numeric({10}), numeric({10}), numeric({1.0}),
         flags({false}), text({"ok"}), text({"wi_close"})});
    auto panel = DataPanel::load(config);
    ASSERT_EQ(panel.corporate_actions.size(), 1u);
    EXPECT_EQ(panel.corporate_actions[0].parent, 0u);
    EXPECT_EQ(panel.corporate_actions[0].child, 1);
    EXPECT_DOUBLE_EQ(panel.corporate_actions[0].child_close, 24.0);
    EXPECT_TRUE(panel.corporate_actions[0].ledger_value_usable);
    EXPECT_FALSE(panel.corporate_actions[0].estimate);
    EXPECT_EQ(panel.corporate_actions[0].value_source, "wi_close");
    EXPECT_EQ(panel.corporate_action_price_checks, 1u);   // parent anchors verified
    EXPECT_EQ(panel.corporate_action_unpriced_children, 0u);
    EXPECT_EQ(panel.corporate_action_legs_estimated, 0u);

    // int32 child ids and YYYY-MM-DD ex-dates are accepted too.
    arrow::Int32Builder narrow;
    ASSERT_TRUE(narrow.Append(22).ok());
    files.write("ledger.parquet", kLedgerColumns,
        {int_ids({11}), text({"1970-01-03"}), text({"spin_off"}), text({"stock"}), numeric({kNaN}),
         narrow.Finish().ValueOrDie(), numeric({0.5}), numeric({24}), numeric({10}), numeric({10}),
         numeric({1.0}), flags({false}), text({"ok"}), text({"wi_close"})});
    panel = DataPanel::load(config);
    ASSERT_EQ(panel.corporate_actions.size(), 1u);
    EXPECT_EQ(panel.corporate_actions[0].bar, 2u);
    EXPECT_EQ(panel.corporate_actions[0].child, 1);
}

TEST(CorporateActionsLoader, StaleAnchorsRefuseTheStart) {
    DataFiles files;
    auto config = four_bar_panel(files);
    const auto rejects = [&](const std::vector<Row>& rows, const std::string& fragment) {
        write_ledger(files, rows);
        try {
            DataPanel::load(config);
            ADD_FAILURE() << "expected a load failure containing: " << fragment;
        } catch (const std::runtime_error& error) {
            EXPECT_NE(std::string(error.what()).find(fragment), std::string::npos) << error.what();
        }
    };
    rejects({{"A", "1970-01-02", "cash_dividend", "", 1.0, kNaN, kNaN, 11, 10}}, "stale");
    rejects({{"A", "1970-01-02", "cash_dividend", "", 1.0, kNaN, kNaN, 10, 12}}, "stale");
    rejects({{"B", "1970-01-03", "spin_off", "D", kNaN, 0.5, 41, 20, 20}}, "child close");
    // The cache quotes the parent on the ex-date but the ledger recorded no close.
    rejects({{"A", "1970-01-02", "cash_dividend", "", 1.0, kNaN, kNaN, kNaN, 10}}, "stale");
    // The ledger recorded a close the cache does not have (E is missing on day 2).
    rejects({{"E", "1970-01-03", "cash_dividend", "", 1.0, kNaN, kNaN, 50, 50}}, "stale");
    // Within tolerance passes; E with no close on either side is not evaluable
    // for the ex-date anchor but still checked on the bar before.
    write_ledger(files, {{"A", "1970-01-02", "cash_dividend", "", 1.0, kNaN, kNaN, 10.0005, 9.9995},
                         {"E", "1970-01-03", "cash_dividend", "", 1.0, kNaN, kNaN, kNaN, 50}});
    auto panel = DataPanel::load(config);
    EXPECT_EQ(panel.corporate_action_legs, 2u);
    EXPECT_EQ(panel.corporate_action_price_checks, 2u);
    // An amount the engine will divide by the previous mark needs that mark's anchor.
    rejects({{"E", "1970-01-03", "cash_dividend", "", 1.0, kNaN, kNaN, kNaN, kNaN}}, "without price anchors");
    // An unresolved amount has nothing to verify, so a bare row is merely not evaluable.
    Row unresolved{"E", "1970-01-03", "cash_dividend", "", kNaN, kNaN, kNaN, kNaN, kNaN};
    unresolved.status = "unresolved";
    write_ledger(files, {unresolved});
    panel = DataPanel::load(config);
    EXPECT_EQ(panel.corporate_action_legs, 1u);
    EXPECT_EQ(panel.corporate_action_price_checks, 0u);
    EXPECT_EQ(panel.corporate_action_price_checks_not_evaluable, 1u);
    // The before anchor is the last observed close, not blindly the previous bar: with
    // A silent on day 1, an ex-date on day 2 is checked against day 0.
    files.write("close.parquet", {"A", "B", "C", "D", "E", "__index_level_0__"},
        {numeric({12,kNaN,10,10}), numeric({20,20,20,20}), numeric({30,30,30,30}),
         numeric({40,40,40,40}), numeric({50,50,50,50}), dates({0,kDay,2*kDay,3*kDay})});
    write_ledger(files, {{"A", "1970-01-03", "cash_dividend", "", 1.0, kNaN, kNaN, 10, 12}});
    EXPECT_EQ(DataPanel::load(config).corporate_action_price_checks, 1u);
    rejects({{"A", "1970-01-03", "cash_dividend", "", 1.0, kNaN, kNaN, 10, 10}}, "before the ex-date");
    // stale_check: false loads a mismatching ledger and reports nothing checked.
    write_ledger(files, {{"A", "1970-01-02", "cash_dividend", "", 1.0, kNaN, kNaN, 11, 10}});
    config.corporate_actions.stale_check = false;
    panel = DataPanel::load(config);
    EXPECT_EQ(panel.corporate_action_legs, 1u);
    EXPECT_EQ(panel.corporate_action_price_checks, 0u);
    EXPECT_EQ(panel.corporate_action_price_checks_not_evaluable, 1u);
}

TEST(CorporateActionsLoader, RefusesTheWrongBasisScopeCalendarAndFiles) {
    DataFiles files;
    auto config = four_bar_panel(files);
    const auto rejects = [&](const std::vector<Row>& rows, const std::string& fragment) {
        write_ledger(files, rows);
        try {
            DataPanel::load(config);
            ADD_FAILURE() << "expected a load failure containing: " << fragment;
        } catch (const std::runtime_error& error) {
            EXPECT_NE(std::string(error.what()).find(fragment), std::string::npos) << error.what();
        }
    };
    Row factor{"A", "1970-01-02", "cash_dividend", "", 1.0, kNaN, kNaN, 10, 10};
    factor.factor = 2.0;
    rejects({factor}, "parentFactorCache");
    factor.factor = kNaN;
    rejects({factor}, "parentFactorCache");
    Row unrelated{"Q", "1970-01-02", "split", "", kNaN, kNaN, kNaN, 1, 1};   // not even a panel symbol
    unrelated.factor = 0.5;
    rejects({unrelated}, "parentFactorCache");
    for (const char* type : {"merger_cash", "merger_stock", "rename", "relist"})
        rejects({{"A", "1970-01-02", type, "B", kNaN, 1.0, 20, 10, 10}}, "events file");
    rejects({{"A", "1970-01-02", "spin_off", "A", kNaN, 1.0, 10, 10, 10}}, "own child");
    rejects({{"A", "1970-01-02", "spin_off", "B", kNaN, -1.0, 20, 10, 10}}, "negative");
    Row untyped{"A", "1970-01-02", "", "", 1.0, kNaN, kNaN, 10, 10};
    untyped.leg = "cash";
    rejects({untyped}, "no event type");
    // A date inside the panel's range that is not a bar is a calendar mismatch.
    files.write("close.parquet", {"A", "B", "C", "D", "E", "__index_level_0__"},
        {numeric({10,10,10,10}), numeric({20,20,20,20}), numeric({30,30,30,30}),
         numeric({40,40,40,40}), numeric({50,50,50,50}), dates({0,kDay,3*kDay,4*kDay})});
    rejects({{"A", "1970-01-03", "cash_dividend", "", 1.0, kNaN, kNaN, 10, 10}}, "not a bar");
    // Missing file and missing column.
    config.corporate_actions.path = "absent.parquet";
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);
    config.corporate_actions.path = "ledger.parquet";
    config.corporate_actions.cash_col = "absentColumn";
    write_ledger(files, {{"A", "1970-01-02", "cash_dividend", "", 1.0, kNaN, kNaN, 10, 10}});
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);
}

TEST(CorporateActionsLoader, LoadedLegsRunThroughTheSimulatorEndToEnd) {
    DataFiles files;
    auto config = four_bar_panel(files);
    // Six bars so the spun-off shares still have a next bar to be sold on.
    files.write("close.parquet", {"A", "B", "C", "D", "E", "__index_level_0__"},
        {numeric({10,10,10,10,10,10}), numeric({20,20,20,20,20,20}), numeric({30,30,30,30,30,30}),
         numeric({40,40,40,40,40,40}), numeric({50,50,50,50,50,50}),
         dates({0,kDay,2*kDay,3*kDay,4*kDay,5*kDay})});
    // B pays $1 on day 2 and spins 0.5 D per share on day 3 (D at 40 against B at 20).
    write_ledger(files, {
        {"B", "1970-01-03", "cash_dividend", "", 1.0, kNaN, kNaN, 20, 20},
        {"B", "1970-01-04", "spin_off", "D", kNaN, 0.5, 40, 20, 20},
    });
    const auto panel = DataPanel::load(config);
    ASSERT_EQ(panel.corporate_actions.size(), 2u);
    for (int mode : {0, 1}) {
        AccountingLedger ledger;
        std::vector<std::vector<float>> plan(panel.T, {0, 1, 0, 0, 0});
        plan[0] = {0, 0, 0, 0, 0};
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));
        EXPECT_NEAR(ledger.gross[2], 1.0 / 20.0, 1e-12) << "mode=" << mode;
        EXPECT_EQ(out.corporate_actions[2].dividend_count, 1u);
        EXPECT_NEAR(ledger.gross[3], 0.5 * 40.0 / 20.0, 1e-12);
        EXPECT_EQ(out.corporate_actions[3].child_positions_created, 1u);
        EXPECT_NEAR(ledger.weights[3][3], 1.0, 1e-12);
        EXPECT_NEAR(out.turnover[4], 1.0, 1e-12);   // D sold on the next bar
        ASSERT_EQ(out.distributions.size(), 1u);
        EXPECT_EQ(out.distributions[0].child, 3);
    }
}

// The exported leg type keys the rows and decides the leg; a row that carries both
// a cash amount and a child cannot be keyed and is refused (same-day cash and stock
// are two rows). Reviewer probe K1.
TEST(CorporateActionsLoader, LegTypesKeyTheRowsAndMixedLegsAreRefused) {
    DataFiles files;
    auto config = four_bar_panel(files);
    const auto rejects = [&](const std::vector<Row>& rows, const std::string& fragment) {
        write_ledger(files, rows);
        try {
            DataPanel::load(config);
            ADD_FAILURE() << "expected a load failure containing: " << fragment;
        } catch (const std::runtime_error& error) {
            EXPECT_NE(std::string(error.what()).find(fragment), std::string::npos) << error.what();
        }
    };
    Row mixed{"A", "1970-01-03", "spin_off", "D", 1.0, 0.5, 40, 10, 10};   // cash and child on a stock row
    rejects({mixed, {"A", "1970-01-03", "spin_off", "D", kNaN, 0.5, 40, 10, 10}}, "carries a cash amount");
    Row cash_with_child{"A", "1970-01-03", "cash_dividend", "D", 1.0, kNaN, kNaN, 10, 10};
    rejects({cash_with_child}, "names a child");
    Row quantity_with_child{"A", "1970-01-03", "split", "D", kNaN, kNaN, kNaN, 10, 10};
    rejects({quantity_with_child}, "names a child");
    Row unknown_leg{"A", "1970-01-03", "spin_off", "D", kNaN, 0.5, 40, 10, 10};
    unknown_leg.leg = "shares";
    rejects({unknown_leg}, "expected cash, stock, rights or quantity");
    // The key is (parent, ex-date, leg type, child): repeats are refused, distinct legs kept.
    rejects({{"A", "1970-01-03", "spin_off", "D", kNaN, 0.5, 40, 10, 10},
             {"A", "1970-01-03", "spin_off", "D", kNaN, 0.5, 40, 10, 10}}, "repeats");
    rejects({{"A", "1970-01-03", "cash_dividend", "", 1.0, kNaN, kNaN, 10, 10},
             {"A", "1970-01-03", "special_dividend", "", 2.0, kNaN, kNaN, 10, 10}}, "repeats");
    write_ledger(files, {{"A", "1970-01-03", "cash_dividend", "", 1.0, kNaN, kNaN, 10, 10},
                         {"A", "1970-01-03", "spin_off", "D", kNaN, 0.5, 40, 10, 10},
                         {"A", "1970-01-03", "spin_off", "C", kNaN, 0.5, 30, 10, 10},
                         {"A", "1970-01-03", "split", "", kNaN, kNaN, kNaN, 10, 10}});
    const auto panel = DataPanel::load(config);
    EXPECT_EQ(panel.corporate_action_rows_matched, 4u);
    EXPECT_EQ(panel.corporate_action_rows_quantity, 1u);
    ASSERT_EQ(panel.corporate_actions.size(), 3u);
    EXPECT_FALSE(panel.corporate_actions[0].stock_leg);
    EXPECT_TRUE(panel.corporate_actions[1].stock_leg);
    EXPECT_TRUE(panel.corporate_actions[2].stock_leg);
    // Rights rows take the stock path and are counted for E3.
    Row rights{"A", "1970-01-03", "rights", "D", kNaN, 0.1, 40, 10, 10};
    write_ledger(files, {rights});
    const auto with_rights = DataPanel::load(config);
    ASSERT_EQ(with_rights.corporate_actions.size(), 1u);
    EXPECT_TRUE(with_rights.corporate_actions[0].stock_leg);
    EXPECT_TRUE(with_rights.corporate_actions[0].rights);
    EXPECT_EQ(with_rights.corporate_action_legs_rights, 1u);
}

// Unresolved rows (an amount or a child the ledger could not resolve) are scheduled
// and counted, never dropped. Reviewer probe K2.
TEST(CorporateActionsLoader, UnresolvedRowsAreScheduledAndCounted) {
    DataFiles files;
    auto config = four_bar_panel(files);
    Row no_amount{"A", "1970-01-03", "cash_dividend", "", kNaN, kNaN, kNaN, 10, 10};
    no_amount.status = "unresolved";
    Row no_child{"B", "1970-01-03", "spin_off", "", kNaN, kNaN, kNaN, 20, 20};
    no_child.status = "unresolved";
    Row flagged{"C", "1970-01-03", "cash_dividend", "", 1.0, kNaN, kNaN, 30, 30};   // resolved amount, flagged status
    flagged.status = "unresolved";
    Row quiet{"D", "1970-01-03", "spin_off", "", kNaN, kNaN, kNaN, 40, 40};        // no child, status ok
    write_ledger(files, {no_amount, no_child, flagged, quiet});
    const auto panel = DataPanel::load(config);
    EXPECT_EQ(panel.corporate_action_rows_matched, 4u);
    EXPECT_EQ(panel.corporate_action_rows_unresolved, 3u);
    EXPECT_EQ(panel.corporate_action_legs, 4u);
    ASSERT_EQ(panel.corporate_actions.size(), 4u);
    for (const auto& leg : panel.corporate_actions) EXPECT_TRUE(leg.unresolved) << leg.parent;
    EXPECT_EQ(panel.corporate_actions[1].child, -1);
    EXPECT_EQ(panel.corporate_action_unpriced_children, 0u);   // no child named, so nothing to resolve
    EXPECT_EQ(panel.corporate_action_price_checks, 4u);        // parent anchors still verified
    // Held parents report them; the flagged-but-resolved amount is still paid.
    for (int mode : {0, 1}) {
        AccountingLedger ledger;
        std::vector<std::vector<float>> plan(panel.T, {1, 1, 1, 1, 0});
        plan[0] = {0, 0, 0, 0, 0};
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(out.corporate_actions[2].legs_unresolved, 4u) << "mode=" << mode;
        EXPECT_EQ(out.corporate_actions[2].dividend_count, 1u);
        EXPECT_NEAR(out.corporate_actions[2].dividend_pnl, 0.25 * 1.0 / 30.0, 1e-12);
        EXPECT_EQ(out.corporate_actions[2].distribution_unpriced, 2u);
    }
}

// The estimate columns decide what a ledger value may do: (3)(4) sources and the
// ledger's own flag mark estimates, wi_close is a price, and a child_close_ex value
// without a panel quote can only have come from another cache.
TEST(CorporateActionsLoader, EstimateColumnsAreReadAndCacheClosesMustExistInThePanel) {
    DataFiles files;
    auto config = four_bar_panel(files);   // E has no close on day 2 (1970-01-03)
    Row quoted{"A", "1970-01-03", "spin_off", "D", kNaN, 0.5, 40, 10, 10};
    quoted.value_source = "child_close_ex";
    Row implied{"B", "1970-01-03", "spin_off", "E", kNaN, 0.5, 48, 20, 20};
    implied.value_source = "vendor_implied";
    implied.estimate = true;
    Row when_issued{"C", "1970-01-03", "spin_off", "E", kNaN, 0.5, 49, 30, 30};
    when_issued.value_source = "wi_close";
    Row downgraded{"D", "1970-01-03", "spin_off", "A", kNaN, 0.5, 10, 40, 40};   // quoted child, flagged
    downgraded.value_source = "child_close_ex";
    downgraded.status = "estimated";
    write_ledger(files, {quoted, implied, when_issued, downgraded});
    const auto panel = DataPanel::load(config);
    ASSERT_EQ(panel.corporate_actions.size(), 4u);
    EXPECT_FALSE(panel.corporate_actions[0].estimate);
    EXPECT_FALSE(panel.corporate_actions[0].ledger_value_usable);
    EXPECT_TRUE(panel.corporate_actions[1].estimate);
    EXPECT_TRUE(panel.corporate_actions[1].ledger_value_usable);
    EXPECT_FALSE(panel.corporate_actions[2].estimate);
    EXPECT_TRUE(panel.corporate_actions[2].ledger_value_usable);
    EXPECT_TRUE(panel.corporate_actions[3].estimate);
    EXPECT_FALSE(panel.corporate_actions[3].ledger_value_usable);
    EXPECT_EQ(panel.corporate_action_legs_estimated, 2u);
    EXPECT_EQ(panel.corporate_actions[1].value_source, "vendor_implied");
    // An estimate for a quoted child is not compared with the quote (the quote wins at run time).
    Row estimate_with_quote{"A", "1970-01-03", "spin_off", "D", kNaN, 0.5, 41, 10, 10};
    estimate_with_quote.value_source = "vendor_implied";
    write_ledger(files, {estimate_with_quote});
    EXPECT_EQ(DataPanel::load(config).corporate_actions.size(), 1u);
    // A cache close the panel does not have is a different cache.
    Row foreign{"A", "1970-01-03", "spin_off", "E", kNaN, 0.5, 50, 10, 10};
    foreign.value_source = "child_close_ex";
    write_ledger(files, {foreign});
    try {
        DataPanel::load(config);
        ADD_FAILURE() << "a child_close_ex value without a panel quote must be refused";
    } catch (const std::runtime_error& error) {
        EXPECT_NE(std::string(error.what()).find("stale"), std::string::npos) << error.what();
        EXPECT_NE(std::string(error.what()).find("child_close_ex"), std::string::npos) << error.what();
    }
    // A quoted child_close_ex value still has to match the panel.
    Row wrong{"A", "1970-01-03", "spin_off", "D", kNaN, 0.5, 41, 10, 10};
    wrong.value_source = "child_close_ex";
    write_ledger(files, {wrong});
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);
}

// Legs that fall on or after the parent's settlement bar are counted: the parent
// holds nothing there, so such consideration has to live in the events file.
TEST(CorporateActionsLoader, LegsAfterTheParentsEndDateAreCounted) {
    DataFiles files;
    auto config = four_bar_panel(files);
    files.write("events.parquet", {"tradingItemId", "securityEndDate"},
                {text({"A"}), dates({kDay})});   // A ends on day 1: settlement opens on bar 2
    config.events.path = "events.parquet";
    write_ledger(files, {{"A", "1970-01-02", "cash_dividend", "", 1.0, kNaN, kNaN, 10, 10},
                         {"A", "1970-01-03", "cash_dividend", "", 1.0, kNaN, kNaN, 10, 10},
                         {"B", "1970-01-03", "cash_dividend", "", 1.0, kNaN, kNaN, 20, 20}});
    const auto panel = DataPanel::load(config);
    EXPECT_EQ(panel.corporate_action_legs, 3u);
    EXPECT_EQ(panel.corporate_action_legs_after_parent_end, 1u);
}

// The rights term columns are optional: absent, no right expires and the panel says so;
// present, the expiry maps to the first bar after the expiry day (T beyond the panel).
TEST(CorporateActionsLoader, RightsTermsAreOptionalColumns) {
    DataFiles files;
    auto config = four_bar_panel(files);   // bars 1970-01-01 .. 01-04
    Row rights{"A", "1970-01-02", "warrant", "E", kNaN, 0.1, kNaN, 10, 10};
    rights.leg = "rights";
    write_ledger(files, {rights});
    auto panel = DataPanel::load(config);
    ASSERT_EQ(panel.corporate_actions.size(), 1u);
    EXPECT_FALSE(panel.corporate_action_rights_terms_loaded);
    EXPECT_EQ(panel.corporate_actions[0].expiry_bar, -1);
    EXPECT_TRUE(panel.corporate_actions[0].rights);
    EXPECT_EQ(panel.corporate_action_rights_without_expiry, 1u);   // no column, so no expiry either
    EXPECT_EQ(panel.corporate_action_legs_rights, 1u);

    // With the three columns: expiry inside the panel, beyond it, and null.
    const auto with_terms = [&](const std::vector<int64_t>& expiry, bool null_expiry = false) {
        std::vector<std::shared_ptr<arrow::Array>> arrays;
        std::vector<std::string> names = kLedgerColumns;
        // Three rows: A->E rights expiring day 2, B->E rights expiring day 40, C spin-off (no terms).
        arrays = {text({"A", "B", "C"}), dates({kDay, kDay, kDay}), text({"rights", "rights", "spin_off"}),
                  text({"rights", "rights", "stock"}), numeric({kNaN, kNaN, kNaN}), text({"E", "E", "D"}),
                  numeric({0.1, 0.1, 0.5}), numeric({kNaN, kNaN, 40}), numeric({10, 20, 30}),
                  numeric({10, 20, 30}), numeric({1, 1, 1}), flags({false, false, false}),
                  text({"ok", "ok", "ok"}), text({"unknown", "unknown", "child_close_ex"})};
        arrow::TimestampBuilder expiries(arrow::timestamp(arrow::TimeUnit::NANO), arrow::default_memory_pool());
        for (size_t i = 0; i < 3; ++i) {
            if (i == 1 && null_expiry) { if (!expiries.AppendNull().ok()) throw std::runtime_error("append"); }
            else if (i == 2) { if (!expiries.AppendNull().ok()) throw std::runtime_error("append"); }
            else if (!expiries.Append(expiry[i] * 1000000).ok()) throw std::runtime_error("append");
        }
        names.push_back("expiryDate"); arrays.push_back(expiries.Finish().ValueOrDie());
        names.push_back("subscriptionPriceCache"); arrays.push_back(numeric({32.0, 5.0, kNaN}));
        names.push_back("exerciseRatioCache"); arrays.push_back(numeric({1.0, 2.0, kNaN}));
        files.write("ledger.parquet", names, arrays);
        return DataPanel::load(config);
    };
    panel = with_terms({2 * kDay, 40 * kDay, 0});
    EXPECT_TRUE(panel.corporate_action_rights_terms_loaded);
    ASSERT_EQ(panel.corporate_actions.size(), 3u);
    EXPECT_EQ(panel.corporate_actions[0].expiry_bar, 3);      // day 2 expires: first bar after = bar 3
    EXPECT_EQ(panel.corporate_actions[1].expiry_bar, 4);      // beyond the panel = T
    EXPECT_EQ(panel.corporate_actions[2].expiry_bar, -1);     // not a right
    EXPECT_DOUBLE_EQ(panel.corporate_actions[0].subscription_price, 32.0);
    EXPECT_DOUBLE_EQ(panel.corporate_actions[1].exercise_ratio, 2.0);
    EXPECT_EQ(panel.corporate_action_rights_without_expiry, 0u);
    EXPECT_EQ(panel.corporate_action_legs_rights, 2u);
    EXPECT_EQ(panel.corporate_action_unpriced_children, 0u);  // D and E are panel symbols
    panel = with_terms({2 * kDay, 40 * kDay, 0}, true);
    EXPECT_EQ(panel.corporate_actions[1].expiry_bar, -1);
    EXPECT_EQ(panel.corporate_action_rights_without_expiry, 1u);
    // An expiry before the ex-date is a contract error.
    EXPECT_THROW(with_terms({0, 40 * kDay, 0}), std::runtime_error);
}

// Marker rows of the export (docs section 9): a cash row without an amount and a
// stock row without a child, status unresolved, values NaN, with the extra
// `markerReason` column the engine does not read. They load, count as unbookable,
// pass the staleness guard on the parent anchors, and are listed on held parents.
TEST(CorporateActionsLoader, MarkerRowsLoadAsUnbookableLegsAndAreListedOnHeldParents) {
    DataFiles files;
    auto config = four_bar_panel(files);
    std::vector<std::string> names = kLedgerColumns;
    names.push_back("markerReason");
    // A: no_cash_amount marker; B: no_child marker (vendor value present in the raw ledger,
    // exported as NaN); C: a vendor-implied cash amount flagged as an estimate; D: an
    // ordinary dividend.
    std::vector<std::shared_ptr<arrow::Array>> arrays = {
        text({"A", "B", "C", "D"}), dates({2 * kDay, 2 * kDay, 2 * kDay, 2 * kDay}),
        text({"special_dividend", "spin_off", "cash_dividend", "cash_dividend"}),
        text({"cash", "stock", "cash", "cash"}), numeric({kNaN, kNaN, 0.3, 0.4}), text({"", "", "", ""}),
        numeric({kNaN, kNaN, kNaN, kNaN}), numeric({kNaN, kNaN, kNaN, kNaN}),
        numeric({10, 20, 30, 40}), numeric({10, 20, 30, 40}), numeric({1, 1, 1, 1}),
        flags({false, false, true, false}), text({"unresolved", "unresolved", "estimated", "ok"}),
        text({"unknown", "vendor_implied", "vendor_implied", "unknown"}),
        text({"no_cash_amount", "no_child", "", ""})};
    files.write("ledger.parquet", names, arrays);
    const auto panel = DataPanel::load(config);
    EXPECT_EQ(panel.corporate_action_rows_matched, 4u);
    EXPECT_EQ(panel.corporate_action_rows_unresolved, 2u);
    EXPECT_EQ(panel.corporate_action_legs, 4u);
    EXPECT_EQ(panel.corporate_action_legs_unbookable, 2u);
    EXPECT_EQ(panel.corporate_action_legs_estimated, 1u);     // the flagged cash amount
    EXPECT_EQ(panel.corporate_action_unpriced_children, 0u);  // no child named, nothing to resolve
    EXPECT_EQ(panel.corporate_action_price_checks, 4u);       // parent anchors of the markers verified
    ASSERT_EQ(panel.corporate_actions.size(), 4u);
    EXPECT_TRUE(panel.corporate_actions[0].unresolved);
    EXPECT_FALSE(panel.corporate_actions[0].estimate);
    EXPECT_TRUE(std::isnan(panel.corporate_actions[0].cash));
    EXPECT_TRUE(panel.corporate_actions[1].unresolved);
    EXPECT_FALSE(panel.corporate_actions[1].child_named);
    EXPECT_EQ(panel.corporate_actions[1].child, -1);
    EXPECT_FALSE(panel.corporate_actions[1].estimate);         // nothing to estimate without a child
    EXPECT_FALSE(panel.corporate_actions[2].unresolved);
    EXPECT_TRUE(panel.corporate_actions[2].estimate);
    EXPECT_DOUBLE_EQ(panel.corporate_actions[2].cash, 0.3);
    EXPECT_FALSE(panel.corporate_actions[3].estimate);
    EXPECT_FALSE(panel.corporate_actions[3].unresolved);
    for (int mode : {0, 1}) {
        AccountingLedger ledger;
        std::vector<std::vector<float>> plan(panel.T, {1, 1, 1, 1, 0});
        plan[0] = {0, 0, 0, 0, 0};
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));
        ASSERT_EQ(out.unconfirmed.size(), 3u) << "mode=" << mode;
        EXPECT_EQ(out.unconfirmed[0].asset, 0u);
        EXPECT_EQ(out.unconfirmed[0].kind, UnconfirmedKind::UnresolvedLeg);
        EXPECT_EQ(out.unconfirmed[0].detail, "no_cash_amount");
        EXPECT_EQ(out.unconfirmed[1].asset, 2u);
        EXPECT_EQ(out.unconfirmed[1].kind, UnconfirmedKind::EstimateValue);
        EXPECT_EQ(out.unconfirmed[1].detail, "cash:value");   // vendor_implied amount, no reason column
        EXPECT_EQ(out.unconfirmed[2].asset, 1u);
        EXPECT_EQ(out.unconfirmed[2].kind, UnconfirmedKind::UnresolvedLeg);
        EXPECT_EQ(out.unconfirmed[2].detail, "no_child");
        for (const auto& record : out.unconfirmed) {
            EXPECT_EQ(record.bar, 2u);
            EXPECT_NEAR(record.weight_before, 0.25, 1e-12);
        }
        EXPECT_EQ(out.corporate_actions[2].unconfirmed_count, 3u);
        EXPECT_NEAR(out.corporate_actions[2].unconfirmed_abs_weight, 0.75, 1e-12);
        EXPECT_EQ(out.corporate_actions[2].legs_unresolved, 2u);
        EXPECT_EQ(out.corporate_actions[2].dividend_count, 2u);   // C (estimate) and D are booked
        EXPECT_EQ(out.corporate_actions[2].dividend_estimated, 1u);
        EXPECT_NEAR(out.corporate_actions[2].dividend_pnl, 0.25 * 0.3 / 30.0 + 0.25 * 0.4 / 40.0, 1e-12);
        EXPECT_EQ(out.corporate_actions[2].distribution_unpriced, 1u);
        // Strict refuses the same run on that bar, naming the markers.
        AccountingLedger strict_ledger;
        auto strict = ledger_config(panel, mode, strict_ledger);
        strict.unresolved_policy = UnresolvedPolicy::Strict;
        try {
            Simulator(panel, strict).run_from_signals(plan_signals(plan, mode));
            ADD_FAILURE() << "strict must fail on the marker bar";
        } catch (const std::runtime_error& error) {
            EXPECT_NE(std::string(error.what()).find("A unresolved_leg(no_cash_amount)"), std::string::npos) << error.what();
            EXPECT_NE(std::string(error.what()).find("B unresolved_leg(no_child)"), std::string::npos) << error.what();
        }
    }
    // A marker whose parent anchor disagrees with the cache is refused like any row.
    arrays[8] = numeric({11, 20, 30, 40});
    files.write("ledger.parquet", names, arrays);
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);
}

// (Y1-B) The optional childValuePerShareCache column: a stock row without a child (or with
// one the panel does not carry) that states a usable value is a value-only leg, not a
// marker; the anchor the value is divided by is required; a cash row may not carry it;
// without the column the row is a marker as before.
TEST(CorporateActionsLoader, ChildValuesPriceLegsWhoseChildIsOutsideThePanel) {
    DataFiles files;
    auto config = four_bar_panel(files);
    std::vector<std::string> names = kLedgerColumns;
    names.push_back("childValuePerShareCache");
    // A: no child id, vendor value 0.6 -> value-only; B: child Z outside the panel, event value 1.5
    // -> value-only; C: no child, no value -> marker; D: ordinary spin into the panel symbol E.
    const auto arrays = [&](std::vector<double> parent_before) {
        return std::vector<std::shared_ptr<arrow::Array>>{
            text({"A", "B", "C", "D"}), dates({2 * kDay, 2 * kDay, 2 * kDay, 2 * kDay}),
            text({"spin_off", "spin_off", "spin_off", "spin_off"}), text({"stock", "stock", "stock", "stock"}),
            numeric({kNaN, kNaN, kNaN, kNaN}), text({"", "Z", "", "E"}),
            numeric({kNaN, kNaN, kNaN, 0.25}), numeric({kNaN, kNaN, kNaN, kNaN}),
            numeric({10, 20, 30, 40}), numeric(parent_before), numeric({1, 1, 1, 1}),
            flags({true, true, false, false}), text({"estimated", "estimated", "unresolved", "ok"}),
            text({"vendor_implied", "event_value", "unknown", "unknown"}),
            numeric({0.6, 1.5, kNaN, kNaN})};
    };
    files.write("ledger.parquet", names, arrays({10, 20, 30, 40}));
    const auto panel = DataPanel::load(config);
    ASSERT_EQ(panel.corporate_actions.size(), 4u);
    EXPECT_EQ(panel.corporate_action_legs_value_only, 2u);
    EXPECT_EQ(panel.corporate_action_legs_unbookable, 1u);
    EXPECT_EQ(panel.corporate_action_legs_estimated, 2u);
    EXPECT_EQ(panel.corporate_action_unpriced_children, 1u);   // Z
    EXPECT_EQ(panel.corporate_action_price_checks, 4u);        // the value legs' anchors are checked
    const auto& a = panel.corporate_actions[0];
    EXPECT_FALSE(a.child_named);
    EXPECT_FALSE(a.unresolved);
    EXPECT_TRUE(a.estimate);
    EXPECT_TRUE(a.ledger_value_usable);
    EXPECT_DOUBLE_EQ(a.child_value, 0.6);
    EXPECT_EQ(a.child, -1);
    const auto& b = panel.corporate_actions[1];
    EXPECT_TRUE(b.child_named);
    EXPECT_EQ(b.child, -1);
    EXPECT_DOUBLE_EQ(b.child_value, 1.5);
    const auto& c = panel.corporate_actions[2];
    EXPECT_TRUE(c.unresolved);
    EXPECT_TRUE(std::isnan(c.child_value));
    for (int mode : {0, 1}) {
        AccountingLedger ledger;
        std::vector<std::vector<float>> plan(panel.T, {1, 1, 1, 1, 0});
        plan[0] = {0, 0, 0, 0, 0};
        auto out = Simulator(panel, ledger_config(panel, mode, ledger))
                       .run_from_signals(plan_signals(plan, mode));
        EXPECT_EQ(out.corporate_actions[2].distribution_value_only, 2u) << "mode=" << mode;
        EXPECT_EQ(out.corporate_actions[2].distribution_estimated, 2u);
        EXPECT_EQ(out.corporate_actions[2].distribution_pending, 1u);      // D -> E waits: E has no close on day 2
        EXPECT_EQ(out.corporate_actions[2].child_positions_created, 0u);
        EXPECT_EQ(out.corporate_actions[3].child_positions_created, 1u);   // E's first quote on day 3
        EXPECT_EQ(out.corporate_actions[2].legs_unresolved, 1u);
        EXPECT_NEAR(out.corporate_actions[2].distribution_pnl, 0.25 * 0.6 / 10.0 + 0.25 * 1.5 / 20.0, 1e-12);
        ASSERT_EQ(out.unconfirmed.size(), 3u);
        EXPECT_EQ(out.unconfirmed[0].detail, "ledger_value_cash:vendor_implied");
        EXPECT_EQ(out.unconfirmed[1].detail, "ledger_value_cash:event_value");
        EXPECT_EQ(out.unconfirmed[2].detail, "no_child");
    }
    // The value is divided by the mark: the previous-close anchor is required like a cash amount.
    files.write("ledger.parquet", names, arrays({kNaN, 20, 30, 40}));
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);
    // A cash row may not carry a child value.
    std::vector<std::shared_ptr<arrow::Array>> mixed = arrays({10, 20, 30, 40});
    mixed[3] = text({"cash", "stock", "stock", "stock"});
    mixed[2] = text({"cash_dividend", "spin_off", "spin_off", "spin_off"});
    mixed[4] = numeric({1.0, kNaN, kNaN, kNaN});
    files.write("ledger.parquet", names, mixed);
    EXPECT_THROW(DataPanel::load(config), std::runtime_error);
    // Without the column a no-child row is a marker and an outside child is unpriced (pre-Y1-B).
    std::vector<std::shared_ptr<arrow::Array>> without = arrays({10, 20, 30, 40});
    without.pop_back();
    files.write("ledger.parquet", kLedgerColumns, without);
    const auto bare = DataPanel::load(config);
    EXPECT_EQ(bare.corporate_action_legs_value_only, 0u);
    EXPECT_EQ(bare.corporate_action_legs_unbookable, 2u);
    EXPECT_TRUE(std::isnan(bare.corporate_actions[1].child_value));
}

// (user review 09-21) The optional estimateReason column names what a flagged row estimated;
// it reaches the unconfirmed detail of quote-priced legs (child_close:<reason>) and cash legs
// (cash:<reason>); without the column the valued tiers say `value` and other flags `unspecified`.
TEST(CorporateActionsLoader, EstimateReasonsAreReadIntoTheUnconfirmedDetail) {
    DataFiles files;
    auto config = four_bar_panel(files);
    std::vector<std::string> names = kLedgerColumns;
    names.push_back("estimateReason");
    // A -> B quoted child, flagged, reason ratio; C cash flagged, reason date; D -> B quoted child, not flagged.
    const auto arrays = [&](bool with_reason) {
        std::vector<std::shared_ptr<arrow::Array>> out = {
            text({"A", "C", "D"}), dates({2 * kDay, 2 * kDay, 2 * kDay}),
            text({"spin_off", "cash_dividend", "spin_off"}), text({"stock", "cash", "stock"}),
            numeric({kNaN, 0.5, kNaN}), text({"B", "", "B"}), numeric({0.1, kNaN, 0.2}),
            numeric({20, kNaN, 20}), numeric({10, 30, 40}), numeric({10, 30, 40}), numeric({1, 1, 1}),
            flags({true, true, false}), text({"estimated", "estimated", "ok"}),
            text({"child_close_ex", "unknown", "child_close_ex"})};
        if (with_reason) out.push_back(text({"ratio", "date", ""}));
        return out;
    };
    files.write("ledger.parquet", names, arrays(true));
    const auto panel = DataPanel::load(config);
    ASSERT_EQ(panel.corporate_actions.size(), 3u);
    EXPECT_TRUE(panel.corporate_actions[0].estimate);
    EXPECT_EQ(panel.corporate_actions[0].estimate_reason, "ratio");
    EXPECT_EQ(panel.corporate_actions[1].estimate_reason, "date");
    EXPECT_FALSE(panel.corporate_actions[2].estimate);
    EXPECT_TRUE(panel.corporate_actions[2].estimate_reason.empty());
    EXPECT_EQ(panel.corporate_action_legs_estimated, 2u);
    for (int mode : {0, 1}) {
        AccountingLedger ledger;
        std::vector<std::vector<float>> plan(panel.T, {1, 0, 1, 1, 0});
        plan[0] = {0, 0, 0, 0, 0};
        auto out = Simulator(panel, ledger_config(panel, mode, ledger)).run_from_signals(plan_signals(plan, mode));
        ASSERT_EQ(out.unconfirmed.size(), 2u) << "mode=" << mode;
        EXPECT_EQ(out.unconfirmed[0].asset, 2u);
        EXPECT_EQ(out.unconfirmed[0].detail, "cash:date");
        EXPECT_EQ(out.unconfirmed[1].asset, 0u);
        EXPECT_EQ(out.unconfirmed[1].detail, "child_close:ratio");
        EXPECT_EQ(out.corporate_actions[2].distribution_estimated, 1u);
        EXPECT_EQ(out.corporate_actions[2].child_positions_created, 2u);   // both spins create B weight
        EXPECT_EQ(out.corporate_actions[2].dividend_estimated, 1u);
        // The accounting is the quote's: A pays 1/3 x 0.1 x 20/10, D 1/3 x 0.2 x 20/40, C 1/3 x 0.5/30.
        EXPECT_NEAR(out.corporate_actions[2].distribution_pnl, (1.0 / 3.0) * 0.1 * 20.0 / 10.0 + (1.0 / 3.0) * 0.2 * 20.0 / 40.0, 1e-12);
        EXPECT_NEAR(out.corporate_actions[2].dividend_pnl, (1.0 / 3.0) * 0.5 / 30.0, 1e-12);
    }
    // Without the column the flags still count, with the fallback reasons.
    files.write("ledger.parquet", kLedgerColumns, arrays(false));
    const auto bare = DataPanel::load(config);
    EXPECT_TRUE(bare.corporate_actions[0].estimate_reason.empty());
    AccountingLedger ledger;
    std::vector<std::vector<float>> plan(bare.T, {1, 0, 1, 1, 0});
    plan[0] = {0, 0, 0, 0, 0};
    auto out = Simulator(bare, ledger_config(bare, 0, ledger)).run_from_signals(plan_signals(plan, 0));
    ASSERT_EQ(out.unconfirmed.size(), 2u);
    EXPECT_EQ(out.unconfirmed[0].detail, "cash:unspecified");
    EXPECT_EQ(out.unconfirmed[1].detail, "child_close:unspecified");
}

