# 기존 오류 수정과 C++ 데이터 연결 — 2026-09-11

프론트 실행·시그널 연결·XML 저장을 복구하고, 합산 재현성과 시작일/유니버스 손익 규칙을 고정했다. C++은 기존 long Parquet과 새 wide 다중 파일을 읽고, AFL은 이름을 모두 등록한 뒤 사용한 값만 읽는다. DB 수집·갱신 작업은 변경하지 않았다.

## 계산 규칙과 리뷰 상태

| 항목 | 현재 동작 |
|---|---|
| E1 재실행 상태 | 기존 수정 유지. top-K 시그널은 저장한 확장 XML에서 새 파이프라인으로 계산한다. 완료한 멀티알파 모듈 상태는 해제한다. |
| E2 기본 이중 delay | 새 편집기 기본 XML은 `delay="0"`. C++ `mode=1`과 기존 사용자가 명시한 XML delay는 유지한다. |
| E3 avgBet/minBet | 평균·최소 보유 종목 수라는 사용자 정의 유지. 기존 내부 이름 정리와 wire 키 호환 유지. |
| E4 combined 결정성 | 각 날짜의 기여를 alpha index 순서로 누적해 스레드 수·완료 순서에 영향을 받지 않는다. 오류 시 대기 중인 worker도 중단한다. raw 합산 overflow는 명시적으로 실패한다. |
| E5 combined 의미 | 기본 `normalized`: 각 알파의 해당 날짜 유효 신호를 gross=1로 정규화하여 합산하고, 최종 포트폴리오를 다시 정규화한다. `raw` 옵션은 신호 원래 크기를 사용한다. |
| E6 결측 종가 | 보유/체결에 필요한 종가가 무효이면 실패한다. 유니버스 이탈 때문에 가격을 지우지 않는다. 경제적인 상폐 손실을 자동 추정하지 않는다. |
| 횡단면 유니버스 | `rank`·`zscore` 표본과 최종 목표 포지션은 신호 날짜의 마스크를 사용한다. 제외 종목이 정상 종목의 순위·평균·표준편차를 바꾸지 않는다. |
| F1·F2 프론트 기반 | 누락된 실제 lib 구현을 추적하고 삭제된 Python-engine fixture 의존성을 교체했다. 별도 clean clone의 npm ci·production build까지 통과했다. |
| F3 MDD | 기존 additive drawdown 수정 유지. |
| F4 시그널 패널 | single/multi/GA에 동일 계약을 사용한다. top-K와 combined를 먼저 받고, 다른 알파는 선택 시 XML로 요청한다. 플롯은 응답 cell 수에 상한을 둔다. |
| F5 simStart | 이전 bar는 모듈·D1 목표 신호 준비만 수행한다. 시작일 이전 포지션·회전·비용·손익은 0이며 시작일의 진입 비용을 기록한다. 요약·연도별·시계열도 실제 시작 구간부터 계산한다. |
| F6 부호·색 | 양수=long, 음수=short. 횡단면 순위를 색으로 사용하여 부호가 뒤집히던 경로를 제거한다. |
| 저장 | 선택한 알파의 확장 XML을 저장한다. 상세 화면은 현재 상세 알파를 기준으로 한다. |
| 실행 설정 | 화면의 D0/D1·Jobs를 실제 API에 전달한다. 기본 D1을 유지하며 D0에서는 첫 bar 진입 비용도 지표에 포함한다. |
| 연환산 | YAML `backtest.bars_per_year`로 일원화한다. 기존 설정의 기본값은 365, 주식 예제는 252다. 비용 기본값 5bp는 유지한다. |

추가로 XML 숫자 `1.2.3`의 부분 파싱 및 float32로 표현할 수 없는 상수는 오류로 처리한다. `returns`는 기존 종가 파생식이며, 데이터 이름이 이 의미를 덮어쓰지 않도록 구분한다.

## 기존 팀 sim과 맞춘 유니버스 이탈

사용자 지시에 따라 기존 구현을 먼저 확인했다.

