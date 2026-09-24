# 결측 종가·상폐 회계 독립 oracle 검증 보고 (oracle 담당, 2026-09-11)

C++는 한 줄도 수정하지 않았음. `simulator.cpp`로 현재 동작을 파악했으나 원장은 계약 문서·브리프에서 독립적으로 다시 구현했음.

## 0. 산출물

| 파일 | 역할 |
| --- | --- |
| `/home/jgo/longshort_platform/cpp-engine/tools/missing_close_oracle.py` | 공용 원장(pandas/numpy). bar별 h_prev/desired/h/trade/mark/retired/진단/gross·fee·net·turnover |
| `/home/jgo/longshort_platform/cpp-engine/tools/check_missing_data.py` | `--self-check`(손계산 10사례) + `--server`(합성 패널 HTTP 대조) |
| `/home/jgo/longshort_platform/cpp-engine/tools/run_top500_missing_close.py` | 실제 Top500 전기간 하네스(알파 5 × R{0,−1}) |
| `scratchpad/orch/check_missing_data.json` | 자체검사 + HTTP 대조 원자료 |
| `scratchpad/orch/top500_run/{report.md,report.json}` | 전기간 실행 표(현행 바이너리) |
| `scratchpad/orch/top500_dryrun/{report.md,report.json}` | 구현 전 바이너리 dry-run 보존본 |

실행은 전부 `/home/jgo/usr/venv-3.11/bin/python`. `/home/base`는 읽기만 했고 기존 파일·설정·C++는 건드리지 않았음.

## 1. 원장 규칙 요약

bar t 처리 순서(브리프 §2 그대로):

1. **정산** — `settlement_bar[i]==t` 또는 (carry ∧ 이벤트 없음 ∧ stale>0 ∧ 연속 결측 ≥ stale). `R = explicit finite ? explicit : default`. `pnl += h_prev·R`, `settled_gross += |h_prev|`, `h=0`, `retired=true` 영구. **수수료 0·turnover 0**.
2. **intent 정규화** — 정산 뒤에 수행. carry면 quote 없어도 finite signal은 intent에 포함(mask·retired 제외), error면 valid close 게이트 유지. signal NaN → target 0.
3. **보유손익** — quote 유효면 `pnl += h_prev·(close/mark − 1)` 후 `mark=close`. 결측이면 손익항 0·mark 유지·동결.
4. **체결** — quote 유효 종목만 `h=desired·scale`, 결측 보유는 `h=h_prev`. `F=Σ_{결측 보유}|h_prev|`, `G=Σ_{유효}|desired|`, `G > max(0,1−F)`일 때만 `scale = max(0,1−F)/G` (**축소만, 확대 없음**).
5. **turnover/fee** — `turnover = Σ_{유효·미퇴출}|h − h_prev|`, `fee = turnover·rate`, `ret = gross − fee`.
6. **통계** — position_count/long/short/max_weight 전부 체결 후 `h` 기준(동결분 포함).
7. **warmup** — mark·intent만 갱신. 보유·turnover·fee·pnl 없음. retired 전이는 반영하되 손익 없음.

forward-fill은 **평가 mark에만** 존재함. 원본 close를 채우지 않고 거래 가능 여부 판정에도 쓰지 않음.

### 계약에 명시가 없어 판단이 필요했던 지점

전부 HTTP 대조에서 엔진 구현과 **일치**함이 사후 확인됨.

| # | 쟁점 | 원장의 선택 | 근거 |
| --- | --- | --- | --- |
| 1 | stale 정산이 미보유 종목도 퇴출시키는가 | `h_prev ≠ 0`일 때만 | `simulator.h`가 "settle frozen **holdings**"라 적음. 게이트를 빼면 상장 전 NaN 구간이 stale bars를 넘겨 IPO 종목이 영구 퇴출됨(합성 패널 실측: h가 0.5→0.0으로 사라짐) |
| 2 | `blocked_trade_weight`의 desired가 축소 전인가 후인가 | 축소 전(raw) | 결측 종목은 frozen budget 분모 G에 안 들어가 scaled desired가 정의되지 않음. 엔진도 `frozen_budget` 사례에서 0.75(=0.15+0.6)로 동일 |
| 3 | `F > 1`일 때 `(1−F)/G` 부호 | `max(0,1−F)/G` | 음수 scale은 포지션 부호를 뒤집음. 실제로는 gross≤1이라 F≤1이므로 결과는 동일 |
| 4 | `delisting_count`가 미보유 정산도 세는가 | 보유분만 | `simulator.h` 주석 "Counts refer to affected **nonzero** bookkeeping weights". 전기간 실행에서 정산 229건 중 delistings 135~139만 계상되는 것으로 교차 확인됨 |
| 5 | intent 정규화와 정산의 순서 | 정산 → 정규화 | mode 0에서 당일 정산된 종목이 당일 intent에 남으면 안 됨 |
| 6 | `pending_delisting_entry_count` 의미 | 정산으로 취소된 비영 실행 intent 수(mode 1에서만 발생) | 엔진이 `cancelledDelistingEntries`로 노출했고 대조 결과 일치 |

## 2. 자체검사 — 계약 문서 §손계산 사례 (비용 0.0005, 기본 가격 100)

`check_missing_data.py --self-check` → 계약 표 12 시나리오 82 체크 + 표 외/리뷰 14 시나리오 102 체크 = **26 시나리오 184 체크 전부 PASS, 실패 0**(표 외 시나리오는 §7·§8). 표의 숫자는 하나도 고치지 않았고 원장이 전부 재현했음.

