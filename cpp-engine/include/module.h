#pragma once

#include <vector>
#include <limits>
#include <cmath>
#include <iosfwd>
#include "signal_vector.h"
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
    double value = 0.0;
    FloatParameter() = default;
    explicit FloatParameter(double v) : value(v) {}
    operator double() const { return value; }
};

// ---------------------------------------------------------------------------
// ModuleInput: ring buffer giving lookback access to pushed SignalVectors
// ---------------------------------------------------------------------------

class ModuleInput {
public:
    unsigned maxLookBack = 0;

    ModuleInput() : num_assets_(0), head_(0), push_count_(0) {}

    void init(unsigned lookback, unsigned num_assets) {
        maxLookBack  = lookback;
        num_assets_  = num_assets;
        unsigned cap = maxLookBack + 1;
        ring_.assign(cap, std::vector<float>(num_assets, std::numeric_limits<float>::quiet_NaN()));
        nan_buf_.assign(num_assets, std::numeric_limits<float>::quiet_NaN());
        head_       = 0;
        push_count_ = 0;
    }

    void push(const SignalVector& sv) {
        unsigned cap = static_cast<unsigned>(ring_.size());
        head_ = (head_ + 1) % cap;
        auto& slot = ring_[head_];
        for (unsigned i = 0; i < num_assets_; ++i)
            slot[i] = sv[i];
        ++push_count_;
    }

    const SignalVector& operator()(unsigned back) const {
        unsigned cap = static_cast<unsigned>(ring_.size());
        if (push_count_ == 0 || back >= push_count_ || back > maxLookBack) {
            sv_scratch_.data = const_cast<float*>(nan_buf_.data());
            sv_scratch_.size = num_assets_;
            return sv_scratch_;
        }
        unsigned idx = (head_ + cap - back) % cap;
        sv_scratch_.data = const_cast<float*>(ring_[idx].data());
        sv_scratch_.size = num_assets_;
        return sv_scratch_;
    }

    const SignalVector& operator()() const { return (*this)(0); }

private:
    unsigned                         num_assets_;
    unsigned                         head_;
    unsigned                         push_count_;
    std::vector<std::vector<float>>  ring_;
    std::vector<float>               nan_buf_;
    mutable SignalVector             sv_scratch_{nullptr, 0};
};

// ---------------------------------------------------------------------------
// Module: abstract base (STAR ABI)
// ---------------------------------------------------------------------------
// - numAssets is set by framework before init()
// - init() is parameterless
// - save_state/load_state: override for stateful modules (default no-op)
// ---------------------------------------------------------------------------

struct Module {
    unsigned numAssets = 0;

    virtual void init() = 0;
    virtual void calculate(unsigned di, SignalVector& output) = 0;
    virtual ~Module() = default;

    void setting_output_to_nan_is_not_necessary() { skip_nan_init_ = true; }
    bool skips_nan_init() const { return skip_nan_init_; }

    virtual void save_state(std::ostream& /*os*/) const {}
    virtual void load_state(std::istream& /*is*/) {}

private:
    bool skip_nan_init_ = false;
};
