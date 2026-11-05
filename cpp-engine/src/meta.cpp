#include "meta.h"
#include "xml_compiler.h"
#include "parallel_executor.h"
#include "simulation_data.h"

#include <Eigen/Dense>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdio>
#include <fstream>
#include <limits>
#include <set>
#include <sstream>
#include <stdexcept>
#include <thread>

OrderedSignalAccumulator::OrderedSignalAccumulator(const DataPanel& data,
    MissingClosePolicy policy, const std::vector<uint8_t>* universe)
    : data_(data), policy_(policy), universe_(universe),
      signals_(data.T, std::vector<float>(data.N, std::numeric_limits<float>::quiet_NaN())),
      next_alpha_(data.T, 0) {}

void OrderedSignalAccumulator::add(std::size_t alpha_index, unsigned di, const float* row, unsigned n,
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
                    std::to_string(di) + ", asset " + data_.symbols[ii]);
            value = sum;
        }
        ++next_alpha_[di];
        lock.unlock();
        ready_.notify_all();
    }

void OrderedSignalAccumulator::add_block(std::size_t alpha_index, unsigned first_di,
                                          std::vector<float>& rows, unsigned n, std::vector<double>& scratch) {
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
                    std::to_string(first_di + offset) + ", asset " + data_.symbols[ii]);
                value = sum;
            }
            ++next_alpha_[first_di + offset];
        }
        lock.unlock();
        ready_.notify_all();
    }

void OrderedSignalAccumulator::cancel() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            cancelled_ = true;
        }
        ready_.notify_all();
    }

std::vector<std::vector<float>> OrderedSignalAccumulator::take_signals() { return std::move(signals_); }


namespace fs = std::filesystem;

namespace meta_dir {

std::vector<std::string> names(const fs::path& meta_dir)
{
    std::vector<std::string> out;
    std::error_code error;
    if (!fs::is_directory(meta_dir, error)) return out;
    for (fs::directory_iterator it(meta_dir, error), end; !error && it != end; it.increment(error)) {
        const std::string name = it->path().filename().string();
        std::error_code type_error;
        if (name.empty() || name[0] == '.' || !it->is_directory(type_error)) continue;
        out.push_back(name);
    }
    std::sort(out.begin(), out.end());
    return out;
}

std::vector<MetaMember> members(const fs::path& meta_dir, const std::string& name)
{
    const fs::path root = meta_dir / name;
    std::error_code error;
    if (name.empty() || name.find('/') != std::string::npos || name == "." || name == ".." ||
        !fs::is_directory(root, error))
        throw std::runtime_error("Unknown meta '" + name + "': no folder " + root.string());

    std::vector<MetaMember> out;
    std::set<fs::path> visited;  // canonical directories already entered
    std::function<void(const fs::path&)> walk = [&](const fs::path& dir) {
        std::error_code canonical_error;
        const fs::path canonical = fs::canonical(dir, canonical_error);
        if (canonical_error || !visited.insert(canonical).second) return;
        std::vector<fs::path> entries;
        for (fs::directory_iterator it(dir), end; it != end; ++it) entries.push_back(it->path());
        // Sorted so that of two aliases of one directory the first by name is the one entered.
        std::sort(entries.begin(), entries.end(),
                  [](const fs::path& a, const fs::path& b) { return a.string() < b.string(); });
        for (const auto& entry : entries) {
            const std::string file = entry.filename().string();
            if (file.empty() || file[0] == '.') continue;
            if (entry.extension() == ".xml" && fs::is_symlink(entry) && !fs::is_regular_file(entry))
                throw std::runtime_error("Cannot read meta member " + entry.string());
            if (fs::is_directory(entry)) {
                walk(entry);
            } else if (fs::is_regular_file(entry) && entry.extension() == ".xml") {
                std::ifstream in(entry, std::ios::binary);
                if (!in) throw std::runtime_error("Cannot read meta member " + entry.string());
                std::ostringstream text;
                text << in.rdbuf();
                out.push_back({entry.lexically_relative(root).generic_string(), text.str()});
            }
        }
    };
    walk(root);
    if (out.empty()) throw std::runtime_error("meta '" + name + "' has no .xml files");
    std::sort(out.begin(), out.end(),
              [](const MetaMember& a, const MetaMember& b) { return a.path < b.path; });
    return out;
}

}  // namespace meta_dir

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

