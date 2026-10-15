# 리팩토링 감사 목록 (2026-10-14)

대상: `longshort_platform` 작업 트리(미커밋 포함). 엔진 src 8,251줄, 프론트 소스 5,228줄.
방법: 지도 에이전트 2(측정) → 서브시스템 점검 5(읽기 전용, 근거 필수) → 도메인별 적대적 검증 2(재현) → 리드 판단.
발견 82건 + 검증자가 추가한 이음새 6건. 검증 결과 확인 67 / 부분 확인 15 / 반박 0.
근거 원문은 `docs/refactoring_audit_2026-10-14/` (MAP_*, FINDINGS_*, VERIFY_*). 아래 ID는 그 파일의 항목 번호임.

---

## 0. 시작 전 전제

1. **커밋.** 작업 트리에 수정 92·삭제 297·신규 30 파일이 있음. 리팩토링은 커밋된 기준점 위에서만 함.
2. **골든 출력.** 리팩토링 판정은 "숫자 동일"로 함. 준비물:
   - 엔진: HTTP 픽스처(`make_http_test_fixture.py`)에 고정 XML 세트(data/delay/tsmean/rank/zscore/neutralize/choice 조합 10개 안팎)를 `/api/simulate`(metricsOnly·full)와 `/api/ga/stream`으로 돌린 JSON 해시. 실데이터 골든은 별도 포트 엔진으로 XML 3~5개 metricsOnly만.
   - 프론트: 실제 엔진에서 캡처한 **스트리밍** NDJSON 픽스처(현재 없음, F2-F5). 이게 없으면 프론트 리팩토링은 게이트가 없음.
3. **레포 밖 결합(기록 필요).** `universe_builder`의 Airflow DAG가 `cpp-engine/tools/restart_engine.sh --build/--prev`를 직접 실행함(`universe_builder/dag/run_step.sh:21`). `tools/data`는 `universe_builder/cache`로의 심링크이고 CTest `test_build_group_panels`가 그걸 import함. 엔진 빌드 의존성 prefix는 `cpp-engine/examples/datasets/.longshort/deps/`(gitignore)에 있음. 이 셋은 지우면 안 되고, 레포 어디에도 적혀 있지 않음 → 헤더 주석 한 줄씩 추가(V1, V2).
4. **현재 저장된 알파 0개.** 저장 파일은 HTTP 응답 본문을 그대로 쓰므로(E1-F7) 응답 형태를 바꾸려면 **누군가 알파를 저장하기 전**이 유일한 기회임. §2-C를 먼저 할 이유.

---

## 1. 등급 A — 지금 고칠 것 (틀린 답·운영 위험, 전부 국소 수정)

