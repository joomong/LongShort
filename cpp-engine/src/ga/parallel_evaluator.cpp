#include "parallel_evaluator.h"
#include <vector>
#include <thread>
#include <mutex>

ParallelEvaluator::ParallelEvaluator(unsigned n_threads, EvalFunc eval_fn)
    : n_threads_(n_threads == 0 ? 1 : n_threads)
    , eval_fn_(std::move(eval_fn))
{}

std::vector<double> ParallelEvaluator::evaluate_batch(
    const std::vector<std::string>& xmls)
{
    size_t n = xmls.size();
    std::vector<double> results(n, 0.0);

    // Pre-hash all XMLs
    std::hash<std::string> hasher;
    std::vector<size_t> hashes(n);
    for (size_t i = 0; i < n; ++i) {
        hashes[i] = hasher(xmls[i]);
    }

    // Identify which indices need evaluation (not cached)
    // Also dedup within the batch: if same hash appears multiple times,
    // only evaluate the first occurrence and copy results to duplicates.
    std::vector<size_t> uncached_indices;          // indices to actually evaluate
    std::unordered_map<size_t, size_t> batch_seen; // hash → first index in this batch
    std::vector<std::pair<size_t, size_t>> deferred; // (dup_idx, first_idx) for intra-batch dups
    {
        std::lock_guard<std::mutex> lock(cache_mutex_);
        for (size_t i = 0; i < n; ++i) {
            auto it = cache_.find(hashes[i]);
            if (it != cache_.end()) {
                results[i] = it->second;
                ++cache_hits_;
            } else {
                auto seen_it = batch_seen.find(hashes[i]);
                if (seen_it != batch_seen.end()) {
                    // Duplicate within this batch — defer copy
                    deferred.push_back({i, seen_it->second});
                    ++cache_hits_;
                } else {
                    batch_seen[hashes[i]] = i;
                    uncached_indices.push_back(i);
                }
            }
        }
    }

    if (uncached_indices.empty()) {
        return results;
    }

    // Distribute uncached work across threads
    size_t n_work = uncached_indices.size();
    unsigned n_threads = std::min((unsigned)n_work, n_threads_);

    // Each thread works on a contiguous slice of uncached_indices
    std::vector<std::thread> threads;
    threads.reserve(n_threads);

    size_t chunk = (n_work + n_threads - 1) / n_threads;

    for (unsigned t = 0; t < n_threads; ++t) {
        size_t begin = t * chunk;
        size_t end   = std::min(begin + chunk, n_work);
        if (begin >= end) break;

        threads.emplace_back([&, begin, end]() {
            for (size_t k = begin; k < end; ++k) {
                size_t idx = uncached_indices[k];
                double val = eval_fn_(xmls[idx]);
                results[idx] = val;

                // Store in cache under lock
                std::lock_guard<std::mutex> lock(cache_mutex_);
                cache_[hashes[idx]] = val;
            }
        });
    }

    for (auto& th : threads) {
        th.join();
    }

    // Copy results for intra-batch duplicates
    for (auto& [dup_idx, first_idx] : deferred) {
        results[dup_idx] = results[first_idx];
    }

    return results;
}
