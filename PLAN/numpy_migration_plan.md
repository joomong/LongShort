# DataCache + Backtester + Signal Numpy 전환 계획

## 목표

DataCache, Backtester, 전체 Signal 모듈을 numpy 기반으로 전환한다. `_cache` DataFrame은 제거한다.

---

## 설계 결정

### 1. 저장 구조: Per-column `(T, N)` C-contiguous 2D array

```python
self.arrays = {"close": ndarray(T,N), "volume": ndarray(T,N), ...}
self.timestamps = ndarray[datetime64]  # (T,)
self.symbols = ndarray[str]            # (N,)
self.sym_index = {"BTCUSDT": 0, ...}  # O(1) lookup
self._ts_index = {ts_value: 0, ...}   # O(1) timestamp lookup
```

**메모리 레이아웃: C-contiguous (row-major)**
- 주 접근 패턴이 `arr[t, :]` (한 타임스탬프의 전체 심볼) → 행 접근이 contiguous
- Stateful signal은 `(N, window)` 2D circular buffer (deque 대신)
- 열 슬라이싱 `arr[:, n]`은 사용하지 않음

### 2. `_cache` DataFrame 완전 제거

- `load()` 내부에서 merge 후 numpy 변환, merged DataFrame은 GC됨
- `get_slice()`, `get_timestamps()`, `DataCacheIterator` 전부 삭제

### 3. PyArrow→pandas→numpy 유지

- 다중 dataset merge가 pandas join 필요
- load()는 시작 시 1회 → 이 경로 최적화 불필요

### 4. Signal compute 시그니처

```python
# 현재
compute(timestamp: pd.Timestamp, data_slice: pd.DataFrame) → pd.Series

# 변경
compute(t_idx: int, arrays: dict[str, np.ndarray]) → np.ndarray
```
- `t_idx`: 현재 타임스탬프의 정수 인덱스
- `arrays`: `{col: ndarray(N,)}` — 현재 타임스탬프의 1D 슬라이스
- 반환: `np.ndarray(N,)` float64
- sym_index가 필요한 모듈(NormalizeBySymbol, EventSignal)은 `arrays["__sym_index__"]` 대신 **생성자에서 cache.sym_index를 받음**

### 5. Strategy 경계: numpy 유지

- `StrategyLike.generate_weights(raw_signals: np.ndarray) → np.ndarray`
- `SignalRatioStrategy.generate_weights` → numpy 전환
- Backtester 루프 내에서 pd.Series 변환 없음

---

## 수정 파일 목록

### 1. `framework/data.py` — numpy 배열 생성

**`load()` 끝에 numpy 변환 추가:**

```python
# 기존 merge 로직 유지 (PyArrow → pandas DataFrame merge)...
merged = merged.sort_index()

# Numpy 변환
ts_index = merged.index.get_level_values(0).unique().sort_values()
sym_list = sorted(merged.index.get_level_values(1).unique().tolist())

self.timestamps = ts_index.values  # datetime64[ns, UTC] ndarray
self.symbols = np.array(sym_list, dtype=object)
self.sym_index = {s: i for i, s in enumerate(sym_list)}
self._ts_index = {ts: i for i, ts in enumerate(ts_index)}

self.arrays: dict[str, np.ndarray] = {}
for col in merged.columns:
    wide = merged[col].unstack(fill_value=np.nan)
    wide = wide.reindex(index=ts_index, columns=sym_list)
    self.arrays[col] = np.ascontiguousarray(wide.values, dtype=np.float64)  # (T, N)

del merged  # GC
```

**빈 데이터 분기:**

```python
if not frames:
    self.timestamps = np.array([], dtype="datetime64[ns, UTC]")
    self.symbols = np.array([], dtype=object)
    self.sym_index = {}
    self._ts_index = {}
    self.arrays = {}
    return
```

**새 메서드:**

```python
def get_column_array(self, column: str) -> np.ndarray:
    """(T, N) float64. KeyError if missing."""
    return self.arrays[column]

def get_timestamp_index(self, timestamp) -> int:
    """O(1) dict lookup. ts → int index."""
    return self._ts_index[timestamp]

def count_timestamps_in_range(self, start_date: str, end_date: str) -> int:
    """self.timestamps 기반 범위 카운트."""
    _start = pd.Timestamp(start_date, tz="UTC")
    _end = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
    mask = (self.timestamps >= _start) & (self.timestamps <= _end)
    return int(mask.sum())
```

**삭제:** `_cache`, `get_slice()`, `get_timestamps()`, `DataCacheIterator` 클래스 전체

**`__all__` 업데이트:** `DataCacheIterator` 제거

---

### 2. `framework/signal_base.py` — compute 시그니처 변경

```python
import numpy as np

class Signal(ABC):
    @abstractmethod
    def compute(self, t_idx: int, arrays: dict[str, np.ndarray]) -> np.ndarray:
        ...
```

**operator overloading**: 동일하게 유지 (numpy 배열은 +,-,*,/ 자연스럽게 지원)

---

### 3. `framework/modules/` — 전체 Signal 모듈 전환

#### 3-1. Stateless 모듈

