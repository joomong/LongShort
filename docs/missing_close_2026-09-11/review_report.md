# 독립 리뷰 — 결측 종가·상폐 회계 구현 (2026-09-11)

리뷰 대상: HEAD `2e77af1` 대비 미커밋 변경 전부(21 modified + untracked `tools/{missing_close_oracle,check_missing_data,run_top500_missing_close}.py`). 코드·빌드 무변경. 서버는 18140~18159에서만 띄웠고 전부 종료 확인함(`ss -ltn` 확인, 잔존 프로세스 0).

리뷰어가 직접 실행한 것:
- `ctest -j4` → 25/25 통과(단 `test_http_contract`는 **전부 SKIP**, 아래 F-05)
- `test_simulator --gtest_filter='*MissingData*'` → 15/15 통과
- `check_missing_data.py --self-check` → 76 checks / 0 failure
- `data_load_probe` + 예제 config(실데이터) → `settlementSymbols=235 / retiredBeforeStart=0 / noSettlementInPanel=821`, 첫 정산 `2660009` bar 3 재확인
- **자체 제작 적대적 시나리오 10종 × (carry/error) × R{0,−1} = 40 케이스**를 합성 fixture + 실서버 + 독립 원장으로 대조 → 38 PASS / 2 FAIL(F-01)
- **구/신 엔진 비트 동일 재검증**: 새로 만든 clean 패널(T=1500, N=120, churn 마스크) × 알파 5종 × mode 0/1 = 10조합을 `/tmp/lsparity/{old,new}_build/parity_probe`로 `%a` 덤프 후 `cmp` → **전부 byte-identical**
- 요청 옵션 13종 fuzz(대문자·숫자·null·object·bool·2^32 초과·소수·음수)
- GA `/api/ga/stream` complete 이벤트를 curl로 수신해 `missingData` 13키·`timeseries.frozenGross` 확인

---

## 0. 결론 — **조건부 머지 가능**

핵심 회계 경로(정산→intent→보유손익→체결→turnover/fee→통계→warmup)는 계약대로 구현됐고, 무결측 입력의 **비트 동일은 독립적으로 재현됨**. 손계산 10사례가 코드에 맞춰 고쳐진 흔적도 없음. P0은 없음.

다만 **P1 1건(F-01, stale 정산이 명시 R을 무시)** 이 엔진·로더·oracle·브리프 4자 중 엔진만 다른 규칙을 쓰는 상태임. 현재 예제 config가 `return_col`을 주석 처리해 둬 잠복 상태이나, 그 한 줄을 여는 순간 stale 정산분의 손익이 조용히 틀림. **F-01 해소(또는 계약 문구를 엔진에 맞추고 로더·oracle·문서를 같이 좁힘) 후 머지 권고.**

P2 중 **F-02(README와 구현이 정반대)** 는 문서 한 줄 수정이라 같이 처리하는 게 맞음.

---

## 1. 발견

