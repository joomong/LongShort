import numpy as np
from ..signal_base import Signal

class DollarVolume(Signal):
    """
    달러 단위 거래대금 시그널.
    - 우선순위: ['quote_volume','quoteVolume','turnover','value'] 컬럼을 그대로 사용
    - 없으면: 'volume' * price_col (보통 base 수량이므로 종가와 곱해 달러로 환산)
    - price_col: 기본 'close'
    """
    def __init__(self, price_col: str = "close", volume_col: str | None = None):
        self.price_col = price_col
        self.volume_col = volume_col

    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        N = arrays["__n_symbols__"]
        price = arrays[self.price_col][t_idx].copy()

        vol = None
        if self.volume_col and self.volume_col in arrays:
            vol = arrays[self.volume_col][t_idx].copy()
        else:
            for cand in ["quote_volume", "quoteVolume", "turnover", "value"]:
                if cand in arrays:
                    vol = arrays[cand][t_idx].copy()
                    break
            if vol is None:
                if "volume" in arrays:
                    vol = arrays["volume"][t_idx] * price
                else:
                    return np.full(N, np.nan, dtype=np.float64)

        return np.where(np.isfinite(vol), vol, np.nan)
