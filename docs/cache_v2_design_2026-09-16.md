# cache_v2 설계 — raw 기준 저장 + 계수 + 총수익률 (2026-09-16)

목적: longshort platform·sim이 쓸 일별 미국 현물 캐시를 **한 가지 가격 기준(as-quoted raw) + 조정계수 2종 + 배당 반영 수익률**로 다시 설계한다. 2026-09-14 프로토타입(`cache_v2_plan_2026-09-14.md`, 분할조정가 기준)의 §3 저장 설계를 대체하고, 원천 조사(`xfeed_price_tables_2026-09-16.md`)의 결론을 저장·갱신 절차로 옮긴다. 이 문서는 계획이며 코드 변경은 아직 없다.

## 0. 결정 요약

| # | 결정 | 근거 |
|---|---|---|
| 1 | 가격은 **raw(as-quoted) 한 벌만** 저장. 분할조정·배당조정 가격은 저장하지 않고 소비자가 계수로 계산 | raw는 재작성이 없어 일별 append가 영구 정합. v1 기준 혼합 사고(옛 행 미갱신)가 구조적으로 사라짐. 분할조정가 = raw ÷ splitFactor, 배당조정가 = raw ÷ (splitFactor ÷ divAdjFactor) |
| 2 | 계수 2종 저장: `splitFactor`(= CIQ `adjustmentfactor`, 그 날부터 스냅샷일까지 누적 분할·주식배당·교환비), `divAdjFactor`(= `ciqpriceequitydivadjfactor`, 배당·스핀오프 누적) | 둘 다 작은 테이블(`ciqsplitcache` 17만 행, `divadjfactor` 450만 행)에서 매일 통째로 재수신 가능. 300종목 검증에서 `ciqsplitcache` 구간이 일별 `adjustmentfactor`를 300/300 정확히 재현 |
| 3 | 수익률은 `returns/total` 한 패널을 1급 산출물로 저장: 분할·현금배당·특별배당·스핀오프(벤더 평가) 반영 | 소비자가 계수를 잘못 곱해 이중계산할 여지를 없앰. 정산(인수합병 R)은 포함하지 않음 |
| 4 | 값 원천은 `ciqpriceequity × adjustmentfactor`, `miprice`는 검증·종료일 판정 전용 | 값은 동일(2010+ 불일치 사실상 0), ciq만 정정 반영·계보 완전. 원천 하나로 "누가 이기나" 규칙을 없앰 |
| 5 | PIT 종료: `lastTradeDate = min(ciq 마지막 종가일, miprice 마지막 종가일, events 종료일 캡)` 이후 `mask/tradable` False | 두 테이블 모두 승계사 가격 꼬리가 있고 방향이 다름(조사 §4-4) |
| 6 | 인수합병·id 변경은 시계열에 넣지 않고 events 파일(v3 계약)로 엔진이 정산 bar에서 계산. raw 기준이므로 캐시단위 환산(k 앵커) 불필요 | Phase 2d~2g의 환산 기계장치가 raw 기준에서는 항등(k=1) |
| 7 | 저장 용량 ≈ 1.7GB(2000~) / ≈ 1.0GB(2010~) [추정] | 프로토타입 2.1GB(mid·bid·ask·midPrice 0.63GB 포함) 대비 감소, "raw·adj·TR 3벌" 안(≈4GB) 대비 절반 이하 |

## 1. 저장 레이아웃

경로(안): `/home/jgo/61/data2/temp_stock/cache_v2_cash_1d/`에 **새 디렉토리로 빌드 후 게이트 통과 시 교체**(프로토타입은 `cache_v2_cash_1d.proto_2026-09-14/`로 이름 변경, 비교 노트북 경로 갱신). 팀 v1(`/home/base/cache`)은 무접촉.

모든 wide 패널: index = 거래일(2000-01-03~최근 완결일), columns = `tradingItemId` int64(모든 파일 동일 순서), 값 float32(volume은 float64). NaN = 행 없음.

