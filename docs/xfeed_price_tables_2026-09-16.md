# xfeed 가격·조정계수 테이블 조사 (2026-09-16)

목적: longshort platform / sim 용 "정확한 일별 데이터셋"(raw ohlcv · 총수익률 · adj ohlcv)을 만들기 전에, xfeed(S&P Capital IQ, 스키마 `xfl_target`)에 어떤 가격·조정가·계수가 오는지 확정하고, 그것으로 세 패널을 어떻게 만들지 정한다. 근거 스크립트는 §9. 관련: `xfeed_source_findings_2026-09-11.md`, `cache_v2_plan_2026-09-14.md`.

## 0. 결론

| # | 결론 |
|---|---|
| 1 | 가격 테이블은 3종. `ciqpriceequity`(S&P "Prices Intraday" 패키지, 현재 주식수 기준 **분할조정가** + `adjustmentfactor`), `miprice`(MI "Market Unadjusted Prices" = **실거래가**), `miadjprice`(MI "Market Adjusted Prices"). |
| 2 | `ciqpriceequity.close × adjustmentfactor` = `miprice.close` 가 종가 기준 1.4M셀 중 5셀(정정분)만 빼고 일치. **raw ohlcv는 `ciqpriceequity × adjustmentfactor`로 만든다**(§4-1, §4-3 이유). `miprice`는 마지막 실거래일 판정과 검증용. |
| 3 | `ciqpriceequitydivadjfactor`(구간형 누적계수)는 현금배당 배당락마다 정확히 `1 − D/P_prev` 스텝(오차 1e-10), 스핀오프도 벤더 평가액으로 포함. 배당 있는 9,343종목 중 계수 없는 종목 4. **총수익률 = (close × divadjfactor).pct_change()** 로 만든다. |
| 4 | WorldQuant `close / fwdcumadjfactor` 대응식은 `fwdCumAdjFactor = adjustmentfactor / divadjfactor`, `adj_x = raw_x / fwdCumAdjFactor = ciq_x × divadjfactor`. v2 캐시에 두 계수 패널이 이미 있어 파생 패널만 추가하면 됨. |
| 5 | `miadjprice`의 분할 원장이 CIQ와 다름(300종목 중 6: WRB 2019 3:2 미반영, JCI 옛 라인에 합병 교환비 0.955를 miadj만 적용, TIGO·AU) → 사용하지 않음. `ciqsplitcache` 구간은 일별 `adjustmentfactor`를 300/300 정확히 재현하므로 계수 재구성은 이 작은 테이블로 충분. |
| 6 | PIT 종료 처리: 두 테이블 모두 상폐 뒤 가짜 호가 꼬리가 있는데 방향이 다름 — `ciqpriceequity`는 `securityenddate`까지(늦은 종료일이면 승계사 가격 게시, BHI 28일), `miprice`는 합병 소멸 종목에서 승계사 가격을 몇 년씩 이어감(XL 1,046일·DD 652일·S 456일). **마지막 실거래일 = min(ciq 마지막 종가일, miprice 마지막 종가일, Phase 2 종료일)** 규칙으로 자른다(§4-4, §4-5). |

## 1. 테이블 목록

`xfl_target`에서 가격·조정·배당·분할 관련 테이블(행수는 `pg_class.reltuples` 추정).

