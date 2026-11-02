# 빌드 실행과 검증

개인 설정 옆의 모듈을 발견하고 증분 빌드한 뒤 C++ 서버를 실행한다. 화면은 별도 Next.js 프로세스로 실행한다. 연구 검증에서는 기존 공유 서버를 보존하고 별도 포트를 사용한다.

## 서버와 화면 실행

평소에는 루트 `longshort` 스크립트로 띄운다([README](../README.md) 실행 절). 아래는 그 스크립트가 호출하는 실행기를 직접 쓸 때의 설명이다.

```bash
cd ~/longshort_workspace
python3 ~/longshort_platform/cpp-engine/tools/run_engine.py --config config.yaml --port <엔진포트>
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
cd ~/longshort_platform/frontend
npm ci
NEXT_PUBLIC_API_URL="http://SERVER_ADDRESS:<엔진포트>" npm run dev -- --port <화면포트>
```

`SERVER_ADDRESS`는 브라우저가 접근할 수 있는 서버 주소로 바꾼다. Production은 같은 API 주소를 지정해 `npm run build` 후 `npm start`로 실행한다.

## 빌드 의존성과 모듈 발견

C++17 컴파일러와 CMake, Arrow/Parquet, Drogon 및 저장소 CMake가 요구하는 라이브러리가 필요하다. pugixml·yaml-cpp·drogon·googletest·Eigen(3.4.0, 헤더 전용, SHA256 고정)은 configure 때 FetchContent로 내려받으므로 최초 configure에는 네트워크가 필요하다. CMake 패키지를 이미 준비한 경로를 우선 사용한다. PyArrow 번들 Arrow/Parquet를 쓰면 해당 Python을 명시한다.

```bash
python3 cpp-engine/tools/run_engine.py --config /path/to/config.yaml \
  --cmake-define LONGSHORT_PYARROW_PYTHON=/path/to/python-with-pyarrow \
  --cmake-define CMAKE_PREFIX_PATH=/path/to/dependency-prefix
```

플랫폼 루트의 `longshort init`이 이 둘을 `LONGSHORT_DEPS`(기본 `/home/base/longshort_deps`)에 만들고, `longshort start`가 `LONGSHORT_PYARROW_PYTHON=$DEPS/venv/bin/python`, `CMAKE_PREFIX_PATH=$DEPS/prefix`로 넘긴다. 서버에 Arrow C++ 패키지와 jsoncpp·uuid 개발 헤더가 설치돼 있지 않아서(root 없음; 런타임 `libjsoncpp.so.25`·`libuuid.so.1`만 있다) `init`은 `venv/`를 시스템 python3로 만들어 `pip install pyarrow==18.1.0`(운영 엔진이 링크된 버전; `LONGSHORT_PYARROW_VERSION`으로 바꿈)으로 `libarrow`·`libparquet`를 얻고, `prefix/`에는 `apt-get download libjsoncpp-dev libjsoncpp25 uuid-dev libuuid1`을 `dpkg -x`로 풀어 `usr/` 아래를 옮긴다(`include/jsoncpp/json`, `include/uuid`, `lib/x86_64-linux-gnu/{libjsoncpp.so*, libuuid.so*, cmake/jsoncpp}`). `-dev` 패키지만 풀면 `.so` 실체가 런타임 패키지에 있어 CMake가 imported target 파일 부재로 실패하므로 런타임 패키지까지 함께 푼다. 이 배치면 jsoncpp CONFIG 패키지와 Drogon의 FindUUID가 추가 지정 없이 찾는다. 빌드 결과는 venv 안의 `libarrow.so`·`libparquet.so`를 실행 시에도 참조하므로 deps는 모든 사용자가 읽을 수 있는 곳에 있어야 한다. 폴더가 없어도 이미 configure 된 빌드는 CMake 캐시의 경로를 쓴다.

헤더와 라이브러리는 같은 설치에서 가져온다. 사용자 모듈의 `.h/.hpp/.cpp/.cc/.cxx`를 발견하고 빌드 경로의 `generated/`에 registry를 생성한다. 점으로 시작하는 하위 경로는 검색에서 제외된다. 변경 없는 registry는 다시 쓰지 않고, 파일 추가·수정·삭제는 증분 빌드가 반영한다. 모듈 문법과 속성은 [모듈 규약](modules.md)을 따른다.

개인 모듈을 고쳤거나 데이터 캐시가 갱신되었으면 `longshort restart`로 반영한다(빌드 → 중지 → 기동; 빌드 실패 시 기존 엔진 유지). `longshort start --build-only`는 서버를 건드리지 않고 컴파일만 확인한다.

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

HTTP 환경변수가 없어서 skip된 경로를 HTTP 통합 검증 통과로 계산하지 않는다. CTest에는 파이썬 검사(`test_run_engine.py`, `test_generate_modules.py`, `test_module_build.py`, `test_meta_weights_oracle.py`)도 등록된다. 오라클 검사는 numpy·scipy·scikit-learn이 없으면 skip(77)한다.

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

