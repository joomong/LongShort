# Phase 2 독립 리뷰 (원천 이벤트 파이프라인 + successor 이벤트)

리뷰어: 독립 리뷰어(적대적). 코드·빌드 무변경, 기존 테스트 바이너리만 실행, xfeed는 read-only 접속(`default_transaction_read_only=on` + `set_session(readonly=True)`), 서버 미기동. 대상: `git diff HEAD` + 미추적 Phase 2 산출물(`data/{master,events}`, `cpp-engine/tools/data/`).

## 0. 결론 — **조건부 머지**

- **엔진(§P2-3)은 P0 없음.** 승계 회계는 계약대로이고 손계산·연쇄·폴백·mode 0/1이 코드와 테스트 양쪽에서 맞음. 기존 테스트 3종(`test_simulator` 43, `test_parquet_loader` 19, `test_config_loader` 8)을 **현 바이너리로 직접 재실행해 전부 통과** 확인했고, 비트 동일 golden(`ReproducesThePreChangeEngineDumpBitForBit`)도 그 안에 있음. Phase 1 리뷰의 F-01·F-03·F-09·F-11·F-13은 현 트리에서 해소돼 있음.
- **머지 전 필수 2건(P1)**: `build_events.py`의 (a) `"RESULTING IN A CASH EQUIVALENT OF $x"`를 현금 다리로 오파싱 → 대가 이중계상(7건, Top500 3건, Comerica R +15.04% vs 정답 +9.34%), (b) `CAN$` 미필터로 캐나다달러를 USD로 계상(2건, R +34.2%/+27.7%). 둘 다 정규식 한 줄 수정 + **이벤트 파일 재생성**이 필요함.
- **같이 처리 권고(P1)**: `CLAUDE.md`·`examples/datasets/README.md`가 **구 정산 규칙**("종료일 다음 bar에 정산")을 그대로 서술함. 실제 규칙은 "종료일 이후 **호가가 끊긴 첫 bar**"이고 루트 `README.md`만 맞음. 계약 요약 문서 두 곳이 틀린 상태로 머지되면 안 됨.
- 그 외 P2 15건은 후속으로 충분함. 실데이터 영향이 확인된 것은 위 3건뿐이고, 나머지는 잠복(현 유니버스 밖)이거나 진단·문서 문제임.

---

## 1. 발견

