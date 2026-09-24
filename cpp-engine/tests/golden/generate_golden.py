#!/usr/bin/env python3
"""Generate golden files for C++ module and metrics cross-validation.

Uses numpy/pandas as the reference implementation. The C++ tests read the
resulting JSON files and compare against their own module outputs.
"""

import json
import math
import numpy as np
import pandas as pd
from pathlib import Path

OUTPUT_DIR = Path(__file__).parent


# ── Fixture: same as make_fixture() in test_module_oracles.cpp ──

FIXTURE = np.array([
    [10.0, 20.0, np.nan],
    [20.0, 20.0, 5.0],
    [30.0, np.nan, 5.0],
    [40.0, 10.0, 5.0],
    [np.nan, 30.0, 5.0],
    [60.0, 40.0, np.nan],
], dtype=np.float64)

T, N = FIXTURE.shape


# ── Module computations ──

def golden_delay(arr: np.ndarray, days: int) -> np.ndarray:
    out = np.full_like(arr, np.nan)
    out[days:] = arr[:-days]
    return out


def golden_tsmean(arr: np.ndarray, days: int) -> np.ndarray:
    """Rolling mean with NaN-tolerant semantics (skip NaN, average remaining).

    Warmup: output is NaN until t+1 >= days (not enough bars yet).
    After warmup: mean of finite values in [t-days+1 .. t]. If all NaN, output NaN.
    """
    T, N = arr.shape
    out = np.full_like(arr, np.nan)
    for t in range(T):
        if t + 1 < days:
            continue
        for n in range(N):
            window = arr[t + 1 - days:t + 1, n]
            valid = window[np.isfinite(window)]
            out[t, n] = np.mean(valid) if len(valid) > 0 else np.nan
    return out


def golden_tszscore(arr: np.ndarray, days: int, ddof: int, min_count: int, eps: float) -> np.ndarray:
    T, N = arr.shape
    out = np.full_like(arr, np.nan)

    for t in range(T):
        start = max(0, t + 1 - days)
        for n in range(N):
            window = arr[start:t+1, n]
            valid = window[np.isfinite(window)]
            n_valid = len(valid)

            if n_valid < min_count:
                out[t, n] = 0.0
                continue
            if not np.isfinite(arr[t, n]):
                out[t, n] = np.nan
                continue

            mean = np.mean(valid)
            denom = n_valid - ddof
            if denom > 0:
                variance = np.sum((valid - mean) ** 2) / denom
            else:
                variance = 0.0
            if variance < 0:
                variance = 0.0
            std = np.sqrt(variance)

            if std < eps:
                out[t, n] = 0.0
            else:
                out[t, n] = (arr[t, n] - mean) / (std + eps)

    return out


def golden_tsrank(arr: np.ndarray, days: int, pct: bool, min_count: int) -> np.ndarray:
    T, N = arr.shape
    out = np.full_like(arr, np.nan)

    for t in range(T):
        start = max(0, t + 1 - days)
        for n in range(N):
            cur = arr[t, n]
            if np.isnan(cur):
                out[t, n] = np.nan
                continue

            window = arr[start:t+1, n]
            valid = window[np.isfinite(window)]
            n_valid = len(valid)

            if n_valid < min_count:
                out[t, n] = np.nan
                continue

            n_less = np.sum(valid < cur)
            n_equal = np.sum(valid == cur)

            rank = n_less + 1.0 + (n_equal - 1.0) / 2.0
            out[t, n] = rank / n_valid if pct else rank

    return out


def golden_decay(arr: np.ndarray, days: int) -> np.ndarray:
    T, N = arr.shape
    out = np.full_like(arr, np.nan)

    for t in range(T):
        for n in range(N):
            weighted_sum = 0.0
            weight_total = 0.0
            for k in range(days):
                if t < k:
                    break
                v = arr[t - k, n]
                if not np.isfinite(v):
                    continue
                w = float(days - k)
                weighted_sum += v * w
                weight_total += w
            out[t, n] = weighted_sum / weight_total if weight_total > 0 else np.nan

    return out


def golden_rank(arr: np.ndarray, pct: bool) -> np.ndarray:
    T, N = arr.shape
    out = np.full_like(arr, np.nan)

    for t in range(T):
        row = arr[t]
        finite_mask = np.isfinite(row)
        n_finite = np.sum(finite_mask)
        if n_finite == 0:
            continue

        for n in range(N):
            vi = row[n]
            if not np.isfinite(vi):
                continue
            n_less = np.sum(row[finite_mask] < vi)
            n_equal = np.sum(row[finite_mask] == vi)
            rank = n_less + 1.0 + (n_equal - 1.0) / 2.0
            out[t, n] = rank / n_finite if pct else rank

    return out


