#include <gtest/gtest.h>
#include "xml_compiler.h"
#include "simulation_data.h"
#include "simulator_engine.h"
#include "modules/Data.h"
#include <cmath>
#include <limits>
#include <stdexcept>
#include <string>
#include <functional>
#include <vector>

namespace {
constexpr double kTol = 1e-12;
constexpr double kFee = 0.001;
constexpr float kMissing = std::numeric_limits<float>::quiet_NaN();

struct Row {
    std::vector<double> previous, desired, weights, trades;
    double gross, execution, fee, net;
};

struct Book {
    DataPanel panel;
    std::vector<std::vector<float>> signals;
    std::vector<Row> rows;

    Book(unsigned bars, unsigned assets, bool thin = false) {
        panel.T = bars;
        panel.N = assets;
        panel.thin_layout = thin;
        for (unsigned t = 0; t < bars; ++t)
            panel.timestamps.push_back(static_cast<int64_t>(t) * 86400000);
        for (unsigned i = 0; i < assets; ++i)
            panel.symbols.push_back("S" + std::to_string(i));
        for (const auto* field : {"open", "high", "low", "close", "vwap"})
            panel.arrays[field].assign(static_cast<size_t>(bars) * assets, 100.0f);
        if (thin) panel.returns_total.assign(static_cast<size_t>(bars) * assets, 0.0);
        signals.assign(bars, std::vector<float>(assets, 0.0f));
    }

    void quote(unsigned t, unsigned i, float open, float high, float low, float close) {
        panel.at("open", t, i) = open;
        panel.at("high", t, i) = high;
        panel.at("low", t, i) = low;
        panel.at("close", t, i) = close;
    }

    void vquote(unsigned t, unsigned i, float vwap, float high, float low, float close) {
        panel.at("vwap", t, i) = vwap;
        panel.at("high", t, i) = high;
        panel.at("low", t, i) = low;
        panel.at("close", t, i) = close;
    }

    SimulatorConfig vconfig() const {
        SimulatorConfig cfg{panel.T, panel.N, kFee};
        cfg.execution_price = ExecutionPrice::Vwap;
        return cfg;
    }

    SimulatorConfig config() const {
        SimulatorConfig cfg{panel.T, panel.N, kFee};
        cfg.execution_price = ExecutionPrice::Ohlc4;
        return cfg;
    }

    SimulatorOutput run(SimulatorConfig cfg, bool pipeline = false) {
        rows.clear();
        cfg.accounting_row_callback = [this](const AccountingRowView& row) {
            EXPECT_EQ(row.bar, rows.size());
            rows.push_back({row.previous_weights, row.desired_weights, row.weights,
                            row.market_trades, row.gross_pnl, row.execution_pnl,
                            row.fee, row.net_pnl});
        };
        if (!pipeline) return Simulator(panel, cfg).run_from_signals(signals);
        auto& raw = panel.arrays["test_signal"];
        raw.clear();
        for (const auto& signal : signals) raw.insert(raw.end(), signal.begin(), signal.end());
        MData data;
        data.column_name = "test_signal";
        data.panel = &panel;
        return Simulator(panel, cfg).run({&data});
    }

    void expect_bar(const SimulatorOutput& out, unsigned t, double holding,
                    double execution, double turnover) const {
        ASSERT_LT(t, out.execution_ret.size());
        ASSERT_LT(t, rows.size());
        EXPECT_TRUE(std::isfinite(out.ret[t]));
        EXPECT_NEAR(out.execution_ret[t], execution, kTol);
        EXPECT_NEAR(out.gross_ret[t], holding + execution, kTol);
        EXPECT_NEAR(out.turnover[t], turnover, kTol);
        EXPECT_NEAR(out.ret[t], holding + execution - turnover * kFee, kTol);
        EXPECT_DOUBLE_EQ(rows[t].execution, out.execution_ret[t]);
        EXPECT_DOUBLE_EQ(rows[t].gross, out.gross_ret[t]);
        EXPECT_DOUBLE_EQ(rows[t].net, out.ret[t]);
        EXPECT_NEAR(rows[t].fee, turnover * kFee, kTol);
    }
};
} // namespace

