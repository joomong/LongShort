# cache_v2 계획·결정 기록 (2026-09-14)

목적: 팀 캐시(`/home/base/cache/cache_v1_cash_1d`, 이하 v1)를 **건드리지 않고** 별도 경로에 수정 캐시(v2)를 만들어, 같은 포트폴리오의 손익이 회계 수정(분할 기준 통일·배당·스핀오프)으로 얼마나 달라지는지와, 데이터가 달라지면 알파·유니버스가 어떻게 달라지는지를 나중에 노트북에서 비교한다. 이번 단계는 **데이터 생성까지**. 비교 노트북은 다음 단계.

## 1. 저장 위치와 원칙

| 항목 | 경로 | 비고 |
|---|---|---|
| v1 (팀, 원본) | `/home/base/cache/cache_v1_cash_1d` | 읽기 전용. 매일 덧붙여짐 |
| v1 고정 스냅샷 | `/home/jgo/61/data2/temp_stock/cache_v1_snapshot_2026-09-14/` | close·open·high·low·volume·VWAP·mid·midPrice·marketCap_USD·currentPrimary·DivData·TopUniverseV1·HoldingReturn + 레포 events 파일. `SHA256SUMS` 동봉 |
| v2 | `/home/jgo/61/data2/temp_stock/cache_v2_cash_1d/` | Master·BaseData·DivData·TopUniverseV2·TopUniverseV2_v1rule·Distributions·MANIFEST.json |
| 코드 | `cpp-engine/tools/data/build_cache_v2.py`, `build_distributions.py` | 미커밋 |
| 브리프 | 세션 스크래치패드 `orch/BRIEF_cache_v2.md` | 계약(컬럼·단위·검증값) 원문 |

원칙: `/home/base` 무수정, DB 읽기 전용 세션, 로더 제어 테이블 무접촉, 팀 협의 전 v1 재생성·덮어쓰기 없음. v1은 살아있는 파일이라 비교 기준선은 **스냅샷 + 해시**로 고정한다.

## 2. 오늘 확인한 사실 (근거는 세션 스크래치패드 스크립트)

### 2-1. close가 무엇을 조정한 가격인가
- DB `ciqpriceequity.priceclose` = 현재 주식 수 기준으로 소급 복원한 **분할조정가**. `adjustmentfactor`를 곱하면 실거래가(AAPL 2014-06-06: 23.056 × 28 = 645.57).
- 현금배당·스핀오프는 **미조정**(COST 2020-12-01 특별배당 $10: 391.77→387.56, eBay/PayPal 2015-07-20: 66.29→28.57).
- v1 close = 위 값을 **수집 당일 기준**으로 저장. 뒤에 분할이 나도 옛 행은 안 고쳐져 기준이 섞임. 유니버스 안 12만 셀 표본에서 1.05%(90종목)가 DB 현재값과 다르고 절반은 분할 배수 차이(TPX 4:1 2020-11-24 전 행 옛 단위, BRO 2014·2018). 연도별 1.0~1.7%(2010~19)→0.1%(2026). 이것이 보유 중 가짜 ±50~75% 점프의 원인(ALL 16건, Top1000 6건, Top500 0건).
- 팀 `Returns.HoldingReturn_Usd` = v1 close의 `pct_change` → 분할조정 가격수익률. 배당·스핀오프 없음. 따라서 받은 현금·주식을 더하는 것은 이중계산이 아님(단, `divadjfactor`를 추가로 곱하면 이중계산).

### 2-2. DB에 있는 기업행사 재료
| 테이블 | 있음 | 없음 |
|---|---|---|
| `ciqdividend` | 배당락·지급·기준·공시일, 금액, 통화, 유형(1 일반, 4 특별·청산, 11 주식배당, 20 스핀오프 catch-all) | 유형 20은 금액·비율 없음(230행 중 0) |
| `ciqsplit` | 비율·배당락일·유형·적용 여부 | – |
| `ciqpriceequitydivadjfactor` | 배당**과 스핀오프**를 합친 누적 조정계수 구간(COST 경계 0.9745 = 1−10/391.77 일치; eBay 0.371→0.882) | – |
| `co_acthist` SPN | 스핀오프 문구(비율·신설사) | 효력일 아님(기록일), 2026-02-19 이후 미적재 |
스핀오프 분배 가치는 계수로 1차 산정 가능: Top500 94건 중 92건에 계수 경계, 72건은 캐시 낙폭과 3%p 내 일치. 교환비·신설사 id는 문구/공시 필요.