| ID | 심각도 | 파일:줄 | 증상 | 근거/재현 | 수정안 | 확신도 |
| --- | --- | --- | --- | --- | --- | --- |
| **P2-01** | **P1** | `cpp-engine/tools/data/build_events.py:293-300` (`_CASH_HEADLINE`/`amounts[0][1]` 경로) | `"...RECEIVED N SHARES OF X ..., RESULTING IN A CASH EQUIVALENT OF $y."`에서 $y를 **현금 다리로 채택**해 주식 대가와 합산 → `settlementReturn` 과대. 전액 주식 합병인데 `kind=mixed`가 됨 | 정규식 `CASH\s+EQUIVALENT\s+OF\s+\$(num)` 값 == 파싱된 `cashPerShare`인 행 **7건**(Top500 3: `2601589` Comerica, `40272951` DISH, `2639311` Pioneer / Top1000 5). Comerica 2026-01-30: mark 88.67(캐시 2026-01-30), FITB 51.95(정산 bar 2026-02-02) → 파일 R=+0.150444, 정답 1.8663×51.95/88.67−1=**+0.0934**, 오차 **+5.7pp**. OS 구간 내부임 | `usd_amounts()`에서 `CASH EQUIVALENT` 뒤 금액을 제외하거나, `IN A STOCK MERGER` 문구가 있으면 현금 다리를 버릴 것. 재생성 필요 | 높음(실측) |
| **P2-02** | **P1** | `build_events.py:180` `_FOREIGN_WORD` | 통화 필터 목록에 `CAD`·`C$`는 있으나 **`CAN$`/`CAN $`가 없음** → 캐나다달러 금액이 USD 현금으로 계산됨 | `2650011`(Shaw, `FOR CAN $40.5`) R=**+0.3419**, `141549237`(Corvus, `CAN$4.10`) R=**+0.2773**. 둘 다 |R|<0.5라 안전장치에 안 걸림. 현재 Top500/Top1000/ALL 어디에도 없어 **잠복** | `_FOREIGN_WORD`에 `CAN` 추가(+`AUS`,`NZD` 등). `has_foreign` 정규식도 동일 | 높음(실측) |
| **P2-03** | **P1** | `CLAUDE.md:26`, `cpp-engine/examples/datasets/README.md:56` | 정산 시점을 **"종료일 다음 패널 bar"** 로 서술 — 2차 리뷰에서 "종료일 이후 첫 무호가 bar"로 바뀐 규칙(`simulator.cpp:157-163`)과 어긋남. 같은 README:48-54의 `events:` 예시도 구 파일(`uss_cash_master_acc.parquet`/`securityEndDate`)이고 `successor_col` 부재. README:59("예제는 60을 쓴다") vs :68("주식 예제도 0") vs `equities_monthly_afl.yaml:51`(`0`) 자기모순 | 루트 `README.md:30`만 새 규칙으로 맞음. 두 문서가 서로를 권위 문서로 가리킴 | 세 문서 정산 문구 통일, events 예시를 실제 config로 교체, stale 문구 0으로 통일 | 높음 |
| P2-04 | P2 | `simulator.cpp:250` vs `:244,248` | `settled_gross += \|h_prev\|`(정산 **전**), `converted_gross += \|h_prev·(1+R)\|`(정산 **후**) — 기준이 달라 `oracle_report §9-5`·`datasets/README.md:72`가 권하는 "총 정산 노출 = settledGross + convertedGross"가 동차가 아님. R=−1이면 승계분이 **양쪽 어디에도 안 남음** | 코드 대조. Top500 실행에서 settledGross 0.2102 / convertedGross 0.1378 | `convertedGrossBefore`(=Σ\|h_prev\|)를 별도 집계하거나 비대칭을 문서에 명시 | 높음 |
| P2-05 | P2 | `simulator.cpp:214-224` | `transferable`이면서 `ratio`가 NaN이고 명시 R도 없으면 R=**default**로 떨어지는데 그대로 `h·(1+default)`를 승계함 → default를 0↔−1로 바꾸면 승계 규모·이후 turnover·fee가 달라져 R 민감도 항등식이 깨짐. 하네스(`run_top500_missing_close.py:172-191`)는 잔차를 **보고만 하고 단언하지 않음** | 현 이벤트 파일은 successor 1,245건 전부 ratio 유한이라 미발현. Top500 잔차 7.7e-16, convertedGross가 R=0/−1에서 비트 동일 | `transferable` 조건에 `isfinite(ratio)` 추가(= computed 가능할 때만 승계), 또는 default-R 승계 금지 | 높음 |
| P2-06 | P2 | `parquet_reader.cpp` `read_event_ids` | id 컬럼은 INT64/STRING/LARGE_STRING만 허용(INT32 미지원)인데 successor 컬럼(`read_optional_ids`)은 INT32를 허용 → 비대칭. int32 id 파일은 로드 실패 | 코드 대조(`switch(chunk->type_id())` 분기) | `read_event_ids`에 INT32 케이스 추가 | 높음 |
| P2-07 | P2 | `simulator.cpp:455-458`, `test_simulator.cpp:1197` | 파일에서 온 **명시 `settlement_return` 범위 검증 없음**. config default만 [−1,1] 검사. R=−3 같은 값이 오면 `h_prev·R`로 조용히 가짜 이익 | 테스트도 config 값만 검사. 현 파일 max\|R\|=0.484라 무해 | 로더 또는 생성자에서 유한·[−1, 상한] 검사 후 거부 또는 NaN 처리 | 높음 |
| P2-08 | P2 | `data_panel.h:44`, `parquet_reader.cpp:468`(`numbers()`) | `settlement_return`이 **float32**로 저장됨(원본 double, oracle은 double) → 실데이터에서 ~1e-8 상대 차이. 합성 시나리오 값은 float 정확이라 "최대 오차 5.5e-17"은 이 경로를 증명하지 못함 | 코드 대조 | `settlement_return`을 double로(또는 oracle을 float32로 반올림) | 높음 |
| P2-09 | P2 | `build_events.py:388-419` + `events_report.md §1` | R의 두 다리를 `priceclose × adjustmentfactor`(as-traded)로 통일하는데, **엔진의 mark는 로컬 캐시 값**임. 표본 1,196 mark 중 캐시==`priceclose` **1,190**, 캐시==`pc×f` **1,186**; factor≠1인 39건 중 **26건이 캐시==pc**. 보고서 §1의 근거("캐시는 수집 당일 raw priceclose", GOOG 2015-10-05 캐시=641.47)는 **틀림** — 641.47은 사멸 라인 `312940617`의 캐시값이고, 살아있는 `260152426`의 같은 날 캐시는 **32.0735**(= 재작성값)임 | DB 재조회 + 캐시 직접 대조. 다만 **산식 자체는 자기정합**(두 다리 동일 기준 → 수익률은 기준 무관). factor==ratio인 역분할 개명 20건은 정확히 상쇄됨(`46636899`: 파일 R=+0.15 = 캐시 7.59/6.60−1 ✓ 실측). factor≠ratio인 **6건**(`285992458`,`27069124`,`34204761`,`704039414`,`629696426`,`261726643`)만 기준이 섞임 — 전부 Top500/Top1000 밖(`34204761`만 ALL) | (a) 보고서 §1 설명 교정, (b) `pc×f`가 캐시 mark와 어긋나고 factor≉ratio면 R을 버리는 가드 추가 | 높음(실측) |
| P2-10 | P2 | `simulator.cpp:220-223` | computed 경로 `ratio·close_succ(캐시)/mark_old(캐시)`는 as-traded 기준 `ratio`를 **캐시 가격**에 곱함. successor의 정산일 `adjustmentfactor`≠1인 행이 **169/1,195**라 기준 혼합 위험 | 전기간 3개 유니버스에서 `computedSettlementReturns=0`(모든 successor 행에 명시 R 존재)이라 **현재 미발현** | computed 경로 사용 시 successor factor 검증, 또는 파일이 항상 명시 R을 제공하도록 계약 | 중간 |
| P2-11 | P2 | `data/events/events_override.csv` (AVB 행) | AVB(`2591859`) endDate 2026-08-14 → 엔진 정산 bar **2026-08-17**. 그런데 successor VMRK(`2016912246`)의 캐시 첫 호가는 **2026-08-18** → `transferable=false` → **현금 정산 + 익일 전량 신규 매수**(round-trip turnover) | 캐시 직접 확인: 2026-08-17 AVB NaN·VMRK NaN, 2026-08-18 VMRK 64.03. EQR(`2610085`, endDate 08-17→정산 08-18)은 정상 승계됨 | override의 AVB endDate를 08-17로 맞추거나(=EQR과 같은 bar) 한계로 기록 | 높음(실측) |
| P2-12 | P2 | `cpp-engine/tools/run_top500_missing_close.py:61-63,81` | `--universe-path`에 **슬래시 포함 상대경로**를 주면 절대화하지 않음(`exists()`는 CWD 기준 통과). 생성 config는 `--output` 디렉터리 기준으로 해석돼 다른 파일을 읽거나 로드 실패 | 재현: `--universe-path sub/MyUni.parquet` → 생성 config에 상대경로 그대로. `Top500/Top1000/ALL`·절대경로는 무영향(실제 3종 생성 확인) | `Path(universe).resolve()` | 높음 |
| P2-13 | P2 | `cpp-engine/tools/data/refresh_master.py:216-220` | `exchangeId`를 DB 현재값으로 **무조건 덮어씀** → 벤더 불변식 `{102,104,106,173,458}`이 깨져 **227행이 화이트리스트 밖**(OTC 134 등), 그중 174행이 endDate NULL(생존), 114행이 ALL 소속. 종료일은 "절대 안 덮어씀"인데 거래소는 "무조건"이라 안전 규칙이 비대칭 | 원본/갱신본 파케이 직접 대조(위반 0행 → 227행) | 화이트리스트 밖 값은 보류 + `exchangeConflict` 플래그 | 높음 |
| P2-14 | P2 | `refresh_master.py:201,204,234` | 종료일 신규 채움(715건) 시 **로컬 캐시에 그 이후 종가가 있는지 검증 안 함** → 17건이 종료일 이후에도 거래 중. ALL 기준 "종료일 이후 정상 호가" 종목 13→**23**. 최악 `307039679`(CVEO) 종료일 2015-07-17 이후 캐시 **2,598 bar** | 파케이+캐시 대조. **손익 피해는 없음**(직접 확인: CVEO는 R=NaN·successor 없음이고 `event_bar≥0`이라 stale 경로도 안 타며, Phase 1 연기 규칙이 2025-11-14까지 정산을 미룸). 대신 ALL 실행의 `postEndDateQuoteBars=3505` 중 74%가 CVEO 한 종목임 | 채움 조건에 `new_end ≥ pricingDate`(같은 프레임에 이미 있음) 가드 + `endDateSuspect` 플래그 | 높음(실측) |
| P2-15 | P2 | `refresh_master.py:196,253,500-513` | `--ids-from-cache` 사용 시 `n_queried`가 항상 마스터 행수라 "조회 대상 28,210 / xfeed 미존재 21,893"으로 **거짓 보고**하고 미존재 표에 `head()`가 없어 보고서가 22,101줄로 폭증. §6 충돌 표는 29건이라 쓰고 `head(20)`만 출력(캡션 없음) — 누락 9건에 최악 사례 NTST(+7,091일)·USPI 포함 | 재현 확인 | `n_queried=len(ids)`, 미존재 표 `head(50)`, 충돌 표 캡션 또는 전체 CSV | 높음 |
| P2-16 | P2 | `cpp-engine/examples/datasets/equities_monthly_afl.yaml:30` + `.gitignore:220` | 추적되는 예제 config가 **gitignore된 산출물**(`data/events/events.parquet`)을 가리키고, 로더는 파일 부재를 오류로 던짐 → fresh clone에서 예제 config가 **반드시 로드 실패**. README에 그 경고 없음 | `git check-ignore -v data/events/events.parquet` → `.gitignore:220 data/` | README에 선행 생성 절차를 전제조건으로 명시하거나 events 블록을 주석 처리한 사본 제공 | 높음 |
| P2-17 | P2 | `build_events.py:472` (`have_terms = cash.notna() \| ratio.notna()`) | 혼합 딜에서 **현금 다리 파싱이 실패해도 주식 다리만으로 R을 만듦** → R이 현금만큼 과소 | `141548655`(Tahoe→Pan American): 원문 `0.1929 STOCK SHARES AND C$ 0.88408`인데 C$가 걸러져 cash=NaN → R=**−0.2756**. 유니버스 밖 | 원문에 통화기호/CASH가 남아 있는데 cash가 NaN이면 R을 버리고 `unparsed_cash_leg`로 기록 | 높음(실측) |
| P2-18 | P2 | `build_events.py:191-194`(`_STOCK_LEG`/`_STOCK_TICKER`) | `"FOR 0.475 PLD SHARES FOR EVERY DRE SHARE HELD"`(숫자+티커+SHARES, `OF` 없음)를 못 잡음 → **Duke Realty(`2607489`, Top500, 2022-09-30)** R=NaN·successor 없음 → 기본 0 현금 정산 | 표본 30건 중 22번. `unparsed` 438건의 대표 문형 | `NUM\s+([A-Z]{1,6})\s+SHARES?\s+FOR\s+(EACH\|EVERY)` 패턴 추가 | 높음(실측) |

