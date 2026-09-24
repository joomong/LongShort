# rc2f-E 브리프 — 남은 불확실성을 엔진·백테스트가 빠짐없이 인식하게 (2026-09-21)

리드: 이 세션. 배경: rc2f 발행(`20260919T0144Z-rc2f`, 61 `temp_stock/cache_v2.1_cash_1d`) 은 원천 교정·귀속·게이트를 통과했으나, 사용자 검토에서 5가지 보완이 확정됨.
목표(완료 기준을 각 태스크에 명시): ① 미해결 사건이 엔진 입력에서 사라지지 않고 보유 시 "손익 미확정" 으로 감지·표시(엄격 모드는 실패) ② `applied_unverified` 38건을 별도 근거로 닫음(통과/실패/검사 불가 3상태) ③ 추정·NaN·미해결을 포함한 전체 노출 보고서와 최종 수치 정정 ④ 고정 스냅샷으로 combo000014 전체 전략 비교(rc2 vs rc2f, 보유 중 미확정 노출 목록) → 그 다음 일별 갱신·라이브 사이징.

경로: SP = `/tmp/claude-1008/-home-jgo/3f2abe1d-a2a7-4017-9ac1-2c7cb961e311/scratchpad`
- rc2f 발행본(읽기 전용): `/home/jgo/61/data2/temp_stock/cache_v2.1_cash_1d/` (qa/rc2f/ 에 overrides·recount·registry·reports), rc2: `...cache_v2.1_cash_1d.prev-20260917T0530Z-rc2/`
- rc2f 로컬 빌드(읽기 전용): `/home/jgo/longshort/rc2f_L1/build/`(apply 스테이지 R1/R2/R3/T2, `cache/` = 발행 드라이런, `recount/`), `/home/jgo/longshort/rc2f_L1/pub_local/`(발행본과 동일)
- 교정 CSV 원본(태스크 소유, 수정은 소유 태스크만): `SP/stage_rc2f/X[1-5]/overrides/*.csv`; 레지스트리 `SP/orch_rc2f/registry/registry_final.csv`
- 브리프·규칙: `SP/orch_rc2f/BRIEF.md`(§1 규칙·§2 상태·§3 스키마), `SP/orch_rc2/BRIEF.md`(정책 12항·§3 계약), 문서 `~/longshort_platform/docs/{cache_v21_build_2026-09-16.md §9·§10, corporate_actions_engine_2026-09-17.md, rights_warrants_design_2026-09-17.md}`, `~/longshort_platform/CLAUDE.md`
- 코드: `~/longshort_platform/cpp-engine/{src,tests,tools/data/cache_v21}` (미커밋 상태 유지, 커밋 금지)
- 엔진 빌드: 자기 디렉토리만(`/tmp/longshort_rc2f_<task>/build`), cmake 옵션 `-DUUID_INCLUDE_DIRS=/home/jgo/61/jgo/miniconda3/include/uuid -DUUID_LIBRARIES=/usr/lib/x86_64-linux-gnu/libuuid.so.1 -DFETCHCONTENT_SOURCE_DIR_DROGON=/tmp/longshort_refactor_deps/drogon-1.9.8 -DFETCHCONTENT_SOURCE_DIR_GOOGLETEST=/tmp/longshort_refactor_deps/googletest-1.14.0 -DFETCHCONTENT_SOURCE_DIR_PUGIXML=/tmp/longshort_refactor_deps/pugixml-1.14`; 리드 빌드 `/tmp/longshort_rc2_lead/build` 참조만. `/tmp/longshort_module_server/build` 접근 금지, pkill 금지, 테스트 서버는 자기 포트.
- 공통 규칙은 `SP/orch_rc2f/BRIEF.md` §1 그대로(GS DB 금지, xfeed 읽기 전용 청크 ≤1000, 61 발행본·rc2 빌드 수정 금지, 순환 검증·미래 가격·0 채움 금지, Top500 은 순서에만). `/home/base/{cache,portfolio}` 는 읽기 전용 입력으로 허용(`/home/base/.secrets` 금지).
- 보고서: `SP/orch_rc2f/E/reports/<TASK>.md`(번호 섹션·표, 숫자는 스크립트 출력), 최종 응답 3줄 요약 + 산출물 경로 + 완료 기준 충족 표.

