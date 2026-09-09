# 프론트 리팩토링 독립 리뷰 — 2026-09-09

검토자: C++/GA/직렬화 담당(프론트 변경의 작성자와 분리).

**결론: 이번 프론트 diff에서 계산값·wire JSON·활성 화면 동작을 바꾸는 회귀나 변경 차단 사항을 발견하지 못했다.** 리뷰 중 제품 코드는 수정하지 않았다.

## 기준과 범위

기존 미커밋 논리 수정까지 포함한 `/tmp/longshort_refactor_baseline_20260909/frontend`와 현재 frontend를 비교했다. `types/index.ts`, `lib/api.ts`, `AlphaRankingTable.tsx`, `BacktestMetrics.tsx`, adapter/render 테스트, 새 `metricFixtures.ts`가 검토 대상이다. 기존 `DetailTimeSeries`의 단리 MDD 수정은 baseline에도 있으므로 이번 변경으로 판단하지 않았다.

## 확인한 계약

| 검토 항목 | 확인 근거 및 판단 |
|---|---|
| 서버 JSON 키 | `StarSummary.minBet`, `minBetMin`, `nonzeroPosition`은 그대로다. API 요청 body와 응답 구조도 diff에 변경 없다. TypeScript AST로 Star 계열 interface 10개의 이름·타입·optional 여부가 baseline과 동일함을 확인했다. |
| 평균·최소 종목 수·활성 bar 매핑 | `adaptStarSummary`는 각각 `s.minBet → avgPositionCount`, `s.minBetMin → minPositionCount`, `s.nonzeroPosition → activeBarCount`만 수행한다. 숫자 단위·배율·반올림 계산은 추가하지 않았다. IS/OS 및 combined 모두 같은 mapper를 사용한다. |
| IS/OS 타입 통합 | 기존 ISMetrics/OSMetrics의 실제 필드·optional/type은 동일했다. AST 비교에서도 의도한 필드명 3개를 치환하면 모두 새 PeriodMetrics와 일치한다. 기존 import 이름은 타입 별칭으로 유지한다. |
| 활성 랭킹 화면 | avgBet 열은 평균 종목 수를, minBet 열은 최소 종목 수를 읽는다. 열 순서·헤더·소수 2자리 포맷과 나머지 수치/색상 호출 인자는 변경하지 않았다. 새 렌더 검사는 IS 3.50/2.00, OS 5.25/1.00을 실제 열 위치에서 확인한다. |
| 레거시 BacktestMetrics | 읽는 속성 이름만 바꿨다. 표시값/라벨/순서는 변경하지 않았다. 이 컴포넌트의 기존 부정확한 헤더는 아래의 기존 문제로 구분했다. |
| `isEquitySeries`/`osEquitySeries` 제거 | baseline 전체 frontend에서 생성·타입·adapter 테스트 assertion 외 runtime reader가 없다. 데이터는 equity가 아니라 누적 PnL 사본이었으며 차트는 실제 `isRetSeries`/`osRetSeries`를 이용한다. 일별 수익률 복원용 isCumPnl/osCumPnl과 직전 누적 PnL은 유지했다. |
| 스냅샷·저장 호환성 | `PinnedSnapshot` 타입은 AST 기준 그대로다. `SignalChart.handlePin`은 chart data에서 별도 retSeries를 만들고, pinnedSnapshots도 그 필드만 저장/조회한다. alpha result나 제거한 누적 PnL record를 localStorage로 저장·복원하는 경로는 없다. |
| 새 metricFixtures | 서버 wire payload가 아닌 렌더 테스트용 내부 타입 fixture다. 명시적으로 평균/최소/활성 bar 값이 다르므로 이름 뒤바뀜을 잡을 수 있다. 서버 adapter 테스트에서는 기존 wire 키를 계속 사용한다. |

원본 wire 객체의 key를 변경하거나 객체를 변이시키는 코드도 없다. combined가 있으면 실제 combined, 없으면 첫 batch를 사용하는 기존 fallback 역시 그대로다.

## 독립 검증

1. 실제 프론트 파일과 baseline adapter를 사용하는 비교 스크립트를 재실행했다. 단일/복수/명시 combined/fallback/빈 batches/기간 시작·끝 경계/GA 완료 등 9개 시나리오에서 필드명 변경 및 제거한 내부 record를 제외한 전체 출력이 동일했다. wire 원본 무변이와 OS 첫날 수익률도 확인했다.
2. TypeScript AST로 raw Star interface 10개와 PinnedSnapshot의 전체 속성을 baseline과 비교했다. IS/OS 두 타입을 새 PeriodMetrics와 비교하고 BacktestAlphaResult가 의도한 record 2개 외에는 변경되지 않았음을 확인했다.
3. 담당자의 실제 Vitest 설정을 검토한 뒤 독립 재실행했다. 4개 파일/16개 테스트 모두 통과했다: adapter 7, API client 계약 7, 활성 랭킹 렌더 1, 레거시 렌더 1.
4. `rg --hidden --no-ignore`로 gitignore에 숨은 lib까지 포함하여 삭제·변경한 속성의 미갱신 소비자를 검색했다. 남은 옛 key 사용은 wire 타입, 명시적 adapter 입력 또는 wire 테스트 payload다.

Vitest는 `/tmp/longshort_refactor_frontend_deps`의 의존성과 임시 설정을 사용했다. production에 없는 `metricColors`는 CSS 문자열을 반환하는 stub으로 대체되어 있어 **이 검증은 숫자·텍스트·열 위치를 확인하며 실제 색상/전체 Next 빌드/브라우저 검증을 뜻하지 않는다.** 새 fixture로 변경한 두 렌더 테스트 외의 기존 Python fixture 의존성은 그대로 남아 있다.

## 기존 상태에서 남아 있는 사항 — 이번 diff의 회귀 아님

- `BacktestMetrics`는 평균 종목 수를 `MinBet`, 활성 bar 수를 `Alpha`라고 표시한다. 이번 변경에서 값/라벨을 바꾸지 않았으며 실제 app/page는 이 컴포넌트를 호출하지 않고 AlphaRankingTable을 사용한다. 레거시 UI를 다시 연결할 때 명칭을 재검토해야 한다.
- `alphaStore.setResult`의 일반 경로는 selectedAlphaIndices 배열을 비우면서 selectedAlphaIndexSet을 비우지 않는다. 새 결과에 체크 표시가 남을 수 있다. 해당 코드는 baseline과 같고 이번 지표 리팩토링은 건드리지 않았다.
- `pipelineFixtures.ts`는 삭제된 Python 엔진 경로를 계속 참조한다. 이번 두 렌더 테스트는 해당 의존성을 벗어났지만 전체 테스트 부채가 해결된 것은 아니다.
- `isMinBetSeries`/`osMinBetSeries` 및 DetailTimeSeries의 minBet 선택 분기는 생성자/활성 진입점이 없는 기존 후속 정리 후보다. 이번 제거한 is/osEquitySeries와 혼동하면 안 된다.

위 기존 항목들은 본 패치에 새로 유입된 문제로 보고하지 않는다. 최종 검증 수치에도 전체 frontend 빌드 성공을 포함해서는 안 된다.
