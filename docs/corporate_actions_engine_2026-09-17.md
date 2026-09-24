# 엔진 기업행동 원장 회계 — 2026-09-17 (rc2 E1)

`backtest.corporate_actions:` 로 로더 export 원장(`engine/ca_ledger_cache.parquet`, rc2 BRIEF §3-3)을 읽어 배당락일에 **현금식 총수익률**(P2)을 장부에 반영한다. 기존 fixed-book 가중치·마크 회계(`docs/missing_data_validation_2026-09-11.md`)를 그대로 두고, 배당락일 bar 에 현금 leg 와 주식 leg 를 추가로 기장한다. 원장이 없는 설정은 이전 엔진과 **비트 동일**하다(§7 테스트 13).

## 1. 설정

```yaml
backtest:
  corporate_actions:
    path: data/engine/ca_ledger_cache.parquet   # 필수. 상대 경로는 config 위치 기준
    id_col: tradingItemId              # 부모 id, int64 또는 문자열(패널 심볼과 같은 문자열)
    ex_date_col: exDate                # timestamp / date32 / date64 / 'YYYY-MM-DD'
    type_col: eventType
    cash_col: cashPerShareCache        # 세전 현금/구주 1주, 캐시 주식수 기준. NaN = 현금 leg 없음
    child_col: childTradingItemId      # 자회사 id. null = 주식 leg 없음
    child_ratio_col: childRatioCache   # 구주 1주당 자회사 주수(캐시 기준)
    child_close_col: childCloseAtExCache
    parent_close_col: parentCloseAtExCache          # staleness 앵커(배당락일 부모 종가)
    parent_close_before_col: parentCloseBeforeCache # staleness 앵커(직전 bar 부모 종가)
    parent_factor_col: parentFactorCache            # split_adj 패널에서는 1.0 이어야 함
    leg_col: legType                   # cash | stock | rights | quantity — 행의 leg 를 정한다
    estimate_col: isEstimate           # bool, 원장의 추정 표시
    value_source_col: childValueSource # child_close_ex | wi_close | event_value | vendor_implied | unknown (P10 ①②③④)
    status_col: status                 # ok | estimated | unresolved
    expiry_col: expiryDate             # 권리·워런트 만료일 — 선택 컬럼(없으면 만료 처리 없음)
    subscription_price_col: subscriptionPriceCache   # 선택, 행사 설계용(미사용)
    exercise_ratio_col: exerciseRatioCache           # 선택, 행사 설계용(미사용)
    child_value_col: childValuePerShareCache         # 선택, 부모 1주당 가치(§9-6: 자회사가 패널 밖이면 손익으로 기장)
    estimate_reason_col: estimateReason              # 선택, flagged 행의 추정 사유(§9-8: ratio/date/value)
    auto_exercise: false               # true 는 미구현 → 기동 거부
    apply_dividends: true
    apply_distributions: true
    unpriced_child: pending            # pending | drop
    stale_check: true
    unresolved_policy: warn            # warn | strict — 보유 중 손익 미확정 사건(§9)
```

| 키 | 기본값 | 의미 |
|---|---|---|
| `path` | (없음 = 원장 미사용) | 설정했는데 파일이 없으면 기동 실패 |
| `*_col` | 로더 export 컬럼명 | 열아홉 개 모두 서로 달라야 하고 비어 있으면 안 됨. 필수 14 컬럼이 파일에 없으면 기동 실패; `expiry_col`·`subscription_price_col`·`exercise_ratio_col`(`docs/rights_warrants_design_2026-09-17.md` §2)·`child_value_col`(§9-6)·`estimate_reason_col`(§9-8) 다섯 개는 선택 |
| `auto_exercise` | false | true 는 "not implemented ... set it to false or remove the key" 로 기동 거부(조용한 무시 금지) |
| `apply_dividends` | true | 현금 leg 기장 여부 |
| `apply_distributions` | true | 주식 leg 기장 여부 |
| `unpriced_child` | pending | 패널에는 있으나 배당락일 시세가 없는 자회사: 첫 관측 종가까지 대기(pending) / 포기(drop, `distribution_unpriced`) |
| `stale_check` | true | 앵커 대조. false 면 대조 없이 적재하고 `/api/config` 에 `staleCheck: false` |
| `unresolved_policy` | warn | 보유 중 부모의 손익 미확정 사건(§9): `warn` 은 기록·보고, `strict` 는 첫 미확정 bar 에서 실행 실패(`/api/simulate` 400, 메시지에 최초 10건). 다른 철자는 기동 거부. 원장 블록 안의 키이므로 `path` 가 필요하다 |

행의 leg 는 `legType` 이 정한다: `cash` 행은 금액(`cashPerShareCache`, NaN = 미해결), `stock`/`rights` 행은 자회사(`childTradingItemId`, null = 미해결), `quantity` 행(분할·주식배당·개명 수량)은 split_adj 패널이 이미 반영하므로 집계만 한다. 같은 날 현금+주식은 두 행이다(§3-1): `cash` 행이 자회사를 이름하거나 `stock` 행이 금액을 실으면 **거부**(키를 만들 수 없고 한 leg 짜리 행과 이중 기장될 수 있음). `payDate` 는 읽지 않는다(지급일 금융은 §6 한계).

## 2. 로더 (`parquet_reader.cpp::read_corporate_actions`)

1. 모든 행에 대해: `eventType` 이 비었거나 `∈ {merger_cash, merger_stock, rename, relist}`(events 파일 담당)이면 **거부**; `parentFactorCache` 가 유한하고 1.0(±1e-9)이 아니면 거부(패널이 split_adj 가 아니거나 원장이 다른 basis); exDate null 거부; `legType ∉ {cash, stock, rights, quantity}` 거부; 두 leg 를 한 행에 실은 행 거부(§1); 음수 ratio 거부; `(부모, exDate, legType, child)` 키 중복 거부(원장 자체 키 §3-1, 같은 leg 를 두 번 기장하지 않음, P6).
2. 부모가 패널 심볼이 아닌 행은 무시(`rowsMatched` 제외). `status == unresolved` 행은 `rowsUnresolved`, `quantity` 행은 `rowsQuantity` 로 집계하고 leg 를 만들지 않는다(quantity). 금액이 NaN 인 `cash` 행과 자회사가 null 인 `stock`/`rights` 행(export 의 **표시용 행**, §9-1; `markerReason` 컬럼은 읽지 않는다)은 **미해결 leg** 로 스케줄하고 `legsUnbookable` 로 집계한다(보이게 하되 기장하지 않음, §3-1 "unresolved 는 표시가 목표", P12 "자료 없음 ≠ 대가 0"). 표시용 행은 값이 없으므로 추정(`estimate`)으로 표시하지 않는다.
3. exDate 는 패널 bar 의 달력일과 일치해야 한다. 패널 범위 밖이면 `legsOutOfRange`, 범위 안인데 bar 가 아니면 기동 실패(원장이 휴장일을 다음 거래일로 옮겼다는 계약이므로 조용히 이동하지 않음).
4. 자회사 id 가 패널에 없으면 `child = -1`(`unpricedChildren`, stderr 한 줄), 자기 자신이면 거부. `rights` 행은 주식 leg 경로를 타고 `legsRights` 로 집계된다. `expiryDate` 컬럼이 있으면(`rightsTermsLoaded`) rights leg 의 `expiry_bar` = 만료일 **다음** 첫 bar(패널 뒤면 T = 발동 없음), 만료일이 배당락일 이전이면 거부, null 이면 `rightsWithoutExpiry`; 행사가·행사비율 컬럼은 읽어 두기만 한다.
5. 추정 표시: `estimate = isEstimate ∨ status == estimated ∨ (stock 행이고 childValueSource ∈ {event_value, vendor_implied})`(P10 ③④), `legsEstimated` 집계(rc2f Y1 부터 **현금 행**도 포함: vendor_implied 현금배당 등 `isEstimate`/`status estimated` 인 현금 leg 는 금액대로 기장하되 `dividendsEstimated`·§9 `estimate_value` 로 표시). 선택 컬럼 `estimateReason`(§9-8) 은 flagged 행에서만 `leg.estimate_reason` 으로 읽는다. `childValueSource ∈ {wi_close, event_value, vendor_implied}` 인 행만 패널 시세 없는 자회사를 원장 값으로 평가할 수 있다(§3 `ledgerValue`).
6. Staleness(기본 on): (a) 배당락일 부모 종가는 **앵커 유한 ⇔ 패널 관측** 이어야 하고 둘 다 있으면 상대 1e-4 이내. (b) `parentCloseBeforeCache` 는 엔진이 실제로 나누는 마크 = 배당락일 **직전 관측** bar(패널 안에 있으면)의 종가와 대조하며, 금액이나 ratio 가 있는 leg 에 이 앵커가 없으면 "evaluation inputs without price anchors" 로 거부(events 가드와 같은 강도; 직전 관측 bar 가 패널 밖이면 검사 불가로 통과). (c) 자회사 종가: 패널이 그 날 자회사를 관측하고 `childValueSource` 가 원장 평가 tier 가 아니면 대조; 패널에 시세가 없는데 `child_close_ex`/`unknown` 값이 실려 있으면 **다른 캐시의 종가** 이므로 거부; wi_close/③④ 값은 대조하지 않는다(패널 시세가 있으면 시세가 이긴다). 불일치는 `"Corporate actions ledger is stale against the price cache: <id> <date> ... - re-export the ledger for this cache"` 로 기동 거부.
7. events 정산 창(`settlement_bar` = 종료일 다음 bar 이후)에 놓인 leg 는 `legsAfterParentEnd` 로 집계한다. 정산은 첫 무시세 bar 에 실제로 일어나므로 그 사이 부모가 시세를 찍고 보유돼 있으면 정상 기장되고, 정산 bar 당일의 leg 는 정산 전 노출로 기장되며(§3 정산 bar 규약), 부모가 retire 된 **뒤** bar 의 leg 만 기장되지 않는다(§6).
8. leg 는 (bar, parent) 로 안정 정렬해 `DataPanel::corporate_actions` 에 두고, 집계는 `corporate_action_{rows, rows_matched, rows_quantity, rows_unresolved, legs, legs_estimated, legs_unbookable, legs_rights, legs_after_parent_end, out_of_range, unpriced_children, price_checks, price_checks_not_evaluable}` 로 `/api/config.corporateActions` 에 나간다(`priceChecks + priceChecksNotEvaluable == legs`). 원장 컬럼 오류 메시지는 "Corporate actions ledger ..." 로 시작한다(events 파일 오류와 구분).

