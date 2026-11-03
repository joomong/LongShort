# LongShort

C++ 엔진과 Next.js 화면으로 알파를 작성·검증하는 연구 플랫폼이다. XML로 데이터와 계산 모듈을 연결하고, 단일·멀티알파 백테스트 및 GA를 실행한다. 사용자 C++ 모듈은 프로그램 시작 시 자동 발견·빌드한다.

플랫폼 코드와 개인 연구 파일을 분리한다. 개인 작업 공간은 `~/longshort_workspace/`다(`LONGSHORT_WORKSPACE`로 바꿀 수 있다).

```text
longshort_platform/       # cpp-engine, frontend, 공용 문서와 설정 예제
longshort_workspace/
  config.yaml            # 데이터·백테스트 설정
  alphas/                # YAML alphas_dir(기본 alphas): <name>/{alpha.xml, metadata.yml, results/, positions/}
  modules/               # 개인 C++ 모듈
  research/              # 연구 노트북·분석 파일
  .longshort/            # 자동 생성되는 빌드·실행 로그; Git/복사 대상에서 제외
```

기본 모듈은 `cpp-engine/src/modules/`에 유지한다. 개인 모듈만 workspace의 `modules/`에 둔다. 데이터는 설정의 공용 캐시 경로를 참조한다.

HTTP 요청 처리용 임시 파일은 `.longshort/uploads/`에 둔다.

웹에서 알파를 저장하면 `alpha.xml`은 태그별 줄바꿈과 2칸 들여쓰기를 적용해 저장한다. `<meta>`를 사용하는 알파도 같은 형식을 사용하며, 주석과 수식의 의미는 유지한다. 기존 저장 파일은 새로 쓰지 않는다.

## 실행

플랫폼 루트의 `longshort` 스크립트 하나로 개인 workspace를 만들고, 개인 모듈을 포함해 엔진을 빌드하고, 엔진과 화면을 함께 띄우고 내린다. 사용자마다 자기 플랫폼 복사본에서 자기 포트 두 개(엔진·화면)로 실행한다. 화면은 기동 시점에 엔진 주소를 고정하므로 사용자마다 화면도 따로 띄운다.

새 사용자는 플랫폼 복사본을 `~/longshort_platform`에 두고 아래 두 줄로 시작한다. 포트 두 개는 다른 사람과 겹치지 않게 고른다.

```bash
~/longshort_platform/longshort init                                  # 최초 1회: 도구 확인, 공용 deps 없으면 생성, 화면 npm ci
~/longshort_platform/longshort start --port 50017 --ui-port 50016   # 첫 기동: workspace 생성, 빌드, 엔진+화면 기동
~/longshort_platform/longshort start                                 # 다음부터는 저장된 포트로
~/longshort_platform/longshort start --build-only                    # 모듈 컴파일만 확인 (돌고 있는 엔진은 그대로)
~/longshort_platform/longshort restart                               # 모듈 수정 반영: 빌드 → 중지 → 기동 (빌드 실패 시 기존 엔진 유지)
~/longshort_platform/longshort stop                                  # 이 workspace의 엔진·화면만 내린다
```