**`sources.py` Price:**
```python
def compute(self, t_idx, arrays):
    return arrays[self.column].copy()  # ndarray(N,)
```

**`constant.py` ConstantSignal:**
```python
def compute(self, t_idx, arrays):
    # __symbols__ 키에서 N 추출 (항상 존재, 데이터 컬럼과 동일 길이)
    N = len(arrays["__symbols__"])
    return np.full(N, self.value, dtype=np.float64)
```

**`ops.py` Add/Sub/Mul/Div/Le/Ge:**
```python
class Add(_Binary):
    def compute(self, t_idx, arrays):
        return self.a.compute(t_idx, arrays) + self.b.compute(t_idx, arrays)
# Sub, Mul, Div 동일

def _to_float01(res):
    return res.astype(np.float64)  # bool ndarray → float

class Le(_Binary):
    def compute(self, t_idx, arrays):
        return _to_float01(self.a.compute(t_idx, arrays) <= self.b.compute(t_idx, arrays))
```

**`rank.py` Rank:**
```python
def compute(self, t_idx, arrays):
    v = self.x.compute(t_idx, arrays)  # ndarray(N,)
    finite_mask = np.isfinite(v)
    ranks = np.full_like(v, np.nan)  # NaN 위치 보존 (원본 .rank()와 동일)
    if finite_mask.any():
        valid = v[finite_mask]
        from scipy.stats import rankdata as _rankdata
        r = _rankdata(valid, method='average')
        if self.pct:
            r = r / len(valid)
        ranks[finite_mask] = r
    return ranks  # NaN 보존 (원본 pd.Series.rank()와 동일 — NaN은 NaN으로 유지)
```

**의미 보존:** 원본 `v.rank(pct=True)`는 NaN을 NaN으로 유지. `nan_to_num` 적용하지 않음. 다운스트림에서 NaN 처리 (backtester의 `fillna(0.0)` 등).

**`zscore.py` ZScore:**
```python
def compute(self, t_idx, arrays):
    s = self.x.compute(t_idx, arrays)  # ndarray(N,)
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
    price = arrays[self.price_col]  # ndarray(N,)
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

#### 3-2. Stateful 모듈 — 2D circular buffer 패턴

**공통 패턴 설명:**

모든 stateful 모듈은 `{sym: deque}` 대신 `(N, window)` 2D circular buffer 사용.
- `self.buffer`: `np.ndarray(N, window)` — lazy init (첫 compute에서 N을 알 수 있음)
- `self.buf_pos`: `int` — 글로벌 write position (모든 심볼이 동시에 관측됨)
- `self.fill_count`: `np.ndarray(N,)` int — 심볼별 유효 관측 수 (NaN이 아닌 값의 count)
- NaN 값은 buffer에 기록되지만 fill_count를 증가시키지 않음

**`tsmean.py` TsMean:**
```python
class TsMean(Signal):
    def __init__(self, x, window):
        self.x = x
        self.window = int(window)
        self.buffer = None  # lazy: (N, window) float64
        self.buf_pos = 0

    def compute(self, t_idx, arrays):
        vals = self.x.compute(t_idx, arrays)  # ndarray(N,)
        N = len(vals)
        if self.buffer is None:
            self.buffer = np.full((N, self.window), np.nan, dtype=np.float64)

        col = self.buf_pos % self.window
        self.buffer[:, col] = vals
        self.buf_pos += 1

        out = np.full(N, np.nan, dtype=np.float64)
        # 원본: len(q) == self.window 이후 출력 (deque maxlen 도달 = window번 append 완료)
        # → buf_pos >= self.window 와 동일
        if self.buf_pos >= self.window:
            # 원본: sum(q) / window → NaN 포함 시 NaN 전파. np.mean과 동일.
            out = np.mean(self.buffer, axis=1)  # NaN 전파 유지
        return out
```

**의미 보존:** 원본은 `deque(maxlen=window)` → `len(q) == window` 후 `sum(q)/window`. NaN 값이 deque에 들어가면 `sum()`이 NaN을 반환하므로 NaN 전파. `np.mean`은 동일하게 NaN 전파. readiness 조건은 `buf_pos >= window` (window번 이상 write 완료)으로 원본의 `len(q) == window`와 정확히 일치. `fill_count`는 불필요 (원본이 NaN 여부를 추적하지 않으므로).

**`delay.py` Delay:**
```python
class Delay(Signal):
    def __init__(self, x, period):
        self.x = x
        self.p = int(period)
        self.buffer = None   # lazy: (N, p+1) float64
        self.buf_pos = 0

    def compute(self, t_idx, arrays):
        vals = self.x.compute(t_idx, arrays)  # ndarray(N,)
        N = len(vals)
        if self.buffer is None:
            self.buffer = np.full((N, self.p + 1), np.nan, dtype=np.float64)

        write_col = self.buf_pos % (self.p + 1)
        # 현재 코드: q.append(v) 후 len(q) > self.p이면 q[0] 반환
        # deque(maxlen=p+1)이므로 append 후 len은 최대 p+1
        # q[0]은 가장 오래된 값 = p 스텝 전의 값
        # circular buffer에서: read_col = (write_col + 1) % (p+1) 이 아니라
        #   append 전 가장 오래된 값을 읽어야 함
        # 현재 코드 순서: len(q) > self.p 확인 → q[0] 읽기 → q.append(v)
        # → 즉 append 전에 읽음. numpy에서는:

        out = np.full(N, np.nan, dtype=np.float64)
        if self.buf_pos >= self.p:
            # read: p 스텝 전 값 = 현재 write position에 있는 값 (곧 덮어쓸 위치)
            read_col = write_col  # 이 위치에 p+1 스텝 전의 값이 있음
            out = self.buffer[:, read_col].copy()

        self.buffer[:, write_col] = vals
        self.buf_pos += 1
        return out
