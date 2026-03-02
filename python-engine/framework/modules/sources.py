# STAR/framework/modules/sources/price.py
import numpy as np
from ..signal_base import Signal

class Price(Signal):
    def __init__(self, column: str = "close"):
        self.column = column
    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        return arrays[self.column][t_idx].copy()
