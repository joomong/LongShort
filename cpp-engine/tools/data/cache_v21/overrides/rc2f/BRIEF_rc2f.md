# rc2f 브리프 — rc2 를 "과거 데이터가 최대한 정리된 고정 스냅샷" 으로 완성

리드: 이 세션. 목표(사용자 확정): 다섯 예외 목록(미해결 수익률 584행 · 분할 인공물 95+9 · Lionsgate 분배비율 · 원장 공백 후보 261 · 이벤트 종료일 171/184)을
**하나의 사건 레지스트리**로 합쳐 사건 단위로 조사하고, 고칠 수 있는 것은 **원천 수준**(가격·계수·원장·종료일·계보)에서 고친 뒤 전체 수익률을 재생성,
남는 것만 정확한 사유와 함께 유지. 584행을 억지로 0 으로 만드는 것이 목표가 아님. 일별 갱신·라이브 사이징은 이 스냅샷 검증 뒤.

경로: SP = `/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad`
- rc2 발행 빌드(읽기 전용, 수정 금지): `SP/build_rc2_final/` (= 61 `temp_stock/cache_v2.1_cash_1d` 20260917T0530Z-rc2 와 동일). 61 발행본도 수정 금지.
- rc2 스테이지(읽기 전용): `SP/stage_rc2/{R1,R2,R3,V1,V2}` (R1/work 에 xfeed 캐시 parquet: work_ciqsplit·work_ciqsplitcache·work_ciqcurrency·anchor_closes; R3/work 에 개명 증거).
- rc2 브리프·보고서(계약·정책 12항·스키마): `SP/orch_rc2/BRIEF.md`, `SP/orch_rc2/reports/*.md`. 문서 `~/longshort_platform/docs/cache_v21_build_2026-09-16.md` §9.
- 코드: `~/longshort_platform/cpp-engine/tools/data/cache_v21/` (python `/home/jgo/usr/venv-3.11/bin/python`; 커밋 금지).
- 레지스트리 v0: `SP/orch_rc2f/registry/` — `registry_cases_v0.csv`(4,519 사건), 담당별 `cases_<CLASS>.csv`, 행 단위 `registry_rows_v0.csv`, `flag4_rows_with_cause.csv`(584행 전부 원인 연결됨: split 230 · spin 135 · link 130 · rights 64 · other 24 · warrant 1).
- 각 태스크의 작업 폴더: `SP/stage_rc2f/<TASK>/` (자유), 교정 CSV 는 `SP/stage_rc2f/<TASK>/overrides/`, 보고서 `SP/orch_rc2f/reports/<TASK>.md`.

## 1. 공통 규칙(위반 = 리뷰 FAIL)
1. GS DB(`/home/base/.secrets`) 접근 금지. xfeed 는 `cache_v21/db.py` 의 읽기 전용 세션만(`tradingitemid = any(%s)` 청크 ≤ 1000, 큰 테이블 전체 스캔 금지, 태스크당 동시 쿼리 ≤ 2, `wait_for_quiet` 사용). 테이블: ciqpriceequity·miprice·ciqdividend·ciqdividendcache·ciqsplit·ciqsplitcache·ciqpriceequitydivadjfactor·co_acthist·ciqsecurity·ciqtradingitem·ciqcompany·sec_idhist·ciqmarketcap·ciqcurrency·ciqexchangerate(스키마 `xfl_target`).
2. 공개 문서(SEC EDGAR·회사 IR·거래소 공지)는 WebFetch/WebSearch 허용 — 검색어에는 회사명·티커·사건명만, 내부 데이터 값 전송 금지. 근거는 URL + 인용 문장을 레지스트리에 기록. 검색 결과 요약만으로 확정 금지(원문 확인).
3. rc2 빌드·스테이지·61 발행본·`/tmp/longshort_module_server/build` 수정 금지, pkill 금지. 산출물은 자기 폴더에만.
4. **순환 검증 금지**: 벤더 계수에서 추정한 값을 같은 계수로 검증하지 않음. 벤더 계수 스텝만 있는 것은 배당·분할의 증거가 아님. 원장 공백을 0 으로 채우지 않음.
5. 가격 그래프가 매끄러워지는 방향을 정답으로 삼지 않음. 분할은 실제 주수 변화(ciqsplit 조건·co_acthist 문구·sharesOutstanding(`derived/marketcap.parquet`) 점프·거래량 단위 변화·MI 가격) 와 가격 단위를 함께 확인.
6. 미래 가격 사용 금지(사건일 이후 정보로 사건일 값을 만들지 않음). 추정은 근거와 함께 `estimate_evidence` 로만.
7. Top500/Top1000 은 조사 **순서**에만 씀(P0 → P1 → P1b → P2). 제외 기준으로 쓰지 않음. 시간이 모자라면 남은 사건을 `not_investigated` 로 **명시**하고 개수를 보고(조용히 빠뜨리지 않음).
8. 실행 중 발견한 새 예외(목록에 없던 것)는 레지스트리에 새 행으로 추가(caseId `NEW-<TASK>-n`).
9. 코드 변경은 X0 만(공통 파이프라인). 조사 태스크는 코드 대신 **교정 CSV** 를 산출(§3 스키마). 자기 분석 스크립트는 자기 폴더에.
10. 보고서는 번호 섹션·표, 숫자는 스크립트 출력에서 복사(추정치는 `(추정)` 표시).

