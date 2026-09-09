# 우리 데이터(cache_v1_cash_1d) → cpp-engine 연결 설계안

- 작성 기준: `/home/jgo/longshort_platform` main@6a70f37, 정적 분석만(빌드/실행 없음). 저장소 무수정.
- 근거 표기: `파일:줄`. 추정은 `[추정]`.

---

## 1. 현재 엔진의 데이터 계약

### 1.1 입력 스키마 (long format 단일 parquet)

| 항목 | 값 | 근거 |
|---|---|---|
| 파일 수 | **1개**. `datasets[0]`만 읽고 나머지는 무시 | `src/data/config_loader.cpp:15` (`datasets[0]`) |
| 경로 해석 | `config.yaml`의 부모 디렉터리 기준 상대경로 | `src/main.cpp:118-119` |
| 포맷 | long(tidy): 1행 = (시각, 심볼) | `src/data/parquet_reader.cpp:88-108` |
| 시간 컬럼 | `time_col`, **int64 고정**(epoch_ms 가정). `Int64Array`로 무조건 static_cast | `parquet_reader.cpp:95, 161` |
| 심볼 컬럼 | `symbol_col`, STRING / LARGE_STRING만 허용 | `parquet_reader.cpp:21-30` |
| 값 컬럼 | `use_col` 리스트. INT64 / DOUBLE / FLOAT 허용 → 전부 **float32로 다운캐스트** | `parquet_reader.cpp:149, 197-211` |
| **필수 컬럼(하드코딩)** | `open, high, low, close, volume` 5개가 `use_col`에 없으면 예외 | `parquet_reader.cpp:66-73` |
| 로드 방식 | `ReadTable()`로 **파일 전체를 메모리에** 올린 뒤 패널로 재배치 | `parquet_reader.cpp:50-53` |
| 시간축 | 등장하는 unique timestamp를 `std::map`으로 오름차순 정렬 | `parquet_reader.cpp:88, 114` |
| 심볼축 | unique symbol을 `std::map<std::string,...>`으로 **사전순 정렬** | `parquet_reader.cpp:89, 118` |
| 결측 | 패널을 NaN으로 채운 뒤 존재하는 행만 덮어씀 → 미등장 (t,n)은 NaN | `parquet_reader.cpp:133-136` |
| 결과 | `DataPanel{T, N, timestamps[T], symbols[N], arrays[col] = float[T*N] row-major}` | `src/data/data_panel.h:8-18` |
| start/end_date | **로더가 무시**. `backtest.start_date/end_date`는 어디서도 필터에 안 쓰임 | `config_loader.cpp:35-36`, `main.cpp:120-126` |

`config.yaml` 현재 값: `Data/ohlcv/um_1d.parquet`, `time_col=open_time`, `symbol_col=symbol`, `use_col=[open,high,low,close,volume]`, `transaction_fee: 5`(bps → `×1e-4` = 5e-4 편도) — `config.yaml:2-17`, `config_loader.cpp:40-41`.
저장소에 `Data/`는 없음(gitignore).

### 1.2 알파 DSL이 접근 가능한 데이터

| 항목 | 값 | 근거 |
|---|---|---|
| 파이프라인 첫 태그 | 반드시 `<data>` | `src/compiler/dag_builder.cpp:245-246` |
| `expr` 기본값 | `"close"` | `dag_builder.cpp:250, 283` |
| `expr` 문법 | 식별자 / 숫자 / `+ - * /` / 괄호 / 단항 `-`만. 함수 호출 없음 | `src/compiler/expr_parser.cpp:16-53, 107-137` |
| **허용 식별자(하드코딩)** | `open, high, low, close, volume` + 의사 식별자 `returns` | `expr_parser.cpp:167-175` |
| `returns` 확장 | `close / Chain(close, Delay(1)) - 1.0` (즉 종가 1일 수익률) | `expr_parser.cpp:140-156` |
| `<data delay="d">` | `d>0`이면 `MDelay(d)`를 뒤에 붙임. 0 ≤ d ≤ T | `dag_builder.cpp:259-273, 291-304` |
| 등록 모듈 태그 | `tsmean, tszscore, tsrank, decay, delay, rank, zscore` + 특수 `data, signal, event, input` | `dag_builder.cpp:172-219, 281-388` |
| 유니버스/마스크 개념 | **없음**. 소스 전체에 universe/mask 관련 코드 부재 | grep 결과 (`src/`, `include/`에 해당 개념 없음) |

### 1.3 시뮬레이터가 소비하는 컬럼

`src/engine/simulator.cpp` (mode=1 D1만 사용 — `main.cpp:293, 319, 532, 740, 790`):

| 단계 | 식 | 근거 |
|---|---|---|
| 정규화 마스크 | `isfinite(close[di][i]) && isfinite(signal[i])` | `simulator.cpp:62, 70` |
| target | `signal / Σ|signal|` (마스크 통과분만) | `simulator.cpp:60-73` |
| 체결 가중 | `exec_w = prev_target` (= `target[di-1]`) | `simulator.cpp:76, 115-117` |
| 수익 | `ret[di] += prev_weights[i] · (close[di][i]/close[di-1][i] − 1)`, 단 `prev_close>0 && close>0` | `simulator.cpp:79-87` |
| 비용 | `ret[di] −= Σ|exec_w − prev_weights| · fee_rate` | `simulator.cpp:90-94` |
| min_bet | `Σ|exec_w| / N` — **N 전체로 나눔** (유니버스 크기에 직접 의존) | `simulator.cpp:108` |

**소비 컬럼은 `"close"` 단 하나이며 문자열이 하드코딩**됨 — `simulator.cpp:58, 80, 153, 175`.
`open/high/low/volume`은 로더의 필수 검증(`parquet_reader.cpp:67`)과 `expr` 허용목록(`expr_parser.cpp:169`)에만 존재하고 시뮬레이터는 쓰지 않음.

거래가능 판정 = "close가 finite인가" 뿐. 상장폐지/유니버스 이탈 시 close를 NaN으로 두면:
- `target=0`이 되어 다음 날 exec_w가 0 → 청산 비용은 정상 부과 (`simulator.cpp:92`)
- 그러나 이탈 당일/다음날의 보유수익은 `close>0` 조건에서 **탈락 → 0 수익 처리** (`simulator.cpp:82`). 실제 −100% 상장폐지 손실을 반영하지 못함.

### 1.4 메트릭 / JSON 계약

| 항목 | 값 | 근거 |
|---|---|---|
| 누적 방식 | **additive(단리)**, 고정 북사이즈 | `docs/math_contract.md`, `src/metrics/timeseries_output.cpp:43` |
| bars_per_year | **365 하드코딩** (const + 호출부 리터럴) | `src/metrics/summary_metrics.h:5`, `yearly_metrics.h:24`, `metrics_engine.h:31`, `main.cpp:294, 307, 320, 533, 544, 741, 753, 791, 803` |
| IS/OS 분할 | timestamp 비교(`>= is_ms`, `>= os_ms`), 요약은 bar 0 제외 | `src/metrics/metrics_engine.cpp:24-38, 51-66` |
| JSON 단위 | 비율 → pp(×100) | `src/api/json_serializer.cpp:17-34, 45` |
| timeseries | bar 0 제외, 길이 T−1 | `timeseries_output.cpp:23, 36` |
| gross 복원 | `r + turnover·fee_rate` | `timeseries_output.cpp:47` |

### 1.5 프론트가 소비하는 백엔드 필드 (엔진측)

| 엔드포인트 | 노출 | 근거 |
|---|---|---|
| `GET /api/config` | `symbols[]`(전체 심볼 문자열 배열), `simStart/simEnd`(패널 첫/마지막 ts), `feeRate`, `barsPerYear=365`, `T`, `N`, `columns[]`(=`use_col`) | `main.cpp:168-215` |
| `POST /api/simulate` | `batches[]`(summary/yearly/timeseries), 단일 알파면 최상위 `signalPlot`, `signalRange`, `distribution` | `main.cpp:560-640` |
| `signalPlot` | `{timestamps[T'], symbols[N], values[T'][N]}` — **T'×N 전체 시그널 행렬을 JSON 배열로** | `json_serializer.cpp:97-129` |
| plot 시작점 | `simStart` 지정 시 그 시점, 없으면 IS 시작 | `main.cpp:597-612` |
| 기본 IS/OS | 요청 바디 기본값 `isStart="2021-01-01"`, `osStart="2024-01-01"` (크립토 기간) | `main.cpp:235-236, 671-672` |
| 서버 | drogon, `LONGSHORT_PORT` 기본 8000, worker 4 | `main.cpp:940-948` |

