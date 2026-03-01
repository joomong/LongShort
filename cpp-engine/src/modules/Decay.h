#pragma once

// MDecay: linear decay weighted average (O(1) incremental per instrument).
// out[ii] = (in[di]*days + in[di-1]*(days-1) + ... + in[di-days+1]*1) / weighted_sum
//
// State: sum, diff, weights, num per instrument.
// Auxiliary: div[] = 1/i reciprocal table.
//
// Update rule per bar per instrument:
//   1. Age:    sum -= diff (each value's weight decreases by 1), weights -= num
//   2. Remove: if past value finite: diff -= past, num -= 1
//   3. Add:    if new value finite: sum += new*days, diff += new, weights += days, num += 1
//   4. Output: sum * div[weights]
struct MDecay : public Module {
    ModuleInput input;
    IntParameter days;

    void init() override {
        input.init(static_cast<unsigned>(days.value), numAssets);
        sum_.assign(numAssets, 0.0);
        diff_.assign(numAssets, 0.0);
        weights_.assign(numAssets, 0);
        num_.assign(numAssets, 0);
        int n = days.value * (days.value + 1) / 2 + 1;
        div_.resize(n, 0.0);
        for (int i = 1; i < n; ++i)
            div_[i] = 1.0 / static_cast<double>(i);
        setting_output_to_nan_is_not_necessary();
    }

    void calculate(unsigned di, SignalVector& output) override {
        input.push(output);
        int d = days.value;

        for (unsigned ii = 0; ii < numAssets; ++ii) {
            // 1. Age
            sum_[ii] -= diff_[ii];
            weights_[ii] -= num_[ii];

            // 2. Remove outgoing (value from `days` bars ago)
            float past = input(static_cast<unsigned>(d))[ii];
            if (std::isfinite(past)) {
                diff_[ii] -= static_cast<double>(past);
                num_[ii] -= 1;
            }

            // 3. Add incoming (current bar's value)
            float new_val = input(0)[ii];
            if (std::isfinite(new_val)) {
                sum_[ii] += static_cast<double>(new_val) * d;
                diff_[ii] += static_cast<double>(new_val);
                weights_[ii] += d;
                num_[ii] += 1;
            }

            // 4. Output
            if (weights_[ii] > 0 && weights_[ii] < static_cast<int>(div_.size()))
                output[ii] = static_cast<float>(sum_[ii] * div_[weights_[ii]]);
            else
                output[ii] = std::numeric_limits<float>::quiet_NaN();
        }
    }

    void save_state(std::ostream& os) const override {
        input.save(os);
        os.write(reinterpret_cast<const char*>(sum_.data()),
                 static_cast<std::streamsize>(numAssets * sizeof(double)));
        os.write(reinterpret_cast<const char*>(diff_.data()),
                 static_cast<std::streamsize>(numAssets * sizeof(double)));
        os.write(reinterpret_cast<const char*>(weights_.data()),
                 static_cast<std::streamsize>(numAssets * sizeof(int)));
        os.write(reinterpret_cast<const char*>(num_.data()),
                 static_cast<std::streamsize>(numAssets * sizeof(int)));
    }

    void load_state(std::istream& is) override {
        input.load(is);
        is.read(reinterpret_cast<char*>(sum_.data()),
                static_cast<std::streamsize>(numAssets * sizeof(double)));
        is.read(reinterpret_cast<char*>(diff_.data()),
                static_cast<std::streamsize>(numAssets * sizeof(double)));
        is.read(reinterpret_cast<char*>(weights_.data()),
                static_cast<std::streamsize>(numAssets * sizeof(int)));
        is.read(reinterpret_cast<char*>(num_.data()),
                static_cast<std::streamsize>(numAssets * sizeof(int)));
    }

private:
    StarVector<double> sum_;
    StarVector<double> diff_;
    StarVector<int> weights_;
    StarVector<int> num_;
    StarVector<double> div_;
};
