"""Parallel multi-alpha backtest via shared memory and ProcessPoolExecutor."""
from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from multiprocessing import shared_memory
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class SharedResultInfo:
    """Shared memory metadata for backtest results."""
    shm_name: str
    n_alphas: int
    n_timestamps_is: int
    n_timestamps_os: int
    n_metrics: int
    dtype: str


@dataclass
class SharedDataInfo:
    """Shared memory metadata for input data (3D array: T x N x C)."""
    shm_name: str
    shape: tuple          # (T, N, C)
    dtype: str
    timestamps_ns: list   # int64 nanosecond values
    symbols: list         # symbol strings
    columns: list         # column names
    transaction_fee_rate: float


class SharedResultManager:
    """Manages backtest result storage in shared memory."""

    N_IS_METRICS = 8
    N_OS_METRICS = 6

    def __init__(self):
        self.shm: Optional[shared_memory.SharedMemory] = None
        self.info: Optional[SharedResultInfo] = None

    def create(self, n_alphas: int, n_timestamps_is: int, n_timestamps_os: int) -> SharedResultInfo:
        size_per_alpha = (
            n_timestamps_is + n_timestamps_os +
            self.N_IS_METRICS + self.N_OS_METRICS
        )
        total_bytes = n_alphas * size_per_alpha * 8
        total_bytes = max(total_bytes, 8)

        self.shm = shared_memory.SharedMemory(create=True, size=total_bytes)
        arr = np.ndarray(
            (n_alphas, size_per_alpha), dtype=np.float64, buffer=self.shm.buf
        )
        arr[:] = np.nan

        self.info = SharedResultInfo(
            shm_name=self.shm.name,
            n_alphas=n_alphas,
            n_timestamps_is=n_timestamps_is,
            n_timestamps_os=n_timestamps_os,
            n_metrics=self.N_IS_METRICS + self.N_OS_METRICS,
            dtype="float64",
        )
        return self.info

    def get_result_array(self) -> np.ndarray:
        size_per_alpha = (
            self.info.n_timestamps_is + self.info.n_timestamps_os +
            self.N_IS_METRICS + self.N_OS_METRICS
        )
        return np.ndarray(
            (self.info.n_alphas, size_per_alpha),
            dtype=self.info.dtype,
            buffer=self.shm.buf,
        )

    def cleanup(self):
        if self.shm:
            self.shm.close()
            self.shm.unlink()
            self.shm = None


class SharedDataManager:
    """Manages input data in shared memory for parallel workers."""

    def __init__(self):
        self.shm: Optional[shared_memory.SharedMemory] = None

    def create(self, cache) -> SharedDataInfo:
        """Convert DataCache's numpy arrays to a 3D array in shared memory."""
        timestamps = cache.timestamps
        symbols = cache.symbols
        columns = sorted(cache.arrays.keys())

        arr = np.stack([cache.arrays[col] for col in columns], axis=2)

        total_bytes = max(arr.nbytes, 8)
        self.shm = shared_memory.SharedMemory(create=True, size=total_bytes)
        shared_arr = np.ndarray(arr.shape, dtype=np.float64, buffer=self.shm.buf)
        shared_arr[:] = arr

        return SharedDataInfo(
            shm_name=self.shm.name,
            shape=arr.shape,
            dtype="float64",
            timestamps_ns=[int(ts) for ts in timestamps.astype('int64')],
            symbols=symbols.tolist(),
            columns=columns,
            transaction_fee_rate=float(cache.transaction_fee_rate),
        )

    def cleanup(self):
        if self.shm:
            self.shm.close()
            self.shm.unlink()
            self.shm = None


class SharedCacheProxy:
    """Worker-side DataCache-compatible interface over shared 3D array."""

    def __init__(self, info: SharedDataInfo):
        self.info = info
        self._shm = shared_memory.SharedMemory(name=info.shm_name)
        self._arr = np.ndarray(info.shape, dtype=info.dtype, buffer=self._shm.buf)

        self.timestamps = np.array(info.timestamps_ns, dtype='datetime64[ns]')
        self.symbols = np.array(info.symbols, dtype=object)
        self.sym_index = {s: i for i, s in enumerate(info.symbols)}
        self._ts_index = {pd.Timestamp(ts): i for i, ts in enumerate(self.timestamps)}

        self.arrays = {}
        for c_idx, col in enumerate(info.columns):
            self.arrays[col] = self._arr[:, :, c_idx]

        self.initial_capital = 0
        self.transaction_fee_rate = info.transaction_fee_rate
        self.start_date = ""
        self.end_date = ""

    @property
    def _loaded(self) -> bool:
        return True

    def close(self):
        if self._shm:
            self._shm.close()
            self._shm = None


