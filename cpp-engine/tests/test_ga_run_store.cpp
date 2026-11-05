#include <gtest/gtest.h>

#include <thread>

#include "ga_run_store.h"

namespace {
GaCandidateEntry entry_of(std::size_t returns) {
    GaCandidateEntry entry;
    entry.xml = "<data/>";
    entry.net_returns.assign(returns, 0.5);
    return entry;
}
const std::size_t kEntry = entry_of(1000).bytes();
const std::size_t kRun = 4096;
bool has(const GaRunStore& store, const std::string& run, std::size_t id) {
    return store.find(run, id).entry != nullptr;
}
}  // namespace

TEST(GaRunStore, ReturnsWhatWasStoredAndNothingElse) {
    GaRunStore store(1 << 20);
    GaRunInfo info;
    info.sim_end_index = 7;
    const auto run = store.begin_run(info);
    EXPECT_EQ(run.size(), 32u);
    EXPECT_NE(run, store.begin_run(info));
    store.put(run, 3, entry_of(1000));
    const auto found = store.find(run, 3);
    ASSERT_TRUE(found.entry);
    EXPECT_EQ(found.entry->net_returns.size(), 1000u);
    EXPECT_EQ(found.run->sim_end_index, 7u);
    EXPECT_FALSE(has(store, run, 4));
    EXPECT_FALSE(has(store, "unknown", 3));
}

TEST(GaRunStore, EvictsWholeOldestFinishedRunsFirst) {
    GaRunStore store(3 * kRun + 4 * kEntry);
    const auto old_finished = store.begin_run({});
    const auto streaming = store.begin_run({});
    const auto new_finished = store.begin_run({});
    store.put(streaming, 0, entry_of(1000));
    store.put(old_finished, 0, entry_of(1000));
    store.put(new_finished, 0, entry_of(1000));
    store.finish_run(old_finished);
    store.finish_run(new_finished);
    store.put(streaming, 1, entry_of(1000));
    EXPECT_EQ(store.bytes(), 3 * kRun + 4 * kEntry);
    store.put(streaming, 2, entry_of(1000));
    EXPECT_FALSE(has(store, old_finished, 0));
    EXPECT_TRUE(has(store, new_finished, 0));
    EXPECT_TRUE(has(store, streaming, 0));
    store.put(streaming, 3, entry_of(1000));
    EXPECT_FALSE(has(store, new_finished, 0));
    EXPECT_TRUE(has(store, streaming, 0));
    EXPECT_TRUE(has(store, streaming, 3));
    EXPECT_LE(store.bytes(), 3 * kRun + 4 * kEntry);
}

TEST(GaRunStore, AStreamingRunGoesLastAndWholeAndStaysGone) {
    GaRunStore store(2 * kRun + 2 * kEntry);
    const auto older = store.begin_run({});
    const auto newer = store.begin_run({});
    store.put(older, 0, entry_of(1000));
    store.put(newer, 0, entry_of(1000));
    store.put(newer, 1, entry_of(1000));
    EXPECT_FALSE(has(store, older, 0));
    EXPECT_TRUE(has(store, newer, 1));
    store.put(older, 1, entry_of(1000));
    EXPECT_FALSE(has(store, older, 1));
    store.put(newer, 2, entry_of(1000));
    store.put(newer, 3, entry_of(1000));
    store.put(newer, 4, entry_of(1000));
    store.put(newer, 5, entry_of(1000));
    EXPECT_FALSE(has(store, newer, 0));
    EXPECT_FALSE(has(store, newer, 5));
    EXPECT_EQ(store.bytes(), 0u);
}

TEST(GaRunStore, AReaderKeepsItsEntryAcrossEvictionAndEmptyRunsCount) {
    GaRunStore store(kRun + kEntry);
    const auto run = store.begin_run({});
    store.put(run, 0, entry_of(1000));
    const auto held = store.find(run, 0);
    store.finish_run(run);
    (void)store.begin_run({});
    EXPECT_FALSE(has(store, run, 0));
    ASSERT_TRUE(held.entry);
    EXPECT_EQ(held.entry->net_returns.size(), 1000u);
    GaRunStore small(3 * kRun);
    for (int i = 0; i < 100; ++i) small.finish_run(small.begin_run({}));
    EXPECT_LE(small.bytes(), 3 * kRun);
}

TEST(GaRunStore, ConcurrentWritersStayUnderTheCap) {
    const std::size_t cap = 4 * (kRun + 10 * kEntry);
    GaRunStore store(cap);
    std::vector<std::thread> threads;
    for (int t = 0; t < 8; ++t)
        threads.emplace_back([&store] {
            for (int r = 0; r < 20; ++r) {
                const auto run = store.begin_run({});
                for (std::size_t id = 0; id < 20; ++id) {
                    store.put(run, id, entry_of(1000));
                    (void)store.find(run, id);
                }
                store.finish_run(run);
            }
        });
    for (auto& thread : threads) thread.join();
    EXPECT_LE(store.bytes(), cap);
}
