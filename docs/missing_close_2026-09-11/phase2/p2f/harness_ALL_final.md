# Top500 결측·상폐 전기간 하네스

- config: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f3_ALL/config_ALL.yaml`
- server: `/tmp/longshort_module_server/build/longshort-server`
- 패널: T=4450 N=6399 2010-01-04 ~ 2026-09-11, feeRate=0.0005 barsPerYear=252
- 요청: simStart=2010-01-04 isStart=2012-01-01 osStart=2020-01-01 mode=1 missingClose=carry staleQuoteSettleBars=config
- 유니버스: ALL (`/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f3_ALL/config_ALL.yaml`)
- 결측 계약 키: ['missingClosePolicy', 'defaultDelistingReturn', 'staleQuoteSettleBars', 'eventsLoaded', 'settlementSymbols']
- 가드: config 선언=True 카운터={'eventsPriceChecks': 1753, 'eventsPriceChecksOutOfRange': 0, 'eventsPriceChecksNotEvaluable': 792} 
- **exit code: 0** (all runs OK)
- 미노출 응답 키: 없음
- 데이터 로드: 5.8s, 로드 직후 RSS 788MB (peak 788MB)

| alpha | R | status | 기간 | bars | PnL | Sharpe(full) | Sharpe(IS) | Sharpe(OS) | MDD | turnover/bar | fee 합계 | retirements | frozenGrossSum | postEndDateQuoteBars | pendingSettlements | successorConversions | convertedGross | computedSettlementReturns | missingData | sec | RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| returns_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -3.6302 | -2.499 | -3.735 | -1.981 | -3.6607 | 1.4468 | 3.0369 | 2545 | 16.1712 | 3505 | 0 | 313 | 0.1335 | 1271 | missingHeldBars=11964, blockedOrders=11981, blockedTradeWeight=16.484618389278875, missingEntries=17, resumedHoldings=255, delistings=1794, delistingPnl=-2.5760720363982562e-06, settledGross=0.6621320196597881, defaultDelistings=462, defaultDelistingGross=0.32357774499295694, terminalFrozenGross=0.05824199741086963, cancelledDelistingEntries=1103, frozenGrossSum=16.171203633739204, retirements=2545, postEndDateQuoteBars=3505, successorConversions=313, convertedGross=0.13352333260130833, computedSettlementReturns=1271 | 1.204 | 980 |
| returns_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -3.5560 | -2.440 | -3.601 | -1.935 | -3.5857 | 1.4468 | 3.0369 | 2545 | 16.1712 | 3505 | 0 | 313 | 0.1335 | 1271 | missingHeldBars=11964, blockedOrders=11981, blockedTradeWeight=16.484618389278875, missingEntries=17, resumedHoldings=255, delistings=1794, delistingPnl=0.07422460347346457, settledGross=0.6621320196597881, defaultDelistings=462, defaultDelistingGross=0.32357774499295694, terminalFrozenGross=0.05824199741086963, cancelledDelistingEntries=1103, frozenGrossSum=16.171203633739204, retirements=2545, postEndDateQuoteBars=3505, successorConversions=313, convertedGross=0.13352333260130833, computedSettlementReturns=1271 | 1.101 | 980 |
| neg_returns_tsmean5_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.0809 | 0.053 | -0.918 | 0.522 | -0.6809 | 0.6234 | 1.3084 | 2545 | 17.5016 | 3505 | 0 | 314 | 0.1123 | 1272 | missingHeldBars=12013, blockedOrders=12028, blockedTradeWeight=17.317236135607363, missingEntries=15, resumedHoldings=272, delistings=1795, delistingPnl=0.0016256691088740384, settledGross=0.5931175813407634, defaultDelistings=462, defaultDelistingGross=0.2638560266393764, terminalFrozenGross=0.05916544149144729, cancelledDelistingEntries=1225, frozenGrossSum=17.501642062458266, retirements=2545, postEndDateQuoteBars=3505, successorConversions=314, convertedGross=0.11229286146833989, computedSettlementReturns=1272 | 1.253 | 982 |
| neg_returns_tsmean5_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.0719 | 0.047 | -0.955 | 0.534 | -0.6999 | 0.6234 | 1.3084 | 2545 | 17.5016 | 3505 | 0 | 314 | 0.1123 | 1272 | missingHeldBars=12013, blockedOrders=12028, blockedTradeWeight=17.317236135607363, missingEntries=15, resumedHoldings=272, delistings=1795, delistingPnl=-0.0074004311873722035, settledGross=0.5931175813407634, defaultDelistings=462, defaultDelistingGross=0.2638560266393764, terminalFrozenGross=0.05916544149144729, cancelledDelistingEntries=1225, frozenGrossSum=17.501642062458266, retirements=2545, postEndDateQuoteBars=3505, successorConversions=314, convertedGross=0.11229286146833989, computedSettlementReturns=1272 | 1.256 | 982 |
| close_tsmean20_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -8.3437 | -1.280 | -1.696 | -1.014 | -8.3944 | 0.0172 | 0.0361 | 2545 | 6.9089 | 3505 | 0 | 314 | 0.2729 | 1272 | missingHeldBars=12049, blockedOrders=12085, blockedTradeWeight=3.759482715229486, missingEntries=36, resumedHoldings=280, delistings=1795, delistingPnl=0.0028856996428258553, settledGross=0.4120652397653348, defaultDelistings=462, defaultDelistingGross=0.11449370512943442, terminalFrozenGross=0.009182187891438263, cancelledDelistingEntries=1225, frozenGrossSum=6.908896189510731, retirements=2545, postEndDateQuoteBars=3505, successorConversions=314, convertedGross=0.2729497421718138, computedSettlementReturns=1272 | 1.117 | 1172 |
| close_tsmean20_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -8.2351 | -1.264 | -1.687 | -0.989 | -8.2861 | 0.0172 | 0.0361 | 2545 | 6.9089 | 3505 | 0 | 314 | 0.2729 | 1272 | missingHeldBars=12049, blockedOrders=12085, blockedTradeWeight=3.759482715229486, missingEntries=36, resumedHoldings=280, delistings=1795, delistingPnl=0.11144623550960356, settledGross=0.4120652397653348, defaultDelistings=462, defaultDelistingGross=0.11449370512943442, terminalFrozenGross=0.009182187891438263, cancelledDelistingEntries=1225, frozenGrossSum=6.908896189510731, retirements=2545, postEndDateQuoteBars=3505, successorConversions=314, convertedGross=0.2729497421718138, computedSettlementReturns=1272 | 1.104 | 1362 |
| afl_100_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.3825 | 0.043 | -0.671 | 0.397 | -2.8601 | 0.0470 | 0.0987 | 2545 | 22.1533 | 3505 | 0 | 310 | 0.0718 | 1268 | missingHeldBars=11213, blockedOrders=11215, blockedTradeWeight=21.53130565217899, missingEntries=18, resumedHoldings=254, delistings=1787, delistingPnl=-0.0016680734863273733, settledGross=0.5709384500848024, defaultDelistings=459, defaultDelistingGross=0.21499728871932025, terminalFrozenGross=0.06360218651192141, cancelledDelistingEntries=1221, frozenGrossSum=22.15329747881868, retirements=2545, postEndDateQuoteBars=3505, successorConversions=310, convertedGross=0.0718035154071809, computedSettlementReturns=1268 | 1.392 | 1495 |
| afl_100_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.3474 | 0.039 | -0.665 | 0.385 | -2.8578 | 0.0470 | 0.0987 | 2545 | 22.1533 | 3505 | 0 | 310 | 0.0718 | 1268 | missingHeldBars=11213, blockedOrders=11215, blockedTradeWeight=21.53130565217899, missingEntries=18, resumedHoldings=254, delistings=1787, delistingPnl=-0.03671628467305005, settledGross=0.5709384500848024, defaultDelistings=459, defaultDelistingGross=0.21499728871932025, terminalFrozenGross=0.06360218651192141, cancelledDelistingEntries=1221, frozenGrossSum=22.15329747881868, retirements=2545, postEndDateQuoteBars=3505, successorConversions=310, convertedGross=0.0718035154071809, computedSettlementReturns=1268 | 0.974 | 1495 |
| afl_963_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.1920 | 0.331 | 0.133 | 0.518 | -0.0751 | 0.0390 | 0.0819 | 2545 | 3.2114 | 3505 | 0 | 272 | 0.0913 | 1120 | missingHeldBars=5210, blockedOrders=5154, blockedTradeWeight=2.9169983494316467, missingEntries=3, resumedHoldings=144, delistings=1420, delistingPnl=0.0008375543270876366, settledGross=0.5255222072017803, defaultDelistings=246, defaultDelistingGross=0.11338546274409848, terminalFrozenGross=0.011901675047035733, cancelledDelistingEntries=978, frozenGrossSum=3.211379257853292, retirements=2545, postEndDateQuoteBars=3505, successorConversions=272, convertedGross=0.09134223624254549, computedSettlementReturns=1120 | 1.419 | 1808 |
| afl_963_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.1992 | 0.342 | 0.139 | 0.531 | -0.0760 | 0.0390 | 0.0819 | 2545 | 3.2114 | 3505 | 0 | 272 | 0.0913 | 1120 | missingHeldBars=5210, blockedOrders=5154, blockedTradeWeight=2.9169983494316467, missingEntries=3, resumedHoldings=144, delistings=1420, delistingPnl=0.008077671126494055, settledGross=0.5255222072017803, defaultDelistings=246, defaultDelistingGross=0.11338546274409848, terminalFrozenGross=0.011901675047035733, cancelledDelistingEntries=978, frozenGrossSum=3.211379257853292, retirements=2545, postEndDateQuoteBars=3505, successorConversions=272, convertedGross=0.09134223624254549, computedSettlementReturns=1120 | 0.961 | 1808 |

## 정산 source 분포

| source | 정산 건수 | 종목 수 | Σ\|exposure\| |
| --- | --- | --- | --- |
| computed | 6203 | 1272 | 2.2774 |
| default | 2091 | 462 | 1.0303 |
| explicit | 297 | 61 | 0.1334 |

## 기본 R 정산 영향 (source=default를 cacheUnitStatus로 분해)

| cacheUnitStatus | 종목 수 | 알파평균 Σ\|exposure\| | 최대 | 최소 | R 0→−1 손익 스윙(평균) | 비중% |
| --- | --- | --- | --- | --- | --- | --- |
| no_terms | 291 | 0.1327 | 0.2077 | 0.0496 | +0.0109 | 64.0 |
| no_ratio | 122 | 0.0467 | 0.0601 | 0.0320 | +0.0120 | 22.5 |
| rejected:R_out_of_range | 8 | 0.0084 | 0.0240 | 0.0021 | +0.0029 | 4.1 |
| no_cache_successor_at_settlement | 17 | 0.0079 | 0.0145 | 0.0048 | +0.0018 | 3.8 |
| rejected:rename_not_1to1 | 8 | 0.0056 | 0.0105 | 0.0014 | +0.0010 | 2.7 |
| no_successor_id | 8 | 0.0036 | 0.0063 | 0.0016 | +0.0005 | 1.7 |
| rejected:cash_R_out_of_range | 7 | 0.0020 | 0.0030 | 0.0011 | +0.0005 | 1.0 |
| no_db_target | 1 | 0.0004 | 0.0009 | 0.0000 | -0.0001 | 0.2 |

- 백필 워크리스트: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f3_ALL/default_settlements_worklist.csv` (462종목)
- 정산 로그 원자료: `settlement_log.csv` (17182행)

## R 민감도 항등식 (PnL 차이 = 정산 손익 차이)

| alpha | PnL(R=0) | PnL(R=-1) | 차이 | delistingPnl 차이 | 잔차 |
| --- | --- | --- | --- | --- | --- |
| returns_zscore | -3.630185 | -3.555958 | 0.074227 | 0.074227 | -4.233e-15 |
| neg_returns_tsmean5_zscore | 0.080941 | 0.071915 | -0.009026 | -0.009026 | 3.157e-16 |
| close_tsmean20_zscore | -8.343678 | -8.235118 | 0.108561 | 0.108561 | -1.098e-14 |
| afl_100_zscore | 0.382484 | 0.347436 | -0.035048 | -0.035048 | -6.800e-16 |
| afl_963_zscore | 0.191984 | 0.199224 | 0.007240 | 0.007240 | -5.291e-17 |
