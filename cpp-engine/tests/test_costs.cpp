#include <gtest/gtest.h>
#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/writer.h>
#include <atomic>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <limits>
#include <string>
#include <vector>
#include <unistd.h>
#include "data/config_loader.h"
#include "data/data_panel.h"
#include "engine/costs.h"
#include "engine/simulator.h"
#include "api/json_serializer.h"
#include "metrics/timeseries_output.h"

// ── Borrow financing of the short book (docs/corporate_actions_engine_2026-09-17.md "costs") ──
//
// cost(bar) = sum over short weights carried into the bar of |w| * rate / day_count * days,
// days = calendar days since the previous bar. Net only; gross untouched.

namespace {
const double kNaN = std::numeric_limits<double>::quiet_NaN();
constexpr double kFee = 0.0005;
constexpr int64_t kDay = 86400000;

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

struct Rows {
    std::vector<unsigned> bar;
    std::vector<double> gross, fee, net, borrow;
    std::vector<std::vector<double>> previous_weights;
    void record(const AccountingRowView& row) {
        bar.push_back(row.bar);
        gross.push_back(row.gross_pnl);
        fee.push_back(row.fee);
        net.push_back(row.net_pnl);
        borrow.push_back(row.borrow_cost);
        previous_weights.push_back(row.previous_weights);
    }
};

SimulatorConfig config_for(const DataPanel& panel, int mode, const BorrowCostModel* borrow, Rows* rows = nullptr) {
    SimulatorConfig config;
    config.T = panel.T;
    config.N = panel.N;
    config.fee_rate = kFee;
    config.mode = mode;
    config.borrow = borrow;
    if (rows) config.accounting_row_callback = [rows](const AccountingRowView& row) { rows->record(row); };
    return config;
}

CostsConfig borrow_config(double default_rate = kNaN, const std::string& path = "", unsigned day_count = 360) {
    CostsConfig config;
    config.borrow.enabled = true;
    config.borrow.default_rate = default_rate;
    config.borrow.path = path;
    config.borrow.day_count = day_count;
    return config;
}

struct DataFiles {
    std::filesystem::path dir;
    DataFiles() {
        static std::atomic<unsigned> sequence{0};
        dir = std::filesystem::temp_directory_path() /
              ("longshort_costs_" + std::to_string(getpid()) + "_" + std::to_string(sequence++));
        std::filesystem::create_directories(dir);
    }
    ~DataFiles() { std::error_code error; std::filesystem::remove_all(dir, error); }
    std::string csv(const std::string& name, const std::string& body) {
        std::ofstream(dir / name) << body;
        return name;
    }
    // Parquet table with string symbols, timestamp[ns] dates (null `to` = open-ended) and double rates.
    std::string parquet(const std::string& name, const std::vector<std::string>& symbols,
                        const std::vector<int64_t>& from, const std::vector<int64_t>& to,
                        const std::vector<double>& rate) {
        arrow::StringBuilder symbol_builder;
        arrow::TimestampBuilder from_builder(arrow::timestamp(arrow::TimeUnit::NANO), arrow::default_memory_pool());
        arrow::TimestampBuilder to_builder(arrow::timestamp(arrow::TimeUnit::NANO), arrow::default_memory_pool());
        arrow::DoubleBuilder rate_builder;
        for (size_t i = 0; i < symbols.size(); ++i) {
            if (!symbol_builder.Append(symbols[i]).ok()) throw std::runtime_error("append");
            if (!from_builder.Append(from[i] * 1000000).ok()) throw std::runtime_error("append");
            if (to[i] < 0) { if (!to_builder.AppendNull().ok()) throw std::runtime_error("append"); }
            else if (!to_builder.Append(to[i] * 1000000).ok()) throw std::runtime_error("append");
            if (!rate_builder.Append(rate[i]).ok()) throw std::runtime_error("append");
        }
        std::vector<std::shared_ptr<arrow::Array>> arrays = {
            symbol_builder.Finish().ValueOrDie(), from_builder.Finish().ValueOrDie(),
            to_builder.Finish().ValueOrDie(), rate_builder.Finish().ValueOrDie()};
        std::vector<std::shared_ptr<arrow::Field>> fields;
        for (const auto& [column, array] : std::vector<std::pair<std::string, std::shared_ptr<arrow::Array>>>{
                 {"symbol", arrays[0]}, {"from", arrays[1]}, {"to", arrays[2]}, {"rate", arrays[3]}})
            fields.push_back(arrow::field(column, array->type()));
        auto output = arrow::io::FileOutputStream::Open((dir / name).string()).ValueOrDie();
        auto table = arrow::Table::Make(arrow::schema(fields), arrays);
        if (!parquet::arrow::WriteTable(*table, arrow::default_memory_pool(), output, 2).ok())
            throw std::runtime_error("write");
        if (!output->Close().ok()) throw std::runtime_error("close");
        return name;
    }
};
}  // namespace

