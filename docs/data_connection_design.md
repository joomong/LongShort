# 우리 데이터(cache_v1_cash_1d) → LongShort 엔진 연결 설계

확인 기준일: 2026-09-08 (main@6a70f37). 저장소·캐시·팀 sim 소스(/home/base/git/sim)를 직접 대조한 정적 분석. 수치는 실측, 추정은 [추정] 표기.
근거 상세(파일:줄 전수)는 [review_2026-09-08/design_agent.md](review_2026-09-08/design_agent.md), 손익 산식 리뷰는 [review_2026-09-08.md](review_2026-09-08.md) 참조.

## 1. 결정 요약

| 항목 | 채택 | 근거 |
|---|---|---|
| 연결 방식 | **A. Python ETL → long parquet** (엔진 입력 계약 유지), 2단계 진행 | 1단계는 C++ 변경 0으로 구동. 로컬에 Arrow/Drogon 빌드 환경이 없어 검증 불가한 C++ 변경을 선행시키지 않음. parity 검증이 파이썬(팀 sim과 같은 언어)에 남음 |
| 심볼 키 | **`tradingItemId` 문자열** | 캐시 컬럼 키 그대로, secmaster 조인 28,207/28,207 완전 일치, 시간 불변. ticker는 secmaster `symbol` null 19.8%·변경 이력 있어 기각 → 표시 라벨(`symbols.csv`)로만 |
| 시간축 | UTC 자정 epoch_ms int64 | 로더가 `Int64Array`로 무검사 캐스트(`parquet_reader.cpp:95,161`), `/api/config`가 `gmtime_r` 사용 |
| 기본 유니버스·기간 | Top500 2010~ 제안 (T=4,195 × N_union=1,020, 일평균 457종목) | 8컬럼 float32 패널 137MB. Top1000은 302MB. **미결 Q1** |
| 수익률 | holding = `Returns/HoldingReturn_Usd`, trading = `Returns/TradingReturn_Usd`, 거래가능 = `TopUniverseV1/Top{N}` ∧ close 유한 | 팀 StatsHandler 규약 그대로. hr은 close 비율과 정확히 동일(배당 미반영) |
| 상수 | `bars_per_year` 252, 비용 1.5bp 편도(config) | 현행 365/5bp면 Sharpe 1.20배 과대 + 고회전 알파 부호 반전 |
| 실행 모드 | 네이티브 XML 알파 = mode 1(D1) + `<data delay="0">`; AlphaPos 주입 parity = mode 0(D0) | 팀 `pos[t]`는 t−1 정보 → 엔진 `exec_w[t]=target[t−1]`과 일치. 이미 shift된 AlphaPos는 D0 |
| 검증 | 팀 `StatsHandler`를 oracle로 한 parity 시험(§7) | 61 마운트에 gross=1인 AlphaPos 9건 실재 |

## 2. 현재 엔진의 데이터 계약

| 항목 | 값 | 근거 |
|---|---|---|
| 입력 | long parquet **1개**(`datasets[0]`만 읽음), 1행=(시각, 심볼) | `config_loader.cpp:15`, `parquet_reader.cpp:88-108` |
| 필수 컬럼 | `open,high,low,close,volume` 하드코딩 검증 | `parquet_reader.cpp:66-73` |
| dtype | 시간 int64, 심볼 STRING/LARGE_STRING, 값 INT64/DOUBLE/FLOAT → **float32** 패널 | `parquet_reader.cpp:21-30,149,197-211` |
| 정렬·결측 | timestamp 오름차순, 심볼 **사전순**, 미등장 셀 NaN | `parquet_reader.cpp:114,118,133-136` |
| `start_date/end_date`, `universe` | 로더가 **무시** | `config_loader.cpp:29-42` |
| DSL 접근 컬럼 | `open,high,low,close,volume` + 의사 식별자 `returns`(=close/close[−1]−1) 하드코딩 | `expr_parser.cpp:140-175` |
| 시뮬레이터 소비 | **`"close"` 하나**, 문자열 하드코딩. 거래가능 = `isfinite(close)` | `simulator.cpp:58,62,70,80` |
| 유니버스/마스크 개념 | 없음 | — |
| 메트릭 상수 | `bars_per_year=365` 10곳 리터럴 + 헤더 상수 | `main.cpp:203,294,…,803`, `summary_metrics.h:5` |
| 응답 | 단일 알파에 `signalPlot`(T×N 전체 시그널 행렬 JSON) 상한 없음 | `json_serializer.cpp:97-129`, `main.cpp:597-638` |

