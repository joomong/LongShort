# Numpy + Numba 통합 마이그레이션 계획

## 목표

DataCache, Backtester, Signal, Strategy 전체를 numpy 기반으로 전환하고, hot path에 Numba JIT를 적용하여 **40x 속도 향상** 달성.

**예상 성능:**

| 항목 | 현재 | Phase A (numpy) | Phase B (numba) |
|------|------|----------------|----------------|
| 단일 backtest | ~2초 | ~0.5초 (4x) | ~0.05초 (40x) |
| GA 1세대 (50 alphas, 8 cores) | ~12초 | ~3초 | ~0.3초 |
| Signal 계산 | ~1초 | ~0.2초 | ~0.02초 |
| Backtest loop | ~1초 | ~0.3초 | ~0.03초 |

---

## 핵심 설계 원칙

### 1. pandas 완전 제거 (데이터 흐름 내부)

pandas는 load() 시 merge 용도와 최종 API 응답 생성에서만 사용. 데이터 흐름 내부(Signal compute, Backtester loop)에서는 numpy만 사용.

### 2. Signal DAG 구조 유지

Python class 기반 DAG traversal 유지. 각 노드 내부 계산만 numpy/numba로 전환. XML 파싱, GA, 병렬화 모두 기존대로 작동.

### 3. 단일/멀티 알파 코드 경로 통합

현재 `run_single_alpha()`와 `run_multiple_alphas()`가 완전히 분리된 코드 경로로 존재하며, IS/OS 분할, metrics 계산, long/short ratio 계산이 각각 중복 구현되어 있다. numpy 전환과 동시에 하나의 통합 경로로 리팩토링한다.

**현재 (중복):**
```
API Router (len(compiled) == 1 ?)
├── run_single_alpha()     ← Backtester 직접 호출 + 수동 IS/OS split + viz
└── run_multiple_alphas()  ← run_backtest() 위임 (framework)
```

**통합 후:**
```
API Router
└── run_backtest(alphas, settings, data_cache, collect_viz=(len==1))
    ├── 각 alpha 개별 실행 → BacktestPeriodResult
    ├── len > 1이면 CombinedAlpha로 combined portfolio 생성
    ├── IS/OS split, metrics, ratio → framework 한 곳에서 처리
    └── collect_viz=True면 heatmap/histogram 생성
```

**통합 대상 로직:**
- IS/OS 분할 (`_split_result_by_date`) — 현재 3곳 → 1곳
- metrics 계산 (`_compute_period_metrics`) — 현재 2곳 → 1곳
- long/short ratio 계산 — 현재 2곳 → 1곳
- 응답 직렬화 — 현재 2가지 패턴 → 1가지

### 4. 2-Phase 접근

- **Phase A**: pandas → numpy 전환 + 단일/멀티 통합 (구조 변경)
- **Phase B**: numpy → numba JIT 가속 (성능 최적화)

Phase A 완료 후 전체 테스트 통과 확인, 그 다음 Phase B 진행.

---

## 설계 결정

### 데이터 저장 구조

```python
# Per-column (T, N) C-contiguous 2D array
self.arrays = {"close": ndarray(T,N), "volume": ndarray(T,N), ...}
self.timestamps = ndarray[datetime64]  # (T,)
self.symbols = ndarray[str]            # (N,)
self.sym_index = {"BTCUSDT": 0, ...}  # O(1) lookup
self._ts_index = {ts_value: 0, ...}   # O(1) timestamp lookup
```

**메모리 레이아웃: C-contiguous (row-major)**
- 주 접근 패턴: `arr[t, :]` (한 타임스탬프의 전체 심볼) → 행 접근이 contiguous
- Stateful signal: `(N, window)` 2D circular buffer (deque 대신)

### Signal compute 시그니처

```python
# 현재
compute(timestamp: pd.Timestamp, data_slice: pd.DataFrame) -> pd.Series

# 변경
compute(t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray
```

- `t_idx`: 현재 타임스탬프의 정수 인덱스
- `arrays`: `{col: ndarray(N,)}` — 현재 타임스탬프의 1D 슬라이스 + `"__symbols__"` 메타데이터
- 반환: `np.ndarray(N,)` float64

### Strategy 경계

```python
StrategyLike.generate_weights(raw_signals: np.ndarray, **kwargs) -> np.ndarray
```

Backtester 루프 내에서 pd.Series 변환 없음.

### PyArrow → pandas → numpy 유지

- 다중 dataset merge가 pandas join 필요
- load()는 시작 시 1회 → 이 경로 최적화 불필요

### Dynamic Universe 처리

`(T, N)` 고정 배열 사용 → 모든 심볼이 모든 타임스탬프에 존재. 소멸된 심볼의 close는 NaN → `invalid` 마스크에서 걸러짐 → 해당 심볼 수익률 0, weight도 NaN → `nan_to_num`으로 0. 고정 배열에서도 liquidation 효과 자연 반영.

---

## Phase A: Numpy 전환

### A-1. `framework/data.py` — numpy 배열 생성, `_cache` 제거

**삭제 대상:** `_cache`, `get_slice()`, `get_timestamps()`, `DataCacheIterator` 클래스 전체, `__all__`에서 `DataCacheIterator` 제거

**`load()` 변경:**