- `init`은 cmake·c++·python3·node가 있는지 확인하고, 공용 빌드 의존성 `LONGSHORT_DEPS`(기본 `/home/base/longshort_deps`)가 없으면 만든다. 이 서버에는 Arrow C++와 jsoncpp·uuid 개발 헤더가 설치돼 있지 않아서(root 없음) `venv/`에 pip로 PyArrow 18.1.0을 넣어 `libarrow`·`libparquet`를 얻고, `prefix/`에 Ubuntu 패키지 `libjsoncpp-dev libjsoncpp25 uuid-dev libuuid1`을 `apt-get download`로 받아 풀어 둔다. 한 번 만들면 모든 사용자가 같이 쓰고, 이미 있으면 건너뛴다. 끝으로 자기 복사본의 `frontend/`에 `npm ci`를 한다.
- `start`는 `~/longshort_workspace/`에 `config.yaml`이 없으면 [설정 예제](config.example.yaml)로 설정을 만들고 `alpha_pool`에 플랫폼 내부 풀의 절대 경로를 기록한다. `alphas/ meta/ modules/ research/`도 만든다. 데이터·AFL·유니버스 경로는 공용 캐시라 사용자별로 다를 것이 없고, 사용자마다 달라야 하는 값은 포트 두 개뿐이다. deps가 없고 빌드 캐시도 없으면 `init`을 안내하고 멈춘다.
- 빌드 경로는 `.longshort/build/`, pid·로그·포트는 `.longshort/`와 `.longshort/logs/`에 둔다. 포트가 이미 쓰이고 있으면 기동하지 않고, `stop`은 자기가 띄운 pid만 내리므로 다른 사용자나 공용 엔진을 건드리지 않는다.
- 브라우저가 접속하는 주소는 `LONGSHORT_HOST`(기본: 서버의 첫 IP)로 바꿀 수 있고, workspace 위치는 `LONGSHORT_WORKSPACE`로 바꿀 수 있다. 자세한 것은 [빌드 안내](docs/build_and_validation.md).
- config의 `alpha_pool`이 플랫폼의 `alpha_pool/`을 가리키면 엔진이 켜질 때 풀의 `alpha.xml`을 풀 기준 설정(`alpha_pool_universe`, 북은 `TOP500_CAP`; 체결은 config 기본; 전체 기간)으로 한 번씩 병렬로 돌려 비용 후 일별 손익을 기억해 두고, 결과표 OS 오른쪽 `Corr` 열에 현재 알파와 풀 동일가중(그날 활동 중인 알파의 평균) 손익의 상관을 시뮬 구간 전체(IS+OS)로 보여준다. 풀에 알파가 추가되면 다음 요청 때 새것만 돌린다. 엔진은 풀에 쓰지 않는다.

엔진을 수동으로 실행하려면 먼저 빌드하고 실행파일에 설정을 전달한다. 화면은 별도 명령으로 실행할 수 있다. YAML의 상대 데이터·카탈로그 경로는 YAML이 있는 폴더 기준이며, AFL 카탈로그는 캐시와 함께 발행되는 `/home/base/cache/cache_v2_cash_1d/afl_catalog.json`을 절대경로로 가리킨다.

```bash
~/longshort_platform/longshort start --build-only
LONGSHORT_PORT=50017 ~/longshort_workspace/.longshort/build/longshort-server ~/longshort_workspace/config.yaml
cd ~/longshort_platform/frontend && NEXT_PUBLIC_API_URL="http://SERVER_ADDRESS:50017" npm run dev -- --port 50016
```

`NEXT_PUBLIC_API_URL`에는 브라우저에서 접근할 수 있는 엔진 주소를 지정한다. production은 해당 주소를 지정한 상태에서 `npm run build` 후 `npm start`로 실행한다.

기본 설정의 `backtest.end_date: latest`는 엔진을 시작하거나 재시작할 때 현재 파일에서 종료일을 정한다. 선택한 종목의 종가가 전부 결측인 끝행은 제외한다. 실행 중 자동 갱신은 하지 않으며, 재시작 후 열려 있는 웹 화면을 새로고침하면 새 날짜 범위를 받는다. 기간을 고정하려면 `end_date: '2026-07-31'`처럼 날짜를 지정한다.

공용 엔진(jgo의 `~/longshort_workspace`, 50007; 화면 50006)도 같은 스크립트로 띄우고 내린다. 데이터 캐시가 갱신되거나 모듈을 고쳤으면 `longshort restart`로 반영한다.

## 데이터와 계산

