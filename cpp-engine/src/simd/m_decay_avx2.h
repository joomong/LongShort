#pragma once

#include <cstddef>

// Runtime gate for using AVX2 kernel safely on the current host.
bool mdecay_runtime_avx2_enabled();

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
// The function is compiled with AVX2 where available; callers must guard with
// mdecay_runtime_avx2_enabled() to avoid illegal instructions on old CPUs.
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
