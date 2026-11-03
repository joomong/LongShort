// Thin cache engine path (BRIEF_S section 3-3): holding P&L = carried x returns_total,
// move_events applied from the bar-start book. Expected values are computed by hand.
#include <gtest/gtest.h>
#include "simulation_data.h"
#include "simulator_engine.h"
#include <cmath>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
constexpr float kNaNf = std::numeric_limits<float>::quiet_NaN();
constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();
constexpr double kFee = 0.001;
constexpr double kTol = 1e-12;

struct Thin {
    DataPanel panel;
    std::vector<std::vector<float>> signals;
    Thin(unsigned T, unsigned N) {
        panel.T = T; panel.N = N;
        panel.timestamps.resize(T);
        for (unsigned t = 0; t < T; ++t) panel.timestamps[t] = static_cast<int64_t>(t) * 86400000;
        for (unsigned ii = 0; ii < N; ++ii) panel.symbols.push_back("S" + std::to_string(ii));
        panel.arrays["close"].assign(static_cast<size_t>(T) * N, 1.0f);
        panel.thin_layout = true;
        panel.returns_total.assign(static_cast<size_t>(T) * N, kNaN);
        signals.assign(T, std::vector<float>(N, 0.0f));
    }
    double& rt(unsigned t, unsigned ii) { return panel.returns_total[static_cast<size_t>(t) * panel.N + ii]; }
    float& close(unsigned t, unsigned ii) { return panel.arrays["close"][static_cast<size_t>(t) * panel.N + ii]; }
    void move(unsigned bar, unsigned source, int32_t destination, double ratio, bool ends) {
        DataPanel::MoveEvent event;
        event.bar = bar; event.source = source; event.destination = destination;
        event.value_ratio = ratio; event.source_ends = ends;
        panel.move_events.push_back(event);
    }
    // Per-bar end-of-bar weights (after received children joined).
    std::vector<std::vector<double>> weights;
    SimulatorOutput run(MissingClosePolicy policy = MissingClosePolicy::Carry,
                        ExecutionPrice price = ExecutionPrice::Close) {
        SimulatorConfig config;
        config.T = panel.T; config.N = panel.N; config.fee_rate = kFee;
        config.missing_close_policy = policy;
        config.execution_price = price;
        weights.assign(panel.T, {});
        config.accounting_row_callback = [this](const AccountingRowView& row) {
            weights[row.bar] = row.weights;
        };
        Simulator simulator(panel, config);
        return simulator.run_from_signals(signals);
    }
};

// Bar 0 opens the book from zero: turnover = gross, ret = -fee * gross.
void expect_open(const SimulatorOutput& out, double gross) {
    EXPECT_NEAR(out.turnover[0], gross, kTol);
    EXPECT_NEAR(out.ret[0], -kFee * gross, kTol);
}
} // namespace

TEST(ThinMoves, OrdinaryDay) {
    Thin c(2, 2);
    c.signals[0] = {1.0f, 0.0f};
    c.signals[1] = {1.0f, 0.0f};
    c.rt(1, 0) = 0.02; c.rt(1, 1) = 0.5;  // unheld B earns nothing
    auto out = c.run();
    expect_open(out, 1.0);
    EXPECT_NEAR(out.ret[1], 0.02, kTol);
    EXPECT_NEAR(out.turnover[1], 0.0, kTol);
}

TEST(ThinMoves, PureSplitHasNoPnl) {
    Thin c(3, 1);
    c.signals.assign(3, {1.0f});
    c.close(1, 0) = 0.5f; c.close(2, 0) = 0.5f;  // raw close halves
    c.rt(1, 0) = 0.0;                            // bar 1 split row: 0
    auto out = c.run();                          // bar 2: no row (NaN) -> 0
    EXPECT_NEAR(out.ret[1], 0.0, kTol);
    EXPECT_NEAR(out.ret[2], 0.0, kTol);
    EXPECT_NEAR(out.turnover[1] + out.turnover[2], 0.0, kTol);
}