```python
def load(self) -> None:
    frames = []
    for ds_config in self.dataset_configs:
        # ... 기존 PyArrow 로딩 + pandas 변환 유지 ...
        if df.empty:
            raise ValueError(
                f"Dataset '{ds_config.name}' has no data for range "
                f"{self.start_date} ~ {self.end_date}"
            )
        # ... standardize, set_index ...
        frames.append(df)

    # Merge
    merged = frames[0]
    for other in frames[1:]:
        merged = merged.join(other, how="outer")

    # Validate columns
    all_use_cols = {col for d in self.dataset_configs for col in d.use_col}
    missing_cols = [col for col in all_use_cols if col not in merged.columns]
    if missing_cols:
        raise ValueError(f"Missing required columns: {missing_cols}")

    merged = merged.sort_index()

    # === Numpy 변환 ===
    ts_index = merged.index.get_level_values(0).unique().sort_values()
    sym_list = sorted(merged.index.get_level_values(1).unique().tolist())

    self.timestamps = ts_index.values                    # datetime64[ns, UTC]
    self.symbols = np.array(sym_list, dtype=object)      # (N,)
    self.sym_index = {s: i for i, s in enumerate(sym_list)}
    self._ts_index = {ts: i for i, ts in enumerate(ts_index)}

    self.arrays: dict[str, np.ndarray] = {}
    for col in merged.columns:
        wide = merged[col].unstack(fill_value=np.nan)
        wide = wide.reindex(index=ts_index, columns=sym_list)
        self.arrays[col] = np.ascontiguousarray(wide.values, dtype=np.float64)

    del merged  # GC
```

**새 메서드:**

```python
def get_column_array(self, column: str) -> np.ndarray:
    """(T, N) float64. KeyError if missing."""
    return self.arrays[column]

def get_timestamp_index(self, timestamp) -> int:
    """O(1) dict lookup. ts -> int index."""
    return self._ts_index[timestamp]

def count_timestamps_in_range(self, start_date: str, end_date: str) -> int:
    _start = pd.Timestamp(start_date, tz="UTC")
    _end = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
    mask = (self.timestamps >= _start) & (self.timestamps <= _end)
    return int(mask.sum())
```

---

### A-2. `framework/signal_base.py` — compute 시그니처 변경

```python
import numpy as np

class Signal(ABC):
    @abstractmethod
    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        ...
```

operator overloading 동일 유지 (numpy 배열은 +,-,*,/ 지원)

---

### A-3. `framework/modules/` — 전체 Signal 모듈 전환

#### Stateless 모듈

**`sources.py` Price:**
```python
def compute(self, t_idx, arrays):
    return arrays[self.column].copy()
```

**`constant.py` ConstantSignal:**
```python
def compute(self, t_idx, arrays):
    N = len(arrays["__symbols__"])
    return np.full(N, self.value, dtype=np.float64)
```

**`ops.py` Add/Sub/Mul/Div/Le/Ge:**
```python
class Add(_Binary):
    def compute(self, t_idx, arrays):
        return self.a.compute(t_idx, arrays) + self.b.compute(t_idx, arrays)

def _to_float01(res):
    return res.astype(np.float64)

class Le(_Binary):
    def compute(self, t_idx, arrays):
        return _to_float01(self.a.compute(t_idx, arrays) <= self.b.compute(t_idx, arrays))
```

**`rank.py` Rank:**
```python
def compute(self, t_idx, arrays):
    v = self.x.compute(t_idx, arrays)
    finite_mask = np.isfinite(v)
    ranks = np.full_like(v, np.nan)
    if finite_mask.any():
        valid = v[finite_mask]
        from scipy.stats import rankdata as _rankdata
        r = _rankdata(valid, method='average')
        if self.pct:
            r = r / len(valid)
        ranks[finite_mask] = r
    return ranks
```

**`zscore.py` ZScore:**
```python
def compute(self, t_idx, arrays):
    s = self.x.compute(t_idx, arrays)
    finite = np.isfinite(s)
    n_valid = finite.sum()
    if n_valid < self.min_count:
        return np.zeros_like(s)
    if self.robust:
        center = np.nanmedian(s)
        scale = 1.4826 * np.nanmedian(np.abs(s - center))
    else:
        center = np.nanmean(s)
        scale = np.nanstd(s, ddof=self.ddof)
    if (not np.isfinite(scale)) or (scale < self.eps):
        return np.zeros_like(s)
    z = (s - center) / (scale + self.eps)
    return np.where(np.isfinite(z), z, 0.0)
```

**`DollarVolume.py`:**
```python
def compute(self, t_idx, arrays):
    price = arrays[self.price_col]
    vol = None
    if self.volume_col and self.volume_col in arrays:
        vol = arrays[self.volume_col]
    else:
        for cand in ["quote_volume", "quoteVolume", "turnover", "value"]:
            if cand in arrays:
                vol = arrays[cand]
                break
        if vol is None:
            if "volume" in arrays:
                vol = arrays["volume"] * price
            else:
                return np.zeros_like(price)
    return np.nan_to_num(vol, nan=0.0)
```

#### Stateful 모듈 — 2D circular buffer 패턴

공통: `{sym: deque}` → `(N, window)` 2D circular buffer. `self.buf_pos` (글로벌 write position). Lazy init (첫 compute에서 N 결정).

