# Math Contract (C++ Engine)

This document is the single source of truth for C++ engine test math.

## Scope

- Applies only to `cpp-engine`.
- `python-engine` is independent and not used as a correctness oracle here.

## Global Constants

- `bars_per_year = 365`
- `eps = 1e-12` (where used in denominator guards)

## Simulation Conventions

- Return aggregation is additive (simple sum), not compounded.
- First bar (`di=0`) has no price return term, only fee impact from turnover.
- D1 mode execution:
  - signal computed at bar `t`
  - execution weight uses previous target (`prev_target`)
  - realized return at bar `t` uses previous executed weights (`prev_weights`) and close-to-close move.
- Turnover formula:
  - `turnover[t] = sum_i(abs(exec_w[i] - prev_weights[i]))`
- Fee impact:
  - `ret[t] -= turnover[t] * fee_rate`

## Summary Metrics

- Input series use finite values only for return-based stats.
- `pnl = sum(ret_finite)`
- `ann_ret = mean(ret_finite) * bars_per_year`
- `sharpe = (mean / std_ddof1) * sqrt(bars_per_year)` when `n>=2` and `std>0`, else `0`
- `mdd` uses additive drawdown on cumulative additive PnL:
  - `cum[t] = sum_{k<=t}(ret[k])`
  - `mdd = min_t(cum[t] - peak_so_far[t])`
- `turnover`, `min_bet`: mean of finite values
- `max_weight`: max of finite values
- `margin = mean(ret_finite) / (turnover + eps)`
- `long_ratio`:
  - per valid bar: `long_weight / (long_weight + short_weight)` if denominator > 0
  - summary: mean of valid bar ratios
- `short_ratio = 1 - long_ratio`
- `nonzero_position = count(nonzero_count > 0)`
- `avg_max_bet = mean(max_weight finite)`
- `min_bet_min = min(min_bet finite)` or `0` if none
- `quality = sharpe * sqrt(abs(1 - maxexp)) * (1 - max_weight)`
  - `maxexp = max(abs(long_weight - short_weight))` on valid bars
- `sharpe80`, `sharpe400`:
  - sharpe on tail window of finite returns (`min(window, n)`), ddof=1
- `minsoc = min(long_weight + short_weight)` on valid bars

## IS/OS Split Contract

- `is_idx`: first index where `timestamp >= is_start_ms`
- `os_idx`: first index where `timestamp >= os_start_ms`
- Summary IS slice:
  - starts at `max(is_idx, 1)` (global bar 0 excluded)
  - ends at `os_idx` (exclusive)
- Summary OS slice:
  - starts at `os_idx`
  - ends at `T` (exclusive)
- Timeseries output excludes raw bar 0:
  - timeseries index `k` corresponds to raw index `k+1`
- Reported boundary indices:
  - `isStartIndex = max(is_idx,1) - 1`
  - `osStartIndex = (os_idx > 0) ? os_idx - 1 : 0`

## Module Contracts

- `MDelay(days)`:
  - output at `t` is input at `t-days`; unavailable lookback -> `NaN`
- `MTsMean(days)`:
  - warmup rule: `t+1 < days` -> `NaN`
  - after warmup: mean of finite values in rolling window
- `MTsZscore(days, ddof, min_count, eps)`:
  - rolling stats over finite window values
  - if valid count `< min_count`: output `0`
  - if current value is non-finite: `NaN`
  - if `std < eps`: `0`
- `MTsRank(days, pct, min_count)`:
  - ascending percentile rank of current value in rolling window finite values (higher value → higher rank)
  - if current is `NaN` or valid count `< min_count`: `NaN`
  - tie handling: average rank
- `MDecay(days)`:
  - linear weighted average with newest weight `days`, oldest weight `1`
  - ignore non-finite values in numerator and denominator
- `MRank(pct)`:
  - cross-sectional rank per bar on finite values, tie-aware
- `MZscore(ddof, min_count, eps)`:
  - cross-sectional z-score on finite values
  - if finite count `< min_count` or `scale < eps`: all `NaN`

## Regression Test Mapping

- Module oracle tests:
  - `tests/test_module_oracles.cpp`
- Summary/full-metrics oracle tests:
  - `tests/test_metrics_oracle.cpp`
- XML/pipeline/signal plot contract tests:
  - `tests/test_pipeline_contract.cpp`
