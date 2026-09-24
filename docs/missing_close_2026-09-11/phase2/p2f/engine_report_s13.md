## 13. 카운터 항등식 수정 — `eventsLoaded` 정의 (2026-09-14)

리드 실측(예제 config `end_date: '2018-12-31'`): `eventsLoaded=227`인데 세 가드 값 합이 `247`이었음.

### 13-1. 원인은 이중 계상이 아니라 분모

가드 루프는 매칭된 행마다 이미 **정확히 한 버킷**만 증가시키고 있었음(`!evaluable → NotEvaluable` → `정산 bar 없음 → OutOfRange` → `→ Checked`, 각 분기에 `continue`). 실제로 리드의 수치도 `116+52+79 = 247`로 자기 일관적이었음.

틀린 것은 **`eventsLoaded`의 정의**였음. `main.cpp`가 이를 `settlement_bar≥0 ∨ 유한 explicit R ∨ 해결된 successor`인 심볼 수로 세고 있어서, 종료일이 패널 밖이고 explicit R·승계사도 없는 매칭 행 20건이 분모에서만 빠졌음(247 − 227 = 20). 전기간 실행은 그 20건이 0이라 우연히 항등식이 성립했음.

### 13-2. 수정

- `DataPanel.events_rows_matched` 추가 — 로더가 이벤트 파일에서 행을 찾은 패널 심볼 수를 셈
- `/api/config`의 `eventsLoaded` = `events_rows_matched`. 버킷 로직은 그대로(이미 배타적이었음)
- 정의를 `/api/config` 코드 주석과 README에 명시: **`eventsLoaded` = 이벤트 파일에서 행을 찾은 패널 심볼 수**, **`settlementSymbols` = 그중 정산이 이 패널 안에서 열리는(종료일이 마지막 bar 이전) 심볼 수**, 세 가드 값은 한 행당 한 버킷으로 `eventsLoaded`를 분할
- 기동 로그 문구도 `N rows matched, M opening inside the panel`로 정정

### 13-3. 테스트

`GuardBucketsPartitionTheMatchedRowsAcrossAShortPanel`: 6 bar 패널에 4개 이벤트 행(A는 bar 2 정산·평가 입력 있음, B는 bar 4 정산·평가 입력 있음, C·D는 입력 없음, E는 행 없음).

| 패널 | matched | checks | outOfRange | notEvaluable | 합 |
| --- | --- | --- | --- | --- | --- |
| 전체 6 bar | 4 | 2 | 0 | 2 | 4 ✓ |
| `end_date`로 B 정산 전에 자름 | 4 | 1 | **1** | 2 | 4 ✓ |

HTTP 계약 테스트도 `guard_rows > 0`이면 **`= eventsLoaded`**(이전엔 `≥ settlementSymbols`)로 강화했고, `eventsLoaded ≥ settlementSymbols`를 함께 단언함.

### 13-4. 재실측

| config | 기동 로그 | `eventsLoaded` | checks / outOfRange / notEvaluable | 합 |
| --- | --- | --- | --- | --- |
| `end_date: '2018-12-31'`(리드와 동일) | `247 rows matched, 159 opening inside the panel, 90 with a successor` | 247 | 117 / 52 / 78 | **247 = MATCH** |
| 전기간(`latest`) | `262 rows matched, 262 opening inside the panel, 101 with a successor` | 262 | 179 / 0 / 83 | **262 = MATCH** |

리드 실측(116/52/79)과 1건 차이는 그 사이 data-events가 파일을 재생성한 결과로 보임(같은 실행에서 `successorEvents`가 72 → 101로 늘었고 checks·notEvaluable은 179/83으로 동일함 — 승계사 id가 더 많이 해결됐으나 그 행들의 캐시 입력은 여전히 NaN, 즉 R1 대상 모집단임).

- `ctest --output-on-failure -j4` → **25/25 통과**, `test_parquet_loader` 28 / `test_http_contract` 18(fixture 8종)
- 옛 엔진 비트 동일: 외부 probe **12조합 byte-identical**
- 서버는 18112·18113에서만 띄우고 종료 확인
