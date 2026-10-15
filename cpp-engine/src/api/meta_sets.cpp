#include "meta_sets.h"
#include "combined_signals.h"
#include "meta_returns.h"
#include "../ga/parallel_evaluator.h"
#include <cmath>
#include <cstdio>
#include <limits>
#include <set>
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

namespace {

// Sums every member's normalized rows into one output per weight matrix, in member order per bar
// (same ordering rule as OrderedSignalAccumulator).
class WeightedAccumulator {
public:
    WeightedAccumulator(const DataPanel& data, const MetaRunConfig& config,
                        const std::vector<const CombineWeights*>& weights, const std::string& name)
        : data_(data), config_(config), weights_(weights), name_(name),
          outputs_(weights.size(), MetaMatrix(data.T, std::vector<float>(data.N, std::numeric_limits<float>::quiet_NaN()))),
          next_member_(data.T, 0) {}

    void add_block(std::size_t member, unsigned first_di, std::vector<float>& rows, unsigned n,
                   std::vector<double>& scratch) {
        if (!n || n != data_.N || rows.size() % n || first_di > data_.T || rows.size() / n > data_.T - first_di)
            throw std::invalid_argument("Combined signal dimensions must match the data panel");
        const unsigned count = static_cast<unsigned>(rows.size() / n);
        if (!count) return;
        std::vector<double> normalized(rows.size());
        for (unsigned offset = 0; offset < count; ++offset) {
            normalize_signal_row(data_, first_di + offset, rows.data() + static_cast<size_t>(offset) * n, scratch,
                                 config_.policy, config_.universe_mask);
            std::copy(scratch.begin(), scratch.begin() + n, normalized.begin() + static_cast<size_t>(offset) * n);
        }
        std::unique_lock<std::mutex> lock(mutex_);
        ready_.wait(lock, [&] {
            return cancelled_ || std::all_of(next_member_.begin() + first_di, next_member_.begin() + first_di + count,
                                             [&](std::size_t next) { return next == member; });
        });
        if (cancelled_) throw std::runtime_error("Combined evaluation cancelled after an alpha failed");
        for (unsigned offset = 0; offset < count; ++offset) {
            const unsigned di = first_di + offset;
            const double* row = normalized.data() + static_cast<size_t>(offset) * n;
            for (std::size_t c = 0; c < weights_.size(); ++c) {
                // Weight rows end at the trading period's end; members emit no rows after it.
                if (di >= weights_[c]->rows.size() || member >= weights_[c]->rows[di].size())
                    throw std::logic_error("meta '" + name_ + "' has no weight for bar " + std::to_string(di));
                const double w = weights_[c]->rows[di][member];
                auto& combined = outputs_[c][di];
                for (unsigned ii = 0; ii < n; ++ii) {
                    if (!std::isfinite(row[ii])) continue;
                    const float contribution = static_cast<float>(row[ii] * w);
                    float& value = combined[ii];
                    const float sum = std::isfinite(value) ? value + contribution : contribution;
                    if (!std::isfinite(sum)) throw std::overflow_error("Combined signal sum overflow at bar " +
                        std::to_string(di) + ", asset " + std::to_string(ii));
                    value = sum;
                }
            }
            ++next_member_[di];
        }
        lock.unlock();
        ready_.notify_all();
    }

    void cancel() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            cancelled_ = true;
        }
        ready_.notify_all();
    }

    std::vector<MetaMatrix> take() { return std::move(outputs_); }

private:
    const DataPanel& data_;
    const MetaRunConfig& config_;
    const std::vector<const CombineWeights*>& weights_;
    const std::string name_;
    std::vector<MetaMatrix> outputs_;
    std::vector<std::size_t> next_member_;
    std::mutex mutex_;
    std::condition_variable ready_;
    bool cancelled_ = false;
};