TEST(ThinMoves, CashDividendAndValueOnly) {
    Thin c(3, 1);
    c.signals.assign(3, {-1.0f});
    c.rt(1, 0) = 0.03;   // price move + dividend
    c.rt(2, 0) = 0.04;   // value-only distribution: no event row
    auto out = c.run();
    EXPECT_NEAR(out.ret[1], -0.03, kTol);
    EXPECT_NEAR(out.ret[2], -0.04, kTol);
    EXPECT_TRUE(c.panel.move_events.empty());
}

TEST(ThinMoves, SpinoffJoinsAfterExecutionThenSoldNextBar) {
    Thin c(3, 2);  // A=0 parent, C=1 child
    c.signals.assign(3, {1.0f, 0.0f});
    c.rt(1, 0) = 0.01;           // parent total includes the child value
    c.rt(1, 1) = 0.5;            // child's own move on the ex-bar does not reach the book
    c.rt(2, 0) = 0.0;
    c.rt(2, 1) = 0.1;
    c.move(1, 0, 1, 0.2, false);
    auto out = c.run();
    EXPECT_NEAR(out.ret[1], 0.01, kTol);
    EXPECT_NEAR(out.turnover[1], 0.0, kTol);
    EXPECT_NEAR(c.weights[1][0], 1.0, kTol);
    EXPECT_NEAR(c.weights[1][1], 0.2, kTol);
    // bar 2: 0.2 x 0.1 = 0.02, target 0 sells 0.2 with the fee
    EXPECT_NEAR(out.turnover[2], 0.2, kTol);
    EXPECT_NEAR(out.ret[2], 0.02 - 0.2 * kFee, kTol);
    EXPECT_NEAR(c.weights[2][1], 0.0, kTol);
    EXPECT_EQ(out.missing_data[1].retirement_count, 0u);
}

TEST(ThinMoves, CashMergerRetiresToZero) {
    Thin c(3, 2);
    c.signals[0] = {1.0f, 0.0f};
    c.signals[1] = {1.0f, 1.0f};  // A is retired: excluded from the target
    c.signals[2] = {1.0f, 1.0f};
    c.rt(1, 0) = 0.25;
    c.move(1, 0, -1, kNaN, true);
    auto out = c.run();
    EXPECT_NEAR(out.ret[1], 0.25 - kFee * 1.0, kTol);
    EXPECT_NEAR(out.turnover[1], 1.0, kTol);
    EXPECT_NEAR(c.weights[1][0], 0.0, kTol);
    EXPECT_NEAR(c.weights[1][1], 1.0, kTol);
    EXPECT_EQ(out.missing_data[1].retirement_count, 1u);
    EXPECT_EQ(out.missing_data[1].successor_conversions, 0u);
    EXPECT_NEAR(c.weights[2][0], 0.0, kTol);
}

TEST(ThinMoves, StockMergerNetsAgainstTarget) {
    Thin c(2, 2);
    c.signals[0] = {1.0f, 0.0f};
    c.signals[1] = {0.0f, 1.0f};
    c.rt(1, 0) = 0.1;
    c.rt(1, 1) = 0.05;   // the handed-over weight earns nothing on the settlement bar
    c.move(1, 0, 1, 1.5, true);
    auto out = c.run();
    EXPECT_NEAR(out.turnover[1], 0.5, kTol);   // carried B 1.5 -> target 1.0
    EXPECT_NEAR(out.ret[1], 0.1 - 0.5 * kFee, kTol);
    EXPECT_NEAR(c.weights[1][1], 1.0, kTol);
    EXPECT_EQ(out.missing_data[1].retirement_count, 1u);
    EXPECT_EQ(out.missing_data[1].successor_conversions, 1u);
    EXPECT_NEAR(out.missing_data[1].converted_gross, 1.5, kTol);
}

