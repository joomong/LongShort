#pragma once

// MZscore: cross-sectional z-score (stateless).
struct MZscore : public Module {
    IntParameter ddof{1};
    IntParameter min_count{2};
    FloatParameter eps{1e-12};

    void init() override {}

    void calculate(unsigned di, SignalVector& output) override {
        double sum   = 0.0;
        int    count = 0;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (std::isfinite(output[ii])) {
                sum += output[ii];
                ++count;
            }
        }

        if (count < min_count.value) {
            output.fill_nan();
            return;
        }

        double mean = sum / count;
        double sq_sum = 0.0;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (std::isfinite(output[ii])) {
                double diff = output[ii] - mean;
                sq_sum += diff * diff;
            }
        }

        double denom = static_cast<double>(count - ddof.value);
        double scale = (denom > 0.0) ? std::sqrt(sq_sum / denom) : 0.0;

        if (scale < eps.value) {
            output.fill_nan();
            return;
        }

        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (std::isfinite(output[ii]))
                output[ii] = static_cast<float>((output[ii] - mean) / (scale + eps.value));
            else
                output[ii] = std::numeric_limits<float>::quiet_NaN();
        }
    }
};
