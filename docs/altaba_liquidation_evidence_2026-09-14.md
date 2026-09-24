# Altaba Inc. (AABA, tradingItemId 2665340) 청산분배 증거 정리 — 2026-09-14

목적: 2019-10-02 Nasdaq 거래 종료 이후 Altaba가 지급한 **후속 청산분배(post-dissolution liquidating
distributions)** 를 1차 자료(SEC EDGAR 8-K Ex-99.1, Nasdaq Form 25)로 전수 확정하고, 캐시의 마지막 종가로
정산 수익률 R을 산출해 `data/events/events_override.csv` 행을 제안함. **CSV는 수정하지 않았음(제안만).**

표기: `[검증]` = 1차 문서에서 문장 단위로 확인, `[추정]` = 산식·가정에 의존, `[미검증]` = 1차 문서로 확인 못 함.

## 1. 요약

- 상장폐지 후 청산분배는 **11회, 합계 USD 22.38/주** (2020-11-02 ~ 2025-05-19 지급). 전부 8-K Ex-99.1 문장으로 확인 `[검증]`.
  상장폐지 전 $51.50(ex 2019-09-24)까지 합치면 73.88/주로 IR 사이트 누계와 일치.
- 마지막 종가 2019-10-02 = **19.63** (v2 캐시 float32 19.629999, DB `ciqpriceequity` 19.63, adjustmentfactor 1.0 → k_t = 1).
  **R = 22.38 / 19.63 − 1 = +14.01%** (무할인). 지급 시점을 반영하면 연 IRR 7.03%, 평탄 할인율 1~5%에서 R = +11.8% ~ +3.7%.
- 청산은 **아직 종결되지 않음**: 최신 8-K(2026-05-07) 기준 NAV 1.62/주(순자산 $842.5M, 519,511,366주)가 남아 있고
  최종분배 공시 없음. 과제 전제("2020–2023 종결")는 사실과 다름. 제안 행은 확정 지급분 22.38만 기입.

## 2. 상장폐지·해산 사실관계 `[검증]`

| 사건 | 날짜 | 출처 | 인용 |
|---|---|---|---|
| 해산증명서 제출 예정 공표 | 2019-09-23 | 8-K 0001193125-19-252251 Ex-99.1 | "the Fund intends to file a certificate of dissolution ... immediately following the close of regular trading on the Nasdaq Global Select Market on October 4, 2019." |
| 마지막 거래일 | 2019-10-02 | altaba.com 보도자료 2019-09-30 | "Nasdaq intends to halt trading in the Fund's shares of common stock ... on October 2, 2019" / "close of regular trading at 4:00 p.m. Eastern time on October 2, 2019" |
| 해산증명서 제출·명의개서 정지 | 2019-10-04 | 8-K 0001193125-19-262790 | "In connection with the filing of the Certificate of Dissolution, effective as of the Effective Time, the Fund closed its stock transfer books and discontinued recording transfers of its common stock" |
| 후속 분배 수령권 | 2019-10-04 기준 주주 | 8-K 0001193125-20-276172 Ex-99.1 | "paid on November 2, 2020 to stockholders of record as of the October 4, 2019 date of dissolution of the Fund (or, as applicable, their permitted transferees by bequest, intestacy or operation of law)" |
| 거래 무기한 정지 | 2019-10-07 개장 전 | 8-K 0001193125-19-252251 Ex-99.1 | "indefinitely suspended prior to the opening of trading on October 7, 2019, and that the Shares will thereafter be delisted" |
| Form 25-NSE (Nasdaq 제출) | 2019-10-07 접수, 서명일 2019-10-04 | 0001354457-19-000499 | 규칙 17 CFR 240.12d2-2(a)(2), 발행인 ALTABA INC., Common Stock |

