# LongShort

C++ 엔진과 Next.js 화면으로 알파를 작성·검증하는 연구 플랫폼이다. XML로 데이터와 계산 모듈을 연결하고, 단일·멀티알파 백테스트 및 GA를 실행한다. 사용자 C++ 모듈은 프로그램 시작 시 자동 발견·빌드한다.

플랫폼 코드와 개인 연구 파일을 분리한다. 현재 개인 작업 공간은 `/home/jgo/longshort_workspace/`다.

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

## 실행

C++ 컴파일러와 Arrow/Parquet·Boost·Drogon 등 의존성을 먼저 준비한다. [최초 빌드 안내](docs/build_and_validation.md)를 따른 뒤 개인 설정으로 실행한다.

현재 작업 공간에서 엔진을 실행한다. 빌드 경로는 설정 파일 옆 `.longshort/build/`로 자동 결정된다.

```bash
cd /home/jgo/longshort_workspace
python3 /home/jgo/longshort_platform/cpp-engine/tools/run_engine.py --config config.yaml --port 50007
```

다른 작업 공간은 `--config /path/to/my/config.yaml`로 지정한다. YAML의 상대 데이터·카탈로그 경로는 YAML이 있는 폴더를 기준으로 해석한다. 현재 workspace는 공용 `afl_catalog.json`의 절대경로를 사용한다. 최초 빌드에 필요한 의존성 옵션은 [빌드 안내](docs/build_and_validation.md)를 따른다.

기본 설정의 `backtest.end_date: latest`는 엔진을 시작하거나 재시작할 때 현재 파일에서 종료일을 정한다. 선택한 종목의 종가가 전부 결측인 끝행은 제외한다. 실행 중 자동 갱신은 하지 않으며, 재시작 후 열려 있는 웹 화면을 새로고침하면 새 날짜 범위를 받는다. 기간을 고정하려면 `end_date: '2026-07-31'`처럼 날짜를 지정한다.

별도 터미널에서 화면을 실행한다.

```bash
cd /home/jgo/longshort_platform/frontend
npm ci
NEXT_PUBLIC_API_URL="http://SERVER_ADDRESS:50007" npm run dev -- --port 50006
```

엔진 포트는 launcher의 `--port`로 바꿀 수 있다. `NEXT_PUBLIC_API_URL`에는 브라우저에서 접근할 수 있는 엔진 주소를 지정한다. production은 해당 주소를 지정한 상태에서 `npm run build` 후 `npm start`로 실행한다.

`SERVER_ADDRESS`는 브라우저에서 접속하는 60서버 주소로 바꾼다. 현재 기동된 웹은 기존 주소와 50006 포트로 사용한다.

현재 엔진 재시작은 `python3 /home/jgo/longshort_platform/cpp-engine/tools/restart_engine.py --force`를 사용한다. 기본 설정·실행파일·로그는 현재 사용자의 `~/longshort_workspace/`에서 찾는다. 데이터 갱신 DAG도 이 재시작 도구를 사용한다. 다른 설정에는 기존 `--config`, `--binary`, `--log-dir` 옵션을 지정한다.

개인 모듈을 수정했다면 workspace에서 `run_engine.py --config config.yaml --build-only`로 먼저 증분 빌드한 뒤 위 재시작 명령을 실행한다. 재시작 도구 자체는 컴파일하지 않는다.

## 데이터와 계산

