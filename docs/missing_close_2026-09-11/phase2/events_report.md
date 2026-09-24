# 정산 이벤트 파일 보고 (2026-09-11 20:35)

생성 명령: `build_events.py --master /home/jgo/longshort_platform/data/master/uss_cash_master_refreshed.parquet --out /home/jgo/longshort_platform/data/events/events.parquet --override /home/jgo/longshort_platform/data/events/events_override.csv --start 2009-01-01 --window 200`

## 1. 규칙

- `endDate` = **로컬 마스터 `securityEndDate`**(data-master 갱신본) 우선, 비어 있으면 `xfl_target.ciqsecurity.securityenddate`. 엔진은 이 날짜 **당일까지 보유**하고 다음 bar에서 정산함 (`parquet_reader.cpp:406` `upper_bound(day_end_ms)`).
- `settlementDate` = `co_acthist.datadate`(사건 기록일), 매칭 실패 시 `endDate`.
- co_acthist 매칭 창 = `|datadate − endDate| ≤ 200`일. 여러 건이면 **가장 가까운 datadate** (동률은 ACQ/MGD/LBO/PVT → BK*/LIQ → 기타 순) 1건만 채택함.
- `settlementReturn = (cash + ratio × acquirerClose) / lastClose − 1`. `lastClose`의 **날짜**는 로컬 캐시 `BaseData/close.parquet`에서 endDate 이하 마지막 유효 종가가 있는 bar(= 엔진이 정산 bar에 들고 있는 mark의 날짜)이고, **값**은 그 날짜의 as-traded 가격임(아래 가격 소스 참조). 캐시에 종가가 아예 없으면 `ciqpriceequity` 30일 소급.
- `acquirerClose`는 **정산 bar**(종료일 다음 거래일) 이후 첫 유효 종가(10일 이내). 엔진이 결측 구간 동안 mark를 동결했다가 정산 bar에서 한 번에 인식하는 규약과 맞추기 위함임 — 예: DFS 마지막 종가는 2025-05-16(200.05)이고 정산 bar는 2025-05-23이라 그 사이 COF 하락이 R에 들어감.
- **가격 소스**: 두 종가 모두 `ciqpriceequity.priceclose × adjustmentfactor`(그날 실제 거래된 가격)를 씀. 캐시는 수집 당일의 raw `priceclose`를 그대로 담고 있는데, CIQ는 나중에 분할·교환이 생기면 `priceclose`를 현재 주식 기준으로 재작성하고 `adjustmentfactor`에 환산값을 남김(GOOG 2015-10-05: 32.0735 × 20 = 641.47 = 캐시값). 그래서 캐시와 DB가 어긋나는 종목이 정확히 정산에서 두 종목을 비교해야 하는 종목들임. R은 비율이라 스케일이 상쇄되므로 **두 다리를 모두 as-traded로 통일**하는 것이 옳고, 캐시는 *어느 bar*에서 mark·정산하는지 정하는 데만 씀. override 4건은 factor=1.0이라 캐시값을 그대로 씀.
- BK11/BK7/BKR/LIQ는 `suggestedReturn=-1`, `settlementReturn`은 NaN(정책은 엔진 config가 결정).
- `--start 2009-01-01` 이전 종료일은 파일에서 제외함(전체 종료일 21123건 중 14709건).

## 2. 대상·매칭

| 항목 | 값 |
|---|---|
| 로컬 마스터 행 | 28210 |
| 종료일 있는 종목(로컬∪DB) | 21123 |
| 종료일 ≥ 2009-01-01 | 6414 |
| 로컬 null → DB에만 있음 | 0 |
| DB null → 로컬에만 있음 | 6 |
| 로컬·DB 종료일 불일치 | 29 |
| 출력 행수 | 6414 |
| 사건 매칭(co_acthist+override) | 5166 (80.5%) |
|   · (gvkey,iid) 정확 매칭 | 5151 |
|   · oldtic1 보조 매칭 | 11 |
|   · override | 4 |
|   · 매칭 없음(종료일만) | 1248 |
| override 적용 | 4건 적용 (/home/jgo/longshort_platform/data/events/events_override.csv) |

