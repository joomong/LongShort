# Top500 결측·상폐 전기간 하네스

- config: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2g3_Top500/config_Top500.yaml`
- server: `/tmp/longshort_module_server/build/longshort-server`
- 패널: T=4450 N=1056 2010-01-04 ~ 2026-09-11, feeRate=0.0005 barsPerYear=252
- 요청: simStart=2010-01-04 isStart=2012-01-01 osStart=2020-01-01 mode=1 missingClose=carry staleQuoteSettleBars=config
- 유니버스: Top500 (`/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2g3_Top500/config_Top500.yaml`)
- 결측 계약 키: ['missingClosePolicy', 'defaultDelistingReturn', 'staleQuoteSettleBars', 'eventsLoaded', 'settlementSymbols']
- 가드: config 선언=True 카운터={'eventsPriceChecks': 218, 'eventsPriceChecksOutOfRange': 0, 'eventsPriceChecksNotEvaluable': 44} 
- **exit code: 0** (all runs OK)
- 미노출 응답 키: 없음
- 데이터 로드: 3.4s, 로드 직후 RSS 284MB (peak 284MB)

| alpha | R | status | 기간 | bars | PnL | Sharpe(full) | Sharpe(IS) | Sharpe(OS) | MDD | turnover/bar | fee 합계 | retirements | frozenGrossSum | postEndDateQuoteBars | pendingSettlements | successorConversions | convertedGross | computedSettlementReturns | missingData | sec | RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| returns_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -3.3996 | -2.627 | -4.092 | -2.079 | -3.4162 | 1.4506 | 3.0448 | 262 | 0.1924 | 1 | 0 | 68 | 0.1354 | 116 | missingHeldBars=30, blockedOrders=30, blockedTradeWeight=0.2590408567585716, resumedHoldings=18, delistings=142, delistingPnl=-0.00031527125297019183, settledGross=0.212862951044262, defaultDelistings=7, defaultDelistingGross=0.018020438364738947, cancelledDelistingEntries=131, frozenGrossSum=0.19244499728518444, retirements=262, postEndDateQuoteBars=1, successorConversions=68, convertedGross=0.1354468741746195, computedSettlementReturns=116 | 0.245 | 374 |
| returns_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -3.3903 | -2.620 | -4.074 | -2.079 | -3.4069 | 1.4506 | 3.0448 | 262 | 0.1924 | 1 | 0 | 68 | 0.1354 | 116 | missingHeldBars=30, blockedOrders=30, blockedTradeWeight=0.2590408567585716, resumedHoldings=18, delistings=142, delistingPnl=0.009003700641534265, settledGross=0.212862951044262, defaultDelistings=7, defaultDelistingGross=0.018020438364738947, cancelledDelistingEntries=131, frozenGrossSum=0.19244499728518444, retirements=262, postEndDateQuoteBars=1, successorConversions=68, convertedGross=0.1354468741746195, computedSettlementReturns=116 | 0.232 | 461 |
| neg_returns_tsmean5_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.4503 | -0.330 | -1.197 | 0.047 | -0.7363 | 0.6285 | 1.3192 | 262 | 0.1073 | 1 | 0 | 68 | 0.1129 | 116 | missingHeldBars=30, blockedOrders=30, blockedTradeWeight=0.07708089776634648, resumedHoldings=18, delistings=142, delistingPnl=-0.0003291990627801895, settledGross=0.16113542423989977, defaultDelistings=7, defaultDelistingGross=0.013338879954007612, cancelledDelistingEntries=131, frozenGrossSum=0.10727102893495466, retirements=262, postEndDateQuoteBars=1, successorConversions=68, convertedGross=0.11286234634937221, computedSettlementReturns=116 | 0.273 | 549 |
| neg_returns_tsmean5_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.4408 | -0.323 | -1.166 | 0.047 | -0.7269 | 0.6285 | 1.3192 | 262 | 0.1073 | 1 | 0 | 68 | 0.1129 | 116 | missingHeldBars=30, blockedOrders=30, blockedTradeWeight=0.07708089776634648, resumedHoldings=18, delistings=142, delistingPnl=0.009156180376989728, settledGross=0.16113542423989977, defaultDelistings=7, defaultDelistingGross=0.013338879954007612, cancelledDelistingEntries=131, frozenGrossSum=0.10727102893495466, retirements=262, postEndDateQuoteBars=1, successorConversions=68, convertedGross=0.11286234634937221, computedSettlementReturns=116 | 0.236 | 549 |
| close_tsmean20_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.5138 | -0.751 | -0.971 | -0.470 | -0.5292 | 0.0070 | 0.0147 | 262 | 0.0481 | 1 | 0 | 68 | 0.0818 | 116 | missingHeldBars=30, blockedOrders=30, blockedTradeWeight=0.0067585869852353955, resumedHoldings=18, delistings=142, delistingPnl=0.00029329487783620996, settledGross=0.1893476335859344, defaultDelistings=7, defaultDelistingGross=0.012187262446342424, cancelledDelistingEntries=131, frozenGrossSum=0.048129871986611936, retirements=262, postEndDateQuoteBars=1, successorConversions=68, convertedGross=0.08182515791753776, computedSettlementReturns=116 | 0.214 | 638 |
| close_tsmean20_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.5051 | -0.738 | -0.938 | -0.470 | -0.5204 | 0.0070 | 0.0147 | 262 | 0.0481 | 1 | 0 | 68 | 0.0818 | 116 | missingHeldBars=30, blockedOrders=30, blockedTradeWeight=0.0067585869852353955, resumedHoldings=18, delistings=142, delistingPnl=0.00906418918490701, settledGross=0.1893476335859344, defaultDelistings=7, defaultDelistingGross=0.012187262446342424, cancelledDelistingEntries=131, frozenGrossSum=0.048129871986611936, retirements=262, postEndDateQuoteBars=1, successorConversions=68, convertedGross=0.08182515791753776, computedSettlementReturns=116 | 0.186 | 638 |
| afl_100_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.2955 | -0.171 | -0.023 | -0.362 | -0.5689 | 0.0453 | 0.0950 | 262 | 0.0577 | 1 | 0 | 68 | 0.0715 | 116 | missingHeldBars=30, blockedOrders=20, blockedTradeWeight=0.0341143677951154, resumedHoldings=18, delistings=141, delistingPnl=-0.0004597222799988862, settledGross=0.15532458244588268, defaultDelistings=6, defaultDelistingGross=0.007721338766213423, cancelledDelistingEntries=130, frozenGrossSum=0.05769281748526743, retirements=262, postEndDateQuoteBars=1, successorConversions=68, convertedGross=0.07154265173868155, computedSettlementReturns=116 | 0.496 | 749 |
| afl_100_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.2877 | -0.167 | -0.015 | -0.362 | -0.5672 | 0.0453 | 0.0950 | 262 | 0.0577 | 1 | 0 | 68 | 0.0715 | 116 | missingHeldBars=30, blockedOrders=20, blockedTradeWeight=0.0341143677951154, resumedHoldings=18, delistings=141, delistingPnl=0.00726161648621454, settledGross=0.15532458244588268, defaultDelistings=6, defaultDelistingGross=0.007721338766213423, cancelledDelistingEntries=130, frozenGrossSum=0.05769281748526743, retirements=262, postEndDateQuoteBars=1, successorConversions=68, convertedGross=0.07154265173868155, computedSettlementReturns=116 | 0.174 | 749 |
| afl_963_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.2502 | 0.390 | 0.388 | 0.423 | -0.0750 | 0.0419 | 0.0879 | 262 | 0.0558 | 1 | 0 | 67 | 0.0897 | 115 | missingHeldBars=30, blockedOrders=20, blockedTradeWeight=0.019504162582531744, resumedHoldings=18, delistings=138, delistingPnl=0.0005065153252676902, settledGross=0.15708302196869012, defaultDelistings=6, defaultDelistingGross=0.005189223002978884, cancelledDelistingEntries=127, frozenGrossSum=0.05576613980073941, retirements=262, postEndDateQuoteBars=1, successorConversions=67, convertedGross=0.08971792705896389, computedSettlementReturns=115 | 0.464 | 887 |
| afl_963_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.2510 | 0.392 | 0.386 | 0.423 | -0.0750 | 0.0419 | 0.0879 | 262 | 0.0558 | 1 | 0 | 67 | 0.0897 | 115 | missingHeldBars=30, blockedOrders=20, blockedTradeWeight=0.019504162582531744, resumedHoldings=18, delistings=138, delistingPnl=0.001269754460982114, settledGross=0.15708302196869012, defaultDelistings=6, defaultDelistingGross=0.005189223002978884, cancelledDelistingEntries=127, frozenGrossSum=0.05576613980073941, retirements=262, postEndDateQuoteBars=1, successorConversions=67, convertedGross=0.08971792705896389, computedSettlementReturns=115 | 0.177 | 887 |

## 정산 source 분포

| source | 정산 건수 | 종목 수 | Σ\|exposure\| |
| --- | --- | --- | --- |
| computed | 579 | 116 | 1.1301 |
| explicit | 93 | 19 | 0.1807 |
| default | 33 | 7 | 0.0565 |

## 기본 R 정산 영향 (source=default를 cacheUnitStatus로 분해)

| cacheUnitStatus | 종목 수 | 알파평균 Σ\|exposure\| | 최대 | 최소 | R 0→−1 손익 스윙(평균) | 비중% |
| --- | --- | --- | --- | --- | --- | --- |
| no_ratio | 3 | 0.0049 | 0.0064 | 0.0029 | +0.0039 | 43.5 |
| no_cache_successor_at_settlement | 2 | 0.0034 | 0.0077 | 0.0014 | +0.0022 | 30.1 |
| no_terms | 2 | 0.0030 | 0.0049 | 0.0003 | +0.0011 | 26.4 |

- 백필 워크리스트: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2g3_Top500/default_settlements_worklist.csv` (7종목)
- 정산 로그 원자료: `settlement_log.csv` (1410행)

## R 민감도 항등식 (PnL 차이 = 정산 손익 차이)

| alpha | PnL(R=0) | PnL(R=-1) | 차이 | delistingPnl 차이 | 잔차 |
| --- | --- | --- | --- | --- | --- |
| returns_zscore | -3.399588 | -3.390269 | 0.009319 | 0.009319 | 1.244e-15 |
| neg_returns_tsmean5_zscore | -0.450250 | -0.440765 | 0.009485 | 0.009485 | 9.714e-17 |
| close_tsmean20_zscore | -0.513830 | -0.505059 | 0.008771 | 0.008771 | -1.249e-16 |
| afl_100_zscore | -0.295454 | -0.287732 | 0.007721 | 0.007721 | 2.992e-16 |
| afl_963_zscore | 0.250248 | 0.251011 | 0.000763 | 0.000763 | -1.290e-17 |