TEST(ExecutionPrice, InitialBuyUnchangedHoldingFlipAndExitUseSignedActualTrades) {
    for (bool thin : {false, true}) {
        for (bool pipeline : {false, true}) {
            SCOPED_TRACE(::testing::Message() << "thin=" << thin << " pipeline=" << pipeline);
            Book b(4, 1, thin);
            b.quote(0, 0, 90, 120, 80, 110);    // OHLC4 = 100
            b.quote(1, 0, 99, 132, 88, 121);    // OHLC4 = 110
            b.quote(2, 0, 81, 108, 72, 99);     // OHLC4 = 90
            b.quote(3, 0, 88, 120, 84, 108);    // OHLC4 = 100
            b.signals = {{1}, {1}, {-1}, {0}};
            if (thin) b.panel.returns_total = {0, 0.1, 99.0 / 121.0 - 1.0, 108.0 / 99.0 - 1.0};
            auto out = b.run(b.config(), pipeline);
            b.expect_bar(out, 0, 0.0, 0.1, 1.0);
            b.expect_bar(out, 1, 0.1, 0.0, 0.0); // Intraday spread alone creates no leg.
            b.expect_bar(out, 2, 99.0 / 121.0 - 1.0, -2.0 * 0.1, 2.0);
            b.expect_bar(out, 3, -(108.0 / 99.0 - 1.0), 0.08, 1.0);
            ASSERT_EQ(b.rows.size(), 4u);
            EXPECT_DOUBLE_EQ(b.rows[0].trades[0], 1.0);
            EXPECT_DOUBLE_EQ(b.rows[1].trades[0], 0.0);
            EXPECT_DOUBLE_EQ(b.rows[2].trades[0], -2.0);
            EXPECT_DOUBLE_EQ(b.rows[3].trades[0], 1.0);
            // The trading contribution must also be computed without an accounting observer.
            auto unobserved = Simulator(b.panel, b.config()).run_from_signals(b.signals);
            EXPECT_EQ(unobserved.ret, out.ret);
            EXPECT_EQ(unobserved.execution_ret, out.execution_ret);
        }
    }
}

TEST(ExecutionPrice, CurrentNormalizedTargetEarnsExecutionPnlOnTheSameBar) {
    for (bool pipeline : {false, true}) {
        Book b(3, 2);
        for (unsigned i = 0; i < 2; ++i) {
            b.quote(1, i, 90, 120, 80, 110);
            b.quote(2, i, 99, 132, 88, 121);
        }
        b.panel.universe_mask = {1, 1, 0, 1, 1, 1};
        b.signals = {{3, -1}, {1, 3}, {0, 0}};
        auto out = b.run(b.config(), pipeline);
        b.expect_bar(out, 0, 0.0, 0.0, 1.0);
        // Each row is masked and normalized on its execution date.
        b.expect_bar(out, 1, 0.05, (-0.75 + 1.25) * 0.1, 2.0);
        b.expect_bar(out, 2, 0.1, -0.1, 1.0);
        EXPECT_EQ(b.rows[0].desired, (std::vector<double>{0.75, -0.25}));
        EXPECT_EQ(b.rows[1].desired, (std::vector<double>{0.0, 1.0}));
        EXPECT_EQ(b.rows[2].desired, (std::vector<double>{0.0, 0.0}));
    }
}

TEST(ExecutionPrice, XmlDelayIsAppliedOnceWithoutAdditionalExecutionDelay) {
    {
        Book b(4, 1);
        for (unsigned t = 0; t < 4; ++t) b.quote(t, 0, 90, 120, 80, 110);
        b.panel.arrays["volume"] = {1, -1, 0, 0};
        auto alphas = compile_xml(R"(<data expr="volume" delay="1"/>)", &b.panel);
        ASSERT_EQ(alphas.size(), 1u);
        auto cfg = b.config();
        cfg.collect_signals = true;
        auto out = Simulator(b.panel, cfg).run(alphas[0].pipeline);
        ASSERT_EQ(out.signals.size(), 4u);
        EXPECT_TRUE(std::isnan(out.signals[0][0]));
        EXPECT_FLOAT_EQ(out.signals[1][0], 1.0f);
        EXPECT_FLOAT_EQ(out.signals[2][0], -1.0f);
        EXPECT_FLOAT_EQ(out.signals[3][0], 0.0f);
        const std::vector<double> trades = {0, 1, -2, 1};
        ASSERT_EQ(out.execution_ret.size(), trades.size());
        for (unsigned t = 0; t < trades.size(); ++t) {
            EXPECT_NEAR(out.execution_ret[t], trades[t] * 0.1, kTol);
            EXPECT_NEAR(out.ret[t], trades[t] * 0.1 - std::fabs(trades[t]) * kFee, kTol);
        }
        auto replay = Simulator(b.panel, cfg).run_from_signals(out.signals);
        EXPECT_EQ(replay.ret, out.ret);
        EXPECT_EQ(replay.execution_ret, out.execution_ret);
    }
}

