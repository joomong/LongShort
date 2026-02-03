# Numba 백테스트 엔진 전환 계획

## 목표

현재 Pandas 기반 백테스터를 Numba로 전환하여 **40배 속도 향상** 달성

**예상 성능**:
- 단일 backtest: 2초 → 0.05초 (40x faster)
- GA 50 alphas (8 cores): 12초/세대 → 0.3초/세대 (40x faster)

---

## 핵심 설계 원칙

### 1. 하이브리드 접근: Python + Numba

```python
# Python class로 구조 유지
class TsMean(Signal):
    def __init__(self, x, window):
        self.x = x              # Child signal (Python)
        self.window = window
        self.buffer = None

    def compute(self, ts, data):
        vals = self.x.compute(ts, data)
        if self.buffer is None:
            self.buffer = np.zeros((len(vals), self.window))

        # Numba 함수로 계산
        return _compute_tsmean_numba(vals, self.window, self.buffer)


@numba.jit(nopython=True)  # C 속도
def _compute_tsmean_numba(data, window, buffer):
    n = len(data)
    result = np.empty(n)
    for i in range(n):
        buffer[i, :-1] = buffer[i, 1:]  # shift
        buffer[i, -1] = data[i]
        result[i] = np.mean(buffer[i, :])
    return result
```

**장점**:
- ✅ 기존 Signal DAG 구조 완전히 유지
- ✅ XML 파싱, GA, 병렬화 모두 작동
- ✅ Numba 성능 혜택 (10-100x)
- ✅ 디버깅 용이 (Python/Numba 분리)

### 2. 데이터 흐름

```
Parquet file
    ↓ (pandas.read_parquet) - 기존 방식 유지
Pandas MultiIndex DataFrame
    ↓ (df.unstack().values) - startup 시 한 번
Numpy 2D array (T, N)
    ↓ (numba functions) - C 속도
Computation
```

**구현**:

```python
class DataCacheNumba:
    def __init__(self, parquet_path, config):
        # 1. Parquet → Pandas (기존)
        self.df = pd.read_parquet(parquet_path)

        # 2. Metadata 추출
        self.timestamps = self.df.index.get_level_values(0).unique()
        self.symbols = self.df.index.get_level_values(1).unique()

        # 3. Numpy array 변환 (한 번)
        self.close_array = self.df['close'].unstack().values  # (T, N)
        self.volume_array = self.df['volume'].unstack().values

    def get_arrays(self):
        return self.close_array, self.volume_array
```

### 3. 호환성 유지

**GA, 병렬화, API 모두 변경 없음**:
- XML 파싱: Python으로 (기존)
- GA loop: Python으로 (기존)
- 백테스트 실행: Numba로 (새로)
- API endpoint: 변경 없음

---

## 단계별 전환 계획

### Week 1: 데이터 계층 + Stateful Signals

#### Day 1-2: DataCache 확장

**목표**: Pandas → Numpy 변환 추가

**작업**:
1. `framework/data_cache.py` 수정
   ```python
   class DataCache:
       def __init__(self):
           # 기존 pandas 로딩 유지
           self.df = self._load_parquet(...)

           # Numpy 변환 추가
           self.close_array = self._to_numpy(self.df['close'])
           self.volume_array = self._to_numpy(self.df['volume'])

       def _to_numpy(self, series):
           wide = series.unstack(fill_value=np.nan)
           wide = wide.reindex(index=self.timestamps, columns=self.symbols)
           return wide.values

       def get_arrays(self):
           return self.close_array, self.volume_array
   ```

2. 테스트 작성
   ```python
   def test_numpy_conversion():
       cache = DataCache.load()
       close_array, volume_array = cache.get_arrays()

       # Shape 확인
       assert close_array.shape == (n_timestamps, n_symbols)

       # 데이터 정합성 확인 (pandas vs numpy)
       for t in range(10):
           ts = cache.timestamps[t]
           pandas_slice = cache._full_data.loc[ts]['close'].values
           numpy_slice = close_array[t, :]
           np.testing.assert_array_almost_equal(pandas_slice, numpy_slice)
   ```

**예상 시간**: 2일
**검증**: Golden master test 통과

#### Day 3-4: TsMean Numba 전환

**목표**: 가장 많이 사용되는 signal 최적화