`universe:` 키는 `config_loader.cpp`가 **읽지 않음**(파싱 대상은 `datasets[0]`과 `backtest`뿐) — 현재 유니버스 선택 수단은 사실상 없음.

---

## 2. 우리 캐시 실체 (`/home/base/cache/cache_v1_cash_1d`)

### 2.1 레이아웃

wide parquet, **필드 1개 = 파일 1개**. 행=날짜, 열=`tradingItemId`(문자열), 값=double.

| 디렉터리 | 파일 | 크기 |
|---|---|---|
| `BaseData/` | open, high, low, close, volume, VWAP, bid, mid, ask, **midPrice**, marketCap_USD, currentPrimary, TimeStamp, tradingItemId (14 + METADATA.yml) | ~2.4GB |
| `Returns/` | HoldingReturn_Usd(429MB), TradingReturn_Usd(411MB), OpenToCloseReturn_Usd(374MB), VWAPToCloseReturn_Usd(385MB) | ~1.6GB |
| `TopUniverseV1/` | ALL, Top1000, Top700, Top500, Top300, Top200, Top100, Top10 (bool, 각 ~18MB) | ~144MB |
| `sector/` | sector.parquet(21.6MB, **double 코드값**), sectorIx.parquet | — |
| 기타 | AflData, AflData_M, DivData, EstData, ETFData, FFD, FFD_stock_d, FundData | — |

**Top400/Top600/Top800/Top900/Top3000 플래그는 `TopUniverseV1/`에 없음** (루트에 축소사본 디렉터리만 존재).

### 2.2 스키마 (실측 `pq.ParquetFile`)

| 파일 | rows | cols | row_groups | dtype | 파일 크기 |
|---|---:|---:|---:|---|---:|
| `BaseData/close.parquet` | 11,811 | 28,208 | 1 | double + `__index_level_0__` timestamp[ns] | 204.3MB |
| `Returns/HoldingReturn_Usd.parquet` | 11,811 | 28,208 | 1 | double | 429.0MB |
| `TopUniverseV1/Top500.parquet` | 11,811 | 28,208 | 2 | **bool** | 18.0MB |
| `TopUniverseV1/Top1000.parquet` | **11,812** | 28,208 | 2 | bool | 18.0MB |

- 인덱스 컬럼명 = `__index_level_0__`, `timestamp[ns]`. 날짜 1980-01-02 ~ 2026-09-08.
- 컬럼명 = `tradingItemId` 문자열 (예 `62311882`, `2590360`=AAPL).
- created_by `parquet-cpp-arrow 19.0.1`.
- **컬럼 선택 읽기가 저렴함**: 단일 row_group 204MB 파일에서 1,020컬럼 선택 = **0.3초**(실측), float32 변환 0.1초.

### 2.3 정의 (팀 sim 원문)

팀 시뮬레이터(/home/base/git/sim) 소스 원문 기준 — 이것이 parity ground truth임.

`/home/base/git/sim/utils/sim_util/handlers.py`

전처리 `_check_dims()` (:375-409):
- `hr/tr/opr`를 `pos`의 index·columns로 reindex (`:390, 393, 396`)
- **비유한값 → 0.0** (`:391, 394, 397`) ← 0.0-fill 함정의 소스
- **마지막 행 → NaN** (`:392, 395, 398`)
- `pos` 비유한값 → 0.0 (`:409`)

`run()` (:411-436) 원문:
```python
tvr = pos.diff(1)
tvr.iloc[0] = pos.iloc[0]          # 첫날 전 포지션 일괄 진입
shifted_pos = pos.shift(1)
shifted_pos.iloc[0] = 0.0
pnl  = (shifted_pos*self.hr + tvr*self.tr  - tvr.abs()*self.cost).sum(axis=1)
pnl2 = (shifted_pos*self.hr + tvr*self.opr - tvr.abs()*self.cost).sum(axis=1)
pos_abs  = pos.abs().sum(axis=1)
bksz     = pos_abs
ret      = pnl / bksz                        # 당일 booksize로 나눔
bksz_tvr = pos_abs.rolling(2).mean()         # 턴오버 분모는 2일 평균 booksize
tvr_pct  = tvr.abs().sum(axis=1) / bksz_tvr
```
- FundingReturns 설정 시 `pnl -= (pos*fr).sum(axis=1)` (`:424`)
- `cost` = `StatsHandlerConfig.TradingCost`, 기본 0.0 (`typedef.py:151`), **|Δpos| 1회당** 부과(편도)

수익률 정의 원문 `/home/base/git/sim/modules/Returns.py:62-84`:

| 필드 | 정의 | 줄 |
|---|---|---|
| `HoldingReturn_Usd` | `close.pct_change(1, fill_method='ffill')` = `close[t]/close[t−1] − 1` | :64 |
| `TradingReturn_Usd` | `close[t] / midPrice[t] − 1`, **`.fillna(0)`** | :67 |
| `OpenToCloseReturn_Usd` | `close[t] / open[t] − 1`, `.fillna(0)` | :70 |
| `VWAPToCloseReturn_Usd` | `close[t] / VWAP[t] − 1`, `.fillna(0)` | :73 |

- 전 종목 NaN인 날(휴장)은 행에서 제거 후 계산 (`Returns.py:63, 66`)
- `Delay>0`이면 **마지막 시점 행을 NaN**으로 (`Returns.py:75-79`)
- `midPrice = (open+high+low+close)/4` — **호가 mid가 아니라 OHLC4**
  (`modules/BaseData_uss_v1.py:100`, `vendor/uss/daily_uss_stock_split.py:105`).
  `bid/mid/ask`는 별도 컬럼으로 존재하지만 TradingPrice로 쓰이지 않음.
- 가격 원천 = `xfl_target.miadjprice`(**split 조정가**), split 발생 시 전 이력 재작성 잡 존재
  (`vendor/uss/daily_uss_stock_split.py:69-113`)
- **배당은 close에 미반영** — DivData가 별도 데이터셋(`modules/DivData_uss_v1.py`, 캐시 `DivData/`)
  → `HoldingReturn_Usd`는 배당 제외 순수 가격수익률

파이프라인 설정 원문 `/home/base/git/sim/data_cache_configs/uss_cash_returns_1d.yml`:
```yaml
Delay: 1
HoldingPrice: BaseData.close
TradingPrice: BaseData.midPrice
OpenToClosePrice: BaseData.open
VWAPToClosePrice: BaseData.VWAP
SecmasterPath: ${VENDOR_RAW_DATA}/uss/cash/master/uss_cash_master_acc.parquet
```
- 캐시 컬럼 키 = secmaster index = **`tradingItemId`** (`handlers.py:152-153`, `BaseData_uss_v1.py:67`)
- `Delay:1` → BaseData/Returns 모두 **마지막 delay일 행을 NaN 마스킹** (`BaseData_uss_v1.py:88-89`)

`TopUniverseV1` 마스크 정의 `/home/base/git/sim/modules/TopUniverse_uss_v1.py:69-110` +
`data_cache_configs/uss_cash_topuniv_1d.yml`:
- 100일 평균 달러거래대금 > 3e6, 21일 중앙값 시총 > 1e7, `currentPrimary` 참
- 21일 랭크 윈도우 liq_score 상위 usize, sticky(진입 60일 / 이탈 10일), TokensToRemove 46종 강제 제외
- 결과는 bool, `fillna(False)` → **PIT 일별 마스크**

### 2.4 크기 실측 (유니버스 union)

`TopUniverseV1/Top{500,1000}.parquet` 전량 로드 후 실측.