**`tsmean.py` TsMean:**
```python
class TsMean(Signal):
    def __init__(self, x, window):
        self.x = x
        self.window = int(window)
        self.buffer = None  # lazy: (N, window) float64
        self.buf_pos = 0

    def compute(self, t_idx, arrays):
        vals = self.x.compute(t_idx, arrays)
        N = len(vals)
        if self.buffer is None:
            self.buffer = np.full((N, self.window), np.nan, dtype=np.float64)
        col = self.buf_pos % self.window
        self.buffer[:, col] = vals
        self.buf_pos += 1
        if self.buf_pos >= self.window:
            return np.mean(self.buffer, axis=1)  # NaN 전파 유지
        return np.full(N, np.nan, dtype=np.float64)
```

**`delay.py` Delay:**
```python
class Delay(Signal):
    def __init__(self, x, period):
        self.x = x
        self.p = int(period)
        self.buffer = None   # lazy: (N, p+1) float64
        self.buf_pos = 0

    def compute(self, t_idx, arrays):
        vals = self.x.compute(t_idx, arrays)
        N = len(vals)
        if self.buffer is None:
            self.buffer = np.full((N, self.p + 1), np.nan, dtype=np.float64)
        write_col = self.buf_pos % (self.p + 1)
        out = np.full(N, np.nan, dtype=np.float64)
        if self.buf_pos > self.p:   # > not >= (read before append)
            out = self.buffer[:, write_col].copy()
        self.buffer[:, write_col] = vals
        self.buf_pos += 1
        return out
```

**`tsrank.py` TSRank:**
```python
class TSRank(Signal):
    def __init__(self, x, window, *, pct=True, ascending=False, min_count=None):
        self.x = x
        self.window = int(window)
        self.pct = bool(pct)
        self.ascending = bool(ascending)
        self.min_count = int(min_count) if min_count is not None else self.window
        self.buffer = None
        self.finite_count = None
        self.buf_pos = 0

    def compute(self, t_idx, arrays):
        vals = self.x.compute(t_idx, arrays)
        N = len(vals)
        if self.buffer is None:
            self.buffer = np.full((N, self.window), np.nan, dtype=np.float64)
            self.finite_count = np.zeros(N, dtype=np.int32)
        col = self.buf_pos % self.window
        if self.buf_pos >= self.window:
            self.finite_count -= np.isfinite(self.buffer[:, col]).astype(np.int32)
        self.finite_count += np.isfinite(vals).astype(np.int32)
        self.buffer[:, col] = vals
        self.buf_pos += 1
        out = np.zeros(N, dtype=np.float64)
        ready = np.isfinite(vals) & (self.finite_count >= self.min_count)
        for n in np.where(ready)[0]:
            row = self.buffer[n]
            valid = row[np.isfinite(row)]
            current = vals[n]
            if self.ascending:
                rank = np.sum(valid < current) + 1
                n_equal = np.sum(valid == current)
                rank = rank + (n_equal - 1) / 2.0
            else:
                rank = np.sum(valid > current) + 1
                n_equal = np.sum(valid == current)
                rank = rank + (n_equal - 1) / 2.0
            if self.pct:
                rank = rank / len(valid)
            out[n] = rank
        return out
```

**`tszscore.py` TSZScore:**
```python
class TSZScore(Signal):
    def __init__(self, x, window, *, ddof=1, min_count=None, epsilon=1e-12):
        self.x = x
        self.window = int(window)
        self.ddof = int(ddof)
        self.min_count = int(min_count) if min_count is not None else self.window
        self.eps = float(epsilon)
        self.buffer = None
        self.finite_count = None
        self.buf_pos = 0

    def compute(self, t_idx, arrays):
        vals = self.x.compute(t_idx, arrays)
        N = len(vals)
        if self.buffer is None:
            self.buffer = np.full((N, self.window), np.nan, dtype=np.float64)
            self.finite_count = np.zeros(N, dtype=np.int32)
        col = self.buf_pos % self.window
        if self.buf_pos >= self.window:
            self.finite_count -= np.isfinite(self.buffer[:, col]).astype(np.int32)
        self.finite_count += np.isfinite(vals).astype(np.int32)
        self.buffer[:, col] = vals
        self.buf_pos += 1
        out = np.zeros(N, dtype=np.float64)
        ready = np.isfinite(vals) & (self.finite_count >= self.min_count)
        for n in np.where(ready)[0]:
            row = self.buffer[n]
            valid = row[np.isfinite(row)]
            mean = valid.mean()
            std = valid.std(ddof=self.ddof)
            if (not np.isfinite(std)) or (std < self.eps):
                out[n] = 0.0
            else:
                z = (vals[n] - mean) / (std + self.eps)
                out[n] = z if np.isfinite(z) else 0.0
        return out
```

**`event.py` EventSignal:**
```python
def compute(self, t_idx, arrays):
    base = self.signal.compute(t_idx, arrays)
    trig = self.trigger.compute(t_idx, arrays)
    N = len(base)
    base = np.nan_to_num(base, nan=0.0)
    trig = np.nan_to_num(trig, nan=0.0)
    if not self._initialized:
        self._hold = np.zeros(N, dtype=np.int32)
        self._initialized = True
    self._hold = np.maximum(self._hold - 1, 0)
    fired = trig > self.threshold
    self._hold[fired] = self.max_holddays
    if not self.bet_all:
        return base
    mask = self._hold > 0
    return np.where(mask, base, 0.0)
```

