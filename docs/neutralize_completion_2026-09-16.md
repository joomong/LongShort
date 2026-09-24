# `<neutralize group="sector|industry"/>` 완료 기록 (2026-09-16)

계획: [neutralize_plan_2026-09-16.md](neutralize_plan_2026-09-16.md). 증거: [neutralize_2026-09-16/](neutralize_2026-09-16/) (oracle_report.md, run_report.md, ctest.log, ctest_summary.md), 그룹 패널 생성 리포트 `data/groups/<cache>/build_report.md`(gitignore).

진행 방식: 패키지 4개(A 데이터 도구, B `base:` 설정, C 엔진 모듈, D 통합·oracle)를 각각 별도 구현 에이전트(opus, low effort)가 만들고, 패키지마다 독립 리뷰(opus, high effort)를 받아 지적사항을 반영한 뒤 리드가 깨끗한 빌드에서 전체 검증을 재실행했다.

## 1. 결과 요약

| 항목 | 결과 |
|---|---|
| XML | `<neutralize group="sector"/>`, `group="industry"`, 파이프 `sector\|industry`(알파 2개), `<choice>` 안, `input="id"` 모두 동작 |
| 의미 | 신호 날짜 유니버스 마스크 → 값 유한·코드 유한·≥0 인 원소만 그룹 평균 → `x − mean_g`, 그 외 NaN, 단독 그룹 0. 팀 `cs_group_neut_1d`와 동일 |
| 기본 데이터 | `base: {cache, groups}` 블록이 `open high low close volume vwap cap sector industry`를 자동 등록·시작 시 전부 로드. `datasets:`는 추가분만 |
| industry 정의 | CIQ `simpleIndustryDescription` 74라벨(11 sector에 완전 중첩), 정적 방송 |
| 팀 sim 대조(oracle) | 엔진 `neutralize` vs `cs_group_neut`(엔진 rank 출력에 적용): max\|diff\| 2.98e-08, 일치 100%, NaN 위치 100%, 그룹합 최대 6.1e-07(sector)·2.4e-07(industry) — Top500 전체 유니버스 4,200 bar × 1,056 심볼(2010-01-04~2026-09-15, 리드 최종 재실행) |
| CTest | 26/26 통과, C++ 284 케이스 + Python 51 케이스, HTTP 계약 21/21 실행(리드 별도 빌드 재실행 동일) |
| 무중단 실행 | Top500/Top1000/ALL × XML 3종 9/9 HTTP 200, 최대 RSS 3.1GB(ALL) |

## 2. 변경 파일

| 구분 | 파일 |
|---|---|
| 엔진 | `cpp-engine/src/modules/Neutralize.h`(신규), `src/compiler/dag_builder.cpp`(태그 등록·에러 3종·`group` trim), `src/data/config_loader.{h,cpp}`(`base:` 전개), `src/data/parquet_reader.cpp`(그룹 패널 stale 가드), `tools/generate_modules.py`(`neutralize` 예약 태그) |
| 데이터 도구 | `cpp-engine/tools/data/build_group_panels.py`, `README_group_panels.md`, `tools/check_group_neutralize_oracle.py`, `tools/make_http_test_fixture.py`(`base:` fixture), `tools/run_top500_missing_close.py`(`base.*` 경로 절대화) |
| 테스트 | `tests/test_modules.cpp`(+9), `test_xml_compiler.cpp`(+7), `test_pipeline_contract.cpp`(+1), `test_golden_crossval.cpp`·`golden/generate_golden.py`·`golden/modules.json`(+2 엔트리, 팀 함수 대조 포함), `test_config_loader.cpp`(+3), `test_parquet_loader.cpp`(+5), `test_http_contract.cpp`(+3), `test_generate_modules.py`, `tests/test_build_group_panels.py`(신규 15), `CMakeLists.txt`(venv 인터프리터 등록, skip 코드 77) |
| 설정·문서 | `examples/datasets/*.yaml` 5개(`base:` 전환), `examples/datasets/README.md`, `README.md`, `CLAUDE.md`, 이 문서·계획 문서 |
| 생성 데이터(gitignore) | `data/groups/cache_v1_cash_1d/`, `data/groups/cache_v2_cash_1d/` — `sector.parquet`, `industry.parquet`, `sectorIx.parquet`, `industryIx.parquet`, `build_report.md`, `MANIFEST.json` |

## 3. 검증 표 (계획 §4.4 대응)

