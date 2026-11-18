#include "field_cache.h"

#include <condition_variable>
#include <cstring>
#include <exception>
#include <list>
#include <map>
#include <mutex>
#include <tuple>

bool FieldKey::operator<(const FieldKey& other) const {
    return std::tie(data_version, canonical_field, date_axis, symbol_axis) <
           std::tie(other.data_version, other.canonical_field, other.date_axis, other.symbol_axis);
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
    std::size_t waiters = 0;
};
struct Entry {
    std::shared_ptr<FieldBuffer> buffer;  // the cache's own reference; null while loading
    std::shared_ptr<Flight> flight;       // null once ready
    std::size_t bytes = 0;
    std::size_t users = 0;                // run references alive
    bool idle_listed = false;
    std::list<const FieldKey*>::iterator idle_position;
};
}

struct FieldCache::State {
    mutable std::mutex mutex;
    std::condition_variable loaded;
    std::map<FieldKey, Entry> entries;
    std::list<const FieldKey*> idle;  // front = most recently released
    FieldCacheStats stats;

    std::size_t optional_bytes() const {
        return stats.active_bytes + stats.idle_bytes + stats.loading_reserved_bytes;
    }
    // Evicts idle fields, least recently used first, until `extra` more bytes fit. With mutex held.
    bool make_room(std::size_t extra) {
        for (auto it = idle.end(); optional_bytes() + extra > stats.budget_bytes && it != idle.begin();) {
            --it;
            auto entry = entries.find(**it);
            stats.idle_bytes -= entry->second.bytes;
            ++stats.evictions;
            it = idle.erase(it);
            entries.erase(entry);  // drops the cache reference; bytes go with the last reference
        }
        return optional_bytes() + extra <= stats.budget_bytes;
    }
    void pin(Entry& entry) {  // with mutex held
        if (entry.users++ == 0) {
            if (entry.idle_listed) {
                idle.erase(entry.idle_position);
                entry.idle_listed = false;
                stats.idle_bytes -= entry.bytes;
            }
            stats.active_bytes += entry.bytes;
        }
    }
    void release(const FieldKey* key) {
        std::lock_guard<std::mutex> lock(mutex);
        auto& entry = entries.find(*key)->second;
        if (--entry.users) return;
        stats.active_bytes -= entry.bytes;
        stats.idle_bytes += entry.bytes;
        idle.push_front(key);
        entry.idle_position = idle.begin();
        entry.idle_listed = true;
        make_room(0);
    }
    // A run reference: keeps the buffer alive and tells the cache when it goes.
    struct Pin {
        std::shared_ptr<State> state;
        const FieldKey* key;
        std::shared_ptr<const FieldBuffer> buffer;
        ~Pin() { state->release(key); }
    };
};

FieldCache::FieldCache(std::size_t budget_bytes) : state_(std::make_shared<State>()) {
    state_->stats.budget_bytes = budget_bytes;
}
FieldCache::~FieldCache() = default;

std::shared_ptr<const FieldBuffer> FieldCache::acquire(const FieldKey& key, const Loader& load,
                                                       std::size_t reserve_bytes) {
    State& s = *state_;
    // With mutex held; `counted` when the run reference was already added to users.
    const auto pinned = [&](std::map<FieldKey, Entry>::iterator it, bool counted = false) {
        if (!counted) s.pin(it->second);
        std::shared_ptr<State::Pin> pin(new State::Pin{state_, &it->first, it->second.buffer});
        return std::shared_ptr<const FieldBuffer>(pin, pin->buffer.get());
    };
    std::unique_lock<std::mutex> lock(s.mutex);
    auto it = s.entries.find(key);
    if (it != s.entries.end()) {
        if (!it->second.flight) {
            ++s.stats.hits;
            return pinned(it);
        }
        const auto flight = it->second.flight;
        ++flight->waiters;
        s.loaded.wait(lock, [&] { return flight->finished; });
        --flight->waiters;
        if (flight->error) std::rethrow_exception(flight->error);
        ++s.stats.hits;
        return pinned(s.entries.find(key), true);  // the loader counted this waiter's reference
    }
    if (!s.make_room(reserve_bytes)) {
        ++s.stats.rejected;
        throw FieldBudgetExceeded("field cache: '" + key.canonical_field + "' needs " + std::to_string(reserve_bytes) +
                                  " bytes beyond the optional-input budget of " + std::to_string(s.stats.budget_bytes) +
                                  " bytes (active " + std::to_string(s.stats.active_bytes) + ")");
    }
    it = s.entries.emplace(key, Entry{}).first;
    const auto flight = std::make_shared<Flight>();
    it->second.flight = flight;
    s.stats.loading_reserved_bytes += reserve_bytes;
    ++s.stats.loads;
    const auto fail = [&](std::exception_ptr error) {  // with mutex held
        s.entries.erase(it);
        flight->error = error;
        flight->finished = true;
        flight->waiters = 0;
        s.loaded.notify_all();
        std::rethrow_exception(error);
    };
    lock.unlock();
    FieldBuffer buffer;
    try {
        buffer = load();
    } catch (...) {
        lock.lock();
        s.stats.loading_reserved_bytes -= reserve_bytes;
        ++s.stats.failures;
        fail(std::current_exception());
    }
    lock.lock();
    s.stats.loading_reserved_bytes -= reserve_bytes;
    const std::size_t bytes = buffer.bytes();
    if (!s.make_room(bytes)) {
        ++s.stats.rejected;
        fail(std::make_exception_ptr(FieldBudgetExceeded(
            "field cache: '" + key.canonical_field + "' holds " + std::to_string(bytes) +
            " bytes beyond the optional-input budget of " + std::to_string(s.stats.budget_bytes) +
            " bytes (active " + std::to_string(s.stats.active_bytes) + ")")));
    }
    it->second.buffer = std::make_shared<FieldBuffer>(std::move(buffer));
    it->second.bytes = bytes;
    it->second.flight.reset();
    auto result = pinned(it);
    it->second.users += flight->waiters;  // waiters' references, so the entry stays until they take them
    flight->finished = true;
    s.loaded.notify_all();
    return result;
}

void FieldCache::set_budget(std::size_t bytes) {
    std::lock_guard<std::mutex> lock(state_->mutex);
    state_->stats.budget_bytes = bytes;
    state_->make_room(0);
}
void FieldCache::set_resident_bytes(std::size_t bytes) {
    std::lock_guard<std::mutex> lock(state_->mutex);
    state_->stats.resident_bytes = bytes;
}
FieldCacheStats FieldCache::stats() const {
    std::lock_guard<std::mutex> lock(state_->mutex);
    return state_->stats;
}
