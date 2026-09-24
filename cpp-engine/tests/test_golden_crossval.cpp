#include <gtest/gtest.h>

#include <cmath>
#include <filesystem>
#include <fstream>
#include <limits>
#include <sstream>
#include <string>
#include <vector>

#include <json/json.h>

#include "module.h"
#include "signal_vector.h"
#include "data/data_panel.h"
#include "metrics/summary_metrics.h"
#include "modules/Decay.h"
#include "modules/Delay.h"
#include "modules/Neutralize.h"
#include "modules/Rank.h"
#include "modules/TsMean.h"
#include "modules/TsRank.h"
#include "modules/TsZscore.h"
#include "modules/Zscore.h"

namespace fs = std::filesystem;

namespace {

constexpr float kTol = 1e-4f;
constexpr double kMetricTol = 1e-9;

// ── JSON loading ──

fs::path golden_dir()
{
    // Try paths relative to the test binary location
    const std::vector<fs::path> candidates = {
        fs::current_path() / "../tests/golden",
        fs::current_path() / "tests/golden",
        fs::current_path() / "../../tests/golden",
    };
    for (const auto& p : candidates) {
        if (fs::exists(p / "modules.json"))
            return fs::weakly_canonical(p);
    }
    return {};
}

Json::Value load_json(const fs::path& path)
{
    std::ifstream ifs(path);
    EXPECT_TRUE(ifs.good()) << "Cannot open " << path;
    Json::Value root;
    Json::CharReaderBuilder rb;
    std::string errs;
    EXPECT_TRUE(Json::parseFromStream(rb, ifs, &root, &errs)) << errs;
    return root;
}

// ── Matrix helpers ──

using Matrix = std::vector<std::vector<float>>;

Matrix json_to_matrix(const Json::Value& arr)
{
    Matrix out;
    for (Json::ArrayIndex i = 0; i < arr.size(); ++i) {
        std::vector<float> row;
        const Json::Value& jrow = arr[i];
        for (Json::ArrayIndex j = 0; j < jrow.size(); ++j) {
            if (jrow[j].isNull())
                row.push_back(std::numeric_limits<float>::quiet_NaN());
            else
                row.push_back(static_cast<float>(jrow[j].asDouble()));
        }
        out.push_back(std::move(row));
    }
    return out;
}

Matrix run_module(Module& mod, const Matrix& input)
{
    if (input.empty())
        return {};
    const unsigned T = static_cast<unsigned>(input.size());
    const unsigned N = static_cast<unsigned>(input[0].size());

    mod.numAssets = N;
    mod.init();

    Matrix out(T, std::vector<float>(N, std::numeric_limits<float>::quiet_NaN()));
    std::vector<float> buf(N);
    SignalVector sv{buf.data(), N};

    for (unsigned t = 0; t < T; ++t) {
        for (unsigned n = 0; n < N; ++n)
            buf[n] = input[t][n];
        mod.calculate(t, sv);
        out[t] = buf;
    }
    return out;
}

bool is_close_or_both_nan(float a, float b, float tol)
{
    if (std::isnan(a) && std::isnan(b))
        return true;
    if (std::isnan(a) || std::isnan(b))
        return false;
    return std::fabs(a - b) <= tol;
}

void expect_matrix_match(const Matrix& actual, const Matrix& expected, const std::string& label, float tol = kTol)
{
    ASSERT_EQ(actual.size(), expected.size()) << label << ": row count mismatch";
    for (size_t t = 0; t < actual.size(); ++t) {
        ASSERT_EQ(actual[t].size(), expected[t].size()) << label << " row=" << t;
        for (size_t n = 0; n < actual[t].size(); ++n) {
            EXPECT_TRUE(is_close_or_both_nan(actual[t][n], expected[t][n], tol))
                << label << " mismatch at t=" << t << " n=" << n
                << " cpp=" << actual[t][n]
                << " numpy=" << expected[t][n];
        }
    }
}

// ── Golden test fixture ──

class GoldenCrossval : public ::testing::Test {
protected:
    static void SetUpTestSuite()
    {
        dir_ = golden_dir();
        if (dir_.empty()) {
            available_ = false;
            return;
        }
        modules_ = load_json(dir_ / "modules.json");
        metrics_ = load_json(dir_ / "metrics.json");
        available_ = true;
    }

    static fs::path dir_;
    static Json::Value modules_;
    static Json::Value metrics_;
    static bool available_;
};

fs::path GoldenCrossval::dir_;
Json::Value GoldenCrossval::modules_;
Json::Value GoldenCrossval::metrics_;
bool GoldenCrossval::available_ = false;

}  // namespace

// ── Module tests: C++ module output vs numpy/pandas golden ──