| # | ID | 내용 | 근거 | 고치는 모양 | 게이트 | 공수 |
|---|---|---|---|---|---|---|
| A1 | E3-F3, V3 | `<data>` 속성 오타(`dealy`, `Delay`)가 delay 0으로 컴파일 → **같은 바 look-ahead**, 오류 없음. 내장 모듈 전부 같은 문제(`dayz`→기본 20, `ddof="1.9"`→1, `pct="flase"`→0) | g++ 프로브 재현 | 내장 팩토리도 생성 모듈과 같은 `validate_keys`/`parse_int` 사용. `RESERVED_TAGS`는 레지스트리에서 유도 | `XmlCompiler.DataRejectsUnknownAttribute`, `BuiltinRejectsUnknownAttribute` 추가 | S |
| A2 | F1-F11 | 데이터 시작일이 2016-01-01(휴일)이라 `fetchConfig`가 "범위 밖"으로 보고 기본 OS 시작 2024-01-01을 매 페이지 로드마다 2023-07로 되돌림. 실서버 화면에서 확인됨 | 프로브 재현 | `fetchConfig`의 outOfRange/resetSplit 블록 삭제, `enforceSettingsDates`만 호출 | `alphaStore.test` + "시작만 클램프되면 분할 유지" 테스트 | S |
| A3 | E1-F4 | 저장의 `batch.json` 단계에 `threads`가 안 넘어가 min(32, 행수) 스레드 사용. 주석은 20이라 적힘 | 스크래치 서버 재현 | `batch_request["threads"] = thread_count` 한 줄 | `ResultFilesEqualLiveSimulateBodies` | S |
| A4 | E1-F6 | 단일 알파 `/api/simulate`가 클라이언트 끊김을 무시. 프론트의 가장 잦은 요청 3종(detail·signals·series)이 전부 이 경로 | 재현(끊긴 요청이 0.71s CPU 계속 사용) | main.cpp:466에 GA와 같은 `cancelled` 콜백 전달 | `SimulateStopsWhenClientDisconnects` 추가 | S |
| A5 | F2-F7 | GA 스트림 파싱/핸들러 예외 시 `reader.cancel()`을 안 불러 엔진이 GA를 계속 돌림 | 프로브(cancel 0회) | `runGAStream`의 `finally`에서 cancel | `apiClient.contract` | S |
| A6 | E1-F5, E2-F5 | `threads` 기본값이 엔드포인트마다 다름(32/20/min(hw,20))이고 simulate는 상한 없음(200 스레드 생성 재현). UI는 1..32로 막지만 API는 안 막음 | 재현 | `parse_run_options`에서 한 번만 `min(요청 or 20, 32, 작업수)` | `SimulateRejectsThreadsAbove32` 추가 | S |
| A7 | E3-F1 | 예제 설정 3개 중 2개(`equities_monthly_afl.yaml`의 `Top500`, `compare_v2_all.yaml`의 `ALL`)가 현재 캐시에 없는 유니버스 이름이라 시작 실패. README가 그 이름을 안내함 | pyarrow로 이름 확인 | 두 YAML 삭제, README 15행 수정 | `test_example_configs`(캐시 없으면 skip) | S |
| A8 | E3-F7 | 설정의 모르는 키를 무시 → `missing_close_polcy: error`가 조용히 carry로 동작 | 프로브 재현 | 블록별 허용 키 목록 1개, 4개 레거시 키 전용 거부 삭제 | `ConfigLoader.RejectsUnknownKeys` | S |
| A9 | E3-F2 | `<choice bind>`와 `<shuffle>`이 변형을 빠뜨리거나 잘못 짝지음(세 번째 bind 멤버 고정, shuffle이 자식 변형 버림). 테스트 0, 사용처 0, GA도 상속 | 프로브 재현 | 두 태그 삭제(`choice`는 유지), math_contract 181행 수정 | 거부 테스트 | S |
| A10 | V1(프론트) | 저장본 불러올 때 유니버스가 없으면 UI는 대체 유니버스를 쓴다고 표시하지만 `runSettings`엔 원래 이름이 남아 라운드 탭 combined·Signals 재실행이 400, 그 오류는 영구 캐시 | 코드 추적 | `runSettings.universe`를 모달이 고른 값으로 | 테스트 추가 | S |
| A11 | E2-F1 | close 체결에서 후속주(successor)가 시세 없는 종목에 떨어지면 동결 예산을 건너뜀 → gross 1.5, error 정책도 안 던짐(OHLC4/VWAP는 정상). `math_contract` 54·56행 위반 | 스크래치 빌드 재현. 현 캐시의 후속 이동 2,963건 중 해당 0건, 한 바만 지속, 실서버는 vwap | 동결·error 게이트를 `apply_successor_transfers` 뒤에서 세 체결가 공통으로 한 번 계산 | `ThinMoves.SuccessorIntoQuotelessDestinationReservesBudget` 추가, 기존 frozen_gross 핀 유지 | M |


### 1.1 진행 상황 (2026-10-15)

A1~A11 전부 수정 완료. 방식: 파일 소유권으로 6개 패키지(E-A main.cpp / E-B 컴파일러 / E-C 설정 / E-D 시뮬레이터 / F-A 스토어 / F-B API 클라이언트), 패키지마다 코딩 에이전트 1 + 적대적 리뷰어 1, 리드가 별도 빌드로 재검증. 전부 미커밋.

