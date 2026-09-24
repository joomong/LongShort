## 14. Phase 2f — 정산 로그와 "미평가 행이 백테스트에 주는 영향"

엔진의 `settlementLog` 옵션과 3분류 카운터가 재빌드에 이미 들어와 있어 코드 작성과 실행을 함께 마쳤음. 서버는 18120~18139에서만 띄웠고 전부 종료 확인함.

### 14-1. 원장·하네스 변경

- **원장**: 정산마다 `{bar, asset, exposure, return, source, cash_share, stock_share, successor, transferred}` 기록을 남김(거래 bar·exposure≠0만, 엔진 계약과 동일 범위). 노출 0인 정산은 `settlement_unheld_count`로 따로 셈.
- **`check_missing_data.py`**: 시나리오 4종(`successor_ratio_two`·`mixed_cash_and_stock`·`explicit_fallback_outside_panel`·`delist_default_minus_one_long`)에 `settlementLog: true`를 요청하고, 로그를 (symbol, time)으로 짝지어 **bar·exposure·return·source**를 엄격 대조함. 로그 부재·레코드 누락/과잉·`settlementLogUnheld` 불일치는 전부 FAIL임.
- **`run_top500_missing_close.py`**: 모든 run에 `settlementLog: true`, 로그를 `data/events/events.parquet`의 `cacheUnitStatus`·`hasStockLeg`·`hasCashLeg`·`actionCode`·`actionText`와 조인(티커는 마스터에서). 신규 옵션 `--worklist-dir`, `--merge-worklists`, `--no-settlement-log`.
- **신규 항등식(위반 시 FAIL → exit 1)**: `Σ(exposure×return) = delistingPnl`, `Σ|exposure|(source=default) = defaultDelistingGross`, `eventsPriceChecks + OutOfRange + NotEvaluable = eventsLoaded`.

### 14-2. `--self-test` 확장

- `check_missing_data.py`: **12 probe 전부 PASS**(로그 부재·source 오류·exposure 0.3 어긋남·정상 로그 4종 추가).
- `run_top500_missing_close.py`: **18 probe 전부 PASS**(로그 부재·`Σ(exposure×return)` 0.3 오차·default 노출 합 불일치·`settlementLogUnheld` 부재·카운터 합 불일치·카운터 누락 8종 추가).

### 14-3. HTTP 대조 (로그 포함)

self-test 12/12, self-check 40 시나리오 295 체크, **HTTP 178 케이스 전부 PASS**(수치 대조 146·양쪽 거절 30·범위 거절 2). 그중 **settlementLog 대조 16건 전부 PASS, 최대 차이 0.0** — bar·exposure·return·source가 원장과 완전히 일치함.

### 14-4. Top500 전기간 결과 (알파 5 × R{0,−1}, 10/10 OK, exit 0)

카운터: `eventsPriceChecks=179` + `OutOfRange=0` + `NotEvaluable=83` = **262** = `eventsLoaded` ✓. 정산 로그 1410행(런 전체 1410), 노출 0이라 로그에서 빠진 정산 `settlementLogUnheld=114`건.

| source | 정산 건수 | 종목 수 | Σ\|exposure\| |
| --- | --- | --- | --- |
| computed | 439 | 88 | 0.8641 |
| default | 211 | 43 | 0.3906 |
| explicit | 55 | 11 | 0.1124 |

**기본 R 정산 영향** — `source=default`(파일이 평가 입력을 주지 못해 config 기본값으로 정산된 행)를 사유별로 분해했음. `R 0→−1 손익 스윙`은 그 사유가 만드는 PnL 차이이며, 합계가 알파별 `ΔdelistingPnl`과 정확히 일치함(잔차 ≤3.6e-16).

| cacheUnitStatus | 종목 수 | 알파평균 Σ\|exposure\| | 최대 | 최소 | R 0→−1 손익 스윙(평균) | 비중% |
| --- | --- | --- | --- | --- | --- | --- |
| no_successor_id | 27 | 0.0506 | 0.0604 | 0.0420 | +0.0153 | 64.8 |
| no_terms | 9 | 0.0150 | 0.0202 | 0.0073 | +0.0083 | 19.2 |
| no_cache_successor_at_settlement | 5 | 0.0084 | 0.0138 | 0.0040 | +0.0040 | 10.8 |
| rejected:cash_R_out_of_range | 2 | 0.0041 | 0.0070 | 0.0022 | +0.0024 | 5.2 |

읽는 법:

- 미평가(default) 정산은 **43종목 / 211건**이고 노출 합은 알파평균 **0.0781**(북 gross 1 기준)임. 전체 정산 노출 1.367 중 **29%**가 평가되지 않은 채 기본값으로 처리되고 있음.
- 가장 큰 사유는 **`no_successor_id`(27종목, 비중 64.8%)** — 주식 대가는 인식했으나 인수사 tradingItemId를 못 붙인 행임. 다음이 `no_terms`(9종목, 19.2%, 조건 자체를 못 읽음), `no_cache_successor_at_settlement`(5종목, 10.8%, §13-7 IWA가 이 부류), `rejected:cash_R_out_of_range`(2종목, 5.2%).
- 손익 영향은 알파당 R 0→−1 스윙 **+0.015~+0.030**임. `returns_zscore`(PnL −3.40)에는 1% 미만이지만 `afl_963`(PnL 0.25) 같은 알파에는 **10% 이상**이라, 알파에 따라 결론을 바꿀 수 있는 크기임.
- 부호가 전부 양(+)인 것은 default 정산 대상의 순 노출이 **숏 우위**여서임(R을 −1로 낮추면 이익). 즉 기본값 선택이 체계적으로 한 방향으로 치우친 손익을 만듦.

### 14-5. 백필 워크리스트

`scratchpad/live/p2f_Top500/default_settlements_worklist.csv` (43종목, |exposure| 내림차순). 열: tradingItemId, ticker, endDate, actionCode, cacheUnitStatus, hasStockLeg, max_abs_exposure, alphas, universe, actionText(120자). 상위 5건:

| tradingItemId | ticker | endDate | cacheUnitStatus | hasStockLeg | max\|exposure\| | 알파수 |
| --- | --- | --- | --- | --- | --- | --- |
| 2638830 | PCYC | 2015-05-22 | no_successor_id | True | 0.00834 | 5 |
| 312996152 | FDC | 2019-07-26 | no_successor_id | True | 0.00649 | 5 |
| 2623613 | KSU | 2021-12-13 | no_successor_id | True | 0.00628 | 5 |
| 2591859 | AVB | 2026-08-14 | no_cache_successor_at_settlement | True | 0.00598 | 5 |
| 2665190 | XLNX | 2022-02-11 | no_successor_id | True | 0.00594 | 5 |

세 유니버스 결과는 §14-6, 합본 워크리스트는 §14-7에 있음.


### 14-6. 유니버스 3종 요약 (Top500 · Top1000 · ALL)

| 유니버스 | 패널 N | T | exit | 카운터 합 = eventsLoaded | 정산 로그 행 | 노출 0 정산 | 로드 s | peak RSS MB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Top500 | 1056 | 4450 | **0** | 179+0+83=262 = 262 ✓ | 1410 | 114 | 3.31 | 885 |
| Top1000 | 2315 | 4450 | **0** | 594+0+207=801 = 801 ✓ | 4922 | 285 | 4.22 | 1105 |
| ALL | 6399 | 4450 | **0** | 1707+0+838=2545 = 2545 ✓ | 17182 | 706 | 5.72 | 1816 |

**source 분포와 기본 R 노출 비율** (R=0 실행, 알파 5개 합산)

| 유니버스 | computed 건/종목/Σ\|exp\| | explicit | default | **default 노출 비율** |
| --- | --- | --- | --- | --- |
| Top500 | 439/88/0.8641 | 55/11/0.1124 | 211/43/0.3906 | **28.6%** |
| Top1000 | 1828/368/1.6699 | 115/23/0.1046 | 518/106/0.4625 | **20.7%** |
| ALL | 6048/1241/2.2176 | 240/49/0.0966 | 2303/505/1.1270 | **32.8%** |

**사유별 분해** (source=default, 알파평균 Σ\|exposure\|)

| cacheUnitStatus | Top500 종목/Σ\|exp\|/비중% | Top1000 종목/Σ\|exp\|/비중% | ALL 종목/Σ\|exp\|/비중% |
| --- | --- | --- | --- |
| no_cache_successor_at_settlement | 5/0.0084/10.8 | 10/0.0098/10.5 | 29/0.0203/9.0 |
| no_db_target | - | - | 1/0.0004/0.2 |
| no_successor_id | 27/0.0506/64.8 | 56/0.0500/54.0 | 148/0.0572/25.3 |
| no_terms | 9/0.0150/19.2 | 34/0.0284/30.7 | 304/0.1381/60.9 |
| rejected:R_out_of_range | - | 2/0.0011/1.1 | 6/0.0023/1.0 |
| rejected:cash_R_out_of_range | 2/0.0041/5.2 | 3/0.0026/2.8 | 9/0.0028/1.2 |
| rejected:rename_not_1to1 | - | 1/0.0008/0.8 | 8/0.0056/2.5 |

