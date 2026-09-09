# 프론트 계산 지표 리팩토링 검토 — 2026-09-09

현재 close-to-close 손익, 비용, 지표 표시값과 HTTP JSON 키를 유지한다. DB/YAML/체결 모델은 이 변경의 범위가 아니다. 기존 DetailTimeSeries의 단리 MDD 변경도 보존했다.

## 적용한 변경

| 기존 내부 이름 | 변경한 내부 이름 | 의미 | 보존한 서버 JSON 키 |
| --- | --- | --- | --- |
| `min_bet` | `avgPositionCount` | 기간의 bar별 보유 종목 수 평균; 활성 랭킹 화면 `avgBet` | `minBet` |
| `minBetMin` | `minPositionCount` | 기간 중 최소 보유 종목 수; 활성 랭킹 화면 `minBet` | `minBetMin` |
| `nonzeroPosition` | `activeBarCount` | 한 종목 이상 보유한 bar 수 | `nonzeroPosition` |

- `frontend/types/index.ts`: 동일했던 IS/OS 구조를 `PeriodMetrics`로 통합하고 `ISMetrics`/`OSMetrics`는 기간 의미를 나타내는 타입 별칭으로 남겼다. 서버 타입 `StarSummary`에는 legacy 키의 실제 의미를 주석으로 명시했다.
- `frontend/lib/api.ts`: 동일한 IS/OS 변환 함수 두 개를 `adaptStarSummary` 한 개로 합쳤다. 서버 키에서 내부 의미 이름으로 명시적으로 매핑한다. 값의 변환·단위 변경은 없다.
- `AlphaRankingTable`과 `BacktestMetrics`: 새 내부 필드를 읽도록 변경했다. 화면 라벨, 순서, 반올림, 표시값은 그대로다.
- `isEquitySeries`/`osEquitySeries`: adapter의 생성과 내부 타입에서 제거했다. 모든 프론트 소스·스크립트·테스트와 동적 import/객체 순회/저장 경로를 검색했을 때 런타임 reader는 없고 adapter 테스트 두 assertion만 있었다. 실제 수익률·MDD 차트는 `isRetSeries`/`osRetSeries`에서 재계산한다. snapshot은 별도 `retSeries`를 읽고 저장하므로 영향을 받지 않는다.
- 누적손익 서버 필드 `StarTimeseries.cumPnl`은 그대로 유지한다. IS/OS 경계의 직전 누적손익을 이용한 일별 수익률 복원도 그대로다.
- 제거 효과: alpha 하나마다 IS/OS 누적손익 record 2개와 합계 `T - isStartIndex`개 날짜 항목 생성을 줄인다. 멀티알파에서는 combined에도 같은 절감이 생긴다. JavaScript 객체의 실제 byte 사용량은 런타임마다 달라 고정 수치로 주장하지 않는다.
- 변경한 두 렌더 테스트는 삭제된 `python-engine` fixture 대신 `__tests__/metricFixtures.ts`의 작은 typed display fixture를 사용한다. 평균/최소 값이 섞이지 않도록 IS/OS의 서로 다른 값을 실제 열 위치에서 검사한다. 서버 응답을 흉내 내는 fixture의 legacy JSON 키는 유지했다.

## 조사했지만 이번에 삭제하지 않은 후보

