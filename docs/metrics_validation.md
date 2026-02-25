# Backtest Metrics Validation

## 목적
`LongShort` 백테스트 메트릭 계산을 구현 내부값 비교가 아닌, 독립 계산기(oracle) 기반의 수학적 정답 검증으로 고정한다.

## 계산 정의
기준 코드는 다음 파일을 따른다.

- `LongShort/framework/backtester.py`
- `LongShort/framework/parallel.py`
- `LongShort/api/services/backtest_service.py`
- `LongShort/framework/data.py`

### 수익률

포트폴리오 수익률은 이전 체결 비중으로 계산한다.

`ret_t = Σ_i w_{t-1,i} * ((close_{t,i} / close_{t-1,i}) - 1)`

아래 조건이면 해당 심볼 기여도는 0으로 처리한다.

- `close_{t-1,i} <= 0`
- `close_{t,i} <= 0`
- 값이 비유한값(`NaN`, `Inf`)

### 메트릭

- `equity_t = Π_k (1 + ret_k)`
- `pnl = equity_last - 1`
- `sharpe = (mean(r) / (std(r, ddof=1) + 1e-12)) * sqrt(365)` (`std<=0` 또는 유효 샘플 부족 시 `0`)
- `mdd = min(equity / (cummax(equity) + 1e-12) - 1)`
- `cagr = equity_last^(365 / max(n, 1)) - 1`
- `turnover = mean(sum(abs(delta_weight_t)))`
- `min_bet = mean(mean(abs(weight_t)))`
- `max_weight = max(max(abs(weight_t)))`
- `nonzero_position = count_t(any(abs(weight_t) > 1e-12))`

## 단위 규칙

내부(`PeriodMetrics`)는 raw 단위를 사용한다.

- `pnl`, `cagr`, `mdd`, `turnover`: 소수 수익률 단위
- `sharpe`: 무차원
- `min_bet`, `max_weight`: 비중 단위

API 직렬화(`metrics_to_api_dict`)에서는 아래 필드만 `%`로 변환한다.

- `pnl`, `cagr`, `mdd`, `turnover`

그 외 필드는 raw 단위를 유지한다.

## 에지 케이스 규칙

- 상장폐지: `(timestamp, symbol)` 행이 누락되면 리인덱싱 후 `close=NaN`. 수익률 기여도는 0.
- `close=0`: 행은 존재하지만 수익률 기여도는 0.
- 신규 상장: 이전 바에 종목이 없으면 초기 보유비중 전이가 발생할 수 있음(턴오버 반영).
- `ret_series` 중 `NaN`: 메트릭 집계 시 finite 값만 사용.
- `D0`/`D1`: 둘 다 이전 체결 비중으로 수익을 계산하되 체결 시점이 다름.
- IS/OS split: 단일 연속 런 후 `os_start` 기준으로 분할(warm-start).

## 테스트 시나리오

| 시나리오 | 핵심 검증 | 테스트 파일 |
|---|---|---|
| A 단조상승 | `pnl>0`, `mdd<=0`, oracle 일치 | `LongShort/tests/unit/test_metrics_truth_table.py` |
| B 단조하락 | `pnl<0`, `mdd<0`, oracle 일치 | `LongShort/tests/unit/test_metrics_truth_table.py` |
| C 변동성/종단 0근처 | 최종 `pnl≈0`, 샤프/드로우다운 분리 | `LongShort/tests/unit/test_metrics_truth_table.py` |
| D all-zero return | 경계값(샤프 0, MDD 0 근처) | `LongShort/tests/unit/test_metrics_truth_table.py` |
| E singleton return | `ddof=1` 경계 처리(샤프 0) | `LongShort/tests/unit/test_metrics_truth_table.py` |
| F 중간 NaN return | finite-only 집계 검증 | `LongShort/tests/unit/test_metrics_truth_table.py` |
| G 상장폐지(NaN close) | 수익률 기여도 0 + 턴오버 전이 | `LongShort/tests/unit/test_metrics_truth_table.py` |
| H `close=0` | 수익률 기여도 0 + 가중치 계산 차이 | `LongShort/tests/unit/test_metrics_truth_table.py` |
| I 신규 상장 | 초기 비중 전이/턴오버 반영 | `LongShort/tests/unit/test_metrics_truth_table.py` |
| J D0 vs D1 | 상수/변동 시그널에서 모드 차이 재현 | `LongShort/tests/unit/test_metrics_truth_table.py` |
| 병렬/직렬 일치 | `n_jobs=1` vs `n_jobs=2` 메트릭/ret_series 일치 | `LongShort/tests/unit/test_metrics_parallel_consistency.py` |
| API 단위 변환 | `%` 변환 필드 vs raw 필드 분리 | `LongShort/tests/unit/test_metrics_calculations.py` |