| 계약 문서 사례 | 시나리오 | 문서 기대값 | 원장 | 판정 |
| --- | --- | --- | --- | --- |
| 보유 long 1의 100→NaN→NaN→80 | `hold_gap_recovery` | gap h=1, trade=0, fee=0, PnL=0 / 복구일 gross=−0.2, turnover=2, net=−0.201 | 동일 | PASS(11) |
| 미보유 종목의 blocked entry | `blocked_entry` | 결측 bar h=0, 복구일 gross=0·h=1 | 동일 | PASS(4) |
| pending 대체 | `pending_replacement` | 복구일 h=1, trade=0, turnover=0, fee=0 | 동일 | PASS(4) |
| frozen budget, 축소만 | `frozen_budget` t1 | h=(0.6, 0.25, 0), turnover=0.25, fee=0.000125 | 동일 | PASS |
| frozen budget 초과 | `frozen_budget` t2·t3 | h=(0.6, 0.2, −0.2) → 복구일 gross=−0.12, h=(0, 0.5, −0.5), turnover=1.2, fee=0.0006 | 동일 | PASS(합계 17) |
| delist fallback 0 | `delist_default_zero` | event gross=0, turnover=fee=0, 이후 재진입 없음 | 동일 | PASS(6) |
| delist fallback −1 | `delist_default_minus_one_long/short` | long +1 → −1, short −1 → +1 | 동일 | PASS(3+3) |
| explicit event override | `explicit_override_default_zero/minus_one` | default 0·−1 모두 −0.25 | 동일 | PASS(2+2) |
| event + fresh quote | `event_with_fresh_quote` | mark 100·당일 quote 120·R=−0.5 → gross=−0.5(+0.2 미가산) | 동일 | PASS(2) |
| simStart/mode0/1 | `warmup_mode0`, `warmup_mode1` | warmup 손익 0, 시작일 quote 없으면 양쪽 체결 0, mode1 첫 체결=직전 intent·mode0=당일 intent | 동일 | PASS(11+11) |

**계약 문서 표에서 틀린 숫자는 발견하지 못했음**(단 9행 "event + fresh quote"는 2차 리뷰 규칙 변경으로 기대값이 대체됨 — §8-2). 표가 내부적으로도 일관됨(사례 5의 시작 상태 h=(0.6,0.25,0)이 사례 4의 결과를 그대로 이어받고, 모든 turnover×0.0005가 표기된 fee와 일치).

명시해 둘 점 하나: 사례 4의 결과 h=(0.6, 0.25, 0)은 gross가 0.85로 1이 아님. "남는 0.15를 임의 재배분하지 않는다"대로 **의도된 과소투자**이며 버그가 아님.

## 3. HTTP 대조

`check_missing_data.py --server`는 임시 디렉터리에 합성 wide parquet(close/signal/universe/events) + YAML을 만들고 18120~18139 중 빈 포트에 서버를 띄워 `<data expr="raw_signal"/>`를 POST함. YAML에는 계약 기본값(carry/0.0/0)만 두고 **변형은 요청 옵션(`missingClose`/`defaultDelistingReturn`/`staleQuoteSettleBars`)으로만** 줘서 `parse_run_options` 경로를 직접 때림. 패널마다 서버 1개를 띄워 4개 옵션 조합을 재사용하고(포트 TIME_WAIT 고갈 방지), 종료는 `finally`에서 강제함.

### 3-1. 플러밍 검증 (구현 전 바이너리)

- 결측·이벤트 없는 4개 패널 × 4 옵션 = **16 PASS**, 원장 대 HEAD 서버 최대 오차 1.7e-18. 즉 **결측 없는 입력에서 독립 원장이 기존 엔진과 사실상 비트 동일**임이 확인됐고, 이게 브리프 §2.9 회귀 기준의 독립 근거가 됨.
- 결측·이벤트 시나리오 12개 × 4 = **48 SKIP**. `/api/config`에 `missingClosePolicy`가 없으면 SKIP으로 표시하는 설계가 의도대로 동작함(오탐 아님).

### 3-2. 실검증 (1차 리뷰 시점, 15키 계약 기준)

> 아래 수치는 2차 규칙 변경(§8) **이전** 실행분임. 엔진 담당이 같은 규칙을 병렬 구현 중이라 서버 대조는 리드 지시 후 재실행 예정이고, 현재 원장은 self-check까지만 검증돼 있음.

재실행하니 `/api/config`에 `missingClosePolicy·defaultDelistingReturn·staleQuoteSettleBars·eventsLoaded·settlementSymbols`가 생겼고 응답에 `missingData`·`timeseries.frozenGross`·`timeseries.settledGross`가 붙어 SKIP이 전부 실대조로 바뀜.

| 항목 | 결과 |
| --- | --- |
| 총 사례 | 24 시나리오 × (carry/error) × (R=0/−1) + 옵션 범위 2 = **98, 전부 PASS** |
| 수치 대조 | 84건. `ret`·`cumPnl`·`cumPnlGross`·`turnover`·`maxWeight`·`drawdown`·`nonzeroCount`·`frozenGross`·`settledGross` + `missingData` **15키**를 전부 필수로 대조(누락=FAIL). 최대 절대오차 **5.96e-09**(explicit R −0.4의 float32 저장분, §7-5) |
| error 정책 fail-fast | 12건이 "양쪽 모두 거부", 예외 문구·bar·asset까지 동일(`invalid close at bar 1, asset 0`) |
| 문서 표에 없는 추가 시나리오 | 8종(§7-3) 전부 일치 |
| 옵션 범위 검증 | `defaultDelistingReturn` −1.5·+3 → 400 `must be a total return in [-1, 1]` (리뷰 F-03 해소 확인) |

§1의 판단 쟁점이 전부 엔진과 일치함이 확인됨: `staleSettlements=1`(보유분만), `blockedTradeWeight=0.75`(raw desired), explicit R 사례에서 `delistings=1·defaultDelistings=0`, `cancelledDelistingEntries=1`.

