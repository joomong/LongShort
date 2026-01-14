# STAR/framework/modules/sources/price.py
import pandas as pd
from ..signal_base import Signal

class Price(Signal):
    def __init__(self, column: str = "close"):
        self.column = column
    def compute(self, timestamp: pd.Timestamp, data_slice: pd.DataFrame):
        return data_slice[self.column]
