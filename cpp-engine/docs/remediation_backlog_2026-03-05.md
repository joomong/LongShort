# C++ Engine Remediation Backlog (2026-03-05)

## 목적
- 외부 리뷰에서 제기된 이슈를 누락 없이 추적한다.
- 구현 순서를 `안정성/정확성 -> 메모리 안정성 -> 성능 최적화 -> 구조 단순화`로 고정한다.
- 각 항목에 우선순위와 완료 기준(DoD)을 명시한다.

## 왜 일부가 초기 계획에서 빠졌는가
- 1차 계획은 크래시/UB/OOM 차단을 우선한 최소 안전 스코프였다.
- 그러나 문서상 전체 백로그(후속 최적화/리팩터)가 분리 기록되지 않아 누락처럼 보였다.
- 이 문서로 반영/미반영 항목을 모두 한 곳에서 관리한다.

## P0 (즉시 수정: 크래시/UB/OOM/결정론)

### P0-1. `days/delay` 컴파일 단계 검증
- 대상: `src/compiler/dag_builder.cpp`
- 내용:
  - `days` 계열(`tsmean/tsrank/tszscore/decay`)은 `1 <= days <= panel->T`
  - `delay`는 `0 <= delay <= panel->T`
  - 위반 시 즉시 `CompileError`
- 이유:
  - `ModuleInput` cap overflow 및 `% 0` UB 차단
  - `MTsRank`의 `w=0` 차단
- DoD:
  - 음수/0/초과 입력 케이스 컴파일 실패 테스트 추가

### P0-2. 멀티 알파 `all_signals` 제거
- 대상: `src/main.cpp`, 필요 시 `src/engine/simulator.*`
- 내용:
  - `all_signals[A][T][N]` 저장 제거
  - `combined_signals[T][N]`만 유지하며 알파별 실행 시 즉시 누적
- 이유:
  - 대형 요청에서 메모리 폭발(OOM) 차단
- DoD:
  - 대형 A/T/N 입력에서 RSS가 `O(T*N)` 수준으로 제한됨
  - `threads>1`에서도 combined 누적 순서가 알파 인덱스 기준으로 결정론 보장

### P0-3. `AlignedAllocator` 규약 준수
- 대상: `src/simd/simd_utils.h`
- 내용:
  - `allocate()`에서 null이면 `throw std::bad_alloc()`
- 이유:
  - STL allocator contract 위반/UB 가능성 제거
- DoD:
  - 관련 단위 테스트 추가(실패 경로 포함)

### P0-4. `MEvent` 재계산 제거 (정확성/결정론)
- 대상: `src/modules/Event.h`, `src/compiler/dag_builder.cpp`, `src/modules/SignalCache.h`
- 내용:
  - `signal_module->calculate()`, `trigger_module->calculate()` 재호출 제거
  - `<signal id>` 캐시 결과 참조로 변경
  - `init()`에서 하위 모듈 재초기화 제거
  - 규칙: `event.signal` 미지정은 연속 체인에서만 허용, 중간 `<data>` 재시작 후 미지정이면 컴파일 에러
- 이유:
  - 동일 bar 중복 계산으로 인한 state 오염 차단
- DoD:
  - 결정론/회귀 테스트에서 기존 대비 결과 안정성 확인

## P1 (핵심 성능: ROI 높음)

### P1-1. `MRank`를 `O(N log N)`으로 교체
- 대상: `src/modules/Rank.h`
- 내용:
  - `(value, index)` finite 목록 정렬 + tie group 평균 rank 반영
- DoD:
  - 기존 랭크 규칙(동점/NaN) parity 테스트 통과

### P1-2. `TsMean/TsZscore`의 `input(w)` hoist
- 대상: `src/modules/TsMean.h`, `src/modules/TsZscore.h`
- 내용:
  - 루프 밖에서 `const auto& old = input(w)` 1회 획득
- DoD:
  - 결과 동일, 성능 미세 개선 확인

### P1-3. Metrics slice 복사 제거
- 대상: `src/metrics/metrics_engine.cpp`, `src/metrics/summary_metrics.*`
- 내용:
  - `slice()` 기반 복사 제거
  - `compute_summary(..., start, end)` 인덱스 기반 계산으로 변경
- DoD:
  - 출력 동일성 유지 + 대량 알파 처리 시간 개선

### P1-4. Simulator 복사 최적화
- 대상: `src/engine/simulator.cpp`
- 내용:
  - `prev_weights = exec_w`, `prev_target = target` 반복 복사 감소
  - `mode=1`에서 swap 기반 업데이트 적용
- DoD:
  - 결과 동일성 테스트 + 성능 회귀 없음

### P1-5. AVX2 실사용 연결 (`__AVX2__` 조건 컴파일 유지)
- 대상: `src/modules/Decay.h`, `src/simd/m_decay_avx2.*`, `CMakeLists.txt`
- 내용:
  - 현재 placeholder 상태 제거, 실제 커널 연결
  - `#ifdef __AVX2__` + fallback 스칼라 경로 유지
  - 동일 패턴을 타 모듈에 확장 가능하게 인터페이스 정리
- DoD:
  - AVX2/스칼라 parity 테스트 통과

## P2 (메모리 구조 개선/대용량 대응)

### P2-1. `ModuleInput` flat ring buffer화
- 대상: `include/module.h`
- 내용:
  - `vector<vector<float>>` -> 단일 `vector<float>`(cap * num_assets)
  - push/read 인덱싱 단순화
- DoD:
  - checkpoint 포함 기능 동일 + 메모리/캐시 효율 개선