| 층 | 검사 | 입력 | 기대 | 실측 | 판정 |
|---|---|---|---|---|---|
| (a) 데이터 도구 | 모양·정렬·코드↔라벨·NaN·중첩·결정성·`--check-against` | 합성 6종목(열 순서 비정렬) | 15케이스 통과 | 15/15 (venv), CTest에서 venv로 실행·미설치 시 skip(77) | PASS |
| (a) 실데이터 | v1 재생성 vs base `sector.parquet` | 28,212심볼 × 11,817일(2026-09-16 캐시 기준 재생성) | 코드가 다른 셀은 base 시변 5심볼뿐 | 일치 97.858%(326,241,237/333,381,204), varies 5심볼 252셀, partial 609심볼(base 쪽 NaN), 상수-오코드 0 | PASS |
| (a) 실데이터 | v2 생성 | 22,871 × 6,713 | K=11/74, 라벨표 v1과 동일 | 동일(`sectorIx`·`industryIx` sha256 일치), 미분류 526심볼 | PASS |
| (b) 설정 | `base:` 전개(가격 exact·그룹 asof)·필수 키·`datasets` 생략·중복·부재 파일 경로 | 임시 YAML·합성 wide | 9필드 등록, 부정 15건 문구 일치 | config 11/11, parquet 34/34 | PASS |
| (b) 로더 | 심볼 순서 섞인 그룹 파일 재정렬 | 합성 | `panel.symbols` 순서, NaN 보존 | 반례(인덱스 반전 빌드)에서 FAIL 확인 후 PASS | PASS |
| (b) 가드 | 캐시가 그룹 파일보다 뒤로 늘어남(asof로 이어짐) / 그룹 파일이 늦게 시작 / 다른 심볼 집합 | 합성 | 앞은 로드 성공·마지막 분류 유지, 뒤 둘은 기동 실패 + 날짜·도구명 메시지 | 3/3 | PASS |
| (c) 모듈 | demean 8케이스 + 음수 코드 | 손계산 벡터 | `[1,2,3,10,20]/[1,1,1,2,2]→[−1,0,1,−5,5]` 등 | 9/9, 변이 5종(마스크·코드필터·min-count·스케일·di) 전부 FAIL 확인 | PASS |
| (d) 컴파일러 | 속성 누락·`country`·필드 미등록·파이프·choice·input·trim | XML | CompileError 문구 / 알파 2개 | 7/7 | PASS |
| (d) 파이프라인 | rank→neutralize 시뮬레이터 mode 0/1 | 합성 패널 | 날짜별 그룹합 < 1e-6, 마스크 밖 NaN | 1/1 | PASS |
| (e) 골든 | numpy 기준 + 팀 `cs_group_neut` 대조 | 6×3 [1,1,2], 6×5 [1,1,2,NaN,−1] | 3중 일치 | max\|diff\| 0.0, 기존 7엔트리 무변경 | PASS |
| (f) HTTP | `/api/config` columns, simulate 200/2알파/400 | `base:` fixture | 9필드 노출, 오류 문구 | 21/21 | PASS |
| (g) oracle | 엔진 B vs `cs_group_neut(A)` | Top500 전체, 2010~ | max\|diff\| < 1e-5, 그룹합 < 1e-4 | 2.98e-08 / 6.1e-07(sector), 2.4e-07(industry) | PASS |
| (g) 참고 | 팀 체인 `cs_group_neut(cs_rank(returns[valid]))` | 같은 패널 | 보고만 | rank 규약 차이(엔진 `r/n`, 팀 `(r−1)/(n−1)`) 보정 후 일치 97.7%/99.4%, 잔차는 rank 단계(동률 순서)에서 발생 | 참고 |
| (h) 무중단 | Top500/1000/ALL × XML 3종 | 예제 설정 | exit 0 | 9/9 200, ALL 3.5s/RSS 3.1GB | PASS |

## 4. 알려진 한계·후속

| 항목 | 내용 |
|---|---|
| 정적 분류 | 현재 마스터 분류를 전 기간 방송(look-ahead). 마스터 일별 아카이브에 분류 컬럼이 2025년 말부터만 있어 PIT 이력 불가. 이력이 생기면 같은 wide 포맷으로 교체 |
| 그룹 파일 갱신 | 그룹 패널은 asof 정렬이라 캐시가 매일 늘어나도 기동·분류가 이어짐(WP-D 리뷰 P1: exact였다면 하루 뒤 기동 실패). 새 심볼은 `build_group_panels.py` 재실행 전까지 미분류. 늦게 시작하거나 다른 캐시용 파일은 로더 가드가 기동 거부 |
| 열 라벨 타입 | 파이썬에서 그룹 parquet은 문자열 컬럼, base `close.parquet`은 int64 컬럼으로 복원됨 — 섞어 쓰면 전부 NaN. 도구·oracle은 `astype(str)`로 정규화 |
| `/api/config` | `base_cache`/`base_groups`는 설정 구조체에만 있고 API 노출은 미구현 |
| `market` 그룹 | 전체 demean(팀 `cs_neut`)은 범위 밖, 필요 시 소규모 추가 |
| 골든 생성기 | `metrics.json` 키 순서가 재생성마다 바뀌는 기존 잡음(값 동일) |