```

**검증:** deque(maxlen=3, period=2):
- t=0: append(v0) → len=1 (≤ 2) → NaN. buf=[v0]
- t=1: append(v1) → len=2 (≤ 2) → NaN. buf=[v0,v1]
- t=2: len=2, **but check is `len(q) > self.p`** which is `2 > 2` = False → NaN. append(v2) → len=3. buf=[v0,v1,v2]
- 아, 현재 코드는 **append 후** 체크가 아니라 **append 전** 체크.

현재 코드 정확히:
```python
q = self.buf.setdefault(sym, deque(maxlen=self.p + 1))
if len(q) > self.p:   # check BEFORE append
    out.loc[sym] = q[0]
q.append(float(v))
```

- t=0: len=0 (≤ 2) → NaN. append(v0). buf=[v0]
- t=1: len=1 (≤ 2) → NaN. append(v1). buf=[v0,v1]
- t=2: len=2 (≤ 2) → NaN. append(v2). buf=[v0,v1,v2]
- t=3: len=3 (> 2) → out=q[0]=v0. append(v3). buf=[v1,v2,v3] (deque maxlen 3이므로 v0 삭제)

→ delay=2이면 t=3에서 v0을 반환 = 올바르게 2 스텝 이전 값.

**수정된 numpy 구현:**
```python
def compute(self, t_idx, arrays):
    vals = self.x.compute(t_idx, arrays)
    N = len(vals)
    if self.buffer is None:
        self.buffer = np.full((N, self.p + 1), np.nan, dtype=np.float64)

    write_col = self.buf_pos % (self.p + 1)

    out = np.full(N, np.nan, dtype=np.float64)
    if self.buf_pos > self.p:   # > not >= (matches `len(q) > self.p` before append)
        # 읽을 위치: 가장 오래된 값 = 다음 write 위치의 값
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
        self.finite_count = None  # (N,) 심볼별 finite 관측 수
        self.buf_pos = 0

    def compute(self, t_idx, arrays):
        vals = self.x.compute(t_idx, arrays)
        N = len(vals)
        if self.buffer is None:
            self.buffer = np.full((N, self.window), np.nan, dtype=np.float64)
            self.finite_count = np.zeros(N, dtype=np.int32)

        col = self.buf_pos % self.window
        # finite count 업데이트
        if self.buf_pos >= self.window:
            old_finite = np.isfinite(self.buffer[:, col])
            self.finite_count -= old_finite.astype(np.int32)
        new_finite = np.isfinite(vals)
        self.finite_count += new_finite.astype(np.int32)

        self.buffer[:, col] = vals
        self.buf_pos += 1

        out = np.zeros(N, dtype=np.float64)
        # 최신 값이 finite이고 min_count 이상 충족하는 심볼만 rank 계산
        latest_finite = np.isfinite(vals)
        ready = latest_finite & (self.finite_count >= self.min_count)

        for n in np.where(ready)[0]:
            row = self.buffer[n]
            finite_mask = np.isfinite(row)
            valid = row[finite_mask]
            # 최신 값의 rank: 현재 값이 valid 배열의 마지막이 아닐 수 있으므로
            # 현재 값 = vals[n], valid 배열에서의 rank 계산
            current = vals[n]
            if self.ascending:
                rank = np.sum(valid < current) + 1  # 1-based
                # average tie-break: count equal values
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

**참고:** 심볼별 루프가 남아있지만, 이는 rank 계산이 심볼별 time-series라서 불가피. 향후 numba로 가속 가능.

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
@dataclass
class EventSignal(Signal):
    signal: Signal
    trigger: Signal
    threshold: float
    max_holddays: int
    bet_all: bool = True

    def __post_init__(self):
        if self.max_holddays <= 0:
            raise ValueError("max_holddays must be > 0")
        self._hold = None    # lazy: ndarray(N,) int — 잔여 hold 일수
        self._initialized = False

    def compute(self, t_idx, arrays):
        base = self.signal.compute(t_idx, arrays)   # ndarray(N,)
        trig = self.trigger.compute(t_idx, arrays)   # ndarray(N,)
        N = len(base)

        base = np.nan_to_num(base, nan=0.0)
        trig = np.nan_to_num(trig, nan=0.0)

        if not self._initialized:
            self._hold = np.zeros(N, dtype=np.int32)
            self._initialized = True

        # decrement holds (매 타임스텝마다 1회)
        self._hold = np.maximum(self._hold - 1, 0)

        # trigger 발동된 심볼: hold 리셋
        fired = trig > self.threshold
        self._hold[fired] = self.max_holddays

        if not self.bet_all:
            return base

        # active mask: hold > 0
        mask = self._hold > 0
        return np.where(mask, base, 0.0)