TEST_F(GoldenCrossval, DelayMatchesNumpy)
{
    if (!available_)
        GTEST_SKIP() << "golden files not found";

    const auto& entry = modules_["delay_1"];
    Matrix input = json_to_matrix(entry["input"]);
    Matrix expected = json_to_matrix(entry["expected"]);

    MDelay mod;
    mod.days = IntParameter(entry["params"]["days"].asInt());
    Matrix actual = run_module(mod, input);

    expect_matrix_match(actual, expected, "Delay(1)");
}

TEST_F(GoldenCrossval, TsMeanMatchesNumpy)
{
    if (!available_)
        GTEST_SKIP() << "golden files not found";

    const auto& entry = modules_["tsmean_3"];
    Matrix input = json_to_matrix(entry["input"]);
    Matrix expected = json_to_matrix(entry["expected"]);

    MTsMean mod;
    mod.days = IntParameter(entry["params"]["days"].asInt());
    Matrix actual = run_module(mod, input);

    expect_matrix_match(actual, expected, "TsMean(3)");
}

TEST_F(GoldenCrossval, TsZscoreMatchesNumpy)
{
    if (!available_)
        GTEST_SKIP() << "golden files not found";

    const auto& entry = modules_["tszscore_3"];
    Matrix input = json_to_matrix(entry["input"]);
    Matrix expected = json_to_matrix(entry["expected"]);

    MTsZscore mod;
    mod.days = IntParameter(entry["params"]["days"].asInt());
    mod.ddof = IntParameter(entry["params"]["ddof"].asInt());
    mod.min_count = IntParameter(entry["params"]["min_count"].asInt());
    mod.eps = FloatParameter(entry["params"]["eps"].asDouble());
    Matrix actual = run_module(mod, input);

    expect_matrix_match(actual, expected, "TsZscore(3)");
}

TEST_F(GoldenCrossval, TsRankMatchesNumpy)
{
    if (!available_)
        GTEST_SKIP() << "golden files not found";

    const auto& entry = modules_["tsrank_3"];
    Matrix input = json_to_matrix(entry["input"]);
    Matrix expected = json_to_matrix(entry["expected"]);

    MTsRank mod;
    mod.days = IntParameter(entry["params"]["days"].asInt());
    mod.pct = IntParameter(entry["params"]["pct"].asBool() ? 1 : 0);
    mod.min_count = IntParameter(entry["params"]["min_count"].asInt());
    Matrix actual = run_module(mod, input);

    expect_matrix_match(actual, expected, "TsRank(3)");
}

TEST_F(GoldenCrossval, DecayMatchesNumpy)
{
    if (!available_)
        GTEST_SKIP() << "golden files not found";

    const auto& entry = modules_["decay_3"];
    Matrix input = json_to_matrix(entry["input"]);
    Matrix expected = json_to_matrix(entry["expected"]);

    MDecay mod;
    mod.days = IntParameter(entry["params"]["days"].asInt());
    Matrix actual = run_module(mod, input);

    expect_matrix_match(actual, expected, "Decay(3)");
}

TEST_F(GoldenCrossval, RankMatchesNumpy)
{
    if (!available_)
        GTEST_SKIP() << "golden files not found";

    const auto& entry = modules_["rank_pct"];
    Matrix input = json_to_matrix(entry["input"]);
    Matrix expected = json_to_matrix(entry["expected"]);

    MRank mod;
    mod.pct = IntParameter(entry["params"]["pct"].asBool() ? 1 : 0);
    Matrix actual = run_module(mod, input);

    expect_matrix_match(actual, expected, "Rank(pct)");
}

TEST_F(GoldenCrossval, ZscoreMatchesNumpy)
{
    if (!available_)
        GTEST_SKIP() << "golden files not found";

    const auto& entry = modules_["zscore"];
    Matrix input = json_to_matrix(entry["input"]);
    Matrix expected = json_to_matrix(entry["expected"]);

    MZscore mod;
    mod.ddof = IntParameter(entry["params"]["ddof"].asInt());
    mod.min_count = IntParameter(entry["params"]["min_count"].asInt());
    mod.eps = FloatParameter(entry["params"]["eps"].asDouble());
    Matrix actual = run_module(mod, input);

    expect_matrix_match(actual, expected, "Zscore");
}

TEST_F(GoldenCrossval, NeutralizeMatchesNumpy)
{
    if (!available_)
        GTEST_SKIP() << "golden files not found";

    for (const char* name : {"neutralize_sector", "neutralize_sector_unclassified"}) {
        SCOPED_TRACE(name);
        const auto& entry = modules_[name];
        Matrix input = json_to_matrix(entry["input"]);
        Matrix expected = json_to_matrix(entry["expected"]);
        ASSERT_FALSE(input.empty());

        const unsigned T = static_cast<unsigned>(input.size());
        const unsigned N = static_cast<unsigned>(input[0].size());
        const Json::Value& json_codes = entry["params"]["codes"];
        ASSERT_EQ(json_codes.size(), static_cast<Json::ArrayIndex>(N));

        // The group panel broadcasts the static codes over every date.
        DataPanel dp;
        dp.T = T;
        dp.N = N;
        auto& codes = dp.arrays["sector"];
        codes.resize(static_cast<size_t>(T) * N);
        for (unsigned t = 0; t < T; ++t) {
            for (unsigned n = 0; n < N; ++n) {
                codes[static_cast<size_t>(t) * N + n] =
                    json_codes[n].isNull() ? std::numeric_limits<float>::quiet_NaN()
                                           : static_cast<float>(json_codes[n].asDouble());
            }
        }

        MNeutralize mod;
        mod.panel = &dp;
        mod.group = entry["params"]["group"].asString();
        mod.column_name = mod.group;
        Matrix actual = run_module(mod, input);

        expect_matrix_match(actual, expected, name);
    }
}

