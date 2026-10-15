#pragma once

#include "../compiler/dag_builder.h"
#include "../data/data_panel.h"
#include "../engine/simulator.h"
#include "meta_weights.h"
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

// Weighted pass for several weight matrices at once: one run per member; member k's normalized row at
// bar t enters output c as float(row * weights[c]->rows[t][k]), summed in member order. Worker, budget,
// cancellation, error wrapping and member release are those of materialize_meta_set.
std::vector<MetaMatrix> materialize_weighted(const DataPanel& data, MetaSet& set, const MetaRunConfig& config,
                                             const std::vector<const CombineWeights*>& weights,
                                             const std::function<bool()>& cancelled,
                                             MetaWorkerBudget* budget = nullptr);

// MetaSetCache key: name, member paths, combine spec key.
std::string meta_set_key(const MetaSet& set, const CombineSpec& spec);

struct MetaCombineInfo {
    unsigned fallback_bars = 0;
    unsigned first_estimated_bar = 0;
};

// Per-request cache: one matrix per set key, computed once even when many threads ask together.
class MetaSetCache {
public:
    // `threads` bounds the member workers of all materializations of this cache together.
    explicit MetaSetCache(unsigned threads) : budget_(threads) {}
    MetaWorkerBudget& budget() { return budget_; }

    // compute runs at most once per name; concurrent callers wait; a failed compute rethrows to every
    // waiter and is not retried (the request is failing anyway).
    std::shared_ptr<const MetaMatrix> get(const std::string& name, const std::function<MetaMatrix()>& compute);

    // Prefetch support: store a computed matrix or its error (a no-op when the key is already done),
    // test for a key, drop keys (non-EW matrices of a finished GA round).
    void put(const std::string& key, std::shared_ptr<const MetaMatrix> matrix, std::exception_ptr error);
    bool contains(const std::string& key);
    void evict(const std::vector<std::string>& keys);

    // Weight diagnostics of non-EW keys, reported in the response.
    void set_info(const std::string& key, MetaCombineInfo info);
    bool info(const std::string& key, MetaCombineInfo* out);

    // Work done for this cache: weighted or EW passes, and member runs (returns and weighted passes).
    struct Stats {
        std::size_t materializations = 0;
        std::size_t member_runs = 0;
    };
    void record(std::size_t materializations, std::size_t member_runs);
    Stats stats();

private:
    struct Entry {
        std::once_flag once;
        std::shared_ptr<const MetaMatrix> matrix;
        std::exception_ptr error;
    };
    MetaWorkerBudget budget_;
    std::mutex mutex_;
    std::map<std::string, std::shared_ptr<Entry>> entries_;
    std::map<std::string, MetaCombineInfo> infos_;
    Stats stats_;
};

class MemberReturnsCache;

// Everything the non-EW passes need besides the set: `settings` is the request part of the member-return
// key (universe, sim start/end, execution price, missing-close policy).
struct MetaCombineContext {
    const DataPanel& data;
    MetaRunConfig config;
    std::string settings;
    MemberReturnsCache& returns;
    MetaSetCache& cache;
    std::function<bool()> cancelled;
};

// Lazy path for one non-EW set: (1) member returns through the returns cache, (2) the weights of
// set.combine, (3) one weighted pass. Records the weight info under meta_set_key(set, set.combine).
// A spec error throws std::invalid_argument("meta 'NAME' <tag>: ...").
MetaMatrix materialize_combined(const MetaCombineContext& context, MetaSet& set);

// Batch path: groups the non-EW sets of `alphas` by name and members, and for every group whose specs
// are not cached yet runs (1) once, (2) per distinct spec and (3) once for all of them, then stores each
// matrix (or its error) in the cache. Returns the keys stored. EW-only sets stay lazy.
std::vector<std::string> prefetch_meta_sets(const MetaCombineContext& context, std::vector<CompiledAlpha>& alphas);
