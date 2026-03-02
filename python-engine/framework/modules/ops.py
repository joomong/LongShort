import numpy as np
from ..signal_base import Signal

class _Binary(Signal):
    def __init__(self, a: Signal, b: Signal):
        self.a, self.b = a, b

class Add(_Binary):
    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        return self.a.compute(t_idx, arrays) + self.b.compute(t_idx, arrays)

class Sub(_Binary):
    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        return self.a.compute(t_idx, arrays) - self.b.compute(t_idx, arrays)

class Mul(_Binary):
    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        return self.a.compute(t_idx, arrays) * self.b.compute(t_idx, arrays)

class Div(_Binary):
    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        return self.a.compute(t_idx, arrays) / self.b.compute(t_idx, arrays)

class Le(_Binary):  # <=
    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        x = self.a.compute(t_idx, arrays)
        y = self.b.compute(t_idx, arrays)
        return (x <= y).astype(np.float64)

class Ge(_Binary):  # >=
    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        x = self.a.compute(t_idx, arrays)
        y = self.b.compute(t_idx, arrays)
        return (x >= y).astype(np.float64)
