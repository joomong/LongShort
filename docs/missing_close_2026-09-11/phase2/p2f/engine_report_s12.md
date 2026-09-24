## 12. 리뷰 후속 3건 — 앵커 필수·카운터 3분류·정산 로그 (2026-09-14)

엔진 산식은 변경하지 않았음.

### 12-1. 앵커 필수 강제

캐시 단위 입력을 앵커 없이 쓰는 구성을 로더가 거절함.

| 층위 | 규칙 | 실패 메시지 |
| --- | --- | --- |
| config | `cash_col` ∨ `successor_ratio_col` 선언 → 대상 가드 2컬럼 필수 | `Events config declares cache-unit inputs without target_price_col/target_price_date_col: ...` |
| config | `successor_ratio_col` 선언 → 승계사 가드 2컬럼 필수 | `Events config declares successor_ratio_col without successor_price_col/...` |
| 행 | `cash_input` ∨ `ratio_input` 유한 → 대상 앵커(가격·날짜) 필수 | `Events file has evaluation inputs without price anchors: <symbol> target anchor missing - regenerate build_events.py` |
| 행 | `ratio_input` 유한 → 승계사 앵커도 필수(승계사가 패널 밖이어도 파일에는 있어야 함) | `... successor anchor missing - ...` |

행 단위 검사는 **정산 bar 계산보다 먼저** 돌아 적재 구간과 무관하게 같은 판정을 냄(R2와 같은 원칙). 이벤트가 없는 구성(골든·비트동일 테스트 포함)은 `read_events`가 즉시 반환하므로 영향 없음.

### 12-2. 카운터 3분류

`eventsPriceChecksSkipped` 폐지 → 세 값이 **패널에 매칭된 이벤트 행 수를 분할**함.

| 키 | 뜻 |
| --- | --- |
| `eventsPriceChecks` | 앵커를 이 패널에서 다시 읽어 대조 완료 |
| `eventsPriceChecksOutOfRange` | 정산 bar가 패널 밖 = 이 패널이 그 이벤트를 쓰지 않음 |
| `eventsPriceChecksNotEvaluable` | 캐시 단위 평가 입력이 없어 대조할 것이 없음 |

`/api/config`·기동 로그·HTTP 계약 테스트를 갱신했음. 가드를 선언하지 않은 config에서는 세 값이 모두 0임.

### 12-3. opt-in 정산 로그

요청 `settlementLog: true`(기본 false, **기본 페이로드 불변**)면 응답에 `settlementLog` 배열과 `settlementLogUnheld` 건수가 붙음. 엔진은 `SimulatorConfig.collect_settlements`가 켜졌을 때만 `SimulatorOutput.settlements`에 기록하므로 기본 경로는 손대지 않음.

| 필드 | 값 |
| --- | --- |
| `symbol`·`time`(ISO 날짜)·`bar` | 정산 종목과 시점 |
| `exposure` | 정산 직전 보유 가중치 `h_prev` |
| `return`·`source` | 적용한 R과 출처 `computed`/`explicit`/`default` |
| `cashShare`·`stockShare` | 주당 대가를 mark로 나눈 현금·주식 몫 |
| `successor`(심볼 또는 null)·`transferred` | 승계 대상과 이전 가중치 |

**거래 기간의 `exposure ≠ 0` 정산만** 기록하고, 미보유 퇴출은 `settlementLogUnheld`로만 셈(진단 `unheld_settlement_count` 추가, `missingData` 20키는 유지).

항등식 2개를 테스트로 고정했음: `Σ(exposure·return) = delistingPnl`, `Σ|exposure| (source=default) = defaultDelistingGross`, 그리고 `로그 길이 = delistings`.

### 12-4. 테스트

| 테스트 | 내용 |
| --- | --- |
| `CacheUnitInputsMayNotBeDeclaredWithoutTheirAnchors` | config 층위 거절 2종 + 앵커 추가 후 통과 + 부재 컬럼 실패 |
| `RowsWithEvaluationInputsMustCarryTheirAnchors` | 현금만 있고 대상 앵커 없음·유한 ratio에 승계사 앵커 없음 → 실패, ratio NaN이면 승계사 앵커 불필요 |
| `GuardSkipsOnlyEventsThisPanelNeverSettles` 외 | 카운터 이름을 3분류로 갱신, 승계사 미해결 행도 대상 앵커로 검사되도록 기대값 수정 |
| `SettlementLogIsOptInAndAddsUpToTheReportedTotals` (HTTP) | 옵션 off면 두 키 부재·`missingData` 불변, on이면 10개 필드 존재·`exposure≠0`·항등식 2개·`길이=delistings` |
| `ConfigPublishesMissingCloseAccountingSettings` (HTTP) | 세 카운터 존재 + 합이 `settlementSymbols` 이상 `N` 이하(가드 미선언이면 전부 0) |

기존 fixture 4종(`SuccessorIdsResolve…`·`CashPerShareLoads…`·IWA 회귀·guard v2 fixture)에 앵커 컬럼을 추가했고, HTTP 합성 fixture 3종(successor·mixed·guard)도 승계사·대상 앵커를 선언하도록 재생성했음.

### 12-5. 검증

- `ctest --output-on-failure -j4` → **25/25 통과**. `test_parquet_loader` 27 / `test_simulator` 50 / `test_config_loader` 8 / `test_http_contract` **18**(fixture 8종 각각)
- 옛 엔진 비트 동일: 외부 probe **12조합 byte-identical** + 레포 golden 통과
- **실데이터 스모크(예제 config, 18111, 종료 확인)**
  - 기동 로그 `price guard 179 checked / 0 out of range / 83 not evaluable` → **합 262 = `settlementSymbols` 262**(항등식 성립)
  - `settlementLog` **132건**(= `delistings`), `settlementLogUnheld` **110**, 직렬화 31.5KB(ALL에서도 여유)
  - `Σ(exposure·return) = −0.001443178632` = `delistingPnl` (**차이 0.00e+00**)
  - `Σ|exposure| (default) = 0.097175538589` = `defaultDelistingGross` (**차이 0.00e+00**)
  - 출처 분포 computed 82 / default 40 / explicit 10
  - 샘플(혼합 딜): `2590144 → 2655888`, 2025-07-18, exposure 0.006464, cashShare 0.5341 + stockShare 0.5310 → R 0.0651, transferred 0.003433

### 12-6. 남은 것

- `notEvaluable` 83건은 캐시 단위 입력이 없어 `settlementReturn`(파일 R) 또는 기본값으로 정산되는 행임. 이 중 기본값 40건의 실제 손익 영향은 이제 `settlementLog`의 `source=default` 항목으로 직접 집계할 수 있음
- 행 단위 앵커 검사는 이 패널이 쓰지 않는 이벤트에도 적용됨(적재 구간 독립성 우선). 파일 전체가 규약을 지키면 비용 없음
- `tools/`·`build_events.py` 미접촉, 커밋 없음
