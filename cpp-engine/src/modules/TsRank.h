#pragma once

// MTsRank: rolling percentile rank (O(days) comparison counting per instrument).
struct MTsRank : public Module {
    void init()
    {
        if (min_count < 0)
            min_count = IntParameter(days);
        unsigned w = (unsigned)days;
        buf.assign(numAssets, std::vector<float>(w, std::numeric_limits<float>::quiet_NaN()));
        pos = 0;
        count = 0;
    }
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        unsigned w = (unsigned)days;

        for (unsigned ii = 0; ii < numAssets; ++ii)
            buf[ii][pos] = output[ii];
        pos = (pos + 1) % w;
        if (count < w) ++count;

        for (unsigned ii = 0; ii < numAssets; ++ii) {
            unsigned cur_slot = (pos + w - 1) % w;
            float cur = buf[ii][cur_slot];

            if (std::isnan(cur)) {
                output[ii] = std::numeric_limits<float>::quiet_NaN();
                continue;
            }

            int n_valid = 0, n_less = 0, n_equal = 0;
            for (unsigned j = 0; j < w; ++j) {
                float v = buf[ii][j];
                if (std::isnan(v)) continue;
                ++n_valid;
                if (v < cur) ++n_less;
                else if (v == cur) ++n_equal;
            }

            if (n_valid < min_count) {
                output[ii] = std::numeric_limits<float>::quiet_NaN();
                continue;
            }

            double rank = (n_less + 1 + (n_equal - 1) / 2.0);
            output[ii] = (float)(pct ? rank / n_valid : rank);
        }
    }
    void checkpoint(boost::archive::binary_iarchive &ar)
    {
        ar & buf & pos & count;
    }
    void checkpoint(boost::archive::binary_oarchive &ar)
    {
        ar & buf & pos & count;
    }

    std::vector<std::vector<float>> buf;
    unsigned pos = 0;
    unsigned count = 0;
    IntParameter days;
    IntParameter pct{1};
    IntParameter min_count{-1};
};
