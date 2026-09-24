# update_daily.py — cache_v2.1 일별 갱신 운영 절차 (T9)

`cpp-engine/tools/data/cache_v21/update_daily.py`. 매일 T−1(미국 정규장 마감일) 데이터가 완결된 뒤 실행해 **이전 빌드를 입력으로 새 build_id의 빌드 디렉토리**를 만들고, 게이트를 통과하면 `publish.py`로 넘긴다. 이전 빌드는 절대 수정하지 않으며, 실패하면 새 디렉토리는 진단용으로 남고 이전 빌드가 현재 빌드로 유지된다.

## 1. 원칙

| 항목 | 규칙 |
|---|---|
| raw 가격 | append/교체는 재수신 창(T−5~T−1) 안의 행과, 계수가 재기술(restate)된 종목의 전 이력에만 한정. 값이 바뀐 셀은 전부 `archive/revisions/date=<관측일>/price_revisions.parquet`에 old/new로 기록되고 행 `source=2`로 교체 |
| 계수 | `ciqsplitcache`·`ciqpriceequitydivadjfactor` 전량 재수신 → 구간 테이블 재생성(`prices_factors` 재사용). 저장본 대비 diff → `factor_revisions.parquet`, 월 1회 전체 스냅샷 `archive/factor_snapshots/date=…/` |
| 파생 | `returns_total` 전량 재계산(`prices_returns`), lifecycle·lineage·exclusions·마스터 QA 재실행(`master_*`), universe·marketcap(T5 CLI)·events(T6 CLI) 재생성 |
| DB | 읽기 전용 세션(`db.DB`), 큰 테이블은 `tradingitemid = any(%s)` ≤1000 청크만. 시작 전 `pg_stat_activity` 대기(`--db-wait-max`, 기본 300초 캡) |
| 자원 | 스레드 ≤8(pyarrow·OMP), 실측 peak RSS 5.7GB(본체) / T5 universe 서브프로세스 11GB. `/home/base`·`/home/jgo/61` 쓰기 없음(61 발행은 `--remote`를 준 경우 publish.py가 수행) |
| 실패 | 게이트·임계 실패 시 종료코드 ≠0, `BUILD.json.status = failed`, publish 미호출. 새 디렉토리는 남김 |

## 2. 디렉토리 레이아웃

이전 빌드(`--prev-build`)는 두 레이아웃을 자동 인식한다.

| 레이아웃 | 판별 | 예 |
|---|---|---|
| stage | `<root>/T2/base_data.parquet` 존재 | `stage_v21/` (T1·T2 원본), `update_daily`의 `--out-stage` 산출물 |
| published | `<root>/base_data.parquet` 존재 | `publish.py --out` 산출물, 61의 발행본 |

출력(`--out-stage`)은 항상 **stage 레이아웃**이며 `publish.py --stage <out-stage>`가 바로 읽는다.

```
<out-stage>/
  BUILD.json                     build_id, prev_build, asOf, 단계별 시간·RSS, 결과·게이트·경고, status
  update.log / subprocess.log    본체 로그 / T5·T6·publish 서브프로세스 로그
  T1/master/*.parquet            instruments, current, classification_history, primary_history, id_mapping,
                                 identifier_history, lifecycle, lineage, tradable_exclusions
  T1/_work/                      ciq_agg·mi_agg(lifecycle 집계, 증분 갱신), calendar, identity, lineage_overlap(캐시), candidate_negatives
  T1/qa/                         master_qa 산출(gates.json, lifecycle_exceptions.csv, …)
  T2/base_data.parquet           raw long (row group = id 청크 39개 유지, (tradingItemId,date) 정렬)
  T2/factors/{split,dividend}.parquet
  T2/derived/returns_total.parquet
  T2/qa/update_gates.md          그날의 게이트 표 (+ factor_artifacts.csv, 새 인공물/불일치 목록)
  T2_work/adjf_long.parquet      side table: 행별 vendor adjustmentfactor·ciqClose (base_data와 같은 row group·행 순서; G-SPLIT·reconcile 근거)
  T2/factors/adjustment_factor_daily.parquet   같은 파일의 하드링크(발행본 이름; 계약: 검증용 side table, split 구간과 항등)
  calendar.parquet               date, nClose, rollingMedian, isTradingDay — publish.apply_calendar와 같은 규칙(종가 수 ≥ 중심 41일 롤링 중앙값의 20%)
  T2_work/{raw_splitcache,raw_divadj}.parquet, asof.json
  T5/derived/marketcap.parquet, T5/universe/membership{,_v1rule}.parquet, T5/qa/
  T6/events/corporate_actions.parquet, T6/qa/
  T6/events/ca_ledger*.{parquet,csv,json}   원장(D1 `ledger_build.py --incremental` 스테이지 = T6/: root/ work/ overrides/ handover/ qa/ledger/ + 상태 json)
  T6/engine/{ca_ledger_cache,events_cache}.parquet(+manifest)   발행 전 엔진 export(표시용 행·노출 요약용; 발행본 export 는 publish 가 다시 생성)
  T1/master/{lifecycle_dates,aux_instruments,lifecycle_aux}.parquet   rc2 보조 종목·날짜 side table(carry + 갱신)
  T1/_work/{lifecycle_instruments,tradable_exclusions_instruments,lifecycle_pre_override,lineage_v1_daily}.parquet   publish 뷰·진단용
  T2_work/split_vendor.parquet   교정 전 벤더 split 표(G-SPLIT 항등·returns_v2_qa --vendor-factors 기준)
  T2_work/returns_rc1_style.parquet   rc1 벤더계수식 수익률(returns_v2_qa G-VENDOR 기준; --no-returns-qa-vendor 면 없음)
  T2/qa/returns_v2/{gates.json,...}, T2/qa/returns_attr_{diff.parquet,unattributed.csv,ledger_diff.csv}   §12 게이트 산출
  rc2f/{rc2f_apply_log.csv,rc2f_apply_info.json,overrides/}   그날의 교정 재적용 로그(publish 가 qa/rc2f 로 복사)
  qa/exposure_daily.md (+ exposure_*.csv)   노출 요약(§12-5)
  view/, publish_view/, rc2_stage/   심볼릭 링크 뷰(returns_v2_qa --build, publish --stage/--rc2-stage 입력; 산출물 아님)
  archive/revisions/date=YYYY-MM-DD/{price,factor,master,lifecycle,ca_ledger}_revisions.parquet
  archive/master_snapshots/date=YYYY-MM-DD/current.parquet
  archive/factor_snapshots/date=YYYY-MM-DD/{split,dividend}.parquet   (월 1회)
```

published 레이아웃 입력: side table은 `factors/adjustment_factor_daily.parquet`, 캘린더는 `calendar.parquet`, 알려진 G-JUMP 인공물 목록은 `qa/T2/factor_artifacts.csv`에서 읽는다(publish가 side table의 row group을 바꾸거나 off-calendar 행을 뺐으면 (id,date) 키로 재정렬해 base_data와 맞춤). 발행본에는 lifecycle 집계(`_work/ciq_agg·mi_agg`)와 1968~ 거래일 캘린더가 없어 U4가 전 종목 재집계(약 33분)로 대체되니, **매일 체인은 전일의 out-stage**를 입력으로 쓰는 것을 권장한다(`--prev-sidecar`로 side table 경로를 따로 줄 수도 있음). 발행본 입력 시험: `stage_v21/T9/qa/run_pub/`(U2·U3·U6 1.3분, 게이트 통과).

## 3. 단계 (`--steps U1,…,U7`, 기본 전체)

