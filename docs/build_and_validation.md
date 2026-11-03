# 빌드 실행과 검증

개인 설정 옆의 모듈을 발견하고 증분 빌드한 뒤 C++ 서버를 실행한다. 화면은 별도 Next.js 프로세스로 실행한다. 연구 검증에서는 기존 공유 서버를 보존하고 별도 포트를 사용한다.

## 서버와 화면 실행

루트 `longshort`가 CMake를 직접 호출해 빌드하고 엔진과 화면을 실행한다([README](../README.md) 실행 절). 별도 Python 실행기는 없다.

```bash
~/longshort_platform/longshort start --port <엔진포트> --ui-port <화면포트>
```

기본 모듈 경로는 설정 파일 옆 `modules/`, 빌드 경로는 `.longshort/build/`다. 모듈 추가나 수정은 다음 프로그램 시작 때 반영된다. 모듈 생성이나 빌드가 실패하면 이전 실행 파일로 기동하지 않는다.

알파 저장 위치는 YAML 최상위 `alphas_dir`(기본 `alphas`, 상대경로는 YAML 폴더 기준)이며 시작할 때 생성·쓰기 확인에 실패하면 엔진이 뜨지 않는다. 저장 형식은 `<alphas_dir>/<name>/{alpha.xml, metadata.yml, results/, positions/}`이며, `positions/<id>.parquet`은 체결 후 보유 비중이 아니라 매 거래 bar의 주문 목표 비중(정규화된 target, AlphaPos 대응)이다. `alphas_dir`는 renameat2(RENAME_NOREPLACE)를 지원하는 로컬 파일시스템(ext4/xfs/btrfs/tmpfs)이어야 하며 sshfs/NFS는 시작 시 거부된다. 진행 중인 저장은 자기 `.<name>.tmp-*` 폴더에 flock을 잡고, 시작 시에는 잠금이 풀린 것만 지우므로 여러 엔진이 한 `alphas_dir`를 공유할 수 있다. 진행 중인 저장은 `lslocks`로 보이며, 엔진이 도는 동안 `.tmp-*` 폴더를 손으로 지우지 않는다. 내장 모듈은 플랫폼의 `cpp-engine/src/modules/`에 있다. workspace를 복사할 때 `.longshort/`는 제외하고 새 빌드를 생성한다. YAML의 데이터·카탈로그 상대경로는 YAML 폴더를 기준으로 하므로, 현재 workspace의 AFL 카탈로그는 플랫폼 파일의 절대경로로 지정한다.

HTTP 프레임워크의 큰 요청 처리용 임시 파일은 `.longshort/uploads/`에 생성된다. workspace 루트에 별도 `uploads/`를 만들지 않는다. 연구 노트북과 분석 파일은 workspace의 `research/`에 둔다.

```bash
LONGSHORT_WORKSPACE=/path/to/workspace ./longshort start --build-only \
  --modules-dir /path/to/modules \
  --build-dir /path/to/build --jobs 4 --build-type Release
```

`start --build-only`는 서버를 시작하지 않고 빌드까지만 수행한다. `--build-dir`는 해당 호출의 빌드·실행에 적용하므로 `restart`에서도 같은 경로를 지정한다. 빌드 잠금(`flock`)과 소스 경로 검사를 수행하며 실패한 빌드 뒤에 이전 서버를 새로 기동하지 않는다. `--jobs`, `--build-type`, `--cmake-define NAME=VALUE`로 빌드를 설정한다. 빌드 디렉터리를 모듈 경로 안에 두거나 같은 빌드 경로에서 동시에 configure/build하지 않는다.

화면은 `frontend/`에서 실행한다.

```bash
cd ~/longshort_platform/frontend
npm ci
NEXT_PUBLIC_API_URL="http://SERVER_ADDRESS:<엔진포트>" npm run dev -- --port <화면포트>
```

`SERVER_ADDRESS`는 브라우저가 접근할 수 있는 서버 주소로 바꾼다. Production은 같은 API 주소를 지정해 `npm run build` 후 `npm start`로 실행한다.

## 빌드 의존성과 모듈 발견