| 패키지 | 항목 | 리뷰 | 리뷰에서 잡힌 것 |
|---|---|---|---|
| E-A | A3, A4, A6 | PASS | skip 게이트를 wall→CPU 시간으로, 미사용 include (리드 반영) |
| E-B | A1, A9 | PASS (2회) | 1차 PASS 뒤 같은 부류 2건 추가 수정: 비-choice 요소의 자식 묵살(`<data><delay/></data>`가 delay 0으로 컴파일), 구조 태그의 `input` 묵살 |
| E-C | A7, A8 | PASS | `test_example_configs`가 상대 `base.cache`를 cwd 기준으로 풀고 universe parquet 부재를 skip 처리 (리드 반영); README 주의문 복원 |
| E-D | A11 | PASS | 30,000 패널 차분 퍼징: 차이는 목표 사례(close 체결·후속주→시세 없는 종목)뿐. 메시지 문구·테스트 정밀도 (리드 반영) |
| F-A | A2, A10 | FAIL→PASS | **A2 전제 오류**: `enforceSettingsDates`는 simEnd 뒤 분할만 리셋하므로 블록 삭제만 하면 데이터 창이 기본 분할 뒤/앞에 있을 때 IS가 1일로 붕괴. 좁힌 가드 `osStart <= minDate || isStart > maxDate`로 수정, 4개 케이스 핀 |
| F-B | A5 | PASS | 실 스트림(Node fetch) 프로브로 abort/done/truncated 경로 불변 확인 |

새 테스트: 엔진 gtest +9 (xml_compiler 41→47, thin_moves 14→15, config_loader 11→13, http_contract 36→38), 파이썬 `test_example_configs`·`ReservedTagsMatchEngineRegistry`, 프론트 +6 (124→130). 삭제: `examples/datasets/{equities_monthly_afl,compare_v2_all}.yaml`, `<shuffle>`·`<choice bind>`, `get_int/get_double/get_bool`, 레거시 4키 전용 거부.

리드 검증: 새 빌드 경고 0(서드파티 drogon 2), CTest 31/31(HTTP 픽스처 포함, skip 0), 프론트 단위 130/130·tsc·lint·e2e 20 통과(1 skip = 라이브 엔진 spec). 실데이터 골든(라이브 50007=수정 전 vs 수정 빌드 50161, 같은 워크스페이스 데이터): XML 7개 metricsOnly·combined 3개·full 1개(47MB, sha256) **9/9 동일**. 거부 케이스는 수정 전 200(`dealy` 알파 IS Sharpe 0.675 vs 올바른 delay 1은 0.595 — look-ahead 부풀림의 실측)에서 수정 후 400(`threads` 0/33, `dealy`, `dayz`, `<shuffle>`).

문서 반영: math_contract(threads 규칙·shuffle 제거), CLAUDE.md·data_contract(모든 미지 키 거부), examples README, plan/progress, `xml_parser.h` 주석. §0-3 레포 밖 결합은 `restart_engine.{sh,py}`·`test_build_group_panels.py` 헤더와 `build_and_validation.md`에 기록함.

추가(2026-10-15, B-C 일부 선행): `combined_mode`/`combinedMode`(raw 합산 스위치) 삭제. combined·meta EW는 항상 "알파별 gross 1 비중 → 합 → 재정규화"로 고정. 사용자 결정(포트폴리오 meta 모드 설계 논의에서). 워크스페이스 config의 `combined_mode:` 줄도 제거(A8 이후 미지 키는 시작 실패이므로 재시작 전 필수).

추가(2026-10-15, 새 기능 meta 1·2단계, 별도 커밋): `meta_dir`·`<meta expr>`·`<ew/>`·`MMetaSource`·`MetaSetCache`/`MetaWorkerBudget`. 3패키지(M1·M2a·M2b) 코더+리뷰어, 리뷰 후속 각 1회. 실데이터 `<meta>` == combined 행 동일. 상세는 `cpp-engine/docs/math_contract.md` "Meta portfolios"와 CLAUDE.md.

