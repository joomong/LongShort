# C++ Engine Implementation Progress

## Active Remediation Backlog
- [remediation_backlog_2026-03-05.md](/home/jgo/qv-project/LongShort/cpp-engine/docs/remediation_backlog_2026-03-05.md)
- 외부 정적 리뷰 기반 안정성/성능/아키텍처 수정 항목의 단일 소스 문서.
- 기존 Phase 완료 이력과 별도로, 남은 수정 계획과 우선순위를 지속 업데이트한다.

## Phase 0: Data Loader
- [x] Directory structure created
- [x] CMakeLists.txt (FetchContent for pugixml, yaml-cpp, googletest; conda Arrow/Parquet/Boost)
- [x] data_panel.h / data_panel.cpp
- [x] parquet_reader.h / parquet_reader.cpp (Arrow v21 API)
- [x] config_loader.h / config_loader.cpp (yaml-cpp)
- [x] test_data_loader.cpp (2/2 pass)
- [x] Builds and tests pass

## Phase 1: Contract Lock
- [x] module.h (Module base, ModuleInput ring buffer, IntParameter/FloatParameter/BoolParameter)
- [x] signal_vector.h (float* + size wrapper)
- [x] star_vector.h (std::vector alias)
- [x] signal_vector.cpp
- [x] Builds and tests pass (covered by test_modules)

## Phase 2: XML Compiler
- [x] xml_parser.h / xml_parser.cpp (pugixml-based)
- [x] expansion.h / expansion.cpp (pipe, range, choice, choice bind="same", shuffle)
- [x] dag_builder.h / dag_builder.cpp (ModuleRegistry, compile_xml)
- [x] expr_parser.h / expr_parser.cpp (recursive descent, "return" expansion)
- [x] test_xml_compiler.cpp (7/7 pass)
- [x] Builds and tests pass

## Phase 3: Module Runtime
- [x] m_data.h / m_data.cpp (reads from DataPanel)
- [x] m_delay.h / m_delay.cpp (ring buffer delay)
- [x] m_tsmean.h / m_tsmean.cpp (circular buffer rolling mean)
- [x] m_tsrank.h / m_tsrank.cpp (rolling percentile rank)
- [x] m_tszscore.h / m_tszscore.cpp (rolling z-score)
- [x] m_event.h / m_event.cpp (event-driven holding mask)
- [x] m_decay.h / m_decay.cpp (linear decay brute-force)
- [x] m_rank.h / m_rank.cpp (cross-sectional rank, input copy to avoid corruption)
- [x] m_zscore.h / m_zscore.cpp (cross-sectional z-score)
- [x] m_ops.h / m_ops.cpp (MAdd/MSub/MMul/MDiv/MLe/MGe/MConstant)
- [x] m_constant.h / m_constant.cpp
- [x] test_modules.cpp (11/11 pass)
- [x] Builds and tests pass

## Phase 4: Simulator Kernel
- [x] simulator.h / simulator.cpp (D0/D1 modes, normalization, no-lookahead)
- [x] Weight normalization inline in simulator (separate file not needed)
- [x] test_simulator.cpp (2/2 pass)
- [x] Builds and tests pass

## Phase 5: Metrics Engine
- [x] summary_metrics.h / summary_metrics.cpp (pnl, ann_ret, sharpe, mdd, turnover, etc.)
- [x] yearly_metrics.h / yearly_metrics.cpp (calendar-year breakdown)
- [x] timeseries_output.h / timeseries_output.cpp (cum_pnl additive, drawdown, etc.)
- [x] metrics_engine.h / metrics_engine.cpp (IS/OS split)
- [x] 단리 전환 완료: 복리 equity curve → additive cumPnL (STAR 고정 북사이즈 방식)
- [x] cagr → ann_ret = mean(daily_ret) × bars_per_year
- [x] MDD: equity ratio → additive peak-current
- [x] test_summary_metrics.cpp (2/2 pass)
- [x] test_yearly_metrics.cpp (2/2 pass)
- [x] Builds and tests pass

## Phase 6: Checkpoint + SIMD
- [x] checkpoint_manager.h / checkpoint_manager.cpp (binary save/load, ICheckpointable)
- [x] simd_utils.h / simd_utils.cpp (AVX2 detection, aligned allocation)
- [x] m_decay_avx2.h / m_decay_avx2.cpp (AVX2 stub)
- [x] test_checkpoint.cpp (4/4 pass)
- [x] test_simd_parity.cpp (5/5 pass)
- [x] Builds and tests pass

## Phase 7: GA Multithread
- [x] search_space.h / search_space.cpp (gene extraction from XML)
- [x] ga_engine.h / ga_engine.cpp (tournament selection, crossover, mutation, elitism)
- [x] parallel_evaluator.h / parallel_evaluator.cpp (thread pool, hash cache with intra-batch dedup)
- [x] test_ga.cpp (6/6 pass)
- [x] test_determinism.cpp (3/3 pass)
- [x] Builds and tests pass

## Phase 8: C++ REST API (Drogon) — STAR 방식
- [x] Step 1: Drogon 설치 + CMake 연동 + 빈 서버 기동
- [x] Step 2: CORS + GET /api/config (config.yaml → DataPanel 로드)
- [x] Step 3: Simulator 시그널 수집 (단일 알파용 T×N signal matrix, collect_signals 옵션)
- [x] Step 4: JSON Serializer (STAR 필드명: return, sharpe, turnover 등)
- [x] Step 5: POST /api/simulate — 단일 알파 (signalPlot + distribution 포함)
- [x] Step 6: POST /api/simulate — 다중 알파 batch (signalPlot 없음)
- [x] Step 7: POST /api/ga/stream (NDJSON 스트리밍)
- [x] Step 8: Alpha CRUD (파일시스템)
- [x] Step 9: 프론트엔드 STAR 방식 재설계 연결

### STAR vs Python 레거시 핵심 차이 (분석 완료)
| 항목 | STAR (목표) | Python 레거시 |
|------|-------------|--------------|
| 시뮬레이션 | simStart(웜업) + isStart + osStart | isStart/isEnd/osStart/osEnd |
| Yearly | 전체 기간 단일 테이블 | IS/OS 분리 |
| PnL 차트 | 전체 기간 단일 + 경계선 | IS/OS 별도 시계열 |
| Summary | 한 행에 IS·OS 나란히 | 별도 객체 |
| PnL 방식 | 단리 (고정 북사이즈) | ~~복리~~ → 단리 전환 완료 |
| 메트릭 | return, sharpe, sharpe80, quality, minsoc | pnl, cagr, sharpe, mdd, min_bet |

## Summary
- **Total source files**: 50+ (.h/.cpp)
- **Total tests**: 10 test executables, 44+ test cases
- **All 10/10 test executables pass (100%)**
- **Build system**: CMake 3.20+, C++17, FetchContent for pugixml/yaml-cpp/gtest/drogon
- **Dependencies**: Arrow 21, Parquet 21, Boost 1.84, jsoncpp 1.9.6 (conda), pugixml 1.14, yaml-cpp 0.8, gtest 1.14, Drogon 1.9.8
- **API endpoints**: GET /api/health, GET /api/config, POST /api/simulate (single+multi), POST /api/ga/stream (NDJSON), GET/POST/DELETE /api/alphas
- **Frontend**: Next.js 14, TypeScript 0 errors, 31/31 tests pass, Playwright E2E 6/6 scenarios pass
- **Phase 8 COMPLETE**: All 9 steps done — C++ REST API + Frontend STAR integration
