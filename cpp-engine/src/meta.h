#pragma once

#include "simulator_engine.h"
#include <algorithm>
#include <charconv>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <exception>
#include <filesystem>
#include <functional>
#include <list>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

struct CompiledAlpha;
struct MetaSet;
struct DataPanel;

// How a <meta> alpha SET is turned into one signal row per bar. Every method weights the members'
// gross-1 rows with w[t,k]; EW is w = 1/K. Estimation windows are member daily gross returns over
// the `days` bars that end at t-1 (never bar t), see platform docs/math_contract.md "Meta portfolios".
enum class CombineMethod { Ew, EwSign, Mvo, MinVar, Erc, Ivol, Spca, Glasso };
enum class ShrinkMode { Diag, LedoitWolf, ConstantCorrelation, Oas };

struct CombineSpec {
    CombineMethod method = CombineMethod::Ew;
    unsigned days = 0;                     // estimation window in bars (EwSign: the sign window)
    unsigned rebalance = 1;                // recompute the weights every N bars (>= 1)
    ShrinkMode shrink = ShrinkMode::Diag;  // Mvo / MinVar covariance shrinkage
    float delta = 0.5f;                    // Diag shrink intensity in [0, 1]; ignored by the automatic modes
    unsigned k = 0;                        // Spca: number of principal components (>= 1)
    float alpha = 0.0f;                    // Glasso: L1 penalty (> 0)

    bool is_ew() const { return method == CombineMethod::Ew; }

    // Canonical text, used for cache keys and the response payload. Fixed format, no locale.
    std::string key() const {
        std::string out = method_name(method);
        if (method == CombineMethod::Ew) return out;
        out += " days=" + std::to_string(days);
        if (method == CombineMethod::Mvo || method == CombineMethod::MinVar) {
            out += std::string(" shrink=") + shrink_name(shrink);
            if (shrink == ShrinkMode::Diag) out += " delta=" + shortest_fixed_decimal(delta);
        }
        if (method == CombineMethod::Spca) out += " k=" + std::to_string(k);
        if (method == CombineMethod::Glasso) out += " alpha=" + shortest_fixed_decimal(alpha);
        out += " rebalance=" + std::to_string(rebalance);
        return out;
    }

    static const char* method_name(CombineMethod m) {
        switch (m) {
            case CombineMethod::Ew: return "ew";
            case CombineMethod::EwSign: return "ewsign";
            case CombineMethod::Mvo: return "mvo";
            case CombineMethod::MinVar: return "minvar";
            case CombineMethod::Erc: return "erc";
            case CombineMethod::Ivol: return "ivol";
            case CombineMethod::Spca: return "spca";
            case CombineMethod::Glasso: return "glasso";
        }
        return "?";
    }
    static const char* shrink_name(ShrinkMode s) {
        switch (s) {
            case ShrinkMode::Diag: return "diag";
            case ShrinkMode::LedoitWolf: return "lw";
            case ShrinkMode::ConstantCorrelation: return "cc";
            case ShrinkMode::Oas: return "oas";
        }
        return "?";
    }

private:
    // Shortest float round-trip text in fixed notation, never scientific ("0.5", "0.1",
    // "0.0000001"); printed as float, not double (0.1f would be 0.10000000149011612).
    // -0 prints as 0 so equal values share one key.
    static std::string shortest_fixed_decimal(float value) {
        if (value == 0.0f) value = 0.0f;
        char buffer[512];
        const auto result = std::to_chars(buffer, buffer + sizeof buffer, value, std::chars_format::fixed);
        return std::string(buffer, result.ptr);
    }
};

// Member weights for combining a <meta> alpha SET. Estimation uses member gross
// daily returns over the `days` bars ending at t-1; rows before the first estimable bar are EW.
struct MemberReturns {
    std::vector<std::string> paths;                 // member order
    std::vector<std::vector<double>> rows;          // T x K gross daily returns; NaN before sim_start
                                                    // T = trading-period end (sim_end_index or panel end)
    unsigned sim_start = 0, T = 0, K = 0;
};

struct CombineWeights {
    std::vector<std::vector<double>> rows;          // T x K; rows before the first estimable bar are EW
    unsigned fallback_bars = 0;                     // bars that fell back to EW after t0
    unsigned first_estimated_bar = 0;               // t0, or T when no bar could be estimated
};

// Throws std::invalid_argument for an unusable spec ("<spca>: k=12 exceeds the member count 8",
// "<mvo>: days=3000 exceeds the trading bars 2690"); the caller prefixes "meta 'NAME' ".
CombineWeights compute_combine_weights(const MemberReturns& returns, const CombineSpec& spec);