```

**참고:** 현재 코드는 `_last_ts`로 중복 타임스텝 방지. numpy 전환 후 backtester가 `t_idx`로 순서대로 호출하므로 중복 없음 → `_last_ts` 불필요.

**`normalize.py` NormalizeBySymbol:**

sym_index 접근 필요 → **생성자에서 benchmark의 인덱스를 lazy resolve**

```python
class NormalizeBySymbol(Signal):
    def __init__(self, x, benchmark="BTCUSDT", *, mode="diff", epsilon=1e-12,
                 window=None, min_count=None):
        # ... 기존 validation 동일 ...
        self.x = x
        self.benchmark = str(benchmark)
        self.mode = mode
        self.eps = float(epsilon)
        self.window = int(window) if window is not None else None
        self.min_count = int(min_count) if min_count is not None else (self.window or 2)
        self._buf = []        # benchmark history for zscore
        self._bench_idx = None  # lazy resolve

    def compute(self, t_idx, arrays):
        s = self.x.compute(t_idx, arrays)  # ndarray(N,)

        # benchmark index lazy resolve: arrays에 __sym_index__ 전달하는 대신
        # 첫 compute에서 cache.sym_index를 통해 resolve
        # → 실제로는 backtester가 sym_index를 arrays에 포함하지 않음
        # → 대안: NormalizeBySymbol 생성 시 sym_index dict를 받음
        # → 또는: arrays에 특수 키로 전달
        # → 결정: backtester가 arrays["__symbols__"] = cache.symbols 전달
        if self._bench_idx is None:
            symbols = arrays["__symbols__"]  # ndarray of str
            matches = np.where(symbols == self.benchmark)[0]
            if len(matches) == 0:
                self._bench_idx = -1  # benchmark not found
            else:
                self._bench_idx = matches[0]

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
            mean_b = valid.mean()
            std_b = valid.std(ddof=1)
            if (not np.isfinite(std_b)) or (std_b < self.eps):
                return np.zeros_like(s)
            res = (s - mean_b) / (std_b + self.eps)

        return np.where(np.isfinite(res), res, 0.0)
```

**`arrays`에 메타데이터 전달 방식:**

Backtester가 `arrays` dict를 만들 때 `"__symbols__"` 키를 추가:
```python
arrays_slice = {col: arr[t_idx] for col, arr in self.cache.arrays.items()}
arrays_slice["__symbols__"] = self.cache.symbols  # ndarray of str, 참조만
```

이 키는 Price 등에서는 무시되고, NormalizeBySymbol에서만 사용.

---

### 4. `framework/backtester.py` — numpy 기반 루프

#### 4-1. Backtester.__init__ 변경

```python
def __init__(self, cache, alpha_recipe, strategy, capital, ...):
    self.cache = cache        # DataCache (기존 self.loader 대체)
    self.alpha = alpha_recipe
    self.strategy = strategy
    # ... 나머지 동일
```

#### 4-2. `_validate_alpha_output` → numpy 검증

```python
@staticmethod
def _validate_alpha_output(raw, *, t_idx: int, N: int) -> np.ndarray:
    """Validate and coerce alpha output to ndarray(N,)."""
    if isinstance(raw, np.ndarray):
        if raw.shape != (N,):
            raise ValueError(f"Alpha output shape {raw.shape}, expected ({N},) at t_idx={t_idx}")
        s = raw.astype(np.float64)
    elif isinstance(raw, (list, tuple)):
        s = np.array(raw, dtype=np.float64)
        if len(s) != N:
            raise ValueError(f"Alpha output length {len(s)}, expected {N} at t_idx={t_idx}")
    else:
        raise TypeError(f"Alpha.compute() must return ndarray/list/tuple, got {type(raw)} at t_idx={t_idx}")

    if np.any(np.isinf(s)):
        raise ValueError(f"Alpha.compute() returned +/-inf at t_idx={t_idx}")
    return s
