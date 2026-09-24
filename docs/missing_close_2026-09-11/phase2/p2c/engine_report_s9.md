## 9. 팀 리뷰 반영 — 룩어헤드 제거 + 혼합 딜 현금 분리 (2026-09-14)

지적 2건: (1) 생성기가 **정산일+최대 10일** 미래 가격으로 만든 R을 엔진이 더 이른 bar에 인식 → 룩어헤드, (2) 혼합 딜의 현금까지 승계사 주식으로 승계. 둘 다 R의 출처를 파일에서 **정산 bar 자체의 관측치**로 옮겨 해결했음. 정산 bar 판정(종료일 이후 호가 끊긴 첫 bar)은 그대로임.

### 9-1. 인터페이스

| 위치 | 추가 |
| --- | --- |
| `EventsConfig` | `cash_col`(optional). 컬럼명 중복 검사 대상에 포함 |
| `DataPanel` | `settlement_cash`(double, 주당 현금, NaN = 현금 다리 없음) |
| JSON | **키 추가 없음**(20개 유지). `settledGross`·`convertedGross`의 의미만 확정 |
| 예제 yaml | `cash_col: cashPerShare` 추가 |

### 9-2. 수익률 결정 — 엔진이 정산 bar에서 직접 가격을 매김

주당 proceeds `V = cash + ratio·P_succ(t)`, 직전 mark를 `M`이라 할 때

| 순위 | 조건 | R |
| --- | --- | --- |
| 1 | **계산 가능**: `M>0` 이고 (승계 가능 ∥ (현금만 있고 주식 다리 없음)) | `R = V/M − 1` (`computedSettlementReturns` 집계) |
| 2 | 계산 불가 + 파일에 유한 explicit R | `R = explicit` |
| 3 | 그 외 | `R = default_delisting_return` |

- **우선순위가 Phase 2와 뒤집혔음**(computed > explicit). 파일 R은 미래 가격 산물이므로, 현장에서 값을 매길 수 있으면 항상 현장 값을 씀
- "주식 다리 있음"은 `settlement_successor_ratio`가 유한한지로 판정함. ratio는 successor가 패널 밖이어도 로더가 채우므로, **패널 밖 주식/혼합 딜은 계산 불가**로 떨어져 explicit R이 살아남음(브리프 §4의 "explicit R은 successor가 패널 밖인 주식/혼합 딜에서만 의미"와 일치)
- 현금 전용 딜은 `R = cash/M − 1`을 엔진이 계산함 → **생성기 R 불필요**

### 9-3. 현금/주식 분리

- 손익 `pnl += h_prev·R` (변동 없음)
- **주식분만 승계**: `h_succ += h_prev·(ratio·P_succ(t)/M)`. 승계 시점은 보유손익 평가 직후라 그날 successor의 quote move를 먹지 않음, turnover/fee 0
- **현금분은 실현**되어 가중치에서 사라짐. 다음 체결에서 target을 채우려면 시장 매수가 필요하고 그만큼 turnover/fee가 정상 발생함
- 승계 불가(패널 밖 / quote 없음 / 같은 bar 정산 / ratio NaN / V ≤ 0) → **전액 현금 정산**

### 9-4. 진단 두 기준 (리드 확정)

| 카운터 | 기준 | 식 |
| --- | --- | --- |
| `settledGross` | **노출 기준** (Phase 1 유지) | 전액 현금 정산 `+= \|h_prev\|`; 혼합 딜 `+= \|h_prev\|·cash/V` |
| `defaultDelistingGross` | 노출 기준 | `+= \|h_prev\|` (R=−1이어도 동일 — 전기간 R 민감도 표 안정) |
| `delistingPnl` | 손익 | `+= h_prev·R` |
| `convertedGross` | **실제 이전 가중치**(가치 기준) | `+= \|h_prev·ratio·P_succ/M\|` = `h_succ` 증가분 |

두 기준은 의도적으로 다르며 합이 `\|h_prev\|`가 되지 않음. `simulator.cpp:187-194` 주석과 테스트 `MixedDealGrossCountersUseExposureAndTransferredWeight`에 그 사실을 명시했음(현금 30 + 주식 50, mark 100 → `settledGross 0.375`(=30/80), `convertedGross 0.5`(=50/100)).

