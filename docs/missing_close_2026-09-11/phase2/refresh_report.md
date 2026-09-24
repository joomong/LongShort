# 마스터 종료일 갱신 보고 (refresh_master.py)

- 실행 시각: 2026-09-11 18:59:33
- 입력: `/home/base/vendor_data/uss/cash/master/uss_cash_master_acc.parquet`
- 출력: `/home/jgo/longshort_platform/data/master/uss_cash_master_refreshed.parquet`
- 조회 소스: xfeed `xfl_target.ciqtradingitem` ⋈ `ciqsecurity` ⟕ `ciqcompany` (가격 조인 없음, 읽기 전용 세션)

## 1. 요약

| 항목 | 건수 |
|---|---|
| 마스터 행수 | 28,210 |
| xfeed 조회 대상 id | 28,210 |
| xfeed에서 찾은 id | 28,204 |
| xfeed에 없는 id | 6 |
| **종료일 신규 채움(이번 실행)** | **715** |
| 종료일 채움 누적(`endDateChanged=True`) | 715 |
| 종료일 불일치(로컬 유지, endDateConflict) | 29 |
| 로컬만 종료일 있음(DB null) | 6 |
| 티커 변경(기존 값 → 새 값, 이번 실행) | 162 |
| 티커 신규 채움(로컬 null, 이번 실행) | 14 |
| 티커 변경 누적(`tickerChanged=True`) | 176 |
| 거래소 변경 누적(`exchangeChanged=True`) | 397 |
| 회사명 변경 | 36 |
| 상장일(securityStartDate) 변경 | 21 |
| tradingItemStatusId 채워진 행 | 27,588 |

종료일이 이미 있던 행은 **덮어쓰지 않음** — DB와 다르면 `endDateConflict=True`로 표시만 함.

## 2. 채워진 종료일 연도별 분포

| 연도 | 건수 |
|---|---|
| 2009 | 1 |
| 2015 | 1 |
| 2017 | 1 |
| 2021 | 2 |
| 2022 | 11 |
| 2023 | 5 |
| 2024 | 6 |
| 2025 | 382 |
| 2026 | 306 |
| **합계** | **715** |

## 3. 변경 전후 샘플 10건 (종료일 채움, 종료일 내림차순)

| tradingItemId | symbol | companyName | 종료일 이전 | 종료일 이후 | statusId |
|---|---|---|---|---|---|
| 606121761 | CYCN | Cyclerion Therapeutics, Inc. | (null) | 2026-09-08 | 11 |
| 1903638301 | JFB | JFB Construction Holdings | (null) | 2026-09-03 | 11 |
| 2627006 | LPSN | LivePerson, Inc. | (null) | 2026-09-03 | 8 |
| 1845753306 | APGE | Apogee Therapeutics, Inc. | (null) | 2026-09-02 | 8 |
| 26254872 | HLX | Helix Energy Solutions Group, Inc. | (null) | 2026-09-01 | 11 |
| 571929238 | CRNX | Crinetics Pharmaceuticals, Inc. | (null) | 2026-08-31 | 8 |
| 418414001 | NCSM | NCS Multistage Holdings, Inc. | (null) | 2026-08-31 | 8 |
| 1980873109 | BBCQ | Bleichroeder Acquisition Corp. II | (null) | 2026-08-27 | 11 |
| 1906022699 | DKNX | Tidal Trust II - Defiance Daily Target 2X Long DKNG ETF | (null) | 2026-08-27 | 5 |
| 288828719 | MBAI | Check-Cap Ltd. | (null) | 2026-08-26 | 11 |

## 4. 티커·거래소 변경 샘플

