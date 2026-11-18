# Math Contract (C++ Engine)

This document is the single source of truth for C++ engine test math.

## Scope

- Defines the engine calculations and the numeric contract consumed by `frontend/src/types.ts`.

## Global Constants

- `bars_per_year = 365`
- `eps = 1e-12` (where used in denominator guards); the `MZscore`/`MTsZscore` `eps` is a float `FloatParameter`,
  default `1e-12f` (= 9.99999996e-13)

## Simulation Conventions

- Target gross exposure is 1: `weight[i] = signal[i] / sum(abs(signal))`
  on eligible names. A balanced long/short target has long 0.5 and absolute
  short 0.5. Returns use this total-book basis without a further factor of 2.
  No-signal bars can have zero exposure; carried corporate positions can differ
  from the target normalization. Gross exposure and gross (pre-fee) PnL are distinct.
- Return aggregation is additive (simple sum), not compounded.
- API `simStart`/`simEnd` select each run's interval without modifying the shared
  data panel. `simEnd` is inclusive by calendar day and maps to an exclusive
  `SimulatorConfig.sim_end_index`; omission uses the loaded panel end. Computation
  starts at `history_start = max(simStart - 1, 0)`; earlier bars are never read by
  modules or the book. Bars before the first IS bar `max(simStart, isStart)` set intent and marks
  without trading; the first trade is on that bar. Execution, signals, metrics and diagnostics stop
  at that end, with no added terminal liquidation. A capped run must equal the
  same-start uncapped run through the selected end.
- A run is rejected with `Insufficient history` when the signal's first valid bar is later than
  `max(isStart bar, simStart bar) - 1` (not below 0) or is not before the run's end. So `delay="1"` or any
  lookback needs isStart later than simStart; isStart equal to simStart only admits lookback 0.
- After `history_start + lookback`, a later simStart matches an earlier one, except where state outlives
  the window: `<tsewma>` (bounded), `<hump>` and `<event>` (path dependent), and `<ffd>` for a name finite
  before `history_start`, `NaN` across it and restarting later (`NaN` for up to `L-1` bars after the
  restart; accepted, the desk math is kept). A non-EW `<meta>` is valid from the first grid bar
  `simStart + days + k·rebalance` whose window lies wholly on real member returns.
- `missingData.retirements` counts kept sources ending on reported trading bars only. A run computes only the
  columns of its run panel: symbols in the universe on some bar in `[history_start, simEnd]`, plus the
  successors they can move into. "Move to a retired destination" no longer fires for a dropped source.
- An initially empty book has no holding return on its first trading bar. It pays
  entry fees and, with OHLC4/VWAP execution, can earn the fill-to-close trading return.
- Explicit XML delay owns signal timing.
  - The loader prepares sector, industry and every named universe mask with a fixed one-bar lag: execution at bar `t` uses metadata from the previous trading bar `t-1`. The first loaded bar has missing group codes and no membership. This is independent of XML delay. Prices, returns and position-move events retain their original dates.
  - `<data expr="close" delay="1"/>` reads the prior bar exactly once.
  - Holding return at `t` uses previous executed weights and the cache's total return for `t`.
- Execution-price contract (`backtest.execution_price`): `close` is the general
  configuration default; the workspace example explicitly selects `vwap`.
  - `fill[t,i] = (open + high + low + close) / 4` for OHLC4, all in the same day's price units.
  - `fill[t,i] = vwap[t,i]` (raw same-day VWAP) for `vwap`. BRAIN Delay-1 PnL is
    reproduced by close holding PnL plus this VWAP trading leg.
  - VWAP requires exact `vwap/high/low/close`; missing cells, `high < low`, or
    close/vwap outside `[low, high]` are unavailable fills under the same
    carry/error policy as OHLC4; nonpositive values are refused for
    relevant positions (a ±inf cell already loaded as NaN, i.e. missing). Close is never substituted.
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
  prices are refused for relevant positions. A ±inf price cell in a data file loads as NaN and follows the
  missing-quote carry/error policy (only in-memory panels can still present an infinite price, which is refused). OHLC4/VWAP uses the cache's same-day price fields.
- Fee impact:
  - `ret[t] -= turnover[t] * fee_rate`

## Summary Metrics

- Input series use finite values only for return-based stats.
- `pnl = sum(ret_finite)`
- `annualReturn = mean(ret_finite) * bars_per_year`
- `sharpe = (mean / std_ddof1) * sqrt(bars_per_year)` when `n>=2` and `std>0`, else `0`
- `maxDrawdown` uses additive drawdown on cumulative additive PnL:
  - `cum[t] = sum_{k<=t}(ret[k])`
  - `peak_so_far[t] = max(0, cum[0], ..., cum[t])`, including the starting book before any returns
  - `maxDrawdown = min_t(cum[t] - peak_so_far[t])`
  - yearly drawdown uses the same zero starting PnL for each year; report drawdown is a gross cumulative-return fraction and the chart formats it as a percentage