TEST(BorrowCost, DisabledModelAndNoModelAreBitIdenticalToTheFixedBook) {
    auto panel = flat_panel(8, 3);
    for (unsigned t = 0; t < panel.T; ++t) {
        panel.at("close", t, 0) = 100.0f + 3.0f * static_cast<float>(t % 3);
        panel.at("close", t, 1) = 50.0f - 1.5f * static_cast<float>(t % 4);
    }
    const std::vector<std::vector<float>> signals(panel.T, {1, -1, 0.5f});
    CostsConfig off;   // everything default: disabled
    const auto disabled = BorrowCostModel::load(off, panel, ".");
    EXPECT_FALSE(disabled.enabled());
    EXPECT_FALSE(disabled.applied());
    for (int mode : {0, 1}) {
        const auto bare = Simulator(panel, config_for(panel, mode, nullptr)).run_from_signals(signals);
        const auto with_model = Simulator(panel, config_for(panel, mode, &disabled)).run_from_signals(signals);
        EXPECT_EQ(with_model.ret, bare.ret) << "mode=" << mode;
        EXPECT_EQ(with_model.turnover, bare.turnover);
        for (double cost : with_model.borrow_cost) EXPECT_EQ(cost, 0.0);
        for (unsigned count : with_model.borrow_unrated) EXPECT_EQ(count, 0u);
        // The gross series is rebuilt from net + fee exactly as before when nothing was charged.
        const auto reference = compute_timeseries(bare.timestamps, bare.ret, bare.turnover, bare.max_weight,
                                                  bare.position_count, kFee, 1);
        const auto rebuilt = compute_timeseries(with_model.timestamps, with_model.ret, with_model.turnover,
                                                with_model.max_weight, with_model.position_count, kFee, 1,
                                                nullptr, nullptr, nullptr, nullptr, &with_model.borrow_cost);
        EXPECT_EQ(rebuilt.cum_pnl_gross, reference.cum_pnl_gross);
        EXPECT_EQ(rebuilt.cum_pnl, reference.cum_pnl);
    }
}

