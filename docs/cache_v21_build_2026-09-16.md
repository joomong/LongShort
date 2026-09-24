# cache_v2.1 구축 기록 (2026-09-16)

raw(as-quoted) 가격 한 벌 + 구간 계수 2종 + 벤더 배당조정계수 기반 총수익률 + 이력형 마스터로 만든 미국 현물 일별 캐시. 설계는 `cache_v2_design_2026-09-16.md`(§1 레이아웃은 이 문서의 long 구조로 대체), 원천 조사는 `xfeed_price_tables_2026-09-16.md`, 계약 원문은 발행본 `qa/BRIEF.md` §3. 프로토타입 v2(`cache_v2_plan_2026-09-14.md`)는 보존.

## 0. 사용자 확정 결정
| 항목 | 결정 |
|---|---|
| 저장 가격 | raw(as-quoted) OHLCV+vwap 한 벌. 분할·배당 조정가는 저장하지 않고 로더가 계수로 계산 |
| 수익률 | `returns_total` 하나(가격변동+분할+현금·특별배당+스핀오프 벤더 평가). 다른 수익률 없음 |
| 기간 | 2000-01-03 ~ asOf(마지막 완결 거래일) |
| 스핀오프 | `divAdjFactor` 벤더값 신뢰 |
| miprice | 검증·종료일 판정 전용 |
| 위치 | 61 `temp_stock/cache_v2.1_cash_1d/`(프로토 v2 옆), v1 무접촉 |
| 비교 | v1과 일별 비교 지속(`compare_v1.py`) |
| 구조(다른 세션 설계 수용) | long 단일 `base_data.parquet`, 계수는 구간 테이블, 마스터 8+1 파일(이력 분리), universe 구간 테이블, archive/revisions, build_id 묶음 발행 |

## 1. 발행본 레이아웃 (build_id `20260916T1917Z-rc1`, asOf 2026-09-15, 299파일 1.5GB)
| 경로 | 내용 | 행/크기 |
|---|---|---|
| `base_data.parquet` | long (date, tradingItemId, open, high, low, close, vwap f32, volume f64, source, qualityFlag). 정렬 (id, date), row group 39(id 비중첩) | 48,020,105행 · 867MB |
| `factors/split.parquet` | 구간 [validFrom, validTo) splitFactor(= ciq adjustmentfactor; ciqsplitcache가 재현 못 하는 551종목은 일별 adjf로 재구성) | 46,408행 |
| `factors/dividend.parquet` | 구간 divAdjFactor(배당·스핀오프 누적, 없음=1.0) | 666,210행 |
| `factors/adjustment_factor_daily.parquet` | 검증용 side table(행별 벤더 adjustmentfactor·분할조정 close) | 48.0M행 · 177MB |
| `derived/returns_total.parquet` | (date, id, ret f64, prevDate). lifecycle 이후 행 제거 | 47,996,893행 · 383MB |
| `derived/marketcap.parquet` | (date, id, marketCap_USD, sharesOutstanding) 종가 있는 셀만 | 46.6M행 · 230MB |
| `master/instruments, current, id_mapping, identifier_history, classification_history, primary_history, lineage, lifecycle, tradable_exclusions` | 이력형 마스터(계약 §3-4) | 22,873종목 |
| `universe/membership.parquet`, `membership_v1rule.parquet` | universe별 편입 구간(포함) | 8 universe × 2 |
| `events/corporate_actions.parquet` | Phase 2 v3 계약, raw 단위(k≡1) | 14,887행 |
| `calendar.parquet` | 거래일(종가 수 ≥ 롤링 중앙값 20%) | 6,731일 중 거래일 6,715 |
| `dividends/`, `distributions/` | 프로토 사건 테이블(캐시단위 컬럼은 프로토 기준) | |
| `qa/` | BRIEF·TASKS·보고서 T1~T10·게이트·감사(AUDIT.md) | |
| `_work/` | 일별 갱신 상태(집계·원시 계수 스냅샷) | |
| `manifest.json`, `README.md` | build_id·asOf·원천·파일 sha256·규약 | |

## 2. 규약 요약
- raw = `ciqpriceequity.price × adjustmentfactor`(같은 행), `volume ÷ adjustmentfactor`. as-quoted라 재작성 없음 → 일별 append 정합.
- `split_adj = raw / splitFactor`, `div_adj = raw × divAdjFactor / splitFactor`, `fwdCumAdjFactor(WQ) = splitFactor / divAdjFactor`.
- `returns_total = adj_t / adj_prev − 1`, `adj = close/splitFactor × divAdjFactor`, prev = 직전 관측 행(결측 건너뜀). 이름: vendor-factor total return(재투자식; 현금식과 차이는 2차항). close 비율로 보유 수익률을 다시 만들지 말 것.
- lifecycle: `lastTradeDate = min(ciq 마지막 종가일, mi 마지막 종가일, events 종료일)`; status active/confirmed(마커 있음)/provisional(마커 없는 무종가). 이후 행은 base_data qualityFlag 4, returns 제거, 엔진 입력 패널은 절단본.
- 이어붙인 과거(relist/rename 805쌍): 새 id `firstTradeDate = max(ciq 첫 종가, 옛 id lastTradeDate+1, mi 첫 종가)`, 원 시작일은 `firstPriceDate`. 같은 securityId 이중 라인 10쌍은 `tradable_exclusions`.
- universe: v1 규칙을 raw 기준·PIT primary·라인 단위 sticky로 계산. 프로토 대비 라인 수준 Top500 중첩 0.9978.
- qualityFlag: 0 confirmed, 1 provisional, 2 mi_mismatch(값은 ciq), 3 ciq_null_close, 4 post_lifecycle_tail.

## 3. 구축 결과와 게이트
| 게이트 | 결과 | 판정 |
|---|---|---|
| 검증값 13종(AAPL 645.57·12,517,270주 / 93.70, COST 스텝 0.974475, EBAY 0.420878, TPX 연속, BRO 16.105) | 13/13 | PASS |
| G-SPLIT 구간 lookup = 벤더 adjustmentfactor | 48.0M행 위반 0 | PASS |
| G-JUMP 분할 경계 가짜 점프 | 11,238 경계 중 인공물 97(전부 OTC, Top500 0) + 스핀오프 상쇄 10; v1 대조군 ALL 16/Top1000 6/Top500 0 재현 | 고지 |
| G-DIV 배당 항등식 `step = 1 − D/P_prev` | 446,552 스텝 중 98.6% 1e-6 일치; 잔여 3,496 중 55종목 벤더 계수 스케일 오류(Top500 라인 PBCT ×2.98·GGWP.Q ×1.63·Regions 옛 ×1.04) | 고지·rc2 |
| G-RAW ciq×adjf vs miprice(2010+) | 28.3M행 중 >1e-3 15,659행(140종목, flag 2), everTop500 7행 | PASS |
| lifecycle | active 7,634 / confirmed 14,887 / provisional 352, 같은 securityId 동시 tradable 0, 라인 동시 멤버 0 | PASS |
| events k≡1·독립검사 8종 | 위반 0, BHI·CHFC·FI 종료일 = 마지막 실거래일 | PASS |
| universe vs 프로토 v1rule(2010+) | 라인 수준 Top500 0.9978 / Top1000 0.9957 / ALL 0.9954; id 수준 0.974(재귀속) | PASS |
| 로더 returns_total 재계산 대조 | 47.97M셀 최대차 9.1e-13 | PASS |
| 성능 | 전체 구축 44분(DB 대기 13분), 하루 append 12초, 계수+returns 재계산 20초, 로더 6필드+returns 12초·8.5GB, export 49초 | – |