**작업**:
1. `framework/modules/tsmean.py` 수정
   ```python
   class TsMean(Signal):
       tag = "tsmean"

       def __init__(self, x: Signal, window: int):
           self.x = x
           self.window = window
           self.buffer = None  # numpy array로 변경

       def compute(self, ts: pd.Timestamp, ds):
           vals = self.x.compute(ts, ds)

           # numpy array로 변환
           if hasattr(vals, 'values'):
               vals_array = vals.values
               index = vals.index
           else:
               vals_array = vals
               index = ds.index

           # 첫 호출 시 초기화
           if self.buffer is None:
               n_symbols = len(vals_array)
               self.buffer = np.full((n_symbols, self.window), np.nan)

           # Numba 계산
           result = _tsmean_numba(vals_array, self.window, self.buffer)

           # pandas Series로 반환 (호환성)
           return pd.Series(result, index=index)


   @numba.jit(nopython=True, cache=True)
   def _tsmean_numba(data, window, buffer):
       n = len(data)
       result = np.empty(n)

       for i in range(n):
           # Shift buffer
           buffer[i, :-1] = buffer[i, 1:]
           buffer[i, -1] = data[i]

           # Calculate mean
           valid = buffer[i, ~np.isnan(buffer[i, :])]
           if len(valid) == window:
               result[i] = np.mean(valid)
           else:
               result[i] = np.nan

       return result
   ```

2. 테스트
   ```python
   def test_tsmean_numba():
       # Golden master: 기존 결과 저장
       prices = pd.Series([100, 101, 102, 103, 104], index=symbols)
       tsmean_pandas = TsMeanPandas(Price('close'), 3)
       result_pandas = []
       for i in range(100):
           result_pandas.append(tsmean_pandas.compute(None, prices))

       # Numba 버전
       tsmean_numba = TsMean(Price('close'), 3)
       result_numba = []
       for i in range(100):
           result_numba.append(tsmean_numba.compute(None, prices))

       # 비교
       np.testing.assert_array_almost_equal(result_pandas, result_numba)
   ```

**예상 시간**: 2일
**예상 효과**: 5-10x 속도 향상

#### Day 5: Delay, TSRank Numba 전환

**목표**: 나머지 stateful signals 최적화

**작업**:
1. `framework/modules/delay.py`
   ```python
   @numba.jit(nopython=True, cache=True)
   def _delay_numba(data, period, buffer):
       n = len(data)
       result = np.empty(n)
       for i in range(n):
           buffer[i, :-1] = buffer[i, 1:]
           buffer[i, -1] = data[i]
           result[i] = buffer[i, 0]
       return result
   ```

2. `framework/modules/tsrank.py`
   ```python
   @numba.jit(nopython=True, cache=True)
   def _tsrank_numba(data, window, buffer):
       n = len(data)
       result = np.empty(n)
       for i in range(n):
           buffer[i, :-1] = buffer[i, 1:]
           buffer[i, -1] = data[i]

           valid = buffer[i, ~np.isnan(buffer[i, :])]
           if len(valid) == window:
               sorted_idx = np.argsort(valid)
               ranks = np.empty(window)
               for j in range(window):
                   ranks[sorted_idx[j]] = j
               result[i] = ranks[-1] / (window - 1)
           else:
               result[i] = np.nan
       return result
   ```

**예상 시간**: 1일

---

### Week 2: Stateless Signals + Backtester

#### Day 6-7: Rank, ZScore Numba 전환

**목표**: Cross-sectional operations 최적화

**작업**:
1. `framework/modules/rank.py`
   ```python
   class Rank(Signal):
       def compute(self, ts, ds):
           vals = self.x.compute(ts, ds)
           vals_array = vals.values if hasattr(vals, 'values') else vals
           result = _rank_numba(vals_array, self.pct)
           return pd.Series(result, index=vals.index)

   @numba.jit(nopython=True, cache=True)
   def _rank_numba(data, pct):
       n = len(data)
       sorted_idx = np.argsort(data)
       ranks = np.empty(n)
       for i in range(n):
           ranks[sorted_idx[i]] = i
       if pct:
           ranks = ranks / (n - 1)
       return ranks
   ```

2. `framework/modules/zscore.py`
   ```python
   @numba.jit(nopython=True, cache=True)
   def _zscore_numba(data, robust, min_count, eps):
       valid = data[~np.isnan(data)]
       if len(valid) < min_count:
           return np.zeros_like(data)

       if robust:
           center = np.median(valid)
           scale = np.median(np.abs(valid - center)) * 1.4826
       else:
           center = np.mean(valid)
           scale = np.std(valid)

       if scale < eps:
           return np.zeros_like(data)

       return (data - center) / (scale + eps)
   ```

