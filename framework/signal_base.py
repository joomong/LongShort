# STAR/framework/signal_base.py
from abc import ABC, abstractmethod
import pandas as pd

class Signal(ABC):
    @abstractmethod
    def compute(self, timestamp: pd.Timestamp, data_slice: pd.DataFrame):
        ...

    # 연산자 오버로드는 "얕은" 의존만: 구현체는 modules.ops.algebra 에 둠
    def _to_signal(self, value):
        from .modules.constant import ConstantSignal  # 늦은 import 로 순환참조 회피
        if isinstance(value, (int, float)): return ConstantSignal(value)
        if not isinstance(value, Signal):
            raise TypeError(f"Signal만 연산 가능: {type(value)}")
        return value

    def __add__(self, other):
        from .modules.ops import Add
        return Add(self, self._to_signal(other))
    def __sub__(self, other):
        from .modules.ops import Sub
        return Sub(self, self._to_signal(other))
    def __mul__(self, other):
        from .modules.ops import Mul
        return Mul(self, self._to_signal(other))
    def __truediv__(self, other):
        from .modules.ops import Div
        return Div(self, self._to_signal(other))
    def __radd__(self, other):
        from .modules.ops import Add
        return Add(self._to_signal(other), self)
    def __rsub__(self, other):
        from .modules.ops import Sub
        return Sub(self._to_signal(other), self)
    def __rmul__(self, other):
        from .modules.ops import Mul
        return Mul(self._to_signal(other), self)
    def __rtruediv__(self, other):
        from .modules.ops import Div
        return Div(self._to_signal(other), self)
    
     # 비교 연산 오버로드
    def __le__(self, other):  # <=
        from .modules.ops import Le
        return Le(self, self._to_signal(other))

    def __ge__(self, other):  # >=
        from .modules.ops import Ge
        return Ge(self, self._to_signal(other))
