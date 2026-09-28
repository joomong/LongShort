#pragma once

#include "../data/data_panel.h"
#include "../engine/simulator.h"
#include <cmath>
#include <condition_variable>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

// A worker can advance only after every earlier alpha has contributed to this
// row. At least the earliest unfinished alpha can always advance. Memory does
// not grow with alpha count; cancellation wakes workers after an evaluation error.
class OrderedSignalAccumulator {
public:
    OrderedSignalAccumulator(const DataPanel& data, const std::string& mode,
                             MissingClosePolicy policy,
                             const std::vector<uint8_t>* universe = nullptr)
        : data_(data), normalized_(mode == "normalized"), policy_(policy), universe_(universe),
          signals_(data.T, std::vector<float>(data.N, std::numeric_limits<float>::quiet_NaN())),
          next_alpha_(data.T, 0) {
        if (mode != "normalized" && mode != "raw")
            throw std::invalid_argument("combinedMode must be 'normalized' or 'raw'");
    }

    void add(std::size_t alpha_index, unsigned di, const float* row, unsigned n,
             std::vector<double>& normalized_scratch) {
        if (di >= data_.T || n != data_.N)
            throw std::invalid_argument("Combined signal dimensions must match the data panel");
        if (normalized_)
            normalize_signal_row(data_, di, row, normalized_scratch, policy_, universe_);
        std::unique_lock<std::mutex> lock(mutex_);
        ready_.wait(lock, [&] { return cancelled_ || next_alpha_[di] == alpha_index; });
        if (cancelled_)
            throw std::runtime_error("Combined evaluation cancelled after an alpha failed");
        auto& combined = signals_[di];
        for (unsigned ii = 0; ii < n; ++ii) {
            const float contribution = normalized_ ? static_cast<float>(normalized_scratch[ii]) : row[ii];
            if (!std::isfinite(contribution))
                continue;
            float& value = combined[ii];
            const float sum = std::isfinite(value) ? value + contribution : contribution;
            if (!std::isfinite(sum))
                throw std::overflow_error("Combined signal sum overflow at bar " +
                    std::to_string(di) + ", asset " + std::to_string(ii));
            value = sum;
        }
        ++next_alpha_[di];
        lock.unlock();
        ready_.notify_all();
    }

    void cancel() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            cancelled_ = true;
        }
        ready_.notify_all();
    }

    std::vector<std::vector<float>> take_signals() { return std::move(signals_); }

private:
    const DataPanel& data_;
    bool normalized_;
    MissingClosePolicy policy_;
    const std::vector<uint8_t>* universe_;
    std::vector<std::vector<float>> signals_;
    std::vector<std::size_t> next_alpha_;
    std::mutex mutex_;
    std::condition_variable ready_;
    bool cancelled_ = false;
};