TEST(BorrowCost, ShortBookPaysTheDefaultRatePerCalendarDayFromNetOnly) {
    auto panel = flat_panel(6, 2);
    for (unsigned t = 0; t < panel.T; ++t) panel.at("close", t, 1) = 50.0f + static_cast<float>(t);
    const auto model = BorrowCostModel::load(borrow_config(0.036), panel, ".");
    EXPECT_TRUE(model.enabled());
    EXPECT_TRUE(model.applied());
    EXPECT_DOUBLE_EQ(model.rate(1, 3), 0.036);
    const std::vector<std::vector<float>> signals(panel.T, {1, -1});   // 0.5 long, 0.5 short
    const double per_day = 0.5 * 0.036 / 360.0 * 1.0;
    for (int mode : {0, 1}) {
        Rows rows;
        const auto out = Simulator(panel, config_for(panel, mode, &model, &rows)).run_from_signals(signals);
        Rows bare_rows;
        const auto bare = Simulator(panel, config_for(panel, mode, nullptr, &bare_rows)).run_from_signals(signals);
        const unsigned first_charged = mode == 0 ? 1 : 2;   // the bar after the short was opened
        for (unsigned di = 0; di < panel.T; ++di) {
            const double expected = di >= first_charged ? per_day : 0.0;
            EXPECT_NEAR(out.borrow_cost[di], expected, 1e-15) << "mode=" << mode << " bar=" << di;
            EXPECT_NEAR(rows.borrow[di], expected, 1e-15);
            EXPECT_EQ(out.borrow_unrated[di], 0u);
            // Gross and fee are what they were; only net pays.
            EXPECT_EQ(rows.gross[di], bare_rows.gross[di]);
            EXPECT_EQ(rows.fee[di], bare_rows.fee[di]);
            EXPECT_EQ(out.turnover[di], bare.turnover[di]);
            EXPECT_NEAR(out.ret[di], bare.ret[di] - expected, 1e-15);
            EXPECT_NEAR(rows.net[di], rows.gross[di] - rows.fee[di] - expected, 1e-15);
        }
        // The gross cumulative series adds the financing back; the net one keeps it.
        const auto series = compute_timeseries(out.timestamps, out.ret, out.turnover, out.max_weight,
                                               out.position_count, kFee, 1, nullptr, nullptr, nullptr, nullptr,
                                               &out.borrow_cost);
        const auto reference = compute_timeseries(bare.timestamps, bare.ret, bare.turnover, bare.max_weight,
                                                  bare.position_count, kFee, 1);
        ASSERT_EQ(series.borrow_cost.size(), reference.cum_pnl.size());
        for (size_t i = 0; i < series.cum_pnl.size(); ++i) {
            EXPECT_NEAR(series.cum_pnl_gross[i], reference.cum_pnl_gross[i], 1e-15) << i;
            EXPECT_LE(series.cum_pnl[i], reference.cum_pnl[i] + 1e-15);
        }
        EXPECT_NEAR(series.cum_pnl.back(), reference.cum_pnl.back() - per_day * (panel.T - first_charged), 1e-15);
    }
}

TEST(BorrowCost, LongOnlyBookBorrowsNothing) {
    auto panel = flat_panel(5, 2);
    const auto model = BorrowCostModel::load(borrow_config(0.05), panel, ".");
    const std::vector<std::vector<float>> signals(panel.T, {1, 1});
    for (int mode : {0, 1}) {
        const auto out = Simulator(panel, config_for(panel, mode, &model)).run_from_signals(signals);
        const auto bare = Simulator(panel, config_for(panel, mode, nullptr)).run_from_signals(signals);
        EXPECT_EQ(out.ret, bare.ret) << "mode=" << mode;
        for (double cost : out.borrow_cost) EXPECT_EQ(cost, 0.0);
    }
}