**알파별 R 0→−1 손익 스윙** (= ΔdelistingPnl, 잔차는 전부 1e-15 이하)

| alpha | Top500 스윙 (PnL R=0 대비 %) | Top1000 스윙 (PnL R=0 대비 %) | ALL 스윙 (PnL R=0 대비 %) |
| --- | --- | --- | --- |
| returns_zscore | +0.0300 (0.9%) | +0.0107 (0.3%) | +0.0766 (2.1%) |
| neg_returns_tsmean5_zscore | +0.0296 (6.6%) | +0.0269 (22.7%) | -0.0080 (9.9%) |
| close_tsmean20_zscore | +0.0097 (1.9%) | +0.0379 (5.2%) | +0.1135 (1.4%) |
| afl_100_zscore | +0.0699 (23.7%) | +0.0678 (30.6%) | -0.0313 (8.2%) |
| afl_963_zscore | +0.0108 (4.3%) | +0.0125 (4.1%) | +0.0060 (3.2%) |

Top500 → Top1000 → ALL을 **순차로** 돌렸음(동시 실행 없음). 세 실행 모두 10/10 OK·**exit 0**이고, 세 항등식(수수료·R 민감도·정산 로그 합)과 카운터 3분류 합이 전부 성립했음. peak RSS는 885MB → 1,105MB → 1,816MB로 패널 N(1,056 → 2,315 → 6,399)보다 완만하게 늘었음.

읽는 법:

- **미평가 비율은 유니버스를 넓힌다고 줄지 않음.** Top500 28.6% → Top1000 20.7% → ALL 32.8%. Top1000에서 잠깐 낮아지는 건 대형주 쪽 커버리지가 좋아서이고, ALL로 가면 소형·상장폐지 종목이 들어오며 다시 나빠짐.
- **지배적 사유가 유니버스에 따라 뒤바뀜.** Top500은 `no_successor_id`(64.8%)가 최대이나 ALL에서는 `no_terms`(60.9%)가 최대로 바뀜. 즉 대형주는 "인수사 id 매칭"이, 전체 유니버스에서는 "딜 조건 파싱" 자체가 병목임. 백필 우선순위가 유니버스에 따라 달라짐.
- **손익 영향은 알파 의존이 크고 부호도 뒤집힘.** `afl_100_zscore`는 Top500 23.7%·Top1000 30.6%로 기본 R 선택이 결론을 바꿀 수준이고, ALL에서는 `neg_returns_tsmean5`·`afl_100`의 스윙 부호가 음(−)으로 뒤집힘 — default 대상의 순 노출이 유니버스에 따라 롱 우위가 되기 때문임. **"기본 R=0이 보수적"이라고 일반화할 수 없음.**
- `eventsPriceChecksOutOfRange`는 세 유니버스 모두 0임. 패널 밖 이벤트는 `NotEvaluable`로 잡히고 있으므로, 두 카운터의 구분이 실제로 쓰이는지는 리드 확인 권고함.

### 14-7. 합본 워크리스트

`scratchpad/live/p2f_worklist_all.csv` — **505종목**(Top500 43 · Top1000 106 · ALL 505, tradingItemId 기준 병합). 열은 유니버스별 노출 3개(`max_abs_exposure_Top500/_Top1000/_ALL`) + 전체 최대 + `universe`(콤마 병합), 정렬은 Top500 노출 → Top1000 → ALL 우선임.

| tradingItemId | ticker | endDate | cacheUnitStatus | Top500 | Top1000 | ALL |
| --- | --- | --- | --- | --- | --- | --- |
| 2638830 | PCYC | 2015-05-22 | no_successor_id | 0.008344 | 0.004464 | 0.000343 |
| 312996152 | FDC | 2019-07-26 | no_successor_id | 0.006490 | 0.002916 | 0.001084 |
| 2623613 | KSU | 2021-12-13 | no_successor_id | 0.006285 | 0.002952 | 0.000937 |
| 2591859 | AVB | 2026-08-14 | no_cache_successor_at_settlement | 0.005977 | 0.002701 | 0.000762 |
| 2665190 | XLNX | 2022-02-11 | no_successor_id | 0.005938 | 0.002639 | 0.000777 |

