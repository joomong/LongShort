#pragma once

// MPcaResid: rolling PCA residual per group (the combo15 MR alpha).
// <pcaresid group="sector" train="1260" refit="252" k="0" var="0.9"/>
// Fit at panel bars b with b % refit == 0 and b >= train on the input rows [b - train, b); NaN before the
// first fit. Fit set per group: in the run universe, same finite group code >= 0, and finite input on every
// train bar (bars >= b are never used for selection). Columns are standardized with the train mean/std
// (ddof 0; zero-std columns dropped), PCA as sklearn on the standardized matrix: k > 0 fixes the component
// count, else the first k with cumulative explained variance ratio >= var (0 < var < 1). Each bar t of the block outputs
// z_hat - z for fitted instruments with finite input and the fitted group code at t (others NaN);
// z_hat = mu + V s with s the least-squares scores on the observed coordinates. No universe mask at t
// (rank/neutralize apply it). The eigenvector sign is arbitrary and does not affect V V^T, so the residual
// is sign-free. Without a group attribute one block is fitted over the whole run universe.
struct MPcaResid : public Module {
    struct Fit {
        float code = 0.0f;
        std::vector<unsigned> ids;
        std::vector<double> mean, sd, mu, V;   // V: p x k, row-major
        int k = 0;
    };

