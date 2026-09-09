# C++ 엔진 리팩토링 독립 리뷰 — 2026-09-09

검토자: 프론트 리팩토링 담당 에이전트. C++ 구현에는 참여하지 않았고 이번 리뷰에서 제품 코드를 수정하지 않았다.

비교 기준은 `/tmp/longshort_refactor_baseline_20260909/cpp-engine`의 이번 턴 시작 전 소스다. HEAD와 비교하면 이전 close-to-close 논리 수정이 섞이므로 이를 기준으로 삼지 않았다. 대상은 simulator, summary/yearly/full/timeseries metrics, main의 호출·plot slice, 이를 뒷받침하는 JSON serializer와 관련 tests/golden이다.

## 발견 사항

### GA `simStart` 읽기 제거의 입력 검증 부수효과 — 수정 확인 완료

구현자가 먼저 발견해 리뷰어에게 전달한 사항을 독립적으로 확인했다. baseline의 GA handler는 XML을 읽은 직후 `get("simStart", "").asString()`을 실행한다. 저장한 문자열이 계산에 사용되지 않아도 object/array 값에서는 JsonCpp 타입 변환 오류가 발생한다. 읽기 자체를 지우면 그 입력을 더 이상 같은 지점에서 거부하지 않으므로 완전한 동작 보존이 아니다.

같은 위치에 `(void)(*body).get("simStart", "").asString();`이 반영된 것을 확인했다. 저장 변수와 비동기 람다 capture만 제거했다. XML 읽기 직후·IS 시작일 읽기 직전의 기존 평가 순서와 타입 오류를 유지하므로 이 항목은 해결됐다.

이 항목 외에 검토 범위에서 새 손익·지표·수명 회귀는 발견하지 못했다.

## 확인한 보존 조건

| 대상 | 확인 내용 |
| --- | --- |
| D0/D1 | D0은 현 target, D1은 직전 target으로 실행한다. 보유 손익은 이전 execution weight로 계산하고, 상태 갱신 swap 순서가 baseline과 동일하다. D0에서 쓰지 않던 `previous_targets`를 할당하지 않는 변경은 선택되지 않는 경로의 저장공간만 줄인다. |
| 종가/수수료 | valid close 필터, abs(signal) 합산 순서, `> 1e-12` 조건, normalized target 계산, holding return, turnover, `ret -= turnover * fee` 순서가 동일하다. 종가 결측 오류 처리는 이전 논리 수정 상태를 유지한다. |
| `run` callback/collection | module의 numAssets 설정→init→bar 계산 순서, NaN 초기화 조건이 같다. raw signal 저장 후 callback, 그 뒤 normalize/손익 계산한다. callback 포인터가 가리키는 buffer는 callback 실행 동안 유효하고 매 bar 재사용되는 이전 계약을 유지한다. |
| `run_from_signals` | 입력 차원 검사는 그대로다. 기존처럼 callback을 부르거나 signals를 복제하지 않는다. 입력 row를 호출 중 읽기만 하므로 필요 없던 float row copy를 제거해도 결과 수명에 영향이 없다. |
| `PortfolioSimulation` 수명 | data는 기존 Simulator가 참조하던 panel이며 config는 Simulator가 소유한 복사본이다. 새 클래스의 참조는 함수 내 사용 기간에 유효하다. output의 vector는 move 반환으로 호출자에게 소유권이 넘어가며 로컬 상태를 참조하지 않는다. |
| `min_bet` 제거 | baseline의 `SimulatorOutput.min_bet`은 `gross exposure / N`으로 채우지만 metrics 함수는 인자를 읽지 않는다. summary는 별도의 nonzero count로 평균·최소를 계산한다. 따라서 배열과 전달 인자 제거는 계산 입력 제거가 아니다. |
| 종목 수 이름 | position_count는 일별 종목 수, avg_position_count는 평균, min_position_count는 최소, active_bar_count는 보유 bar 수다. 기존 threshold와 빈 구간 처리, int/double 타입을 보존했다. |
| JSON | `minBet`, `minBetMin`, `nonzeroPosition`, `nonzeroCount`는 외부 키를 유지하고 새 내부 이름에 매핑한다. 기존 배율·정수/실수 값 변환도 같다. |
| summary MDD/PnL | 첫 finite return을 누적값의 초기값으로 사용한 뒤 같은 순서로 더한다. scalar로 바꾸면서 0을 먼저 더하지 않았으므로 초기 signed zero까지 기존 순서를 보존한다. peak 0, additive drawdown 비교도 같다. |
| yearly MDD/PnL | baseline처럼 0에서 시작해 연도별 finite return을 순서대로 더한다. ann_ret/sharpe/margin의 반복 계산을 이 합계로 재사용하지만 원래 각 loop의 입력과 순서가 같다. 빈 연도 구간의 0 결과도 유지한다. |
| 기간 | full metrics의 IS/OS 탐색, global bar 0 생략, timeseries 상대 index 보정, summary의 시작·종료 범위는 인자 이름 변경 외 그대로다. |
| plot slice | main이 선택한 시작 index를 그대로 serializer에 전달한다. 기존 slice의 row/date 범위와 순서를 직접 순회하므로 JSON 값이 같고 JSON은 입력 참조를 보관하지 않는다. signalRange도 같은 순서로 계산한다. |
| histogram | finite/nonzero 제외 규칙, min/max, 50개 bin, bin_width와 index 산술 순서를 유지한다. flattened 복사 대신 두 번 읽는 방식이다. |

