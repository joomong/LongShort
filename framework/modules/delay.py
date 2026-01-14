# STAR/framework/modules/stateful/delay.py
import pandas as pd
from collections import deque
from ..signal_base import Signal

class Delay(Signal):
    def __init__(self, x: Signal, period: int):
        self.x, self.p = x, int(period)
        self.buf = {}  # symbol -> deque(maxlen=p+1)

    def compute(self, ts: pd.Timestamp, ds: pd.DataFrame):
        vals = self.x.compute(ts, ds)
        out = pd.Series(index=vals.index, dtype=float)
        for sym, v in vals.items():
            q = self.buf.setdefault(sym, deque(maxlen=self.p + 1))
            if len(q) > self.p:
                out.loc[sym] = q[0]
            q.append(float(v))
        return out
