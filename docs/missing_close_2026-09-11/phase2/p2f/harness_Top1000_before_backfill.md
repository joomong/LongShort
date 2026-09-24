# Top500 결측·상폐 전기간 하네스

- config: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f_Top1000/config_Top1000.yaml`
- server: `/tmp/longshort_module_server/build/longshort-server`
- 패널: T=4450 N=2315 2010-01-04 ~ 2026-09-11, feeRate=0.0005 barsPerYear=252
- 요청: simStart=2010-01-04 isStart=2012-01-01 osStart=2020-01-01 mode=1 missingClose=carry staleQuoteSettleBars=config
- 유니버스: Top1000 (`/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f_Top1000/config_Top1000.yaml`)
- 결측 계약 키: ['missingClosePolicy', 'defaultDelistingReturn', 'staleQuoteSettleBars', 'eventsLoaded', 'settlementSymbols']
- 가드: config 선언=True 카운터={'eventsPriceChecks': 594, 'eventsPriceChecksOutOfRange': 0, 'eventsPriceChecksNotEvaluable': 207} 
- **exit code: 0** (all runs OK)
- 미노출 응답 키: 없음
- 데이터 로드: 4.2s, 로드 직후 RSS 413MB (peak 413MB)

| alpha | R | status | 기간 | bars | PnL | Sharpe(full) | Sharpe(IS) | Sharpe(OS) | MDD | turnover/bar | fee 합계 | retirements | frozenGrossSum | postEndDateQuoteBars | pendingSettlements | successorConversions | convertedGross | computedSettlementReturns | missingData | sec | RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| returns_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -3.5585 | -2.787 | -4.207 | -2.257 | -3.5722 | 1.4493 | 3.0421 | 801 | 3.4707 | 3 | 0 | 126 | 0.1332 | 368 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=3.6858100588719864, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=0.002366541762630396, settledGross=0.37882984299269395, defaultDelistings=106, defaultDelistingGross=0.11610735568964016, terminalFrozenGross=0.01317473096700952, cancelledDelistingEntries=283, frozenGrossSum=3.470740009214544, retirements=801, postEndDateQuoteBars=3, successorConversions=126, convertedGross=0.13315549025775314, computedSettlementReturns=368 | 0.518 | 526 |
| returns_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -3.5478 | -2.773 | -4.152 | -2.258 | -3.5609 | 1.4493 | 3.0421 | 801 | 3.4707 | 3 | 0 | 126 | 0.1332 | 368 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=3.6858100588719864, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=0.013061070375011405, settledGross=0.37882984299269395, defaultDelistings=106, defaultDelistingGross=0.11610735568964016, terminalFrozenGross=0.01317473096700952, cancelledDelistingEntries=283, frozenGrossSum=3.470740009214544, retirements=801, postEndDateQuoteBars=3, successorConversions=126, convertedGross=0.13315549025775314, computedSettlementReturns=368 | 0.485 | 638 |
| neg_returns_tsmean5_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.1182 | -0.087 | -1.072 | 0.370 | -0.6615 | 0.6265 | 1.3149 | 801 | 2.6338 | 3 | 0 | 126 | 0.1154 | 368 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=2.5952328104677265, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=0.002056279371587423, settledGross=0.3279857083401092, defaultDelistings=106, defaultDelistingGross=0.08981153809173686, terminalFrozenGross=0.009132112576565888, cancelledDelistingEntries=308, frozenGrossSum=2.633812867367029, retirements=801, postEndDateQuoteBars=3, successorConversions=126, convertedGross=0.11542019631543987, computedSettlementReturns=368 | 0.512 | 750 |
| neg_returns_tsmean5_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.0913 | -0.067 | -1.023 | 0.383 | -0.6440 | 0.6265 | 1.3149 | 801 | 2.6338 | 3 | 0 | 126 | 0.1154 | 368 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=2.5952328104677265, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=0.028935730868134165, settledGross=0.3279857083401092, defaultDelistings=106, defaultDelistingGross=0.08981153809173686, terminalFrozenGross=0.009132112576565888, cancelledDelistingEntries=308, frozenGrossSum=2.633812867367029, retirements=801, postEndDateQuoteBars=3, successorConversions=126, convertedGross=0.11542019631543987, computedSettlementReturns=368 | 0.479 | 750 |
| close_tsmean20_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.7301 | -0.866 | -1.267 | -0.496 | -0.7758 | 0.0071 | 0.0149 | 801 | 1.6996 | 3 | 0 | 126 | 0.0787 | 368 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=1.4707681266358337, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=-8.905230174578273e-05, settledGross=0.3562613550101481, defaultDelistings=106, defaultDelistingGross=0.0847161005128137, terminalFrozenGross=0.0036482930567070314, cancelledDelistingEntries=308, frozenGrossSum=1.6995768948780063, retirements=801, postEndDateQuoteBars=3, successorConversions=126, convertedGross=0.07865215094753779, computedSettlementReturns=368 | 0.399 | 750 |
| close_tsmean20_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.6923 | -0.820 | -1.211 | -0.439 | -0.7379 | 0.0071 | 0.0149 | 801 | 1.6996 | 3 | 0 | 126 | 0.0787 | 368 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=1.4707681266358337, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=0.03777770298042869, settledGross=0.3562613550101481, defaultDelistings=106, defaultDelistingGross=0.0847161005128137, terminalFrozenGross=0.0036482930567070314, cancelledDelistingEntries=308, frozenGrossSum=1.6995768948780063, retirements=801, postEndDateQuoteBars=3, successorConversions=126, convertedGross=0.07865215094753779, computedSettlementReturns=368 | 0.397 | 750 |
| afl_100_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.2215 | -0.122 | -0.133 | -0.167 | -0.3994 | 0.0463 | 0.0973 | 801 | 1.2850 | 3 | 0 | 125 | 0.0649 | 367 | missingHeldBars=1673, blockedOrders=1637, blockedTradeWeight=1.1017974180397687, missingEntries=1, resumedHoldings=41, delistings=494, delistingPnl=-0.00022274977173565688, settledGross=0.3307743382266759, defaultDelistings=104, defaultDelistingGross=0.08568049971649407, terminalFrozenGross=0.0024575988646810313, cancelledDelistingEntries=307, frozenGrossSum=1.285033798581024, retirements=801, postEndDateQuoteBars=3, successorConversions=125, convertedGross=0.0649302624185753, computedSettlementReturns=367 | 0.693 | 860 |
| afl_100_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.1537 | -0.085 | -0.095 | -0.135 | -0.3944 | 0.0463 | 0.0973 | 801 | 1.2850 | 3 | 0 | 125 | 0.0649 | 367 | missingHeldBars=1673, blockedOrders=1637, blockedTradeWeight=1.1017974180397687, missingEntries=1, resumedHoldings=41, delistings=494, delistingPnl=0.06755959565245762, settledGross=0.3307743382266759, defaultDelistings=104, defaultDelistingGross=0.08568049971649407, terminalFrozenGross=0.0024575988646810313, cancelledDelistingEntries=307, frozenGrossSum=1.285033798581024, retirements=801, postEndDateQuoteBars=3, successorConversions=125, convertedGross=0.0649302624185753, computedSettlementReturns=367 | 0.402 | 973 |
| afl_963_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.3048 | 0.522 | 0.414 | 0.666 | -0.0687 | 0.0411 | 0.0864 | 801 | 2.2401 | 3 | 0 | 120 | 0.0869 | 357 | missingHeldBars=1587, blockedOrders=1550, blockedTradeWeight=2.0211537329251907, missingEntries=1, resumedHoldings=37, delistings=476, delistingPnl=0.002095338996706892, settledGross=0.36155929566470013, defaultDelistings=96, defaultDelistingGross=0.0862340502424199, terminalFrozenGross=0.004531836516022915, cancelledDelistingEntries=295, frozenGrossSum=2.240067072698101, retirements=801, postEndDateQuoteBars=3, successorConversions=120, convertedGross=0.08691901182891972, computedSettlementReturns=357 | 0.731 | 1102 |
| afl_963_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.3172 | 0.543 | 0.413 | 0.699 | -0.0715 | 0.0411 | 0.0864 | 801 | 2.2401 | 3 | 0 | 120 | 0.0869 | 357 | missingHeldBars=1587, blockedOrders=1550, blockedTradeWeight=2.0211537329251907, missingEntries=1, resumedHoldings=37, delistings=476, delistingPnl=0.014560975954652146, settledGross=0.36155929566470013, defaultDelistings=96, defaultDelistingGross=0.0862340502424199, terminalFrozenGross=0.004531836516022915, cancelledDelistingEntries=295, frozenGrossSum=2.240067072698101, retirements=801, postEndDateQuoteBars=3, successorConversions=120, convertedGross=0.08691901182891972, computedSettlementReturns=357 | 0.366 | 1102 |

## 정산 source 분포

| source | 정산 건수 | 종목 수 | Σ\|exposure\| |
| --- | --- | --- | --- |
| computed | 1828 | 368 | 1.6699 |
| default | 518 | 106 | 0.4625 |
| explicit | 115 | 23 | 0.1046 |

## 기본 R 정산 영향 (source=default를 cacheUnitStatus로 분해)

| cacheUnitStatus | 종목 수 | 알파평균 Σ\|exposure\| | 최대 | 최소 | R 0→−1 손익 스윙(평균) | 비중% |
| --- | --- | --- | --- | --- | --- | --- |
| no_successor_id | 56 | 0.0500 | 0.0570 | 0.0460 | +0.0161 | 54.0 |
| no_terms | 34 | 0.0284 | 0.0344 | 0.0241 | +0.0102 | 30.7 |
| no_cache_successor_at_settlement | 10 | 0.0098 | 0.0183 | 0.0072 | +0.0029 | 10.5 |
| rejected:cash_R_out_of_range | 3 | 0.0026 | 0.0048 | 0.0013 | +0.0019 | 2.8 |
| rejected:R_out_of_range | 2 | 0.0011 | 0.0018 | 0.0001 | +0.0004 | 1.1 |
| rejected:rename_not_1to1 | 1 | 0.0008 | 0.0016 | 0.0002 | -0.0003 | 0.8 |

- 백필 워크리스트: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f_Top1000/default_settlements_worklist.csv` (106종목)
- 정산 로그 원자료: `settlement_log.csv` (4922행)

## R 민감도 항등식 (PnL 차이 = 정산 손익 차이)

| alpha | PnL(R=0) | PnL(R=-1) | 차이 | delistingPnl 차이 | 잔차 |
| --- | --- | --- | --- | --- | --- |
| returns_zscore | -3.558502 | -3.547807 | 0.010695 | 0.010695 | 1.284e-15 |
| neg_returns_tsmean5_zscore | -0.118156 | -0.091277 | 0.026879 | 0.026879 | -1.908e-16 |
| close_tsmean20_zscore | -0.730134 | -0.692267 | 0.037867 | 0.037867 | 1.735e-16 |
| afl_100_zscore | -0.221484 | -0.153702 | 0.067782 | 0.067782 | 1.388e-17 |
| afl_963_zscore | 0.304774 | 0.317240 | 0.012466 | 0.012466 | -1.388e-16 |
