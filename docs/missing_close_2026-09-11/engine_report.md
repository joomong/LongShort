# engine 담당 구현 보고 (2026-09-11)

브리프 §2·§4·§5 구현 완료. 링크 깨짐(`normalize_signal_row` 5인자 미정의) 해소했고, 전체 ctest 25/25 + HTTP 계약 17/17 통과임. 구(舊) 엔진(HEAD 2e77af1)과 청정 패널에서 **비트 동일**을 별도 probe로 실증했음.

## 1. 변경 파일

| 파일 | 변경 |
| --- | --- |
| `cpp-engine/src/engine/simulator.cpp` | 전면 재작성. `PortfolioSimulation`이 정산→intent→보유손익/mark→체결→통계 순으로 진행. `normalize_signal_row` 5인자 정의, 생성자에 settlement 벡터·default R 검증 추가 |
| `cpp-engine/src/engine/simulator.h` | `MissingDataDiagnostics::stale_settlement_count` 1줄만 추가(§1 허용 범위). 그 외 공용 인터페이스 무변경 |
| `cpp-engine/src/main.cpp` | `RunOptions`에 `missing_close_policy`/`default_delisting_return`/`stale_quote_settle_bars`, `parse_run_options` 검증, `simulator_config` 전달, `missing_data_payload()`, `/api/config` 5개 키, 기동 시 정책 해석·이벤트 집계 로그 |
| `cpp-engine/src/api/json_serializer.cpp` | `timeseries_to_json`에 `frozenGross`/`settledGross` 배열 (항상 방출) |
| `cpp-engine/src/api/combined_signals.h` | `OrderedSignalAccumulator(data, mode, policy)` — combined 정규화도 같은 정책 사용 |
| `cpp-engine/src/metrics/timeseries_output.{h,cpp}` | `TimeseriesOutput.frozen_gross/settled_gross` + `compute_timeseries` 뒤쪽 optional 포인터 2개 |
| `cpp-engine/src/metrics/metrics_engine.{h,cpp}` | 위 2개 인자 통과만 |
| `cpp-engine/tests/test_simulator.cpp` | 신규 `MissingDataAccounting` 14케이스(+474줄). 기존 4개에 `Error` 정책 지정 1줄씩(아래 §4-a) |
| `cpp-engine/tests/test_http_contract.cpp` | 신규 4케이스(config 키·missingData·combined/GA 옵션·잘못된 옵션 400) |
| `cpp-engine/tests/test_json_serializer.cpp` | 신규 2케이스(시계열 키 항상 존재, start_index 오프셋) |

`tests/test_metrics_oracle.cpp`·`tests/test_combined_alpha.cpp`는 **손대지 않음**(뒤쪽 기본인자·기본 정책 덕에 무변경 통과). `src/data/*`·`tools/*`·예제 YAML은 미접촉임.

## 2. 핵심 결정