## 3. 회계 (`simulator.cpp::book_corporate_actions`)

bar 순서: `settle → normalize_intent → require_* → **book_corporate_actions** → value_holdings → apply_successor_transfers → execute → **apply_received_children** → 통계`. 부모의 전일 장부 가중치 `w = carried_[parent]`(정산 후·체결 전), 전일 마크 `m = marks_[parent]`(그 bar 평가 전) 를 쓴다. 미보유(`w = 0`)·retired·warmup 에서는 아무것도 기장하지 않는다.

| leg | 식 | 진단 |
|---|---|---|
| 현금 | `pnl += w × cashCache / m` (부호 = w, 세전 100%, 수량 불변 P3). 금액 NaN 이면 기장 없음. `legs_unresolved` 는 보유 부모의 leg 중 원장이 unresolved 로 표시했거나 금액·자회사가 없는 것의 수(표시일 뿐 금액이 있으면 기장은 된다) | `dividend_count`, `dividend_pnl`, `legs_unresolved` |
| 주식 | `w_child = w × childRatioCache × child_close / m`(하나의 헬퍼 식, 배당락일·첫 시세 경로 동일), `pnl += w_child`(받은 가치), 자회사 마크 = `child_close` | `distribution_count`, `distribution_pnl`, `child_positions_created`, `distribution_estimated`, `legs_unresolved` |

부모는 평소대로 `w × (close_ex/m − 1)` 을 같은 bar 에 얻으므로 bar 합계는 `w × [(close_ex + cash + ratio × child_close)/m − 1]`, 즉 P2 의 현금식 수익률이다. 소수 가중치·음수(숏) 그대로 통과한다. 부모가 배당락일에 무시세(동결)면 현금은 마지막 관측 마크로 그날 기장되고 가격 하락은 시세 재개 bar 에 인식된다(합은 현금식과 같음).

`child_close` 의 출처(`DistributionRecord.source`):

| source | 조건 | 처리 |
|---|---|---|
| `childClose` | 자회사가 배당락일 bar 에 관측 종가 | 패널 종가로 생성(값은 시세). 원장이 추정 표시한 leg(비율·날짜 추정)면 레코드 `estimate = true`·`distribution_estimated`·§9 `estimate_value(child_close:<reason>)` (§9-8, 2026-09-21 이전에는 false) |
| `ledgerValue` | 시세 없음 + 자회사가 한 번도 시세를 찍은 적 없음 + 원장 `childCloseAtExCache` 유한 + `childValueSource ∈ {wi_close(P10 ②), event_value(③), vendor_implied(④)}` + carry 정책 | 원장 값을 첫 마크로 생성. ③④(또는 `isEstimate`/`status=estimated`)이면 레코드 `estimate = true`, `distribution_estimated` 집계. 첫 실제 시세에서 `w_child × (close/원장값 − 1)` 인식. `stale_quote_settle_bars` 의 무시세 계수는 생성 bar **다음** bar 부터 센다. Error 정책은 무시세 보유를 허용하지 않으므로 이 경로를 쓰지 않고 대기한다 |
| `firstQuote` | 위 둘 다 아님 + `unpriced_child: pending` | 부모 `w, m` 을 배당락일 값으로 고정해 대기(`distribution_pending` = bar 말 대기 leg 수). 자회사 첫 관측 종가 bar 에 그 종가로 생성·손익 인식. 미래 가격을 배당락일로 가져오지 않는다(P10). 대기 중 자회사가 retired 되면 unpriced. 부모가 그 사이 정산·매도돼도 배당락일 권리는 남는다 |
| `unpriced` | 자회사가 패널에 없음 / null(미해결) / ratio NaN / 이미 retired / `drop` 정책 | `distribution_unpriced`, 손익 미인식(R3 aux 로 줄임) |
| `ledgerValueCash` | 자회사가 패널에 없고(`child = -1`: 미확인 또는 패널 밖 id) 원장이 `childValuePerShareCache`(부모 1주당 가치, ②③④ tier) 를 실음 | 가치를 현금처럼 `w × value / m` 으로 손익 인식, 포지션 없음(§9-6). ③④·isEstimate 면 `estimate`·`distribution_estimated`·§9 `estimate_value`; `distribution_value_only` 집계 |
| `expired` | rights leg 가 pending 인 채 `expiry_bar` 에 도달(만료 bar 의 첫 시세도 소멸; 만료 검사가 시세 검사보다 앞) | 가치 0 으로 닫음, `rights_expired`(unpriced 와 달리 배당락일이 아니라 만료 다음 bar 에 닫히고 그때까지 `distribution_pending` 에 있음). 이미 생성된 권리가 만료 뒤 무시세로 보유 중이면 정산 경로로 R = −1(explicit, fee 없음), `rights_expired_held`/`rights_expired_gross`, `missingData` 의 delisting 에도 집계. 만료 뒤에도 시세를 찍는 권리는 events 의 종료일 뒤 시세처럼 침묵 bar 까지 평가·거래. **행정 정산 우선순위: events 정산 > 권리 만료 > stale-quote** — 만료가 알려진 권리 증권은 stale 규칙에서 제외되므로 만료 전 무시세는 동결만 되고(default return 정산 없음) 만료 bar 이후에 −1 로 닫힌다. 한 증권을 여러 rights leg 가 교부하면 증권의 만료 = 그중 **가장 이른** 만료 |

같은 날 규약: 한 bar 안에서 현금 leg 를 먼저, 주식 leg 를 나중에 기장한다. 자회사가 받는 그 bar 에 자기 현금 leg 가 있으면 **bar 에 들고 들어온 자회사 가중치만** 현금을 받고 그날 받은 주식은 받지 않는다(받은 주식은 체결 뒤에 장부에 얹히므로; 이중계산 없음).

