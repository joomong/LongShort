#pragma once
#include "SignalInputs.h"

// MCsResid: <csresid signal_1="y" signal_2="a" [signal_3="b" ...] [group="sector"]/> (stateless).
// Per bar, the residual of y on the other inputs over cells in the run universe with every input
// finite (and, with group, a finite group code >= 0). Without group an intercept is included;
// with group y and the regressors are demeaned within each group and no intercept is added.
// OLS by column-pivoted QR. Fewer than 2 cells: all NaN. Other cells are NaN.
struct MCsResid : public Module, public SignalInputs {
    std::string group;
    const DataPanel* panel = nullptr;
    const std::vector<uint8_t>* universe = nullptr;

    unsigned min_signal_inputs() const override { return 2; }
    unsigned max_signal_inputs() const override { return 0; }
    void init() override {}
    void calculate(unsigned di, SignalVector& output) override
    {
        const float nan = std::numeric_limits<float>::quiet_NaN();
        const auto* sel = universe ? universe : panel ? &panel->universe_mask : nullptr;
        const uint8_t* m = sel && !sel->empty() ? sel->data() + static_cast<size_t>(di) * numAssets : nullptr;
        const float* y0 = signal_row(0);
        const size_t k = signal_count() - 1;
        const float* codes = group.empty() ? nullptr : panel->row(group, di);
        std::vector<unsigned> cells;
        cells.reserve(numAssets);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            output[ii] = nan;
            bool ok = (!m || m[ii]) && std::isfinite(y0[ii]);
            for (size_t c = 0; c < k && ok; ++c) ok = std::isfinite(signal_row(c + 1)[ii]);
            if (ok && codes) ok = std::isfinite(codes[ii]) && codes[ii] >= 0.0f;
            if (ok) cells.push_back(ii);
        }
        const Eigen::Index n = static_cast<Eigen::Index>(cells.size());
        if (n < 2) return;
        const Eigen::Index kk = static_cast<Eigen::Index>(k);
        Eigen::VectorXd y(n);
        Eigen::MatrixXd x(n, codes ? kk : kk + 1);
        for (Eigen::Index r = 0; r < n; ++r) {
            y(r) = y0[cells[r]];
            for (Eigen::Index c = 0; c < kk; ++c) x(r, c) = signal_row(c + 1)[cells[r]];
            if (!codes) x(r, kk) = 1.0;
        }
        if (codes) {
            std::map<float, std::vector<Eigen::Index>> members;
            for (Eigen::Index r = 0; r < n; ++r) members[codes[cells[r]]].push_back(r);
            for (const auto& entry : members) {
                const auto& idx = entry.second;
                double ybar = 0.0;
                Eigen::VectorXd xbar = Eigen::VectorXd::Zero(kk);
                for (Eigen::Index r : idx) { ybar += y(r); xbar += x.row(r).transpose(); }
                ybar /= static_cast<double>(idx.size());
                xbar /= static_cast<double>(idx.size());
                for (Eigen::Index r : idx) { y(r) -= ybar; x.row(r) -= xbar.transpose(); }
            }
        }
        const Eigen::VectorXd beta = x.colPivHouseholderQr().solve(y);
        const Eigen::VectorXd resid = y - x * beta;
        for (Eigen::Index r = 0; r < n; ++r) output[cells[r]] = static_cast<float>(resid(r));
    }
};
