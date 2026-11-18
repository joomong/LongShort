#include <gtest/gtest.h>
#include "field_cache.h"
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <new>
#include <future>
#include <thread>

// Observes when one watched allocation (a field array) is returned to the allocator.
static std::atomic<const void*> g_watched{nullptr};
static std::atomic<bool> g_watched_freed{false};
void* operator new(std::size_t size) {
    if (void* p = std::malloc(size ? size : 1)) return p;
    throw std::bad_alloc();
}
void operator delete(void* p) noexcept {
    if (p && p == g_watched.load()) g_watched_freed = true;
    std::free(p);
}
void operator delete(void* p, std::size_t) noexcept { operator delete(p); }

namespace {
FieldKey key(const std::string& field, const std::string& symbols = "A,B") {
    return FieldKey{"v3", field, "d0..d1", symbols};
}
// A loader that counts its calls and builds a buffer of `count` floats valued `value`.
FieldCache::Loader counting(std::atomic<unsigned>& calls, size_t count, float value = 1.0f) {
    return [&calls, count, value] {
        ++calls;
        FieldBuffer buffer;
        buffer.values.assign(count, value);
        return buffer;
    };
}
constexpr size_t kFloats = 25;            // 100 bytes per field
constexpr size_t kBytes = kFloats * sizeof(float);
}

TEST(FieldCache, Reuse) {
    FieldCache cache(10 * kBytes);
    std::atomic<unsigned> calls{0};
    {   // run A = {x, y}
        const auto x = cache.acquire(key("x"), counting(calls, kFloats));
        const auto y = cache.acquire(key("y"), counting(calls, kFloats));
    }
    {   // run B = {y, z}: y comes from the cache
        const auto y = cache.acquire(key("y"), counting(calls, kFloats));
        const auto z = cache.acquire(key("z"), counting(calls, kFloats));
    }
    const auto loads_after_A_and_B = cache.stats().loads;
    EXPECT_EQ(loads_after_A_and_B, 3u); // A={x,y}, B={y,z}
    EXPECT_EQ(calls.load(), 3u);
    // A different symbol axis is a different field buffer.
    (void)cache.acquire(key("y", "A,C"), counting(calls, kFloats));
    EXPECT_EQ(cache.stats().loads, 4u);
}

TEST(FieldCache, SingleFlight) {
    FieldCache cache(10 * kBytes);
    std::atomic<unsigned> calls{0};
    std::atomic<bool> go{false};
    const auto slow = [&calls] {
        ++calls;
        std::this_thread::sleep_for(std::chrono::milliseconds(50));  // others arrive while it loads
        FieldBuffer buffer;
        buffer.values.assign(kFloats, 7.0f);
        return buffer;
    };
    constexpr int kThreads = 16;
    std::vector<std::shared_ptr<const FieldBuffer>> got(kThreads);
    std::vector<std::thread> threads;
    for (int i = 0; i < kThreads; ++i)
        threads.emplace_back([&, i] {
            while (!go.load()) std::this_thread::yield();
            got[i] = cache.acquire(key("x"), slow);
        });
    go = true;
    for (auto& thread : threads) thread.join();
    const unsigned concurrent_same_key_loads = calls.load();
    EXPECT_EQ(concurrent_same_key_loads, 1u);
    EXPECT_EQ(cache.stats().loads, 1u);
    for (const auto& buffer : got) {
        ASSERT_TRUE(buffer);
        EXPECT_EQ(buffer.get(), got[0].get());
    }
    EXPECT_EQ(cache.stats().active_bytes, kBytes);
}

TEST(FieldCacheLifecycle, FailedLoadIsRemovedAndRetried) {
    FieldCache cache(10 * kBytes);
    std::atomic<unsigned> calls{0};
    EXPECT_THROW(cache.acquire(key("x"), [&calls]() -> FieldBuffer { ++calls; throw std::runtime_error("disk"); }, kBytes),
                 std::runtime_error);
    EXPECT_EQ(cache.stats().loading_reserved_bytes, 0u);
    EXPECT_EQ(cache.stats().failures, 1u);
    const auto x = cache.acquire(key("x"), counting(calls, kFloats, 3.0f), kBytes);
    EXPECT_EQ(calls.load(), 2u);
    EXPECT_EQ(x->values[0], 3.0f);
    EXPECT_EQ(cache.stats().loading_reserved_bytes, 0u);
    EXPECT_EQ(cache.stats().peak_loading_reserved_bytes, kBytes);
}