## 2. 사건 최종 상태(세 가지 + 미조사)
| finalStatus | 뜻 | 처리 |
|---|---|---|
| `resolved_evidence` | 근거로 원천 교정(교정 CSV 행 있음) | 정상 데이터 |
| `confirmed_normal` | 조사 결과 데이터가 맞음(실제 가격 급변·실제 무배당·정상 종료 등) | 정상 데이터, 교정 없음(근거 기록) |
| `estimate_evidence` | 근거 있는 추정만 가능(교정 CSV 행에 `isEstimate=true`) | 추정 표시(retFlag 3 / status estimated) |
| `unresolved_insufficient` | 자료 부족 | 원본 보존·NaN·retFlag 4, `residualReason` 필수 |
| `not_investigated` | 시간 부족으로 미조사 | 보고서에 개수·목록 |

레지스트리 필수 컬럼(사건마다): `tradingItemId, symbol, eventDate, impactFrom, impactTo, primaryClass, lists, sourcesChecked, finding, fix, residualReason, finalStatus, overrideRows(교정 CSV 행 id 들)`.

## 3. 교정 CSV 스키마(`SP/stage_rc2f/<TASK>/overrides/`; X0 가 적용기 구현·검증)
공통 컬럼: `overrideId(<TASK>-n), caseId, tradingItemId, evidence, source, isEstimate(true/false), owner`.
| 파일 | 추가 컬럼 | 의미 |
|---|---|---|
| `split_overrides.csv` | `exDate, action ∈ {drop, set_factor, move_date, add, confirm}, newFactor, newExDate` | `factors/split.parquet` 구간 경계(=원장 split 행) 교정. `drop`: 벤더 스텝이 인공물 → 경계 제거(구간 병합). `set_factor`: 실제 분할비로(newFactor = 신주/구주, 예 1:5 역분할 0.2). `move_date`: 효력일 이동. `add`: 누락 분할 추가. `confirm`: 변경 없음(근거만). raw 복원(prices_build 의 벤더 adjustmentfactor)은 건드리지 않음 |
| `price_overrides.csv` | `dateFrom, dateTo, op ∈ {multiply, set_close}, value, fields(기본 close,open,high,low,vwap)` | 검증된 **단위 오류**만(예: 분할 후 N 일간 옛 단위). base_data 에 적용 + qualityFlag 비트. 거래량은 건드리지 않음 |
| `ledger_overrides.csv` | `eventId(우선) 또는 match_tradingItemId,match_exDate,match_legType,match_childTradingItemId; action ∈ {set, drop, add}; set(JSON: childRatio, childTradingItemId, childValuePerOldShare, childValueSource, cashPerShare, cashCurrency, eventType, legType, status, isEstimate, exDate, payDate, subscriptionPrice, expiryDate, exerciseRatio, notes)` | 원장 leg 교정/삭제/추가. `add` 는 set JSON 에 필요한 전 필드. status 는 §2 상태에 맞게(`ok`/`estimated`/`unresolved`) |
| `lifecycle_overrides.csv` | `lastTradeDate, effectiveDate, settlementDate, payDate, endReason ∈ {cash_merger, stock_merger, mixed_merger, id_change, halt_suspension, bankruptcy, liquidation, data_gap, delist_other, still_trading}, successorTradingItemId, successorRatio, cashPerShare, treatment ∈ {settle_cash, convert_successor, settle_mixed, last_close, unresolved, extend_prices}` | `lifecycle_dates` + `events/corporate_actions`(settlementReturn·cashPerShare·successor) + 가격 꼬리 컷 + lineage 반영. `extend_prices` 는 lastTradeDate 가 실제로 더 뒤(데이터 결손)일 때 — 근거 필수 |
| `lineage_overrides.csv` | `oldId, newId, action ∈ {set_ratio, drop, add, set_chainable}, ratio, effectiveDate, relation, chainable` | 계보 링크 교정 |