- `/home/base/git/sim/utils/sim_util/handlers.py`의 `StatsHandler.run`은 입력 포지션의 `diff(1)`을 매매량, `shift(1)`을 보유손익의 포지션으로 사용한다. 독립적인 유니버스 강제청산 기능은 없다.
- 실제 비교에 사용한 `pvp_t500_none_0/AlphaPos.parquet`의 생성 코드 `/home/jgo/61/jgo/research/glasso/src/sim_modules/pv_champ_peer.py`는 `sig.where(valid)` 뒤 `sig.shift(1)` 및 정규화를 수행한다.
- 이에 맞춰 C++은 신호일 마스크를 목표 포지션에 적용하고 `mode=1` 지연에 함께 넣는다. 실행일의 새 마스크로 전날 목표를 다시 지우지 않는다. 이탈일 t에 만든 0 목표는 t+1 종가에 체결된다.
- 팀의 다른 모듈은 마스크 위치가 다를 수 있다. 예를 들어 `FactorPosition.py`에는 지연된 가격 뒤 현재 날짜 마스크를 적용하는 경로도 있다. 위 기준을 팀 전체 알파의 유일한 규칙이라고 주장하지 않는다.

가격 100→90→81, 50% 롱의 사례에서 이탈 신호일 손익 -5%, 다음 날 청산일까지 보유손익 -5%를 반영한다. 테스트 비용 10bp에서는 재배분 회전율 1에 -0.1% 비용이 더해진다. 전 종목 청산으로 최종 gross가 0이 되어도 그날 손실·비용은 남는다.

현재 플랫폼의 종가 체결 규칙을 유지하여 oracle의 trading return을 0으로 둔다. 팀 `StatsHandler`의 `Return`은 당일 gross로 나누고 gross=0이면 0으로 채우므로, 전 종목 청산 검증에는 실제 금액 기준 `PnL`을 사용한다. 팀의 결측 수익률 0 채우기를 C++에 복사하지 않았다.

## 데이터 사용

실행 가능한 예제: [equities_monthly_afl.yaml](../cpp-engine/examples/datasets/equities_monthly_afl.yaml), [카탈로그·시간 규칙 안내](../cpp-engine/examples/datasets/README.md).

- 날짜·종목 ID를 공통 가격 축으로 정렬한다. Arrow timestamp 단위를 epoch milliseconds로 정확히 변환하며 중복 좌표와 지원하지 않는 타입을 거절한다.
- 필요한 종목 컬럼과 관련 row group을 읽는다. 가격의 유니버스 밖 이력은 보존한다.
- AFL 카탈로그는 600개 팩터·1,192개 이름이다. 동일 파일의 별칭은 같은 lazy column을 공유하며 동시 접근 시 한 번만 읽는다. 사용자 C++ 모듈의 `panel->row()` 접근도 같은 경로다.
- `number_of_revision`은 원본 팩터 선택이 필요한 이름이므로 임의로 960/963에 연결하지 않는다. 명시적 별칭 생성 방법을 예제에 제공한다.
- `AflData_M`은 월별 관측 캐시다. `alignment: asof`는 관측 시각 이전의 값을 과거로 채우지 않으며, 마지막 관측을 이후 가격 날짜에 유지한다. 매일 갱신된 AFL 원천 데이터로 간주하지 않는다.
- `loadedFields`/`loadedDataBytes`는 이미 읽은 수치 값 배열을 나타낸다. 전체 프로세스 RSS와는 다르며, 프로세스가 다른 사용자는 별도 메모리를 사용한다.

## 검증

