# 결측 종가·상폐 회계 구현 완료 기록 — 2026-09-11

> 역사 기록: 아래 Phase 1의 사건별 C++ 정산과 run_top500_missing_close.py 실행 절차는 thin-v1 경로로 교체되어 현재 실행 명령이 아니다. 검증 자료와 당시 수치는 재현 근거로 보존한다.

기준: HEAD `2e77af1` 위의 미커밋 변경(24개 파일 수정, 신규 6개). 리드(Claude)가 engine·data·oracle 3역할 + 독립 리뷰어를 오케스트레이션함. 계약 원문은 [missing_data_validation_2026-09-11.md](missing_data_validation_2026-09-11.md), 구현 브리프·담당별 보고·리뷰·실행 증거는 [missing_close_2026-09-11/](missing_close_2026-09-11/)에 있음.

## 1. 요약

| 항목 | 결과 |
|---|---|
| 해결한 블로커 | 주식 예제에서 첫 거래일(bar 252, Marvel 상폐)부터 모든 알파가 `invalid close` 예외로 중단되던 문제([review_2026-09-11_independent.md](review_2026-09-11_independent.md) §2) |
| 기본 동작 | `missing_close_policy: carry` — NaN 종가의 보유분 동결·복구일 전액 인식, 결측 종목 주문 불체결, 상폐는 명시 종료일 이후 **호가가 끊긴 첫 bar**에 정산(기본 R=0, 명시 R 우선). 0·음수·inf 종가는 두 정책 모두 오류. `error`는 기존 즉시 실패 |
| 검증 | CTest 25/25(test_simulator 38; HTTP 계약은 env 지정 시 별도 17/17), 독립 파이썬 원장 self-check 184/184·HTTP 대조 122/122(최대 오차 1e-16 수준), 구 엔진 대비 무결측 입력 **비트 동일**(레포 내 golden + 외부 12조합), Top500 전기간 5알파 × R{0,−1} 완주(최종 stale 0 기준 10/10; 이전 라운드 stale 30/60/120 포함 40/40) |
| 리뷰 | P0 없음, P1 1건(stale 정산의 명시 R 무시) 수정 완료, P2 13건 중 9건 처리·4건 후속 |

## 2. 회계 계약 (구현 기준)

> 이 절은 Phase 1(2026-09-11) 기준이다. 정산 수익률 우선순위(계산값 > 파일 > 기본값)·승계(주식 몫만)·진단 두 기준·`cash_col`은 §9~§10(2026-09-14)에서 갱신됐고 현행 계약은 §10과 `CLAUDE.md`·예제 README를 따른다.

bar t 순서: **정산 → intent 정규화 → 보유손익 → 체결 → turnover/fee → 통계**. warmup(`sim_start_index` 이전)은 mark·intent만 갱신.

| 규칙 | 내용 |
|---|---|
| 종가 3상태 | 유효(finite, >0) / 결측(**NaN만**) / 비정상(0·음수·±inf). 비정상은 보유·체결·intent 어느 경로든 관여하면 두 정책 모두 `runtime_error`(HTTP 400) |
| 평가 mark | 마지막 관측 유효 close. 결측 bar는 보유손익 0·mark 유지(동결), 복구 bar에 `h_prev·(close/mark−1)` 전액 인식 |
| 체결 | 유효 quote 종목만 `h = desired`. 결측 종목은 `h = h_prev`(동결), 주문은 blocked(과거 주문 누적 없음). 동결 gross F가 있으면 나머지 목표는 `max(0, 1−F)`를 넘는 부분만 비례 축소(확대 없음) |
| 정산(event) | `settlement_bar` = 종료일(일 단위 내림) 다음 첫 bar에 정산이 **열리고**, 그 bar부터 처음 호가가 없는(NaN) bar에 **닫힘**. 종료일 이후에도 호가가 있으면 확인된 호가가 우선해 정상 평가·체결하고 `postEndDateQuoteBars`로 집계, 끝까지 호가면 미정산(`pendingSettlements`). 정산 시 `pnl += h_prev·R`, R = 명시 `settlement_return`(유한) > `default_delisting_return`, 직전 mark 기준. 수수료·turnover 0, retired 영구(재진입·pending 취소) |
| 정산(stale) | 선택 휴리스틱(기본 0=사용 안 함, 주식 예제도 0): 이벤트 없는 **보유** 종목이 연속 결측 ≥ N이면 같은 방식으로 정산. 미보유 종목은 retire하지 않음. 가격 부재가 상폐를 확정하지 않으므로 종료일 없는 종목은 마스터 보정이 우선([목록](missing_close_2026-09-11/no_end_date_symbols.md)) |
| intent | carry: 유한 신호면 quote가 NaN이어도 포함(retired·마스크 제외). error: 기존 valid-close 게이트(그래서 error에서는 미보유+비정상 가격이 조용히 제외됨 — 의도된 비대칭) |
| error 정책 | 보유/체결 종목의 quote 무효 시 기존 메시지 그대로 `runtime_error`(HTTP 400). 이벤트 정산은 error에서도 적용 |
| 무결측·무이벤트 입력 | 연산 순서까지 동일 → 기존 결과와 비트 동일 |

옵션: YAML `backtest.{missing_close_policy, default_delisting_return(−1~1), stale_quote_settle_bars}`, 최상위 `events: {path, id_col, end_date_col, return_col}`; 요청 `missingClose / defaultDelistingReturn / staleQuoteSettleBars`로 override. 응답 `missingData`(missingHeldBars, blockedOrders, blockedTradeWeight, missingEntries, resumedHoldings, delistings, delistingPnl, settledGross, defaultDelistings, defaultDelistingGross, staleSettlements, cancelledDelistingEntries, terminalFrozenGross, frozenGrossSum, retirements, postEndDateQuoteBars, pendingSettlements), timeseries `frozenGross`·`settledGross`, `/api/config`에 `missingClosePolicy·defaultDelistingReturn·staleQuoteSettleBars·eventsLoaded·settlementSymbols`.

## 3. 구현

| 영역 | 파일 | 내용 |
|---|---|---|
| 엔진 | `cpp-engine/src/engine/simulator.{h,cpp}` | `PortfolioSimulation` 재작성(mark·동결·frozen budget·정산·retired), `MissingDataDiagnostics`, `AccountingRowView` 콜백, `normalize_signal_row(policy)` |
| API | `src/main.cpp`, `src/api/{json_serializer.cpp,combined_signals.h}`, `src/metrics/*` | 요청 옵션·검증, `missingData` 합계, 시계열 2종, `/api/config` 키, GA·combined 동일 정책 |
| 데이터 | `src/data/{config_loader.cpp,parquet_reader.cpp}` + 헤더 | 옵션·`events:` 파싱, secmaster 이벤트 → `settlement_bar/settlement_return`(int64/string id, timestamp·date·string 날짜, 중복 id·파일 부재 오류) |
| 예제 | `cpp-engine/examples/datasets/{equities_monthly_afl.yaml,README.md}` | `events`(secmaster)·carry·stale 0 설정, 규칙·휴리스틱 문서 |
| 테스트 | `tests/test_simulator.cpp`(+22), `test_parquet_loader.cpp`(+6), `test_config_loader.cpp`(+3), `test_http_contract.cpp`(+4), `test_json_serializer.cpp`(+2), `tests/golden/missing_close_bitwise.json` | 계약 손계산 10사례(mode 0/1, 1e-12), 비트 동일 golden, 로더 타입·경계 |
| 도구 | `cpp-engine/tools/{missing_close_oracle.py, check_missing_data.py, run_top500_missing_close.py}` | 독립 원장, self-check·HTTP 대조, 전기간 하네스(R 민감도·fee 항등식 자동 검사) |

## 4. 검증 증거

| 검증 | 결과 |
|---|---|
| CTest | 25/25 (test_simulator 38). **HTTP 계약 17건은 ctest 기본 실행에서 skip**되며 `LONGSHORT_TEST_SERVER/CONFIG` 지정 시 fixture 4종 각각 17/17 통과(별도 실행 증거) |
| 손계산 10사례 | gtest·원장 모두 계약 표 숫자 그대로 통과(코드에 맞춘 수정 없음) |
| 비트 동일 | 레포 내 golden(T=96·N=14, 6케이스 `%a` 완전 일치) + 외부 probe 2패널×3알파×mode 2 = 12조합 byte-identical, 리뷰어 독립 재현(T=1500·N=120) 10조합 일치 |
| 독립 원장 HTTP 대조 | 26시나리오(2차 규칙 반영) 122 PASS, 필수 키 17개·시계열 길이 전수 요구, error 정책 거부 케이스 문구·bar·asset 동일 |
| Top500 전기간 | 아래 표. 이전 바이너리는 10/10 첫 bar 실패 → 현재 30/30 완주 |
| 항등식 | `PnL(R=−1)−PnL(R=0) = Δ delistingPnl` 잔차 ≤ 8e-16, `cumPnlGross−cumPnl = Σ turnover·fee` ≤ 1.4e-14, `Σ frozenGross = frozenGrossSum` |
| 메모리 | 로드 후 RSS 278MB, 반복 요청 40회 후 737MB plateau(누수 없음), 멀티알파 4×5회 +5MB |

Top500 2010-01-04~2026-09-10 (n=4,197 bar, N=1,056, mode 1, 5bp, 252, **stale 0**, 최종 바이너리):

| alpha | R | PnL | Sharpe(full) | turnover/bar | delistings(보유) | retirements | postEndQuote | termFrozen |
|---|---|---|---|---|---|---|---|---|
| returns→zscore | 0 / −1 | −3.392 / −3.380 | −2.63 / −2.60 | 1.449 | 134 | 235 | 1 | 0.019 |
| −returns→tsmean5→zscore | 0 / −1 | −0.443 / −0.338 | −0.33 / −0.25 | 0.628 | 134 | 235 | 1 | 0.016 |
| close→tsmean20→zscore | 0 / −1 | −0.514 / −0.557 | −0.75 / −0.80 | 0.007 | 134 | 235 | 1 | 0.011 |
| afl_100→zscore | 0 / −1 | −0.295 / −0.082 | −0.17 / −0.05 | 0.045 | 133 | 235 | 1 | 0.012 |
| afl_963→zscore | 0 / −1 | 0.247 / 0.264 | 0.39 / 0.41 | 0.042 | 130 | 235 | 1 | 0.025 |

공통 진단: missingHeldBars 1,698(stale 60이면 819), staleSettlements 0, pendingSettlements 0, postEndDateQuoteBars 1(Wolfspeed 하루). 이 표는 회계 경로 완주 확인용이며 알파 판정 근거가 아님(t = Sharpe·√(4197/252): returns→zscore −10.7, tsmean5 −1.3, tsmean20 −3.1, afl_100 −0.7, afl_963 +1.6 — 정의된 비용 5bp·D1 하에서 대부분 음의 유의). `settledGross` 전기간 합 0.22~0.34 → R 선택의 PnL 영향 −0.044~+0.220(afl_100은 −0.296→−0.075로 75% 이동 — "무시 가능" 아님). stale 30/60/120 민감도(이전 라운드): PnL ±0.001, staleSettlements 5/5/4, terminalFrozenGross 0.4~1.4% → 0으로 끄면 1.1~2.5%.

