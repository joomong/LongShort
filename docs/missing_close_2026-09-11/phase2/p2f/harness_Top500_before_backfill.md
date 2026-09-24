# Top500 결측·상폐 전기간 하네스

- config: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f_Top500/config_Top500.yaml`
- server: `/tmp/longshort_module_server/build/longshort-server`
- 패널: T=4450 N=1056 2010-01-04 ~ 2026-09-11, feeRate=0.0005 barsPerYear=252
- 요청: simStart=2010-01-04 isStart=2012-01-01 osStart=2020-01-01 mode=1 missingClose=carry staleQuoteSettleBars=config
- 유니버스: Top500 (`/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f_Top500/config_Top500.yaml`)
- 결측 계약 키: ['missingClosePolicy', 'defaultDelistingReturn', 'staleQuoteSettleBars', 'eventsLoaded', 'settlementSymbols']
- 가드: config 선언=True 카운터={'eventsPriceChecks': 179, 'eventsPriceChecksOutOfRange': 0, 'eventsPriceChecksNotEvaluable': 83} 
- **exit code: 0** (all runs OK)
- 미노출 응답 키: 없음
- 데이터 로드: 3.3s, 로드 직후 RSS 284MB (peak 284MB)

| alpha | R | status | 기간 | bars | PnL | Sharpe(full) | Sharpe(IS) | Sharpe(OS) | MDD | turnover/bar | fee 합계 | retirements | frozenGrossSum | postEndDateQuoteBars | pendingSettlements | successorConversions | convertedGross | computedSettlementReturns | missingData | sec | RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| returns_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -3.4003 | -2.628 | -4.095 | -2.080 | -3.4170 | 1.4504 | 3.0443 | 262 | 0.9099 | 0 | 0 | 44 | 0.0920 | 88 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=1.1217144637873813, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=-0.0012998062389651874, settledGross=0.25605847306538326, defaultDelistings=43, defaultDelistingGross=0.0992182351364117, cancelledDelistingEntries=76, frozenGrossSum=0.9099132094376523, retirements=262, successorConversions=44, convertedGross=0.09197687627771506, computedSettlementReturns=88 | 0.236 | 374 |
| returns_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -3.3704 | -2.599 | -4.016 | -2.083 | -3.3870 | 1.4504 | 3.0443 | 262 | 0.9099 | 0 | 0 | 44 | 0.0920 | 88 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=1.1217144637873813, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=0.028665857612008436, settledGross=0.25605847306538326, defaultDelistings=43, defaultDelistingGross=0.0992182351364117, cancelledDelistingEntries=76, frozenGrossSum=0.9099132094376523, retirements=262, successorConversions=44, convertedGross=0.09197687627771506, computedSettlementReturns=88 | 0.219 | 461 |
| neg_returns_tsmean5_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.4488 | -0.329 | -1.196 | 0.048 | -0.7360 | 0.6284 | 1.3190 | 262 | 0.6195 | 0 | 0 | 44 | 0.0741 | 88 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=0.6312512496735008, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=0.0009435874883051529, settledGross=0.1995099363744602, defaultDelistings=43, defaultDelistingGross=0.0857277680958332, cancelledDelistingEntries=81, frozenGrossSum=0.6194653067271604, retirements=262, successorConversions=44, convertedGross=0.07414328025236352, computedSettlementReturns=88 | 0.211 | 461 |
| neg_returns_tsmean5_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.4192 | -0.307 | -1.135 | 0.058 | -0.7129 | 0.6284 | 1.3190 | 262 | 0.6195 | 0 | 0 | 44 | 0.0741 | 88 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=0.6312512496735008, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=0.03053643557094478, settledGross=0.1995099363744602, defaultDelistings=43, defaultDelistingGross=0.0857277680958332, cancelledDelistingEntries=81, frozenGrossSum=0.6194653067271604, retirements=262, successorConversions=44, convertedGross=0.07414328025236352, computedSettlementReturns=88 | 0.237 | 549 |
| close_tsmean20_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.5134 | -0.751 | -0.970 | -0.470 | -0.5288 | 0.0070 | 0.0147 | 262 | 0.4515 | 0 | 0 | 44 | 0.0510 | 88 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=0.30077705214326583, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=0.0006690229188913174, settledGross=0.22033096878526637, defaultDelistings=43, defaultDelistingGross=0.07439105721620849, cancelledDelistingEntries=81, frozenGrossSum=0.451534634535511, retirements=262, successorConversions=44, convertedGross=0.05097617577555802, computedSettlementReturns=88 | 0.175 | 549 |
| close_tsmean20_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.5037 | -0.733 | -0.944 | -0.449 | -0.5191 | 0.0070 | 0.0147 | 262 | 0.4515 | 0 | 0 | 44 | 0.0510 | 88 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=0.30077705214326583, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=0.01041700494070421, settledGross=0.22033096878526637, defaultDelistings=43, defaultDelistingGross=0.07439105721620849, cancelledDelistingEntries=81, frozenGrossSum=0.451534634535511, retirements=262, successorConversions=44, convertedGross=0.05097617577555802, computedSettlementReturns=88 | 0.171 | 549 |
| afl_100_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.2951 | -0.171 | -0.023 | -0.362 | -0.5685 | 0.0453 | 0.0950 | 262 | 0.4649 | 0 | 0 | 44 | 0.0426 | 88 | missingHeldBars=306, blockedOrders=270, blockedTradeWeight=0.3478992932265449, missingEntries=1, resumedHoldings=18, delistings=141, delistingPnl=-0.000132192439883651, settledGross=0.18405748136179753, defaultDelistings=42, defaultDelistingGross=0.0708782438904053, cancelledDelistingEntries=80, frozenGrossSum=0.4648680566992511, retirements=262, successorConversions=44, convertedGross=0.042615942703486974, computedSettlementReturns=88 | 0.492 | 749 |
| afl_100_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.2253 | -0.131 | 0.024 | -0.342 | -0.5377 | 0.0453 | 0.0950 | 262 | 0.4649 | 0 | 0 | 44 | 0.0426 | 88 | missingHeldBars=306, blockedOrders=270, blockedTradeWeight=0.3478992932265449, missingEntries=1, resumedHoldings=18, delistings=141, delistingPnl=0.06975590820814474, settledGross=0.18405748136179753, defaultDelistings=42, defaultDelistingGross=0.0708782438904053, cancelledDelistingEntries=80, frozenGrossSum=0.4648680566992511, retirements=262, successorConversions=44, convertedGross=0.042615942703486974, computedSettlementReturns=88 | 0.165 | 749 |
| afl_963_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.2502 | 0.391 | 0.390 | 0.422 | -0.0750 | 0.0419 | 0.0879 | 262 | 0.5298 | 0 | 0 | 43 | 0.0611 | 87 | missingHeldBars=306, blockedOrders=269, blockedTradeWeight=0.40014585477786174, missingEntries=1, resumedHoldings=18, delistings=138, delistingPnl=0.0005045623876872846, settledGross=0.18566794382094748, defaultDelistings=40, defaultDelistingGross=0.060408802849812926, cancelledDelistingEntries=77, frozenGrossSum=0.5297810595169035, retirements=262, successorConversions=43, convertedGross=0.061111498788016645, computedSettlementReturns=87 | 0.448 | 884 |
| afl_963_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.2610 | 0.405 | 0.411 | 0.430 | -0.0750 | 0.0419 | 0.0879 | 262 | 0.5298 | 0 | 0 | 43 | 0.0611 | 87 | missingHeldBars=306, blockedOrders=269, blockedTradeWeight=0.40014585477786174, missingEntries=1, resumedHoldings=18, delistings=138, delistingPnl=0.011257291504908758, settledGross=0.18566794382094748, defaultDelistings=40, defaultDelistingGross=0.060408802849812926, cancelledDelistingEntries=77, frozenGrossSum=0.5297810595169035, retirements=262, successorConversions=43, convertedGross=0.061111498788016645, computedSettlementReturns=87 | 0.168 | 884 |

## 정산 source 분포

| source | 정산 건수 | 종목 수 | Σ\|exposure\| |
| --- | --- | --- | --- |
| computed | 439 | 88 | 0.8641 |
| default | 211 | 43 | 0.3906 |
| explicit | 55 | 11 | 0.1124 |

## 기본 R 정산 영향 (source=default를 cacheUnitStatus로 분해)

| cacheUnitStatus | 종목 수 | 알파평균 Σ\|exposure\| | 최대 | 최소 | R 0→−1 손익 스윙(평균) | 비중% |
| --- | --- | --- | --- | --- | --- | --- |
| no_successor_id | 27 | 0.0506 | 0.0604 | 0.0420 | +0.0153 | 64.8 |
| no_terms | 9 | 0.0150 | 0.0202 | 0.0073 | +0.0083 | 19.2 |
| no_cache_successor_at_settlement | 5 | 0.0084 | 0.0138 | 0.0040 | +0.0040 | 10.8 |
| rejected:cash_R_out_of_range | 2 | 0.0041 | 0.0070 | 0.0022 | +0.0024 | 5.2 |

- 백필 워크리스트: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f_Top500/default_settlements_worklist.csv` (43종목)
- 정산 로그 원자료: `settlement_log.csv` (1410행)

## R 민감도 항등식 (PnL 차이 = 정산 손익 차이)

| alpha | PnL(R=0) | PnL(R=-1) | 차이 | delistingPnl 차이 | 잔차 |
| --- | --- | --- | --- | --- | --- |
| returns_zscore | -3.400321 | -3.370355 | 0.029966 | 0.029966 | 3.643e-16 |
| neg_returns_tsmean5_zscore | -0.448808 | -0.419216 | 0.029593 | 0.029593 | 2.706e-16 |
| close_tsmean20_zscore | -0.513447 | -0.503699 | 0.009748 | 0.009748 | -4.077e-16 |
| afl_100_zscore | -0.295144 | -0.225256 | 0.069888 | 0.069888 | -1.388e-16 |
| afl_963_zscore | 0.250244 | 0.260997 | 0.010753 | 0.010753 | -1.128e-16 |
