# LongShort Backtester Performance Optimization Plan

> **v8 — 실제 코드 기반 검토 반영**

---

## Review History
- v1~v6: 초안 및 반복 리뷰
- v7: 레거시 이중 경로 제거, Numba 제거, 네이밍 정리
- **v8 (current): 실제 코드 대조 후 시그니처/파라미터/로직 수정**

## v8 주요 변경 (v7 대비)
1. Delay `__init__` 시그니처: `(x, p=1)` → 실제 코드 `(x, period: int)` 반영
2. TSRank `ascending` 기본값: `True` → 실제 코드 `False` 반영
3. TSZScore `epsilon` 파라미터: `eps=1e-8` → 실제 코드 `epsilon=1e-12` 반영, `min_count` 기본값 `window`
4. 모든 모듈: `super().__init__()` 제거 (실제 코드에 없음)
5. 모든 모듈: 기존 `tag`, `from_xml()`, convenience 함수 보존 명시
6. DataLoader: `self._df` → 실제 속성명 `self._full_data` 수정
7. Backtester turnover: 전체 심볼 기준 → 실제 코드의 union universe(`idx_all`) 기준으로 수정
8. Backtester `previous_close`: Strategy에 전달하는 형태를 기존 코드와 동일하게 수정
9. Phase 0: `run()` type hint만 수정 (실제 반환은 이미 tuple)

---

## 0. 사전 작업 (Phase 0)

### 0.1 Backtester.run() type hint 수정
- 파일: `framework/backtester.py:217`
- 현재: `def run(self) -> BacktestResult:` (실제 반환은 이미 `(result, viz_bundle)` tuple — line 363)
- 수정: `def run(self) -> tuple[BacktestResult, dict | None]:`
- 호출부는 이미 `is_result, _ = is_bt.run()` 패턴 사용 중

---

## 1. 병목 분석

### 메인 루프 (backtester.py:235-327)
```
for timestamp, data_slice in self.loader:            # Line 235
    idx_all = idx_current.union(previous_close.index)  # Line 239
    previous_executed_weights.reindex(idx_all, ...)     # Line 241
    r_t = (prev_exec_w.reindex(...) * asset_ret).sum()  # Line 265
    raw = self.alpha.compute(timestamp, data_slice)      # Line 276
    turnover = (exec_w_all - prev_exec_w).abs().sum()    # Line 304
```

### Stateful 모듈 병목
| 모듈 | 파일:라인 | 오버헤드 |
|------|-----------|---------|
| TSRank | tsrank.py:49-66 | `pd.Series(valid).rank()` per symbol (Line 61) |
| TSZScore | tszscore.py:47-64 | deque→ndarray + `.loc[sym]` per symbol |
| Delay | delay.py:30-35 | `.loc[sym]` + deque append per symbol |
| TsMean | tsmean.py:21-25 | `sum(q)/window` + `.loc[sym]` per symbol |

**공통 병목:** 심볼별 Python for-loop + `.loc[sym]` 단건 Series 쓰기

---

## 2. 접근 방식: Circular Buffer + Vectorized 연산

Signal.compute() 외부 인터페이스(pd.Series 입출력) 유지. 내부 심볼별 deque를 2D circular buffer로 교체하여 배치 처리. 레거시 코드는 유지하지 않고 직접 교체.

---

## 3. Phase 1: Stateful 모듈 Circular Buffer

### 변경 파일 (5개)
- `framework/modules/_circular_buffer.py` (신규)
- `framework/modules/delay.py`
- `framework/modules/tsmean.py`
- `framework/modules/tsrank.py`
- `framework/modules/tszscore.py`

### 3.1 CircularBuffer (_circular_buffer.py)