| 단계 | 내용 | 실패(중단) 조건 |
|---|---|---|
| U1 master | 종목 규칙(`prices_build.SQL_V1_RULE ∪ SQL_EVER_MAIN`) 재평가 → 이전 집합에 없고 2000+ 종가 ≥1행인 새 id를 전 이력 1회 수신해 추가(음성 후보는 `candidate_negatives`에 기억, `--full-lifecycle`에서 재검사). 전 종목 identity 재조회 → `current` 재작성·이전값과 diff(`master_revisions`), `instruments` 갱신/추가, 분류·primary 변화는 `classification_history`/`primary_history`에 관측 행 append(source `db_current`, effectiveFrom=관측일, 이전 구간은 관측일−1로 닫음). `id_mapping`·`identifier_history` 전량 재생성(작은 테이블). 스냅샷 `archive/master_snapshots/` | identity 조회 실패 |
| U2 raw | `ciqpriceequity`·`miprice` [prev asOf−10일, 상한] 전 종목 청크 조회 → 캘린더 갱신(이전 `calendar.parquet` nClose + 새 날짜의 종가 수 → publish 규칙으로 isTradingDay 재판정, `calendar.parquet` 재작성) → asOf 결정(§4) → 창 = asOf까지 최근 `--refresh-days`(5) **거래일** → raw 변환(`prices_build.transform_chunk`) → **off-calendar 날짜(종가 수 < 롤링 중앙값 20%) 행은 base_data에 넣지 않고 revisions reason `off_calendar`로 기록**(저장돼 있던 그 날짜 행은 제거·`off_calendar_removed`) → 저장 행과 비교(상대차 >1e-6 = 정정, 행 신규/소멸 기록) → qualityFlag(§5) → G-RAW 창 통계 | 종가 수 임계(§4), 저장 창 행의 >0.5% & >50행이 재조회에서 사라짐(퓨즈) |
| U3 factors | 계수 전량 재수신 → 재기술 의심 종목 R = (이전 split source `ciqpriceequity_adjf`) ∪ (splitcache 창 변경) ∪ (창 행의 adjf ≠ side file) ∪ (창 행의 adjf ≠ splitcache lookup) → R의 전 이력 재조회, **raw 불변 확인**(허용 `1e-6×max(1,adjf)+1.2e-7×raw` [추정: 벤더 6자리 저장 근거]) → side file 갱신 → base_data·side file 단일 병합 패스(row group 유지·정렬·정합 증명) → `prices_factors.reconcile_split`·`check_intervals` → 계수 저장, diff → `factor_revisions`, 월 스냅샷 | raw 불변 위반(`T2/qa/raw_invariance_violations.csv`), 구간 검사 실패 |
| U4 lifecycle | ciq/mi 집계(first/last close·row, n_*)를 창·재조회 행으로 증분 갱신(주 1회 `--full-lifecycle`: `master_lifecycle.step_aggregate` 전 종목 재집계 16분×2) → `step_lifecycle`(§5 규칙: 마커 없는 무종가는 provisional; events 마커 = 이전 빌드 events → `T6/events` carry) → `step_lineage`(v1 후보) → `apply_lineage_first_dates(cal, db)` → `step_exclusions` → `master_qa.run_all` → **rc2f: lineage 일별 병합(이전 v2 행 carry + 새 v1 쌍, chainGap 재계산) + lineage 교정 재적용**(§12-2). lifecycle 은 여기서는 교정 전(instruments)·revisions 는 U5 finalize 에서 | events 파일 없음 |
| U5 derived | T5(`universe_marketcap.py build …`, `universe_build.py run …` + `--t5-universe-args`) → T6(`events_build.py prepare-master/prepare-close/run/finalize`) → **finalize(rc2f)**: lifecycle 교정 재적용·aux 행 append·events 델타(R3 reclass/override 행 carry + 교정 패치)·lifecycle_dates 재구축·lineage merger 링크 → **U-L 원장 증분**(`ledger_build.py --incremental --prev <이전 원장 스테이지|발행본> --asof <T−1> --out T6 --build <out-stage> --overrides-glob …`) → settlementReturnScope 라벨·payDate 갱신 → `prices_returns.build_returns` v2(원장·lineage·lifecycle) → rc1 벤더식 수익률(G-VENDOR 기준) → `returns_v2_qa` → 이전 빌드 대비 변화 셀 귀속(§12-4) | 원장 증분 rc≠0·stale(코드 5); T5/T6 실패는 경고 + carry |
| U6 gates | §6 + §12 게이트(G-OVR·G-PRICE-OVR·G-RAWINV·G-RETQA·G-RET-ATTR·G-LEDGER·G-SOURCE·G-EXPO), 발행 전 엔진 export, `qa/exposure_daily.md` | blocking 게이트 실패 |
| U7 publish | `publish.py --stage <out>/publish_view --t2 <out>/T2 --rc2-stage <out>/rc2_stage --out <publish-out> --build-id … --ledger --lineage --orch/--orch-rc2/--orch-rc2f`(로컬) → 발행본 엔진 export(`engine/ca_ledger_cache`·`events_cache`) 앵커 재검사(G-ANCHOR) → 통과 시 `publish.py --sync-only --out <publish-out> --remote <61>`(rsync → `.incoming-<id>` → sha 대조 → rename 2회) | publish rc ≠0, 앵커 불일치, sync 실패(코드 8; 로컬 발행본은 남음) |

`--dry-run`: U1(마스터 파일은 씀, 작음)·U2 조회·비교·U3 계수 diff까지 수행하고 base_data/계수/파생은 쓰지 않음. 결과 `qa/dryrun_<날짜>.json`.

## 4. asOf 결정과 종가 수 임계

- `--asof YYYY-MM-DD`: 명시. 그 날의 ciq 종가 수 ≥ 0.9 × 직전 거래일 종가 수가 아니면 중단(종료코드 4).
- `--asof auto`(기본): 상한 = 정규장 마감(16:00 ET, 조기폐장 13:00, NYSE 휴장일 하드코딩 = `observe_finalization`)이 지난 최근 거래일. 그 이하 거래일 중 임계를 넘는 마지막 날. 상한일이 아직 임계 미달이면 종료코드 3(데이터 미도착 → 크론 재시도), `--allow-stale`이면 그 전날로 진행.
- 거래일 = publish.apply_calendar와 같은 규칙: 그 날 종가 수 ≥ 중심 41일 롤링 중앙값(min_periods 5)의 20%. 이전 빌드 `calendar.parquet`(없으면 base_data에서 재계산)의 nClose에 새 날짜의 종가 수를 이어 붙여 재판정하므로 publish의 판정과 일관됨. 실측 2000~2026-09-15: 6,731일 중 거래일 6,715·off-calendar 16(휴일 단일 OTC 프린트, 종가 1건).
- 실측(2026-09-15): 일별 ciq 종가 7,218~7,287, mi 7,217~7,313 → 임계 6,500 안팎. v1의 고정 5,500 규칙을 대체.

## 5. qualityFlag·status 규칙

| 값 | 의미 | 부여 |
|---|---|---|
| 0 | confirmed | 창 안 행: mi 종가와 일치(≤1e-3) 또는 mi 없음(asOf 이전 날짜) |
| 1 | provisional | **asOf(T−1) 행**은 ciq·mi 종가가 둘 다 있고 일치할 때만 0, 아니면 1. 다음 날 재조회에서 0/2로 확정 |
| 2 | mi_mismatch | asOf 이전 날짜에서 |ciq/mi−1| > 1e-3 |
| 3 | ciq_null_close | 종가 없음 |
| 4 | post_lifecycle_tail | publish가 부여. 입력에 있으면 읽을 때 0으로 되돌리고 publish가 다시 부여 |