리뷰가 남긴 후속 항목(이번에 안 함, C로 편입):
- **E2-F1 꼬리**: carry 정책에서 후속주가 **부정 가격(corrupt close)** 종목에 떨어지면 거부 대신 동결됨(`require_priced_quotes`가 이전 북만 봄). 기준선 OHLC4도 같았음. math_contract 58-59행("비양수 가격은 관련 포지션에서 거부") 위반. 공유 루프에 `kQuoteInvalid && carried != 0 → throw` 한 줄.
- 결합 재실행(`run_from_signals`)은 행 훅이 없어 시작 후 취소 불가(사전 체크만). 저장 행 단계도 행 사이에서만 취소.
- `check_group_neutralize_oracle.py`의 `DEFAULT_GROUPS/DEFAULT_CACHE/--universe`가 여전히 cache_v1 → E3-F10(오라클 정리)과 함께.
- `fetchConfig` 분할 규칙의 경계: osStart가 데이터 시작 직후면 IS 1~2일(의도된 "클램프는 분할 유지" 규칙, 현 워크스페이스는 해당 없음). 최소 IS 길이는 새 규칙이라 보류.
- `ddof/min_count/eps/maxholddays` 범위 검사 없음(기준선과 동일).
- 저장본 로드 시 `executionPrice`도 유니버스처럼 엔진 미지원 값을 그대로 재실행에 씀(A10의 자매).
- GA 템플릿 경로는 자식 요소 오류를 후보 컴파일 시점에야 냄.

---

## 2. 등급 B — 구조 정리 (순서가 있음)

### B-1. 프론트가 쓰지 않는 GA 응답 경로 삭제 (F1-F3 ≡ F2-F2, E1-F2, F1-F4, F2-F5)
- 사실: 프론트는 항상 `streamCandidates: true`를 보냄(api.ts:309). 엔진의 `complete`/`generation_end` 경로(main.cpp 1144·1164-1191, `popSize`/`generations` 별칭)는 이 플래그가 false일 때만 돎.
- 그런데 프론트 핸들러 60여 줄, `adaptGACompleteToResponse`, `setResult`의 non-null 경로, `mockRun`, e2e 11개 중 7개, 단위 테스트 4개, 픽스처 `cpp_ga_events.ndjson`이 전부 그 경로 위에 있음. 엔진 쪽도 GA 테스트 4개 중 3개가 옛 경로.
- 결과: 어제의 "단일 알파가 히트맵으로 안 넘어감" 버그를 테스트가 못 잡은 이유. 실제 흐름(첫 후보 자동 선택, 라운드별 combined 요청, `search_start` 에코, `adaptGACandidate`)은 어느 테스트도 안 탐.
- 순서: (1) 실제 엔진에서 스트리밍 NDJSON 캡처 → `mockRun`이 이벤트를 청크 단위로 재생 → (2) 픽스처 4개 재생성(현재 9/11 캡처, `mode` 키 남아 있고 엔진이 지금 쓰는 키 15개 없음, drawdown 값은 net 기준) → (3) 양쪽 코드 삭제 + `math_contract.md:197-199` 삭제.
- 공수 M–L. **B-2의 전제.**