def write_result_to_shared(
    result_info: SharedResultInfo,
    alpha_idx: int,
    is_ret_series: np.ndarray,
    os_ret_series: np.ndarray,
    is_metrics: List[float],
    os_metrics: List[float],
):
    """Worker-side: write backtest result to its shared memory slot."""
    shm = shared_memory.SharedMemory(name=result_info.shm_name)
    try:
        size_per_alpha = (
            result_info.n_timestamps_is + result_info.n_timestamps_os +
            SharedResultManager.N_IS_METRICS + SharedResultManager.N_OS_METRICS
        )
        arr = np.ndarray(
            (result_info.n_alphas, size_per_alpha),
            dtype=result_info.dtype,
            buffer=shm.buf,
        )
        offset = 0
        arr[alpha_idx, offset:offset + len(is_ret_series)] = is_ret_series
        offset += result_info.n_timestamps_is
        arr[alpha_idx, offset:offset + len(os_ret_series)] = os_ret_series
        offset += result_info.n_timestamps_os
        arr[alpha_idx, offset:offset + len(is_metrics)] = is_metrics
        offset += SharedResultManager.N_IS_METRICS
        arr[alpha_idx, offset:offset + len(os_metrics)] = os_metrics
    finally:
        shm.close()


def _backtest_worker(
    alpha_idx: int,
    alpha,
    settings_tuple: tuple,
    capital: float,
    shared_data_info: SharedDataInfo,
    mode: str,
    result_info: SharedResultInfo,
) -> Tuple[int, bool, str]:
    """Worker function for parallel backtest execution."""
    proxy = None
    try:
        from .backtester import (
            Backtester, PeriodSettings,
            _build_period_result, _split_series_by_date,
        )

        settings = PeriodSettings(*settings_tuple)

        proxy = SharedCacheProxy(shared_data_info)

        bt = Backtester(
            cache=proxy,
            alpha_recipe=alpha,
            capital=capital,
            mode=mode,
            collect_viz=False,
            start_date=settings.is_start,
            end_date=settings.os_end,
        )
        ret_s, to_s, mb_s, mw_s, lw_s, sw_s, _ = bt.run()

        os_start = settings.os_start
        is_ret, os_ret = _split_series_by_date(ret_s, os_start)
        is_to, os_to = _split_series_by_date(to_s, os_start)
        is_mb, os_mb = _split_series_by_date(mb_s, os_start)
        is_mw, os_mw = _split_series_by_date(mw_s, os_start)
        is_lw, os_lw = _split_series_by_date(lw_s, os_start)
        is_sw, os_sw = _split_series_by_date(sw_s, os_start)

        is_period = _build_period_result(is_ret, is_to, is_mb, is_mw, is_lw, is_sw)
        os_period = _build_period_result(os_ret, os_to, os_mb, os_mw, os_lw, os_sw)

        is_metrics_obj = is_period.metrics
        os_metrics_obj = os_period.metrics

        is_ret_vals = is_ret.fillna(0.0).values
        os_ret_vals = os_ret.fillna(0.0).values

        is_metrics_list = [
            is_metrics_obj.pnl, is_metrics_obj.cagr,
            is_metrics_obj.sharpe, is_metrics_obj.mdd,
            is_metrics_obj.turnover, is_metrics_obj.min_bet,
            is_metrics_obj.max_weight, np.nan,
        ]
        os_metrics_list = [
            os_metrics_obj.pnl, os_metrics_obj.cagr,
            os_metrics_obj.sharpe, os_metrics_obj.mdd,
            np.nan, np.nan,
        ]

        write_result_to_shared(
            result_info, alpha_idx,
            is_ret_vals, os_ret_vals,
            is_metrics_list, os_metrics_list,
        )
        return (alpha_idx, True, "")

    except Exception as e:
        return (alpha_idx, False, str(e))
    finally:
        if proxy is not None:
            proxy.close()


