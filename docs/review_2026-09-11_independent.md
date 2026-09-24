# 2026-09-11 커밋(a63f45f · 2cd5b67 · dcc902f · 3cb53da) 독립 리뷰

확인 기준일: 2026-09-11, HEAD 3cb53da. 빌드 `/tmp/longshort_module_server/build`(Release, PyArrow 18.1 Arrow, tests ON). 테스트·서버 실행은 이 리뷰에서 직접 재현했고, 실데이터 실행은 별도 포트(8123)에서 기동 후 종료함.

## 1. 직접 실행 결과

| 항목 | 결과 |
|---|---|
| CTest | **25/25 통과**, 3.6초 (`LONGSHORT_TEST_SERVER` 미지정이라 HTTP 계약 테스트는 skip 경로 — 그 부분은 미재현) |
| Vitest | 15 파일 **65/65 통과** |
| 엔진 기동 (`examples/datasets/equities_monthly_afl.yaml`) | 4초. T=4,447(2009-01~2026-09-08, warmup 252 포함) × N=1,055, eager 5필드 94MB, RSS 285MB, 컬럼 1,197개 등록 |
| 실데이터 `/api/simulate` 3건 (`tsrank→rank` 2012~2020 / 2010~2011, `tsmean→zscore` 2015~2024) | **3/3 실패**: `Cannot value or execute position: invalid close at bar 252, asset 131` |

## 2. 블로커 — 결측 종가 fail-fast × 실제 유니버스

| 사실 | 값 |
|---|---|
| bar 252 | 거래 시작일 2010-01-04 (warmup 직후 첫 bar) |
| asset 131 | tradingItemId 23937615 — 2009-12-31 close 54.08, 2010-01-04부터 NaN(상폐). 마스크는 전날까지 True |
| 메커니즘 | D1: 신호일(t−1) 마스크 True → 목표 ≠ 0 → t 종가 체결 필요 → close NaN → `simulator.cpp:22-26` 예외 |
| 같은 규칙으로 막히는 날짜 | 2010~2026 **144일**(연 2~19회). 원인 = 유니버스 안 상폐 132종목 + 일시 결측 18셀 + 캐시 마지막 행(2026-09-08, close 미채움)의 전 종목 |
| 영향 | 전 종목을 보유하는 알파(rank/zscore 계열 전부)는 실데이터에서 **첫 상폐일을 넘지 못함**. 예제 config는 첫 거래일에 바로 실패 |
| 검증 범위 | wide 데이터 parity는 6종목×65bar 부분집합, StatsHandler parity는 합성 5bar — **전기간 Top500 실행은 한 번도 수행되지 않음** |

팀 sim은 같은 상황을 `hr` 0-fill(`handlers.py:391`)로 "무손실 청산" 처리하고, C++은 이를 의도적으로 복사하지 않았음(logic_completion §유니버스 이탈). 즉 정책 부재가 아니라 **정책이 "오류"로 고정**된 상태이며, 이 상태로는 주식 데이터 연결이 실사용 불가.

제안(둘 중 하나, 또는 둘 다):
1. 엔진 config `missing_close_policy: error | liquidate_last_close`. `liquidate_last_close` = 마지막 유효 종가로 청산(그날 수익 0, 회전·비용 부과) → 팀 StatsHandler와 같은 결과. 기본값은 error로 두되 주식 예제는 liquidate.
2. 유니버스 마스크를 "다음 bar 종가 존재" 조건으로 좁히는 전처리(마스크에만 1일 lookahead, 가격은 무관). 상폐 종목이 마지막 거래일에 목표 0이 되어 D1에서 마지막 유효 종가에 청산됨.
부수: 예제 `end_date`를 캐시 실효 마지막일(마스크 True가 있는 마지막 날)로 — 마지막 행 미채움 때문에 마지막 bar에서 전 종목 예외.

## 3. 보고 내용 vs 실제