| 테이블 | 패키지(`xfl_control.xfc_pac`) | 행수 | 키 | 내용 |
|---|---|---|---|---|
| `ciqpriceequity` | Prices Intraday (Recent / 1990s / Extended History) | 5.9억 | (tradingitemid, pricingdate) | open/high/low/close/mid/bid/ask, volume, vwap, **adjustmentfactor**. 현재 주식수 기준으로 소급 재계산(분할·주식배당·교환비). 배당·스핀오프 미조정 |
| `miprice` | MI Price (Recent / Extended History), "MI Market Unadjusted Prices" | 12억 | (tradingitemid, pricedate) | 실거래 OHLC·mid·bid·ask, volume(주), currencyid, nav, sourceidentifier(0/1, 0은 228행뿐). vwap 없음. **close가 null인 호가만 있는 행**(bid만 있는 OTC 날)이 유니버스 기준 7%(371만 행) 있어 행 존재 ≠ 거래 |
| `miadjprice` | MI Adjusted Price, "MI Market Adjusted Prices" | 13억 | (tradingitemid, pricedate) | 분할조정 OHLC·volume·vwap. `ciqpriceequity`와 대체로 같으나 일부 종목 stale(§4-2). 스냅샷 당일(2026-09-15) 행 없음 |
| `mibestpricedate` | (MI) | 21억 | (tradingitemid, pricedate) 달력일 | 그 날짜 이하 마지막 가격일. 휴장·휴일에도 행이 있고 상폐 후에도 오늘까지 이어짐(값은 마지막 가격일 고정) |
| `ciqpriceequitydivadjfactor` | Dividend Adjustment Factors | 450만 | (tradingitemid, fromdate) 구간 | 배당·스핀오프 누적계수 `divadjfactor`. 최신 구간 1.0, 과거로 갈수록 작아짐 |
| `ciqsplitcache` | Split Cache | 17만 | (tradingitemid, splitdate) 구간 | `cumulativesplitfactor`(= adjustmentfactor 구간), `latestsplitfactor` |
| `ciqsplit` | – | 18만 | splitid | 분할·주식배당 개별 사건: exdate, rate, splittypeid(11 주식배당, 12 분할, 20 타사주식 분배 …), appliedflag |
| `ciqdividend` | – | 830만 | dividendid | 배당 개별 사건: exdate/paydate/recorddate/announceddate, divamount, currencyid, dividendtypeid, supplementaltypeid |
| `ciqdividendcache` | Dividend Cache | 840만 | – | 배당별 `adjustmentfactor`, `splitadjdivamount`(현재 주식수 기준 배당액), `dividendpaymenttypeid`(1 Cash·2 Stock·3 Spin-off·4 Rights) |
| `ciqpriceequitysharesoutstdg` | Equity Shares Outstanding | 650만 | (tradingitemid, fromdate) 구간 | 주식수 — 현재 기준으로 재계산됨(AAPL 2014년 241억주) |
| `sec_idhist` | Compustat | 62만 | (gvkey, iid, item, efffrom) | TIC·EXCHG·ISIN·PRIHIST 이력 = **PIT 티커·거래소** |

`dd_item`(1.2만 행)은 Compustat 데이터사전이라 CIQ 테이블 정의는 없음. `pacvertofeedpop`은 적재 패키지 id(예: 561 = Prices Intraday Recent History).

## 2. 조정 의미 — 알려진 사례로 확정

| 종목·날짜 | `ciqpriceequity.close` | `adjustmentfactor` | `miprice.close` | `miadjprice.close` | 해석 |
|---|---|---|---|---|---|
| AAPL 2014-06-06 (7:1 직전) | 23.056 | 28 | 645.57 | 23.056 | raw = ciq × adjf. adjf = 이후 분할 누적(7×4) |
| AAPL 2014-06-09 | 23.425 | 4 | 93.70 | 23.425 | 분할 당일부터 adjf 4 |
| AAPL 2020-08-31 (4:1) | 129.04 | 1 | 129.04 | 129.04 | |
| COST 2020-11-30 → 12-01 ($10 특별배당) | 391.77 → 387.56 | 1 | 동일 | 동일 | 세 테이블 모두 **배당 미조정**. `divadjfactor` 0.91797 → 0.94202, 비 0.97447 = 1 − 10/391.77 |
| EBAY 2015-07-17 → 07-20 (PayPal 분리) | 66.29 → 28.57 | 1 | 동일 | 동일 | 세 테이블 모두 **스핀오프 미조정**. `divadjfactor` 0.37116 → 0.88186, 비 0.42088 → 분배가치 66.29×(1−0.42088) = $38.39(추정: 벤더 평가) |

