#include <gtest/gtest.h>

#include <cmath>
#include <limits>
#include <vector>

#include "module.h"
#include "signal_vector.h"
#include "modules/Decay.h"
#include "modules/Delay.h"
#include "modules/Rank.h"
#include "modules/TsMean.h"
#include "modules/TsRank.h"
#include "modules/TsZscore.h"
#include "modules/Zscore.h"

namespace {

using Matrix = std::vector<std::vector<float>>;

constexpr float kTol = 1e-5f;

float nanf()
{
    return std::numeric_limits<float>::quiet_NaN();
}

bool is_close_or_both_nan(float a, float b, float tol = kTol)
{
    if (std::isnan(a) && std::isnan(b))
        return true;
    if (std::isnan(a) || std::isnan(b))
        return false;
    return std::fabs(a - b) <= tol;
}

void expect_matrix_near(const Matrix& actual, const Matrix& expected, float tol = kTol)
{
    ASSERT_EQ(actual.size(), expected.size());
    for (size_t t = 0; t < actual.size(); ++t) {
        ASSERT_EQ(actual[t].size(), expected[t].size()) << "row=" << t;
        for (size_t n = 0; n < actual[t].size(); ++n) {
            EXPECT_TRUE(is_close_or_both_nan(actual[t][n], expected[t][n], tol))
                << "mismatch at t=" << t << " n=" << n
                << " actual=" << actual[t][n]
                << " expected=" << expected[t][n];
        }
    }
}

Matrix make_fixture()
{
    return {
        {10.0f, 20.0f, nanf()},
        {20.0f, 20.0f, 5.0f},
        {30.0f, nanf(), 5.0f},
        {40.0f, 10.0f, 5.0f},
        {nanf(), 30.0f, 5.0f},
        {60.0f, 40.0f, nanf()},
    };
}

Matrix run_module(Module& mod, const Matrix& input)
{
    if (input.empty())
        return {};
    const unsigned T = static_cast<unsigned>(input.size());
    const unsigned N = static_cast<unsigned>(input[0].size());

    mod.numAssets = N;
    mod.init();

    Matrix out(T, std::vector<float>(N, nanf()));
    std::vector<float> buf(N, nanf());
    SignalVector sv{buf.data(), N};

    for (unsigned t = 0; t < T; ++t) {
        for (unsigned n = 0; n < N; ++n)
            buf[n] = input[t][n];
        mod.calculate(t, sv);
        out[t] = buf;
    }
    return out;
}

Matrix oracle_delay(const Matrix& input, unsigned days)
{
    const size_t T = input.size();
    const size_t N = input[0].size();
    Matrix out(T, std::vector<float>(N, nanf()));
    for (size_t t = 0; t < T; ++t) {
        for (size_t n = 0; n < N; ++n) {
            if (t < days)
                out[t][n] = nanf();
            else
                out[t][n] = input[t - days][n];
        }
    }
    return out;
}

Matrix oracle_tsmean(const Matrix& input, unsigned days)
{
    const size_t T = input.size();
    const size_t N = input[0].size();
    Matrix out(T, std::vector<float>(N, nanf()));

    for (size_t t = 0; t < T; ++t) {
        for (size_t n = 0; n < N; ++n) {
            if (t + 1 < days) {
                out[t][n] = nanf();
                continue;
            }

            double sum = 0.0;
            int cnt = 0;
            for (size_t k = t + 1 - days; k <= t; ++k) {
                float v = input[k][n];
                if (std::isfinite(v)) {
                    sum += v;
                    ++cnt;
                }
            }
            out[t][n] = (cnt > 0) ? static_cast<float>(sum / static_cast<double>(cnt)) : nanf();
        }
    }
    return out;
}

Matrix oracle_tszscore(const Matrix& input, unsigned days, int ddof, int min_count, double eps)
{
    const size_t T = input.size();
    const size_t N = input[0].size();
    Matrix out(T, std::vector<float>(N, nanf()));

    for (size_t t = 0; t < T; ++t) {
        const size_t start = (t + 1 > days) ? (t + 1 - days) : 0;

        for (size_t n = 0; n < N; ++n) {
            float cur = input[t][n];
            std::vector<double> vals;
            vals.reserve(days);
            for (size_t k = start; k <= t; ++k) {
                float v = input[k][n];
                if (std::isfinite(v))
                    vals.push_back(v);
            }

            const int n_valid = static_cast<int>(vals.size());
            if (n_valid < min_count) {
                out[t][n] = 0.0f;
                continue;
            }
            if (!std::isfinite(cur)) {
                out[t][n] = nanf();
                continue;
            }

            double sum = 0.0;
            double sq_sum = 0.0;
            for (double v : vals) {
                sum += v;
                sq_sum += v * v;
            }
            double mean = sum / static_cast<double>(n_valid);
            int denom = n_valid - ddof;
            double variance = (denom > 0)
                ? (sq_sum - (sum * sum) / static_cast<double>(n_valid)) / static_cast<double>(denom)
                : 0.0;
            if (variance < 0.0)
                variance = 0.0;
            double std_dev = std::sqrt(variance);
            if (std_dev < eps)
                out[t][n] = 0.0f;
            else
                out[t][n] = static_cast<float>((cur - mean) / (std_dev + eps));
        }
    }
    return out;
}

Matrix oracle_tsrank(const Matrix& input, unsigned days, bool pct, int min_count)
{
    const size_t T = input.size();
    const size_t N = input[0].size();
    Matrix out(T, std::vector<float>(N, nanf()));

    for (size_t t = 0; t < T; ++t) {
        const size_t start = (t + 1 > days) ? (t + 1 - days) : 0;
        for (size_t n = 0; n < N; ++n) {
            float cur = input[t][n];
            if (std::isnan(cur)) {
                out[t][n] = nanf();
                continue;
            }

            int n_valid = 0;
            int n_less = 0;
            int n_equal = 0;
            for (size_t k = start; k <= t; ++k) {
                float v = input[k][n];
                if (std::isnan(v))
                    continue;
                ++n_valid;
                if (v < cur)
                    ++n_less;
                else if (v == cur)
                    ++n_equal;
            }

            if (n_valid < min_count) {
                out[t][n] = nanf();
                continue;
            }

            double rank = n_less + 1.0 + (n_equal - 1.0) / 2.0;
            out[t][n] = pct
                ? static_cast<float>(rank / static_cast<double>(n_valid))
                : static_cast<float>(rank);
        }
    }
    return out;
}

Matrix oracle_decay(const Matrix& input, unsigned days)
{
    const size_t T = input.size();
    const size_t N = input[0].size();
    Matrix out(T, std::vector<float>(N, nanf()));

    for (size_t t = 0; t < T; ++t) {
        for (size_t n = 0; n < N; ++n) {
            double weighted_sum = 0.0;
            double weight_total = 0.0;
            for (size_t k = 0; k < days; ++k) {
                if (t < k)
                    break;
                float v = input[t - k][n];
                if (!std::isfinite(v))
                    continue;
                const double w = static_cast<double>(days - k);
                weighted_sum += static_cast<double>(v) * w;
                weight_total += w;
            }
            out[t][n] = (weight_total > 0.0)
                ? static_cast<float>(weighted_sum / weight_total)
                : nanf();
        }
    }
    return out;
}

Matrix oracle_rank(const Matrix& input, bool pct)
{
    const size_t T = input.size();
    const size_t N = input[0].size();
    Matrix out(T, std::vector<float>(N, nanf()));

    for (size_t t = 0; t < T; ++t) {
        int n_finite = 0;
        for (size_t n = 0; n < N; ++n) {
            if (std::isfinite(input[t][n]))
                ++n_finite;
        }
        if (n_finite == 0) {
            for (size_t n = 0; n < N; ++n)
                out[t][n] = nanf();
            continue;
        }

        for (size_t n = 0; n < N; ++n) {
            float vi = input[t][n];
            if (!std::isfinite(vi)) {
                out[t][n] = nanf();
                continue;
            }
            int n_less = 0;
            int n_equal = 0;
            for (size_t j = 0; j < N; ++j) {
                float vj = input[t][j];
                if (!std::isfinite(vj))
                    continue;
                if (vj < vi)
                    ++n_less;
                else if (vj == vi)
                    ++n_equal;
            }
            double rank = n_less + 1.0 + (n_equal - 1.0) / 2.0;
            out[t][n] = pct
                ? static_cast<float>(rank / static_cast<double>(n_finite))
                : static_cast<float>(rank);
        }
    }
    return out;
}

Matrix oracle_zscore(const Matrix& input, int ddof, int min_count, double eps)
{
    const size_t T = input.size();
    const size_t N = input[0].size();
    Matrix out(T, std::vector<float>(N, nanf()));

    for (size_t t = 0; t < T; ++t) {
        std::vector<double> vals;
        vals.reserve(N);
        for (size_t n = 0; n < N; ++n) {
            float v = input[t][n];
            if (std::isfinite(v))
                vals.push_back(v);
        }

        const int cnt = static_cast<int>(vals.size());
        if (cnt < min_count) {
            for (size_t n = 0; n < N; ++n)
                out[t][n] = nanf();
            continue;
        }

        double sum = 0.0;
        for (double v : vals)
            sum += v;
        double mean = sum / static_cast<double>(cnt);

        double sq_sum = 0.0;
        for (double v : vals) {
            double d = v - mean;
            sq_sum += d * d;
        }
        double denom = static_cast<double>(cnt - ddof);
        double scale = (denom > 0.0) ? std::sqrt(sq_sum / denom) : 0.0;
        if (scale < eps) {
            for (size_t n = 0; n < N; ++n)
                out[t][n] = nanf();
            continue;
        }

        for (size_t n = 0; n < N; ++n) {
            float v = input[t][n];
            out[t][n] = std::isfinite(v)
                ? static_cast<float>((v - mean) / (scale + eps))
                : nanf();
        }
    }
    return out;
}

}  // namespace