TEST(BorrowCost, TableIntervalsOverrideTheDefaultAndOtherSymbolsKeepIt) {
    DataFiles files;
    auto panel = flat_panel(6, 3);
    // S1 pays 7.2% on days 2..3 (inclusive), S2 has no row; everything else the 3.6% default.
    const std::string table = files.parquet("rates.parquet", {"S1", "S1", "ZZ"},
        {2 * kDay, 4 * kDay, 0}, {3 * kDay, -1, -1}, {0.072, 0.018, 0.5});
    const auto model = BorrowCostModel::load(borrow_config(0.036, table), panel, files.dir.string());
    EXPECT_EQ(model.table_rows(), 3u);
    EXPECT_EQ(model.table_rows_matched(), 2u);   // ZZ is not in the panel
    EXPECT_EQ(model.symbols_rated(), 1u);
    EXPECT_DOUBLE_EQ(model.rate(1, 1), 0.036);
    EXPECT_DOUBLE_EQ(model.rate(1, 2), 0.072);
    EXPECT_DOUBLE_EQ(model.rate(1, 3), 0.072);
    EXPECT_DOUBLE_EQ(model.rate(1, 4), 0.018);   // open-ended row
    EXPECT_DOUBLE_EQ(model.rate(1, 5), 0.018);
    EXPECT_DOUBLE_EQ(model.rate(2, 3), 0.036);
    const std::vector<std::vector<float>> signals(panel.T, {2, -1, -1});   // shorts 0.25 each
    for (int mode : {0, 1}) {
        const auto out = Simulator(panel, config_for(panel, mode, &model)).run_from_signals(signals);
        const auto expected = [&](unsigned di) {
            if (di < (mode == 0 ? 1u : 2u)) return 0.0;
            return 0.25 * model.rate(1, di) / 360.0 + 0.25 * 0.036 / 360.0;
        };
        for (unsigned di = 0; di < panel.T; ++di)
            EXPECT_NEAR(out.borrow_cost[di], expected(di), 1e-15) << "mode=" << mode << " bar=" << di;
        EXPECT_NEAR(out.borrow_cost[3], 0.25 * 0.072 / 360.0 + 0.25 * 0.036 / 360.0, 1e-15);
    }
    // The same table as CSV, with an empty `to` for the open-ended row.
    const std::string csv = files.csv("rates.csv",
        "symbol,from,to,rate\nS1,1970-01-03,1970-01-04,0.072\nS1,1970-01-05,,0.018\nZZ,1970-01-01,,0.5\n");
    const auto from_csv = BorrowCostModel::load(borrow_config(0.036, csv), panel, files.dir.string());
    EXPECT_EQ(from_csv.table_rows_matched(), 2u);
    for (unsigned bar = 0; bar < panel.T; ++bar) EXPECT_DOUBLE_EQ(from_csv.rate(1, bar), model.rate(1, bar)) << bar;
    // Without a default, a rated symbol is charged and an unrated short is counted.
    const auto no_default = BorrowCostModel::load(borrow_config(kNaN, table), panel, files.dir.string());
    EXPECT_TRUE(no_default.applied());
    EXPECT_TRUE(std::isnan(no_default.rate(2, 3)));
    const auto out = Simulator(panel, config_for(panel, 0, &no_default)).run_from_signals(signals);
    EXPECT_NEAR(out.borrow_cost[3], 0.25 * 0.072 / 360.0, 1e-15);
    EXPECT_EQ(out.borrow_unrated[3], 1u);
    EXPECT_EQ(out.borrow_unrated[0], 0u);   // nothing carried into the first bar
}

TEST(BorrowCost, CalendarDaysBetweenBarsCountTheWeekend) {
    // Thu 1970-01-01, Fri 1970-01-02, Mon 1970-01-05, Tue 1970-01-06.
    auto panel = flat_panel(4, 2);
    panel.timestamps = {0, kDay, 4 * kDay, 5 * kDay};
    const auto model = BorrowCostModel::load(borrow_config(0.036, "", 365), panel, ".");
    const std::vector<std::vector<float>> signals(panel.T, {0, -1});   // fully short
    const auto out = Simulator(panel, config_for(panel, 0, &model)).run_from_signals(signals);
    EXPECT_EQ(out.borrow_cost[0], 0.0);
    EXPECT_NEAR(out.borrow_cost[1], 1.0 * 0.036 / 365.0 * 1.0, 1e-15);
    EXPECT_NEAR(out.borrow_cost[2], 1.0 * 0.036 / 365.0 * 3.0, 1e-15);   // Friday close to Monday close
    EXPECT_NEAR(out.borrow_cost[3], 1.0 * 0.036 / 365.0 * 1.0, 1e-15);
}

TEST(BorrowCost, WarmupBarsChargeNothingBeforeTheFirstHolding) {
    auto panel = flat_panel(6, 2);
    const auto model = BorrowCostModel::load(borrow_config(0.036), panel, ".");
    const std::vector<std::vector<float>> signals(panel.T, {1, -1});
    for (int mode : {0, 1}) {
        auto config = config_for(panel, mode, &model);
        config.sim_start_index = 2;
        const auto out = Simulator(panel, config).run_from_signals(signals);
        // Bars 0..2 carry no holding into them. Warmup keeps the intent current, so both
        // modes open the short on bar 2 and the first charge is the bar after.
        for (unsigned di = 0; di <= 2; ++di) EXPECT_EQ(out.borrow_cost[di], 0.0) << "mode=" << mode << " bar=" << di;
        EXPECT_NEAR(out.borrow_cost[3], 0.5 * 0.036 / 360.0, 1e-15) << "mode=" << mode;
        EXPECT_NEAR(out.borrow_cost[4], 0.5 * 0.036 / 360.0, 1e-15);
    }
}

