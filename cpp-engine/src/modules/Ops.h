#pragma once

// MBinaryOp: base for binary operator modules.
// Composite: owns lhs and rhs sub-modules.
struct MBinaryOp : public Module {
    Module* lhs = nullptr;
    Module* rhs = nullptr;

    void init() override {
        lhs_buf_.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
        rhs_buf_.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
        lhs->numAssets = numAssets;
        lhs->init();
        rhs->numAssets = numAssets;
        rhs->init();
    }

    void save_state(std::ostream& os) const override {
        lhs->save_state(os);
        rhs->save_state(os);
    }

    void load_state(std::istream& is) override {
        lhs->load_state(is);
        rhs->load_state(is);
    }

protected:
    std::vector<float> lhs_buf_;
    std::vector<float> rhs_buf_;

    void eval_operands(unsigned di, const SignalVector& output) {
        SignalVector lsv{ lhs_buf_.data(), numAssets };
        SignalVector rsv{ rhs_buf_.data(), numAssets };
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            lsv[ii] = output[ii];
            rsv[ii] = output[ii];
        }
        lhs->calculate(di, lsv);
        rhs->calculate(di, rsv);
    }
};

struct MAdd : public MBinaryOp {
    void calculate(unsigned di, SignalVector& output) override {
        eval_operands(di, output);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float l = lhs_buf_[ii], r = rhs_buf_[ii];
            output[ii] = (std::isfinite(l) && std::isfinite(r))
                ? l + r : std::numeric_limits<float>::quiet_NaN();
        }
    }
};

struct MSub : public MBinaryOp {
    void calculate(unsigned di, SignalVector& output) override {
        eval_operands(di, output);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float l = lhs_buf_[ii], r = rhs_buf_[ii];
            output[ii] = (std::isfinite(l) && std::isfinite(r))
                ? l - r : std::numeric_limits<float>::quiet_NaN();
        }
    }
};

struct MMul : public MBinaryOp {
    void calculate(unsigned di, SignalVector& output) override {
        eval_operands(di, output);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float l = lhs_buf_[ii], r = rhs_buf_[ii];
            output[ii] = (std::isfinite(l) && std::isfinite(r))
                ? l * r : std::numeric_limits<float>::quiet_NaN();
        }
    }
};

struct MDiv : public MBinaryOp {
    void calculate(unsigned di, SignalVector& output) override {
        eval_operands(di, output);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float l = lhs_buf_[ii], r = rhs_buf_[ii];
            if (!std::isfinite(l) || !std::isfinite(r) || r == 0.0f)
                output[ii] = std::numeric_limits<float>::quiet_NaN();
            else
                output[ii] = l / r;
        }
    }
};

struct MLe : public MBinaryOp {
    void calculate(unsigned di, SignalVector& output) override {
        eval_operands(di, output);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float l = lhs_buf_[ii], r = rhs_buf_[ii];
            if (!std::isfinite(l) || !std::isfinite(r))
                output[ii] = std::numeric_limits<float>::quiet_NaN();
            else
                output[ii] = (l <= r) ? 1.0f : 0.0f;
        }
    }
};

struct MGe : public MBinaryOp {
    void calculate(unsigned di, SignalVector& output) override {
        eval_operands(di, output);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float l = lhs_buf_[ii], r = rhs_buf_[ii];
            if (!std::isfinite(l) || !std::isfinite(r))
                output[ii] = std::numeric_limits<float>::quiet_NaN();
            else
                output[ii] = (l >= r) ? 1.0f : 0.0f;
        }
    }
};
