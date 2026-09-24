## 13. Phase 2e — 검증 도구 계약(E-3)과 구조 플래그

팀 리뷰 `docs/review_phase2d_2026-09-14.md` R3(검증 도구의 실패 판정 부족)과 브리프 §E-2·E-3 반영. 반례는 `/tmp/longshort_phase2d_coverage_audit/validator_counterexamples/summary.json`. 서버는 18120~18139에서만 띄웠고 전부 종료 확인함.

### 13-1. 원장 — 구조 플래그 기반 computable (E-2)

`hasStockLeg`/`hasCashLeg`를 입력으로 받아 계약 그대로 구현했음:

```
computable = mark 유효 ∧ (stock_leg ∨ cash_leg)
             ∧ (¬stock_leg ∨ (승계 가능 ∧ ratio 유한))
             ∧ (¬cash_leg ∨ cash 유한)
```

즉 **현금만으로 값을 매기는 것은 `hasStockLeg=false`일 때뿐**임. 컬럼이 없으면 `stock = ratio 유한 ∨ 승계사 id 지정`, `cash = cash 유한`으로 유도함.

부수 변경 하나가 중요함: **NaN ratio를 1.0으로 채우던 기본값을 폐지**했음. 이전에는 "교환비 미상"과 "1:1"을 구분하지 못해, 선언된 주식 다리를 값 매길 수 없는데도 1주로 가정해 계산할 수 있었음. 이제 NaN ratio는 "환산 불가"로 취급돼 파일 R로 내려감.

시나리오 `mixed_stock_leg_ratio_unknown` 추가(승계사는 패널에 있고 호가도 있으나 ratio NaN + cash 80 + 파일 R −0.3): 기대값은 **파일 R −0.3**이며, 현금만 쓰면 −0.2가 나와 주식 다리를 조용히 버리게 됨. 기존 `successor_quote_missing`·`mixed_unusable_successor_*`도 생성기가 실제로 낼 형태(환산 실패면 ratioCache NaN)로 입력을 바꾸고 `hasStockLeg`를 명시했음 — 기대 결과는 그대로임.

self-check: **40 시나리오 295 체크 전부 PASS**.

### 13-2. `check_missing_data.py` — 거절 판정 엄격화 (R3)

- 원장이 거절하는 케이스는 이제 **HTTP 400 + 메시지 위치 일치**일 때만 PASS임. 원장 메시지에서 `bar N, asset M`을 뽑아 서버 메시지가 같은 bar·asset을 지목하는지 확인함. 5xx·200·다른 위치의 400은 전부 FAIL임(반례 `hold_gap_recovery__error__R0`의 "무관한 500을 both reject로 통과"가 막힘).
- 범위 거절(`defaultDelistingReturn` −1.5/+3)도 400이 아니면 FAIL임.
- 요약이 케이스 종류를 분리 출력함: `kinds: numeric=146, both_reject=30, range_reject=2`. **"178 수치 대조" 같은 표현을 쓰지 않도록** 총계와 수치 대조 건수를 분리했음.

### 13-3. `run_top500_missing_close.py` — 실패를 exit code로 (R3)

`validate_run`/`validate_sensitivity`를 추가해 200 응답이라도 다음이면 FAIL로 내림:

| 조건 | 임계 |
| --- | --- |
| 거래 bar 0 (빈 200) | bars == 0 |
| 수수료 항등식 잔차 | > 1e-9 |
| R 민감도 항등식 잔차, 또는 turnover·fee가 R에 따라 움직임 | > 1e-9 |
| config에 가드 컬럼이 있는데 `/api/config`의 `eventsPriceChecks`가 0 | — |
| `missingData` 필수 키 누락 | — |

**어느 run이든 OK가 아니면 exit 1**이고, 보고서 머리에 `exit code`와 사유가, 아래에 실패 사유 표(alpha·R·status·HTTP·사유)가 들어감.

실측 확인:

| 실행 | 결과 |
| --- | --- |
| 정상 전기간(Top500, 알파 5 × R 2) | 10/10 OK, **exit 0**, 가드 `config 선언=True eventsPriceChecks=70` |
| 강제 실패(`--sim-start 2030-01-01`) | 2/2 FAIL, **exit 1**, 표에 `400 simStart is after the available trading period` |
| HTTP 대조 실패 있음 | **exit 2** (0이 아님) |

### 13-4. `--self-test` — 도구가 스스로 실패를 잡는지

제품 응답이 아니라 **메모리 내 가짜 응답**으로 판정 함수를 때림.

- `check_missing_data.py --self-test`: 8 probe 전부 PASS. 무관한 500·다른 위치의 400·빈 200·잘린 시계열·키 누락·정산 숫자 0.3 오차가 전부 FAIL로 잡히고, 정상 응답과 위치 일치 400만 PASS임.
- `run_top500_missing_close.py --self-test`: 10 probe 전부 PASS. bars 0·수수료 잔차 0.3·가드 미실행·키 누락·R 민감도 잔차 0.3·turnover가 R 따라 움직임이 전부 FAIL, 실패 2건이 exit 1로 이어짐.

### 13-5. 합성 fixture에 가드·구조 컬럼 (E-3)