TEST(ExecutionPrice, DefaultCloseMatchesLegacyHandCalculationWithoutOhlColumns) {
    Book b(3, 1);
    b.panel.arrays["close"] = {100, 110, 99};
    for (const auto* field : {"open", "high", "low"}) b.panel.arrays.erase(field);
    b.signals = {{1}, {-1}, {0}};
    SimulatorConfig cfg{3, 1, kFee};
    EXPECT_EQ(cfg.execution_price, ExecutionPrice::Close);
    auto implicit = b.run(cfg);
    b.expect_bar(implicit, 0, 0.0, 0.0, 1.0);
    b.expect_bar(implicit, 1, 0.1, 0.0, 2.0);
    b.expect_bar(implicit, 2, 0.1, 0.0, 1.0);
    cfg.execution_price = ExecutionPrice::Close;
    auto explicit_close = b.run(cfg);
    EXPECT_EQ(implicit.ret, explicit_close.ret);
    EXPECT_EQ(implicit.gross_ret, explicit_close.gross_ret);
    EXPECT_EQ(implicit.execution_ret, explicit_close.execution_ret);
}

TEST(ExecutionPrice, FlatIntradayPricesGiveExactCloseParity) {
    {
        Book b(4, 2);
        for (const auto* field : {"open", "high", "low", "close"})
            b.panel.arrays[field] = {100, 100, 110, 90, 121, 81, 99, 100};
        b.signals = {{3, -1}, {-1, 1}, {0, 0}, {1, 0}};
        auto cfg = b.config();
        auto ohlc = b.run(cfg);
        cfg.execution_price = ExecutionPrice::Close;
        auto close = b.run(cfg);
        EXPECT_EQ(ohlc.ret, close.ret);
        EXPECT_EQ(ohlc.gross_ret, close.gross_ret);
        EXPECT_EQ(ohlc.turnover, close.turnover);
        EXPECT_EQ(ohlc.long_weight, close.long_weight);
        EXPECT_EQ(ohlc.short_weight, close.short_weight);
        EXPECT_EQ(ohlc.execution_ret, (std::vector<double>(4, 0.0)));
    }
}

TEST(ExecutionPrice, MissingOhlCarryReservesFrozenBudgetButStillValuesObservedClose) {
    for (bool thin : {false, true}) {
        for (const auto* field : {"open", "high", "low"}) {
            SCOPED_TRACE(::testing::Message() << "thin=" << thin << " missing=" << field);
            Book b(3, 3, thin);
            b.signals = {{1, 1, 0}, {0, 0, 1}, {0, 0, 1}};
            b.quote(1, 0, 90, 120, 80, 110);
            b.panel.at(field, 1, 0) = kMissing;
            b.quote(1, 2, 90, 120, 80, 110);
            b.quote(2, 0, 99, 132, 88, 121);
            b.quote(2, 2, 99, 132, 88, 121);
            // Thin total return deliberately differs from the raw close return.
            if (thin) b.panel.returns_total = {0, 0, 0, 0.125, 0, 0.9, 0.1, 0, 0.1};
            auto out = b.run(b.config());
            b.expect_bar(out, 1, 0.5 * (thin ? 0.125 : 0.1), 0.5 * 0.1, 1.0);
            EXPECT_EQ(b.rows[1].weights, (std::vector<double>{0.5, 0.0, 0.5}));
            EXPECT_EQ(b.rows[1].trades, (std::vector<double>{0.0, -0.5, 0.5}));
            EXPECT_NEAR(out.missing_data[1].frozen_gross, 0.5, kTol);
            EXPECT_EQ(out.missing_data[1].blocked_order_count, 1u);
            EXPECT_NEAR(out.missing_data[1].blocked_trade_weight, 0.5, kTol);
            // The observed close updated the mark: next holding return is 110 -> 121.
            b.expect_bar(out, 2, 0.5 * 0.1 + 0.5 * 0.1, 0.0, 1.0);
            EXPECT_EQ(b.rows[2].trades, (std::vector<double>{-0.5, 0.0, 0.5}));
            EXPECT_DOUBLE_EQ(out.missing_data[2].frozen_gross, 0.0);
        }
    }
}

