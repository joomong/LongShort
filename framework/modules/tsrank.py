import numpy as np
import pandas as pd
from collections import deque
from ..signal_base import Signal


class TSRank(Signal):
    """
    Rolling time-series rank of the latest value per symbol.
    - window: number of observations to keep
    - pct: True -> rank in [0,1], False -> 1..window
    - ascending: True -> smallest is rank 1
    - min_count: minimum finite observations required to emit a rank (default window)
    """
    def __init__(
        self,
        x: Signal,
        window: int = 20,
        *,
        pct: bool = True,
        ascending: bool = False,
        min_count: int | None = None,
    ):
        if window <= 0:
            raise ValueError("window must be positive")

        self.x = x
        self.window = int(window)
        self.pct = bool(pct)
        self.ascending = bool(ascending)
        self.min_count = int(min_count) if min_count is not None else self.window
        self.buf: dict[str, deque] = {}  # symbol -> deque of recent values

    def compute(self, ts: pd.Timestamp, ds: pd.DataFrame):
        vals = self.x.compute(ts, ds)
        if not isinstance(vals, pd.Series):
            return vals  # scalar/array-like input: leave untouched

        vals = vals.astype(float)
        out = pd.Series(index=vals.index, dtype=float)

        for sym, val in vals.items():
            q = self.buf.setdefault(sym, deque(maxlen=self.window))
            q.append(float(val))

            arr = np.asarray(q, dtype=float)
            finite_mask = np.isfinite(arr)
            valid = arr[finite_mask]

            # Need enough finite observations and the latest value must be finite to rank it
            if (len(valid) < self.min_count) or (not np.isfinite(arr[-1])):
                continue

            ranks = pd.Series(valid).rank(
                method="average", pct=self.pct, ascending=self.ascending
            )
            out.loc[sym] = ranks.iloc[-1]

        return out.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def tsrank(
    x: Signal,
    window: int = 20,
    *,
    pct: bool = True,
    ascending: bool = False,
    min_count: int | None = None,
):
    """Convenience wrapper to build a rolling time-series rank signal."""
    return TSRank(x, window=window, pct=pct, ascending=ascending, min_count=min_count)
