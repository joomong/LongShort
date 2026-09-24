# Top500 결측·상폐 전기간 하네스

- config: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f2_Top1000/config_Top1000.yaml`
- server: `/tmp/longshort_module_server/build/longshort-server`
- 패널: T=4450 N=2315 2010-01-04 ~ 2026-09-11, feeRate=0.0005 barsPerYear=252
- 요청: simStart=2010-01-04 isStart=2012-01-01 osStart=2020-01-01 mode=1 missingClose=carry staleQuoteSettleBars=config
- 유니버스: Top1000 (`/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f2_Top1000/config_Top1000.yaml`)
- 결측 계약 키: ['missingClosePolicy', 'defaultDelistingReturn', 'staleQuoteSettleBars', 'eventsLoaded', 'settlementSymbols']
- 가드: config 선언=True 카운터={'eventsPriceChecks': 608, 'eventsPriceChecksOutOfRange': 0, 'eventsPriceChecksNotEvaluable': 193} 
- **exit code: 0** (all runs OK)
- 미노출 응답 키: 없음
- 데이터 로드: 3.8s, 로드 직후 RSS 413MB (peak 413MB)

| alpha | R | status | 기간 | bars | PnL | Sharpe(full) | Sharpe(IS) | Sharpe(OS) | MDD | turnover/bar | fee 합계 | retirements | frozenGrossSum | postEndDateQuoteBars | pendingSettlements | successorConversions | convertedGross | computedSettlementReturns | missingData | sec | RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| returns_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -3.5582 | -2.787 | -4.206 | -2.257 | -3.5720 | 1.4493 | 3.0421 | 801 | 3.4707 | 3 | 0 | 136 | 0.1463 | 379 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=3.6858100588719864, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=0.002634929428301534, settledGross=0.36568743264881837, defaultDelistings=100, defaultDelistingGross=0.10580052801081206, terminalFrozenGross=0.01317473096700952, cancelledDelistingEntries=283, frozenGrossSum=3.470740009214544, retirements=801, postEndDateQuoteBars=3, successorConversions=136, convertedGross=0.14632909784312856, computedSettlementReturns=379 | 0.461 | 527 |
| returns_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -3.5610 | -2.784 | -4.169 | -2.258 | -3.5741 | 1.4493 | 3.0421 | 801 | 3.4707 | 3 | 0 | 136 | 0.1463 | 379 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=3.6858100588719864, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=-0.00015007329173241993, settledGross=0.36568743264881837, defaultDelistings=100, defaultDelistingGross=0.10580052801081206, terminalFrozenGross=0.01317473096700952, cancelledDelistingEntries=283, frozenGrossSum=3.470740009214544, retirements=801, postEndDateQuoteBars=3, successorConversions=136, convertedGross=0.14632909784312856, computedSettlementReturns=379 | 0.422 | 527 |
| neg_returns_tsmean5_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.1187 | -0.087 | -1.072 | 0.370 | -0.6618 | 0.6265 | 1.3149 | 801 | 2.6338 | 3 | 0 | 136 | 0.1243 | 379 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=2.5952328104677265, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=0.0015228315887730658, settledGross=0.3190627151064437, defaultDelistings=100, defaultDelistingGross=0.08497136628652086, terminalFrozenGross=0.009132112576565888, cancelledDelistingEntries=308, frozenGrossSum=2.633812867367029, retirements=801, postEndDateQuoteBars=3, successorConversions=136, convertedGross=0.12429864270521221, computedSettlementReturns=379 | 0.506 | 639 |
| neg_returns_tsmean5_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.0872 | -0.064 | -1.008 | 0.375 | -0.6342 | 0.6265 | 1.3149 | 801 | 2.6338 | 3 | 0 | 136 | 0.1243 | 379 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=2.5952328104677265, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=0.03298769565859051, settledGross=0.3190627151064437, defaultDelistings=100, defaultDelistingGross=0.08497136628652086, terminalFrozenGross=0.009132112576565888, cancelledDelistingEntries=308, frozenGrossSum=2.633812867367029, retirements=801, postEndDateQuoteBars=3, successorConversions=136, convertedGross=0.12429864270521221, computedSettlementReturns=379 | 0.509 | 750 |
| close_tsmean20_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.7302 | -0.866 | -1.268 | -0.496 | -0.7759 | 0.0071 | 0.0149 | 801 | 1.6996 | 3 | 0 | 136 | 0.0853 | 379 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=1.4707681266358337, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=-0.00019424194360790543, settledGross=0.34956256433864163, defaultDelistings=100, defaultDelistingGross=0.07976311931658185, terminalFrozenGross=0.0036482930567070314, cancelledDelistingEntries=308, frozenGrossSum=1.6995768948780063, retirements=801, postEndDateQuoteBars=3, successorConversions=136, convertedGross=0.0853013270520719, computedSettlementReturns=379 | 0.431 | 863 |
| close_tsmean20_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.6957 | -0.824 | -1.223 | -0.439 | -0.7414 | 0.0071 | 0.0149 | 801 | 1.6996 | 3 | 0 | 136 | 0.0853 | 379 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=1.4707681266358337, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=0.03433844301814296, settledGross=0.34956256433864163, defaultDelistings=100, defaultDelistingGross=0.07976311931658185, terminalFrozenGross=0.0036482930567070314, cancelledDelistingEntries=308, frozenGrossSum=1.6995768948780063, retirements=801, postEndDateQuoteBars=3, successorConversions=136, convertedGross=0.0853013270520719, computedSettlementReturns=379 | 0.383 | 863 |
| afl_100_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.2216 | -0.123 | -0.133 | -0.167 | -0.3994 | 0.0463 | 0.0973 | 801 | 1.2850 | 3 | 0 | 135 | 0.0704 | 378 | missingHeldBars=1673, blockedOrders=1637, blockedTradeWeight=1.1017974180397687, missingEntries=1, resumedHoldings=41, delistings=494, delistingPnl=-0.00028815597729447886, settledGross=0.3253118911668773, defaultDelistings=98, defaultDelistingGross=0.08111675821907197, terminalFrozenGross=0.0024575988646810313, cancelledDelistingEntries=307, frozenGrossSum=1.285033798581024, retirements=801, postEndDateQuoteBars=3, successorConversions=135, convertedGross=0.07035481995592392, computedSettlementReturns=378 | 0.658 | 973 |
| afl_100_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.1583 | -0.088 | -0.099 | -0.141 | -0.3944 | 0.0463 | 0.0973 | 801 | 1.2850 | 3 | 0 | 135 | 0.0704 | 378 | missingHeldBars=1673, blockedOrders=1637, blockedTradeWeight=1.1017974180397687, missingEntries=1, resumedHoldings=41, delistings=494, delistingPnl=0.0629304479494767, settledGross=0.3253118911668773, defaultDelistings=98, defaultDelistingGross=0.08111675821907197, terminalFrozenGross=0.0024575988646810313, cancelledDelistingEntries=307, frozenGrossSum=1.285033798581024, retirements=801, postEndDateQuoteBars=3, successorConversions=135, convertedGross=0.07035481995592392, computedSettlementReturns=378 | 0.36 | 973 |
| afl_963_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.3047 | 0.522 | 0.415 | 0.666 | -0.0687 | 0.0412 | 0.0864 | 801 | 2.2401 | 3 | 0 | 131 | 0.0935 | 369 | missingHeldBars=1587, blockedOrders=1550, blockedTradeWeight=2.0211537329251907, missingEntries=1, resumedHoldings=37, delistings=476, delistingPnl=0.001980811935888725, settledGross=0.35492350146002805, defaultDelistings=91, defaultDelistingGross=0.08616648073131074, terminalFrozenGross=0.004531836516022915, cancelledDelistingEntries=295, frozenGrossSum=2.240067072698101, retirements=801, postEndDateQuoteBars=3, successorConversions=131, convertedGross=0.0934823271955482, computedSettlementReturns=369 | 0.681 | 1109 |
| afl_963_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.3275 | 0.560 | 0.464 | 0.701 | -0.0713 | 0.0412 | 0.0864 | 801 | 2.2401 | 3 | 0 | 131 | 0.0935 | 369 | missingHeldBars=1587, blockedOrders=1550, blockedTradeWeight=2.0211537329251907, missingEntries=1, resumedHoldings=37, delistings=476, delistingPnl=0.024841577761114467, settledGross=0.35492350146002805, defaultDelistings=91, defaultDelistingGross=0.08616648073131074, terminalFrozenGross=0.004531836516022915, cancelledDelistingEntries=295, frozenGrossSum=2.240067072698101, retirements=801, postEndDateQuoteBars=3, successorConversions=131, convertedGross=0.0934823271955482, computedSettlementReturns=369 | 0.359 | 1109 |

## 정산 source 분포

| source | 정산 건수 | 종목 수 | Σ\|exposure\| |
| --- | --- | --- | --- |
| computed | 1884 | 379 | 1.7247 |
| default | 489 | 100 | 0.4378 |
| explicit | 88 | 18 | 0.0746 |

## 기본 R 정산 영향 (source=default를 cacheUnitStatus로 분해)

| cacheUnitStatus | 종목 수 | 알파평균 Σ\|exposure\| | 최대 | 최소 | R 0→−1 손익 스윙(평균) | 비중% |
| --- | --- | --- | --- | --- | --- | --- |
| no_terms | 57 | 0.0473 | 0.0556 | 0.0440 | +0.0164 | 53.9 |
| no_successor_id | 35 | 0.0332 | 0.0383 | 0.0278 | +0.0120 | 37.9 |
| no_cache_successor_at_settlement | 4 | 0.0046 | 0.0082 | 0.0019 | +0.0005 | 5.2 |
| rejected:R_out_of_range | 2 | 0.0011 | 0.0018 | 0.0001 | +0.0004 | 1.2 |
| rejected:cash_R_out_of_range | 1 | 0.0008 | 0.0015 | 0.0004 | +0.0008 | 0.9 |
| rejected:rename_not_1to1 | 1 | 0.0008 | 0.0016 | 0.0002 | -0.0003 | 0.9 |

- 백필 워크리스트: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f2_Top1000/default_settlements_worklist.csv` (100종목)
- 정산 로그 원자료: `settlement_log.csv` (4922행)

## R 민감도 항등식 (PnL 차이 = 정산 손익 차이)

| alpha | PnL(R=0) | PnL(R=-1) | 차이 | delistingPnl 차이 | 잔차 |
| --- | --- | --- | --- | --- | --- |
| returns_zscore | -3.558233 | -3.561018 | -0.002785 | -0.002785 | -2.815e-16 |
| neg_returns_tsmean5_zscore | -0.118692 | -0.087227 | 0.031465 | 0.031465 | -1.735e-16 |
| close_tsmean20_zscore | -0.730243 | -0.695710 | 0.034533 | 0.034533 | 3.469e-16 |
| afl_100_zscore | -0.221552 | -0.158334 | 0.063219 | 0.063219 | 4.163e-17 |
| afl_963_zscore | 0.304656 | 0.327517 | 0.022861 | 0.022861 | -2.914e-16 |
