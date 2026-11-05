#pragma once

#include <cstddef>
#include <cstdint>
#include <iomanip>
#include <list>
#include <memory>
#include <mutex>
#include <random>
#include <sstream>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

#include <json/json.h>

#include "simulator_engine.h"

// What a row click needs from a completed GA candidate, kept instead of running the alpha again.
struct GaCandidateEntry {
    std::string xml;
    FullMetricsResult metrics;
    Json::Value missing_data;
    Json::Value meta;                // null without <meta> sets
    std::vector<double> net_returns; // by panel row; pool correlation is computed at read time

    std::size_t bytes() const {
        const auto& series = metrics.timeseries;
        const auto size = [](const auto& values) { return values.capacity() * sizeof(values[0]); };
        return sizeof(*this) + xml.capacity() + size(metrics.yearly) + size(series.timestamps) +
            size(series.cum_pnl) + size(series.cum_pnl_gross) + size(series.turnover_pct) +
            size(series.position_count) + size(series.max_weight_pct) + size(series.drawdown) +
            size(series.frozen_gross) + size(net_returns) + 2048 + 512 * meta.size();  // the Json members
    }
};

struct GaRunInfo {
    Json::Value context;
    std::size_t first_reported_bar = 0;
    std::size_t sim_end_index = 0;
    unsigned threads = 1;
};

// run id -> candidate id -> entry, process-wide. Over the byte cap whole runs go, oldest first: finished
// runs before any run still streaming. Candidates of an evicted run are not stored.
class GaRunStore {
  public:
    struct Found {
        std::shared_ptr<const GaRunInfo> run;
        std::shared_ptr<const GaCandidateEntry> entry;
    };

    explicit GaRunStore(std::size_t cap_bytes) : cap_bytes_(cap_bytes) {}

    std::string begin_run(GaRunInfo info) {
        std::random_device random;
        std::ostringstream id;
        id << std::hex << std::setfill('0');
        for (int i = 0; i < 4; ++i) id << std::setw(8) << static_cast<std::uint32_t>(random());
        std::list<Run> evicted;
        std::lock_guard<std::mutex> lock(mutex_);
        runs_.emplace_back();
        runs_.back().id = id.str();
        runs_.back().info = std::make_shared<const GaRunInfo>(std::move(info));
        bytes_ += kRunBytes;
        evict_over_cap(evicted);
        return id.str();
    }

    void put(const std::string& run_id, std::size_t candidate_id, GaCandidateEntry entry) {
        const std::size_t bytes = entry.bytes();
        auto stored = std::make_shared<const GaCandidateEntry>(std::move(entry));
        std::list<Run> evicted;  // freed after the lock is released
        std::lock_guard<std::mutex> lock(mutex_);
        Run* run = find_run(run_id);
        if (!run || !run->entries.emplace(candidate_id, std::move(stored)).second) return;
        run->bytes += bytes;
        bytes_ += bytes;
        evict_over_cap(evicted);
    }

    void finish_run(const std::string& run_id) {
        std::lock_guard<std::mutex> lock(mutex_);
        if (Run* run = find_run(run_id)) run->streaming = false;
    }

    // Empty run/entry when the run was evicted or the id is unknown.
    Found find(const std::string& run_id, std::size_t candidate_id) const {
        std::lock_guard<std::mutex> lock(mutex_);
        const Run* run = const_cast<GaRunStore*>(this)->find_run(run_id);
        if (!run) return {};
        const auto found = run->entries.find(candidate_id);
        if (found == run->entries.end()) return {};
        return {run->info, found->second};
    }

    std::size_t bytes() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return bytes_;
    }

  private:
    static constexpr std::size_t kRunBytes = 4096;  // keeps empty runs under the cap too

    struct Run {
        std::string id;
        std::shared_ptr<const GaRunInfo> info;
        std::unordered_map<std::size_t, std::shared_ptr<const GaCandidateEntry>> entries;
        std::size_t bytes = kRunBytes;
        bool streaming = true;
    };

    Run* find_run(const std::string& run_id) {
        for (auto& run : runs_) if (run.id == run_id) return &run;
        return nullptr;
    }

    void evict_over_cap(std::list<Run>& evicted) {
        while (bytes_ > cap_bytes_ && !runs_.empty()) {
            auto victim = runs_.begin();
            for (auto run = runs_.begin(); run != runs_.end(); ++run)
                if (!run->streaming) { victim = run; break; }
            bytes_ -= victim->bytes;
            evicted.splice(evicted.end(), runs_, victim);
        }
    }

    const std::size_t cap_bytes_;
    mutable std::mutex mutex_;
    std::list<Run> runs_;  // oldest first
    std::size_t bytes_ = 0;
};