// Runs every member's signal rows into `accumulator` (EW or weighted) with the request's workers.
template <typename Accumulator>
void run_members(const DataPanel& data, MetaSet& set, const MetaRunConfig& config,
                 const std::function<bool()>& cancelled, MetaWorkerBudget* budget, Accumulator& accumulator) {
    const std::size_t count = set.members.size();
    if (count == 0) throw std::logic_error("meta set '" + set.name + "' has no members");
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
}

}  // namespace

MetaMatrix materialize_meta_set(const DataPanel& data, MetaSet& set, const MetaRunConfig& config,
                                const std::function<bool()>& cancelled, MetaWorkerBudget* budget) {
    OrderedSignalAccumulator accumulator(data, config.policy, config.universe_mask);
    run_members(data, set, config, cancelled, budget, accumulator);
    return accumulator.take_signals();
}

std::vector<MetaMatrix> materialize_weighted(const DataPanel& data, MetaSet& set, const MetaRunConfig& config,
                                             const std::vector<const CombineWeights*>& weights,
                                             const std::function<bool()>& cancelled, MetaWorkerBudget* budget) {
    for (const auto* matrix : weights)
        if (!matrix || matrix->rows.size() > data.T)
            throw std::logic_error("meta '" + set.name + "' weights must have at most one row per bar");
    WeightedAccumulator accumulator(data, config, weights, set.name);
    run_members(data, set, config, cancelled, budget, accumulator);
    return accumulator.take();
}

std::string meta_set_key(const MetaSet& set, const CombineSpec& spec) {
    std::string key = set.name;
    for (const auto& path : set.member_paths) key += "\n" + path;
    return key + "\n" + spec.key();
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

void MetaSetCache::put(const std::string& key, std::shared_ptr<const MetaMatrix> matrix, std::exception_ptr error) {
    std::shared_ptr<Entry> entry;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        auto& slot = entries_[key];
        if (!slot) slot = std::make_shared<Entry>();
        entry = slot;
    }
    std::call_once(entry->once, [&] {
        entry->matrix = std::move(matrix);
        entry->error = error;
    });
}

bool MetaSetCache::contains(const std::string& key) {
    std::lock_guard<std::mutex> lock(mutex_);
    return entries_.count(key) != 0;
}

void MetaSetCache::evict(const std::vector<std::string>& keys) {
    std::lock_guard<std::mutex> lock(mutex_);
    for (const auto& key : keys) {
        entries_.erase(key);
        infos_.erase(key);
    }
}

void MetaSetCache::set_info(const std::string& key, MetaCombineInfo info) {
    std::lock_guard<std::mutex> lock(mutex_);
    infos_[key] = info;
}

bool MetaSetCache::info(const std::string& key, MetaCombineInfo* out) {
    std::lock_guard<std::mutex> lock(mutex_);
    auto found = infos_.find(key);
    if (found == infos_.end()) return false;
    *out = found->second;
    return true;
}

void MetaSetCache::record(std::size_t materializations, std::size_t member_runs) {
    std::lock_guard<std::mutex> lock(mutex_);
    stats_.materializations += materializations;
    stats_.member_runs += member_runs;
}

MetaSetCache::Stats MetaSetCache::stats() {
    std::lock_guard<std::mutex> lock(mutex_);
    return stats_;
}