TEST(ExecutionPrice, MissingOhlEntryIsBlockedAndErrorRejectsRequiredEntryOrExit) {
    for (const auto* field : {"open", "high", "low"}) {
        Book entry(1, 1);
        entry.signals = {{1}};
        entry.panel.at(field, 0, 0) = kMissing;
        auto out = entry.run(entry.config());
        entry.expect_bar(out, 0, 0.0, 0.0, 0.0);
        EXPECT_DOUBLE_EQ(entry.rows[0].weights[0], 0.0);
        EXPECT_EQ(out.missing_data[0].missing_entry_count, 1u);
        EXPECT_EQ(out.missing_data[0].blocked_order_count, 1u);
        auto cfg = entry.config();
        cfg.missing_close_policy = MissingClosePolicy::Error;
        EXPECT_THROW(entry.run(cfg), std::runtime_error);

        Book exit(2, 1);
        exit.signals = {{1}, {0}};
        exit.panel.at(field, 1, 0) = kMissing;
        cfg = exit.config();
        cfg.missing_close_policy = MissingClosePolicy::Error;
        EXPECT_THROW(exit.run(cfg), std::runtime_error);
    }
}

TEST(ExecutionPrice, MissingUnheldZeroTargetCellsDoNotPoisonEitherPolicy) {
    for (auto policy : {MissingClosePolicy::Carry, MissingClosePolicy::Error}) {
        Book b(2, 2);
        b.signals = {{1, 0}, {1, 0}};
        b.quote(0, 0, 90, 120, 80, 110);
        b.quote(1, 0, 99, 132, 88, 121);
        // Include both an observed close with missing OHL and an entirely absent quote.
        b.quote(0, 1, kMissing, kMissing, kMissing, 100);
        b.quote(1, 1, kMissing, kMissing, kMissing, kMissing);
        auto cfg = b.config();
        cfg.missing_close_policy = policy;
        auto out = b.run(cfg);
        b.expect_bar(out, 0, 0.0, 0.1, 1.0);
        b.expect_bar(out, 1, 0.1, 0.0, 0.0);
        EXPECT_EQ(out.missing_data[0].blocked_order_count, 0u);
        EXPECT_EQ(out.missing_data[1].blocked_order_count, 0u);
    }
}

TEST(ExecutionPrice, MissingOhlColumnsAreRejectedAtConstruction) {
    for (const auto* field : {"open", "high", "low"}) {
        SCOPED_TRACE(field);
        Book b(1, 1);
        b.panel.arrays.erase(field);
        EXPECT_THROW((Simulator{b.panel, b.config()}), std::invalid_argument);
    }
}

TEST(ExecutionPrice, ErrorAllowsMissingOhlWhenObservedHoldingRequiresNoTrade) {
    Book b(2, 1);
    b.signals = {{1}, {1}};
    b.quote(1, 0, kMissing, 120, 80, 110);
    auto cfg = b.config();
    cfg.missing_close_policy = MissingClosePolicy::Error;
    auto out = b.run(cfg);
    b.expect_bar(out, 1, 0.1, 0.0, 0.0);
    EXPECT_DOUBLE_EQ(b.rows[1].weights[0], 1.0);
    EXPECT_EQ(out.missing_data[1].blocked_order_count, 0u);
}

