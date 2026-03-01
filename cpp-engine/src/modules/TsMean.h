#pragma once

// MTsMean: rolling mean over a window (circular buffer, O(window) per bar).
// O(1) incremental upgrade planned for Phase C.
struct MTsMean : public Module {
    IntParameter days;

    void init() override {
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

        if (count_ < w) {
            output.fill_nan();
            return;
        }

        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float sum   = 0.0f;
            int   valid = 0;
            for (unsigned j = 0; j < w; ++j) {
                float v = buf_[ii][j];
                if (!std::isnan(v)) {
                    sum += v;
                    ++valid;
                }
            }
            output[ii] = (valid > 0)
                ? sum / static_cast<float>(valid)
                : std::numeric_limits<float>::quiet_NaN();
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
