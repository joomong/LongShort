# 빌드 실행과 검증

개인 설정 옆의 모듈을 발견하고 증분 빌드한 뒤 C++ 서버를 실행한다. 화면은 별도 Next.js 프로세스로 실행한다. 연구 검증에서는 기존 공유 서버를 보존하고 별도 포트를 사용한다.

## 서버와 화면 실행

현재 작업 공간(`/home/jgo/longshort_workspace`)에서 공용 실행기를 호출한다.

```bash
cd /home/jgo/longshort_workspace
python3 /home/jgo/longshort_platform/cpp-engine/tools/run_engine.py --config config.yaml --port 50007
```

기본 모듈 경로는 설정 파일 옆 `modules/`, 빌드 경로는 `.longshort/build/`다. 모듈 추가나 수정은 다음 프로그램 시작 때 반영된다. 모듈 생성이나 빌드가 실패하면 이전 실행 파일로 기동하지 않는다.

알파 저장 위치는 YAML 최상위 `alphas_dir`(기본 `alphas`, 상대경로는 YAML 폴더 기준)이며 시작할 때 생성·쓰기 확인에 실패하면 엔진이 뜨지 않는다. 저장 형식은 `<alphas_dir>/<name>/{alpha.xml, metadata.yml, results/, positions/}`이며, `positions/<id>.parquet`은 체결 후 보유 비중이 아니라 매 거래 bar의 주문 목표 비중(정규화된 target, AlphaPos 대응)이다. `alphas_dir`는 renameat2(RENAME_NOREPLACE)를 지원하는 로컬 파일시스템(ext4/xfs/btrfs/tmpfs)이어야 하며 sshfs/NFS는 시작 시 거부된다. 진행 중인 저장은 자기 `.<name>.tmp-*` 폴더에 flock을 잡고, 시작 시에는 잠금이 풀린 것만 지우므로 여러 엔진이 한 `alphas_dir`를 공유할 수 있다. 진행 중인 저장은 `lslocks`로 보이며, 엔진이 도는 동안 `.tmp-*` 폴더를 손으로 지우지 않는다. 내장 모듈은 플랫폼의 `cpp-engine/src/modules/`에 있다. workspace를 복사할 때 `.longshort/`는 제외하고 새 빌드를 생성한다. YAML의 데이터·카탈로그 상대경로는 YAML 폴더를 기준으로 하므로, 현재 workspace의 AFL 카탈로그는 플랫폼 파일의 절대경로로 지정한다.

HTTP 프레임워크의 큰 요청 처리용 임시 파일은 `.longshort/uploads/`에 생성된다. workspace 루트에 별도 `uploads/`를 만들지 않는다. 연구 노트북과 분석 파일은 workspace의 `research/`에 둔다.

```bash
python3 cpp-engine/tools/run_engine.py \
  --config /path/to/config.yaml \
  --modules-dir /path/to/modules \
  --build-dir /path/to/build \
  --port 8001
```

`--build-only`는 서버를 시작하지 않고 빌드까지만 수행한다. `--jobs`, `--build-type`, `--cmake-define NAME=VALUE`로 빌드를 설정한다. 빌드 디렉터리를 모듈 경로 안에 두거나 같은 빌드 경로에서 동시에 configure/build하지 않는다.

화면은 `frontend/`에서 실행한다.

```bash
cd /home/jgo/longshort_platform/frontend
npm ci
NEXT_PUBLIC_API_URL="http://SERVER_ADDRESS:50007" npm run dev -- --port 50006
```

`SERVER_ADDRESS`는 브라우저가 접근할 수 있는 60서버 주소로 바꾼다. 현재 기동된 웹은 기존 주소와 50006 포트로 사용한다. Production은 같은 API 주소를 지정해 `npm run build` 후 `npm start`로 실행한다.

## 빌드 의존성과 모듈 발견

C++17 컴파일러와 CMake, Arrow/Parquet, Boost, Drogon 및 저장소 CMake가 요구하는 라이브러리가 필요하다. pugixml·yaml-cpp·drogon·googletest·Eigen(3.4.0, 헤더 전용, SHA256 고정)은 configure 때 FetchContent로 내려받으므로 최초 configure에는 네트워크가 필요하다. CMake 패키지를 이미 준비한 경로를 우선 사용한다. PyArrow 번들 Arrow/Parquet를 쓰면 해당 Python을 명시한다.

```bash
python3 cpp-engine/tools/run_engine.py --config /path/to/config.yaml \
  --cmake-define LONGSHORT_PYARROW_PYTHON=/path/to/python-with-pyarrow \
  --cmake-define CMAKE_PREFIX_PATH=/path/to/dependency-prefix
```

헤더와 라이브러리는 같은 설치에서 가져온다. 사용자 모듈의 `.h/.hpp/.cpp/.cc/.cxx`를 발견하고 빌드 경로의 `generated/`에 registry를 생성한다. 점으로 시작하는 하위 경로는 검색에서 제외된다. 변경 없는 registry는 다시 쓰지 않고, 파일 추가·수정·삭제는 증분 빌드가 반영한다. 모듈 문법과 속성은 [모듈 규약](../modules/README.md)을 따른다.

