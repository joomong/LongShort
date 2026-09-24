#pragma once
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

struct CostsConfig;
struct DataPanel;

// Borrow financing of the short book (docs/corporate_actions_engine_2026-09-17.md "costs").
// A bar charges sum over short bookkeeping weights |w| * rate / day_count * days, where
// the weights are the book carried into the bar and days are the calendar days since the
// previous bar (panel timestamps). Rates come from an interval table, else the default;
// a short with neither is not charged and counted as unrated. The charge leaves gross
// P&L untouched and is taken from net P&L only.
class BorrowCostModel {
public:
    struct RateInterval {
        unsigned first_bar;
        unsigned last_bar;   // inclusive
        double rate;
    };

    // Reads the configured table (parquet or .csv by extension; relative paths resolve
    // against config_dir) and maps its symbols and dates onto the panel. Overlapping
    // intervals for one symbol are refused.
    static BorrowCostModel load(const CostsConfig& config, const DataPanel& panel,
                                const std::string& config_dir);

    bool enabled() const { return enabled_; }
    // Enabled with at least one way to price a short in this panel: a symbol with a
    // rate interval inside the panel's dates, or a default rate.
    bool applied() const { return enabled_ && (symbols_rated_ > 0 || has_default_); }
    unsigned day_count() const { return day_count_; }
    double default_rate() const { return default_rate_; }
    bool has_default_rate() const { return has_default_; }
    std::size_t table_rows() const { return table_rows_; }
    std::size_t table_rows_matched() const { return table_rows_matched_; }
    std::size_t symbols_rated() const { return symbols_rated_; }
    const std::string& table_path() const { return table_path_; }

    // Annual rate for `asset` on bar `bar`; NaN when no interval covers it and there is no default.
    double rate(unsigned asset, unsigned bar) const;
    // Cost of carrying `weights` from the previous bar to bar `di`; adds the short
    // positions that had no rate to `unrated`.
    double charge(unsigned di, const std::vector<double>& weights, unsigned& unrated) const;

private:
    bool enabled_ = false;
    bool has_default_ = false;
    double default_rate_ = 0.0;
    unsigned day_count_ = 360;
    std::string table_path_;
    std::size_t table_rows_ = 0;
    std::size_t table_rows_matched_ = 0;
    std::size_t symbols_rated_ = 0;
    std::vector<int64_t> timestamps_;
    std::vector<std::vector<RateInterval>> intervals_; // per asset, sorted by first_bar
};