### B-2. 결과/뷰 상태머신을 유도값으로 (F1-F1, F1-F2, F1-F6, F1-F7, F1-F8, F1-F9)
- `viewMode`는 쓰는 곳 11군데지만 16,000스텝 퍼징에서 `selectedAlphaIndex !== null`과 한 번도 다르지 않았음 → 필드·`setViewMode`·`ViewMode` 타입 삭제.
- 기본 선택 규칙이 5군데(setResult, appendGACandidate, selectResultTab, loadSavedResult, singleAlphaSignals). 어제 수정은 live finish/stop만 덮고, 저장본 로드·라운드 탭 클릭은 여전히 Performance. **판단:** 저장본 로드는 Performance 우선이 의도(재실행 없이 성과 보기)이므로 유지하되, "저장본→Performance, 실시간 단일→Signals" 규칙을 **한 곳**에 둠. 검증자가 지적한 `explicitPerformance` 플래그 추가는 안 함.
- 멀티 알파 시그널 LRU(5개)·`signalCacheOrder`·`selectSignalAlpha`·`selectedSignalAlphaIndex`: 시그널은 알파 1개일 때만 받으므로 도달 불가 → 삭제.
- `gaProgress`는 `isRunning`+`gaRounds`에서 유도됨(렌더되는 건 `.generation` 하나) → 삭제. `gaSession`은 result가 있으면 항상 true → 삭제.
- 탭 가시행 술어·정렬 비교자가 5군데 → `lib/gaCombined.ts`의 `visibleRows(alphas, tab)` 하나로.
- 공수 S×6. 게이트: `gaResults`·`alphaStore` 테스트 + 불변식 퍼징 테스트 추가.

### B-3. API 클라이언트 정리 (F2-F8 ≡ F1-F12, F2-F3, F2-F9, V2)
- `runSimulate`(위치 인자 6개+옵션)·`runGAStream`(위치 인자 13개, 이름은 popSize/generations인데 전송은 firstSearch/stopRound) → `(xml, runSettings, options, signal)`. GA 기본값이 4층(store/useBacktest/api/engine) → store 한 곳.
- 어댑터의 `?? resp.<top-level>` 폴백이 엔진 필드 누락을 숨김(프로브: 배치 timeseries를 지우면 combined 곡선이 `alpha_1` 이름으로 표시됨) → 단일 배치 어댑터, 누락 시 throw. 검증 로직 추가 말고 폴백 삭제만.
- FastAPI 시절 `detail` 오류 파싱, `deleteAlpha`(호출 0), 모든 요청에 붙는 `collectSignals/TopK`(엔진이 프론트 요청에서 안 읽음) 삭제.
- 공수 S×4.

### B-C. 엔진 응답 형태 — **저장된 알파가 0개인 지금** (E1-F7 결정 → E1-F8, E1-F13, E1-F1)
- 결정(리드 추천): 저장 파일은 응답 본문 그대로 유지(단순). 대신 형태 정리를 **지금** 끝내고, 그 뒤엔 저장 파일이 형태를 고정한다는 사실을 `alpha_storage.h`에 적음.
- E1-F8: `copy_top_metrics`로 지표가 최상위와 `batches[0]`에 두 번 직렬화됨(행당 ~0.5MB 추정) → 삭제, 프론트는 배치만 읽음.
- E1-F13: 와이어 키 `minBet`=평균 종목 수, `minBetMin`=최소 종목 수를 양쪽이 서로 되돌림 → 내부 이름으로 전송, 어댑터 rename 삭제. `data_contract.md`·CLAUDE.md의 "legacy key" 문장 삭제.
- E1-F1: 응답이 불리언 5개×단일/다중의 곱. 프론트는 4가지만 씀(detail/signals/series/aggregate). 상위 K 재실행(최대 5개 재시뮬)과 combined 시그널 페이로드는 테스트만 도달 → **삭제만**(요청 종류 enum 같은 층 추가 안 함). `math_contract.md:204-208` 수정.
- 공수 M. 게이트: `test_http_contract` + 프론트 contract 테스트 + 골든.

