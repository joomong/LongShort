# C++ 리팩토링 독립 감사 — 2026-09-09

대상은 현재 close-to-close 계산을 유지하는 이름·소유권·메모리 정리다. 데이터 연결, YAML 구성 추가, VWAP 등 체결 모델 변경은 범위에 포함하지 않았다. 기존 미커밋 논리 수정이 포함된 `/tmp/longshort_refactor_baseline_20260909`를 변경 전 기준으로 사용했다.

## 이번 담당 범위에서 적용한 정리

| 대상 | 변경 전 실제 사용 | 적용 및 계약 보존 근거 |
|---|---|---|
| `GAEngine::get_fitness` | private 선언·정의만 존재, 호출자 없음 | 제거. GA 평가 콜백은 이미 점수 하나를 반환한다. |
| `GAConfig::fitness_metric` | 위 dead 함수와 테스트의 `sharpe` 대입에서만 사용 | 제거. 서버는 이 필드를 받지 않으며 `metrics.is_summary.sharpe`를 콜백에서 직접 반환한다. |
| `GAResult::best_metrics` | 기본값 대입만 하고 읽히지 않음 | 제거. 실제 최종 JSON은 `best_xml`을 다시 실행한 전체 지표로 만든다. `ga_engine.h`의 SummaryMetrics 의존성도 제거했다. |
| 마지막 세대의 `next_population` | 평가·보고를 마친 뒤 후손을 생성하고 즉시 함수 종료 | 마지막 progress 콜백 뒤 후손 생성 생략. 반환 best XML·history, 평가 호출, 외부에 노출되는 난수 상태는 그대로다. |
| `signal_plot_to_json` | main에서 먼저 선택 구간 행렬·날짜를 복사해서 전달 | 선택 시작 index 인자 추가(기본값 0). 원본 행렬에서 선택 구간만 직렬화한다. JSON 필드와 행/날짜 순서를 유지한다. |
| `signal_distribution_to_json` | 모든 유효·비제로 값을 `vector<float> all`로 다시 수집 | 첫 순회에서 min/max, 두 번째 순회에서 50개 bin counts 계산. `isfinite && abs(v)>1e-12`, bin 산식·경계 clamp, 같은 값만 있을 때 처리, 빈 값 결과는 그대로다. |
| JSON 내부 필드 참조 | `m.min_bet`, `m.min_bet_min`, `m.nonzero_position`, `ts.nonzero_positions` | 루트의 명명 정리에 맞춰 평균 종목 수·최소 종목 수·활성 bar 수·일별 종목 수 이름으로 변경. 외부 `minBet`, `minBetMin`, `nonzeroPosition`, `nonzeroCount` 키를 유지한다. |

GA 관련 구현은 `cpp-engine/src/ga/ga_engine.{h,cpp}` 및 기존 `test_ga.cpp`의 무효 설정 대입 한 줄만 수정했다. `population_size/generations==0`에 대한 이전 입력 검사는 보존했다.

## 메모리 효과와 범위

- 선택 구간 길이를 P, 종목 수를 N, 히스토그램 유효값 수를 K라 하면 main의 slice 제거로 float 데이터 `4×P×N` bytes와 날짜 `8×P` bytes 복사를 줄인다. 각 row의 vector 객체/allocator overhead도 줄어든다. API에 offset을 추가하는 것과 main에서 실제 복사본 생성을 제거하는 것이 함께 적용되어야 이 효과가 있다.
- 히스토그램 `all` 제거는 최소 `4×K` bytes의 값 배열을 줄인다. 실제 이전 peak는 `vector` capacity 확대와 재할당 임시 버퍼 때문에 더 클 수 있다. 새 임시 메모리는 50개 int counts에 비례한다.
- 원본 collected signals와 최종 JSON 행렬 자체는 필요하므로 남는다. 이번 패치를 AFL 전체 적재량이나 응답 JSON 크기 자체를 해결하는 변경으로 해석하면 안 된다.
- GA 마지막 후손 생략은 마지막 한 번의 population×gene 수에 비례한 복사·변이·할당을 없앤다. 최대 heap 자체는 이전 세대에서 같은 크기에 도달할 수 있으므로 전체 실행 peak 감소를 보장하지 않는다.

## 확인했지만 삭제하지 않은 구조와 보류 사유