| 보고 | 판정 |
|---|---|
| C++ 184·Python 36·프론트 65·Chrome 5 통과 | CTest/Vitest 재현 확인. Chrome·HTTP env 케이스는 미재현 |
| "팀 손익 대조 6사례 통과" | 사실이나 **tr=0·합성 5bar** 조건(`check_team_sim_parity.py:6,115-120`). 보유손익·비용 회계의 동치만 증명. trading leg(Δpos·tr)는 미구현이라 고회전 알파 Sharpe 20~40% 과소(review_2026-09-08 §2.3)는 그대로 |
| wide Parquet 연결 | 로더·좌표 정렬·lazy AFL·마스크 로딩은 동작 확인(기동 4초, 285MB). 시뮬레이션은 §2로 실패 |
| 기본 D1·5bp 유지 | 주식 예제도 5bp — 팀 1.5bp 대비 고회전 알파 부호 반전 위험 그대로. 설계 결정으로 문서화됨 |
| E1 재실행 상태 | 처리 확인: top-K 신호는 저장 XML로 재컴파일(`main.cpp:480`), 모듈 init은 `assign`/`input.init` |
| E2 기본 delay | 새 편집기 기본 `delay="0"`, 저장 XML의 delay=1은 유지 → 한 화면에 두 지연 규약 공존(문서화 필요) |
| E4/E5 combined | `OrderedSignalAccumulator`로 알파 인덱스 순 누적 + normalized 기본. 확인 |
| E6 결측 종가 | 예외 처리로 확정. §2 |
| F1/F2 프론트 기반 | `frontend/lib` 6개 모듈 추적·`.gitignore` 예외, C++ 캡처 fixture. clean clone 빌드 기록 확인 |
| F3~F6 | additive drawdown, batch 내 signalPlot + `collectSignals:true, topK 5`, simStart→`sim_start_index` 워밍업 규약, 부호 보존 색상. 확인 |
| bars_per_year | config 일원화(`config_loader.cpp:161`), 예제 252. `/api/config`도 config 값 반환 |

## 4. 코드 리뷰 노트 (P2)

| # | 위치 | 내용 |
|---|---|---|
| R1 | `src/api/combined_signals.h:26-51` | 행 단위 condition_variable 핸드셰이크 — 정확성은 맞으나 wakeup이 알파 수×T에 비례(1,000알파×4,447bar). 알파별 임시 버퍼 후 인덱스 순 합산이 단순하고 빠름 [추정] |
| R2 | `src/data/data_panel.h:14-24`, `parquet_reader.cpp:220-224` | lazy 필드 최초 로드가 Drogon 핸들러 스레드에서 블로킹(cold 0.5s+)이고 전역 mutex로 직렬화 → 동시 사용자 첫 요청 지연. 설계상 수용 가능하나 문서에 명시 |
| R3 | `src/engine/simulator.cpp:112-129` | 신호일 close 무효(일시 halt)는 조용히 목표 제외, 체결일 무효는 예외 — 두 규칙의 비대칭을 계약 문서에 적을 것 |
| R4 | `src/api/json_serializer.cpp:104-167` | signalPlot 250k셀 stride 샘플링. `rowStride/symbolStride/sampled` 메타는 있으나 프론트 툴팁이 "표본"임을 표시하는지 미확인 |
| R5 | `examples/datasets/equities_monthly_afl.yaml:31` | 주식 예제가 5bp — 팀 규약 1.5bp와 다름을 주석으로만 남김. 예제 사용자가 그대로 쓸 가능성 큼 |

## 5. 재현

```bash
# 테스트
ctest --test-dir /tmp/longshort_module_server/build --output-on-failure -j4
cd frontend && npx vitest run
# 실데이터 실패 재현 (별도 포트)
LONGSHORT_PORT=8123 /tmp/longshort_module_server/build/longshort-server \
  cpp-engine/examples/datasets/equities_monthly_afl.yaml
curl -s -X POST http://127.0.0.1:8123/api/simulate -H 'Content-Type: application/json' \
  -d '{"xml":"<data expr=\"close\" delay=\"0\"/><tsrank days=\"20\"/><rank/>","isStart":"2012-01-01","osStart":"2020-01-01"}'
# → {"error":"Cannot value or execute position: invalid close at bar 252, asset 131"}
```

막히는 날짜 144일 집계 스크립트: 리뷰 세션 scratchpad(`delist/`)에서 Top500 마스크 ∧ close로 계산(D1: `mask[t−1] ∧ valid[t−1] ∧ ¬valid[t]`).
