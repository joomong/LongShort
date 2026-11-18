#pragma once
#include "SignalInputs.h"

// MBucketDemean: <bucketdemean signal_1="x" signal_2="by" n="5"/> (stateless).
// Cells: run universe, x and by finite. by is ranked per bar (average rank for ties),
// pct = rank / count, bucket = ceil(pct * n) clamped to 1..n; output x minus its bucket mean.
struct MBucketDemean : public Module, public SignalInputs {
    int n_buckets = 5;
    const DataPanel* panel = nullptr;
    const std::vector<uint8_t>* universe = nullptr;

    unsigned min_signal_inputs() const override { return 2; }
    unsigned max_signal_inputs() const override { return 2; }
    void init() override {}
    void calculate(unsigned di, SignalVector& output) override
    {
        const float nan = std::numeric_limits<float>::quiet_NaN();
        const auto* sel = universe ? universe : panel ? &panel->universe_mask : nullptr;
        const uint8_t* m = sel && !sel->empty() ? sel->data() + static_cast<size_t>(di) * numAssets : nullptr;
        const float* x = signal_row(0);
        const float* by = signal_row(1);
        std::vector<std::pair<float, unsigned>> cells;
        cells.reserve(numAssets);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            output[ii] = nan;
            if ((!m || m[ii]) && std::isfinite(x[ii]) && std::isfinite(by[ii])) cells.emplace_back(by[ii], ii);
        }
        const size_t n = cells.size();
        if (n == 0) return;
        std::sort(cells.begin(), cells.end());
        std::vector<double> sum(n_buckets, 0.0);
        std::vector<int> count(n_buckets, 0);
        std::vector<int> bucket(numAssets, 0);
        for (size_t begin = 0; begin < n;) {
            size_t end = begin + 1;
            while (end < n && cells[end].first == cells[begin].first) ++end;
            const double pct = (static_cast<double>(begin) + 1.0 +
                                static_cast<double>(end - begin - 1) / 2.0) / static_cast<double>(n);
            const int b = std::min(n_buckets - 1, std::max(0, static_cast<int>(std::ceil(pct * n_buckets)) - 1));
            for (size_t i = begin; i < end; ++i) {
                bucket[cells[i].second] = b;
                sum[b] += x[cells[i].second];
                ++count[b];
            }
            begin = end;
        }
        for (const auto& cell : cells) {
            const int b = bucket[cell.second];
            output[cell.second] = static_cast<float>(x[cell.second] - sum[b] / count[b]);
        }
    }
};