TEST(FieldCacheLifecycle, WaitersOfAFailedLoadAllSeeTheFailure) {
    FieldCache cache(10 * kBytes);
    std::atomic<unsigned> calls{0};
    std::atomic<bool> started{false};
    const auto failing = [&]() -> FieldBuffer {
        ++calls;
        started = true;
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
        throw std::runtime_error("disk");
    };
    std::vector<std::future<bool>> results;
    results.push_back(std::async(std::launch::async, [&] {
        try { (void)cache.acquire(key("x"), failing, kBytes); return false; } catch (const std::runtime_error&) { return true; }
    }));
    while (!started) std::this_thread::yield();
    for (int i = 0; i < 6; ++i)
        results.push_back(std::async(std::launch::async, [&] {
            try { (void)cache.acquire(key("x"), failing, kBytes); return false; } catch (const std::runtime_error&) { return true; }
        }));
    for (auto& result : results) {
        ASSERT_EQ(result.wait_for(std::chrono::seconds(10)), std::future_status::ready);
        EXPECT_TRUE(result.get());
    }
    const auto s = cache.stats();
    EXPECT_LE(calls.load(), 7u);
    EXPECT_EQ(s.loading_reserved_bytes, 0u);
    EXPECT_EQ(s.active_bytes + s.idle_bytes, 0u);
    (void)cache.acquire(key("x"), counting(calls, kFloats), kBytes);  // retried afterwards
}

TEST(FieldCacheLifecycle, AllocationFailuresLeaveNoWaiterLeakOrDeadlock) {
    for (const char* site : {"pin", "entry", "publish", "adopt"}) {
        SCOPED_TRACE(site);
        static std::atomic<const char*> armed_site{nullptr};
        armed_site = site;
        field_cache_testing::fault = [](const char* at) {
            const char* expected = armed_site.load();
            if (expected && std::string(at) == expected && armed_site.compare_exchange_strong(expected, nullptr))
                throw std::bad_alloc();
        };
        FieldCache cache(10 * kBytes);
        std::atomic<bool> started{false};
        const auto slow = [&] {
            started = true;
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
            FieldBuffer buffer;
            buffer.values.assign(kFloats, 1.0f);
            return buffer;
        };
        if (std::string(site) == "adopt") {
            FieldBuffer buffer;
            buffer.values.assign(kFloats, 1.0f);
            EXPECT_THROW((void)cache.adopt(buffer), std::bad_alloc);
        } else {
            std::vector<std::future<void>> runs;
            std::atomic<bool> first_done{false};
            runs.push_back(std::async(std::launch::async, [&] {
                struct Done { std::atomic<bool>& flag; ~Done() { flag = true; } } done{first_done};
                (void)cache.acquire(key("x"), slow, kBytes);
            }));
            for (int i = 0; i < 4; ++i)
                runs.push_back(std::async(std::launch::async, [&] {
                    while (!started && !first_done) std::this_thread::yield();
                    (void)cache.acquire(key("x"), slow, kBytes);
                }));
            unsigned failed = 0;
            for (auto& run : runs) {
                ASSERT_EQ(run.wait_for(std::chrono::seconds(10)), std::future_status::ready);  // no waiter left behind
                try { run.get(); } catch (const std::bad_alloc&) { ++failed; } catch (const std::exception&) { ++failed; }
            }
            EXPECT_GE(failed, 1u);
        }
        field_cache_testing::fault = nullptr;
        const auto s = cache.stats();
        EXPECT_EQ(s.active_bytes, 0u);
        EXPECT_EQ(s.loading_reserved_bytes, 0u);
        EXPECT_LE(s.idle_bytes, kBytes);
        const auto again = cache.acquire(key("x"), slow, kBytes);  // usable afterwards
        EXPECT_EQ(cache.stats().active_bytes, kBytes);
    }
}

TEST(FieldCacheLifecycle, AdoptedBufferIsActiveUntilReleasedAndNeverIdle) {
    FieldCache cache(10 * kBytes);
    FieldBuffer buffer;
    buffer.values.assign(kFloats, 2.0f);
    auto held = cache.adopt(std::move(buffer));
    EXPECT_EQ(cache.stats().active_bytes, kBytes);
    held.reset();
    EXPECT_EQ(cache.stats().active_bytes, 0u);
    EXPECT_EQ(cache.stats().idle_bytes, 0u);
}

