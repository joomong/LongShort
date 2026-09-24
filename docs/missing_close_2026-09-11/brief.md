# 결측 종가·상폐 회계 구현 브리프 (2026-09-11, 리드: Claude)

저장소: /home/jgo/longshort_platform (HEAD 2e77af1 + 미커밋 헤더 변경). 스펙 원문: `docs/missing_data_validation_2026-09-11.md` (계약 확정 절·손계산 사례 표). 이 브리프는 그 계약을 구현 단위로 옮긴 것이며, 충돌 시 이 브리프 > 계약 문서.

## 0. 공통 규칙
- 언어: 코드 주석 영어, 보고 한국어(~임체, 표). 근거는 파일:줄.
- /home/base 는 read-only. 절대 쓰지 말 것. 서버 테스트는 빈 포트(예: 18100~18199 중 하나)에서 띄우고 반드시 종료.
- 커밋 금지(리드가 함). 담당 파일 밖 수정 금지. 기존 테스트가 깨지면 원인을 고치되 기대값을 바꾸는 "맞추기"는 금지(결측·이벤트가 없는 입력은 기존 결과와 비트 동일해야 함).
- Python: /home/jgo/usr/venv-3.11/bin/python (pyarrow/pandas/numpy). 시스템 python3엔 numpy 없음.
- 기본값 유지: mode=1(D1), fee 5bp, bars_per_year 365(주식 예제 252), combined normalized.

## 1. 공용 인터페이스 (리드가 이미 헤더에 반영함 — 변경 금지, 필요하면 리드에게 보고)
`cpp-engine/src/data/config_loader.h`
```cpp
struct EventsConfig { std::string path; std::string id_col="tradingItemId"; std::string end_date_col="securityEndDate"; std::string return_col; };
// BacktestConfig 추가 필드
std::string missing_close_policy = "carry";   // "carry" | "error"
double default_delisting_return = 0.0;
unsigned stale_quote_settle_bars = 0;         // 0 = never
EventsConfig events;
```
`cpp-engine/src/data/data_panel.h`
```cpp
std::vector<int32_t> settlement_bar;    // symbols와 정렬. 종료일 다음 첫 bar; -1 없음; 0 = 패널 시작 전 종료(처음부터 retired)
std::vector<float>   settlement_return; // explicit R, NaN = default 사용. 비어 있으면 이벤트 없음
```
`cpp-engine/src/engine/simulator.h` (다른 세션이 추가한 타입 유지): `MissingClosePolicy{Carry,Error}`, `MissingDataDiagnostics`, `AccountingRowView`, `SimulatorConfig.{missing_close_policy, default_delisting_return, stale_quote_settle_bars, accounting_row_callback}`, `SimulatorOutput.missing_data`, `normalize_signal_row(..., MissingClosePolicy)`.

## 2. 회계 계약 (fixed-book notional weights h, 기존 산식 유지)
용어: quote 유효 = close finite && > 0. mark[i] = 마지막 관측 유효 close(평가가격). h_prev = 직전 bar 종료 보유, desired = 이번 bar 실행 목표(mode 0: 당일 intent, mode 1: 전일 intent). retired[i] = 정산 완료(영구).
bar t 처리 순서:
1. **정산(settlement)**: !retired && (settlement_bar[i]==t  ||  (policy carry && 이벤트 없음(settlement_bar<0) && stale_quote_settle_bars>0 && 연속 결측 bar 수 ≥ stale_quote_settle_bars)) → R = settlement_return[i] finite ? 그 값 : default_delisting_return. pnl += h_prev[i]·R (정상 quote move 대신, 중복 없음). settled_gross += |h_prev[i]|. h[i]=0, retired=true, pending 취소. **수수료 0, turnover 미포함**. 진단: delisting_count++, delisting_pnl, default_delisting_count(기본 R 사용 시), stale 경로는 별도 카운트(default_delisting_count에 포함하되 diagnostics에 stale 여부를 알 수 있게 — 필드 추가 허용: `unsigned stale_settlement_count`).
   settlement_bar==0 이면 첫 bar부터 retired (보유 불가, intent 제외).
