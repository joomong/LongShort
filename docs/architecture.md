# 내부 구조와 실행 흐름

플랫폼 저장소는 공용 엔진·화면 코드를 Git으로 관리한다. 승인 전략 정의는 소스 옆 공용 폴더에 둔다.
개인 설정, 연구 중인 알파, 사용자 모듈과 생성 파일은 workspace에 둔다.

```text
/home/base/longshort/
  longshort_platform/   공용 엔진·화면 코드 저장소
  dependencies/         공용 C++ 라이브러리
  alpha_pool/           승인 알파 정의와 설명
  meta_pool/            메타 전략 정의용 폴더
  portfolio/            포트폴리오 정의용 폴더

longshort_workspace/
  config.yaml           데이터·실행 설정
  alphas/               개인 저장 알파와 실행 결과
  meta/                 현재 엔진이 읽는 메타 구성원
  modules/              개인 C++ 모듈
  research/             연구 노트북·분석 파일
  .longshort/
    build/              개인 엔진 빌드
    frontend/           개인 Next.js 출력·타입·캐시
    logs/               개인 로그
```

`alphas/`와 `meta/`의 실제 경로는 설정의 `alphas_dir`, `meta_dir`로 지정한다.
`alpha_pool/`은 현재 Corr 계산에 연결되어 있다.
`meta_pool/`과 `portfolio/`는 준비된 폴더이며 아직 실행 기능에서 읽지 않는다.
데이터 캐시는 저장소 밖의 설정 경로에서 읽는다.

## C++ 엔진

`cpp-engine/src/`에는 실행 기능별 구현을 두고 계산 모듈만 `modules/`로 구분한다.
내부 헤더는 구현 옆에, 사용자 모듈용 공개 헤더는 `cpp-engine/include/`에 둔다.

| 구현 | 역할 |
|---|---|
| `loader` | 설정·캐시 읽기와 실행 입력 준비 |
| `xml_compiler` | XML·수식 해석, 모듈 등록과 계산 연결 |
| `simulator_engine` | 날짜별 신호·포지션 실행과 성과 지표 |
| `alpha_search` | 후보 생성·평가와 GA 탐색 |
| `meta` | 메타 구성원 실행과 결합 가중치 |
| `alpha_storage` | 알파 정의·실행 결과·목표 비중 저장 |
| `pool_correlation` | 승인 알파 풀과 수익률 상관관계 비교 |
| `http_server` | HTTP 요청과 JSON 응답 |
| `parallel_executor` | 공통 병렬 실행과 취소 |

실행 흐름은 다음과 같다.

```text
config.yaml + 데이터 캐시 → loader → 실행 입력
웹 XML + 실행 설정 → http_server → xml_compiler → simulator_engine
                                              → alpha_search (GA)
계산 결과 → 성과 지표·시계열·신호 → JSON → 웹 화면
alpha_pool/<이름>/alpha.xml → 구성원 시뮬레이션 → 동일가중 수익률 → Corr
```

풀은 설정의 유니버스·체결가·비용으로 평가하며 엔진은 풀 파일에 쓰지 않는다.
풀과의 상관관계는 실행 구간 전체(IS+OS)의 비용 후 일별 수익률로 계산한다.

## 웹 화면

`frontend/src/api.ts`와 `types.ts`가 엔진 계약을 사용하고,
`runStore.ts`가 설정·실행 결과·화면 선택 상태를 보관한다.
`useAlphaSearch.ts`는 후보 스트림을, `useResultData.ts`는 성과·신호 상세 조회를 처리한다.
`chart.ts`, `chartStyle.ts`, `format.ts`, `theme.ts`는 차트 동작·표시·테마를 맡는다.
화면 컴포넌트는 `src/components/`, Next.js 진입점은 `src/app/`에 둔다.

엔진이 지표와 성과 시계열, 신호의 날짜별 중심·스케일을 계산한다.
화면은 실행 당시 context를 유지하며 단위 변환, 차트, 색상과 툴팁을 표시한다.
결과 형식과 계산 규칙은 [계산 계약](math_contract.md)을 따른다.

검사는 `frontend/tests/unit/`과 `frontend/tests/e2e/`에 둔다.
테스트 설정도 `tests/`에 모으며 ESLint·PostCSS 설정은 `package.json`에서 관리한다.
