#include <gtest/gtest.h>
#include <atomic>
#include <cstring>
#include <mutex>
#include <random>
#include <set>
#include <thread>
#include "parallel_executor.h"
#include "xml_compiler.h"
#include "simulator_engine.h"
#include "modules/Ffd.h"
#include "modules/TsRank.h"
#include "modules/TsArgExtreme.h"

TEST(ModuleParallelism, DisjointRangesReuseBoundedWorkersAndSmallJobsStaySerial) {
    const auto caller = std::this_thread::get_id();
    AssetParallelScope scope(32);
    std::mutex mutex;
    std::set<std::thread::id> all_ids;
    for (unsigned assets : {2051u, 4099u, 2051u}) {
        std::vector<int> visits(assets);
        parallel_for_assets(assets, 1000, [&](unsigned begin, unsigned end) {
            { std::lock_guard<std::mutex> lock(mutex); all_ids.insert(std::this_thread::get_id()); }
            for (unsigned i = begin; i < end; ++i) ++visits[i];
        });
        EXPECT_EQ(std::count(visits.begin(), visits.end(), 1), assets);
    }
    EXPECT_EQ(all_ids.size(), 4u); // caller plus three reusable workers
    parallel_for_assets(100, 1000, [&](unsigned begin, unsigned end) {
        EXPECT_EQ(begin, 0u); EXPECT_EQ(end, 100u);
        EXPECT_EQ(std::this_thread::get_id(), caller);
    });
    unsigned calls = 0;
    parallel_for_assets(2051, 1, [&](unsigned begin, unsigned end) {
        EXPECT_EQ(begin, 0u); EXPECT_EQ(end, 2051u); ++calls;
    });
    EXPECT_EQ(calls, 1u);
}

TEST(ModuleParallelism, ExceptionsJoinWorkersAndPoolCanBeReused) {
    AssetParallelScope scope(4);
    for (bool worker_fails : {true, false}) {
        std::atomic<unsigned> finished{0};
        EXPECT_THROW(parallel_for_assets(2051, 1000, [&](unsigned begin, unsigned) {
            ++finished;
            if ((begin != 0) == worker_fails) throw std::runtime_error("test failure");
        }), std::runtime_error);
        EXPECT_EQ(finished.load(), 4u);
    }
    std::atomic<unsigned> count{0};
    parallel_for_assets(2051, 1000, [&](unsigned begin, unsigned end) {
        parallel_for_assets(end - begin, 1000, [&](unsigned b, unsigned e) { count += e - b; });
    });
    EXPECT_EQ(count.load(), 2051u);
}

TEST(ModuleParallelism, NestedScopesRestoreTheOuterScope) {
    AssetParallelScope outer(4);
    {
        AssetParallelScope serial(1);
        unsigned calls = 0;
        parallel_for_assets(2051, 1000, [&](unsigned, unsigned) { ++calls; });
        EXPECT_EQ(calls, 1u);
    }
    std::atomic<unsigned> calls{0};
    parallel_for_assets(2051, 1000, [&](unsigned, unsigned) { ++calls; });
    EXPECT_EQ(calls.load(), 4u);
}

namespace {
using History = std::vector<std::vector<float>>;
History history(unsigned T, unsigned N) {
    std::mt19937 rng(713);
    History rows(T, std::vector<float>(N));
    for (unsigned t = 0; t < T; ++t)
        for (unsigned i = 0; i < N; ++i) {
            const unsigned value = rng();
            rows[t][i] = i == 0 || value % 11 == 0 ? std::numeric_limits<float>::quiet_NaN()
                : i == 1 ? 3.0f : static_cast<float>(value % 101) / 8.0f;
            if (i == 2 && t % 17 == 0) rows[t][i] = std::numeric_limits<float>::infinity();
        }
    return rows;
}
History run(Module& module, const History& rows, unsigned threads) {
    AssetParallelScope scope(threads);
    module.numAssets = static_cast<unsigned>(rows[0].size());
    module.init();
    auto result = rows;
    for (unsigned t = 0; t < rows.size(); ++t) {
        SignalVector output{result[t].data(), module.numAssets};
        module.calculate(t, output);
    }
    return result;
}
void expect_bits(const History& actual, const History& expected) {
    ASSERT_EQ(actual.size(), expected.size());
    for (size_t t = 0; t < actual.size(); ++t) {
        ASSERT_EQ(actual[t].size(), expected[t].size());
        ASSERT_EQ(std::memcmp(actual[t].data(), expected[t].data(), actual[t].size() * sizeof(float)), 0)
            << "bar=" << t;
    }
}
}