| ID | 심각도 | 파일:줄 | 증상 | 재현/근거 | 수정안 | 확신도 |
| --- | --- | --- | --- | --- | --- | --- |
| **F-01** | **P1** | `cpp-engine/src/engine/simulator.cpp:144` | stale 정산(`stale_now`) 경로에서 `settlement_return`에 유한한 명시 R이 있어도 **무시하고 `default_delisting_return`을 씀**. 게다가 `default_delisting_count`/`default_delisting_gross`까지 증가시켜 "기본값을 썼다"고 잘못 보고함 | 직접 재현: `securityEndDate=NaT` + `settlementReturn=-0.4` 행 + `staleQuoteSettleBars=2`. 서버 `delistingPnl=0.0`(R기본0) / `-1.0`(R기본−1), 독립 원장 `-0.4`. `defaultDelistings` 서버 1 vs 원장 0. 스크립트 `scratchpad/rev/e1.py`. 브리프 §2.1은 "R = settlement_return finite ? 그 값 : default"로 event/stale 구분이 없고, data 담당은 §5-3에서 "stale 정산에서도 명시 R이 우선하도록 한 의도적 선택"으로 null end date 행에도 R을 채움(`parquet_reader.cpp:411`), oracle도 `missing_close_oracle.py:244`에서 두 경로 모두 explicit 우선 | `if (event_now && has_returns && ...)` → `if (has_returns && ...)`. 또는 계약을 "explicit R은 event 전용"으로 좁히고 로더(411줄 조건), oracle(244줄), README를 함께 고칠 것. 어느 쪽이든 회귀 테스트 1건 추가 필수 | 높음(실측) |
| **F-02** | P2 | `cpp-engine/examples/datasets/README.md:61` | "`path`가 비어 있거나 **파일이 없으면 이벤트 없이 로드한다**"고 적었으나 구현은 `parquet_reader.cpp:364`에서 `Events parquet not found`로 **던짐**. 테스트(`test_parquet_loader.cpp` `EventsWithoutConfigurationLeaveTheSettlementVectorsEmpty`)가 throw를 단언하므로 문서만 stale. data 담당 보고서 §5-4도 같은 오류를 반복함 | `git diff` 대조 + 테스트 단언 | README 문장을 "`path`가 비어 있으면 이벤트 없이 로드하고, 설정된 파일이 없으면 로드 실패다"로 교정 | 높음 |
| **F-03** | P2 | `cpp-engine/src/engine/simulator.cpp:334` | `default_delisting_return`에 **유한성만 검사**하고 범위 검사가 없음. `-5.0`이나 `+3.0`이 HTTP로도 그대로 통과해 `h_prev·R`에 곱해짐 | 실서버 fuzz: `defaultDelistingReturn=-5` / `+3` 모두 200. `scratchpad/rev/opts.py` | 총수익률 하한 `R >= -1.0` 검사 추가(생성자 + `parse_run_options`). 상한은 정책 판단 | 높음 |
| **F-04** | P2 | `cpp-engine/src/main.cpp:375-380` | `parse_missing_close_policy` 실패 시 carry 폴백 + 경고 출력 분기가 **도달 불가 코드**임. `config_loader.cpp:188`이 이미 carry/error 외 값을 로드 단계에서 던지기 때문. engine 보고서의 근거("설정 오타로 모든 요청이 400 되는 걸 방지")는 실제 동작과 불일치 — 오타면 **기동 자체가 실패**함 | `config_loader.cpp:186-189` vs `main.cpp:375` 대조 | 폴백 분기를 제거하거나, 반대로 `config_loader`의 검증을 완화해 의도한 관대함을 살릴 것(둘 중 하나로 통일) | 높음 |
| **F-05** | P2 | `cpp-engine/tests/test_http_contract.cpp:280` | ctest 기본 실행에서 **HTTP 계약 17건이 전부 SKIP**됨(`LONGSHORT_TEST_CONFIG` 미설정 시 `config.yaml` 탐색 실패). "ctest 25/25 통과"는 HTTP 계약에 대해 아무 보증도 못 함. 또한 포트가 **18080 하드코딩**이라 잔존 서버가 그 포트를 잡고 있으면 `wait_until_ready()`가 옛 서버의 `/api/health` 200을 받아 **stale 바이너리 상대로 false-pass** 가능(oracle 보고서 §6-4의 orphan 서버 관찰과 같은 계열) | `./test_http_contract` 직접 실행 → 17 SKIPPED. `ServerProcess::wait_until_ready()`가 `waitpid(WNOHANG)` 후 곧바로 `http_get`을 시도하는 구조 | ctest에 fixture config를 등록해 SKIP을 없애고, 포트를 동적 확보 + bind 성공을 확인(예: 자식이 실제로 그 포트를 listen 중인지 검사)하도록 변경 | 중간 |
| **F-06** | P2 | `cpp-engine/tools/check_missing_data.py:510, 518, 527` | 하네스의 대조가 **조건부**임. `turnover/maxWeight/drawdown/cumPnlGross/nonzeroCount`는 길이가 다르면 비교를 건너뛰고, `frozenGross/settledGross`와 `missingData` 13키도 **응답에 키가 없으면 조용히 비교 대상에서 빠짐**(590줄은 `missingData` 객체 존재만 검사). 키 하나가 누락돼도 PASS가 나옴 | 코드 구조. 실제로 F-01은 값이 달라서 잡혔으나, 키 누락형 회귀는 못 잡음 | 기대 키 집합을 필수로 두고 누락 시 FAIL 처리 | 높음 |
| **F-07** | P2 | `cpp-engine/tools/missing_close_oracle.py:315-317` | 원장이 `F == 0`일 때도 스케일을 계산함(`if tradable_gross > room`). 엔진은 `simulator.cpp:239`에서 `frozen_gross > 0.0`일 때만 진입. 무결측 입력에서 Σ\|target\|이 부동소수로 1을 미세 초과하면 **원장만 축소**가 걸려 엔진과 ~1e-16 갈림(허용오차 1e-9라 FAIL이 안 남) | 코드 대조. 실측 clean 패널에서는 발현 안 함(최대 오차 1.7e-18) | 원장에도 `if frozen_budget > 0.0` 게이트를 넣어 계약("F=0이면 기존 경로와 동일")과 맞출 것 | 높음 |
| **F-08** | P2 | `cpp-engine/src/main.cpp:289-306` | `missing_data_payload`가 `total.frozen_gross`를 누산하지만 payload에 **방출하지 않음**(죽은 계산). 반대로 `frozenGross`는 per-bar 시계열과 `terminalFrozenGross`로만 노출돼, "동결 노출의 기간 총량"을 API 소비자가 직접 합산해야 함 | 코드 | 누산을 지우거나 `frozenGrossBars` 같은 이름으로 노출(의미가 asset-bar 합이므로 이름 주의) | 높음 |
| **F-09** | P2 | `cpp-engine/src/engine/simulator.h:16`, `main.cpp:298` | `delistings`(=`delisting_count`)가 `h_prev != 0`인 정산만 셈. 실데이터 전기간에서 실제 정산 229건 중 135~139만 보고됨. **총 retire 건수를 알 수 있는 카운터가 없음** | oracle 보고서 §4 표 + 헤더 주석 | 의도된 정의라면 유지하되 `retirements`(전체) 카운터를 추가하거나, 최소한 `/api/config`·문서에 "보유분만 집계"를 명시 | 중간 |
| **F-10** | P2 | `cpp-engine/src/data/parquet_reader.cpp:280` | `day_end_ms`가 종료일을 **일 단위 floor 후** +86,399,999를 더함. 브리프 §3의 `upper_bound(end_date_ms + 86399999)`와 **비자정 종료일에서 결과가 다름**(구현이 1 bar 빠름). 구현 쪽이 의미상 옳으나 브리프 문구와 어긋나고 테스트가 없음 | 직접 재현: 종료일 `2026-01-06 12:00` → 브리프식 bar 3, 구현 bar 2. `scratchpad/rev/daybound.py` + `data_load_probe` | 브리프/문서 문구를 구현에 맞게 교정하고 비자정 종료일 테스트 1건 추가. 실데이터는 전부 자정이라 현 결과 영향 0 | 높음 |
| **F-11** | P2 | `cpp-engine/tools/data_load_probe.cpp:43` vs `main.cpp:382-388,470` | 이름은 같고 뜻이 다른 `eventsLoaded`. probe = `settlement_bar.size()`(실데이터 **1056**), 서버 `/api/config` = `settlement_bar>=0 ‖ 유한 R` 심볼 수(실데이터 **235**). 두 보고서가 같은 키로 다른 수치를 인용 중 | probe 실행값 1056 vs 서버 235 | 한쪽 이름을 바꿀 것(probe는 `panelSymbols` 등) | 높음 |
| **F-12** | P2 | `cpp-engine/src/main.cpp:241` | `staleQuoteSettleBars`가 `UINT_MAX` 초과일 때 jsoncpp 내부 메시지 `"LargestInt out of UInt range"`가 그대로 400 본문에 나감(다른 옵션은 친절한 메시지) | fuzz: `staleQuoteSettleBars=5000000000` → 400 + 위 문구 | `asInt64()` 값을 직접 상한 비교한 뒤 자체 메시지로 던질 것 | 높음 |
| **F-13** | P2 | `cpp-engine/src/engine/simulator.h:86` vs `:60` | 기본값 불일치 — `normalize_signal_row`의 policy 기본은 `Error`, `SimulatorConfig::missing_close_policy`의 기본은 `Carry`, `OrderedSignalAccumulator`의 기본도 `Error`. 지금은 `main.cpp:518`이 명시 전달해서 맞지만, 누락하면 per-alpha 정규화(quote 없는 종목 제외)와 combined 시뮬(포함)이 **조용히 어긋남** | 코드. 회귀 테스트 없음 | 세 기본값을 한 값으로 통일하거나 `OrderedSignalAccumulator`의 기본 인자를 제거해 호출부가 반드시 지정하게 할 것 | 중간 |