```python
import numpy as np
import pandas as pd

class CircularBuffer:
    """2D circular buffer (max_symbols × window). 심볼별 독립 카운트."""

    def __init__(self, max_symbols: int, window: int):
        self.buf = np.full((max_symbols, window), np.nan)
        self.ptr = np.zeros(max_symbols, dtype=np.int64)
        self.count = np.zeros(max_symbols, dtype=np.int64)
        self.window = window
        self.sym_to_idx: dict[str, int] = {}
        self._next_idx = 0

    def _ensure_capacity(self, needed: int):
        if needed < self.buf.shape[0]:
            return
        new_size = max(self.buf.shape[0] * 2, needed + 1)
        new_buf = np.full((new_size, self.window), np.nan)
        new_buf[:self.buf.shape[0], :] = self.buf
        self.buf = new_buf
        new_ptr = np.zeros(new_size, dtype=np.int64)
        new_ptr[:len(self.ptr)] = self.ptr
        self.ptr = new_ptr
        new_count = np.zeros(new_size, dtype=np.int64)
        new_count[:len(self.count)] = self.count
        self.count = new_count

    def get_indices(self, symbols: pd.Index) -> np.ndarray:
        result = np.empty(len(symbols), dtype=np.int64)
        for i, s in enumerate(symbols):
            key = str(s)
            if key in self.sym_to_idx:
                result[i] = self.sym_to_idx[key]
            else:
                idx = self._next_idx
                self._ensure_capacity(idx + 1)
                self.sym_to_idx[key] = idx
                self._next_idx += 1
                result[i] = idx
        return result

    def append_batch(self, indices: np.ndarray, values: np.ndarray):
        col_idx = self.ptr[indices] % self.window
        self.buf[indices, col_idx] = values
        self.ptr[indices] += 1
        self.count[indices] = np.minimum(self.count[indices] + 1, self.window)

    def get_ordered_window(self, idx: int) -> np.ndarray:
        """시간순(oldest→newest). concat으로 circular 해소."""
        cnt = int(self.count[idx])
        if cnt == 0:
            return np.array([], dtype=float)
        w = self.window
        if cnt < w:
            return self.buf[idx, :cnt].copy()
        start = int(self.ptr[idx]) % w
        return np.concatenate([self.buf[idx, start:w], self.buf[idx, :start]])
```

**Wraparound 검증 (window=3, data=[10,20,30,40,50]):**
```
after 10: buf=[10,nan,nan] ptr=1 cnt=1 → [10]
after 20: buf=[10,20,nan]  ptr=2 cnt=2 → [10,20]
after 30: buf=[10,20,30]   ptr=3 cnt=3 → start=0 → [10,20,30] ✓
after 40: buf=[40,20,30]   ptr=4 cnt=3 → start=1 → [20,30]+[40]=[20,30,40] ✓
after 50: buf=[40,50,30]   ptr=5 cnt=3 → start=2 → [30]+[40,50]=[30,40,50] ✓
```

### 3.2 Delay (delay.py) — read-before-append 보존

기존 `compute()`를 직접 교체. `tag`, `from_xml()` 보존.

**기존 시그니처:** `__init__(self, x: Signal, period: int)` — 유지
**원본 의미 (period=2):** `[10,20,30,40,50] → [NaN,NaN,NaN,10,20]`
**period=0:** `[10,20,30] → [NaN,10,20]`

```python
import numpy as np
import pandas as pd
from ._circular_buffer import CircularBuffer
from ..signal_base import Signal
from framework.exceptions import XmlCompileError

class Delay(Signal):
    tag = "delay"

    @classmethod
    def from_xml(cls, sig, attrib, registry):
        days_raw = (attrib.get("days") or "").strip()
        if not days_raw:
            raise XmlCompileError("Missing required attribute: days")
        try:
            days = int(days_raw)
        except ValueError as e:
            raise XmlCompileError(f"Invalid days: {days_raw!r}") from e
        return cls(sig, days)

    def __init__(self, x: Signal, period: int):
        self.x, self.p = x, int(period)
        self.cbuf = CircularBuffer(32, self.p + 1)

    def compute(self, ts: pd.Timestamp, ds: pd.DataFrame):
        vals = self.x.compute(ts, ds)
        indices = self.cbuf.get_indices(vals.index)
        values = vals.to_numpy(dtype=float) if isinstance(vals, pd.Series) else np.asarray(vals, dtype=float)
        result = np.full(len(values), np.nan)
        counts = self.cbuf.count[indices]
        # READ FIRST (원본 line 32: len(q)>p 체크는 append 전)
        has_enough = counts > self.p
        if np.any(has_enough):
            mask = has_enough
            oldest_col = (self.cbuf.ptr[indices[mask]] - counts[mask]) % self.cbuf.window
            result[mask] = self.cbuf.buf[indices[mask], oldest_col]
        # THEN APPEND (원본 line 34)
        self.cbuf.append_batch(indices, values)
        return pd.Series(result, index=vals.index)
```

