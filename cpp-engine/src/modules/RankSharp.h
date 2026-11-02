#pragma once
#include "Rank.h"

// MRankSharp: r = MRank pct output - 0.5 (same mask, NaN and tie rule),
// out = sign(r) * |r|^exp in double, stored float. Derives from MRank so the
// compile_xml universe injection (dynamic_cast<MRank*>) reaches it.
struct MRankSharp : public MRank {
    void init()
    {
        pct = IntParameter(1);
        MRank::init();
    }
    void calculate(unsigned di, SignalVector &output)
    {
        MRank::calculate(di, output);
        const double e = exp;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (!std::isfinite(output[ii])) continue;
            const double r = static_cast<double>(output[ii]) - 0.5;
            const double m = std::pow(std::fabs(r), e);
            output[ii] = static_cast<float>(r > 0.0 ? m : r < 0.0 ? -m : 0.0);
        }
    }

    FloatParameter exp{2.0f};
};