| tradingItemId | symbol 이전 | symbol 이후 | companyName | 거래소 이전 | 거래소 이후 |
|---|---|---|---|---|---|
| 2613624 | FCX | FCX.A | Freeport-McMoRan Inc. | 106 | 106 |
| 2944882 |  | PSYS | Psychiatric Solutions, Inc. | 104 | 458 |
| 238601432 |  | ABKF.Q | Ambac Financial Group, Inc. | 104 | 134 |
| 409423132 | PRTH | MACQ | M I Acquisitions, Inc. | 173 | 173 |
| 415710746 | VRRM | GSHT | Gores Holdings II, Inc. | 173 | 173 |
| 404363073 | PHUN | STLR | Stellar Acquisition III Inc. | 173 | 173 |
| 2656648 | ARMP | APHB | AmpliPhi Biosciences Corporation | 102 | 102 |
| 575258167 | GTEC | GLAC | Greenland Acquisition Corporation | 173 | 173 |
| 592931131 | PHGE | CHAC | Chardan Healthcare Acquisition Corp. | 102 | 102 |
| 216929933 | VTOL | ERA | Era Group Inc. | 106 | 106 |

| tradingItemId | symbol | 거래소 이전 | 거래소 이후 | 종료일 |
|---|---|---|---|---|
| 2666977 | KORE | 104 | 374 | 2004-05-14 |
| 2944882 | PSYS | 104 | 458 | 2010-11-15 |
| 238601432 | ABKF.Q | 104 | 134 | 2013-04-30 |
| 2605421 | DELL | 104 | 458 | 2013-10-29 |
| 217281176 | KRFT | 104 | 458 | 2015-07-03 |
| 2622570 | ISLE | 104 | 458 | 2017-05-01 |
| 593862819 | EQOS.Q | 173 | 134 | 2020-10-01 |
| 618453333 | TCF | 104 | 458 | 2021-06-08 |
| 24362525 | NUAN | 104 | 458 | 2022-03-03 |
| 404432222 | FRTA | 104 | 458 | 2022-03-22 |

## 5. 유니버스별 변경 종목 (2010년 이후 합집합)

| 유니버스 | 구성종목(합집합) | 종료일 채움 | 티커 변경 | 거래소 변경 | 종료일 불일치 | CSV |
|---|---|---|---|---|---|---|
| Top500 | 1,021 | 27 | 3 | 14 | 9 | `enddate_changes_Top500.csv` |
| Top1000 | 2,250 | 98 | 9 | 40 | 16 | `enddate_changes_Top1000.csv` |
| ALL | 6,317 | 362 | 86 | 242 | 20 | `enddate_changes_ALL.csv` |

### Top500 종료일 채움 27건 (상위 15건, 종료일 내림차순)

| tradingItemId | symbol | companyName | 종료일 |
|---|---|---|---|
| 275149234 | LBRD.K | Liberty Broadband Corporation | 2026-08-19 |
| 2610085 | EQR | Equity Residential | 2026-08-17 |
| 2591859 | AVB | AvalonBay Communities, Inc. | 2026-08-14 |
| 2608773 | EA | Electronic Arts Inc. | 2026-08-05 |
| 2628731 | MASI | Masimo Corporation | 2026-06-11 |
| 2649388 | SEE | Sealed Air Corporation | 2026-04-08 |
| 2610459 | EXAS | Exact Sciences Corporation | 2026-03-23 |
| 717448194 | CFLT | Confluent, Inc. | 2026-03-20 |
| 557570191 | DAY | Dayforce, Inc. | 2026-02-16 |
| 267935529 | CYBR | CyberArk Software Ltd. | 2026-02-10 |
| 2601589 | CMA | Comerica Incorporated | 2026-01-30 |
| 222487131 | HOUS | Anywhere Real Estate Inc. | 2026-01-08 |
| 28674540 | HBI | Hanesbrands Inc. | 2025-11-28 |
| 2622146 | IPG | The Interpublic Group of Companies, Inc. | 2025-11-27 |
| 2613114 | FL | Foot Locker, Inc. | 2025-09-05 |

### Top1000 종료일 채움 98건 (상위 15건, 종료일 내림차순)