| 유니버스 | 구간 | T | N_union | 일별 True 평균 | p05 | p95 | max |
|---|---|---:|---:|---:|---:|---:|---:|
| Top500 | 1980~ | 11,811 | 1,965 | 397.1 | — | — | 473 |
| Top500 | 2010~ | 4,195 | **1,020** | **457.4** | 446 | 468 | 472 |
| Top500 | 2015~ | 2,937 | 887 | 454.6 | 446 | 463 | 467 |
| Top500 | 2020~ | 1,679 | 723 | 452.1 | 444 | 460 | 465 |
| Top1000 | 2010~ | 4,196 | **2,249** | **926.0** | 906 | 940 | 946 |
| Top1000 | 2015~ | 2,938 | 1,937 | 922.6 | 905 | 939 | 945 |
| Top1000 | 2020~ | 1,680 | 1,570 | 916.1 | 902 | 931 | 937 |

필드 1개 = T×N float32:

| 유니버스 | 구간 | T×N cells | **f32 1필드 MB** | 엔진 필수 5컬럼 MB | 8컬럼(+hr,tr,valid) MB |
|---|---|---:|---:|---:|---:|
| Top500 | 2010~ | 4,278,900 | **17.1** | 85.6 | 137.0 |
| Top500 | 2015~ | 2,605,119 | 10.4 | 52.1 | 83.4 |
| Top1000 | 2010~ | 9,436,804 | **37.7** | 188.7 | 302.0 |
| Top1000 | 2015~ | 5,690,906 | 22.8 | 113.8 | 182.1 |

long-format 변환 시 행 수 (ETL 산출물 규모):

| 유니버스 | 구간 | dense 행 수 (T×N_union) | sparse 행 수 (유니버스 내부만) |
|---|---|---:|---:|
| Top500 | 2010~ | 4,278,900 | **1,918,853** |
| Top1000 | 2010~ | 9,436,804 | **3,885,496** |

`ReadTable()`이 파일 전체를 올린 뒤 패널을 **추가로** 할당하므로 피크는 두 배 이상 [추정]:
long 8컬럼 dense Top1000 2010~ → Arrow 테이블 ~0.8GB + 패널 0.30GB ≈ **1.1GB 피크** [추정].

### 2.5 secmaster (tradingItemId → ticker)

**실제 경로 = `/home/base/vendor_data/uss/cash/master/uss_cash_master_acc.parquet`** (1.59MB, 로컬 디스크).
임무에 적힌 `/data2/vendor_data/uss/...`, `/home/jgo/61/data2/vendor_data/uss/...`는 **둘 다 부재**(확인함).

| 항목 | 값 |
|---|---|
| rows | **28,207** (= 캐시 데이터 컬럼 수와 정확히 일치) |
| index | `tradingItemId` int64 |
| 컬럼 | companyId, companyName, primaryFlag, primaryFlagTi, **symbol**(string), exchangeId, ISOCode, marketCap_USD, pricingDate, securityStartDate, securityEndDate, currentPrimary, simpleIndustryDescription, sector, sharesOutstanding |
| 조인 | 캐시 컬럼 ∩ secmaster = **28,207 / 28,207, 양쪽 여집합 0, 중복 0** |
| 함정 | `symbol` **null 19.8%(5,597)**, `sector` null 624. 스냅샷 1장이라 티커 변경 이력은 옆 `history_acc/`(12,069 일자별 파일) |

스팟체크: AAPL=2590360, MSFT=2630413, NVDA=2635154, TSLA=108803915.

### 2.6 함정 (실측)

