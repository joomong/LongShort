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


class SharedResultManager:
    """Manages backtest result storage in shared memory."""

    N_IS_METRICS = 8  # pnl, cagr, sharpe, mdd, turnover, min_bet, max_weight, (ir placeholder)
    N_OS_METRICS = 6  # pnl, cagr, sharpe, mdd, (ir placeholder), (corr placeholder)

    def __init__(self):
        self.shm: Optional[shared_memory.SharedMemory] = None
        self.info: Optional[SharedResultInfo] = None

    def create(self, n_alphas: int, n_timestamps_is: int, n_timestamps_os: int) -> SharedResultInfo:
        size_per_alpha = (
            n_timestamps_is + n_timestamps_os +
            self.N_IS_METRICS + self.N_OS_METRICS
        )
        total_bytes = n_alphas * size_per_alpha * 8  # float64
        # Ensure at least 8 bytes to avoid zero-size allocation
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
    strategy,
    settings_tuple: tuple,  # (is_start, is_end, os_start, os_end)
    capital: float,
    data_loader_kwargs: dict,
    mode: str,
    result_info: SharedResultInfo,
    transaction_fee_rate: float = 0.0010,
) -> Tuple[int, bool, str]:
    """Worker function for parallel backtest execution.

    Runs a SINGLE continuous backtest from is_start to os_end, then splits
    the result at os_start so OS starts with carried-over weights.

    Returns: (alpha_idx, success, error_msg)
    """
    try:
        from .backtester import (
            Backtester, PeriodSettings,
            _compute_period_metrics, _split_result_by_date,
        )
        from .dataloader import DataLoader

        settings = PeriodSettings(*settings_tuple)

        # Single continuous run from is_start to os_end
        full_kwargs = {**data_loader_kwargs, "start_date": settings.is_start, "end_date": settings.os_end}
        full_loader = DataLoader(**full_kwargs)
        bt = Backtester(full_loader, alpha, strategy, capital, mode=mode, collect_viz=False, transaction_fee_rate=transaction_fee_rate)
        full_result, _ = bt.run()

        # Split at OS start
        is_result, os_result = _split_result_by_date(full_result, settings.os_start)

        is_metrics_obj = _compute_period_metrics(is_result)
        os_metrics_obj = _compute_period_metrics(os_result)

        # Write to shared memory
        is_ret_vals = is_result.ret_series.fillna(0.0).values
        os_ret_vals = os_result.ret_series.fillna(0.0).values

        is_metrics_list = [
            is_metrics_obj.pnl, is_metrics_obj.cagr,
            is_metrics_obj.sharpe, is_metrics_obj.mdd,
            is_metrics_obj.turnover, is_metrics_obj.min_bet,
            is_metrics_obj.max_weight, np.nan,  # ir placeholder
        ]
        os_metrics_list = [
            os_metrics_obj.pnl, os_metrics_obj.cagr,
            os_metrics_obj.sharpe, os_metrics_obj.mdd,
            np.nan, np.nan,  # ir, corr placeholders
        ]

        write_result_to_shared(
            result_info, alpha_idx,
            is_ret_vals, os_ret_vals,
            is_metrics_list, os_metrics_list,
        )
        return (alpha_idx, True, "")

    except Exception as e:
        return (alpha_idx, False, str(e))


def run_parallel_backtests(
    alphas: list,
    strategy,
    settings,  # PeriodSettings
    capital: float,
    *,
    data_loader_kwargs: dict,
    mode: str = "D0",
    n_jobs: int = -1,
    transaction_fee_rate: float = 0.0010,
) -> list:
    """Run multiple alpha backtests in parallel using ProcessPoolExecutor.

    Returns a list of (alpha_idx, BacktestPeriodResult) for successful alphas,
    plus (alpha_idx, None) for failed ones.
    """
    from .backtester import (
        BacktestPeriodResult, PeriodMetrics,
    )

    n_jobs = max(1, n_jobs)

    # We need to know how many IS/OS timestamps to size the shared result memory.
    # Run a quick probe to count timestamps.
    # With unified IS/OS (single continuous run + split), the IS period
    # has (n_ts_is - 1) returns (first bar has no return), and the OS period
    # has n_ts_os returns (transition bar now has a return from carried-over weights).
    from .dataloader import DataLoader
    is_probe = DataLoader(**{**data_loader_kwargs, "start_date": settings.is_start, "end_date": settings.is_end})
    n_ts_is = 0
    for _ in is_probe:
        n_ts_is += 1
    # IS returns: first bar has no return
    n_ret_is = max(n_ts_is - 1, 0)

    os_probe = DataLoader(**{**data_loader_kwargs, "start_date": settings.os_start, "end_date": settings.os_end})
    n_ts_os = 0
    for _ in os_probe:
        n_ts_os += 1
    # OS returns: transition bar now has a return (carried-over weights)
    n_ret_os = n_ts_os

    n_alphas = len(alphas)
    result_manager = SharedResultManager()
    result_info = result_manager.create(n_alphas, n_ret_is, n_ret_os)

    settings_tuple = (settings.is_start, settings.is_end, settings.os_start, settings.os_end)

    success_map = {}
    try:
        with ProcessPoolExecutor(max_workers=n_jobs) as executor:
            futures = {}
            for idx, alpha in enumerate(alphas):
                fut = executor.submit(
                    _backtest_worker,
                    idx, alpha, strategy,
                    settings_tuple, capital,
                    data_loader_kwargs, mode,
                    result_info,
                    transaction_fee_rate,
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

        # Read results from shared memory
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

            # Reconstruct series (timestamps not stored in shared memory, use index)
            is_ret_series = pd.Series(is_ret_vals, dtype=float)
            os_ret_series = pd.Series(os_ret_vals, dtype=float)

            results.append((idx, BacktestPeriodResult(
                is_metrics=is_metrics,
                os_metrics=os_metrics,
                is_ret_series=is_ret_series,
                os_ret_series=os_ret_series,
            )))

        return results

    finally:
        result_manager.cleanup()