| tradingItemId | symbol | companyName | 종료일 |
|---|---|---|---|
| 1845753306 | APGE | Apogee Therapeutics, Inc. | 2026-09-02 |
| 26254872 | HLX | Helix Energy Solutions Group, Inc. | 2026-09-01 |
| 2626140 | LEG | Leggett & Platt, Incorporated | 2026-08-26 |
| 79279710 | TWO | Two Harbors Investment Corp. | 2026-08-24 |
| 275149234 | LBRD.K | Liberty Broadband Corporation | 2026-08-19 |
| 3346316 | WBS | Webster Financial Corporation | 2026-08-19 |
| 2610085 | EQR | Equity Residential | 2026-08-17 |
| 2591859 | AVB | AvalonBay Communities, Inc. | 2026-08-14 |
| 580712670 | QVCA.Q | Qurate Retail, Inc. | 2026-08-06 |
| 2608773 | EA | Electronic Arts Inc. | 2026-08-05 |
| 234239320 | TMHC | Taylor Morrison Home Corporation | 2026-07-24 |
| 28384364 | GTLS | Chart Industries, Inc. | 2026-07-15 |
| 1674044674 | NUVL | Nuvalent, Inc. | 2026-07-15 |
| 1680507426 | OLPX | Olaplex Holdings, Inc. | 2026-07-06 |
| 432507356 | JHG | Janus Henderson Group plc | 2026-06-30 |

### ALL 종료일 채움 362건 (상위 15건, 종료일 내림차순)

| tradingItemId | symbol | companyName | 종료일 |
|---|---|---|---|
| 606121761 | CYCN | Cyclerion Therapeutics, Inc. | 2026-09-08 |
| 1903638301 | JFB | JFB Construction Holdings | 2026-09-03 |
| 2627006 | LPSN | LivePerson, Inc. | 2026-09-03 |
| 1845753306 | APGE | Apogee Therapeutics, Inc. | 2026-09-02 |
| 26254872 | HLX | Helix Energy Solutions Group, Inc. | 2026-09-01 |
| 571929238 | CRNX | Crinetics Pharmaceuticals, Inc. | 2026-08-31 |
| 418414001 | NCSM | NCS Multistage Holdings, Inc. | 2026-08-31 |
| 1980873109 | BBCQ | Bleichroeder Acquisition Corp. II | 2026-08-27 |
| 667854876 | FBRX | Forte Biosciences, Inc. | 2026-08-26 |
| 2626140 | LEG | Leggett & Platt, Incorporated | 2026-08-26 |
| 288828719 | MBAI | Check-Cap Ltd. | 2026-08-26 |
| 247112884 | RMAX | RE/MAX Holdings, Inc. | 2026-08-25 |
| 673377418 | REAX | The Real Brokerage Inc. | 2026-08-24 |
| 79279710 | TWO | Two Harbors Investment Corp. | 2026-08-24 |
| 1815606231 | BTMC.Q | Bitcoin Depot Inc. | 2026-08-24 |

## 6. 종료일 불일치·xfeed 미존재 (덮어쓰지 않음)

- 종료일 불일치 29건: 로컬 값을 유지하고 `endDateConflict=True`로만 표시함.