### P2-2. Parquet loader 중복 메모리 축소
- 대상: `src/data/parquet_reader.cpp`
- 내용:
  - `flat_sym` string row 저장 제거(심볼 인덱스화)
  - `std::map` 기반 unique/index 구성 비용 축소
  - 가능 시 dictionary encoding 활용
- DoD:
  - 대용량 로드 메모리 사용량 유의미 감소

### P2-3. 멀티 알파 `collect_signals` 정책화
- 대상: `src/main.cpp`, API contract 문서
- 내용:
  - multi 기본 `collect_signals=false`
  - 요청 파라미터로 선택 수집 허용(필요 시 top-K 제한)
- DoD:
  - 기본 경로 메모리 안정성 보장 + 계약 문서 명시

## P3 (구조 단순화/유지보수성)

### P3-1. SearchSpace sentinel 제거
- 대상: `src/ga/search_space.h`, `src/ga/search_space.cpp`
- 내용:
  - sentinel gene 트릭 제거
  - `template_` 정식 필드 도입
- DoD:
  - decode 로직 단순화 + 테스트 보강

### P3-2. range 파서 통합
- 대상: `src/ga/search_space.cpp`, `src/compiler/expansion.cpp`
- 내용:
  - `"..."` 파싱 로직 공용화
  - split('.') 취약 로직 제거
- DoD:
  - 동일 입력에 동일 파싱 결과 보장

### P3-3. expansion copy-heavy 완화
- 대상: `src/compiler/expansion.cpp`
- 내용:
  - `std::map` 복사량 축소(구조/생성 경로 개선)
- DoD:
  - 조합 확장 성능 개선 + 동작 동일

## Hot-loop 방어 분기 재정리 (P1~P2 사이)

### H-1. `compute_timeseries()` 사이즈 분기
- 대상: `src/metrics/timeseries_output.cpp`
- 내용:
  - 루프 내 `(i < vec.size())` 반복 분기 제거
  - 사전 계약 검증(assert/검증)으로 이동

### H-2. `MData::calculate()` 범위 분기
- 대상: `src/modules/Data.h`
- 내용:
  - simulator 계약이 보장되는 경로에선 불필요 분기 제거 여부 결정
  - 단독 호출 가능성을 고려해 정책 명확화

## 테스트 실행 기준
- 최소 회귀:
  - `test_modules`
  - `test_simulator`
  - `test_xml_compiler`
  - `test_combined_alpha`
  - `test_determinism`
  - `test_simd_parity`
- 성능/메모리 검증:
  - 대형 multi-alpha 요청 재현 벤치(고정 입력)
  - 변경 전/후 wall-time, peak RSS 비교 리포트 남김

## 진행 상태 표기 규칙
- `[ ]` 예정
- `[~]` 진행중
- `[x]` 완료

## 현재 상태 (2026-03-05)
- `[x]` P0-1 days/delay compile-time validation
- `[x]` P0-2 multi-alpha streaming combine + deterministic accumulation order
- `[x]` P0-3 allocator bad_alloc contract
- `[x]` P0-4 event cache-reference architecture
- `[x]` P1-1 rank O(N log N)
- `[x]` P1-2 TsMean/TsZscore input(w) hoist
- `[x]` P1-3 metrics slice removal
- `[x]` P1-4 simulator copy reduction
- `[x]` P1-5 AVX2 real path wiring
- `[x]` P2-1 ModuleInput flat ring
- `[x]` P2-2 parquet memory de-dup
- `[x]` P2-3 collect_signals policy
- `[x]` P3-1 search_space sentinel removal
- `[x]` P3-2 range parser unification
- `[x]` P3-3 expansion copy reduction
- `[x]` H-1 timeseries per-iter branch cleanup
- `[x]` H-2 MData bounds-check policy cleanup

## 최근 업데이트
- 2026-03-05: 멀티알파 합산 경로에서 worker별 `sim_out.signals (T*N)` 임시 보관을 제거했다. `Simulator` signal-row callback 기반으로 즉시 누적해 피크 메모리 리스크를 낮췄다.
- 2026-03-05: `ModuleInput` 저장소를 `vector<vector<float>>`에서 flat ring(`vector<float>`)으로 변경하고 직렬화 호환 경로를 반영했다.
- 2026-03-05: `SearchSpace`의 sentinel gene 템플릿 저장 트릭을 제거하고 `template_xml` 필드로 단순화했다.
- 2026-03-05: `ModuleInput` flat ring 변경에 대한 checkpoint round-trip 테스트를 추가해 직렬화 회귀를 방지했다.
- 2026-03-05: `ModuleInput` legacy wire format(`vector<vector<float>>`) 로딩 호환 테스트를 추가해 기존 체크포인트 파일 호환성을 검증했다.
- 2026-03-05: `search_space`와 `expansion`의 range 파서를 `range_parser` 공용 로직으로 통합해 파싱 불일치 리스크를 줄였다.
- 2026-03-05: 멀티 알파는 기본적으로 시그널 수집을 하지 않도록 하고, 요청 시 `collectSignalsTopK` 범위에서만 배치별 signal payload를 선택 수집하도록 정책화했다.
- 2026-03-05: `cartesian_attribs`를 DFS 기반 생성으로 바꿔 확장 단계의 중간 `std::map` 복사를 줄였다.
- 2026-03-05: `compute_timeseries`의 루프 내 size 분기를 제거하고, 함수 시작 시 입력 길이 계약을 1회 검증하도록 정리했다.
- 2026-03-05: `MData`의 범위 체크는 디버그 빌드에서만 유지하고 릴리즈 핫패스 분기를 제거했다.
- 2026-03-05: Parquet 로더에서 row별 symbol string/전체 컬럼 평탄화 버퍼 중복을 제거하고 row 인덱스 매핑 기반 직접 적재로 메모리 사용량을 줄였다.
