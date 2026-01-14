# STAR/framework/modules/sources/constant.py
import pandas as pd
from ..signal_base import Signal

class ConstantSignal(Signal):
    def __init__(self, value: float):
        self.value = float(value)
    def compute(self, timestamp: pd.Timestamp, data_slice: pd.DataFrame):
        return pd.Series(self.value, index=data_slice.index, dtype=float)
