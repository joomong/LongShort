#pragma once

#include "../compiler/dag_builder.h"
#include "../data/data_panel.h"
#include "../engine/simulator.h"
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <algorithm>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

// Materialization of <meta> alpha sets: the EW combined row of every member, as in /api/simulate
// with combinedOnly. Only the combined T x N matrix is kept; member matrices are never stored.
using MetaMatrix = std::vector<std::vector<float>>;

struct MetaRunConfig {
    SimulatorConfig member_config;                    // request settings (dates, price, policy, mask)
    unsigned threads = 1;                             // bounded by the member count
    MissingClosePolicy policy = MissingClosePolicy::Carry;
    const std::vector<uint8_t>* universe_mask = nullptr;
};

// Member-worker tokens shared by every materialization of one request, so concurrent sets
// together never run more than the request's thread count.
class MetaWorkerBudget {
public:
    explicit MetaWorkerBudget(unsigned tokens) : free_(std::max(1u, tokens)) {}
    // Blocks until at least one token is free, then takes up to `wanted` (>= 1).
    unsigned acquire(unsigned wanted);
    void release(unsigned count);

private:
    std::mutex mutex_;
    std::condition_variable available_;
    unsigned free_;
};

// Runs every member's signal rows (no book) in parallel and sums their normalized rows in member
// order. A member failure rethrows as "meta 'NAME' member PATH: <what()>"; EvaluationCancelled passes
// through unchanged. Member pipelines are released after they run. With a budget, the worker
// count is what the budget grants (at least 1, at most min(threads, K)), held until return.
MetaMatrix materialize_meta_set(const DataPanel& data, MetaSet& set, const MetaRunConfig& config,
                                const std::function<bool()>& cancelled, MetaWorkerBudget* budget = nullptr);

// Per-request cache: one matrix per set name, computed once even when many threads ask together.
class MetaSetCache {
public:
    // `threads` bounds the member workers of all materializations of this cache together.
    explicit MetaSetCache(unsigned threads) : budget_(threads) {}
    MetaWorkerBudget& budget() { return budget_; }

    // compute runs at most once per name; concurrent callers wait; a failed compute rethrows to every
    // waiter and is not retried (the request is failing anyway).
    std::shared_ptr<const MetaMatrix> get(const std::string& name, const std::function<MetaMatrix()>& compute);

private:
    struct Entry {
        std::once_flag once;
        std::shared_ptr<const MetaMatrix> matrix;
        std::exception_ptr error;
    };
    MetaWorkerBudget budget_;
    std::mutex mutex_;
    std::map<std::string, std::shared_ptr<Entry>> entries_;
};