**`normalize.py` NormalizeBySymbol:**
```python
def compute(self, t_idx, arrays):
    s = self.x.compute(t_idx, arrays)
    if self._bench_idx is None:
        symbols = arrays["__symbols__"]
        matches = np.where(symbols == self.benchmark)[0]
        self._bench_idx = matches[0] if len(matches) > 0 else -1
    if self._bench_idx == -1:
        return np.zeros_like(s)
    b = float(s[self._bench_idx])
    if not np.isfinite(b):
        return np.zeros_like(s)
    if self.mode == "diff":
        res = s - b
    elif self.mode == "ratio":
        res = (s / (b + self.eps)) - 1.0
    else:  # zscore
        self._buf.append(b)
        if self.window is not None and len(self._buf) > self.window:
            self._buf = self._buf[-self.window:]
        arr = np.asarray(self._buf, dtype=np.float64)
        valid = arr[np.isfinite(arr)]
        if len(valid) < self.min_count:
            return np.zeros_like(s)
        mean_b, std_b = valid.mean(), valid.std(ddof=1)
        if (not np.isfinite(std_b)) or (std_b < self.eps):
            return np.zeros_like(s)
        res = (s - mean_b) / (std_b + self.eps)
    return np.where(np.isfinite(res), res, 0.0)
```

---

### A-4. `framework/backtester.py` — numpy 기반 루프 + 정리

#### 삭제 대상

- **`SignalLike` 타입**: `pd.Series | Mapping[str, float] | list[float] | np.ndarray` — numpy 전환 후 `compute()`는 항상 `np.ndarray` 반환. 타입 alias 불필요, `_validate_alpha_output`도 ndarray만 검증.
- **`TRANSACTION_FEE_RATE = 0.0010` 하드코딩 상수**: config.yaml에서 이미 `DataCache.transaction_fee_rate`로 읽어오고 있음. 하드코딩 기본값은 config과 불일치 위험. 함수 시그니처의 `transaction_fee_rate=TRANSACTION_FEE_RATE` 기본값도 제거하고, 호출부에서 항상 `data_cache.transaction_fee_rate`를 명시적으로 전달.
- **`BacktestResult` dataclass 완전 삭제**: `run()`에서 바로 IS/OS split하여 `BacktestPeriodResult`를 직접 반환. `_split_result_by_date()` 별도 함수도 삭제, `run()` 내부에서 `os_start` 인덱스 기준으로 배열 슬라이싱. 중간 컨테이너 `BacktestResult` 불필요.
  - `summary()` 삭제 (CLI 미사용)
  - `avg_turnover`, `initial_capital` 중복 필드 제거
  - `Backtester.__init__`에 `is_start`, `is_end`, `os_start`, `os_end` 전달 → `run()` 안에서 직접 `PeriodResult` 두 개 생성
- **IS/OS helpers 영역 정리:**
  - `_compute_core_metrics()` → dict 반환 후 `PeriodMetrics`로 다시 대입하는 불필요한 중간 단계. dict 없이 바로 `PeriodMetrics` 반환하도록 통합.
  - `_compute_period_metrics()` → `BacktestResult` 삭제와 함께 제거. `run()` 내부에서 직접 metrics 계산.
  - `_compute_is_metrics` / `_compute_os_metrics` 별칭 → 삭제.
  - `_split_result_by_date()` → `run()` 내부에서 index 슬라이싱으로 대체, 삭제.
  - `metrics_to_api_dict()` → pnl에 cagr을 덮어쓰는 해킹 제거. 프론트엔드에서 필드명을 바로잡거나, API 스키마에서 명확히 `cagr` 필드를 사용. 직렬화 로직은 API 레이어로 이동.
  - `AlphaResult` dataclass → `BacktestPeriodResult`와 필드 거의 동일 + IS/OS 중복. `PeriodResult` 도입 후 `AlphaResult = BacktestPeriodResult + name + rank`로 단순화.
  - `MultipleBacktestResult.to_dict()` → 직렬화는 API 레이어 책임. framework에서 제거, `api/services/`로 이동.
  - IR/Corr 계산 후 frozen PeriodMetrics 전체 필드 수동 복사 (759~769줄) → `PeriodMetrics`에서 `ir`/`corr`을 별도 필드로 분리하거나, metrics 생성 시점에 한번에 주입.
- **`StrategyLike` Protocol / `SignalRatioStrategy` class / `signal_ratio_weights` function**: strategy 구현체가 `SignalRatioStrategy` 하나뿐인데 Protocol + class + function 3겹 래핑. 전부 삭제하고 backtester loop 안에 `signals / abs_sum` 정규화를 인라인. `framework/__init__.py`, API service, GA service, 테스트에서 import 제거.
- **`ISMetrics = PeriodMetrics` / `OSMetrics = PeriodMetrics` 별칭**: 동일한 `PeriodMetrics`의 불필요한 alias. IS/OS 구분은 사용처(변수명)에서 하면 됨. 테스트의 import도 `PeriodMetrics`로 통일.
- **`BacktestPeriodResult` IS/OS 필드 중복**: `is_metrics`, `os_metrics`, `is_ret_series`, `os_ret_series` 등 동일 패턴이 is/os 접두사로 중복. `PeriodResult` 추출 후 `BacktestPeriodResult`가 두 개의 `PeriodResult`를 갖도록 리팩토링. Optional 필드도 제거.