MemberReturns collect_member_returns(const DataPanel& data, const MetaSet& set, const MetaRunConfig& config,
                                     const std::function<bool()>& cancelled, MetaWorkerBudget* budget) {
    const std::size_t count = set.members.size();
    if (count == 0) throw std::logic_error("meta set '" + set.name + "' has no members");
    MemberReturns out;
    out.paths = set.member_paths;
    // Rows end at the trading period's end (sim_end_index), like the member's gross_ret.
    out.T = std::min<unsigned>(data.T, config.member_config.sim_end_index);
    out.K = static_cast<unsigned>(count);
    out.sim_start = config.member_config.sim_start_index;
    out.rows.assign(out.T, std::vector<double>(count, std::numeric_limits<double>::quiet_NaN()));
    const unsigned wanted = std::max(1u, std::min<unsigned>(config.threads, static_cast<unsigned>(count)));
    struct Tokens {
        MetaWorkerBudget* budget;
        unsigned count;
        ~Tokens() { if (budget) budget->release(count); }
    } tokens{budget, budget ? budget->acquire(wanted) : wanted};
    const unsigned thread_count = tokens.count;
    std::atomic<std::size_t> next_index{0};
    std::atomic<bool> failed{false};
    std::exception_ptr first_error;
    std::mutex error_mutex;
    auto worker = [&] {
        while (!failed.load(std::memory_order_acquire)) {
            const std::size_t i = next_index.fetch_add(1);
            if (i >= count) return;
            try {
                try {
                    if (cancelled()) throw EvaluationCancelled();
                    SimulatorConfig member_config = config.member_config;
                    member_config.collect_signals = false;
                    member_config.signal_row_callback = [&](unsigned, const float*, unsigned) {
                        if (failed.load() || cancelled()) throw EvaluationCancelled();
                    };
                    const SimulatorOutput output = Simulator(data, member_config).run(set.members[i].pipeline);
                    const std::size_t bars = std::min<std::size_t>(output.gross_ret.size(), out.T);
                    for (std::size_t t = out.sim_start; t < bars; ++t) out.rows[t][i] = output.gross_ret[t];
                } catch (const EvaluationCancelled&) {
                    throw;
                } catch (const std::exception& error) {
                    throw std::runtime_error("meta '" + set.name + "' member " + set.member_paths.at(i) +
                                             ": " + error.what());
                }
            } catch (...) {
                {
                    std::lock_guard<std::mutex> lock(error_mutex);
                    if (!first_error) first_error = std::current_exception();
                }
                failed.store(true, std::memory_order_release);
                return;
            }
        }
    };
    std::vector<std::thread> workers;
    try {
        for (unsigned t = 0; t < thread_count; ++t) workers.emplace_back(worker);
    } catch (...) {
        failed.store(true, std::memory_order_release);
        for (auto& thread : workers) thread.join();
        throw;
    }
    for (auto& thread : workers) thread.join();
    if (first_error) std::rethrow_exception(first_error);
    if (cancelled()) throw EvaluationCancelled();
    return out;
}

