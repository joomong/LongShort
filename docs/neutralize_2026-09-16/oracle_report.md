# `<neutralize>` real-data oracle — PASS

- config: `/home/jgo/longshort_platform/cpp-engine/examples/datasets/equities_monthly_afl.yaml`
- groups: `/home/jgo/longshort_platform/data/groups/cache_v1_cash_1d`
- server: `/tmp/longshort_build_lead/longshort-server` on port 50123, peak RSS 3,183,344 kB
- pass criteria: primary max|diff| < 1e-05, per-date group sums < 0.0001, NaN positions identical

- Panel 4200 bars x 1056 symbols (2010-01-04..2026-09-15); subset 60 symbols (every 17th). A requested over the full panel in 1.9s.
- A on the subset equals A on the full panel restricted to it: max|diff| 0.000e+00, NaN agreement 1.000000.
- Secondary rank stage alone (engine rank rewritten in the team convention) vs `cs_rank(returns[valid])`: max|diff| 4.376e-03, match share 0.999437, NaN agreement 1.000000 over 1,921,619 finite cells. Any residual below is inherited from this stage, not from `neutralize`.

## Primary — engine `neutralize` vs `cs_group_neut` on the engine's own A

| group    | scope           | max\|diff\| | match share (1e-5) | NaN agreement | both finite | cells     |
|----------|-----------------|-------------|--------------------|---------------|-------------|-----------|
| sector   | subset (60 sym) | 2.979e-08   | 1.00000000         | 1.00000000    | 100,755     | 252,000   |
| sector   | full panel      | 2.980e-08   | 1.00000000         | 1.00000000    | 1,921,619   | 4,435,200 |
| industry | subset (60 sym) | 2.980e-08   | 1.00000000         | 1.00000000    | 100,755     | 252,000   |
| industry | full panel      | 2.980e-08   | 1.00000000         | 1.00000000    | 1,921,619   | 4,435,200 |

## Group sums and verdict

| group    | codes | classified symbols | max per-date group sum | worst date | full-panel request | verdict |
|----------|-------|--------------------|------------------------|------------|--------------------|---------|
| sector   | 11    | 1056               | 6.119e-07              | 2026-08-12 | 1.9s               | PASS    |
| industry | 74    | 1056               | 2.403e-07              | 2014-02-18 | 2.0s               | PASS    |

## Secondary (report only) — full team chain `cs_group_neut(cs_rank(returns[valid]), codes)`

| group    | max\|diff\| raw | match share raw | max\|diff\| rank-scale adjusted | match share adjusted | NaN agreement |
|----------|-----------------|-----------------|---------------------------------|----------------------|---------------|
| sector   | 4.365e-03       | 0.571759        | 4.301e-03                       | 0.976900             | 1.000000      |
| industry | 4.185e-03       | 0.582247        | 4.046e-03                       | 0.993765             | 1.000000      |
