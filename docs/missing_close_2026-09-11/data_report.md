# data 담당 보고 — 결측 종가·상폐 이벤트 로딩 (2026-09-11)

작업 트리 `/tmp/longshort_wt_data`(브랜치 wip/data-events, HEAD 2e77af1). 커밋 없음. `/home/base` 읽기만 함. 메인 트리 미수정(`git apply --check`만 실행).

## 1. 변경 파일

| 파일 | 내용 |
| --- | --- |
| `cpp-engine/src/data/config_loader.cpp` | `backtest.{missing_close_policy, default_delisting_return, stale_quote_settle_bars}` 파싱·검증, 최상위 `events:` 파싱(`parse_events` helper 추가) |
| `cpp-engine/src/data/parquet_reader.cpp` | 이벤트 로딩(`day_end_ms`/`trimmed`/`read_event_ids`/`read_event_dates`/`read_events` helper), `load_config` 끝에서 `panel.settlement_bar`·`settlement_return` 채움 |
| `cpp-engine/tests/test_config_loader.cpp` | 신규 테스트 3개(기본값·명시값+events·오류 14케이스) |
| `cpp-engine/tests/test_parquet_loader.cpp` | 신규 테스트 5개 + 합성 이벤트 fixture helper(`EndDate`/`end_dates`/`text_ids`/`int_ids`/`four_bar_panel`) |
| `cpp-engine/tools/data_load_probe.cpp` | 출력에 `events` 블록(건수·정산 예정·첫 3건) 추가 |
| `cpp-engine/examples/datasets/equities_monthly_afl.yaml` | `events:` 블록, `missing_close_policy: carry`, `default_delisting_return: 0.0`, `stale_quote_settle_bars: 60` |
| `cpp-engine/examples/datasets/README.md` | "결측 종가·상폐 정산 규칙" 절 신설, 기존 "종가 없으면 실행 실패" 문장을 정책 의존으로 교정 |

헤더(`config_loader.h`/`data_panel.h`)는 손대지 않음. 공용 인터페이스 변경 요청 없음.

## 2. 규칙 결정(구현 확정 사항)

| 항목 | 결정 | 근거/비고 |
| --- | --- | --- |
| 정산 bar | `upper_bound(timestamps, day_end_ms(end))`, ≥T면 -1 | 종료일 당일까지 보유, 다음 bar 정산 |
| `day_end_ms` | 종료일을 **일 단위로 floor 후** +86,399,999 | 브리프의 `end+86399999`와 자정 데이터에서 완전 동일. 시각이 붙은 종료일(00:00 아님)에서만 차이 나며 그 경우 브리프 식은 하루 더 보유하게 됨. 실데이터 20,408건 전부 자정이라 실질 차이 0 |
| 첫 bar 이전 종료 | `settlement_bar = 0` | upper_bound가 자연히 0 반환 |
| id 타입 | int64 / string / large_string만. int64는 `std::to_string`, 문자열은 앞뒤 공백 trim | 그 외 타입·null·빈 문자열은 오류 |
| end date 타입 | timestamp(s/ms/us/ns), date32, date64, string(`YYYY-MM-DD`), null 타입 열 | 그 외는 오류. ms 미만 정밀도는 기존 `time_value`가 거부 |
| null end date | 이벤트 없음(-1) | 파일에 없는 심볼도 동일 |
| 중복 id | 패널 안팎 무관하게 **전체 파일 기준** 오류 | 테스트로 두 경우 모두 검증 |
| `return_col` | 유한값만 기록, null/NaN은 NaN(기본 R 사용) | **end date가 null이어도 값이 있으면 기록함** — stale 경로 정산에서도 명시 R이 우선하도록 한 의도적 선택 |
| 컬럼 이름 | `id_col`/`end_date_col`/`return_col` 서로 달라야 함 | 같은 이름이면 config 단계에서 오류 |
| 파일 부재 | `path` 비면 무이벤트, **설정된 파일이 없으면 오류**(리드 결정: 오탈자 경로가 조용히 정산을 끄는 것을 막음) | — |
| 벡터 크기 | 이벤트 로드 시 길이 N(심볼 정렬과 동일), 미로드 시 길이 0 | 유니버스 subset에서도 선택된 심볼 순서에 정렬됨(테스트) |
| 읽기 범위 | 지정한 2~3개 컬럼만 projection 후 table/reader 즉시 해제 | 로드 시간·RSS 변화 측정 불가 수준(아래) |

파싱 검증: `missing_close_policy`는 carry/error 외 오류, `default_delisting_return`은 비유한(`.nan`/`.inf`)·비수치 오류, `stale_quote_settle_bars`는 음수·소수 오류, `events`는 매핑 아님·path 없음/빈 문자열·빈 컬럼명 오류. 각 케이스의 실제 예외 메시지를 `data_load_probe`로 개별 확인해 "다른 이유로 던져서 통과"가 아님을 확인함.

## 3. 테스트

| 파일 | 테스트 수(기존→현재) | 신규 케이스 |
| --- | --- | --- |
| `test_config_loader.cpp` | 5 → 8 | 기본값(carry/0.0/0/events 기본 컬럼명), 명시 옵션+events 전체 키+path만 준 경우, 오류 14케이스 |
| `test_parquet_loader.cpp` | 11 → 16 | ①events 미설정·파일 부재 → 벡터 빈 상태, ②end date 타입 7종(ts s/ms/us/ns, date32, date64, string) × {다음 bar 정산, null, 첫 bar 이전(0), 패널 밖(-1), 마지막 bar 종료(-1), 미등록 id}, ③int64 id + 파일 순서≠패널 순서 + 유니버스 subset 정렬, ④`return_col` 명시 R/null, ⑤오류 9케이스(중복 id 2종, 컬럼 부재 3종, double id, 숫자 end date, null id, 잘못된 문자열 날짜) |

