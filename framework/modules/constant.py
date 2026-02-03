# STAR/framework/modules/sources/constant.py
import numpy as np
from ..signal_base import Signal

class ConstantSignal(Signal):
    def __init__(self, value: float):
        self.value = float(value)
    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        N = arrays["__n_symbols__"]
        return np.full(N, self.value, dtype=np.float64)