```

**참고:** dict 입력과 pd.Series 입력은 더 이상 지원하지 않음 (모든 signal이 ndarray 반환). universe alignment (`.reindex()`)은 고정 심볼 배열이므로 불필요.

#### 4-3. run() 메인 루프

```python
def run(self):
    T = len(self.cache.timestamps)
    N = len(self.cache.symbols)

    ret_arr = np.zeros(T, dtype=np.float64)
    to_arr = np.zeros(T, dtype=np.float64)
    mb_arr = np.zeros(T, dtype=np.float64)
    mw_arr = np.zeros(T, dtype=np.float64)
    lw_arr = np.zeros(T, dtype=np.float64)
    sw_arr = np.zeros(T, dtype=np.float64)
    has_return = np.zeros(T, dtype=bool)  # 첫 바 마킹

    prev_exec_weights = np.zeros(N, dtype=np.float64)
    prev_target_weights = np.zeros(N, dtype=np.float64)
    prev_close = None  # ndarray(N,) or None

    viz_rows = [] if self.collect_viz else None

    for t_idx in range(T):
        arrays_slice = {col: self.cache.arrays[col][t_idx] for col in self.cache.arrays}
        arrays_slice["__symbols__"] = self.cache.symbols

        cur_close = self.cache.arrays["close"][t_idx]  # ndarray(N,)

        # 1) 수익률
        if prev_close is not None:
            invalid = np.isnan(cur_close) | np.isnan(prev_close) | (cur_close <= 0) | (prev_close <= 0)
            asset_ret = np.where(invalid, 0.0, (cur_close / prev_close) - 1.0)
            asset_ret = np.where(np.isfinite(asset_ret), asset_ret, 0.0)
            r_t = float(np.sum(prev_exec_weights * asset_ret))
            ret_arr[t_idx] = r_t
            has_return[t_idx] = True
        else:
            asset_ret = np.zeros(N, dtype=np.float64)

        # 2) signal → weights
        raw = self.alpha.compute(t_idx, arrays_slice)
        raw_validated = self._validate_alpha_output(raw, t_idx=t_idx, N=N)
        raw_viz = raw_validated.copy()  # viz용 (NaN 유지)
        raw_signals = np.nan_to_num(raw_validated, nan=0.0)

        try:
            target_weights = self.strategy.generate_weights(
                raw_signals,
                t_idx=t_idx,
                arrays=arrays_slice,
                prev_close=prev_close,
            )
        except TypeError:
            # 기존 전략 호환: 추가 인자를 받지 않으면 원래 시그니처로 호출
            target_weights = self.strategy.generate_weights(raw_signals)
        target_weights = np.nan_to_num(np.asarray(target_weights, dtype=np.float64), nan=0.0)

        # D0/D1 mode
        exec_weights = target_weights if self.mode == "D0" else prev_target_weights

        # 3) turnover, min_bet, max_weight
        turnover = float(np.sum(np.abs(exec_weights - prev_exec_weights)))
        to_arr[t_idx] = turnover
        if has_return[t_idx]:
            ret_arr[t_idx] -= turnover * self.transaction_fee_rate
        mb_arr[t_idx] = float(np.mean(np.abs(exec_weights))) if N > 0 else 0.0
        mw_arr[t_idx] = float(np.max(np.abs(exec_weights))) if N > 0 else 0.0
        lw_arr[t_idx] = float(np.sum(exec_weights[exec_weights > 0])) if N > 0 else 0.0
        sw_arr[t_idx] = float(np.sum(np.abs(exec_weights[exec_weights < 0]))) if N > 0 else 0.0

        # 4) viz (collect_viz=True일 때만)
        if self.collect_viz:
            # 최종 출력 시 pd.DataFrame으로 변환 (viz는 API 전용)
            viz_rows.append((t_idx, raw_viz, exec_weights, asset_ret, cur_close))

        # 5) 상태 업데이트
        prev_exec_weights = exec_weights.copy()
        prev_target_weights = target_weights.copy()
        prev_close = cur_close.copy()

    # 결과 변환
    timestamps = pd.DatetimeIndex(self.cache.timestamps)
    # 첫 바 (has_return=False)는 제외
    valid_mask = has_return
    ret_series = pd.Series(ret_arr[valid_mask], index=timestamps[valid_mask])
    to_series = pd.Series(to_arr, index=timestamps)
    mb_series = pd.Series(mb_arr, index=timestamps)
    mw_series = pd.Series(mw_arr, index=timestamps)
    lw_series = pd.Series(lw_arr, index=timestamps)
    sw_series = pd.Series(sw_arr, index=timestamps)

    avg_turnover = float(to_series.mean()) if len(to_series) else 0.0

    result = BacktestResult(
        ret_series=ret_series, avg_turnover=avg_turnover,
        initial_capital=self.initial_capital,
        turnover_series=to_series, min_bet_series=mb_series,
        max_weight_series=mw_series, long_weight_series=lw_series,
        short_weight_series=sw_series,
    )

    # Viz bundle
    viz_bundle = None
    if self.collect_viz and viz_rows:
        # viz_rows를 pd.DataFrame으로 변환 (API 호환)
        symbols = self.cache.symbols
        rows = []
        for (ti, raw_v, ew, ar, cc) in viz_rows:
            ts = self.cache.timestamps[ti]
            for n_idx, sym in enumerate(symbols):
                rows.append({
                    'date': pd.Timestamp(ts),
                    'symbol': sym,
                    'open': self.cache.arrays['open'][ti, n_idx] if 'open' in self.cache.arrays else np.nan,
                    'high': self.cache.arrays['high'][ti, n_idx] if 'high' in self.cache.arrays else np.nan,
                    'low': self.cache.arrays['low'][ti, n_idx] if 'low' in self.cache.arrays else np.nan,
                    'close': cc[n_idx],
                    'signal': raw_v[n_idx],
                    'weight': ew[n_idx],
                    'ret': ar[n_idx],
                })
        sym_df = pd.DataFrame(rows).set_index('date')

        r = ret_series.fillna(0.0)
        equity = (1.0 + r).cumprod() * self.initial_capital
        port_df = pd.DataFrame({
            'equity': equity,
            'mdd': (equity / equity.cummax() - 1.0),
            'turnover': to_series.reindex(r.index).fillna(0.0),
            'min_bet': mb_series.reindex(r.index).fillna(0.0),
        })
        viz_bundle = {'symbols': sym_df, 'portfolio': port_df}

    return (result, viz_bundle)
