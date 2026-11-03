#include "pool_correlation.h"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <fstream>
#include <limits>
#include <sstream>
#include <thread>

namespace fs = std::filesystem;

namespace alpha_pool {

std::vector<PoolAlpha> list(const fs::path& dir) {
    std::vector<PoolAlpha> out;
    std::error_code error;
    if (dir.empty() || !fs::is_directory(dir, error)) return out;
    for (const auto& entry : fs::directory_iterator(dir, error)) {
        std::error_code probe;   // per-entry probes must not stop the listing
        if (!entry.is_directory(probe)) continue;
        const fs::path xml_path = entry.path() / "alpha.xml";
        if (!fs::is_regular_file(xml_path, probe)) continue;
        std::ifstream in(xml_path);
        if (!in) continue;
        std::stringstream buffer;
        buffer << in.rdbuf();
        if (buffer.str().empty()) continue;
        out.push_back({entry.path().filename().string(), buffer.str()});
    }
    std::sort(out.begin(), out.end(), [](const PoolAlpha& a, const PoolAlpha& b) { return a.name < b.name; });
    return out;
}

std::size_t first_active_row(const std::vector<double>& ret) {
    for (std::size_t t = 0; t < ret.size(); ++t)
        if (std::isfinite(ret[t]) && ret[t] != 0.0) return t;
    return ret.size();
}

double correlation(const std::vector<double>& a, std::size_t a_first, const std::vector<double>& b,
                   std::size_t b_first, std::size_t begin, std::size_t end, std::size_t min_days,
                   std::size_t* days) {
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const std::size_t start = std::max({begin, a_first, b_first});
    const std::size_t stop = std::min({end, a.size(), b.size()});
    std::size_t n = 0;
    double sa = 0.0, sb = 0.0;
    for (std::size_t t = start; t < stop; ++t) {
        if (!std::isfinite(a[t]) || !std::isfinite(b[t])) continue;
        sa += a[t]; sb += b[t]; ++n;
    }
    if (days) *days = n;
    if (n < std::max<std::size_t>(min_days, 2)) return nan;
    const double ma = sa / static_cast<double>(n), mb = sb / static_cast<double>(n);
    double saa = 0.0, sbb = 0.0, sab = 0.0;
    for (std::size_t t = start; t < stop; ++t) {
        if (!std::isfinite(a[t]) || !std::isfinite(b[t])) continue;
        const double da = a[t] - ma, db = b[t] - mb;
        saa += da * da; sbb += db * db; sab += da * db;
    }
    if (!(saa > 0.0) || !(sbb > 0.0)) return nan;
    return sab / std::sqrt(saa * sbb);
}

Series equal_weight(const std::vector<Series>& alphas) {
    Series ew;
    ew.name = "ew";
    std::size_t rows = 0;
    for (const Series& s : alphas) rows = std::max(rows, s.ret.size());
    ew.ret.assign(rows, 0.0);
    for (std::size_t t = 0; t < rows; ++t) {
        double sum = 0.0;
        unsigned active = 0;
        for (const Series& s : alphas) {
            if (t < s.first_active || t >= s.ret.size() || !std::isfinite(s.ret[t])) continue;
            sum += s.ret[t];
            ++active;
        }
        ew.ret[t] = active ? sum / active : 0.0;
    }
    ew.first_active = rows;
    for (const Series& s : alphas) ew.first_active = std::min(ew.first_active, s.first_active);
    return ew;
}

std::shared_ptr<const Cache::Snapshot> Cache::snapshot() const {
    std::lock_guard<std::mutex> lock(swap_mutex_);
    return snapshot_;
}

std::size_t Cache::size() const { return snapshot()->alphas.size(); }

std::vector<std::string> Cache::names() const {
    std::vector<std::string> out;
    for (const Series& s : snapshot()->alphas) out.push_back(s.name);
    return out;
}

std::size_t Cache::refresh(const std::function<std::vector<double>(const std::string& xml)>& run, unsigned threads,
                           std::vector<std::string>* failures) {
    std::lock_guard<std::mutex> refresh_lock(refresh_mutex_);
    const auto listed = list(dir_);
    const auto current = snapshot();
    auto next = std::make_shared<Snapshot>();
    std::vector<std::size_t> todo;   // indices into `listed` that need a run
    next->alphas.reserve(listed.size());
    for (std::size_t i = 0; i < listed.size(); ++i) {
        const PoolAlpha& p = listed[i];
        const auto kept = std::find_if(current->alphas.begin(), current->alphas.end(),
                                       [&](const Series& s) { return s.name == p.name && s.xml == p.xml; });
        if (kept != current->alphas.end()) { next->alphas.push_back(*kept); continue; }
        const auto failed = std::find_if(current->failed.begin(), current->failed.end(),
                                         [&](const auto& f) { return f.first == p.name && f.second == p.xml; });
        if (failed != current->failed.end()) { next->failed.push_back(*failed); continue; }
        todo.push_back(i);
    }
    if (todo.empty() && next->alphas.size() == current->alphas.size() && next->failed.size() == current->failed.size())
        return 0;   // unchanged listing: keep the current snapshot (and its EW) as is

    std::vector<Series> fresh(todo.size());
    std::vector<std::string> errors(todo.size());
    std::atomic<std::size_t> cursor{0};
    auto worker = [&] {
        for (;;) {
            const std::size_t k = cursor.fetch_add(1);
            if (k >= todo.size()) return;
            const PoolAlpha& p = listed[todo[k]];
            try {
                Series s;
                s.name = p.name;
                s.xml = p.xml;
                s.ret = run(p.xml);
                s.first_active = first_active_row(s.ret);
                fresh[k] = std::move(s);
            } catch (const std::exception& error) {
                errors[k] = error.what();
            } catch (...) {
                errors[k] = "unknown error";
            }
        }
    };
    const unsigned count = std::max(1u, std::min<unsigned>(threads, static_cast<unsigned>(todo.size())));
    std::vector<std::thread> pool;
    for (unsigned i = 1; i < count; ++i) pool.emplace_back(worker);
    worker();
    for (auto& t : pool) t.join();

    std::size_t added = 0;
    for (std::size_t k = 0; k < todo.size(); ++k) {
        const PoolAlpha& p = listed[todo[k]];
        if (!errors[k].empty()) {
            next->failed.emplace_back(p.name, p.xml);
            if (failures) failures->push_back(p.name + ": " + errors[k]);
            continue;
        }
        next->alphas.push_back(std::move(fresh[k]));
        ++added;
    }
    std::sort(next->alphas.begin(), next->alphas.end(), [](const Series& a, const Series& b) { return a.name < b.name; });
    next->ew = equal_weight(next->alphas);
    {
        std::lock_guard<std::mutex> lock(swap_mutex_);
        snapshot_ = std::move(next);
    }
    return added;
}

Json::Value Cache::correlation_json(const std::vector<double>& ret, std::size_t begin, std::size_t end) const {
    const auto snap = snapshot();
    if (snap->alphas.empty()) return Json::Value();
    const std::size_t run_first = first_active_row(ret);
    const auto number = [](double r) { return std::isfinite(r) ? Json::Value(r) : Json::Value(); };
    Json::Value out;
    std::size_t days = 0;
    out["ew"] = number(correlation(ret, run_first, snap->ew.ret, snap->ew.first_active, begin, end, kMinDays, &days));
    out["days"] = static_cast<Json::UInt64>(days);
    return out;
}

}  // namespace alpha_pool
