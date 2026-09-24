#pragma once
#include "../data/data_panel.h"

// MNeutralize: cross-sectional group demean (stateless).
// `column_name` names the panel field holding the group codes; `group` keeps the
// public XML name. A symbol joins a group only when its value is finite and its
// code is finite and >= 0, matching the team's `cs_group_neut_1d`: everything
// else is unclassified and leaves NaN. No scaling, no minimum group size.
struct MNeutralize : public Module {
    void init()
    {
        member_codes.reserve(numAssets);
        group_sum.reserve(numAssets);
        group_count.reserve(numAssets);
    }
    void calculate(unsigned di, SignalVector &output)
    {
        const auto* mask = panel && !panel->universe_mask.empty()
            ? panel->universe_mask.data() + static_cast<size_t>(di) * panel->N : nullptr;
        // Excluded symbols must not influence group means.
        if (mask) {
            for (unsigned ii = 0; ii < numAssets; ++ii)
                if (!mask[ii]) output[ii] = std::numeric_limits<float>::quiet_NaN();
        }

        if (panel == nullptr || !panel->has_column(column_name)) {
            output.fill_nan();
            return;
        }
        const float* codes = panel->row(column_name, di);

        member_codes.clear();
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            const float code = codes[ii];
            if (std::isfinite(output[ii]) && std::isfinite(code) && code >= 0.0f)
                member_codes.push_back(code);
            else
                output[ii] = std::numeric_limits<float>::quiet_NaN();
        }
        if (member_codes.empty()) {
            output.fill_nan();
            return;
        }

        // Integer codes compare exactly; K is small, so sorted-unique + binary
        // search keeps the group lookup allocation-free.
        std::sort(member_codes.begin(), member_codes.end());
        member_codes.erase(
            std::unique(member_codes.begin(), member_codes.end()), member_codes.end());

        group_sum.assign(member_codes.size(), 0.0);
        group_count.assign(member_codes.size(), 0);
        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (!std::isfinite(output[ii])) continue;
            const size_t g = static_cast<size_t>(
                std::lower_bound(member_codes.begin(), member_codes.end(), codes[ii]) -
                member_codes.begin());
            group_sum[g] += static_cast<double>(output[ii]);
            ++group_count[g];
        }

        for (unsigned ii = 0; ii < numAssets; ++ii) {
            if (!std::isfinite(output[ii])) continue;
            const size_t g = static_cast<size_t>(
                std::lower_bound(member_codes.begin(), member_codes.end(), codes[ii]) -
                member_codes.begin());
            const double mean = group_sum[g] / static_cast<double>(group_count[g]);
            output[ii] = static_cast<float>(static_cast<double>(output[ii]) - mean);
        }
    }

    std::vector<float> member_codes;   // sorted unique codes present on this date
    std::vector<double> group_sum;
    std::vector<int> group_count;
    std::string column_name;           // panel field holding the group codes
    std::string group;                 // public group name from the XML attribute
    const DataPanel* panel = nullptr;  // required; the factory rejects a null panel
};
