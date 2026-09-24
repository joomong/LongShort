# Top500 결측·상폐 전기간 하네스

- config: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2g3_Top1000/config_Top1000.yaml`
- server: `/tmp/longshort_module_server/build/longshort-server`
- 패널: T=4450 N=2315 2010-01-04 ~ 2026-09-11, feeRate=0.0005 barsPerYear=252
- 요청: simStart=2010-01-04 isStart=2012-01-01 osStart=2020-01-01 mode=1 missingClose=carry staleQuoteSettleBars=config
- 유니버스: Top1000 (`/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2g3_Top1000/config_Top1000.yaml`)
- 결측 계약 키: ['missingClosePolicy', 'defaultDelistingReturn', 'staleQuoteSettleBars', 'eventsLoaded', 'settlementSymbols']
- 가드: config 선언=True 카운터={'eventsPriceChecks': 646, 'eventsPriceChecksOutOfRange': 0, 'eventsPriceChecksNotEvaluable': 155} 
- **exit code: 0** (all runs OK)
- 미노출 응답 키: 없음
- 데이터 로드: 4.2s, 로드 직후 RSS 419MB (peak 419MB)

| alpha | R | status | 기간 | bars | PnL | Sharpe(full) | Sharpe(IS) | Sharpe(OS) | MDD | turnover/bar | fee 합계 | retirements | frozenGrossSum | postEndDateQuoteBars | pendingSettlements | successorConversions | convertedGross | computedSettlementReturns | missingData | sec | RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| returns_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -3.5587 | -2.786 | -4.205 | -2.257 | -3.5724 | 1.4495 | 3.0425 | 801 | 2.7734 | 3 | 0 | 157 | 0.1721 | 405 | missingHeldBars=1784, blockedOrders=1784, blockedTradeWeight=2.850935829136987, resumedHoldings=43, delistings=497, delistingPnl=0.0028945685967350595, settledGross=0.3404503522499534, defaultDelistings=56, defaultDelistingGross=0.05063885173916051, terminalFrozenGross=0.01317473096700952, cancelledDelistingEntries=468, frozenGrossSum=2.773425529861237, retirements=801, postEndDateQuoteBars=3, successorConversions=157, convertedGross=0.17213462697162907, computedSettlementReturns=405 | 0.499 | 533 |
| returns_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -3.5535 | -2.782 | -4.186 | -2.253 | -3.5666 | 1.4495 | 3.0425 | 801 | 2.7734 | 3 | 0 | 157 | 0.1721 | 405 | missingHeldBars=1784, blockedOrders=1784, blockedTradeWeight=2.850935829136987, resumedHoldings=43, delistings=497, delistingPnl=0.008062648679913798, settledGross=0.3404503522499534, defaultDelistings=56, defaultDelistingGross=0.05063885173916051, terminalFrozenGross=0.01317473096700952, cancelledDelistingEntries=468, frozenGrossSum=2.773425529861237, retirements=801, postEndDateQuoteBars=3, successorConversions=157, convertedGross=0.17213462697162907, computedSettlementReturns=405 | 0.522 | 645 |
| neg_returns_tsmean5_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.1182 | -0.087 | -1.072 | 0.371 | -0.6617 | 0.6265 | 1.3151 | 801 | 2.1269 | 3 | 0 | 157 | 0.1398 | 405 | missingHeldBars=1784, blockedOrders=1784, blockedTradeWeight=2.0987387851410833, resumedHoldings=43, delistings=497, delistingPnl=0.001974210758031866, settledGross=0.303848466848695, defaultDelistings=56, defaultDelistingGross=0.04212472283814218, terminalFrozenGross=0.009132112576565888, cancelledDelistingEntries=469, frozenGrossSum=2.126904157623986, retirements=801, postEndDateQuoteBars=3, successorConversions=157, convertedGross=0.139772110786498, computedSettlementReturns=405 | 0.53 | 756 |
| neg_returns_tsmean5_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.1068 | -0.078 | -1.040 | 0.370 | -0.6503 | 0.6265 | 1.3151 | 801 | 2.1269 | 3 | 0 | 157 | 0.1398 | 405 | missingHeldBars=1784, blockedOrders=1784, blockedTradeWeight=2.0987387851410833, resumedHoldings=43, delistings=497, delistingPnl=0.013356559392849108, settledGross=0.303848466848695, defaultDelistings=56, defaultDelistingGross=0.04212472283814218, terminalFrozenGross=0.009132112576565888, cancelledDelistingEntries=469, frozenGrossSum=2.126904157623986, retirements=801, postEndDateQuoteBars=3, successorConversions=157, convertedGross=0.139772110786498, computedSettlementReturns=405 | 0.479 | 756 |
| close_tsmean20_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.7307 | -0.866 | -1.268 | -0.497 | -0.7763 | 0.0071 | 0.0150 | 801 | 1.2966 | 3 | 0 | 157 | 0.0999 | 405 | missingHeldBars=1784, blockedOrders=1784, blockedTradeWeight=1.2430253734435681, resumedHoldings=43, delistings=497, delistingPnl=-0.000612981154175527, settledGross=0.33520308168200114, defaultDelistings=56, defaultDelistingGross=0.044664335541757715, terminalFrozenGross=0.0036482930567070314, cancelledDelistingEntries=469, frozenGrossSum=1.2965846116023263, retirements=801, postEndDateQuoteBars=3, successorConversions=157, convertedGross=0.09994207054343161, computedSettlementReturns=405 | 0.407 | 756 |
| close_tsmean20_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.7000 | -0.830 | -1.217 | -0.455 | -0.7456 | 0.0071 | 0.0150 | 801 | 1.2966 | 3 | 0 | 157 | 0.0999 | 405 | missingHeldBars=1784, blockedOrders=1784, blockedTradeWeight=1.2430253734435681, resumedHoldings=43, delistings=497, delistingPnl=0.03009526899144612, settledGross=0.33520308168200114, defaultDelistings=56, defaultDelistingGross=0.044664335541757715, terminalFrozenGross=0.0036482930567070314, cancelledDelistingEntries=469, frozenGrossSum=1.2965846116023263, retirements=801, postEndDateQuoteBars=3, successorConversions=157, convertedGross=0.09994207054343161, computedSettlementReturns=405 | 0.406 | 756 |
| afl_100_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.2219 | -0.123 | -0.133 | -0.168 | -0.3997 | 0.0463 | 0.0973 | 801 | 0.8257 | 3 | 0 | 156 | 0.0849 | 404 | missingHeldBars=1049, blockedOrders=1044, blockedTradeWeight=0.7960819382599165, resumedHoldings=41, delistings=494, delistingPnl=-0.0006189187748756208, settledGross=0.31106528808046924, defaultDelistings=54, defaultDelistingGross=0.04080323979811969, terminalFrozenGross=0.0024575988646810313, cancelledDelistingEntries=468, frozenGrossSum=0.8257233310021437, retirements=801, postEndDateQuoteBars=3, successorConversions=156, convertedGross=0.0848722182407514, computedSettlementReturns=404 | 0.736 | 979 |
| afl_100_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.1862 | -0.103 | -0.118 | -0.146 | -0.3939 | 0.0463 | 0.0973 | 801 | 0.8257 | 3 | 0 | 156 | 0.0849 | 404 | missingHeldBars=1049, blockedOrders=1044, blockedTradeWeight=0.7960819382599165, resumedHoldings=41, delistings=494, delistingPnl=0.03507554590490575, settledGross=0.31106528808046924, defaultDelistings=54, defaultDelistingGross=0.04080323979811969, terminalFrozenGross=0.0024575988646810313, cancelledDelistingEntries=468, frozenGrossSum=0.8257233310021437, retirements=801, postEndDateQuoteBars=3, successorConversions=156, convertedGross=0.0848722182407514, computedSettlementReturns=404 | 0.378 | 983 |
| afl_963_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.3049 | 0.522 | 0.414 | 0.666 | -0.0687 | 0.0412 | 0.0864 | 801 | 1.6521 | 3 | 0 | 151 | 0.1094 | 394 | missingHeldBars=969, blockedOrders=964, blockedTradeWeight=1.6182304937123546, resumedHoldings=37, delistings=476, delistingPnl=0.0022172868054768403, settledGross=0.33926335913930783, defaultDelistings=48, defaultDelistingGross=0.03229027316829746, terminalFrozenGross=0.004531836516022915, cancelledDelistingEntries=451, frozenGrossSum=1.6521142494265106, retirements=801, postEndDateQuoteBars=3, successorConversions=151, convertedGross=0.10942024224271918, computedSettlementReturns=394 | 0.683 | 1029 |
| afl_963_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.3079 | 0.527 | 0.438 | 0.660 | -0.0687 | 0.0412 | 0.0864 | 801 | 1.6521 | 3 | 0 | 151 | 0.1094 | 394 | missingHeldBars=969, blockedOrders=964, blockedTradeWeight=1.6182304937123546, resumedHoldings=37, delistings=476, delistingPnl=0.005181932253185929, settledGross=0.33926335913930783, defaultDelistings=48, defaultDelistingGross=0.03229027316829746, terminalFrozenGross=0.004531836516022915, cancelledDelistingEntries=451, frozenGrossSum=1.6521142494265106, retirements=801, postEndDateQuoteBars=3, successorConversions=151, convertedGross=0.10942024224271918, computedSettlementReturns=394 | 0.391 | 1033 |

## 정산 source 분포

| source | 정산 건수 | 종목 수 | Σ\|exposure\| |
| --- | --- | --- | --- |
| computed | 2013 | 405 | 1.8527 |
| default | 270 | 56 | 0.2105 |
| explicit | 178 | 36 | 0.1742 |

## 기본 R 정산 영향 (source=default를 cacheUnitStatus로 분해)

| cacheUnitStatus | 종목 수 | 알파평균 Σ\|exposure\| | 최대 | 최소 | R 0→−1 손익 스윙(평균) | 비중% |
| --- | --- | --- | --- | --- | --- | --- |
| no_ratio | 23 | 0.0171 | 0.0194 | 0.0142 | +0.0074 | 40.4 |
| no_terms | 20 | 0.0132 | 0.0172 | 0.0095 | +0.0064 | 31.1 |
| no_cache_successor_at_settlement | 7 | 0.0081 | 0.0148 | 0.0044 | +0.0014 | 19.2 |
| no_successor_id | 2 | 0.0013 | 0.0020 | 0.0003 | +0.0011 | 3.2 |
| rejected:R_out_of_range | 2 | 0.0011 | 0.0018 | 0.0001 | +0.0004 | 2.5 |
| rejected:cash_R_out_of_range | 1 | 0.0008 | 0.0015 | 0.0004 | +0.0008 | 1.9 |
| rejected:rename_not_1to1 | 1 | 0.0008 | 0.0016 | 0.0002 | -0.0003 | 1.8 |

- 백필 워크리스트: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2g3_Top1000/default_settlements_worklist.csv` (56종목)
- 정산 로그 원자료: `settlement_log.csv` (4922행)

## R 민감도 항등식 (PnL 차이 = 정산 손익 차이)

| alpha | PnL(R=0) | PnL(R=-1) | 차이 | delistingPnl 차이 | 잔차 |
| --- | --- | --- | --- | --- | --- |
| returns_zscore | -3.558698 | -3.553530 | 0.005168 | 0.005168 | 7.442e-16 |
| neg_returns_tsmean5_zscore | -0.118231 | -0.106849 | 0.011382 | 0.011382 | -1.527e-16 |
| close_tsmean20_zscore | -0.730685 | -0.699977 | 0.030708 | 0.030708 | 1.908e-16 |
| afl_100_zscore | -0.221905 | -0.186210 | 0.035694 | 0.035694 | 2.914e-16 |
| afl_963_zscore | 0.304925 | 0.307889 | 0.002965 | 0.002965 | 1.023e-16 |