**변경 전:**
```python
@dataclass(frozen=True)
class BacktestPeriodResult:
    is_metrics: PeriodMetrics
    os_metrics: PeriodMetrics
    is_ret_series: pd.Series
    os_ret_series: pd.Series
    is_turnover_series: Optional[pd.Series] = None
    os_turnover_series: Optional[pd.Series] = None
    is_min_bet_series: Optional[pd.Series] = None
    os_min_bet_series: Optional[pd.Series] = None
    is_max_weight_series: Optional[pd.Series] = None
    os_max_weight_series: Optional[pd.Series] = None
    is_long_ratio: float = 0.0
    is_short_ratio: float = 0.0
```

**변경 후:**
```python
@dataclass(frozen=True)
class PeriodResult:
    """단일 기간(IS 또는 OS) 백테스트 결과."""
    metrics: PeriodMetrics
    ret_series: np.ndarray
    turnover_series: np.ndarray
    min_bet_series: np.ndarray
    max_weight_series: np.ndarray
    long_ratio: float = 0.0
    short_ratio: float = 0.0

@dataclass(frozen=True)
class BacktestPeriodResult:
    """IS/OS 분리 백테스트 결과."""
    is_period: PeriodResult
    os_period: PeriodResult
```

#### __init__ 변경

```python
def __init__(self, cache, alpha_recipe, strategy, capital, ...,
             start_date=None, end_date=None):
    self.cache = cache        # DataCache (기존 self.loader 대체)
    self.alpha = alpha_recipe
    self.strategy = strategy
    # start/end 범위를 정수 인덱스로 변환
    self.start_idx = 0
    self.end_idx = len(cache.timestamps)
    if start_date:
        _start = pd.Timestamp(start_date, tz="UTC")
        self.start_idx = int(np.searchsorted(cache.timestamps, _start))
    if end_date:
        _end = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
        self.end_idx = int(np.searchsorted(cache.timestamps, _end, side='right'))
```

#### _validate_alpha_output → numpy

```python
@staticmethod
def _validate_alpha_output(raw, *, t_idx: int, N: int) -> np.ndarray:
    if isinstance(raw, np.ndarray):
        if raw.shape != (N,):
            raise ValueError(f"Alpha output shape {raw.shape}, expected ({N},)")
        s = raw.astype(np.float64)
    elif isinstance(raw, (list, tuple)):
        s = np.array(raw, dtype=np.float64)
        if len(s) != N:
            raise ValueError(f"Alpha output length {len(s)}, expected {N}")
    else:
        raise TypeError(f"Alpha.compute() must return ndarray, got {type(raw)}")
    if np.any(np.isinf(s)):
        raise ValueError(f"Alpha returned +/-inf at t_idx={t_idx}")
    return s
```

#### run() 메인 루프

```python
def run(self):
    T = self.end_idx - self.start_idx
    N = len(self.cache.symbols)

    ret_arr = np.zeros(T, dtype=np.float64)
    to_arr = np.zeros(T, dtype=np.float64)
    mb_arr = np.zeros(T, dtype=np.float64)
    mw_arr = np.zeros(T, dtype=np.float64)
    lw_arr = np.zeros(T, dtype=np.float64)
    sw_arr = np.zeros(T, dtype=np.float64)
    has_return = np.zeros(T, dtype=bool)

    prev_exec_weights = np.zeros(N, dtype=np.float64)
    prev_target_weights = np.zeros(N, dtype=np.float64)
    prev_close = None

    for i, t_idx in enumerate(range(self.start_idx, self.end_idx)):
        arrays_slice = {col: self.cache.arrays[col][t_idx] for col in self.cache.arrays}
        arrays_slice["__symbols__"] = self.cache.symbols
        cur_close = self.cache.arrays["close"][t_idx]

        # 1) 수익률
        if prev_close is not None:
            invalid = np.isnan(cur_close) | np.isnan(prev_close) | (cur_close <= 0) | (prev_close <= 0)
            asset_ret = np.where(invalid, 0.0, (cur_close / prev_close) - 1.0)
            asset_ret = np.where(np.isfinite(asset_ret), asset_ret, 0.0)
            ret_arr[i] = float(np.sum(prev_exec_weights * asset_ret))
            has_return[i] = True

        # 2) signal -> weights
        raw = self.alpha.compute(t_idx, arrays_slice)
        raw_validated = self._validate_alpha_output(raw, t_idx=t_idx, N=N)
        raw_signals = np.nan_to_num(raw_validated, nan=0.0)
        target_weights = self.strategy.generate_weights(raw_signals)
        target_weights = np.nan_to_num(np.asarray(target_weights, dtype=np.float64), nan=0.0)

        exec_weights = target_weights if self.mode == "D0" else prev_target_weights

        # 3) turnover, metrics
        turnover = float(np.sum(np.abs(exec_weights - prev_exec_weights)))
        to_arr[i] = turnover
        if has_return[i]:
            ret_arr[i] -= turnover * self.transaction_fee_rate
        mb_arr[i] = float(np.mean(np.abs(exec_weights)))
        mw_arr[i] = float(np.max(np.abs(exec_weights))) if N > 0 else 0.0
        lw_arr[i] = float(np.sum(exec_weights[exec_weights > 0]))
        sw_arr[i] = float(np.sum(np.abs(exec_weights[exec_weights < 0])))

        # 4) 상태 업데이트
        prev_exec_weights = exec_weights.copy()
        prev_target_weights = target_weights.copy()
        prev_close = cur_close.copy()

    # 결과 변환 (API 경계에서만 pandas)
    timestamps = pd.DatetimeIndex(self.cache.timestamps[self.start_idx:self.end_idx])
    ret_series = pd.Series(ret_arr[has_return], index=timestamps[has_return])
    # ... BacktestResult 구성 ...
```