### B-4. 엔진 중복 로직 (E2-F2 ≡ E3-F8, E2-F3, E2-F4+V4, E2-F14, E3-F15, E1-F10, E1-F3, E1-F12)
- GA `search_space.cpp`가 두 번째 XML 확장기·직렬화기. `5|5|10`이 simulate에선 3개, GA에선 2개 → `expand_value`·`pipeline_to_xml`을 export해 GA가 쓰고 `split/try_parse_range/escape_attribute/element_to_xml` 삭제. 중복 제거 규칙 하나로 통일, `math_contract.md:180` 수정.
- ×100 단위 변환이 3곳(serializer/yearly/timeseries)이고 API가 퍼센트포인트와 분수를 섞음(summary mdd는 pp, timeseries drawdown은 분수, fitness는 /100으로 되돌림) → 계산은 분수만, ×100은 serializer에서만. API 불변.
- Sharpe 4벌, drawdown 엔진 3벌+프론트 1벌(V4). 엔진 `drawdown`·`frozenGross`·`missingData`는 프론트가 안 읽음 → sharpe/drawdown 함수 하나씩, drawdown 소유자는 엔진으로 정하고 프론트 재계산(F2-F1, ~150줄) 삭제. 단, F2-F1의 "정확 복제" 증명은 isStartIndex=0일 때만 확인됨 → 재생성 픽스처로 isStart>0 케이스 핀 먼저.
- 모듈 구동 루프 2벌(`run` vs `run_signal_rows`), `data` op 처리 2벌(dag_builder 278-305 ≡ 313-337), 병렬 워커 루프 2벌(main.cpp 473-535 ≡ 756-819, 실패 분기는 한 번도 실행 안 됨), 상태코드 매핑 3벌(같은 런타임 오류가 simulate 400·save 500·GA 런치 실패는 **빈 200**) → 각각 하나로.
- 공수 S×6, M×1.

---

## 3. 등급 C — 삭제·정리 (독립, 아무 때나)

