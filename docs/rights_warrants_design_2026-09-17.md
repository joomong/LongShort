# 권리·워런트 설계 노트 — rc2 E3 (2026-09-17)

상태: §3 만료 처리·§4-3 거절 경로·§5 진단·§6 테스트 1~8 은 **구현됨**(E3, `docs/corporate_actions_engine_2026-09-17.md` §1·§2.4·§3·§4·§7 32~38). §4-1 자동 행사는 설계만. 테스트 9(HTTP) 는 fixture 의 D→W 워런트로 기존 HTTP 테스트에 흡수.

P11: "GS 가 미인도했다는 이유로 제외하지 않음. 원천의 교부 조건 보존, 평가·매각·행사·만료 지원, 자동 행사 여부는 별도 설정(기본 off)". 이 노트는 원장 `rights`/`warrant` 행이 E1 의 주식 leg 경로(`docs/corporate_actions_engine_2026-09-17.md` §3)를 어떻게 타는지, 만료(`rights_expired`)·자동 행사(`auto_exercise`)를 어떻게 넣을지, 무엇을 왜 미루는지를 정한다. 구현은 E2 리뷰 판정 뒤 E3 로.

## 1. 현재 상태 (E1 기준, 변경 없음)

| 항목 | 현재 |
|---|---|
| 원장(R1 raw) | `eventType ∈ {rights, warrant}`, `legType = rights`, `childTradingItemId` = 권리·워런트 증권 id(상장 시), `childRatio` = 구주 1주당 권리 수, `subscriptionPrice`(행사가, 기초주 1주당), `expiryDate`, `exerciseRatio`(권리 1개당 기초주 수). 스테이지 R1: rights 64 + warrant 1 = 65 행, **전부 `status=unresolved`**(64 건 `no_terms`: 자회사·비율 미상, GME 1 건만 조건 보존) |
| 엔진 export(§3-3) | `legType=rights` 65 행이 나가지만 **`subscriptionPrice`·`expiryDate`·`exerciseRatio` 는 export 컬럼에 없다**(R1 `CA_EXPORT_COLUMNS`). E3 는 R1 에 세 컬럼 추가를 요청한다(§2) |
| 엔진(E1) | `rights` 행 = 주식 leg(`stock_leg=true, rights=true`, `/api/config.legsRights`). 자회사(권리 증권)가 패널에 있고 시세가 있으면 배당락일에 `w_child = w × childRatioCache × close_right / m` 생성·익일 전략 목표(NaN → 0 청산); 시세가 없으면 `pending`; 패널에 없으면 `unpriced`. 만료·행사는 없음 → 시세 없는 권리는 **끝까지 pending** 으로 남아 `terminalDistributionPending` 에만 보인다 |

## 2. export 추가 컬럼 (R1 요청, 캐시 단위)

| 컬럼 | 정의 | 비고 |
|---|---|---|
| `expiryDate` | 원장 그대로(ts). null = 미상 | 만료 처리의 유일한 트리거 |
| `subscriptionPriceCache` | `subscriptionPrice / sf_parent(exDate 직전)` — 기초주 1 현재주당 행사가(`cashPerShareCache` 와 같은 환산) | 행사 전용 |
| `exerciseRatioCache` | `exerciseRatio × sf_parent(exDate 직전) / sf_child(exDate)` — 권리 1 현재단위당 기초 현재주 수(`childRatioCache` 와 같은 환산) | 행사 전용 |

세 컬럼은 **선택**(config 키 `expiry_col`, `subscription_price_col`, `exercise_ratio_col`, 기본값 위 이름; 파일에 `expiryDate` 가 없으면 만료 기능이 꺼지고 `/api/config.corporateActions.rightsTermsLoaded=false`, 있으면 null 인 rights 행은 `rightsWithoutExpiry`). 기존 14 컬럼 계약은 그대로. 엔진은 `subscriptionPriceCache`·`exerciseRatioCache` 를 leg 에 실어 두기만 한다(행사 미구현). R1 이 `export_ca_ledger` 에 세 컬럼을 추가하면 자동으로 읽힌다.

## 3. 주식 leg 경로 위의 권리 상태 기계

| 상태 | 조건 | E1 처리(유지) | E3 추가 |
|---|---|---|---|
| `childClose` | 배당락일에 권리 증권 시세 | 생성·익일 매각(전략 목표 NaN → 0, 기존 fee) | 없음 |
| `ledgerValue` | 시세 없음, 원장 WI/추정값 | 생성(추정 표시) | 없음 |
| `pending` | 시세 없음, 값 없음 | 부모 `w, m` 고정 대기 → 첫 시세에 생성(`firstQuote`) | **만료 검사**: `expiryDate` 가 지난 첫 bar(`bar_ts > day_end(expiryDate)`)까지 시세가 없으면 leg 를 닫는다 → `rights_expired` |
| `unpriced` | 권리 증권이 패널에 없음 / ratio 없음 / drop | 손익 미인식, 배당락일에 레코드 | 만료 검사 대상 아님(이미 닫힘) |
| 보유 중 동결 | 생성된 권리가 시세를 잃음 | carry 동결(청산 가정 없음) | 만료 지난 첫 bar 에 **가치 0 확정**: 동결 가중치에 R = −1 을 events 정산과 같은 경로(`book_settlements` 의 explicit return)로 적용, fee 없음, `rights_expired_held`/`rights_expired_gross`(`missingData.delistings` 에도 집계). 만료 뒤에도 시세를 찍으면 침묵 bar 까지 평가·거래(events 의 종료일 뒤 시세와 같은 규칙). 증권의 만료 = 그 증권을 교부한 rights leg 들의 가장 이른 만료 |