## 4. 태스크(웨이브 1 병렬 6개 → 웨이브 2 독립 리뷰 → 웨이브 3 통합·재생성·회귀·발행)
### X0 파이프라인(코드; opus, effort LOW) — P0
`cache_v21/rc2f_apply.py`(교정 적용기) + `cache_v21/rc2f_build.sh`(재생성 드라이버) + `cache_v21/rc2f_recount.py`(재집계) + 테스트 `tests/test_rc2f_apply.py`.
- 입력: rc2 빌드(`SP/build_rc2_final`), rc2 스테이지, `SP/stage_rc2f/*/overrides/*.csv`(여러 태스크 폴더를 모두 모음; overrideId 유일·스키마 검증·같은 대상 중복 교정은 오류).
- 적용 순서: price → factors/split(원장 split 행은 ledger_build 가 factors/split 경계에서 재생성하므로 교정된 factors 를 ledger_build `--root` 에 공급) → lifecycle/events/lineage → `ledger_build.py`(교정 CSV 를 읽는 `--overrides` 옵션 추가; MANUAL_OVERRIDES 는 유지) → `prices_returns.py`(v2) → `loader.export_ca_ledger` → `ledger_qa.py`·`returns_v2_qa.py` 게이트 → `rc2f_recount.py`.
- 출력: `SP/stage_rc2f/build/`(publish.py `--rc2-stage` 가 읽는 R1/R2/R3 레이아웃 그대로: `R1/events/ca_ledger.parquet`+sidecar+`R1/engine/…`, `R3/master/*`, `R3/events/corporate_actions_reclass.parquet`, `R3/factors_aux`, `R3/base_data_aux.parquet`; 바뀐 factors/split·base_data 는 `SP/stage_rc2f/build/T2/` 같은 자리에 두고 publish.py 가 그것을 쓰도록 `--stage` 인자 확장) + `rc2f_apply_log.csv`(교정마다 적용 결과·영향 행 수).
- 게이트(필수): ① 교정 CSV 가 비어 있으면 rc2 와 **비트 동일**(returns_total·ca_ledger·factors/split·corporate_actions·lifecycle_dates·lineage 전부, 실데이터로 실측) ② 합성 교정 각 종류 1건씩 → 기대 결과(원장 행·returns 셀·factors 구간·events 행) 테스트 ③ 적용 로그에 모든 overrideId 가 applied/rejected 로 나타남(조용한 무시 0).
- `rc2f_recount.py`: 레지스트리 v0 의 사건마다 재생성 후 상태(flag 4 잔여 여부, 원장 status, 변화한 셀) 를 붙여 `registry_after.csv` 와 요약(584행 → 잔여 n, 원인별) 출력. rc2 대비 바뀐 returns 셀은 **전부** 어떤 overrideId/caseId 에 귀속돼야 함(귀속 안 되는 변화 = FAIL).
- 시작 즉시 §3 스키마 검증기(`rc2f_apply.py validate <csv>`) 를 먼저 만들어 다른 태스크가 쓰게 할 것(1시간 내).