### 3.3 TsMean (tsmean.py)

기존 `tag`, `from_xml()` 보존.

```python
import numpy as np
import pandas as pd
from ._circular_buffer import CircularBuffer
from ..signal_base import Signal
from framework.xml_helpers import parse_positive_int

class TsMean(Signal):
    tag = "tsmean"

    @classmethod
    def from_xml(cls, sig, attrib, registry):
        return cls(sig, parse_positive_int(attrib, "days"))

    def __init__(self, x: Signal, window: int):
        self.x, self.window = x, int(window)
        self.cbuf = CircularBuffer(32, self.window)

    def compute(self, ts: pd.Timestamp, ds: pd.DataFrame):
        vals = self.x.compute(ts, ds)
        indices = self.cbuf.get_indices(vals.index)
        values = vals.to_numpy(dtype=float) if isinstance(vals, pd.Series) else np.asarray(vals, dtype=float)
        self.cbuf.append_batch(indices, values)
        counts = self.cbuf.count[indices]
        result = np.full(len(values), np.nan)
        full = counts == self.window  # 원본 line 24: len(q)==window
        for i in np.where(full)[0]:
            arr = self.cbuf.get_ordered_window(indices[i])
            result[i] = arr.sum() / self.window
        return pd.Series(result, index=vals.index)
```

### 3.4 TSZScore (tszscore.py)

기존 `tag`, `from_xml()`, convenience 함수 `tszscore()` 보존.

**주의:** 기존 파라미터 `epsilon=1e-12` (not 1e-8), `min_count` 기본값 `window` (not 2)

```python
import numpy as np
import pandas as pd
from ._circular_buffer import CircularBuffer
from ..signal_base import Signal
from framework.xml_helpers import parse_positive_int

class TSZScore(Signal):
    tag = "tszscore"

    @classmethod
    def from_xml(cls, sig, attrib, registry):
        return cls(sig, parse_positive_int(attrib, "days"))

    def __init__(
        self,
        x: Signal,
        window: int = 20,
        *,
        ddof: int = 1,
        min_count: int | None = None,
        epsilon: float = 1e-12,
    ):
        if window <= 0:
            raise ValueError("window must be positive")
        self.x = x
        self.window = int(window)
        self.ddof = int(ddof)
        self.min_count = int(min_count) if min_count is not None else self.window
        self.eps = float(epsilon)
        self.cbuf = CircularBuffer(32, self.window)

    def compute(self, ts: pd.Timestamp, ds: pd.DataFrame):
        vals = self.x.compute(ts, ds)
        if not isinstance(vals, pd.Series):
            return vals
        vals = vals.astype(float)
        indices = self.cbuf.get_indices(vals.index)
        values = vals.to_numpy(dtype=float)
        self.cbuf.append_batch(indices, values)
        counts = self.cbuf.count[indices]
        result = np.full(len(values), np.nan)
        sufficient = counts >= self.min_count
        for i in np.where(sufficient)[0]:
            arr = self.cbuf.get_ordered_window(indices[i])
            valid = arr[np.isfinite(arr)]
            if len(valid) < self.min_count:
                continue
            mean = valid.mean()
            std = valid.std(ddof=self.ddof)
            if (not np.isfinite(std)) or (std < self.eps):
                result[i] = 0.0
            else:
                result[i] = (values[i] - mean) / (std + self.eps)
        out = pd.Series(result, index=vals.index)
        return out.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def tszscore(
    x: Signal,
    window: int = 20,
    *,
    ddof: int = 1,
    min_count: int | None = None,
    epsilon: float = 1e-12,
):
    """Convenience wrapper to build a time-series z-score signal."""
    return TSZScore(x, window=window, ddof=ddof, min_count=min_count, epsilon=epsilon)
```