## 4. 전기간 하네스 (run_top500_missing_close.py)

`--config cpp-engine/examples/datasets/equities_monthly_afl.yaml`, simStart 2010-01-04 / isStart 2012-01-01 / osStart 2020-01-01, mode 1, missingClose carry, **staleQuoteSettleBars는 요청에서 생략해 config 값 60을 사용**.

패널 T=4449 N=1056 (거래 4197 bar, 2010-01-04~2026-09-10), `eventsLoaded=235` `settlementSymbols=235`, 로드 3.31초 RSS 278MB, 프로세스 peak RSS 878MB, 실행당 0.16~0.453초. 10/10 OK.

| alpha | R | PnL | Sharpe(full) | IS | OS | MDD | tvr/bar | fee 합계 | retirements | delistings | stale | cancelled | settledGross | frozenGrossSum | termFrozen |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| returns_zscore | 0 | -3.3968 | -2.626 | -4.093 | -2.078 | -3.4145 | 1.4500 | 3.0428 | 240 | 139 | 5 | 72 | 0.3445 | 1.9707 | 0.00374 |
| returns_zscore | -1 | -3.3926 | -2.602 | -4.002 | -2.095 | -3.4104 | 1.4500 | 3.0428 | 240 | 139 | 5 | 72 | 0.3445 | 1.9707 | 0.00374 |
| neg_returns_tsmean5_zscore | 0 | -0.4423 | -0.324 | -1.197 | 0.058 | -0.7366 | 0.6283 | 1.3184 | 240 | 139 | 5 | 77 | 0.2694 | 1.5067 | 0.00459 |
| neg_returns_tsmean5_zscore | -1 | -0.3322 | -0.243 | -0.990 | 0.101 | -0.6583 | 0.6283 | 1.3184 | 240 | 139 | 5 | 77 | 0.2694 | 1.5067 | 0.00459 |
| close_tsmean20_zscore | 0 | -0.5142 | -0.752 | -0.974 | -0.470 | -0.5292 | 0.0070 | 0.0147 | 240 | 139 | 5 | 77 | 0.2674 | 1.1626 | 0.00376 |
| close_tsmean20_zscore | -1 | -0.5578 | -0.799 | -1.078 | -0.424 | -0.5728 | 0.0070 | 0.0147 | 240 | 139 | 5 | 77 | 0.2674 | 1.1626 | 0.00376 |
| afl_100_zscore | 0 | -0.2956 | -0.171 | -0.023 | -0.363 | -0.5685 | 0.0452 | 0.0950 | 240 | 138 | 5 | 76 | 0.2219 | 1.2911 | 0.00474 |
| afl_100_zscore | -1 | -0.0755 | -0.044 | 0.136 | -0.298 | -0.4743 | 0.0452 | 0.0950 | 240 | 138 | 5 | 76 | 0.2219 | 1.2911 | 0.00474 |
| afl_963_zscore | 0 | 0.2471 | 0.386 | 0.388 | 0.415 | -0.0750 | 0.0419 | 0.0878 | 240 | 135 | 5 | 73 | 0.2351 | 2.1652 | 0.01172 |
| afl_963_zscore | -1 | 0.2529 | 0.390 | 0.399 | 0.361 | -0.0750 | 0.0419 | 0.0878 | 240 | 135 | 5 | 73 | 0.2351 | 2.1652 | 0.01172 |

공통 진단: `missingHeldBars=819`, `blockedOrders=820`(AFL 알파는 약간 작음), `resumedHoldings=18`, `missingEntries=1`. 누락 키 없음(15키 전수 존재).

유효 표본 n: full **4197** bar, IS(2012-01~2019-12) **2012** bar, OS(2020-01~) **1681** bar. 평균 보유 452~458 종목. Sharpe는 전부 |t|<2 수준이라 알파 판정 근거로는 못 씀 — 여기서는 회계 경로가 끝까지 도는지를 보는 용도임.

하네스가 자동 확인하는 항등식 2개:

- **R 민감도 항등식** 잔차 최대 8.2e-16, turnover·fee 합계는 R에 대해 완전 불변(5/5 True).
- **fee 항등식** 최대 오차 1.4e-14.

### stale_quote_settle_bars의 실제 효과 (하네스 결함 1건 수정 후 재측정)

처음엔 하네스가 `staleQuoteSettleBars=0`을 요청에 강제로 실어 config의 60을 덮어쓰고 있었음. 요청에서 생략하도록 고친 뒤 재실행한 차이:

| 지표 | stale=0 | stale=60(config) |
| --- | --- | --- |
| missingHeldBars | 1698 | **819** |
| delistings | 130~134 | **135~139** (stale 5건 포함) |
| staleSettlements | 0 | **5** |
| terminalFrozenGross | 0.0106~0.0250 | **0.0038~0.0117** |

즉 stale 규칙은 실데이터에서 실제로 발동하며(이벤트가 없는 5개 종목), 동결 asset-bar를 절반 이하로 줄이고 잔여 동결 노출을 3~4배 줄임. "이벤트만으로 충분하다"는 가정은 틀림.

### 구현 전 바이너리 dry-run (요청받은 확인 항목)

`scratchpad/orch/top500_dryrun/`에 보존돼 있음. 10개 실행 전부 400으로 실패했고 하네스는 오류를 표에 남기고 끝까지 진행했음. 최초 오류는 전부 동일:

```
Cannot value or execute position: invalid close at bar 252, asset 131
```