```

#### 4-4. `StrategyLike` / `SignalRatioStrategy` — numpy 전환

```python
class StrategyLike(Protocol):
    def generate_weights(self, raw_signals: np.ndarray, **kwargs) -> np.ndarray: ...

def signal_ratio_weights(raw_signals: np.ndarray) -> np.ndarray:
    signals = np.nan_to_num(raw_signals, nan=0.0)
    total = np.sum(np.abs(signals))
    if total < 1e-12:
        return np.zeros_like(signals)
    return signals / total

class SignalRatioStrategy:
    def generate_weights(self, raw_signals, **kwargs):
        return signal_ratio_weights(raw_signals)
```

#### 4-5. `CombinedAlpha` — numpy 전환

```python
class CombinedAlpha:
    def __init__(self, alphas: list):
        self.alphas = alphas

    def compute(self, t_idx: int, arrays: dict) -> np.ndarray:
        combined = None
        for alpha in self.alphas:
            try:
                s = alpha.compute(t_idx, arrays)
            except Exception:
                continue
            s = np.asarray(s, dtype=np.float64)
            if combined is None:
                combined = s.copy()
            else:
                combined = np.nan_to_num(combined, nan=0.0) + np.nan_to_num(s, nan=0.0)
        if combined is None:
            N = next(iter(arrays.values())).shape[0]
            return np.zeros(N, dtype=np.float64)
        return combined
```

#### 4-6. `run_backtest_with_periods` — DataCacheIterator 제거

```python
def run_backtest_with_periods(alpha, strategy, settings, ..., data_cache=None):
    # DataCacheIterator 대신 cache를 직접 전달
    # start/end 범위 필터링은 Backtester 내부에서 t_idx 범위로 처리

    # 또는: cache에서 sub-range를 정수 인덱스로 지정
    bt = Backtester(data_cache, alpha, strategy, capital, mode=mode,
                    collect_viz=False, transaction_fee_rate=...,
                    start_date=settings.is_start, end_date=settings.os_end)
    full_result, _ = bt.run()
    # split은 동일...
```

**Backtester에 start_date/end_date 파라미터 추가:**
```python
def __init__(self, cache, ..., start_date=None, end_date=None):
    self.start_idx = 0
    self.end_idx = len(cache.timestamps)
    if start_date:
        _start = pd.Timestamp(start_date, tz="UTC")
        self.start_idx = int(np.searchsorted(cache.timestamps, _start))
    if end_date:
        _end = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
        self.end_idx = int(np.searchsorted(cache.timestamps, _end, side='right'))
```

루프: `for t_idx in range(self.start_idx, self.end_idx):`

---

### 5. `framework/parallel.py` — SharedDataManager 최적화

#### 5-1. `SharedDataManager.create` — 3중 루프 → `np.stack`

```python
def create(self, cache) -> SharedDataInfo:
    columns = sorted(cache.arrays.keys())
    instruments = cache.symbols.tolist()
    timestamps = cache.timestamps

    # (T, N, F) 3D array 구성
    arr = np.stack([cache.arrays[col] for col in columns], axis=2)  # (T, N, F)

    total_bytes = max(arr.nbytes, 8)
    self.shm = shared_memory.SharedMemory(create=True, size=total_bytes)
    shared_arr = np.ndarray(arr.shape, dtype=np.float64, buffer=self.shm.buf)
    shared_arr[:] = arr

    return SharedDataInfo(
        shm_name=self.shm.name,
        shape=arr.shape,
        dtype="float64",
        timestamps_iso=[pd.Timestamp(ts).isoformat() for ts in timestamps],
        instruments=instruments,
        columns=columns,
    )
```

#### 5-2. `SharedDataIterator` → numpy 기반 (DataFrame 생성 제거)

**`_backtest_worker`에서 SharedDataIterator 사용 대신 직접 numpy 배열 접근:**

```python
def _backtest_worker(alpha_idx, alpha, strategy, settings_tuple, capital,
                     shared_data_info, mode, result_info, transaction_fee_rate):
    # Shared memory에서 3D array 읽기
    shm = shared_memory.SharedMemory(name=shared_data_info.shm_name)
    try:
        arr = np.ndarray(shared_data_info.shape, dtype=shared_data_info.dtype, buffer=shm.buf)
        # (T, N, F)

        # Lightweight cache-like wrapper for Backtester
        cache_proxy = SharedCacheProxy(
            arr=arr,
            timestamps=shared_data_info.timestamps_iso,
            instruments=shared_data_info.instruments,
            columns=shared_data_info.columns,
        )

        bt = Backtester(cache_proxy, alpha, strategy, capital, mode=mode,
                        collect_viz=False, transaction_fee_rate=transaction_fee_rate,
                        start_date=settings.is_start, end_date=settings.os_end)
        full_result, _ = bt.run()
        # ... split, metrics, write_result_to_shared ...
    finally:
        shm.close()