---

## 2. 항목별 판정

### 2-1. 승계 회계 — **확인됨**

| 계약 항목 | 판정 | 근거 |
| --- | --- | --- |
| R 결정 순서 explicit→computed→default | 확인됨 | `simulator.cpp:214-224`. explicit은 event/stale 구분 없이 우선(Phase 1 F-01 해소), computed는 `transferable && isfinite(ratio) && marks_>0`일 때만 |
| `h_succ += h_prev·(1+R)`를 평가 **직후** 반영 | 확인됨 | `advance()` `:89-96`이 `settle→normalize→(Error검사)→value_holdings→apply_successor_transfers→execute` 순. `apply_successor_transfers()` `:266-273`이 `value_holdings` 뒤라 `mark_succ`는 이미 당일 close → 승계분 당일 수익 0이 구조적으로 성립 |
| turnover/fee 0 | 확인됨 | `execute()` `:377`의 `trade = weights_ − carried_`에서 carried_에 이미 승계분이 들어 있어 이전 자체는 거래로 안 잡힘. 테스트 `SuccessorQuoteCarries...`가 turnover 0·fee 0 고정 |
| `settledGross`(현금만)·`convertedGross` 배타 | 확인됨(기준 비대칭은 P2-04) | `:241-253`의 if/else가 상호배타. per-asset 시계열 `settled_gross_[ii]`도 현금 분기에만 기록 |
| retired는 old만 | 확인됨 | `settle()` 1단계에서 정산 대상만 `retired_=1`. 테스트가 `retired[3][0]=1, [3][1]=0` 고정 |
| 연쇄 가드(같은 bar 정산 successor) | 확인됨 | 정산 집합을 먼저 확정(`settles_now_`)한 뒤 `book_settlements`에서 `!retired_[successor]` 검사 → 인덱스 순서 무관. 테스트 `chained` 케이스 |
| 패널 밖/자기참조/무호가 → 현금 폴백 | 확인됨 | 로더가 −1로 떨구고(`parquet_reader.cpp` `unresolved_successors`), 엔진은 `quote_state_[successor]==Observed`를 요구. 실데이터 Top500 패널에서 `24 successor id(s) are not in the loaded panel` 경고 확인 |
| `computedSettlementReturns` 정의 | 확인됨 | computed일 때만 증가, `defaultDelistings`에서 제외(`:233-238`) |
| mode 0/1 | 확인됨 | 승계 테스트 4종이 모두 `for (int mode : {0,1})` 루프 |
| warmup 중 승계 | 확인됨(단 테스트 없음, §4) | warmup은 `weights_`를 매 bar 0으로 채워(`:118`) `previous_weights_`가 항상 0 → `exposure==0`이라 이전이 발생하지 않음. `retirement_count`만 증가 |
| 승계분이 그 bar에 즉시 리밸런스되는 점 | 확인됨 | `execute()`가 successor의 weights_를 target으로 덮으므로 승계의 **지속 효과는 turnover 절감뿐**임. `engine_report §8-3`은 정확히 그렇게 적었으나 테스트 이름(`...AtTheNextExecution`)은 오해 소지 |

