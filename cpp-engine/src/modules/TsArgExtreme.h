#pragma once

// MTsArgExtreme<IsMax>: lag (0..days-1) of the max (tsargmax) / min (tsargmin) finite value in
// the window; ties -> most recent (smallest lag). NaN if fewer than min_count finite values.
// Amortized O(1) per instrument: monotonic deque per instrument in a preallocated ring of
// `days` slots (bar index + value).
// With Value=true the same deque yields the extreme value itself (tsmax / tsmin).
template <bool IsMax, bool Value = false>
struct MTsArgExtreme : public Module {
    unsigned lookback(unsigned) const override { return (int)days > 1 ? (int)days - 1 : 0; }
    void init()
    {
        if (min_count < 0)
            min_count = IntParameter(days);
        unsigned w = (unsigned)days;
        dq_bar.assign((size_t)numAssets * w, 0u);
        dq_val.assign((size_t)numAssets * w, 0.0f);
        dq_head.assign(numAssets, 0u);
        dq_size.assign(numAssets, 0u);
        finite_flag.assign((size_t)numAssets * w, 0);
        running_count.assign(numAssets, 0);
        bar_count = 0;
    }
    void calculate(unsigned /*di*/, SignalVector &output)
    {
        const unsigned w = (unsigned)days;
        const unsigned bar = bar_count++;
        const unsigned slot = bar % w;

        parallel_for_assets(numAssets, std::min(64u, 3u * w), [&](unsigned begin, unsigned end) {
            for (unsigned ii = begin; ii < end; ++ii) {
                unsigned* qb = dq_bar.data() + (size_t)ii * w;
                float* qv = dq_val.data() + (size_t)ii * w;
                unsigned& head = dq_head[ii];
                unsigned& size = dq_size[ii];
                char& flag = finite_flag[(size_t)ii * w + slot];  // holds the bar leaving the window

                if (bar >= w && flag)
                    running_count[ii] -= 1;
                // Expire the front entry that left the window (at most one per bar).
                if (size > 0 && qb[head] + w <= bar) {
                    head = (head + 1) % w;
                    --size;
                }

                float v = output[ii];
                flag = std::isfinite(v) ? 1 : 0;
                if (flag) {
                    running_count[ii] += 1;
                    // Pop dominated entries; ties are popped so the most recent survives.
                    while (size > 0) {
                        float back = qv[(head + size - 1) % w];
                        if (IsMax ? (back <= v) : (back >= v))
                            --size;
                        else
                            break;
                    }
                    unsigned pos = (head + size) % w;
                    qb[pos] = bar;
                    qv[pos] = v;
                    ++size;
                }

                if (running_count[ii] < min_count || size == 0)
                    output[ii] = std::numeric_limits<float>::quiet_NaN();
                else
                    output[ii] = Value ? qv[head] : (float)(bar - qb[head]);
            }
        });
    }

    std::vector<unsigned> dq_bar;
    std::vector<float> dq_val;
    std::vector<unsigned> dq_head;
    std::vector<unsigned> dq_size;
    std::vector<char> finite_flag;
    std::vector<int> running_count;
    unsigned bar_count = 0;
    IntParameter days{20};
    IntParameter min_count{-1};
};

using MTsArgMax = MTsArgExtreme<true>;
using MTsArgMin = MTsArgExtreme<false>;
using MTsMax = MTsArgExtreme<true, true>;
using MTsMin = MTsArgExtreme<false, true>;