| ID | 내용 | 판단 |
|---|---|---|
| E2-F6 ≡ E3-F9 | `run_logic_tests.py`는 현재 트리를 컴파일 못 함(arrow/api.h 3개 TU). CLAUDE.md·README·build_and_validation이 아직 안내 | 삭제 + 문서 3곳 |
| E2-F11 ≡ E3-F13 | 체크포인트: 런타임 호출 0, 시뮬레이터 상태와 불일치, 모듈 9개에 boost 직렬화 보일러플레이트, 모든 TU에 boost 헤더 | `CheckpointManager`·`Module::checkpoint`·`ModuleInput::serialize`·테스트·boost 의존성 삭제 |
| E2-F8 ≡ E3-F6 | `bench_2000_breakdown`은 모든 현재 설정에서 abort. 그것만이 4-인자 `DataPanel::load`·`load_parquet`·레거시 first-dataset 뷰·`selected_fields`의 비테스트 사용자 | 벤치·CMake 타깃·레거시 로더 표면 삭제, 테스트는 `DataPanel::load(config)`로 |
| E2-F7 | GA 평가기 캐시는 `scheduled` 집합 때문에 절대 히트 못 함(히트하면 오히려 "Missing payload"로 GA 종료). `run`/`run_streaming`/`evaluate_batch`/`cache_hits`/`history` 호출 0 | 삭제, 테스트는 `run_detailed`로 |
| E2-F9, E2-F10 | 실행 모드 제거의 잔재(`execution_intent()`, 골든 6케이스 중 3개 skip) | 삭제 |
| E2-F13 | `SimulatorConfig.T/N`은 패널과 항상 같아야 함(생성자가 검사), 42곳이 복사. `bars_per_year=365` 선언 5곳 | 필드 삭제, 기본값은 config_loader에만. 테스트 churn 커서 M |
| E3-F10 | 오라클 5개(≈1.5k줄) 참조 0. 결측 오라클 절반은 엔진이 거부하는 정산 규칙 | 팀 패리티·결측 self-check 2개는 CTest(skip 코드)로 등록, 나머지 삭제, 정산 분기 삭제 |
| E3-F11, E3-F12 | 지운 docs·`tools/data`로의 댕글링 링크(parquet_reader 오류 메시지 2곳, README, CLAUDE.md, data_contract 섹션 번호 인용 7곳), 예제 README의 v1 설명·`.bak` 3개·`uploads/tmp` 빈 디렉토리 256개 | 정리. **단 `examples/datasets/.longshort/deps`는 라이브 빌드 prefix라 유지**(V2) |
| E3-F14 | parquet_reader 923줄 중 ~200줄이 Arrow 보일러플레이트 복사(open 3벌, 타입 switch 5벌, `read_long_doubles`≈`read_field`) | `open_reader`·템플릿 `read_field` 하나로. M |
| E3-F4 | 선택 유니버스가 `dynamic_cast`로 Rank/Zscore/Neutralize 세 모듈에만 전달. 사용자 모듈은 항상 기본 유니버스 | 팩토리에 마스크 포인터 전달, cast 루프 삭제. 지금은 사용자 모듈 0개라 잠복 |
| E3-F16 | 손익에 영향 있는 가드(thin 이벤트 날짜가 바가 아닐 때 거부, :889-892)가 테스트 0 | 테스트 1개 추가. `symbol_labels_path`는 사용처 0 → 삭제 |
| F1-F14, F2-F13 | `BacktestMetrics`(131줄, 헤더가 CLAUDE.md 명명과 모순)·`SummaryMetrics` import 0, 렌더 테스트만이 살림. `rank`·`longRatio`·`ir`·`corr` 등 쓰기만 하고 안 읽는 필드 | 컴포넌트·테스트·필드 삭제 |
| F2-F4, E1-F9 | 시그널 요청마다 히스토그램 3개 계산(분위수 클립 `distribution`은 로그 히스토그램이 없을 때만 표시되는데 엔진은 항상 보냄). T×N 복사(~47MB 추정)와 2회 `nth_element` | 엔진 `distribution`·프론트 `distribution*` 4필드 삭제. `signalRange`는 툴팁에 표시되므로 로그 히스토그램 min/max에서 유도 |
| F2-F10, F2-F12, F2-F11 | 수수료 기반 gross 재구성(기본 10bp, 엔진은 1.5bp)·도달 불가 `minBet` 차트, CSS와 어긋난 하드코딩 팔레트 4개, 같은 시그널 값 포맷터 5벌(0.00001234가 패널마다 "0.000012"/"1.23e-5"/"0.0000") | 삭제·통일 |
| F1-F13, F1-F15 | `useBacktest`의 중복 리셋(revision 두 번 증가)·ref 미러링; 로더 3개의 오류 정책이 다름(detail·combined 오류는 영구 캐시, signals만 Retry) | 정리; 재선택 시 `detailError` 지움 |
| F2-F6 | 히트맵 정규화 경로(실서버 ~4M셀, 워커 사용)는 테스트가 못 닿음(e2e 최대 20k셀), 캐시 2벌 | 캐시 하나로, ≥500k셀 테스트 1개 |
| E1-F14 | Drogon 본문 1MiB 기본 한도 < 엔진의 5000개 한도. 1.1MB 요청은 JSON 아닌 빈 413 | 한도 하나로 맞춤 |
| E1-F15 | 핸들러의 실패·취소·동시성 절반이 테스트 0 (목록은 원문) | B-4와 함께 테스트 6개 추가 |
| E1-F20, V1 | 런처 스크립트의 조용한 폴백(`--modules-dir` 오타 시 모듈 없이 빌드), `restart_engine.py` 테스트 0 | 폴백은 오류로. **`restart_engine.sh`와 `--build/--prev`는 DAG 진입점이라 유지** |
| E3-F5 | 재시작 ~190s. 51M행 `base_data.parquet` 좌표를 8번 디코드, `returns.parquet`를 float·double로 두 번 적재(~70MB 중복) | 파일당 1회 디코드. M, 값 테스트 있음. 성능 항목이라 마지막 |
| E1-F17 | 저장이 행 파이프라인을 최대 3회 실행(검증 컴파일·행 시뮬·batch 재시뮬) | 행 통과 시 시그널을 누산기에 바로 → M, 성능 항목 |

---

## 4. 등급 D — 하지 않을 것 (근거와 함께)