std::shared_ptr<const MemberReturns> MemberReturnsCache::get(const std::string& key,
                                                             const std::function<MemberReturns()>& compute) {
    std::unique_lock<std::mutex> lock(mutex_);
    for (;;) {
        auto found = entries_.find(key);
        if (found == entries_.end()) break;
        std::shared_ptr<Entry> entry = found->second;
        if (entry->done) {
            lru_.remove(key);
            lru_.push_front(key);
            return entry->value;
        }
        changed_.wait(lock, [&] { return entry->done; });
        // Completed, evicted or failed: look it up again. A waiter never inherits the computing
        // caller's exception (it may be that request's cancellation); after a failure the entry is
        // gone and this caller computes with its own callback.
    }
    auto entry = std::make_shared<Entry>();
    entries_[key] = entry;
    lock.unlock();
    std::shared_ptr<const MemberReturns> value;
    std::exception_ptr error;
    try {
        value = std::make_shared<const MemberReturns>(compute());
    } catch (...) {
        error = std::current_exception();
    }
    lock.lock();
    entry->done = true;
    if (error) {
        entries_.erase(key);
        changed_.notify_all();
        lock.unlock();
        std::rethrow_exception(error);
    }
    entry->value = value;
    lru_.push_front(key);
    while (lru_.size() > kCapacity) {
        entries_.erase(lru_.back());
        lru_.pop_back();
    }
    changed_.notify_all();
    return value;
}

unsigned MetaWorkerBudget::acquire(unsigned wanted) {
    std::unique_lock<std::mutex> lock(mutex_);
    available_.wait(lock, [&] { return free_ > 0; });
    const unsigned granted = std::min(free_, std::max(1u, wanted));
    free_ -= granted;
    return granted;
}

void MetaWorkerBudget::release(unsigned count) {
    {
        std::lock_guard<std::mutex> lock(mutex_);
        free_ += count;
    }
    available_.notify_all();
}

namespace {

// Sums every member's normalized rows into one output per weight matrix, in member order per bar
// (same ordering rule as OrderedSignalAccumulator).
class WeightedAccumulator {
public:
    WeightedAccumulator(const DataPanel& data, const MetaRunConfig& config,
                        const std::vector<const CombineWeights*>& weights, const std::string& name)
        : data_(data), config_(config), weights_(weights), name_(name),
          outputs_(weights.size(), MetaMatrix(data.T, std::vector<float>(data.N, std::numeric_limits<float>::quiet_NaN()))),
          next_member_(data.T, 0) {}

