# Top500 결측·상폐 전기간 하네스

- config: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f2_Top500/config_Top500.yaml`
- server: `/tmp/longshort_module_server/build/longshort-server`
- 패널: T=4450 N=1056 2010-01-04 ~ 2026-09-11, feeRate=0.0005 barsPerYear=252
- 요청: simStart=2010-01-04 isStart=2012-01-01 osStart=2020-01-01 mode=1 missingClose=carry staleQuoteSettleBars=config
- 유니버스: Top500 (`/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f2_Top500/config_Top500.yaml`)
- 결측 계약 키: ['missingClosePolicy', 'defaultDelistingReturn', 'staleQuoteSettleBars', 'eventsLoaded', 'settlementSymbols']
- 가드: config 선언=True 카운터={'eventsPriceChecks': 197, 'eventsPriceChecksOutOfRange': 0, 'eventsPriceChecksNotEvaluable': 65} 
- **exit code: 0** (all runs OK)
- 미노출 응답 키: 없음
- 데이터 로드: 3.2s, 로드 직후 RSS 285MB (peak 285MB)

| alpha | R | status | 기간 | bars | PnL | Sharpe(full) | Sharpe(IS) | Sharpe(OS) | MDD | turnover/bar | fee 합계 | retirements | frozenGrossSum | postEndDateQuoteBars | pendingSettlements | successorConversions | convertedGross | computedSettlementReturns | missingData | sec | RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| returns_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -3.3997 | -2.627 | -4.095 | -2.080 | -3.4164 | 1.4504 | 3.0443 | 262 | 0.9099 | 0 | 0 | 57 | 0.1182 | 102 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=1.1217144637873813, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=-0.0007120433611026294, settledGross=0.2296650804872558, defaultDelistings=29, defaultDelistingGross=0.07445009863286367, cancelledDelistingEntries=76, frozenGrossSum=0.9099132094376523, retirements=262, successorConversions=57, convertedGross=0.11824066671042288, computedSettlementReturns=102 | 0.238 | 374 |
| returns_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -3.3857 | -2.610 | -4.022 | -2.083 | -3.4024 | 1.4504 | 3.0443 | 262 | 0.9099 | 0 | 0 | 57 | 0.1182 | 102 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=1.1217144637873813, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=0.013301868542757007, settledGross=0.2296650804872558, defaultDelistings=29, defaultDelistingGross=0.07445009863286367, cancelledDelistingEntries=76, frozenGrossSum=0.9099132094376523, retirements=262, successorConversions=57, convertedGross=0.11824066671042288, computedSettlementReturns=102 | 0.222 | 462 |
| neg_returns_tsmean5_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.4499 | -0.329 | -1.197 | 0.048 | -0.7366 | 0.6284 | 1.3190 | 262 | 0.6195 | 0 | 0 | 57 | 0.1019 | 102 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=0.6312512496735008, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=-0.00013557126527320552, settledGross=0.17161338442930485, defaultDelistings=29, defaultDelistingGross=0.05531292460759666, cancelledDelistingEntries=81, frozenGrossSum=0.6194653067271604, retirements=262, successorConversions=57, convertedGross=0.10193353342829489, computedSettlementReturns=102 | 0.212 | 462 |
| neg_returns_tsmean5_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.4283 | -0.313 | -1.126 | 0.041 | -0.7096 | 0.6284 | 1.3190 | 262 | 0.6195 | 0 | 0 | 57 | 0.1019 | 102 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=0.6312512496735008, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=0.021463649355021618, settledGross=0.17161338442930485, defaultDelistings=29, defaultDelistingGross=0.05531292460759666, cancelledDelistingEntries=81, frozenGrossSum=0.6194653067271604, retirements=262, successorConversions=57, convertedGross=0.10193353342829489, computedSettlementReturns=102 | 0.211 | 462 |
| close_tsmean20_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.5139 | -0.751 | -0.972 | -0.470 | -0.5293 | 0.0070 | 0.0147 | 262 | 0.4515 | 0 | 0 | 57 | 0.0668 | 102 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=0.30077705214326583, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=0.000227221465950168, settledGross=0.2043552040876872, defaultDelistings=29, defaultDelistingGross=0.05266207090918384, cancelledDelistingEntries=81, frozenGrossSum=0.451534634535511, retirements=262, successorConversions=57, convertedGross=0.06680098931122289, computedSettlementReturns=102 | 0.216 | 551 |
| close_tsmean20_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.5212 | -0.760 | -0.969 | -0.467 | -0.5366 | 0.0070 | 0.0147 | 262 | 0.4515 | 0 | 0 | 57 | 0.0668 | 102 | missingHeldBars=306, blockedOrders=307, blockedTradeWeight=0.30077705214326583, missingEntries=1, resumedHoldings=18, delistings=142, delistingPnl=-0.007040997131205272, settledGross=0.2043552040876872, defaultDelistings=29, defaultDelistingGross=0.05266207090918384, cancelledDelistingEntries=81, frozenGrossSum=0.451534634535511, retirements=262, successorConversions=57, convertedGross=0.06680098931122289, computedSettlementReturns=102 | 0.173 | 551 |
| afl_100_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | -0.2953 | -0.171 | -0.023 | -0.362 | -0.5687 | 0.0453 | 0.0950 | 262 | 0.4649 | 0 | 0 | 57 | 0.0585 | 102 | missingHeldBars=306, blockedOrders=270, blockedTradeWeight=0.3478992932265449, missingEntries=1, resumedHoldings=18, delistings=141, delistingPnl=-0.00027858012293541945, settledGross=0.1681655982519248, defaultDelistings=28, defaultDelistingGross=0.04608618570550747, cancelledDelistingEntries=80, frozenGrossSum=0.4648680566992511, retirements=262, successorConversions=57, convertedGross=0.058486072828956966, computedSettlementReturns=102 | 0.456 | 663 |
| afl_100_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | -0.2492 | -0.144 | 0.011 | -0.355 | -0.5586 | 0.0453 | 0.0950 | 262 | 0.4649 | 0 | 0 | 57 | 0.0585 | 102 | missingHeldBars=306, blockedOrders=270, blockedTradeWeight=0.3478992932265449, missingEntries=1, resumedHoldings=18, delistings=141, delistingPnl=0.04580760558257205, settledGross=0.1681655982519248, defaultDelistings=28, defaultDelistingGross=0.04608618570550747, cancelledDelistingEntries=80, frozenGrossSum=0.4648680566992511, retirements=262, successorConversions=57, convertedGross=0.058486072828956966, computedSettlementReturns=102 | 0.17 | 663 |
| afl_963_zscore | 0 | OK | 2010-01-04~2026-09-11 | 4198 | 0.2499 | 0.390 | 0.389 | 0.422 | -0.0750 | 0.0419 | 0.0879 | 262 | 0.5298 | 0 | 0 | 56 | 0.0791 | 101 | missingHeldBars=306, blockedOrders=269, blockedTradeWeight=0.40014585477786174, missingEntries=1, resumedHoldings=18, delistings=138, delistingPnl=0.00018489640852817685, settledGross=0.1676120635914631, defaultDelistings=28, defaultDelistingGross=0.0407677560225562, cancelledDelistingEntries=77, frozenGrossSum=0.5297810595169035, retirements=262, successorConversions=56, convertedGross=0.07909000339406365, computedSettlementReturns=101 | 0.483 | 889 |
| afl_963_zscore | -1 | OK | 2010-01-04~2026-09-11 | 4198 | 0.2744 | 0.427 | 0.448 | 0.434 | -0.0750 | 0.0419 | 0.0879 | 262 | 0.5298 | 0 | 0 | 56 | 0.0791 | 101 | missingHeldBars=306, blockedOrders=269, blockedTradeWeight=0.40014585477786174, missingEntries=1, resumedHoldings=18, delistings=138, delistingPnl=0.024690133795916825, settledGross=0.1676120635914631, defaultDelistings=28, defaultDelistingGross=0.0407677560225562, cancelledDelistingEntries=77, frozenGrossSum=0.5297810595169035, retirements=262, successorConversions=56, convertedGross=0.07909000339406365, computedSettlementReturns=101 | 0.169 | 889 |

## 정산 source 분포

| source | 정산 건수 | 종목 수 | Σ\|exposure\| |
| --- | --- | --- | --- |
| computed | 509 | 102 | 1.0037 |
| default | 143 | 29 | 0.2693 |
| explicit | 53 | 11 | 0.0941 |

## 기본 R 정산 영향 (source=default를 cacheUnitStatus로 분해)

| cacheUnitStatus | 종목 수 | 알파평균 Σ\|exposure\| | 최대 | 최소 | R 0→−1 손익 스윙(평균) | 비중% |
| --- | --- | --- | --- | --- | --- | --- |
| no_successor_id | 15 | 0.0314 | 0.0484 | 0.0230 | +0.0115 | 58.2 |
| no_terms | 12 | 0.0189 | 0.0228 | 0.0098 | +0.0077 | 35.2 |
| no_cache_successor_at_settlement | 2 | 0.0036 | 0.0069 | 0.0009 | +0.0007 | 6.6 |

- 백필 워크리스트: `/tmp/claude-1008/-home-jgo/3bd99f3c-b437-4ac4-934f-711593113d11/scratchpad/live/p2f2_Top500/default_settlements_worklist.csv` (29종목)
- 정산 로그 원자료: `settlement_log.csv` (1410행)

## R 민감도 항등식 (PnL 차이 = 정산 손익 차이)

| alpha | PnL(R=0) | PnL(R=-1) | 차이 | delistingPnl 차이 | 잔차 |
| --- | --- | --- | --- | --- | --- |
| returns_zscore | -3.399731 | -3.385717 | 0.014014 | 0.014014 | 1.506e-15 |
| neg_returns_tsmean5_zscore | -0.449896 | -0.428297 | 0.021599 | 0.021599 | 1.874e-16 |
| close_tsmean20_zscore | -0.513897 | -0.521165 | -0.007268 | -0.007268 | -5.447e-16 |
| afl_100_zscore | -0.295298 | -0.249211 | 0.046086 | 0.046086 | -3.469e-17 |
| afl_963_zscore | 0.249916 | 0.274421 | 0.024505 | 0.024505 | -4.163e-17 |