- [전체 엔진 검사](logic_completion_2026-09-11/engine_test_summary.json): CMake/CTest **25/25 target, C++ 184개 + Python 36개 통과**, 실패·skip 0. HTTP 13개, 실제 Arrow/config 11개, Boost 체크포인트 6개와 기존 golden/SIMD/2,000알파 검사를 포함한다.
- 프론트: **Vitest 65개**, lint/typecheck·production build, **실제 Chrome 5개 통과**. live C++의 D0/D1·Jobs·GA 완료·XML 저장/로드, 실제 canvas 색과 선택/캐시/화면 복원을 검사했다. [상세 기록](logic_completion_frontend_2026-09-11.md)
- [독립 clean clone](logic_completion_2026-09-11/clean_clone_build.json): 커밋된 파일만 clone하고 npm ci부터 production build까지 통과했다.
- [팀 sim HTTP parity](logic_completion_2026-09-11/team_sim_parity.json): D0/D1 유니버스 이탈·시작일·전체 청산의 5사례에서 일별 PnL 차이 **0**, 보유 종목 수·회전 일치. 결측 청산 종가 오류 1사례 확인.
- [실제 wide 데이터](logic_completion_2026-09-11/wide_data_parity.json): 실제 Top500 중 6종목·65bar의 close/volume/AFL100·마스크·좌표가 독립 참조와 float32 기준 정확히 일치했다. 600개 AFL 등록, eager 2개→사용 후 3개 필드 적재, 값 배열 4,680byte. 전체 프로세스 peak RSS는 **492,539,904byte(약 493MB)**였으며, 큰 wide footer/schema 때문에 값 배열 크기만으로 전체 RAM을 추정할 수 없다.
- [실제 AFL XML→HTTP](logic_completion_2026-09-11/real_afl_http.json): 같은 파일의 `afl_100`과 `HL52W`가 실제 주식의 raw 신호·모든 지표에서 정확히 일치하고 적재 필드는 **2→3→3**으로 유지됐다.
- [GA worker 수명](logic_completion_2026-09-11/ga_lifecycle.json): 실제 client 연결 종료 시 worker 수가 기준치로 돌아왔고, 활성 GA 상태의 서버 종료도 정상 완료됐다. 이 테스트의 취소 지연은 약 50ms이며 모든 모듈에 같은 지연을 보장하는 수치는 아니다.

재실행:

```bash
/home/jgo/usr/venv-3.11/bin/python cpp-engine/tools/check_team_sim_parity.py \
  --server /tmp/longshort_module_server/build/longshort-server \
  --team-sim /home/base/git/sim \
  --output /tmp/longshort_team_parity_20260911.json
```

## 분담과 독립 리뷰

root가 유니버스·시작일 손익, XML 등록 필드와 AFL 카탈로그, 팀 sim oracle를 맡았다. engine_audit가 combined/API/metrics 및 횡단면 마스크, build_validation이 데이터 로더·config·CMake/Arrow 검증, frontend_refactor가 실제 프론트 복구와 브라우저 검증을 맡았다.

교차 리뷰에서 XML 테스트의 전체 파이프라인 실행 누락, 상세 화면 저장의 이전 선택 우선 문제, API loadedFields 타입 불일치, raw 합산 overflow 후 누적값 초기화, 횡단면 유니버스 표본 오염을 찾아 보완했다.

## 최초 필드 읽기 메모리 보완

실제 두 AFL 필드를 threads=2로 처음 읽을 때 피크 RSS가 커지는 것을 확인했다. `read_field` 전체 수명에 mutex를 두어 파일 디코딩을 직렬화했다. 이미 읽은 값 접근·알파 계산은 계속 병렬이다. [전후 측정](logic_completion_2026-09-11/cold_field_memory.json)에서 원가격·기간·비용 및 전체 HTTP JSON을 유지한 채 peak RSS는 **630,636,544→579,383,296byte(약 8.1% 감소)**, 첫 요청은 **320.5→587.1ms**, 재사용 요청은 약 1ms였다. 단일 전후 관측이며 allocator 잔류 메모리 때문에 전체 RSS의 상한을 보장하지 않는다. 이 후속 5줄 변경 후 기존 config/Parquet 검사 **11개**와 실제 두 필드 HTTP 대조를 재검증했다.

## 담당별 기록과 커밋

- [엔진/API·교차 리뷰](logic_completion_engine_2026-09-11.md)
- [데이터·빌드 하네스](logic_completion_data_2026-09-11.md)
- [프론트·실제 브라우저](logic_completion_frontend_2026-09-11.md)
- `a63f45f`: 손익 시점·유니버스·combined/API와 wide/lazy AFL 데이터 연결.
- `2cd5b67`: 프론트 빌드 복구·시그널·D0/D1/Jobs·실제 XML 저장·브라우저 검증.
- `dcc902f`: 서로 다른 필드의 최초 파일 읽기를 직렬화해 큰 메타데이터 버퍼가 겹치는 것을 줄인다.

작성자는 `joomong <jmgo97@gmail.com>`이며 원격 push는 수행하지 않는다. 데이터 원본과 기존 팀 sim은 읽기만 했다.

기존 README/작업 지침에 남아 있던 삭제된 FastAPI/Python-engine 경로와 외부 qv-project 참조도 현재 C++/Next.js 실행·검증 안내로 정리했다. 사용자 서버를 재시작하지 않았고, 팀이 기동한 테스트 서버는 검증 후 종료했다.