TEST(ModuleParallelism, ModulesMatchSerialBitsAndResetAcrossAssetCounts) {
    MFfd ffd; ffd.d = FloatParameter(0.4f); ffd.thres = FloatParameter(0.001f);
    MTsRank rank; rank.days = IntParameter(63); rank.min_count = IntParameter(10);
    MTsMax maximum; maximum.days = IntParameter(20); maximum.min_count = IntParameter(5);
    MTsMin minimum; minimum.days = IntParameter(20); minimum.min_count = IntParameter(5);
    MTsArgMax argmax; argmax.days = IntParameter(20); argmax.min_count = IntParameter(5);
    MTsArgMin argmin; argmin.days = IntParameter(20); argmin.min_count = IntParameter(5);
    for (Module* module : std::vector<Module*>{&ffd, &rank, &maximum, &minimum, &argmax, &argmin})
        for (unsigned N : {2051u, 11u, 2051u}) {
            auto rows = history(160, N);
            auto expected = run(*module, rows, 1);
            expect_bits(run(*module, rows, 4), expected);
            expect_bits(run(*module, rows, 1), expected);
            EXPECT_TRUE(std::any_of(expected.back().begin(), expected.back().end(), [](float v) { return std::isfinite(v); }));
        }
}

TEST(ModuleParallelism, SimulatorSignalsAndAccountingStayIdentical) {
    DataPanel data;
    data.T = 160; data.N = 2051;
    data.arrays["feature"] = {};
    const auto rows = history(data.T, data.N);
    for (const auto& row : rows) data.arrays["feature"].insert(data.arrays["feature"].end(), row.begin(), row.end());
    data.arrays["close"].resize(static_cast<size_t>(data.T) * data.N);
    for (unsigned t = 0; t < data.T; ++t) {
        data.timestamps.push_back(static_cast<int64_t>(t) * 86400000LL);
        for (unsigned i = 0; i < data.N; ++i)
            data.arrays["close"][static_cast<size_t>(t) * data.N + i] = 100.0f + static_cast<float>((t + i) % 17);
    }
    for (unsigned i = 0; i < data.N; ++i) data.symbols.push_back(std::to_string(i));
    const std::string xml = R"(<data expr="feature" delay="1"/><tsrank days="63" min_count="10"/>
        <ffd d="0.4" thres="0.001"/><tsmax days="20" min_count="5"/><rank/>)";
    auto alpha = compile_xml(xml, &data);
    SimulatorConfig config{data.T, data.N, 0.0005, true};
    const auto serial = Simulator(data, config).run(alpha[0].pipeline);
    config.module_threads = 4;
    const auto parallel = Simulator(data, config).run(alpha[0].pipeline);
    expect_bits(parallel.signals, serial.signals);
    EXPECT_EQ(parallel.ret, serial.ret);
    EXPECT_EQ(parallel.gross_ret, serial.gross_ret);
    EXPECT_EQ(parallel.turnover, serial.turnover);
    EXPECT_EQ(parallel.position_count, serial.position_count);
    History observed;
    Simulator(data, config).run_signal_rows(alpha[0].pipeline, [&](unsigned, const float* row, unsigned n) {
        observed.emplace_back(row, row + n);
    });
    expect_bits(observed, serial.signals);
    EXPECT_THROW(Simulator(data, config).run_signal_rows(alpha[0].pipeline, [&](unsigned t, const float*, unsigned) {
        if (t == 100) throw std::runtime_error("cancelled");
    }), std::runtime_error);
    expect_bits(Simulator(data, config).run(alpha[0].pipeline).signals, serial.signals);
}