### 2-3. 유니버스별 현황 (2010~2026-09, 건수)
| 문제 | Top500 | Top1000 | ALL |
|---|---|---|---|
| 일평균 종목 | 457 | 926 | 2,211 |
| 임시 결측 run(벤더 무기록 / 우리 누락) | 18 (9/9) | 43 (23/20) | 287 (186/100) |
| 유니버스 안에서 사라짐(정산 대상) | 132 | 477 | 1,747 |
| └ 최종 조건·교환비 미확보(기본 R) | 4 | 42 | 369 |
| └ 승계사 가격 캐시에 없음 | 8 | 20 | 47 |
| 캐시에 없는 회사 중 유니버스 진입분 | 15 (0.5%) | 54 (0.9%) | 464 (2.8%) |
| 스핀오프(모회사 가격만 하락) | 94 | 139 | 194 |
| 배당 미반영 평균(종목-연도) | 1.38% | 1.28% | 1.32% |
| 캐시 분할 경계 가짜 점프 | 0 | 6 | 16 |
| 종료일 확인/추정/flag | 32/44/0 | 45/176/3 | 45/604/15 |

캐시에 없는 회사는 현재 거래소가 OTC(452/464)라 **하락 후 이탈만** 빠져 있음(1년 고점 근처 이탈 0/1/11). 예외는 BATS 상장 CBOE. MLP(EPD·ET·MPLX·PAA)는 Common Stock 서브타입 필터로 별도 누락.

## 3. v2 설계 결정
1. **종목 집합** = v1 규칙(현재 주요거래소 5곳·Common Stock·USD) ∪ Compustat 거래소 이력상 한 번이라도 NYSE/AMEX/NASDAQ(11/12/14)였던 종목. 마스터에 `inV1Rule`·`everMainExchange`·`inV1Cache` 플래그를 두어 v1 동등 집합을 언제든 복원.
2. **단일 기준**: DB를 오늘 한 번에 읽어 전 기간이 현재 주식 수 기준. `adjustmentFactor` 패널을 같이 저장해 실거래 단위 조건(딜 조건·배당액)을 캐시 단위로 환산.
3. **기간** 2000-01-03~마지막 완결일. 후행 all-NaN(delay) 행 없음.
4. **필드** v1과 동일 이름(open·high·low·close·volume·VWAP·mid·bid·ask·midPrice·marketCap_USD·currentPrimary) + `adjustmentFactor`·`divAdjFactor`. float32.
5. **배당** `divAmount_USD`(지급 기준) + `divAmountCacheUnit`(현재 기준 1주당 = USD ÷ adjustmentFactor). 현금 유형만.
6. **유니버스** v1 규칙 그대로 v2 데이터에 적용(`TopUniverseV2`) + v1 규칙 종목만으로 만든 `TopUniverseV2_v1rule`(동일 비교용). TokensToRemove 동일.
7. **Distributions** 테이블: kind ∈ {cash, stock, split}. 스핀오프는 신설사 id·교환비(문구/공시)·계수 기반 분배 가치·v2 가격 기반 검증치·status. DB 밖에서 채운 건 전부 `Distributions/research_log.md`에 출처·URL·신뢰도와 함께 기록. **비율을 지어내지 않음**(모르면 unknown).
8. 엔진 변경(배당 현금 다리·스핀오프 부분 이전 opt-in 입력)은 다음 단계.

## 4. 작업 배정과 상태
| 작업 | 담당 | 산출물 | 상태 |
|---|---|---|---|
| v1 스냅샷 복사·해시 | 리드 | `cache_v1_snapshot_2026-09-14/` (2.6GB, 32파일, close sha256 원본 일치 22611adb…) | 완료 |
| T1 v2 패널·마스터·마스크 | 데이터 에이전트 | `build_cache_v2.py`, MANIFEST, `Master/build_report.md` (13.5분, RSS 15.6GB, 6,713×22,871, 검증값 10/10, v1rule Top500 중첩 99.4%) | 완료 |
| T2 배당·스핀오프·분할 테이블 | 데이터 에이전트 | `build_distributions.py`, `Distributions/*`, `research_log.md` | 진행 |
| T3 독립 감사 | 감사 에이전트 | `AUDIT.md` (9항목 PASS: DB 대조 10만 셀 100%, 분할 가짜 점프 v2 0건(v1 대조군 16건 재현), v1rule Top500 중첩 0.994; 조치 2건 → T1 재실행: 현금배당 유형 6/15/17/18/19/23 추가, 펀드(DBC) 유니버스 누출 제거) | 완료 |
| 비교 노트북 | 다음 단계 | `~/longshort/cache_v1_vs_v2/` | 진행 |

