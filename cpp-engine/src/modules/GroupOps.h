#pragma once
#include "../data/data_panel.h"

// Group cross-sectional modules (stateless): group_rank, group_mean,
// group_zscore. Membership matches MNeutralize: universe-masked, finite value,
// finite code and code >= 0 on the signal date; everything else is NaN.
struct MGroupBase : public Module {
    void init()
    {
        member_codes.reserve(numAssets);
        gidx.assign(numAssets, -1);
        group_count.reserve(numAssets);
    }

    // Masks output, assigns gidx[ii] (-1 = non-member, output NaN) and returns
    // the number of groups K present today; group_count holds their sizes.
    size_t bucket(unsigned di, SignalVector &output)
    {
        const auto* selected = universe ? universe : panel ? &panel->universe_mask : nullptr;
        const auto* mask = selected && !selected->empty()
            ? selected->data() + static_cast<size_t>(di) * numAssets : nullptr;
        member_codes.clear();
        group_count.clear();
        if (panel == nullptr || !panel->has_column(column_name)) {
            output.fill_nan();
            std::fill(gidx.begin(), gidx.end(), -1);
            return 0;
        }
        const float* codes = panel->row(column_name, di);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            const float code = codes[ii];
            if ((!mask || mask[ii]) && std::isfinite(output[ii]) &&
                std::isfinite(code) && code >= 0.0f) {
                member_codes.push_back(code);
                gidx[ii] = 0;
            } else {
                output[ii] = std::numeric_limits<float>::quiet_NaN();
                gidx[ii] = -1;
            }
        }
        std::sort(member_codes.begin(), member_codes.end());
        member_codes.erase(
            std::unique(member_codes.begin(), member_codes.end()), member_codes.end());
        group_count.assign(member_codes.size(), 0);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (gidx[ii] < 0) continue;
            gidx[ii] = static_cast<int>(
                std::lower_bound(member_codes.begin(), member_codes.end(), codes[ii]) -
                member_codes.begin());
            ++group_count[gidx[ii]];
        }
        return member_codes.size();
    }

    std::vector<float> member_codes;   // sorted unique codes present on this date
    std::vector<int> gidx;             // group index per instrument, -1 = non-member
    std::vector<int> group_count;
    std::string column_name;           // panel field holding the group codes
    std::string group;                 // public group name from the XML attribute
    const DataPanel* panel = nullptr;  // required; the factory rejects a null panel
    const std::vector<uint8_t>* universe = nullptr; // run-selected mask (universe.names); null = panel default
};

// MGroupRank: rank within the group, MRank's tie rule (average rank, ties by
// value only); pct divides by the group size. One sort of (code, value, index)
// gives both the groups and their sizes, so bucket() is not used.
struct MGroupRank : public MGroupBase {
    void init()
    {
        MGroupBase::init();
        entries.reserve(numAssets);
    }
    void calculate(unsigned di, SignalVector &output)
    {
        const auto* selected = universe ? universe : panel ? &panel->universe_mask : nullptr;
        const auto* mask = selected && !selected->empty()
            ? selected->data() + static_cast<size_t>(di) * numAssets : nullptr;
        if (panel == nullptr || !panel->has_column(column_name)) {
            output.fill_nan();
            return;
        }
        const float* codes = panel->row(column_name, di);
        entries.clear();
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            const float code = codes[ii];
            if ((!mask || mask[ii]) && std::isfinite(output[ii]) &&
                std::isfinite(code) && code >= 0.0f)
                entries.push_back(Entry{code, output[ii], ii});
            output[ii] = std::numeric_limits<float>::quiet_NaN();
        }
        std::sort(entries.begin(), entries.end(), [](const Entry& l, const Entry& r) {
            if (l.code != r.code) return l.code < r.code;
            if (l.v != r.v) return l.v < r.v;
            return l.idx < r.idx;
        });
        const size_t n = entries.size();
        size_t group_begin = 0;
        while (group_begin < n) {
            size_t group_end = group_begin + 1;
            while (group_end < n && entries[group_end].code == entries[group_begin].code)
                ++group_end;
            const double group_size = static_cast<double>(group_end - group_begin);
            size_t begin = group_begin;
            while (begin < group_end) {
                size_t end = begin + 1;
                while (end < group_end && entries[end].v == entries[begin].v)
                    ++end;
                const double rank =
                    static_cast<double>(begin - group_begin) + 1.0 +
                    static_cast<double>(end - begin - 1) / 2.0;
                const float out = static_cast<float>(pct ? rank / group_size : rank);
                for (size_t i = begin; i < end; ++i) output[entries[i].idx] = out;
                begin = end;
            }
            group_begin = group_end;
        }
    }

    struct Entry { float code; float v; unsigned idx; };
    std::vector<Entry> entries;
    IntParameter pct{1};
};