## 4. v1 대비 (compare_v1.py, 2010+, Top500)
| 항목 | 값(표본) |
|---|---|
| 분할조정 종가 불일치(>1e-4) | 공통 22.70M셀 중 2.36%, 그중 91%가 상수비(레벨) 차이 — v1 이력 원장(miadjprice 계열: SCCO 1.012 전 기간, TFCF 1.357, JCI 1.047, TIGO 0.785)·옛 분할 기준(SLG·ODFL·WOLF·WRB)·v1 잠정가 날짜 클러스터 |
| 가격수익률 불일치(>1e-4) | 0.026%(Top500 보유 셀 80건) |
| 배당 기여 | Top500 동일가중 북 연 2.19%(2010~25 연 1.75~2.74%); 연율 v1 10.94% → v2.1 13.33%(+2.39%p ≈ 배당), 분할·정정 효과 연 0.008% |
| 큰 차이 사례 | EBAY 2015-07-20 +59.3%p(PayPal 스핀오프), COST 2020-12-01 +2.59%p; \|차\|>10% Top500 88건 중 배당·스핀오프 86 |
| 유니버스 Top500 중첩(v1 라이브) | id 0.9676 / 라인 0.9870; 차이 = 재귀속 105k 멤버-일, 순위 경계 17k, OTC 규칙 밖 12k(FNMA·FRCB·CBOE), PIT 선행 6.6k, currentPrimary 차이 2.6k(RACE·WOLF) |
| 종목 집합 | 공통 12,235 / v1에만 29(Top500 0) / v2.1에만 3,935(OTC 3,907, Altaba 등) |
| 일별 모드(09-15) | 종가차 0건, 가격수익률차 2건(역분할), Top500 대칭차 4 |


### 4-1. 라이브 북(combo000014 목표 포지션, 2025-01-02~2026-09-16, 427일, 평균 gross 1.54M = long 0.77M / short 0.77M) — `scratchpad/book/book_impact.py`
| 항목 | USD/년(목표 포지션 규모) | gross 대비 %/년 | 비고 |
|---|---|---|---|
| v1 가격수익률 P&L | 156,252 | 10.13 | |
| v2.1 가격수익률 P&L | 156,255 | 10.13 | 분할·정정·꼬리 차이 ≈ 0(2026년 4.6 USD) |
| v2.1 총수익률 P&L | 158,707 | 10.29 | |
| 배당 효과(총−가격) | +2,452 | +0.159 | long +1.94%/년(long gross), short −1.62%/년 → 달러중립이라 상쇄 |
| 현금배당만(dividends_long 기준) | +2,808 | +0.182 | 스핀오프 벤더평가분 제외 시 |
| 실제 GS 배당 현금(2025-07-18~2026-09-15, PM 북 5~12M gross) | 현금배당 순액 −13,495(−11,792/년, gross의 −0.175%/년) | | 부호 불일치 해소(§4-3): PnL_decomp `Div` −7,374는 합병 정산 현금(EA +16,380·EXAS −9,975·CYBR −360)이 섞인 값, 순수 배당은 −13,419. 롱 배당 30% 원천징수(withheld 18,715)가 원인 — 세전이면 +5,401(+0.07%/년) |
큰 단일 셀: WDC 2025-02-24 숏(Sandisk 스핀오프, 가격 −28.7% vs 총 −5.6%, −1,793 USD), DD 2025-11-03 롱(Qnity, −57.5% vs +1.6%, +867), TDG 2025-09-02 숏 특별배당(−717).

### 4-2. 정의 차이(2.19% vs 2.39%p)
2.19% = 같은 멤버 집합에서 연도별 (총수익률 − 가격수익률) 연수익 차이의 2010~2025 산술평균(D−C). 2.39%p = 전 기간 기하 연율 차이(A→E)로, 배당이 가격 성장(~11%/년)과 복리로 결합(2.19 × 1.109 ≈ 2.43)하고 유니버스 정의 차이(−0.04%p)가 섞인 값. 같은 멤버 기준 D−C의 연율 차이는 2.42%p.

