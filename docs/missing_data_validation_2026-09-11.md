# 결측·상폐 회계 독립 검증 — 2026-09-11

기준 HEAD는 `2e77af1`이다. 검토자는 simulator/loader/HTTP 구현을 담당하지 않는다. `cpp-engine/tools/check_missing_data.py`와 검증 문서만 담당하며, 기존 untracked `docs/review_2026-09-11_independent.md`, 원본 cache 및 다른 담당자의 서버를 변경하지 않는다.

## 확정 회계 계약

실제 shares/cash/drift 포트폴리오로 바꾸는 작업이 아니다. 기존 **fixed-book notional weights** `h`를 유지한다.

- 관측 가능한 정상 가격은 finite이고 양수인 close다. 결측 동안 직전 관측 평가가격을 기억하고 보유 h를 동결한다. 결측 가격으로 신규 진입·청산·반전을 체결하지 않으며 시장 turnover/fee는 0이다.
- 복구일은 `h_prev * (close / last_observed_mark - 1)`을 전액 계상한 뒤 최신 실행 target으로 재조정한다. gap 중의 별도 수익을 추측하거나 가격을 원본에 채워 넣지 않는다.
- carry 정책에서는 quote가 없어도 finite signal을 intent 정규화에 포함한다. raw signal NaN은 기존처럼 target 0이다. 이전 pending 주문량을 누적하지 않고 새 실행 target으로 대체한다. mode=1은 intent와 PIT mask를 한 bar 지연한다.
- 동결 gross `F`가 있을 때 나머지 tradable target은 `max(0, 1-F)`를 넘는 부분만 비례 축소한다. 남는 예산을 채우도록 확대하지 않는다. F=0이면 기존 경로를 유지한다.
- 상폐는 명시 securityEndDate 다음 거래 bar 또는 명시 정산일에 발생한다. 미래 가격의 NaN 패턴에서 사건을 추론하지 않는다.
- event R은 직전 평가가격부터 정산까지의 총 return이며 정상 quote move **대신** `h_prev*R`을 적용한다. explicit finite R을 우선하고 나머지만 default 0 또는 sensitivity -1을 사용한다.
- 행정 정산은 시장 체결이 아니므로 turnover/fee=0이고 settled gross를 별도 기록한다. retired 상태는 영구적이며 보유를 없애고 pending entry를 취소한다.
- warmup은 intent/평가 준비만 하고 보유·수수료·PnL은 만들지 않는다. 비용은 두 시나리오 모두 편도 5bp를 유지한다.

## 독립 oracle 구성

1. 가격 관측 마스크와 valuation mark는 Python/Pandas에서 별도로 계산한다. forward-fill은 oracle의 평가원장에만 사용하며 거래 가능 여부나 원본 close에는 사용하지 않는다.
2. intent는 raw signal·PIT mask로 만들고 mode에 따라 시간축에서 이동한다. 포트폴리오는 NumPy 배열의 상태 전이와 원장 identity로 계산한다. C++ 함수·코드를 추출하거나 베끼지 않는다.
3. 별도로 손으로 고정한 작은 예제의 position/trade/gross/fee/net 숫자를 사용한다. oracle 자체가 이 숫자를 만족하는지 먼저 검사한다.
4. C++ opt-in `AccountingRowView`를 테스트 probe에서 받아 bar별 pre/post h, signed market trade, mark, retired/event 및 gross/fee/net를 비교한다. 행정 정산에 시장 수수료가 붙지 않는지, 결측 체결이 없는지를 원장 identity로도 확인한다.
5. full Top500에서는 trace를 stream으로 읽고 bar별 수익·체결·position identity를 검사한다. 정상 실행의 downsampled signalPlot을 oracle 입력으로 사용하지 않는다. 필요하면 검사에 필요한 최소 trace만 별도 임시 파일로 만들고 큰 JSON을 UI 응답에 추가하지 않는다.

## 손계산 사례

예제 비용은 `c=0.0005`, 가격이 별도 표시되지 않은 종목은 100으로 일정하다.

