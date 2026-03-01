#pragma once

#include <vector>

// StarVector<T>: shim for std::vector<T>
// Later replaceable with a SIMD-aligned allocator without changing call sites.
template<typename T>
using StarVector = std::vector<T>;