    unsigned lookback(unsigned first_bar) const override
    {
        const unsigned long long t = std::max(1, (int)train), r = std::max(1, (int)refit);
        const unsigned long long earliest = first_bar + t;
        return saturating_bars(earliest + (r - earliest % r) % r - first_bar);
    }
    void init()
    {
        T = static_cast<unsigned>(std::max(1, (int)train));
        R = static_cast<unsigned>(std::max(1, (int)refit));
        input.init(T, numAssets);
        fits.clear();
        resid.assign(numAssets, std::numeric_limits<float>::quiet_NaN());
    }
    void calculate(unsigned di, SignalVector &output)
    {
        input.push(output);   // input(b - r) is the input row of bar r <= b
        if (di >= T && di % R == 0) refit_at(di);

        const float* codes = group_codes(di);
        for (const Fit& f : fits) {
            const int p = static_cast<int>(f.ids.size());
            obs.clear();
            dz.clear();
            for (int j = 0; j < p; ++j) {
                const unsigned ii = f.ids[j];
                const float x = output[ii];
                if (!std::isfinite(x) || (codes && codes[ii] != f.code)) continue;
                obs.push_back(j);
                dz.push_back((static_cast<double>(x) - f.mean[j]) / f.sd[j] - f.mu[j]);
            }
            if (obs.empty()) continue;
            project(f.V, p, f.k, obs, dz, rec);
            for (size_t o = 0; o < obs.size(); ++o)
                resid[f.ids[obs[o]]] = static_cast<float>(rec[o] - dz[o]);  // z_hat - z = V s - (z - mu)
        }
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            output[ii] = resid[ii];
            resid[ii] = std::numeric_limits<float>::quiet_NaN();
        }
    }

    const float* group_codes(unsigned di) const
    {
        if (group.name().empty() || panel == nullptr || !panel->has_column(group.name())) return nullptr;
        return panel->row(group.name(), di);
    }

    // Refit every group on the input rows [b - T, b) = input(T) .. input(1).
    void refit_at(unsigned b)
    {
        fits.clear();
        const auto* sel = universe ? universe : panel ? &panel->universe_mask : nullptr;
        const uint8_t* mask = sel && !sel->empty() ? sel->data() : nullptr;
        const bool grouped = !group.name().empty();
        if (grouped && (panel == nullptr || !panel->has_column(group.name()))) return;
        std::vector<float> code_of(numAssets, 0.0f);
        std::vector<unsigned> cand;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            bool ok = true;
            const float c0 = grouped ? panel->row(group.name(), b - T)[ii] : 0.0f;
            if (grouped && !(std::isfinite(c0) && c0 >= 0.0f)) continue;
            for (unsigned r = b - T; r < b && ok; ++r) {
                if (mask && !mask[static_cast<size_t>(r) * numAssets + ii]) ok = false;
                else if (grouped && panel->row(group.name(), r)[ii] != c0) ok = false;
                else if (!std::isfinite(input(b - r)[ii])) ok = false;
            }
            if (ok) { cand.push_back(ii); code_of[ii] = c0; }
        }
        std::vector<float> codes;
        for (unsigned ii : cand) codes.push_back(code_of[ii]);
        std::sort(codes.begin(), codes.end());
        codes.erase(std::unique(codes.begin(), codes.end()), codes.end());
        for (float g : codes) {
            Fit f;
            f.code = g;
            for (unsigned ii : cand) {
                if (code_of[ii] != g) continue;
                double s = 0.0, s2 = 0.0;
                for (unsigned r = b - T; r < b; ++r) s += input(b - r)[ii];
                const double m = s / T;
                for (unsigned r = b - T; r < b; ++r) {
                    const double e = input(b - r)[ii] - m;
                    s2 += e * e;
                }
                const double sd = std::sqrt(s2 / T);
                if (!(sd > 0.0) || !std::isfinite(sd)) continue;  // zero-std column dropped
                f.ids.push_back(ii);
                f.mean.push_back(m);
                f.sd.push_back(sd);
            }
            const int p = static_cast<int>(f.ids.size());
            if (p == 0) continue;
            std::vector<double> Z(static_cast<size_t>(T) * p);
            for (unsigned r = 0; r < T; ++r) {
                const float* row = input(T - r).data();   // bar b - T + r
                for (int j = 0; j < p; ++j)
                    Z[static_cast<size_t>(r) * p + j] = (row[f.ids[j]] - f.mean[j]) / f.sd[j];
            }
            f.k = fit_pca(Z, static_cast<int>(T), p, static_cast<double>(var), (int)k, f.mu, f.V);
            fits.push_back(std::move(f));
        }
    }

    // Z: n x p standardized train matrix, row-major. Matches sklearn PCA: centers Z again (mu), components =
    // top eigenvectors of the covariance. k > 0 fixes the count, else the first k with cumulative explained
    // variance ratio >= var. Writes mu (p) and V (p x k, row-major); returns k.
    static int fit_pca(const std::vector<double>& Z, int n, int p, double var, int k, std::vector<double>& mu,
                       std::vector<double>& V)
    {
        using Mat = Eigen::Matrix<double, Eigen::Dynamic, Eigen::Dynamic, Eigen::RowMajor>;
        Eigen::Map<const Mat> z(Z.data(), n, p);
        const Eigen::RowVectorXd m = z.colwise().mean();
        const Mat c = z.rowwise() - m;
        const Eigen::MatrixXd cov = (c.transpose() * c) / std::max(1, n - 1);
        Eigen::SelfAdjointEigenSolver<Eigen::MatrixXd> es(cov);
        // Eigenvalues ascending; reorder descending. Tiny negative rounding is clipped like the SVD's S^2 >= 0.
        const Eigen::VectorXd ev = es.eigenvalues().cwiseMax(0.0);
        const double total = ev.sum();
        int kk = k > 0 ? std::min(k, p) : 0;
        if (kk == 0) {
            kk = 1;  // numpy argmax of an all-false vector is 0, i.e. one component
            double cum = 0.0;
            for (int j = 0; j < p; ++j) {
                cum += total > 0.0 ? ev(p - 1 - j) / total : 0.0;
                if (cum >= var) { kk = j + 1; break; }
            }
        }
        mu.assign(m.data(), m.data() + p);
        V.assign(static_cast<size_t>(p) * kk, 0.0);
        for (int j = 0; j < kk; ++j)
            for (int i = 0; i < p; ++i)
                V[static_cast<size_t>(i) * kk + j] = es.eigenvectors()(i, p - 1 - j);
        return kk;
    }

    // d = z - mu on the observed coordinates obs (indices into 0..p-1). Least-squares factor scores on the
    // observed rows of V (= V^T d when every coordinate is observed); writes the reconstruction V_obs s into fit.
    static void project(const std::vector<double>& V, int p, int k, const std::vector<int>& obs,
                        const std::vector<double>& d, std::vector<double>& fit)
    {
        const int no = static_cast<int>(obs.size());
        Eigen::MatrixXd vo(no, k);
        Eigen::VectorXd dv(no);
        for (int o = 0; o < no; ++o) {
            dv(o) = d[o];
            for (int j = 0; j < k; ++j) vo(o, j) = V[static_cast<size_t>(obs[o]) * k + j];
        }
        Eigen::VectorXd s;
        if (no == p) s = vo.transpose() * dv;  // orthonormal columns: least squares = V^T d
        else s = vo.completeOrthogonalDecomposition().solve(dv);
        const Eigen::VectorXd r = vo * s;
        fit.assign(r.data(), r.data() + no);
    }

    unsigned T = 1, R = 1;             // train / refit after init()
    ModuleInput input;                 // input history, lookback T
    std::vector<Fit> fits;             // current block's fits, one per group
    StarVector<float> resid;
    std::vector<int> obs;
    std::vector<double> dz, rec;
    const DataPanel* panel = nullptr;  // group codes; the factory sets it
    const std::vector<uint8_t>* universe = nullptr; // run-selected mask (universe.names); null = panel default
    GroupParameter group{""};
    IntParameter train{1260};
    IntParameter refit{252};
    IntParameter k{0};
    FloatParameter var{0.9f};
    // HELP rolling PCA residual z_hat - z per group: train=1260 refit=252 var=0.9 (k>0 fixes the component count)
};
