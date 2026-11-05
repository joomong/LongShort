#pragma once

#include <vector>
#include <limits>
#include <cmath>
#include <string>
#include <utility>
#include "signal_vector.h"
#include "parallel_executor.h"
#include "star_vector.h"

// ---------------------------------------------------------------------------
// Parameter types (STAR convention)
// ---------------------------------------------------------------------------

struct IntParameter {
    int value = 0;
    IntParameter() = default;
    explicit IntParameter(int v) : value(v) {}
    operator int() const { return value; }
};

struct FloatParameter {
    float value = 0.0f;
    FloatParameter() = default;
    explicit FloatParameter(float v) : value(v) {}
    operator float() const { return value; }
};

// Group name (sector|industry) validated like built-in group=; empty = no group.
// The module looks the panel field up itself (the name is the field name).
struct GroupParameter {
    std::string value;
    GroupParameter() = default;
    explicit GroupParameter(std::string v) : value(std::move(v)) {}
    operator const std::string&() const { return value; }
    const std::string& name() const { return value; }
};

// ---------------------------------------------------------------------------
// ModuleInput: ring buffer giving lookback access to pushed SignalVectors
// ---------------------------------------------------------------------------

class ModuleInput {
public:
    struct RowView {
        const float* ptr = nullptr;
        unsigned size = 0;

        const float* data() const { return ptr; }
        float operator[](unsigned i) const { return ptr[i]; }
    };

    unsigned maxLookBack = 0;

    ModuleInput() : num_assets_(0), head_(0), push_count_(0) {}

    void push(const SignalVector& sv) {
        // Lazy allocation: maxLookBack set in init(), ring allocated on first push
        if (ring_flat_.empty()) {
            num_assets_ = sv.size;
            unsigned cap = maxLookBack + 1;
            ring_flat_.assign(static_cast<size_t>(cap) * num_assets_,
                std::numeric_limits<float>::quiet_NaN());
            nan_buf_.assign(num_assets_, std::numeric_limits<float>::quiet_NaN());
            head_ = 0;
            push_count_ = 0;
        }
        unsigned cap = static_cast<unsigned>(ring_capacity());
        if (cap == 0 || num_assets_ == 0) {
            ++push_count_;
            return;
        }
        head_ = (head_ + 1) % cap;
        float* slot = row_ptr_mut(head_);
        for (unsigned i = 0; i < num_assets_; ++i)
            slot[i] = sv[i];
        ++push_count_;
    }

    RowView operator()(unsigned back) const {
        unsigned cap = static_cast<unsigned>(ring_capacity());
        if (push_count_ == 0 || back >= push_count_ || back > maxLookBack || cap == 0)
            return RowView{nan_buf_.data(), num_assets_};
        unsigned idx = (head_ + cap - back) % cap;
        return RowView{row_ptr(idx), num_assets_};
    }

    RowView operator()() const { return (*this)(0); }

    // Legacy init for explicit allocation (non-STAR code paths)
    void init(unsigned lookback, unsigned num_assets) {
        maxLookBack  = lookback;
        num_assets_  = num_assets;
        unsigned cap = maxLookBack + 1;
        ring_flat_.assign(static_cast<size_t>(cap) * num_assets,
            std::numeric_limits<float>::quiet_NaN());
        nan_buf_.assign(num_assets, std::numeric_limits<float>::quiet_NaN());
        head_       = 0;
        push_count_ = 0;
    }

private:
    size_t ring_capacity() const {
        return (num_assets_ == 0) ? 0 : ring_flat_.size() / num_assets_;
    }

    const float* row_ptr(unsigned idx) const {
        return ring_flat_.data() + static_cast<size_t>(idx) * num_assets_;
    }

    float* row_ptr_mut(unsigned idx) {
        return ring_flat_.data() + static_cast<size_t>(idx) * num_assets_;
    }

    unsigned                         num_assets_;
    unsigned                         head_;
    unsigned                         push_count_;
    std::vector<float>               ring_flat_;
    std::vector<float>               nan_buf_;
};

// ---------------------------------------------------------------------------
// Module: abstract base (STAR ABI)
// ---------------------------------------------------------------------------

// Lookback or bar meaning "never valid"; bar sums saturate here instead of wrapping.
constexpr unsigned kNeverValidBar = std::numeric_limits<unsigned>::max() / 2;
inline unsigned saturating_bars(unsigned long long bars) {
    return bars < kNeverValidBar ? static_cast<unsigned>(bars) : kNeverValidBar;
}

struct Module {
    unsigned numAssets = 0;

    virtual void init() = 0;
    virtual void calculate(unsigned di, SignalVector& output) = 0;
    // Bars after first_bar (the panel bar where the input is first valid) until the output is first valid.
    virtual unsigned lookback(unsigned first_bar) const { return 0; }
    virtual ~Module() = default;

    void setting_output_to_nan_is_not_necessary() { skip_nan_init_ = true; }
    bool skips_nan_init() const { return skip_nan_init_; }

private:
    bool skip_nan_init_ = false;
};