| 항목 | 결정 | 근거 |
| --- | --- | --- |
| bar 처리 순서 | 정산 → intent 정규화 → 보유손익/mark → 체결/turnover/fee → 통계 | retired가 intent에서 빠져야 하므로 정규화를 정산 뒤로 둠 |
| retired의 intent 제외 | `normalize_signal_row` 시그니처 유지. retired 자리만 NaN으로 덮은 signal 복사본을 넘김. retired가 하나도 없으면 복사 없이 원본 포인터 직행 | §1 인터페이스 불변 + 청정 입력 비트 동일 |
| frozen budget | `budget = max(0, 1−F)`; `F == 0`이면 스케일 계산 자체를 생략 | Σ\|target\|이 부동소수로 1을 미세 초과해 축소가 걸리는 사고 방지. `1−F<0` 부호 반전도 0 클램프 |
| 손익 누산 순서 | 단일 누산기에 정산항(오름차순) → 보유항(오름차순) | 이벤트 없으면 옛 `holding_return`과 항·순서가 동일 → 비트 동일 |
| mark | 마지막 관측 유효 close. 정산 시 `mark *= (1+R)` (R=−1이면 0) | 헤더 주석 "an event's final mark is its settlement value, and can be zero" |
| stale 정산 | `policy==Carry && settlement_bar<0 && bars>0 && 연속결측(당일 포함) ≥ bars && carried≠0 && 직전 관측 mark 존재` | **리드 결정 반영(2026-09-11)**: 목적이 "동결된 보유분 해제"이므로 보유 게이트 추가. 미보유 종목은 N bar 넘게 결측이어도 retire 안 되고 호가 재개 시 재진입 가능. mark 조건은 유지(보유⇒mark 존재라 이제는 방어적 중복) |
| 정산 카운터 | `delisting_count`/`settled_gross`/`default_*`/`stale_*`는 `h_prev≠0`일 때만 증가. 보유 0이어도 retired 전이는 발생 | 헤더 "held positions settled on this bar", "Counts refer to affected nonzero bookkeeping weights" |
| 카운터 포함관계 | `missing_entry_count ⊂ blocked_order_count` (신규 진입도 blocked에 포함) | 차집합은 뺄셈으로 복원 가능, 역은 불가 |
| `pending_delisting_entry_count` | 정산 bar에 실행 intent가 살아 있던 종목 수 = 취소된 pending. mode 1에서만 구조적으로 발생 | mode 0은 정산 후 정규화라 항상 0 |
| Error 정책 | trading bar에서 `!retired && !quote && (carried≠0 \|\| desired≠0)` → `runtime_error`(기존 메시지 유지). warmup 미검사, 이벤트 정산은 Error에서도 적용 | 브리프 §2-8 |
| 시계열 전달 | `compute_timeseries/compute_full_metrics`에 `const std::vector<double>*` 2개를 뒤쪽 기본인자로 | metrics가 engine 헤더에 의존하지 않게, 기존 호출부 무변경 |
| YAML 정책 문자열 | 기동 시 해석, 미인식 값은 carry 폴백 + stdout 경고. 요청에 **명시된** 값만 엄격 검증(400) | 설정 오타로 모든 요청이 400 되는 걸 방지 |
| `eventsLoaded` | 엔진은 `DataPanel`만 보므로 = `settlement_bar≥0` 또는 유한 `settlement_return`을 가진 심볼 수. `settlementSymbols` = `settlement_bar≥0` | 패널 밖 종료일도 −1이라 원본 행수와 구분 불가 (§4-b) |
| HTTP 키 | 브리프 12개 키 전부 + `cancelledDelistingEntries` 1개 추가 | 목록 외 추가이므로 확인 필요 |

## 3. 테스트 결과

| 스위트 | 개수 | 결과 |
| --- | --- | --- |
| `ctest` 전체 | 25 | 25 통과 / 0 실패 (3.5s) |
| `test_simulator` | 43 (기존 14 + 신규 29) | 전부 통과. 계약문서 손계산 10사례 + stale(조건/보유게이트/explicit R 3건) + warmup-event + 옛엔진 golden 비트동일 + Error정산 + 차원/범위검증 + 리뷰 §3 누락분 4건, 대부분 mode 0/1 양쪽 |
| `test_json_serializer` | 12 (기존 10 + 2) | 통과 |
| `test_http_contract` | 17 (기존 13 + 4) | 합성 fixture 6종(무이벤트 / 이벤트 / 결측 30bar / null종료일+explicit R / 종료일 이후 호가 / successor 승계) 각각 17/17 통과. env 없으면 skip은 정상 |
| `test_metrics_oracle` / `test_combined_alpha` | 7 / 9 | 무변경 통과 |

손계산 대조는 전부 `run_from_signals` + `accounting_row_callback` 원장으로 h·trade·mark·retired·진단·ret·turnover를 1e-12로 비교했음. 신규 14케이스는 **첫 실행에서 전부 통과**(기대값을 코드에 맞춰 고친 곳 없음).

### 비트 동일 실증 (브리프 §2-9)
`git archive HEAD`로 구 엔진을 별도 트리에 빌드하고 동일 알파를 `%a` 16진으로 덤프해 `cmp` 했음.

| 패널 | 알파 × mode | 결과 |
| --- | --- | --- |
| fixture T=2192 N=4 | 3알파 × mode 0/1 = 6조합 | `ret/turnover/max_weight/long/short/position_count` 전 bar 완전 동일 |
| 합성 T=4000 N=500 | 2알파 × mode 0/1 | 완전 동일. 실행 0.47–0.51s(구/신 차이 없음), RSS ~204MB 동일 |

보유 게이트 반영 후 재실행해도 8조합 전부 동일함(stale은 `stale_quote_settle_bars>0`에서만 동작하므로 기본 경로 무영향).

트리·probe: `/tmp/lsparity/{old,new}_build/parity_probe`, 소스 `…/scratchpad/parity_probe.cpp` (레포 밖 임시물, 27MB).