### 4-3. GS TRS 실제 처리 — 직접 확인(2026-09-17, `scratchpad/gs/{gs_db,div_recon,div_match}.py`, DB `stocks`.{Equity_CA_GS, Equity_position_GS, Equity_allocation_activity_GS, Funding_Interest_GS, Overview_GS}, 2025-06-27~2026-09-15, 단일 전략 지수 `.GSIIMSK1`)
| 항목 | 계약상 실제 처리(관측) | 근거 |
|---|---|---|
| 현금배당 인식 | **배당락일**에 손익 인식(`date`==`Ex Date` 100%), 지급일은 중앙값 17일 뒤. 권리 기준 수량 = 배당락 전일 보유(1,725건 중 99.5% 일치; 예외는 SCCO 주식배당 소수주·특별배당 DPS 미기재 PGR 등 9건) | Equity_CA_GS × Equity_position_GS |
| 원천징수 | **롱: 미국 30%**(820건), 아일랜드·캐나다·스위스계 25%(ACN·AEM·CRH·JCI·MDT·PNR·QSR·STE·STX·TEL·TT·WCN·WTW)·35%(BG·GRMN)·15%(NXPI)·10%(CCL)·0%(AU·BAP·FTI·GFS·RPRX·SLB). **숏: 항상 100% 지급**(847건 세율 0) | DVD Tax Rate × 부호 crosstab |
| 배당 합계(418일) | 롱 세전 66,748 → 세후 48,033(withheld 18,715 = 28%), 숏 −61,528 → 순 −13,495. 세전 합은 +5,401 | 연율 −0.175%(세후) vs +0.07%(세전) of gross MV 6.74M |
| 우리 데이터 대조 | GS 현금배당 1,725건 중 1,714건 v2.1 `dividends_long`에 존재(11건은 심볼 매핑: MMC·BK·FCNCA·BF/B), 금액 일치 1,704(CRH 3건은 벤더 값이 25% 세후 0.2775 vs GS 세전 0.37, BAP FX). 역방향 41건 미기록은 GS CA 피드 결손일(2026-05-01~06, 08-20~26 등 `Funding_Interest_GS.Dividends` NaN 48일) | div_match.parquet |
| 스핀오프 | **현금 아님 — 자회사 노출을 비율대로 추가**(소수주 허용), 모회사 수량 불변: DD→Q 0.5(262→131주, 11-03 종가 97.00으로 계상, 11-05 97.22 매도 +28), HON→SOLS 0.25(6.25주, 익일 매도, 소수주 0.25는 종가 45.07 정산), CMCSA→VSNT 0.04(955→38.2주, 익일 매도, 0.2주 종가 정산), BDX 숏→WAT −0.135, HON 2026-06-29 분리(−204 HON → −102 HON + −102 HONA, 07-01 양쪽 청산). **FNF→FG 0.6주는 CA에만 있고 포지션·거래 기록 없음**, **GME 2025-10-03 워런트 배분은 수량 0으로 기록**(벤더 계수는 1.16% 가치 인정 → 모델이 롱에 과대 계상) | Equity_CA_GS `Stock 1 Qty Change`, IsCorporateAction=1 거래(가격 0) |
| 스핀오프(숏 모회사) | **자회사 숏 노출이 비율대로 추가**(음수 수량), 자회사 첫 종가로 계상, 기존 자회사 포지션과 상계·소수주 유지: BDX −1 → WAT −0.135(기존 WAT 54 매수와 합산 52.865), HON −204 → HON −102 + HONA −102(06-29 마크 220.19, HONA borrow 3.92%, 07-01 225.62 매수 커버 → 자회사 leg −553). 스핀 당일 TRS 손익 = 현금식: HON 숏 +1,676(+3.5%) vs v2.1 returns_total −6.41%(재투자식, 숏 +3,031) → 1,355 과대 — D/P 47.6% 대형 분배의 재투자식 왜곡이 라이브 계좌에서 실측됨. DD 롱은 현금식+자회사 종가(checkValueFrac 0.594)가 GS +1.89%와 일치(벤더 D는 Q 95.0 기준 0.582) | Equity_position_GS 06-26~07-01, 02-06~02-12 |
| 합병 | 현금 딜: 종료일에 딜가로 현금 정산(K 83.50·EXAS 105·EA 210, 우리 events 조건과 일치, 정산일은 EXAS·EA가 우리보다 1일 늦음). 혼합·주식 딜: 인수사 노출로 전환(ANSS 199.91+0.3399 SNPS, CYBR 45+2.2005 PANW, AVB 2.793 EQR — 전부 일치). **CTRA→DVN 0.7(2026-05-07)은 우리 events에 조건 없음(no_terms)** | events/corporate_actions.parquet 대조 |
| 금융비용 | Financing Rate 3.63% + 스프레드 롱 +25bp / 숏 −20bp, borrow 대부분 0(최대 25bp) → 순 금융비용 ≈ gross의 0.23~0.25%/년. 거래비용 1.5bp of traded(`Rebalancing Cost`). `Rebalancing Adjustment Amount`는 체결가-종가 차이 항목 | Funding_Interest_GS, Equity_position_GS |
| v1 파이프라인의 스핀 당일 거동(PM 북 기간) | DD 11-03 v1 HoldingReturn −57.5%(무조정) 이후 실거래 −82/+418/−417/−332/+630주(5일 회전 ≈ 73k USD, 포지션 ≈ 20k). 주수 목표 = 값/전일 종가 항등이 export에서 확인(979.8/244.95=4, 1040.7/104.07=10) → 가격 반토막이 주수 목표를 부풀린 것으로 추정 | combo export vs Equity_allocation_activity_GS |

라이브 북 환산(목표 포지션 1.54M, 롱 배당수익률 1.94%·숏 1.62%, 30% 일괄 가정): 세전 +2.5k/년 → 세후 ≈ −2.0k/년(−0.13%/년 gross). 엔진의 총수익률(양측 100%)은 TRS 실제와 롱 배당의 30%만큼 어긋남 — 시뮬 비용 항목 또는 배당 leg 계수(long 0.7/short 1.0)로 반영 필요.

## 5. 독립 감사 (T7)
판정 **조건부 발행 가능**(계약 FAIL 0, 벤더 데이터 결함 고지 조건) — `qa/T7/AUDIT.md`.
| 항목 | 판정 | 핵심 |
|---|---|---|
| A manifest·스키마·정렬·달력 | PASS | 178파일 sha256·행수 일치, 중복 0, 날짜 ⊆ 거래일 |
| B raw 원자료 무작위 2,000셀 | PASS | ciq×adjf 6필드 일치(최대 5.6e-8), miprice ≤1e-6, flag 2/3 의미 일치 |
| C 계수 | 주의 | 빈틈·겹침 0, 300종목 856,581행 DB 일치; 극단 계수 686종목은 값이 옳음(AAPL 112, NVDA 480, DZA 500) — 가격 수준 필터는 raw로 |
| D returns_total | 주의 | 497종목 1.29M행 독립 재계산 최대차 0.0; 배당 항등식 99.0%; **TYC 옛 라인 314828302 스핀오프 leg 누락(2007-07-02 −42%, 2012-10-01 −38%)**, 배당계수 스케일 오류 55종목(Top500 3), D/P≥10% 분배일 재투자식 vs 현금식 5pp↑ 14일 |
| E lifecycle | PASS | flag 4 = 꼬리 전부, 사례 19/19, status 재확인 40/40; TEVA 7727657 = ADR→보통주 전환(09-14) 전 산발 체결 610행 |
| F lineage | 주의 | high/medium 3쌍 동시 tradable(유니버스 동시 멤버 0), 미적용 31쌍은 별개 회사 확인 |
| G universe | PASS | 라인 중첩 0.9978 재현, 멤버 수 급변일 0, 금지 id 0, 전환 연속 |
| H events | 주의 | k≡1 0위반, endDate>lastTradeDate 184(enddate_gap), 승계사 집합 밖 122, 1:1 개명 거절 258 분류 |
| I 소비자 관점 | 주의 | Top500 tradable∧universe 1.92M셀 NaN 8(첫 종가), \|r\|>50% 12셀 전부 실제 |
| J 잔여 | 주의 | 인공물 97 중 유니버스 당일 멤버 1(ARB Top1000), G-RAW everTop500 7행 ≤0.4% |
알려진 문제 목록 `qa/T7/returns_known_issues.csv`(145행), README에 고지.

