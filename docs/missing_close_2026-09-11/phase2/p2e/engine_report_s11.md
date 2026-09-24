## 11. Phase 2e — 구조/평가 분리 + 가드 v2 (2026-09-14)

팀 리뷰 `docs/review_phase2d_2026-09-14.md`의 R1·R2와 브리프 §E-2 계약을 구현했음.

### 11-1. R1 — 다리 존재와 평가 가능성 분리

생성기가 승계사 환산에 실패하면 `successorRatioCache`만 NaN이 되고 `cashPerShareCache`는 남는데, 엔진이 "유한 ratio = 주식 다리"로 판정해 그런 행을 **현금 전용 딜로 오인**하고 그 계산값을 파일 R보다 우선했음(실제 32건, IWA 12678069는 −0.0109 대신 **−0.5258**).

| 항목 | 내용 |
| --- | --- |
| `EventsConfig` | `stock_leg_col`·`cash_leg_col`(선택, bool) |
| `DataPanel` | `settlement_has_stock_leg`·`settlement_has_cash_leg`·`settlement_successor_named` |
| 유도(컬럼 없을 때) | stock = ratio 유한 **∨** 승계사 id 지정(패널 밖 포함), cash = cash 유한 |
| 계산 조건 | `computable = priced_mark ∧ (stock ∨ cash) ∧ (¬stock ∨ transferable) ∧ (¬cash ∨ cash 유한)` — **현금만 계산은 `hasStockLeg=false`일 때뿐** |
| 불가 시 | 파일 R > 기본값, 전액 현금 정산(승계 없음) |

`transferable`은 이제 `승계사 호가 관측 ∧ ratio 유한 ∧ mark>0`이고, 승계는 `computable ∧ stock_leg`일 때만 일어남.

### 11-2. R2 — 가드 v2: 사용하는 이벤트는 절대 건너뛰지 않음

v1은 앵커 날짜가 적재 패널 밖이면 건너뛰어, **warmup 한 bar 차이로 같은 이벤트가 통과/거절**로 갈렸음. v2는 로더가 이벤트마다 **엔진의 정산 bar `s*`**(`settlement_bar`부터 대상 종가가 처음 관측되지 않는 bar)를 패널에서 직접 계산함.

| 상황 | 판정 |
| --- | --- |
| `s*`가 패널 밖(이 패널이 그 이벤트를 안 씀) | 유일한 건너뜀 → `eventsPriceChecksSkipped` |
| 승계사 앵커 있음 + 승계사 패널 안 | 날짜 == `timestamps[s*]` 아니면 실패, `close[succ][s*]` 상대 1e-4 대조 |
| 승계사 패널 밖 | 승계사 측만 건너뜀(엔진도 승계 안 함) |
| **대상 앵커**(신규 `target_price_col`/`target_price_date_col`) | 날짜 == `s*` 직전 마지막 유효 종가일 아니면 실패, 값 대조 |
| 앵커가 NaN/Inf인데 대응 평가 입력(ratioCache/cashCache) 유한 | 실패 |
| 앵커 날짜 정밀도 | 자정이면 일 단위, 시각이 붙으면 그 bar와 **정확히** 일치해야 함 |

리뷰 재현 케이스(`/tmp/longshort_p2d_loader_review_20260914/cases.py`) 대비 판정 변화:

| 케이스 | v1 | v2 |
| --- | --- | --- |
| `past_guard_date_no_warmup` / `_with_warmup` | skip / 실패 (**불일치**) | **둘 다 실패**(날짜 불일치) |
| `reference_nan_disables_check` / `_inf_` | 통과(검사 생략) | **실패**(유한 ratio를 매길 앵커 없음) |
| `future_guard_date_skips` | skip | **실패**(정산 bar 아님) |
| `future_event_unused_but_rejects` | 실패(오거절) | **skip**(이 패널이 안 쓰는 이벤트) |
| `intraday_*_false_reject` / `_false_accept` | 오거절 / 오수락 | **둘 다 실패**(앵커가 정산 bar가 아님) |
| `daily_match`·`cached_nan/zero/inf`·`explicit_tail_guard_rejects`·`latest_tail_trim_guard_skips`·`missing/self_successor` | — | 의도대로 유지 |

