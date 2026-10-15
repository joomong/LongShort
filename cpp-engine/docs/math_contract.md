# Math Contract (C++ Engine)

This document is the single source of truth for C++ engine test math.

## Scope

- Applies only to `cpp-engine`.
- `python-engine` is independent and not used as a correctness oracle here.

## Global Constants

- `bars_per_year = 365`
- `eps = 1e-12` (where used in denominator guards)

## Simulation Conventions

- Target gross exposure is 1: `weight[i] = signal[i] / sum(abs(signal))`
  on eligible names. A balanced long/short target has long 0.5 and absolute
  short 0.5. Returns use this total-book basis without a further factor of 2.
  No-signal bars can have zero exposure; carried corporate positions can differ
  from the target normalization. Gross exposure and gross (pre-fee) PnL are distinct.
- Return aggregation is additive (simple sum), not compounded.
- API `simStart`/`simEnd` select each run's interval without modifying the shared
  data panel. `simEnd` is inclusive by calendar day and maps to an exclusive
  `SimulatorConfig.sim_end_index`; omission uses the loaded panel end. Earlier
  bars still warm the modules. Execution, signals, metrics and diagnostics stop
  at that end, with no added terminal liquidation. A capped run must equal the
  same-start uncapped run through the selected end.
- An initially empty book has no holding return on its first trading bar. It pays
  entry fees and, with OHLC4/VWAP execution, can earn the fill-to-close trading return.
- Explicit XML delay owns signal timing; there is no Interval/execution mode.
  - The pipeline output at bar `t` is masked and normalized at `t`, then executed at `t`.
  - `<data expr="close" delay="1"/>` reads the prior bar exactly once.
  - Holding return at `t` uses previous executed weights and the close-to-close move.
- Execution-price contract (`backtest.execution_price`): `close` is the general
  configuration default; the stock examples explicitly select `ohlc4`.
  - `fill[t,i] = (open + high + low + close) / 4` for OHLC4, all in the same day's price units.
  - `fill[t,i] = vwap[t,i]` (raw same-day VWAP) for `vwap`. BRAIN Delay-1 PnL is
    reproduced by close holding PnL plus this VWAP trading leg.
  - VWAP requires exact `vwap/high/low/close`; missing cells, `high < low`, or
    close/vwap outside `[low, high]` are unavailable fills under the same
    carry/error policy as OHLC4; nonpositive/infinite values are refused for
    relevant positions. Close is never substituted.
  - `market_trade[t,i] = target_weight[t,i] - carried_weight[t,i]` on executable names.
  - `execution_pnl[t] = sum_i(market_trade[t,i] * (close[t,i] / fill[t,i] - 1))`.
  - `gross_ret[t] = holding_pnl[t] + execution_pnl[t]`; the execution term is zero under close execution.
  - Current pipeline targets execute without an additional shift; market trades
    can earn the fill-to-close return on that date. Explicit XML `delay` is the
    only signal delay.
  - This preserves the team's fixed-notional approximation. It does not convert
    target weights into an exact share/cash ledger or add price-drift rebalancing.
- Turnover is the sum of absolute **market** trades. Free corporate-action
  transfers are excluded; residual rebalance orders after a transfer are included.
- Carry freezes a position without an executable quote and reserves its budget.
  A finite close can still mark holding PnL when an OHL/VWAP cell is missing. OHLC4/VWAP
  never substitutes a close or forward-filled price. Error policy rejects a
  required trade without a fill. Inconsistent finite quotes (open, vwap or close outside
  low/high, or high < low) is an unavailable fill under the same carry/error policy. Nonpositive
  or nonfinite-non-NaN prices are refused for relevant positions. A missing OHL/VWAP field or as-of source
  cannot enable OHLC4/VWAP execution.
- Fee impact:
  - `ret[t] -= turnover[t] * fee_rate`

## Summary Metrics

- Input series use finite values only for return-based stats.
- `pnl = sum(ret_finite)`
- `ann_ret = mean(ret_finite) * bars_per_year`
- `sharpe = (mean / std_ddof1) * sqrt(bars_per_year)` when `n>=2` and `std>0`, else `0`
- `mdd` uses additive drawdown on cumulative additive PnL:
  - `cum[t] = sum_{k<=t}(ret[k])`
  - `peak_so_far[t] = max(0, cum[0], ..., cum[t])`, including the starting book before any returns
  - `mdd = min_t(cum[t] - peak_so_far[t])`
  - yearly drawdown uses the same zero starting PnL for each year; the chart reports `(equity - peak_equity) / initial_capital * 100`