TEST(ThinMoves, MixedCashAndStockMerger) {
    Thin c(2, 2);
    c.signals[0] = {1.0f, 0.0f};
    c.signals[1] = {0.0f, 1.0f};
    c.rt(1, 0) = 0.1;              // (cash 0.5 + stock 0.6) / 1 - 1
    c.move(1, 0, 1, 0.6, true);
    auto out = c.run();
    EXPECT_NEAR(out.turnover[1], 0.4, kTol);
    EXPECT_NEAR(out.ret[1], 0.1 - 0.4 * kFee, kTol);
    EXPECT_NEAR(out.missing_data[1].converted_gross, 0.6, kTol);
}

TEST(ThinMoves, RightsExpiry) {
    Thin c(2, 2);  // A=0, right R=1
    c.signals[0] = {0.75f, 0.25f};
    c.signals[1] = {1.0f, 0.0f};
    c.close(1, 1) = kNaNf;
    c.rt(1, 1) = -1.0;
    c.move(1, 1, -1, kNaN, true);
    auto out = c.run();
    EXPECT_NEAR(out.turnover[1], 0.25, kTol);
    EXPECT_NEAR(out.ret[1], -0.25 - 0.25 * kFee, kTol);
    EXPECT_EQ(out.missing_data[1].retirement_count, 1u);
    EXPECT_EQ(out.missing_data[1].missing_held_count, 0u);  // settled, not frozen
}

TEST(ThinMoves, LongShortSymmetric) {
    Thin c(2, 4);  // A->C long, B->D short
    c.signals[0] = {0.5f, -0.5f, 0.0f, 0.0f};
    c.signals[1] = {0.0f, 0.0f, 0.5f, -0.5f};
    c.rt(1, 0) = 0.1; c.rt(1, 1) = 0.1;
    c.move(1, 0, 2, 2.0, true);
    c.move(1, 1, 3, 2.0, true);
    auto out = c.run();
    EXPECT_NEAR(out.ret[1], 0.0 - 1.0 * kFee, kTol);   // 0.05 - 0.05; trades 0.5 + 0.5
    EXPECT_NEAR(out.turnover[1], 1.0, kTol);
    EXPECT_NEAR(c.weights[1][2], 0.5, kTol);
    EXPECT_NEAR(c.weights[1][3], -0.5, kTol);
    EXPECT_EQ(out.missing_data[1].successor_conversions, 2u);
    EXPECT_NEAR(out.missing_data[1].converted_gross, 2.0, kTol);
}

TEST(ThinMoves, ExistingChildPlusReceipt) {
    Thin c(2, 2);
    c.signals[0] = {0.5f, 0.5f};
    c.signals[1] = {0.5f, 0.5f};
    c.rt(1, 0) = 0.02; c.rt(1, 1) = 0.1;
    c.move(1, 0, 1, 0.2, false);
    auto out = c.run();
    EXPECT_NEAR(out.ret[1], 0.5 * 0.02 + 0.5 * 0.1, kTol);
    EXPECT_NEAR(out.turnover[1], 0.0, kTol);
    EXPECT_NEAR(c.weights[1][1], 0.6, kTol);
}

TEST(ThinMoves, SameBarChainUsesBarStartBook) {
    Thin c(2, 3);  // A->B ends, B->C distribution on the same bar
    c.signals[0] = {0.5f, 0.5f, 0.0f};
    c.signals[1] = {0.0f, 1.0f, 0.0f};
    c.rt(1, 0) = 0.0; c.rt(1, 1) = 0.0;
    c.move(1, 0, 1, 1.0, true);
    c.move(1, 1, 2, 0.1, false);
    auto out = c.run();
    EXPECT_NEAR(out.turnover[1], 0.0, kTol);   // carried B 0.5 + 0.5 = target 1
    EXPECT_NEAR(c.weights[1][1], 1.0, kTol);
    EXPECT_NEAR(c.weights[1][2], 0.05, kTol);  // 0.5 (bar-start B) x 0.1, not 1.0 x 0.1
    EXPECT_NEAR(out.ret[1], 0.0, kTol);
}