### 3.5 TSRank (tsrank.py) — finite 체크 + O(n log n) rank

기존 `tag`, `from_xml()`, convenience 함수 `tsrank()` 보존.

**주의:** 기존 `ascending=False` (not True), `min_count` 기본값 `window` (not 2)

```python
import numpy as np
import pandas as pd
from ._circular_buffer import CircularBuffer
from ..signal_base import Signal
from framework.xml_helpers import parse_positive_int

class TSRank(Signal):
    tag = "tsrank"

    @classmethod
    def from_xml(cls, sig, attrib, registry):
        return cls(sig, parse_positive_int(attrib, "days"))

    def __init__(
        self,
        x: Signal,
        window: int = 20,
        *,
        pct: bool = True,
        ascending: bool = False,
        min_count: int | None = None,
    ):
        if window <= 0:
            raise ValueError("window must be positive")
        self.x = x
        self.window = int(window)
        self.pct = bool(pct)
        self.ascending = bool(ascending)
        self.min_count = int(min_count) if min_count is not None else self.window
        self.cbuf = CircularBuffer(32, self.window)

    def compute(self, ts: pd.Timestamp, ds: pd.DataFrame):
        vals = self.x.compute(ts, ds)
        if not isinstance(vals, pd.Series):
            return vals
        vals = vals.astype(float)
        indices = self.cbuf.get_indices(vals.index)
        values = vals.to_numpy(dtype=float)
        self.cbuf.append_batch(indices, values)
        counts = self.cbuf.count[indices]
        result = np.full(len(values), np.nan)
        # 원본 line 58: (len(valid)<min_count) or (not isfinite(arr[-1]))
        sufficient = (counts >= self.min_count) & np.isfinite(values)
        for i in np.where(sufficient)[0]:
            arr = self.cbuf.get_ordered_window(indices[i])
            valid = arr[np.isfinite(arr)]
            if len(valid) < self.min_count:
                continue
            target = values[i]
            # O(n log n) argsort rank — replaces pd.Series(valid).rank()
            order = np.argsort(valid if self.ascending else -valid, kind='mergesort')
            ranks = np.empty(len(valid), dtype=float)
            ranks[order] = np.arange(1, len(valid) + 1, dtype=float)
            # average tie-breaking
            sorted_vals = valid[order]
            j = 0
            while j < len(sorted_vals):
                k = j
                while k < len(sorted_vals) - 1 and sorted_vals[k] == sorted_vals[k + 1]:
                    k += 1
                if k > j:
                    avg = ranks[order[j:k + 1]].mean()
                    ranks[order[j:k + 1]] = avg
                j = k + 1
            target_mask = valid == target
            avg_rank = ranks[target_mask].mean()
            result[i] = avg_rank / len(valid) if self.pct else avg_rank
        out = pd.Series(result, index=vals.index)
        return out.replace([np.inf, -np.inf], np.nan).fillna(0.0)


def tsrank(
    x: Signal,
    window: int = 20,
    *,
    pct: bool = True,
    ascending: bool = False,
    min_count: int | None = None,
):
    """Convenience wrapper to build a rolling time-series rank signal."""
    return TSRank(x, window=window, pct=pct, ascending=ascending, min_count=min_count)
```

---

## 4. Phase 2: Backtester 메인 루프 Vectorized 연산

### 변경 파일 (2개)
- `framework/backtester.py`
- `framework/dataloader.py`

### 4.1 DataLoader.get_all_symbols()