2. **보유손익**: !retired: quote 유효 && mark 존재 → pnl += h_prev[i]·(close/mark − 1), mark=close, (결측 후 첫 유효면 resumed_holding_count++ if h_prev≠0). quote 결측 → pnl 항 0, mark 유지, 동결(missing_held_count++ if h_prev≠0, frozen_gross += |h_prev|). mark가 없는데 h_prev≠0 은 불가능해야 함(있으면 runtime_error).
3. **체결**: quote 유효인 종목만 h[i]=desired_scaled[i]; 결측 종목은 h[i]=h_prev[i](동결), blocked_order_count++, blocked_trade_weight += |desired−h_prev| (desired≠h_prev일 때). 미보유 종목의 결측일 신규 진입은 체결되지 않고(missing_entry_count++) 다음 유효일에 그때의 최신 desired만 적용(과거 주문 누적 없음).
   frozen budget: F = Σ_{결측 보유}|h_prev|, G = Σ_{quote 유효}|desired|. G > max(0, 1−F) 이면 desired_scaled = desired·(1−F)/G, 아니면 그대로(**확대 없음**). F=0이면 기존 경로와 동일.
4. turnover = Σ_{quote 유효}|h[i]−h_prev[i]|, fee = turnover·fee_rate, ret[t] = pnl − fee.
5. 통계: position_count = |h|>1e-12 (동결 포함), long/short/max_weight는 h 기준.
6. warmup(t < sim_start_index): mark/intent만 갱신, 보유·turnover·fee·pnl 없음, 정산 이벤트도 손익 없음(retired 상태 전이는 반영).
7. **normalize_signal_row**: Carry → finite signal이면 quote 없어도 intent 포함(단 retired 제외, 마스크 규칙 그대로); Error → 기존 게이트(valid close) 유지. signal NaN → target 0.
8. **Error 정책** = 기존 fail-fast와 동일 동작(보유/체결 종목 quote 무효 시 runtime_error). 이벤트는 Error에서도 정산에 사용(정산은 quote 불필요). Carry에서 정산 없이 끝까지 결측인 보유분은 마지막 bar 진단의 frozen_gross로 노출.
9. 결측·이벤트 없는 입력에서 모든 출력이 현재와 **비트 동일**(기존 golden/oracle/HTTP 테스트 무변경 통과).

## 3. 데이터/설정
YAML(`backtest:` 아래): `missing_close_policy: carry|error`, `default_delisting_return: 0.0`, `stale_quote_settle_bars: 0`; 최상위 `events: {path, id_col, end_date_col, return_col}`.
events 로딩(`parquet_reader.cpp` load_config 안): path 상대경로는 config_dir 기준. id_col int64/string 허용(문자열로 정규화해 symbols와 매칭), end_date_col timestamp(ns/us/ms/s)/date32/date64/string(YYYY-MM-DD) 허용, null=이벤트 없음. return_col 있으면 double/float, null=NaN. 중복 id는 오류. settlement_bar = 패널 timestamps에서 `upper_bound(end_date_ms + 86399999)` 인덱스(즉 end date 당일까지 보유, 다음 bar 정산); 그 인덱스가 T 이상이면 -1(패널 밖); end date가 첫 bar 이전이면 0. 이벤트 파일에 없는 심볼은 -1/NaN. 로드 결과를 `/api/config`에 `eventsLoaded`(건수), `settlementSymbols`(패널 내 정산 예정 수)로 노출(엔진 담당과 합의된 키).
실데이터: `/home/base/vendor_data/uss/cash/master/uss_cash_master_acc.parquet` (28,210행, index=tradingItemId int64, securityEndDate timestamp[ns], 2010~ 유니버스 1,056종목 중 종료일 있음 235·끝결측+마지막호가 유니버스내 133·그중 종료일 없음 8, 종료일 이후 정상 호가 1건(Wolfspeed 1일)).
예제 `cpp-engine/examples/datasets/equities_monthly_afl.yaml`에 `events:`와 `missing_close_policy: carry`, `stale_quote_settle_bars: 60` 추가.

