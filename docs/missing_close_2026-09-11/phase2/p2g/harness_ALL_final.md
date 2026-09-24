# Top500 결측·상폐 전기간 하네스

- config: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2g3_ALL/config_ALL.yaml`
- server: `/tmp/longshort_module_server/build/longshort-server`
- 패널: T=4450 N=6399 2010-01-04 ~ 2026-09-11, feeRate=0.0005 barsPerYear=252
- 요청: simStart=2010-01-04 isStart=2012-01-01 osStart=2020-01-01 mode=1 missingClose=carry staleQuoteSettleBars=config
- 유니버스: ALL (`/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2g3_ALL/config_ALL.yaml`)
- 결측 계약 키: ['missingClosePolicy', 'defaultDelistingReturn', 'staleQuoteSettleBars', 'eventsLoaded', 'settlementSymbols']
- 가드: config 선언=True 카운터={'eventsPriceChecks': 1767, 'eventsPriceChecksOutOfRange': 0, 'eventsPriceChecksNotEvaluable': 778} 
- **exit code: 0** (all runs OK)
- 미노출 응답 키: 없음
- 데이터 로드: 6.2s, 로드 직후 RSS 785MB (peak 785MB)

| alpha | R | status | 기간 | bars | PnL | Sharpe(full) | Sharpe(IS) | Sharpe(OS) | MDD | turnover/bar | fee 합계 | retirements | frozenGrossSum | postEndDateQuoteBars | pendingSettlements | successorConversions | convertedGross | computedSettlementReturns | missingData | sec | RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| returns_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -3.6312 | -2.499 | -3.735 | -1.980 | -3.6617 | 1.4472 | 3.0378 | 2545 | 14.7522 | 3505 | 0 | 322 | 0.1355 | 1283 | missingHeldBars=9569, blockedOrders=9585, blockedTradeWeight=14.897502198959293, missingEntries=16, resumedHoldings=254, delistings=1795, delistingPnl=0.0008280803275413053, settledGross=0.6629955510720313, defaultDelistings=447, defaultDelistingGross=0.3224023368703889, terminalFrozenGross=0.05825422654523342, cancelledDelistingEntries=1704, frozenGrossSum=14.75220727532875, retirements=2545, postEndDateQuoteBars=3505, successorConversions=322, convertedGross=0.13553126494333334, computedSettlementReturns=1283 | 1.259 | 977 |
| returns_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -3.5562 | -2.439 | -3.603 | -1.932 | -3.5860 | 1.4472 | 3.0378 | 2545 | 14.7522 | 3505 | 0 | 322 | 0.1355 | 1283 | missingHeldBars=9569, blockedOrders=9585, blockedTradeWeight=14.897502198959293, missingEntries=16, resumedHoldings=254, delistings=1795, delistingPnl=0.07582144893179124, settledGross=0.6629955510720313, defaultDelistings=447, defaultDelistingGross=0.3224023368703889, terminalFrozenGross=0.05825422654523342, cancelledDelistingEntries=1704, frozenGrossSum=14.75220727532875, retirements=2545, postEndDateQuoteBars=3505, successorConversions=322, convertedGross=0.13553126494333334, computedSettlementReturns=1283 | 1.203 | 1166 |
| neg_returns_tsmean5_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.0801 | 0.052 | -0.918 | 0.521 | -0.6818 | 0.6235 | 1.3088 | 2545 | 16.3595 | 3505 | 0 | 323 | 0.1156 | 1284 | missingHeldBars=9618, blockedOrders=9632, blockedTradeWeight=16.225108886673816, missingEntries=14, resumedHoldings=271, delistings=1796, delistingPnl=0.0008656146373918396, settledGross=0.590127482762971, defaultDelistings=447, defaultDelistingGross=0.2596999082966115, terminalFrozenGross=0.05917574889800575, cancelledDelistingEntries=1705, frozenGrossSum=16.359536905503287, retirements=2545, postEndDateQuoteBars=3505, successorConversions=323, convertedGross=0.11556996236395758, computedSettlementReturns=1284 | 1.306 | 1168 |
| neg_returns_tsmean5_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.0736 | 0.048 | -0.960 | 0.537 | -0.7021 | 0.6235 | 1.3088 | 2545 | 16.3595 | 3505 | 0 | 323 | 0.1156 | 1284 | missingHeldBars=9618, blockedOrders=9632, blockedTradeWeight=16.225108886673816, missingEntries=14, resumedHoldings=271, delistings=1796, delistingPnl=-0.005711267487503939, settledGross=0.590127482762971, defaultDelistings=447, defaultDelistingGross=0.2596999082966115, terminalFrozenGross=0.05917574889800575, cancelledDelistingEntries=1705, frozenGrossSum=16.359536905503287, retirements=2545, postEndDateQuoteBars=3505, successorConversions=323, convertedGross=0.11556996236395758, computedSettlementReturns=1284 | 1.405 | 1355 |
| close_tsmean20_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -8.3391 | -1.280 | -1.694 | -1.014 | -8.3898 | 0.0172 | 0.0360 | 2545 | 5.9102 | 3505 | 0 | 323 | 0.2789 | 1284 | missingHeldBars=9654, blockedOrders=9689, blockedTradeWeight=3.144768295358195, missingEntries=35, resumedHoldings=279, delistings=1796, delistingPnl=0.007191558102227187, settledGross=0.41124499895781663, defaultDelistings=447, defaultDelistingGross=0.11171476695312879, terminalFrozenGross=0.009183532768722408, cancelledDelistingEntries=1705, frozenGrossSum=5.910165907075065, retirements=2545, postEndDateQuoteBars=3505, successorConversions=323, convertedGross=0.2789075281429689, computedSettlementReturns=1284 | 1.093 | 1357 |
| close_tsmean20_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -8.2326 | -1.263 | -1.685 | -0.990 | -8.2836 | 0.0172 | 0.0360 | 2545 | 5.9102 | 3505 | 0 | 323 | 0.2789 | 1284 | missingHeldBars=9654, blockedOrders=9689, blockedTradeWeight=3.144768295358195, missingEntries=35, resumedHoldings=279, delistings=1796, delistingPnl=0.11362981074059422, settledGross=0.41124499895781663, defaultDelistings=447, defaultDelistingGross=0.11171476695312879, terminalFrozenGross=0.009183532768722408, cancelledDelistingEntries=1705, frozenGrossSum=5.910165907075065, retirements=2545, postEndDateQuoteBars=3505, successorConversions=323, convertedGross=0.2789075281429689, computedSettlementReturns=1284 | 1.084 | 1357 |
| afl_100_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.3815 | 0.042 | -0.671 | 0.397 | -2.8605 | 0.0470 | 0.0987 | 2545 | 21.1829 | 3505 | 0 | 319 | 0.0740 | 1280 | missingHeldBars=8820, blockedOrders=8833, blockedTradeWeight=20.750357444937062, missingEntries=17, resumedHoldings=253, delistings=1788, delistingPnl=-0.0018240753736967592, settledGross=0.5694670162469663, defaultDelistings=444, defaultDelistingGross=0.21055957104215756, terminalFrozenGross=0.06360368455003124, cancelledDelistingEntries=1700, frozenGrossSum=21.18285414744759, retirements=2545, postEndDateQuoteBars=3505, successorConversions=319, convertedGross=0.07398593577976888, computedSettlementReturns=1280 | 1.414 | 1489 |
| afl_100_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.3420 | 0.038 | -0.666 | 0.385 | -2.8582 | 0.0470 | 0.0987 | 2545 | 21.1829 | 3505 | 0 | 319 | 0.0740 | 1280 | missingHeldBars=8820, blockedOrders=8833, blockedTradeWeight=20.750357444937062, missingEntries=17, resumedHoldings=253, delistings=1788, delistingPnl=-0.041384612821689246, settledGross=0.5694670162469663, defaultDelistings=444, defaultDelistingGross=0.21055957104215756, terminalFrozenGross=0.06360368455003124, cancelledDelistingEntries=1700, frozenGrossSum=21.18285414744759, retirements=2545, postEndDateQuoteBars=3505, successorConversions=319, convertedGross=0.07398593577976888, computedSettlementReturns=1280 | 1.048 | 1677 |
| afl_963_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.1918 | 0.331 | 0.133 | 0.517 | -0.0751 | 0.0390 | 0.0819 | 2545 | 2.5897 | 3505 | 0 | 279 | 0.0927 | 1130 | missingHeldBars=3786, blockedOrders=3777, blockedTradeWeight=2.526749812992061, missingEntries=2, resumedHoldings=144, delistings=1420, delistingPnl=0.0006898878082420212, settledGross=0.5241368651649675, defaultDelistings=230, defaultDelistingGross=0.10751179125264078, terminalFrozenGross=0.011905241304890613, cancelledDelistingEntries=1365, frozenGrossSum=2.5896847178882827, retirements=2545, postEndDateQuoteBars=3505, successorConversions=279, convertedGross=0.09269312226676198, computedSettlementReturns=1130 | 1.419 | 1807 |
| afl_963_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.1959 | 0.337 | 0.141 | 0.523 | -0.0760 | 0.0390 | 0.0819 | 2545 | 2.5897 | 3505 | 0 | 279 | 0.0927 | 1130 | missingHeldBars=3786, blockedOrders=3777, blockedTradeWeight=2.526749812992061, missingEntries=2, resumedHoldings=144, delistings=1420, delistingPnl=0.004788276413589671, settledGross=0.5241368651649675, defaultDelistings=230, defaultDelistingGross=0.10751179125264078, terminalFrozenGross=0.011905241304890613, cancelledDelistingEntries=1365, frozenGrossSum=2.5896847178882827, retirements=2545, postEndDateQuoteBars=3505, successorConversions=279, convertedGross=0.09269312226676198, computedSettlementReturns=1130 | 0.971 | 1807 |

## 정산 source 분포

| source | 정산 건수 | 종목 수 | Σ\|exposure\| |
| --- | --- | --- | --- |
| computed | 6261 | 1284 | 2.2957 |
| default | 2015 | 447 | 1.0119 |
| explicit | 319 | 65 | 0.1375 |

## 기본 R 정산 영향 (source=default를 cacheUnitStatus로 분해)

| cacheUnitStatus | 종목 수 | 알파평균 Σ\|exposure\| | 최대 | 최소 | R 0→−1 손익 스윙(평균) | 비중% |
| --- | --- | --- | --- | --- | --- | --- |
| no_terms | 290 | 0.1326 | 0.2079 | 0.0495 | +0.0109 | 65.2 |
| no_ratio | 110 | 0.0430 | 0.0568 | 0.0293 | +0.0100 | 21.2 |
| no_cache_successor_at_settlement | 19 | 0.0093 | 0.0175 | 0.0056 | +0.0022 | 4.6 |
| rejected:R_out_of_range | 7 | 0.0083 | 0.0238 | 0.0020 | +0.0028 | 4.1 |
| rejected:rename_not_1to1 | 6 | 0.0044 | 0.0076 | 0.0011 | +0.0017 | 2.1 |
| no_successor_id | 7 | 0.0034 | 0.0063 | 0.0014 | +0.0003 | 1.7 |
| rejected:cash_R_out_of_range | 7 | 0.0020 | 0.0030 | 0.0011 | +0.0005 | 1.0 |
| no_db_target | 1 | 0.0004 | 0.0009 | 0.0000 | -0.0001 | 0.2 |

- 백필 워크리스트: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2g3_ALL/default_settlements_worklist.csv` (447종목)
- 정산 로그 원자료: `settlement_log.csv` (17190행)

## R 민감도 항등식 (PnL 차이 = 정산 손익 차이)

| alpha | PnL(R=0) | PnL(R=-1) | 차이 | delistingPnl 차이 | 잔차 |
| --- | --- | --- | --- | --- | --- |
| returns_zscore | -3.631202 | -3.556209 | 0.074993 | 0.074993 | 2.609e-15 |
| neg_returns_tsmean5_zscore | 0.080135 | 0.073558 | -0.006577 | -0.006577 | -2.099e-16 |
| close_tsmean20_zscore | -8.339075 | -8.232637 | 0.106438 | 0.106438 | -1.343e-14 |
| afl_100_zscore | 0.381521 | 0.341960 | -0.039561 | -0.039561 | -3.331e-16 |
| afl_963_zscore | 0.191840 | 0.195939 | 0.004098 | 0.004098 | 4.684e-17 |
