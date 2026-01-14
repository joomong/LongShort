# STAR/framework/modules/stateful/sma.py
import pandas as pd
from collections import deque
from ..signal_base import Signal

class SMA(Signal):
    def __init__(self, x: Signal, window: int):
        self.x, self.window = x, int(window)
        self.buf = {}  # symbol -> deque

    def compute(self, ts: pd.Timestamp, ds: pd.DataFrame):
        vals = self.x.compute(ts, ds)
        out = pd.Series(index=vals.index, dtype=float)
        for sym, val in vals.items():
            q = self.buf.setdefault(sym, deque(maxlen=self.window))
            q.append(float(val))
            if len(q) == self.window:
                out.loc[sym] = sum(q) / self.window
        return out
