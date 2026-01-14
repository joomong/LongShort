import pandas as pd
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

    def compute(self, ts, ds: pd.DataFrame):
        idx = ds.index
        price = ds[self.price_col].astype(float)

        vol = None
        if self.volume_col and self.volume_col in ds.columns:
            vol = ds[self.volume_col].astype(float)
        else:
            for cand in ["quote_volume", "quoteVolume", "turnover", "value"]:
                if cand in ds.columns:
                    vol = ds[cand].astype(float)
                    break
            if vol is None:
                # 마지막으로 'volume' (base 수량) * price 로 환산
                if "volume" in ds.columns:
                    vol = ds["volume"].astype(float) * price
                else:
                    # 유효 컬럼이 전혀 없으면 0 반환
                    return pd.Series(0.0, index=idx)

        dv = vol.copy()
        dv = dv.fillna(0.0)
        return dv