TEST(ExecutionPrice, CorruptOhlIsRelevantForUnchangedHoldingsButNotUnheldZeroTargets) {
    for (auto policy : {MissingClosePolicy::Carry, MissingClosePolicy::Error}) {
        Book b(2, 2);
        b.signals = {{1, 0}, {1, 0}};
        b.panel.at("open", 0, 1) = std::numeric_limits<float>::infinity();
        b.panel.at("high", 1, 1) = 50; // Inconsistent, but no holding or target in S1.
        auto cfg = b.config();
        cfg.missing_close_policy = policy;
        auto out = b.run(cfg);
        b.expect_bar(out, 1, 0.0, 0.0, 0.0);
        b.panel.at("open", 1, 0) = std::numeric_limits<float>::infinity();
        EXPECT_THROW(b.run(cfg), std::runtime_error);
    }
}

TEST(ExecutionPrice, CorruptRequiredOhlQuotesAreRejectedUnderBothPolicies) {
    for (auto policy : {MissingClosePolicy::Carry, MissingClosePolicy::Error}) {
        for (const auto* field : {"open", "high", "low"}) {
            for (float bad : {0.0f, -1.0f, std::numeric_limits<float>::infinity(),
                              -std::numeric_limits<float>::infinity()}) {
                SCOPED_TRACE(::testing::Message() << field << "=" << bad);
                Book b(1, 1);
                b.signals = {{1}};
                b.panel.at(field, 0, 0) = bad;
                auto cfg = b.config();
                cfg.missing_close_policy = policy;
                EXPECT_THROW(b.run(cfg), std::runtime_error);
            }
        }
    }
}

TEST(ExecutionPrice, InconsistentOhlBlocksTheFillInsteadOfInventingAnExecutionPrice) {
    // Positive source values can disagree (for example, a vendor open below low).
    // Carry waits for a usable bar; error rejects a required fill.
    for (const auto& ohlc : std::vector<std::vector<float>>{
                 {130, 120, 80, 110}, {70, 120, 80, 110},
                 {100, 105, 80, 110}, {100, 120, 115, 110}, {100, 80, 120, 100}}) {
            Book b(1, 1);
            b.signals = {{1}};
            b.quote(0, 0, ohlc[0], ohlc[1], ohlc[2], ohlc[3]);
            auto cfg = b.config();
            auto out = b.run(cfg);
            b.expect_bar(out, 0, 0.0, 0.0, 0.0);
            EXPECT_DOUBLE_EQ(b.rows[0].weights[0], 0.0);
            EXPECT_EQ(out.missing_data[0].blocked_order_count, 1u);
            cfg.missing_close_policy = MissingClosePolicy::Error;
            EXPECT_THROW(b.run(cfg), std::runtime_error);
    }
}

TEST(ExecutionPrice, OhlcAverageUsesDoublePrecisionForFiniteLargePrices) {
    Book b(1, 1);
    const float unit = std::ldexp(1.0f, 126);
    // Each cell is finite; summing them in float would overflow.
    b.quote(0, 0, unit, 2.0f * unit, 0.5f * unit, 2.0f * unit);
    b.signals = {{1}};
    auto out = b.run(b.config());
    b.expect_bar(out, 0, 0.0, 5.0 / 11.0, 1.0);
}

TEST(ExecutionPrice, EndingSourceTransfersBeforeOrdersAndOnlyResidualTradeEarnsLeg) {
    for (double ratio : {1.0, 1.5}) {
        Book b(2, 2, true);
        b.signals = {{1, 0}, {0, 1}};
        b.quote(1, 0, kMissing, kMissing, kMissing, kMissing);
        b.quote(1, 1, 90, 120, 80, 110);
        b.panel.returns_total = {0, 0, 0.125, 0.7};
        b.panel.move_events.push_back({1, 0, 1, ratio, true});
        auto out = b.run(b.config());
        const double residual_trade = 1.0 - ratio;
        b.expect_bar(out, 1, 0.125, residual_trade * 0.1, std::fabs(residual_trade));
        EXPECT_EQ(b.rows[1].previous, (std::vector<double>{1.0, 0.0}));
        EXPECT_EQ(b.rows[1].weights, (std::vector<double>{0.0, 1.0}));
        EXPECT_EQ(b.rows[1].trades, (std::vector<double>{0.0, residual_trade}));
        EXPECT_EQ(out.missing_data[1].successor_conversions, 1u);
        EXPECT_DOUBLE_EQ(out.missing_data[1].converted_gross, ratio);
    }
}