### 2-2. 비트 동일·회귀 — **확인됨**

- 현 바이너리 직접 실행: `test_simulator` **43/43**, `test_parquet_loader` **19/19**, `test_config_loader` **8/8** 통과(재빌드 없음).
- 골든 회귀 2종이 파일에 존재: `ReproducesThePreChangeEngineDumpBitForBit`(`test_simulator.cpp:990`, `tests/golden/missing_close_bitwise.json`), `CompleteQuotesKeepTheExistingFixedBookResultBitForBit`(`:752`). Phase 1 리뷰가 지적한 "레포 밖 probe로만 고정" 문제가 해소됨.
- Phase 1 동작 무변경: 동결(`value_holdings` `:336-342`)·stale(`:164-172`)·정산 연기(`:157-163`, `event_now = event_due && !observed`) 로직에 Phase 2 변경이 닿지 않음. 전기간 하네스 R 민감도 잔차 ≤ **7.7e-16**(Top500 5알파).

### 2-3. 로더 — **확인됨(P2-06 1건)**

- successor id 정규화: null→빈 문자열(현금), INT64/INT32/STRING/LARGE_STRING/NA 지원, trim 포함. **id 컬럼만 INT32 미지원**(P2-06).
- 자기참조(`successor->second == n`)·패널 밖은 −1 + `std::cerr` 1줄 경고. 중복 id는 `Duplicate events id` 예외. ratio는 `read_doubles()`로 double 유지.
- `return_col`: `numbers()`가 null→NaN, ±inf는 `isfinite` 검사에서 탈락해 "명시 R 없음"으로 취급됨(안전). **단 범위 검증 없음**(P2-07), **float32 저장**(P2-08).
- **실데이터 probe 실행**(`data_load_probe cpp-engine/examples/datasets/equities_monthly_afl.yaml --summary`):