## 3. 우리 캐시 실체 (`/home/base/cache/cache_v1_cash_1d`)

### 3.1 레이아웃·스키마

wide parquet, 필드 1개 = 파일 1개. 행=날짜(`__index_level_0__` timestamp[ns], 1980-01-02~2026-09-08, 11,811행), 열=`tradingItemId` 문자열 28,208개, 값 double. 컬럼 선택 읽기가 저렴함(1,020컬럼 0.3초 실측).

| 디렉터리 | 내용 | 비고 |
|---|---|---|
| `BaseData/` | open/high/low/close/volume/VWAP/bid/mid/ask/**midPrice**/marketCap_USD/currentPrimary 등 | close 204MB/파일 |
| `Returns/` | HoldingReturn_Usd, TradingReturn_Usd, OpenToCloseReturn_Usd, VWAPToCloseReturn_Usd | 각 ~400MB |
| `TopUniverseV1/` | ALL, Top10/100/200/300/500/700/1000 (bool 일별 PIT 마스크) | 각 18MB |
| `sector/` | sector(double 코드), sectorIx | 코드 순서 비표준(§3.3) |
| `Top500/`, `Top1000/` | 위 4종의 축소 사본(2000년~, 컬럼 1,461/3,188) | ETL 입력으로 쓰면 읽기량 1/7. `Top3000/`은 stale(2025-11) |
| secmaster | `/home/base/vendor_data/uss/cash/master/uss_cash_master_acc.parquet` (28,207행, index tradingItemId, symbol/companyName/sector/securityEndDate…) | 로컬 존재 |

### 3.2 정의 (팀 sim 소스 확정)

| 필드 | 정의 | 근거 |
|---|---|---|
| `HoldingReturn_Usd` | `close.pct_change(1, fill_method='ffill')` — 배당 미반영, split 조정가 기준 | `modules/Returns.py:64` |
| `TradingReturn_Usd` | `close[t]/midPrice[t] − 1`, `.fillna(0)` | `Returns.py:66` |
| `midPrice` | **`(open+high+low+close)/4`** — 호가 mid 아님 | `modules/BaseData_uss_v1.py:100` |
| `TopUniverseV1/Top{N}` | 100일 평균 달러거래대금 >3e6 ∧ 시총 >1e7 ∧ currentPrimary, 21일 랭크, sticky(진입 60/이탈 10일), TokensToRemove 46종 제외 | `modules/TopUniverse_uss_v1.py:69-110`, METADATA.yml |
| StatsHandler P&L | `pnl[t] = Σ pos[t−1]·hr[t] + Δpos[t]·tr[t] − |Δpos[t]|·cost`, 첫날 `Δpos=pos`, `ret = pnl/Σ|pos|` | `utils/sim_util/handlers.py:416-430` |

### 3.3 함정 (실측)

| # | 함정 | 실측 | ETL 대응 |
|---|---|---|---|
| T1 | Returns 0.0-fill은 **상폐 이후** 구간(99.98%가 0.0); 상장 전은 NaN | 유효 셀 내부 0.0은 0.75%(진짜 보합) | 유효 셀 밖 hr/tr을 NaN으로 되돌림 |
| T2 | close 유효 구간에서 hr NaN 0.0000% | — | `close.notna()` ∧ 마스크로 충분 |
| T3 | **TokensToRemove 46종이 ALL 포함 전 유니버스에서 영구 제외** — JPM(2622876)·GS(2615898)·HON(2618950)·BKNG·SPGI 등 | 일평균 457 ≈ 500−46, 926 ≈ 1000−46 | 그대로 쓸지 **미결 Q7** |
| T4 | Top1000.parquet 11,812행 vs Top500 11,811행(2026-07-03 1일 차이) | — | 날짜축은 마스크 ∩ close 인덱스 |
| T5 | 마지막 행(당일) 전 컬럼 False | 실효 마지막일 2026-09-04 | 마지막 행 제외 |
| T6 | sector 코드 비표준 | 1 Energy, 2 IT, 3 Industrials, 4 Materials, 5 Financials, 6 ConsDisc, 7 ConsStaples, 8 RealEstate, 9 CommSvc, 10 HealthCare, 11 Utilities | `sectorIx` 조인 |
| T7 | secmaster `symbol` null 19.8% | — | 키로 쓰지 않음 |