### 9-5. 테스트 (신규 5건, 수정 1건)

| 테스트 | 내용 |
| --- | --- |
| `MixedDealRealizesTheCashLegAndCarriesOnlyTheStockLeg` (mode 0/1) | 현금 50 + 주식 50, mark 100 → R=0, 승계 0.5·현금실현 0.5, 다음 리밸런스 turnover가 **주식분 기준 0.5** |
| `MixedDealGrossCountersUseExposureAndTransferredWeight` (mode 0/1) | 현금 30 + 주식 50 → R=−0.2, `settledGross 0.375`(노출) vs `convertedGross 0.5`(가치) |
| `CashOnlyDealsArePricedFromTheCashAmountAndTheMark` (mode 0/1) | cash 120 / mark 100 → R=0.2를 엔진이 계산, default −1 미사용, 전액 현금 정산 |
| `SettlementReturnsPreferPricedProceedsThenTheFileThenTheDefault` (mode 0/1) | computed > explicit > default 3단 우선순위 |
| `StockOnlyConversionsReportNoSettledExposure` | 주식 전용 딜은 `settledGross 0` |
| `SuccessorRatioAndExplicitReturnPriceTheExchange` **수정** | Phase 2의 "explicit이 ratio를 덮는다" 단언을 새 규칙(정산 bar 관측치가 파일 R을 이김 + 패널 밖일 때만 파일 R 사용)으로 교체 |

로더: `CashPerShareLoadsBesideTheSuccessorColumns`(혼합/현금전용/미등록 행, 컬럼 부재 오류), config: `cash_col` 파싱·중복 검사 추가. 차원 가드에 `settlement_cash`도 포함.

### 9-6. 부수 수정

서버 기동 시 config·데이터 로드를 try/catch로 감싸 **깨끗하게 종료**하도록 했음(`main.cpp:305-318`). 이전에는 events 컬럼명 오타 같은 로드 오류가 uncaught exception → `terminate` → **core dump**로 끝났음(스모크 중 실제 관측). 이제 `Cannot start: <메시지>` + exit 1임.

### 9-7. 검증

- `cmake --build /tmp/longshort_module_server/build -j8` + `ctest --output-on-failure -j4` → **25/25 통과**
- `test_simulator` 48 / `test_parquet_loader` 20 / `test_config_loader` 8 / `test_http_contract` 17(fixture 7종 각각) / json_serializer 12 / metrics_oracle 7 / combined_alpha 9
- 옛 엔진 비트 동일: 레포 golden 테스트 통과 + 외부 probe **12조합 byte-identical**
- 실서버 스모크(18106, 종료 확인). B가 $40 현금 + 0.5 D주로 인수되는 혼합 딜, 파일 R은 NaN:
  - `computedSettlementReturns=1`, `defaultDelistings=0` → 엔진이 정산 bar 가격으로 직접 값을 매김
  - `convertedGross=0.123694` = h_prev(0.2)·0.5·P_D/M, `settledGross=0.066776` = 0.2·40/V (노출 기준)
  - 가치 기준이었다면 0.2·40/M ≈ 0.062 — 실제 값이 다르므로 **노출 기준이 적용됨을 확인**
  - `delistingPnl=−0.014307` = 0.2·R, `missingData` 키 20개 유지
- 잘못된 `cash_col` config로 기동 → `exit 1` + `Cannot start: Events parquet column not found: noSuchColumn ...` (core dump 없음)

### 9-8. 남은 것

- `V ≤ 0`(주식·현금 모두 0 이하)이면 승계하지 않고 전액 현금 정산으로 처리함. 실제로는 `ratio·P_succ > 0`이면 발생하지 않음
- 파일의 `settlementReturn`은 이제 **패널 밖 successor를 가진 주식/혼합 딜에서만** 쓰임. 생성기가 현금 전용 딜에 R을 채워도 엔진은 무시함 — 생성기 쪽에서 R 계산을 줄일 여지가 있음(data-events 담당 판단)
- `AccountingRowView`는 승계분을 별도로 노출하지 않음. 원장 대조는 `settled_gross`(per-asset, 이제 혼합 딜에서는 현금 노출분)와 `weights`−`market_trades` 차이로 해야 함