lifecycle status는 `master_lifecycle.classify`(마커 = securityEndDate·status 4/5/8/11·events 종료; 마커 없는 10거래일 무종가는 provisional/data_gap).

## 6. 게이트 (`T2/qa/update_gates.md`, `BUILD.json.results.gates`)

| 게이트 | 검사 | blocking |
|---|---|---|
| intervals | split·dividend 구간 빈틈·겹침·열림(`prices_factors.check_intervals`) | 예 |
| G-SPLIT | 전 행 lookup vs side file adjustmentfactor 1e-6(`prices_qa.gate_split`, 반례 1구간 ×1.5 검출 포함) | 예 |
| G-JUMP | 계수 변경 종목·재조회 종목·창 안 경계에서 split_adj 40%↑ & raw 40%↓ 점프(`prices_qa.fake_jumps`). 알려진 인공물(`factor_artifacts.csv`) 제외한 **새** 건 ≤ `--allow-new-fake-jumps`(기본 0). 반례: 경계 하루 이동 검출 | 예 |
| G-DIV | 새 배당 스텝만 `1−D/P_prev` 항등식(`prices_qa.div_step_identity`), 5자리 반올림·무보정식 설명분 제외한 genuine 불일치 | 기본 경고(`--strict-div`로 blocking) |
| G-RAW | 창 안 ciq vs mi 종가 불일치율 ≤ `--raw-mismatch-max`(0.01) | 예 |
| publish | base_data id ⊆ lifecycle id, lifecycle id = instruments id | 예 |
| returns | returns 행 수 = close notna 행 수 | 예 |
| master | master_qa self-test·집합 정합·이력 겹침 0·lifecycle self-test | 예 |
| publish | base_data id ⊆ lifecycle id, lifecycle id = instruments ∪ aux | 예 |
| G-OVR | 그날의 교정 재적용 로그 vs 이전 빌드 로그: rejected 0, applied→rejected 회귀 0, 이전 로그의 id 누락 0(RULE-* 제외); 상태 변화(applied→no_op 등)·새 id 는 목록 | 예 |
| G-PRICE-OVR | price 교정 창 안의 저장 행 전부 qualityFlag 8/32(꼬리 4) | 예 |
| G-RAWINV | 재기술 종목의 벤더 raw 가 저장값과 다름(§12-3): 기본 저장값 유지 + 목록(`--strict-raw` 면 blocking, `--raw-change-policy abort` 면 코드 5) | 옵션 |
| G-RETQA | `returns_v2_qa` G-CASH/G-SPLIT/G-NAN/G-LINK/G-ORACLE(+G-VENDOR: rc1 벤더식 기준) 전부 pass | 예 |
| G-RET-ATTR | 이전 빌드 대비 바뀐 returns 셀 전부 귀속(§12-4), 미귀속 0 | 예 |
| G-LEDGER | 원장 sidecar asOf ≥ 빌드 asOf(stale 아님; `--allow-stale-ledger` 면 통과+표시) | 예 |
| G-SOURCE | co_acthist 마지막 datadate ≥ asOf − `--co-acthist-max-lag-days`(7); 아니면 경고 + 대체 원천 의존 기록 | 아니오 |
| G-EXPO | 라이브 북 보유 × (asOf 행 flag 3/4/NaN, 새 조건 없는 정산, 새 표시용 행, lastTradeDate 이후 보유) 교차 0 | `--strict-exposure` 만 |
| G-ANCHOR | (U7, 로컬 발행 뒤·원격 동기화 **전**) 발행본 `engine/events_cache`·`ca_ledger_cache` 앵커(마크·승계사 종가·자회사 종가) = 발행 패널 split_adj 종가; 통과해야 `publish.py --sync-only --remote` 로 61 에 올림 | 예(코드 8) |
| G-FACTOR-JUMP | 그날 추가·변경된 split 경계(validFrom ≤ asOf)의 raw 점프 × parentFactor |log| ≤ log 4 | 예 |
| G-RET-OUTLIER | 이전 asOf 이전 셀이 새로 4배 이상/−75% 이하 이동 0, 어디든 |ret| > 50 0 (새 날짜 4배 이상은 목록) | 예 |
| G-EVENTS | 채택된 events 델타 행(변경·신규·삭제) ≤ `--events-delta-max`(100); not_adopted T6 차이·조건 없는 정산 증감 기록 | 예 |
| G-DOUBLE-LEG | 옛 id 와 계보 새 id 에 같은 (child, ratio) 분배 leg 가 ±3일 안에 둘 다 있음 = 0 | 예 |

`--selftest`: DB 없이 검출기 20건 반례 테스트(정정 1셀·행 소멸·플래그 규칙·임계·계수 diff·재기술 검출·raw 불변·G-JUMP 경계 이동·G-DIV 새 스텝·병합·증분 집계·이력 append).

## 7. 크론 예시

§11(D4 운영 배선 초안)로 대체. 래퍼 `cron/update_daily.sh` + 준비 프로브 `cron/readiness_probe.py` + 크론 라인 `cron/crontab.example`(미등록). 요지: T3 관측(§11-1)상 ciq 순번 03 적재(07:00 KST, EDT)에서 임계는 통과하나 종가·거래량은 순번 04(10:00)·05(14:00)에서 바뀌고 분할 파일은 14:00, 배당 파일은 16:00 에 적재되므로, 크론은 14:10~19:10 매시 시도하고 프로브가 "전 원천 적재 완료"를 판정한 슬롯(EDT 16:10 / EST 17:10)에서 실행한다.

종료코드(update_daily.py): 0 성공 / **9 성공+노출 경고**(빌드·발행 완료, `qa/exposure_daily.md` 에 라이브 북 교차 있음; `--exposure-warn-rc`, 0 이면 비활성; `--strict-exposure` 면 대신 7) / 3 데이터 미완결(재시도) / 4 종가 수 임계·행 소멸 퓨즈(instruments 행 기준; aux 는 별도 집계) / 5 raw 불변(`--raw-change-policy abort` 일 때)·구간 검사·원장 증분 실패·원장 stale / 6 events 파일 없음 / 7 게이트 실패 / 8 publish 실패·앵커 불일치 / 1 예외.

## 8. 실패 시 대응

