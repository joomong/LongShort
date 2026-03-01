#pragma once

// MTsRank: rolling percentile rank (O(days) comparison counting per instrument).
struct MTsRank : public Module {
    IntParameter days;
    IntParameter pct{1};          // 1=percentile, 0=raw rank
    IntParameter ascending{0};    // 0=descending (big value → high rank)
    IntParameter min_count{-1};   // -1 → resolved to days in init()

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

            int n_valid = 0, n_less = 0, n_equal = 0;
            for (unsigned j = 0; j < w; ++j) {
                float v = buf_[ii][j];
                if (std::isnan(v)) continue;
                ++n_valid;
                if (ascending.value) {
                    if (v < cur) ++n_less;
                    else if (v == cur) ++n_equal;
                } else {
                    if (v > cur) ++n_less;
                    else if (v == cur) ++n_equal;
                }
            }

            if (n_valid < min_count.value) {
                output[ii] = std::numeric_limits<float>::quiet_NaN();
                continue;
            }

            double rank = (n_less + 1 + (n_equal - 1) / 2.0);
            output[ii] = static_cast<float>(
                pct.value ? rank / n_valid : rank);
        }
    }

    void save_state(std::ostream& os) const override {
        // circular buffer state
    }

    void load_state(std::istream& is) override {
        // circular buffer state
    }

private:
    std::vector<std::vector<float>> buf_;
    unsigned pos_ = 0;
    unsigned count_ = 0;
};
