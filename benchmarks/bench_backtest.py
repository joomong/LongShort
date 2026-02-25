#!/usr/bin/env python
"""Performance benchmark: single backtest + signal kernels.

Measures wall-clock time for:
  1. Signal computation (DAG pass)
  2. Backtest loop (_backtest_loop kernel)
  3. Full backtest (Backtester.run)
  4. Stateful signal kernels (tsmean, delay, tsrank, tszscore, event)

Validates golden-master accuracy (rtol=1e-6) alongside timing.

Usage:
    python benchmarks/bench_backtest.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

# Ensure project root is on path
_PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT))

from framework.data import DataCache
from framework.backtester import Backtester, _compute_metrics

import numba


# ── Golden dataset (small, for accuracy) ──────────────────────────────────

DATES_GOLDEN = pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC")
SYMBOL_PRICES_GOLDEN = {
    "BTC": [100, 102, 99, 105, 103, 107, 110, 108, 112, 115],
    "ETH": [50, 48, 52, 51, 55, 53, 56, 58, 57, 60],
    "SOL": [10, 11, 10, 12, 11, 13, 12, 14, 13, 15],
}
RAW_SIGNALS = {"BTC": 1.0, "ETH": -1.0, "SOL": 0.5}
NORMALIZED_WEIGHTS = {"BTC": 0.4, "ETH": -0.4, "SOL": 0.2}


class ConstAlpha:
    def compute(self, t_idx, arrays):
        symbols = arrays["__symbols__"]
        return np.array([RAW_SIGNALS.get(s, 0.0) for s in symbols])


def _create_parquet(path: Path, n_symbols: int = 3, n_days: int = 10,
                    symbol_prices: dict | None = None,
                    dates: pd.DatetimeIndex | None = None):
    """Write OHLCV parquet. If symbol_prices given, use golden data."""
    if symbol_prices is not None and dates is not None:
        rows = []
        for i, ts in enumerate(dates):
            ts_ms = int(ts.value // 1_000_000)
            for sym, prices in symbol_prices.items():
                c = prices[i]
                rows.append({"open_time": ts_ms, "symbol": sym,
                             "open": float(c - 1), "high": float(c + 2),
                             "low": float(c - 2), "close": float(c),
                             "volume": 1000.0})
    else:
        # Synthetic large dataset
        dates = pd.date_range("2020-01-01", periods=n_days, freq="D", tz="UTC")
        syms = [f"SYM{i:04d}" for i in range(n_symbols)]
        rng = np.random.default_rng(42)
        rows = []
        for i, ts in enumerate(dates):
            ts_ms = int(ts.value // 1_000_000)
            for s in syms:
                c = 100.0 + rng.normal(0, 2)
                rows.append({"open_time": ts_ms, "symbol": s,
                             "open": c - 1, "high": c + 2,
                             "low": c - 2, "close": c, "volume": 1000.0})
    pd.DataFrame(rows).to_parquet(path, index=False)


def _make_config_and_cache(parquet_path, start_date, end_date):
    import yaml
    config = {
        "datasets": [{"name": "bench", "path": str(parquet_path),
                       "time_col": "open_time", "symbol_col": "symbol",
                       "use_col": ["open", "high", "low", "close", "volume"]}],
        "universe": None,
        "backtest": {"start_date": start_date, "end_date": end_date,
                     "initial_capital": 1000000, "transaction_fee": 10},
    }
    config_path = parquet_path.parent / "config.yaml"
    yaml.dump(config, open(config_path, "w"))
    cache = DataCache(str(config_path))
    cache.load()
    return cache


# ── Accuracy check ─────────────────────────────────────────────────────────

def check_golden_accuracy(tmp_dir: Path):
    """Run golden backtest and compare with independent calculation."""
    print("\n=== Golden-master accuracy check ===")
    pq = tmp_dir / "golden.parquet"
    _create_parquet(pq, symbol_prices=SYMBOL_PRICES_GOLDEN, dates=DATES_GOLDEN)
    cache = _make_config_and_cache(pq, "2024-01-01", "2024-01-10")

    bt = Backtester(cache=cache, alpha_recipe=ConstAlpha(),
                    capital=1_000_000, mode="D0",
                    start_date="2024-01-01", end_date="2024-01-10")
    _, ret_s, to_s, mb_s, mw_s, lw_s, sw_s, _ = bt.run()
    to_s = to_s[1:] if to_s.size == ret_s.size + 1 else to_s
    mb_s = mb_s[1:] if mb_s.size == ret_s.size + 1 else mb_s
    mw_s = mw_s[1:] if mw_s.size == ret_s.size + 1 else mw_s
    m = _compute_metrics(ret_s, to_s, mb_s, mw_s)

    # Independent calculation
    closes = pd.DataFrame(SYMBOL_PRICES_GOLDEN, index=DATES_GOLDEN).sort_index()
    syms = sorted(NORMALIZED_WEIGHTS.keys())
    w = np.array([NORMALIZED_WEIGHTS[s] for s in syms])
    asset_rets = closes[syms].values[1:] / closes[syms].values[:-1] - 1.0
    port_rets = (asset_rets * w).sum(axis=1)
    equity = np.cumprod(1.0 + port_rets)

    indep_pnl = float(equity[-1] - 1.0)
    indep_sharpe = float((port_rets.mean() / (port_rets.std(ddof=1) + 1e-12))
                         * np.sqrt(365))

    checks = [
        ("PnL", m.pnl, indep_pnl),
        ("Sharpe", m.sharpe, indep_sharpe),
    ]
    all_ok = True
    for name, bt_val, indep_val in checks:
        ok = np.isclose(bt_val, indep_val, rtol=1e-6)
        status = "PASS" if ok else "FAIL"
        print(f"  {name}: bt={bt_val:.8f}  indep={indep_val:.8f}  [{status}]")
        if not ok:
            all_ok = False

    # Return series element-wise
    bt_rets = ret_s
    ok = np.allclose(bt_rets, port_rets, rtol=1e-6)
    status = "PASS" if ok else "FAIL"
    print(f"  Return series (element-wise, rtol=1e-6): [{status}]")
    if not ok:
        all_ok = False

    return all_ok


# ── Performance benchmarks ─────────────────────────────────────────────────

class RandomAlpha:
    """Alpha returning random signals (seeded for reproducibility)."""
    def __init__(self, seed=123):
        self._rng = np.random.default_rng(seed)

    def compute(self, t_idx, arrays):
        n = len(arrays["__symbols__"])
        return self._rng.standard_normal(n)


def bench_full_backtest(tmp_dir: Path, n_symbols: int, n_days: int,
                        n_repeats: int = 5):
    """Benchmark full Backtester.run()."""
    pq = tmp_dir / f"bench_{n_symbols}x{n_days}.parquet"
    end_date = (pd.Timestamp("2020-01-01") + pd.Timedelta(days=n_days - 1))
    _create_parquet(pq, n_symbols=n_symbols, n_days=n_days)
    cache = _make_config_and_cache(pq, "2020-01-01",
                                    end_date.strftime("%Y-%m-%d"))

    # Warm-up (JIT compilation)
    alpha = RandomAlpha(seed=42)
    bt = Backtester(cache=cache, alpha_recipe=alpha,
                    capital=1_000_000, mode="D0",
                    start_date="2020-01-01",
                    end_date=end_date.strftime("%Y-%m-%d"))
    bt.run()

    times = []
    for i in range(n_repeats):
        alpha = RandomAlpha(seed=42 + i)
        bt = Backtester(cache=cache, alpha_recipe=alpha,
                        capital=1_000_000, mode="D0",
                        start_date="2020-01-01",
                        end_date=end_date.strftime("%Y-%m-%d"))
        t0 = time.perf_counter()
        bt.run()
        elapsed = time.perf_counter() - t0
        times.append(elapsed)

    return times


def bench_backtest_loop_only(n_symbols: int, n_days: int, n_repeats: int = 10):
    """Benchmark just the _backtest_loop kernel (Pass 2)."""
    from framework.backtester import _backtest_loop

    rng = np.random.default_rng(99)
    close_2d = 100.0 + rng.normal(0, 2, (n_days, n_symbols))
    signals_2d = rng.standard_normal((n_days, n_symbols))

    # Warm-up
    _backtest_loop(close_2d, signals_2d, 0.001, 0)

    times = []
    for _ in range(n_repeats):
        t0 = time.perf_counter()
        _backtest_loop(close_2d, signals_2d, 0.001, 0)
        elapsed = time.perf_counter() - t0
        times.append(elapsed)

    return times


class _BenchLeaf:
    """Minimal leaf signal for benchmarks (not a Signal subclass to avoid tag registration)."""
    def __init__(self, data_2d):
        self._data = data_2d
        self._t = 0
    def compute(self, t_idx, arrays):
        row = self._data[self._t % len(self._data)]
        self._t += 1
        return row


def bench_signal_kernels(n_symbols: int, n_days: int, n_repeats: int = 10):
    """Benchmark stateful signal kernels individually."""
    from framework.modules.tsmean import TsMean
    from framework.modules.delay import Delay
    from framework.modules.tsrank import TSRank
    from framework.modules.tszscore import TSZScore

    rng = np.random.default_rng(77)

    data = rng.standard_normal((n_days, n_symbols))
    results = {}

    for name, make_signal in [
        ("TsMean(20)", lambda d: TsMean(_BenchLeaf(d), window=20)),
        ("Delay(5)", lambda d: Delay(_BenchLeaf(d), period=5)),
        ("TSRank(20)", lambda d: TSRank(_BenchLeaf(d), window=20)),
        ("TSZScore(20)", lambda d: TSZScore(_BenchLeaf(d), window=20)),
    ]:
        # Warm-up
        sig = make_signal(data)
        arrays = {"__symbols__": [f"S{i}" for i in range(n_symbols)]}
        for t in range(n_days):
            sig.compute(t, arrays)

        times = []
        for _ in range(n_repeats):
            sig = make_signal(data)
            t0 = time.perf_counter()
            for t in range(n_days):
                sig.compute(t, arrays)
            elapsed = time.perf_counter() - t0
            times.append(elapsed)
        results[name] = times

    return results


# ── Main ───────────────────────────────────────────────────────────────────

def _fmt_times(times):
    arr = np.array(times)
    return f"mean={arr.mean()*1000:.2f}ms  std={arr.std()*1000:.2f}ms  min={arr.min()*1000:.2f}ms"


def main():
    import tempfile
    tmp_dir = Path(tempfile.mkdtemp(prefix="bench_"))
    print(f"Temp dir: {tmp_dir}")
    print(f"Numba version: {numba.__version__}")
    print()

    # 1. Golden accuracy
    accuracy_ok = check_golden_accuracy(tmp_dir)
    print(f"\n  Overall accuracy: {'PASS' if accuracy_ok else 'FAIL'}")

    # 2. Backtest loop kernel (pure computation, no I/O)
    print("\n=== Backtest loop kernel (_backtest_loop) ===")
    for n_sym, n_day in [(17, 365), (17, 1000), (50, 1000)]:
        times = bench_backtest_loop_only(n_sym, n_day)
        print(f"  {n_sym} syms x {n_day} days:  {_fmt_times(times)}")

    # 3. Signal kernels
    print("\n=== Stateful signal kernels (17 syms x 365 days) ===")
    sig_results = bench_signal_kernels(17, 365)
    for name, times in sig_results.items():
        print(f"  {name}:  {_fmt_times(times)}")

    print("\n=== Stateful signal kernels (50 syms x 1000 days) ===")
    sig_results = bench_signal_kernels(50, 1000)
    for name, times in sig_results.items():
        print(f"  {name}:  {_fmt_times(times)}")

    # 4. Full backtest (end-to-end)
    print("\n=== Full backtest (Backtester.run) ===")
    for n_sym, n_day in [(17, 365), (17, 1000), (50, 1000)]:
        times = bench_full_backtest(tmp_dir, n_sym, n_day, n_repeats=5)
        print(f"  {n_sym} syms x {n_day} days:  {_fmt_times(times)}")

    print()
    return 0 if accuracy_ok else 1


if __name__ == "__main__":
    sys.exit(main())
