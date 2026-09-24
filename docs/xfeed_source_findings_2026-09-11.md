# xfeed 원천 DB 확인 — 상폐 정보·OTC 커버리지 (2026-09-11)

팀 개발자의 xfeed 조회 결과를 Claude가 읽기 전용(`default_transaction_read_only`)으로 재확인한 기록. 접속 정보는 `sim/vendor/uss/save_uss_master_market_data.py`의 것을 그대로 사용(localhost:5432/xfeed, 스키마 `xfl_target`, 427 테이블).

## 1. 확인된 사실

| 주장 | 판정 | 근거 |
|---|---|---|
| "종료일 없는 8종목"은 DB에 종료일 있음 | **확인** | `ciqsecurity.securityenddate`: ANSS 2025-07-17, DFS 2025-05-22, HES 2025-07-18, CYBR 2026-02-10, EXAS 2026-03-23, EA 2026-08-05, AVB 2026-08-14, EQR 2026-08-17 |
| `co_acthist`에 인수 조건 있음 | **확인**(4건) | DFS: Capital One 1.0192주 / HES: Chevron 1.025주 / ANSS: $199.91 + Synopsys 0.3399주 / CYBR: $45 + Palo Alto 2.2005주 (actiontxt 자유 텍스트) |
| 파산 기업이 벤더에 있음 | **확인** — 앞서 "벤더에 없다"고 한 내 결론은 **철회** | Pink Sheets(exchangeId 134) 트레이딩아이템: SIVB.Q(1987~2024-11 9,190행), SBNY(2004~현재), FRCB(2010~현재), SICP, BBBY.Q(~2023-09), CHKA.Q(~2021-02) |
| 로컬 마스터 종료일 누락 규모 | 714행 | 로컬 `securityEndDate` null 7,802행 중 DB에 종료일 있음 714(2025년 382·2026년 305) |
| OTC 이동 종목 누락 규모 | 전 상장 이력 3,555개 중 로컬 44개 | Pink Sheets·USD·미국 소재·Common Stock·primary 기준. 2010년 이후 250일 이상 시총 ≥$2B **58개**(로컬 4), ≥$10B **8개**(로컬 0) — FNMA·FMCC·First Republic·Signature·Bed Bath·Sears·Cobalt 등(시총 이상치 MMEX·GTCH·EPAZ 제외) |

## 2. 누락 원인 (수집 코드)

| 위치 | 문제 |
|---|---|
| `save_uss_master_market_data.py:101` `ti.exchangeId in ('102','104','106','173','458')` | `ciqtradingitem.exchangeid`는 **현재** 거래소. OTC로 옮겨간 종목은 과거 날짜 조회(1979~ 백필 포함, `history_acc` 12,072일)에서도 상장 시절 이력까지 통째로 제외 → 파산·상장폐지 기업 survivorship |
| `save_uss_master_market_data.py:102` `join ciqpriceequity … pricingDate IN (…)` + `get_acc_master()` (:37-47 concat, 중복은 오늘 우선) | 오늘 가격이 없는 종목은 행이 안 오므로 종료일이 **영원히 null**(714행). 종료일은 상폐 후 기록되는데 그 시점엔 이미 수집 대상에서 빠짐 |

## 3. 정산 입력이 될 8종목 (웹·DB 교차 확인)

| 종목 | 종료일(DB) | 사건 | 조건 | 정산 수익률 산식 |
|---|---|---|---|---|
| DFS | 2025-05-22 | Capital One 인수(주식) | 1.0192 COF/주 | 1.0192·COF(효력일)/DFS 마지막 종가 − 1 |
| HES | 2025-07-18 | Chevron 인수(주식) | 1.025 CVX/주 | 1.025·CVX/HES − 1 |
| ANSS | 2025-07-17 | Synopsys 인수(현금+주식) | $199.91 + 0.3399 SNPS | (199.91 + 0.3399·SNPS)/ANSS − 1 |
| CYBR | 2026-02-10 | Palo Alto 인수(현금+주식) | $45 + 2.2005 PANW | (45 + 2.2005·PANW)/CYBR − 1 |
| EXAS | 2026-03-23 | Abbott 인수(현금) | $105 | 105/EXAS − 1 |
| EA | 2026-08-05 | PIF·Silver Lake·Affinity LBO(현금), 8/4 클로징 | $210 | 210/EA − 1 |
| AVB | 2026-08-14 | Equity Residential과 합병(주식), 8/17 클로징 | 2.793 EQR(→VMRK)/주 | 2.793·VMRK/AVB − 1 |
| EQR | 2026-08-17 | 합병 후 Vivmark Residential(VMRK)로 개명·재상장 | 동일 주식 유지 | 상폐 아님 — 새 트레이딩아이템(VMRK)으로 **연속** 처리 필요 |

EXAS·EA·AVB·EQR이 `co_acthist`에 없는 이유: **`co_acthist` 최신 datadate가 2026-02-19**로 약 7개월 지연(네 건 모두 그 이후 종료). 웹 출처: EA 클로징·$210(sec.gov 8-K, stocktitan), Abbott–Exact $105 현금(investing.com, prnewswire), AVB–EQR 2.793 교환비·VMRK(sec.gov 8-K/S-4). 웹 확인은 검증용이고 파이프라인 입력은 구조화 소스(co_acthist 갱신분 또는 `filingdata`의 8-K)로 잡아야 함.

## 4. 쓰지 않고 있는 xfeed 자산