`rights_expired` 와 `unpriced` 의 차이: unpriced 는 "이 패널에서 평가 불가" 로 **배당락일에** 닫히고(값 미인식·미래에도 없음), expired 는 "권리는 존재했고 기다렸으나 만료일까지 시세가 없어 가치 0 으로 확정" 으로 **만료 bar 에** 닫힌다. 손익은 둘 다 0 이지만 레코드 `source`(`unpriced` vs `expired`), 닫힌 bar, `distribution_pending` 의 감소 시점이 다르다. 만료일이 null 인 rights 행은 E1 과 같이 끝까지 pending(진단 `rightsWithoutExpiry` 로 노출).

`expiryDate` 가 패널 마지막 bar 이후면 만료는 일어나지 않고 `terminalDistributionPending` 에 남는다. 만료 bar 에 첫 시세가 찍혀도 권리는 이미 소멸(만료 검사가 시세 검사보다 앞); 만료 **전** 마지막 bar 의 첫 시세는 생성한다. 만료일이 배당락일 이전인 행은 로더가 거부한다.

행정 정산 우선순위(`settle()`): **events 정산 > 권리 만료 > stale-quote**. 만료가 알려진 권리 증권은 stale 규칙에서 제외된다 — 그렇지 않으면 `stale_quote_settle_bars=N` 이 만료 전에 default return(0)으로 정산해 만료 규칙이 영영 발동하지 않는다(리뷰 프로브 D-1, 테스트 39). 한 증권을 여러 rights leg(여러 부모)가 교부하면 증권의 만료 bar = 그 leg 들의 **최소** `expiry_bar`; pending leg 의 만료는 leg 별 `expiry_bar` 를 쓴다.

## 4. `auto_exercise` (기본 false) — 설계와 유보 사유

### 4-1. true 일 때의 회계(설계)

행사 가능 마지막 bar = `expiryDate` 당일 또는 그 이전 마지막 패널 bar(E−1 bar). 그 bar 에 보유 중인(생성돼 시세가 있는) 권리 가중치 `w_r`(마크 `p_r`), 기초주 종가 `p_u`, 행사가 `K = subscriptionPriceCache`, 비율 `x = exerciseRatioCache`:

| 단계 | 식 | 비고 |
|---|---|---|
| 내재가치 판정 | `x × (p_u − K) > 0` 일 때만 행사, 아니면 소멸(§3 만료와 동일, 가치 0) | 등가치·외가치는 행사하지 않음 |
| 권리 수 | `n = w_r / p_r`(fixed book, Book=1) | |
| 기초주 취득 | `w_u += n × x × p_u`(종가 마크로 장부에 얹음, received 경로와 같이 체결 뒤) | 자회사 생성과 같은 "받은 가중치" 규칙 |
| 현금 지출 | `c = n × x × K` — **fixed book 에는 현금 잔고가 없으므로** 이를 K 가격의 기초주 **매수 체결**로 취급: `turnover += c`, fee = `c × fee_rate` | 다음 bar 에 전략 목표가 되돌리는 것은 평소 규칙 |
| 손익 | `pnl += n × [x × (p_u − K)] − w_r` (권리 마크 → 내재가치) | 권리 leg 는 닫힘(`rights_exercised`) |

pending(시세 없는) 권리는 행사할 수 없다(권리 마크·수량이 없음) → 만료 규칙. 부분 행사·현금 결제형 워런트·조기 행사는 범위 밖.

### 4-2. 왜 미루나

1. **export 에 조건 컬럼이 없다**(§2): 캐시 단위 환산(`sf_parent`, `sf_child`)은 R1 소관.
2. **현금 잔고 부재**: 지출 `c` 를 "K 가격 매수" 로 근사하면 그 bar 의 gross 노출이 `c` 만큼 늘어 동결 예산·정규화와 상호작용한다(다음 bar 전략 목표가 되돌리지만 하루치 노출·fee 가 생김). 이 근사가 V1 대조값과 맞는지 실사례가 없다.
3. **실측 부재**: GS 관측(§2 BRIEF)엔 행사 사례가 없고 GME 워런트는 미인도 — 오라클 없이 회계를 확정하지 않는다(P10 정신).
4. 원장 65 행 중 64 행이 조건 미상(`no_terms`) — 행사 로직이 적용될 행이 현재 1 개.

### 4-3. 설정·거절 경로(E3 1차에 구현)

```yaml
backtest:
  corporate_actions:
    auto_exercise: false      # 기본. true 는 미구현 → 기동 거부
    expiry_col: expiryDate    # 선택 컬럼(§2)
    subscription_price_col: subscriptionPriceCache
    exercise_ratio_col: exerciseRatioCache
```

