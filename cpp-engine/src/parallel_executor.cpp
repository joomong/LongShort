#include "parallel_executor.h"
#include <algorithm>
#include <condition_variable>
#include <exception>
#include <mutex>
#include <thread>
#include <vector>
#include <cmath>
#include <deque>
#include <limits>

namespace {
thread_local AssetParallelScope* current_scope = nullptr;
}

struct AssetParallelScope::State {
    explicit State(unsigned requested) : lanes(std::max(1u, std::min(4u, requested))) {}
    unsigned lanes;
    std::vector<std::thread> workers;
    std::mutex mutex;
    std::condition_variable ready, done;
    std::function<void(unsigned, unsigned)> task;
    std::size_t generation = 0;
    unsigned assets = 0, remaining = 0;
    bool stopping = false;
    std::exception_ptr error;

    void start() {
        if (!workers.empty()) return;
        try {
            for (unsigned lane = 1; lane < lanes; ++lane) workers.emplace_back([this, lane] {
                std::size_t seen = 0;
                std::unique_lock<std::mutex> lock(mutex);
                for (;;) {
                    ready.wait(lock, [&] { return stopping || generation != seen; });
                    if (stopping) return;
                    seen = generation;
                    const unsigned begin = static_cast<std::size_t>(assets) * lane / lanes;
                    const unsigned end = static_cast<std::size_t>(assets) * (lane + 1) / lanes;
                    lock.unlock();
                    std::exception_ptr failure;
                    try { task(begin, end); } catch (...) { failure = std::current_exception(); }
                    lock.lock();
                    if (failure && !error) error = failure;
                    if (--remaining == 0) done.notify_one();
                }
            });
        } catch (...) {
            stop();
            throw;
        }
    }
    void stop() {
        { std::lock_guard<std::mutex> lock(mutex); stopping = true; }
        ready.notify_all();
        for (auto& worker : workers) if (worker.joinable()) worker.join();
    }
    ~State() { stop(); }
};

AssetParallelScope::AssetParallelScope(unsigned threads)
    : state_(std::make_unique<State>(threads)), previous_(current_scope) {
    current_scope = this;
}

AssetParallelScope::~AssetParallelScope() {
    current_scope = previous_;
}

void AssetParallelScope::run(unsigned assets, std::size_t work_per_asset,
                             const std::function<void(unsigned, unsigned)>& task) {
    // Daily dispatch costs more than small arithmetic loops. Use only sizeable
    // cross-sections and >=64K estimated operations; keep short windows serial.
    if (state_->lanes == 1 || assets < 512 || work_per_asset == 0 ||
        work_per_asset < (std::size_t{65536} + assets - 1) / assets) {
        task(0, assets);
        return;
    }
    auto& state = *state_;
    state.start();
    {
        std::lock_guard<std::mutex> lock(state.mutex);
        state.assets = assets;
        state.task = task;
        state.error = nullptr;
        state.remaining = state.lanes - 1;
        ++state.generation;
    }
    state.ready.notify_all();
    std::exception_ptr failure;
    // A nested helper on the caller follows the same serial policy as helpers
    // invoked by workers; never dispatch recursively into the occupied pool.
    auto* saved_scope = current_scope;
    current_scope = nullptr;
    try { task(0, assets / state.lanes); } catch (...) { failure = std::current_exception(); }
    current_scope = saved_scope;
    std::unique_lock<std::mutex> lock(state.mutex);
    state.done.wait(lock, [&] { return state.remaining == 0; });
    if (!failure) failure = state.error;
    state.task = {}; // never keep module or signal references between dates
    lock.unlock();
    if (failure) std::rethrow_exception(failure);
}

void parallel_for_assets(unsigned assets, std::size_t work_per_asset,
                         const std::function<void(unsigned, unsigned)>& task) {
    if (current_scope) current_scope->run(assets, work_per_asset, task);
    else task(0, assets);
}

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