### 3.4 크기

| 유니버스 | 구간 | T | N_union | 일평균 | 1필드 f32 | 8컬럼 패널 | signalPlot JSON [추정] |
|---|---|---:|---:|---:|---:|---:|---:|
| Top500 | 2010~ | 4,195 | 1,020 | 457 | 17MB | 137MB | ~64MB |
| Top500 | 2015~ | 2,937 | 887 | 455 | 10MB | 83MB | ~39MB |
| Top1000 | 2010~ | 4,196 | 2,249 | 926 | 38MB | 302MB | ~142MB |

로더가 Arrow 테이블 전체를 올린 뒤 패널을 추가 할당하므로 피크는 약 2배 [추정] (Top1000 8컬럼 ≈1.1GB).

## 4. 연결 옵션 비교

| | **A. Python ETL → long parquet** | B. C++ 로더가 wide 다중 파일 직접 읽기 | C1. 축소사본(`Top500/`)만 사용 | C2. Python 사이드카 |
|---|---|---|---|---|
| C++ 변경 | **0**(1단계) / 6파일(2단계) | `parquet_reader` 전면 + config + panel | A와 동일 | IPC 계층 신규 |
| 공수 [추정] | 0.5~1일 + 2~3일 | 4~7일 | A−0.2일 | 5~8일 |
| 로컬 검증 | **빌드 불필요**(1단계) | 빌드 환경 없어 **불가** | 동일 | 불가 |
| 로드 성능 | long 1.9M~9.4M행 파싱, 수~수십 초 [추정] | 0.3초/필드(실측) | 1/3 | IPC 오버헤드 |
| parity 용이성 | **최상**(고정 아티팩트, pandas로 대조) | 중(C++ 내부 마스킹을 규약과 맞춰야) | 동일 | 중 |
| 위험 | 로드 시간·피크 메모리 | Arrow C++ 대량 컬럼 API, timestamp[ns] 인덱스 처리 | 2000년~·고정 Top-N | 프로세스 2개 |

**추천 A.** B의 성능 이점은 서버 기동 1회 로드에만 해당해 ROI가 낮음 — 계약 확정 후 성능 개선으로 뒤에 붙임(Phase 5). C1은 A의 입력 최적화로만 채택.

## 5. 추천안 스펙

### 5.1 panel 계약 (ETL 산출물)

| 컬럼 | dtype | 정의 |
|---|---|---|
| `open_time` | int64 | UTC 자정 epoch_ms |
| `symbol` | string | tradingItemId |
| `open, high, low, volume` | double | `BaseData/*`, 유효 마스크 적용 |
| `close` | double | `BaseData/close`. **1단계에서는 유효 밖을 NaN으로 덮어씀**(마스크 주입 수단) |
| `hr` | double | `Returns/HoldingReturn_Usd`, 유효 밖 NaN |
| `tr` | double | `Returns/TradingReturn_Usd`, 유효 밖 NaN |
| `valid` | double | 유효 1.0 / NaN |

부속: `symbols.csv`(tradingItemId, ticker, companyName, sector, sectorIx, securityEndDate), `manifest.json`(입력 mtime·인자·T·N·체크섬).

### 5.2 ETL 도구 (`tools/etl/build_uss_panel.py`, venv-3.11)