TEST(FieldCacheLifecycle, BytesCountCoordinates) {
    FieldBuffer buffer;
    buffer.values.assign(6, 0.0f);
    buffer.timestamps = {1, 2};
    buffer.symbols = {"AB", "CDE", "F"};
    EXPECT_EQ(buffer.bytes(), 6 * sizeof(float) + 2 * sizeof(int64_t) + 6);
}

TEST(FieldCacheLifecycle, ConcurrentRequestsWithinTheirOwnBudgetAllSucceed) {
    // 16 requests, each holding 2 fields (within its own budget of 3); together 32 fields over a budget of 3.
    const size_t budget = 3 * kBytes;
    FieldCache cache(budget);
    std::atomic<unsigned> calls{0};
    std::atomic<int> ready{0};
    constexpr int kRequests = 16;
    std::vector<std::future<bool>> results;
    for (int r = 0; r < kRequests; ++r)
        results.push_back(std::async(std::launch::async, [&, r] {
            const auto run = std::make_shared<FieldRun>();
            try {
                const auto a = cache.acquire(key("a" + std::to_string(r)), counting(calls, kFloats), kBytes, run);
                const auto b = cache.acquire(key("b" + std::to_string(r)), counting(calls, kFloats), kBytes, run);
                ++ready;
                while (ready.load() < kRequests) std::this_thread::yield();  // all hold their fields together
                return run->bytes == 2 * kBytes;
            } catch (const FieldBudgetExceeded&) {
                ++ready;
                return false;
            }
        }));
    for (auto& result : results) EXPECT_TRUE(result.get());
    const auto s = cache.stats();
    EXPECT_EQ(s.rejected, 0u);
    EXPECT_GT(s.over_budget_events, 0u);
    EXPECT_GE(s.peak_optional_bytes, size_t{kRequests} * 2 * kBytes);
    EXPECT_EQ(s.active_bytes, 0u);
    EXPECT_LE(s.idle_bytes, budget);  // idle trimmed back to the budget
}

TEST(FieldCache, PinnedAlive) {
    FieldCache cache(2 * kBytes);
    const std::vector<float> resident(1000, 5.0f);  // a base input, process lifetime
    cache.set_resident_bytes(resident.size() * sizeof(float));
    std::atomic<unsigned> calls{0};
    const auto x = cache.acquire(key("x"), counting(calls, kFloats, 9.0f));
    {
        const auto y = cache.acquire(key("y"), counting(calls, kFloats));
    }
    cache.set_budget(0);  // y is idle and goes; x is held by a run and stays
    const auto s = cache.stats();
    const bool resident_alive = s.resident_bytes == resident.size() * sizeof(float) && resident[999] == 5.0f;
    const bool active_alive = x->values.size() == kFloats && x->values[kFloats - 1] == 9.0f && s.active_bytes == kBytes;
    EXPECT_TRUE(resident_alive && active_alive);
    EXPECT_EQ(s.idle_bytes, 0u);
    EXPECT_GE(s.evictions, 1u);
}

TEST(FieldCache, LastReference) {
    FieldCache cache(10 * kBytes);
    const size_t resident_bytes = 4096;
    cache.set_resident_bytes(resident_bytes);
    std::atomic<unsigned> calls{0};
    {
        auto run_one = cache.acquire(key("x"), counting(calls, kFloats));
        auto run_two = cache.acquire(key("x"), counting(calls, kFloats));
        g_watched = run_one->values.data();
        g_watched_freed = false;
        run_one.reset();
        EXPECT_EQ(cache.stats().active_bytes, kBytes);  // run_two still holds it
    }
    EXPECT_EQ(cache.stats().active_bytes, 0u);
    EXPECT_EQ(cache.stats().idle_bytes, kBytes);
    EXPECT_FALSE(g_watched_freed.load());  // the cache's own reference keeps the idle buffer
    cache.set_budget(0);
    const auto s = cache.stats();
    const size_t bytes_after_last_ref_and_eviction = s.resident_bytes + s.active_bytes + s.idle_bytes;
    EXPECT_EQ(bytes_after_last_ref_and_eviction, resident_bytes);
    EXPECT_TRUE(g_watched_freed.load());   // array bytes returned
    g_watched = nullptr;
    EXPECT_EQ(s.evictions, 1u);
    // Reacquiring after eviction reloads.
    (void)cache.acquire(key("x"), counting(calls, 0));
    EXPECT_EQ(calls.load(), 2u);
}