#### Strategy 인라인 (StrategyLike / SignalRatioStrategy 삭제)

기존 `strategy.generate_weights()` 호출을 backtester loop 내부에 인라인:

```python
# run() 루프 안에서 직접 정규화
raw_signals = np.nan_to_num(raw_validated, nan=0.0)
abs_sum = np.sum(np.abs(raw_signals))
target_weights = raw_signals / abs_sum if abs_sum > 1e-12 else np.zeros_like(raw_signals)
```

삭제: `StrategyLike` Protocol, `SignalRatioStrategy` class, `signal_ratio_weights()` function, `framework/__init__.py` export

#### CombinedAlpha numpy 전환

```python
class CombinedAlpha:
    def compute(self, t_idx: int, arrays: dict) -> np.ndarray:
        combined = None
        for alpha in self.alphas:
            s = np.asarray(alpha.compute(t_idx, arrays), dtype=np.float64)
            if combined is None:
                combined = s.copy()
            else:
                combined = np.nan_to_num(combined, nan=0.0) + np.nan_to_num(s, nan=0.0)
        if combined is None:
            N = len(arrays["__symbols__"])
            return np.zeros(N, dtype=np.float64)
        return combined
```

---

### A-5. `framework/parallel.py` — SharedDataManager + SharedCacheProxy

#### SharedDataManager.create 벡터화

```python
def create(self, cache) -> SharedDataInfo:
    columns = sorted(cache.arrays.keys())
    instruments = cache.symbols.tolist()
    arr = np.stack([cache.arrays[col] for col in columns], axis=2)  # (T, N, F)
    total_bytes = max(arr.nbytes, 8)
    self.shm = shared_memory.SharedMemory(create=True, size=total_bytes)
    shared_arr = np.ndarray(arr.shape, dtype=np.float64, buffer=self.shm.buf)
    shared_arr[:] = arr
    return SharedDataInfo(
        shm_name=self.shm.name, shape=arr.shape, dtype="float64",
        timestamps_iso=[pd.Timestamp(ts).isoformat() for ts in cache.timestamps],
        instruments=instruments, columns=columns,
    )
```

#### SharedCacheProxy (새 클래스)

```python
class SharedCacheProxy:
    """Worker-side: shared memory 3D array를 DataCache 인터페이스로 제공."""
    def __init__(self, arr, timestamps, instruments, columns):
        self.timestamps = np.array([pd.Timestamp(ts) for ts in timestamps])
        self.symbols = np.array(instruments, dtype=object)
        self.sym_index = {s: i for i, s in enumerate(instruments)}
        self.arrays = {}
        for f_idx, col in enumerate(columns):
            self.arrays[col] = arr[:, :, f_idx]  # (T, N) view, no copy
```

Worker에서 `SharedDataIterator` → `SharedCacheProxy` + `Backtester(cache_proxy, ...)` 로 변경.

---

### A-6. API 레이어 수정 — 단일/멀티 통합

#### 현재 구조 (제거 대상)

```python
# api/routers/backtest.py — 현재
if len(compiled) == 1:
    return run_single_alpha(...)   # 70줄, 수동 IS/OS split + viz
return run_multiple_alphas(...)    # 38줄, framework 위임
```

```python
# api/services/backtest_service.py — 현재
def run_single_alpha(compiled_signal, settings, mode, data_cache, compiled_xml):
    # Backtester 직접 호출, viz 수집, 수동 split, 수동 metrics, 수동 ratio
    ...

def run_multiple_alphas(compiled_alphas, compiled_xmls, settings, mode, data_cache):
    # run_backtest() 위임
    ...
```

#### 통합 후 구조

**`api/routers/backtest.py`:**
```python
# 단일/멀티 구분 없이 하나의 함수 호출
return run_backtest(
    alphas=compiled,
    xmls=compiled_xmls,
    settings=req.settings,
    mode=mode,
    data_cache=data_cache,
)
```