## 5. 다음 단계에서 재야 할 것
- 회계 효과: 같은 일별 비중(라이브 북 또는 5개 테스트 알파의 v1 비중) 고정 → v1/v2 가격·배당·스핀오프로 재평가. 층별(분할 기준 → 배당 → 스핀오프 → 수집 범위) 델타 표.
- 신호·유니버스 효과: v2로 알파·마스크 재계산.
- 결과 표는 종목별 기존/수정 손익, 반영 배당, 스핀오프 수령 주식·가치, 합병 현금·주식, 분할 오류 제거 효과, 비중·비용 변화.

## 6. 결과 기록 (2026-09-14 저녁)

### 6-1. v2 패널 (T1, 최종 MANIFEST build_time 2026-09-14 20:15:53)
- 규모: T×N = 6,713일(2000-01-03~2026-09-11) × 22,871종목, 36 parquet 2.0GB. 첫 전체 빌드 13.5분·RSS 15.6GB, 이후 재실행 3회(커버리지 절 추가 / 배당 유형·펀드 스크린 / 펀드 스크린 v1 동등화)는 각각 사전 고지 후 실행, BaseData·Master·`_v1rule` 마스크 해시는 첫 빌드와 동일.
- 종목: v1 규칙 17,464 / 과거 주요거래소 이력 20,829 / v1 캐시에 있음 17,582 / **v1에 없던 종목 5,289** / currentPrimary 18,199 / 회사 행 없음 440.
- 검증값 10/10(AAPL 23.056070·124.807503, TPX 25.645→25.12 경계 없음, BRO 16.105, EBAY 66.29→28.57, COST 배당 10.0, SIVB.Q 2022년 251일, FRCB 116836102 2023-03 23일).
- 유니버스(2010+ 일중앙값): ALL 2,243(v1rule 2,168) / Top1000 928 / Top500 458. v1 스냅샷 대비 중첩(|v1∩v2|/|v1| 중앙값): v1rule 변형 Top500 0.994·Top1000 0.995·ALL 0.992, 전체 변형 Top500 0.989·ALL 0.995.
- 발견: v1에는 있고 v2에 없는 열 — 2010+ 유효 열 12,264 중 29(대부분 DB에서 가격 이력이 지워진 종목; v1 Top500 해당일 0). v1 공통 셀 중 2.18%(Top500 마스크 0.94%)가 값이 다르며 v2는 전수 DB와 일치(옛 기준·벤더 정정분).
- 배당: 현금 유형 {1,4,5,6,7,8,10,15,17,18,19,23}(감사 지적으로 6/15/17/18/19/23 추가; 17은 미국 펀드·캐나다 원천징수 코드로 8,741행), 주식형 11/20/21/22/31/33 제외. **v1 DivData 결함 발견**: 2024년 15,398셀이 DB의 2배(중복 합산), 2015년 이전은 사실상 비어 있음 → v1 배당 패널은 기준으로 쓰지 말 것.
- 펀드 스크린: 전체 변형에서만 `(NOT inV1Rule) AND (companyTypeId==13 OR exchange∈{117,467,358})` → DBC(NYSE Arca 상장 원자재 ETF, v1엔 없음) 제외, ARCC 등 v1이 갖는 BDC/CEF는 유지. 부작용: 거래소 leg가 운영기업 2개(DSL Downey Financial 등, 2008년 이전 활동)도 제외 — `TopUniverseV2/fund_exclusions.csv`.

