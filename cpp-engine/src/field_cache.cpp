#include "field_cache.h"

#include <algorithm>
#include <condition_variable>
#include <cstring>
#include <exception>
#include <list>
#include <map>
#include <tuple>

namespace field_cache_testing {
void (*fault)(const char* site) = nullptr;
}

namespace {
void fault_point(const char* site) {
    if (auto hook = field_cache_testing::fault) hook(site);
}
}

bool FieldKey::operator<(const FieldKey& other) const {
    return std::tie(data_version, canonical_field, date_axis, symbol_axis) <
           std::tie(other.data_version, other.canonical_field, other.date_axis, other.symbol_axis);
}

std::size_t FieldBuffer::bytes() const {
    std::size_t total = values.size() * sizeof(float) + timestamps.size() * sizeof(int64_t);
    for (const auto& symbol : symbols) total += symbol.size();
    return total;
}

std::string field_axis_key(const std::vector<int64_t>& timestamps) {
    std::string key(timestamps.size() * sizeof(int64_t), '\0');
    if (!timestamps.empty()) std::memcpy(key.data(), timestamps.data(), key.size());
    return key;
}
std::string field_axis_key(const std::vector<std::string>& symbols) {
    std::string key;
    for (const auto& symbol : symbols) key += std::to_string(symbol.size()) + ':' + symbol;
    return key;
}

namespace {
// One load in flight. Waiters keep it so they see the outcome even after a failed entry is erased.
struct Flight {
    bool finished = false;
    std::exception_ptr error;
};
struct Entry {
    std::shared_ptr<FieldBuffer> buffer;  // the cache's own reference; null while loading
    std::shared_ptr<Flight> flight;       // null once published
    std::size_t bytes = 0;
    std::size_t users = 0;                // run references alive
    std::size_t pending = 0;              // waiters woken by the load and not yet holding a reference
    // The entry's single LRU node, allocated with the entry: here while in use, spliced into the idle list
    // while idle, so becoming idle or active again allocates nothing.
    std::list<const FieldKey*> node;
    std::list<const FieldKey*>::iterator idle_position;
    bool idle = false;
};
}

struct FieldCache::State {
    mutable std::mutex mutex;
    std::condition_variable loaded;
    std::map<FieldKey, Entry> entries;
    std::list<const FieldKey*> idle;  // front = most recently released
    FieldCacheStats stats;

    // A run reference, allocated before any count is raised; only an armed pin changes counts when it goes.
    struct Pin {
        std::shared_ptr<State> state;
        const FieldKey* key = nullptr;  // null: an adopted (anonymous) buffer
        std::shared_ptr<const FieldBuffer> buffer;
        std::shared_ptr<FieldRun> run;
        std::size_t bytes = 0;
        bool armed = false;
        ~Pin() {
            if (!armed) return;
            buffer.reset();  // before release: the cache (or nobody, for an adopted buffer) holds the last reference
            state->release(*this);
        }
    };