TEST(ExecutionPrice, NonendingChildReceiptAfterOrdersHasNoFreeTradingPnl) {
    Book b(3, 2, true);
    b.signals = {{1, 0}, {1, 0}, {1, 0}};
    b.quote(1, 1, 90, 120, 80, 110);
    b.quote(2, 1, 99, 132, 88, 121);
    b.panel.returns_total = {0, 0, 0.03, 0.7, 0.02, 0.04};
    b.panel.move_events.push_back({1, 0, 1, 0.25, false});
    auto out = b.run(b.config());
    b.expect_bar(out, 1, 0.03, 0.0, 0.0);
    EXPECT_EQ(b.rows[1].weights, (std::vector<double>{1.0, 0.25}));
    EXPECT_EQ(b.rows[1].trades, (std::vector<double>{0.0, 0.0}));
    b.expect_bar(out, 2, 0.02 + 0.25 * 0.04, -0.25 * 0.1, 0.25);
    EXPECT_EQ(b.rows[2].trades, (std::vector<double>{0.0, -0.25}));
}

TEST(ExecutionPrice, MissingSuccessorOhlReservesTransferredExposureBeforeMarketOrders) {
    Book b(2, 3, true);
    b.signals = {{1, 0, 0}, {0, 0, 1}};
    b.quote(1, 0, kMissing, kMissing, kMissing, kMissing);
    b.quote(1, 1, kMissing, 120, 80, 110);
    b.quote(1, 2, 90, 120, 80, 110);
    b.panel.returns_total = {0, 0, 0, 0.125, 0.7, 0.9};
    b.panel.move_events.push_back({1, 0, 1, 0.5, true});
    auto out = b.run(b.config());
    b.expect_bar(out, 1, 0.125, 0.5 * 0.1, 0.5);
    EXPECT_EQ(b.rows[1].weights, (std::vector<double>{0.0, 0.5, 0.5}));
    EXPECT_EQ(b.rows[1].trades, (std::vector<double>{0.0, 0.0, 0.5}));
    EXPECT_DOUBLE_EQ(out.missing_data[1].frozen_gross, 0.5);
    EXPECT_EQ(out.missing_data[1].blocked_order_count, 1u);
}

TEST(ExecutionPrice, FixedNotionalConventionIsDistinctFromAnUnrebalancedShareLedger) {
    Book b(2, 1);
    b.quote(0, 0, 90, 120, 80, 110); // Buy one unit of notional at execution price 100.
    b.quote(1, 0, 121, 121, 121, 121);
    b.signals = {{1}, {1}};
    auto out = b.run(b.config());
    b.expect_bar(out, 0, 0.0, 0.1, 1.0);
    b.expect_bar(out, 1, 0.1, 0.0, 0.0);
    EXPECT_DOUBLE_EQ(b.rows[1].weights[0], 1.0);
    // Engine weights remain fixed-book notionals. A physical 1/100-share holding
    // instead earns (121-110)/100 = 0.11 on day 1, before any rebalancing or fees.
    const double share_ledger_day_one = (121.0 - 110.0) / 100.0;
    EXPECT_NEAR(share_ledger_day_one - out.gross_ret[1], 0.01, kTol);
}

TEST(ExecutionPriceVwap, FillIsSameDayVwapAndTurnoverMatchesClose) {
    Book b(3, 2);
    b.signals = {{1, -1}, {1, 1}, {0, 0}};
    b.vquote(0, 0, 100, 120, 80, 110);
    b.vquote(0, 1, 100, 120, 80, 110);
    b.vquote(1, 0, 125, 130, 105, 121);
    b.vquote(1, 1, 110, 130, 105, 121);
    b.vquote(2, 0, 121, 150, 100, 121);
    b.vquote(2, 1, 110, 150, 100, 121);
    auto out = b.run(b.vconfig());
    b.expect_bar(out, 0, 0.0, 0.5 * 0.1 - 0.5 * 0.1, 1.0);
    b.expect_bar(out, 1, 0.5 * 0.1 - 0.5 * 0.1, 1.0 * (121.0 / 110.0 - 1.0), 1.0);
    b.expect_bar(out, 2, 0.0, -0.5 * 0.0 - 0.5 * (121.0 / 110.0 - 1.0), 1.0);
    auto close_out = b.run(SimulatorConfig{b.panel.T, b.panel.N, kFee});
    EXPECT_EQ(close_out.turnover, out.turnover);
}

