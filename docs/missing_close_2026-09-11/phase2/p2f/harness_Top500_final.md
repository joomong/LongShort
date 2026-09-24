# Top500 결측·상폐 전기간 하네스

- config: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f3_Top500/config_Top500.yaml`
- server: `/tmp/longshort_module_server/build/longshort-server`
- 패널: T=4450 N=1056 2010-01-04 ~ 2026-09-11, feeRate=0.0005 barsPerYear=252
- 요청: simStart=2010-01-04 isStart=2012-01-01 osStart=2020-01-01 mode=1 missingClose=carry staleQuoteSettleBars=config
- 유니버스: Top500 (`/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f3_Top500/config_Top500.yaml`)
- 결측 계약 키: ['missingClosePolicy', 'defaultDelistingReturn', 'staleQuoteSettleBars', 'eventsLoaded', 'settlementSymbols']
- 가드: config 선언=True 카운터={'eventsPriceChecks': 207, 'eventsPriceChecksOutOfRange': 0, 'eventsPriceChecksNotEvaluable': 55} 
- **exit code: 0** (all runs OK)
- 미노출 응답 키: 없음
- 데이터 로드: 3.2s, 로드 직후 RSS 285MB (peak 285MB)

| alpha | R | status | 기간 | bars | PnL | Sharpe(full) | Sharpe(IS) | Sharpe(OS) | MDD | turnover/bar | fee 합계 | retirements | frozenGrossSum | postEndDateQuoteBars | pendingSettlements | successorConversions | convertedGross | computedSettlementReturns | missingData | sec | RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| returns_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -3.4002 | -2.628 | -4.095 | -2.080 | -3.4168 | 1.4504 | 3.0443 | 262 | 0.9099 | 0 | 0 | 60 | 0.1262 | 105 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=1.1217144637873813, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=-0.0011339841408691733, settledGross=0.22174113463703993, defaultDelistings=21, defaultDelistingGross=0.04294392084449331, cancelledDelistingEntries=76, frozenGrossSum=0.9099132094376523, retirements=262, successorConversions=60, convertedGross=0.12617190469719186, computedSettlementReturns=105 | 0.22 | 375 |
| returns_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -3.3772 | -2.607 | -4.041 | -2.083 | -3.3939 | 1.4504 | 3.0443 | 262 | 0.9099 | 0 | 0 | 60 | 0.1262 | 105 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=1.1217144637873813, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=0.021798933022268853, settledGross=0.22174113463703993, defaultDelistings=21, defaultDelistingGross=0.04294392084449331, cancelledDelistingEntries=76, frozenGrossSum=0.9099132094376523, retirements=262, successorConversions=60, convertedGross=0.12617190469719186, computedSettlementReturns=105 | 0.195 | 375 |
| neg_returns_tsmean5_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.4493 | -0.329 | -1.196 | 0.048 | -0.7360 | 0.6284 | 1.3190 | 262 | 0.6195 | 0 | 0 | 60 | 0.1045 | 105 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=0.6312512496735008, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=0.0004631432986547097, settledGross=0.16905374826619546, defaultDelistings=21, defaultDelistingGross=0.034751752344620385, cancelledDelistingEntries=81, frozenGrossSum=0.6194653067271604, retirements=262, successorConversions=60, convertedGross=0.10449156945080994, computedSettlementReturns=105 | 0.251 | 463 |
| neg_returns_tsmean5_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.4412 | -0.323 | -1.148 | 0.041 | -0.7220 | 0.6284 | 1.3190 | 262 | 0.6195 | 0 | 0 | 60 | 0.1045 | 105 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=0.6312512496735008, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=0.008518842936927578, settledGross=0.16905374826619546, defaultDelistings=21, defaultDelistingGross=0.034751752344620385, cancelledDelistingEntries=81, frozenGrossSum=0.6194653067271604, retirements=262, successorConversions=60, convertedGross=0.10449156945080994, computedSettlementReturns=105 | 0.219 | 463 |
| close_tsmean20_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.5138 | -0.751 | -0.971 | -0.470 | -0.5292 | 0.0070 | 0.0147 | 262 | 0.4515 | 0 | 0 | 60 | 0.0724 | 105 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=0.30077705214326583, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=0.0003076573869614042, settledGross=0.19879983986535235, defaultDelistings=21, defaultDelistingGross=0.036115978940613074, cancelledDelistingEntries=81, frozenGrossSum=0.451534634535511, retirements=262, successorConversions=60, convertedGross=0.07238680018681919, computedSettlementReturns=105 | 0.179 | 463 |
| close_tsmean20_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.5134 | -0.749 | -0.956 | -0.467 | -0.5288 | 0.0070 | 0.0147 | 262 | 0.4515 | 0 | 0 | 60 | 0.0724 | 105 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=0.30077705214326583, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=0.0007129927819575709, settledGross=0.19879983986535235, defaultDelistings=21, defaultDelistingGross=0.036115978940613074, cancelledDelistingEntries=81, frozenGrossSum=0.451534634535511, retirements=262, successorConversions=60, convertedGross=0.07238680018681919, computedSettlementReturns=105 | 0.181 | 463 |
| afl_100_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.2952 | -0.171 | -0.023 | -0.362 | -0.5687 | 0.0453 | 0.0950 | 262 | 0.4649 | 0 | 0 | 60 | 0.0617 | 105 | missingHeldBars=306, blockedOrders=270, blockedTradeWeight=0.3478992932265449, missingEntries=1, resumedHoldings=18, delistings=141, delistingPnl=-0.00021880177444872387, settledGross=0.1649126163580234, defaultDelistings=20, defaultDelistingGross=0.0338711635058942, cancelledDelistingEntries=80, frozenGrossSum=0.4648680566992511, retirements=262, successorConversions=60, convertedGross=0.06174588458135399, computedSettlementReturns=105 | 0.484 | 664 |
| afl_100_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.2614 | -0.151 | 0.001 | -0.355 | -0.5601 | 0.0453 | 0.0950 | 262 | 0.4649 | 0 | 0 | 60 | 0.0617 | 105 | missingHeldBars=306, blockedOrders=270, blockedTradeWeight=0.3478992932265449, missingEntries=1, resumedHoldings=18, delistings=141, delistingPnl=0.03365236173144547, settledGross=0.1649126163580234, defaultDelistings=20, defaultDelistingGross=0.0338711635058942, cancelledDelistingEntries=80, frozenGrossSum=0.4648680566992511, retirements=262, successorConversions=60, convertedGross=0.06174588458135399, computedSettlementReturns=105 | 0.162 | 664 |
| afl_963_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.2501 | 0.390 | 0.389 | 0.422 | -0.0750 | 0.0419 | 0.0879 | 262 | 0.5298 | 0 | 0 | 59 | 0.0807 | 104 | missingHeldBars=306, blockedOrders=269, blockedTradeWeight=0.40014585477786174, missingEntries=1, resumedHoldings=18, delistings=138, delistingPnl=0.00041989131538965513, settledGross=0.16595729636119244, defaultDelistings=20, defaultDelistingGross=0.02586238994671548, cancelledDelistingEntries=77, frozenGrossSum=0.5297810595169035, retirements=262, successorConversions=59, convertedGross=0.08073666640553874, computedSettlementReturns=104 | 0.424 | 689 |
| afl_963_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.2599 | 0.404 | 0.409 | 0.434 | -0.0750 | 0.0419 | 0.0879 | 262 | 0.5298 | 0 | 0 | 59 | 0.0807 | 104 | missingHeldBars=306, blockedOrders=269, blockedTradeWeight=0.40014585477786174, missingEntries=1, resumedHoldings=18, delistings=138, delistingPnl=0.01013069880226263, settledGross=0.16595729636119244, defaultDelistings=20, defaultDelistingGross=0.02586238994671548, cancelledDelistingEntries=77, frozenGrossSum=0.5297810595169035, retirements=262, successorConversions=59, convertedGross=0.08073666640553874, computedSettlementReturns=104 | 0.167 | 689 |

## 정산 source 분포

| source | 정산 건수 | 종목 수 | Σ\|exposure\| |
| --- | --- | --- | --- |
| computed | 524 | 105 | 1.0392 |
| default | 103 | 21 | 0.1735 |
| explicit | 78 | 16 | 0.1544 |

## 기본 R 정산 영향 (source=default를 cacheUnitStatus로 분해)

| cacheUnitStatus | 종목 수 | 알파평균 Σ\|exposure\| | 최대 | 최소 | R 0→−1 손익 스윙(평균) | 비중% |
| --- | --- | --- | --- | --- | --- | --- |
| no_ratio | 14 | 0.0235 | 0.0269 | 0.0161 | +0.0110 | 67.8 |
| no_cache_successor_at_settlement | 4 | 0.0070 | 0.0117 | 0.0043 | +0.0029 | 20.0 |
| no_terms | 3 | 0.0042 | 0.0075 | 0.0009 | +0.0011 | 12.2 |

- 백필 워크리스트: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f3_Top500/default_settlements_worklist.csv` (21종목)
- 정산 로그 원자료: `settlement_log.csv` (1410행)

## R 민감도 항등식 (PnL 차이 = 정산 손익 차이)

| alpha | PnL(R=0) | PnL(R=-1) | 차이 | delistingPnl 차이 | 잔차 |
| --- | --- | --- | --- | --- | --- |
| returns_zscore | -3.400155 | -3.377222 | 0.022933 | 0.022933 | 2.498e-16 |
| neg_returns_tsmean5_zscore | -0.449297 | -0.441242 | 0.008056 | 0.008056 | -7.286e-17 |
| close_tsmean20_zscore | -0.513819 | -0.513414 | 0.000405 | 0.000405 | -4.018e-16 |
| afl_100_zscore | -0.295239 | -0.261368 | 0.033871 | 0.033871 | -5.551e-17 |
| afl_963_zscore | 0.250150 | 0.259861 | 0.009711 | 0.009711 | -5.551e-17 |