**주의:** 실제 속성명은 `self._full_data` (not `self._df`)

```python
def get_all_symbols(self) -> list[str] | None:
    """Full-load mode: sorted unique symbols. Chunk mode: None → pandas fallback."""
    if hasattr(self, '_full_data') and self._full_data is not None:
        return sorted(self._full_data.index.get_level_values('instrument').unique().tolist())
    return None
```

### 4.2 Backtester.run() — 능력 기반 분기

**핵심:**
- `(result, viz_bundle)` tuple 반환 유지
- Signal.compute(), Strategy.generate_weights() pd.Series 인터페이스 유지
- `previous_close`를 기존과 동일하게 이전 바의 `data_slice['close']` 형태로 Strategy에 전달
- vectorized 연산은 포트폴리오 연산(return, turnover, weights)에만 사용
- `get_all_symbols()` 미지원(chunk mode)이면 기존 pandas 경로 사용 — **능력 기반** 분기

**기존 코드와의 차이점 (주의):**
- 기존 turnover는 `idx_all` (현재+이전 바 union) 기준. vectorized 경로는 전체 심볼 배열 기준 → 심볼이 사라지면 해당 위치가 0이 되므로 liquidation 처리는 동일하지만, mean 계산(minbet) 분모가 달라짐
- **해결:** `minbet`은 `idx_all` 크기 기준이 아닌, 실제 non-zero weight 수 기준으로 맞춰야 함

