#pragma once

#include <cstdio>
#include <cstdlib>
#include <string>

// How a <meta> alpha SET is turned into one signal row per bar. Every method weights the members'
// gross-1 rows with w[t,k]; EW is w = 1/K. Estimation windows are member daily gross returns over
// the `days` bars that end at t-1 (never bar t), see docs/math_contract.md "Meta portfolios".
enum class CombineMethod { Ew, EwSign, Mvo, MinVar, Erc, Ivol, Spca, Glasso };
enum class ShrinkMode { Diag, LedoitWolf, ConstantCorrelation, Oas };

struct CombineSpec {
    CombineMethod method = CombineMethod::Ew;
    unsigned days = 0;                     // estimation window in bars (EwSign: the sign window)
    unsigned rebalance = 1;                // recompute the weights every N bars (>= 1)
    ShrinkMode shrink = ShrinkMode::Diag;  // Mvo / MinVar covariance shrinkage
    double delta = 0.5;                    // Diag shrink intensity in [0, 1]; ignored by the automatic modes
    unsigned k = 0;                        // Spca: number of principal components (>= 1)
    double alpha = 0.0;                    // Glasso: L1 penalty (> 0)

    bool is_ew() const { return method == CombineMethod::Ew; }

    // Canonical text, used for cache keys and the response payload. Fixed format, no locale.
    std::string key() const {
        std::string out = method_name(method);
        if (method == CombineMethod::Ew) return out;
        out += " days=" + std::to_string(days);
        if (method == CombineMethod::Mvo || method == CombineMethod::MinVar) {
            out += std::string(" shrink=") + shrink_name(shrink);
            if (shrink == ShrinkMode::Diag) out += " delta=" + fixed6(delta);
        }
        if (method == CombineMethod::Spca) out += " k=" + std::to_string(k);
        if (method == CombineMethod::Glasso) out += " alpha=" + fixed6(alpha);
        out += " rebalance=" + std::to_string(rebalance);
        return out;
    }

    static const char* method_name(CombineMethod m) {
        switch (m) {
            case CombineMethod::Ew: return "ew";
            case CombineMethod::EwSign: return "ewsign";
            case CombineMethod::Mvo: return "mvo";
            case CombineMethod::MinVar: return "minvar";
            case CombineMethod::Erc: return "erc";
            case CombineMethod::Ivol: return "ivol";
            case CombineMethod::Spca: return "spca";
            case CombineMethod::Glasso: return "glasso";
        }
        return "?";
    }
    static const char* shrink_name(ShrinkMode s) {
        switch (s) {
            case ShrinkMode::Diag: return "diag";
            case ShrinkMode::LedoitWolf: return "lw";
            case ShrinkMode::ConstantCorrelation: return "cc";
            case ShrinkMode::Oas: return "oas";
        }
        return "?";
    }

private:
    // Shortest text that round-trips to the same double ("0.5", "0.1", "1e-07", "0.3333333333");
    // -0 prints as 0 so equal values share one key.
    static std::string fixed6(double value) {
        if (value == 0.0) value = 0.0;
        char buffer[64];
        for (int precision = 1; precision <= 17; ++precision) {
            std::snprintf(buffer, sizeof buffer, "%.*g", precision, value);
            if (std::strtod(buffer, nullptr) == value) break;
        }
        return buffer;
    }
};