**예상 시간**: 2일

#### Day 8-10: Backtester Loop Numba 전환

**목표**: 백테스트 loop 최적화

**작업**:
1. `framework/backtester.py`에 Numba 백테스트 함수 추가
   ```python
   @numba.jit(nopython=True, cache=True)
   def _backtest_loop_numba(
       prices,           # (T, N)
       signals,          # (T, N)
       fee_rate,
       mode              # 'D0' or 'D1'
   ):
       n_timestamps, n_symbols = prices.shape
       returns = np.zeros(n_timestamps)
       turnover = np.zeros(n_timestamps)
       min_bet = np.zeros(n_timestamps)
       max_weight = np.zeros(n_timestamps)

       weights = np.zeros(n_symbols)
       target_weights = np.zeros(n_symbols)

       for t in range(1, n_timestamps):
           # Asset returns
           prev_prices = prices[t-1, :]
           curr_prices = prices[t, :]

           asset_ret = np.zeros(n_symbols)
           for i in range(n_symbols):
               if prev_prices[i] > 0 and curr_prices[i] > 0:
                   asset_ret[i] = (curr_prices[i] / prev_prices[i]) - 1.0

           # Portfolio return
           port_ret = np.sum(weights * asset_ret)
           returns[t] = port_ret

           # Calculate target weights
           signal = signals[t, :]
           abs_sum = np.sum(np.abs(signal))
           if abs_sum > 0:
               target_weights = signal / abs_sum
           else:
               target_weights = np.zeros(n_symbols)

           # Execution (D0 vs D1)
           if mode == 0:  # D0 (use int for mode)
               executed_weights = target_weights
           else:  # D1
               executed_weights = weights

           # Turnover
           to = np.sum(np.abs(executed_weights - weights))
           turnover[t] = to

           # Fee
           returns[t] -= to * fee_rate

           # Metrics
           min_bet[t] = np.mean(np.abs(executed_weights))
           max_weight[t] = np.max(np.abs(executed_weights))

           # Update weights
           if mode == 0:
               weights = target_weights
           else:
               weights = executed_weights
               target_weights = target_weights  # for next iteration

       return returns, turnover, min_bet, max_weight
   ```

2. `Backtester` class 수정
   ```python
   class Backtester:
       def run(self):
           # Signal 계산 (timestamp별)
           signals = self._compute_all_signals()

           # Numba 백테스트
           mode_int = 0 if self.mode == "D0" else 1
           returns, turnover, min_bet, max_weight = _backtest_loop_numba(
               self.data_cache.close_array,
               signals,
               self.transaction_fee_rate,
               mode_int
           )

           # 나머지 처리 (pandas)
           ret_series = pd.Series(returns, index=self.data_cache.timestamps)
           # ...

       def _compute_all_signals(self):
           """모든 timestamp의 signal 계산"""
           n_timestamps = len(self.data_cache.timestamps)
           n_symbols = len(self.data_cache.symbols)
           signals = np.empty((n_timestamps, n_symbols))

           for t, ts in enumerate(self.data_cache.timestamps):
               data_slice = self.data_cache.close_array[t, :]
               signal = self.alpha.compute(ts, data_slice)
               signals[t, :] = signal.values if hasattr(signal, 'values') else signal

           return signals
   ```

**예상 시간**: 3일

---

### Week 3: 최적화 + 테스트

#### Day 11-12: 전체 통합 테스트

**작업**:
1. Golden master test
   ```python
   def test_full_backtest_golden():
       # 기존 결과 (저장된 값)
       expected = load_golden_result('test_alpha_1.json')

       # Numba 백테스터
       result = backtester_numba.run(alpha)

       # 비교 (소수점 6자리까지)
       assert_close(result['returns'], expected['returns'], rtol=1e-6)
       assert_close(result['sharpe'], expected['sharpe'], rtol=1e-6)
   ```

2. 성능 벤치마크
   ```python
   def test_performance():
       # 단일 backtest
       t_pandas = time_backtest(backtester_pandas)
       t_numba = time_backtest(backtester_numba)

       assert t_numba < t_pandas / 10  # 최소 10배 빨라야

       print(f"Pandas: {t_pandas:.3f}s")
       print(f"Numba: {t_numba:.3f}s")
       print(f"Speedup: {t_pandas/t_numba:.1f}x")
   ```