## Pipeline 검증 (계산 → API → UI)

### 경로별 책임 분리

| 경로 | 목적 | 자동 검증 레이어 |
|---|---|---|
| `/api/ga/run-stream` | 실제 UI Run 버튼 실행 경로 | backend stream contract + frontend hook/store + Playwright E2E |
| `/api/backtest/run` | 백엔드 API 계약/직렬화 경로 | backend contract + frontend render injection |

### 파이프라인 시나리오 표

| 입력 | 기대값 | 검증 파일 |
|---|---|---|
| deterministic single alpha payload | `/api/backtest/run` 직렬화 결과가 oracle/fixture와 일치 | `LongShort/tests/unit/test_api_pipeline_contract.py` |
| deterministic multi alpha payload | Combined + 개별 alpha metric/series 필드 일치 | `LongShort/tests/unit/test_api_pipeline_contract.py` |
| no-variable XML (`run-stream` fallback) | NDJSON 이벤트가 `complete` 1개로 종료 | `LongShort/tests/unit/test_api_ga_stream_pipeline.py` |
| GA search stream | `progress → generation_end.partialResult → complete` 순서/스키마 보장 | `LongShort/tests/unit/test_api_ga_stream_pipeline.py` |
| hook state transition | `isRunning/gaProgress/result/error/selectedCell` 전이 일치 | `LongShort/frontend/__tests__/useBacktest.pipeline.test.tsx` |
| API client parsing | chunk 분할 NDJSON 파싱/오류 메시지 전달 일치 | `LongShort/frontend/__tests__/apiClient.contract.test.ts` |
| metrics table render | 부호/소수점/% 문자열 포맷 고정 | `LongShort/frontend/__tests__/BacktestMetrics.render.test.tsx` |
| summary render | Combined IS/OS 포맷 고정 | `LongShort/frontend/__tests__/SummaryMetrics.render.test.tsx` |
| ranking render | Combined row fallback(`-`) 및 정렬 텍스트 고정 | `LongShort/frontend/__tests__/AlphaRankingTable.render.test.tsx` |
| detail yearly render | Net/Gross/Sharpe/MDD/Fee Impact + `transactionFeeRate` 반영 | `LongShort/frontend/__tests__/DetailSummaryTable.render.test.tsx` |
| detail chart 파생값 | PnL/MDD 파생 데이터 계산 고정(픽셀 검증 제외) | `LongShort/frontend/__tests__/DetailTimeSeries.derived.test.ts` |
| 사용자 플로우 E2E | Run 클릭 후 최종 화면 수치 문자열 exact match | `LongShort/frontend/tests/e2e/metrics_pipeline.spec.ts` |

## 재현 명령

작업 디렉토리: `LongShort`

```bash
conda run -n qv python -m pytest -q \
  tests/unit/test_metrics_calculations.py \
  tests/unit/test_metrics_truth_table.py \
  tests/unit/test_metrics_parallel_consistency.py \
  tests/unit/test_reliability_golden.py \
  tests/unit/test_backtester.py \
  tests/unit/test_api_pipeline_contract.py \
  tests/unit/test_api_ga_stream_pipeline.py

cd frontend && npm test
cd frontend && npm run test:e2e -- tests/e2e/metrics_pipeline.spec.ts

# non-blocking 통합 실행 스크립트
./scripts/test_pipeline.sh
```
