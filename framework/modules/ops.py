import pandas as pd
import numpy as np
from ..signal_base import Signal

class _Binary(Signal):
    def __init__(self, a: Signal, b: Signal):
        self.a, self.b = a, b

class Add(_Binary):
    def compute(self, ts: pd.Timestamp, ds: pd.DataFrame):
        return self.a.compute(ts, ds) + self.b.compute(ts, ds)

class Sub(_Binary):
    def compute(self, ts: pd.Timestamp, ds: pd.DataFrame):
        return self.a.compute(ts, ds) - self.b.compute(ts, ds)

class Mul(_Binary):
    def compute(self, ts: pd.Timestamp, ds: pd.DataFrame):
        return self.a.compute(ts, ds) * self.b.compute(ts, ds)

class Div(_Binary):
    def compute(self, ts: pd.Timestamp, ds: pd.DataFrame):
        return self.a.compute(ts, ds) / self.b.compute(ts, ds)
# --- 비교 노드: 결과를 원소별로 float(0/1)로 반환 ---
def _to_float01(res):
    # res: bool or array-like of bool
    if isinstance(res, (pd.Series, pd.DataFrame, np.ndarray)):
        return res.astype(float)
    # python bool
    return 1.0 if bool(res) else 0.0

class Le(_Binary):  # <=
    def compute(self, ts, df):
        x = self.a.compute(ts, df)
        y = self.b.compute(ts, df)
        return _to_float01(x <= y)

class Ge(_Binary):  # >=
    def compute(self, ts, df):
        x = self.a.compute(ts, df)
        y = self.b.compute(ts, df)
        return _to_float01(x >= y)