| 위치/구조 | 확인한 사실 | 판단 |
| --- | --- | --- |
| `DetailTimeSeries`의 `minBet` 분기, `isMinBetSeries`/`osMinBetSeries` | adapter에 writer가 없고, 유일한 caller인 `AlphaDetailView.METRICS`에도 `minBet` 선택지가 없다. 내부에서 빈 배열을 준비하는 코드가 남아 있다. | UI 분기와 타입까지 함께 삭제할 수 있는 후속 후보. `FMT_MINBET`는 일반 차트의 기본 formatter로도 쓰이므로 한꺼번에 지우면 안 된다. |
| `BacktestMetrics`, `SummaryMetrics` 컴포넌트 | 페이지의 static/dynamic import에는 없고 렌더 테스트에서만 import한다. 활성 결과 화면은 `AlphaRankingTable`이다. | 테스트와 함께 제품 UI 정리 범위를 확정한 뒤 삭제할 후보. 이번에는 이름 변경 후에도 레거시 렌더 계약을 유지했다. |
| `PeriodMetrics.ir`/`corr` | 현재 저장소에 생산자와 소비자가 없는 타입 필드. | 타입만 존재해 그 자체로 RAM을 할당하지 않는다. 호환 타입 정리 후보. |
| 내부 `sharpe400`/`minsoc`, alpha의 `longRatio`/`shortRatio` | 서버 값은 존재하고 adapter가 복사하지만 현재 프론트 runtime reader는 없다. | 작은 scalar 복사 정리 후보. 외부 wire 필드를 없앨 근거는 아니다. |
| `selectedAlphaIndices`와 `selectedAlphaIndexSet` | 같은 선택을 배열과 Set으로 이중 저장한다. 일반 `setResult`는 배열만 지우고 Set을 지우지 않는다. `AlphaRankingTable`은 Set을 읽는다. | 새 결과에서 체크 표시가 남을 수 있는 기존 UI 상태 오류. 계산 지표 리팩토링과 분리해서 고칠 대상. |
| `BacktestMetrics`의 `MinBet`/`Alpha` 헤더 | 이 레거시 컴포넌트는 평균 보유 종목 수와 보유 bar 수를 옛 라벨로 표시한다. 현재 페이지에서는 호출하지 않는다. | 활성 화면 `avgBet`/`minBet` 정의는 맞다. 이 컴포넌트를 복귀시키려면 라벨을 재검토해야 한다. |

검색은 gitignore에 가려질 수 있는 `frontend/lib`까지 포함해 `rg --hidden --no-ignore`로 수행했다. `Object.entries`/`Object.keys`, 동적 import, 저장소의 snapshot/localStorage 경로도 확인했다. 선언 또는 테스트 사용처만으로 런타임 메모리 사용이라고 판단하지 않았다.

## 검증

1. 리팩토링 전 baseline과 현재의 실제 `lib/api.ts`를 TypeScript 5.9.3으로 변환해 9개 응답 시나리오를 비교했다. 이름 변경 및 의도적으로 제거한 내부 record를 제외하면 전체 출력이 동일하다. 단일/멀티알파, combined fallback/명시 응답, 빈 batches, 기간 밖/중간 시작 인덱스, GA 완료 응답을 포함한다. 원본 wire payload가 변하지 않는 점과 IS/OS 첫 일별 수익률도 확인했다.
2. 실제 Vitest 4개 파일에서 16개 테스트가 통과했다: adapter 7개, API client contract 7개, 랭킹 렌더 1개, 레거시 표 렌더 1개.
3. 테스트 의존성은 `/tmp/longshort_refactor_frontend_deps`에만 설치했다. 부재한 production `metricColors`는 임시 Vitest config에서 빈 CSS class 함수로 대체하여 텍스트·값·열 매핑만 검증했다. 제품 소스에 누락 모듈을 복원하지 않았다.
4. 전체 Next 빌드, 전체 TypeScript typecheck, 전체 Vitest, 브라우저 및 실제 metric 색상은 검증하지 않았다. 기존 `frontend/lib` 모듈 6개 부재와 일부 테스트의 삭제된 Python fixture 의존성이 남아 있다.

재현 명령:

```bash
node docs/refactor_2026-09-09_frontend_check.cjs /tmp/longshort_logic_deps/typescript.js /tmp/longshort_refactor_baseline_20260909/frontend
node /tmp/longshort_refactor_frontend_deps/node_modules/vitest/vitest.mjs run --config /tmp/longshort_refactor_frontend_deps/vitest.config.mts
```

`refactor_2026-09-09_frontend_check.cjs`는 TypeScript 라이브러리 파일 경로와 수정 전 프론트 경로를 인자로 받는다. `/tmp` 도구·baseline은 이 작업 세션의 검증 산출물이며 저장소 배포 의존성이 아니다. 이 담당 기록 작성 시점에는 미커밋 상태였으며, 최종 변경 이력은 저장소 커밋에 기록한다.