| # | 함정 | 실측 |
|---|---|---|
| T1 | **Returns 0.0-fill은 "상폐 이후"에만** — 상장 전은 NaN | 상장 전 구간: ret NaN 100.0% / 0.0 0.0%. 상폐 후 구간: **ret 0.0 99.98%**. 상장 내부: 0.0 19.3% |
| T2 | close 유효 구간에서는 ret이 NaN이 되지 않음 | Top500 멤버 2010+ : close 유효 셀의 ret NaN **0.0000**, ret 0.0 0.75%(진짜 보합) → **`close.notna()`를 유효 마스크로 써도 안전** |
| T3 | close NaN 비율 | 무작위 20열 전기간 85.1%, Top500 멤버 2010+ **16.1%** |
| T4 | **TokensToRemove 46종이 전 유니버스에서 영구 제외** | Top500/Top1000/**ALL** 모두에서 JPM(2622876)·GS(2615898)·HON(2618950) **전 기간 True 0일**. 일별 평균 457.4≈500−46, 926.0≈1000−46 |
| T5 | **Top500과 Top1000의 인덱스가 다름** | Top1000 11,812행 vs Top500 11,811행 — 차이는 **2026-07-03** 1일. 두 플래그를 같은 인덱스로 가정하면 어긋남 |
| T6 | 마지막 행 전 컬럼 False | 2026-09-08은 미채움 → **실효 마지막일 2026-09-04**. T에서 1 빼고 쓸 것 |
| T7 | sector 코드가 표준 GICS 순서 아님 | `sectorIx` 실측: 0 None, 1 Energy, 2 IT, 3 Industrials, 4 Materials, 5 Financials, 6 ConsDisc, 7 ConsStaples, 8 RealEstate, 9 CommSvc, 10 HealthCare, 11 Utilities |
| T8 | `HoldingReturn_Usd`는 배당 미반영 | 가격 원천 `xfl_target.miadjprice`(split 조정), 배당은 별도 `DivData/` |
| T9 | `midPrice`는 호가 mid가 아님 | `(open+high+low+close)/4`. 호가 `bid/mid/ask`는 별도 컬럼이나 TradingPrice로 미사용 |

### 2.7 Top-N 축소 사본 (`Top500/`, `Top1000/` 디렉터리)

하위 = `AflData / BaseData / FFD / Returns` 4개 (Top100/200 동일, 나머지는 FFD 없이 3개).

| 경로 | rows | cols | 날짜 범위 | 크기 |
|---|---:|---:|---|---:|
| 원본 `BaseData/close` | 11,811 | 28,207 | 1980-01-02~2026-09-08 | 204.3MB |
| `Top500/BaseData/close` | **6,710** | **1,461** | **2000-01-03**~2026-09-08 | 29.5MB |
| `Top500/Returns/HoldingReturn_Usd` | 6,710 | 1,461 | 동일 | 57.8MB |
| `Top1000/BaseData/close` | 6,710 | **3,188** | 동일 | 53.4MB |
| `Top1000/Returns/HoldingReturn_Usd` | 6,710 | 3,188 | 동일 | 107.9MB |
| `Top3000/BaseData/close` | 6,509 | 7,829 | 2000-01-03~**2025-11-17 (stale)** | 94.6MB |

- 축소율 28,207 → 1,461(Top500, 5.2%) / 3,188(Top1000, 11.3%), 행도 2000년 시작으로 절단.
- `Top500/AflData/*`는 601개 파일이나 **rows=320** — 일봉이 아닌 별도 주기 [추정].
- Top3000 사본은 stale이므로 사용 금지.

---

## 3. 연결 옵션 비교

| | **A. Python ETL → long parquet** | **B. C++ 로더가 wide 다중 파일 직접 읽기** | **C1. 축소사본(`Top500/`) 활용** | **C2. Python 사이드카 서비스** |
|---|---|---|---|---|
| 개요 | 캐시 → 마스킹/정렬 → 기존 계약의 long parquet 1개 산출 | `parquet_reader.cpp`를 wide 다중 파일 리더로 교체, 컬럼 선택 로드 | A/B의 입력을 `Top500/` 사본으로 대체 | 파이썬이 패널을 만들어 Arrow IPC/소켓으로 C++에 공급 |
| 변경 파일 (C++) | **0개**(1단계) / 6개(2단계, 5절) | `parquet_reader.{h,cpp}` 전면, `config_loader`, `data_panel` | A와 동일 | `main.cpp`, `parquet_reader`, 신규 IPC 계층 |
| 변경 파일 (도구) | 신규 ETL 1개 | 없음 | 신규 ETL 1개 | 신규 서비스 |
| 공수 [추정] | **0.5~1일**(1단계 light-up), +2~3일(2단계) | **4~7일** + Arrow API 디버깅 | A 대비 −0.2일 | 5~8일 |
| 빌드 검증 | **로컬 빌드 불필요**(1단계) | **로컬에 Arrow/Drogon 없어 검증 불가** — 최대 리스크 | 동일 | 검증 불가 |
| 성능(로드) | long 4.3M~9.4M행 파싱, 행별 map lookup. 수~수십 초 [추정] | 컬럼 선택 읽기 **0.3초/필드**(실측) — 압도적 | A/B 각각의 1/3 규모 | 파이썬 측 빠름, IPC 오버헤드 |
| 성능(메모리 피크) | Arrow 테이블 + 패널 이중 할당 ≈ 1.1GB(Top1000 8컬럼) [추정] | 패널만 ≈ 0.3GB | 1/3 | 이중 + 프로세스 2개 |
| 디스크 | 신규 산출물 (Top500 2010~ 8컬럼 long ≈ 수백 MB) | **0** | 신규 산출물(작음) | 0 |
| 재현성 / parity | **최상** — ETL 산출물이 고정 아티팩트. 팀 sim과 같은 입력을 파이썬에서 그대로 재현·대조 가능 | 중 — C++ 내부 마스킹 로직을 팀 규약과 맞춰야 하고 대조가 어려움 | A와 동일 | 중 |
| 유니버스 교체 | ETL 재실행 (분 단위) | config 한 줄 | ETL 재실행 | 서비스 재기동 |
| 위험 | 로드 시간·피크 메모리, 산출물 관리 | Arrow C++ 대량 컬럼 API, 인덱스 컬럼(`__index_level_0__` timestamp[ns]) 처리, 검증 불가 | 사본이 2000년~ / 특정 Top-N에 고정, Top3000 stale | 운영 복잡도 2배 |
| 기존 크립토 경로 | **그대로 유지**(datasets 교체만) | 리더 교체로 회귀 위험 | 유지 | 유지 |

### 추천: **A (Python ETL → long parquet)**, 2단계로 진행

근거:
1. **1단계는 C++ 변경 0으로 불이 켜짐.** 기존 계약이 요구하는 `open/high/low/close/volume`(`parquet_reader.cpp:66-73`)이 캐시 `BaseData/`에 전부 실재하므로, ETL이 스키마만 맞추면 `config.yaml` 한 줄 교체로 즉시 구동됨. 로컬에 빌드 환경이 없는 현 상황에서 **검증 불가능한 C++ 변경을 선행시키지 않는 것이 결정적**.
2. **parity 검증이 파이썬 쪽에 남음.** 팀 `StatsHandler`와 같은 언어·같은 pandas 연산으로 입력을 만들 수 있어, 엔진 결과와의 차이가 "데이터"인지 "P&L 모델"인지 분리 가능(6절 시험 설계).
3. B의 성능 이점(0.3초 vs 수십 초)은 실재하나, **로드는 서버 기동 시 1회**뿐이라 ROI가 낮음. B는 A로 계약을 확정한 뒤 성능 개선으로 뒤에 붙이는 것이 안전함.
4. C1은 A의 **입력 최적화**로만 채택 — 축소사본이 2000년~/고정 Top-N이고 Top3000은 stale이라 단독 안은 부적합. 단 Top500/Top1000 사본은 원본과 값이 동일하므로 ETL 입력으로 쓰면 읽기량이 1/7로 줄어듦.
5. C2는 프로세스가 하나 더 늘어 운영·결정론 비용이 큼. 기각.

---

## 4. 추천안 상세 스펙

### 4.1 panel 계약 (ETL 산출물 스키마)

| 컬럼 | dtype | 정의 | 필수 이유 |
|---|---|---|---|
| `open_time` | int64 | **epoch_ms, UTC 자정** (`date.tz_localize('UTC').value // 1e6`) | 로더가 Int64Array로 무조건 캐스트 (`parquet_reader.cpp:95,161`) |
| `symbol` | string | **`tradingItemId` 문자열** (아래 근거) | STRING/LARGE_STRING만 허용 (`parquet_reader.cpp:21-30`) |
| `open`,`high`,`low`,`volume` | double | `BaseData/{open,high,low,volume}` | 하드코딩 필수 검증 (`parquet_reader.cpp:66-73`) |
| `close` | double | `BaseData/close` | 시뮬레이터가 유일하게 소비 (`simulator.cpp:58,80`) |
| `hr` | double | `Returns/HoldingReturn_Usd` | 2단계 P&L (5절 b) |
| `tr` | double | `Returns/TradingReturn_Usd` | 2단계 trading leg (5절 a) |
| `valid` | double | `TopUniverseV1/Top{N}` → 1.0 / NaN | 2단계 거래가능 판정 (5절 d) |

**심볼 키는 `tradingItemId`를 채택.** 근거:

| 후보 | 판정 |
|---|---|
| `tradingItemId` | **채택**. 캐시 컬럼 키 그대로(무손실), secmaster 조인 28,207/28,207 완전 일치, 시간에 대해 불변, 사전순 정렬(`parquet_reader.cpp:118`)이 결정론적 |
| `ticker(symbol)` | **기각**. secmaster `symbol`이 **null 19.8%**, 티커 재사용/변경 이력 존재(`history_acc/` 12,069 파일) → 키 충돌·시간 비일관 |
| `"TICKER.id"` 합성 | 기각. 정렬·조인 의미가 깨지고 파싱 규약이 추가됨 |

티커는 **표시용 라벨**로 분리 공급 — ETL이 `symbols.csv`(`tradingItemId, ticker, companyName, sector, sectorIx`)를 함께 산출하고, 프론트가 이를 읽어 히트맵/검색에 표시(4.5).

시간축은 **UTC 자정 epoch_ms**. 캐시 인덱스가 tz-naive `timestamp[ns]`이므로 UTC로 고정 해석해야 `/api/config`의 `gmtime_r` 기반 날짜 문자열(`main.cpp:180-187`)과 IS/OS 경계 비교(`metrics_engine.cpp:24-38`)가 어긋나지 않음.

### 4.2 ETL 도구 스펙

위치 제안: `tools/etl/build_uss_panel.py` (저장소 신규 파일 — 이번 작업에서는 **작성하지 않음**).
실행: `/home/jgo/usr/venv-3.11/bin/python`.

입력:
- `/home/base/cache/cache_v1_cash_1d/BaseData/{open,high,low,close,volume}.parquet`
- `/home/base/cache/cache_v1_cash_1d/Returns/{HoldingReturn_Usd,TradingReturn_Usd}.parquet`
- `/home/base/cache/cache_v1_cash_1d/TopUniverseV1/Top{N}.parquet`
- `/home/base/vendor_data/uss/cash/master/uss_cash_master_acc.parquet`
- (선택) `Top500/`·`Top1000/` 축소사본을 입력으로 쓰면 읽기량 1/7

출력:
- `<out>/panel.parquet` (long)
- `<out>/symbols.csv` (라벨 매핑)
- `<out>/manifest.json` (입력 파일 mtime/크기, 인자, T, N, 행 수, 체크섬)

CLI 인자:

| 인자 | 기본 | 설명 |
|---|---|---|
| `--universe` | `Top500` | `TopUniverseV1/` 플래그 이름 |
| `--start` / `--end` | `2010-01-01` / 실효 마지막일 | 기간 |
| `--columns` | `open,high,low,close,volume,hr,tr,valid` | 산출 컬럼 |
| `--layout` | `sparse` | `sparse`=유니버스 내부 행만 / `dense`=T×N_union 전부 |
| `--out` | — | 출력 디렉터리 |
| `--drop-tokens-removed` | `false` | TokensToRemove 46종을 union에서 제외할지(이미 마스크가 False라 무영향, 명시용) |

마스크 규칙 (순서 고정):
1. 날짜축 = `TopUniverseV1/Top{N}` 인덱스 ∩ `BaseData/close` 인덱스, `--start`~`--end` 절단, **마지막 행 제외**(T6).
2. 종목축 = 기간 내 `Top{N}` 마스크가 **한 번이라도 True**인 컬럼(= N_union).
3. 일별 유효 = `Top{N}[t,i] == True` **AND** `close[t,i]`가 유한 (T2에 따라 이 둘이면 충분).
4. `valid = 1.0` if 유효 else `NaN`.
5. **`close`는 유효하지 않은 곳을 NaN으로 덮어씀** — 이것이 1단계에서 유니버스 마스크를 엔진에 주입하는 유일한 수단(`simulator.cpp:62,70,82`).
6. `open/high/low/volume`도 동일 마스크 적용(무결성).

0.0-fill 처리 (T1):
- `hr`/`tr`은 **유효 셀만 남기고 나머지는 NaN으로 되돌림**. 상폐 이후 0.0 패딩이 그대로 들어가면 P&L·변동성이 희석됨.
- 유효 셀 내부의 0.0은 진짜 보합(0.75%)이므로 보존.

검증 체크리스트 (ETL이 자체 assert):

| # | 검사 | 기대 |
|---|---|---|
| V1 | `open_time` 유일·오름차순, UTC 자정 | 위반 0 |
| V2 | `symbol` 문자열, secmaster에 전부 존재 | 여집합 0 |
| V3 | 일별 유효 종목 수 | Top500 2010~ 이면 평균 457±, p05 446 / p95 468 |
| V4 | `valid==1`인 셀에서 `close` 유한 | 100% |
| V5 | `valid` NaN 셀에서 `close` NaN | 100% |
| V6 | `hr` 재계산 대조: `close[t]/close[t−1]−1` vs 캐시 `hr` | 유효 셀에서 일치(ffill 차이 허용 오차 명시) |
| V7 | 재실행 결정론 | 동일 인자 → parquet 바이트 동일 |
| V8 | T, N_union | 2.4 표와 일치 |

### 4.3 `config.yaml` 확장안

```yaml
datasets:
  - name: uss_top500
    path: Data/uss/top500_2010.parquet
    time_col: open_time
    symbol_col: symbol
    use_col: [open, high, low, close, volume, hr, tr, valid]

backtest:
  start_date: "2010-01-01"
  end_date:   "2026-09-04"
  initial_capital: 1000000
  transaction_fee: 1.5        # bps, 편도
  # --- 신규 (2단계) ---
  bars_per_year: 252
  price_col:  close           # 정규화 마스크/기본 수익률용
  return_col: hr              # holding leg. 미지정 시 close 비율
  trading_return_col: tr      # trading leg. 미지정 시 미사용
  valid_col:  valid           # 거래가능 판정. 미지정 시 price_col 유한성
  mode: 0                     # 0=D0(팀 규약과 정합), 1=D1(현행)
```

전부 **optional + 현행 기본값**으로 두어 크립토 config가 무변경으로 동작하게 함.
`start_date`/`end_date`는 현재 로더가 무시하므로, 실제 기간 절단은 ETL이 담당(엔진에서 필터를 켜려면 별도 변경 필요 — 4.4 D 참조).

### 4.4 C++ 변경 목록 (코드 미작성, 최소 diff 설명)

| # | 파일:줄 | 변경 | 단계 |
|---|---|---|---|
| A | `src/data/config_loader.h:6-16`, `.cpp:29-42` | `BacktestConfig`에 `bars_per_year, price_col, return_col, trading_return_col, valid_col, mode` 필드 추가 + `backtest` 섹션에서 optional 파싱(기본값 = 현행) | 2 |
| B | `src/data/parquet_reader.cpp:66-73` | `open/high/low/close/volume` 하드코딩 필수 검증을 **제거하거나** "config가 지정한 컬럼만 필수"로 완화 | 2 |
| C | `src/data/parquet_reader.cpp:95, 161` | `time_col`을 `Int64Array`로 **무검사 static_cast** → `timestamp[ns]` 등이 오면 UB. `type_id()` 확인 후 미지원 타입은 명시적 예외 | 2 (안전성) |
| D | `src/data/parquet_reader.cpp` / `main.cpp:120-126` | (선택) `start_date/end_date`로 행 필터 — 현재 완전히 무시됨 | 3 |
| E | `src/engine/simulator.cpp:58,80,153,175` | `"close"` 리터럴 4곳 → `config_.price_col`. 정규화 마스크(`:62,70`)는 `valid_col` 우선, 없으면 현행 | 2 |
| F | `src/engine/simulator.cpp:79-87` | `return_col` 지정 시 `ret[di] += prev_weights[i]·hr[di][i]` 경로 추가(기존 close 비율은 fallback) | 2 |
| G | `src/engine/simulator.cpp:89-94` | `trading_return_col` 지정 시 `ret[di] += Σ(exec_w−prev_weights)·tr[di][i]` 항 추가 | 2 |
| H | `src/engine/simulator.cpp:76,115-120` | `mode`를 config에서 받도록(현재 `main.cpp`가 항상 1로 덮어씀) | 2 |
| I | `src/main.cpp:203,294,307,320,533,544,741,753,791,803` | `365` 리터럴 10곳 → `g_config.bars_per_year` | 2 |
| J | `src/metrics/summary_metrics.h:5` | `BARS_PER_YEAR=365`는 **기본값 상수로만** 유지(호출부는 전부 config 경유) | 2 |
| K | `src/compiler/expr_parser.cpp:167-175` | 허용 식별자 하드코딩 배열 → `panel->arrays`의 키 집합 조회로 교체. `returns` 의사 식별자는 `return_col`이 있으면 그것으로 확장(`:140-156`) | 2 |
| L | `src/api/json_serializer.cpp:97-129` + `src/main.cpp:597-638` | `signalPlot` 상한: 심볼 top-K / 시간축 stride 다운샘플. 요청 파라미터로 노출 | **1 (필수)** |
| M | `src/main.cpp:235-236, 671-672` | 요청 기본 `isStart/osStart`("2021-01-01"/"2024-01-01") → 패널 범위 기반 산출 또는 config | 3 |
| N | `src/main.cpp:168-215` | `/api/config`에 `symbolLabels`(또는 별도 `GET /api/symbols`) 추가 — tradingItemId → ticker/이름/섹터 | 2 |
| O | `src/engine/simulator.cpp:108` | `min_bet = Σ|exec_w|/N` — N이 17→1020이 되면 의미가 붕괴. "유효 종목 수"로 나누거나 지표 재정의 | 3 |

**1단계(불 켜기)에 실제로 필요한 C++ 변경은 L 하나뿐**이며, L조차 요청 시 `collectSignals`를 끄면(현재 프론트가 단일 알파에서 항상 받음) 회피 가능 → 엄밀하게는 **0 변경으로 구동 가능**.

### 4.5 프론트 변경 목록

| 우선 | 항목 | 위치 |
|---|---|---|
| **선행** | 부재 모듈 6개 복구 (`lib/{themeColors,colorScale,metricColors,uplotUtils,uplotAdapters,snapshotColors}.ts`) — 없으면 빌드 불가 | `frontend/lib/` |
| P0 | `signalPlot` 다운샘플 수신 + 값 개수 가드 | `lib/api.ts:194, 221-241` |
| P0 | 히트맵 심볼축 가상화(보이는 행만) + `getImageData` 전면 백업 제거(오버레이 분리 캔버스) | `components/visualization/SignalHeatmap.tsx:113-215, 211, 236-242` |
| P0 | `colorPalettes` 캐시를 `sortedLen`별 → **고정 256단계 분위수 팔레트**로 교체 (주식은 일별 유효수가 달라져 키가 최대 N개로 폭증) | `SignalHeatmap.tsx:154-179, 201` |
| P1 | 컨테이너 `max-h-48`(192px) 상향 + 심볼 라벨/섹터 그룹핑, `symbolLabels` 소비 | `SignalHeatmap.tsx:350`, `types/index.ts:81` |
| P1 | 검색 필터 `useMemo` + 결과 `slice(0,50)`, 심볼 단독 선택 시 행 하이라이트 | `SignalHeatmap.tsx:69-72, 101, 318-325, 361-374` |
| P1 | 날짜 기본값 제거 → config 기반 산출 일원화. `outOfRange` 판정에 "기본값 미변경" 조건 추가(2019~ 패널이면 크립토 분할이 그대로 통과됨) | `store/alphaStore.ts:69-76, 218-232` |
| P2 | `barsPerYear` 표시(365 vs 252 혼동 방지). 현재 타입에만 있고 읽는 코드 0건 | `SettingsBar.tsx:120-129`, `types/index.ts:85` |
| P2 | `/api/simulate` 타임아웃(`AbortSignal.timeout`) | `lib/api.ts:221-241` |
| P2 | `SignalChart` 좌측 T행 테이블 가상화, `:265`의 하드코딩 365 → barsPerYear 연동 | `SignalChart.tsx:76-87, 265-266` |
| P2 | 유니버스/기간 표시 위젯(현재 화면 정보는 `N` 숫자 하나뿐) | `SettingsBar.tsx:127` |

**signalPlot을 손대지 않으면 1단계에서 바로 터짐**:

| 시나리오 | 값 개수 | JSON [추정, 15B/값] | fillRect 호출 |
|---|---:|---:|---:|
| 현재 17×2070 | 3.5만 | ~0.5MB | 3.5만 |
| Top500 2010~ (1020×4195) | 4.28M | **~64MB** | 4.28M (×122) |
| Top1000 2010~ (2249×4196) | 9.44M | **~142MB** | 9.44M (×270) |

---

## 5. P&L 모델 확장 필요사항

### 5.0 대응관계 (먼저 확정)

엔진의 `exec_w[t]`가 팀의 `pos[t]`, `prev_weights[t]`가 팀의 `pos[t−1]`에 **정확히** 대응함 — **D0/D1 두 모드 모두**.

| 모드 | `exec_w[t]` | `prev_weights[t]` | 근거 |
|---|---|---|---|
| D0 (mode=0) | `target[t]` | `target[t−1]` | `simulator.cpp:76, 118-120` |
| D1 (mode=1) | `target[t−1]` | `target[t−2]` | `simulator.cpp:76, 115-117` |

첫 bar도 일치: 엔진 `di=0`은 `prev_weights=0, turnover=Σ|target[0]|` (`simulator.cpp:30, 92`) = 팀 `shifted_pos[0]=0, tvr[0]=pos[0]` (`handlers.py:417-419`).
booksize도 일치: 엔진 target은 `Σ|target|=1`로 생성(`simulator.cpp:71`) → 팀 `ret = pnl/bksz`의 분모가 항상 1.

따라서
```
엔진 ret[t] = Σ pos[t−1]·(close[t]/close[t−1] − 1) − Σ|Δpos[t]|·fee
팀  pnl[t] = Σ pos[t−1]·hr[t] + Σ Δpos[t]·tr[t] − Σ|Δpos[t]|·cost
```
**차이는 정확히 두 가지: (i) `Σ Δpos·tr` 항의 부재, (ii) 가격비 vs `hr` 컬럼.**

정보시점: 팀은 `pos[t]`를 t−1까지 데이터로 계산(알파 모듈 내부 shift, `Delay`는 tail 마스킹일 뿐).
엔진 네이티브 알파는 내부 shift가 없으므로 **D1이 팀의 정보시점과 일치**함.
반대로 이미 shift된 AlphaPos를 주입할 때는 **D0**가 맞음.

### 5.1 (a) trading leg `Σ Δpos·tr` 추가

| 방안 | 내용 | 평가 |
|---|---|---|
| **컬럼 기반 (권장)** | config `trading_return_col: tr` 지정 시 `simulator.cpp:89-94`의 turnover 루프에서 `ret[di] += (exec_w[i] − prev_weights[i]) · tr_row[i]` 를 함께 누적 | 루프 재사용, 분기 1개. mode와 직교 |
| mode 확장(mode=2) | 새 모드로 별도 경로 | 모드 축과 수익률 축이 섞여 조합 폭발. **기각** |

주의: `tr`은 **비용이 아니라 부호 있는 수익률 항**임(`tr[t]=close[t]/OHLC4[t]−1`). 절대값을 씌우면 안 됨.
크기는 미측정 — **parity 시험(6절 P-c)에서 이 항만 켰다 껐다 하며 기여도를 실측할 것**. 사전 추정치를 쓰지 말 것.

### 5.2 (b) 수익률을 `close` 비율 대신 `hr` 컬럼으로

| 항목 | 엔진 현행 | `hr` 사용 시 |
|---|---|---|
| 정의 | `close[t]/close[t−1]−1`, `prev_close>0 && close>0`일 때만 (`simulator.cpp:82-84`) | `Returns/HoldingReturn_Usd` |
| 결측 구간 | close 1일 결측 → **그날과 다음날 2일치 수익이 통째로 소실** | 팀은 `pct_change(fill_method='ffill')`이라 결측일 0, 다음날에 2일 누적 수익 인식 (`Returns.py:64`) |
| 배당 | 미반영 | **미반영** (원천 `xfl_target.miadjprice`는 split만 조정, 배당은 별도 `DivData/`) |
| 코퍼레이트 액션 | split 조정됨 (`daily_uss_stock_split.py:69-113`이 이력 재작성) | 동일 |
| 상폐 이후 | close NaN → 스킵 | **0.0 패딩** (T1) → ETL이 NaN 복원 필수 |

→ parity 목적에서는 **`hr` 사용이 필수**. `close` 비율 경로는 fallback으로만 유지.
배당 포함 total return이 필요하면 `DivData/`를 합성하는 별도 작업이 필요함(7절 미결).

### 5.3 (c) `bars_per_year = 252`

실측 근거: Top500 2010-01-04~2026-09-04 구간 T=**4,195** 거래일 / 16.67년 = **251.6/년**.

| 지표 | 365 → 252 변화 |
|---|---|
| `ann_ret = mean·bars` (`summary_metrics.cpp:49`) | **×0.690** (365→252) |
| `sharpe = (mean/std)·√bars` (`:61`) | **×0.831** (= 1/1.2036) |
| `sharpe80/400` (`:199, 217`), `yearly.ann_ret/sharpe` (`yearly_metrics.cpp:74, 91`) | 동일 배율 |

**현행 그대로 두면 Sharpe가 1.204배, 연율수익률이 1.448배 부풀려짐.** 프론트는 `barsPerYear`를 읽지도 표시하지도 않으므로(`types/index.ts:85`, 사용처 0건) **화면상 구분 불가** → 4.4-I/J와 4.5-P2를 함께 처리해야 함.

### 5.4 (d) 유니버스 마스크로 거래가능 판정

| 단계 | 방식 | 부작용 |
|---|---|---|
| 1단계 | ETL이 **유니버스 밖 `close`를 NaN**으로 덮어씀 | `simulator.cpp:62,70,82`가 자동으로 배제. 교차단면 모듈도 NaN 인지(`Rank.h:12-19`, `Zscore.h:10-15`)라 전파됨. **단, `<tsmean days=60>` 등 시계열 모듈의 롤링 창이 유니버스 진입 이후로만 워밍업됨** — 팀 규약(전 이력으로 계산 후 마지막에 `self.valid` 적용)과 **의미가 다름** |
| 2단계 (권장) | `price_col`은 **마스킹 없는 전 이력**, 별도 `valid_col`을 정규화 단계(`simulator.cpp:62,70`)에만 적용 | 팀 규약과 일치. 4.4-E 변경 필요 |

즉 **1단계 산출물로는 팀 parity를 논하지 말 것.** parity는 2단계(`valid_col` 도입) 이후.

일별 유효 종목 수는 `nonzero_count`(`simulator.cpp:106`)와 별개 개념이므로, 진단용으로 `valid` 합계를 timeseries에 추가하는 것을 권장(선택).

### 5.5 (e) 상장폐지 / 마스크 이탈

| 주체 | 처리 | 결과 |
|---|---|---|
| 엔진 현행 | close NaN → 그날 보유수익 **0 처리**(`simulator.cpp:82`), 다음 bar에 `target=0`이 되어 **청산 비용은 정상 부과**(`:92`) | 실제 −100% 상폐 손실 미반영 |
| 팀 sim | `hr`가 상폐 이후 **0.0 패딩**(T1 실측 99.98%), `_check_dims`가 비유한값도 0.0으로(`handlers.py:391`) | **동일하게 미반영** |

→ **두 시스템이 같은 편향을 공유**하므로 parity에는 지장 없음. 다만 연구 편향으로 별도 기록 필요.

권고:
- ETL이 secmaster `securityEndDate`로 상폐 종목·시점을 `symbols.csv`에 표기.
- 옵션 플래그로 "유니버스 이탈 직전 강제 청산"(마지막 유효일에 valid=1 유지 + target 0 유도) 또는 "상폐일 −100% 주입" 모드를 제공할지 결정(7절 미결).
- 상폐 종목 비중이 성과에 미치는 영향을 별도 진단표로 산출.

---

## 6. 단계별 실행 계획

### Phase 0 — 선행 (블로커 해소)

| 항목 | DoD | 검증 |
|---|---|---|
| 프론트 부재 모듈 6개 복구 | `frontend/lib/{themeColors,colorScale,metricColors,uplotUtils,uplotAdapters,snapshotColors}.ts` 존재 | `npm ci && npm run build` 성공 |
| C++ 빌드 환경 확보 (Arrow/Parquet/Drogon/yaml-cpp/jsoncpp/Boost.serialization/GTest) | `cmake --build` 성공 | 기존 테스트 **전부** 통과 (`tests/*.cpp` 17개) |

**Phase 2 이후 전부 이 환경에 막힘.** 로컬 불가면 빌드 서버를 먼저 정할 것.

### Phase 1 — ETL v1 + 무변경 불 켜기

| 항목 | DoD |
|---|---|
| `tools/etl/build_uss_panel.py` 작성 | `--universe Top500 --start 2010-01-01 --layout sparse` 실행 성공 |
| 산출물 | `panel.parquet`, `symbols.csv`, `manifest.json` |
| 자체 검증 | V1~V8 전부 통과 (4.2) |
| 엔진 구동 | `GET /api/config` → `N=1020`, `simStart=2010-01-04`, `simEnd=2026-09-04`, `T=4195` |
| 알파 1개 | `<data expr="close"/><tsrank days="20"/><rank/>` 가 200 응답 |
| 회피 | 요청에서 `collectSignals` 억제 또는 4.4-L 선반영 (미적용 시 signalPlot ~64MB) |

**이 단계 성과 수치는 신뢰하지 말 것** — `bars_per_year=365`, trading leg 부재, 롤링 창 워밍업 차이(5.4).

### Phase 2 — C++ 2단계 변경

| 항목 | DoD |
|---|---|
| 4.4의 A,B,C,E,F,G,H,I,J,K,N 반영 | 크립토 config **무변경 회귀 통과**(기본값이 현행과 동일함을 테스트로 고정) |
| `config.yaml` 확장 | 4.3 스키마 파싱, 미지정 시 현행 동작 |
| 신규 테스트 | `bars_per_year` 주입 테스트, `return_col`/`trading_return_col`/`valid_col` 경로 단위 테스트, `time_col` 비-int64 시 명시적 예외(4.4-C) |

### Phase 3 — parity 시험 (핵심)

**입력 실물** (실측 확인):

| 항목 | 값 |
|---|---|
| 사용 가능한 AlphaPos | `/home/jgo/61/jgo/research/glasso/data/01_raw/{pv_ind_none_check_0, pv_peer_valid_t1000_0, pvp_t500_none_0, pvp_t500_peerpost_corr_0, pvp_t500_peerpost_gl_0, pvp_t500_peervt_corr_0, pvp_t500_peervt_gl_0, pvp_t500_secneut_0, pvp_t500_secneut_post_0}/AlphaPos.parquet` (9건, 각 15~17MB) |
| 권장 기준 | `pvp_t500_none_0` — 4,169행 × 28,181열, 2010-01-04~2026-07-31, float64, index `__index_level_0__` timestamp[ns], 컬럼 int64 tradingItemId |
| 실측 성질 | 일별 `Σ|pos|` = **1.00000000** (std 1.1e-16, 예외 0일), `Σpos` mean\|net\| 0.078, nnz mean 55.4, all-NaN 행 0건 |
| **부재** | `/home/jgo/research/pv_alpha/sim_runs/export/champion_V100_pow15_t10_d5/AlphaPos.parquet` — 해당 경로 체계는 존재한 적 없음(원래 `research/pv_alpha/cache`). 현재 `/home/jgo/61/jgo/research/00_grave/pv/data/01_raw/champion_V100_pow15_t10_d5/`에 **METADATA.yml만 잔존, parquet 삭제됨** |

**시험 설계**

| # | 시험 | 방법 | DoD |
|---|---|---|---|
| T-1 | oracle 생성 | 팀 `StatsHandler`를 **직접 호출**. `AlphaFile`에 pos DataFrame 주입 가능(`typedef.py:160`), `HoldingReturns/TradingReturns/OpenToCloseReturns`는 캐시 경로. `TradingCost`를 0.0 / 1.5e-4 두 케이스 | `oracle_pnl.parquet` (PnL, BookSize, Return, Tvr, PnL2) |
| T-2 | 집합 정합 (**선결**) | AlphaPos non-NaN 셀 집합 vs ETL 패널 `valid` 셀 집합 비교. AlphaPos 생성 시 `univ`는 해당 run의 METADATA.yml로 확인 | 불일치 셀 **0**. 0이 아니면 ETL 마스크 규칙을 AlphaPos 생성 규칙에 맞춰 조정 |
| T-3 | 주입 경로 | `Simulator::run_from_signals`(`simulator.h:38`)는 존재하나 **HTTP 미노출**(`main.cpp:537` combined 내부 전용). 신규 gtest `tests/test_parity_alphapos.cpp` 또는 `POST /api/simulate/positions` 추가 | mode=0으로 AlphaPos 주입 성공 |
| T-4 | 단계적 대조 | 아래 표 | 각 단계 허용오차 통과 |
| T-5 | end-to-end 역방향 | 엔진 네이티브 알파를 mode=1로 실행 → 시그널을 파이썬으로 재현 → oracle과 대조 | 시그널 생성부까지 포함해 일치 |

| 단계 | 엔진 설정 | 대조 대상 | 허용오차 |
|---|---|---|---|
| P-a | mode=0, `return_col=hr`, tr 미사용, fee=0 | oracle `Σ pos[t−1]·hr[t]` | rel 1e-5 |
| P-b | P-a + fee=1.5e-4 | oracle에서 `− Σ|Δpos|·cost` 포함 | rel 1e-5 |
| P-c | P-b + `trading_return_col=tr` | oracle `PnL` 전체 | rel 1e-5 |
| P-d | 누적 | `timeseries.cumPnl` vs oracle `PnL.cumsum()` (엔진은 bar 0 제외, `timeseries_output.cpp:23,36` → 인덱스 정렬 필요) | abs 1e-6 |
| P-e | 요약 | oracle을 `bars_per_year=252`로 계산한 Sharpe/annRet | rel 1e-6 |

**정밀도 상한**: 패널이 float32(`data_panel.h:13`, `parquet_reader.cpp:204`)이고 팀 oracle은 float64. 셀당 상대오차 ~1e-7, 1,000종목 합산 시 **~1e-5 수준** [추정] → 허용오차를 이보다 타이트하게 잡으면 안 됨. 더 엄격한 parity가 필요하면 `DataPanel`을 double로 바꾸는 별도 결정이 필요함(메모리 2배).

**재정규화 함정**: `run_from_signals`가 `target = signal/Σ|signal|`을 **다시 계산**하며, 분모는 `isfinite(close) && isfinite(signal)`인 셀만 합산(`simulator.cpp:155-168`). AlphaPos는 이미 gross=1이지만 **패널 마스크와 AlphaPos non-NaN 집합이 다르면 재정규화로 값이 바뀜** → T-2가 선결 조건인 이유.

**마지막 행 함정**: 팀 `_check_dims`가 `hr/tr/opr`의 **마지막 행을 NaN**으로 만듦(`handlers.py:392,395,398`). oracle 마지막 날 PnL은 NaN이므로 대조에서 제외할 것.

### Phase 4 — 프론트 P0 (4.5)
DoD: Top500 2010~ 패널에서 히트맵 첫 렌더 1초 이내 [목표치], 패널 드래그 시 프레임 드랍 없음, `/api/simulate` 응답 5MB 이하.

### Phase 5 — 성능 (옵션 B로의 이행, 선택)
DoD: 서버 기동 로드 시간 및 피크 RSS를 Phase 1 대비 측정·개선. 계약(4.1)은 유지.

---

## 7. 미결 질문 (사용자 결정 필요)

| # | 질문 | 선택지 / 영향 |
|---|---|---|
| Q1 | **기본 유니버스·기간** | Top500 2010~ (T=4195, N=1020, 패널 8컬럼 137MB, signalPlot ~64MB) vs Top1000 2010~ (T=4196, N=2249, 302MB, ~142MB). 2015~로 줄이면 각각 83MB/182MB |
| Q2 | **심볼 키** | `tradingItemId` 확정(권장, 4.1) 시 프론트에 숫자만 노출됨 → 라벨 API(4.4-N)와 프론트 소비를 이번 스코프에 넣을지 |
| Q3 | **거래비용** | 팀 운용은 `TradingCost: 0.0`(alpha-yml-pipeline.md:174). 엔진 기본을 0으로 맞출지, 1.5e-4 편도를 쓸지. parity 시험은 두 값 모두로 돌릴 예정 |
| Q4 | **실행 모드** | 네이티브 알파는 D1(팀 정보시점과 일치), 주입 parity는 D0. 두 모드를 API로 노출할지 config 고정할지 |
| Q5 | **배당** | `hr`는 배당 미반영 순수 가격수익률(T8). total return이 필요하면 `DivData/` 합성이 **별도 스코프** — 지금 할지 |
| Q6 | **상폐 처리** | 팀 규약(상폐 후 0 수익, 손실 미반영)을 그대로 따를지, 강제 청산 / −100% 주입 옵션을 넣을지 (5.5) |
| Q7 | **TokensToRemove 46종** | JPM·GS·HON·FDX·BKNG·BRK.A·SPGI 등 대형주가 **전 유니버스에서 영구 제외**됨(T4). 이 유니버스를 그대로 쓸지, 자체 유니버스를 만들지 |
| Q8 | **signalPlot 정책** | 심볼 top-K / 시간축 stride / 별도 엔드포인트 / 아예 제거. 현재 상한 없음이라 1단계에서 바로 문제됨 |
| Q9 | **다중 데이터셋** | `datasets[0]`만 읽는 구조(`config_loader.cpp:15`)를 유지하고 config 파일을 갈아끼울지, 다중 데이터셋을 지원할지 |
| Q10 | **빌드 환경** | Arrow/Drogon을 로컬에 세울지 별도 서버에서 빌드할지. **Phase 2 이후 전부 여기에 막힘** |
| Q11 | **패널 정밀도** | float32 유지(parity 허용오차 ~1e-5) vs double 전환(메모리 2배, 엄격 parity) |
| Q12 | **`min_bet` 재정의** | `Σ|w|/N`(`simulator.cpp:108`)이 N=17→1020에서 의미 붕괴. 유효 종목 수로 나눌지, 지표를 바꿀지, 그대로 둘지 |

---

## 부록: 프론트엔드 영향 상세

### A.1 개요 (심볼 17 → 500~2000)

경로는 모두 `/home/jgo/longshort_platform/frontend/` 기준.

| 항목 | 현황 | 근거 |
|---|---|---|
| 심볼 하드코딩 | **없음**. `BTCUSDT` 등은 테스트 픽스처뿐. 숫자 17은 주석 1건 | `components/visualization/SignalHeatmap.tsx:69` |
| `config.symbols` 사용처 | **없음** (타입 정의·store 저장만) | `types/index.ts:81`, `store/alphaStore.ts:213` |
| `config.N` 사용처 | `{config.N} symbols` 텍스트 1곳 | `components/.../SettingsBar.tsx:127` |
| `config.barsPerYear` | 타입에만 존재, **읽는 코드 0건** | `types/index.ts:85` |
| 날짜 기본값 | `simStart 2020-01-01 / isStart 2023-01-01 / osStart 2024-01-01` | `store/alphaStore.ts:69-76` |
| 날짜 자동보정 | config 범위 밖이면 `isStart=minDate`, `osStart=min+0.7×기간`로 재계산 | `store/alphaStore.ts:218-232` |
| 유니버스 선택 UI | **없음** | — |
| 가상화 라이브러리 | **없음** (`next/react/uplot/zustand/monaco`만) | `frontend/package.json:15-23` |

#### 히트맵이 핵심 병목

`SignalHeatmap.tsx`는 단일 `<canvas>`에 **셀당 `fillRect` 1회**. 가상화/샘플링 없음.

| 시나리오 | 셀 수 | `fillRect` 호출 | `getImageData` 백업 |
|---|---|---|---|
| 현재 17×2070 | 3.5만 | 3.5만 | ~1.6MB |
| 500×4000 | 200만 (×57) | 200만 | ~16MB |
| 2000×4000 | 800만 (×227) | 800만 | ~64MB |

- 셀 크기: `cellW=max(1, W/T)`, `cellH=max(2, H/N)`, 컨테이너 높이 상한 `max-h-48`=192px → `SignalHeatmap.tsx:9-10, 135-140, 350`. N=2000이면 **동시에 96행만 보임(5%)**.
- `drawBase`가 `ResizeObserver`마다 재호출 → 패널 드래그 중 64MB 할당+800만 fillRect 반복 (`SignalHeatmap.tsx:211, 236-242`). **가장 위험**.
- `colorPalettes` 캐시가 "그날 유효 종목 수(`sortedLen`)"별로 문자열 배열을 만듦 (`:154-179, 201`). 크립토는 매일 전 종목 존재 → 키 1개. **주식은 상장/폐지로 일별 유효수가 계속 달라져 키가 최대 N개** → 크립토 전용 최적화가 주식에서 역효과.
- `colSorted`가 T개 정렬 배열을 전부 보유 (`:159-180`) → 2000×4000이면 +64MB, 정렬 O(T·N log N).
- 검색 드롭다운이 `useMemo` 없이 매 입력마다 전 심볼 필터 + `slice` 없음 (`:70-72, 361-374`). 주석 `:69`가 "17 symbols max" 전제를 명시.

#### 응답 페이로드

- `signalPlot`은 서버·클라 **양쪽 다 다운샘플링 없음**. `simStart` 이후로 자르는 것이 유일 (`cpp-engine/src/main.cpp:597-621`).
- 프론트는 `response.json()`으로 **전량 버퍼링 파싱**, **타임아웃 없음** (`lib/api.ts:221-241`). 스트리밍은 GA 전용 (`lib/api.ts:271-289`).
- 브라우저가 백엔드로 직접 fetch(Next rewrite 없음) → Next 바디 제한 미적용 (`lib/api.ts:16`, `next.config.mjs`).
- `values`에 `null`이 섞여 V8 packed-double 최적화 불가 (`types/index.ts:123`, `json_serializer.cpp:117-121`). 미국 주식은 미상장 NaN이 대량이라 이 경로를 정통으로 밟음.

| 시나리오 | 값 개수 | JSON 크기 [추정, ~15B/값] |
|---|---|---|
| 17×2070 | 3.5만 | ~0.5MB |
| 500×4000 | 200만 | **~30MB** |
| 2000×4000 | 800만 | **~120MB** |

#### 별도 발견 (연결 작업의 선행조건)

`frontend/lib/` 아래 6개 모듈이 **파일 부재**: `themeColors, colorScale, metricColors, uplotUtils, uplotAdapters, snapshotColors`
(`SignalHeatmap.tsx:6-7`, `SignalChart.tsx:9-19`, `DetailTimeSeries.tsx:8-19`, `AlphaRankingTable.tsx:5` 등에서 import).
`node_modules`, `.next`도 없음 → **현재 작업 트리는 프론트 빌드 불가**. 데이터 연결과 무관하게 선행 복구 필요.
