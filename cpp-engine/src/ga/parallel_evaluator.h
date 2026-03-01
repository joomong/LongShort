#pragma once
#include <vector>
#include <string>
#include <functional>
#include <unordered_map>
#include <mutex>
#include <thread>

class ParallelEvaluator {
public:
    using EvalFunc = std::function<double(const std::string& xml)>;

    ParallelEvaluator(unsigned n_threads, EvalFunc eval_fn);

    // Evaluate a batch of XML strings, return fitnesses in the same order.
    // Uses a hash cache to skip duplicate XMLs.
    std::vector<double> evaluate_batch(const std::vector<std::string>& xmls);

    size_t cache_hits() const { return cache_hits_; }

private:
    unsigned  n_threads_;
    EvalFunc  eval_fn_;
    std::unordered_map<size_t, double> cache_;
    std::mutex cache_mutex_;
    size_t cache_hits_ = 0;
};