namespace {

std::string returns_key(const MetaSet& set, const std::string& settings) {
    uint64_t hash = 1469598103934665603ull;  // FNV-1a 64 over every member XML, in member order
    for (const auto& member : set.members) {
        for (unsigned char c : member.source_xml) {
            hash ^= c;
            hash *= 1099511628211ull;
        }
        hash ^= 0xff;
        hash *= 1099511628211ull;
    }
    char hex[17];
    std::snprintf(hex, sizeof hex, "%016llx", static_cast<unsigned long long>(hash));
    std::string key = set.name;
    for (const auto& path : set.member_paths) key += "\n" + path;
    return key + "\n" + hex + "\n" + settings;
}

struct CombinedSet {
    std::vector<MetaMatrix> matrices;          // empty matrix where errors[c] is set
    std::vector<std::exception_ptr> errors;
    std::vector<MetaCombineInfo> infos;
};

// Passes (1)-(3) for `specs` (non-Ew, distinct). A member failure or cancellation throws.
CombinedSet combine_set(const MetaCombineContext& context, MetaSet& set, const std::vector<CombineSpec>& specs) {
    const std::size_t members = set.members.size();
    auto returns = context.returns.get(returns_key(set, context.settings), [&] {
        auto value = collect_member_returns(context.data, set, context.config, context.cancelled,
                                            &context.cache.budget());
        context.cache.record(0, members);
        return value;
    });
    CombinedSet out;
    out.matrices.resize(specs.size());
    out.errors.resize(specs.size());
    out.infos.resize(specs.size());
    std::vector<CombineWeights> weights(specs.size());
    std::vector<const CombineWeights*> used;
    std::vector<std::size_t> used_index;
    for (std::size_t c = 0; c < specs.size(); ++c) {
        try {
            weights[c] = compute_combine_weights(*returns, specs[c]);
        } catch (const std::invalid_argument& error) {
            out.errors[c] = std::make_exception_ptr(std::invalid_argument("meta '" + set.name + "' " + error.what()));
            continue;
        }
        out.infos[c] = {weights[c].fallback_bars, weights[c].first_estimated_bar};
        used.push_back(&weights[c]);
        used_index.push_back(c);
    }
    auto matrices = materialize_weighted(context.data, set, context.config, used, context.cancelled,
                                         &context.cache.budget());
    context.cache.record(1, members);
    for (std::size_t u = 0; u < used.size(); ++u) out.matrices[used_index[u]] = std::move(matrices[u]);
    return out;
}

}  // namespace

MetaMatrix materialize_combined(const MetaCombineContext& context, MetaSet& set) {
    auto combined = combine_set(context, set, {set.combine});
    if (combined.errors[0]) std::rethrow_exception(combined.errors[0]);
    context.cache.set_info(meta_set_key(set, set.combine), combined.infos[0]);
    return std::move(combined.matrices[0]);
}

std::vector<std::string> prefetch_meta_sets(const MetaCombineContext& context, std::vector<CompiledAlpha>& alphas) {
    struct Group {
        MetaSet* set = nullptr;
        std::vector<CombineSpec> specs;
        std::set<std::string> keys;
    };
    std::map<std::string, Group> groups;
    std::vector<std::string> order;
    for (auto& alpha : alphas)
        for (auto& set : alpha.meta_sets) {
            if (set.combine.is_ew() || set.members.empty()) continue;
            const std::string key = meta_set_key(set, set.combine);
            const std::string group_key = key.substr(0, key.size() - set.combine.key().size());
            auto& group = groups[group_key];
            if (!group.set) {
                group.set = &set;
                order.push_back(group_key);
            }
            if (context.cache.contains(key) || !group.keys.insert(key).second) continue;
            group.specs.push_back(set.combine);
        }
    std::vector<std::string> stored;
    for (const auto& group_key : order) {
        auto& group = groups[group_key];
        if (group.specs.empty()) continue;
        CombinedSet combined;
        std::exception_ptr failure;
        try {
            combined = combine_set(context, *group.set, group.specs);
        } catch (const EvaluationCancelled&) {
            throw;
        } catch (...) {
            failure = std::current_exception();  // member failure: every candidate of this set fails with it
        }
        for (std::size_t c = 0; c < group.specs.size(); ++c) {
            const std::string key = meta_set_key(*group.set, group.specs[c]);
            if (failure) {
                context.cache.put(key, nullptr, failure);
            } else if (combined.errors[c]) {
                context.cache.put(key, nullptr, combined.errors[c]);
            } else {
                context.cache.set_info(key, combined.infos[c]);
                // Round memory is C x T x N x 4 bytes: keep the meta parameter grid per round small.
                context.cache.put(key, std::make_shared<const MetaMatrix>(std::move(combined.matrices[c])), nullptr);
            }
            stored.push_back(key);
        }
    }
    return stored;
}
