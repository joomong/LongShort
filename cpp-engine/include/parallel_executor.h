#pragma once

#include <atomic>
#include <cstddef>
#include <functional>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

// One run owns at most min(threads, 4) execution lanes, including its caller.
// Workers are created lazily and reused across dates and eligible modules.
class AssetParallelScope {
public:
    explicit AssetParallelScope(unsigned threads);
    ~AssetParallelScope();
    AssetParallelScope(const AssetParallelScope&) = delete;
    AssetParallelScope& operator=(const AssetParallelScope&) = delete;

private:
    struct State;
    std::unique_ptr<State> state_;
    AssetParallelScope* previous_;
    void run(unsigned assets, std::size_t work_per_asset,
             const std::function<void(unsigned, unsigned)>& task);
    friend void parallel_for_assets(unsigned, std::size_t,
                                    const std::function<void(unsigned, unsigned)>&);
};

// No scope, one lane, or a small workload executes synchronously. Ranges are
// disjoint [begin, end); all finish before return, including when a task throws.
void parallel_for_assets(unsigned assets, std::size_t work_per_asset,
                         const std::function<void(unsigned, unsigned)>& task);

class EvaluationCancelled : public std::runtime_error {
public:
    EvaluationCancelled() : std::runtime_error("Evaluation cancelled") {}
};

struct EvaluationResult {
    double fitness = 0.0;
    std::string error;
};

class ParallelEvaluator {
public:
    using EvalFunc = std::function<double(const std::string& xml)>;

    ParallelEvaluator(unsigned n_threads, EvalFunc eval_fn);

    // Evaluate a batch of XML strings, return fitnesses in the same order.
    // Uses a hash cache to skip duplicate XMLs.
    std::vector<double> evaluate_batch(const std::vector<std::string>& xmls);
    using Completion = std::function<void(size_t, const EvaluationResult&)>;
    using Cancelled = std::function<bool()>;
    // Completion callbacks run on the calling thread, never on a worker.
    std::vector<EvaluationResult> evaluate_results(
        const std::vector<std::string>& xmls, Completion completed = {}, Cancelled cancelled = {});

    size_t cache_hits() const { return cache_hits_.load(); }

private:
    unsigned  n_threads_;
    EvalFunc  eval_fn_;
    std::unordered_map<std::string, EvaluationResult> cache_;
    std::mutex cache_mutex_;
    std::atomic<size_t> cache_hits_{0};
};