### 6-2. 독립 감사 (T3, `AUDIT.md`)
9항목 PASS: MANIFEST 36/36 해시 일치, ALL 마스크 10만 셀 DB 대조 100% 일치(최대 상대오차 5.9e-8), 분할 경계 가짜 점프 v2 0건(같은 코드로 v1 대조군 16건 재현), 알려진 사례 13/13, 유니버스 동등성 Top500 0.9936(p05 0.9914). 소비자 주의사항(AUDIT.md "What a consumer must know"): DivData는 달력일 인덱스, `divAmountCacheUnit` NaN 1,084셀, `close×adjustmentFactor`는 벤더 정정이 있는 소수 종목(폐쇄형 펀드 권리락·RYN·ASA)에서 근사, 권리락·스핀오프는 close에 실제 하락으로 남음, 상폐 후 0 채움 없음(v1 Returns와 다름).
- 수정 후 재검증(T3, "Post-fix re-check"): 36/36 해시 일치(BaseData·Master·`_v1rule` 29파일은 첫 빌드와 바이트 동일), 배당 패널 = 12유형 셀별 재구성과 일치(AEM·QSR·OVV 유형 17 값 DB 일치), DBC 전 마스크 0셀·ARCC 유지, `_v1rule/ALL ⊆ TopUniverseV2/ALL` 위반 0, 거래소 leg로 빠진 운영기업 4개(DSL·QGP·ACLO·AX)는 전부 2009년 이전 상폐로 v1 멤버일 0.

### 6-3. 배당·스핀오프·분할 테이블 (T2, `Distributions/`)
- `distributions.parquet` 488,746행(키 = tradingItemId·exDate·kind·leg, 82컬럼): 현금 475,912(ok 448,416 / 마스터 밖 종목 등 no_price 27,496), 분할 11,893, 스핀오프 941행 = 885사건(다종목 분배는 leg별 행).
- 스핀오프 상태(사건 기준): ok 351 / 교환비 미확보 325 / 가격 없음 150 / 가치 불일치(계수 기반 분배가치 vs 신설사 가격×비율 차이 >3%p) 105 / 신설사 id 미확보 10. ok 행은 전부 출처가 있음(DB type-20 252, co_acthist 문구 68, 웹 31). **v2 Top500 2010+ 스핀오프 112건: ok 91 / 가치 불일치 12 / 교환비 미확보 6 / 가격 없음 3**(리드 스팟체크).
- 웹·수동 조사 53건은 `Distributions/research_log.md`에 사건별 질의·URL·발견 조건·신뢰도 기록(해결 42, 확인 2, id만 5, 비율만 2, 미해결 2: ETN/Axcelis·ZWS/Regal 비율, LCAP.A·C→Travelers는 xfeed에 라인 없음). 비율을 지어낸 행 없음. 남은 미확정은 Top500 32건(교환비 21·id 3·불일치 18 중 일부 중복)·Top1000 62건이 로그에 행 단위로 나열됨.
- 검증 사례: COST 10.0(현금), eBay→PYPL 1:1(계수 0.579 vs 가격 0.611, 3.1%p라 value_mismatch 표시이지만 비율은 확정), ITT→XLS·XYL 각 1:1 + 당일 1:2 역분할(캐시단위 2.0), Liberty→LMCA 1:1(캐시단위 3.0), DuPont→Qnity 0.5(2026년 1:3 역분할 반영 1.5), NiSource→CPGX 1:1(벤더 계수는 현금등가 27.93 사용), TFCF.A→FOXA 1/3(디즈니 0.4517 DIS 몫은 정산 파일 override에 남음, `tfcf_note.md`).
- T1 DivData와 교차검증: USD 금액 448,264/448,266 일치, 캐시단위 89셀 불일치(같은 날 주식배당·factor 채움 차이, CZFS 등) — 소비 시 `cashPerShareCacheUnit`은 Distributions 쪽을 우선하고 차이는 후속 정리.
- 계약과 다른 점: `ratioCacheUnit`은 배당락 **전날** 마지막 종가의 adjustmentFactor 사용(당일 역분할 정합), 현금은 배당락일 factor(없으면 직전), 다리별 행·추가 진단 컬럼(`*Db`, `mismatchGap/Class`, `impliedValueFracExCash` 등).

## 7. 사용자가 읽을 파일
| 파일 | 내용 |
|---|---|
| `/home/jgo/61/data2/temp_stock/cache_v2_cash_1d/README.md` | v2 파일·단위·규칙 설명 |
| `.../Master/build_report.md` | 빌드 수치·검증값·v1 대비 커버리지 |
| `.../AUDIT.md` | 독립 감사 9항목 + 수정 후 재검증, "What a consumer must know" |
| `.../Distributions/distributions_report.md` | 배당·스핀오프·분할 집계와 검증 사례 |
| `.../Distributions/research_log.md` | DB 밖에서 찾아 넣은 스핀오프 조건 53건(출처 URL·신뢰도)과 미확정 목록 |
| `.../TopUniverseV2/fund_exclusions.csv` | 전체 변형 유니버스에서 걸러낸 펀드·ETN 목록 |
| `docs/cache_v2_plan_2026-09-14.md` (이 문서) | 결정·근거·상태 |

