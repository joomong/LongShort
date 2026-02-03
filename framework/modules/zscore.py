import numpy as np
from ..signal_base import Signal

class ZScore(Signal):
    tag = "zscore"

    def __init__(
        self,
        x: Signal,
        *,
        robust: bool = False,
        ddof: int = 1,
        min_count: int = 2,
        epsilon: float = 1e-12,
    ):
        self.x = x
        self.robust = robust
        self.ddof = ddof
        self.min_count = min_count
        self.eps = epsilon

    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        s = self.x.compute(t_idx, arrays)
        finite = np.isfinite(s)
        count = int(finite.sum())

        if count < self.min_count:
            return np.zeros_like(s)

        vals = s[finite]
        if self.robust:
            center = np.nanmedian(vals)
            scale = 1.4826 * np.nanmedian(np.abs(vals - center))
        else:
            center = np.nanmean(vals)
            scale = np.nanstd(vals, ddof=self.ddof)

        if (not np.isfinite(scale)) or (scale < self.eps):
            return np.zeros_like(s)

        z = (s - center) / (scale + self.eps)
        z = np.where(np.isfinite(z), z, 0.0)
        return z
