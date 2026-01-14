import pandas as pd
import numpy as np
from abc import ABC, abstractmethod

class StrategyBase(ABC):
    """모든 전략 클래스가 따라야 할 기본 설계도."""
    @abstractmethod
    def generate_weights(self, raw_signals: pd.Series) -> pd.Series:
        """
        원석(raw signal)을 받아 최종 투자 비중(weights)으로 변환합니다.
        """
        pass

class SignalRatioStrategy(StrategyBase):
    """
    시그널의 절대값 비율에 따라 자본을 배분하는 전략.
    - 롱/숏 그룹의 크기는 각 그룹의 시그널 총합 비율로 결정됩니다.
    - 그룹 내 자산의 크기는 해당 그룹 내 시그널 비율로 결정됩니다.
    """
    def generate_weights(self, raw_signals: pd.Series) -> pd.Series:
        signals = raw_signals.fillna(0)
        
        # 모든 시그널의 절대값 총합을 계산
        total_strength = signals.abs().sum()
        
        # 만약 모든 시그널이 0이면 투자하지 않음
        if total_strength == 0:
            return pd.Series(0.0, index=signals.index)
            
        # 각 시그널을 절대값 총합으로 나누어 최종 투자 비중(weights)을 계산
        weights = signals / total_strength
        
        return weights


class LeveragedSignalStrategy(StrategyBase):
    """
    Alpha가 생성한 시그널 값을 총자본 대비 목표 레버리지(투자 비중)로
    직접 사용하는 가장 간단한 전략.
    """
    def generate_weights(self, raw_signals: pd.Series) -> pd.Series:
        """
        이 전략에서는 raw_signals가 이미 목표 비중이므로,
        결측치를 처리하고 그대로 반환합니다.
        """
        weights = raw_signals.fillna(0)
        return weights


class EqualWeightActiveStrategy(StrategyBase):
    """
    - active 규칙: |signal| >= thr 면 활성
    - 활성 K개면 각 종목 비중 = sign(signal) * min(1/K, w_cap)
    - gross target은 자동으로 1에 가깝게 맞지만, per-name 캡으로 조금 낮아질 수 있음
    - K < K_min 이면 gross를 (K / K_min)로 축소(바스켓 최소 보호)
    """
    def __init__(self, thr: float = 1e-12, K_min: int = 10, w_cap: float = 0.10):
        self.thr = float(thr)
        self.K_min = int(K_min)
        self.w_cap = float(w_cap)

    def generate_weights(self, raw_signals: pd.Series) -> pd.Series:
        s = pd.Series(raw_signals).astype(float).fillna(0.0)
        active = s.abs() >= self.thr
        K = int(active.sum())

        if K == 0:
            return s.mul(0.0)

        # 1/K 균등 배분 (부호는 시그널 부호를 따름)
        base = 1.0 / K
        w = s.where(active, 0.0)
        w = w.apply(lambda x: (1 if x > 0 else (-1 if x < 0 else 0)) * base)

        # per-name 캡 적용
        w = w.clip(lower=-self.w_cap, upper=self.w_cap)

        # gross 계산
        gross = w.abs().sum()

        # 바스켓 최소 보호: K < K_min이면 총 레버리지를 K/K_min로 줄임
        # (gross가 원래 1 근처라면 최종 gross ≈ min(1, K/K_min))
        if K < self.K_min:
            scale = K / self.K_min
            w = w * scale

        # 혹시 gross가 0이 되면 모두 0
        if w.abs().sum() <= 1e-15:
            return w.mul(0.0)

        return w

