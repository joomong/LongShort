# STAR/framework/modules/stateful/delay.py
import numpy as np
from ..signal_base import Signal
from framework.exceptions import XmlCompileError

try:
    import numba
    _HAS_NUMBA = True
except ImportError:
    _HAS_NUMBA = False


def _delay_kernel_numpy(vals, buf, write_pos, period, count):
    """Numpy fallback: circular buffer read/write for delay."""
    N = len(vals)
    buf_size = period + 1
    if count > period:
        read_pos = (write_pos - period) % buf_size
        out = buf[:, read_pos].copy()
    else:
        out = np.full(N, np.nan, dtype=np.float64)
    buf[:, write_pos] = vals
    return out


if _HAS_NUMBA:
    @numba.jit(nopython=True, cache=True)
    def _delay_kernel_numba(vals, buf, write_pos, period, count):
        N = len(vals)
        buf_size = period + 1
        out = np.empty(N, dtype=np.float64)
        if count > period:
            read_pos = (write_pos - period) % buf_size
            for i in range(N):
                out[i] = buf[i, read_pos]
        else:
            out[:] = np.nan
        buf[:, write_pos] = vals
        return out

    _delay_kernel = _delay_kernel_numba
else:
    _delay_kernel = _delay_kernel_numpy


class Delay(Signal):
    tag = "delay"

    @classmethod
    def from_xml(cls, sig, attrib, registry):
        days_raw = (attrib.get("days") or "").strip()
        if not days_raw:
            raise XmlCompileError("Missing required attribute: days")
        try:
            days = int(days_raw)
        except ValueError as e:
            raise XmlCompileError(f"Invalid days: {days_raw!r}") from e
        return cls(sig, days)

    def __init__(self, x: Signal, period: int):
        self.x, self.p = x, int(period)
        self._buf: np.ndarray | None = None  # (N, p+1) circular buffer
        self._pos: int = 0  # write position in circular buffer
        self._count: int = 0  # number of values written so far

    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        vals = self.x.compute(t_idx, arrays)
        N = len(vals)

        # Lazy init buffer
        if self._buf is None:
            self._buf = np.full((N, self.p + 1), np.nan, dtype=np.float64)
            self._pos = 0
            self._count = 0

        out = _delay_kernel(vals, self._buf, self._pos, self.p, self._count)
        self._count += 1
        self._pos = (self._pos + 1) % (self.p + 1)
        return out
