// Oracle tests for <ffd> (fixed-window fractional difference) and <pcaresid> (rolling PCA residual per
// group). The ffd oracle is the desk weight recursion with a plain forward-filled sum; the pcaresid oracle
// refits with Eigen's JacobiSVD (a different algorithm from the module's eigen solver) and solves the
// observed-coordinate least squares with an SVD.
#include <gtest/gtest.h>

#include <cmath>
#include <cstring>
#include <limits>
#include <random>
#include <sstream>
#include <string>
#include <vector>

#include "module.h"
#include "signal_vector.h"
#include "compiler/dag_builder.h"
#include "data/data_panel.h"
#include "modules/Ffd.h"
#include "modules/PcaResid.h"

namespace {

constexpr float kNaN = std::numeric_limits<float>::quiet_NaN();
using Hist = std::vector<std::vector<float>>;  // [t][n]

Hist run_module(Module& m, const Hist& h, unsigned from, unsigned to) {
    const unsigned N = (unsigned)h[0].size();
    Hist out;
    std::vector<float> buf(N);
    for (unsigned t = from; t < to; ++t) {
        buf = h[t];
        SignalVector sv{buf.data(), N};
        m.calculate(t, sv);
        out.push_back(buf);
    }
    return out;
}

Hist run_module(Module& m, const Hist& h) {
    m.numAssets = (unsigned)h[0].size();
    m.init();
    return run_module(m, h, 0, (unsigned)h.size());
}

void expect_same(float got, float want, double tol, const char* what, unsigned t, unsigned n) {
    if (std::isnan(want)) {
        EXPECT_TRUE(std::isnan(got)) << what << " t=" << t << " n=" << n << " got " << got;
    } else if (tol == 0.0) {
        EXPECT_EQ(got, want) << what << " t=" << t << " n=" << n;
    } else {
        EXPECT_NEAR(got, want, tol * std::max(1.0, std::fabs((double)want))) << what << " t=" << t << " n=" << n;
    }
}

void expect_hist_same(const Hist& got, const Hist& want, double tol, const char* what) {
    ASSERT_EQ(got.size(), want.size());
    for (unsigned t = 0; t < got.size(); ++t)
        for (unsigned n = 0; n < got[t].size(); ++n) expect_same(got[t][n], want[t][n], tol, what, t, n);
}

// ---------------------------------------------------------------------------
// ffd
// ---------------------------------------------------------------------------

// Columns: 0 all-NaN, 1 NaN until bar 50, 2 constant, 3.. random with ~15% NaN.
Hist make_history(unsigned T, unsigned N, unsigned seed) {
    std::mt19937 rng(seed);
    std::uniform_real_distribution<float> u(10.0f, 200.0f);
    std::uniform_real_distribution<float> p(0.0f, 1.0f);
    Hist h(T, std::vector<float>(N));
    for (unsigned t = 0; t < T; ++t)
        for (unsigned n = 0; n < N; ++n) {
            if (n == 0) h[t][n] = kNaN;
            else if (n == 1) h[t][n] = t < 50 ? kNaN : u(rng);
            else if (n == 2) h[t][n] = 42.5f;
            else h[t][n] = p(rng) < 0.15f ? kNaN : u(rng);
        }
    return h;
}

std::vector<double> desk_weights(double d, double thres) {  // oldest-first, as getWeights_FFD
    std::vector<double> w{1.0};
    for (int k = 1; k < 1000; ++k) {
        const double next = -w.back() / k * (d - k + 1);
        if (std::fabs(next) < thres) break;
        w.push_back(next);
    }
    return std::vector<double>(w.rbegin(), w.rend());
}

Hist oracle_ffd(const Hist& h, double d, double thres) {
    const auto w = desk_weights(d, thres);
    const unsigned L = (unsigned)w.size(), T = (unsigned)h.size(), N = (unsigned)h[0].size();
    Hist out(T, std::vector<float>(N, kNaN));
    for (unsigned n = 0; n < N; ++n) {
        std::vector<double> ff(T, std::numeric_limits<double>::quiet_NaN());
        int first = -1;
        double last = std::numeric_limits<double>::quiet_NaN();
        for (unsigned t = 0; t < T; ++t) {
            if (std::isfinite(h[t][n])) {
                last = h[t][n];
                if (first < 0) first = (int)t;
            }
            ff[t] = last;
        }
        for (unsigned t = 0; t < T; ++t) {
            if (!std::isfinite(h[t][n]) || first < 0 || t + 1 < (unsigned)first + L) continue;
            double s = 0.0;
            for (unsigned k = 0; k < L; ++k) s += w[k] * ff[t - L + 1 + k];
            out[t][n] = (float)s;
        }
    }
    return out;
}

TEST(Ffd, WeightsFollowTheDeskRecursion) {
    MFfd m;
    m.d = FloatParameter(0.5f);
    m.thres = FloatParameter(0.01f);
    m.numAssets = 1;
    m.init();
    const auto want = desk_weights((double)0.5f, (double)0.01f);
    ASSERT_EQ(m.weights.size(), want.size());
    for (size_t k = 0; k < want.size(); ++k) EXPECT_EQ(m.weights[k], want[k]) << k;
    EXPECT_EQ(m.weights.back(), 1.0);

    MFfd book;  // the combo15 setting hits the 1000-weight cap
    book.d = FloatParameter(0.24f);
    book.thres = FloatParameter(0.00001f);
    book.numAssets = 1;
    book.init();
    EXPECT_EQ(book.L, 1000u);
}

TEST(Ffd, MatchesOracle) {
    const Hist h = make_history(400, 12, 3);
    for (auto [d, thres] : {std::pair{0.24f, 0.01f}, std::pair{0.5f, 0.001f}, std::pair{1.0f, 0.1f}}) {
        MFfd m;
        m.d = FloatParameter(d);
        m.thres = FloatParameter(thres);
        expect_hist_same(run_module(m, h), oracle_ffd(h, (double)d, (double)thres), 1e-5, "ffd");
    }
}

TEST(Ffd, DZeroIsIdentityOnFiniteInput) {
    const Hist h = make_history(100, 6, 4);
    MFfd m;
    m.d = FloatParameter(0.0f);
    const Hist out = run_module(m, h);
    EXPECT_EQ(m.L, 1u);
    for (unsigned t = 0; t < h.size(); ++t)
        for (unsigned n = 0; n < h[t].size(); ++n)
            expect_same(out[t][n], std::isfinite(h[t][n]) ? h[t][n] : kNaN, 0.0, "ffd d=0", t, n);
}

// ---------------------------------------------------------------------------
// pcaresid
// ---------------------------------------------------------------------------

// Two sectors (0: n<5, 1: 5<=n<9) and one unclassified instrument (9). Instrument 3 moves to sector 1 at
// bar 200, instrument 7 is missing at bar 75, instrument 8 is constant (zero std), instrument 4 is out of
// the run universe at bar 10 only.
struct Fixture {
    unsigned T, N;
    DataPanel dp;
    Hist x;
    std::vector<uint8_t> mask;
    float code(unsigned t, unsigned n) const { return dp.arrays.at("sector")[(size_t)t * N + n]; }
};

Fixture make_fixture(unsigned T, unsigned N, unsigned seed) {
    Fixture f{T, N, {}, Hist(T, std::vector<float>(N)), std::vector<uint8_t>((size_t)T * N, 1)};
    f.dp.T = T;
    f.dp.N = N;
    auto& sector = f.dp.arrays["sector"];
    sector.resize((size_t)T * N);
    std::mt19937 rng(seed);
    std::normal_distribution<double> g(0.0, 1.0);
    std::vector<double> load(N);
    for (auto& l : load) l = 0.5 + 0.5 * std::fabs(g(rng));
    for (unsigned t = 0; t < T; ++t) {
        const double f0 = g(rng), f1 = g(rng);
        for (unsigned n = 0; n < N; ++n) {
            float code = n < 5 ? 0.0f : n < 9 ? 1.0f : kNaN;
            if (n == 3 && t >= 200) code = 1.0f;
            sector[(size_t)t * N + n] = code;
            const double factor = (n < 5 && !(n == 3 && t >= 200)) ? f0 : f1;
            double v = 100.0 + 10.0 * load[n] * factor + 2.0 * g(rng);
            if (n == 8) v = 55.0;
            f.x[t][n] = (n == 7 && t == 75) ? kNaN : (float)v;
        }
    }
    f.mask[(size_t)10 * N + 4] = 0;
    return f;
}

struct OFit {
    float code;
    std::vector<unsigned> ids;
    std::vector<double> mean, sd;
    Eigen::VectorXd mu;
    Eigen::MatrixXd V;
};

std::vector<OFit> oracle_fit(const Fixture& f, unsigned b, unsigned train, double var, int kfix, bool grouped) {
    std::vector<OFit> fits;
    std::vector<float> codes;
    std::vector<unsigned> cand;
    std::vector<float> code_of(f.N, 0.0f);
    for (unsigned n = 0; n < f.N; ++n) {
        const float c0 = grouped ? f.code(b - train, n) : 0.0f;
        if (grouped && !(std::isfinite(c0) && c0 >= 0.0f)) continue;
        bool ok = true;
        for (unsigned r = b - train; r < b && ok; ++r)
            ok = f.mask[(size_t)r * f.N + n] && (!grouped || f.code(r, n) == c0) && std::isfinite(f.x[r][n]);
        if (ok) { cand.push_back(n); code_of[n] = c0; codes.push_back(c0); }
    }
    std::sort(codes.begin(), codes.end());
    codes.erase(std::unique(codes.begin(), codes.end()), codes.end());
    for (float gcode : codes) {
        OFit o;
        o.code = gcode;
        for (unsigned n : cand) {
            if (code_of[n] != gcode) continue;
            double s = 0.0, s2 = 0.0;
            for (unsigned r = b - train; r < b; ++r) s += f.x[r][n];
            const double m = s / train;
            for (unsigned r = b - train; r < b; ++r) s2 += (f.x[r][n] - m) * (f.x[r][n] - m);
            const double sd = std::sqrt(s2 / train);
            if (!(sd > 0.0)) continue;
            o.ids.push_back(n);
            o.mean.push_back(m);
            o.sd.push_back(sd);
        }
        const int p = (int)o.ids.size();
        if (p == 0) continue;
        Eigen::MatrixXd Z(train, p);
        for (unsigned r = 0; r < train; ++r)
            for (int j = 0; j < p; ++j) Z(r, j) = (f.x[b - train + r][o.ids[j]] - o.mean[j]) / o.sd[j];
        o.mu = Z.colwise().mean();
        const Eigen::MatrixXd C = Z.rowwise() - o.mu.transpose();
        Eigen::JacobiSVD<Eigen::MatrixXd> svd(C, Eigen::ComputeThinV);
        const Eigen::VectorXd s2 = svd.singularValues().array().square();
        const double total = s2.sum();
        int k = kfix > 0 ? std::min(kfix, p) : 1;
        if (kfix <= 0) {
            double cum = 0.0;
            for (int j = 0; j < p; ++j) {
                cum += s2(j) / total;
                if (cum >= var) { k = j + 1; break; }
            }
        }
        o.V = svd.matrixV().leftCols(k);
        fits.push_back(std::move(o));
    }
    return fits;
}

std::vector<float> oracle_resid(const Fixture& f, unsigned t, const std::vector<OFit>& fits, bool grouped) {
    std::vector<float> out(f.N, kNaN);
    for (const OFit& o : fits) {
        std::vector<int> obs;
        std::vector<double> d;
        for (size_t j = 0; j < o.ids.size(); ++j) {
            const unsigned n = o.ids[j];
            if (!std::isfinite(f.x[t][n]) || (grouped && f.code(t, n) != o.code)) continue;
            obs.push_back((int)j);
            d.push_back((f.x[t][n] - o.mean[j]) / o.sd[j] - o.mu(j));
        }
        if (obs.empty()) continue;
        Eigen::MatrixXd Vo(obs.size(), o.V.cols());
        Eigen::VectorXd dv(obs.size());
        for (size_t i = 0; i < obs.size(); ++i) {
            Vo.row(i) = o.V.row(obs[i]);
            dv(i) = d[i];
        }
        const Eigen::VectorXd s = Vo.jacobiSvd(Eigen::ComputeThinU | Eigen::ComputeThinV).solve(dv);
        const Eigen::VectorXd fit = Vo * s;
        for (size_t i = 0; i < obs.size(); ++i) out[o.ids[obs[i]]] = (float)(fit(i) - dv(i));
    }
    return out;
}

Hist oracle_pcaresid(const Fixture& f, unsigned train, unsigned refit, double var, int kfix, bool grouped) {
    Hist out(f.T, std::vector<float>(f.N, kNaN));
    std::vector<OFit> fits;
    for (unsigned t = 0; t < f.T; ++t) {
        if (t >= train && t % refit == 0) fits = oracle_fit(f, t, train, var, kfix, grouped);
        out[t] = oracle_resid(f, t, fits, grouped);
    }
    return out;
}

MPcaResid make_module(const Fixture& f, int train, int refit, float var, int k, bool grouped) {
    MPcaResid m;
    m.panel = &f.dp;
    m.universe = &f.mask;
    if (grouped) m.group = GroupParameter("sector");
    m.train = IntParameter(train);
    m.refit = IntParameter(refit);
    m.var = FloatParameter(var);
    m.k = IntParameter(k);
    return m;
}

TEST(PcaResid, MatchesOracleByGroup) {
    const Fixture f = make_fixture(300, 10, 11);
    for (float var : {0.6f, 0.9f, 0.99f}) {
        MPcaResid m = make_module(f, 60, 30, var, 0, true);
        expect_hist_same(run_module(m, f.x), oracle_pcaresid(f, 60, 30, (double)var, 0, true), 1e-4, "pcaresid");
    }
}

TEST(PcaResid, MatchesOracleFixedKAndUngrouped) {
    const Fixture f = make_fixture(300, 10, 12);
    MPcaResid fixed = make_module(f, 60, 30, 0.9f, 2, true);
    expect_hist_same(run_module(fixed, f.x), oracle_pcaresid(f, 60, 30, 0.9, 2, true), 1e-4, "pcaresid k=2");
    for (const auto& fit : fixed.fits) {
        EXPECT_EQ(fit.k, 2);
        for (unsigned n : fit.ids) EXPECT_NE(n, 8u) << "zero-std column must be dropped";
    }
    MPcaResid whole = make_module(f, 60, 30, 0.9f, 0, false);
    expect_hist_same(run_module(whole, f.x), oracle_pcaresid(f, 60, 30, 0.9, 0, false), 1e-4, "pcaresid ungrouped");
    ASSERT_EQ(whole.fits.size(), 1u);
}

TEST(PcaResid, FitSetAndBlockRules) {
    const Fixture f = make_fixture(300, 10, 13);
    MPcaResid m = make_module(f, 60, 30, 0.9f, 0, true);
    const Hist out = run_module(m, f.x);
    for (unsigned t = 0; t < 60; ++t)
        for (unsigned n = 0; n < f.N; ++n) EXPECT_TRUE(std::isnan(out[t][n])) << "before the first fit t=" << t;
    for (unsigned t = 60; t < 300; ++t) {
        EXPECT_TRUE(std::isnan(out[t][8])) << "constant column t=" << t;
        EXPECT_TRUE(std::isnan(out[t][9])) << "unclassified instrument t=" << t;
        EXPECT_FALSE(std::isnan(out[t][0])) << "fitted instrument t=" << t;
    }
    // Instrument 4 left the run universe at bar 10: not in the first fit (rows [0, 60)), fitted from bar 90.
    for (unsigned t = 60; t < 90; ++t) EXPECT_TRUE(std::isnan(out[t][4])) << t;
    for (unsigned t = 90; t < 300; ++t) EXPECT_FALSE(std::isnan(out[t][4])) << t;
    // Instrument 7 is missing at bar 75: no output at 75, excluded from the fits that train on it (bars 90 and 120).
    EXPECT_TRUE(std::isnan(out[75][7]));
    for (unsigned t = 90; t < 150; ++t) EXPECT_TRUE(std::isnan(out[t][7])) << t;
    EXPECT_FALSE(std::isnan(out[150][7]));
    // Instrument 3 changes sector at bar 200: it keeps the sector-0 fit until then, then has no fit with its
    // new code until a block trains entirely on the new code (bar 270 trains on [210, 270)).
    EXPECT_FALSE(std::isnan(out[199][3]));
    for (unsigned t = 200; t < 270; ++t) EXPECT_TRUE(std::isnan(out[t][3])) << t;
    EXPECT_FALSE(std::isnan(out[270][3]));
}

// ---------------------------------------------------------------------------
// XML compiler
// ---------------------------------------------------------------------------

TEST(FfdPcaResidXml, DefaultsExplicitAttributesAndErrors) {
    Fixture f = make_fixture(1300, 10, 15);   // long enough for the default train=1260
    f.dp.arrays["close"] = std::vector<float>((size_t)f.T * f.N, 1.0f);
    const DataPanel* dp = &f.dp;

    auto compiled = compile_xml("<data expr=\"close\" delay=\"1\"/><ffd/><pcaresid group=\"sector\"/>", dp, &f.mask, nullptr);
    ASSERT_EQ(compiled.size(), 1u);
    MFfd* ffd = nullptr;
    MPcaResid* pca = nullptr;
    for (auto& m : compiled[0].modules) {
        if (auto* p = dynamic_cast<MFfd*>(m.get())) ffd = p;
        if (auto* p = dynamic_cast<MPcaResid*>(m.get())) pca = p;
    }
    ASSERT_NE(ffd, nullptr);
    ASSERT_NE(pca, nullptr);
    EXPECT_EQ((float)ffd->d, 0.24f);
    EXPECT_EQ((float)ffd->thres, 0.00001f);
    EXPECT_EQ(pca->group.name(), "sector");
    EXPECT_EQ((int)pca->train, 1260);
    EXPECT_EQ((int)pca->refit, 252);
    EXPECT_EQ((int)pca->k, 0);
    EXPECT_EQ((float)pca->var, 0.9f);
    EXPECT_EQ(pca->universe, &f.mask);
    EXPECT_EQ(pca->panel, dp);

    compiled = compile_xml("<data expr=\"close\" delay=\"1\"/><ffd d=\"0.5\" thres=\"0.01\"/>"
                           "<pcaresid train=\"120\" refit=\"21\" k=\"3\" var=\"0.5\"/>", dp, &f.mask, nullptr);
    for (auto& m : compiled[0].modules) {
        if (auto* p = dynamic_cast<MFfd*>(m.get())) { EXPECT_EQ((float)p->d, 0.5f); EXPECT_EQ((float)p->thres, 0.01f); }
        if (auto* p = dynamic_cast<MPcaResid*>(m.get())) {
            EXPECT_TRUE(p->group.name().empty());
            EXPECT_EQ((int)p->train, 120);
            EXPECT_EQ((int)p->refit, 21);
            EXPECT_EQ((int)p->k, 3);
            EXPECT_EQ((float)p->var, 0.5f);
        }
    }

    for (const char* bad : {"<data expr=\"close\" delay=\"1\"/><ffd thres=\"-1\"/>",
                            "<data expr=\"close\" delay=\"1\"/><ffd days=\"3\"/>",
                            "<data expr=\"close\" delay=\"1\"/><pcaresid var=\"1.5\"/>",
                            "<data expr=\"close\" delay=\"1\"/><pcaresid var=\"1\"/>",
                            "<data expr=\"close\" delay=\"1\"/><pcaresid var=\"0\"/>",
                            "<data expr=\"close\" delay=\"1\"/><pcaresid train=\"1\"/>",
                            "<data expr=\"close\" delay=\"1\"/><pcaresid train=\"1301\"/>",
                            "<data expr=\"close\" delay=\"1\"/><pcaresid k=\"-1\"/>",
                            "<data expr=\"close\" delay=\"1\"/><pcaresid group=\"country\"/>"})
        EXPECT_THROW(compile_xml(bad, dp, &f.mask, nullptr), CompileError) << bad;
}

}  // namespace