### 비-이슈로 확인했으나 기록이 필요한 동작 변화
- `SimulatorConfig::missing_close_policy` 기본이 `Carry`로 바뀌면서, 정책을 지정하지 않는 모든 직접 소비자가 **fail-fast → 동결**로 조용히 바뀜. 저장소 안에서는 `tools/bench_2000_breakdown.cpp` 1곳과 기존 테스트 몇 개가 해당(테스트는 `Error`로 고정 완료). 외부 소비자가 있으면 고지 필요. 계약상 의도된 변화라 버그로 분류하지 않음.

---

## 2. 계약 대비 항목별 판정

| # | 항목 | 판정 | 근거 |
| --- | --- | --- | --- |
| 1 | 계약 §2 순서·규칙 | **확인됨(1건 예외: F-01)** | `advance()` `simulator.cpp:71-99`가 정산→intent→(Error 검사)→보유손익→체결→통계 순. 정산 bar에 유효 quote가 있어도 `settle()`이 `retired_=1`로 만들고 `value_holdings`가 `retired` 조기 continue(`:209`)라 **중복 없음**(테스트 `SettlementReplacesTheQuoteMoveOnTheSameBar`, 내 e10 실측 일치). R=−1이면 `marks_ *= 0` → 0(`:166-167`). retired 영구·재진입 불가(`:125-126`, `:250-253`) — e10/gtest 확인. pending 취소는 `pending_delisting_entry_count`(`:252`). `settlement_bar==0`은 di=0에서 `carried_=0`이라 손익 없이 retire — 내 e9(mode 1)에서 원장과 일치. explicit R 우선은 **event 경로만** 동작(F-01). stale은 `carried_[ii] != 0.0 && isfinite(marks_)` 게이트(`:138`)로 보유분만 — e6(stale=1), e7(warmup) 실측 일치 |
| 2 | 비트 동일 회귀 | **확인됨** | 코드 대조: 손익 누산은 단일 누산기에 오름차순(`:218`), 이벤트 없으면 정산항 0개 → 옛 `holding_return` 항·순서 동일. `marks_`는 직전 bar에 저장한 `static_cast<double>(close)`라 옛 `(double)close/previous_close`와 같은 double 나눗셈. `turnover`는 전 ii 오름차순 `|weights_-carried_|`, 동결·retired는 정확히 0 기여. `scale=1.0` 곱은 IEEE 정확. warmup·mode 1 swap 순서도 등가. **실측**: 신규 clean 패널(T=1500 N=120, 유니버스 churn 포함) × 알파 5 × mode 0/1 = 10조합 `%a` 덤프 `cmp` 전부 byte-identical. 단, 회귀가 **레포 밖 임시 probe로만** 고정돼 있음(gtest `CompleteQuotesKeepTheExistingFixedBookResultBitForBit`는 신 엔진의 Error vs Carry 비교라 옛 엔진 대조가 아님) → §3 참조 |
| 3 | mode 0/1 인덱스·경계·마지막 bar | **확인됨** | `execution_intent()` `:112-114`, 말미 swap `:104-106`이 warmup에도 적용돼 `exec_w[t]=target[t−1]` 유지. gtest `FirstExecutionFollowsTheModeIntentAndNeedsAStartQuote`가 sim_start_index 경계의 mode 0/1을 고정. warmup 중 정산은 `retired` 전이만(`WarmupPricesAndSettlementsMakeNoHistoricalPnl`, 내 e7). `terminalFrozenGross = missing_data.back().frozen_gross`(`main.cpp:320`) — 마지막 bar 기준, 실서버에서 `timeseries.frozenGross` 말단과 일치 확인 |
| 4 | Error 정책 동일성 | **확인됨** | `require_tradable_quotes` `:191-202`의 조건 `(carried != 0 ‖ desired != 0) && !valid_close`가 옛 `holding_return` `:22`와 동치, 메시지·bar·asset 문자열 동일. 옛 `"invalid previous close"` 분기는 옛 코드에서도 도달 불가(보유는 항상 직전 bar 유효 quote를 전제)라 삭제가 안전. 실측: 원장이 거부하는 10+8 케이스에서 서버가 같은 문구·같은 bar/asset으로 400. 이벤트 정산은 `settle()`이 정책과 무관하게 먼저 돌아 quote 없이 동작(gtest `SettlementEventsStillApplyUnderTheErrorPolicy`) |
| 5 | 스레드 안전 | **확인됨** | `normalized_scratch`는 워커 람다 지역 변수(`main.cpp:528`), `OrderedSignalAccumulator::policy_`는 생성 후 읽기 전용, `normalize_signal_row`는 `data_`(const)만 읽고 출력 버퍼만 씀. `masked_signal_`/`retired_`/`marks_`/`missing_bars_`는 전부 `PortfolioSimulation` 멤버이고 워커마다 `execute_alpha`가 지역 `Simulator`를 새로 만듦 → 공유 없음. `g_missing_close_policy`·`g_events_loaded`는 서버 기동 전 1회 설정. `batches[i]`는 인덱스 분리 기록. 다만 F-13(기본 정책 불일치)이 구조적 취약점 |
| 6 | JSON 계약 | **확인됨** | `missingData` 13키(브리프 12 + `cancelledDelistingEntries`) 실서버에서 전수 확인, 의미도 원장과 76+38 케이스에서 최대 오차 5.6e-17. `timeseries.frozenGross/settledGross`는 항상 방출(`json_serializer.cpp:95-102`)되고 길이 = `timestamps` 길이 = `T − first_metric_bar`(mode 1·simStart 0이면 bar 0 제외) — `compute_timeseries` `:68-71`의 인덱싱이 나머지 계열과 같은 `i` 오프셋이라 정합. `/api/config` 5키 확인. **GA complete 이벤트에도 `missingData` 13키 + batches[0].missingData + timeseries 2계열이 붙는 것을 curl로 직접 확인**. 요청 옵션 검증은 대문자·숫자·null·object·bool·소수·음수 모두 400(F-12만 메시지 품질 이슈, F-03은 범위 미검증) |
| 7 | 데이터 로더 | **확인됨(F-10 문구 불일치)** | 실데이터 `data_load_probe` 재실행 → **235 / 0 / 821**, 첫 정산 3건까지 보고서와 일치. `upper_bound(day_end_ms)` 경계: 종료일 당일 bar는 보유, 다음 bar 정산(gtest가 timestamp s/ms/us/ns·date32·date64·string 7종 × {다음 bar, null, 첫 bar 이전=0, 패널 밖=−1, 마지막 bar 종료=−1, 미등록} 전수 고정). ms 미만 정밀도는 `time_value`가 거부. int64 id는 `std::to_string`, 문자열은 trim. 중복 id는 패널 안팎 모두 오류. 파일 부재 = 오류(문서만 반대, F-02). `settlement_return` null/NaN → NaN. 유니버스 subset 정렬 테스트 존재. `read_events`가 `latest_end` tail-trim **이후**에 호출돼 `panel.T` 확정 상태에서 bar를 계산하는 점 확인 |
| 8 | 테스트 적정성 | **의문** | 손계산 10사례가 계약 표의 숫자를 **문자 그대로** 단언함(−0.2/−0.201/turnover 2/0.25/0.000125/−0.12/1.2/0.0006/−1/+1/−0.25/−0.5). 코드에 맞춰 고친 흔적 없음. Carry/Error·mode 0/1 커버리지도 대체로 양방향. 그러나 §3의 누락 목록이 적지 않고, 특히 **stale × explicit R 조합이 비어 있어 F-01이 통과**했음. HTTP 계약은 ctest에서 실행조차 안 됨(F-05) |
| 9 | oracle 독립성 | **확인됨(F-07 1건)** | 구조가 C++와 다름(numpy 불리언 마스크 + `np.flatnonzero`, 단계별 주석이 계약 문장을 따라감). 판단이 필요했던 6개 쟁점을 `LedgerConfig` 플래그로 노출해 근거를 명시한 점이 오히려 독립성의 증거. 원장 identity(`PnL(R=−1)−PnL(R=0) = delistingPnl 차이`, `cumPnlGross−cumPnl = Σturnover·feeRate`)는 `run_top500_missing_close.py`에서 **실제로 실행**되고 잔차 1e-15/1e-14 수준. `--self-check` 76/76 리뷰어가 직접 재실행 확인. SKIP 로직은 `/api/config`의 `missingClosePolicy` 유무로만 갈려 오탐 소지 없음(현재 SKIP 0). **오탐 소지는 F-06**(키 누락형). F-01은 이 원장 덕분에 리뷰에서 잡혔으므로 oracle이 실제로 작동함을 반증함. 미세 차이 F-07은 계약 문구와의 불일치이지 엔진 문제 아님 |
| 10 | 성능·메모리 | **확인됨** | bar당 추가 패스는 `settle`(N) + `execute`의 `tradable_gross`(N, `frozen_gross>0`일 때만) ≈ +1~2N. `masked_signal_`은 `any_retired_`일 때만 `assign` — retired 0이면 **할당·복사 0건**(`:179-185`). 콜백용 4개 버퍼는 `records_rows_`일 때만 할당(테스트 전용). 항상 드는 비용은 `output.missing_data` T×struct(약 72~80B×T ≈ T=4449에서 ~0.35MB)와 `full_metrics`의 `vector<double>(T)` 2개(각 35KB). 실측(구/신 parity probe 동일 패널, engine 보고서 T=4000 N=500) 실행시간·RSS 차이 측정 노이즈 이내. 이벤트 로딩은 컬럼 2~3개 projection 후 즉시 해제 |