TEST(ThinMoves, MoveToRetiredDestinationThrows) {
    Thin c(2, 3);  // A->B and B->C both end on one bar: A's weight would vanish
    c.signals[0] = {0.5f, 0.5f, 0.0f};
    c.signals[1] = {0.0f, 0.0f, 1.0f};
    c.move(1, 0, 1, 1.0, true);
    c.move(1, 1, 2, 1.0, true);
    EXPECT_THROW(c.run(), std::runtime_error);
}

TEST(ThinMoves, FrozenHoldingResumesWithAccumulatedReturn) {
    Thin c(4, 2);
    c.signals.assign(4, {0.5f, 0.5f});
    c.signals[1] = {0.0f, 1.0f};         // wants to sell A, but A has no quote
    c.close(1, 0) = kNaNf; c.close(2, 0) = kNaNf;
    c.rt(3, 0) = 0.3;                    // resumption row spans bars 1..3
    auto out = c.run();
    EXPECT_NEAR(out.ret[1], 0.0, kTol);
    EXPECT_EQ(out.missing_data[1].missing_held_count, 1u);
    EXPECT_EQ(out.missing_data[1].blocked_order_count, 1u);
    EXPECT_NEAR(c.weights[1][0], 0.5, kTol);   // frozen
    EXPECT_NEAR(c.weights[1][1], 0.5, kTol);   // budget 1 - 0.5 caps B at 0.5
    EXPECT_NEAR(out.turnover[1], 0.0, kTol);
    EXPECT_NEAR(out.ret[3], 0.5 * 0.3, kTol);
    EXPECT_EQ(out.missing_data[3].resumed_holding_count, 1u);
}

TEST(ThinMoves, SettlementBarWithoutQuote) {
    Thin c(2, 2);
    c.signals[0] = {1.0f, 0.0f};
    c.signals[1] = {0.0f, 1.0f};
    c.close(1, 0) = kNaNf;
    c.rt(1, 0) = -0.4;
    c.move(1, 0, -1, kNaN, true);
    auto out = c.run();
    EXPECT_NEAR(out.ret[1], -0.4 - kFee, kTol);
    EXPECT_NEAR(out.turnover[1], 1.0, kTol);
    EXPECT_EQ(out.missing_data[1].missing_held_count, 0u);
    EXPECT_EQ(out.missing_data[1].retirement_count, 1u);
}

// A successor position landing on a quoteless name cannot trade on the move bar:
// its budget is reserved and Error rejects the required sale, for every execution price.
TEST(ThinMoves, SuccessorIntoQuotelessDestinationReservesBudget) {
    const auto make = [] {
        Thin c(3, 3);
        c.signals[0] = {1.0f, 0.0f, 0.0f};
        c.signals[1] = {0.0f, 0.0f, 1.0f};
        c.signals[2] = {0.0f, 0.0f, 1.0f};
        c.close(1, 0) = kNaNf; c.close(1, 1) = kNaNf; c.close(2, 1) = kNaNf;
        c.move(1, 0, 1, 0.5, true);
        for (const char* name : {"open", "high", "low"})
            c.panel.arrays[name] = c.panel.arrays["close"];
        return c;
    };
    for (ExecutionPrice price : {ExecutionPrice::Close, ExecutionPrice::Ohlc4}) {
        Thin c = make();
        auto out = c.run(MissingClosePolicy::Carry, price);
        for (unsigned bar : {1u, 2u}) {
            EXPECT_EQ(c.weights[bar], (std::vector<double>{0.0, 0.5, 0.5})) << "bar " << bar;
            EXPECT_DOUBLE_EQ(out.missing_data[bar].frozen_gross, 0.5) << "bar " << bar;
        }
        Thin e = make();
        try {
            e.run(MissingClosePolicy::Error, price);
            ADD_FAILURE() << "Error policy must reject the unfillable sale";
        } catch (const std::runtime_error& error) {
            // Rejected on the move bar, not only later by the pre-valuation gate.
            const std::string what = error.what();
            EXPECT_NE(what.find("Cannot execute"), std::string::npos) << what;
            EXPECT_NE(what.find("at bar 1, asset 1"), std::string::npos) << what;
        }
    }
}
