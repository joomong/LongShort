#pragma once

// MBinaryOp: base for binary operator modules.
// Composite: owns lhs and rhs sub-modules.
struct MBinaryOp : public Module {
    unsigned lookback(unsigned first_bar) const override
    {
        return std::max(lhs->lookback(first_bar), rhs->lookback(first_bar));
    }
    void init()
    {
        lhs_buf.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
        rhs_buf.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
        lhs->numAssets = numAssets;
        lhs->init();
        rhs->numAssets = numAssets;
        rhs->init();
    }

    Module* lhs = nullptr;
    Module* rhs = nullptr;
    std::vector<float> lhs_buf;
    std::vector<float> rhs_buf;

    void eval_operands(unsigned di, const SignalVector &output) {
        SignalVector lsv{ lhs_buf.data(), numAssets };
        SignalVector rsv{ rhs_buf.data(), numAssets };
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            lsv[ii] = output[ii];
            rsv[ii] = output[ii];
        }
        lhs->calculate(di, lsv);
        rhs->calculate(di, rsv);
    }
};

struct MAdd : public MBinaryOp {
    void calculate(unsigned di, SignalVector &output)
    {
        eval_operands(di, output);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float l = lhs_buf[ii], r = rhs_buf[ii];
            output[ii] = (std::isfinite(l) && std::isfinite(r))
                ? l + r : std::numeric_limits<float>::quiet_NaN();
        }
    }
};

struct MSub : public MBinaryOp {
    void calculate(unsigned di, SignalVector &output)
    {
        eval_operands(di, output);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float l = lhs_buf[ii], r = rhs_buf[ii];
            output[ii] = (std::isfinite(l) && std::isfinite(r))
                ? l - r : std::numeric_limits<float>::quiet_NaN();
        }
    }
};

struct MMul : public MBinaryOp {
    void calculate(unsigned di, SignalVector &output)
    {
        eval_operands(di, output);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float l = lhs_buf[ii], r = rhs_buf[ii];
            output[ii] = (std::isfinite(l) && std::isfinite(r))
                ? l * r : std::numeric_limits<float>::quiet_NaN();
        }
    }
};

struct MDiv : public MBinaryOp {
    void calculate(unsigned di, SignalVector &output)
    {
        eval_operands(di, output);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            float l = lhs_buf[ii], r = rhs_buf[ii];
            if (!std::isfinite(l) || !std::isfinite(r) || r == 0.0f)
                output[ii] = std::numeric_limits<float>::quiet_NaN();
            else
                output[ii] = l / r;
        }
    }
};
