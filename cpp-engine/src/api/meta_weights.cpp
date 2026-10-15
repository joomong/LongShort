#include "meta_weights.h"

#include <Eigen/Dense>
#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>

namespace {

using Eigen::MatrixXd;
using Eigen::VectorXd;

std::string tag_of(const CombineSpec& spec) {
    const bool ew = spec.method == CombineMethod::Ew || spec.method == CombineMethod::EwSign;
    return std::string("<") + (ew ? "ew" : CombineSpec::method_name(spec.method)) + ">";
}

bool all_finite(const VectorXd& v) { return v.allFinite(); }

// Sum |w| = 1. False for an all-zero or non-finite vector.
bool normalize(VectorXd& w) {
    if (!all_finite(w)) return false;
    const double gross = w.cwiseAbs().sum();
    if (!std::isfinite(gross) || gross <= 0.0) return false;
    w /= gross;
    return true;
}

// Cholesky solve of A x = b; on failure retry once with a 1e-12 * tr(A) / K ridge.
bool solve_spd(const MatrixXd& a, const VectorXd& b, VectorXd& x) {
    Eigen::LLT<MatrixXd> llt(a);
    if (llt.info() == Eigen::Success) {
        x = llt.solve(b);
        if (all_finite(x)) return true;
    }
    const double ridge = 1e-12 * a.trace() / static_cast<double>(a.rows());
    if (!std::isfinite(ridge)) return false;
    MatrixXd shifted = a;
    shifted.diagonal().array() += ridge;
    llt.compute(shifted);
    if (llt.info() != Eigen::Success) return false;
    x = llt.solve(b);
    return all_finite(x);
}

struct WindowStats {
    VectorXd mean;
    MatrixXd centered;
    MatrixXd cov1;   // ddof 1
};

WindowStats window_stats(const MatrixXd& x) {
    WindowStats s;
    s.mean = x.colwise().mean().transpose();
    s.centered = x.rowwise() - s.mean.transpose();
    s.cov1 = (s.centered.transpose() * s.centered) / static_cast<double>(x.rows() - 1);
    return s;
}

// sklearn.covariance.ledoit_wolf (block formula collapsed): biased covariance shrunk to mu * I.
MatrixXd ledoit_wolf_cov(const MatrixXd& xc) {
    const double n = static_cast<double>(xc.rows());
    const double p = static_cast<double>(xc.cols());
    const MatrixXd s0 = (xc.transpose() * xc) / n;
    const MatrixXd x2 = xc.array().square().matrix();
    const VectorXd emp_cov_trace = x2.colwise().sum().transpose() / n;
    const double mu = emp_cov_trace.sum() / p;
    const double beta_sum = (x2.transpose() * x2).sum();
    const double delta_sum = (xc.transpose() * xc).array().square().sum() / (n * n);
    double beta = 1.0 / (p * n) * (beta_sum / n - delta_sum);
    double delta = delta_sum - 2.0 * mu * emp_cov_trace.sum() + p * mu * mu;
    delta /= p;
    beta = std::min(beta, delta);
    const double shrinkage = beta == 0.0 ? 0.0 : beta / delta;
    MatrixXd out = (1.0 - shrinkage) * s0;
    out.diagonal().array() += shrinkage * mu;
    return out;
}

// sklearn.covariance.oas.
MatrixXd oas_cov(const MatrixXd& xc) {
    const double n = static_cast<double>(xc.rows());
    const double p = static_cast<double>(xc.cols());
    const MatrixXd s0 = (xc.transpose() * xc) / n;
    const double alpha = s0.array().square().mean();
    const double mu = s0.trace() / p;
    const double mu_squared = mu * mu;
    const double num = alpha + mu_squared;
    const double den = (n + 1.0) * (alpha - mu_squared / p);
    const double shrinkage = den == 0.0 ? 1.0 : std::min(num / den, 1.0);
    MatrixXd out = (1.0 - shrinkage) * s0;
    out.diagonal().array() += shrinkage * mu;
    return out;
}

// Ledoit & Wolf (2004) "Honey, I shrunk the sample covariance matrix": constant-correlation target,
// delta* = max(0, min(1, (pi - rho) / gamma / T)) on the biased covariance of the centered window.
MatrixXd constant_correlation_cov(const MatrixXd& xc) {
    const Eigen::Index n_rows = xc.rows();
    const Eigen::Index p = xc.cols();
    const double t = static_cast<double>(n_rows);
    const MatrixXd s = (xc.transpose() * xc) / t;
    const VectorXd var = s.diagonal();
    const VectorXd sd = var.array().sqrt().matrix();
    double rbar = 0.0;
    if (p > 1) {
        for (Eigen::Index i = 0; i < p; ++i)
            for (Eigen::Index j = 0; j < p; ++j)
                if (i != j) rbar += s(i, j) / (sd(i) * sd(j));
        rbar /= static_cast<double>(p * (p - 1));
    }
    MatrixXd f(p, p);
    for (Eigen::Index i = 0; i < p; ++i)
        for (Eigen::Index j = 0; j < p; ++j) f(i, j) = i == j ? s(i, i) : rbar * sd(i) * sd(j);
    const MatrixXd y2 = xc.array().square().matrix();
    // pi_ij = mean_t (y_ti y_tj)^2 - s_ij^2
    const MatrixXd pi_mat = (y2.transpose() * y2) / t - s.array().square().matrix();
    const double pi_hat = pi_mat.sum();
    // theta_ii,ij = mean_t y_ti^3 y_tj - s_ii s_ij
    const MatrixXd y3 = xc.array().cube().matrix();
    const MatrixXd theta = (y3.transpose() * xc) / t - (var.asDiagonal() * s);
    double rho = pi_mat.diagonal().sum();
    for (Eigen::Index i = 0; i < p; ++i)
        for (Eigen::Index j = 0; j < p; ++j)
            if (i != j)
                rho += rbar / 2.0 * (sd(j) / sd(i) * theta(i, j) + sd(i) / sd(j) * theta(j, i));
    const double gamma = (f - s).array().square().sum();
    const double kappa = (pi_hat - rho) / gamma;
    const double delta = std::max(0.0, std::min(1.0, kappa / t));
    return delta * f + (1.0 - delta) * s;
}

MatrixXd shrunk(const WindowStats& s, const CombineSpec& spec) {
    switch (spec.shrink) {
        case ShrinkMode::Diag: {
            MatrixXd out = (1.0 - spec.delta) * s.cov1;
            out.diagonal() = s.cov1.diagonal();
            return out;
        }
        case ShrinkMode::LedoitWolf: return ledoit_wolf_cov(s.centered);
        case ShrinkMode::Oas: return oas_cov(s.centered);
        case ShrinkMode::ConstantCorrelation: return constant_correlation_cov(s.centered);
    }
    throw std::logic_error("unknown shrink mode");
}

// Griveau-Billion et al. (2013) cyclic coordinate descent for equal risk contribution:
// minimize x'Sx/2 - b * sum log x, b = 1/K.
bool erc_weights(const MatrixXd& cov, VectorXd& w) {
    const Eigen::Index k = cov.rows();
    MatrixXd sigma = cov;
    sigma.diagonal().array() += 1e-12 * cov.trace() / static_cast<double>(k);
    const double b = 1.0 / static_cast<double>(k);
    VectorXd x(k);
    for (Eigen::Index i = 0; i < k; ++i) {
        if (!(sigma(i, i) > 0.0)) return false;
        x(i) = 1.0 / std::sqrt(sigma(i, i));
    }
    for (int sweep = 0; sweep < 1000; ++sweep) {
        double max_change = 0.0;
        for (Eigen::Index i = 0; i < k; ++i) {
            const double c = sigma.col(i).dot(x) - sigma(i, i) * x(i);
            const double next = (-c + std::sqrt(c * c + 4.0 * sigma(i, i) * b)) / (2.0 * sigma(i, i));
            max_change = std::max(max_change, std::fabs(next - x(i)));
            x(i) = next;
        }
        if (!x.allFinite()) return false;
        if (max_change <= 1e-10 * x.cwiseAbs().maxCoeff()) {
            w = x;
            return normalize(w);
        }
    }
    return false;
}

// Replicates alpha_zoo_results.py _spca_weight on the window standardized by its ddof-1 sd (as
// the research feeds it), then maps the standardized-unit weights back to raw member returns (/sd).
bool spca_weights(const MatrixXd& x_raw, unsigned k_components, VectorXd& w) {
    const Eigen::Index n = x_raw.rows();
    const Eigen::Index k = x_raw.cols();
    const VectorXd raw_mean = x_raw.colwise().mean().transpose();
    const VectorXd raw_sd =
        ((x_raw.rowwise() - raw_mean.transpose()).array().square().colwise().sum() / double(n - 1))
            .sqrt().matrix().transpose();
    for (Eigen::Index i = 0; i < k; ++i)
        if (!(raw_sd(i) > 1e-12)) return false;
    const MatrixXd x = x_raw * raw_sd.cwiseInverse().asDiagonal();
    const WindowStats s = window_stats(x);
    const VectorXd sd = s.cov1.diagonal().array().sqrt().matrix();
    VectorXd score(k);
    for (Eigen::Index i = 0; i < k; ++i) score(i) = sd(i) > 1e-12 ? s.mean(i) / sd(i) : 0.0;
    const MatrixXd scaled = score.asDiagonal() * s.cov1 * score.asDiagonal();
    Eigen::SelfAdjointEigenSolver<MatrixXd> eig(scaled);
    if (eig.info() != Eigen::Success) return false;
    const Eigen::Index m = std::min<Eigen::Index>(k_components, k);
    MatrixXd q(k, m);
    for (Eigen::Index c = 0; c < m; ++c) q.col(c) = eig.eigenvectors().col(k - 1 - c);  // descending
    const MatrixXd loading = score.asDiagonal() * q;
    const MatrixXd pc = x * loading;
    const WindowStats ps = window_stats(pc);
    VectorXd a;
    if (!solve_spd(ps.cov1 + ps.mean * ps.mean.transpose(), ps.mean, a) || !normalize(a)) return false;
    VectorXd w_std = loading * a;
    if (!normalize(w_std)) return false;
    w = w_std.cwiseQuotient(raw_sd);
    return normalize(w);
}

// sklearn enet_coordinate_descent_gram with beta = 0, positive = false, cyclic order.
void enet_cd_gram(VectorXd& w, double alpha, const MatrixXd& q_mat, const VectorXd& q, int max_iter,
                  double tol) {
    const Eigen::Index m = q_mat.rows();
    VectorXd h = q_mat * w;
    const double y_norm2 = q.dot(q);
    const double d_w_tol = tol;
    tol *= y_norm2;
    for (int n_iter = 0; n_iter < max_iter; ++n_iter) {
        double w_max = 0.0;
        double d_w_max = 0.0;
        for (Eigen::Index ii = 0; ii < m; ++ii) {
            if (q_mat(ii, ii) == 0.0) continue;
            const double w_ii = w(ii);
            if (w_ii != 0.0) h -= w_ii * q_mat.col(ii);
            const double tmp = q(ii) - h(ii);
            const double sign = tmp > 0.0 ? 1.0 : (tmp < 0.0 ? -1.0 : 0.0);
            w(ii) = sign * std::max(std::fabs(tmp) - alpha, 0.0) / q_mat(ii, ii);
            if (w(ii) != 0.0) h += w(ii) * q_mat.col(ii);
            d_w_max = std::max(d_w_max, std::fabs(w(ii) - w_ii));
            w_max = std::max(w_max, std::fabs(w(ii)));
        }
        if (w_max == 0.0 || d_w_max / w_max < d_w_tol || n_iter == max_iter - 1) {
            const double q_dot_w = w.dot(q);
            const double dual_norm = (q - h).cwiseAbs().maxCoeff();
            const double r_norm2 = y_norm2 + w.dot(h) - 2.0 * q_dot_w;
            double gap;
            double constant;
            if (dual_norm > alpha) {
                constant = alpha / dual_norm;
                gap = 0.5 * (r_norm2 + r_norm2 * constant * constant);
            } else {
                constant = 1.0;
                gap = r_norm2;
            }
            gap += alpha * w.cwiseAbs().sum() - constant * y_norm2 + constant * q_dot_w;
            if (gap < tol) break;
        }
    }
}

MatrixXd without(const MatrixXd& a, Eigen::Index skip) {
    const Eigen::Index n = a.rows();
    MatrixXd out(n - 1, n - 1);
    for (Eigen::Index i = 0, oi = 0; i < n; ++i) {
        if (i == skip) continue;
        for (Eigen::Index j = 0, oj = 0; j < n; ++j) {
            if (j == skip) continue;
            out(oi, oj++) = a(i, j);
        }
        ++oi;
    }
    return out;
}

VectorXd column_without(const MatrixXd& a, Eigen::Index col, Eigen::Index skip) {
    VectorXd out(a.rows() - 1);
    for (Eigen::Index i = 0, oi = 0; i < a.rows(); ++i)
        if (i != skip) out(oi++) = a(i, col);
    return out;
}

// sklearn _graphical_lasso(mode='cd', max_iter=100, tol=1e-4, enet_tol=1e-4); false when it does not
// converge or the iterate becomes non-finite / non-SPD.
bool graphical_lasso(const MatrixXd& emp, double alpha, MatrixXd& covariance) {
    const Eigen::Index p = emp.rows();
    const int max_iter = 100;
    const double tol = 1e-4;  // sklearn default, as glasso_mvo.py calls it
    const double enet_tol = 1e-4;
    const double eps = std::numeric_limits<double>::epsilon();
    MatrixXd cov = emp * 0.95;
    cov.diagonal() = emp.diagonal();
    Eigen::LLT<MatrixXd> init(cov);
    if (init.info() != Eigen::Success) return false;
    MatrixXd prec = init.solve(MatrixXd::Identity(p, p));
    if (p == 1) {
        covariance = emp;
        return true;
    }
    for (int iter = 0; iter < max_iter; ++iter) {
        for (Eigen::Index idx = 0; idx < p; ++idx) {
            const MatrixXd sub = without(cov, idx);
            const VectorXd row = column_without(emp, idx, idx);
            VectorXd coefs = -column_without(prec, idx, idx) / (prec(idx, idx) + 1000.0 * eps);
            enet_cd_gram(coefs, alpha, sub, row, max_iter, enet_tol);
            const VectorXd cov_col = column_without(cov, idx, idx);
            prec(idx, idx) = 1.0 / (cov(idx, idx) - cov_col.dot(coefs));
            const VectorXd off = -prec(idx, idx) * coefs;
            const VectorXd new_cov = sub * coefs;
            for (Eigen::Index i = 0, oi = 0; i < p; ++i) {
                if (i == idx) continue;
                prec(i, idx) = off(oi);
                prec(idx, i) = off(oi);
                cov(idx, i) = new_cov(oi);
                cov(i, idx) = new_cov(oi);
                ++oi;
            }
        }
        if (!std::isfinite(prec.sum())) return false;
        const double l1_off = prec.cwiseAbs().sum() - prec.diagonal().cwiseAbs().sum();
        const double d_gap = (emp.cwiseProduct(prec)).sum() - static_cast<double>(p) + alpha * l1_off;
        if (std::fabs(d_gap) < tol) {
            covariance = cov;
            return true;
        }
        if (iter > 0) {
            Eigen::LLT<MatrixXd> check(prec);  // the objective is finite only for an SPD precision
            if (check.info() != Eigen::Success) return false;
        }
    }
    return false;
}

// glasso_mvo.py: graphical lasso on 0.99 corr + 0.01 I, covariance rescaled by the window sd,
// w = (C_gl + mu mu')^-1 mu (same direction as C_gl^-1 mu).
bool glasso_weights(const WindowStats& s, double alpha, VectorXd& w) {
    const Eigen::Index p = s.cov1.rows();
    const VectorXd d = s.cov1.diagonal().array().sqrt().matrix();
    for (Eigen::Index i = 0; i < p; ++i)
        if (!(d(i) > 0.0) || !std::isfinite(d(i))) return false;
    MatrixXd r = d.cwiseInverse().asDiagonal() * s.cov1 * d.cwiseInverse().asDiagonal();
    r = 0.99 * r;
    r.diagonal().array() += 0.01;
    MatrixXd cov_gl;
    if (!graphical_lasso(r, alpha, cov_gl)) return false;
    const MatrixXd c = d.asDiagonal() * cov_gl * d.asDiagonal();
    if (!solve_spd(c + s.mean * s.mean.transpose(), s.mean, w)) return false;
    return normalize(w);
}

// Every method except EwSign treats a member whose window sd is <= 1e-12 (e.g. a flat book) as an
// estimator failure: the bar gets the EW row and is counted in fallback_bars. Without this rule
// minvar/ERC would silently put the whole book on that member while ivol/spca/glasso fall back.
bool has_flat_member(const MatrixXd& x) {
    const VectorXd mean = x.colwise().mean().transpose();
    const Eigen::ArrayXd var =
        (x.rowwise() - mean.transpose()).array().square().colwise().sum().transpose() / double(x.rows() - 1);
    return !((var.sqrt() > 1e-12).all());
}

bool weights_for_matrix(const MatrixXd& x, const CombineSpec& spec, VectorXd& w) {
    const Eigen::Index k = x.cols();
    if (spec.method != CombineMethod::Ew && spec.method != CombineMethod::EwSign && has_flat_member(x))
        return false;
    switch (spec.method) {
        case CombineMethod::Ew:
            w = VectorXd::Constant(k, 1.0 / static_cast<double>(k));
            return true;
        case CombineMethod::EwSign: {
            const VectorXd mean = x.colwise().mean().transpose();
            w = mean.unaryExpr([](double v) { return v > 0.0 ? 1.0 : (v < 0.0 ? -1.0 : 0.0); });
            return normalize(w);
        }
        case CombineMethod::Ivol: {
            const WindowStats s = window_stats(x);
            w = s.cov1.diagonal().array().sqrt().inverse().matrix();
            return normalize(w);
        }
        case CombineMethod::Mvo: {
            const WindowStats s = window_stats(x);
            return solve_spd(shrunk(s, spec), s.mean, w) && normalize(w);
        }
        case CombineMethod::MinVar: {
            const WindowStats s = window_stats(x);
            return solve_spd(shrunk(s, spec), VectorXd::Ones(k), w) && normalize(w);
        }
        case CombineMethod::Erc: return erc_weights(window_stats(x).cov1, w);
        case CombineMethod::Spca: return spca_weights(x, spec.k, w);
        case CombineMethod::Glasso: return glasso_weights(window_stats(x), spec.alpha, w);
    }
    return false;
}

MatrixXd to_matrix(const std::vector<std::vector<double>>& window) {
    if (window.empty()) throw std::invalid_argument("empty window");
    MatrixXd x(static_cast<Eigen::Index>(window.size()), static_cast<Eigen::Index>(window[0].size()));
    for (std::size_t t = 0; t < window.size(); ++t) {
        if (window[t].size() != window[0].size()) throw std::invalid_argument("ragged window");
        for (std::size_t k = 0; k < window[t].size(); ++k) x(Eigen::Index(t), Eigen::Index(k)) = window[t][k];
    }
    return x;
}

void validate(const MemberReturns& r, const CombineSpec& spec) {
    const std::string tag = tag_of(spec);
    if (r.K == 0) throw std::invalid_argument(tag + ": the set has no members");
    if (r.rows.size() != r.T) throw std::invalid_argument(tag + ": returns have the wrong bar count");
    for (const auto& row : r.rows)
        if (row.size() != r.K) throw std::invalid_argument(tag + ": returns have the wrong member count");
    if (spec.method == CombineMethod::Ew) return;
    if (spec.rebalance < 1)
        throw std::invalid_argument(tag + ": rebalance=" + std::to_string(spec.rebalance) + " must be >= 1");
    const bool sign = spec.method == CombineMethod::EwSign;
    const char* days_name = sign ? "sign" : "days";
    if (spec.days < (sign ? 1u : 2u))
        throw std::invalid_argument(tag + ": " + days_name + "=" + std::to_string(spec.days) + " must be >= " +
                                    (sign ? "1" : "2"));
    const unsigned trading = r.T > r.sim_start ? r.T - r.sim_start : 0;
    if (spec.days > trading)
        throw std::invalid_argument(tag + ": " + days_name + "=" + std::to_string(spec.days) +
                                    " exceeds the trading bars " + std::to_string(trading));
    if (spec.method == CombineMethod::Spca) {
        if (spec.k < 1) throw std::invalid_argument(tag + ": k=0 must be >= 1");
        if (spec.k > r.K)
            throw std::invalid_argument(tag + ": k=" + std::to_string(spec.k) + " exceeds the member count " +
                                        std::to_string(r.K));
    }
    if (spec.method == CombineMethod::Glasso && !(spec.alpha > 0.0))
        throw std::invalid_argument(tag + ": alpha must be > 0");
    if ((spec.method == CombineMethod::Mvo || spec.method == CombineMethod::MinVar) &&
        spec.shrink == ShrinkMode::Diag && !(spec.delta >= 0.0 && spec.delta <= 1.0))
        throw std::invalid_argument(tag + ": shrink must be in [0,1]");
}

}  // namespace