```
T=4449 N=1056  settlementSymbols=262  explicitReturns=186  retiredBeforeStart=0  noSettlementInPanel=794
stderr: Events: 24 successor id(s) are not in the loaded panel; those settle in cash
```
  - Top500 이벤트 행 262건(= `/api/config.settlementSymbols`), 명시 R 186건, successor는 파일 기준 95건 중 24건이 패널 밖 → 71건 로드(= `successorEvents=71`, 전기간 실행 `successorConversions=43`).
  - probe는 `successorEvents`를 출력하지 않음(서버 `/api/config`에만 있음) — 사소한 관측.

### 2-4. 이벤트 파일 품질 — **조건부(P1 2건)**

| 소항목 | 판정 | 근거 |
| --- | --- | --- |
| (a) 매칭 오매칭 | **확인됨** | 표본 30건 전수 재조회에서 `ciqgvkeyiid(objectid)`→(gvkey,iid)→`co_acthist` 결과가 파일과 **30/30 일치**(actionCode·datadate 모두). oldtic1 보조 매칭은 전체 5,166건 중 11건뿐이라 노출이 작음. §3 표 참조 |
| (b) 조건 파싱 | **버그 2건** | 선택형(`_ELECTION`)은 현금·주식 파싱보다 **먼저** 걸러짐(`:263-266`) → 표본 25번 EMCF `OPTIONAL CASH-OR-STOCK` 정상 NaN. "CENTS"는 10건 전부 회사명(99 Cents Only/Salem Five Cents)이고 오파싱 0건. 분수 표기는 미지원이나 오파싱이 아닌 미검출. **실제 버그는 `CASH EQUIVALENT`(P2-01)와 `CAN$`(P2-02)**. 다중 인수사는 `multi_class`로 차단됨(16건) |
| (c) `settlementReturn` 산식·가격 보정 | **조건부** | 인수사 종가를 **정산 bar(=종료일 다음 캐시 bar) 이후 첫 유효 종가**로 쓰는 것은 엔진 규약(무호가 첫 bar 정산·동결 구간 일괄 인식)과 **일치**함. 다만 종료일 이후에도 호가가 이어지는 55건에서는 엔진이 정산을 미뤄 기준이 어긋남(명시 R 보유 9건, 전부 postBars ≤ 4라 영향 미미). `priceclose×adjustmentfactor` 보정은 **보고서 근거가 틀렸고**(P2-09) 무조건 적용이라 factor≠ratio 6건에서 기준이 섞임 — 다만 산식 자체는 자기정합이고 역분할 개명 20건은 캐시 기준과 정확히 일치함을 실측 확인(`46636899` R=+0.15 = 7.59/6.60−1) |
| (d) 안전장치 | **부분 확인** | `\|R\|>0.5`(현금 딜도 0.5)는 벤더 오기(ACIA $0.01 vs 115, SINA $0.04 vs 43.26, ML $2 vs 85.9)를 실제로 잡음. 반면 **정상 대형 프리미엄과 구분하지 못함** — 임계 아래의 오기(P2-01 Comerica +5.7pp, P2-02 Shaw +34pp)는 전부 통과함. Top500 잔존 \|R\|>10%는 4건뿐이고 그중 NetSuite(+20.7%)·CB&I(+10.3%)는 실제 사건으로 보임 |
| (e) 현금 딜 평균 −0.25% | **해석 정정** | 동결 규약 때문이 **아님**. 2,002건 중 R<−10% **32건**이 합계 **−7.00**을 기여해 평균을 끌어내림. \|R\|≤10%만 보면 평균 **−0.0002**, 중앙값 **+0.00028**로 사실상 0임. 좌측 꼬리는 CVR·조건부대가·공개매수 불확실성·벤더 오기임 |
| (f) successor 1,245 / 캐시 안 1,171 | **확인됨** | 캐시 밖 74건은 로더가 −1로 떨궈 현금 정산(코드+경고 확인). 그 74건 중 대상이 Top500 멤버인 것은 7건. 유니버스가 좁으면 추가로 떨어짐(Top500 패널에서 95→71) — 구조적이며 정상 |
| (g) override 4건·VMRK | **확인됨(P2-11 1건)** | 파일 `source=='override'` 4행(EA·EXAS·AVB·EQR) 확인. `2016912246`은 DB에서 ticker **VMRK**, exchange 106(NYSE), currency 160(USD), status 15, endDate NULL로 실재함. 캐시에는 **2026-08-18부터** 존재해 EQR(정산 08-18)은 정상 승계, **AVB(정산 08-17)는 승계 불가 → 현금 정산**(P2-11) |
| (h) 마스터 갱신 | **조건부** | 충돌 29건은 **로컬 우선·DB 미채택**(`refresh_master.py:201-204`)이고 보고서 :373·:485에 명시됨 — 이른 날짜로 당기지 않으므로 정산 조기 발생 위험은 없음. 다만 5건 독립 재조회 결과 **DB가 옳은 경우가 더 많음**(CERN·KSU·SPW·GMGI=DB, NTST=로컬; 29건 전체로 DB가 캐시 마지막 종가와 일치 12건 vs 로컬 6건). WOLF(`2603877`)는 전용 처리가 아니라 blanket 규칙에 걸려 우연히 유지된 것이고, 실제로는 DB 2025-09-26이 옳으며 로컬 2026-08-11은 **후속 종목(`1962040598`) 종가 8 bar가 구 id에 잘못 붙은 아티팩트**임(두 계열 값이 완전 동일, 예: 2026-08-11 둘 다 30.14). 보고서는 표 한 줄로만 언급하고 판정이 없음. 신규 채움 715건 쪽이 더 위험(P2-14) |