def golden_zscore(arr: np.ndarray, ddof: int, min_count: int, eps: float) -> np.ndarray:
    T, N = arr.shape
    out = np.full_like(arr, np.nan)

    for t in range(T):
        row = arr[t]
        valid = row[np.isfinite(row)]
        cnt = len(valid)

        if cnt < min_count:
            continue

        mean = np.mean(valid)
        denom = cnt - ddof
        scale = np.sqrt(np.sum((valid - mean) ** 2) / denom) if denom > 0 else 0.0

        if scale < eps:
            continue

        for n in range(N):
            v = row[n]
            if np.isfinite(v):
                out[t, n] = (v - mean) / (scale + eps)

    return out


# ── Group neutralization (MNeutralize) ──

# Static classification codes for the 3 fixture symbols, broadcast over every row.
NEUTRALIZE_CODES = [1.0, 1.0, 2.0]

# A second case widens the fixture with two unclassified symbols: a missing code
# and a finite negative one. The shared FIXTURE above is left untouched so the
# other golden entries keep their values.
NEUTRALIZE_WIDE_FIXTURE = np.hstack([
    FIXTURE,
    np.array([[7.0], [np.nan], [3.0], [11.0], [4.0], [9.0]], dtype=np.float64),
    np.array([[2.0], [8.0], [6.0], [np.nan], [1.0], [5.0]], dtype=np.float64),
])
NEUTRALIZE_WIDE_CODES = [1.0, 1.0, 2.0, np.nan, -1.0]


def golden_neutralize(arr: np.ndarray, codes) -> np.ndarray:
    """Cross-sectional group demean, matching team `cs_group_neut_1d`.

    A cell joins its group only when the value is finite and the code is finite
    and >= 0; every other cell is NaN. Singleton groups become 0. No scaling and
    no minimum group size.
    """
    codes = np.asarray(codes, dtype=np.float64)
    out = np.full_like(arr, np.nan)
    for t in range(arr.shape[0]):
        row = arr[t]
        usable = np.isfinite(row) & np.isfinite(codes) & (codes >= 0.0)
        for code in np.unique(codes[usable]):
            members = usable & (codes == code)
            out[t, members] = row[members] - row[members].mean()
    return out


def crosscheck_neutralize(arr: np.ndarray, codes, expected: np.ndarray) -> str:
    """Compare the numpy reference above against the team sim implementation."""
    try:
        import sys
        sys.path.append('/home/base/sim/utils')
        from func_util.lib_funcs import cs_group_neut
    except Exception as error:  # noqa: BLE001 - the team library is optional here
        return f"SKIPPED (cannot import func_util.lib_funcs: {error})"

    columns = [f"SYM{n}" for n in range(arr.shape[1])]
    index = list(range(arr.shape[0]))
    alpha = pd.DataFrame(arr, index=index, columns=columns)
    group = pd.DataFrame(
        np.tile(np.asarray(codes, dtype=np.float64), (arr.shape[0], 1)),
        index=index, columns=columns)
    team = cs_group_neut(alpha, group).to_numpy()

    both_nan = np.isnan(team) & np.isnan(expected)
    diff = np.where(both_nan, 0.0, np.abs(team - expected))
    max_diff = float(np.nanmax(diff))
    if not np.array_equal(np.isnan(team), np.isnan(expected)):
        raise AssertionError("cs_group_neut NaN placement differs from the numpy reference")
    if max_diff > 1e-12:
        raise AssertionError(f"cs_group_neut differs from the numpy reference: max|diff|={max_diff}")
    return f"OK (max|diff|={max_diff:.3e} vs func_util.lib_funcs.cs_group_neut)"


# ── Metrics computation ──

def generate_metrics_input():
    """Same as test_metrics_oracle.cpp SummaryMatchesReferenceImplementation."""
    T = 140
    ret = np.zeros(T)
    turnover = np.zeros(T)
    max_weight = np.zeros(T)
    long_weight = np.zeros(T)
    short_weight = np.zeros(T)
    position_count = np.zeros(T, dtype=int)

    for i in range(T):
        ret[i] = ((i % 7 - 3) * 0.0015) + (0.0007 if i % 5 == 0 else -0.0002)
        if i % 17 == 0:
            ret[i] = np.nan

        turnover[i] = 0.10 + 0.02 * (i % 4)
        if i % 19 == 0:
            turnover[i] = np.nan

        max_weight[i] = 0.15 + 0.03 * (i % 5)
        if i % 11 == 0:
            max_weight[i] = np.nan

        long_weight[i] = 0.48 + 0.06 * math.sin(i * 0.2)
        short_weight[i] = 0.52 + 0.04 * math.cos(i * 0.1)
        position_count[i] = 0 if i % 6 == 0 else 3

    return ret, turnover, max_weight, long_weight, short_weight, position_count


