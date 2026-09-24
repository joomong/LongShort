# Top500 결측·상폐 전기간 하네스

- config: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f3_Top1000/config_Top1000.yaml`
- server: `/tmp/longshort_module_server/build/longshort-server`
- 패널: T=4450 N=2315 2010-01-04 ~ 2026-09-11, feeRate=0.0005 barsPerYear=252
- 요청: simStart=2010-01-04 isStart=2012-01-01 osStart=2020-01-01 mode=1 missingClose=carry staleQuoteSettleBars=config
- 유니버스: Top1000 (`/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f3_Top1000/config_Top1000.yaml`)
- 결측 계약 키: ['missingClosePolicy', 'defaultDelistingReturn', 'staleQuoteSettleBars', 'eventsLoaded', 'settlementSymbols']
- 가드: config 선언=True 카운터={'eventsPriceChecks': 635, 'eventsPriceChecksOutOfRange': 0, 'eventsPriceChecksNotEvaluable': 166} 
- **exit code: 0** (all runs OK)
- 미노출 응답 키: 없음
- 데이터 로드: 3.8s, 로드 직후 RSS 419MB (peak 419MB)

| alpha | R | status | 기간 | bars | PnL | Sharpe(full) | Sharpe(IS) | Sharpe(OS) | MDD | turnover/bar | fee 합계 | retirements | frozenGrossSum | postEndDateQuoteBars | pendingSettlements | successorConversions | convertedGross | computedSettlementReturns | missingData | sec | RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| returns_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -3.5594 | -2.787 | -4.206 | -2.258 | -3.5731 | 1.4493 | 3.0421 | 801 | 3.4707 | 3 | 0 | 150 | 0.1702 | 395 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=3.6858100588719864, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=0.0014690113014550613, settledGross=0.3416541636441332, defaultDelistings=71, defaultDelistingGross=0.05792397422379724, terminalFrozenGross=0.01317473096700952, cancelledDelistingEntries=283, frozenGrossSum=3.470740009214544, retirements=801, postEndDateQuoteBars=3, successorConversions=150, convertedGross=0.17019040711501954, computedSettlementReturns=395 | 0.45 | 532 |
| returns_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -3.5518 | -2.779 | -4.170 | -2.262 | -3.5649 | 1.4493 | 3.0421 | 801 | 3.4707 | 3 | 0 | 150 | 0.1702 | 395 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=3.6858100588719864, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=0.009054506634889516, settledGross=0.3416541636441332, defaultDelistings=71, defaultDelistingGross=0.05792397422379724, terminalFrozenGross=0.01317473096700952, cancelledDelistingEntries=283, frozenGrossSum=3.470740009214544, retirements=801, postEndDateQuoteBars=3, successorConversions=150, convertedGross=0.17019040711501954, computedSettlementReturns=395 | 0.465 | 644 |
| neg_returns_tsmean5_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.1178 | -0.086 | -1.072 | 0.371 | -0.6614 | 0.6265 | 1.3149 | 801 | 2.6338 | 3 | 0 | 150 | 0.1384 | 395 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=2.5952328104677265, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=0.0024500836479645115, settledGross=0.30470622426797606, defaultDelistings=71, defaultDelistingGross=0.04864442950989298, terminalFrozenGross=0.009132112576565888, cancelledDelistingEntries=308, frozenGrossSum=2.633812867367029, retirements=801, postEndDateQuoteBars=3, successorConversions=150, convertedGross=0.13844405933684956, computedSettlementReturns=395 | 0.505 | 756 |
| neg_returns_tsmean5_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.1058 | -0.078 | -1.035 | 0.371 | -0.6493 | 0.6265 | 1.3149 | 801 | 2.6338 | 3 | 0 | 150 | 0.1384 | 395 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=2.5952328104677265, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=0.014390375216864697, settledGross=0.30470622426797606, defaultDelistings=71, defaultDelistingGross=0.04864442950989298, terminalFrozenGross=0.009132112576565888, cancelledDelistingEntries=308, frozenGrossSum=2.633812867367029, retirements=801, postEndDateQuoteBars=3, successorConversions=150, convertedGross=0.13844405933684956, computedSettlementReturns=395 | 0.481 | 756 |
| close_tsmean20_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.7307 | -0.866 | -1.268 | -0.497 | -0.7763 | 0.0071 | 0.0150 | 801 | 1.6996 | 3 | 0 | 150 | 0.0965 | 395 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=1.4707681266358337, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=-0.0006327911439117927, settledGross=0.3385138085549515, defaultDelistings=71, defaultDelistingGross=0.0547124127382446, terminalFrozenGross=0.0036482930567070314, cancelledDelistingEntries=308, frozenGrossSum=1.6995768948780063, retirements=801, postEndDateQuoteBars=3, successorConversions=150, convertedGross=0.09648858565473865, computedSettlementReturns=395 | 0.401 | 757 |
| close_tsmean20_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.7028 | -0.833 | -1.232 | -0.453 | -0.7485 | 0.0071 | 0.0150 | 801 | 1.6996 | 3 | 0 | 150 | 0.0965 | 395 | missingHeldBars=2408, blockedOrders=2409, blockedTradeWeight=1.4707681266358337, missingEntries=1, resumedHoldings=43, delistings=497, delistingPnl=0.027232440374281113, settledGross=0.3385138085549515, defaultDelistings=71, defaultDelistingGross=0.0547124127382446, terminalFrozenGross=0.0036482930567070314, cancelledDelistingEntries=308, frozenGrossSum=1.6995768948780063, retirements=801, postEndDateQuoteBars=3, successorConversions=150, convertedGross=0.09648858565473865, computedSettlementReturns=395 | 0.392 | 757 |
| afl_100_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.2218 | -0.123 | -0.133 | -0.167 | -0.3994 | 0.0463 | 0.0973 | 801 | 1.2850 | 3 | 0 | 149 | 0.0808 | 394 | missingHeldBars=1673, blockedOrders=1637, blockedTradeWeight=1.1017974180397687, missingEntries=1, resumedHoldings=41, delistings=494, delistingPnl=-0.0005422606693080997, settledGross=0.31491606004191386, defaultDelistings=69, defaultDelistingGross=0.05722981625377902, terminalFrozenGross=0.0024575988646810313, cancelledDelistingEntries=307, frozenGrossSum=1.285033798581024, retirements=801, postEndDateQuoteBars=3, successorConversions=149, convertedGross=0.08075582905175295, computedSettlementReturns=394 | 0.673 | 866 |
| afl_100_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.1780 | -0.098 | -0.109 | -0.147 | -0.3944 | 0.0463 | 0.0973 | 801 | 1.2850 | 3 | 0 | 149 | 0.0808 | 394 | missingHeldBars=1673, blockedOrders=1637, blockedTradeWeight=1.1017974180397687, missingEntries=1, resumedHoldings=41, delistings=494, delistingPnl=0.04322236639054189, settledGross=0.31491606004191386, defaultDelistings=69, defaultDelistingGross=0.05722981625377902, terminalFrozenGross=0.0024575988646810313, cancelledDelistingEntries=307, frozenGrossSum=1.285033798581024, retirements=801, postEndDateQuoteBars=3, successorConversions=149, convertedGross=0.08075582905175295, computedSettlementReturns=394 | 0.349 | 866 |
| afl_963_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.3052 | 0.523 | 0.415 | 0.667 | -0.0687 | 0.0412 | 0.0864 | 801 | 2.2401 | 3 | 0 | 144 | 0.1056 | 384 | missingHeldBars=1587, blockedOrders=1550, blockedTradeWeight=2.0211537329251907, missingEntries=1, resumedHoldings=37, delistings=476, delistingPnl=0.002569115357208743, settledGross=0.3429142557816921, defaultDelistings=64, defaultDelistingGross=0.04891791352688819, terminalFrozenGross=0.004531836516022915, cancelledDelistingEntries=295, frozenGrossSum=2.240067072698101, retirements=801, postEndDateQuoteBars=3, successorConversions=144, convertedGross=0.10564943126183833, computedSettlementReturns=384 | 0.661 | 913 |
| afl_963_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.3190 | 0.546 | 0.439 | 0.690 | -0.0687 | 0.0412 | 0.0864 | 801 | 2.2401 | 3 | 0 | 144 | 0.1056 | 384 | missingHeldBars=1587, blockedOrders=1550, blockedTradeWeight=2.0211537329251907, missingEntries=1, resumedHoldings=37, delistings=476, delistingPnl=0.016359025960046812, settledGross=0.3429142557816921, defaultDelistings=64, defaultDelistingGross=0.04891791352688819, terminalFrozenGross=0.004531836516022915, cancelledDelistingEntries=295, frozenGrossSum=2.240067072698101, retirements=801, postEndDateQuoteBars=3, successorConversions=144, convertedGross=0.10564943126183833, computedSettlementReturns=384 | 0.363 | 916 |

## 정산 source 분포

| source | 정산 건수 | 종목 수 | Σ\|exposure\| |
| --- | --- | --- | --- |
| computed | 1963 | 395 | 1.8192 |
| default | 346 | 71 | 0.2674 |
| explicit | 152 | 31 | 0.1505 |

## 기본 R 정산 영향 (source=default를 cacheUnitStatus로 분해)

| cacheUnitStatus | 종목 수 | 알파평균 Σ\|exposure\| | 최대 | 최소 | R 0→−1 손익 스윙(평균) | 비중% |
| --- | --- | --- | --- | --- | --- | --- |
| no_ratio | 35 | 0.0263 | 0.0295 | 0.0226 | +0.0110 | 49.0 |
| no_terms | 21 | 0.0138 | 0.0174 | 0.0098 | +0.0062 | 25.7 |
| no_cache_successor_at_settlement | 7 | 0.0072 | 0.0131 | 0.0040 | +0.0010 | 13.5 |
| rejected:R_out_of_range | 3 | 0.0026 | 0.0060 | 0.0003 | +0.0003 | 4.8 |
| no_successor_id | 3 | 0.0022 | 0.0041 | 0.0003 | +0.0019 | 4.2 |
| rejected:cash_R_out_of_range | 1 | 0.0008 | 0.0015 | 0.0004 | +0.0008 | 1.5 |
| rejected:rename_not_1to1 | 1 | 0.0008 | 0.0016 | 0.0002 | -0.0003 | 1.4 |

- 백필 워크리스트: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f3_Top1000/default_settlements_worklist.csv` (71종목)
- 정산 로그 원자료: `settlement_log.csv` (4922행)

## R 민감도 항등식 (PnL 차이 = 정산 손익 차이)

| alpha | PnL(R=0) | PnL(R=-1) | 차이 | delistingPnl 차이 | 잔차 |
| --- | --- | --- | --- | --- | --- |
| returns_zscore | -3.559410 | -3.551824 | 0.007585 | 0.007585 | -1.041e-17 |
| neg_returns_tsmean5_zscore | -0.117769 | -0.105829 | 0.011940 | 0.011940 | -7.286e-17 |
| close_tsmean20_zscore | -0.730687 | -0.702822 | 0.027865 | 0.027865 | 2.949e-16 |
| afl_100_zscore | -0.221811 | -0.178047 | 0.043765 | 0.043765 | 1.110e-16 |
| afl_963_zscore | 0.305238 | 0.319028 | 0.013790 | 0.013790 | -3.296e-16 |
