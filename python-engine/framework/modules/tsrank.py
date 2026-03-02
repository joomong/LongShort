import numpy as np
from scipy.stats import rankdata
from ..signal_base import Signal
from framework.xml_helpers import parse_positive_int

import numba


@numba.jit(nopython=True, cache=True)
def _tsrank_kernel_numba(buf, cur_pos, N, window, min_count, pct, ascending):
    """Numba kernel: per-symbol rolling rank without scipy."""
    out = np.empty(N, dtype=np.float64)
    out[:] = np.nan
    for i in range(N):
        cur_val = buf[i, cur_pos]
        if cur_val != cur_val:  # NaN
            continue

        # Count valid entries
        n_valid = 0
        for j in range(window):
            v = buf[i, j]
            if v == v:
                n_valid += 1

        if n_valid < min_count:
            continue

        # Compute rank of current value among valid entries
        n_less = 0
        n_equal = 0
        for j in range(window):
            v = buf[i, j]
            if v != v:
                continue
            if ascending:
                if v < cur_val:
                    n_less += 1
                elif v == cur_val:
                    n_equal += 1
            else:
                if v > cur_val:
                    n_less += 1
                elif v == cur_val:
                    n_equal += 1

        # Average rank (1-based)
        rank_val = n_less + 1.0 + (n_equal - 1.0) / 2.0
        if pct:
            rank_val = rank_val / n_valid
        out[i] = rank_val
    return out

_tsrank_kernel = _tsrank_kernel_numba


class TSRank(Signal):
    tag = "tsrank"

    @classmethod
    def from_xml(cls, sig, attrib, registry):
        return cls(sig, parse_positive_int(attrib, "days"))

    """
    Rolling time-series rank of the latest value per symbol.
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
        self._buf: np.ndarray | None = None  # (N, window)
        self._pos: int = 0
        self._count: int = 0

    def reset(self):
        self._buf = None
        self._pos = 0
        self._count = 0
        super().reset()

    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        vals = self.x.compute(t_idx, arrays)
        N = len(vals)

        # Lazy init
        if self._buf is None:
            self._buf = np.full((N, self.window), np.nan, dtype=np.float64)
            self._pos = 0
            self._count = 0

        # Write current values
        self._buf[:, self._pos] = vals
        self._count += 1
        cur_pos = self._pos
        self._pos = (self._pos + 1) % self.window

        return _tsrank_kernel(
            self._buf, cur_pos, N, self.window,
            self.min_count, self.pct, self.ascending,
        )


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