## 6. 운영
- 일별 갱신 `cache_v21/update_daily.py`(U1 마스터 델타 → U2 T−5 재조회·mi 대조·캘린더 → U3 계수 전량 재수신·revisions → U4 lifecycle → U5 returns·marketcap·universe·events → U6 게이트 → U7 publish). 시험 3회 통과(재실행 변경 0, 5일 append 원본 동일, 정정 3셀 복구). 본체 1.9분, T5·T6 포함 12분. 절차 `cache_v21/README_update_daily.md`.
- 확정 시각 관측 `observe_finalization.py` 루프(매시 05분, 2026-09-29까지) → provisional→confirmed 컷오프 정책 확정 후 U2 규칙 갱신.
- v1 비교 `compare_v1.py --mode daily` 매일.
- 로더: `cache_v21/loader.py`(파이썬 참조, `SPEC_loader.md` C++ 이관용). export_wide(cut='lifecycle', groups=True)가 v1 레이아웃 wide(+sector/industry 방송 패널)를 만들어 현행 엔진 `datasets:`로 기동 확인.

## 7. 미해결·후속
| # | 항목 | 성격 |
|---|---|---|
| 1 | **rc2 배당계수 보정 정책(결정 필요)**: 벤더 스텝이 자기 배당 테이블과 모순(현금배당·비현금 분배 없음)인 55종목 스케일 오류와 TYC 옛 라인 스핀오프 leg 누락 — (a) 벤더값 유지+고지 (b) 현금배당 implied 스텝으로 교정·Distributions ok 사건으로 누락 leg 보충(교정 로그 `factors/dividend_repairs.csv`, source 표시) | 결정 |
| 2 | provisional→confirmed 컷오프 시각: 관측 루프 10거래일 후(09-30) 정책 확정 → update_daily U2 반영 | 관측 중 |
| 3 | 일별 갱신 운영 전환: 크론 등록(관리자), 발행 후 compare_v1 daily 자동 실행 | 운영 |
| 4 | C++ 로더 이관: `SPEC_loader.md` + export parity(`loader.py compare`); 현행 소스 빌드에서 `base: {cache, groups}` 기동 1회 확인 | 팀 |
| 5 | events 잔여: endDate>lastTradeDate 184(enddate_gap), 승계사 집합 밖 122, 1:1 개명 거절 258 중 de-SPAC 28 검토, 분할비 백필 19행 | 백필 |
| 6 | lineage medium 585건 dry-run(ok 261) 승계 행 추가 여부, TEVA ADR→보통주 lineage | 검토 |
| 7 | 유니버스: PIT primary로 빠진 RACE·WOLF·DD(2.6k 멤버-일) 리뷰, primary_history 관측 축적 | 검토 |
| 8 | co_acthist 로더 스케줄 정상화 후 events 재생성(2026-02-20 이후 no_terms 298) | 관리자 |
| 9 | Compustat 식별자 패키지 2026-02-19 정지 → identifier_history 최신 개명 미반영 | 관리자 |
| 10 | 2000~2009 miprice 전수 대조(현재 표본 740셀 0건) | 선택 |
| 11 | TRS 배당 조건 반영: 롱 배당 30%(국가별 25/35/15/10/0) 원천징수·숏 100% 지급을 엔진 비용 항목 또는 배당 leg 계수로 반영, 조약세율(15%) 미적용 사유 GS 확인 | 결정·확인 |
| 12 | events 백필: CTRA→DVN 0.7(2026-05-07) no_terms, EXAS·EA 정산일 GS 기준 +1일 | 백필 |
| 13 | 비현금 배분 규칙: 워런트·권리(GME 2025-10-03)는 TRS 미인도 → 벤더 계수의 가치 인정분을 returns_total에서 제외할지 결정; FG 0.6주 미기록은 GS 문의 | 결정 |
| 14 | GS CA 피드 결손일(Funding_Interest.Dividends NaN 48일, 2026-05-01~06·08-20~26 포함) — 라이브 P&L 노트북 배당 항목 과소 가능, 재수신 요청 | 관리자 |

## 8. 코드·경로
`longshort_platform/cpp-engine/tools/data/cache_v21/`(미커밋): db, prices_{build,factors,returns,qa,perf}, master_{build,history,lifecycle,lineage,qa,report}, universe_{common,marketcap,build,qa,report}, events_build, loader(+tests, SPEC_loader.md), publish, update_daily(+README), compare_v1, observe_finalization(+loop, summarize), audit_v21. 브리프·보고서 원본은 발행본 `qa/`.

## 9. rc2 — 현금식 총수익률 + 기업행동 원장 + 엔진 CA 회계 (2026-09-17)

### 9-1. 정책(사용자 확정)과 결정
| 항목 | 내용 |
|---|---|
| 정책 | 세전·계약 중립(GS 30% 공제 제외, 비용은 선택 모듈) / **모든** 배당·분배일 현금식 `R = (부모 잔존가치 + 현금 + 자회사·권리 가치)/전일가 − 1`(D/P 임계 없음) / 현금배당은 현금·분사 주식은 별도 포지션(재투자 없음) / 고정 기준금액 / 벤더 배당계수는 원본 보존·교차검증용 / 이중계산 금지(엔진은 원장으로, returns_total은 연구용) / 자회사 포지션은 기본 기능·다음 리밸런싱에서 전략 목표 / 차입비 모듈·연구 모드 표시 / 분사 가치 우선순위 ① 자회사 당일 종가 ② 검증 W/I ③ 사건 근거 ④ 무결 벤더 implied(③④ 추정 표시) / 권리·워런트 보존·만료·자동행사 설정 |
| 단위 환산 | `cashPerShareCache = cash / sf_parent(prevDate)`, `childRatioCache = childRatio × sf_child(exDate) / sf_parent(prevDate)`(HON 2026-06-29 → 1.0 = GS −102/−102). 엔진 패널은 split_adj 유지, 원장은 raw 보존 |
| 다중 leg 합병 | 현금+승계사 leg = events 행(`settlementReturn` = events leg만의 partial R, `settlementReturnScope=events_legs_only`), 추가 자회사 leg = 원장 spin_off 행을 **옛 id·정산일·구주 기준**으로 키잉(승계사 키면 엔진이 전일 가중치 0으로 유실 — V1 실측). 엔진은 정산 bar 의 원장 leg 를 정산 전 노출·마크로 기장하고 events 와 중복되는 현금·승계사 leg 는 count-only |
| 이어붙인 과거 | 승계 id 의 복사 이력에는 keeper 사건을 `stitched_copy`(keeperEventId, exportExclude)로 복제 → returns 는 경제적으로 동일, 엔진 export 는 제외 |
| 벤더 스텝만 있는 배당 | ciqdividend/co_acthist 행이 없으면 plausibility 통과 시 estimated(④ vendor_implied), 아니면 unresolved(값 NaN) — ok 로 들어가지 않음 |
| 계보 귀속 | 사건의 타 id 귀속은 rename/relist(같은 증권)만, 합병 링크 금지(TFC 에 TOBC 배당 36행 +32%p 오귀속 제거) |