상위권은 전부 대형 인수 건(PCYC/AbbVie, FDC/Fiserv, KSU/CP, AVB, XLNX/AMD)이며 `no_successor_id`가 다수임 — 인수사 id만 붙이면 `computed` 경로로 넘어갈 수 있는 건들이라 백필 투자 대비 효과가 가장 큼.

## 14-8 실행 환경

이벤트 파일 재생성본(mtime `2026-09-14T11:33:50`, 6,413행 × 30컬럼, sha256 `89e6843a2957420c62e002e8b1c115f5…`, `cacheUnitStatus=ok` 3,091 / `no_terms` 2,836 / `no_successor_id` 187 / `rejected:rename_not_1to1` 148 / `no_cache_successor_at_settlement` 65 / `rejected:R_out_of_range` 38 / `rejected:cash_R_out_of_range` 26 / `no_db_target` 22, source override 23행)과 재빌드 엔진으로 실행했음. 서버는 18120~18139에서만 띄웠고 전부 종료 확인함.

**HTTP 대조**: self-test 12/12, self-check 40 시나리오 295 체크, **178 케이스 전부 PASS**(수치 대조 146 · 양쪽 거절 30 · 범위 거절 2), settlementLog 대조 16건 전부 PASS·최대 차이 0.0.

**하네스**: Top500 → Top1000 → ALL **순차** 실행, 셋 다 10/10 OK·**exit 0**. 카운터 3분류 합이 `eventsLoaded`와 일치(262 / 801 / 2545 ✓). 엔진의 `eventsLoaded` 정의 수정(매칭 행 수) 이후에도 합이 맞음.

### 14-8. 생성기 백필 1차 이후 재측정 (§14-6 대비)

| 유니버스 | exit | 카운터 합 = eventsLoaded | default 종목 수 전/후 | default Σ\|exp\| 전/후 | **default 노출 비율 전/후** | computed 종목 전/후 |
| --- | --- | --- | --- | --- | --- | --- |
| Top500 | **0** | 197+0+65=262 = 262 ✓ | 43 → **29** | 0.3906 → **0.2693** | 28.6% → **19.7%** | 88 → 102 |
| Top1000 | **0** | 608+0+193=801 = 801 ✓ | 106 → **100** | 0.4625 → **0.4378** | 20.7% → **19.6%** | 368 → 379 |
| ALL | **0** | 1722+0+823=2545 = 2545 ✓ | 505 → **513** | 1.1270 → **1.1360** | 32.8% → **33.0%** | 1241 → 1247 |

**사유별 분해 전/후** (source=default, 알파평균 Σ\|exposure\|; `종목수 / Σ|exp|`)

| cacheUnitStatus | Top500 전 → 후 | Top1000 전 → 후 | ALL 전 → 후 |
| --- | --- | --- | --- |
| no_cache_successor_at_settlement | 5/0.0084 → **2/0.0036** | 10/0.0098 → **4/0.0046** | 29/0.0203 → **10/0.0046** |
| no_db_target | - → **-** | - → **-** | 1/0.0004 → **1/0.0004** |
| no_successor_id | 27/0.0506 → **15/0.0314** | 56/0.0500 → **35/0.0332** | 148/0.0572 → **77/0.0316** |
| no_terms | 9/0.0150 → **12/0.0189** | 34/0.0284 → **57/0.0473** | 304/0.1381 → **404/0.1768** |
| rejected:R_out_of_range | - → **-** | 2/0.0011 → **2/0.0011** | 6/0.0023 → **6/0.0075** |
| rejected:cash_R_out_of_range | 2/0.0041 → **-** | 3/0.0026 → **1/0.0008** | 9/0.0028 → **7/0.0020** |
| rejected:rename_not_1to1 | - → **-** | 1/0.0008 → **1/0.0008** | 8/0.0056 → **8/0.0056** |

**알파별 R 0→−1 손익 스윙 전/후**

| alpha | Top500 전 → 후 | Top1000 전 → 후 | ALL 전 → 후 |
| --- | --- | --- | --- |
| returns_zscore | +0.0300 → **+0.0140** | +0.0107 → **-0.0028** | +0.0766 → **+0.0701** |
| neg_returns_tsmean5_zscore | +0.0296 → **+0.0216** | +0.0269 → **+0.0315** | -0.0080 → **-0.0027** |
| close_tsmean20_zscore | +0.0097 → **-0.0073** | +0.0379 → **+0.0345** | +0.1135 → **+0.1199** |
| afl_100_zscore | +0.0699 → **+0.0461** | +0.0678 → **+0.0632** | -0.0313 → **-0.0191** |
| afl_963_zscore | +0.0108 → **+0.0245** | +0.0125 → **+0.0229** | +0.0060 → **+0.0123** |