독립 확인 결과 bar 252 = **2010-01-04, 첫 거래 bar**, asset 131 = tradingItemId **23937615 = Marvel Entertainment**(securityEndDate 2009-12-31, 마지막 호가도 2009-12-31). 옛 동작이 "첫 상폐에서" 죽는 게 맞고, 그 첫 상폐가 하필 워밍업 직후 첫 거래 bar임. D1이 2009-12-31 intent를 2010-01-04로 끌고 가는데 그날 호가가 없어서 터지는 경로라 mode 1에서 특히 빨리 드러남.

## 5. 계약·브리프 숫자 검증 (/home/base 읽기 전용, 독립 재계산)

| 문서 주장 | 실측 | 판정 |
| --- | --- | --- |
| 마스터 parquet 28,210행, index=tradingItemId int64, securityEndDate timestamp[ns] | 28,210행 / int64 중복 0 / timestamp[ns] | **일치** |
| 2010~ 유니버스 1,056종목 | 패널(warmup 252bar 포함, 2009-01-02~2026-09-10) 기준 1,056. 2010-01-01 이후만 세면 1,021 | **일치**(warmup 포함 기준임을 명시할 것) |
| 그중 종료일 있음 235 | 235. 서버 `eventsLoaded=235`, `settlementSymbols=235`와도 일치 | **일치** |
| 끝결측 + 마지막 호가 유니버스 내 133 | **138** | **불일치(경미)**. 정의 차이로 보임. 하위 항목 "그중 종료일 없음 8"은 138 기준으로도 정확히 8이라 결론은 안 바뀜 |
| 종료일 이후 정상 호가 1건(Wolfspeed 1일) | 정확히 1건: tradingItemId 2603877, 종료일 2026-08-11, 호가 2026-08-12 하루 | **일치**. 손계산 §"event + fresh quote"와 같은 경로이며 원장·엔진 모두 정산값만 쓰고 +호가 수익을 더하지 않음 |
| (참고) 패널 내 정산 분포 | 235건, 최초 bar 3(2009-01-07, warmup), 거래 시작 이후 229건, 최후 bar 4428(2026-08-12) | — |

`securityStartDate`는 string, `securityEndDate`는 timestamp[ns]로 타입이 비대칭임. 지금 계약은 end만 쓰므로 문제 없으나 start를 쓰게 되면 로더에 문자열 경로가 또 필요함.

## 6. 남은 리스크·후속

1. **default R=0 자체는 가정임.** 이번 실행은 explicit return 없이(`return_col` 주석 처리) 전부 default를 탔음. settledGross 전기간 합계가 0.22~0.34(북 gross 1 기준)라 R 선택의 PnL 영향은 −0.044~+0.220 수준. 작아 보여도 `afl_100`은 PnL이 −0.2956 → −0.0755로 74% 움직이므로 "무시 가능"이 아님. explicit return 컬럼이 붙으면 재실행 필요.
2. **stale=60은 튜닝되지 않은 값임.** 5건을 정산시키고 동결을 절반으로 줄이는 실효가 있는 만큼, 60이라는 숫자 자체가 결과에 영향을 주는 자유 파라미터임. 30/60/120 민감도를 한 번 돌려볼 가치 있음(하네스에 `--stale-bars`로 들어가 있음).
3. **carry 정책에서 마지막까지 결측인 보유분**은 `terminalFrozenGross` 0.004~0.012로 남음. 청산되지 않은 노출이므로 성과 해석 시 별도 표기 필요.
4. 검증 도중 포트 18101에 다른 담당의 **orphan 서버**(pid 1644814, PPID 1, fixture `scratchpad/http_fixture_events/config.yaml`)가 떠 있는 것을 관찰했음. 내 것이 아니라 건드리지 않았고 이후 스스로 사라짐. gtest HTTP 계약 테스트가 부모 종료 시 자식 서버를 남기는 경로가 있는지 엔진 담당이 한 번 확인해 두면 좋겠음.
5. 내 하네스가 띄운 서버는 전부 종료 확인했음. 저장소 변경은 신규 파일 3개(`cpp-engine/tools/{missing_close_oracle,check_missing_data,run_top500_missing_close}.py`)뿐이며 커밋하지 않았음.

## 7. 독립 리뷰 반영 (review_report.md §1 F-06·F-07, §3 누락 테스트)

### 7-1. F-07 — 원장의 frozen budget 축소를 `F > 0`으로 게이트

`missing_close_oracle.py`의 축소 조건을 `frozen_budget > 0.0 and tradable_gross > room`으로 바꿨음. 계약 "F=0이면 기존 경로와 동일"과 이제 문자 그대로 일치하며, Σ|target|이 부동소수로 1을 미세 초과해도 무결측 입력에서 원장만 축소가 걸리는 경로가 사라졌음.

부수적으로 확인한 불변식: 북 gross는 `F + min(G, 1−F) ≤ 1`이라 **항상 1 이하**임. 따라서 `F > 1`은 구조적으로 불가능하고, 리뷰 전 내가 걱정했던 "음수 scale로 포지션 부호가 뒤집히는" 분기는 도달 불가임(`max(0, 1−F)` 가드는 그대로 둠).

### 7-2. F-06 — 조용히 건너뛰는 대조 경로 제거

- `missingData` **필수 15키**(기존 13 + 엔진이 추가한 `frozenGrossSum`·`retirements`)를 전수 요구함. 키가 없으면 **FAIL**, 예상 키 집합에 없는 키가 새로 생겨도 **FAIL**(무단 추가 감지).
- per-bar 시계열 `turnover·maxWeight·drawdown·cumPnlGross·nonzeroCount·frozenGross·settledGross` 7종을 필수로 요구하고, **길이 불일치도 FAIL**로 바꿨음(이전에는 길이가 다르면 비교를 건너뜀).
- 옛 바이너리(`/api/config`에 `missingClosePolicy` 없음)만 예외로 SKIP을 유지하되, 그 경우에도 레거시 시계열은 여전히 대조하고 값이 틀리면 FAIL임.
- 가드가 비어 있지 않음을 음성 테스트로 확인했음: `retirements`/`frozenGrossSum`/`frozenGross` 삭제, `turnover` 길이 −1, `missingData` 객체 삭제, 예상 외 키 추가 6가지 변형이 전부 FAIL로 잡힘.