기존 combined raw 합산과 병렬 덧셈 순서도 변경하지 않았다. 스레드 실행 순서에 따른 combined 차이는 이번 리팩토링이 해결한 항목으로 주장하면 안 된다.

## 검증 및 남은 검증 공백

- golden metrics JSON을 실제 파싱해 비교했다. dead `input.min_bet` 제거와 이름 치환을 제외하면 입력·기대 숫자 전체가 정확히 동일했다. 기대값을 새 계산에 맞추어 수정한 흔적은 없다.
- 기존 손계산 D0/D1·비용·결측 close 테스트는 config/인자 변경만 반영하며 기대값을 유지한다. 새 flat-bar count 테스트는 평균/최소와 active bar 수를 구분한다.
- 기존 pipeline collection 및 pipeline/replay 일치 테스트를 확인했다. 다만 최초 리뷰 시점의 저장소 tests에는 `signal_row_callback`을 설정하는 케이스가 없다. 따라서 callback 횟수·raw 값·호출 순서와 replay에서 callback/collect를 무시하는 계약은 자동 검증 공백이다. 코드는 baseline과 동일하며 현재 확인된 구현 버그라는 뜻은 아니다. 구현자와 통합 검증 담당에게 이 공백을 전달했다.
- 전체 컴파일과 엔진 test 실행, baseline JSON 비교 실행은 `build_validation` 담당이 수행한다. 이 리뷰에서는 그 작업을 중복 실행하지 않았다.
- plot의 기본 시작값으로 timeseries-relative `is_start_index`를 raw signals에 적용하는 기존 동작도 보존했다. 이 기존 index 의미 문제와 simStart의 실행범위 문제는 별도 논리 검토 대상이며 slice 제거의 새 회귀로 분류하지 않았다.

## 후속 확인

- GA handler의 `.asString()` 유지 변경을 최종 소스에서 확인했다. 발견된 동작 변경 위험은 해결됐다.
- `Simulator.RawSignalObserverAndCollectionKeepTheirExecutionPathContract` 추가를 확인했다. bar 순서/횟수, raw close와 warmup NaN, collector와 callback raw 값의 일치, replay에서 callback 재호출·signal matrix 복제가 없는 점, pipeline/replay 수익·회전율 일치를 검사한다. normalize 대비 callback의 정확한 호출 위치는 소스 순서 검토로 확인했다.
- `SimulatorConfig` 주석도 두 flag/callback이 `run()`의 계약임을 명시하도록 바뀌었다.
- 검토 범위에서 해결되지 않은 새 회귀 발견 사항은 없다. 추가 테스트를 포함한 최종 컴파일·실행 결과는 통합 검증 담당 보고를 따른다.