`adjustmentfactor` 변경일은 `ciqsplitcache` 구간과 정확히 일치(AAPL 112/56/28/4/1, COST 2→1 2000-01-14, EBAY 8/4/2/1). `ciqpriceequitysharesoutstdg`도 같은 현재 기준이라 실제 주식수 = 값 ÷ adjustmentfactor.

## 3. 항등식 검증

### 3-1. raw = `ciqpriceequity × adjustmentfactor` vs `miprice` (v2 Top500 멤버 300종목 무작위, 2000~2026, 1,407,752행)

| 필드 | 비교 | 상대차 >0.1% | >1% | 최대 | 해석 |
|---|---|---|---|---|---|
| close | mi vs ciq×adjf | 5 | 1 | 1.5% | 전부 벤더 정정(AAPL 2008-10-22 ciq 96.87 = 실제, mi 95.58 미정정 등). **ciq가 맞는 쪽** |
| open / high | 〃 | 0 / 0 | – | 0.02% / 0.04% | 동일 |
| low | 〃 | 4,298 | 1,757 | 15% | **전부 EVRG 1종목**: mi의 low가 2000~2018년 close로 채워짐(퇴화), ciq는 정상 |
| volume | mi vs ciq/adjf | 194 | 110 | ×3 | 소수 정정(양쪽 어느 쪽이 맞는지 미확인) |
| close | miadj vs ciq | 15,403 | 15,400 | 50% | 6종목: TIGO(2022-05까지 ×1.27), WRB(2019-04 3:2 미반영), JCI(2016-09 0.955 교환비 미반영), AU, MSFT 3행, CVX 1행 → §4-2 |

ciq 행 중 miprice에 없는 행 5,927(0.4%): CCO 3,411·RIG 2,255·WDAY 139·FERG 95(계보·구멍, §4-3), 꼬리 연속 21행(BHI 19, CTXS·SUNE 각 1, §4-4).

### 3-2. `divadjfactor` 스텝 = 1 − D/P_prev (같은 300종목)

| 항목 | 값 |
|---|---|
| 계수 스텝(구간 경계) | 14,553 |
| 현금배당 배당락(유형 1,4,5,6,7,8,10,15,17,18,19,23) | 14,530 |
| 스텝 ↔ 현금배당 일치 | 14,482. `abs(step − (1 − D/adjf/P_prev))` 중앙값 1.2e-10, 배당 있는 228종목 중 224종목 최대 <1e-4 |
| 항등식 어긋난 종목 4 | OVV 2001-12-06(CAD 배당, 검증식이 FX 미반영 — 벤더 계수 문제 아님), WINM.Q 2015-04-27(같은 날 유형 20 분배와 합산), RIG 2007-11-27(현금 $33.03 + 주식 교환 재편을 계수가 함께 반영), Regions Financial 옛 라인(61484878, 2000~04 매 분기 −0.3%p, 다른 가격 라인으로 계산된 듯 — 추정) |
| 스텝만 있음 71 | 62 = 같은 날 비현금 분배(유형 20 스핀오프 등, 분배가치 중앙값 12%, LCAP.A 89%·STR 68%·T 2002 66%), 8 = 2001-09-17(휴장 중 배당락 9-12의 스텝이 재개장일에 잡힘), EPB 2012-10-31 1건 |
| 배당만 있음 46 | 45 = 미래 배당락(2026-09-16 이후, 계수 아직 없음)·2001-09-12 휴장·상장폐지 후 유령 배당(KMG 2006, PPDI 2011) — 실질 결손 0 |
| 계수 없는 종목 26 | 전부 현금배당 0인 종목 → 계수 결측은 1.0으로 해석 |