| 대상 | 감사 결과 | 판단 |
|---|---|---|
| `CompiledAlpha::modules` | unique_ptr로 표현식 하위 노드까지 소유한다. pipeline에 없는 하위 노드도 여기에 있다. | 유지. 실제 수명·해제를 담당한다. |
| `CompiledAlpha::pipeline` | top-level 모듈 실행 순서인 비소유 포인터 목록이다. | 유지. modules와 서로 다른 역할이다. |
| `CompiledAlpha::output` | 비소유 마지막 출력 포인터. compiler/pipeline/stress 테스트에서 검사하고 expression 테스트에서는 직접 init/calculate한다. | 단순 미사용 필드가 아니므로 유지. accessor 변경은 별도 계약 정리가 필요하다. |
| `ModuleInput` ring/NaN buffer | lookback 계산과 warmup 결측값 제공, checkpoint 읽기·쓰기에서 사용한다. | 유지. 과거 리뷰의 재실행 초기화 수정도 유지한다. |
| `Module::skip_nan_init_` | 일부 모듈이 설정하고 simulator가 첫 모듈 실행 전에 읽는다. | 유지. 사용되지 않는 bool이 아니다. |
| `MChain` | returns 표현식에서 close/Delay(close) 계산을 위해 expr_parser가 만든다. | 유지. registry에 없다는 이유만으로 제거하면 안 된다. |
| `MSignalSave::label` | compiler가 id를 문자열로 복사하나 소비자·checkpoint에서 읽지 않는다. 실제 이름 해석은 compiler의 cache_registry가 한다. | 실제 dead 메타데이터 후보. 이번 담당 구현에서는 보류하고 기록했다. |
| `SearchSpace::Gene::description` | extract 단계에서 tag.attribute 문자열 생성 후 소비자가 없다. | 진단용 메타데이터로 의도했을 가능성이 있으나 현재 dead. 정리 후보로 기록했다. |
| `SimulatorConfig::bars_per_year` | simulator 내부에서는 사용하지 않는다. 연환산은 metrics 함수 인자에서 실제로 수행된다. 스트레스 테스트는 cfg 필드를 metrics 호출에 넘긴다. | 책임 분리 대상이나 단순 문자열 치환으로 삭제하면 테스트/호출 계약을 깨므로 전체 호출자 정리 필요. |
| `BacktestConfig::{start_date,end_date,initial_capital}` | YAML에서 필수로 파싱하지만 C++ 계산에서는 사용하지 않는다. | 외부 YAML 계약이므로 새 데이터·설정 작업을 미룬 이번 패치에서 삭제하지 않는다. 날짜를 적용하거나 자본을 곱하는 변경도 계산 의미 변경이다. |
| GA HTTP 경로의 `sim_start_str` | JSON 값을 읽고 stream lambda에 capture하지만 본문에서 쓰지 않는다. | 미사용 로컬 후보. 일반 simulate 경로의 동명 변수는 plot 시작점을 정하므로 일괄 제거 대상이 아니다. |
| main의 `run_signals_only` | 이름과 달리 Simulator::run으로 손익·turnover까지 재계산한다. | 재실행 후 signals 수집이라는 의미의 이름이 정확하다. 손익 계산 자체를 건너뛰는 새 실행 경로는 이번 정리에서 추가하지 않는다. |
| batch/top-level/combined JSON의 반복된 timeseries | 프론트 HTTP 계약에 사용되는 데이터다. | 출력 필드를 삭제하는 메모리 최적화는 보류. |

## 이번에 확인한 추가 수명 문제

멀티알파 요청은 모든 `CompiledAlpha`를 한 번에 컴파일하고 요청 끝까지 보관한다. 각 알파 실행이 끝나도 상태ful 모듈의 ring, running sums, signal cache 등은 해당 modules 객체와 함께 남는다. 대략 ring float storage는 `4×N×Σ(lookback+1)` bytes이며 여기에는 NaN buffer와 통계별 별도 누적 배열이 추가된다.

이 구조는 leak는 아니다. 요청 종료에 정상 해제되지만, worker 수와 무관하게 완료된 알파 상태까지 누적 보유하므로 peak가 알파 수에 비례할 수 있다. top-K signals 수집에서 같은 객체를 재실행하기 때문에 임의로 modules만 clear하면 안 된다. XML을 보관하고 선정된 알파만 재컴파일하거나 명시적인 실행 상태 해제 계약을 넣는 후속 정리가 필요하다. 전체 구조를 바꾸지 않기 위해 이번 담당 패치에서는 유지했다.

GA fitness 평가도 현재는 IS Sharpe 하나를 얻으려고 전체 yearly·timeseries·OS summary를 생성한다. 사용하지 않는 계산 결과가 있지만 metric 구간·NaN·표준편차·warmup 계약을 그대로 따라가는 전용 계산 경로가 필요하므로 단순 삭제 대상과 구분한다.

## 검증

- GA와 Determinism 기존 테스트 10개 모두 통과. g++ C++17/O2/Wall/Wextra 빌드도 통과했다.
- 새 `test_json_serializer.cpp` 7개 모두 통과. legacy JSON 키, offset 이후 날짜·행 정렬, 범위 끝/초과, NaN/Inf/null, 0/극소값 제외, 상수 50-bin 결과를 검증했다.
- 변경 전 `json_serializer.cpp`의 signalPlot/histogram 함수 본문을 oracle로 사용해 500개 행렬×4개 시작점×2개 함수 = 4,000건의 JSON Value 전체 동등성 비교를 통과했다. 행렬에는 NaN/양·음 Inf/0/±1e-12/1e-13/상수열/빈 행렬을 포함했다. probe는 `/tmp/longshort_refactor_ga_20260909/serializer_parity.cpp`에 있다.
- 전체 엔진 통합/빌드 검증과 다른 담당 변경의 리뷰는 루트에서 취합한다. 이 문서의 통과 결과는 전체 HTTP 서버/DB 검증 완료를 뜻하지 않는다.