// MGroupMean: weighted mean of the group's members, assigned to every member.
// Weights: equal (w = 1) or cap (w = panel field `cap` on the PREVIOUS bar;
// cap at di contains di's close: look-ahead). Members without a finite w > 0
// do not contribute but still receive the mean; a group with no valid weight
// (always on di = 0 for cap) leaves its members NaN.
// mean: arithmetic sum(w*x)/sum(w), or harmonic sum(w)/sum(w/x) where x == 0
// is not a member (NaN, like <inverse/>) and a zero or non-finite result is NaN.
struct MGroupMean : public MGroupBase {
    void init()
    {
        MGroupBase::init();
        group_sum.reserve(numAssets);
        group_wsum.reserve(numAssets);
    }
    void calculate(unsigned di, SignalVector &output)
    {
        const size_t k = bucket(di, output);
        if (k == 0) return;
        group_sum.assign(k, 0.0);
        group_wsum.assign(k, 0.0);
        if (harmonic)
            for (unsigned ii = 0; ii < numAssets; ++ii)
                if (gidx[ii] >= 0 && output[ii] == 0.0f) {
                    output[ii] = std::numeric_limits<float>::quiet_NaN();
                    gidx[ii] = -1;
                }
        const float* w = !cap_weight ? nullptr
            : (di > 0 && panel->has_column(weight_column)) ? panel->row(weight_column, di - 1)
            : nullptr;
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (gidx[ii] < 0) continue;
            double wi = 1.0;
            if (cap_weight) {
                if (!w || !std::isfinite(w[ii]) || !(w[ii] > 0.0f)) continue;
                wi = static_cast<double>(w[ii]);
            }
            const double x = static_cast<double>(output[ii]);
            group_sum[gidx[ii]] += harmonic ? wi / x : wi * x;
            group_wsum[gidx[ii]] += wi;
        }
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (gidx[ii] < 0) continue;
            const int g = gidx[ii];
            double m = std::numeric_limits<double>::quiet_NaN();
            if (group_wsum[g] > 0.0) {
                if (!harmonic) m = group_sum[g] / group_wsum[g];
                else if (group_sum[g] != 0.0) m = group_wsum[g] / group_sum[g];
            }
            output[ii] = std::isfinite(m) ? static_cast<float>(m)
                                          : std::numeric_limits<float>::quiet_NaN();
        }
    }

    std::vector<double> group_sum;
    std::vector<double> group_wsum;
    IntParameter cap_weight{0};        // XML weight: equal (0) | cap (1)
    IntParameter harmonic{0};          // XML mean: arithmetic (0) | harmonic (1)
    std::string weight_column = "cap"; // panel field holding the weights
};

// MGroupZscore: (x - m_g) / (s_g + 1e-12f), s_g sample sd; groups with fewer
// than 2 members or s_g < 1e-12 leave their members NaN.
struct MGroupZscore : public MGroupBase {
    void init()
    {
        MGroupBase::init();
        group_mean.reserve(numAssets);
        group_sd.reserve(numAssets);
    }
    void calculate(unsigned di, SignalVector &output)
    {
        const size_t k = bucket(di, output);
        if (k == 0) return;
        group_mean.assign(k, 0.0);
        group_sd.assign(k, 0.0);
        for (unsigned ii = 0; ii < numAssets; ++ii)
            if (gidx[ii] >= 0) group_mean[gidx[ii]] += static_cast<double>(output[ii]);
        for (size_t g = 0; g < k; ++g) group_mean[g] /= static_cast<double>(group_count[g]);
        for (unsigned ii = 0; ii < numAssets; ++ii)
            if (gidx[ii] >= 0) {
                const double diff = static_cast<double>(output[ii]) - group_mean[gidx[ii]];
                group_sd[gidx[ii]] += diff * diff;
            }
        for (size_t g = 0; g < k; ++g)
            group_sd[g] = group_count[g] < 2
                ? 0.0 : std::sqrt(group_sd[g] / static_cast<double>(group_count[g] - 1));
        const double eps = static_cast<double>(1e-12f);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (gidx[ii] < 0) continue;
            const int g = gidx[ii];
            if (group_count[g] < 2 || group_sd[g] < 1e-12)
                output[ii] = std::numeric_limits<float>::quiet_NaN();
            else
                output[ii] = static_cast<float>(
                    (static_cast<double>(output[ii]) - group_mean[g]) / (group_sd[g] + eps));
        }
    }

    std::vector<double> group_mean;
    std::vector<double> group_sd;
};