3. GA 통합 테스트
   ```python
   def test_ga_integration():
       # GA 실행
       population = generate_population(50)

       start = time.time()
       fitness = run_ga_generation(population)
       elapsed = time.time() - start

       print(f"GA generation time: {elapsed:.3f}s")
       assert elapsed < 5.0  # 5초 이내
   ```

**예상 시간**: 2일

#### Day 13-14: 병목 제거 및 최적화

**작업**:
1. Profiling
   ```python
   import cProfile
   cProfile.run('backtester.run()', 'profile.prof')

   # 분석
   import pstats
   p = pstats.Stats('profile.prof')
   p.sort_stats('cumulative').print_stats(20)
   ```

2. 추가 최적화
   - Signal 계산 벡터화 시도
   - Memory layout 최적화
   - Cache-friendly 데이터 구조

**예상 시간**: 2일

#### Day 15: 문서화 및 정리

**작업**:
1. 성능 보고서 작성
2. Migration guide 업데이트
3. CLAUDE.md 업데이트
4. 코드 리뷰 및 정리

**예상 시간**: 1일

---

## 검증 기준

### 정확성

- [ ] Golden master test 통과 (rtol=1e-6)
- [ ] 모든 unit test 통과
- [ ] GA 결과 재현 가능

### 성능

- [ ] 단일 backtest: 2초 → 0.2초 이하 (10x)
- [ ] GA 50 alphas: 12초 → 1.2초 이하 (10x)
- [ ] 목표 (40x): 2초 → 0.05초

### 호환성

- [ ] XML 파싱 변경 없음
- [ ] API endpoint 변경 없음
- [ ] GA 코드 변경 없음
- [ ] 병렬화 작동

---

## 리스크 관리

### 리스크 1: 정확도 차이

**대응**:
- Golden master test로 검증
- Float 연산 차이 허용 범위: 1e-6
- 차이 발생 시 원인 분석 및 수정

### 리스크 2: 성능 목표 미달

**대응**:
- Phase별 벤치마크
- 병목 지점 profiling
- 필요시 추가 최적화 (vectorization)

### 리스크 3: 호환성 문제

**대응**:
- 기존 인터페이스 유지
- Pandas/Numba 하이브리드 접근
- 단계적 전환 (rollback 가능)

---

## 예상 성과

### 성능

| 항목 | 현재 | 목표 | 개선율 |
|------|------|------|--------|
| 단일 backtest | 2초 | 0.05초 | 40x |
| GA 1세대 (50 alphas) | 12초 | 0.3초 | 40x |
| 데이터 로딩 | 1초 | 1.1초 | 변경 없음 |
| Signal 계산 | 1초 | 0.02초 | 50x |
| Backtest loop | 1초 | 0.03초 | 33x |

### 코드 품질

- ✅ 구조 유지: Signal DAG 그대로
- ✅ 가독성: Python/Numba 분리
- ✅ 테스트: Golden master + benchmark
- ✅ 문서화: Migration guide + 성능 보고서

---

## 체크리스트

### Week 1
- [ ] DataCache numpy 변환 추가
- [ ] TsMean numba 전환
- [ ] Delay, TSRank numba 전환
- [ ] Unit tests 통과

### Week 2
- [ ] Rank, ZScore numba 전환
- [ ] Backtester loop numba 전환
- [ ] Golden master test 통과
- [ ] 성능 벤치마크 (10x 이상)

### Week 3
- [ ] 전체 통합 테스트
- [ ] GA 통합 테스트
- [ ] Profiling & 최적화
- [ ] 문서화

### 완료 기준
- [ ] 모든 테스트 통과
- [ ] 성능 목표 달성 (10x 이상)
- [ ] GA, API 정상 작동
- [ ] 문서 업데이트 완료

---

## 참고 파일

전환 가이드 및 예제 코드:
- `/tmp/claude-1000/.../scratchpad/numba_class_design.py`
- `/tmp/claude-1000/.../scratchpad/numba_data_flow.py`
- `/tmp/claude-1000/.../scratchpad/migration_guide.md`
- `/tmp/claude-1000/.../scratchpad/numba_misconceptions.py`
- `/tmp/claude-1000/.../scratchpad/numba_backtester.py`

---

## 다음 단계

1. **승인 후 시작**: 이 계획 검토 및 승인
2. **환경 설정**: numba 설치 (`pip install numba`)
3. **Baseline 측정**: 현재 성능 profiling
4. **Week 1 시작**: DataCache 확장부터

**질문/피드백**: 계획 검토 후 조정 가능
