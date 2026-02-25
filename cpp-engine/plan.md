## LongShort Pure C++ Migration Plan (Drogon + SSE + React Frontend)

### Summary
- 목표: FastAPI 제거, API/연산 모두 C++로 전환
- 프론트: React/Next.js 그대로 사용
- 스트리밍: NDJSON 대신 `SSE`로 재설계
- 범위: 프론트 변경은 필요한 만큼 허용

### Public API / Interface Changes
- 유지:
  - `GET /api/symbols`
  - `GET /api/data-range`
  - `POST /api/backtest/run`
  - `POST /api/ga/run`
  - `POST /api/alphas`
  - `GET /api/alphas`
  - `GET /api/alphas/{name}`
- 변경:
  - 기존 `POST /api/ga/run-stream` (NDJSON) 제거
  - 신규 `GET /api/ga/stream/{jobId}` (SSE) 추가
  - 신규 `POST /api/ga/jobs` 추가: GA 작업 생성 후 `jobId` 반환
  - 신규 `POST /api/ga/jobs/{jobId}/cancel` 추가: 작업 취소
- 응답 에러 포맷 기본: `{"detail":"..."}` 유지

### Implementation Plan
1. C++ 서버 베이스 구축
- Drogon 프로젝트 초기화, 라우팅/JSON/CORS/전역 에러핸들러 구성
- 빌드 체인(CMake, CI target) 확정

2. API 계약 재정의
- 기존 FastAPI 계약 중 유지 대상과 변경 대상 분리
- SSE 이벤트 스키마 명세:
  - `event: progress`
  - `event: generation_end`
  - `event: complete`
  - `event: error`
- 각 이벤트 `data` JSON 구조 고정

3. GA 비동기 잡 시스템 구현
- `jobId` 기준 잡 레지스트리(상태, 진행률, 결과, 에러, 취소 플래그) 설계
- 워커 스레드/큐로 GA 실행
- SSE 구독자는 job 이벤트를 실시간 수신

4. Backtest/Alpha/Data API 이식
- Python 서비스 로직을 C++ 서비스 계층으로 이식
- XML 컴파일, 백테스트 계산, 저장/조회 로직 연결
- 날짜 검증/입력 검증 규칙 동일 반영

5. React 프론트 통신 계층 개편
- `runGAStream(fetch NDJSON)` 제거
- `createGAJob -> EventSource(/api/ga/stream/{jobId})` 패턴으로 교체
- 취소 버튼은 `cancel` API 호출로 연결
- 기존 UI 상태(진행률/부분결과/완료/에러)는 동일 UX 유지

6. 전환 및 컷오버
- Python/C++ 병행 검증 기간 운영
- 동일 입력에 대한 결과 비교 후 C++ 서버로 트래픽 전환
- FastAPI 코드 경로 제거

### Test Cases and Scenarios
- API 기능:
  - Backtest/GA/Alpha/Data 정상 응답
  - 검증 실패(날짜 역전, XML 오류, 없는 alpha)
- SSE:
  - `progress -> generation_end -> complete` 순서 검증
  - 연결 재시도/중도 끊김/서버 에러 이벤트 검증
  - job 취소 시 `error` 또는 `canceled` 이벤트 검증
- 프론트 통합:
  - 진행률 실시간 반영
  - 부분결과 렌더링
  - 완료 시 최종 결과 반영
- 회귀:
  - 핵심 메트릭(sharpe/mdd/pnl) 허용 오차 범위 검증

### Assumptions and Defaults
- 프레임워크: Drogon
- 스트리밍: SSE 표준(EventSource)
- 프론트: React/Next.js 유지, API 클라이언트는 재작성 허용
- 최종 상태: Python/FastAPI 미사용(완전 C++)