### 2-5. 하네스 — **확인됨(P2-12)**

- 생성 config가 절대화하는 키(`datasets[*].path`·`catalog`·`universe.path`·`events.path`)는 엔진이 config_dir 기준으로 푸는 키 4종과 **정확히 일치**함(`config_loader.cpp:100,125`, `parquet_reader.cpp:27-29,414,497,540`). `datasets[*].fields.*`는 dataset.path에 join되므로 재작성 불필요. 키 부재/None은 crash 없이 skip.
- 신규 열 7종(`retirements`,`frozenGrossSum`,`postEndDateQuoteBars`,`pendingSettlements`,`successorConversions`,`convertedGross`,`computedSettlementReturns`)은 `missingData`/top-level 양쪽 fallback으로 읽고 키 부재 시 공란 처리(crash 없음).
- 서버는 try/finally로 terminate→wait(10)→kill, 누수 없음. 포트는 `check_missing_data.py:50`의 **18120~18139** 하드코딩(리뷰어 대역 18140~18159와 무충돌).
- 미흡: `--universe-path` 상대경로 미절대화(P2-12), R 민감도 잔차를 **단언하지 않음**(깨져도 exit 0), `CONTRACT_CONFIG_KEYS`에 `successorEvents` 누락으로 보고서 표에 승계 로드 여부가 안 나옴, `REVIEW_ADDED_KEYS`는 dead 상수.

### 2-6. 문서·계약 — **미흡(P2-03)**

- 정확한 것: `datasets/README.md:72`의 승계 문단(우선순위·transferable 조건·당일 수익 미반영·비용 0·`settledGross`는 현금만)은 구현과 일치하고, **혼합 딜 근사도 사용자 가시 문서에 명시**돼 있음("현금+주식 혼합 딜은 총수익률로 전액 승계하는 근사이며 현금 몫 분리는 후속 과제"). `CLAUDE.md:26`도 successor 두 키와 `settledGross` 정의를 담고 있음.
- 미흡: 정산 시점 구 규칙(P2-03), `CLAUDE.md`·루트 `README.md`에 혼합 딜 근사 문구 없음, 신규 진단 키 4종(`postEndDateQuoteBars`·`pendingSettlements`·`retirements`·`frozenGrossSum`)이 사용자 문서에 전무, `settledGross+convertedGross` 비대칭 미경고(P2-04), 예제 config가 gitignore 산출물 의존(P2-16).
- 참고: `simulator.cpp:213` 주석은 혼합 딜 분리가 "per-share cash amount가 없어서" 미구현이라 하지만, `events.parquet`에 `cashPerShare`가 이미 있음(비영 2,344행). 실제 이유는 로더가 그 컬럼을 읽지 않는 것임 — 주석 근거 교정 필요.

---

## 3. 이벤트 파일 표본 검증 (무작위 30건, `source='co_acthist'`, seed 20260911)

검증 방법: 각 `tradingItemId`를 DB에서 독립 재조회 — `ciqtradingitem`+`ciqsecurity`(티커·종료일), `ciqgvkeyiid`(objectid→gvkey/iid), `co_acthist`(해당 키의 행 중 종료일과 가장 가까운 것), successor의 `ciqtradingitem.tickersymbol`.

**요약: 종목·사건 매칭 30/30 일치(actionCode·datadate 전부), 종료일 30/30 DB와 동일. 조건 파싱은 정상 17 / 정보부족으로 NaN 처리 타당 12 / 부당한 미파싱 1(DRE).**

