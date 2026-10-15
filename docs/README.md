# 플랫폼 문서와 연구 노트북

실행에 필요한 안내는 이 폴더에, 데이터와 전략의 판단 근거는 주제별 연구 노트북에 둔다. 계산 계약은 현재 코드와 엔진 수학 문서를 기준으로 확인한다.

| 목적 | 안내 |
|---|---|
| 빌드, 실행, 모듈 발견, 검증 | [빌드와 검증](build_and_validation.md) |
| 데이터 설정과 현재 thin 계약 | [데이터 계약](data_contract.md) |
| 손익, 체결, 지표의 정확한 정의 | [엔진 계산 계약](../cpp-engine/docs/math_contract.md) |
| 개인 데이터 설정 | [데이터 설정 예제](../cpp-engine/examples/datasets/README.md) |
| 사용자 계산 모듈 작성 | [모듈 규약](../modules/README.md) |

## 연구 노트북

| 질문 | 노트북 |
|---|---|
| 데이터와 손익 계산을 믿을 수 있는가 | [데이터와 손익](../../longshort_workspace/research/notebooks/data_and_pnl_contract.ipynb) |
| 어떤 종목을 어떤 규칙으로 거래할 것인가 | [유니버스 설계](../../longshort_workspace/research/notebooks/universe/membership.ipynb) |
| 알파는 무엇을 예측하고 언제 가용한가 | [알파 정의와 정보 시점](../../longshort_workspace/research/notebooks/alpha_definition_and_timing.ipynb) |
| 알파를 어떻게 합치고 위험을 제한할 것인가 | [포트폴리오와 위험](../../longshort_workspace/research/notebooks/portfolio_construction_and_risk.ipynb) |
| 전략 변경과 실제 운용 차이를 어떻게 설명하는가 | [전략 재현과 실제 운용](../../longshort_workspace/research/notebooks/strategy_reproduction_and_live.ipynb) |

노트북에 필요한 함수와 경량 관측 입력이 포함되어 있다. 저장된 과거 연구 결과, 직접 재계산한 범위, 합성 검증, 실제 원장과 운용 개시 전 재구성 이력은 각 본문에서 구분한다. 그림은 Matplotlib 기본 테마를 사용한다.

검증 도구가 새 결과를 작성할 때는 임시 출력 폴더나 지정한 연구 결과 폴더를 사용한다. 이 문서 폴더에 날짜별 실행 보고서를 다시 쌓지 않는다.
