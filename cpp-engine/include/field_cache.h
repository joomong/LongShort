#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

// One optional input field (AFL) read for one run panel: data version (file size and mtime_ns at read
// time), canonical field (the resolved source file), date axis and symbol axis. Axis strings are exact
// serializations, so equal keys mean equal coordinates.
struct FieldKey {
    std::string data_version, canonical_field, date_axis, symbol_axis;
    bool operator<(const FieldKey& other) const;
};

// A float32 T x N row-major array and the coordinates it was read for.
struct FieldBuffer {
    std::vector<float> values;
    std::vector<int64_t> timestamps;
    std::vector<std::string> symbols;
    // values (4 B each) + timestamps (8 B each) + symbol characters
    std::size_t bytes() const;
};

// Thrown when one request's own active fields would exceed the optional-input budget.
struct FieldBudgetExceeded : std::runtime_error {
    using std::runtime_error::runtime_error;
};

// Bytes of fields one request (run panel) holds; read and written under the cache mutex only.
struct FieldRun {
    std::size_t bytes = 0;
};

struct FieldCacheStats {
    std::size_t resident_bytes = 0;          // base inputs and loaded parent AFL columns, process lifetime
    std::size_t active_bytes = 0;            // fields held by at least one run (cached and anonymous)
    std::size_t idle_bytes = 0;              // fields only the cache holds, evictable
    std::size_t loading_reserved_bytes = 0;  // reservations of reads in progress (array + decode peak)
    std::size_t budget_bytes = 0;
    std::size_t peak_loading_reserved_bytes = 0;
    std::size_t peak_optional_bytes = 0;     // highest active + idle + loading_reserved seen
    std::uint64_t loads = 0, evictions = 0, hits = 0, rejected = 0, failures = 0;
    std::uint64_t over_budget_events = 0;    // increases that left active + idle + reserved above the budget
};

namespace field_cache_testing {
// Called at allocation sites ("pin", "entry", "publish", "adopt"); tests make it throw. Null in production.
extern void (*fault)(const char* site);
}

// Optional-input field cache. The cache keeps its own reference to each loaded buffer; acquire hands out
// run references. A field with run references is active and never evicted; when its last run reference goes
// it becomes idle. The budget is soft: idle fields go least recently used first whenever active + idle +
// loading reservations exceed it, but active fields of concurrent requests may exceed it for a while (counted
// in over_budget_events). A request is refused only when its own active fields (FieldRun) would exceed the
// budget. Bytes return to the allocator when the last reference goes. Concurrent first requests for a key
// share one load; a failed load is removed, its waiters see the failure, and a later request retries.
class FieldCache {
public:
    using Loader = std::function<FieldBuffer()>;
    explicit FieldCache(std::size_t budget_bytes);
    ~FieldCache();
    FieldCache(const FieldCache&) = delete;
    FieldCache& operator=(const FieldCache&) = delete;

    // reserve_bytes: bytes the read may need at its peak, held against the budget while it runs.
    // read_lock: taken before the reservation and held through load(), so queued reads hold no reservation.
    std::shared_ptr<const FieldBuffer> acquire(const FieldKey& key, const Loader& load, std::size_t reserve_bytes = 0,
                                               const std::shared_ptr<FieldRun>& run = {},
                                               std::mutex* read_lock = nullptr);
    // A private buffer counted as active until its run reference goes; never kept idle.
    std::shared_ptr<const FieldBuffer> adopt(FieldBuffer buffer, const std::shared_ptr<FieldRun>& run = {});
    void set_budget(std::size_t bytes);
    void set_resident_bytes(std::size_t bytes);
    void add_resident_bytes(std::size_t bytes);
    FieldCacheStats stats() const;

    struct State;
private:
    std::shared_ptr<State> state_;
};

// Exact axis serializations for FieldKey.
std::string field_axis_key(const std::vector<int64_t>& timestamps);
std::string field_axis_key(const std::vector<std::string>& symbols);