### 9-2. 산출물(발행 build, asOf 2026-09-15)
| 파일 | 내용 |
|---|---|
| `events/ca_ledger.parquet` | raw 원장 473,291행(사건 466,926 + stitched_copy 6,365) sha fa3c55af, status ok 463,508 / estimated 2,181 / unresolved 1,237(Top500 ≈11), 교정 로그 `ca_ledger_repairs.csv`, 게이트 Q1~Q17 |
| `engine/ca_ledger_cache.parquet` | split_adj 단위 엔진 export 452,668행(aux 자회사 포함, merger/rename/relist·events 중복·stitched 제외, 키 중복 0) + manifest |
| `derived/returns_total.parquet` | 현금식 v2 48,311,778행, retFlag 0/1/2/3/4 = 47,547,863 / 446,476 / 397 / 1,573 / 584; 게이트 G-CASH(무사건 행 비트 동일)·G-SPLIT·G-ORACLE 14/14·G-VENDOR(rc1 대비 430,164셀 차이 전부 분류, unexplained 0)·G-NAN·G-LINK 567/567 |
| `master/` | lineage v2 2,861행(개명 거절 258 재분류: de-SPAC 120·단순 개명 54·재상장 43·합병 24·분사 13·오매칭 4, 미해결 28), `lifecycle_dates.parquet`(마지막 거래일·효력일·정산일·지급일 분리, 184건), `aux_instruments.parquet` 134(가격 117 id 314,925행, lifecycle status aux, tradable_exclusions 로 거래 제외), TEVA 7727657 firstTradeDate 2026-09-14(ADR 1:1) |
| `events/corporate_actions.parquet` | R3 재구축 225행(다중 leg 전 leg, 13행 `multi_leg_split_path`), k≡1 0위반, 36컬럼 |
| `base_data.parquet` / `factors/` | aux 병합 48,335,030행(전역 정렬 유지), factors_aux(split 221·dividend 3,198) 병합, 구간 계약 재검증 |
| `qa/` | rc2 브리프·태스크·보고서·리뷰(qa/rc2/reports), R1~R3·V1·V2 qa, `qa/V2/qa/rc2/known_issues_v2.csv` |

### 9-3. 엔진(C++, 동결·검증)
`backtest.corporate_actions`(원장 로더·staleness 앵커·현금/주식 leg 회계·pending/unpriced·추정 표시·정산 bar 규칙·진단/JSON/`/api/config`), `backtest.costs.borrow`(기본 off 비트 동일, 표/기본요율, net 만 차감), 권리 만료(우선순위 정산 > 만료 > stale, `auto_exercise:true` 기동 거부). 테스트: 별도 빌드 CTest 28/28, gtest 347/0/0(`test_corporate_actions` 46, `test_costs` 11, HTTP 24), 원장 없으면 골든 비트 동일. 문서 `docs/corporate_actions_engine_2026-09-17.md`, `docs/rights_warrants_design_2026-09-17.md`, CLAUDE.md 계약 문단.

### 9-4. 검증
| 검증 | 결과 |
|---|---|
| 리뷰(opus HIGH 독립) | E1 FAIL→PASS(P0 추정값 무표시 등 13건), E2 PASS, E3 FAIL→PASS(stale vs 만료 우선순위), E1 §9 PASS, R1 FAIL→PASS(조건부)→R1b/R1c 반영, R2 FAIL→PASS(계보 갭 사건·corporate_actions 원천·stale 검사), R3 FAIL→PASS(다중 leg·DISC.K·factors_aux) |
| V1 하니스(`tests/recon_engine_ca.py`) | 실제 사건 12건(GS 마크 세전 기대값) + 합성 18 + HTTP 13 + 정산 bar/이연 케이스: **2,189 PASS / 0 FAIL**(리드 별도 빌드 재실행 동일). 이중계산: 엔진 ex-date 손익 = w × returns_total v2(≤2.8e-17) |
| V2 감사 | DD 회귀: 가격수익률 입력이면 스핀 당일 신호 pct 1.00·최대 롱·회전 +6.2%p, v2 입력이면 pct 0.26 중립. rc1 145행: 배당계수·대형 분배·TYC·첫행 NaN 해소, 분할 인공물 95+9는 factors/split 정책 대기(값 rc1 동일). 게이트가 못 보는 4류(≈Top500 50행)를 찾아 R1c 로 수정(V2 §3), Top500 사건일 193/193 독립 재계산 일치 |
| 리드 최종 | 별도 빌드 CTest 28/28, 파이썬 스위트 8/8, 최종 build 의 returns = R2 파일 비트 동일(TEVA 링크 1행 제외, 의도), export 공통 키 값 동일, 오라클 11건 재확인, 로더 info 22,990 id·계수 무결 |

### 9-5. 미해결·후속(rc2)
| # | 항목 |
|---|---|
| 1 | 분할 인공물 95+9건(OTC, 값 rc1 동일): factors/split 교정 정책 결정 후 반영 |
| 2 | Lionsgate 412297487 2025-05-07 STRZ leg(추정비율 0.0747) unresolved — 조건 확인 후 값 입력 |
| 3 | 벤더 스텝 unresolved 22셀·원장 공백 후보 261셀(Top500 2) 원천 조사 |
| 4 | events endDate>lastTradeDate 171건 캡 여부(lifecycle_dates 입력), 승계사 첫 호가 60일 뒤 3행 |
| 5 | 라이브 북 사이징: 주수 목표가 가격 하락에 비례해 줄어드는 라이브 규칙(추정)은 v2 입력과 별개 — 운용 측 확인 |
| 6 | update_daily: 원장 일별 append(ledger_build 증분) 미구현 — 현재는 stale 검사(abort/표시)로 보호 |
| 7 | 비용 모듈 요율표·국가별 배당 세율 옵션(P1 정책상 기본 계산 제외) |

## 10. rc2f — 고정 스냅샷 완성: 예외 레지스트리·원천 교정 (2026-09-17 착수, 2026-09-19 발행)

### 10-1. 목표·원칙(사용자 확정)
일별 갱신·라이브 사이징 전에 rc2 를 "과거 데이터가 최대한 정리된 고정 스냅샷" 으로 완성. 다섯 예외 목록(미해결 수익률 584행 · 분할 인공물 95+9 · Lionsgate 분배비율 · 원장 공백 후보 261 · 이벤트 종료일 184)을 **사건 단위 레지스트리**로 합쳐 중복을 제거하고, 고칠 수 있는 것은 원천(가격·계수·원장·종료일·계보)에서 고친 뒤 전체 수익률을 재생성, 남는 것만 사유와 함께 유지. 규칙: 순환 검증 금지(벤더 계수로 만든 값을 같은 계수로 검증 X), 원장 공백 0 채움 금지, 가격 그래프 매끄러움 ≠ 정답, 미래 가격 금지, Top500 은 조사 순서에만. 최종 상태 세 가지: `resolved_evidence`(근거로 원천 교정) · `confirmed_normal`(정상 확인) · `estimate_evidence`(근거 있는 추정, retFlag 3/status estimated) · `unresolved_insufficient`(자료 부족, NaN/retFlag 4 + 사유).