**정산 bar 의 원장 leg (split-path 딜, V1 P0 반영)**: 부모가 events 로 실제 정산되는 bar(`settles_now_`)에 놓인 원장 leg 는 **정산 전 노출·마크**(`previous_weights_`, 정산 수익률이 곱해지기 전 마크 `pre_settlement_mark_`)에 기장한다. events 행이 현금·승계 leg 를, 원장이 추가 자회사 leg 를 구 id 로 같은 bar 에 실으면 bar 합계는 `w × [(cash_events + ratio_succ × succ + Σ ratio_child × child)/mark − 1]` 이 된다(SLM 2014-05-01: (17.00 + 9.02)/25.75 − 1 = +1.05%). 이중계산 방지 규칙(정확히, 보수적 P6): (a) events 행에 현금 leg 가 있으면(`hasCashLeg`) 같은 bar 의 원장 **현금** leg 는 **금액과 무관하게** count-only(`legs_deferred_to_events`) — 현금은 events 가 1회 실현하며, 원장의 금액이 events 와 다르더라도 원장 쪽은 버려진다; events 행에 현금 leg 가 없으면 원장 현금 leg 를 정산 전 마크로 기장한다. (b) 원장 주식 leg 가 events 의 승계사와 **같은 증권**을 이름하면 **비율과 무관하게** count-only(승계 이전이 1회 교부; 원장의 비율이 events 와 달라도 원장 쪽은 버려진다). 따라서 **로더 export(R1 규칙)는 정산 bar 에 events 의 현금 leg 와 겹치는 현금 행이나 승계사를 이름하는 주식 행을 내보내면 안 된다** — 그런 대가는 events 딜 조건(`cashPerShareCache`, `successorRatioCache`)에 넣는다; 엔진은 그런 행을 `legsDeferredToEvents` 로 보이게만 한다. (c) 부모가 **이전 bar** 에 retire 된 뒤의 leg 는 노출 0 이라 기장되지 않는다(로더 `legsAfterParentEnd` 로만 보임). 정산 bar 의 leg 가 pending 으로 넘어가면 정산 전 `w, m` 을 고정해 뒤에 생성되며 그 레코드의 `on_settlement_bar` 도 true 다. 진단: bar `legs_on_settlement_bar`(정산 bar 에 놓여 정산 전 노출로 처리한 leg 수, count-only 포함)·`legs_deferred_to_events`, 레코드 `on_settlement_bar`, JSON `legsOnSettlementBar`/`legsDeferredToEvents`/`distributionLog.onSettlementBar`. 부모가 정산되지 않는 bar 의 leg 는 이전과 동일.

**청산 시점 1 bar 차이(의도된 규칙)**: events 의 승계 이전분은 **정산 bar 당일** 실행(체결·fee)에서 전략 목표로 재조정되고(승계사는 그 bar 에 시세가 있어 즉시 거래 가능), 원장이 만든 스핀 자회사(정산 bar 의 것 포함)는 체결 뒤에 장부에 얹혀 **다음 bar** 부터 거래된다(교부 주식은 그날 체결 불가라는 P8 규칙). 두 경로를 통일하지 않는다.
**받은 가중치의 체결 시점**: `received_[child]` 는 그 bar 의 `execute()` 뒤에 `weights_` 에 더해진다(시장 체결·turnover·수수료 없음). 다음 bar 부터 자회사는 보통 자산이다 — 전략 목표가 있으면 그 목표로, 신호 NaN 이면 목표 0 → 다음 실행 bar 에 종가 체결 + 기존 `fee_rate`(P8). 기존 자회사 보유와는 부호 상계된다(롱 부모 + 숏 자회사: 전략의 −0.5 체결 뒤 +0.05 가 얹혀 −0.45, 다음 bar 에 −0.05 매도). 자회사 시세가 다음 bar 에 없으면 기존 carry 정책(동결, 청산 가정 없음, 동결 gross 만큼 다른 목표 축소).

D0/D1 은 `carried_` 를 공유하므로 두 실행 경로에서 같은 식이다. 경로마다 테스트가 `for mode in {0,1}` 로 돈다.

## 4. 진단·출력

- bar 별 `SimulatorOutput::corporate_actions[T]` = `{dividend_count, dividend_pnl, distribution_count, distribution_pnl, child_positions_created, distribution_estimated, distribution_pending(상태), distribution_unpriced, legs_unresolved, rights_expired, rights_expired_held, rights_expired_gross, legs_on_settlement_bar, legs_deferred_to_events, dividend_estimated, unconfirmed_count, unconfirmed_abs_weight}`; `AccountingRowView::corporate_actions` 로 probe 가능. `SimulatorOutput::unconfirmed` 는 §9 의 레코드 목록(항상 수집).
- 레코드(opt-in `SimulatorConfig::collect_distributions`, HTTP `distributionLog: true`): `DistributionRecord{bar, ex_bar, parent, child(-1 = 패널 밖·미해결), parent_weight, child_weight, cash_share(같은 bar 같은 부모의 현금/m 합), source, estimate(기장한 값이 원장 추정치), on_settlement_bar(부모 정산 bar 에 정산 전 노출로 기장), value_source(원장 childValueSource 원문), event_type(원장 eventType 원문)}`. 주식 leg 만 기록한다(현금 leg 는 진단 합계로 충분).
- `/api/simulate`: `corporateActions` = `{dividends, dividendPnl, distributions, distributionPnl, childPositionsCreated, distributionsEstimated, distributionsUnpriced, legsUnresolved, distributionPendingBars(leg·bar 가중 합), terminalDistributionPending(마지막 bar), rightsExpired, rightsExpiredHeld, rightsExpiredGross, legsOnSettlementBar, legsDeferredToEvents, dividendsEstimated, unconfirmed(§9), pnlUnconfirmed(§9)}` 를 `missingData` 옆에 항상, 최상위·batch·combined 에 같은 규칙으로 복사. `timeseries.dividendPnl`·`timeseries.distributionPnl`·`timeseries.unconfirmedWeight` 는 `frozenGross` 처럼 거래 구간 bar 별 배열. `distributionLog` 는 요청 시만(`parent, time, bar, exDate, child|null, parentWeight, childWeight, cashShare, source(childClose|ledgerValue|firstQuote|unpriced|expired), estimate, onSettlementBar, valueSource, eventType`). 기존 `missingData` 20 키는 그대로.
- `/api/config.corporateActions`: `{loaded, rows, rowsMatched, rowsQuantity, rowsUnresolved, legs, legsEstimated, legsUnbookable, legsRights, rightsTermsLoaded, rightsWithoutExpiry, autoExercise(항상 false), legsAfterParentEnd, legsOutOfRange, unpricedChildren, priceChecks, priceChecksNotEvaluable, applyDividends, applyDistributions, unpricedChild, staleCheck, unresolvedPolicy}`. 기동 로그에 같은 집계 한 줄.

## 5. 오라클 재현 (`tests/test_corporate_actions.cpp`)

| 사건 | fixture(캐시 기준) | 엔진 배당락일 손익 | 근거 |
|---|---|---|---|
| DD 2025-11-03 롱 | prev 81.65 → ex 34.69, Q 97.00, ratio 0.5, w = 1 | (34.69 + 0.5×97.00)/81.65 − 1 = +0.018860956 (**+1.89%**), 자회사 가중치 0.593999 | GS 일치값 §2 |
| HON 2026-06-29 숏 | raw 232.21 → 227.80, HONA 220.19, raw 1주 → 0.5 HON + 0.5 HONA. split_adj 패널은 반감을 이력에 재기술하므로 prev = 2×232.21 = 464.42, ratioCache = 1.0, parentFactorCache = 1 | 부모 노출 −0.204 에 +0.007217005 = 노출의 **+3.538%**, 자회사 가중치 −0.096720(−102 HONA) | GS +3.5%. raw 식 (0.5×227.80 + 0.5×220.19)/232.21 − 1 과 1e-9 일치 |

두 값 모두 float32 종가(패널 저장형)로 계산한 기대값과 1e-9 이내다.

## 6. 한계 (문서화된 미구현)

- 현금 잔고·지급일 금융 없음: fixed book 은 현금 계정이 없으므로 배당은 배당락일에 손익으로만 잡히고 `payDate` 는 읽지 않는다.
- 권리·워런트 **행사** 없음: 만료(가치 0, `rights_expired`)는 구현됐고 `auto_exercise` 는 설계만(`docs/rights_warrants_design_2026-09-17.md` §4; true 면 기동 거부). export 에 `expiryDate` 컬럼이 없으면 만료도 없어 시세 없는 권리는 pending 으로 남는다(`rightsTermsLoaded=false`).
- 차입·스프레드 비용 없음(E2 `backtest.costs`), 세전·계약 중립(P1).
- `ledgerValue` 는 자회사가 패널에서 한 번도 시세를 찍지 않은 경우에만 쓴다. 이미 마크가 있는 자회사가 배당락일에만 결측이면 pending 으로 대기한다(마크 충돌 방지). `missing_close_policy: error` 에서는 쓰지 않는다(무시세 보유 금지) — 그 정책에서 W/I 자회사 가치는 첫 시세 bar 에 인식된다.
- 추정 표시는 원장의 `isEstimate`·`status`·`childValueSource` 를 그대로 믿는다. R1 export 는 현재 wi_close fallback 에도 `isEstimate=True` 를 세우므로(리뷰 §5) 그 행은 R1 이 고칠 때까지 추정으로 표시된다.
- 부모가 retire 된 뒤 bar 의 leg 는 기장되지 않는다(정산 bar 당일의 leg 는 §3 규약대로 정산 전 노출로 기장). `legsAfterParentEnd` 는 정산 창에 걸린 leg 수이지 소실된 leg 수가 아니다.
- 대기(pending) leg 가 끝까지 시세를 못 받으면(만료일이 없거나 패널 뒤) `terminalDistributionPending` 으로만 보고된다.