def run_parallel_backtests(
    alphas: list,
    strategy,
    settings,
    capital: float,
    *,
    data_cache,
    mode: str = "D0",
    n_jobs: int = -1,
) -> list:
    """Run multiple alpha backtests in parallel using ProcessPoolExecutor."""
    from .backtester import (
        BacktestPeriodResult, PeriodResult, PeriodMetrics,
    )

    n_jobs = max(1, n_jobs)

    n_ts_is = data_cache.count_timestamps_in_range(settings.is_start, settings.is_end)
    n_ret_is = max(n_ts_is - 1, 0)

    n_ts_os = data_cache.count_timestamps_in_range(settings.os_start, settings.os_end)
    n_ret_os = n_ts_os

    n_alphas = len(alphas)
    result_manager = SharedResultManager()
    result_info = result_manager.create(n_alphas, n_ret_is, n_ret_os)

    data_manager = SharedDataManager()
    shared_data_info = data_manager.create(data_cache)

    settings_tuple = (settings.is_start, settings.is_end, settings.os_start, settings.os_end)

    success_map = {}
    try:
        with ProcessPoolExecutor(max_workers=n_jobs) as executor:
            futures = {}
            for idx, alpha in enumerate(alphas):
                fut = executor.submit(
                    _backtest_worker,
                    idx, alpha,
                    settings_tuple, capital,
                    shared_data_info, mode,
                    result_info,
                )
                futures[fut] = idx

            for fut in as_completed(futures):
                idx = futures[fut]
                try:
                    alpha_idx, success, err = fut.result()
                    success_map[alpha_idx] = success
                    if not success:
                        print(f"Alpha {alpha_idx} failed: {err}")
                except Exception as e:
                    success_map[idx] = False
                    print(f"Alpha {idx} worker error: {e}")

        results = []
        arr = result_manager.get_result_array()
        for idx in range(n_alphas):
            if not success_map.get(idx, False):
                results.append((idx, None))
                continue

            offset = 0
            is_ret_vals = arr[idx, offset:offset + n_ret_is]
            offset += n_ret_is
            os_ret_vals = arr[idx, offset:offset + n_ret_os]
            offset += n_ret_os
            is_metrics_vals = arr[idx, offset:offset + SharedResultManager.N_IS_METRICS]
            offset += SharedResultManager.N_IS_METRICS
            os_metrics_vals = arr[idx, offset:offset + SharedResultManager.N_OS_METRICS]

            is_metrics = PeriodMetrics(
                pnl=is_metrics_vals[0], cagr=is_metrics_vals[1],
                sharpe=is_metrics_vals[2], mdd=is_metrics_vals[3],
                turnover=is_metrics_vals[4], min_bet=is_metrics_vals[5],
                max_weight=is_metrics_vals[6],
            )
            os_metrics = PeriodMetrics(
                pnl=os_metrics_vals[0], cagr=os_metrics_vals[1],
                sharpe=os_metrics_vals[2], mdd=os_metrics_vals[3],
            )

            is_ret_series = pd.Series(is_ret_vals, dtype=float)
            os_ret_series = pd.Series(os_ret_vals, dtype=float)

            # Build minimal PeriodResult from shared memory data
            is_period = PeriodResult(
                metrics=is_metrics,
                ret_series=is_ret_series,
                turnover_series=pd.Series(dtype=float),
                min_bet_series=pd.Series(dtype=float),
                max_weight_series=pd.Series(dtype=float),
            )
            os_period = PeriodResult(
                metrics=os_metrics,
                ret_series=os_ret_series,
                turnover_series=pd.Series(dtype=float),
                min_bet_series=pd.Series(dtype=float),
                max_weight_series=pd.Series(dtype=float),
            )

            results.append((idx, BacktestPeriodResult(
                is_period=is_period,
                os_period=os_period,
            )))

        return results

    finally:
        data_manager.cleanup()
        result_manager.cleanup()