## 4. HTTP/JSON (엔진 담당)
요청 옵션(`parse_run_options`): `missingClose`("carry"|"error", 기본 config), `defaultDelistingReturn`(double, 기본 config), `staleQuoteSettleBars`(unsigned, 기본 config).
응답: batch/combined 각각 `missingData` 객체(합계: missingHeldBars, blockedOrders, blockedTradeWeight, missingEntries, resumedHoldings, delistings, delistingPnl, settledGross, defaultDelistings, defaultDelistingGross, staleSettlements, terminalFrozenGross(마지막 bar)), timeseries에 `frozenGross`·`settledGross` per-bar 배열(기존 timeseries 길이 규약과 동일). `/api/config`에 `missingClosePolicy`, `defaultDelistingReturn`, `staleQuoteSettleBars`, `eventsLoaded`, `settlementSymbols`.
GA 경로도 같은 옵션 적용. combined(run_from_signals)도 동일 회계.

## 5. 테스트
엔진(gtest, `tests/test_simulator.cpp` 추가 케이스): 계약 문서 §손계산 사례 10개를 그대로(비용 0.0005, 가격 100 기본). 각 케이스에서 ret/turnover/position_count 및 `accounting_row_callback`으로 받은 h·trade·mark·retired·진단을 손계산과 1e-12 대조. + 결측 없는 입력의 비트 동일 회귀 + Error 정책 기존 예외 유지 + mode 0/1 각각.
데이터(gtest): `tests/test_config_loader.cpp`(옵션 파싱·검증·기본값), `tests/test_parquet_loader.cpp`(합성 이벤트 parquet: 타입별 end date, null, 첫 bar 이전, 패널 밖, return_col, 미등록 심볼, 중복 id 오류).
독립 oracle(python, oracle 담당): §6.

## 6. 빌드
메인 트리(엔진 담당): `/tmp/longshort_module_server/build` 사용. `cmake --build /tmp/longshort_module_server/build --target engine test_simulator longshort-server -j8` 등 필요한 타깃만. 전체: `ctest --test-dir /tmp/longshort_module_server/build --output-on-failure -j4`.
worktree(데이터 담당): 소스 `/tmp/longshort_wt_data`(브랜치 wip/data-events, 헤더 반영됨). 빌드 디렉터리 `/tmp/longshort_wt_data_build` 새로 configure:
```
cmake -S /tmp/longshort_wt_data/cpp-engine -B /tmp/longshort_wt_data_build -DCMAKE_BUILD_TYPE=Release -DLONGSHORT_BUILD_TESTS=ON \
  -DLONGSHORT_PYARROW_PYTHON=/home/jgo/usr/venv-3.11/bin/python -DCMAKE_PREFIX_PATH=/tmp/longshort_module_deps/prefix \
  -DBoost_DIR=/usr/local/lib/cmake/Boost-1.85.0 -Dboost_serialization_DIR=/usr/local/lib/cmake/boost_serialization-1.85.0 \
  -DLONGSHORT_MODULE_DIR=/tmp/longshort_logic_data_modules
```
(FetchContent 소스는 메인 캐시의 `FETCHCONTENT_SOURCE_DIR_*` 값이 있으면 같은 -D로 넘겨 재다운로드를 피하고, 없으면 다운로드 허용.) 타깃: `engine test_config_loader test_parquet_loader data_load_probe` 만 빌드(서버 불필요).

# ===== Phase 2 브리프 (2026-09-11 오후): 원천 이벤트 파이프라인 + successor 이벤트 =====

목표: 1~5단계 — (1) 로더 스케줄은 관리자 요청서만, (2) 마스터 종료일 직접 갱신, (3) co_acthist → 정산 이벤트 파일, (4) 엔진 successor 이벤트 + `return_col` 연결, (5) Top500·Top1000·ALL 전기간 검증. 6단계(OTC 가격 재수집)는 범위 밖.