| 증상(BUILD.json.failed.code) | 원인 | 대응 |
|---|---|---|
| 3 not complete yet | 벤더 적재 지연 | 재시도(크론 15:10). 하루 넘게 지속되면 `--allow-stale`로 전날 기준 빌드 후 로더 담당 문의 |
| 4 close-count threshold | 부분 적재·휴장일 오인 | `asof_pick.checks` 확인. 휴장일이면 `observe_finalization.NYSE_HOLIDAYS` 갱신 제안, 부분 적재면 재시도 |
| 4 stored rows missing | 재조회 결과가 저장 행보다 적음(DB 장애·로더 롤백) | 삭제 금지 퓨즈. `compare.removed_rows` 목록 확인 후 재시도; 진짜 벤더 삭제면 `REMOVED_ROWS_MAX_FRAC` 상향 1회 실행 |
| 5 raw not invariant (`--raw-change-policy abort`) / G-RAWINV 목록(기본 keep) | 계수 재기술 종목의 raw까지 바뀜(새 역분할 뒤 전 이력 재기술: 정밀도 바닥 단위 오류(×1e10)·vwap 반올림 드리프트·정정+분할 동시) | `T2/qa/raw_invariance_violations.csv`(id 별 표 = BUILD.json `U3.raw_invariance.by_id`). 기본은 저장값 유지 + `vendor_restated_kept` revisions; 벤더 정정이 맞으면 `--accept-raw-change <id,…>` 로 재실행(`vendor_restated_accepted`, 행 source 2); 단위 오류면 `overrides/rc2f/D<날짜>/price_overrides.csv` 로 레지스트리 사건 등록 |
| 5 ledger incremental failed / ledger stale | D1 원장 증분 rc≠0, sidecar asOf < 빌드 asOf | `subprocess.log`(ledger_build --incremental), `T6/events/ca_ledger_build_info.json`; 원천 지연이면 다음 슬롯, 급하면 `--allow-stale-ledger`(꼬리 retFlag 4 표시) |
| 7 gates: G-OVR | 교정 재적용 회귀·rejected·누락 | `rc2f/rc2f_apply_log.csv` 의 rejected/status 변화 행(BUILD.json `gates.G-OVR.examples`). 벤더 표가 바뀌어 교정이 no_op/rejected 가 되면 교정 CSV 갱신(레지스트리) 후 재실행 |
| 7 gates: G-PRICE-OVR | price 교정 창 안 행이 flag 8/32 가 아님 | 교정이 유실된 행(`examples`): 재기술 종목 병합 경로 점검 |
| 7 gates: G-RETQA | returns_v2_qa 게이트 실패 | `T2/qa/returns_v2/gates.json`·`*_mismatch.csv`(rc2 V2 규약과 동일) |
| 7 gates: G-RET-ATTR | 귀속 안 되는 변화 셀 | `T2/qa/returns_attr_unattributed.csv`: 창·정정·원장·교정·계보·lifecycle·신규 id 어디에도 안 잡힌 셀 = 규칙 밖 변화 → 원인 규명 전 발행 금지 |
| 7 gates: G-EXPO(`--strict-exposure`) / 9 | 보유 종목 미확정 | `qa/exposure_daily.md`·`exposure_book_hits.csv` → §11-4 절차 |
| 8 anchor mismatch | 발행본 엔진 export 앵커 ≠ 패널 | `BUILD.json U7.anchor_check`; 발행 산출물은 남으나 61 발행 금지 |
| 5 interval check | splitcache 창 겹침/빈틈 | `prices_factors` 규칙상 발생하지 않아야 함 → 벤더 데이터 검토 |
| 7 gates: G-JUMP | 새 벤더 계수 인공물 | `T2/qa/factor_artifacts_new_<날짜>.csv` 검토 → 인공물이면 `--allow-new-fake-jumps N`으로 재실행(목록은 `factor_artifacts.csv`에 누적) |
| 7 gates: G-RAW | ciq·mi 대량 불일치 | 어느 쪽 정정인지 T3 관측과 대조. ciq 값이 원천이므로 대개 mi 지연; 임계 상향 재실행 또는 대기 |
| 7 gates: publish/master | 종목 집합 불일치 | U1 로그의 새 id·identity 누락 확인 |
| 8 publish 실패 | rsync·sha256 불일치 | `subprocess.log`; 로컬 publish 산출물은 남아 있으므로 `publish.py --remote`만 재실행 가능 |
| T5/T6 경고(carry) | 모듈 실패 | 이전 파일이 carry되어 asOf가 하루 뒤짐. `subprocess.log`로 원인 확인 후 `--steps U5,U6,U7` 재실행(U5는 returns도 다시 계산, 수 초) |

부분 재실행: `--steps`로 일부만 돌리면 건너뛴 단계의 산출물은 이전 빌드에서 복사된다(U2/U3를 건너뛰면 base_data·side file 복사, U5를 건너뛰면 returns 복사).

## 9. revisions 스키마 (`archive/revisions/date=<관측일 KST>/`)