TEST(ExecutionPriceVwap, ExplicitDelayedSignalExecutesAtVwapWithoutAnotherShift) {
    Book b(2, 1);
    b.signals = {{0}, {1}}; // Already delayed pipeline output.
    b.vquote(1, 0, 100, 120, 80, 110);
    auto out = b.run(b.vconfig());
    b.expect_bar(out, 0, 0.0, 0.0, 0.0);
    b.expect_bar(out, 1, 0.0, 0.1, 1.0);
}

TEST(ExecutionPriceVwap, MissingCarryFreezesBudgetAndFillsWhenVwapReturns) {
    for (const auto* field : {"vwap", "high", "low"}) {
        SCOPED_TRACE(field);
        Book b(3, 3);
        b.signals = {{1, 1, 0}, {0, 0, 1}, {0, 0, 1}};
        b.vquote(1, 0, 100, 120, 80, 110);
        b.panel.at(field, 1, 0) = kMissing;
        b.vquote(1, 2, 100, 120, 80, 110);
        b.vquote(2, 0, 110, 132, 88, 121);
        b.vquote(2, 2, 110, 132, 88, 121);
        auto out = b.run(b.vconfig());
        b.expect_bar(out, 1, 0.5 * 0.1, 0.5 * 0.1, 1.0);
        EXPECT_EQ(b.rows[1].weights, (std::vector<double>{0.5, 0.0, 0.5}));
        EXPECT_NEAR(out.missing_data[1].frozen_gross, 0.5, kTol);
        EXPECT_EQ(out.missing_data[1].blocked_order_count, 1u);
        b.expect_bar(out, 2, 0.5 * 0.1 + 0.5 * 0.1, -0.5 * 0.1 + 0.5 * 0.1, 1.0);
        EXPECT_EQ(b.rows[2].trades, (std::vector<double>{-0.5, 0.0, 0.5}));
    }
}

TEST(ExecutionPriceVwap, ErrorThrowsOnlyWhenTradeRequired) {
    Book hold(2, 1);
    hold.signals = {{1}, {1}};
    hold.panel.at("vwap", 1, 0) = kMissing;
    auto cfg = hold.vconfig();
    cfg.missing_close_policy = MissingClosePolicy::Error;
    auto out = hold.run(cfg);
    hold.expect_bar(out, 1, 0.0, 0.0, 0.0);

    Book exit(2, 1);
    exit.signals = {{1}, {0}};
    exit.panel.at("vwap", 1, 0) = kMissing;
    EXPECT_THROW(exit.run(cfg), std::runtime_error);
}

TEST(ExecutionPriceVwap, VwapOrCloseOutsideRangeIsUnfilled) {
    for (const auto& q : std::vector<std::vector<float>>{
                 {130, 120, 80, 110}, {70, 120, 80, 110}, {100, 105, 80, 110},
                 {100, 120, 115, 110}, {100, 80, 120, 100}}) {
        Book b(1, 1);
        b.signals = {{1}};
        b.vquote(0, 0, q[0], q[1], q[2], q[3]);
        auto cfg = b.vconfig();
        auto out = b.run(cfg);
        b.expect_bar(out, 0, 0.0, 0.0, 0.0);
        EXPECT_EQ(out.missing_data[0].blocked_order_count, 1u);
        cfg.missing_close_policy = MissingClosePolicy::Error;
        EXPECT_THROW(b.run(cfg), std::runtime_error);
    }
}

TEST(ExecutionPriceVwap, CorruptRequiredQuotesThrowButUnheldZeroTargetsDoNot) {
    for (const auto* field : {"vwap", "high", "low"}) {
        for (float bad : {0.0f, -1.0f, std::numeric_limits<float>::infinity()}) {
            Book b(2, 2);
            b.signals = {{1, 0}, {1, 0}};
            b.panel.at(field, 1, 1) = bad; // unheld, zero target
            EXPECT_NO_THROW(b.run(b.vconfig()));
            b.panel.at(field, 1, 0) = bad; // held position
            EXPECT_THROW(b.run(b.vconfig()), std::runtime_error);
        }
    }
    Book b(1, 1);
    b.signals = {{1}};
    b.panel.at("vwap", 0, 0) = -5.0f;
    EXPECT_THROW(b.run(b.vconfig()), std::runtime_error);
}