- `loader`는 캐시의 가격·수익률·이벤트·유니버스·그룹 패널과 AFL을 읽어 실행 입력을 준비한다. 설정은 `base: {cache, groups}`, 선택적인 `afl: {path, catalog}`, `universe`로 구성한다. 경로와 필드는 [설정 YAML과 데이터 연결](docs/config.md)을 따른다.
- 기본 필드는 `open high low close volume vwap cap sector industry returns adj_returns adj_factor`다. `returns`는 총수익률이며 보유 손익에도 사용하고, `adj_returns`는 분할조정 가격수익률이다. AFL은 `AFL.<이름>`(예: `AFL.EPSNumRevFY1C`)으로 등록하며 처음 사용하는 필드만 읽는다.
- sector·industry·모든 universe 마스크는 입력 준비 시 이전 거래일 기준으로 구성한다. XML `delay`는 알파 입력의 별도 지연이다. 가격·수익률·기업행동 이벤트는 원래 발생 날짜로 유지한다.
- 체결은 Close/OHLC4/VWAP 중 실행별로 선택한다. OHLC4는 raw `(open + high + low + close) / 4`, VWAP는 raw 당일 `vwap`을 사용한다. 설정 예제는 `vwap`, 편도 비용은 1.5bp, 주식 연환산은 252다.
- 손익은 고정 명목금액 근사다. 기존 보유 손익에 실제 시장 매매 비중 × `(close / fill - 1)`을 더하고 turnover 비용을 뺀다. 기업행동 position 이동 자체는 매매 손익이나 수수료를 만들지 않는다.
- 체결할 수 없는 관측에서 `missing_close_policy: carry`는 해당 주문을 보류하고 기존 노출을 동결·예약한다. `error`는 필요한 거래를 수행할 수 없으면 실행을 실패시킨다. 정확한 체결·결측 규칙은 [계산 계약](docs/math_contract.md)을 따른다.
- Sim period의 From/To는 실행별 거래 범위다(To 당일 포함, 휴장일이면 그 이전 마지막 데이터 bar까지). 시작 전 워밍업은 유지하며 종료일 뒤 계산은 멈춘다. 단일·combined·GA, 연도표·차트·상세 신호 조회가 같은 범위를 사용한다. To 미지정은 로드된 데이터 끝까지이고, 선택한 종료일에 강제청산을 추가하지 않는다.
- 평균·최소 보유 종목 수는 `avgPositionCount`, `minPositionCount`다. 누적 손익과 drawdown은 단리(additive) 기준이다. 성과 지표와 GA 적합도는 수수료 차감 전 gross 수익률 기준이고, 차트의 PnL은 gross 실선·net 점선으로 함께 보여준다.
- 목표 포지션은 `Σ|weight| = 1` 기준이다. 균형 롱숏은 long 0.5 / short 0.5이며 수익률을 추가로 2로 나누지 않는다. 결과의 `context.reportNotional`은 $20M 전체 gross 명목금액이며, 설정의 `initial_capital`과 별개다.
- 연도표 `sharpe[ir]`의 IR은 해당 연도 일별 gross 수익률의 평균 ÷ 표본 표준편차(ddof=1)이며 연환산하지 않는다. `quality = Sharpe × √|1 − 최대 순노출| × (1 − 최대 단일종목 비중)`이다. GA는 IS Sharpe를 최적화한다.
- 연도별 `fitness = Sharpe × √(|annualReturn| / max(turnover, 0.125))`다. API의 수익률·비중·turnover·drawdown은 소수 비율로 전달하며 퍼센트 변환은 화면 표시에서만 한다. 계산식의 상세한 단위는 [계산 계약](docs/math_contract.md)에 있다.
- combined는 각 알파의 시그널 행을 gross 1 비중으로 바꾼 뒤 더하고 최종 포트폴리오를 다시 정규화한다.

```xml
<data expr="returns" delay="1"/>
<tsmean days="20"/>
<zscore/>
<neutralize group="sector"/>
```

`neutralize`는 신호를 그룹(`sector` 또는 `industry`) 평균으로 뺀다. 그룹 코드는 `base:` 블록으로 등록되는 기본 필드 `sector`/`industry`에서 읽으며, 파일은 그룹 패널 생성 도구(`universe_builder/cache/README_build_groups.md`, 캐시 빌더 저장소)로 만든다. `group="sector|industry"`처럼 파이프로 여러 그룹을 한 번에 돌릴 수 있다.

산술 체인 태그 `<abs/> <log/> <sqrt/> <sign/> <inverse/> <reverse/> <power exp="2"/> <signedpower exp="0.5"/> <max value="0"/> <min value="3"/> <affine factor="1" offset="0"/>`는 앞 단계 출력에 원소별로 적용된다. `<data expr>` 안에서는 `abs(close - open) / close`, `log(volume)`, `signedpower(returns, 0.5)`처럼 `abs log sqrt sign power signedpower`를 함수로 쓴다(지수는 숫자만). 입력이나 결과가 NaN·무한대면 NaN이다.