- 후속 분배는 DTC가 수령권 기록을 유지("The Fund has requested that The Depository Trust Company ('DTC') maintain
  records representing the right to receive any post-dissolution liquidating distributions, including transfers of such rights").
  즉 **2019-10-02 종가에 산 주식이 2019-10-04 기준 보유면 11회 분배를 전부 받음** — 캐시 마지막 bar 보유 포지션에 22.38 귀속이 타당.
- 벤더 필드: `ciqsecurity.securityenddate` 2019-10-10(security 레벨), `exchangeHistory` thru 2019-10-07, `lastPriceDate` 2019-10-02,
  `tradingitemstatusid` 5. DB `co_acthist`(gvkey 062634)에는 2017-06-19 N/C(YHOO→AABA 개명) 한 건뿐 — 청산 코드 행 없음.

## 3. 마지막 종가와 캐시 단위 앵커 `[검증]`

| 항목 | 값 | 출처 |
|---|---|---|
| v2 캐시 close 2019-10-02 | 19.629999 (float32) | `/home/jgo/61/data2/temp_stock/cache_v2_cash_1d/BaseData/close.parquet` col 2665340; 2019-10-03부터 NaN |
| DB `ciqpriceequity` 2019-10-02 | priceclose 19.63, adjustmentfactor 1.0, volume 48,010,480 | xfl_target, 읽기 전용 |
| DB 가격 행 | 5,909행, 1996-04-12 ~ 2019-10-02 | 〃 |
| k_t (as-traded / cache) | **1.0** → `cashPerShareCache` = `cashPerShare` | 2019년 이후 분할 없음 |
| $51.50 ex-date 전후 종가 | 2019-09-23 70.80 → 2019-09-24 19.51 | 캐시. 51.50은 `DivData/divAmount_USD` 2019-09-24 행(dividendTypeId 4 / supplemental 27 Liquidation, payDate 2019-09-23, recordDate 2019-09-16)에 이미 반영 |

캐시 주변 종가: 09-24 19.51, 09-25 19.55, 09-26 19.72, 09-27 19.57, 09-30 19.48, 10-01 19.63, **10-02 19.63**.

## 4. 청산분배 전표

### 4-1. 상장폐지 전(참고, DB 기반영) `[검증]`

| 발표 | 기준일 | 지급일 | ex-date | USD/주 | 출처 | 인용 |
|---|---|---|---|---|---|---|
| 2019-09-06 | 2019-09-16 | 2019-09-23 | 2019-09-24 | 51.50 | altaba.com 보도자료 (8-K 미제출, EDGAR에 2019-06-27~09-23 사이 8-K 없음) https://altaba.com/press-releases/altaba-announces-pre-dissolution-liquidating-distribution-5150/ | "a pre-dissolution liquidating distribution of $51.50 in cash per share ... which will be paid on September 23, 2019 to stockholders of record as of September 16, 2019" / "the ex-dividend date for such distribution will be September 24, 2019, the day after the payment date" |

### 4-2. 상장폐지 후(2019-10-02 이후) — 제안 행 대상 `[검증]`

기준일은 전 회차 공통 **2019-10-04(해산일) 기준 주주**(§2). 출처는 모두 Altaba 8-K(CIK 1011006) Ex-99.1, 아래 URL은 `https://www.sec.gov/Archives/edgar/data/1011006/` 하위 경로.

| # | 이사회 승인 | 8-K 접수 | 지급일 | USD/주 | 총액(USD) | 8-K 접수번호 / Ex-99.1 경로 | 인용(Ex-99.1) |
|---|---|---|---|---|---|---|---|
| 1 | 2020-10-23 | 2020-10-26 | 2020-11-02 | 8.33 | 4,327,529,678.78 | 0001193125-20-276172 / `000119312520276172/d67237dex991.htm` | "approved a liquidating distribution of $8.33 per share ... or $4,327,529,678.78 in the aggregate (the 'Liquidating Distribution'), which will be paid on November 2, 2020 to stockholders of record as of the October 4, 2019 date of dissolution of the Fund" |
| 2 | 2021-07-23 | 2021-07-26 | 2021-08-05 | 7.48 | 3,885,945,017.68 | 0001341004-21-000240 / `000134100421000240/ex99-1.htm` | "approved a liquidating distribution of $7.48 per share ... or $3,885,945,017.68 in the aggregate ..., which will be payable on August 5, 2021." |
| 3 | 2021-08-19 | 2021-08-20 | 2021-09-01 | 0.54 | 280,536,137.64 | 0001193125-21-252166 / `000119312521252166/d117804dex991.htm` | "approved a liquidating distribution of $0.54 per share ... or $280,536,137.64 in the aggregate ..., which will be payable on September 1, 2021." |
| 4 | 2021-12-20 | 2021-12-21 | 2021-12-30 | 0.67 | 348,072,615 | 0001193125-21-363121 / `000119312521363121/d443323dex991.htm` | "approved a liquidating distribution of $0.67 per share ... or $348,072,615 in the aggregate ..., which will be payable on December 30, 2021." |
| 5 | 2022-03-07 | 2022-03-07 | 2022-03-15 | 0.24 | 124,682,727 | 0001193125-22-067946 / `000119312522067946/d301328dex991.htm` | "approved a liquidating distribution of $0.24 per share ... or $124,682,727 in the aggregate ..., which will be payable on March 15, 2022." |
| 6 | 2022-05-27 | 2022-05-27 | 2022-06-07 | 0.75 | 389,633,524 | 0001193125-22-162363 / `000119312522162363/d319197dex991.htm` | "approved a liquidating distribution of $0.75 per share ... or $389,633,524 in the aggregate ..., which will be payable on June 7, 2022." |
| 7 | 2022-07-20 | 2022-07-21 | 2022-07-29 | 1.43 | 742,901,253 | 0001193125-22-198565 / `000119312522198565/d384876dex991.htm` | "approved a liquidating distribution of $1.43 per share ... or $742,901,253 in the aggregate ..., which will be payable on July 29, 2022." |
| 8 | 2023-01-05 | 2023-01-05 | 2023-01-13 | 0.68 | 353,267,728 | 0001193125-23-002415 / `000119312523002415/d264271dex991.htm` | "approved a liquidating distribution of $0.68 per share ... or $353,267,728 in the aggregate ..., which will be payable on January 13, 2023." |
| 9 | (발표일 기준) | 2023-02-09 | 2023-02-17 | 0.96 | 498,730,911 | 0001193125-23-030081 / `000119312523030081/d391290dex991.htm` | "approved a liquidating distribution of $0.96 per share ... or $498,730,911 in the aggregate ..., which will be payable on February 17, 2023." |
| 10 | (발표일 기준) | 2024-07-31 | 2024-08-13 | 1.10 | 571,462,502 | 0001193125-24-189941 / `000119312524189941/d858253dex991.htm` | "approved a liquidating distribution of $1.10 per share ... or $571,462,502 in the aggregate ..., which will be payable on August 13, 2024." |
| 11 | (발표일 기준) | 2025-05-06 | 2025-05-19 | 0.20 | 103,902,273 | 0001193125-25-113830 / `000119312525113830/d937305dex992.htm` (Ex-99.2) | "approved a liquidating distribution of $0.20 per share ... or $103,902,273 in the aggregate ..., which will be payable on May 19, 2025." |
| | | | **합계** | **22.38** | | | |

- 누계 검산: 22.38 + 51.50 = **73.88** = altaba.com IR 페이지 누계 표기와 일치. 각 회차 총액 ÷ 주당액 = 519,511,36x주로
  8-K 첨부 재무상태표의 발행주식수 519,511,366과 일치(예: 4,327,529,678.78/8.33 = 519,511,366.0; 103,902,273/0.20 = 519,511,365).
- 전수성: EDGAR submissions 색인(CIK 1011006, 2019-09 이후 8-K 29건)을 전부 열람. 위 11건 외의 8-K는 재무상태표(Item 2.02),
  해산 절차(7.01), 임원 변경(5.02)이며 분배 없음. Item 8.01 8-K 중 분배가 아닌 것 없음. 각 보도자료는 "partial distribution of the
  remaining assets"라고 명시 — **최종분배(final distribution) 공시는 2026-09-14 현재 없음**.

## 5. 청산 종결 상태 — 미종결 `[검증]`

| 기준일 | 순자산(USD 천) | NAV/주 | 발행주식 | 출처 |
|---|---|---|---|---|
| 2025-03-31 | 917,334 | 1.77 | 519,511,366 | 8-K 2025-05-06 Ex-99.1 |
| 2025-09-30 | 831,837 | 1.60 | 519,511,366 | 8-K 2025-11-10 Ex-99.1 |
| 2026-03-31 | 842,482 | 1.62 | 519,511,366 | 8-K 2026-05-07 Ex-99.1 (총자산 869,641 중 income tax receivable 596,644) |

- 잔여 NAV의 대부분이 세금 환급 채권이라 회수 시점·금액 불확실. **가상 상한 `[추정]`**: 22.38 + 1.62 = 24.00 → R ≈ +22.3%.
  상한은 참고용이며 override에 기입하지 않음(README §2 "추정치 금지").
- 마지막 8-K(2026-05-07) 이후 N-CSRS(2026-08-13), NPORT-P, N-PX만 제출됨. Form 15 등록말소 없음.

## 6. 정산 수익률 R

정산 bar = endDate(2019-10-02) 다음 무호가 bar = **2019-10-03** (엔진 규약: 정산은 그 다음 무호가 bar). mark = 19.63.

| 방식 | 값 | 비고 |
|---|---|---|
| **무할인 R = 22.38/19.63 − 1** | **+14.01%** | 제안 행이 산출하는 값. 엔진은 2019-10-03 bar에 일시 인식 |
| 연 IRR (19.63 지급 2019-10-02 → 11회 수취) | **+7.03%/yr** | 현금가중 평균 회수기간 1.96년 `[추정]` |
| PV 할인 R, 평탄 1% | +11.81% | PV 21.95 |
| PV 할인 R, 평탄 2% | +9.68% | PV 21.53 |
| PV 할인 R, 평탄 3% | +7.63% | PV 21.13 |
| PV 할인 R, 평탄 4% | +5.64% | PV 20.74 |
| PV 할인 R, 평탄 5% | +3.71% | PV 20.36 |
| 2023-02-17까지 수취분만(9회, 21.08) | +7.39% | 과제의 "2020–2023" 창만 세면 |

- 할인율은 임의 평탄율이며 실제 무위험금리(2019-10 ~ 2025-05 T-bill 0~5.4%)를 대입하지 않았음 `[미검증]`.
  엔진이 정산 bar에 R을 일시 인식하는 규약상 **무할인 +14.01%** 가 기입 대상이고, 할인 수치는 시점 왜곡 크기(최대 −10%p)를 보여주는 감도.
- 시장은 2019-10-02 종가 19.63에 이미 시점·불확실성 할인을 반영했으므로 R>0은 사후 실현 관점의 값임.

## 7. `events_override.csv` 제안 행 (미기입)

헤더(현 파일): `tradingItemId,ticker,endDate,settlementDate,actionCode,cashPerShare,successorTicker,successorTradingItemId,successorRatio,source,confidence,evidence_url,note,entered_on,election_rule,supersedes,superseded_on`

```csv
2665340,AABA,2019-10-02,2019-10-04,LIQ,22.38,,,,sec_8k,high,https://www.sec.gov/Archives/edgar/data/1011006/000119312520276172/d67237dex991.htm,"Post-dissolution liquidating distributions to holders of record as of 2019-10-04 (transfer books closed on dissolution; DTC tracks the rights): 8.33 paid 2020-11-02 + 7.48 (2021-08-05) + 0.54 (2021-09-01) + 0.67 (2021-12-30) + 0.24 (2022-03-15) + 0.75 (2022-06-07) + 1.43 (2022-07-29) + 0.68 (2023-01-13) + 0.96 (2023-02-17) + 1.10 (2024-08-13) + 0.20 (2025-05-19) = 22.38 USD/share, each from Altaba 8-K Ex-99.1 (CIK 1011006, accessions 0001193125-20-276172, 0001341004-21-000240, 0001193125-21-252166, 0001193125-21-363121, 0001193125-22-067946, 0001193125-22-162363, 0001193125-22-198565, 0001193125-23-002415, 0001193125-23-030081, 0001193125-24-189941, 0001193125-25-113830). Pre-dissolution 51.50 (ex 2019-09-24) is already in DivData and excluded. Undiscounted sum; liquidation still open (NAV 1.62/share at 2026-03-31, 8-K 2026-05-07), so this is a floor. Last Nasdaq close 2019-10-02 = 19.63 (DB adjustmentfactor 1.0, k=1). See docs/altaba_liquidation_evidence_2026-09-14.md",2026-09-14,,,
```

필드 근거:

| 필드 | 값 | 근거 |
|---|---|---|
| `endDate` | 2019-10-02 | 보유 마지막 날 = 캐시 마지막 유효 종가일(README §3). 정산은 2019-10-03 bar |
| `settlementDate` | 2019-10-04 | 사건 기록일 규약(README §4·`co_acthist.datadate` 상당) = 해산일이자 전 회차 공통 기준일 |
| `actionCode` | LIQ | 벤더 코드 없음(co_acthist는 N/C뿐). `BANKRUPTCY_CODES`("BK11","BK7","BKR","LIQ")에 속해 `suggestedReturn=-1`, `settlementReturn=NaN`이 찍히지만 엔진 1차 입력은 `cashPerShareCache`(=22.38, k=1)라 R은 +14.01%로 계산됨. −1은 현금 다리를 값매길 수 없을 때의 fallback 힌트에 그침. 리뷰어가 LIQ의 −1 힌트 동거를 싫어하면 대안 코드 검토 필요 |
| `cashPerShare` | 22.38 | as-traded USD 합계(§4-2). 2019년 이후 분할 없어 캐시 단위 동일 |
| `successor*` | 공란 | 승계 주식 없음(현금 청산) |
| `source`/`confidence` | sec_8k / high | README §2 우선순위 2(EDGAR 8-K 본문·별첨) |
| `evidence_url` | 1회차 Ex-99.1 | 공백 금지 규칙상 URL 1개. 나머지 10건 접수번호는 note에 열거 |
| `election_rule` | 공란 | 선택형 대가 아님. note에 EITHER/ELECT 문구 없음 |

**적용 전 확인 사항(블로커)**

1. 현재 `build_events.py` 마스터 `data/master/uss_cash_master_refreshed.parquet`(v1 유니버스 합집합, 28,210행)에 **2665340이 없음**
   (v2 `Master/v2_only_items.csv` 1775행, `inV1Cache=False`). 이대로 넣으면 로그 `override id 2665340 is absent from the master; skipped`로
   무시됨. v2 마스터(`cache_v2_cash_1d/Master/master.parquet`, 2665340 포함, TopUniverseV2 Top500 멤버 4,909일)로 이벤트를 빌드하는 경로에서만 유효.
2. 잔여 NAV 1.62/주가 추후 지급되면 §4-1 정정 절차(`supersedes`/`superseded_on` + history append)로 갱신.
3. 재생성 후 `verified invariants (1)-(6)`·`verified look-ahead: 0 of N` 확인(README §5).

## 8. 출처·검증 방법

- EDGAR submissions 색인: `https://data.sec.gov/submissions/CIK0001011006.json` (2026-09-14 21:38 KST 취득, 최근 1,000건).
- 각 8-K·Ex-99.1은 `curl -A "<UA>"`로 원문 취득 후 HTML 태그 제거·문장 검색. 원문 텍스트 사본: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/<접수일>_<파일명>.txt` (세션 임시).
- Form 25-NSE: `https://www.sec.gov/Archives/edgar/data/1011006/000135445719000499/primary_doc.xml`.
- IR 사이트 누계(73.88) 및 2019-09-06·09-30 보도자료: `https://altaba.com/`, `https://altaba.com/press-releases/` (2차 확인용).
- DB 조회는 `build_events.py`와 동일 상수·`default_transaction_read_only=on`으로 `ciqgvkeyiid`/`co_acthist`/`ciqpriceequity`/`ciqtradingitem⋈ciqsecurity` 읽기만 수행. `/home/base` 무수정.