| 인자 | 기본 |
|---|---|
| `--universe` | Top500 |
| `--start` / `--end` | 2010-01-01 / 실효 마지막일 |
| `--columns` | open,high,low,close,volume,hr,tr,valid |
| `--layout` | sparse(유니버스 내부 행만, Top500 2010~ 1.92M행) / dense(T×N_union 4.28M행) |

마스크 규칙(순서 고정): 날짜축 = Top{N} ∩ close 인덱스, 기간 절단, 마지막 행 제외 → 종목축 = 기간 내 한 번이라도 True → 일별 유효 = True ∧ close 유한 → valid/close/hr/tr에 마스크 적용.

자체 검증: 시간 유일·오름차순, 심볼 secmaster 전수 존재, 일별 유효수 분포(Top500 2010~: 평균 457, p05 446, p95 468), valid↔close 유한 일치, hr 재계산 대조, 재실행 바이트 동일.

### 5.3 `config.yaml` 확장 (전부 optional, 기본값 = 현행 → 크립토 config 무변경)

```yaml
datasets:
  - name: uss_top500
    path: Data/uss/top500_2010.parquet
    time_col: open_time
    symbol_col: symbol
    use_col: [open, high, low, close, volume, hr, tr, valid]
backtest:
  transaction_fee: 1.5        # bps 편도 (팀 규약; 운용 yml은 0.0 → 미결 Q3)
  bars_per_year: 252
  price_col: close            # 정규화·fallback 수익률
  return_col: hr              # holding leg (미지정 시 close 비율)
  trading_return_col: tr      # trading leg (미지정 시 미사용)
  valid_col: valid            # 거래가능 (미지정 시 price_col 유한성)
  mode: 1                     # 네이티브 알파 기본. AlphaPos 주입 parity는 0
```

### 5.4 C++ 변경 목록 (코드 미작성)

| # | 파일:줄 | 변경 | 단계 |
|---|---|---|---|
| A | `config_loader.{h,cpp}` | 신규 optional 필드 파싱 | 2 |
| B | `parquet_reader.cpp:66-73` | OHLCV 필수 검증 → config 지정 컬럼만 필수 | 2 |
| C | `parquet_reader.cpp:95,161` | time_col 타입 검사(현재 무검사 static_cast → 비int64면 UB) | 2 |
| E | `simulator.cpp:58,80,153,175` | `"close"` 리터럴 → `price_col`; 정규화 마스크에 `valid_col` 우선 | 2 |
| F | `simulator.cpp:79-87` | `return_col` 있으면 `prev_w·hr` | 2 |
| G | `simulator.cpp:89-94` | `trading_return_col` 있으면 `+Σ(exec_w−prev_w)·tr` | 2 |
| H | `simulator.cpp:76,115-120`, `main.cpp:293` | mode를 config/요청에서 | 2 |
| I·J | `main.cpp` 365 리터럴 10곳, `summary_metrics.h:5` | `bars_per_year` config 경유 | 2 |
| K | `expr_parser.cpp:167-175` | 허용 식별자 → 패널 컬럼 집합 | 2 |
| L | `json_serializer.cpp:97-129`, `main.cpp:597-638` | **signalPlot 상한**(심볼 top-K / 시간 stride) | **1 필수** |
| N | `main.cpp:168-215` | `/api/config`에 symbolLabels | 2 |
| — | `simulator.cpp:126-219` | `run_from_signals` 복사본을 `run`과 공통화(F·G를 두 벌 고치지 않도록) | 2 |

F·G·E는 [review_2026-09-08.md](review_2026-09-08.md) §2.1 대응관계대로 인덱스 변경 없이 붙음.

### 5.5 프론트 변경

