import numpy as np
import pandas as pd
from collections import deque
from ..signal_base import Signal


class TSZScore(Signal):
    """
    Time-series z-score of an underlying signal per symbol.
    Keeps a rolling window and standardizes the latest value.
    """
    def __init__(
        self,
        x: Signal,
        window: int = 20,
        *,
        ddof: int = 1,
        min_count: int | None = None,
        epsilon: float = 1e-12,
    ):
        if window <= 0:
            raise ValueError("window must be positive")

        self.x = x
        self.window = int(window)
        self.ddof = int(ddof)
        self.min_count = int(min_count) if min_count is not None else self.window
        self.eps = float(epsilon)
        self.buf: dict[str, deque] = {}  # symbol -> deque of recent values

    def compute(self, ts: pd.Timestamp, ds: pd.DataFrame):
        vals = self.x.compute(ts, ds)
        if not isinstance(vals, pd.Series):
            # Non-vector input: nothing to standardize
            return vals

        vals = vals.astype(float)
        out = pd.Series(index=vals.index, dtype=float)

        for sym, val in vals.items():
            q = self.buf.setdefault(sym, deque(maxlen=self.window))
            q.append(float(val))

            arr = np.asarray(q, dtype=float)
            valid = arr[np.isfinite(arr)]

            if len(valid) < self.min_count:
                continue

            mean = valid.mean()
            std = valid.std(ddof=self.ddof)

            if (not np.isfinite(std)) or (std < self.eps):
                out.loc[sym] = 0.0
                continue

            out.loc[sym] = (arr[-1] - mean) / (std + self.eps)

        return out.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def tszscore(
    x: Signal,
    window: int = 20,
    *,
    ddof: int = 1,
    min_count: int | None = None,
    epsilon: float = 1e-12,
):
    """Convenience wrapper to build a time-series z-score signal."""
    return TSZScore(x, window=window, ddof=ddof, min_count=min_count, epsilon=epsilon)
