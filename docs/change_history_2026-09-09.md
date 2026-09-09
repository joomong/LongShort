# 변경 이력 — 2026-09-09

기준 커밋은 `6a70f37`이다. 기존 close-to-close 손익 모델의 오류 수정과, 결과를 보존하는 리팩토링을 구분해 기록한다.

## 1. 계산 오류 수정

커밋: `7f34b5b` — `fix(engine): correct state reset and close-to-close metrics`

- 보유·체결 종가가 없을 때 손익을 0으로 숨기는 동작을 오류로 처리.
- 시계열 모듈 재실행 시 버퍼·누적 상태 초기화.
- 초기 손실을 포함하는 요약/연도별 단리 MDD 및 프론트 낙폭 계산.
- 데이터 뒤의 IS 시작일, 잘못된 시뮬레이터 입력과 GA population/generations 검사.
- avgBet=평균 보유 종목 수, minBet=최소 보유 종목 수라는 기존 계산의 의미를 계약 문서에 명시.

[수정 단계 기록](logic_fixes_2026-09-09.md)

## 2. 계산 결과를 보존하는 리팩토링

커밋: `0dca046` — `refactor: clarify position metrics and remove unused state`

- 평균/최소 보유 종목 수와 보유 bar 수의 내부 이름을 명확화.
- 미사용 gross/N 배열·인자·시뮬레이터 연환산 필드, GA dead code 제거.
- 포트폴리오 전이, 프론트 IS/OS 타입·변환의 중복 통합.
- 시그널 복사, plot 구간 복사, histogram 평탄화 배열, 누적손익 임시 배열, 프론트 미사용 record 제거.
- 외부 JSON 키·화면 라벨·수치·실행 시점·비용 기본값 보존.
- 교차 리뷰에서 확인한 입력 검증 부수효과와 raw signal observer 계약까지 보존.

[리팩토링 종합 보고서](refactor_2026-09-09.md) · [엔진 교차 리뷰](refactor_2026-09-09_peer_review_engine.md) · [프론트 교차 리뷰](refactor_2026-09-09_peer_review_frontend.md)

최종 검증:

- 엔진 130/130 통과(실패/skip 0).
- 전후 60개 시나리오 JSON 바이트 동일, serializer 4,000건 동등성 확인.
- 프론트 16개 테스트 및 adapter 9개 시나리오 통과.
- 최신 main/benchmark 구문·타입 검사 통과.
- 검증 환경의 Boost 1.85 헤더·serialization 라이브러리를 일치시켜 기존 checkpoint fixture 통과.
- 프론트 색상 모듈 일부는 테스트 대체물을 사용했다. 전체 Next 빌드·브라우저·HTTP 실행·Parquet 로딩 검증은 포함하지 않았다.

현재 검증 절차는 [검증 문서](refactor_2026-09-09_validation.md)와 [실행 스크립트](../cpp-engine/tools/run_logic_tests.py)를 참조한다.

## 3. 리뷰와 조사 기록 보존

커밋 제목: `docs: archive reviews and record change history`

9월 8~9일 리뷰, 당시 코드에 대한 재현 스크립트와 결과, AFL 규모 조사와 데이터 연결 설계 문서를 함께 보존한다. 데이터 연결 설계는 조사 기록이며 DB/YAML 확장이나 새로운 체결 모델을 구현한 변경은 아니다.

`docs/review_2026-09-08/`, `docs/review_2026-09-09/`의 probe는 당시 소스 기준의 관측 기록이다. 현재 코드 검증에는 위의 실행 스크립트를 사용한다.

[기존 통합 리뷰](review_2026-09-08.md) · [추가 리뷰](review_2026-09-09.md) · [연결 설계 조사](data_connection_design.md)

남은 정리 후보와 기존 논리 문제는 종합 보고서에서 적용 완료 항목과 구분했다.