- `turnover`: mean of finite values
- `avgPositionCount = mean(position_count)` within the summary period, or `0` if empty. This is the average number of held symbols.
- `maxWeight`: max of finite values
- `margin = mean(ret_finite) / (turnover + eps)`
- `longRatio`:
  - per valid bar: `long_weight / (long_weight + short_weight)` if denominator > 0
  - summary: mean of valid bar ratios
- `shortRatio = 1 - longRatio`
- `activeBarCount = count(position_count > 0)`
- `avgMaxWeight = mean(daily maximum absolute weight)`
- `minPositionCount = min(position_count)` within the summary period, or `0` if empty. This is the minimum number of held symbols.
- Position-count summaries include zero-position bars. The simulator counts weights with `abs(weight) > 1e-12`.
- `quality = sharpe * sqrt(abs(1 - maxexp)) * (1 - maxWeight)`
  - `maxexp = max(abs(long_weight - short_weight))` on valid bars
  - `short_weight` is an absolute short exposure; all weights here are fractions,
    not percentage points. Both maxima are taken over the corresponding IS/OS
    period. This is a platform score penalizing net exposure and single-symbol
    concentration, not the yearly fitness or GA objective (GA uses IS Sharpe).
- `sharpe80`, `sharpe400`:
  - sharpe on tail window of finite returns (`min(window, n)`), ddof=1
- `minGrossExposure = min(long_weight + short_weight)` on valid bars

## Yearly IR

- `informationRatio = mean(daily gross returns) / std_ddof1(daily gross returns)` within each
  calendar year and the requested trading interval, using finite returns only.
- IR is not annualized and does not subtract a benchmark. With fewer than two
  returns or zero sample standard deviation it is 0, matching Sharpe's degenerate
  cases. Sharpe retains its existing denominator guard (`std + eps`).
- The yearly table displays `sharpe[ir]`; an unavailable IR is displayed as `[—]`.

## Yearly Fitness