### 7-3. 시나리오 추가 (리뷰 §3 누락 목록)

self-check 기준 **20 시나리오 143 체크 전부 PASS**(계약 표 12/76 + 표 외 8/67). 추가분:

| 시나리오 | 리뷰 항목 | 고정한 기대값 |
| --- | --- | --- |
| `stale_with_explicit_return` | §3-1 / F-01 | null end date + explicit R=−0.4 + stale=2 → `delistingPnl=−0.4·h_prev`, `delistings=1`, `staleSettlements=1`, **`defaultDelistings=0`·`defaultDelistingGross=0`** |
| `two_frozen_budget` | §3-4 | A·B 동시 동결 F=0.7, target gross 1.0 → scale 0.3, h=(0.4, 0.3, 0.15, 0.15), turnover 0.1, blockedTradeWeight 0.7 |
| `frozen_budget_exhausted` | §3-5 | F=1.0 → room 0 → 유효 종목 target이 0으로 평탄화(부호 반전 없음), turnover 0, position_count 2 |
| `universe_exit_while_missing` | §3-6 | 결측 중 마스크 이탈 → intent에서만 빠지고 보유는 영구 동결(h_A=0.5 유지, `delistings=0`, `retirements=0`) |
| `relisted_after_settlement` | §3-9 | 정산 후 같은 id가 120·150으로 호가 재개해도 gross 0·h 0·turnover 0 |
| `option_range__defaultDelistingReturn_{-1.5,3}` | F-03 | HTTP **400** |
| (기존) `stale_settlement`, `mode1_pending_cancelled`, `retired_before_panel` | §3-7·8 관련 | 유지 |

원장 쪽에도 `default_delisting_return < −1`을 생성자에서 거부하도록 가드를 넣었음(정산 총수익률은 포지션 원금보다 더 잃을 수 없음).

### 7-4. 현재 바이너리 실행 결과 — 리뷰 지적 3건이 이미 해소돼 있었음

지시받은 대로 현재 바이너리로 1회 실행한 결과, "지금은 FAIL이 정상"이라던 항목들이 **전부 PASS**로 나왔음. 엔진 담당이 이미 고친 것으로 보임.

| 리뷰 항목 | 현재 서버 동작 | 판정 |
| --- | --- | --- |
| F-01 stale이 explicit R 무시 | `delistingPnl=−0.4000000059`, `defaultDelistings=0`, `staleSettlements=1` | **해소됨** |
| F-03 `defaultDelistingReturn` 범위 미검증 | −1.5·+3 모두 400 `defaultDelistingReturn must be a total return in [-1, 1]` | **해소됨** |
| F-08/F-09 신규 키 | `frozenGrossSum`·`retirements` 둘 다 존재하고 원장과 일치 | **해소됨** |

HTTP 대조 최종: **98 케이스 전부 PASS**(24 시나리오 × 4 옵션 + 옵션 범위 2). 전기간 실행도 10/10 OK이며 `retirements=240`(= 패널 이벤트 235 + stale 5)으로 §5에서 독립 계산한 235와 정합함.

### 7-5. 이번 라운드에서 새로 발견한 것 2건

1. **explicit settlement return이 float32로 저장됨.** `DataPanel::settlement_return`이 `std::vector<float>`라 parquet의 double R이 float32로 잘림. 실측: R=−0.4 → 적용값 −0.40000000596046448, 원장(float64) 대비 **5.96e-09** 차이. 원장도 같은 캐스팅을 하도록 고쳐 일치시켰고, 손계산 체크는 계약 숫자 −0.4를 유지한 채 그 체크에만 float32 허용오차(1e-6)를 붙였음. 영향은 |h_prev|당 ~1e-8로 실무상 무시 가능하나, R을 0.333333 같은 값으로 주면 같은 크기의 오차가 항상 따라옴. **의도한 정밀도인지 확인 필요**(double로 올릴지, 문서에 float32 정밀도라고 명시할지).
2. **`retirements`의 warmup 포함 여부가 계약에 없었음.** 엔진은 warmup 구간의 정산도 카운트함(실측: `warmup_mode0` 시나리오에서 서버 1 vs 초기 원장 0). 다른 진단은 전부 warmup에서 0이라 원장도 그 규약을 따랐었는데, `retirements`는 장부 이벤트가 아니라 "이 패널에서 몇 종목이 퇴출됐나"를 묻는 상태 전이 카운터이므로 warmup 포함이 더 literal한 해석이라 판단해 원장을 맞췄음(`LedgerConfig.count_warmup_retirements`로 플래그 노출). 전기간 240 = 235(이벤트, warmup 6건 포함) + 5(stale)로 교차 검증됨. **계약 문서에 한 줄 명시 권고.**

### 7-6. 남은 미이행 항목

- 리뷰 §3-13 "실데이터 전기간의 bar별 원장 대조"는 여전히 미이행임. 현 하네스는 집계 항등식 2개만 검사하고, 독립 원장의 bar별 대조는 T≤12·N≤4 합성 패널에만 걸려 있음. 실데이터 bar별 대조를 하려면 엔진이 `accounting_row_callback` 기반 trace를 파일로 내보내는 probe가 필요함(내 담당 파일 밖이라 리드 판단 필요).
- 리뷰 §3-2(옛 엔진 대비 비트 동일을 레포 안에 고정), §3-10~12는 C++ 테스트·문서 쪽이라 내 담당이 아님.