## Y1 엔진·로더 — 미해결 사건 전달 + 보유 시 감지 + 엄격 모드 (C++/python; opus, effort LOW 구현 → HIGH 리뷰)
현황: `cache_v21/loader.py export_ca_ledger`(≈1229행) 가 금액 없는 현금 leg(`no_cash`)·자회사 미확인 stock/rights leg(`no_child`) 를 export 에서 **제거하고 개수만 기록** → 엔진의 `legs_unresolved` 진단이 그 사건을 볼 수 없음. events 행의 조건 없음(settlementReturn NaN → `default_delisting_return`) 도 개수(`default_delisting_count`)만 있음.
요구:
1. export: 제거하던 두 종류를 **표시용 행**으로 내보냄(status `unresolved`, 값 NaN, child null 허용, 새 컬럼 `markerReason` ∈ {no_cash_amount, no_child, …}); 기존 제외 규칙(stitched_copy·events 중복·종료 부모 현금·stale 앵커)은 유지하되 각 제외 사유별 개수를 manifest 에. export 스키마 변경은 엔진 로더·V1 하니스·`ledger_qa` Q11~Q13 과 호환되게(컬럼 추가만).
2. 엔진: 표시용 행(cash NaN / child null / status unresolved) 을 거부하지 않고 읽어, **보유 중인 부모**(ex bar 직전 가중치 ≠ 0) 에 대해 `unconfirmed` 기록: bar 별 진단 + 실행 요약 `corporateActions.unconfirmed = {count, sumAbsWeight, items:[{symbol, date, weightBefore, kind}]}`(kind ∈ unresolved_leg / default_settlement(조건 없는 events 정산) / estimate_value(추정값 기장) / pending_dropped(자회사 미가격 포기)) + `pnlUnconfirmed: bool`. JSON 결과·`/api` 응답·timeseries 출력에 노출. 설정 `backtest.corporate_actions.unresolved_policy: warn | strict`(기본 warn; strict 면 첫 미확정 사건에서 실행 실패 — 오류 메시지에 최초 N건 목록, HTTP 는 4xx). 유니버스·포지션·손익 계산은 바꾸지 않음(원장 없는 입력은 비트 동일 유지).
3. 테스트: gtest(표시용 행 보유/미보유, strict 실패, default_settlement·estimate 목록, JSON 필드), config 로더 테스트, fixture 생성기(`tools/make_http_test_fixture.py`) 표시용 행 포함, HTTP contract, 기존 골든 비트 동일, `tests/recon_engine_ca.py` V1 하니스 2,189 + 표시용 행 케이스 추가 → 전부 PASS(자기 빌드 CTest 전체).
4. 발행본 rc2f 원장으로 export 재생성(`SP/stage_rc2f/E/Y1/engine/ca_ledger_cache.parquet` + manifest; 표시용 행 수·사유별 개수 보고), 문서 `docs/corporate_actions_engine_2026-09-17.md` 절 추가, CLAUDE.md 한 줄.
완료 기준: 위 4항 전부 + 보고서(`E/reports/Y1.md`: 설계·스키마·진단 필드·테스트 수·export 통계). 리드가 별도 빌드로 재검증.

## Y2 applied_unverified 38 — 3상태 검사 + 건별 근거 (X0 코드 / X1·X2 데이터)
현황: `rc2f_apply.py`(≈1255행) split raw 점프 검사는 가격이 없으면 성공값을 반환하고, 실패해도 적용은 됨(`applied_unverified`). 38건 = X1 37 + X2-44; X1 의 isEstimate false 19·true 18.
요구:
- Y2a(X0): 검사를 pass / fail / uncheckable(경계 ±5 거래일 raw 없음) 3상태로, 로그 status `applied`(pass) / `applied_unverified`(fail) / `applied_unchecked`(uncheckable) + reason 에 검사값; 검증기(validate) 는 fail 이 예상되는 교정(근거가 raw 점프가 아닌 MI/공시형) 을 CSV 컬럼 `checkBasis ∈ {raw_jump, mi_ratio, document, none}`(선택) 으로 받아 fail 이면 `checkBasis` 가 raw_jump 가 아닌지 확인해 경고/오류 구분. 테스트 추가.
- Y2b(X1·X2): 38건마다 "검사가 왜 실패했고 이 교정이 왜 맞는가" 를 별도 근거(MI 비, ciqsplit, co_acthist, 공시)로 닫음 → 유지(근거 기록·`checkBasis`), 추정 강등(isEstimate true/원장 estimated), 철회(행 제거) 중 하나. `SP/stage_rc2f/E/reports/Y2_unverified_38.md`(건별 표: overrideId, 사건, 검사 실패 사유, 근거, 판정). CSV 는 소유 태스크 파일에 반영.
완료 기준: 38건 전부 판정 + 3상태 코드/테스트 + 리뷰어 표본 재확인 PASS.

