# Phase 2g 감사 보고 — override 35행 독립 검토 (auditor, 2026-09-14)

판정 CSV: `scratchpad/live/p2g_audit_override.csv` (35행, 브리프 열 + `R_current`·`R_after_correction`·`severity` 3열 추가).
파일 수정 없음(override·생성기·엔진·`/home/base` 미접촉). DB는 read-only 세션.

## 0. 범위·방법

| 항목 | 값 |
|---|---|
| 대상 | `data/events/events_override.csv` 11:51 판 35행 (`events.parquet` 11:53 생성분과 필드 일치 확인) |
| 파일 변동 | 13:25에 선택형 10행(BNI·PBG·NLC·FRX·KMP·EPB·BRCM·SNI·TFCF.A·VMW)과 `election_rule`·`supersedes`·`superseded_on` 열이 추가됨. 원본 35행의 cash·ratio·successor·endDate·actionCode는 **변화 없음**(parquet 대조). 추가 10행은 G-2상 리드 2차 검토 범위라 본 감사에서 제외 |
| 1차 출처 | SEC 8-K/8-K12B/424B3/DEFM14A 34건 + 회사 완료 보도자료(EA). 조항은 CSV `clause` 열에 인용 |
| 가격 | 캐시 `close.parquet`(마지막 5/첫 5 종가) + `ciqpriceequity.priceclose×adjustmentfactor`(as-traded) 양쪽 대조 |
| 엔진 규칙 | `parquet_reader.cpp:557` `settlement_bar = upper_bound(timestamps, day_end(endDate))` = **endDate 다음 bar**, 그 뒤 첫 무호가 bar에서 정산(`simulator.cpp:161-164`). 따라서 endDate가 마지막 실제 거래일보다 늦으면 그 일수만큼 stale mark로 들고 있다가 늦게 정산됨 |
| 체크 기준 | G-1 5항목. medium 2행(TYC·LVGO)은 1차 출처로 확인됨 |

## 1. TYC(34969540) 워크스루

### 1.1 대가의 주어 — 기입값은 상대측(JCI) 조건

co_acthist(gvkey 010787, 2016-09-06): "SHAREHOLDERS OF TYCO ... PRORATIONED TO A PER SHARE RATE OF $6.06 AND .8263 OF THE 'NEW' JOHNSON CONTROLS". 이 문구는 **JCI 주주** 조건임.