## 8. 2차 리뷰 반영 — carry 범위 축소와 종료일·호가 충돌

서버 대조는 리드 지시 대기 중이라 이번 절은 **`--self-check` 기준**임. 26 시나리오 **184 체크 전부 PASS, 실패 0**.

### 8-1. 규칙 1 — carry는 NaN 종가에만 적용

`missing_close_oracle.py`가 종가를 3상태로 나눔:

| 상태 | 정의 | 처리 |
| --- | --- | --- |
| valid | `isfinite && > 0` | 정상 평가·체결 |
| missing | **NaN만** | carry에서 동결, error에서 fail-fast |
| abnormal | 0, 음수, ±inf (NaN 아닌 비정상) | **두 정책 모두 오류**(`LedgerError` → 엔진은 400) |

판단이 필요했던 지점과 선택:

- **거부 범위**: 기존 fail-fast와 같은 게이트(`보유 ≠ 0 || 실행 intent ≠ 0`)를 쓰되 abnormal이면 정책과 무관하게 던짐. 무조건 거부(유니버스 밖 종목의 0 가격까지)는 과할 수 있어 채택하지 않았음. 다만 carry는 정규화에 quote 없는 종목도 포함하므로 **마스크 안에서 유한 signal을 가진 종목의 abnormal 가격은 사실상 전부 걸림** — carry가 error보다 abnormal에 더 엄격해지는 비대칭이 생기는데, 이는 "carry는 NaN만 처리한다"의 직접 귀결임.
- **예외 문구**: error 정책의 결측 거부는 기존 문구(`invalid close at bar X, asset Y`)를 그대로 두고, abnormal은 값이 보이도록 `abnormal close 0.0 at bar X, asset Y`로 구분했음.
- `missing_run`(stale 카운터)은 이제 **NaN bar만** 증가함. abnormal bar는 run을 리셋함.
- 동결 집합도 `보유 && NaN`으로 좁혔음(이전 `보유 && !valid`).

### 8-2. 규칙 2 — 종료일과 호가가 충돌하면 정산을 미룸

`settlement_bar`에 유효 quote가 있으면 정산하지 않고 **pending**으로 두고 정상 평가·체결함. 이후 **첫 NaN bar**에 정산하며, R은 그 시점의 직전 mark 기준(`mark ← mark·(1+R)`)임. 패널 끝까지 quote가 이어지면 미정산·보유 유지.

신규 진단 2개를 원장에 추가하고 `check_missing_data.py` 필수 키를 **15 → 17키**로 늘렸음:

| 키 | 정의 | 원장 구현 |
| --- | --- | --- |
| `postEndDateQuoteBars` | 종료일 이후 유효 quote로 평가된 asset-bar 수 | 거래 bar에서 `pending && valid` 개수 합. **보유 여부와 무관**(데이터 충돌 자체를 세는 진단이라는 판단; 실데이터에서 이 값이 1이 나와야 §5의 "종료일 이후 호가 1건(2603877)"과 맞음) |
| `pendingSettlements` | 마지막 bar 기준 미정산 이벤트 수 | `pending && !retired`를 매 bar 상태로 기록하고 **마지막 bar 값**을 보고(`terminalFrozenGross`와 같은 축약) |

**이 규칙 때문에 계약 표 9행 "event + fresh quote"의 기대값이 대체됨.** 표는 "직전mark100, event당일quote120/R=−0.5이면 gross=−0.5이며 +0.2를 더하지 않는다"였으나, 새 규칙에서는 그 bar가 정산 대상이 아니므로 **+0.2를 정상 인식**하고 다음 NaN bar에서 −0.5로 정산함. 코드에 맞춘 수정이 아니라 규칙이 바뀐 결과이며, 시나리오 주석에 그 경위를 남겼음.

**브리프 §2.1 `settlement_bar==0` 문구와의 충돌**: "첫 bar부터 retired"인데 규칙 2는 유효 quote가 있으면 미룸. 원장은 규칙 2를 균일 적용함(호가가 계속 나오면 벤더 종료일보다 호가를 믿음). `retired_before_panel` 시나리오는 A를 전 구간 NaN으로 바꿔 "패널 시작 전 종료 = 호가 없음"이라는 실제 의미를 반영했고, 결과는 종전과 동일함. **계약 문서에 한 줄 정리 필요.**

### 8-3. 시나리오 추가·갱신

| 시나리오 | 내용 | 고정한 기대값 |
| --- | --- | --- |
| `deferred_settlement_mode0` / `_mode1` | Wolfspeed형: 종료일 bar에 quote 120, 다음 bar NaN | t2 gross **+0.2**·h=1·`postEndDateQuoteBars=1`·`pendingSettlements=1`, t3 정산 gross 0·h=0·`retirements=1`·`pendingSettlements=0`, t4 재호가에도 h=0. mode 1은 t3에 `cancelledDelistingEntries=1` 추가 |
| `quotes_outlive_end_date` | 종료일 이후 호가가 패널 끝까지 지속 | 미정산 — `retirements=0`, `delistings=0`, `delistingPnl=0`(R=−1인데도 부과 안 함), h=1 유지, `pendingSettlements=1`, `postEndDateQuoteBars` 3bar |
| `abnormal_{zero,negative,inf}_price` | 보유 종목에 0 / −1 / +inf 가격 | carry·error **양쪽 모두 거부**(6건 전부 PASS) |
| `event_with_fresh_quote` | 계약 표 9행 | 위 §8-2대로 갱신 |
| 기존 정산 시나리오 6종 | `delist_default_*`, `explicit_override_*`, `mode1_pending_cancelled`, `retired_before_panel`, `relisted_after_settlement` | 정산 bar 종가를 NaN으로 바꿔 "정상 상폐"(마지막 호가=종료일) 형태로 정정. **표의 기대 숫자(−1/+1/−0.25/0)는 그대로 유지됨** |