유니버스 전체(v2 마스터 22,871): 현금배당 있는 종목 9,343 중 계수 없는 종목 **4**(NXC 채권펀드 324회, 나머지 1회짜리 3), 계수 있으나 배당 없는 종목 7,457(계수 1.0 또는 스핀오프만). 비USD 현금배당 367행/57종목(계수식은 통화 무관, 현금식은 FX 필요).

### 3-3. 총수익률 두 방식

계수식 `r = (P_t·f_t)/(P_{t−1}·f_{t−1}) − 1 = P_t/(P_{t−1} − D) − 1`(배당을 배당락 전일 가격에 재투자하는 관행), 현금식 `r = (P_t + D)/P_{t−1} − 1`. 차이는 2차항 `≈ (D/P)·r`.

| 종목 | 최대 절대차 | 누적(2000~) 계수식 / 현금식 / 가격만 |
|---|---|---|
| AAPL | 1.4e-2(2008-10-22, miprice 오류일 제외 시 <1e-4) | 395.87 / 395.88 / 331.52 |
| COST | 3.9e-4(2015-02-05 $5 특별배당) | 30.45 / 30.45 / 20.26 |
| EBAY | 0.593(2015-07-20 스핀오프 — 현금식엔 없음) | 16.48 / 6.94 / 6.12 |

→ 현금배당만 보면 두 방식은 동치, 스핀오프·주식분배는 계수식만 잡는다.

## 4. 테이블 간 불일치와 PIT 문제

### 4-1. `miprice`를 raw 원본으로 쓰지 않는 이유
- 벤더 정정이 `ciqpriceequity`에만 반영(AAPL 2008-10-22, MSFT 3행, CVX 1행), EVRG low 퇴화(§3-1).
- 계보가 다름: 재상장·법인이전 종목은 `ciqpriceequity`가 새 id에 과거를 이어붙이는데(RIG 51757943: ciq 1993~, mi 2008-12-19~; CCO 618650356: ciq 2005~, mi 2019-06-05~), 옛 라인이 `miprice`에 다른 id로 남아 있지도 않음(Transocean Inc. 2000~2008은 어느 id에도 없음). Compustat은 gvkey 028338/iid 01 하나로 RIG를 1993년부터 잇는다.
- 구멍: WDAY는 IPO 2012-10-12인데 `miprice`는 2013-05-03부터.
- 꼬리도 `miprice`가 낫지 않다(§4-4): ciq가 긴 경우(468종목)와 mi가 긴 경우(200종목)가 다 있고 mi 쪽이 더 길다. `miprice`는 마지막 종가일 비교(min 규칙)에만 쓴다.

### 4-2. `miadjprice`
분할 원장을 CIQ와 따로 유지해 결과가 다름: WRB 2019-04-03 3:2 주식배당 미반영(이전 전 구간 ×1.5 stale), JCI 옛 라인(2623229)은 2016-09 Tyco 합병 교환비 0.955를 miadj만 적용(CIQ는 옛 주식 기준 유지, `adjustmentfactor` 1.0), TIGO 2022-05 이전 ×1.27, AU 2002 이전 2:1. `raw = ciq × adjustmentfactor`가 `miprice`와 일치하므로 CIQ 원장이 as-quoted와 정합인 쪽. 사용 금지.

### 4-3. 계보(stitching)
- `ciqpriceequity`는 "현재 트레이딩아이템 기준 연속 시계열"(FERG는 3개 id 모두 2003-07-03부터 같은 과거를 가짐, EVRG/WR도 양쪽 1968~). 같은 회사의 옛 id와 새 id가 **같은 날짜 행을 중복**으로 갖는다(CCO 옛 24687598 2005~2019-05-01, 새 618650356 2005~2026).
- 유니버스 패널을 만들 때 옛 id·새 id를 둘 다 열로 두면 같은 주식이 두 번 들어간다. 규칙: `securityenddate`가 있는 옛 id는 종료일까지, 새 id는 자기 `miprice` 첫 거래일(또는 옛 id 종료 다음 날)부터만 유효. `Master.exchangeHistory`(sec_idhist)와 gvkey/iid로 계보를 표시.
- 전 유니버스 계보·꼬리 통계는 §4-5.