**`api/services/backtest_service.py`:**
```python
def run_backtest(alphas, xmls, settings, mode, data_cache):
    """단일/멀티 알파 통합 백테스트 실행."""
    _validate_data_cache(data_cache)
    capital = data_cache.initial_capital
    fee_rate = data_cache.transaction_fee_rate
    is_single = (len(alphas) == 1)

    # 1) 각 alpha 개별 실행
    alpha_results = []
    for i, alpha in enumerate(alphas):
        bt = Backtester(
            cache=data_cache,
            alpha_recipe=alpha,
            strategy=SignalRatioStrategy(),
            capital=capital,
            mode=mode,
            transaction_fee_rate=fee_rate,
            start_date=settings.isStart,
            end_date=settings.osEnd,
            collect_viz=(is_single and i == 0),  # single일 때만 viz 수집
        )
        result, viz = bt.run()
        is_result, os_result = _split_result_by_date(result, settings.osStart)
        alpha_results.append({
            "is_result": is_result,
            "os_result": os_result,
            "viz": viz,
            "xml": xmls[i] if i < len(xmls) else None,
        })

    # 2) 멀티일 때 combined portfolio
    combined_result = None
    if not is_single:
        combined_alpha = CombinedAlpha([a for a in alphas])
        bt = Backtester(
            cache=data_cache,
            alpha_recipe=combined_alpha,
            strategy=SignalRatioStrategy(),
            capital=capital,
            mode=mode,
            transaction_fee_rate=fee_rate,
            start_date=settings.isStart,
            end_date=settings.osEnd,
        )
        result, _ = bt.run()
        is_result, os_result = _split_result_by_date(result, settings.osStart)
        combined_result = {
            "isMetrics": compute_and_serialize_metrics(is_result),
            "osMetrics": compute_and_serialize_metrics(os_result),
            # ... return series, equity 등
        }

    # 3) 응답 조합 (단일이든 멀티든 같은 구조)
    response_alphas = []
    for ar in alpha_results:
        entry = {
            "isMetrics": compute_and_serialize_metrics(ar["is_result"]),
            "osMetrics": compute_and_serialize_metrics(ar["os_result"]),
            "xml": ar["xml"],
            # ... return series, turnover 등
        }
        if ar["viz"]:
            entry["heatmap"] = heatmap_from_viz_symbols(ar["viz"]["symbols"])
            raw_signals = ar["viz"]["symbols"]["signal"].to_numpy(dtype=float)
            entry["signalRange"], entry["distribution"] = range_and_hist(raw_signals)
        response_alphas.append(entry)

    return {
        "combined": combined_result,
        "alphas": response_alphas,
    }
```

**핵심:**
- `run_single_alpha()`, `run_multiple_alphas()` 삭제
- `run_backtest()` 하나로 통합
- IS/OS split, metrics, ratio 계산이 한 곳에서만 실행
- viz는 `collect_viz` 플래그로 제어 (single일 때만)

#### 기타 수정

**`_validate_data_cache`:**
```python
def _validate_data_cache(data_cache):
    if not hasattr(data_cache, 'arrays') or not data_cache.arrays:
        raise RuntimeError("DataCache not loaded")
```

**`api/routers/data.py`:**
- `cache.get_timestamps()` → `cache.timestamps` 로 변경

**변경 불필요:**
- `api/xml_alpha_sources.py` — Signal 객체 생성만 (compute 시그니처 무관)
- `api/routers/` — backtest.py 외 대부분 변경 불필요

---

## Phase B: Numba JIT 가속

Phase A 완료 후 진행. numpy 기반 코드에 `@numba.jit(nopython=True, cache=True)` 적용.

### 전략: 하이브리드 Python + Numba

Signal DAG traversal은 Python, 내부 계산만 Numba kernel로 분리.

```python
class TsMean(Signal):
    def compute(self, t_idx, arrays):
        vals = self.x.compute(t_idx, arrays)  # Python: DAG traversal
        # ... buffer init ...
        return _tsmean_kernel(vals, self.buffer, self.buf_pos, self.window)  # Numba

@numba.jit(nopython=True, cache=True)
def _tsmean_kernel(vals, buffer, buf_pos, window):
    N = len(vals)
    col = buf_pos % window
    buffer[:, col] = vals
    out = np.empty(N)
    if buf_pos + 1 >= window:
        for i in range(N):
            out[i] = np.mean(buffer[i])
    else:
        out[:] = np.nan
    return out
```

### Numba 적용 대상

| 모듈 | Numba 함수 | 핵심 연산 |
|------|-----------|----------|
| `tsmean.py` | `_tsmean_kernel` | buffer write + mean per row |
| `delay.py` | `_delay_kernel` | buffer read/write |
| `tsrank.py` | `_tsrank_kernel` | buffer write + per-symbol rank |
| `tszscore.py` | `_tszscore_kernel` | buffer write + per-symbol mean/std |
| `event.py` | `_event_kernel` | hold decrement + trigger + mask |
| `backtester.py` | `_backtest_loop` | 전체 backtest 루프 (가장 큰 가속) |

### Numba 미적용 (오버헤드 > 이점)

- `sources.py` Price — 단순 배열 인덱싱
- `constant.py` — `np.full` 한 줄
- `ops.py` — numpy 산술 (이미 C 수준)
- `normalize.py` — 벤치마크 1개 값 추출 + 산술

### Backtester 2-Pass 실행

```python
def run(self):
    T = self.end_idx - self.start_idx
    N = len(self.cache.symbols)

    # Pass 1: Signal DAG 실행 (Python)
    signals_2d = np.zeros((T, N), dtype=np.float64)
    for i, t_idx in enumerate(range(self.start_idx, self.end_idx)):
        arrays_slice = {col: self.cache.arrays[col][t_idx] for col in self.cache.arrays}
        arrays_slice["__symbols__"] = self.cache.symbols
        raw = self.alpha.compute(t_idx, arrays_slice)
        signals_2d[i] = np.nan_to_num(
            self._validate_alpha_output(raw, t_idx=t_idx, N=N), nan=0.0)

    # Pass 2: Backtest 루프 (Numba)
    close_2d = self.cache.arrays["close"][self.start_idx:self.end_idx]
    mode_int = 0 if self.mode == "D0" else 1
    ret, to, mb, mw = _backtest_loop(close_2d, signals_2d, self.transaction_fee_rate, mode_int)
```