C++17 컴파일러와 CMake, Arrow/Parquet, Drogon 및 저장소 CMake가 요구하는 라이브러리가 필요하다. pugixml·yaml-cpp·drogon·Eigen(3.4.0, 헤더 전용, SHA256 고정)은 configure 때 FetchContent로 내려받으며 테스트 빌드는 googletest도 사용한다. 최초 configure에는 네트워크가 필요하다. CMake 패키지를 이미 준비한 경로를 우선 사용한다. PyArrow 번들 Arrow/Parquet를 쓰면 해당 Python을 명시한다.

```bash
./longshort start --build-only \
  --cmake-define LONGSHORT_PYARROW_PYTHON=/path/to/python-with-pyarrow \
  --cmake-define CMAKE_PREFIX_PATH=/path/to/dependency-prefix
```

플랫폼 루트의 `longshort init`이 이 둘을 `LONGSHORT_DEPS`(기본 `/home/base/longshort_deps`)에 만들고, `longshort start`가 `LONGSHORT_PYARROW_PYTHON=$DEPS/venv/bin/python`, `CMAKE_PREFIX_PATH=$DEPS/prefix`로 넘긴다. 서버에 Arrow C++ 패키지와 jsoncpp·uuid 개발 헤더가 설치돼 있지 않아서(root 없음; 런타임 `libjsoncpp.so.25`·`libuuid.so.1`만 있다) `init`은 `venv/`를 시스템 python3로 만들어 `pip install pyarrow==18.1.0`(운영 엔진이 링크된 버전; `LONGSHORT_PYARROW_VERSION`으로 바꿈)으로 `libarrow`·`libparquet`를 얻고, `prefix/`에는 `apt-get download libjsoncpp-dev libjsoncpp25 uuid-dev libuuid1`을 `dpkg -x`로 풀어 `usr/` 아래를 옮긴다(`include/jsoncpp/json`, `include/uuid`, `lib/x86_64-linux-gnu/{libjsoncpp.so*, libuuid.so*, cmake/jsoncpp}`). `-dev` 패키지만 풀면 `.so` 실체가 런타임 패키지에 있어 CMake가 imported target 파일 부재로 실패하므로 런타임 패키지까지 함께 푼다. 이 배치면 jsoncpp CONFIG 패키지와 Drogon의 FindUUID가 추가 지정 없이 찾는다. 빌드 결과는 venv 안의 `libarrow.so`·`libparquet.so`를 실행 시에도 참조하므로 deps는 모든 사용자가 읽을 수 있는 곳에 있어야 한다. 폴더가 없어도 이미 configure 된 빌드는 CMake 캐시의 경로를 쓴다.

헤더와 라이브러리는 같은 설치에서 가져온다. 사용자 모듈의 `.h/.hpp/.cpp/.cc/.cxx`를 발견하고 빌드 경로의 `generated/`에 헤더 include와 `register_<파일명>(registry)` 호출 목록만 생성한다. 모듈의 타입·속성은 C++ 등록 선언을 컴파일한다. 점으로 시작하는 하위 경로는 검색에서 제외된다. 변경 없는 registry는 다시 쓰지 않고, 파일 추가·수정·삭제는 증분 빌드가 반영한다. 모듈 문법과 속성은 [모듈 규약](modules.md)을 따른다.

개인 모듈을 고쳤거나 데이터 캐시가 갱신되었으면 `longshort restart`로 반영한다(빌드 → 중지 → 기동; 빌드 실패 시 기존 엔진 유지). `longshort start --build-only`는 서버를 건드리지 않고 컴파일만 확인한다.

## AVX2 빌드

Decay의 일반·AVX2 커널은 `src/modules/decay_kernel.*`에 함께 둔다. `LONGSHORT_ENABLE_AVX2=ON`(기본)이면 엔진과 이를 사용하는 대상에 AVX2 컴파일 옵션을 적용하며 `__AVX2__` 전처리 조건으로 가속 구현을 포함한다. 이 실행파일은 AVX2 지원 CPU에서 사용한다. 런타임 CPU 탐지는 하지 않는다. 일반 연산 빌드는 다음과 같다.

```bash
./longshort start --build-only --cmake-define LONGSHORT_ENABLE_AVX2=OFF
```

## 모듈 내부 병렬 계산

단일 알파 `/api/simulate`는 요청의 `threads`(기본 20, 1..32) 안에서 최대 4개 실행 스레드로 FFD, TsRank, TsMax/TsMin/TsArgMax/TsArgMin을 종목별로 분할한다. 호출 스레드도 계산에 참여하고 나머지 워커는 한 시뮬레이션 동안 재사용한다. 날짜 순서와 각 종목의 합산 순서는 유지한다. 종목 수와 계산량이 작거나 `threads=1`이면 단일 스레드로 실행한다.

