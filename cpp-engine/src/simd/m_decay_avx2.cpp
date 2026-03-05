#include "m_decay_avx2.h"

#include "simd_utils.h"

#include <cmath>
#include <cstdint>
#include <limits>

#if defined(__AVX2__)
#include <immintrin.h>
#endif

bool mdecay_runtime_avx2_enabled()
{
#if defined(__AVX2__)
    return has_avx2();
#else
    return false;
#endif
}

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
                             float* output)
{
    for (unsigned k = 0; k < 8; ++k) {
        const unsigned idx = ii + k;
        const float prev_value = past[idx];

        sum[idx] -= diff[idx];
        weights[idx] -= num[idx];

        if (std::isfinite(prev_value)) {
            diff[idx] -= static_cast<double>(prev_value);
            --num[idx];
        }

        const float new_value = curr[idx];
        if (std::isfinite(new_value)) {
            sum[idx] += static_cast<double>(new_value) * days;
            diff[idx] += static_cast<double>(new_value);
            weights[idx] += days;
            ++num[idx];
        }

        if (weights[idx] > 0 && static_cast<std::size_t>(weights[idx]) < div_size)
            output[idx] = static_cast<float>(sum[idx] * div[weights[idx]]);
        else
            output[idx] = std::numeric_limits<float>::quiet_NaN();
    }
}

#if defined(__AVX2__)
static inline uint32_t finite_mask_8(const float* ptr)
{
    const __m256 values = _mm256_loadu_ps(ptr);
    const __m256 ordered = _mm256_cmp_ps(values, values, _CMP_ORD_Q);
    const __m256 abs_mask = _mm256_set1_ps(-0.0f);
    const __m256 abs_values = _mm256_andnot_ps(abs_mask, values);
    const __m256 inf = _mm256_set1_ps(std::numeric_limits<float>::infinity());
    const __m256 lt_inf = _mm256_cmp_ps(abs_values, inf, _CMP_LT_OQ);
    const __m256 finite = _mm256_and_ps(ordered, lt_inf);
    return static_cast<uint32_t>(_mm256_movemask_ps(finite));
}
#endif

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
                           float* output)
{
#if defined(__AVX2__)
    const uint32_t prev_mask = finite_mask_8(past + ii);
    const uint32_t curr_mask = finite_mask_8(curr + ii);

    for (unsigned k = 0; k < 8; ++k) {
        const unsigned idx = ii + k;

        sum[idx] -= diff[idx];
        weights[idx] -= num[idx];

        if ((prev_mask & (1u << k)) != 0u) {
            const float prev_value = past[idx];
            diff[idx] -= static_cast<double>(prev_value);
            --num[idx];
        }

        if ((curr_mask & (1u << k)) != 0u) {
            const float new_value = curr[idx];
            sum[idx] += static_cast<double>(new_value) * days;
            diff[idx] += static_cast<double>(new_value);
            weights[idx] += days;
            ++num[idx];
        }

        if (weights[idx] > 0 && static_cast<std::size_t>(weights[idx]) < div_size)
            output[idx] = static_cast<float>(sum[idx] * div[weights[idx]]);
        else
            output[idx] = std::numeric_limits<float>::quiet_NaN();
    }
#else
    mdecay_process_8_scalar(ii, days, curr, past, weights, num, sum, diff, div, div_size, output);
#endif
}