- `turnover`: mean of finite values
- `avg_position_count = mean(position_count)` within the summary period, or `0` if empty. This is the average number of held symbols (UI `avgBet`, JSON `minBet`).
- `max_weight`: max of finite values
- `margin = mean(ret_finite) / (turnover + eps)`
- `long_ratio`:
  - per valid bar: `long_weight / (long_weight + short_weight)` if denominator > 0
  - summary: mean of valid bar ratios
- `short_ratio = 1 - long_ratio`
- `active_bar_count = count(position_count > 0)`
- `avg_max_bet = mean(max_weight finite)`
- `min_position_count = min(position_count)` within the summary period, or `0` if empty. This is the minimum number of held symbols (UI `minBet`, JSON `minBetMin`).
- Position-count summaries include zero-position bars. The simulator counts weights with `abs(weight) > 1e-12`.
- `quality = sharpe * sqrt(abs(1 - maxexp)) * (1 - max_weight)`
  - `maxexp = max(abs(long_weight - short_weight))` on valid bars
  - `short_weight` is an absolute short exposure; all weights here are fractions,
    not percentage points. Both maxima are taken over the corresponding IS/OS
    period. This is a platform score penalizing net exposure and single-symbol
    concentration, not the yearly fitness or GA objective (GA uses IS Sharpe).
- `sharpe80`, `sharpe400`:
  - sharpe on tail window of finite returns (`min(window, n)`), ddof=1
- `minsoc = min(long_weight + short_weight)` on valid bars

## Yearly IR

- `ir = mean(daily gross returns) / std_ddof1(daily gross returns)` within each
  calendar year and the requested trading interval, using finite returns only.
- IR is not annualized and does not subtract a benchmark. With fewer than two
  returns or zero sample standard deviation it is 0, matching Sharpe's degenerate
  cases. Sharpe retains its existing denominator guard (`std + eps`).
- The yearly table displays `sharpe[ir]`. Older API responses without `ir` show
  `[—]` instead of guessing the annualization factor.

## Yearly Fitness

- Uses the [referenced Fitness formula](https://github.com/Xiaoyu-hub/worldquant/blob/main/raw/articles/worldquant-alpha-mining-strategies.md)
  with the platform's leverage-1 return convention:
  `fitness = sharpe * sqrt(abs(ann_ret / 100) / max(daily_tvr, 0.125))`.
- `ann_ret` is the displayed annual gross return in percentage points; convert
  it to a fraction exactly once. `daily_tvr` is the mean daily turnover fraction
  for that calendar year. The denominator floor is 12.5% daily turnover.
- Both use the requested trading interval and exclude warmup. Negative Sharpe
  yields negative Fitness; zero Sharpe yields 0, including degenerate years.
- Return is based on the full gross book, not half the book: no factor of 2 is
  introduced. Thus this score uses the referenced formula but differs from its
  BRAIN half-book return convention. The $20M reporting book remains unchanged.
- This is a yearly reporting metric. GA continues to optimize IS Sharpe.

## IS/OS Split Contract

- `is_idx`: first index where `timestamp >= is_start_ms`, or `T` when no such bar exists
- `os_idx`: first index where `timestamp >= os_start_ms`
- Summary IS slice:
  - starts at `max(is_idx, sim_start_index)`
  - ends at `os_idx` (exclusive)
- Summary OS slice:
  - starts at `os_idx`
  - ends at `T` (exclusive)
- Timeseries output starts at `sim_start_index`, including bar 0 when requested.
  - timeseries index `k` corresponds to raw index `k+sim_start_index`
- Reported boundary indices:
  - IS/OS indices are clamped offsets from the first reported trading bar.

## Module Contracts

- Calling `init()` starts a fresh run: rolling sums, counts and input history must reset, including when the asset count changes. Checkpoint restoration happens after initialization.

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


## Meta portfolios

`<meta expr="NAME"/>` begins a chain from the member alphas in `meta_dir/NAME/**/*.xml`
(sorted by relative path). Its value is an alpha SET, not a signal. `<ew/>` turns a SET into
a signal: every member's raw row is normalized to gross 1 with the request's universe mask
and missing-close policy, the K rows are summed in member order, and the simulator
normalizes the sum again at execution. This is the same computation as the combined
batch of those members, so `<meta expr="x"/>` alone equals `/api/simulate` with
`xmls=[members]` and `combinedOnly`. Any op that needs a signal (pipeline end, `<signal id>`,
an ordinary module) while the value is a SET applies `<ew/>` implicitly. `<meta>` takes no
`delay`: members keep their own `<data delay>` and the engine adds no shift. Members are
re-run under the meta request's settings, must compile to exactly one alpha, may not contain
`<meta>`, and one failing member fails the request with the member path in the message.
`<data>` may not restart a pipeline that began with `<meta>`, and a chain that began with
`<meta>` must be named with `<signal id>` before another chain starts (an unnamed set would
run its members for nothing). Sets are materialized once per
request (also across GA candidates); only the combined T×N matrix is kept, and the member
workers of all materializations in one request share the request's `threads` budget (a set takes
its workers once when it starts, so a set that starts while another holds most tokens keeps the
smaller count until it finishes).