| # | tradingItemId | ticker | endDate(파일=DB) | code(파일/DB) | datadate(파일/DB) | cash | ratio | successor(티커) | R | 판정 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 8813266 | RTU | 2010-03-12 | ACQ/ACQ | 2010-03-15 / 동일 | - | 0.73456 | 9483689(UTF) | 0.0157 | O |
| 2 | 2632867 | NSM | 2011-09-23 | ACQ/ACQ | 2011-09-26 / 동일 | 25 | - | - | 0.0004 | O (TI/National Semi $25) |
| 3 | 34117364 | VRUS | 2012-01-18 | ACQ/ACQ | 2012-01-18 / 동일 | 137 | - | - | 0.0002 | O (Gilead/Pharmasset $137) |
| 4 | 27649825 | GOLF | 2012-07-24 | ACQ/ACQ | 2012-07-25 / 동일 | 6.1 | - | - | 0.0033 | O |
| 5 | 2643497 | ROCM | 2013-11-13 | ACQ/ACQ | 2013-11-15 / 동일 | 20 | - | - | 0.0000 | O |
| 6 | 2604161 | CBST | 2015-01-21 | ACQ/ACQ | 2015-01-23 / 동일 | 102 | - | - | 0.0006 | O (Merck/Cubist $102) |
| 7 | 23312151 | URZ | 2015-06-19 | ACQ/ACQ | 2015-06-22 / 동일 | - | 0.255 | 141549170(UUUU) | 0.2434 | 조건 O / R +24% 원인 미확인 |
| 8 | 11124864 | BMR | 2016-01-28 | ACQ/ACQ | 2016-01-28 / 동일 | 23.8239 | - | - | 0.0006 | O |
| 9 | 22813843 | OCAT | 2016-02-10 | ACQ/ACQ | 2016-02-11 / 동일 | 8.5 | - | - | 0.0035 | O |
| 10 | 2644737 | STJ | 2017-01-04 | ACQ/ACQ | 2017-01-05 / 동일 | 46.75 | 0.8708 | 2586533(ABT) | 0.0062 | O (실제 딜 $46.75+0.8708 ABT) |
| 11 | 268329113 | SBCP | 2017-12-29 | ACQ/ACQ | 2018-01-02 / 동일 | - | 0.89 | 2598949(CSFL) | -0.0072 | O |
| 12 | 215503912 | LVNT.A | 2018-03-13 | N/C/N/C | 2018-03-12 / 동일 | - | 1.0 | 549496298(GLIB.A) | -0.0109 | O (개명 승계) |
| 13 | 2601469 | JNP | 2018-08-14 | ACQ/ACQ | 2018-08-15 / 동일 | 11.5 | - | - | 0.0000 | O |
| 14 | 241615972 | TLRA | 2020-03-31 | ACQ/ACQ | 2020-04-01 / 동일 | - | - | - | NaN | 타당(원문에 교환비 없음: `RECEIVED STOCK SHARES OF ...`) |
| 15 | 237168168 | FNJN | 2020-07-23 | ACQ/ACQ | 2020-07-27 / 동일 | 1.55 | - | - | 0.0065 | O |
| 16 | 2615571 | GSB | 2020-08-27 | ACQ/ACQ | 2020-08-28 / 동일 | - | - | - | NaN | 타당(actiontxt 없음) |
| 17 | 37284106 | AVM | 2020-11-13 | ACQ/ACQ | 2020-11-16 / 동일 | 13.75 | - | - | 0.0000 | O |
| 18 | 684070193 | DMYD | 2021-05-06 | ACQ/ACQ | 2021-04-21 / 동일 | - | - | - | NaN | 타당(`NO PER SHARE TERMS`) |
| 19 | 694766737 | FTIV | 2021-07-09 | ACQ/ACQ | 2021-06-25 / 동일 | - | - | - | NaN | 타당(SPAC 역합병, 조건 없음) |
| 20 | 648584042 | CCAC | 2022-02-28 | N/C/N/C | 2022-02-09 / 동일 | - | - | - | NaN | 타당(원문 없음), 단 newtic1=QNGY 승계 미연결 |
| 21 | 703766047 | WARR | 2022-09-02 | LIQ/LIQ | 2022-09-05 / 동일 | - | - | - | NaN | O (`suggestedReturn=-1`, 정책은 엔진) |
| 22 | 2607489 | DRE | 2022-09-30 | ACQ/ACQ | 2022-10-03 / 동일 | - | - | - | NaN | **X — 원문 `FOR 0.475 PLD SHARES FOR EVERY DRE SHARE HELD`가 파싱 가능한데 미검출(P2-18). Top500 대형주** |
| 23 | 717833688 | NLIT | 2022-10-31 | N/C/N/C | 2022-09-29 / 동일 | - | - | - | NaN | 타당(원문 없음) |
| 24 | 699938693 | HAAC | 2022-11-03 | LIQ/LIQ | 2022-11-04 / 동일 | - | - | - | NaN | O |
| 25 | 2608981 | EMCF | 2022-12-30 | ACQ/ACQ | 2023-01-03 / 동일 | - | - | - | NaN | O (`OPTIONAL CASH-OR-STOCK` → election 차단) |
| 26 | 1686161371 | VMGA | 2023-05-08 | LIQ/LIQ | 2023-05-16 / 동일 | - | - | - | NaN | O |
| 27 | 2618408 | HSKA | 2023-06-12 | ACQ/ACQ | 2023-06-14 / 동일 | 120 | - | - | 0.0001 | O (Mars/Heska $120) |
| 28 | 1676075577 | LITT | 2023-06-15 | LIQ/LIQ | 2023-06-16 / 동일 | - | - | - | NaN | O |
| 29 | 530321375 | PETQ | 2024-10-24 | ACQ/ACQ | 2024-10-28 / 동일 | 31 | - | - | 0.0006 | O |
| 30 | 1680937070 | CFSB | 2025-10-31 | ACQ/ACQ | 2025-11-04 / 동일 | 14.25 | - | - | 0.0000 | O |

