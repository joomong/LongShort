import numpy as np
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

    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        s = self.x.compute(t_idx, arrays)
        N = len(s)

        # Find benchmark index
        symbols = arrays["__symbols__"]
        bench_idx = None
        for i, sym in enumerate(symbols):
            if sym == self.benchmark:
                bench_idx = i
                break

        if bench_idx is None:
            return np.zeros(N, dtype=np.float64)

        b = float(s[bench_idx])
        if not np.isfinite(b):
            return np.zeros(N, dtype=np.float64)

        if self.mode == "diff":
            res = s - b
        elif self.mode == "ratio":
            res = (s / (b + self.eps)) - 1.0
        else:  # zscore
            self._update_bench_history(b)
            arr = np.asarray(self._buf, dtype=float)
            valid = arr[np.isfinite(arr)]
            if len(valid) < self.min_count:
                return np.zeros(N, dtype=np.float64)
            mean_b = valid.mean()
            std_b = valid.std(ddof=1)
            if (not np.isfinite(std_b)) or (std_b < self.eps):
                return np.zeros(N, dtype=np.float64)
            res = (s - mean_b) / (std_b + self.eps)

        res = np.where(np.isfinite(res), res, 0.0)
        return res


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