## 7. 테스트 (`ctest`: `test_corporate_actions` 46 → 56(Y1), `test_config_loader` +2, `test_http_contract` +2 → +3(Y1))

| # | 테스트 | 내용 |
|---|---|---|
| 1 | LongAndShortCashDividendsBookAgainstThePreviousMark | 롱/숏 × D0/D1, 100→95 + 현금 5 = 0, 수량·turnover·fee 불변 |
| 2 | SpinOffLongMatchesTheDdOracle | §5 DD, 자회사 마크·다음 bar 청산 fee, 레코드 |
| 3 | SpinOffShortMatchesTheHonOracle | §5 HON, 숏 자회사 −102, 다음 bar 환매 fee |
| 4 | ReceivedSharesNetAgainstAnExistingChildWeight | 롱 부모 + 숏 자회사 상계 |
| 5 | FractionalRatioAndSameDayCashPlusStockAddUpToTheCashStyleReturn | ratio 0.3 + 현금 4, 파일 순서 무관, 레코드 cash_share |
| 6 | ChildTargetsAndLiquidationFollowTheNormalExecutionRules | 목표 0 청산 fee / 전략 목표 유지 / 시세 결측 동결·예산 축소 |
| 7 | PendingChildIsCreatedAtItsFirstQuoteWithTheExDateEntitlement | 익일 첫 시세, 부모를 먼저 팔아도 배당락일 권리 |
| 8 | PendingLegsThatNeverQuoteStayPendingOrDropByPolicy | 끝까지 대기·drop·대기 중 retired |
| 9 | UnpricedChildrenAreReportedAndRecogniseNoValue | 패널 밖 / ratio NaN |
| 10 | LedgerValuePricesAChildThatHasNeverQuoted | when-issued 40 → 첫 시세 44 |
| 11 | LegsOnUnheldWarmupOrRetiredParentsBookNothing | warmup·미보유·같은 bar 정산 |
| 12 | ApplyFlagsSwitchEachLegOffAndLeaveTheRestBitForBit | 스위치별 차이 = 해당 leg |
| 13 | InputsWithoutALedgerReproduceThePreChangeDumpBitForBit | `tests/golden/missing_close_bitwise.json`(HEAD 2e77af1) 6 케이스 × {원장 없음, leg 있으나 off} 비트 동일 |
| 14 | RejectsLegsThatDoNotReferenceThePanelInOrder | 시뮬레이터 생성자 검증 |
| 15 | TimeseriesCarryTheDividendAndDistributionSeries | 거래 구간 오프셋·JSON 키 |
| 16 | Loader.RowsScheduleLegsOnTheirExDateBarsAndResolveChildren | 매치·범위 밖·split 행·자회사 해석·집계 |
| 17 | Loader.IntegerIdsAndWhenIssuedValuesAreAccepted | int64 id, 원장 자회사 값 |
| 18 | Loader.StaleAnchorsRefuseTheStart | 부모/직전/자회사 앵커 불일치, 앵커 유무 불일치, 허용오차, stale_check off |
| 19 | Loader.RefusesTheWrongBasisScopeCalendarAndFiles | factor≠1·null, merger/rename, 자기 자식, 음수, 키 중복, bar 아님, 파일·컬럼 없음 |
| 20 | Loader.LoadedLegsRunThroughTheSimulatorEndToEnd | parquet → DataPanel → 시뮬레이터 D0/D1 |
| 21 | EstimatedLedgerValuesAreBookedWithTheFlagAndCacheClosesAreNot | vendor_implied/event_value → `estimate`, wi_close → 가격, child_close_ex 무시세 → pending; 시세 있는 추정 leg 는 시세 |
| 22 | UnresolvedLegsAreCountedAndNeverBooked | 금액 NaN·자회사 null → `legs_unresolved`, 기장 0 |
| 23 | ErrorPolicyWaitsForTheFirstQuoteInsteadOfHoldingALedgerValue | Error 정책 무예외, 첫 시세 생성; carry 는 원장 값 생성·동결 |
| 24 | LedgerValueChildStartsTheStaleQuoteClockAfterItsCreation | `stale_quote_settle_bars=2` 가 생성 다음 두 bar 뒤에 발동 |
| 25 | SeveralParentsAndTheChildsOwnCashOnTheSameBar | 롱·숏 두 부모 → 한 자회사 상계, 자회사 자기 현금은 사전 보유분만 |
| 26 | ExDateChildTargetsAndParentSettlementsKeepTheEntitlement | 배당락일 자회사 목표 + received, 부모 정산 뒤 pending 생존 |
| 27 | AParentWithoutAQuoteOnTheExDateStillPaysAgainstItsLastMark | 부모 동결(1·2 bar) 배당락, 합 = 현금식 |
| 28 | Loader.LegTypesKeyTheRowsAndMixedLegsAreRefused | K1: 두 leg 한 행 거부, legType 키 중복 거부, cash+stock+quantity 4행, rights 집계 |
| 29 | Loader.UnresolvedRowsAreScheduledAndCounted | K2: `rowsUnresolved`, 미해결 leg 스케줄·집계, 보유 부모에서 `legs_unresolved` |
| 30 | Loader.EstimateColumnsAreReadAndCacheClosesMustExistInThePanel | `isEstimate`/`status`/`childValueSource` 해석, `child_close_ex` 무시세 거부, 시세 있는 추정치 미대조 |
| 31 | Loader.LegsAfterTheParentsEndDateAreCounted | events 정산 bar 이후 leg 집계 |
| 32 | QuotedRightsFollowTheStockLegPath | 시세 있는 권리 = 주식 leg(회귀) |
| 33 | PendingRightsExpireAtZeroOnTheBarAfterExpiry | 만료 다음 bar `rights_expired`, 레코드 `expired`, unpriced 와 bar·source 대비 |
| 34 | AFirstQuoteBeforeExpiryCreatesTheRight | 만료 직전 첫 시세 → firstQuote, 만료 미발동 |
| 35 | HeldRightsWithoutAQuoteAfterExpirySettleAtMinusOne | 동결 보유 → R=−1 정산, fee 0, `rights_expired_held` |
| 36 | RightsWithoutOrBeyondExpiryStayPendingAndOnlyRightsExpire | 만료 null/패널 뒤/spin_off 는 E1 과 비트 동일 |
| 37 | RejectsExpiryBarsBeforeTheExDate | 생성자 검증 |
| 39 | ExpiryOutranksTheStaleQuoteRuleForHeldRights | 리뷰 프로브 D-1: `stale_quote_settle_bars=2` 에도 만료 전 동결·만료 bar −1, 런 gross 0; 만료 없는 이름은 stale 그대로 |
| 40 | QuotesAfterExpiryAreValuedUntilSilentAndAFirstQuoteOnTheExpiryBarLapses | 만료 뒤 시세 평가 → 첫 침묵 bar −1; 만료 bar 첫 시세는 소멸 |
| 41 | SeveralLegsIntoOneRightUseTheMinimumExpiryWhenHeldAndTheirOwnWhenPending | 다중 leg 최소 만료(보유) / leg 별 만료(pending) |
| 42 | SettlementBarLegsBookAgainstThePreSettlementExposureWithEventsCash | V1 C2: events 현금 14.5 + 원장 자회사 0.5×40, mark 100 → −0.655(롱/숏), 자회사 익일 매각 |
| 43 | SettlementBarLegsBookBesideASuccessorTransfer | V1 C2 DISC.A 형: 승계 0.5 S + 자회사 C·D → −0.11117, 승계분 당일·자회사 익일 청산(1 bar 차이) |
| 44 | SlmSplitPathBooksTheContractReturn | V1 C3 SLM 2014-05-01: (17.00 + 9.02)/25.75 − 1 = +1.05%(롱/숏) |
| 45 | SettlementBarLegsNeverPayWhatTheEventsRowAlreadyPays | events 현금 + 원장 현금 → 1회, 승계사 지명 주식 leg count-only, 승계 전용 events + 원장 현금 → 기장 |
| 46 | PendingSettlementBarLegsKeepTheirExposureAndFlag | 정산 bar leg 가 pending → 첫 시세에 정산 전 w·m 로 생성, 레코드 `on_settlement_bar` 유지 |
| 38 | Loader.RightsTermsAreOptionalColumns | 컬럼 없음 → 기능 off·집계, 있음 → expiry_bar(패널 안·뒤·null), 만료<배당락 거부 |
| 47 | MarkerLegsOnHeldParentsAreListedAsUnconfirmedAndUnheldOnesAreNot | §9: 표시용 현금·자회사 행 보유 → `unresolved_leg(no_cash_amount/no_child)`, 부호 유지 가중치, 금액 있는 unresolved 행은 기장+`status_unresolved`; 표시용 행 없는 실행과 비트 동일; 미보유 0 |
| 48 | StrictPolicyFailsOnTheFirstUnconfirmedBarAndListsItsEvents | strict 첫 미확정 bar 에서 runtime_error(날짜·심볼·kind(detail)·weightBefore, `first N listed`), warn 계속, 미확정 없으면 strict == warn 비트 동일, 미보유는 strict 통과 |
| 49 | DefaultSettlementsEstimatesAndDroppedChildrenAreListedByKind | `default_settlement(no_deal_terms/deal_terms_not_priced/stale_quote)` vs explicit·computed 정산 미기록; `estimate_value(cash_estimate/ledger_value:vendor_implied)`, wi_close 는 미기록; `pending_dropped(child_not_in_panel/ratio_unknown/policy_drop/child_retired_while_pending/pending_at_end)`; leg 당 레코드 1개 |
| 50 | TimeseriesCarryTheUnconfirmedWeightSeries | `compute_timeseries` 13번째 시리즈 → JSON `unconfirmedWeight`(거래 구간 오프셋, 없으면 빈 배열) |
| 51 | Loader.MarkerRowsLoadAsUnbookableLegsAndAreListedOnHeldParents | 표시용 행(`markerReason` 컬럼 포함) parquet → `legsUnbookable` 2, 추정 아님, 앵커 검사 통과·불일치 거부, 보유 시 3 레코드·strict 거부 메시지 |
| 52 | LedgerValuesForChildrenOutsideThePanelAreBookedAsCashWithoutAPosition | §9-6: (97+3)/100−1 = 0, `distribution_value_only`·estimated, 포지션·회전·fee 없음, 레코드 `LedgerValueCash`, 숏은 지급, wi_close 는 미확정 아님, child_close_ex 는 unpriced, 마커·off·패널 안 자회사 불변 |
| 53 | Loader.ChildValuesPriceLegsWhoseChildIsOutsideThePanel | 선택 컬럼 `childValuePerShareCache`: 자회사 없음/패널 밖 값 leg → `legsValueOnly` 2, 앵커 필수, 현금 행의 값 거부, 컬럼 없으면 종전(마커·unpriced) |
| 54 | MarkerCashLegsDeferredToTheEventsRowAreNotUnconfirmed | R-Y1 P1-1: 정산 bar 의 표시용 현금 leg 가 events 현금 leg 옆이면 count-only·미확정 아님·strict 통과, 승계 전용 events 면 미확정·strict 실패; 한 부모의 두 leg 는 `unconfirmed_abs_weight` 를 1회 |
| 55 | QuotePricedLegsFlaggedByTheLedgerStayUnconfirmedWithTheirReason | §9-8: 시세로 평가한 flagged leg — 회계는 unflagged 와 비트 동일, 레코드 `estimate`·`distribution_estimated`, `estimate_value(child_close:ratio)`·strict 거부; pending 은 `first_quote:<reason>`; 현금 `cash:date`; unflagged 는 없음 |
| 56 | Loader.EstimateReasonsAreReadIntoTheUnconfirmedDetail | 선택 컬럼 `estimateReason` → `leg.estimate_reason`·detail; 컬럼 없으면 `value`(③④)/`unspecified` 대체 |
| — | `tools/data/cache_v21/tests/test_loader.py` | `test_export_ca_ledger_units`(표시용 행·키 충돌·값 leg·`childValuePerShareCache`), `test_export_events_split_adj`(§9-5) |
| — | ConfigLoader.CorporateActions* | `auto_exercise: true` 거부, 세 컬럼 키 |
| — | HttpContract (기존 2 개 확장) | `rightsExpired`·`source expired`·`rightsTermsLoaded`·`autoExercise` |
| — | ConfigLoader.CorporateActions* | 기본값·전체 키(14 컬럼)·거부 사례 |
| — | HttpContract.ConfigPublishesTheCorporateActionsLedgerReport | `/api/config.corporateActions` 키·분할 항등 |
| — | HttpContract.SimulateReportsCorporateActionTotalsSeriesAndOptInLog | `corporateActions` 19 키, 시리즈 합 = 합계, opt-in `distributionLog` 항목·합계 대조(`ledgerValueCash` 포함), combined/batch 복사 |
| — | HttpContract.SimulateReportsUnconfirmedPnlAndTheStrictPolicyReturns400 | §9: `unconfirmed{count,sumAbsWeight,byKind,items}`·`pnlUnconfirmed`, fixture 표시용 행 A/C 와 B→E 항목, `timeseries.unconfirmedWeight` 합 = `sumAbsWeight`, combined/batch; 같은 fixture 를 `unresolved_policy: strict` 로 띄운 두 번째 서버(포트+1)가 400 + 메시지 |
| — | ConfigLoader.CorporateActions* | `unresolved_policy` 기본 warn·strict 읽기·다른 철자 거부 |