TEST(BorrowCost, EnabledWithoutAnyRateIsUnappliedAndChargesNothing) {
    auto panel = flat_panel(5, 3);
    const auto model = BorrowCostModel::load(borrow_config(), panel, ".");
    EXPECT_TRUE(model.enabled());
    EXPECT_FALSE(model.applied());
    EXPECT_FALSE(model.has_default_rate());
    EXPECT_TRUE(std::isnan(model.rate(0, 1)));
    const std::vector<std::vector<float>> signals(panel.T, {1, -1, -1});
    const auto out = Simulator(panel, config_for(panel, 0, &model)).run_from_signals(signals);
    const auto bare = Simulator(panel, config_for(panel, 0, nullptr)).run_from_signals(signals);
    EXPECT_EQ(out.ret, bare.ret);
    for (unsigned di = 1; di < panel.T; ++di) EXPECT_EQ(out.borrow_unrated[di], 2u) << di;
    EXPECT_EQ(out.borrow_unrated[0], 0u);
}

TEST(BorrowCost, TablesAreValidatedBeforeAnythingIsCharged) {
    DataFiles files;
    auto panel = flat_panel(6, 2);
    const auto rejects = [&](const std::string& path, const std::string& fragment) {
        try {
            BorrowCostModel::load(borrow_config(0.01, path), panel, files.dir.string());
            ADD_FAILURE() << "expected a load failure containing: " << fragment;
        } catch (const std::runtime_error& error) {
            EXPECT_NE(std::string(error.what()).find(fragment), std::string::npos) << error.what();
        }
    };
    // Overlaps: an open-ended row followed by a later start, and two closed rows sharing a day.
    rejects(files.parquet("open.parquet", {"S0", "S0"}, {0, 2 * kDay}, {-1, 3 * kDay}, {0.01, 0.02}), "overlapping");
    rejects(files.csv("shared.csv", "symbol,from,to,rate\nS0,1970-01-01,1970-01-03,0.01\nS0,1970-01-03,,0.02\n"),
            "overlapping");
    // Overlaps are refused even for symbols outside the panel: the table itself is wrong.
    rejects(files.csv("outside.csv", "symbol,from,to,rate\nZZ,1970-01-01,,0.01\nZZ,1970-01-02,,0.02\n"),
            "overlapping");
    rejects(files.csv("backwards.csv", "symbol,from,to,rate\nS0,1970-01-05,1970-01-02,0.01\n"), "ends before");
    rejects(files.csv("negative.csv", "symbol,from,to,rate\nS0,1970-01-01,,-0.01\n"), "negative");
    rejects(files.csv("text.csv", "symbol,from,to,rate\nS0,1970-01-01,,cheap\n"), "non-numeric");
    rejects(files.csv("columns.csv", "ticker,from,to,rate\nS0,1970-01-01,,0.01\n"), "column not found");
    rejects("absent.csv", "not found");
    // Adjacent days are not an overlap, and a disabled module never opens the table.
    EXPECT_NO_THROW(BorrowCostModel::load(
        borrow_config(0.01, files.csv("adjacent.csv", "symbol,from,to,rate\nS0,1970-01-01,1970-01-02,0.01\nS0,1970-01-03,,0.02\n")),
        panel, files.dir.string()));
    CostsConfig disabled = borrow_config(0.01, "absent.csv");
    disabled.borrow.enabled = false;
    EXPECT_NO_THROW(BorrowCostModel::load(disabled, panel, files.dir.string()));
    CostsConfig zero_days = borrow_config(0.01);
    zero_days.borrow.day_count = 0;
    EXPECT_THROW(BorrowCostModel::load(zero_days, panel, "."), std::runtime_error);
}