### 4-4. 꼬리(상폐·합병 뒤 가짜 호가)
- `ciqpriceequity`는 항상 `securityenddate`까지 행을 둔다(종료일 있는 상폐 14,790종목 중 ciq 마지막일 = 종료일 12,549, 종료일 전 2,241, 종료일 후 0). 종료일이 늦으면 그 사이를 승계사 가격으로 채움: BHI 2592597 마지막 실거래 2017-07-03(`miprice` 끝), ciq는 07-05~31에 BKR 433925520 종가·거래량 게시 → 첫 가짜 행 −35%. CHFC 30일, FI 28일. 종료일 1행짜리도 많음(SIVB.Q 2024-11-07 종가 0.006·거래량 934, CTXS·SUNE·SUP·SPWR.Q·LAZR.Q).
- `miprice`는 반대로 **합병 소멸(status 8) 종목에서 승계사 가격을 몇 년씩 이어감**: ULTI 2019-05-02 이후 2025-10-31까지(2,374일), NSR 1,831일, WOOF 1,387일, XL 1,046일, DD(EIDP→DowDuPont) 652일, S(Sprint→T-Mobile) 456일, TEN 594일. 거래량도 같이 들어 있어 행만 봐선 구분 불가.
- `mibestpricedate`는 `miprice` 기준(SIVB 11-07 이후 bestpricedate = 11-06). 2023-03-10~27 거래정지 구간엔 두 테이블 모두 행 없음.
- 규칙: **마지막 유효일 = min(ciq 마지막 종가일, miprice 마지막 종가일, Phase 2 종료일 캡)**, 그 뒤 행은 NaN. 표본(Top500 300종목 중 상폐 100)에서는 ciq=mi 97, 차이 3(BHI 28일·CTXS 1일·SUNE 1일)이고 `securityEndDate`는 38/100에서 마지막 실거래보다 늦다(최대 1,803일). 둘 다 같은 승계사 가격으로 늦게 끝나는 경우는 이 규칙으로 못 잡고 Phase 2의 co_acthist 기록일 캡에 의존.

### 4-5. 전 유니버스 통계 (v2 마스터 22,871종목, 2000-01-01 이후, `coverage.py`·`tails.py`)

| 항목 | ALL 22,871 | everTop1000 3,244 | everTop500 1,495 |
|---|---|---|---|
| 행수 ciq / mi / miadj | 4,802만 / 5,159만 / 5,173만 | 1,181만 / 1,190만 / 1,193만 | 626만 / 630만 / 631만 |
| `miprice` 행 없는 종목 | 125 | 1 (KLT 2000~01) | 0 |
| `miprice` close null 행 | 371만 | 98,489 | 51,042 |
| 상폐 종목(ciq 마지막일 < 2026-09) | 15,240 | 1,663 | 653 |
| 마지막 종가일 ciq > mi (ciq 꼬리) | 468 (40일 초과 57, 최대 4,118) | 11 (최대 30) | 4 (최대 28) |
| 마지막 종가일 mi > ciq (mi 꼬리) | 200 (40일 초과 115, 최대 6,150) | 15 (40일 초과 13, 최대 2,374) | 3 (XL·DD·S, 456~1,046) |
| 상장 중 종목의 마지막일 불일치 | 0 | 0 | 0 |
| ciq 첫일 < mi 첫일(ciq가 과거를 이어붙임) | 223 (250일 초과 70) | 22 (OA 5,517일·CCO 4,954·RIG 3,273) | 7 |
| mi 첫일 < ciq 첫일(mi가 더 이른 이력) | 1,850 | 81 (5일 초과 21: FRHC 2003 vs 2015, FWLT, WBMD, PCL 2000 vs 2001-10) | 32 |
| mi 행수/ciq 행수 < 0.9(구멍) | 99 | 4 (OA·CCO·RIG 계보, PACD) | 2 |
| `miprice` 통화 2개 이상 | 4 | 0 | 0 |

