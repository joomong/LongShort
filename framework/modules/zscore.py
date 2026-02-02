import numpy as np
import pandas as pd
from ..signal_base import Signal

class ZScore(Signal):
    tag = "zscore"
    """
    시점 t에서 입력 시그널 x(ts, ds) -> 심볼별 Series 를 받아
    크로스섹션 Z-score 로 변환하여 Series 반환.

    옵션:
      - robust: True 이면 center=median, scale=1.4826*MAD 사용
      - ddof: 표준편차 ddof(기본 1)
      - min_count: 유효 표본 최소 개수 미만이면 0 반환
    """
    def __init__(
        self,
        x: Signal,
        *,
        robust: bool = False,
        ddof: int = 1,
        min_count: int = 2,
        epsilon: float = 1e-12,
    ):
        self.x = x
        self.robust = robust
        self.ddof = ddof
        self.min_count = min_count
        self.eps = epsilon

    def compute(self, ts, ds):
        s = self.x.compute(ts, ds)  # 기대: 심볼 인덱스의 pd.Series
        if not isinstance(s, pd.Series):
            # 입력이 스칼라/단일값이면 그대로 반환(표준화 의미 없음)
            return s

        s = s.astype(float)

        if self.robust:
            center = np.nanmedian(s.values)
            scale  = 1.4826 * np.nanmedian(np.abs(s.values - center))
        else:
            center = np.nanmean(s.values)
            scale  = np.nanstd(s.values, ddof=self.ddof)

        if (s.count() < self.min_count) or (not np.isfinite(scale)) or (scale < self.eps):
            return pd.Series(0.0, index=s.index)

        z = (s - center) / (scale + self.eps)
        z = z.replace([np.inf, -np.inf], np.nan).fillna(0.0)
        return z