### 서버 스모크 (전부 종료 확인, 포트 18101/18102 free)
- 이벤트 fixture: `eventsLoaded=2 settlementSymbols=2`, 2023-06-16 explicit R=−0.25(settledGross 0.2) / 2024-03-02 default R=0(0.375), `delistingPnl=−0.05`; 요청 `defaultDelistingReturn:-1` → −0.425 (= 0.2·−0.25 + 0.375·−1) ✓
- 결측 fixture(B 30bar NaN): carry → `missingHeldBars 30 / blockedOrders 29 / resumedHoldings 1`, `frozenGross` 30bar 비영(최대 0.2); `missingClose:"error"` → HTTP 400 `"Cannot value or execute position: invalid close at bar 852, asset 1"`; `staleQuoteSettleBars:10, defaultDelistingReturn:-1` → stale 1건, `delistingPnl −0.2`
- `timeseries.frozenGross/settledGross` 길이 = `timestamps` 길이, 합계 = `missingData.settledGross` 항등식 성립(테스트로 고정)

## 4. 미해결 / 애매한 점

**a. 기존 test_simulator 4개를 한 줄씩 고쳤음 — 유일하게 "무변경 통과" 지시와 충돌한 지점.**
`SimulatorConfig.missing_close_policy` 기본값이 헤더에서 `Carry`(§1 변경 금지)라, 옛 fail-fast 의미에 의존하던 아래 4개는 Carry로는 구조적으로 통과 불가임. **기대값은 한 글자도 안 바꾸고** `cfg.missing_close_policy = MissingClosePolicy::Error;` 한 줄씩만 추가했음(브리프 §5 "Error 정책 기존 예외 유지" 해석).

| 테스트 | Carry에서 깨지는 이유 |
| --- | --- |
| `InvalidHeldCloseIsAnErrorInBothExecutionPaths` | 보유 중 결측은 이제 동결이라 throw 없음 |
| `MissingExecutionCloseCannotCreateAPosition` | 결측 진입은 blocked entry라 throw 없음 |
| `ExcludedHeldAssetWithMissingLiquidationCloseStillFails` | 위와 동일 |
| `MissingUnheldAssetDoesNotPoisonReturns` | Carry는 quote 없는 A를 intent에 포함 → B 비중이 1.0이 아니라 0.5가 되어 `ret[2]`가 0.1이 아닌 0.05 |

마지막 건은 단순 예외 문제가 아니라 **계약 자체의 귀결**임(계약문서 "blocked entry" 사례 그대로: intent는 잡히지만 h=0이라 예산이 비게 됨). Carry 기본값을 유지할지, 아니면 엔진 기본값을 Error로 두고 서버만 carry를 넘길지는 리드 판단 필요함.

**b. `/api/config`의 `eventsLoaded` 정의.** `settlement_bar == -1`이 "이벤트 없음"과 "패널 밖 종료"를 구분 못해 원본 parquet 행 수를 복원할 수 없음. 현재는 패널에 매칭된 이벤트 심볼 수임. data 담당이 정확한 로드 행수를 `DataPanel`에 노출하면 그쪽으로 교체하는 게 맞음.

**c. stale 정산 — 리드 결정으로 해소됨(2026-09-11).** `carried≠0` 보유 게이트를 추가해, stale 정산은 동결된 보유분만 해제하고 미보유 종목은 결측이 N bar를 넘어도 retire 하지 않음(호가 재개 시 재진입 가능). oracle 담당 원장 규칙과 일치함. 회귀 테스트 `StaleSettlementReleasesHoldingsButNeverRetiresAnUnheldName`(mode 0/1)에서 (i) 미보유 종목 4bar 결측 후 80에 재진입·retired 0·frozen_gross 0, (ii) 같은 패널에서 보유분은 기존대로 2번째 결측 bar에 정산(gross −1, turnover 0, 이후 재진입 없음)을 고정했음. 부작용으로 warmup에서는 보유가 항상 0이라 stale 정산이 아예 발생하지 않음. mark 조건은 보유⇒mark 존재라 이제 중복이지만 방어적으로 유지했음.

**d. `AccountingRowView.desired_weights`는 frozen-budget 스케일 *이전* 원 intent임.** 스케일 후 값은 `weights`로만 노출됨. oracle 담당이 스케일 계수를 직접 검증하려면 `weights/desired` 비로 역산해야 함.

**e. warmup bar의 `SimulatorOutput.missing_data`는 항상 0으로 둠**(보유가 0이라 모든 카운터가 구조적으로 0). retired 전이만 반영됨. 콜백은 warmup에도 매 bar 호출되며 `trading_enabled=false`로 구분됨.

**f. 실데이터 전기간 실행·sensitivity는 미수행**(브리프상 oracle 담당 §6 범위).

## 5. 리드가 확인할 것