- 상장 중 종목은 세 테이블 마지막일이 전부 같다 → min 규칙은 살아있는 종목을 자르지 않는다.
- `miprice`가 더 이른 이력을 가진 81종목(Top1000)은 대부분 며칠 차이(1~5일 60종목). 1년 이상 차이는 ciq 계보가 늦게 시작하는 경우(FRHC·FWLT·WBMD)로, 채우려면 `miprice` 앞부분을 붙이되 플래그 필요(기본은 붙이지 않음).

## 5. WorldQuant `fwdcumadjfactor` 대응

WQ XML(`wq_xml.jpeg`): `<data expr="close/NIO_MATRIX[double]fwdcumadjfactor" delay="1"/>` — raw close를 "그 날짜부터 오늘까지의 누적 조정계수"로 나눠 조정가를 만든다.

| WQ | 우리 산식 | AAPL 2014-06-06 | COST 2020-11-30 → 12-01 |
|---|---|---|---|
| `close` (raw) | `ciq.close × adjustmentfactor` (= `miprice.close`) | 645.57 | 391.77 → 387.56 |
| `fwdcumadjfactor` (분할+배당) | `adjustmentfactor / divadjfactor` | 28 / 0.8762 = 31.957 | 1/0.91797 = 1.0894 → 1/0.94202 = 1.0615 |
| `close / fwdcumadjfactor` | `ciq.close × divadjfactor` | 20.20 | 359.63 → 365.09 (+1.52%, 가격만은 −1.07%) |
| 분할만 조정 | `adjustmentfactor`만 나눔 = `ciq.close` | 23.056 | 391.77 → 387.56 |

계수 정의를 이렇게 두면 `adj_x = raw_x / fwdCumAdjFactor` 가 open/high/low/close/vwap에 동일하게 적용되고, volume은 `raw_volume × (adjustmentfactor 부분)`만 곱한다(배당 계수는 거래량에 안 씀).

## 6. 제안 데이터셋 (v2 캐시 위에 파생 패널 추가)

v2(`61:temp_stock/cache_v2_cash_1d`)에 이미 `BaseData/{open,high,low,close,volume,VWAP,mid,bid,ask,adjustmentFactor,divAdjFactor}`가 있다(단일 스냅샷, 현재 기준). 필요한 것은 아래 파생과 PIT 마스크.

| 패널 | 산식 | 의미 |
|---|---|---|
| `raw/{open,high,low,close,vwap,mid,bid,ask}` | `BaseData/x × adjustmentFactor` | 그 날 실제 거래된 가격(WQ `close`). 검증: `miprice`와 1.4M셀 중 정정 5셀 외 일치 |
| `raw/volume` | `BaseData/volume ÷ adjustmentFactor` | 실제 거래 주수 |
| `factor/fwdCumAdjFactor` | `adjustmentFactor ÷ divAdjFactor`(divAdj 결측=1) | WQ `fwdcumadjfactor` |
| `factor/fwdSplitFactor` | `adjustmentFactor` | 분할만 |
| `adj/{open,…,close,vwap}` | `raw/x ÷ fwdCumAdjFactor` = `BaseData/x × divAdjFactor` | 분할+배당+스핀오프 조정가 |
| `adj/volume` | `raw/volume × adjustmentFactor` = `BaseData/volume` | |
| `returns/total` | `adj/close.pct_change()` | 진짜 수익률(현금배당·특별배당·스핀오프 벤더평가 포함) |
| `returns/price` | `BaseData/close.pct_change()` | 현행 팀 `HoldingReturn_Usd`와 동일 정의 |
| `returns/cashDiv`(선택) | `(close + DivData/divAmountCacheUnit[첫 거래일≥ex])/close.shift() − 1` | 계수 검증용·스핀오프 제외 변형 |
| `mask/tradable` | `close.notna() & date ≤ lastTradeDate` | `lastTradeDate = min(ciq 마지막 종가일, miprice 마지막 종가일, Phase 2 종료일 캡)` (§4-4) |
| `Master` 추가 컬럼 | `miFirstDate, miLastDate(종가 기준), lastTradeDate, tailSource(ciq/mi/enddate 중 무엇이 잘랐는지), backfilledBefore(=miFirstDate if ciq 첫일 < mi 첫일), lineagePrevId/NextId` | 계보·꼬리 근거 표시 |