현재 60서버에서 새 workspace를 최초 빌드할 때 쓰는 의존성 설정은 다음과 같다. 의존성은 플랫폼 쪽에 있으며 개인 빌드 결과는 workspace에 생성된다. 첫 configure 이후에는 CMake 캐시에 유지된다. 아래 `cpp-engine/examples/datasets/.longshort/deps/metrics-20261013`은 gitignore된 폴더지만 **현재 운영 엔진 빌드의 의존성 prefix**(jsoncpp·uuid 헤더)다. 예제 폴더 정리 때 지우지 않는다.

```bash
/home/jgo/usr/venv-3.11/bin/python /home/jgo/longshort_platform/cpp-engine/tools/run_engine.py \
  --config /path/to/workspace/config.yaml --build-only --jobs 4 \
  --cmake-define LONGSHORT_PYARROW_PYTHON=/home/jgo/usr/venv-3.11/bin/python \
  --cmake-define CMAKE_PREFIX_PATH=/home/jgo/longshort_platform/cpp-engine/examples/datasets/.longshort/deps/metrics-20261013 \
  --cmake-define UUID_INCLUDE_DIRS=/home/jgo/longshort_platform/cpp-engine/examples/datasets/.longshort/deps/metrics-20261013/usr/include/uuid \
  --cmake-define UUID_LIBRARIES=/usr/lib/x86_64-linux-gnu/libuuid.so.1
```

기동된 현재 엔진의 재시작은 `python3 /home/jgo/longshort_platform/cpp-engine/tools/restart_engine.py --force`로 한다. 기본 설정은 `~/longshort_workspace/config.yaml`, 실행파일은 `.longshort/build/longshort-server`, 로그·PID·lock은 `.longshort/logs/`다. 기본 대기 시간 600초는 최초 데이터 로딩까지 포함한다. 다른 작업 공간에는 `--config`, `--binary`, `--log-dir`를 함께 지정한다.

개인 모듈 변경을 반영할 때는 workspace에서 `run_engine.py --config config.yaml --build-only`를 먼저 실행하고 엔진을 재시작한다. 데이터만 갱신되었을 때는 재시작으로 다시 읽는다.

## C++와 HTTP 검증

테스트 빌드는 별도 경로에 만들고 `LONGSHORT_BUILD_TESTS=ON`으로 설정한다. 실제 실행 결과를 기준으로 통과·실패·skip을 보고한다.

```bash
cmake -S cpp-engine -B /path/to/test-build \
  -DLONGSHORT_BUILD_TESTS=ON \
  -DLONGSHORT_PYARROW_PYTHON=/path/to/python-with-pyarrow \
  -DCMAKE_PREFIX_PATH=/path/to/dependency-prefix
cmake --build /path/to/test-build --parallel 4
python3 cpp-engine/tools/make_http_test_fixture.py --output-dir /tmp/longshort-http-fixture
LONGSHORT_TEST_SERVER=/path/to/test-build/longshort-server \
LONGSHORT_TEST_CONFIG=/tmp/longshort-http-fixture/config.yaml \
ctest --test-dir /path/to/test-build --output-on-failure -j 4
```

HTTP 환경변수가 없어서 skip된 경로를 HTTP 통합 검증 통과로 계산하지 않는다. `run_logic_tests.py`는 Arrow/설정/HTTP를 제외한 가벼운 in-memory 경로다. `--with-tool-tests`는 모듈 생성·기동 도구 검사도 수행한다.

```bash
python3 -m unittest discover -s cpp-engine/tests -p test_run_engine.py -v
python3 -m unittest discover -s cpp-engine/tests -p test_module_build.py -v
```

## 화면 검증

`frontend/`에서 실행한다.

```bash
npm test
npm run build
npm run lint
npm run test:e2e
```

Playwright는 `PLAYWRIGHT_PORT`, `PLAYWRIGHT_CHROME_PATH`, `PLAYWRIGHT_USE_BUILD`, `PLAYWRIGHT_ENGINE_URL`을 지원한다. 실제 C++ 통합에는 별도 엔진 주소를 지정하고, 저장 fixture 기반 화면 검사와 구분한다.

## 데이터 갱신과 연구 검증

데이터는 서버 재시작 때 다시 읽는다. 기본 `end_date: latest`는 선택 종목의 close가 전부 NaN인 끝행만 제외한다. 실행 중 자동 갱신은 하지 않는다. 개인 데이터 설정은 [데이터 계약](data_contract.md)과 [설정 예제](../cpp-engine/examples/datasets/README.md)를 따른다.

원천 데이터를 테스트 성공을 위해 수정하지 않는다. 합성 fixture와 실행 보고서는 `/tmp` 등 별도 출력 경로에 생성한다. 연구 노트북은 설치된 Python 커널에서 Run All로 실행하며, 필요한 외부 패키지는 각 노트북의 import 셀에 명시되어 있다.