## P2-0 공통
- xfeed 접속: `/home/base/git/sim/vendor/uss/save_uss_master_market_data.py`의 DB_* 상수를 코드에서 읽어 psycopg2로 접속, **반드시** `options='-c default_transaction_read_only=on'` + `conn.set_session(readonly=True)`. 비밀번호를 출력·기록 금지. 무거운 쿼리는 `statement_timeout` 지정.
- 스키마 힌트: `xfl_target.ciqsecurity(securityid, companyid, securityname, securitystartdate, securityenddate, securitysubtypeid, primaryflag)`, `ciqtradingitem(tradingitemid, securityid, tickersymbol, exchangeid, currencyid, tradingitemstatusid, primaryflag)`, `ciqcompany`, `ciqpriceequity(tradingitemid, pricingdate, priceclose…)`, `ciqmarketcap(companyid, pricingdate, marketcap[USD mn])`, `co_acthist(gvkey, datadate, actioncd, actiontxt, gvkeyaqc, iid, newconm, newcusip, newexchg1, newtic1, oldconm, oldtic1, …)` (최신 datadate 2026-02-19 — 로더 스케줄 문제, 그 이후는 override 표), `ciqgvkeyiid`(CIQ↔Compustat gvkey/iid 매핑 — 컬럼은 직접 확인), `r_actioncd`(코드 설명), `sec_idhist(gvkey, iid, item, efffrom, effthru, itemvalue)`.
- 로컬 마스터: `/home/base/vendor_data/uss/cash/master/uss_cash_master_acc.parquet`(28,210행, index tradingItemId int64, securityEndDate timestamp[ns], symbol string). `/home/base`는 **읽기 전용** — 출력은 전부 `/home/jgo/longshort_platform/data/{master,events}/`(gitignore 대상)에.
- 스크립트 위치: `cpp-engine/tools/data/` (신규 디렉터리). venv `/home/jgo/usr/venv-3.11/bin/python`. 보고서 한국어 ~임체, 숫자엔 근거.

## P2-1 (data-master) 마스터 종료일 갱신
`cpp-engine/tools/data/refresh_master.py --master <acc.parquet> --out data/master/uss_cash_master_refreshed.parquet [--ids-from-cache]`
- 로컬 마스터의 모든 tradingItemId에 대해 `ciqsecurity.securityenddate/securitystartdate`, `ciqtradingitem.tickersymbol/exchangeid/tradingitemstatusid`, `ciqcompany.companyname`을 가격 조인 없이 조회해 갱신. 기존 컬럼·인덱스·dtype 유지(스키마 동일) + `refreshedAt`, `endDateChanged`(bool) 추가.
- 보고: 갱신된 종료일 수(예상 714), 연도별 분포, 변경 전후 샘플 10건, 티커·거래소 변경 건수. 유니버스(Top500/1000/ALL 2010~ 합집합) 안에서 바뀐 종목 목록 CSV.
- 부가: 최근 이벤트 override 표 `data/events/events_override.csv`(컬럼: tradingItemId, ticker, endDate, settlementDate, actionCode, cashPerShare, successorTicker, successorRatio, note, source_url) — 리드가 웹으로 확인한 4건 선입력: EA(2610085? 아님 — EA=2608773: 2026-08-05, 현금 $210, 효력 2026-08-04), EXAS(2610459: 2026-03-23, 현금 $105), AVB(2591859: 2026-08-14, 주식 2.793 EQR/VMRK), EQR(2610085: 2026-08-17, 개명 → VMRK 연속, ratio 1). 기존 DB 4건(DFS·HES·ANSS·CYBR)은 co_acthist에서 자동 파싱되므로 override에 넣지 말 것(검증용 비교만).

## P2-2 (data-events) 정산 이벤트 파일
`cpp-engine/tools/data/build_events.py --master <refreshed or acc> --out data/events/events.parquet --override data/events/events_override.csv --start 2009-01-01`
- 대상: 마스터 전 종목 중 종료일이 있는 것(+override). 각 종목에 대해:
  1. `endDate` = 보유 마지막 날(엔진 `end_date_col`): min(securityEndDate, 이벤트 effective date−1 중 적절한 값) — 규칙을 정하고 문서화. 기본은 securityEndDate.
  2. `co_acthist` 매칭: `ciqgvkeyiid`로 (gvkey,iid) ↔ tradingItemId 정확 매칭, 실패 시 (oldtic1 = 마스터 symbol AND |datadate − 종료일| ≤ 200일) 보조 매칭. `actionCode` 기록.
  3. 조건 파싱(정규식, actiontxt): 현금 `$X`(per share), 주식 `N SHARES OF <acquirer>`, 혼합. 인수사 식별: `gvkeyaqc`→`ciqgvkeyiid`→primary tradingItemId, 실패 시 `newtic1`/`newconm`으로 `ciqtradingitem`/`ciqcompany` 검색. 인수사 종가는 `ciqpriceequity` datadate(없으면 다음 거래일).
  4. `settlementReturn` = (cash + ratio × acquirerClose) / 대상 마지막 유효 종가(로컬 캐시 close 또는 ciqpriceequity) − 1. 파싱 실패·정보 부족 → NaN. BK*/LIQ는 R=−1 제안값을 `suggestedReturn`에, `settlementReturn`은 NaN(정책 결정은 엔진 config).
  5. 개명/합병 승계: N/C 또는 주식 교환 인수 → `successorTradingItemId`(인수사·신규 아이템), `successorRatio`(구주 1주당 승계 주식 수). 현금+주식 혼합은 cash는 R에, 주식은 successor로 — 둘 다 기록하고 엔진이 처리(§P2-3).