## 8. 다음 단계
1. 엔진 opt-in `distributions` 입력(현금 다리: 롱 +·숏 −, 스핀오프: 원 종목 유지 + 신설사 부분 이전, 미평가는 상태 표시) + 정산 파일을 v2 캐시 앵커로 재생성(`build_events.py --close-cache/--universe-dir`).
2. `longshort_platform` 비교 노트북: ① 같은 일별 비중 고정 → v1 스냅샷 vs v2(분할 기준 → 배당 → 스핀오프 순 층별 델타) ② v2로 알파·유니버스 재계산. 결과 표는 §5.
3. 미확정 스핀오프(Top500 32건) 추가 조사, 팀 협의 후 v1 재생성 여부 결정. 세션 스테이지 1.9GB(`scratchpad/cache_v2_stage`)는 재실행 필요 없으면 삭제 가능.

## 9. 비교 연구 착수 (2026-09-14 밤, 사용자 지시 "포지션·MDD·샤프 등 달라지는 부분 연구")
브리프 `scratchpad/orch/BRIEF_compare_v1_v2.md`. 산출물: `/home/jgo/longshort/cache_v1_vs_v2/{cache_v1_vs_v2.ipynb, compare_lib.py, FINDINGS.md}` (연구 폴더; 플랫폼 레포 아님), 큰 중간물은 61 `temp_stock/compare_v1_v2/`.

층 정의(알파 5종 × Top500·Top1000[·ALL]):
| 층 | 가격 | 유니버스 | 정산 | 계산 |
|---|---|---|---|---|
| L0 | v1 스냅샷 | TopUniverseV1 | v1 events | 엔진 + 파이썬 원장(1e-9 일치 확인) |
| L1 | v2 | TopUniverseV2_v1rule | v2 재생성 events | 엔진 + 원장 → 가격 기준·정산 앵커 효과 |
| L2 | v2 | TopUniverseV2 | v2 events | 엔진 + 원장 → 커버리지(OTC 이동 종목) 효과 |
| L3 | L1/L2 + 현금배당(배당락일, 롱 +·숏 −) | | | 원장 재평가 |
| L4 | L3 + 스핀오프(status ok만, 배당락일 종가로 현금화 가정) | | | 원장 재평가 |
| FIX | L0의 v1 비중 고정 → v2 가격·배당·스핀오프로 재평가 | | | "같은 매매의 손익 차이" |
| SIG | L1/L2 vs L0 포지션(일별 Jaccard·비중 상관·Σ|Δw|)·손익 | | | "다른 매매" 효과 |

리드 결정: 배당은 배당락일 인식(지급일 미모델), 스핀오프 수령 주식은 배당락일 종가로 현금화, 층 분해는 기본 R=0, v1 기준선은 고정 스냅샷. 역할: A(v2 events 재생성·설정·엔진 실행), B(원장 커널·노트북·FINDINGS), C(감사: 룩어헤드·이중계산·Sharpe n·MDD 정의).