TEST(ModuleOracle, DelayDays1MatchesReference)
{
    Matrix input = make_fixture();
    Matrix expected = oracle_delay(input, 1);

    MDelay mod;
    mod.days = IntParameter(1);
    Matrix actual = run_module(mod, input);

    expect_matrix_near(actual, expected);
}

TEST(ModuleOracle, TsMeanDays3MatchesReference)
{
    Matrix input = make_fixture();
    Matrix expected = oracle_tsmean(input, 3);

    MTsMean mod;
    mod.days = IntParameter(3);
    Matrix actual = run_module(mod, input);

    expect_matrix_near(actual, expected);
}

TEST(ModuleOracle, TsZscoreMatchesReference)
{
    Matrix input = make_fixture();
    Matrix expected = oracle_tszscore(input, 3, 1, 3, 1e-12);

    MTsZscore mod;
    mod.days = IntParameter(3);
    mod.ddof = IntParameter(1);
    mod.min_count = IntParameter(3);
    mod.eps = FloatParameter(1e-12);
    Matrix actual = run_module(mod, input);

    expect_matrix_near(actual, expected);
}

TEST(ModuleOracle, TsRankMatchesReference)
{
    Matrix input = make_fixture();
    Matrix expected = oracle_tsrank(input, 3, true, 3);

    MTsRank mod;
    mod.days = IntParameter(3);
    mod.pct = IntParameter(1);
    mod.min_count = IntParameter(3);
    Matrix actual = run_module(mod, input);

    expect_matrix_near(actual, expected);
}

