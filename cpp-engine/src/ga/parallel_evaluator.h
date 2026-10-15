#pragma once
#include <vector>
#include <string>
#include <functional>
#include <unordered_map>
#include <mutex>
#include <thread>
#include <atomic>
#include <stdexcept>

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
