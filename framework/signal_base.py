# STAR/framework/signal_base.py
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Dict, Optional, Type

import numpy as np

class Signal(ABC):
    name: str = ""
    tag: ClassVar[Optional[str]] = None

    _tag_registry: ClassVar[Dict[str, Type["Signal"]]] = {}

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if cls.tag is not None:
            existing = Signal._tag_registry.get(cls.tag)
            if existing is not None and existing is not cls:
                raise ValueError(
                    f"Duplicate tag registration: '{cls.tag}' "
                    f"by {cls.__name__} and {existing.__name__}"
                )
            Signal._tag_registry[cls.tag] = cls

    @classmethod
    def from_xml(cls, sig: "Signal", attrib: dict, registry: dict) -> "Signal":
        """Create Signal instance from XML tag. Default: simple tags that only take a signal."""
        return cls(sig)

    def reset(self):
        """Reset internal state for re-execution. Traverses the signal DAG."""
        for v in self.__dict__.values():
            if isinstance(v, Signal):
                v.reset()

    @abstractmethod
    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
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