fixture는 기존 `DataFiles::write`(Arrow C++ writer)로 **테스트 실행 시 생성**함 — pyarrow 의존이나 바이너리 fixture 커밋 없음.

```
ctest --test-dir /tmp/longshort_wt_data_build -R '^test_(config_loader|parquet_loader)$' --output-on-failure
→ 100% tests passed, 0 failed (2/2).  test_data_loader도 추가로 통과 확인.
```
빌드는 브리프 §6 커맨드에 `-DUUID_INCLUDE_DIRS=/home/jgo/61/jgo/miniconda3/include/uuid -DUUID_LIBRARIES=/usr/lib/x86_64-linux-gnu/libuuid.so.1`을 더해야 configure가 통과함(drogon FindUUID 실패. 메인 캐시에도 같은 값이 들어 있음).

## 4. 실데이터 확인 (`data_load_probe` + 예제 config)

패널: T=4,449(2009-01-02 ~ 2026-09-10), N=1,056, trade_start_index=252.

| 항목 | 값 |
| --- | --- |
| `eventsLoaded`(= `settlement_bar.size()`) | 1,056 |
| `settlementSymbols`(settlement_bar ≥ 0) | **235** |
| `retiredBeforeStart`(== 0) | 0 |
| `noSettlementInPanel`(== -1) | 821 |
| `explicitReturns` | 0 (`return_col` 미설정) |
| 첫 정산 3건 | `2660009` bar 3 / 2009-01-07, `2614568` bar 58 / 2009-03-27, `2643621` bar 62 / 2009-04-02 |

독립 확인: pandas/pyarrow로 패널 구성부터 settlement_bar까지 따로 계산했고 위 6개 수치와 첫 3건이 **전부 일치**함.

브리프 §3 실측치와의 정합성:
- "종료일 있음 235" = `settlementSymbols` 235로 정확히 일치. 이벤트 파일 28,210행 중 유니버스 1,056종목은 **전부** 행이 존재하고(매칭률 100%), 그중 235개만 종료일이 non-null임. 나머지 821개는 종료일 null이므로 -1.
- "끝결측 133 중 종료일 없음 8": 같은 정의(마지막 유효 호가 이후가 전부 결측 && 그 마지막 호가일에 유니버스 소속)로 재계산하면 **138**, 그중 종료일 없음 **8**로 8은 정확히 일치하고 총계만 5 차이남(유니버스 소속 시점 판정 방식 차이로 추정). 종료일 없이 끝까지 결측인 8종목이 `stale_quote_settle_bars` 경로의 실제 대상임.
- "종료일 이후 정상 호가 1건": `2603877`(Wolfspeed)이 정산 bar 4428(2026-08-12)에 **유효 종가가 있는 채로** 정산 대상이 됨. 엔진은 계약 §2.1대로 이 bar에서 정상 quote move 대신 `h_prev·R`만 적용해야 함(중복 금지). 나머지 234건은 정산 bar 이전에 이미 호가가 끊김.
- 이벤트 로딩 비용: 로드 시간 3,17x ms(이벤트 없음) vs 3,16~3,18x ms(이벤트 있음), peak RSS 283~287MB로 양쪽 동일 — 측정 노이즈 이내.

## 5. 엔진 담당과 맞출 것 / 미해결

| # | 내용 |
| --- | --- |
| 1 | `/api/config` 키 산출: probe는 `eventsLoaded = panel.settlement_bar.size()`(로드 시 N, 미로드 0), `settlementSymbols = count(settlement_bar >= 0)`로 셌음. 같은 정의를 쓰면 실데이터에서 1,056 / 235가 나옴 |
| 2 | 패널 벡터만으로는 "이벤트 파일에 행은 있으나 종료일이 null"과 "파일에 아예 없음"을 구분할 수 없음(둘 다 -1). 매칭 건수를 별도로 노출하려면 헤더에 필드 추가가 필요함 — 현재는 요청하지 않음 |
| 3 | `settlement_return`은 종료일이 null인 행에서도 유한한 명시 R이 있으면 채움. stale 정산에서도 명시 R이 우선한다는 의미이며, 기본 R만 쓰길 원하면 알려주면 로더에서 조건을 좁힘 |
| 4 | 존재하지 않는 이벤트 파일 경로를 오류 없이 무시함(브리프 문구 따름). fail-fast가 맞다면 한 줄 변경으로 바꿀 수 있음 |
| 5 | `day_end_ms`가 종료일을 일 단위로 floor함(2절). 오라클이 `end+86399999`을 그대로 쓰더라도 실데이터·픽스처 전부 자정이라 결과 동일 |
| 6 | 서버·HTTP·시뮬레이터 경로는 미검증(담당 밖). 이 트리에서는 `engine`, `test_config_loader`, `test_parquet_loader`, `test_data_loader`, `data_load_probe`만 빌드·실행함 |

## 6. 산출물

- 패치 `/tmp/longshort_wt_data.patch` (603줄, 담당 7개 파일만). 메인 트리에서 `git apply --check` 통과 확인.
  - 헤더 2개(`config_loader.h`/`data_panel.h`)는 메인 트리와 **바이트 동일**이라 패치에서 제외함. 포함하면 `git apply`가 실패함.
  - 참고용 전체 diff는 `/tmp/longshort_wt_data_full.patch`(650줄, 헤더 포함 — 메인 트리에는 적용 불가).
- 빌드 디렉터리 `/tmp/longshort_wt_data_build`.
