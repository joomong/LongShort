# Top500 결측·상폐 전기간 하네스

- config: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f2_ALL/config_ALL.yaml`
- server: `/tmp/longshort_module_server/build/longshort-server`
- 패널: T=4450 N=6399 2010-01-04 ~ 2026-09-11, feeRate=0.0005 barsPerYear=252
- 요청: simStart=2010-01-04 isStart=2012-01-01 osStart=2020-01-01 mode=1 missingClose=carry staleQuoteSettleBars=config
- 유니버스: ALL (`/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f2_ALL/config_ALL.yaml`)
- 결측 계약 키: ['missingClosePolicy', 'defaultDelistingReturn', 'staleQuoteSettleBars', 'eventsLoaded', 'settlementSymbols']
- 가드: config 선언=True 카운터={'eventsPriceChecks': 1722, 'eventsPriceChecksOutOfRange': 0, 'eventsPriceChecksNotEvaluable': 823} 
- **exit code: 0** (all runs OK)
- 미노출 응답 키: 없음
- 데이터 로드: 5.7s, 로드 직후 RSS 786MB (peak 786MB)

| alpha | R | status | 기간 | bars | PnL | Sharpe(full) | Sharpe(IS) | Sharpe(OS) | MDD | turnover/bar | fee 합계 | retirements | frozenGrossSum | postEndDateQuoteBars | pendingSettlements | successorConversions | convertedGross | computedSettlementReturns | missingData | sec | RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| returns_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -3.6297 | -2.499 | -3.735 | -1.980 | -3.6602 | 1.4468 | 3.0369 | 2545 | 16.1712 | 3505 | 0 | 290 | 0.1233 | 1246 | missingHeldBars=11964, blockedOrders=11981, blockedTradeWeight=16.484618389278875, missingEntries=17, resumedHoldings=255, delistings=1794, delistingPnl=0.00048098283503505436, settledGross=0.6723603916821912, defaultDelistings=513, defaultDelistingGross=0.3511997876734213, terminalFrozenGross=0.05824199741086963, cancelledDelistingEntries=1103, frozenGrossSum=16.171203633739204, retirements=2545, postEndDateQuoteBars=3505, successorConversions=290, convertedGross=0.12328301317839802, computedSettlementReturns=1246 | 1.16 | 978 |
| returns_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -3.5596 | -2.442 | -3.607 | -1.934 | -3.5894 | 1.4468 | 3.0369 | 2545 | 16.1712 | 3505 | 0 | 290 | 0.1233 | 1246 | missingHeldBars=11964, blockedOrders=11981, blockedTradeWeight=16.484618389278875, missingEntries=17, resumedHoldings=255, delistings=1794, delistingPnl=0.07053725660401292, settledGross=0.6723603916821912, defaultDelistings=513, defaultDelistingGross=0.3511997876734213, terminalFrozenGross=0.05824199741086963, cancelledDelistingEntries=1103, frozenGrossSum=16.171203633739204, retirements=2545, postEndDateQuoteBars=3505, successorConversions=290, convertedGross=0.12328301317839802, computedSettlementReturns=1246 | 1.109 | 980 |
| neg_returns_tsmean5_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.0806 | 0.053 | -0.918 | 0.522 | -0.6811 | 0.6234 | 1.3084 | 2545 | 17.5016 | 3505 | 0 | 291 | 0.1046 | 1247 | missingHeldBars=12013, blockedOrders=12028, blockedTradeWeight=17.317236135607363, missingEntries=15, resumedHoldings=272, delistings=1795, delistingPnl=0.001290950500958395, settledGross=0.6008013973228752, defaultDelistings=513, defaultDelistingGross=0.28878003403412633, terminalFrozenGross=0.05916544149144729, cancelledDelistingEntries=1225, frozenGrossSum=17.501642062458266, retirements=2545, postEndDateQuoteBars=3505, successorConversions=291, convertedGross=0.10458906618546095, computedSettlementReturns=1247 | 1.253 | 980 |
| neg_returns_tsmean5_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.0779 | 0.051 | -0.951 | 0.535 | -0.7005 | 0.6234 | 1.3084 | 2545 | 17.5016 | 3505 | 0 | 291 | 0.1046 | 1247 | missingHeldBars=12013, blockedOrders=12028, blockedTradeWeight=17.317236135607363, missingEntries=15, resumedHoldings=272, delistings=1795, delistingPnl=-0.001410338019033421, settledGross=0.6008013973228752, defaultDelistings=513, defaultDelistingGross=0.28878003403412633, terminalFrozenGross=0.05916544149144729, cancelledDelistingEntries=1225, frozenGrossSum=17.501642062458266, retirements=2545, postEndDateQuoteBars=3505, successorConversions=291, convertedGross=0.10458906618546095, computedSettlementReturns=1247 | 1.264 | 980 |
| close_tsmean20_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -8.3435 | -1.280 | -1.696 | -1.014 | -8.3942 | 0.0172 | 0.0361 | 2545 | 6.9089 | 3505 | 0 | 291 | 0.2676 | 1247 | missingHeldBars=12049, blockedOrders=12085, blockedTradeWeight=3.759482715229486, missingEntries=36, resumedHoldings=280, delistings=1795, delistingPnl=0.0030277168093274646, settledGross=0.4173881459772537, defaultDelistings=513, defaultDelistingGross=0.12957850834144055, terminalFrozenGross=0.009182187891438263, cancelledDelistingEntries=1225, frozenGrossSum=6.908896189510731, retirements=2545, postEndDateQuoteBars=3505, successorConversions=291, convertedGross=0.2675678486080052, computedSettlementReturns=1247 | 1.111 | 1169 |
| close_tsmean20_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -8.2236 | -1.262 | -1.684 | -0.989 | -8.2745 | 0.0172 | 0.0361 | 2545 | 6.9089 | 3505 | 0 | 291 | 0.2676 | 1247 | missingHeldBars=12049, blockedOrders=12085, blockedTradeWeight=3.759482715229486, missingEntries=36, resumedHoldings=280, delistings=1795, delistingPnl=0.12297475023890629, settledGross=0.4173881459772537, defaultDelistings=513, defaultDelistingGross=0.12957850834144055, terminalFrozenGross=0.009182187891438263, cancelledDelistingEntries=1225, frozenGrossSum=6.908896189510731, retirements=2545, postEndDateQuoteBars=3505, successorConversions=291, convertedGross=0.2675678486080052, computedSettlementReturns=1247 | 1.113 | 1356 |
| afl_100_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.3828 | 0.043 | -0.671 | 0.397 | -2.8601 | 0.0470 | 0.0987 | 2545 | 22.1533 | 3505 | 0 | 287 | 0.0664 | 1243 | missingHeldBars=11213, blockedOrders=11215, blockedTradeWeight=21.53130565217899, missingEntries=18, resumedHoldings=254, delistings=1787, delistingPnl=-0.0013951435416425738, settledGross=0.5762593892958812, defaultDelistings=509, defaultDelistingGross=0.23113932400814605, terminalFrozenGross=0.06360218651192141, cancelledDelistingEntries=1221, frozenGrossSum=22.15329747881868, retirements=2545, postEndDateQuoteBars=3505, successorConversions=287, convertedGross=0.06643215933569745, computedSettlementReturns=1243 | 1.386 | 1490 |
| afl_100_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.3637 | 0.040 | -0.662 | 0.386 | -2.8570 | 0.0470 | 0.0987 | 2545 | 22.1533 | 3505 | 0 | 287 | 0.0664 | 1243 | missingHeldBars=11213, blockedOrders=11215, blockedTradeWeight=21.53130565217899, missingEntries=18, resumedHoldings=254, delistings=1787, delistingPnl=-0.020466983652234656, settledGross=0.5762593892958812, defaultDelistings=509, defaultDelistingGross=0.23113932400814605, terminalFrozenGross=0.06360218651192141, cancelledDelistingEntries=1221, frozenGrossSum=22.15329747881868, retirements=2545, postEndDateQuoteBars=3505, successorConversions=287, convertedGross=0.06643215933569745, computedSettlementReturns=1243 | 0.977 | 1490 |
| afl_963_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.1918 | 0.330 | 0.133 | 0.517 | -0.0751 | 0.0390 | 0.0819 | 2545 | 3.2114 | 3505 | 0 | 250 | 0.0845 | 1096 | missingHeldBars=5210, blockedOrders=5154, blockedTradeWeight=2.9169983494316467, missingEntries=3, resumedHoldings=144, delistings=1420, delistingPnl=0.0006490634961591308, settledGross=0.5322725481644707, defaultDelistings=294, defaultDelistingGross=0.1352759148830836, terminalFrozenGross=0.011901675047035733, cancelledDelistingEntries=978, frozenGrossSum=3.211379257853292, retirements=2545, postEndDateQuoteBars=3505, successorConversions=250, convertedGross=0.08446210684935358, computedSettlementReturns=1096 | 1.314 | 1621 |
| afl_963_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.2041 | 0.350 | 0.162 | 0.531 | -0.0760 | 0.0390 | 0.0819 | 2545 | 3.2114 | 3505 | 0 | 250 | 0.0845 | 1096 | missingHeldBars=5210, blockedOrders=5154, blockedTradeWeight=2.9169983494316467, missingEntries=3, resumedHoldings=144, delistings=1420, delistingPnl=0.012985318410308748, settledGross=0.5322725481644707, defaultDelistings=294, defaultDelistingGross=0.1352759148830836, terminalFrozenGross=0.011901675047035733, cancelledDelistingEntries=978, frozenGrossSum=3.211379257853292, retirements=2545, postEndDateQuoteBars=3505, successorConversions=250, convertedGross=0.08446210684935358, computedSettlementReturns=1096 | 0.945 | 1621 |

## 정산 source 분포

| source | 정산 건수 | 종목 수 | Σ\|exposure\| |
| --- | --- | --- | --- |
| computed | 6079 | 1247 | 2.2271 |
| default | 2342 | 513 | 1.1360 |
| explicit | 170 | 35 | 0.0780 |

## 기본 R 정산 영향 (source=default를 cacheUnitStatus로 분해)

| cacheUnitStatus | 종목 수 | 알파평균 Σ\|exposure\| | 최대 | 최소 | R 0→−1 손익 스윙(평균) | 비중% |
| --- | --- | --- | --- | --- | --- | --- |
| no_terms | 404 | 0.1768 | 0.2659 | 0.0973 | +0.0208 | 77.4 |
| no_successor_id | 77 | 0.0316 | 0.0413 | 0.0217 | +0.0092 | 13.8 |
| rejected:R_out_of_range | 6 | 0.0075 | 0.0236 | 0.0014 | +0.0033 | 3.3 |
| rejected:rename_not_1to1 | 8 | 0.0056 | 0.0105 | 0.0014 | +0.0010 | 2.4 |
| no_cache_successor_at_settlement | 10 | 0.0046 | 0.0093 | 0.0023 | +0.0016 | 2.0 |
| rejected:cash_R_out_of_range | 7 | 0.0020 | 0.0030 | 0.0011 | +0.0005 | 0.9 |
| no_db_target | 1 | 0.0004 | 0.0009 | 0.0000 | -0.0001 | 0.2 |

- 백필 워크리스트: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f2_ALL/default_settlements_worklist.csv` (513종목)
- 정산 로그 원자료: `settlement_log.csv` (17182행)

## R 민감도 항등식 (PnL 차이 = 정산 손익 차이)

| alpha | PnL(R=0) | PnL(R=-1) | 차이 | delistingPnl 차이 | 잔차 |
| --- | --- | --- | --- | --- | --- |
| returns_zscore | -3.629697 | -3.559640 | 0.070056 | 0.070056 | -5.357e-15 |
| neg_returns_tsmean5_zscore | 0.080609 | 0.077908 | -0.002701 | -0.002701 | 1.587e-16 |
| close_tsmean20_zscore | -8.343534 | -8.223587 | 0.119947 | 0.119947 | -8.799e-15 |
| afl_100_zscore | 0.382759 | 0.363687 | -0.019072 | -0.019072 | -1.249e-16 |
| afl_963_zscore | 0.191798 | 0.204134 | 0.012336 | 0.012336 | -1.752e-16 |