```python
from collections import OrderedDict

def run(self) -> tuple[BacktestResult, dict | None]:
    # ... 기존 초기화 ...

    all_symbols = None
    if hasattr(self.loader, 'get_all_symbols'):
        all_symbols = self.loader.get_all_symbols()
    has_full_universe = all_symbols is not None and len(all_symbols) > 0

    if has_full_universe:
        N = len(all_symbols)
        sym_to_idx = {s: i for i, s in enumerate(all_symbols)}
        prev_exec_w = np.zeros(N)
        prev_target_w = np.zeros(N)
        prev_close_arr = np.full(N, np.nan)
        has_prev = False
        _idx_cache: OrderedDict[tuple, np.ndarray] = OrderedDict()
        _MAX_CACHE = 64

    for timestamp, data_slice in self.loader:
        idx_current = data_slice.index

        if has_full_universe:
            # 인덱스 매핑 (LRU 캐시)
            idx_key = tuple(str(s) for s in idx_current)
            if idx_key in _idx_cache:
                _idx_cache.move_to_end(idx_key)
                cur_idx = _idx_cache[idx_key]
            else:
                cur_idx = np.array([sym_to_idx[str(s)] for s in idx_current])
                _idx_cache[idx_key] = cur_idx
                if len(_idx_cache) > _MAX_CACHE:
                    _idx_cache.popitem(last=False)

            if has_prev:
                cur_close = data_slice["close"].to_numpy(dtype=float)
                prev_c = prev_close_arr[cur_idx]
                invalid = np.isnan(cur_close) | np.isnan(prev_c) | (cur_close<=0) | (prev_c<=0)
                asset_ret_arr = np.where(invalid, 0.0, cur_close / prev_c - 1.0)
                asset_ret_arr = np.where(np.isinf(asset_ret_arr), 0.0, asset_ret_arr)
                r_t = float(np.dot(prev_exec_w[cur_idx], asset_ret_arr))
                ret_history[timestamp] = r_t
            else:
                asset_ret_arr = np.zeros(len(idx_current))

            # Signal (pandas 인터페이스 유지)
            raw = self.alpha.compute(timestamp, data_slice)
            raw_signals_viz = self._validate_alpha_output(raw, timestamp=timestamp, universe=idx_current)
            raw_signals = raw_signals_viz.fillna(0.0)

            # Strategy (기존과 동일하게 previous_close 전달)
            # 기존 코드: previous_close = data_slice['close'] (이전 바의 close Series)
            prev_close_series = pd.Series(prev_close_arr[cur_idx], index=idx_current) if has_prev else None
            try:
                target_weights = self.strategy.generate_weights(
                    raw_signals, timestamp=timestamp, data_slice=data_slice,
                    previous_close=prev_close_series)
            except TypeError:
                target_weights = self.strategy.generate_weights(raw_signals)

            target_w = pd.Series(target_weights, index=idx_current).fillna(0.0)
            target_arr = target_w.to_numpy(dtype=float)
            exec_arr = target_arr if self.mode == "D0" else prev_target_w[cur_idx]

            # Turnover/metrics (vectorized)
            # 기존 코드: executed_weights_all = executed_weights.reindex(idx_all, fill_value=0.0)
            # idx_all = idx_current.union(previous_close.index)
            # → 전체 심볼 배열에서 동일 효과: 이전 바 심볼이 현재 사라지면 exec_full[해당]=0
            exec_full = np.zeros(N)
            exec_full[cur_idx] = exec_arr
            turnover = float(np.abs(exec_full - prev_exec_w).sum())
            turnover_history[timestamp] = turnover
            if timestamp in ret_history:
                ret_history[timestamp] -= turnover * self.transaction_fee_rate

            # minbet: 기존 코드는 executed_weights_all.abs().mean() (idx_all 크기 기준)
            # → 전체 N 기준 mean과 다름. idx_all 크기를 재현해야 함.
            prev_active = np.flatnonzero(~np.isnan(prev_close_arr)) if has_prev else np.array([], dtype=int)
            idx_all_size = len(set(cur_idx.tolist()) | set(prev_active.tolist()))
            minbet_history[timestamp] = float(np.abs(exec_full).sum() / max(idx_all_size, 1))

            max_weight_history[timestamp] = float(np.abs(exec_arr).max()) if len(exec_arr) > 0 else 0.0
            long_weight_history[timestamp] = float(exec_arr[exec_arr > 0].sum()) if len(exec_arr) > 0 else 0.0
            short_weight_history[timestamp] = float(np.abs(exec_arr[exec_arr < 0]).sum()) if len(exec_arr) > 0 else 0.0

            # viz (pandas 유지)
            if self.collect_viz:
                df_row = data_slice[['open','high','low','close']].copy()
                df_row['signal'] = raw_signals_viz
                df_row['weight'] = pd.Series(exec_arr, index=idx_current)
                df_row['ret'] = pd.Series(asset_ret_arr, index=idx_current) if has_prev else 0.0
                df_row['symbol'] = df_row.index
                df_row['date'] = pd.Timestamp(timestamp)
                viz_rows.append(df_row.reset_index(drop=True))

            # State update
            prev_exec_w[:] = 0.0; prev_exec_w[cur_idx] = exec_arr
            prev_target_w[:] = 0.0; prev_target_w[cur_idx] = target_arr
            prev_close_arr[:] = np.nan
            prev_close_arr[cur_idx] = data_slice["close"].to_numpy(dtype=float)
            has_prev = True
        else:
            # 기존 pandas 경로 (chunk mode — get_all_symbols() 미지원)
            ...

    return (result, viz_bundle)
```

---

## 5. 영향 범위

### 변경 파일 (7개)
| 파일 | Phase |
|------|-------|
| `framework/backtester.py` | 0+2 (type hint + vectorized 경로) |
| `framework/dataloader.py` | 2 (get_all_symbols 추가) |
| `framework/modules/_circular_buffer.py` | 1 (신규) |
| `framework/modules/delay.py` | 1 |
| `framework/modules/tsmean.py` | 1 |
| `framework/modules/tsrank.py` | 1 |
| `framework/modules/tszscore.py` | 1 |

### 변경 불필요 (확인 완료)
- `signal_base.py` — compute() 시그니처 불변
- `modules/rank.py`, `zscore.py`, `ops.py`, `sources.py`, `constant.py` — stateless, pd.Series 호환
- `modules/event.py` — 별도 검토
- `parallel.py` — 독립 워커, 투명
- `data_cache.py` — get_all_symbols() 없음 → 기존 pandas 경로
- `xml_helpers.py`, `exceptions.py` — 무관
- `api/*`, `frontend/*` — BacktestResult 형식 불변, 반환 tuple 기존과 동일
- Strategy 구현체 — pd.Series 인터페이스 유지