### 10-2. 조직·산출물
| 항목 | 내용 |
|---|---|
| 레지스트리 | `qa/rc2f/registry/registry_final.csv` 4,711 사건(초기 4,519 + 신규 192): 원천 교정 1,667 · 정상 확인 872 · 근거 추정 1,369 · 자료 부족 803(사유 전부 기입) · 미조사 0 |
| 태스크 | X0 파이프라인(교정 적용기 `rc2f_apply.py`·드라이버 `rc2f_build.sh`·재집계 `rc2f_recount.py`) · X1 SPLIT 1,645 · X2 DIST 579(Lionsgate) · X3 GAP 387 · X4 ENDDATE 331+합병 668 · X5 LINK 1,759(클래스는 최종 분류 기준). 전 태스크 opus HIGH 독립 리뷰(1차 FAIL 5/6 → 재작업 → 재검수 PASS) |
| 교정 CSV(`qa/rc2f/overrides/`) | 19파일 3,320행: ledger 1,000 · split 1,051 · lifecycle 482 · lineage 739 · price 34(단위 오류 15종목 19,385행) · aux 14. 적용 로그 3,342행(applied 2,314 · no_op 958 · applied_unverified 38 · applied_no_effect 32 · rejected 0), 조용한 무시 0 |
| 규칙 변경(코드) | ① lineage `chainable` 링크만 연결(근거 없는 assumed-1.0 첫 행은 NaN) ② when-issued 겹침은 옛 id 마지막 거래 이후 첫 새 id 행에서 연결 ③ reassigned 행의 stitched_copy 복제(원장 공백 240셀의 원인) ④ 벤더 스텝 마커 창 (prevDate, date+7] ⑤ 전환일 귀속은 현금 leg 한정 ⑥ CSV 분할 경계의 status 는 근거 기준(가격 판정은 notes) ⑦ 합병 2차 leg 정산 bar 정렬(자회사 종가 있을 때만) |
| 게이트 | 빈 교정 + rc2 규칙 = rc2 와 14파일 sha256 동일; 새 규칙만 857셀 전부 규칙 귀속; 통합 빌드 54,136 변화 셀(재스케일 노이즈 제외 실질 704셀, Top500 당일 27셀) **전부 overrideId/규칙 귀속**(미귀속 0); ledger_qa Q1~Q17 all_ok; returns G-CASH/G-SPLIT/G-NAN/G-VENDOR(unexplained 0)/G-LINK/G-ORACLE 13/13; V1 엔진 하니스 2,189/0; V2 DD·WDC·HON 회귀 rc2 동일(pct 1.00/0.98/1.00 → 0.26/0.74/0.57) |

### 10-3. 결과(발행 build `20260919T0144Z-rc2f`, asOf 2026-09-15, 506파일; rc2 는 `.prev-20260917T0530Z-rc2`)
| 항목 | rc2 | rc2f |
|---|---|---|
| returns_total 행 / retFlag 0·1·2·3·4 | 48,311,778 / 47,860,107·449,116·397·1,574·584 | 48,335,394 / 47,883,508·449,689·532·1,255·**410** |
| 원래 미해결 584행 | — | 해소 312(연결 불가 NaN 103·정상 79·추정 82·분배 33·현금 15) · 잔여 272(분할 172·스핀 53·권리 33·링크 11·기타 3, 전부 사유 있음) · 새 flag 4 138(rc2 에서 flag 0 으로 숨어 있던 비현실 링크 첫 행 70 + 근거 없는 ok 의 정직한 강등 54 + lifecycle 14) |
| Top500 당일 flag 4 | 16 | **1** |
| 원장 | 473,291행 ok 469,848·est 2,194·unres 1,249 | 473,770행 ok 471,376·est 1,485·unres 909 |
| 엔진 export | 452,668 | 453,064 |
| base_data | 48,335,030 | 48,359,363(aux 14 id 추가, 가격 보충 18건, 미러 꼬리 컷 6,379행 제거) |

대표 교정: Lionsgate 클래스별(LGF.A 1주 → 1.12 LION + 0.0746667 STRZ, LGF.B → 1.0 LION + 0.0666667 STRZ, 8-K·424B3·주수 항등식), GGP→HHC 0.098344(rc2 0.983, Top500), TYC 2012 PNR 0.239943(벤더 오타 0.2339943, 오라클 갱신), MMCE 청산 분배 1.35(−88% → +2.0%), CMO $7.30 특별배당, AUG·BDC·PBAL 새 id 첫날 분할 경계 이중 적용 제거, 정밀도 바닥 단위 오류 15종목(raw 10⁷~10¹⁴배) MI 복원, 옛 id 미러 꼬리 106쌍 컷, 합병 조건 162건 복원(convert 96·mixed 52·cash 14), 상장 권리 ③ 추정 31건.

### 10-4. 남은 것(rc2f)
| # | 항목 |
|---|---|
| 1 | 자료 부족 803 사건(분할 인공물 232: 마이크로캡에 독립 주식수 원천 없음 — FINRA daily list 류 외부 원천 필요; 계보 371: OTC 역분할·리버스머저 비율 원천 없음; 권리 34: CEF 공모 조건) — 원본 보존·NaN/retFlag 4 |
| 2 | `applied_unverified` 38 경계(raw 점프 검사 미달; X1 근거는 raw/MI 비 변화형) — `qa/rc2f/recount`·`split_override_cells.csv` 로 건별 재확인 가능 |
| 3 | update_daily: 원장 증분 append 미구현(stale 검사 유지), 2026-09-14/15 ETF 32행은 다음 적재 때 no_op 처리됨 |
| 4 | 라이브 사이징 규칙 확인, 비용 모듈 요율표(rc2 §9-5 5·7 그대로) |
| 5 | 코드 커밋(cache_v21 rc2f_* + 수정 10파일, 미커밋) |

## 11. rc2f-E — 남은 불확실성의 엔진 전달·검증 (2026-09-21, 발행 `20260921T0119Z-rc2f2`)