std::vector<double> shrink_covariance(const std::vector<std::vector<double>>& window, const CombineSpec& spec) {
    const MatrixXd x = to_matrix(window);
    const MatrixXd c = shrunk(window_stats(x), spec);
    std::vector<double> out(static_cast<std::size_t>(c.size()));
    for (Eigen::Index i = 0; i < c.rows(); ++i)
        for (Eigen::Index j = 0; j < c.cols(); ++j) out[std::size_t(i * c.cols() + j)] = c(i, j);
    return out;
}

bool weights_for_window(const std::vector<std::vector<double>>& window, const CombineSpec& spec,
                        std::vector<double>& weights) {
    const MatrixXd x = to_matrix(window);
    if (!x.allFinite()) return false;
    VectorXd w;
    if (!weights_for_matrix(x, spec, w)) return false;
    weights.assign(w.data(), w.data() + w.size());
    return true;
}

CombineWeights compute_combine_weights(const MemberReturns& returns, const CombineSpec& spec) {
    validate(returns, spec);
    const unsigned T = returns.T;
    const unsigned K = returns.K;
    const std::vector<double> ew(K, 1.0 / static_cast<double>(K));
    CombineWeights out;
    out.rows.assign(T, ew);
    if (spec.method == CombineMethod::Ew) {
        out.first_estimated_bar = T;
        return out;
    }
    const unsigned t0 = returns.sim_start + spec.days;
    out.first_estimated_bar = std::min(t0, T);
    const unsigned days = spec.days;
    std::vector<double> current = ew;
    bool current_fallback = false;
    MatrixXd x(days, K);
    VectorXd w;
    for (unsigned t = t0; t < T; ++t) {
        if ((t - t0) % spec.rebalance == 0) {
            for (unsigned r = 0; r < days; ++r)
                for (unsigned k = 0; k < K; ++k) x(r, k) = returns.rows[t - days + r][k];
            const bool ok = x.allFinite() && weights_for_matrix(x, spec, w);
            current_fallback = !ok;
            if (ok)
                current.assign(w.data(), w.data() + w.size());
            else
                current = ew;
        }
        out.rows[t] = current;
        if (current_fallback) ++out.fallback_bars;
    }
    return out;
}