### `_backtest_loop` Numba 함수

```python
@numba.jit(nopython=True, cache=True)
def _backtest_loop(close_2d, signals_2d, fee_rate, mode_int):
    T, N = close_2d.shape
    ret = np.zeros(T)
    turnover = np.zeros(T)
    min_bet = np.zeros(T)
    max_weight = np.zeros(T)
    prev_weights = np.zeros(N)
    prev_target = np.zeros(N)
    prev_close = close_2d[0]

    for t in range(1, T):
        cur_close = close_2d[t]
        asset_ret = np.zeros(N)
        for i in range(N):
            if prev_close[i] > 0 and cur_close[i] > 0:
                asset_ret[i] = (cur_close[i] / prev_close[i]) - 1.0

        for i in range(N):
            ret[t] += prev_weights[i] * asset_ret[i]

        sig = signals_2d[t]
        abs_sum = 0.0
        for i in range(N):
            abs_sum += abs(sig[i])
        target = np.zeros(N)
        if abs_sum > 1e-12:
            for i in range(N):
                target[i] = sig[i] / abs_sum

        exec_w = target if mode_int == 0 else prev_target

        to = 0.0
        for i in range(N):
            to += abs(exec_w[i] - prev_weights[i])
        turnover[t] = to
        ret[t] -= to * fee_rate

        abs_w_sum = 0.0
        abs_w_max = 0.0
        for i in range(N):
            aw = abs(exec_w[i])
            abs_w_sum += aw
            if aw > abs_w_max:
                abs_w_max = aw
        min_bet[t] = abs_w_sum / N if N > 0 else 0.0
        max_weight[t] = abs_w_max

        prev_weights = exec_w.copy()
        prev_target = target.copy()
        prev_close = cur_close

    return ret, turnover, min_bet, max_weight
```

---

## 실행 순서

### Phase A: numpy 전환 + 단일/멀티 통합

1. `framework/data.py` — numpy 배열 생성, `_cache`/`get_slice()`/`get_timestamps()`/`DataCacheIterator` 삭제
2. `framework/signal_base.py` — compute 시그니처 변경
3. `framework/modules/*` — 전체 signal 모듈 numpy 전환
4. `framework/backtester.py` — numpy 기반 루프, start/end index, `_validate_alpha_output` numpy
5. `framework/parallel.py` — `SharedDataManager.create` 벡터화, `SharedCacheProxy` 추가
6. `api/services/backtest_service.py` — `run_single_alpha()`/`run_multiple_alphas()` 삭제 → `run_backtest()` 통합
7. `api/routers/backtest.py` — 단일/멀티 분기 제거, `run_backtest()` 단일 호출
8. `api/routers/data.py` — `get_timestamps()` → `timestamps` 속성
9. 테스트 수정 및 전체 통과 확인

### Phase B: numba JIT 가속

9. `pip install numba` + 환경 설정
10. Stateful signal kernels: `_tsmean_kernel`, `_delay_kernel`, `_tsrank_kernel`, `_tszscore_kernel`, `_event_kernel`
11. `_backtest_loop` numba 함수 (2-pass 구조)
12. 성능 벤치마크 및 golden master 정확도 검증

---

## 테스트

### 수정 대상 파일

- `tests/unit/test_data_cache.py`
- `tests/unit/test_data_cache_edge.py`
- `tests/unit/test_data_cache_integration.py`
- `tests/unit/test_backtester.py`
- `tests/unit/test_api_backtest.py`
- `tests/unit/test_api_phase2.py`
- `tests/unit/test_alpha_detail_view.py`
- `tests/unit/test_dataloader.py`
- `tests/unit/test_reliability_golden.py`
- Signal 관련 테스트 파일들

### 변경 내용

- `DataCacheIterator`, `cache._cache`, `get_slice()`, `get_timestamps()` 참조 → numpy 인터페이스로
- signal 테스트: `compute(ts, df)` → `compute(t_idx, arrays_dict)`
- backtester 테스트: `Backtester(iterator, ...)` → `Backtester(cache, ...)`
- `_validate_alpha_output` 테스트: ndarray 입력
- 전략 테스트: ndarray 입출력

### 검증 기준

**정확성:**
- Golden master test 통과 (rtol=1e-6)
- 모든 unit test 통과
- GA 결과 재현 가능

**성능 (Phase A):**
- 단일 backtest: 최소 3x 향상

**성능 (Phase B):**
- 단일 backtest: 최소 10x, 목표 40x
- GA 50 alphas: 최소 10x

**호환성:**
- XML 파싱 변경 없음
- API endpoint 변경 없음
- GA 코드 변경 없음 (parallel.py의 인터페이스만 변경)

---

## 리스크 관리

| 리스크 | 심각도 | 완화 |
|--------|--------|------|
| Circular buffer 버그 | 높음 | Delay read-before-append 검증, wraparound 테스트 |
| compute 시그니처 변경 범위 | 높음 | 모든 signal + 호출부 일괄 변경 (부분 변경 불가) |
| Golden master 정확도 차이 | 높음 | rtol=1e-6 허용, 차이 발생 시 원인 분석 |
| Numba 첫 호출 JIT 컴파일 시간 | 중 | `cache=True`로 디스크 캐시, warmup call |
| scipy.stats.rankdata 의존 | 낮음 | Rank 모듈에서만 사용, 이미 설치됨 |