fixture events parquet에 `hasStockLeg`·`hasCashLeg`와 가드 앵커 4종(`targetCacheMark`/`Date`, `successorCacheClose`/`Date`)을 넣고 config에 `stock_leg_col`·`cash_leg_col`·`target_price_col`·`target_price_date_col`·`successor_price_col`·`successor_price_date_col`을 선언했음. 앵커는 **엔진 정산 bar 기준**으로 계산함 — 승계사는 정산 bar 당일 종가, 대상은 그 직전 마지막 유효 종가. 승계사를 그 bar에 값 매길 수 없으면 앵커와 ratioCache를 함께 NaN으로 둬 불변식 ③을 지킴.

효과: 이전엔 `eventsPriceChecks=0`(선언 자체가 없어 가드가 안 돌았음)이었으나 이제 합성 대조가 **가드 happy-path를 태움**(`eventsPriceChecks=1, skipped=0` 확인).

### 13-6. 엔진 E-2 반영 후 재대조 — 12건 해소

엔진이 E-2(구조 플래그·가드 v2·대상 앵커)를 반영해 재빌드된 뒤 재실행했음:

| 항목 | 결과 |
| --- | --- |
| `--self-test` | 8 probe 전부 PASS |
| `--self-check` | 40 시나리오 295 체크 전부 PASS |
| HTTP 대조 | **178 케이스 전부 PASS** (이전 166 PASS / 12 FAIL) |
| 종류별 | 수치 대조 **146**, 양쪽 거절 **30**, 범위 거절 **2** |
| 수치 대조 최대 절대오차 | **5.51e-17** |

직전에 FAIL이던 3 시나리오가 전부 계약대로 바뀌었음 — `mixed_stock_leg_ratio_unknown`·`mixed_unusable_successor_no_quote`·`mixed_unusable_successor_outside_panel` 모두 `computedSettlementReturns=0`, `delistingPnl=-0.30000001`(파일 R). 즉 `hasStockLeg=true`인데 ratio를 환산할 수 없으면 더 이상 현금 다리만으로 값을 매기지 않음.

### 13-7. IWA 12678069 실이벤트 회귀

실행 시각 `data/events/events.parquet`: mtime `2026-09-14T10:54:43.602147`, 6413행 × 29컬럼, sha256 `180289d82779b6ae6ed34fe9fb0eff51…`. 컬럼 목록:

```
tradingItemId, endDate, settlementDate, actionCode, settlementReturn, suggestedReturn, cashPerShare, successorTradingItemId, successorRatio, acquirerName, source, confidence, actionText, matchMethod, endDateSource, endDateConflict, dbEndDateReason, acquirerPriceDate, hasCashLeg, hasStockLeg, cacheUnitStatus, cashPerShareCache, successorRatioCache, cacheUnitFactorTarget, cacheUnitFactorSuccessor, targetCacheMark, targetCacheMarkDate, successorCacheClose, successorCacheCloseDate
```

이벤트 행(WINDSTREAM CORP가 IOWA TELECOMMUNICATIONS SERVICES 인수, 주당 **$7.90 + Windstream 0.804주**):

| 필드 | 값 |
| --- | --- |
| endDate / settlementDate | 2010-05-28 / 2010-06-01 |
| `hasCashLeg` / `hasStockLeg` | True / True |
| `cashPerShare` / `successorRatio` | 7.9 / 0.804 |
| `successorTradingItemId` | 28090336 (**가격 캐시에 컬럼 자체가 없음**) |
| `cacheUnitStatus` | `no_cache_successor_at_settlement` |
| `cashPerShareCache` / `successorRatioCache` | NaN / NaN (불변식 ①·② 준수) |
| 가드 앵커 4종 | 전부 NaN |
| `settlementReturn`(as-traded 폴백) | **−0.010884** |

§11 방식(단일 종목 패널·자체 마스크·fee 0·`default_delisting_return=-1`·mode 0)으로 실행한 결과:

| 항목 | 기대 | 엔진 | 차이 |
| --- | --- | --- | --- |
| 정산 bar | 2010-06-01 (bar 7718, 종료일 다음 bar가 곧 첫 무호가 bar) | 동일 | — |
| mark | 16.65999984741211 (2010-05-28) | 동일 | — |
| 정산 bar `ret` | **−0.010883553885** (파일 R) | −0.010883553885 | **0.00e+00** |
| `computedSettlementReturns` | 0 | 0 | — |
| `defaultDelistings` | 0 (default −1이 아니라 파일 R 사용) | 0 | — |
| `settledGross` / `convertedGross` | 1.0 / 0.0 (전액 현금 폴백) | 1.0 / 0.0 | — |

**회귀 크기**: 승계사를 값 매길 수 없는데 현금 다리만으로 계산했다면 `7.90/16.66 − 1 = −0.525810`, 즉 **−52.6%**가 나왔을 것임. 실제는 −1.09%로, 단일 이벤트에서 북 절반이 사라지는 차이임. §11-3(ANSS −46.6%)·§12(NVLS −89.6%)와 같은 계열의 마지막 구멍이 막혔음.

**가드 카운터 관측**: 이 패널에서는 `eventsPriceChecks=0`, `eventsPriceChecksSkipped=0`이었음. IWA 행은 `cacheUnitStatus≠ok`라 앵커가 전부 NaN이고 평가 입력도 NaN이라 **비교할 기준값 자체가 없음** — 정산 bar가 패널 안이므로 "건너뜀"도 아님. 즉 **`cacheUnitStatus≠ok` 행은 두 카운터 어디에도 안 잡힘**. 가드가 살아 있다는 증거로는 전기간 Top500 실행의 `eventsPriceChecks=70`(§13-3 표)을 봐야 함. 계약에 "사용됐으나 기준값 없음" 범주가 없다는 점이므로 리드 확인 권고함.