TEST(FieldCache, TotalOptionalBudget) {
    // The budget is soft for concurrent requests; idle fields are trimmed so that, once requests release,
    // active + idle + reserved is back within it.
    const size_t optional_budget_bytes = 3 * kBytes + kBytes / 2;
    FieldCache cache(optional_budget_bytes);
    std::atomic<unsigned> calls{0};
    std::vector<std::thread> runs;
    for (int r = 0; r < 4; ++r)
        runs.emplace_back([&, r] {
            for (int i = 0; i < 50; ++i) {
                const auto run = std::make_shared<FieldRun>();
                const auto a = cache.acquire(key("f" + std::to_string((r + i) % 6)), counting(calls, kFloats), kBytes, run);
                const auto b = cache.acquire(key("f" + std::to_string((r + i + 1) % 6)), counting(calls, kFloats), kBytes, run);
            }
        });
    for (auto& run : runs) run.join();
    const auto s = cache.stats();
    const size_t active_bytes = s.active_bytes, idle_bytes = s.idle_bytes, loading_reserved_bytes = s.loading_reserved_bytes;
    EXPECT_LE(active_bytes + idle_bytes + loading_reserved_bytes, optional_budget_bytes);
    EXPECT_EQ(active_bytes, 0u);
    EXPECT_EQ(loading_reserved_bytes, 0u);
    EXPECT_GT(s.evictions, 0u);
    EXPECT_EQ(s.rejected, 0u);
    // Sequentially, without concurrent holders, the total never exceeds the budget.
    FieldCache serial(optional_budget_bytes);
    for (int i = 0; i < 20; ++i) {
        const auto run = std::make_shared<FieldRun>();
        const auto a = serial.acquire(key("f" + std::to_string(i % 6)), counting(calls, kFloats), kBytes, run);
        const auto b = serial.acquire(key("f" + std::to_string((i + 1) % 6)), counting(calls, kFloats), kBytes, run);
    }
    EXPECT_EQ(serial.stats().over_budget_events, 0u);
    EXPECT_LE(serial.stats().peak_optional_bytes, optional_budget_bytes);
}

TEST(FieldCache, ExcessRejected) {
    FieldCache cache(2 * kBytes);
    std::atomic<unsigned> calls{0};
    const auto other = cache.acquire(key("o"), counting(calls, 2 * kFloats), 0, std::make_shared<FieldRun>());
    const auto run = std::make_shared<FieldRun>();
    const auto held = cache.acquire(key("x"), counting(calls, kFloats + 1), 0, run);  // another request's bytes do not count
    bool over_budget_request_reported = false;
    try {
        (void)cache.acquire(key("y"), counting(calls, kFloats), kBytes, run);  // this request alone would exceed
    } catch (const FieldBudgetExceeded& error) {
        const std::string what = error.what();
        over_budget_request_reported = what.find("budget") != std::string::npos && what.find("request") != std::string::npos &&
                                       what.find("idle") != std::string::npos && what.find("reserved") != std::string::npos;
    }
    EXPECT_TRUE(over_budget_request_reported);
    EXPECT_EQ(calls.load(), 2u);  // rejected before reading
    // Without a reservation the size is known only after reading; it is still rejected and not kept.
    EXPECT_THROW((void)cache.acquire(key("z"), counting(calls, kFloats), 0, run), FieldBudgetExceeded);
    // A cached field that would put this request over is refused as well.
    EXPECT_THROW((void)cache.acquire(key("o"), counting(calls, 0), 0, run), FieldBudgetExceeded);
    const auto s = cache.stats();
    EXPECT_EQ(s.rejected, 3u);
    EXPECT_EQ(run->bytes, held->values.size() * sizeof(float));
    EXPECT_EQ(s.loading_reserved_bytes, 0u);
    EXPECT_EQ(s.active_bytes, other->bytes() + held->bytes());
}