| 사례 | 독립 기대값의 핵심 |
| --- | --- |
| 보유 long 1의 100→NaN→NaN→80 | gap 두 bar의 h=1, trade=0, fee=0, PnL=0. 복구일 gross=-0.2. 그날 다른 종목 +1로 이동하면 시장 turnover=2, net=-0.201. |
| 미보유 종목의 blocked entry | quote 결측 bar에 intent가 +1이어도 h=0. 첫 fresh quote에서 해당 mode의 최신 target만 적용한다. 이전 100→80 손실을 미보유 계정에 부과하지 않는다. |
| pending 대체 | h=1 동결 중 target 0→-1, 복구일 최신 실행 target이 +1이면 시장 trade/fee=0. 과거 청산/반전 주문을 차례대로 실행하지 않는다. |
| frozen budget, 축소만 | 초기 h=(0.6,0.2,0.2), A 결측. 새 target=(0.75,0.25,0)이면 h=(0.6,0.25,0), turnover=0.25, fee=0.000125. 남는 0.15를 임의 재배분하지 않는다. |
| frozen budget 초과 | 위 다음 target=(0,0.5,-0.5)이면 h=(0.6,0.2,-0.2). A가80으로복구하면 gross=-0.12, h=(0,0.5,-0.5), turnover=1.2, fee=0.0006. |
| delist fallback 0 | held h가 행정 정산으로 0이 되며 event gross=0, market turnover=fee=0. 이후 quote가 있어도 재진입하지 않는다. |
| delist fallback -1 | long +1은 event gross=-1, short -1은 +1. 정상 quote move와 중복 합산하지 않는다. |
| explicit event override | explicit R=-0.25는 default 0/-1 모두 -0.25를 사용한다. 사건 날짜/종목·signal은 동일하다. |
| event + fresh quote | 직전mark100, event당일quote120/R=-0.5이면 long1 gross=-0.5이며 +0.2를 더하지 않는다. |
| simStart/mode0/1 | warmup의 가격 변화·event는 과거 보유손익을 만들지 않는다. mode1 첫 체결은 직전 intent, mode0은 당일 intent. 시작일 quote가 없으면 두 경우 모두 시장 체결0. |

각 예제에서 raw return뿐 아니라 actual signed trade와 post-position을 확인한다. 무결측 입력은 기존 fixed-book 결과가 정확히 유지되는 별도 회귀 대상으로 삼는다.

## 실제 전기간 실행 종료 기준

- 원본 `/home/base/cache/cache_v1_cash_1d/`를 읽기만 한다. Top500 PIT, 시작 2010-01-01, end latest, warmup 252, annualization 252, 편도 비용 5bp를 사용한다.
- 가격 알파 3개: `returns → zscore`, `-returns → tsmean(5) → zscore`, `close → tsmean(20) → zscore`.
- AFL 알파 2개: `afl_100 → zscore`, `afl_963 → zscore`. monthly source의 as-of mapping을 유지한다.
- 위 5개를 fallback 0과 -1로 각각 끝까지 실행한다. 동일 loaded panel, 동일 event 집합, 동일 raw signal을 사용하며 explicit return은 두 경우 모두 유지한다.
- event 시간/종목/override manifest hash와 raw signal hash의 일치를 확인한다. fixture 끝날짜를 임의로 줄이거나 실패 종목을 제거하거나 fee를 바꿔 성공시키지 않는다.
- 각 실행의 기간·종목 수·PnL/Sharpe/MDD·시장 turnover/fee·동결/blocked/recovery/event/retired 위반 수·실행시간·RSS를 보고한다. 불일치면 최초 bar/종목과 원장 값을 남기고 실패로 종료한다.

## 진행 상태

현재는 계약·독립 검증 계획 단계다. engine 담당이 제공할 AccountingRowView와 data 담당의 event 스키마가 준비되면 probe/하네스를 연결한다. full Top500 성공 및 sensitivity 완료라고 아직 주장하지 않는다.
