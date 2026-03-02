#pragma once

#include <vector>
#include <limits>
#include <cmath>
#include <boost/archive/binary_iarchive.hpp>
#include <boost/archive/binary_oarchive.hpp>
#include <boost/serialization/vector.hpp>
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
    friend class boost::serialization::access;
public:
    unsigned maxLookBack = 0;

    ModuleInput() : num_assets_(0), head_(0), push_count_(0) {}

    void push(const SignalVector& sv) {
        // Lazy allocation: maxLookBack set in init(), ring allocated on first push
        if (ring_.empty()) {
            num_assets_ = sv.size;
            unsigned cap = maxLookBack + 1;
            ring_.assign(cap, std::vector<float>(num_assets_, std::numeric_limits<float>::quiet_NaN()));
            nan_buf_.assign(num_assets_, std::numeric_limits<float>::quiet_NaN());
        }
        unsigned cap = static_cast<unsigned>(ring_.size());
        head_ = (head_ + 1) % cap;
        auto& slot = ring_[head_];
        for (unsigned i = 0; i < num_assets_; ++i)
            slot[i] = sv[i];
        ++push_count_;
    }

    const std::vector<float>& operator()(unsigned back) const {
        unsigned cap = static_cast<unsigned>(ring_.size());
        if (push_count_ == 0 || back >= push_count_ || back > maxLookBack || cap == 0)
            return nan_buf_;
        unsigned idx = (head_ + cap - back) % cap;
        return ring_[idx];
    }

    const std::vector<float>& operator()() const { return (*this)(0); }

    // Legacy init for explicit allocation (non-STAR code paths)
    void init(unsigned lookback, unsigned num_assets) {
        maxLookBack  = lookback;
        num_assets_  = num_assets;
        unsigned cap = maxLookBack + 1;
        ring_.assign(cap, std::vector<float>(num_assets, std::numeric_limits<float>::quiet_NaN()));
        nan_buf_.assign(num_assets, std::numeric_limits<float>::quiet_NaN());
        head_       = 0;
        push_count_ = 0;
    }

private:
    unsigned                         num_assets_;
    unsigned                         head_;
    unsigned                         push_count_;
    std::vector<std::vector<float>>  ring_;
    std::vector<float>               nan_buf_;

    template<class Archive>
    void serialize(Archive& ar, const unsigned int /*version*/) {
        ar & num_assets_;
        ar & maxLookBack;
        ar & head_;
        ar & push_count_;
        ar & ring_;
        ar & nan_buf_;
    }
};

// ---------------------------------------------------------------------------
// Module: abstract base (STAR ABI)
// ---------------------------------------------------------------------------

struct Module {
    unsigned numAssets = 0;

    virtual void init() = 0;
    virtual void calculate(unsigned di, SignalVector& output) = 0;
    virtual ~Module() = default;

    void setting_output_to_nan_is_not_necessary() { skip_nan_init_ = true; }
    bool skips_nan_init() const { return skip_nan_init_; }

    virtual void checkpoint(boost::archive::binary_iarchive& /*ar*/) {}
    virtual void checkpoint(boost::archive::binary_oarchive& /*ar*/) {}

private:
    bool skip_nan_init_ = false;
};