`auto_exercise: true` → `config_loader` 가 `"backtest.corporate_actions.auto_exercise is not implemented (docs/rights_warrants_design_2026-09-17.md §4); set false or remove the key"` 로 **기동 거부**(조용한 무시 금지). `/api/config.corporateActions.autoExercise=false` 를 항상 보고.

## 5. 진단·JSON (E3 1차 = 만료만)

| 위치 | 필드 |
|---|---|
| bar 진단 `CorporateActionDiagnostics` | `rights_expired`(만료로 닫힌 pending leg 수), `rights_expired_held`(동결 보유가 0 확정된 수), `rights_expired_gross`(그 노출) — 구현됨 |
| `DistributionRecord.source` | `Expired`(`bar` = 만료 bar, `ex_bar` = 배당락일, `child_weight = 0`) — 구현됨 |
| `/api/simulate.corporateActions` | `rightsExpired`, `rightsExpiredHeld`, `rightsExpiredGross`; `distributionLog` 에 `source: expired` — 구현됨(키 13 개, HTTP 테스트 고정) |
| `/api/config.corporateActions` | `legsRights`, `rightsTermsLoaded`, `rightsWithoutExpiry`, `autoExercise`(항상 false) — 구현됨 |
| 기동 로그 | "N rights with terms, M without expiry" / "without terms columns" — 구현됨 |

## 6. 테스트 계획 → 구현 (`test_corporate_actions.cpp` 32~38, `test_config_loader`, HTTP; 전부 D0/D1)

| # | 케이스 | 기대 |
|---|---|---|
| 1 | 권리 증권이 배당락일에 시세 → 생성·익일 fee 매각 | E1 과 동일(회귀) |
| 2 | 시세 없음 + 만료일 패널 안 → 만료 bar 에 `rights_expired` 1, pending 0, 레코드 `expired`, 손익 0 | unpriced 와 bar·source 구분 |
| 3 | 만료 직전 bar 에 첫 시세 → `firstQuote` 생성, 만료 미발동 | 순서: 시세 우선 |
| 4 | 생성 뒤 동결 상태로 만료 → R=−1 정산, fee 0, `rights_expired_held` | 기존 정산 경로 재사용 |
| 5 | 만료일 null → 끝까지 pending, `rightsWithoutExpiry` | E1 거동 유지 |
| 6 | 만료일이 패널 뒤 → `terminalDistributionPending` | |
| 7 | 로더: 세 컬럼 없음 → 기능 off·`rightsTermsLoaded=false`; 있음 → leg 에 실림; `auto_exercise: true` → 기동 거부 메시지 | config 테스트 |
| 8 | 비트 동일: rights 행 없는 원장·rights 있으나 만료일 null 은 E1 결과와 `EXPECT_EQ` | |
| 9 | HTTP: fixture 에 만료일 있는 rights 행 1 개 추가 → `rightsExpired 1`, `distributionLog` `expired` | 계약 |

## 7. GME 2025-10-03 워런트 (실사례)

| 항목 | 값(R1 원장 `24268335_20251003_warrant_0`) |
|---|---|
| 부모 / 자회사 | GME 24268335 / GME.WS 1962567035(NYSE Equity Warrant, 2025-10-08 부터 상장, `ciqpriceequity` 행 없음, base_data 에 없음) |
| 조건 | `childRatio 0.1`(10주당 워런트 1), `subscriptionPrice 32.0`, `exerciseRatio 1.0`, `expiryDate 2026-10-30`; source `manual_override`(GameStop 2025-09-30 보도자료), status `unresolved`, `childValueSource unknown` |
| GS | Spin 으로 관측, **미인도** — 도구는 제외하지 않는다(P11) |
| 엔진 현재 | 자회사가 패널에 없으므로 배당락일에 `unpriced`(손실 미인식), `/api/config.unpricedChildren`; export 에 `expiryDate` 가 없어 `rightsTermsLoaded=false` |
| R3 aux 로 GME.WS 가 패널에 들어오고 R1 이 만료 컬럼을 내보내면 | 2025-10-03~07 시세 없음 → `pending`; 첫 시세(2025-10-08~) 에 `firstQuote` 로 `w_ws = w_GME × 0.1 × close_ws / close_GME(10-02)` 생성 → 다음 bar 전략 목표(NaN → 0) 매각 fee. 시세가 끝내 없으면 2026-10-30 다음 bar 에 `rights_expired`(fixture 의 D→W 워런트가 같은 경로를 HTTP 로 검증) |
| `auto_exercise: true` 였다면 | 2026-10-30 이전 마지막 bar 에 `1.0 × (close_GME − 32) > 0` 이면 워런트 수 × 1.0 주를 32 에 매수 체결(turnover·fee), 아니면 소멸 — §4-2 로 유보 |

## 8. 범위 밖(명시)

현금 결제형·조기 행사·부분 행사, 권리 매매의 별도 스프레드, 행사 결제 지연(T+n), 권리 발행회사가 부모와 다른 경우(제3자 워런트)는 다루지 않는다.