시계열 태그 `<tsdelta/> <tssum/> <tsstddev/> <tsargmax/> <tsargmin/> <tsfill/>`(속성 `days`, 일부 `min_count`·`ddof`)와 횡단면·그룹 태그 `<winsorize std/> <normalize usestd limit/> <hump hump/> <group_rank group/> <group_mean group weight="equal|cap" mean="arithmetic|harmonic"/> <group_zscore group/> <rank_sharp exp/>`, 그리고 `<tsewma span|halflife|alpha/> <tsmax days/> <tsmin days/> <exp/>`(expr `exp(x)`도 가능), 종목당 비중 상한 `<truncate max side post/>`, 그리고 북 MR 알파의 `<ffd d thres/>`(분수 차분)와 `<pcaresid group train refit k var/>`(그룹별 롤링 PCA 잔차)가 있다. 정확한 규칙은 [수학 계약](docs/math_contract.md) Module Contracts를 따른다.

[사용자 모듈 규약](docs/modules.md) · [자동 빌드 설계](docs/build_and_validation.md) · [문서 목록](docs/README.md)

## 실행 결과와 화면

엔진은 `{context, alphas, combined}` 형태의 `SimulationResponse`를 반환한다. 각 `AlphaResult` 안에 요약 지표, 연도별 지표, 성과 시계열, 신호가 목적별로 담긴다. 실행 요청의 `output`은 `performance`, `signals`, `signalSeries`, `full` 중 선택한다.

성과 시계열은 엔진이 `max(simStart, isStart)`부터 누적손익과 gross drawdown을 계산하며 OS는 IS를 이어간다. 화면은 이 값을 차트로 전달하고 단위·색상·툴팁을 표시한다. 시계열 날짜는 epoch milliseconds 숫자이고 실행 설정 날짜는 `YYYY-MM-DD`다. 신호 히트맵은 엔진이 제공한 날짜별 중심·스케일을 사용한다.

결과의 `context`는 실행 당시 기간·유니버스·체결가·비용·표시 명목금액을 보존한다. 설정창을 바꾸거나 저장 결과를 불러와도 기존 결과의 기준을 바꾸지 않는다. 상세 조회는 해당 결과의 원래 context를 사용한다.

## C++ 구성

`cpp-engine/src/`에는 실행 기능별 구현을 두고 계산 모듈만 `modules/`로 구분한다. 내부 헤더는 구현 옆에, 사용자 모듈용 공개 헤더는 `cpp-engine/include/`에 둔다.

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

## 화면 구성

`frontend/src/`의 `api.ts`와 `types.ts`가 엔진 계약을 사용하고, `runStore.ts`가 설정·실행 결과·화면 선택 상태를 보관한다. `useAlphaSearch.ts`는 후보 스트림을, `useResultData.ts`는 선택 결과의 성과·신호 조회를 처리한다. `chart.ts`, `chartStyle.ts`, `format.ts`, `theme.ts`는 차트 동작·표시·테마를 맡는다. 화면 컴포넌트는 `frontend/src/components/`, Next.js 진입점은 `frontend/src/app/`에 둔다.

## 검증

프런트엔드 구현은 `frontend/src/`, 단위 검사는 `frontend/tests/unit/`, 브라우저 검사는 `frontend/tests/e2e/`에 둔다. 테스트 설정과 공통 초기화도 `tests/`에 모은다. ESLint·PostCSS 설정은 `package.json`에서 관리한다. 루트의 `next.config.mjs`, `tsconfig.json`, `tailwind.config.ts`는 각각 Next.js 실행, TypeScript 경로·타입, 화면 스타일 생성을 담당한다.

프론트는 `frontend/`에서 `npm test`, `npm run build`, `npm run lint`로 검사한다. 브라우저 검사는 `npm run test:e2e`이며 실제 엔진 통합에는 `PLAYWRIGHT_ENGINE_URL`을 지정한다.

C++ 검증은 CMake/CTest를 사용한다. 테스트 빌드와 HTTP 합성 fixture 생성은 [검증 안내](docs/build_and_validation.md)에 있다.