### actionCode별 매칭·산출

| actionCode | 행수 | settlementReturn 유한 | successor 있음 |
|---|---|---|---|
| ACQ | 3898 | 2891 (74%) | 962 |
| (매칭없음) | 1248 | 0 (0%) | 0 |
| LIQ | 620 | 0 (0%) | 0 |
| N/C | 566 | 277 (49%) | 279 |
| PVT | 45 | 18 (40%) | 0 |
| BK11 | 30 | 0 (0%) | 0 |
| SPN | 4 | 0 (0%) | 0 |
| DFF | 1 | 0 (0%) | 0 |
| OTH | 1 | 0 (0%) | 0 |
| SEC | 1 | 0 (0%) | 0 |

## 3. 조건 파싱

매칭된 5166건 중 조건(현금 또는 교환비) 확보 3302건 (63.9%), `settlementReturn` 유한 3186건 (49.7% of 전체).

| 파싱 결과 | 건수 | 매칭 대비 |
|---|---|---|
| cash | 2021 | 39.1% |
| stock | 740 | 14.3% |
| not_a_settlement | 657 | 12.7% |
| no_text | 484 | 9.4% |
| unparsed | 436 | 8.4% |
| no_terms | 359 | 6.9% |
| mixed | 293 | 5.7% |
| election | 136 | 2.6% |
| foreign_currency | 20 | 0.4% |
| multi_class | 16 | 0.3% |
| override | 4 | 0.1% |

## 4. settlementReturn 분포

| 구분 | n | 평균 | p05 | p25 | 중앙 | p75 | p95 |
|---|---|---|---|---|---|---|---|
| 전체 | 3186 | 0.0002 | -0.0557 | -0.0005 | 0.0004 | 0.0040 | 0.0524 |
| 현금 전용 딜 | 1998 | -0.0029 | -0.0208 | 0.0000 | 0.0003 | 0.0012 | 0.0080 |
| 주식·혼합 딜 | 1188 | 0.0055 | -0.0931 | -0.0121 | 0.0040 | 0.0206 | 0.1044 |

현금 딜의 R은 마지막 거래일 종가와 인수대금 차이라 0 근처여야 함.

남아 있는 이상치는 대부분 (a) CVR·조건부 대가가 붙어 현금 명시분이 실제 대가보다 작은 딜(TOBIRA $28.35+CVR, SGTX $14.92+CVR), (b) 공개매수가 불확실해 마지막 호가가 인수가보다 낮았던 딜(NTSU 마지막 종가 90.34 → $109), (c) 벤더 원문 오기임. 파싱으로 더 줄일 수 있는 건은 아님.


**|R| > 10% 현금 딜 이상치: 38건 / 1998건 (1.9%)**

