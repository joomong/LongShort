#pragma once

// MTsFill: bounded forward fill. A finite input is output as is; a non-finite input (NaN, +-inf) outputs the
// last finite input while at most `days` bars have passed since it, otherwise NaN (also before the first finite input).
// State per instrument: last value and its bar. O(1) per instrument.
struct MTsFill : public Module {
    unsigned lookback(unsigned) const override { return static_cast<unsigned>((int)days); }
    void init()
    {
        last_value.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
        last_valid_bar.assign(numAssets, kNone);
    }
    void calculate(unsigned di, SignalVector &output)
    {
        const unsigned d = static_cast<unsigned>((int)days);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            const float x = output[ii];
            if (std::isfinite(x)) {
                last_value[ii] = x;
                last_valid_bar[ii] = di;
            } else {
                output[ii] = last_valid_bar[ii] != kNone && di - last_valid_bar[ii] <= d
                                 ? last_value[ii] : std::numeric_limits<float>::quiet_NaN();
            }
        }
    }

    static constexpr unsigned kNone = std::numeric_limits<unsigned>::max();
    std::vector<float> last_value;
    std::vector<unsigned> last_valid_bar;
    IntParameter days{20};
};
