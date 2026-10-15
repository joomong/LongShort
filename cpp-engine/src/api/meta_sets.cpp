#include "meta_sets.h"
#include "combined_signals.h"
#include "../ga/parallel_evaluator.h"
#include <algorithm>
#include <atomic>
#include <stdexcept>
#include <thread>

unsigned MetaWorkerBudget::acquire(unsigned wanted) {
    std::unique_lock<std::mutex> lock(mutex_);
    available_.wait(lock, [&] { return free_ > 0; });
    const unsigned granted = std::min(free_, std::max(1u, wanted));
    free_ -= granted;
    return granted;
}

void MetaWorkerBudget::release(unsigned count) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        free_ += count;
    }
    available_.notify_all();
}

MetaMatrix materialize_meta_set(const DataPanel& data, MetaSet& set, const MetaRunConfig& config,
                                const std::function<bool()>& cancelled, MetaWorkerBudget* budget) {
    const std::size_t count = set.members.size();
    if (count == 0) throw std::logic_error("meta set '" + set.name + "' has no members");
    const unsigned wanted = std::max(1u, std::min<unsigned>(config.threads, static_cast<unsigned>(count)));
    struct Tokens {
        MetaWorkerBudget* budget;
        unsigned count;
        ~Tokens() { if (budget) budget->release(count); }
    } tokens{budget, budget ? budget->acquire(wanted) : wanted};
    const unsigned thread_count = tokens.count;
    OrderedSignalAccumulator accumulator(data, config.policy, config.universe_mask);
    std::atomic<std::size_t> next_index{0};
    std::atomic<bool> failed{false};
    std::exception_ptr first_error;
    std::mutex error_mutex;
    auto worker = [&] {
        std::vector<double> normalized_scratch;
        while (!failed.load(std::memory_order_acquire)) {
            const std::size_t i = next_index.fetch_add(1);
            if (i >= count) return;
            try {
                try {
                    std::vector<float> signal_block;
                    signal_block.reserve(static_cast<size_t>(32) * data.N);
                    unsigned first_bar = 0;
                    auto flush = [&] {
                        if (signal_block.empty()) return;
                        accumulator.add_block(i, first_bar, signal_block, data.N, normalized_scratch);
                        signal_block.clear();
                    };
                    auto observe = [&](unsigned di, const float* row, unsigned n) {
                        if (failed.load() || cancelled()) throw EvaluationCancelled();
                        if (signal_block.empty()) first_bar = di;
                        signal_block.insert(signal_block.end(), row, row + n);
                        if (signal_block.size() == static_cast<size_t>(32) * n) flush();
                    };
                    Simulator(data, config.member_config).run_signal_rows(set.members[i].pipeline, observe);
                    flush();
                    set.members[i] = CompiledAlpha{};
                } catch (const EvaluationCancelled&) {
                    throw;
                } catch (const std::exception& error) {
                    throw std::runtime_error("meta '" + set.name + "' member " + set.member_paths.at(i) +
                                             ": " + error.what());
                }
            } catch (...) {
                {
                    std::lock_guard<std::mutex> lock(error_mutex);
                    if (!first_error) first_error = std::current_exception();
                }
                failed.store(true, std::memory_order_release);
                accumulator.cancel();
                return;
            }
        }
    };
    std::vector<std::thread> workers;
    try {
        for (unsigned t = 0; t < thread_count; ++t) workers.emplace_back(worker);
    } catch (...) {
        failed.store(true, std::memory_order_release);
        accumulator.cancel();
        for (auto& thread : workers) thread.join();
        throw;
    }
    for (auto& thread : workers) thread.join();
    if (first_error) std::rethrow_exception(first_error);
    if (cancelled()) throw EvaluationCancelled();
    return accumulator.take_signals();
}

std::shared_ptr<const MetaMatrix> MetaSetCache::get(const std::string& name,
                                                    const std::function<MetaMatrix()>& compute) {
    std::shared_ptr<Entry> entry;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        auto& slot = entries_[name];
        if (!slot) slot = std::make_shared<Entry>();
        entry = slot;
    }
    // call_once never runs the callable twice here: failures are stored, not thrown out of it.
    std::call_once(entry->once, [&] {
        try {
            entry->matrix = std::make_shared<const MetaMatrix>(compute());
        } catch (...) {
            entry->error = std::current_exception();
        }
    });
    if (entry->error) std::rethrow_exception(entry->error);
    return entry->matrix;
}
