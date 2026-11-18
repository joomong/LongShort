#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

// One optional input field (AFL) read for one run panel: data version, canonical field (its source file),
// date axis and symbol axis. Axis strings are exact serializations, so equal keys mean equal coordinates.
struct FieldKey {
    std::string data_version, canonical_field, date_axis, symbol_axis;
    bool operator<(const FieldKey& other) const;
};

// A float32 T x N row-major array and the coordinates it was read for.
struct FieldBuffer {
    std::vector<float> values;
    std::vector<int64_t> timestamps;
    std::vector<std::string> symbols;
    std::size_t bytes() const { return values.size() * sizeof(float); }
};

// Thrown when a field cannot be held within the optional-input budget even after every idle field is gone.
struct FieldBudgetExceeded : std::runtime_error {
    using std::runtime_error::runtime_error;
};

struct FieldCacheStats {
    std::size_t resident_bytes = 0;          // base inputs held for the process lifetime (reported only)
    std::size_t active_bytes = 0;            // fields held by at least one run
    std::size_t idle_bytes = 0;              // fields only the cache holds, evictable
    std::size_t loading_reserved_bytes = 0;  // reservations of loads in flight (array + decode peak)
    std::size_t budget_bytes = 0;            // optional-input budget: active + idle + loading_reserved <= budget
    std::uint64_t loads = 0, evictions = 0, hits = 0, rejected = 0, failures = 0;
};

// Optional-input field cache. The cache keeps its own reference to each loaded buffer; acquire hands out run
// references. A field with run references is active and never evicted; when its last run reference goes it
// becomes idle, and idle fields go least recently used first whenever active + idle + loading reservations
// exceed the budget. Bytes return to the allocator when the last reference (run or cache) is dropped.
// Concurrent first requests for one key share a single load; a failed load is removed and can be retried.
// A request that cannot fit beside the active fields throws FieldBudgetExceeded; references the caller
// already holds stay valid and are released by the caller.
class FieldCache {
public:
    using Loader = std::function<FieldBuffer()>;
    explicit FieldCache(std::size_t budget_bytes);
    ~FieldCache();
    FieldCache(const FieldCache&) = delete;
    FieldCache& operator=(const FieldCache&) = delete;

    // reserve_bytes: bytes the load may need at its peak (new array + decode), held against the budget
    // until the load finishes; 0 checks the budget with the actual size once read.
    std::shared_ptr<const FieldBuffer> acquire(const FieldKey& key, const Loader& load, std::size_t reserve_bytes = 0);
    void set_budget(std::size_t bytes);
    void set_resident_bytes(std::size_t bytes);
    FieldCacheStats stats() const;

private:
    struct State;
    std::shared_ptr<State> state_;
};

// Exact axis serializations for FieldKey.
std::string field_axis_key(const std::vector<int64_t>& timestamps);
std::string field_axis_key(const std::vector<std::string>& symbols);
