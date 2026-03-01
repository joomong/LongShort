#pragma once

#include <cmath>
#include <cstring>
#include <limits>

// SignalVector: float array wrapper for module I/O
// Holds a non-owning view into a contiguous float buffer.
struct SignalVector {
    float*   data;
    unsigned size;

    float& operator[](unsigned i) { return data[i]; }
    const float& operator[](unsigned i) const { return data[i]; }

    // Fill all elements with NaN
    void fill_nan() {
        for (unsigned i = 0; i < size; ++i)
            data[i] = std::numeric_limits<float>::quiet_NaN();
    }
};
