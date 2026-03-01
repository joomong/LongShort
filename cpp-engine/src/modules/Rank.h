#pragma once

// MRank: cross-sectional percentile rank (stateless).
struct MRank : public Module {
    IntParameter pct{1};  // 1=percentile, 0=raw rank

    void init() override {
        input_copy_.resize(numAssets);
    }

    void calculate(unsigned di, SignalVector& output) override {
        for (unsigned ii = 0; ii < numAssets; ++ii)
            input_copy_[ii] = output[ii];

        int n_finite = 0;
        for (unsigned ii = 0; ii < numAssets; ++ii)
            if (std::isfinite(input_copy_[ii]))
                ++n_finite;

        if (n_finite == 0) {
            output.fill_nan();
            return;
        }

        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float vi = input_copy_[ii];
            if (!std::isfinite(vi)) {
                output[ii] = std::numeric_limits<float>::quiet_NaN();
                continue;
            }

            int n_less = 0, n_equal = 0;
            for (unsigned jj = 0; jj < numAssets; ++jj) {
                float vj = input_copy_[jj];
                if (!std::isfinite(vj)) continue;
                if (vj < vi) ++n_less;
                else if (vj == vi) ++n_equal;
            }

            double rank = n_less + 1.0 + (n_equal - 1.0) / 2.0;
            output[ii] = static_cast<float>(
                pct.value ? rank / n_finite : rank);
        }
    }

private:
    std::vector<float> input_copy_;
};