    std::size_t optional_bytes() const {
        return stats.active_bytes + stats.idle_bytes + stats.loading_reserved_bytes;
    }
    void note_increase() {  // with mutex held, after any count went up
        stats.peak_optional_bytes = std::max(stats.peak_optional_bytes, optional_bytes());
        stats.peak_loading_reserved_bytes = std::max(stats.peak_loading_reserved_bytes, stats.loading_reserved_bytes);
        if (optional_bytes() > stats.budget_bytes) ++stats.over_budget_events;
    }
    // Evicts idle fields, least recently used first, while the total is over the budget. With mutex held;
    // allocates nothing.
    void trim() {
        for (auto it = idle.end(); optional_bytes() > stats.budget_bytes && it != idle.begin();) {
            --it;
            auto entry = entries.find(**it);
            if (entry->second.pending) continue;  // woken waiters are about to take it
            stats.idle_bytes -= entry->second.bytes;
            ++stats.evictions;
            const auto next = std::next(it);
            entry->second.node.splice(entry->second.node.begin(), idle, it);
            entries.erase(entry);  // drops the cache reference; bytes go with the last reference
            it = next;
        }
    }
    std::size_t run_bytes(const std::shared_ptr<FieldRun>& run) const { return run ? run->bytes : 0; }
    [[noreturn]] void refuse(const FieldKey* key, std::size_t bytes, const std::shared_ptr<FieldRun>& run) {
        ++stats.rejected;
        throw FieldBudgetExceeded(
            "field cache: request would hold " + std::to_string(run_bytes(run) + bytes) + " bytes of optional inputs" +
            (key ? " with '" + key->canonical_field + "'" : std::string()) + " (" + std::to_string(bytes) +
            " more), over the budget of " + std::to_string(stats.budget_bytes) + " bytes; active " +
            std::to_string(stats.active_bytes) + ", idle " + std::to_string(stats.idle_bytes) + ", reserved " +
            std::to_string(stats.loading_reserved_bytes));
    }
    // Takes a run reference on a published entry into a preallocated pin. With mutex held; allocates nothing.
    void arm(Pin& pin, std::map<FieldKey, Entry>::iterator it, const std::shared_ptr<State>& self,
             const std::shared_ptr<FieldRun>& run) noexcept {
        Entry& entry = it->second;
        if (entry.users++ == 0) {
            if (entry.idle) {
                entry.node.splice(entry.node.begin(), idle, entry.idle_position);
                entry.idle = false;
                stats.idle_bytes -= entry.bytes;
            }
            stats.active_bytes += entry.bytes;
        }
        pin.state = self;
        pin.key = &it->first;
        pin.buffer = entry.buffer;
        pin.run = run;
        pin.bytes = entry.bytes;
        if (run) run->bytes += entry.bytes;
        pin.armed = true;
    }
    void release(const Pin& pin) noexcept {
        std::lock_guard<std::mutex> lock(mutex);
        if (pin.run) pin.run->bytes -= pin.bytes;
        if (!pin.key) {
            stats.active_bytes -= pin.bytes;
            return;
        }
        auto& entry = entries.find(*pin.key)->second;
        if (--entry.users) return;
        stats.active_bytes -= entry.bytes;
        stats.idle_bytes += entry.bytes;
        idle.splice(idle.begin(), entry.node);
        entry.idle_position = idle.begin();
        entry.idle = true;
        trim();
    }
};

FieldCache::FieldCache(std::size_t budget_bytes) : state_(std::make_shared<State>()) {
    state_->stats.budget_bytes = budget_bytes;
}
FieldCache::~FieldCache() = default;