GA 후보, 여러 알파, 메타 구성원, 저장 결과 재계산은 기존 바깥쪽 병렬 실행을 사용하고 각 모듈 안에서는 추가 스레드를 만들지 않는다. 모듈 계산이 끝나야 다음 모듈과 체결 계산을 수행하며, 예외·취소 시 워커를 정리한다. C++에서 직접 시뮬레이터를 사용하는 경우 `SimulatorConfig.module_threads`로 실행 범위를 지정한다(기본 1).

## C++와 HTTP 검증

테스트 빌드는 별도 경로에 만들고 `LONGSHORT_BUILD_TESTS=ON`으로 설정한다. 실제 실행 결과를 기준으로 통과·실패·skip을 보고한다.

```bash
cmake -S cpp-engine -B /path/to/test-build \
  -DLONGSHORT_BUILD_TESTS=ON \
  -DLONGSHORT_PYARROW_PYTHON=/path/to/python-with-pyarrow \
  -DCMAKE_PREFIX_PATH=/path/to/dependency-prefix
cmake --build /path/to/test-build --parallel 4
/path/to/python-with-pyarrow cpp-engine/tests/make_http_test_fixture.py --output-dir /tmp/longshort-http-fixture
LONGSHORT_TEST_SERVER=/path/to/test-build/longshort-server \
LONGSHORT_TEST_CONFIG=/tmp/longshort-http-fixture/config.yaml \
ctest --test-dir /path/to/test-build --output-on-failure -j 4
```

HTTP 환경변수가 없어서 skip된 경로를 HTTP 통합 검증 통과로 계산하지 않는다. CTest에는 파이썬 검사(`test_longshort_launcher.py`, `test_module_build.py`, `test_meta_weights_oracle.py`)도 등록된다. 오라클 검사는 numpy·scipy·scikit-learn이 없으면 skip(77)한다.

```bash
python3 -m unittest discover -s cpp-engine/tests -p test_longshort_launcher.py -v
python3 -m unittest discover -s cpp-engine/tests -p test_module_build.py -v
```

## 화면과 결과 계약

`frontend/src/types.ts`는 `RunContext`, `AlphaResult`, `SimulationResponse`와 목적별 요청을 정의하고 `api.ts`는 이 계약을 그대로 전송한다. `runStore.ts`는 원래 실행 context를 보존한다. `useAlphaSearch.ts`와 `useResultData.ts`가 검색 스트림·성과·신호 조회를 조율하고, `chart.ts`, `chartStyle.ts`, `format.ts`, `theme.ts`가 표시와 상호작용을 담당한다. Next.js 진입점은 `frontend/src/app/`, 화면 컴포넌트는 `frontend/src/components/`에 둔다.

계약 검증은 소수 비율과 epoch-ms 날짜가 그대로 전달되는지, 엔진 성과 곡선이 IS 시작점에서 기준을 잡고 OS까지 이어지는지, 저장 결과의 context를 상세 조회에도 사용하는지 확인한다. 신호 출력은 `performance`, `signals`, `signalSeries`, `full` 목적별로 필요한 계산과 결과를 요청한다. 히트맵 중심·스케일은 엔진 결과를 사용하고 브라우저는 표시값을 Float32로 반올림해 색상에 적용한다.

## 화면 검증

`frontend/`에서 실행한다.

```bash
npm test
npm run build
npm run lint
npm run test:e2e
```

Playwright는 `PLAYWRIGHT_PORT`, `PLAYWRIGHT_CHROME_PATH`, `PLAYWRIGHT_USE_BUILD`, `PLAYWRIGHT_ENGINE_URL`을 지원한다. 실제 C++ 통합에는 별도 엔진 주소를 지정하고, 저장 fixture 기반 화면 검사와 구분한다.


프런트엔드 테스트 설정은 `tests/vitest.config.ts`, `tests/playwright.config.ts`, 공통 초기화는 `tests/setup.ts`에 있다. npm 검사 명령이 이 경로를 사용한다. 브라우저의 임시 결과와 리포트는 `tests/.artifacts/`에 생성한다.
