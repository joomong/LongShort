#pragma once

#include "../data/data_panel.h"
#include "../engine/simulator.h"
#include <cmath>
#include <algorithm>
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
    OrderedSignalAccumulator(const DataPanel& data,
                             MissingClosePolicy policy,
                             const std::vector<uint8_t>* universe = nullptr)
        : data_(data), policy_(policy), universe_(universe),
          signals_(data.T, std::vector<float>(data.N, std::numeric_limits<float>::quiet_NaN())),
          next_alpha_(data.T, 0) {}

    void add(std::size_t alpha_index, unsigned di, const float* row, unsigned n,
             std::vector<double>& normalized_scratch) {
        if (di >= data_.T || n != data_.N)
            throw std::invalid_argument("Combined signal dimensions must match the data panel");
        normalize_signal_row(data_, di, row, normalized_scratch, policy_, universe_);
        std::unique_lock<std::mutex> lock(mutex_);
        ready_.wait(lock, [&] { return cancelled_ || next_alpha_[di] == alpha_index; });
        if (cancelled_)
            throw std::runtime_error("Combined evaluation cancelled after an alpha failed");
        auto& combined = signals_[di];
        for (unsigned ii = 0; ii < n; ++ii) {
            const float contribution = static_cast<float>(normalized_scratch[ii]);
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

    // Reduce a bounded block of complete rows under one lock. Alpha order
    // within every row remains identical to add(), with fewer wakeups.
    void add_block(std::size_t alpha_index, unsigned first_di,
                   std::vector<float>& rows, unsigned n,
                   std::vector<double>& scratch) {
        if (!n || n != data_.N || rows.size() % n || first_di > data_.T ||
            rows.size() / n > data_.T - first_di)
            throw std::invalid_argument("Combined signal dimensions must match the data panel");
        const unsigned count = static_cast<unsigned>(rows.size() / n);
        if (!count) return;
        for (unsigned offset = 0; offset < count; ++offset) {
            float* row = rows.data() + static_cast<size_t>(offset) * n;
            normalize_signal_row(data_, first_di + offset, row, scratch, policy_, universe_);
            for (unsigned ii = 0; ii < n; ++ii) row[ii] = static_cast<float>(scratch[ii]);
        }
        std::unique_lock<std::mutex> lock(mutex_);
        ready_.wait(lock, [&] {
            return cancelled_ || std::all_of(next_alpha_.begin() + first_di,
                next_alpha_.begin() + first_di + count,
                [&](size_t next) { return next == alpha_index; });
        });
        if (cancelled_) throw std::runtime_error("Combined evaluation cancelled after an alpha failed");
        for (unsigned offset = 0; offset < count; ++offset) {
            auto& combined = signals_[first_di + offset];
            const float* row = rows.data() + static_cast<size_t>(offset) * n;
            for (unsigned ii = 0; ii < n; ++ii) {
                if (!std::isfinite(row[ii])) continue;
                float& value = combined[ii];
                const float sum = std::isfinite(value) ? value + row[ii] : row[ii];
                if (!std::isfinite(sum)) throw std::overflow_error("Combined signal sum overflow at bar " +
                    std::to_string(first_di + offset) + ", asset " + std::to_string(ii));
                value = sum;
            }
            ++next_alpha_[first_di + offset];
        }
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
    MissingClosePolicy policy_;
    const std::vector<uint8_t>* universe_;
    std::vector<std::vector<float>> signals_;
    std::vector<std::size_t> next_alpha_;
    std::mutex mutex_;
    std::condition_variable ready_;
    bool cancelled_ = false;
};