| ID | 제안 | 안 하는 이유 |
|---|---|---|
| F1-F5 | 로더가 revision 변경 때만 abort | 동시 엔진 요청 상한이 사라져 in-flight 레지스트리가 필요해짐(과설계). 대신 A4(엔진 취소)로 비용을 없앰 |
| F1-F10 | 선택 동그라미 삭제 | 사용자 결정: 제출 기능 예약. 유지 |
| F2-F14 | 핀 스냅샷 삭제/초기화 | 실행 간 비교가 의도일 수 있음. 사용자 확인 전 보류 |
| F2-F16 | 레이아웃 픽셀 e2e 삭제 | 그 수치들은 의도된 레이아웃 결정을 지킴. 문자열 부재 검사 8개만 삭제 |
| F2-F17 | `SignalHeatmap` ref 구조 재작성 | 가독성 항목, 동작 문제 없음. M 공수 대비 가치 낮음 |
| E1-F11 | GA 핸들러의 JSON을 `EvaluationResult`로 | jsoncpp가 GA 라이브러리로 들어감. 본문만 static 함수로 빼는 건 함 |
| E1-F18 | 워커 그룹 통합 | 셧다운 비용이 합이 아님(검증자 반박) |
| E2-F2의 cap 지적 | `<choice>`가 5000 확장 cap을 치는 것 | 문서화된 동작(`math_contract.md:180-182`) |
| E3-F12의 `.longshort/` 삭제 | 예제 폴더 정리 | 라이브 빌드 의존성 prefix. 옮기려면 `build_and_validation.md:58-60`과 CMakeCache 같이 |

---

## 5. 추천 순서

1. 커밋 → 골든(§0-2) → 레포 밖 결합 주석(§0-3).
2. **A1~A11** (하루). 전부 국소이고 서로 독립. A1·A2·A3·A4는 오늘 당장.
3. **B-C**(응답 형태) — 저장된 알파가 생기기 전에.
4. **B-1**(옛 GA 경로 삭제 + 스트리밍 픽스처) — 프론트 리팩토링의 게이트.
5. **B-2 → B-3** (프론트), 병렬로 **B-4** (엔진).
6. **C**는 빈 시간에 하나씩. 성능 항목(E3-F5, E1-F17, F2-F6)은 마지막.

각 단계는 "작은 PR + 리뷰 + 골든 동일 + 테스트 수 보고"로 닫음.

---

## 6. 점검에서 "이상해 보이지만 괜찮음"으로 분류된 것 (재조사 불필요)

- `cold_read_mutex`가 모든 Parquet 읽기를 직렬화: 디코드 중 일시 메모리(~1.6GB)를 묶어 두는 의도. 필드당 1회라 요청 지연은 첫 터치뿐.
- GA 후보당 T×N 버퍼 없음: 후보는 시그널 수집 없이 돌아 워커당 0.5~0.84MB. T×N(52MB)은 단일 시뮬·GA best에서만.
- 모듈이 실행 시간을 지배(실규모 합성 패널: 북 89ms, rank 추가 시 602ms, 지표 0.1ms, 컴파일 0.02ms). 북의 중복 시세 분류는 수 % 이하.
- gross/net·북 크기(전체, /2 없음)·부동소수 누적 순서는 경로 간 일관. 결합 합산 순서 고정은 테스트가 핀.
- `GADetailLoader`의 `KEEP` 집합·`stopGA` no-op·requestId+abort 패턴·revision 가드·`startGARound` follow 규칙: 전부 의도된 것이고 테스트가 핀.
- 날짜 산술(`T12:00:00Z`+setUTCDate)·ISO 문자열 비교: 타임존 안전.
- 히트맵 워커 자체는 정당(4M셀 ~610ms). 문제는 캐시 중복과 테스트 부재뿐(C).

## 7. 미확인

- 결합 normalized 모드에서 thin 패널의 은퇴 종목 마스크: `add_block`이 `normalize_intent`의 은퇴 NaN 마스크 없이 정규화함. 은퇴 종목이 유한 시그널(상수 등)을 내면 알파별 비중이 틀어질 수 있음. 2-알파 thin 프로브로 확인 필요.
- 실엔진 청크 경계(round_end/round_start/search_complete가 별도 청크인지): F1-F5의 abort 빈도를 결정. 로그로 확인.
- E3-F5의 190s 중 디코드 비중, E1-F8의 실제 바이트, E1-F14의 실제 GA XML 길이: 측정 안 됨.