TEST(BorrowCost, TimeseriesJsonCarriesTheBorrowSeries) {
    const std::vector<int64_t> timestamps = {0, kDay, 2 * kDay};
    const std::vector<double> ret(3, 0.0), turnover(3, 0.0), max_weight(3, 0.0);
    const std::vector<int> position_count(3, 0);
    const std::vector<double> borrow = {9.0, 0.0001, 0.0002};
    const auto series = compute_timeseries(timestamps, ret, turnover, max_weight, position_count, kFee, 1,
                                           nullptr, nullptr, nullptr, nullptr, &borrow);
    ASSERT_EQ(series.borrow_cost.size(), 2u);   // the warmup bar is dropped with the rest
    EXPECT_DOUBLE_EQ(series.borrow_cost[1], 0.0002);
    EXPECT_DOUBLE_EQ(series.cum_pnl_gross[1], 0.0003);   // net 0 + financing added back
    EXPECT_DOUBLE_EQ(series.cum_pnl[1], 0.0);
    const Json::Value json = timeseries_to_json(series);
    ASSERT_EQ(json["borrowCost"].size(), 2u);
    EXPECT_DOUBLE_EQ(json["borrowCost"][0].asDouble(), 0.0001);
    const Json::Value bare = timeseries_to_json(compute_timeseries(
        timestamps, ret, turnover, max_weight, position_count, kFee, 1));
    ASSERT_TRUE(bare.isMember("borrowCost"));
    EXPECT_TRUE(bare["borrowCost"].isArray());
    EXPECT_TRUE(bare["borrowCost"].empty());
}

// Financing follows the book carried into the bar: a short that settles or freezes
// on the bar still paid for the night before it, and a received short child pays
// from the bar after it joins the book.
TEST(BorrowCost, SettledFrozenAndReceivedShortsPayForTheNightsTheyWereHeld) {
    const double per_day = 0.5 * 0.036 / 360.0;
    // Frozen: S1 loses its quote on bars 2-3; the short keeps paying while frozen.
    auto frozen = flat_panel(6, 2);
    frozen.at("close", 2, 1) = std::numeric_limits<float>::quiet_NaN();
    frozen.at("close", 3, 1) = std::numeric_limits<float>::quiet_NaN();
    const auto model = BorrowCostModel::load(borrow_config(0.036), frozen, ".");
    const std::vector<std::vector<float>> signals(frozen.T, {1, -1});
    const auto out = Simulator(frozen, config_for(frozen, 0, &model)).run_from_signals(signals);
    for (unsigned di = 1; di < frozen.T; ++di) EXPECT_NEAR(out.borrow_cost[di], per_day, 1e-15) << di;
    EXPECT_EQ(out.missing_data[2].missing_held_count, 1u);

    // Settled: S1 ends on bar 2 (event on the silent bar). The night into bar 2 is
    // paid, nothing after; the pending settlement is not a market trade.
    auto settled = flat_panel(6, 2);
    for (unsigned di = 2; di < settled.T; ++di) settled.at("close", di, 1) = std::numeric_limits<float>::quiet_NaN();
    settled.settlement_bar = {-1, 2};
    const auto settled_model = BorrowCostModel::load(borrow_config(0.036), settled, ".");
    const auto settled_out = Simulator(settled, config_for(settled, 0, &settled_model)).run_from_signals(signals);
    EXPECT_NEAR(settled_out.borrow_cost[1], per_day, 1e-15);
    EXPECT_NEAR(settled_out.borrow_cost[2], per_day, 1e-15);   // carried into the settlement bar
    EXPECT_EQ(settled_out.missing_data[2].delisting_count, 1u);
    for (unsigned di = 3; di < settled.T; ++di) EXPECT_EQ(settled_out.borrow_cost[di], 0.0) << di;

    // Received: a short parent spins off a child on bar 2; the received short child
    // (-0.2 = -1 * 0.4 * 50 / 100) pays from bar 3 and is bought back at the bar-3 close.
    auto spin = flat_panel(6, 3);
    for (unsigned di = 0; di < spin.T; ++di) spin.at("close", di, 1) = 50.0f;
    DataPanel::CorporateActionLeg leg;
    leg.bar = 2; leg.parent = 0; leg.child = 1; leg.stock_leg = true; leg.child_ratio = 0.4;
    leg.event_type = "spin_off"; leg.value_source = "child_close_ex";
    spin.corporate_actions = {leg};
    const auto spin_model = BorrowCostModel::load(borrow_config(0.036), spin, ".");
    const std::vector<std::vector<float>> short_parent(spin.T, {-1, 0, 1});   // -0.5 parent, +0.5 filler
    const auto spin_out = Simulator(spin, config_for(spin, 0, &spin_model)).run_from_signals(short_parent);
    const double parent_night = 0.5 * 0.036 / 360.0;
    EXPECT_NEAR(spin_out.borrow_cost[2], parent_night, 1e-15);
    EXPECT_NEAR(spin_out.borrow_cost[3], parent_night + 0.5 * 0.4 * 50.0 / 100.0 * 0.036 / 360.0, 1e-15);
    EXPECT_NEAR(spin_out.borrow_cost[4], parent_night, 1e-15);   // the child was bought back on bar 3
    EXPECT_EQ(spin_out.corporate_actions[2].child_positions_created, 1u);
}

