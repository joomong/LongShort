# Math Contract (C++ Engine)

This document is the single source of truth for C++ engine test math.

## Scope

- Applies only to `cpp-engine`.
- `python-engine` is independent and not used as a correctness oracle here.

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
  `SimulatorConfig.sim_end_index`; omission uses the loaded panel end. Earlier
  bars still warm the modules. Execution, signals, metrics and diagnostics stop
  at that end, with no added terminal liquidation. A capped run must equal the
  same-start uncapped run through the selected end.
- An initially empty book has no holding return on its first trading bar. It pays
  entry fees and, with OHLC4/VWAP execution, can earn the fill-to-close trading return.
- Explicit XML delay owns signal timing; there is no Interval/execution mode.
  - The pipeline output at bar `t` is masked and normalized with the universe membership and group codes known at the close of bar `t - pit_lag` (`backtest.pit_lag`, default 1, since 2026-10-29; before that the membership decided on the close of `t` itself was applied at `t`), then executed at `t`. The first `pit_lag` bars of the loaded panel have no membership, so size `warmup_bars` accordingly.
  - `<data expr="close" delay="1"/>` reads the prior bar exactly once.
  - Holding return at `t` uses previous executed weights and the close-to-close move.
- Execution-price contract (`backtest.execution_price`): `close` is the general
  configuration default; the stock examples explicitly select `ohlc4`.
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
  missing-quote carry/error policy (only in-memory panels can still present an infinite price, which is refused). A missing OHL/VWAP field or as-of source
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

- Calling `init()` starts a fresh run: rolling sums, counts and input history must reset, including when the asset count changes.

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
  - if current is non-finite or valid count `< min_count`: `NaN`
  - tie handling: average rank
- `MDecay(days)`:
  - linear weighted average with newest weight `days`, oldest weight `1`
  - ignore non-finite values in numerator and denominator
- `MRank(pct)`:
  - cross-sectional rank per bar on finite values, tie-aware
- `MZscore(ddof, min_count, eps)`:
  - cross-sectional z-score on finite values
  - if finite count `< min_count` or `scale < eps`: all `NaN`
- `<signal id>` names are unique within a pipeline: a second `<signal id="x">` is a compile error (`Duplicate signal id 'x'`); expanded alternatives (`|`, `<choice>`) compile separately.
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
- Time-series tags (2026-10-22; per instrument, window = last `days` bars incl. today, lag 0 = today; non-finite values are missing):
  - `<tsdelta days=1>`: `x[t] − x[t−days]`; `NaN` if either is non-finite or fewer than `days+1` bars were seen (days=1 equals `x − delay(x,1)` bit for bit)
  - `<tssum days=20 min_count=days>`: sum of the finite window values; `NaN` if fewer than `min_count`; a `NaN` today does not force `NaN`
  - `<tsstddev days=20 ddof=1 min_count=days>`: std of the finite window values, divisor `n−ddof` (rolling double sums, variance clamped at 0); `NaN` if `n < min_count` or `n−ddof <= 0`; a window whose finite values are all equal is exactly `0`
  - `<tsargmax|tsargmin days=20 min_count=days>`: lag (0..days−1) of the max/min finite window value, ties → most recent; `NaN` if fewer than `min_count` finite values (monotonic deque, amortized O(1))
  - `<tsfill days=20>` (sample-and-hold, 2026-10-26): blocks `[k·days, (k+1)·days)` of the panel bar index (anchored at panel
    index 0, independent of simStart); the whole block outputs `x` at the block's first bar (`NaN` included, no carry of older
    values) and ignores the other inputs of the block; `days=1` is the identity. With delay ≥ 1 data the first block is `NaN`
  - `<tsmax|tsmin days=20 min_count=days>`: max/min of the finite window values; `NaN` if fewer than `min_count`; a `NaN` today does not force `NaN`
  - `<tsewma span=20 | halflife | alpha, adjust=1, min_count=1>`: pandas `ewm(alpha, adjust, ignore_na=False, min_periods=min_count).mean()` with the
    float-rounded parameter (α = 2/(span+1), 1−exp(ln 0.5/halflife) or alpha; at most one given); a missing bar outputs the previous mean;
    infinite memory, so the value depends on the panel start (difference bounded by (1−α)^warmup)
- Fractional difference and PCA residual (2026-10-29, built in from the combo15 MR workspace modules; outputs are bit-identical to them):
  - `<ffd d=0.24 thres=0.00001>`: per instrument; weights `w_0 = 1`, `w_k = −w_{k−1}/k·(d−k+1)` until `|w_k| < thres` or 1000 weights
    (the desk's `getWeights_FFD`); `out[t] = Σ_k w_k·x~[t−k]` with `x~` the input forward-filled from its first finite value, double
    accumulation; `NaN` when today's input is non-finite or fewer than `len(w)` bars have passed since the first finite input. `d=0` is the identity
  - `<pcaresid group train=1260 refit=252 k=0 var=0.9>`: refit at panel bars `b ≥ train` with `b % refit == 0` on the input rows `[b−train, b)`
    (`NaN` before the first fit); `group` is optional (`sector|industry`, validated like neutralize; absent = one block over the run universe).
    Fit set per group: in the run universe, finite code ≥ 0 equal on every train bar, finite input on every train bar. Columns standardized with the
    train mean/std (ddof 0; zero-std columns dropped); PCA as sklearn on the standardized matrix (`k > 0` fixes the component count, else the first `k`
    with cumulative explained-variance ratio ≥ `var`, `0 < var < 1`). Output at bar `t` of the block: `z_hat − z` for fitted instruments with finite
    input and the fitted group code at `t`, `z_hat = mu + V·s`, `s` the least-squares scores on the observed coordinates (`Vᵀ(z−mu)` when all are
    observed); everything else `NaN`. The run-universe mask is applied to the fit set only, not at `t` (rank/neutralize do that)
- Cross-sectional and group tags (2026-10-22): the run universe mask is applied first exactly like rank/zscore/neutralize
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

- Affine (2026-10-29, built in from the former `examples/modules` sample): `<affine factor="1" offset="0"/>` maps each finite x to `factor * x + offset`, computed in double and stored as float; a non-finite input or result becomes NaN (the unary chain rule). Stateless, no universe mask.

- Alpha pool correlation (2026-10-29, config `alpha_pool`): each `<alpha_pool>/<name>/alpha.xml` is simulated once at startup (new or changed entries on the next request) with `alpha_pool_universe` (default: the config default universe) and the config execution price over the whole panel; its daily net return by panel row is cached. The EW pool return on bar t is the mean of the pool alphas already active on t (first nonzero return <= t). `/api/simulate` adds to every batch (and `combined`) `poolCorr: {ew, days}`: the Pearson correlation of the daily net return of the run with the EW pool over the sim window [simStart, simEnd], using only bars at or after the first nonzero return of both sides; null below 60 overlapping bars or with zero variance. One number per run; nothing per pool alpha. The frontend shows `ew` right of the OS block. The pool is read-only for the engine.

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

### Combination methods (phase 3)

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
Responses: every meta batch and GA candidate event carries `meta: [{name, members, combine}]`,
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