HTTP: `tools/make_http_test_fixture.py` 가 `engine/ca_ledger_cache.parquet`(A 현금 0.5 @2022-06-15, C→D 0.1 @2023-03-10, B→E(패널 밖) 0.2 @2024-02-20, D→W 워런트 0.1 @2024-09-10 만료 2024-10-15 — W 는 전 구간 NaN 인 5번째 심볼, 표시용 행 A 현금 NaN @2023-09-12 `no_cash_amount`·C 자회사 null @2025-02-14 `no_child`(§9-1), B→F(패널 밖) vendor_implied 값 3.0 @2025-06-12(§9-6), D→A 0.05 @2025-08-15 시세 평가·`estimateReason` ratio(§9-8), §3-3 16 컬럼 + 만료·행사 3 컬럼 + `markerReason`·`childValuePerShareCache`·`estimateReason`)를 만들고 `backtest.corporate_actions.path` 로 연결하므로 `test_http_contract` 25 개가 원장이 켜진 서버로 돈다(기존 계약 무변경). 서버 포트는 `LONGSHORT_TEST_PORT`(기본 18080)로 바꿀 수 있다. `distributionLog` 응답 예: C→D `childWeight 0.0333`(= 0.3×0.1×155.13/139.68, `valueSource child_close_ex`, `estimate false`), B→E `source: unpriced`.

실행: `docs/logic_completion_data_2026-09-11.md` 의 CMake/CTest 명령에 `-DUUID_INCLUDE_DIRS=... -DUUID_LIBRARIES=...`(기존 빌드 캐시와 동일) 를 더해 별도 빌드 디렉토리에서 27 target / gtest 319 케이스 전부 통과, skip 0. rc2f Y1(2026-09-21, `/tmp/longshort_rc2f_y1/build`): 28 target / gtest 359 케이스(HTTP fixture 연결) 전부 통과, skip 0; V1 하니스 `recon_engine_ca.py --http` 2,677 pass / 0 fail(표시용 행 3 + 값 leg 3 + 시세 추정 3 + §E2 실데이터 8행 + §G events export 6 케이스 + §9 대조 포함).


## 8. costs — 차입비 모듈 (rc2 E2, 2026-09-17)

`backtest.costs:` 는 선택형 비용 모듈이다(P1 세전·계약 중립, P9 시장·계약별 설정, GS 값을 기본값으로 굳히지 않음). 기본은 모두 off 이고 off 이면 결과는 **비트 동일**(`SimulatorConfig::borrow == nullptr`, 아무것도 빼지 않음; `test_costs` 1).

```yaml
backtest:
  costs:
    borrow:
      enabled: false        # true 면 숏 장부에 차입비
      default_rate: null    # 연율(소수, 예 0.005). null = 기본 없음
      path: null            # 심볼·구간별 연율 표(.csv 또는 parquet): symbol, from, to, rate
      day_count: 360
      symbol_col: symbol    # 표 컬럼명(기본값)
      from_col: from        # 적용 시작 달력일(포함)
      to_col: to            # 적용 종료 달력일(포함), null/빈 칸 = 무기한
      rate_col: rate        # 연율
    report_unapplied: true  # 미반영 상태를 결과에 표시(P9)
```

### 8-1. 회계

bar `t` 의 차입비 = `Σ_{w<0} |w| × rate_i(t) / day_count × days(t)`; `w` 는 **bar 에 들고 들어온 장부 가중치**(전 bar 종가 체결 결과 `previous_weights_`), `days(t)` = 패널 타임스탬프 기준 달력일 `(ts[t] − ts[t−1]) / 86 400 000`(주말을 건너뛴 월요일 bar 는 3), 첫 bar(`t=0`)는 0. warmup 은 보유가 없어 0 이고, 첫 거래 bar 도 들고 들어온 보유가 없어 0 이다(첫 체결 다음 bar 부터). 비용은 `ret`(net) 에서만 빠지고 gross 는 그대로다: `AccountingRowView.net_pnl = gross − fee − borrow`, `timeseries.cumPnlGross` 는 fee 와 함께 borrow 를 더해 복원한다. 요약 지표(Sharpe 등)는 net 기준.

