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


# ── Metrics computation ──

def generate_metrics_input():
    """Same as test_metrics_oracle.cpp SummaryMatchesReferenceImplementation."""
    T = 140
    ret = np.zeros(T)
    turnover = np.zeros(T)
    min_bet = np.zeros(T)
    max_weight = np.zeros(T)
    long_weight = np.zeros(T)
    short_weight = np.zeros(T)
    nonzero_count = np.zeros(T, dtype=int)

    for i in range(T):
        ret[i] = ((i % 7 - 3) * 0.0015) + (0.0007 if i % 5 == 0 else -0.0002)
        if i % 17 == 0:
            ret[i] = np.nan

        turnover[i] = 0.10 + 0.02 * (i % 4)
        if i % 19 == 0:
            turnover[i] = np.nan

        min_bet[i] = 0.015 + 0.004 * (i % 3)
        if i % 13 == 0:
            min_bet[i] = np.nan

        max_weight[i] = 0.15 + 0.03 * (i % 5)
        if i % 11 == 0:
            max_weight[i] = np.nan

        long_weight[i] = 0.48 + 0.06 * math.sin(i * 0.2)
        short_weight[i] = 0.52 + 0.04 * math.cos(i * 0.1)
        nonzero_count[i] = 0 if i % 6 == 0 else 3

    return ret, turnover, min_bet, max_weight, long_weight, short_weight, nonzero_count


def sharpe_of(r: np.ndarray, bars_per_year: int) -> float:
    if len(r) < 2:
        return 0.0
    mean = np.mean(r)
    std = np.std(r, ddof=1)
    if std <= 0.0:
        return 0.0
    return float((mean / (std + 1e-12)) * np.sqrt(bars_per_year))


def golden_summary(ret, turnover, min_bet, max_weight, long_weight, short_weight, nonzero_count, bars_per_year):
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

    # min_bet: average number of symbols with position (matches C++ which uses nonzero_count)
    avg_min_bet = float(np.mean(nonzero_count))

    mw_finite = max_weight[np.isfinite(max_weight)]
    max_max_weight = float(np.max(mw_finite)) if len(mw_finite) > 0 else 0.0

    nz = int(np.sum(nonzero_count > 0))

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

    # min_bet_min: minimum number of symbols with position (matches C++ nonzero_count)
    min_bet_min = float(np.min(nonzero_count))

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
        "min_bet": avg_min_bet,
        "max_weight": max_max_weight,
        "nonzero_position": nz,
        "win_rate": win_rate,
        "margin": margin,
        "long_ratio": long_ratio,
        "short_ratio": short_ratio,
        "avg_max_bet": avg_max_bet,
        "min_bet_min": min_bet_min,
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

    with open(OUTPUT_DIR / "modules.json", "w") as f:
        json.dump(modules, f, indent=2)
    print(f"Wrote {OUTPUT_DIR / 'modules.json'}")

    # ── Metrics golden file ──
    ret, turnover, min_bet, max_weight, long_weight, short_weight, nonzero_count = generate_metrics_input()

    summary = golden_summary(ret, turnover, min_bet, max_weight, long_weight, short_weight, nonzero_count, 365)

    # Also save raw input so C++ can verify it loaded correctly
    def vec_to_list(v):
        return [None if np.isnan(x) else float(x) for x in v]

    metrics_data = {
        "bars_per_year": 365,
        "input": {
            "ret": vec_to_list(ret),
            "turnover": vec_to_list(turnover),
            "min_bet": vec_to_list(min_bet),
            "max_weight": vec_to_list(max_weight),
            "long_weight": vec_to_list(long_weight),
            "short_weight": vec_to_list(short_weight),
            "nonzero_count": [int(x) for x in nonzero_count],
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
    print(f"Summary sharpe: {summary['sharpe']:.10f}")
    print(f"Summary pnl: {summary['pnl']:.10f}")
    print(f"Summary mdd: {summary['mdd']:.10f}")
    print(f"Summary quality: {summary['quality']:.10f}")


if __name__ == "__main__":
    main()