1. ~~기존 test_simulator 4개 수정~~ — 리드 승인 완료 (§4-a), 변경 없음.
2. ~~HTTP `cancelledDelistingEntries` 키~~ / ~~`eventsLoaded` 정의~~ / ~~`OrderedSignalAccumulator` 기본값~~ — 리드 승인 완료, 변경 없음.
3. **`eventsLoaded`**는 data 담당이 실제 로드 행수를 `DataPanel`에 노출하면 그때 교체하면 됨(현재 정의는 승인됨).
4. ~~stale 정산의 미보유 종목 retire~~ — 리드 결정 반영 완료 (§4-c). 추가 확인 불필요함.
5. HTTP 계약 테스트 2건을 fixture 비의존으로 바꿨음(결측 있는 fixture에서 `missingClose:"error"`가 400을 내는 게 정상이라, "0이어야 한다" 대신 항등식·포함관계·정책 일관성으로 검증). 결측 30bar fixture에서도 17/17 통과함.
6. 비트 동일 증빙을 직접 재확인하려면 `/tmp/lsparity/{old,new}_build/parity_probe CONFIG XML MODE` 출력을 `cmp` 하면 됨.

---

## 6. 독립 리뷰 반영 (2026-09-11)

리뷰 보고서 `scratchpad/orch/review_report.md` §1·§3 중 엔진 담당분 처리 결과임. 리드가 직접 고친 F-02·F-05·F-10·F-11 및 config_loader 측 F-03은 손대지 않았음.

| ID | 처리 | 파일:줄 | 고정 테스트 |
| --- | --- | --- | --- |
| **F-01 (P1)** | 수정. `event_now && has_returns && …` → `has_returns && …`. 유한한 explicit R은 event/stale **양 경로에서 우선**하고, 그때 `default_delisting_count/gross`는 증가하지 않음 | `simulator.cpp:143-150` | `StaleSettlementUsesAnExplicitReturnWhenTheEndDateIsNull` (mode 0/1): null end date + R=−0.4 + stale=2 → `delistingPnl = −0.4·h_prev`, `defaultDelistings=0`, `staleSettlements=1`, mark 60. 실서버(18103)에서도 `delistingPnl/settledGross = −0.400000006`(float 저장값) 확인 |
| **F-03** | 수정. 생성자와 `parse_run_options` 모두 `유한 && −1 ≤ R ≤ 1` 검사, 위반 시 `invalid_argument` / 400 `"defaultDelistingReturn must be a total return in [-1, 1]"` | `simulator.cpp:339-343`, `main.cpp:250-255` | `RejectsASettlementReturnOutsideATotalReturn`(−1/−0.25/0/1 허용, −1.0000001/−5/3/inf/NaN 거부) + HTTP `InvalidMissingCloseOptionsAreRejected` |
| **F-04** | 수정. 도달 불가 carry 폴백 제거. 미인식 값이면 stderr 한 줄 후 `return 1`(config_loader가 먼저 던지므로 실제로는 방어선) | `main.cpp:394-399` | — (도달 불가 경로라 테스트 없음) |
| **F-08** | 수정. 죽은 누산을 `frozenGrossSum`(bar별 frozen gross의 합 = asset-bar 가중 노출 총량)으로 방출 | `main.cpp:326` | HTTP `SimulateReportsMissingDataTotalsAndFrozenSeries`가 `Σ timeseries.frozenGross == frozenGrossSum` 항등식 검사 |
| **F-09** | 수정. `MissingDataDiagnostics::retirement_count` 추가(보유 여부·event/stale 무관, warmup 포함) → JSON `retirements`. warmup bar도 `missing_data`에 기록하고 합계는 bar 0부터(그 외 카운터는 warmup에서 구조적으로 0이라 값 불변) | `simulator.h:16`, `simulator.cpp:152,98-101`, `main.cpp:304-327` | `RetirementCountSeesUnheldAndWarmupSettlements`(warmup 정산 1 + 미보유 정산 1 = retirements 2, delistings 0) + HTTP `delistings ≤ retirements` |
| **F-12** | 수정. `parse_uint_option()` 하나로 `threads`·`collectSignalsTopK`·`signalPlotMaxCells`·`staleQuoteSettleBars`·`popSize`·`generations`를 int64로 받아 상한 비교 후 자체 메시지 | `main.cpp:64-77` | HTTP: 4개 키에 5e9 입력 → 400이고 본문에 키 이름 포함·`"LargestInt"` 미포함 |
| **F-13** | 수정. `normalize_signal_row`·`OrderedSignalAccumulator`의 policy 기본 인자 **제거**. 호출부 전부 명시(`simulator.cpp`, `combined_signals.h`, `main.cpp`, `test_combined_alpha.cpp` 7곳) | `simulator.h:85`, `combined_signals.h:19` | 컴파일 강제(기본값 누락 시 빌드 실패) |

