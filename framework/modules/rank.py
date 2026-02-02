# STAR/framework/modules/ops/rank.py
import pandas as pd
from ..signal_base import Signal

class Rank(Signal):
    tag = "rank"

    def __init__(self, x: Signal, pct: bool = True):
        self.x, self.pct = x, pct
    def compute(self, ts: pd.Timestamp, ds: pd.DataFrame):
        v = self.x.compute(ts, ds)
        return v.rank(pct=self.pct)