| 테이블 | 용도 |
|---|---|
| `co_acthist` | 기업행사(ACQ/MGD/BK11/BK7/LIQ/N-C/SPN…)·인수 조건 — 정산 수익률·상폐 사유 |
| `ciqsecurity.securityenddate`, `ciqtradingitemstatus` | 종료일·상태를 가격 조인 없이 직접 조회 |
| `sec_idhist` | 식별자·티커 이력 |
| Pink Sheets(134) 트레이딩아이템 가격 | 파산·상장폐지 후 가격 경로(손실이 가격에 반영) |
| `ciqpriceequitydivadjfactor`, `ciqsplit`, `ciqdividend` | 배당 조정 총수익률(현재 HoldingReturn은 배당 미반영) |
| `filingdata`·`ciqkeydocument`, `ciqtranscript*`, `nikkeinews*`, `ciqownholdingperiodhst` | 공시·트랜스크립트·뉴스·보유 이력 |

## 5. 해결 순서 (제안)

1. **마스터 갱신 잡**: 로컬 마스터 전 tradingItemId에 대해 `ciqsecurity`·`ciqtradingitem`에서 종료일·상태·거래소를 가격 조인 없이 매일 갱신 → 714행 즉시 보정, 엔진 `events`의 종료일 소스.
2. **기업행사 → 정산 수익률**: `co_acthist` ACQ/MGD 조건(현금·교환비)을 파싱하고 인수사 효력일 종가로 R 계산 → `events.return_col`. 최신 지연분(7개월)은 `filingdata` 8-K 또는 수동 override 표로 보완. 개명·재상장(EQR→VMRK)은 정산이 아니라 **식별자 연속** 규칙이 필요.
3. **수집 필터 교체**: "현재 거래소 ∈ 허용" → "한 번이라도 허용 거래소에 있던 tradingItemId" 집합(식별자 이력 기반)으로 바꾸고 OTC 이후 가격까지 수집, 캐시 전체 재구축. 파산 손실이 가격 경로에 들어오면 엔진의 기본 R=0은 M&A 케이스에만 남아 근사가 타당해짐.
4. 그 다음 엔진: `stale_quote_settle_bars`는 폐기 가능, `default_delisting_return`은 명시 R로 대체.

산출물: `scratchpad/xfeed/{stale_end_dates.csv, otc_former_listed_us.csv, otc_former_large_caps.csv}` (세션 임시), 이 문서의 표는 재조회로 재현 가능.

## 6. 전체 유니버스로 확장할 때 (2026-09-11 추가 감사)

| 유니버스(2010~) | 합집합 | 일평균 | 유니버스 안 상폐 | 로컬 종료일 없음 → DB에 있음 | 결측(halt) 셀 |
|---|---|---|---|---|---|
| Top500 | 1,021 | 457 | 132 | 8 → 8 | 26 |
| Top1000 | 2,250 | 925 | 477 | 49 → 46 | — |
| ALL(`TopUniverseV1/ALL`) | 6,317 | 2,210 (p05 1,723·p95 2,800) | 1,746 (연 75~199) | 255 → 229 | 546 (member-day의 0.006%, 348종목) |

ALL 상폐 1,746건 중 `co_acthist` 기록(마지막 호가 −120~+200일)이 있는 건 **1,592(91%)**: ACQ 1,520 · N/C(개명) 49 · PVT 9 · BK11 6 · SPN 5 · LIQ 3. 즉 유니버스 안에서 끝나는 사건은 압도적으로 인수이고, 파산은 대부분 유니버스 이탈 후(OTC) 발생 — 파산 손실 반영은 정산 규칙이 아니라 **OTC 가격 수집**으로 풀어야 함. 엔진 규모는 ALL에서도 필드당 112MB(T=4,449×N=6,317 float32), eager 5필드 0.56GB로 문제 없음.

## 7. `co_acthist`가 2026-02-19에서 멈춘 이유 (Xpressfeed 로더 상태, 읽기 전용 조회)

| 항목 | 확인 |
|---|---|
| S&P 전송 | 정상. `/Xpressfeed/cana`, `/Xpressfeed/caintntl`(co_acthist), `/Xpressfeed/caref`, `caprihistxna`, `secidtichist`, `secidunlicisinhist` 모두 **2026-09-10 파일까지 도착**(`xfu_actualzipfile`, currentlyavailable=1) |
| 로더 다운로드 | 각 디렉터리에서 **2026-02-19 파일 1개만** 다운로드(2026-02-20 11:09) 후 없음 |
| 적재 구독(`xfu_loadsub`) | 718(Corporate Actions NA)·720·733(Corporate Actions International) → **schedule 737 "Meritz_Schedule2"** = cron `0 0 23 31 1 ? 2025`(2025-01-31 1회) / 729·731(Security Identifiers TIC/ISIN History) → **schedule 741 "Meritz_Schedule4"** = `0 0 23 1 2 ? 2025`(2025-02-01 1회). 활성·오류 없음 |
| 정상 패키지 | 가격·AFL·추정치 구독은 **schedule 22 "every 1hour"**(`0 0 * * * ?`)로 매시간 적재(loadjob 2026-09-11 08:00 성공) |
| 결론 | "뭔가 바뀐" 게 아니라 Compustat 기업행사·식별자 이력 패키지가 **처음부터 반복 스케줄에 안 걸려 있음**. 2026-02-20 적재는 1회성(수동 실행 추정). 파일은 일별 전체 스냅샷(~2.4MB, 행수 121k)이라 스케줄만 붙이면 최신 파일 1개로 복구됨 |

조치(xfeed 로더 관리자): loadsub 718·720·733·729·731을 schedule 22(매시간) 또는 일 1회 스케줄에 배정. 우리 캐시나 DB 데이터를 고칠 일은 아님.