### 리뷰 §3 누락 테스트 반영

| # | 항목 | 추가한 테스트 |
| --- | --- | --- |
| 1 | stale + explicit R | `StaleSettlementUsesAnExplicitReturnWhenTheEndDateIsNull` (F-01) |
| 2 | 옛 엔진 대비 비트 동일을 **레포 안에** 고정 | `tests/golden/missing_close_bitwise.json`(26KB) + `ReproducesThePreChangeEngineDumpBitForBit`. 합성 패널(T=96 N=14, 유니버스 churn, 결측·이벤트 없음)을 테스트 코드가 정수연산+단일 나눗셈으로 결정적으로 생성하고, 6케이스(signal 변형 3 × mode 0/1, simStart 0/3/6)의 `ret`·`turnover`를 `%a` 문자열로 **완전 일치** 비교. golden은 HEAD 2e77af1 엔진에 같은 생성기를 붙인 probe가 만들었음. panel/signal FNV-1a digest도 함께 검증해 생성기가 바뀌면 명확히 실패함. 변이 검사(golden 한 줄 조작)로 실제 탐지됨을 확인 |
| 3 | `default_delisting_return` 범위 | `RejectsASettlementReturnOutsideATotalReturn` (F-03) |
| 4 | 두 종목 동시 동결 frozen budget | `TwoSimultaneousFreezesShareOneShrunkBudget` (mode 0/1): F=0.6 공유, 축소 후 h=(0.4,0.2,0.3,0.1), turnover 0.2/fee 0.0001, 동시 복구 gross −0.18, net −0.1806, 전 bar gross ≤ 1 |
| 5 | F ≥ 1 → 예산 0 평탄화 | `AFullyFrozenBookLeavesNoBudgetAndFlattensTheRest` (mode 0/1): F=1.0에서 유효 quote 종목이 0으로 평탄화(turnover 0), 절반 복구 시 정확히 절반만 배정 |
| 6 | 결측 중 유니버스 이탈 | `UniverseExitDuringAGapFreezesTheHoldingPermanently` (mode 0/1): Carry는 영구 동결(frozen_gross 0.5·retired 0·position_count 2), 같은 패널이 Error에서는 여전히 `runtime_error` |

### 재검증

- `cmake --build /tmp/longshort_module_server/build -j8` 전체 + `ctest --output-on-failure -j4` → **25/25 통과**(리드가 고친 data 테스트 포함)
- `test_simulator` 36 / `test_http_contract` 17(fixture 4종 각각) / `test_json_serializer` 12 / `test_metrics_oracle` 7 / `test_combined_alpha` 9
- 모듈 파이프라인 경로 외부 parity 재확인: 2 패널 × 알파 3 × mode 0/1 = **12조합 전부 byte-identical**(리뷰 지적대로 레포 안 golden과 별개로 유지)
- 서버 스모크는 18103에서만 띄우고 종료 확인(`ss -ltn` 18100~18199 free, 잔존 프로세스 0)

### 남은 것(엔진 담당 범위 밖 또는 미배정)

- F-06(하네스 키 누락 시 조용한 PASS)·F-07(oracle의 F=0 게이트)은 oracle 담당 파일임
- 리뷰 §3의 #7(warmup stale 미발동 고정), #8(stale 경계값), #9(정산 후 재상장), #11(로더 타입), #13(실데이터 bar별 원장 대조)은 이번 배정에 없어 미착수. #7은 현재 `carried≠0` 게이트로 구조적으로 불가능하며 §4-c에 기술만 해 둔 상태임
- `tools/bench_2000_breakdown.cpp`가 정책을 지정하지 않아 Carry 기본으로 동작함(담당 외 파일이라 미수정, 리뷰의 "비-이슈로 기록" 항목)

---

## 7. 2차 리뷰(팀 개발자) 반영 — 정책 2건 (2026-09-11)

### 7-1. carry는 NaN에만 적용

close를 3상태로 분류하도록 바꿨음(`simulator.cpp:10-30`).

| 값 | 상태 | 동작 |
| --- | --- | --- |
| finite && > 0 | Observed | 평가·체결 |
| NaN | Missing | carry=동결/차단, error=기존 fail-fast |
| 0, 음수, ±inf | **Invalid** | **두 정책 모두 `runtime_error`** `"Invalid close price (not NaN) at bar X, asset Y"` |