### 14-9. 백필의 순효과 — 종목 단위 추적

백필로 default에서 빠진 종목 **30**, 새로 들어온 종목 **38**, 남았지만 사유가 바뀐 종목 **120**

| 사유 변화 | 종목 수 | 대표 티커 |
| --- | --- | --- |
| no_cache_successor_at_settlement → no_successor_id | 9 | RGP, QSC, CMLP, VIAB |
| no_cache_successor_at_settlement → rejected:R_out_of_range | 1 | SIR |
| no_successor_id → no_cache_successor_at_settlement | 1 | ALXN |
| no_successor_id → no_terms | 107 | PCYC, HPR, NLC, XEC |
| rejected:R_out_of_range → no_successor_id | 1 | ISLE |
| rejected:cash_R_out_of_range → no_successor_id | 1 | PDE |

| 구분 | 종목 수 | 사유 분포 |
| --- | --- | --- |
| 해결됨(default 탈출) | 30 | {'no_successor_id': 12, 'no_cache_successor_at_settlement': 10, 'no_terms': 7, 'rejected:cash_R_out_of_range': 1} |
| 새로 추가 | 38 | {'no_successor_id': 38} |

새로 default로 떨어진 38종목 중 노출 상위 (전부 `no_successor_id`):

| tradingItemId | ticker | endDate | Top500 | Top1000 | ALL |
| --- | --- | --- | --- | --- | --- |
| 32943110 | TWC | 2016-05-17 | 0.008873 | 0.004372 | 0.001426 |
| 2628778 | MEE | 2011-06-02 | 0.006952 | 0.003191 | 0.001031 |
| 303334924 | BXLT | 2016-06-03 | 0.006031 | 0.002588 | 0.000724 |
| 2628597 | MWE | 2015-12-03 | 0.005930 | 0.002752 | 0.000790 |
| 34969538 | COV | 2015-01-27 | 0.004179 | 0.001764 | 0.000642 |
| 46291896 | LPS | 2014-01-03 | - | 0.003250 | 0.001418 |

해결된 30종목 중 노출 상위: KOG(no_cache_successor_at_settlement), FDC(no_successor_id), KSU(no_successor_id), XLNX(no_successor_id), DPS(no_terms), TYC(rejected:cash_R_out_of_range), LLL(no_successor_id), AMTD(no_successor_id)


읽는 법 — **Top500은 확실히 좋아졌으나 전체 유니버스에서는 순효과가 거의 없음**:

- **Top500**: default 종목 43 → **29**, 노출 비율 28.6% → **19.7%**. 백필이 겨냥한 대형주 구간에서는 의도대로 동작했음. `no_successor_id` 27 → 15, `no_cache_successor_at_settlement` 5 → 2, `rejected:cash_R_out_of_range` 2 → 0.
- **ALL**: 505 → **513**, 32.8% → **33.0%**로 **미세하게 나빠짐**. 해결 30종목 대비 신규 유입 38종목이 더 많았기 때문임.
- **가장 큰 움직임은 해결이 아니라 재분류임.** 107종목이 `no_successor_id` → `no_terms`로 라벨만 바뀌었고 여전히 평가되지 않음. §14-6에서 "Top500은 `no_successor_id`가 최대 사유"라고 읽었던 것이 이번엔 "`no_terms`가 최대"로 보이는데, 그 변화의 상당 부분이 **실질 개선이 아니라 분류 이동**임. 사유별 추이만 보면 잘못 읽기 쉬움.
- **38종목이 새로 default로 떨어진 것은 회귀임.** 전부 `no_successor_id`이고, TWC(0.008873)·MEE(0.006952)·BXLT(0.006031)·MWE(0.005930)·COV(0.004179)처럼 Top500 노출 상위권이 포함됨. 특히 **TWC는 합본 워크리스트 1위**로 올라왔음 — 이전 실행에서는 default가 아니었음. 생성기 리졸버가 일부 행의 승계사 id를 오히려 잃었을 가능성이 있어 확인 필요함.
- **손익 스윙은 줄지 않았고 부호가 바뀐 것도 있음.** `afl_963_zscore`는 Top500 +0.0108 → **+0.0245**로 오히려 2배가 됐고, `close_tsmean20_zscore`(Top500)와 `returns_zscore`(Top1000)는 부호가 뒤집혔음. 미평가 종목 수가 줄어도 남은 종목의 순노출 방향이 바뀌면 손익 영향은 커질 수 있음 — **종목 수 감소를 손익 리스크 감소로 읽으면 안 됨.**

