#include "meta_returns.h"
#include "../ga/parallel_evaluator.h"
#include <algorithm>
#include <atomic>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <thread>

MemberReturns collect_member_returns(const DataPanel& data, const MetaSet& set, const MetaRunConfig& config,
                                     const std::function<bool()>& cancelled, MetaWorkerBudget* budget) {
    const std::size_t count = set.members.size();
    if (count == 0) throw std::logic_error("meta set '" + set.name + "' has no members");
    MemberReturns out;
    out.paths = set.member_paths;
    // Rows end at the trading period's end (sim_end_index), like the member's gross_ret.
    out.T = std::min<unsigned>(data.T, config.member_config.sim_end_index);
    out.K = static_cast<unsigned>(count);
    out.sim_start = config.member_config.sim_start_index;
    out.rows.assign(out.T, std::vector<double>(count, std::numeric_limits<double>::quiet_NaN()));
    const unsigned wanted = std::max(1u, std::min<unsigned>(config.threads, static_cast<unsigned>(count)));
    struct Tokens {
        MetaWorkerBudget* budget;
        unsigned count;
        ~Tokens() { if (budget) budget->release(count); }
    } tokens{budget, budget ? budget->acquire(wanted) : wanted};
    const unsigned thread_count = tokens.count;
    std::atomic<std::size_t> next_index{0};
    std::atomic<bool> failed{false};
    std::exception_ptr first_error;
    std::mutex error_mutex;
    auto worker = [&] {
        while (!failed.load(std::memory_order_acquire)) {
            const std::size_t i = next_index.fetch_add(1);
            if (i >= count) return;
            try {
                try {
                    if (cancelled()) throw EvaluationCancelled();
                    SimulatorConfig member_config = config.member_config;
                    member_config.collect_signals = false;
                    member_config.signal_row_callback = [&](unsigned, const float*, unsigned) {
                        if (failed.load() || cancelled()) throw EvaluationCancelled();
                    };
                    const SimulatorOutput output = Simulator(data, member_config).run(set.members[i].pipeline);
                    const std::size_t bars = std::min<std::size_t>(output.gross_ret.size(), out.T);
                    for (std::size_t t = out.sim_start; t < bars; ++t) out.rows[t][i] = output.gross_ret[t];
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
                return;
            }
        }
    };
    std::vector<std::thread> workers;
    try {
        for (unsigned t = 0; t < thread_count; ++t) workers.emplace_back(worker);
    } catch (...) {
        failed.store(true, std::memory_order_release);
        for (auto& thread : workers) thread.join();
        throw;
    }
    for (auto& thread : workers) thread.join();
    if (first_error) std::rethrow_exception(first_error);
    if (cancelled()) throw EvaluationCancelled();
    return out;
}

std::shared_ptr<const MemberReturns> MemberReturnsCache::get(const std::string& key,
                                                             const std::function<MemberReturns()>& compute) {
    std::unique_lock<std::mutex> lock(mutex_);
    for (;;) {
        auto found = entries_.find(key);
        if (found == entries_.end()) break;
        std::shared_ptr<Entry> entry = found->second;
        if (entry->done) {
            lru_.remove(key);
            lru_.push_front(key);
            return entry->value;
        }
        changed_.wait(lock, [&] { return entry->done; });
        // Completed, evicted or failed: look it up again. A waiter never inherits the computing
        // caller's exception (it may be that request's cancellation); after a failure the entry is
        // gone and this caller computes with its own callback.
    }
    auto entry = std::make_shared<Entry>();
    entries_[key] = entry;
    lock.unlock();
    std::shared_ptr<const MemberReturns> value;
    std::exception_ptr error;
    try {
        value = std::make_shared<const MemberReturns>(compute());
    } catch (...) {
        error = std::current_exception();
    }
    lock.lock();
    entry->done = true;
    if (error) {
        entries_.erase(key);
        changed_.notify_all();
        lock.unlock();
        std::rethrow_exception(error);
    }
    entry->value = value;
    lru_.push_front(key);
    while (lru_.size() > kCapacity) {
        entries_.erase(lru_.back());
        lru_.pop_back();
    }
    changed_.notify_all();
    return value;
}