---

## 6. 검증 전략

### 6.1 단위 테스트

```python
class TestCircularBuffer:
    def test_wraparound(self):
        """window=3, data=[10,20,30,40,50] → 올바른 시간순 반환."""
        ...

    def test_capacity_expansion(self):
        """초기 4 → 1000 심볼 자동 확장."""
        cbuf = CircularBuffer(4, 10)
        for i in range(1000):
            cbuf.get_indices(pd.Index([f"S{i}"]))
        assert cbuf.buf.shape[0] >= 1000

    def test_pickle_roundtrip(self):
        """parallel.py 호환: pickle 직렬화."""
        import pickle
        cbuf = CircularBuffer(32, 10)
        idx = cbuf.get_indices(pd.Index(["A", "B"]))
        cbuf.append_batch(idx, np.array([1.0, 2.0]))
        restored = pickle.loads(pickle.dumps(cbuf))
        np.testing.assert_array_equal(restored.buf, cbuf.buf)

    def test_long_running_memory(self):
        """10K bars 메모리 누수 확인."""
        import tracemalloc
        tracemalloc.start()
        cbuf = CircularBuffer(32, 20)
        idx = cbuf.get_indices(pd.Index(["A","B","C"]))
        for _ in range(10000):
            cbuf.append_batch(idx, np.random.randn(3))
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 1024 * 1024  # < 1MB
        tracemalloc.stop()


class TestDelay:
    @pytest.mark.parametrize("period,inputs,expected", [
        (2, [10,20,30,40,50], [np.nan,np.nan,np.nan,10,20]),
        (2, [10,20,30,40],    [np.nan,np.nan,np.nan,10]),
        (0, [10,20,30],       [np.nan,10,20]),
        (1, [10,20,30],       [np.nan,np.nan,10]),
        (3, [10,20,30],       [np.nan,np.nan,np.nan]),
    ])
    def test_delay_values(self, period, inputs, expected):
        result = run_delay(period, inputs)
        np.testing.assert_array_equal(result, expected)


class TestTSRank:
    @pytest.mark.parametrize("data,asc,expected", [
        ([1,2,2,3], True,  [1.0,2.5,2.5,4.0]),
        ([1,2,2,3], False, [4.0,2.5,2.5,1.0]),
        ([3,1,4,1,5], True, [3.0,1.5,4.0,1.5,5.0]),
        ([1,1,1], True, [2.0,2.0,2.0]),
    ])
    def test_rank_matches_pandas(self, data, asc, expected):
        """argsort 기반 rank가 pd.Series.rank(method='average')와 동일한지 검증."""
        pd_result = pd.Series(data, dtype=float).rank(method='average', ascending=asc).values
        np.testing.assert_allclose(np_result, pd_result)

    def test_finite_check(self):
        """window=[1.0, 2.0, NaN], latest=NaN → skip (NaN 출력)."""
        ...

    def test_nan_in_middle(self):
        """window=[1.0, NaN, 3.0], latest=3.0 → rank among [1.0, 3.0] only."""
        ...

    def test_ascending_false_default(self):
        """기본 ascending=False 동작 확인."""
        ...


class TestBacktester:
    def test_run_returns_tuple(self):
        result = bt.run()
        assert isinstance(result, tuple) and len(result) == 2
        assert isinstance(result[0], BacktestResult)

    @pytest.mark.parametrize("mode", ["D0", "D1"])
    def test_known_values(self, mode):
        """알려진 입력으로 수익률/턴오버 정확성 검증."""
        ...

    def test_strategy_previous_close(self):
        """Strategy에 previous_close가 현재 바 심볼 기준 pd.Series 또는 None으로 전달."""
        class SpyStrategy:
            def generate_weights(self, signals, **kwargs):
                self.last_prev_close = kwargs.get('previous_close')
                return signal_ratio_weights(signals)
        spy = SpyStrategy()
        Backtester(loader, alpha, spy, 1000).run()
        assert spy.last_prev_close is None or isinstance(spy.last_prev_close, pd.Series)

    def test_chunk_mode_fallback(self):
        """DataCacheIterator(get_all_symbols 없음) → 기존 pandas 경로 동작."""
        ...
```