### X1 SPLIT(조사; opus, effort MEDIUM) — `cases_SPLIT.csv` 1,633 사건(P0 11 · P1 17 · P1b 321 · P2 1,284)
- 1차(P0~P1b 349): 분할 인공물 97+9·원장 split unresolved 235·split_step_rows·G-JUMP·split boundary. 사건마다 ciqsplit 조건(rate·type·exDate)·co_acthist 문구·raw 종가 전후(±10 거래일)·MI 가격·sharesOutstanding·거래량 단위 대조 → 판정 4 종: 가격 문제(price_overrides) / 계수 문제(split_overrides drop·set_factor) / 날짜 문제(move_date) / 실제 급변(confirm, confirmed_normal).
- 2차(P2 1,284 = estimated 968 + unquoted 411): 스크립트 일괄 검사(계수 vs sharesOutstanding·거래량·MI·가격 갭 전후 레벨 연속성) → 일치는 `confirmed_normal`(근거=일괄 검사 값), 불일치만 개별 조사. "값 없음" 과 "정상" 을 구분.
- 산출: 레지스트리, `overrides/split_overrides.csv`·`price_overrides.csv`, 판정 규칙·검사 스크립트, 보고서(판정 분포표, 교정이 returns 에 미칠 셀 수 추정).

### X2 DIST(조사; opus, effort MEDIUM) — `cases_DIST.csv` 565 사건(P0 57 · P1 37 · P1b 253 · P2 218)
- 원장 미해결 spin_off 156·rights 64·other 33·cash 10·warrant 1, 비현실 spin leg 22, 정산성 현금 31, 대형 분배·5pp 차이·retFlag 3/4 Top500 known issues, estimated vendor_implied spin 155(가치 원천 승격 가능 여부).
- **Lionsgate 412297487 2025-05-07 STRZ leg 는 개별 사건으로 끝까지**: 공식 분리 문서(Form 10/8-K/보도자료)에서 옛 주식 종류별 교부 증권·구주 1주당 수량·같은 날 병합/교환·단주·효력일 확인, 클래스별 조건이 다르면 행 분리, "구주 1주 → 최종 무엇" 표 작성 후 엔진 단위 환산(X0 export 식). 추정비율 0.0747 을 가격 차이로 역산해 확정하지 않음.
- rights/warrant: 조건(구주당 권리 수·행사가·만기·상장 여부) 을 ciqdividend/co_acthist/공시에서 확보 → 가치 = 상장 권리 종가(①) 또는 (기초 종가−행사가)+×비율(③, estimate) — 조건 없으면 unresolved + 사유.
- spin/other: P10 우선순위(① 자회사 exDate 종가×비율 ② W/I ③ 사건 근거 ④ 벤더 implied) 로 승격 가능한지, 자회사 id 매칭 오류(TYC 류)·비율 단위(구주/신주) 확인. 대형 분배·정산성 현금은 "배당 vs 합병 대가" 구분.
- 산출: 레지스트리, `overrides/ledger_overrides.csv`, 보고서(사건 유형 × 최종 상태 표, Lionsgate 절).

### X3 GAP(조사; opus, effort MEDIUM) — `cases_GAP.csv` 377 사건(gap261 + vendor_implied 현금 117 + 마커 22)
- 후보마다: ciqdividend(±30일, 타입 전부) → 같은 회사 다른 id(lineage·sec_idhist) 의 사건 → 외화 배당(ciqcurrency·환율) → 주식배당·권리(ciqsplit type, co_acthist) → 같은 날 합병 대가(events) → ciqdividendcache/공시 순으로 확인.
- 판정: 실제 사건 확인 → `ledger_overrides add`(status ok, 세전) / id·단위 오류 → 기존 leg `set` / 벤더 계수 오류 확인 → 배당을 만들지 않고 `confirmed_normal`(사유 vendor_factor_error, 계수는 교차검증용이므로 교정 불필요) / 근거 없음 → vendor_implied 행은 `estimate_evidence` 유지 가능 여부 재판단(근거 없는 추정은 unresolved 로 강등 = `set status=unresolved`).
- 산출: 레지스트리, `overrides/ledger_overrides.csv`, 보고서(원천별 발견 표).

