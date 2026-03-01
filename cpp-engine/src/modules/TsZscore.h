#pragma once

// MTsZscore: rolling z-score (O(1) incremental per instrument).
// Uses one-pass formula: variance = (sum_sq - sum^2/n) / (n - ddof)
// State: running_sum, running_sum_sq, running_count per instrument.
struct MTsZscore : public Module {
    ModuleInput input;
    IntParameter days;
    IntParameter ddof{1};
    IntParameter min_count{-1};
    FloatParameter eps{1e-12};

    void init() override {
        if (min_count.value < 0)
            min_count = IntParameter(days.value);
        unsigned w = static_cast<unsigned>(days.value);
        input.init(w, numAssets);
        running_sum_.assign(numAssets, 0.0);
        running_sum_sq_.assign(numAssets, 0.0);
        running_count_.assign(numAssets, 0);
        bar_count_ = 0;
    }

    void calculate(unsigned di, SignalVector& output) override {
        unsigned w = static_cast<unsigned>(days.value);
        input.push(output);
        ++bar_count_;

        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float new_val = input(0)[ii];

            // Add incoming
            if (std::isfinite(new_val)) {
                running_sum_[ii] += static_cast<double>(new_val);
                running_sum_sq_[ii] += static_cast<double>(new_val) * new_val;
                running_count_[ii] += 1;
            }

            // Remove outgoing (only after window is full)
            if (bar_count_ > w) {
                float old_val = input(w)[ii];
                if (std::isfinite(old_val)) {
                    running_sum_[ii] -= static_cast<double>(old_val);
                    running_sum_sq_[ii] -= static_cast<double>(old_val) * old_val;
                    running_count_[ii] -= 1;
                }
            }

            int n = running_count_[ii];

            if (n < min_count.value) {
                output[ii] = 0.0f;
                continue;
            }

            float cur = new_val;
            if (!std::isfinite(cur)) {
                output[ii] = std::numeric_limits<float>::quiet_NaN();
                continue;
            }

            double mean = running_sum_[ii] / n;
            int denom = n - ddof.value;
            double variance = (denom > 0)
                ? (running_sum_sq_[ii] - running_sum_[ii] * running_sum_[ii] / n) / denom
                : 0.0;
            if (variance < 0.0) variance = 0.0;  // guard against floating point artifact
            double std_dev = std::sqrt(variance);

            if (std_dev < eps.value) {
                output[ii] = 0.0f;
            } else {
                output[ii] = static_cast<float>((cur - mean) / (std_dev + eps.value));
            }
        }
    }

    void save_state(std::ostream& os) const override {
        input.save(os);
        os.write(reinterpret_cast<const char*>(running_sum_.data()),
                 static_cast<std::streamsize>(numAssets * sizeof(double)));
        os.write(reinterpret_cast<const char*>(running_sum_sq_.data()),
                 static_cast<std::streamsize>(numAssets * sizeof(double)));
        os.write(reinterpret_cast<const char*>(running_count_.data()),
                 static_cast<std::streamsize>(numAssets * sizeof(int)));
        os.write(reinterpret_cast<const char*>(&bar_count_), sizeof(bar_count_));
    }

    void load_state(std::istream& is) override {
        input.load(is);
        is.read(reinterpret_cast<char*>(running_sum_.data()),
                static_cast<std::streamsize>(numAssets * sizeof(double)));
        is.read(reinterpret_cast<char*>(running_sum_sq_.data()),
                static_cast<std::streamsize>(numAssets * sizeof(double)));
        is.read(reinterpret_cast<char*>(running_count_.data()),
                static_cast<std::streamsize>(numAssets * sizeof(int)));
        is.read(reinterpret_cast<char*>(&bar_count_), sizeof(bar_count_));
    }

private:
    StarVector<double> running_sum_;
    StarVector<double> running_sum_sq_;
    StarVector<int> running_count_;
    unsigned bar_count_ = 0;
};