std::shared_ptr<const FieldBuffer> FieldCache::acquire(const FieldKey& key, const Loader& load, std::size_t reserve_bytes,
                                                       const std::shared_ptr<FieldRun>& run, std::mutex* read_lock) {
    State& s = *state_;
    fault_point("pin");
    // Allocated before any count is raised and declared before the lock, so it is never destroyed under it.
    const std::shared_ptr<State::Pin> pin(new State::Pin());
    const auto handle = [&] { return std::shared_ptr<const FieldBuffer>(pin, pin->buffer.get()); };
    std::unique_lock<std::mutex> lock(s.mutex);
    auto it = s.entries.find(key);
    if (it != s.entries.end()) {
        if (it->second.flight) {
            const auto flight = it->second.flight;
            ++it->second.pending;
            s.loaded.wait(lock, [&] { return flight->finished; });
            if (flight->error) std::rethrow_exception(flight->error);  // the failed entry is gone with its pending count
            it = s.entries.find(key);  // published and kept while pending
            --it->second.pending;
        }
        if (s.run_bytes(run) + it->second.bytes > s.stats.budget_bytes) s.refuse(&it->first, it->second.bytes, run);
        ++s.stats.hits;
        s.arm(*pin, it, state_, run);
        s.note_increase();
        return handle();
    }
    if (s.run_bytes(run) + reserve_bytes > s.stats.budget_bytes) s.refuse(&key, reserve_bytes, run);
    fault_point("entry");
    const auto flight = std::make_shared<Flight>();     // a failure here has counted and published nothing
    it = s.entries.emplace(key, Entry{}).first;
    try {
        it->second.node.push_back(&it->first);
        it->second.flight = flight;
    } catch (...) {
        s.entries.erase(it);
        throw;
    }
    ++s.stats.loads;
    lock.unlock();
    bool reserved = false;
    try {
        std::shared_ptr<FieldBuffer> published;
        {
            std::unique_lock<std::mutex> read_guard;
            if (read_lock) read_guard = std::unique_lock<std::mutex>(*read_lock);  // reserve only once reading
            {
                std::lock_guard<std::mutex> reserve_guard(s.mutex);
                s.stats.loading_reserved_bytes += reserve_bytes;
                reserved = true;
                s.trim();  // idle fields make room first; what is left over is an over-budget event
                s.note_increase();
            }
            FieldBuffer buffer = load();
            fault_point("publish");
            published = std::make_shared<FieldBuffer>(std::move(buffer));
        }
        lock.lock();
        s.stats.loading_reserved_bytes -= reserve_bytes;
        reserved = false;
        const std::size_t bytes = published->bytes();
        if (s.run_bytes(run) + bytes > s.stats.budget_bytes) s.refuse(&key, bytes, run);
        it->second.buffer = std::move(published);
        it->second.bytes = bytes;
        it->second.flight.reset();
        flight->finished = true;
        s.arm(*pin, it, state_, run);  // waiters hold `pending` and arm their own preallocated pins when they wake
        s.trim();
        s.note_increase();
        s.loaded.notify_all();
        return handle();
    } catch (...) {
        if (!lock.owns_lock()) lock.lock();
        if (reserved) s.stats.loading_reserved_bytes -= reserve_bytes;
        ++s.stats.failures;
        flight->error = std::current_exception();
        flight->finished = true;
        s.entries.erase(it);
        s.loaded.notify_all();
        throw;
    }
}

std::shared_ptr<const FieldBuffer> FieldCache::adopt(FieldBuffer buffer, const std::shared_ptr<FieldRun>& run) {
    State& s = *state_;
    fault_point("adopt");
    const std::shared_ptr<State::Pin> pin(new State::Pin());
    const auto owned = std::make_shared<const FieldBuffer>(std::move(buffer));
    std::lock_guard<std::mutex> lock(s.mutex);
    const std::size_t bytes = owned->bytes();
    if (s.run_bytes(run) + bytes > s.stats.budget_bytes) s.refuse(nullptr, bytes, run);
    s.stats.active_bytes += bytes;
    if (run) run->bytes += bytes;
    pin->state = state_;
    pin->buffer = owned;
    pin->run = run;
    pin->bytes = bytes;
    pin->armed = true;
    s.trim();
    s.note_increase();
    return std::shared_ptr<const FieldBuffer>(pin, pin->buffer.get());
}

void FieldCache::set_budget(std::size_t bytes) {
    std::lock_guard<std::mutex> lock(state_->mutex);
    state_->stats.budget_bytes = bytes;
    state_->trim();
}
void FieldCache::set_resident_bytes(std::size_t bytes) {
    std::lock_guard<std::mutex> lock(state_->mutex);
    state_->stats.resident_bytes = bytes;
}
void FieldCache::add_resident_bytes(std::size_t bytes) {
    std::lock_guard<std::mutex> lock(state_->mutex);
    state_->stats.resident_bytes += bytes;
}
FieldCacheStats FieldCache::stats() const {
    std::lock_guard<std::mutex> lock(state_->mutex);
    return state_->stats;
}