rate_i(t): 표에서 심볼 i 의 구간 `[from, to]` 가 bar 의 달력일을 포함하면 그 연율, 없으면 `default_rate`, 그것도 없으면 **미반영**(0 부과, `borrowUnratedShortBars` 집계, `borrowApplied=false` 가 아닌 한 반영 상태는 "부분"). 구현 `src/engine/costs.{h,cpp}` `BorrowCostModel`: 표를 기동 시 한 번 읽어 심볼별 bar 구간으로 바꾸고, bar 마다 숏 종목만 이진 탐색한다(비용 O(숏 종목 수 × log 구간 수)). 시뮬레이터와 분리한 이유: 표 적재·검증·요율 조회가 회계와 무관한 순수 데이터 로직이고 단독 테스트가 쉽다. 호출부는 `PortfolioSimulation::advance` 의 fee 차감 직후 한 곳.

### 8-2. 표 검증 (기동 실패)

| 조건 | 처리 |
|---|---|
| `path` 가 있는데 파일 없음(모듈 on) | 거부 |
| 컬럼 없음, 심볼/from/rate null, 비숫자 rate(csv) | 거부 |
| 음수·비유한 rate, `to < from` | 거부 |
| 같은 심볼의 구간 겹침(무기한 뒤 새 구간 포함) | **거부**(패널 밖 심볼도 검사; "later rows win" 대신 명시적 오류) |
| 인접 구간(`to` 다음 날 `from`) | 허용 |
| 패널 밖 심볼 | 무시(`tableRows` 에만 집계) |
| `enabled: false` | 표를 열지 않음 |
| `day_count` 0 | 거부(config 단계에서도 거부) |
| csv 인용부호·셀 안 쉼표 | 미지원(쉼표마다 분리; 심볼·날짜·요율에는 불필요) |

### 8-3. 출력

- `/api/config.costs = {borrow: {enabled, applied, defaultRate|null, dayCount, path|null, tableRows, tableRowsMatched, symbolsRated}, reportUnapplied}`. `applied = enabled ∧ (symbolsRated > 0 ∨ defaultRate)` — `symbolsRated` 는 패널 날짜 안에 구간이 하나라도 있는 심볼 수라, 매치는 됐지만 구간이 패널 밖인 표만 있으면 기본요율 없이는 미반영이다. 기동 로그 한 줄.
- `/api/simulate.costs = {borrowEnabled, borrowApplied, borrowCost(거래 구간 합)}` + `report_unapplied` 이면 `borrowUnratedShortBars`(요율 없이 넘어간 숏 종목·bar 수), 미반영이면 `borrowUnapplied: "disabled" | "no rates: ..."`. 최상위·batch·combined 동일. `timeseries.borrowCost` 는 bar 별 배열(`frozenGross` 처럼 항상 존재).
- `SimulatorOutput::borrow_cost[T]`, `borrow_unrated[T]`, `AccountingRowView::borrow_cost`.

### 8-4. 한계

스프레드·수수료 외 비용 없음, 롱 자금조달·리베이트 없음, 요율은 연율 단순 비례(복리 없음), 표는 심볼×달력 구간만(국가·계약별 기본값은 표로 표현). 스핀 자회사 청산은 기존 `fee_rate`(추가 규칙 없음, §3-4).

### 8-5. 테스트 (`test_costs` 11, `test_config_loader` +1, `test_http_contract` +1)

| # | 테스트 | 내용 |
|---|---|---|
| 1 | DisabledModelAndNoModelAreBitIdenticalToTheFixedBook | off = 모델 없음, gross/net 시리즈 비트 동일 |
| 2 | ShortBookPaysTheDefaultRatePerCalendarDayFromNetOnly | D0/D1, 0.5 숏 × 3.6%/360, gross·fee 불변, net 만 차감, cumPnlGross 복원 |
| 3 | LongOnlyBookBorrowsNothing | 롱 전용 0 |
| 4 | TableIntervalsOverrideTheDefaultAndOtherSymbolsKeepIt | parquet·csv 표, 구간 안 7.2%·무기한 1.8%·기본 3.6%, 기본 없는 미요율 숏 집계 |
| 5 | CalendarDaysBetweenBarsCountTheWeekend | 금→월 3일, day_count 365 |
| 6 | WarmupBarsChargeNothingBeforeTheFirstHolding | sim_start 2: 첫 체결 다음 bar 부터 |
| 7 | EnabledWithoutAnyRateIsUnappliedAndChargesNothing | applied=false, 0 부과, unrated 집계 |
| 8 | TablesAreValidatedBeforeAnythingIsCharged | 겹침(무기한·공유일·패널 밖)·역구간·음수·비숫자·컬럼·파일·인접 허용·off 미개방·day_count 0 |
| 9 | TimeseriesJsonCarriesTheBorrowSeries | 거래 구간 오프셋·gross 복원·JSON 키 |
| 10 | SettledFrozenAndReceivedShortsPayForTheNightsTheyWereHeld | 동결 숏 계속 부과, 정산 bar 까지 부과, 스핀 숏 자회사는 다음 bar 부터 |
| 11 | ParquetTablesAcceptIntegerSymbols | int64 심볼·date32 `to`, 패널 밖 구간만 있으면 `applied=false` |
| — | ConfigLoader.CostsDefaultToEveryModuleOffAndReadEveryKey | 기본값·전체 키·null·거부 |
| — | HttpContract.ConfigAndSimulateReportTheBorrowCostModule | `/api/config.costs`·`simulate.costs`·`timeseries.borrowCost`·combined·전부 숏 알파에서 borrow>0 와 gross−net = fee+borrow |

HTTP fixture(`make_http_test_fixture.py`)는 `costs/borrow_rates.csv`(B 2%@2021–2022, C 1.5%@2023–, 기본 1% — 모두 임의 테스트 값)를 켠다. 계약 알파는 롱 전용이라 숫자는 안 바뀌고 적재·보고만 검사한다. 수동 실측 `<data expr="-close"/>`(전부 숏): 2021-01-04 bar 3.43e-5(≈ Σ|w|·r/360 = 0.0121/360), 2023-01-03 3.14e-5(B 기본·C 1.5% 전환), 거래 구간 합 0.069622, `cumPnlGross − cumPnl = fee 합 + borrow 합` 정확히 일치.


## 9. 손익 미확정(unconfirmed) 감지 — rc2f Y1 (2026-09-21)

미해결 사건이 엔진 입력에서 사라지지 않게 하고, **보유 중인** 포지션의 손익을 입력이 확정하지 못할 때 그것을 bar·실행 단위로 드러낸다. 회계(가중치·손익·유니버스·회전)는 바꾸지 않는다: 원장 없는 입력은 이전과 비트 동일(§7 테스트 13), 표시용 행이 있는 입력은 없는 입력과 비트 동일(테스트 47, V1 `marker_rows_held`, 전체 패널 rc2f Top500 rank 알파에서 R1 export 대비 `cumPnl`·`turnover`·`dividendPnl`·`distributionPnl` 비트 동일).

### 9-1. Export 표시용 행 (`cache_v21/loader.py export_ca_ledger`)

이전 export 는 금액 없는 현금 leg(`cash_leg_without_amount`)와 자회사 미확인 stock/rights leg(`value_leg_without_child`)를 **제거하고 개수만** 남겼다. 이제 두 종류는 **표시용 행**으로 내보낸다.

| 컬럼 | 표시용 행 값 |
|---|---|
| `markerReason` (신규, string, 그 밖의 행은 null) | `no_cash_amount` / `no_child` |
| `status` | `unresolved`(원장 status 가 `estimated` 여도) |
| `isEstimate` | false |
| `cashPerShareCache`, `childRatioCache`, `childCloseAtExCache` | NaN |
| `childTradingItemId` | null |
| `parentCloseAtExCache`, `parentCloseBeforeCache` | 다른 행과 같은 패널 앵커(staleness 대조 대상) |
| `childValueSource`, `childRatioRaw`, `eventId`, `eventType` | 원문 유지 |

다른 제외 규칙(excluded_event_types·stitched_copy·events 중복·종료 부모 현금·패널 밖·직전 종가 없음·stale 앵커)은 표시용 행에도 그대로 적용된다. 표시용 행이 실제 행과 같은 엔진 키 `(tradingItemId, exDate, legType, childTradingItemId)` 를 가지면(같은 날 실제 현금 행 옆의 금액 없는 벤더 마커) 표시용 행만 버리고, 표시용 행끼리 겹치면 하나만 남긴다(`marker_rows.dropped_key_conflict`); 실제 행끼리의 중복은 여전히 ContractError. manifest `marker_rows` = `{exported, by_reason, in_ledger_by_reason, excluded_by_other_rules_by_reason, dropped_key_conflict(_by_reason), ledger_status_by_reason}`; `dropped` 에서 두 옛 키는 빠진다. `ledger_qa` Q11 은 표시용 행을 허용하되 (사유 ↔ 결손) 일치·unresolved·값 NaN·표시 없는 결손 0 을 검사한다(`markerReason` 컬럼이 없는 옛 export 는 옛 규칙).

