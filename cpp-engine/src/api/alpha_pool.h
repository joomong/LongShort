#pragma once

#include <cstddef>
#include <filesystem>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include <json/json.h>

// Alpha pool (2026-10-29): the approved alphas in <alpha_pool>/<name>/alpha.xml (a README.md sits beside
// each; the engine reads the XML only and never writes into the pool). Every pool alpha is simulated once
// with the pool's standard settings (the config's default universe and execution price, the whole panel)
// and its daily net return is kept, together with the equal-weight (EW) combination of the alphas that are
// already active on each bar. /api/simulate reports one number per run: the Pearson correlation of the
// daily net return with the EW pool over the sim window (rows where both sides are active).
namespace alpha_pool {

struct PoolAlpha {
    std::string name;
    std::string xml;
};

// <dir>/<name>/alpha.xml entries sorted by name. A missing or unreadable directory yields an empty list;
// entries without a readable alpha.xml are skipped.
std::vector<PoolAlpha> list(const std::filesystem::path& dir);

struct Series {
    std::string name;
    std::string xml;              // source text; a changed file is simulated again
    std::vector<double> ret;      // daily net return by panel row (zero before the first position)
    std::size_t first_active = 0; // first row with a nonzero return; ret.size() when never active
};

std::size_t first_active_row(const std::vector<double>& ret);

// Pearson correlation of a and b over rows [begin, end) that are >= both first-active rows and inside both
// vectors. *days receives the number of rows used; the result is NaN when days < min_days or either side
// has zero variance.
double correlation(const std::vector<double>& a, std::size_t a_first, const std::vector<double>& b,
                   std::size_t b_first, std::size_t begin, std::size_t end, std::size_t min_days,
                   std::size_t* days);

// Equal weight of the alphas active on each bar: ew[t] = mean of ret_i[t] over i with first_active <= t.
Series equal_weight(const std::vector<Series>& alphas);

class Cache {
  public:
    static constexpr std::size_t kMinDays = 60;   // fewer overlapping days reports null

    explicit Cache(std::filesystem::path dir) : dir_(std::move(dir)) {}
    const std::filesystem::path& dir() const { return dir_; }

    // Lists the pool and simulates, through `run` (xml -> daily net return by panel row), every alpha that is
    // new or whose XML changed, `threads` at a time; alphas that `run` rejects are reported in `failures`
    // ("name: message") and retried only when their XML changes. Alphas removed from the pool are dropped.
    // Returns the number of alphas simulated. Safe to call concurrently; readers keep seeing the previous
    // snapshot until the new one is complete.
    std::size_t refresh(const std::function<std::vector<double>(const std::string& xml)>& run, unsigned threads,
                        std::vector<std::string>* failures = nullptr);

    std::size_t size() const;
    std::vector<std::string> names() const;

    // {"ew": r|null, "days": n} for a run's daily net return by panel row (zeros before its sim start) over
    // rows [begin, end). Null when the pool is empty.
    Json::Value correlation_json(const std::vector<double>& ret, std::size_t begin, std::size_t end) const;

  private:
    struct Snapshot {
        std::vector<Series> alphas;                   // by name
        Series ew;
        std::vector<std::pair<std::string, std::string>> failed;  // name -> xml that failed (no retry until changed)
    };
    std::shared_ptr<const Snapshot> snapshot() const;

    std::filesystem::path dir_;
    mutable std::mutex swap_mutex_;      // guards snapshot_
    std::mutex refresh_mutex_;           // one refresh at a time
    std::shared_ptr<const Snapshot> snapshot_ = std::make_shared<const Snapshot>();
};

}  // namespace alpha_pool
