#pragma once

#include "../compiler/combine_spec.h"
#include <string>
#include <vector>

// Member weights for combining a <meta> alpha SET (see combine_spec.h). Estimation uses member gross
// daily returns over the `days` bars ending at t-1; rows before the first estimable bar are EW.
struct MemberReturns {
    std::vector<std::string> paths;                 // member order
    std::vector<std::vector<double>> rows;          // T x K gross daily returns; NaN before sim_start
                                                    // T = trading-period end (sim_end_index or panel end)
    unsigned sim_start = 0, T = 0, K = 0;
};

struct CombineWeights {
    std::vector<std::vector<double>> rows;          // T x K; rows before the first estimable bar are EW
    unsigned fallback_bars = 0;                     // bars that fell back to EW after t0
    unsigned first_estimated_bar = 0;               // t0, or T when no bar could be estimated
};

// Throws std::invalid_argument for an unusable spec ("<spca>: k=12 exceeds the member count 8",
// "<mvo>: days=3000 exceeds the trading bars 2690"); the caller prefixes "meta 'NAME' ".
CombineWeights compute_combine_weights(const MemberReturns& returns, const CombineSpec& spec);

// Every method except EwSign: a member with window sd <= 1e-12 makes the bar an EW fallback.

// Exposed for unit tests. `window` is days x K (row-major, finite). Both work on K x K row-major
// matrices. shrink_covariance returns the covariance used by Mvo/MinVar (Diag uses ddof 1;
// lw/oas/cc use the biased 1/n covariance of the centered window like sklearn / Ledoit-Wolf 2004).
std::vector<double> shrink_covariance(const std::vector<std::vector<double>>& window, const CombineSpec& spec);
// Normalized weights (sum |w| = 1) for one window; false when the estimator fails (EW fallback).
bool weights_for_window(const std::vector<std::vector<double>>& window, const CombineSpec& spec,
                        std::vector<double>& weights);