// Every method except EwSign: a member with window sd <= 1e-12 makes the bar an EW fallback.

// Exposed for unit tests. `window` is days x K (row-major, finite). Both work on K x K row-major
// matrices. shrink_covariance returns the covariance used by Mvo/MinVar (Diag uses ddof 1;
// lw/oas/cc use the biased 1/n covariance of the centered window like sklearn / Ledoit-Wolf 2004).
std::vector<double> shrink_covariance(const std::vector<std::vector<double>>& window, const CombineSpec& spec);
// Normalized weights (sum |w| = 1) for one window; false when the estimator fails (EW fallback).
bool weights_for_window(const std::vector<std::vector<double>>& window, const CombineSpec& spec,
                        std::vector<double>& weights);

// Materialization of <meta> alpha sets: the EW combined row of every member, as in /api/simulate
// with combinedOnly. Only the combined T x N matrix is kept; member matrices are never stored.
using MetaMatrix = std::vector<std::vector<float>>;

struct MetaRunConfig {
    SimulatorConfig member_config;                    // request settings (dates, price, policy, mask)
    unsigned threads = 1;                             // bounded by the member count
    MissingClosePolicy policy = MissingClosePolicy::Carry;
    const std::vector<uint8_t>* universe_mask = nullptr;
};

// Member-worker tokens shared by every materialization of one request, so concurrent sets
// together never run more than the request's thread count.
class MetaWorkerBudget {
public:
    explicit MetaWorkerBudget(unsigned tokens) : free_(std::max(1u, tokens)) {}
    // Blocks until at least one token is free, then takes up to `wanted` (>= 1).
    unsigned acquire(unsigned wanted);
    void release(unsigned count);

private:
    std::mutex mutex_;
    std::condition_variable available_;
    unsigned free_;
};

// Runs every member's signal rows (no book) in parallel and sums their normalized rows in member
// order. A member failure rethrows as "meta 'NAME' member PATH: <what()>"; EvaluationCancelled passes
// through unchanged. Member pipelines are released after they run. With a budget, the worker
// count is what the budget grants (at least 1, at most min(threads, K)), held until return.
MetaMatrix materialize_meta_set(const DataPanel& data, MetaSet& set, const MetaRunConfig& config,
                                const std::function<bool()>& cancelled, MetaWorkerBudget* budget = nullptr);

// Weighted pass for several weight matrices at once: one run per member; member k's normalized row at
// bar t enters output c as float(row * weights[c]->rows[t][k]), summed in member order. Worker, budget,
// cancellation, error wrapping and member release are those of materialize_meta_set.
std::vector<MetaMatrix> materialize_weighted(const DataPanel& data, MetaSet& set, const MetaRunConfig& config,
                                             const std::vector<const CombineWeights*>& weights,
                                             const std::function<bool()>& cancelled,
                                             MetaWorkerBudget* budget = nullptr);

// MetaSetCache key: name, member paths, combine spec key.
std::string meta_set_key(const MetaSet& set, const CombineSpec& spec);

struct MetaCombineInfo {
    unsigned fallback_bars = 0;
    unsigned first_estimated_bar = 0;
};

// Per-request cache: one matrix per set key, computed once even when many threads ask together.
class MetaSetCache {
public:
    // `threads` bounds the member workers of all materializations of this cache together.
    explicit MetaSetCache(unsigned threads) : budget_(threads) {}
    MetaWorkerBudget& budget() { return budget_; }

    // compute runs at most once per name; concurrent callers wait; a failed compute rethrows to every
    // waiter and is not retried (the request is failing anyway).
    std::shared_ptr<const MetaMatrix> get(const std::string& name, const std::function<MetaMatrix()>& compute);

    // Prefetch support: store a computed matrix or its error (a no-op when the key is already done),
    // test for a key, drop keys (non-EW matrices of a finished GA round).
    void put(const std::string& key, std::shared_ptr<const MetaMatrix> matrix, std::exception_ptr error);
    bool contains(const std::string& key);
    void evict(const std::vector<std::string>& keys);

    // Weight diagnostics of non-EW keys, reported in the response.
    void set_info(const std::string& key, MetaCombineInfo info);
    bool info(const std::string& key, MetaCombineInfo* out);