## GA search and streamed results

The objective remains gross IS Sharpe. The initial search evaluates up to `firstSearch`
(default 200) distinct expanded XMLs. If the complete search space fits, it is evaluated
once and the run finishes in round 1. Later rounds evaluate up to `roundSize` new XMLs
(default 50), including 20% uniform exploration and offspring from successful parents.
Previously scheduled XMLs never consume another evaluation slot. Parameter lists are
trimmed and deduplicated. Structural `<choice>` searches use the compiler expansion
contract and its existing 5,000-pipeline cap; ordinary parameter spaces stay factored.
`<shuffle>` and `<choice bind>` were removed (2026-10-15): they silently dropped or mispaired variants.

`stopRound` (default 40) includes the initial round. `stopAlphas` (default 400) limits
unique evaluation attempts, including failed candidates. Exhaustion, round limit or
candidate limit ends the run; budgets are reserved before parallel dispatch. `threads`
(one rule for simulate, save and GA requests: omitted means 20, otherwise 1..32, else 400;
each site uses at most its work count) limits candidate workers. Workers take the next available
candidate; controller-thread callbacks serialize events. XML keys, rather than hash
values alone, identify duplicates. Parent selection and equal-score ties use planned
candidate order, independent of completion order.

With `streamCandidates: true`, `/api/ga/stream` sends newline-delimited `search_start`,
`round_start`, `candidate`, `round_end`, and `search_complete` events. Rounds are one-based.
Candidate events contain stable indices, XML, round, origin, status, IS/OS summary and
yearly metrics; failures contain their reason. Full signal matrices and daily series
are loaded for a selected candidate rather than broadcast for every evaluation.
Legacy requests without this flag retain generation events and the best-alpha complete
payload. `popSize` and `generations` remain request aliases for the initial search and
round limit.

The web start button and Shift+Enter run GA; there is no separate ordinary/GA button choice.
The web exposes only Stats and round tabs. Stats contains all unique evaluated candidates;
each round tab appears on round start and fills as candidates complete. Stop preserves
received results. The source search XML is retained after completion. `metricsOnly: true`
on `/api/simulate` returns unchanged accounting/metrics without collecting or serializing
signals. `signalSeriesOnly: true` requires `signalPlotSymbols` and one concrete alpha;
it returns the exact selected signal columns without recomputing unused distributions.
These optional flags do not change the ordinary response contract.

GA tables keep a top `batch` row for two or more successful candidates: Stats uses all
successful unique candidates and each round uses its own candidates. The browser loads
the aggregate after the visible cohort finishes/stops, caches it by membership, and
passes concrete XMLs in stable candidate order using `/api/simulate` with `xmls`,
`combinedOnly: true`, and `metricsOnly: true`. Each `xmls` entry must compile to exactly
one alpha; the response has an empty `batches` array and the ordinary `combined` payload.
This computes signals and the existing normalized combined portfolio, rather than
averaging metrics or rerunning individual portfolio accounting. Bounded blocks reduce
accumulator wakeups while preserving the exact per-row floating-point addition order.
Selected daily details are fetched after their round finishes, avoiding duplicate
pipeline evaluation alongside the ongoing search. Reclicking the selected row is a
no-op, including when only the first candidate of a round has arrived.
Displayed IDs are zero-based positions in the sorted visible table; selection and saved
XMLs retain their original internal candidate indices. Multi-alpha results remain in
performance detail, with signal-analysis plots exposed only for a single-alpha result.

Large signal plots and distributions are serialized independently in parallel. The web
normalizes large heatmaps in up to two workers, partitioned by complete dates; each date
uses the same complete cross section and median/MAD computation as the synchronous path.
Raw signal values, tooltips, plot sampling, portfolio returns and accounting remain unchanged.