// ── Metrics test: C++ compute_summary vs numpy/pandas golden ──

TEST_F(GoldenCrossval, SummaryMetricsMatchNumpy)
{
    if (!available_)
        GTEST_SKIP() << "golden files not found";

    const Json::Value& input = metrics_["input"];
    const Json::Value& expected = metrics_["expected"];
    const unsigned bars_per_year = metrics_["bars_per_year"].asUInt();

    // Load input vectors from golden file
    const auto load_vec_double = [](const Json::Value& arr) {
        std::vector<double> v;
        v.reserve(arr.size());
        for (Json::ArrayIndex i = 0; i < arr.size(); ++i) {
            if (arr[i].isNull())
                v.push_back(std::numeric_limits<double>::quiet_NaN());
            else
                v.push_back(arr[i].asDouble());
        }
        return v;
    };

    const auto load_vec_int = [](const Json::Value& arr) {
        std::vector<int> v;
        v.reserve(arr.size());
        for (Json::ArrayIndex i = 0; i < arr.size(); ++i)
            v.push_back(arr[i].asInt());
        return v;
    };

    auto ret = load_vec_double(input["ret"]);
    auto turnover = load_vec_double(input["turnover"]);

    auto max_weight = load_vec_double(input["max_weight"]);
    auto long_weight = load_vec_double(input["long_weight"]);
    auto short_weight = load_vec_double(input["short_weight"]);
    auto position_count = load_vec_int(input["position_count"]);

    ASSERT_EQ(ret.size(), 140u);

    const auto actual = compute_summary(
        ret, turnover, max_weight, long_weight, short_weight, position_count, bars_per_year);

    EXPECT_NEAR(actual.pnl, expected["pnl"].asDouble(), kMetricTol)
        << "pnl: cpp=" << actual.pnl << " numpy=" << expected["pnl"].asDouble();
    EXPECT_NEAR(actual.ann_ret, expected["ann_ret"].asDouble(), kMetricTol)
        << "ann_ret";
    EXPECT_NEAR(actual.sharpe, expected["sharpe"].asDouble(), kMetricTol)
        << "sharpe";
    EXPECT_NEAR(actual.mdd, expected["mdd"].asDouble(), kMetricTol)
        << "mdd";
    EXPECT_NEAR(actual.turnover, expected["turnover"].asDouble(), kMetricTol)
        << "turnover";
    EXPECT_NEAR(actual.avg_position_count, expected["avg_position_count"].asDouble(), kMetricTol)
        << "avg_position_count";
    EXPECT_NEAR(actual.max_weight, expected["max_weight"].asDouble(), kMetricTol)
        << "max_weight";
    EXPECT_EQ(actual.active_bar_count, expected["active_bar_count"].asInt())
        << "active_bar_count";
    EXPECT_NEAR(actual.win_rate, expected["win_rate"].asDouble(), kMetricTol)
        << "win_rate";
    EXPECT_NEAR(actual.margin, expected["margin"].asDouble(), kMetricTol)
        << "margin";
    EXPECT_NEAR(actual.long_ratio, expected["long_ratio"].asDouble(), kMetricTol)
        << "long_ratio";
    EXPECT_NEAR(actual.short_ratio, expected["short_ratio"].asDouble(), kMetricTol)
        << "short_ratio";
    EXPECT_NEAR(actual.avg_max_bet, expected["avg_max_bet"].asDouble(), kMetricTol)
        << "avg_max_bet";
    EXPECT_NEAR(actual.min_position_count, expected["min_position_count"].asDouble(), kMetricTol)
        << "min_position_count";
    EXPECT_NEAR(actual.quality, expected["quality"].asDouble(), kMetricTol)
        << "quality";
    EXPECT_NEAR(actual.sharpe400, expected["sharpe400"].asDouble(), kMetricTol)
        << "sharpe400";
    EXPECT_NEAR(actual.sharpe80, expected["sharpe80"].asDouble(), kMetricTol)
        << "sharpe80";
    EXPECT_NEAR(actual.minsoc, expected["minsoc"].asDouble(), kMetricTol)
        << "minsoc";
}