- Tyco 8-K Item 2.01(2016-09-06, [a16-17941_18k.htm](https://www.sec.gov/Archives/edgar/data/833444/000110465916143068/a16-17941_18k.htm)): "Tyco shareholders received 0.955 ordinary shares of Tyco (which shares are now referred to as shares of the Company ...) for each Tyco ordinary share they held by virtue of a 0.955-for-one share consolidation." — **현금 없음**.
- 같은 8-K: JCI 주주는 현금 선택 $34.88 또는 주식 1주, 프로레이션 후 미선택·주식선택 주주 "$5.7293 in cash ... and 0.8357 Company ordinary shares", 총 현금 "approximately $3.864 billion". JCI 8-K(53669, [a16-17944_18k.htm](https://www.sec.gov/Archives/edgar/data/53669/000110465916143072/a16-17944_18k.htm)) 동일.
- $6.06/0.8263의 정체: 6.06/34.88 = 0.1737, 1−0.1737 = 0.8263 → JCI 전체 주주 평균 블렌드(현금 비중 17.4%). Tyco 조건과 무관.

### 1.2 가격 단위 — TYC 캐시 라인은 이미 신주(병합 후) 단위

| 날짜 | TYC 캐시 | TYC DB priceclose | adjustmentfactor | TYC as-traded | JCI(372277281) 캐시 | JCI DB as-traded |
|---|---|---|---|---|---|---|
| 2016-08-31 | 45.738 | 45.738 | 0.955 | 43.68 | 45.948 | 43.88 |
| 2016-09-01 | 47.131 | 47.131 | 0.955 | 45.01 | 47.162 | 45.04 |
| 2016-09-02 | 47.738 | 47.738 | 0.955 | 45.59 | 47.592 | 45.45 |
| 2016-09-06 | **48.900** | 48.900 | 1.000 | 48.90 | **48.900** (vol 6,146,416 동일) | 48.90 |
| 2016-09-07 | NaN | — | — | — | 47.22 | 47.22 |

- 캐시 TYC = as-traded ÷ 0.955: CIQ가 0.955 consolidation을 TYC 이력 전체에 소급 적용한 값을 캐시가 담고 있음.
- 09-06 bar는 Tyco 자체 거래가 아니라 합병사 JCI의 첫날 print(가격·거래량 동일)가 TYC id에 붙은 것. 즉 엔진의 mark 48.90은 **신주 1주 가격**임.
- 따라서 캐시 단위 교환비는 1.0. 0.955를 다시 곱하면 이중 반영(브리프의 "TYC 유형"). 현재 기입(0.8263 + $6.06)은 주어 오류와 단위 오류가 겹쳐 R = (6.06+0.8263×47.22)/48.90−1 = **−7.8%**.

### 1.3 효력일·endDate·정산 bar

- 효력: 2016-09-02 23:59 NY(8-K). Tyco 자체 마지막 거래 09-02. JCI 티커 거래 개시 09-06.
- endDate 09-06(현재) → settlement_bar=09-07 → 09-07 무호가 → 정산 09-07, mark 48.90.
- endDate 09-02(정정) → settlement_bar=09-06 → 09-06 관측(post_end_quote_bar 1건) → 정산 09-07, mark 48.90. **엔진 결과 동일**. 기록 정확성 차원에서 09-02 권고.

### 1.4 정정값

| 필드 | 현재 | 정정 | 근거 |
|---|---|---|---|
| cashPerShare | 6.06 | 없음(0) | 8-K Item 2.01: share consolidation만 |
| successorRatio | 0.8263 | **0.955** (as-traded, Tyco 구주 1주당) | 동상 |
| successorRatioCache | 0.8263 | **1.0** | §1.2: 캐시 라인·mark가 이미 신주 단위 |
| successorTradingItemId | 372277281 | 유지 | JCI NYSE USD 보통주 |
| endDate | 2016-09-06 | 2016-09-02 | 효력일. 엔진 정산 bar는 09-07로 동일 |
| source/confidence | co_acthist_text/medium | sec_8k/high | 8-K URL 위 |

생성기 주의: `cacheUnitFactorTarget`을 mark 날짜(09-06, adj 1.0)에서 잡으면 ratio 0.955가 그대로 캐시 단위로 들어가 R −7.8%가 재현됨. 대상 unit factor는 **대상이 실제 거래한 마지막 bar(09-02, adj 0.955)** 에서 잡아야 1.0이 나옴(45.59/47.74 = 0.955 → 0.955/0.955). 일반 규칙: 대상 마지막 5 bar의 `adjustmentfactor`가 1이 아니면 캐시가 신주 단위라는 신호.

### 1.5 손익 영향

정정 후 R = 47.22/48.90 − 1 = **−3.44%**(JCI 09-07 자체 하락). 현재 −7.82%와의 차이 4.4%p가 TYC 정산일 손익 오류.

## 2. 선택형 10행 2차 검토 + TYC 산술 확인 (코디네이터 요청, 13:40)

판정 CSV: `scratchpad/live/p2g_audit_election.csv`(17행 = 기입 10 + 미기입 7; `entered`·`hold_reason_verdict` 열 추가). 대상은 override 13:25 판의 신규 10행(`election_rule=non_election_default`)과 `p2g_election.csv`의 미기입 7행.

### 2.1 TYC 산술 독립 확인 — 리드 결정 지지

| 경로 | 09-02→09-06 | 정산 bar(09-07) | 누적(09-02 기준) |
|---|---|---|---|
| 캐시, ratio 1.0(리드 결정) | 47.738→48.900 = **+2.43%** | 47.22/48.90 = **−3.44%** | 47.22/47.738 = **−1.09%** |
| 캐시, ratio 0.955(생성기안) | +2.43% | 0.955×47.22/48.90 = −7.78% | **−5.54%** |
| DB as-traded 실제 보유자 | Tyco 45.59 → 0.955 JCI×48.90 = +2.43% | 0.955×47.22 = 45.10 | 45.10/45.59 = **−1.09%** |

실제 Tyco 주주(as-traded 45.59 → 0.955 JCI)의 누적 −1.09%가 캐시·ratio 1.0 경로와 소수 셋째 자리까지 일치하고, 0.955 유지안은 −5.54%로 4.4%p 과소. 리드의 "캐시 단위 ratio 1.0, 누적 −1.1%(+2.4% 후 −3.4%)" 확정, 생성기 0.955안 기각이 옳음.

### 2.2 기입 10행 판정

| ticker | (a) 인용 조항=미선택 배정? | (b) 안분 최종치? | (c) 승계 id·단위·적용일 | 판정 |
|---|---|---|---|---|
| BNI | 예: no-election "will receive cash"(현금선택 100% 현금, 주식선택만 92.25% 안분) | 해당 없음(현금 100%) | 현금; 마지막 02-12=endDate | confirmed |
| PBG | 예: 425의 49.4/50.6은 "did not make valid elections" 전용 수치 | **예비치**. "closing 3영업일 후 최종 발표" 예고분이 EDGAR 전문검색(2010-02~04)·PBG/PEP 3월 filing에 없음(SC 13E3/A는 집계 50/50만) → 예비치 유지·medium 타당 | PEP 2638536 ✓ 패리티 1.003; **endDate 03-01→02-26**(마지막 거래, 1 bar 지연; 기재 settlementDate 03-02와 불일치) | corrected(timing) |
| NLC | 예: no-election "deemed ... to receive cash" 100% | 해당 없음 | 현금; 마지막 12-01=endDate; 선택마감 12-07이 closing 후 → 현금 6일 이름(무해) | confirmed |
| FRX | 예: 미선택 12.97% "will receive the Standard Election Consideration" | 면제(주식선택만 $25.67+0.3326로 안분) | 244652395=Actavis→Allergan 동일 라인 ✓ 패리티 1.008; 마지막 06-30=endDate | confirmed |
| KMP | 예: no election 84,925,868 units → Mixed | 면제(주식선택만 2.2264+$9.5414) | KMI ✓ 1.015; 마지막 11-26=endDate | confirmed |
| EPB | 예: no election 19,388,823 units → Mixed | 면제(주식선택만 1.0527+$0.6802) | KMI ✓ 1.012; endDate 11-27은 휴장일, 정산 bar 11-28 동일(표기만 11-26 권고) | confirmed |
| BRCM | 예: 미선택 22.3% "deemed to be Cash Electing Shares" | **최종**(02-01) 안분치 $51.4829+0.0242 — 미선택자가 직접 안분을 맞은 케이스 | AVGO 555183874 ✓ as-traded 1.003. **캐시 AVGO는 10:1 분할 조정치(02-01 캐시 13.37 vs DB 133.7) → successorRatioCache=0.242 필요**(TERP형) | confirmed(+단위 경고) |
| SNI | 예: no-election "by default receive, the Mixed Consideration" | 면제(주식선택 11.79%만 안분); $65.82는 cash top-up 반영 최종 | 1776948460=Series C→WBD ✓ 1.018; **endDate 05-22→03-06**(마지막 거래; 53 bar 지연, 기재 settlementDate 03-06과 불일치) | corrected(timing) |
| TFCF.A | 예: "valid stock election or no valid election ... 0.4517 shares" | 면제(현금선택만 $50.667340+0.007929) | DIS ✓ 1.000(03-19 ex-FOX 종가 기준); endDate 03-19 정정·supersedes 기록 ✓ | confirmed |
| VMW | 예: "elected ... cash ... or did not make a valid election ... $142.50" | 해당 없음(주식선택 ~96%만 52.1/47.9 안분) | 현금; **endDate 11-22→11-21**(마지막 거래; 정산 11-24로 1 bar 지연) | corrected(timing) |

집계: confirmed 7, corrected(endDate만) 3, 조건·주어 오류 0. 10행 모두 "미선택 주주 최종 배정" 규칙을 충족하며 평균 블렌드 기입은 없음. PBG만 안분치가 예비(최종 미공시).

### 2.3 미기입 7행 사유 검토

| ticker | 기입자 사유 | 판정 | 한 줄 근거 |
|---|---|---|---|
| PCYC | 미선택 실현치 미공시 | **약함 → 기입 권고** | 계약 §1.1(c)(vii) "In no event shall ... No Election Shares be subject to proration" → 미선택 기본값 $152.25+1.6639 ABBV는 계약으로 고정, 공시 대상 아님. 미응모 13%도 합병에서 동일 메뉴(ex99(a)(5)(i)). NLC(선택마감 closing 후)와 같은 기준으로 medium 기입. 패리티 05-22 261.25 vs 261.21 |
| ANDV | "approximately 87%" 정밀도 | 타당 | 8-K Item 8.01에 버킷 주식수 없음 → ±1%p(주식 다리 ±0.6%). 대안: 근사 기입(cash 19.80, ratio 1.627, medium)이 기본 R보다 오차 작음 — 리드 판단 |
| EP | 워런트 다리 | 부분 타당 | 미선택 Mixed $14.65+0.4187 KMI+0.640 warrant 확인. CVR·특별배당 "note" 규약 준용 시 현금+주식 기입·워런트 note(과소 ~4%) 가능 — 리드 판단 |
| VMED | multi_class | 타당 | 선택형 아님(고정 $17.50+0.2582 LBTYA+0.1928 LBTYK), 승계 2클래스 |
| SUG | ETE 캐시 없음 | 타당 | 미선택→1 ETE unit(현금 0), 안분 미발동. ETE 25772700(현 ET) DB 가격 있음. WBS처럼 successor id 기입 후 fallback 권고 |
| TMX | RTO ADS 캐시 없음 | 타당 | 미선택→deemed Stock Election→1.4899 RTO ADS. 6113524 DB 가격 있음(10-13 27.58). 동일 권고 |
| SUN | ETP 캐시 없음 | 타당이나 **id 오류** | 428532605는 2017-05-01 시작 라인(2012 가격 없음). 2012 ETP 라인=**9185265**(2004~2017-04-28). 미선택→Standard Mix $25+0.5245 ✓ |

### 2.4 기입 담당·리드 전달 사항

1. endDate 3건 정정: PBG 2010-02-26, SNI 2018-03-06, VMW 2023-11-21 (규칙: 마지막 실제 거래일).
2. BRCM 재생성 시 AVGO 캐시 단위 factor 10 확인(successorRatioCache 0.242, R≈+0.3%). 캐시 단위 factor가 1로 나오면 §1.4 유형 오류.
3. TFCF.A: 03-19 Fox Corp 분배(−28.7% 캐시 갭)는 정산 밖의 스핀오프 이벤트. BaseData에 조정 시계열이 없어 엔진이 손실로 계상할 위험 — 리드 확인.
4. PCYC 기입 권고(cash 152.25, ratio 1.6639, succ 225079155, endDate 2015-05-22, medium). SUN successor 9185265로 정정. SUG·TMX·SUN은 WBS와 같은 방식(조건+id 기입, 캐시 부재 fallback)으로 통일.
5. PBG는 최종 안분치 미공시 상태를 note에 남기고 medium 유지.

## 3. 판정 집계

| 판정 | 건수 | 행 |
|---|---|---|
| confirmed | 17 | CCE, AMB, BUCY, LINT.A, DPS, CQH, LLL, FDC, APC, S, TERP, XEC, KSU, XLNX, LSI, GTLS, EQR |
| corrected — 조건·id(terms) | 5 | **TYC**(주어+단위), **GRUB**(승계 id), **ALXN**(승계 id), **AVB**(승계 id), **WBS**(승계 id 공란) |
| corrected — endDate만(timing) | 12 | RAX, ESRX, WP, TSS, AGN, AMTD, NBL, LVGO, WORK, VER, EXAS, EA |
| unresolved | 1 | **INXN**(교환공개매수 정책) |

체크별 실패 건수: check1 4(GRUB·ALXN·AVB·WBS), check2 1(TYC), check3 5(TYC·GRUB·ALXN·WBS·INXN), check4 15(timing 12 + TYC·GRUB·ALXN; INXN unknown), check5 1(TYC 기입 시점).

## 4. 기입 담당에게 넘길 정정 목록

### 4.1 조건·승계 id 정정 (5건)

| ticker | 필드 | 현재 → 정정 | 근거·비고 | R 현재 → 정정 후 |
|---|---|---|---|---|
| TYC 34969540 | cash / ratio / endDate | 6.06 → 없음; 0.8263 → 0.955(캐시 단위 1.0); 09-06 → 09-02 | §1 | −7.8% → −3.4% |
| GRUB 249349939 | successor / endDate | 559632106(TKAY.F, OTC **보통주** USD 라인, 2021-06 $89~90) → **639739529**(JTKW.Y, DR, USD, 06-16 $18.01; Nasdaq 'GRUB' ADS의 현재 id); 06-16 → 06-14 | 8-K Item 2.01(Grubhub 2021-06-15): "3.355 American Depositary Shares ... each ADS representing one-fifth of an ordinary share". 보통주 라인에 3.355를 곱해 `rejected:R_out_of_range`가 났던 것. 캐시엔 두 라인 다 없음 → 엔진은 기본 R, settlementReturn은 DB로 산출 | NaN → +3.9%(DB) |
| ALXN 2587941 | successor / endDate | 6173862(AZN **NYSE 보통주** 라인: 2021년엔 USD 박스 $115·일 1.4천주, 2026-02-03 직상장부터 캐시) → **2591301**(AZN NasdaqGM DR, 07-21 $57.77·1,955만주, 2026-01-30 종료); 07-26 → 07-20 | 8-K ex99.1: "$60 in cash and 2.1243 ... ADSs (each ADS representing one-half of one ordinary share)". DB 패리티 60+2.1243×57.77=182.72 vs 182.50. 캐시에 ADS 라인 없음 | NaN → +0.1%(DB) |
| AVB 2591859 | successor | 2016912246(VMRK) → **2610085**(EQR) | VMRK id는 DB상 EQR 이력을 그대로 실은 동일 라인(2024-01-02~, 08-17까지 가격 동일)이나 **캐시는 08-18부터** → 정산 bar 08-17에 종가 없어 `no_cache_successor_at_settlement`. EQR 라인은 08-17 63.66 보유. 기존 EQR→VMRK N/C 행(08-18)이 다음 bar에 체인. 대안: 생성기가 2016912246의 DB 08-17 가격 사용 | NaN → −3.4% |
| WBS 3346316 | successor | 공란 → **2592842**(SAN NYSE DR USD, 08-20 $14.21) | 8-K Item 2.01(2026-08-20): "2.0548 Banco Santander American Depositary Shares and $48.75 in cash". 캐시에 없음 → 엔진 기본 R, settlementReturn은 DB로 | NaN → +0.5%(DB) |

### 4.2 endDate 정렬 (12건, timing-only)

규칙: **endDate = 대상의 마지막 실제 거래일**(엔진이 다음 bar를 settlement_bar로 잡아 첫 무호가 bar=완료일 bar에서 정산). 현재는 DB `securityenddate`를 그대로 써서 완료일(마지막 거래 +1 bar) 또는 CIQ 레코드 종료일(최대 +25일)이 들어가 있음.

| ticker | 현재 endDate → 정정 | 지연 bar | R 현재 → 정정 후 | 비고 |
|---|---|---|---|---|
| ESRX | 2019-01-14 → 2018-12-20 | 16 | +4.6% → +0.6% | 효력 12-20 종가 후, CI 12-21 거래 개시 |
| LVGO | 2020-11-12 → 2020-10-29 | 10 | −14.4% → −8.7% | 완료 10-30; 정정 후에도 TDOC 10-30 −9.8%는 실제 |
| AMTD | 2020-10-14 → 2020-10-05 | 7 | +5.8% → −0.4% | 완료 10-06 |
| NBL | 2020-10-14 → 2020-10-02 | 8 | +3.5% → +2.3% | 완료 10-05 |
| AGN | 2020-05-11 → 2020-05-08 | 1 | +2.9% → +1.8% | Scheme 효력 05-08 |
| VER | 2021-11-01 → 2021-10-29 | 1 | +0.9% → −0.7% | 완료 11-01 |
| WP | 2019-07-31 → 2019-07-30 | 1 | −1.6% → −0.2% | 효력 07-31 |
| TSS | 2019-09-18 → 2019-09-17 | 1 | −1.1% → −0.1% | 8-K "consummation on September 17, 2019" |
| WORK | 2021-07-21 → 2021-07-20 | 1 | +1.9% → +0.8% | 완료 07-21 |
| RAX | 2016-11-03 → 2016-11-02 | 1 | 0.0% → 0.0% | 현금, 지급만 1 bar 지연 |
| EXAS | 2026-03-23 → 2026-03-20 | 1 | +0.1% → +0.1% | 현금; 8-K "halted prior to the opening of trading on March 23, 2026" |
| EA | 2026-08-05 → 2026-08-04 | 1 | +0.1% → +0.1% | 현금; 완료 08-04 |

손익 해석: 승계 주식을 정산 후 계속 보유하면 종점 가치는 경로 불변이라 총 P&L은 같음. 차이는 (i) stale mark 구간 동안 가중치 동결·거래 불가, (ii) 정산일 손익이 엉뚱한 bar에 계상(위 R 차이), (iii) 정산 시 승계 포지션을 즉시 청산하는 설정이면 승계사 갭 구간 수익이 그대로 오차. ESRX·LVGO·AMTD·NBL은 5 bar 이상이라 Top500 하네스에서 눈에 띌 크기.

### 4.3 unresolved — INXN 118522494 (교환공개매수, 정책 결정 필요)

- 조건 0.7067 DLR은 **응모 주주** 조건(8-K 2020-03-13). 초기 응모기간 만료 03-09 12:01am(83.3% 응모), 후속 기간 만료 03-12 12:01am(누적 92.3%), 03-12 인수.
- 미응모 주주(non-election 상당)는 <95%라 post-offer reorganization: 자산매각→청산배당으로 DLR 주식 수령, "Dutch dividend withholding tax ... 15%" 공제([424B3](https://www.sec.gov/Archives/edgar/data/1297996/000119312520017577/d858267d424b3.htm)).
- 캐시: 03-09까지 패리티 1.000(85.34 vs 0.7067×120.67=85.28). 초기 만료 후 03-10/11/12 print 87.69/76.00/77.41은 패리티 95.0/96.8/90.7 대비 −8/−21/−15% stub(미응모 잔여주 가격). 현재 endDate 03-12 → 정산 03-13(DLR 143.34) R **+30.9%** = stub 할인 회수 + DLR 03-13 +11.7%.
- 선택지: (a) 초기 응모 모델 endDate 03-06 → mark 90.22, 정산 03-09 → −5.5%; (b) 후속 응모 endDate 03-09 → mark 85.34, 정산 03-10 → +11.3%; (c) 미응모 모델 ratio≈0.7067×0.85, 수령 시점 불명. 어느 쪽도 큰 일간 DLR 변동(−5.9%/+11.4%)을 정산 bar에 싣게 됨. 정책 확정 전 평가 입력 NaN 유지 권고.

### 4.4 근거·confidence 정비 (판정 불변)

- CCE: evidence_url을 10-K → TCCC 8-K 2010-03-03 Item 1.01([a2196953z8-k.htm](https://www.sec.gov/Archives/edgar/data/0000021344/000104746910001749/a2196953z8-k.htm), "(A) one share of SplitCo common stock and (B) $10.00 in cash")로 교체.
- EXAS: 발표 보도자료 → 완료 8-K([d128802d8k.htm](https://www.sec.gov/Archives/edgar/data/1124140/000119312526118700/d128802d8k.htm)).
- LVGO: confidence medium → high (8-K Introductory Note: $4.24+0.5920, 특별배당 $7.09 기준일 10-29 확인 → 마지막 종가 cum, 현금 11.33 타당).
- TYC: source co_acthist_text → sec_8k, confidence high.
- DPS: 10-Q는 기준일(07-06)만 기재, 지급일 07-10은 미확인(정산 bar와 같아 영향 없음).

## 5. 자동 휴리스틱 (35행)

(a) 연속성: 대상 마지막 5 캐시 종가 vs 승계 첫 5 캐시 종가, 마지막날 패리티 = (cash + ratio×승계 정산bar 종가)/대상 마지막 종가. as-traded 패리티는 DB `priceclose×adjustmentfactor`로 같은 식. (b) |settlementReturn|>10%. (c) 승계 라인 거래소·통화(`ciqtradingitem`)·캐시 존재.

| ticker | 대상 마지막 5 종가(캐시) | 승계 첫 5 종가(캐시, 정산 bar부터) | 마지막날 패리티 (cash+ratio×승계)/대상 | as-traded 패리티 | settlementReturn | ±10% 초과 | 승계 거래소/통화 | 승계 캐시 라인 |
|---|---|---|---|---|---|---|---|---|
| CCE | 30.58 31.01 31.08 31.00 31.80 | 22.01 22.32 22.51 22.34 22.55 | 1.007 | 1.007 | +0.7% |  | NYSE/USD | 있음 |
| AMB | 36.16 36.49 36.99 35.64 34.07 | 34.00 32.91 34.11 33.70 32.67 | 0.998 | 0.998 | -0.2% |  | NYSE/USD | 있음 |
| BUCY | 91.75 91.75 91.74 91.85 92.00 |  |  |  | +0.0% |  |  |  |
| LINT.A | 16.02 15.51 16.77 16.13 15.98 | 15.97 16.02 15.71 15.34 14.76 | 0.999 | 0.999 | -0.1% |  | NasdaqGS/USD | 있음 |
| TYC | 46.07 45.74 47.13 47.74 48.90 | 48.90 47.22 47.86 45.70 46.10 | 0.950 | 0.950 | -7.8% |  | NYSE/USD | 있음 |
| RAX | 31.94 31.94 31.94 31.99 31.99 |  |  |  | +0.0% |  |  |  |
| DPS | 123.68 123.22 123.81 122.92 123.66 | 22.19 24.00 25.00 24.25 24.80 | 1.018 | 1.018 | +1.8% |  | NasdaqGS/USD | 있음 |
| CQH | 31.21 31.06 30.75 31.67 32.07 | 67.30 68.01 66.98 67.91 68.56 | 0.997 | 0.997 | +0.7% |  | NYSE/USD | 있음 |
| ESRX | 98.22 96.64 95.03 95.80 92.33 | 179.80 181.30 178.01 187.63 192.79 | 1.002 | 1.002 | +4.6% |  | NYSE/USD | 있음 |
| LLL | 256.36 254.16 248.29 246.18 245.17 | 189.13 178.15 185.77 184.30 184.96 | 1.003 | 1.003 | -5.5% |  | NYSE/USD | 있음 |
| FDC | 28.68 28.69 28.40 29.15 31.69 | 102.30 104.64 103.91 105.43 104.66 | 0.978 | 0.978 | +0.1% |  | NasdaqGS/USD | 있음 |
| WP | 131.60 133.86 136.10 137.85 135.00 | 133.69 133.25 131.21 130.16 125.95 | 1.001 | 1.001 | -1.6% |  | NYSE/USD | 있음 |
| APC | 73.76 73.10 72.80 72.21 72.77 | 47.13 47.13 44.99 44.76 43.70 | 1.001 | 1.001 | +0.1% |  | NYSE/USD | 있음 |
| TSS | 134.08 135.84 135.48 134.56 133.27 | 163.74 164.35 162.68 161.62 159.66 | 0.995 | 0.995 | -1.1% |  | NYSE/USD | 있음 |
| INXN | 90.22 85.34 87.69 76.00 77.41 | 128.37 143.34 128.31 138.24 137.34 | 1.172 | 1.172 | +30.9% | 예 | NYSE/USD | 있음 |
| S | 7.99 8.61 8.29 8.44 8.62 | 83.74 84.97 84.25 81.83 85.93 | 0.996 | 0.998 | +1.3% |  | NasdaqGS/USD | 있음 |
| AGN | 188.70 192.78 193.89 192.99 193.02 | 83.96 87.90 90.46 88.87 89.94 | 1.000 | 1.000 | +2.9% |  | NYSE/USD | 있음 |
| TERP | 20.24 20.06 19.74 20.81 19.35 | 27.15 29.96 31.33 30.11 31.16 | 0.668 | 1.002 | +10.6% | 예 | NYSE/USD | 있음 |
| AMTD | 38.74 39.15 40.92 40.82 39.97 | 36.94 36.75 37.78 37.74 38.23 | 1.002 | 1.002 | +5.8% |  | NYSE/USD | 있음 |
| NBL | 8.75 8.52 8.55 8.34 8.46 | 71.19 72.70 72.30 73.78 75.22 | 1.002 | 1.002 | +3.5% |  | NYSE/USD | 있음 |
| LVGO | 138.91 136.67 142.89 145.40 139.77 | 217.89 196.46 190.40 195.24 198.74 | 1.004 | 1.004 | -14.4% | 예 | NYSE/USD | 있음 |
| GRUB | 61.99 62.28 61.14 61.05 59.89 |  |  |  | NaN |  | OTCPK/USD | 없음 |
| WORK | 44.00 44.57 44.70 45.16 45.20 | 240.11 242.11 248.28 248.25 247.60 | 1.005 | 1.005 | +1.9% |  | NYSE/USD | 있음 |
| ALXN | 186.61 180.51 180.03 179.45 182.50 | (2026-02-03~) 184.32 187.45 187.16 193.03 188.01 | 2.474 | 2.474 | NaN |  | NYSE/USD | 2026-02~만(2021 없음) |
| XEC | 81.39 88.25 87.26 90.00 87.20 | 21.76 22.25 22.77 22.65 22.07 | 1.002 | 1.002 | +2.4% |  | NYSE/USD | 있음 |
| VER | 51.95 52.16 51.63 51.53 50.30 | 71.43 70.86 71.99 72.28 71.28 | 1.001 | 1.001 | +0.9% |  | NYSE/USD | 있음 |
| KSU | 299.98 297.24 297.13 298.64 293.59 | 70.76 72.80 73.66 73.86 72.35 | 1.002 | 1.002 | +2.2% |  | NYSE/USD | 있음 |
| XLNX | 210.13 218.14 225.20 216.55 194.92 | 113.18 114.27 121.47 117.69 112.37 | 1.001 | 1.001 | +1.0% |  | NasdaqGS/USD | 있음 |
| LSI | 138.03 139.18 139.82 134.12 133.10 | 148.96 146.82 146.07 146.39 145.17 | 1.002 | 1.002 | -1.3% |  | NYSE/USD | 있음 |
| EXAS | 103.85 103.94 103.75 103.92 104.91 |  |  |  | +0.1% |  |  |  |
| GTLS | 209.60 209.87 209.79 209.91 209.90 |  |  |  | +0.0% |  |  |  |
| EA | 208.91 209.59 209.86 209.91 209.70 |  |  |  | +0.1% |  |  |  |
| AVB | 183.84 181.76 179.79 183.91 184.06 | 64.03 64.35 65.14 66.79 68.14 | 0.972 | 0.972 | NaN |  | NYSE/USD | 08-18~만(08-17 없음) |
| EQR | 65.09 64.42 65.97 65.97 63.66 | 64.03 64.35 65.14 66.79 68.14 | 1.006 | 1.006 | +0.6% |  | NYSE/USD | 있음 |
| WBS | 79.02 79.00 78.69 77.97 77.57 |  |  |  | NaN |  |  |  |

읽는 법:
- 패리티 0.95 = TYC 단위 이중 반영(§1.2). 0.668 = TERP 캐시 BEPC가 3:2 분할 조정치인 탓이며 as-traded 패리티 1.002로 정상(생성기 factor 1.5 반영). 0.978(FDC)은 마지막날 FDC +8.7% 프리미엄, 정산일 R +0.05%로 무해. 1.172(INXN)은 §4.3.
- |R|>10% 3건: INXN(정책), TERP(BEPC 07-31 자체 +10.4%, 실제), LVGO(endDate 지연 효과, 정정 후 −8.7%).
- 승계 라인 거래소·통화: 모두 NYSE/Nasdaq·USD. 캐시 부재 4건(GRUB·WBS 라인 없음, ALXN 2021 구간 없음, AVB 정산 bar 없음)은 §4.1.

## 6. 생성기·엔진에 넘길 관찰

1. **DB `securityenddate` 우선 규칙이 timing 12건의 원인**: CIQ 종료일은 완료일(+1 bar)이거나 레코드 정리일(+25일)이라 엔진 규칙(endDate 다음 bar부터 무호가 감시)과 어긋남. override 행은 마지막 실제 거래일로 두고, 일반 행도 "캐시 마지막 종가일 < DB 종료일"이면 마지막 종가일을 쓰는 편이 엔진 의미에 맞음.
2. **TYC형 단위 검출**: 대상 마지막 bar들의 `adjustmentfactor≠1`이면 캐시 라인이 병합 후 단위. `cacheUnitFactorTarget`은 대상이 실제 거래한 마지막 bar에서 계산해야 함(§1.4).
3. **캐시에 없는 승계 라인**: ADS 승계(JET·AZN 2021·SAN)와 VMRK 08-17은 DB `ciqpriceequity`에 가격이 있음. settlementReturn 참고값은 DB로 채우고 `cacheUnitStatus`로 엔진 경로만 막는 게 정보 손실이 적음.
4. **AZN 라인 교체**: 2591301(ADS, 2026-01-30 종료)→6173862(보통주 직상장 2026-02-03) 전환은 별도 N/C(ratio 0.5) 이벤트 후보. 본 감사 범위 밖.
5. INXN 유형(교환공개매수·후속기간·post-offer reorganization)은 선택형 규칙(`election_rule`)의 한 갈래로 정의 필요.

## 7. 범위 밖 메모

13:25 추가 10행(선택형)은 검토하지 않음. 이 중 VMW(36296353)·PBG(2638528)의 note에 쉼표가 들어가 `cut`으로 보면 열이 밀려 보이나 CSV 파싱(quoted)은 정상.
