# STAR/framework/modules/stateful/tsmean.py
import numpy as np
from ..signal_base import Signal
from framework.xml_helpers import parse_positive_int

import numba


@numba.jit(nopython=True, cache=True)
def _tsmean_kernel_numba(vals, buf, pos, window, count):
    N = len(vals)
    buf[:, pos] = vals
    out = np.empty(N, dtype=np.float64)
    if count + 1 >= window:
        for i in range(N):
            s = 0.0
            c = 0
            for j in range(window):
                v = buf[i, j]
                if v == v:  # NaN check
                    s += v
                    c += 1
            out[i] = s / c if c > 0 else np.nan
    else:
        out[:] = np.nan
    return out

_tsmean_kernel = _tsmean_kernel_numba


class TsMean(Signal):
    tag = "tsmean"

    @classmethod
    def from_xml(cls, sig, attrib, registry):
        return cls(sig, parse_positive_int(attrib, "days"))

    def __init__(self, x: Signal, window: int):
        self.x, self.window = x, int(window)
        self._buf: np.ndarray | None = None  # (N, window) circular buffer
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

        col = self._pos % self.window
        out = _tsmean_kernel(vals, self._buf, col, self.window, self._count)
        self._count += 1
        self._pos = (self._pos + 1) % self.window
        return out