TEST(ExecutionPriceVwap, CorporateTransferHasNoExecutionLeg) {
    Book b(2, 2, true);
    b.signals = {{1, 0}, {0, 1}};
    b.vquote(1, 0, kMissing, kMissing, kMissing, kMissing);
    b.vquote(1, 1, 100, 120, 80, 110);
    b.panel.returns_total = {0, 0, 0.125, 0.7};
    b.panel.move_events.push_back({1, 0, 1, 1.0, true});
    auto out = b.run(b.vconfig());
    b.expect_bar(out, 1, 0.125, 0.0, 0.0);
    EXPECT_EQ(b.rows[1].trades, (std::vector<double>{0.0, 0.0}));
}

TEST(ExecutionPriceVwap, MissingVwapColumnRejectedAtConstruction) {
    for (const auto* field : {"vwap", "high", "low"}) {
        Book b(1, 1);
        b.panel.arrays.erase(field);
        EXPECT_THROW((Simulator{b.panel, b.vconfig()}), std::invalid_argument);
    }
}

namespace {
void expect_throw_with(const std::function<void()>& fn, const std::string& text) {
    try { fn(); ADD_FAILURE() << "expected throw"; }
    catch (const std::runtime_error& e) { EXPECT_NE(std::string(e.what()).find(text), std::string::npos) << e.what(); }
}
} // namespace

TEST(ExecutionPriceVwap, HeldMissingCloseReservesBudgetOnceForBothIntradayPrices) {
    for (bool vwap : {false, true}) {
        SCOPED_TRACE(vwap);
        Book b(2, 3);
        b.signals = {{1, 1, 0}, {0, 0, 1}};
        b.panel.at("close", 1, 0) = kMissing;
        auto out = b.run(vwap ? b.vconfig() : b.config());
        EXPECT_DOUBLE_EQ(out.missing_data[1].frozen_gross, 0.5);
        EXPECT_EQ(b.rows[1].weights, (std::vector<double>{0.5, 0.0, 0.5}));
    }
}

TEST(ExecutionPriceVwap, HighBelowLowAloneIsUnfilledAndHeldPositionIsFrozen) {
    Book b(1, 1);
    b.signals = {{1}};
    b.vquote(0, 0, 100, 99.9f, 100.1f, 100);
    auto out = b.run(b.vconfig());
    EXPECT_DOUBLE_EQ(b.rows[0].weights[0], 0.0);
    EXPECT_EQ(out.missing_data[0].blocked_order_count, 1u);

    Book h(2, 2);
    h.signals = {{1, 0}, {0, 1}};
    h.vquote(1, 0, 130, 120, 80, 110); // held name: vwap above high
    out = h.run(h.vconfig());
    EXPECT_EQ(h.rows[1].weights, (std::vector<double>{1.0, 0.0}));
    EXPECT_DOUBLE_EQ(out.missing_data[1].frozen_gross, 1.0);
    EXPECT_EQ(out.missing_data[1].blocked_order_count, 1u);
    auto cfg = h.vconfig();
    cfg.missing_close_policy = MissingClosePolicy::Error;
    expect_throw_with([&] { h.run(cfg); }, "VWAP");
}

TEST(ExecutionPriceVwap, ErrorMessagesNameVwap) {
    Book missing(2, 1);
    missing.signals = {{1}, {0}};
    missing.panel.at("vwap", 1, 0) = kMissing;
    auto cfg = missing.vconfig();
    cfg.missing_close_policy = MissingClosePolicy::Error;
    expect_throw_with([&] { missing.run(cfg); }, "VWAP");
    Book corrupt(1, 1);
    corrupt.signals = {{1}};
    corrupt.panel.at("vwap", 0, 0) = -5.0f;
    expect_throw_with([&] { corrupt.run(corrupt.vconfig()); }, "VWAP");
}