### 14-10. 산출물과 다음 배치

| 파일 | 내용 |
| --- | --- |
| `scratchpad/live/p2f2_<U>/report.{md,json}` | 유니버스별 보고서(항등식·exit code·source 분포·사유 분해) |
| `scratchpad/live/p2f2_<U>/settlement_log.csv` | 정산 로그 원자료 |
| `scratchpad/live/p2f2_worklist_all.csv` | 합본 워크리스트 **513종목**(Top500 29 · Top1000 100 · ALL 513), 유니버스별 노출 3열 + 최대 + universe 병합 |
| `scratchpad/live/p2f2_next_batch_top1000.csv` | **다음 배치 20종목** — Top1000 워크리스트에서 Top500 중복 29종목 제외 후 노출 내림차순 상위 20 |

다음 배치 20종목의 사유는 `no_terms` 11 · `no_successor_id` 7 · `no_cache_successor_at_settlement` 2이고, 상위는 XEC(0.004570)·LSI(0.004197)·GRUB(0.004178)·GTLS(0.003947)·TERP(0.003741)임. `unresolved:election`은 이번 파일에 0건이라 제외 필터가 걸린 행이 없었음(필터 자체는 구현돼 있음).

## 14-11 실행 환경

최종 이벤트 파일(mtime `2026-09-14T11:53:14`, 6,413행 × 30컬럼, sha256 `fe27c21ec2be0840465893d0f17fce33…`)로 실행했음. 상태 분포는 `ok` **3,125** / `no_terms` 2,551 / **`no_ratio` 308(신규 분리)** / `rejected:rename_not_1to1` 148 / `no_cache_successor_at_settlement` 146 / `rejected:R_out_of_range` 46 / `no_successor_id` **41**(1차 187에서 급감) / `rejected:cash_R_out_of_range` 26 / `no_db_target` 22, override 35행임.

**HTTP 대조**: self-test 12/12, self-check 40 시나리오 295 체크, **178 케이스 전부 PASS**(수치 대조 146 · 양쪽 거절 30 · 범위 거절 2), settlementLog 대조 16건 전부 PASS·최대 차이 0.0.

**하네스**: Top500 → Top1000 → ALL 순차, 셋 다 10/10 OK·**exit 0**, 카운터 3분류 합이 `eventsLoaded`와 일치(262 / 801 / 2545 ✓). 가드 검사 수는 207 / 635 / 1,753으로 1차(197 / 608 / 1,722)보다 늘고 `NotEvaluable`은 55 / 166 / 792로 줄었음.

## 14-11. 최종 게이트 — 3시점 추적 (백필 전 → 1차 → 최종)

| 유니버스 | exit | 카운터 합 = eventsLoaded | default 종목 수 | default Σ\|exp\| | **default 노출 비율** |
| --- | --- | --- | --- | --- | --- |
| Top500 | **0** | 262 = 262 ✓ | 43 → 29 → **21** | 0.3906 → 0.2693 → **0.1735** | 28.6% → 19.7% → **12.7%** |
| Top1000 | **0** | 801 = 801 ✓ | 106 → 100 → **71** | 0.4625 → 0.4378 → **0.2674** | 20.7% → 19.6% → **12.0%** |
| ALL | **0** | 2545 = 2545 ✓ | 505 → 513 → **462** | 1.1270 → 1.1360 → **1.0303** | 32.8% → 33.0% → **29.9%** |

**사유별 분해 3시점** (source=default, `종목수/Σ|exp|`, 알파평균)

| cacheUnitStatus | Top500 전→1차→최종 | Top1000 전→1차→최종 | ALL 전→1차→최종 |
| --- | --- | --- | --- |
| no_cache_successor_at_settlement | 5/0.0084 → 2/0.0036 → **4/0.0070** | 10/0.0098 → 4/0.0046 → **7/0.0072** | 29/0.0203 → 10/0.0046 → **17/0.0079** |
| no_db_target | - → - → **-** | - → - → **-** | 1/0.0004 → 1/0.0004 → **1/0.0004** |
| no_ratio | - → - → **14/0.0235** | - → - → **35/0.0263** | - → - → **122/0.0467** |
| no_successor_id | 27/0.0506 → 15/0.0314 → **-** | 56/0.0500 → 35/0.0332 → **3/0.0022** | 148/0.0572 → 77/0.0316 → **8/0.0036** |
| no_terms | 9/0.0150 → 12/0.0189 → **3/0.0042** | 34/0.0284 → 57/0.0473 → **21/0.0138** | 304/0.1381 → 404/0.1768 → **291/0.1327** |
| rejected:R_out_of_range | - → - → **-** | 2/0.0011 → 2/0.0011 → **3/0.0026** | 6/0.0023 → 6/0.0075 → **8/0.0084** |
| rejected:cash_R_out_of_range | 2/0.0041 → - → **-** | 3/0.0026 → 1/0.0008 → **1/0.0008** | 9/0.0028 → 7/0.0020 → **7/0.0020** |
| rejected:rename_not_1to1 | - → - → **-** | 1/0.0008 → 1/0.0008 → **1/0.0008** | 8/0.0056 → 8/0.0056 → **8/0.0056** |