| 디렉토리/파일 | 내용 | 원천 | 크기 [추정] |
|---|---|---|---|
| `raw/open, high, low, close, vwap` | as-quoted 가격(그날 실제 거래 단위) | `ciqpriceequity.price* × adjustmentfactor` (일별 append도 같은 식) | 5 × ~155MB |
| `raw/volume` | 실제 거래 주수(float64) | `volume ÷ adjustmentfactor` | ~300MB |
| `factor/splitFactor` | 누적 분할계수(그 날 → 스냅샷일). 분할조정가 = raw ÷ splitFactor | `ciqsplitcache` 구간(첫 구간 이전 = csf×lsf), 검증용으로 `ciqpriceequity.adjustmentfactor` 대조 | ~20MB |
| `factor/divAdjFactor` | 배당·스핀오프 누적계수(최신 1.0). 결측 = 1.0(배당 없는 종목), 첫 구간 이전 = 첫 구간값 | `ciqpriceequitydivadjfactor` | ~30MB |
| `returns/total` | `((raw/splitFactor)·divAdjFactor).pct_change()` — 직전 **관측** 종가 대비(결측 구간 건너뜀, 엔진 carry 규약과 동일). `lastTradeDate` 이후 NaN | 위 세 패널 | ~180MB |
| `returns/price` | `(raw/splitFactor).pct_change()` — v1 `HoldingReturn_Usd`와 같은 정의, 분해·대조용 | 〃 | ~180MB (선택) |
| `marketCap_USD` | 시총(USD, 종가 있는 날만) | `ciqmarketcap` × FX | ~175MB |
| `mask/tradable` | `raw/close.notna() & date ≤ lastTradeDate & 계보 중복 구간 아님` | master | ~20MB |
| `universe/ALL, Top1000, Top700, Top500, Top300, Top200, Top100, Top10` | v1 규칙(TokensToRemove 포함)을 raw 기준으로 계산. 달러거래대금 `close×volume`은 기준 무관 | raw·marketCap·currentPrimary | 8 × ~20MB |
| `master/master.parquet` | 프로토타입 컬럼 + `lastTradeDate, lastTradeSource{ciq,mi,events}, ciqLastCloseDate, miFirstDate, miLastCloseDate, backfilledBefore, lineagePrevId, lineageNextId, securityId, gvkey/iid` | `ciqtradingitem/security/company`, `sec_idhist`, coverage | 작음 |
| `events/events.parquet` | Phase 2 v3 계약. raw 기준이라 `cashPerShareCache = cashPerShare`, `successorRatioCache = successorRatio`; 로더 가드(`successorCacheClose` 대조)는 유지 | `co_acthist` + override CSV | 작음 |
| `dividends/dividends_long.parquet`, `distributions/` | 배당·분할·스핀오프 사건 테이블(프로토타입 그대로, 현금식 수익률·감사용) | `ciqdividend`, `ciqsplit`, 조사 로그 | ~50MB |
| `MANIFEST.json`, `README.md`, `AUDIT.md` | asOfDate, 원천 최대 날짜, 파일 sha256·행·열, 게이트 결과 | – | – |

저장하지 않는 것: 분할조정 OHLCV, 배당조정 OHLCV, `mid/bid/ask/midPrice`(플랫폼 미사용, 필요 시 DB에서 별도 모듈), `mibestpricedate`(파생), v1 호환 `BaseData/` 레이아웃(필요하면 §5의 export 스크립트로 생성물만 만듦).

### 1-1. 소비자 산식(로더가 계산)

| 소비자가 원하는 것 | 산식 | 비고 |
|---|---|---|
| 분할조정 가격(신호용 기본 `close`) | `raw / splitFactor` | 전 구간 스냅샷일 기준. 과거 분할이 새로 생기면 `splitFactor` 열만 바뀌고 raw는 불변 |
| 배당+분할 조정 가격(WQ `close/fwdcumadjfactor`) | `raw / (splitFactor / divAdjFactor)` = `raw × divAdjFactor / splitFactor` | `fwdCumAdjFactor = splitFactor / divAdjFactor` |
| 가격 수준 필터(>$5 등), 딜 조건 적용 | `raw` 그대로 | PIT 정확 |
| 보유 손익 | `returns/total` | close 비율로 다시 계산하지 말 것(분할일 −75%, 배당 누락) |
| 당일 체결 손익(close/vwap, close/open) | `raw` 끼리 비율 | 같은 날이면 기준 무관 |
| 분할조정 거래량 | `raw/volume × splitFactor` | 달러거래대금은 `raw close × raw volume` |
| 현금식 총수익률(현금 보유 가정) | `(P + D_ex)/P_prev − 1`, `D_ex = dividends_long` 배당락일(첫 거래일≥ex) 금액 ÷ splitFactor(ex일) | 계수식과 차이는 2차항, 스핀오프는 미포함 |

## 2. 초기 구축 절차