| 우선 | 항목 | 위치 |
|---|---|---|
| 선행 | 부재 모듈 6개 복구(`lib/{uplotUtils,uplotAdapters,themeColors,metricColors,snapshotColors,colorScale}.ts`) — `.gitignore:17 lib/` 예외 필요 | `frontend/lib/` |
| P0 | signalPlot 다운샘플 수신·값 개수 가드, 히트맵 행 가상화·`getImageData` 전면 백업 제거, 팔레트 캐시 고정 256단계 | `lib/api.ts:194,221-241`, `SignalHeatmap.tsx:113-215` |
| P1 | 히트맵 높이·심볼 라벨(`symbols.csv`)·검색 `useMemo`, 날짜 기본값 config 일원화 | `SignalHeatmap.tsx:350,69-72`, `store/alphaStore.ts:69-76,218-232` |
| P2 | `barsPerYear` 표시(현재 읽는 코드 0건), simulate 타임아웃, `SignalChart` 365→barsPerYear | `SettingsBar.tsx`, `SignalChart.tsx:265` |

## 6. P&L 모델 확장

### 6.1 대응관계 (확정)

| | `exec_w[t]` | `prev_weights[t]` |
|---|---|---|
| mode 0 (D0) | `target[t]` | `target[t−1]` |
| mode 1 (D1) | `target[t−1]` | `target[t−2]` |

`exec_w[t] ≡ 팀 pos[t]`, `prev_weights[t] ≡ pos[t−1]`, 첫 bar·booksize(Σ|target|=1)도 일치. 차이는 **(i) `Σ Δpos·tr` 부재, (ii) close 비율 vs hr 컬럼** 두 가지뿐. 네이티브 알파는 내부 shift가 없으므로 D1이 팀 정보시점(t−1)과 일치하고, `<data delay="1">`을 더하면 1바 초과 지연이 됨(프론트 기본 XML이 그 상태).

### 6.2 항목별

| 항목 | 방안 | 실측 근거 |
|---|---|---|
| (a) trading leg | `trading_return_col` 컬럼 기반으로 turnover 루프에 `+(exec_w−prev_w)·tr` 누적. mode 확장은 기각(축 혼합) | Top500 2015~: 회전 1단위당 +0.2~0.9bp, 일평균 turnover 0.56 알파 Sharpe +63%(0.29→0.48), 0.27 +26%, 0.08 +2%. 부호는 알파가 맞으면 (+). 체결가 가정(OHLC4/VWAP/open)에 크기 0~2.6배 민감 |
| (b) hr 컬럼 | `return_col=hr`, close 비율은 fallback | hr ≡ close 비율(max diff 0). 차이는 close 1일 결측 시 팀은 ffill로 2일 누적, 엔진은 2일 소실(11.7년 12셀) |
| (c) 252 | I·J | Top500 2010-01-04~2026-09-04 T=4,195 = 251.6/년. 365 유지 시 Sharpe ×1.204, ann_ret ×1.448 |
| (d) 거래가능 | 1단계: close NaN 덮어쓰기. 2단계: `valid_col`을 정규화에만 적용, price는 전 이력 유지 | 1단계는 `<tsmean>` 등 롤링 창이 유니버스 진입 후에만 워밍업 → 팀 규약(전 이력 계산 후 valid 적용)과 다름. **parity는 2단계 이후** |
| (e) 상폐·이탈 | 엔진: close NaN 구간 보유손익 0 처리 + 청산비용. 팀: hr 0.0 패딩. **같은 편향 공유** → parity 무관, 연구 편향으로 기록. 강제청산/−100% 주입 옵션은 미결 Q6 | — |
| (f) 비용 | config 1.5bp | 5bp 유지 시 rev5 Sharpe 0.48→−0.19 |

## 7. 단계별 계획

| Phase | 내용 | DoD |
|---|---|---|
| 0 선행 | 프론트 부재 모듈 복구, **C++ 빌드 환경**(conda Arrow/Parquet/Boost/jsoncpp + Drogon FetchContent) | `npm run build`, `cmake --build` + 기존 테스트 17개 통과. Phase 2 이후 전부 여기 막힘 |
| 1 ETL v1 | `build_uss_panel.py` + config 교체, C++ 무변경 | `/api/config` N=1,020 T=4,195; `<data expr="close"/><tsrank days="20"/><rank/>` 200 응답(`collectSignals` 억제 또는 L 선반영). **이 단계 수치는 신뢰하지 말 것**(365·5bp·leg 부재·워밍업 차이) |
| 2 C++ 확장 | §5.4 A~N | 크립토 config 무변경 회귀 통과, 신규 경로 단위 테스트 |
| 3 parity | 아래 | 각 단계 허용오차 통과 |
| 4 프론트 P0 | §5.5 | Top500 히트맵 첫 렌더 1초 이내 [목표], 응답 5MB 이하 |
| 5 성능(선택) | 옵션 B로 이행 | 로드 시간·피크 RSS 개선, 계약 유지 |