### 11-1. 배경·목표
사용자 검토(09-21): ① 미해결 사건이 엔진 입력에서 사라지는 경로(export 가 금액 없는 현금 leg·자회사 미확인 leg 를 제거) ② `applied_unverified` 38건 별도 근거 ③ 추정·NaN·미해결 포함 전체 노출 보고서와 수치 정정 ④ 고정 스냅샷 combo000014 전체 전략 비교 → 그 뒤 일별 갱신·라이브 사이징. 브리프 `qa/rc2f/E/BRIEF_E.md`, 보고서 `qa/rc2f/E/reports/`.

### 11-2. 결과
| 태스크 | 결과 | 리뷰 |
|---|---|---|
| Y1 엔진·로더 | export 가 버리던 leg 를 표시용 행(`markerReason` no_cash_amount 11·no_child 86)으로 전달, 패널 밖 자회사의 원장 ②③④ 값은 값 leg(104)로 기장; 엔진 `corporateActions.unconfirmed{count,sumAbsWeight,byKind,items}`·`pnlUnconfirmed`·timeseries `unconfirmedWeight`·`/api/config`, `unresolved_policy: warn|strict`(strict 는 첫 미확정 bar 에서 실패, HTTP 400); `loader.export_events` → `engine/events_cache.parquet`(split_adj 환산·앵커 재계산, 6,828 ok 항등, 가드 4건 통과); 회계 불변(원장 없는 입력·표시용 행 유무 비트 동일) | R-Y1 PASS(조건부→반영): 자기 빌드 CTest 28/28·gtest 357·V1 2,600/0, 진단 합성 79/79 |
| Y2 3상태 검사 + 근거 | raw 점프 검사 pass/fail/uncheckable(가격 없으면 성공값 반환 경로 제거), `checkBasis` 컬럼(raw_jump/mi_ratio/document/none; fail+근거 없음 = 오류); 1,051 분할 교정 = pass 48·fail 29·uncheckable 20·confirm 954 → 49건 전부 별도 근거로 닫음: 유지 42(document 38·mi_ratio 3·none 1)·추정 강등 3·옛 단위 프린트 가격 교정 대체 4·철회 4; 추정 가격 교정은 qualityFlag 32 → retFlag≥3 | R-Y2 PASS(조건부→반영): fail 29 전수 재확인(공시 17 원문), 검사 불가 10/10 |
| Y3 노출 보고서 | `rc2f_exposure.py` → `qa/rc2f/E/exposure/exposure_report.md`; 라이브 북(combo000014 2025-01~2026-09, 보유 셀 185,678) 의 flag 3/4/NaN/무조건 정산 노출 **0**(조건 있는 정산 16셀 23,085 USD = gross 의 0.0035%) | R-Y34 PASS: 전 수치 독립 재계산 일치 |
| Y4 전략 비교 | combo000014 동일 AlphaPos, 2016~2026: 전 층 손익 rc2 0.6945 → rc2f 0.6953(+8.3bp 자본, ≈1,238 USD @1.5M), Sharpe 2.329→2.331, MDD·회전 동일, 라이브 구간 셀 동일; 차이 8셀 전부 교정 id 귀속; 보유 중 미확정 노출 18→7셀(라이브 0); C++ 엔진 교차(Y1 빌드): 분배 손익 비트 동일·정산 82/82·순수익률 최대차 2.1e-6, `unconfirmed` 4항목(AA 정산 조건 없음·DISH/TMUS 권리 추정·CNH 추정) = 파이썬 노출표와 셀 단위 일치, strict 는 2016-11-01 AA 에서 실패(의도) | R-Y34 PASS |
| 추가 발견·교정 | Alcoa 2016-11-01 분리(1 Arconic + ⅓ Alcoa Corp, 8-K) lifecycle+lineage+중복 leg 삭제; X5 'stitched 1.0' 51 재판정(de-SPAC 1:1 44 화이트리스트 `linkVerified`·비율 교체 3·미해결 3·제외 1); 새 flag 4 중 사건 판정과 어긋난 62셀 정리; X0 규칙 2건(convPF·WTW 중복) | — |

### 11-3. 발행본 수치(rc2f2 vs rc2, 발행본 재계산)
| 항목 | rc2 | rc2f2 |
|---|---|---|
| retFlag 0·1·2·3·4 | 47,860,107·449,116·397·1,574·584 | 47,860,986·449,689·533·1,261·**350** |
| 원래 flag 4 584행 | — | 정상 분류 전환(0/1/2) 124 · 추정 전환 85 · 잘못된 연결 제거(NaN) 103 · 잔여 271 · 행 삭제 1 |
| 새 flag 4 79 | — | 근거 없는 ok 의 정직한 강등 55 · WI 링크 행 10 · lifecycle 승계 13 · 꼬리 1(rc2 에서는 flag 0/1/3 으로 숨어 있던 값) |
| 첫 행 NaN | 22,419 | 22,575(연결 불가 275·겹침 743·계보 없음 21,557) |
| Top500 당일 flag 4 / flag 3 / NaN | 16 / 24 / 0 | **1 / 8 / 0** |
| 라이브 북 노출(flag 3/4/NaN/무조건 정산) | — | 0 |
| 자료 부족 803 사건 | — | SPLIT 232 · LINK 371 · DIST 135 · ENDDATE 62 · GAP 2 · PRICE 1(사건별 표 `exposure_report.md` §3-3; 라이브 북 보유 0) |
| 원장 / export | 473,291 / 452,668 | 473,770 / 453,265(표시용 97 + 값 leg 104 포함) + events_cache 14,887 |

### 11-4. 남은 것
| # | 항목 |
|---|---|
| 1 | 자료 부족 803(외부 주식수 원천·OTC 비율·CEF 권리 조건) — NaN/retFlag 4·표시용 행으로 엔진에 전달됨(보유 시 `unconfirmed`) |
| 2 | 엔진: 정책 키가 원장 블록에만(events 만 있는 설정은 strict 불가), wi_close 재평가·null-child 승계 대조 없음, frontend 미표시 |
| 3 | update_daily 원장 증분·events_cache 재생성 훅, 라이브 사이징 규칙 확인(다음 단계) |
| 4 | 코드 미커밋(엔진 + cache_v21 rc2f/E 변경) |

### 11-5. 검토 후속(2026-09-21, 발행 `20260921T0220Z-rc2f3`)
사용자 검토 잔여 1건 반영: 자회사 **종가**로 평가해도 교부비율·배당락일이 추정이면 손익 미확정 — export 새 컬럼 `estimateReason`(ratio/date/value, '+' 결합; 발행본 stock 116 value·6 ratio·2 ratio+date, rights 30, cash 17, quantity 983) 과 엔진 `estimate_value(child_close:<reason>)` 미확정 기록(strict 감지, 금액 불변; 대상 8행 = CP 2001 분사 3 leg(주식수 추정비율)·Delta Apparel 2000(비율+배당락일)·2008-03-26 2건·2020-12-17). 리드 별도 빌드 CTest 28/28·gtest CA 56, V1 2,677/0. rc2f3 는 rc2f2 대비 `engine/ca_ledger_cache.parquet`(sha cd236240)만 변경, 나머지 파일 동일(rc2f2 는 `.prev-20260921T0119Z-rc2f2`). 표현 규칙: "라이브 북 노출 0" 은 조사한 포지션·기간(combo000014 2025-01~2026-09) 한정, 동일 포지션 비교는 회계 차이 검증(신호 재생성·사이징 검증은 일별 갱신 뒤). 교정 CSV·레지스트리 저장소 사본 `cache_v21/overrides/rc2f/`(rc2f_apply/rc2f_build 기본 glob). 남은 803 은 외부 자료 확보 시 보완 목록.