- 출력 스키마(parquet, long): `tradingItemId`(string), `endDate`(timestamp), `settlementDate`(timestamp), `actionCode`, `settlementReturn`(double), `suggestedReturn`, `cashPerShare`, `successorTradingItemId`(string, null), `successorRatio`(double), `acquirerName`, `source`('co_acthist'|'override'), `confidence`('parsed'|'manual'|'unknown'), `actionText`(원문 200자).
- 보고(`data/events/events_report.md`): 매칭률(ACQ/N-C/…별), 파싱 성공률, R 분포(현금 딜은 0 근처여야 함 — 이상치 목록), DFS·HES·ANSS·CYBR 손계산 대조, 실패 사례 상위 20건. Top500 유니버스 종목(리드 제공 목록 `docs/missing_close_2026-09-11/no_end_date_symbols.md` 포함)의 커버리지 별도 표.

## P2-3 (engine) successor 이벤트 + return_col 연결
- `EventsConfig`에 `successor_col`, `successor_ratio_col`(optional) 추가; `DataPanel`에 `settlement_successor`(int32, symbols 인덱스, −1 없음/패널 밖), `settlement_successor_ratio`(double). 로더는 data 담당 코드지만 이번엔 엔진 담당이 `parquet_reader.cpp`의 events 블록을 확장(리드 승인).
- 회계(정산 bar = 종료일 이후 첫 NaN bar, 기존 규칙 유지): `R` = explicit finite ? explicit : default. `pnl += h_prev·R`. successor가 있고 그 bar에 유효 quote가 있으면 **현금화 대신 승계**: `h_succ += h_prev·(1+R)` (승계분은 시장 체결 아님 → turnover/fee 0, mark_succ는 당일 close), 진단 `successor_conversions`, `converted_gross`. successor 없음/패널 밖/quote 없음 → 기존 현금 정산. 이후 승계 보유분은 정상 규칙(다음 체결에서 target대로 리밸런스, 그때 turnover/fee 발생).
  - explicit R과 successor가 동시에 있으면 R은 현금+주식 합산 총수익률이므로 승계 가중치는 h_prev·(1+R)로 일관됨. R이 NaN이고 successor만 있으면 R = ratio·close_succ(t)/mark_old − 1 을 엔진이 계산(진단에 'computed' 카운트).
- 예제 config `equities_monthly_afl.yaml`: `events: {path: ../../../data/events/events.parquet, id_col: tradingItemId, end_date_col: endDate, return_col: settlementReturn, successor_col: successorTradingItemId, successor_ratio_col: successorRatio}`.
- 테스트: successor 승계(mode 0/1, quote 있음/없음, 패널 밖, 혼합 딜), 기존 테스트 무변경, 비트 동일 golden 유지. CTest 전체 통과.
- JSON: `missingData.successorConversions`, `convertedGross`; `/api/config`에 `successorEvents`.

## P2-4 (oracle) 원장·하네스
- 원장에 successor 규칙 반영 + 시나리오(승계 quote 있음/없음/패널 밖/혼합/mode 0/1) → self-check, HTTP 대조(엔진 완료 후 리드 지시).
- 하네스: events 파일을 쓰는 config로 Top500·Top1000·ALL(각각 예제 config 복사본, `universe.path`만 교체, `data/configs/`) 실행. 표에 `successorConversions`·`convertedGross`·`postEndDateQuoteBars`·`pendingSettlements` 포함, R 민감도(default 0/−1) 유지. 실행시간·RSS 기록.

## P2-5 (리드) 검증·문서·커밋 제안
