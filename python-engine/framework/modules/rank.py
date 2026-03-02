# STAR/framework/modules/ops/rank.py
import numpy as np
from scipy.stats import rankdata
from ..signal_base import Signal

class Rank(Signal):
    tag = "rank"

    def __init__(self, x: Signal, pct: bool = True):
        self.x, self.pct = x, pct

    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        v = self.x.compute(t_idx, arrays)
        out = np.full_like(v, np.nan)
        finite = np.isfinite(v)
        if not finite.any():
            return out
        ranks = rankdata(v[finite], method="average")
        if self.pct:
            n = int(finite.sum())
            ranks = ranks / n
        out[finite] = ranks
        return out