- Uses the [referenced Fitness formula](https://github.com/Xiaoyu-hub/worldquant/blob/main/raw/articles/worldquant-alpha-mining-strategies.md)
  with the platform's leverage-1 return convention:
  `fitness = sharpe * sqrt(abs(annualReturn) / max(turnover, 0.125))`.
- `annualReturn` and `turnover` are fractions. `turnover` is the mean daily turnover
  for that calendar year. The denominator floor is 12.5% daily turnover.
- Both use the requested trading interval. Negative Sharpe
  yields negative Fitness; zero Sharpe yields 0, including degenerate years.
- Return is based on the full gross book with no factor of 2. The reporting
  notional is `context.reportNotional = 20000000`, independent of `initial_capital`.
- This is a yearly reporting metric. GA continues to optimize IS Sharpe.

## IS/OS Split and Performance Reports

- Summary slices use the requested trading span. IS begins at
  `max(simStart, isStart)` and ends before `osStart`; OS uses its part of the requested
  trading interval. Yearly metrics begin at `max(simStart, isStart)` and stop at `simEnd`,
  so bars before isStart are warm-up and the first year row holds only the bars from that start.
- Everything reported starts at that same first reported bar, `max(simStart, isStart)`: the yearly
  table, performance series, signal plot and heatmap rows, `signalSeries`, `distribution`,
  `logDistribution`, `range` and their examples, `poolCorrelation`, `missingData` totals and saved
  `positions/` rows, in `/api/simulate`, batch, combined, saved results and GA. simStart only warms up:
  modules still run from `history_start`, but the top-level book is flat until that bar. Its first
  entry, turnover and fee land on that bar (the bar before it sets the intent), and there are no holdings,
  PnL or fees earlier, so IS summary and GA fitness include the entry. Combined batches follow the same
  rule. Members of non-EW meta sets alone trade from simStart, so estimation keeps its pre-isStart
  returns. Whole-panel alpha pool runs are unchanged. `context` keeps both dates. `isStart` with no bar
  on or before `simEnd` is rejected with HTTP 400. `meta[].fallbackBars` is an estimation diagnostic: it
  counts warm-up bars too and does not start at the first reported bar.
- The performance report begins at `max(simStart, isStart)` and stops at `simEnd`
  inclusive by calendar day. Its cumulative net/gross return is rebased at that
  report start; the gross high-water mark starts at zero before the first reported return.
- OS continues the IS cumulative curve and gross high-water mark; it is not rebased
  at `osStart`. The engine produces the report directly.
- `performance.osStartIndex` is the clamped OS boundary relative to the returned
  performance arrays. All performance arrays share the same epoch-ms timestamps.

## Result Contract and Units

`/api/simulate` returns `SimulationResponse {context, alphas, combined}`. `alphas` contains
`AlphaResult` entries and `combined` is an optional aggregate result. The shared TypeScript
interfaces are in [frontend/src/types.ts](../frontend/src/types.ts). An alpha result has
stable `id`, optional `xml`, `round` and `origin`, `status` (`completed` or `failed`), and
optional `error`, `summary: {is, os}`, `yearly`, `performance`, `signals`, `poolCorrelation`,
`missingData` and `meta`. The combined result has `id = -1`.

- `context` preserves `simStart`, `simEnd`, `isStart`, `osStart`, `universe`,
  `executionPrice`, `threads`, `feeRate` and `reportNotional` for that result.
- Returns, weights, turnover, drawdown, win rate and exposure ratios are fractions,
  never percentage points. For example `annualReturn: 0.2` means 20% and
  `maxDrawdown: -0.05` means -5%. Sharpe, information ratio, quality and fitness are
  dimensionless; position counts are numbers and margin is a return/turnover ratio.
- Data-series timestamps and histogram example timestamps are epoch milliseconds.
  Request/context dates are `YYYY-MM-DD` strings.
- `performance` contains aligned `timestamps`, `cumulativeNet`, `cumulativeGross`,
  `drawdown`, `turnover`, `maxWeight`, `positionCount` and `osStartIndex`.
  Dollar presentation multiplies cumulative return by `context.reportNotional` ($20M).
  That reporting notional is distinct from YAML `initial_capital`.
- `signals` contains optional `plot`, `range`, `distribution` and `logDistribution`. `distribution`,
  `logDistribution` and `range` cover the plot's columns: symbols that are universe members on at least
  one reported row. Names in the run panel only for warm-up or as successors stay out, so these outputs do
  not depend on simStart.
  Plot values stay raw; membership and heatmap center/scale are separate fields.
- Browser code renders the engine's financial results. It does not reconstruct daily
  returns from cumulative PnL, reconstruct gross returns from fees, or recalculate drawdown.

The simulation request selects `output`:

| Value | Result payload and work |
|---|---|
| `performance` | Summary, yearly metrics and performance series; no signal payload |
| `signals` | Signal plot/range/distributions without unrelated portfolio or metric work |
| `signalSeries` | Exact selected columns, requiring one concrete alpha and `signalPlotSymbols`; no unrelated portfolio/metrics/full T×N signal matrix |
| `full` | Performance and signal payloads; the default |
| `diagnostics` | Predictive diagnostics of the final signal (one concrete alpha, no `<meta>`); no portfolio/metrics or per-instrument panel. See Diagnostics Output |

Selected-series calculations still evaluate the full cross section when the pipeline needs
it. Meta weight estimation still performs its required member-return calculations.

## Diagnostics Output

`output="diagnostics"` with a `diagnostics` object. Required: `horizons` (distinct integers 1..252), `isEnd`.
Optional (absent = not computed): `quantiles` (default 5), `topk` (default [1,10]), `null {n, minShift, seed}`,
`groups` (`sector|industry`), `conditionOn` (a `<signal id>` of the same pipeline) with `conditionBuckets`,
`autocorrLags`, `distributionQuantiles`. Computed in one `run_signal_rows` pass; ring buffer of max-horizon rows plus IS rank rows (float32) for the null.

- Signal S[t] = final pipeline output; target cells: universe mask[t] (one-bar lagged, as above) and finite S[t].
- Forward return F_h[t] = Π_{k=1..h}(1+returns[t+k]) − 1 (total returns); any non-finite term excludes the cell; t+h past simEnd excludes the day. Outcome only, never a pipeline input.
- Day sample (t, h): target cells with finite F_h; fewer than 30 → NaN day. IC = Pearson, RankIC = Pearson of average-tie ranks.
- Quantiles: p = rank/n within the sample, bucket ceil(p·Q); bucket means of F_h, spread = bucket Q − bucket 1 (NaN if either bucket is empty).
- Windows: IS = isStart ≤ t and t+h ≤ isEnd (bar-based purge); OS = osStart ≤ t. IS summary per horizon for ic, rankic, spread: n, first/last timestamp,
  mean, std (ddof 1), icir = mean/std (not annualized), t = icir·√n, hacT (Newey–West Bartlett, lag max(5,h)), positiveFraction. OS: n, mean.
- coverage[t] = finite S / members; topk[t] = sum of the k largest |S| / Σ|S|; IS means in `isSummary`.
- null (IS, h=1 RankIC): N IS sample days, shifts k ~ U{minShift..N−minShift} (mt19937_64, seed), signal rank rows circularly shifted on the day axis against return rank rows,
  Pearson of per-row percentile ranks over the common cells. Reports actual, nullMean, nullStd, p = (#|null| ≥ |actual| + 1)/(n+1).
- groups: per code (finite, ≥ 0) daily h=1 RankIC; IS mean, n, hacT and mean group size. condition: percentile buckets of the saved C[t] within the sample, h=1 RankIC and spread per bucket, IS summary.
- distribution: IS daily quantiles of target S and missing fraction 1 − finite S / members, IS means. autocorr[L]: Pearson of percentile ranks of cells finite at t and t−L, IS mean.
- Daily arrays run from the first reported bar to simEnd, NaN as null.

## Saved Result Provenance

Saved results preserve their original context. Loading a result or editing current controls
never changes that context. Every detail request uses the selected result's context; an
unavailable saved universe is reported rather than replaced with the engine default.
Original metrics are never combined with signals computed under different settings.
Running with changed settings creates a new result. XML definitions remain stored as XML.

## Heatmap Display Statistics

The engine sends `signals.plot.centers` and `scales` for each returned date. Statistics use
all finite cells among the returned sampled columns, including finite values outside the
universe; the membership mask controls display separately. Each date is independent.

The center is the median of sorted finite values; an even median is `a/2 + b/2`. Scale is
`1.4826 * median(abs(x - center))`. When MAD is zero but values differ, scale uses Welford
population standard deviation over the sorted finite values while retaining the median
center. An empty row returns null statistics. The browser calculates only the display value
`Math.fround((value - center) / scale)` (zero for zero scale), then maps it to a color. Raw
signal values, plot sampling and tooltip values remain raw.

## Module Contracts

- Eligible per-instrument modules (FFD, TsRank and the TsArgExtreme family) may split instruments across workers. Dates and each instrument's accumulation/comparison order stay unchanged; serial and parallel outputs must match bitwise, including missing-value masks.

- Calling `init()` starts a fresh run: rolling sums, counts and input history must reset, including when the asset count changes.

- `MDelay(days)`:
  - output at `t` is input at `t-days`; unavailable lookback -> `NaN`
- `MTsMean(days, min_count=days)`:
  - fewer than `days` bars computed -> `NaN`
  - fewer than `min_count` finite values in the rolling window -> `NaN`
  - otherwise: mean of finite values in rolling window
  - the default `min_count=days` needs a full finite window, as pandas `rolling(days).mean()` does (`min_periods = days`);
    `min_count="1"` is the rule before 2026-11-11 (any window holding a finite value)
- `MTsZscore(days, ddof, min_count, eps)`:
  - rolling stats over finite window values
  - if valid count `< min_count`: output `NaN` (pandas `rolling(days)` convention; before 2026-11-12 the output
    was `0`, also where the current value is non-finite)
  - if current value is non-finite: `NaN`
  - if `std < eps`: `0`
- `MTsRank(days, pct, min_count)`:
  - ascending percentile rank of current value in rolling window finite values (higher value → higher rank)
  - if current is non-finite or valid count `< min_count`: `NaN`
  - tie handling: average rank
- `MDecay(days, min_count=days)`:
  - linear weighted average with newest weight `days`, oldest weight `1`
  - ignore non-finite values in numerator and denominator
  - fewer than `min_count` finite values in the rolling window -> `NaN`; the default `min_count=days` needs a full finite
    window, `min_count="1"` is the rule before 2026-11-11 (partial weights over whatever is finite, also during warm-up)
- `MRank(pct)`:
  - cross-sectional rank per bar on finite values, tie-aware
- `MZscore(ddof, min_count, eps)`:
  - cross-sectional z-score on finite values
  - if finite count `< min_count` or `scale < eps`: all `NaN`
- `<signal id>` names are unique within a pipeline: a second `<signal id="x">` is a compile error (`Duplicate signal id 'x'`); expanded alternatives (`|`, `<choice>`) compile separately.
- Saved-signal inputs `signal_1 .. signal_K` (2026-11-09, `include/SignalInputs.h`): ids saved earlier in the same pipeline by
  `<signal id>`, numbered from 1 without gaps; an unknown id, a gap, `signal_0`/leading zeros, a count outside the module's
  range, `input=` on the same tag, or `signal_N` on a module that is not `SignalInputs` is a compile error. The module reads the
  saved rows of the same bar and overwrites the current chain value (the current chain is not an operand); name the result
  with a following `<signal id>`. First valid bar: the latest first valid bar of the inputs. User modules inherit
  `SignalInputs` (visible through `ModulePrelude.h`) and receive the rows through the same compiler path.
  - `MSignalBinary` (`src/modules/SignalBinary.h`): `<add|sub|mul|div|min|max signal_1 signal_2/>` exactly two inputs, NaN if
    either is not finite, `sub` = s1 − s2, `div` = s1 / s2 (±inf → NaN); `<mean .../>` two or more, NaN unless all finite;
    `<nanmean ... min_count="1"/>` mean of finite inputs, NaN below `min_count` (≥ 1). Double arithmetic, one float rounding.
    `min`/`max` without `signal_N` are the unary `<min value>`/`<max value>`.
  - `MGate` (`Gate.h`): `<gate signal_1 signal_2 gt= ge= lt= le=/>` s1 where s2 is finite and passes every given bound, else NaN.
  - `MCsResid` (`CsResid.h`): `<csresid signal_1 signal_2 [signal_3 ...] [group]/>` per-bar OLS residual of s1 on s2..sK (column-
    pivoted QR) over run-universe cells with every input finite (and a finite group code ≥ 0); intercept without `group`,
    within-group demeaning and no intercept with it; fewer than 2 cells → all NaN.
  - `MBucketDemean` (`BucketDemean.h`): `<bucketdemean signal_1 signal_2 n="5"/>` s1 minus its mean within buckets
    ceil(pct·n) of s2's per-bar average-tie rank / count, over run-universe cells with both finite.
  - Single input (current chain), run-universe masked: `MCsDemedian` `<csdemedian/>` minus the cross-sectional median (mean of
    the middle two for an even count); `MCsWinsorQ` `<cswinsorq lo="0.01" hi="0.99"/>` clip to linearly interpolated
    cross-sectional quantiles, 0 ≤ lo < hi ≤ 1.
- inf is never a value: a ±inf cell read from a data file is NaN (missing); after every module call (each pipeline
  step and each `MChain` step, user modules included) ±inf in the output is replaced by `NaN`, so downstream modules
  and the gross-1 target normalization treat it exactly like `NaN` (the `(t, i)` cell gets no position).
- Number syntax in alpha XML (attributes and `<data expr>` literals): plain decimals only (`5`, `-0.5`, `.5`, `5.`),
  parsed locale-independently (`std::from_chars`); exponents (`1e-3`), hexadecimal, a leading `+`, `inf` and `nan` are
  errors. Integer attributes take `-?digits`; ranges `start...end` / `start...step...end` take integers only
  (`Invalid range '...'`), at most 10000 values per range. `FloatParameter` attributes and expr literals are
  parsed directly to float (rounded once; e.g. `exp="0.1"` is 0.100000001490116); a decimal that rounds to ±inf, or a
  non-zero decimal that rounds to 0, is an error (subnormal-range decimals are accepted). The combination
  finalizer attributes `shrink`/`alpha` are read the same way (float, one rounding); the weight computation is double. Values shown in the web never use scientific notation.
- Element-wise arithmetic `MUnary<Op>` (`src/modules/Unary.h`, stateless, in place):
  - chain tags `<abs/>` `<log/>` `<sqrt/>` `<sign/>` `<inverse/>` `<reverse/>` `<power exp>` `<signedpower exp>`
    `<max value>` `<min value>`; the `<data expr>` functions `abs log sqrt sign power(x, p) signedpower(x, p)`
    use the same Op, so `<data expr="f(x)"/>` equals `<data expr="x"/><f/>` bit for bit
    (the exponent must be a numeric literal; `-x`, `1/x` are ordinary expr arithmetic; no comparisons)
  - computed in double, stored as float; a non-finite input or result (including float overflow) is `NaN`
  - `log`: `x <= 0` -> `NaN`; `sqrt`: `x < 0` -> `NaN`; `inverse`: `±0` -> `NaN`; `sign(±0) = 0`
  - `power`: negative base with a non-integer exponent or `0^negative` -> `NaN`;
    `signedpower = sign(x)·|x|^exp` (`0^negative` -> `NaN`, `±0^p` -> `+0` for `p >= 0`)
  - `exp` (`<exp/>`, expr `exp(x)`): `e^x`; float overflow (x ≳ 88.7) -> `NaN`
  - `max`/`min` against the scalar `value` (ties return `value`, so `max(-0, 0) = +0`); a `NaN` input stays `NaN`
- Time-series tags ( per instrument, window = last `days` bars incl. today, lag 0 = today; non-finite values are missing):
  - `<tsdelta days=1>`: `x[t] − x[t−days]`; `NaN` if either is non-finite or fewer than `days+1` bars were seen (days=1 equals `x − delay(x,1)` bit for bit)
  - `<tssum days=20 min_count=days>`: sum of the finite window values; `NaN` if fewer than `min_count`; a `NaN` today does not force `NaN`
  - `<tsstddev days=20 ddof=1 min_count=days>`: std of the finite window values, divisor `n−ddof` (rolling double sums, variance clamped at 0); `NaN` if `n < min_count` or `n−ddof <= 0`; a window whose finite values are all equal is exactly `0`
  - `<tsargmax|tsargmin days=20 min_count=days>`: lag (0..days−1) of the max/min finite window value, ties → most recent; `NaN` if fewer than `min_count` finite values (monotonic deque, amortized O(1))
  - `<tsfill days=20>` (sample-and-hold): blocks `[k·days, (k+1)·days)` of the panel bar index (anchored at panel
    index 0, independent of simStart); the whole block outputs `x` at the block's first bar (`NaN` included, no carry of older
    values) and ignores the other inputs of the block; `days=1` is the identity. With delay ≥ 1 data the first block is `NaN`
  - `<tsmax|tsmin days=20 min_count=days>`: max/min of the finite window values; `NaN` if fewer than `min_count`; a `NaN` today does not force `NaN`
  - `<tsewma span=20 | halflife | alpha, adjust=1, min_count=1>`: pandas `ewm(alpha, adjust, ignore_na=False, min_periods=min_count).mean()` with the
    float-rounded parameter (α = 2/(span+1), 1−exp(ln 0.5/halflife) or alpha; at most one given); a missing bar outputs the previous mean;
    infinite memory, so the value depends on where computation starts (difference bounded by (1−α)^bars since then)
- Fractional difference and PCA residual:
  - `<ffd d=0.24 thres=0.00001>`: per instrument; weights `w_0 = 1`, `w_k = −w_{k−1}/k·(d−k+1)` until `|w_k| < thres` or 1000 weights
    (the desk's `getWeights_FFD`); `out[t] = Σ_k w_k·x~[t−k]` with `x~` the input forward-filled from its first finite value, double
    accumulation; `NaN` when today's input is non-finite or fewer than `len(w)` bars have passed since the first finite input. `d=0` is the identity
  - `<pcaresid group train=1260 refit=252 k=0 var=0.9>`: refit at panel bars with `b % refit == 0` once `train` input rows were computed, on the input rows `[b−train, b)`
    (`NaN` before the first fit); `group` is optional (`sector|industry`, validated like neutralize; absent = one block over the run universe).
    Fit set per group: in the run universe, finite code ≥ 0 equal on every train bar, finite input on every train bar. Columns standardized with the
    train mean/std (ddof 0; zero-std columns dropped); PCA as sklearn on the standardized matrix (`k > 0` fixes the component count, else the first `k`
    with cumulative explained-variance ratio ≥ `var`, `0 < var < 1`). Output at bar `t` of the block: `z_hat − z` for fitted instruments with finite
    input and the fitted group code at `t`, `z_hat = mu + V·s`, `s` the least-squares scores on the observed coordinates (`Vᵀ(z−mu)` when all are
    observed); everything else `NaN`. The run-universe mask is applied to the fit set only, not at `t` (rank/neutralize do that)
- Cross-sectional and group tags: the run universe mask is applied first exactly like rank/zscore/neutralize
  (excluded or non-finite → `NaN`, never in any statistic); statistics are two-pass double with sample sd (ddof 1).
  Group tags use neutralize's membership (finite value, finite code ≥ 0 on the signal date) and its `group="sector|industry"` validation.
  - `<winsorize std=4>`: clip to `[m − std·s, m + std·s]`; fewer than 2 finite values pass unchanged
  - `<normalize usestd=0 limit=0>`: `x − m`; `usestd`: `(x − m)/s`, whole row `NaN` if count < 2 or `s < 1e-12`; `limit > 0` clamps to `±limit`
  - `<hump hump=0.01>`: per instrument with `prev` (initially `NaN`); `limit = hump·Σ|x|` over today's masked finite inputs;
    `prev` `NaN` → `x`; `|x − prev| > limit` → `prev + sign·limit`, else `prev`; a missing/excluded instrument outputs `NaN` and resets `prev`
  - `<group_rank group pct=1>`: rank within the group with `<rank/>`'s tie rule, pct divides by the group size (one group = `<rank/>` bit for bit)
  - `<group_mean group weight=equal|cap mean=arithmetic|harmonic>`: arithmetic `Σw·x/Σw`, harmonic `Σw/Σ(w/x)` (`x = ±0` is not a member;
    reciprocal sum 0 or non-finite → group `NaN`); `cap` weights are the `cap` field on the previous bar (di−1, never di), a member without a
    valid weight still receives the mean, a group without any → `NaN`. Group PER = `<group_mean group="sector" weight="cap" mean="harmonic"/>`
  - `<group_zscore group>`: `(x − m_g)/(s_g + 1e-12)`; a group with fewer than 2 members or `s_g < 1e-12` → `NaN`
  - `<rank_sharp exp=2>`: `r = <rank/> pct − 0.5`, `sign(r)·|r|^exp` (exp=3 is the research `rank_sharp`)
  - `<truncate max side=0 post=1>` (per-name cap; put it last): sets are all nonzero `|x|` (side=0) or longs and shorts separately (side=1);
    output `sign(x)·min(|x|, c)`, zeros stay 0. post=1: `c` is the water-filling level so the weight after gross-1 normalization is ≤ `max`
    (of its leg for side=1); if `max·n_nonzero < 1` every nonzero member is clipped to the smallest nonzero `|x|` (equal weights).
    post=0: `c = max·Σ|x|` of the set. Desk `cs_truncate(x, t)` = `side=1 post=0`; `cs_truncate(x, t, post_trunc=True, absolute=True)` = defaults

- Affine: `<affine factor="1" offset="0"/>` maps each finite x to `factor * x + offset`, computed in double and stored as float; a non-finite input or result becomes NaN (the unary chain rule). Stateless, no universe mask.

- Alpha pool correlation (config `alpha_pool`): each `<alpha_pool>/<name>/alpha.xml` is simulated once at startup (new or changed entries on the next request) with `alpha_pool_universe` (default: the config default universe) and the config execution price over the whole panel; its daily net return by panel row is cached. The EW pool return on bar t is the mean of the pool alphas already active on t (first nonzero return <= t). `/api/simulate` adds `poolCorrelation: {ew, days}` to each alpha and combined result: the Pearson correlation of the daily net return of the run with the EW pool over the reported window [max(simStart, isStart), simEnd], using only bars at or after the first nonzero return of both sides; null below 60 overlapping bars or with zero variance. One number per run; nothing per pool alpha. The frontend shows `ew` right of the OS block. The pool is read-only for the engine.

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

### Combination methods

A SET is finalized by exactly one of `<ew/>`, `<ew sign="N"/>`, `<mvo days shrink rebalance/>`,
`<minvar days shrink rebalance/>`, `<erc days rebalance/>`, `<ivol days rebalance/>`,
`<spca days k rebalance/>`, `<glasso days alpha rebalance/>` (an implicit finalization is `<ew/>`).
Every method produces per-bar member weights w[t,k] applied to the members' gross-1 rows:
EW w = 1/K; sign-EW w ∝ sign(mean); ivol w ∝ 1/σ; MVO w ∝ Σ_s⁻¹μ; minvar w ∝ Σ_s⁻¹1; ERC equal
risk contribution on the sample Σ; sPCA and GLasso replicate the alpha_zoo research code (scaled
PCA with k components and plug-in MVO; graphical lasso precision on 0.99·corr + 0.01·I).
`shrink` is a number in [0,1] (Σ_δ = (1−δ)S + δ·diag S, default 0.5) or `lw` (Ledoit-Wolf, scaled
identity target), `cc` (constant-correlation target) or `oas`. Negative member weights are allowed;
constrained variants are separate work. Estimation uses the members' gross daily returns (collected over the
trading period only, i.e. up to the request's simEnd) over the `days` bars ending at t−1 (never
bar t); bars before `sim_start + days` use EW; `rebalance`
recomputes every N bars and carries the row in between; Σ|w| = 1 on every estimated row; a failed
or non-finite solution falls back to EW for that bar and is counted (`fallbackBars` in the
response; carried rows after a fallback count too). Member returns are collected once per set and
settings and cached across requests (a waiter on another request's computation never inherits that
request's failure or cancellation; it recomputes); the weighted pass re-runs the members once for
all candidates of a request round; the round holds one T×N float matrix per candidate
(C × T × N × 4 bytes), so keep the meta parameter grid per round small. Bars where a method
falls back to EW equal the `<ew/>` row up to float rounding (bit-identical only when K is a power
of two); a member whose weight is exactly 0 contributes nothing (every member row is finite after
normalization, so combined rows are NaN only on bars no member emits). Conventions: sPCA standardizes the window by its sd, applies the research
`_spca_weight` logic verbatim (no market neutralization: the engine has no market series) and maps
the standardized weights back by 1/sd; GLasso ports sklearn's coordinate-descent `graphical_lasso`
(max_iter 100, dual-gap tol 1e-4 as the research call) and keeps the research `(Σ+μμᵀ)⁻¹μ` form,
falling back to EW on non-convergence where the research used the unconverged estimate; `lw`/`oas` use the
sklearn formulas on the centered window; `cc` is Ledoit-Wolf (2004) covCor. A member whose window has zero variance (a flat book) makes that bar an
estimator failure for every method except sign-EW (EW row, counted); with `shrink="0"` and
`days ≤ K` the sample covariance is singular and the ridge-regularized solve returns a null-space
portfolio, so use δ > 0 there. Cost at K≈200: every method except GLasso takes seconds per candidate
even with daily rebalancing; GLasso takes minutes with `rebalance=1` and a few seconds with
`rebalance=21` when windows converge (non-converging windows cost 100 sweeps each; the research
used monthly), so use a rebalance of 21 or more for it. Linear algebra comes from Eigen 3.4.0 (header-only, fetched at configure time).
Responses: every meta alpha result and search candidate carries `meta: [{name, members, combine}]`,
plus `fallbackBars` and `firstEstimatedBar` (absolute panel bar) for non-EW combinations; a GA
request with `metaStats: true` adds `metaStats{materializations, memberRuns}` to `search_complete`.

## GA search and streamed results

The objective remains gross IS Sharpe. The initial search evaluates up to `firstSearch`
(default 200) distinct expanded XMLs. If the complete search space fits, it is evaluated
once and the run finishes in round 1. Later rounds evaluate up to `roundSize` new XMLs
(default 50), including 20% uniform exploration and offspring from successful parents.
Previously scheduled XMLs never consume another evaluation slot. Parameter lists are
trimmed and deduplicated. Structural `<choice>` searches use the compiler expansion
contract and its existing 5,000-pipeline cap; ordinary parameter spaces stay factored.

`stopRound` (default 40) includes the initial round. `stopAlphas` (default 400) limits
unique evaluation attempts, including failed candidates. Exhaustion, round limit or
candidate limit ends the run; budgets are reserved before parallel dispatch. `threads`
(one rule for simulate, save and GA requests: omitted means 20, otherwise 1..32, else 400;
each site uses at most its work count) limits candidate workers. Workers take the next available
candidate; controller-thread callbacks serialize events. XML keys, rather than hash
values alone, identify duplicates. Parent selection and equal-score ties use planned
candidate order, independent of completion order.

`/api/ga/stream` sends newline-delimited events:

- `search_start {runId, context}`
- `round_start {round, size}`
- `candidate {candidate: AlphaResult}`
- `round_end {round, bestSharpe}`
- `search_complete {stopReason, evaluated, bestXml}`
- `error {error}`

Rounds are one-based. Candidate results contain stable IDs, XML, round, origin, status,
IS/OS summary and yearly metrics; failed results carry their error. Full performance and
signals are requested for a selected candidate rather than broadcast for every evaluation.

`GET /api/ga/runs/{runId}/candidates/{id}/performance` answers a row click from what the
candidate's evaluation kept, without simulating. The body equals `/api/simulate`
`output=performance` for the candidate's XML and the run's settings (`alphas[0].id` is 0 there
too); pool correlation is computed at read time. It returns 404 for an unknown or evicted run, a
failed candidate or an invalid id. Runs share a process-wide 2 GiB store: over the cap whole
runs are evicted, oldest first, finished runs before any still streaming. A run larger than the
cap evicts itself and keeps no later candidates. On 404 the web client falls back to
`/api/simulate` `output=performance`, so those clicks simulate again; other errors are shown.

The web start button and Shift+Enter run the search. Stats contains all evaluated candidates;
round tabs appear on round start and fill as candidates complete. Stop preserves received
results and the source XML remains available after completion. The request uses `firstSearch`,
`roundSize`, `stopRound` and `stopAlphas`.

For two or more successful candidates, Stats and each round can request a combined result
using concrete `xmls`, `combinedOnly: true` and `output: "performance"`. Each entry compiles
to one alpha. The response uses the same nested `SimulationResponse`, with an aggregate
`AlphaResult` in `combined`. Signals are combined before final portfolio accounting; metrics
are not averaged. Membership and candidate order identify the cached combined result.
Selected performance and signal payloads are loaded independently using their purpose-specific
output requests. Display sorting never changes the stable candidate ID or saved XML identity.