def sharpe_of(r: np.ndarray, bars_per_year: int) -> float:
    if len(r) < 2:
        return 0.0
    mean = np.mean(r)
    std = np.std(r, ddof=1)
    if std <= 0.0:
        return 0.0
    return float((mean / (std + 1e-12)) * np.sqrt(bars_per_year))


def golden_summary(ret, turnover, max_weight, long_weight, short_weight, position_count, bars_per_year):
    finite_r = ret[np.isfinite(ret)]
    if len(finite_r) == 0:
        return {}

    cum = np.cumsum(finite_r)

    pnl = float(cum[-1])
    mean_r = float(np.mean(finite_r))
    ann_ret = mean_r * bars_per_year
    sharpe = sharpe_of(finite_r, bars_per_year)

    peak = np.maximum.accumulate(np.concatenate(([0.0], cum)))[1:]
    mdd = float(np.min(cum - peak))

    to_finite = turnover[np.isfinite(turnover)]
    avg_turnover = float(np.mean(to_finite)) if len(to_finite) > 0 else 0.0

    # avg_position_count: average number of symbols with position
    avg_position_count = float(np.mean(position_count))

    mw_finite = max_weight[np.isfinite(max_weight)]
    max_max_weight = float(np.max(mw_finite)) if len(mw_finite) > 0 else 0.0

    nz = int(np.sum(position_count > 0))

    wins = int(np.sum(finite_r > 0.0))
    win_rate = wins / len(finite_r)
    margin = mean_r / (avg_turnover + 1e-12)

    # long/short ratio
    n = min(len(long_weight), len(short_weight))
    sum_ratio = 0.0
    cnt = 0
    for i in range(n):
        lw, sw = long_weight[i], short_weight[i]
        if not (np.isfinite(lw) and np.isfinite(sw)):
            continue
        total = lw + sw
        if total <= 0.0:
            continue
        sum_ratio += lw / total
        cnt += 1
    long_ratio = sum_ratio / cnt if cnt > 0 else 0.0
    short_ratio = 1.0 - long_ratio

    # avg_max_bet = mean of finite max_weight
    avg_max_bet = float(np.mean(mw_finite)) if len(mw_finite) > 0 else 0.0

    # min_position_count: minimum number of symbols with position
    min_position_count = float(np.min(position_count))

    # quality
    maxexp = 0.0
    for i in range(n):
        lw, sw = long_weight[i], short_weight[i]
        if not (np.isfinite(lw) and np.isfinite(sw)):
            continue
        maxexp = max(maxexp, abs(lw - sw))
    quality = sharpe * math.sqrt(abs(1.0 - maxexp)) * (1.0 - max_max_weight)

    # sharpe400
    w400 = min(400, len(finite_r))
    sharpe400 = sharpe_of(finite_r[-w400:], bars_per_year)

    # sharpe80
    w80 = min(80, len(finite_r))
    sharpe80 = sharpe_of(finite_r[-w80:], bars_per_year)

    # minsoc
    mn = np.inf
    found = False
    for i in range(n):
        lw, sw = long_weight[i], short_weight[i]
        if not (np.isfinite(lw) and np.isfinite(sw)):
            continue
        mn = min(mn, lw + sw)
        found = True
    minsoc = mn if found else 0.0

    return {
        "pnl": pnl,
        "ann_ret": ann_ret,
        "sharpe": sharpe,
        "mdd": mdd,
        "turnover": avg_turnover,
        "avg_position_count": avg_position_count,
        "max_weight": max_max_weight,
        "active_bar_count": nz,
        "win_rate": win_rate,
        "margin": margin,
        "long_ratio": long_ratio,
        "short_ratio": short_ratio,
        "avg_max_bet": avg_max_bet,
        "min_position_count": min_position_count,
        "quality": quality,
        "sharpe400": sharpe400,
        "sharpe80": sharpe80,
        "minsoc": minsoc,
    }


# ── JSON serialization helpers ──

def ndarray_to_list(arr: np.ndarray):
    """Convert 2D array to nested list, NaN → None."""
    result = []
    for row in arr:
        result.append([None if np.isnan(v) else float(v) for v in row])
    return result