- 보유·체결 경로: `require_priced_quotes()`가 `carried≠0 ‖ desired≠0`인 Invalid 종목에서 던짐(정책 무관, Error의 기존 검사보다 먼저 실행돼 메시지가 우선함)
- intent 경로: `normalize_signal_row`의 carry 분기에서 in-mask·finite signal인 Invalid 종목을 만나면 던짐 → warmup과 combined 누산기(`OrderedSignalAccumulator`)에도 동일 적용
- **비대칭 1건(의도)**: Error 정책은 원래 게이트가 Invalid를 intent에서 제외하므로 "미보유·미체결 + Invalid"는 던지지 않음. 기존 테스트 `MissingUnheldAssetDoesNotPoisonReturns`(Error + inf 미보유)가 요구하는 동작이라 유지했음. carry는 intent에 포함하므로 던짐
- 테스트 `CarryFreezesOnlyNaNAndRejectsEveryOtherUnusablePrice`(mode 0/1): NaN은 동결 유지, {0, −1, +inf, −inf} × {보유·체결 / 미보유 intent} × {carry, error}에서 전부 throw, 메시지 문자열까지 대조

### 7-2. 종료일-호가 충돌: 정산은 첫 무호가 bar로 연기

`settlement_bar`는 이제 정산을 **여는** 시점이고, 호가가 끊긴 bar가 **닫는** 시점임(`simulator.cpp:126-140`).

- `di ≥ settlement_bar`이고 quote가 Observed → 정산하지 않고 정상 평가·체결 계속, `post_end_quote_bars++`
- 그 이후 처음 non-Observed bar → 그 시점 mark 기준으로 정산(explicit R 우선, 없으면 default)
- 패널 끝까지 호가가 이어지면 정산하지 않고 보유 유지 → 마지막 bar에 `pending_settlement_count`
- warmup에도 같은 규칙(정산 전이만 반영, 손익 없음)
- `settlement_bar == 0`에 유효 호가가 있으면 "처음부터 retired"가 아니라 첫 무호가 bar까지 거래함 — 새 규칙의 귀결이라 명시해 둠
- 진단 2개 추가: `post_end_quote_bars` → JSON `postEndDateQuoteBars`, `pending_settlement_count` → JSON `pendingSettlements`
- non-Observed 판정에 NaN뿐 아니라 Invalid도 포함되나, Invalid는 관여 시 7-1에서 먼저 던지므로 실질적으로 NaN 트리거임

**기존 테스트 기대값 수정(이유는 각 테스트 주석에 기재)**: 정산 bar에 유효 호가가 있으면 이제 정산이 일어나지 않으므로, 이벤트를 쓰는 기존 6개 테스트의 정산 bar 종가를 NaN으로 바꿔 이벤트가 실제로 발동하게 했음(`DefaultZeroSettlement…`, `MinusOneSettlement…`, `ExplicitSettlementReturnOverrides…`, `WarmupPricesAndSettlements…`, `SettlementEventsStillApplyUnderTheErrorPolicy`, `RetirementCountSeesUnheldAndWarmupSettlements`). 기대 숫자(gross·turnover·mark·카운터)는 그대로임.

`SettlementReplacesTheQuoteMoveOnTheSameBar`는 새 규칙과 정반대 의미가 되어 두 테스트로 교체했음.

| 테스트 | 내용 |
| --- | --- |
| `AQuoteOnTheEndDateDefersTheSettlementToTheSilentBar` (mode 0/1) | 종료일 bar에 120 호가 → 정산 없이 gross **+0.2** 인식·mark 120·`postEndDateQuoteBars=1`, 다음 NaN bar에 mark 120 기준 R=−0.5 정산(gross −0.5, mark 60, turnover 0) |
| `AnEndDateThatNeverStopsQuotingStaysUnsettled` (mode 0/1) | 끝까지 호가 → retired 0·delistings 0·보유 유지, `postEndDateQuoteBars=3`, 마지막 bar `pendingSettlements=1` |

### 재검증