```

**`SharedCacheProxy`:** DataCache와 동일 인터페이스를 제공하는 경량 래퍼

```python
class SharedCacheProxy:
    """Worker-side: shared memory 3D array를 DataCache 인터페이스로 제공."""
    def __init__(self, arr, timestamps, instruments, columns):
        # arr: (T, N, F) from shared memory
        self.timestamps = np.array([pd.Timestamp(ts) for ts in timestamps])
        self.symbols = np.array(instruments, dtype=object)
        self.sym_index = {s: i for i, s in enumerate(instruments)}

        self.arrays = {}
        for f_idx, col in enumerate(columns):
            self.arrays[col] = arr[:, :, f_idx]  # (T, N) view, no copy
```

---

### 6. API 레이어 수정

#### 6-1. `api/services/backtest_service.py`

```python
# 삭제: from framework.data import DataCache, DataCacheIterator
# 변경: from framework.data import DataCache

def run_single_alpha(compiled_signal, settings, mode, data_cache, compiled_xml=None):
    _validate_data_cache(data_cache)
    # DataCacheIterator 대신 Backtester에 cache + start/end 직접 전달
    bt = Backtester(
        cache=data_cache,
        alpha_recipe=compiled_signal,
        strategy=SignalRatioStrategy(),
        capital=data_cache.initial_capital,
        mode=mode,
        transaction_fee_rate=data_cache.transaction_fee_rate,
        start_date=settings.isStart,
        end_date=settings.osEnd,
    )
    result, viz = bt.run()
    # 이후 로직 동일 (heatmap, histogram, split 등)
```

#### 6-2. `_validate_data_cache` 변경

```python
def _validate_data_cache(data_cache):
    # 기존: data_cache._cache is None 체크
    # 변경: data_cache.arrays 존재 여부 체크
    if not hasattr(data_cache, 'arrays') or not data_cache.arrays:
        raise RuntimeError("DataCache not loaded")