### parity 시험 (Phase 3)

입력: `/home/jgo/61/jgo/research/glasso/data/01_raw/pvp_t500_none_0/AlphaPos.parquet` (4,169행×28,181열, 2010-01-04~2026-07-31, 일별 Σ|pos|=1.0 정확, 같은 디렉터리에 9건). pv_alpha champion AlphaPos는 아카이브에서 parquet 삭제되어 부재.

| # | 시험 | 방법 | DoD |
|---|---|---|---|
| T-1 | oracle | 팀 `StatsHandler` 직접 호출(`AlphaFile`로 pos 주입, `typedef.py:160`), cost 0 / 1.5e-4 두 케이스 | `oracle_pnl.parquet` |
| T-2 | 집합 정합(선결) | AlphaPos non-NaN 셀 = ETL `valid` 셀 | 불일치 0 (아니면 `run_from_signals` 재정규화로 값이 바뀜) |
| T-3 | 주입 경로 | `Simulator::run_from_signals`는 HTTP 미노출 → gtest `test_parity_alphapos.cpp` 또는 `POST /api/simulate/positions` | mode 0 주입 성공 |
| T-4 | 단계 대조 | P-a hold만(fee 0) → P-b +비용 → P-c +tr → P-d cumPnl(bar 0 제외 정렬) → P-e 252 기준 summary | rel 1e-5 (float32 패널 한계 [추정]), cum abs 1e-6 |
| T-5 | 역방향 | 네이티브 알파 mode 1 → 시그널 파이썬 재현 → oracle 대조 | 일치 |

주의: 팀 `_check_dims`가 hr/tr 마지막 행을 NaN으로 만들어 oracle 마지막 날은 대조 제외. 파이썬 커널 재현본(`review_2026-09-08/scripts/quant/kernel.py`)이 T-4·T-5의 출발점.

## 8. 미결 질문 (사용자 결정)

| # | 질문 | 선택지 / 영향 |
|---|---|---|
| Q1 | 기본 유니버스·기간 | Top500 2010~(137MB) / Top1000 2010~(302MB) / 2015~로 축소 |
| Q2 | 심볼 라벨 API를 이번 스코프에 넣을지 | 없으면 화면에 tradingItemId 숫자만 |
| Q3 | 거래비용 기본값 | 팀 운용 yml은 `TradingCost: 0.0`, 리서치 규약 1.5bp. parity는 둘 다 |
| Q4 | mode를 API로 노출할지 config 고정할지 | 네이티브 D1 / 주입 D0 |
| Q5 | 배당 | hr은 배당 미반영. total return은 `DivData/` 합성 별도 스코프 |
| Q6 | 상폐 처리 | 팀 규약 그대로(0 수익) / 강제청산 / −100% 주입 |
| Q7 | TokensToRemove 46종(JPM·GS·HON…) 제외 유니버스를 그대로 쓸지 | 자체 유니버스면 팀 parity 대상이 달라짐 |
| Q8 | signalPlot 정책 | top-K / stride / 별도 엔드포인트 / 제거 |
| Q9 | `datasets[0]` 단일 구조 유지 vs 다중 데이터셋 | config 교체 운용 vs 로더 확장 |
| Q10 | **빌드 환경** | 로컬 conda 구성 vs 별도 빌드 서버 — Phase 2 이후 전부 의존 |
| Q11 | 패널 정밀도 | float32 유지(parity 1e-5) vs double(메모리 2배) |
| Q12 | `min_bet` 시리즈(`Σ|w|/N`, 현재 미사용) 정리 | 유효 종목 수 기준 재정의 or 삭제 |