def main():
    # ── Module golden files ──
    modules = {}

    modules["delay_1"] = {
        "input": ndarray_to_list(FIXTURE),
        "params": {"days": 1},
        "expected": ndarray_to_list(golden_delay(FIXTURE, 1)),
    }

    modules["tsmean_3"] = {
        "input": ndarray_to_list(FIXTURE),
        "params": {"days": 3},
        "expected": ndarray_to_list(golden_tsmean(FIXTURE, 3)),
    }

    modules["tszscore_3"] = {
        "input": ndarray_to_list(FIXTURE),
        "params": {"days": 3, "ddof": 1, "min_count": 3, "eps": 1e-12},
        "expected": ndarray_to_list(golden_tszscore(FIXTURE, 3, 1, 3, 1e-12)),
    }

    modules["tsrank_3"] = {
        "input": ndarray_to_list(FIXTURE),
        "params": {"days": 3, "pct": True, "min_count": 3},
        "expected": ndarray_to_list(golden_tsrank(FIXTURE, 3, True, 3)),
    }

    modules["decay_3"] = {
        "input": ndarray_to_list(FIXTURE),
        "params": {"days": 3},
        "expected": ndarray_to_list(golden_decay(FIXTURE, 3)),
    }

    modules["rank_pct"] = {
        "input": ndarray_to_list(FIXTURE),
        "params": {"pct": True},
        "expected": ndarray_to_list(golden_rank(FIXTURE, True)),
    }

    modules["zscore"] = {
        "input": ndarray_to_list(FIXTURE),
        "params": {"ddof": 1, "min_count": 2, "eps": 1e-12},
        "expected": ndarray_to_list(golden_zscore(FIXTURE, 1, 2, 1e-12)),
    }

    neutralize_expected = golden_neutralize(FIXTURE, NEUTRALIZE_CODES)
    modules["neutralize_sector"] = {
        "input": ndarray_to_list(FIXTURE),
        "params": {"group": "sector", "codes": NEUTRALIZE_CODES},
        "expected": ndarray_to_list(neutralize_expected),
    }
    neutralize_crosscheck = crosscheck_neutralize(FIXTURE, NEUTRALIZE_CODES, neutralize_expected)

    wide_expected = golden_neutralize(NEUTRALIZE_WIDE_FIXTURE, NEUTRALIZE_WIDE_CODES)
    modules["neutralize_sector_unclassified"] = {
        "input": ndarray_to_list(NEUTRALIZE_WIDE_FIXTURE),
        # NaN codes serialize as null, like NaN values do.
        "params": {"group": "sector",
                   "codes": [None if np.isnan(c) else float(c) for c in NEUTRALIZE_WIDE_CODES]},
        "expected": ndarray_to_list(wide_expected),
    }
    wide_crosscheck = crosscheck_neutralize(
        NEUTRALIZE_WIDE_FIXTURE, NEUTRALIZE_WIDE_CODES, wide_expected)

    with open(OUTPUT_DIR / "modules.json", "w") as f:
        json.dump(modules, f, indent=2)
    print(f"Wrote {OUTPUT_DIR / 'modules.json'}")

    # ── Metrics golden file ──
    ret, turnover, max_weight, long_weight, short_weight, position_count = generate_metrics_input()

    summary = golden_summary(ret, turnover, max_weight, long_weight, short_weight, position_count, 365)

    # Also save raw input so C++ can verify it loaded correctly
    def vec_to_list(v):
        return [None if np.isnan(x) else float(x) for x in v]

    metrics_data = {
        "bars_per_year": 365,
        "input": {
            "ret": vec_to_list(ret),
            "turnover": vec_to_list(turnover),
            "max_weight": vec_to_list(max_weight),
            "long_weight": vec_to_list(long_weight),
            "short_weight": vec_to_list(short_weight),
            "position_count": [int(x) for x in position_count],
        },
        "expected": summary,
    }

    with open(OUTPUT_DIR / "metrics.json", "w") as f:
        json.dump(metrics_data, f, indent=2)
    print(f"Wrote {OUTPUT_DIR / 'metrics.json'}")

    # ── Verification: print a few values for sanity ──
    print("\n--- Sanity check ---")
    print(f"Delay(1) row 0: {modules['delay_1']['expected'][0]}")
    print(f"Delay(1) row 1: {modules['delay_1']['expected'][1]}")
    print(f"TsMean(3) row 2: {modules['tsmean_3']['expected'][2]}")
    print(f"Rank(pct) row 1: {modules['rank_pct']['expected'][1]}")
    print(f"Neutralize(sector) row 0: {modules['neutralize_sector']['expected'][0]}")
    print(f"Neutralize(sector) team cross-check: {neutralize_crosscheck}")
    print(f"Neutralize(unclassified) row 0: {modules['neutralize_sector_unclassified']['expected'][0]}")
    print(f"Neutralize(unclassified) team cross-check: {wide_crosscheck}")
    print(f"Summary sharpe: {summary['sharpe']:.10f}")
    print(f"Summary pnl: {summary['pnl']:.10f}")
    print(f"Summary mdd: {summary['mdd']:.10f}")
    print(f"Summary quality: {summary['quality']:.10f}")


if __name__ == "__main__":
    main()
