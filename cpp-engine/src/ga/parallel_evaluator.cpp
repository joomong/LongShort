#include "parallel_evaluator.h"
#include <algorithm>
#include <cmath>
#include <condition_variable>
#include <deque>
#include <limits>

ParallelEvaluator::ParallelEvaluator(unsigned n_threads, EvalFunc eval_fn)
    : n_threads_(std::max(1u, n_threads)), eval_fn_(std::move(eval_fn)) {}

std::vector<double> ParallelEvaluator::evaluate_batch(const std::vector<std::string>& xmls) {
    const auto evaluations = evaluate_results(xmls);
    std::vector<double> scores;
    for (const auto& evaluation : evaluations) scores.push_back(evaluation.fitness);
    return scores;
}

std::vector<EvaluationResult> ParallelEvaluator::evaluate_results(
    const std::vector<std::string>& xmls, Completion completed, Cancelled cancelled) {
    std::vector<EvaluationResult> results(xmls.size());
    std::vector<size_t> work, cached;
    std::vector<std::pair<size_t, size_t>> duplicates;
    std::unordered_map<std::string, size_t> seen;
    {
        std::lock_guard<std::mutex> lock(cache_mutex_);
        for (size_t i = 0; i < xmls.size(); ++i) {
            const auto stored = cache_.find(xmls[i]);
            if (stored != cache_.end()) {
                results[i] = stored->second;
                cached.push_back(i);
                ++cache_hits_;
            } else {
                const auto inserted = seen.emplace(xmls[i], i);
                if (inserted.second) work.push_back(i);
                else { duplicates.emplace_back(i, inserted.first->second); ++cache_hits_; }
            }
        }
    }
    for (size_t i : cached) {
        if (cancelled && cancelled()) throw EvaluationCancelled();
        if (completed) completed(i, results[i]);
    }
    std::atomic<size_t> next{0};
    std::atomic<bool> stopping{false};
    std::mutex mutex;
    std::condition_variable ready;
    std::deque<size_t> finished;
    size_t workers_done = 0;
    std::exception_ptr worker_error;
    const unsigned count = std::min<size_t>(n_threads_, work.size());
    std::vector<std::thread> workers;
    workers.reserve(count);
    auto join = [&] { for (auto& worker : workers) if (worker.joinable()) worker.join(); };
    try {
        for (unsigned t = 0; t < count; ++t) {
            workers.emplace_back([&] {
                try {
                    while (!stopping.load(std::memory_order_acquire)) {
                        if (cancelled && cancelled()) { stopping.store(true); break; }
                        const size_t slot = next.fetch_add(1);
                        if (slot >= work.size()) break;
                        const size_t i = work[slot];
                        EvaluationResult value;
                        try {
                            value.fitness = eval_fn_(xmls[i]);
                            if (!std::isfinite(value.fitness))
                                throw std::runtime_error("GA objective is not finite");
                        } catch (const EvaluationCancelled&) {
                            stopping.store(true);
                            break;
                        } catch (const std::exception& error) {
                            value.fitness = -std::numeric_limits<double>::infinity();
                            value.error = error.what();
                        } catch (...) {
                            value.fitness = -std::numeric_limits<double>::infinity();
                            value.error = "Unknown candidate evaluation error";
                        }
                        results[i] = value;
                        {
                            std::lock_guard<std::mutex> lock(cache_mutex_);
                            cache_.emplace(xmls[i], value);
                        }
                        {
                            std::lock_guard<std::mutex> lock(mutex);
                            finished.push_back(i);
                        }
                        ready.notify_one();
                    }
                } catch (...) {
                    std::lock_guard<std::mutex> lock(mutex);
                    if (!worker_error) worker_error = std::current_exception();
                    stopping.store(true);
                }
                {
                    std::lock_guard<std::mutex> lock(mutex);
                    ++workers_done;
                }
                ready.notify_one();
            });
        }
        for (;;) {
            std::unique_lock<std::mutex> lock(mutex);
            ready.wait(lock, [&] { return !finished.empty() || workers_done == count; });
            if (finished.empty()) break;
            const size_t i = finished.front();
            finished.pop_front();
            lock.unlock();
            if (cancelled && cancelled()) throw EvaluationCancelled();
            if (completed) completed(i, results[i]);
        }
    } catch (...) {
        stopping.store(true);
        join();
        throw;
    }
    join();
    if (worker_error) std::rethrow_exception(worker_error);
    if (stopping.load() || (cancelled && cancelled())) throw EvaluationCancelled();
    for (const auto& duplicate : duplicates) {
        results[duplicate.first] = results[duplicate.second];
        if (completed) completed(duplicate.first, results[duplicate.first]);
    }
    return results;
}