### 9-1. A 결과 (엔진 실행·v2 정산 파일)
- v2 events 8,693행(빌더 무변경, 마스터 인덱스 래퍼만), 불변식·리드 검사 PASS, 캐시단위 환산 = v2 adjustmentFactor 나눗셈(4,008행 최대 상대오차 7.6e-8).
- **경제 조건 동일 가정 깨짐**: 공통 6,192 id 중 448행이 endDate(327)·승계사 id(106)·비율/주식다리(85)·settlementReturn(186)에서 다름. 원인은 전부 빌더의 캐시 의존 결정(종료일 캡 = 마지막 캐시 호가, 정산 bar = 캐시 결측, 인수사 라인 = 캐시 호가 존재, 개명 승계 = 캐시 열 존재). v1 Top500 멤버 7종목(BHI·PARA·VIAB·CTXS·RRD·PDE·QRTE.A). → L1−L0에는 정산 조건 성분이 섞임(귀속에서 분리).
- **v2 연속 호가 라인 74개**: 상폐된 id가 승계사 가격 기준으로 계속 호가(BHI 2017-07-05 −35% 가짜 일수익률, Top500 마스크 안, 정산 안 됨). 조사·정량화·절단 변형은 B/C 과제.
- Altaba(AABA): v1 규칙 통과인데 v1 캐시에 없던 135종목 중 하나, L1에서 기본 R로 정산(afl_100 Top500 북의 13.7%, 2019-10-11) → L1에도 작은 커버리지 효과 존재, 순수 확장 효과는 L2−L1.
- 엔진 실행 90/90(L0/L1/L2 × Top500/Top1000/ALL × 5알파 × R{0,−1}) 가드 거부 0, 총 2분 36초. Top500 R=0(n=4,198): 대부분 알파는 L0→L1→L2 변화가 작고(afl_963 Sharpe 0.390/0.369/0.374), close_tsmean20_zscore는 vol 0.041→0.057·MDD −0.53→−0.80로 커짐(귀속 대기), afl_100 기본 R 노출 0.008→0.142(Altaba).
- 하네스에 opt-in 패치 2건(`--timeseries-dir`, events 경로 메타) — `engine_runs/harness_patch.diff`, self-test 33/33.
- A 추가 조사: (a) 연속 호가 74라인의 원인은 벤더가 상폐 id 밑에 **생존사(승계사)의 행을 securityenddate까지 계속 게시**하는 것(BHI 2592597의 2017-07-05~31 행은 BKR 433925520의 종가·거래량과 동일; id 재사용 아님). v1에 없는 이유는 v1 원시 파일이 2025-02-06 일괄 적재 시점에 고정됐고 그 미러 행은 그 뒤에 xfeed에 들어왔기 때문(같은 SQL을 오늘 돌리면 둘 다 나옴). 엔진 영향은 Top500·Top1000에서 **BHI 한 종목·2017-07-05 한 bar**(L1−L0 = w_BHI×(−0.354−(−0.051)), 5알파 모두 1e-5 내 확인), 나머지 73은 점프 bar에 마스크 밖. v2 재빌드 시 수정안: 상폐 라인의 행을 co_acthist 기록일 이후(또는 승계사 종가와 같아지는 첫 bar부터) 제거. 목록 `compare_v1_v2/events_v2/continuation_lines.csv`.
  (b) Altaba: v1 규칙은 통과하지만 2025-02-06 적재 결과에 없던 라인(현재 DB 복사본으로는 원인 관측 불가; 비슷한 135종목은 대부분 when-issued·ETF·2025~26 신규). afl_100(=HL52W 52주 고저폭)이 $51.50 청산배당 후 0.66→259.6으로 튀어 z=14.4 → 비중 13.7%가 된 **팩터 미조정 아티팩트**. DB엔 2019-09-24 $51.50(type 4)만 있고 상폐 후 청산배당은 없음 → R=0은 19.63 회수 가정. SEC 근거로 override 제안 조사 중(`docs/altaba_liquidation_evidence_2026-09-14.md`).
- Altaba 근거 조사 완료(`docs/altaba_liquidation_evidence_2026-09-14.md`): 상폐 후 청산분배 11회 합계 **$22.38/주**(전부 8-K Ex-99.1 인용, 누계 73.88은 IR 누계·발행주식수와 정합), 마지막 종가 19.63 → 무할인 R **+14.0%**(지급시점 IRR 7.0%), 청산 미종결(2026-05 NAV 1.62 잔존). override 행 제안(적용은 v2 마스터 경로에서만 가능, LIQ 코드의 −1 힌트는 리뷰 필요). 적용 순서: B 1차 노트북 → v2 events에 override 반영·엔진 L1/L2 재실행 → 노트북 재실행 → C 감사.
- Altaba override 적용본(별도 경로 `compare_v1_v2/events_v2_altaba/`, `engine_runs_altaba/`, 리포 CSV 무변경): 엔진이 `cashPerShareCache` 22.38로 R=+14.01% 계산(정산 bar 10-11→10-03), LIQ의 −1 힌트는 미사용. 영향은 afl_100에 집중 — Top500 R=0 Sharpe −0.177→−0.165·MDD −0.559→−0.539(L1), R=−1에서는 −0.241→−0.162·−0.693→−0.536; 기본 R 노출 6종목 0.142 → 5종목 0.0056. afl_963은 비트 동일. 최종 노트북은 override 적용본을 L1/L2로 쓰고 적용 전 값을 "백필 전"으로 병기.