## Y3 전체 노출 보고서 + 최종 수치 정정 (python; opus, effort LOW)
`cache_v21/rc2f_exposure.py` → `qa/rc2f/exposure_report.md`(+csv): 발행본(61 rc2f) 기준으로
1. 셀 집계: retFlag 0~4 + 새 id 첫 행 NaN(연결 불가) + estimate(3) 를 유니버스(Top500/Top1000/ALL, 당일 멤버십)×연도 표로; Top500 당일 flag 4 / flag 3 / NaN 첫 행 각각.
2. 584행 정정 표: "정상 분류 전환(0/1/2) 127 · 추정 전환 82 · 잘못된 연결 제거(NaN) 103 · 잔여 271 + 행 삭제 1 · 새 flag 4 139(사유별)" 를 recount 파일에서 재계산(숫자는 스크립트 출력).
3. 자료 부족 803 사건을 클래스×사유 범주로 집계하고 ENDDATE 62·GAP 2·PRICE 1 은 **사건별** 표(id, 날짜, 사유, 보유 영향 가능성).
4. 라이브 북(combo000014 목표 포지션 `/home/base/portfolio/portfolio_v1_0/combo/combo_export/combo000014/*_pos_values.parquet`, 2025-01~) 과 flag 3/4/NaN 셀·미확정 events 정산의 교차: 날짜·종목·포지션 값 목록 + 합계(|w| 기준).
5. 발행 manifest·registry_final·recount 숫자 간 불일치 점검(272 vs 271+1 등) 후 정정 표.
완료 기준: 스크립트 재실행 가능, 보고서 표 전부 스크립트 출력, 리드 검산.

## Y4 combo000014 전체 전략 비교 — 고정 스냅샷 rc2 vs rc2f (python + 가능하면 C++; opus, effort MEDIUM)
기존 파이프라인 재사용: `~/longshort/cache_v1_vs_v2/{production_engine_lib.py, compare_lib.py, FINDINGS_production_engine.md}`(combo000014 14 알파 AlphaPos → 파이썬 회계 커널 `cpp-engine/tools/missing_close_oracle.py::run_ledger`, 정산 events, 배당·분사 layers). 입력을 rc2(`.prev-…rc2`) 와 rc2f 로 바꿔(가격 = `loader.py export_wide` split_adj close·TopUniverse, events = 캐시 `events/corporate_actions.parquet`(cache 단위 컬럼), 배당·분사 = `engine/ca_ledger_cache.parquet` cash/stock leg 값(v2 DivData 대신)) 동일 AlphaPos 로 두 캐시의 손익·회전·Sharpe·layer 분해를 비교. 차이 셀을 registry_final/overrideId 에 귀속(rc2f 재집계 `returns_diff.parquet` 활용). 보유 중 미확정 노출 목록(flag 3/4/NaN/조건 없는 정산) 을 날짜·종목·가중치로. C++ 엔진 경로(`base:` + `backtest.corporate_actions` + Y1 진단)는 Y1 완료 후 같은 책으로 1회 교차 실행(가능하면; `examples/datasets/compare_v2.yaml` 을 본떠 rc2f export_wide 로).
완료 기준: 노트북 또는 스크립트(재실행 exit 0), `E/reports/Y4.md`(설정표, 성과 비교표, 차이 귀속표, 미확정 노출표, C++ 교차 결과 또는 미실행 사유), 리드 검산.

## 리뷰(웨이브 2, opus HIGH 독립)
R-Y1: 코드 통독·자기 빌드 CTest·표시용 행 보유/미보유 실측·strict·골든 비트 동일·V1. R-Y2: 38건 표본 전수 재확인(근거 원문). R-Y3/R-Y4: 숫자 재계산·귀속 표본. FAIL 이면 재작업.