## 5. 실데이터 이벤트 집계 (secmaster, 패널 1,056종목)

| 항목 | 값 |
|---|---|
| 종료일 있음(패널 내 정산 예정) | 235 (첫 bar 이전 종료 0, 패널 밖 −1: 821) |
| 끝결측 + 마지막 호가 유니버스 내 | 138 (그중 종료일 없음 8 → 기본 설정에서는 끝까지 동결, stale 60을 켜면 5건 정산) |
| 종료일 이후 정상 호가 | 1건(Wolfspeed 2603877, 2026-08-12 하루) — 그날 호가로 정상 평가 후 다음 결측 bar에 정산(`postEndDateQuoteBars=1`) |
| 급락형 가격 경로 | 유니버스 내 132건 중 직전 60일 −50% 또는 1년 고점 −80% 사례 **0건**. 상폐 유형(M&A/파산/기타)은 마스터에 사유 필드가 없어 **미확인** — 급락 부재는 파산형이 드물다는 정황일 뿐 확정 근거가 아님. SVB·Signature·First Republic(2023)·Silvergate·BBBY·Chesapeake는 마스터에 **부재** [확인 필요: 파산 기업 survivorship] |

## 6. 리뷰 결과와 처리

| ID | 내용 | 처리 |
|---|---|---|
| F-01 P1 | stale 정산이 명시 R 무시·defaultDelistings 오집계 | 수정 + 회귀 테스트(mode 0/1) + 원장 시나리오 |
| F-02 | README "파일 없으면 무시" ↔ 구현 throw | README 교정(설정된 파일 부재 = 오류) |
| F-03 | default R 범위 미검증 | config·생성자·요청 모두 −1 ≤ R ≤ 1 |
| F-04 | 도달 불가 폴백 분기 | 제거 |
| F-06/F-07 | 하네스 키 누락 시 조용한 PASS / 원장 F=0 게이트 | 필수 키·길이 FAIL 처리 / 게이트 추가 |
| F-08/F-09 | frozen gross 죽은 누산 / 전체 retire 건수 없음 | `frozenGrossSum`, `retirements` 방출 |
| F-10 | 비자정 종료일 문구 | README 문구·로더 테스트 추가 |
| F-11 | probe `eventsLoaded` 의미 상이 | probe 키를 `panelSymbols`로 |
| F-12/F-13 | unsigned 옵션 메시지 / policy 기본 인자 불일치 | 자체 메시지 400 / 기본 인자 제거(호출부 명시) |
| F-05 | ctest에서 HTTP 계약 17건 skip(기존) | 후속 — fixture config 등록 필요 |
| 2차 리뷰 1 | stale 60 자동 정산은 추가 가정 | 예제 기본 0, 옵션·휴리스틱 명시, 종료일 없는 8종목 목록 산출 |
| 2차 리뷰 2 | 종료일 이후 확인된 호가 무시 | 호가 우선·첫 결측 bar 정산, `postEndDateQuoteBars`·`pendingSettlements` 진단 |
| 2차 리뷰 3 | 0·음수·inf도 carry | NaN만 carry, 비정상 가격은 두 정책 모두 오류 |
| 2차 리뷰 4 | 문서 과잉 결론(전부 \|t\|<2, 사실상 M&A, CTest=HTTP 포함) | t값 실계산 교체, 유형 미확인으로 정정, HTTP 별도 실행 명시 |
| 누락 테스트 #7·#8·#9·#11·#13 | warmup stale, stale 경계, 재상장 id 재사용, 로더 타입, 실데이터 bar별 원장 대조 | 후속 |

## 7. 가정·한계·후속