| tradingItemId | symbol | companyName | 로컬 종료일 | DB 종료일 | 차이(일) |
|---|---|---|---|---|---|
| 2603877 | WOLF | Wolfspeed, Inc. | 2026-08-11 | 2025-09-26 | -319 |
| 670832450 |  | The Shyft Group, Inc. | 2025-06-30 | 2025-07-01 | 1 |
| 76328597 | GMGI | Golden Matrix Group, Inc. | 2024-04-09 | 2026-03-02 | 692 |
| 1770633882 | BYN | Banyan Acquisition Corporation | 2023-12-29 | 2025-03-05 | 432 |
| 2638772 | PDCE | PDC Energy, Inc. | 2023-08-04 | 2026-09-09 | 1132 |
| 383654849 | COUP | Coupa Software Incorporated | 2023-03-16 | 2023-03-01 | -15 |
| 271445139 | STOR | Store Capital LLC | 2023-03-16 | 2023-02-02 | -42 |
| 701933110 | GMTX | Gemini Therapeutics, Inc. | 2023-01-31 | 2022-12-29 | -33 |
| 1776668241 | ACQC | Relativity Acquisition Corp. | 2023-01-18 | 2026-07-23 | 1282 |
| 263437463 | ZEN | Zendesk, Inc. | 2023-01-06 | 2022-11-29 | -38 |
| 694917323 | BENE | Benessere Capital Acquisition Corp. | 2022-11-03 | 2022-10-31 | -3 |
| 272646178 | CDK | CDK Global, LLC | 2022-09-23 | 2022-07-05 | -80 |
| 675858625 | PSTH | Pershing Square Tontine Holdings, Ltd. | 2022-09-23 | 2022-07-25 | -60 |
| 585187122 | PLAN | Anaplan, Inc. | 2022-09-23 | 2022-06-21 | -94 |
| 2599340 | CERN | Cerner Corporation | 2022-09-23 | 2022-06-14 | -101 |
| 2590705 | ARNA | Arena Pharmaceuticals, Inc. | 2022-09-23 | 2022-03-10 | -197 |
| 246220994 | MNDT | Mandiant, Inc. | 2022-09-13 | 2022-09-09 | -4 |
| 61519384 | SPW | SPX Corporation | 2022-08-31 | 1998-10-21 | -8715 |
| 2623613 | KSU | Kansas City Southern | 2022-04-29 | 2021-12-13 | -137 |
| 244324100 | FI | Frank's International N.V. | 2021-10-29 | 2026-07-13 | 1718 |

- xfeed `ciqtradingitem`에 없는 id 6건: 로컬 값 그대로 둠.

| tradingItemId | symbol | companyName | 종료일 |
|---|---|---|---|
| 1916232047 | NYMT.G | New York Mortgage Trust, Inc. |  |
| 1888587347 | DMN | Damon Inc. |  |
| 1949664895 | MAAS | Highest Performances Holdings Inc. |  |
| 1951779791 | METC.I | Ramaco Resources, Inc. |  |
| 1970382722 | SLRX | Decoy Therapeutics Inc. |  |
| 347991862 | SUNB | Ashtead Group plc |  |

`tradingItemStatusId` 분포 (`ciqtradingitemstatus`):

| statusId | 이름 | 행수 |
|---|---|---|
| 2 | In bankruptcy, receivership, or reorganization | 14 |
| 4 | Delisted, or suspended indefinitely | 2,513 |
| 5 | Expired, matured, or called | 2,464 |
| 6 | First day of trading | 1 |
| 8 | Merged | 10,612 |
| 11 | Inactive | 5,479 |
| 15 | Active | 6,505 |

## 7. 검증 종목 (브리프 §3의 8종목)

| symbol | tradingItemId | 종료일 이전 | 종료일 이후 | 판정 | statusId |
|---|---|---|---|---|---|
| DFS | 34969516 | (null) | 2025-05-22 | 채움 | 8 |
| HES | 27143925 | (null) | 2025-07-18 | 채움 | 8 |
| ANSS | 2590144 | (null) | 2025-07-17 | 채움 | 8 |
| CYBR | 267935529 | (null) | 2026-02-10 | 채움 | 8 |
| EXAS | 2610459 | (null) | 2026-03-23 | 채움 | 8 |
| EA | 2608773 | (null) | 2026-08-05 | 채움 | 8 |
| AVB | 2591859 | (null) | 2026-08-14 | 채움 | 8 |
| EQR | 2610085 | (null) | 2026-08-17 | 채움 | 11 |

## 8. 재현

```
/home/jgo/usr/venv-3.11/bin/python cpp-engine/tools/data/refresh_master.py \
  --master /home/base/vendor_data/uss/cash/master/uss_cash_master_acc.parquet \
  --out /home/jgo/longshort_platform/data/master/uss_cash_master_refreshed.parquet
```

추가 컬럼: `tradingItemStatusId`(ciqtradingitem, Int64) · `refreshedAt` · `endDateChanged` · `tickerChanged` · `exchangeChanged` · `endDateConflict`. 기존 15컬럼·인덱스(tradingItemId int64)·dtype는 그대로임.