```

#### 6-3. `api/xml_alpha_sources.py`, `api/routers/*.py`

`compile_with_sources`는 Signal 객체를 생성할 뿐이므로 변경 불필요. Signal의 `from_xml()` 클래스메서드도 변경 불필요 (생성자 시그니처는 동일, `compute` 시그니처만 변경).

---

### 7. 설계 노트

#### Dynamic Universe 처리

현재 backtester는 `idx_all = idx_current.union(previous_close.index)`로 심볼 출현/소멸을 추적. numpy 전환 후 `(T, N)` 고정 배열 사용 → 모든 심볼이 모든 타임스탬프에 존재. 소멸된 심볼의 close는 NaN → `invalid` 마스크에서 `np.isnan(cur_close)`로 걸러짐 → 해당 심볼 수익률 0, weight도 NaN → `nan_to_num`으로 0. 따라서 고정 배열에서도 liquidation 효과가 자연스럽게 반영됨.

---

### 8. Numba JIT 가속

#### 8-1. 전략: 하이브리드 Python + Numba

Python class로 Signal DAG 구조를 유지하면서, **내부 계산 로직만 `@numba.jit(nopython=True)` 함수로 분리**:

```python
class TsMean(Signal):
    def compute(self, t_idx, arrays):
        vals = self.x.compute(t_idx, arrays)  # Python: DAG traversal
        # ... buffer init ...
        return _tsmean_kernel(vals, self.buffer, self.buf_pos, self.window)  # Numba: 계산

@numba.jit(nopython=True, cache=True)
def _tsmean_kernel(vals, buffer, buf_pos, window):
    N = len(vals)
    col = buf_pos % window
    buffer[:, col] = vals
    out = np.empty(N)
    if buf_pos + 1 >= window:
        for i in range(N):
            out[i] = np.mean(buffer[i])  # C 속도 루프
    else:
        out[:] = np.nan
    return out
```

#### 8-2. Numba 적용 대상

**Stateful signal kernels** (심볼별 루프가 있는 hot path):

| 모듈 | Numba 함수 | 핵심 연산 |
|------|-----------|----------|
| `tsmean.py` | `_tsmean_kernel` | buffer write + `np.mean` per row |
| `delay.py` | `_delay_kernel` | buffer read/write |
| `tsrank.py` | `_tsrank_kernel` | buffer write + per-symbol rank (for 루프) |
| `tszscore.py` | `_tszscore_kernel` | buffer write + per-symbol mean/std (for 루프) |
| `event.py` | `_event_kernel` | hold decrement + trigger + mask |

**Backtester 루프** — 가장 큰 가속:

```python
@numba.jit(nopython=True, cache=True)
def _backtest_loop(close_2d, signals_2d, weights_prev, fee_rate, mode_int):
    """
    close_2d: (T, N), signals_2d: (T, N) — 미리 계산된 전체 signal
    returns: (T,) ret, (T,) turnover, (T,) min_bet, (T,) max_weight
    """
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
        # asset returns
        asset_ret = np.zeros(N)
        for i in range(N):
            if prev_close[i] > 0 and cur_close[i] > 0:
                asset_ret[i] = (cur_close[i] / prev_close[i]) - 1.0

        ret[t] = 0.0
        for i in range(N):
            ret[t] += prev_weights[i] * asset_ret[i]

        # signal → weights (signal_ratio: abs_sum normalization)
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

#### 8-3. 2단계 실행: Signal → Backtest

Backtester.run()은 2패스로 분리:

```python
def run(self):
    T = len(self.cache.timestamps)
    N = len(self.cache.symbols)

    # Pass 1: Signal DAG 실행 (Python — DAG traversal은 numba 불가)
    signals_2d = np.zeros((T, N), dtype=np.float64)
    for t_idx in range(self.start_idx, self.end_idx):
        arrays_slice = {col: self.cache.arrays[col][t_idx] for col in self.cache.arrays}
        arrays_slice["__symbols__"] = self.cache.symbols
        raw = self.alpha.compute(t_idx, arrays_slice)
        signals_2d[t_idx] = np.nan_to_num(
            self._validate_alpha_output(raw, t_idx=t_idx, N=N), nan=0.0
        )

    # Pass 2: Backtest 루프 (Numba — C 속도)
    close_2d = self.cache.arrays["close"][self.start_idx:self.end_idx]
    sig_2d = signals_2d[self.start_idx:self.end_idx]
    mode_int = 0 if self.mode == "D0" else 1
    ret, to, mb, mw = _backtest_loop(close_2d, sig_2d, ..., self.transaction_fee_rate, mode_int)

    # 결과 변환 (Python)
    ...
```

**이점:**
- Signal DAG (Python class 기반, 동적 dispatch)는 Python에서 실행
- 각 signal 내부의 numpy 계산은 `@numba.jit` kernel로 가속 (10-50x)
- Backtest 루프 전체는 `@numba.jit`으로 C 속도 (30-40x)
- signal 루프 + backtest 루프 합산 40x 목표 달성 가능

#### 8-4. Numba 미적용 모듈

다음은 계산이 단순하여 numba 오버헤드가 이점을 초과:
- `sources.py` Price — 단순 배열 인덱싱
- `constant.py` — `np.full` 한 줄
- `ops.py` — numpy 산술 (이미 C 수준)
- `normalize.py` — 벤치마크 1개 값 추출 + 산술

---

## 실행 순서

**Phase A: numpy 전환 (pandas 제거)**
1. `framework/data.py` — numpy 배열 생성 추가, `_cache`/`get_slice()`/`get_timestamps()`/`DataCacheIterator` 삭제
2. `framework/signal_base.py` — compute 시그니처 변경
3. `framework/modules/*` — 전체 signal 모듈 numpy 전환
4. `framework/backtester.py` — numpy 기반 루프 (2패스 구조: signal 계산 + backtest 루프)
5. `framework/parallel.py` — `SharedDataManager.create` 벡터화, `SharedCacheProxy`
6. `api/services/backtest_service.py` — DataCacheIterator 참조 제거
7. 테스트 수정 및 전체 통과 확인

**Phase B: numba JIT 가속**
8. Stateful signal kernels: `_tsmean_kernel`, `_delay_kernel`, `_tsrank_kernel`, `_tszscore_kernel`, `_event_kernel`
9. `_backtest_loop` numba 함수
10. 성능 벤치마크 및 golden master 정확도 검증

---

## 테스트

**새 테스트 (`tests/unit/test_data_cache_numpy.py`):**
- array shape `(T, N)`, value 정확성, symbol ordering, sym_index
- get_column_array, get_timestamp_index, count_timestamps_in_range
- merged datasets의 NaN handling (outer join 시 missing → NaN)
- 빈 데이터: arrays={}, timestamps=[], symbols=[]

**기존 테스트 수정:**
- `DataCacheIterator`, `cache._cache`, `get_slice()`, `get_timestamps()` 참조 전부 제거
- numpy 인터페이스(`cache.arrays`, `cache.timestamps`, `cache.symbols`)로 변경
- signal 테스트: `compute(ts, df)` → `compute(t_idx, arrays_dict)` 호출로 변경
- backtester 테스트: `Backtester(iterator, ...)` → `Backtester(cache, ...)` 변경
- `_validate_alpha_output` 테스트: ndarray 입력으로 변경
- 전략 테스트: ndarray 입력/출력으로 변경

**수정 대상 파일:**
- `tests/unit/test_data_cache.py`
- `tests/unit/test_data_cache_edge.py`
- `tests/unit/test_data_cache_integration.py`
- `tests/unit/test_backtester.py`
- `tests/unit/test_api_backtest.py`
- `tests/unit/test_api_phase2.py`
- `api/services/backtest_service.py` (DataCacheIterator import 및 사용 제거)
- signal 관련 테스트 파일들

**검증:**
```bash
pytest tests/unit/test_data_cache_numpy.py -v
pytest tests/unit/test_data_cache.py tests/unit/test_data_cache_edge.py -v
pytest tests/unit/ -v
pytest -v
```