| 파일 | 컬럼 | 비고 |
|---|---|---|
| price_revisions.parquet | tradingItemId int64, date ts[ns], field str(open/high/low/close/vwap/volume/**row**), old f64, new f64, observedAt ts[ns], reason str | reason: `vendor_refetch`(창 안 값 변경, 행 source=2) / `vendor_late_row`(창 안 새 행, old NaN) / `vendor_removed`(저장 행이 재조회에 없음 → 삭제, new NaN) / `vendor_refetch_full_added`·`vendor_refetch_full_removed`(계수 재기술 종목 전 이력 재조회의 행 추가·삭제) / `off_calendar`(비거래일 날짜의 새 행, 저장 안 함, new=close) / `off_calendar_removed`(저장돼 있던 비거래일 행 제거) / `vendor_restated_kept`(재기술 종목의 벤더 값이 저장값과 다르나 저장값 유지: old=저장(유지), new=벤더 값) / `vendor_restated_accepted`(`--accept-raw-change`: 벤더 값 채택, 행 source 2). field=row는 close 값을 old/new에 담음 |
| factor_revisions.parquet | table(split/dividend), tradingItemId, validFrom, validTo_old, validTo_new, factor_old, factor_new, source_old, source_new, kind(added/changed/removed), observedAt | (tradingItemId, validFrom) 키. snapshotDate 차이는 무시 |
| master_revisions.parquet | table(current/instruments/id_mapping/identifier_history), tradingItemId(-1 = 테이블 단위), field, old str, new str, observedAt | current 12필드·instruments 5필드의 값 변화. 분류·primary는 별도로 history 테이블에 append |
| lifecycle_revisions.parquet | tradingItemId, field(status/lastTradeDate/endReason/firstTradeDate), old, new, observedAt | 교정 후 lifecycle(+aux) 기준, U5 finalize 가 기록. status 전이 표는 BUILD.json `U5.finalize.status_transitions` |
| ca_ledger_revisions.parquet | D1 `events/ca_ledger_revisions/date=<asOf>/revisions.parquet` 사본(원장 행 added/changed/removed) | 귀속(G-RET-ATTR)의 원장 창 근거 |

같은 관측일에 두 번 빌드하면(재실행) 이전 시도의 `date=<관측일>/` 은 `date=<관측일>.run<N>/` 으로 남기고 새로 쓴다(덮어쓰기 없음).

## 10. 알려진 제한·제안 (T9 보고서 참조)

- T5 marketcap CLI는 증분 옵션이 없어 매일 `ciqmarketcap` 전량 재수신(약 5분). `--since` 옵션을 T5에 제안.
- G-DIV 잔여 불일치는 벤더 계수 특성상 매일 0이 아닐 수 있어 기본 비차단.
- raw 불변 위반은 §12-3(기본 저장값 유지 + 목록, `--accept-raw-change`, `--raw-change-policy abort`).
- side table(`T2_work/adjf_long.parquet` = 발행본 `factors/adjustment_factor_daily.parquet`)은 G-SPLIT 전수·reconcile의 근거이므로 빌드 체인에서 함께 갱신·보존한다(publish가 같은 이름으로 발행).
- T5 universe CLI 인자는 T5 확정 후 재확인(`--t5-universe-args`로 코드 수정 없이 반영 가능).

## 11. 운영 배선 초안 (D4, 2026-09-21; 크론 미등록·61 미접촉)

파일: `cron/update_daily.sh`(래퍼) · `cron/readiness_probe.py`(준비 프로브, xfl_control 읽기 전용) · `cron/prune_builds.sh`(보존 정책, 기본 dry-run) · `cron/crontab.example` · `cron/logrotate.conf`. 근거·표 원본은 `SP/orch_daily/reports/D4.md`, 표 산출 스크립트 `SP/stage_daily/D4/timing_analysis.py`.

### 11-1. 실행 시각 (T3 관측 2026-09-16~21, 관측 120회, 비검열 세션 3개 09-16·17·18; 파일 도착은 xfl_control 8-21~9-20)

원천 적재 시각표(KST, **EDT** 기준; EST(11월 첫 일요일~3월 둘째 일요일)는 +1h — 벤더 스케줄이 ET 기준이라는 [추정], 2026-11-02 이후 실측 확인 항목):

| 원천 | 파일 | 도착 KST(관측 범위) | 로더 적재(매시 :00) | 내용 |
|---|---|---|---|---|
| ciq 가격 순번 03 | PriceEquityRecentHistoryIntradayChange<D>-03 | 06:12~06:15 (21 거래일 08-21~09-19) | 07:00 | T−1 종가 행 도착(최종 종가 수의 98.7~98.8%; 직전일 대비 0.984~0.990 ≥ 0.9), **임계 통과** |
| ciq 순번 04 | …-04 | 08:41~09:49 (21 거래일) | 09:00/10:00 | 종가 정정(표본 465 중 40·3·51, 상대 중위 3e-4 최대 3e-3)·OHL·거래량(+2~4%)·vwap, 종가 수 최종 |
| ciq 순번 05 | …-05 | 13:07~13:16 (21 거래일; 휴장 라벨 09-07 만 15:10) | 14:00 | 거래량 최종(전 행, 중위 0·최대 1.1%) — 이후 변경 0(≤102h 관측) |
| MI 종가 배치 | MIPriceRecentHistChange(15분 간격) | 05:40~06:16 큰 파일 | 06:00/07:00 | 표본 463/465 |
| MI 정정·sourceidentifier | 〃 | 08:39~09:10 | 09:00/10:00 | 종가 3~31 정정, 전 행 sourceidentifier |
| MI 거래량 최종 | 〃 | 12:09~13:01 | 13:00/14:00 | 이후 변경 0 |
| 분할 ciqsplit·ciqsplitcache | SplitsV2Change<D+1> | 13:10~13:25 (20일) | 14:00 | U3 계수 재수신 |
| 배당 ciqdividend·cache | DividendsV2Change<D+1> | 15:09~15:13 (20일) | 16:00 | D1 원장 증분 |
| 식별자·상태 ciqsecurity·tradingitem | IDCSecurityChange<D+1> | 17:39~17:54 (20일) | 18:00 | U1 identity·lifecycle 마커(1일 지연 허용) |
| 시총 ciqmarketcap | MarketCapV2IntradayChange | 수시 | 매시 | T5 전량 재수신 |
| co_acthist | s_co_acthist | 2026-02-20 이후 정지 | — | 알려진 이슈(build doc §7-8) |

KST 시각별 조건 충족(3세션, EDT):

| KST(EDT) | KST(EST) | 임계 통과 | ciq 종가 최종 | ciq·MI 전 필드 최종 | 분할 | 배당 | 식별자 |
|---|---|---|---|---|---|---|---|
| 07:10 | 08:10 | 3/3 | 0/3 | 0/3 | 전날 | 전날 | 전날 |
| 10:10 | 11:10 | 3/3 | 3/3 | 0/3 | 전날 | 전날 | 전날 |
| 14:10 | 15:10 | 3/3 | 3/3 | 3/3 | 당일 | 전날 | 전날 |
| **16:10** | **17:10** | 3/3 | 3/3 | 3/3 | 당일 | 당일 | 전날 |
| 18:10 | 19:10 | 3/3 | 3/3 | 3/3 | 당일 | 당일 | 당일 |

권고: **16:10 KST(EDT) / 17:10 KST(EST)** = 가격·계수·배당까지 당일 파일 반영(다음 미국 개장 22:30/23:30 KST 까지 6h). 크론은 14:10~19:10 매시 호출하고 프로브가 판정하므로 DST 전환 시 crontab 수정 불필요. "≥99%" 는 통계로 못 세움(무실패 n=3(변경 추적)·21(파일 도착); rule of three 로 실패율 95% 상한 3/n ≈ 14% 이상) — 도착 산포(순번 05 9분, 배당 4분, 분할 15분)와 적재 시각 대비 여유(순번 05 +2h, 배당 +10분 + 매시 재시도 3회)로 갈음하고 크론 로그 누적으로 사후 검증. 조기폐장(13:00 ET; 2026-11-27·12-24) 은 마감만 3h 빠르고 파일 스케줄은 같다고 보아 슬롯 동일[추정]. 휴장일(`observe_finalization.NYSE_HOLIDAYS` 하드코딩, 연 1회 갱신)은 프로브가 "세션 ≤ 이전 asOf" 로 건너뜀. 재시도 간격 1h(로더가 매시 :00 적재). 가격만 급하면 `--early`(순번 05·분할까지; 배당은 전날 파일).

### 11-2. 래퍼 흐름·종료코드 조치

`cron/update_daily.sh`: flock → 61 마운트 확인 → `readiness_probe.py --prev-build $ROOT/current` → (`state/done_<D>` 면 종료) → `update_daily.py --prev-build current --out-stage builds/<id> --asof <D> --publish-out published/<id> --remote <61> --threads 8 --orch $ROOT/orch` (토요일 `--full-lifecycle`; nice/ionice; timeout 3h) → 코드별 조치. 경보 = `$ROOT/alerts/<시각>_rc<code>.flag`(+`LATEST` 링크) 와 `ALERT_CMD`(비어 있으면 파일만; 서버에 MTA 없음 — 메일/웹훅 전송은 후속).

| rc | 뜻 | 래퍼 조치 | 사람 |
|---|---|---|---|
| 0 | 성공·발행 | `current` 이동, `state/done_<D>`, 61 manifest asOfDate == D 확인, 로컬 prune | — |
| 9 | 보유 종목 미확정 경고(발행됨; D2 확정 `--exposure-warn-rc 9`) | 0 과 같음 + 경보(`qa/exposure_daily.md` 첨부) | §11-4 절차 |
| 3 | 데이터 미완결 | 다음 슬롯 재시도; 19:10 에도 3 이면 경보 | 로더 지연 확인 |
| 4 | 종가 수 임계·행 소멸 퓨즈 | 1회 재시도, 재발 시 `state/failed_<D>` + 경보 | `BUILD.json.failed`·`asof_pick.checks`(부분 적재/휴장 오인/벤더 삭제) |
| 5 | raw 불변·구간 검사·원장 stale | 즉시 실패 고정 + 경보 | D1 원장 증분(`ca_ledger_build_info.json` asOf ≥ D) 확인 후 재실행; raw 위반은 `T2/qa/raw_invariance_violations.csv` |
| 6 | events 파일 없음 | 실패 고정 + 경보 | 경로 확인 |
| 7 | blocking 게이트 | 실패 고정 + 경보 | `T2/qa/update_gates.md`; 인공물이면 `--allow-new-fake-jumps N` 재실행 |
| 8 | publish 실패 | 1회 재시도, 이후 고정 + 경보 | `61/…incoming-<id>`·`.prev-` 상태 확인(§11-3), publish 만 재실행 |
| 10/11 | 61 마운트 없음 / 프로브 오류 | 다음 슬롯; 마지막 슬롯이면 경보 | sshfs 재마운트 / DB |
| 124/137 | timeout | 실패 고정 + 경보 | 61 두 rename 사이 중단 여부 확인 |
| 1 | 예외 | 실패 고정 + 경보 | `logs/update_<id>.log` |

같은 세션은 최대 3회 빌드 시도(`state/tries_<D>`), 실패 고정은 `rm state/failed_<D>` 로 해제.

### 11-3. 61 발행 원자성·보존

publish.py: `rsync → <remote>.incoming-<id>` → 전 파일 sha256 대조 → `rename(remote → remote.prev-<prev_id>)` → `rename(incoming → remote)`. 두 rename 사이(수 ms)에는 `remote` 가 없고, 그 사이 중단되면 `.prev-`·`.incoming-` 만 남는다 → 복구는 `mv <remote>.incoming-<id> <remote>`(sha 는 이미 대조됨) 또는 `mv <remote>.prev-<prev_id> <remote>`(롤백). 래퍼는 성공 후 manifest asOfDate 를 대조하고 prune dry-run 이 `remote` 부재를 경고한다.

보존(`cron/prune_builds.sh`; 로컬은 래퍼가 성공 후 `--apply-local`, 61 은 월 1회 사람이 `--apply-remote`):

| 대상 | 규칙 | 실측 크기 |
|---|---|---|
| 로컬 `builds/`(stage) | current + 최신 성공 3개, 실패 빌드 7일 | 1.7GB/빌드 → 정상 상태 ≈ 7GB |
| 로컬 `published/` | 최신 2개 | 1.4~1.6GB/개 |
| 61 `.prev-*` | 최신 2개 유지; 그보다 오래된 것은 그 달 `.snap-YYYY-MM` 없으면 rename(= 그 달 첫 발행본 스냅샷), 있으면 삭제; 스냅샷은 수동(12개월 권고) | 1.6GB/개 → current+2+12개월 ≈ 24GB |
| 61 `.incoming-*` | 24h 지난 것 삭제(중단된 publish) | — |
| `archive/`(revisions·master_snapshots·factor_snapshots) | 매 빌드 carry-forward → 최신 stage 빌드가 전 이력 보유(publish 는 61 로 내보내지 않음). 월 1회 스냅샷(61 `.snap-`) 에는 archive 가 없으므로 분기 1회 `archive/` 를 61 `cache_v2.1_cash_1d_archive/` 로 rsync 권고(후속) | revisions 15KB/일(변경 0일)~수백KB/일, 월 factor 7.4MB, master 0.5MB/일 → ≈ 0.3GB/년 |

### 11-4. 정책 권고

- `--strict-exposure`(D2): 기본 **끔**(경고 코드 9 + 발행). 근거: 미확정은 라이브 북 노출 0 이 정상 상태(rc2f-E Y3)이고 새 미확정이 보유 종목에 생기면 그날 데이터 자체는 맞으므로(가격·계수) 발행을 막기보다 경보로 사람이 §11-4 절차를 밟는 편이 안전. 라이브 사이징(주문 생성)을 이 캐시로 돌리기 시작하면 **켬** 으로 전환 권고(미확정 보유 = 손익 미확정 상태로 주문을 내지 않게).
- 엔진 `unresolved_policy`: 연구·백테스트 기본 `warn`(보고만; 회계 불변), **라이브 실행 설정은 `strict`**(첫 미확정 bar 에서 HTTP 400) — 미확정을 조용히 안고 주문을 만들지 않게. strict 가 실패시키면 §11-4 절차로 원장 교정 후 재실행.
- `unconfirmed`(엔진 `corporateActions.unconfirmed` / 캐시 `qa/exposure_daily.md` flag 3·4·NaN·조건 없는 정산) 항목 절차: ① 레지스트리(`overrides/rc2f/registry_final.csv`) 에 사건 행 추가(caseId `NEW-D-n`, sourcesChecked·finding·finalStatus) ② 근거가 있으면 새 하위 디렉토리(`overrides/rc2f/D<날짜>/…_overrides.csv`, overrideId 접두어 `D<날짜>-n`, 스키마 `BRIEF_rc2f.md` §3) 에 교정 행 append, `rc2f_apply.py validate` ③ `rm state/done_<D>` 후 래퍼 재실행(원장 증분이 교정 CSV 를 재적용, 노출 요약 재생성) ④ 자료 부족이면 `unresolved_insufficient` 로 두고 경보만 닫음(엔진에는 표시용 행/NaN 으로 전달됨).

### 11-5. 체크리스트

실행 전(첫 등록 시 1회): D2 통합 확인(aux 종목은 U2/U3 조회 집합에 포함되고 퓨즈는 instruments 행 기준 — D4 실측의 rc 4 는 해소, §12) · `ln -s <rc2f2 발행본 사본 또는 첫 daily out-stage> $ROOT/current`(발행본 입력이면 `--prev-work <stage_v21/T1/_work 사본>` 으로 calendar·lineage_overlap 을 빌려 U4 재집계·overlap 재조회를 피함; 이후 체인은 stage) · `cp -r /home/jgo/longshort/daily/orch/* $ROOT/orch/`(BRIEF/TASKS/reports + rc2/ + rc2f/; publish 가 qa/ 로 복사, 없으면 건너뜀) · `mountpoint /home/jgo/61/data2` · `readiness_probe.py --prev-build $ROOT/current` rc 0/3/4 확인 · `update_daily.sh --dry-run` 1회 · `crontab.example` 등록 · `NYSE_HOLIDAYS` 다음 해 추가 · publish 경로는 D2 에서 내구화(`--work-root`, `--orch*` 파생; 스크래치패드 의존 없음) · `EXPOSURE_WARN_RC=9`(D2 확정, `--exposure-warn-rc`).
매일 후: `state/last_success.json` asOf == 어제 세션 · `alerts/LATEST` 없음 · `logs/update_<id>.log` 게이트 표 · `state/exposure_<D>.md` 보유 교차 0 · 61 `manifest.json` build_id/asOfDate · `df -h /home`(빌드 ≥ 10GB 여유).
주 1회: 토요일 `--full-lifecycle` 성공 · prune dry-run 계획 확인 · `cron.log` 의 wait/skip 패턴(EDT 16:10, EST 17:10 에서 실행됐는지).
DST 전환 뒤(11-02, 03-15): 첫 3일 실행 슬롯이 17:10 으로 밀렸는지, 프로브 DETAIL_* 의 적재 시각이 +1h 인지 확인.

## 12. rc2f 계약의 일별 유지 (D2, 2026-09-21; `update_daily_rc2f.py`)

고정 스냅샷 rc2f2(발행 `20260921T0119Z-rc2f2`)의 계약 = 벤더 표 + 원천 교정(`overrides/rc2f/**`) + 규칙. 일별 빌드는 벤더 층(raw 창·계수·lifecycle·lineage·events·원장)을 다시 만들므로 교정을 **매일 새 표에 재적용**하고(멱등: 그날의 적용 로그를 이전 빌드 로그와 대조), 이전 빌드 대비 바뀐 returns 셀은 **전부 원인에 귀속**돼야 한다(rc2f_recount 규칙). 테스트 `tests/test_update_daily_rc2f.py`(합성 fixture 7건, DB 없음).

### 12-1. 입력·옵션

| 옵션 | 기본 | 뜻 |
|---|---|---|
| `--overrides-glob` | `cache_v21/overrides/rc2f/*/*.csv` | 재적용할 교정 CSV(X1~X5 + 새 하위 디렉토리 `D<날짜>/`). 파일 sha 는 BUILD.json `manifest.rc2f.overrides` |
| `--ledger-mode auto|skip`, `--ledger-prev` | auto, 없음 | U-L 원장 증분(D1) 호출(`--prev` = 전일 `T6`). **발행본을 `--prev-build` 로 쓰는 첫 빌드는 `--ledger-prev <rc2f R1 스테이지>`(`/home/jgo/longshort/rc2f_L2/build/R1`, apply 인계 파일 `handover/` 보유) 필수**(없으면 인자 오류; `--ledger`/`--ledger-mode skip` 이면 제외, R-D2 P1-6). `--ledger <path>` 를 주면 그 원장(sidecar asOf 로 stale 판정), `--allow-stale-ledger` 는 꼬리 retFlag 4 |
| `--work-root` | `/home/jgo/longshort/daily` | `--out-stage`(`builds/<id>`)·`--publish-out`(`published/<id>`)·`--orch`(`orch/`) 기본 위치. 세션 스크래치패드(`/tmp/…`) 의존 없음 |
| `--orch`, `--orch-rc2`, `--orch-rc2f` | `<work-root>/orch`, `<orch>/rc2`, `<orch>/rc2f` | publish 가 qa/ 로 복사하는 브리프·보고서(없으면 건너뜀). 내구 사본 `/home/jgo/longshort/daily/orch/` |
| `--prev-work` | 없음 | 이전 빌드가 발행본일 때 stage `T1/_work`(calendar 1968+·lineage_overlap·identity) 를 빌려 overlap 재조회를 피함(없으면 발행본 `calendar.parquet` 의 거래일(2000+) 사용, P2-6) |
| `--raw-change-policy keep|abort`, `--accept-raw-change <ids>`, `--strict-raw` | keep | §12-3 |
| `--strict-exposure`, `--exposure-warn-rc` | 끔, 9 | §12-5 |
| `--book-glob` | combo000014 `*_pos_values.parquet` | 라이브 북(읽기 전용, 최신 파일의 마지막 북 날짜 ≤ asOf+7d) |
| `--no-returns-qa-vendor` | (계산함) | returns_v2_qa G-VENDOR 기준(rc1 벤더식 수익률 재계산, 약 1분) 생략 |
| `--co-acthist-max-lag-days` | 7 | G-SOURCE |
| `--mi-lag-tdays` | 2 | finalize: active 종목의 lastTradeDate 가 ciq 보다 ≤N 거래일 늦은 miprice 에서 왔으면 ciq 마지막 종가 유지(`lastTradeSource` `ciq(mi-lag)`; 0 = T1 규칙만) |
| `--full-refetch` | 끔 | U3: 전 종목 ciqpriceequity 전 이력 재조회(주 1회 스윕; D3 실측: 일별 재조회 집합 밖의 벤더 재기술 3,725셀·삭제 38행은 이 스윕에서만 드러남 → price_revisions/G-RAWINV 로 기록). 전량 13분·메모리 +10GB 안팎; 토요일 `--full-lifecycle` 슬롯에 같이 권고 |
| `--events-delta-max`, `--ret-abs-max` | 100, 50 | G-EVENTS·G-RET-OUTLIER 임계 |

### 12-2. 교정 재적용 규칙 (kind 별)

| kind | 어디서 | 규칙 | 로그 status |
|---|---|---|---|
| price | U2/U3 | 저장 행은 이미 교정값(qualityFlag 8/32) → 그대로 유지(재기술 종목의 전 이력 재조회도 공통 행은 저장값). 창·재조회에서 **새로 온 행**이 교정 창 안이면 multiply/set_close + flag 8/32. raw 불변 검사는 교정 창 행을 제외. G-PRICE-OVR 가 창 안 전 행의 flag 를 확인 | applied(새 행) / carried |
| split | U3 | 벤더 표 재구축(splitcache + reconcile) → `T2_work/split_vendor.parquet` 로 보존 → `rc2f_apply.apply_split_overrides`(3상태 raw 점프 검사 포함) → `T2/factors/split.parquet`. G-SPLIT(prices_qa)·returns_v2_qa `--vendor-factors` 는 벤더 표 기준 | applied / applied_unverified / applied_unchecked / no_op(confirm) / rejected |
| lineage | U4 | 이전 빌드 lineage(v2+교정) 행 carry + 그날 v1 후보의 새 쌍 append(version `v1-daily`, ratio NaN → chainable False = 첫 행 NaN). chainGapTradingDays 는 매일 재계산, chainable 은 교정이 손대지 않은 행만 R3 공식으로. 교정은 현재 표 기준 멱등: drop = 쌍 있으면 적용/없으면 carried, add = rc2f 행 있으면 carried·v1 쌍이면 교체, set_ratio = ratioSource 가 `:<id>` 면 carried·쌍 없으면 rejected, set_chainable = 값 같으면 carried | applied / carried / applied_no_effect / rejected |
| lifecycle | U5 finalize | **종료된 id(confirmed/provisional)의 가격·식별 입력(ciq/mi 마지막 종가·securityEndDate·tradingItemStatusId)이 안 바뀌었으면 lastTradeDate/status/endReason/eventEndDate 를 이전 빌드에서 carry**(마커 원천 차이만으로는 재심하지 않음; 신선한 판정과 다른 것은 `T1/qa/lifecycle_end_dates_carried.csv`). **events 는 이전 빌드 행을 전부 carry 하고 델타 id 만** 그날 T6 행으로 바꾼다(델타 = 새로 종료된 id ∪ lifecycle 종료(lastTradeDate/status)가 바뀐 id ∪ 되살아난 id, 교정 id 제외; 채택 안 된 T6 차이는 `T6/qa/events_transitions.csv` kind not_adopted 로 목록 — 스냅샷 재심 금지, R-D2 P0-2). 델타 id 는 최종 events 로 다시 classify(당일 마커), active 종목의 mi 지연(`--mi-lag-tdays` 2)은 ciq 마지막 종가 유지. 그 위에 그날 U4 lifecycle(instruments, 교정 전)·carry 한 lifecycle_aux(마지막 종가 갱신)·재구축한 lifecycle_dates·U4 lineage 에 `rc2f_apply.apply_lifecycle_overrides` 재적용(교정 id 의 events 행은 carry 된 rc2f 패치본 유지, 재패치 없음 → reviewFlag 누적 없음). firstTradeDate/firstPriceDate/firstTradeSource 는 입력(ciq 첫 종가·계보 옛 id 종료일·델타/신규 여부)이 안 바뀐 id 는 이전 빌드 값 carry, 그 뒤 R3 `lineage_v2_first_date_patch`(aux 선행 링크점, TEVA) 재적용(P1-1); lineage 의 chainGapTradingDays/chainable 도 두 입력(옛 id 종료일·새 id 첫 거래일)이 안 바뀐 쌍은 carry(스냅샷은 다른 거래일 목록으로 계산됨). aux 행 append → `T1/master/lifecycle.parquet`, aux id 는 tradable_exclusions(reason aux_instrument) | applied / carried |
| ledger | U-L | 원장 빌더(D1)가 `--overrides-glob` 으로 재적용; 결과 `T6/events/ca_ledger_csv_overrides.csv` 를 일별 로그에 합침(빌더가 안 돈 날 = carried) | (빌더 status) |
| aux | U2 전 | `aux_additions.csv` 의 id 가 이미 빌드에 있으면 carried; 새 id 면 `apply_aux_additions`(xfeed 읽기) 로 가격·계수·lifecycle_aux 수집 → 병합(실패 = rejected = G-OVR FAIL, 조용한 무시 없음) | carried / applied / rejected |
| RULE-X4-scope | U5 | `label_settlement_scope` 를 그날 원장으로 재평가(라벨 유지 행은 carried) | applied / carried |

G-OVR = 그날 로그 vs 이전 빌드 로그(`qa/rc2f/rc2f_apply_log.csv` 또는 stage `rc2f/`): rejected 0·회귀 0·누락 0·교정 CSV 파싱 오류 0 이 통과. applied→no_op 같은 상태 변화는 목록(경고)이며 그 id 의 셀은 귀속 창이 된다. lineage 의 텍스트 컬럼(evidence/version/sources/reviewFlag)은 값 컬럼이 이전 빌드와 같은 행에서 이전 문자열을 복원해 재적용이 문자열을 누적시키지 않는다(멱등, P1-2).

### 12-3. 벤더 재기술(raw 불변 위반)의 처리 — raw 와 계수는 한 기준

새 역분할 뒤 벤더가 전 이력 adjustmentfactor 를 재기술하면 raw(=price×adjf) 가 바뀔 수 있다(실측 2026-09-16 빌드: 5 종목 — OHLC 는 불변이고 vwap 만 반올림 드리프트 4 종목, JAGX 533895898 은 2017-08~2018-05 OHLC 가 ×1e10 = 정밀도 바닥 단위 오류, rc2f X1 가격 교정과 같은 부류). 기본(`keep`): **raw 와 split 계수를 함께 저장값으로 유지** — OHLC 가 바뀐 id 는 이전 빌드의 구간 표를 carry 하고(벤더 경계 중 이전 asOf 이후 것만 `_split_one` add 로 덧붙여 새 분할은 잃지 않음, source `|daily_kept_add`), side table 행은 carry 한 구간의 lookup 으로 채우며, 그 id 의 split 교정은 status carried; 벤더 값은 `price_revisions`(`vendor_restated_kept`)·`factor_revisions`(kind `vendor_not_adopted:*`)·`T2/qa/raw_invariance_violations.csv` 에 기록만(G-RAWINV 목록, `--strict-raw` 면 차단). vwap 만 다른 id 는 raw OHLC 가 불변이므로 벤더 계수를 그대로 쓴다. 벤더 정정이 맞으면 `--accept-raw-change <id,…>` 로 raw+계수 채택(`vendor_restated_accepted`, 행 source 2, 교정 창 행은 교정 재적용), 단위 오류면 레지스트리 사건 + `overrides/rc2f/D<날짜>/price_overrides.csv`(등록 뒤에는 교정 창이 검사에서 빠져 정상 경로로 돌아옴). `--raw-change-policy abort` 는 T9 규칙(코드 5). 값 건전성 게이트(R-D2 P0-1): **G-FACTOR-JUMP**(그날 추가·변경된 split 경계의 raw 점프 × parentFactor, |log|>log 4 차단)·**G-RET-OUTLIER**(이전 asOf 이전 날짜의 셀이 새로 |log(1+ret)|>log 4 가 되거나 어디든 |ret|>`--ret-abs-max`(50) 이면 차단; 새 날짜의 4배 이상 이동은 목록).

### 12-4. 변화 셀 귀속 (G-RET-ATTR) — 좁은 창

이전 빌드 returns(발행본이면 lifecycle 마스킹된 것; 새 빌드도 그날 lifecycle·캘린더로 마스킹해 대조) 와 새 returns 의 차이(값 |Δ|>1e-9·flag·행 추가/삭제, `rc2f_recount.returns_diff`) 를 다음 창에 귀속하고, 어디에도 안 잡히면 FAIL: new_date(이전 asOf 뒤 날짜의 추가 행) · price_revision(정정·지연·삭제 행과 그 id 의 다음 관측 — 재조회 창이 수익률을 바꾸는 유일한 경로) · factor_revision(split 경계 ±10일) · ledger_revision(원장 diff 행: exDate ~ 첫 관측 +10일, keeper/stitched id·계보 새 id 링크 행 포함; D1 revisions 포함) · override_status(상태 바뀐/새 교정의 id) · lineage(쌍 추가/삭제/ratio·chainable·효력일 변화 ±45일, 옛 id 마지막일) · lifecycle(lastTradeDate 이동: 옛 날짜~새 날짜 사이 행만; firstTradeDate 이동: 첫 행 주변; status/endReason 변화는 창 없음) · new_id · aux_new · stale_cut. "날짜 ≥ 창 시작이면 무조건" 창은 없다(R-D2 P1-5). 같은 실행이 **G-DOUBLE-LEG**(옛 id 와 계보 새 id 에 같은 (child, ratio) 분배 leg 가 ±3일 안에 둘 다 있으면 차단, P0-2) 와 **G-EVENTS**(채택된 events 델타 행 수 ≤ `--events-delta-max` 100, 채택 안 된 T6 차이 수·조건 없는 정산 증감 양방향 기록) 를 낸다. 산출 `T2/qa/returns_attr_diff.parquet`(attributedTo;how)·`returns_attr_unattributed.csv`·`returns_attr_ledger_diff.csv`·`T6/qa/events_transitions.csv`.

### 12-5. 노출 요약 (`qa/exposure_daily.md`, G-EXPO)

(1) 새 asOf 행의 retFlag 분포·flag 3/4/NaN 목록(`exposure_asof_flagged.csv`, Top500 표시) (2) 조건 없는 정산(events settlementReturn NaN·승계사 없음) 중 이전 빌드에 없던 것(`exposure_new_unconditioned_settlements.csv`) (3) 엔진 export 표시용 행(markerReason) 중 이전 export 에 없던 것(`exposure_display_rows.csv`) (4) 라이브 북 최신 파일의 마지막 북 날짜 보유 종목과 (1)(2)(3)·lastTradeDate 이후 보유·asOf 행 |ret|>100% 교차(`exposure_book_hits.csv`). 조건 없는 정산은 신규·소멸 양방향으로 세고 relist/rename 승계가 있는 행은 `lineageSuccessor` 로 표시한다. 교차가 있으면 경고(종료코드 `--exposure-warn-rc` 9, 발행은 됨); `--strict-exposure` 면 G-EXPO blocking(코드 7). 절차는 §11-4.

### 12-6. 실측 (2026-09-21 재작업 후, `/home/jgo/longshort/daily_D2/`, 보고서 `SP/orch_daily/reports/D2.md` §13)

rc2f2 발행본 사본 → 09-16(발행본 입력, `--prev-work` + `--ledger-prev R1`) → 09-17 → 09-17 재실행(같은 입력) → 09-18 체인 전부 rc 0, blocking 게이트 전부 PASS, 발행 드라이런(앵커 검사 PASS). 같은 입력 2회 = 계약 파일 sha256 38/38 동일(lineage 포함). 본체 12.4~20.4분(첫 빌드는 aux 148 종목 전 이력 재조회 포함; T5 6.7분·T6 3분·원장 증분 3분·returns_v2_qa 75초), peak RSS 8.1~8.3GB, 스테이지 2.2GB/일·발행본 1.5GB/일. 스냅샷→일별 전환(첫 빌드 vs rc2f2): events 행 변경 0(새 종료 5), firstTradeDate 변경 0, lineage 변경 0(최근 새 쌍 2), 종료 판정 carry 6,937(마커 원천 차이 목록), 실제 lifecycle 전이 endReason 22·status 21; 변화 셀 12,428 전부 귀속(new_date 7,289·새 id 4,553·원장 467·lineage 61·factor 52·price 6). 이후 날: 변화 셀 = new_date(+price 4). 교정 로그 3일 동일(applied 1,538·no_op 953·carried 830·unverified 22·unchecked 19, rejected 0). 발행본 09-18 returns retFlag 4 = 350(rc2f2 350, 셀 추가·소멸 0), JAGX 2018-06-01 = rc2f2 값. 비차단 FAIL: G-RAWINV(JAGX 533895898 단위 오류 매일, raw+계수 carry), G-SOURCE(co_acthist 2026-02-19), 09-16 G-DIV(asOf 이후 단계 79 제외 뒤 잔여).

### 12-7. BUILD.json 기록 (`rc2f.*`, `results.manifest.rc2f`)

`prepare`(aux id 수, 교정 파일 수·행), `U4`(lineage 병합), `ledger`(D1 rc·asOf·rows·sha·revisions 수), `source_freshness`(co_acthist 마지막 datadate·지연일·대체 원천), `finalize`(events 병합·lifecycle 교정·status 전이·lifecycle_revisions·scope 라벨), `returns_qa`, `returns_attribution`, `engine_export_stage`(표시용 행 수), `exposure`, `publish`(발행본 export sha·앵커 검사), `manifest`(원장 asOf·sha, 교정 파일 sha 목록·전체 sha, links_verified/chainable, 표시용 행, 그날 revisions 파일별 행 수, 적용 로그 status 분포).