### 6.2 엣지 케이스
| 케이스 | 예상 | 테스트 |
|--------|------|--------|
| 빈 유니버스 | 빈 Series | ✓ |
| 단일 심볼 | rank=1.0, zscore=0.0 | ✓ |
| 전부 NaN | fillna(0.0) | ✓ |
| 심볼 등장/소멸 | 버퍼 유지/신규 | ✓ |
| 심볼 재등장 | 이전 버퍼 유지 | ✓ |
| window=1 | 즉시 출력 | ✓ |
| min_count > window | 항상 NaN | ✓ |
| Circular wraparound | window=3, 100데이터 | ✓ |
| D0/D1 모드 | 정확한 수익률 | ✓ |
| Delay period=0 | 1-bar delay | ✓ |
| str vs int 심볼 | str() 통일 | ✓ |
| NaN in middle of window | 올바른 rank | ✓ |
| chunk mode | pandas fallback | ✓ |

### 6.3 벤치마크

**Go/No-Go:**
| Phase | 최소 속도향상 | 미달 시 |
|-------|-------------|--------|
| 1 | 2x | 프로파일링 후 병목 재분석 |
| 2 | 추가 1.5x | vectorized 경로 재검토 |

메모리: < 1.5x.

---

## 7. 위험 및 완화

| 위험 | 심각도 | 완화 |
|------|--------|------|
| Circular buffer 버그 | 높음 | wraparound 검증 (5값 추적) + 단위 테스트 |
| TSRank tie-breaking | 높음 | pandas rank()와 4패턴 비교 + NaN middle 테스트 |
| Signal.compute() 계약 | 높음 | 항상 pd.Series(index=vals.index) 반환 |
| Backtester 반환 타입 | 높음 | Phase 0에서 type hint 수정 + tuple 테스트 |
| Strategy 인터페이스 | 높음 | previous_close를 현재 바 심볼 기준 pd.Series로 전달 |
| minbet 분모 차이 | 중 | idx_all 크기 재현 로직 + 기존 결과 비교 테스트 |
| Pickle 직렬화 | 중 | roundtrip 테스트 |
| _idx_cache | 중 | LRU eviction (OrderedDict, max=64) |
| 메모리 누수 | 중 | tracemalloc 기반 장기 실행 테스트 |

---

## 8. 의존성

**필수 변경: 없음** (numpy, pandas 기존)
**추가 의존성: 없음**

---

## 9. 구현 순서

```
Phase 0 (사전)
└── backtester.py type hint 수정 (line 217)

Phase 1 (최우선)
├── _circular_buffer.py 생성
├── CircularBuffer 단위 테스트 (wraparound, capacity, pickle, memory)
├── Delay 교체 + 테스트 (period=0,1,2,3)
├── TsMean 교체 + 테스트
├── TSZScore 교체 + 테스트 (epsilon=1e-12, min_count=window)
├── TSRank 교체 + 테스트 (ascending=False, 4 tie패턴 + finite + NaN middle)
├── 기존 전체 테스트 통과
└── 벤치마크 2x 확인

Phase 2
├── DataLoader.get_all_symbols() (self._full_data 사용)
├── Backtester.run() vectorized 경로
│   ├── LRU 캐시
│   ├── previous_close: 현재 바 심볼 기준 pd.Series
│   └── minbet: idx_all 크기 기준 mean
├── D0/D1 + chunk mode fallback + tuple 반환 테스트
└── 벤치마크 추가 1.5x 확인
```
