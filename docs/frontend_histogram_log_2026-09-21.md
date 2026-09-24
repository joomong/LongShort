# Log histogram deployment — 2026-09-21

The signal histogram now defaults to log-spaced bins computed by the C++ engine from every finite value over the complete requested trading span and all instruments, before heatmap sampling. Signal values and simulation metrics are unchanged.

- Positive-only data uses log; negative-only data uses negative-log; mixed signs or zeros use sign-preserving log1p with the smallest nonzero magnitude as linthresh.
- All 200 bins have raw-unit centers and 201 raw-unit edges; the full min/max, zeros, tiny finite values, and both tails are retained. Nonfinite values are excluded.
- `signalDistributionLog` is additive and distinct from the existing corporate-action `distributionLog` ledger. Its nested `linear` histogram uses the identical full finite population and full range. The legacy top-level `distribution` remains backward compatible.
- UI defaults to Log, offers Linear, and uses gray bars with a separate spectrum strip. Tooltips retain raw values and actual representative symbol/date click targets. Older cached responses without the new field remain explicitly Linear until rerun.

## Validation

- Frontend: 90 unit tests, 11 Chromium E2E tests, production build/type/lint pass. The optional existing live-engine E2E is skipped without its environment variable; actual engine/browser verification below was executed separately.
- C++: 28 CTest targets passed; the HTTP target originally skipped without a fixture, so it was then explicitly executed against a freshly generated synthetic fixture: 25 HTTP tests passed. Final serializer suite: 23 tests passed, including zero/tiny/negative/constant/extreme/missing values, count conservation, paired-mode population equality and independence from heatmap sampling.
- D0/D1 comparison of close, returns+rank, and close+tsmean+rank: all existing response fields matched the previous live binary. Full 2020–2026 close run: existing raw signals, linear histogram, summaries, yearly values and time-series values matched; only additional diagnostic fields were introduced.
- Real browser at http://172.22.34.60:50006: 200 bins, 7,090,251 observations; Log peak 4.8693%, full-range Linear peak 99.9597%. Identical totals, default Log selected, zero page errors. (The earlier legacy trimmed-range Linear peak was 95.36%; the new paired Linear uses the complete range.)

## Runtime

- Existing frontend address/port retained: 50006; API port retained: 50007.
- Running binary: `/tmp/longshort_histogram_engine_20260921`, PID at deployment 3338109.
- Config: `/home/jgo/longshort_platform/cpp-engine/examples/datasets/compare_v2_all.yaml`; log `/tmp/longshort_histogram_live_50007.log`.
- Prior executable retained for rollback: `/tmp/longshort_module_server/build/longshort-server`. Port sharing was unavailable, so replacement was started after stopping the prior process; readiness and real response verified afterward.
- Temporary validation engine on 50008 stopped after cutover. Source builds remain in `/tmp/longshort_build_final`.