- **`default_delisting_return = 0`은 가정**(M&A 현금청산 근사). 명시 수익률은 `events.return_col`로 주입 가능하나 현재 데이터에 없음. 파산 손실은 반영되지 않음 — 위 §5의 마스터 부재 문제와 함께 원천 DB에서 확인 필요.
- `stale_quote_settle_bars`는 휴리스틱이라 예제 기본 0. 켜지 않으면 종료일 없는 8종목의 보유분은 끝까지 동결(무손실 보유 가정)로 남으므로, 근본 해법은 마스터 종료일 보정.
- carry에서 끝까지 결측인 보유분은 `terminalFrozenGross`(0.4~1.2%)로 남음 — 성과 해석 시 별도 표기.
- `retirements`는 warmup·미보유 정산 포함, `delistings`는 보유분만. `settlement_bar==0`(패널 시작 전 종료)도 호가가 있으면 규칙대로 pending — 계약 문서 §2.1 문구 갱신 필요.
- `DataPanel::settlement_return`이 float32라 명시 R이 1e-8 수준으로 잘림(−0.4 → −0.40000000596). double 전환은 후속.
- 실데이터 bar별 원장 대조(리뷰 §3 #13)는 `accounting_row_callback` trace probe가 필요해 미이행 — 집계 항등식 2개만 검사됨.
- 2차 과제(별도): weight 저장/복원(저장 시 weight·결과 스냅샷, 불러올 때 즉시 표시).

## 8. 재현

```bash
# 빌드·테스트
cmake --build /tmp/longshort_module_server/build -j8 && ctest --test-dir /tmp/longshort_module_server/build --output-on-failure -j4
# 독립 원장 self-check + HTTP 대조
/home/jgo/usr/venv-3.11/bin/python cpp-engine/tools/check_missing_data.py --self-check
/home/jgo/usr/venv-3.11/bin/python cpp-engine/tools/check_missing_data.py --server /tmp/longshort_module_server/build/longshort-server --output /tmp/check_missing_data.json
# Top500 전기간 (stale 민감도는 --stale-bars N)
/home/jgo/usr/venv-3.11/bin/python cpp-engine/tools/run_top500_missing_close.py \
  --config cpp-engine/examples/datasets/equities_monthly_afl.yaml \
  --server /tmp/longshort_module_server/build/longshort-server --output /tmp/top500_run
```

## 9. Phase 2 — 원천 이벤트 파이프라인 + 승계(successor) 이벤트 (2026-09-11 저녁)

목표(사용자 지시 1~5단계): 로더 스케줄 요청서 → 마스터 종료일 갱신 → co_acthist 정산 이벤트 파일 → 엔진 successor·`return_col` → Top500/1000/ALL 전기간 검증. 6단계(OTC 재수집·캐시 v2)는 범위 밖.

| 단계 | 산출물 | 결과 |
|---|---|---|
| 1 로더 스케줄 | [xfeed_loader_schedule_request_2026-09-11.md](xfeed_loader_schedule_request_2026-09-11.md) | 관리자 요청서(loadsub 718·720·733·729·731 → schedule 22). 적용 전까지 2026-02-19 이후 사건은 override 표 |
| 2 마스터 갱신 | `cpp-engine/tools/data/refresh_master.py` → `data/master/uss_cash_master_refreshed.parquet` | 종료일 **715건** 신규(2025 382·2026 306), 기존 20,408건 불변, DB 불일치 29건은 `endDateConflict` 표시만. 8종목 전부 채움. 멱등 확인 |
| 3 이벤트 파일 | `cpp-engine/tools/data/build_events.py` → `data/events/events.parquet`(6,414행) + `events_override.csv`(EA·EXAS·AVB·EQR) | 사건 매칭 80.5%(gvkey/iid 정확 5,151), 조건 파싱 64%, `settlementReturn` 유한 3,186(49.7%), successor 1,241. 현금 딜 R 평균 −0.29%(p05 −2.1%·p95 +0.8%), \|R\|>10% 38건(1.9%)은 CVR·공개매수 할인. DFS·HES·ANSS·CYBR + override 4건 손계산 일치. 리뷰 P1 2건 반영: 환산 문구("cash equivalent of $x") 이중계상 19건 교정(Comerica +15.0%→+9.3%), 외화 딜 8건 `foreign_currency`로 제외, Duke Realty 파싱 추가 |
| 4 엔진 | `EventsConfig.{successor_col,successor_ratio_col}`, `DataPanel.{settlement_successor,settlement_successor_ratio}`, 승계 회계, JSON 3키 | 정산 bar에서 R = explicit → computed(ratio·close_succ/mark) → default, `h_succ += h_prev·(1+R)`(체결 아님, fee 0, 당일 successor move 미계상), successor 없음/quote 없음/같은 bar 정산이면 현금 폴백. `settledGross`=현금 정산만, `convertedGross`=승계분. test_simulator 43·CTest 25/25, 골든 비트동일 유지 |
| 4′ 원장 | `missing_close_oracle.py` successor 규칙, 33 시나리오 244 self-check, HTTP 대조 **150/150**(20키·오차 5.5e-17) | 정의 불일치 1건(`settledGross` 승계 포함 여부)은 "현금만"으로 고정 |
| 5 검증 | 하네스 `--universe-path` → 아래 표 | Top500·Top1000·ALL × 5알파 × R{0,−1} **30/30 완주** |

전기간 결과(2010-01-04~2026-09-10, mode 1·5bp·252, stale 0, 실제 이벤트 파일):

| 유니버스 | N | 이벤트/승계 | 보유 정산(기본 R 사용) | 승계 건수 | 동결 bar | 종료일 이후 호가 bar | 잔여 동결 | 로드 | 요청당 |
|---|---|---|---|---|---|---|---|---|---|
| Top500 | 1,056 | 262 / 72 | 138~142 (39~42) | 43~44 | 471 (이전 1,698) | 1 | **0** | 3.2s·282MB | 0.2~0.5s |
| Top1000 | 2,315 | 801 / 169 | 476~497 (92~102) | 120~126 | 1,929~2,750 | 4 | 0.3~1.3% | 3.8s·419MB | 0.4~0.7s |
| ALL | 6,398 | 2,545 / 389 | 1,419~1,794 (282~498) | 244~287 | 6k~13k | 3,505 | 0.9~6.3% | 5.8s·778MB | 1.0~1.4s |

- Top500은 종료일 보정만으로 잔여 동결이 **0**이 됐고 기본 R(=0 가정) 의존이 정산의 30%로 줄었음(나머지는 명시·계산 R). R=0/−1 민감도: Top500 PnL 차이 0.00~0.07.
- ALL의 `postEndDateQuoteBars` 3,505는 55개 사건(대부분 N/C·미매칭)에서 종료일 뒤에도 호가가 이어지는 경우 — "호가 우선" 규칙대로 정상 평가되며 마스터 종료일 품질 이슈로 기록. ALL의 가격수준 알파(close→tsmean20→zscore) PnL −8.4는 마이크로캡 극단 수익률(엔진 아닌 알파 문제).
- 이벤트 파일 규칙 중 중요한 둘: ① 가격은 `ciqpriceequity.priceclose × adjustmentfactor`(as-traded)로 통일(캐시 raw close는 분할 후 재작성됨 — GOOG 2015 사례), ② 인수사 가격은 co_acthist `datadate`가 아니라 **정산 bar**(종료일 다음 거래일) 기준 — 엔진의 mark 동결→일괄 인식 규약과 일치(DFS R=−5.7%가 정상).

남은 것: 혼합 딜(현금+주식)은 총수익률로 전액 승계하는 근사(`cashPerShare` 분리는 후속), 선택형·다종목 대가·CVR 딜은 R NaN(기본 R), `co_acthist` 7개월 지연분은 override 4건 외 미반영(로더 스케줄 적용 후 재생성), 6단계 OTC 재수집.

## 10. Phase 2c — 팀 리뷰 3건 반영: 정산 수익률을 엔진이 정산 bar에서 계산 (2026-09-14)

팀 개발자 리뷰 지적과 원인:

| # | 지적 | 원인(리드 판단) |
|---|---|---|
| 1 | 생성기가 인수사 가격을 정산일 이후 최대 10일까지 앞으로 뒤져 R을 만들고 엔진은 그 R을 더 이른 bar에 인식 → 룩어헤드 | 생성기 폴백을 리뷰 때 놓침. 근본 원인은 시점 의존 값(R)을 파일에 사전 계산해 넘긴 설계 |
| 2 | 현금+주식 혼합 딜의 현금 몫까지 인수사 주식으로 승계 → 불필요한 회전·비용 | 범위 축소용 근사로 채택했으나 명백히 틀린 회계 |
| 3 | 마스터 종료일 충돌(로컬≠DB) 29건을 표시만 하고 로컬 유지 | "기존 값 덮어쓰지 말 것" 지시의 결과 |

수정:

| 대상 | 변경 |
|---|---|
| 엔진 | `EventsConfig.cash_col` → `DataPanel.settlement_cash`. 정산 bar `t`에서 `V = cash + ratio·close_succ(t)`, `R = V/mark − 1`을 **엔진이 계산**하고 우선순위를 **계산값 > 파일 `return_col` > 기본값**으로 바꿈(파일 R은 승계자가 패널 밖인 주식/혼합 딜에서만 쓰임). **주식 몫 `h·ratio·close_succ/mark`만 승계**, 현금 몫은 실현. 진단은 두 기준: `settledGross`·`defaultDelistingGross`는 노출 기준 `\|h\|`(혼합 딜은 `\|h\|·cash/V`), `convertedGross`는 실제 이전 가중치. JSON 키 추가 없음(20개). 부수: config/데이터 로드 실패가 core dump 대신 `Cannot start: …` exit 1 |
| 생성기 | 정산 bar 이후를 보는 가격 폴백 제거, 실제 사용 날짜 `acquirerPriceDate` 기록. DB 종료일은 **트레이딩아이템의 마지막 DB 가격과 10일 이내로 맞을 때만** 채택(`ciqsecurity.securityenddate`는 증권 단위이고 최근 로드일 2026-09-11이 16,087개 증권에 롤링 마커로 박혀 있음) — 아니면 로컬 마스터, 그것도 없으면 마지막 가격. `endDateSource`·`endDateConflict`·`data/events/enddate_conflicts.csv` |
| 원장 | `missing_close_oracle.py`에 같은 규칙(계산 우선·현금/주식 분리·두 기준). self-check 277/277(37 시나리오) |

증거:

- CTest **25/25**(test_simulator 48, test_parquet_loader 20, HTTP 계약 17×fixture 7), 무결측 입력 골든 비트동일 + 외부 probe 12조합 byte-identical.
- 원장 HTTP 대조 **166/166 PASS**(수치 대조·양쪽 거절·범위 거절 합산; `phase2/p2c/check_http_166_before_s11.json`).
- 룩어헤드 독립 검사(리드, 캐시 close 기준): 인수사 가격이 있는 1,357행 전부에서 `acquirerPriceDate ≤ 엔진 정산 bar`(종료일 이후 첫 무호가 bar). 정확히 정산 bar 1,330·그 이전 27·**이후 0**. (`phase2/lookahead_check_p2c.csv`)
- 종료일 충돌 28건 판정: DB 채택 20, 로컬 복귀 8(NTST·USPI·PDCE는 DB 2026-09-11 마커 vs 마지막 가격 2007/2023, SLM·QRTE.A·TLRY·FI·GMGI는 증권이 다른 트레이딩아이템으로 이어진 경우). "DB 무조건 우선"이었다면 PDCE 등은 정산 bar가 패널 밖이 되어 영원히 동결됐을 것.
- 실이벤트 날짜별 원장 대조(오라클, 패널을 대상+승계사 2종목으로 제한·자체 마스크·fee 0·`default_delisting_return=-1`): 정산 bar의 엔진 `ret` = 손계산 R, **4/4 일치(최대 차이 2.2e-16)**. 파일의 `settlementReturn`과 7~8자리에서 갈리고 `computedSettlementReturns=1`이라 엔진이 직접 계산했음이 숫자로 확인됨. 승계 가중치는 정산 bar turnover `|1−c|`와 승계사 강제 청산 시 turnover `c`(차이 0), 다음 bar 수익 = 승계사 t→t+1 수익으로 확인.

| 종목 | 종료일 | 딜 조건 | 엔진 ret(정산 bar) | 손계산 차이 | settledGross(노출) | convertedGross(가치) |
|---|---|---|---|---|---|---|
| ANSS 2590144 | 2025-07-17 | $199.91 + 0.3399 SNPS | +0.0651080 | 2.2e-16 | 0.501442 | 0.531018 |
| DFS 34969516 | 2025-05-22 | 1.0192 COF | −0.0570681 | 0 | 0 | 0.942932 |
| EXAS 2610459 | 2026-03-23 | $105 | +0.0008578 | 0 | 1 | 0 |
| EA 2608773 | 2026-08-05 | $210 | +0.0014306 | 0 | 1 | 0 |

- 이 대조에서 원장 쪽 구멍 1건을 잡음: 승계사가 지정됐으나 그 bar에 쓸 수 없는 혼합 딜에서 엔진은 파일 R로 폴백하는데 원장은 현금 몫만으로 계산했음(ANSS 조건이면 +6.5% vs −46.6%). 엔진이 옳아 원장을 맞추고 시나리오 2종을 추가: self-check **289/289**, HTTP **174/174 PASS**(수치 대조 142 · 양쪽 거절 30 · 범위 거절 2). 계약 문구: "주식 다리가 있는데 승계사를 그 bar에 쓸 수 없으면 현금 몫만으로 값을 매기지 않고 파일 R > 기본값으로 간다".
- 전기간 하네스(2010-01-04~2026-09-11, mode 1·5bp·252, stale 0, 재생성 이벤트 파일, 5알파 × R{0,−1}): Top500·Top1000·ALL **30/30 완주**. 아래 표는 Phase 2(§9, 패널 끝 09-10) → Phase 2c(패널 끝 09-11) 범위.

| 유니버스 | N | 정산(보유) | 기본 R 사용 | 엔진 계산 R | 승계 건수 | 동결 bar | 잔여 동결 | settledGross | convertedGross | 로드·요청당 |
|---|---|---|---|---|---|---|---|---|---|---|
| Top500 | 1,056 | 138~142 | 39~42 → 40~43 | 0 → **87~88** | 43~44 | 471 → **306** | 0 → 0 | 0.16~0.21 → 0.18~0.26 | 0.06~0.14 → 0.06~0.11 | 3.2s·281MB, 0.2~0.5s |
| Top1000 | 2,315 | 476~497 | 92~102 → 95~105 | 0 → **357~368** | 120~126 | 1,929~2,750 → 1,587~2,408 | 0.2~1.3% → 0.2~1.3%(동일) | 0.30~0.34 → 0.33~0.38 | 0.09~0.17 → 0.10~0.20 | 3.9s·425MB, 0.4~0.7s |
| ALL | 6,399 | 1,420~1,795 | 282~498 → 284~502 | 0 → **1,091~1,244** | 244~287 | 6.0k~12.9k → 5.2k~12.0k | 0.9~6.3% → 0.9~6.4% | 0.40~0.64 → 0.42~0.68 | 0.09~0.28 → 0.10~0.19 | 5.6s·785MB, 0.9~1.4s |

- 동결 bar가 준 것은 충돌 20건에서 DB 종료일(더 이른 실제 종료, 예 KSU 2021-12-13 vs 로컬 2022-04-29)을 채택해 마지막 호가 직후 정산되기 때문. `settledGross`가 늘고 `convertedGross`가 준 것은 혼합 딜의 현금 몫이 승계에서 빠져 노출 기준 정산으로 옮겨간 효과. Top1000 잔여 동결(terminalFrozenGross)은 alpha별로 Phase 2와 소수점까지 동일(0.0025~0.0132) — 종료일 없는 잔여 종목은 그대로임. R=0/−1 민감도 항등식(PnL 차이 = delistingPnl 차이) 잔차 ≤ 1.3e-15.
- PnL 변화는 작음(Top500 afl_963 Sharpe 0.387→0.416, returns_zscore −2.627→−2.611; 패널 하루 추가 포함). ALL close_tsmean20 −8.4는 §9와 같은 알파 문제.
- 증거 파일: `docs/missing_close_2026-09-11/phase2/p2c/`(하네스 3종 md/json, 원장 289/174 json, 실이벤트 손계산 json, 생성기 보고서, 충돌 CSV, 리드 룩어헤드 CSV, 엔진 §9·원장 §11 보고서).

남은 것: `co_acthist` 지연분(2026-02-19 이후)은 override 4건 외 미반영 — 로더 스케줄 적용 후 `build_events.py` 재실행. AVB는 VMRK 첫 호가(08-18)가 정산 bar(08-17)보다 늦어 계산 불가 → 파일 R도 NaN이라 기본 R 사용(승계 조건은 파일에 남음). 6단계 OTC 재수집·캐시 v2. weight 저장/복원(프론트 2차).


## 11. Phase 2d — 가격 단위 정합 (2026-09-14, 팀 리뷰 추가 지적)

지적: §10의 엔진 계산 `R = (cash + ratio·close_succ(t))/mark − 1`에서 `close_succ(t)`는 **캐시 close = DB `priceclose`(이후 분할까지 재작성된 현재 기준)**이고 `cash`·`ratio`·`mark`는 당시 거래 기준이라 단위가 섞임. 리드 재현(`phase2/p2c/unit_mismatch_audit_lead.csv`, successor 이벤트 1,101건 × 캐시 close × DB `adjustmentfactor`):

| 사례 | 캐시 mark | 캐시 승계사 종가(t) | 배수(승계/대상) | 엔진 R(§10) | 정답 R |
|---|---|---|---|---|---|
| NVLS 2634982 → LRCX, 2012-06-05, 1.125주 | as-traded | 2024년 10:1 분할 반영 | 10 / 1 | −89.6% | +3.95% |
| GOOG 312940617 → 260152426, 2015-10-06, 1주 | 641.47 | 32.272(2022년 20:1 반영) | 20 / 1 | −95.0% | +0.62% |
| 2635984 → Google 11311662, 2010-02-22, 0.001주+$0.15 | 0.688 | 13.57(2014·2022 분할) | 40 / 1 | 틀림 | 파일 R +0.67% |

- 영향 범위: 대상 쪽 배수≠1 **50건**, 승계사 쪽 **151건**, 둘 중 하나 **191건**(17%). 엔진 R과 정답의 차이 >1% 169건, >10% **162건**, >50% 133건. 대상 쪽 배수는 같은 증권의 역분할·재상장(N/C)에서 나옴 — 승계사만 고치면 안 됨.
- 캐시 close가 DB `priceclose`와 같은 비율은 대상 97.5%·승계사 97.9% — 캐시 빌드 시점과 DB 기준이 다를 수 있어 DB 배수를 그냥 곱하면 안 되고 **캐시에 앵커**해야 함: `k = asTraded(DB priceclose×adjustmentfactor, 그 날짜)/cacheClose(그 날짜)`를 대상(마지막 유효 종가일)과 승계사(정산 bar)에서 각각 재서 `cashPerShareCache = cash/k_t`, `successorRatioCache = ratio·k_s/k_t`. 엔진 산식은 그대로이고 입력만 캐시 단위로 바뀜. 파일이 캐시 빌드에 묶이므로 엔진 로더가 `successorCacheClose`/`successorCacheCloseDate`로 패널 종가와 대조해 불일치 시 기동 실패(스테일 가드).
- 부수 정정: 종료일 규칙이 코드에서는 `dbEndDate ≤ lastpx+10일` 한쪽만이었음 → 문서대로 양방향 `|dbEndDate − lastpx| ≤ 10일`.
- 실이벤트 4건(§10)이 이 문제를 못 잡은 이유: ANSS·DFS·EXAS·EA 모두 2025~26년 사건이라 승계사(SNPS·COF)에 그 뒤 분할이 없음. 회귀는 NVLS·GOOG처럼 승계사가 나중에 분할한 과거 사건으로 잡아야 함.

수정:

| 대상 | 변경 |
|---|---|
| 생성기 | 각 successor 이벤트에서 `k_t = asTraded_target(마지막 종가일)/cacheClose_target`, `k_s = asTraded_succ(정산 bar)/cacheClose_succ`를 재서 `cashPerShareCache = cash/k_t`, `successorRatioCache = ratio·k_s/k_t` 등 8컬럼 추가(원 컬럼은 감사용 유지). 환산 불가(캐시/DB 가격 결측) 152건은 NaN → 엔진은 파일 R로 폴백. 종료일 규칙 양방향 `\|dbEndDate − lastpx\| ≤ 10일`(채택 건수 변화 0) |
| 엔진 | 산식 무변경. `EventsConfig.successor_price_col/successor_price_date_col` 스테일 가드: 로더가 파일의 승계사 캐시 종가를 패널 종가와 대조, 상대오차 >1e-4면 `Cannot start: Events file is stale against the price cache …` exit 1(날짜가 패널 밖이면 건너뜀). `/api/config`에 `eventsPriceChecks`·`eventsPriceChecksSkipped` 2키 추가(`missingData` 20키 유지). 예제 yaml은 `*Cache` 컬럼 + 가드 2컬럼 |

검증:

- 생성기 항등식: 전 successor 행에서 `(cashPerShareCache + successorRatioCache·successorCacheClose)/targetCacheMark − 1` = as-traded `settlementReturn`, **1,098행 최대 오차 2.2e-16**. 지목 3건: NVLS k_s=10 −0.896→**+0.0395**, GOOG k_s=20 −0.950→**+0.0062**, 2635984 k_s=40 −0.762→**+0.0067**. 영향 186건(k_t≠1 30·k_s≠1 160), 바로잡힌 |ΔR|>10% 159건.
- 리드 교차검증: 생성기의 `successorCacheClose`·`targetCacheMark`는 리드 감사 캐시값과 100% 일치. 리드 감사의 "정답 R"(캐시=DB 조정가 가정 후 DB 배수 적용)은 25행에서 생성기와 달랐는데, 전부 캐시가 DB 조정가와 다른 행(예: 옛 GOOG 아이템은 캐시가 as-traded 641.47인데 DB `priceclose`는 20:1 반영 32.07)이라 **DB 배수를 곱하는 방식이 틀리고 캐시 앵커가 맞음**을 확인한 것.
- 엔진: CTest **25/25**(test_parquet_loader 21, test_simulator 48, HTTP 17×7), 옛 엔진 비트동일 유지. fixture 스모크: 4:1 어긋난 파일 → exit 1 + 메시지, 일치 파일 → checks=1. 실데이터(Top500 예제 config) 기동: successor 72건 중 가드 70건 통과·skip 0(2건은 가드 셀 비어 미적용), `delistingPnl −0.0096`(−89%류 폭주 없음).
- 전기간 하네스(단위 보정 파일, 같은 엔진 규칙, 5알파 × R{0,−1}): Top500·Top1000·ALL **30/30 완주**, 가드 통과. §10(p2c) → 단위 보정(p2d):

| 유니버스 | 엔진 계산 R | 기본 R 사용 | convertedGross | delistingPnl(R=0) | afl_963 Sharpe | returns_zscore Sharpe |
|---|---|---|---|---|---|---|
| Top500 | 87~88 → 93~94 | 40~43 → 38~41 | 0.06~0.11 → 0.04~0.09 | −0.014~+0.056 → −0.009~+0.071 | 0.416 → 0.400 | −2.611 → −2.633 |
| Top1000 | 357~368 → 368~379 | 95~105 → 90~100 | 0.10~0.20 → 0.06~0.13 | −0.064~+0.088 → −0.006~+0.068 | 0.586 → 0.523 | −2.770 → −2.793 |
| ALL | 1,091~1,244 → 1,113~1,266 | 284~502 → 273~491 | 0.10~0.19 → 0.07~0.27 | **−0.234~+0.104 → −0.032~+0.117** | 0.399 → 0.330 | −2.473 → −2.502 |

- 동결·정산 건수(missingHeldBars·delistings·successorConversions·잔여 동결)는 p2c와 동일 — 단위 보정은 정산 bar가 아니라 정산 금액과 승계 가중치만 바꿈. ALL의 delistingPnl 하한이 −0.23에서 −0.03으로 올라온 것이 −90%류 가짜 손실이 사라진 흔적이고, 승계 가중치가 커진 곳(ALL convertedGross 상한 0.19→0.27)은 승계사 가격이 캐시에서 분할로 작아져 있던 사건들.
- 원장 실이벤트 회귀(§10 방식, DB as-traded 손계산 vs 엔진 정산 bar `ret`): 3/3 차이 0(캐시 float32 정밀도 5e-8 이내). 당시 기준 컬럼으로 계산하면 NVLS −89.6%·GOOG −95.0%·2635984 −76.2%가 나오는 것도 재현 — 단일 사건으로 북 전액에 가까운 가짜 손실이 났을 사안. 승계 가중치(turnover `|1−c|`)·승계분 mark(다음 bar 승계사 수익 일치) 확인.

| 종목 | 정산 bar | k_s | 수정 전 엔진 R | 수정 후 엔진 R | DB 손계산 |
|---|---|---|---|---|---|
| NVLS 2634982 → LRCX | 2012-06-05 | 10 | −0.8961 | +0.0394900 | +0.0394900 |
| GOOG 312940617 → 260152426 | 2015-10-06 | 20 | −0.9497 | +0.0061889 | +0.0061889 |
| 2635984 → Google 11311662 | 2010-02-22 | 40 | −0.7623 | +0.0066841 | +0.0066841 |

- 가드: `successorCacheClose`를 4배로 바꾼 복사본 → exit 1, `Events file is stale against the price cache: 2625662 2012-06-05 file=14.928000 cache=3.732000 - regenerate build_events.py`. 정상 파일은 `eventsPriceChecks=1 / eventsPriceChecksSkipped=0`.
- 원장 HTTP 대조 **174/174 PASS**(수치 대조 142 · 양쪽 거절 30 · 범위 거절 2), self-check 289/289 — 원장은 한 줄도 바꾸지 않았음(회계 규칙이 아니라 입력 단위 문제였다는 증거). 원자료 `phase2/p2c/check_p2d.json`, `oracle_report_s12.md`.
- 남은 것: 합성 fixture에 `successor_price_col`을 선언해 가드 happy-path를 HTTP 대조에 태우기, 가드는 승계사 쪽만 검사하므로 대상 쪽 배수(k_t≠1, 30건)는 재생성 규약에 의존, 캐시 재구축 시 `build_events.py` 재실행 필수.

## 12. Phase 2e — 이벤트 파일 계약 v3: 구조/평가 분리·양쪽 가드·실패 판정 (2026-09-14, 팀 리뷰 `review_phase2d_2026-09-14.md`)

리뷰 R1~R4의 공통 원인은 이벤트 파일이 **경제적 사실**(딜 구조·원 조건·as-traded R)과 **엔진 평가 입력**(캐시 단위 환산값)을 한 컬럼 집합에 섞어 두고, 엔진이 "값이 있느냐"로 "다리가 있느냐"를 추정한 것. 계약 v3는 셋을 분리한다.

| 그룹 | 컬럼 | 규칙 |
|---|---|---|
| 구조(사실) | `hasCashLeg`, `hasStockLeg` | 원 조건에서 유도. 승계사 id가 없어도 교환비를 지우지 않음 |
| 평가 입력(캐시 단위) | `cashPerShareCache`, `successorRatioCache` | **선언된 다리를 전부 환산했을 때만** 값 존재(하나라도 실패면 둘 다 NaN). DB 누락 시 대체값·k=1 승인 금지, override도 같은 측정 경로 |
| 평가 상태 | `cacheUnitStatus` | ok / no_successor_id / no_cache_successor_at_settlement / no_db_* / rejected:* |
| 가드 | `targetCacheMark/Date`, `successorCacheClose/Date` | 승계사 측은 **정산 bar 당일**만(대체 금지), 대상 측은 마지막 유효 종가 |
| 폴백 | `settlementReturn` | as-traded, 정산 bar 이전 가격만 |

엔진: `stock_leg_col`/`cash_leg_col`(구조 플래그) → 현금만 계산은 `hasStockLeg=false`일 때만, 혼합 딜의 승계사를 매길 수 없으면 파일 R > 기본값(R1). 가드 v2는 로더가 이벤트별 엔진 정산 bar를 패널에서 계산해 **사용되는 이벤트는 warmup·조회 기간과 무관하게 반드시 검사**(R2), 대상 측 가드 추가, 기준값 NaN/Inf 거절. 도구: 하네스 exit code·항등식 임계·가드 검사 건수 필수, 원장 대조는 HTTP 400+메시지 일치만 거절 PASS, `--self-test`로 리뷰 반례 고정(R3). 불변식 ①~⑥은 생성기 verify()와 리드 독립검사(`check_events_contract_v3.py`)가 이중으로 확인.

결과:

| 항목 | 결과 |
|---|---|
| 이벤트 파일 v3 | 6,413행·29컬럼(최종 sha256 180289d8…). 리드 독립검사 불변식 ①~⑥ + 가드 날짜=정산 bar + 대상 앵커=마지막 유효 종가 **전부 0건 위반**(`phase2/p2e/contract_v3_check_lead.txt`). `cacheUnitStatus`: ok 3,066 · no_terms 2,568 · no_successor_id 366 · no_cache_successor_at_settlement 169 · rejected 222 · no_db_target 22. hasStockLeg 1,815 · hasCashLeg 2,316 · 둘 다 286. 생성기 verify() 항등식 주식 1,062 + 현금 2,004 최대 오차 2.2e-16, `--self-test` 18케이스(R4 경계·R1 경계·해피패스). 승계사 id 없는 혼합 21건(리뷰 지목 4건 포함) 원 조건 보존·평가 입력 NaN. override도 측정 경로(AVB k_t=0.99999999 측정값). 부수: 벤더 오기 `FOR $11.10 SHARES`를 교환비로 읽던 7건 제거 |
| 엔진(R1) | 구조 플래그로 다리 판정, 현금만 계산은 `hasStockLeg=false`일 때만. IWA 12678069를 파일→로더→엔진으로 재현: ret **−0.010884**(파일 R), 현금만 −0.5258 아님, computed 0. test_simulator 50 · test_parquet_loader 26 · CTest **25/25**(리드 재실행), 옛 엔진 비트동일 |
| 엔진(R2) | 가드 v2: 로더가 이벤트별 정산 bar를 패널에서 계산, 사용 이벤트는 warmup과 무관하게 같은 판정(리뷰 케이스 past_guard_date 0/1 동일), 앵커 NaN/Inf 거절, 대상 앵커 추가, 미사용 이벤트만 건너뜀. 실데이터 가드 검사(최종 파일) Top500 **179** · Top1000 **594** · ALL **1,707**, 건너뜀 0, 실패 0 |
| 도구(R3) | 하네스: 어느 run이든 OK 아니면 exit 1, 항등식 잔차 >1e-9·bars 0·가드 0 → FAIL, `--self-test` 10 probe. 원장 대조: 거절 PASS는 HTTP 400 + 같은 bar·asset 메시지만, 요약 kinds 분리(numeric/both_reject/range_reject), `--self-test` 8 probe. 리뷰 반례 4종 전부 FAIL로 잡힘. 원장 self-check 295/295(NaN ratio→1.0 기본값 폐지, 혼합+ratio 미상 시나리오 추가) |
| 전기간 하네스 | Top500·Top1000·ALL **30/30, exit 0**(최종 파일 sha256 180289d8…로 재실행, 가드 검사 179/594/1,707·건너뜀 0). §11(p2d) 대비: Top500 computed 93~94 → 87~88, 기본 R 38~41 → 40~43(혼합 딜 6건이 현금만 계산에서 파일 R로), delistingPnl(R=0) 하한 −0.0095 → **−0.0013**; Top1000 −0.0060 → −0.0002; ALL 거의 불변. 승계 수치·동결·잔여 동결 불변. afl_963 Sharpe Top500 0.400 → 0.391, Top1000 0.523 → 0.522, ALL 0.330 → 0.330 |

- 원장 HTTP 대조 **178/178 PASS**(수치 146 · 양쪽 거절 30 · 범위 거절 2, 최대 오차 5.5e-17) — 엔진 E-2 전 12건 FAIL(혼합+ratio 미상 3 시나리오)이 전부 해소. IWA 12678069 실이벤트: 승계사 28090336이 캐시에 없어 `no_cache_successor_at_settlement`, 엔진 ret = 파일 R **−0.010884**(차이 0), computed 0·default 0(현금만 계산이면 −52.6%).
- 남은 한계: `cacheUnitStatus≠ok`이면서 정산이 패널 안인 행은 검사할 앵커가 없어 `eventsPriceChecks`/`Skipped` 어디에도 안 잡힘(집계 불완전, 세 번째 카운터 후보). `hasStockLeg`인데 교환비·승계사 id가 모두 없는 347행은 주식 대가 사실만 남고 R은 파일 R > 기본값(파싱 개선 대상). 전기간 하네스는 최종 파일(10:54)로 재실행해 아래 갱신.

## 13. Phase 2f — 앵커 필수·가드 카운터 3분류·기본 R 정산 영향·백필 (2026-09-14)

팀 리뷰 후속 3건과 사용자 결정("데이터가 없으면 우리가 찾아서 채운다").

| 항목 | 처리 |
|---|---|
| 로더 앵커 필수 | 평가 입력(`cashPerShareCache`/`successorRatioCache`)이 있으면 대상 앵커 필수, 교환비가 있으면 승계사 앵커도 필수 — 없으면 기동 실패. 가드 컬럼 없이 캐시 단위 입력을 쓰는 구성도 거부 |
| 가드 카운터 3분류 | `eventsPriceChecks`(검사 완료) / `eventsPriceChecksOutOfRange`(정산 bar 패널 밖) / `eventsPriceChecksNotEvaluable`(평가 입력 없음). 합 = 패널 매칭 이벤트 수를 도구가 항등식으로 검사(Top500: 179 + 0 + 83 = 262) |
| 정산 로그 | 요청 옵션 `settlementLog: true` → 거래 기간·보유 정산의 {symbol, time, bar, exposure, return, source, cashShare, stockShare, successor, transferred}. 항등식 Σexposure·return = delistingPnl, Σ\|exposure\|(default) = defaultDelistingGross |
| override 근거 필수 | `source ∈ {sec_8k, filingdata, press_release, co_acthist_text, web_news}`, `confidence`, `evidence_url`, `entered_on` 없으면 생성기 거부. override도 같은 k 측정·가드·불변식 경로, 파일에 `overrideSource` 기록. 절차 `cpp-engine/tools/data/README_events_backfill.md` |

**기본 R 정산의 실제 영향(Top500, 5알파, 정산 로그 기준)**: 보유 정산 노출의 source 분포 computed 0.864 · **default 0.391(29%)** · explicit 0.112. default 사유별: `no_successor_id` 27종목 64.8% · `no_terms` 9종목 19.2% · `no_cache_successor_at_settlement` 5종목 10.8% · `rejected:cash_R_out_of_range` 2종목 5.2%. R=0 대 −1 손익 스윙은 알파당 +0.015~+0.030 — returns_zscore(PnL −3.4)엔 1% 미만이나 afl_963(PnL 0.25)엔 10% 이상이라 알파별 결론을 바꿀 수 있음. 부호가 전부 양(+)인 것은 default 대상의 순노출이 숏 우위라 기본값 선택이 한 방향으로 치우친다는 뜻. 즉 347행은 "계산 버그가 아니라 데이터 한계"가 맞지만 Top500에서도 무시할 크기가 아님.

백필 순서: ① `no_successor_id`는 벤더 문구가 인수사 대신 대상을 적고 교환비를 생략하는 패턴이 많아(FDC→Fiserv, XLNX→AMD, LLL→Harris, AMTD→Schwab…) 인수사 이름 → DB 트레이딩아이템 자동 해석을 생성기에 추가, ② 교환비·현금은 근거 조사(co_acthist 원문 > filingdata/8-K > 보도자료 > 뉴스)로 override 기입, ③ 선택형 대가(EITHER … OR …)는 기입하지 않고 `unresolved:election`으로 목록화해 정책 결정 대기(BNI·PCYC·NLC·ANDV·TFCF·FRX 등 큰 건이 여기 속함).

**세 유니버스 영향(정산 로그, 백필 전)**: default 노출 비율 Top500 **28.6%** · Top1000 **20.7%** · ALL **32.8%** — 유니버스를 넓혀도 줄지 않음. 지배 사유가 뒤바뀜: Top500은 `no_successor_id` 64.8%, ALL은 `no_terms` 60.9%(대형주는 인수사 id 매칭, 전체는 조건 파싱이 병목). 손익 스윙(R=0 vs −1)은 afl_100 기준 Top500 23.7%·Top1000 30.6%로 결론을 바꿀 수준이고 ALL에서는 일부 알파의 부호가 음으로 뒤집혀 **"기본 R=0이 보수적"이라는 일반화는 불가**. 워크리스트 합본 505종목(Top500 43·Top1000 106). 자원: peak RSS 0.9/1.1/1.8GB.

**리드 실측으로 잡은 결함(수정 중)**: 예제 config를 `end_date: 2018-12-31`로만 바꿔 기동하면 `OutOfRange=52`로 구분은 동작하지만 `Checked 116 + OutOfRange 52 + NotEvaluable 79 = 247 ≠ eventsLoaded 227` (`phase2/p2f/subperiod_2018_api_config.json`). 원인은 리드가 추정한 이중 계상이 아니라 **분모**였음: 버킷은 이미 행당 하나였고, `eventsLoaded`가 `settlement_bar≥0 ∨ explicit R ∨ 해결된 successor`로 세어져 종료일이 패널 밖이고 둘 다 없는 매칭 행 20건이 분모에서만 빠짐(전기간엔 그런 행이 0이라 우연히 일치). 수정: `eventsLoaded` = 이벤트 파일에서 행을 찾은 패널 심볼 수(`events_rows_matched`), `settlementSymbols` = 그중 정산이 패널 안에서 열리는 심볼 수로 정의를 고정하고 `/api/config`·README에 명시. 6bar 패널 fixture로 전기간(2/0/2=4)·부분 기간(1/1/2=4) 항등식을 테스트에 고정. 재실측: 2018 config `247 matched, 117/52/78` 합 247 ✓, 전기간 `262, 179/0/83` ✓. CTest 25/25(test_parquet_loader 28).

**백필 1차(Top500 워크리스트 43건) 결과**:

| 수단 | 결과 |
|---|---|
| 승계사 자동 해석(생성기) | 후보를 정산 bar 당일 캐시 가격 유무로 걸러 유일할 때만 채택(여럿이면 `ambiguous`). 파일 전체 `no_successor_id` **366→187**, 승계사 id 1,447→1,630(gvkey 매칭 1,187·ambiguous 46·미국 라인 없음 101). EMC→DVMT, CVH→AET, ETFC→MS가 이것만으로 `ok` |
| 파서 추가(ALL `no_terms` 대상) | `POST-SPLIT SHARE`(12), `N OF AN <회사> ADS`(3), `FOR EVERY THREE SHARES HELD`+영문 수사(50). PGN 0.87083 DUK 자동 해결. 파서로 불가한 유형 전수: `RECEIVED STOCK SHARES OF …`(교환비 원문에 없음) 368 · `NO PER SHARE TERMS`(SPAC·역합병) 748 · `EITHER/ELECT`(선택형) 331 |
| 근거 조사 override 19건 추가(총 23행, SEC 8-K/DEFM14A 또는 co_acthist 원문) | FDC 0.303 FISV · KSU $90+2.884 CP · XLNX 1.7234 AMD · LLL 1.30 LHX · AMTD 1.0837 SCHW · WORK $26.79+0.0776 CRM · ESRX $48.75+0.2434 CI · WP $11+0.9287 FIS · S 0.10256 TMUS · TSS 0.8101 GPN · AGN $120.30+0.8660 ABBV · APC $59+0.2934 OXY · ALXN $60+2.1243 AZN · BUCY $92 · TYC $6.06+0.8263 JCI · DPS $103.75+1.0 KDP · AMB 1.0 PLD · CCE $10+1.0 신 CCE · LINT.A 1.0. 23행 중 21행 `ok`, R −7.8%~+5.8%(FDC +0.05%, APC +0.08%, BUCY 0.00%) |
| 기입하지 않은 것 | 선택형 15건(`unresolved:election`: PCYC·BNI·NLC·ANDV·TFCF.A·FRX·SUG·PBG·EP·EPB·KMP·BRCM·VMW·SNI·VMED, 현금·주식 옵션과 프로레이션은 CSV note) — 정책 결정 대기. 승계사가 캐시 밖 5건(RGP→ETP, PDE→Valaris, ACMP→신 WPZ, AA 역분할+스핀+개명, ALXN→AZN GBP 라인) — 조건은 알지만 패널에 가격 없음 |
| 합계 | Top500 워크리스트 **22/43 해결, 노출의 52.5%**. 파일 `cacheUnitStatus=ok` **3,066→3,091**, `hasStockLeg`인데 교환비·승계사 모두 없음 347→53. 리드 독립검사 불변식 전부 0건 위반(sha256 89e6843a…), 룩어헤드 0/1,587, `--self-test` 26/26, CTest 25/25 |

**백필 1차 후 재측정(오라클, 하네스 3종 exit 0, HTTP 178/178)**:

| 유니버스 | default 종목 | default 노출 비율 | 비고 |
|---|---|---|---|
| Top500 | 43 → **29** | 28.6% → **19.7%** | `no_successor_id` 27→15, `no_cache_successor` 5→2, `rejected:cash_R` 2→0 |
| Top1000 | 106 → 100 | 20.7% → 19.6% | 소폭 |
| ALL | 505 → 513 | 32.8% → 33.0% | 미세 악화 |

- 종목 단위 추적이 사유별 추이보다 정확했음: 해결 30 · **신규 유입 38** · 사유만 바뀐 120(그중 `no_successor_id→no_terms` 107 = 승계사는 풀렸는데 교환비가 없는 행 → 상태를 `no_ratio`로 분리하도록 지시). 신규 유입 38(TWC·MEE·BXLT·MWE·COV 등, 전부 `no_successor_id`)은 리졸버의 "정산 bar 캐시 가격 있는 유일 후보" 필터가 기존 gvkey 정확 매치까지 버린 회귀 — gvkey 매치는 절대 삭제하지 않고 캐시 필터는 후보가 여럿일 때만 쓰도록 수정 지시(2차 배치와 함께).
- **손익 스윙은 줄지 않았음**: afl_963(Top500) +0.0108→+0.0245로 2배, close_tsmean20(Top500)·returns_zscore(Top1000)는 부호 반전. default 종목 수 감소를 손익 리스크 감소로 읽으면 안 되며, 남는 default 노출의 방향(롱/숏 우위)이 스윙을 결정함.

**회귀 수정 + 2차 배치(Top1000 상위 20) 결과**:

| 항목 | 결과 |
|---|---|
| 리졸버 회귀 수정 | gvkey/iid 정확 매치는 절대 삭제하지 않음. 4단 티어(USD∧마스터∧정산 bar 가격 → USD∧마스터 → USD → 전체)로 좁히되 비면 다음 티어로 내려가 항상 id를 냄, 여럿이면 티커 → 점수. 1차에서 `no_successor_id`가 된 **113행 전부 복구**(동일 108·개선 3·변경 2). TWC `ok`(+0.79%)·COV `ok`(−1.26%), MEE·BXLT·MWE는 id 유지 + `no_cache_successor_at_settlement` + 파일 R(엔진 explicit 폴백). `phase2/p2f/resolver_regression_check.csv` |
| 상태 `no_ratio` 신설 | 주식 다리 선언·승계사 있음·교환비 없음 **308행**을 `no_terms`(2,551)에서 분리 |
| 2차 배치 20종목 | **13건 해결(배치 노출 67.8%)**: 리졸버만으로 LPS·WCRX·FTO, override로 XEC 4.0146 CTRA · LSI 0.895 EXR · GTLS $210 BKR · TERP 0.47625 BEPC · VER 0.705 O · LVGO $11.33+0.5920 TDOC · INXN 0.7067 DLR · RAX $32 · CQH 0.4750 LNG · NBL 0.1191 CVX(전부 SEC 8-K). 미해결 7: 선택형 2(TMX·SUN), 승계사 캐시 밖 4(AZEK·CPX·TLCR·WBS), GRUB 1(JET ADS 라인 캐시에 없음) |
| override 누계 | 35행(근거 필수 스키마), 그중 31행 `ok`, 4행은 승계사가 패널 밖(AVB·ALXN·WBS·GRUB) |
| 파일 전후(Phase 2e 시작 → 최종) | `ok` 3,066 → **3,125**, `no_successor_id` 366 → **41**, `no_terms` 2,568 → 2,551 + `no_ratio` 308, `no_cache_successor_at_settlement` 169 → 146. 합본 워크리스트 505건 중 `ok` 43건(노출 17.8%; Top500 52.5%·Top1000 36.4%·ALL 2.9%) |
| 검증 | 생성기 verify() 불변식(평가 가능 3,125, 항등식 주식 1,118 + 현금 2,007, 최대 오차 2.2e-16), 룩어헤드 0/1,711, `--self-test` 27/27. 리드 독립검사 최종 파일(sha256 fe27c21e…) **0건 위반**(`phase2/p2f/contract_v3_check_lead_after_backfill2.txt`) |

**최종 게이트(오라클, 최종 파일 sha256 fe27c21e…)**: HTTP 대조 **178/178**(수치 146·양쪽 거절 30·범위 거절 2, settlementLog 16건 차이 0), 하네스 Top500·Top1000·ALL **30/30 exit 0**, 항등식 전부 성립, 카운터 합 = `eventsLoaded`(262/801/2,545). 가드 검사 207/635/1,753, NotEvaluable 55/166/792.

| 3시점 | Top500 | Top1000 | ALL |
|---|---|---|---|
| default 노출 비율(백필 전 → 1차 → 최종) | 28.6% → 19.7% → **12.7%** | 20.7% → 19.6% → **12.0%** | 32.8% → 33.0% → **29.9%** |
| `no_successor_id` 종목 | 27 → 15 → **0** | 56 → 35 → **3** | 148 → 77 → **8** |
| `no_ratio`(신설, 승계사 있음·교환비 미상) | 14 | 35 | 122 |
| `no_cache_successor_at_settlement` | 2 → 4 | — | 10 → 17 |
| afl_100 손익 스윙(R=0 vs −1) | +0.0699 → +0.0339 | +0.0678 → +0.0438 | −0.0350(부호 유지) |

- 종목 단위 추적: 1차→최종 **해결 51 · 신규 유입 0**, 회귀 38종목(TWC·MEE·BXLT·MWE·COV)과 IWA 전부 default 이탈. ALL이 1차에 악화됐다 회복한 것이 회귀 수정 효과.
- 남은 것: 합본 워크리스트 462종목(Top500 21·Top1000 71). 상위는 PCYC·AVB·BNI·AA·NLC·RGP — 선택형(정책 대기)과 승계사 캐시 밖(캐시 커버리지, 6단계 OTC/캐시 v2)이며, Top500 잔여 다수는 `no_ratio`라 교환비 파싱·조사가 다음 타깃. ALL은 `no_terms`(SPAC·역합병 748건 등)가 남아 기본 R 선택이 여전히 손익을 수 % 움직임 — "기본 R=0" 가정을 문서·보고에 계속 명시.
- 증거: `phase2/p2f/`(하네스 3시점 md/json, HTTP json, 정산 로그 기반 워크리스트 3시점, override 최종 CSV, 백필 결과 CSV, 리졸버 회귀 검증 CSV, 리드 독립검사 3회, 엔진 §12·§13·오라클 §14·생성기 보고서).

## 14. Phase 2g — 백필 35행 정확성 감사·선택형 규칙·전후 손익 (2026-09-14, 팀 합의)

기능 추가를 멈추고 백필 입력의 정확성을 먼저 확인한다. 엔진이 옳아도 합병 조건이 틀리면 모든 테스트를 통과하면서 손익이 틀리기 때문이다(TYC: co_acthist의 프로레이션 문구 `$6.06 + 0.8263`은 JCI 주주 조건인데 Tyco 주주에 적용됨. Tyco 주주는 병합비율 0.955 신주만 받았고, 마지막 캐시 종가가 이미 신주 기준일 가능성까지 있어 0.955를 그대로 곱하면 이중 반영. R이 우연히 −7.8%로 비슷해 검증을 통과했음).

| 단계 | 내용 | 담당 |
|---|---|---|
| 1 | TYC 정정: 대가의 주어(대상 주주)·마지막 가격의 병합 반영 여부·정산일을 1차 출처로 확정 | 검토(독립) → 생성 |
| 2 | 나머지 34행 5항목 대조(종목 id / 대가의 주어 / 가격 단위·연속성 / 적용일 / 1차 근거), 행별 confirmed·corrected·unresolved. 근거 부족은 '해결 완료'에서 제외 | 검토(독립, 기입 담당과 분리) |
| 3 | 선택형 대가 규칙 = **미선택 주주의 최종 배정**(합병계약 non-election 조항 + 프로레이션 결과). 확인 안 되면 미평가. 일괄 현금 옵션·평균 배분율 불채택 | 생성 |
| 4 | 정정 전후 손익(정산일 손익·승계 가중치·현금 몫)을 정산 로그로 종목별 비교, 전기간 3유니버스 회귀 확인 후 엔진·도구·문서 3커밋 | 오라클 → 리드 |

override에 정정 이력(`supersedes`, history CSV)과 `election_rule` 열을 추가하고, 확인된 조건은 계산·확인 못 한 조건은 기본 R 사용 사실과 손익 영향을 드러내는 것까지를 이번 범위로 한다.

**독립 감사 결과(35행, `phase2/p2g/audit_override_35.csv`·`audit_report.md`)**: confirmed **17** / corrected **17**(조건·id 5, 종료일만 12) / unresolved **1**(INXN).

| 유형 | 건 | 내용 |
|---|---|---|
| 대가의 주어 오류 | 1 | **TYC**: `$6.06+0.8263`은 JCI 주주 프로레이션 블렌드(6.06/34.88=0.1737→0.8263). Tyco 주주는 0.955-for-1 병합만, 현금 0(8-K Item 2.01, 2016-09-06). 캐시 TYC 라인은 이미 신주 단위(as-traded/0.955)이고 09-06 bar는 JCI print → 캐시 단위 ratio 1.0, R −7.8% → **−3.4%** |
| 승계사 id 오류 | 4 | GRUB(JET 보통주 OTC → ADS 639739529), ALXN(AZN 보통주 → Nasdaq ADS 2591301), AVB(VMRK → EQR 2610085로 체인), WBS(공란 → SAN ADS 2592842). 캐시 라인이 없어 파일 R만 산출 |
| 종료일 늦음 | 12 | 마지막 실제 거래일보다 1~16 bar 늦어 stale mark 후 늦은 정산(ESRX +4.6%→+0.6%, AMTD +5.8%→−0.4%, LVGO −14.4%→−8.7%). 원인은 DB `securityenddate` 우선 규칙 |
| 정책 필요 | 1 | INXN: 0.7067은 응모 주주 조건, 미응모는 청산배당+15% 원천세(424B3). 팀 규칙(미선택 주주 처리)으로 처리 지시 |
| 휴리스틱 | — | 패리티 이상 4건(TYC 0.95·TERP 0.668은 분할조정치로 정상·FDC·INXN), \|R\|>10% 3건, 승계 라인 전부 NYSE/Nasdaq·USD. medium 2행(TYC·LVGO) 8-K로 확인 |

**리드 결정 — 종료일 캡 규칙**: 종료일 늦음은 개별 오류가 아니라 규칙 문제임. 파일 전체에서 `endDate > 마지막 캐시 종가일`인 평가 가능 행이 **922/3,133**(중앙값 1일, 75% ≤3일, 최대 645일). 생성기에 `endDate = min(endDate, targetCacheMarkDate)`를 전 행 적용(`endDateOriginal` 보존, 출처에 캡 표시)하도록 지시. 이 변경은 922건의 정산 bar를 앞당기므로 전후 비교의 예상 변경 목록에 포함.

**정정 적용(생성)**: corrected 17행 전부 `supersedes`/history(19행)로 정정, 감사 기대 R과 16/17 일치. INXN은 미응모 주주의 세후 청산배당액 확인 불가 → override에서 제거·`unresolved:election_default_unknown`. 종료일 캡 규칙 전면 적용: **1,580행** 캡(중앙 3일·p75 15일·최대 10,106일; db 1,499·local 75·override 6), 캡 후 `endDate > 마지막 캐시 종가일` 0건, `endDateOriginal` 보존(스키마 31열).

**TYC 최종 판정(리드)**: 생성 담당은 0.955 유지(누적 −5.5%)를 주장했으나 기각. 캐시 TYC 라인은 전 구간 신주 단위(`c = P_old/0.955`)라 09-02 캐시 47.738이 이미 신주 1주 가격이고, 경제적 정답은 구주 1주 = 0.955 신주 → `0.955×47.22/45.59 − 1 = −1.1%`. 엔진 경로(endDate 09-02 → 09-06 호가 48.90 우선 보유 +2.4% → 09-07 정산 mark 48.90, V = 1.0×47.22 → −3.4%)의 누적이 정확히 −1.1%. 일반 규칙으로 고정: **`k_t`는 종료일 이하 마지막 캐시 종가일에서 측정**(대상 라인이 종료일 뒤 호가를 이으며 기준이 바뀌는 경우를 처리), 가드 앵커는 마지막 유효 종가 유지.

**선택형 17건(팀 규칙 = 미선택 주주의 최종 배정)**: 기입 10건 — BNI $100 · NLC $38.80 · TFCF.A 0.4517 DIS · FRX $26.04+0.3306 · PBG $18.031+0.32546 · EPB $4.65+0.9451 · KMP $10.77+2.1931 · BRCM $51.4829+0.0242 · VMW $142.50 · SNI $65.82+1.0584(전부 `election_rule=non_election_default` + 계약 조항 인용). 미기입 7건 — PCYC(미응모분 2차 선택 결과 미공시)·ANDV(배분율 "approximately 87%")·EP(워런트 다리)·VMED(다중 클래스)·SUG·TMX·SUN(승계사 라인 캐시 밖). 2차 독립 검토 진행 중.

override 44행(ok 41), 평가 가능 3,161행, `--self-test` 35/35, 변경 id 목록 1,603행(enddate_capped 1,574 · corrected_enddate 12 · election_added 10 · corrected_terms 5 · 기타 2) — 목록 밖 변경 0건.

**k_t 규칙 재생성(생성)**: `k_t`를 종료일 이하 마지막 캐시 종가일(`cacheUnitFactorTargetDate`, 스키마 32열)에서 측정. TYC: ktDate 09-02, k_t 0.955, mark 48.90(09-06), **ratioCache 1.0, R −0.034356**(감사 기대치와 일치). 이 규칙으로 값이 바뀐 행 8건 — TYC 외 7건은 마지막 종가일엔 DB 가격이 없고 ktDate엔 있어 `no_db_target → ok`(15건 남음). 평가 가능 3,161 → **3,167**. 파일 R 분모를 `cacheMark×k_t`로 맞춰 불변식 ⑤ 유지(최대 오차 2.2e-16), `--self-test` 36/36(TYC 형태 합성 케이스 추가). 변경 id 목록 1,610행(enddate_capped 1,574 · corrected_enddate 12 · election_added 10 · kt_date_rule 7 · corrected_terms 4 · TYC 1 · 기타 2).

**리드 게이트(파일)**: 독립검사 불변식 8종 **0건 위반**(sha256 8251b792…, `phase2/p2g/contract_v3_check_lead.txt`), CTest **25/25**(엔진 변경 없음).

**정정 전후 손익(오라클, 기준선 = Phase 2f 최종 정산 로그, `phase2/p2g/before_after_comparison.md`)**: HTTP 178/178, 하네스 3종 30/30 exit 0(카운터 합 = eventsLoaded 262/801/2,545; 가드 검사 217/645/1,768). `--compare-log` **PASS** — 허용 목록 1,610종목 밖의 구조 변경 0건.

| 항목 | 값 |
|---|---|
| 정산 로그 변경 | 25,256행 / 1,585종목. 목록 1,610종목 중 **944종목은 로그 변화 없음**(보유 0·패널 밖) — 파일 변경 건수와 백테스트 영향 건수는 6배 차이 |
| 정산일 손익 차이 합 | **−0.0509** = election_added −0.0669 · enddate_capped +0.0163 · corrected_terms −0.0093 · removed(INXN) +0.0081 · corrected_enddate +0.0019 |
| default 노출 비율(Phase 2f 최종 → 2g) | Top500 12.7% → **5.7%**(21→9종목), Top1000 12.0% → **10.3%**(71→60), ALL 29.9% → **29.5%**(462→450). Phase 2e 시작 28.6% 대비 1/5 |
| 지목 6종목 | TYC return −0.0782 → **−0.0344**; AVB default → computed, 0 → −0.0340; GRUB 0 → +0.0392; ESRX·AMTD·LVGO는 정산 bar가 앞당겨짐(2019-01-15→2018-12-21, 2020-10-15→2020-10-06, 2020-11-13→2020-10-30) |

- 도구에서 잡은 구조적 사실: fixed-book gross 1 정규화 때문에 한 종목의 정산일이 바뀌면 **동시 보유 종목 비중이 전부 미세 재배분**됨(919종목 exposure 최대 차 5.3e-05, 손익 +0.000054). "바뀌지 않아야 할 종목" 전제가 정규화 장부에선 성립하지 않으므로 비교 도구는 구조 변경(source·return·정산일)과 정규화 파급(exposure만)을 분리하고 파급 1e-3 초과만 회귀로 봄(self-test 28).

**선택형 2차 독립 검토(`phase2/p2g/audit_election_17.csv`, `audit_report.md` §2)**: 기입 10행 **confirmed 7 / 종료일만 정정 3(PBG 02-26·SNI 03-06·VMW 11-21) / 조건·주어 오류 0** — 전부 미선택 주주 전용 배정, 평균 블렌드 없음(BNI·NLC·VMW 현금 100%, FRX·KMP·EPB·SNI·TFCF.A는 Mixed 바구니로 안분 면제, BRCM만 미선택자 직접 안분 $51.4829+0.0242, PBG는 예비 결과라 medium 타당). TYC 산술 독립 확인: 캐시 +2.43% 보유 후 정산 −3.44% = 누적 **−1.09%** = DB as-traded 실보유자 −1.09%; 생성기 0.955안은 4.4%p 과소 → 기각 타당. 미기입 7건 중 5건 타당, **PCYC는 기입 가능**(No Election = Mixed 안분 면제 → $152.25 + 1.6639 ABBV), **SUN은 승계사 id 오류**(2017 라인 → 2012 ETP 라인 9185265). 경고 2건: BRCM은 AVGO 10:1 분할 조정치라 `successorRatioCache` 0.242여야 함(k_s로 자동 처리 확인 대상), **TFCF.A 03-19 캐시 −28.7% 갭은 Fox Corp 스핀오프 분배**로 BaseData에 조정 시계열이 없어 보유자에게 가짜 손실이 계상됨 — 이벤트 파일로 못 고치는 캐시 한계(6단계 캐시 v2 과제). 마지막 생성 라운드(endDate 3·PCYC·SUN·SUG/TMX 통일 기입·BRCM 확인·TFCF.A 기록) 진행.

**마지막 생성 라운드(2차 검토 반영)**: endDate 3건 정정(PBG R +0.0116·SNI +0.0124·VMW +0.0001, 감사값 일치), PCYC 기입($152.25+1.6639 ABBV, 계약 §1.1(c)(vii) No Election=Mixed·안분 면제, R +0.0038), SUG 1.0 ETE·TMX 1.4899 RTO ADS·SUN $25+0.5245 ETP(9185265로 정정) 통일 기입(캐시 라인 부재 → 파일 R 폴백), BRCM `successorRatioCache` **0.242** 확인(k_s=10), TFCF.A 스핀오프 갭은 생성기 보고서 §7-2 "캐시 한계"에 기록. override **48행**(이력 22), 선택형 17건 최종 **기입 14 / 미기입 3**(ANDV·EP·VMED). 파일 `ok` 3,168, 룩어헤드 0/1,699, `--self-test` 36/36, 변경 id 1,612행. 리드 독립검사 최종 파일(sha256 9c36ed6b…) **0건 위반**.

**최종 게이트(오라클, 최종 파일 sha256 9c36ed6b…, `phase2/p2g/before_after_comparison_final.md`)**: 하네스 3종 **30/30 exit 0**(카운터 합 = eventsLoaded 262/801/2,545, 가드 검사 218/646/1,769), `--compare-log` **PASS** — 허용 목록 1,612종목 밖 구조 변경 0건, 정규화 파급은 1차와 동일(918종목, 최대 5.3e-05). 2차 정정은 기존 정정을 건드리지 않음(다른 유형의 손익 차이가 소수점 6자리까지 1차와 동일). 정산일 손익 차이 합산(3유니버스×5알파×2가정, 전략 손익 아님) −0.0509 → **−0.0534**(audit2_enddate PBG·SNI·VMW −0.0244, audit2_added +0.000003; `election_added`가 10→7종목으로 보이는 건 3건이 audit2_enddate로 재분류된 것). PCYC default→computed(+0.0038), SUN default→explicit(+0.0238), SNI는 정산이 77일 앞당겨져 보유 손익이 통째로 달라진 최대 사례.

| default 노출 비율 | Phase 2e 시작 | 2f 최종 | 2g 1차 | **2g 최종** |
|---|---|---|---|---|
| Top500 | 28.6% | 12.7%(21종목) | 5.7%(9) | **4.1%(7)** |
| Top1000 | 20.7% | 12.0%(71) | 10.3%(60) | **9.4%** |
| ALL | 32.8% | 29.9%(462) | 29.5%(450) | **29.3%** |

**판정(철회, 2026-09-14 밤 팀 리뷰 3건)**: 위 "커밋 조건 충족"은 아래 세 지적을 반영하기 전의 판정이라 철회함. ① 비교 게이트가 승계사 id·주식 이전량·현금/주식 배분 변경을 구조 변경으로 거절하지 않아(허용 목록 없이도 PASS) "허용 목록 밖 구조 변경 0"은 승계까지 보증하지 않음 → 구조 필드 확장·반례 3종 self-test. ② 종료일 일괄 캡은 과도함(LAZR.Q 105일·옛 ATVI 5,573일·TNTP.F 10,106일: 수집 중단·아이템 변경·거래 종료를 가격 유무로 구분 못 함) → 근거 있는 정정(감사 15건) 유지, 캡은 기록일 근접(추정) 또는 5일 이내(추정)만, 그 밖은 원 종료일 + `enddate_gap` 검토 표시. ③ 위 −0.053은 3유니버스×5알파×2가정의 정산 손익 차이 **합산**이지 전략 손익 변화가 아님 → 유니버스·알파·R별 표와 보유손익 포함 전체 PnL 변화로 다시 제시. 세 건 반영·재실행 후 판정을 다시 씀.

**① 게이트 보완 결과(오라클, `phase2/p2g/before_after_comparison_gate2.md`; 3차 리뷰로 필수 비교 필드 9종을 추가해 한쪽 로그에 `successor`/`source` 열이 없으면 머지 전에 FAIL, self-test 33/33)**: `successor`·`cashShare`·`stockShare`(차 >1e-6)를 구조 변경으로 승격, `transferred`는 `transferred/exposure` 비율로 비교(절대값 비교에선 진짜 이전 비중 변화가 정규화 파급에 묻혔음). 리뷰 반례 3종(승계사 id 1→99 / 이전 비중 0.008→0.5 / 주식→현금)이 허용 목록 없이 전부 FAIL, self-test 31/31(outer merge의 int→float 승격으로 −1 vs −1.0이 다르게 비교되던 버그도 잡음). 재판정 PASS: 허용 목록 밖 구조 변경 0, 구조 변경 17,474행/649종목, 승계사 id가 실제로 바뀐 180행/6종목(GRUB·ALXN·AVB·WBS·SUN) 전부 포착.

**③ 셀별·전체 PnL(전략 손익 기준, `before_after_run_pnl_gate2.csv`)**: 정산일 손익 차이는 유니버스×알파×R 표로만 제시. 보유손익 포함 전체 PnL 변화(30 run)의 최대는 **Top500/afl_100 R=−1: PnL −0.0264, Sharpe −0.015**. 변화가 R=−1 실행에 몰려 있고(R=0은 대부분 1e-4 이하, R=−1은 1e-2 수준) 정산일 차이와 전체 PnL 차이가 R=−1에서 일치(−0.026430 대 −0.0264) — 이번 정정의 가치는 "기본값으로 −100%를 먹던 정산"을 줄여 R 민감도 폭을 좁힌 데 있으며, 정산 외 경로로 새는 변화는 없음.

**② 종료일 캡 3분기 규칙(생성, 파일 sha256 7624a2f8…)**: 일괄 캡 폐지. (a) co_acthist **기록일(datadate)**이 마지막 캐시 종가일과 ±5일 이내면 캡 **1,292행**(당초 `corroborated`로 명명했으나 팀 리뷰대로 이는 효력일 확인이 아닌 **기록일 근접 추정**이며, 61행은 승계사 평가일이 기록일보다 앞섬 → `record_date_match`로 재명명·표시), (b) 갭 5일 이내면 캡 `+capped_estimate` **96행**, (c) 그 밖은 **원 종료일 유지** + `endDateGapDays` + `reviewFlag=enddate_gap` **184행**(중앙 61일·p75 413일·최대 10,106일; >365일 52건은 아이템 변경/식별자 재사용 의심, 60~365일 40건은 수집 중단 의심, ≤60일 92건은 절차 지연). override 48행은 캡 대상에서 제외(근거 기반 날짜를 가격 커버리지가 덮지 않음). 스키마 34열, `--self-test` 40/40, 평가 가능 3,166. 리드 독립검사 **0건 위반**. 이 규칙으로 일괄 캡 대비 184행이 원 종료일로 복귀했고 그중 평가 가능은 13행.

**최종 게이트(3분기 파일 sha256 7624a2f8…, 보완된 게이트, `phase2/p2g/before_after_comparison_final.md`·`before_after_run_pnl_final.csv`)**: 하네스 3종 **30/30 exit 0**(카운터 합 = eventsLoaded, 가드 218/646/1,767), `--compare-log` **PASS**(허용 목록 밖 구조 변경 0, 구조 변경 17,086행). 일괄 캡 대비 변경 규모 축소(변경 종목 1,586→1,215, 파급 918→560). flagged 184건의 실제 영향은 14종목/288행, 정산일 손익 차이 합산 −0.0019(= **−19bp**, 30개 실행의 단순 합이라 단일 전략 영향이 아님). 실행별로 보면 정산일 기여 최대는 ALL/neg_returns_tsmean5 R=−1 −12.5bp, ALL/returns_zscore R=−1 −8.5bp이고 Top500·Top1000은 대부분 0 — **ALL 유니버스에만 실질적으로 걸림**. 실행 전체 PnL 이동 최대 +0.0034(ALL/afl_100), PnL 대비 비율 최대 2.09%(ALL/neg_returns_tsmean5 R=−1, PnL 0.0736). 정산일 기여와 실행 PnL 이동의 부호가 어긋나는 실행이 다수인데 정산일이 옮겨지면 그 사이 보유손익도 달라지기 때문이며, 그래서 정산일 합계를 전략 영향으로 쓰지 않음. 앞선 "무시 가능" 서술은 취소: 부호·순위는 불변이나 ALL의 저PnL 알파에선 2% 수준까지 움직임로 결과를 뒤집지 않으며, estimate 96건의 손익 기여는 0 — 근거가 약한 캡을 flagged로 옮겨도 손익 손해 없음. default 노출 비율 Top500 **4.1%**·Top1000 **9.4%**·ALL **29.4%**.

| 전체 PnL 변화(기준선 Phase 2f 최종 → 2g 최종, 보유손익 포함, run 단위) | 값 |
|---|---|
| 최대 변화 | Top500/afl_100 R=−1: PnL **−0.0264**, Sharpe −0.015 |
| R=0 실행 | 대부분 \|Δ\| ≤ 1e-3 |
| R=−1 실행 | 1e-2 수준 — 정정이 바꾼 것은 주로 "기본값 −100%를 먹던 정산" |
| 정산일 차이 vs 전체 PnL 차이 | R=−1에서 일치(정산 외 경로로 새는 변화 없음) |

**판정(재작성)**: 팀 리뷰 3건 반영 후 게이트 통과. 이번 정정의 가치는 개별 손익을 크게 바꾼 것이 아니라 **R 민감도 폭을 좁힌 것**(Top500 default 노출 28.6%→4.1%)이며, 남은 default 노출(ALL 29.4%: SPAC·역합병 `no_terms` 2,550, 승계 라인 캐시 부재, 선택형 미확정 3건)과 flagged 종료일 184건, 스핀오프 미조정 갭(TFCF.A)은 기본 R 사용 사실·손익 영향과 함께 문서화해 6단계와 후속 배치로 넘김. 커밋 3개(엔진·도구·문서) 제안. 남은 것은 데이터 한계로 문서화됨 — 승계 라인이 캐시에 없는 건(AVB·ALXN·WBS·GRUB·SUG·TMX·SUN 등, 파일 R 폴백), 스핀오프 미조정 갭(TFCF.A 2019-03-19 −28.7%), SPAC·역합병 조건 부재(ALL `no_terms` 2,550), 선택형 미확정 3건(ANDV·EP·VMED). 이들은 기본 R 사용 사실과 손익 영향(위 표)으로 드러내며 6단계(캐시 v2)와 후속 백필 배치로 넘긴다.

### 14-끝. 완료 기록 — 검증된 범위와 추정의 구분 (2026-09-14, 3차 리뷰 반영)

"회계 오류가 더 없다"고 단정하지 않는다. 아래는 이번 라운드에서 **검증된 것**, **추정으로 남긴 것**, **미확정으로 표시한 것**의 경계다. 최종 파일 sha256 `081bfa39…`(35컬럼; 직전 `7624a2f8…` 대비 라벨·`endDateBasis`·`reviewFlag`만 추가, 평가 입력 7필드는 전 6,413행 동일 — 필드별 해시 일치, 하네스 재실행 불필요). 리드 독립검사 0건 위반, CTest 25/25, 엔진·하네스·생성기 프로세스 0(`process_check.txt`).

| 구분 | 범위 | 근거 |
|---|---|---|
| **검증됨** | override 48행(`endDateBasis=confirmed`): 1차 출처(8-K·DEFM14A·합병계약) + 독립 감사 2회(35행 5항목, 선택형 10행) 통과 | `audit_override_35.csv`, `audit_election_17.csv`, 이력 22행 |
| **검증됨** | 엔진 회계 규칙: 구조 플래그·캐시 단위·앵커 필수·가드 카운터 항등식·정산 로그 항등식 | CTest 25/25, HTTP 178/178, 원장 self-check 295, 하네스 30/30 exit 0 |
| **검증됨** | 비교 게이트: 승계사·현금/주식 배분·이전 비율·필수 필드 누락을 거절(반례 self-test 33) | `before_after_comparison_final.md`, 허용 목록 밖 구조 변경 0 |
| **추정** | 종료일 캡 `record_date_match` 1,292행: co_acthist **기록일** ±5일 근접(효력일 컬럼은 xfeed에 없음). 그중 312행(전체)·92행(이 등급)은 승계사 평가일이 기록일보다 앞섬(`reviewFlag=succ_valued_before_record`) | 생성기 보고서 §7-1 |
| **추정** | 종료일 캡 `gap_estimate` 96행: 갭 ≤5일. 손익 기여 0 | 동상 |
| **미확정** | `original`·flagged 184행: 갭 >5일이고 기록일로 뒷받침 안 됨 → 원 종료일 유지. 실행별 영향은 ALL의 저PnL 알파에서 최대 2.09%(정산일 기여 최대 −12.5bp), Top500·Top1000은 대부분 0 | §14 본문 |
| **미확정** | 기본 R 사용 노출: Top500 4.1%·Top1000 9.4%·ALL 29.4%(SPAC·역합병 `no_terms` 2,550, 승계 라인 캐시 부재, 선택형 미확정 ANDV·EP·VMED) | 정산 로그 source 분포 |
| **캐시 한계** | 스핀오프 미조정 갭(TFCF.A 2019-03-19 −28.7%), 은퇴 id에 후속 종가가 붙는 아티팩트(WOLF·TYC) | 생성기 보고서 §7-2 |

후속 과제: 효력일은 `filingdata`/8-K에서 별도 수집해야 `record_date_match`를 `confirmed`로 올릴 수 있음; flagged 184행은 아이템 변경/수집 중단/거래 종료를 식별자 이력(`sec_idhist`)으로 분류; 캐시 v2(6단계)에서 OTC·스핀오프 조정 반영.