rc2f 발행 원장(`events/ca_ledger.parquet` sha 8d7b478c…)으로 재생성한 export(sha 7456a0b1…): 453,265 행 = 이전 453,064 행(셀 단위 동일) + 값 leg 104 행(§9-6) + 표시용 97 행(`no_cash_amount` 11, `no_child` 86; 원장에는 24·192 있고 그중 13·1 은 다른 규칙으로 제외, 104 는 값 leg, 키 충돌 0).

### 9-2. 엔진 기록

보유 중 부모(ex bar 직전 가중치 ≠ 0; 정산 bar 는 정산 전 노출)에 대해 `UnconfirmedRecord{bar, asset, weight_before, kind, detail}` 를 기록한다. **leg 하나에 레코드 하나**(먼저 해당하는 kind):

| kind | 조건 | detail |
|---|---|---|
| `unresolved_leg` | `leg.unresolved`(status unresolved 또는 표시용 행)인 원장 leg. 금액이 있으면 기장은 이전대로 된다 | `no_cash_amount`, `no_child`, `ratio_unknown`, `status_unresolved` |
| `default_settlement` | events 정산이 계산도 명시 수익률도 아닌 `default_delisting_return` 으로 이루어짐(`defaultDelistings` 와 같은 사건); stale-quote 정산 포함 | `no_deal_terms`, `deal_terms_not_priced`(선언된 leg 를 이 패널이 가격화 못함), `stale_quote` |
| `estimate_value` | 원장이 추정 표시한 leg 가 기장됨: 추정 표시 현금 leg(§2-5), `ledgerValue`/`ledgerValueCash` 로 기장된 추정값(§9-6), **시세로 평가했지만 비율·날짜가 추정인 leg**(§9-8) | `cash:<reason>`, `ledger_value:<childValueSource>`, `ledger_value_cash:<childValueSource>`, `child_close:<reason>`, `first_quote:<reason>` — `<reason>` = export `estimateReason`(ratio/date/value, '+' 결합), 없으면 ③④ 는 `value`, 그 밖은 `unspecified` |
| `pending_dropped` | 자회사 가치를 인식하지 못하고 포기: 패널 밖·비율 없음·retired·`drop` 정책·대기 중 자회사 retired, 그리고 마지막 bar 에 아직 pending 인 leg | `child_not_in_panel`, `ratio_unknown`, `child_retired`, `policy_drop`, `child_retired_while_pending`, `pending_at_end` |

기록되지 않는 것: 미보유 부모의 leg, warmup, 추정 표시가 없는 leg(시세 평가·wi_close 가격 포함), 계산·명시 수익률 정산, 권리 만료(규칙에 따른 0), events 로 넘긴 count-only leg. `weightBefore` 는 부호를 유지한 장부 가중치다.

bar 진단 `unconfirmed_count`(레코드 수)·`unconfirmed_abs_weight`(그 bar 의 **서로 다른 부모**별 |weightBefore| 합 — 한 부모의 두 leg 는 같은 노출을 두 번 세지 않는다) 는 원장 유무와 무관하게 채워진다(`default_settlement` 는 events 만으로도 생긴다). 정산 bar 에서 events 에 넘긴 count-only leg(`legsDeferredToEvents`)는 events 행이 손익을 확정하므로 표시용 행이어도 기록하지 않는다(현금·주식 루프 같은 순서).

### 9-3. 출력

- `/api/simulate.corporateActions.unconfirmed = {count, sumAbsWeight, byKind: {unresolved_leg, default_settlement, estimate_value, pending_dropped}, items: [{symbol, date, bar, weightBefore, kind, detail}], itemsTruncated, itemsOmitted}`, `corporateActions.pnlUnconfirmed = count > 0`; 최상위·batch·combined 모두. `sumAbsWeight` = Σ_bar `unconfirmed_abs_weight`(bar·부모 단위 1회). `items` 는 bar 순으로 최대 10,000개(`kUnconfirmedItemsCap`)까지 실리고 그 이상은 `itemsTruncated: true`·`itemsOmitted` 로 개수만 남는다(`count`·`byKind` 는 항상 전체). `items[].bar` 는 **패널 bar 인덱스**(warmup 포함 0 기준, `distributionLog.bar` 와 같은 축)이고 `timeseries.*` 배열은 거래 구간 오프셋(`sim_start_index`, D1 은 max(sim_start, 1))에서 시작하므로 `bar − 오프셋` 이 시리즈 인덱스다; `date` 로 맞추는 편이 안전하다.
- `timeseries.unconfirmedWeight` = bar 별 `unconfirmed_abs_weight`(거래 구간 오프셋; 합 = `sumAbsWeight`).
- `/api/config.corporateActions.legsUnbookable`(표시용 leg 수)·`legsValueOnly`(§9-6)·`unresolvedPolicy`; `/api/simulate.corporateActions.distributionsValueOnly`.
- 예(HTTP fixture, `<data expr="close"/><rank/>`): `{"count": 4, "sumAbsWeight": 0.8, "byKind": {"unresolved_leg": 2, "pending_dropped": 1, "estimate_value": 1, "default_settlement": 0}, "items": [{"symbol": "A", "date": "2023-09-12", "bar": 1350, "weightBefore": 0.1, "kind": "unresolved_leg", "detail": "no_cash_amount"}, {"symbol": "B", "date": "2024-02-20", ..., "kind": "pending_dropped", "detail": "child_not_in_panel"}, {"symbol": "C", "date": "2025-02-14", ..., "kind": "unresolved_leg", "detail": "no_child"}, {"symbol": "B", "date": "2025-06-12", ..., "kind": "estimate_value", "detail": "ledger_value_cash:vendor_implied"}], "itemsTruncated": false, "itemsOmitted": 0}`.

### 9-4. `unresolved_policy: strict`

첫 미확정 bar 의 회계가 끝난 뒤(같은 bar 의 사건을 모두 모은 뒤) `runtime_error`: `Corporate actions: unconfirmed P&L on <date> under unresolved_policy strict (<n> event(s) on this bar[, first 10 listed]): <symbol> <kind>(<detail>) weightBefore=<w>; ... - resolve the ledger/events rows or set unresolved_policy: warn`. `/api/simulate` 는 400 `{"error": ...}`; GA 는 후보 실패로 처리. 미확정 사건이 없으면 strict 와 warn 은 비트 동일. `SimulatorConfig::strict_report_limit`(기본 10)가 N.

### 9-5. events export (`cache_v21/loader.py export_events`, rc2f Y1-A)

발행본 `events/corporate_actions.parquet` 의 cache 단위 컬럼은 raw 기준(k≡1)이라 split_adj 엔진 패널에서는 가드가 어긋난다(BRCM→AVGO 앵커 137.68 vs 13.77 등 4건, QRTE.A→QVCA.Q 는 비율 1.0 을 그대로 쓰면 +4,770% 정산). `export_events(out, basis='split_adj', cut='lifecycle')` 는 엔진의 정산 규칙(종료일 다음 첫 bar 이후 대상의 첫 무시세 bar t, mark = t 직전 관측 종가)을 export 패널에서 재현해 앵커를 다시 쓰고 조건을 SPEC_loader §7 대로 환산한다: `cashPerShareCache = cash_raw / sf_T(mark)`, `successorRatioCache = ratio_raw × sf_S(t) / sf_T(mark)`, `targetCacheMark`·`successorCacheClose` = split_adj 종가(mark·t), `cacheUnitFactorTarget/Successor = sf_T/sf_S`, `cacheUnitStatusRaw` = 원본 status, `settlementBarDate` = t. 원본이 NaN 으로 둔 조건(no_terms·rejected:*)은 유지한다. 상태: `ok` / `target_not_in_panel` / `no_mark_in_panel` / `no_settlement_bar_in_panel`(대상이 패널 끝까지 시세, 비율 NaN) / `successor_not_in_panel` / `no_successor_close_at_settlement`(비율 NaN → 엔진은 settlementReturn·기본값). `export_wide` 가 `engine/events_cache.parquet` + `engine/events_cache_manifest.json` 으로 자동 호출하며 엔진 YAML 은 `events: {path: <export>/engine/events_cache.parquet, end_date_col: endDate, ...}`(compare_v2.yaml 과 같은 컬럼 이름).