## 12. 일별 갱신(D) — rc2f 계약을 매일 이어가기 (2026-09-21)

### 12-1. 목표·조직
스냅샷 정리 마무리(자료 부족 803 은 `overrides/rc2f/registry_final.csv` 보완 목록) 뒤, 매일 T−1 데이터로 이전 빌드에서 새 빌드를 만들 때 rc2f3 와 같은 계약을 유지하고 (a) 증분 = 전체 재생성(귀속된 차이만) (b) 미확정 매일 표시·보유 시 경고 (c) stale·결손·재기술 게이트 차단을 실측으로 증명. 브리프 `scratchpad/orch_daily/BRIEF_D.md`(사본 `/home/jgo/longshort/daily/orch/`), 보고서 `orch_daily/reports/D1~D4·D1_review·D2_review·D3`.

### 12-2. 결과
| 태스크 | 내용 | 검증 |
|---|---|---|
| D1 원장 증분 | `ledger_build.py --incremental --prev … --asof … --out …`(`ledger_incremental.py`): 원장 직접 원천만 재조회(97 쿼리·6초, id 전량·날짜 창 없음), revisions `events/ca_ledger_revisions/date=<asOf>[.run<N>]/`, 교정 CSV 재적용(idempotent), sidecar asOf·원천 적재 현황·정적 원천 경고(distributions·co_acthist 2026-02-19 이후 적재 없음 → 그 뒤 종료 id 246건 표시용 행: returns 제외·export `coacthist_stale_termination`), 비-USD fxRate 매 실행 재계산, /tmp 의존 0(내구 slim 참조 `~/longshort/daily/ref/rc2_ref`) | 게이트 09-11/09-14/09-15 증분 체인 = 전체 재생성 sha 동일 6/6(refresh·scratch), 합성 4 클래스 13/13, 교정 1005/1005; R-D1 조건부 PASS → 반영 |
| D2 update_daily 통합 | `update_daily.py` + `update_daily_rc2f.py`: 교정 CSV 재적용·aux 148 종목·원장 증분·export(표시용 행·estimateReason)·events_cache·앵커 검사(rsync 전)·returns v2 게이트 6종·이전 빌드 대비 변화 셀 귀속(창 축소, 미귀속 = FAIL)·노출 요약 `qa/exposure_daily.md`(rc 9 = 성공+노출 경고, `--strict-exposure`)·BUILD 기록. 재작업으로 추가된 blocking 게이트: **G-FACTOR-JUMP**(경계 raw 점프×pf), **G-RET-OUTLIER**(기존 셀 4배/−75% 이동), **G-EVENTS**(events 델타 채택 ≤100·조건 없는 정산 증감), **G-DOUBLE-LEG**(옛/새 id 중복 자회사 leg ±3일); 재기술 종목은 raw+계수를 한 기준(kept 는 계수 carry, `--accept-raw-change` 는 둘 다 채택), events 는 이전 행 carry + 델타 id 만 채택(not_adopted 목록), firstTradeDate/종료일/chainGap 은 입력 불변 id carry, 멱등(같은 입력 2회 sha 동일), 첫 빌드 `--prev-work`·`--ledger-prev` 필수 | 체인 09-16→17→18 rc 0·게이트 21종 PASS, 17~20분/8.3GB; R-D2 FAIL(JAGX raw/계수 혼합 +1.37e11 통과, events 재생성 → GGP 이중 계상) → 재작업 → 재검수 PASS(잔여 P2 6) |
| D3 체인 재현 하니스 | `tests/chain_reproduce.py`·`chain_compare.py`: rc2f3 → 체인 09-16→18 vs 한 번에(full-lifecycle)·전량 재조회·원장 한 번·day0 재실행, 셀 diff 18 클래스 귀속 + 값 게이트(새 과거 |ret|>5, 연결 부모 중복 자회사 leg ±3일), asOf 시각 재검 | 최종 코드 exit 0: 원장 sha 4경로 동일, diff 184,101셀 전부 귀속(미귀속 0), 값 게이트 0/0, 태그 누적·경로 의존·events 재생성·firstTradeDate 전환 전부 0. 잔여: 재조회 집합 밖 벤더 재기술 3,763셀+38행(주 1회 `--full-refetch` 스윕), 증분 lifecycle first_* 재집계(ASBH, returns 영향 0) |
| D4 운영 배선 | 벤더 적재 시각 실측(ciq 순번03 07:00 임계 통과·순번04 10:00 종가 확정·순번05 14:00 거래량·분할 14:00·배당 16:00·IDC 18:00 KST) → 기본 실행 슬롯 **16:10 KST(EDT)/17:10(EST)**, 크론 초안 `cache_v21/cron/{update_daily.sh(flock·프로브·종료코드 조치·경보 플래그), readiness_probe.py, prune_builds.sh, crontab.example, logrotate.conf}`(미등록), 보존(로컬 current+3·61 .prev 2 + 월 스냅샷), 정책(`--strict-exposure` 라이브 전환 시, 엔진 `unresolved_policy` 연구 warn·라이브 strict), 체크리스트 README §7·§11·§12 | 플러밍 시험 통과 |

리드 최종: 09-18 한 걸음 독립 재실행(D2 b2 입력) 결과 대조(§12-3), D3 최종 하니스 확인. 발행본은 rc2f3 유지(일별 빌드의 61 발행은 크론 등록과 함께 사용자 확인 후).

### 12-3. 남은 것
| # | 항목 |
|---|---|
| 1 | 크론 등록·경보 전송(MTA 없음, `ALERT_CMD` 훅)·61 발행 전환 — 사용자 확인 후 |
| 2 | 주 1회 `--full-refetch` 스윕 운영 편입(체인 사각지대 3,763셀), not_adopted events 1,012 의 채택/기각 절차, revisions 에 asOf 표기, 증분 lifecycle first_* 재집계 |
| 3 | 라이브 사이징 검증(새 데이터로 신호 재생성 → 주문·사이징) — 다음 단계 |
| 4 | 코드 미커밋(엔진 + cache_v21 rc2f/E/D 변경, cron/, overrides/rc2f/) |