- `cmake --build /tmp/longshort_module_server/build -j8` + `ctest --output-on-failure -j4` → **25/25 통과**
- `test_simulator` 38 / `test_http_contract` 17(fixture 5종 각각) / json_serializer 12 / metrics_oracle 7 / combined_alpha 9
- 옛 엔진 대비 비트 동일: 레포 내 golden 테스트 통과 + 외부 probe 12조합 byte-identical 유지(청정 패널은 전부 Observed라 두 정책 변경 모두 무영향)
- HTTP 항등식 추가: `retirements − staleSettlements + pendingSettlements == /api/config.settlementSymbols`
- 실서버 스모크(18104, 종료 확인): B 종료일 2023-06-15 이후 5일간 호가 지속 → **2023-06-21에 정산**(`delistingPnl −0.1 = 0.2·(−0.5)`), C는 끝까지 호가 → `pendingSettlements=1`, `postEndDateQuoteBars=675`, `retirements=1`

### 남은 확인거리

- `settlement_bar == 0` + 유효 호가 조합의 의미 변경(위 참조) — data 담당 로더 주석/문서와 문구를 맞출 필요가 있음
- Error 정책의 Invalid 비대칭(7-1) — 계약 문서에 한 줄 기록하면 좋음

---

## 8. Phase 2 — successor 이벤트 + return_col 연결 (§P2-3)

리드 승인에 따라 이번엔 `parquet_reader.cpp`의 events 블록과 `config_loader.{h,cpp}`·`data_panel.h`의 events 확장도 엔진 담당으로 처리했음. oracle 담당이 먼저 확정한 5개 규칙과 구현이 일치함(아래 8-3).

### 8-1. 인터페이스

| 위치 | 추가 |
| --- | --- |
| `EventsConfig` | `successor_col`, `successor_ratio_col`(둘 다 optional, ratio는 successor 없이 못 씀). 컬럼명 중복 검사를 5개 전체로 확장 |
| `DataPanel` | `settlement_successor`(int32, symbols 인덱스, −1 = 없음/패널 밖/자기참조), `settlement_successor_ratio`(double, NaN = 미상) |
| `MissingDataDiagnostics` | `successor_conversions`, `converted_gross`, `computed_settlement_count` |
| JSON | `missingData.{successorConversions, convertedGross, computedSettlementReturns}` → `missingData` 키 **총 20개**, `/api/config.successorEvents` |

### 8-2. 로더 (`parquet_reader.cpp`)

- successor id는 `read_optional_ids()`로 읽음: null 허용(=현금 정산), int64/int32/string/large_string을 `id_col`과 동일하게 문자열 정규화(trim 포함)
- ratio는 `read_doubles()`로 **double 정밀도 유지**(float 왕복 없음). double/float/int64/int32/null 지원
- 패널에 없는 successor·자기참조는 −1로 두고 로더가 `std::cerr`에 건수 1줄 경고. 정산 스케줄(`settlement_bar`) 자체는 영향 없음
- 이름이 지정된 컬럼이 파일에 없으면 기존 규칙대로 로드 실패

### 8-3. 회계 (`simulator.cpp`)

정산 bar 판정은 Phase 1 규칙 그대로(종료일 이후 첫 무호가 bar). `settle()`을 2단계로 나눠 **누가 정산되는지 먼저 확정**한 뒤 수익률·승계를 결정함 — 연쇄 승계 가드가 인덱스 순서에 의존하지 않게 하기 위함임.

| oracle 규칙 | 구현 |
| --- | --- |
| 1. 승계 시점 = 보유손익 평가 **직후** | `apply_successor_transfers()`가 `value_holdings()` 뒤·`execute()` 앞에서 `carried_[succ] += h_prev·(1+R)`. 승계분은 그날 successor의 quote move를 먹지 않음(mark_succ는 이미 당일 close로 갱신됨). turnover/fee 0 |
| 2. `convertedGross = Σ\|h_prev·(1+R)\|`(이전 후 값), `successorConversions`=건수, computed는 `defaultDelistings/Gross`에서 제외 | `if (!explicit_return && !computed_return)`로만 default 카운터 증가 |
| 3. 연쇄 가드 | `transferable = succ≥0 && succ≠ii && !retired_[succ] && quote_state_[succ]==Observed`. 같은 bar에 정산되는 successor는 1단계에서 이미 `retired_`라 제외 → 현금 정산 |
| 4. 혼합 딜 근사 | explicit R(총수익률)로 손익 인식 후 `h_prev·(1+R)` 전액을 successor로 승계. **현금 몫 분리는 미구현** — 코드 주석(`simulator.cpp:157-161`)과 이 문단에 근사임을 명시 |
| 5. JSON 20키 | HTTP 테스트가 `totals.getMemberNames().size() == 20`으로 고정 |

R 결정 순서: `explicit finite` → (transferable && ratio finite && mark>0이면) `ratio·close_succ/mark_old − 1`(computed) → `default_delisting_return`. 승계 후 보유분은 그 bar의 `execute()`에서 target대로 리밸런스되며 그때 turnover/fee가 정상 발생함.