rc2f 발행본: 14,887 행 전부 패널 안, 6,828 `ok`(현금 4,644·비율 2,728 환산; sf_T≠1 123, sf_S≠1 698), 비율 탈락 73(승계사가 정산 bar 에 무시세). 검증: `ok` 6,828 행 전부 raw 정산식과 split_adj 정산식의 R 이 일치(최대 상대오차 9e-8, float32)하고 앵커는 split_adj 패널과 비트 동일, 발행 `settlementReturn` 과 1e-6 이내(`SP/stage_rc2f/E/Y1/engine/events_cache_identity_check.json`). 엔진 전체 패널(Top500 멤버 1,548 종목, 2000~2026): 가드 584 checked / 0 out of range / 136 not evaluable, 4건 모두 `computed` 로 발행 수익률과 일치(+0.26%·+2.43%·+2.61%·−2.59%), |R|>100% 없음. 테스트: `tests/test_loader.py::test_export_events_split_adj`(합성 105→101 정산 뒤 3:2 분할 → 비율 0.75, 104→102 현금 80+0.8, 항등·상태·manifest), V1 하니스 §G(합성 2 + 실데이터 4 쌍: export 수락·`eventsPriceChecks 1`·정산 bar·R = raw 식 = 발행값, raw 행은 가드 거부).

### 9-6. 패널 밖 자회사의 원장 추정값 기장 (rc2f Y1-B)

권리·자회사가 패널(aux 포함)에 없고 원장에 ③④ 추정값(`childValuePerOldShare`, status estimated, 예 TMUS 2020-06-24·DISH 2019-11-25 rights)이 있는 stock/rights leg 를 엔진이 `unpriced` 0 으로 두면 `returns_total`(flag 3 추정값 기장)과 경로가 어긋난다. 정렬: export 가 모든 값 leg 에 `childValuePerShareCache = childValuePerOldShare / sfParentBefore`(현재 부모 1주당 가치)를 싣고, 자회사 id 가 없어도 ②③④ 값이 있으면 표시용 행이 아니라 값 leg 로 낸다(`no_child` 마커 = 값도 없는 행). 엔진은 `child = -1`(미확인 또는 패널 밖) 이고 `childValueSource ∈ {wi_close, event_value, vendor_implied}` 이며 값이 유한·양이면(export·`ledger_qa` Q11 도 같은 조건 — 유한·양 — 으로 값 leg 를 정한다) `w × value / m` 을 그날 손익으로 인식하고 포지션을 만들지 않는다(`DistributionSource::LedgerValueCash`, 레코드 `child null`, `childWeight` = 인식한 손익, bar `distribution_value_only`, JSON `distributionsValueOnly`, `/api/config.legsValueOnly`). ③④·isEstimate 면 `distributionsEstimated` + §9 `estimate_value(ledger_value_cash:<source>)`; `wi_close` 는 가격이라 미확정이 아니다. 자회사가 패널 안이면 종전대로(시세·ledgerValue·pending). 값 leg 의 직전 종가 앵커는 현금 leg 처럼 필수이고, 현금 행이 값을 실으면 거부. `apply_distributions: false` 면 함께 꺼진다. 컬럼이 없는 옛 export 는 종전대로 `unpriced`.

rc2f 발행 원장 재생성 export: 453,265 행 = R1 453,064 행(셀 단위 동일) + 값 leg 104 행(자회사 id 없음·추정값 있음: rights event_value 27, stock event_value 5·vendor_implied 72; 이전에는 `value_leg_without_child` 로 제거) + 표시용 97 행(`no_cash_amount` 11, `no_child` 86). 값 컬럼은 값 leg 823 행 중 731 행 유한(패널 밖 자회사 118 행 = vendor_implied 78·event_value 40 이 값 leg 경로). 전체 패널 rank 알파 실측(R1 export 대비): 회전·`dividendPnl` 비트 동일, `cumPnlGross` 차이는 52 bar 에서 `distributionPnl` 차이와 2e-16 이내로 같고 그 합 = `ledgerValueCash` 레코드 6건의 합(+0.00278).

### 9-8. 시세로 평가한 추정 leg (사용자 검토 2026-09-21)

원장 `status=estimated, isEstimate=true, childValueSource=child_close_ex` 인 stock leg(발행 원장 22행, export 8행: CP 2597514 2001-10-03 ×3 — 주식수에서 비율 추정, 2605527 2000-07-03 ×2 — 배당락일 추정, 6699559/49334329 2008-03-26, 145334731 2020-12-17)는 자회사 종가로 평가돼도 **교부비율·날짜가 추정**이라 손익이 미확정인데 종전 엔진은 `ledgerValue` 로 평가한 경우만 estimate 로 표시했다. 정렬:

- export 새 컬럼 `estimateReason`(`loader.estimate_reason()`): 추정 표시(isEstimate 또는 status estimated) 행에 `ratio`(표시된 stock/rights 행 중 자회사 가격은 관측이라 비율이 추정, notes 'share-count derived'/'R3_ratio_estimated'/'ratio_assumed'/'assumed', 표시된 quantity 행의 분할 계수) · `date`(notes/repairOf 의 ex-date inferred / ex_date_inferred / exDate_moved / move_date) · `value`(childValueSource ∈ {event_value, vendor_implied}, 현금 행의 amount_* 표시)를 이 순서로 '+' 결합; 표시가 있는데 사유가 없으면 `unspecified`; 표시 없는 행·표시용 행은 null. `isEstimate` 는 그대로. manifest `estimate_reason_by_leg`·`estimate_flagged_without_reason`·`quoted_child_estimates`. rc2f export: stock value 116·ratio 6·ratio+date 2, rights value 30, cash value 16·date+value 1, quantity ratio 981·ratio+date 2, 사유 없는 표시 0, 시세 평가 추정 stock 행 8.
- 엔진: stock/rights leg 를 자회사 종가(`childClose`·`firstQuote`)로 평가하더라도 `leg.estimate`(export isEstimate 또는 status estimated) 면 레코드 `estimate=true`·`distribution_estimated` + 보유 시 `estimate_value(child_close:<estimateReason>)`/`(first_quote:<reason>)` → strict 감지. 현금 leg 는 `cash:<reason>`. 금액·가중치는 불변(시세가 값을 정한다; 테스트 55 에서 unflagged 와 비트 동일).
- 실측(V1 §E2): 규제 패널(부모+자회사, 배당락 ±12·8일)에서 부모를 보유하면 8행 전부 `estimate_value(child_close:ratio|ratio+date)` 로 잡힌다(2597514 는 미해결 leg `ratio_unknown` 도 함께).

### 9-7. 한계

- `unresolved_policy` 는 `backtest.corporate_actions` 블록의 키라 원장 `path` 없이는 설정할 수 없다: events 만 있는 설정(원장 없음)은 `default_settlement` 를 기록·보고만 하고 strict 로 실패시킬 수 없다(필요하면 최상위 키로 옮기는 설계 변경이 든다).
- 표시용 행은 "사건이 있었으나 대가를 모른다"는 표시일 뿐이며 `unconfirmed` 는 그 노출을 세지 손익 크기를 추정하지 않는다.
- `wi_close`(P10 ②) 는 가격으로 보아 미확정에 넣지 않는다 — when-issued 가격이 첫 정규 시세와 크게 어긋나는 사건은 `ledgerValue` 경로의 첫 시세 손익으로만 드러난다(재평가·플래그 없음).
- 자회사가 null 인 값 leg(§9-6)는 events 승계사와 대조할 수 없다(정산 bar 의 승계 이중 교부 검사는 `child >= 0` 인 leg 만; null-child 값 leg 가 승계사와 같은 증권일 가능성은 export 의 `events_cash_duplicate` 규칙만 막는다).
- 스테일 앵커·직전 종가 없음으로 export 에서 제외된 행(`parent_close_stale_before_exDate`·`no_prior_parent_close`)은 manifest 개수로만 보이고 엔진의 `unconfirmed` 에는 나타나지 않는다.
- 프론트엔드는 `unconfirmed`·`pnlUnconfirmed`·`unconfirmedWeight` 를 아직 그리지 않는다(JSON 계약만).
- 자회사가 패널 밖이고 값도 없으면 `pending_dropped(child_not_in_panel)` 이 된다: 유니버스로 좁힌 패널(예 Top500 멤버만)에서는 실제로 가격이 있는 자회사도 여기 잡히므로, 전체 북 비교는 자회사가 포함된 패널로 돌려야 한다.
- 패널 안 자회사인데 비율이 없는 leg(값만 있음)는 종전대로 `unpriced`(발행 원장에 해당 행 없음).
- `export_events` 는 패널이 자라면(일별 갱신) 다시 내야 한다(정산 bar·앵커가 패널에 묶임); `no_settlement_bar_in_panel` 행은 그때 정산 가능해진다.

