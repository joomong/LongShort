#pragma once

// MTsZscore: rolling z-score (circular buffer, O(window) per bar).
// O(1) incremental upgrade planned for Phase C.
struct MTsZscore : public Module {
    IntParameter days;
    IntParameter ddof{1};
    IntParameter min_count{-1};   // -1 → resolved to days in init()
    FloatParameter eps{1e-12};

    void init() override {
        if (min_count.value < 0)
            min_count = IntParameter(days.value);
        unsigned w = static_cast<unsigned>(days.value);
        buf_.assign(numAssets, std::vector<float>(w, std::numeric_limits<float>::quiet_NaN()));
        pos_   = 0;
        count_ = 0;
    }

    void calculate(unsigned di, SignalVector& output) override {
        unsigned w = static_cast<unsigned>(days.value);

        for (unsigned ii = 0; ii < numAssets; ++ii)
            buf_[ii][pos_] = output[ii];
        pos_ = (pos_ + 1) % w;
        if (count_ < w)
            ++count_;

        for (unsigned ii = 0; ii < numAssets; ++ii) {
            unsigned cur_slot = (pos_ + w - 1) % w;
            float cur = buf_[ii][cur_slot];

            if (std::isnan(cur)) {
                output[ii] = std::numeric_limits<float>::quiet_NaN();
                continue;
            }

            double sum  = 0.0;
            int n_valid = 0;
            for (unsigned j = 0; j < w; ++j) {
                float v = buf_[ii][j];
                if (!std::isnan(v)) {
                    sum += v;
                    ++n_valid;
                }
            }

            if (n_valid < min_count.value) {
                output[ii] = 0.0f;
                continue;
            }

            double mean = sum / n_valid;
            double sq_sum = 0.0;
            for (unsigned j = 0; j < w; ++j) {
                float v = buf_[ii][j];
                if (!std::isnan(v)) {
                    double diff = v - mean;
                    sq_sum += diff * diff;
                }
            }

            int denom = n_valid - ddof.value;
            double std_dev = (denom > 0) ? std::sqrt(sq_sum / denom) : 0.0;

            if (std_dev < eps.value) {
                output[ii] = 0.0f;
            } else {
                output[ii] = static_cast<float>((cur - mean) / (std_dev + eps.value));
            }
        }
    }

    void save_state(std::ostream& os) const override {
        // TODO: checkpoint for O(1) incremental
    }

    void load_state(std::istream& is) override {
        // TODO: checkpoint for O(1) incremental
    }

private:
    std::vector<std::vector<float>> buf_;
    unsigned pos_ = 0;
    unsigned count_ = 0;
};