| tradingItemId | endDate | code | cash | lastClose | R | actionText |
|---|---|---|---|---|---|---|
| 61528180 | 2018-10-18 | ACQ | 13.5 | 9.1 | 0.484 | MLCJR LLC ACQUIRED ENERGY XXI GULF COAST INC. FOR $13.5 PER SHARE |
| 2658958 | 2020-12-21 | ACQ | 0.13 | 0.246 | -0.472 | TAT MERGER SUB LLC (PRIVATE) ACQUIRED TRANSATLANTIC PETROLEUM LTD. FOR $0.13 PER |
| 2655520 | 2025-12-09 | ACQ | 0.09 | 0.1515 | -0.406 | SUPERIOR INDUSTRIES INTERNATIONAL INC ACQUIRED BY OAKTREE CAPITAL MANAGEMENT LP  |
| 706408678 | 2025-11-20 | ACQ | 1.04 | 1.74 | -0.402 | XOMA ROYALTY CORP ACQUIRED LAVA THERAPEUTICS NV FOR $1.04, SPC SPL DIV=[$1.04-$1 |
| 690983043 | 2022-03-01 | PVT | 14.05 | 10.04 | 0.399 | BURGUNDY TECHNOLOGY ACQUISITION CORP LIQUIDATED FOR $14.05 PER SHARE. |
| 288282133 | 2025-10-08 | ACQ | 0.1242 | 0.2012 | -0.383 | XENOTHERAPEUTICS INC ACQUIRED ESSA PHARMA INC FOR $0.1242 APPROXIMATELY PER SHAR |
| 10015538 | 2017-02-17 | ACQ | 56 | 40.71 | 0.376 | BAIN CAPITAL PRIVATE EQUITY (PRIVATE) ACQUIRED BLUE NILE INC FOR $56.00 PER SHAR |
| 689337092 | 2025-06-20 | ACQ | 0.57 | 0.88 | -0.352 | CONCENTRA BIOSCIENCES LLC (A PRIVATE ENTITY) ACQUIRED KRONOS BIO INC FOR $0.57 A |
| 695262585 | 2023-08-10 | ACQ | 14.92 | 22.47 | -0.336 | LLY ACQUIRED SGTX FOR $14.92 IN CASH FOR EACH SHARE HELD. |
| 1817015587 | 2025-10-06 | ACQ | 4.04 | 6 | -0.327 | MONOGRAM TECHNOLOGIES INCACQUIRED BY ZIMMER BIOMET HOLDINGS INC FOR $4.04 IN CAS |
| 268941271 | 2016-11-01 | ACQ | 28.35 | 42.09 | -0.326 | ALLERGAN PLC ACQUIRED TOBIRA THERAPEUTICS INC FOR $28.35 PER SHARE. |
| 142493385 | 2017-10-02 | ACQ | 8.5 | 12.46 | -0.318 | IAC/INTERACTIVECORP ACQUIRED ANGIE'S LIST INC. FOR $ 8.50 PER SHARE. |
| 328762288 | 2017-07-21 | ACQ | 1.75 | 2.35 | -0.255 | GURNET POINT CAPITAL LIMITED ACQUIRED INNOCOLL HOLDINGS PLC. FOR $1.75 PER SHARE |
| 37848218 | 2009-02-25 | ACQ | 2 | 2.64 | -0.242 | MEDICINES COMPANY ACQUIRED TARGANTA THERAPEUTICS CORP FOR $2.00 PER SHARE. |
| 374257723 | 2023-01-23 | ACQ | 3.92 | 5 | -0.216 | TRIPLE FLAG PRECIOUS METALS CORP ACQUIRED MAVERIX METALS INC FOR  $3.92, |
| 39594742 | 2016-11-07 | ACQ | 109 | 90.34 | 0.207 | ORACLE CORP ACQUIRED NETSUITE INC FOR $109.00 PER SHARE. |
| 2620557 | 2009-03-24 | ACQ | 4.5 | 5.6 | -0.196 | ENDO PHARMACEUTICALS HOLDINGS INC ACQUIRED INDEVUS PHARMACEUTICALS INC FOR $4.50 |
| 669258409 | 2026-02-06 | ACQ | 4.2913 | 5.34 | -0.196 | XOMA ROYALTY CORP ACQUIRED GENERATION BIO CO FOR $4.2913. |
| 697906179 | 2023-09-22 | ACQ | 4 | 4.91 | -0.185 | REGENERON PHARMACEUTICALS ACQUIRED DECIBEL THERAPEUTICS INC FOR $4 PER SHARE. |
| 6137152 | 2009-06-24 | ACQ | 1 | 0.85 | 0.176 | HARTFORD FINANCIAL SERVICES GROUP INC ACQUIRED FEDERAL TRUST CORP FOR $1.00 PER  |
| 633694984 | 2023-06-07 | ACQ | 0.91 | 1.1 | -0.173 | SHIN NIPPON BIO LAB (GLOBAL) ACQUIRED SATSUMA PHARMACEUTICALS INC FOR  $0.91, |
| 2640270 | 2013-04-10 | ACQ | 1.65 | 1.99 | -0.171 | STARBUCK BANCSHARES INC ACQUIRED PREMIERWEST BANCORP FOR $1.65 PER SHARE. |
| 670828381 | 2026-01-27 | ACQ | 2.1986 | 2.65 | -0.170 | XENOTHERAPEUTICS INC (PRIVATE) ACQUIRED REPARE THERAPEUTICS INC   FOR $2.1986 |
| 32561003 | 2013-10-25 | ACQ | 10.75 | 12.78 | -0.159 | CUBIST PHARMACEUTICALS INC ACQUIRED OPTIMER PHARMACEUTICALS INC FOR $10.75 PER S |
| 243030327 | 2017-12-11 | ACQ | 1.52 | 1.79 | -0.151 | MALLINCKRODT PLC ACQUIRED OCERA THERAPEUTICS INC. FOR $1.52 PER SHARE. |

### R을 만들지 못한/버린 사유

| 사유 | 건수 |
|---|---|
| rename_not_1to1 | 169 |
| no_acquirer_quote | 81 |
| R_out_of_range | 41 |
| cash_R_out_of_range | 25 |

`cash_R_out_of_range`(현금 딜 |R| > 50%)·`R_out_of_range`(|R| > 50%)는 원문 오기·집계액 오인식이 대부분이라 `settlementReturn`을 NaN으로 두고 successor도 지움(엔진 기본 정산으로 떨어뜨림).

버려진 상위 15건(마지막 종가 기준):

| tradingItemId | symbol | endDate | code | cash | lastClose | actionText |
|---|---|---|---|---|---|---|
| 2664456 | CWEI | 2017-04-24 | ACQ | 138.39 | 131.9 | NOBLE ENERGY INC. ACQUIRED CLAYTON WILLIAMS ENERGY INC. SHAREHOLDERS OF CLAYTON  |
| 333845943 | ACIA | 2021-02-26 | ACQ | 0.01 | 115 | CISCO SYSTEMS INC ACQUIRED ACACIA COMMUNICATIONS INC FOR $0.01 PER SHARE |
| 1681264344 | ML | 2025-04-16 | ACQ | 2 | 85.9 | MONEYLION INC ACQUIRED BY GEN DIGITAL INC FOR $2.00 PER EACH SHARE HELD. |
| 2629766 | MDP | 2021-12-01 | ACQ | 16.99 | 59.07 | GRAY TELEVISION ACQUIRED MEREDITH CORP. FOR {$16.99 PER SHARE}. |
| 34969540 | TYC | 2016-09-06 | ACQ | 6.06 | 48.9 | JOHNSON CONTROLS INC ACQUIRED TYCO INTERNATIONAL PLC THROUGH A REVERSE MERGER. S |
| 9917918 | ATLS | 2011-02-17 | ACQ | 38.25 | 46.46 | CHEVRON CORP ACQUIRED ATLAS ENERGY INC.  SHAREHOLDERS OF ATLAS ENERGY INC RECEIV |
| 141575791 | ASBB | 2017-09-29 | ACQ | - | 45.1 | FIRST BANCORP/NC ACQUIRED ASB BANCORP INC. SHAREHOLDERS OF ASB BANCORP INC. RECE |
| 2666215 | SINA | 2021-07-09 | ACQ | 0.04 | 43.26 | NEW WAVE MERGERSUB LTD ACQUIRED SINA CORP, FOR $0.04 PER SHARE. |
| 2640342 | PDE | 2011-05-31 | ACQ | 15.6 | 40.7 | ENSCO PLC ACQUIRED PRIDE INTERNATIONAL INC.  SHAREHOLDERS OF PRIDE INTERNATIONAL |
| 2655068 | SCNB | 2017-03-31 | ACQ | 40.495 | 40.41 | PEOPLE'S UNITED FINL INC ACQUIRED SUFFOLK BANCORP.  SHAREHOLDERS OF SUFFOLK BANC |
| 83039012 | WBMD | 2009-10-23 | ACQ | - | 34.28 | HLTH CORP ACQUIRED WEBMD HEALTH CORP THROUGH A REVERSE MERGER.  SHAREHOLDERS OF  |
| 2586852 | ATVI | 2023-10-12 | ACQ | 95 | 30.07 | MICROSOFT CORP ACQUIRED ACTIVISION BLIZZARD INC FOR $95.00 PER SHARE. |
| 257101816 | TIER | 2019-06-14 | ACQ | - | 28.88 | CUZ ACQUIRED TIER AND SHAREHOLDERS OF TIER WILL RECEIVE 2.98 SHARES OF CUZ FOR E |
| 30175602 | CFCB | 2017-06-09 | ACQ | 9.3625 | 27.73 | MIDLAND STATES BANCORP ACQUIRED CENTRUE FINANCIAL CORP.  SHAREHOLDERS OF CENTRUE |
| 2611810 | FCBS | 2017-03-29 | ACQ | 22.5 | 23.08 | SUMMIT FINANCIAL GROUP INC. ACQUIRED FIRST CENTURY BANKSHARES INC. SHAREHOLDERS  |


**전체(현금·주식·혼합) |R| > 20%: 36건 / 3186건 (1.1%)** — 상위 12건

| tradingItemId | endDate | code | cash | ratio | R | actionText |
|---|---|---|---|---|---|---|
| 61528180 | 2018-10-18 | ACQ | 13.5 | - | 0.484 | MLCJR LLC ACQUIRED ENERGY XXI GULF COAST INC. FOR $13.5 PER SHARE |
| 2658958 | 2020-12-21 | ACQ | 0.13 | - | -0.472 | TAT MERGER SUB LLC (PRIVATE) ACQUIRED TRANSATLANTIC PETROLEUM LTD. FOR |
| 34861577 | 2017-06-01 | ACQ | - | 0.671 | -0.444 | B. RILEY FINANCIAL INC. ACQUIRED FBR & CO. SHAREHOLDERS OF FBR & CO RE |
| 34160070 | 2018-07-09 | ACQ | - | 2.1757 | -0.428 | MRVL ACQUIRED CAVM AND CAVM RECEIVED 2.1757 STOCK SHARES OF MRVL FOR E |
| 2655520 | 2025-12-09 | ACQ | 0.09 | - | -0.406 | SUPERIOR INDUSTRIES INTERNATIONAL INC ACQUIRED BY OAKTREE CAPITAL MANA |
| 706408678 | 2025-11-20 | ACQ | 1.04 | - | -0.402 | XOMA ROYALTY CORP ACQUIRED LAVA THERAPEUTICS NV FOR $1.04, SPC SPL DIV |
| 690983043 | 2022-03-01 | PVT | 14.05 | - | 0.399 | BURGUNDY TECHNOLOGY ACQUISITION CORP LIQUIDATED FOR $14.05 PER SHARE. |
| 288282133 | 2025-10-08 | ACQ | 0.1242 | - | -0.383 | XENOTHERAPEUTICS INC ACQUIRED ESSA PHARMA INC FOR $0.1242 APPROXIMATEL |
| 10015538 | 2017-02-17 | ACQ | 56 | - | 0.376 | BAIN CAPITAL PRIVATE EQUITY (PRIVATE) ACQUIRED BLUE NILE INC FOR $56.0 |
| 33914308 | 2009-11-12 | ACQ | - | 0.0659631 | -0.373 | INTELLIPHARMACEUTICS INTERNATIONAL INC ACQUIRED VASOGEN INC. SHAREHOLD |
| 2586489 | 2009-07-02 | ACQ | - | 0.019442 | 0.361 | UNIFY CORP ACQUIRED AXS-ONE INC.  SHAREHOLDERS OF AXS-ONE INC RECEIVED |
| 2640066 | 2009-03-31 | ACQ | - | 0.335485 | 0.357 | ALLIANCE HEALTHCARD INC ACQUIRED ACCESS PLANS USA INC.  SHAREHOLDERS O |

### 리뷰 지적 반영 (review_p2_report.md §1)

P2-01(`CASH EQUIVALENT OF $x`를 현금 다리로 오파싱), P2-02(`CAN$`/`CDN$` 미필터), P2-18(`0.475 PLD SHARES FOR EVERY ...` 미파싱)을 고친 뒤 해당 행의 변화임. `before`는 수정 전 2026-09-11 빌드 값.

| 종목 | tradingItemId | 지적 | before R | after R | cash | ratio | 리뷰 기대 |
|---|---|---|---|---|---|---|---|
| CMA | 2601589 | P2-01 cash equivalent | +0.1504 | +0.0934 | - | 1.8663 | +0.0934 (1.8663x51.95/88.67-1) |
| DISH | 40272951 | P2-01 cash equivalent | -0.1216 | -0.1292 | - | 0.350877 | 주식 전액 |
| PXD | 2639311 | P2-01 cash equivalent | +0.0013 | -0.0004 | - | 2.3234 | 주식 전액 |
| CADE | 542341937 | P2-01 cash equivalent | +0.0783 | +0.0509 | - | 2.475 | 주식 전액 |
| RPT | 2642029 | P2-01 cash equivalent | +0.0080 | +0.0033 | - | 0.6049 | 주식 전액 |
| IS | 1671892993 | P2-01 cash equivalent | -0.0112 | -0.0151 | - | 0.1089 | 주식 전액 |
| MNRL | 606938070 | P2-01 cash equivalent | NaN | NaN | - | 1.133 | 주식 전액 |
| SJR | 2650011 | P2-02 CAN$ | +0.3419 | NaN | - | - | CAD 대가 → NaN |
| KOR | 141549237 | P2-02 CAN$ | +0.2773 | NaN | - | - | CAD 대가 → NaN |
| PNI | 29975856 | P2-02 CDN$ | +0.0897 | NaN | - | - | CAD 대가 → NaN |
| SVLC | 192288682 | P2-02 CDN$ | +0.1808 | NaN | - | - | CAD 대가 → NaN |
| SILU | 24884901 | P2-02 CDN$ | +0.0618 | NaN | - | - | CAD 대가 → NaN |
| HTE | 23181585 | P2-02 CDN$ | +0.0627 | NaN | - | - | CAD 대가 → NaN |
| XG | 128444415 | P2-02 CDN$ | -0.0016 | NaN | - | - | CAD 대가 → NaN |
| DRE | 2607489 | P2-18 티커+SHARES | NaN | +0.0284 | - | 0.475 | 0.475 PLD |

`excluded_equivalent`(환산 문구 금액을 대가에서 제외) 표시 행: 30건.

## 5. 손계산 대조

리드 확인값은 **조건(현금·교환비·인수사)** 이므로 그 세 값을 대조함. R은 조건에서 나오는 파생값이고, 분모가 엔진이 실제로 들고 있는 mark(종료일 이하 마지막 유효 종가)라 마지막 호가와 정산 bar 사이 인수사 주가 변동이 그대로 들어감.

| 종목 | tradingItemId | endDate | code | 파싱 조건 | 가격 | R | 리드 확인값 | 일치 |
|---|---|---|---|---|---|---|---|---|
| DFS | 34969516 | 2025-05-22 | ACQ | cash=-, ratio=1.0192, succ=COF | mark=200.1, acq=185.1 | -0.0571 | 1.0192 COF | O |
| HES | 27143925 | 2025-07-18 | ACQ | cash=-, ratio=1.025, succ=CVX | mark=149, acq=149.7 | 0.0299 | 1.025 CVX | O |
| ANSS | 2590144 | 2025-07-17 | ACQ | cash=199.91, ratio=0.3399, succ=SNPS | mark=374.3, acq=584.8 | 0.0651 | $199.91 + 0.3399 SNPS | O |
| CYBR | 267935529 | 2026-02-10 | ACQ | cash=45.0, ratio=2.2005, succ=PANW | mark=408.9, acq=165.3 | -0.0003 | $45 + 2.2005 PANW | O |
| EXAS | 2610459 | 2026-03-23 | ACQ | cash=105.0, ratio=-, succ=- | mark=104.9 | 0.0009 | $105 (override) | O |
| EA | 2608773 | 2026-08-05 | ACQ | cash=210.0, ratio=-, succ=- | mark=209.7 | 0.0014 | $210 (override) | O |
| AVB | 2591859 | 2026-08-14 | ACQ | cash=-, ratio=2.793, succ=VMRK | mark=184.1, acq=64.03 | -0.0284 | 2.793 VMRK (override) | O |
| EQR | 2610085 | 2026-08-17 | N/C | cash=-, ratio=1.0, succ=VMRK | mark=63.66, acq=64.03 | 0.0058 | N/C 1.0 VMRK (override) | O |

조건 일치 8/8건.

## 6. 실패 사례 상위 20건

매칭은 됐으나 `settlementReturn`을 만들지 못한 건(보유 마지막 종가 기준 내림차순).

| tradingItemId | symbol | endDate | code | 파싱 | lastClose | actionText |
|---|---|---|---|---|---|---|
| 2663799 | WSC | 2011-06-24 | ACQ | election | 384.5 | BERKSHIRE HATHAWAY INC ACQUIRED WESCO FINANCIAL CORP.  SHAREHOLDERS OF WESCO FINANCIAL COR |
| 2654526 | BH | 2018-05-01 | N/C | unparsed | 342.3 | CHANGE TO GUIDEABR NAME ONLY.5/1/2018 COM CHANGE TO CL B AFTER REORGANIZATION |
| 47439862 | IAC | 2020-07-01 | N/C | no_text | 323.4 | nan |
| 2623613 | KSU | 2022-04-29 | ACQ | unparsed | 293.6 | CANADIAN PACIFIC RAILWAY LIMITED ACQUIRED KANSAS CITY SOUTHERN RECEIVED STOCK SHARES OF KA |
| 2601285 | COHR | 2022-06-30 | ACQ | unparsed | 266.2 | II-VI INC AQUIRED COHERENT INC  AND RECEIVED STOCK SHARES OF COHERENT INC |
| 2638830 | PCYC | 2015-05-22 | ACQ | election | 261.2 | ABBVIE INC ACQUIRED PHARMACYCLICS INC.  SHAREHOLDERS OF PHARMACYCLICS INC RECEIVED EITHER  |
| 413474314 | LLL | 2019-06-28 | ACQ | unparsed | 245.2 | HARRIS CORP ACQUIRED L3 TECHNOLOGIES INC. RECEIVED STOCK SHARES OF HARRIS CORP FOR EACH SH |
| 2665190 | XLNX | 2022-02-11 | ACQ | unparsed | 194.9 | ADVANCED MICRO DEVICES INC AQUIRED XILINX INC AND RECEIVED STOCK SHARES OF XILINX INC |
| 244652395 | AGN | 2020-05-11 | ACQ | unparsed | 193 | ABBVIE INC. ACQUIRED ALLERGAN PLC. RECEIVED CASH AND STOCK SHARES OF ABBV FOR EACH SHARE H |
| 2587941 | ALXN | 2021-07-26 | ACQ | unparsed | 182.5 | ASTRAZENECA PLC AQUIRED ALEXION PHARMACEUTICALS INC AND RECEIVED STOCK SHARES OF ALEXION P |
| 2597014 | CCMP | 2022-07-05 | ACQ | unparsed | 173.7 | ENTEGRIS INC AQUIRED CMC MATERIALS INC  AND RECEIVED STOCK SHARES OF CMC MATERIALS INC |
| 115913443 | IPHI | 2021-04-20 | ACQ | unparsed | 172.3 | MARVELL TECHNOLOGY GROUP LTD AQUIRED INPHI CORP AND RECEIVED STOCK SHARES OF INPHI CORP |
| 237173349 | DATA | 2019-08-02 | ACQ | unparsed | 169.5 | CRM ACQUIRED TABLEAU SOFTWARE INC. RECEIVED STOCK SHARES OF CRM FOR EACH SHARE HELD. |
| 271907900 | PRAH | 2021-07-01 | ACQ | unparsed | 165.2 | ICON PLC AQUIRED PR A HEALTH SCIENCES INC AND RECEIVED STOCK SHARES OF PR A HEALTH SCIENCE |
| 2657553 | ANDV | 2018-09-28 | ACQ | election | 153.5 | MARATHON PETROLEUM CORP ACQUIRED ANDEAVOR. SHAREHOLDERS OF ANDEAVOR RECEIVED $152.27 CASH  |
| 36296353 | VMW | 2023-11-22 | ACQ | unparsed | 142.5 | BROADCOM INC ACQUIRED VMWARE INC -CL A. |
| 285243068 | ATK | 2015-02-10 | N/C | no_text | 140 | nan |
| 626487515 | LVGO | 2020-11-12 | ACQ | unparsed | 139.8 | TELADOC HEALTH INC ACQUIRED LIVONGO HEALTH INC RECEIVED STOCK SHARES OF LIVONGO HEALTH INC |
| 181986985 | WP | 2019-07-31 | ACQ | unparsed | 135 | FIDELITY NATIONAL INFO SVCS ACQUIRED WORLDPAY INC. RECEIVED CASH AND STOCK SHARES OF FIS F |
| 2658541 | TSS | 2019-09-18 | ACQ | unparsed | 133.3 | GLOBAL PAYMENTS INC. ACQUIRED TOTAL SYSTEM SERVICES INC. RECEIVED STOCK SHARES OF TOTAL SY |

대형주 실패는 대부분 벤더 원문이 교환비를 빼먹은 주식 인수(KSU·XLNX·AGN·ALXN·VMW·DATA 등: `RECEIVED STOCK SHARES OF ...`)임. 이 경우 엔진은 `default_delisting_return`(기본 0)으로 정산하는데, 인수가 근처에서 마지막 호가가 형성되는 주식 합병에서는 0이 나쁘지 않은 근사임. 정확히 하려면 8-K/S-4 기반 override가 필요함.

### successor 요약

| 항목 | 값 |
|---|---|
| successor 있는 행 | 1241 |
|   · 주식·혼합 인수(acquirer) | 962 |
|   · 개명·재상장 연속(N/C) | 279 |
| successor가 가격 캐시에 있음 | 1167 |
| successor가 캐시 밖(엔진은 현금 정산으로 되돌림) | 74 |

## 7. 유니버스 커버리지

2010-01-01 이후 하루라도 멤버였던 종목 기준.

| 유니버스 | 멤버 수 | 이벤트 있음 | co_acthist 매칭 | R 산출 |
|---|---|---|---|---|
| Top500 | 1021 | 239 | 226 (95%) | 169 (71%) |
| Top1000 | 2250 | 754 | 713 (95%) | 576 (76%) |
| ALL | 6317 | 2468 | 2279 (92%) | 1682 (68%) |

## 8. 출력 스키마·엔진 설정

```yaml
events:
  path: /home/jgo/longshort_platform/data/events/events.parquet
  id_col: tradingItemId
  end_date_col: endDate
  return_col: settlementReturn
  successor_col: successorTradingItemId        # P2-3
  successor_ratio_col: successorRatio          # P2-3
```

```
tradingItemId: string
endDate: timestamp[ns]
settlementDate: timestamp[ns]
actionCode: string
settlementReturn: double
suggestedReturn: double
cashPerShare: double
successorTradingItemId: string
successorRatio: double
acquirerName: string
source: string
confidence: string
actionText: string
matchMethod: string
```

`source`는 `co_acthist`·`override`·`master`(종료일만 있고 사건 미매칭) 세 값임. 엔진은 `tradingItemId`·`endDate`·`settlementReturn`(+ P2-3에서 successor 두 컬럼)만 읽으므로 나머지는 감사용임.