- 상폐·합병 정산(현금·승계주식)은 그대로 events 파일(엔진이 정산 bar에서 계산). `returns/total`은 상폐일까지의 수익률만 담고 정산 R은 포함하지 않는다(이중계산 방지).
- 배당락일이 휴장일이면(2001-09-12) 계수 구간은 달력일 기준이라 거래일 시계열에서 자동으로 다음 거래일에 스텝이 실림. `DivData`는 달력일 인덱스이므로 현금식은 "첫 거래일 ≥ ex" 규칙 필요.
- `divAdjFactor`는 지급일이 아니라 배당락일 인식. 지급일 모델링 안 함(현금 흐름 지연 무시).
- v2 `BaseData/divAdjFactor` 결측 채움: 배당 없는 종목은 열 전체 NaN(6,076열) → 1.0, 첫 구간이 첫 가격일보다 늦은 42열은 첫 구간값으로 뒤로 채움(bfill), 중간 결측 7열은 앞값(ffill). 꼬리 결측은 0열.
- 주의: `returns/total`에 `divAdjFactor`를 다시 곱하거나 `DivData` 현금을 더하면 이중계산.

## 7. 결정이 필요한 항목
1. 스핀오프 가치: 벤더 계수(§3-2, 분배가치 중앙값 12%)를 그대로 쓸지, Distributions 테이블의 신설사 가격×교환비(status ok 351사건)로 덮을지. 둘의 3%p 초과 불일치 105사건(v2 Distributions 보고서).
2. 계보 처리: 옛 id·새 id 중복 구간을 옛 id 우선으로 자를지(PIT), 새 id 연속 시계열을 쓸지(Compustat 방식). 알파 계산엔 연속이 편하고, 유니버스·거래 시뮬엔 PIT가 맞음. 둘 다 두고 `Master` 플래그로 선택하는 방안.
3. 총수익률 규약: 계수식(재투자, 벤더와 동일) vs 현금식(현금 보유). 차이는 2차항(특별배당 4bp).

## 8. 부수 발견
- `ciqdividend.announceddate`는 USD 현금배당 486만 행 중 35%만 있고 그중 23%는 배당락 이후 날짜 → PIT 공시일로 못 씀. `recorddate`·`paydate`는 97~100%.
- `ciqdividendcache.splitadjdivamount`가 현재 기준 배당액이라 `divAmountCacheUnit` 재계산과 교차검증 가능.
- `miprice.sourceidentifier`는 0/1 두 값(0은 2018년 이후 228행, 의미 미확인), `currencyid`는 USD 종목 전부 160(통화 2개 이상인 종목 4).
- `mibestpricedate`는 파생 정보라 캐시에 넣을 가치 낮음(21억 행). 정지·결측 판별은 가격 행 유무로 충분.

## 9. 근거 스크립트 (세션 스크래치패드 `xfeed/`)
`q.py`(읽기 전용 접속), `identity.py` → `identity.parquet`(300종목 3테이블 대조), `factors.py`(AAPL/COST/EBAY 항등식), `divfactor_sample.py` → `divfactor_*.{parquet,csv}`, `divcov.py` → `divcov.parquet`(유니버스 계수 커버리지), `coverage.py` → `coverage.parquet`(전 종목 3테이블 커버리지, 46분), `coverage_analysis.py`·`tails.py` → `coverage_stats.parquet`·`tails.parquet`(꼬리·계보 통계).