| 단계 | 내용 | 산출/게이트 |
|---|---|---|
| B1 종목 집합·마스터 | 프로토타입 규칙(v1 규칙 ∪ Compustat 주요거래소 이력, 펀드 스크린) 그대로. `sec_idhist`로 계보(gvkey/iid → 옛/새 tradingItemId) 표시 | master, 계보 목록 |
| B2 가격 스냅샷 | `ciqpriceequity` 전 구간 1회 수신(13.5분 실측) → `raw = price × adjustmentfactor`, `volume ÷ adjustmentfactor`. `adjustmentfactor` 일별 값은 검증용으로 보관(저장 안 함) | raw/* |
| B3 계수 | `ciqsplitcache` → `splitFactor`; B2의 `adjustmentfactor`와 전수 대조(허용 1e-6). `ciqpriceequitydivadjfactor` → `divAdjFactor` | G-SPLIT: 불일치 종목 0(300표본 실측 0) |
| B4 miprice 대조 | 2010+ 전 종목 `raw/close` vs `miprice.priceclose` 전수 대조(0.1% 초과 목록·연도별), OHLV도 동일. ciq 값 유지, 목록은 AUDIT | G-RAW: 불일치 셀 목록이 정정 사례로 설명됨 |
| B5 종료일·마스크 | `lastTradeDate = min(ciq 마지막 종가일, mi 마지막 종가일, events 종료일 캡)`; 계보 중복 구간(옛 id 종료일 이후 새 id만 유효) → `mask/tradable` | G-TAIL: `lastTradeDate` 이후 tradable 0, 같은 securityId 두 id 동시 tradable 0 |
| B6 수익률 | `returns/total`, `returns/price` 계산 | G-JUMP: `factor` 변경일에 `returns/price` 절대값 >40%이면서 raw 변동 <40%인 셀 0(v1 대조군 16건 재현 확인). G-DIV: 새 계수 구간마다 `abs(step − (1 − D/P_prev))` <1e-6(같은 날 비현금 분배 제외). G-ORACLE: 300종목 현금식과 최대 차 <1e-4(스핀오프일 제외) |
| B7 유니버스 | v1 규칙 재계산, v1 스냅샷과 중첩 | G-UNIV: v1rule 변형 Top500 중첩 ≥0.99 |
| B8 events 재생성 | `build_events.py`를 raw 기준으로(환산 제거, 가드 유지). Altaba 등 override 승계 | 리드 독립검사 불변식 0건 위반 |
| B9 감사·발행 | AUDIT.md(게이트 결과·알려진 예외 목록), MANIFEST sha256, README | 발행 후 비교 노트북 경로 갱신 |

## 3. 일별 갱신 절차 (`update_cache_v2.py`, 매일 T−1 완결 후)

| 단계 | 내용 | 실패 시 |
|---|---|---|
| U1 마스터 델타 | 새 tradingItemId(신규상장·id 변경)·`securityenddate`·status·거래소 변화 수신. 새 id는 전 이력 1회 수신해 열 추가, 계보 연결 | 열 추가 실패 시 중단 |
| U2 raw append | T−5~T−1 행을 `ciqpriceequity`에서 받아 `× adjustmentfactor` 후 **덮어쓰기**(늦은 정정 흡수). 같은 구간 `miprice`와 대조 → 불일치 로그. T−1 행은 ciq·mi 종가가 모두 있고 일치할 때만 확정, 아니면 잠정 플래그 | 종가 수 < 임계(v1의 5,500 규칙 계승) → 중단 |
| U3 계수 전량 재수신 | `ciqsplitcache`·`divadjfactor` 전체 → 두 계수 패널 재작성. 저장본 대비 바뀐 (종목, 날짜, 배수) 목록 = 새 분할·정정 → 로그 | 변화 종목의 raw 불변 확인 실패 → 중단 |
| U4 종료일·마스크 | `lastTradeDate` 갱신(ciq 끊김·mi 끊김·종료일), `mask/tradable` 재작성 | – |
| U5 수익률·시총·유니버스 | `returns/*` 전량 재계산(벡터 연산, 초 단위), `marketCap` append, 유니버스 재계산(롤링 100일) | – |
| U6 events | 주 1회 또는 `co_acthist` 갱신 시 재생성(로더 스케줄 문제 해결 전까지는 수동) | – |
| U7 게이트·발행 | §2 게이트 중 일별용(G-RAW 최근 5일, G-SPLIT, G-JUMP, G-TAIL) 통과 시 스테이징 → sha256 → 원자적 교체, MANIFEST에 asOfDate | 실패 시 이전 버전 유지, 리포트만 |

소요 [추정]: U2 수 초, U3 1~2분, U5 2~5분, 파일 재작성 1GB(로컬 디스크 1~2분, 61로는 rsync). 하루 총 5~10분. 빌드는 gqdata 로컬에서 하고 61에는 결과만 동기화.

## 4. 인수합병·계보·배당의 처리 경계

| 사건 | 시계열(raw·factor·returns) | events/master | 엔진 |
|---|---|---|---|
| 분할·주식배당·역분할·교환비 재기준 | `splitFactor`에 반영, raw 불변 | – | 분할조정 뷰 자동 |
| 현금·특별배당 | `divAdjFactor` 스텝 → `returns/total` | `dividends_long` 참고 | 배당락일 인식, 지급일 미모델 |
| 스핀오프 | `divAdjFactor` 스텝(벤더 평가액) → `returns/total` | `distributions` 상태(ok 351건)·연구 로그 | 신설사 주식 이전은 후속(opt-in) |
| 현금·주식 인수합병, 파산 | `lastTradeDate`에서 끝, 이후 NaN | `endDate, cashPerShare, successor, ratio`(raw 단위) | 정산 bar에서 `R = (cash + ratio·P_succ)/P_last − 1` 계산, 현금 실현·주식 이전 |
| 개명·재상장·법인이전(같은 증권, id 변경) | 새 id에 ciq가 이어붙인 과거 보존(신호 워밍업용), `mask/tradable`은 옛 id 종료 다음 날부터 | `lineagePrevId/NextId`, events에 ratio 1 승계 행 | 무비용 이전 |
| OTC 이동 후 하락·파산 | OTC 구간 raw 포함(프로토타입 규칙) | 종료일·BK 코드 | 기본 R 또는 파일 R |

## 5. 플랫폼(longshort_platform) 변경 항목

| 항목 | 변경 | 검증 |
|---|---|---|
| 로더 | 캐시 `layout: 2` 인식: `raw/*`, `factor/*` 읽고 `close = raw/splitFactor`(및 open/high/low/vwap) 생성, `close_raw`·`close_tr` 별칭 제공, `volume`(분할조정)·`volume_raw` | 기존 v1 레이아웃 경로 유지(`layout: 1`) |
| 보유 수익률 | `return_col` 기본을 `returns/total`로. 파생 `returns`(close 비율)는 raw에 적용 금지(분할일 오류) | 분할·배당락 포함 구간 fixture 비트 검사 |
| 마스크 | `mask/tradable ∧ universe` | Top500 전기간 무중단 실행 |
| events | raw 단위 파일 읽기(`*Cache` 컬럼 = 원 조건), 스테일 가드 유지 | 하네스 30/30, 원장 HTTP 테스트 |
| 트레이딩 레그(미구현) | 같은 날 raw 비율 사용 명시 | – |
| 레거시 sim 호환 | `export_v1_layout.py`: `BaseData/{open,…}` 분할조정 + `Returns/HoldingReturn_Usd`를 생성물로 출력(원천 아님, 재현 가능) | v1 스냅샷 대비 셀 일치율 보고 |

## 6. 결정 요청

| # | 항목 | 제안 |
|---|---|---|
| D1 | 시작일 2000 vs 2010 | 2000(추가 비용 ≈0.6GB, 위기 구간 테스트 가치). 유니버스는 2010+만 검증 |
| D2 | `mid/bid/ask/midPrice` | 저장 안 함(플랫폼 미사용) |
| D3 | `returns/price` 저장 | 저장(180MB, v1 대조·분해에 상시 필요) |
| D4 | 스핀오프 가치 | `returns/total`은 벤더 계수 일원화, Distributions는 감사·후속 opt-in 이전용 |
| D5 | 일별 값 원천 | ciq×adjf 단일, miprice는 검증·종료 판정만 |
| D6 | 배포 | 새 디렉토리 빌드 → 게이트 → 교체, 프로토타입 보존, 비교 노트북 경로 갱신 |
| D7 | 일별 갱신 병행 기간 | 2주간 v1 대비 일별 diff 리포트 후 플랫폼 기본 캐시 전환 |

## 7. 이 문서가 답하지 않은 것
- `miprice`가 더 이른 이력을 가진 종목(Top1000 21종목, FRHC 12년 등)의 앞부분 채움 — 기본은 채우지 않음, 목록만 AUDIT.
- 두 테이블의 일중 확정 시각(2026-09-15 당일 종가 차이 관측) — 하루치를 시간대별로 스냅샷해 확인 후 U2의 확정 규칙 조정.
- 로더 스케줄(`co_acthist` 2026-02-20 이후 미적재) — 관리자 요청서(`xfeed_loader_schedule_request_2026-09-11.md`) 처리 대기.
- 트레이딩 레그·배당 현금 다리·스핀오프 주식 이전의 엔진 구현 — 별도 단계.