    void add_block(std::size_t member, unsigned first_di, std::vector<float>& rows, unsigned n,
                   std::vector<double>& scratch) {
        if (!n || n != data_.N || rows.size() % n || first_di > data_.T || rows.size() / n > data_.T - first_di)
            throw std::invalid_argument("Combined signal dimensions must match the data panel");
        const unsigned count = static_cast<unsigned>(rows.size() / n);
        if (!count) return;
        std::vector<double> normalized(rows.size());
        for (unsigned offset = 0; offset < count; ++offset) {
            normalize_signal_row(data_, first_di + offset, rows.data() + static_cast<size_t>(offset) * n, scratch,
                                 config_.policy, config_.universe_mask);
            std::copy(scratch.begin(), scratch.begin() + n, normalized.begin() + static_cast<size_t>(offset) * n);
        }
        std::unique_lock<std::mutex> lock(mutex_);
        ready_.wait(lock, [&] {
            return cancelled_ || std::all_of(next_member_.begin() + first_di, next_member_.begin() + first_di + count,
                                             [&](std::size_t next) { return next == member; });
        });
        if (cancelled_) throw std::runtime_error("Combined evaluation cancelled after an alpha failed");
        for (unsigned offset = 0; offset < count; ++offset) {
            const unsigned di = first_di + offset;
            const double* row = normalized.data() + static_cast<size_t>(offset) * n;
            for (std::size_t c = 0; c < weights_.size(); ++c) {
                // Weight rows end at the trading period's end; members emit no rows after it.
                if (di >= weights_[c]->rows.size() || member >= weights_[c]->rows[di].size())
                    throw std::logic_error("meta '" + name_ + "' has no weight for bar " + std::to_string(di));
                const double w = weights_[c]->rows[di][member];
                auto& combined = outputs_[c][di];
                for (unsigned ii = 0; ii < n; ++ii) {
                    if (!std::isfinite(row[ii])) continue;
                    const float contribution = static_cast<float>(row[ii] * w);
                    float& value = combined[ii];
                    const float sum = std::isfinite(value) ? value + contribution : contribution;
                    if (!std::isfinite(sum)) throw std::overflow_error("Combined signal sum overflow at bar " +
                        std::to_string(di) + ", asset " + data_.symbols[ii]);
                    value = sum;
                }
            }
            ++next_member_[di];
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

    std::vector<MetaMatrix> take() { return std::move(outputs_); }

private:
    const DataPanel& data_;
    const MetaRunConfig& config_;
    const std::vector<const CombineWeights*>& weights_;
    const std::string name_;
    std::vector<MetaMatrix> outputs_;
    std::vector<std::size_t> next_member_;
    std::mutex mutex_;
    std::condition_variable ready_;
    bool cancelled_ = false;
};

// Runs every member's signal rows into `accumulator` (EW or weighted) with the request's workers.
template <typename Accumulator>
void run_members(const DataPanel& data, MetaSet& set, const MetaRunConfig& config,
                 const std::function<bool()>& cancelled, MetaWorkerBudget* budget, Accumulator& accumulator) {
    const std::size_t count = set.members.size();
    if (count == 0) throw std::logic_error("meta set '" + set.name + "' has no members");
    const unsigned wanted = std::max(1u, std::min<unsigned>(config.threads, static_cast<unsigned>(count)));
    struct Tokens {
        MetaWorkerBudget* budget;
        unsigned count;
        ~Tokens() { if (budget) budget->release(count); }
    } tokens{budget, budget ? budget->acquire(wanted) : wanted};
    const unsigned thread_count = tokens.count;
    std::atomic<std::size_t> next_index{0};
    std::atomic<bool> failed{false};
    std::exception_ptr first_error;
    std::mutex error_mutex;
    auto worker = [&] {
        std::vector<double> normalized_scratch;
        while (!failed.load(std::memory_order_acquire)) {
            const std::size_t i = next_index.fetch_add(1);
            if (i >= count) return;
            try {
                try {
                    std::vector<float> signal_block;
                    signal_block.reserve(static_cast<size_t>(32) * data.N);
                    unsigned first_bar = 0;
                    auto flush = [&] {
                        if (signal_block.empty()) return;
                        accumulator.add_block(i, first_bar, signal_block, data.N, normalized_scratch);
                        signal_block.clear();
                    };
                    auto observe = [&](unsigned di, const float* row, unsigned n) {
                        if (failed.load() || cancelled()) throw EvaluationCancelled();
                        if (signal_block.empty()) first_bar = di;
                        signal_block.insert(signal_block.end(), row, row + n);
                        if (signal_block.size() == static_cast<size_t>(32) * n) flush();
                    };
                    Simulator(data, config.member_config).run_signal_rows(set.members[i].pipeline, observe);
                    flush();
                    set.members[i] = CompiledAlpha{};
                } catch (const EvaluationCancelled&) {
                    throw;
                } catch (const std::exception& error) {
                    throw std::runtime_error("meta '" + set.name + "' member " + set.member_paths.at(i) +
                                             ": " + error.what());
                }
            } catch (...) {
                {
                    std::lock_guard<std::mutex> lock(error_mutex);
                    if (!first_error) first_error = std::current_exception();
                }
                failed.store(true, std::memory_order_release);
                accumulator.cancel();
                return;
            }
        }
    };
    std::vector<std::thread> workers;
    try {
        for (unsigned t = 0; t < thread_count; ++t) workers.emplace_back(worker);
    } catch (...) {
        failed.store(true, std::memory_order_release);
        accumulator.cancel();
        for (auto& thread : workers) thread.join();
        throw;
    }
    for (auto& thread : workers) thread.join();
    if (first_error) std::rethrow_exception(first_error);
    if (cancelled()) throw EvaluationCancelled();
}

}  // namespace

MetaMatrix materialize_meta_set(const DataPanel& data, MetaSet& set, const MetaRunConfig& config,
                                const std::function<bool()>& cancelled, MetaWorkerBudget* budget) {
    OrderedSignalAccumulator accumulator(data, config.policy, config.universe_mask);
    run_members(data, set, config, cancelled, budget, accumulator);
    return accumulator.take_signals();
}

std::vector<MetaMatrix> materialize_weighted(const DataPanel& data, MetaSet& set, const MetaRunConfig& config,
                                             const std::vector<const CombineWeights*>& weights,
                                             const std::function<bool()>& cancelled, MetaWorkerBudget* budget) {
    for (const auto* matrix : weights)
        if (!matrix || matrix->rows.size() > data.T)
            throw std::logic_error("meta '" + set.name + "' weights must have at most one row per bar");
    WeightedAccumulator accumulator(data, config, weights, set.name);
    run_members(data, set, config, cancelled, budget, accumulator);
    return accumulator.take();
}

std::string meta_set_key(const MetaSet& set, const CombineSpec& spec) {
    std::string key = set.name;
    for (const auto& path : set.member_paths) key += "\n" + path;
    return key + "\n" + spec.key();
}

std::shared_ptr<const MetaMatrix> MetaSetCache::get(const std::string& name,
                                                    const std::function<MetaMatrix()>& compute) {
    std::shared_ptr<Entry> entry;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        auto& slot = entries_[name];
        if (!slot) slot = std::make_shared<Entry>();
        entry = slot;
    }
    // call_once never runs the callable twice here: failures are stored, not thrown out of it.
    std::call_once(entry->once, [&] {
        try {
            entry->matrix = std::make_shared<const MetaMatrix>(compute());
        } catch (...) {
            entry->error = std::current_exception();
        }
    });
    if (entry->error) std::rethrow_exception(entry->error);
    return entry->matrix;
}

void MetaSetCache::put(const std::string& key, std::shared_ptr<const MetaMatrix> matrix, std::exception_ptr error) {
    std::shared_ptr<Entry> entry;
    {
        std::lock_guard<std::mutex> lock(mutex_);
        auto& slot = entries_[key];
        if (!slot) slot = std::make_shared<Entry>();
        entry = slot;
    }
    std::call_once(entry->once, [&] {
        entry->matrix = std::move(matrix);
        entry->error = error;
    });
}

bool MetaSetCache::contains(const std::string& key) {
    std::lock_guard<std::mutex> lock(mutex_);
    return entries_.count(key) != 0;
}

void MetaSetCache::evict(const std::vector<std::string>& keys) {
    std::lock_guard<std::mutex> lock(mutex_);
    for (const auto& key : keys) {
        entries_.erase(key);
        infos_.erase(key);
    }
}

void MetaSetCache::set_info(const std::string& key, MetaCombineInfo info) {
    std::lock_guard<std::mutex> lock(mutex_);
    infos_[key] = info;
}

bool MetaSetCache::info(const std::string& key, MetaCombineInfo* out) {
    std::lock_guard<std::mutex> lock(mutex_);
    auto found = infos_.find(key);
    if (found == infos_.end()) return false;
    *out = found->second;
    return true;
}

void MetaSetCache::record(std::size_t materializations, std::size_t member_runs) {
    std::lock_guard<std::mutex> lock(mutex_);
    stats_.materializations += materializations;
    stats_.member_runs += member_runs;
}

MetaSetCache::Stats MetaSetCache::stats() {
    std::lock_guard<std::mutex> lock(mutex_);
    return stats_;
}

namespace {

std::string returns_key(const MetaSet& set, const std::string& settings) {
    uint64_t hash = 1469598103934665603ull;  // FNV-1a 64 over every member XML, in member order
    for (const auto& member : set.members) {
        for (unsigned char c : member.source_xml) {
            hash ^= c;
            hash *= 1099511628211ull;
        }
        hash ^= 0xff;
        hash *= 1099511628211ull;
    }
    char hex[17];
    std::snprintf(hex, sizeof hex, "%016llx", static_cast<unsigned long long>(hash));
    std::string key = set.name;
    for (const auto& path : set.member_paths) key += "\n" + path;
    return key + "\n" + hex + "\n" + settings;
}

struct CombinedSet {
    std::vector<MetaMatrix> matrices;          // empty matrix where errors[c] is set
    std::vector<std::exception_ptr> errors;
    std::vector<MetaCombineInfo> infos;
};

// Passes (1)-(3) for `specs` (non-Ew, distinct). A member failure or cancellation throws.
CombinedSet combine_set(const MetaCombineContext& context, MetaSet& set, const std::vector<CombineSpec>& specs) {
    const std::size_t members = set.members.size();
    auto returns = context.returns.get(returns_key(set, context.settings), [&] {
        auto value = collect_member_returns(context.data, set, context.config, context.cancelled,
                                            &context.cache.budget());
        context.cache.record(0, members);
        return value;
    });
    CombinedSet out;
    out.matrices.resize(specs.size());
    out.errors.resize(specs.size());
    out.infos.resize(specs.size());
    std::vector<CombineWeights> weights(specs.size());
    std::vector<const CombineWeights*> used;
    std::vector<std::size_t> used_index;
    for (std::size_t c = 0; c < specs.size(); ++c) {
        try {
            weights[c] = compute_combine_weights(*returns, specs[c]);
        } catch (const std::invalid_argument& error) {
            out.errors[c] = std::make_exception_ptr(std::invalid_argument("meta '" + set.name + "' " + error.what()));
            continue;
        }
        out.infos[c] = {weights[c].fallback_bars, weights[c].first_estimated_bar};
        used.push_back(&weights[c]);
        used_index.push_back(c);
    }
    auto matrices = materialize_weighted(context.data, set, context.config, used, context.cancelled,
                                         &context.cache.budget());
    context.cache.record(1, members);
    for (std::size_t u = 0; u < used.size(); ++u) out.matrices[used_index[u]] = std::move(matrices[u]);
    return out;
}

}  // namespace

MetaMatrix materialize_combined(const MetaCombineContext& context, MetaSet& set) {
    auto combined = combine_set(context, set, {set.combine});
    if (combined.errors[0]) std::rethrow_exception(combined.errors[0]);
    context.cache.set_info(meta_set_key(set, set.combine), combined.infos[0]);
    return std::move(combined.matrices[0]);
}

std::vector<std::string> prefetch_meta_sets(const MetaCombineContext& context, std::vector<CompiledAlpha>& alphas) {
    struct Group {
        MetaSet* set = nullptr;
        std::vector<CombineSpec> specs;
        std::set<std::string> keys;
    };
    std::map<std::string, Group> groups;
    std::vector<std::string> order;
    for (auto& alpha : alphas)
        for (auto& set : alpha.meta_sets) {
            if (set.combine.is_ew() || set.members.empty()) continue;
            const std::string key = meta_set_key(set, set.combine);
            const std::string group_key = key.substr(0, key.size() - set.combine.key().size());
            auto& group = groups[group_key];
            if (!group.set) {
                group.set = &set;
                order.push_back(group_key);
            }
            if (context.cache.contains(key) || !group.keys.insert(key).second) continue;
            group.specs.push_back(set.combine);
        }
    std::vector<std::string> stored;
    for (const auto& group_key : order) {
        auto& group = groups[group_key];
        if (group.specs.empty()) continue;
        CombinedSet combined;
        std::exception_ptr failure;
        try {
            combined = combine_set(context, *group.set, group.specs);
        } catch (const EvaluationCancelled&) {
            throw;
        } catch (...) {
            failure = std::current_exception();  // member failure: every candidate of this set fails with it
        }
        for (std::size_t c = 0; c < group.specs.size(); ++c) {
            const std::string key = meta_set_key(*group.set, group.specs[c]);
            if (failure) {
                context.cache.put(key, nullptr, failure);
            } else if (combined.errors[c]) {
                context.cache.put(key, nullptr, combined.errors[c]);
            } else {
                context.cache.set_info(key, combined.infos[c]);
                // Round memory is C x T x N x 4 bytes: keep the meta parameter grid per round small.
                context.cache.put(key, std::make_shared<const MetaMatrix>(std::move(combined.matrices[c])), nullptr);
            }
            stored.push_back(key);
        }
    }
    return stored;
}