### 8-4. 하네스 변경

- `check_missing_data.py`: 필수 `missingData` 17키, `Scenario.expect_reject`(양 정책 거부를 self-check에서 검증), 이벤트 fixture가 null end date + explicit R 행도 기록.
- `run_top500_missing_close.py`: 표에 `postEndDateQuoteBars`·`pendingSettlements` 열 추가(키 없으면 공란). 30개 시나리오 fixture 직렬화(inf·전구간 NaN·null 종료일 포함) 스모크 통과.
- 서버 대조 미실행. §3-2·§4의 수치는 2차 규칙 이전 실행분이며, 엔진 구현 후 리드 지시로 재실행 필요함.

### 8-5. 실데이터 영향 예상

§5에서 독립 확인한 대로 종료일 이후 유효 호가는 전 기간 **1건**(tradingItemId 2603877, 종료일 2026-08-11, 호가 2026-08-12 하루)뿐임. 그 종목은 2026-08-12 호가가 평가에 반영되고 정산은 그 다음 NaN bar로 밀림. 따라서 전기간 기대값은 `postEndDateQuoteBars ≈ 1`, `pendingSettlements`는 마지막 bar에 미정산이 남는지에 따라 0 또는 1임. 규칙 1 쪽은 실데이터 close에 0·음수·inf가 있으면 전 실행이 400으로 바뀌므로 미리 확인했음: 패널 4,449 × 1,056 = **4,698,144 셀 중 valid 3,594,007 / NaN 1,104,137 / abnormal 0**. **0·음수·inf는 한 셀도 없어 규칙 1이 전기간 실행을 깨지 않음**(float32 캐스팅 후 측정). 다만 이는 현재 캐시 상태이며, 벤더 갱신 후에도 유지된다는 보장은 아님 — 하네스가 400을 그대로 표에 남기므로 회귀는 감지됨.

## 9. Phase 2 — successor(합병·개명) 이벤트 (§P2-4)

엔진이 같은 규칙을 병렬 구현 중이라 이번 절도 **`--self-check` 기준**임. 33 시나리오 **244 체크 전부 PASS, 실패 0**. 서버 대조와 전기간 실행은 `data/events/events.parquet` 생성 후 리드 지시로 수행함.

### 9-1. 원장에 넣은 승계 규칙

정산 bar(= 종료일 이후 첫 NaN bar, §8-2 규칙 유지)에서:

1. **R 결정** — `explicit finite ? explicit : (승계 가능하고 mark_old 유효면 ratio·close_succ(t)/mark_old − 1, 아니면 default)`. 두 번째 경로는 `computedSettlementReturns`로 집계하며 **`defaultDelistings`에는 넣지 않음**(기본값을 쓴 게 아니므로).
2. **`pnl += h_prev·R`** — 기존과 동일.
3. **승계 가능하면 현금화 대신 이전** — `h_succ += h_prev·(1+R)`, **turnover·fee 0**, `mark_succ`는 당일 close. 승계 불가(패널 밖·당일 quote 없음·승계 대상도 같은 bar 정산·자기 자신)면 기존 현금 정산.
4. 승계분은 이후 정상 규칙 — 같은 bar의 체결에서 target과 다르면 그 차액만 turnover·fee가 붙음.

구현상 판단이 필요했던 지점:

| 쟁점 | 선택 | 근거 |
| --- | --- | --- |
| 이전 시점 | **평가(3단계) 직후**에 `h_prev`에 더함 | 정산 단계에서 바로 더하면 승계분이 승계사의 당일 수익까지 먹어 이중계상됨. 평가 뒤에 얹으면 `mark_succ`가 이미 당일 close라 "승계분은 당일 수익 0"이 자동으로 성립하고, 체결 단계의 `turnover = Σ|h − h_prev|`도 승계를 거래로 세지 않음 |
| 승계사가 같은 bar에 정산되는 연쇄 | 승계 불가로 보고 현금 정산 | 정산 집합을 먼저 확정(`post_retired`)한 뒤 대상 유효성을 판정함. 퇴출될 장부로 넘기면 즉시 사라짐 |
| `convertedGross` 정의 | **이전된 가중치** `Σ|h_prev·(1+R)|` | 실제로 승계사 장부에 올라간 크기. 기존 `settledGross`(=`Σ|h_prev|`, 이전 노출)와 짝을 이루도록 둘 다 기록함 |
| `successorConversions`·`computedSettlementReturns` 집계 게이트 | 보유분만(`h_prev ≠ 0`) | `delistings`·`defaultDelistings`와 같은 규약. 비중 0인 정산은 장부에 아무것도 남기지 않음 |
| `successorRatio`가 null | **1.0** | "구주 1주당 승계 주식 수"의 자연스러운 기본값(개명·1:1 합병) |

필수 `missingData` 키를 17 → **20키**로 늘렸음: `successorConversions`, `convertedGross`, `computedSettlementReturns` 추가(엔진이 방출하지 않으면 FAIL).

### 9-2. 시나리오 7종 추가