TEST(ModuleOracle, DecayDays3MatchesReference)
{
    Matrix input = make_fixture();
    Matrix expected = oracle_decay(input, 3);

    MDecay mod;
    mod.days = IntParameter(3);
    Matrix actual = run_module(mod, input);

    expect_matrix_near(actual, expected);
}

TEST(ModuleOracle, RankPctMatchesReference)
{
    Matrix input = make_fixture();
    Matrix expected = oracle_rank(input, true);

    MRank mod;
    mod.pct = IntParameter(1);
    Matrix actual = run_module(mod, input);

    expect_matrix_near(actual, expected);
}

TEST(ModuleOracle, ZscoreMatchesReference)
{
    Matrix input = make_fixture();
    Matrix expected = oracle_zscore(input, 1, 2, 1e-12);

    MZscore mod;
    mod.ddof = IntParameter(1);
    mod.min_count = IntParameter(2);
    mod.eps = FloatParameter(1e-12);
    Matrix actual = run_module(mod, input);

    expect_matrix_near(actual, expected);
}


TEST(ModuleOracles, ReinitializationClearsAllRollingState)
{
    const Matrix input = make_fixture();
    MTsMean mean; mean.days = IntParameter(3);
    MTsZscore z; z.days = IntParameter(3); z.min_count = IntParameter(2);
    MDelay delay; delay.days = IntParameter(2);
    MDecay decay; decay.days = IntParameter(3);
    for (Module* mod : std::vector<Module*>{&mean, &z, &delay, &decay}) {
        const Matrix first = run_module(*mod, input);
        expect_matrix_near(run_module(*mod, input), first);
        // Asset-count changes must also reset the ring's shape and history.
        Matrix wider = input;
        for (auto& row : wider) row.push_back(7.0f);
        run_module(*mod, wider);
        expect_matrix_near(run_module(*mod, input), first);
    }
}