    // Work done for this cache: weighted or EW passes, and member runs (returns and weighted passes).
    struct Stats {
        std::size_t materializations = 0;
        std::size_t member_runs = 0;
    };
    void record(std::size_t materializations, std::size_t member_runs);
    Stats stats();

private:
    struct Entry {
        std::once_flag once;
        std::shared_ptr<const MetaMatrix> matrix;
        std::exception_ptr error;
    };
    MetaWorkerBudget budget_;
    std::mutex mutex_;
    std::map<std::string, std::shared_ptr<Entry>> entries_;
    std::map<std::string, MetaCombineInfo> infos_;
    Stats stats_;
};

class MemberReturnsCache;

// Everything the non-EW passes need besides the set: `settings` is the request part of the member-return
// key (universe, sim start/end, execution price, missing-close policy).
struct MetaCombineContext {
    const DataPanel& data;
    MetaRunConfig config;
    std::string settings;
    MemberReturnsCache& returns;
    MetaSetCache& cache;
    std::function<bool()> cancelled;
};

// Lazy path for one non-EW set: (1) member returns through the returns cache, (2) the weights of
// set.combine, (3) one weighted pass. Records the weight info under meta_set_key(set, set.combine).
// A spec error throws std::invalid_argument("meta 'NAME' <tag>: ...").
MetaMatrix materialize_combined(const MetaCombineContext& context, MetaSet& set);

// Batch path: groups the non-EW sets of `alphas` by name and members, and for every group whose specs
// are not cached yet runs (1) once, (2) per distinct spec and (3) once for all of them, then stores each
// matrix (or its error) in the cache. Returns the keys stored. EW-only sets stay lazy.
std::vector<std::string> prefetch_meta_sets(const MetaCombineContext& context, std::vector<CompiledAlpha>& alphas);

// Runs every member with Simulator::run under config.member_config and stores its gross daily
// returns (fees excluded) as rows[t][k]; bars before sim_start_index or without a value are NaN.
// Members are not released (the weighted pass re-runs them). Worker, budget, cancellation and
// error wrapping ("meta 'NAME' member PATH: ...") are those of materialize_meta_set.
MemberReturns collect_member_returns(const DataPanel& data, const MetaSet& set, const MetaRunConfig& config,
                                     const std::function<bool()>& cancelled, MetaWorkerBudget* budget = nullptr);

// Process-wide member-return cache: LRU of 16 entries by last use, keyed by a caller-built string.
// One compute per key at a time; concurrent callers wait. A failed compute is not stored and only
// its own caller sees the error: waiters then compute with their own callback (a cancelled request
// must not fail another request waiting on the same key).
class MemberReturnsCache {
public:
    static constexpr std::size_t kCapacity = 16;
    std::shared_ptr<const MemberReturns> get(const std::string& key, const std::function<MemberReturns()>& compute);

private:
    struct Entry {
        bool done = false;
        std::shared_ptr<const MemberReturns> value;
    };
    std::mutex mutex_;
    std::condition_variable changed_;
    std::map<std::string, std::shared_ptr<Entry>> entries_;
    std::list<std::string> lru_;   // front = most recently used completed entry
};

// Meta member sets: <meta_dir>/<name>/**/*.xml, one alpha XML per file.
namespace meta_dir {

struct MetaMember {
    std::string path;  // relative to <meta_dir>/<name>
    std::string xml;   // file content verbatim
};

// Immediate subdirectories of meta_dir (dot entries skipped), sorted; {} if meta_dir is absent.
std::vector<std::string> names(const std::filesystem::path& meta_dir);

// All *.xml files under <meta_dir>/<name>, recursively, sorted by relative path. Dot entries are
// skipped; symlinks are followed but a directory is entered at most once. Throws std::runtime_error
// for an unknown/invalid name or a folder without .xml files.
std::vector<MetaMember> members(const std::filesystem::path& meta_dir, const std::string& name);

}  // namespace meta_dir

// Adds normalized signals in alpha order, independent of worker completion order.
class OrderedSignalAccumulator {
public:
    OrderedSignalAccumulator(const DataPanel& data, MissingClosePolicy policy,
                             const std::vector<uint8_t>* universe = nullptr);
    void add(std::size_t alpha_index, unsigned di, const float* row, unsigned n,
             std::vector<double>& normalized_scratch);
    void add_block(std::size_t alpha_index, unsigned first_di, std::vector<float>& rows,
                   unsigned n, std::vector<double>& scratch);
    void cancel();
    std::vector<std::vector<float>> take_signals();

private:
    const DataPanel& data_;
    MissingClosePolicy policy_;
    const std::vector<uint8_t>* universe_;
    std::vector<std::vector<float>> signals_;
    std::vector<std::size_t> next_alpha_;
    std::mutex mutex_;
    std::condition_variable ready_;
    bool cancelled_ = false;
};
