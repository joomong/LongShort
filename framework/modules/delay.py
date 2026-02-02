# STAR/framework/modules/stateful/delay.py
import pandas as pd
from collections import deque
from ..signal_base import Signal
from framework.exceptions import XmlCompileError

class Delay(Signal):
    tag = "delay"

    @classmethod
    def from_xml(cls, sig, attrib, registry):
        # NOTE: days allows negative/zero values (unlike parse_positive_int).
        # This matches original _compile_pipeline behavior for <delay>.
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