| 시나리오 | 내용 | 고정한 기대값 |
| --- | --- | --- |
| `successor_conversion_mode0` / `_mode1` | ratio 1, 승계사 가격 무변동 | R이 **0으로 계산**되어 default −1이 쓰이지 않음. `convertedGross=1.0`, `computedSettlementReturns=1`, `defaultDelistings=0`, **turnover·fee 0**, 이후 bar에서 승계사 +10%가 정상 수익 0.1로 잡힘 |
| `successor_ratio_two` | ratio 2, 승계사 60 | R = 2·60/100 − 1 = **0.2**, gross 0.2, 1.2가 승계사로, target 1.0이라 **초과분 0.2만 turnover**(fee 0.0001) |
| `successor_with_explicit_return` | explicit R=−0.25 + ratio 2 | explicit이 우선 → gross −0.25, `convertedGross=0.75`, `computedSettlementReturns=0`, turnover 0.25 |
| `successor_quote_missing` | 정산 bar에 승계사도 NaN | 승계 불가 → 현금 정산 default −1, `successorConversions=0`, 승계사 신규 진입도 blocked(`missingEntries=1`), 다음 bar에 정상 진입 |
| `successor_outside_panel` | events가 패널에 없는 id를 승계사로 지목 | 로더가 −1로 떨구고 현금 default 정산, `successorConversions=0` |
| `successor_then_rebalance` | 승계 후 다음 bar에 3종목 리밸런스 | 승계 bar turnover·fee **0**, 다음 bar turnover 1.0·fee 0.0005 |

합성 events parquet에 `successorTradingItemId`(string, null 허용)·`successorRatio`(double) 컬럼을 넣도록 fixture writer를 확장했음. 패널 밖 승계는 실제로 패널에 없는 문자열(`ZZZ_NOT_IN_PANEL`)을 기록해 로더 경로까지 태움.

### 9-3. 하네스 (§P2-4)

- `--universe-path {Top500|Top1000|ALL|경로}` 추가. 예제 config를 읽어 `universe.path`만 교체한 사본을 `<output>/config_<이름>.yaml`로 생성함. 이때 `datasets[*].path`·`catalog`·`universe.path`·`events.path`의 **상대 경로를 원본 config 디렉터리 기준 절대 경로로 해석**해 사본이 어디 있어도 동작하게 했음(Top500/Top1000/ALL 3종 생성 확인).
- 표에 `retirements`·`frozenGrossSum`·`postEndDateQuoteBars`·`pendingSettlements`·`successorConversions`·`convertedGross`·`computedSettlementReturns` 열 추가(키 없으면 공란), 헤더에 유니버스명·패널 크기(T·N·loadedDataBytes)·미노출 응답 키 목록 추가. 실행시간·로드 시간·RSS·peak RSS는 기존대로 기록함.
- R 민감도 항등식(default 0/−1)은 유지. 다만 **승계가 개입하면 이 항등식의 해석이 달라짐**: 승계 경로는 R을 계산하거나 explicit을 쓰므로 default를 바꿔도 영향이 없고, 차이는 현금 정산분에만 남음. 잔차 자체는 여전히 `ΔPnL = ΔdelistingPnl`로 성립해야 함.

### 9-4. 미실행·확인 필요

- 서버 대조·전기간 실행 모두 미실행(`data/events/events.parquet` 대기). 실행 시 §8-5대로 abnormal 가격 0건은 확인돼 있음.
- `convertedGross`를 이전 전(`Σ|h_prev|`)으로 정의했다면 엔진과 어긋남 — 첫 HTTP 대조에서 바로 드러남.
- 혼합 딜(현금+주식)에서 브리프는 "R은 현금+주식 합산 총수익률이므로 승계 가중치 `h_prev·(1+R)`"로 정했는데, 이 규약은 **현금 몫까지 승계사 주식으로 들어간다**는 뜻임. 현금을 실제로 빼서 잔여만 승계하려면 별도 필드(`cashPerShare`)가 필요함 — 현재 계약대로 구현했고, 실데이터에 혼합 딜이 얼마나 되는지는 events 파일 생성 후 확인 필요함.

### 9-5. `settledGross` 정의 확정 후 재대조 (리드 결정 반영)

1차 엔진 대조에서 승계 시나리오 20건이 `settledGross`(per-bar 시계열·합계) 하나만 정확히 **1.0** 차이로 FAIL했음. 원인은 §9-1 표의 정의 선택 차이임 — 원장은 승계도 "행정 정산된 노출"로 보아 `settledGross`에 넣고 `convertedGross`에 **중복** 계상했고, 엔진은 둘을 배타적으로 다뤘음.

리드 결정: **`settledGross` = 현금으로 정산된 노출만, 승계분은 `convertedGross`에만.** 원장을 이 정의로 고쳤음(승계가 실제로 일어난 정산은 `settled_gross`·per-asset `settled_gross_hist` 둘 다 건너뜀). `successor_conversion_mode0/_mode1`의 기대값도 `settled_gross 1.0 → 0.0`으로 갱신했고, 승계가 없는 `stale_settlement`의 1.0은 그대로임.

재대조 결과 (`scratchpad/orch/check_p2b.json`):

| 항목 | 결과 |
| --- | --- |
| self-check | 33 시나리오 **244 체크 전부 PASS** |
| HTTP 대조 | **150 케이스 전부 PASS** (37 시나리오 × 4 옵션 + 옵션 범위 2) |
| 수치 대조 | 118건, `missingData` **20키 전수** + 시계열 9종, 최대 절대오차 **5.51e-17** |
| 양쪽 거부 | 30건(error 정책 결측 + abnormal 가격) |
| 키 누락·길이 불일치 | **0건** |
| 승계 시나리오 | 28건 전부 PASS (`successorConversions`·`convertedGross`·`computedSettlementReturns` 포함 20키 일치) |

예: `successor_ratio_two`(ratio 2, 승계사 60) — 서버·원장 모두 `delistingPnl=0.19999999999999996`, `convertedGross=1.2`, `computedSettlementReturns=1`, `settledGross=0`.

두 정의가 배타적이 되었으므로 **"정산된 총 노출"은 이제 `settledGross + convertedGross`로 읽어야 함**. 전기간 표에서 두 열을 함께 보도록 이미 나란히 배치돼 있음. 서버는 18120~18139에서만 띄웠고 종료 확인했음(잔존 0).