---

## 4. 누락 테스트

| # | 대상 | 왜 필요한가 |
| --- | --- | --- |
| 1 | warmup 구간 승계(`sim_start_index` > 정산 bar + successor) | 구조상 `exposure==0`이라 안전하지만 브리프가 명시한 항목이고 고정값이 없음. `retirement_count`만 증가하는지 |
| 2 | 다대일 승계의 **회계**(A·B → C) | 로더 테스트(`SuccessorIdsResolveToPanelIndices...`)는 인덱스 해석만 검증. `pending_transfer_[succ] += transfer` 누적은 시뮬 레벨에서 미검증 |
| 3 | 승계 bar에 frozen budget이 동시에 걸릴 때 | `execute()`의 `scale`은 승계분을 예산에 넣지 않음. F>0 + 승계 조합의 기대값이 고정돼 있지 않음 |
| 4 | 명시 R=−1 + successor | `converted_gross=0`, `settled_gross=0`이라 노출이 두 카운터 어디에도 안 남는 경계(P2-04) |
| 5 | successor 있고 ratio NaN, 명시 R 없음 | default-R이 승계 규모로 들어가 R 민감도 항등식이 깨지는 경로(P2-05). 현재 전혀 커버 없음 |
| 6 | 패널 `settlement_return` 범위 밖(−3, +50) | `RejectsASettlementReturnOutsideATotalReturn`는 **config default만** 검사(P2-07) |
| 7 | events parquet의 int32 `id_col` | successor 컬럼만 int32를 허용하는 비대칭(P2-06) |
| 8 | 종료일 null + successor + stale 정산 | stale 경로(`event_bar<0`)에서도 successor 이전이 일어나는데 의도한 동작인지 고정된 곳이 없음 |
| 9 | 실데이터 연동 스모크의 자동화 | `equities_monthly_afl.yaml`은 gitignore된 파일에 의존해 CI에서 재현 불가(P2-16) |

---

## 5. 논-이슈(확인했으나 문제 아님)

- **"CENTS" 단위 오파싱**: 10건 전부 회사명(99 Cents Only Stores, Salem Five Cents Savings Bank)이고 현금은 `$` 표기에서 정상 파싱됨. 오파싱 0건.
- **CAD/GBP/EUR/JPY 누출**: grep 매치 59건을 전수 확인했더니 전부 회사명(Cadence·Cascade·Euronet·NYSE Euronext 등)이며 실제 USD 딜임. 통화 필터가 무너진 건 `CAN$`뿐(P2-02).
- **선택형(election) 오인**: `_ELECTION`이 현금·주식 파싱보다 먼저 실행돼 136건을 NaN으로 차단함. `$X IN CASH OR N SHARES` 형도 패턴에 포함.
- **mark 노후(endDate − markDate)**: 명시 R 3,193건의 중앙값 0일, 99분위 28일, 365일 초과 1건. 현금 딜은 금액이 고정이라 무영향이고, 주식 딜은 R이 동결 구간 수익을 회수하는 구조라 오히려 정합적임.
- **CVEO(`307039679`) 2,598 bar 종료일 초과 호가**: 손익 피해 없음 — R=NaN·successor 없음이고 `event_bar≥0`이라 stale 경로도 안 탐. Phase 1 연기 규칙이 실제로 보호함(P2-14는 진단·마스터 품질 문제로 한정).
- **P2-09의 `×adjustmentfactor`**: 보고서 설명은 틀렸지만 산식은 자기정합이며, factor==ratio인 역분할 개명 20건은 캐시 기준 정답과 정확히 일치함(실측). "엔진 mark와 분모가 다르다"는 1차 의심은 기각.
- **Phase 1 리뷰 지적의 해소**: F-01(stale에서 명시 R 무시) → `book_settlements`에 event 게이트 없음으로 해소. F-03(범위 검증) → `simulator.cpp:455-458` + `main.cpp:252-255`. F-09 → `retirement_count` 추가. F-11 → probe에 `panelSymbols`. F-13 → `OrderedSignalAccumulator`가 policy를 필수 인자로 받음.
- **JSON 계약**: `missingData` 3키(`successorConversions`/`convertedGross`/`computedSettlementReturns`) + `/api/config.successorEvents` 존재 확인(`main.cpp:348-350,508`). timeseries `frozenGross`/`settledGross` 방출 확인(`json_serializer.cpp:95-103`).
- **전기간 실행**: Top500 `retirements=262`·`successorConversions=43`·`convertedGross=0.1378`·`postEndDateQuoteBars=1`·`pendingSettlements=0`, ALL `retirements=2545`·`successorConversions=285`·`postEndDateQuoteBars=3505`. R 민감도 잔차 ≤7.7e-16, `convertedGross`는 R=0/−1에서 비트 동일(모든 승계가 명시 R 사용).

---

*검증 산출물은 전부 `/tmp/claude-1008/.../scratchpad/rev/`에만 기록했고, 저장소·`/home/base`는 무변경임. DB는 read-only 세션으로만 접속했으며 비밀번호는 출력하지 않았음.*
