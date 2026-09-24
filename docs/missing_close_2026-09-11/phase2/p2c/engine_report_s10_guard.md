## 10. 이벤트 파일 스테일 가드 (2026-09-14)

지적: 엔진 정산이 **캐시 close**(이후 분할까지 재작성된 기준)에 **당시 기준** cash·ratio를 곱해 단위가 섞였음(NVLS→LRCX 2012 −89.6% vs 정답 +3.95%, GOOG 2015 −95.0% vs +0.62%, successor 1,101건 중 162건이 10% 초과 오차). 해결은 생성기가 **캐시 단위로 환산한 컬럼**을 내는 것이고, 엔진 산식은 그대로 둔 채 **파일이 그 캐시에 묶여 있음을 로더가 강제**하도록 했음.

### 10-1. 구현

| 위치 | 내용 |
| --- | --- |
| `EventsConfig` | `successor_price_col`·`successor_price_date_col`(선택, 둘 다 함께여야 하고 `successor_col` 필요). 컬럼명 중복 검사 대상에 포함 |
| `DataPanel` | `events_price_checks`, `events_price_checks_skipped` |
| `read_events` | successor가 패널에서 해결된 행에 대해, 파일의 날짜에 해당하는 bar를 찾아 `close[successor][bar]`를 다시 읽고 파일 값과 대조. 상대오차 `>1e-4`(또는 캐시 값이 비유한) → **로드 실패**. 날짜가 패널 밖이면 skip, successor가 패널 밖이면 애초에 비교 대상 아님 |
| 메시지 | `Events file is stale against the price cache: <successor> <YYYY-MM-DD> file=<v> cache=<v> - regenerate build_events.py` (§9-6의 try/catch가 받아 `Cannot start:` + exit 1). 원문의 em dash는 로그 인코딩 안전을 위해 ASCII 하이픈으로 씀 |
| `/api/config` | `eventsPriceChecks`, `eventsPriceChecksSkipped` (**키 2개 추가**, `missingData`는 20개 유지) |
| 기동 로그 | `... 72 with a successor; price guard 70 checked / 0 skipped` |

산식은 손대지 않았음 — `V = cash + ratio·P_succ(t)`, `R = V/mark − 1`, 주식분만 승계(§9) 그대로임.

### 10-2. 문서

- 예제 yaml: `cash_col: cashPerShareCache`, `successor_ratio_col: successorRatioCache`, `successor_price_col: successorCacheClose`, `successor_price_date_col: successorCacheCloseDate` + "캐시 단위 / 캐시 재구축 후 이벤트 재생성" 주석
- `examples/datasets/README.md`: events 블록 주석을 캐시 단위 컬럼명으로 바꾸고 **"단위와 스테일 가드"** 항목 신설(분할 재작성 때문에 단위가 섞이는 이유, NVLS·GOOG 사례, 1e-4 임계, 실패 메시지, skip 조건, `/api/config` 2키). 승계 절에도 단위 요구사항 한 문단 추가

### 10-3. 테스트 (신규 4건)

| 테스트 | 내용 |
| --- | --- |
| `StalePriceGuardComparesTheSuccessorCloseAgainstThisCache` | ① 일치 통과(`checks=1`) ② 허용오차 내(9e-5) 통과 ③ 4:1 분할 불일치 → 메시지 4개 조각 전수 대조하며 실패 ④ 날짜 패널 밖 → `skipped=1` ⑤ successor 미해결 → 비교 자체 없음 |
| `test_config_loader` | 가드 2컬럼 파싱 + 3가지 오설정(짝 없음·successor_col 없음) 거부 |
| `test_http_contract` | `/api/config`의 `eventsPriceChecks`·`eventsPriceChecksSkipped` 존재 |

### 10-4. 검증

- `cmake --build /tmp/longshort_module_server/build -j8` + `ctest --output-on-failure -j4` → **25/25 통과**. `test_simulator` 48 / `test_parquet_loader` 21 / `test_config_loader` 8 / `test_http_contract` 17(fixture 7종 각각)
- 옛 엔진 비트 동일: golden 테스트 + 외부 probe **12조합 byte-identical** 유지
- fixture 스모크(18108, 종료): 4:1 분할로 어긋난 파일 → `exit 1` + `Cannot start: Events file is stale against the price cache: D 2023-06-16 file=39.901875 cache=159.607498 - regenerate build_events.py`. 일치 파일 → 기동, `eventsPriceChecks=1 / skipped=0`
- **실데이터 스모크(18109, 종료)**: data-events가 재생성한 `data/events/events.parquet`에 4개 캐시 컬럼이 모두 들어와 예제 config로 기동됨
  - `T=4450 N=1056`, 이벤트 262건 전부 패널 내, successor 72건, **price guard 70건 검사 전부 통과 / skip 0**, 패널 밖 successor 24건은 현금 정산
  - `/api/simulate`(returns→zscore, 2011~, 3,946 bar) → `retirements=262`, `delistings=132`, `computedSettlementReturns=88`, `successorConversions=40`, `defaultDelistings=38`, `convertedGross=0.0840`, `settledGross=0.2486`, **`delistingPnl=−0.0096`**(전기간 합, 이전의 −89%류 폭주 없음), `postEndDateQuoteBars=0`, `pendingSettlements=0`

### 10-5. 남은 것

- successor 72건 중 **70건만** 가드가 검사함 — 나머지 2건은 가드 가격·날짜 셀이 비어 있음(생성기가 인수사 가격을 캐시 밖에서 구한 경우로 추정). 커버리지를 100%로 올릴지는 data-events 담당 판단임
- 가드는 successor가 **패널 안**일 때만 동작함. 현금 전용 딜의 `cashPerShareCache` 단위는 이 가드가 검증하지 못하므로, 캐시 재구축 시 이벤트 재생성 규약(문서화 완료)에 의존함
