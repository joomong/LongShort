# LongShort

C++ 엔진과 Next.js 화면으로 알파를 작성·검증하는 연구 플랫폼이다. XML로 데이터와 계산 모듈을 연결하고, 단일·멀티알파 백테스트 및 GA를 실행한다. 사용자 C++ 모듈은 프로그램 시작 시 자동 발견·빌드한다.

## 실행

C++ 컴파일러와 Arrow/Parquet·Boost·Drogon 등 의존성을 먼저 준비한다. [최초 빌드 안내](docs/module_discovery_build_2026-09-09.md)를 따른 뒤 개인 설정으로 실행한다.

```bash
python3 cpp-engine/tools/run_engine.py --config /path/to/my/config.yaml
```

기본 설정의 `backtest.end_date: latest`는 엔진을 시작하거나 재시작할 때 현재 파일에서 종료일을 정한다. 선택한 종목의 종가가 전부 결측인 끝행은 제외한다. 실행 중 자동 갱신은 하지 않으며, 재시작 후 열려 있는 웹 화면을 새로고침하면 새 날짜 범위를 받는다. 기간을 고정하려면 `end_date: '2026-07-31'`처럼 날짜를 지정한다.

별도 터미널에서 화면을 실행한다.

```bash
cd frontend
npm ci
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

엔진 포트는 launcher의 `--port`로 바꿀 수 있다. `NEXT_PUBLIC_API_URL`에는 브라우저에서 접근할 수 있는 엔진 주소를 지정한다. production은 해당 주소를 지정한 상태에서 `npm run build` 후 `npm start`로 실행한다.

## 데이터와 계산

- 기존 long Parquet 및 필드별 wide Parquet을 C++이 직접 읽는다. [주식·AFL 설정 예제](cpp-engine/examples/datasets/README.md)는 날짜별 유니버스와 필요한 AFL 필드만 읽는 방법을 설명한다.
- `base:` 블록(가격 캐시 + 그룹 패널 디렉토리)을 주면 기본 필드 `open high low close volume vwap cap sector industry`가 설정에 나열하지 않아도 시작 시 전부 읽힌다. 파일이 하나라도 없거나 그룹 패널이 가격 달력을 덮지 못하면 기동을 거부한다.
- 기본은 종가 체결, D1(mode=1), 편도 비용 5bp다. D0/D1과 비용·연환산 설정은 명시된 값으로 계산한다. 기본 연환산은 365이며 주식 예제는 252다.
- 유니버스는 가격과 별도 마스크다. 신호 날짜에 적용하고 실행 모드의 지연을 함께 받는다.
- 결측 종가는 `backtest.missing_close_policy`(기본 `carry`)로 처리한다: 보유분은 마지막 유효 종가로 동결(체결·비용·손익 없음)했다가 호가 재개 시 누적 수익을 인식하고, 호가 없는 종목의 주문은 체결하지 않는다. `error`는 기존 즉시 실패 동작이다. 상폐는 `events:` 파일의 명시 종료일 이후 호가가 끊긴 첫 bar에 정산하며, 수익률은 그 bar에서 딜 조건(주당 현금 + 교환비×인수사 종가)으로 엔진이 계산한다(계산 불가 시 파일 수익률 > `default_delisting_return`, 기본 0은 가정). 주식 몫만 인수사 보유 가중치로 승계하고 현금 몫은 실현한다. 캐시 종가는 이후 분할까지 재작성된 기준이므로 딜 조건은 생성기가 캐시 단위로 환산한 `*Cache` 컬럼을 쓰고, 로더가 파일의 인수사 캐시 종가를 패널과 대조해 캐시 재구축 뒤 낡은 파일이면 기동을 거부한다. 이벤트 파일은 `cpp-engine/tools/data/{refresh_master.py,build_events.py}`로 xfeed에서 만든다(`data/events/`). 결과의 `missingData`에 동결·정산·승계 건수를 보고한다. 자세한 규칙은 [데이터 예제](cpp-engine/examples/datasets/README.md)를 본다.
- `avgBet`은 평균 보유 종목 수, `minBet`은 기간 중 최소 보유 종목 수다. 누적 손익과 drawdown은 단리(additive) 기준이다.
- combined의 기본은 각 알파를 정규화한 동일 비중 결합 후 최종 포트폴리오 정규화다. 기존 raw 합산은 `combined_mode: raw`로 선택한다.

```xml
<data expr="returns" delay="0"/>
<tsmean days="20"/>
<zscore/>
<neutralize group="sector"/>
```

`neutralize`는 신호를 그룹(`sector` 또는 `industry`) 평균으로 뺀다. 그룹 코드는 `base:` 블록으로 등록되는 기본 필드 `sector`/`industry`에서 읽으며, 파일은 [그룹 패널 생성 도구](cpp-engine/tools/data/README_group_panels.md)로 만든다. `group="sector|industry"`처럼 파이프로 여러 그룹을 한 번에 돌릴 수 있다.

[사용자 모듈 규약](modules/README.md) · [자동 빌드 설계](docs/module_discovery_2026-09-09.md) · [계산·데이터 변경과 검증 기록](docs/logic_completion_2026-09-11.md)

## 검증

프론트는 `frontend/`에서 `npm test`, `npm run build`, `npm run lint`로 검사한다. 브라우저 검사는 `npm run test:e2e`이며 실제 엔진 통합에는 `PLAYWRIGHT_ENGINE_URL`을 지정한다.

C++의 완전한 검증은 CMake/CTest를 사용한다. HTTP 합성 fixture 생성과 환경변수, 실제 주식 데이터 oracle 명령은 [검증 안내](docs/logic_completion_data_2026-09-11.md)에 있다. `run_logic_tests.py`는 Arrow/HTTP를 제외한 가벼운 엔진 검사 경로다.
