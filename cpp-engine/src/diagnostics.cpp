#include "diagnostics.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <limits>
#include <numeric>
#include <random>
#include <set>
#include <stdexcept>
#include <thread>

namespace diagnostics {

namespace {
constexpr double kNaN = std::numeric_limits<double>::quiet_NaN();

Json::Value number(double v) { return std::isfinite(v) ? Json::Value(v) : Json::Value(); }

Json::Value doubles(const std::vector<double>& values) {
    Json::Value out(Json::arrayValue);
    for (double v : values) out.append(number(v));
    return out;
}

double mean_of(const std::vector<double>& v) {
    if (v.empty()) return kNaN;
    double s = 0;
    for (double x : v) s += x;
    return s / static_cast<double>(v.size());
}

std::vector<int> parse_int_list(const Json::Value& v, const char* key, int lo, int hi, bool required_nonempty) {
    if (!v.isArray()) throw std::invalid_argument(std::string("diagnostics.") + key + " must be an array of integers");
    if (required_nonempty && v.empty()) throw std::invalid_argument(std::string("diagnostics.") + key + " must not be empty");
    std::vector<int> out;
    for (const auto& item : v) {
        if (!item.isIntegral() || !item.isInt64() || item.asInt64() < lo || item.asInt64() > hi)
            throw std::invalid_argument(std::string("diagnostics.") + key + " entries must be integers in " +
                                        std::to_string(lo) + ".." + std::to_string(hi));
        const int x = static_cast<int>(item.asInt64());
        if (std::find(out.begin(), out.end(), x) != out.end())
            throw std::invalid_argument(std::string("diagnostics.") + key + " has a duplicate entry " + std::to_string(x));
        out.push_back(x);
    }
    return out;
}

int parse_int(const Json::Value& v, const char* key, int lo, int hi) {
    if (!v.isIntegral() || !v.isInt64() || v.asInt64() < lo || v.asInt64() > hi)
        throw std::invalid_argument(std::string("diagnostics.") + key + " must be an integer in " +
                                    std::to_string(lo) + ".." + std::to_string(hi));
    return static_cast<int>(v.asInt64());
}

void reject_unknown(const Json::Value& v, const std::set<std::string>& allowed, const std::string& where) {
    for (const auto& name : v.getMemberNames())
        if (!allowed.count(name)) throw std::invalid_argument(where + ": unknown key '" + name + "'");
}

Json::Value summary_json(const SeriesSummary& s, const std::vector<int64_t>& ts, std::size_t first_bar) {
    Json::Value j;
    j["n"] = Json::UInt64(s.n);
    if (s.n == 0) {
        for (const char* k : {"firstTimestamp", "lastTimestamp", "mean", "std", "icir", "t", "hacT", "positiveFraction"})
            j[k] = Json::Value();
        return j;
    }
    j["firstTimestamp"] = Json::Int64(ts[first_bar + s.first]);
    j["lastTimestamp"] = Json::Int64(ts[first_bar + s.last]);
    j["mean"] = number(s.mean);
    j["std"] = number(s.std);
    j["icir"] = number(s.icir);
    j["t"] = number(s.t);
    j["hacT"] = number(s.hac_t);
    j["positiveFraction"] = number(s.positive);
    return j;
}

Json::Value os_json(const std::vector<double>& values, const std::vector<std::size_t>& days) {
    double sum = 0;
    std::size_t n = 0;
    for (auto d : days)
        if (std::isfinite(values[d])) { sum += values[d]; ++n; }
    Json::Value j;
    j["n"] = Json::UInt64(n);
    j["mean"] = n ? number(sum / static_cast<double>(n)) : Json::Value();
    return j;
}

double rank_ic(const std::vector<double>& s, const std::vector<double>& f) {
    return pearson(average_ranks(s), average_ranks(f));
}
}  // namespace

std::vector<double> average_ranks(const std::vector<double>& x) {
    const std::size_t n = x.size();
    std::vector<std::size_t> order(n);
    std::iota(order.begin(), order.end(), std::size_t{0});
    std::stable_sort(order.begin(), order.end(), [&](std::size_t a, std::size_t b) { return x[a] < x[b]; });
    std::vector<double> r(n);
    for (std::size_t i = 0; i < n;) {
        std::size_t j = i + 1;
        while (j < n && x[order[j]] == x[order[i]]) ++j;
        const double avg = (static_cast<double>(i + 1) + static_cast<double>(j)) / 2.0;
        for (std::size_t k = i; k < j; ++k) r[order[k]] = avg;
        i = j;
    }
    return r;
}

double pearson(const std::vector<double>& x, const std::vector<double>& y) {
    const std::size_t n = std::min(x.size(), y.size());
    if (n < 2) return kNaN;
    double mx = 0, my = 0;
    for (std::size_t i = 0; i < n; ++i) { mx += x[i]; my += y[i]; }
    mx /= static_cast<double>(n);
    my /= static_cast<double>(n);
    double sxy = 0, sxx = 0, syy = 0;
    for (std::size_t i = 0; i < n; ++i) {
        const double dx = x[i] - mx, dy = y[i] - my;
        sxy += dx * dy; sxx += dx * dx; syy += dy * dy;
    }
    if (!(sxx > 0) || !(syy > 0)) return kNaN;
    return sxy / std::sqrt(sxx * syy);
}

std::vector<double> quantile_means(const std::vector<double>& x, const std::vector<double>& y, int quantiles) {
    const auto r = average_ranks(x);
    const double n = static_cast<double>(x.size());
    std::vector<double> sum(quantiles, 0.0);
    std::vector<std::size_t> count(quantiles, 0);
    for (std::size_t i = 0; i < x.size(); ++i) {
        int q = static_cast<int>(std::ceil(r[i] / n * quantiles));
        q = std::clamp(q, 1, quantiles);
        sum[q - 1] += y[i];
        ++count[q - 1];
    }
    std::vector<double> out(quantiles, kNaN);
    for (int q = 0; q < quantiles; ++q)
        if (count[q]) out[q] = sum[q] / static_cast<double>(count[q]);
    return out;
}

double newey_west_t(const std::vector<double>& x, int lag) {
    const std::size_t n = x.size();
    if (n < 2) return kNaN;
    const double m = mean_of(x);
    auto gamma = [&](std::size_t l) {
        double s = 0;
        for (std::size_t t = l; t < n; ++t) s += (x[t] - m) * (x[t - l] - m);
        return s / static_cast<double>(n);
    };
    double var = gamma(0);
    const std::size_t L = std::min<std::size_t>(static_cast<std::size_t>(std::max(lag, 0)), n - 1);
    for (std::size_t l = 1; l <= L; ++l)
        var += 2.0 * (1.0 - static_cast<double>(l) / static_cast<double>(lag + 1)) * gamma(l);
    if (!(var > 0)) return kNaN;
    return m / std::sqrt(var / static_cast<double>(n));
}

double sorted_quantile(const std::vector<double>& sorted, double q) {
    if (sorted.empty()) return kNaN;
    const double pos = q * static_cast<double>(sorted.size() - 1);
    const std::size_t lo = static_cast<std::size_t>(std::floor(pos));
    const std::size_t hi = std::min(lo + 1, sorted.size() - 1);
    return sorted[lo] + (pos - static_cast<double>(lo)) * (sorted[hi] - sorted[lo]);
}

SeriesSummary summarize(const std::vector<double>& values, const std::vector<std::size_t>& days, int hac_lag) {
    SeriesSummary s;
    std::vector<double> x;
    for (auto d : days) {
        if (!std::isfinite(values[d])) continue;
        if (x.empty()) s.first = d;
        s.last = d;
        x.push_back(values[d]);
    }
    s.n = x.size();
    if (s.n == 0) return s;
    s.mean = mean_of(x);
    double ss = 0, pos = 0;
    for (double v : x) { ss += (v - s.mean) * (v - s.mean); pos += v > 0; }
    s.positive = pos / static_cast<double>(s.n);
    s.std = s.n > 1 ? std::sqrt(ss / static_cast<double>(s.n - 1)) : kNaN;
    s.icir = s.std > 0 ? s.mean / s.std : kNaN;
    s.t = s.icir * std::sqrt(static_cast<double>(s.n));
    s.hac_t = newey_west_t(x, hac_lag);
    return s;
}

NullResult circular_shift_null(const std::vector<std::vector<float>>& sig_ranks,
                               const std::vector<std::vector<float>>& ret_ranks,
                               int n, int min_shift, uint64_t seed, int min_sample, unsigned threads) {
    NullResult result;
    const std::size_t D = std::min(sig_ranks.size(), ret_ranks.size());
    result.dates = D;
    if (D == 0 || static_cast<int64_t>(D) - min_shift < min_shift) {
        result.reason = "IS has " + std::to_string(D) + " dates; need at least 2*minShift";
        return result;
    }
    std::mt19937_64 rng(seed);
    std::uniform_int_distribution<int64_t> dist(min_shift, static_cast<int64_t>(D) - min_shift);
    std::vector<std::size_t> shifts(static_cast<std::size_t>(n) + 1, 0);  // [0] = actual
    for (int i = 1; i <= n; ++i) shifts[i] = static_cast<std::size_t>(dist(rng));

    auto statistic = [&](std::size_t k) {
        double sum = 0;
        std::size_t used = 0;
        std::vector<double> a, b;
        for (std::size_t j = 0; j < D; ++j) {
            const auto& s = sig_ranks[j];
            const auto& r = ret_ranks[(j + k) % D];
            a.clear(); b.clear();
            const std::size_t N = std::min(s.size(), r.size());
            for (std::size_t i = 0; i < N; ++i)
                if (std::isfinite(s[i]) && std::isfinite(r[i])) { a.push_back(s[i]); b.push_back(r[i]); }
            if (static_cast<int>(a.size()) < min_sample) continue;
            const double c = pearson(a, b);
            if (std::isfinite(c)) { sum += c; ++used; }
        }
        return used ? sum / static_cast<double>(used) : kNaN;
    };
    std::vector<double> values(shifts.size(), kNaN);
    std::atomic<std::size_t> next{0};
    const unsigned workers = std::max(1u, std::min<unsigned>(threads, static_cast<unsigned>(shifts.size())));
    std::vector<std::thread> pool;
    for (unsigned w = 0; w < workers; ++w)
        pool.emplace_back([&] {
            for (std::size_t i = next++; i < shifts.size(); i = next++) values[i] = statistic(shifts[i]);
        });
    for (auto& t : pool) t.join();

    result.actual = values[0];
    result.values.assign(values.begin() + 1, values.end());
    std::vector<double> finite;
    for (double v : result.values) if (std::isfinite(v)) finite.push_back(v);
    if (!std::isfinite(result.actual) || finite.empty()) {
        result.reason = "statistic undefined (too few common columns)";
        return result;
    }
    result.mean = mean_of(finite);
    double ss = 0;
    std::size_t exceed = 0;
    for (double v : finite) {
        ss += (v - result.mean) * (v - result.mean);
        exceed += std::fabs(v) >= std::fabs(result.actual);
    }
    result.std = finite.size() > 1 ? std::sqrt(ss / static_cast<double>(finite.size() - 1)) : kNaN;
    result.p = (static_cast<double>(exceed) + 1.0) / (static_cast<double>(finite.size()) + 1.0);
    result.ok = true;
    return result;
}

Options parse_options(const Json::Value& v) {
    if (!v.isObject()) throw std::invalid_argument("diagnostics output requires a 'diagnostics' object");
    reject_unknown(v, {"horizons", "isEnd", "quantiles", "null", "topk", "groups", "conditionOn",
                       "conditionBuckets", "autocorrLags", "distributionQuantiles"}, "diagnostics");
    Options o;
    if (!v.isMember("horizons")) throw std::invalid_argument("diagnostics.horizons is required");
    o.horizons = parse_int_list(v["horizons"], "horizons", 1, 252, true);
    if (!v.isMember("isEnd") || !v["isEnd"].isString() || v["isEnd"].asString().empty())
        throw std::invalid_argument("diagnostics.isEnd is required (YYYY-MM-DD)");
    o.is_end = v["isEnd"].asString();
    if (v.isMember("quantiles")) o.quantiles = parse_int(v["quantiles"], "quantiles", 2, 100);
    if (v.isMember("null")) {
        const auto& n = v["null"];
        if (!n.isObject()) throw std::invalid_argument("diagnostics.null must be an object");
        reject_unknown(n, {"n", "minShift", "seed"}, "diagnostics.null");
        o.null_enabled = true;
        if (n.isMember("n")) o.null_n = parse_int(n["n"], "null.n", 1, 10000);
        if (n.isMember("minShift")) o.null_min_shift = parse_int(n["minShift"], "null.minShift", 1, 100000);
        if (n.isMember("seed")) {
            if (!n["seed"].isIntegral() || !(n["seed"].isUInt64()))
                throw std::invalid_argument("diagnostics.null.seed must be a non-negative integer");
            o.null_seed = n["seed"].asUInt64();
        }
    }
    if (v.isMember("topk")) o.topk = parse_int_list(v["topk"], "topk", 1, 1000000, false);
    if (v.isMember("groups")) {
        const auto& g = v["groups"];
        if (!g.isArray()) throw std::invalid_argument("diagnostics.groups must be an array");
        for (const auto& item : g) {
            const std::string name = item.isString() ? item.asString() : "";
            if (name != "sector" && name != "industry")
                throw std::invalid_argument("diagnostics.groups entries must be 'sector' or 'industry'");
            if (std::find(o.groups.begin(), o.groups.end(), name) != o.groups.end())
                throw std::invalid_argument("diagnostics.groups has a duplicate entry " + name);
            o.groups.push_back(name);
        }
    }
    if (v.isMember("conditionOn")) {
        if (!v["conditionOn"].isString() || v["conditionOn"].asString().empty())
            throw std::invalid_argument("diagnostics.conditionOn must be a nonempty <signal id> name");
        o.condition_on = v["conditionOn"].asString();
    }
    if (v.isMember("conditionBuckets")) o.condition_buckets = parse_int(v["conditionBuckets"], "conditionBuckets", 2, 100);
    if (v.isMember("autocorrLags")) o.autocorr_lags = parse_int_list(v["autocorrLags"], "autocorrLags", 1, 252, false);
    if (v.isMember("distributionQuantiles")) {
        const auto& q = v["distributionQuantiles"];
        if (!q.isArray()) throw std::invalid_argument("diagnostics.distributionQuantiles must be an array");
        for (const auto& item : q) {
            if (!item.isNumeric() || item.asDouble() < 0.0 || item.asDouble() > 1.0)
                throw std::invalid_argument("diagnostics.distributionQuantiles entries must be numbers in [0, 1]");
            o.distribution_quantiles.push_back(item.asDouble());
        }
    }
    return o;
}

Accumulator::Accumulator(const Options& options, const Layout& layout) : options_(options), layout_(layout) {
    eval_h_ = options_.horizons;
    const bool need_h1 = options_.null_enabled || !options_.groups.empty() || !options_.condition_on.empty();
    if (need_h1 && std::find(eval_h_.begin(), eval_h_.end(), 1) == eval_h_.end()) eval_h_.push_back(1);
    int span = *std::max_element(eval_h_.begin(), eval_h_.end());
    for (int l : options_.autocorr_lags) span = std::max(span, l);
    ring_ = static_cast<std::size_t>(span) + 1;
    D_ = layout_.end > layout_.first ? layout_.end - layout_.first : 0;
    const std::size_t N = layout_.N;
    sig_.assign(ring_ * N, std::numeric_limits<float>::quiet_NaN());
    logcum_.assign(ring_ * N, 0.0);
    badcum_.assign(ring_ * N, 0);
    zerocum_.assign(ring_ * N, 0);
    cond_.assign(2 * N, std::numeric_limits<float>::quiet_NaN());
    codes_.assign(2 * options_.groups.size() * N, std::numeric_limits<float>::quiet_NaN());
    daily_.resize(eval_h_.size());
    for (auto& d : daily_) {
        d.ic.assign(D_, kNaN); d.rankic.assign(D_, kNaN); d.spread.assign(D_, kNaN); d.n.assign(D_, 0);
        d.quantile_means.assign(options_.quantiles, std::vector<double>(D_, kNaN));
    }
    coverage_.assign(D_, kNaN);
    topk_.assign(options_.topk.size(), std::vector<double>(D_, kNaN));
    groups_.resize(options_.groups.size());
    if (!options_.condition_on.empty()) {
        cond_rankic_.assign(options_.condition_buckets, std::vector<double>(D_, kNaN));
        cond_spread_.assign(options_.condition_buckets, std::vector<double>(D_, kNaN));
    }
    dist_sum_.assign(options_.distribution_quantiles.size(), 0.0);
    ac_sum_.assign(options_.autocorr_lags.size(), 0.0);
    ac_n_.assign(options_.autocorr_lags.size(), 0);
}

void Accumulator::observe(unsigned di, const float* signal, const uint8_t* member, const double* returns,
                          const std::vector<const float*>& group_codes, const float* condition) {
    if (di < layout_.first || di >= layout_.end) return;
    const std::size_t N = layout_.N;
    const std::size_t s = slot(di) * N, p = slot(di - (di > layout_.first ? 1 : 0)) * N;
    std::size_t members = 0, targets = 0;
    std::vector<double> absval, values;
    for (std::size_t i = 0; i < N; ++i) {
        const bool m = member == nullptr || member[i];
        members += m;
        const bool target = m && std::isfinite(signal[i]);
        sig_[s + i] = target ? signal[i] : std::numeric_limits<float>::quiet_NaN();
        if (target) {
            ++targets;
            absval.push_back(std::fabs(static_cast<double>(signal[i])));
            values.push_back(signal[i]);
        }
        if (di == layout_.first) {
            logcum_[s + i] = 0; badcum_[s + i] = 0; zerocum_[s + i] = 0;
        } else {
            const double r = returns[i];
            logcum_[s + i] = logcum_[p + i];
            badcum_[s + i] = badcum_[p + i];
            zerocum_[s + i] = zerocum_[p + i];
            if (!std::isfinite(r)) ++badcum_[s + i];
            else if (1.0 + r <= 0.0) ++zerocum_[s + i];
            else logcum_[s + i] += std::log1p(r);
        }
    }
    const std::size_t two = (di % 2) * N;
    if (condition) std::copy(condition, condition + N, cond_.begin() + two);
    for (std::size_t g = 0; g < group_codes.size(); ++g)
        std::copy(group_codes[g], group_codes[g] + N, codes_.begin() + (di % 2) * group_codes.size() * N + g * N);

    const std::size_t d = di - layout_.first;
    coverage_[d] = members ? static_cast<double>(targets) / static_cast<double>(members) : kNaN;
    double total = 0;
    for (double a : absval) total += a;
    if (total > 0) {
        std::sort(absval.begin(), absval.end(), std::greater<double>());
        for (std::size_t k = 0; k < options_.topk.size(); ++k) {
            const std::size_t take = std::min<std::size_t>(options_.topk[k], absval.size());
            double sum = 0;
            for (std::size_t i = 0; i < take; ++i) sum += absval[i];
            topk_[k][d] = sum / total;
        }
    }
    if (di <= layout_.is_end) {
        if (!values.empty() && !dist_sum_.empty()) {
            std::sort(values.begin(), values.end());
            for (std::size_t q = 0; q < dist_sum_.size(); ++q)
                dist_sum_[q] += sorted_quantile(values, options_.distribution_quantiles[q]);
            ++dist_days_;
        }
        if (members) { missing_sum_ += 1.0 - coverage_[d]; ++missing_days_; }
        for (std::size_t l = 0; l < options_.autocorr_lags.size(); ++l) {
            const unsigned L = static_cast<unsigned>(options_.autocorr_lags[l]);
            if (di < layout_.first + L) continue;
            const std::size_t q = slot(di - L) * N;
            std::vector<double> a, b;
            for (std::size_t i = 0; i < N; ++i)
                if (std::isfinite(sig_[s + i]) && std::isfinite(sig_[q + i])) { a.push_back(sig_[s + i]); b.push_back(sig_[q + i]); }
            if (static_cast<int>(a.size()) < options_.min_sample) continue;
            const double c = rank_ic(a, b);
            if (std::isfinite(c)) { ac_sum_[l] += c; ++ac_n_[l]; }
        }
    }
    for (std::size_t hi = 0; hi < eval_h_.size(); ++hi) {
        const unsigned h = static_cast<unsigned>(eval_h_[hi]);
        if (di >= layout_.first + h) evaluate(di - h, di, static_cast<int>(h), hi);
    }
}

void Accumulator::evaluate(unsigned t, unsigned u, int h, std::size_t hi) {
    const std::size_t N = layout_.N, st = slot(t) * N, su = slot(u) * N;
    std::vector<unsigned> cols;
    std::vector<double> s, f;
    for (std::size_t i = 0; i < N; ++i) {
        if (!std::isfinite(sig_[st + i]) || badcum_[su + i] != badcum_[st + i]) continue;
        const double fr = zerocum_[su + i] != zerocum_[st + i] ? -1.0 : std::expm1(logcum_[su + i] - logcum_[st + i]);
        if (!std::isfinite(fr)) continue;
        cols.push_back(static_cast<unsigned>(i));
        s.push_back(sig_[st + i]);
        f.push_back(fr);
    }
    const std::size_t d = t - layout_.first;
    auto& day = daily_[hi];
    day.n[d] = static_cast<int>(s.size());
    if (static_cast<int>(s.size()) >= options_.min_sample) {
        day.ic[d] = pearson(s, f);
        day.rankic[d] = rank_ic(s, f);
        const auto qm = quantile_means(s, f, options_.quantiles);
        for (int q = 0; q < options_.quantiles; ++q) day.quantile_means[q][d] = qm[q];
        day.spread[d] = qm.back() - qm.front();
    }
    if (h == 1) evaluate_h1_extras(t, cols, s, f);
}

void Accumulator::evaluate_h1_extras(unsigned t, const std::vector<unsigned>& cols, const std::vector<double>& s,
                                     const std::vector<double>& f) {
    const std::size_t N = layout_.N, d = t - layout_.first;
    const std::size_t G = options_.groups.size();
    for (std::size_t g = 0; g < G; ++g) {
        const float* codes = codes_.data() + (t % 2) * G * N + g * N;
        std::map<float, std::vector<std::size_t>> members;
        for (std::size_t k = 0; k < cols.size(); ++k) {
            const float c = codes[cols[k]];
            if (std::isfinite(c) && c >= 0.0f) members[c].push_back(k);
        }
        for (const auto& [code, idx] : members) {
            auto it = groups_[g].find(code);
            if (it == groups_[g].end())
                it = groups_[g].emplace(code, GroupDaily{std::vector<double>(D_, kNaN), std::vector<double>(D_, kNaN)}).first;
            it->second.size[d] = static_cast<double>(idx.size());
            if (static_cast<int>(idx.size()) < options_.min_sample) continue;
            std::vector<double> a, b;
            for (auto k : idx) { a.push_back(s[k]); b.push_back(f[k]); }
            it->second.rankic[d] = rank_ic(a, b);
        }
    }
    if (!options_.condition_on.empty()) {
        const float* cond = cond_.data() + (t % 2) * N;
        std::vector<std::size_t> idx;
        std::vector<double> c;
        for (std::size_t k = 0; k < cols.size(); ++k)
            if (std::isfinite(cond[cols[k]])) { idx.push_back(k); c.push_back(cond[cols[k]]); }
        if (!idx.empty()) {
            const auto r = average_ranks(c);
            const int B = options_.condition_buckets;
            std::vector<std::vector<double>> bs(B), bf(B);
            for (std::size_t j = 0; j < idx.size(); ++j) {
                const int b = std::clamp(static_cast<int>(std::ceil(r[j] / static_cast<double>(idx.size()) * B)), 1, B) - 1;
                bs[b].push_back(s[idx[j]]);
                bf[b].push_back(f[idx[j]]);
            }
            for (int b = 0; b < B; ++b) {
                if (static_cast<int>(bs[b].size()) < options_.min_sample) continue;
                cond_rankic_[b][d] = rank_ic(bs[b], bf[b]);
                const auto qm = quantile_means(bs[b], bf[b], options_.quantiles);
                cond_spread_[b][d] = qm.back() - qm.front();
            }
        }
    }
    if (options_.null_enabled && t + 1 <= layout_.is_end) {
        // Independent percentile ranks: signal over all targets of t, returns over the h=1 sample.
        std::vector<float> sig_row(N, std::numeric_limits<float>::quiet_NaN());
        std::vector<float> ret_row(N, std::numeric_limits<float>::quiet_NaN());
        const std::size_t st = slot(t) * N;
        std::vector<double> all;
        std::vector<std::size_t> at;
        for (std::size_t i = 0; i < N; ++i)
            if (std::isfinite(sig_[st + i])) { all.push_back(sig_[st + i]); at.push_back(i); }
        const auto rs = average_ranks(all);
        for (std::size_t j = 0; j < at.size(); ++j) sig_row[at[j]] = static_cast<float>(rs[j] / static_cast<double>(at.size()));
        const auto rf = average_ranks(f);
        for (std::size_t k = 0; k < cols.size(); ++k) ret_row[cols[k]] = static_cast<float>(rf[k] / static_cast<double>(cols.size()));
        null_sig_.push_back(std::move(sig_row));
        null_ret_.push_back(std::move(ret_row));
    }
}

Json::Value Accumulator::finish(const std::vector<int64_t>& timestamps, unsigned threads) const {
    Json::Value out;
    const std::size_t first = layout_.first;
    out["timestamps"] = Json::Value(Json::arrayValue);
    for (std::size_t d = 0; d < D_; ++d) out["timestamps"].append(Json::Int64(timestamps[first + d]));
    out["isEndTimestamp"] = Json::Int64(timestamps[layout_.is_end]);
    out["minSample"] = options_.min_sample;

    auto is_days = [&](int h) {
        std::vector<std::size_t> days;
        for (std::size_t d = 0; d < D_; ++d)
            if (first + d + static_cast<std::size_t>(h) <= layout_.is_end) days.push_back(d);
        return days;
    };
    std::vector<std::size_t> os_days;
    for (std::size_t d = 0; d < D_; ++d)
        if (first + d >= layout_.os_start) os_days.push_back(d);

    out["horizons"] = Json::Value(Json::objectValue);
    for (std::size_t hi = 0; hi < options_.horizons.size(); ++hi) {
        const int h = options_.horizons[hi];
        const auto& day = daily_[hi];
        Json::Value j;
        j["ic"] = doubles(day.ic);
        j["rankic"] = doubles(day.rankic);
        j["n"] = Json::Value(Json::arrayValue);
        for (int n : day.n) j["n"].append(n);
        j["spread"] = doubles(day.spread);
        j["quantileMeans"] = Json::Value(Json::arrayValue);
        for (const auto& q : day.quantile_means) j["quantileMeans"].append(doubles(q));
        const auto isd = is_days(h);
        const int lag = std::max(5, h);
        j["is"]["ic"] = summary_json(summarize(day.ic, isd, lag), timestamps, first);
        j["is"]["rankic"] = summary_json(summarize(day.rankic, isd, lag), timestamps, first);
        j["is"]["spread"] = summary_json(summarize(day.spread, isd, lag), timestamps, first);
        j["is"]["hacLag"] = lag;
        j["os"]["ic"] = os_json(day.ic, os_days);
        j["os"]["rankic"] = os_json(day.rankic, os_days);
        j["os"]["spread"] = os_json(day.spread, os_days);
        out["horizons"][std::to_string(h)] = std::move(j);
    }

    std::vector<std::size_t> is_any;  // t <= isEnd, for per-date statistics without a horizon
    for (std::size_t d = 0; d < D_; ++d)
        if (first + d <= layout_.is_end) is_any.push_back(d);
    auto is_mean = [&](const std::vector<double>& v) {
        double sum = 0; std::size_t n = 0;
        for (auto d : is_any) if (std::isfinite(v[d])) { sum += v[d]; ++n; }
        return n ? sum / static_cast<double>(n) : kNaN;
    };
    out["coverage"] = doubles(coverage_);
    out["topk"] = Json::Value(Json::objectValue);
    out["isSummary"]["coverage"] = number(is_mean(coverage_));
    out["isSummary"]["days"] = Json::UInt64(is_any.size());
    out["isSummary"]["topk"] = Json::Value(Json::objectValue);
    for (std::size_t k = 0; k < options_.topk.size(); ++k) {
        out["topk"][std::to_string(options_.topk[k])] = doubles(topk_[k]);
        out["isSummary"]["topk"][std::to_string(options_.topk[k])] = number(is_mean(topk_[k]));
    }

    if (options_.null_enabled) {
        const auto r = circular_shift_null(null_sig_, null_ret_, options_.null_n, options_.null_min_shift,
                                           options_.null_seed, options_.min_sample, threads);
        Json::Value j;
        j["horizon"] = 1;
        j["n"] = options_.null_n;
        j["minShift"] = options_.null_min_shift;
        j["seed"] = Json::UInt64(options_.null_seed);
        j["dates"] = Json::UInt64(r.dates);
        if (r.ok) {
            j["actual"] = number(r.actual);
            j["nullMean"] = number(r.mean);
            j["nullStd"] = number(r.std);
            j["p"] = number(r.p);
        } else {
            j["error"] = r.reason;
        }
        out["null"] = std::move(j);
    }

    if (!options_.groups.empty()) {
        const auto isd = is_days(1);
        out["groups"] = Json::Value(Json::objectValue);
        for (std::size_t g = 0; g < options_.groups.size(); ++g) {
            Json::Value codes(Json::objectValue);
            for (const auto& [code, daily] : groups_[g]) {
                const auto s = summarize(daily.rankic, isd, 5);
                double size_sum = 0; std::size_t size_n = 0;
                for (auto d : isd) if (std::isfinite(daily.size[d])) { size_sum += daily.size[d]; ++size_n; }
                Json::Value j;
                j["n"] = Json::UInt64(s.n);
                j["mean"] = s.n ? number(s.mean) : Json::Value();
                j["hacT"] = s.n ? number(s.hac_t) : Json::Value();
                j["meanSize"] = size_n ? number(size_sum / static_cast<double>(size_n)) : Json::Value();
                j["sizeDays"] = Json::UInt64(size_n);
                std::string key = std::to_string(static_cast<long long>(code));
                if (static_cast<float>(static_cast<long long>(code)) != code) key = std::to_string(code);
                codes[key] = std::move(j);
            }
            out["groups"][options_.groups[g]]["rankicH1"] = std::move(codes);
        }
    }

    if (!options_.condition_on.empty()) {
        const auto isd = is_days(1);
        Json::Value j;
        j["on"] = options_.condition_on;
        j["buckets"] = options_.condition_buckets;
        j["rankic"] = Json::Value(Json::arrayValue);
        j["spread"] = Json::Value(Json::arrayValue);
        j["is"] = Json::Value(Json::arrayValue);
        for (int b = 0; b < options_.condition_buckets; ++b) {
            j["rankic"].append(doubles(cond_rankic_[b]));
            j["spread"].append(doubles(cond_spread_[b]));
            Json::Value s;
            s["rankic"] = summary_json(summarize(cond_rankic_[b], isd, 5), timestamps, first);
            s["spread"] = summary_json(summarize(cond_spread_[b], isd, 5), timestamps, first);
            j["is"].append(std::move(s));
        }
        out["condition"] = std::move(j);
    }

    if (!options_.distribution_quantiles.empty()) {
        Json::Value j;
        j["quantiles"] = doubles(options_.distribution_quantiles);
        std::vector<double> means(dist_sum_.size(), kNaN);
        for (std::size_t q = 0; q < means.size(); ++q)
            if (dist_days_) means[q] = dist_sum_[q] / static_cast<double>(dist_days_);
        j["isMean"] = doubles(means);
        j["days"] = Json::UInt64(dist_days_);
        j["missingRatioIsMean"] = missing_days_ ? number(missing_sum_ / static_cast<double>(missing_days_)) : Json::Value();
        out["distribution"] = std::move(j);
    }

    if (!options_.autocorr_lags.empty()) {
        out["autocorr"] = Json::Value(Json::objectValue);
        for (std::size_t l = 0; l < options_.autocorr_lags.size(); ++l) {
            Json::Value j;
            j["n"] = Json::UInt64(ac_n_[l]);
            j["isMean"] = ac_n_[l] ? number(ac_sum_[l] / static_cast<double>(ac_n_[l])) : Json::Value();
            out["autocorr"][std::to_string(options_.autocorr_lags[l])] = std::move(j);
        }
    }
    return out;
}

}  // namespace diagnostics