---

## 3. 누락 테스트 목록

우선순위 순.

1. **stale 정산 + 유한한 explicit R** — F-01이 바로 여기로 빠져나감. 엔진·oracle 양쪽에 필수.
2. **옛 엔진 대비 비트 동일을 레포 안에 고정하는 회귀** — 현재 증빙은 `/tmp/lsparity`의 임시 트리뿐. `CompleteQuotesKeepTheExistingFixedBookResultBitForBit`는 신 엔진 내부 Error vs Carry 비교라 HEAD 대비 보증이 아님. golden 스냅샷(체크인된 기대 `%a` 덤프)이나 `test_golden_crossval` 확장으로 고정할 것.
3. **`default_delisting_return` 범위** — `-1` 미만·양수 입력의 거부(F-03) 또는 최소한 현 허용 동작을 못 박는 테스트.
4. **두 종목 이상 동시 동결의 frozen budget** — gtest는 단일 동결만. (리뷰어 e3에서 통과 확인했으나 고정 안 됨)
5. **F ≥ 1(budget 0)** — 유효 quote 종목이 전부 강제 평탄화되는 경로. (e2에서 통과 확인, 미고정)
6. **결측 중 유니버스 이탈** — 옛 엔진에서는 throw였던 조합(`ExcludedHeldAssetWithMissingLiquidationCloseStillFails`)이 Carry에서 영구 동결로 바뀜. (e5 통과, 미고정)
7. **워밍업 구간 안에서 stale 조건이 성립하는 패널** — 보유가 0이라 구조적으로 발동 안 한다는 사실이 어디에도 고정돼 있지 않음. (e7 통과, 미고정)
8. **`stale_quote_settle_bars`의 큰 값·경계** — 1(첫 결측 bar 즉시 정산, e6에서 확인), 패널 길이 초과(발동 안 함), `UINT_MAX` 초과 요청의 400 메시지(F-12).
9. **정산 직후 같은 id 재상장** — retired가 영구라 wide 패널에서 같은 컬럼이 다시 호가를 내도 영원히 제외됨. 실데이터에서 Wolfspeed(`2603877`)가 정산 bar에 유효 종가가 있는 케이스라 인접 위험. 최소 문서화 + 테스트 1건.
10. **비자정 `securityEndDate`** — F-10. 브리프식과 결과가 갈리는 유일한 입력.
11. **로더의 `LARGE_STRING` id/end date, `NA`-타입 end date 컬럼, 앞뒤 공백 id** — 코드에 경로가 있으나 테스트 없음.
12. **HTTP 계약 테스트를 ctest에서 실제로 실행** — F-05. 현재 17건 전부 SKIP.
13. **실데이터 전기간의 bar별 원장 대조** — 계약 문서 §독립 oracle 5항("full Top500에서 trace를 stream으로 읽고 bar별 수익·체결·position identity 검사")이 미이행. 현 하네스는 집계 항등식 2개만 검사함. 독립 원장 검증이 T≤12, N≤4 합성 패널에만 걸려 있는 상태.

