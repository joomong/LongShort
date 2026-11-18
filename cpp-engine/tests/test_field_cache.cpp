#include <gtest/gtest.h>
#include "field_cache.h"
#include <atomic>
#include <chrono>
#include <cstdlib>
#include <new>
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
    EXPECT_THROW(cache.acquire(key("x"), [&calls]() -> FieldBuffer { ++calls; throw std::runtime_error("disk"); }),
                 std::runtime_error);
    EXPECT_EQ(cache.stats().loading_reserved_bytes, 0u);
    const auto x = cache.acquire(key("x"), counting(calls, kFloats, 3.0f));
    EXPECT_EQ(calls.load(), 2u);
    EXPECT_EQ(x->values[0], 3.0f);
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
    const size_t optional_budget_bytes = 3 * kBytes + kBytes / 2;
    FieldCache cache(optional_budget_bytes);
    std::atomic<unsigned> calls{0};
    std::atomic<bool> stop{false};
    std::atomic<bool> violated{false};
    std::thread watcher([&] {
        while (!stop.load()) {
            const auto s = cache.stats();
            if (s.active_bytes + s.idle_bytes + s.loading_reserved_bytes > optional_budget_bytes) violated = true;
        }
    });
    std::vector<std::thread> runs;
    for (int r = 0; r < 4; ++r)
        runs.emplace_back([&, r] {
            for (int i = 0; i < 50; ++i) {
                try {
                    const auto a = cache.acquire(key("f" + std::to_string((r + i) % 6)), counting(calls, kFloats), kBytes);
                    const auto b = cache.acquire(key("f" + std::to_string((r + i + 1) % 6)), counting(calls, kFloats), kBytes);
                } catch (const FieldBudgetExceeded&) {
                }
            }
        });
    for (auto& run : runs) run.join();
    stop = true;
    watcher.join();
    const auto s = cache.stats();
    const size_t active_bytes = s.active_bytes, idle_bytes = s.idle_bytes, loading_reserved_bytes = s.loading_reserved_bytes;
    EXPECT_LE(active_bytes + idle_bytes + loading_reserved_bytes, optional_budget_bytes);
    EXPECT_FALSE(violated.load());
    EXPECT_EQ(active_bytes, 0u);
    EXPECT_EQ(loading_reserved_bytes, 0u);
    EXPECT_GT(s.evictions, 0u);
}

TEST(FieldCache, ExcessRejected) {
    FieldCache cache(2 * kBytes);
    std::atomic<unsigned> calls{0};
    const auto held = cache.acquire(key("x"), counting(calls, 2 * kFloats - 1));  // nearly the whole budget
    bool over_budget_request_reported = false;
    try {
        (void)cache.acquire(key("y"), counting(calls, kFloats), kBytes);  // reservation alone cannot fit
    } catch (const FieldBudgetExceeded& error) {
        over_budget_request_reported = std::string(error.what()).find("budget") != std::string::npos;
    }
    EXPECT_TRUE(over_budget_request_reported);
    EXPECT_EQ(calls.load(), 1u);  // rejected before reading
    // Without a reservation the size is known only after reading; it is still rejected and not kept.
    EXPECT_THROW((void)cache.acquire(key("z"), counting(calls, kFloats)), FieldBudgetExceeded);
    const auto s = cache.stats();
    EXPECT_EQ(s.rejected, 2u);
    EXPECT_EQ(s.active_bytes, held->values.size() * sizeof(float));
    EXPECT_EQ(s.loading_reserved_bytes, 0u);
    EXPECT_LE(s.active_bytes + s.idle_bytes, 2 * kBytes);
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
    EXPECT_EQ(fallback.afl_cache_budget_bytes, std::size_t{16} << 30);
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
