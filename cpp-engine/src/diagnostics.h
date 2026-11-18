#pragma once
// Signal diagnostics for output="diagnostics": forward-return IC / RankIC / quantile spread per
// horizon, coverage, top-k weight share, circular-shift null, group and conditional IC, signal
// distribution and rank autocorrelation. Forward returns are measured only; they never enter the
// pipeline. Spec: workspace research/workflow/engine_ext_spec_20261109.md section B.
#include <json/json.h>
#include <cstddef>
#include <cstdint>
#include <map>
#include <string>
#include <vector>

namespace diagnostics {

// Average ranks 1..n (ties share the mean of their positions). Inputs must be finite.
std::vector<double> average_ranks(const std::vector<double>& x);
// Pearson correlation; NaN when n < 2 or either side has zero variance.
double pearson(const std::vector<double>& x, const std::vector<double>& y);
// Mean of `y` per bucket q = ceil(rank(x)/n * Q), q in 1..Q; empty bucket -> NaN.
std::vector<double> quantile_means(const std::vector<double>& x, const std::vector<double>& y, int quantiles);
// mean / sqrt(NW long-run variance / N) with Bartlett weights 1 - l/(lag+1); NaN when N < 2 or
// the variance is not positive. Autocovariances use divisor N.
double newey_west_t(const std::vector<double>& x, int lag);
// Linear interpolation quantile (numpy default) of sorted finite values.
double sorted_quantile(const std::vector<double>& sorted, double q);

// Summary of one daily series over a set of days (non-finite values skipped).
struct SeriesSummary {
    std::size_t n = 0;
    std::size_t first = 0, last = 0;  // day positions of the first/last used value
    double mean = 0, std = 0, icir = 0, t = 0, hac_t = 0, positive = 0;
};
// days: positions into `values`, ascending. HAC lag as given.
SeriesSummary summarize(const std::vector<double>& values, const std::vector<std::size_t>& days, int hac_lag);

// Circular-shift null on per-date percentile-rank rows (NaN = not in that row). The statistic for
// shift k is the mean over dates j of Pearson(sig[j], ret[(j+k) % D]) on the columns finite in both,
// dates with fewer than `min_sample` common columns skipped. k ~ U{min_shift, ..., D - min_shift}.
struct NullResult {
    bool ok = false;
    std::string reason;
    std::size_t dates = 0;
    double actual = 0, mean = 0, std = 0, p = 0;
    std::vector<double> values;
};
NullResult circular_shift_null(const std::vector<std::vector<float>>& sig_ranks,
                               const std::vector<std::vector<float>>& ret_ranks,
                               int n, int min_shift, uint64_t seed, int min_sample, unsigned threads);

struct Options {
    std::vector<int> horizons;          // required, 1..252, unique (kept in request order)
    std::string is_end;                 // required YYYY-MM-DD
    int quantiles = 5;
    bool null_enabled = false;
    int null_n = 200, null_min_shift = 63;
    uint64_t null_seed = 0;
    std::vector<int> topk{1, 10};
    std::vector<std::string> groups;
    std::string condition_on;           // <signal id> label; empty = off
    int condition_buckets = 5;
    std::vector<int> autocorr_lags;
    std::vector<double> distribution_quantiles;
    int min_sample = 30;
};
// Parses the request's "diagnostics" object; throws std::invalid_argument.
Options parse_options(const Json::Value& value);

// Bar indexes of the run: dates t in [first, end) are reported, IS is first <= t with t + h <= is_end
// (inclusive bar), OS is t >= os_start.
struct Layout {
    unsigned first = 0, end = 0, is_end = 0, os_start = 0, N = 0;
};

class Accumulator {
public:
    Accumulator(const Options& options, const Layout& layout);
    // Every bar in [first, end) in order. member: nullptr = all members. returns: total return of
    // bar di per column. group_codes: one row per options.groups entry. condition: nullptr unless
    // conditionOn is set.
    void observe(unsigned di, const float* signal, const uint8_t* member, const double* returns,
                 const std::vector<const float*>& group_codes, const float* condition);
    Json::Value finish(const std::vector<int64_t>& timestamps, unsigned threads) const;

private:
    struct HorizonDaily {
        std::vector<double> ic, rankic, spread;
        std::vector<int> n;
        std::vector<std::vector<double>> quantile_means;  // [q][day]
    };
    struct GroupDaily {
        std::vector<double> rankic, size;
    };
    void evaluate(unsigned t, unsigned u, int h, std::size_t hi);
    void evaluate_h1_extras(unsigned t, const std::vector<unsigned>& cols, const std::vector<double>& s,
                            const std::vector<double>& f);
    std::size_t slot(unsigned di) const { return di % ring_; }

    Options options_;
    Layout layout_;
    std::vector<int> eval_h_;       // requested horizons plus 1 when an h=1 statistic needs it
    std::size_t ring_ = 1, D_ = 0;
    std::vector<float> sig_;        // ring x N, NaN when not a target
    std::vector<double> logcum_;    // ring x N cumulative log growth from `first`
    std::vector<int32_t> badcum_, zerocum_;  // ring x N cumulative non-finite / total-loss counts
    std::vector<float> cond_;       // 2 x N
    std::vector<float> codes_;      // 2 x G x N
    std::vector<HorizonDaily> daily_;
    std::vector<double> coverage_;
    std::vector<std::vector<double>> topk_;
    std::vector<std::map<float, GroupDaily>> groups_;
    std::vector<std::vector<double>> cond_rankic_, cond_spread_;  // [bucket][day]
    std::vector<double> dist_sum_;
    std::size_t dist_days_ = 0;
    double missing_sum_ = 0;
    std::size_t missing_days_ = 0;
    std::vector<double> ac_sum_;
    std::vector<std::size_t> ac_n_;
    std::vector<std::vector<float>> null_sig_, null_ret_;
};

}  // namespace diagnostics