---

## 4. 논-이슈 (의심했으나 문제 없음)

| 의심 | 결론 |
| --- | --- |
| `settle()`이 `missing_bars_`를 당일 포함으로 미리 계산(`:133`)하고 `value_holdings`가 나중에 증가(`:225`) → 이중 계산? | 아님. `settle` 진입 시점의 `missing_bars_`는 t−1까지의 누적이고 `+1`은 지역 변수. 상태 갱신은 `value_holdings` 한 곳뿐. `stale` 임계는 "당일 포함 연속 결측 ≥ N"으로 계약대로임(e6: stale=1이면 첫 결측 bar에 정산) |
| 옛 `"Cannot value position: invalid previous close"` 예외가 사라짐 → Error 정책 회귀? | 아님. 옛 코드에서도 도달 불가 분기였음(보유가 0이 아니려면 직전 bar에 유효 quote가 있었어야 함). 실측에서도 18개 거부 케이스가 전부 `"invalid close at bar X, asset Y"`로 옛것과 동일 |
| `execute()`가 retired·동결 종목까지 turnover 루프에 포함 → 정산에 수수료가 붙나? | 아님. retired는 `weights_=0, carried_=0`, 동결은 `weights_=carried_`라 `trade`가 정확히 0.0. 비트 동일도 유지 |
| `missing_data_payload`가 `sim_start_index`부터 합산하는데 `full_metrics`는 mode 1에서 `max(sim_start_index,1)`부터 → `Σ timeseries.settledGross ≠ missingData.settledGross`? | 아님. 차이가 나는 유일한 bar(mode 1 & sim_start_index==0의 bar 0)는 `previous_weights_`가 전부 0이라 모든 카운터가 구조적으로 0. 실서버에서 항등식 성립 확인 |
| Carry가 quote 없는 종목을 intent에 포함시켜 실데이터에서 book이 크게 과소투자되나? | 실측상 무시 가능. 전기간 `missingEntries=1`, `blockedOrders=820` / 4197 bar × ~455종목. `terminalFrozenGross` 0.004~0.012 |
| `masked_signal_`을 retired 발생 이후 매 bar 복사 → 성능? | N=1056·T=4449에서 총 ~19MB memcpy. parity probe 실행시간 차이 측정 불가 수준 |
| oracle이 warmup에서 retired 종목의 mark도 갱신(`missing_close_oracle.py:276`, C++은 `:209`에서 skip) → 불일치? | 손익에 영향 없음(retired는 평가·체결 모두 제외). HTTP 대조 대상도 아님. 다만 `marks_after`를 원장끼리 비교할 일이 생기면 차이가 남 |
| `/api/ga/stream`이 빈 본문을 반환 | 클라이언트 아티팩트. Python `urlopen`에서는 빈 본문이나 `curl -N`으로는 정상적으로 `generation_end` + `complete`(missingData 13키 포함)가 옴. `req->connected()` 판정과 관련된 기존 동작이며 이번 변경과 무관 |
| `read_events`의 `GetColumnByName`이 동명 컬럼에서 nullptr 역참조? | 아님. 앞선 `schema->GetFieldIndex`가 중복 이름에 −1을 반환해 "column not found"로 먼저 던짐 |
| `day_end_ms`의 far-future 종료일 오버플로 | 가드 있음(`:284-285`), `INT64_MAX` 반환 → `upper_bound`가 T → −1 |
