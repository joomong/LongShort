# `<neutralize>` uninterrupted runs — Top500 / Top1000 / ALL (2026-09-16)

Base config `cpp-engine/examples/datasets/equities_monthly_afl.yaml` (v1 cache, `base:` block,
`data/groups/cache_v1_cash_1d`), `universe.path` swapped by
`run_top500_missing_close.py::universe_config()` to
`/home/base/cache/cache_v1_cash_1d/TopUniverseV1/<name>.parquet`. One server per universe on its
own port (50118/50119/50120), started and stopped by the run script; the shared server on 50007
was not touched. Full trading period of the config (`start_date: 2010-01-01`, `end_date: latest`,
252 warmup bars), `isStart=2010-01-01`, `osStart=2100-01-01`.

## Requests

| universe | T x N | XML | HTTP | alphas | wall | server peak RSS | loadedFields | loadedDataBytes |
|---|---|---|---|---|---|---|---|---|
| Top500 | 4,451 x 1,056 | `sector` | 200 | 1 | 1.1s | 881,252 kB | 9 | 169,209,216 |
| Top500 | 4,451 x 1,056 | `industry` | 200 | 1 | 1.2s | 1,328,732 kB | 9 | 169,209,216 |
| Top500 | 4,451 x 1,056 | `sector\|industry` | 200 | 2 | 0.4s | 1,328,732 kB | 9 | 169,209,216 |
| Top1000 | 4,451 x 2,316 | `sector` | 200 | 1 | 1.9s | 1,371,216 kB | 9 | 371,106,576 |
| Top1000 | 4,451 x 2,316 | `industry` | 200 | 1 | 2.0s | 2,070,308 kB | 9 | 371,106,576 |
| Top1000 | 4,451 x 2,316 | `sector\|industry` | 200 | 2 | 1.0s | 2,078,148 kB | 9 | 371,106,576 |
| ALL | 4,451 x 6,402 | `sector` | 200 | 1 | 3.3s | 2,209,620 kB | 9 | 1,025,830,872 |
| ALL | 4,451 x 6,402 | `industry` | 200 | 1 | 3.5s | 3,061,128 kB | 9 | 1,025,830,872 |
| ALL | 4,451 x 6,402 | `sector\|industry` | 200 | 2 | 2.8s | 3,133,192 kB | 9 | 1,025,830,872 |

XML column is the `group` attribute; the request bodies are:

- `sector` -> `<data expr="returns" delay="0"/><rank/><neutralize group="sector"/>`
- `industry` -> `<data expr="returns" delay="0"/><rank/><neutralize group="industry"/>`
- `sector|industry` -> `<data expr="returns" delay="0"/><rank/><neutralize group="sector|industry"/>`

## Startup and memory

| universe | startup (load) | peak RSS after the three runs | eager field bytes |
|---|---|---|---|
| Top500 | 5.0s | 1,328,732 kB | 169,209,216 |
| Top1000 | 5.0s | 2,078,148 kB | 371,106,576 |
| ALL | 8.0s | 3,133,192 kB | 1,025,830,872 |

## Result

- 9/9 requests returned HTTP 200; no server exited or crashed, and every server was stopped by the script that started it.
- The piped `group="sector|industry"` request expands to 2 alphas in every universe, the single-group
  requests to 1, as the compiler contract requires.
- `loadedFields` is 9 everywhere (the `base:` block's seven price fields plus `sector` and
  `industry`); AFL stays lazy and contributes nothing to `loadedDataBytes`.
- `ALL.parquet` restricted to the configured trading period leaves N=6,402 symbols, not the whole
  28,210-symbol cache: `read_universe` keeps only columns that are a member on at least one bar
  inside the panel.
- Peak RSS grows with the panel but stays far below the machine's 125 GB; the largest run (ALL,
  3,133,192 kB) holds 1.03 GB of eager field values plus per-request signal buffers.

## Stale-group-panel guard (loader)

`parquet_reader.cpp::load_config` now refuses to start when `base.groups` is configured and a bar on
which `close` has at least one finite value carries no finite `sector` (or `industry`) code:

```
Group panel 'sector' has no codes on 2026-09-15: base.groups is stale or was built for another
cache; regenerate it with tools/data/build_group_panels.py
```

Without it an out-of-date or foreign group panel aligns to nothing (`alignment: exact`), every code
becomes NaN, `neutralize` emits an all-NaN row and the book quietly empties over that stretch — the
failure mode both the WP-A and WP-B reviews asked to be closed. One pass over T x N for the two
fields; the ALL run above (4,451 x 6,402) shows no measurable startup cost.

Checked against the three shipped `base:` configs before and after — none of them trips the guard:

| config | close bars | group bars | close bars absent from the group panel | close symbols absent from it |
|---|---|---|---|---|
| `equities_monthly_afl.yaml` (v1 live) | 11,816 | 11,816 | 0 | 0 |
| `compare_v1_snapshot.yaml` (v1 snapshot cache + v1 groups) | 11,815 | 11,816 | 0 | 0 |
| `compare_v2*.yaml` (v2) | 6,713 | 6,713 | 0 | 0 |

Mutation self-test: with the `throw` disabled (`if (false && quoted && !classified)`) both new cases
in `tests/test_parquet_loader.cpp` fail; with it restored all 33 cases pass.
