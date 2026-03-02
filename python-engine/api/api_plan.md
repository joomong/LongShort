# API Plan

## Phase 4: Single Alpha IS/OS 분리

### 배경
- `run_single_alpha`가 전체 기간을 한 번에 실행 → IS/OS metrics가 동일한 값
- `run_multiple_alphas`는 `PeriodSettings`로 정상 분리 중
- 엔진(`framework/backtester.py`)은 IS/OS 분리 이미 지원

### TODO
- [x] `backtest_service.py`의 `run_single_alpha`에서 IS/OS 기간별로 분리 — 전체 기간 실행 후 `osStart` 기준으로 ret_series를 split하여 각각 metrics 계산
- [x] IS/OS 각각의 metrics + retSeries를 응답에 포함
- [x] heatmap/distribution은 전체 기간 기준 유지
- [ ] 프론트엔드 `BacktestMetrics` 컴포넌트에서 IS/OS 수치가 다르게 표시되는지 확인

---

## Phase 5: Portfolio Visualization 데이터 노출

### 배경
- 엔진이 `viz["portfolio"]` (equity curve, MDD, turnover, min_bet 시계열)를 반환하지만 API에서 무시 중
- 프론트엔드에서 equity curve, drawdown 차트 등에 활용 가능

### TODO
- [ ] `run_single_alpha` 응답에 `portfolioSeries` (equity, mdd 시계열) 추가
- [ ] 프론트엔드 타입 + 차트 컴포넌트에서 portfolio 시계열 표시
