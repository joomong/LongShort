#pragma once

#include <cstddef>

// Build-time selection: true when LONGSHORT_ENABLE_AVX2 is enabled.
bool mdecay_avx2_enabled();

// Scalar reference update for 8 instruments [ii, ii+7].
void mdecay_process_8_scalar(unsigned ii,
                             int days,
                             const float* curr,
                             const float* past,
                             int* weights,
                             int* num,
                             double* sum,
                             double* diff,
                             const double* div,
                             std::size_t div_size,
                             float* output);

// AVX2-assisted update for 8 instruments [ii, ii+7].
// AVX2 builds require an AVX2-capable host; disabled builds use the scalar kernel.
void mdecay_process_8_avx2(unsigned ii,
                           int days,
                           const float* curr,
                           const float* past,
                           int* weights,
                           int* num,
                           double* sum,
                           double* diff,
                           const double* div,
                           std::size_t div_size,
                           float* output);
