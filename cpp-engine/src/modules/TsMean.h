#pragma once

// MTsMean: rolling mean over a window (O(1) incremental per instrument).
// Uses ModuleInput ring buffer for outgoing value access.
// State: running_sum, running_count per instrument.
struct MTsMean : public Module {
    ModuleInput input;
    IntParameter days;

    void init() override {
        unsigned w = static_cast<unsigned>(days.value);
        input.init(w, numAssets);
        running_sum_.assign(numAssets, 0.0);
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
                running_count_[ii] += 1;
            }

            // Remove outgoing (only after window is full)
            if (bar_count_ > w) {
                float old_val = input(w)[ii];
                if (std::isfinite(old_val)) {
                    running_sum_[ii] -= static_cast<double>(old_val);
                    running_count_[ii] -= 1;
                }
            }

            // Output
            if (bar_count_ < w) {
                output[ii] = std::numeric_limits<float>::quiet_NaN();
            } else if (running_count_[ii] > 0) {
                output[ii] = static_cast<float>(running_sum_[ii] / running_count_[ii]);
            } else {
                output[ii] = std::numeric_limits<float>::quiet_NaN();
            }
        }
    }

    void save_state(std::ostream& os) const override {
        input.save(os);
        os.write(reinterpret_cast<const char*>(running_sum_.data()),
                 static_cast<std::streamsize>(numAssets * sizeof(double)));
        os.write(reinterpret_cast<const char*>(running_count_.data()),
                 static_cast<std::streamsize>(numAssets * sizeof(int)));
        os.write(reinterpret_cast<const char*>(&bar_count_), sizeof(bar_count_));
    }

    void load_state(std::istream& is) override {
        input.load(is);
        is.read(reinterpret_cast<char*>(running_sum_.data()),
                static_cast<std::streamsize>(numAssets * sizeof(double)));
        is.read(reinterpret_cast<char*>(running_count_.data()),
                static_cast<std::streamsize>(numAssets * sizeof(int)));
        is.read(reinterpret_cast<char*>(&bar_count_), sizeof(bar_count_));
    }

private:
    StarVector<double> running_sum_;
    StarVector<int> running_count_;
    unsigned bar_count_ = 0;
};
