# LongShort API

FastAPI backend used by the Next.js frontend.

## Run

```bash
cd /home/jgo/qv-project/LongShort
. .venv/bin/activate
uvicorn api.main:app --reload --port 8000
```

## Endpoints (current)

- **GET** `/api/symbols`
- **GET** `/api/data-range`
- **POST** `/api/backtest/run`
- **POST** `/api/alphas`
- **GET** `/api/alphas`
- **GET** `/api/alphas/{name}`

> Notes
> - There is **no** `/api/backtest/run-multiple` endpoint.
> - There is **no** `/api/alphas/{name}/signal` endpoint.

## XML input (current)

### Policy
- The XML must describe a **pipeline that starts with `<data .../>`**.
- Multiple top-level tags are allowed (the compiler wraps internally).
- Compiler entrypoint: `api.xml_alpha.compile(xml: str) -> list[CompiledAlpha]`.

### Supported tags (Phase 1–2)
- `<data expr="open|high|low|close|volume" delay="int"/>`
- `<rank/>`
- `<zscore/>`
- `<tszscore days="int|range"/>`
- `<tsrank days="int|range"/>`
- `<tsmean days="int|range"/>`
- `<delay days="int"/>`
- `<signal id="..."/>` (save-only, does not change the signal)
- `<choice> ... </choice>` (children are alternatives)

### Branching/expansion
- Pipe inside attribute values: `expr="close | volume"`
- Integer range: `days="20...10...60"` or `days="3...5"`
- `<choice>` creates alternatives which combine via Cartesian product.

### Example

```xml
<data expr="close" delay="1"/>
<rank/>
<choice>
  <tszscore days="20...10...40"/>
  <tsrank days="10"/>
</choice>
```

## Architecture

```
frontend (Next.js)
  → POST /api/backtest/run
    → api/routers/backtest.py        # 라우터: XML 컴파일 + 분기
      → api/services/backtest_service.py  # 접착제: 엔진 호출 + JSON 가공
        → framework/backtester.py         # 엔진: 시뮬레이션, metrics 계산
        → framework/data_cache.py         # 데이터: parquet 캐시
```

| 레이어 | 파일 | 역할 |
|--------|------|------|
| Router | `api/routers/backtest.py` | XML 컴파일, single/multi 분기 |
| Service | `api/services/backtest_service.py` | 엔진 호출, heatmap/distribution/metrics 가공, JSON 반환 |
| Engine | `framework/backtester.py` | `Backtester`, `PeriodSettings`, IS/OS metrics 계산, `SignalRatioStrategy` |
| Data | `framework/data_cache.py` | parquet 로드 + 캐싱 |

> `framework/strategy.py`는 삭제됨. `SignalRatioStrategy`와 `signal_ratio_weights()`는 `framework/backtester.py`로 이동.

### Signal Tag Registry

새 XML 태그 추가 시 `framework/modules/` 아래에 Signal 서브클래스를 생성하고 `tag` 속성을 정의하면
`Signal.__init_subclass__`를 통해 자동 등록된다. `api/xml_alpha.py` 수정은 불필요.

| 파일 | 역할 |
|------|------|
| `framework/signal_base.py` | `Signal._tag_registry` (태그 자동 등록) |
| `framework/exceptions.py` | `XmlCompileError` (공통 예외) |
| `framework/xml_helpers.py` | `parse_positive_int` (공통 헬퍼) |

### 경로별 동작

- **Single alpha** (`run_single_alpha`): heatmap + distribution + metrics 반환
- **Multiple alphas** (`run_multiple_alphas`): `PeriodSettings`로 IS/OS 분리 실행, 알파별 순위 반환

### IS/OS 분리 방식

- **Single alpha**: 전체 기간(isStart~osEnd)을 한 번에 실행 후, `osStart` 기준으로 ret_series를 split → IS/OS 각각 metrics 계산. heatmap/distribution은 전체 기간 기준.
- **Multiple alphas**: `PeriodSettings`로 IS/OS를 별도 `Backtester`로 실행.

## Visualization 계산 방식 (single alpha)

`backtest_service.py`의 `run_single_alpha` 응답에 포함되는 시각화 데이터 계산 로직.

### Heatmap (`heatmap_from_viz_symbols`)

1. Backtester의 viz bundle에서 심볼별 시그널 DataFrame을 추출
2. `pivot_table(index=timestamp, columns=symbol, values=signal)` — 2D 행렬 생성
3. **Per-symbol z-score 정규화**: 각 심볼(열) 별로 `(값 - 평균) / 표준편차` 적용
   - 심볼 간 스케일 차이를 제거하여 히트맵에서 공정 비교 가능
   - std=0인 심볼은 NaN 처리 → 프론트에서 "데이터 없음"으로 표시
4. NaN/Inf → `null`, 유한값 → `float`로 변환하여 JSON 반환
5. 축: timestamps(시간순 정렬), symbols(알파벳 정렬)

### Signal Distribution (`range_and_hist`)

1. 히트맵 값 행렬을 1D로 flatten
2. NaN/Inf 제거
3. `numpy.histogram(flat, bins=50)` — 50개 구간의 도수 분포 계산
4. `signalRange: {min, max}` + `distribution: int[50]` 반환

### Metrics (`compute_metrics`)

전체 기간 수익률 시계열에서 계산:

| 지표 | 계산 |
|------|------|
| `pnl` | `(equity[-1] / equity[0] - 1) × 100` (%) |
| `sharpe` | `(mean(r) / std(r)) × √365` (연율화) |
| `mdd` | `min(equity / cummax(equity) - 1) × 100` (%) |
| `turnover` | `avg_turnover × 100` (%) |

> `equity = cumprod(1 + r)`, `bars_per_year = 365`

## Request/response models

See `api/models/schemas.py` for the source of truth.

### 주요 변경 이력

- `barInterval`: `Literal['D0', 'D1', 'intraday']` 허용 (`intraday`는 내부적으로 `D0`로 변환됨)
- `ISMetrics` / `OSMetrics`: `total_return` 필드가 `pnl`로 통일됨 (backtester.py, parallel.py, 프론트엔드 모두 동일)
- 빈 DataCache로 백테스트 요청 시 400 에러 반환

## Tests

```bash
python -m pytest tests/ -x --tb=short
```

| 파일 | 대상 |
|------|------|
| `test_api_backtest.py` | API 엔드포인트, resolve_mode, compute_metrics, date validation |
| `test_api_phase2.py` | Phase 2 API (branching/expansion) |
| `test_backtester.py` | Backtester 엔진, IS/OS split, multi-alpha, parallel |
| `test_data_cache.py` | DataCache 로드 + 슬라이싱 |
| `test_data_cache_edge.py` | DataCache edge cases |
| `test_data_cache_integration.py` | DataCache + Backtester 통합 |
| `test_dataloader.py` | DataLoader 파일 읽기 |
| `test_metrics_calculations.py` | metrics 계산 정합성 |
| `test_xml_alpha.py` | XML 컴파일러 파이프라인 + branching |
| `test_xml_alpha_event_compile.py` | Event 태그 컴파일 + 검증 |
| `test_event_signal.py` | EventSignal 모듈 compute 동작 |
| `test_expr_parser.py` | Expression parser (산술, return) |
| `test_reliability_golden.py` | 골든 테스트 (결과 재현성) |
| `test_alpha_detail_view.py` | Alpha detail view (long/short, weights) |