### 11-3. 테스트 (신규 6건, 교체 1건)

| 테스트 | 내용 |
| --- | --- |
| `DeclaredStockLegWithoutACacheRatioUsesTheFileReturn` (loader, mode 0/1) | **IWA 실제 행을 파일→로더→엔진 경로로 재현**: cash 7.9·raw ratio 0.804·ratioCache NaN·mark 16.66 → `ret = −0.010884`(파일 R), 현금만 −0.5258이 아님, `computedSettlementReturns=0`·`default=0`·`conversions=0` |
| `DeclaredLegsMustAllBePriceableBeforeAReturnIsComputed` (engine, mode 0/1) | 플래그 컬럼 경로·승계사 id 유도 경로·현금 전용 경로 3갈래 |
| `RejectsLegFlagsThatDoNotMatchThePanel` | 플래그 벡터 차원 가드 |
| `GuardChecksEveryEventThisPanelActuallySettles` | 일치·허용오차(9e-5)·캐시 NaN/Inf/불일치·앵커 NaN/Inf/null날짜·날짜 불일치 메시지 |
| `GuardVerdictDoesNotDependOnTheLoadedWindow` | **R2 핵심**: warmup 0/1 모두 같은 실패 판정 |
| `GuardSkipsOnlyEventsThisPanelNeverSettles` | 미사용 이벤트·tail trim·명시 end·승계사 미해결 |
| `GuardPinsIntradayAnchorsToTheExactSettlementBar` | 일중 오거절·오수락 둘 다 거절 |
| `GuardRequiresBothColumnsOfEachDeclaredAnchor` (기존 교체) | 컬럼 부재 실패 + 미정산 이벤트 skip |

config 파싱 4건(대상 가드 짝·플래그 컬럼·중복 이름) 추가.

### 11-4. 문서

예제 yaml에 `stock_leg_col: hasStockLeg`, `cash_leg_col: hasCashLeg`, `target_price_col: targetCacheMark`, `target_price_date_col: targetCacheMarkDate` 선언. README에 **"다리 구조와 평가 가능성은 별개다"** 항목 신설(IWA 사례 포함)하고 가드 절을 v2 규칙으로 갱신(정산 bar 기준·건너뜀 조건 2개·앵커 날짜 정밀도).

### 11-5. 검증

- `ctest --output-on-failure -j4` → **25/25 통과**. `test_simulator` 50 / `test_parquet_loader` 26 / `test_config_loader` 8 / `test_http_contract` 17(fixture 8종 각각)
- 옛 엔진 비트 동일: golden 테스트 + 외부 probe **12조합 byte-identical**
- **실데이터(예제 config, 18110, 종료 확인)**: data-events가 6개 컬럼을 모두 채운 파일을 재생성해 정상 기동
  - `price guard 179 checked / 0 skipped`(v1의 70 → 179, **건너뜀 0**). 실패 0건 = 재생성 파일이 이 캐시와 일치함
  - 전기간 시뮬 비교: `computedSettlementReturns` 88 → **82**, `defaultDelistings` 38 → **40**, `delistingPnl` −0.009606 → **−0.001443**. 승계 관련(`successorConversions=40`, `convertedGross=0.084021`, `settledGross=0.248570`)은 불변
  - 즉 이 유니버스에서 R1에 해당하는 행 **6건**이 현금 전용 오계산에서 파일 R/기본값으로 교정됐고, 그만큼 상폐 손익 왜곡이 사라졌음

### 11-6. 남은 것

- 일중 앵커 규칙은 "자정이면 일 단위, 아니면 정확 일치"로 구현했음. 브리프 문구는 일 단위만 명시했으나 그대로 두면 리뷰의 일중 오수락이 남아 정확 일치를 추가했음 — 일봉 파일에는 영향 없음
- 현금 전용 딜의 `cashPerShareCache` 단위는 **대상 앵커**로 검증되지만, 대상 앵커 컬럼이 없는 파일에서는 여전히 재생성 규약에만 의존함
- E-3(검증 도구 실패 판정)·E-1(생성기)은 다른 담당 범위이며 `tools/`는 건드리지 않았음
