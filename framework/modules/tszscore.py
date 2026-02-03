import numpy as np
from ..signal_base import Signal
from framework.xml_helpers import parse_positive_int

try:
    import numba
    _HAS_NUMBA = True
except ImportError:
    _HAS_NUMBA = False


def _tszscore_kernel_numpy(buf, cur_pos, N, min_count, ddof, eps):
    """Numpy fallback: per-symbol z-score from buffer."""
    out = np.zeros(N, dtype=np.float64)
    for i in range(N):
        row = buf[i]
        valid = row[np.isfinite(row)]

        if len(valid) < min_count:
            continue

        mean = valid.mean()
        std = valid.std(ddof=ddof)

        if (not np.isfinite(std)) or (std < eps):
            out[i] = 0.0
            continue

        cur_val = row[cur_pos]
        if np.isfinite(cur_val):
            z = (cur_val - mean) / (std + eps)
            out[i] = z if np.isfinite(z) else 0.0

    return out


if _HAS_NUMBA:
    @numba.jit(nopython=True, cache=True)
    def _tszscore_kernel_numba(buf, cur_pos, N, window, min_count, ddof, eps):
        """Numba kernel: per-symbol z-score without numpy array ops."""
        out = np.zeros(N, dtype=np.float64)
        for i in range(N):
            cur_val = buf[i, cur_pos]
            if cur_val != cur_val:  # NaN
                continue

            # Compute mean and count
            s = 0.0
            n_valid = 0
            for j in range(window):
                v = buf[i, j]
                if v == v:
                    s += v
                    n_valid += 1

            if n_valid < min_count:
                continue

            mean = s / n_valid

            # Compute std
            ss = 0.0
            for j in range(window):
                v = buf[i, j]
                if v == v:
                    d = v - mean
                    ss += d * d

            denom = n_valid - ddof
            if denom <= 0:
                continue
            std = (ss / denom) ** 0.5

            if std < eps:
                out[i] = 0.0
                continue

            z = (cur_val - mean) / (std + eps)
            if z == z:  # NaN check
                out[i] = z

        return out

    _tszscore_kernel = _tszscore_kernel_numba
else:
    _tszscore_kernel = None  # Use numpy path directly


class TSZScore(Signal):
    tag = "tszscore"

    @classmethod
    def from_xml(cls, sig, attrib, registry):
        return cls(sig, parse_positive_int(attrib, "days"))

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
        self._buf: np.ndarray | None = None  # (N, window)
        self._pos: int = 0
        self._count: int = 0

    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        vals = self.x.compute(t_idx, arrays)
        N = len(vals)

        # Lazy init
        if self._buf is None:
            self._buf = np.full((N, self.window), np.nan, dtype=np.float64)
            self._pos = 0
            self._count = 0

        # Write
        self._buf[:, self._pos] = vals
        self._count += 1
        cur_pos = self._pos
        self._pos = (self._pos + 1) % self.window

        if _HAS_NUMBA:
            return _tszscore_kernel(
                self._buf, cur_pos, N, self.window,
                self.min_count, self.ddof, self.eps,
            )
        else:
            return _tszscore_kernel_numpy(
                self._buf, cur_pos, N,
                self.min_count, self.ddof, self.eps,
            )


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