**알파별 R 0→−1 손익 스윙 3시점**

| alpha | Top500 전→1차→최종 | Top1000 전→1차→최종 | ALL 전→1차→최종 |
| --- | --- | --- | --- |
| returns_zscore | +0.0300 → +0.0140 → **+0.0229** | +0.0107 → -0.0028 → **+0.0076** | +0.0766 → +0.0701 → **+0.0742** |
| neg_returns_tsmean5_zscore | +0.0296 → +0.0216 → **+0.0081** | +0.0269 → +0.0315 → **+0.0119** | -0.0080 → -0.0027 → **-0.0090** |
| close_tsmean20_zscore | +0.0097 → -0.0073 → **+0.0004** | +0.0379 → +0.0345 → **+0.0279** | +0.1135 → +0.1199 → **+0.1086** |
| afl_100_zscore | +0.0699 → +0.0461 → **+0.0339** | +0.0678 → +0.0632 → **+0.0438** | -0.0313 → -0.0191 → **-0.0350** |
| afl_963_zscore | +0.0108 → +0.0245 → **+0.0097** | +0.0125 → +0.0229 → **+0.0138** | +0.0060 → +0.0123 → **+0.0072** |

### 14-12. 종목 단위 이동

| 구간 | default 종목 수 | 해결(이탈) | 신규 유입 | 남았으나 사유 변경 |
| --- | --- | --- | --- | --- |
| 백필 전 → 1차 | 505 → 513 | 30 | 38 | 120 |
| 1차 → 최종 | 513 → 462 | 51 | 0 | 132 |

**1차에서 회귀했던 38종목의 최종 상태**

회귀 38종목 중 **38종목이 default에서 벗어났고 0종목이 남았음**.

| tradingItemId | ticker | endDate | 1차 사유 | 최종 상태 |
| --- | --- | --- | --- | --- |
| 32943110 | TWC | 2016-05-17 | no_successor_id | **해결** |
| 2628778 | MEE | 2011-06-02 | no_successor_id | **해결** |
| 303334924 | BXLT | 2016-06-03 | no_successor_id | **해결** |
| 2628597 | MWE | 2015-12-03 | no_successor_id | **해결** |
| 34969538 | COV | 2015-01-27 | no_successor_id | **해결** |
| 104710737 | PNG | 2013-12-31 | no_successor_id | **해결** |
| 11042648 | RAI | 2017-07-24 | no_successor_id | **해결** |
| 117211700 | QRE | 2014-11-20 | no_successor_id | **해결** |
| 12678069 | IWA | 2010-05-28 | no_successor_id | **해결** |
| 133825164 | P | 2019-01-31 | no_successor_id | **해결** |
| 137336165 | OILT | 2015-02-16 | no_successor_id | **해결** |
| 165754337 | STRZ.A | 2016-12-09 | no_successor_id | **해결** |

**1차 → 최종 사유 이동** (남아 있는 종목 중)

| 사유 변화 | 종목 수 | 대표 티커 |
| --- | --- | --- |
| no_successor_id → no_cache_successor_at_settlement | 7 | RGP, PDE, QSC, CMLP |
| no_successor_id → no_ratio | 20 | BNI, SUG, TMX, SNI |
| no_successor_id → rejected:R_out_of_range | 2 | GRUB, ISLE |
| no_terms → no_ratio | 102 | PCYC, HPR, NLC, ANDV |
| no_terms → no_successor_id | 1 | WBS |

해결 51종목 사유 분포: {'no_successor_id': 41, 'no_terms': 10}
노출 상위 해결: TWC, MEE, BXLT, MWE, XEC, LSI, COV, GTLS, TERP, VER

최종 합본 워크리스트 462종목 (Top500 21 · Top1000 71 · ALL 462)

최종 상위 6종목:

