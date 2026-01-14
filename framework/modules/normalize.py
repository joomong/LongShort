import numpy as np
import pandas as pd
from ..signal_base import Signal


class NormalizeBySymbol(Signal):
    """
    Cross-sectional normalization using a benchmark symbol.
    - mode="diff":   s - b
    - mode="ratio": (s / b) - 1
    - mode="zscore": (s - mean_b) / std_b  (mean/std over benchmark's own time series)
    """
    def __init__(
        self,
        x: Signal,
        benchmark: str = "BTCUSDT",
        *,
        mode: str = "diff",
        epsilon: float = 1e-12,
        window: int | None = None,
        min_count: int | None = None,
    ):
        mode = mode.lower()
        if mode not in {"diff", "ratio", "zscore"}:
            raise ValueError("mode must be 'diff', 'ratio', or 'zscore'")
        if window is not None and window <= 0:
            raise ValueError("window must be positive when provided")

        self.x = x
        self.benchmark = str(benchmark)
        self.mode = mode
        self.eps = float(epsilon)
        self.window = int(window) if window is not None else None
        # Defaults: need at least 2 points for std; else fall back to window
        self.min_count = (
            int(min_count)
            if min_count is not None
            else (self.window if self.window is not None else 2)
        )
        self._buf: list[float] = []  # benchmark history for zscore

    def _update_bench_history(self, value: float):
        self._buf.append(value)
        if self.window is not None and len(self._buf) > self.window:
            self._buf = self._buf[-self.window :]

    def compute(self, ts, ds):
        s = self.x.compute(ts, ds)
        if not isinstance(s, pd.Series):
            return s

        s = s.astype(float)
        if self.benchmark not in s.index:
            return pd.Series(0.0, index=s.index)

        b = float(s.loc[self.benchmark])
        if not np.isfinite(b):
            return pd.Series(0.0, index=s.index)

        if self.mode == "diff":
            res = s - b
        elif self.mode == "ratio":
            res = (s / (b + self.eps)) - 1.0
        else:  # zscore
            self._update_bench_history(b)
            arr = np.asarray(self._buf, dtype=float)
            valid = arr[np.isfinite(arr)]
            if len(valid) < self.min_count:
                return pd.Series(0.0, index=s.index)
            mean_b = valid.mean()
            std_b = valid.std(ddof=1)
            if (not np.isfinite(std_b)) or (std_b < self.eps):
                res = pd.Series(0.0, index=s.index)
            else:
                res = (s - mean_b) / (std_b + self.eps)

        return res.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def normalize_by_symbol(
    x: Signal,
    benchmark: str = "BTCUSDT",
    *,
    mode: str = "diff",
    epsilon: float = 1e-12,
    window: int | None = None,
    min_count: int | None = None,
):
    return NormalizeBySymbol(
        x,
        benchmark=benchmark,
        mode=mode,
        epsilon=epsilon,
        window=window,
        min_count=min_count,
    )