- 기존 long Parquet 및 필드별 wide Parquet을 C++이 직접 읽는다. [주식·AFL 설정 예제](cpp-engine/examples/datasets/README.md)는 날짜별 유니버스와 필요한 AFL 필드만 읽는 방법을 설명한다.
- `base:` 블록(가격 캐시 + 그룹 패널 디렉토리)을 주면 기본 필드 `open high low close volume vwap cap sector industry`가 설정에 나열하지 않아도 시작 시 전부 읽힌다. 파일이 하나라도 없거나 그룹 패널이 가격 달력을 덮지 못하면 기동을 거부한다.
- 일반 설정의 `backtest.execution_price` 기본값은 `close`이고, 현재 실행 예제 `us_equity.yaml`의 기본값도 `close`다. 화면의 Execution에서 Close/OHLC4/VWAP를 실행별로 선택할 수 있다. VWAP는 raw 당일 `vwap`으로 체결하며, `(close / VWAP - 1)` 매매 손익과 결측·범위 불일치·오류 규칙은 OHLC4와 같고 exact `vwap/high/low`가 필요하다(vwap이나 종가가 [low, high] 밖이면 미체결). BRAIN Delay-1 손익은 종가 보유 손익 + VWAP 매매 leg로 재현된다. OHLC4는 raw `(open + high + low + close) / 4`이며 원본 가격을 바꾸지 않는다. Interval/실행 mode와 엔진의 자동 하루 지연은 제거했다. 지연은 XML의 `delay="1"`로 명시하며 추가로 지연하지 않는다. 편도 비용 1.5bp와 목표 명목금액 규칙은 유지된다. 기본 연환산은 365이며 주식 예제는 252다. `/api/config`의 `executionPrice`와 화면의 Execution 선택창에서 체결 기준을 고른다.
- 손익 모델은 원래 팀의 고정 명목금액 근사(fixed-notional approximation)이며 정확한 주식 수 원장은 아니다. OHLC4에서는 기존 보유 손익에 실제 시장 매매 비중 × `(close / OHLC4 - 1)`을 더한다. 기업행동 position 이동 자체는 이 매매 손익이나 수수료를 만들지 않는다.
- OHLC4의 `open/high/low/close`는 exact 정렬 원천이 필수이며, 필드 선택을 제한하거나 lazy로 선언해도 로드한다. 필드·파일·컬럼 부재 또는 asof 가격 원천은 오류이며 종가 체결로 대체하지 않는다. 개별 OHLC 셀 결측에서 `carry`는 매매를 막고 기존 노출을 동결·예약하되 관측 종가와 보유 수익률은 정상 반영한다. `error`는 필요한 매매를 실행할 수 없으면 실패한다. 시가·종가가 고가·저가 범위를 벗어난 경우에도 `carry`는 주문을 보류하며, 0 이하나 무한대 가격은 오류로 거부한다.
- 유니버스는 가격과 별도 마스크다. 신호 날짜에 적용하고 실행 모드의 지연을 함께 받는다.
- Sim period의 From/To는 실행별 거래 범위다(To 당일 포함, 휴장일이면 그 이전 마지막 데이터 bar까지). 시작 전 워밍업은 유지하며 종료일 뒤 계산은 멈춘다. 단일·combined·GA, 연도표·차트·상세 신호 조회가 같은 범위를 사용한다. To 미지정은 로드된 데이터 끝까지이고, 선택한 종료일에 강제청산을 추가하지 않는다.
- 현재 thin-v1 캐시는 `base.cache/manifest.json`으로 판별한다. `close`는 조정 전 가격, `returns.parquet`의 `returns`는 총수익률로 기본 신호와 보유 손익에 쓰며, `adj_returns`는 분할조정 가격 수익률이다. `events.parquet`은 position 이동을 처리한다. 옛 `returns_total.parquet`이 남은 thin 캐시는 다시 빌드해야 한다. `universe.name`으로 구간형 유니버스를 고른다. thin 설정에서는 최상위 `events:`와 `backtest.corporate_actions:`를 쓰지 않는다. [thin 예제와 전환 상태](cpp-engine/examples/datasets/README.md)를 본다.
- 일반 nonthin long/wide 데이터 설정도 계속 읽는다. 이 경로의 XML `returns`는 종가 변화율에서 파생된다. 기업행동 손익을 포함한 thin 총수익률은 제공되지 않으며, 기존 최상위 `events:`·`backtest.corporate_actions:` 설정은 현재 로더가 거부한다. 결측 종가는 `backtest.missing_close_policy`(기본 `carry`)가 동결하고 시세 재개 시 누적 움직임을 인식한다. 자세한 규칙은 [데이터 예제](cpp-engine/examples/datasets/README.md)에 있다.
- `avgBet`은 평균 보유 종목 수, `minBet`은 기간 중 최소 보유 종목 수다. 누적 손익과 drawdown은 단리(additive) 기준이다. 성과 지표(요약·연도별·drawdown·IS Sharpe 정렬·GA 적합도)는 수수료 차감 전 gross 수익률 기준이고, 차트의 PnL은 gross 실선·net 점선으로 함께 보여준다.
- 목표 포지션은 `Σ|weight| = 1` 기준이다. 균형 롱숏은 long 0.5 / short 0.5이며 수익률을 추가로 2로 나누지 않는다. 화면의 $20M book은 이 전체 gross 명목금액이다. 여기서 gross 노출과 수수료 차감 전 gross 손익은 서로 다른 개념이다.
- 연도표 `sharpe[ir]`의 IR은 해당 연도 일별 gross 수익률의 평균 ÷ 표본 표준편차(ddof=1)이며 연환산하지 않는다. 유효 수익률이 2개 미만이거나 표준편차가 0이면 0이다. `quality = Sharpe × √|1 − 최대 순노출| × (1 − 최대 단일종목 비중)`이며, 최대 순노출은 IS/OS 각각에서 `max|long − absolute short|`, 비중은 0~1 단위다. GA는 quality 대신 IS Sharpe를 최적화한다.
- 연도표 margin 오른쪽의 `fitness = Sharpe × √(|ann.ret / 100| / max(daily turnover, 0.125))`다. [참고 수식](https://github.com/Xiaoyu-hub/worldquant/blob/main/raw/articles/worldquant-alpha-mining-strategies.md)을 사용하되 Returns는 현재 표와 같은 전체 gross book·레버리지 1 기준이며 2배로 바꾸지 않는다. Turnover는 일별 소수 비율(20%=0.2)이고 최소 분모 0.125는 12.5%다.
- combined의 기본은 각 알파를 정규화한 동일 비중 결합 후 최종 포트폴리오 정규화다. 기존 raw 합산은 `combined_mode: raw`로 선택한다.

```xml
<data expr="returns" delay="1"/>
<tsmean days="20"/>
<zscore/>
<neutralize group="sector"/>
```

`neutralize`는 신호를 그룹(`sector` 또는 `industry`) 평균으로 뺀다. 그룹 코드는 `base:` 블록으로 등록되는 기본 필드 `sector`/`industry`에서 읽으며, 파일은 [그룹 패널 생성 도구](cpp-engine/tools/data/README_group_panels.md)로 만든다. `group="sector|industry"`처럼 파이프로 여러 그룹을 한 번에 돌릴 수 있다.

[사용자 모듈 규약](modules/README.md) · [자동 빌드 설계](docs/build_and_validation.md) · [연구와 계약 문서](docs/README.md)

## 검증

프론트는 `frontend/`에서 `npm test`, `npm run build`, `npm run lint`로 검사한다. 브라우저 검사는 `npm run test:e2e`이며 실제 엔진 통합에는 `PLAYWRIGHT_ENGINE_URL`을 지정한다.

C++의 완전한 검증은 CMake/CTest를 사용한다. HTTP 합성 fixture 생성과 환경변수, 실제 주식 데이터 oracle 명령은 [검증 안내](docs/build_and_validation.md)에 있다. `run_logic_tests.py`는 Arrow/HTTP를 제외한 가벼운 엔진 검사 경로다.