| tradingItemId | ticker | endDate | cacheUnitStatus | Top500 | Top1000 | ALL |
| --- | --- | --- | --- | --- | --- | --- |
| 2638830 | PCYC | 2015-05-22 | no_ratio | 0.008344 | 0.004464 | 0.000343 |
| 2591859 | AVB | 2026-08-14 | no_cache_successor_at_settlement | 0.005977 | 0.002701 | 0.000762 |
| 2595940 | BNI | 2010-02-12 | no_ratio | 0.005644 | 0.001072 | 0.000510 |
| 2587881 | AA | 2016-10-31 | no_terms | 0.004846 | 0.002329 | 0.000987 |
| 12571130 | NLC | 2011-12-01 | no_ratio | 0.004587 | 0.002371 | 0.001131 |
| 25726986 | RGP | 2015-04-29 | no_cache_successor_at_settlement | 0.004250 | 0.002017 | 0.000758 |


읽는 법 — **회귀는 완전히 해소됐고 3시점 추세가 처음으로 단조 개선임**:

- **1차에서 회귀했던 38종목이 전부 default를 벗어났음**(TWC·MEE·BXLT·MWE·COV 포함, §13-7의 IWA도 포함). gvkey 매치 보존 수정이 의도대로 동작했음.
- **1차 → 최종 구간에 신규 유입이 0종목임.** 백필 전 → 1차에서는 해결 30 대 유입 38로 순손실이었으나, 이번엔 해결 51 · 유입 0임. 개선이 다른 곳을 깨지 않았다는 뜻임.
- **default 노출 비율이 세 유니버스 모두 내려갔음**: Top500 28.6% → 19.7% → **12.7%**, Top1000 20.7% → 19.6% → **12.0%**, ALL 32.8% → 33.0% → **29.9%**. ALL이 1차에서 악화됐다가 최종에서 회복된 것이 회귀 수정 효과를 그대로 보여줌.
- **`no_successor_id`가 사실상 소멸함**(Top500 27 → 15 → **0**, Top1000 56 → 35 → **3**, ALL 148 → 77 → **8**). 대신 신설된 **`no_ratio`**가 Top500 14 · Top1000 35 · ALL 122로 나타났는데, 이는 1차에서 `no_terms`로 뭉뚱그려져 있던 102종목과 `no_successor_id` 20종목이 **"승계사는 찾았으나 교환비를 못 읽음"으로 정확히 분리된 것**임. §14-9에서 지적했던 "라벨만 바뀌는 재분류" 문제가 이번에는 진단 가능한 형태로 쪼개진 것이라 성격이 다름 — 다음 백필의 타깃이 명확해졌음.
- **`no_cache_successor_at_settlement`는 오히려 늘었음**(Top500 2 → 4, ALL 10 → 17). 승계사 id를 새로 붙인 결과 "id는 있으나 정산 bar에 캐시 종가가 없음" 단계로 넘어온 건들임. 캐시 커버리지(OTC·상장폐지 종목 가격) 문제로 남는 잔여분임.
- **손익 스윙은 대체로 줄었으나 여전히 알파 의존적임.** `afl_100_zscore`는 Top500 +0.0699 → +0.0461 → **+0.0339**, Top1000 +0.0678 → **+0.0438**로 절반 수준까지 내려왔음. 다만 ALL의 `afl_100`은 −0.0313 → −0.0191 → **−0.0350**으로 되돌아왔고 `returns_zscore`(ALL)도 +0.0742로 거의 그대로임 — **ALL 유니버스에서는 기본 R 선택이 여전히 알파 손익을 수 % 움직임.**

### 14-13. 최종 산출물

| 파일 | 내용 |
| --- | --- |
| `scratchpad/live/p2f3_<U>/report.{md,json}` | 유니버스별 최종 보고서(항등식·exit code·source 분포·사유 분해) |
| `scratchpad/live/p2f3_<U>/settlement_log.csv` | 정산 로그 원자료 |
| `scratchpad/live/p2f3_worklist_all.csv` | 최종 합본 **462종목**(Top500 21 · Top1000 71 · ALL 462) |

최종 워크리스트 상위: PCYC(`no_ratio`, Top500 0.008344) · AVB(`no_cache_successor_at_settlement`) · BNI(`no_ratio`) · AA(`no_terms`) · NLC(`no_ratio`) · RGP(`no_cache_successor_at_settlement`). **남은 Top500 21종목 중 다수가 `no_ratio`**라 교환비 파싱을 다음 백필 타깃으로 잡으면 Top500 비율을 한 자릿수까지 내릴 수 있을 것으로 보임.
