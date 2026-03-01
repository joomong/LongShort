#pragma once

// AVX2-accelerated inner loop for MDecay::calculate.
//
// Only compiled when __AVX2__ is defined (i.e. -mavx2 flag).
// The scalar reference path lives in src/modules/m_decay.cpp.
//
// Calling convention:
//   - `input`  : pointer to the ring-buffer row for `back` steps ago (float*)
//   - `output` : accumulator arrays wsum/wtotal, one entry per asset
//   - `w`      : weight for this back-step (double)
//   - `N`      : number of assets
//
// The AVX2 path processes 8 floats per iteration using 256-bit registers,
// accumulating weighted sums with NaN masking.

#ifdef __AVX2__

#include <immintrin.h>
#include <cstddef>

// Accumulate one ring-buffer row into wsum[]/wtotal[] for all assets.
// weight = (days - back) cast to float for the AVX2 path.
// Caller loops over back in [0, days) and calls this once per row.
inline void mdecay_accumulate_avx2(const float* __restrict__ row,
                                   float* __restrict__ wsum,
                                   float* __restrict__ wtotal,
                                   float weight,
                                   std::size_t N)
{
    __m256 vw = _mm256_set1_ps(weight);
    std::size_t i = 0;
    for (; i + 8 <= N; i += 8) {
        __m256 v    = _mm256_loadu_ps(row + i);
        __m256 ws   = _mm256_loadu_ps(wsum + i);
        __m256 wt   = _mm256_loadu_ps(wtotal + i);

        // mask: finite values only (not NaN, not Inf)
        __m256 mask = _mm256_cmp_ps(v, v, _CMP_ORD_Q); // v == v -> not NaN
        __m256 inf  = _mm256_set1_ps(__builtin_inff());
        __m256 abv  = _mm256_andnot_ps(_mm256_set1_ps(-0.0f), v);
        __m256 fin  = _mm256_cmp_ps(abv, inf, _CMP_LT_OS);
        mask        = _mm256_and_ps(mask, fin);

        __m256 contrib = _mm256_mul_ps(v, vw);
        ws = _mm256_add_ps(ws, _mm256_and_ps(contrib, mask));
        wt = _mm256_add_ps(wt, _mm256_and_ps(vw, mask));

        _mm256_storeu_ps(wsum + i, ws);
        _mm256_storeu_ps(wtotal + i, wt);
    }
    // Scalar tail
    for (; i < N; ++i) {
        float v = row[i];
        if (v == v && v != __builtin_inff() && v != -__builtin_inff()) {
            wsum[i]   += v * weight;
            wtotal[i] += weight;
        }
    }
}

#endif // __AVX2__
