#pragma once

#include "meta_sets.h"
#include "meta_weights.h"
#include <functional>
#include <list>
#include <map>
#include <memory>
#include <mutex>
#include <string>

// Runs every member with Simulator::run under config.member_config and stores its gross daily
// returns (fees excluded) as rows[t][k]; bars before sim_start_index or without a value are NaN.
// Members are not released (the weighted pass re-runs them). Worker, budget, cancellation and
// error wrapping ("meta 'NAME' member PATH: ...") are those of materialize_meta_set.
MemberReturns collect_member_returns(const DataPanel& data, const MetaSet& set, const MetaRunConfig& config,
                                     const std::function<bool()>& cancelled, MetaWorkerBudget* budget = nullptr);

// Process-wide member-return cache: LRU of 16 entries by last use, keyed by a caller-built string.
// One compute per key at a time; concurrent callers wait. A failed compute is not stored and only
// its own caller sees the error: waiters then compute with their own callback (a cancelled request
// must not fail another request waiting on the same key).
class MemberReturnsCache {
public:
    static constexpr std::size_t kCapacity = 16;
    std::shared_ptr<const MemberReturns> get(const std::string& key, const std::function<MemberReturns()>& compute);

private:
    struct Entry {
        bool done = false;
        std::shared_ptr<const MemberReturns> value;
    };
    std::mutex mutex_;
    std::condition_variable changed_;
    std::map<std::string, std::shared_ptr<Entry>> entries_;
    std::list<std::string> lru_;   // front = most recently used completed entry
};