// ---- Engine: run panels read AFL through the panel's FieldCache ----
#include "fixtures/cache_fixture.h"
#include "loader.h"
#include "simulation_data.h"

namespace {
using namespace cache_fixture;
struct AflCache {
    ThinCache cache;
    AflCache() {
        std::filesystem::create_directories(cache.dir / "afl");
        cache.write("afl/4.parquet", {"101", "102", "__index_level_0__"},
            {doubles({1.0, 2.0, 3.0}), doubles({4.0, kNaN, 6.0}), stamps({at(0), at(2), at(5)})});
        cache.write("afl/5.parquet", {"101", "102", "__index_level_0__"},
            {doubles({7.0, 8.0, 9.0}), doubles({1.0, 2.0, 3.0}), stamps({at(0), at(2), at(5)})});
        std::ofstream(cache.dir / "catalog.json") <<
            R"({"schema_version":3,"value_semantics":"observations","alignment":"exact",)"
            R"("fields":{"ROE":"4.parquet","ROE_alias":"4.parquet","BP":"5.parquet"}})";
    }
    DataPanel load(const std::string& extra = "") const {
        auto config = BacktestConfig::from_yaml(cache.config(top500, "afl: {path: afl, catalog: catalog.json" + extra + "}\n"));
        config.end_date = "2020-01-10";
        return DataPanel::load(config);
    }
};
void read_all(const DataPanel& run, const char* field) {
    for (unsigned t = 0; t < run.T; ++t) (void)run.row(field, t);
}
}

TEST(FieldCacheLifecycle, RunPanelsReuseAndReleaseAflFields) {
    AflCache afl;
    const auto parent = afl.load();
    ASSERT_TRUE(parent.field_cache);
    EXPECT_GT(parent.field_cache->stats().resident_bytes, 0u);
    RunPanelCache runs(parent);
    float roe = 0;
    {
        const auto run = runs.get("", 0, parent.T);
        read_all(*run, "AFL.ROE");
        read_all(*run, "AFL.ROE_alias");
        roe = run->at("AFL.ROE", 5, index_of(*run, "101"));
        const auto s = parent.field_cache->stats();
        EXPECT_EQ(s.loads, 1u);
        EXPECT_GT(s.active_bytes, 0u);
    }
    EXPECT_FLOAT_EQ(roe, 3.0f);
    EXPECT_EQ(parent.field_cache->stats().active_bytes, 0u);  // the run ended: idle
    EXPECT_GT(parent.field_cache->stats().idle_bytes, 0u);
    {
        const auto run = runs.get("", 0, parent.T);  // same panel, fields re-pinned from the cache
        read_all(*run, "AFL.ROE");
        read_all(*run, "AFL.BP");
        EXPECT_EQ(parent.field_cache->stats().loads, 2u);  // only BP was read
        const auto other = make_run_panel(parent, "", 0, parent.T);  // same coordinates, another panel
        read_all(*other, "AFL.ROE");
        EXPECT_EQ(parent.field_cache->stats().loads, 2u);
    }
    EXPECT_FALSE(parent.lazy_columns.at("AFL.ROE")->loaded());
    parent.field_cache->set_budget(0);
    EXPECT_EQ(parent.field_cache->stats().idle_bytes, 0u);
    EXPECT_EQ(parent.field_cache->stats().evictions, 2u);
    {
        const auto run = runs.get("", 0, parent.T);
        parent.field_cache->set_budget(std::size_t{1} << 30);
        read_all(*run, "AFL.ROE");
        EXPECT_EQ(parent.field_cache->stats().loads, 3u);  // evicted, so read again
        EXPECT_FLOAT_EQ(run->at("AFL.ROE", 5, index_of(*run, "101")), 3.0f);
    }
}

TEST(FieldCacheLifecycle, BudgetComesFromConfig) {
    AflCache afl;
    const auto config = BacktestConfig::from_yaml(afl.cache.config(top500, "afl: {path: afl, catalog: catalog.json, cache_budget_gib: 0.5}\n"));
    EXPECT_EQ(config.afl_cache_budget_bytes, std::size_t{1} << 29);
    const auto fallback = BacktestConfig::from_yaml(afl.cache.config(top500, "afl: {path: afl, catalog: catalog.json}\n"));
    EXPECT_EQ(fallback.afl_cache_budget_bytes, std::size_t{32} << 30);
    EXPECT_EQ(afl.load(", cache_budget_gib: 0.5").field_cache->stats().budget_bytes, std::size_t{1} << 29);
    EXPECT_THROW(BacktestConfig::from_yaml(afl.cache.config(top500, "afl: {path: afl, catalog: catalog.json, cache_budget_gib: -1}\n")),
                 std::runtime_error);
}