### X4 ENDDATE(조사; opus, effort MEDIUM) — `cases_ENDDATE.csv` 184 + 원장 merger_stock unresolved 665(`cases_LINK.csv` 의 `ledger_unresolved: merger_stock` 행; X5 와 겹치지 않게 X4 가 소유)
- 사건마다 세 날짜(마지막 거래일·효력일·지급/정산일) 를 **독립 근거**로: ciqsecurity.securityenddate·ciqtradingitem 상태·ciqpriceequity/miprice 마지막 실제 거래(거래량>0)·co_acthist(효력·지급)·events·공시. 가격을 자른 날짜로 다시 종료일을 검증하지 않음.
- 유형 분류: 현금 합병 / 주식 교환 / 혼합 / 단순 id 변경 / 거래정지 / 파산·청산 / 데이터 결손. 주식 교환은 승계사 id·비율로 연결(lineage 또는 events successor). 거래가 끝났어도 정산 권리는 남음 → `treatment` 결정(settle_cash·convert_successor·last_close·unresolved·extend_prices). 정지 상태면 "보유 시 명시적으로 감지" 되도록 unresolved + settlementDate 없음.
- merger_stock unresolved 665: 조건(acquirer·비율·현금) 을 co_acthist 문구·ciqdividend 타입·공시에서 복원 시도 → `lifecycle_overrides`(treatment) 로. 못 찾으면 `last_close` 유지가 타당한지(마지막 거래일 종가가 stale/정지가 아닌지) 만 확인해 `confirmed_normal(last_close)` 또는 `unresolved`.
- 산출: 레지스트리, `overrides/lifecycle_overrides.csv`(+필요 시 `ledger_overrides.csv` merger 현금/주식 leg), 보고서(유형 × 처리 표, 종료일 이동 분포).

### X5 LINK(조사; opus, effort MEDIUM) — `cases_LINK.csv` 중 merger_stock 제외(link_suspect 194 · relist/rename unresolved 75 · stitched 15+4 · known_issues 10 · flag4 LINK 130 · estimated relist/rename 870 은 2차 일괄)
- link_implausible 129: 옛 id 마지막 종가(원가격 인공물?)·변환비(lineage ratio vs ciqsplit)·갭 사건 누락(DISCK 0.5주 류)·overlap 판정 → 교정은 `lineage_overrides`(ratio/drop/chainable) 또는 `price_overrides`(옛 id 꼬리 원가격 단위 오류) 또는 `ledger_overrides`(누락 leg add).
- estimated relist/rename 870(ratio 1.0 가정): 일괄 검사(옛 id 마지막 종가 vs 새 id 첫 종가 × ratio, ciqsplit·sec_idhist 로 비율 근거) → 근거 있으면 ratio 확정(`set_ratio`, isEstimate false), 아니면 유지.
- 산출: 레지스트리, `overrides/lineage_overrides.csv`(+price/ledger), 보고서.

## 5. 리뷰 프로토콜(웨이브 2, opus HIGH 독립)
- 조사 태스크: P0 전수 + P1 전수 + P1b/P2 무작위 10%(seed 기록) 재조사 — 근거 재확인(원문/쿼리 재실행), 순환 검증·미래 정보·Top500 제외 위반, 교정 CSV 스키마·중복·부호·단위(구주/신주, split_adj/raw) 검증, `confirmed_normal` 의 근거 적정성. 판정 P0(교정이 틀림/근거 없음) · P1(사유 부실) · P2(문구). FAIL 이면 SendMessage 로 재작업.
- X0: 코드 통독, 빈 교정 비트 동일 실측 재현, 합성 교정 테스트, 귀속 게이트.
- 리드 최종: 통합 재생성 → 게이트 전부 → V1 하니스(엔진, 별도 빌드) → V2 회귀(DD·오라클 14·rc2 대비 변화 셀 전수 귀속) → 발행(rc2f) → 문서·메모리.