class KellyMultiAssetStrategy:
    """
    포트폴리오 켈리:
      - μ, Σ를 롤링 EMA로 추정
      - w* ∝ (Σ + λI)^(-1) μ
      - 분수 켈리 k, gross=1 정규화, 개별 캡 적용
      - 신호(raw_signals) != 0 인 자산만 대상으로 가중 배분(마스크)
    """
    def __init__(self,
                 decay: float = 0.94,     # EMA 감쇠 (0.9~0.99)
                 ridge: float = 1e-6,     # Σ 안정화 λ
                 frac_kelly: float = 0.5, # 분수 켈리 k
                 per_name_cap: float = 0.3,  # |w_i| ≤ cap
                 smooth_alpha: float = 0.0   # w_t = α w*_t + (1-α) w_{t-1}
                 ):
        self.decay = float(decay)
        self.ridge = float(ridge)
        self.frac_kelly = float(frac_kelly)
        self.per_name_cap = float(per_name_cap)
        self.smooth_alpha = float(smooth_alpha)

        self.names_: list[str] = []
        self.mu_: pd.Series | None = None          # EMA(mean)
        self.cov_: pd.DataFrame | None = None      # EMA(cov)
        self.prev_w_: pd.Series | None = None      # for smoothing

    def _ensure_state(self, names: pd.Index):
        if self.mu_ is None or set(self.names_) != set(names):
            self.names_ = list(names)
            z = pd.Series(0.0, index=names)
            self.mu_  = z.copy()
            self.cov_ = pd.DataFrame(np.eye(len(names))*1e-8, index=names, columns=names)
            self.prev_w_ = z.copy()

    def _update_moments(self, r: pd.Series):
        # r: 실현 수익률 벡터 (index = names)
        d = self.decay
        # mean
        self.mu_ = d*self.mu_ + (1-d)*r
        # cov (EMA of second moment)
        # cov_t = d*cov_{t-1} + (1-d) * (r - mu_{t-1})(r - mu_{t-1})^T  (standard EMA approximation)
        diff = (r - self.mu_)
        outer = np.outer(diff.values, diff.values)
        cov_new = d*self.cov_.values + (1-d)*outer
        self.cov_ = pd.DataFrame(cov_new, index=self.cov_.index, columns=self.cov_.columns)

    def generate_weights(self, raw_signals: pd.Series,
                         *, timestamp=None, data_slice: pd.DataFrame | None = None,
                         previous_close: pd.Series | None = None) -> pd.Series:
        """
        Backtester가 kwargs로 data_slice, previous_close를 넘겨주면 사용.
        - previous_close가 없으면 직전 수익 갱신을 생략(첫 바 등)
        - raw_signals는 마스크/방향만 사용(0이면 대상 제외)
        """
        names = raw_signals.index
        self._ensure_state(names)

        # 직전→현재 close 기반 실현 수익률
        if (data_slice is not None) and (previous_close is not None):
            c_prev = previous_close.reindex(names).astype(float)
            c_now  = data_slice['close'].reindex(names).astype(float)
            r = (c_now / c_prev) - 1.0
            r = r.fillna(0.0)
            self._update_moments(r)

        # (Σ + λI)^{-1} μ
        mu = self.mu_.reindex(names).fillna(0.0).values
        cov = self.cov_.reindex(index=names, columns=names).values
        cov.flat[::len(names)+1] += self.ridge  # ridge to diagonal

        try:
            inv = np.linalg.pinv(cov, rcond=1e-8)
        except Exception:
            inv = np.eye(len(names))

        w_star = inv @ mu  # unconstrained Kelly direction
        if np.allclose(w_star, 0):
            w_star = np.zeros_like(mu)

        # 신호 마스크 & 방향성(부호) 반영: signal=0 → weight=0
        sig = raw_signals.fillna(0.0).values
        mask = (np.abs(sig) > 0)
        w_star = np.where(mask, w_star, 0.0)

        # 분수 켈리 스케일
        w_star = self.frac_kelly * w_star

        # 개별 캡
        cap = float(self.per_name_cap)
        if cap > 0:
            w_star = np.clip(w_star, -cap, +cap)

        # gross=1 정규화 (non-zero만)
        gross = np.sum(np.abs(w_star))
        if gross > 0:
            w_star = w_star / gross

        w = pd.Series(w_star, index=names)

        # 스무딩
        if self.smooth_alpha > 0:
            w = self.smooth_alpha*w + (1.0 - self.smooth_alpha)*self.prev_w_.reindex(names).fillna(0.0)

        self.prev_w_ = w.copy()
        return w