### 8-4. 테스트 (신규 7건)

| 테스트 | 내용 |
| --- | --- |
| `SuccessorQuoteCarriesTheHoldingInsteadOfCashingOut` (mode 0/1) | ratio 2.0·50 vs mark 100 → R=0, `convertedGross=1.0`, `settledGross=0`, computed 1건, old만 retired, 목표와 일치해 turnover/fee 0 |
| `ConvertedSharesRebalanceAtTheNextExecution` (mode 0/1) | successor를 원하지 않으면 승계분을 그 bar에 매도 → trade −1.0, turnover 2.0, fee 2·5bp |
| `SuccessorRatioAndExplicitReturnPriceTheExchange` (mode 0/1) | ratio 1.5 → R=−0.25·convertedGross 0.75·부족분 0.25만 시장 매수. explicit R=−0.1이 ratio를 덮고 computed 0건 |
| `AnUnavailableSuccessorFallsBackToCashSettlement` (mode 0/1) | successor 무호가 / 패널 밖 / 같은 bar 정산(연쇄) 3경우 모두 현금 정산 + `settledGross=1.0` |
| `RejectsSuccessorVectorsThatDoNotMatchThePanel` | 길이 불일치 시 `invalid_argument` |
| `SuccessorIdsResolveToPanelIndicesAndAcceptEitherIdType` (로더) | 다대일 승계(A·B→C), null successor, int64 id, ratio null |
| `SuccessorsOutsideThePanelAndSelfReferencesStayCashSettlements` (로더) | 패널 밖·자기참조는 −1, 컬럼 부재는 로드 실패 |

`test_config_loader`에도 successor 컬럼 파싱·검증(ratio 단독 금지, 중복 컬럼명 금지) 3케이스 추가했음.

### 8-5. 예제 config

`examples/datasets/equities_monthly_afl.yaml`의 `events:`를 `path: ../../../data/events/events.parquet`, `end_date_col: endDate`, `return_col: settlementReturn`, `successor_col: successorTradingItemId`, `successor_ratio_col: successorRatio`로 교체했음. **이 파일은 data-events 담당이 만드는 중이라 아직 없음** — 로더가 파일 부재를 오류로 처리하므로 이 예제 config로는 지금 서버가 뜨지 않음. 실데이터 스모크는 파일 생성 후 리드가 돌릴 것.

### 8-6. 검증

- `cmake --build /tmp/longshort_module_server/build -j8` + `ctest --output-on-failure -j4` → **25/25 통과**
- `test_simulator` 43 / `test_parquet_loader` 19 / `test_config_loader` 8 / `test_http_contract` 17(fixture 6종 각각) / json_serializer 12 / metrics_oracle 7 / combined_alpha 9
- 옛 엔진 비트 동일: 레포 golden 테스트 통과 + 외부 probe 2패널 × 알파 3 × mode 0/1 = **12조합 byte-identical**
- 실서버 스모크(18105, 종료 확인): B→D ratio 1.25 승계 → `/api/config.successorEvents=1`, `successorConversions=1`, `computedSettlementReturns=1`, `convertedGross=0.309234`, `delistingPnl=0.109234`, `settledGross=0`. 역산 h_prev = 0.309234−0.109234 = **0.2**(rank 가중치)와 일치, R = 0.5461 ≈ 1.25·(D/B 종가비 1.2369)−1 ✓

### 8-7. 남은 것 / 알려진 근사

- **혼합 딜의 현금 몫 분리 미구현**(위 규칙 4). `cashPerShare`가 이벤트 파일에 들어오면 후속 작업으로 분리 필요
- `AccountingRowView`에는 승계 정보를 노출하지 않음(헤더 §1 고정). 승계는 `market_trades`에 잡히지 않고 `carried`→`weights` 차이로만 보이므로, oracle이 원장 단위로 대조하려면 진단(`converted_gross`)을 쓰거나 뷰 확장이 필요함
- HTTP 계약 테스트의 "carry vs error 완전 일치" 단언은 **결측이 전혀 없는 fixture에서만** 적용하도록 좁혔음. 결측이 있으면 combined 경로의 per-alpha 정규화가 정책에 따라 달라지는 게 계약상 정상이기 때문임(그 경우엔 `pendingSettlements`·`postEndDateQuoteBars`가 정책 무관하게 같은지만 검사)
- Phase 2의 §P2-1/§P2-2(파이썬 도구)와 §P2-4/§P2-5는 다른 담당