TEST(FieldCacheLifecycle, OverBudgetRunFailsClearly) {
    AflCache afl;
    const auto parent = afl.load();
    parent.field_cache->set_budget(1);
    const auto run = make_run_panel(parent, "", 0, parent.T);
    EXPECT_THROW((void)run->row("AFL.ROE", 0), FieldBudgetExceeded);
    parent.field_cache->set_budget(std::size_t{1} << 30);
    EXPECT_FLOAT_EQ(run->at("AFL.ROE", 5, index_of(*run, "101")), 3.0f);  // retried after the failure
}

TEST(FieldCacheLifecycle, RewrittenFileSameKeyDifferentData) {
    AflCache afl;
    const auto parent = afl.load();
    {
        const auto run = make_run_panel(parent, "", 0, parent.T);
        EXPECT_FLOAT_EQ(run->at("AFL.ROE", 5, index_of(*run, "101")), 3.0f);
    }
    // Publish new values of the same size right away: written beside, renamed over the old file.
    afl.cache.write("afl/4.next.parquet", {"101", "102", "__index_level_0__"},
        {doubles({1.0, 2.0, 30.0}), doubles({4.0, kNaN, 6.0}), stamps({at(0), at(2), at(5)})});
    std::filesystem::rename(afl.cache.dir / "afl/4.next.parquet", afl.cache.dir / "afl/4.parquet");
    const auto run = make_run_panel(parent, "", 0, parent.T);
    EXPECT_FLOAT_EQ(run->at("AFL.ROE", 5, index_of(*run, "101")), 30.0f);
    EXPECT_EQ(parent.field_cache->stats().loads, 2u);
}

TEST(FieldCacheLifecycle, SymlinkSwapIsANewKey) {
    AflCache afl;
    afl.cache.write("afl/4.v2.parquet", {"101", "102", "__index_level_0__"},
        {doubles({1.0, 2.0, 40.0}), doubles({4.0, kNaN, 6.0}), stamps({at(0), at(2), at(5)})});
    std::filesystem::rename(afl.cache.dir / "afl/4.parquet", afl.cache.dir / "afl/4.v1.parquet");
    std::filesystem::create_symlink("4.v1.parquet", afl.cache.dir / "afl/4.parquet");
    const auto parent = afl.load();
    {
        const auto run = make_run_panel(parent, "", 0, parent.T);
        EXPECT_FLOAT_EQ(run->at("AFL.ROE", 5, index_of(*run, "101")), 3.0f);
    }
    std::filesystem::create_symlink("4.v2.parquet", afl.cache.dir / "afl/4.link");
    std::filesystem::rename(afl.cache.dir / "afl/4.link", afl.cache.dir / "afl/4.parquet");
    const auto run = make_run_panel(parent, "", 0, parent.T);
    EXPECT_FLOAT_EQ(run->at("AFL.ROE", 5, index_of(*run, "101")), 40.0f);
}

TEST(FieldCacheLifecycle, LoadedParentProjectionIsCountedActiveAndResident) {
    AflCache afl;
    auto parent = afl.load();
    const size_t resident_before = parent.field_cache->stats().resident_bytes;
    (void)parent.at("AFL.ROE", 0, 0);  // loads the parent column for the process lifetime
    EXPECT_EQ(parent.field_cache->stats().resident_bytes,
              resident_before + size_t{parent.T} * parent.N * sizeof(float));
    {
        const auto run = make_run_panel(parent, "", 0, parent.T);
        read_all(*run, "AFL.ROE");
        EXPECT_GT(parent.field_cache->stats().active_bytes, 0u);
        EXPECT_EQ(parent.field_cache->stats().loads, 0u);  // projected, not read
    }
    EXPECT_EQ(parent.field_cache->stats().active_bytes, 0u);
    EXPECT_EQ(parent.field_cache->stats().idle_bytes, 0u);
}