TEST(BorrowCost, ParquetTablesAcceptIntegerSymbols) {
    DataFiles files;
    auto panel = flat_panel(4, 2);
    panel.symbols = {"11", "22"};
    arrow::Int64Builder symbols;
    ASSERT_TRUE(symbols.Append(22).ok());
    arrow::TimestampBuilder from(arrow::timestamp(arrow::TimeUnit::NANO), arrow::default_memory_pool());
    ASSERT_TRUE(from.Append(0).ok());
    arrow::Date32Builder to;
    ASSERT_TRUE(to.Append(2).ok());   // 1970-01-03, inclusive
    arrow::DoubleBuilder rate;
    ASSERT_TRUE(rate.Append(0.05).ok());
    std::vector<std::shared_ptr<arrow::Array>> arrays = {
        symbols.Finish().ValueOrDie(), from.Finish().ValueOrDie(), to.Finish().ValueOrDie(), rate.Finish().ValueOrDie()};
    std::vector<std::shared_ptr<arrow::Field>> fields = {
        arrow::field("symbol", arrays[0]->type()), arrow::field("from", arrays[1]->type()),
        arrow::field("to", arrays[2]->type()), arrow::field("rate", arrays[3]->type())};
    auto output = arrow::io::FileOutputStream::Open((files.dir / "ints.parquet").string()).ValueOrDie();
    ASSERT_TRUE(parquet::arrow::WriteTable(*arrow::Table::Make(arrow::schema(fields), arrays),
                                           arrow::default_memory_pool(), output, 2).ok());
    ASSERT_TRUE(output->Close().ok());
    const auto model = BorrowCostModel::load(borrow_config(kNaN, "ints.parquet"), panel, files.dir.string());
    EXPECT_EQ(model.table_rows_matched(), 1u);
    EXPECT_EQ(model.symbols_rated(), 1u);
    EXPECT_TRUE(model.applied());
    EXPECT_DOUBLE_EQ(model.rate(1, 2), 0.05);
    EXPECT_TRUE(std::isnan(model.rate(1, 3)));   // past the inclusive end, no default
    EXPECT_TRUE(std::isnan(model.rate(0, 1)));
    // An interval entirely outside the panel rates no symbol: not applied without a default.
    const auto outside = BorrowCostModel::load(
        borrow_config(kNaN, files.csv("late.csv", "symbol,from,to,rate\n11,1971-01-01,,0.05\n")), panel, files.dir.string());
    EXPECT_EQ(outside.table_rows_matched(), 1u);
    EXPECT_EQ(outside.symbols_rated(), 0u);
    EXPECT_FALSE(outside.applied());
}
